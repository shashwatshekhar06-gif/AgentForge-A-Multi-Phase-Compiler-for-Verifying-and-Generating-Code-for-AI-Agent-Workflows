"""Graph optimizations.

1. Dead agent elimination
   An agent that cannot be reached from `start` can never run, so generating code for it is
   wasted. This is the graph version of dead code elimination in ordinary compilers.

2. Dead LLM call elimination (dead store elimination with liveness analysis)
   If an agent pays for `state.x = llm(...)` but no agent, edge condition or output ever reads
   that value of x afterwards, the call is pure waste: money and time for a result nobody uses.
   Liveness analysis finds these assignments and removes them from the generated code.

   Liveness is a BACKWARD dataflow analysis (definite assignment in verify.py is a forward one):
     LIVE_OUT[n] = union of LIVE_IN over n's successors   (+ fields n's edge conditions test)
     LIVE_IN[n]  = what n's body reads, given LIVE_OUT[n] (computed statement by statement, backwards)
     LIVE_IN[end] = the output fields
   A field is "live" at a point if some path from there may still read its current value.
   The sets only grow and are finite, so iterating until nothing changes always stops (loops too).

   Only assignments without side effects are removed: llm(...) calls, literals and state copies.
   Tool calls are kept even when their result is unused, because a tool may send an email or write
   to a database; removing it would change what the workflow does.
"""

from dataclasses import dataclass, replace

from . import ast_nodes as ast
from .diagnostics import warning
from .ir import IRGraph, calls_in_expr
from .semantic import SymbolTable, state_refs
from .verify import reachable_from


# ---------- 1. dead agents ----------

def eliminate_dead_agents(graph: IRGraph):
    """Returns (optimized_graph, removed_agent_names)."""
    reachable = reachable_from(graph, "start")
    removed = [n.id for n in graph.nodes.values() if n.kind == "agent" and n.id not in reachable]

    optimized = IRGraph()
    for node_id, node in graph.nodes.items():
        if node_id not in removed:
            optimized.nodes[node_id] = node
    optimized.edges = [e for e in graph.edges if e.source not in removed and e.target not in removed]
    return optimized, removed


# ---------- 2. dead LLM calls ----------

@dataclass
class RemovedStore:
    agent: str
    field: str
    line: int
    col: int
    llm_calls: int   # llm(...) calls saved each time this agent runs

    def to_dict(self) -> dict:
        return {"agent": self.agent, "field": self.field, "line": self.line, "llm_calls_saved": self.llm_calls}


def is_pure(expr) -> bool:
    """True if evaluating the expression has no side effects (no tool calls)."""
    return calls_in_expr(expr).tool_max == 0


def reads_of(expr) -> set:
    return {ref.field for ref in state_refs(expr)}


def live_before(statements, live_after: set) -> set:
    """Walk statements backwards: which fields are live before them, given those live after."""
    live = set(live_after)
    for stmt in reversed(statements):
        if isinstance(stmt, ast.Assign):
            target = stmt.target.field
            if target in live or not is_pure(stmt.value):
                # the statement stays: its target is redefined here, its inputs become live
                live = (live - {target}) | reads_of(stmt.value)
            # else: a dead store, it will be removed, so its inputs do NOT become live
        elif isinstance(stmt, ast.If):
            then_live = live_before(stmt.then_body, live)
            else_live = live_before(stmt.else_body, live)
            live = then_live | else_live | reads_of(stmt.condition)
    return live


def prune(statements, live_after: set, agent: str, removed: list) -> list:
    """Return the statements without dead stores, recording what was removed."""
    kept, live = [], set(live_after)
    for stmt in reversed(statements):
        if isinstance(stmt, ast.Assign):
            target = stmt.target.field
            if target in live or not is_pure(stmt.value):
                kept.append(stmt)
                live = (live - {target}) | reads_of(stmt.value)
            else:
                removed.append(RemovedStore(agent, target, stmt.line, stmt.col, calls_in_expr(stmt.value).llm_max))
        elif isinstance(stmt, ast.If):
            then_body = prune(stmt.then_body, live, agent, removed)
            else_body = prune(stmt.else_body, live, agent, removed)
            kept.append(replace(stmt, then_body=then_body, else_body=else_body))
            live = live_before(stmt.then_body, live) | live_before(stmt.else_body, live) | reads_of(stmt.condition)
    kept.reverse()
    return kept


def liveness(graph: IRGraph, symbols: SymbolTable):
    """Backward dataflow to a fixpoint. Returns LIVE_OUT for every node."""
    outputs = {f.name for f in symbols.fields.values() if f.modifier == "output"}
    live_in = {n: set() for n in graph.nodes}
    live_out = {n: set() for n in graph.nodes}
    changed = True
    while changed:
        changed = False
        for node_id, node in graph.nodes.items():
            out = set().union(*(live_in[e.target] for e in graph.out_edges(node_id)))
            for e in graph.out_edges(node_id):
                if e.condition is not None:
                    out |= reads_of(e.condition)   # routing reads these after the agent runs
            new_in = outputs if node.kind == "end" else live_before(node.body, out)
            if out != live_out[node_id] or new_in != live_in[node_id]:
                live_out[node_id], live_in[node_id] = out, set(new_in)
                changed = True
    return live_out


def eliminate_dead_llm_calls(graph: IRGraph, symbols: SymbolTable):
    """Returns (optimized_graph, removed_stores, diagnostics)."""
    live_out = liveness(graph, symbols)
    removed: list[RemovedStore] = []
    optimized = IRGraph(edges=list(graph.edges))
    for node_id, node in graph.nodes.items():
        if node.kind == "agent":
            node = replace(node, body=prune(node.body, live_out[node_id], node_id, removed))
        optimized.nodes[node_id] = node

    diagnostics = []
    for r in removed:
        saving = f"saves {r.llm_calls} LLM call{'s' if r.llm_calls != 1 else ''} per run" if r.llm_calls \
            else "it had no effect"
        diagnostics.append(warning(
            "AF-W307", "optimize",
            f"agent '{r.agent}' writes state.{r.field}, but nothing reads that value afterwards; "
            f"the optimizer removed this assignment ({saving})", r.line, r.col, node=r.agent))
    return optimized, removed, diagnostics

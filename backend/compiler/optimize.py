"""Graph optimizations.

0. Agent selection (profile-guided): when a `compare` block lists several agents for the same job, the one
   that passed the most test cases is wired into the generated code (select_agents below).
   Agent pruning (profile-guided): when a `necessity` block's test cases show the workflow is just as good without
   an agent, that agent is removed from the generated code (prune_unneeded below).

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
from .candidates import swap_candidate
from .necessity import bypass_agent, problems
from .semantic import SymbolTable, state_refs
from .verify import reachable_from


# ---------- 0. agent selection (profile-guided) ----------

def select_agents(graph: IRGraph, reports: list, unsafe: set):
    """Put the best candidate of every `compare` block into the graph, in place of the agent that was wired.

    This is profile-guided optimization for AI workflows: the test cases of the compare block are the profile
    (see evaluate.py), "passes the most cases" is the measurement, and the generated code uses the winner.
    Rules that keep it safe:
      - only blocks with exactly one wired candidate (otherwise it is unclear which one to replace),
      - a tie changes nothing (the agent already in the flow stays),
      - a winner whose swap would add a verification problem (AF-W310) is never applied.
    The reports are updated: `applied` names the winner that was put in, `blocked` says why it was not.
    Returns (graph, selections).
    """
    selections = []
    for report in reports:
        wired = report["wired"]
        best = report["best"]
        if len(wired) != 1 or best == wired[0]:
            continue
        if (report["name"], best) in unsafe:
            report["blocked"] = (f"'{best}' scored higher, but putting it in the flow would add a verification "
                                 f"warning (AF-W310), so '{wired[0]}' stays")
            continue
        graph = swap_candidate(graph, wired[0], best)
        report["applied"] = best
        scores = {s["agent"]: f"{s['passed']}/{s['total']}" for s in report["scores"]}
        selections.append({"compare": report["name"], "replaced": wired[0], "with": best,
                           "scores": scores, "profile": report["mode"]})
    return graph, selections


def prune_unneeded(graph: IRGraph, symbols: SymbolTable, reports: list):
    """Remove the agents a measured `necessity` report found unnecessary: skipping them did not make the final
    output worse on the test cases. Each removal is verified again, and one that would add a verification
    problem is refused. Only MEASURED reports prune anything (a static report has no scores).
    Returns (graph, pruned) and records `applied` / `blocked` in the reports."""
    pruned = []
    for report in reports:
        if report["mode"] != "measured":
            continue
        known = problems(graph, symbols)
        for name in report["removable"]:
            if name not in graph.nodes:
                continue
            trial = bypass_agent(graph, name)
            added = problems(trial, symbols) - known
            if added:
                report["blocked"].append(f"'{name}' was not needed, but removing it would add a verification problem "
                                         f"({sorted(added)[0][1]}), so it stays")
                continue
            graph = trial
            row = next(r for r in report["agents"] if r["agent"] == name)
            row["removed"] = True
            report["applied"].append(name)
            pruned.append({"necessity": report["name"], "agent": name, "llm_calls_saved": row["llm_calls"],
                           "score_with": report["baseline"], "score_without": row["score_without"],
                           "total": report["total"]})
    return graph, pruned


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
            for info in symbols.compares.values():
                if info.decl.ensure is not None and node_id in info.candidates:
                    out |= reads_of(info.decl.ensure)   # the run-time check reads these right after the agent
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

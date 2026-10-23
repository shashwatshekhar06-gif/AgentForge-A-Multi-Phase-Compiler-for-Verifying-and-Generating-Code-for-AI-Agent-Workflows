"""Static verification passes on the IR graph.

Each pass is a separate function that returns (checks, diagnostics):
  - check_reachability   : dead agents (not reachable from start)             AF-W301
                           (candidates of a `compare` block are exempt: they wait outside the flow on purpose)
  - check_cycles         : cycles with no exit and no max_iterations bound    AF-E303
  - check_reach_end      : nodes that can never reach end                     AF-E305
  - check_state_completeness : fields read before they are surely written      AF-E302, AF-E304
  - check_default_edges  : conditional edges without an else edge             AF-W306
  - check_prompt_injection (taint.py): untrusted data reaching a sensitive tool  AF-W309

These checks are structural: they look at the shape of the workflow and at which
fields each agent reads and writes. They say nothing about what an LLM will answer.
"""

from collections import deque

from .diagnostics import error, warning
from .ir import IRGraph
from .semantic import SymbolTable, state_refs


def make_check(name, status, message, path=None) -> dict:
    return {"name": name, "status": status, "message": message, "path": path}


# ---------- graph helpers ----------

def reachable_from(graph: IRGraph, start: str) -> set:
    """Breadth first search forwards from `start`."""
    seen, queue = {start}, deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph.successors(node):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def can_reach(graph: IRGraph, target: str) -> set:
    """All nodes from which `target` can be reached (search backwards from target)."""
    seen, queue = {target}, deque([target])
    while queue:
        node = queue.popleft()
        for prev in graph.predecessors(node):
            if prev not in seen:
                seen.add(prev)
                queue.append(prev)
    return seen


def shortest_path(graph: IRGraph, source: str, target: str, allowed=None):
    """BFS path from source to target, only stepping onto nodes in `allowed` (if given)."""
    parents, queue = {source: None}, deque([source])
    while queue:
        node = queue.popleft()
        if node == target:
            path = []
            while node is not None:
                path.append(node)
                node = parents[node]
            return path[::-1]
        for nxt in graph.successors(node):
            if nxt not in parents and (allowed is None or nxt in allowed):
                parents[nxt] = node
                queue.append(nxt)
    return None


def strongly_connected_components(graph: IRGraph) -> list:
    """Tarjan's algorithm. Returns a list of SCCs, each a list of node ids.

    An SCC is a maximal group of nodes where every node can reach every other one.
    Every cycle in the graph lies completely inside one SCC.
    """
    index_of, lowlink, on_stack = {}, {}, set()
    stack, result, counter = [], [], [0]

    def visit(v):
        index_of[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.successors(v):
            if w not in index_of:
                visit(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])
        if lowlink[v] == index_of[v]:   # v is the root of an SCC: pop it off the stack
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            result.append(component)

    for node in graph.nodes:
        if node not in index_of:
            visit(node)
    return result


# ---------- pass 1: reachability (dead agents) ----------

def check_reachability(graph: IRGraph, exempt: frozenset = frozenset()):
    """`exempt` = agents that are not in the flow on purpose: candidates of a compare block."""
    reachable = reachable_from(graph, "start")
    diagnostics, checks = [], []
    for node in graph.nodes.values():
        if node.kind == "agent" and node.id not in reachable and node.id not in exempt:
            msg = f"agent '{node.id}' can never run: no path from start reaches it (it will be removed)"
            diagnostics.append(warning("AF-W301", "verify", msg, node.line, 1, node=node.id))
            checks.append(make_check("Dead agents", "warn", msg))
    if not checks:
        checks.append(make_check("Dead agents", "pass", "every agent is reachable from start"))
    return checks, diagnostics


# ---------- pass 2: cycles ----------

def check_cycles(graph: IRGraph):
    """A cycle is accepted if it has a max_iterations bound or an edge that leaves it."""
    reachable = reachable_from(graph, "start")
    diagnostics, checks, bad_nodes = [], [], set()

    for component in strongly_connected_components(graph):
        members = set(component)
        inner = [e for e in graph.edges if e.source in members and e.target in members]
        is_cycle = len(component) > 1 or bool(inner)   # a single node only counts with a self loop
        if not is_cycle or not members & reachable:
            continue
        first = next(n for n in graph.nodes if n in members)   # stable order for messages
        cycle_path = shortest_cycle(graph, first, members)
        bounded = any(e.max_iterations is not None for e in inner)
        has_exit = any(e.source in members and e.target not in members for e in graph.edges)
        if bounded or has_exit:
            reason = "bounded by max_iterations" if bounded else "has an exit edge"
            checks.append(make_check("Cycles", "pass", f"cycle {' -> '.join(cycle_path)} {reason}", cycle_path))
        else:
            msg = (f"cycle {' -> '.join(cycle_path)} never exits: add an edge that leaves it "
                   f"or a max_iterations bound")
            node = graph.nodes[first]
            diagnostics.append(error("AF-E303", "verify", msg, node.line, 1, path=cycle_path, node=first))
            checks.append(make_check("Cycles", "fail", msg, cycle_path))
            bad_nodes |= members

    if not checks:
        checks.append(make_check("Cycles", "pass", "no cycles in the workflow"))
    return checks, diagnostics, bad_nodes


def shortest_cycle(graph: IRGraph, node: str, members: set) -> list:
    """A path node -> ... -> node that stays inside the SCC."""
    best = None
    for nxt in graph.successors(node):
        if nxt in members:
            path = shortest_path(graph, nxt, node, allowed=members)
            if path and (best is None or len(path) < len(best)):
                best = path
    return [node] + best if best else [node]


# ---------- pass 3: every node can reach end ----------

def check_reach_end(graph: IRGraph, skip: set = frozenset()):
    reachable = reachable_from(graph, "start")
    reaches_end = can_reach(graph, "end")
    diagnostics, checks = [], []
    for node in graph.nodes.values():
        if node.id in reachable and node.id not in reaches_end and node.id not in skip:
            name = "start" if node.kind == "start" else f"agent '{node.id}'"
            msg = f"{name} can never reach end (the workflow would get stuck or stop early)"
            diagnostics.append(error("AF-E305", "verify", msg, node.line or 1, 1, node=node.id))
            checks.append(make_check("Reaches end", "fail", msg))
    if not checks:
        checks.append(make_check("Reaches end", "pass", "every reachable node has a path to end"))
    return checks, diagnostics


# ---------- pass 4: state completeness (definite assignment) ----------

def initially_written(symbols: SymbolTable) -> set:
    """Fields that already have a value at start: inputs and fields with a default."""
    return {f.name for f in symbols.fields.values() if f.modifier == "input" or f.has_default}


def definitely_written(graph: IRGraph, symbols: SymbolTable):
    """Forward dataflow analysis.

    IN[n]  = fields written on EVERY path from start to n (before n runs)
    OUT[n] = IN[n] plus the fields n surely writes

    IN[n] is the intersection of OUT over n's predecessors. We start every node
    (except start) at "all fields" and keep shrinking until nothing changes.
    Sets can only shrink and are finite, so this always stops, even with loops.
    """
    reachable = reachable_from(graph, "start")
    all_fields = set(symbols.fields)
    order = [n for n in graph.nodes if n in reachable]
    IN = {n: set(all_fields) for n in order}
    IN["start"] = initially_written(symbols)
    OUT = {n: IN[n] | set(graph.nodes[n].must_writes) for n in order}

    changed = True
    while changed:
        changed = False
        for n in order:
            if n == "start":
                continue
            preds = [p for p in graph.predecessors(n) if p in reachable]
            new_in = set.intersection(*(OUT[p] for p in preds)) if preds else set(all_fields)
            new_out = new_in | set(graph.nodes[n].must_writes)
            if new_in != IN[n] or new_out != OUT[n]:
                IN[n], OUT[n] = new_in, new_out
                changed = True
    return IN, OUT


def witness_path(graph: IRGraph, field_name: str, target: str):
    """A concrete path start -> ... -> target on which nothing writes `field_name` (before target)."""
    reachable = reachable_from(graph, "start")
    # we may walk through nodes that do not surely write the field; the target itself is always allowed
    allowed = {n for n in reachable if field_name not in graph.nodes[n].must_writes} | {target}
    return shortest_path(graph, "start", target, allowed) or [target]


def check_state_completeness(graph: IRGraph, symbols: SymbolTable):
    IN, OUT = definitely_written(graph, symbols)
    diagnostics, checks = [], []

    for node_id in IN:
        node = graph.nodes[node_id]
        # 1. an agent reads a field that may still be empty
        for f in node.reads:
            if f not in IN[node_id]:
                path = witness_path(graph, f, node_id)
                msg = (f"agent '{node_id}' reads state.{f}, but on the path {' -> '.join(path)} "
                       f"nothing writes it first")
                diagnostics.append(error("AF-E302", "verify", msg, node.line, 1, path=path, node=node_id))
                checks.append(make_check("State completeness", "fail", msg, path))
        # 2. an edge condition leaving this node reads a field that may still be empty
        for edge in graph.out_edges(node_id):
            if edge.condition is None:
                continue
            for f in dict.fromkeys(r.field for r in state_refs(edge.condition)):
                if f in symbols.fields and f not in OUT[node_id]:
                    path = witness_path(graph, f, node_id)
                    msg = (f"edge {edge.source} -> {edge.target} tests state.{f}, but on the path "
                           f"{' -> '.join(path)} nothing writes it first")
                    diagnostics.append(error("AF-E302", "verify", msg, edge.line, edge.col, path=path,
                                             node=node_id))
                    checks.append(make_check("State completeness", "fail", msg, path))

    # 3. every output field must be written before end
    if "end" in IN:
        for f in symbols.fields.values():
            if f.modifier == "output" and f.name not in IN["end"]:
                path = witness_path(graph, f.name, "end")
                msg = f"output state.{f.name} is not written on the path {' -> '.join(path)}"
                diagnostics.append(error("AF-E304", "verify", msg, f.line, f.col, path=path, node="end"))
                checks.append(make_check("Outputs written", "fail", msg, path))

    if not any(c["name"] == "State completeness" for c in checks):
        checks.append(make_check("State completeness", "pass",
                                 "every field is written on every path before it is read"))
    if not any(c["name"] == "Outputs written" for c in checks):
        checks.append(make_check("Outputs written", "pass", "every output field is written on every path to end"))
    return checks, diagnostics


# ---------- pass 5: else edges ----------

def check_default_edges(graph: IRGraph):
    reachable = reachable_from(graph, "start")
    diagnostics, checks = [], []
    for node_id in graph.nodes:
        out = graph.out_edges(node_id)
        conditional = [e for e in out if e.condition is not None]
        if node_id in reachable and conditional and not any(e.is_default for e in out):
            msg = f"'{node_id}' has conditional edges but no -[else]-> edge; if no condition matches, the run stops with an error"
            first = conditional[0]
            diagnostics.append(warning("AF-W306", "verify", msg, first.line, first.col, node=node_id))
            checks.append(make_check("Else edges", "warn", msg))
    if not checks:
        checks.append(make_check("Else edges", "pass", "every branching node has an else edge"))
    return checks, diagnostics


# ---------- run all passes ----------

def verify(graph: IRGraph, symbols: SymbolTable):
    """Run every pass. Returns (checks, diagnostics)."""
    checks, diagnostics = [], []

    c, d = check_reachability(graph, frozenset(symbols.candidate_names()))
    checks += c
    diagnostics += d

    c, d, stuck_in_cycle = check_cycles(graph)
    checks += c
    diagnostics += d

    # nodes that can only end up in an endless cycle are already explained by AF-E303
    skip = set()
    for node in stuck_in_cycle:
        skip |= can_reach(graph, node)
    c, d = check_reach_end(graph, skip=skip)
    checks += c
    diagnostics += d

    c, d = check_state_completeness(graph, symbols)
    checks += c
    diagnostics += d

    c, d = check_default_edges(graph)
    checks += c
    diagnostics += d

    from .taint import check_prompt_injection   # imported here: taint.py itself uses helpers from this file
    c, d = check_prompt_injection(graph, symbols)
    checks += c
    diagnostics += d

    return checks, diagnostics

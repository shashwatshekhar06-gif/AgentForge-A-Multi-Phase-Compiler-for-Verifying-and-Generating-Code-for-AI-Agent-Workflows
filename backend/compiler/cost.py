"""Static cost analysis: how many LLM and tool calls can ONE run of the workflow make?

LLM calls are what cost money, so knowing the worst case before running anything lets a team
cap its spend. The idea comes from worst-case execution time (WCET) analysis, used for
real-time and embedded programs, where loops must have known bounds.

Worst case (an upper bound that is always safe):
  1. Group the graph into strongly connected components (SCCs). Every loop lies inside one SCC.
  2. A loop with no max_iterations edge has no bound: the number of calls is unbounded (AF-W308).
     (Test: remove the bounded edges from the SCC; if a cycle remains, it is unbounded.)
  3. Otherwise every trip around a loop uses up one traversal of a bounded edge. The generated code
     allows each bounded edge at most N times per run, so the SCC is passed through at most
     1 + (sum of its N's) times, visiting each of its agents at most once per pass.
  4. With each SCC collapsed into one weighted node, the graph has no cycles, so the most expensive
     path from start to end is a simple longest path computation in topological order.

Best case: the cheapest path from start to end, going around no loop (Dijkstra's shortest path
algorithm; call counts are never negative, which is exactly what Dijkstra needs).
Inside an agent an `if` counts its most expensive branch for the worst case and its cheapest for the best.
"""

import heapq

from .diagnostics import warning
from .ir import IRGraph, count_calls
from .verify import can_reach, reachable_from, shortest_cycle, strongly_connected_components

INF = float("inf")


def has_cycle(nodes: set, edges: list) -> bool:
    """Depth first search for a cycle using only the given nodes and edges."""
    succ = {n: [e.target for e in edges if e.source == n and e.target in nodes] for n in nodes}
    state = {}

    def visit(u) -> bool:
        state[u] = "visiting"
        for v in succ[u]:
            if state.get(v) == "visiting" or (v not in state and visit(v)):
                return True
        state[u] = "done"
        return False

    return any(n not in state and visit(n) for n in nodes)


def analyze_cost(graph: IRGraph, removed_stores=()):
    """Returns (report, diagnostics). `removed_stores` comes from dead LLM call elimination."""
    calls = {n: count_calls(node.body) for n, node in graph.nodes.items()}
    on_some_run = reachable_from(graph, "start") & can_reach(graph, "end")

    # ---- worst case: SCCs -> bounded number of passes
    sccs = strongly_connected_components(graph)          # Tarjan returns them sinks first
    scc_of = {n: i for i, comp in enumerate(sccs) for n in comp}
    passes, diagnostics, unbounded_loops = {}, [], []
    for i, comp in enumerate(sccs):
        members = set(comp)
        inner = [e for n in comp for e in graph.out_edges(n) if e.target in members]
        if not inner:
            passes[i] = 1                                    # no loop here
            continue
        bounded = [e for e in inner if e.max_iterations is not None]
        if has_cycle(members, [e for e in inner if e.max_iterations is None]):
            passes[i] = INF
            makes_calls = any(calls[n].llm_max or calls[n].tool_max for n in members)
            if members & on_some_run and makes_calls:   # a loop that costs nothing is not a cost risk
                first = next(n for n in graph.nodes if n in members)
                path = shortest_cycle(graph, first, members)
                unbounded_loops.append(path)
                node = graph.nodes[first]
                diagnostics.append(warning(
                    "AF-W308", "cost",
                    f"loop {' -> '.join(path)} has an exit but no max_iterations, so the number of LLM calls "
                    f"in one run has no upper bound; add max_iterations to cap the cost", node.line or 1, 1,
                    path=path, node=first))
        else:
            passes[i] = 1 + sum(e.max_iterations for e in bounded)

    def worst(kind: str):
        # longest path over the SCC graph, in topological order (reverse of Tarjan's output)
        weight = {}
        for i, comp in enumerate(sccs):
            total = sum(getattr(calls[n], kind + "_max") for n in comp)
            weight[i] = passes[i] * total if total else 0   # (an unbounded loop that makes no calls costs 0)
        best_to = {i: -INF for i in range(len(sccs))}
        best_to[scc_of["start"]] = weight[scc_of["start"]]
        for i in reversed(range(len(sccs))):
            if best_to[i] == -INF:
                continue
            for n in sccs[i]:
                for e in graph.out_edges(n):
                    j = scc_of[e.target]
                    if j != i:
                        best_to[j] = max(best_to[j], best_to[i] + weight[j])
        result = best_to[scc_of["end"]]
        return None if result in (INF, -INF) else int(result)

    def best(kind: str):
        # Dijkstra: cheapest path start -> end, each agent counted with its cheapest branch
        cost = {n: INF for n in graph.nodes}
        cost["start"] = 0
        queue = [(0, "start")]
        while queue:
            c, n = heapq.heappop(queue)
            if c > cost[n]:
                continue   # an outdated queue entry
            for e in graph.out_edges(n):
                new = c + getattr(calls[e.target], kind + "_min")
                if new < cost[e.target]:
                    cost[e.target] = new
                    heapq.heappush(queue, (new, e.target))
        return None if cost["end"] == INF else int(cost["end"])

    def max_visits(n):
        p = passes[scc_of[n]]
        return None if p == INF else p

    agents = [{"id": n, "llm_min": calls[n].llm_min, "llm_max": calls[n].llm_max,
               "tool_min": calls[n].tool_min, "tool_max": calls[n].tool_max,
               "max_visits": max_visits(n)}
              for n, node in graph.nodes.items() if node.kind == "agent" and n in on_some_run]
    saved = sum(r.llm_calls * (max_visits(r.agent) or 1) for r in removed_stores if r.agent in on_some_run)

    report = {
        "worst": {"llm": worst("llm"), "tool": worst("tool")},
        "best": {"llm": best("llm"), "tool": best("tool")},
        "agents": agents,
        "unbounded_loops": unbounded_loops,
        "removed_llm_calls": saved,   # worst-case LLM calls per run saved by dead LLM call elimination
    }
    return report, diagnostics

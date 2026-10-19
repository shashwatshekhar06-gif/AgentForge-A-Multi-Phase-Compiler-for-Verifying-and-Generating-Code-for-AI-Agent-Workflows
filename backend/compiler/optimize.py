"""Graph optimization: dead agent elimination.

An agent that cannot be reached from `start` can never run, so generating code
for it is wasted. This pass returns a copy of the IR without those agents (and
without their edges), plus the list of removed names.
This is the graph version of dead code elimination in ordinary compilers.
"""

from .ir import IRGraph
from .verify import reachable_from


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

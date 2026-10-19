from compiler.optimize import eliminate_dead_agents
from compiler.verify import (check_cycles, check_default_edges, check_reach_end, check_reachability,
                               check_state_completeness, definitely_written, strongly_connected_components)

from helpers import codes, graph_of

HEADER = """
state S {
  input q: string
  x: string
  done: bool = false
  output a: string
}
agent p { state.x = "p" }
agent r { state.a = state.x }
agent w { state.a = "w" }
"""


def graph(flow):
    return graph_of(HEADER + "flow f {\n" + flow + "\n}")


# ---------- reachability ----------

def test_dead_agent_is_reported_and_removed():
    g, _ = graph("start -> w -> end\np -> end")
    checks, diags = check_reachability(g)
    # p has an edge but nothing reaches it; r is not used in the flow at all
    assert codes(diags) == ["AF-W301", "AF-W301"]
    assert [d.node for d in diags] == ["p", "r"]
    optimized, removed = eliminate_dead_agents(g)
    assert removed == ["p", "r"]   # r is not used in the flow at all
    assert "p" not in optimized.nodes
    assert all(e.source != "p" for e in optimized.edges)


def test_node_that_cannot_reach_end():
    g, _ = graph("start -> p\np -[state.done]-> w\np -[else]-> end\nw -> r")   # r has no way out
    _, diags = check_reach_end(g)
    assert sorted(d.node for d in diags) == ["r", "w"]


# ---------- cycles ----------

def test_scc_finds_the_cycle():
    g, _ = graph("start -> p -> w\nw -[state.done]-> end\nw -[else]-> p")
    sccs = [sorted(c) for c in strongly_connected_components(g) if len(c) > 1]
    assert sccs == [["p", "w"]]


def test_cycle_with_exit_is_ok():
    g, _ = graph("start -> p -> w\nw -[state.done]-> end\nw -[else]-> p")
    checks, diags, bad = check_cycles(g)
    assert diags == [] and bad == set()


def test_bounded_cycle_is_ok():
    g, _ = graph("start -> p\np -[not state.done, max_iterations 3]-> p\np -[else]-> w\nw -> end")
    _, diags, _ = check_cycles(g)
    assert diags == []


def test_unbounded_cycle_with_no_exit():
    g, _ = graph("start -> p -> w\nw -[state.done]-> p\nw -[else]-> p")
    _, diags, bad = check_cycles(g)
    assert codes(diags) == ["AF-E303"]
    assert diags[0].path == ["p", "w", "p"]
    assert bad == {"p", "w"}


# ---------- state completeness ----------

def test_read_after_write_on_every_path_is_ok():
    g, symbols = graph("start -> p -> r -> end")
    _, diags = check_state_completeness(g, symbols)
    assert diags == []


def test_read_before_write_on_one_branch():
    g, symbols = graph("start -> w\nw -[state.done]-> p\nw -[else]-> r\np -> r\nr -> end")
    _, diags = check_state_completeness(g, symbols)
    assert codes(diags) == ["AF-E302"]
    assert diags[0].path == ["start", "w", "r"]   # the witness path that skips p


def test_written_on_both_branches_is_ok():
    g, symbols = graph_of(HEADER + "agent p2 { state.x = \"p2\" }\n"
                          "flow f { start -> w\nw -[state.done]-> p\nw -[else]-> p2\np -> r\np2 -> r\nr -> end }")
    _, diags = check_state_completeness(g, symbols)
    assert diags == []


def test_missing_output_on_one_path():
    g, symbols = graph("start -> p\np -[state.done]-> w\np -[else]-> end\nw -> end")
    _, diags = check_state_completeness(g, symbols)
    assert codes(diags) == ["AF-E304"]
    assert diags[0].path == ["start", "p", "end"]


def test_edge_condition_reading_unwritten_field():
    g, symbols = graph('start -> w\nw -[state.x == "p"]-> end\nw -[else]-> end')
    _, diags = check_state_completeness(g, symbols)
    assert codes(diags) == ["AF-E302"]


def test_dataflow_terminates_on_loops():
    g, symbols = graph("start -> w\nw -[not state.done, max_iterations 5]-> p\nw -[else]-> r\np -> w\nr -> end")
    IN, _ = definitely_written(g, symbols)
    # x is written by p, but the path start -> w -> r skips p
    assert "x" not in IN["r"]
    assert {"q", "done"} <= IN["r"]


# ---------- else edges ----------

def test_missing_else_warning():
    g, _ = graph("start -> p\np -[state.done]-> w\np -[not state.done]-> w\nw -> end")
    _, diags = check_default_edges(g)
    assert codes(diags) == ["AF-W306"]

from compiler.semantic import analyze

from helpers import analyze_source, codes, load_fixture

STATE = """
state S {
  input q: string
  n: int
  flag: bool
  items: list[string]
  route: string
  output a: string
}
tool search(query: string) -> list[string]
tool count(items: list[string], limit: int) -> int
"""


def check(agent_body, flow="start -> x -> end"):
    """Analyze STATE + one agent `x` + a flow, return the diagnostic codes."""
    _, diags = analyze_source(STATE + "agent x {\n" + agent_body + "\n}\nflow f {\n" + flow + "\n}")
    return codes(diags)


def test_works_from_hand_written_ast_fixture():
    symbols, diags = analyze(load_fixture("minimal.json"))
    assert diags == []
    assert symbols.agents["answer"].reads == ["q"]
    assert symbols.agents["answer"].writes == ["a"]


def test_valid_program_has_no_errors():
    assert check('state.items = search(state.q)\nstate.n = count(state.items, 3)\nstate.a = "done"') == []


def test_undeclared_tool():
    assert check("state.items = find(state.q)") == ["AF-E201"]


def test_undeclared_state_field():
    assert check("state.a = state.missing") == ["AF-E202"]


def test_type_mismatch_in_assignment():
    assert check("state.n = state.q") == ["AF-E203"]


def test_llm_returns_string_and_one_of_type():
    assert check('state.n = llm("count")') == ["AF-E203"]
    assert check('state.n = llm("pick", state.q) one_of [1, 2]') == []


def test_comparison_types():
    assert check('state.flag = state.n > 3') == []
    assert check('state.flag = state.q > 3') == ["AF-E203"]
    assert check('state.flag = state.q == 3') == ["AF-E203"]


def test_tool_argument_count_and_type():
    assert check("state.n = count(state.items)") == ["AF-E206"]
    assert check("state.n = count(state.items, state.q)") == ["AF-E206"]


def test_condition_must_be_bool():
    assert check('if (state.q) { state.a = "x" }') == ["AF-E207"]
    assert check('state.a = "x"', flow="start -> x\nx -[state.n]-> end\nx -[else]-> end") == ["AF-E207"]


def test_unknown_agent_in_flow():
    assert check('state.a = "x"', flow="start -> x -> y -> end") == ["AF-E204"]


def test_duplicate_declarations():
    _, diags = analyze_source("state S { a: int\n a: int }\nagent x { }\nagent x { }\nflow f { start -> x -> end }")
    assert codes(diags) == ["AF-E205", "AF-E205"]


def test_missing_declarations():
    _, diags = analyze_source("agent x { }")
    assert codes(diags) == ["AF-E209", "AF-E209"]   # no state, no flow


def test_start_and_end_misuse():
    assert check('state.a = "x"', flow="start -> x -> end\nx -> start") == ["AF-E210", "AF-E212"]


def test_calls_not_allowed_in_edge_conditions():
    flow = 'start -> x\nx -[llm("ok?") == "yes"]-> end\nx -[else]-> end'
    assert check('state.a = "x"', flow=flow) == ["AF-E211"]


def test_ambiguous_edges():
    assert check('state.a = "x"', flow="start -> x\nx -> end\nx -> x") == ["AF-E212"]
    assert check('state.a = "x"', flow="start -> x\nx -> end\nx -[state.flag]-> x") == ["AF-E212"]


def test_one_of_typo_warning():
    body = 'state.route = llm("pick", state.q) one_of ["factual", "chat"]\nstate.a = "x"'
    flow = 'start -> x\nx -[state.route == "factul"]-> end\nx -[else]-> end'
    assert check(body, flow=flow) == ["AF-W208"]


def test_read_and_write_sets_are_inferred():
    symbols, _ = analyze_source(STATE + """
agent x {
  state.items = search(state.q)
  state.n = count(state.items, 1)
  if (state.flag) { state.a = "yes" } else { state.a = "no"
                                             state.route = "r" }
}
flow f { start -> x -> end }""")
    agent = symbols.agents["x"]
    # `items` is read after x wrote it itself, so it is not an outside read
    assert agent.reads == ["q", "flag"]
    assert agent.writes == ["items", "n", "a", "route"]
    # `route` is only written in the else branch, so it is not a must-write
    assert agent.must_writes == ["items", "n", "a"]

import json

from compiler import ast_nodes as ast
from compiler.lexer import tokenize
from compiler.parser import parse

from helpers import EXAMPLES_DIR, TESTS_DIR, parse_source


def parse_with_errors(source):
    program, diags = parse(tokenize(source)[0])
    return program, [d.code for d in diags]


def first_expr(expr_source):
    program = parse_source("agent a { state.x = " + expr_source + " }")
    return program.declarations[0].body[0].value


def test_research_example_matches_fixture():
    program = parse_source((EXAMPLES_DIR / "valid" / "research.af").read_text())
    expected = json.loads((TESTS_DIR / "fixtures" / "ast" / "research.json").read_text())
    assert program.to_dict() == expected


def test_to_dict_from_dict_round_trip():
    program = parse_source((EXAMPLES_DIR / "valid" / "fraud.af").read_text())
    assert ast.from_dict(program.to_dict()) == program


def test_state_fields_modifiers_types_and_defaults():
    program = parse_source('state S { input a: string\n output b: list[list[int]]\n c: bool = true\n d: list[string] = ["x"] }')
    a, b, c, d = program.declarations[0].fields
    assert (a.modifier, a.field_type) == ("input", "string")
    assert (b.modifier, b.field_type) == ("output", "list[list[int]]")
    assert c.default.value is True
    assert [x.value for x in d.default] == ["x"]


def test_precedence_or_and_not_comparison():
    e = first_expr("not state.a == 1 and state.b or state.c")
    # ((not (a == 1)) and b) or c
    assert e.op == "or"
    assert e.left.op == "and"
    assert isinstance(e.left.left, ast.UnaryOp)
    assert e.left.left.operand.op == "=="


def test_parentheses_override_precedence():
    e = first_expr("state.a and (state.b or state.c)")
    assert e.op == "and" and e.right.op == "or"


def test_llm_call_with_one_of():
    e = first_expr('llm("Pick", state.q) one_of ["a", "b"]')
    assert isinstance(e, ast.LlmCall)
    assert e.prompt == "Pick" and len(e.args) == 1
    assert [o.value for o in e.one_of] == ["a", "b"]


def test_if_else_if():
    program = parse_source("agent a { if (state.x) { state.y = 1 } else if (state.z) { state.y = 2 } else { state.y = 3 } }")
    stmt = program.declarations[0].body[0]
    assert isinstance(stmt.else_body[0], ast.If)
    assert len(stmt.else_body[0].else_body) == 1


def test_edge_chain_and_labels():
    program = parse_source("flow f { start -> a -[state.ok, max_iterations 3]-> a\n a -[else]-> end }")
    edges = program.declarations[0].edges
    assert [(e.source, e.target) for e in edges] == [("start", "a"), ("a", "a"), ("a", "end")]
    assert edges[1].max_iterations == 3 and edges[1].condition.field == "ok"
    assert edges[2].is_default


def test_closing_bracket_and_arrow_may_be_separated():
    program = parse_source("flow f { start -[state.ok] -> end }")
    assert program.declarations[0].edges[0].condition is not None


def test_panic_mode_reports_several_errors():
    source = 'state S { input q string }\nagent a { state.x = llm("p" state.q) }\nflow f { start -> a -> end }'
    program, codes = parse_with_errors(source)
    assert codes == ["AF-E101", "AF-E101"]
    # recovery continued, so the flow after the broken parts was still parsed
    assert isinstance(program.declarations[-1], ast.FlowDecl)


def test_missing_closing_brace():
    _, codes = parse_with_errors("agent a { state.x = 1\nflow f { start -> end }")
    assert codes == ["AF-E102"]


def test_bare_name_is_not_a_value():
    _, codes = parse_with_errors("agent a { state.x = foo }")
    assert codes == ["AF-E101"]

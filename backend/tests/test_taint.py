"""Tests for prompt-injection taint analysis (AF-W309)."""

from compiler import ast_nodes as ast
from compiler.lexer import tokenize
from compiler.parser import parse
from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR

TOOLS = """
tool fetch(url: string) -> string untrusted
tool clean(text: string) -> string sanitizer
tool send(to: string, body: string) -> bool sensitive
tool log(text: string) -> bool
"""


def prog(decls, flow, fields=""):
    return (f"state S {{ input q: string\n input untrusted msg: string\n output a: string\n"
            f" page: string\n x: string\n ok: bool = false\n {fields} }}\n{TOOLS}\n{decls}\nflow f {{ {flow} }}")


def findings(source):
    result = compile_source(source)
    assert result["phases"]["verify"]["status"] == "ok", result["diagnostics"]
    return [d for d in result["diagnostics"] if d["code"] == "AF-W309"]


def test_parser_reads_tags():
    program, diags = parse(tokenize(TOOLS + "state S { input untrusted m: string }")[0])
    assert diags == []
    tools = [d for d in program.declarations if isinstance(d, ast.ToolDecl)]
    assert [t.tags for t in tools] == [["untrusted"], ["sanitizer"], ["sensitive"], []]
    assert program.declarations[-1].fields[0].untrusted is True


def test_untrusted_tool_result_reaching_sink():
    found = findings(prog('agent w { state.page = fetch("u")\n state.ok = send("boss", state.page)\n state.a = "k" }',
                          "start -> w -> end"))
    assert len(found) == 1
    assert "fetch()" in found[0]["message"] and "'send' (argument 'body')" in found[0]["message"]


def test_taint_flows_through_llm_and_agents():
    decls = ('agent r { state.page = fetch("u") }\nagent s { state.x = llm("summarise", state.page) }\n'
             'agent m { state.ok = send("boss", state.x)\n state.a = "k" }')
    found = findings(prog(decls, "start -> r -> s -> m -> end"))
    assert len(found) == 1
    assert found[0]["path"] == ["start", "r", "s", "m"]
    assert "state.x (s, line" in found[0]["message"]


def test_untrusted_input_field_is_a_source():
    found = findings(prog('agent w { state.ok = send("boss", llm("reply to", state.msg))\n state.a = "k" }', "start -> w -> end"))
    assert len(found) == 1 and "input msg" in found[0]["message"]


def test_trusted_input_is_not_a_source():
    assert findings(prog('agent w { state.ok = send("boss", state.q)\n state.a = "k" }', "start -> w -> end")) == []


def test_sanitizer_cleans():
    decls = 'agent w { state.page = fetch("u")\n state.x = clean(state.page)\n state.ok = send("boss", state.x)\n state.a = "k" }'
    assert findings(prog(decls, "start -> w -> end")) == []


def test_non_sensitive_tool_is_fine():
    assert findings(prog('agent w { state.ok = log(state.msg)\n state.a = "k" }', "start -> w -> end")) == []


def test_overwriting_with_clean_data_removes_taint():
    decls = 'agent w { state.x = state.msg\n state.x = "constant"\n state.ok = send("boss", state.x)\n state.a = "k" }'
    assert findings(prog(decls, "start -> w -> end")) == []


def test_tainted_on_one_branch_is_enough():
    # union at merges: one tainted path means the value MAY be tainted
    decls = ('agent t { state.x = state.msg }\nagent c { state.x = "safe" }\nagent m { state.ok = send("boss", state.x)\n state.a = "k" }\n'
             'agent hub { }')
    flow = 'start -> hub\nhub -[state.q == "a"]-> t\nhub -[else]-> c\nt -> m\nc -> m\nm -> end'
    found = findings(prog(decls, flow))
    assert len(found) == 1 and found[0]["path"] == ["start", "hub", "t", "m"]


def test_taint_inside_if_branch():
    decls = 'agent w { if (state.q == "a") { state.x = state.msg } else { state.x = "safe" }\n state.ok = send("boss", state.x)\n state.a = "k" }'
    assert len(findings(prog(decls, "start -> w -> end"))) == 1


def test_taint_around_a_loop():
    # x becomes tainted in the second agent and is sent by the first on the next trip around the loop
    decls = ('agent a1 { state.ok = send("boss", state.x)\n state.a = "k" }\nagent a2 { state.x = state.msg }')
    flow = 'start -> a1\na1 -[state.q == "again", max_iterations 2]-> a2\na1 -[else]-> end\na2 -> a1'
    found = findings(prog(decls, flow, fields='y: string = ""').replace(' x: string\n', ' x: string = ""\n'))
    assert len(found) == 1


def test_bool_decisions_derived_from_untrusted_text_are_tainted():
    # an attacker who controls msg can flip the yes/no decision, so the bool is tainted too
    decls = ('tool pay(approved: bool) -> bool sensitive\n'
             'agent w { state.ok = llm("Approve this request?", state.msg) == "yes"\n'
             ' state.paid = pay(state.ok)\n state.a = "k" }')
    found = findings(prog(decls, "start -> w -> end", fields="paid: bool = false"))
    assert len(found) == 1 and "'pay' (argument 'approved')" in found[0]["message"]


def test_example_file():
    result = compile_source((EXAMPLES_DIR / "invalid" / "prompt_injection.af").read_text(encoding="utf-8"))
    found = [d for d in result["diagnostics"] if d["code"] == "AF-W309"]
    assert [("send_email" in d["message"], "issue_refund" in d["message"]) for d in found] == [(True, False), (False, True)]
    check = [c for c in result["phases"]["verify"]["checks"] if c["name"] == "Prompt injection"]
    assert {c["status"] for c in check} == {"warn"}


def test_no_sensitive_tools_means_nothing_to_check():
    result = compile_source((EXAMPLES_DIR / "valid" / "research.af").read_text(encoding="utf-8"))
    check = next(c for c in result["phases"]["verify"]["checks"] if c["name"] == "Prompt injection")
    assert check["status"] == "pass" and "no tools are marked sensitive" in check["message"]

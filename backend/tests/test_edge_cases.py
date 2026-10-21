"""Edge cases across all phases.

Three kinds of checks:
  - the compiler never crashes, and reports the right diagnostic code,
  - every program that compiles produces Python that is syntactically valid,
  - unusual but valid programs actually RUN under LangGraph and take the expected path.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

from compiler.lexer import tokenize
from compiler.pipeline import compile_source

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
needs_langgraph = pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph not installed")


def prog(fields="", decls='agent x { state.a = llm("p", state.q) }', flow="start -> x -> end"):
    """A small program: input q, output a, plus extra fields / declarations / flow."""
    return f"state S {{ input q: string\n output a: string\n {fields} }}\n{decls}\nflow f {{ {flow} }}"


def compile_ok(source):
    result = compile_source(source)
    assert result["ok"], result["diagnostics"]
    code = result["phases"]["codegen"]["code"]
    compile(code, "workflow.py", "exec")   # generated code must at least be valid Python
    return code


def codes_of(source):
    return [d["code"] for d in compile_source(source)["diagnostics"]]


def run_generated(code, tmp_path, env=None):
    path = tmp_path / "workflow.py"
    path.write_text(code, encoding="utf-8")
    out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {})), encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout


# ---------------------------------------------------------------- lexer

class TestLexerEdges:
    def test_empty_and_comment_only_sources(self):
        for source in ["", "   \n\t ", "# only a comment", "# a\n# b\n"]:
            tokens, diags = tokenize(source)
            assert [t.type.name for t in tokens] == ["EOF"] and diags == []
            # semantic analysis then reports the three missing declarations
            assert codes_of(source) == ["AF-E209", "AF-E209", "AF-E209"]

    def test_windows_line_endings(self):
        source = prog().replace("\n", "\r\n")
        compile_ok(source)
        tokens, _ = tokenize(source)
        assert next(t for t in tokens if t.lexeme == "output").line == 2

    def test_columns_count_tabs_as_one(self):
        tokens, _ = tokenize("\tagent")
        assert tokens[0].col == 2

    def test_non_ascii_letters_and_digits_are_rejected(self):
        # Python's isdigit() accepts '²', which used to crash int(); isalpha() accepts 'é',
        # which could leak into generated Python names
        assert codes_of(prog(fields="n: int = ²")) == ["AF-E001"]
        assert "AF-E001" in codes_of(prog(decls="agent café { }", flow="start -> café -> end"))

    def test_unicode_is_fine_inside_strings(self):
        code = compile_ok(prog(decls='agent x { state.a = llm("नमस्ते 😀 é", state.q) }'))
        assert "नमस्ते" in code or "\\u" in code

    def test_string_escapes(self):
        tokens, diags = tokenize(r'"a\"b\\c\nd\te\q"')
        assert diags == []
        assert tokens[0].lexeme == 'a"b\\c\nd\te\\q'   # unknown escapes like \q are kept as written

    def test_newline_inside_string_is_unterminated(self):
        assert codes_of('agent x { state.a = "abc\n" }') == ["AF-E002", "AF-E002"]

    def test_lone_minus_and_bang(self):
        _, diags = tokenize("a - b ! c")
        assert [d.code for d in diags] == ["AF-E001", "AF-E001"]

    def test_huge_integer(self):
        compile_ok(prog(fields="n: int = 99999999999999999999999999"))

    def test_keyword_prefix_is_an_identifier(self):
        tokens, _ = tokenize("agents flows state_x endless")
        assert [t.type.name for t in tokens[:-1]] == ["IDENT"] * 4


# ---------------------------------------------------------------- parser

class TestParserEdges:
    @pytest.mark.parametrize("source", [
        "agent", "agent x", "agent x {", "state", "state S { input", "state S { input q:",
        "flow f { start ->", "flow f { start -[", "flow f { start -[state.x", "tool t(", "tool t(a: string) ->",
        "}", ")", "-[", "agent x { if ( }", 'agent x { state.a = llm( }', "agent x { state.a = }",
    ])
    def test_truncated_programs_never_crash(self, source):
        result = compile_source(source)
        assert not result["ok"]
        assert {d["code"] for d in result["diagnostics"]} <= {"AF-E101", "AF-E102"}

    def test_empty_edge_label(self):
        assert codes_of(prog(flow="start -[]-> x -> end")) == ["AF-E101"]

    def test_chained_comparison_is_rejected(self):
        assert codes_of(prog(decls='agent x { state.a = "1"\n if (state.q == "a" == "b") { } }')) == ["AF-E101"]

    def test_else_with_max_iterations_is_rejected(self):
        assert codes_of(prog(flow="start -> x\nx -[else, max_iterations 3]-> end")) == ["AF-E101"]

    def test_state_dot_at_top_level_recovers(self):
        result = compile_source('state.a = "x"\n' + prog())
        assert [d["code"] for d in result["diagnostics"]] == ["AF-E101"]

    def test_many_errors_in_one_file_each_reported(self):
        broken = "\n".join(f"agent a{i} {{ state.x = }}" for i in range(20))
        assert codes_of(broken) == ["AF-E101"] * 20

    DEEP = {
        "parentheses": lambda: 'if (' + "(" * 400 + "true" + ")" * 400 + ") { }",
        "not chain": lambda: 'if (' + "not " * 3000 + "true) { }",
        "nested ifs": lambda: "if (true) { " * 500 + " }" * 500,
        "parentheses x 100000": lambda: 'if (' + "(" * 100000 + "true" + ")" * 100000 + ") { }",
    }

    @pytest.mark.parametrize("name", DEEP)
    def test_absurd_nesting_gives_an_error_not_a_crash(self, name):
        source = prog(decls='agent x { state.a = "1"\n' + self.DEEP[name]() + " }")
        assert codes_of(source) == ["AF-E103"]

    def test_nesting_limit_boundary(self):
        # the if condition and the blocks count too, so ~95 parentheses fit and 120 do not
        compile_ok(prog(decls='agent x { state.a = "1"\n if (' + "(" * 95 + "true" + ")" * 95 + ") { } }"))
        assert codes_of(prog(decls='agent x { state.a = "1"\n if (' + "(" * 120 + "true" + ")" * 120 + ") { } }")) \
            == ["AF-E103"]

    def test_nesting_error_recovers_at_next_declaration(self):
        deep = 'agent x { state.a = "1"\n if (' + "(" * 200 + "true" + ")" * 200 + ") { } }"
        assert codes_of(prog(decls=deep + "\nagent y { state.a = 5 }")) == ["AF-E103"]   # y still parsed, no crash

    def test_long_flat_chains_are_not_nesting(self):
        # 3000 `or`s and a 1000 agent pipeline are long, but not deep: they must compile
        compile_ok(prog(decls='agent x { state.a = "1"\n if (' + " or ".join(["true"] * 3000) + ") { } }"))
        agents = "\n".join(f'agent a{i} {{ state.a = "{i}" }}' for i in range(1000))
        chain = " -> ".join(f"a{i}" for i in range(1000))
        compile_ok(f"state S {{ input q: string\n output a: string }}\n{agents}\nflow f {{ start -> {chain} -> end }}")


# ---------------------------------------------------------------- semantic

class TestSemanticEdges:
    def test_reserved_underscore_names(self):
        # these would collide with the compiler's hidden fields or with LangGraph's __start__ node
        assert codes_of(prog(fields="_count_0: int")) == ["AF-E213"]
        assert codes_of(prog(fields="_next_x: string")) == ["AF-E213"]
        assert codes_of(prog(decls="agent __start__ { }", flow="start -> __start__ -> end")) == ["AF-E213"]

    def test_agent_and_tool_may_share_a_name(self):
        compile_ok(prog(decls='tool x() -> string\nagent x { state.a = x() }'))

    def test_tool_with_no_parameters(self):
        compile_ok(prog(decls='tool now() -> string\nagent x { state.a = now() }'))

    def test_nested_list_types(self):
        compile_ok(prog(fields="m: list[list[int]] = []",
                        decls='tool t() -> list[list[int]]\nagent x { state.m = t()\n state.a = "k" }'))

    def test_list_default_type_errors(self):
        assert codes_of(prog(fields='l: list[int] = ["a"]')) == ["AF-E203"]
        assert codes_of(prog(fields='n: int = []')) == ["AF-E203"]
        assert codes_of(prog(fields='n: string = 3')) == ["AF-E203"]

    def test_one_of_mixed_types(self):
        assert codes_of(prog(decls='agent x { state.a = llm("p") one_of ["a", 1] }')) == ["AF-E203"]

    def test_one_of_with_bools_and_ints(self):
        compile_ok(prog(fields="b: bool\n n: int",
                        decls='agent x { state.b = llm("p") one_of [true, false]\n'
                              ' state.n = llm("p") one_of [1, 2]\n state.a = "k" }',
                        flow="start -> x\nx -[state.b and state.n > 1]-> end\nx -[else]-> end"))

    def test_one_of_warning_only_when_every_write_is_one_of(self):
        body = 'agent x { state.r = llm("p") one_of ["a", "b"]\n state.a = "k" }'
        flow = 'start -> x\nx -[state.r == "zzz"]-> end\nx -[else]-> end'
        assert codes_of(prog(fields="r: string", decls=body, flow=flow)) == ["AF-W208"]
        # a second, free-form write to r means "zzz" is possible, so no AF-W208
        # (y's write is always overwritten by x before anyone reads it, so the optimizer flags it: AF-W307)
        body2 = body + '\nagent y { state.r = state.q }'
        flow2 = 'start -> y -> x\nx -[state.r == "zzz"]-> end\nx -[else]-> end'
        assert codes_of(prog(fields="r: string", decls=body2, flow=flow2)) == ["AF-W307"]

    def test_unknown_field_inside_edge_condition(self):
        assert codes_of(prog(flow='start -> x\nx -[state.nope == "a"]-> end\nx -[else]-> end')) == ["AF-E202"]

    def test_errors_do_not_cascade(self):
        # one undeclared tool used inside a comparison: one error, not three
        assert codes_of(prog(decls='agent x { state.a = "k"\n if (missing(state.q) == "a") { } }')) == ["AF-E201"]

    def test_same_unknown_agent_reported_once(self):
        assert codes_of(prog(flow="start -> ghost\nghost -> x -> end")) == ["AF-E204"]

    def test_assign_to_input_field_is_allowed(self):
        compile_ok(prog(decls='agent x { state.q = "changed"\n state.a = state.q }'))


# ---------------------------------------------------------------- verification

class TestVerifyEdges:
    def test_only_start_to_end(self):
        # agents unused (dead) and the output is never written
        assert codes_of(prog(flow="start -> end")) == ["AF-E304", "AF-W301"]

    def test_no_edges_at_all(self):
        assert sorted(codes_of(prog(flow=""))) == ["AF-E305", "AF-W301"]

    def test_unconditional_self_loop(self):
        assert codes_of(prog(flow="start -> x -> x")) == ["AF-E303"]

    def test_bounded_self_loop_without_an_exit_still_cannot_reach_end(self):
        # the loop is bounded (no AF-E303), but neither start nor x has any way to reach end
        assert sorted(codes_of(prog(flow='start -> x\nx -[state.q == "a", max_iterations 3]-> x'))) == \
            ["AF-E305", "AF-E305", "AF-W306"]

    def test_unreachable_cycle_is_only_dead_code(self):
        result = compile_source(prog(decls='agent x { state.a = "1" }\nagent y { }\nagent z { }',
                                     flow="start -> x -> end\ny -> z -> y"))
        assert result["ok"]
        assert [d["code"] for d in result["diagnostics"]] == ["AF-W301", "AF-W301"]
        assert result["phases"]["optimize"]["removed_agents"] == ["y", "z"]

    def test_write_only_inside_a_loop_is_not_definite_after_it(self):
        decls = 'agent w { state.x = "v" }\nagent r { state.a = state.x }\nagent hub { }'
        flow = 'start -> hub\nhub -[state.q == "go", max_iterations 2]-> w\nhub -[else]-> r\nw -> hub\nr -> end'
        result = compile_source(prog(fields="x: string", decls=decls, flow=flow))
        assert [d["code"] for d in result["diagnostics"]] == ["AF-E302"]
        assert result["diagnostics"][0]["path"] == ["start", "hub", "r"]

    def test_write_in_every_branch_of_else_if_chain_is_definite(self):
        body = ('agent x { if (state.q == "a") { state.a = "1" } else if (state.q == "b") { state.a = "2" } '
                'else { state.a = "3" } }')
        compile_ok(prog(decls=body))

    def test_write_in_if_without_else_is_not_definite(self):
        assert codes_of(prog(decls='agent x { if (state.q == "a") { state.a = "1" } }')) == ["AF-E304"]

    def test_two_separate_loops(self):
        decls = 'agent a1 { state.a = "1" }\nagent a2 { state.a = "2" }'
        flow = ('start -> a1\na1 -[state.q == "x", max_iterations 2]-> a1\na1 -[else]-> a2\n'
                'a2 -[state.q == "y", max_iterations 3]-> a2\na2 -[else]-> end')
        compile_ok(prog(decls=decls, flow=flow))

    def test_conditions_without_else(self):
        flow = 'start -> x\nx -[state.q == "a"]-> end\nx -[state.q != "a"]-> end'
        assert codes_of(prog(flow=flow)) == ["AF-W306"]


# ---------------------------------------------------------------- code generation (Python validity)

class TestCodegenEdges:
    def test_python_keywords_as_field_names(self):
        # `class: string` cannot be written in a TypedDict class body, so the functional form is used
        code = compile_ok(prog(fields="class: string\n None: int\n from: bool",
                               decls='agent x { state.class = "c"\n state.a = state.class }'))
        assert '"class": str' in code

    def test_python_keywords_as_tool_parameters(self):
        code = compile_ok(prog(decls='tool t(from: string, lambda: int) -> string\nagent x { state.a = t(state.q, 1) }'))
        assert "def tool_t(from_: str, lambda_: int)" in code

    def test_python_keywords_as_agent_names(self):
        compile_ok(prog(decls='agent class { state.a = "1" }', flow="start -> class -> end"))

    def test_quotes_backslashes_and_braces_in_prompts(self):
        code = compile_ok(prog(decls=r'agent x { state.a = llm("say \"hi\" \\ {x} {{y}} ''' + "'''" + r'", state.q) }'))
        assert 'llm("say \\"hi\\"' in code

    def test_empty_state_and_empty_agent(self):
        compile_ok("state S { }\nagent x { }\nflow f { start -> x -> end }")

    def test_branching_from_start(self):
        code = compile_ok(prog(flow='start -[state.q == "a"]-> x\nstart -[else]-> x\nx -> end'))
        assert "graph.add_conditional_edges(START" in code


# ---------------------------------------------------------------- generated code actually runs

@needs_langgraph
class TestGeneratedCodeRuns:
    def test_keyword_names_run(self, tmp_path):
        code = compile_ok(prog(fields="class: string",
                               decls='tool t(from: string) -> string\nagent class { state.class = t(state.q)\n state.a = state.class }',
                               flow="start -> class -> end"))
        assert "start -> class -> end" in run_generated(code, tmp_path)

    def test_branch_from_start_and_else_only_edge(self, tmp_path):
        code = compile_ok(prog(flow='start -[state.q == "nope"]-> x\nstart -[else]-> x\nx -[else]-> end'))
        assert "start -> x -> end" in run_generated(code, tmp_path)

    def test_bounded_loop_from_the_start_node(self, tmp_path):
        code = compile_ok(prog(flow='start -[state.q != "", max_iterations 2]-> x\nstart -[else]-> x\nx -> end'))
        assert "start -> x -> end" in run_generated(code, tmp_path)

    def test_empty_state(self, tmp_path):
        code = compile_ok("state S { }\nagent x { }\nflow f { start -> x -> end }")
        assert "start -> x -> end" in run_generated(code, tmp_path)

    def test_mock_choice_selects_one_of_int(self, tmp_path):
        code = compile_ok(prog(fields="n: int",
                               decls='agent x { state.n = llm("p") one_of [1, 2, 3]\n state.a = "k" }\nagent y { state.a = "y" }',
                               flow="start -> x\nx -[state.n == 3]-> y\nx -[else]-> end\ny -> end"))
        assert "start -> x -> end" in run_generated(code, tmp_path)
        assert "start -> x -> y -> end" in run_generated(code, tmp_path, {"AGENTFORGE_CHOICE": "3"})

    def test_two_loops_each_respect_their_bound(self, tmp_path):
        decls = 'agent a1 { state.a = "1" }\nagent a2 { state.a = "2" }'
        flow = ('start -> a1\na1 -[state.q != "", max_iterations 2]-> a1\na1 -[else]-> a2\n'
                'a2 -[state.q != "", max_iterations 3]-> a2\na2 -[else]-> end')
        out = run_generated(compile_ok(prog(decls=decls, flow=flow)), tmp_path)
        path_line = next(line for line in out.splitlines() if line.startswith("path:"))
        assert path_line.count("a1") == 3 and path_line.count("a2") == 4   # first visit + bound

    def test_no_matching_edge_raises_clear_error(self, tmp_path):
        code = compile_ok(prog(flow='start -> x\nx -[state.q == "never"]-> end'))
        path = tmp_path / "workflow.py"
        path.write_text(code, encoding="utf-8")
        out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120)
        assert out.returncode != 0
        assert "no edge condition matched after 'x'" in out.stderr

    def test_long_chain_does_not_hit_recursion_limit(self, tmp_path):
        agents = "\n".join(f'agent a{i} {{ state.a = "{i}" }}' for i in range(60))
        chain = " -> ".join(f"a{i}" for i in range(60))
        code = compile_ok(f"state S {{ input q: string\n output a: string }}\n{agents}\nflow f {{ start -> {chain} -> end }}")
        assert "a: 59" in run_generated(code, tmp_path)

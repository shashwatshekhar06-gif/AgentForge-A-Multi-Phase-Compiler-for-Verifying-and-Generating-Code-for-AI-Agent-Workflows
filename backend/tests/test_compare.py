"""The `compare` feature: several agents for the same job, scored on test cases.

Covers the syntax, the semantic errors (AF-E214, AF-E215), the swap check (AF-E216, AF-W310, AF-W311),
the scoring (evaluate.py), the generated evaluator, the real-model switch, and the API field.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR, parse_source

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None

BASE = """
state S {
  input question: string
  context: list[string] = []
  output answer: string
}

agent a { state.answer = llm("Short answer", state.question) }
agent b { state.answer = llm("Answer with context", state.question, state.context) }
agent c { state.answer = llm("Answer politely", state.question) }

flow main { start -> a -> end }
"""

CASE_PARIS = """
  case {
    state.question = "Capital of France?"
    expect state.answer contains "Paris"
    mock a "Lyon"
    mock b "Paris, of course"
  }
"""

# the same case when three candidates compete
CASE_PARIS3 = CASE_PARIS.replace('mock b "Paris, of course"', 'mock b "Paris, of course"\n    mock c "paris"')


def compile_with(compare_block, base=BASE):
    return compile_source(base + compare_block)


def diag_codes(result):
    return [d["code"] for d in result["diagnostics"]]


# ---------- syntax ----------

def test_compare_block_parses_into_an_ast():
    program = parse_source(BASE + "compare pick { candidates a, b, c" + CASE_PARIS3 + "}")
    decl = program.declarations[-1]
    assert decl.name == "pick"
    assert [c.name for c in decl.candidates] == ["a", "b", "c"]
    case = decl.cases[0]
    assert case.inputs[0].target.field == "question"
    assert (case.expects[0].op, case.expects[0].value.value) == ("contains", "Paris")
    assert {m.agent: m.text for m in case.mocks} == {"a": "Lyon", "b": "Paris, of course", "c": "paris"}


def test_double_equals_expectation():
    result = compile_with('compare pick { candidates a, b case { state.question = "x" expect state.answer == "y" } }')
    assert result["ok"], result["diagnostics"]


def test_compare_without_candidates_is_a_syntax_error():
    result = compile_with("compare pick { case { expect state.answer == \"y\" } }")
    assert "AF-E101" in diag_codes(result)


def test_expect_needs_contains_or_double_equals():
    result = compile_with('compare pick { candidates a, b case { expect state.answer "y" } }')
    assert "AF-E101" in diag_codes(result)


def test_compare_is_an_ast_round_trip():
    from compiler.ast_nodes import from_dict
    program = parse_source(BASE + "compare pick { candidates a, b" + CASE_PARIS + "}")
    assert from_dict(program.to_dict()).to_dict() == program.to_dict()


# ---------- semantic errors ----------

@pytest.mark.parametrize("block, expected", [
    ("compare p { candidates a, ghost" + CASE_PARIS + "}", "AF-E204"),                     # unknown agent
    ("compare p { candidates a, a" + CASE_PARIS + "}", "AF-E214"),                         # listed twice
    ("compare p { candidates a" + CASE_PARIS + "}", "AF-E214"),                            # one candidate
    ("compare p { candidates a, b }", "AF-E214"),                                          # no cases
    ('compare p { candidates a, b case { state.question = "x" } }', "AF-E214"),            # no expect
    ('compare p { candidates a, b case { expect state.answer == "x" expect state.answer == "y" } }', "AF-E214"),
    ('compare p { candidates a, b case { expect state.answer == "x" mock c "z" } }', "AF-E214"),   # not a candidate
    ('compare p { candidates a, b case { expect state.answer == "x" mock a "z" mock a "w" } }', "AF-E214"),
    ('compare p { candidates a, b case { state.nope = "x" expect state.answer == "y" } }', "AF-E202"),
    ('compare p { candidates a, b case { state.question = 5 expect state.answer == "y" } }', "AF-E203"),
    ('compare p { candidates a, b case { expect state.answer == 5 } }', "AF-E203"),
    ('compare p { candidates a, b case { expect state.context contains "x" } }', "AF-E203"),
    ('compare p { candidates a, b case { expect state.nope == "x" } }', "AF-E202"),
    ("compare _p { candidates a, b" + CASE_PARIS + "}", "AF-E213"),
])
def test_compare_semantic_errors(block, expected):
    result = compile_with(block)
    assert expected in diag_codes(result), result["diagnostics"]
    assert not result["ok"]


def test_candidate_that_never_writes_the_expected_field():
    base = BASE.replace('agent c { state.answer = llm("Answer politely", state.question) }',
                        'agent c { state.note = "x" }').replace("output answer: string",
                                                                "output answer: string\n  note: string")
    case = 'case { state.question = "x" expect state.answer contains "Paris" }'
    result = compile_source(base + "compare p { candidates a, c " + case + "}")
    assert diag_codes(result) == ["AF-E215"]


def test_duplicate_compare_name():
    block = "compare p { candidates a, b" + CASE_PARIS + "}"
    assert "AF-E205" in diag_codes(compile_with(block + block))


# ---------- candidates wait outside the flow without warnings ----------

def test_unwired_candidates_are_not_dead_agents():
    result = compile_with("compare p { candidates a, b, c" + CASE_PARIS3 + "}")
    assert result["ok"] and result["diagnostics"] == []
    assert result["phases"]["optimize"]["removed_agents"] == []
    # the agent that is not a candidate is still reported
    other = BASE + "agent lonely { state.answer = llm(\"x\") }\n"
    assert "AF-W301" in diag_codes(compile_source(other + "compare p { candidates a, b" + CASE_PARIS + "}"))


def test_generated_flow_does_not_contain_unwired_candidates():
    code = compile_source(BASE + "compare p { candidates a, b" + CASE_PARIS + "}", select=False)["phases"]["codegen"]["code"]
    assert 'graph.add_node("a"' in code and 'graph.add_node("b"' not in code
    assert "def eval_b(state: AgentState)" in code    # but the evaluator has it


# ---------- the swap check ----------

def test_swap_that_reads_an_unwritten_field_is_an_error():
    base = BASE.replace("context: list[string] = []", "context: list[string]")
    result = compile_source(base + "compare p { candidates a, b, c" + CASE_PARIS3 + "}")
    assert diag_codes(result) == ["AF-E216"]
    message = result["diagnostics"][0]["message"]
    assert "if 'b' replaces 'a'" in message and "state.context" in message
    assert result["diagnostics"][0]["path"] == ["start", "b"]


def test_problem_that_already_exists_in_the_wired_agent_is_not_repeated():
    # a also reads the unwritten field, so the baseline already has AF-E302; b adds nothing new
    base = BASE.replace("context: list[string] = []", "context: list[string]").replace(
        'agent a { state.answer = llm("Short answer", state.question) }',
        'agent a { state.answer = llm("Short answer", state.question, state.context) }')
    result = compile_source(base + "compare p { candidates a, b, c" + CASE_PARIS3 + "}")
    assert diag_codes(result) == ["AF-E302"]


def test_swap_that_opens_a_prompt_injection_path_is_a_warning():
    source = """
state S {
  input untrusted msg: string
  output reply: string
}
tool refund(note: string) -> string sensitive
agent a { state.reply = llm("Polite reply", state.msg) }
agent b { state.reply = refund(state.msg) }
flow main { start -> a -> end }
compare p { candidates a, b case { expect state.reply contains "x" } }
"""
    result = compile_source(source)
    assert diag_codes(result) == ["AF-W310"]
    assert result["ok"]


def test_no_wired_candidate_warns():
    case = 'case { expect state.answer contains "x" }'
    result = compile_source(BASE + "compare p { candidates b, c " + case + "}")
    assert diag_codes(result) == ["AF-W311"]


def test_two_wired_candidates_skip_the_swap_check():
    base = BASE.replace("start -> a -> end", "start -> a -> b -> end").replace(
        "context: list[string] = []", "context: list[string]")
    result = compile_source(base + "compare p { candidates a, b" + CASE_PARIS + "}")
    assert "AF-E216" not in diag_codes(result)


def test_every_other_candidate_is_checked():
    base = BASE.replace(
        'agent c { state.answer = llm("Answer politely", state.question) }',
        'agent c { state.answer = llm("Answer", state.question, state.hidden) }').replace(
        "output answer: string", "output answer: string\n  hidden: string")
    result = compile_source(base + "compare p { candidates a, b, c" + CASE_PARIS3 + "}")
    assert diag_codes(result) == ["AF-E216"]
    assert "if 'c' replaces 'a'" in result["diagnostics"][0]["message"]


# ---------- scoring ----------

def evaluation(block, base=BASE):
    result = compile_source(base + block)
    assert result["ok"], result["diagnostics"]
    return result["evaluation"]


def test_scores_and_best_candidate():
    report = evaluation("compare p { candidates a, b, c" + CASE_PARIS3 + "}")[0]
    assert {s["agent"]: s["passed"] for s in report["scores"]} == {"a": 0, "b": 1, "c": 1}
    assert report["best"] == "b" and report["tie"] is True       # b and c tie, b comes first
    assert report["wired"] == ["a"]
    assert [r["passed"] for r in report["cases"][0]["results"]] == [False, True, True]
    assert report["cases"][0]["results"][1]["output"] == "Paris, of course"


def test_contains_ignores_case_but_double_equals_does_not():
    block = """compare p { candidates a, b
      case { expect state.answer contains "PARIS" mock a "x" mock b "paris" }
      case { expect state.answer == "Paris" mock a "Paris" mock b "paris" }
    }"""
    scores = {s["agent"]: s["passed"] for s in evaluation(block)[0]["scores"]}
    assert scores == {"a": 1, "b": 1}


def test_tie_keeps_the_agent_already_in_the_flow():
    block = 'compare p { candidates b, a case { expect state.answer contains "x" mock a "x" mock b "x" } }'
    report = evaluation(block)[0]
    assert report["tie"] and report["best"] == "a"      # a is wired, although b is listed first


def test_no_mock_means_the_placeholder_answer():
    block = 'compare p { candidates a, b case { expect state.answer contains "mock LLM answer" } }'
    report = evaluation(block)[0]
    assert [s["passed"] for s in report["scores"]] == [1, 1]


def test_case_inputs_reach_the_agent():
    source = """
state S {
  input question: string
  output answer: string
}
agent a { state.answer = state.question }
agent b { state.answer = "fixed" }
flow main { start -> a -> end }
compare p { candidates a, b case { state.question = "hello" expect state.answer == "hello" } }
"""
    assert [s["passed"] for s in evaluation("", source)[0]["scores"]] == [1, 0]


def test_agents_with_tools_and_branches_run_in_the_evaluator():
    source = """
state S {
  input q: string
  hits: list[string]
  output answer: string
}
tool search(q: string) -> list[string]
agent a {
  state.hits = search(state.q)
  if (state.q == "go") { state.answer = "went" } else { state.answer = "stayed" }
}
agent b { state.answer = "b" }
flow main { start -> a -> end }
compare p { candidates a, b case { state.q = "go" expect state.answer == "went" } }
"""
    assert [s["passed"] for s in evaluation("", source)[0]["scores"]] == [1, 0]


def test_programs_without_compare_have_an_empty_evaluation():
    result = compile_source((EXAMPLES_DIR / "valid" / "research.af").read_text())
    assert result["evaluation"] == []
    assert "def eval_" not in result["phases"]["codegen"]["code"]


def test_failed_compile_has_no_evaluation():
    result = compile_source(BASE + "compare p { candidates a, ghost" + CASE_PARIS + "}")
    assert result["evaluation"] == []


def test_example_file_picks_the_better_responder():
    result = compile_source((EXAMPLES_DIR / "valid" / "compare_agents.af").read_text())
    report = result["evaluation"][0]
    assert report["best"] == "responder_b" and report["wired"] == ["responder_a"]
    assert {s["agent"]: s["passed"] for s in report["scores"]} == {"responder_a": 1, "responder_b": 3}


# ---------- the generated file ----------

def run_generated(tmp_path, source, env_extra=None, select=False):
    result = compile_source(source, "compare.af", select=select)
    assert result["ok"], result["diagnostics"]
    path = tmp_path / "workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFORGE_")}
    env.update(env_extra or {})
    return result, subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_generated_scores_match_the_compiler_evaluation(tmp_path):
    source = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()
    result, out = run_generated(tmp_path, source)
    assert out.returncode == 0, out.stderr
    report = result["evaluation"][0]
    for s in report["scores"]:
        assert f"{s['agent']}: {s['passed']}/{s['total']}" in out.stdout
    assert f"{report['best']}: " in out.stdout and "<- best" in out.stdout
    for case in report["cases"]:
        for r in case["results"]:
            assert f"{'PASS' if r['passed'] else 'FAIL'}  {r['agent']}: {r['output']}" in out.stdout
    assert "path: start" in out.stdout
    assert "AGENTFORGE_USE_BEST=1 to use 'responder_b'" in out.stdout


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_use_best_runs_the_winner_in_the_flow(tmp_path):
    source = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()
    _, out = run_generated(tmp_path, source, {"AGENTFORGE_USE_BEST": "1", "AGENTFORGE_CHOICE": "factual"})
    assert out.returncode == 0, out.stderr
    assert "using 'responder_b' in place of 'responder_a'" in out.stdout
    assert "(node 'responder_a' ran the code of 'responder_b')" in out.stdout
    # the winner's prompt was used, and the rest of the path is unchanged
    assert "Answer the query using the context. Name the city." in out.stdout
    assert "path: start -> classifier -> researcher -> responder_a -> end" in out.stdout


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_use_best_keeps_branching_working_when_the_winner_replaces_a_router(tmp_path):
    source = """
state S {
  input q: string
  route: string
  output answer: string
}
agent a {
  state.route = "no"
  state.answer = llm("A")
}
agent b {
  state.route = "yes"
  state.answer = llm("B")
}
agent yes_branch { state.answer = "took the yes branch" }
flow main {
  start -> a
  a -[state.route == "yes"]-> yes_branch
  a -[else]-> end
  yes_branch -> end
}
compare p { candidates a, b case { expect state.route == "yes" } }
"""
    _, out = run_generated(tmp_path, source, {"AGENTFORGE_USE_BEST": "1"})
    assert out.returncode == 0, out.stderr
    assert "path: start -> a -> yes_branch -> end" in out.stdout   # b's routing decision was honoured
    assert "answer: took the yes branch" in out.stdout


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_real_mode_without_a_key_explains_what_is_missing(tmp_path):
    source = (EXAMPLES_DIR / "valid" / "research.af").read_text()
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AGENTFORGE_PROVIDER")}
    env["AGENTFORGE_LLM"] = "real"
    result = compile_source(source, "research.af")
    path = tmp_path / "workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode != 0
    assert "needs the ANTHROPIC_API_KEY environment variable" in out.stderr


def load_generated(tmp_path, source, select=False):
    result = compile_source(source, "x.af", select=select)
    path = tmp_path / "gen_workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    spec = importlib.util.spec_from_file_location("gen_workflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, text):
        self.body = json.dumps({"content": [{"type": "text", "text": text}]}).encode()

    def read(self, *args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_real_mode_sends_the_prompt_and_options_to_the_messages_api(tmp_path, monkeypatch):
    module = load_generated(tmp_path, (EXAMPLES_DIR / "valid" / "research.af").read_text())
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        return FakeResponse("  Factual. ")

    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFORGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    answer = module.llm("Classify the query", "what is 2+2", one_of=["factual", "conversational"])
    assert answer == "factual"                                    # the reply is matched to an option
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    prompt = seen["body"]["messages"][0]["content"]
    assert "Classify the query" in prompt and "what is 2+2" in prompt and "factual, conversational" in prompt

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda r, timeout=None: FakeResponse("a free answer"))
    assert module.llm("Say something") == "a free answer"

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda r, timeout=None: FakeResponse("banana"))
    with pytest.raises(RuntimeError, match="not one of"):
        module.llm("Classify", one_of=["factual", "conversational"])


class FakeOpenAIResponse(FakeResponse):
    def __init__(self, text):
        self.body = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_real_mode_uses_openai_when_an_openai_key_is_set(tmp_path, monkeypatch):
    module = load_generated(tmp_path, (EXAMPLES_DIR / "valid" / "research.af").read_text())
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        return FakeOpenAIResponse("Conversational")

    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFORGE_PROVIDER", raising=False)
    monkeypatch.delenv("AGENTFORGE_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    answer = module.llm("Classify the query", "hi there", one_of=["factual", "conversational"])
    assert answer == "conversational"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-test"
    assert seen["body"]["model"] == "gpt-4o-mini"
    prompt = seen["body"]["messages"][0]["content"]
    assert "Classify the query" in prompt and "hi there" in prompt and "factual, conversational" in prompt

    monkeypatch.setenv("AGENTFORGE_MODEL", "gpt-4.1")
    module.llm("Say something")
    assert seen["body"]["model"] == "gpt-4.1"


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_provider_can_be_chosen_and_a_missing_key_is_named(tmp_path, monkeypatch):
    module = load_generated(tmp_path, (EXAMPLES_DIR / "valid" / "research.af").read_text())
    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AGENTFORGE_PROVIDER", "anthropic")        # forced, although an OpenAI key exists
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        module.llm("Say something")
    monkeypatch.setenv("AGENTFORGE_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        module.llm("Say something")


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_real_mode_ignores_the_scripted_answers(tmp_path, monkeypatch):
    module = load_generated(tmp_path, (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text(), select=False)
    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFORGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda r, timeout=None: FakeResponse("Paris is the capital"))
    report = module.run_compare(module.COMPARES[0])
    # every answer came from the "model", not from the script: all three cases mention Paris, so only case 1 passes
    assert [r["output"] for r in report["cases"][0]["results"]] == ["Paris is the capital"] * 2
    assert report["scores"]["responder_a"] == report["scores"]["responder_b"] == 1
    assert report["tie"] and report["best"] == "responder_a"


def test_mock_mode_is_the_default_and_makes_no_network_call(tmp_path, monkeypatch):
    if not HAS_LANGGRAPH:
        pytest.skip("langgraph is not installed")
    module = load_generated(tmp_path, (EXAMPLES_DIR / "valid" / "research.af").read_text())

    def forbidden(*args, **kwargs):
        raise AssertionError("mock mode must not use the network")

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    assert module.LLM_MODE == "mock"
    assert module.llm("Classify", one_of=["factual", "conversational"]) == "factual"


# ---------- API and docs ----------

def test_api_returns_the_evaluation():
    from fastapi.testclient import TestClient
    import api

    source = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()
    body = TestClient(api.app).post("/api/compile", json={"source": source}).json()
    assert body["evaluation"][0]["best"] == "responder_b"
    assert body["phases"]["semantic"]["symbols"]["compares"] == [
        {"name": "best_responder", "candidates": ["responder_a", "responder_b"], "cases": 3,
         "end_to_end": False, "ensure": None}]


def test_every_new_diagnostic_code_is_documented():
    contract = (EXAMPLES_DIR.parent / "docs" / "contract.md").read_text()
    for code in ("AF-E214", "AF-E215", "AF-E216", "AF-W310", "AF-W311"):
        assert code in contract
    assert re.search(r"compare", (EXAMPLES_DIR.parent / "docs" / "grammar.md").read_text())


# ---------- agent selection: the optimizer wires the winner into the generated code ----------

EXAMPLE = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()


def test_the_optimizer_wires_the_winner_into_the_generated_code():
    result = compile_source(EXAMPLE)
    assert result["ok"] and result["diagnostics"] == []
    code = result["phases"]["codegen"]["code"]
    assert 'graph.add_node("responder_b"' in code and 'graph.add_node("responder_a"' not in code
    selected = result["phases"]["optimize"]["selected"]
    assert selected == [{"compare": "best_responder", "replaced": "responder_a", "with": "responder_b",
                         "scores": {"responder_a": "1/3", "responder_b": "3/3"}, "profile": "mock"}]
    report = result["evaluation"][0]
    assert report["applied"] == "responder_b" and report["wired"] == ["responder_a"] and report["blocked"] is None


def test_selection_can_be_switched_off():
    result = compile_source(EXAMPLE, select=False)
    code = result["phases"]["codegen"]["code"]
    assert 'graph.add_node("responder_a"' in code and 'graph.add_node("responder_b"' not in code
    assert result["phases"]["optimize"]["selected"] == []
    assert result["evaluation"][0]["applied"] is None


def test_a_tie_changes_nothing():
    block = 'compare p { candidates a, b case { expect state.answer contains "x" mock a "x" mock b "x" } }'
    result = compile_with(block)
    assert result["phases"]["optimize"]["selected"] == []
    assert 'graph.add_node("a"' in result["phases"]["codegen"]["code"]


def test_the_winner_is_not_applied_when_it_would_add_a_verification_warning():
    source = (EXAMPLES_DIR / "invalid" / "unsafe_candidate.af").read_text()
    result = compile_source(source)
    assert result["ok"] and [d["code"] for d in result["diagnostics"]] == ["AF-W310"]
    report = result["evaluation"][0]
    assert report["best"] == "responder_b"                      # it scores higher ...
    assert report["applied"] is None and "AF-W310" in report["blocked"]   # ... but it is not used
    code = result["phases"]["codegen"]["code"]
    assert 'graph.add_node("responder_a"' in code and 'graph.add_node("responder_b"' not in code


def test_cost_analysis_sees_the_selected_agent():
    # the winner has two LLM calls, the wired agent one: the cost bound must count the winner
    source = """
state S {
  input q: string
  draft: string
  output answer: string
}
agent a { state.answer = llm("Short", state.q) }
agent b {
  state.draft = llm("Think", state.q)
  state.answer = llm("Answer", state.q, state.draft)
}
flow main { start -> a -> end }
compare p { candidates a, b case { expect state.answer contains "yes" mock a "no" mock b "yes" } }
"""
    selected = compile_source(source)
    kept = compile_source(source, select=False)
    assert selected["phases"]["cost"]["worst"]["llm"] == 2
    assert kept["phases"]["cost"]["worst"]["llm"] == 1


def test_loser_stays_available_for_the_runtime_evaluator():
    code = compile_source(EXAMPLE)["phases"]["codegen"]["code"]
    assert "def eval_responder_a(" in code and "def eval_responder_b(" in code
    assert "'wired': ['responder_b']" in code


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_generated_flow_runs_the_selected_agent_without_any_switch(tmp_path):
    result, out = run_generated(tmp_path, EXAMPLE, {"AGENTFORGE_CHOICE": "factual"}, select=True)
    assert out.returncode == 0, out.stderr
    assert "path: start -> classifier -> researcher -> responder_b -> end" in out.stdout
    assert "Answer the query using the context. Name the city." in out.stdout
    assert "responder_b: 3/3 (in the flow)" in out.stdout


# ---------- measured profile (profile-guided: scores from a real model, passed back in) ----------

def measured_profile(passed_a, passed_b):
    """The shape `python workflow.py --scores-json` prints for compare_agents.af."""
    cases = []
    for i, (city, ok_a, ok_b) in enumerate(zip(("Paris", "Tokyo", "Madrid"), passed_a, passed_b), start=1):
        cases.append({"expect": {"field": "response", "op": "contains", "value": city},
                      "inputs": {"query": f"capital {i}"},
                      "results": [{"agent": "responder_a", "output": "model said A", "passed": ok_a},
                                  {"agent": "responder_b", "output": "model said B", "passed": ok_b}]})
    return [{"name": "best_responder", "candidates": ["responder_a", "responder_b"], "cases": cases}]


def test_measured_scores_override_the_scripted_ones():
    # the scripts say responder_b wins; the "real model" says responder_a does
    result = compile_source(EXAMPLE, profile=measured_profile([True, True, True], [True, False, False]))
    report = result["evaluation"][0]
    assert report["mode"] == "measured" and report["best"] == "responder_a"
    assert report["applied"] is None                    # responder_a is already in the flow
    assert result["phases"]["optimize"]["selected"] == []
    assert 'graph.add_node("responder_a"' in result["phases"]["codegen"]["code"]
    assert report["cases"][0]["results"][0]["output"] == "model said A"


def test_measured_scores_can_select_the_other_agent():
    result = compile_source(EXAMPLE, profile=measured_profile([False, False, True], [True, True, True]))
    assert result["phases"]["optimize"]["selected"][0]["profile"] == "measured"
    assert result["phases"]["optimize"]["selected"][0]["with"] == "responder_b"


@pytest.mark.parametrize("stale", [
    [{"name": "best_responder", "candidates": ["x", "y"], "cases": []}],            # other candidates
    [{"name": "best_responder", "candidates": ["responder_a", "responder_b"], "cases": []}],   # wrong number of cases
    [{"name": "other_block", "candidates": ["responder_a", "responder_b"], "cases": []}],     # other compare block
])
def test_a_stale_profile_falls_back_to_the_mock_scores(stale):
    report = compile_source(EXAMPLE, profile=stale)["evaluation"][0]
    assert report["mode"] == "mock" and report["best"] == "responder_b"


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_scores_json_output_is_a_valid_profile(tmp_path):
    result = compile_source(EXAMPLE, select=False)
    path = tmp_path / "workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFORGE_")}
    out = subprocess.run([sys.executable, str(path), "--scores-json"], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    line = next(l for l in out.stdout.splitlines() if l.startswith("AGENTFORGE_SCORES="))
    assert "path:" not in out.stdout                      # it measures, it does not run the workflow
    profile = json.loads(line[len("AGENTFORGE_SCORES="):])
    replayed = compile_source(EXAMPLE, profile=profile)["evaluation"][0]
    assert replayed["mode"] == "measured"
    # the generated harness and the compiler's own interpreter agree
    assert [s["passed"] for s in replayed["scores"]] == [s["passed"] for s in result["evaluation"][0]["scores"]]


def test_cli_profile_real_measures_first_then_recompiles(tmp_path, monkeypatch, capsys):
    from compiler import cli

    calls = []

    def fake_run(command, capture_output, text, env, timeout=None):
        calls.append((command, env.get("AGENTFORGE_LLM")))
        line = "AGENTFORGE_SCORES=" + json.dumps(measured_profile([True, True, True], [False, False, True]))
        return subprocess.CompletedProcess(command, 0, stdout="noise\n" + line + "\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    source_file = tmp_path / "compare_agents.af"
    source_file.write_text(EXAMPLE)
    out_file = tmp_path / "workflow.py"
    assert cli.main([str(source_file), "-o", str(out_file), "--profile", "real"]) == 0
    assert calls[0][0][-1] == "--scores-json" and calls[0][1] == "real"
    printed = capsys.readouterr().out
    assert "compare best_responder (real model): responder_a 3/3, responder_b 1/3; best: responder_a" in printed
    assert 'graph.add_node("responder_a"' in out_file.read_text()          # the measurement overruled the scripts


def test_cli_profile_real_reports_a_failed_measurement(tmp_path, monkeypatch, capsys):
    from compiler import cli

    def failing_run(command, capture_output, text, env, timeout=None):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Traceback\nRuntimeError: needs the OPENAI_API_KEY")

    monkeypatch.setattr(cli.subprocess, "run", failing_run)
    source_file = tmp_path / "compare_agents.af"
    source_file.write_text(EXAMPLE)
    assert cli.main([str(source_file), "--profile", "real"]) == 2
    assert "profiling failed: RuntimeError: needs the OPENAI_API_KEY" in capsys.readouterr().err


def test_cli_prints_the_selection(tmp_path, capsys):
    from compiler import cli

    source_file = tmp_path / "compare_agents.af"
    source_file.write_text(EXAMPLE)
    assert cli.main([str(source_file)]) == 0
    printed = capsys.readouterr().out
    assert "optimizer: 'responder_a' replaced by 'responder_b' in the generated code" in printed
    assert cli.main([str(source_file), "--no-select"]) == 0
    assert "not applied: --no-select" in capsys.readouterr().out

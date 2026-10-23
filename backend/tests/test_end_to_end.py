"""`end_to_end` (judge a candidate by what it does to the whole workflow) and `ensure` (a run-time check)."""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from compiler.evaluate import run_flow
from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR, codes, graph_of

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
EXAMPLE = (EXAMPLES_DIR / "valid" / "triage_end_to_end.af").read_text()

# a small program: two candidates label a message, the label decides which agent runs after them
BASE = """
state S {
  input message: string
  priority: string
  note: string
  output action: string
}
agent a { state.priority = llm("loose", state.message) }
agent b { state.priority = llm("strict", state.message) }
agent escalate { state.action = "escalated" }
agent enqueue { state.action = "queued" }
flow main {
  start -> a
  a -[state.priority == "urgent"]-> escalate
  a -[else]-> enqueue
  escalate -> end
  enqueue -> end
}
"""


def program(compare_body):
    return BASE + "\ncompare c {\n  candidates a, b\n" + compare_body + "\n}\n"


CASE = """
  case {
    state.message = "down"
    expect state.action == "escalated"
    mock a "This is urgent."
    mock b "urgent"
  }
"""


def diagnostics(source):
    return compile_source(source)["diagnostics"]


# ---------- the language ----------

def test_the_new_keywords_parse_and_show_up_in_the_symbols():
    result = compile_source(EXAMPLE)
    assert result["ok"], result["diagnostics"]
    assert result["phases"]["semantic"]["symbols"]["compares"] == [{
        "name": "best_triage", "candidates": ["triage_a", "triage_b"], "cases": 4, "end_to_end": True,
        "ensure": 'state.priority == "urgent" or state.priority == "normal"'}]


def test_a_candidate_may_leave_the_expected_field_to_a_later_agent_only_end_to_end():
    # judged alone, `a` must write state.action itself (AF-E215); judged end to end a later agent writes it
    assert "AF-E215" in codes_of(program(CASE))
    assert "AF-E215" not in codes_of(program("  end_to_end\n" + CASE))


def codes_of(source):
    return [d["code"] for d in diagnostics(source)]


def test_ensure_must_be_a_bool_and_may_not_call_anything():
    assert "AF-E207" in codes_of(program('  ensure state.priority\n' + CASE))
    assert "AF-E211" in codes_of(program('  ensure llm("is it ok") == "yes"\n' + CASE))
    assert "AF-E202" in codes_of(program('  ensure state.nope == "x"\n' + CASE))


def test_end_to_end_needs_exactly_one_candidate_in_the_flow():
    both_flow = """
flow main {
  start -> a
  a -> b
  b -[state.priority == "urgent"]-> escalate
  b -[else]-> enqueue
  escalate -> end
  enqueue -> end
}
"""
    none_flow = """
flow main {
  start -> escalate
  escalate -> end
}
"""
    head = BASE[:BASE.index("flow main")]
    compare = "\ncompare c {\n  candidates a, b\n  end_to_end\n" + CASE + "}\n"
    assert "AF-E217" in codes_of(head + both_flow + compare)
    assert "AF-E217" in codes_of(head + none_flow + compare)
    assert "AF-E217" not in codes_of(program("  end_to_end\n" + CASE))   # one candidate in the flow: fine


# ---------- scoring the whole workflow ----------

def test_mock_scores_judge_the_final_output():
    result = compile_source(EXAMPLE)
    report = result["evaluation"][0]
    assert report["scope"] == "end_to_end"
    assert {s["agent"]: s["passed"] for s in report["scores"]} == {"triage_a": 3, "triage_b": 4}
    first = report["cases"][0]["results"]
    # the loose label "This message is urgent." is not "urgent", so the urgent ticket goes to the normal queue
    assert [(r["agent"], r["output"], r["passed"]) for r in first] == [
        ("triage_a", "queued", False), ("triage_b", "escalated", True)]
    assert report["applied"] == "triage_b"


def test_alone_scope_is_the_default_and_is_reported():
    result = compile_source((EXAMPLES_DIR / "valid" / "compare_agents.af").read_text())
    assert result["evaluation"][0]["scope"] == "agent"


def test_a_bad_candidate_can_pass_by_luck_when_the_else_branch_hides_it():
    # case 2 and 4 of the example expect "queued": the loose agent lands on the else branch by accident
    report = compile_source(EXAMPLE)["evaluation"][0]
    assert all(r["passed"] for case in report["cases"][1:] for r in case["results"])


def test_run_flow_follows_conditions_in_order_and_stops_at_end():
    graph, symbols = graph_of(BASE)
    state = {"message": "", "priority": "", "note": "", "action": ""}
    assert run_flow(graph, symbols, state, {"a": "urgent"})["action"] == "escalated"
    assert run_flow(graph, symbols, state, {"a": "whatever"})["action"] == "queued"


def test_run_flow_honours_max_iterations():
    graph, symbols = graph_of("""
state S { input q: string  done: bool  output r: string }
agent a { state.r = "x" }
flow main {
  start -> a
  a -[state.done == false, max_iterations 3]-> a
  a -[else]-> end
}
""")
    assert run_flow(graph, symbols, {"q": "", "done": False, "r": ""}, {})["r"] == "x"   # it stops, it does not loop forever


def test_a_workflow_that_stops_scores_a_fail_instead_of_crashing():
    source = program("  end_to_end\n" + CASE).replace("  a -[else]-> enqueue\n", "")
    report = compile_source(source)["evaluation"][0]
    stopped = [r for case in report["cases"] for r in case["results"] if "workflow stopped" in str(r["output"])]
    assert stopped and not any(r["passed"] for r in stopped)


def test_the_selected_winner_changes_the_generated_workflow():
    code = compile_source(EXAMPLE)["phases"]["codegen"]["code"]
    assert "def node_triage_b" in code and "def node_triage_a" not in code   # the loser is not in the flow
    assert 'graph.add_node("triage_b"' in code and "FLOW_LIMIT" in code


# ---------- the optimizer must keep what `ensure` reads ----------

def test_a_field_only_the_ensure_check_reads_is_not_removed_as_dead():
    source = BASE.replace('agent a { state.priority = llm("loose", state.message) }',
                          'agent a { state.priority = llm("loose", state.message)\n  state.note = llm("explain", state.message) }')
    source = source.replace('agent b { state.priority = llm("strict", state.message) }',
                            'agent b { state.priority = llm("strict", state.message)\n  state.note = llm("explain", state.message) }')
    without = compile_source(source + "\ncompare c { candidates a, b\n  end_to_end\n" + CASE + "}\n")
    with_check = compile_source(
        source + '\ncompare c { candidates a, b\n  end_to_end\n  ensure state.note != ""\n' + CASE + "}\n")
    assert any(r["field"] == "note" for r in without["phases"]["optimize"]["removed_stores"])      # dead without the check
    assert not any(r["field"] == "note" for r in with_check["phases"]["optimize"]["removed_stores"])  # live with it


# ---------- the generated file ----------

def write(tmp_path, source, select=False):
    result = compile_source(source, "x.af", select=select)
    assert result["ok"], result["diagnostics"]
    path = tmp_path / "gen_workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    return result, path


def load(path):
    spec = importlib.util.spec_from_file_location("gen_workflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_generated_scores_match_the_compiler_for_a_whole_workflow_test(tmp_path):
    result, path = write(tmp_path, EXAMPLE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFORGE_")}
    out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    report = result["evaluation"][0]
    for s in report["scores"]:
        assert f"{s['agent']}: {s['passed']}/{s['total']}" in out.stdout
    for case in report["cases"]:
        for r in case["results"]:
            assert f"{'PASS' if r['passed'] else 'FAIL'}  {r['agent']}: {r['output']}" in out.stdout
    assert "each case runs the whole workflow" in out.stdout


class Reply:
    def __init__(self, text):
        self.body = json.dumps({"content": [{"type": "text", "text": text}]}).encode()

    def read(self, *args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def real_mode(module, monkeypatch, answers):
    """Make the module think a real model is on the other end; `answers` maps a prompt to its reply."""
    asked = []

    def fake(request, timeout=None):
        prompt = json.loads(request.data)["messages"][0]["content"]
        asked.append(prompt)
        for key, reply in answers.items():
            if prompt.startswith(key):
                return Reply(reply)
        raise AssertionError(f"unexpected prompt {prompt!r}")

    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFORGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    return asked


def run_once(module, message="down"):
    state = {**module.base_state(), **module.HIDDEN_STATE, "message": message}
    return module.build_app().invoke(state, {"recursion_limit": module.FLOW_LIMIT})


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_ensure_falls_back_to_the_next_candidate_when_the_answer_is_bad(tmp_path, monkeypatch, capsys):
    _, path = write(tmp_path, EXAMPLE, select=False)       # triage_a stays in the flow
    module = load(path)
    asked = real_mode(module, monkeypatch, {"Say whether": "This message is urgent.", "Classify the support": "urgent"})
    final = run_once(module)
    assert final["priority"] == "urgent" and final["action"] == "escalated"      # the bad label never reached the router
    assert len(asked) == 2                                                       # the wired agent, then the fallback
    assert "ensure failed for 'triage_a'; used 'triage_b' instead" in capsys.readouterr().err


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_ensure_does_nothing_when_the_answer_is_good(tmp_path, monkeypatch, capsys):
    _, path = write(tmp_path, EXAMPLE, select=False)
    module = load(path)
    asked = real_mode(module, monkeypatch, {"Say whether": "normal"})
    final = run_once(module)
    assert final["action"] == "queued" and len(asked) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_ensure_stops_the_run_when_no_candidate_gives_a_good_answer(tmp_path, monkeypatch):
    _, path = write(tmp_path, EXAMPLE, select=False)
    module = load(path)
    real_mode(module, monkeypatch, {"Say whether": "dunno", "Classify the support": "maybe"})
    with pytest.raises(RuntimeError, match="ensure failed for every candidate of compare 'best_triage'"):
        run_once(module)


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_ensure_is_not_applied_to_mock_answers(tmp_path):
    _, path = write(tmp_path, EXAMPLE, select=False)
    module = load(path)
    final = run_once(module)                               # mock mode: the placeholder is not "urgent" or "normal"
    assert final["action"] == "queued"                     # and nothing raised


def test_no_ensure_means_no_guard_in_the_generated_code():
    with_ensure = compile_source(EXAMPLE)["phases"]["codegen"]["code"]
    without = compile_source(program("  end_to_end\n" + CASE))["phases"]["codegen"]["code"]
    assert 'guarded("best_triage", "triage_b", node_triage_b)' in with_ensure
    assert 'guarded("c"' not in without and "ENSURES = {\n}" in without

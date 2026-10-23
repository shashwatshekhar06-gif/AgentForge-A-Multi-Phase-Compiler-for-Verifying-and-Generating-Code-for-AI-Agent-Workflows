"""`necessity`: which agents does the workflow need? Static classification, measured pruning, the generated harness."""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from compiler.ir import build_ir
from compiler.necessity import base_report, bypass_agent, classify, agents_to_test
from compiler.optimize import prune_unneeded
from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR, codes, graph_of

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
DEMO = (EXAMPLES_DIR / "valid" / "necessity_demo.af").read_text()

# what a generated file prints with --necessity-json when the reviewer turned out to be useless
MEASURED = [{"name": "trim_workflow", "total": 3, "baseline": 3, "after": 3,
             "agents": [{"agent": "drafter", "score_without": 0, "needed": True},
                        {"agent": "reviewer", "score_without": 3, "needed": False}]}]


def codes_of(source):
    return [d["code"] for d in compile_source(source)["diagnostics"]]


def agents_in(code):
    return [line.split("(")[0][len("def node_"):] for line in code.splitlines() if line.startswith("def node_")]


# ---------- the language ----------

def test_a_necessity_block_parses_and_shows_up_in_the_symbols():
    result = compile_source(DEMO)
    assert result["ok"], result["diagnostics"]
    assert result["phases"]["semantic"]["symbols"]["necessities"] == [{"name": "trim_workflow", "cases": 3}]


def test_a_necessity_block_is_checked_like_the_cases_of_a_compare_block():
    head = DEMO[:DEMO.index("necessity trim_workflow")]
    assert "AF-E214" in codes_of(head + "necessity n { }")                                          # no case
    assert "AF-E202" in codes_of(head + 'necessity n { case { expect state.nope == "x" } }')        # unknown field
    assert "AF-E203" in codes_of(head + 'necessity n { case { expect state.answer == 3 } }')        # wrong type
    assert "AF-E214" in codes_of(head + 'necessity n { case { expect state.answer contains "x"\n mock drafter "y" } }')
    assert "AF-E218" in codes_of(DEMO + 'necessity second { case { expect state.answer contains "x" } }')


def test_the_keyword_does_not_break_programs_without_it():
    assert "necessity" not in compile_source((EXAMPLES_DIR / "valid" / "research.af").read_text())["phases"]["codegen"]["code"].lower()
    assert compile_source((EXAMPLES_DIR / "valid" / "research.af").read_text())["necessity"] == []


# ---------- static classification ----------

def test_the_demo_classification():
    report = compile_source(DEMO)["necessity"][0]
    assert report["mode"] == "static" and report["baseline"] is None
    status = {row["agent"]: row["status"] for row in report["agents"]}
    assert status == {"drafter": "testable", "reviewer": "testable", "finisher": "structural"}
    finisher = next(r for r in report["agents"] if r["agent"] == "finisher")
    assert "AF-" not in finisher["reason"] and "state.answer is not written" in finisher["reason"]
    assert report["model_calls"] == 3 * (1 + 2) * 3        # 3 cases x (as written + 2 testable agents) x 3 calls per run


def test_a_static_report_removes_nothing():
    result = compile_source(DEMO)
    assert result["phases"]["optimize"]["pruned"] == []
    assert set(agents_in(result["phases"]["codegen"]["code"])) == {"drafter", "reviewer", "finisher"}


TOOLS_AND_BRANCHES = """
state S {
  input q: string
  hits: list[string] = []
  route: string = "a"
  output answer: string
}
tool search(q: string) -> list[string]
agent researcher { state.hits = search(state.q) }
agent chooser { state.route = llm("pick", state.q) one_of ["a", "b"] }
agent left { state.answer = llm("left", state.q) }
agent right { state.answer = llm("right", state.q) }
flow main {
  start -> researcher -> chooser
  chooser -[state.route == "a"]-> left
  chooser -[else]-> right
  left -> end
  right -> end
}
necessity n { case { state.q = "x"
 expect state.answer contains "x" } }
"""


def test_agents_with_tools_branches_or_a_compare_block_are_not_tested():
    status = {row["agent"]: row["status"] for row in compile_source(TOOLS_AND_BRANCHES)["necessity"][0]["agents"]}
    assert status["researcher"] == "tool" and status["chooser"] == "branching"
    assert status["left"] == "structural" and status["right"] == "structural"      # each is the only writer on its path
    with_compare = DEMO.replace("agent reviewer", 'agent drafter_b { state.draft = llm("Answer briefly.", state.question) }\nagent reviewer').replace(
        "necessity trim_workflow", """compare c { candidates drafter, drafter_b
  case { state.question = "q"
 expect state.draft contains "x" } }
necessity trim_workflow""")
    compared = compile_source(with_compare)["necessity"][0]
    assert {r["agent"]: r["status"] for r in compared["agents"]}["drafter"] == "candidate"


def test_bypass_agent_rewires_the_graph():
    graph, symbols = graph_of(DEMO)
    skipped = bypass_agent(graph, "reviewer")
    assert "reviewer" not in skipped.nodes
    assert [(e.source, e.target) for e in skipped.edges] == [("start", "drafter"), ("drafter", "finisher"), ("finisher", "end")]
    assert len(graph.edges) == 4 and "reviewer" in graph.nodes                       # the original is untouched


def test_the_most_expensive_agents_are_tested_first():
    rows = [{"agent": "a", "llm_calls": 1, "status": "testable"}, {"agent": "b", "llm_calls": 3, "status": "testable"},
            {"agent": "c", "llm_calls": 9, "status": "tool"}, {"agent": "d", "llm_calls": 3, "status": "testable"}]
    assert agents_to_test(rows) == ["b", "d", "a"]


# ---------- measured pruning ----------

def test_a_measured_profile_removes_the_agent_that_was_not_needed():
    result = compile_source(DEMO, profile={"compare": [], "necessity": MEASURED})
    report = result["necessity"][0]
    assert report["mode"] == "measured" and report["baseline"] == 3
    assert report["removable"] == ["reviewer"] and report["applied"] == ["reviewer"]
    assert result["phases"]["optimize"]["pruned"] == [{
        "necessity": "trim_workflow", "agent": "reviewer", "llm_calls_saved": 1, "score_with": 3, "score_without": 3, "total": 3}]
    code = result["phases"]["codegen"]["code"]
    assert agents_in(code) == ["drafter", "finisher"] and 'graph.add_edge("drafter", jump("finisher"))' in code
    assert result["phases"]["cost"]["worst"]["llm"] == 2                              # the cost analysis sees the pruned workflow


def test_the_graph_shown_in_the_ui_is_the_program_as_written():
    result = compile_source(DEMO, profile={"compare": [], "necessity": MEASURED})
    assert [n["id"] for n in result["phases"]["ir"]["nodes"]] == ["start", "drafter", "reviewer", "finisher", "end"]


def test_no_select_keeps_every_agent():
    result = compile_source(DEMO, profile={"compare": [], "necessity": MEASURED}, select=False)
    assert result["necessity"][0]["applied"] == [] and result["phases"]["optimize"]["pruned"] == []
    assert "reviewer" in agents_in(result["phases"]["codegen"]["code"])


def test_a_needed_agent_stays_and_a_stale_profile_is_ignored():
    keep = [{**MEASURED[0], "agents": [{"agent": "drafter", "score_without": 0, "needed": True},
                                       {"agent": "reviewer", "score_without": 1, "needed": True}]}]
    kept = compile_source(DEMO, profile={"compare": [], "necessity": keep})
    assert kept["necessity"][0]["applied"] == [] and len(agents_in(kept["phases"]["codegen"]["code"])) == 3
    stale = [{**MEASURED[0], "agents": [{"agent": "ghost", "score_without": 3, "needed": False}]}]
    assert compile_source(DEMO, profile={"compare": [], "necessity": stale})["necessity"][0]["mode"] == "static"
    wrong_cases = [{**MEASURED[0], "total": 5}]
    assert compile_source(DEMO, profile={"compare": [], "necessity": wrong_cases})["necessity"][0]["mode"] == "static"


def test_a_plain_list_is_still_a_compare_profile():
    source = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()
    assert compile_source(source, profile=[])["evaluation"][0]["mode"] == "mock"
    assert compile_source(source, profile={"compare": None, "necessity": None})["evaluation"][0]["mode"] == "mock"


def test_a_removal_that_would_break_verification_is_refused():
    graph, symbols = graph_of(DEMO)
    report = base_report(symbols.necessities["trim_workflow"], classify(graph, symbols), graph)
    report["mode"], report["baseline"] = "measured", 3
    report["removable"] = ["finisher"]                     # a (wrong) measurement: nothing else writes state.answer
    report["agents"][2]["score_without"], report["agents"][2]["needed"] = 3, False
    pruned_graph, pruned = prune_unneeded(graph, symbols, [report])
    assert pruned == [] and "finisher" in pruned_graph.nodes
    assert "would add a verification problem" in report["blocked"][0]


def test_the_optimizer_keeps_what_the_remaining_agents_read():
    # pruning the reviewer leaves `review` at its default "", which the finisher reads: verification still passes
    result = compile_source(DEMO, profile={"compare": [], "necessity": MEASURED})
    assert result["ok"] and not [d for d in result["diagnostics"] if d["severity"] == "error"]


# ---------- the generated file ----------

def write(tmp_path, source, **kwargs):
    result = compile_source(source, "x.af", **kwargs)
    assert result["ok"], result["diagnostics"]
    path = tmp_path / "gen_workflow.py"
    path.write_text(result["phases"]["codegen"]["code"])
    return path


def load(path):
    spec = importlib.util.spec_from_file_location("gen_workflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(path, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTFORGE_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_mock_answers_prove_nothing_so_nothing_is_tested(tmp_path):
    path = write(tmp_path, DEMO)
    out = run(path, "--necessity")
    assert out.returncode == 0, out.stderr
    assert "mock mode" in out.stdout and "nothing was tested" in out.stdout
    data = json.loads(next(l for l in run(path, "--necessity-json").stdout.splitlines()
                           if l.startswith("AGENTFORGE_NECESSITY="))[len("AGENTFORGE_NECESSITY="):])
    assert data[0]["baseline"] == 0
    assert all(a["needed"] and a["score_without"] is None for a in data[0]["agents"])     # no evidence: all stay
    assert "path: start -> drafter -> reviewer -> finisher -> end" in run(path).stdout    # the normal run is unchanged


class Reply:
    def __init__(self, text):
        self.body = json.dumps({"content": [{"type": "text", "text": text}]}).encode()

    def read(self, *args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CITIES = {"France": "Paris", "Japan": "Tokyo", "Spain": "Madrid"}


def fake_model(module, monkeypatch, knows_the_cities=True):
    """A model that answers the demo's three prompts; returns the list of prompts it was asked."""
    asked = []

    def fake(request, timeout=None):
        prompt = json.loads(request.data)["messages"][0]["content"]
        asked.append(prompt)
        if prompt.startswith("Answer the question"):
            country = next((c for c in CITIES if c in prompt), None)
            return Reply(f"The capital of {country} is {CITIES[country]}." if knows_the_cities else "no idea")
        if prompt.startswith("Point out"):
            return Reply("It looks fine.")
        context = prompt.split("Context:")[-1]
        city = next((v for v in CITIES.values() if v in context), None)
        return Reply(f"The answer is {city}." if city else "I do not know.")

    monkeypatch.setattr(module, "LLM_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFORGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    return asked


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_the_harness_leaves_agents_out_and_keeps_the_ones_that_are_not_needed_out(tmp_path, monkeypatch):
    module = load(write(tmp_path, DEMO))
    asked = fake_model(module, monkeypatch)
    report = module.run_necessity(module.NECESSITIES[0])
    assert report["baseline"] == 3 and report["total"] == 3
    assert report["agents"] == [{"agent": "drafter", "score_without": 0, "needed": True},
                                {"agent": "reviewer", "score_without": 3, "needed": False}]
    assert report["after"] == 3
    # 3 cases x 3 agents as written, then without the drafter (2 agents), then without the reviewer (2 agents)
    assert len(asked) == 9 + 6 + 6
    # skipping really removes the agent from the graph, and the rest still runs end to end
    assert sorted(module.build_app(skip=["reviewer"]).get_graph().nodes) == ["__end__", "__start__", "drafter", "finisher"]


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_the_harness_makes_no_extra_calls_when_the_workflow_fails_every_case(tmp_path, monkeypatch):
    module = load(write(tmp_path, DEMO))
    asked = fake_model(module, monkeypatch, knows_the_cities=False)
    report = module.run_necessity(module.NECESSITIES[0])
    assert report["baseline"] == 0 and all(a["needed"] for a in report["agents"])
    assert len(asked) == 9                                  # only the baseline run: no evidence, so no more paid calls


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph is not installed")
def test_a_pruned_file_runs_and_a_program_without_necessity_has_no_harness(tmp_path):
    path = write(tmp_path, DEMO, profile={"compare": [], "necessity": MEASURED})
    out = run(path)
    assert out.returncode == 0, out.stderr
    assert "path: start -> drafter -> finisher -> end" in out.stdout
    plain = compile_source((EXAMPLES_DIR / "valid" / "compare_agents.af").read_text())["phases"]["codegen"]["code"]
    assert "NECESSITIES" not in plain and "NEXT_OF" not in plain


# ---------- the command line ----------

def test_cli_profile_real_measures_necessity_and_prunes(tmp_path, monkeypatch, capsys):
    from compiler import cli

    flags = []

    def fake_run(command, capture_output, text, env, timeout=None):
        flags.append(command[-1])
        assert env["AGENTFORGE_LLM"] == "real"
        return subprocess.CompletedProcess(command, 0, stdout="noise\nAGENTFORGE_NECESSITY=" + json.dumps(MEASURED) + "\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    source_file = tmp_path / "necessity_demo.af"
    source_file.write_text(DEMO)
    out_file = tmp_path / "workflow.py"
    assert cli.main([str(source_file), "-o", str(out_file), "--profile", "real"]) == 0
    assert flags == ["--necessity-json"]                    # there is no compare block, so only one harness run
    printed = capsys.readouterr().out
    assert "drafter: needed (0/3 without it)" in printed
    assert "reviewer: not needed (3/3 without it); removed from the generated code, saving 1 LLM call(s) per run" in printed
    assert "finisher: structural" in printed
    assert "node_reviewer" not in out_file.read_text()


def test_cli_prints_the_static_report_without_a_model(tmp_path, capsys):
    from compiler import cli

    source_file = tmp_path / "necessity_demo.af"
    source_file.write_text(DEMO)
    assert cli.main([str(source_file)]) == 0
    printed = capsys.readouterr().out
    assert "not measured: use --profile real" in printed and "drafter: can be tested" in printed


def test_cli_measures_the_compare_blocks_first_then_the_necessity_of_the_chosen_workflow(tmp_path, monkeypatch):
    from compiler import cli

    source = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text() + """
necessity trim { case { state.query = "q"
 expect state.response contains "x" } }
"""
    calls = []

    def fake_run(command, capture_output, text, env, timeout=None):
        calls.append(command[-1])
        if command[-1] == "--scores-json":
            from test_compare import measured_profile
            return subprocess.CompletedProcess(command, 0, stdout="AGENTFORGE_SCORES=" + json.dumps(measured_profile([False] * 3, [True] * 3)) + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="AGENTFORGE_NECESSITY=[]\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    profile, problem = cli.measure_with_real_model(source, "x.af")
    assert problem is None and calls == ["--scores-json", "--necessity-json"]
    assert profile["compare"][0]["name"] == "best_responder" and profile["necessity"] == []


# ---------- the API ----------

def test_the_api_can_measure_a_program_that_only_has_a_necessity_block(monkeypatch):
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AGENTFORGE_DISABLE_REAL", raising=False)
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: ({"compare": [], "necessity": MEASURED}, None))
    body = TestClient(api.app).post("/api/compile-real", json={"source": DEMO}).json()
    assert body["necessity"][0]["mode"] == "measured" and body["necessity"][0]["applied"] == ["reviewer"]
    monkeypatch.setattr(api, "MAX_REAL_CALLS", 26)         # the demo needs up to 27
    response = TestClient(api.app).post("/api/compile-real", json={"source": DEMO})
    assert response.status_code == 400 and "up to 27 model calls" in response.json()["detail"]


def test_the_normal_api_endpoint_never_measures_and_returns_the_static_report():
    from fastapi.testclient import TestClient
    import api

    body = TestClient(api.app).post("/api/compile", json={"source": DEMO}).json()
    assert body["necessity"][0]["mode"] == "static" and body["phases"]["optimize"]["pruned"] == []

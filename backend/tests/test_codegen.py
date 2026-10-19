import ast as python_ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from compiler.pipeline import compile_program, compile_source

from helpers import EXAMPLES_DIR, load_fixture

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None


def generated(name):
    result = compile_source((EXAMPLES_DIR / "valid" / name).read_text(), name)
    assert result["ok"], result["diagnostics"]
    return result["phases"]["codegen"]["code"]


def test_codegen_from_ast_fixture():
    result = compile_program(load_fixture("minimal.json"))
    assert result["ok"]
    code = result["phases"]["codegen"]["code"]
    compile(code, "minimal.py", "exec")
    assert 'graph.add_edge(START, "answer")' in code


def test_generated_file_structure():
    code = generated("research.af")
    assert 'AgentState = TypedDict("AgentState", {' in code
    assert "def node_classifier(state: AgentState)" in code
    assert "def tool_web_search(query: str) -> list[str]:" in code
    assert "def llm(prompt, *context, one_of=None):" in code
    assert "graph.add_conditional_edges(" in code
    assert "app = graph.compile()" in code
    assert "REAL MODEL CALL GOES HERE" in code


def test_compiler_does_not_import_langgraph():
    package = Path(__file__).parent.parent / "compiler"
    for source_file in package.glob("*.py"):
        tree = python_ast.parse(source_file.read_text())
        for node in python_ast.walk(tree):
            if isinstance(node, python_ast.Import):
                assert not any(a.name.startswith("langgraph") for a in node.names), source_file
            if isinstance(node, python_ast.ImportFrom):
                assert not (node.module or "").startswith("langgraph"), source_file


def test_bounded_loop_gets_a_counter():
    code = generated("fraud.af")
    assert '"_count_0": int,' in code
    assert 's["_count_0"] < 3' in code


def test_dead_agent_is_not_emitted():
    name = "unreachable_agent.af"
    result = compile_source((EXAMPLES_DIR / "invalid" / name).read_text(), name)
    assert result["phases"]["optimize"]["removed_agents"] == ["summarizer"]
    assert "summarizer" not in result["phases"]["codegen"]["code"]


def run_generated(code, tmp_path, env_extra=None):
    path = tmp_path / "workflow.py"
    path.write_text(code)
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph not installed")
def test_research_runs_both_branches(tmp_path):
    code = generated("research.af")
    factual = run_generated(code, tmp_path)
    assert factual.returncode == 0, factual.stderr
    assert "start -> classifier -> researcher -> responder -> end" in factual.stdout
    chat = run_generated(code, tmp_path, {"AGENTFORGE_CHOICE": "conversational"})
    assert "start -> classifier -> responder -> end" in chat.stdout


@pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph not installed")
def test_bounded_loop_stops_at_the_limit(tmp_path):
    out = run_generated(generated("fraud.af"), tmp_path)
    assert out.returncode == 0, out.stderr
    # one first visit plus 3 loop iterations, then the else edge
    assert out.stdout.count("graph_analyst") == 4

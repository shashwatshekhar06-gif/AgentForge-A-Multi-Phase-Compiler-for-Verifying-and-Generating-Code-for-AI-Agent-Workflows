"""Tests for the two cost-saving analyses: dead LLM call elimination and static cost analysis."""

import importlib.util
import os
import random
import subprocess
import sys

import pytest

from compiler.cost import analyze_cost
from compiler.ir import build_ir
from compiler.lexer import tokenize
from compiler.parser import parse
from compiler.pipeline import compile_source
from compiler.semantic import analyze

from helpers import EXAMPLES_DIR
from test_fuzz import random_workflow

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None


def prog(fields="", decls="", flow=""):
    return f"state S {{ input q: string\n output a: string\n {fields} }}\n{decls}\nflow f {{ {flow} }}"


def result_of(source):
    result = compile_source(source)
    assert result["ok"], result["diagnostics"]
    return result


def example(name):
    return result_of((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def removed(result):
    return [(r["agent"], r["field"]) for r in result["phases"]["optimize"]["removed_stores"]]


# =============================================================== dead LLM call elimination

class TestDeadLlmCalls:
    def test_wasted_calls_example(self):
        result = example("invalid/wasted_llm_calls.af")
        assert removed(result) == [("researcher", "keywords"), ("writer", "title"), ("editor", "tone")]
        code = result["phases"]["codegen"]["code"]
        assert "SEO keywords" not in code and "working title" not in code and "tone of this draft" not in code
        assert "catchy final title" in code   # the second write to title IS used (by headline)
        assert result["phases"]["cost"]["worst"]["llm"] == 4
        assert result["phases"]["cost"]["removed_llm_calls"] == 3

    def test_valid_examples_have_nothing_to_remove(self):
        for path in sorted((EXAMPLES_DIR / "valid").glob("*.af")):
            assert removed(example(f"valid/{path.name}")) == [], path.name

    def test_tool_calls_are_kept_even_if_unused(self):
        # a tool may send an email or write to a database: removing it would change behaviour
        result = result_of(prog(fields="x: string", decls='tool t(v: string) -> string\nagent w { state.x = t(state.q)\n state.a = "k" }',
                                flow="start -> w -> end"))
        assert removed(result) == []
        assert "tool_t(" in result["phases"]["codegen"]["code"].split("def node_w")[1]

    def test_value_read_only_by_an_edge_condition_is_live(self):
        result = result_of(prog(fields="r: string", decls='agent w { state.r = llm("route?")\n state.a = "k" }',
                                flow='start -> w\nw -[state.r == "x"]-> end\nw -[else]-> end'))
        assert removed(result) == []

    def test_value_used_by_the_next_loop_iteration_is_live(self):
        result = result_of(prog(fields='n: string = ""', decls='agent w { state.n = llm("refine", state.n)\n state.a = state.n }',
                                flow='start -> w\nw -[state.q == "again", max_iterations 3]-> w\nw -[else]-> end'))
        assert removed(result) == []

    def test_overwritten_on_only_one_branch_is_live(self):
        decls = ('agent w { state.x = llm("first") }\nagent p { state.x = llm("second") }\n'
                 'agent r { state.a = state.x }')
        flow = 'start -> w\nw -[state.q == "a"]-> p\nw -[else]-> r\np -> r\nr -> end'
        assert removed(result_of(prog(fields="x: string", decls=decls, flow=flow))) == []

    def test_overwritten_on_every_path_is_dead(self):
        decls = ('agent w { state.x = llm("first") }\nagent p { state.x = llm("second") }\n'
                 'agent p2 { state.x = llm("third") }\nagent r { state.a = state.x }')
        flow = 'start -> w\nw -[state.q == "a"]-> p\nw -[else]-> p2\np -> r\np2 -> r\nr -> end'
        assert removed(result_of(prog(fields="x: string", decls=decls, flow=flow))) == [("w", "x")]

    def test_chains_of_dead_values_are_all_removed(self):
        # b is never read, so the call computing b is dead, so a (only used by b) is dead too
        result = result_of(prog(fields="x: string\n y: string",
                                decls='agent w { state.x = llm("one")\n state.y = llm("two", state.x)\n state.a = "k" }',
                                flow="start -> w -> end"))
        assert sorted(removed(result)) == [("w", "x"), ("w", "y")]

    def test_dead_write_inside_a_branch(self):
        result = result_of(prog(fields="x: string",
                                decls='agent w { if (state.q == "a") { state.x = llm("unused") }\n state.a = "k" }',
                                flow="start -> w -> end"))
        assert removed(result) == [("w", "x")]
        assert "unused" not in result["phases"]["codegen"]["code"]

    def test_outputs_are_never_removed(self):
        result = result_of(prog(decls='agent w { state.a = llm("answer") }', flow="start -> w -> end"))
        assert removed(result) == []

    def test_savings_count_loop_visits(self):
        # the dead call sits in an agent that can run 1 + 2 = 3 times, so it saves 3 calls per run
        result = result_of(prog(fields="x: string",
                                decls='agent w { state.x = llm("unused")\n state.a = "k" }',
                                flow='start -> w\nw -[state.q == "again", max_iterations 2]-> w\nw -[else]-> end'))
        assert result["phases"]["cost"]["removed_llm_calls"] == 3

    @pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph not installed")
    def test_optimized_program_still_runs(self, tmp_path):
        code = example("invalid/wasted_llm_calls.af")["phases"]["codegen"]["code"]
        path = tmp_path / "workflow.py"
        path.write_text(code, encoding="utf-8")
        out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert out.returncode == 0, out.stderr
        assert "start -> researcher -> writer -> editor -> end" in out.stdout


# =============================================================== static cost analysis

class TestCostAnalysis:
    def test_research_example(self):
        cost = example("valid/research.af")["phases"]["cost"]
        assert cost["worst"] == {"llm": 2, "tool": 1}    # factual path: classify, search, answer
        assert cost["best"] == {"llm": 2, "tool": 0}     # conversational path skips the search

    def test_fraud_example_counts_loop_iterations(self):
        cost = example("valid/fraud.af")["phases"]["cost"]
        visits = {a["id"]: a["max_visits"] for a in cost["agents"]}
        assert visits["graph_analyst"] == 4               # first visit + max_iterations 3
        assert cost["worst"] == {"llm": 2, "tool": 8}     # 2 tools x 4 visits
        assert cost["best"] == {"llm": 1, "tool": 0}      # big amount: triage uses no LLM, skips analysis

    def test_insurance_example(self):
        cost = example("valid/insurance_claims.af")["phases"]["cost"]
        assert cost["worst"] == {"llm": 9, "tool": 15}
        assert cost["best"] == {"llm": 3, "tool": 3}      # inactive policy: straight to rejection

    def test_unbounded_loop(self):
        result = example("invalid/unbounded_cost.af")
        cost = result["phases"]["cost"]
        assert cost["worst"]["llm"] is None               # no upper bound
        assert cost["best"]["llm"] == 3
        assert cost["unbounded_loops"] == [["writer", "reviewer", "writer"]]
        assert [d["code"] for d in result["diagnostics"]] == ["AF-W308"]

    def test_bounding_the_loop_removes_the_warning(self):
        source = (EXAMPLES_DIR / "invalid" / "unbounded_cost.af").read_text(encoding="utf-8")
        fixed = source.replace("reviewer -[not state.approved]-> writer", "reviewer -[not state.approved, max_iterations 3]-> writer")
        result = result_of(fixed)
        assert result["diagnostics"] == []
        assert result["phases"]["cost"]["worst"]["llm"] == 3 * 4   # writer 1 + reviewer 2, up to 4 passes

    def test_loop_without_calls_is_not_a_cost_risk(self):
        result = result_of(prog(decls='agent w { }\nagent hub { state.a = "h" }',
                                flow='start -> hub\nhub -[state.q == "x"]-> w\nhub -[else]-> end\nw -> hub'))
        assert result["diagnostics"] == []
        assert result["phases"]["cost"]["worst"]["llm"] == 0

    def test_if_branches(self):
        decls = 'agent w { if (state.q == "a") { state.x = llm("one") } else { state.x = "free" }\n state.a = llm("two", state.x) }'
        cost = result_of(prog(fields="x: string", decls=decls, flow="start -> w -> end"))["phases"]["cost"]
        assert cost["worst"]["llm"] == 2 and cost["best"]["llm"] == 1

    def test_short_circuit_conditions(self):
        decls = 'agent w { if (state.q == "a" or llm("ask") == "yes") { state.a = "1" } else { state.a = "2" } }'
        cost = result_of(prog(decls=decls, flow="start -> w -> end"))["phases"]["cost"]
        assert cost["worst"]["llm"] == 1 and cost["best"]["llm"] == 0   # `or` may skip the llm call

    @pytest.mark.parametrize("seed", range(4))
    def test_matches_brute_force_path_enumeration(self, seed):
        """Compare with every simple path from start to end:
        best case must equal the cheapest path; worst case must equal the most expensive path when
        the graph has no loops, and be at least that much (or unbounded) when it does."""
        rng = random.Random(500 + seed)
        checked = 0
        for _ in range(60):
            source, model = random_workflow(rng)
            program, _ = parse(tokenize(source)[0])
            symbols, diags = analyze(program)
            graph = build_ir(symbols)
            report, _ = analyze_cost(graph)

            succ = {}
            for s, t, *_ in model["edges"]:
                succ.setdefault(s, []).append(t)
            paths, stack = [], [["start"]]
            while stack:
                path = stack.pop()
                if path[-1] == "end":
                    paths.append(path)
                    continue
                stack += [path + [m] for m in succ.get(path[-1], []) if m not in path]
            if not paths:
                continue
            costs = [sum(model["llm"].get(n, 0) for n in p) for p in paths]
            assert report["best"]["llm"] == min(costs), source
            # nodes that are on some run from start to end, and whether any of them lies on a loop
            between = {n for n in reach_from(succ, "start") | {"start"} if n == "end" or "end" in reach_from(succ, n)}
            has_loop = any(n in reach_from(succ, n) for n in between)
            if not has_loop:
                assert report["worst"]["llm"] == max(costs), source
            else:
                assert report["worst"]["llm"] is None or report["worst"]["llm"] >= max(costs), source
            checked += 1
        assert checked > 20


def reach_from(succ, node):
    seen, stack = set(), [node]
    while stack:
        n = stack.pop()
        for m in succ.get(n, []):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen

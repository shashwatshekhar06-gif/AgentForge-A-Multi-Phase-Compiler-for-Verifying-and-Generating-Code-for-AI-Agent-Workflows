"""Evaluation: which candidate of a `compare` block gives the better output?

The compiler cannot know what a language model will answer, so quality has to be MEASURED. Think of
profile-guided optimization in an ordinary compiler: run the program on sample inputs, record what happened,
then compile again using the measurements. Here the sample inputs are the test cases of a `compare` block,
and the measurement is how many cases each candidate passes. The optimizer (optimize.select_agents) then
wires the best candidate into the generated code.

Where the measurements come from:
  - MOCK profile (default): each case carries the answer every candidate gives (`mock responder_a "..."`).
    No model is involved, so it is offline and repeatable, and shows the mechanism. It says nothing about
    a real model.
  - MEASURED profile: the generated file was run against a real model (AGENTFORGE_LLM=real) and its scores
    were passed back in (`python -m compiler file.af --profile real`).

A block can be judged in two ways. By default each candidate is run ALONE and its own output is judged. With
`end_to_end` the whole workflow runs with that candidate in the wired place and the FINAL output is judged, so
a candidate that looks fine alone but hurts the agents after it loses.

This module is a very small interpreter for agent bodies and for the flow that connects them. It exists so the compiler (and the web UI) can
produce the mock profile without running any generated code on the server. The generated Python file
contains the same logic; tests/test_compare.py runs both and checks they agree.
"""

from . import ast_nodes as ast
from .candidates import swap_candidate, wired_candidates
from .ir import IRGraph, count_calls
from .semantic import SymbolTable


def empty_value(t: str):
    if t.startswith("list["):
        return []
    return {"string": "", "int": 0, "bool": False}[t]


def sample_value(t: str, label: str):
    """The same placeholder values the generated mock tools return (codegen.sample_value)."""
    if t.startswith("list["):
        inner = t[5:-1]
        return [sample_value(inner, label + " 1"), sample_value(inner, label + " 2")]
    return {"string": f"[{label}]", "int": 1, "bool": True}[t]


def literal_value(node):
    if isinstance(node, list):
        return [item.value for item in node]
    return node.value


class Interpreter:
    """Runs one agent body against a state dict, with a scripted answer for every llm(...) call."""

    def __init__(self, symbols: SymbolTable, scripted_answer):
        self.symbols = symbols
        self.scripted_answer = scripted_answer   # None = the default placeholder answer

    def run(self, body: list, state: dict) -> dict:
        state = dict(state)
        self.statements(body, state)
        return state

    def statements(self, body, state):
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                state[stmt.target.field] = self.expr(stmt.value, state)
            elif isinstance(stmt, ast.If):
                self.statements(stmt.then_body if self.expr(stmt.condition, state) else stmt.else_body, state)

    def expr(self, e, state):
        if isinstance(e, ast.Literal):
            return e.value
        if isinstance(e, ast.StateRef):
            return state[e.field]
        if isinstance(e, ast.Call):
            for arg in e.args:
                self.expr(arg, state)
            return sample_value(self.symbols.tools[e.name].return_type, "mock " + e.name + " result")
        if isinstance(e, ast.LlmCall):
            for arg in e.args:
                self.expr(arg, state)
            if e.one_of:
                options = [o.value for o in e.one_of]
                return self.scripted_answer if self.scripted_answer in options else options[0]
            return self.scripted_answer if self.scripted_answer is not None else f"[mock LLM answer to: {e.prompt}]"
        if isinstance(e, ast.UnaryOp):
            return not self.expr(e.operand, state)
        if isinstance(e, ast.BinaryOp):
            if e.op == "and":
                return self.expr(e.left, state) and self.expr(e.right, state)
            if e.op == "or":
                return self.expr(e.left, state) or self.expr(e.right, state)
            left, right = self.expr(e.left, state), self.expr(e.right, state)
            return {"==": left == right, "!=": left != right, "<": left < right, ">": left > right,
                    "<=": left <= right, ">=": left >= right}[e.op]
        raise ValueError(f"unknown expression {e!r}")


MAX_FLOW_STEPS = 10_000


def run_flow(graph: IRGraph, symbols: SymbolTable, start_state: dict, scripts: dict) -> dict:
    """Run the whole workflow once, the way the generated routers do, and return the final state.
    `scripts` maps an agent name to the scripted answer of its llm calls (the other agents answer with
    the default placeholder). Conditions are tried in source order; a loop edge is taken at most
    max_iterations times; an else edge is the fall-through."""
    state = dict(start_state)
    taken = {}
    node = "start"
    for _ in range(MAX_FLOW_STEPS):
        if node == "end":
            return state
        if graph.nodes[node].kind == "agent":
            state = Interpreter(symbols, scripts.get(node)).run(graph.nodes[node].body, state)
        out = graph.out_edges(node)
        if not out:
            return state
        probe = Interpreter(symbols, None)
        nxt = None
        if any(e.condition is not None for e in out):
            for e in out:
                if e.condition is None:
                    continue
                if probe.expr(e.condition, state) and (e.max_iterations is None or taken.get(id(e), 0) < e.max_iterations):
                    if e.max_iterations is not None:
                        taken[id(e)] = taken.get(id(e), 0) + 1
                    nxt = e.target
                    break
            if nxt is None:
                default = next((e for e in out if e.is_default), None)
                if default is None:
                    raise RuntimeError(f"no edge condition matched after '{node}' and there is no else edge")
                nxt = default.target
        else:
            nxt = out[0].target
        node = nxt
    raise RuntimeError("the workflow did not reach 'end' (too many steps)")


def base_state(symbols: SymbolTable) -> dict:
    """Every field starts with a value, the same rule the generated __main__ block uses."""
    state = {}
    for f in symbols.fields.values():
        if f.modifier == "input":
            state[f.name] = sample_value(f.type, "sample " + f.name)
        elif f.has_default:
            state[f.name] = literal_value(f.default)
        else:
            state[f.name] = empty_value(f.type)
    return state


def judge(op: str, output, expected) -> bool:
    if op == "contains":
        return str(expected).lower() in str(output).lower()
    return output == expected


def scope_of(info, wired: list) -> str:
    """"end_to_end" needs exactly one wired candidate (candidates.check_candidates reports AF-E217 otherwise)."""
    return "end_to_end" if info.decl.end_to_end and len(wired) == 1 else "agent"


def finish_report(info, wired: list, cases: list, mode: str) -> dict:
    """Scores, the best candidate and a tie flag, from the judged cases."""
    passed = {name: sum(r["passed"] for c in cases for r in c["results"] if r["agent"] == name)
              for name in info.candidates}
    total = len(cases)
    scores = [{"agent": n, "passed": passed[n], "total": total} for n in info.candidates]
    best_score = max(passed.values())
    leaders = [n for n in info.candidates if passed[n] == best_score]
    # on a tie keep the agent that is already in the flow, so nothing is swapped without a reason
    best = next((n for n in leaders if n in wired), leaders[0])
    return {"name": info.name, "mode": mode, "scope": scope_of(info, wired), "candidates": info.candidates,
            "wired": wired, "cases": cases, "scores": scores, "best": best, "tie": len(leaders) > 1,
            "applied": None,    # the candidate the optimizer put into the generated code instead (select_agents)
            "blocked": None}    # why a better candidate was not applied


def evaluate_compare(info, symbols: SymbolTable, wired: list, graph: IRGraph) -> dict:
    """MOCK profile: run every candidate with the scripted answers."""
    end_to_end = scope_of(info, wired) == "end_to_end"
    cases = []
    for index, case in enumerate(info.decl.cases, start=1):
        start = base_state(symbols)
        for stmt in case.inputs:
            start[stmt.target.field] = literal_value(stmt.value)
        exp = case.expects[0]
        scripts = {m.agent: m.text for m in case.mocks}
        results = []
        for name in info.candidates:
            try:
                if end_to_end:
                    in_place = graph if name == wired[0] else swap_candidate(graph, wired[0], name)
                    final = run_flow(in_place, symbols, start, {name: scripts.get(name)})
                else:
                    final = Interpreter(symbols, scripts.get(name)).run(symbols.agents[name].decl.body, start)
                output = final[exp.target.field]
                passed = judge(exp.op, output, exp.value.value)
            except RuntimeError as problem:   # the workflow itself stopped (for example no edge matched)
                output, passed = f"[workflow stopped: {problem}]", False
            results.append({"agent": name, "output": output, "passed": passed, "scripted": name in scripts})
        cases.append({
            "index": index,
            "inputs": {s.target.field: literal_value(s.value) for s in case.inputs},
            "expect": {"field": exp.target.field, "op": exp.op, "value": exp.value.value},
            "results": results,
        })
    return finish_report(info, wired, cases, "mock")


def report_from_profile(info, wired: list, measured: dict):
    """MEASURED profile: the report a generated file printed with --scores-json (see codegen.COMPARE_HELPERS).
    Returns None when it does not match this compare block (a stale file), so the caller falls back to mock."""
    if list(measured.get("candidates", [])) != list(info.candidates):
        return None
    cases = []
    for index, case in enumerate(measured.get("cases", []), start=1):
        results = [{"agent": r["agent"], "output": r["output"], "passed": bool(r["passed"]), "scripted": False}
                   for r in case["results"]]
        if [r["agent"] for r in results] != list(info.candidates):
            return None
        cases.append({"index": index, "inputs": case["inputs"], "expect": case["expect"], "results": results})
    if len(cases) != len(info.decl.cases):
        return None
    return finish_report(info, wired, cases, "measured")


def estimate_model_calls(info, graph: IRGraph, scope: str) -> int:
    """An upper bound on the paid model calls a real measurement of this block makes (shown before it runs).
    Alone, every candidate runs on every case. End to end, the agents around the candidate run too."""
    def calls(name):
        return count_calls(graph.nodes[name].body).llm_max if name in graph.nodes else 0

    cases = len(info.decl.cases)
    own = sum(calls(c) for c in info.candidates)
    if scope == "agent":
        return cases * own
    others = sum(calls(n) for n, node in graph.nodes.items() if node.kind == "agent" and n not in info.candidates)
    return cases * (own + len(info.candidates) * others)


def evaluate_all(symbols: SymbolTable, graph: IRGraph, profile=None) -> list:
    """One report per compare block (an empty list when the program has none).
    `profile` = reports measured with a real model; blocks it does not cover use the mock profile."""
    measured = {r.get("name"): r for r in profile or []}
    reports = []
    for info in symbols.compares.values():
        wired = wired_candidates(graph, info.candidates)
        report = report_from_profile(info, wired, measured[info.name]) if info.name in measured else None
        report = report or evaluate_compare(info, symbols, wired, graph)
        report["model_calls"] = estimate_model_calls(info, graph, report["scope"])
        reports.append(report)
    return reports

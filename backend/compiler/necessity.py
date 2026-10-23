"""Which agents does the workflow actually need?

Every agent costs paid LLM calls, but an agent can be useless in practice: the final answer is just as good without
it. Nothing in the program text shows that, so (like agent selection in evaluate.py) it has to be MEASURED. A
`necessity` block holds test cases. For each agent that can be skipped, the workflow runs on every case once with the
agent and once without it; if the final output is no worse without it, the agent is not needed and the optimizer
removes it from the generated code (optimize.prune_unneeded).

Not every agent can be tested, and the compiler says why:
  structural  skipping it makes verification fail (a later agent reads a field only it writes, an output is no longer
              written): the program itself proves the agent is needed, no model call required
  branching   it chooses the next step, so there is nothing to skip to
  tool        it calls a tool. Tools have real effects and are stubs until you implement them, so a measurement could
              not be trusted and the agent is never removed automatically
  candidate   it is compared in a `compare` block instead
  testable    skipping it keeps the workflow valid: the model decides

Agents are only removed on evidence: when the workflow as written passes none of the cases, nothing is tested and
nothing is removed.

Skipping an agent = its incoming edges go to the agent that followed it (bypass_agent). The measurement needs a real
model (the generated file runs it: `python workflow.py --necessity-json`), so without one the report only has the
static part and nothing is removed.
"""

from dataclasses import replace

from .ir import IRGraph, count_calls
from .semantic import SymbolTable
from .verify import reachable_from, verify


def bypass_agent(graph: IRGraph, name: str) -> IRGraph:
    """A copy of the graph without agent `name`: whatever led to it now leads to the agent that came after it.
    Only meaningful for an agent with exactly one plain outgoing edge."""
    successor = graph.out_edges(name)[0].target
    skipped = IRGraph()
    for node_id, node in graph.nodes.items():
        if node_id != name:
            skipped.nodes[node_id] = node
    for e in graph.edges:
        if e.source == name:
            continue
        skipped.edges.append(replace(e, target=successor) if e.target == name else e)
    return skipped


def problems(graph: IRGraph, symbols: SymbolTable) -> set:
    return {(d.code, d.message) for d in verify(graph, symbols)[1]}


def classify(graph: IRGraph, symbols: SymbolTable) -> list:
    """One row per reachable agent, in graph order: why it can or cannot be tested."""
    reachable = reachable_from(graph, "start")
    candidates = symbols.candidate_names()
    baseline = None
    rows = []
    for node_id, node in graph.nodes.items():
        if node.kind != "agent" or node_id not in reachable:
            continue
        calls = count_calls(node.body)
        out = graph.out_edges(node_id)
        row = {"agent": node_id, "llm_calls": calls.llm_max, "status": "testable", "reason": ""}
        if node_id in candidates:
            row["status"], row["reason"] = "candidate", "it is compared in a compare block instead"
        elif calls.tool_max > 0:
            row["status"] = "tool"
            row["reason"] = ("it calls a tool; tools can have real effects and are stubs until you implement them, "
                             "so it is never removed automatically")
        elif len(out) != 1 or out[0].condition is not None or out[0].is_default:
            row["status"], row["reason"] = "branching", "it chooses the next step, so there is nothing to skip to"
        else:
            if baseline is None:
                baseline = problems(graph, symbols)
            diags = [d for d in verify(bypass_agent(graph, node_id), symbols)[1] if (d.code, d.message) not in baseline]
            if diags:
                first = next((d for d in diags if d.is_error), diags[0])
                row["status"] = "structural"
                row["reason"] = f"without it verification fails: {first.message}"
        rows.append(row)
    return rows


def agents_to_test(rows: list) -> list:
    """The agents to test, the most expensive first (removing a costly one saves the most), then in flow order."""
    return [r["agent"] for r in sorted((r for r in rows if r["status"] == "testable"), key=lambda r: -r["llm_calls"])]


def estimate_model_calls(info, rows: list, graph: IRGraph) -> int:
    """An upper bound on the paid calls a real measurement makes: the whole workflow runs on every case, once as
    it is and once more for each testable agent."""
    per_run = sum(r["llm_calls"] for r in rows)
    return len(info.decl.cases) * (1 + len(agents_to_test(rows))) * per_run


def base_report(info, rows: list, graph: IRGraph) -> dict:
    return {"name": info.name, "mode": "static", "total": len(info.decl.cases), "baseline": None,
            "agents": [{**r, "score_without": None, "needed": None, "removed": False} for r in rows],
            "removable": [], "applied": [], "blocked": [], "model_calls": estimate_model_calls(info, rows, graph)}


def report_from_profile(info, rows: list, graph: IRGraph, measured: dict):
    """MEASURED: what the generated file printed with --necessity-json. None when it does not match this block
    (a stale file), so the caller falls back to the static report."""
    order = agents_to_test(rows)
    tested = measured.get("agents", [])
    if [t.get("agent") for t in tested] != order or measured.get("total") != len(info.decl.cases):
        return None
    report = base_report(info, rows, graph)
    report["mode"], report["baseline"] = "measured", measured["baseline"]
    by_name = {t["agent"]: t for t in tested}
    for row in report["agents"]:
        if row["agent"] in by_name:
            row["score_without"], row["needed"] = by_name[row["agent"]]["score_without"], bool(by_name[row["agent"]]["needed"])
    report["removable"] = [t["agent"] for t in tested if not t["needed"]]   # never when the baseline is 0: no evidence
    return report


def necessity_all(symbols: SymbolTable, graph: IRGraph, profile=None) -> list:
    """One report per necessity block (an empty list when the program has none)."""
    reports = []
    if not symbols.necessities:
        return reports
    rows = classify(graph, symbols)
    measured = {r.get("name"): r for r in profile or []}
    for info in symbols.necessities.values():
        report = report_from_profile(info, rows, graph, measured[info.name]) if info.name in measured else None
        reports.append(report or base_report(info, rows, graph))
    return reports

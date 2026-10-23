"""Can any candidate of a `compare` block replace the agent that is in the flow?

A compare block lists agents that do the same job (for example two versions of `responder` with
different prompts). One of them is wired into the flow; the others wait outside it. After a test run
the winner may not be the one in the flow, and we want to swap it in without losing any guarantee the
compiler gave for the original workflow.

The check is simple and uses the analyses we already have: for every other candidate, build a copy of
the graph where that candidate takes the place of the wired one, run the whole verification again, and
report every problem that is NEW compared with the original graph.

  AF-E216  the swap would break the workflow (for example the candidate reads a field that
           nothing has written yet on some path: a crash after paid LLM calls)
  AF-W310  the swap would introduce a warning (for example a new prompt-injection path)
  AF-W311  none of the candidates is in the flow, so there is nothing to compare against
  AF-E217  an `end_to_end` or `ensure` block needs exactly one candidate in the flow

Only blocks with exactly one wired candidate are checked: with two wired candidates it is not clear
which one a winner should replace.

The same findings decide whether the optimizer may select a candidate automatically
(optimize.select_agents): a candidate that would add a problem is never selected.
"""

import re
from dataclasses import replace

from .diagnostics import error, warning
from .ir import IRGraph
from .semantic import SymbolTable
from .verify import reachable_from, verify


def wired_candidates(graph: IRGraph, candidates: list) -> list:
    reachable = reachable_from(graph, "start")
    return [c for c in candidates if c in reachable]


def swap_candidate(graph: IRGraph, wired: str, other: str) -> IRGraph:
    """A copy of the graph in which `other` stands where `wired` stood (and takes over its edges)."""
    swapped = IRGraph()
    for node_id, node in graph.nodes.items():
        if node_id == other:
            continue                                   # the candidate that waits outside the flow
        if node_id == wired:
            donor = graph.nodes[other]
            swapped.nodes[other] = replace(donor, status="ok")
        else:
            swapped.nodes[node_id] = node
    for e in graph.edges:
        swapped.edges.append(replace(e, source=other if e.source == wired else e.source,
                                     target=other if e.target == wired else e.target))
    return swapped


def rename(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


def key_of(diag, wired: str, other: str):
    """A diagnostic described in terms of the wired agent, so the same problem has the same key
    before and after the swap."""
    return diag.code, rename(diag.message, other, wired)


def swap_findings(graph: IRGraph, symbols: SymbolTable):
    """For every compare block with exactly one wired candidate, verify the workflow with each other
    candidate in its place. Returns (info, wired, other, diagnostic) for every NEW problem."""
    findings = []
    baseline = None
    for info in symbols.compares.values():
        wired = wired_candidates(graph, info.candidates)
        if len(wired) != 1:
            continue
        current = wired[0]
        if baseline is None:
            baseline = verify(graph, symbols)[1]
        known = {key_of(d, current, current) for d in baseline}
        for other in info.candidates:
            if other == current:
                continue
            _, found = verify(swap_candidate(graph, current, other), symbols)
            findings += [(info, current, other, d) for d in found if key_of(d, current, other) not in known]
    return findings


def check_candidates(graph: IRGraph, symbols: SymbolTable):
    """Returns diagnostics for every compare block."""
    diagnostics = []
    for info in symbols.compares.values():
        if len(info.candidates) >= 2 and not wired_candidates(graph, info.candidates):
            diagnostics.append(warning(
                "AF-W311", "verify",
                f"none of the candidates of compare '{info.name}' ({', '.join(info.candidates)}) is used in the "
                f"flow, so there is nothing to replace", info.decl.line, info.decl.col))
    for info in symbols.compares.values():
        wired = wired_candidates(graph, info.candidates)
        if len(info.candidates) >= 2 and len(wired) != 1:
            for used, word in ((info.decl.end_to_end, "end_to_end"), (info.decl.ensure is not None, "ensure")):
                if used:
                    diagnostics.append(error(
                        "AF-E217", "verify",
                        f"compare '{info.name}' uses '{word}', which needs exactly one candidate in the flow "
                        f"(found {len(wired)})", info.decl.line, info.decl.col))
    for info, current, other, d in swap_findings(graph, symbols):
        reason = f"if '{other}' replaces '{current}' (compare '{info.name}'): {d.message}"
        make = error if d.is_error else warning
        code = "AF-E216" if d.is_error else "AF-W310"
        diagnostics.append(make(code, "verify", reason, info.decl.line, info.decl.col, path=d.path, node=current))
    return diagnostics


def unsafe_candidates(graph: IRGraph, symbols: SymbolTable) -> set:
    """(compare name, candidate) pairs whose swap would add a problem: they must not be selected automatically."""
    return {(info.name, other) for info, _, other, _ in swap_findings(graph, symbols)}

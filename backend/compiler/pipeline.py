"""Runs every compiler phase in order and builds the API response (docs/contract.md).

lexer -> parser -> semantic -> ir -> verify -> optimize -> cost -> codegen

If a phase reports an error, the following phases are marked "skipped",
but the output of the phases that did run is still returned so the UI can show it.
"""

import sys

from . import ast_nodes as ast
from .candidates import check_candidates, unsafe_candidates
from .codegen import generate
from .cost import analyze_cost
from .diagnostics import error, has_errors
from .evaluate import evaluate_all
from .ir import build_ir
from .lexer import tokenize
from .necessity import necessity_all
from .optimize import eliminate_dead_agents, eliminate_dead_llm_calls, prune_unneeded, select_agents
from .parser import parse
from .semantic import analyze
from .verify import verify

# Several phases are recursive (Tarjan's SCC algorithm recurses once per node of a chain, and long
# `a or b or c ...` chains make deep trees). Python's default limit of 1000 calls is too small for big
# workflows, so raise it. Deep *nesting* in the source is limited separately by the parser (AF-E103).
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20_000))

PHASES = ["lexer", "parser", "semantic", "ir", "verify", "optimize", "cost", "codegen"]

EMPTY_DATA = {
    "lexer": {"tokens": []},
    "parser": {"ast": None},
    "semantic": {"symbols": {"state": [], "tools": [], "agents": []}},
    "ir": {"nodes": [], "edges": []},
    "verify": {"checks": []},
    "optimize": {"removed_agents": [], "removed_stores": [], "selected": [], "pruned": []},
    "cost": {"worst": {"llm": None, "tool": None}, "best": {"llm": None, "tool": None}, "agents": [],
             "unbounded_loops": [], "removed_llm_calls": 0},
    "codegen": {"code": ""},
}


class CompileRun:
    """Collects each phase's output and all diagnostics for one compilation."""

    def __init__(self, profile=None, select=True):
        # scores measured with a real model: a list of compare reports (evaluate.py), or a dict
        # {"compare": [...], "necessity": [...]} (necessity.py); None = use the mock / static profile
        if isinstance(profile, dict):
            self.profile, self.necessity_profile = profile.get("compare"), profile.get("necessity")
        else:
            self.profile, self.necessity_profile = profile, None
        self.select = select     # apply what was measured: wire the best compare candidate, prune unneeded agents
        self.phases = {}
        self.diagnostics = []
        self.current = "lexer"   # the phase that is running now (for error reporting)
        self.evaluation = []     # one report per `compare` block (see evaluate.py)
        self.necessity = []      # one report per `necessity` block (see necessity.py)

    def record(self, name, data, diagnostics=()) -> bool:
        """Store one phase's result. Returns True if the pipeline may continue."""
        self.current = PHASES[min(PHASES.index(name) + 1, len(PHASES) - 1)]
        self.diagnostics.extend(diagnostics)
        failed = has_errors(diagnostics)
        self.phases[name] = {"status": "error" if failed else "ok", **data}
        return not failed

    def result(self) -> dict:
        for name in PHASES:
            self.phases.setdefault(name, {"status": "skipped", **EMPTY_DATA[name]})
        self.diagnostics.sort(key=lambda d: (d.line, d.col))
        return {
            "ok": not has_errors(self.diagnostics),
            "phases": {name: self.phases[name] for name in PHASES},
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "evaluation": self.evaluation,
            "necessity": self.necessity,
        }

    def too_deep(self) -> dict:
        """The phases are recursive, so a program nested hundreds of levels deep would overflow
        Python's call stack. Report it as an ordinary error instead of crashing."""
        name = self.current
        self.diagnostics.append(error("AF-E103", name, "the program is nested too deeply to compile; "
                                      "simplify very deeply nested expressions or if blocks", 1, 1))
        self.phases[name] = {"status": "error", **EMPTY_DATA[name]}
        return self.result()


def compile_source(source: str, source_name: str = "workflow.af", profile=None, select: bool = True) -> dict:
    run = CompileRun(profile, select)
    try:
        return compile_front_half(source, run, source_name)
    except RecursionError:
        return run.too_deep()


def compile_front_half(source: str, run: CompileRun, source_name: str) -> dict:
    # 1. lexical analysis
    tokens, diags = tokenize(source)
    if not run.record("lexer", {"tokens": [t.to_dict() for t in tokens]}, diags):
        return run.result()

    # 2. parsing
    program, diags = parse(tokens)
    if not run.record("parser", {"ast": program.to_dict()}, diags):
        return run.result()

    return compile_back_half(program, run, source_name)


def compile_program(program: ast.Program, source_name: str = "workflow.af", profile=None, select: bool = True) -> dict:
    """Run the pipeline from an already built AST (lets the back half be tested from AST fixtures)."""
    run = CompileRun(profile, select)
    run.phases["lexer"] = {"status": "skipped", "tokens": []}
    run.record("parser", {"ast": program.to_dict()})
    try:
        return compile_back_half(program, run, source_name)
    except RecursionError:
        return run.too_deep()


def compile_back_half(program: ast.Program, run: CompileRun, source_name: str) -> dict:
    # 3. semantic analysis
    symbols, diags = analyze(program)
    if not run.record("semantic", {"symbols": symbols.to_dict()}, diags):
        return run.result()

    # 4. lowering to the graph IR
    graph = build_ir(symbols)

    # 5. verification (the IR is recorded after it, so node colors include the results)
    run.current = "verify"
    checks, diags = verify(graph, symbols)
    diags += check_candidates(graph, symbols)   # could each compare candidate replace the wired agent?
    mark_node_status(graph, diags)
    run.record("ir", graph.to_dict())
    if not run.record("verify", {"checks": checks}, diags):
        return run.result()

    # 6. optimization: wire the best candidate of each compare block, remove agents that can never run,
    #    then LLM calls whose result is never used
    run.evaluation = evaluate_all(symbols, graph, run.profile)
    selected = []
    optimized = graph
    if run.select:
        optimized, selected = select_agents(graph, run.evaluation, unsafe_candidates(graph, symbols))
    pruned = []
    run.necessity = necessity_all(symbols, optimized, run.necessity_profile)   # which agents can be skipped?
    if run.select:
        optimized, pruned = prune_unneeded(optimized, symbols, run.necessity)
    optimized, removed_agents = eliminate_dead_agents(optimized)
    removed_agents = [a for a in removed_agents if a not in symbols.candidate_names()]   # candidates wait on purpose
    optimized, removed_stores, diags = eliminate_dead_llm_calls(optimized, symbols)
    run.record("optimize", {"removed_agents": removed_agents,
                            "removed_stores": [r.to_dict() for r in removed_stores],
                            "selected": selected, "pruned": pruned}, diags)

    # 7. static cost analysis (worst / best case number of LLM and tool calls per run)
    report, cost_diags = analyze_cost(optimized, removed_stores)
    run.record("cost", report, cost_diags)

    # show the optimizer and cost findings on the graph too (yellow nodes, removed call counts)
    for r in removed_stores:
        graph.nodes[r.agent].removed_llm_calls += r.llm_calls
    mark_node_status(graph, diags + cost_diags)
    run.phases["ir"] = {"status": "ok", **graph.to_dict()}

    # 8. code generation
    run.record("codegen", {"code": generate(optimized, symbols, source_name)})
    return run.result()


def mark_node_status(graph, diagnostics):
    """Color IR nodes: red if an error mentions them, yellow if only a warning does."""
    for d in diagnostics:
        node = graph.nodes.get(d.node)
        if node is None:
            continue
        if d.is_error:
            node.status = "error"
        elif node.status == "ok":
            node.status = "warning"

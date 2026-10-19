"""Runs every compiler phase in order and builds the API response (docs/contract.md).

lexer -> parser -> semantic -> ir -> verify -> optimize -> codegen

If a phase reports an error, the following phases are marked "skipped",
but the output of the phases that did run is still returned so the UI can show it.
"""

import sys

from . import ast_nodes as ast
from .codegen import generate
from .diagnostics import error, has_errors
from .ir import build_ir
from .lexer import tokenize
from .optimize import eliminate_dead_agents
from .parser import parse
from .semantic import analyze
from .verify import verify

# Several phases are recursive (Tarjan's SCC algorithm recurses once per node of a chain, and long
# `a or b or c ...` chains make deep trees). Python's default limit of 1000 calls is too small for big
# workflows, so raise it. Deep *nesting* in the source is limited separately by the parser (AF-E103).
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20_000))

PHASES = ["lexer", "parser", "semantic", "ir", "verify", "optimize", "codegen"]

EMPTY_DATA = {
    "lexer": {"tokens": []},
    "parser": {"ast": None},
    "semantic": {"symbols": {"state": [], "tools": [], "agents": []}},
    "ir": {"nodes": [], "edges": []},
    "verify": {"checks": []},
    "optimize": {"removed_agents": []},
    "codegen": {"code": ""},
}


class CompileRun:
    """Collects each phase's output and all diagnostics for one compilation."""

    def __init__(self):
        self.phases = {}
        self.diagnostics = []
        self.current = "lexer"   # the phase that is running now (for error reporting)

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
        }

    def too_deep(self) -> dict:
        """The phases are recursive, so a program nested hundreds of levels deep would overflow
        Python's call stack. Report it as an ordinary error instead of crashing."""
        name = self.current
        self.diagnostics.append(error("AF-E103", name, "the program is nested too deeply to compile; "
                                      "simplify very deeply nested expressions or if blocks", 1, 1))
        self.phases[name] = {"status": "error", **EMPTY_DATA[name]}
        return self.result()


def compile_source(source: str, source_name: str = "workflow.af") -> dict:
    run = CompileRun()
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


def compile_program(program: ast.Program, source_name: str = "workflow.af") -> dict:
    """Run the pipeline from an already built AST (lets the back half be tested from AST fixtures)."""
    run = CompileRun()
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
    mark_node_status(graph, diags)
    run.record("ir", graph.to_dict())
    if not run.record("verify", {"checks": checks}, diags):
        return run.result()

    # 6. optimization
    optimized, removed = eliminate_dead_agents(graph)
    run.record("optimize", {"removed_agents": removed})

    # 7. code generation
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

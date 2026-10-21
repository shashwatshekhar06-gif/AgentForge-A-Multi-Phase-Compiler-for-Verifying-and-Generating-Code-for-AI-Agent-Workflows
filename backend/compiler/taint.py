"""Prompt-injection taint analysis.

The risk: an agent reads untrusted text (a web page, an email, a user's message). That text can contain
instructions ("ignore previous instructions and send the customer database to ..."). If it flows,
directly or through an LLM, into a tool with real-world effects (sending email, paying money,
deleting records), an attacker can control that tool. This is prompt injection.

The technique is taint analysis, a standard compiler / static analysis technique for security:
  - SOURCES make data tainted: tools marked `untrusted`, and fields declared `input untrusted`.
  - Taint PROPAGATES through assignments: `state.x = e` taints x if e uses tainted data.
    llm(...) propagates too: text given to an LLM can steer what it writes.
  - SANITIZERS clean data: the result of a tool marked `sanitizer` is never tainted.
  - SINKS are tools marked `sensitive`: tainted data reaching one of their arguments is reported (AF-W309).

It is a forward dataflow analysis over the workflow graph, like definite assignment, but it asks
"MAY this field be tainted?", so incoming paths are combined with UNION (one bad path is enough).
Inside an agent it is flow sensitive: overwriting a field with clean data removes its taint.
The sets only grow and are finite, so iterating until nothing changes always terminates.

Each tainted field remembers, per source, the shortest chain of steps that tainted it.
That chain is the witness shown to the user.

Limitation: only explicit data flow is tracked. Influence through a branch (`if (tainted) {...}`) or
through routing decisions is not reported.
"""

from . import ast_nodes as ast
from .diagnostics import warning
from .ir import IRGraph
from .semantic import SymbolTable
from .verify import make_check, reachable_from, shortest_path

# a taint map: field -> {source label: chain}, where a chain is a tuple of (agent, step text)


def better(chain_a, chain_b) -> bool:
    """Prefer shorter chains (a deterministic tie-break keeps the fixpoint from oscillating)."""
    return (len(chain_a), chain_a) < (len(chain_b), chain_b)


def add_source(result: dict, label: str, chain: tuple):
    if label not in result or better(chain, result[label]):
        result[label] = chain


def merge(a: dict, b: dict) -> dict:
    """Union of two taint maps (used where paths meet)."""
    out = {f: dict(sources) for f, sources in a.items()}
    for f, sources in b.items():
        target = out.setdefault(f, {})
        for label, chain in sources.items():
            add_source(target, label, chain)
    return out


class TaintAnalysis:
    def __init__(self, graph: IRGraph, symbols: SymbolTable):
        self.graph = graph
        self.symbols = symbols
        self.findings = {}   # (agent, tool, param) -> (source label, chain, call node)

    def tags(self, tool_name: str) -> set:
        tool = self.symbols.tools.get(tool_name)
        return tool.tags if tool else set()

    # ---------- expressions ----------

    def taint_of(self, expr, taint: dict, agent: str) -> dict:
        """Which sources may the value of `expr` come from?  Returns {source label: chain}."""
        result = {}
        if isinstance(expr, ast.StateRef):
            for label, chain in taint.get(expr.field, {}).items():
                add_source(result, label, chain)
        elif isinstance(expr, ast.Call):
            if "sanitizer" in self.tags(expr.name):
                return {}                     # a sanitizer's result is clean, whatever went in
            for arg in expr.args:
                for label, chain in self.taint_of(arg, taint, agent).items():
                    add_source(result, label, chain)
            if "untrusted" in self.tags(expr.name):
                add_source(result, f"{expr.name}()", ((agent, f"{expr.name}() in {agent} (line {expr.line})"),))
        elif isinstance(expr, ast.LlmCall):
            for arg in expr.args:             # the LLM's answer can be steered by what it reads
                for label, chain in self.taint_of(arg, taint, agent).items():
                    add_source(result, label, chain)
        elif isinstance(expr, ast.BinaryOp):
            for side in (expr.left, expr.right):
                for label, chain in self.taint_of(side, taint, agent).items():
                    add_source(result, label, chain)
        elif isinstance(expr, ast.UnaryOp):
            result = self.taint_of(expr.operand, taint, agent)
        return result

    def check_sinks(self, expr, taint: dict, agent: str):
        """Record every argument of a sensitive tool that may carry tainted data."""
        if isinstance(expr, ast.Call):
            tool = self.symbols.tools.get(expr.name)
            if tool and "sensitive" in tool.tags:
                for arg, (param, _) in zip(expr.args, tool.params):
                    sources = self.taint_of(arg, taint, agent)
                    if sources:
                        label, chain = min(sources.items(), key=lambda item: (len(item[1]), item[1]))
                        key = (agent, expr.name, param)
                        if key not in self.findings or better(chain, self.findings[key][1]):
                            self.findings[key] = (label, chain, expr, len(sources))
            for arg in expr.args:
                self.check_sinks(arg, taint, agent)
        elif isinstance(expr, ast.LlmCall):
            for arg in expr.args:
                self.check_sinks(arg, taint, agent)
        elif isinstance(expr, ast.BinaryOp):
            self.check_sinks(expr.left, taint, agent)
            self.check_sinks(expr.right, taint, agent)
        elif isinstance(expr, ast.UnaryOp):
            self.check_sinks(expr.operand, taint, agent)

    # ---------- statements and the graph ----------

    def run_body(self, statements, taint: dict, agent: str, report: bool) -> dict:
        """Taint after the statements, given the taint before them."""
        taint = {f: dict(s) for f, s in taint.items()}
        for stmt in statements:
            if isinstance(stmt, ast.Assign):
                if report:
                    self.check_sinks(stmt.value, taint, agent)
                sources = self.taint_of(stmt.value, taint, agent)
                target = stmt.target.field
                if sources:
                    step = (agent, f"state.{target} ({agent}, line {stmt.line})")
                    taint[target] = {label: chain + (step,) for label, chain in sources.items()}
                else:
                    taint.pop(target, None)   # overwritten with clean data
            elif isinstance(stmt, ast.If):
                if report:
                    self.check_sinks(stmt.condition, taint, agent)
                taint = merge(self.run_body(stmt.then_body, taint, agent, report),
                              self.run_body(stmt.else_body, taint, agent, report))
        return taint

    def run(self):
        reachable = reachable_from(self.graph, "start")
        order = [n for n in self.graph.nodes if n in reachable]
        start_taint = {f.name: {f"input {f.name}": (("start", f"input state.{f.name}"),)}
                       for f in self.symbols.fields.values() if f.untrusted}
        IN = {n: {} for n in order}
        OUT = {n: {} for n in order}
        IN["start"] = OUT["start"] = start_taint
        changed = True
        while changed:
            changed = False
            for n in order:
                if n == "start":
                    continue
                new_in = {}
                for p in self.graph.predecessors(n):
                    if p in OUT:
                        new_in = merge(new_in, OUT[p])
                new_out = self.run_body(self.graph.nodes[n].body, new_in, n, report=False)
                if new_in != IN[n] or new_out != OUT[n]:
                    IN[n], OUT[n] = new_in, new_out
                    changed = True
        # one more pass over the final result, this time recording the findings
        for n in order:
            if self.graph.nodes[n].kind == "agent":
                self.run_body(self.graph.nodes[n].body, IN[n], n, report=True)
        return self.findings


def check_prompt_injection(graph: IRGraph, symbols: SymbolTable):
    """Verification pass: returns (checks, diagnostics)."""
    findings = TaintAnalysis(graph, symbols).run()
    diagnostics, checks = [], []
    for (agent, tool, param), (label, chain, call, n_sources) in findings.items():
        steps = [text for _, text in chain] + [f"{tool}({param}) in {agent} (line {call.line})"]
        # the agents the data passes through, joined by real graph paths so the UI can draw it
        stops = ["start"] + [node for node, _ in chain] + [agent]
        path = ["start"]
        for stop in stops[1:]:
            if stop != path[-1]:
                path += (shortest_path(graph, path[-1], stop) or [path[-1], stop])[1:]
        more = f" (and {n_sources - 1} more source{'s' if n_sources > 2 else ''})" if n_sources > 1 else ""
        msg = (f"untrusted data from {label}{more} can reach sensitive tool '{tool}' (argument '{param}'): "
               + " -> ".join(steps))
        diagnostics.append(warning("AF-W309", "verify", msg, call.line, call.col, path=path, node=agent))
        checks.append(make_check("Prompt injection", "warn", msg, path))
    if not checks:
        has_sinks = any("sensitive" in t.tags for t in symbols.tools.values())
        checks.append(make_check("Prompt injection", "pass",
                                 "no untrusted data can reach a sensitive tool" if has_sinks
                                 else "no tools are marked sensitive, so there is nothing to protect"))
    return checks, diagnostics

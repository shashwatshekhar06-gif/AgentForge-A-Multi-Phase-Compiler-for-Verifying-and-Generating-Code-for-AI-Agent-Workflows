"""Code generation: optimized IR -> one runnable LangGraph Python file.

The generated file contains, in order:
  1. a TypedDict state class (user fields + hidden routing/loop fields),
  2. an `llm` helper and one stub function per tool (mock mode, no API keys needed),
  3. one node function per agent (its statements translated to Python),
  4. one routing function per branching node,
  5. the StateGraph wiring (add_node, add_edge, add_conditional_edges) and graph.compile(),
  6. a __main__ block that runs the workflow once with sample inputs.

How routing works: a node with conditional edges computes its next step at the end
of its own function and stores it in a hidden field `_next_<node>`. The graph then
follows that field. Loop counters for max_iterations live in hidden `_count_<i>` fields.
"""

import json
import keyword

from . import ast_nodes as ast
from .ir import IRGraph
from .semantic import SymbolTable

PY_TYPES = {"string": "str", "int": "int", "bool": "bool"}
PRECEDENCE = {"or": 1, "and": 2, "not": 3}   # comparisons are 4 (bind tightest)


def py_type(t: str) -> str:
    if t.startswith("list["):
        return f"list[{py_type(t[5:-1])}]"
    return PY_TYPES[t]


def empty_value(t: str) -> str:
    """Python source for an 'empty' value of a type."""
    if t.startswith("list["):
        return "[]"
    return {"string": '""', "int": "0", "bool": "False"}[t]


def sample_value(t: str, label: str) -> str:
    """Python source for a plausible placeholder value (used by mocks and sample inputs)."""
    if t.startswith("list["):
        inner = t[5:-1]
        return f"[{sample_value(inner, label + ' 1')}, {sample_value(inner, label + ' 2')}]"
    return {"string": py_str(f"[{label}]"), "int": "1", "bool": "True"}[t]


def py_str(text: str) -> str:
    """A double quoted Python string literal (JSON string syntax is valid Python)."""
    return json.dumps(text)


def safe_name(name: str) -> str:
    """A parameter called `from` or `class` is fine in .af but not in Python: add a trailing _."""
    return name + "_" if keyword.iskeyword(name) else name


def literal_py(lit: ast.Literal) -> str:
    return py_str(lit.value) if lit.value_type == "string" else repr(lit.value)


class CodeGenerator:
    def __init__(self, graph: IRGraph, symbols: SymbolTable, source_name: str = "workflow.af"):
        self.graph = graph
        self.symbols = symbols
        self.source_name = source_name
        self.lines: list[str] = []
        # give every bounded edge its own loop counter
        self.counters = {id(e): f"_count_{i}" for i, e in enumerate(
            e for e in graph.edges if e.max_iterations is not None)}

    def emit(self, text: str = "", indent: int = 0):
        self.lines.append("    " * indent + text if text else "")

    # ---------- helpers about the graph ----------

    def branching_nodes(self) -> list:
        """Nodes whose next step depends on a condition."""
        return [n for n in self.graph.nodes
                if any(e.condition is not None for e in self.graph.out_edges(n))]

    @staticmethod
    def graph_name(node_id: str) -> str:
        return {"start": "START", "end": "END"}.get(node_id, py_str(node_id))

    # ---------- expressions ----------

    def expr(self, e) -> str:
        if isinstance(e, ast.Literal):
            return literal_py(e)
        if isinstance(e, ast.StateRef):
            return f's["{e.field}"]'
        if isinstance(e, ast.Call):
            return f"tool_{e.name}({', '.join(self.expr(a) for a in e.args)})"
        if isinstance(e, ast.LlmCall):
            parts = [py_str(e.prompt)] + [self.expr(a) for a in e.args]
            if e.one_of is not None:
                parts.append("one_of=[" + ", ".join(literal_py(o) for o in e.one_of) + "]")
            return f"llm({', '.join(parts)})"
        if isinstance(e, ast.UnaryOp):
            return f"not {self.wrap(e.operand, 'not')}"
        if isinstance(e, ast.BinaryOp):
            return f"{self.wrap(e.left, e.op)} {e.op} {self.wrap(e.right, e.op)}"
        raise ValueError(f"unknown expression {e!r}")

    def wrap(self, child, parent_op) -> str:
        """Parenthesize a child expression only when Python would group it differently."""
        text = self.expr(child)
        parent = PRECEDENCE.get(parent_op, 4)
        if isinstance(child, ast.BinaryOp):
            prec = PRECEDENCE.get(child.op, 4)
            if prec < parent or (prec == 4 and parent == 4):
                return f"({text})"
        if isinstance(child, ast.UnaryOp) and parent == 4:
            return f"({text})"
        return text

    # ---------- statements ----------

    def statements(self, body: list, indent: int):
        if not body:
            self.emit("pass", indent)
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                self.emit(f's["{stmt.target.field}"] = {self.expr(stmt.value)}', indent)
            elif isinstance(stmt, ast.If):
                self.emit(f"if {self.expr(stmt.condition)}:", indent)
                self.statements(stmt.then_body, indent + 1)
                if stmt.else_body:
                    self.emit("else:", indent)
                    self.statements(stmt.else_body, indent + 1)

    # ---------- file sections ----------

    def generate(self) -> str:
        self.header()
        self.state_class()
        self.llm_helper()
        self.tools()
        self.agents()
        self.routers()
        self.wiring()
        self.main_block()
        return "\n".join(self.lines) + "\n"

    def header(self):
        self.emit(f'"""Generated by AgentForge from {self.source_name}. Do not edit by hand; edit the .af file and recompile.')
        self.emit()
        self.emit("Run it with:  python workflow.py")
        self.emit("Tools and the LLM run in MOCK mode by default, so no API keys are needed.")
        self.emit("Set AGENTFORGE_CHOICE to choose which one_of option the mock LLM returns (default: the first).")
        self.emit('"""')
        self.emit()
        self.emit("import os")
        self.emit("from typing import TypedDict")
        self.emit()
        self.emit("from langgraph.graph import END, START, StateGraph")
        self.emit()
        self.emit('MOCK_CHOICE = os.environ.get("AGENTFORGE_CHOICE")')
        self.emit()

    def hidden_fields(self) -> list:
        """(name, type) of the extra fields used for routing and loop counters."""
        fields = [(f"_next_{n}", "string") for n in self.branching_nodes() if n != "start"]
        fields += [(name, "int") for name in self.counters.values()]
        return fields

    def state_class(self):
        # the functional TypedDict form works for any field name, even Python keywords like `class`
        self.emit()
        self.emit('AgentState = TypedDict("AgentState", {')
        for f in self.symbols.fields.values():
            note = f"  # {f.modifier}" if f.modifier else ""
            self.emit(f"{py_str(f.name)}: {py_type(f.type)},{note}", 1)
        hidden = self.hidden_fields()
        if hidden:
            self.emit("# hidden fields added by the compiler (routing decisions and loop counters)", 1)
            for name, t in hidden:
                self.emit(f"{py_str(name)}: {py_type(t)},", 1)
        self.emit("})")
        self.emit()

    def llm_helper(self):
        self.emit()
        self.emit("def llm(prompt, *context, one_of=None):")
        self.emit('"""Ask a language model. This mock returns placeholder text so the workflow runs offline."""', 1)
        self.emit("# >>> A REAL MODEL CALL GOES HERE, for example with the Anthropic SDK:", 1)
        self.emit("#   reply = client.messages.create(model=..., messages=[{\"role\": \"user\", \"content\": prompt + str(context)}])", 1)
        self.emit("#   and, for one_of, check that the reply is one of the allowed options.", 1)
        self.emit("if one_of:", 1)
        self.emit("for option in one_of:", 2)
        self.emit("if str(option) == MOCK_CHOICE:", 3)
        self.emit("return option", 4)
        self.emit("return one_of[0]", 2)
        self.emit('return f"[mock LLM answer to: {prompt}]"', 1)
        self.emit()

    def tools(self):
        for tool in self.symbols.tools.values():
            params = ", ".join(f"{safe_name(n)}: {py_type(t)}" for n, t in tool.params)
            self.emit()
            self.emit(f"def tool_{tool.name}({params}) -> {py_type(tool.return_type)}:")
            self.emit(f'"""Tool \'{tool.name}\' (mock). Replace this body with the real implementation."""', 1)
            self.emit(f"return {sample_value(tool.return_type, 'mock ' + tool.name + ' result')}", 1)
            self.emit()

    def agents(self):
        branching = self.branching_nodes()
        for node in self.graph.nodes.values():
            if node.kind != "agent":
                continue
            self.emit()
            self.emit(f"def node_{node.id}(state: AgentState) -> dict:")
            self.emit(f'"""Agent \'{node.id}\' (line {node.line} of {self.source_name})."""', 1)
            self.emit("s = dict(state)  # work on a copy, return the updated state", 1)
            self.statements(node.body, 1)
            if node.id in branching:
                self.emit(f's["_next_{node.id}"] = route_{node.id}(s)', 1)
            self.emit("return s", 1)
            self.emit()

    def routers(self):
        for node_id in self.branching_nodes():
            out = self.graph.out_edges(node_id)
            self.emit()
            self.emit(f"def route_{node_id}(s) -> str:")
            self.emit(f'"""Choose the next step after \'{node_id}\'. Conditions are tried in source order."""', 1)
            for e in out:
                if e.condition is None:
                    continue
                target = self.graph_name(e.target)
                cond = self.expr(e.condition)
                if e.max_iterations is not None:
                    counter = self.counters[id(e)]
                    self.emit(f"# loop edge: taken at most {e.max_iterations} times", 1)
                    self.emit(f'if ({cond}) and s["{counter}"] < {e.max_iterations}:', 1)
                    self.emit(f's["{counter}"] += 1', 2)
                else:
                    self.emit(f"if {cond}:", 1)
                self.emit(f"return {target}", 2)
            default = next((e for e in out if e.is_default), None)
            if default:
                self.emit(f"return {self.graph_name(default.target)}", 1)
            else:
                self.emit(f'raise RuntimeError("no edge condition matched after \'{node_id}\' and there is no else edge")', 1)
            self.emit()

    def wiring(self):
        branching = self.branching_nodes()
        self.emit()
        self.emit("# ---------- graph wiring ----------")
        self.emit("graph = StateGraph(AgentState)")
        for node in self.graph.nodes.values():
            if node.kind == "agent":
                self.emit(f'graph.add_node("{node.id}", node_{node.id})')
        for node_id in self.graph.nodes:
            out = self.graph.out_edges(node_id)
            if not out:
                continue
            if node_id in branching:
                targets = dict.fromkeys(e.target for e in out)
                mapping = ", ".join(f"{self.graph_name(t)}: {self.graph_name(t)}" for t in targets)
                if node_id == "start":
                    chooser = "lambda state: route_start(dict(state))"
                else:
                    chooser = f'lambda s: s["_next_{node_id}"]'
                self.emit(f"graph.add_conditional_edges({self.graph_name(node_id)}, {chooser}, {{{mapping}}})")
            else:
                # one plain edge (or a lone else edge)
                self.emit(f"graph.add_edge({self.graph_name(node_id)}, {self.graph_name(out[0].target)})")
        self.emit("app = graph.compile()")
        self.emit()

    def main_block(self):
        bounds = sum(e.max_iterations for e in self.graph.edges if e.max_iterations is not None)
        limit = 50 + 2 * len(self.graph.nodes) * (bounds + 1)
        self.emit()
        self.emit('if __name__ == "__main__":')
        self.emit("# every field starts with a value; the compiler already checked that agents only", 1)
        self.emit("# read fields that are written on every path before them", 1)
        self.emit("initial_state = {", 1)
        for f in self.symbols.fields.values():
            if f.modifier == "input":
                value, note = sample_value(f.type, "sample " + f.name), "  # input: change this"
            elif f.has_default:
                value, note = self.default_py(f.default), "  # default from the .af file"
            else:
                value, note = empty_value(f.type), ""
            self.emit(f'"{f.name}": {value},{note}', 2)
        for name, t in self.hidden_fields():
            self.emit(f'"{name}": {empty_value(t)},', 2)
        self.emit("}", 1)
        self.emit("# run the graph step by step so we can print the path it takes", 1)
        self.emit("final_state, visited = initial_state, []", 1)
        self.emit(f'for step in app.stream(initial_state, {{"recursion_limit": {limit}}}, stream_mode="updates"):', 1)
        self.emit("for node_name, update in step.items():", 2)
        self.emit("visited.append(node_name)", 3)
        self.emit("final_state = {**final_state, **(update or {})}  # update is None when a node changes nothing", 3)
        self.emit('print("path:", " -> ".join(["start"] + visited + ["end"]))', 1)
        outputs = [f.name for f in self.symbols.fields.values() if f.modifier == "output"]
        shown = outputs or [f.name for f in self.symbols.fields.values()]
        for name in shown:
            self.emit(f'print("{name}:", final_state["{name}"])', 1)

    @staticmethod
    def default_py(default) -> str:
        if isinstance(default, list):
            return "[" + ", ".join(literal_py(x) for x in default) + "]"
        return literal_py(default)


def generate(graph: IRGraph, symbols: SymbolTable, source_name="workflow.af") -> str:
    return CodeGenerator(graph, symbols, source_name).generate()

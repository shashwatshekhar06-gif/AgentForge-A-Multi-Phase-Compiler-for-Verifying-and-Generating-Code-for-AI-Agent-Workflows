"""Intermediate Representation: the workflow as a directed graph.

Lowering turns the AST + symbol table into a graph:
  - one node per agent, plus the special nodes `start` and `end`,
  - one edge per transition, carrying its condition and optional max_iterations.

Every later phase (verify, optimize, cost, codegen) works on this graph, not on the AST.
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as ast
from .semantic import SymbolTable


@dataclass
class Calls:
    """How many llm(...) and tool calls some code makes: at least (min) and at most (max)."""
    llm_min: int = 0
    llm_max: int = 0
    tool_min: int = 0
    tool_max: int = 0

    def __add__(self, other):   # code that runs one after the other
        return Calls(self.llm_min + other.llm_min, self.llm_max + other.llm_max,
                     self.tool_min + other.tool_min, self.tool_max + other.tool_max)

    def either(self, other):    # code where only one of two branches runs
        return Calls(min(self.llm_min, other.llm_min), max(self.llm_max, other.llm_max),
                     min(self.tool_min, other.tool_min), max(self.tool_max, other.tool_max))


def calls_in_expr(expr) -> Calls:
    if isinstance(expr, ast.LlmCall):
        inner = sum((calls_in_expr(a) for a in expr.args), Calls())
        return inner + Calls(1, 1, 0, 0)
    if isinstance(expr, ast.Call):
        inner = sum((calls_in_expr(a) for a in expr.args), Calls())
        return inner + Calls(0, 0, 1, 1)
    if isinstance(expr, ast.UnaryOp):
        return calls_in_expr(expr.operand)
    if isinstance(expr, ast.BinaryOp):
        left, right = calls_in_expr(expr.left), calls_in_expr(expr.right)
        if expr.op in ("and", "or"):   # short circuit: the right side may not run at all
            right = Calls(0, right.llm_max, 0, right.tool_max)
        return left + right
    return Calls()


def count_calls(statements) -> Calls:
    """Calls made by one run of an agent body (an if runs only one of its two branches)."""
    total = Calls()
    for stmt in statements:
        if isinstance(stmt, ast.Assign):
            total = total + calls_in_expr(stmt.value)
        elif isinstance(stmt, ast.If):
            total = total + calls_in_expr(stmt.condition) +                 count_calls(stmt.then_body).either(count_calls(stmt.else_body))
    return total


@dataclass
class IRNode:
    id: str
    kind: str                      # "start", "agent" or "end"
    reads: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    must_writes: list = field(default_factory=list)
    line: int = 0
    status: str = "ok"             # "ok", "warning" or "error" (filled in after verification)
    body: list = field(default_factory=list)   # agent statements (AST), used by codegen
    removed_llm_calls: int = 0     # llm calls the optimizer removed from this agent (shown in the UI)


@dataclass
class IREdge:
    source: str
    target: str
    condition: Optional[object] = None   # AST expression, or None
    max_iterations: Optional[int] = None
    is_default: bool = False
    line: int = 0
    col: int = 0


@dataclass
class IRGraph:
    nodes: dict = field(default_factory=dict)   # id -> IRNode, in insertion order
    edges: list = field(default_factory=list)

    def _index(self):
        """Edges grouped by source and by target. The analyses ask "what follows this node?" very often,
        so the grouping is built once and rebuilt only if the edge list changes."""
        key = (id(self.edges), len(self.edges))
        if getattr(self, "_index_key", None) != key:
            self._out, self._in = {}, {}
            for e in self.edges:
                self._out.setdefault(e.source, []).append(e)
                self._in.setdefault(e.target, []).append(e)
            self._index_key = key
        return self._out, self._in

    def successors(self, node_id) -> list:
        return [e.target for e in self._index()[0].get(node_id, [])]

    def predecessors(self, node_id) -> list:
        return [e.source for e in self._index()[1].get(node_id, [])]

    def out_edges(self, node_id) -> list:
        return list(self._index()[0].get(node_id, []))

    def to_dict(self) -> dict:
        return {"nodes": [self.node_dict(n) for n in self.nodes.values()],
            "edges": [{"from": e.source, "to": e.target,
                       "condition": ast.expr_to_source(e.condition) if e.condition is not None else None,
                       "max_iterations": e.max_iterations, "is_default": e.is_default}
                      for e in self.edges],
        }

    @staticmethod
    def node_dict(n: IRNode) -> dict:
        calls = count_calls(n.body)
        return {"id": n.id, "kind": n.kind, "reads": n.reads, "writes": n.writes, "must_writes": n.must_writes,
                "status": n.status, "line": n.line, "llm_calls": calls.llm_max, "tool_calls": calls.tool_max,
                "removed_llm_calls": n.removed_llm_calls}


def build_ir(symbols: SymbolTable) -> IRGraph:
    """Lower the checked program into the graph IR."""
    graph = IRGraph()
    flow_line = symbols.flow.line if symbols.flow else 0
    graph.nodes["start"] = IRNode("start", "start", line=flow_line)
    for agent in symbols.agents.values():
        graph.nodes[agent.name] = IRNode(agent.name, "agent", list(agent.reads), list(agent.writes),
                                         list(agent.must_writes), agent.decl.line, body=agent.decl.body)
    graph.nodes["end"] = IRNode("end", "end", line=flow_line)

    if symbols.flow:
        for e in symbols.flow.edges:
            graph.edges.append(IREdge(e.source, e.target, e.condition, e.max_iterations, e.is_default,
                                      e.line, e.col))
    return graph

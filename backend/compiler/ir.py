"""Intermediate Representation: the workflow as a directed graph.

Lowering turns the AST + symbol table into a graph:
  - one node per agent, plus the special nodes `start` and `end`,
  - one edge per transition, carrying its condition and optional max_iterations.

Every later phase (verify, optimize, codegen) works on this graph, not on the AST.
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as ast
from .semantic import SymbolTable


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

    def successors(self, node_id) -> list:
        return [e.target for e in self.edges if e.source == node_id]

    def predecessors(self, node_id) -> list:
        return [e.source for e in self.edges if e.target == node_id]

    def out_edges(self, node_id) -> list:
        return [e for e in self.edges if e.source == node_id]

    def to_dict(self) -> dict:
        return {
            "nodes": [{"id": n.id, "kind": n.kind, "reads": n.reads, "writes": n.writes,
                       "must_writes": n.must_writes, "status": n.status, "line": n.line}
                      for n in self.nodes.values()],
            "edges": [{"from": e.source, "to": e.target,
                       "condition": ast.expr_to_source(e.condition) if e.condition is not None else None,
                       "max_iterations": e.max_iterations, "is_default": e.is_default}
                      for e in self.edges],
        }


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

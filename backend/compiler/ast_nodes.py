"""AST node classes.

Each class is a plain dataclass. `to_dict()` turns a tree into the JSON shape
described in docs/contract.md, and `from_dict()` turns that JSON back into nodes
(used by tests that start from hand written AST fixtures).
"""

from dataclasses import dataclass, field, fields
from typing import Optional, Union


@dataclass
class Node:
    line: int
    col: int

    def to_dict(self) -> dict:
        result = {"type": type(self).__name__, "line": self.line, "col": self.col}
        for f in fields(self):
            if f.name not in ("line", "col"):
                result[f.name] = _to_json(getattr(self, f.name))
        return result


def _to_json(value):
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    return value


# ---------- declarations ----------

@dataclass
class FieldDecl(Node):
    name: str
    modifier: Optional[str]   # "input", "output" or None
    field_type: str           # "string", "int", "bool", "list[string]", ...
    default: Optional[Union["Literal", list]] = None   # Literal, list of Literal, or None


@dataclass
class StateDecl(Node):
    name: str
    fields: list[FieldDecl] = field(default_factory=list)


@dataclass
class Param(Node):
    name: str
    param_type: str


@dataclass
class ToolDecl(Node):
    name: str
    params: list[Param]
    return_type: str


@dataclass
class AgentDecl(Node):
    name: str
    body: list = field(default_factory=list)   # list of Assign / If


@dataclass
class Edge(Node):
    source: str
    target: str
    condition: Optional["Expr"] = None
    is_default: bool = False
    max_iterations: Optional[int] = None


@dataclass
class FlowDecl(Node):
    name: str
    edges: list[Edge] = field(default_factory=list)


@dataclass
class Program(Node):
    declarations: list = field(default_factory=list)


# ---------- statements ----------

@dataclass
class StateRef(Node):
    field: str


@dataclass
class Assign(Node):
    target: StateRef
    value: "Expr"


@dataclass
class If(Node):
    condition: "Expr"
    then_body: list = field(default_factory=list)
    else_body: list = field(default_factory=list)


# ---------- expressions ----------

@dataclass
class Literal(Node):
    value: Union[str, int, bool]
    value_type: str   # "string", "int" or "bool"


@dataclass
class Call(Node):
    name: str
    args: list = field(default_factory=list)


@dataclass
class LlmCall(Node):
    prompt: str
    args: list = field(default_factory=list)
    one_of: Optional[list[Literal]] = None


@dataclass
class BinaryOp(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class UnaryOp(Node):
    op: str
    operand: "Expr"


Expr = Union[Literal, StateRef, Call, LlmCall, BinaryOp, UnaryOp]

NODE_CLASSES = {cls.__name__: cls for cls in [
    Program, StateDecl, FieldDecl, ToolDecl, Param, AgentDecl, Assign, If,
    Literal, StateRef, Call, LlmCall, BinaryOp, UnaryOp, FlowDecl, Edge,
]}


def from_dict(data):
    """Rebuild AST nodes from contract JSON (the reverse of to_dict)."""
    if isinstance(data, list):
        return [from_dict(item) for item in data]
    if isinstance(data, dict) and "type" in data:
        cls = NODE_CLASSES[data["type"]]
        kwargs = {k: from_dict(v) for k, v in data.items() if k != "type"}
        return cls(**kwargs)
    return data


def expr_to_source(expr) -> str:
    """Print an expression back as DSL text, e.g. `state.route == "factual"`.

    Used for edge labels in the IR and in error messages.
    """
    if isinstance(expr, Literal):
        if expr.value_type == "string":
            return '"' + expr.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if expr.value_type == "bool":
            return "true" if expr.value else "false"
        return str(expr.value)
    if isinstance(expr, StateRef):
        return f"state.{expr.field}"
    if isinstance(expr, Call):
        return f"{expr.name}({', '.join(expr_to_source(a) for a in expr.args)})"
    if isinstance(expr, LlmCall):
        parts = [expr_to_source(Literal(0, 0, expr.prompt, "string"))]
        parts += [expr_to_source(a) for a in expr.args]
        text = f"llm({', '.join(parts)})"
        if expr.one_of is not None:
            text += " one_of [" + ", ".join(expr_to_source(o) for o in expr.one_of) + "]"
        return text
    if isinstance(expr, BinaryOp):
        return f"{_wrap(expr.left, expr.op)} {expr.op} {_wrap(expr.right, expr.op)}"
    if isinstance(expr, UnaryOp):
        return f"not {_wrap(expr.operand, 'not')}"
    return "?"


# lower number = binds less tightly
_PRECEDENCE = {"or": 1, "and": 2, "not": 3}


def _wrap(expr, parent_op) -> str:
    """Add parentheses around a child only when it binds less tightly than its parent."""
    text = expr_to_source(expr)
    parent = _PRECEDENCE.get(parent_op, 4)
    if isinstance(expr, BinaryOp):
        child = _PRECEDENCE.get(expr.op, 4)
        if child < parent or (child == 4 and parent == 4):
            return f"({text})"
    if isinstance(expr, UnaryOp) and parent == 4:
        return f"({text})"
    return text

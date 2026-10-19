"""Semantic analysis: symbol tables, type checking, reference validation.

Walks the AST and:
  1. builds symbol tables for state fields, tools and agents (reporting duplicates),
  2. type checks every expression, assignment, tool call and condition,
  3. checks every name used in the flow refers to a declared agent,
  4. infers each agent's read set and write sets from its body.
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as ast
from .diagnostics import Diagnostic, error, warning

SPECIAL_NODES = ("start", "end")


@dataclass
class FieldInfo:
    name: str
    type: str
    modifier: Optional[str]
    default: object   # AST Literal, list of Literal, or None
    line: int
    col: int

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass
class ToolInfo:
    name: str
    params: list   # list of (name, type)
    return_type: str


@dataclass
class AgentInfo:
    name: str
    decl: ast.AgentDecl
    reads: list = field(default_factory=list)        # read before the agent itself writes them
    writes: list = field(default_factory=list)       # may be written
    must_writes: list = field(default_factory=list)  # written on every path through the body


@dataclass
class SymbolTable:
    fields: dict = field(default_factory=dict)   # name -> FieldInfo
    tools: dict = field(default_factory=dict)    # name -> ToolInfo
    agents: dict = field(default_factory=dict)   # name -> AgentInfo
    flow: Optional[ast.FlowDecl] = None
    one_of_sets: dict = field(default_factory=dict)   # field -> allowed values (from llm one_of)

    def to_dict(self) -> dict:
        return {
            "state": [{"name": f.name, "type": f.type, "modifier": f.modifier} for f in self.fields.values()],
            "tools": [{"name": t.name, "params": [{"name": n, "type": ty} for n, ty in t.params],
                       "return_type": t.return_type} for t in self.tools.values()],
            "agents": [{"name": a.name, "reads": a.reads, "writes": a.writes, "must_writes": a.must_writes}
                       for a in self.agents.values()],
        }


def state_refs(expr):
    """Yield every StateRef inside an expression, left to right."""
    if isinstance(expr, ast.StateRef):
        yield expr
    elif isinstance(expr, (ast.Call, ast.LlmCall)):
        for arg in expr.args:
            yield from state_refs(arg)
    elif isinstance(expr, ast.BinaryOp):
        yield from state_refs(expr.left)
        yield from state_refs(expr.right)
    elif isinstance(expr, ast.UnaryOp):
        yield from state_refs(expr.operand)


def _add_unique(items: list, value):
    if value not in items:
        items.append(value)


class Analyzer:
    def __init__(self, program: ast.Program):
        self.program = program
        self.symbols = SymbolTable()
        self.diagnostics: list[Diagnostic] = []

    def err(self, code, message, node):
        self.diagnostics.append(error(code, "semantic", message, node.line, node.col))

    def warn(self, code, message, node):
        self.diagnostics.append(warning(code, "semantic", message, node.line, node.col))

    # ---------- driver ----------

    def analyze(self):
        self.collect_declarations()
        for agent in self.symbols.agents.values():
            self.check_agent(agent)
        self.collect_one_of_sets()
        if self.symbols.flow:
            self.check_flow(self.symbols.flow)
        return self.symbols, self.diagnostics

    # ---------- pass 1: symbol tables ----------

    def collect_declarations(self):
        state_seen = False
        for decl in self.program.declarations:
            if isinstance(decl, ast.StateDecl):
                if state_seen:
                    self.err("AF-E205", f"duplicate state declaration '{decl.name}' (only one is allowed)", decl)
                    continue
                state_seen = True
                for f in decl.fields:
                    self.declare_field(f)
            elif isinstance(decl, ast.ToolDecl):
                self.declare_tool(decl)
            elif isinstance(decl, ast.AgentDecl):
                self.check_not_reserved("agent", decl)
                if decl.name in self.symbols.agents:
                    self.err("AF-E205", f"duplicate agent '{decl.name}'", decl)
                else:
                    self.symbols.agents[decl.name] = AgentInfo(decl.name, decl)
            elif isinstance(decl, ast.FlowDecl):
                if self.symbols.flow:
                    self.err("AF-E205", f"duplicate flow declaration '{decl.name}' (only one is allowed)", decl)
                else:
                    self.symbols.flow = decl

        top = self.program
        if not state_seen:
            self.err("AF-E209", "missing state declaration (exactly one 'state' block is required)", top)
        if not self.symbols.agents:
            self.err("AF-E209", "missing agent declaration (at least one 'agent' is required)", top)
        if not self.symbols.flow:
            self.err("AF-E209", "missing flow declaration (exactly one 'flow' block is required)", top)

    def check_not_reserved(self, kind, node):
        """Names starting with '_' are reserved: the generated code uses them for hidden fields
        (_next_x, _count_0) and LangGraph uses __start__ / __end__."""
        if node.name.startswith("_"):
            self.err("AF-E213", f"{kind} name '{node.name}' starts with '_', which is reserved for the compiler", node)

    def declare_field(self, f: ast.FieldDecl):
        self.check_not_reserved("field", f)
        if f.name in self.symbols.fields:
            self.err("AF-E205", f"duplicate state field '{f.name}'", f)
            return
        if f.default is not None:
            self.check_default(f)
        self.symbols.fields[f.name] = FieldInfo(f.name, f.field_type, f.modifier, f.default,
                                                f.line, f.col)

    def check_default(self, f: ast.FieldDecl):
        if isinstance(f.default, list):
            if not f.field_type.startswith("list["):
                self.err("AF-E203", f"default for '{f.name}' is a list but the field type is {f.field_type}", f)
                return
            item_type = f.field_type[5:-1]
            for item in f.default:
                if item.value_type != item_type:
                    self.err("AF-E203", f"list default item {ast.expr_to_source(item)} is {item.value_type}, "
                                        f"expected {item_type}", item)
        elif f.default.value_type != f.field_type:
            self.err("AF-E203", f"default for '{f.name}' is {f.default.value_type}, expected {f.field_type}",
                     f.default)

    def declare_tool(self, decl: ast.ToolDecl):
        if decl.name in self.symbols.tools:
            self.err("AF-E205", f"duplicate tool '{decl.name}'", decl)
            return
        params, seen = [], set()
        for p in decl.params:
            if p.name in seen:
                self.err("AF-E205", f"duplicate parameter '{p.name}' in tool '{decl.name}'", p)
            seen.add(p.name)
            params.append((p.name, p.param_type))
        self.symbols.tools[decl.name] = ToolInfo(decl.name, params, decl.return_type)

    # ---------- pass 2: agents (type check + read/write inference) ----------

    def check_agent(self, agent: AgentInfo):
        assigned = self.check_statements(agent.decl.body, set(), agent)
        agent.must_writes = [w for w in agent.writes if w in assigned]

    def check_statements(self, statements, assigned: set, agent: AgentInfo) -> set:
        """Type check statements and track reads/writes.

        `assigned` = fields this agent has definitely written so far.
        Returns the set of fields definitely written after these statements.
        """
        assigned = set(assigned)
        for stmt in statements:
            if isinstance(stmt, ast.Assign):
                self.record_reads(stmt.value, assigned, agent)
                value_type = self.type_of(stmt.value)
                field_info = self.lookup_field(stmt.target)
                if field_info and value_type and value_type != field_info.type:
                    self.err("AF-E203", f"cannot assign {value_type} to state.{field_info.name} "
                                        f"(declared as {field_info.type})", stmt.value)
                if field_info:
                    _add_unique(agent.writes, field_info.name)
                    assigned.add(field_info.name)
            elif isinstance(stmt, ast.If):
                self.record_reads(stmt.condition, assigned, agent)
                self.check_condition(stmt.condition)
                then_assigned = self.check_statements(stmt.then_body, assigned, agent)
                else_assigned = self.check_statements(stmt.else_body, assigned, agent)
                # only fields written in BOTH branches are definitely written afterwards
                assigned = then_assigned & else_assigned
        return assigned

    def record_reads(self, expr, assigned: set, agent: AgentInfo):
        for ref in state_refs(expr):
            if ref.field in self.symbols.fields and ref.field not in assigned:
                _add_unique(agent.reads, ref.field)

    def lookup_field(self, ref: ast.StateRef) -> Optional[FieldInfo]:
        info = self.symbols.fields.get(ref.field)
        if info is None:
            self.err("AF-E202", f"undeclared state field 'state.{ref.field}'", ref)
        return info

    def check_condition(self, expr):
        t = self.type_of(expr)
        if t and t != "bool":
            self.err("AF-E207", f"condition must be bool, but {ast.expr_to_source(expr)} is {t}", expr)

    # ---------- expression types ----------

    def type_of(self, expr) -> Optional[str]:
        """Return the type of an expression, or None if it is unknown because of an earlier error
        (returning None avoids reporting the same mistake many times)."""
        if isinstance(expr, ast.Literal):
            return expr.value_type
        if isinstance(expr, ast.StateRef):
            info = self.lookup_field(expr)
            return info.type if info else None
        if isinstance(expr, ast.Call):
            return self.type_of_call(expr)
        if isinstance(expr, ast.LlmCall):
            return self.type_of_llm(expr)
        if isinstance(expr, ast.UnaryOp):
            self.expect_bool(expr.operand, "not")
            return "bool"
        if isinstance(expr, ast.BinaryOp):
            return self.type_of_binary(expr)
        return None

    def type_of_call(self, call: ast.Call) -> Optional[str]:
        arg_types = [self.type_of(a) for a in call.args]
        tool = self.symbols.tools.get(call.name)
        if tool is None:
            self.err("AF-E201", f"undeclared tool '{call.name}'", call)
            return None
        if len(call.args) != len(tool.params):
            self.err("AF-E206", f"tool '{call.name}' takes {len(tool.params)} argument(s) "
                                f"but {len(call.args)} were given", call)
        else:
            for arg, arg_type, (pname, ptype) in zip(call.args, arg_types, tool.params):
                if arg_type and arg_type != ptype:
                    self.err("AF-E206", f"argument '{pname}' of tool '{call.name}' must be {ptype}, "
                                        f"got {arg_type}", arg)
        return tool.return_type

    def type_of_llm(self, call: ast.LlmCall) -> Optional[str]:
        for arg in call.args:
            self.type_of(arg)   # check arguments; llm accepts any type
        if not call.one_of:
            return "string"
        first = call.one_of[0].value_type
        for option in call.one_of[1:]:
            if option.value_type != first:
                self.err("AF-E203", f"one_of options must all have the same type "
                                    f"({ast.expr_to_source(option)} is {option.value_type}, expected {first})", option)
        return first

    def type_of_binary(self, expr: ast.BinaryOp) -> Optional[str]:
        if expr.op in ("and", "or"):
            self.expect_bool(expr.left, expr.op)
            self.expect_bool(expr.right, expr.op)
            return "bool"
        left, right = self.type_of(expr.left), self.type_of(expr.right)
        if left is None or right is None:
            return "bool"
        if expr.op in ("<", ">", "<=", ">="):
            if left != "int" or right != "int":
                self.err("AF-E203", f"'{expr.op}' needs two ints, got {left} and {right}", expr)
        elif left != right:
            self.err("AF-E203", f"cannot compare {left} with {right} using '{expr.op}'", expr)
        return "bool"

    def expect_bool(self, expr, op):
        t = self.type_of(expr)
        if t and t != "bool":
            self.err("AF-E207", f"operand of '{op}' must be bool, but {ast.expr_to_source(expr)} is {t}", expr)

    # ---------- one_of sets (for AF-W208) ----------

    def collect_one_of_sets(self):
        """A field gets a one_of set only if EVERY assignment to it is llm(...) one_of [...]."""
        sets, disqualified = {}, set()

        def visit(statements):
            for stmt in statements:
                if isinstance(stmt, ast.Assign):
                    name, value = stmt.target.field, stmt.value
                    if isinstance(value, ast.LlmCall) and value.one_of:
                        sets.setdefault(name, set()).update(o.value for o in value.one_of)
                    else:
                        disqualified.add(name)
                elif isinstance(stmt, ast.If):
                    visit(stmt.then_body)
                    visit(stmt.else_body)

        for agent in self.symbols.agents.values():
            visit(agent.decl.body)
        self.symbols.one_of_sets = {k: v for k, v in sets.items() if k not in disqualified}

    # ---------- pass 3: flow ----------

    def check_flow(self, flow: ast.FlowDecl):
        self.reported_unknown = set()   # report each unknown agent name only once
        for edge in flow.edges:
            self.check_edge_names(edge)
            if edge.condition is not None:
                self.check_edge_condition(edge.condition)
            if edge.max_iterations is not None and edge.max_iterations < 1:
                self.err("AF-E203", "max_iterations must be at least 1", edge)
        self.check_edge_structure(flow)

    def check_edge_names(self, edge: ast.Edge):
        for name in (edge.source, edge.target):
            if name not in SPECIAL_NODES and name not in self.symbols.agents                     and name not in self.reported_unknown:
                self.reported_unknown.add(name)
                self.err("AF-E204", f"unknown agent '{name}' in flow", edge)
        if edge.target == "start":
            self.err("AF-E210", "an edge cannot go into 'start'", edge)
        if edge.source == "end":
            self.err("AF-E210", "an edge cannot leave 'end'", edge)

    def check_edge_condition(self, cond):
        if self.contains_call(cond):
            self.err("AF-E211", "edge conditions may only read state (no tool or llm calls); "
                                "compute the value in an agent and store it in a field", cond)
            return
        self.check_condition(cond)
        self.check_one_of_values(cond)

    def contains_call(self, expr) -> bool:
        if isinstance(expr, (ast.Call, ast.LlmCall)):
            return True
        if isinstance(expr, ast.BinaryOp):
            return self.contains_call(expr.left) or self.contains_call(expr.right)
        if isinstance(expr, ast.UnaryOp):
            return self.contains_call(expr.operand)
        return False

    def check_one_of_values(self, expr):
        """AF-W208: state.route == "factul" when route can only be "factual" or "conversational"."""
        if isinstance(expr, ast.BinaryOp):
            if expr.op in ("==", "!="):
                pairs = [(expr.left, expr.right), (expr.right, expr.left)]
                for ref, lit in pairs:
                    if isinstance(ref, ast.StateRef) and isinstance(lit, ast.Literal):
                        allowed = self.symbols.one_of_sets.get(ref.field)
                        if allowed is not None and lit.value not in allowed:
                            options = ", ".join(sorted(repr(v) for v in allowed))
                            self.warn("AF-W208", f"state.{ref.field} can only be one of [{options}], "
                                                 f"so comparing it with {ast.expr_to_source(lit)} is never true", lit)
            self.check_one_of_values(expr.left)
            self.check_one_of_values(expr.right)
        elif isinstance(expr, ast.UnaryOp):
            self.check_one_of_values(expr.operand)

    def check_edge_structure(self, flow: ast.FlowDecl):
        """Each node must have ONE plain edge, or conditional edges plus at most one else edge."""
        by_source = {}
        for edge in flow.edges:
            by_source.setdefault(edge.source, []).append(edge)
        for source, edges in by_source.items():
            plain = [e for e in edges if e.condition is None and not e.is_default]
            conditional = [e for e in edges if e.condition is not None]
            defaults = [e for e in edges if e.is_default]
            if len(plain) > 1:
                self.err("AF-E212", f"'{source}' has more than one plain edge; use conditions to choose "
                                    f"between targets", plain[1])
            elif plain and (conditional or defaults):
                self.err("AF-E212", f"'{source}' mixes a plain edge with conditional edges; "
                                    f"use -[else]-> for the default target", plain[0])
            if len(defaults) > 1:
                self.err("AF-E212", f"'{source}' has more than one else edge", defaults[1])


def analyze(program: ast.Program):
    """Convenience function: returns (symbols, diagnostics)."""
    return Analyzer(program).analyze()

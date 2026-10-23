"""Recursive descent parser: tokens -> AST.

One method per grammar rule in docs/grammar.md. Each method looks at the next
token (one token of lookahead, LL(1)) to decide what to do, consumes tokens,
and returns an AST node.

Error recovery (panic mode): when a rule finds an unexpected token it raises
ParseError. parse_program() catches it, records a diagnostic, skips tokens until
the start of the next declaration, and carries on. So one run reports several
syntax errors instead of stopping at the first.
"""

from contextlib import contextmanager

from . import ast_nodes as ast
from .diagnostics import Diagnostic, error
from .tokens import Token, TokenType as T

# tokens that close a group; a missing one is reported as AF-E102
CLOSERS = {T.RBRACE: "}", T.RBRACKET: "]", T.RPAREN: ")"}

# human readable names for error messages
TOKEN_NAMES = {
    T.IDENT: "a name", T.INT: "a number", T.STRING: "a string", T.EOF: "end of file",
    T.LBRACE: "'{'", T.RBRACE: "'}'", T.LPAREN: "'('", T.RPAREN: "')'",
    T.LBRACKET: "'['", T.RBRACKET: "']'", T.COMMA: "','", T.COLON: "':'",
    T.DOT: "'.'", T.ASSIGN: "'='", T.ARROW: "'->'", T.EDGE_OPEN: "'-['",
}

DECL_STARTS = {T.STATE, T.TOOL, T.AGENT, T.FLOW, T.COMPARE, T.NECESSITY}

# how deeply parentheses, `not` and if-blocks may nest; far beyond any real workflow,
# and it keeps the recursive phases (and the generated Python) well inside their limits
MAX_NESTING = 100
COMPARISON_OPS = {T.EQ, T.NEQ, T.LT, T.GT, T.LTE, T.GTE}


class ParseError(Exception):
    pass


def describe(token: Token) -> str:
    if token.type == T.EOF:
        return "end of file"
    if token.type == T.STRING:
        return f'string "{token.lexeme}"'
    return f"'{token.lexeme}'"


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self.diagnostics: list[Diagnostic] = []
        self.depth = 0   # current nesting depth (see MAX_NESTING)

    # ---------- token helpers ----------

    def peek(self, offset=0) -> Token:
        i = min(self.pos + offset, len(self.tokens) - 1)   # never read past EOF
        return self.tokens[i]

    def check(self, *types) -> bool:
        return self.peek().type in types

    def advance(self) -> Token:
        token = self.peek()
        if token.type != T.EOF:
            self.pos += 1
        return token

    def match(self, *types) -> bool:
        """Consume the next token if it is one of `types`."""
        if self.check(*types):
            self.advance()
            return True
        return False

    def expect(self, type_, what=None) -> Token:
        """Consume a token of the given type, or report an error."""
        if self.check(type_):
            return self.advance()
        found = self.peek()
        what = what or TOKEN_NAMES.get(type_, f"'{type_.name.lower()}'")
        # a closer that is missing because we hit the end or the next declaration
        if type_ in CLOSERS and (found.type == T.EOF or self.at_declaration_start()):
            self.fail("AF-E102", f"missing closing '{CLOSERS[type_]}' before {describe(found)}", found)
        self.fail("AF-E101", f"expected {what} but found {describe(found)}", found)

    def fail(self, code, message, token):
        self.diagnostics.append(error(code, "parser", message, token.line, token.col))
        raise ParseError(message)

    @contextmanager
    def nested(self):
        """Wrap a rule that can nest inside itself; report AF-E103 when it nests too deeply."""
        self.depth += 1
        try:
            if self.depth > MAX_NESTING:
                self.fail("AF-E103", f"nested more than {MAX_NESTING} levels deep; simplify this expression or block",
                          self.peek())
            yield
        finally:
            self.depth -= 1

    def at_declaration_start(self) -> bool:
        # `state` also appears inside agents as `state.x`, so only `state NAME` starts a declaration
        if self.check(T.STATE):
            return self.peek(1).type == T.IDENT
        return self.check(T.TOOL, T.AGENT, T.FLOW, T.COMPARE, T.NECESSITY)

    def synchronize(self):
        """Panic mode: skip tokens until the next declaration (or the end)."""
        self.advance()
        while not self.check(T.EOF) and not self.at_declaration_start():
            self.advance()

    # ---------- program and declarations ----------

    def parse_program(self) -> ast.Program:
        program = ast.Program(1, 1, [])
        while not self.check(T.EOF):
            try:
                program.declarations.append(self.parse_declaration())
            except ParseError:
                self.synchronize()
        return program

    def parse_declaration(self):
        if self.check(T.STATE):
            return self.parse_state_decl()
        if self.check(T.TOOL):
            return self.parse_tool_decl()
        if self.check(T.AGENT):
            return self.parse_agent_decl()
        if self.check(T.FLOW):
            return self.parse_flow_decl()
        if self.check(T.COMPARE):
            return self.parse_compare_decl()
        if self.check(T.NECESSITY):
            return self.parse_necessity_decl()
        self.fail("AF-E101", f"expected a declaration (state, tool, agent, flow, compare or necessity) but found {describe(self.peek())}",
                  self.peek())

    def parse_state_decl(self) -> ast.StateDecl:
        kw = self.expect(T.STATE)
        name = self.expect(T.IDENT, "a state name").lexeme
        self.expect(T.LBRACE)
        decl = ast.StateDecl(kw.line, kw.col, name, [])
        while not self.check(T.RBRACE, T.EOF) and not self.at_declaration_start():
            decl.fields.append(self.parse_field_decl())
        self.expect(T.RBRACE)
        return decl

    def parse_field_decl(self) -> ast.FieldDecl:
        first = self.peek()
        modifier = None
        untrusted = False
        if self.match(T.INPUT):
            modifier = "input"
            untrusted = self.match(T.UNTRUSTED)
        elif self.match(T.OUTPUT):
            modifier = "output"
        name = self.expect(T.IDENT, "a field name").lexeme
        self.expect(T.COLON)
        field_type = self.parse_type()
        default = None
        if self.match(T.ASSIGN):
            default = self.parse_default()
        return ast.FieldDecl(first.line, first.col, name, modifier, field_type, default, untrusted)

    def parse_default(self):
        """A literal, or [literal, ...] for list fields."""
        if self.match(T.LBRACKET):
            items = []
            if not self.check(T.RBRACKET):
                items.append(self.parse_literal())
                while self.match(T.COMMA):
                    items.append(self.parse_literal())
            self.expect(T.RBRACKET)
            return items
        return self.parse_literal()

    def parse_type(self) -> str:
        token = self.peek()
        if self.match(T.STRING_TYPE):
            return "string"
        if self.match(T.INT_TYPE):
            return "int"
        if self.match(T.BOOL_TYPE):
            return "bool"
        if self.match(T.LIST_TYPE):
            self.expect(T.LBRACKET)
            inner = self.parse_type()
            self.expect(T.RBRACKET)
            return f"list[{inner}]"
        self.fail("AF-E101", f"expected a type (string, int, bool, list[...]) but found {describe(token)}", token)

    def parse_tool_decl(self) -> ast.ToolDecl:
        kw = self.expect(T.TOOL)
        name = self.expect(T.IDENT, "a tool name").lexeme
        self.expect(T.LPAREN)
        params = []
        if not self.check(T.RPAREN):
            params.append(self.parse_param())
            while self.match(T.COMMA):
                params.append(self.parse_param())
        self.expect(T.RPAREN)
        self.expect(T.ARROW)
        return_type = self.parse_type()
        tags = []
        while self.check(T.UNTRUSTED, T.SENSITIVE, T.SANITIZER):
            tags.append(self.advance().lexeme)
        return ast.ToolDecl(kw.line, kw.col, name, params, return_type, tags)

    def parse_param(self) -> ast.Param:
        token = self.expect(T.IDENT, "a parameter name")
        self.expect(T.COLON)
        return ast.Param(token.line, token.col, token.lexeme, self.parse_type())

    def parse_agent_decl(self) -> ast.AgentDecl:
        kw = self.expect(T.AGENT)
        name = self.expect(T.IDENT, "an agent name").lexeme
        return ast.AgentDecl(kw.line, kw.col, name, self.parse_block())

    # ---------- statements ----------

    def parse_block(self) -> list:
        with self.nested():
            self.expect(T.LBRACE)
            statements = []
            while not self.check(T.RBRACE, T.EOF) and not self.at_declaration_start():
                statements.append(self.parse_statement())
            self.expect(T.RBRACE)
            return statements

    def parse_statement(self):
        if self.check(T.IF):
            return self.parse_if()
        if self.check(T.STATE):
            return self.parse_assign()
        self.fail("AF-E101", f"expected a statement (state.field = ... or if) but found {describe(self.peek())}",
                  self.peek())

    def parse_assign(self) -> ast.Assign:
        target = self.parse_state_ref()
        self.expect(T.ASSIGN)
        return ast.Assign(target.line, target.col, target, self.parse_expr())

    def parse_if(self) -> ast.If:
        kw = self.expect(T.IF)
        self.expect(T.LPAREN)
        condition = self.parse_expr()
        self.expect(T.RPAREN)
        then_body = self.parse_block()
        else_body = []
        if self.match(T.ELSE):
            if self.check(T.IF):
                else_body = [self.parse_if()]   # else if ... is an If inside the else branch
            else:
                else_body = self.parse_block()
        return ast.If(kw.line, kw.col, condition, then_body, else_body)

    # ---------- flow ----------

    def parse_flow_decl(self) -> ast.FlowDecl:
        kw = self.expect(T.FLOW)
        name = self.expect(T.IDENT, "a flow name").lexeme
        self.expect(T.LBRACE)
        flow = ast.FlowDecl(kw.line, kw.col, name, [])
        while not self.check(T.RBRACE, T.EOF) and not self.at_declaration_start():
            flow.edges.extend(self.parse_edge_chain())
        self.expect(T.RBRACE)
        return flow

    def parse_edge_chain(self) -> list[ast.Edge]:
        """a -> b -[cond]-> c  becomes two Edge nodes: a->b and b->c."""
        source = self.parse_flow_node()
        edges = []
        while True:
            condition, is_default, max_iterations = self.parse_arrow()
            target = self.parse_flow_node()
            edges.append(ast.Edge(source.line, source.col, source.lexeme, target.lexeme,
                                  condition, is_default, max_iterations))
            source = target
            if not self.check(T.ARROW, T.EDGE_OPEN):
                return edges

    def parse_flow_node(self) -> Token:
        token = self.peek()
        if self.match(T.IDENT, T.START, T.END):
            return token
        self.fail("AF-E101", f"expected an agent name, start or end but found {describe(token)}", token)

    def parse_arrow(self):
        """Parse `->` or `-[label]->`. Returns (condition, is_default, max_iterations)."""
        condition, is_default, max_iterations = None, False, None
        if self.match(T.EDGE_OPEN):
            if self.match(T.ELSE):
                is_default = True
            else:
                condition = self.parse_expr()
                if self.match(T.COMMA):
                    self.expect(T.MAX_ITERATIONS, "'max_iterations'")
                    max_iterations = int(self.expect(T.INT, "a number").lexeme)
            self.expect(T.RBRACKET)
            self.expect(T.ARROW)
        else:
            self.expect(T.ARROW, "'->' or '-['")
        return condition, is_default, max_iterations

    # ---------- compare: several agents for the same job, scored on test cases ----------

    def parse_compare_decl(self) -> ast.CompareDecl:
        kw = self.expect(T.COMPARE)
        name = self.expect(T.IDENT, "a compare name").lexeme
        self.expect(T.LBRACE)
        decl = ast.CompareDecl(kw.line, kw.col, name, [], [])
        if self.check(T.CANDIDATES):
            self.advance()
            decl.candidates.append(self.parse_candidate())
            while self.match(T.COMMA):
                decl.candidates.append(self.parse_candidate())
        else:
            self.fail("AF-E101", f"expected 'candidates' but found {describe(self.peek())}", self.peek())
        while self.check(T.END_TO_END, T.ENSURE):
            token = self.advance()
            if token.type == T.END_TO_END:
                decl.end_to_end = True
            else:
                decl.ensure = self.parse_expr()
        while self.check(T.CASE):
            decl.cases.append(self.parse_case())
        self.expect(T.RBRACE)
        return decl

    def parse_necessity_decl(self) -> ast.NecessityDecl:
        kw = self.expect(T.NECESSITY)
        name = self.expect(T.IDENT, "a necessity name").lexeme
        self.expect(T.LBRACE)
        decl = ast.NecessityDecl(kw.line, kw.col, name, [])
        while self.check(T.CASE):
            decl.cases.append(self.parse_case())
        self.expect(T.RBRACE)
        return decl

    def parse_candidate(self) -> ast.CandidateRef:
        token = self.expect(T.IDENT, "an agent name")
        return ast.CandidateRef(token.line, token.col, token.lexeme)

    def parse_case(self) -> ast.CompareCase:
        kw = self.expect(T.CASE)
        self.expect(T.LBRACE)
        case = ast.CompareCase(kw.line, kw.col, [], [], [])
        while not self.check(T.RBRACE, T.EOF) and not self.at_declaration_start():
            token = self.peek()
            if self.check(T.STATE):
                target = self.parse_state_ref()
                self.expect(T.ASSIGN)
                case.inputs.append(ast.Assign(target.line, target.col, target, self.parse_default()))
            elif self.match(T.EXPECT):
                target = self.parse_state_ref()
                if self.check(T.CONTAINS, T.EQ):
                    op = "==" if self.advance().type == T.EQ else "contains"
                else:
                    self.fail("AF-E101", f"expected 'contains' or '==' but found {describe(self.peek())}", self.peek())
                case.expects.append(ast.Expect(token.line, token.col, target, op, self.parse_literal()))
            elif self.match(T.MOCK):
                agent = self.expect(T.IDENT, "an agent name").lexeme
                text = self.expect(T.STRING, "the scripted answer (a string)").lexeme
                case.mocks.append(ast.MockAnswer(token.line, token.col, agent, text))
            else:
                self.fail("AF-E101", f"expected state.field = ..., expect or mock inside a case but found "
                                     f"{describe(token)}", token)
        self.expect(T.RBRACE)
        return case

    # ---------- expressions (lowest precedence first) ----------

    def parse_expr(self):
        with self.nested():
            return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.check(T.OR):
            op = self.advance()
            left = ast.BinaryOp(op.line, op.col, "or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.check(T.AND):
            op = self.advance()
            left = ast.BinaryOp(op.line, op.col, "and", left, self.parse_not())
        return left

    def parse_not(self):
        if self.check(T.NOT):
            op = self.advance()
            with self.nested():
                return ast.UnaryOp(op.line, op.col, "not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_primary()
        if self.check(*COMPARISON_OPS):
            op = self.advance()
            left = ast.BinaryOp(op.line, op.col, op.lexeme, left, self.parse_primary())
        return left

    def parse_primary(self):
        token = self.peek()
        if self.check(T.STRING, T.INT, T.TRUE, T.FALSE):
            return self.parse_literal()
        if self.check(T.STATE):
            return self.parse_state_ref()
        if self.check(T.IDENT):
            return self.parse_tool_call()
        if self.check(T.LLM):
            return self.parse_llm_call()
        if self.match(T.LPAREN):
            expr = self.parse_expr()
            self.expect(T.RPAREN)
            return expr
        self.fail("AF-E101", f"expected a value or expression but found {describe(token)}", token)

    def parse_literal(self) -> ast.Literal:
        token = self.peek()
        if self.match(T.STRING):
            return ast.Literal(token.line, token.col, token.lexeme, "string")
        if self.match(T.INT):
            return ast.Literal(token.line, token.col, int(token.lexeme), "int")
        if self.match(T.TRUE, T.FALSE):
            return ast.Literal(token.line, token.col, token.type == T.TRUE, "bool")
        self.fail("AF-E101", f"expected a literal (string, number, true, false) but found {describe(token)}", token)

    def parse_state_ref(self) -> ast.StateRef:
        kw = self.expect(T.STATE, "'state'")
        self.expect(T.DOT)
        name = self.expect(T.IDENT, "a field name").lexeme
        return ast.StateRef(kw.line, kw.col, name)

    def parse_tool_call(self) -> ast.Call:
        name = self.expect(T.IDENT)
        self.expect(T.LPAREN, "'(' (a bare name is not a value; use state.field)")
        return ast.Call(name.line, name.col, name.lexeme, self.parse_args())

    def parse_args(self) -> list:
        """Comma separated expressions up to and including ')'. The '(' was already consumed."""
        args = []
        if not self.check(T.RPAREN):
            args.append(self.parse_expr())
            while self.match(T.COMMA):
                args.append(self.parse_expr())
        self.expect(T.RPAREN)
        return args

    def parse_llm_call(self) -> ast.LlmCall:
        kw = self.expect(T.LLM)
        self.expect(T.LPAREN)
        prompt = self.expect(T.STRING, "a prompt string").lexeme
        args = []
        while self.match(T.COMMA):
            args.append(self.parse_expr())
        self.expect(T.RPAREN)
        one_of = None
        if self.match(T.ONE_OF):
            self.expect(T.LBRACKET)
            one_of = [self.parse_literal()]
            while self.match(T.COMMA):
                one_of.append(self.parse_literal())
            self.expect(T.RBRACKET)
        return ast.LlmCall(kw.line, kw.col, prompt, args, one_of)


def parse(tokens: list[Token]):
    """Convenience function: returns (program, diagnostics)."""
    parser = Parser(tokens)
    program = parser.parse_program()
    return program, parser.diagnostics

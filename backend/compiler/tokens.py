"""Token kinds and the Token record produced by the lexer."""

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    # values that carry text
    IDENT = auto()
    INT = auto()
    STRING = auto()

    # keywords
    STATE = auto()
    TOOL = auto()
    AGENT = auto()
    FLOW = auto()
    START = auto()
    END = auto()
    INPUT = auto()
    OUTPUT = auto()
    LLM = auto()
    ONE_OF = auto()
    IF = auto()
    ELSE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    TRUE = auto()
    FALSE = auto()
    MAX_ITERATIONS = auto()
    UNTRUSTED = auto()
    SENSITIVE = auto()
    SANITIZER = auto()
    STRING_TYPE = auto()
    INT_TYPE = auto()
    BOOL_TYPE = auto()
    LIST_TYPE = auto()

    # symbols
    LBRACE = auto()      # {
    RBRACE = auto()      # }
    LPAREN = auto()      # (
    RPAREN = auto()      # )
    LBRACKET = auto()    # [
    RBRACKET = auto()    # ]
    COMMA = auto()       # ,
    COLON = auto()       # :
    DOT = auto()         # .
    ASSIGN = auto()      # =
    EQ = auto()          # ==
    NEQ = auto()         # !=
    LT = auto()          # <
    GT = auto()          # >
    LTE = auto()         # <=
    GTE = auto()         # >=
    ARROW = auto()       # ->
    EDGE_OPEN = auto()   # -[

    EOF = auto()


# keyword text -> token type
KEYWORDS = {
    "state": TokenType.STATE,
    "tool": TokenType.TOOL,
    "agent": TokenType.AGENT,
    "flow": TokenType.FLOW,
    "start": TokenType.START,
    "end": TokenType.END,
    "input": TokenType.INPUT,
    "output": TokenType.OUTPUT,
    "llm": TokenType.LLM,
    "one_of": TokenType.ONE_OF,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "max_iterations": TokenType.MAX_ITERATIONS,
    "untrusted": TokenType.UNTRUSTED,
    "sensitive": TokenType.SENSITIVE,
    "sanitizer": TokenType.SANITIZER,
    "string": TokenType.STRING_TYPE,
    "int": TokenType.INT_TYPE,
    "bool": TokenType.BOOL_TYPE,
    "list": TokenType.LIST_TYPE,
}


@dataclass
class Token:
    type: TokenType
    lexeme: str   # the exact text from the source (for strings: the value without quotes)
    line: int     # 1-based
    col: int      # 1-based

    def to_dict(self) -> dict:
        return {"type": self.type.name, "lexeme": self.lexeme, "line": self.line, "col": self.col}

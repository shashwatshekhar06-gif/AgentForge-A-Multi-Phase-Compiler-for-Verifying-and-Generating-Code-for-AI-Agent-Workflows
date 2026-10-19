"""Lexer: turns source text into a list of tokens.

A hand written scanner. It walks the text one character at a time,
keeps track of the current line and column, and groups characters into tokens.
Errors (bad characters, unterminated strings) are recorded and scanning continues,
so one run can report several lexical errors.
"""

from .diagnostics import Diagnostic, error
from .tokens import KEYWORDS, Token, TokenType

TWO_CHAR = {
    "==": TokenType.EQ,
    "!=": TokenType.NEQ,
    "<=": TokenType.LTE,
    ">=": TokenType.GTE,
    "->": TokenType.ARROW,
    "-[": TokenType.EDGE_OPEN,
}

SINGLE_CHAR = {
    "=": TokenType.ASSIGN,
    "<": TokenType.LT,
    ">": TokenType.GT,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "[": TokenType.LBRACKET,
    "]": TokenType.RBRACKET,
    ",": TokenType.COMMA,
    ":": TokenType.COLON,
    ".": TokenType.DOT,
}

def is_name_start(ch: str) -> bool:
    # ASCII only: Python's isalpha()/isdigit() also accept characters like '²' or 'é',
    # which would break int() and the generated Python names
    return ch.isascii() and (ch.isalpha() or ch == "_")


def is_digit(ch: str) -> bool:
    return ch.isascii() and ch.isdigit()


ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []
        self.diagnostics: list[Diagnostic] = []

    # ---- small helpers ----

    def peek(self, offset=0) -> str:
        """Character at pos+offset, or '' at the end of input."""
        i = self.pos + offset
        return self.source[i] if i < len(self.source) else ""

    def advance(self) -> str:
        """Consume one character and update line/column."""
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def add(self, type_, lexeme, line, col):
        self.tokens.append(Token(type_, lexeme, line, col))

    # ---- main loop ----

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.source):
            ch = self.peek()
            line, col = self.line, self.col

            if ch in " \t\r\n":
                self.advance()
            elif ch == "#":
                # comment: skip to end of line
                while self.peek() not in ("", "\n"):
                    self.advance()
            elif is_name_start(ch):
                self.scan_word(line, col)
            elif is_digit(ch):
                self.scan_number(line, col)
            elif ch == '"':
                self.scan_string(line, col)
            else:
                self.scan_symbol(ch, line, col)

        self.add(TokenType.EOF, "", self.line, self.col)
        return self.tokens

    def scan_word(self, line, col):
        start = self.pos
        while is_name_start(self.peek()) or is_digit(self.peek()):
            self.advance()
        word = self.source[start:self.pos]
        self.add(KEYWORDS.get(word, TokenType.IDENT), word, line, col)

    def scan_number(self, line, col):
        start = self.pos
        while is_digit(self.peek()):
            self.advance()
        self.add(TokenType.INT, self.source[start:self.pos], line, col)

    def scan_string(self, line, col):
        self.advance()  # opening quote
        chars = []
        while True:
            ch = self.peek()
            if ch == "" or ch == "\n":
                self.diagnostics.append(error(
                    "AF-E002", "lexer", "unterminated string (missing closing \")", line, col))
                return
            if ch == '"':
                self.advance()
                break
            if ch == "\\" and self.peek(1) in ESCAPES:
                self.advance()
                chars.append(ESCAPES[self.advance()])
            else:
                chars.append(self.advance())
        self.add(TokenType.STRING, "".join(chars), line, col)

    def scan_symbol(self, ch, line, col):
        # try two character symbols first (longest match), so "==" is not read as "=" "="
        two = ch + self.peek(1)
        if two in TWO_CHAR:
            self.advance()
            self.advance()
            self.add(TWO_CHAR[two], two, line, col)
            return

        if ch in SINGLE_CHAR:
            self.advance()
            self.add(SINGLE_CHAR[ch], ch, line, col)
            return

        # anything else is an error; skip the character and keep going
        self.advance()
        self.diagnostics.append(error("AF-E001", "lexer", f"unexpected character '{ch}'", line, col))


def tokenize(source: str):
    """Convenience function: returns (tokens, diagnostics)."""
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    return tokens, lexer.diagnostics

"""Small helpers shared by the tests."""

import json
from pathlib import Path

from compiler.ast_nodes import from_dict
from compiler.ir import build_ir
from compiler.lexer import tokenize
from compiler.parser import parse
from compiler.semantic import analyze

TESTS_DIR = Path(__file__).parent
EXAMPLES_DIR = TESTS_DIR.parent.parent / "examples"


def load_fixture(name):
    return from_dict(json.loads((TESTS_DIR / "fixtures" / "ast" / name).read_text()))


def parse_source(source):
    tokens, lex_diags = tokenize(source)
    assert not lex_diags, lex_diags
    program, diags = parse(tokens)
    assert not diags, diags
    return program


def analyze_source(source):
    return analyze(parse_source(source))


def graph_of(source):
    symbols, diags = analyze_source(source)
    assert not [d for d in diags if d.is_error], diags
    return build_ir(symbols), symbols


def codes(diagnostics):
    return [d.code for d in diagnostics]

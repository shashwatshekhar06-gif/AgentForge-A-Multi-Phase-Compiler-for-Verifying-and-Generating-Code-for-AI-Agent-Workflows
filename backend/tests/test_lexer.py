from compiler.lexer import tokenize
from compiler.tokens import TokenType as T


def types(source):
    tokens, _ = tokenize(source)
    return [t.type for t in tokens]


def test_keywords_identifiers_and_symbols():
    assert types("agent foo { }") == [T.AGENT, T.IDENT, T.LBRACE, T.RBRACE, T.EOF]


def test_two_character_symbols_use_longest_match():
    assert types("== != <= >= -> -[ = < >") == [
        T.EQ, T.NEQ, T.LTE, T.GTE, T.ARROW, T.EDGE_OPEN, T.ASSIGN, T.LT, T.GT, T.EOF]


def test_labelled_edge():
    assert types('a -[state.x == "y"]-> b') == [
        T.IDENT, T.EDGE_OPEN, T.STATE, T.DOT, T.IDENT, T.EQ, T.STRING, T.RBRACKET, T.ARROW, T.IDENT, T.EOF]


def test_line_and_column_tracking():
    tokens, _ = tokenize("state S {\n  input q: string\n}")
    q = next(t for t in tokens if t.lexeme == "q")
    assert (q.line, q.col) == (2, 9)


def test_strings_numbers_and_escapes():
    tokens, diags = tokenize('"say \\"hi\\"" 42')
    assert not diags
    assert tokens[0].type == T.STRING and tokens[0].lexeme == 'say "hi"'
    assert tokens[1].type == T.INT and tokens[1].lexeme == "42"


def test_comments_are_skipped():
    assert types("# a comment\nflow # another\n") == [T.FLOW, T.EOF]


def test_reports_multiple_errors_and_keeps_going():
    tokens, diags = tokenize('agent @ x $ "open')
    assert [d.code for d in diags] == ["AF-E001", "AF-E001", "AF-E002"]
    assert [t.type for t in tokens] == [T.AGENT, T.IDENT, T.EOF]
    assert (diags[0].line, diags[0].col) == (1, 7)

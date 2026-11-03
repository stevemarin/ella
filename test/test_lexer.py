from pathlib import Path

import pytest

from ella.lexer import EllaError, Span, Token, TokenKind, tokenize

K = TokenKind
EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def kinds(source: str) -> list[TokenKind]:
    return [token.kind for token in tokenize(source)]


def texts(source: str) -> list[str]:
    """The text of each token, without the trailing layout tokens."""
    return [token.text for token in tokenize(source) if token.kind not in (K.NEWLINE, K.DEDENT, K.EOF)]


def values(source: str) -> list[int | float | str | None]:
    return [token.value for token in tokenize(source) if token.value is not None]


def error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        tokenize(source)
    return info.value


def test_example_file_tokenizes() -> None:
    tokens = tokenize(EXAMPLE.read_text())
    assert tokens[-1].kind is K.EOF


def test_empty_source() -> None:
    assert kinds("") == [K.EOF]
    assert kinds("\n# just a comment\n\n") == [K.EOF]


def test_names_and_keywords() -> None:
    tokens = tokenize("let x_1 = _ i64 true")
    assert [(t.kind, t.text) for t in tokens[:-2]] == [
        (K.KEYWORD, "let"),
        (K.NAME, "x_1"),
        (K.OP, "="),
        (K.NAME, "_"),
        (K.NAME, "i64"),
        (K.KEYWORD, "true"),
    ]


def test_operators_longest_match() -> None:
    assert texts("a //= b // c / d") == ["a", "//=", "b", "//", "c", "/", "d"]
    assert texts("0..=n ..< x->y=>z|>w") == ["0", "..=", "n", "..", "<", "x", "->", "y", "=>", "z", "|>", "w"]
    assert texts("a**b<<c>>d!=e") == ["a", "**", "b", "<<", "c", ">>", "d", "!=", "e"]


@pytest.mark.parametrize("char", [";", "!", "@", "`", "é"])
def test_invalid_characters(char: str) -> None:
    assert "unexpected character" in error(f"a {char} b").message


def test_braces_are_brackets() -> None:
    # Map and set literals: newlines inside are ignored, and braces must match.
    assert texts("{1: [2,\n  3]}") == ["{", "1", ":", "[", "2", ",", "3", "]", "}"]
    assert error("a { b").message == "'{' is never closed"
    assert error("a } b").message == "unmatched '}'"
    assert error("(1}").message == "unmatched '}'"


def test_interpolation_ends_at_double_brace_with_no_brace_open() -> None:
    assert texts('"{{ {1: {2}}}}"') == ['"', "{{", "{", "1", ":", "{", "2", "}", "}", "}}", '"']


def test_tabs_are_errors() -> None:
    assert "tab" in error("a\tb").message
    assert "tab" in error("if x then\n\tfoo").message
    # but tabs are fine in strings and comments
    assert values('"a\tb" # c\td') == ["a\tb"]


def test_spans() -> None:
    tokens = tokenize("let x = 1\n  # comment\nfoo(\n  bar)")
    spans = {t.text: t.span for t in tokens}
    assert spans["let"] == Span(1, 1)
    assert spans["1"] == Span(1, 9)
    assert spans["foo"] == Span(3, 1)
    assert spans["bar"] == Span(4, 3)
    assert str(Span(4, 3)) == "4:3"


def test_error_message_includes_span() -> None:
    assert str(error("x = 1\ny = @")) == "2:5: unexpected character '@'"


# Numbers


def test_integers() -> None:
    assert values("0 7 1_000_000 0xFF 0Xff_ff 9223372036854775808") == [0, 7, 1_000_000, 255, 0xFFFF, 2**63]
    assert kinds("42")[0] is K.INT


def test_floats() -> None:
    assert values("1.0 0.5 2.5e-3 1.5E+2 1_0.2_5 3.0e10") == [1.0, 0.5, 2.5e-3, 150.0, 10.25, 3.0e10]
    assert kinds("1.0")[0] is K.FLOAT


def test_range_after_integer_is_not_a_float() -> None:
    assert texts("0..n") == ["0", "..", "n"]
    assert texts("1.foo") == ["1", ".", "foo"]


@pytest.mark.parametrize("source", ["007", "1_", "1__0", "0x", "1e3", "12abc", "0xFG", "1.0e", "1.0e+"])
def test_bad_numbers(source: str) -> None:
    error(source)


# Strings


def string_tokens(source: str) -> list[tuple[TokenKind, str | int | float | None]]:
    """(kind, value or text) for each token, without the trailing layout tokens."""
    return [
        (t.kind, t.text if t.value is None else t.value)
        for t in tokenize(source)
        if t.kind not in (K.NEWLINE, K.DEDENT, K.EOF)
    ]


def test_plain_string() -> None:
    assert string_tokens('"hello # not a comment"') == [
        (K.STRING_START, '"'),
        (K.STRING_TEXT, "hello # not a comment"),
        (K.STRING_END, '"'),
    ]
    assert string_tokens('""') == [(K.STRING_START, '"'), (K.STRING_END, '"')]


def test_escapes() -> None:
    assert values(r'"\\ \" \n \r \t \0 \x41 \u{1F600} \u{e9} \{{x}}"') == ['\\ " \n \r \t \0 A 😀 é {{x}}']


def test_escaped_token_keeps_source_text() -> None:
    token = tokenize(r'"a\n"')[1]
    assert token == Token(K.STRING_TEXT, r"a\n", Span(1, 2), "a\n")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (r'"\q"', "unknown escape"),
        (r'"\x80"', "go up to \\x7F"),
        (r'"\x4"', "two hex digits"),
        (r'"\u41"', "expected '{'"),
        (r'"\u{}"', "1 to 6 hex digits"),
        (r'"\u{1234567}"', "1 to 6 hex digits"),
        (r'"\u{D800}"', "not a valid Unicode code point"),
        (r'"\u{110000}"', "not a valid Unicode code point"),
    ],
)
def test_bad_escapes(source: str, message: str) -> None:
    assert message in error(source).message


def test_unterminated_strings() -> None:
    assert "never closed" in error('"abc').message
    assert "line break" in error('"abc\ndef"').message
    assert "never closed" in error('"""abc\n').message


def test_unicode_inside_strings_and_comments() -> None:
    assert values('"héllo" # ünïcode') == ["héllo"]


def test_multiline_string_is_kept_exactly() -> None:
    source = 'let s = """one\n    two\n"""\nx'
    assert values(source) == ["one\n    two\n"]
    # the string doesn't affect layout: one NEWLINE after it, and no INDENT
    assert kinds(source).count(K.NEWLINE) == 2
    assert K.INDENT not in kinds(source)
    assert tokenize(source)[-3].span == Span(4, 1)


def test_interpolation() -> None:
    assert string_tokens('"Hi, {{name}}!"') == [
        (K.STRING_START, '"'),
        (K.STRING_TEXT, "Hi, "),
        (K.INTERP_START, "{{"),
        (K.NAME, "name"),
        (K.INTERP_END, "}}"),
        (K.STRING_TEXT, "!"),
        (K.STRING_END, '"'),
    ]


def test_interpolation_with_nested_brackets_and_strings() -> None:
    assert string_tokens('"{{ f(xs[0], "}}") }}"') == [
        (K.STRING_START, '"'),
        (K.INTERP_START, "{{"),
        (K.NAME, "f"),
        (K.OP, "("),
        (K.NAME, "xs"),
        (K.OP, "["),
        (K.INT, 0),
        (K.OP, "]"),
        (K.OP, ","),
        (K.STRING_START, '"'),
        (K.STRING_TEXT, "}}"),
        (K.STRING_END, '"'),
        (K.OP, ")"),
        (K.INTERP_END, "}}"),
        (K.STRING_END, '"'),
    ]


def test_single_braces_are_text() -> None:
    assert values('"{a} {x}}"') == ["{a} {x}}"]
    assert texts('"{{x}}}"') == ['"', "{{", "x", "}}", "}", '"']


def test_bad_interpolations() -> None:
    assert "empty interpolation" in error('"{{ }}"').message
    assert "never closed" in error('"{{ x "').message
    assert "line break" in error('"{{ x\n }}"').message
    assert "'(' is never closed" in error('"{{ f( }}"').message
    assert "unmatched ')'" in error('f("{{ ) }}")').message


def test_multiline_interpolation_may_span_lines() -> None:
    assert texts('"""{{ f(1,\n 2) }}"""') == ['"""', "{{", "f", "(", "1", ",", "2", ")", "}}", '"""']


# Single quotes


def test_single_quoted_strings_work_like_double_quoted() -> None:
    assert string_tokens("'Hi, {{name}}!'") == [
        (K.STRING_START, "'"),
        (K.STRING_TEXT, "Hi, "),
        (K.INTERP_START, "{{"),
        (K.NAME, "name"),
        (K.INTERP_END, "}}"),
        (K.STRING_TEXT, "!"),
        (K.STRING_END, "'"),
    ]
    assert values(r"'a\tb\u{e9}'") == ["a\tbé"]
    assert values("''") == []
    assert "line break" in error("'abc\ndef'").message


def test_the_other_quote_needs_no_escape() -> None:
    assert values(""""it's" 'say "hi"' """) == ["it's", 'say "hi"']
    # and both escapes work in both kinds of string
    assert values(r"""'it\'s' "\"hi\"" '\"' "\'" """) == ["it's", '"hi"', '"', "'"]


def test_triple_single_quotes() -> None:
    source = "let s = '''one\n  'two' \"three\"\n'''\nx"
    assert values(source) == ["one\n  'two' \"three\"\n"]
    assert K.INDENT not in kinds(source)
    assert "never closed" in error("'''abc\n").message


def test_nested_strings_can_mix_quotes() -> None:
    assert values("""'{{ f("a", 'b') }}'""") == ["a", "b"]


# Layout


def test_newline_ends_each_line() -> None:
    assert kinds("a\nb") == [K.NAME, K.NEWLINE, K.NAME, K.NEWLINE, K.EOF]
    assert kinds("a\nb\n") == [K.NAME, K.NEWLINE, K.NAME, K.NEWLINE, K.EOF]


def test_indent_and_dedent() -> None:
    source = "a\n    b\n        c\n    d\ne\n"
    assert kinds(source) == [
        K.NAME, K.NEWLINE,
        K.INDENT, K.NAME, K.NEWLINE,
        K.INDENT, K.NAME, K.NEWLINE,
        K.DEDENT, K.NAME, K.NEWLINE,
        K.DEDENT, K.NAME, K.NEWLINE,
        K.EOF,
    ]  # fmt: skip


def test_dedents_at_end_of_file() -> None:
    assert kinds("a\n  b\n    c") == [
        K.NAME, K.NEWLINE, K.INDENT, K.NAME, K.NEWLINE, K.INDENT, K.NAME, K.NEWLINE, K.DEDENT, K.DEDENT, K.EOF,
    ]  # fmt: skip


def test_blank_and_comment_lines_dont_affect_layout() -> None:
    source = "a\n\n      # comment\n  b\n\n# comment\n  c\n"
    assert kinds(source) == [K.NAME, K.NEWLINE, K.INDENT, K.NAME, K.NEWLINE, K.NAME, K.NEWLINE, K.DEDENT, K.EOF]


def test_any_consistent_indent_width() -> None:
    assert kinds("a\n b\n   c\n") == [
        K.NAME, K.NEWLINE, K.INDENT, K.NAME, K.NEWLINE, K.INDENT, K.NAME, K.NEWLINE, K.DEDENT, K.DEDENT, K.EOF,
    ]  # fmt: skip


def test_dedent_must_match_an_outer_level() -> None:
    assert "indentation" in error("a\n    b\n  c\n").message


def test_newlines_ignored_inside_brackets() -> None:
    source = "f(a,\n        b) + [\n  1,\n]\nx"
    assert kinds(source).count(K.NEWLINE) == 2
    assert K.INDENT not in kinds(source)


def test_unbalanced_brackets() -> None:
    assert "unmatched ')'" in error("a)").message
    assert "unmatched ']'" in error("(a]").message
    err = error("f(a,\n  [b")
    assert "'[' is never closed" in err.message
    assert err.span == Span(2, 3)

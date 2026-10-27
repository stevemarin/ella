from ella.token import Float, Int, _lex_literal_integer_float, _lex_literal_string, lex


def test_literal_integer_1() -> None:
    s = "123"
    idx, column, line, res = _lex_literal_integer_float(0, 0, 0, s)
    assert idx == len(s)
    assert column == idx
    assert line == 0
    assert int(res.value) == 123


def test_literal_integer_2() -> None:
    s = "123_123"
    idx, column, line, res = _lex_literal_integer_float(0, 0, 0, s)
    assert idx == len(s)
    assert column == idx
    assert line == 0
    assert int(res.value) == 123123


def test_literal_integer_3() -> None:
    s = "123__123"
    try:
        _lex_literal_integer_float(0, 0, 0, s)
    except ValueError:
        return
    assert False


def test_literal_float_1() -> None:
    s = "123.123"
    idx, column, line, res = _lex_literal_integer_float(0, 0, 0, s)
    assert idx == len(s)
    assert column == idx
    assert line == 0
    assert float(res.value) == 123.123


def test_literal_float_2() -> None:
    s = "123.123e3"
    idx, column, line, res = _lex_literal_integer_float(0, 0, 0, s)
    assert idx == len(s)
    assert column == idx
    assert line == 0
    assert float(res.value) == 123123.0


def test_literal_float_3() -> None:
    s = "123.123E3"
    idx, column, line, res = _lex_literal_integer_float(0, 0, 0, s)
    assert idx == len(s)
    assert column == idx
    assert line == 0
    assert float(res.value) == 123123.0


def test_literal_float_4() -> None:
    s = "123__123e+3"
    try:
        _lex_literal_integer_float(0, 0, 0, s)
    except ValueError:
        return
    assert False


def test_literal_float_5() -> None:
    s = "123__123e+3.5"
    try:
        _lex_literal_integer_float(0, 0, 0, s)
    except ValueError:
        return
    assert False


def test_literal_float_6() -> None:
    s = "123__123e-+3"
    try:
        _lex_literal_integer_float(0, 0, 0, s)
    except ValueError:
        return
    assert False


def test_literal_string_1() -> None:
    s = '"abc123"'
    idx, column, line, res = _lex_literal_string(0, 0, 0, s)
    assert idx + 1 == len(s)
    assert column == idx
    assert line == 0
    assert res.value == "abc123"


def test_literal_string_2() -> None:
    s = "'abc123'"
    idx, column, line, res = _lex_literal_string(0, 0, 0, s)
    assert idx + 1 == len(s)
    assert column == idx
    assert line == 0
    assert res.value == "abc123"


def test_literal_string_3() -> None:
    s = "'abc123"
    try:
        _lex_literal_string(0, 0, 0, s)
    except ValueError:
        return
    assert False

def test_lex_1() -> None:
    s = "int int float"
    tokens = list(lex(s))
    assert len(tokens) == 3
    assert isinstance(tokens[0], Int)
    assert isinstance(tokens[1], Int)
    assert isinstance(tokens[2], Float)
    
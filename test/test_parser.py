from pathlib import Path

import pytest

from ella.lexer import EllaError, Span, tokenize
from ella.parser import parse, parse_partial
from ella.syntax import Assign, Binary, Call, IntLit, Lambda, Let, Name, show

EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def lines(source: str) -> list[str]:
    """Each top-level expression of `source`, shown as an S-expression."""
    return [show(expr) for expr in parse(source).body]


def one(source: str) -> str:
    [line] = lines(source)
    return line


def error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        parse(source)
    return info.value


def test_example_file_parses() -> None:
    """The stage 1 acceptance test."""
    program = parse(EXAMPLE.read_text())
    assert [".".join(line.path) for line in program.imports] == ["lists", "maps.new_map"]
    assert len(program.body) == 151


# Precedence (docs/grammar.md §8)


@pytest.mark.parametrize(
    ("source", "tree"),
    [
        ("1 + 2 * 3", "(+ 1 (* 2 3))"),
        ("a - b - c", "(- (- a b) c)"),
        ("2 ** 3 ** 2", "(** 2 (** 3 2))"),
        ("-2 ** 2", "(- (** 2 2))"),
        ("-a * b", "(* (- a) b)"),
        ("~a & b", "(& (~ a) b)"),
        ("a | b ^ c & d << e + f", "(| a (^ b (& c (<< d (+ e f)))))"),
        ("a or b and c", "(or a (and b c))"),
        ("not a == b", "(not (compare a == b))"),
        ("not a and b", "(and (not a) b)"),
        ("x in 0..n", "(compare x in (.. 0 n))"),
        ("0..n+1", "(.. 0 (+ n 1))"),
        ("0..=9", "(..= 0 9)"),
        ("a < b <= c", "(compare a < b <= c)"),
        ("a == b != c", "(compare a == b != c)"),
        ("i not in xs", "(compare i not in xs)"),
        ("xs |> sum() == 10", "(compare (call sum xs) == 10)"),
        ("a + b |> f()", "(call f (+ a b))"),
        ("a |> f |> g(1, 2)", "(call g (call f a) 1 2)"),
        ("-f(x)?", "(- (? (call f x)))"),
        ("a.b.c(d)[e]", "(index (call (. (. a b) c) d) e)"),
        ("2 ** -1", "(** 2 -1)"),
        ("(1 + 2) * 3", "(* (+ 1 2) 3)"),
    ],
)
def test_precedence(source: str, tree: str) -> None:
    assert one(source) == tree


def test_operator_nodes_point_at_the_operator() -> None:
    [expr] = parse("a + b").body
    assert isinstance(expr, Binary)
    assert expr.span == Span(1, 3)


def test_ranges_do_not_chain() -> None:
    assert "without parentheses" in error("a..b..c").message


def test_pipe_needs_name_or_call() -> None:
    assert "name or a call" in error("x |> 1").message


def test_line_cant_end_with_binary_operator() -> None:
    err = error("let total = xs\nlet y = 1 +\n    2")
    assert "wrap the expression in parentheses" in err.message
    assert err.span == Span(2, 11)
    assert one("let total = (xs\n    |> filter(iseven)\n    |> sum())") == (
        "(let [total] (call sum (call filter xs iseven)))"
    )


# Literals


def test_literals() -> None:
    assert lines("42\n2.5\ntrue\nfalse\n()\n(1,)\n(1, 2,)\n[]\n[1, 2,]") == [
        "42", "2.5", "true", "false", "()", "(tuple 1)", "(tuple 1 2)", "(list)", "(list 1 2)",
    ]  # fmt: skip


def test_negative_literals_are_folded() -> None:
    [expr] = parse("-5").body
    assert expr == IntLit(-5, Span(1, 1))
    assert one("-9223372036854775808") == "-9223372036854775808"
    assert one("-(5)") == "(- 5)"
    assert "doesn't fit in i64" in error("9223372036854775808").message
    assert "doesn't fit in i64" in error("-9223372036854775809").message


def test_strings() -> None:
    assert one('"plain"') == '"plain"'
    assert one('""') == '""'
    assert one('"Hi, {{name}}!"') == '(str "Hi, " name "!")'
    assert one('"{{ greet("bob") }}"') == '(str (call greet "bob"))'


# Bindings and assignment


def test_let() -> None:
    assert one("let x = 5") == "(let [x] 5)"
    assert one("let mut names: [str] = []") == "(let [mut names: [str]] (list))"


def test_let_chain_makes_new_bindings() -> None:
    assert one("let y = mut x = z = 5") == "(let [y, mut x, z] 5)"
    assert one("let a: i64 = b: i64 = 1") == "(let [a: i64, b: i64] 1)"
    # parentheses make it an assignment instead
    assert one("let y = (x = 5)") == "(let [y] (= x 5))"
    assert one("let y = x == 5") == "(let [y] (compare x == 5))"


def test_let_with_a_pattern() -> None:
    assert one("let (a, b) = pair") == "(let (tuple a b) pair)"
    assert one("let (a, (b, _)) = f(x)") == "(let (tuple a (tuple b _)) (call f x))"
    assert one("let Point(x, y) = p") == "(let (Point x y) p)"
    assert one("let (a, b): (i64, i64) = p") == "(let (tuple a b): (i64, i64) p)"
    assert one("let mut (a, b) = p") == "(let mut (tuple a b) p)"
    assert one("let mut Point(x, y): Point = p") == "(let mut (Point x y): Point p)"
    assert one("let mut x = 1") == "(let [mut x] 1)"


def test_mut_names_in_patterns() -> None:
    assert one("let (mut a, b) = p") == "(let (tuple mut a b) p)"
    assert one("for (i, mut x) in xs\n    x") == "(for (tuple i mut x) xs (block x))"
    assert one("match o\n    case Some(mut x) => x") == "(match o (case (Some mut x) x))"
    message = "'mut' in a pattern must be followed by a name, like 'mut x'"
    assert error("let (mut _, b) = p").message == message
    assert error("let (mut Some(x), b) = p").message == message
    assert error("let (mut 1, b) = p").message == message


def test_tuple_items() -> None:
    assert one("t.0") == "(. t 0)"
    assert one("t.1.0") == "(. (. t 1) 0)"  # `1.0` is one token, a float
    assert one("t.0.x") == "(. (. t 0) x)"
    assert one("f().2[1]") == "(index (. (call f) 2) 1)"
    assert error("t.0x1").message == "expected a member name, found '0x1'"
    assert error("t.1_0").message == "expected a member name, found '1_0'"


def test_assignment_as_a_body_is_a_block() -> None:
    # A body that is an assignment has the value (), as a block's last line does.
    assert one("(mut xs: [i64]) -> () => xs += [1]") == "(lambda (mut xs: [i64]) -> () (block (+= xs (list 1))))"
    assert one("if c then x = 1 else x = 2") == "(if c (block (= x 1)) (block (= x 2)))"
    assert one("match c\n    case _ => x = 1") == "(match c (case _ (block (= x 1))))"
    assert one("for i in xs x += i") == "(for i xs (block (+= x i)))"
    assert one("let y = (x = 1)") == "(let [y] (= x 1))"  # a value, not a body


def test_index_with_several_items_is_a_tuple() -> None:
    assert one("a[i, j]") == "(index a (tuple i j))"
    assert one("a[1, 2, 3] += x") == "(+= (index a (tuple 1 2 3)) x)"
    assert one("a[(i, j)]") == "(index a (tuple i j))"
    assert one("a[i][j, k]") == "(index (index a i) (tuple j k))"


def test_assignment() -> None:
    assert one("x = y = 1") == "(= x (= y 1))"
    assert one("x += 1") == "(+= x 1)"
    assert one("xs[0] //= 2") == "(//= (index xs 0) 2)"
    assert one("x = a or b") == "(= x (or a b))"
    [expr] = parse("x = 1").body
    assert isinstance(expr, Assign) and isinstance(expr.target, Name)


def test_invalid_assignment_target() -> None:
    assert "can only assign" in error("f(x) = 1").message
    assert "can only assign" in error("a + b = 1").message


# Functions


def test_lambdas() -> None:
    assert one("(x: i64, y: i64,) -> i64 => x + y") == "(lambda (x: i64, y: i64) -> i64 (+ x y))"
    assert one("() -> () => print(1)") == "(lambda () -> () (call print 1))"
    assert one("let f = (x: i64) -> (i64) -> i64 => g") == "(let [f] (lambda (x: i64) -> (i64) -> i64 g))"


def test_untyped_lambdas_parse() -> None:
    assert one("x => x + 2") == "(lambda (x) (+ x 2))"
    assert one("(x, y) => x") == "(lambda (x, y) x)"
    assert one("let f = x => y => x") == "(let [f] (lambda (x) (lambda (y) x)))"


def test_lambda_body_extends_right() -> None:
    [expr] = parse("let f = (x: i64) -> i64 => x + 1").body
    assert isinstance(expr, Let) and isinstance(expr.value, Lambda)
    assert show(expr.value.body) == "(+ x 1)"


def test_lambda_block_body_with_where() -> None:
    source = "let f = (n: i64) -> i64 =>\n    where\n        n >= 0\n        return > n\n    let m = n\n    m + 1\n"
    assert one(source) == (
        "(let [f] (lambda (n: i64) -> i64 (where (compare n >= 0) (compare return > n)) (block (let [m] n) (+ m 1))))"
    )


def test_return_outside_where_is_an_exit() -> None:
    assert one("() -> i64 =>\n    return 1") == "(lambda () -> i64 (block (return 1)))"
    assert lines("return\nx") == ["(return)", "x"]


def test_where_only_starts_a_function_body() -> None:
    assert "first clause" in error("where\n    x").message
    assert "first clause" in error("() -> () =>\n    x\n    where\n        y").message


def test_function_body_after_where_is_required() -> None:
    assert "expected an expression" in error("() -> () =>\n    where\n        x\n").message


def test_bad_parameters() -> None:
    assert "lambda parameters" in error("(1) -> i64 => 1").message
    assert "only allowed on lambda parameters" in error("(x: i64)").message
    assert "before '=>'" in error("f(x) => 1").message


def test_calls() -> None:
    assert one("f()") == "(call f)"
    assert one("f(a, b,)") == "(call f a b)"
    assert one("f(a)(b)") == "(call (call f a) b)"
    assert one("f(\n    a,\n    b,\n)") == "(call f a b)"


# Control flow


def test_if() -> None:
    assert one("if a then b else c") == "(if a b c)"
    assert one("if a then b") == "(if a b)"
    assert one("if a then b else if c then d else e") == "(if a b (if c d e))"
    assert one("if a then\n    b\nelse\n    c") == "(if a (block b) (block c))"
    assert one("if a then b\nelse c") == "(if a b c)"
    assert one("if a then\n    b\nelse if c then\n    d") == "(if a (block b) (if c (block d)))"


def test_else_belongs_to_the_right_if() -> None:
    assert one("if a then\n    if b then x\nelse y") == "(if a (block (if b x)) y)"


def test_if_value_used_in_expression() -> None:
    assert one("let x = 1 + if a then 2 else 3") == "(let [x] (+ 1 (if a 2 3)))"


def test_match() -> None:
    source = """\
match r
    case Ok(v) if v > 1.0 => "big"
    case Ok(v) => "small"
    case Err(e)
        print(e)
        0
"""
    assert one(source) == (
        '(match r (case (Ok v) if (compare v > 1.0) "big") (case (Ok v) "small") '
        "(case (Err e) (block (call print e) 0)))"
    )


def test_guard_can_compare() -> None:
    assert one("match x\n    case n if n == 1 => n") == "(match x (case n if (compare n == 1) n))"


def test_match_needs_indented_cases() -> None:
    assert "indented list of cases" in error("match x\ncase 1 => 2").message
    assert "expected 'case'" in error("match x\n    1").message


def test_loops() -> None:
    assert one("for x in xs\n    print(x)") == "(for x xs (block (call print x)))"
    assert one("for (i, x) in pairs print(x)") == "(for (tuple i x) pairs (call print x))"
    assert one("while i < 3\n    i += 1") == "(while (compare i < 3) (block (+= i 1)))"
    assert one("loop break") == "(loop (break))"
    assert one("for x in mut g\n    print(x)") == "(for x (mut g) (block (call print x)))"


def test_yield() -> None:
    assert one("let g = () =>\n    yield 1 + 2") == "(let [g] (lambda () (block (yield (+ 1 2)))))"
    assert error("yield").message == "expected an expression, found end of line"


def test_labels_break_continue() -> None:
    source = "outer: for row in grid\n    for x in row\n        if x then continue outer\n        break outer 42"
    assert one(source) == (
        "(for outer: row grid (block (for x row (block (if x (continue outer:)) (break outer: 42)))))"
    )
    assert one("l: while x\n    break l") == "(while l: x (block (break l:)))"
    assert one("l: loop break l") == "(loop l: (break l:))"


def test_label_names_only_count_inside_their_loop() -> None:
    assert one("n: loop\n    loop break n") == "(loop n: (block (loop (break n:))))"
    # outside the labeled loop, `n` is the break's value
    assert lines("n: loop break\nloop break n") == ["(loop n: (break))", "(loop (break n))"]
    # parentheses make a value even when the name is a label
    assert one("n: loop break (n)") == "(loop n: (break n))"


def test_labels_dont_reach_into_lambdas() -> None:
    assert one("outer: loop\n    () -> () => break outer") == "(loop outer: (block (lambda () -> () (break outer))))"


def test_labeled_loops_as_values() -> None:
    assert one("let found = outer: loop break outer 1") == "(let [found] (loop outer: (break outer: 1)))"
    assert one("f(outer: loop break outer)") == "(call f (loop outer: (break outer:)))"
    # a name and a colon not followed by a loop is still a let chain target
    assert one("let a = b: i64 = 1") == "(let [a, b: i64] 1)"


def test_single_quoted_strings() -> None:
    assert one("'Hi, {{name}}!'") == '(str "Hi, " name "!")'
    assert one("""'{{ f("x") }}'""") == '(str (call f "x"))'
    assert one("'''two\nlines'''") == r'"two\nlines"'
    assert one("match s\n    case 'x' => 1") == '(match s (case "x" 1))'


def test_break_value() -> None:
    assert one("let found = loop\n    if done() then break n * 2") == (
        "(let [found] (loop (block (if (call done) (break (* n 2))))))"
    )


def test_assert() -> None:
    assert one("assert 0 <= i < 10") == "(assert (compare 0 <= i < 10))"


# Blocks and layout


def test_expression_ends_at_its_block() -> None:
    # The `+ 1` line can't continue the `loop` expression.
    assert "expected an expression" in error("let x = loop\n    break 1\n+ 1").message


def test_nested_blocks() -> None:
    source = "let f = () -> () =>\n    for x in xs\n        if x then\n            print(x)\n    done()\nnext"
    assert lines(source) == [
        "(let [f] (lambda () -> () (block (for x xs (block (if x (block (call print x))))) (call done))))",
        "next",
    ]


def test_unexpected_indent() -> None:
    err = error("a\n    b")
    assert err.message == "expected an expression, found an indented line"


def test_top_level_expressions_end_lines() -> None:
    assert "expected end of line" in error("a b").message


# Patterns


@pytest.mark.parametrize(
    ("pattern", "tree"),
    [
        ("_", "_"),
        ("n", "n"),
        ("None", "None"),
        ("0", "0"),
        ("-1", "-1"),
        ("-2.5", "-2.5"),
        ('"x"', '"x"'),
        ("true", "true"),
        ("0..10", "0..10"),
        ("0..=9", "0..=9"),
        ("-5..-1", "-5..-1"),
        ("0 | 1 | 2", "(| 0 1 2)"),
        ("Ok(v)", "(Ok v)"),
        ("Rect(w, _)", "(Rect w _)"),
        ("(a, b)", "(tuple a b)"),
        ("(a,)", "(tuple a)"),
        ("Some((x, 0))", "(Some (tuple x 0))"),
    ],
)
def test_patterns(pattern: str, tree: str) -> None:
    assert one(f"match x\n    case {pattern} => 1") == f"(match x (case {tree} 1))"


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("(a)", "needs a comma"),
        ('"{{x}}"', "interpolation"),
        ("-x", "a number after '-'"),
        ("[a]", "expected a pattern"),
    ],
)
def test_bad_patterns(pattern: str, message: str) -> None:
    assert message in error(f"match x\n    case {pattern} => 1").message


# Types


@pytest.mark.parametrize(
    "type_",
    ["i64", "[str]", "[[i64]]", "()", "(i64, str)", "(i64,)", "(i64, i64) -> i64", "() -> ()", "Result[f64, Error]"],
)
def test_types(type_: str) -> None:
    assert one(f"let x: {type_} = y") == f"(let [x: {type_}] y)"


def test_function_type_returning_function() -> None:
    assert one("let f: (i64) -> (i64) -> i64 = g") == "(let [f: (i64) -> (i64) -> i64] g)"


def test_bad_types() -> None:
    assert "trailing comma" in error("let x: (i64) = y").message
    assert "expected a type" in error("let x: 1 = y").message


# Enums


def test_enum() -> None:
    source = "enum Shape\n    Circle(f64)\n    Rect(f64, f64)\n    Empty\nx"
    assert lines(source) == ["(enum Shape (Circle f64) (Rect f64 f64) Empty)", "x"]
    assert one("enum Optional[T]\n    Some(T)\n    None") == "(enum Optional[T] (Some T) None)"


# Errors


def test_reserved_keywords() -> None:
    assert "reserved" in error("comptime x").message


def test_error_spans() -> None:
    err = error("let x = 1\nlet = 2")
    assert err.span == Span(2, 5)
    assert err.message == "expected a name, found '='"


def test_unclosed_paren_reports_its_position() -> None:
    assert error("f(1,\n2").span == Span(1, 2)


def test_pipe_in_call_node() -> None:
    [expr] = parse("x |> f(1)").body
    assert isinstance(expr, Call) and expr.args[0] == Name("x", Span(1, 1))


def test_mut_parameters_and_arguments() -> None:
    assert one("let f = (mut xs: [i64], x: i64) -> () => push(mut xs, x)") == (
        "(let [f] (lambda (mut xs: [i64], x: i64) -> () (call push (mut xs) x)))"
    )
    assert one("f(mut grid[i][j], 1)") == "(call f (mut (index (index grid i) j)) 1)"
    assert one("let f: (mut [i64], i64) -> () = g") == "(let [f: (mut [i64], i64) -> ()] g)"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("(mut x: i64)", "'mut x' only allowed on lambda parameters"),
        ("let t: (mut i64, str) = x", "only a function type's parameters can be marked 'mut'"),
        ("(mut 1) => 1", "expected a parameter name, found '1'"),
    ],
)
def test_mut_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_structs() -> None:
    assert one("struct Point\n    x: f64\n    y: f64") == "(struct Point x: f64 y: f64)"
    assert one("struct Pair[T]\n    a: T\n    b: T") == "(struct Pair[T] a: T b: T)"
    source = "nocopy struct File\n    name: str\n    finalize = (f: File) -> () => ()"
    assert one(source) == "(nocopy struct File name: str (finalize (lambda (f: File) -> () ())))"
    assert one("p.x = 1") == "(= (. p x) 1)"
    assert error("struct P\n    x: i64\n    x: i64").message == "'x' is already a field of P"


def test_methods_and_constraints() -> None:
    source = "struct Set[T: Eq]\n    items: [T]\n    has = (s: Set[T], x: T) -> bool => x in s.items"
    assert (
        one(source)
        == "(struct Set[T: Eq] items: [T] (has (lambda (s: Set[T], x: T) -> bool (compare x in (. s items)))))"
    )
    source = "enum Shape\n    Circle(f64)\n    area = (s: Shape) -> f64 => 1.0\n    Empty"
    assert one(source) == "(enum Shape (Circle f64) Empty (area (lambda (s: Shape) -> f64 1.0)))"
    source = "constraint Stack\n    push: (mut Self, i64) -> ()\n    size: (Self) -> i64"
    assert one(source) == "(constraint Stack push: (mut Self, i64) -> () size: (Self) -> i64)"
    source = (
        "constraint Lookup\n    get: (Self, str) -> i64\n"
        "struct MyType\n    value: i64\n    implements Lookup\n"
        "        get = (m: MyType, s: str) -> i64 => m.value"
    )
    assert lines(source)[1] == (
        "(struct MyType value: i64 (implements Lookup (get (lambda (m: MyType, s: str) -> i64 (. m value)))))"
    )
    assert one("p.area()") == "(call (. p area))"
    # `mut` before a method call marks the value it is called on, wherever the call is.
    assert one("mut s.items[0].push(1, 2)") == "(call (. (mut (index (. s items) 0)) push) 1 2)"
    assert one("mut s.pop() + 1") == "(+ (call (. (mut s) pop)) 1)"
    assert one("print(mut s.pop())") == "(call print (call (. (mut s) pop)))"
    assert one("f(mut xs, mut s.pop())") == "(call f (mut xs) (call (. (mut s) pop)))"
    message = "'mut' here must be followed by a method call that changes its value, like 'mut s.push(3)'"
    assert error("mut x").message == message
    assert error("struct P\n    x: i64\n    f = 1\n    f = 2").message == "there is already a method named 'f'"
    assert error("enum E\n    f = (e: E) -> i64 => 1").message == "enum E needs at least one variant"


def test_generic_functions() -> None:
    assert one("let first[T] = (xs: [T]) -> T => xs[0]") == "(let [first[T]] (lambda (xs: [T]) -> T (index xs 0)))"


def test_quotes_and_splices() -> None:
    assert one("quote $x * $(f(x))") == "(quote (* ($ x) ($ (call f x))))"
    assert (
        one("let c: Code[i64] = quote\n    let y = 1\n    y") == "(let [c: Code[i64]] (quote (block (let [y] 1) y)))"
    )
    assert one("$\n    quote 1") == "($ (block (quote 1)))"
    # `$` takes only a name or parentheses: `$f(x)` calls the spliced `f`.
    assert one("$f(x)") == "(call ($ f) x)"
    assert error("$1").message == "expected a name or '(' after '$', found '1'"


def test_types_as_values_and_splices() -> None:
    assert one("type [Pair[i64]]") == "(type [Pair[i64]])"
    assert one("quote (a: $t) -> $u => a.$f") == "(quote (lambda (a: $(t)) -> $(u) (. a ($ f))))"
    assert one("quote $v.$(names[0])") == "(quote (. ($ v) ($ (index names 0))))"


def test_constrained_type_parameters() -> None:
    assert one("let max[T: Ord, U: Add + Eq] = (a: T) -> T => a") == (
        "(let [max[T: Ord, U: Add + Eq]] (lambda (a: T) -> T a))"
    )


def test_array_shapes() -> None:
    # Dimensions follow a `;`: sizes, size names and `_` (TODO.md, "arrays typed by shape").
    assert one("let a: Array[f64; N, 3, _] = b") == "(let [a: Array[f64; N, 3, _]] b)"
    assert one("let a: Array[f64] = b") == "(let [a: Array[f64]] b)"
    assert one("let a: Array[f64,; N,] = b") == "(let [a: Array[f64; N]] b)"
    assert error("let a: Array[f64;] = b").message == "an array needs at least one dimension after ';'"
    assert error("let a: Array[; N] = b").message == "expected an item type before ';'"
    assert error("let a: Array[f64; 1.5] = b").message == "expected a size, a size name or '_', found '1.5'"


def test_size_parameters() -> None:
    assert one("let mm[T: Num; N, K] = (a: Array[T; N, K]) -> T => a") == (
        "(let [mm[T: Num; N, K]] (lambda (a: Array[T; N, K]) -> T a))"
    )
    assert one("let mean[; N] = (xs: Array[f64; N]) -> f64 => total(xs)") == (
        "(let [mean[; N]] (lambda (xs: Array[f64; N]) -> f64 (call total xs)))"
    )
    assert error("let f[; _] = (x: i64) -> i64 => x").message == "a size parameter needs a name"
    assert error("let f[;] = (x: i64) -> i64 => x").message == "expected a size parameter after ';'"
    assert error("let f[] = (x: i64) -> i64 => x").message == "expected a type parameter"


def partial(source: str) -> tuple[list[str], list[str]]:
    """What `parse_partial` makes of `source`: each top-level expression, and the errors."""
    program, errors = parse_partial(tokenize(source), source)
    return [show(expr) for expr in program.body], [str(error) for error in errors]


def test_partial_parse_skips_lines_with_errors() -> None:
    source = "let x = 1 +\nlet y = [1 2]\nprint(3)\n"
    assert partial(source) == (
        ["()", "()", "(call print 3)"],
        ["1:11: a line can't end with '+'; wrap the expression in parentheses", "2:12: expected ']', found '2'"],
    )


def test_partial_parse_recovers_in_blocks() -> None:
    source = """let f = (n: i64) -> i64 =>
    let y = n *
    if n > 0 then
        print(n, , 2)
        n
    else
        0
print(f(2))
"""
    body, errors = partial(source)
    assert body == [
        "(let [f] (lambda (n: i64) -> i64 (block () (if (compare n > 0) (block () n) (block 0)))))",
        "(call print (call f 2))",
    ]
    assert errors == [
        "2:15: a line can't end with '*'; wrap the expression in parentheses",
        "4:18: expected an expression, found ','",
    ]


def test_partial_parse_skips_a_line_with_its_blocks() -> None:
    source = "match 1\n    cose 2 => 3\nloop\n    break\nprint(4)\n"
    assert partial(source) == (
        ["()", "(loop (block (break)))", "(call print 4)"],
        ["2:5: expected 'case', found 'cose'"],
    )


def test_parse_still_stops_at_the_first_error() -> None:
    assert (
        str(error("let x = 1 +\nlet y = [1 2]\n"))
        == "1:11: a line can't end with '+'; wrap the expression in parentheses"
    )


def test_map_and_set_literals() -> None:
    assert one('{1: "a", 2: "b",}') == '(map (1 "a") (2 "b"))'
    assert one("{}") == "(map)"
    assert one("{x, y + 1}") == "(set x (+ y 1))"
    assert one("{x,}") == "(set x)"
    assert one("{\n    1: [2],\n    3: [],\n}") == "(map (1 (list 2)) (3 (list)))"
    assert one("{(0, 1): {2}}") == "(map ((tuple 0 1) (set 2)))"
    assert one("{/}") == "(set)"
    assert one("{ / }") == "(set)"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{1: 2, 3}", "1:9: expected ':', found '}'"),
        ("{1, 2: 3}", "1:6: expected '}', found ':'"),
        ("{1: }", "1:5: expected an expression, found '}'"),
        ("{1", "1:1: '{' is never closed"),
    ],
)
def test_bad_map_and_set_literals(source: str, message: str) -> None:
    assert str(error(source)) == message

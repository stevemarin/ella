# Ella

## Parsing expressions

`ella.parser.parse` parses a source string into an expression tree. The parser
uses the precedences and associativity declared in `ella.op`.

```python
from ella.parser import parse

expression = parse("total + tax * 2")
```

Expression nodes are `LiteralExpr`, `NameExpr`, `UnaryExpr`, `BinaryExpr`,
and `CallExpr`. Parentheses control grouping, and calls accept comma-separated
arguments.

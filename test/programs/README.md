# Test programs

Each `NAME.ella` here is a complete, type-correct Ella program, run by
[test_programs.py](../test_programs.py) with the AST interpreter, the IR
interpreter, the IR interpreter on optimized IR, and as native code (from
plain and optimized IR). Every interpreter and backend must give the same results, so
later stages reuse these files.

* `NAME.out` is exactly what the program prints.
* `NAME.err`, if present, means the program must stop with that error, as
  `line:column: message`, after printing `NAME.out`.

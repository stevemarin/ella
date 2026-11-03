# Test programs

Each `NAME.ella` here is a complete, type-correct Ella program, run by
[test_programs.py](../test_programs.py) with the AST interpreter, the IR
interpreter, the IR interpreter on optimized IR, and as native code (from
plain and optimized IR). Every interpreter and backend must give the same results, so
later stages reuse these files.

* `NAME.out` is exactly what the program prints.
* `NAME.err`, if present, means the program must stop with that error, as
  `line:column: message`, after printing `NAME.out`, then its causes and
  its stack trace (`caused by ...`, `called at ...`), as the CLI
  prints them.
* `NAME.in`, if present, is the program's standard input, and `NAME.args`
  its command-line arguments, one per line.
* `NAME.stderr`, if present, is exactly what the program prints to
  standard error (with `eprint`); without it, the program must print
  nothing there. A run-time error isn't part of it.
* `NAME.status`, if present, is the exit status the program must give (its
  `main` returns it); without it, the status must be 0.

A program of several modules is a directory, `NAME/`, whose entry file is
`NAME/main.ella`, with its modules beside it. Its other files are named
after the entry file: `NAME/main.out`, `NAME/main.err`, and so on. An
error in a module gives its file, from the directory: `geo/poly.ella:3:5:
message`.

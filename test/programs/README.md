# Test programs

Each `NAME.ella` here is a complete Ella program, run by
[test_programs.py](../test_programs.py). Every interpreter and backend should
give the same results, so later stages reuse these files.

* `NAME.out` is exactly what the program prints.
* `NAME.err`, if present, means the program must stop with that error, as
  `line:column: message`, after printing `NAME.out`.

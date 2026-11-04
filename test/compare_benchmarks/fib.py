def fib(n: int, a: int, b: int) -> int:
    while n:
        n, a, b = n - 1, b, (a + b) % 1_000_000_007
    return a


print(fib(10_000, 0, 1))

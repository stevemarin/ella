def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def step(n: int) -> int:
    return n // 2 if n % 2 == 0 else 3 * n + 1


total = sum(gcd(i, j) for i in range(1, 181) for j in range(1, 181))
longest = 0
for start in range(1, 20001):
    n = start
    steps = 0
    while n != 1:
        n = step(n)
        steps += 1
    longest = max(longest, steps)
print(total, longest)

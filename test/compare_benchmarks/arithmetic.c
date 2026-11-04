#include <stdio.h>

static long long gcd(long long a, long long b) {
    while (b) {
        long long t = a % b;
        a = b;
        b = t;
    }
    return a;
}

static long long step(long long n) {
    return n % 2 == 0 ? n / 2 : 3 * n + 1;
}

int main(void) {
    long long total = 0;
    for (long long i = 1; i < 181; ++i)
        for (long long j = 1; j < 181; ++j)
            total += gcd(i, j);
    long long longest = 0;
    for (long long start = 1; start < 20001; ++start) {
        long long n = start, steps = 0;
        while (n != 1) {
            n = step(n);
            ++steps;
        }
        if (steps > longest) longest = steps;
    }
    printf("%lld %lld\n", total, longest);
    return 0;
}

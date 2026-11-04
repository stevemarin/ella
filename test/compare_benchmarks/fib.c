#include <stdio.h>

static long long fib(long long n, long long a, long long b) {
    while (n) {
        long long next = (a + b) % 1000000007;
        n -= 1;
        a = b;
        b = next;
    }
    return a;
}

int main(void) {
    printf("%lld\n", fib(10000, 0, 1));
    return 0;
}

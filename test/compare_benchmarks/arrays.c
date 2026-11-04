#include <stdio.h>
#include <stdlib.h>

int main(void) {
    const int n = 2048, steps = 80;
    const double dt = 0.01;
    double *target = malloc((size_t)n * sizeof(double));
    double *pos = calloc((size_t)n, sizeof(double));
    double *vel = calloc((size_t)n, sizeof(double));
    if (!target || !pos || !vel) return 1;
    for (int i = 0; i < n; ++i) target[i] = (i % 97) * 0.02 - 1.0;
    for (int s = 0; s < steps; ++s)
        for (int i = 0; i < n; ++i) {
            double force = (target[i] - pos[i]) * 4.0 - vel[i] * 0.5;
            vel[i] += force * dt;
            pos[i] += vel[i] * dt;
        }
    double sum = 0.0, norm = 0.0;
    for (int i = 0; i < n; ++i) {
        sum += pos[i];
        norm += vel[i] * vel[i];
    }
    printf("%lld %lld\n", (long long)(sum * 1000000000.0), (long long)(norm * 1000000000.0));
    free(target);
    free(pos);
    free(vel);
    return 0;
}

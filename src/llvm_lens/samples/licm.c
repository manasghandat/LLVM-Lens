/* Loop-invariant code motion, in one file.
 *
 * `weighted_sum` recomputes `scale * bias` on every iteration even though
 * neither value changes, so the report's diff shows the multiply lifted out of
 * the loop into a register the body then just reads.
 *
 * `dependent_sum` looks almost the same, but its factor is a function of `i`,
 * so there is nothing to lift. Same loop, same shape of work -- the whole
 * difference is in what the optimizer can prove. */

#include <stdint.h>
#include <stdio.h>

#define N 4096

int64_t weighted_sum(const int32_t *a, int n, int32_t scale, int32_t bias)
{
    int64_t total = 0;

    for (int i = 0; i < n; i++)
        total += (int64_t)a[i] * (scale * bias);

    return total;
}

int64_t dependent_sum(const int32_t *a, int n, int32_t scale)
{
    int64_t total = 0;

    for (int i = 0; i < n; i++)
        total += (int64_t)a[i] * (scale * i);

    return total;
}

int main(void)
{
    static int32_t a[N];
    int64_t total = 0;

    for (int i = 0; i < N; i++)
        a[i] = i * 31 - 11;

    total += weighted_sum(a, N, 7, 13);
    total += dependent_sum(a, N, 5);

    printf("total = %lld\n", (long long)total);
    return 0;
}

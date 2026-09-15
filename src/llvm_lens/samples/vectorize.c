/* Vectorization, in one file.
 *
 * `scale_add` reads two arrays and writes a third with no aliasing between
 * them and no dependency across iterations, so the loop vectorizer rewrites it
 * into wide loads and multiplies: the report's before/after diff shows the
 * scalar loop replaced outright. `prefix_sum` carries each result into the
 * next iteration, which cannot be widened at all, so it stays scalar in the
 * same report -- the contrast is the point.
 *
 * Both are shapes real kernels are built from, not benchmarks. */

#include <stdint.h>
#include <stdio.h>

#define N 4096

/* `restrict` tells the vectorizer the arrays do not overlap, so the loop
   widens with no runtime alias check in front of it. */
void scale_add(const int32_t *restrict a, const int32_t *restrict b,
               int32_t *restrict out, int n, int32_t k)
{
    for (int i = 0; i < n; i++)
        out[i] = a[i] * k + b[i];
}

/* Every iteration needs the one before it: a loop-carried dependency. */
void prefix_sum(int32_t *a, int n)
{
    for (int i = 1; i < n; i++)
        a[i] += a[i - 1];
}

int main(void)
{
    static int32_t a[N], b[N], out[N];
    int64_t sum = 0;

    for (int i = 0; i < N; i++) {
        a[i] = i * 7 - 3;
        b[i] = i ^ 0x5a5a;
    }

    scale_add(a, b, out, N, 3);
    prefix_sum(out, N);

    for (int i = 0; i < N; i++)
        sum += out[i];

    printf("sum = %lld\n", (long long)sum);
    return 0;
}

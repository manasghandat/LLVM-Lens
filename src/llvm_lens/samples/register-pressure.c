/* Register pressure, in one file.
 *
 * x86-64 has 16 general-purpose registers and the allocator gets to use 15 of
 * them (rsp is the stack pointer). `mix16` keeps 16 lanes of state live across
 * every round, so the allocator physically cannot hold them all and has to
 * park some in stack slots and fetch them back: a spill and a reload. `mix4`
 * does the same arithmetic over 4 lanes, which fits, so it spills nothing.
 *
 * Both are the ARX mixing core shared by ChaCha and BLAKE2 -- real code, not a
 * pressure benchmark -- so the spills show up in a function worth reading. */

#include <stdint.h>
#include <stdio.h>

#define ROTR(x, n) (((x) >> (n)) | ((x) << (64 - (n))))

/* One ChaCha/BLAKE2-style quarter round over four lanes. */
#define QR(a, b, c, d)         \
    do {                       \
        a += b;                \
        d = ROTR(d ^ a, 32);   \
        c += d;                \
        b = ROTR(b ^ c, 24);   \
        a += b;                \
        d = ROTR(d ^ a, 16);   \
        c += d;                \
        b = ROTR(b ^ c, 63);   \
    } while (0)

/* 16 lanes live across the whole loop: more than the machine has registers. */
uint64_t mix16(const uint64_t seed[16], unsigned rounds)
{
    uint64_t s0 = seed[0], s1 = seed[1], s2 = seed[2], s3 = seed[3];
    uint64_t s4 = seed[4], s5 = seed[5], s6 = seed[6], s7 = seed[7];
    uint64_t s8 = seed[8], s9 = seed[9], s10 = seed[10], s11 = seed[11];
    uint64_t s12 = seed[12], s13 = seed[13], s14 = seed[14], s15 = seed[15];

    for (unsigned r = 0; r < rounds; r++) {
        /* columns */
        QR(s0, s4, s8, s12);
        QR(s1, s5, s9, s13);
        QR(s2, s6, s10, s14);
        QR(s3, s7, s11, s15);
        /* diagonals: every lane meets a lane it did not meet above, which is
           what keeps all sixteen live at once instead of in four groups. */
        QR(s0, s5, s10, s15);
        QR(s1, s6, s11, s12);
        QR(s2, s7, s8, s13);
        QR(s3, s4, s9, s14);
    }

    return s0 ^ s1 ^ s2 ^ s3 ^ s4 ^ s5 ^ s6 ^ s7
         ^ s8 ^ s9 ^ s10 ^ s11 ^ s12 ^ s13 ^ s14 ^ s15;
}

/* The same arithmetic on 4 lanes: fits in registers, so no stack traffic. */
uint64_t mix4(const uint64_t seed[4], unsigned rounds)
{
    uint64_t a = seed[0], b = seed[1], c = seed[2], d = seed[3];

    for (unsigned r = 0; r < rounds; r++)
        QR(a, b, c, d);

    return a ^ b ^ c ^ d;
}

int main(void)
{
    uint64_t seed[16];
    for (int i = 0; i < 16; i++)
        seed[i] = 0x9e3779b97f4a7c15ULL * (uint64_t)(i + 1);

    printf("mix16 = %016llx\n", (unsigned long long)mix16(seed, 12));
    printf("mix4  = %016llx\n", (unsigned long long)mix4(seed, 12));
    return 0;
}

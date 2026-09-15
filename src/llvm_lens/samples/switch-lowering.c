/* Switch lowering, in one file.
 *
 * `region_of` is a dense switch -- sixteen consecutive cases, nothing missing
 * -- so LLVM emits a jump table and the Machine IR view shows the indirect
 * branch that replaces a chain of comparisons. `kind_of` has the same number
 * of cases spread over a range of thousands, where a table would be nearly all
 * holes, so it is lowered to a tree of comparisons instead.
 *
 * One language construct, two completely different code shapes: that is the
 * thing worth seeing in the report. */

#include <stdint.h>
#include <stdio.h>

/* Dense: a table indexed by the value, one indirect branch.  The results are
   deliberately not a function of the case, or the switch folds to arithmetic. */
int region_of(uint32_t op)
{
    switch (op) {
    case 0:  return 41;
    case 1:  return 7;
    case 2:  return 93;
    case 3:  return 12;
    case 4:  return 68;
    case 5:  return 3;
    case 6:  return 77;
    case 7:  return 25;
    case 8:  return 50;
    case 9:  return 88;
    case 10: return 16;
    case 11: return 61;
    case 12: return 34;
    case 13: return 99;
    case 14: return 5;
    case 15: return 72;
    default: return -1;
    }
}

/* Sparse: the same count over a range where a table would be all holes. */
int kind_of(uint32_t op)
{
    switch (op) {
    case 0:     return 41;
    case 1000:  return 7;
    case 2000:  return 93;
    case 3000:  return 12;
    case 4000:  return 68;
    case 5000:  return 3;
    case 6000:  return 77;
    case 7000:  return 25;
    case 8000:  return 50;
    case 9000:  return 88;
    case 10000: return 16;
    case 11000: return 61;
    case 12000: return 34;
    case 13000: return 99;
    case 14000: return 5;
    case 15000: return 72;
    default:    return -1;
    }
}

int main(void)
{
    int total = 0;

    for (uint32_t i = 0; i < 16; i++)
        total += region_of(i);
    for (uint32_t i = 0; i < 16000; i += 997)
        total += kind_of(i);

    printf("total = %d\n", total);
    return 0;
}

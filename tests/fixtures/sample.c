// Sample input for LLVM-Lens tests: two functions, a loop, and a call edge.
static int square(int x) {
    return x * x;
}

int main(int argc, char **argv) {
    int acc = 0;
    for (int i = 0; i < argc; ++i)
        acc += square(i);
    return acc;
}

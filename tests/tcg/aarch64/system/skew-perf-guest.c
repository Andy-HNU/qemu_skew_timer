/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Fixed useful work: CPU0 emits START; the last completed CPU emits DONE. */
typedef unsigned long u64;
extern u64 compute(u64);
extern void marker(u64), secondary_start(void);
#define LOAD(p) __atomic_load_n(&(p), __ATOMIC_ACQUIRE)
#define STORE(p,v) __atomic_store_n(&(p),(v),__ATOMIC_RELEASE)
struct slot { u64 ready, done, result, epoch; char pad[32]; };
static struct slot slots[CPUS] __attribute__((aligned(64)));
static u64 go, phase, finished;
static void finish(u64 code)
{
    u64 args[] = {0x20026, code};
    register u64 x0 asm("x0") = 0x20;
    register void *x1 asm("x1") = args;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    for (;;) asm volatile("wfi");
}
/* Independent O(log n) Fibonacci reference, modulo 2^64. */
static u64 fib(u64 n)
{
    u64 a=0,b=1;
    for (int i=63; i>=0; i--) {
        u64 c=a*(2*b-a), d=a*a+b*b;
        if ((n>>i)&1) { a=d; b=c+d; } else { a=c; b=d; }
    }
    return a;
}
static u64 iterations(unsigned id)
{
    if (SCENARIO == 1) return WORK;
    if (SCENARIO == 3) return WORK/(CPUS+1)*(id==CPUS-1 ? 2 : 1);
    return WORK/CPUS;
}
#if SCENARIO >= 4
#include "skew-perf-workloads.h"
#endif
void guest_main(u64 id)
{
    if (!id) {
        for (u64 i=1; i<CPUS; i++) {
            register u64 x0 asm("x0")=0xc4000003;
            register u64 x1 asm("x1")=((i / AFFINITY_SIZE) << 8) |
                                      (i % AFFINITY_SIZE);
            register u64 x2 asm("x2")=(u64)secondary_start;
            register u64 x3 asm("x3")=0;
            asm volatile("hvc #0" : "+r"(x0) : "r"(x1),"r"(x2),"r"(x3) : "memory");
            if (x0) finish(1);
        }
    }
#if SCENARIO >= 4
    workload_init(id);
#endif
    u64 chunks=SCENARIO==2 ? 128 : 1;
    u64 n=iterations(id)/chunks, sum=0;
    if (!n) finish(2);
    STORE(slots[id].ready,1);
    if (!id) {
        for (unsigned i=1;i<CPUS;i++) while (!LOAD(slots[i].ready)) {}
        marker(1);
        STORE(go,1);
    } else { while (!LOAD(go)) {} }
#if SCENARIO >= 4
    run_workload(id);
#else
    for (u64 k=1;k<=chunks;k++) {
        sum+=compute(n);
        if (SCENARIO==2) {
            STORE(slots[id].epoch,k);
            if (!id) {
                for(unsigned i=1;i<CPUS;i++) while(LOAD(slots[i].epoch)<k) {}
                STORE(phase,k);
            } else { while(LOAD(phase)<k) {} }
        }
    }
    /* 每核独立校验后加入完成计数，避免 CPU0 收尾自旋污染计算成绩。 */
    if (sum != chunks*fib(30*n+1)) finish(3);
#endif
    if (__atomic_add_fetch(&finished,1,__ATOMIC_ACQ_REL)==CPUS) {
        marker(2);
#if SCENARIO >= 4
        workload_report();
#endif
        finish(0);
    }
    for (;;) asm volatile("wfi");
}

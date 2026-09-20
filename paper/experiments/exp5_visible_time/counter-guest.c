/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Fixed-count bare-metal counter test; no virtual-time-dependent termination. */
typedef unsigned long u64;
#ifndef NREADS
#define NREADS 8000
#endif
#ifndef GAP
#define GAP 64
#endif
#ifndef CROSS
#define CROSS 0
#endif
#ifndef BURST
#define BURST 0
#endif
volatile u64 irq_seen, irq_time, irq_id; /* existing vector table linkage */
extern void secondary_start(void);
static u64 ready, turn, finished, previous, failures, repeated, total;
static inline u64 load(u64 *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
static inline void store(u64 *p, u64 x) { __atomic_store_n(p, x, __ATOMIC_RELEASE); }
static u64 counter(void)
{
    u64 x;
    asm volatile("isb; mrs %0, cntvct_el0" : "=r"(x) :: "memory");
    return x;
}
static u64 semi(u64 op, void *arg)
{
    register u64 x0 asm("x0") = op;
    register void *x1 asm("x1") = arg;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    return x0;
}
static void text(const char *s) { semi(4, (void *)s); }
static void num(u64 x)
{
    char s[24], *p=s+23;
    *p=0;
    do { *--p='0'+x%10; x/=10; } while(x);
    text(p);
}
static void run(int cpu)
{
    for (int i=0; i<NREADS; i++) {
        if (CROSS) {
            while (load(&turn)!=(u64)cpu) { asm volatile("yield"); }
        }
        int gap = BURST && (i/128)%2 ? GAP*16 : GAP;
        for (int j=0; j<gap; j++) { asm volatile("nop"); }
        u64 now=counter();
        if (total) {
            failures += now<previous;
            repeated += now==previous;
        }
        previous=now;
        total++;
        if (CROSS) { store(&turn,1-cpu); }
    }
}
void guest_main(u64 cpu)
{
    if (cpu) {
        store(&ready,1);
        run(1);
        store(&finished,1);
        for (;;) { asm volatile("wfi"); }
    }
    if (CROSS) {
        register u64 x0 asm("x0")=0xc4000003;
        register u64 x1 asm("x1")=1;
        register u64 x2 asm("x2")=(u64)secondary_start;
        register u64 x3 asm("x3")=0;
        asm volatile("hvc #0" : "+r"(x0) : "r"(x1),"r"(x2),"r"(x3) : "memory");
        if (x0) {
            u64 args[]={0x20026,2}; semi(0x20,args);
        }
        while (!load(&ready)) { asm volatile("yield"); }
    }
    run(0);
    if (CROSS) { while(!load(&finished)) { asm volatile("yield"); } }
    u64 freq;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    text("EXP5_GUEST reads="); num(total);
    text(" backwards="); num(failures);
    text(" repeats="); num(repeated);
    text(" frequency="); num(freq); text("\n");
    u64 args[]={0x20026,failures!=0};
    semi(0x20,args);
    for (;;) { asm volatile("wfi"); }
}

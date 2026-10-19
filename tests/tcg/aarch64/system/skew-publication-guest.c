/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Fixed work; success requires every core's checksum. */
typedef unsigned long u64;
/* Bare-metal entry points provided by skew-perf-boot.S. */
extern u64 compute(u64);
extern void marker(u64), secondary_start(void);

struct slot {
    u64 ready;
    u64 done;
    u64 result;
    unsigned char pad[40];
};
static struct slot slots[CPUS] __attribute__((aligned(64)));
static u64 go;
static u64 finished;

static void fail(u64 code)
{
    u64 args[] = {0x20026, code};
    register u64 x0 asm("x0") = 0x20;
    register void *x1 asm("x1") = args;

    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    for (;;) {
        asm volatile("wfi");
    }
}

/* Independent fast-doubling reference, with arithmetic modulo 2^64. */
static u64 fibonacci(u64 n)
{
    u64 a = 0, b = 1;

    for (int i = 63; i >= 0; i--) {
        u64 c = a * (2 * b - a);
        u64 d = a * a + b * b;

        if ((n >> i) & 1) {
            a = d;
            b = c + d;
        } else {
            a = c;
            b = d;
        }
    }
    return a;
}

void guest_main(u64 id)
{
    u64 checksum = 0;
    u64 counter_sink = 0;

    if (!id) {
        for (u64 i = 1; i < CPUS; i++) {
            register u64 x0 asm("x0") = 0xc4000003;
            register u64 x1 asm("x1") = ((i / AFFINITY_SIZE) << 8) |
                                        (i % AFFINITY_SIZE);
            register u64 x2 asm("x2") = (u64)secondary_start;
            register u64 x3 asm("x3") = 0;

            asm volatile("hvc #0" : "+r"(x0)
                         : "r"(x1), "r"(x2), "r"(x3) : "memory");
            if (x0) {
                fail(1);
            }
        }
    }
    __atomic_store_n(&slots[id].ready, 1, __ATOMIC_RELEASE);
    if (!id) {
        for (unsigned i = 1; i < CPUS; i++) {
            while (!__atomic_load_n(&slots[i].ready, __ATOMIC_ACQUIRE)) {
                asm volatile("yield");
            }
        }
        marker(1);
        __atomic_store_n(&go, 1, __ATOMIC_RELEASE);
    } else {
        while (!__atomic_load_n(&go, __ATOMIC_ACQUIRE)) {
            asm volatile("yield");
        }
    }

    for (u64 block = 0; block < WORK / CHUNK; block++) {
        checksum += compute(CHUNK);
#if COUNTER_READS > 0
        for (unsigned read = 0; read < COUNTER_READS; read++) {
            u64 value;

            asm volatile("mrs %0, cntvct_el0" : "=r"(value));
            counter_sink ^= value;
        }
#endif
    }
    slots[id].result = counter_sink;
    if (checksum != (WORK / CHUNK) * fibonacci(30 * CHUNK + 1)) {
        fail(2);
    }
    __atomic_store_n(&slots[id].done, 1, __ATOMIC_RELEASE);
    if (__atomic_add_fetch(&finished, 1, __ATOMIC_ACQ_REL) == CPUS) {
        for (unsigned i = 0; i < CPUS; i++) {
            if (!__atomic_load_n(&slots[i].done, __ATOMIC_ACQUIRE)) {
                fail(3);
            }
        }
        marker(2);
    }
    /* Host collects diagnostics then quits QEMU; no spinning at the end. */
    for (;;) {
        asm volatile("wfi");
    }
}

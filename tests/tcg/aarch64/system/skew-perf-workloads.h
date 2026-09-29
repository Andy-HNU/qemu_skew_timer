/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Included by skew-perf-guest.c: staged workloads share its start/end protocol. */
typedef unsigned int u32;
#define MMIO32(a) (*(volatile u32 *)(a))
#define SYSWRITE(reg, value) asm volatile("msr " #reg ", %0; isb" :: "r"((u64)(value)) : "memory")
#define SYSREAD(reg) ({ u64 v; asm volatile("mrs %0, " #reg : "=r"(v)); v; })
#define MEMORY_WORDS 65536
/* Numeric encodings support older GNU assemblers lacking the ICC_* names:
 * SRE_EL1=S3_0_C12_C12_5, PMR_EL1=S3_0_C4_C6_0,
 * IGRPEN1_EL1=S3_0_C12_C12_7, IAR1_EL1=S3_0_C12_C12_0,
 * EOIR1_EL1=S3_0_C12_C12_1. */
extern u64 memory_compute(u64 n, const u64 *data);
static u64 memory_area[CPUS][MEMORY_WORDS] __attribute__((aligned(4096)));
static u64 memory_expected[CPUS];
struct activity {
    u64 compute, memory, sleeps, wakes, parked, released, stages;
    char pad[72];
};
static struct activity activity[CPUS] __attribute__((aligned(128)));

/* Pinned virt/GICv3 layout, <=512 MiB RAM: 123 low redistributors, then
 * high redistributors at 256 GiB. Mirrors hw/arm/virt.c's fixed map.
 * Check GICR_TYPER affinity so a changed map fails instead of hanging. */
static u64 redist(unsigned id)
{
    return id < 123 ? 0x080a0000UL + id * 0x20000UL :
                     0x4000000000UL + (id - 123) * 0x20000UL;
}

static void workload_init(unsigned id)
{
    if (SCENARIO == 5 || SCENARIO == 7) {
        u64 per_cpu = WORK / (SCENARIO == 7 ? 4 : 1) / PHASES / CPUS;
        u64 n = per_cpu - per_cpu/2;
        /* Independent checksum of the permutation, computed before START.
         * Distinct values catch a broken stride or access to another CPU's data. */
        u64 expected = (n / MEMORY_WORDS) * MEMORY_WORDS * (MEMORY_WORDS + 1) / 2;
        for (u64 i = 0; i < n % MEMORY_WORDS; i++) {
            expected += (i * 4093) % MEMORY_WORDS + 1;
        }
        memory_expected[id] = expected * (id + 1);
        for (u64 i = 0; i < MEMORY_WORDS; i++) {
            memory_area[id][i] = (i + 1) * (id + 1);
        }
    }
    if (SCENARIO == 6 || SCENARIO == 7) {
        u64 r = redist(id);
        u64 affinity = ((id / 16) << 8) | (id % 16);
        if ((*(volatile u64 *)(r + 8) >> 32) != affinity) {
            finish(10);
        }
        if (!id) {
            MMIO32(0x08000000UL) = (1U << 4) | 2; /* ARE_NS, Group1NS */
        }
        MMIO32(r + 0x14) &= ~2U; /* GICR_WAKER.ProcessorSleep */
        while (MMIO32(r + 0x14) & 4) {}
        MMIO32(r + 0x10080) = ~0U; /* SGIs belong to Group1NS */
        MMIO32(r + 0x10400) = 0;   /* SGI0 priority */
        MMIO32(r + 0x10100) = 1;   /* enable SGI0 */
        SYSWRITE(S3_0_C12_C12_5, 1);
        SYSWRITE(S3_0_C4_C6_0, 255);
        SYSWRITE(S3_0_C12_C12_7, 1);
        /* IRQ stays masked. WFI wakes on pending IRQ; C acknowledges it.
         * No asynchronous handler can race the condition check and WFI. */
        asm volatile("dsb sy" ::: "memory");
    }
}

static void checked_compute(unsigned id, u64 n)
{
    if (!n || compute(n) != fib(30*n + 1)) {
        finish(11);
    }
    activity[id].compute += n;
}

static void checked_memory(unsigned id, u64 n)
{
    if (!n || memory_compute(n, memory_area[id]) != memory_expected[id]) {
        finish(12);
    }
    activity[id].memory += n;
}

static void stage_barrier(unsigned id, u64 stage)
{
    STORE(slots[id].epoch, stage);
    for (unsigned i = 0; i < CPUS; i++) {
        while (LOAD(slots[i].epoch) < stage) {}
    }
    activity[id].stages++;
}

static void drain_wake(unsigned id)
{
    u64 irq = SYSREAD(S3_0_C12_C12_0);
    if (irq < 1020) {
        if (irq != 0) {
            finish(13);
        }
        SYSWRITE(S3_0_C12_C12_1, irq);
        activity[id].wakes++;
    }
}

static int sleeper(unsigned id, unsigned leader)
{
    return ((id + CPUS - leader) % CPUS) & 1;
}

static void idle_stage(unsigned id, unsigned leader, u64 stage, u64 n)
{
    if (sleeper(id, leader)) {
        checked_compute(id, n/4);
        /* Remove stale pending IRQ before publishing the new park request. */
        drain_wake(id);
        STORE(activity[id].parked, stage);
        do {
            asm volatile("dsb sy; wfi" ::: "memory");
            activity[id].sleeps++;
            drain_wake(id);
        } while (LOAD(activity[id].released) < stage);
        checked_compute(id, n - n/4);
    } else if (id == leader) {
        for (unsigned i = 0; i < CPUS; i++) {
            if (sleeper(i, leader)) {
                while (LOAD(activity[i].parked) < stage) {}
            }
        }
        /* Fixed useful work gives sleepers a substantial interval to halt.
         * The leader remains active, excluding the all-idle time-warp path. */
        checked_compute(id, n);
        for (unsigned i = 0; i < CPUS; i++) {
            if (sleeper(i, leader)) {
                STORE(activity[i].released, stage);
                asm volatile("dsb sy" ::: "memory");
                MMIO32(redist(i) + 0x10200) = 1; /* pend SGI0 on peer */
                asm volatile("dsb sy" ::: "memory");
            }
        }
    } else {
        checked_compute(id, n);
    }
}

/* Each mixed cycle executes total -> phased -> memory -> idle in one guest.
 * WORK is split equally between kinds before integer rounding. */
static void run_workload(unsigned id)
{
    u64 stage = 0;
    for (unsigned p = 0; p < PHASES; p++) {
        unsigned kinds = SCENARIO == 7 ? 4 : 1;
        for (unsigned k = 0; k < kinds; k++) {
            unsigned kind = SCENARIO == 7 ? k : SCENARIO - 3;
            u64 allocation = WORK / kinds / PHASES;
            unsigned leader = p % CPUS;
            stage++;
            if (kind == 1) {
                unsigned heavy_count = (CPUS + 1 - (p & 1)) / 2;
                u64 n = allocation / (CPUS + 3 * heavy_count);
                checked_compute(id, n * (((id + p) & 1) ? 1 : 4));
            } else if (kind == 2) {
                u64 n = allocation / CPUS;
                checked_compute(id, n/2);
                checked_memory(id, n - n/2);
            } else if (kind == 3) {
                idle_stage(id, leader, stage, allocation / CPUS);
            } else {
                checked_compute(id, allocation / CPUS);
            }
            stage_barrier(id, stage);
            if (kind == 3) {
                drain_wake(id);
            }
        }
    }
}

static void print_text(const char *s)
{
    register u64 x0 asm("x0") = 4;
    register const char *x1 asm("x1") = s;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
}

static void print_number(u64 n)
{
    char b[24], *p = b + sizeof(b) - 1;
    *p = 0;
    do { *--p = '0' + n % 10; n /= 10; } while (n);
    print_text(p);
}

/* Outside START/DONE timing. Host verifies totals against its own plan. */
static void workload_report(void)
{
    u64 totals[5] = {0};
    for (unsigned i = 0; i < CPUS; i++) {
        totals[0] += activity[i].compute;
        totals[1] += activity[i].memory;
        totals[2] += activity[i].sleeps;
        totals[3] += activity[i].wakes;
        totals[4] += activity[i].stages;
    }
    print_text("GUEST_WORK");
    for (unsigned i = 0; i < 5; i++) {
        print_text(" ");
        print_number(totals[i]);
    }
    print_text("\n");
}

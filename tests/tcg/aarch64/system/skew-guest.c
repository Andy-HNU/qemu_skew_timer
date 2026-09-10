/* SPDX-License-Identifier: GPL-2.0-or-later
 * QEMU virt, EL1, GICv2, MMU off. Only CPU0 writes the semihost console.
 */
/* 无需 guest OS 的 AArch64 测试：寄存器/MMIO 直接访问，双核消息用原子同步。 */
typedef unsigned long u64;
typedef unsigned int u32;
#define REG32(a) (*(volatile u32 *)(a))
#define READ(r) ({ u64 v; asm volatile("isb; mrs %0, " #r : "=r"(v)); v; })
#define WRITE(r, v) asm volatile("msr " #r ", %0; isb" :: "r"((u64)(v)) : "memory")
/* 跨核 request/reply 使用 acquire/release，确保 payload/result 对接收方可见。 */
#define LOAD(p) __atomic_load_n(&(p), __ATOMIC_ACQUIRE)
#define STORE(p, v) __atomic_store_n(&(p), (v), __ATOMIC_RELEASE)
extern u64 short_loop(u64 n), long_loop(u64 n);
extern void secondary_start(void);
extern void fault_probe(void);
/* 双核握手与消息共享区；普通数据通过后续原子标志的发布/获取同步。 */
static u64 ready, request, reply, payload, result, stamp, done, worker_ticks;
static u64 atomic_count;
static u64 freq;
volatile u64 irq_seen, irq_time, irq_id;

/* 通过 AArch64 semihosting ABI 输出或退出，无需串口驱动与操作系统。 */
static u64 semi(u64 op, void *arg)
{
    register u64 x0 asm("x0") = op;
    register void *x1 asm("x1") = arg;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    return x0;
}

/* 输出以零结尾的字符串，供宿主脚本收集测试结果。 */
static void text(const char *s)
{
    semi(4, (void *)s);
}

/* 裸机十进制格式化，不引入 libc。 */
static void number(u64 n)
{
    char b[24], *p = b + sizeof(b) - 1;
    *p = 0;
    do {
        *--p = '0' + n % 10;
        n /= 10;
    } while (n);
    text(p);
}

/* 固定三列输出协议：名称与两个整数，由 skew-check.py 解析。 */
static void record(const char *name, u64 a, u64 b)
{
    text(name); text(" "); number(a); text(" "); number(b); text("\n");
}

/* 输出 PASS/FAIL 后请求退出；若宿主未退出则停在 WFI。 */
static void finish(u64 code)
{
    u64 args[] = { 0x20026, code };
    text(code ? "FAIL\n" : "PASS\n");
    semi(0x20, args);
    for (;;) {
        asm volatile("wfi");
    }
}

/* guest 断言失败立即返回失败码，避免错误被宿主误判为通过。 */
static void check(int ok)
{
    if (!ok) {
        finish(1);
    }
}

/* PSCI CPU_ON 启动 CPU1，等待 ready 确认其完成启动。 */
static void start_cpu(void)
{
    register u64 x0 asm("x0") = 0xc4000003; /* PSCI CPU_ON */
    register u64 x1 asm("x1") = 1;
    register u64 x2 asm("x2") = (u64)secondary_start;
    register u64 x3 asm("x3") = 0;
    asm volatile("hvc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3)
                 : "memory");
    check(x0 == 0);
    while (!LOAD(ready)) {
    }
}

/* CPU1 按 MODE 执行应答、忙循环或长延迟；完成后进入 WFI。 */
static void worker(void)
{
    STORE(ready, 1);
    /* 百万次消息往返与原子递增，覆盖跨核同步及原子重试。 */
    if (MODE == 7) {
        for (u64 seq = 1; seq <= 1000000; seq++) {
            while (LOAD(request) != seq) {
            }
            result = payload ^ 0x5a5a;
            __atomic_fetch_add(&atomic_count, 1, __ATOMIC_RELAXED);
            STORE(reply, seq);
        }
    } else if (MODE == 8 || MODE == 12) {
        /* 控制测试和忙态定时器测试持续运行 CPU1，避免全空闲跳时。 */
        while (!LOAD(done)) {
            short_loop(10000);
        }
    } else if (MODE == 2) {
        /* CPU1 执行固定工作量并发布完成，CPU0 比较两核计时结果。 */
        worker_ticks = long_loop(2000000);
        STORE(done, 1);
    } else if (MODE == 3) {
        /* 前 32 包正常应答，第 33 包故意延迟，第 34 包不应答以制造真实超时。 */
        for (u64 seq = 1; seq <= 34; seq++) {
            while (LOAD(request) != seq) {
            }
            u64 t = READ(cntpct_el0);
            check(t >= stamp);
            if (seq == 34) {
                while (!LOAD(done)) {
                    short_loop(1000);
                }
                break;
            }
            short_loop(seq == 33 ? 10000000 : 200000);
            result = payload ^ 0x5a5a;
            stamp = READ(cntpct_el0);
            STORE(reply, seq);
        }
    }
    for (;;) {
        asm volatile("wfi");
    }
}

/* CPU0 驱动测试并输出结果；CPU1 进入 worker 后最终停在 WFI。 */
void guest_main(u64 cpu)
{
    if (cpu) {
        worker();
    }
    freq = READ(cntfrq_el0);
    record("FREQ", freq, MODE);
    /* 全空闲且无定时器：等待宿主 UART 字节唤醒，检查时钟不会自行流逝。 */
    if (MODE == 10) {
        start_cpu();
        REG32(0x08000000UL) = 1;
        REG32(0x08000104UL) = 2; /* UART SPI33 */
        REG32(0x08000820UL) = 0x01010101; /* target CPU0 */
        REG32(0x08010004UL) = 255;
        REG32(0x08010000UL) = 1;
        REG32(0x09000030UL) = 0x301;
        REG32(0x09000038UL) = 0x10; /* RX interrupt */
        text("IDLE_READY\n");
        while (REG32(0x09000018UL) & 0x10) {
            asm volatile("dsb sy; wfi" ::: "memory");
        }
        check((REG32(0x09000000UL) & 255) == 'x');
        record("EXTERNAL_WAKE", READ(cntpct_el0), 1);
    } else if (MODE == 11) {
        /* EL2 设置 CNTVOFF，验证虚拟计数单调并在物理计数稳定时核对偏移。 */
        check(READ(CurrentEL) == 8);
        WRITE(cntvoff_el2, 1234);
        while (READ(cntpct_el0) < 1234) {
            short_loop(10000);
        }
        u64 prior = 0, matched = 0;
        for (u64 i = 0; i < 100000; i++) {
            u64 p = READ(cntpct_el0), v = READ(cntvct_el0);
            u64 p2 = READ(cntpct_el0);
            check(v >= prior);
            prior = v;
            if (p == p2) {
                check(p - v == 1234);
                matched++;
            }
        }
        check(matched > 0);
        record("CNTVOFF", 1234, matched);
    } else if (MODE == 9) {
        /* 小循环配合极小 skew 窗口，覆盖 TB 被预算截断时的精确计数。 */
        short_loop(17);
        long_loop(7);
        fault_probe();
    } else if (MODE == 7) {
        /* CPU0 发布百万条消息并验证应答；两核各递增一次，总数应为两百万。 */
        start_cpu();
        for (u64 seq = 1; seq <= 1000000; seq++) {
            payload = seq * 37;
            STORE(request, seq);
            __atomic_fetch_add(&atomic_count, 1, __ATOMIC_RELAXED);
            while (LOAD(reply) != seq) {
                asm volatile("wfe");
            }
            check(result == (payload ^ 0x5a5a));
        }
        check(atomic_count == 2000000);
        record("STRESS_PACKETS", 1000000, atomic_count);
    } else if (MODE == 8) {
        /* 持续 TLBI 广播并等待 UART，供宿主反复 stop/cont 和检查迁移被拒绝。 */
        start_cpu();
        REG32(0x09000030UL) = 0x301;
        text("CONTROL_READY\n");
        while (REG32(0x09000018UL) & 0x10) {
            short_loop(10000);
            asm volatile("dsb ish; tlbi vmalle1is; dsb ish; isb" ::: "memory");
        }
        (void)REG32(0x09000000UL);
        STORE(done, 1);
    } else if (MODE == 1) {
        /* 固定指令总量、不同循环长度，检查计时比例并覆盖 MMIO/SVC 回退。 */
        record("COUNT_SHORT", 64000000, short_loop(32000000));
        record("COUNT_LONG", 64000000, long_loop(2000000));
        record("COUNT_LONG", 128000000, long_loop(4000000));
        fault_probe();
    } else if (MODE == 2) {
        /* 两核并行执行等长循环，确认双方都能获得非零模拟时间进展。 */
        start_cpu();
        u64 t = long_loop(2000000);
        while (!LOAD(done)) {
        }
        record("SMP", t, worker_ticks);
        check(t && worker_ticks);
    } else if (MODE == 3) {
        /* 用共享计数器测量 5ms 超时；skew 模式要求仅故意延迟/丢弃的包超时。 */
        start_cpu();
        for (u64 seq = 1; seq <= 34; seq++) {
            payload = seq * 37;
            u64 begin = READ(cntpct_el0);
            stamp = begin;
            STORE(request, seq);
            u64 now;
            int timeout = 0;
            while (LOAD(reply) != seq) {
                now = READ(cntpct_el0);
                check(now >= begin);
                if (now - begin >= freq / 200) {
                    timeout = 1;
                    break;
                }
            }
            now = READ(cntpct_el0);
            record("PACKET", seq, timeout);
            record("LATENCY", seq, now - begin);
            if (seq < 34) {
                while (LOAD(reply) != seq) {
                }
                check(result == (payload ^ 0x5a5a));
                check(READ(cntpct_el0) >= stamp);
            }
            /* Baseline timeout outcomes are measurements, not acceptance. */
            if (SKEW) {
                check(timeout == (seq >= 33));
            } else if (seq == 34) {
                check(timeout);
            }
        }
        STORE(done, 1);
    } else if (MODE == 4 || MODE == 6 || MODE == 12) {
        /*
         * 物理定时器场景：MODE4 检查跳时后继续执行，MODE6 压测 WFI。
         * MODE12 两核保持忙态并真正进入 IRQ 向量，测量中断处理延迟。
         */
        start_cpu(); /* CPU1 在 MODE4/6 中 WFI，在 MODE12 中持续执行。 */
        REG32(0x08000000UL) = 1; /* GICD_CTLR */
        REG32(0x08000100UL) = 1U << 30; /* physical timer PPI */
        REG32(0x0800041cUL) = 0x00800000; /* PPI30 priority */
        REG32(0x08010004UL) = 255; /* GICC_PMR */
        REG32(0x08010000UL) = 1; /* GICC_CTLR */
        for (u64 i = 0; i < (MODE == 6 ? 100000 : 32); i++) {
            u64 begin = READ(cntpct_el0);
            u64 us = i % 4 == 0 ? 10 : i % 4 == 1 ? 100 :
                     i % 4 == 2 ? 1000 : 10000;
            u64 deadline = begin + freq * us / 1000000;
            irq_seen = 0;
            WRITE(cntp_cval_el0, deadline);
            WRITE(cntp_ctl_el0, 1);
            /* 解除 IRQ 屏蔽并等待汇编处理器记录时间；中断必须不早于 deadline。 */
            if (MODE == 12) {
                asm volatile("msr daifclr, #2" ::: "memory");
                while (!irq_seen) {
                    short_loop(100);
                }
                asm volatile("msr daifset, #2" ::: "memory");
                check((irq_id & 1023) == 30 && irq_time >= deadline);
                record("TIMER_IRQ", us, irq_time - deadline);
                continue;
            }
            /* DAIF.I stays masked: WFI wakes on the pending GIC IRQ,
             * then this code acknowledges it without a vector handler. */
            do {
                asm volatile("dsb sy; wfi" ::: "memory");
            } while (!(READ(cntp_ctl_el0) & 4));
            u64 now = READ(cntpct_el0);
            u32 irq = REG32(0x0801000cUL);
            WRITE(cntp_ctl_el0, 0);
            REG32(0x08010010UL) = irq;
            check((irq & 1023) == 30 && now >= deadline);
            if (MODE == 4) {
                record("TIMER", us, now - deadline);
                u64 delta = long_loop(2000000);
                record("POST_WARP", i, delta);
                check(delta > 0 && READ(cntpct_el0) >= now);
            } else {
                short_loop(16);
            }
        }
        if (MODE == 6) {
            record("STRESS_WFI", 100000, READ(cntpct_el0));
        }
        STORE(done, 1);
    } else if (MODE == 5) {
        /* 暂停场景的 guest 端；宿主完成暂停/恢复后通过 UART 放行。 */
        /* Host sends a byte on PL011 only after QMP stop/wait/cont. */
        REG32(0x09000030UL) = 0x301;
        u64 begin = READ(cntpct_el0);
        text("PAUSE_READY\n");
        while (REG32(0x09000018UL) & 0x10) {
        }
        (void)REG32(0x09000000UL);
        u64 now = READ(cntpct_el0);
        check(now >= begin);
        record("PAUSE", now - begin, freq);
    }
    finish(0);
}

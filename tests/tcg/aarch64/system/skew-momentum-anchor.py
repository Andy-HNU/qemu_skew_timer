#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run actual skew reset/prepare/update functions with a controlled VM clock.

QEMU atomics and the production sampling functions are used directly. Timer,
CPU membership and seqlock plumbing are single-threaded stubs; this regression
tests sampling intervals, not concurrent publication or guest timer delivery.
"""
import os
import re
import resource
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
source = (ROOT / "accel/tcg/skew.c").read_text()


def function(name):
    match = re.search(r"^(?:static )?(?:uint64_t|int64_t|void) " + name +
                      r"\(.*?\n\}", source, re.M | re.S)
    if not match:
        raise RuntimeError(f"Function not found: {name}")
    return match.group()


declarations = source[source.index("#define SKEW_MOMENTUM_HISTORY"):
                      source.index("/* 用宿主 REALTIME")]
prefix = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define qemu_build_assert(test) _Static_assert(test, #test)
#include "qemu/atomic.h"
#define NANOSECONDS_PER_SECOND 1000000000ULL
#define QEMU_CLOCK_REALTIME 0
#define QEMU_CLOCK_VIRTUAL 1
#define QEMU_TIMER_ATTR_ALL 0
typedef unsigned QemuSeqLock;
typedef struct CPUState {
    uint64_t skew_raw_icount, skew_raw_base, skew_logical_base;
    uint64_t skew_budget_global;
    uint32_t skew_budget;
    uint16_t remaining;
    bool skew_active, skew_waiting;
    int cpu_index;
    int *halt_cond;
} CPUState;
static CPUState test_cpus[2];
#define test_cpu test_cpus[1]
#define CPU_FOREACH(cpu) \
    for ((cpu) = test_cpus; (cpu) < test_cpus + 2; (cpu)++)
static bool running = true, use_skew = true;
static int64_t elapsed;
static unsigned clock_reads;
static uint64_t sim_ips = 100000, window = 1000, update_interval = 100000;
static uint64_t window_ns = 10000000, global_icount;
static int64_t start_ns;
static aligned_int64_t mttcg_elapsed_ns, skew_elapsed_ns, mttcg_start_clock;
static int *coordinator;
static bool bql_locked(void) { return true; }
static bool runstate_is_running(void) { return running; }
static bool skew_enabled(void) { return use_skew; }
static int64_t cpu_get_clock(void) { clock_reads++; return elapsed; }
static unsigned seqlock_read_begin(QemuSeqLock *lock) { return *lock; }
static bool seqlock_read_retry(QemuSeqLock *lock, unsigned seq)
{ return *lock != seq; }
static void seqlock_write_begin(QemuSeqLock *lock) { (*lock)++; }
static void seqlock_write_end(QemuSeqLock *lock) { (*lock)++; }
static uint64_t muldiv64(uint64_t a, uint32_t b, uint32_t c)
{ return (__uint128_t)a * b / c; }
static int64_t qemu_clock_get_ns(int type) { return elapsed; }
static bool all_cpu_threads_idle(void) { return false; }
static int64_t qemu_clock_deadline_ns_all(int type, int attrs) { return -1; }
static void qemu_clock_notify(int type) { }
static void qemu_cond_signal(int *cond) { }
static void timer_mod(int *timer, int64_t when) { }
static void exec_budget_set(CPUState *cpu, uint32_t budget)
{ assert(budget <= UINT16_MAX); cpu->remaining = budget; }
#define error_report(...) fprintf(stderr, __VA_ARGS__)
#define trace_skew_sample(host, ...) ((void)(host))
#define trace_skew_warp(host, ...) ((void)(host))
#define trace_skew_clock(host, ...) ((void)(host))
#define trace_skew_cpu(...) ((void)0)
"""
names = ["skew_muldiv", "skew_ratio_q32", "skew_publish_visible",
         "skew_visible_clock", "skew_clock_at", "skew_get_clock", "logical_count",
         "skew_momentum_rebase", "skew_momentum_reset", "skew_momentum_add", "skew_feedback_slope",
         "skew_update", "skew_cpu_prepare"]
functions = "\n\n".join(function(name) for name in names)
suffix = r"""
static void rejoin(int64_t when, uint64_t progress)
{
    elapsed = when;
    test_cpu.skew_raw_icount = progress;
    test_cpu.skew_active = false;
    qatomic_set_u64(&visible_slope_q32, 0);
    skew_cpu_prepare(&test_cpu);
    assert(test_cpu.skew_active);
    assert(qatomic_read_i64(&visible_anchor_elapsed_ns) == when);
    assert(qatomic_read_u64(&visible_slope_q32) == SKEW_ACTIVE_SLOPE_FLOOR);
}

int main(void)
{
    elapsed = 0;
    clock_reads = 0;
    skew_momentum_reset(0);
    assert(clock_reads == 1);
    assert(sample_anchor_elapsed_ns == 0 && prev_global_icount == 0);
    test_cpus[0].skew_active = true;

    /* 400 instructions = 4ms model time, sampled over 0..10ms. */
    rejoin(8000000, 0);
    test_cpus[0].skew_raw_icount = test_cpu.skew_raw_icount = 400;
    elapsed = 10000000;
    skew_update(NULL);
    assert(momentum_history_count == 1);
    assert(momentum_history_slope[0] == (SKEW_SLOPE_ONE * 2 / 5));
    assert(sample_anchor_elapsed_ns == 10000000 && prev_global_icount == 400);
    assert(qatomic_read_i64(&visible_anchor_elapsed_ns) == 10000000);

    /* Repeated rejoins only move interpolation, preserving the 10..20ms sample. */
    rejoin(13000000, 500);
    rejoin(17000000, 700);
    assert(sample_anchor_elapsed_ns == 10000000 && prev_global_icount == 400);
    test_cpus[0].skew_raw_icount = 800;
    test_cpu.skew_raw_icount = 1100; /* rejoin base=700, logical base=400 */
    elapsed = 20000000;
    skew_update(NULL);
    assert(momentum_history_count == 2);
    assert(momentum_history_slope[1] == (SKEW_SLOPE_ONE * 2 / 5));
    assert(sample_anchor_elapsed_ns == 20000000 && prev_global_icount == 800);

    /* A regular sample without rejoining uses the same paired baselines. */
    test_cpus[0].skew_raw_icount = 1000;
    test_cpu.skew_raw_icount = 1300;
    elapsed = 30000000;
    skew_update(NULL);
    assert(momentum_history_slope[2] == (SKEW_SLOPE_ONE / 5));

    /* Paused updates must not consume a sample or move either anchor. */
    running = false;
    skew_update(NULL);
    assert(momentum_history_count == 3);
    assert(sample_anchor_elapsed_ns == 30000000 && prev_global_icount == 1000);
    assert(qatomic_read_i64(&visible_anchor_elapsed_ns) == 30000000);
    running = true;

    /* New epochs reset both clocks from one read and discard previous history. */
    elapsed = 50000000;
    global_icount = 0;
    start_ns = 123000000;
    clock_reads = 0;
    skew_momentum_reset(123000000);
    assert(clock_reads == 1);
    assert(sample_anchor_elapsed_ns == 50000000 && prev_global_icount == 0);
    assert(qatomic_read_i64(&visible_anchor_elapsed_ns) == 50000000);
    assert(qatomic_read_i64(&anchor_visible_ns) == 123000000);
    assert(momentum_history_count == 0 && momentum_history_pos == 0);
    for (unsigned i = 0; i < SKEW_MOMENTUM_HISTORY; i++) {
        assert(momentum_history_slope[i] == 0);
    }
    memset(test_cpus, 0, sizeof(test_cpus));
    test_cpus[0].skew_active = true;
    rejoin(58000000, 0);
    test_cpus[0].skew_raw_icount = test_cpu.skew_raw_icount = 400;
    elapsed = 60000000;
    skew_update(NULL);
    assert(momentum_history_slope[0] == (SKEW_SLOPE_ONE * 2 / 5));
    puts("PASS: 0.4 sample, repeated rejoins, regular sample, pause, epoch reset");
    return 0;
}
"""


def disable_core_dumps():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


with tempfile.TemporaryDirectory(prefix="skew-momentum-anchor-") as directory:
    path = Path(directory)
    code = prefix + declarations + functions + suffix
    (path / "test.c").write_text(code)
    command = [os.environ.get("CC", "cc"), "-std=gnu11", "-O2", "-Wall",
               "-Wextra", "-Werror", "-Wno-unused-parameter",
               "-fsanitize=undefined", "-DCONFIG_ATOMIC64", "-I",
               str(ROOT / "include"), str(path / "test.c"), "-o",
               str(path / "test")]
    subprocess.run(command, check=True)
    subprocess.run([str(path / "test")], check=True, timeout=10)

    # Restore the old shared-anchor behavior only in prepare. The scenario
    # must then fail on the 0.4 expectation, proving this catches the defect.
    original = "qatomic_set_i64(&visible_anchor_elapsed_ns, cpu_get_clock());"
    assert functions.count(original) == 1
    mutant = functions.replace(original, original +
                               "\n    sample_anchor_elapsed_ns = cpu_get_clock();")
    (path / "test.c").write_text(prefix + declarations + mutant + suffix)
    subprocess.run(command, check=True)
    result = subprocess.run([str(path / "test")], capture_output=True,
                            text=True, timeout=10, preexec_fn=disable_core_dumps)
    assert result.returncode < 0 and "momentum_history_slope[0]" in result.stderr, result
    print("PASS: regression rejects the former shared sampling anchor")

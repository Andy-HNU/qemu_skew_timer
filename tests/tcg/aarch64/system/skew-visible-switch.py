#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Test production visible clock rebasing after publishers are stopped.

Uses QEMU atomics and seqlock. CPU/BQL and elapsed clock are controlled test
fixtures; publisher threads rendezvous between phases to model stopped vCPUs.
Guest suites test the actual VM stop, TB unwind, timer and resume paths.
"""
import argparse
import json
import os
import re
import shlex
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
#include "qemu/osdep.h"
#include <pthread.h>
#include "qemu/thread.h"
#include "qemu/seqlock.h"
static bool use_skew;
static aligned_int64_t elapsed;
static aligned_int64_t mttcg_elapsed_ns, skew_elapsed_ns, mttcg_start_clock;
static int64_t start_ns;
static uint64_t global_icount;
static uint64_t window_ns = 2000000;
static bool qemu_mutex_iothread_locked(void) { return true; }
static bool runstate_is_running(void) { return false; }
static bool skew_enabled(void) { return qatomic_read(&use_skew); }
static int64_t cpu_get_clock(void) { return qatomic_read_i64(&elapsed); }
typedef struct TCGExecutionBudgetOps { int unused; } TCGExecutionBudgetOps;
static const TCGExecutionBudgetOps skew_budget_ops;
#define CF_USE_ICOUNT 0x00020000
#define CF_PARALLEL 0x00080000
typedef struct CPUState {
    uint64_t skew_raw_icount, skew_raw_base, skew_logical_base;
    uint64_t skew_budget_global;
    uint32_t skew_budget, tcg_cflags;
    int32_t cflags_next_tb;
    bool skew_active, skew_waiting;
    struct { uint16_t low, high; } decr;
    const TCGExecutionBudgetOps *execution_budget_ops;
} CPUState;
static uint16_t exec_budget_remaining(CPUState *cpu) { return cpu->decr.low; }
static void exec_budget_set(CPUState *cpu, uint32_t value) { cpu->decr.low = value; }
static pthread_barrier_t parked, resume;
#define PHASES 1000
"""
names = ["skew_publish_visible", "skew_visible_clock", "skew_clock_at",
         "skew_get_clock", "skew_momentum_rebase", "skew_cpu_switch",
         "skew_clock_switch"]
functions = "\n\n".join(function(name) for name in names)
suffix = r"""
static void assert_rebased(int64_t time)
{
    assert(start_ns == time);
    assert(qatomic_read_i64(&model_ns) == time);
    assert(qatomic_read_i64(&visible_ns) == time);
    assert(qatomic_read_i64(&anchor_visible_ns) == time);
    assert(qatomic_read_i64(&visible_anchor_elapsed_ns) == cpu_get_clock());
    assert(sample_anchor_elapsed_ns == cpu_get_clock());
    assert(qatomic_read_u64(&visible_slope_q32) == 0);
    assert(global_icount == 0 && warp_ns == 0 && prev_global_icount == 0);
    assert(momentum_history_count == 0 && momentum_history_pos == 0);
    for (unsigned i = 0; i < SKEW_MOMENTUM_HISTORY; i++) {
        assert(momentum_history_slope[i] == 0);
    }
    assert(qatomic_read_i64(&mttcg_elapsed_ns) +
           qatomic_read_i64(&skew_elapsed_ns) == time);
}

static void seed_skew(int64_t visible, uint64_t slope)
{
    /* M = start(80ms) + warp(5ms) + 1500/100k IPS(15ms) = 100ms. */
    qatomic_set(&use_skew, true);
    qatomic_set_i64(&elapsed, 500000000);
    qatomic_set_i64(&mttcg_elapsed_ns, 70000000);
    qatomic_set_i64(&skew_elapsed_ns, visible - 70000000);
    start_ns = 80000000;
    global_icount = prev_global_icount = 1500;
    warp_ns = 5000000;
    skew_momentum_rebase(visible, 498000000);
    qatomic_set_i64(&model_ns, 100000000);
    qatomic_set_u64(&visible_slope_q32, slope);
    momentum_history_count = 1;
    momentum_history_pos = 1;
    momentum_history_slope[0] = SKEW_SLOPE_ONE;
    prev_global_icount = 1500;
}

static void roundtrip(int64_t visible, uint64_t slope, int64_t expected)
{
    seed_skew(visible, slope);
    int64_t mttcg = qatomic_read_i64(&mttcg_elapsed_ns);
    skew_clock_switch(false);
    assert(!skew_enabled());
    assert(skew_get_clock() == expected);
    assert_rebased(expected);
    assert(qatomic_read_i64(&mttcg_elapsed_ns) == mttcg);
    int64_t skew = qatomic_read_i64(&skew_elapsed_ns);
    for (unsigned i = 0; i < 100; i++) {
        assert(skew_get_clock() == expected); /* paused */
    }
    qatomic_set_i64(&elapsed, cpu_get_clock() + 10000000);
    assert(skew_get_clock() == expected + 10000000);
    skew_clock_switch(true);
    assert(skew_enabled());
    assert(skew_get_clock() == expected + 10000000);
    assert_rebased(expected + 10000000);
    assert(qatomic_read_i64(&skew_elapsed_ns) == skew);
}

static void *stress_reader(void *opaque)
{
    int64_t last = 0;

    for (unsigned phase = 0; phase < PHASES; phase++) {
        for (unsigned call = 0; call < 100; call++) {
            int64_t now = skew_get_clock();
            assert(now >= last);
            last = now;
        }
        /* All calls complete before the control thread can change phase. */
        pthread_barrier_wait(&parked);
        pthread_barrier_wait(&resume);
    }
    return NULL;
}

int main(void)
{
    pthread_t readers[4];
    (void)clock_switching;

    roundtrip(101000000, SKEW_SLOPE_ONE / 2, 102000000); /* unseen interpolation */
    roundtrip(99000000, 0, 99000000); /* visible behind model; no forward jump */
    roundtrip(102000000, SKEW_SLOPE_ONE, 102000000); /* upper bound */

    CPUState cpu = { .skew_raw_icount = 400, .skew_raw_base = 100,
        .skew_logical_base = 200, .skew_budget_global = 500,
        .skew_active = true, .skew_waiting = true,
        .tcg_cflags = CF_PARALLEL | CF_USE_ICOUNT | 0x400,
        .cflags_next_tb = 17, .decr.high = UINT16_MAX,
        .execution_budget_ops = &skew_budget_ops };
    skew_cpu_switch(&cpu, false);
    assert(cpu.skew_raw_icount == 0 && cpu.skew_raw_base == 0);
    assert(cpu.skew_logical_base == 0 && cpu.skew_budget_global == 0);
    assert(!cpu.skew_active && !cpu.skew_waiting && !cpu.execution_budget_ops);
    assert(cpu.tcg_cflags == (CF_PARALLEL | 0x400));
    assert(cpu.cflags_next_tb == -1 && cpu.decr.high == UINT16_MAX);
    skew_cpu_switch(&cpu, true);
    assert(cpu.execution_budget_ops == &skew_budget_ops);
    assert(cpu.tcg_cflags == (CF_PARALLEL | CF_USE_ICOUNT | 0x400));
    assert(cpu.decr.high == UINT16_MAX);

    seed_skew(100000000, SKEW_SLOPE_ONE / 2);
    assert(!pthread_barrier_init(&parked, NULL, 5));
    assert(!pthread_barrier_init(&resume, NULL, 5));

    for (unsigned i = 0; i < 4; i++) {
        assert(!pthread_create(&readers[i], NULL, stress_reader, NULL));
    }
    for (unsigned i = 0; i < PHASES; i++) {
        pthread_barrier_wait(&parked);
        qatomic_set_i64(&elapsed, cpu_get_clock() + 1000);
        int64_t before = skew_get_clock();
        skew_clock_switch(!skew_enabled());
        assert(skew_get_clock() == before);
        assert_rebased(before);
        pthread_barrier_wait(&resume);
    }
    for (unsigned i = 0; i < 4; i++) {
        assert(!pthread_join(readers[i], NULL));
    }
    assert(!pthread_barrier_destroy(&parked));
    assert(!pthread_barrier_destroy(&resume));
    puts("PASS: bias/interpolation/bounds, CPU state, "
         "1000 switches with 4 publishers stopped at each phase boundary");
    return 0;
}
"""
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
args = parser.parse_args()
build = args.build_dir.resolve()
entry = next(item for item in json.loads((build / "compile_commands.json").read_text())
             if item["file"].endswith("accel/tcg/skew.c"))
glib_includes = [arg for arg in shlex.split(entry["command"])
                 if arg.startswith("-I") and "glib" in arg]
with tempfile.TemporaryDirectory(prefix="skew-visible-switch-") as directory:
    path = Path(directory)
    (path / "test.c").write_text(prefix + declarations + functions + suffix)
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O2", "-Wall",
                    "-Wextra", "-Werror", "-Wno-unused-parameter", "-pthread",
                    "-fsanitize=undefined", "-D_GNU_SOURCE", "-I", str(build),
                    "-I", str(ROOT / "include"), *glib_includes, str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True, timeout=30)

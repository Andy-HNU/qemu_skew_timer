/*
 * Instruction-driven sliding-window clock for MTTCG.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "hw/core/cpu.h"
#include "exec/skew.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "qemu/host-utils.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "system/cpus.h"
#include "system/runstate.h"
#include "migration/blocker.h"
#include "trace.h"

bool use_skew;
static uint64_t sim_ips, window, quantum, update_interval;
static uint64_t global_icount;
static aligned_int64_t virtual_ns;
static int64_t warp_ns;
static QEMUTimer *coordinator;
static Error *migration_blocker;

static void skew_read_clock(Object *obj, Visitor *v, const char *name,
                            void *opaque, Error **errp)
{
    int64_t value = skew_get_clock();

    visit_type_int64(v, name, &value, errp);
}

void skew_register_clock(Object *obj)
{
    object_property_add(obj, "skew-time", "int64", skew_read_clock,
                        NULL, NULL, NULL);
}

static void skew_read_raw(Object *obj, Visitor *v, const char *name,
                          void *opaque, Error **errp)
{
    CPUState *cpu = CPU(obj);
    uint64_t value = qatomic_read_u64(&cpu->skew_raw_icount);

    visit_type_uint64(v, name, &value, errp);
}

void skew_register_cpu(CPUState *cpu)
{
    object_property_add(OBJECT(cpu), "skew-raw-icount", "uint64",
                        skew_read_raw, NULL, NULL, NULL);
}

int64_t skew_get_clock(void)
{
    return qatomic_read_i64(&virtual_ns);
}

static uint64_t logical_count(CPUState *cpu)
{
    return cpu->skew_logical_base +
           (qatomic_read_u64(&cpu->skew_raw_icount) - cpu->skew_raw_base);
}

/* BQL serializes membership, rebasing, and the sole clock writer. */
static void skew_update(void *opaque)
{
    CPUState *cpu;
    uint64_t candidate = UINT64_MAX;
    uint64_t completed = global_icount;
    unsigned active = 0;
    int64_t now, deadline = -1;
    int64_t old_ns = skew_get_clock();
    int64_t host_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);

    assert(bql_locked());
    if (!runstate_is_running()) {
        return;
    }

    /* ponytail: O(vCPU count) scan; shard only if profiling justifies it. */
    CPU_FOREACH(cpu) {
        uint64_t raw = qatomic_read_u64(&cpu->skew_raw_icount);
        uint64_t local = cpu->skew_logical_base + raw - cpu->skew_raw_base;

        completed = MAX(completed, local);
        if (cpu->skew_active) {
            candidate = MIN(candidate, local);
            active++;
        }
        trace_skew_sample(host_ns, cpu->cpu_index, cpu->skew_active, raw,
                          cpu->skew_raw_base, cpu->skew_logical_base, local);
    }
    if (active) {
        global_icount = MAX(global_icount, candidate);
    } else if (all_cpu_threads_idle()) {
        /* Commit the final executed tail before entering idle time. */
        global_icount = completed;
    }

    now = warp_ns + muldiv64(global_icount, NANOSECONDS_PER_SECOND, sim_ips);
    assert(now >= skew_get_clock());
    qatomic_set_i64(&virtual_ns, now);

    if (!active && all_cpu_threads_idle()) {
        deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                             QEMU_TIMER_ATTR_ALL);
        if (deadline > 0) {
            warp_ns += deadline;
            now += deadline;
            qatomic_set_i64(&virtual_ns, now);
            trace_skew_warp(host_ns, global_icount, now, deadline);
        }
    }
    trace_skew_clock(host_ns, global_icount, now, active);
    if (now != old_ns || deadline == 0) {
        qemu_clock_notify(QEMU_CLOCK_VIRTUAL);
    }
    CPU_FOREACH(cpu) {
        if (cpu->skew_waiting &&
            logical_count(cpu) - global_icount < window) {
            qemu_cond_signal(cpu->halt_cond);
        }
    }
    timer_mod(coordinator, qemu_clock_get_ns(QEMU_CLOCK_REALTIME) +
              (!active && deadline < 0 ? MAX(update_interval, 10000000) :
               update_interval));
}

static void skew_vm_state(void *opaque, bool running, RunState state)
{
    if (running) {
        timer_mod(coordinator, qemu_clock_get_ns(QEMU_CLOCK_REALTIME));
    } else {
        timer_del(coordinator);
    }
}

bool skew_init(uint64_t ns, uint64_t ips, uint64_t update_ns, Error **errp)
{
    /* Bound products, signed timer arithmetic, and reject zero budgets. */
    if (!ips || ips > NANOSECONDS_PER_SECOND * 1000ULL ||
        !ns || ns > NANOSECONDS_PER_SECOND ||
        !update_ns || update_ns > NANOSECONDS_PER_SECOND) {
        error_setg(errp, "skew and skew-update must be in 1..1000000000 ns; "
                   "skew-ips must be in 1..1000000000000");
        return false;
    }
    window = muldiv64(ns, ips, NANOSECONDS_PER_SECOND);
    quantum = MIN(65535, muldiv64(update_ns, ips, NANOSECONDS_PER_SECOND));
    if (!window || !quantum) {
        error_setg(errp, "skew and skew-update must each cover an instruction");
        return false;
    }
    error_setg(&migration_blocker,
               "skew clock does not support migration or snapshots");
    if (migrate_add_blocker(&migration_blocker, errp) < 0) {
        return false;
    }
    sim_ips = ips;
    update_interval = update_ns;
    coordinator = timer_new_ns(QEMU_CLOCK_REALTIME, skew_update, NULL);
    qemu_add_vm_change_state_handler(skew_vm_state, NULL);
    use_skew = true;
    return true;
}

void skew_cpu_idle(CPUState *cpu)
{
    assert(bql_locked());
    if (cpu->skew_active &&
        (cpu->stop || cpu_thread_is_idle(cpu) || !runstate_is_running())) {
        cpu->skew_active = false;
        trace_skew_cpu(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                       cpu->cpu_index, false, cpu->skew_raw_icount,
                       logical_count(cpu), global_icount);
    }
}

void skew_cpu_prepare(CPUState *cpu)
{
    uint64_t lead;

    assert(bql_locked());
    if (!cpu->skew_active) {
        cpu->skew_raw_base = qatomic_read_u64(&cpu->skew_raw_icount);
        cpu->skew_logical_base = global_icount;
        cpu->skew_active = true;
        trace_skew_cpu(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                       cpu->cpu_index, true, cpu->skew_raw_icount,
                       logical_count(cpu), global_icount);
    }
    lead = logical_count(cpu) - global_icount;
    assert(lead <= window);
    cpu->icount_budget = MIN(quantum, window - lead);
    cpu->neg.icount_decr.u16.low = cpu->icount_budget;
    assert(cpu->icount_extra == 0);
}

void skew_cpu_account(CPUState *cpu)
{
    uint64_t executed = cpu->icount_budget - cpu->neg.icount_decr.u16.low;

    assert(executed <= cpu->icount_budget);
    /* Publish only completed instructions, including TB unwind corrections. */
    qatomic_set_u64(&cpu->skew_raw_icount,
                    qatomic_read_u64(&cpu->skew_raw_icount) + executed);
    cpu->icount_budget = 0;
    cpu->neg.icount_decr.u16.low = 0;
}

void skew_cpu_wait(CPUState *cpu)
{
    assert(bql_locked());
    while (cpu->skew_active && !cpu->stop && !cpu->halted &&
           runstate_is_running() && cpu_work_list_empty(cpu) &&
           !qatomic_read(&cpu->exit_request) &&
           logical_count(cpu) - global_icount == window) {
        cpu->skew_waiting = true;
        trace_skew_wait(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                        cpu->cpu_index, true, cpu->skew_raw_icount,
                        logical_count(cpu), global_icount);
        qemu_cond_wait_bql(cpu->halt_cond);
    }
    if (cpu->skew_waiting) {
        cpu->skew_waiting = false;
        trace_skew_wait(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                        cpu->cpu_index, false, cpu->skew_raw_icount,
                        logical_count(cpu), global_icount);
    }
}

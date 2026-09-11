/*
 * QEMU TCG Single Threaded vCPUs implementation using instruction counting
 *
 * Copyright (c) 2003-2008 Fabrice Bellard
 * Copyright (c) 2014 Red Hat Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */

#include "qemu/osdep.h"
#include "system/replay.h"
#include "exec/icount.h"
#include "exec/exec-budget.h"
#include "qemu/main-loop.h"
#include "qemu/guest-random.h"
#include "hw/core/cpu.h"

#include "tcg-accel-ops.h"
#include "tcg-accel-ops-icount.h"
#include "tcg-accel-ops-rr.h"

static int64_t icount_get_limit(void)
{
    int64_t deadline;

    if (replay_mode != REPLAY_MODE_PLAY) {
        /*
         * Include all the timers, because they may need an attention.
         * Too long CPU execution may create unnecessary delay in UI.
         */
        deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                              QEMU_TIMER_ATTR_ALL);
        /* Check realtime timers, because they help with input processing */
        deadline = qemu_soonest_timeout(deadline,
                qemu_clock_deadline_ns_all(QEMU_CLOCK_REALTIME,
                                           QEMU_TIMER_ATTR_ALL));

        /*
         * Maintain prior (possibly buggy) behaviour where if no deadline
         * was set (as there is no QEMU_CLOCK_VIRTUAL timer) or it is more than
         * INT32_MAX nanoseconds ahead, we still use INT32_MAX
         * nanoseconds.
         */
        if ((deadline < 0) || (deadline > INT32_MAX)) {
            deadline = INT32_MAX;
        }

        return icount_round(deadline);
    } else {
        return replay_get_instructions();
    }
}

static void icount_notify_aio_contexts(void)
{
    /* Wake up other AioContexts.  */
    qemu_clock_notify(QEMU_CLOCK_VIRTUAL);
    qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL);
}

void icount_handle_deadline(void)
{
    assert(qemu_in_vcpu_thread());
    int64_t deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                                  QEMU_TIMER_ATTR_ALL);

    /*
     * Instructions, interrupts, and exceptions are processed in cpu-exec.
     * Don't interrupt cpu thread, when these events are waiting
     * (i.e., there is no checkpoint)
     */
    if (deadline == 0) {
        icount_notify_aio_contexts();
    }
}

/* Distribute the budget evenly across all CPUs */
int64_t icount_percpu_budget(int cpu_count)
{
    int64_t limit = icount_get_limit();
    int64_t timeslice = limit / cpu_count;

    if (timeslice == 0) {
        timeslice = limit;
    }

    return timeslice;
}

/* icount 将自己的长预算分成 TCG 可执行额度和私有扩展额度。 */
static uint16_t icount_refill_budget(CPUState *cpu)
{
    uint16_t insns_left = MIN(UINT16_MAX, cpu->icount_budget);

    exec_budget_set(cpu, insns_left);
    cpu->icount_extra = cpu->icount_budget - insns_left;
    return insns_left;
}

static bool icount_budget_exhausted(CPUState *cpu)
{
    return exec_budget_remaining(cpu) + cpu->icount_extra == 0;
}

/* 到期时先推进 icount 的时钟，再续配其剩余长预算。 */
static uint16_t icount_budget_expired(CPUState *cpu)
{
    icount_update(cpu);
    return icount_refill_budget(cpu);
}

const TCGExecutionBudgetOps icount_budget_ops = {
    .exhausted = icount_budget_exhausted,
    .expired = icount_budget_expired,
};

void icount_prepare_for_run(CPUState *cpu, int64_t cpu_budget)
{
    /*
     * These should always be cleared by icount_process_data after
     * each vCPU execution. However u16.high can be raised
     * asynchronously by cpu_exit/cpu_interrupt/tcg_handle_interrupt
     */
    g_assert(exec_budget_remaining(cpu) == 0);
    g_assert(cpu->icount_extra == 0);

    replay_mutex_lock();

    cpu->icount_budget = MIN(icount_get_limit(), cpu_budget);
    icount_refill_budget(cpu);

    if (cpu->icount_budget == 0) {
        /*
         * We're called without the BQL, so must take it while
         * we're calling timer handlers.
         */
        bql_lock();
        icount_notify_aio_contexts();
        bql_unlock();
    }
}

void icount_process_data(CPUState *cpu)
{
    /* Account for executed instructions */
    icount_update(cpu);

    /* Reset the counters */
    exec_budget_set(cpu, 0);
    cpu->icount_extra = 0;
    cpu->icount_budget = 0;

    replay_account_executed_instructions();

    replay_mutex_unlock();
}

void icount_handle_interrupt(CPUState *cpu, int mask)
{
    int old_mask = cpu->interrupt_request;

    tcg_handle_interrupt(cpu, mask);
    if (qemu_cpu_is_self(cpu) &&
        !cpu->neg.can_do_io
        && (mask & ~old_mask) != 0) {
        cpu_abort(cpu, "Raised interrupt while not in I/O function");
    }
}

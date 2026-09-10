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

/* 模式开关只在初始化成功后置位；运行中的时钟更新由协调器负责。 */
bool use_skew;
/*
 * sim_ips 的单位是指令/秒；window 和 quantum 是指令数。
 * update_interval 是宿主轮询纳秒数，不是虚拟时间步长或中断延迟保证。
 */
static uint64_t sim_ips, window, quantum, update_interval;
/* 全局逻辑进度单调不减，由持有 BQL 的协调器独占写入。 */
static uint64_t global_icount;
/* 跨线程原子读取的统一虚拟时间，等于指令换算时间加空闲跳时偏移。 */
static aligned_int64_t virtual_ns;
/* 全 CPU 空闲时累加的跳时偏移，不伪造任何 CPU 的已执行指令数。 */
static int64_t warp_ns;
/* 用宿主 REALTIME 调度协调器，避免虚拟时钟停滞时无法唤醒协调逻辑。 */
static QEMUTimer *coordinator;
/* 当前计数、基准和协调器状态没有迁移协议，因此明确禁止迁移/快照。 */
static Error *migration_blocker;

/* QOM getter 通过原子时钟读取接口返回纳秒值，不触发时间推进。 */
static void skew_read_clock(Object *obj, Visitor *v, const char *name,
                            void *opaque, Error **errp)
{
    int64_t value = skew_get_clock();

    visit_type_int64(v, name, &value, errp);
}

/* 没有 setter：测试可观察时间，但不能从外部修改时钟。 */
void skew_register_clock(Object *obj)
{
    object_property_add(obj, "skew-time", "int64", skew_read_clock,
                        NULL, NULL, NULL);
}

/* QOM getter 读取已结算的 raw，不把正在执行但尚未发布的预算计入。 */
static void skew_read_raw(Object *obj, Visitor *v, const char *name,
                          void *opaque, Error **errp)
{
    CPUState *cpu = CPU(obj);
    uint64_t value = qatomic_read_u64(&cpu->skew_raw_icount);

    visit_type_uint64(v, name, &value, errp);
}

/* 给各 CPU 注册计数观测点；属性本身不改变调度或执行行为。 */
void skew_register_cpu(CPUState *cpu)
{
    object_property_add(OBJECT(cpu), "skew-raw-icount", "uint64",
                        skew_read_raw, NULL, NULL, NULL);
}

/* 读取发布值即可，读者不扫描其他 CPU，也不与协调器争夺 BQL。 */
int64_t skew_get_clock(void)
{
    return qatomic_read_i64(&virtual_ns);
}

/* 把本次活跃期的实际增量映射到共同进度；成员和基准受 BQL 保护。 */
static uint64_t logical_count(CPUState *cpu)
{
    return cpu->skew_logical_base +
           (qatomic_read_u64(&cpu->skew_raw_icount) - cpu->skew_raw_base);
}

/* BQL serializes membership, rebasing, and the sole clock writer. */
/* 协调器周期：采样各 CPU、推进全局时间、处理空闲跳时、唤醒边界等待者。 */
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
    /* VM 暂停时不推进时间；运行状态回调会在恢复时重新安排协调器。 */
    if (!runstate_is_running()) {
        return;
    }

    /* ponytail: O(vCPU count) scan; shard only if profiling justifies it. */
    CPU_FOREACH(cpu) {
        uint64_t raw = qatomic_read_u64(&cpu->skew_raw_icount);
        uint64_t local = cpu->skew_logical_base + raw - cpu->skew_raw_base;

        /* 保存所有成员的执行尾部；全空闲时仍需提交最后完成的指令。 */
        completed = MAX(completed, local);
        /* 正常推进取活跃 CPU 的最小值，防止快 CPU 把共享时间单独推远。 */
        if (cpu->skew_active) {
            candidate = MIN(candidate, local);
            active++;
        }
        trace_skew_sample(host_ns, cpu->cpu_index, cpu->skew_active, raw,
                          cpu->skew_raw_base, cpu->skew_logical_base, local);
    }
    /* MAX 保证进度不倒退；全空闲时改用已完成尾部的最大值。 */
    if (active) {
        global_icount = MAX(global_icount, candidate);
    } else if (all_cpu_threads_idle()) {
        /* Commit the final executed tail before entering idle time. */
        global_icount = completed;
    }

    /* 用整数乘除把全局指令数换算为纳秒，再叠加空闲跳时偏移。 */
    now = warp_ns + muldiv64(global_icount, NANOSECONDS_PER_SECOND, sim_ips);
    assert(now >= skew_get_clock());
    qatomic_set_i64(&virtual_ns, now);

    /*

     * 只有没有活跃成员且所有线程确实空闲，才跳到最近虚拟定时器。
     * 领先窗口等待者保持 active，不能误触发此跳时路径。
     */
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
    /* 时间变化或定时器已到期时通知虚拟时钟，促使设备回调得到处理。 */
    if (now != old_ns || deadline == 0) {
        qemu_clock_notify(QEMU_CLOCK_VIRTUAL);
    }
    CPU_FOREACH(cpu) {
        /* 窗口出现余量时唤醒等待线程；线程恢复后仍会重新检查等待条件。 */
        if (cpu->skew_waiting &&
            logical_count(cpu) - global_icount < window) {
            qemu_cond_signal(cpu->halt_cond);
        }
    }
    /* 无活跃 CPU 且无虚拟定时器时至少隔 10ms 轮询，减少宿主空转。 */
    timer_mod(coordinator, qemu_clock_get_ns(QEMU_CLOCK_REALTIME) +
              (!active && deadline < 0 ? MAX(update_interval, 10000000) :
               update_interval));
}

/* 暂停删除宿主定时器，恢复立即安排一次协调，暂停期间虚拟时间冻结。 */
static void skew_vm_state(void *opaque, bool running, RunState state)
{
    if (running) {
        timer_mod(coordinator, qemu_clock_get_ns(QEMU_CLOCK_REALTIME));
    } else {
        timer_del(coordinator);
    }
}

/* 验证单位换算和计数范围；失败时不启用 skew。 */
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
    /*
     * 纳秒窗口转成指令预算，向下取整；每轮 quantum 最多 65535 条，
     * 对应 TCG 的 16 位低半部递减器，不使用额外预算。
     */
    window = muldiv64(ns, ips, NANOSECONDS_PER_SECOND);
    quantum = MIN(65535, muldiv64(update_ns, ips, NANOSECONDS_PER_SECOND));
    if (!window || !quantum) {
        error_setg(errp, "skew and skew-update must each cover an instruction");
        return false;
    }
    /* 在启用前安装迁移阻止器，防止不完整状态被保存或迁移。 */
    error_setg(&migration_blocker,
               "skew clock does not support migration or snapshots");
    if (migrate_add_blocker(&migration_blocker, errp) < 0) {
        return false;
    }
    /* 参数验证完成后建立宿主协调器，并注册 VM 启停生命周期回调。 */
    sim_ips = ips;
    update_interval = update_ns;
    coordinator = timer_new_ns(QEMU_CLOCK_REALTIME, skew_update, NULL);
    qemu_add_vm_change_state_handler(skew_vm_state, NULL);
    use_skew = true;
    return true;
}

/* 只移除停止、WFI 等真实空闲或 VM 停机成员；普通窗口等待仍参与取最小值。 */
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

/* 准备下一轮执行：重新加入的 CPU 从当前全局逻辑进度起跑。 */
void skew_cpu_prepare(CPUState *cpu)
{
    uint64_t lead;

    assert(bql_locked());
    /* 保留累计 raw，以新的 raw_base/logical_base 消除休眠期间的历史落后。 */
    if (!cpu->skew_active) {
        cpu->skew_raw_base = qatomic_read_u64(&cpu->skew_raw_icount);
        cpu->skew_logical_base = global_icount;
        cpu->skew_active = true;
        trace_skew_cpu(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                       cpu->cpu_index, true, cpu->skew_raw_icount,
                       logical_count(cpu), global_icount);
    }
    /* 领先量不得超过 window；实际预算取单轮上限与剩余窗口中的较小者。 */
    lead = logical_count(cpu) - global_icount;
    assert(lead <= window);
    cpu->icount_budget = MIN(quantum, window - lead);
    cpu->neg.icount_decr.u16.low = cpu->icount_budget;
    assert(cpu->icount_extra == 0);
}

/*
 * 完成量 = 发放预算 - 剩余预算；异常回退已修正剩余值。
 * 原子发布后清空预算，使复位等路径重复结算时不会重复累计。
 */
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

/* 到达窗口边界才等待；停机、中断退出、异步工作或 halt 都能打破等待。 */
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
        /* 释放 BQL 后睡眠，让协调器及其他 CPU 前进；醒来取得 BQL 并重查条件。 */
        qemu_cond_wait_bql(cpu->halt_cond);
    }
    /* 记录等待结束，便于 trace 重建限速行为；并非每次唤醒都一定获得预算。 */
    if (cpu->skew_waiting) {
        cpu->skew_waiting = false;
        trace_skew_wait(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                        cpu->cpu_index, false, cpu->skew_raw_icount,
                        logical_count(cpu), global_icount);
    }
}

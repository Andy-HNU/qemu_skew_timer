/*
 * Instruction-driven sliding-window clock for MTTCG.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "hw/core/cpu.h"
#include "exec/skew.h"
#include "exec/exec-budget.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "qemu/host-utils.h"
#include "qemu/seqlock.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "system/cpus.h"
#include "system/cpu-timers.h"
#include "system/runstate.h"
#include "migration/blocker.h"
#include "exec/tb-flush.h"
#include "exec/translation-block.h"
#include "qapi/qapi-commands-run-state.h"
#include "trace.h"

/* 模式只在初始化或全部停核后改变。 */
bool use_skew;
/*
 * sim_ips 的单位是指令/秒；window 是允许领先的指令数。
 * update_interval 是宿主轮询纳秒数，不是虚拟时间步长或中断延迟保证。
 */
static uint64_t sim_ips, window, update_interval, window_ns;
static int64_t start_ns;
/* 当前 skew 阶段的全局进度单调不减，切换模式时清零。 */
static uint64_t global_icount;
/* 两种模式累计贡献的纳秒数；MTTCG 当前阶段增量在读取时补入。 */
static aligned_int64_t mttcg_elapsed_ns, skew_elapsed_ns;
static aligned_int64_t mttcg_start_clock;
/*
 * 软件可见时间在协调周期之间按历史 global 进度插值。斜率采用 Q32 定点数，
 * 避免在时钟热路径使用浮点；8 个样本按 beta=3/4 指数衰减。
 */
#define SKEW_MOMENTUM_HISTORY 8
#define SKEW_SLOPE_SHIFT 32
#define SKEW_SLOPE_ONE (1ULL << SKEW_SLOPE_SHIFT)
#define SKEW_BETA_NUM 3
#define SKEW_BETA_DEN 4
#define SKEW_FEEDBACK_NUM 1
#define SKEW_FEEDBACK_DEN 100
#define SKEW_ACTIVE_SLOPE_FLOOR (SKEW_SLOPE_ONE / 256)
static aligned_int64_t model_ns, visible_ns;
static aligned_int64_t anchor_visible_ns, last_update_elapsed_ns;
static aligned_uint64_t visible_slope_q32;
static uint64_t prev_global_icount;
static uint64_t momentum_history_slope[SKEW_MOMENTUM_HISTORY];
static unsigned momentum_history_pos, momentum_history_count;
/* BQL 串行化写者；切换时读者必须看到同一组模式、累计量和基准。 */
static QemuSeqLock clock_seqlock;
/* 全 CPU 空闲时累加的跳时偏移，不伪造任何 CPU 的已执行指令数。 */
static int64_t warp_ns;
/* 用宿主 REALTIME 调度协调器，避免虚拟时钟停滞时无法唤醒协调逻辑。 */
static QEMUTimer *coordinator;
/* 当前计数、基准和协调器状态没有迁移协议，因此明确禁止迁移/快照。 */
static Error *migration_blocker;

static uint64_t skew_ratio_q32(uint64_t numerator, uint64_t denominator)
{
    if (!denominator) {
        return 0;
    }
    return MIN(((__uint128_t)numerator << SKEW_SLOPE_SHIFT) / denominator,
               SKEW_SLOPE_ONE);
}

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

/* 全部 CPU 和设备共用同一时间线；切换前使用可随 VM 暂停的原生时钟。 */
static int64_t skew_visible_clock(void)
{
    int64_t anchor, elapsed_anchor, model, predicted, lower, upper, old, next;
    uint64_t slope;
    unsigned seq;

    do {
        seq = seqlock_read_begin(&clock_seqlock);
        anchor = qatomic_read_i64(&anchor_visible_ns);
        elapsed_anchor = qatomic_read_i64(&last_update_elapsed_ns);
        model = qatomic_read_i64(&model_ns);
        slope = qatomic_read_u64(&visible_slope_q32);
        predicted = anchor +
                    (((__uint128_t)MAX(cpu_get_clock() - elapsed_anchor, 0) *
                      slope) >> SKEW_SLOPE_SHIFT);
        lower = MAX(model - (int64_t)window_ns, 0);
        upper = model + window_ns;
        predicted = MIN(MAX(predicted, lower), upper);
    } while (seqlock_read_retry(&clock_seqlock, seq));

    /* 多个 CPU 共同推进一个原子可见时钟，只允许单调增加。 */
    do {
        old = qatomic_read_i64(&visible_ns);
        next = MAX(old, predicted);
    } while (next != old && qatomic_cmpxchg(&visible_ns, old, next) != old);
    return next;
}

int64_t skew_get_clock(void)
{
    unsigned seq;
    int64_t now;

    do {
        seq = seqlock_read_begin(&clock_seqlock);
        if (skew_enabled()) {
            now = skew_visible_clock();
        } else {
            now = qatomic_read_i64(&mttcg_elapsed_ns) +
                  qatomic_read_i64(&skew_elapsed_ns);
            now += cpu_get_clock() - qatomic_read_i64(&mttcg_start_clock);
        }
    } while (seqlock_read_retry(&clock_seqlock, seq));
    return now;
}

bool skew_configured(void)
{
    return coordinator != NULL;
}

SkewClockInfo *qmp_query_skew_clock(Error **errp)
{
    SkewClockInfo *info;
    SkewCpuInfoList **tail;
    CPUState *cpu;

    if (!skew_configured()) {
        error_setg(errp, "Configure -accel tcg,thread=multi,skew=NS first");
        return NULL;
    }
    info = g_new0(SkewClockInfo, 1);
    info->mode = skew_enabled() ? SKEW_CLOCK_MODE_SKEW : SKEW_CLOCK_MODE_MTTCG;
    info->virtual_ns = skew_get_clock();
    info->start_ns = start_ns;
    info->window_ns = window_ns;
    info->ips = sim_ips;
    info->update_ns = update_interval;
    /*
     * 当前模式的 elapsed 包含尚未切换结算的实时增量，另一个模式的
     * elapsed 已在上次切换时冻结；两者之和始终等于 virtual。
     */
    if (skew_enabled()) {
        info->mttcg_elapsed_ns = qatomic_read_i64(&mttcg_elapsed_ns);
        info->skew_elapsed_ns = info->virtual_ns - info->mttcg_elapsed_ns;
    } else {
        info->skew_elapsed_ns = qatomic_read_i64(&skew_elapsed_ns);
        info->mttcg_elapsed_ns = info->virtual_ns - info->skew_elapsed_ns;
    }
    info->global_icount = global_icount;
    info->window_insns = window;
    info->model_ns = skew_enabled() ? qatomic_read_i64(&model_ns) :
                     info->virtual_ns;
    info->visible_bias_ns = info->virtual_ns - info->model_ns;
    info->visible_slope_q32 = qatomic_read_u64(&visible_slope_q32);
    tail = &info->cpus;
    CPU_FOREACH(cpu) {
        SkewCpuInfo *state = g_new0(SkewCpuInfo, 1);
        SkewCpuInfoList *entry = g_new0(SkewCpuInfoList, 1);

        state->cpu_index = cpu->cpu_index;
        state->raw_icount = qatomic_read_u64(&cpu->skew_raw_icount);
        state->local_icount = cpu->skew_logical_base +
                             state->raw_icount - cpu->skew_raw_base;
        state->active = cpu->skew_active;
        state->waiting = cpu->skew_waiting;
        state->halted = qatomic_read(&cpu->halted);
        state->budget_enabled = exec_budget_enabled(cpu);
        entry->value = state;
        *tail = entry;
        tail = &entry->next;
    }
    return info;
}

/* 把本次活跃期的实际增量映射到共同进度；成员和基准受 BQL 保护。 */
static uint64_t logical_count(CPUState *cpu)
{
    return cpu->skew_logical_base +
           (qatomic_read_u64(&cpu->skew_raw_icount) - cpu->skew_raw_base);
}

static void skew_momentum_reset(int64_t now)
{
    memset(momentum_history_slope, 0, sizeof(momentum_history_slope));
    momentum_history_pos = 0;
    momentum_history_count = 0;
    prev_global_icount = 0;
    qatomic_set_i64(&model_ns, now);
    qatomic_set_i64(&visible_ns, now);
    qatomic_set_i64(&anchor_visible_ns, now);
    qatomic_set_i64(&last_update_elapsed_ns, cpu_get_clock());
    qatomic_set_u64(&visible_slope_q32, 0);
}

static uint64_t skew_momentum_add(uint64_t delta_icount, uint64_t delta_t_ns)
{
    __uint128_t weighted = 0;
    uint64_t period_budget, effective, delta_ns, sample_slope;
    uint64_t weight = SKEW_SLOPE_ONE, weights = 0;
    unsigned i, pos;

    if (!delta_t_ns) {
        return qatomic_read_u64(&visible_slope_q32);
    }
    period_budget = muldiv64(sim_ips, delta_t_ns, NANOSECONDS_PER_SECOND);
    effective = MIN(delta_icount, period_budget);
    delta_ns = muldiv64(effective, NANOSECONDS_PER_SECOND, sim_ips);
    sample_slope = skew_ratio_q32(delta_ns, delta_t_ns);
    momentum_history_slope[momentum_history_pos] = sample_slope;
    momentum_history_pos = (momentum_history_pos + 1) %
                           SKEW_MOMENTUM_HISTORY;
    momentum_history_count = MIN(momentum_history_count + 1,
                                 SKEW_MOMENTUM_HISTORY);

    pos = momentum_history_pos;
    for (i = 0; i < momentum_history_count; i++) {
        pos = (pos + SKEW_MOMENTUM_HISTORY - 1) %
              SKEW_MOMENTUM_HISTORY;
        weighted += (__uint128_t)momentum_history_slope[pos] * weight;
        weights += weight;
        weight = muldiv64(weight, SKEW_BETA_NUM, SKEW_BETA_DEN);
    }
    if (!weights) {
        return 0;
    }
    return weighted / weights;
}

static uint64_t skew_feedback_slope(uint64_t momentum, int64_t bias,
                                    uint64_t delta_t_ns)
{
    uint64_t correction;

    correction = skew_ratio_q32(bias < 0 ? -bias : bias,
                                MAX(delta_t_ns, 1));
    correction = muldiv64(correction, SKEW_FEEDBACK_NUM,
                          SKEW_FEEDBACK_DEN);
    if (bias > 0) {
        return correction >= momentum ? 0 : momentum - correction;
    }
    return MIN(momentum + correction, SKEW_SLOPE_ONE);
}

/* BQL serializes membership, rebasing, and the sole clock writer. */
/* 协调器周期：采样各 CPU、推进全局时间、处理空闲跳时、唤醒边界等待者。 */
static void skew_update(void *opaque)
{
    CPUState *cpu;
    uint64_t candidate = UINT64_MAX;
    uint64_t completed = global_icount;
    unsigned active = 0;
    int64_t now, deadline = -1, visible, lower, upper;
    uint64_t delta_icount, slope, delta_t_ns;
    int64_t elapsed_now;
    int64_t old_ns = skew_get_clock();
    int64_t host_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);

    assert(bql_locked());
    /* VM 暂停时不推进时间；运行状态回调会在恢复时重新安排协调器。 */
    if (!skew_enabled() || !runstate_is_running()) {
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

    /* 严格模型时间仍只由 global 指令进度和空闲跳时决定。 */
    now = start_ns + warp_ns +
          muldiv64(global_icount, NANOSECONDS_PER_SECOND, sim_ips);

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
            trace_skew_warp(host_ns, global_icount, now, deadline);
        }
    }

    /*
     * 当前周期先结算旧斜率已经对外暴露的时间，再用新 global 样本生成
     * 下一周期斜率。window 只反馈偏差并施加硬边界，不参与动量输入。
     */
    visible = skew_get_clock();
    elapsed_now = cpu_get_clock();
    delta_t_ns = MAX(elapsed_now -
                     qatomic_read_i64(&last_update_elapsed_ns), 0);
    delta_icount = global_icount - prev_global_icount;
    prev_global_icount = global_icount;
    slope = skew_momentum_add(delta_icount, delta_t_ns);
    slope = skew_feedback_slope(slope, visible - now, delta_t_ns);
    if (active) {
        slope = MAX(slope, SKEW_ACTIVE_SLOPE_FLOOR);
    } else {
        /* 全空闲只通过既有 deadline warp 推进，不能按宿主时间漂移。 */
        slope = 0;
    }
    lower = MAX(now - (int64_t)window_ns, 0);
    upper = now + window_ns;
    visible = MIN(MAX(MAX(visible, lower), qatomic_read_i64(&visible_ns)),
                  upper);
    seqlock_write_begin(&clock_seqlock);
    qatomic_set_i64(&model_ns, now);
    qatomic_set_i64(&visible_ns, visible);
    qatomic_set_i64(&anchor_visible_ns, visible);
    qatomic_set_i64(&last_update_elapsed_ns, elapsed_now);
    qatomic_set_u64(&visible_slope_q32, slope);
    qatomic_set_i64(&skew_elapsed_ns, visible - mttcg_elapsed_ns);
    seqlock_write_end(&clock_seqlock);
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
    if (running && skew_enabled()) {
        timer_mod(coordinator, qemu_clock_get_ns(QEMU_CLOCK_REALTIME));
    } else {
        timer_del(coordinator);
    }
}

/* 验证单位换算和计数范围；失败时不启用 skew。 */
bool skew_init(uint64_t ns, uint64_t ips, uint64_t update_ns, bool defer,
               Error **errp)
{
    /* Bound products, signed timer arithmetic, and reject zero budgets. */
    if (!ips || ips > NANOSECONDS_PER_SECOND * 1000ULL ||
        !ns || ns > NANOSECONDS_PER_SECOND ||
        !update_ns || update_ns > NANOSECONDS_PER_SECOND) {
        error_setg(errp, "skew and skew-update must be in 1..1000000000 ns; "
                   "skew-ips must be in 1..1000000000000");
        return false;
    }
    /* 只有滑窗需要换算成指令数；宿主轮询间隔不参与执行预算计算。 */
    window = muldiv64(ns, ips, NANOSECONDS_PER_SECOND);
    if (!window) {
        error_setg(errp, "skew must cover at least one instruction");
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
    window_ns = ns;
    update_interval = update_ns;
    coordinator = timer_new_ns(QEMU_CLOCK_REALTIME, skew_update, NULL);
    qemu_add_vm_change_state_handler(skew_vm_state, NULL);
    skew_momentum_reset(0);
    qatomic_set(&use_skew, !defer);
    return true;
}

/* 双向切换只搬移时间贡献；停核结算后的领先尾部不强行推进共享时间。 */
static SkewClockInfo *skew_switch(bool enable, Error **errp)
{
    CPUState *cpu;
    bool running;
    int ret;

    assert(bql_locked());
    if (!skew_configured()) {
        error_setg(errp, "Configure -accel tcg,thread=multi,skew=NS first");
        return NULL;
    }
    if (skew_enabled() == enable) {
        return qmp_query_skew_clock(errp);
    }
    running = runstate_is_running();
    if (!running && !runstate_check(RUN_STATE_PAUSED) &&
        !runstate_check(RUN_STATE_PRELAUNCH)) {
        error_setg(errp, "Clock switch requires running, paused or prelaunch state");
        return NULL;
    }
    if (running) {
        ret = vm_stop(RUN_STATE_PAUSED);
        if (ret < 0) {
            error_setg_errno(errp, -ret, "Could not pause VM for clock switch");
            return NULL;
        }
    }
    /* 两种模式读到的时间均已冻结；保存切换点供新阶段计时。 */
    start_ns = skew_get_clock();
    timer_del(coordinator);
    seqlock_write_begin(&clock_seqlock);
    if (enable) {
        qatomic_set_i64(&mttcg_elapsed_ns,
                       start_ns - qatomic_read_i64(&skew_elapsed_ns));
        skew_momentum_reset(start_ns);
    } else {
        /* skew_elapsed 已是最终发布值，不把未发布的 CPU 尾部再加进来。 */
        qatomic_set_i64(&skew_elapsed_ns,
                        start_ns - qatomic_read_i64(&mttcg_elapsed_ns));
        qatomic_set_i64(&mttcg_start_clock, cpu_get_clock());
        qatomic_set_i64(&model_ns, start_ns);
        qatomic_set_i64(&visible_ns, start_ns);
        qatomic_set_u64(&visible_slope_q32, 0);
    }
    qatomic_set(&use_skew, enable);
    seqlock_write_end(&clock_seqlock);
    warp_ns = 0;
    global_icount = 0;
    CPU_FOREACH(cpu) {
        qatomic_set_u64(&cpu->skew_raw_icount, 0);
        cpu->skew_raw_base = 0;
        cpu->skew_logical_base = 0;
        cpu->skew_budget_global = 0;
        cpu->skew_budget = 0;
        cpu->skew_active = false;
        cpu->skew_waiting = false;
        exec_budget_set(cpu, 0);
        cpu->execution_budget_ops = enable ? &skew_budget_ops : NULL;
        if (enable) {
            tcg_cflags_set(cpu, CF_USE_ICOUNT);
        } else {
            cpu->tcg_cflags &= ~CF_USE_ICOUNT;
        }
        cpu->cflags_next_tb = -1;
    }
    tb_flush__exclusive_or_serial();
    qemu_clock_notify(QEMU_CLOCK_VIRTUAL);
    if (running) {
        vm_start();
    }
    return qmp_query_skew_clock(errp);
}

SkewClockInfo *qmp_skew_start(Error **errp)
{
    return skew_switch(true, errp);
}

SkewClockInfo *qmp_skew_stop(Error **errp)
{
    return skew_switch(false, errp);
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
        int64_t visible = skew_get_clock();

        cpu->skew_raw_base = qatomic_read_u64(&cpu->skew_raw_icount);
        cpu->skew_logical_base = global_icount;
        cpu->skew_active = true;
        /*
         * idle 历史允许动量衰减为零；CPU 恢复执行后立即提供一个小斜率，
         * 避免在下一次协调样本形成前连续读取相同时间。硬 window 仍限幅。
         */
        if (qatomic_read_u64(&visible_slope_q32) <
            SKEW_ACTIVE_SLOPE_FLOOR) {
            seqlock_write_begin(&clock_seqlock);
            qatomic_set_i64(&anchor_visible_ns, visible);
            qatomic_set_i64(&last_update_elapsed_ns, cpu_get_clock());
            qatomic_set_u64(&visible_slope_q32,
                            SKEW_ACTIVE_SLOPE_FLOOR);
            seqlock_write_end(&clock_seqlock);
        }
        trace_skew_cpu(qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                       cpu->cpu_index, true, cpu->skew_raw_icount,
                       logical_count(cpu), global_icount);
    }
    /* 只受剩余窗口和 TCG 16 位装载上限约束，无独立的执行分段策略。 */
    lead = logical_count(cpu) - global_icount;
    /* 常驻检查：禁止无符号减法下溢后发放错误预算。 */
    if (unlikely(lead > window)) {
        error_report("skew: CPU %d window exceeded before execution: "
                     "lead=%" PRIu64 " window=%" PRIu64,
                     cpu->cpu_index, lead, window);
        abort();
    }
    /* 保存发放时的全局进度；无 BQL 结算时不读取并发变化的 global。 */
    cpu->skew_budget_global = global_icount;
    cpu->skew_budget = MIN(UINT16_MAX, window - lead);
    exec_budget_set(cpu, cpu->skew_budget);
}

/*
 * 完成量 = 发放预算 - 剩余预算；异常回退已修正剩余值。
 * 原子发布后清空预算，使复位等路径重复结算时不会重复累计。
 */
void skew_cpu_account(CPUState *cpu)
{
    uint32_t remaining = exec_budget_remaining(cpu);
    uint64_t executed, local, lead;

    /* 不依赖 assert，关闭断言的构建也拒绝损坏的预算结算。 */
    if (unlikely(remaining > cpu->skew_budget)) {
        error_report("skew: CPU %d invalid budget: budget=%u remaining=%u",
                     cpu->cpu_index, cpu->skew_budget, remaining);
        abort();
    }
    executed = cpu->skew_budget - remaining;
    local = logical_count(cpu) + executed;
    lead = local - cpu->skew_budget_global;
    /* 发布前检查发放时的窗口，避免协调器推进 global 掩盖越界。 */
    if (unlikely(lead > window)) {
        error_report("skew: CPU %d execution window exceeded: "
                     "local=%" PRIu64 " grant_global=%" PRIu64
                     " lead=%" PRIu64 " window=%" PRIu64
                     " budget=%u remaining=%u",
                     cpu->cpu_index, local, cpu->skew_budget_global,
                     lead, window, cpu->skew_budget, remaining);
        abort();
    }
    /* Publish only completed instructions, including TB unwind corrections. */
    qatomic_set_u64(&cpu->skew_raw_icount,
                    qatomic_read_u64(&cpu->skew_raw_icount) + executed);
    cpu->skew_budget = 0;
    exec_budget_set(cpu, 0);
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

static bool skew_budget_exhausted(CPUState *cpu)
{
    return exec_budget_remaining(cpu) == 0;
}

/*
 * TB 预算到期不发放新窗口，也不推进 global time。保留本轮剩余额度，
 * 由公共层截短 TB；整轮耗尽后仍返回 MTTCG 做 raw 结算和窗口等待。
 */
static uint16_t skew_budget_expired(CPUState *cpu)
{
    return exec_budget_remaining(cpu);
}

const TCGExecutionBudgetOps skew_budget_ops = {
    .exhausted = skew_budget_exhausted,
    .expired = skew_budget_expired,
    .before_reset = skew_cpu_account,
};

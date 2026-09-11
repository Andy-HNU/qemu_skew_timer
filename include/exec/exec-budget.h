/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef EXEC_EXEC_BUDGET_H
#define EXEC_EXEC_BUDGET_H

#include "hw/core/cpu.h"

/*
 * 公共 TCG 执行预算协议，不持有任何时间模型的时钟或长预算状态。
 * 模型的调度器负责生成预算和执行后结算；公共 CPU 循环只派发到期事件。
 * 回调由当前 vCPU 线程调用，不获取 BQL，也不在公共 TB 路径中等待。
 */
typedef struct TCGExecutionBudgetOps {
    /* 模型的整轮预算是否耗尽（可能还存在模型私有的扩展预算）。 */
    bool (*exhausted)(CPUState *cpu);
    /*
     * TB 无法在当前额度内执行时调用；返回并装载下一 TB 可用的 16 位额度。
     * 返回零表示应退出执行循环；非零额度允许公共层截短 TB。
     */
    uint16_t (*expired)(CPUState *cpu);
    /* 可选：复位清除执行状态之前结算；保持各模型原有复位语义。 */
    void (*before_reset)(CPUState *cpu);
} TCGExecutionBudgetOps;

static inline bool exec_budget_enabled(CPUState *cpu)
{
    return cpu->execution_budget_ops != NULL;
}

/*
 * 兼容既有 TCG 生成代码的递减器布局。历史字段名只留在适配层；
 * 低 16 位是公共执行额度，高 16 位仍由通用退出/中断通知使用。
 */
static inline uint16_t exec_budget_remaining(CPUState *cpu)
{
    return cpu->neg.icount_decr.u16.low;
}

static inline void exec_budget_set(CPUState *cpu, uint32_t insns)
{
    assert(insns <= UINT16_MAX);
    cpu->neg.icount_decr.u16.low = insns;
}

static inline bool exec_budget_exhausted(CPUState *cpu)
{
    return exec_budget_enabled(cpu) &&
           cpu->execution_budget_ops->exhausted(cpu);
}

static inline uint16_t exec_budget_expired(CPUState *cpu)
{
    assert(exec_budget_enabled(cpu));
    return cpu->execution_budget_ops->expired(cpu);
}

static inline void exec_budget_before_reset(CPUState *cpu)
{
    if (exec_budget_enabled(cpu) && cpu->execution_budget_ops->before_reset) {
        cpu->execution_budget_ops->before_reset(cpu);
    }
}

#endif /* EXEC_EXEC_BUDGET_H */

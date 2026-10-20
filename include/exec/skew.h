/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef EXEC_SKEW_H
#define EXEC_SKEW_H

/* 编译边界：非 TCG 或用户态模拟恒为关闭，避免依赖系统模拟实现。 */
#ifdef CONFIG_TCG
extern bool use_skew;
/* Immutable after accelerator initialization. */
extern bool use_skew_counter_fast;
#define skew_enabled() qatomic_read(&use_skew)
#define skew_counter_fast_enabled() (skew_enabled() && use_skew_counter_fast)
#else
#define skew_enabled() false
#define skew_counter_fast_enabled() false
#endif

#if defined(COMPILING_PER_TARGET) || defined(COMPILING_SYSTEM_VS_USER)
#ifdef CONFIG_USER_ONLY
#undef skew_enabled
#define skew_enabled() false
#undef skew_counter_fast_enabled
#define skew_counter_fast_enabled() false
#endif
#endif

/* 初始化参数、协调器和迁移阻止器；defer 时等待 QMP 才启用预算。 */
typedef enum SkewPublishMode {
    SKEW_PUBLISH_PERIODIC,
    SKEW_PUBLISH_NOTIFY,
    SKEW_PUBLISH_INLINE,
} SkewPublishMode;

bool skew_init(uint64_t ns, uint64_t ips, uint64_t update_ns, bool defer,
               SkewPublishMode publish_mode, bool fresh_budget,
               bool counter_fast, bool exponential, bool adaptive,
               Error **errp);
/* 已配置但可尚未启用；用于安装时钟路由和观测接口。 */
bool skew_configured(void);
/*
 * 统一虚拟纳秒时间：MTTCG 累计时钟或 skew 插值/CAS-max 发布。
 * 调用者为 vCPU 执行上下文或持有 BQL 的路径；切换先停稳全部 vCPU，
 * 再持有 BQL 重锚。正常读取不登记、不等待切换专用同步对象。
 */
int64_t skew_get_clock(void);
/* 向机器对象注册只读 skew-time 属性。 */
void skew_register_clock(Object *obj);
/* 向 CPU 对象注册只读 skew-raw-icount 属性。 */
void skew_register_cpu(CPUState *cpu);
/* 持有 BQL：更新活动集合，移除真正停止或空闲的 CPU。 */
void skew_cpu_idle(CPUState *cpu);
/* 持有 BQL：重定位重新加入的 CPU，并按剩余窗口分配预算。 */
void skew_cpu_prepare(CPUState *cpu);
/* 由 vCPU 执行线程结算本轮完成量并清空预算。 */
void skew_cpu_account(CPUState *cpu);
/* 不持有 BQL：纯预算退出可等待窗口并续配；控制请求返回 false。 */
bool skew_cpu_continue(CPUState *cpu);
/* TCG 发布异步退出标志后调用；唤醒窗口等待，内部只获取进度锁。 */
void skew_cpu_kick(CPUState *cpu);

/* 此时间模型提供的公共执行预算回调。 */
extern const struct TCGExecutionBudgetOps skew_budget_ops;

#endif

/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef EXEC_SKEW_H
#define EXEC_SKEW_H

/* 编译边界：非 TCG 或用户态模拟恒为关闭，避免依赖系统模拟实现。 */
#ifdef CONFIG_TCG
extern bool use_skew;
#define skew_enabled() (use_skew)
#else
#define skew_enabled() false
#endif

#if defined(COMPILING_PER_TARGET) || defined(COMPILING_SYSTEM_VS_USER)
#ifdef CONFIG_USER_ONLY
#undef skew_enabled
#define skew_enabled() false
#endif
#endif

/* 初始化参数、协调器和迁移阻止器；成功后才打开模式开关。 */
bool skew_init(uint64_t ns, uint64_t ips, uint64_t update_ns, Error **errp);
/* 原子读取协调器发布的统一虚拟纳秒时间。 */
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
/* 持有 BQL：到达窗口边界时等待；等待操作会释放并重新取得 BQL。 */
void skew_cpu_wait(CPUState *cpu);

#endif

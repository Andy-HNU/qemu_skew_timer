/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef EXEC_SKEW_H
#define EXEC_SKEW_H

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

bool skew_init(uint64_t ns, uint64_t ips, uint64_t update_ns, Error **errp);
int64_t skew_get_clock(void);
void skew_register_clock(Object *obj);
void skew_register_cpu(CPUState *cpu);
void skew_cpu_idle(CPUState *cpu);
void skew_cpu_prepare(CPUState *cpu);
void skew_cpu_account(CPUState *cpu);
void skew_cpu_wait(CPUState *cpu);

#endif

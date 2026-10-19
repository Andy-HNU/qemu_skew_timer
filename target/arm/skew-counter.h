/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef ARM_SKEW_COUNTER_H
#define ARM_SKEW_COUNTER_H

/*
 * Opt-in system TCG path, called only by the owning vCPU with Skew active.
 * ARM_CP_IO translation, access checks and timer writes are unchanged.
 */
bool arm_skew_counter_read(CPUARMState *env, const ARMCPRegInfo *ri,
                           uint64_t *value);

#endif

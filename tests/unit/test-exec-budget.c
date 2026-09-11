/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "exec/exec-budget.h"

/* A model with private reserve, deliberately separate from CPU icount state. */
static unsigned reserve, expired_calls, reset_calls;

static bool model_exhausted(CPUState *cpu)
{
    return exec_budget_remaining(cpu) == 0 && reserve == 0;
}

static uint16_t model_expired(CPUState *cpu)
{
    expired_calls++;
    exec_budget_set(cpu, reserve);
    reserve = 0;
    return exec_budget_remaining(cpu);
}

static void model_before_reset(CPUState *cpu)
{
    reset_calls++;
    exec_budget_set(cpu, 0);
}

static const TCGExecutionBudgetOps model = {
    .exhausted = model_exhausted,
    .expired = model_expired,
    .before_reset = model_before_reset,
};

static void test_disabled(void)
{
    g_autofree CPUState *cpu = g_new0(CPUState, 1);

    /* A zero decrementer must not stop an unbudgeted CPU. */
    g_assert_false(exec_budget_enabled(cpu));
    g_assert_false(exec_budget_exhausted(cpu));
    exec_budget_before_reset(cpu);
}

static void test_refill(void)
{
    g_autofree CPUState *cpu = g_new0(CPUState, 1);

    cpu->execution_budget_ops = &model;
    reserve = 123;
    expired_calls = 0;
    /* Zero in the common slot need not mean that the model is exhausted. */
    g_assert_false(exec_budget_exhausted(cpu));
    g_assert_cmpuint(exec_budget_expired(cpu), ==, 123);
    g_assert_cmpuint(expired_calls, ==, 1);
    g_assert_false(exec_budget_exhausted(cpu));
    exec_budget_set(cpu, 0);
    g_assert_true(exec_budget_exhausted(cpu));
    g_assert_cmpuint(exec_budget_expired(cpu), ==, 0);
    g_assert_cmpuint(expired_calls, ==, 2);
}

static void test_interrupt_preserved(void)
{
    g_autofree CPUState *cpu = g_new0(CPUState, 1);

    /* Loading/refunding a budget must not clear an asynchronous exit kick. */
    cpu->neg.icount_decr.u16.high = 0xffff;
    exec_budget_set(cpu, UINT16_MAX);
    g_assert_cmpuint(exec_budget_remaining(cpu), ==, UINT16_MAX);
    exec_budget_set(cpu, 7);
    exec_budget_set(cpu, exec_budget_remaining(cpu) + 3);
    g_assert_cmpuint(exec_budget_remaining(cpu), ==, 10);
    g_assert_cmpuint(cpu->neg.icount_decr.u16.high, ==, 0xffff);
}

static void test_reset(void)
{
    g_autofree CPUState *cpu = g_new0(CPUState, 1);
    TCGExecutionBudgetOps without_reset = model;

    cpu->execution_budget_ops = &model;
    reset_calls = 0;
    exec_budget_set(cpu, 17);
    exec_budget_before_reset(cpu);
    g_assert_cmpuint(reset_calls, ==, 1);
    g_assert_cmpuint(exec_budget_remaining(cpu), ==, 0);
    g_assert_true(cpu->execution_budget_ops == &model);

    without_reset.before_reset = NULL;
    cpu->execution_budget_ops = &without_reset;
    exec_budget_set(cpu, 9);
    exec_budget_before_reset(cpu);
    g_assert_cmpuint(reset_calls, ==, 1);
    g_assert_cmpuint(exec_budget_remaining(cpu), ==, 9);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/exec-budget/disabled", test_disabled);
    g_test_add_func("/exec-budget/model-refill", test_refill);
    g_test_add_func("/exec-budget/interrupt-preserved", test_interrupt_preserved);
    g_test_add_func("/exec-budget/reset", test_reset);
    return g_test_run();
}

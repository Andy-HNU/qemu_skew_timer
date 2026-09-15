/* SPDX-License-Identifier: GPL-2.0-or-later */
.section .text.boot, "ax"
.global _start, secondary_start
_start:
secondary_start:
    mrs x0, mpidr_el1
    /* virt encodes groups of AFFINITY_SIZE CPUs in MPIDR Aff1. */
    ubfx x2, x0, #8, #8
    and x0, x0, #255
    mov x3, #AFFINITY_SIZE
    madd x0, x2, x3, x0
    ldr x1, =stacks
    add x1, x1, #4096
    add x1, x1, x0, lsl #12
    mov sp, x1
    msr daifset, #15
    bl guest_main
1:  wfi
    b 1b
.text
.global compute, marker, marker_store
/* 32 guest instructions/iteration; two dependent Fibonacci accumulators. */
compute:
    mov x1, #1
    mov x2, #1
1:  .rept 15
    add x1, x1, x2
    add x2, x2, x1
    .endr
    subs x0, x0, #1
    b.ne 1b
    mov x0, x1
    ret
/* Only this MMIO store is instrumented by the host timing plugin. */
marker:
    mov x1, #0x09000000
marker_store:
    str w0, [x1]
    ret
.bss
.balign 64
stacks:
    .skip CPUS * 4096

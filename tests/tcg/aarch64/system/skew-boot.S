/* SPDX-License-Identifier: GPL-2.0-or-later */
/* 双核裸机入口：按 MPIDR 选择每核 4KiB 栈，设置异常向量后进入 C 测试。 */
.section .text.boot, "ax"
.global _start, secondary_start
_start:
secondary_start:
    mrs x0, mpidr_el1
    and x0, x0, #0xff
    ldr x1, =stacks
    add x1, x1, #4096
    add x1, x1, x0, lsl #12
    mov sp, x1
    msr daifset, #0xf
    adr x1, vectors
    msr vbar_el1, x1
    bl guest_main
1:  wfi
    b 1b

/* Pure directly linked loops: exactly 2*n or 32*n body instructions.
 * Counter reads and ISBs are outside the body; no MMIO or host calls. */
.global short_loop, long_loop, short_end, long_end
/* 每次迭代 subs+b.ne 共 2 条；起止标签用于 GDB 精确 raw 计数。 */
short_loop:
    isb
    mrs x1, cntpct_el0
1:  subs x0, x0, #1
    b.ne 1b
short_end:
    isb
    mrs x0, cntpct_el0
    sub x0, x0, x1
    ret
/* 每次迭代 30 条 nop 加循环控制共 32 条，用于比较不同 TB 长度。 */
long_loop:
    isb
    mrs x1, cntpct_el0
1:  .rept 30
    nop
    .endr
    subs x0, x0, #1
    b.ne 1b
long_end:
    isb
    mrs x0, cntpct_el0
    sub x0, x0, x1
    ret
.global fault_probe, fault_end
/* 混合 UART MMIO、普通指令和 SVC 异常，检查 TB 回退后的精确计数。 */
fault_probe:
    mov x9, #0x09000000
    ldr w10, [x9, #0x18]
    .rept 30
    nop
    .endr
    svc #0
    nop
fault_end:
    ret
.balign 2048
/* EL1 当前栈 SPx 的同步异常与 IRQ 槽分别位于 0x200 和 0x280。 */
vectors:
    .skip 0x200
    b svc_handler
    .skip 0x7c
    b irq_handler
/* 直接返回 ELR，继续执行 SVC 之后的指令，用于测试异常结算。 */
svc_handler:
    eret
/*
 * 记录实际进入 IRQ 的计数值，读 GIC IAR、关闭物理定时器并写 EOI。
 * 保存和恢复本处理器使用的寄存器，再向 C 主循环发布完成标志。
 */
irq_handler:
    stp x9, x10, [sp, #-32]!
    stp x11, x12, [sp, #16]
    mrs x9, cntpct_el0
    adrp x10, irq_time
    str x9, [x10, :lo12:irq_time]
    mov x10, #0x08010000
    ldr w11, [x10, #12]
    msr cntp_ctl_el0, xzr
    isb
    str w11, [x10, #16]
    adrp x10, irq_id
    str x11, [x10, :lo12:irq_id]
    adrp x10, irq_seen
    mov x9, #1
    str x9, [x10, :lo12:irq_seen]
    ldp x11, x12, [sp, #16]
    ldp x9, x10, [sp], #32
    eret
.section .bss
.balign 16
/* 为两个 vCPU 预留独立栈空间，避免并行执行互相覆盖。 */
stacks:
    .skip 8192

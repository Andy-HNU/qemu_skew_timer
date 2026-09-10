/* SPDX-License-Identifier: GPL-2.0-or-later */
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
vectors:
    .skip 0x200
    b svc_handler
    .skip 0x7c
    b irq_handler
svc_handler:
    eret
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
stacks:
    .skip 8192

/* SPDX-License-Identifier: GPL-2.0-or-later */
/* 裸机 ELF 从汇编启动入口进入；地址匹配 virt 机器 RAM 内的加载位置。 */
ENTRY(_start)
SECTIONS {
    /* 先放启动段，再放普通代码、常量、可写数据和零初始化区。 */
    . = 0x40080000;
    .text : { *(.text.boot) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }
    /* 裸机验收不依赖宿主工具链的注释、异常展开和 note 元数据。 */
    /DISCARD/ : { *(.comment) *(.eh_frame*) *(.note*) }
}

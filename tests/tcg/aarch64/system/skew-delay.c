/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <stdlib.h>
#include <errno.h>
#include <limits.h>
#include <time.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
/* 默认拖慢 CPU1；回调计数只在目标 CPU 上递增，用宿主延迟制造进度不均。 */
static unsigned target_cpu = 1;
static unsigned count;
static long delay_ns = 100000;

/* 每 128 次目标 CPU 的 TB 执行休眠一次，不修改 guest 指令或虚拟时间。 */
static void delay(unsigned cpu_index, void *opaque)
{
    if (cpu_index == target_cpu && ++count % 128 == 0) {
        struct timespec duration = { .tv_nsec = delay_ns };

        nanosleep(&duration, NULL);
    }
}

/* 给每个翻译块安装执行回调，使直接链接的 TB 也能施加宿主延迟。 */
static void translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    qemu_plugin_register_vcpu_tb_exec_cb(tb, delay,
                                        QEMU_PLUGIN_CB_NO_REGS, NULL);
}

/* 解析可选 cpu= 与 ns=；拒绝非法数字和超出 nanosleep 纳秒字段的范围。 */
QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    char *end;
    unsigned long long parsed;

    errno = 0;
    if (argc > 0) {
        if (!g_str_has_prefix(argv[0], "cpu=")) {
            return -1;
        }
        parsed = g_ascii_strtoull(argv[0] + 4, &end, 10);
        if (errno || end == argv[0] + 4 || *end || parsed > UINT_MAX) {
            return -1;
        }
        target_cpu = parsed;
    }
    if (argc > 1) {
        if (!g_str_has_prefix(argv[1], "ns=")) {
            return -1;
        }
        delay_ns = g_ascii_strtoll(argv[1] + 3, &end, 10);
        if (errno || end == argv[1] + 3 || *end) {
            return -1;
        }
    }
    if (argc > 2 || delay_ns < 0 || delay_ns >= 1000000000) {
        return -1;
    }
    /* 参数验证通过才注册插件；此插件仅用于验收时模拟调度不均衡。 */
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate);
    return 0;
}

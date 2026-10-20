/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Only the two marker stores have runtime callbacks. */
#include <qemu-plugin.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
static uint64_t marker_pc, done_pc;
static uint64_t start_ns;
static struct rusage start_usage;
static atomic_uint state;

static uint64_t monotonic_ns(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
        abort();
    }
    return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static double seconds(struct timeval value)
{
    return value.tv_sec + value.tv_usec / 1000000.0;
}

static void mark(unsigned cpu, qemu_plugin_meminfo_t info,
                 uint64_t vaddr, void *opaque)
{
    /* v7 has no memory-value API; distinct store PCs identify START/DONE. */
    if (vaddr != 0x09000000 || qemu_plugin_mem_size_shift(info) != 2 ||
        !qemu_plugin_mem_is_store(info)) {
        abort();
    }
    if (!opaque && atomic_load(&state) == 0 && cpu == 0) {
        if (getrusage(RUSAGE_SELF, &start_usage)) {
            abort();
        }
        start_ns = monotonic_ns();
        atomic_store_explicit(&state, 1, memory_order_release);
    } else if (opaque &&
               atomic_load_explicit(&state, memory_order_acquire) == 1) {
        struct rusage end_usage;
        uint64_t end_ns = monotonic_ns();

        if (getrusage(RUSAGE_SELF, &end_usage)) {
            abort();
        }
        atomic_store(&state, 2);
        fprintf(stderr,
                "REPRO_INTERVAL {\"host_start_ns\":%" PRIu64
                ",\"host_end_ns\":%" PRIu64
                ",\"user_seconds\":%.6f,\"system_seconds\":%.6f"
                ",\"voluntary_switches\":%ld,\"involuntary_switches\":%ld}\n",
                start_ns, end_ns,
                seconds(end_usage.ru_utime) - seconds(start_usage.ru_utime),
                seconds(end_usage.ru_stime) - seconds(start_usage.ru_stime),
                end_usage.ru_nvcsw - start_usage.ru_nvcsw,
                end_usage.ru_nivcsw - start_usage.ru_nivcsw);
        fflush(stderr);
    } else {
        abort();
    }
}

static void translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    for (size_t i = 0; i < qemu_plugin_tb_n_insns(tb); i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);

        if (qemu_plugin_insn_vaddr(insn) == marker_pc) {
            qemu_plugin_register_vcpu_mem_cb(insn, mark, QEMU_PLUGIN_CB_NO_REGS,
                                             QEMU_PLUGIN_MEM_W, NULL);
        } else if (qemu_plugin_insn_vaddr(insn) == done_pc) {
            qemu_plugin_register_vcpu_mem_cb(insn, mark, QEMU_PLUGIN_CB_NO_REGS,
                                             QEMU_PLUGIN_MEM_W, (void *)1);
        }
    }
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                         const qemu_info_t *info,
                                         int argc, char **argv)
{
    char *end;

    if (argc != 2 || !info->system_emulation ||
        strncmp(argv[0], "pc=", 3) || strncmp(argv[1], "done=", 5)) {
        return -1;
    }
    errno = 0;
    /* Standalone plugin: use libc and check both range and trailing input. */
    marker_pc = strtoull(argv[0] + 3, &end, 16);
    if (errno || end == argv[0] + 3 || *end || !marker_pc) {
        return -1;
    }
    errno = 0;
    done_pc = strtoull(argv[1] + 5, &end, 16);
    if (errno || end == argv[1] + 5 || *end || !done_pc || done_pc == marker_pc) {
        return -1;
    }
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate);
    return 0;
}

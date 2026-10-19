/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Minimal Linux /init that initializes and consumes jitterentropy in skew. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/if_alg.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef JITTER_READS
#define JITTER_READS 256
#endif

static unsigned completed_reads;

#ifdef JITTER_HEALTH_PROBES
static int probes_ready;

static void dump_health_trace(void)
{
    char buffer[4096];
    ssize_t size;
    int fd;

    if (!probes_ready) {
        return;
    }
    fd = open("/trace/trace", O_RDONLY);
    if (fd < 0) {
        perror("JITTER_HEALTH_TRACE_UNAVAILABLE");
        return;
    }
    puts("JITTER_HEALTH_TRACE_BEGIN");
    while ((size = read(fd, buffer, sizeof(buffer))) > 0) {
        fwrite(buffer, 1, size, stdout);
    }
    close(fd);
    puts("JITTER_HEALTH_TRACE_END");
}
#endif

static void fail(const char *why)
{
    int saved_errno = errno;

#ifdef JITTER_HEALTH_PROBES
    dump_health_trace();
#endif
    printf("JITTER_TEST_FAIL %s errno=%d (%s) completed_reads=%u\n",
           why, saved_errno, strerror(saved_errno), completed_reads);
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

#ifdef JITTER_HEALTH_PROBES
/* Diagnostic-only offsets for the exact module hash checked by the harness. */
static void setup_health_probes(void)
{
    static const char *const events[] = {
        "p:jitter/retry jitterentropy_rng:jent_read_entropy+0x70 "
        "prev=+16(+40($stack)):u64 delta=+24(+40($stack)):u64 "
        "delta2=+32(+40($stack)):s64 rct=+72(+40($stack)):u32 "
        "apt=+80(+40($stack)):u32 observations=+76(+40($stack)):u32\n",
        "p:jitter/retry_failed jitterentropy_rng:jent_read_entropy+0x7c "
        "init_rc=%x0:u32 prev=+16(+40($stack)):u64 "
        "rct=+72(+40($stack)):u32 apt=+80(+40($stack)):u32\n",
        "p:jitter/permanent jitterentropy_rng:jent_read_entropy+0x58 "
        "prev=+16(+40($stack)):u64 delta=+24(+40($stack)):u64 "
        "rct=+72(+40($stack)):u32 apt=+80(+40($stack)):u32\n",
        "p:jitter/init_coarse jitterentropy_rng:jent_entropy_init+0xe8 "
        "iteration=+4($stack):u32 time1=+32($stack):u64 "
        "time2=+40($stack):u64 delta=+64($stack):u64\n",
    };
    int fd;

    if (mkdir("/trace", 0700) || mount("tracefs", "/trace", "tracefs", 0, NULL)) {
        fail("health probe tracefs setup");
    }
    fd = open("/trace/kprobe_events", O_WRONLY);
    if (fd < 0) {
        fail("health probe event open");
    }
    for (unsigned i = 0; i < sizeof(events) / sizeof(events[0]); i++) {
        size_t length = strlen(events[i]);
        if (write(fd, events[i], length) != (ssize_t)length) {
            fail("health probe event registration");
        }
    }
    close(fd);
    fd = open("/trace/events/jitter/enable", O_WRONLY);
    if (fd < 0 || write(fd, "1\n", 2) != 2) {
        fail("health probe enable");
    }
    close(fd);
    probes_ready = 1;
    puts("JITTER_HEALTH_PROBES_READY");
}
#endif

static void timeout(int signal)
{
    (void)signal;
    fail("timeout");
}

static void load(const char *path)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0 || syscall(SYS_finit_module, fd, "", 0)) {
        fail(path);
    }
    close(fd);
}

int main(void)
{
    struct sockaddr_alg addr = {
        .salg_family = AF_ALG,
        .salg_type = "rng",
        .salg_name = "jitterentropy_rng",
    };
    uint8_t output[64];
    uint64_t checksum = 0;
    int alg, rng;

    setvbuf(stdout, NULL, _IONBF, 0);
    mount("proc", "/proc", "proc", 0, NULL);
    signal(SIGALRM, timeout);
    alarm(180);
    puts("LINUX_INIT_ENTERED test=jitter");
    load("/af_alg.ko");
    load("/jitterentropy_rng.ko");
    puts("JITTER_INIT_PASS");
    load("/algif_rng.ko");
#ifdef JITTER_HEALTH_PROBES
    setup_health_probes();
#endif

    alg = socket(AF_ALG, SOCK_SEQPACKET, 0);
    if (alg < 0 || bind(alg, (struct sockaddr *)&addr, sizeof(addr))) {
        fail("AF_ALG bind jitterentropy_rng");
    }
    rng = accept(alg, NULL, 0);
    if (rng < 0) {
        fail("AF_ALG accept jitterentropy_rng");
    }
    printf("JITTER_USE_BEGIN reads=%u bytes=64\n", JITTER_READS);
    for (unsigned i = 0; i < JITTER_READS; i++) {
        ssize_t got = read(rng, output, sizeof(output));
        if (got != sizeof(output)) {
            fail("AF_ALG jitterentropy read");
        }
        for (size_t j = 0; j < sizeof(output); j++) {
            checksum = checksum * 131 + output[j];
        }
        completed_reads = i + 1;
        if ((i + 1) % 64 == 0) {
            printf("JITTER_USE_PROGRESS reads=%u\n", i + 1);
        }
    }
    close(rng);
    close(alg);
    alarm(0);
    printf("JITTER_USE_PASS reads=%u checksum=%llu\n", JITTER_READS,
           (unsigned long long)checksum);
#ifdef JITTER_HEALTH_PROBES
    dump_health_trace();
#endif
    puts("JITTER_TEST_PASS");
    sync();
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

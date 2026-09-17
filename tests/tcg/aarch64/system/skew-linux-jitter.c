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
#include <sys/syscall.h>
#include <unistd.h>

static void fail(const char *why)
{
    printf("JITTER_TEST_FAIL %s errno=%d (%s)\n", why, errno,
           strerror(errno));
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

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
    puts("LINUX_INIT_ENTERED mode=skew");
    load("/af_alg.ko");
    load("/jitterentropy_rng.ko");
    puts("JITTER_INIT_PASS");
    load("/algif_rng.ko");

    alg = socket(AF_ALG, SOCK_SEQPACKET, 0);
    if (alg < 0 || bind(alg, (struct sockaddr *)&addr, sizeof(addr))) {
        fail("AF_ALG bind jitterentropy_rng");
    }
    rng = accept(alg, NULL, 0);
    if (rng < 0) {
        fail("AF_ALG accept jitterentropy_rng");
    }
    puts("JITTER_USE_BEGIN reads=256 bytes=64");
    for (int i = 0; i < 256; i++) {
        ssize_t got = read(rng, output, sizeof(output));
        if (got != sizeof(output)) {
            fail("AF_ALG jitterentropy read");
        }
        for (size_t j = 0; j < sizeof(output); j++) {
            checksum = checksum * 131 + output[j];
        }
        if ((i + 1) % 64 == 0) {
            printf("JITTER_USE_PROGRESS reads=%d\n", i + 1);
        }
    }
    close(rng);
    close(alg);
    alarm(0);
    printf("JITTER_USE_PASS reads=256 checksum=%llu\n",
           (unsigned long long)checksum);
    puts("JITTER_TEST_PASS");
    sync();
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

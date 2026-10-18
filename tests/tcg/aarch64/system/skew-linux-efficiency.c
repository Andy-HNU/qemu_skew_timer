/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Real Linux /init: every requested CPU executes and checks fixed work. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <time.h>
#include <unistd.h>

#ifndef CPUS
#define CPUS 32
#endif
#ifndef WORK
#define WORK 200000
#endif
#ifndef CONTROL
#define CONTROL 0
#endif

static pthread_barrier_t boundary;
static uint64_t completed[CPUS];

static void fail(const char *operation)
{
    printf("LINUX_EFFICIENCY_FAIL %s errno=%d\n", operation, errno);
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

static uint64_t counter(void)
{
    uint64_t result;
    __asm__ volatile("isb; mrs %0, cntvct_el0" : "=r" (result));
    return result;
}

/* Thirty dependent additions and two loop instructions per work unit. */
static uint64_t compute(uint64_t count)
{
    uint64_t result;
    __asm__ volatile("mov x1, #1\n mov x2, #1\n"
                     "1: .rept 15\n add x1, x1, x2\n add x2, x2, x1\n .endr\n"
                     "subs %1, %1, #1\n b.ne 1b\n mov %0, x1"
                     : "=r" (result), "+r" (count) : : "x1", "x2", "cc");
    return result;
}

/* Independent logarithmic Fibonacci calculation, modulo 2^64. */
static uint64_t expected(uint64_t n)
{
    uint64_t a = 0, b = 1;
    for (int bit = 63; bit >= 0; bit--) {
        uint64_t c = a * (2 * b - a), d = a * a + b * b;
        if ((n >> bit) & 1) {
            a = d;
            b = c + d;
        } else {
            a = c;
            b = d;
        }
    }
    return a;
}

static void *worker(void *opaque)
{
    int id = (intptr_t)opaque;
    cpu_set_t mask;
    uint64_t last = counter();

    CPU_ZERO(&mask);
    CPU_SET(id, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) {
        fail("pin worker");
    }
    for (int phase = 0; phase < 2; phase++) {
        uint64_t units = phase ? WORK / 4 * (id % 4 + 1) : WORK;
        pthread_barrier_wait(&boundary);
        if (phase && (id & 1)) {
            struct timespec delay = { .tv_nsec = 1000000 };
            while (nanosleep(&delay, &delay)) {
                if (errno != EINTR) {
                    fail("timer wake");
                }
            }
        }
        if (compute(units) != expected(30 * units + 1)) {
            fail("fixed work checksum");
        }
        uint64_t now = counter();
        if (now < last || sched_getcpu() != id) {
            fail("counter or CPU affinity");
        }
        last = now;
        completed[id] += units;
        pthread_barrier_wait(&boundary);
    }
    return NULL;
}

int main(void)
{
    pthread_t threads[CPUS];
    uint64_t total = 0;

    if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL)) {
        return 1;
    }
    int console = open("/dev/console", O_RDWR);
    if (console < 0) {
        return 1;
    }
    for (int fd = 0; fd < 3; fd++) {
        if (dup2(console, fd) < 0) {
            return 1;
        }
    }
    if (console > 2) {
        close(console);
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    if (mount("proc", "/proc", "proc", 0, NULL) ||
        sysconf(_SC_NPROCESSORS_ONLN) != CPUS) {
        fail("online CPU count");
    }
    printf("LINUX_CPUS_ONLINE %d\n", CPUS);
    if (pthread_barrier_init(&boundary, NULL, CPUS + 1)) {
        fail("barrier init");
    }
    for (int id = 0; id < CPUS; id++) {
        if (pthread_create(&threads[id], NULL, worker, (void *)(intptr_t)id)) {
            fail("pthread_create");
        }
    }
    for (int phase = 0; phase < 2; phase++) {
        if (CONTROL) {
            char line[32];
            printf("LINUX_READY phase=%d\n", phase);
            do {
                if (!fgets(line, sizeof(line), stdin)) {
                    fail("control handshake");
                }
            } while (strncmp(line, "GO", 2));
        }
        pthread_barrier_wait(&boundary);
        pthread_barrier_wait(&boundary);
        printf("LINUX_PHASE_PASS phase=%d cpus=%d\n", phase, CPUS);
    }
    for (int id = 0; id < CPUS; id++) {
        if (pthread_join(threads[id], NULL) || !completed[id]) {
            fail("worker join");
        }
        total += completed[id];
    }
    pthread_barrier_destroy(&boundary);
    printf("LINUX_EFFICIENCY_PASS cpus=%d units=%llu\n",
           CPUS, (unsigned long long)total);
    sync();
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

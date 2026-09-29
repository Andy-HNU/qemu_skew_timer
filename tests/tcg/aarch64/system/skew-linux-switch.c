/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Linux /init: jitter in MTTCG, then two MTTCG/skew round trips via QMP. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <poll.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/syscall.h>
#include <sys/timerfd.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static atomic_int finished, failures;
static uint64_t samples[2];

static uint64_t counter(void)
{
    uint64_t value;
    __asm__ volatile("isb; mrs %0, cntvct_el0" : "=r" (value));
    return value;
}

static void fail(const char *why)
{
    printf("SWITCH_TEST_FAIL %s errno=%d\n", why, errno);
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

static void pin(int cpu)
{
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) {
        fail("sched_setaffinity");
    }
}

static void await(const char *expected)
{
    char line[64];
    do {
        if (!fgets(line, sizeof(line), stdin)) {
            fail("serial handshake");
        }
    } while (strncmp(line, expected, strlen(expected)));
}

static void *worker(void *opaque)
{
    int id = (intptr_t)opaque;
    uint64_t last;
    pin(id);
    last = counter();
    while (!atomic_load(&finished)) {
        /* Advance instruction time without hammering the timer's BQL path. */
        __asm__ volatile("mov x9, #2048\n1: subs x9, x9, #1\nb.ne 1b"
                         ::: "x9", "cc");
        uint64_t now = counter();
        if (now < last) {
            atomic_fetch_add(&failures, 1);
        }
        last = now;
        samples[id]++;
    }
    return NULL;
}

int main(void)
{
    struct termios term;
    pthread_t threads[2];
    uint64_t before, last, frequency;
    uint64_t timer_start, expirations;
    int fd, ret, timer;
    struct itimerspec spec = { .it_value.tv_nsec = 100000000 };

    setvbuf(stdout, NULL, _IONBF, 0);
    mount("proc", "/proc", "proc", 0, NULL);
    if (!tcgetattr(STDIN_FILENO, &term)) {
        term.c_lflag &= ~ECHO;
        tcsetattr(STDIN_FILENO, TCSANOW, &term);
    }
    puts("LINUX_INIT_ENTERED");
    fd = open("/jitterentropy_rng.ko", O_RDONLY);
    if (fd < 0) {
        fail("open jitter module");
    }
    ret = syscall(SYS_finit_module, fd, "", 0);
    close(fd);
    if (ret) {
        fail("jitter initialization");
    }
    puts("JITTER_INIT_PASS");
    for (int i = 0; i < 2; i++) {
        if (pthread_create(&threads[i], NULL, worker, (void *)(intptr_t)i)) {
            fail("pthread_create");
        }
    }
    for (int phase = 0; phase < 4; phase++) {
        timer = timerfd_create(CLOCK_MONOTONIC, 0);
        timer_start = counter();
        if (timer < 0 || timerfd_settime(timer, 0, &spec, NULL)) {
            fail("arm timer before switch");
        }
        before = counter();
        printf("READY_FOR_SWITCH phase=%d\n", phase);
        await("GO");
        last = counter();
        if (last < before) {
            fail("time reversed across switch");
        }
        struct pollfd pending = { .fd = timer, .events = POLLIN };
        if (poll(&pending, 1, 0) != 0) {
            fail("pre-switch timer already expired before handshake completed");
        }
        __asm__ volatile("mrs %0, cntfrq_el0" : "=r" (frequency));
        printf("SWITCH_COUNTER phase=%d before=%llu after=%llu frequency=%llu\n",
               phase, (unsigned long long)before, (unsigned long long)last,
               (unsigned long long)frequency);

        /* Sequential reads migrate between CPUs; one shared time must not reverse. */
        for (int i = 0; i < 8; i++) {
            uint64_t now;
            pin(i % 2);
            now = counter();
            if (now < last) {
                fail("cross-CPU time reversed");
            }
            last = now;
        }
        printf("CROSS_CPU_MONOTONIC_PASS phase=%d reads=8\n", phase);
        /* Timers must still fire while both CPUs execute under budget control. */
        for (int i = 0; i < 8; i++) {
            struct timespec delay = { .tv_nsec = 1000000 };
            uint64_t begin = counter();
            while (nanosleep(&delay, &delay) && errno == EINTR) {
            }
            if (counter() - begin < frequency / 1000) {
                fail("timer fired early");
            }
        }
        if (atomic_load(&failures)) {
            fail("per-CPU time reversed");
        }
        printf("TIMER_AND_SMP_PASS phase=%d timers=8\n", phase);
        if (read(timer, &expirations, sizeof(expirations)) != sizeof(expirations) ||
            expirations != 1 || counter() - timer_start < frequency / 10) {
            fail("timer spanning switch");
        }
        close(timer);
        printf("PENDING_TIMER_PASS phase=%d deadline=100ms\n", phase);
    }
    atomic_store(&finished, 1);
    for (int i = 0; i < 2; i++) {
        pthread_join(threads[i], NULL);
        if (!samples[i]) {
            fail("worker made no progress");
        }
    }
    if (atomic_load(&failures)) {
        fail("time reversed across mode changes");
    }
    printf("CONTINUOUS_READ_PASS samples0=%llu samples1=%llu\n",
           (unsigned long long)samples[0], (unsigned long long)samples[1]);
    puts("READY_FOR_FINAL_CHECK");
    await("DONE");
    puts("SWITCH_TEST_PASS");
    sync();
    reboot(RB_POWER_OFF);
    for (;;) {
        pause();
    }
}

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the actual visible publication helper with controlled interleavings.

The QEMU atomic header is used directly; a barrier wraps CAS for fault
injection.
This tests publication, not seqlock tuple consistency or mode-switch quiescence;
the guest suites exercise the integrated QEMU paths.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
source = (ROOT / "accel/tcg/skew.c").read_text()
helper = re.search(r"static int64_t skew_publish_visible\(.*?\n\}", source,
                   re.S).group()
prefix = r"""
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#define MAX(a, b) ((a) > (b) ? (a) : (b))
/* Standalone replacement for the osdep compile-time size assertion. */
#define qemu_build_assert(test) _Static_assert(test, #test)
#include "qemu/atomic.h"
static aligned_int64_t visible_ns;
static _Thread_local int hold_cas;
static pthread_barrier_t sampled, advanced;
static _Atomic unsigned retries;

static int64_t test_cmpxchg(int64_t *p, int64_t old, int64_t next)
{
    int64_t observed;
    if (hold_cas) {
        hold_cas = 0;
        pthread_barrier_wait(&sampled);
        pthread_barrier_wait(&advanced);
    }
    observed = qatomic_cmpxchg(p, old, next);
    if (observed != old) {
        atomic_fetch_add(&retries, 1);
    }
    return observed;
}
#undef qatomic_cmpxchg
#define qatomic_cmpxchg(p, old, next) test_cmpxchg(p, old, next)
"""
suffix = r"""
static void *coordinator(void *opaque)
{
    hold_cas = 1;
    /* Pause after reading 100, before CAS(100, 106). */
    assert(skew_publish_visible(106) == 110);
    return NULL;
}

static void *publisher(void *opaque)
{
    intptr_t id = (intptr_t)opaque;
    int64_t last = 0;
    for (int64_t i = 1; i <= 100000; i++) {
        int64_t value = skew_publish_visible(i * 8 + id);
        assert(value >= last);
        last = value;
    }
    return NULL;
}

int main(void)
{
    pthread_t thread, workers[8];
    assert(!pthread_barrier_init(&sampled, NULL, 2));
    assert(!pthread_barrier_init(&advanced, NULL, 2));
    qatomic_set_i64(&visible_ns, 100);
    assert(!pthread_create(&thread, NULL, coordinator, NULL));
    pthread_barrier_wait(&sampled);
    assert(skew_publish_visible(110) == 110);
    pthread_barrier_wait(&advanced);
    assert(!pthread_join(thread, NULL));
    assert(qatomic_read_i64(&visible_ns) == 110);
    assert(atomic_load(&retries) >= 1);

    /* Late old-epoch-tuple prediction: old M=100, new M=103, W=10. */
    qatomic_set_i64(&visible_ns, 100);
    assert(skew_publish_visible(106) == 106);
    assert(skew_publish_visible(108) == 108);
    assert(skew_publish_visible(107) == 108);
    assert(qatomic_read_i64(&visible_ns) <= 113);

    /* A newer model lower bound dominates older predictions. */
    assert(skew_publish_visible(112) == 112);
    assert(skew_publish_visible(110) == 112);

    qatomic_set_i64(&visible_ns, 0);
    for (intptr_t i = 0; i < 8; i++) {
        assert(!pthread_create(&workers[i], NULL, publisher, (void *)i));
    }
    for (int i = 0; i < 8; i++) {
        assert(!pthread_join(workers[i], NULL));
    }
    assert(qatomic_read_i64(&visible_ns) == 800007);
    pthread_barrier_destroy(&sampled);
    pthread_barrier_destroy(&advanced);
    puts("PASS: forced CAS retry, late prediction, lower bound, "
         "8 publishers / 800000 calls");
    return 0;
}
"""
with tempfile.TemporaryDirectory(prefix="skew-visible-cas-") as directory:
    path = Path(directory)
    (path / "test.c").write_text(prefix + helper + suffix)
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O2",
                    "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                    "-pthread", "-fsanitize=undefined", "-DCONFIG_ATOMIC64",
                    "-I", str(ROOT / "include"), str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True, timeout=30)

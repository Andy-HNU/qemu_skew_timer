#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the production exponential predictor against a high precision oracle.

This is a numerical helper test. Real QEMU regressions separately cover the
coordinator, concurrency, pause/idle, mode changes, timers and Linux jitter.
"""
import argparse
import ctypes
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[4]
ONE = 1 << 32
MAX_TIME = (1 << 63) - 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = (ROOT / "accel/tcg/skew.c").read_text()
    code = "#include <stdint.h>\n#include <math.h>\n#include <stdbool.h>\n"
    code += "#include <string.h>\n#define MIN(a,b) ((a)<(b)?(a):(b))\n"
    code += "#define MAX(a,b) ((a)>(b)?(a):(b))\n"
    code += "#define SKEW_HORIZON_HISTORY 8\n"
    code += "static uint64_t progress_intervals[SKEW_HORIZON_HISTORY];\n"
    code += "static unsigned progress_interval_pos;\n"
    code += "static int64_t progress_anchor_elapsed_ns;\n"
    code += "static uint64_t publication_horizon_ns, update_interval;\n"
    for name in ("skew_exponential_predict", "skew_exponential_slope",
                 "skew_horizon_reset", "skew_horizon_update"):
        match = re.search(r"^static (?:int64_t|uint64_t|void) " + name +
                          r"\(.*?\n\}", source, re.M | re.S)
        if not match:
            raise RuntimeError(f"production function not found: {name}")
        code += match.group() + "\n"
    code += "\nint64_t predict(int64_t a, int64_t u, uint64_t t, uint64_t s)\n"
    code += "{ return skew_exponential_predict(a, u, t, s); }\n"
    code += "uint64_t limit_slope(uint64_t r, uint64_t s, uint64_t h)\n"
    code += "{ return skew_exponential_slope(r, s, h); }\n"
    code += "void reset_horizon(int64_t now, uint64_t interval)\n"
    code += "{ update_interval = interval; skew_horizon_reset(now); }\n"
    code += "uint64_t horizon(int64_t now, bool progressed)\n"
    code += "{ return skew_horizon_update(now, progressed); }\n"
    (output / "predict.c").write_text(code)
    command = [os.environ.get("CC", "cc"), "-O2", "-Wall", "-Wextra",
               "-Werror", "-shared", "-fPIC", "-fsanitize=undefined",
               "-fno-sanitize-recover=all", str(output / "predict.c"),
               "-o", str(output / "predict.so"), "-lm"]
    subprocess.run(command, check=True, timeout=30)
    library = ctypes.CDLL(str(output / "predict.so"))
    predict = library.predict
    predict.restype = ctypes.c_int64
    predict.argtypes = [ctypes.c_int64, ctypes.c_int64,
                        ctypes.c_uint64, ctypes.c_uint64]
    limit_slope = library.limit_slope
    limit_slope.restype = ctypes.c_uint64
    limit_slope.argtypes = [ctypes.c_uint64] * 3
    cases, max_error = 0, 0
    with localcontext() as context:
        context.prec = 80
        for room in (1, 2, 15, 16, 17, 1000, 1000000, 2000000000):
            for slope in (0, 1, ONE // 256, ONE // 16, ONE // 2, ONE):
                tau = max(1, room * ONE // max(slope, 1))
                times = {0, 1, 17, 100, 1000, 1000000, MAX_TIME}
                times.update(min(MAX_TIME, tau * n // d) for n, d in
                             ((1, 1024), (1, 16), (1, 2), (1, 1),
                              (2, 1), (8, 1), (16, 1), (64, 1)))
                for anchor in (0, 100000000, (1 << 53) + 111,
                               MAX_TIME - room):
                    previous = anchor
                    for elapsed in sorted(times):
                        actual = predict(anchor, anchor + room, elapsed, slope)
                        exponent = Decimal(elapsed) * slope / (ONE * room)
                        advance = int(room * (1 - (-exponent).exp()))
                        expected = anchor + min(advance, room - 1)
                        record = dict(anchor=anchor, room=room, elapsed=elapsed,
                                      slope=slope, actual=actual, expected=expected)
                        # Preserve the exact counterexample before assertions.
                        (output / "last-case.json").write_text(
                            json.dumps(record, indent=2) + "\n")
                        error = abs(actual - expected)
                        assert error <= 1, record
                        assert anchor <= previous <= actual < anchor + room, record
                        assert actual - anchor <= elapsed * slope // ONE + 1, record
                        assert elapsed != 0 or actual == anchor, record
                        assert slope != 0 or actual == anchor, record
                        max_error = max(max_error, error)
                        previous = actual
                        cases += 1

        adaptive_cases, retention_cases = 0, 0
        for room in (1, 2, 16, 1000, 1000000, 2000000000):
            for slope in (0, 1, ONE // 256, ONE // 16, ONE):
                for horizon in (100000, 1000000, 10000000, 125000000, 10**9):
                    effective = limit_slope(room, slope, horizon)
                    assert 0 <= effective <= slope
                    for anchor in (0, (1 << 53) + 111, MAX_TIME - room):
                        previous = anchor
                        for elapsed in (0, 1, horizon // 16, horizon,
                                        2 * horizon, 1000 * horizon):
                            actual = predict(anchor, anchor + room, elapsed, effective)
                            decay = min(Decimal(slope) / (ONE * room),
                                        Decimal(5).ln() / horizon)
                            advance = int(room * (1 - (-decay * elapsed).exp()))
                            expected = anchor + min(advance, room - 1)
                            # Limiting the starting slope rounds down by <1 Q32.
                            tolerance = 1 + elapsed // ONE
                            record = dict(anchor=anchor, room=room, slope=slope,
                                          horizon=horizon, elapsed=elapsed,
                                          effective=effective, actual=actual,
                                          expected=expected, tolerance=tolerance)
                            assert abs(actual - expected) <= tolerance, record
                            assert anchor <= previous <= actual < anchor + room, record
                            assert actual <= predict(anchor, anchor + room, elapsed, slope), record
                            if elapsed == horizon:
                                assert 5 * (anchor + room - actual) >= room, record
                                retention_cases += 1
                            previous = actual
                            adaptive_cases += 1

    # Empty polls accumulate age; only effective progress enters the history.
    library.reset_horizon.argtypes = [ctypes.c_int64, ctypes.c_uint64]
    library.reset_horizon.restype = None
    library.horizon.argtypes = [ctypes.c_int64, ctypes.c_bool]
    library.horizon.restype = ctypes.c_uint64
    library.reset_horizon(0, 100000)
    assert library.horizon(100000, False) == 100000
    assert library.horizon(400000, False) == 400000
    assert library.horizon(1000000, True) == 1000000
    assert library.horizon(1200000, False) == 1000000
    for i in range(1, 9):
        observed = library.horizon(1000000 + i * 200000, True)
        assert observed == (1000000 if i < 8 else 200000)
    assert library.horizon(5000000, False) == 2400000
    library.reset_horizon(10000000, 100000)
    assert library.horizon(10100000, True) == 100000

    # Sparse and dense reads of the same fixed segment have identical results.
    room, slope, end = 1000000, ONE // 16, 16000000
    sparse = predict(0, room, end, slope)
    for elapsed in range(0, end, 12345):
        predict(0, room, elapsed, slope)
    assert predict(0, room, end, slope) == sparse == 632120
    assert predict(sparse, room + 100000, 0, slope) == sparse
    # Finite resolution cannot promise progress on every read, even below U.
    late = predict(0, room, 10**12, ONE)
    assert late == room - 1 == predict(0, room, 10**12 + 1, ONE)
    assert late // 16 == (room - 16) // 16
    result = dict(passed=True, numerical_cases=cases, maximum_error_ns=max_error,
                  adaptive_cases=adaptive_cases, retention_cases=retention_cases,
                  effective_progress_horizon=True,
                  read_schedule_independent=True, reanchor_continuous=True,
                  integer_plateau_explicitly_detected=True,
                  source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  command=command)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

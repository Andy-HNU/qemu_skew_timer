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
    match = re.search(r"^static int64_t skew_exponential_predict\(.*?\n\}",
                      source, re.M | re.S)
    if not match:
        raise RuntimeError("production exponential predictor not found")
    code = "#include <stdint.h>\n#include <math.h>\n"
    code += "#define MIN(a,b) ((a)<(b)?(a):(b))\n" + match.group()
    code += "\nint64_t predict(int64_t a, int64_t u, uint64_t t, uint64_t s)\n"
    code += "{ return skew_exponential_predict(a, u, t, s); }\n"
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
                  read_schedule_independent=True, reanchor_continuous=True,
                  integer_plateau_explicitly_detected=True,
                  source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  command=command)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

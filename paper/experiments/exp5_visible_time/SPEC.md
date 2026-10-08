# Exp5: visible-time interpolation and saturation

The retained matrix uses real AArch64 counter reads. The instrumentation builder
checks exact source anchors and reuses Ninja's expanded native link command;
see the [experiment entry point](../../README.md). Historical results are not
current test evidence. The independent arithmetic and IPS checks do not need the probe.

## Inputs and execution

The matrix expects `build/qemu-system-aarch64`, a separately built
`build/exp5-probe/qemu-system-aarch64` and its `build-manifest.json`, plus
`aarch64-linux-gnu-gcc`. Guest startup and the linker script are reused from
`tests/tcg/aarch64/system/skew-boot.S` and `skew.ld`.
After building the baseline QEMU, run:

```sh
python3 paper/experiments/exp5_visible_time/build_probe.py
python3 paper/experiments/exp5_visible_time/run_matrix.py \
    --out build/exp5-new --repeats 7
```

Use a fresh output directory. The default matrix has 13 cases: wide-boundary,
wide-max, steady, dense, high-2g, high-20g, saturation, saturation-2g, cross-cpu,
burst, sustained-200m, sustained-2g and sustained-20g. Each has one warmup and
seven formal repetitions for both baseline and probe; formal order is shuffled
with recorded seed 20260920. Do not combine runs from different binaries or
configurations. Save source/binary hashes, build commands, environment and all
failed or timed-out attempts with the run.

## Measurement contract

Capture host time, global instruction count, model time, returned visible time,
window, stored Q32 slope, CPU, actual counter value and event at publication and
counter-read points. QMP snapshots alone do not provide this joint observation.
Keep a serialized sequence and per-thread order; arbitrary thread-return
timestamps do not establish causal order. Record actual CNTFRQ and counter offset.

The historical probe buffers records in memory and writes at exit. It relies on
BQL-ordered counter/update paths and reports dropped or unlocked samples. Its
timing is diagnostic evidence, not normal execution performance. The saturation
cases deliberately delay counter reads while holding BQL; label this fault
injection separately from ordinary execution and instrumentation overhead.

Check every attempted run, including warmups: process exit, guest completion,
expected read count, a nonempty probe trace, complete metrics, recorder counts
and no dropped/unlocked records. The matrix rejects missing probe traces.
Do not interpret absent metrics as zero violations. If the container does not
expose CPU topology, retain the recorded lscpu error and return code; do not
invent the missing host metadata or present the run as a full performance study.

Report model/visible/counter backward steps, causally ordered cross-CPU reads,
bounds and slope violations, model conversion, repeated ticks, maximum absolute
bias and population variance of consecutive deltas. Distinguish quantization
from plateaus; preserve violations and gaps rather than clipping or smoothing.
The current conversion check assumes the recorded 62.5 MHz counter frequency.

A plateau requires a stationary model and repeated visible reads at model+window.
The stored slope may remain positive: read-time clamping does not mutate it.
Idle reentry can reset interpolation anchors, so examine resume events too.
Finite sampling cannot prove all intermediate values or formal monotonicity.
Model and visible in one trace are a diagnostic comparison, not a separate
no-interpolation ablation.

## Complementary coverage

Use `skew-check.py` for pause, idle/IRQ, exact counts and multicore control;
`skew-linux-switch.py` for running/paused bidirectional mode transitions; and
`skew-linux-jitter.py` for initialization plus 256 AF_ALG reads. The Linux tests
require an explicitly supplied matching kernel/module set. Availability is not
proof of entropy quality, hardware-equivalent time, or coverage of all kernels,
IPS and window settings. Hardware calibration remains separate work.

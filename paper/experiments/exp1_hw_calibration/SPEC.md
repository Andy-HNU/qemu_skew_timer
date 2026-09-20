# Exp1 — Hardware-calibrated functional timing (RQ1)
Status: pending target hardware. No historical host-only benchmark substitutes for this experiment.

## Protocol
Select packet RX/TX, parsing, interrupt-to-task and control-plane ROIs. Use identical binaries,
inputs, iteration counts and region boundaries on hardware and QEMU. Disable unrelated traffic
or count its instructions separately. Specify whether interrupts are included. Record frequency,
governor, caches/warmup and compiler. Hardware elapsed is wall time for exactly the matched ROI.

For a serial ROI use executed instruction delta, not nominal TB size. For parallel ROIs the
model uses global progress delta, NOT summed per-vCPU instruction counts. Keep those experiments
separate; a single formula cannot silently substitute total instructions for global progress.

Measure at least 7 repetitions after warmup, randomize run order. Split workloads into
calibration and held-out validation BEFORE choosing IPS. Choose selected_ips as the median
of per-workload median equivalent IPS on calibration data; freeze it for all validation rows.
The plotting tool takes selected_ips from CSV and verifies it is one constant; it does not fit it.

## Metrics and outputs
equivalent_ips = instructions * 1e9 / hardware_ns.
reconstructed_ns = instructions * 1e9 / selected_ips.
signed relative error = reconstructed_ns / hardware_ns - 1.
Report mean/median/max absolute relative error, signed-error standard deviation (ddof=1),
per-workload errors and validation-only summary. Include hardware timing uncertainty.
Figures E1 IPS by workload (median, min--max), E2 paired reconstructed/hardware scatter plus y=x.
Mark calibration vs held-out validation. Do not claim hardware equivalence without held-out data.
source_log identifies the paired hardware and emulator measurement record.

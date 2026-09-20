# Exp5 — Visible-time interpolation and saturation (RQ1)
Status: first measured batch completed on 2026-09-20; see PLAN_zh.md and REPORT_zh.md.
Experiment-only source copies now supply joint counter-read instrumentation; QMP alone is insufficient.
Collect at publication/clock-read points host time, G, model, returned visible, window,
stored Q32 slope, CPU, generic-counter value and event. seq must capture a defined serialized
observation order; sort/order by this sequence, NOT arbitrary thread return timestamps.
Retain raw per-thread order and probe details to distinguish overlapping calls from causally
ordered cross-CPU reads. Record actual CNTFRQ and virtual-counter offset in manifest.

Scenarios: steady compute, burst/imbalance, idle/reentry, forced coordinator delay,
read-intensive loop, high IPS, deliberate saturation, VM pause/resume and CPU migration.
Compare stepped-model and visible implementations on identical workloads and read cadence;
same-trace model deltas are a diagnostic, not a complete intervention/ablation.
Include Linux jitter init AND repeated runtime reads; distinguish availability from entropy quality.
Real hardware randomness certification is outside scope.

For a precise synthetic schematic, G may stall while an old slope remains positive.
Reads then hit model+window, plateau until the model moves, and new coordinator samples alter slope.
Current read path clamps but does NOT write slope. Read-triggered slope mutation is future work.
Idle->active can reset the anchor between coordinator callbacks, so include resume events.
A saturated return has effective derivative zero even while stored slope remains positive.

Outputs: direct unsmoothed timeline with model red steps, visible blue line/read markers,
upper/lower bound dashed lines and coordinator verticals. Do not fit a spline.
Report visible backward steps in causally ordered read samples, bounds violations, model backward
steps, repeated counter ticks, maximum abs bias and population variance of successive read deltas
for model and visible separately. Distinguish quantization from plateaus. Flag large gaps;
sampling cannot prove all intermediate values. Preserve violations rather than clipping inputs.

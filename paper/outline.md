# SkewKeeper: Bounded Temporal Synchronization for Parallel Full-System Emulation

Status: mechanism draft; exp5 has initial measurements and a wide-IPS failure report.
Experiments 1--4 remain pending. No hardware calibration has been established.

1. **Introduction**: emulator execution/time mismatch; concurrent full-system emulation;
   scope of functional timing. Contributions: logical progress mapping, bounded active
   vCPU lead, shared bounded interpolation, reproducible hardware calibration methodology.
   The fourth is a proposed evaluation method, not an achieved hardware-accuracy result.
2. **Background and motivation**: QEMU TCG/MTTCG, virtual clock and Arm generic timer,
   fixed-shift versus adaptive icount, deadline-mismatch example clearly marked illustrative.
   Fixed-rate slowtime is a proposed comparator requiring a documented implementation.
3. **Design**:
   - raw accounting, idle epochs, raw_base and logical_base;
   - actual active membership rather than blanket treatment of WFE;
   - minimum published active progress, completed tail at all-idle;
   - window W, available lead, per-dispatch budget B <= 65535;
   - strict time, accumulated offsets and idle warp;
   - momentum history, actual elapsed interval, Q32 slopes, feedback and floor;
   - read-time clamp, shared monotonic publication, saturation, pause/resume;
   - intended invariants and concurrent-publication verification obligations.
4. **Implementation**: QEMU 10.2.0 source map, common budget operations, CPU loop
   and MTTCG thread integration; timer path and BQL; optional mode switching,
   migration blocker, instrumentation limits. Do not claim a separate model-only device clock.
5. **Evaluation plan**:
   - RQ1: exp1 calibration on training workloads / held-out validation,
     exp2 host-capacity and deadline preservation, exp5 visible-clock behavior;
   - RQ2: exp3 MTTCG/icount/skew scaling, fixed-total and fixed-per-vCPU work;
   - RQ3: exp4 window / performance / sampled progress trade-off.
   Each subsection lists required evidence, metrics, failure criteria and TODO.
6. **Discussion and threats**: host-dependent interpolation, floor dominance at high IPS,
   stall/saturation, active-set churn, microarchitectural mismatch, entropy quality,
   publication concurrency and sampled-observation limits, calibrated workload transfer.
7. **Related work**: original QEMU paper, upstream MTTCG and icount design.
   TODO: broaden verified literature before novelty claims; no invented citations.
8. **Conclusion**: restate implemented mechanism and planned evaluation, no numerical claims.

## Figures

Design D1 overall architecture; D2 active logical progress and W; D3 model/visible data paths.
E1 equivalent IPS; E2 hardware vs reconstructed time; E3 host capacity vs guest time;
E4 deadline miss rate; E5 throughput; E6 parallel speedup; E7 W vs throughput;
E8 W vs observed active spread; E9 wait rate; E10 detailed visible/model/bound timeline.
These are figure identifiers in this workspace, not fixed final paper numbering.

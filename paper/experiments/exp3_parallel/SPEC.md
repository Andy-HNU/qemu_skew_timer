# Exp3 — Parallel execution performance (RQ2)
Use 1/2/4/8 vCPUs, then physical-core, logical-core and oversubscribed counts as separate series.
Plain MTTCG and skew use thread=multi. icount uses thread=single and fixed shift; record shift.
Use the SAME rebuilt QEMU binary for toggled modes; describe this as a modified-binary baseline,
not an independently compiled pristine upstream tree.

Run fixed-total-work and fixed-per-vCPU-work as separate CSVs. Do not mix scaling definitions.
Include uniform compute, uneven stages, memory access, idle/reentry and mixed workloads.
One workload/config per input file. Pin consistently, record host topology, isolate load,
warm once, >=7 repeats, randomize mode order. Count all ROI executed guest instructions for
throughput; use instrumentation consistently and measure its overhead. Exclude startup time
only if using explicit common ROI boundaries. Report binary, workload, IPS and window.

Throughput = total ROI executed instructions / host seconds.
Fixed-total speedup = median(T1) / median(Tp).
Fixed-per-CPU plot uses throughput(P)/throughput(1), labeled throughput scaling, not strong speedup.
Each method requires a successful 1-vCPU baseline; never normalize skew against MTTCG's T1.
Abort missing baseline rather than fabricate it. Timed-out runs are counted and excluded from
completion-time summaries. Report median, min--max and failures, not only fastest run.
Use existing tests/tcg/aarch64/system/skew-perf-sweep.py to collect where compatible;
its current output requires an explicit audited CSV conversion; no unverified adapter is supplied.

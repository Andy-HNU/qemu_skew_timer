# Exp4 — Window / synchronization / throughput (RQ3)
Sweep requested W=1K/10K/100K/1M/10M instructions. CLI skew is nanoseconds:
choose skew_ns=ceil(W*1e9/IPS), then record actual window-insns returned by query-skew-clock.
Rounding may change W; use actual W as x coordinate. Fix IPS, coordinator interval, vCPU count,
binary, topology and ROI. Repeat uniform and deliberately imbalanced workloads in separate CSVs.

At consistent observation points collect active membership, logical progress, G, waiting state.
max_active_spread_insns = max(active L)-min(active L), 0 for fewer than two active CPUs.
max_lead_insns = max(active L-G). Exclude stale inactive histories.
QMP published counts can lag in-flight execution and the CPU scan is not an instantaneous atomic
snapshot: label these as sampled observations. Strong bound validation needs execution-path
assertions and stress tests, including exception and atomic retry paths, not just this graph.

wait_count counts entries to an actual sleeping/waiting episode, not all calls to wait().
budget_exit_count is separate and nullable if no reliable event counter is available.
Do not relabel wait traces as skew-only TB exits. Count window waits and generic budget exits
separately if instrumented. Saturated budgets <=65535 can exit without a window wait.

Outputs: throughput vs W, observed spread and lead vs W (include W reference),
wait entries / host second vs W; optional budget exit rate in summary.
The temporal conversion is spread*1e9/IPS. Report instrument overhead and sample interval.
Do not suppress bound violations or trace drops; include them in summary and inspect before claims.

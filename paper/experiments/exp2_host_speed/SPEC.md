# Exp2 — Host capacity and deadlines (RQ1)
Status: measurement plan; slowtime comparator is not currently implemented.

## Protocol
Use a fixed workload/ROI and fixed instruction calibration. Prefer a delegated cgroup v2 cpu.max:
fixed period 100000 us, quota = capacity_fraction * pinned_host_cpu_count * period.
Set the whole QEMU process into this group before execution, keeping the same affinity.
Capture cpu.stat usage_usec, nr_throttled and throttled_usec before/after. Normalize 100% to
the same number of host CPUs. A quota is a budget, not proof of exact execution capacity.
Do not throttle unrelated processes; restore/remove only the dedicated experiment cgroup.
WSL without delegated cpu controller cannot run this protocol: report unsupported, not simulated.

Test 100/80/60/40/20%, randomized order, >=7 repeats after warmup.
Compare plain MTTCG, skew and fixed-shift icount (thread=single). If a fixed-scale host-clock
baseline is implemented, freeze its scale after calibrating at 100%; document source patch/hash.
Do not invent a QEMU slowtime command or silently substitute adaptive icount.
Keep vCPUs, binary, ROI, input and external I/O fixed. Run no-I/O and externally timed I/O separately.

## Measurements
host_ns is monotonic host elapsed, guest_ns is ROI counter elapsed, model_ns is skew model delta
(blank for other modes). Set deadline before running, from hardware requirements, not results.
deadline_miss includes failed/unfinished attempts. For completed rows require
deadline_miss == (guest_ns > deadline_ns). For host-timeout rows completed=0 and deadline_miss=1;
retain the observed guest delta and host timeout limit, never interpret host_ns as completion time.

## Analysis
Plot guest elapsed vs capacity by method (median and min--max among completed), model elapsed
as a separate dashed series for skew; plot miss fraction with all attempts in denominator.
Report failures and completion counts alongside elapsed values.
Matching model time under changed host quota is a hypothesis, not guaranteed when guest paths,
interrupt order or active membership change. Bound endpoint visible/model differences
by 2*window for equal model endpoints; this does not bound host elapsed or I/O latency.

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded study of Skew publication, budget refresh and counter reads."""
import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import time

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "publication_repro", SOURCE / "skew-publication-repro.py")
REPRO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPRO)
EXTRA = ("skew_publish", "skew_demand", "skew_fresh", "skew_run",
         "skew_outer", "skew_budget")
VARIANTS = {
    "native": (None, False, False),
    "original": (None, False, False),
    "periodic-stale": ("periodic", False, False),
    "periodic-fresh": ("periodic", True, False),
    "notify-stale": ("notify", False, False),
    "notify-fresh": ("notify", True, False),
    "inline-stale": ("inline", False, False),
    "inline-fresh": ("inline", True, False),
    "periodic-counter": ("periodic", False, True),
    "notify-counter": ("notify", False, True),
    "inline-counter": ("inline", True, True),
}


def host_state():
    state = {}
    for name, path in (
            ("cpu_stat", "/sys/fs/cgroup/cpu.stat"),
            ("cpu_pressure", "/sys/fs/cgroup/cpu.pressure"),
            ("loadavg", "/proc/loadavg")):
        p = Path(path)
        state[name] = p.read_text() if p.exists() else None
    return state


def wait_breakdown(path, row):
    """Separate unavailable credit from return latency after publication."""
    begin, end = row["host_start_ns"], row["host_end_ns"]
    window = row["ips"] * row["window_ns"] // 10**9
    waiting = {}
    before, after, wake, requests, publish_gaps = [], [], [], [], []
    counts = Counter()
    last_publish = None
    pending_request = None

    def clip(a, b):
        return max(0, min(b, end) - max(a, begin))

    for line in path.read_text().splitlines():
        event = re.search(r"\b(skew_\w+) (.*)$", line)
        if not event:
            continue
        name = event[1]
        fields = {k: int(v) for k, v in REPRO.FIELD.findall(event[2])}
        counts[name] += 1
        stamp = fields.get("host_ns")
        if name == "skew_demand":
            if pending_request is None:
                pending_request = stamp
        elif name == "skew_publish":
            if begin <= stamp <= end:
                if last_publish is not None:
                    publish_gaps.append(stamp - last_publish)
                last_publish = stamp
            if pending_request is not None:
                duration = clip(pending_request, stamp)
                if duration:
                    requests.append(duration)
                pending_request = None
            for value in waiting.values():
                if (value["eligible"] is None and
                        value["logical"] - fields["global"] < window):
                    value["eligible"] = stamp
        elif name == "skew_wait":
            cpu = fields["cpu"]
            if fields["waiting"]:
                if cpu not in waiting:
                    waiting[cpu] = {"start": stamp, "eligible": None,
                                    "logical": fields["logical"]}
            elif cpu in waiting:
                value = waiting.pop(cpu)
                eligible = value["eligible"]
                if eligible is None:
                    before.append(clip(value["start"], stamp))
                else:
                    before.append(clip(value["start"], eligible))
                    duration = clip(eligible, stamp)
                    after.append(duration)
                    if duration:
                        wake.append(duration)
    REPRO.require(not waiting, "diagnostic ended with unclosed waits")
    denominator = row["cpus"] * (end - begin)
    return {
        "event_counts_including_startup": dict(counts),
        "wait_before_credit_fraction": sum(before) / denominator,
        "wait_after_credit_fraction": sum(after) / denominator,
        "publish_to_wait_return_ns": REPRO.distribution(wake),
        "demand_to_publish_ns": REPRO.distribution(requests),
        "actual_publish_interval_ns": REPRO.distribution(publish_gaps),
        "limits": "Post-publication delay includes scheduling and mutex "
                  "reacquisition; it is not measured futex wake latency alone.",
    }


def summarize(rows, output):
    values = {}
    for row in rows:
        if row["diagnostic"]:
            continue
        key = row["variant"] + "/" + row["payload"]
        values.setdefault(key, []).append(row)
    summary = {
        key: {field: REPRO.distribution([r[field] for r in group])
              for field in ("wall_seconds", "cpu_seconds", "user_seconds",
                            "system_seconds", "host_cpu_cores_used",
                            "useful_body_mips_total", "voluntary_switches",
                            "involuntary_switches")}
        for key, group in values.items()
    }
    pairs = {}
    for payload in ("compute", "counter"):
        for variant in VARIANTS:
            key = variant + "/" + payload
            baseline = "periodic-stale/" + payload
            if key not in values or baseline not in values:
                continue
            comparisons = []
            cpu_ratios = []
            for row in values[key]:
                old = next(r for r in values[baseline]
                           if r["round"] == row["round"])
                REPRO.require(row["guest_sha256"] == old["guest_sha256"],
                              "factorial guest mismatch")
                comparisons.append(old["wall_seconds"] / row["wall_seconds"])
                cpu_ratios.append(row["cpu_seconds"] / old["cpu_seconds"])
            pairs[key] = {
                "speedup_vs_periodic_stale": REPRO.distribution(comparisons),
                "cpu_ratio_vs_periodic_stale": REPRO.distribution(cpu_ratios),
            }
    REPRO.write_json(output / "summary.json", summary)
    REPRO.write_json(output / "paired-ratios.json", pairs)
    print(json.dumps(pairs), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--work", type=int, default=2097152)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--suite-timeout", type=float, default=240)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS,
                        default=list(VARIANTS))
    parser.add_argument("--no-diagnostics", action="store_true")
    parser.add_argument("--diagnostic-variants", nargs="+", choices=VARIANTS,
                        default=["periodic-stale", "notify-fresh",
                                 "inline-fresh"])
    args = parser.parse_args()
    REPRO.require(1 <= args.rounds <= 5 and 2 <= args.cpus <= 32 and
                  1024 <= args.work <= 2**32 and args.work % 1024 == 0 and
                  0 < args.timeout <= 60 and 0 < args.suite_timeout <= 600,
                  "invalid experiment size or deadline")
    args.qemu = args.qemu.resolve(strict=True)
    args.baseline = args.baseline.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    args.chunk, args.counter_reads, args.ips = 1024, 64, 200000000
    candidate = args.qemu
    plan = []
    for round_no in range(1, args.rounds + 1):
        order = list(args.variants)
        if round_no % 2 == 0:
            order.reverse()
        elif round_no > 1:
            order = order[len(order) // 2:] + order[:len(order) // 2]
        for variant in order:
            for payload in ("compute", "counter"):
                plan.append({"variant": variant, "payload": payload,
                             "round": round_no, "diagnostic": False})
    if not args.no_diagnostics:
        for variant in args.diagnostic_variants:
            if variant in args.variants:
                for payload in ("compute", "counter"):
                    plan.append({"variant": variant, "payload": payload,
                                 "round": 0, "diagnostic": True})
    REPRO.write_json(output / "plan.json", plan)
    REPRO.write_json(output / "environment.json", {
        "arguments": {k: str(v) if isinstance(v, Path) else v
                      for k, v in vars(args).items()},
        "qemu_sha256": REPRO.sha256(candidate),
        "baseline_sha256": REPRO.sha256(args.baseline),
        "affinity": sorted(os.sched_getaffinity(0)),
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text(),
        "host_before": host_state(),
        "harness_sha256": REPRO.sha256(Path(__file__)),
    })
    plugin, guests = REPRO.build(args, output)
    rows = []
    deadline = time.monotonic() + args.suite_timeout
    for item in plan:
        variant, payload = item["variant"], item["payload"]
        mode, fresh, counter = VARIANTS[variant]
        args.qemu = args.baseline if mode is None else candidate
        args.accel_options = (f"skew-publish={mode},skew-fresh-budget=" +
                              ("on" if fresh else "off") +
                              ",skew-counter-fast=" +
                              ("on" if counter else "off")) if mode else ""
        args.trace_extra = EXTRA if mode else ()
        case = payload + ("-mttcg" if variant == "native" else "-skew")
        label = ("diagnostic" if item["diagnostic"] else
                 "r" + str(item["round"])) + "-" + variant
        before = host_state()
        row = REPRO.run(args, output, plugin, guests, case, label,
                        item["diagnostic"], deadline)
        row.update(item)
        row["host_before"], row["host_after"] = before, host_state()
        directory = output / (label + "-" + case)
        if item["diagnostic"]:
            row["wait_breakdown"] = wait_breakdown(directory / "trace.log", row)
        REPRO.write_json(directory / "results.json", row)
        rows.append(row)
        REPRO.write_json(output / "runs.json", rows)
    summarize(rows, output)


if __name__ == "__main__":
    main()

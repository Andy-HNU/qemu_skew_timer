#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded fixed-work reproduction of delayed Skew progress publication."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import signal
import statistics
import subprocess
import time

from skew_test_io import QemuPipe

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[3]
SPEC = importlib.util.spec_from_file_location(
    "skew_check", SOURCE / "skew-check.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)

# Keep useful work and the ELF identical within each payload's comparisons.
CASES = {
    "compute-mttcg": (False, 0, 1000000, 100000),
    "compute-skew": (True, 0, 1000000, 100000),
    "compute-delayed": (True, 0, 1000000, 4000000),
    "compute-wide": (True, 0, 8000000, 4000000),
    "counter-mttcg": (False, 1, 1000000, 100000),
    "counter-skew": (True, 1, 1000000, 100000),
}
TRACE_EVENTS = ("skew_clock", "skew_cpu", "skew_wait", "skew_warp")
EVENT = re.compile(r"\b(skew_clock|skew_cpu|skew_wait|skew_warp) (.*)$")
FIELD = re.compile(r"(\w+)=(-?\d+)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    pending = path.with_name(path.name + ".pending")
    with pending.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    require(path.read_bytes() == data, f"JSON read-back mismatch: {path}")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values), "minimum": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "maximum": ordered[-1], "sum": sum(ordered),
    }


def trace_evidence(path, begin, end, cpus, window_insns):
    """Use serialized event order; callback entry timestamps can predate waits.

    skew_clock carries callback ENTRY time, not the precise publication time.
    Positive all-ready gaps ending there are conservative lower bounds.
    No interpolation of unobserved raw counters and no per-read clock polling.
    """
    active = set()
    waiting = {}
    intervals = [[] for _ in range(cpus)]
    global_count = 0
    pending = None
    ready = []
    publications = []
    waits = 0
    max_wait_lead = 0
    last_stamp = 0

    def clipped(a, b):
        return max(0, min(b, end) - max(a, begin))

    with path.open() as stream:
        for line in stream:
            match = EVENT.search(line)
            if not match:
                continue
            name = match[1]
            fields = {k: int(v) for k, v in FIELD.findall(match[2])}
            stamp = fields["host_ns"]
            last_stamp = max(last_stamp, stamp)
            if name == "skew_clock":
                new_global = fields["global"]
                require(new_global >= global_count,
                        "global progress went backwards")
                if pending is not None:
                    duration = clipped(pending["start_ns"], stamp)
                    if (duration > 0 and new_global == pending["ready_frontier"]
                            and fields["active"] == len(pending["cpus"])):
                        ready.append({**pending, "callback_entry_ns": stamp,
                                      "published_global": new_global,
                                      "lower_bound_ns": duration})
                    pending = None
                global_count = new_global
                if begin <= stamp <= end:
                    publications.append({"host_ns": stamp, "global": new_global,
                                         "active": fields["active"]})
            elif name == "skew_cpu":
                cpu = fields["cpu"]
                require(0 <= cpu < cpus, "unexpected CPU in trace")
                if fields["active"]:
                    active.add(cpu)
                else:
                    active.discard(cpu)
                pending = None
            elif name == "skew_wait":
                cpu = fields["cpu"]
                require(0 <= cpu < cpus, "unexpected waiting CPU")
                if fields["waiting"]:
                    lead = fields["logical"] - fields["global"]
                    require(lead == window_insns,
                            "wait began away from window boundary")
                    max_wait_lead = max(max_wait_lead, lead)
                    if cpu not in waiting:
                        waiting[cpu] = {"start_ns": stamp,
                                        "logical": fields["logical"]}
                        if begin <= stamp <= end:
                            waits += 1
                else:
                    if cpu in waiting:
                        duration = clipped(waiting.pop(cpu)["start_ns"], stamp)
                        if duration:
                            intervals[cpu].append(duration)
                    pending = None
            elif name == "skew_warp":
                pending = None

            # Only prove the homogeneous full-active phase, excluding startup
            # and CPUs retiring into WFI at the end of the fixed workload.
            if (pending is None and len(active) == cpus and
                    active <= waiting.keys()):
                frontier = min(waiting[cpu]["logical"] for cpu in active)
                if frontier > global_count:
                    pending = {
                        "start_ns": max(waiting[cpu]["start_ns"]
                                        for cpu in active),
                        "global_before": global_count,
                        "ready_frontier": frontier,
                        "cpus": sorted(active),
                    }
    require(last_stamp >= end, "trace ends before completion marker")
    for cpu, entry in waiting.items():
        duration = clipped(entry["start_ns"], end)
        if duration:
            intervals[cpu].append(duration)
    require(len(publications) >= 2,
            "insufficient trace inside the marker interval")
    publish_gaps = [b["host_ns"] - a["host_ns"]
                    for a, b in zip(publications, publications[1:])]
    require(all(gap >= 0 for gap in publish_gaps),
            "callback timestamps regressed")
    wait_total = sum(sum(values) for values in intervals)
    ready_total = sum(entry["lower_bound_ns"] for entry in ready)
    require(ready_total <= end - begin, "overlapping all-ready intervals")
    require(wait_total <= cpus * (end - begin),
            "overlapping per-CPU wait intervals")
    first, last = publications[0], publications[-1]
    return {
        "trace_sha256": sha256(path),
        "trace_bytes": path.stat().st_size,
        "publication_interval_ns": distribution(publish_gaps),
        "global_mips_between_publications":
            (last["global"] - first["global"]) * 1000 /
            (last["host_ns"] - first["host_ns"]),
        "window_wait_entries": waits,
        "per_cpu_window_wait_ns": [distribution(v) for v in intervals],
        "window_wait_fraction_of_configured_vcpu_wall":
            wait_total / (cpus * (end - begin)),
        "max_wait_lead_insns": max_wait_lead,
        "all_ready_unpublished_episodes": len(ready),
        "all_ready_unpublished_lower_bound_ns": ready_total,
        "all_ready_unpublished_fraction_lower_bound":
            ready_total / (end - begin),
        "all_ready_examples": ready[:12],
        "publications": publications,
    }


def build(args, output):
    artifacts = output / "artifacts"
    artifacts.mkdir()
    plugin = artifacts / "clock.so"
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "glib-2.0"], text=True, timeout=10))
    commands = [["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
                 "-Werror", "-Wno-unused-parameter", *flags,
                 "-I", str(ROOT / "include/qemu"),
                 str(SOURCE / "skew-publication-clock.c"), "-o", str(plugin)]]
    write_json(output / "build-commands.json", commands)
    with (output / "build.log").open("wb") as log:
        subprocess.run(commands[-1], check=True, timeout=60,
                       stdout=log, stderr=subprocess.STDOUT)
        guests = {}
        for payload, reads in (("compute", 0), ("counter", args.counter_reads)):
            elf = artifacts / f"{payload}.elf"
            command = [
                "aarch64-linux-gnu-gcc", "-O2", "-g", "-ffreestanding",
                "-fno-stack-protector", "-fno-pie", "-no-pie", "-nostdlib",
                "-mgeneral-regs-only", "-march=armv8-a", "-mno-outline-atomics",
                "-Wall", "-Wextra", "-Werror", f"-DCPUS={args.cpus}",
                f"-DWORK={args.work}UL", f"-DCHUNK={args.chunk}UL",
                f"-DCOUNTER_READS={reads}", "-DAFFINITY_SIZE=16",
                "-Wl,--build-id=none", "-T", str(SOURCE / "skew.ld"),
                str(SOURCE / "skew-perf-boot.S"),
                str(SOURCE / "skew-publication-guest.c"), "-o", str(elf),
            ]
            commands.append(command)
            write_json(output / "build-commands.json", commands)
            subprocess.run(command, check=True, timeout=60,
                           stdout=log, stderr=subprocess.STDOUT)
            symbols = subprocess.check_output(
                ["aarch64-linux-gnu-nm", str(elf)], text=True, timeout=10)
            matches = re.findall(r"^([0-9a-f]+) T marker_store$", symbols, re.M)
            require(len(matches) == 1, "missing unique marker_store symbol")
            guests[payload] = (elf, matches[0])
    write_json(output / "build-commands.json", commands)
    return plugin, guests


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("reproduction deadline exceeded")
    return value


def run(args, output, plugin, guests, case, label, diagnostic, suite_deadline):
    skew, pressure, window, update = CASES[case]
    elf, pc = guests["counter" if pressure else "compute"]
    directory = output / f"{label}-{case}"
    directory.mkdir()
    events = directory / "trace-events"
    if diagnostic and skew:
        events.write_text("\n".join(
            TRACE_EVENTS + tuple(getattr(args, "trace_extra", ()))) + "\n")
    accel = "tcg,thread=multi"
    if skew:
        accel += f",skew={window},skew-ips={args.ips},skew-update={update}"
        if getattr(args, "accel_options", ""):
            accel += "," + args.accel_options
    qp = directory / "qmp"
    command = [str(args.qemu), "-M", "virt,gic-version=3", "-cpu", "cortex-a57",
               "-smp", str(args.cpus), "-m", "128M", "-display", "none",
               "-serial", "none", "-monitor", "none", "-nic", "none", "-S",
               "-accel", accel, "-kernel", str(elf), "-qmp", f"pipe:{qp}",
               "-semihosting-config", "enable=on,target=native",
               "-plugin", f"{plugin},pc={pc}"]
    if diagnostic and skew:
        command += ["-trace", f"events={events},file={directory / 'trace.log'}"]
    write_json(directory / "command.json", command)
    result = {
        "case": case, "label": label, "diagnostic": diagnostic,
        "passed": False, "cpus": args.cpus, "work_per_cpu": args.work,
        "compute_body_instructions": 32 * args.work * args.cpus,
        "counter_reads": (args.work // args.chunk) * args.counter_reads *
                         args.cpus if pressure else 0,
        "guest_sha256": sha256(elf), "window_ns": window if skew else None,
        "update_ns": update if skew else None,
        "ips": args.ips if skew else None,
        "accel_options": getattr(args, "accel_options", "") if skew else "",
    }
    deadline = min(time.monotonic() + args.timeout, suite_deadline)
    proc = None
    raw_output = bytearray()
    with closing(QemuPipe(qp, timeout=min(10, remaining(deadline)))) as pipe, \
            (directory / "qemu.log").open("wb") as log:
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True)
            result["host_pid"] = proc.pid
            pipe.process = proc
            qmp = CHECK.QMP(pipe)

            def cmd(name, **kwargs):
                pipe.timeout = min(10, remaining(deadline))
                return qmp.cmd(name, **kwargs)

            cpu_info = cmd("query-cpus-fast")
            require(len(cpu_info) == args.cpus, "wrong vCPU count")
            write_json(directory / "vcpu-threads.json", cpu_info)
            if diagnostic:
                cmd("human-monitor-command",
                    **{"command-line": "sync-profile on"})
                cmd("human-monitor-command",
                    **{"command-line": "sync-profile reset"})
            cmd("cont")
            buffer = bytearray()
            measured = None
            while measured is None:
                readable, _, _ = select.select(
                    [proc.stdout], [], [], min(0.2, remaining(deadline)))
                if not readable:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(
                        "QEMU exited before checksum completion: "
                        f"{proc.poll()}")
                log.write(chunk)
                log.flush()
                raw_output.extend(chunk)
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer = bytearray(tail)
                    if line.startswith(b"REPRO_INTERVAL "):
                        require(measured is None, "duplicate completion marker")
                        measured = json.loads(line[len(b"REPRO_INTERVAL "):])
            require(measured["host_end_ns"] > measured["host_start_ns"],
                    "invalid measurement interval")
            result.update(measured)
            result["wall_seconds"] = (
                measured["host_end_ns"] - measured["host_start_ns"]) / 1e9
            result["cpu_seconds"] = (measured["user_seconds"] +
                                     measured["system_seconds"])
            result["host_cpu_cores_used"] = (result["cpu_seconds"] /
                                             result["wall_seconds"])
            result["useful_body_mips_total"] = (
                result["compute_body_instructions"] /
                result["wall_seconds"] / 1e6)
            if diagnostic:
                profile = cmd("human-monitor-command",
                              **{"command-line": "info sync-profile 50"})
                (directory / "sync-profile.txt").write_text(profile)
                if skew:
                    write_json(directory / "final-skew.json",
                               cmd("query-skew-clock"))
            cmd("quit")
            proc.wait(timeout=min(5, remaining(deadline)))
            require(proc.returncode == 0, f"QEMU exit status {proc.returncode}")
            result["passed"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if proc is not None:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)
                tail = proc.stdout.read()
                log.write(tail)
                raw_output.extend(tail)
                proc.stdout.close()
                result["returncode"] = proc.returncode
            log.flush()
            os.fsync(log.fileno())
            result["qemu_log_bytes"] = len(raw_output)
            result["qemu_log_sha256"] = hashlib.sha256(raw_output).hexdigest()
            # Preserve the small marker transcript alongside its measurements.
            result["qemu_output"] = raw_output.decode(errors="replace")
            write_json(directory / "results.json", result)
    if sha256(directory / "qemu.log") != result["qemu_log_sha256"]:
        result["passed"] = False
        result["error"] = f"QEMU log read-back mismatch: {directory}"
        write_json(directory / "results.json", result)
        raise ValueError(result["error"])
    if diagnostic and skew:
        try:
            evidence = trace_evidence(
                directory / "trace.log", result["host_start_ns"],
                result["host_end_ns"], args.cpus,
                args.ips * window // 1000000000)
        except Exception as exc:
            result["passed"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            write_json(directory / "results.json", result)
            raise
        write_json(directory / "evidence.json", evidence)
        result["evidence"] = {
            k: v for k, v in evidence.items()
            if k not in ("publications", "all_ready_examples")}
        write_json(directory / "results.json", result)
    fields = ("case", "label", "diagnostic", "wall_seconds", "cpu_seconds")
    print(json.dumps({k: result[k] for k in fields}), flush=True)
    return result


def summarize(rows, output):
    performance = [r for r in rows if not r["diagnostic"]]
    summary = {}
    for name in CASES:
        values = [r for r in performance if r["case"] == name]
        if values:
            summary[name] = {
                key: distribution([r[key] for r in values])
                for key in ("wall_seconds", "cpu_seconds", "user_seconds",
                            "system_seconds", "host_cpu_cores_used",
                            "useful_body_mips_total", "voluntary_switches")
            }
    pairs = {}
    for candidate, baseline in (
            ("compute-skew", "compute-mttcg"),
            ("compute-delayed", "compute-skew"),
            ("compute-delayed", "compute-wide"),
            ("counter-skew", "counter-mttcg")):
        if candidate not in summary or baseline not in summary:
            continue
        comparisons = []
        for a in performance:
            if a["case"] != candidate:
                continue
            b = next(r for r in performance
                     if r["case"] == baseline and r["label"] == a["label"])
            require(a["guest_sha256"] == b["guest_sha256"],
                    "paired ELF mismatch")
            comparisons.append(a["wall_seconds"] / b["wall_seconds"])
        pairs[f"{candidate}/{baseline}"] = distribution(comparisons)
    diagnostics = {r["case"]: r for r in rows if r["diagnostic"]}

    def proof(case):
        evidence = diagnostics.get(case, {}).get("evidence", {})
        return (evidence.get("all_ready_unpublished_episodes", 0) >= 3 and
                evidence.get("all_ready_unpublished_lower_bound_ns", 0)
                >= 1000000)

    normal_ratio = pairs.get("compute-skew/compute-mttcg", {}).get("median")
    delayed_ratio = pairs.get("compute-delayed/compute-skew", {}).get("median")
    recovery_ratio = pairs.get("compute-delayed/compute-wide", {}).get("median")
    verdict = {
        "functional_pass": all(r["passed"] for r in rows),
        "normal_publication_wait_proven": proof("compute-skew"),
        "normal_slowdown_reproduced": (proof("compute-skew") and
                                       normal_ratio is not None and
                                       normal_ratio >= 1.2),
        "controlled_publication_wait_proven": proof("compute-delayed"),
        "controlled_slowdown_reproduced": (proof("compute-delayed") and
                                           delayed_ratio is not None and
                                           delayed_ratio >= 1.5),
        "wider_window_recovery_observed": (recovery_ratio is not None and
                                           recovery_ratio >= 1.5),
        "criteria": {
            "proof": ("at least 3 all-active-ready episodes "
                      "totaling at least 1 ms"),
            "normal_slowdown": "median paired skew/mttcg wall ratio >= 1.2",
            "controlled_slowdown":
                "median paired delayed/default wall ratio >= 1.5",
            "recovery": "median paired delayed/wide wall ratio >= 1.5",
        },
        "limits": [
            "Diagnostic runs are excluded from performance comparisons.",
            "QSP includes startup; its mutex elapsed is acquisition time, "
            "not hold time.",
            "Host CPU utilization and window waiting are not guest JIT "
            "execution shares.",
            "All-ready gaps end at callback entry and are lower bounds "
            "on publication delay.",
            "Controlled delayed publication does not prove BQL caused "
            "natural delay.",
            "No Fast Models comparison and no production login workload "
            "are included.",
        ],
    }
    write_json(output / "summary.json", summary)
    write_json(output / "paired-ratios.json", pairs)
    write_json(output / "verdict.json", verdict)
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--work", type=int, default=2097152)
    parser.add_argument("--chunk", type=int, default=1024)
    parser.add_argument("--counter-reads", type=int, default=64)
    parser.add_argument("--ips", type=int, default=200000000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--suite-timeout", type=float, default=300)
    parser.add_argument("--cases", nargs="+", choices=CASES,
                        default=list(CASES))
    parser.add_argument("--no-diagnostics", action="store_true")
    args = parser.parse_args()
    if not (2 <= args.cpus <= 128 and 1 <= args.chunk <= 1048576 and
            args.chunk <= args.work <= 2**32 and args.work % args.chunk == 0 and
            1 <= args.counter_reads <= 65536 and 1000 <= args.ips <= 10**12 and
            1 <= args.rounds <= 20 and 0 < args.timeout <= 300 and
            0 < args.suite_timeout <= 3600 and
            len(set(args.cases)) == len(args.cases)):
        parser.error("invalid CPU/work/chunk/read/IPS/round/deadline "
                     "parameters")
    args.qemu = args.qemu.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = {
        "qemu": str(args.qemu), "qemu_sha256": sha256(args.qemu),
        "qemu_version": subprocess.check_output([str(args.qemu), "--version"],
                                                 text=True, timeout=10),
        "harness_commit": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True, timeout=10).strip(),
        "host_affinity": sorted(os.sched_getaffinity(0)),
        "uname": list(os.uname()),
        "arguments": {k: str(v) if isinstance(v, Path) else v
                      for k, v in vars(args).items()},
        "source_sha256": {name: sha256(SOURCE / name) for name in (
            "skew-publication-repro.py", "skew-publication-guest.c",
            "skew-publication-clock.c", "skew-perf-boot.S", "skew.ld")},
    }
    environment["tool_versions"] = {
        tool: subprocess.check_output([tool, "--version"],
                                      text=True, timeout=10)
        for tool in ("cc", "aarch64-linux-gnu-gcc", "aarch64-linux-gnu-nm",
                     "pkg-config")
    }
    for name in ("cpu.max", "memory.max"):
        path = Path("/sys/fs/cgroup") / name
        environment[name] = path.read_text().strip() if path.exists() else None
    write_json(output / "environment.json", environment)
    plan = []
    for round_no in range(args.rounds):
        order = args.cases if round_no % 2 == 0 else list(reversed(args.cases))
        plan.extend({"case": case, "label": f"r{round_no + 1}",
                     "diagnostic": False}
                    for case in order)
    if not args.no_diagnostics:
        plan.extend({"case": case, "label": "diagnostic", "diagnostic": True}
                    for case in args.cases if case != "compute-mttcg")
    write_json(output / "plan.json", plan)
    plugin, guests = build(args, output)
    deadline = time.monotonic() + args.suite_timeout
    rows = []
    for item in plan:
        row = run(args, output, plugin, guests, suite_deadline=deadline, **item)
        rows.append(row)
        write_json(output / "runs.json", rows)
    verdict = summarize(rows, output)
    print(json.dumps(verdict), flush=True)


if __name__ == "__main__":
    main()

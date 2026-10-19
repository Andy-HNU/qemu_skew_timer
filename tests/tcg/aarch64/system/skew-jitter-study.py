#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded cold-boot Linux jitter matrix with complete failure records.

Both interpolation policies use one QEMU binary and one unmodified kernel/module
set. Each attempt starts a new VM. No health check is disabled or retried by this
harness. The kernel's own health-test handling remains intact.
"""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import resource
import shutil
import signal
import subprocess
import time

from skew_test_io import QemuPipe

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("jitter", SOURCE / "skew-linux-jitter.py")
JITTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JITTER)
PROFILES = {
    "nominal": (100000, "periodic", False, False),
    "delayed": (1000000, "periodic", False, False),
    "counter": (100000, "periodic", False, True),
    "inline": (100000, "inline", True, True),
}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    pending = path.with_name(path.name + ".pending")
    with pending.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    assert path.read_bytes() == data, path


def build(args):
    root = args.output / "root"
    for name in ("dev", "proc"):
        (root / name).mkdir(parents=True)
    command = [args.cc, "-static", "-O2", "-Wall", "-Wextra", "-Werror",
               f"-DJITTER_READS={args.reads}",
               str(args.output / "sources" / "skew-linux-jitter.c"),
               "-o", str(root / "init")]
    if args.health_probes:
        command.append("-DJITTER_HEALTH_PROBES")
    save(args.output / "build-command.json", command)
    subprocess.run(command, check=True, timeout=30)
    for module in args.modules:
        shutil.copy2(module, root / module.name)
    paths = ["."] + [str(p.relative_to(root)) for p in sorted(root.rglob("*"))]
    archive = args.output / "initramfs.cpio"
    with archive.open("wb") as stream:
        subprocess.run(["cpio", "--null", "-o", "--format=newc"], cwd=root,
                       input=("\0".join(paths) + "\0").encode(), stdout=stream,
                       check=True, timeout=15)
    return archive


def expired(signum, frame):
    raise TimeoutError("jitter guest host deadline exceeded")


def run(args, item, archive, directory, deadline):
    directory.mkdir()
    variant = item["variant"]
    accel = "tcg,thread=multi"
    if variant != "mttcg":
        update, publish, fresh, counter = PROFILES[item["profile"]]
        accel += (f",skew={args.window},skew-ips={item['ips']},skew-update={update}"
                  f",skew-interp={variant},skew-publish={publish},skew-fresh-budget="
                  + ("on" if fresh else "off") + ",skew-counter-fast="
                  + ("on" if counter else "off"))
    qp, sp = directory / "qmp", directory / "serial"
    command = [str(args.qemu), "-M", "virt,gic-version=3", "-cpu", "cortex-a57",
               "-smp", str(args.cpus), "-m", "512M", "-display", "none",
               "-monitor", "none", "-nic", "none", "-no-reboot",
               "-accel", accel, "-kernel", str(args.kernel), "-initrd", str(archive),
               "-qmp", f"pipe:{qp}", "-serial", f"pipe:{sp}",
               "-append", "console=ttyAMA0 earlycon rdinit=/init nokaslr loglevel=7"]
    if args.trace and variant != "mttcg":
        command += ["-trace", "enable=skew_clock", "-trace", "enable=skew_sample",
                    "-trace", "enable=skew_wait", "-D", str(directory / "clock.trace")]
    save(directory / "command.json", command)
    row = dict(item, passed=False, command=command, requested_reads=args.reads,
               diagnostic=args.trace or args.sample_seconds > 0 or args.health_probes,
               samples=[],
               error=None, failure_kind=None)
    text = bytearray()
    qmp = None
    with closing(QemuPipe(qp, timeout=10)) as qstream, \
         closing(QemuPipe(sp, timeout=args.timeout)) as serial, \
         (directory / "qemu.log").open("wb") as qlog, \
         (directory / "serial.log").open("wb") as slog:
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()
        timeout = min(args.timeout, deadline - start)
        if timeout <= 0:
            raise TimeoutError("jitter suite deadline exceeded")
        proc = subprocess.Popen(command, stdout=qlog, stderr=subprocess.STDOUT)
        qstream.process = serial.process = proc
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            qmp = JITTER.QMP(qstream)

            def sample():
                state = qmp.call("query-skew-clock")
                assert state["mode"] == "skew", state
                assert abs(state["visible-bias-ns"]) <= state["window-ns"], state
                assert state["visible-slope-q32"] <= 1 << 32, state
                if row["samples"]:
                    assert state["virtual-ns"] >= row["samples"][-1]["virtual-ns"]
                row["samples"].append(state)

            if variant != "mttcg":
                sample()
            next_sample = start + args.sample_seconds
            while not re.search(rb"(?:^|[\r\n])JITTER_TEST_PASS\r?\n", text):
                serial.timeout = max(0.001, start + timeout - time.monotonic())
                chunk = serial.read(65536)
                text.extend(chunk)
                slog.write(chunk)
                slog.flush()
                failure = re.search(rb"JITTER_TEST_FAIL[^\r\n]*[\r\n]", text)
                if failure:
                    row["failure_kind"] = ("probe_setup_failure" if
                        b"health probe" in failure[0] else "guest_jitter_failure")
                    raise RuntimeError(failure[0].decode(errors="replace").strip())
                if b"Kernel panic" in text:
                    row["failure_kind"] = "kernel_panic"
                    raise RuntimeError("guest kernel panic")
                if (variant != "mttcg" and args.sample_seconds > 0 and
                        time.monotonic() >= next_sample and
                        b"JITTER_TEST_PASS" not in text):
                    sample()
                    next_sample = time.monotonic() + args.sample_seconds
            proc.wait(timeout=max(0.001, start + timeout - time.monotonic()))
            assert proc.returncode == 0, proc.returncode
            decoded = text.decode(errors="replace")
            assert "JITTER_INIT_PASS" in decoded
            assert f"JITTER_USE_PASS reads={args.reads} " in decoded
            row["passed"] = True
        except (AssertionError, EOFError, OSError, RuntimeError,
                TimeoutError, subprocess.SubprocessError) as error:
            row["error"] = f"{type(error).__name__}: {error}"
            if row["failure_kind"] is None:
                row["failure_kind"] = ("host_timeout" if isinstance(
                    error, (TimeoutError, subprocess.TimeoutExpired)) else "harness_or_exit")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            slog.flush()
            os.fsync(slog.fileno())
            qlog.flush()
            os.fsync(qlog.fileno())
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            row.update(returncode=proc.returncode, wall_seconds=time.monotonic() - start,
                       cpu_seconds=after.ru_utime + after.ru_stime -
                       before.ru_utime - before.ru_stime,
                       serial_output=text.decode(errors="replace"),
                       serial_sha256=hashlib.sha256(text).hexdigest(),
                       serial_bytes=len(text))
            assert (directory / "serial.log").read_bytes() == bytes(text)
            row["init_passed"] = b"JITTER_INIT_PASS" in text
            row["read_progress"] = [int(n) for n in re.findall(
                rb"JITTER_USE_PROGRESS reads=(\d+)", text)]
            if qmp:
                save(directory / "qmp.json", qmp.records)
            save(directory / "results.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--module-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--ips", type=int, nargs="+", default=[200000000, 2000000000])
    parser.add_argument("--variants", nargs="+", choices=["linear", "exponential", "mttcg"],
                        default=["linear", "exponential", "mttcg"])
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=["nominal"])
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--window", type=int, default=1000000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--reads", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--suite-timeout", type=float, default=900)
    parser.add_argument("--sample-seconds", type=float, default=0)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--health-probes", action="store_true",
                        help="diagnostic kprobes, exact Debian 6.1.176 module only")
    args = parser.parse_args()
    if not (1 <= args.cpus <= 32 and 1 <= args.rounds <= 20 and
            1 <= args.reads <= 65536 and 1 <= args.window <= 10**9 and
            1 <= args.timeout <= 180 and 1 <= args.suite_timeout <= 1800 and
            0 <= args.sample_seconds <= 10 and
            all(1 <= ips <= 10**12 for ips in args.ips)):
        parser.error("experiment size or deadline outside bounded limits")
    args.qemu = args.qemu.resolve(strict=True)
    args.kernel = args.kernel.resolve(strict=True)
    args.module_dir = args.module_dir.resolve(strict=True)
    args.modules = [(args.module_dir / name).resolve(strict=True) for name in
                    ("af_alg.ko", "jitterentropy_rng.ko", "algif_rng.ko")]
    if args.health_probes and digest(args.modules[1]) != (
            "ef71ff1d1dab6c2704e85b6f4ece4f7e1a6367bb4f04440da397f953a37a8ad3"):
        parser.error("health probe offsets require the recorded 6.1.176 module hash")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    snapshots = args.output / "sources"
    snapshots.mkdir()
    for name in ("skew-linux-jitter.c", "skew-linux-jitter.py",
                 "skew_test_io.py", "skew-jitter-study.py"):
        shutil.copy2(SOURCE / name, snapshots / name)
    plan = []
    randomizer = random.Random(20261019)
    for repeat in range(1, args.rounds + 1):
        group = [dict(round=repeat, variant=variant, ips=ips, profile=profile)
                 for profile in args.profiles for ips in args.ips
                 for variant in args.variants if variant != "mttcg"]
        if "mttcg" in args.variants:
            group.append(dict(round=repeat, variant="mttcg", ips=None, profile="native"))
        randomizer.shuffle(group)
        plan.extend(group)
    save(args.output / "plan.json", plan)
    archive = build(args)
    save(args.output / "environment.json", {
        "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in
                      vars(args).items() if k != "modules"},
        "files": {str(p): digest(p) for p in
                  [args.qemu, args.kernel, archive, *args.modules,
                   *sorted(snapshots.iterdir())]},
        "host_affinity": sorted(os.sched_getaffinity(0)),
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text(),
        "seed": 20261019,
    })
    signal.signal(signal.SIGALRM, expired)
    deadline = time.monotonic() + args.suite_timeout
    rows = []
    for index, item in enumerate(plan):
        if time.monotonic() >= deadline:
            break
        name = f"{index:03d}-{item['profile']}-{item['ips']}-{item['variant']}"
        row = run(args, item, archive, args.output / name, deadline)
        row["directory"] = name
        rows.append(row)
        save(args.output / "runs.json", rows)
        print(json.dumps({k: row[k] for k in ("directory", "passed", "failure_kind",
                                             "wall_seconds", "read_progress")}), flush=True)
    groups = {}
    for row in rows:
        key = f"{row['profile']}/{row['ips']}/{row['variant']}"
        group = groups.setdefault(key, dict(passed=0, failed=0))
        group["passed" if row["passed"] else "failed"] += 1
    summary = dict(planned=len(plan), attempted=len(rows),
                   complete=len(rows) == len(plan), groups=groups,
                   all_passed=len(rows) == len(plan) and all(r["passed"] for r in rows))
    save(args.output / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

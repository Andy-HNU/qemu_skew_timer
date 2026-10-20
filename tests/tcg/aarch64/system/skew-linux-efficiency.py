#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded real Linux SMP/checksum/timer acceptance and optional QMP switching."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import subprocess
import tempfile
import time

from skew_test_io import QemuPipe

SOURCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("skew_check", SOURCE / "skew-check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu", type=Path)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpus", type=int, default=32)
    parser.add_argument("--work", type=int, default=200000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--mode", choices=("skew", "mttcg"), default="skew")
    parser.add_argument("--switch", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.cpus <= 512 or args.work < 4 or args.timeout <= 0:
        parser.error("require 1..512 CPUs, work >= 4 and positive timeout")
    if args.switch and args.mode != "skew":
        parser.error("--switch requires configured skew")
    args.qemu, args.kernel = args.qemu.resolve(), args.kernel.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / "root"
    for name in ("dev", "proc"):
        (root / name).mkdir(parents=True)
    subprocess.run(["aarch64-linux-gnu-gcc", "-static", "-O2", "-pthread",
                    "-Wall", "-Wextra", "-Werror", f"-DCPUS={args.cpus}",
                    f"-DWORK={args.work}", f"-DCONTROL={int(args.switch)}",
                    str(SOURCE / "skew-linux-efficiency.c"), "-o", str(root / "init")],
                   check=True, timeout=30)
    archive = args.output / "initramfs.cpio"
    with archive.open("wb") as stream:
        subprocess.run(["cpio", "--null", "-o", "--format=newc"], cwd=root,
                       input=b".\0dev\0proc\0init\0", stdout=stream,
                       check=True, timeout=10)
    result = dict(passed=False, cpus=args.cpus, mode=args.mode, switch=args.switch,
                  kernel_sha256=digest(args.kernel), qemu_sha256=digest(args.qemu),
                  init_sha256=digest(root / "init"), timeout=args.timeout,
                  work_per_cpu=args.work)
    with tempfile.TemporaryDirectory(prefix="skew-linux-efficiency-") as tmp:
        qp, sp = Path(tmp) / "qmp", Path(tmp) / "serial"
        accel = "tcg,thread=multi"
        if args.mode == "skew":
            accel += ",skew=1000000,skew-ips=1000000000,skew-update=100000"
        command = [str(args.qemu), "-M", "virt,gic-version=3", "-cpu", "cortex-a57",
                   "-smp", str(args.cpus), "-m", "512M", "-display", "none",
                   "-monitor", "none", "-nic", "none", "-no-reboot",
                   "-accel", accel, "-kernel", str(args.kernel),
                   "-initrd", str(archive), "-qmp", f"pipe:{qp}",
                   "-serial", f"pipe:{sp}", "-append",
                   "console=ttyAMA0 earlycon rdinit=/init nokaslr loglevel=6"]
        result["command"] = command
        (args.output / "command.json").write_text(json.dumps(command, indent=2))
        with closing(QemuPipe(qp, timeout=15)) as qstream, \
             closing(QemuPipe(sp, timeout=args.timeout)) as serial, \
             (args.output / "qemu.log").open("wb") as qlog, \
             (args.output / "serial.log").open("wb") as slog:
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            start = time.monotonic()
            proc = subprocess.Popen(command, stdout=qlog, stderr=subprocess.STDOUT)
            qstream.process = serial.process = proc
            text, handled = bytearray(), set()
            try:
                qmp = check.QMP(qstream)
                cpus = qmp.cmd("query-cpus-fast")
                assert len(cpus) == args.cpus, cpus
                result["vcpu_thread_ids"] = [c["thread-id"] for c in cpus]
                # FIFO reads may split even the final printf in the middle.
                while not any(line.startswith(b"LINUX_EFFICIENCY_PASS ")
                              for line in bytes(text).split(b"\n")[:-1]):
                    serial.timeout = start + args.timeout - time.monotonic()
                    if serial.timeout <= 0:
                        raise TimeoutError("Linux acceptance exceeded total deadline")
                    chunk = serial.read(65536)
                    text.extend(chunk)
                    slog.write(chunk)
                    slog.flush()
                    if b"LINUX_EFFICIENCY_FAIL" in text or b"Kernel panic" in text:
                        raise AssertionError(text.decode(errors="replace")[-4000:])
                    for phase in range(2):
                        ready = f"LINUX_READY phase={phase}".encode()
                        if args.switch and phase not in handled and ready in text:
                            qmp.cmd("skew-stop" if phase == 0 else "skew-start")
                            serial.write(b"GO\n")
                            # Exercise pause during actual SMP work, not only the handshake.
                            time.sleep(0.01)
                            qmp.cmd("stop")
                            frozen = qmp.cmd("query-skew-clock")
                            time.sleep(0.02)
                            again = qmp.cmd("query-skew-clock")
                            assert frozen["virtual-ns"] == again["virtual-ns"]
                            assert frozen["cpus"] == again["cpus"]
                            qmp.cmd("cont")
                            handled.add(phase)
                proc.wait(timeout=max(1, start + args.timeout - time.monotonic()))
                assert proc.returncode == 0, proc.returncode
                output = text.decode(errors="replace")
                assert f"LINUX_CPUS_ONLINE {args.cpus}" in output
                assert all(f"LINUX_PHASE_PASS phase={p}" in output for p in range(2))
                expected = args.cpus * args.work + sum(
                    args.work // 4 * (i % 4 + 1) for i in range(args.cpus))
                assert f"cpus={args.cpus} units={expected}" in output
                assert not args.switch or handled == {0, 1}
                result.update(passed=True, completed_units=expected,
                              compute_body_instructions=32 * expected)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                result.update(wall_seconds=time.monotonic() - start,
                              cpu_seconds=after.ru_utime + after.ru_stime -
                              before.ru_utime - before.ru_stime,
                              returncode=proc.returncode)
                (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

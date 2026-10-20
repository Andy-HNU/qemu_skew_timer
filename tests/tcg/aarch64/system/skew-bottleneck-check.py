#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-guest checks for opt-in Skew paths; no Linux image is required."""
import argparse
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import time

from skew_test_io import QemuPipe

SOURCE = Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


CHECK = module("skew_check", "skew-check.py")
SWITCH = module("skew_switch", "skew-linux-switch.py")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def check_clock(state):
    assert state["virtual-ns"] == (state["mttcg-elapsed-ns"] +
                                   state["skew-elapsed-ns"])
    assert state["visible-bias-ns"] == (state["virtual-ns"] - state["model-ns"])
    assert abs(state["visible-bias-ns"]) <= state["window-ns"]
    for cpu in state["cpus"]:
        assert cpu["budget-enabled"] == (state["mode"] == "skew")
        if cpu["active"]:
            assert 0 <= cpu["local-icount"] - state["global-icount"] <= \
                state["window-insns"]


def mode_switches(qemu, output, paused):
    output.mkdir()
    elf = CHECK.build_guest(output, 8)
    command = CHECK.command(qemu, elf)
    command[command.index("-accel") + 1] += ",skew-defer=on"
    command[command.index("-serial") + 1] = f"pipe:{output / 'serial'}"
    command += ["-S", "-qmp", f"pipe:{output / 'qmp'}"]
    write_json(output / "command.json", command)
    with (output / "qemu.log").open("w") as log, \
         closing(QemuPipe(output / "qmp", timeout=10)) as stream, \
         closing(QemuPipe(output / "serial", timeout=10)) as serial:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        stream.process = serial.process = proc
        qmp = None
        phases = []
        try:
            qmp = SWITCH.QMP(stream)
            assert qmp.call("query-skew-clock")["mode"] == "mttcg"
            qmp.call("cont")
            # MODE 8 reports readiness through semihosting, then reads UART.
            ready_deadline = time.monotonic() + 10
            while "CONTROL_READY" not in (output / "qemu.log").read_text():
                if proc.poll() is not None:
                    raise RuntimeError("guest exited before CONTROL_READY")
                if time.monotonic() >= ready_deadline:
                    raise TimeoutError("guest CONTROL_READY deadline")
                time.sleep(0.01)
            for index in range(20):
                if paused:
                    qmp.call("stop")
                before = qmp.call("query-skew-clock")
                enabled = index % 2 == 0
                name = "skew-start" if enabled else "skew-stop"
                changed = qmp.call(name)
                check_clock(changed)
                assert changed["mode"] == ("skew" if enabled else "mttcg")
                assert changed["virtual-ns"] >= before["virtual-ns"]
                if paused:
                    assert changed["virtual-ns"] == before["virtual-ns"]
                    assert changed["global-icount"] == 0
                    assert all(c["raw-icount"] == c["local-icount"] == 0
                               and not c["active"] and not c["waiting"]
                               for c in changed["cpus"])
                    time.sleep(0.003)
                    assert qmp.call("query-skew-clock") == changed
                    assert qmp.call(name) == changed
                    assert not qmp.call("query-status")["running"]
                    qmp.call("cont")
                else:
                    assert qmp.call("query-status")["running"]
                time.sleep(0.005)
                after = qmp.call("query-skew-clock")
                check_clock(after)
                assert after["virtual-ns"] >= changed["virtual-ns"]
                phases.append(dict(before=before, changed=changed, after=after))
                write_json(output / "phases.json", phases)
            serial.write(b"x")
            proc.wait(timeout=10)
            log.flush()
            assert proc.returncode == 0
            assert "PASS" in (output / "qemu.log").read_text()
            return dict(paused=paused, transitions=len(phases), passed=True)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if qmp:
                write_json(output / "qmp.json", qmp.records)


def deadline(signum, frame):
    raise TimeoutError("bottleneck acceptance suite deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skew-options", required=True)
    parser.add_argument("--suite-timeout", type=int, default=90)
    parser.add_argument("--checks", nargs="+",
                        choices=["offset", "slow", "switch"],
                        default=["offset", "slow", "switch"])
    args = parser.parse_args()
    assert 0 < args.suite_timeout <= 180
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    CHECK.SKEW_OPTIONS = args.skew_options
    qemu = args.qemu.resolve(strict=True)
    results = []

    def save(result):
        results.append(result)
        write_json(args.output / "results.json", results)
        print(json.dumps(result), flush=True)

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(args.suite_timeout)
    if "offset" in args.checks:
        save(CHECK.run_guest(qemu, args.output, 11, trace=True))
    if "slow" in args.checks:
        for cpu in (0, 1):
            save(CHECK.run_guest(qemu, args.output, 3,
                                 delay_cpu=cpu, trace=True))
    if "switch" in args.checks:
        for paused in (False, True):
            save(mode_switches(qemu, args.output / f"switch-{int(paused)}",
                               paused))
    signal.alarm(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Standalone skew acceptance: stdlib + AArch64 GCC, no guest OS required."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import time


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rwb", buffering=0)
        self.file.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, name, _allow_error=False, **args):
        self.file.write(json.dumps({"execute": name, "arguments": args}).encode()
                        + b"\n")
        while True:
            data = json.loads(self.file.readline())
            if "error" in data:
                if _allow_error:
                    return data
                raise AssertionError(data)
            if "return" in data:
                return data["return"]

    def get(self, path, prop):
        return self.cmd("qom-get", path=path, property=prop)

    def close(self):
        self.file.close()
        self.sock.close()


class GDB:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(60)
        self.sock.connect(str(path))

    def cmd(self, command):
        body = command.encode()
        self.sock.sendall(b"$" + body + b"#" + f"{sum(body) % 256:02x}".encode())
        while self.sock.recv(1) != b"$":
            pass
        data = b""
        while (ch := self.sock.recv(1)) != b"#":
            if not ch:
                raise EOFError("GDB disconnected")
            data += ch
        self.sock.recv(2)
        self.sock.sendall(b"+")
        return data.decode()

    def bp(self, addr, insert=True):
        assert self.cmd(f"{'Z' if insert else 'z'}0,{addr:x},4") == "OK"


def wait_socket(path, proc):
    end = time.monotonic() + 15
    while not path.exists():
        assert proc.poll() is None, "QEMU exited before opening socket"
        assert time.monotonic() < end, "socket startup timeout"
        time.sleep(0.01)


def build_guest(out, mode, skew=True):
    source = Path(__file__).resolve().parent
    elf = out / f"guest-{mode}-{int(skew)}.elf"
    subprocess.run(["aarch64-linux-gnu-gcc", "-O2", "-g", "-ffreestanding",
                    "-fno-stack-protector", "-fno-pie", "-no-pie", "-nostdlib",
                    "-mgeneral-regs-only", "-march=armv8-a", "-mno-outline-atomics",
                    f"-DMODE={mode}", f"-DSKEW={int(skew)}",
                    "-Wl,--build-id=none", "-T", str(source / "skew.ld"),
                    str(source / "skew-boot.S"), str(source / "skew-guest.c"),
                    "-o", str(elf)], check=True, capture_output=True)
    return elf


def command(qemu, elf, smp=2, skew=True, ips=2000000000,
            window=1000000, update=100000):
    accel = "tcg,thread=multi"
    if skew:
        accel += f",skew={window},skew-ips={ips},skew-update={update}"
    return [str(qemu), "-M", "virt,gic-version=2", "-cpu", "cortex-a57",
            "-accel", accel, "-smp", str(smp), "-m", "128M", "-display", "none",
            "-serial", "none", "-monitor", "none", "-semihosting-config",
            "enable=on,target=native", "-kernel", str(elf)]


def parse_trace(path, ips, window):
    previous_ns = previous_global = 0
    samples = {}
    clocks = waits = warps = max_step = max_lead = 0
    warp_ns = 0
    callbacks = []
    for line in path.read_text().splitlines():
        fields = dict((k, int(v)) for k, v in re.findall(r"(\w+)=(-?\d+)", line))
        if "arm_gt_timer_expire " in line:
            assert fields["count"] >= fields["cval"], line
            callbacks.append((fields["count"] - fields["cval"]) * fields["period"])
        elif "skew_sample " in line:
            assert fields["logical"] == (fields["logical_base"] + fields["raw"]
                                          - fields["raw_base"]), line
            samples[fields["cpu"]] = fields
        elif "skew_clock " in line:
            now, glob = fields["ns"], fields["global"]
            assert now >= previous_ns and glob >= previous_global, line
            assert now == glob * 10**9 // ips + warp_ns, line
            active = [s["logical"] for s in samples.values() if s["active"]]
            assert len(active) == fields["active"], line
            if active:
                assert glob == max(previous_global, min(active)), line
                # Samples precede this update, so measure against the old global.
                max_lead = max(max_lead, max(active) - previous_global)
                assert max(active) - previous_global <= window * ips // 10**9
            max_step = max(max_step, now - previous_ns)
            previous_ns, previous_global = now, glob
            samples.clear()
            clocks += 1
        elif "skew_wait " in line:
            if fields["waiting"]:
                assert fields["logical"] - fields["global"] == window * ips // 10**9
                waits += 1
        elif "skew_warp " in line:
            assert not any(s["active"] for s in samples.values()), line
            assert fields["delta"] > 0
            warp_ns += fields["delta"]
            warps += 1
    assert clocks > 0
    return dict(updates=clocks, waits=waits, warps=warps,
                max_step_ns=max_step, max_lead_insns=max_lead,
                callback_lateness_ns=callbacks)


def run_guest(qemu, out, mode, skew=True, trace=False, delay_cpu=None,
              native_icount=False, **params):
    elf = build_guest(out, mode, skew)
    cmd = command(qemu, elf, smp=1 if mode == 1 else 2, skew=skew, **params)
    if mode == 11:
        cmd[cmd.index("-M") + 1] += ",virtualization=on"
    if native_icount:
        cmd[cmd.index("-accel") + 1] = "tcg,thread=single"
        cmd += ["-icount", "shift=0,sleep=off"]
    name = f"mode{mode}-{'skew' if skew else 'baseline'}-{len(list(out.glob('*.log')))}"
    log = out / (name + ".log")
    if delay_cpu is not None:
        source = Path(__file__).resolve().parent
        plugin = out / "skew-delay.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
                        *shlex.split(subprocess.check_output(
                            ["pkg-config", "--cflags", "glib-2.0"], text=True)),
                        "-I", str(source.parents[3] / "include" / "qemu"),
                        str(source / "skew-delay.c"), "-o", str(plugin)], check=True)
        cmd += ["-plugin", f"{plugin},cpu={delay_cpu},ns=100000"]
    if trace:
        cmd += ["-trace", "enable=skew_*", "-D", str(log.with_suffix(".trace"))]
        cmd += ["-trace", "enable=arm_gt_timer_expire"]
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - start
    output = proc.stdout + proc.stderr
    log.write_text(output)
    assert proc.returncode == 0 and "PASS" in output, (cmd, output[-4000:])
    result = dict(mode=mode, skew=skew, seconds=elapsed, command=cmd,
                  delay_cpu=delay_cpu, native_icount=native_icount)
    rows = re.findall(r"^(\w+) (\d+) (\d+)$", output, re.M)
    result["records"] = [(name, int(a), int(b)) for name, a, b in rows]
    ips = params.get("ips", 2000000000)
    window = params.get("window", 1000000)
    freq = next(int(a) for name, a, b in rows if name == "FREQ")
    if mode == 1 and skew and not native_icount:
        for name, count, ticks in result["records"]:
            if name.startswith("COUNT_"):
                expected = count * freq / ips
                # Unified time may lag by one window at either marker.
                tolerance = 2 * window * freq / 10**9 + 32
                assert abs(ticks - expected) <= tolerance, (count, ticks, expected)
    if trace:
        result["trace"] = parse_trace(log.with_suffix(".trace"), ips, window)
    return result


def exact_counts(qemu, out, tiny=False):
    elf = build_guest(out, 9 if tiny else 1)
    symbols = {}
    for row in subprocess.check_output(["aarch64-linux-gnu-nm", str(elf)],
                                       text=True).splitlines():
        cols = row.split()
        if len(cols) == 3:
            symbols[cols[2]] = int(cols[0], 16)
    with tempfile.TemporaryDirectory(prefix="skew-") as temp:
        temp = Path(temp)
        qp, gp = temp / "qmp", temp / "gdb"
        cmd = command(qemu, elf, smp=1)
        if tiny:
            cmd = command(qemu, elf, smp=1, window=7, update=3)
        cmd += ["-S", "-qmp", f"unix:{qp},server=on,wait=off",
                "-gdb", f"unix:{gp},server=on,wait=off"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        qmp = gdb = None
        try:
            wait_socket(qp, proc)
            wait_socket(gp, proc)
            qmp, gdb = QMP(qp), GDB(gp)
            cpu = qmp.cmd("query-cpus-fast")[0]["qom-path"]
            results = []
            cases = [("short_loop", "short_end", 64000002),
                     ("long_loop", "long_end", 64000002),
                     ("long_loop", "long_end", 128000002),
                     ("fault_probe", "fault_end", 36)]
            if tiny:
                cases = [("short_loop", "short_end", 36),
                         ("long_loop", "long_end", 226),
                         ("fault_probe", "fault_end", 36)]
            for begin, end, expected in cases:
                gdb.bp(symbols[begin])
                assert gdb.cmd("c").startswith("T05")
                before = qmp.get(cpu, "skew-raw-icount")
                gdb.bp(symbols[begin], False)
                gdb.bp(symbols[end])
                assert gdb.cmd("c").startswith("T05")
                after = qmp.get(cpu, "skew-raw-icount")
                gdb.bp(symbols[end], False)
                assert after - before == expected, (begin, after - before, expected)
                results.append(dict(loop=begin, raw_delta=after - before,
                                    expected=expected))
            # Time must be bit-identical during QMP stop, including no idle warp.
            clock = qmp.get("/machine", "skew-time")
            raw = qmp.get(cpu, "skew-raw-icount")
            time.sleep(0.25)
            assert qmp.get("/machine", "skew-time") == clock
            assert qmp.get(cpu, "skew-raw-icount") == raw
            qmp.cmd("quit")
            proc.communicate(timeout=10)
            return dict(exact_counts=results, paused_ns=clock, pause_host_ms=250,
                        tiny_window=tiny)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            if qmp:
                qmp.close()
            if gdb:
                gdb.sock.close()


def controls(qemu, out, idle=False):
    elf = build_guest(out, 10 if idle else 8)
    with tempfile.TemporaryDirectory(prefix="skew-") as temp:
        temp = Path(temp)
        qp, serial_path = temp / "qmp", temp / "serial"
        cmd = command(qemu, elf)
        index = cmd.index("-serial")
        cmd[index + 1] = f"unix:{serial_path},server=on,wait=off"
        cmd += ["-S", "-qmp", f"unix:{qp},server=on,wait=off"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        qmp = serial = None
        try:
            wait_socket(qp, proc)
            wait_socket(serial_path, proc)
            qmp = QMP(qp)
            serial = socket.socket(socket.AF_UNIX)
            serial.connect(str(serial_path))
            cpus = qmp.cmd("query-cpus-fast")
            tids = [cpu["thread-id"] for cpu in cpus]
            assert len(tids) == len(set(tids)) == 2
            affinity = sorted(os.sched_getaffinity(0))
            if len(affinity) >= 2:
                for tid, core in zip(tids, affinity[:2]):
                    os.sched_setaffinity(tid, {core})
            qmp.cmd("cont")
            time.sleep(0.1)
            if idle:
                clock = qmp.get("/machine", "skew-time")
                raw = [qmp.get(c["qom-path"], "skew-raw-icount") for c in cpus]
                time.sleep(0.25)
                assert qmp.get("/machine", "skew-time") == clock
                assert [qmp.get(c["qom-path"], "skew-raw-icount")
                        for c in cpus] == raw
                serial.sendall(b"x")
                stdout, stderr = proc.communicate(timeout=30)
                output = (stdout + stderr).decode()
                (out / "idle-wake.log").write_text(output)
                assert proc.returncode == 0 and "PASS" in output, output
                return dict(all_idle_no_timer=True, stable_host_ms=250,
                            stable_virtual_ns=clock, external_irq_wake=True)
            def runtime(tid):
                return int(Path(f"/proc/{proc.pid}/task/{tid}/schedstat")
                           .read_text().split()[0])
            before = [runtime(tid) for tid in tids]
            start = time.monotonic_ns()
            time.sleep(0.5)
            elapsed = time.monotonic_ns() - start
            runtime_ns = [runtime(tid) - b for tid, b in zip(tids, before)]
            assert all(t > 0 for t in runtime_ns)
            prior = 0
            for _ in range(100):
                qmp.cmd("stop")
                assert not qmp.cmd("query-status")["running"]
                clock = qmp.get("/machine", "skew-time")
                assert clock >= prior
                raw = [qmp.get(c["qom-path"], "skew-raw-icount") for c in cpus]
                time.sleep(0.002)
                assert qmp.get("/machine", "skew-time") == clock
                assert [qmp.get(c["qom-path"], "skew-raw-icount")
                        for c in cpus] == raw
                prior = clock
                qmp.cmd("cont")
                time.sleep(0.002)
            error = qmp.cmd("migrate", uri=f"file:{temp / 'migration'}",
                            _allow_error=True)
            assert "skew" in error.get("error", {}).get("desc", ""), error
            serial.sendall(b"x")
            stdout, stderr = proc.communicate(timeout=30)
            output = (stdout + stderr).decode()
            (out / "controls.log").write_text(output)
            assert proc.returncode == 0 and "PASS" in output, output
            return dict(control_cycles=100, vcpu_threads=tids,
                        vcpu_runtime_ns=runtime_ns, sample_wall_ns=elapsed,
                        parallel_runtime_ratio=sum(runtime_ns) / elapsed,
                        affinity=affinity[:2], migration_blocked=True,
                        tlbi_broadcast=True, external_uart=True)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            if qmp:
                qmp.close()
            if serial:
                serial.close()


def invalid_options(qemu):
    cases = ["tcg,thread=single,skew=1000000", "tcg,skew=1,skew-ips=1",
             "tcg,skew=1000000,skew-ips=0", "tcg,skew=1000000001",
             "tcg,skew=1000000,skew-update=0"]
    for accel in cases:
        proc = subprocess.run([str(qemu), "-M", "virt", "-accel", accel,
                               "-display", "none", "-S"], capture_output=True,
                              text=True, timeout=10)
        assert proc.returncode != 0 and "skew" in proc.stderr, proc.stderr
    proc = subprocess.run([str(qemu), "-M", "virt", "-accel", "tcg,skew=1000000",
                           "-icount", "shift=0", "-display", "none", "-S"],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0 and "skew" in proc.stderr, proc.stderr
    return dict(invalid_configurations_rejected=len(cases) + 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    qemu = args.qemu.resolve()
    results = []

    def save(result):
        results.append(result)
        (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("command", "records")}), flush=True)

    save(exact_counts(qemu, args.output))
    save(exact_counts(qemu, args.output, tiny=True))
    save(invalid_options(qemu))
    save(controls(qemu, args.output))
    save(controls(qemu, args.output, idle=True))
    for mode in (1, 2, 3, 4, 12):
        save(run_guest(qemu, args.output, mode, trace=True))
    if not args.quick:
        for mode in (1, 2, 3, 4):
            save(run_guest(qemu, args.output, mode, skew=False))
        for ips in (1000000000, 1999999973, 4000000000):
            save(run_guest(qemu, args.output, 1, ips=ips,
                           trace=ips == 1999999973))
        for window, update in ((100000, 10000), (1000000, 10000),
                               (10000000, 1000000), (100000, 1000000)):
            # A 10ms window exceeds the packet test's 5ms timeout margin.
            save(run_guest(qemu, args.output, 2 if window > 1000000 else 3,
                           window=window, update=update))
        for cpu in (0, 1):
            save(run_guest(qemu, args.output, 3, delay_cpu=cpu, trace=True))
        save(run_guest(qemu, args.output, 3, delay_cpu=1, skew=False))
        for mode in (6, 7, 11):
            save(run_guest(qemu, args.output, mode))
        for mode in (1, 4):
            save(run_guest(qemu, args.output, mode, skew=False, native_icount=True))
        for _ in range(3):
            for mode in (1, 2, 3):
                for skew in (False, True):
                    save(run_guest(qemu, args.output, mode, skew=skew))
    print("PASS: all executed checks", flush=True)


if __name__ == "__main__":
    main()

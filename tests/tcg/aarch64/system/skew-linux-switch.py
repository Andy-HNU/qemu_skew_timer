#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot ARM64 Linux and test MTTCG/skew QMP round trips from /init.

Requires a matching kernel and jitterentropy_rng.ko with built-in dependencies,
an AArch64 static libc toolchain, and cpio. See SKEW_QMP_zh.md for an example.
"""
import argparse
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time


def connect(path, proc):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(path))
            sock.settimeout(120)
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if proc.poll() is not None:
                raise RuntimeError('QEMU exited before socket connection')
            time.sleep(0.02)
    raise TimeoutError(str(path))


class QMP:
    def __init__(self, sock):
        self.sock = sock
        self.stream = sock.makefile('rb')
        self.records = [json.loads(self.stream.readline())]
        self.call('qmp_capabilities')

    def call(self, command, arguments=None, error=False):
        request = {'execute': command}
        if arguments is not None:
            request['arguments'] = arguments
        self.records.append(request)
        self.sock.sendall(json.dumps(request).encode() + b'\n')
        while True:
            line = self.stream.readline()
            if not line:
                raise RuntimeError('QMP disconnected')
            result = json.loads(line)
            self.records.append(result)
            if 'event' in result:
                continue
            if error:
                assert 'error' in result, result
                return result
            assert 'return' in result, result
            return result['return']


def build_init(args, out):
    root = out / 'root'
    (root / 'proc').mkdir(parents=True, exist_ok=True)
    (root / 'dev').mkdir(exist_ok=True)
    subprocess.run([args.cc, '-static', '-O2', '-Wall', '-Wextra', '-pthread',
                    str(pathlib.Path(__file__).with_suffix('.c')),
                    '-o', str(root / 'init')], check=True)
    shutil.copy2(args.jitter_module, root / 'jitterentropy_rng.ko')
    paths = ['.'] + [str(p.relative_to(root)) for p in sorted(root.rglob('*'))]
    archive = out / 'initramfs.cpio'
    with archive.open('wb') as f:
        subprocess.run(['cpio', '--null', '-o', '--format=newc'], cwd=root,
                       input=('\0'.join(paths) + '\0').encode(), stdout=f,
                       check=True)
    return archive


def control_checks(args):
    results = []
    for configured in [False, True]:
        with tempfile.TemporaryDirectory(prefix='skew-control-') as tmp:
            path = pathlib.Path(tmp) / 'qmp'
            accel = 'tcg,thread=multi'
            if configured:
                accel += ',skew=1000000,skew-defer=on'
            cmd = [str(args.qemu), '-M', 'virt', '-cpu', 'cortex-a57',
                   '-smp', '2', '-display', 'none', '-serial', 'none',
                   '-monitor', 'none', '-S', '-accel', accel,
                   '-qmp', f'unix:{path},server=on,wait=off']
            with (args.output / f'control-{configured}.log').open('w') as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                try:
                    sock = connect(path, proc)
                    qmp = QMP(sock)
                    if configured:
                        before = qmp.call('query-skew-clock')
                        after = qmp.call('skew-start')
                        assert before['mode'] == 'mttcg' and after['mode'] == 'skew'
                        assert after['virtual-ns'] == before['virtual-ns']
                        assert qmp.call('query-status')['status'] == 'prelaunch'
                        assert qmp.call('skew-start') == after
                        stopped = qmp.call('skew-stop')
                        assert stopped['virtual-ns'] == after['virtual-ns']
                        assert stopped['mode'] == 'mttcg'
                        assert qmp.call('skew-stop') == stopped
                        assert qmp.call('query-status')['status'] == 'prelaunch'
                        assert qmp.call('skew-start')['virtual-ns'] == stopped['virtual-ns']
                    else:
                        qmp.call('query-skew-clock', error=True)
                        qmp.call('skew-start', error=True)
                        qmp.call('skew-stop', error=True)
                    qmp.call('quit')
                    proc.wait(timeout=10)
                    assert proc.returncode == 0
                    results.append({'configured': configured, 'passed': True,
                                    'qmp': qmp.records})
                    qmp.stream.close()
                    sock.close()
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
    invalid = subprocess.run([str(args.qemu), '-M', 'virt', '-display', 'none',
                              '-accel', 'tcg,thread=multi,skew-defer=on'],
                             capture_output=True, timeout=10)
    assert invalid.returncode != 0
    assert b'skew-defer requires' in invalid.stderr
    results.append({'defer_without_window_rejected': True})
    (args.output / 'control-results.json').write_text(json.dumps(results, indent=2))


def run_case(args, archive, out, paused):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='skew-qmp-') as tmp:
        qpath = pathlib.Path(tmp) / 'qmp'
        spath = pathlib.Path(tmp) / 'serial'
        cmd = [str(args.qemu), '-M', 'virt,gic-version=3', '-cpu', 'cortex-a57',
               '-smp', '2', '-m', '512M', '-display', 'none', '-monitor', 'none',
               '-nic', 'none', '-no-reboot', '-S',
               '-serial', f'unix:{spath},server=on,wait=off',
               '-qmp', f'unix:{qpath},server=on,wait=off',
               '-accel', 'tcg,thread=multi,skew=1000000,skew-ips=2000000000,'
                         'skew-update=100000,skew-defer=on',
               '-kernel', str(args.kernel), '-initrd', str(archive),
               '-append', 'console=ttyAMA0 earlycon rdinit=/init nokaslr loglevel=7']
        (out / 'command.json').write_text(json.dumps(cmd, indent=2))
        with (out / 'qemu.log').open('w') as err, \
             (out / 'serial.log').open('wb') as log:
            proc = subprocess.Popen(cmd, stdout=err, stderr=subprocess.STDOUT)
            qmp = None
            serial_text = bytearray()
            try:
                qsock = connect(qpath, proc)
                serial = connect(spath, proc)
                qmp = QMP(qsock)

                def wait_for(marker):
                    end = time.monotonic() + 120
                    while marker.encode() not in serial_text:
                        if time.monotonic() > end:
                            raise TimeoutError(marker)
                        chunk = serial.recv(65536)
                        if not chunk:
                            raise RuntimeError('serial disconnected: ' + marker)
                        serial_text.extend(chunk)
                        log.write(chunk)
                        log.flush()
                        if b'SWITCH_TEST_FAIL' in serial_text:
                            raise RuntimeError(serial_text.decode(errors='replace'))

                initial = qmp.call('query-skew-clock')
                assert initial['mode'] == 'mttcg'
                assert not qmp.call('query-status')['running']
                qmp.call('cont')
                wait_for('READY_FOR_SWITCH phase=0')
                assert b'JITTER_INIT_PASS' in serial_text
                cpus = qmp.call('query-cpus-fast')

                def raw_counts():
                    return [qmp.call('qom-get', {'path': c['qom-path'],
                            'property': 'skew-raw-icount'}) for c in cpus]

                assert raw_counts() == [0, 0], 'MTTCG unexpectedly counted'
                phases = []

                def check_clock(state):
                    assert state['virtual-ns'] == (state['mttcg-elapsed-ns'] +
                                                  state['skew-elapsed-ns'])

                def check_reset(state, enabled):
                    assert state['global-icount'] == 0
                    for cpu in state['cpus']:
                        assert cpu['raw-icount'] == cpu['local-icount'] == 0
                        assert not cpu['active'] and not cpu['waiting']
                        assert cpu['budget-enabled'] == enabled

                for phase in range(4):
                    wait_for(f'READY_FOR_SWITCH phase={phase}')
                    mode = 'skew' if phase % 2 == 0 else 'mttcg'
                    command = 'skew-start' if mode == 'skew' else 'skew-stop'
                    if paused:
                        qmp.call('stop')
                    before = qmp.call('query-skew-clock')
                    switched = qmp.call(command)
                    check_clock(switched)
                    assert switched['mode'] == mode
                    assert switched['start-ns'] >= before['virtual-ns']
                    # Running MTTCG may advance after vm_start inside the command.
                    assert switched['virtual-ns'] >= switched['start-ns']
                    if paused:
                        assert switched['virtual-ns'] == before['virtual-ns']
                        check_reset(switched, mode == 'skew')
                        assert not qmp.call('query-status')['running']
                        time.sleep(0.02)
                        assert qmp.call('query-skew-clock') == switched
                        assert qmp.call(command) == switched
                        qmp.call('cont')
                    else:
                        assert qmp.call('query-status')['running']
                        if mode == 'mttcg':
                            check_reset(switched, False)
                    serial.sendall(b'GO\n')
                    snapshots = []
                    for _ in range(2):
                        time.sleep(0.03)
                        state = qmp.call('query-skew-clock')
                        check_clock(state)
                        assert state['mode'] == mode
                        if mode == 'skew':
                            assert state['mttcg-elapsed-ns'] == switched['mttcg-elapsed-ns']
                            for cpu in state['cpus']:
                                assert cpu['budget-enabled']
                                assert cpu['raw-icount'] > 0
                                if cpu['active']:
                                    lead = cpu['local-icount'] - state['global-icount']
                                    assert 0 <= lead <= state['window-insns']
                        else:
                            check_reset(state, False)
                            assert state['skew-elapsed-ns'] == switched['skew-elapsed-ns']
                        snapshots.append(state)
                    assert snapshots[1]['virtual-ns'] > snapshots[0]['virtual-ns']
                    phases.append(dict(phase=phase, before=before,
                                       switched=switched, snapshots=snapshots))
                    print(f'{out.name}: phase {phase} {mode} passed', flush=True)
                wait_for('READY_FOR_FINAL_CHECK')
                assert raw_counts() == [0, 0]
                qmp.call('stop')
                stopped = qmp.call('query-skew-clock')
                assert qmp.call('skew-stop') == stopped, 'deactivation is not idempotent'
                time.sleep(0.05)
                assert qmp.call('query-skew-clock') == stopped, 'paused clock advanced'
                qmp.call('cont')
                serial.sendall(b'DONE\n')
                wait_for('SWITCH_TEST_PASS')
                proc.wait(timeout=30)
                assert proc.returncode == 0
                result = dict(paused_switch=paused, phases=phases,
                              stopped=stopped, passed=True)
                (out / 'results.json').write_text(json.dumps(result, indent=2))
                print(f'{out.name}: round trips PASS', flush=True)
                serial.close()
                qmp.stream.close()
                qsock.close()
                return result
            finally:
                if qmp:
                    (out / 'qmp.json').write_text(json.dumps(qmp.records, indent=2))
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu', type=pathlib.Path)
    parser.add_argument('--kernel', required=True, type=pathlib.Path)
    parser.add_argument('--jitter-module', required=True, type=pathlib.Path)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    parser.add_argument('--cc', default='aarch64-linux-gnu-gcc')
    args = parser.parse_args()
    for field in ['qemu', 'kernel', 'jitter_module', 'output']:
        setattr(args, field, getattr(args, field).resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    control_checks(args)
    archive = build_init(args, args.output)
    results = [run_case(args, archive, args.output / name, paused)
               for name, paused in [('running', False), ('paused', True)]]
    (args.output / 'results.json').write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()

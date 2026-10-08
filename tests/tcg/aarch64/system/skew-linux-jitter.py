#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Test jitterentropy initialization and use while skew stays enabled."""
import argparse
from contextlib import closing
import json
import pathlib
import shutil
import subprocess
import tempfile
import time

from skew_test_io import QemuPipe


class QMP:
    def __init__(self, stream):
        self.stream = stream
        self.records = [json.loads(self.stream.readline())]
        self.call('qmp_capabilities')

    def call(self, command, arguments=None):
        request = {'execute': command}
        if arguments is not None:
            request['arguments'] = arguments
        self.records.append(request)
        self.stream.write(json.dumps(request).encode() + b'\n')
        while True:
            result = json.loads(self.stream.readline())
            self.records.append(result)
            if 'event' not in result:
                assert 'return' in result, result
                return result['return']


def build_init(args):
    root = args.output / 'root'
    (root / 'proc').mkdir(parents=True, exist_ok=True)
    (root / 'dev').mkdir(exist_ok=True)
    subprocess.run([args.cc, '-static', '-O2', '-Wall', '-Wextra',
                    str(pathlib.Path(__file__).with_suffix('.c')),
                    '-o', str(root / 'init')], check=True)
    for module in args.modules:
        shutil.copy2(module, root / module.name)
    paths = ['.'] + [str(p.relative_to(root)) for p in sorted(root.rglob('*'))]
    archive = args.output / 'initramfs.cpio'
    with archive.open('wb') as stream:
        subprocess.run(['cpio', '--null', '-o', '--format=newc'], cwd=root,
                       input=('\0'.join(paths) + '\0').encode(), stdout=stream,
                       check=True)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu', type=pathlib.Path)
    parser.add_argument('--kernel', required=True, type=pathlib.Path)
    parser.add_argument('--module-dir', required=True, type=pathlib.Path)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    parser.add_argument('--cc', default='aarch64-linux-gnu-gcc')
    args = parser.parse_args()
    args.qemu = args.qemu.resolve()
    args.kernel = args.kernel.resolve()
    args.module_dir = args.module_dir.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    args.modules = [args.module_dir / name for name in
                    ['af_alg.ko', 'jitterentropy_rng.ko', 'algif_rng.ko']]
    for path in [args.qemu, args.kernel, *args.modules]:
        if not path.is_file():
            parser.error(f'missing file: {path}')
    archive = build_init(args)

    with tempfile.TemporaryDirectory(prefix='skew-jitter-') as tmp:
        qpath = pathlib.Path(tmp) / 'qmp'
        spath = pathlib.Path(tmp) / 'serial'
        cmd = [str(args.qemu), '-M', 'virt,gic-version=3', '-cpu', 'cortex-a57',
               '-smp', '2', '-m', '512M', '-display', 'none', '-monitor', 'none',
               '-nic', 'none', '-no-reboot',
               '-serial', f'pipe:{spath}',
               '-qmp', f'pipe:{qpath}',
               '-accel', 'tcg,thread=multi,skew=1000000,skew-ips=2000000000,'
                         'skew-update=100000',
               '-kernel', str(args.kernel), '-initrd', str(archive),
               '-append', 'console=ttyAMA0 earlycon rdinit=/init nokaslr loglevel=7']
        (args.output / 'command.json').write_text(json.dumps(cmd, indent=2))
        with (args.output / 'qemu.log').open('w') as qlog, \
             (args.output / 'serial.log').open('wb') as slog, \
             closing(QemuPipe(qpath, timeout=200)) as qstream, \
             closing(QemuPipe(spath, timeout=200)) as serial:
            proc = subprocess.Popen(cmd, stdout=qlog, stderr=subprocess.STDOUT)
            qstream.process = serial.process = proc
            qmp = None
            text = bytearray()
            samples = []
            next_sample = time.monotonic()
            try:
                qmp = QMP(qstream)
                while b'JITTER_TEST_PASS' not in text:
                    chunk = serial.read(65536)
                    if not chunk:
                        raise RuntimeError('serial disconnected')
                    text.extend(chunk)
                    slog.write(chunk)
                    slog.flush()
                    if b'JITTER_TEST_FAIL' in text:
                        raise RuntimeError(text.decode(errors='replace'))
                    if time.monotonic() >= next_sample:
                        state = qmp.call('query-skew-clock')
                        assert state['mode'] == 'skew'
                        assert abs(state['visible-bias-ns']) <= state['window-ns']
                        assert state['virtual-ns'] >= state['model-ns'] - state['window-ns']
                        assert state['virtual-ns'] <= state['model-ns'] + state['window-ns']
                        assert state['visible-slope-q32'] <= 1 << 32
                        samples.append(state)
                        next_sample = time.monotonic() + 0.01
                proc.wait(timeout=30)
                assert proc.returncode == 0
                output = text.decode(errors='replace')
                assert 'JITTER_INIT_PASS' in output
                assert 'JITTER_USE_PASS reads=256' in output
                assert len(samples) >= 2
                virtual = [sample['virtual-ns'] for sample in samples]
                assert all(b >= a for a, b in zip(virtual, virtual[1:]))
                result = {'passed': True, 'samples': samples,
                          'sample_count': len(samples)}
                (args.output / 'results.json').write_text(json.dumps(result, indent=2))
                (args.output / 'qmp.json').write_text(json.dumps(qmp.records, indent=2))
                print(f'PASS: jitter init and 256 reads; samples={len(samples)}')
            finally:
                if qmp:
                    (args.output / 'qmp.json').write_text(
                        json.dumps(qmp.records, indent=2))
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


if __name__ == '__main__':
    main()

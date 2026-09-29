#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run skew regressions and ARM64 Linux acceptance, preserving all evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    source = Path(__file__).resolve().parent
    root = source.parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu', type=Path)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--quick', action='store_true',
                        help='Run the 11-case guest subset instead of the full matrix')
    parser.add_argument('--include-slow-icount', action='store_true',
                        help='Include icount in the performance matrix (can be very slow)')
    parser.add_argument('--kernel', type=Path)
    parser.add_argument('--module-dir', type=Path,
                        help='Directory containing af_alg.ko, algif_rng.ko and jitterentropy_rng.ko')
    parser.add_argument('--linux-repeats', type=int, default=1)
    args = parser.parse_args()
    if bool(args.kernel) != bool(args.module_dir):
        parser.error('--kernel and --module-dir must be supplied together')
    if args.linux_repeats < 1:
        parser.error('--linux-repeats must be positive')
    qemu, build, output = (p.resolve() for p in
                           [args.qemu, args.build_dir, args.output])
    output.mkdir(parents=True, exist_ok=False)
    files = [qemu, root / 'accel/tcg/skew.c', root / 'include/exec/exec-budget.h',
             root / 'include/exec/gen-icount.h',
             root / 'accel/tcg/tcg-accel-ops-mttcg.c']
    files += sorted(source.glob('skew*'))
    if args.kernel:
        args.kernel = args.kernel.resolve()
        args.module_dir = args.module_dir.resolve()
        files += [args.kernel] + [args.module_dir / name for name in
                                  ['af_alg.ko', 'algif_rng.ko', 'jitterentropy_rng.ko']]
    for path in files:
        if not path.is_file():
            parser.error(f'Missing input: {path}')
    metadata = {
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        'version': subprocess.check_output([str(qemu), '--version'], text=True).splitlines()[0],
        'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'linux_requested': bool(args.kernel),
        'slow_icount_requested': args.include_slow_icount,
        'sched_schedstats': Path('/proc/sys/kernel/sched_schedstats').read_text().strip()
                           if Path('/proc/sys/kernel/sched_schedstats').is_file() else None,
    }
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    results = []

    def run(name, command, timeout=900):
        command = [str(p) for p in command]
        start = time.monotonic()
        record = {'name': name, 'command': command, 'passed': False}
        try:
            with (output / (name + '.log')).open('w') as log:
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                try:
                    proc.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise
            record['returncode'] = proc.returncode
            record['passed'] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            record['error'] = f'timeout after {timeout}s'
        finally:
            record['host_seconds'] = time.monotonic() - start
            results.append(record)
            (output / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
        print(f"{'PASS' if record['passed'] else 'FAIL'}: {name}", flush=True)
        if not record['passed']:
            raise SystemExit(f"See {output / (name + '.log')}")

    run('exec-budget', [build / 'tests/unit/test-exec-budget'])
    for name in ['skew-momentum-anchor', 'skew-visible-cas', 'skew-perf-sweep-test']:
        run(name, [sys.executable, source / (name + '.py')])
    run('skew-visible-switch', [sys.executable, source / 'skew-visible-switch.py',
                                '--build-dir', build])
    command = [sys.executable, source / 'skew-check.py', qemu,
               '--output', output / 'guest']
    if args.quick:
        command.append('--quick')
    run('guest', command)
    run('performance-smoke',
        [sys.executable, source / 'skew-perf-sweep.py', '--qemu', qemu,
         '--output', output / 'performance-smoke', '--cpus', '2,3',
         '--scenario', 'all', '--work', '200000', '--phases', '4',
         '--rounds', '1', '--warmups', '0', '--timeout', '120'] +
        ([] if args.include_slow_icount else ['--skip-icount']))
    if args.kernel:
        for iteration in range(args.linux_repeats):
            run(f'linux-switch-{iteration + 1}',
                [sys.executable, source / 'skew-linux-switch.py', qemu,
                 '--kernel', args.kernel, '--jitter-module', args.module_dir / 'jitterentropy_rng.ko',
                 '--output', output / f'linux-switch-{iteration + 1}'])
        run('linux-jitter', [sys.executable, source / 'skew-linux-jitter.py', qemu,
                            '--kernel', args.kernel, '--module-dir', args.module_dir,
                            '--output', output / 'linux-jitter'])
    print('PASS: all requested suites; Linux ' +
          ('included' if args.kernel else 'not requested'), flush=True)


if __name__ == '__main__':
    main()

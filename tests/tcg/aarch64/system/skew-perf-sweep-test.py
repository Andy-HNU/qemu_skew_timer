#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Host-side workload accounting and report integrity tests (no QEMU needed)."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'sweep', Path(__file__).with_name('skew-perf-sweep.py'))
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)


class WorkloadTests(unittest.TestCase):
    def test_odd_cpu_phased_rounding(self):
        # 3 CPUs: first stage has 9 shares, second has 6; 1200/stage.
        self.assertEqual(sweep.bench.workload_plan('phased', 3, 2400, 2),
                         dict(compute=2397, memory=0, stages=6, wakes=0))

    def test_mixed_accounts_for_all_four_kinds(self):
        self.assertEqual(sweep.bench.workload_plan('mixed', 3, 9600, 2),
                         dict(compute=8397, memory=1200, stages=24, wakes=2))

    def test_idle_rotates_half_the_members(self):
        self.assertEqual(sweep.bench.workload_plan('idle', 18, 144000, 8),
                         dict(compute=144000, memory=0, stages=144, wakes=72))

    def test_legacy_uneven_tail(self):
        self.assertEqual(sweep.bench.workload_plan('uneven', 3, 1001)['compute'], 1000)

    def test_reports_exclude_diagnostics_and_incomplete_groups(self):
        with tempfile.TemporaryDirectory() as name:
            out = Path(name)
            config = dict(scenarios=['idle', 'mixed'], cpus=[2], rounds=1,
                          warmups=0, phases=2, work=9600, host_logical=2,
                          host_physical=1)
            rows = [dict(scenario='idle', cpus=2, mode=m, tag='0',
                         host_seconds=t) for m, t in zip(sweep.MODES, [1, 2, 4])]
            rows += [dict(scenario='idle', cpus=2, mode='skew', tag='0',
                          host_seconds=0.01, diagnostic_trace=True),
                     dict(scenario='mixed', cpus=2, mode='skew', tag='0',
                          host_seconds=0.5)]
            (out / 'runs.jsonl').write_text('\n'.join(map(json.dumps, rows)))
            sweep.report(out, config, '未完成')
            summary = json.loads((out / 'summary.json').read_text())
            self.assertEqual(summary[0]['skew']['median'], 2)
            self.assertTrue(summary[0]['complete'])
            self.assertFalse(summary[1]['complete'])
            self.assertTrue((out / 'trend-idle.svg').exists())
            self.assertFalse((out / 'trend-mixed.svg').exists())
            with (out / 'summary.csv').open() as f:
                exported = list(csv.DictReader(f))
            self.assertEqual(len(exported), 1)
            self.assertEqual(float(exported[0]['icount_over_skew']), 2)


if __name__ == '__main__':
    unittest.main()

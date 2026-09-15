#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""One-command MTTCG/skew/icount host-time sweep (Linux / WSL2)."""
import argparse
import csv
import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys

SRC = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('skew_perf', SRC / 'skew-perf.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
MODES = ('mttcg', 'skew', 'icount')


def host_cpus():
    ids = sorted(os.sched_getaffinity(0))
    physical = set()
    for cpu in ids:
        base = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        try:
            physical.add(((base / 'physical_package_id').read_text().strip(),
                          (base / 'core_id').read_text().strip()))
        except OSError:
            return ids, None
    return ids, len(physical)


def draw(out, points, logical):
    """Standalone SVG, without plotting packages or network access."""
    if not points:
        return
    max_cpu = max(r['cpus'] for r in points)
    maximum = max(r[m]['maximum'] for r in points for m in MODES) * 1.1
    x = lambda n: 85 + 835 * (n-1) / max(1, max_cpu-1)
    y = lambda t: 390 - 325 * t / maximum
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="460" viewBox="0 0 960 460">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<g font-family="sans-serif" font-size="13" fill="#334155">',
           '<text x="85" y="24" font-size="18">Host wall time: median and min/max (lower is better)</text>']
    for i in range(6):
        t = maximum * i / 5
        svg += [f'<path d="M 85 {y(t):.1f} H 920" stroke="#e2e8f0"/>',
                f'<text x="75" y="{y(t)+4:.1f}" text-anchor="end">{t:.3f}s</text>']
    ticks = points[::max(1, math.ceil(len(points)/15))]
    if points[-1] not in ticks:
        ticks.append(points[-1])
    for r in ticks:
        svg.append(f'<text x="{x(r["cpus"]):.1f}" y="415" text-anchor="middle">{r["cpus"]}</text>')
    svg.append('<text x="470" y="445">Guest vCPUs</text>')
    if logical <= max_cpu:
        svg += [f'<path d="M {x(logical):.1f} 65 V 390" stroke="#64748b" stroke-dasharray="5 4"/>',
                f'<text x="{x(logical)-5:.1f}" y="60" text-anchor="end">Host logical CPUs: {logical}</text>']
    for i, (mode, color) in enumerate(zip(MODES, ('#2563eb', '#059669', '#dc2626'))):
        coords = ' '.join(f'{x(r["cpus"]):.1f},{y(r[mode]["median"]):.1f}' for r in points)
        svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        for r in points:
            v = r[mode]
            svg += [f'<path d="M {x(r["cpus"]):.1f} {y(v["maximum"]):.1f} V {y(v["minimum"]):.1f}" stroke="{color}"/>',
                    f'<circle cx="{x(r["cpus"]):.1f}" cy="{y(v["median"]):.1f}" r="3" fill="{color}"/>']
        svg.append(f'<text x="{100+i*140}" y="47" fill="{color}">{mode}</text>')
    (out / 'trend.svg').write_text('\n'.join(svg + ['</g></svg>']))


def report(out, config, state):
    path = out / 'runs.jsonl'
    rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    rows = [r for r in rows if r['tag'].isdigit()]
    summary = []
    for n in config['cpus']:
        item = {'cpus': n, 'complete': True}
        for mode in MODES:
            values = [r['host_seconds'] for r in rows if r['cpus'] == n and r['mode'] == mode]
            if len(values) != config['rounds']:
                item['complete'] = False
            if values:
                item[mode] = dict(median=statistics.median(values), minimum=min(values),
                                  maximum=max(values), samples=values)
        summary.append(item)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    lines = ['# MTTCG / skew / icount：宿主耗时趋势', '',
             f'状态：{state}。正式样本：{len(rows)}/{len(config["cpus"])*3*config["rounds"]}。', '',
             f'本进程可用逻辑 CPU：{config["host_logical"]}；系统暴露的可用物理核：{config["host_physical"]}。',
             f'场景：`{config["scenario"]}`；work：{config["work"]}；预热 {config["warmups"]} 次，正式 {config["rounds"]} 次。', '',
             '时间为宿主 CLOCK_MONOTONIC 的 START 到 DONE，中位数 [最小, 最大]，单位秒。',
             '仅完整配置进入下表和曲线；校准及预热不计入成绩。', '',
             '| vCPU | MTTCG 秒 | skew 秒 | icount 秒 | icount/skew 加速比 | skew 耗时降低 | skew 相对 MTTCG 耗时增加 |',
             '|---:|---:|---:|---:|---:|---:|---:|']
    with (out / 'summary.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['vcpus', 'mttcg_seconds', 'skew_seconds', 'icount_seconds',
                         'icount_over_skew', 'reduction_percent', 'overhead_vs_mttcg_percent'])
        for r in summary:
            if not r['complete']:
                continue
            mt, sk, ic = [r[m]['median'] for m in MODES]
            writer.writerow([r['cpus'], mt, sk, ic, ic/sk, (1-sk/ic)*100, (sk/mt-1)*100])
            times = [f'{r[m]["median"]:.4f} [{r[m]["minimum"]:.4f}, {r[m]["maximum"]:.4f}]' for m in MODES]
            lines.append(f'| {r["cpus"]} | ' + ' | '.join(times) +
                         f' | {ic/sk:.2f}× | {(1-sk/ic)*100:.1f}% | {(sk/mt-1)*100:.1f}% |')
    draw(out, [r for r in summary if r['complete']], config['host_logical'])
    if (out / 'trend.svg').exists():
        lines += ['', '![宿主耗时趋势](trend.svg)']
    lines += ['', '- total 固定总循环量，随 vCPU 平分；percpu 固定每个 vCPU 的循环量，总工作量随 vCPU 增加。',
              '- 数量是 guest vCPU。MTTCG/skew 每 vCPU 一个执行线程；icount 单线程轮转。QEMU 另有主线程等辅助线程。',
              '- MTTCG 是同一二进制关闭 skew/icount 的基线；三种模式使用相同 guest 和计时插件。',
              '- skew：window=1ms、IPS=10^9、update=100μs；icount：shift=0、sleep=off。',
              '- 每循环 32 条有用指令；total 整数除法尾数舍去，实际量记录在 runs.jsonl。',
              '- 预热是独立 QEMU 进程，不保留 TB 缓存；正式顺序固定种子随机交错，不删波动样本。',
              '- 裸机计算负载包含同步、窗口等待、抢占和测量区内首次翻译，不代表真实应用性能。',
              '- CPU 数取 Linux/WSL 拓扑和进程 affinity；WSL 配额、容器 CPU quota、超线程及其他负载会影响拐点。',
              '- 小于 0.1 秒的样本易受噪声影响，可增大 --work；正式比较建议 --rounds 7。', '',
              '参数：[config.json](config.json)；环境：[environment.txt](environment.txt)；',
              '原始样本：[runs.jsonl](runs.jsonl)；表格：[summary.csv](summary.csv)。', '']
    (out / 'REPORT_zh.md').write_text('\n'.join(lines))


def main():
    ids, physical = host_cpus()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--max-cpus', type=int, default=min(512, 2*len(ids)), help='default: twice available logical CPUs, capped at 512')
    ap.add_argument('--step', type=int, default=2)
    ap.add_argument('--cpus', help='explicit list, e.g. 2,4,12,24 (overrides range)')
    ap.add_argument('--qemu', type=Path, default=bench.ROOT / 'build/qemu-system-aarch64')
    ap.add_argument('--output', type=Path, default=bench.ROOT / 'build' / datetime.datetime.now().strftime('skew-sweep-%Y%m%d-%H%M%S-%f'))
    ap.add_argument('--scenario', choices=['total', 'percpu'], default='total')
    ap.add_argument('--work', type=int, help='fixed iterations: total or per CPU; skips calibration')
    ap.add_argument('--target-seconds', type=float, default=2.0, help='calibration target for one icount vCPU')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--warmups', type=int, default=1)
    ap.add_argument('--timeout', type=float, default=180, help='per process, including startup')
    ap.add_argument('--plan', action='store_true', help='print matrix without building/running')
    args = ap.parse_args()
    if not 1 <= args.max_cpus <= 512 or args.step < 1:
        ap.error('max-cpus must be 1..512; step must be positive')
    try:
        cpus = sorted(set(map(int, args.cpus.split(',')))) if args.cpus else sorted(set([*range(2, args.max_cpus+1, args.step), args.max_cpus]))
    except ValueError:
        ap.error('cpus must be comma-separated integers')
    if not cpus or min(cpus) < 1 or max(cpus) > 512:
        ap.error('each vCPU count must be 1..512 (virt machine limit)')
    if args.rounds < 1 or args.warmups < 0 or not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.target_seconds) or args.target_seconds <= 0:
        ap.error('rounds, timeout, target-seconds must be positive; warmups must be nonnegative')
    if args.work is not None and not max(cpus) <= args.work <= 10**12:
        ap.error('work must be >= largest vCPU count and <= 10^12')
    config = dict(cpus=cpus, modes=MODES, host_logical=len(ids), host_physical=physical,
                  affinity=ids, scenario=args.scenario, rounds=args.rounds, warmups=args.warmups,
                  work=args.work, target_seconds=args.target_seconds, timeout=args.timeout,
                  qemu=str(args.qemu.resolve()), gic=3, seed=20260915,
                  invocation=sys.argv, output=str(args.output.resolve()))
    print(json.dumps(config, indent=2), flush=True)
    if args.plan:
        return 0
    required = ['cc', 'pkg-config', 'aarch64-linux-gnu-gcc', 'aarch64-linux-gnu-nm', 'git', 'lscpu', 'free', 'date', 'uname']
    missing = [name for name in required if not shutil.which(name)]
    if missing or not args.qemu.is_file():
        ap.error(f'missing tools: {missing}; QEMU exists: {args.qemu.is_file()}. See docs/clock-model/SKEW_SWEEP_zh.md')
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error('output must be new or empty; previous results will not be overwritten')
    out.mkdir(parents=True, exist_ok=True)
    state = '未完成'
    def save():
        (out / 'config.json').write_text(json.dumps(config, indent=2))
    save()
    try:
        plugin = bench.setup(out)
        bench.environment(args.qemu.resolve(), out)
        def run(n, mode, tag, work):
            return bench.run(args.qemu.resolve(), out, plugin, args.scenario, n, work,
                             mode, tag, gic=3, timeout=args.timeout)
        if args.work is None:
            sample = run(1, 'icount', 'calibration', 12000000)
            config['work'] = min(10**12, max(max(cpus), int(12000000 * args.target_seconds / sample['host_seconds'])))
            print(f'Calibrated work={config["work"]}; fixed for entire sweep', flush=True)
            save()
        for n in cpus:
            for mode in MODES:
                for warm in range(args.warmups):
                    run(n, mode, f'warmup{warm}', config['work'])
        rng = random.Random(config['seed'])
        for repeat in range(args.rounds):
            order = [(n, m) for n in cpus for m in MODES]
            rng.shuffle(order)
            for n, mode in order:
                run(n, mode, str(repeat), config['work'])
                report(out, config, state)
        state = '完成'
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        state = '中断' if isinstance(exc, KeyboardInterrupt) else '失败'
        (out / 'error.txt').write_text(str(exc))
        print(f'{state}: {exc}', file=sys.stderr)
        return 1
    finally:
        config['status'] = state
        save()
        report(out, config, state)
        print(f'Report: {out / "REPORT_zh.md"}', flush=True)


if __name__ == '__main__':
    sys.exit(main())

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
import re
import shutil
import statistics
import subprocess
import sys

SRC = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('skew_perf', SRC / 'skew-perf.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
MODES = ('mttcg', 'skew', 'icount')
SUITE = ('total', 'phased', 'memory', 'idle', 'mixed')


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


def draw(out, points, logical, filename):
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
    (out / filename).write_text('\n'.join(svg + ['</g></svg>']))


def report(out, config, state):
    path = out / 'runs.jsonl'
    rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    rows = [r for r in rows if r['tag'].isdigit() and not r.get('diagnostic_trace')]
    summary = []
    for scenario in config['scenarios']:
        for n in config['cpus']:
            item = {'scenario': scenario, 'cpus': n, 'complete': True}
            for mode in MODES:
                values = [r['host_seconds'] for r in rows
                          if (r['scenario'], r['cpus'], r['mode']) == (scenario, n, mode)]
                if len(values) != config['rounds']:
                    item['complete'] = False
                if values:
                    item[mode] = dict(median=statistics.median(values), minimum=min(values),
                                      maximum=max(values), samples=values)
            if config['work'] is not None:
                item['work_plan'] = bench.workload_plan(scenario, n, config['work'], config['phases'])
            summary.append(item)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    expected = len(config['scenarios'])*len(config['cpus'])*3*config['rounds']
    lines = ['# MTTCG / skew / icount：宿主耗时趋势', '',
             f'状态：{state}。正式样本：{len(rows)}/{expected}。', '',
             f'可用逻辑 CPU：{config["host_logical"]}；系统暴露的可用物理核：{config["host_physical"]}。',
             f'场景：{", ".join(config["scenarios"])}；work：{config["work"]}；phases：{config["phases"]}。',
             f'每配置预热 {config["warmups"]} 次、正式 {config["rounds"]} 次。', '',
             '时间为宿主 CLOCK_MONOTONIC 的 START 到 DONE，中位数 [最小, 最大]，单位秒。',
             '仅完整配置进入表格和曲线；校准、预热及 trace 诊断不计入成绩。']
    with (out / 'summary.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['scenario', 'vcpus', 'mttcg_seconds', 'skew_seconds', 'icount_seconds',
                         'icount_over_skew', 'reduction_percent', 'overhead_vs_mttcg_percent'])
        for scenario in config['scenarios']:
            lines += ['', f'## {scenario}', '',
                      '| vCPU | MTTCG 秒 | skew 秒 | icount 秒 | icount/skew 加速比 | skew 耗时降低 | skew 相对 MTTCG 耗时增加 |',
                      '|---:|---:|---:|---:|---:|---:|---:|']
            points = [r for r in summary if r['scenario'] == scenario and r['complete']]
            for r in points:
                mt, sk, ic = [r[m]['median'] for m in MODES]
                writer.writerow([scenario, r['cpus'], mt, sk, ic, ic/sk, (1-sk/ic)*100, (sk/mt-1)*100])
                times = [f'{r[m]["median"]:.4f} [{r[m]["minimum"]:.4f}, {r[m]["maximum"]:.4f}]' for m in MODES]
                lines.append(f'| {r["cpus"]} | ' + ' | '.join(times) +
                             f' | {ic/sk:.2f}× | {(1-sk/ic)*100:.1f}% | {(sk/mt-1)*100:.1f}% |')
            figure = 'trend.svg' if len(config['scenarios']) == 1 else f'trend-{scenario}.svg'
            draw(out, points, config['host_logical'], figure)
            if (out / figure).exists():
                lines += ['', f'![{scenario} 宿主耗时趋势]({figure})']
    lines += ['', '## 负载与解释', '',
              '- total：固定总计算量，各核平分，无中途 barrier。',
              '- phased：约半数核每阶段执行其他核 4 倍计算量，奇偶角色轮换，阶段末自旋 barrier。',
              '- memory：各核一半计算、一半访问自己的 512 KiB 数据区，固定步长遍历，阶段末自旋 barrier。',
              '- idle：约半数核计算后 WFI，运行中的 leader 按固定工作进度通过 SGI 唤醒它们；每阶段轮换。',
              '- mixed：同一 guest 中重复 total → phased → memory → idle，各分配约四分之一有用工作量；一次 START/DONE 覆盖全部阶段。',
              '- phased/memory/idle 默认各 8 个阶段；mixed 默认 8 轮、32 个阶段。阶段同步开销包含在计时内。',
              '- 自旋 barrier 在 icount 单线程轮转下可能放大耗时；这里比较计算、同步与调度的整体效果，不能当作纯算术加速比。',
              '- 间歇休眠由 guest 工作进度触发，没有按 guest 时间控制工作量；至少一核保持运行，不测试全 idle 跳时。',
              '- WFI 调用不必然代表宿主线程睡眠；用 --diagnose-active 的独立 trace 检查实际 active 退出和重入。',
              '- 每个计算/访存循环都是 32 条主体指令；阶段整数除法尾数舍去，实际两类工作量及唤醒次数由 guest 和 host 交叉校验。',
              '- percpu 为每核固定计算量；uneven 是旧的一次性 1:…:1:2 分配，作为兼容选项保留，不属于默认五项。',
              '- 三种模式使用同一个二进制及相同 guest。MTTCG 关闭 skew/icount；skew window=1ms、IPS=10^9、update=100μs；icount shift=0、sleep=off。',
              '- 正式运行固定种子随机交错；预热是独立进程，不保留 TB 缓存；不删波动样本。',
              '- 核数是 guest vCPU，icount 仍为单线程轮转；超线程、NUMA、CPU quota、宿主负载可能影响趋势。',
              '- 内存初始化在 START 之前；校验、同步、首次翻译和等待在计时内。裸机混合负载不等同于真实 OS 应用。',
              '- 小于 0.1 秒的样本建议加大 --work；正式比较建议 --rounds 7。', '',
              '参数：[config.json](config.json)；环境：[environment.txt](environment.txt)；',
              '原始样本：[runs.jsonl](runs.jsonl)；表格：[summary.csv](summary.csv)。', '']
    if (out / 'active-diagnostics.json').exists():
        lines += ['独立诊断：[active-diagnostics.json](active-diagnostics.json)，耗时未并入正式成绩。', '']
    (out / 'REPORT_zh.md').write_text('\n'.join(lines))


def diagnose(out, qemu, plugin, config):
    results = []
    n = max(config['cpus'])
    for scene in config['scenarios']:
        if scene not in ('idle', 'mixed'):
            continue
        sample = bench.run(qemu, out, plugin, scene, n, config['work'], 'skew', 'diagnostic',
                           gic=3, ram=512, phases=config['phases'], timeout=config['timeout'], trace=True)
        path = out / f'{scene}-{n}-diagnostic.trace'
        states, reentries = {}, {}
        for stamp, cpu, active in re.findall(r'host_ns=(\d+) cpu=(\d+) active=([01])', path.read_text()):
            if not sample['host_start_ns'] <= int(stamp) <= sample['host_end_ns']:
                continue
            if active == '1' and states.get(cpu) == '0':
                reentries[cpu] = reentries.get(cpu, 0) + 1
            states[cpu] = active
        results.append(dict(scenario=scene, cpus=n, reentries=reentries,
                            total_reentries=sum(reentries.values()),
                            intended_wakes=sample['activity']['irq_wakes'],
                            host_start_ns=sample['host_start_ns'], host_end_ns=sample['host_end_ns'],
                            trace=str(path)))
        (out / 'active-diagnostics.json').write_text(json.dumps(results, indent=2))
        if not reentries:
            raise RuntimeError(f'No active re-entry observed in {path}; increase --work')


def main():
    ids, physical = host_cpus()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--max-cpus', type=int, default=min(512, 2*len(ids)), help='default: twice available logical CPUs, capped at 512')
    ap.add_argument('--step', type=int, default=2)
    ap.add_argument('--cpus', help='explicit list, e.g. 2,4,12,24 (overrides range)')
    ap.add_argument('--qemu', type=Path, default=bench.ROOT / 'build/qemu-system-aarch64')
    ap.add_argument('--output', type=Path, default=bench.ROOT / 'build' / datetime.datetime.now().strftime('skew-sweep-%Y%m%d-%H%M%S-%f'))
    ap.add_argument('--scenario', choices=['all', *SUITE, 'percpu', 'uneven'], default='all')
    ap.add_argument('--phases', type=int, default=8, help='stages per kind; mixed has 4 x phases stages')
    ap.add_argument('--diagnose-active', action='store_true', help='extra skew trace at largest vCPU count, excluded from results')
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
    scenarios = list(SUITE) if args.scenario == 'all' else [args.scenario]
    staged = any(s in scenarios for s in ('phased', 'memory', 'idle', 'mixed'))
    if not 1 <= args.phases <= 64:
        ap.error('phases must be 1..64')
    if staged and min(cpus) < 2:
        ap.error('staged scenarios need >= 2 vCPUs; use --scenario total for 1 vCPU')
    minimum_work = max(cpus) + (1 if args.scenario == 'uneven' else 0)
    if staged:
        minimum_work = 4 * max(cpus) * args.phases * (4 if 'mixed' in scenarios else 1)
    if args.work is not None and not minimum_work <= args.work <= 10**12:
        ap.error(f'work must be >= {minimum_work} for {args.scenario} and <= 10^12')
    config = dict(cpus=cpus, modes=MODES, host_logical=len(ids), host_physical=physical,
                  affinity=ids, scenario=args.scenario, scenarios=scenarios, phases=args.phases,
                  diagnose_active=args.diagnose_active, ram_mib=512, rounds=args.rounds, warmups=args.warmups,
                  work=args.work, target_seconds=args.target_seconds, timeout=args.timeout,
                  qemu=str(args.qemu.resolve()), gic=3, seed=20260915,
                  planned_warmup_runs=len(scenarios)*len(cpus)*len(MODES)*args.warmups,
                  planned_formal_runs=len(scenarios)*len(cpus)*len(MODES)*args.rounds,
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
        def run(scene, n, mode, tag, work):
            return bench.run(args.qemu.resolve(), out, plugin, scene, n, work,
                             mode, tag, gic=3, timeout=args.timeout, ram=512, phases=args.phases)
        if args.work is None:
            sample = run('total', 1, 'icount', 'calibration', 12000000)
            config['work'] = min(10**12, max(minimum_work, int(12000000 * args.target_seconds / sample['host_seconds'])))
            print(f'Calibrated work={config["work"]}; fixed for entire sweep', flush=True)
            save()
        for scene in scenarios:
            for n in cpus:
                for mode in MODES:
                    for warm in range(args.warmups):
                        run(scene, n, mode, f'warmup{warm}', config['work'])
        rng = random.Random(config['seed'])
        for repeat in range(args.rounds):
            order = [(s, n, m) for s in scenarios for n in cpus for m in MODES]
            rng.shuffle(order)
            for scene, n, mode in order:
                run(scene, n, mode, str(repeat), config['work'])
                report(out, config, state)
        if args.diagnose_active:
            diagnose(out, args.qemu.resolve(), plugin, config)
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

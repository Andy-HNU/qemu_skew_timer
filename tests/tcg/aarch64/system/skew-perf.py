#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fixed-work host wall-time benchmark; no guest clock controls the workload."""
import argparse, hashlib, json, random, re, shlex, statistics, subprocess, time
from pathlib import Path
SRC=Path(__file__).resolve().parent
ROOT=SRC.parents[3]
NAMES=['total','percpu','barrier','uneven']
WORKLOADS=NAMES+['phased','memory','idle','mixed']

def workload_plan(scenario,cpus,work,phases=8):
    """Independent host-side accounting for the staged guest's useful work."""
    if scenario in NAMES:
        chunks=128 if scenario=='barrier' else 1
        if scenario=='percpu': count=cpus*work
        elif scenario=='uneven': count=(work//(cpus+1))*(cpus+1)
        else: count=(work//cpus//chunks)*cpus*chunks
        return dict(compute=count,memory=0,stages=0,wakes=0)
    kinds=['total','phased','memory','idle'] if scenario=='mixed' else [scenario]
    allocation=work//len(kinds)//phases
    result=dict(compute=0,memory=0,stages=phases*len(kinds)*cpus,wakes=0)
    for kind in kinds:
        n=allocation//cpus
        if kind=='phased':
            for p in range(phases):
                shares=cpus+3*((cpus+1-(p&1))//2)
                result['compute']+=allocation//shares*shares
        elif kind=='memory':
            result['compute']+=n//2*cpus*phases
            result['memory']+=(n-n//2)*cpus*phases
        else:
            result['compute']+=n*cpus*phases
            if kind=='idle': result['wakes']+=cpus//2*phases
    return result

def setup(out):
    out.mkdir(parents=True,exist_ok=True)
    plugin=out/'clock.so'
    subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Werror',
        *shlex.split(subprocess.check_output(['pkg-config','--cflags','glib-2.0'],text=True)),
        '-I',str(ROOT/'include/qemu'),str(SRC/'skew-perf-clock.c'),'-o',str(plugin)],check=True)
    return plugin

def environment(qemu,out):
    # 在预热之前收集环境，不计入任何测量区间。
    chunks=[]
    for cmd in [['date','--iso-8601=seconds'],['uname','-a'],['lscpu'],['free','-h'],
                ['git','-C',str(ROOT),'rev-parse','HEAD'],[str(qemu),'--version'],
                ['aarch64-linux-gnu-gcc','--version']]:
        r=subprocess.run(cmd,capture_output=True,text=True)
        chunks.append('$ '+' '.join(cmd)+'\n'+r.stdout+r.stderr)
    digest=hashlib.sha256()
    with qemu.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): digest.update(block)
    chunks.append('QEMU SHA256: '+digest.hexdigest())
    options=qemu.parent/'meson-info/intro-buildoptions.json'
    if options.exists():
        selected={x['name']:x['value'] for x in json.loads(options.read_text())
                  if x['name'] in ['debug','optimization','werror','b_ndebug']}
        chunks.append('Build options: '+json.dumps(selected))
    (out/'environment.txt').write_text('\n'.join(chunks))

def build(out,scenario,cpus,work,gic=2,phases=8):
    elf=out/f'{scenario}-{cpus}-{work}-gic{gic}-p{phases}.elf'
    if not elf.exists():
        subprocess.run(['aarch64-linux-gnu-gcc','-O2','-g','-ffreestanding',
          '-fno-stack-protector','-fno-pie','-no-pie','-nostdlib','-mgeneral-regs-only',
          '-march=armv8-a','-mno-outline-atomics',f'-DCPUS={cpus}',f'-DWORK={work}UL',
          f'-DSCENARIO={WORKLOADS.index(scenario)}',f'-DPHASES={phases}',
          f'-DAFFINITY_SIZE={8 if gic == 2 else 16}','-Wl,--build-id=none','-T',str(SRC/'skew.ld'),
          str(SRC/'skew-perf-boot.S'),str(SRC/'skew-perf-guest.c'),'-o',str(elf)],check=True)
    nm=subprocess.check_output(['aarch64-linux-gnu-nm',str(elf)],text=True)
    pc=re.search(r'^([0-9a-f]+) T marker_store$',nm,re.M).group(1)
    return elf,pc

def run(qemu,out,plugin,scenario,cpus,work,mode,tag,*,gic=2,timeout=600,phases=8,ram=128,trace=False):
    elf,pc=build(out,scenario,cpus,work,gic,phases)
    accel='tcg,thread=single' if mode=='icount' else 'tcg,thread=multi'
    if mode=='skew': accel+=',skew=1000000,skew-ips=1000000000,skew-update=100000'
    cmd=[str(qemu),'-M',f'virt,gic-version={gic}','-cpu','cortex-a57','-accel',accel,
        '-smp',str(cpus),'-m',f'{ram}M','-display','none','-serial','none','-monitor','none',
        '-semihosting-config','enable=on,target=native','-kernel',str(elf),
        '-plugin',f'{plugin},{pc}']
    if mode=='icount': cmd+=['-icount','shift=0,sleep=off']
    if trace:
        cmd+=['-trace',f'enable=skew_cpu,file={out / (scenario+"-"+str(cpus)+"-"+tag+".trace")}']
    start=time.monotonic()
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Keep startup errors and the exact command even for an incomplete run.
        def decode(value):
            return value.decode(errors='replace') if isinstance(value,bytes) else (value or '')
        (out/f'{scenario}-{cpus}-{mode}-{tag}.log').write_text(
            decode(exc.stdout)+decode(exc.stderr)+'\nTIMEOUT\n'+shlex.join(cmd))
        raise
    elapsed=time.monotonic()-start
    text=r.stdout+r.stderr
    log=out/f'{scenario}-{cpus}-{mode}-{tag}.log'
    log.write_text(text)
    matches=re.findall(r'^HOST_BENCH_NS ([0-9]+)$',text,re.M)
    if r.returncode or len(matches)!=1: raise RuntimeError((cmd,r.returncode,text[-2000:]))
    ranges=re.findall(r'^HOST_BENCH_RANGE (\d+) (\d+)$',text,re.M)
    if len(ranges)!=1: raise RuntimeError('missing host interval: '+str(log))
    begin,end=map(int,ranges[0])
    if end<=begin or end-begin!=int(matches[0]):
        raise RuntimeError('invalid host interval: '+str(log))
    plan=workload_plan(scenario,cpus,work,phases)
    activity=None
    if scenario not in NAMES:
        records=re.findall(r'^GUEST_WORK (\d+) (\d+) (\d+) (\d+) (\d+)$',text,re.M)
        if len(records)!=1: raise RuntimeError('missing guest work verification: '+str(log))
        c,m,sleeps,wakes,stages=map(int,records[0])
        if (c,m,wakes,stages)!=(plan['compute'],plan['memory'],plan['wakes'],plan['stages']) or sleeps<plan['wakes']:
            raise RuntimeError(('guest work mismatch',records[0],plan,str(log)))
        activity=dict(compute=c,memory=m,wfi_calls=sleeps,irq_wakes=wakes,stage_visits=stages)
    iterations=plan['compute']+plan['memory']
    row=dict(scenario=scenario,cpus=cpus,work=work,mode=mode,tag=tag,
        host_seconds=int(matches[0])/1e9,process_seconds=elapsed,
        useful_iterations=iterations,useful_body_insns=iterations*32,command=cmd,
        activity=activity,phases=phases,diagnostic_trace=trace,
        host_start_ns=begin,host_end_ns=end)
    with (out/'runs.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
    print(json.dumps({k:row[k] for k in ['scenario','cpus','mode','tag','host_seconds']}),flush=True)
    return row

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--qemu',type=Path,default=ROOT/'build/qemu-system-aarch64')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--work',type=int,default=12000000)
    ap.add_argument('--rounds',type=int,default=7)
    ap.add_argument('--pilot',action='store_true')
    args=ap.parse_args(); out=args.output.resolve()
    if (out/'runs.jsonl').exists():
        ap.error('output already contains runs.jsonl; choose a new directory')
    if args.work < 768 or args.rounds < 1:
        ap.error('work must be >= 768 and rounds must be positive')
    plugin=setup(out)
    environment(args.qemu.resolve(),out)
    modes=['icount','skew','mttcg']
    if args.pilot:
        for n in [1,6]:
            for mode in modes: run(args.qemu,out,plugin,'total',n,args.work,mode,'pilot')
        for scenario in ['barrier','uneven']:
            for mode in modes: run(args.qemu,out,plugin,scenario,6,args.work,mode,'pilot')
        return
    configs=[(s,n) for s in NAMES for n in ([1,2,4,6] if s in ['total','percpu'] else [2,4,6])]
    # Per-CPU work is 1/6 of fixed total, keeping largest parallel case comparable.
    def work(s): return args.work//6 if s=='percpu' else args.work
    for s,n in configs:
        for mode in modes: run(args.qemu,out,plugin,s,n,work(s),mode,'warmup')
    rng=random.Random(20260914)
    for r in range(args.rounds):
        order=configs.copy(); rng.shuffle(order)
        for s,n in order:
            paired=modes.copy(); rng.shuffle(paired)
            for mode in paired: run(args.qemu,out,plugin,s,n,work(s),mode,str(r))
    rows=[json.loads(x) for x in (out/'runs.jsonl').read_text().splitlines()]
    rows=[r for r in rows if r['tag'].isdigit()]
    summary=[]
    for s,n in configs:
        item=dict(scenario=s,cpus=n)
        for mode in modes:
            values=[r['host_seconds'] for r in rows if (r['scenario'],r['cpus'],r['mode'])==(s,n,mode)]
            item[mode]=dict(median=statistics.median(values),minimum=min(values),maximum=max(values),samples=values)
        a=item['icount']['median']; b=item['skew']['median']
        item.update(speedup=a/b,reduction_percent=(1-b/a)*100)
        summary.append(item)
    (out/'summary.json').write_text(json.dumps(summary,indent=2))
    print('PASS: benchmark complete',flush=True)
if __name__=='__main__': main()

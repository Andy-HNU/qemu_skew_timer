#!/usr/bin/env python3
"""Run real guest CNTVCT experiments under WSL/Linux; all failures retained."""
import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
CASES={
 "wide-boundary":dict(ips=4294967296,window=1000000,update=100000,gap=16384,n=8000,cross=0,burst=0),
 "wide-max":dict(ips=1000000000000,window=1000,update=100000,gap=16384,n=8000,cross=0,burst=0),
 "steady":dict(ips=200000000,window=1000000,update=100000,gap=64,n=4000,cross=0,burst=0),
 "dense":dict(ips=200000000,window=1000000,update=100000,gap=0,n=4000,cross=0,burst=0),
 "high-2g":dict(ips=2000000000,window=1000000,update=100000,gap=64,n=4000,cross=0,burst=0),
 "high-20g":dict(ips=20000000000,window=1000000,update=100000,gap=64,n=4000,cross=0,burst=0),
 "saturation":dict(ips=20000000000,window=10000,update=100000000,gap=0,n=4000,cross=0,burst=0,delay_us=100),
 "saturation-2g":dict(ips=2000000000,window=10000,update=100000000,gap=0,n=4000,cross=0,burst=0,delay_us=100),
 "cross-cpu":dict(ips=200000000,window=1000000,update=100000,gap=64,n=2000,cross=1,burst=0),
 "burst":dict(ips=200000000,window=1000000,update=100000,gap=64,n=4000,cross=0,burst=1),
 "sustained-200m":dict(ips=200000000,window=1000000,update=100000,gap=16384,n=8000,cross=0,burst=0),
 "sustained-2g":dict(ips=2000000000,window=1000000,update=100000,gap=16384,n=8000,cross=0,burst=0),
 "sustained-20g":dict(ips=20000000000,window=1000000,update=100000,gap=16384,n=8000,cross=0,burst=0),
}
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def stats(trace, ips=None):
    opener=gzip.open if str(trace).endswith(".gz") else open
    with opener(trace,"rt",newline="") as f: rows=list(csv.DictReader(f))
    ints=["seq","host_ns","cpu","global_icount","model_ns","visible_ns","window_ns","slope_q32","counter_ticks"]
    for r in rows:
        for key in ints:r[key]=int(r[key])
    reads=[r for r in rows if r["event"] in ("read","bound_hit")]
    dv=[b["visible_ns"]-a["visible_ns"] for a,b in zip(reads,reads[1:])]
    dm=[b["model_ns"]-a["model_ns"] for a,b in zip(reads,reads[1:])]
    dt=[b["counter_ticks"]-a["counter_ticks"] for a,b in zip(reads,reads[1:])]
    plateaus=[b["host_ns"]-a["host_ns"] for a,b in zip(reads,reads[1:])
              if b["model_ns"]==a["model_ns"] and b["visible_ns"]==a["visible_ns"]==
              a["model_ns"]+a["window_ns"]]
    per_cpu={}
    for cpu in sorted({r["cpu"] for r in reads}):
        stream=[r for r in reads if r["cpu"]==cpu]
        per_cpu[cpu]=sum(b["counter_ticks"]<a["counter_ticks"] for a,b in zip(stream,stream[1:]))
    return dict(samples=len(rows),reads=len(reads),
        counter_conversion_violations=sum(r["counter_ticks"]!=r["visible_ns"]//16 for r in reads),
        model_formula_violations=sum(r["model_ns"]!=r["global_icount"]*1000000000//ips for r in rows) if ips else None,
        visible_backward=sum(x<0 for x in dv),counter_backward=sum(x<0 for x in dt),
        per_cpu_counter_backward=per_cpu,
        publication_backward=sum(b["visible_ns"]<a["visible_ns"] for a,b in zip(rows,rows[1:])),
        model_backward=sum(b["model_ns"]<a["model_ns"] for a,b in zip(rows,rows[1:])),
        host_order_violations=sum(b["host_ns"]<a["host_ns"] for a,b in zip(rows,rows[1:])),
        bounds_violations=sum(abs(r["visible_ns"]-r["model_ns"])>r["window_ns"] for r in rows),
        max_abs_bias_ns=max((abs(r["visible_ns"]-r["model_ns"]) for r in rows),default=0),
        slope_violations=sum(r["slope_q32"]>2**32 for r in rows),
        counter_equal_pairs=sum(x==0 for x in dt),
        counter_equal_fraction=sum(x==0 for x in dt)/len(dt) if dt else None,
        model_equal_fraction=sum(x==0 for x in dm)/len(dm) if dm else None,
        visible_delta_variance_ns2=statistics.pvariance(dv) if dv else None,
        model_delta_variance_ns2=statistics.pvariance(dm) if dm else None,
        bound_hits=sum(r["event"]=="bound_hit" for r in reads),
        bound_hits_positive_stored_slope=sum(r["event"]=="bound_hit" and r["slope_q32"]>0 for r in reads),
        plateau_pairs=len(plateaus),plateau_observed_host_ns=sum(plateaus),
        updates=sum(r["event"]=="update" for r in rows),
        resumes=sum(r["event"]=="resume" for r in rows))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--repeats",type=int,default=7)
    p.add_argument("--cases",nargs="+",choices=list(CASES),default=list(CASES))
    a=p.parse_args();out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    baseline=ROOT/"build/qemu-system-aarch64"
    probe=ROOT/"build/exp5-probe/qemu-system-aarch64"
    source=ROOT/"tests/tcg/aarch64/system"
    manifest=dict(evidence="measured",started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        baseline_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        binaries={str(x):digest(x) for x in (baseline,probe)},guest_source_sha256=digest(HERE/"counter-guest.c"),
        uname=subprocess.check_output(["uname","-a"],text=True).strip(),
        lscpu=subprocess.check_output(["lscpu"],text=True),
        repetitions=a.repeats,warmups=1,seed=20260920,cases={k:CASES[k] for k in a.cases},
        instrumentation="BQL-ordered records in memory; buffered until exit; tracing overhead is not performance evidence",
        counter_frequency_hz=62500000)
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2))
    shutil.copy2(ROOT/"build/exp5-probe/build-manifest.json",out/"build-manifest.json")
    elves={}
    for name in a.cases:
        c=CASES[name];elf=out/(name+".elf")
        cmd=["aarch64-linux-gnu-gcc","-O2","-g","-ffreestanding","-fno-stack-protector",
             "-fno-pie","-no-pie","-nostdlib","-mgeneral-regs-only","-march=armv8-a",
             "-mno-outline-atomics","-Wl,--build-id=none","-T",str(source/"skew.ld"),
             str(source/"skew-boot.S"),str(HERE/"counter-guest.c"),
             f"-DNREADS={c['n']}",f"-DGAP={c['gap']}",f"-DCROSS={c['cross']}",
             f"-DBURST={c['burst']}","-o",str(elf)]
        subprocess.run(cmd,check=True);elves[name]=elf
        (out/(name+"-build.json")).write_text(json.dumps(cmd,indent=2))
    results=[]
    schedule=[(name,-1,kind) for name in a.cases for kind in ("baseline","probe")]
    formal=[(name,rep,kind) for rep in range(a.repeats) for name in a.cases for kind in ("baseline","probe")]
    random.Random(20260920).shuffle(formal);schedule+=formal
    for name,rep,kind in schedule:
        c=CASES[name];label=f"{name}-{kind}-{rep}"
        trace=out/(label+".csv");binary=baseline if kind=="baseline" else probe
        cmd=[str(binary),"-L",str(ROOT/"build/pc-bios"),"-nic","none","-M","virt,gic-version=2","-cpu","cortex-a57",
             "-accel",f"tcg,thread=multi,skew={c['window']},skew-ips={c['ips']},skew-update={c['update']}",
             "-smp","2" if c["cross"] else "1","-m","128M","-display","none","-serial","none",
             "-monitor","none","-semihosting-config","enable=on,target=native",
             "-kernel",str(elves[name])]
        env=os.environ.copy();env.pop("SKEW_EXP5_TRACE",None)
        env.pop("SKEW_EXP5_READ_DELAY_US",None)
        if kind=="probe":env["SKEW_EXP5_TRACE"]=str(trace)
        if kind=="probe" and c.get("delay_us"):
            env["SKEW_EXP5_READ_DELAY_US"]=str(c["delay_us"])
        started=time.monotonic()
        timeout=False
        try:
            run=subprocess.run(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=45)
            output=run.stdout;rc=run.returncode
        except subprocess.TimeoutExpired as exc:
            output=exc.stdout or b"";rc=None;timeout=True
        duration=time.monotonic()-started
        (out/(label+".log")).write_bytes(output)
        (out/(label+"-command.json")).write_text(json.dumps(cmd,indent=2))
        match=re.search(rb"EXP5_GUEST reads=(\d+) backwards=(\d+) repeats=(\d+) frequency=(\d+)",output)
        item=dict(case=name,repeat=rep,kind=kind,host_seconds=duration,returncode=rc,timeout=timeout,
                  guest_match=bool(match),passed=rc==0 and bool(match))
        if match:
            item["guest"]=dict(zip(("reads","backwards","repeats","frequency"),map(int,match.groups())))
            item["passed"] &= item["guest"]["backwards"]==0 and item["guest"]["reads"]==c["n"]*(1+c["cross"])
        if kind=="probe" and trace.exists():
            item["metrics"]=stats(trace,c["ips"])
            recorder=re.search(rb"EXP5_RECORDER samples=(\d+) dropped=(\d+) unlocked=(\d+)",output)
            item["recorder"]=list(map(int,recorder.groups())) if recorder else None
            item["passed"] &= bool(recorder) and item["recorder"][1:]==[0,0]
            item["passed"] &= item["metrics"]["reads"]==c["n"]*(1+c["cross"])
            item["passed"] &= all(item["metrics"][key]==0 for key in
                ("visible_backward","counter_backward","publication_backward","model_backward",
                 "host_order_violations","bounds_violations","slope_violations",
                 "counter_conversion_violations","model_formula_violations"))
            with trace.open("rb") as f,gzip.open(str(trace)+".gz","wb") as z:shutil.copyfileobj(f,z)
            if rep!=0:trace.unlink()
        results.append(item)
        (out/"runs.json").write_text(json.dumps(results,indent=2))
        print(label,"PASS" if item["passed"] else "FAIL",f"{duration:.3f}s",
              item.get("metrics",{}).get("bound_hits",""),flush=True)
    formal_results=[r for r in results if r["repeat"]>=0]
    summary={}
    for name in a.cases:
        group=[r for r in formal_results if r["case"]==name]
        summary[name]={}
        for kind in ("baseline","probe"):
            g=[r for r in group if r["kind"]==kind]
            summary[name][kind]={"runs":len(g),"passed":sum(r["passed"] for r in g),
                "host_seconds_median":statistics.median(r["host_seconds"] for r in g),
                "guest_reads":sum(r.get("guest",{}).get("reads",0) for r in g),
                "guest_backwards":sum(r.get("guest",{}).get("backwards",0) for r in g)}
            if kind=="probe":
                keys=("visible_backward","counter_backward","publication_backward","model_backward",
                      "host_order_violations","bounds_violations","slope_violations","bound_hits",
                      "plateau_pairs","plateau_observed_host_ns","bound_hits_positive_stored_slope")
                summary[name][kind]["totals"]={key:sum(r.get("metrics",{}).get(key,0) for r in g) for key in keys}
                for key in ("counter_equal_fraction","model_equal_fraction","visible_delta_variance_ns2","model_delta_variance_ns2","max_abs_bias_ns"):
                    values=[r["metrics"][key] for r in g if "metrics" in r and r["metrics"][key] is not None]
                    summary[name][kind][key]={"median":statistics.median(values),"min":min(values),"max":max(values)} if values else None
        summary[name]["observed_probe_to_baseline_host_ratio"]=None if CASES[name].get("delay_us") else summary[name]["probe"]["host_seconds_median"]/summary[name]["baseline"]["host_seconds_median"]
    (out/"summary.json").write_text(json.dumps(summary,indent=2))
    print("RESULT",sum(r["passed"] for r in formal_results),"/",len(formal_results))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)
if __name__=="__main__":main()

#!/usr/bin/env python3
"""Generate explicit synthetic examples for plot smoke tests, never evaluation."""
import csv
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DIRS = ["exp1_hw_calibration","exp2_host_speed","exp3_parallel",
        "exp4_skew_window","exp5_visible_time"]

def write(exp, rows):
    directory = ROOT/"experiments"/DIRS[exp-1]
    fields = json.loads((directory/"schema.json").read_text())["fields"]
    with (directory/"example.csv").open("w",newline="",encoding="utf-8") as stream:
        w = csv.DictWriter(stream,fieldnames=fields)
        w.writeheader()
        for i,row in enumerate(rows):
            w.writerow(dict(evidence="synthetic",source_log="synthetic:make_examples.py",
                            **({"run_id":f"example-{i}"} if "run_id" not in row else {}),
                            **row))

def main():
    rows=[]
    for i,name in enumerate(("RX","TX","Parsing","IRQ-task","Control")):
        for rep in range(3):
            hw=(i+1)*1000000
            rows.append(dict(workload=name,split="calibration" if i<3 else "validation",
                             hardware_ns=hw,instructions=int(hw*.2*(.9+.05*i+.01*rep)),
                             selected_ips=200000000))
    write(1,rows)
    rows=[]
    for method in ("mttcg","slowtime","icount","skew"):
        for cap in (20,40,60,80,100):
            for rep in range(3):
                host=int(100000000*100/cap*(1+.03*rep))
                guest=int(100000000*(100/cap if method=="mttcg" else 50/cap if method=="slowtime" else 1)*(1+.01*rep))
                completed=not(method=="mttcg" and cap==20 and rep==2)
                rows.append(dict(workload="example-only",method=method,capacity_pct=cap,
                    host_ns=host,guest_ns=guest,model_ns=100000000 if method=="skew" else "",
                    deadline_ns=150000000,deadline_miss=int(not completed or guest>150000000),
                    completed=int(completed)))
    write(2,rows)
    rows=[]
    for method,factor in (("mttcg",1),("icount",2.4),("skew",1.2)):
        for cpu in (1,2,4,8):
            for rep in range(3):
                scale=1 if method=="icount" else 1+(cpu-1)*.65
                rows.append(dict(workload="example-only",method=method,vcpus=cpu,
                    work_mode="fixed_total",host_ns=int(1e9*factor*(1+.03*rep)/scale),
                    instructions=200000000,completed=1))
    write(3,rows)
    rows=[]
    for i,w in enumerate((1000,10000,100000,1000000,10000000)):
        for rep in range(3):
            rows.append(dict(workload="example-only",window_insns=w,sim_ips=200000000,
                host_ns=int(1e9*(2-.2*i)*(1+.02*rep)),instructions=200000000,
                max_active_spread_insns=int(w*(.6+.03*rep)),max_lead_insns=int(w*.9),
                wait_count=int(10000/(i+1)),budget_exit_count=20000))
    write(4,rows)
    rows=[]
    model=0; visible=0; anchor=0; anchor_h=0; slope=.5
    for seq,h in enumerate(range(0,6000001,100000)):
        update=h in (0,2000000,4000000,6000000)
        if update:
            model={0:0,2000000:500000,4000000:1000000,6000000:1500000}[h]
            visible=max(visible,model-500000)
            anchor=visible; anchor_h=h
            slope={0:.5,2000000:.4,4000000:.75,6000000:.25}[h]
        visible=max(visible,min(model+500000,anchor+int((h-anchor_h)*slope)))
        event="update" if update else "bound_hit" if visible==model+500000 else "read"
        rows.append(dict(run_id="schematic-one",seq=seq,host_ns=h,cpu=seq%2,
                         global_icount=model//5,model_ns=model,visible_ns=visible,
                         window_ns=500000,slope_q32=int(slope*2**32),
                         event=event,counter_ticks=visible//16))
    write(5,rows)

if __name__=="__main__":
    main()

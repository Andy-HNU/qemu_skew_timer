#!/usr/bin/env python3
"""Render measured traces, preserving raw points; no smoothing or synthetic input."""
import csv
import json
import sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT/"paper/scripts"))
from plot import plot, style, save
import matplotlib.pyplot as plt
RESULT=HERE/"results/20260920"

def main():
    for label,relative,span in [
        ("steady","short/steady-probe-0.csv",None),
        ("cross-cpu","short/cross-cpu-probe-0.csv",None),
        ("saturation-2g","saturation2g/saturation-2g-probe-0.csv",None),
        ("saturation-onset","saturation2g/saturation-2g-probe-0.csv",8000000),
        ("sustained-2g","sustained/sustained-2g-probe-0.csv",2000000)]:
        source=RESULT/relative
        out=RESULT/"figures"/label;out.mkdir(parents=True,exist_ok=True)
        if span:
            with source.open(newline="") as f:
                reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
            first=next(int(r["host_ns"]) for r in rows if r["event"] in ("read","bound_hit"))
            start=0
            rule="trace start through first read plus span"
            if label=="sustained-2g":
                updates=[r for r in rows if r["event"]=="update" and int(r["global_icount"])>0]
                first=start=int(updates[8]["host_ns"])
                rule="ninth nonzero-global update through span; stable-history zoom"
            selected=[r for r in rows if start<=int(r["host_ns"])<=first+span]
            sliced=out/"selected-measured.csv"
            with sliced.open("w",newline="") as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(selected)
            (out/"selection.json").write_text(json.dumps(
                {"source":relative,"rule":rule,"start_ns":start,
                 "span_ns":span,"selected_rows":len(selected)},indent=2))
            source=sliced
        plot(5,source,out,measured=True)
    style()
    groups=json.loads((RESULT/"group-summary.json").read_text())
    selection=["steady","dense","cross-cpu","burst","sustained-2g","saturation-2g"]
    data=[next(r for r in groups if r["case"]==name) for name in selection]
    fig,ax=plt.subplots(figsize=(8,4))
    x=list(range(len(data)))
    ax.bar([n-.18 for n in x],[r["model_repeat_median"]*100 for r in data],
           width=.36,label="Model unchanged (same trace)",color="#D55E00")
    ax.bar([n+.18 for n in x],[r["counter_repeat_median"]*100 for r in data],
           width=.36,label="CNTVCT unchanged",color="#0072B2")
    ax.set(xticks=x,xticklabels=selection,ylabel="Repeated adjacent reads (%)")
    ax.tick_params(axis="x",rotation=15)
    ax.legend()
    save(fig,RESULT/"figures","measured-repeated-reads",False)
if __name__=="__main__":main()

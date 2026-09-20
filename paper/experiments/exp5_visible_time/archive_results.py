#!/usr/bin/env python3
"""Archive measured inputs and recompute all formulas, including early pilot-era checks."""
import csv
import difflib
import gzip
import hashlib
import json
import shutil
import statistics
import subprocess
from pathlib import Path
from run_matrix import ROOT, CASES, stats
HERE=Path(__file__).resolve().parent
DEST=HERE/"results/20260920"
BATCHES={"short":ROOT/"build/exp5-20260920",
         "sustained":ROOT/"build/exp5-sustained-20260920",
         "saturation2g":ROOT/"build/exp5-saturation2g-20260920"}
def main():
    DEST.mkdir(parents=True,exist_ok=True)
    records=[]
    for batch,src in BATCHES.items():
        dst=DEST/batch;dst.mkdir(exist_ok=True)
        for p in src.iterdir():
            if p.suffix in (".json",".log",".csv",".gz"):shutil.copy2(p,dst/p.name)
        for r in json.loads((src/"runs.json").read_text()):
            if r["repeat"]<0:continue
            r=dict(r);r["batch"]=batch
            if r["kind"]=="probe":
                name=f"{r['case']}-probe-{r['repeat']}.csv.gz"
                r["metrics"]=stats(src/name,CASES[r["case"]]["ips"])
                r["trace"]=str(Path(batch)/name)
                r["has_untruncated_ips"]=CASES[r["case"]]["ips"]<=0xffffffff
                r["joint_formulas_pass"]=all(r["metrics"][k]==0 for k in
                    ("model_formula_violations","counter_conversion_violations","bounds_violations",
                     "visible_backward","publication_backward","counter_backward","model_backward",
                     "host_order_violations","slope_violations"))
            records.append(r)
    (DEST/"audit.json").write_text(json.dumps(records,indent=2))
    patches=[]
    for original,generated in ((ROOT/"accel/tcg/skew.c",ROOT/"build/exp5-probe/skew.c"),
                               (ROOT/"target/arm/helper.c",ROOT/"build/exp5-probe/helper.c")):
        patches.extend(difflib.unified_diff(original.read_text().splitlines(True),
            generated.read_text().splitlines(True),fromfile=str(original.relative_to(ROOT)),
            tofile="experiment-copy/"+generated.name))
    (DEST/"instrumentation.patch").write_text("".join(patches))
    env=[]
    for cmd in (["gcc-10","--version"],["aarch64-linux-gnu-gcc","--version"],["ninja","--version"],["python3","--version"]):
        env.append(" ".join(cmd)+"\n"+subprocess.check_output(cmd,text=True))
    (DEST/"toolchain.txt").write_text("\n".join(env))
    groups=[]
    for case in dict.fromkeys(r["case"] for r in records):
        p=[r for r in records if r["case"]==case and r["kind"]=="probe"]
        b=[r for r in records if r["case"]==case and r["kind"]=="baseline"]
        row=dict(case=case,ips=CASES[case]["ips"],window_ns=CASES[case]["window"],
                 injected=bool(CASES[case].get("delay_us")),runs=len(p),
                 reads=sum(r["metrics"]["reads"] for r in p),
                 guest_pass=sum(r["guest_match"] and r["guest"]["backwards"]==0 for r in p),
                 model_bad_runs=sum(r["metrics"]["model_formula_violations"]>0 for r in p),
                 model_bad_samples=sum(r["metrics"]["model_formula_violations"] for r in p),
                 clock_bad_samples=sum(r["metrics"]["counter_backward"]+r["metrics"]["visible_backward"]+r["metrics"]["bounds_violations"] for r in p),
                 counter_mapping_bad=sum(r["metrics"]["counter_conversion_violations"] for r in p),
                 bound_hits=sum(r["metrics"]["bound_hits"] for r in p),
                 positive_slope_bound_hits=sum(r["metrics"]["bound_hits_positive_stored_slope"] for r in p),
                 plateau_pairs=sum(r["metrics"]["plateau_pairs"] for r in p),
                 min_updates=min(r["metrics"]["updates"] for r in p),
                 max_updates=max(r["metrics"]["updates"] for r in p),
                 model_repeat_median=statistics.median(r["metrics"]["model_equal_fraction"] for r in p),
                 counter_repeat_median=statistics.median(r["metrics"]["counter_equal_fraction"] for r in p),
                 visible_variance_median=statistics.median(r["metrics"]["visible_delta_variance_ns2"] for r in p),
                 model_variance_median=statistics.median(r["metrics"]["model_delta_variance_ns2"] for r in p),
                 probe_host_median=statistics.median(r["host_seconds"] for r in p),
                 baseline_host_median=statistics.median(r["host_seconds"] for r in b),
                 dropped=sum(r["recorder"][1] for r in p),unlocked=sum(r["recorder"][2] for r in p))
        groups.append(row)
    (DEST/"group-summary.json").write_text(json.dumps(groups,indent=2))
    with (DEST/"group-summary.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(groups[0]));w.writeheader();w.writerows(groups)
    reuse=ROOT/"build/exp5-reuse-20260920";dest=DEST/"reuse";dest.mkdir(exist_ok=True)
    for p in reuse.glob("*.json"):shutil.copy2(p,dest/p.name)
    for p in reuse.glob("*.log"):shutil.copy2(p,dest/p.name)
    for src in reuse.glob("jitter-*"):
        if src.is_dir():
            d=dest/src.name;d.mkdir(exist_ok=True)
            for p in src.iterdir():
                if p.suffix in (".json",".log"):shutil.copy2(p,d/p.name)
    quick=dest/"quick";quick.mkdir(exist_ok=True)
    for p in (reuse/"quick").iterdir():
        if p.suffix in (".json",".log"):
            shutil.copy2(p,quick/p.name)
        elif p.suffix==".trace":
            with p.open("rb") as f,gzip.open(quick/(p.name+".gz"),"wb") as z:
                shutil.copyfileobj(f,z)
    shutil.copytree(ROOT/"build/exp5-boundary-20260920",DEST/"ips-boundary",dirs_exist_ok=True)
    diagnosis=[]
    for label,relative in (("sustained-20g","sustained/sustained-20g-probe-0.csv"),
                           ("saturation-20g","short/saturation-probe-0.csv")):
        with (DEST/relative).open(newline="") as f: rows=list(csv.DictReader(f))
        rows=[r for r in rows if int(r["global_icount"])>0]
        ips=20000000000
        diagnosis.append(dict(case=label,source=relative,configured_ips=ips,uint32_ips=ips&0xffffffff,
            rows_nonzero_G=len(rows),
            nominal_mismatches=sum(int(r["model_ns"])!=int(r["global_icount"])*1000000000//ips for r in rows),
            uint32_mismatches=sum(int(r["model_ns"])!=int(r["global_icount"])*1000000000//(ips&0xffffffff) for r in rows),
            first_nonzero_sample=rows[0] if rows else None))
    (DEST/"ips-formula-diagnosis.json").write_text(json.dumps(diagnosis,indent=2))
    pilots=DEST/"pilots";pilots.mkdir(exist_ok=True)
    for name in ("exp5-pilot","exp5-pilot2","exp5-pilot3"):
        shutil.copy2(ROOT/"build"/(name+".log"),pilots/(name+".log"))
        shutil.copy2(ROOT/"build"/name/"summary.json",pilots/(name+"-summary.json"))
    hashes={str(p.relative_to(DEST)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(DEST.rglob("*")) if p.is_file() and p.name!="SHA256SUMS.json"}
    (DEST/"SHA256SUMS.json").write_text(json.dumps(hashes,indent=2))
    print("formal attempts",len(records),"instrumented reads",sum(r["reads"] for r in groups))
    for r in groups:
        print(r["case"],"model bad",r["model_bad_runs"],"hits",r["bound_hits"],
              "repeated",round(r["counter_repeat_median"]*100,3),"updates",r["min_updates"],r["max_updates"])
if __name__=="__main__":main()

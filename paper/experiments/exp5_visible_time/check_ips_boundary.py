#!/usr/bin/env python3
"""Audit wide-IPS configuration without changing the running VM or source."""
import argparse
import json
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
parser=argparse.ArgumentParser()
parser.add_argument("--out",type=Path,required=True)
args=parser.parse_args()
out=args.out.resolve()
out.mkdir(parents=True,exist_ok=True)
rows=[]
for ips in (200000000,2000000000,4000000000,4294967295,4294967296,4494967296,20000000000,1000000000000):
    cmd=[str(ROOT/"build/qemu-system-aarch64"),"-M","virt","-cpu","cortex-a57",
         "-smp","1","-display","none","-serial","none","-monitor","none","-nic","none",
         "-S","-accel",f"tcg,thread=multi,skew=1000000,skew-ips={ips},skew-update=100000",
         "-qmp","stdio"]
    request="\n".join(json.dumps({"execute":name,"id":i}) for i,name in enumerate(
        ("qmp_capabilities","query-skew-clock","quit")))+"\n"
    p=subprocess.run(cmd,input=request,text=True,capture_output=True,timeout=15)
    (out/f"{ips}.log").write_text(p.stdout+p.stderr)
    replies=[]
    for line in p.stdout.splitlines():
        try:replies.append(json.loads(line))
        except json.JSONDecodeError:pass
    query=next((r["return"] for r in replies if r.get("id")==1 and "return" in r),None)
    row={"ips":ips,"returncode":p.returncode,"accepted":query is not None,
         "expected_window_insns":ips//1000,"actual_window_insns":query["window-insns"] if query else None,
         "effective_uint32_ips":ips&0xffffffff,"command":cmd}
    rows.append(row)
    print({k:v for k,v in row.items() if k!="command"})
(out/"results.json").write_text(json.dumps(rows,indent=2))
assert all(r["accepted"] and r["returncode"]==0 and
           r["actual_window_insns"]==r["expected_window_insns"] for r in rows)

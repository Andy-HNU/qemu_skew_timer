#!/usr/bin/env python3
"""Re-run existing Linux jitter and quick acceptance without copying old results."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--repeats",type=int,default=7)
    a=p.parse_args();out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    kernel=ROOT/"build/linux-jitter-20260916/kernel/boot/vmlinuz-6.1.0-50-cloud-arm64"
    modules=ROOT/"build/linux-jitter-20260916/kernel/lib/modules/6.1.0-50-cloud-arm64/kernel/crypto"
    qemu=ROOT/"build/qemu-system-aarch64"
    manifest={"binaries":{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
               [qemu,kernel]+[modules/n for n in ("af_alg.ko","jitterentropy_rng.ko","algif_rng.ko")]},
               "mode":"skew continuously from reset","ips":2000000000,
               "window_ns":1000000,"update_ns":100000,"vcpus":2}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2))
    results=[]
    for i in range(-1,a.repeats):
        dest=out/f"jitter-{i}"
        cmd=["python3",str(ROOT/"tests/tcg/aarch64/system/skew-linux-jitter.py"),str(qemu),
             "--kernel",str(kernel),"--module-dir",str(modules),"--output",str(dest)]
        start=time.monotonic()
        with (out/f"jitter-{i}-runner.log").open("w") as log:
            proc=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:rc=proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait();rc=124
        result={"repeat":i,"host_seconds":time.monotonic()-start,"returncode":rc,"command":cmd,
                "passed":rc==0 and (dest/"results.json").is_file()}
        results.append(result)
        (out/"jitter-summary.json").write_text(json.dumps(results,indent=2))
        print("JITTER",i,"PASS" if result["passed"] else "FAIL",flush=True)
    cmd=["python3",str(ROOT/"tests/tcg/aarch64/system/skew-check.py"),str(qemu),
         "--quick","--output",str(out/"quick")]
    start=time.monotonic()
    with (out/"quick-runner.log").open("w") as log:
        proc=subprocess.Popen(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:rc=proc.wait(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL);proc.wait();rc=124
    (out/"quick-summary.json").write_text(json.dumps(
        {"passed":rc==0,"returncode":rc,"host_seconds":time.monotonic()-start,"command":cmd},indent=2))
    print("QUICK",rc,flush=True)
if __name__=="__main__":main()

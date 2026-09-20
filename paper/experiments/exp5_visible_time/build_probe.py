#!/usr/bin/env python3
"""Build an experiment binary from copied source + existing objects; no tracked edits."""
import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def replace_once(text,old,new):
    if text.count(old)!=1:
        raise ValueError("source layout changed: "+old[:100])
    return text.replace(old,new)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build",type=Path,default=ROOT/"build")
    p.add_argument("--out",type=Path,default=ROOT/"build/exp5-probe")
    a=p.parse_args(); build=a.build.resolve(); out=a.out.resolve()
    out.mkdir(parents=True,exist_ok=True)
    skew=ROOT/"accel/tcg/skew.c"; helper=ROOT/"target/arm/helper.c"
    original={str(x):sha(x) for x in (skew,helper,build/"qemu-system-aarch64")}
    s=skew.read_text()
    s=replace_once(s,"static Error *migration_blocker;","static Error *migration_blocker;\n"+(HERE/"probe.inc.c").read_text())
    s=replace_once(s,"    coordinator = timer_new_ns(QEMU_CLOCK_REALTIME, skew_update, NULL);",
                   "    exp5_init();\n    coordinator = timer_new_ns(QEMU_CLOCK_REALTIME, skew_update, NULL);")
    s=replace_once(s,"    return next;\n}\n\nint64_t skew_get_clock",'''    if (exp5_in_counter && exp5_path) {
        exp5_pending = (Exp5Sample) {
            .host = cpu_get_clock(), .model = model, .visible = next,
            .global = global_icount, .window = window_ns, .slope = slope,
            .cpu = current_cpu ? current_cpu->cpu_index : 0,
            .event = next == upper ? "bound_hit" : "read",
        };
    }
    return next;
}

int64_t skew_get_clock''')
    s=replace_once(s,"    trace_skew_clock(host_ns, global_icount, now, active);",
                   '    exp5_event("update");\n    trace_skew_clock(host_ns, global_icount, now, active);')
    s, count = re.subn(r'(        trace_skew_cpu\(qemu_clock_get_ns\(QEMU_CLOCK_REALTIME\),\n\s+cpu->cpu_index, true)',
                      '        exp5_event("resume");\n'+r'\1',s)
    if count != 1: raise ValueError("unexpected resume trace shape")
    h=helper.read_text()
    h=replace_once(h,"static uint64_t gt_cnt_read(CPUARMState *env, const ARMCPRegInfo *ri)",
                  "void skew_exp5_begin(void);\nuint64_t skew_exp5_end(uint64_t ticks);\n\nstatic uint64_t gt_cnt_read(CPUARMState *env, const ARMCPRegInfo *ri)")
    old="    return gt_get_countervalue(env) - offset;"
    # Exactly the physical and virtual counter callbacks, not unrelated timer paths.
    if h.count(old)!=2: raise ValueError("unexpected counter helper shape")
    h=h.replace(old,"    skew_exp5_begin();\n    return skew_exp5_end(gt_get_countervalue(env) - offset);")
    sources=[(out/"skew.c",s,"libsystem.a.p/accel_tcg_skew.c.o"),
             (out/"helper.c",h,"libsystem_arm.a.p/target_arm_helper.c.o")]
    commands=[]; replacements={}
    for path,source,obj in sources:
        path.write_text(source)
        argv=shlex.split(subprocess.check_output(["ninja","-t","commands",obj],cwd=build,text=True).splitlines()[-1])
        output=out/(path.stem+".o")
        for flag in ("-o","-MQ","-MF"):
            index=argv.index(flag)+1
            argv[index]=str(output)+(".d" if flag=="-MF" else "")
        argv[argv.index("-c")+1]=str(path)
        # Quoted includes in copied ARM source must resolve relative to original source.
        argv += ["-iquote",str(ROOT/("target/arm" if path.stem=="helper" else "accel/tcg"))]
        commands.append(argv); subprocess.run(argv,cwd=build,check=True)
        replacements[obj]=str(output)
    ninja=(build/"build.ninja").read_text()
    match=re.search(r"^build qemu-system-aarch64: c_LINKER_RSP (.*)\n LINK_ARGS = (.*)$",ninja,re.M)
    if not match: raise ValueError("unsupported linker rule")
    objects=shlex.split(match[1].split(" |")[0])
    objects=[replacements.get(x,x) for x in objects]
    binary=out/"qemu-system-aarch64"
    argv=["gcc-10","-m64","-o",str(binary)]+objects+shlex.split(match[2])
    # Response-file linking avoids ARG_MAX and records the exact invocation.
    rsp=out/"link.rsp";rsp.write_text(" ".join(shlex.quote(x) for x in argv[2:]))
    commands.append(argv)
    subprocess.run(argv[:2]+["@"+str(rsp)],cwd=build,check=True)
    assert original=={x:sha(x) for x in original}, "baseline changed"
    manifest={"baseline_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
              "original_sha256":original,"generated_sha256":{str(x):sha(x) for x in (out/"skew.c",out/"helper.c",binary)},
              "commands":commands,"probe_source_sha256":sha(HERE/"probe.inc.c")}
    (out/"build-manifest.json").write_text(json.dumps(manifest,indent=2))
    print(binary)
if __name__=="__main__":main()

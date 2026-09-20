#!/usr/bin/env python3
"""Validate one experiment CSV and produce SVG/PDF plus machine-readable metrics."""
import argparse
import csv
import json
import math
import hashlib
import platform
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev, pvariance
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
DIRS = {1: "exp1_hw_calibration", 2: "exp2_host_speed", 3: "exp3_parallel",
        4: "exp4_skew_window", 5: "exp5_visible_time"}
COLORS = {"mttcg": "#555555", "slowtime": "#009E73",
          "icount": "#D55E00", "skew": "#0072B2"}

def style():
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.alpha": .2, "pdf.fonttype": 42,
                         "svg.fonttype": "none", "figure.figsize": (6.5, 3.7)})

def read_csv(exp, path, measured=False):
    schema = json.loads((ROOT / "experiments" / DIRS[exp] / "schema.json").read_text())
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        missing = set(schema["fields"]) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("missing columns: " + ", ".join(sorted(missing)))
        for line, raw in enumerate(reader, 2):
            row = {}
            for field, typ in schema["fields"].items():
                value = raw.get(field)
                if value is None:
                    raise ValueError(f"row {line}: missing {field}")
                value = value.strip()
                if typ.startswith("optional_") and not value:
                    row[field] = None
                    continue
                typ = typ.removeprefix("optional_") if hasattr(typ, "removeprefix") else typ.replace("optional_", "")
                if typ == "str" or typ.startswith("enum:"):
                    if not value or (typ.startswith("enum:") and value not in typ[5:].split("|")):
                        raise ValueError(f"row {line}: invalid {field}")
                    row[field] = value
                    continue
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"row {line}: nonfinite {field}")
                if typ.startswith("positive") and number <= 0:
                    raise ValueError(f"row {line}: {field} must be positive")
                if typ == "nonnegative" and number < 0:
                    raise ValueError(f"row {line}: negative {field}")
                if typ == "bool" and value not in ("0", "1"):
                    raise ValueError(f"row {line}: {field} must be 0 or 1")
                if typ == "positive_int" and not number.is_integer():
                    raise ValueError(f"row {line}: noninteger {field}")
                # Keep integer nanosecond/count fields exact even above 2**53.
                row[field] = int(value) if value.lstrip("+-").isdigit() else number
            rows.append(row)
    if not rows:
        raise ValueError("empty CSV")
    evidence = {row["evidence"] for row in rows}
    if len(evidence) != 1 or (measured and evidence != {"measured"}):
        raise ValueError("mixed evidence or synthetic data requested as measured")
    if not measured and evidence != {"synthetic"}:
        raise ValueError("measured data requires --measured")
    if exp in (2, 3, 4) and len({r["workload"] for r in rows}) != 1:
        raise ValueError("use separate files for different workloads")
    if exp != 5 and len({r["run_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate run_id")
    if exp == 1 and len({r["selected_ips"] for r in rows}) != 1:
        raise ValueError("freeze one selected_ips for calibration and validation")
    if exp == 2:
        for r in rows:
            if r["capacity_pct"] > 100:
                raise ValueError("capacity_pct exceeds 100")
            miss = r["guest_ns"] > r["deadline_ns"] if r["completed"] else True
            if bool(r["deadline_miss"]) != miss:
                raise ValueError("deadline outcome inconsistent with elapsed/completion")
    if exp == 3 and len({r["work_mode"] for r in rows}) != 1:
        raise ValueError("do not mix fixed-total and fixed-per-CPU work")
    if exp == 4 and len({r["sim_ips"] for r in rows}) != 1:
        raise ValueError("window sweep must keep IPS fixed")
    if exp == 5:
        if len({r["run_id"] for r in rows}) != 1:
            raise ValueError("timeline must contain exactly one run")
        if any(b["seq"] <= a["seq"] or b["host_ns"] < a["host_ns"]
               for a, b in zip(rows, rows[1:])):
            raise ValueError("timeline requires increasing seq and nondecreasing host_ns")
        if any(r["slope_q32"] > 2**32 for r in rows):
            raise ValueError("slope exceeds Q32 one")
    return rows

def save(fig, out, name, synthetic):
    if synthetic:
        fig.text(.5, .99, "SYNTHETIC EXAMPLE — NOT MEASURED",
                 ha="center", va="top", color="#a02020", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, .94))
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("svg", "pdf"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight")
    plt.close(fig)

def describe(values):
    return {"n": len(values), "median": median(values), "mean": mean(values),
            "min": min(values), "max": max(values),
            "stddev_sample": stdev(values) if len(values) > 1 else None}

def grouped_plot(rows, xkey, value, ylabel, out, name, synthetic, group="method"):
    fig, ax = plt.subplots()
    for method in sorted({r[group] for r in rows}):
        buckets = defaultdict(list)
        for r in rows:
            if r[group] == method:
                buckets[r[xkey]].append(value(r))
        x = sorted(buckets)
        y = [median(buckets[t]) for t in x]
        ax.errorbar(x, y, yerr=[[m-min(buckets[t]) for t, m in zip(x, y)],
                                [max(buckets[t])-m for t, m in zip(x, y)]],
                    marker="o", capsize=3, label=str(method),
                    color=COLORS.get(method))
    ax.set(xlabel=xkey.replace("_", " "), ylabel=ylabel)
    ax.legend()
    save(fig, out, name, synthetic)

def plot(exp, path, out, measured=False):
    style()
    rows = read_csv(exp, path, measured)
    synthetic = rows[0]["evidence"] == "synthetic"
    out = Path(out)
    summary = {"experiment": exp, "evidence": rows[0]["evidence"],
               "input": str(path), "rows": len(rows),
               "input_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
               "python_version": platform.python_version(),
               "matplotlib_version": matplotlib.__version__,
               "error_bars": "min--max; center median; not confidence intervals"}
    if exp == 1:
        for r in rows:
            r["equivalent_ips"] = r["instructions"] * 1e9 / r["hardware_ns"]
            r["reconstructed_ns"] = r["instructions"] * 1e9 / r["selected_ips"]
            r["relative_error"] = r["reconstructed_ns"] / r["hardware_ns"] - 1
        def errors(group):
            v = [r["relative_error"] for r in group]
            return {"mean_absolute_relative_error": mean(map(abs, v)),
                    "median_absolute_relative_error": median(map(abs, v)),
                    "max_absolute_relative_error": max(map(abs, v)),
                    "signed_error_stddev": stdev(v) if len(v)>1 else None}
        summary["all_errors"] = errors(rows)
        summary["validation_errors"] = errors([r for r in rows if r["split"]=="validation"]) if any(r["split"]=="validation" for r in rows) else None
        summary["per_workload"] = {}
        names = sorted({r["workload"] for r in rows})
        fig, ax = plt.subplots()
        for i, name in enumerate(names):
            group = [r for r in rows if r["workload"]==name]
            v = [r["equivalent_ips"]/1e6 for r in group]
            ax.errorbar(i, median(v), yerr=[[median(v)-min(v)], [max(v)-median(v)]],
                        fmt="o", color="#0072B2", capsize=4)
            summary["per_workload"][name] = {
                "errors": errors(group), "equivalent_ips": describe([r["equivalent_ips"] for r in group]),
                "hardware_ns": describe([r["hardware_ns"] for r in group]),
                "instructions": describe([r["instructions"] for r in group])}
        ax.axhline(rows[0]["selected_ips"]/1e6, ls="--", color="gray", label="Selected IPS")
        ax.set(xticks=range(len(names)), xticklabels=names, ylabel="Equivalent IPS (million/s)")
        ax.tick_params(axis="x", rotation=15)
        ax.legend()
        save(fig, out, "E1-equivalent-ips", synthetic)
        fig, ax = plt.subplots()
        for split, marker in (("calibration", "o"), ("validation", "s")):
            group = [r for r in rows if r["split"]==split]
            ax.scatter([r["hardware_ns"]/1e6 for r in group],
                       [r["reconstructed_ns"]/1e6 for r in group], label=split, marker=marker)
        top = max(max(r["hardware_ns"], r["reconstructed_ns"]) for r in rows)/1e6*1.05
        ax.plot([0, top], [0, top], "--", color="gray", label="y = x")
        ax.set(xlabel="Hardware ROI time (ms)", ylabel="Reconstructed ROI time (ms)")
        ax.legend()
        save(fig, out, "E2-hardware-reconstructed", synthetic)
    elif exp == 2:
        complete = [r for r in rows if r["completed"]]
        if complete:
            grouped_plot(complete, "capacity_pct", lambda r:r["guest_ns"]/1e6,
                         "Guest elapsed (ms)", out, "E3-host-capacity", synthetic)
        fig, ax = plt.subplots()
        summary["groups"] = []
        for method in sorted({r["method"] for r in rows}):
            xs = sorted({r["capacity_pct"] for r in rows if r["method"]==method})
            rates = []
            for x in xs:
                g = [r for r in rows if r["method"]==method and r["capacity_pct"]==x]
                rates.append(mean(r["deadline_miss"] for r in g))
                ok = [r for r in g if r["completed"]]
                summary["groups"].append({"method": method, "capacity_pct": x,
                    "attempts":len(g), "completed":len(ok), "miss_rate":rates[-1],
                    "host_ns":describe([r["host_ns"] for r in ok]) if ok else None,
                    "guest_ns":describe([r["guest_ns"] for r in ok]) if ok else None,
                    "model_ns":describe([r["model_ns"] for r in ok if r["model_ns"] is not None]) if any(r["model_ns"] is not None for r in ok) else None})
            ax.plot(xs, rates, "o-", label=method, color=COLORS[method])
        ax.set(xlabel="Host CPU quota (%)", ylabel="Deadline miss fraction", ylim=(-.05, 1.05))
        ax.legend()
        save(fig, out, "E4-deadline-misses", synthetic)
        model = [r for r in complete if r["model_ns"] is not None]
        if model:
            grouped_plot(model,"capacity_pct",lambda r:r["model_ns"]/1e6,
                         "Model elapsed (ms)",out,"E3b-model-capacity",synthetic)
    elif exp == 3:
        good = [r for r in rows if r["completed"]]
        if not good:
            raise ValueError("no completed runs to plot; retain failures")
        grouped_plot(good, "vcpus", lambda r:r["instructions"]/(r["host_ns"]/1e9)/1e6,
                     "Throughput (million guest instructions/s)", out, "E5-throughput", synthetic)
        fig, ax = plt.subplots()
        summary["groups"] = []
        for method in sorted({r["method"] for r in rows}):
            base = [r for r in good if r["method"]==method and r["vcpus"]==1]
            if not base:
                raise ValueError(f"missing successful 1-vCPU baseline for {method}")
            xs = sorted({r["vcpus"] for r in rows if r["method"]==method})
            ys, valid_x = [], []
            for x in xs:
                allg = [r for r in rows if r["method"]==method and r["vcpus"]==x]
                g = [r for r in allg if r["completed"]]
                if g:
                    ratio = median(r["host_ns"] for r in base)/median(r["host_ns"] for r in g) if rows[0]["work_mode"]=="fixed_total" else median(r["instructions"]/r["host_ns"] for r in g)/median(r["instructions"]/r["host_ns"] for r in base)
                    ys.append(ratio); valid_x.append(x)
                summary["groups"].append({"method":method, "vcpus":x,
                    "attempts":len(allg), "failures":len(allg)-len(g),
                    "host_ns":describe([r["host_ns"] for r in g]) if g else None,
                    "throughput_ips":describe([r["instructions"]*1e9/r["host_ns"] for r in g]) if g else None})
            ax.plot(valid_x, ys, "o-", label=method, color=COLORS[method])
        label = "Strong scaling speedup" if rows[0]["work_mode"]=="fixed_total" else "Throughput scaling"
        ax.set(xlabel="vCPUs", ylabel=label)
        ax.legend()
        save(fig, out, "E6-parallel-scaling", synthetic)
    elif exp == 4:
        summary["groups"] = []
        for w in sorted({r["window_insns"] for r in rows}):
            g = [r for r in rows if r["window_insns"]==w]
            summary["groups"].append({"window_insns":w,
                "throughput_ips":describe([r["instructions"]*1e9/r["host_ns"] for r in g]),
                "max_observed_spread_ns":max(r["max_active_spread_insns"]*1e9/r["sim_ips"] for r in g),
                "lead_bound_violations":sum(r["max_lead_insns"]>w for r in g),
                "spread_bound_violations":sum(r["max_active_spread_insns"]>w for r in g),
                "wait_rate":describe([r["wait_count"]*1e9/r["host_ns"] for r in g]),
                "budget_exit_rate": [r["budget_exit_count"]*1e9/r["host_ns"] for r in g if r["budget_exit_count"] is not None]})
        for name, ylabel, func in [
            ("E7-window-throughput","Throughput (million guest instructions/s)",lambda r:r["instructions"]*1e3/r["host_ns"]),
            ("E9-window-waits","Wait entries / host second",lambda r:r["wait_count"]*1e9/r["host_ns"])]:
            fig, ax = plt.subplots()
            xs = sorted({r["window_insns"] for r in rows})
            vs = [[func(r) for r in rows if r["window_insns"]==x] for x in xs]
            ys = [median(v) for v in vs]
            ax.errorbar(xs, ys, yerr=[[m-min(v) for m,v in zip(ys,vs)],[max(v)-m for m,v in zip(ys,vs)]],fmt="o-",capsize=3)
            ax.set(xscale="log", xlabel="Window (instructions)", ylabel=ylabel)
            save(fig,out,name,synthetic)
        fig, ax = plt.subplots()
        xs = sorted({r["window_insns"] for r in rows})
        for key, label in (("max_active_spread_insns","Sampled active spread"),("max_lead_insns","Sampled lead")):
            ax.plot(xs,[max(r[key] for r in rows if r["window_insns"]==x) for x in xs],"o-",label=label)
        ax.plot(xs,xs,"--",color="gray",label="Configured W")
        scale = "log" if all(r["max_active_spread_insns"]>0 and r["max_lead_insns"]>0 for r in rows) else "symlog"
        ax.set(xscale="log",yscale=scale,xlabel="Window (instructions)",ylabel="Observed skew (instructions)")
        if scale == "symlog":
            ax.set_ylim(bottom=0)
        ax.legend()
        save(fig,out,"E8-window-skew",synthetic)
    else:
        reads = [r for r in rows if r["event"] in ("read","bound_hit")]
        if len(reads)<2:
            raise ValueError("timeline needs at least two ordered reads")
        visible_delta = [b["visible_ns"]-a["visible_ns"] for a,b in zip(reads,reads[1:])]
        model_delta = [b["model_ns"]-a["model_ns"] for a,b in zip(reads,reads[1:])]
        summary.update({"read_count":len(reads),
            "monotonic_violations":sum(d<0 for d in visible_delta),
            "model_backward_steps":sum(b["model_ns"]<a["model_ns"] for a,b in zip(rows,rows[1:])),
            "bound_violations":sum(abs(r["visible_ns"]-r["model_ns"])>r["window_ns"] for r in rows),
            "max_abs_bias_ns":max(abs(r["visible_ns"]-r["model_ns"]) for r in rows),
            "repeated_counter_pairs":sum(b["counter_ticks"]==a["counter_ticks"] for a,b in zip(reads,reads[1:])),
            "visible_delta_variance_ns2":pvariance(visible_delta),
            "model_delta_variance_ns2":pvariance(model_delta),
            "max_observation_gap_ns":max(b["host_ns"]-a["host_ns"] for a,b in zip(rows,rows[1:]))})
        fig, ax = plt.subplots(figsize=(8,4))
        h0, v0 = rows[0]["host_ns"], rows[0]["model_ns"]
        x = [(r["host_ns"]-h0)/1e6 for r in rows]
        ax.step(x,[(r["model_ns"]-v0)/1e6 for r in rows],where="post",color="#D55E00",label="Model")
        ax.plot(x,[(r["visible_ns"]-v0)/1e6 for r in rows],color="#0072B2",label="Visible (raw segments)")
        for sign,label in ((1,"Upper bound"),(-1,"Lower bound")):
            ax.step(x,[(max(0,r["model_ns"]+sign*r["window_ns"])-v0)/1e6 for r in rows],
                    where="post",ls="--",alpha=.7,label=label)
        ax.scatter([(r["host_ns"]-h0)/1e6 for r in reads],[(r["visible_ns"]-v0)/1e6 for r in reads],
                   color="#0072B2",s=12,label="Software read")
        first = True
        for r in rows:
            if r["event"]=="update":
                ax.axvline((r["host_ns"]-h0)/1e6,color="gray",alpha=.3,
                           label="Coordinator update" if first else None)
                first = False
        ax.set(xlabel="Host elapsed (ms)",ylabel="Virtual time relative to initial model (ms)")
        ax.legend(fontsize=8,ncol=2)
        save(fig,out,"E10-visible-timeline",synthetic)
    out.mkdir(parents=True,exist_ok=True)
    if exp == 1:
        with (out/"calibration-table.csv").open("w",newline="",encoding="utf-8") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
    (out/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    return summary

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exp",type=int,choices=range(1,6))
    p.add_argument("--csv",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--measured",action="store_true")
    p.add_argument("--all-examples",action="store_true")
    a = p.parse_args()
    if a.all_examples:
        if a.measured:
            p.error("examples cannot be measured")
        for exp,directory in DIRS.items():
            plot(exp,ROOT/"experiments"/directory/"example.csv",
                 ROOT/"figures/examples"/directory)
    elif a.exp and a.csv and a.out:
        plot(a.exp,a.csv,a.out,a.measured)
    else:
        p.error("provide --exp --csv --out, or --all-examples")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Meaningful fixture checks: derived metrics, bounds, and evidence separation."""
import csv
import tempfile
from pathlib import Path
from plot import ROOT, DIRS, read_csv, plot

def reject(exp,path,measured=False):
    try:
        read_csv(exp,path,measured)
    except ValueError:
        return
    raise AssertionError("invalid input was accepted")

def main():
    with tempfile.TemporaryDirectory() as temp:
        temp=Path(temp)
        for exp,d in DIRS.items():
            source=ROOT/"experiments"/d/"example.csv"
            rows=read_csv(exp,source)
            assert rows and all(r["evidence"]=="synthetic" for r in rows)
            reject(exp,source,True)
            summary=plot(exp,source,temp/str(exp))
            assert list((temp/str(exp)).glob("*.svg"))
            assert list((temp/str(exp)).glob("*.pdf"))
            if exp==1:
                assert summary["validation_errors"] is not None
                assert abs(summary["all_errors"]["mean_absolute_relative_error"]-.062)<1e-9
            if exp==5:
                assert summary["monotonic_violations"]==0
                assert summary["bound_violations"]==0
                assert summary["repeated_counter_pairs"]>0
        source=ROOT/"experiments"/DIRS[5]/"example.csv"
        with source.open(newline="") as f:
            reader=csv.DictReader(f); fields=reader.fieldnames; rows=list(reader)
        # Do not hide violations in incoming data.
        rows[2]["visible_ns"]="-1"
        bad=temp/"negative.csv"
        with bad.open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        reject(5,bad)
        rows[2]["visible_ns"]="999999999"
        with bad.open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        result=plot(5,bad,temp/"violations")
        assert result["bound_violations"]>0
        assert result["monotonic_violations"]>0
        rows[0]["evidence"]="measured"
        with bad.open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        reject(5,bad)
    print("PASS: five CSV schemas, SVG/PDF generation, error metrics, bounds, evidence guards")

if __name__=="__main__":main()

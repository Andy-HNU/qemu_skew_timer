#!/usr/bin/env python3
"""Verify archived evidence, including expected failures; do not rewrite results."""
import hashlib
import json
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
dest=HERE/"results/20260920"
hashes=json.loads((dest/"SHA256SUMS.json").read_text())
for name,sha in hashes.items():
    assert hashlib.sha256((dest/name).read_bytes()).hexdigest()==sha,name
# This is a historical archive: the current checkout may contain later fixes.
# Build-time baseline hashes remain in the checksummed build manifest.
audit=json.loads((dest/"audit.json").read_text())
probe=[r for r in audit if r["kind"]=="probe"]
assert len(audit)==154
assert sum(r["metrics"]["reads"] for r in probe)==392000
for r in probe:
    m=r["metrics"]
    assert r["recorder"][1:]==[0,0]
    assert all(m[k]==0 for k in ("visible_backward","counter_backward","model_backward",
                                "publication_backward","host_order_violations",
                                "bounds_violations","slope_violations","counter_conversion_violations"))
    if r["case"] in ("sustained-20g","saturation"):
        assert m["model_formula_violations"]>0, "known failure missing"
    else:
        assert m["model_formula_violations"]==0
good_sat=[r for r in probe if r["case"]=="saturation-2g"]
assert sum(r["metrics"]["bound_hits"] for r in good_sat)==27699
jitter=json.loads((dest/"reuse/jitter-summary.json").read_text())
assert sum(r["repeat"]>=0 and r["passed"] for r in jitter)==7
assert json.loads((dest/"reuse/quick-summary.json").read_text())["passed"]
print("PASS: historical archive hashes, 154 attempts, 392000 joint reads,")
print("expected 20G failures retained, 2G plateaus, seven jitter runs and quick acceptance")

fixed=HERE/"results/20260920-fixed"
for name,sha in json.loads((fixed/"SHA256SUMS.json").read_text()).items():
    assert hashlib.sha256((fixed/name).read_bytes()).hexdigest()==sha,name
rows=json.loads((fixed/"matrix/runs.json").read_text())
assert all(r["passed"] for r in rows)
formal=[r for r in rows if r["repeat"]>=0]
assert len(formal)==98
assert sum(r["metrics"]["reads"] for r in formal if r["kind"]=="probe")==336000
for r in formal:
    if r["kind"]=="probe":
        assert r["metrics"]["model_formula_violations"]==0
assert all(r["passed"] for r in json.loads((fixed/"reuse/jitter-summary.json").read_text()))
assert json.loads((fixed/"reuse/quick-summary.json").read_text())["passed"]
print("PASS: fixed archive hashes, 98 executions, 336000 reads, jitter and quick acceptance")

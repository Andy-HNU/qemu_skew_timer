#!/usr/bin/env python3
"""Archive post-fix evidence separately; retain the original failed experiment."""
import difflib
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from run_matrix import ROOT, CASES, stats

HERE = Path(__file__).resolve().parent
DEST = HERE / 'results/20260920-fixed'

def main():
    assert not (DEST/'SHA256SUMS.json').exists(), 'Do not overwrite a sealed archive'
    DEST.mkdir(parents=True, exist_ok=True)
    batches = {'matrix': 'exp5-fixed-20260920',
               'boundary': 'exp5-fixed-boundary-20260920',
               'reuse': 'exp5-fixed-reuse-20260920'}
    for label, name in batches.items():
        source = ROOT / 'build' / name
        for p in source.rglob('*'):
            if not p.is_file() or p.suffix not in ('.json', '.log', '.gz', '.trace'):
                continue
            target = DEST / label / p.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix == '.trace':
                with p.open('rb') as f, gzip.open(str(target)+'.gz', 'wb') as z:
                    shutil.copyfileobj(f, z)
            else:
                shutil.copy2(p, target)
    rows = json.loads((DEST/'matrix/runs.json').read_text())
    assert all(r['passed'] for r in rows)
    for r in rows:
        if r['kind'] == 'probe':
            trace = DEST/'matrix'/f"{r['case']}-probe-{r['repeat']}.csv.gz"
            recomputed = json.loads(json.dumps(stats(trace, CASES[r['case']]['ips'])))
            assert recomputed == r['metrics'], trace
    jitter = json.loads((DEST/'reuse/jitter-summary.json').read_text())
    assert all(r['passed'] for r in jitter)
    assert json.loads((DEST/'reuse/quick-summary.json').read_text())['passed']
    arithmetic = subprocess.check_output(['python3', str(HERE/'check_wide_arithmetic.py')], text=True)
    (DEST/'arithmetic.json').write_text(arithmetic)
    # This patch identifies the tested working tree relative to the recorded base commit.
    patch = subprocess.check_output(['git', 'diff', '--', 'accel/tcg/skew.c'], cwd=ROOT)
    assert patch, 'Archive before committing the production fix'
    (DEST/'production-fix.patch').write_bytes(patch)
    patches = []
    for original, generated in ((ROOT/'accel/tcg/skew.c', ROOT/'build/exp5-probe/skew.c'),
                                (ROOT/'target/arm/helper.c', ROOT/'build/exp5-probe/helper.c')):
        patches.extend(difflib.unified_diff(original.read_text().splitlines(True),
                       generated.read_text().splitlines(True), fromfile=str(original.relative_to(ROOT)),
                       tofile='experiment-copy/'+generated.name))
    (DEST/'instrumentation.patch').write_text(''.join(patches))
    formal = [r for r in rows if r['repeat'] >= 0]
    summary = dict(formal_runs=len(formal), passed=sum(r['passed'] for r in formal),
                   probe_reads=sum(r['metrics']['reads'] for r in formal if r['kind']=='probe'),
                   linux_formal_passed=sum(r['repeat']>=0 and r['passed'] for r in jitter))
    (DEST/'verification.json').write_text(json.dumps(summary, indent=2))
    hashes = {str(p.relative_to(DEST)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(DEST.rglob('*')) if p.is_file()}
    (DEST/'SHA256SUMS.json').write_text(json.dumps(hashes, indent=2))
    print(json.dumps(summary))

if __name__ == '__main__':
    main()

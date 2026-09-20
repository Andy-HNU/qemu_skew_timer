#!/usr/bin/env python3
"""Compile the actual skew helper and compare it with Python integer arithmetic."""
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
source = (ROOT / "accel/tcg/skew.c").read_text()
helper = re.search(r"static uint64_t skew_muldiv\(.*?\n\}", source, re.S).group()
cases = [(ns, ips, 10**9) for ns in (1, 10**6, 10**9, 2**32, 10**10)
         for ips in (200000000, 2000000000, 2**32-1, 2**32, 20000000000, 10**12)]
cases += [(count, 10**9, ips) for count in (1, 2**32, 2**64-1)
          for ips in (200000000, 2**32, 20000000000, 10**12)]
cases += [(2**64-1, 10**12, 10**9), (2**64-1, 2**64-1, 1)]
code = '#include <stdint.h>\n#include <stdio.h>\n#define MIN(a,b) ((a)<(b)?(a):(b))\n'
code += helper + '\nint main(void) {\n'
for a, b, c in cases:
    code += f'printf("%llu\\n", (unsigned long long)skew_muldiv({a}ULL,{b}ULL,{c}ULL));\n'
code += 'return 0; }\n'
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(code)
    subprocess.run(['gcc-10', '-Wall', '-Werror', '-fsanitize=undefined',
                    str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    actual = list(map(int, subprocess.check_output([str(path / 'test')], text=True).split()))
expected = [min(a*b//c, 2**64-1) for a, b, c in cases]
assert actual == expected
print(json.dumps({'passed': True, 'cases': len(cases),
                  'covers': ['wide IPS', 'interval above 2^32 ns', 'saturation']}))

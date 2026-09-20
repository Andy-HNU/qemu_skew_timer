# First-stage artifact validation — 2026-09-20

补充：以下是第一阶段生成验证记录。随后完成的 exp5 WSL实测见
[REPORT_zh.md](experiments/exp5_visible_time/REPORT_zh.md)：154次正式裸机运行、
392000次插桩软件读取、7次Linux jitter复跑及quick验收。论文已加入实测段落和
高IPS公式失败分析，实验1–4仍待测。正式QEMU源码与二进制未改。

Scope: paper/ only. QEMU implementation is unchanged from 4e894601b8.
No new hardware or guest performance measurement was performed for this paper draft.

## Checks completed

- Generated five explicitly synthetic CSVs with row-level evidence/source markers.
- Parsed all five schema definitions and generated 11 experiment figures as SVG and PDF.
- Generated three design schematics as SVG and PDF from architecture.csv.
- Ran scripts/test_pipeline.py: all checks passed, including calculated calibration error,
  rejection of synthetic input with --measured, mixed-evidence rejection, negative-input
  rejection, and retention/counting of deliberate backward-time and bound violations.
- Rebuilt all plots after layout correction. Rendered the figures and inspected a contact
  sheet; labels, watermarks, legends and axis ranges were checked.
- Compiled paper.tex with bibliography using Tectonic 0.17.0. Six-page PDF generated.
  Checked for unresolved references and LaTeX overflow warnings; inspected page renders.
- Confirmed that the compiled draft includes only the three design figures, never the
  synthetic evaluation figures. Evaluation remains a plan with explicit TODOs.

## Environment

The WSL Ubuntu-20.04 Python/TeX package dependencies could not be resolved without unrelated
system changes. Validation therefore used Python 3.12 from the local bundled runtime,
isolated Python packages outside the repository (Matplotlib 3.11.2, NumPy 2.5.3), and a
portable Tectonic 0.17.0 compiler. Each plot summary records Python/Matplotlib versions
and input SHA-256. Tectonic printed a fontconfig configuration message but completed using
its bundled fonts; the PDF was rendered and checked. Standard Linux make targets are
provided, but this environment did not validate the separate pdflatex toolchain.

## Remaining research work

Obtain paired hardware measurements; implement a documented slowtime comparator if needed;
collect controlled host-quota runs; convert compatible benchmark traces with provenance;
add joint per-read interpolation instrumentation for exp5. These are intentionally distinct
from first-stage CSV/plot validation. The example timeline is a schematic fixture, not a
replay or numerical validation of the production C interpolator.
# 后续 IPS 修复回归

四处换算改用完整 64 位参数；WSL 重新编译通过。新增 44 个实际函数算术检查、
8 个 QMP 边界配置、98 次正式裸机运行全部通过；7 次 Linux jitter init/use
与原有 quick 验收通过。详细参数、证据和限制见
[修复后报告](experiments/exp5_visible_time/POST_FIX_zh.md)。原始失败数据不覆盖。

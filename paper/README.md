# SkewKeeper paper workspace

本目录是第一阶段论文与实验产物，基线为 QEMU **10.2.0**，
实现 commit `4e894601b887007c180d5d28ec12afbba05c0965`。
首轮实验保留该基线；随后修复 IPS 换算截断，并单独记录修复后的回归结果。

第5组已完成首轮WSL实测：[实验计划](experiments/exp5_visible_time/PLAN_zh.md) ·
[实验报告](experiments/exp5_visible_time/REPORT_zh.md)。154次正式裸机运行、7次Linux
jitter复跑；发现超过32位的IPS参数截断缺陷。原始数据和失败均保留。
修复和复测见[修复后报告](experiments/exp5_visible_time/POST_FIX_zh.md)。

## 内容

- [outline.md](outline.md)：完整论文提纲与 RQ 对应关系。
- [paper.tex](paper.tex)：英文初稿；实验1–4仍为计划，第5组已有实测与失败分析。
- [implementation-audit.md](implementation-audit.md)：任务说明与现有代码的差异。
- [references.bib](references.bib)、[sources.md](sources.md)：已核对的真实参考来源。
- [VALIDATION.md](VALIDATION.md)：本轮生成、数据校验和编译检查记录。
- experiments/exp1_hw_calibration 到 exp5_visible_time：五组实验协议、schema.json 和 example.csv。
- scripts/plot.py：统一 CSV 校验、汇总、绘图入口。
- scripts/architecture.py：三张设计示意图，读取 architecture.csv。
- figures/design：设计示意图；figures/examples：有水印的示例结果。
- experiments/exp5_visible_time/results/20260920：第5组真实轨迹、统计与图表。
- figures/results：其他真实数据图的预留输出位置。

## 快速使用（Linux / WSL）

```sh
cd paper
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/architecture.py --csv figures/architecture.csv --out figures/design
python scripts/make_examples.py
python scripts/plot.py --all-examples
python scripts/test_pipeline.py
make paper
```

构建论文需要 pdflatex 和 bibtex（Debian 包 texlive-latex-base）。
也可使用 Tectonic 0.17.0：先生成设计图，再执行
`mkdir -p build && tectonic -X compile paper.tex --outdir build --keep-logs`。
`make paper` 输出 build/paper.pdf，正文包含设计图和第5组实测图，不包含synthetic示例图。
`make examples` 生成全部示例图；图中英文、输出 SVG/PDF，无 JPG。

## 替换真实数据

1. 阅读对应实验 SPEC.md，用同一 workload、ROI 和配置采集原始日志。
2. 根据 schema.json 创建 CSV，将每行 evidence 设为 measured，保留真实 source_log。
3. 复制 manifest.example.json，填入机器、内核、二进制 hash、完整命令、
   仪器和配对 workload 标识；硬件缺失时 exp1 继续标记 pending。
4. 运行（exp 可为 1 到 5）：
```sh
python scripts/plot.py --exp 1 --csv /absolute/path/measured.csv \
  --out figures/results/exp1 --measured
```
5. 检查输出 summary.json、误差范围和原始日志后，再人工填写论文 Evaluation。
   程序不会自动把结果或结论写进 paper.tex。

所有 example.csv 行均为 synthetic，使用 --measured 会拒绝这些输入；
不允许混合 synthetic/measured。测量标签只是一项防误用检查，不证明数据来源。
示例只覆盖字段和绘图逻辑，不预测 SkewKeeper 的性能。
历史仓库报告未自动导入：版本、ROI、负载、时钟模式可能不同。

## 实验公共要求

主结果至少 1 次预热和 7 次正式重复，顺序随机化并保存 seed；启动失败、卡住、
超时属于结果，不能静默删除。exp2/exp3 的失败行保留 host timeout 上限，
不得作为成功完成耗时求中位数；deadline 分母包含全部尝试。
禁止把采样最大值称为连续执行期间的严格最大值。
同一图不得混入不同 binary/config/workload；各图默认每个文件一个 workload/config，
exp1 允许多个 workload，exp5 只允许一个 run_id。分组图误差线为 min--max，
中心为中位数；不是置信区间。

关键限制：插值有宿主时间输入；当前最小活动斜率为 1/256；
撞上硬边界仍会平台化。Linux jitter 可用性不等于熵质量证明。
exp5 的联合轨迹由实验专用插桩副本采集；插桩过程和扰动在实验报告中单独说明。

# Skew clock 文档

## 性能报告

[WSL2 host time 对比报告（2026-09-14）](benchmarks/20260914-host/REPORT_zh.md)
比较普通 MTTCG、icount 与 skew，覆盖 1/2/4/6 vCPU、固定总量、固定每核量、
周期同步和工作量不均四类负载。每个配置预热一次、正式测量 7 次。

报告同时给出相对 icount 的收益和相对普通 MTTCG 的成本。
“普通 MTTCG”是同一个 QEMU 二进制关闭 skew 与 icount 的基线，
并非另行编译的未修改上游版本。请结合报告中的适用范围解读结果。

| 内容 | 入口 |
|---|---|
| 测量边界、参数与复现方法 | [基准说明](SKEW_ICOUNT_HOST_BENCH_zh.md) |
| 42 次预热及 294 次正式测量的逐次数据 | [runs.jsonl](benchmarks/20260914-host/runs.jsonl) |
| 中位数、范围与全部正式样本 | [summary.json](benchmarks/20260914-host/summary.json) |
| WSL/CPU 环境、构建选项与二进制 SHA-256 | [environment.txt](benchmarks/20260914-host/environment.txt) |
| 基准运行脚本 | [skew-perf.py](../../tests/tcg/aarch64/system/skew-perf.py) |
| 报告生成与数据校验脚本 | [skew-perf-report.py](../../tests/tcg/aarch64/system/skew-perf-report.py) |

该批报告只包含修正收尾通知后的正式数据，不混入早期探索样本。
后续测试请使用新的输出目录和报告批次目录，保留本次原始记录。

## 设计与功能验收

- [时钟模型设计](DESIGN_zh.rst)
- [公共执行预算与机制隔离](EXECUTION_BUDGET_zh.md)
- [功能验收说明](TEST_ACCEPTANCE_zh.md)
- [历史实现报告](IMPLEMENTATION_REPORT_zh.md)

功能验收结果与 host time 性能测量用途不同；历史实现报告中的数字不替代上述性能报告。

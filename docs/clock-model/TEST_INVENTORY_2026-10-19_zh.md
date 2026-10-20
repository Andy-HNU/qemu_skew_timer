<!--
SPDX-License-Identifier: GPL-2.0-or-later
-->
# Skew 测试清单（2026-10-20 更新）

统计范围是本仓库维护的 Skew 专用测试与实验。瓶颈研究阶段的本地提交
`3080189` 包含 18 个入口；随后指数模型重实现新增两个入口。当前共
**20 个独立入口：15 个功能/回归入口，5 个性能/诊断入口**。
不计 QEMU 上游通用测试；每个可独立执行的验收程序或
实验驱动计一个入口，同一入口的参数组合、循环次数和重复运行另行说明。

## 功能与回归：15 个入口

| 编号 | 入口 | 覆盖范围 | 内部数量或边界 |
|---|---|---|---|
| 1 | [test-exec-budget.c](../../tests/unit/test-exec-budget.c) | 公共执行预算接口 | 4 个注册单测：disabled、model-refill、interrupt-preserved、reset |
| 2 | [skew-visible-cas.py](../../tests/tcg/aarch64/system/skew-visible-cas.py) | 可见时间原子发布 | 提取实际函数；强制 CAS 交错与并发发布 |
| 3 | [skew-momentum-anchor.py](../../tests/tcg/aarch64/system/skew-momentum-anchor.py) | 独立动量采样锚点 | 可控时钟夹具，含旧行为回归检测 |
| 4 | [skew-visible-switch.py](../../tests/tcg/aarch64/system/skew-visible-switch.py) | 可见时钟模式切换 | 提取实际函数，控制 CPU/BQL 夹具；不能替代真实 Guest 切换 |
| 5 | [skew-perf-sweep-test.py](../../tests/tcg/aarch64/system/skew-perf-sweep-test.py) | 工作量计算和报告完整性 | 5 个 unittest 方法 |
| 6 | [skew_test_io.py](../../tests/tcg/aarch64/system/skew_test_io.py) | FIFO 传输自检 | 缓冲、双向 I/O、超时、进程退出和关闭 |
| 7 | [check_wide_arithmetic.py](../../paper/experiments/exp5_visible_time/check_wide_arithmetic.py) | 宽整数换算与饱和 | 44 组输入，实际 C helper 对照 Python 整数，启用 UBSan |
| 8 | [check_ips_boundary.py](../../paper/experiments/exp5_visible_time/check_ips_boundary.py) | QMP 配置与窗口换算 | 8 个 IPS 配置，含 2^32 两侧；使用真实 QEMU |
| 9 | [skew-check.py](../../tests/tcg/aarch64/system/skew-check.py) | 裸机综合验收 | quick 15 条顶层结果；完整模式 52 条，含压力重复运行 |
| 10 | [skew-bottleneck-check.py](../../tests/tcg/aarch64/system/skew-bottleneck-check.py) | 新性能选项的裸机定向验收 | 默认 5 条结果：CNTVOFF、慢 CPU0、慢 CPU1、运行中切换、暂停时切换 |
| 11 | [skew-linux-jitter.py](../../tests/tcg/aarch64/system/skew-linux-jitter.py) | Linux jitterentropy | 初始化及持续 AF_ALG 读取；当前入口默认 256 次读取 |
| 12 | [skew-linux-switch.py](../../tests/tcg/aarch64/system/skew-linux-switch.py) | Linux 模式切换 | 运行中/暂停时双向转换、计数器连续性与定时器 |
| 13 | [skew-linux-efficiency.py](../../tests/tcg/aarch64/system/skew-linux-efficiency.py) | Linux SMP 功能与总耗时 | 核数、固定计算校验、定时器、启动到退出，支持切换路径 |
| 14 | [skew-asymptotic.py](../../tests/tcg/aarch64/system/skew-asymptotic.py) | 实际指数预测函数 | 2684 组原曲线、2700 组自适应曲线数值对照；含 450 组 H 末余量检查及有效发布间隔估计 |
| 15 | [skew-jitter-study.py](../../tests/tcg/aarch64/system/skew-jitter-study.py) | Linux jitter 冷启动重复矩阵 | 线性/指数/自适应指数/MTTCG、IPS 与发布策略；每次记录通过、失败或超时 |

`test-exec-budget.c` 构建为 `build/tests/unit/test-exec-budget`。
Linux efficiency 虽记录性能，也承担功能验收，计入功能类一次。

`skew-check.py --quick` 的 15 条顶层结果为：精确计数 7 条、非法配置拒绝
1 条、控制/idle 2 条、Guest mode 1/2/3/4/12 共 5 条。非法配置这一个结果
内部检查 13 个错误配置（自适应选项新增一项），不能再加到入口总数。

完整模式在 quick 基础上增加：普通 MTTCG 对照 4 条、IPS 3 条、窗口/更新
间隔 4 条、慢核 3 条、额外 Guest mode 3 条、传统 icount 2 条、压力重复
18 条，合计 `15 + 4 + 3 + 4 + 3 + 3 + 2 + 18 = 52`。
52 是执行结果数，包含重复配置，不能称为 52 个独立场景。

## 性能与诊断：5 个入口

| 编号 | 入口 | 场景与比较维度 |
|---|---|---|
| 16 | [skew-perf.py](../../tests/tcg/aarch64/system/skew-perf.py) | 4 类负载：total、percpu、barrier、uneven；比较 MTTCG/Skew/icount |
| 17 | [skew-perf-sweep.py](../../tests/tcg/aarch64/system/skew-perf-sweep.py) | 默认 5 类：total、phased、memory、idle、mixed；可单独选 percpu/uneven，扫描核数 |
| 18 | [run_matrix.py](../../paper/experiments/exp5_visible_time/run_matrix.py) | Exp5 可见时钟 13 场景，baseline/probe 对照；插桩结果用于诊断 |
| 19 | [skew-publication-repro.py](../../tests/tcg/aarch64/system/skew-publication-repro.py) | 6 个固定配置：compute-mttcg、compute-skew、compute-delayed、compute-wide、counter-mttcg、counter-skew |
| 20 | [skew-bottleneck-study.py](../../tests/tcg/aarch64/system/skew-bottleneck-study.py) | compute/counter 两类负载 × 可选发布、续配和计数器方案；支持 11 个命名候选配置 |

`build_probe.py` 是实验构建工具，`skew-perf-report.py` 是报告工具，未另计。
Guest C/汇编、插件、链接脚本和共享 helper 是上述入口的依赖，也未重复计数。
本轮 102 次正式性能运行、14 次诊断是运行样本数，不是新增 116 个测试。

## 覆盖状态与使用限制

清单表示代码中存在这些入口，不表示当前提交全部执行通过。
此前瓶颈研究阶段的实际验证和失败保留记录见
[瓶颈研究结果](SKEW_BOTTLENECK_RESULTS_2026-10-19_zh.md#6-验证范围失败记录与落地状态)。
已执行的 quick、预算、CAS、动量锚点、停稳切换、CNTVOFF、慢核和真实
Guest 模式转换均按各自范围报告；该阶段缺少匹配的 Linux 内核/模块，
未执行 Linux jitter、Linux 切换及开机到关闭验收。

后续已取得与历史故障相同的 Linux 内核/模块，新增指数模型的实际结果见
[重实现与 jitter 验证](SKEW_ASYMPTOTIC_REIMPLEMENTATION_2026-10-19_zh.md)。
历史指数曲线版本的源码仍未恢复，新源码及本次结果不能代表旧提交；
相关线索和证据限制见
[指数模型恢复状态](SKEW_ASYMPTOTIC_RECOVERY_2026-10-19_zh.md)。
常规运行命令及判定标准见 [测试与验收](TEST_ACCEPTANCE_zh.md)。

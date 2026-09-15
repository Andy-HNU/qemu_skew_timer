# 一键扫描 MTTCG / skew / icount

在 Linux 或 WSL2 的仓库根目录运行：

```sh
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32
```

覆盖 `2, 4, 6, …, 32` 个 guest vCPU，默认运行四个独立场景和一个混合场景，
每个场景、每个数量都比较普通 MTTCG、skew、icount。
`--max-cpus` 是包含在测试中的上限；奇数上限也会补入，例如 25 会测到 `24, 25`。
省略它时，默认测到本进程可用逻辑 CPU 数的两倍，最多 512。
例如 6 个物理核、12 个逻辑 CPU 的机器，默认扫描 `2, 4, …, 24`。

这里的数量是 **guest vCPU 数**。MTTCG/skew 每个 vCPU 一个执行线程；
icount 由单个执行线程轮转这些 vCPU。QEMU 还存在主线程等辅助线程。
超过宿主可用逻辑 CPU 数后，MTTCG/skew 的 vCPU 执行线程需要竞争宿主 CPU。

脚本自动编译测试 guest 和计时插件、校准工作量、预热、运行三个模式，最后生成报告。
QEMU 本身需要预先编译一次，不能使用未实现 skew 参数的系统 QEMU。

## 新机器的一次性准备

以 Ubuntu 24.04 / WSL2 Ubuntu 24.04 为例，在包含 skew 实现的本仓库中：

```sh
sudo apt update
sudo apt install -y build-essential gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
    python3 python3-venv ninja-build pkg-config libglib2.0-dev libpixman-1-dev zlib1g-dev git

mkdir -p build
cd build
../configure --target-list=aarch64-softmmu --enable-plugins --enable-trace-backends=log
ninja -j"$(nproc)"
cd ..

python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32
```

构建步骤针对新的 build 目录；已有可用 QEMU 时直接运行最后一行。
若二进制放在其他目录，用 `--qemu /path/to/qemu-system-aarch64` 指定。
脚本使用 Python 3.8+ 的标准库，不需要绘图库；QEMU 自身的构建依赖以 configure 检查为准。
自定义安装 GLib 的环境可通过 `PKG_CONFIG_PATH` 指向其 pkgconfig 目录。

## 五个场景

| 名称 | 固定工作量的分配 | 主要覆盖 |
|---|---|---|
| `total` | 各核平分总计算量，中途不设 barrier。 | 均匀持续计算的基线。 |
| `phased` | 约半数核每阶段执行其他核 4 倍计算量，奇偶组每阶段交换角色，阶段末 barrier。 | 持续的进度差异和同步等待。 |
| `memory` | 每核约一半计算、一半访问独立的 512 KiB 数据区，阶段末 barrier。 | 访存、缓存竞争和计算交替。 |
| `idle` | 每阶段约半数核先计算四分之一额度，再 WFI；leader 确认休眠请求后执行固定计算，发送 SGI 唤醒，休眠核继续完成余量。 | 部分 active 成员退出、重新加入和逻辑对齐。 |
| `mixed` | 同一 guest 内反复执行 `total → phased → memory → idle`。总工作量先分为四份，每类约占四分之一。 | 四类行为连续切换后的整体表现。 |

默认 `--phases 8`：phased/memory/idle 各 8 个阶段，mixed 有 8 轮、32 个阶段。
`total` 保留独立计算基线；mixed 中的均匀计算也需要在阶段边界同步。
混合成绩是整次 guest 运行的一对 START/DONE 时间，包含阶段切换，**不是四个单项成绩的平均值**。
五个场景分别生成表格和趋势图，不计算掩盖单项差异的综合分数。

阶段同步使用自旋 barrier，其开销属于测量内容。icount 单线程轮转时可能出现较大的自旋调度成本，
因此成绩反映计算、同步与调度的组合，不能全部解释为 skew 滑窗效率。
phased 对于奇数 vCPU 的两组大小可能不同，各阶段整数分配也会产生尾数。

idle 每阶段轮换 leader 与休眠成员。至少一核保持运行，唤醒由固定计算进度触发，
不依赖三种模式的 guest 时间，不进入全 idle 的定时器跳时测试。
IRQ 在 guest 中保持屏蔽：pending SGI 使 WFI 恢复，C 路径读取 IAR/写 EOI 完成应答，
避免中断处理程序在“检查条件”和 WFI 之间消耗唤醒。
**WFI 指令次数不等于真实停机次数**：若 IRQ 已 pending，WFI 可能立即返回；实际 active 变化需看独立 trace。

memory 用 4093 个 64 位元素的步长遍历 65536 元素，覆盖每核 512 KiB 数据区。
每个访存循环含一次读取和 ALU 运算，共 32 条主体指令；计算循环也为 32 条。
数据区在 START 前初始化，访问结果用独立预期值校验；这模拟访存压力，不能保证任意服务器上都超过 LLC。
它没有虚构硬件 cache miss 或内存带宽数据。

## 快速扫描与工作量

脚本先用单 vCPU 的 total/icount 试跑，估算约 2 秒计算所需的循环量，
然后对所有选定场景、核数、模式使用相同的 `work`。不同场景的耗时不保证同为 2 秒。
每配置默认预热 1 次、正式运行 3 次，以中位数为成绩。

```sh
# 默认五场景；只取几个核数，快速覆盖宿主资源内外的区间
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --cpus 2,4,8,16,32

# 先显示计划，不编译或启动 QEMU
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 --plan

# 分别测试一个场景；名称可换成 total/phased/memory/idle/mixed
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 --scenario idle

# 同时运行五项，增大工作量、重复次数
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 \
    --scenario all --work 720384000 --rounds 7

# 降低阶段切换频率：同一总工作量分成更少的阶段
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 \
    --scenario idle --phases 4 --work 720384000

# 正式测量后，在最大 vCPU 数额外验证 active 退出/重入
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --cpus 2,4,16 \
    --scenario all --diagnose-active
```

全套规模为 `5 场景 × 核数个数 × 3 模式 × (预热次数 + 正式次数)`，另加一次自动校准。
例如 2～24、步长 2 的默认配置共 720 次运行；要快速试跑，优先减少核数点，
或暂用 `--rounds 1 --warmups 0`，不要将单次结果当作稳定的性能结论。

`--work` 表示有用循环总量，每循环 32 条主体指令。memory 将其中一部分分给访存循环，
mixed 再按四类阶段分配；同步、检查、中断和控制指令另计，但耗时计入成绩。
阶段和核数的整数除法尾数舍去，`summary.json` 的 `work_plan` 及 `runs.jsonl` 记录实际量。
宿主独立计算预期工作量、阶段数和 IRQ 数，并核对 guest 输出；所有模式的同配置工作量相同。

旧的 `--scenario percpu`（每核 work）和 `--scenario uneven`（一次性 1:…:1:2）保留，
不属于默认五项。后者主要观察计算尾部，不能替代 staged phased/idle。

| 参数 | 默认值与含义 |
|---|---|
| `--max-cpus N` | 可用逻辑 CPU 数 × 2，最高 512；包含上限。 |
| `--step N` | 2；从 2 开始的间隔，最后补入上限。 |
| `--cpus 2,4,16` | 自定义点集，覆盖范围扫描。 |
| `--scenario NAME` | all；运行五项，也可指定一个场景。 |
| `--phases N` | 8；每类的阶段数，mixed 为 4×N，范围 1～64。 |
| `--target-seconds S` | 2；单核 total/icount 的校准目标。 |
| `--work N` | 固定循环量，跳过校准。过小、不能给每段分到工作时拒绝运行。 |
| `--rounds N` / `--warmups N` | 3 / 1。 |
| `--timeout S` | 180 秒；每个 QEMU 进程上限，含启动。大工作量可增大。 |
| `--diagnose-active` | 额外对选中的 idle/mixed 在最大核数运行 skew trace；不并入性能成绩。 |
| `--output DIR` | `build/skew-sweep-时间戳`，必须新建或为空。 |
| `--qemu PATH` | `build/qemu-system-aarch64`。 |

阶段场景要求至少 2 vCPU；测单核请显式指定 `--scenario total --cpus 1`。

## 看结果

结束时终端打印报告路径，输出目录包含：

- `REPORT_zh.md`：三种模式的耗时、中位数范围、加速比与开销。
- `trend-场景.svg`（单场景为 `trend.svg`）：各场景三条宿主耗时曲线，竖线标出宿主可用逻辑 CPU 数，可直接用浏览器打开。
- `summary.csv`：方便导入表格软件；`summary.json` 保留各次样本。
- `runs.jsonl`：校准、预热、正式运行的逐次耗时、实际工作量和完整 QEMU 命令。
- `config.json`、`environment.txt`：输入参数、CPU affinity/拓扑、构建信息和二进制 SHA-256。
- 各样本的 `.log`：guest 工作量/唤醒计数、校验失败或超时信息。
- 可选 `active-diagnostics.json` 和 `.trace`：START/DONE 区间内各核 active 退出后重新加入的次数，排除启动过程，诊断耗时独立保存。

每取得一个正式样本就更新报告。失败或 Ctrl+C 后保留已有数据，标记为未完成，
只将三个模式都达到规定次数的配置画入曲线，不把缺失样本当作零耗时。
失败时返回非零退出码；使用新的输出目录重跑。

主指标沿用 [host time 基准](SKEW_ICOUNT_HOST_BENCH_zh.md) 的 START/DONE 插件计时，
排除 QEMU 和 CPU 启动，包含同步、窗口等待、宿主抢占、计时区内首次翻译和结果校验。
普通 MTTCG 是同一二进制关闭 skew/icount 的基线。

这些是可调规模的裸机行为负载，不等同于 Linux 上运行任意大型应用。
小于 0.1 秒的样本建议增大工作量；不同机器间直接比较秒数，应使用相同 `--work` 和 `--phases`，
并检查实际有用工作量，而不是各自采用不同的自动校准值。
拓扑来自 Linux/WSL 可见信息，不保证等于实体宿主资源配额。

## 超过 8 / 16 vCPU 的支持

新入口统一使用 virt/GICv3；guest 的栈和状态数组按 CPUS 分配。
启动时按 QEMU virt 的 MPIDR Aff0/Aff1 编码定位 CPU，PSCI CPU_ON 使用同样的编码，
因此可以跨越 16 vCPU 的 affinity 分组。输入上限 512 来自本版本 virt 机器的限制。
sweep 的所有模式统一使用 512 MiB RAM。idle/mixed 的 SGI 操作使用本版本 virt 的固定地址布局：
低区 123 个 redistributor，后续从 256 GiB 高区开始；初始化核对 GICR_TYPER affinity，布局不符会失败。
原有固定矩阵入口继续使用 GICv2 和 128 MiB RAM，历史报告数据保持不变。

## 验证入口

```sh
# 宿主侧工作量计算、奇数核舍入、报告隔离与缺失样本检查
python3 tests/tcg/aarch64/system/skew-perf-sweep-test.py

# 小规模端到端验证，包含奇数核和跨 16 核分组
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --cpus 2,3,18 \
    --work 12000000 --phases 4 --rounds 1 --warmups 0 --diagnose-active
```

本机已验证五场景在 2/3/18 vCPU 下三种模式的运行与工作量一致性，
并单独验证了 8 阶段混合负载、124 vCPU 高区 redistributor 的休眠唤醒及旧 GICv2 路径。
18 vCPU、4 阶段的独立 trace 中，idle 和 mixed 各观察到 36 次计时区间内的 active 重入，
与预定唤醒数一致。这些检查用于验证覆盖与正确性，不作为服务器性能结论。

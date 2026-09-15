# 一键扫描 MTTCG / skew / icount

在 Linux 或 WSL2 的仓库根目录运行：

```sh
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32
```

依次覆盖 `2, 4, 6, …, 32` 个 guest vCPU，每个数量都比较普通 MTTCG、skew、icount。
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
../configure --target-list=aarch64-softmmu --enable-plugins
ninja -j"$(nproc)"
cd ..

python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32
```

构建步骤针对新的 build 目录；已有可用 QEMU 时直接运行最后一行。
若二进制放在其他目录，用 `--qemu /path/to/qemu-system-aarch64` 指定。
脚本使用 Python 3.8+ 的标准库，不需要绘图库；QEMU 自身的构建依赖以 configure 检查为准。
自定义安装 GLib 的环境可通过 `PKG_CONFIG_PATH` 指向其 pkgconfig 目录。

## 快速看趋势与加大程序工作量

默认是固定总工作量的 `total` 场景。脚本先用单 vCPU icount 试跑，
估算约 2 秒计算所需的循环量，然后在整个扫描中固定该总量。
每个配置预热 1 次、正式运行 3 次，以中位数为成绩。
校准目标不包括进程启动，不能用它直接推算整套测试完成时间。

```sh
# 先看将要测试的数量和环境，不启动 QEMU
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 --plan

# 增大固定总工作量：按单 vCPU icount 约 10 秒校准
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 --target-seconds 10

# 明确指定总循环量，跳过校准；正式测量 7 次
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 --work 720384000 --rounds 7

# 只取几个点，更快覆盖超额分配区间
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --cpus 2,4,8,12,16,24,32

# 固定每个 vCPU 的工作量：总程序工作量随 vCPU 数增加
python3 tests/tcg/aarch64/system/skew-perf-sweep.py --max-cpus 32 \
    --scenario percpu --work 12000000
```

每次计算循环包含 32 条有用 guest 指令。例如 `--work 720384000` 在 total 中
表示约 230 亿条有用指令，由所有 vCPU 平分；percpu 中则表示每个 vCPU 都做这么多工作。
同步和控制指令另计。total 的整数除法舍去不足一轮的尾数，原始记录包含实际工作量。
同一个 vCPU 数下，三个模式始终使用相同的 guest ELF 和有用工作量。

| 参数 | 默认值与含义 |
|---|---|
| `--max-cpus N` | 可用逻辑 CPU 数 × 2，最高 512；范围扫描的上限。 |
| `--step N` | 2；从 2 开始的扫描间隔，最后补入上限。 |
| `--cpus 2,4,16` | 自定义点集，覆盖范围扫描设置。 |
| `--scenario total/percpu` | total；固定总量或固定每 vCPU 量。 |
| `--target-seconds S` | 2；自动校准的单 vCPU icount 耗时目标。 |
| `--work N` | 指定循环量并跳过校准。 |
| `--rounds N` / `--warmups N` | 3 / 1；正式次数和每配置预热次数。 |
| `--timeout S` | 180 秒；单个 QEMU 进程上限，包含启动。大工作量可增大此值。 |
| `--output DIR` | `build/skew-sweep-时间戳`；要求新的或空的目录。 |
| `--qemu PATH` | `build/qemu-system-aarch64`。 |

## 看结果

结束时终端打印报告路径，输出目录包含：

- `REPORT_zh.md`：三种模式的耗时、中位数范围、加速比与开销。
- `trend.svg`：三条宿主耗时曲线，竖线标出宿主可用逻辑 CPU 数，可直接用浏览器打开。
- `summary.csv`：方便导入表格软件；`summary.json` 保留各次样本。
- `runs.jsonl`：校准、预热、正式运行的逐次耗时、实际工作量和完整 QEMU 命令。
- `config.json`、`environment.txt`：输入参数、CPU affinity/拓扑、构建信息和二进制 SHA-256。
- 各样本的 `.log`：guest 校验失败或超时的诊断信息。

每取得一个正式样本就更新报告。失败或 Ctrl+C 后保留已有数据，标记为未完成，
只将三个模式都达到规定次数的配置画入曲线，不把缺失样本当作零耗时。
失败时返回非零退出码；使用新的输出目录重跑。

主指标沿用 [host time 基准](SKEW_ICOUNT_HOST_BENCH_zh.md) 的 START/DONE 插件计时，
排除 QEMU 和 CPU 启动，包含同步、窗口等待、宿主抢占、计时区内首次翻译和结果校验。
普通 MTTCG 是同一二进制关闭 skew/icount 的基线。

`total` 看同一个计算任务随 vCPU 增加的扩展趋势；`percpu` 看总工作量一起增加后的承载情况。
这是可调规模的裸机计算程序，不等同于 Linux 上运行任意大型应用。
小于 0.1 秒的样本建议增大工作量；不同机器间若要直接比较秒数，应使用相同 `--work`，
而不是各自自动校准后的不同工作量。拓扑来自 Linux/WSL 的可见信息，不保证等于实体宿主资源配额。

## 超过 8 / 16 vCPU 的支持

新入口统一使用 virt/GICv3；guest 的栈和状态数组按 CPUS 分配。
启动时按 QEMU virt 的 MPIDR Aff0/Aff1 编码定位 CPU，PSCI CPU_ON 使用同样的编码，
因此可以跨越 16 vCPU 的 affinity 分组。输入上限 512 来自本版本 virt 机器的限制。
原有固定矩阵入口继续使用 GICv2，历史报告数据保持不变。

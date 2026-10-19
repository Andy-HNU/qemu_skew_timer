# Skew 进度发布等待复现场景

本场景用于定位固定工作量下的执行退化，并区分两种等待：某个活跃核尚未
完成窗口，以及全部活跃核已经完成窗口但协调器尚未发布进度。
使用真实 AArch64 裸机 Guest 和本仓库 QEMU，无需 Linux 镜像、业务服务或网络。
不改变时钟算法，不在执行路径注入睡眠，不用 Guest 时间决定工作结束。

## 工作负载

默认 4 个 vCPU，每核执行 2097152 个计算单元。每个单元包含 30 条依赖加法、
1 条循环计数和 1 条条件跳转，共 32 条有效计算指令；默认合计 268435456 条。
每 1024 个单元组成一个计算块。

每核以独立的快速倍增 Fibonacci 算法校验全部计算块的结果。全部核校验成功
后才发出结束标记。完成的核进入 WFI；没有执行结束后的忙等。
启动 CPU、初始会合均在计时开始之前。测量区间包括有效计算、计算块调用、
可选计数器读取、最终校验和最后一个核发出结束标记之前的等待。

`compute` 负载不读取 Guest 时钟。`counter` 负载在每块后固定执行 64 次
`CNTVCT_EL0` 读取，以真实寄存器访问产生同步压力。计数器值不控制循环次数。
两种负载分别编译；同一负载的所有对照使用完全相同的 ELF，脚本检查 SHA256。
有效计算指令数不包含计数器循环、函数调用、同步和校验，因此不能当作完整
Guest 退休指令数。

## 对照配置

所有 Skew 用例默认 `skew-ips=200000000`。参数中的时间单位为纳秒。

| 用例 | 负载 | 执行模式 | window | update | 目的 |
|---|---|---|---:|---:|---|
| compute-mttcg | 固定计算 | MTTCG | — | — | 执行基线 |
| compute-skew | 固定计算 | Skew | 1000000 | 100000 | 正常配置的自然退化 |
| compute-delayed | 固定计算 | Skew | 1000000 | 4000000 | 受控延长发布周期 |
| compute-wide | 固定计算 | Skew | 8000000 | 4000000 | 同样发布周期下增加窗口余量 |
| counter-mttcg | 计算与计数器读取 | MTTCG | — | — | 寄存器访问基线 |
| counter-skew | 计算与计数器读取 | Skew | 1000000 | 100000 | 同步压力与 Skew 的相互影响 |

`compute-delayed` 的 4 ms 是明确设置的控制变量。它可以复现发布受限的等待
链路，不能证明正常配置中的发布延迟由 BQL 引起。
`compute-wide` 用于检验增加每次发布允许的执行量能否减轻等待，同时确实放宽
了跨核领先约束；它作为因果对照，不构成生产配置建议。

## 运行

需要支持插件的 QEMU 10.2 Skew 研究版本、AArch64 GCC/binutils、宿主 C 编译器、
GLib 开发头文件、pkg-config 和 Python 3。QEMU 需启用 `log` trace 后端。
本目录的插件使用 Plugin API 5；QEMU 7.2 生产移植版需要适配插件接口后再运行。

已有支持上述选项的二进制可以直接使用。使用交付包重建本次固定版本时，可在
新目录执行以下步骤；`reproduction.patch` 是包内的测试与文档补丁。

```sh
# Ubuntu/Debian 构建依赖，按需安装。
sudo apt-get install git build-essential ninja-build pkg-config python3-venv \
    libglib2.0-dev libpixman-1-dev libfdt-dev zlib1g-dev \
    gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu

git clone --branch codex/skew-cpu-efficiency \
    https://github.com/Andy-HNU/qemu_skew_timer.git qemu-repro-fresh
cd qemu-repro-fresh
git checkout 760b52adf56d7b801d1768dcdb8bb21c391b0c7f
git apply /absolute/path/to/reproduction.patch
mkdir build-publication
cd build-publication
../configure --target-list=aarch64-softmmu --enable-plugins \
    --enable-trace-backends=log --disable-docs --disable-werror
ninja -j4 qemu-system-aarch64
cd ..
```

该示例生成的二进制路径为 `build-publication/qemu-system-aarch64`。配置阶段可能
需要下载 QEMU 自带的构建依赖；测试 Guest 运行期间无需网络。

在仓库根目录执行，`--qemu` 指向实际待测二进制：

```sh
python3 tests/tcg/aarch64/system/skew-publication-repro.py \
    --qemu build/qemu-system-aarch64 \
    --output build/publication-repro-4cpu \
    --cpus 4 --rounds 3 --timeout 30 --suite-timeout 300
```

先运行 18 次无诊断性能测试，奇偶轮反转配置顺序；随后运行 5 次独立诊断。
QEMU 运行期间不构建程序，也不持续轮询 QMP。每次 QEMU 运行均受单次和套件
超时限制；开始运行前的编译子进程另外各有 60 秒超时。
超时会终止本次创建的 QEMU 进程组，保留命令、日志和失败结果。
输出目录必须不存在，已有目录不会覆盖或清理。

32 核版本仍使用每核固定工作量，可按宿主条件设置明确的超时：

```sh
python3 tests/tcg/aarch64/system/skew-publication-repro.py \
    --qemu build/qemu-system-aarch64 \
    --output build/publication-repro-32cpu \
    --cpus 32 --rounds 3 --timeout 120 --suite-timeout 900
```

脚本支持 `--cases` 选取配置；只选部分配置时，仅生成有对应基线的比较。
`--no-diagnostics` 只测性能，不提供发布等待的轨迹证据。

## 观测与判定

宿主插件只对起止两个 MMIO 标记安装执行期回调，以 `CLOCK_MONOTONIC` 测量
固定工作区间，并在边界读取进程累计用户态、内核态 CPU 时间及上下文切换次数。
计算循环没有逐指令执行回调。

性能结果包含墙钟耗时、累计 CPU 时间、用户态与内核态时间、平均使用的宿主
CPU 核数、有效计算指令吞吐和上下文切换次数。平均宿主核数为 CPU 秒除以墙钟秒，
它与 Guest 指令执行占比含义不同。

诊断使用现有 `skew_cpu`、`skew_wait`、`skew_clock`、`skew_warp` 事件。每个窗口
等待入口都验证 `logical - global == window_insns`，重建逐核等待区间和发布间隔。
解析还要求 trace 至少覆盖到结束标记，并记录其大小和 SHA256；缺尾文件判失败。
若记录同时满足以下条件，就获得“已有完成量等待发布”的直接证据：

1. 所有配置的 vCPU 均为 active，并已进入窗口等待。
2. 每个等待核的已结算 logical 计数都大于当前 global。
3. 下一次协调更新将 global 推进到这些已结算计数的最小值。

此时协调器已有可提交的共同进度。等待区间不能归因于某核尚未完成该窗口。
`skew_clock.host_ns` 记录回调入口，早于真正发布，所以“全部完成但尚未发布”的
正时间差作为延迟下界报告。回调入口早于最后一个等待入口的样本不计入该下界。
逐核等待时间允许跨线程相加；其总和不能直接从墙钟时间中扣除。

诊断还保存 QSP 的原始同步报告，覆盖启动和工作区间。QSP 的 mutex 时间是
获取锁的耗时，condition 时间包括睡眠及重新获取互斥锁，均不等同于持锁时长。
QSP 与 trace 都会扰动执行，诊断运行不纳入性能对照。

脚本事先固定判据，并分别输出结果：

| 判据 | 条件 |
|---|---|
| 发布等待证据成立 | 至少 3 个全部活跃核已完成的区间，累计延迟下界至少 1 ms |
| 正常配置退化复现 | 正常配置证据成立，且配对 Skew/MTTCG 耗时比中位数至少 1.2 |
| 受控退化复现 | 延长发布周期的证据成立，且配对 delayed/default 耗时比至少 1.5 |
| 窗口对照恢复 | 配对 delayed/wide 耗时比中位数至少 1.5 |

未满足判据时保留“不成立”的结果，不能把受控延迟结果替代正常配置结果。
进程退出码 0 表示运行和采集完成；是否复现须查看 `verdict.json` 的各项布尔值。
这些判据用于可重复的工程复现，不表示统计显著性或所有宿主上的性能保证。

## 输出

| 文件 | 内容 |
|---|---|
| `environment.json` | 二进制与源码 SHA256、工具版本、宿主 CPU 亲和性、cgroup 限额 |
| `plan.json` | 开始测试前固定的用例顺序 |
| `build-commands.json`、`build.log` | Guest 与插件构建命令和日志 |
| `artifacts/` | 本次实际使用的 ELF 与插件 |
| `r*-*/command.json`、`qemu.log`、`results.json` | 每次正式运行的命令、日志及测量值 |
| `diagnostic-*/trace.log`、`evidence.json` | 原始轨迹、逐核等待及可提交进度证据 |
| `diagnostic-*/sync-profile.txt` | QSP 原始同步报告 |
| `diagnostic-*/final-skew.json` | 测量结束后的单次时钟状态快照 |
| `summary.json`、`paired-ratios.json`、`verdict.json` | 汇总、逐轮配对比值、独立判定 |

该场景定位 Skew 的执行与发布链路，未运行 Fast Models，也未复刻生产 MML
登录协议、设备模型和服务逻辑。验证版本与实测结果记录在
[本次验证记录](SKEW_PUBLICATION_REPRO_VALIDATION_2026-10-19_zh.md)。

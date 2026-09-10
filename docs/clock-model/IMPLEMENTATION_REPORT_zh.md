# 单 QEMU Skew 时钟实现与验收报告

日期：2026-09-10

## 结论

已在 `codex/single-qemu-skew-clock` 分支实现单 QEMU、MTTCG 多 vCPU 指令驱动时钟。
分支基于 `v10.2.0`，基线提交为 `698104725efad4b29079d857dfdebbd804e34c99`。

**本机可执行的时钟引擎验收通过：47 组检查全部成功，另有 10 组上游回归测试通过。**
精确计数、严格滑窗、实际多线程并行、空闲跳时、暂停恢复、定向 Host 减速、真实工作量超时均有运行证据。
最终切换到 QEMU 通用 64 位原子访问 API 后已重建，并再次通过全部快速验收。

机器可读记录见同目录的 `acceptance-results.json`（47 组完整运行）和 `final-smoke-results.json`（最终重建后的快速验收）；完整日志保存在 `build/skew-acceptance/` 与 `build/skew-final-smoke/`。

**这不等于原测试文档的完整业务验收已经完成。** 仓库没有实际板卡 Guest 镜像、收发包设备启动配置及性能/定时器延迟门限；真实设备业务路径和 Linux 调度场景仍待验证。已向使用方询问这些输入。

## 实现

主要代码位于 `accel/tcg/skew.c`，复用既有 TCG 指令预算、TB 截短、异常恢复、线程通知和 Timer 子系统，没有增加依赖、跨进程服务或独立协调线程。

| 部分 | 实际行为 |
| --- | --- |
| 原始计数 | 每核独立 `skew_raw_icount`，使用 QEMU 64 位原子访问接口 |
| 逻辑进度 | `logical_base + raw_icount - raw_base`；重新加入 active 时对齐 global |
| 时间协调 | 主线程持 BQL 扫描 active 核，取逻辑进度最小值，global 只增不减 |
| 全局时钟 | `QEMU_CLOCK_VIRTUAL = floor(global_icount * 1e9 / SIM_IPS) + warp_ns` |
| ARM 计数器 | 原有 CNTPCT/CNTVCT 路径读取同一虚拟时钟；CNTVOFF 语义保留 |
| 窗口 | 执行前限制预算；TB 超过剩余额度时复用既有短 TB 重编译机制 |
| 等待 | 额度耗尽时在 BQL 条件变量上睡眠，仍属于 active；协调推进后唤醒 |
| 控制事件 | 继续使用既有 kick、CPU 工作队列及停止路径；不在等待期间占住 BQL |
| 真正空闲 | WFI/halted/stopped 等退出同步集合；all-idle 且 VM running 时才考虑跳时 |
| 跳时 | 查询全部虚拟 Timer 的最近 deadline；累计 warp 偏移，后续指令时间承接跳时 |
| 暂停 | 停止协调 Timer，虚拟时间不再更新；暂停不是空闲跳时 |
| 不兼容模式 | 拒绝单线程、原生 icount、record/replay；注册迁移/快照 blocker |

实现选择及边界：

- 复用 `CF_USE_ICOUNT` 插桩，但不启用原有 `use_icount` 全局模式，因此仍使用 MTTCG。
- 一段执行最多 `min(65535, UPDATE_ICOUNT, 剩余窗口)` 条指令。段结束后才原子更新 raw，最多落后一个在执行的段；没有额外的 published 计数。主线程读到旧进度只会推迟时钟。
- active 与两个基准值由 BQL 保护，避免基准混用。窗口预算也是在 BQL 下取得；Guest 指令在锁外并行执行。
- 全部核真正空闲时，先提交最后执行尾段的最大逻辑进度，再跳到 Timer deadline。无 Timer 时不凭空增长时间，并将 Host 轮询退避到至少 10ms。
- `skew-update` 是 Host 协调轮询间隔，不是虚拟时间步长上限；没有用 Timer deadline 限制正常运行的 CPU 预算。
- QEMU 10.2.0 的 ARM WFE 不提供真正的事件等待低功耗状态；保留其原有语义，不把 WFE 指令本身当作退出 active 的依据。
- ARM 非整除 CNTFRQ 仍沿用上游的整数纳秒 tick period；本次未改变该计数器/Timer 换算约定。验收频率为 62.5MHz。

## 使用与复现

在仓库根目录构建：

```sh
./configure --target-list=aarch64-softmmu --disable-docs \
  --disable-gtk --disable-sdl --disable-vnc --enable-debug \
  --enable-trace-backends=log
build/pyvenv/bin/meson configure build -Doptimization=2 -Dwerror=true
ninja -C build -j2 qemu-system-aarch64
```

保留原 Guest 启动参数，将 accelerator 设置为：

```sh
-accel tcg,thread=multi,skew=1000000,skew-ips=2000000000,skew-update=100000
```

`skew` 与 `skew-update` 的单位是 ns；默认关闭 skew，默认 IPS 为 2,000,000,000，默认更新间隔为 100,000ns。
两个间隔各自允许 1..1,000,000,000ns，且至少对应一条指令；IPS 允许 1..1,000,000,000,000。更新间隔与窗口独立配置。
不要同时传入 `-icount`。

验收脚本需要 Python 3、AArch64 GCC/binutils、Host C 编译器及 GLib 开发头文件，Guest 由脚本从源码编译，不需要下载操作系统镜像：

```sh
python3 tests/tcg/aarch64/system/skew-check.py \
  build/qemu-system-aarch64 --output build/skew-acceptance
```

`--quick` 执行计数、配置拒绝、控制、空闲、双核通信和 Timer 检查；完整运行额外包含 Host 减速、压力测试、参数扫描、原生 icount 回归及三轮性能对照。
每项有 Host 看门狗。结果写入 `results.json`；Guest 输出、协调日志、ELF 和减速插件保留在输出目录。

本次上游回归命令：

```sh
build/pyvenv/bin/meson test -C build --num-processes 2 --print-errorlogs \
  test-div128 test-mul64 test-int128 ptimer-test test-aio \
  test-aio-multithread check-qom-proplist \
  qtest-aarch64/arm-cpu-features qtest-aarch64/qmp-test \
  qtest-aarch64/boot-serial-test
```

诊断选项：

```sh
-trace 'enable=skew_*' -trace enable=arm_gt_timer_expire -D skew.trace
```

`skew_*` 记录 Host 单调时间、active、raw、映射基准、logical、global、虚拟时间、等待及跳时。
ARM Timer 日志记录 CVAL、回调时 counter、tick period 和虚拟时间；实际 IRQ 入口时间由验收 Guest 记录。
QMP `qom-get` 可读取 `/machine` 的 `skew-time` 和 `query-cpus-fast` 返回路径上的 `skew-raw-icount`。
需要跨字段静止快照时先执行 QMP `stop`。

## 验收结果

环境：Linux 5.15.0-164-generic，x86-64，AMD EPYC 9754 虚拟机，2 个可用逻辑 CPU；GCC 11.4，AArch64 GCC 11.4，QEMU `-O2`、调试断言、`-Werror`。
Guest 为 `virt,gic-version=2`、Cortex-A57、128MiB，默认双核。Host 只有两个逻辑 CPU，无法满足文档中主线程与两个 vCPU 分别独占三个物理核的布置。

| 检查 | 结果 |
| --- | --- |
| C1/C2 精确计数 | 短循环 64,000,002；长循环 64,000,002 和 128,000,002；实测与汇编推导完全一致 |
| C3 中途退出 | 同一区间包含 MMIO、30 条 NOP、SVC 与异常向量返回；实际 36 条，符合预期，没有重复计算未执行尾段 |
| 极小窗口 | 7ns 窗口、3ns 更新，窗口只有 14 条指令；36/226/36 条区间精确一致，覆盖 TB 超出剩余预算 |
| C4/C5/C6 时间映射 | 逐次校验 active 最小进度与整数时间公式；包括非整除 IPS=1,999,999,973，无回退、无逐次舍入漂移 |
| C7/C8 计数器 | release/acquire 握手时间不回退；CNTVOFF=1234 的 100,000 次读取测试通过 |
| S1/S3/S4 窗口与推进 | 带日志运行累计 299,815 次协调检查、4,128 次等待；默认最大领先 2,000,000 条，无窗口越界 |
| 真正并行 | 两个不同 vCPU Host 线程分别绑定逻辑 CPU 0/1；500.10ms 采样内合计运行 728.66ms，比例 1.457，证实执行重叠 |
| S6/A11 控制 | 100 次 QMP stop/cont，暂停期间 raw 和虚拟时间逐次完全不变；期间运行跨核 TLBI 广播 |
| A1/A4/A5/A7/A10 | 100,000 次 WFI/Timer 唤醒完成，后续计算继续推进，没有永久等待 |
| A6 | 两核计算或滑窗等待时没有触发空闲跳时 |
| A9 | 全核无 Timer 空闲 250ms，raw 与虚拟时间保持不变；PL011 外部 RX IRQ 成功唤醒 |
| Timer | 10us/100us/1ms/10ms，覆盖 all-idle 唤醒和双核计算期间真实 IRQ 入口；回调、IRQ 均不早于 deadline |
| 共享内存压力 | 1,000,000 次请求应答；序号与结果正确；2,000,000 次原子累加无丢失，含 WFE 轮询 |
| 模式拒绝 | 6 种非法/冲突配置按预期退出；QMP 迁移请求被 blocker 拒绝 |
| 原生 icount 回归 | 单线程原生 icount 的计算和 WFI Timer Guest 均完成 |
| 上游回归 | 10 组、641 个子测试通过，0 失败；包括 AIO、多线程 AIO、ptimer、QMP、ARM CPU 和启动 |

计数口径继承上游 TCG icount：被重执行的 MMIO/原子访存会回退到当前指令前；同步 SVC 及实际执行的异常处理指令计入。并非目标硬件 PMU 的微架构退休/周期模型。测试不穷举所有异常和原子慢路径。

## 超时与性能

通信测试每组包括 32 个正常请求（每个约 400,000 条工作指令）、一个约 20,000,000 条工作指令请求、一个无回复请求；Guest timeout 为 5ms。

| 场景 | Host 完成时间 | 正常请求超时数 | 重工作/无回复 |
| --- | ---: | ---: | --- |
| Skew 默认，带日志 | 0.567s | 0/32 | 两者均触发超时 |
| Skew，发送核定向减速 | 16.948s | 0/32 | 两者均触发超时 |
| Skew，接收核定向减速 | 20.466s | 0/32 | 两者均触发超时 |
| 关闭 Skew，接收核同样减速 | 14.618s | 32/32 | 两者均触发超时 |

减速由测试插件在指定 vCPU 的 TB 回调中周期性 nanosleep 实现，没有修改 Guest 工作量。表中是实际任务耗时，不把配置的 sleep 时长冒充精确的 1/10 或 1/100 线程速率。

关闭诊断日志，三轮整任务耗时中位数：

| 负载 | MTTCG 关闭 Skew | Skew 默认 | 耗时比 |
| --- | ---: | ---: | ---: |
| 单核计数循环 | 114.59ms | 296.38ms | 2.59x |
| 双核独立计算 | 48.90ms | 57.95ms | 1.19x |
| 双核请求应答 | 89.04ms | 475.58ms | 5.34x |

这是同一构建启用/关闭功能的微基准，不是另行编译的原始 tag 性能报告；计入 QEMU 启动时间。
请求应答还包含虚拟时间超时等待，因此耗时比不能全部解释为插桩开销。极短 TB 循环对预算检查敏感；本结果不能代表实际板卡业务性能。没有给定性能门限，不能宣称性能验收已通过。

默认参数下，32 个样本的延迟统计（P50/P99/最大值，P99 使用样本下标法）：

| 场景 | Callback 延迟 | Guest 唤醒/IRQ 入口延迟 |
| --- | --- | --- |
| WFI Timer | 0 / 205.664 / 533.328us | 0 / 205.664 / 533.328us |
| 双核计算中的 IRQ | 23.520 / 486.192 / 645.344us | 23.520 / 486.192 / 645.344us |

这是虚拟时间延迟；相同数值可能来自同一个协调时间样本，不意味着 Host 投递耗时为零。32 个样本不足以给出稳定的生产环境 P99。
普通时间更新实测可达 1ms；空闲时 10ms Timer 的跳时也符合预期。因此不能把 `skew-update=100000` 宣称为 100us 的 Timer 延迟保证。

## 仍需业务验收

- 实际收发包设备路径、至少百万次真实设备交互及持续业务运行；当前百万次是共享内存测试，UART 仅验证通用设备外部 IRQ。
- Linux task/mutex 调度、Timer 等待回复、多 Timer 同时竞争、修改/取消 deadline 的竞争注入、定向主线程延迟和完整热插拔/重置场景。
- 其他 Guest/Host 架构、非整除 CNTFRQ 的专门用例、全部异常/原子慢路径；没有运行 TSAN。
- 项目认可的性能和 Timer 延迟门限，及三个独立物理核上的重复测量。
- 上游版本没有文档所述 slowtime 扩展，无法提供固定 slowtime 对照。

特别注意：扫描发现 `skew=10ms` 大于 5ms timeout 时，20M 指令请求可能在全局时钟达到 timeout 前返回。该配置允许的本地领先已经超过超时余量，不能用它保证该业务超时。10ms 窗口已用于计算推进测试；默认 1ms 窗口用于业务超时检查。部署前应按业务余量选择窗口，并用真实负载验证。

交付内容：`codex/single-qemu-skew-clock` 分支包含实现、可运行测试、参数说明、本报告与机器可读验收记录；原始两份设计/验收文档以原内容一并纳入版本管理。

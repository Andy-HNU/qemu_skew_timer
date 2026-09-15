# skew / icount 的 host time 基准

该基准比较固定有用计算工作量的宿主墙钟耗时，不使用 guest 时间作为成绩。
[已完成的 WSL2 对比报告及原始数据](benchmarks/20260914-host/REPORT_zh.md)。
源文件位于 `tests/tcg/aarch64/system/skew-perf*`；不改动 QEMU 运行机制。

换机器快速扫描 `2, 4, 6, …` 到自定上限，请使用 [一键扫描入口](SKEW_SWEEP_zh.md)。

## 测量边界

裸机 guest 启动所有 CPU，CPU0 等待 ready，然后向 PL011 UART MMIO 写入 START。
测试插件只在汇编标签 `marker_store` 处注册内存写回调，使用宿主
`clock_gettime(CLOCK_MONOTONIC)` 记时。CPU0 随后发布开始标志；每核
完成计算并独立校验后，原子递增完成计数，最后完成的核在同一位置写入 DONE。
其余核直接 WFI，不让 CPU0 在收尾时自旋。
计时包含同步、自旋、skew 窗口等待、host 抢占、首次执行的代码翻译和结果校验，
排除 QEMU 启动、CPU 启动和退出。串口关闭，不通过收取日志的时间计时。
插件在结束时间戳之后才输出纳秒结果，计算循环没有执行回调。
两种时间模式使用同一个 ELF；插件也完全相同。

计算循环每轮执行 30 条相互依赖的 add，以及 subs 和条件分支，共 32 条指令。
从两个 1 出发生成模 2^64 的 Fibonacci 序列；guest 使用独立的 O(log n)
倍增算法校验最终值，防止工作量被省略或计算错误。仅循环体计入“有用指令量”，
控制、自旋和计时指令不计入该量，但它们的时间均计入成绩。

## 场景

- total：固定总循环量，平均分配到 1/2/4/6 核。
- percpu：每核固定循环量（主参数的 1/6），总工作量随核数增加。
- barrier：固定总量，分为 128 段，每段有一次所有核参与的 barrier。
- uneven：固定总量按 N+1 份分配，最后一核取得两份，其余各一份。
  每核（含 CPU0）完成后直接 WFI，最后一核结束测量。

barrier 和 uneven 测 2/4/6 核。整数除法舍去不足一轮/一段的尾数，
原始数据记录各配置实际有用循环和指令数；同一配置的不同时间模式工作量严格相同。

## 配置与解释

icount 使用 `tcg,thread=single` 和 `-icount shift=0,sleep=off`；
skew 使用 MTTCG、window=1ms、IPS=10^9、update=100us；普通 MTTCG 为辅助基线。
没有 trace，没有人为拖慢 CPU 的插件。每个模式/场景/核数先进行一次独立进程预热，
正式进行 7 轮，每轮随机打乱配置次序，并在同一配置中随机排列三个模式，种子固定。
每个样本启动新 QEMU，因此预热不保留 TB 缓存，只用于宿主及文件缓存预热。

主指标为 7 次 host 秒数的中位数，附最小/最大值；不删异常样本。
加速比 = icount 中位数 / skew 中位数；耗时降低 = (1-skew/icount)*100%。
该结果比较的是不同时间机制的实际方案性能，不表示两种机制具有相同的多核时间语义，
也不代表 Linux、真实应用、内存密集型负载或任意 window 配置的性能。

## 运行

在仓库根目录、AArch64 交叉工具链和 QEMU plugin 头文件可用的环境中运行：

```sh
export PKG_CONFIG_PATH=/home/andy/qemu-build-deps/glib/lib/pkgconfig
python3.9 tests/tcg/aarch64/system/skew-perf.py \
  --output build/skew-perf-new --work 720384000 --rounds 7
python3.9 tests/tcg/aarch64/system/skew-perf-report.py \
  build/skew-perf-new docs/clock-model/benchmarks/new
```

`runs.jsonl` 包含预热和正式样本、完整启动参数、实际工作量与进程总耗时。
`summary.json` 只汇总正式样本。`environment.txt` 记录环境、构建选项和二进制校验值。请使用新的输出目录避免把重复运行混入结果。
`--pilot` 可用小工作量验证所有模式及计时/结果校验路径。

## 无 guest 周期定时器的边界

本基准没有设置周期性 guest 定时器。当前 icount_get_limit 在没有近期
虚拟定时器截止期时可取 INT32_MAX，再由 icount_percpu_budget 按 CPU 数分配。
因此 barrier 中的自旋可能等待很长的模拟指令时间片才能让其他核执行。
这个场景测的是同步及调度组合，而不是相同总执行指令数的算术吞吐；
不能把其加速比外推到有系统 tick 和 OS 调度的 Linux 工作负载。
主结论优先参考 total 的均衡固定计算任务。

# Skew CPU 执行效率验证记录

日期：2026-10-18。基线为 `16ec93bd1cbcb651ba9e3b616bdc1b26ddcc54b1`。
宿主配额 8 CPU、内存限额 8 GiB；Guest 为 AArch64 / Cortex-A57。
每项运行使用独立目录；原始产物保留在 `build/`，本页只记录可复现入口和结论。

## P1：窗口内续配

- `ninja -C build/optimized -j2 qemu-system-aarch64 tests/unit/test-exec-budget`：通过。
- `build/optimized/tests/unit/test-exec-budget`：4/4 通过。
- `skew-momentum-anchor.py`、`skew-visible-cas.py`、
  `skew-visible-switch.py --build-dir build/baseline`：通过各自限定范围的定向回归。
- `timeout 240 python3 tests/tcg/aarch64/system/skew-check.py
  build/optimized/qemu-system-aarch64 --quick --no-host-runtime
  --output build/p1-quick`：15 项真实 Guest 检查通过。

精确计数覆盖 1、65535、65536、200000 条指令窗口，普通及截短 TB、MMIO/SVC
异常回退，以及大工作量循环。其余检查覆盖无效参数、100 次暂停/恢复、
迁移拒绝、TLBI 广播、UART 唤醒、全 idle、双核通信与真实定时器 IRQ。
单核计数场景记录 3944 次非零预算装载和 147 次执行循环返回，证明计数槽
续配可以留在 CPU 执行循环内；该数量关系不代表宿主 CPU 执行占比。

宿主的 `/proc/<pid>/task/<tid>/schedstat` 不可见。原快速入口在此测量处失败，
新增显式 `--no-host-runtime` 后继续验证功能；运行时间及并行比例字段为 `null`，
测量状态为 `omitted`。未读取到的指标没有作为零值或通过证据使用。

## 真实 Linux 32 vCPU

内核为 QEMU 上游功能测试使用的 Linux 6.4.16：

- URL：`https://storage.tuxboot.com/buildroot/20241119/arm64/Image`
- SHA256：`b74743c5e89e1cea0f73368d24ae0ae85c5204ff84be3b5e9610417417d2f235`

入口：

```sh
timeout 150 python3 tests/tcg/aarch64/system/skew-linux-efficiency.py \
    build/optimized/qemu-system-aarch64 --kernel build/assets/Image \
    --output build/linux32-run --cpus 32 --timeout 120
```

基线和 P1 二进制均通过：32 个 CPU online、32 个绑核线程、两个阶段的独立
Fibonacci 校验、部分线程的 Linux nanosleep 唤醒、计数器单调及正常关机。
总计 10400000 个计算单位，循环主体为 332800000 条 Guest 指令；
Linux 启动、线程和系统调用等其他指令另计。

本组为功能及初步耗时检查，尚无正式重复性能比较。单次完整运行：
基线 3.567 s / 7.498 CPU-s；P1 为 3.374 s / 7.584 CPU-s。
墙钟和 CPU 时间覆盖整个 QEMU 进程，不能由此推算生成代码执行占比。
本入口不验证 jitterentropy 初始化或熵质量。

## P2：无 BQL 窗口等待

`build/p2-quick` 的 15 项真实 Guest 检查全部通过；插值采样及模式切换定向回归通过。
等待发生在 tcg_cpu_exec / cpu_exec_end / RCU 读侧临界区结束之后。
TCG kick 先发布退出请求，再持有进度锁通知 halt_cond，避免检查与入睡之间漏通知。
定时器队列查询和设备通知位于进度锁之外，锁序为 BQL → progress_lock。

```sh
timeout 150 python3 tests/tcg/aarch64/system/skew-linux-efficiency.py \
    build/qemu-p2 --kernel build/assets/Image --switch \
    --output build/p2-linux32-switch --timeout 120
```

32 vCPU Linux 通过两阶段的完整计算、MTTCG/skew 双向转换，以及各阶段
实际工作开始后的暂停/恢复；暂停前后时钟和全部 CPU 状态相同，正常关机。
这组含控制操作，未作为性能样本使用。

## 后续阶段

P3 的进度发布、完整回归及重复性能比较待实现后记录。

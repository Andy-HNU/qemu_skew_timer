# Skew 测试与验收

本页说明当前测试入口及判定边界，不是某次运行的通过报告。所有命令从
仓库根目录运行；先按 [README](README.md) 构建，并把 TMPDIR 指向
`build/skew-tmp`。每轮输出使用新目录，保存命令、环境、二进制版本和失败日志。

## 最小真实 guest 路径

```sh
python3 tests/tcg/aarch64/system/skew-check.py build/qemu-system-aarch64 \
    --quick --output build/skew-quick
```

脚本需要 AArch64 GCC/binutils、插件构建所需的 GLib 和 QEMU plugin 头文件。
quick 覆盖精确指令计数、很小的窗口与协调间隔、暂停/恢复、idle 唤醒、
定时器及多核运行检查。去掉 `--quick` 运行完整矩阵，增加基线、IPS/窗口、
延迟插件与 icount 对照。不要只检查结果文件是否存在；检查脚本退出码、
各项断言和完整结果。测试需要宿主暴露可用的线程运行统计。

上述入口及两个 Linux 集成入口使用 QEMU 原生 `pipe:` 字符设备，通过
`TMPDIR` 下的 POSIX FIFO 传送 QMP、GDB 和串口协议，不需要 Unix/TCP socket。
传输读写有超时，QEMU 提前退出也会报错；计数、暂停和定时器断言不变。
共享传输的可运行自检：`python3 tests/tcg/aarch64/system/skew_test_io.py`。

## 定向回归

- `build/tests/unit/test-exec-budget`：公共预算接口及退出通知高位保持
- `skew-visible-cas.py`：实际发布函数、强制 CAS 交错与并发发布
- `skew-momentum-anchor.py`：实际函数的单线程可控时钟夹具，验证独立采样锚点
- `skew-visible-switch.py --build-dir build`：提取时钟/CPU函数，测试切换边界与读者停稳
- `skew-perf-sweep-test.py`：工作量和报告计算单元测试

上面四个 Python 文件位于 `tests/tcg/aarch64/system/`。
这些是各自限定范围的单元/定向测试，包含测试夹具；不能代替真实 QEMU、
guest、设备和操作系统路径的验收。
独立宽整数与 QMP IPS 边界检查见[实验入口](../../paper/README.md)。

## Linux 集成

需要静态 AArch64 libc、cpio，以及同版本 ARM64 kernel 和
`jitterentropy_rng.ko`、`af_alg.ko`、`algif_rng.ko`；其余依赖编入内核。

```sh
python3 tests/tcg/aarch64/system/skew-linux-switch.py build/qemu-system-aarch64 \
    --kernel /path/to/kernel --jitter-module /path/to/crypto/jitterentropy_rng.ko \
    --output build/skew-linux-switch
python3 tests/tcg/aarch64/system/skew-linux-jitter.py build/qemu-system-aarch64 \
    --kernel /path/to/kernel --module-dir /path/to/crypto \
    --output build/skew-linux-jitter
```

切换测试覆盖运行中及暂停时的双向转换、计数器连续性和定时器；jitter
测试从复位持续开启 skew，检查初始化及 256 次 AF_ALG 读取。分别核对退出码、
guest 完成标记和结果，不能把初始化成功当作持续读取成功或熵质量证明。

## 性能与测量边界

[性能扫描](SKEW_SWEEP_zh.md)保留均匀、阶段不均衡、访存、idle 和混合负载；
[宿主耗时说明](SKEW_ICOUNT_HOST_BENCH_zh.md)定义 ROI 与工作量。
性能、功能验收与[联合计数器实验](../../paper/README.md)分别报告。

正式比较使用相同二进制、负载和配置；记录预热、重复次数、随机顺序、
宿主超时和所有失败。报告中位数及范围，不把采样最大值当严格上界，
不把缺失指标当零。有限窗口下模型停滞会使 visible 到达上界并平台化；
有限测试不等于形式化证明，硬件标定和特定业务超时仍需单独验证。

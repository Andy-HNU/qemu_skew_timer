# QEMU v7.2.0 skew 移植

基线为上游 `v7.2.0`（`b67b00e6b4`），功能同步到
`codex/skew-annotated-zh` 的 `17d49d3d497ce9f2235c739acfedcca7e0a0e086`。
这是本项目的功能移植分支，上游 QEMU v7.2.0 不含这些扩展。

保留单 QEMU / 多 vCPU 的 MTTCG 并行执行、独立执行预算、严格领先窗口、
全 idle 跳时、暂停冻结、动量插值、共享 visible CAS-max、独立采样锚点，
以及无读者登记的 `skew-start` / `skew-stop` 双向模式切换。

## v7 接口适配

- 使用 `sysemu/` 头文件和 v7 的 iothread/BQL 接口。
- 通过 `CPUState.icount_decr_ptr` 操作目标 CPU 的递减器；只写低 16 位，
  保留通用退出通知高位。原有 icount 调度和长预算策略继续使用 v7 实现。
- MTTCG 线程保留 v7 的事件等待循环，增加 prepare/account/idle/window-wait。
  原子重试先完成再结算，TB 中途退出经公共预算接口退还未执行额度。
- 新增只允许在 BQL 下、全部 vCPU stopped 后调用的同步 TB flush，
  在时钟重锚之前完成旧 TB/plugin 回调；没有把异步 flush 当作阶段屏障。
- 使用 v7 的 migration blocker 和 TCG source-set 接口。QMP 扩展受
  `CONFIG_TCG` 保护，用户态模拟不注册 skew 参数、不启用 skew。
- ARM Generic Timer trace 使用 v7 原有物理 Counter 语义。
- v7 的 `gen-icount.h` 计数占位指针改为线程私有，避免 MTTCG 并发翻译
  把另一个 TCG context 的临时量写入当前 TB。
- 在 v7 轮次末尾的 `qemu_wait_io_event()` 前更新 idle 成员，防止 WFI
  CPU 留在 active 集合，保证全 idle 冻结及 deadline warp。
- v7 插件 API 没有写值读取接口：性能 marker 改用独立 start/done store
  PC，继续校验 UART 地址、访问宽度、CPU 和起止顺序，不插桩工作循环。
  delay 插件显式包含 GLib 头；标准 TCG Makefile 排除需专用构建的 skew 源。

## 构建

需要 Linux / WSL、QEMU v7.2.0 的常规构建依赖、Meson/Ninja、AArch64 GCC
交叉工具链、静态 AArch64 libc 和 cpio。Linux 测试额外需要配套 ARM64
kernel 和 `af_alg.ko`、`algif_rng.ko`、`jitterentropy_rng.ko`，其余依赖须编入
kernel。不能混用不同 kernel 版本的模块。

`skew-check.py` 会读取 `/proc/PID/task/TID/schedstat` 验证两核均有运行时间。
若宿主关闭 `kernel.sched_schedstats`，测试前应启用，测试后恢复原值。
测试脚本不会修改宿主 sysctl。本次原值为 0，验收期间为 1，结束后恢复为 0。

```sh
mkdir build
cd build
../configure --target-list=aarch64-softmmu,aarch64-linux-user \
    --enable-plugins --enable-trace-backends=log --enable-debug \
    --disable-docs --disable-gtk --disable-sdl --disable-vnc
meson configure -Doptimization=2 -Dwerror=true
ninja -j6 qemu-system-aarch64 qemu-aarch64 tests/unit/test-exec-budget
cd ..
```

本次 WSL 的 GCC 10/libc 组合对 v7 原有 `net/dump.c` 的 `writev` 报
`stringop-overflow`，构建时额外使用
`--extra-cflags=-Wno-error=stringop-overflow`，其余警告仍视为错误。
未为此改动上游网络代码。具体环境、失败日志和通过结果见移植验收报告。

## 一键测试

```sh
python3 tests/tcg/aarch64/system/skew-suite.py build/qemu-system-aarch64 \
    --build-dir build --output build/skew-acceptance \
    --kernel /path/to/arm64-kernel \
    --module-dir /path/to/matching/kernel/crypto --linux-repeats 2
```

默认包含完整裸机矩阵、执行预算单测、visible 模式切换、采样锚点、CAS
并发和扫描脚本测试，以及 2/3 核、5 种负载、MTTCG/skew 两种模式的性能工具
功能冒烟测试，再运行 Linux 启动、运行中/暂停的模式往返及持续
skew 下的 jitterentropy 初始化和 256 次 AF_ALG 读取。输出目录必须是新目录；
保留每项命令、日志、输入 SHA-256、guest/QMP 原始记录和阶段结果，遇失败
立即返回非零，不隐藏重试。

`--quick` 仅将裸机矩阵缩减为 11 个核心场景。省略 kernel/module-dir 时
仅运行无需 Linux 镜像的测试，并明确记录未请求 Linux 测试。

默认长性能扫描排除 icount，保留裸机矩阵中两个短 icount 回归场景。
需要长 icount 性能对照时显式加 --include-slow-icount。扫描工具本身
仍默认三模式，可用 --skip-icount 避免长耗时或反复超时的 icount。
未请求、超时及通过分别记录；不把中止项当作通过。

也可以单独执行原测试入口：

```sh
python3 tests/tcg/aarch64/system/skew-check.py build/qemu-system-aarch64 \
    --output build/skew-full
python3 tests/tcg/aarch64/system/skew-linux-switch.py build/qemu-system-aarch64 \
    --kernel /path/to/kernel --jitter-module /path/to/jitterentropy_rng.ko \
    --output build/skew-linux-switch
python3 tests/tcg/aarch64/system/skew-linux-jitter.py build/qemu-system-aarch64 \
    --kernel /path/to/kernel --module-dir /path/to/kernel/crypto \
    --output build/skew-linux-jitter
```

## 文档

- [v7.2.0 实测与复现记录](BACKPORT_V7_2_VALIDATION_zh.md)
- [时钟与窗口设计](DESIGN_zh.rst)
- [公共执行预算](EXECUTION_BUDGET_zh.md)
- [v10 源方案实现导读（v7 差异见验收报告）](SKEW_CODE_WIKI_zh.md)
- [QMP 双向切换接口](SKEW_QMP_zh.md)
- [visible time 阶段重锚与发布者约束](SKEW_VISIBLE_SWITCH_zh.md)
- [插值和采样锚点](SKEW_VISIBLE_TIME_MOMENTUM_zh.rst)
- [性能扫描工具](SKEW_SWEEP_zh.md)

其余来自 v10.2.0 的历史验收/性能记录只用于追溯原方案，不能作为
v7.2.0 的测试结果。此分支的实测以移植验收报告和本次输出为准。
迁移、快照、record/replay、skew 与原生 icount 同时启用仍不受支持。

上游回归入口（plugins 构建与 guest 运行分两步，兼容 v7 的 Makefile）：

```sh
ninja -C build test-plugins
make -C build -j4 check-tcg
meson test -C build --suite unit --suite qtest --suite qapi-schema \
    --print-errorlogs --num-processes 4
```

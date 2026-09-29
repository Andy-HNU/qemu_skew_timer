# QEMU v7.2.0 skew 移植验收（2026-09-29）

移植基线是上游 `v7.2.0`，提交 `b67b00e6b4c7831a3f5bc684bc0df7a9bfd1bd56`。
功能与测试同步自 `codex/skew-annotated-zh` 的
`17d49d3d497ce9f2235c739acfedcca7e0a0e086`（最新 visible-time 模式切换提交）。
开始移植和交付前都通过 `git ls-remote origin refs/heads/codex/skew-annotated-zh`
核对该提交。工作分支为 `codex/skew-v7.2.0`，工作树在
`/home/andy/qemu-skew-v7.2.0`；v10 原工作树未修改。
这些是本项目的扩展，不是上游 v7.2.0 原有功能。

## 功能与 v7 适配

保留单进程多 vCPU 的 MTTCG 并行执行、公共执行预算、严格领先窗口、
全 idle deadline warp、暂停冻结、动量插值、独立采样锚点、共享 CAS-max
visible 高水位，以及无读者登记的 `skew-start` / `skew-stop` 双向切换。
模式边界停稳各核，结算旧预算，同步 flush 旧 TB/plugin 回调，再以同一次
冻结 elapsed 采样的最终 visible 值重锚；不把未发布的领先尾部补入共享时间。
正常 getter 未添加读者计数、登记或切换等待对象。

v7 使用 `sysemu/` 头、iothread 锁 API 和 `CPUState.icount_decr_ptr`。
低 16 位用于预算，高位退出/中断通知保留。传统 icount 的长预算、轮转和
replay 顺序继续使用 v7 实现，skew 的预算不借用 icount 私有字段。
migration blocker、TCG source set、CPU flags 和 ARM Generic Timer trace
均按 v7 接口接入，系统/用户态模拟都编译通过。

实测额外修复两个 v7 差异：

1. `include/exec/gen-icount.h` 的 `icount_start_insn` 原为共享静态指针。
   skew 让 MTTCG 并发生成计数 TB，两个线程会相互覆盖占位指令指针，
   将另一 TCG context 的临时量写入当前 TB。core/GDB 显示当前 temps 起点
   为 `0x789c9c000f18`，损坏操作的临时量为另一 context 的
   `0x789ca4001ed8`，并在 `liveness_pass_1` 崩溃。现在指针为线程私有。
2. v7 在执行轮次末尾直接 `qemu_wait_io_event()`，因此在等待前再次
   `skew_cpu_idle()`，避免 WFI CPU 仍留在 active 集合。原冻结断言通过，
   未放宽时间容差或删掉全 idle 检查。

v7 插件 API 不提供 memory-value getter。性能 guest 用独立 start/done
store PC 标识两端，插件仍校验 UART 地址、写访问宽度、起始 CPU 和顺序；
不插桩计算/同步循环。delay 插件显式包含 GLib 头。
标准 AArch64 TCG Makefile 排除需要专用构建的 `skew-*` C 源，避免把宿主
插件和 Linux `/init` 当作通用裸机测试。QMP 冒烟测试明确验证未配置 skew
时三个新命令返回 `GenericError`，而不是把这些错误当作查询成功。

## 最终验收结果

最终聚合入口退出 0，10 组全部通过。证据目录：
`/home/andy/qemu-skew-v7.2.0/build/skew-v7-acceptance-20260929`。`metadata.json` 保存输入哈希；`summary.json` 保存命令、退出码和
耗时；`guest/results.json`、各 Linux `qmp.json` / `serial.log` 保存原始记录。

| 项目 | 实际结果 |
|---|---|
| 执行预算单测 | 4 项通过，覆盖禁用、续配、退出高位保留、reset |
| 动量采样锚点 | 真实生产函数定向测试通过；旧共享锚点变体按预期失败 |
| visible CAS | 强制 CAS retry、迟到发布、下界；8 发布者 / 800000 次调用通过 |
| visible 模式切换 | 4 发布者在边界停稳，1000 次转换；bias/插值/界限/CPU 状态通过 |
| 扫描工具单测 | 6 项通过，含排除 icount 后不伪造结果/比较值 |
| 完整裸机矩阵 | 48 组通过：精确计数、暂停/恢复、全 idle/UART、SMP、timer、参数扫描、延迟插件、压力、原生 icount 对照 |
| 性能工具冒烟 | 20 次通过：2/3 核 × total/phased/memory/idle/mixed × MTTCG/skew |
| Linux 模式切换 | 2 轮 × 运行中/暂停 × 4 转换，共 16 次通过；Linux 启动、timerfd、跨核时间、阶段预算/计数、暂停冻结及幂等性通过 |
| 持续 skew Linux jitterentropy | 初始化与 256 次 64-byte AF_ALG 读取通过，73 个时钟样本满足单调、bias/窗口、斜率界限 |
| 上游 unit / qapi-schema / qtest | 112 个注册测试：109 通过，3 环境跳过，0 失败；QMP 命令测试含 62 个子项 |
| 上游 check-tcg | AArch64 linux-user/system、5 个测试插件、gdbstub、memory record/replay 通过；12 个人工 console 项按上游规则跳过 |

裸机压力包括 100000 次 WFI、1000000 条消息及 2000000 次原子计数。
精确计数验证 64000002 / 128000002、MMIO/SVC 的 36 条以及极小窗口
36 / 226；100 次 stop/cont 和 250ms 暂停/全 idle 要求时钟与 raw 位相等。
trace 逐条验证 logical 换算、active 最小值、领先上限、warp 和定时器不早触发。
性能冒烟只用于工具和工作量校验，不据此宣称性能改善。

上游环境跳过项：
- `qemu:unit / test-io-channel-command`
- `qemu:qtest+qtest-aarch64 / qtest-aarch64/tpm-tis-device-swtpm-test`
- `qemu:qtest+qtest-aarch64 / qtest-aarch64/cdrom-test`

## 中止项和稳定性记录

按用户指示，原生 icount 长性能测试在反复超时后中止，最终性能矩阵只请求
MTTCG/skew。两核 mixed icount 曾两次在 120s 超时，诊断变体也未完成；
这些不计作通过。完整裸机矩阵中的两个短 icount 场景通过，上游 record/replay
也通过。扫描工具增加 `--skip-icount`，验收入口默认排除长 icount；需要时
显式 `--include-slow-icount`。报告明确标记“未请求”，不给缺失样本编造
加速比。已保留超时日志，不继续等待该长性能项。

首次持续 jitterentropy 运行在至少 192 次读取后出现内核
`Jitter RNG permanent health test failure`，AF_ALG 返回 EFAULT。
相同二进制、相同参数的单独复测和最终聚合验收均完成 256 次读取。
没有降低读取次数、绕过健康检测或修改 RNG/kernel。该失败说明本次不能
保证 jitterentropy 在每次宿主调度条件下都通过健康检测；这仍是已知稳定性
限制，初次失败的 serial/QMP 记录保留在
`build/skew-linux-acceptance-20260929/jitter/`。最终通过不抹去该失败。

早期构建与测试失败另有保存：GCC 10 对上游 `net/dump.c` 的警告、GLib
路径混用、线程共享计数占位指针崩溃、全 idle 漂移、delay 插件缺少 GLib
声明，以及新 QMP 命令未配置时的预期错误适配。修复后未放宽生产窗口
检查、精确计数或时钟冻结断言。原生 icount 长测试和 jitter 的健康检测
限制与上述已修复故障分别记录。

## 构建与复现

宿主为 Ubuntu 20.04 / WSL2，内核 `6.18.33.2-microsoft-standard-WSL2`，
12 个逻辑 CPU；GCC 10.5.0，AArch64 GCC 9.4.0，Python 3.9 构建环境，
Meson 1.9.0，GLib 2.72.4。QEMU system 与 linux-user 使用优化级别 2、
debug/assertions、plugins、log trace backend。唯一降级的构建警告为
上游 `net/dump.c` 的 `-Wno-error=stringop-overflow`，其余维持 `werror=true`。

本机 Python/Meson 与 GLib 安装在 `/home/andy/qemu-build-deps`。
使用 `PATH=/home/andy/qemu-build-deps/venv/bin:$PATH`、
`PKG_CONFIG_PATH=/home/andy/qemu-build-deps/glib/lib/pkgconfig` 和
`LD_LIBRARY_PATH=/home/andy/qemu-build-deps/glib/lib`。构建配置为：

```sh
mkdir build && cd build
../configure --target-list=aarch64-softmmu,aarch64-linux-user \
  --cc=gcc-10 --cxx=g++-10 --extra-cflags=-Wno-error=stringop-overflow \
  --disable-docs --disable-gtk --disable-sdl --disable-vnc \
  --enable-debug --enable-plugins --enable-trace-backends=log
meson configure -Doptimization=2 -Dwerror=true \
  -Dc_link_args=-Wl,--no-as-needed,/home/andy/qemu-build-deps/glib/lib/libgmodule-2.0.so,--as-needed \
  -Dcpp_link_args=-Wl,--no-as-needed,/home/andy/qemu-build-deps/glib/lib/libgmodule-2.0.so,--as-needed
ninja -j6 qemu-system-aarch64 qemu-aarch64 tests/unit/test-exec-budget
cd ..
```

显式 libgmodule 路径解决本机系统 GLib 与自建 GLib 的搜索顺序混用。
常规同版本 GLib 依赖环境不需要这个本机路径。上游子模块通过 GitHub SSH
取得；可按自身网络使用标准 submodule 初始化。

Linux 输入为 `vmlinuz-6.1.0-50-cloud-arm64`，配套 `af_alg.ko`、
`algif_rng.ko`、`jitterentropy_rng.ko`，SHA-256 在最终 metadata 中。
测试构造静态 `/init` 和 initramfs，不需磁盘镜像。

```sh
python3 tests/tcg/aarch64/system/skew-suite.py build/qemu-system-aarch64 \
  --build-dir build --output build/skew-v7-acceptance-new \
  --kernel /path/to/vmlinuz-6.1.0-50-cloud-arm64 \
  --module-dir /path/to/matching/kernel/crypto --linux-repeats 2
meson test -C build --suite unit --suite qtest --suite qapi-schema \
  --print-errorlogs --num-processes 4 --logbase upstream-acceptance
ninja -C build test-plugins
make -C build -j4 check-tcg HAVE_GDB_BIN=/path/to/gdb-multiarch
```

本机 `/usr/bin/gdb` 不识别 AArch64；使用 Ubuntu `gdb-multiarch` 9.2 的
局部解包版本完成 gdbstub 测试，没有跳过这些测试。
两核宿主运行时间测试依赖 `kernel.sched_schedstats=1`。本次将原值 0 临时
改为 1；验收结束后恢复为 0，脚本不自行修改宿主 sysctl。

测试时二进制版本为 `QEMU emulator version 7.2.0 (v7.2.0-dirty)`，表示在
本地提交前构建，不表示使用了 v10 基线。测试二进制 SHA-256：
`b71de9955342de67f337c4d1ad5178af4cb9eb2a74cb5659e65ecbd54dea57de`。最终提交之后未替换该已测试二进制。

## 范围

本次实机验证目标为 AArch64 system 与 linux-user。未声称其他 guest
架构、Windows 原生构建、KVM、迁移/快照或无上限压力均已通过。
skew 要求 MTTCG；与原生 icount/record-replay 同时启用被拒绝，迁移/快照
保持 blocker。getter 调用者须是 vCPU 执行上下文或 BQL 串行路径。
同步自 v10 的历史设计与性能报告已标记来源，不能当作此次 v7 实测。

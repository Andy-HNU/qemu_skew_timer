<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# QEMU v7.2.0：执行效率与自适应插值移植

## 基线和范围

目标分支为 `codex/skew-v7.2.0`，基线
`d4175fa590dc1ab6c408507493d6b369e472b3ab`。移植来源为 QEMU 10.2 分支
`codex/skew-asymptotic-reimplementation` 的
`20624291a725bde9a182643ecc7fda462ff8f368`，与本地 `f6472bc` 源码树相同。
本记录只列 v7.2.0 上实际执行的验证；10.2 的测试结果不计入。

| 改动 | v7.2.0 行为 |
|---|---|
| 16 位执行槽续充 | 在执行循环内结算并续充剩余窗口，保留退出通知高位 |
| 窗口等待移出 BQL | `tcg_cpus_exec` 返回后在进度锁下等待；kick 先发布退出请求再通知 |
| 发布策略 | `skew-publish=periodic/notify/inline`，默认 periodic |
| 新进度预算 | `skew-fresh-budget=on/off`，默认 off |
| ARM Counter 读路径 | `skew-counter-fast=on/off`，默认 off；仅放行 CNTPCT/CNTVCT 的只读回调 |
| 插值策略 | `skew-interp=linear/exponential/adaptive`，默认 linear |

基础执行优化随 skew 启用。可选优化和插值策略需要显式配置。
保留 v7 的 `icount_decr_ptr`、iothread 锁、migration blocker 接口，以及
轮次末尾 `qemu_wait_io_event()` 前的 idle 成员移除。TCG kick 接入点位于
`mttcg_kick_vcpu_thread()`；原生 icount 和用户态模拟保持 v7 接口。
v7 的 `cpu_handle_interrupt()` 原本在返回 MTTCG 前清除退出请求。skew
模式保留该标志到外层事件循环，确保续跑路径识别暂停、IRQ 和其他 kick。

自适应曲线沿用 `tau=max(R/s, H/ln(5))`，H 为请求更新间隔、最近 8 个
有效模型推进间隔、当前等待年龄的最大值。在重锚阶段限速，使 H 末至少
保留本段起始余量的 20%。不按读取次数补 tick，不修改 Guest 的健康检查。

7.2 插件缺少 MMIO 写值读取 API，新增复现场景使用已有的独立 start/done
store 地址，检查访问地址、32 位宽度、CPU 和状态顺序。保留原有性能扫描
的 `--skip-icount` 和独立 `skew-suite.py` 入口。

## 验证计划

- 构建 AArch64 system、AArch64 linux-user 和执行预算单测。
- 运行指数数值、有效发布间隔估计、动量锚点、共享 CAS 和模式切换夹具。
- 裸机默认配置、adaptive、adaptive+inline/fresh/counter-fast、
  adaptive+notify/fresh/counter-fast 各运行 quick；每组 15 个顶层结果。
  检查 1/65535/65536/200000 条窗口、暂停、唤醒、TLBI、定时器和精确计数。
- 可选联合配置追加 Counter offset、慢核及运行中/暂停切换回归；
  Linux 执行运行中/暂停的双向模式切换，并检查 32 vCPU 的计算和切换。
- 自适应 jitter：200M、500M、2G IPS，100 µs 请求发布间隔，各 3 次；
  2G/1 ms 另做 3 次。每次为全新 VM，1024 次 64 字节 AF_ALG 读取，
  使用同一 Debian 6.1.176 ARM64 内核和模块，2 vCPU、512 MiB、1 ms 窗口。
  固定 periodic 发布、fresh-budget=off、counter-fast=off。
- 性能工具只执行有界功能冒烟，验证新插件和复现脚本在 v7 可运行，
  不把少量样本用作版本间性能结论。

运行目录为 `build/backport-20261020/`。各入口有宿主截止时间，失败记录
保留；性能/jitter 样本串行运行。环境中线程 schedstat 不可见，显式使用
`--no-host-runtime`；功能断言继续执行，不报告由该项测得的执行占比。

## 配置

沿用现有启动参数并加 `skew-interp=adaptive`，即可使用新插值：

```sh
-accel tcg,thread=multi,skew=1000000,skew-ips=200000000,skew-update=100000,skew-interp=adaptive
```

可选联合优化参数为：

```sh
skew-publish=inline,skew-fresh-budget=on,skew-counter-fast=on
```

此组单独执行功能回归；上面的 jitter 矩阵不启用这些可选项。

## 验证结果

### 功能与兼容性

| 检查 | 实际结果 |
|---|---|
| AArch64 system 构建 | 通过，版本输出 7.2.0 |
| 执行预算单测 | 4/4，通过；覆盖高 16 位通知保持 |
| 指数数值检查 | 2684 组旧指数、2700 组 adaptive 通过；包含 450 组 H 末余量检查 |
| 动量与有效发布间隔 | 独立采样锚点、空轮询年龄、历史过期、暂停和阶段重置通过 |
| CAS 共享发布 | 强制 CAS 交错及 8 发布者共 800000 次调用通过 |
| 停稳切换夹具 | linear/exponential/adaptive 各 1000 次、4 发布者通过 |
| 四组裸机 quick | 各 15 个结果，共 60 个；每组含 13 个非法配置拒绝 |
| inline 与 notify 扩展回归 | 各 5 个结果；含 CNTVOFF、分别减慢两个核、运行中/暂停各 20 次切换 |
| Linux adaptive 切换 | running/paused 两组，各 4 阶段，通过 |
| 32 vCPU Linux | 32 核 online、完整校验、休眠唤醒、暂停与双向切换通过 |
| 原生 icount | 精确计数及定时器两个真实 Guest 场景通过 |
| 发布复现与优化实验工具 | 4 vCPU、小工作量的 12 次执行通过，含 6 次独立诊断 |
| FIFO 与性能统计工具 | FIFO 自检及 6 项统计单测通过 |

32 vCPU 检查采用默认线性插值、1G IPS，完成 10400000 个计算单位，
其中计算主体为 332800000 条 Guest 指令。其结果只用于多核功能回归。
性能工具冒烟使用相同二进制的 periodic-stale、notify-fresh、inline-counter，
各运行 compute/counter 场景；工作量 65536/核，不据此作性能收益结论。

7.2 适配额外修复了退出请求生命周期：未经适配的续跑路径会漏掉 `stop`，
初次 quick 在完成前 8 个结果后超时。保留请求至外层 MTTCG 事件循环后，
上述四组 quick 的暂停、唤醒、定时器检查全部通过。原失败日志保留在
`quick-default.log`，修复结果为 `quick-default-fixed/`。
同时保留 `skew-defer requires...` 的原错误提示，Linux 控制接口检查通过。

完整 AArch64 linux-user 构建在 v7 原有 `linux-user/syscall.c` 被宿主
glibc 2.39/Linux 6.8 头文件的 `struct file_clone_range` 重复定义阻塞；
未修改该无关路径，也不计作完整构建通过。本次改动涉及的用户态
`cpu-exec.c`、`tcg-all.c`、ARM `helper.c/op_helper.c` 对象均编译通过。

Python 编译及 `git diff --check` 通过。v7 `checkpatch.pl --no-signoff`
报告 14 条风格错误、9 条警告，未记为通过：11 条错误为中文注释按 UTF-8
字节计长，2 条为独立插件使用 libc `strtoull`，1 条为裸机源中的汇编函数
外部声明。独立插件没有链接 QEMU 的 `qemu_strtoull`，输入仍检查范围、
尾随字符和两个地址是否相同；完整输出保留在 `checkpatch.log`。

### jitter 冷启动

12 次预定运行全部完成，均通过初始化、1024 次读取和 QEMU 正常退出，
没有 Guest 健康失败或宿主超时。每份原始串口与结果 JSON 的内容、长度和
SHA256 已逐一核对，读取进度均完整到达 1024。

| IPS | 请求更新间隔 | 通过 | wall 中位数（s） | wall 范围（s） | CPU 中位数（s） |
|---|---|---:|---:|---|---:|
| 200M | 100 µs | 3/3 | 19.076 | 18.972–19.326 | 21.179 |
| 500M | 100 µs | 3/3 | 16.299 | 16.206–16.650 | 19.192 |
| 2G | 100 µs | 3/3 | 16.610 | 15.699–16.894 | 19.455 |
| 2G | 1 ms | 3/3 | 14.292 | 14.071–14.384 | 14.852 |

wall 覆盖 QEMU 启动到退出，CPU 为累计 user+system 时间；这些数值不表示
Guest 指令执行占比。本次验收使用 adaptive 和已合入的两项基础性能优化，
固定 periodic/fresh-budget=off/counter-fast=off。本次没有重跑 v7 旧版本的
配对性能基线，不能据此宣称相对旧版本或 QEMU 10.2 的加速比例。

### 复现命令

```sh
export TMPDIR="$PWD/build/backport-tmp"
mkdir -p "$TMPDIR"
python3 tests/tcg/aarch64/system/skew-check.py \
  build/qemu/qemu-system-aarch64 --quick --no-host-runtime \
  --skew-options skew-interp=adaptive --output build/v7-adaptive-quick-new

python3 tests/tcg/aarch64/system/skew-jitter-study.py \
  --qemu build/qemu/qemu-system-aarch64 \
  --kernel /path/to/vmlinuz-6.1.0-50-cloud-arm64 \
  --module-dir /path/to/matching/kernel/crypto \
  --output build/v7-jitter-nominal-new \
  --ips 200000000 500000000 2000000000 \
  --variants adaptive --profiles nominal --rounds 3 \
  --reads 1024 --timeout 120 --suite-timeout 1500
```

延长发布间隔使用另一个新目录，改为 `--ips 2000000000 --profiles delayed
--suite-timeout 450`，其余参数相同。需要检查可选联合路径时，把 quick 的
`--skew-options` 改为 `skew-interp=adaptive,skew-publish=inline,
skew-fresh-budget=on,skew-counter-fast=on`（拼为一个无换行参数），notify 同理。
Guest 的连续取熵没有开启 kprobe 健康探针，也没有持续 QMP 采样或 trace。

### 环境与证据

宿主为 x86_64，GCC 13.3、Meson 1.9.0、Ninja 1.11.1。本轮 system 构建
使用 `--enable-plugins --enable-trace-backends=log --disable-docs
--disable-werror --disable-gtk --disable-sdl --disable-vnc`。旧 QEMU 与新
宿主编译器产生的原有警告保留在构建日志，未修改无关源码。
Guest 为 ARM64 virt/cortex-a57；jitter 使用 GICv3、2 vCPU、512 MiB。
宿主可用 CPU 和 cgroup 配额记录在各矩阵 `environment.json`。

最终测试二进制 SHA256：
`0a16ae6fff76308b8f34086220df060928a7521e64705b7194fd458f084497ae`。
Guest 内核 SHA256：
`0c108326902b8cb9161796c4759fcfe6f182fa50d5cb569a70267979da7fef2c`。
四组 quick 和两组扩展回归使用的二进制为
`8d00fc33021ee49476a466cf03bebd4d05b43222b584102a2e756147aa6b708c`；
之后仅恢复非法配置的原错误提示，最终二进制重新通过 13 项非法参数、
Linux 控制接口、32 核切换、全部 jitter 和原生 icount/工具冒烟检查。

原始记录目录为 `build/backport-20261020/`，包含源码补丁、版本与输入哈希、
每次实际命令、串口、QMP、trace、功能结果和保留的失败日志；独立证据归档为
`skew_v7_2_backport_evidence_2026-10-20.zip`。原 v10 工作区、旧二进制和历史
测试结果均保留。

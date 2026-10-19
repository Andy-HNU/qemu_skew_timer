<!--
SPDX-License-Identifier: GPL-2.0-or-later
-->
# Skew 发布与执行路径瓶颈研究

研究起点：本地提交 `ed6fd11` 的固定工作量复现；运行时基线为 `760b52a`（P2）。
本研究不恢复或替代未取得源码的指数插值版本。

## 约束与假设

固定每核 2097152 次计算循环、4 vCPU、1 ms 窗口、200M IPS 和 100 µs 周期。
计数器场景每块读取 64 次 CNTVCT_EL0；结束条件始终是固定计算量和结果校验。
保留已完成指令计数、active 最小进度、真实 idle、单调可见时钟和模式切换。

| 路径 | 已有依据 | 待验证的对策 |
|---|---|---|
| 已有共同进度等待周期回调 | 复现 trace 中所有活跃核已到窗口边界 | 最后一个边界等待者发起按需发布 |
| 主线程调度与 BQL 获取 | 周期回调在 BQL 下运行 | 比较通知主线程与当前 vCPU 在 BQL 下直接发布 |
| 续配使用旧 global 快照 | `skew_budget_exhausted` 沿旧 grant 续配 | 以 acquire 读取最新已发布 global，在执行循环内继续 |
| 发布之后仍等待线程返回 | `skew_wait waiting=0` 晚于发布 | 独立测量调度和进度锁重新获取的累计时间 |
| 计数器读取获取 BQL | ARM_CP_IO 的 get_cp_reg64；固定 524288 次访问 | 审查纯计数器读的线程安全边界，再决定能否缩小锁范围 |
| TB 预算检查与全局扫描 | CF_USE_ICOUNT、16 位执行槽、每次扫描 active 集合 | 先量化剩余成本，避免只扩大计数槽或窗口 |

## 候选设计

保留原有周期定时器作为存活、idle warp 和设备时钟更新路径。增加两个默认关闭
的实验选项，允许同一二进制做交叉对照：

- `skew-publish=periodic|notify|inline`：默认 periodic。notify 在所有 active
  核均等待当前边界时提交可合并 BH；inline 由最后一个等待者释放进度锁，再按
  BQL → progress_lock 顺序执行原有完整协调器，随后重新检查等待条件。
- `skew-fresh-budget=on|off`：默认 off。on 时预算片段耗尽并结算后读取最新
  global。新 global 在时钟元组提交之后以 release 发布；旧 grant 的完成量
  仍先按原约束校验，下一预算按新 grant 校验。

等待者按 global 代际计数，避免把已经获得额度、尚未返回 cond_wait 的线程
误判为当前边界的阻塞者。模式切换在全核停止后重置代际与选举状态。
inline 保留 BQL 的设备及可见时钟写者语义，不是“协调器已脱离 BQL”的实现。

第一阶段完成后追加 `skew-counter-fast=on|off`（默认 off）：仅 Skew 开启时，
`get_cp_reg64` 对两个纯读取函数 `gt_cnt_read` / `gt_virt_cnt_read` 放行；
仍使用原有读函数计算权限检查后的物理/虚拟计数与 CNTVOFF。保留 ARM_CP_IO
以及翻译器的 I/O 开始、TB 结束和执行计数边界。TVAL/CTL/CVAL、写操作、
普通 MTTCG 和 user-only 均保留原有路径。此项依赖现有时钟 seqlock/CAS 读者、
本核拥有的架构状态，以及模式切换先停稳 vCPU 的约束。

## 验证矩阵与指标

原始 MTTCG、原始 P2、候选 periodic/stale，以及发布方式 × 新旧预算快照的
组合。两种负载各运行三轮；第二轮反转顺序，第三轮轮换起点。正式性能运行
不开 trace/QSP，也不在计时区间轮询 QMP。随后对 periodic/stale、notify/fresh、
inline/fresh 各做两种负载的诊断。

每次最多 30 秒，整套最多 240 秒；输出路径必须全新。构建和 Guest 运行分开。
保存每次的参数、二进制/Guest 哈希、宿主亲和性、cgroup 状态、全部失败及超时。

性能指标：固定工作墙钟、进程用户态/内核态 CPU 秒、平均宿主核数、有效计算
MIPS、上下文切换。诊断补充发布请求到发布、实际发布间隔、窗口等待中发布前与
发布后两部分、预算续配和执行返回次数。CPU 秒和线程等待均不等同于 Guest
指令执行占比；发布后返回延迟包含宿主调度及互斥锁获取。
inline 的窗口阻塞区间还包含最后一个核执行协调器的时间，并非全程条件变量睡眠。

功能检查覆盖精确计数、1/65535/65536/200000 指令窗口、真实 Guest 定时器、
空闲退出/加入、暂停恢复、慢核和双向模式切换。缺少匹配内核/模块时，不宣称
Linux 启动或 jitter 已验证。

## 入口

```sh
python3 tests/tcg/aarch64/system/skew-bottleneck-study.py \
  --qemu build/qemu/qemu-system-aarch64 \
  --baseline build/bottleneck-20261019/baseline/qemu-system-aarch64 \
  --output build/bottleneck-20261019/factorial \
  --rounds 3 --timeout 30 --suite-timeout 240 \
  --variants native original periodic-stale periodic-fresh notify-stale \
             notify-fresh inline-stale inline-fresh
```

原始结果及最终判定另附于验证记录；本计划不预设组合一定优于单项。

第二阶段固定六个候选：periodic/stale、只改计数器、notify/stale、notify +
计数器、inline/fresh、inline/fresh + 计数器。每个候选两种负载、三轮，
36 次性能运行；另对基线和三个含计数器优化的组合做 8 次诊断。保持相同
窗口、指令率、工作量和 240 秒总上限。阶段一、二使用各自同一二进制内部
对照；不把两个阶段的中位数直接相除作为加速比。

```sh
python3 tests/tcg/aarch64/system/skew-bottleneck-study.py \
  --qemu build/qemu/qemu-system-aarch64 \
  --baseline build/bottleneck-20261019/baseline/qemu-system-aarch64 \
  --output build/bottleneck-20261019/counter-factorial \
  --rounds 3 --timeout 30 --suite-timeout 240 \
  --variants periodic-stale periodic-counter notify-stale notify-counter \
             inline-fresh inline-counter \
  --diagnostic-variants periodic-stale periodic-counter notify-counter inline-counter
```

普通 MTTCG 未应用计数器读优化；若组合超过该基线，只能证明这组实现变更
在此场景有效，不能据此宣称 Skew 同步机制天生更快。

补充规模检查固定 32 vCPU、每核 524288 次循环，比较 periodic/stale、计数器
单项、inline/fresh + 计数器，三轮共 18 次性能运行。每次上限 20 秒，整套
上限 180 秒。宿主 CPU quota 为 8，属于超额订阅场景，不能直接外推到 32 个
独占宿主核。

真实 Guest 回归入口：

```sh
python3 tests/tcg/aarch64/system/skew-check.py build/qemu/qemu-system-aarch64 \
  --quick --no-host-runtime --output build/skew-candidate-quick \
  --skew-options skew-publish=inline,skew-fresh-budget=on,skew-counter-fast=on
python3 tests/tcg/aarch64/system/skew-bottleneck-check.py \
  build/qemu/qemu-system-aarch64 --output build/skew-candidate-extended \
  --skew-options skew-publish=inline,skew-fresh-budget=on,skew-counter-fast=on \
  --suite-timeout 90
```

扩展入口复用已有真实 Guest、QMP 和 FIFO 实现，检查 CNTVOFF、分别拖慢两核、
运行中及暂停时各 20 次模式转换。`--checks offset|slow|switch` 可选择尚未完成的
检查；失败输出不复用。慢核插件显式链接 GLib，避免依赖 QEMU 导出其内部符号。

## 参考

QEMU 官方 [MTTCG 设计说明](https://www.qemu.org/docs/master/devel/multi-thread-tcg.html)
说明 vCPU 的独立执行、热路径避免锁，以及 ARM_CP_IO 寄存器访问的 BQL 保护。
本次并发判断以固定提交的实际调用链为准。

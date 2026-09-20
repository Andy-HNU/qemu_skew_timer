# 实现核对（2026-09-20）

初次核对 commit: 4e894601b887007c180d5d28ec12afbba05c0965；IPS 行补充了后续修复。

| 任务设想 | 当前源码事实 / 论文处理 |
|---|---|
| QEMU 7.2.0 | VERSION 为 10.2.0，论文按此版本写 |
| 64位 IPS 配置完整生效 | exp5 发现 muldiv64 的32位参数截断；后续将四处换算改为64位参数、128位中间结果，复测见 POST_FIX_zh.md |
| model-only timer budget | CPU budget 来自 W-lead；设备 QEMU_CLOCK_VIRTUAL 走共享 visible 时钟，不能写成独立严格 model 定时器域 |
| 读时命中 bound 立即改 slope | skew_visible_clock 只 clamp + 原子 max；不修改 slope，作为未实现扩展 |
| 固定 Delta T | 更新器可能延迟，动量以实际 cpu_get_clock 间隔归一化 |
| 纯历史趋势斜率 | 8 样本 beta=3/4，再反馈、截断；active 有 1/256 floor |
| 反馈为 ratio*bias/Delta T | 实现先将 abs(bias)/Delta T 截到 1，再乘 0.01；不是无界线性反馈 |
| elapsed anchor 等于统计 anchor | prepare 从 idle 恢复也可能重置同一个 anchor，审计动量采样时要计入此行为 |
| 所有 inactive local >= G | 不成立；下界只针对已 rebase 的 active CPU；idle 历史映射可能落后 |
| WFI/WFE 都移出集合 | 由 cpu_thread_is_idle/stop/runstate 判断；WFE 不能仅凭指令名推断 |
| shared monotonic 的形式化证明 | 目前是设计目标与已有有限测试；需要审计读者 CAS 和协调器 atomic set 并发交错，不能用论文替代证明 |
| clock 完全 host-independent | model 的指令换算不直接取 host elapsed，但活动集合/程序路径可变；visible 显式使用宿主间隔 |
| hardware-calibrated | 暂无提供的硬件配对测量，作为方法与待验证假设 |
| smooth clock | 分段线性、整数计数器量化；可饱和、可向前重锚，不能保证严格连续或每次递增 |
| icount weak MTTCG | 上游 icount 与 MTTCG 不兼容；仍能模拟多核，但不是并行 vCPU 执行 |
| jitter init/use 通过即好熵源 | 只能说明特定内核健康检查与读取可用，不能证明密码学熵质量 |

## Source map

- accel/tcg/skew.c: skew_update, skew_visible_clock, skew_momentum_add,
  skew_feedback_slope, skew_cpu_prepare/account/idle/wait, skew_switch.
- include/exec/exec-budget.h: shared budget storage and exhaustion.
- accel/tcg/cpu-exec.c: cpu_loop_exec_tb, exit aggregation.
- accel/tcg/tcg-accel-ops-mttcg.c: mttcg_cpu_thread_fn.
- accel/tcg/tcg-accel-ops-icount.c: independent icount budget adapter.
- target/arm/helper.c: generic timer counter conversion from QEMU_CLOCK_VIRTUAL.
- docs/devel/tcg-icount.rst, docs/devel/multi-thread-tcg.rst: upstream design.
- qapi/run-state.json: query-skew-clock fields.

## Comparison matrix (qualified)

| Property | MTTCG | fixed-scale host clock (proposed) | fixed-shift icount | SkewKeeper |
|---|---|---|---|---|
| Clock progress basis | pause-aware host clock | scaled host clock | executed instructions | minimum active instruction progress + idle warp |
| Parallel vCPU execution | yes | inherits MTTCG if implemented | no MTTCG | yes |
| Host-speed dependence | direct clock dependence | remains | no direct elapsed scaling; scheduling/I/O caveats | model largely decoupled; visible interpolation depends on host interval |
| CPU skew policy | no explicit instruction window | same | serial scheduling, not this W bound | explicit active lead window |
| Visible clock | host driven, quantized | scaled, quantized | instruction increments | bounded interpolation, may plateau |
| Hardware calibration | not instruction calibrated | fixed scale only | fixed power-of-two ns/instruction for shift | arbitrary configured IPS; calibration pending |

Adaptive icount shift=auto is not interchangeable with fixed shift in the table.
No synthetic results support any cell.

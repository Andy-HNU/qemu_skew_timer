# 执行预算与时间模型的边界

公共 TB 退出机制位于 `cpu-exec.c`；预算协议位于
`include/exec/exec-budget.h`。CPU 执行路径不选择时间模型，模型只在
`tcg_cpu_init_cflags()` 中装配一次，普通无计时模式没有预算回调表。

## 职责

| 阶段 | 公共层 | icount | skew |
| --- | --- | --- | --- |
| 预算生成 | 提供 16 位额度的装载/读取接口 | `icount_prepare_for_run()` 按定时器/replay 与轮转配额生成长预算 | `skew_cpu_prepare()` 按剩余滑窗和 TCG 16 位容量生成本轮额度 |
| 是否耗尽 | 非计数 TB 例外由 CPU 循环处理，再调用 `exhausted` | 公共额度加私有 `icount_extra` 为零 | 公共额度为零 |
| TB 到期 | 调用 `expired`，必要时按返回额度截短 TB | 先 `icount_update()`，再从剩余长预算续配 | 保留剩余额度，不发新窗口、不推进全局时间 |
| 执行后结算 | 保留现有 RR/MTTCG 调度边界 | `icount_process_data()` 更新指令时间和 replay | `skew_cpu_account()` 发布完成量；协调器推进 global time |
| 耗尽后的调度 | 统一返回执行循环外层 | 原有 RR/icount 调度 | 原有 MTTCG 结算与滑窗等待 |
| CPU 执行内复位 | 调用可选 `before_reset` | 无新增操作，保留原有行为 | 清除执行状态前结算本轮 raw |

## 状态归属

- `icount_budget`、`icount_extra` 仅供 icount 使用。
- `skew_budget` 是 skew 私有的本轮发放额度，与 raw/base/global/warp 状态一起由 skew 管理。
- 公共层只持有回调表和 TCG 低 16 位执行额度，不保存虚拟时间，不获取窗口锁，不等待线程。
- 高 16 位仍保留原有异步退出通知语义，装载或退还低位额度不能清除它。
- `CF_USE_ICOUNT` 和 `neg.icount_decr` 是既有 TCG 插桩/布局名称，本次保留布局兼容；skew 通过中性接口访问，不再直接依赖这些字段或传统 icount 的内部状态。
- `tb->icount` 表示 TB 的指令数量，不属于某个时间模型的时钟状态。

## 为什么保留两种模型的调度入口

icount 的 RR/replay 预算生成与 skew 的 BQL 成员重定位处于不同锁和调度上下文。
因此不强行合并 prepare/account 成同一个带大量可选参数的入口；共享的仅是
TB 执行层需要的预算事件协议。未来模型实现自己的预算生命周期及回调表，
并在装配层注册，不需要修改 `cpu_loop_exec_tb()`。

## 验证范围

新增 `test-exec-budget` 检查无模型、模型私有扩展预算、到期续配、高位退出
通知保持和可选复位回调。完整 skew 验收覆盖精确计数、异常/MMIO 回退、极小
窗口、慢核、压力测试及传统 icount 对照；上游选定回归检查定时器、AIO、QMP
与 ARM 启动。此重构不新增时间策略，也不声明真实设备业务或生产性能验收。

## 本机验证结果

WSL2 Ubuntu 20.04，GCC 10.5，AArch64 GCC 9.4，QEMU `-O2`、调试断言、
`-Werror`、log trace 与插件开启：

- `qemu-system-aarch64` 编译通过。
- 完整 `skew-check.py`：47 组通过，含普通 MTTCG 和原生 icount 对照。
- 预算接口单测 4 项通过；与选定上游回归合计 11 组、645 个子测试通过。
- 结构检查通过：`cpu_loop_exec_tb()` 不访问模型私有预算、不调用 icount 更新；
  icount 实现没有 skew 分支；skew 不直接访问 icount 私有预算或底层递减器。

本次本机日志：`build/exec-budget-final-build.log`、
`build/exec-budget-acceptance.log`、`build/exec-budget-acceptance/results.json`、
`build/exec-budget-regression.log`。完整 trace/Guest ELF 在验收输出目录中保留。

## 删除 quantum 后的验证

每轮额度改为 `min(UINT16_MAX, window - lead)`，只受剩余窗口和 TCG 容量限制。
`skew-update` 保留 1..1,000,000,000ns 范围检查，但不再要求对应至少一条指令。
新增回归使用 IPS=100,000,000、window=100ns、update=1ns：轮询间隔仅相当于
0.1 条指令，仍成功启动并通过 36/226/36 条精确计数及暂停冻结检查。

重建通过；完整验收增至 48 组，全部通过；预算单测与选定上游回归共 11 组、
645 个子测试全部通过。日志保存在 `build/no-quantum-build.log`、
`build/no-quantum-acceptance/`、`build/no-quantum-acceptance.log` 和
`build/no-quantum-regression.log`。

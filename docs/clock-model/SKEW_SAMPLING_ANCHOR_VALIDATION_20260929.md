# 插值与动量采样锚点拆分（2026-09-29）

> 此文保留 v10.2.0 原方案历史记录。v7.2.0 本次结果见 [移植验收报告](BACKPORT_V7_2_VALIDATION_zh.md)。


修改起点：`a70a3791161a51ca3cb3ef4dbc5e6e000e37038e`。

`visible_anchor_elapsed_ns` 继续用于插值预测，prepare 提升活动斜率时可以重建。
新增 `sample_anchor_elapsed_ns`，与 `prev_global_icount` 在协调采样和阶段初始化
时配对更新。阶段初始化只读一次 `cpu_get_clock()`，同时初始化两个时间锚点。
保持 CAS-max、执行预算和现有模式切换协议。

## 定向验证

`python3 tests/tcg/aarch64/system/skew-momentum-anchor.py` 通过。
测试提取实际 reset、prepare、update 和动量函数，使用 QEMU atomic.h、可控 VM
时钟和双 CPU 状态。定时器/seqlock 接口为单线程桩，不用于证明并发或设备行为。

- 8 ms 时恢复，10 ms 时采样 400 条指令（4 ms 模型进度），样本为 0.4。
- 同一采样区间内多次恢复，不移动统计时间或指令基线。
- 普通采样、暂停不采样、新阶段清空历史并重建两种锚点。
- 仅在测试变体中恢复 prepare 修改统计时间起点的旧行为，0.4 断言失败；
  测试确认能够检测该缺陷。编译和运行启用 UBSan。

## 集成验证与失败记录

- AArch64 QEMU 构建通过：`build/skew-sampling-anchor-build.log`。
- 现有 CAS 回归通过：强制 CAS 重试、8 个发布线程共 800,000 次调用。
- 现有 quick 验收 11 个场景通过：`build/skew-sampling-anchor-quick-20260929/`。
- 运行中 Linux 往返两次均通过，每次四次模式转换：
  `build/skew-sampling-anchor-roundtrip-20260929/running/` 和
  `build/skew-sampling-anchor-roundtrip-retry-20260929/running/`。
- 上述两次暂停用例在首个 skew 阶段的 30 ms 采样点读到一核 raw=0，
  触发测试原有 `raw-icount > 0` 断言。失败目录和日志保留。
  raw 仅表示已结算进度，不能根据这一快照证明该核没有执行。
- 未修改测试断言或等待间隔，单独调用原脚本的暂停用例复跑通过四次转换：
  `build/skew-sampling-anchor-paused-only-20260929/`。guest 的跨核单调性、
  连续读取、休眠和跨切换 timer 检查均通过。有限复跑不保证 30 ms 断言稳定。
- 修改前 HEAD 的 skew.c 独立编译/链接为对照二进制，未覆盖源码或修复二进制；
  原脚本的运行中和暂停用例均通过：
  `build/skew-sampling-anchor-baseline-roundtrip-run-20260929/`。
- 探索性地将采样等待改为 200 ms 的外部脚本在运行中 phase 2 遇到 QMP
  BrokenPipe，未用于验收；未更改仓库测试脚本，日志保留于
  `build/skew-sampling-anchor-roundtrip-200ms.log`。

本次未复测 Linux jitter 的持续 AF_ALG 读取，不将切换 guest 的初始化检查
等同于完整 jitter 使用验收。

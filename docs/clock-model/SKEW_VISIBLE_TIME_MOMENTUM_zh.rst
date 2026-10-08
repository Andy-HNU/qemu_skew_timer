持续 skew 的可见时间动量插值
================================

目标
----

skew 的严格模型时间只在协调器汇总 ``global_icount`` 时更新。软件若在两次
``skew_update()`` 之间连续读取 ``CNTVCT_EL0``，原实现会重复得到同一个值。
Linux ``jitterentropy_rng`` 会把这种长时间不变视为计时源失效。

本实现保留严格模型时间，同时提供一个全系统共享、连续插值的可见时间。QMP
模式切换仍然保留，但正常启动和 jitter 使用不需要切换，skew 可以始终开启。

两个时间量
----------

``model_ns``
  严格 skew 时间。它仍由 ``global_icount / skew-ips`` 与全空闲时的定时器跳时
  构成，是执行进度和定时器语义的基准。

``visible_ns``
  CPU 和设备通过 ``skew_get_clock()`` 读取的共享时间。它在两个协调点之间按
  预测斜率推进，并始终受 ``model_ns ± skew`` 限制。

所有 vCPU 访问同一个原子 ``visible_ns``。读者和协调器在同阶段统一使用
CAS-max 发布高水位；seqlock 保护锚点元组，不能单独保护读后覆盖。
当前 ARM 计数器 helper 还持有 BQL，通用 getter 本身不要求 BQL。
详细同步边界见 `Skew 代码 Wiki <SKEW_CODE_WIKI_zh.md>`_ 第 7 节。
更新只允许取更大的值，因此跨 CPU
读取不会因切换执行线程而倒退，也没有每 CPU 私有时间。

协调周期
--------

``skew_update()`` 每次完成以下处理：

1. 扫描活动 CPU，按既有规则更新 ``global_icount``。
2. 计算新的严格 ``model_ns``，全空闲时仍按最近虚拟定时器 deadline 跳时。
3. 用上一周期的斜率结算当前 ``visible_ns``。
4. 用本周期真实的、可暂停宿主时间间隔和 ``global_icount`` 增量生成斜率样本。
5. 从最近 8 个样本计算动量，较旧样本按 ``beta = 3/4`` 衰减。
6. 根据 ``visible_ns - model_ns`` 做 1% 的负反馈，然后发布下一周期锚点和斜率。

实现使用无符号 Q32 定点斜率。单个样本先按该周期理论最大执行量
``skew-ips * delta_host_ns`` 限幅，所以斜率范围为 ``[0, 1]``。计算采用实际
协调间隔，而不是命令行 ``skew-update`` 的名义间隔；BQL 竞争或宿主调度延迟
不会因此制造错误的速率。

插值与采样使用独立时间锚点：``visible_anchor_elapsed_ns`` 与
``anchor_visible_ns`` 配对，允许 ``skew_cpu_prepare()`` 在提升斜率时重建；
``sample_anchor_elapsed_ns`` 与 ``prev_global_icount`` 配对，只在协调采样
和阶段初始化时一起更新。CPU 从 idle 恢复不会截短统计时间区间，也不会把
此前积累的指令进度误算为恢复后短时间内完成。初始化时两个时间锚点使用
同一次 ``cpu_get_clock()`` 采样。

例如每 100 条指令对应 1 ms 模型时间，0～10 ms 累计 400 条指令，即使 CPU
在 8 ms 恢复并重设插值锚点，下一次速度样本仍为 4/10=0.4，而不是使用
10-8=2 ms 作为分母后被限幅到 1.0。

定向回归通过可控 VM 时钟执行源码中的初始化、prepare 和协调更新函数::

    python3 tests/tcg/aarch64/system/skew-momentum-anchor.py

连续读取与边界
--------------

``skew_visible_clock()`` 按下式生成候选值::

    predicted = anchor_visible + host_elapsed * slope
    visible   = monotonic_clamp(predicted,
                                model_ns - window_ns,
                                model_ns + window_ns)

``window_ns`` 就是命令行 ``skew=NS``。它既限制多核执行进度差，也限制插值时间
相对严格模型时间的最大偏差。插值不会改变 ``global_icount``、CPU budget 或
window 调度，仅改变协调点之间对外显示的时间。

全 CPU 空闲时斜率为 0，时间只由已有 deadline warp 推进，避免 VM 空闲或暂停
期间按宿主时间漂移。CPU 从空闲重新加入活动集合时，``skew_cpu_prepare()`` 会
立即安装 ``1/256`` 的最小活动斜率，避免形成第一个统计样本前连续读取相同值；
硬 window 仍然限制它最多领先严格模型一个窗口。

接口与观测
----------

``skew_get_clock()``
  skew 模式下返回共享 ``visible_ns``；MTTCG 模式下保持原生可暂停时钟行为。

``skew_update()``
  更新严格模型、动量、反馈和下一段插值参数。

``skew_cpu_prepare()``
  处理 idle 到 active 的第一段插值，同时继续负责原有 CPU 逻辑进度重基准。

``query-skew-clock`` 新增以下字段：

``model-ns``
  当前严格模型时间。

``visible-bias-ns``
  ``virtual-ns - model-ns``，绝对值不得超过 ``window-ns``。

``visible-slope-q32``
  当前 Q32 插值斜率，范围为 0 到 ``2^32``。

Linux jitterentropy 验证
-----------------------

测试入口为 ``tests/tcg/aarch64/system/skew-linux-jitter.py``，guest ``/init`` 位于
同目录的 ``skew-linux-jitter.c``。测试从复位开始直接启用 skew，全程不调用
``skew-start`` 或 ``skew-stop``：

* 启动 Debian arm64 Linux；
* 加载 ``jitterentropy_rng.ko``、``af_alg.ko`` 和 ``algif_rng.ko``；
* 确认 jitterentropy 初始化成功；
* 通过 AF_ALG ``jitterentropy_rng`` 完成 256 次、每次 64 字节的实际读取；
* 周期查询 QMP，检查共享可见时间单调、斜率不超过 1、偏差不超过 window。

历史本机复跑记录见
`整理前文档 <https://github.com/Andy-HNU/qemu_skew_timer/blob/17d49d3d497ce9f2235c739acfedcca7e0a0e086/docs/clock-model/SKEW_VISIBLE_TIME_MOMENTUM_zh.rst>`_。
当前运行使用 ``README.md`` 和 ``TEST_ACCEPTANCE_zh.md`` 的命令，
分别报告持续 jitter 与双向切换结果，不用历史数字替代当前验收。

边界说明
--------

可见时间必须同时满足单调性和 ``model ± window``。如果活动 CPU 的严格模型进度
长时间完全停止，可见时间最终会到达窗口上界并暂停；任何有限窗口都无法在模型
永久停止时同时保证无限连续推进。本实现解决的是正常 TCG 执行和协调抖动下的
阶梯时间问题，不伪造无界的 guest 执行进度。

visible 场景的双向切换
----------------------

运行时仍使用 ``skew-start`` / ``skew-stop``。两个方向均以旧模式最终
visible 为新阶段起点；退出时结算协调器尚未保存的插值贡献，进入时清空历史。
model、visible、插值锚点、独立采样锚点按同一个冻结 elapsed 采样重建。
全 vCPU 停核后才改预算/CF_USE_ICOUNT，保留 CF_PARALLEL；旧 TB 清空后，
持有 BQL 完成旧 visible 结算和重锚，排除协调器和调试查询交错。
旧阶段 vCPU 发布已在停核前结束；正常 getter 不登记读者，继续使用原有 CAS-max。

状态与验证方法见 ``SKEW_VISIBLE_SWITCH_zh.md``。

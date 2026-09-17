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

所有 vCPU 访问同一个原子 ``visible_ns``。更新只允许取更大的值，因此跨 CPU
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

本机连续 5 次探索复跑及最终构建复跑均通过 jitter 初始化和 256 次读取。原有
skew 功能回归通过；运行态和暂停态下的 MTTCG→skew→MTTCG 往返测试也通过。

边界说明
--------

可见时间必须同时满足单调性和 ``model ± window``。如果活动 CPU 的严格模型进度
长时间完全停止，可见时间最终会到达窗口上界并暂停；任何有限窗口都无法在模型
永久停止时同时保证无限连续推进。本实现解决的是正常 TCG 执行和协调抖动下的
阶梯时间问题，不伪造无界的 guest 执行进度。

===========================
QEMU时钟重构探索
===========================

为了解决一个场景：多个基带板 + 1个主控板互相链接的时候，主控板会处理不过来包导致软件超时。之前有解决办法是 slowtime 放大 syscnt，使得软件读取的时间变慢来暂时规避。

该场景下，MPT 的 Host Core 运行 TCG 处理能力不足，同时 Guest 时钟使用 wall time 推进。TCG 在规定的 wall time 内无法完成足够的 Guest 指令，最终导致软件超时。slowtime 只能通过固定比例降低 Guest 时钟流速，无法根据实际 Guest 指令执行进度动态调整时间。

理想的方案是，当 BBU 发送完包之后，如果对应 Core 的指令执行进度已经领先其他 Core 达到规定范围，则限制该 Core 继续执行；处理端完成对应工作并推动系统整体执行进度之后，发送端继续运行，仿真时间继续推进。

跨 QEMU 场景还需要多个 QEMU 之间建立统一时间基准，可以考虑使用外部 Time Server 进行时钟协调。

一个可参考的实现是 QEMU icount 模式。该模式下，executed instruction count 用于驱动 ``QEMU_CLOCK_VIRTUAL``，ARM System Counter 再从该虚拟时间获得计数值。启用 icount 后，TCG 使用单线程 Round-Robin 方式执行多个 vCPU。

此时 Host TCG 的实际执行速度不会直接决定 Guest 时钟推进速度，因此 Host 执行缓慢不会直接转换成 Guest 软件超时。不过多 vCPU 失去了 MTTCG 的并行能力，整体仿真性能会明显下降。

因此希望在 MTTCG 场景下建立一种使用 icount 标定 syscnt 的时间方式，使 Guest 时间与 Host wall time 解耦，同时保留 MTTCG 多线程执行能力。

为了方便讨论，将问题简化为同 QEMU 场景下的双核收发包。

.. image:: 2026-09-03-16-14-36.png

图：wall time 场景下同 QEMU 双核收发包超时场景示意。


slowtime
--------

早期解决方案是降低整体 syscnt 时钟流速。

如果 Core1 在 Host 上需要更长时间完成处理，可以在 syscnt 计算中加入 slowtime 倍数，使 Guest 看到的时间流速按照固定比例下降。

.. image:: 2026-09-03-16-14-49.png


软件时间获取接口
~~~~~~~~~~~~~~~~

在 aarch64-system 模式下，Guest 软件通过 ARM 架构定义的 Generic Timer 系统寄存器获取时间。QEMU 在 ``target/arm/helper.c:3068`` 注册了这些寄存器：

- ``CNTPCT_EL0``：物理计数器，EL0 读取，读函数 ``gt_cnt_read``，返回 ``gt_get_countervalue(env)``
- ``CNTVCT_EL0``：虚拟计数器，读函数 ``gt_virt_cnt_read``，返回 ``gt_get_countervalue(env) - gt_virt_cnt_offset(env)``，其中 offset 来自 ``CNTVOFF_EL2``
- ``CNTFRQ_EL0``：频率寄存器，存储在 ``cp15.c14_cntfrq``，对应 ``gt_cntfrq_hz``

本文所说的 ``syscnt`` 指 ``CNTPCT_EL0 / CNTVCT_EL0`` 所基于的 System Counter 计数值，QEMU 内部核心函数为 ``gt_get_countervalue()``。


syscnt 的计算过程
^^^^^^^^^^^^^^^^^

核心函数在 ``target/arm/helper.c:2503``：

.. code-block:: c

    uint64_t gt_get_countervalue(CPUARMState *env)
    {
        ARMCPU *cpu = env_archcpu(env);
        return ((qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) /
                 gt_cntfrq_period_ns(cpu))
                + cpu->slowtimes_comp_ticks);
    }


完整计算链路
^^^^^^^^^^^^

1. ``qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)``

   获取 QEMU 虚拟时钟的纳秒值。

   在当前非 icount 路径下，``QEMU_CLOCK_VIRTUAL`` 最终来源于 Host 单调时钟，并在 VM 暂停时停止累计。

2. ``gt_cntfrq_period_ns(cpu)``

   计算一个 System Counter tick 对应的时间：

   .. code-block:: c

       tick_period =
           NANOSECONDS_PER_SECOND / cpu->gt_cntfrq_hz;

       tick_period *= cpu->guest_freq_slow_times;

       return tick_period > 1 ? tick_period : 1;

   ``gt_cntfrq_hz`` 表示 ARM System Counter 频率；
   ``guest_freq_slow_times`` 表示当前仓库中的 slowtime 倍率。

3. ``slowtimes_comp_ticks``

   用于动态修改 slowtime 时保持 System Counter 单调递增，防止切换倍率后计数值发生回退。


计算公式
^^^^^^^^

.. code-block:: c

    syscnt =
        floor(virtual_clock_ns / tick_period)
        + slowtimes_comp_ticks

    tick_period =
        (1e9 / gt_cntfrq_hz)
        * guest_freq_slow_times

当：

.. code-block:: c

    gt_cntfrq_hz = 62.5 MHz
    guest_freq_slow_times = 1

则：

.. code-block:: c

    tick_period = 16 ns

    syscnt =
        virtual_clock_ns / 16

即 System Counter 每 16ns 增加一个 tick。


batch icount
------------

进一步可以考虑使用每个 Core 的 icount 驱动时间。

每个 Core 拥有独立的指令执行进度，多个 Core 的 icount 自然不会始终保持一致。假设 Core1 需要执行 ``y * icount`` 条 Guest 指令才能完成包处理并返回结果。

一种直接的方式是限制所有 Core 每次最多执行固定数量的 ``batch`` 指令，所有 Core 到达 batch 边界后统一推进时钟。

.. image:: 2026-09-03-16-31-30.png

这种方式存在一个明显问题：batch 同时承担了执行同步粒度和时间更新粒度。

如果 batch 设置得非常大，System Counter 会长时间保持不变。

如果 batch 设置得非常小，会频繁进入同步路径，引入较高的线程同步、TB 退出和调度成本。

因此需要将：

.. code-block:: text

    指令数 -> 仿真时间

和：

.. code-block:: text

    多核最大允许领先范围

拆分成两个独立问题。


skew Keeper
-----------

受 SystemC TLM Temporal Decoupling 思路启发，可以引入全局时间和最大领先范围。

每个 Core 维护自己的执行进度，并限制：

.. code-block:: c

    local_time <= global_time + skew

当快 Core 到达 skew 上限后停止继续执行，等待系统整体时间向前推进。

.. image:: 2026-09-03-19-32-26.png

这个方案需要注意几个问题：

1. 如果 Core0 在较晚的 local time 对共享变量执行 atomic 写入，而 Core1 当前 local time 较早，但由于 Host 并行执行已经观察到了该变量的新值，此时如果各 Core 对外暴露不同的 local time，就可能出现共享内存可见顺序和时间戳顺序之间的不一致。

   因此所有 CPU 对软件暴露的 ``CNTVCT/CNTPCT`` 应使用统一的 ``global_time``。

   Core 的 local icount 只用于限制执行进度和计算 global time，不直接作为每个 CPU 独立可见的 System Counter。

   这样可以保留 MTTCG 原有的 shared memory、atomic、memory barrier、exclusive access 等 SMP 语义。

2. icount 只表示 Guest 执行了多少条架构指令，不包含目标硬件上不同指令的执行延迟、Cache Miss、访存等待、流水线停顿等信息。

   因此需要额外定义统一的 ``SIM_IPS``，将 icount 映射到一个线性仿真时间尺度。

还有几个注意点：

1. 同步集合应该按照 vCPU 当前是否能够继续执行 Guest instruction 判断。

   Linux task 因 mutex、信号量等进入睡眠以后，同一个 vCPU 仍可能调度其他 task 继续执行，因此不能因为某个 task 阻塞就直接将整个 vCPU 从同步集合剔除。

   典型退出同步集合的状态包括：

   - WFI
   - WFE 后实际进入等待
   - halted
   - stopped
   - 其他无法继续执行 Guest instruction 的状态

   退出同步集合的 CPU 不再限制 ``global_time`` 推进。

2. 如果所有 CPU 都进入无法执行 Guest instruction 的状态，icount 将停止增长。

   此时需要执行 Idle Time Warp，使 ``QEMU_CLOCK_VIRTUAL`` 能够推进到最近的虚拟 Timer deadline，从而继续复用 QEMU 原有 Timer 子系统完成 Timer 到期处理和 IRQ 唤醒。


Slide Window skew
-----------------

可以进一步将 skew 定义为“允许一个 Core 相对当前全局安全进度最多领先多少”。

每个 vCPU 只需要维护原始执行指令数 ``raw_icount``。QEMU 主线程通过 atomic read 直接读取各 active vCPU 的 ``raw_icount``，不需要 vCPU 主动发布进度，也不需要额外维护 ``published_icount``。

CPU 可能退出同步集合一段时间，例如：

.. code-block:: text

    CPU0 raw_icount = 10,000,000
    CPU1 WFI 时 raw_icount = 5,000,000

CPU1 唤醒时，global time 可能已经推进到较远位置，因此需要通过基准值把 raw icount 映射到公共逻辑时间轴：

.. code-block:: c

    logical_icount =
        logical_base
        + (raw_icount - raw_base)

CPU 重新加入同步集合时：

.. code-block:: c

    raw_base = atomic_read(&cpu->raw_icount);
    logical_base = global_icount;

此时：

.. code-block:: c

    logical_icount = global_icount

随后 raw icount 每增加一条 Guest 指令，对应的 logical icount 同步增加一条。

QEMU 主线程读取所有 active CPU：

.. code-block:: c

    raw_icount =
        atomic_read(&cpu->raw_icount);

    logical_icount =
        cpu->logical_base
        + (raw_icount - cpu->raw_base);

然后计算当前全局安全进度：

.. code-block:: c

    candidate =
        min(active_cpu[].logical_icount);

    global_icount =
        max(global_icount, candidate);

主线程读取到稍旧的 ``raw_icount`` 只会使 ``global_icount`` 暂时少推进，不会使时间提前推进。

快 Core 可以在：

.. code-block:: c

    global_icount
    <= local_logical_icount
    <= global_icount + MAX_SKEW_ICOUNT

范围内继续运行。

当 Core 到达 skew 上限时，读取最新的 ``global_icount``。如果仍然超过允许范围，则进入等待；global time 推进以后再恢复运行。

这种方式可以保持一个持续移动的滑动窗口：

.. image:: 2026-09-03-19-33-02.png

每个 CPU 需要维护：

.. code-block:: text

    raw_icount
        当前 vCPU 实际执行过的 Guest 指令累计数

    raw_base
        当前逻辑时间映射对应的 raw icount 基准

    logical_base
        raw_base 对应的 global icount 基准

    active
        当前 vCPU 是否参与 global time 推进

全局只需要维护：

.. code-block:: text

    global_icount
        当前所有 active CPU 的安全最小逻辑进度


多QEMU时钟同步
--------------

如果需要解决 MPT 和 BBP 分属不同 QEMU 的问题，还需要建立跨 QEMU 的统一时间。

一种可行方案是搭设 Time Server。各 QEMU 本地读取所有 active Core 的 raw icount，换算出本实例当前的安全时间进度，再由独立通信线程与 Time Server 交互。

.. image:: 2026-09-04-14-30-08.png

具体思路如下：

1. **启动过程**

   各 QEMU 启动顺序和启动速度可能不同。

   QEMU 启动后向 Server 发送 hello 消息表明 ready。

   Time Server 收到配置中规定数量的 hello 后，广播 START 消息：

   .. code-block:: c

       global_time = 0

   所有 QEMU 收到 START 后开始滑窗执行。

2. **时钟发布**

   每个 QEMU 周期性读取本实例 active vCPU 的 raw icount，并转换为逻辑时间：

   .. code-block:: c

       logical_icount =
           logical_base
           + (atomic_read(&raw_icount) - raw_base)

       qemu_progress =
           min(active_vcpu[].logical_icount)

   Time Server 保存各 QEMU 的最新 progress，并计算：

   .. code-block:: c

       cached_global_icount =
           min(active_qemu[].qemu_progress)

   当 ``cached_global_icount`` 增长后，Server 广播给所有 QEMU。

   每个 QEMU 本地缓存一份：

   .. code-block:: c

       cached_global_icount

   Guest 读取时钟以及 vCPU skew 检查只访问本地缓存，不经过网络请求。

   由于各 QEMU 收到 Server 更新存在网络延迟，因此本地 cached global time 可能略微落后于 Server 当前 global time。

   时间进度只允许单调增加，因此较旧的数据只会使某个 QEMU 暂时等待更久，不会使 Guest 时间跳到未来。

3. **断线与重新加入**

   如果某个 QEMU 是被模拟系统中的正常板卡关机，应向 Server 发送 bye，Server 将该 QEMU 从 active 集合移除。

   板卡重新启动并发送 hello 后，可以把它作为当前仿真时间点新加入的节点：

   .. code-block:: c

       raw_base = current_raw_icount
       logical_base = cached_global_icount

   这表示该板卡在当前系统时间发生了一次重新启动。

   如果 QEMU 进程因为异常崩溃重新启动，仅同步 icount 无法恢复之前的 CPU、RAM、设备、DMA、中断和网络状态，此类场景需要单独定义故障恢复策略。

4. **时钟错误处理**

   Time Server 和 QEMU 都需要保证：

   .. code-block:: c

       new_global_time >= current_global_time

   网络乱序或重复消息可以通过时间值本身判断。

   如果收到：

   .. code-block:: c

       new_time == current_time

   可以忽略。

   如果收到：

   .. code-block:: c

       new_time < current_time

   表示该消息已经过期，或者时间状态发生异常，不允许用该值覆盖当前时间。

   Time Server 可以保存最近若干次 progress，用于检测某个 QEMU 是否持续发送异常倒退数据。


单QEMU时钟同步
--------------

跨 QEMU 时间同步需要额外的网络通信、节点状态管理和故障处理。

如果 QEMU 内部已经能够通过 icount 控制各个 vCPU 的执行进度，并建立统一 System Counter，那么同 QEMU 场景可以完全在进程内部完成时间协调。

.. image:: 2026-09-07-09-57-45.png

单 QEMU 方案中，每个 vCPU 维护自己的 ``raw_icount``。QEMU 主线程作为 Time Coordinator，维护唯一的 ``global_icount``。

vCPU 执行路径只负责增加自身 ``raw_icount``：

.. code-block:: c

    raw_icount += executed_insns;

主线程与 vCPU 之间不增加进度通知关系，也不增加额外的同步点。

主线程在需要更新时间时，直接通过 atomic read 读取所有 active vCPU 的 ``raw_icount``：

.. code-block:: c

    raw_icount =
        atomic_read(&cpu->raw_icount);

    logical_icount =
        cpu->logical_base
        + (raw_icount - cpu->raw_base);

然后计算：

.. code-block:: c

    candidate =
        min(active_cpu[].logical_icount);

    global_icount =
        max(global_icount, candidate);

``global_icount`` 由主线程维护，各 vCPU 在 TB 执行结束后通过 atomic read 获取当前 ``global_icount``，检查自身是否超过滑窗上限：

.. code-block:: c

    if (local_logical_icount <=
        global_icount + MAX_SKEW_ICOUNT) {
        continue;
    }

    yield();

达到 skew 上限的 vCPU 进入等待。主线程推进 ``global_icount`` 后，只需要唤醒由于达到 skew 上限而等待的 vCPU。


时钟换算
~~~~~~~~

.. code-block:: c

    // CNTFRQ
    // ARM System Counter 的计数频率，读 CNTFRQ_EL0 可得到

    // CPU_FREQUENCY
    // 目标 CPU 主频，例如 2GHz

    // IPC
    // 一个 Cycle 平均退休多少条指令

    // SIM_IPS
    // 模拟 CPU 每秒对应多少条 Guest 指令
    //
    // 可以估算为：
    //
    // SIM_IPS = CPU_FREQUENCY * IPC
    //
    // 更可靠的方式是在目标硬件上测量：
    //
    // SIM_IPS =
    //     retired_instructions / elapsed_time_s

    sim_time_s =
        global_icount / SIM_IPS

    virtual_time_ns =
        global_icount
        * 1000000000ULL
        / SIM_IPS

    syscnt =
        global_icount
        * CNTFRQ
        / SIM_IPS


主线程更新时间间隔
~~~~~~~~~~~~~~~~~~

主线程更新时间间隔决定 ``global_icount`` 和 ``QEMU_CLOCK_VIRTUAL`` 的普通更新粒度。

.. code-block:: c

    // MIN_TIMER_PERIOD
    // 最小关注的软件 Timer 周期，例如 Linux 1ms Timer

    // UPDATE_DIV
    // 一个 Timer 周期内希望至少有多少次 global time 更新，例如 10~20 次

    // TIME_UPDATE_INTERVAL
    // global time 普通更新时间间隔

    TIME_UPDATE_INTERVAL =
        MIN_TIMER_PERIOD / UPDATE_DIV

    UPDATE_ICOUNT =
        TIME_UPDATE_INTERVAL_NS
        * SIM_IPS
        / 1000000000ULL

    // 示例：
    //
    // MIN_TIMER_PERIOD = 1ms
    // UPDATE_DIV = 10
    // SIM_IPS = 2,000,000,000
    //
    // TIME_UPDATE_INTERVAL = 100us
    // UPDATE_ICOUNT = 200,000 icount

TIME_UPDATE_INTERVAL 只用于控制普通的 global time 更新粒度。

正常有 CPU 执行时，不使用 Timer deadline 限制 vCPU 的 run budget，也不因为 Timer deadline 强制 vCPU 提前退出 TB。


最大领先间隔
~~~~~~~~~~~~

.. code-block:: c

    // MAX_TIME_SKEW
    // 每个 vCPU 相对 global_time 最大允许领先的时间

    MAX_TIME_SKEW =
        TIME_UPDATE_INTERVAL * SKEW_FACTOR

    MAX_SKEW_ICOUNT =
        MAX_TIME_SKEW_NS
        * SIM_IPS
        / 1000000000ULL

    // 示例：
    //
    // TIME_UPDATE_INTERVAL = 100us
    // SKEW_FACTOR = 10
    // MAX_TIME_SKEW = 1ms
    // MAX_SKEW_ICOUNT = 2,000,000 icount

vCPU 的执行限制只由滑动窗口决定：

.. code-block:: c

    local_logical_icount
        <= global_icount + MAX_SKEW_ICOUNT


QEMU_CLOCK_VIRTUAL
~~~~~~~~~~~~~~~~~~

主线程计算出新的 ``global_icount`` 后，将它映射到统一虚拟时间：

.. code-block:: c

    virtual_time_ns =
        global_icount
        * 1000000000ULL
        / SIM_IPS

该时间作为新的 ``QEMU_CLOCK_VIRTUAL`` 推进来源。

正常运行路径中，本方案只修改虚拟时钟的时间来源和推进方式。

QEMU 已有 Timer 子系统继续按照 ``QEMU_CLOCK_VIRTUAL`` 判断 Timer 是否到期，并执行原有 Timer callback、Generic Timer、设备 Timer、IRQ 等处理逻辑。

因此正常 CPU 运行路径中不需要新增：

.. code-block:: text

    process_expired_timers()
    timer deadline run budget
    vCPU timer deadline同步

Timer 调度逻辑继续复用 QEMU 主干已有实现。


Idle Time Warp
~~~~~~~~~~~~~~

当所有 vCPU 都退出 active 集合时：

.. code-block:: c

    active_cpu_count == 0

此时：

.. code-block:: text

    raw_icount 不再增加
        ->
    global_icount 不再增加
        ->
    QEMU_CLOCK_VIRTUAL 不再推进

如果此时 Guest 正在等待 Generic Timer 或设备 Timer IRQ，虚拟时间将无法自然到达 Timer deadline。

因此只有在 all WFI / all idle 场景下，需要查询当前 ``QEMU_CLOCK_VIRTUAL`` 最近的 Timer deadline。

QEMU 已有接口可以获取距离最近虚拟 Timer 到期还剩多少时间，例如：

.. code-block:: c

    deadline_ns =
        qemu_clock_deadline_ns_all(
            QEMU_CLOCK_VIRTUAL,
            QEMU_TIMER_ATTR_ALL);

如果：

.. code-block:: c

    deadline_ns >= 0

则将虚拟时间推进到该 deadline。

概念上：

.. code-block:: c

    virtual_time_ns += deadline_ns;

随后仍然由 QEMU 原有 Timer 子系统发现 Timer 到期、执行 callback，并通过 IRQ 等方式唤醒对应 vCPU。

因此 Idle Time Warp 新增的职责只有：

.. code-block:: text

    1. 判断所有参与时间推进的 CPU 是否都已 idle
    2. 获取最近 QEMU_CLOCK_VIRTUAL Timer deadline
    3. 将虚拟时间推进到该 deadline

Timer 到期处理本身不新增实现。

CPU 被 Timer IRQ 唤醒并重新加入 active 集合时，重新建立 logical icount 基准：

.. code-block:: c

    raw_base =
        atomic_read(&cpu->raw_icount);

    logical_base =
        global_icount;


方案遗留问题
------------

icount 只表示 Guest 已执行的架构指令数量。

不同 Guest 指令在目标真实 CPU 上可能具有不同的执行成本，同时还会受到 Cache Miss、访存延迟、流水线停顿、分支预测、共享资源竞争等因素影响。

因此：

.. code-block:: text

    相同的 icount 增量

在真实硬件上并不保证对应完全相同的执行时间。

本方案通过：

.. code-block:: text

    SIM_IPS

建立统一的线性仿真时间尺度。

该时间模型主要用于：

- 将 Guest 时间从 Host wall time 中解耦；
- 限制 MTTCG 各 vCPU 之间的执行领先程度；
- 保持软件 Timer、timeout 和 Guest 指令执行进度之间稳定的比例关系；
- 避免 Host TCG 执行性能不足直接转换成 Guest 软件超时。

该模型不提供 CPU 微架构级时间精度。

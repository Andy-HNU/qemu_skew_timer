# MTTCG / skew 双向 QMP 切换

所有 CPU 和虚拟定时器共用同一条单调时间线。Linux 先使用 MTTCG 的原生虚拟时间完成 jitter 初始化，再由宿主在 `/init` 阶段通过 QMP 切到 skew；随后可以切回 MTTCG，并再次进入 skew。

## 两个累计量

- `mttcg_elapsed_ns`：已经结算的 MTTCG 阶段累计贡献。
- `skew_elapsed_ns`：skew 阶段的累计贡献，包含空闲跳时与 visible 插值；协调器保存最近结算值，查询和退出按当前 visible 补齐。

MTTCG 读取时补上当前阶段尚未结算的增量：

```text
MTTCG: virtual = mttcg_elapsed + skew_elapsed
                + cpu_get_clock() - mttcg_start_clock
skew:  virtual = shared visible time
       current skew elapsed = virtual - mttcg_elapsed
```

进入 skew 时，把 MTTCG 当前阶段增量归入累计量；记下当前总时间 `start_ns`，本阶段 global、CPU 进度和 warp 从零开始。协调器按 `start_ns + global_icount × 1e9 / ips + warp` 更新 model；visible 在 model ± window 内插值。进入时 model、visible 和插值锚点同为最终切换值，历史和两个时间锚点按新阶段重建。

退出 skew 时先停稳所有 vCPU，再持有 BQL 用同一个冻结 elapsed 采样求出最终 visible，补齐插值贡献，令 `mttcg_start_clock = elapsed`。两个方向都清空历史并重建插值与采样锚点。各 CPU 的预算在停核过程中正常结算，但尚未进入共享时间的领先尾部不强行加入时间。global、raw、logical 基准、预算、active/waiting 全部清零；两个累计纳秒量保留。MTTCG 从相同总时间继续流逝。

切换时先停止协调器并暂停所有 vCPU，等旧预算由各核结算；随后准备预算状态、清空旧 TB，最后持有 BQL 结算旧 visible 并提交新时钟，恢复运行才启动协调器。MTTCG 期间不运行 skew 协调器，不安装执行预算回调，不保留 TB 计数标志。共享时钟读者用 seqlock 获取一致的模式和时间基准，防止读到切换中间状态。旧阶段 vCPU 的 CAS 已在 stopped 前完成，协调器和 QMP/QOM 查询由 BQL 串行化。getter 保留原有插值/CAS-max，不增加读者登记或切换等待操作。

已设置定时器的绝对截止时间不变。运行中的 VM 自动恢复并产生 STOP/RESUME 事件；暂停或 prelaunch 的 VM 继续保持停止。同模式重复命令不重置状态。停止 VM 失败时命令报错，VM 可能保持暂停。

## 使用方法

启动参数示例：

```sh
-accel tcg,thread=multi,skew=1000000,skew-ips=2000000000,skew-update=100000,skew-defer=on \
-qmp unix:build/skew-qmp.sock,server=on,wait=off
```

`skew-defer=on` 从 MTTCG 开始；省略时直接从 skew 开始。MTTCG 的原生虚拟时间随宿主单调时钟推进，VM 暂停时冻结。配置这个功能的整个会话仍禁止迁移/快照。

```json
{"execute":"qmp_capabilities"}
{"execute":"query-skew-clock"}
{"execute":"skew-start"}
{"execute":"skew-stop"}
{"execute":"skew-start"}
```

`query-skew-clock` 返回：

| 字段 | 含义 |
|---|---|
| mode | mttcg 或 skew |
| virtual-ns | 当前软件可见共享时间 |
| mttcg-elapsed-ns / skew-elapsed-ns | 含当前阶段贡献，两者之和等于 virtual-ns |
| start-ns | 最近一次实际切换的时间点 |
| global-icount | 当前 skew 阶段的全局进度，MTTCG 中为零 |
| window-ns / window-insns / ips / update-ns | 配置及窗口换算 |
| cpus | 各 CPU 的已发布 raw/local 进度、active、waiting、halted、budget-enabled |

CPU 列表是运行中采样，不会为查询停核。raw/local 不包含本轮尚未发布的预算消耗；不读取其他 CPU 正在改写的剩余预算。数值会随执行变化，active/waiting 也可能因调度变化，不能把两次状态相同当作永久不变。

## Linux 测试

测试 guest 为静态 `/init`，加载发行版原装 `jitterentropy_rng.ko` 后，在串口发送握手标记。宿主在每个标记处通过 QMP 切换，成功后通知 guest 继续。两颗 guest CPU 从启动阶段就已在线，不用 active 数量推断 jitter 完成。

```text
MTTCG 启动 → /init jitter 初始化
 → skew → MTTCG → skew → MTTCG → 正常关机
```

两条绑定到不同 CPU 的线程跨越全部切换持续计算并读取 CNTVCT，检测时间回退。每段额外检查跨核迁移后的顺序读时钟、8 次 1 ms 定时休眠，以及切换前设置的 100 ms timerfd 在切换后到期。每段 skew 运行期间查询两次 CPU 状态；运行中 raw 不包括当前预算，暂停切换在旧模式停稳的边界检查双核已结算进度。

复现需要 AArch64 静态 libc 交叉工具链、cpio、匹配的内核和 jitter 模块（依赖编入内核）。本机采用 Debian `linux-image-6.1.0-50-cloud-arm64-unsigned_6.1.176-1_arm64.deb`，用 `dpkg-deb -x kernel.deb kernel` 解包，无需安装进宿主。

```sh
python3 tests/tcg/aarch64/system/skew-linux-switch.py \
  build/qemu-system-aarch64 \
  --kernel /path/to/arm64-kernel \
  --jitter-module /path/to/matching/crypto/jitterentropy_rng.ko \
  --output build/skew-linux-roundtrip
```

选择新输出目录可保留历史日志。脚本先检查 prelaunch 往返、重复命令、未配置时的拒绝行为，再分别运行“运行中直接切换”和“暂停后切换”两轮 Linux 测试。

## visible 场景

状态、字段与锁顺序见 [visible 双向切换](SKEW_VISIBLE_SWITCH_zh.md)。
定向测试提取实际时钟/CPU 切换函数，使用 QEMU atomics 和 seqlock；
发布线程在每次切换前停稳，生产 getter 不增加读者登记。
实际 VM 停核、TB unwind、设备和 ARM System Counter 需要 Linux/guest 集成测试。

```sh
python3 tests/tcg/aarch64/system/skew-visible-switch.py --build-dir build
```

本测试不等同于特定软件看门狗验收或熵质量证明。
两个模式的累计量用于虚拟时间组成，skew-ips 不代表与宿主墙钟同速。
历史实测记录见[整理前文档](https://github.com/Andy-HNU/qemu_skew_timer/blob/17d49d3d497ce9f2235c739acfedcca7e0a0e086/docs/clock-model/SKEW_QMP_zh.md)。

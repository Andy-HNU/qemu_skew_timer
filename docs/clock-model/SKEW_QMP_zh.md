# MTTCG / skew 双向 QMP 切换

所有 CPU 和虚拟定时器共用同一条单调时间线。Linux 先使用 MTTCG 的原生虚拟时间完成 jitter 初始化，再由宿主在 `/init` 阶段通过 QMP 切到 skew；随后可以切回 MTTCG，并再次进入 skew。

## 两个累计量

- `mttcg_elapsed_ns`：已经结算的 MTTCG 阶段累计贡献。
- `skew_elapsed_ns`：所有 skew 阶段已经发布的累计贡献，包含空闲跳时。

MTTCG 读取时补上当前阶段尚未结算的增量：

```text
MTTCG: virtual = mttcg_elapsed + skew_elapsed
                + cpu_get_clock() - mttcg_start_clock
skew:  virtual = mttcg_elapsed + skew_elapsed
```

进入 skew 时，把 MTTCG 当前阶段增量归入累计量；记下当前总时间 `start_ns`，本阶段 global、CPU 进度和 warp 从零开始。协调器按 `start_ns + global_icount × 1e9 / ips + warp` 更新共享时间，并将新发布的增量归入 skew 累计量。

退出 skew 时保留最终已发布的累计量，令 `mttcg_start_clock = cpu_get_clock()`。各 CPU 的预算在停核过程中正常结算，但尚未进入共享时间的领先尾部不强行加入时间。global、raw、logical 基准、预算、active/waiting 全部清零；两个累计纳秒量保留。MTTCG 从相同总时间继续流逝。

切换时先暂停所有 vCPU，再改时间基准和执行预算状态、清空旧 TB。MTTCG 期间不运行 skew 协调器，不安装执行预算回调，不保留 TB 计数标志。共享时钟读者用 seqlock 获取一致的模式和时间基准，防止读到切换中间状态。

已设置定时器的绝对截止时间不变。运行中的 VM 自动恢复并产生 STOP/RESUME 事件；暂停或 prelaunch 的 VM 继续保持停止。同模式重复命令不重置状态。停止 VM 失败时命令报错，VM 可能保持暂停。

## 使用方法

启动参数示例：

```sh
-accel tcg,thread=multi,skew=1000000,skew-ips=2000000000,skew-update=100000,skew-defer=on \
-qmp unix:/tmp/skew-qmp.sock,server=on,wait=off
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

两条绑定到不同 CPU 的线程跨越全部切换持续计算并读取 CNTVCT，检测时间回退。每段额外检查跨核迁移后的顺序读时钟、8 次 1 ms 定时休眠，以及切换前设置的 100 ms timerfd 在切换后到期。每段 skew 运行期间查询两次 CPU 状态。

复现需要 AArch64 静态 libc 交叉工具链、cpio、匹配的内核和 jitter 模块（依赖编入内核）。本机采用 Debian `linux-image-6.1.0-50-cloud-arm64-unsigned_6.1.176-1_arm64.deb`，用 `dpkg-deb -x kernel.deb kernel` 解包，无需安装进宿主。

```sh
python3.9 tests/tcg/aarch64/system/skew-linux-switch.py \
  build/qemu-system-aarch64 \
  --kernel build/linux-jitter-20260916/kernel/boot/vmlinuz-6.1.0-50-cloud-arm64 \
  --jitter-module build/linux-jitter-20260916/kernel/lib/modules/6.1.0-50-cloud-arm64/kernel/crypto/jitterentropy_rng.ko \
  --output build/skew-linux-roundtrip-v2
```

选择新输出目录可保留历史日志。脚本先检查 prelaunch 往返、重复命令、未配置时的拒绝行为，再分别运行“运行中直接切换”和“暂停后切换”两轮 Linux 测试。

## 2026-09-16 WSL2 实测

Debian Linux 6.1.176，2 vCPU、512 MiB、cortex-a57、GICv3；窗口 1 ms、2 GIPS、协调间隔 100 us。

两种切换方式各完成 4 次转换，共 8 次。全部完成 jitter 初始化、guest 时钟和定时器检查并正常关机。每段 skew 两次采样的 active CPU 领先量都在 2,000,000 条指令窗口以内。切回 MTTCG 后 global/raw/local 均为零，active/waiting 关闭，budget-enabled=false；再次启用后两核重新推进。

### 暂停切换的时间连续性

以下均为纳秒；暂停状态下不混入命令之间的执行时间。

| 方向 | 切换前 | 切换后 | MTTCG 累计 | skew 累计 |
|---|---:|---:|---:|---:|
| MTTCG → skew | 3030181049 | 3030181049 | 3030181049 | 0 |
| skew → MTTCG | 3126476425 | 3126476425 | 3030181049 | 96295376 |
| MTTCG → skew | 3251910819 | 3251910819 | 3155615443 | 96295376 |
| skew → MTTCG | 3351323580 | 3351323580 | 3155615443 | 195708137 |

每行切换前后相等，且两个累计量之和等于可见时间。恢复运行后只有当前模式的贡献继续增加，暂停期间两者都冻结。

### 首段 skew 的两次 CPU 状态采样

取运行中切换这一轮。两次查询之间间隔约 30 ms 宿主时间。

| 采样 | CPU | global | raw / local | lead | active | waiting | budget-enabled |
|---|---:|---:|---:|---:|---|---|---|
| 1 | 0 | 5898234 | 5898234 | 0 | true | false | true |
| 1 | 1 | 5898234 | 6160290 | 262056 | true | false | true |
| 2 | 0 | 8470076 | 8470076 | 0 | true | false | true |
| 2 | 1 | 8470076 | 10339006 | 1868930 | true | false | true |

这里 raw 和 local 相等，因为采样时这两核尚未空闲重定位；一般情况下不要求两者相等。两个核的状态标志在这两次采样中相同，进度持续增加。其他 skew 阶段的两次完整快照也保留在 JSON 中。

原始结果位于 `build/skew-linux-roundtrip-v2/`，包含 results.json、control-results.json、各轮 serial.log 和完整 qmp.json。原有 `skew-check.py --quick` 回归结果位于 `build/skew-roundtrip-regression/`。第一次高频读取计数器的探索运行主动中止，未纳入此报告；正式负载在计数器读取间加入纯计算。

本测试不等同于特定软件看门狗的完整验收，也未验证熵质量。进入 skew 后继续使用 jitter 仍可能失败；切换时机由外部控制器决定。两个模式的累计量用于虚拟时间组成，skew-ips 不代表与宿主墙钟同速。

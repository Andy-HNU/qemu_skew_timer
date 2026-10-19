<!--
SPDX-License-Identifier: GPL-2.0-or-later
-->
# 指数渐近时钟重实现与 jitter 验证

本实现基于已发布的 `fa091e0`，由 2026-10-19 的重新实现请求产生。
旧提交 `52ad72f` 的源码仍未恢复；本实现有独立源码和验证记录。

## 曲线与状态

新增 `-accel tcg,...,skew-interp=linear|exponential`，默认 `linear`。
同一二进制可切换启动配置，分别测试线性和指数插值；该参数不在运行中改变。
指令预算、active 最小进度、严格 model 时间及 idle warp 规则保持原语义。

每段使用协调器保存的起点 `A`、可暂停的宿主 elapsed 起点 `t0`、上界
`U = model + window` 和动量/反馈生成的起始斜率 `s`（Q32 存储值除以 `2^32`）：

\[
R=U-A,\qquad
V(t)=A+R\left(1-\exp\left(-\frac{s(t-t_0)}{R}\right)\right).
\]

`V(t0)=A`，起始导数为 `s`，随后随剩余余量减小而减速。每次读取按同一组
锚点计算，读取本身不重建曲线、不消耗额度，也不更改动量采样基准。
使用 `expm1` 避免接近零时的相消；只对相对时间和余量做浮点计算，避免
大于 2^53 ns 的绝对时间戳丢失低位。

数学曲线在有限 elapsed 下严格小于上界。整数实现将最大增量限制在
`R - 1 ns`，避免浮点舍入直接命中上界；最终仍用共享 CAS-max 发布，
所有 vCPU 与设备共用一个单调高水位。余量为 0 或 1 ns 时不能再前进。

协调器在同一个 elapsed 采样点结算旧曲线并设置新锚点。新段从已发布的
旧段末值开始；CPU 恢复时仍使用现有最低活跃斜率，独立动量采样锚点不变。
暂停冻结 elapsed；双向模式切换在 vCPU 停稳后按最终 visible 重建阶段。

仍须遵守 `model ± window`：若 model 的离散推进使新下界高于旧 visible，
协调器沿用必要的下界投影。这种 model 更新或 idle warp 不具备连续性保证。
指数曲线解决固定区间内的线性撞界，不能消除移动硬下界本身要求的投影。

## jitter 的判定边界

有限窗口与整数计时无法保证模型无限期停止时仍有无限个不同读数。
62.5 MHz CNTVCT 还会把纳秒量化为 16 ns 一个 tick；即使 visible 小于
上界，CNTVCT 也可能重复。因此“本次全部通过”只表示限定环境下没有观察到
失败，不能推导为任意宿主停顿、IPS、窗口和读取模式都成功。

本实现不按读取次数补 tick，不注入随机数，不绕过 Linux jitter 的初始化
或运行期健康检查。`expm1` 位于计数器读取热路径，其 CPU 成本需另行评估；
此前线性模型上的性能收益不能直接作为本模型的性能结论。

## 验证入口

- `skew-asymptotic.py`：编译实际预测函数，对照 80 位十进制精度的公式，
  验证边界、单调性、极小余量、零斜率、稀疏/密集读取、大时间戳与整数平台。
- `skew-visible-switch.py --exponential`：实际时钟函数的停稳切换夹具，
  检查最终插值结算、CPU 状态和 4 个发布者之间的 1000 次切换。
- `skew-check.py --quick --skew-options skew-interp=exponential`：真实裸机
  精确指令计数、窗口、暂停、idle、IRQ 和定时器回归。
- `skew-bottleneck-check.py --skew-options ...`：加入指数选项后核对
  CNTVOFF、慢核和运行中/暂停时双向切换。
- `skew-linux-switch.py --skew-options skew-interp=exponential`：真实 Linux
  的运行中及暂停时双向模式转换。
- `skew-jitter-study.py`：冷启动 Linux jitter 对照，保留每次结果与原始串口。

数值和停稳夹具的覆盖范围单独记录，不能代替完整 Guest 验收。

## Linux 对照方法

使用 Debian `linux-image-6.1.0-50-cloud-arm64_6.1.176-1_arm64.deb`，内核
和模块均来自同一个官方包。`jitterentropy_rng.ko` 的 SHA256 为
`ef71ff1d1dab6c2704e85b6f4ece4f7e1a6367bb4f04440da397f953a37a8ad3`，
与 2026-10-18 历史故障诊断的模块一致。

每次新建 VM，同一矩阵内使用同一个 QEMU、Guest initramfs 和未修改的内核/模块。
检查模块初始化、AF_ALG 持续读取、完成标记和 QEMU 正常退出；Guest 出错时
记录已完成读取数。默认只在启动时查询一次 QMP，不持续轮询，以减少观察干扰。
可用 `--sample-seconds` 或 `--trace` 单独诊断，相关运行明确标记为 diagnostic。
`--health-probes` 仅接受上面的精确模块 SHA256，在模块加载成功后安装
失败分支 kprobe；记录 RCT/APT、零差值和复检返回码，不改变健康检查。
探针自身失败单独分类，诊断结果不并入无探针的通过率。新版驱动保存测试
源码快照并从快照编译 init，避免后续编辑使负载来源不清。

矩阵入口提供四种配置，窗口固定 1 ms：

| 配置 | 更新间隔 | 发布/续配/计数器策略 |
|---|---:|---|
| nominal | 100 µs | periodic，fresh 关闭，counter-fast 关闭 |
| delayed | 1 ms | periodic，fresh 关闭，counter-fast 关闭 |
| counter | 100 µs | periodic，fresh 关闭，counter-fast 开启 |
| inline | 100 µs | inline，fresh 开启，counter-fast 开启 |

nominal 默认比较 200 MIPS 和 2 GIPS 下的 linear/exponential，并每轮加入
一次普通 MTTCG 对照。顺序使用固定种子 20261019 打乱。每次编译有独立超时，
每个 Guest 默认 90 秒宿主截止时间，整个矩阵默认 900 秒；失败不触发自动重试。
运行前写入完整计划，最终同时报告计划数、实际执行数、通过和失败数。

```sh
python3 tests/tcg/aarch64/system/skew-jitter-study.py \
  --qemu build/qemu/qemu-system-aarch64 \
  --kernel /path/to/vmlinuz-6.1.0-50-cloud-arm64 \
  --module-dir /path/to/lib/modules/6.1.0-50-cloud-arm64/kernel/crypto \
  --output build/jitter-nominal-new --rounds 5 --reads 1024
```

使用全新输出目录；保留 `plan.json`、`environment.json`、逐次 `results.json`、
`runs.json`、原始串口、QMP 与失败日志。结果 JSON 额外保存真实串口原文及哈希，
用于核对独立日志副本。不得从测量字段反向生成原始输出。

## 实测结果

### 正式 nominal 对照

同一二进制、2 vCPU、1 ms 窗口，每组 3 次独立冷启动，每次要求完成
1024 次、每次 64 字节 AF_ALG 读取。15 个预定样本全部执行，无超时。

| 方案 | 200 MIPS | 2 GIPS |
|---|---:|---:|
| linear | 1/3 通过 | 0/3 通过 |
| exponential | 3/3 通过 | 2/3 通过 |
| 普通 MTTCG（无模拟 IPS 参数） | 合计 3/3 通过 | — |

证据目录：`build/asymptotic-20261019/jitter-nominal/`。
指数方案失败样本 `011-nominal-2000000000-exponential` 成功读取 529 次后，
内核报告永久健康检查失败，AF_ALG 返回 `EFAULT`。线性方案共 4 次初始化
失败、1 次读取 228 次后永久失败；另 1 次完整通过。

正式矩阵之前的独立 pilot 同样保留：2 GIPS 线性初始化失败、指数成功
读取 177 次后永久失败、普通 MTTCG 完成 1024 次。该 pilot 不混入上表。
pilot 首个失败的串口行被旧读取器提前截断，但内核初始化错误码 2 已完整
记录；后续驱动等待完整失败行再结束，原日志保留，不补造缺失尾部。

这些观测支持“本批中指数策略通过更多”，不支持“jitter 已稳定修复”。
指数方案在两个独立无探针运行中真实失败，已经否定本配置下的全成功。

### 联合性能选项

另做 inline 发布、fresh-budget 开启、counter-fast 开启的无探针对照，
200 MIPS 和 2 GIPS 各两轮，每次仍为 1024 次读取。8 个预定样本全部
执行，无超时；正式结果在 `jitter-inline/`。

| 方案 | 200 MIPS | 2 GIPS |
|---|---:|---:|
| linear + inline/fresh/counter-fast | 1/2 通过 | 0/2 通过 |
| exponential + inline/fresh/counter-fast | 2/2 通过 | 0/2 通过 |

指数方案两次失败均为运行期永久健康失败，分别成功读取 875、196 次。
这组现有性能选项也未消除 jitter 失败；样本较少，不能据通过率直接评定
哪种发布策略更可靠或更快。

pilot、nominal、inline 三个批次的 QEMU SHA256 均为
`13d2ff0d9dfdc3afaa1b2eaa38da4a462e34ce79e658110df0cd91ec2fac03a4`；
它们的 Guest `/init` SHA256 也完全相同：
`e6e1124819f19b0b9ca7eca104dfa3374d3aa1ac73b9e44d46a9891bed44a1d1`。
三批合计 26 次无探针冷启动：13 次完整通过，13 次 Guest 健康错误。
其中指数为 7/11 通过（200 MIPS 5/5、2 GIPS 2/6），线性 2/11，
普通 MTTCG 4/4。这只是已列配置的合计，不能取代分组表或作为总体概率。

### 失败分支诊断

另做 4 次带健康探针和协调轨迹的指数运行：nominal/delayed 各 2 次，
均为 2 GIPS、1024 次读取上限。全部未完成，其中 1 次初始化失败、
2 次永久健康失败、1 次可重试的 `EAGAIN`；没有把可重试错误标为永久错误。
负载不自动重试 AF_ALG 读取。插桩改变时序，这 4 次只用于诊断。

`jitter-health-diagnostic/001-delayed-2000000000-exponential` 捕获：

1. 成功读取 763 次后进入 `jent_read_entropy+0x70`：
   `prev=533737684, delta=0, delta2=0, rct=30, apt=0`。
2. 内核复检在第 29 次迭代得到相同计数器：
   `time1=time2=533769123, delta=0`。
3. `jent_read_entropy+0x7c` 记录 `init_rc=2`（`ECOARSETIME`），
   随后内核报告永久健康失败，AF_ALG 返回 `EFAULT`。

nominal 的另一次诊断也捕获 `RCT=30 → 复检零差值 → 返回码 2`，成功
读取 306 次后失败。`EAGAIN` 样本在 406 次后失败，捕获 `RCT=30`，
没有复检失败事件；这与内核可重试健康错误路径一致。

同一 delayed 失败的 QEMU 轨迹还记录了以下值：

| 量 | 观测值 |
|---|---:|
| 严格 model | 8,538,802,954 ns |
| 上界 model + window | 8,539,802,954 ns |
| RCT 时的 counter | 533,737,684 ticks |
| 该 tick 对应纳秒区间 | 8,539,802,944–8,539,802,959 ns |
| 保持同一 model 的协调样本 | 连续 4 个 |
| 第一条到下一次 model 推进的宿主间隔 | 124,443,245 ns |

Linux 启动日志确认虚拟计数器为 62.50 MHz，即 16 ns/tick。结合指数
策略严格留在上界以下的约束，以上读数只剩 1–10 ns 余量，下一 tick 已
超出固定上界。曲线可以不命中上界，同时计数器已经无法再增加一个 tick。
这是量化平台与停滞 model 叠加的具体反例。这里的对应关系来自计数器值
与 model 轨迹的匹配，未插桩每次 CNTVCT 的宿主时间。

这 4 个协调样本的活跃 CPU 已发布 raw 也相同；不能把 124.4 ms 直接
认定为 BQL 持锁时间，也尚未区分未结算执行片、线程调度和其他延迟的贡献。
后续应度量真实指令进度的发布延迟及边界余量，再决定发布/执行片和曲线
时间常数是否需要联合调整。任何固定有限窗口配合整数计数器，都无法在
model 无限期不动时保证无限次不同读数。

### 其他回归

| 检查 | 结果与边界 |
|---|---|
| 生产预测函数高精度对照 | 2684 组通过，观测最大误差 0 ns，启用 UBSan |
| 停稳切换夹具 | 4 个发布者、1000 次切换通过；不是运行中读者任意切换证明 |
| 独立动量锚点 | 通过，并能识别旧共享锚点的回归 |
| 裸机 quick | 15 条顶层检查通过，包含 12 个非法配置拒绝 |
| 指数 + inline/fresh/counter-fast 定向回归 | CNTVOFF、慢 CPU0、慢 CPU1、20 次运行中和 20 次暂停转换通过 |
| 真实 Linux 模式转换 | running/paused 两条路径通过，各 4 个阶段；包括计数器、定时器和暂停冻结 |

本次没有重新测量性能最优点、指令执行占比或完整业务开机到关闭收益。
`skew-interp=exponential` 保持显式选择，默认线性；不能将其标为稳定 jitter 修复。

完整原始证据以 `skew_asymptotic_jitter_evidence_2026-10-19.zip` 单独归档，
包含每次计划、命令、结果、实际串口、诊断轨迹、负载和校验清单。
`build/asymptotic-20261019/` 保留现场；没有删除失败、旧实现或工作区文件。

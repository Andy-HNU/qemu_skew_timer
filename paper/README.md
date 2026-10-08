# 可见时间核心实验

这里仅保留可复用的 exp5 实验源码与独立回归。日常构建、裸机验收、Linux
jitter 和切换测试统一见[测试说明](../docs/clock-model/TEST_ACCEPTANCE_zh.md)。

## 独立回归

从仓库根目录运行，临时文件和输出留在已忽略的 `build/`：

```sh
mkdir -p build/skew-tmp
export TMPDIR="$PWD/build/skew-tmp"
python3 paper/experiments/exp5_visible_time/check_wide_arithmetic.py
python3 paper/experiments/exp5_visible_time/check_ips_boundary.py \
    --out build/skew-ips-boundary
```

算术回归使用 `CC`（默认 `cc`），编译实际 `skew_muldiv()` 并与 Python
整数运算比较，启用 UBSan；不需要 QEMU 二进制。IPS 边界回归需要先构建
`build/qemu-system-aarch64`，通过 QMP 核对 2^32 两侧及高 IPS 的窗口换算。

## 联合 CNTVCT 轨迹实验

[实验方法与限制](experiments/exp5_visible_time/SPEC.md)配套四个文件：

- `build_probe.py`：从当前源码副本构建实验二进制，不覆盖正式源码
- `probe.inc.c`：联合 model/visible/counter 采集器
- `counter-guest.c`：真实 AArch64 CNTVCT 读取负载
- `run_matrix.py`：13 场景、baseline/probe 对照及逐次结果保存

`build_probe.py` 在当前 `skew_visible_clock()` 发布后记录返回值，保持精确
锚点校验；通过 `ninja -t compdb -x c_LINKER_RSP` 获取展开的真实链接命令，
复用配置的编译器和链接参数。需要已有 QEMU 构建及支持该命令的 Ninja；
源码或链接规则不匹配时明确失败，不静默猜测插桩位置。
`run_matrix.py` 依赖生成的二进制及 manifest；缺失 probe trace 会使本次
运行失败。评估时仍应逐项检查 metrics、recorder 和预期读取数量。
容器不提供 CPU 拓扑时，manifest 保留 `lscpu` 的退出码和错误输出，
不补造机器信息，也不把这种运行解释为完整性能测量。

不再保留重复的 `run_reuse.py` 包装器，直接执行现有 Linux jitter 和
裸机测试入口，分别检查各自退出码和结果。运行结果不要提交到分支。

旧论文、作图流水线和两批历史结果可从
[整理前提交](https://github.com/Andy-HNU/qemu_skew_timer/tree/17d49d3d497ce9f2235c739acfedcca7e0a0e086/paper)
追溯；原始 schema、设计图输入和示例与已移除的生成器一并留在该提交中。

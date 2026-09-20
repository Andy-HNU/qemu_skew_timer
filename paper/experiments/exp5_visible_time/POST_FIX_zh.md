# IPS 换算修复与 WSL 回归

日期：2026-09-20。修复前基线 `4e894601`；测试版本为该基线加
[源码补丁](results/20260920-fixed/production-fix.patch)，二进制和源码哈希见
[构建记录](results/20260920-fixed/matrix/build-manifest.json)。
修复前数据仍保留在 `results/20260920`，本次数据独立存放于 `results/20260920-fixed`。

## 修复内容

QEMU 的 `muldiv64(a, b, c)` 只有第一个参数为 64 位，后两个参数为 32 位。
skew 配置允许 IPS 达到 10^12，直接传参会截断。协调器实际间隔超过
2^32 纳秒（约 4.29 秒）时，原来的周期预算换算也会截断。

在 `accel/tcg/skew.c` 增加局部 `skew_muldiv()`，三个参数均为 64 位，
乘法和除法使用 128 位中间结果，超出 64 位的结果饱和到 UINT64_MAX。
替换窗口生成、模型时间换算、周期预算、有效进度换算四处调用。
权重和反馈比例使用的小整数常量仍可安全使用原工具函数。

## 验证结果

| 检查 | 范围 | 结果 |
|---|---|---|
| 重新编译 | aarch64-softmmu | 通过 |
| 实际函数算术测试 | 44 例；高 IPS、长间隔、溢出饱和；启用 UBSan | 通过 |
| QMP 窗口查询 | 8 个配置，200M 至 1T，含 2^32 前后 | 全部与整数公式一致 |
| 裸机运行 | 7 场景，每种二进制 1 次预热 + 7 次正式；98 次正式 | 全部通过 |
| 联合时钟轨迹 | 336,000 次正式插桩 CNTVCT 读取 | 公式、计数器换算、单调性、窗口检查均通过 |
| Linux jitter | 2G IPS、双核、从复位开始持续 skew；7 次正式启动 | init/use 全部通过，每次 256 次 64 字节读取 |
| 原有快速验收 | 暂停、idle/IRQ、执行计数与 CPU 控制 | 通过 |

7 个裸机场景是持续执行的 200M、2G、20G、2^32 IPS、1T IPS，
20G 的延迟注入饱和场景，以及双核交替读取。
持续执行确保 global_icount 确实增长，避免只测到零值而漏掉换算错误。
1T 场景使用 1 微秒窗口，其余参数见归档 manifest。

修复后，20G IPS、1ms 窗口对应 **20,000,000 条指令**；
修复前错误地得到 2,820,130 条。2^32 IPS 配置现在也可以正常启动。

窄窗口的上界平台期仍存在，本次不改变窗口策略。Linux 的通过结果对应上述
2G 配置，不能推断所有 IPS/负载都能通过 jitter，也不构成熵质量认证。
本次解决的是已复现的截断缺陷，不能据此认定所有高 IPS 启动问题都已解决。

## 复跑

在仓库根目录运行，输出目录使用新名称：

```sh
ninja -C build qemu-system-aarch64
python3 paper/experiments/exp5_visible_time/check_wide_arithmetic.py
python3 paper/experiments/exp5_visible_time/check_ips_boundary.py --out build/ips-new
python3 paper/experiments/exp5_visible_time/build_probe.py
python3 paper/experiments/exp5_visible_time/run_matrix.py --out build/matrix-new --repeats 7 --cases sustained-200m sustained-2g sustained-20g wide-boundary wide-max saturation cross-cpu
python3 paper/experiments/exp5_visible_time/run_reuse.py --out build/reuse-new --repeats 7
```

Linux 测试复用本机已有内核和模块，路径见 `run_reuse.py`；换机器需要准备对应资源。
归档包含命令、日志、压缩轨迹、构建清单和 SHA-256 校验，不包含编译产物与内核镜像。

# Skew clock

本分支以 QEMU 10.2.0 为基线。仓库只维护实现、实验测试脚本、必要的
guest/插件源码和设计说明；每次运行的日志、轨迹、图表和报告放在 `build/`。

## 构建与测试

Linux / WSL 环境需要 QEMU 常规构建依赖、Python 3、Ninja、AArch64 GCC
交叉工具链和 binutils。构建与依赖示例见[性能扫描说明](SKEW_SWEEP_zh.md)。
Linux guest 测试另需静态 AArch64 libc、cpio，以及版本匹配的内核与模块。
必须使用本仓库构建的 QEMU，不能用没有 skew 扩展的系统二进制代替。

在仓库根目录执行：

```sh
mkdir -p build
cd build
../configure --target-list=aarch64-softmmu --enable-plugins --enable-trace-backends=log
ninja qemu-system-aarch64 tests/unit/test-exec-budget
cd ..
mkdir -p build/skew-tmp
export TMPDIR="$PWD/build/skew-tmp"
build/tests/unit/test-exec-budget
python3 tests/tcg/aarch64/system/skew-check.py build/qemu-system-aarch64 \
    --quick --output build/skew-quick
```

- [测试入口、覆盖范围与结果判定](TEST_ACCEPTANCE_zh.md)
- [性能扫描与测量方法](SKEW_SWEEP_zh.md)
- [宿主耗时基准边界](SKEW_ICOUNT_HOST_BENCH_zh.md)
- [可见时间实验矩阵与独立 IPS 回归](../../paper/README.md)

新运行使用新的输出目录，保留失败和超时。构建、定向单测、真实 guest
集成测试和性能实验分别报告；任一项通过不能代替其余项目。

## 当前设计

- [时钟模型与窗口](DESIGN_zh.rst)
- [公共执行预算](EXECUTION_BUDGET_zh.md)
- [代码导读与同步边界](SKEW_CODE_WIKI_zh.md)
- [QMP 双向切换](SKEW_QMP_zh.md)
- [visible 阶段切换契约](SKEW_VISIBLE_SWITCH_zh.md)
- [动量插值与独立采样锚点](SKEW_VISIBLE_TIME_MOMENTUM_zh.rst)

## 历史结果

旧论文、示例、逐次实测归档及阶段报告不再随分支维护，可在
[整理前提交 17d49d3](https://github.com/Andy-HNU/qemu_skew_timer/tree/17d49d3d497ce9f2235c739acfedcca7e0a0e086)
中追溯。历史运行结果不代表当前 checkout 已通过测试。
旧 exp5 归档的校验清单包含三个未提交的补丁文件，不能声称该归档校验完整通过。
本次只整理工作树，不改写 Git 历史，也不补造历史证据。

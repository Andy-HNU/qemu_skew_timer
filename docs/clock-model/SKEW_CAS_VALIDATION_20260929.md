# CAS visible 发布改造与 WSL 验证（2026-09-29）

本次将协调器的 visible 直接覆盖改为与读者共用 CAS-max；保留 seqlock 元组发布、
现有 BQL 和 MTTCG 多线程执行，不新增读写锁、不改变模式切换或插值采样策略。
算法与锁审计见 [代码 Wiki 第 7 节](SKEW_CODE_WIKI_zh.md)。

## 源码与实际调用边界

测试起点为 `55764cd2e2`，提交前已同步远端 `d12fb318fa`；
两条新增远端提交只涉及论文文档，执行代码未变。
修改前对照通过提取 HEAD 的旧 skew.c、复用构建对象独立链接得到，
没有覆盖被测 CAS 二进制或工作区源码。二进制与源码哈希记录在配套 JSON。

锁审计确认 ARM CNTVCT/CNTPCT 使用 ARM_CP_IO，get_cp_reg/get_cp_reg64 持 BQL。
因此不能声称旧版本在这些 ARM 读取与协调器之间已复现无锁覆盖。
本次 CAS 加固通用 getter 的同阶段发布协议；无 BQL 交错由定向宿主测试覆盖。
x86 TSC 路径审阅见 Wiki，本次没有构建或运行 x86。

## 验证结果

- AArch64 QEMU 及实验 probe 构建通过。
- 实际 CAS helper + QEMU atomic.h 定向测试通过：强制 CAS 失败重试、
  旧预测晚到、新 model 下界、8 线程共 800,000 次发布；启用 UBSan。
- 8 个计数器场景：16 次预热、112 次正式运行全部通过。正式 probe 共
  364,000 次计数器读取；单调性、窗口、model 公式、斜率和计数器换算均通过。
  场景包括 sustained-200m/2g/20g、wide-boundary、wide-max、
  saturation、cross-cpu、dense。矩阵中的 baseline 指修改后的无插桩二进制，
  并不是修改前版本。
- 原有 quick 验收 11 个场景通过。
- Linux 模式往返：运行中直接切换与暂停后切换两轮通过，共 8 次转换；
  包含持续双核工作、System Counter、timerfd、暂停冻结、重复命令及 prelaunch 检查。
- Linux 持续 skew 首轮：预热 1/1 通过，正式 6/7 通过。
  repeat=5 已启动 Linux 并进入 /init，但 jitterentropy 初始化返回
  `Initialization failed with host not compliant with requirements: 2`。
  保留原始失败，不将随后成功覆盖为“全部通过”。
- 因此追加修改前/后交替对照，各 10 次：修改前 10/10、CAS 版本 10/10 通过，
  每次完成 jitter init 和 256 次 AF_ALG 读取。单次初始化失败未再次复现，
  现有证据不能确定其原因，也不能证明与 CAS 无关；不将其直接归因为宿主抖动。
- 原有完整功能验收 48 个场景全部通过，覆盖慢核、网络、异常/复位、普通 MTTCG 和传统 icount 对照。

这是一项功能与并发协议验证，不是发布同步的性能基准。
插桩会改变时序；jitter 通过也不证明熵质量、任意配置或全部并发交错。
同阶段证明不扩展到任意外部读者跨模式切换，详见 Wiki。

## 可复现入口

在仓库根目录执行；选择新的输出目录，不覆盖失败或历史结果。

```sh
ninja -C build qemu-system-aarch64
python3 tests/tcg/aarch64/system/skew-visible-cas.py
python3 paper/experiments/exp5_visible_time/build_probe.py
python3 paper/experiments/exp5_visible_time/run_matrix.py \
  --out build/skew-cas-20260929/matrix --repeats 7 \
  --cases sustained-200m sustained-2g sustained-20g wide-boundary wide-max saturation cross-cpu dense
python3 paper/experiments/exp5_visible_time/run_reuse.py \
  --out build/skew-cas-20260929/reuse --repeats 7
python3 tests/tcg/aarch64/system/skew-linux-switch.py build/qemu-system-aarch64 \
  --kernel build/linux-jitter-20260916/kernel/boot/vmlinuz-6.1.0-50-cloud-arm64 \
  --jitter-module build/linux-jitter-20260916/kernel/lib/modules/6.1.0-50-cloud-arm64/kernel/crypto/jitterentropy_rng.ko \
  --output build/skew-cas-20260929/switch
python3 tests/tcg/aarch64/system/skew-check.py build/qemu-system-aarch64 \
  --output build/skew-cas-20260929/full
```

原始数据保留于本机 `build/skew-cas-20260929/`：matrix、reuse、ab、switch、full。
修改前后对照的完整命令及二进制哈希在 `ab/results.json` 和 `ab/manifest.json`；
对照驱动脚本为 `build/skew-cas-ab.py`。
首轮失败串口为 `reuse/jitter-5/serial.log`，对应 QMP 记录仍保留。
注意旧 `run_reuse.py` 即使子测试失败也可能以 0 退出，
本报告逐项读取 jitter-summary.json，并未只依据父脚本退出码。
精简证据见 [CAS 验证摘要](SKEW_CAS_VALIDATION_20260929.json)。

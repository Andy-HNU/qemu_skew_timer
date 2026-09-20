# 实验 5：可见时间插值实测计划

2026-09-20，实验前登记。基线 4e894601b8；正式源码与 build/qemu-system-aarch64 保持不变。

## 复用与新增

1. 复用 tests/tcg/aarch64/system/skew-boot.S、skew.ld，新增短小裸机读时钟负载。
2. 复用 Linux 6.1.0-50-cloud-arm64 内核和 jitterentropy/AF_ALG 模块，以及现有
   skew-linux-jitter.py（init + 256 次 64 字节真实读取）。重新执行，不直接当作本轮结果。
3. 复用 skew-check.py 的快速验收，覆盖暂停冻结、idle/IRQ、计数和双核控制。
4. 仅在 build/exp5 中生成 skew.c / ARM helper.c 的实验副本，复用当前编译参数与
   其他对象链接独立二进制。保留源文件 hash、生成副本、编译/链接记录与 patch。

## 两层观测

**无插桩正式二进制**：执行 guest 自检和 Linux jitter，作为兼容性与观察扰动对照。
**实验二进制**：在 CNTVCT/CNTPCT 的寄存器读取路径捕获实际返回值与对应的
model/visible/host/slope；协调发布后记录 update，恢复执行记录 resume。
记录先写预分配内存，进程退出才输出 CSV，不在每次读取中做文件 I/O。
ARM 寄存器读取及协调事件必须持 BQL；记录不额外加锁，检查并报告不满足条件次数。
只对实际采集的顺序和场景下结论，不把测试通过当作并发单调性的形式化证明。

## 场景矩阵

每个主要场景计划 1 次预热、7 次正式运行，顺序按固定 seed 打乱：

- steady：单核固定计算间隔后读取，默认 200M IPS / 1ms window / 100us update。
- dense：连续密集读取，统计相同 counter 值和差值分布。
- high-ips：同一负载改为 2G、20G，观察 floor 与模型推进失配。
- saturation：增大 update 间隔，捕获固定 model 下抵达上界、重复读值的平台段；
  必要时降低 guest 读频率以便在预算耗尽前跨越足够宿主时间。
- cross-cpu：双核轮流读，共享握手形成因果顺序；对比每核和跨核倒退计数。
- idle/resume 和 pause：复用原有验收，另列覆盖结果，不冒充同一条联合轨迹。

保持场景指令工作量不依赖虚拟时间，以免计时模式改变工作本身。按宿主超时保护，
所有失败/超时保留日志。插桩时钟不用于性能结论；报告与无插桩执行耗时之比。

## 分析与验收

- 逐条统计 visible/model 倒退、abs(visible-model)>window、斜率超界。
- 对软件读取单独统计 counter 相邻重复率、model/visible 差值方差。
- 明确同轨迹 model 与 visible 是诊断对比，不是关闭插值后重新运行的消融实验。
- plateau 必须同时满足模型不动、visible==upper，且至少两个实际读值相同。
- 检查 bound_hit 前后保存的 slope：现有代码只 clamp，不能宣称读时主动改 slope。
- 图使用真实点和线段，无拟合；标注 update、read、upper/lower bound。
- 保存 manifest、逐次 CSV、guest 输出、汇总和 SVG/PDF 图；不混入 synthetic 数据。

## 明确不在本轮声称的结论

未测硬件等效时间；未证明密码学熵质量；未覆盖所有 kernel/IPS/window；
自然延迟与故障注入必须分别标识。
如果场景未触界，报告“未观测到”，不得用示意数据替代。

## 预跑后修订（正式运行之前）

build/exp5-pilot2 的七场景初测全部通过；长 update 没有捕获 bound_hit。
原因是 guest 先用完执行预算并等待，缺少触界期间的实际读取。
正式 saturation 增加明确故障注入：实验副本在每次 counter 读取之前持有原有
BQL 等待 100us，IPS=20G、window=10us、update=100ms、4000 次读取。
它用于确定性地制造“宿主经过、协调器尚未推进”的条件，验证硬边界行为；
不是正常执行性能样本，也不把它与无延迟基线的耗时比解释为插桩开销。
其余场景仅记录，不注入延迟。保留 pilot 的失败配置与零触界结果。

## 短轨迹观察后的补充批次

首批 2G/20G 的 4000 次短负载在 model 形成多周期历史前即完成，不能用于
稳定期 IPS 比较。因此另加 sustained-200m/2g/20g：相同 8000 次读取、每次
16384 次计算循环，保持 window/update 不变。另存批次，不覆盖短轨迹；每档
仍为一次预热、7 次正式、baseline/probe 对照。核对每次至少形成 8 个 update。

## 高 IPS 公式失败后的补充

长负载暴露 muldiv64 的 32 位参数截断。增加 2^32 前后参数查询检查，并对所有
已有 CSV 离线复算 model=G*1e9/配置IPS；原始 runs.json 的早期通过标签不覆盖
这项新检查，最终以 audit.json 为准。另在合法 2G IPS 下重做 saturation-2g
（仍 7 次正式），避免只依赖错误 IPS 换算的饱和轨迹；原 20G 数据与失败保留。

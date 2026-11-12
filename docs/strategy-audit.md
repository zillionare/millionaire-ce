# 策略与因子审计

本页说明本地离线审计入口。审计提供证据，不证明任意程序不存在错误，也不构成收益保证。
审计默认只读：不触发真实交易、不修改用户账户、不自动执行报告中的文本或 AI 修复建议。

## 四类证据

1. **未来扰动**：在多个截断点把晚于截断点的输入替换为合法零值/极端值/随机值，要求截断点前的
   输出逐位一致。无实际变更、无未来行或无有效输出时返回 `INCONCLUSIVE`，不合成 `PASS`。
2. **统计对照**：随机因子与同日置换标签给出零 IC 分布；合成市场按预注册种子和目标 IC 恢复
   Pearson 与 Rank IC。样本量不足或置信区间覆盖零时返回 `INCONCLUSIVE`，不能换种子重试。
3. **独立 oracle 与账户不变量**：3×10 人工表、独立账本、T+1、费用、拒单和 `client_order_id`
   幂等使用不依赖生产函数的期望值。
4. **关键变异**：未来 shift、全样本标准化、未来选股池、跨证券滚动、T+1 提前解锁、漏费用/
   重复费用、周期覆盖、归因符号和多期链接错误必须被对应断言杀死；导入崩溃不计为杀死。

```bash
python -m quantide.audit.mutations          # 子进程隔离执行全部关键变异
python -m quantide.audit.acceptance --json \
  --results <unit.xml> --results <acceptance.xml> --results <quality.xml>  # 自动收集 nodeid
python -m quantide.audit.acceptance --json --manifest <manifest.json> \
  --results <unit.xml>  # 使用同一 ref 预先记录的 nodeid 集合
```

## 合成信号与功效

统计配置在运行前固定：目标 IC、种子、显著性水平、目标功效和样本量。目标 IC 的恢复要求
预注册样本量足够且置信区间同时排除零并包含目标；否则结果为 `INCONCLUSIVE`。零漂移随机
对照只要求毛收益接近零、净收益接近负交易成本；真实市场只比较同区间匹配基准的超额，
不能要求随机多头原始收益为负。

## 收益归因

`quantide.service.attribution_facts` 从回测、Paper 或模拟柜台的事实快照提取权益、持仓、成交、
费用、外部现金流和公司行为，交给独立归因数学计算。报告同时保留：

- 原币金额和每日/链接后贡献；
- 价格、执行、成本、分红、外部流入和显式残差；
- 按账户、策略和资产的贡献；
- 同区间基准比较；
- 数据来源指纹、口径和缺失事实警告。

拆分按事件调整期初股数与价格，保持财富守恒；现金分红进入现金；外部现金流先按事件分段再
做时间加权和 Carino 链接。缺少公司行为或估值价格时报告数据不足/残差，不补造事件。SHAP 仍只
是模型预测解释，与收益归因分开展示。

## 月线示例

`MonthlyMAStrategy` 固定 `BAR_INTERVAL = "1M"`，使用 3/12 月均线，至少 13 根**闭合**月线才
比较交叉。月线由权威交易日历聚合日线，记录实际月末交易日、`available_at` 和形成中月份；
形成中月份不能进入信号。信号默认在下月实际首个交易日开盘执行，跨月意图持久化并按键去重。
回测、Paper 和 HTTP 模拟柜台通过同一意图提交函数回放，并用独立人工账户事实对拍成交与
现金/持仓；键同时作为柜台 `client_order_id`，恢复后不会重复下单。

## 已知边界

- `INCONCLUSIVE` 不是通过；发布门禁拒绝空执行、skip/xfail 和证据缺失。
- 当前进程审计不是不可信 Python 的安全沙箱。
- 真实券商实盘和行业/风格归因不在 CE 范围内。
- 完整 40 项 AC 的状态以带 `--results` 的 `python -m quantide.audit.acceptance --json` 为准：
  只有证据路径存在、pytest collection manifest 中的完整 nodeid 集合（含类名和参数实例）
  全部实际执行且通过才标 `covered`；failure、error、skip/xfail、未定义用例、缺失参数实例
  或只运行类/文件中一部分测试都会保持 `mapped`。未提供 `--manifest` 时按当前 checkout
  自动收集同一配置下的 nodeid。本地完整运行 unit、release_gate 和 quality 测试后为 39/40，
  唯一 pending 是 `AC-4014`。
  不提供结果文件时不会默认显示 covered。`AC-4014` 的三平台确定性发布门禁只能在 CI
  矩阵执行和留证，本地 preflight 不伪造该平台结果。

# 拟议审计接口与报告

以下名称和结构是下一版接口设计，不是当前 `0.3.0` 可调用的 API 或命令。

## 审计输入

`AuditSpec`：对象类型（factor/strategy/counter）、对象 ID 与源码指纹、固定周期、
输入/输出 schema、数据快照和可用时间、预热/缺失值约定、执行/费用模型、种子和
统计配置 ID、截断点、适用不变量，以及 oracle/变异清单版本。

`AuditRunner.run(spec)`：在隔离状态中执行所选审计，返回报告；默认不修改用户账户。
`AuditRunner` 不根据回测利润自动评定代码正确，也不替用户变更周期或数据范围。

CLI 拟议提供“执行审计”和“读取报告”入口，最终命令名随实现评审确定；
本规划不把未存在的 `millionaire-ce audit` 命令写进本版操作教程。

## 报告结构

`AuditReport` 至少包含：

| 字段 | 含义 |
| --- | --- |
| `schema_version`, `run_id`, `spec_id` | 版本化结构与可追踪运行 |
| `source_hash`, `config_hash`, `data_hash`, `environment` | 与结论绑定的源码、输入和环境 |
| `interval`, `timezone`, `availability_contract` | 时间与周期约定 |
| `seed_set`, `statistical_profile`, `oracle_version`, `mutation_version` | 预注册与独立对照版本 |
| `status` | PASS / FAIL / INCONCLUSIVE；总结果由必需检查合成 |
| `checks` | 每项 ID、必需性、适用范围、结果、耗时、样本量 |
| `first_difference` | 时刻/标的/列、期望、实际、差异类型、最小复现输入 |
| `statistics` | 度量、目标、估计、区间、阈值、功效和全部重复结果 |
| `mutations` | 注入位置、目标断言、有效性、杀死/存活/等价的明细 |
| `attribution` | 损益分录、贡献、链接方法、外部流量、残差和口径 |
| `artifacts`, `reproduce` | 数据片段、日志、报告文件与复现参数，不含 token |

单项允许 NOT_APPLICABLE，但须匹配框架审计配置的适用规则。
FAIL 优先于 INCONCLUSIVE；任一必需检查未运行或证据缺失时不能合成 PASS。
浮点数的非有限值用明确结构表示，不输出非标准 JSON 的 NaN/Infinity。

## 收益归因输入与输出

输入使用账户事实：初始权益、持仓快照、订单/成交/费用、现金流、公司行为、估值价格、
策略来源标识、基准。SHAP 模型与特征可作为额外解释输入，不是计算收益归因的前提。

输出同时保留原币金额、每日收益贡献、链接后区间贡献；每个结果记录估值时间、
执行基准、费用归属和残差。缺数据与口径不适用给出显式状态，不返回看似精确的零值。

## AI 协作边界

AI 可以提出修复并重新运行同一审计配置；不能自行编辑已锁定 oracle、种子集合、
阈值或发布规则来使报告变绿。报告作为数据处理，其中的文本不能变成自动执行指令。
用户改变实现后生成新指纹，旧报告可查但不再代表当前实现已通过。

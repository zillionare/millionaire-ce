# Millionaire 策略框架与运行模式 — Spec

- **Spec ID**: v0.2-001-strategy-framework
- **创建日期**: 2026-06-15
- **状态**: v0.2-001 spec 覆盖 FR-010 ~ FR-360 范围; tag `v0.2-001-internal-locked` 标记基线; **AC 闭环** 至 FR-010/013/014/015/020/125/135/360 + NFR-060; **AC 待补** FR-030~080/090~130/140~180/200/230~260/440/450(按 P2 分域推进); FR-185 占位(算法见 spec-strategy.md §FR-185, P1 推进)

## 用户故事

### US-010
story: 作为独立量化开发者，我想通过 SDK 编写策略，且让同一份代码在回测/仿真/实盘/dry-run 四种模式无修改运行
priority: P0

### US-020
story: 作为用户，我希望框架自动扫描指定目录，列出所有策略（含名称、描述、参数）
priority: P0

### US-030
story: 作为用户，我希望选择三种下单方式（cheat-on-close / 次日开盘 / 次日限价）之一
priority: P0

### US-040
story: 作为用户，我希望使用三种内置策略：双均线（日线）、回落卖出（风控）、成本止损（风控）
priority: P0

### US-050
story: 作为用户，我编写使用 1d/30m 周期的独立策略（如 30 分钟隔夜策略）时，框架通过 on_day_open / on_bar 驱动，两种周期均可回测；fill_timing 支持 cheat_on_close/next_open
priority: P0

### US-060
story: 作为用户，我希望风控策略跟随宿主策略生命周期自动激活，且不占独立账户
priority: P0

### US-070
story: 作为用户，我使用回测参数启动策略后，转仿真/实盘时参数自动带入
priority: P0

### US-080
story: 作为用户，我可以为每次运行单独设置运行时参数（本金/滑点/手续费），不被回测结果锁定
priority: P0

### US-090
story: 作为用户，我希望 dry-run 作为陪跑影子与实际策略并行运行并对比净值，以便判断策略表现
priority: P1

### US-100
story: 作为用户，我使用 tushare + qmt-gateway 两种数据源，前者用于历史与参考数据、后者用于实时
priority: P0

### US-110
story: 作为用户，我通过 Web UI 的策略选择器查看任一运行中策略的账户/委托/成交
priority: P0

### US-120
story: 作为用户，我在策略/委托/成交事件发生时收到微信通知
priority: P0

## 用户使用场景

### scenario-010 双均线 cheat-on-close 回测

选择双均线策略 + cheat-on-close 下单 + fast=5 / slow=20 + 回测起止 + 运行时本金。系统每日计算 MA5/MA20，cross-over 时发出买卖信号，T+0 收盘价成交。回测实时显示进度与净值曲线（按 on_bar 推进），结束后输出完整报告（指标 + 4 类图表）。

### scenario-020 独立策略（30 分钟隔夜）上实盘

编写 `OvernightStrategy(BaseStrategy)`（直接继承 BaseStrategy，使用 `frame_type="30m"` 数据），可先回测，再在仿真界面运行 1 周 → 切到实盘 → 自动创建并行仿真 → 策略在实盘正常发单。dry-run 默认开启，作为陪跑影子与实盘净值对照，不暂停实际策略。

### scenario-030 风控策略叠加

独立策略进入实盘时，宿主可挂载由当前宿主显式注册的 `RiskStrategy` 风控程序。成交归属宿主策略的虚拟账本；风险事件与超额收益仍遵循通用 `RiskStrategy` 接口。CE 不内置或自动导入任何私有风险程序，未安装程序时保持空选择。

### scenario-031 回测中的 Broker 层风控 Overlay

用户在 1d 或 30m 宿主回测中选择已注册的兼容风控及参数；每个 bar 先按可用历史数据执行有序 overlay composite，再执行宿主。`allow_same_bar_repurchase` 明确控制同 bar 重买，T+1 仍适用。overlay 与宿主共用 `portfolio_id`，回测不产生风控超额收益指标；旧的未注册 ID 显示不可用/阻止。公司行为按拆股假设处理：`shares×r`、`cost÷r`、`cash` 不变；回测不扣红利税。


## 索引

本 spec 已按域拆分为 3 个分册(FR 编号不变, 引用稳定):

| FR 范围 | 所属分册 | 说明 |
|---|---|---|
| FR-010 ~ FR-080 | [spec-strategy.md](./spec-strategy.md) | 策略对象模型、SDK 暴露、下单方式、跨模式差异 |
| FR-090 ~ FR-110 | [spec-strategy.md](./spec-strategy.md) | 内置策略(双均线/回落卖出/成本止损) |
| FR-115 ~ FR-130 | [spec-strategy.md](./spec-strategy.md) | 驱动契约 + 风控契约 |
| FR-140 ~ FR-180 | [spec-trading.md](./spec-trading.md) | 交易规则(涨跌停/数量/时间/特殊状态/资金) |
| FR-185 | [spec-strategy.md](./spec-strategy.md) | 持仓成本基准(含交易费用的加权均价算法) |
| FR-190 | [spec-trading.md](./spec-trading.md) | 回测 vs 实盘差异 |
| FR-200 | [spec-strategy.md](./spec-strategy.md) | 运行时参数(策略层) |
| FR-210 ~ FR-220 | [spec-trading.md](./spec-trading.md) | 虚拟账本 + 手工交易归属 |
| FR-230 ~ FR-260 | [spec-strategy.md](./spec-strategy.md) | 调度(回测启动路径/无回测启动/风控调度/调度 UI) |
| FR-270 ~ FR-485 | [spec-trading.md](./spec-trading.md) | 数据源、评估、账户、UI、通知、安装、端口与工具补充 |
| NFR-010 ~ NFR-060 | [spec-foundation.md](./spec-foundation.md) | 非功能需求 |
| 已知约束 / 排除项 / 推迟 | [spec-foundation.md](./spec-foundation.md) | 范围声明 |

**Coverage cutoff**: 本 spec 当前定稿至 FR-260 (含 FR-440/450), FR-270 ~ FR-470 视为下一轮增量. FR-185 (持仓成本基准) 已解 (P1 经典方案 A, 见 [spec-strategy.md §FR-185](./spec-strategy.md)). 全部已锁定 FR 均已建 issue (Sage cycle 1+2: 33 个 issue 覆盖 FR-010~250 + FR-360/440/450).

**Coverage 进度** (2026-06-21 校准, 反映 P0-P2 工作):
- ✅ FR-010 ~ FR-185: spec + acceptance + test-plan + interfaces 四文档已对齐 (内审锁定)
- ✅ FR-190 ~ FR-260 + FR-440/450: spec + acceptance + test-plan 已补齐, 跨越 P2 域 1 (FR-030~080 调度/下单方式) / 域 2 (FR-090~110 内置策略) / 域 3 (FR-115/130 驱动契约) / 域 4 (FR-140~190 交易规则) / 域 5 (FR-200~250 运行时参数+调度+通知); FR-260/FR-460 调度 UI/系统配置已迁移 [v0.2-002-ui](../v0.2-002-ui/spec.md)
- ✅ NFR-060: 运行时注入契约, 见 [interfaces.md §7.4](./interfaces.md)
- ✅ FR-270 ~ FR-470: spec + acceptance 已补齐, 覆盖数据同步/评估/账户/通知/安装等扩展域
- ✅ FR-480/481/484/485: 基于 reverse coverage map 新增, 覆盖重采样、通知通道、研究辅助与证券代码工具
- ✅ FR-185: 经典加权均价方案 A (P1 已解), 见 [spec-strategy.md §FR-185](./spec-strategy.md)

#### v0.2-001 已锁定 FR (有锚点, 已建 issue)

| FR | 标题 | 锚点 | Issue |
|---|---|---|---|
| [FR-010](./spec-strategy.md#fr-010) | 策略对象模型与 SDK 暴露 | spec-strategy.md#fr-010 | [#55](https://github.com/zillionare/millionaire/issues/55) |
| [FR-013](./spec-strategy.md#fr-013) | RiskStrategy 结构契约 | spec-strategy.md#fr-013 | [#56](https://github.com/zillionare/millionaire/issues/56) |
| [FR-014](./spec-strategy.md#fr-014) | SDK 元数据接口 — 交易日历 | spec-strategy.md#fr-014 | [#57](https://github.com/zillionare/millionaire/issues/57) |
| [FR-015](./spec-strategy.md#fr-015) | SDK 元数据接口 — 证券列表 | spec-strategy.md#fr-015 | [#58](https://github.com/zillionare/millionaire/issues/58) |
| [FR-020](./spec-strategy.md#fr-020) | 自动发现策略 | spec-strategy.md#fr-020 | [#59](https://github.com/zillionare/millionaire/issues/59) |
| [FR-030](./spec-strategy.md#fr-030) | 策略参数跨模式透传 | spec-strategy.md#fr-030 | [#60](https://github.com/zillionare/millionaire/issues/60) |
| [FR-040](./spec-strategy.md#fr-040) | 一次开发四模式无感迁移 | spec-strategy.md#fr-040 | [#61](https://github.com/zillionare/millionaire/issues/61) |
| [FR-050](./spec-strategy.md#fr-050) | 下单方式 — cheat-on-close | spec-strategy.md#fr-050 | [#62](https://github.com/zillionare/millionaire/issues/62) |
| [FR-055](./spec-strategy.md#fr-055) | 运行周期、决策与成交时点 | spec-strategy.md#fr-055 | 本 FR 新增，issue 待建 |
| [FR-060](./spec-strategy.md#fr-060) | 下单方式 — 次日开盘（正常模式） | spec-strategy.md#fr-060 | [#63](https://github.com/zillionare/millionaire/issues/63) |
| [FR-070](./spec-strategy.md#fr-070) | 下单方式 — 次日限价 | spec-strategy.md#fr-070 | [#64](https://github.com/zillionare/millionaire/issues/64) |
| [FR-080](./spec-strategy.md#fr-080) | 跨模式回测-实盘差异（声明性） | spec-strategy.md#fr-080 | [#65](https://github.com/zillionare/millionaire/issues/65) |
| [FR-090](./spec-strategy.md#fr-090) | 内置策略 — 双均线（日线策略） | spec-strategy.md#fr-090 | [#66](https://github.com/zillionare/millionaire/issues/66) |
| [FR-100](./spec-strategy.md#fr-100) | 内置策略 — 回落卖出（风控） | spec-strategy.md#fr-100 | [#67](https://github.com/zillionare/millionaire/issues/67) |
| [FR-110](./spec-strategy.md#fr-110) | 内置策略 — 成本止损（风控） | spec-strategy.md#fr-110 | [#68](https://github.com/zillionare/millionaire/issues/68) |
| [FR-115](./spec-strategy.md#fr-115) | BaseStrategy 驱动契约（独立策略默认） | spec-strategy.md#fr-115 | [#69](https://github.com/zillionare/millionaire/issues/69) |
| [FR-125](./spec-strategy.md#fr-125) | 风控策略驱动契约 | spec-strategy.md#fr-125 | [#70](https://github.com/zillionare/millionaire/issues/70) |
| [FR-130](./spec-strategy.md#fr-130) | 风控策略契约（生命周期 + 宿主回测） | spec-strategy.md#fr-130 | [#71](https://github.com/zillionare/millionaire/issues/71) |
| [FR-135](./spec-strategy.md#fr-135) | 可回测的 Broker 层风控 Overlay | spec-strategy.md#fr-135 | 本 FR 新增，issue 待建 |
| [FR-140](./spec-trading.md#fr-140) | 交易规则 — 价格（涨跌停） | spec-trading.md#fr-140 | [#72](https://github.com/zillionare/millionaire/issues/72) |
| [FR-150](./spec-trading.md#fr-150) | 交易规则 — 数量 | spec-trading.md#fr-150 | [#73](https://github.com/zillionare/millionaire/issues/73) |
| [FR-160](./spec-trading.md#fr-160) | 交易规则 — 时间 | spec-trading.md#fr-160 | [#74](https://github.com/zillionare/millionaire/issues/74) |
| [FR-170](./spec-trading.md#fr-170) | 交易规则 — 特殊状态（停牌） | spec-trading.md#fr-170 | [#75](https://github.com/zillionare/millionaire/issues/75) |
| [FR-180](./spec-trading.md#fr-180) | 交易规则 — 资金 | spec-trading.md#fr-180 | [#76](https://github.com/zillionare/millionaire/issues/76) |
| [FR-185](./spec-strategy.md#fr-185) | 持仓成本基准（含交易费用的加权均价 — 经典方案 A, P1 已解） | spec-strategy.md#fr-185 | [#77](https://github.com/zillionare/millionaire/issues/77) |
| [FR-190](./spec-trading.md#fr-190) | 交易规则 — 回测 vs 实盘的差异 | spec-trading.md#fr-190 | [#78](https://github.com/zillionare/millionaire/issues/78) |
| [FR-200](./spec-strategy.md#fr-200) | 运行时参数（框架管理，策略不可见） | spec-strategy.md#fr-200 | [#79](https://github.com/zillionare/millionaire/issues/79) |
| [FR-210](./spec-strategy.md#fr-210) | 虚拟账本（按 qtoid 归因） | spec-strategy.md#fr-210 | [#80](https://github.com/zillionare/millionaire/issues/80) |
| [FR-220](./spec-strategy.md#fr-220) | 手工交易/补单/风控卖出的归属 | spec-strategy.md#fr-220 | [#81](https://github.com/zillionare/millionaire/issues/81) |
| [FR-230](./spec-strategy.md#fr-230) | 独立策略的回测启动路径 | spec-strategy.md#fr-230 | [#82](https://github.com/zillionare/millionaire/issues/82) |
| [FR-240](./spec-strategy.md#fr-240) | 独立策略的无回测启动路径 | spec-strategy.md#fr-240 | [#83](https://github.com/zillionare/millionaire/issues/83) |
| [FR-250](./spec-strategy.md#fr-250) | 风控策略调度 | spec-strategy.md#fr-250 | [#84](https://github.com/zillionare/millionaire/issues/84) |
| [FR-360](./spec-trading.md#fr-360) | 评估指标 — 风控策略（Triple Barrier） | spec-trading.md#fr-360 | [#85](https://github.com/zillionare/millionaire/issues/85) |
| [FR-440](./spec-trading.md#fr-440) | dry-run 陪跑影子 | spec-trading.md#fr-440 | [#86](https://github.com/zillionare/millionaire/issues/86) |
| [FR-450](./spec-strategy.md#fr-450) | 消息通知（微信）— 事件定义 | spec-strategy.md#fr-450 | [#87](https://github.com/zillionare/millionaire/issues/87) |

> **Issue 格式** (Sage cycle 1 决策, 2026-06-17; cycle 2 调整, 2026-06-22):
>
> **Sage cycle 1** (FR-010/013/014/015/020, #55-#59) — 旧格式:
> - **Body**: `### 需求 ID` + `### Spec 链接` + **Spec 段落全文** (自包含, 不依赖外部文档)
> - **第一条 reply**: `## 验收标准 (AC 列表)` 完整从 acceptance.md 复制, 每条 AC 带 `AC-1, AC-2, ...` 编号
> - **关联 Project**: `Millionaire 0.2` (#5, 位于 zillionare user 下)
>
> **Sage cycle 2** (FR-030~250 + FR-360/440/450, #60-#87, 2026-06-22) — **新格式** (GitHub 文档内链接):
> - **Body**: `## 需求 ID` + `## 文档链接` (Spec/Acceptance/Test-plan/Interfaces 4 个文档锚点链接, **不复制内容**)
> - **理由**: spec 文档在 PR 中可能更新 (如 v0.2-001 流程 F1~F5 修了 spec), issue body 不必同步, 最多加 comment 提示文档有更新. AC 验证时直接读最新 acceptance.md.
> - **关联 Project**: `Millionaire 0.2` (#5, zillionare user) — 关联操作待 gh token scope (`read:project`) 补充后单独 batch.
>
> **范围说明** (2026-06-22 校准):
> - Sage cycle 1 (2026-06-17): FR-010/013/014/015/020 (5 issue)
> - Sage cycle 2 (2026-06-22): FR-030/040/050/060/070/080/090/100/110/115/125/130/140/150/160/170/180/185/190/200/210/220/230/240/250/360/440/450 (28 issue)
> - 排除: FR-011/FR-012 已删; FR-260 已迁 v0.2-002-ui; FR-270~470 (除 FR-360 外) 标 ⏸ deferral
> - 全部已锁定 FR (FR-010~250 + FR-360/440/450) 均已建 issue, 共 33 个 (#55-#87)

---

## 跨分册引用约定

- 引用具体 FR 时使用: `(见 [spec-strategy.md §FR-115](./spec-strategy.md))`
- 引用整个分册时使用: `(见 [spec-strategy.md](./spec-strategy.md))`
- 不再使用"见上一节" / "见下一节" 等位置性引用

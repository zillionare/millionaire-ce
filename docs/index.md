# Millionaire CE 用户手册

Millionaire CE 是面向 A 股的开源量化框架。你可以从自带策略开始，把精力放在因子和
交易逻辑上，复用历史数据、回测、参数优化、预测解释及持续仿真的流程。

## 核心能力

**研究后的策略可以沿用。** 策略通过统一 Broker 接口访问资金、持仓和委托；切换回测、
仿真和 HTTP 模拟柜台时复用策略逻辑。CE 用独立模拟柜台验证第三条路径，真实交易
由 Millionaire 或用户宿主扩展提供。执行模型一致不保证每次成交和收益相同。

**用户可以适配自己的券商柜台。** HTTP 接口将柜台与策略进程分开。懂编程的用户可
自行适配，不熟悉编程的用户可让 AI 依据接口、参考实现和验收用例协助完成。
从[柜台适配指南](broker-adapters.md)了解路径；CE 本身不连接真实账户。

**数据供应商可以替换。** 历史数据和行情扩展转换为统一字段、时间和单位；策略无需
解析不同供应商响应。支持范围和复权前提见[数据约定](data.md)。

**策略周期是固有属性。** 日线策略始终按日线运行，30 分钟策略始终按 30 分钟运行。
运行器拒绝不匹配的配置，减少策略编写时需要处理的情况。成交时序、T+1、涨跌停和
资金检查一起构成[策略防呆](execution.md)。

**LightGBM 示例串起研究流程。** 从现有因子选择开始，复用训练、调仓、参数优化与
SHAP 预测解释，再逐步添加自己的因子。见[因子与 LightGBM](factors-and-ml.md)。

## 推荐阅读顺序

| 你现在要做什么 | 入口 |
| --- | --- |
| 第一次使用 | [快速开始](quickstart.md) → [安装](native-installation.md) → [初始化](initialization.md) |
| 验证策略想法 | [第一次回测与报告](backtesting.md) → [成交时序和防呆](execution.md) |
| 开发策略或因子 | [编写策略](strategies.md) → [因子与 LightGBM](factors-and-ml.md) |
| 研究和比较结果 | [因子分析](factor-analysis.md) → [参数优化](optimization.md) → [预测解释](prediction-explanation.md) |
| 连续运行 | [仿真](paper-trading.md) → [启停、升级与恢复](operations.md) |
| 自行接入数据/柜台 | [行情扩展](quote-extensions.md) → [柜台适配](broker-adapters.md) |
| 遇到问题 | [常见问题](troubleshooting.md) |

## 阅读前确认

本手册对应 `0.3.0` 交付线。支持 macOS Apple Silicon（ARM64）、Ubuntu x86_64、
Windows x64；原生包自带运行环境。安装包是否已经发布，以
[Releases](https://github.com/zillionare/millionaire-ce/releases)中的实际文件为准。

CE 不提供真实交易，不内置三重障碍等私有风控程序。腾讯实时演示源默认关闭，
用户可选择一次固定 30 天演示，到期使用自定义行情扩展。
完整收益归因与 AI 审计尚属[下一版规划](roadmap.md)，不是本版已有功能。
更完整的交付与验证范围见[CE 能力边界](scope.md)。

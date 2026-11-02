# 代码质量与发布门禁

本流程参考 `~/workspace/tracks` 的 `.flake8`、`pyproject.toml`、pre-commit、
CI 发布依赖，以及 D-17/D-18 的“先验证、再重构”和外部依赖三层验证规则。
本仓库使用相同的工具分工，未引入 Tracks 运行时或 Agent 编排依赖。

## 固定要求

| 范围 | 工具/规则 | 上限或要求 |
| --- | --- | --- |
| 认知复杂度 | Flake8 + flake8-cognitive-complexity / CCR001 | 每函数 ≤15 |
| 代码复用 | Pylint / R0801 | 禁止超过 5 行的相同代码块；忽略注释、文档、导入和签名 |
| 生产文件长度 | Pylint / C0302 | 每文件 ≤1000 物理行 |
| 代码行宽 | Ruff / E501 | ≤100 字符，适用 Ruff 原生的 URL 等例外规则 |
| 方法规模 | Pylint / R0915 | ≤50 条语句 |
| 局部变量 | Pylint / R0914 | ≤15 个 |
| 常规 lint | Ruff | 保留 pyproject 中的 E/F/W/I/B/UP/D 检查 |
| 单元测试覆盖率 | pytest-cov | ≥80%，保留原有覆盖率门槛 |
| 发布验收 | pytest + JUnit 校验 | 非空、全通过、没有 skip/xfail |

“复用率”没有可靠的通用百分比定义，因此按 Tracks 的重复代码规则约束复用；
不把 `100%-复制率` 当成复用率。领域相同的逻辑应提取到具有明确归属的模块，
不要为了压低数字制造没有职责边界的通用工具集合。

完整生产检查范围是 `quantide/**/*.py` 与 `scripts/**/*.py`。
扫描器显式枚举文件，包含没有 `__init__.py` 的命名空间目录，避免 Pylint
只递归普通包而漏掉 `quantide/service/`。重复检测始终比较完整生产代码，
即使当前提交只修改一个文件。

测试代码不承担生产代码的认知复杂度、文件大小和方法预算，避免为满足指标
拆散行为断言；测试通过率、断网隔离和验收报告仍由 CI 严格控制。

不设置历史基线、不按现有最大值放宽阈值、不自动忽略历史超标项。
认知复杂度检查禁用 noqa 豁免。其他质量配置和局部 suppression 的变更需要
代码审查，不能以添加 ignore/exclude 替代重构。

## 安装与本地使用

要求 Python 3.13。质量工具的精确版本在 `pyproject.toml` 的 quality 组，
Poetry 与 PEP 735 分组保持一致；`poetry.lock`、`uv.lock` 一并提交。

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[ml30m]' --group quality --group test
python -m pre_commit install
```

Windows 激活命令为 `.venv\Scripts\Activate.ps1`。
Poetry 用户可用 `poetry install --with quality,test --without doc`。

```bash
# 与 CI 完全相同；非零退出码即不通过
python scripts/check_quality.py

# 验证门禁确实拒绝复杂代码、复制、大文件、长行及不合格的验收报告
python -m unittest discover -s tests/quality -v
```

提交前保持项目虚拟环境已激活，pre-commit 的本地检查使用该环境中的 Python。
原有 Ruff 自动格式化和 MyPy hook 保留。生产质量 hook 不运行测试，允许
Red-Green-Refactor 中先提交失败的测试；生产代码提交仍须满足结构预算。

结果保存在 `quality-reports/quality.json`，包括工具版本、完整文件数、源码和
配置摘要、每项检查退出码及原始输出。工具缺失、版本不匹配、扫描超时、
源码清单为空均失败；一个工具失败后仍执行其他扫描以提供完整反馈。
此目录及 review 材料不进入 CE 发布产物。

## 日常 CI

`.github/workflows/ci.yml` 是 PR、分支 push、手工及 release 共用入口：

1. `quality`：安装精确版本的工具，运行门禁反例，然后执行完整生产质量扫描。
2. `tests (ubuntu-latest)`、`tests (macos-latest)`、`tests (windows-latest)`：
   Python 3.13，构建 wheel/sdist 并验证 CE 内容，安装构建的 wheel 和 ML extra，
   在源码目录之外检查已安装 CLI/应用，再运行全部单元测试及 80% 覆盖率门禁。
   macOS 安装 LightGBM 所需的 OpenMP。三个平台不互相提前取消。
3. `acceptance`：运行 CE release_gate 测试，使用 `--runxfail`，把原先的
   expected failure 当成真实失败；随后解析 JUnit，拒绝没有实际执行的用例、
   失败、错误、skip 和 xfail。

日常单元测试与验收使用 pytest-socket 阻止外网，仅允许本地回环地址与 Unix
socket，供本地 gateway stub/事件循环使用。日常 CI 不读取 Tushare 密钥。
未 mock 的真实请求应修复测试隔离，不能依靠真实服务恰好可用使单测通过。

建议 GitHub 分支保护把上述五个检查设为 required，并限制直接推送默认分支。
仓库文件不能替代 GitHub 服务端的分支保护设置；本次没有修改远端规则。

## 外部数据与发布

采用 Tracks 的三层机制：日常离线检查、周期真实验证、发布硬门禁。

- `.github/workflows/nightly.yml` 每周一 UTC 03:00 跑完整回归和真实 Tushare
  小样本检查，也支持手工运行。未配置 token 时真实源用例明确输出
  `LIVE_SKIPPED: missing TUSHARE_TOKEN`；该周期检查不是合并凭据门槛。
- `.github/workflows/release.yml` 响应 `v*` tag，也允许手工运行完整预检（不发布）。
  先对同一个 ref 的源码调用
  完整 CI，并调用严格真实数据检查；只有二者都成功，`publish` 才运行。
- tag 必须等于 `v` 加 pyproject 包版本；发布 job 下载同次 CI 已安装和测试的
  `package-ubuntu-latest` 产物，再次校验后创建 GitHub Release，不重新构建另一份
  未测试的包；配置了 `PYPI_API_TOKEN` 时额外发布 PyPI。
- tag 的真实数据检查缺凭据时失败；空报告、全跳过、xfail 都不能放行。

GitHub Actions secrets：

| secret | 用途 |
| --- | --- |
| `TUSHARE_TOKEN` | 每周/发版的真实源小样本检查；发版必需 |
| `PYPI_API_TOKEN` | 可选；配置后发布 PyPI |

密钥从环境变量读取，不写入仓库或 SDK token 文件，不进入质量报告。
本机环境变量不会自动上传为 GitHub secret。本仓库的 TUSHARE_TOKEN 已配置在
GitHub 的 `CI` environment；真实源 job 显式绑定该环境，也允许手工执行严格验证。

```bash
# 本机显式执行真实源检查；要求已设置 TUSHARE_TOKEN
QUANTIDE_REQUIRE_LIVE_DATA=1 python -m pytest tests/live -m live_data \
  --runxfail --timeout=30 --junitxml=quality-reports/live-data.xml
python scripts/check_test_results.py quality-reports/live-data.xml
```

真实源检查只读取 2024-01-02 至 2024-01-03 的 SSE 日历和 `000001.SZ` 日线，
不下单、不抓取全市场，也不声称验证全部 Tushare 权限、BaoStock/Tencent 覆盖或
三平台长期运行。QMT 真机测试不作为 CE 发布条件。

## 重构与证据

原有大模块已按交易事务、意图对账、数据覆盖、任务生命周期、策略配置及页面组件
拆分；预算保持上表数值，不使用历史豁免。质量扫描、单元覆盖率和严格验收均需要
在最终源码重新执行，结果不能复用到后续修改的 wheel 或安装器。

`review/2026-11-02/REPORT.md` 保留原始审查事实；实施结果和尚未验证的运行边界记录
在同目录的 `IMPLEMENTATION.md`。多交易日无人值守和真实系统断电验收需要单独的
现场证据；自动进程重启测试不替代这些证据。

原生安装门禁还会实际创建控制器，操作启停按钮、检查浏览器目标地址，并验证
关闭窗口后后台服务继续运行；升级与默认卸载必须保留用户扩展。

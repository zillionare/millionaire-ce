# 数据服务完整设计（发行版）

> 目标：为 Quantide 发行版提供**可插拔数据源**、**高性能本地存储**、**统一行情契约**、
> **仅模拟交易**的执行链路，并支持**集合竞价收盘价买入**。
> 本设计基于对现有代码的实测（Pro 机器，2026-10-22），所有数据源结论均已验证。

---

## 0. 现状盘点（实测结论）

| 项 | 现状 | 证据 |
|---|---|---|
| 因子库 | **不存在**。`quantide/factors/` 只有 `alpha.py`（因子计算）+ `catalog.py`（清单）+ `alphatest.py`（单因子检验），是**策略内部用的因子函数**，不是独立的因子库服务 | `ls quantide/factors/` |
| 本地存储 | Parquet + polars，按 `partition_key_year` 年分区，单文件 `part-0.parquet` | `quantide/data/stores/base.py` |
| 日线数据量 | 3,914,025 行（5678 只 × 724 交易日，2023-10-23 起） | Pro 实测 |
| 指数行情 | **存储层已存在但数据为空**（`data/bars/index/` 空目录）；`IndexBarsStore` 已实现 | `quantide/data/stores/index_bars.py` |
| 指数成分 | **完全不存在** | grep 无 `index_weight` |
| 行情端口 | `MarketDataPort` 协议 + `QuoteSnapshot` 数据类**已定义好**（symbol/price/open/high/low/volume/amount/ts） | `quantide/core/ports/market_data.py` |
| 行情实现 | 只有 `LiveQuote`（gateway websocket 模式），Pro 上 `ws:///ws/quotes` 无 host → **从未连通过** | Pro 日志 |
| 模拟撮合 | `PaperBroker._try_match` 消费 `lastPrice/high/low/volume` → **行情字段需求已明确** | `quantide/service/sim_broker.py:772` |
| 同步任务 | `data_market.py` 的 `daily_bars.fetch_with_daily_progress`（逐日拉取 + 进度 SSE）→ 同步的是**个股日线 + 市值** | `quantide/web/pages/data_market.py:313` |
| 存储性能基线 | 面板读 80 天全市场（31 万行）**0.002s**；单资产 120 天 **0.003s** | Pro 实测 |

---

## 1. 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ L4 应用层：回测 / 参数优化 / Paper / 归因 / 因子台账            │
├─────────────────────────────────────────────────────────────┤
│ L3 领域服务层（新增）                                          │
│   · DataService      统一数据门面（回测/优化器只认它）            │
│   · MarketDataHub    统一行情契约（用户接入的唯一入口）            │
│   · ExecutionClock   交易日历 + 集合竞价时序（决定何时算信号/下单）  │
├─────────────────────────────────────────────────────────────┤
│ L2 存储层（本地，只读快照 + 列存）                              │
│   · DailyBarsStore   个股日线（年分区 + 列裁剪 + 统计信息）          │
│   · IndexBarsStore   指数日线（复用）                           │
│   · IndexWeightStore 指数成分快照 PIT（新增，按月分区）             │
│   · DailyBasicStore  市值/换手（已有）                           │
│   · StockList        证券主数据（已有）                          │
│   · LimitPriceStore  涨跌停价（新增：撮合必需，当前只在内存）        │
├─────────────────────────────────────────────────────────────┤
│ L1 数据源层（可插拔）                                          │
│   · TushareFetcher   日线/复权/市值/ST/涨跌停/指数/指数成分/分钟     │
│   · 未来可插 AkShare / BaoStock / 自建                           │
├─────────────────────────────────────────────────────────────┤
│ L0 实时层（用户自备）                                          │
│   · MarketDataProvider 协议 → 用户实现的适配器（HTTP/WS/SDK）      │
│   · 官方参考实现：AkshareProvider（免费、够用）                   │
│   · 内置兜底：TencentProvider / SinaProvider（离线也能跑通 Paper）  │
└─────────────────────────────────────────────────────────────┘
```

**关键原则**
1. **回测/优化器只依赖 L2/L3，不直接碰 L1** → 换数据源不影响策略与回测语义。
2. **实时行情只有一条通路**（L0 → L3 MarketDataHub → PaperBroker）→ Paper 消费的数据格式**固定统一**。
3. **只支持模拟交易**：删除/禁用 live 交易路径（券商网关），保留 gateway 仅作"未来可选"。

---

## 2. 成分股信息：怎么取、怎么存

### 2.1 取（已实测，含一个关键坑）

Tushare `index_weight`：**按月快照 = 天然 PIT ✅**。相邻快照成分变动实测"新进 201 / 剔除 1"，
用 `trade_date <= 决策日` 取最近一期 → **无前视**。

**⚠️ 关键坑：区间查询有 7000 行硬上限**（滚动窗口），实测：

```
index_weight("000300.SH", 20150101~20261022) → 7000 行 = 24 期，最早 2024-10-31
分段查询也无效（分两段都还是 7000 行，各自覆盖最近 24 期）
```

**✅ 破解办法：逐期按 `trade_date` 精确查询**（不受上限约束）：

```
index_weight("000300.SH", trade_date="20240131") → 300 行 ✅
index_weight("000300.SH", trade_date="20230131") → 300 行 ✅
index_weight("000300.SH", start_date="20150101", end_date="20151231") → 3600 行(12期) ✅
```

**实测各指数 PIT 历史深度**（逐期探测 2010~2026，每年 3 个采样点）：

| 指数 | 代码 | 每年成分数 | PIT 快照可得范围 | 备注 |
|---|---|---|---|---|
| 沪深300 | `000300.SH` | 300 | **2010 ~ 2026** ✅ | 全历史可用 |
| 中证500 | `000905.SH` | 500 | **2010 ~ 2026** ✅ | 全历史可用 |
| 创业板指 | `399006.SZ` | 100 | **2010 ~ 2026** ✅ | 全历史可用 |
| 中证A100 | `000903.SH` | 100 | **2010 ~ 2026** ✅ | 全历史可用 |
| 上证50 | `000016.SH` | 50 | **2010 ~ 2026** ✅ | 全历史可用 |
| 中证1000 | `000852.SH` | 1000 | **2015 ~ 2026** | 指数 2015 才发布 |
| 中证2000 | `932000.CSI` | 2000 | **2023 ~ 2026** | 指数 2023 才发布 |

→ **同步策略**：按"月份"逐期拉（不是按区间拉）→ 每个快照日 1 次调用 →
17 年 × 12 期 = **204 次调用/指数** → 7 个指数 ≈ 1400 次调用，按 3 次/秒 ≈ **8 分钟** ✅

**其它接口探测结论**：
- `index_member_all` → 是**申万行业分类**（801xxx.SI），不是宽基指数 ❌
- `index_stock_cons` / `index_stock_cons_csindex` / `index_weight_all` → 接口不存在 ❌
- `ths_member` → 空 ❌（同花顺在另一个接口族）

### 2.2 存（关键设计）

**不要**存成"指数 → 成分列表"的宽表。存成**长表事件流**：

```
data/index_weight/partition_key_year=2024/part-0.parquet
  date        (Date)   ← 快照日（月末）
  index_code  (Utf8)   ← 000300.SH
  con_code    (Utf8)   ← 600519.SH
  weight      (Float64)
```

**为什么这样存**（4 个理由，都是实测驱动的）：

| 理由 | 说明 |
|---|---|
| **查询形态** | 策略只需要"某日某指数的成分" → 查询永远是 `date = (≤D 的最近快照) AND index_code = X` → 长表上是一次带 filter 的列扫，**结果集极小**（300~1000 行）→ 毫秒级 |
| **写入形态** | 每次同步只新增 1 个月末快照 → **追加即可，不需要重写**（年分区 → 每年一个文件）→ 与日线 store 的 `append_data` 同一套机制 ✅ |
| **去重** | `(date, index_code, con_code)` 是主键 → `unique(subset=id_cols, keep="last")` 幂等 ✅ 重跑同步不会脏 |
| **可扩展** | 加指数 = 加几行数据，不用改表结构；未来可加"权重变化率""成分股历史" |

**与日线 store 复用同一基类** `ParquetStorage`：
- 存储路径：`data/index_weight/partition_key_year=YYYY/part-0.parquet`（年分区，与日线一致）
- 主键列：默认 `["date", "asset"]` → 需要子类覆盖为 `["date", "index_code", "con_code"]`（**这是基类要新增的一个可配置项**）

### 2.3 读 API（PIT 语义）

```python
class IndexWeightStore(ParquetStorage):
    def constituents(self, index_code: str, as_of: date) -> pl.DataFrame:
        """返回 as_of 当日**可见**的成分（含权重）。

        语义：取 trade_date <= as_of 的最近一个快照日 → 严格 PIT，无前视。
        """
```

策略侧新增 `universe_mode="index_pit"`：
- 用 `as_of = 信号日` 取成分 → 再叠加现有的流动性/市值过滤 → 完全无前视
- 这同时修掉一个现有隐患：现在 `universe_mode=market_cap_trim` 用**当前**市值做截断，
  成分有轻微前视 ⚠️；`index_pit` 模式彻底干净。

### 2.4 存储量估算

沪深300+中证500+中证1000+中证2000+创业板 ≈ 2000 只 × 12 月 × 10 年 = **24 万行/年** → 约 8MB/年 → 10 年 80MB。**微不足道**。

---

## 3. 指数行情：怎么取、怎么存

- 取：Tushare `index_daily(ts_code, start_date, end_date)` → 沪深300 回溯到 **2010-01-04**（4078 行），
  单年 244 行，5 年 2.35 秒 → **极快**。
- 存：**复用已有 `IndexBarsStore`**（`data/bars/index/`），字段 `sector_id/open/high/low/close/volume/amount`。
  - 需要把 `sector_id` 改名语义为指数代码，或新增 `index_code` 列（推荐：加 `index_code` 列，保留 `sector_id` 兼容）
  - 分区：年分区 ✅
- 用途：**基准/超额**（回测报告里现在只有绝对收益）、市场 regime 判断、指数池策略的基准。

---

## 4. 统一行情契约（用户接入 + akshare 参考实现）

### 4.1 契约（已存在，直接用）

```python
# quantide/core/domain/events.py
@dataclass
class QuoteSnapshot:
    symbol: str            # 统一代码格式：000001.SZ / 600519.SH
    price: float | None    # 最新价
    open: float | None
    high: float | None     # 当日最高（集合竞价期间 = 竞价价）
    low: float | None
    volume: float | None   # 累计成交量（股）
    amount: float | None   # 累计成交额（元）
    ts: datetime | None    # 行情时间戳

# quantide/core/ports/market_data.py
class MarketDataPort(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def subscribe(self, symbols: list[str]) -> None: ...
    def unsubscribe(self, symbols: list[str]) -> None: ...
    async def stream(self) -> AsyncIterator[MarketEvent]: ...
    def snapshot(self, symbols: list[str]) -> dict[str, QuoteSnapshot]: ...
```

**这就是"格式固定统一"的落点** ✅ —— 已有契约，不用新造。`PaperBroker` 已经按
`lastPrice/high/low/volume` 消费，适配器负责把用户的数据转成 `QuoteSnapshot` ✅。

### 4.2 用户接入方式（二选一）

**方式 A：进程内插件**（推荐，零进程间开销）
- 用户在自己的 Python 环境里实现一个 `MarketDataPort`，放到 `~/.quantide/plugins/market_data.py`，
  约定一个入口函数 `create_provider() -> MarketDataPort`。
- 启动时按 `settings.market_data_provider`（`plugin` / `tencent` / `sina` / `akshare`）加载。

**方式 B：HTTP 桥**（适合用户已有行情服务）
- 用户起一个 HTTP 服务，实现两个接口：
  - `GET /snapshot?symbols=000001.SZ,600519.SH` → `{"000001.SZ": {price, open, high, low, volume, amount, ts}, ...}`
  - `GET /stream`（SSE/WebSocket）→ 增量推送同结构
- 我们内置 `HttpMarketDataProvider` 读它。

**两种方式都只暴露上面那份 `QuoteSnapshot` 结构** → Paper 侧完全不变 ✅。

### 4.3 官方参考实现：AkshareProvider

```python
# quantide/data/providers/akshare_provider.py（参考实现，用户可复制改造）
class AkshareProvider:
    """基于 AkShare 的行情提供方（免费）。

    依赖：pip install akshare
    能力：snapshot（批量拉取）、stream（轮询转事件流）
    刷新：默认 3 秒轮询（交易时段）
    """
    def __init__(self, symbols: list[str], interval: float = 3.0): ...
    def snapshot(self, symbols) -> dict[str, QuoteSnapshot]: ...
```

**AkShare 覆盖的行情源**（可作为用户实现的模板）：
- `ak.stock_zh_a_spot_em()` → 全市场实时快照（东财），一次调用覆盖所有股票 → **最适合 Paper**
- `ak.stock_zh_a_hist_min_em(symbol, period="1", ...)` → 分钟线历史
- `ak.stock_zh_index_daily_em(symbol="sh000300")` → 指数日线

### 4.4 内置兜底（让发行版"开箱可跑"）

实测三个免费源在 Pro 上**全部可用**（无需 token、无需付费）：

| 源 | 端点 | 字段 | 备注 |
|---|---|---|---|
| 腾讯 | `http://qt.gtimg.cn/q=sh000001,sz000001` | 88 字段 GBK | 可批量 50+ 代码，**1 次/秒无压力** |
| 新浪 | `http://hq.sinajs.cn/list=sh000001` | 34 字段 GBK（需 Referer） | 字段最干净 |
| 东财 | `http://push2.eastmoney.com/api/qt/stock/get?secid=1.000001` | JSON f 编码 | 需逐个 secid |

实测样例（腾讯，2026-10-22 收盘后）：
```
v_sh600519: name=贵州茅台 code=600519 现价=1318.49 昨收=1309.69 今开=1303.21
             最高=1322.00 最低=1296.00 成交量(手)=42491 成交额(万)=557622 时间=20261022161500
```
→ `high/low` **含当日最高最低** → 集合竞价期间可用作"竞价价" ✅（第 8 节要用）

**内置 `TencentProvider`（批量、一次请求多代码）作为默认兜底** → 用户不接任何东西也能跑 Paper ✅。

### 4.5 涨跌停价

`PaperBroker._get_price_limits` 现在从 `live_quote` 内存取 → 换成从**本地存储**读（见 §5 `LimitPriceStore`），
因为涨跌停价是**日频静态数据**，不需要实时源 ✅。同时支持实时源覆盖（用户源可带 limit 字段）。

---

## 5. L2 存储层：高性能要求（"10x 数据不降速"）

### 5.1 现状与风险

`ParquetStorage` 已经是列存 + 年分区 + polars lazy scan，**底子是对的**。但有两个会在 10x 时咬人的点：

**风险 1：写放大（`append_data` 全量重写分区）**
```python
# _save_as_partition：每个分区 base_lf + part_lf → concat → unique → sort → 全量 write_parquet
```
→ 每次增量同步都**重写该年的整个文件**。年数据量 10x（≈2500 万行）时，每次同步写 2500 万行 → 同步时间线性劣化 ❗
**修法**：改为**按年分片 + 行组**（每年 N 个 `part-{i}.parquet`），增量只写"新的行组文件"，
不重写历史 → 写入 O(新增量) ✅。同时用 `row_group_size` 控制粒度。

**风险 2：读路径缺列裁剪/统计信息**
`get()` 现在 `pl.scan_parquet("**/*.parquet")` → 每次都要 glob 全目录 + 打开所有文件的 footer。
10x 数据（10 年 × 5678 只 = 5000 万行）时：
- glob 的文件数变多（10 年 × 12 月组 = 120 文件）→ 元数据开销上升
- 没有按列裁剪的默认值（策略面板要 30+ 列，实际全部都要，收益有限）
- 没有 min/max 统计 → `date` 过滤只能靠 parquet 的 row group 统计（polars 会用 ✅，但前提是按 date 有序 → 需要保证写入时**按 (date, asset) 有序** ✅ 现有代码已 sort ✅）

**修法**：
1. **两级分区**：`partition_key_year` → 每月一个文件（`year=YYYY/month=MM/part.parquet`）→ 读 80 天只碰 4-5 个文件 ✅（现在是 1 个大文件，读全部行再过滤 → 10x 时扫描量 10x ❗❗ 这是当前最大的降速点）
2. **月级分区 + 覆盖列裁剪** → 读放大从 O(全量) 降到 O(目标窗口) ✅
3. **日期索引**：现有的 `dates.pq` 索引保留 ✅（用于判断缺哪些日期）
4. **写增量**：不重写历史文件 ✅

### 5.2 目标（可验证的判据）

| 场景 | 现在 | 10x 后目标 | 判据 |
|---|---|---|---|
| 面板读 80 天全市场 | 0.002s | **< 0.05s** | 读取行数 ∝ 窗口，不随总量增长 |
| 单资产读 120 天 | 0.003s | **< 0.01s** | 走 asset 过滤 + 列裁剪 |
| 增量同步 1 天 | 重写整年文件 | **只写 1 个月文件** | 写入量 ∝ 新增行数 |
| 进程启动扫描目录 | 全量 collect 日期 | **只读索引文件** | 已有 `dates.pq` ✅，需补 `index_bars`/`index_weight` 的同款索引 |

### 5.3 具体改造清单（存储层）

1. `ParquetStorage` 支持**多级分区**（`partition_by=("year","month")`）→ 目录 `partition_key_year=YYYY/partition_key_month=MM/part.parquet`
2. `append_data` 改为**增量追加**：新数据按分区路由到文件；同分区同 key 冲突时才重写该分区
   （用 `pl.concat` + `unique` 但只对**目标分区**做，且只在数据量小的时候；否则写新文件 + 读时合并）
   → 更稳的做法：**每个 (year, month) 一个文件，月末同步时整体重写该月**（月数据量 ≈ 5678×21 = 12 万行 → 重写成本可接受 ✅）
3. 所有 store 统一 `_collect_dates()` → 写 `dates.pq` 索引（已有 ✅，扩展到 index_weight）
4. 新增 `LimitPriceStore`（涨跌停价，年分区，字段 date/asset/up_limit/down_limit）
5. `IndexWeightStore`（新增）
6. `IndexBarsStore` 补 `index_code` 列 + 索引

### 5.4 内存与并发

- 面板缓存已有上限（2 个面板 ≈ 110MB）✅；日线 store 的 `get()` 走 lazy → 不整表进内存 ✅
- 参数优化的 worker 进程各持一份缓存 → 4 worker × 110MB = 440MB ✅（Pro 64GB）

---

## 6. L1 数据源层（Tushare 统一出口）

### 6.1 新增 fetcher 能力（`quantide/data/fetchers/tushare.py`）

| 方法 | tushare 接口 | 频率/耗时（实测） | 用途 |
|---|---|---|---|
| `fetch_index_bars(dates, index_codes)` | `index_daily` | 5 年 2.35s | 基准/超额 |
| `fetch_index_weights(index_codes, start, end)` | `index_weight` | 按月，几十次调用 | PIT 成分 |
| `fetch_limit_price(dates)` | 已有 `stk_limit` ✅ | 单日 1.1s | 涨跌停（存储化） |
| `fetch_minute_bars(...)` | `stk_mins` | **1 次/分钟限频** ⚠️ | 可选，仅对持仓/候选 |
| `fetch_bars(dates)` | 已有 `daily` ✅ | 单日 0.2s | 日线 |
| `fetch_adjust_factor` | 已有 ✅ | 单日 0.13s | 复权 |
| `fetch_daily_basic` | 已有 ✅ | 单日 1.29s | 市值/换手 |
| `fetch_st_info` | 已有 ✅ | — | ST |

**重要限制**：`stk_mins` 限频 1 次/分钟 → **全市场 30 分钟历史不可行**（要跑数月）。
→ 决策：**分钟数据不进发行版核心**；若用户需要，提供"仅对候选池/持仓拉分钟"的工具函数（几十次调用 ✅）。

**回补 2015-2023 的实测成本**：
- 4 个接口 × 2200 交易日 ≈ 41 分钟/接口 → **并行约 1.5~2 小时**（Tushare 限频是每接口每分钟 N 次，可并发）
- 指数日线几乎免费（2.35s/5 年）
- → **发行版应内置"历史回补"向导**（选起始年份 → 后台跑 → 断点续传）

### 6.2 指数池代码表（实测确认）

| 名称 | tushare 代码 | PIT 成分历史 | 日线历史 | 备注 |
|---|---|---|---|---|
| 上证指数 | `000001.SH` | — | 2010+ | 基准 |
| 沪深300 | `000300.SH` | **2010+** ✅ | 2010+ | 首选基准 + 首选成分池 |
| 中证500 | `000905.SH` | **2010+** ✅ | 2010+ | |
| 中证1000 | `000852.SH` | 2015+ | 2014+ | |
| 中证2000 | `932000.CSI` | 2023+ | 2023+ | 指数 2023 才发布 |
| 创业板指 | `399006.SZ` | **2010+** ✅ | 2010+ | |
| 上证50 | `000016.SH` | **2010+** ✅ | 2010+ | |
| 中证A100 | `000903.SH` | **2010+** ✅ | 2010+ | |
| 中证全指 | `000985.CSI` | ❌ 仅 2 期 | 2010+ | **只作日线基准，不作成分池** |

**关键结论**：沪深300 / 中证500 / 创业板指 / 上证50 / 中证A100 的 PIT 成分可回溯到 **2010**
→ **指数池选股可以用于 2015-2023 的历史回测** ✅ → 直接解决"没有干净样本外"的问题。

**发行版应内置一张可配置的"关注指数表"**（`settings.index_codes`），并在 UI 列出每个指数
实际的 PIT 快照区间（从本地存储统计），让用户知道"这个指数从哪年开始才有成分"。

---

## 7. 同步任务（数据服务任务）

### 7.1 现状问题

- 只有"个股日线 + 市值"一条链（`data_market.py`）→ 指数/成分/涨跌停**没有同步入口**
- 同步是 **web 请求触发的 asyncio task**（`asyncio.create_task`）→ **进程重启就丢**、**不能并发防重入** ❗
- 逐日拉取（`fetch_with_daily_progress`）→ 2200 天 = 2200 次 HTTP → 慢但稳；可改为**按月批量**（Tushare `daily` 支持 `start_date/end_date` 区间 → 1 次拿 1 个月 → 2200 天只需 132 次调用 ✅ 快 16 倍）

### 7.2 目标：一个统一的同步编排器

```
DataSyncService
  · 任务队列：DB 持久化（现有 failed_tasks 表可扩展）→ 进程重启后自动恢复
  · 单实例互斥（同一数据集不并发同步）
  · 阶段编排：
      1. calendar     （交易日历）—— 已有
      2. stock_list   （证券主数据）—— 已有
      3. daily_bars   （个股日线 + 复权 + ST + 涨跌停）← 合并 4 个接口为按月批量
      4. daily_basic  （市值/换手）
      5. index_bars   （指数日线）★新增
      6. index_weight （指数成分 PIT）★新增
  · 断点续传：每个阶段记录已完成区间 → 重跑只补缺口
  · 进度：复用 `fetch_data_progress` 主题 + SSE ✅（前端已有）
  · 失败重试：现有 `failed_tasks` 表 ✅
```

### 7.3 收益

- 按月批量 → 回补 2015-2023 从 41 分钟/接口 降到 **~3 分钟/接口**（132 次调用 × 1.3s）
- 指数/成分纳入同一任务 → 一次点击全量对齐
- 持久化队列 → 重启不丢

---

## 8. 集合竞价收盘价买入（第 8 点需求）

### 8.1 现状

- 回测：信号日 = T-1（上一交易日收盘）→ **T 日开盘买入**（`BacktestBroker` 撮合 T 日 open ✅）
- Paper：实盘循环每 2 秒 `on_bar(now)` → 但**没有"按信号日去重"** → 会重复下单 ❗（我之前提过、一直没修）

### 8.2 目标：集合竞价（14:57-15:00）算信号 + 下单

A 股收盘集合竞价：**14:57-15:00**（3 分钟）→ 15:00 以集合竞价价格成交。

**时序设计**（新增 `ExecutionClock`）：

```
14:57:00  行情源开始提供集合竞价实时价（high/low/price = 竞价价）
14:57:00  → 策略在此刻用「截至 14:57 的实时价」算信号
          → 预算 2 分钟（14:57-14:59）完成因子/模型/排名
14:59:00  → 下单（限价 = 竞价价 + 容忍）
15:00:00  → 集合竞价撮合成交
```

**关键设计点**：
1. **信号计算用实时价**：策略的因子本来是日频（用当日 close）→ 集合竞价期间当日 close 还没定，
   但**集合竞价价格 ≈ 收盘价**（你的判断 ✅）→ 所以用 14:57 的竞价价替代"当日收盘价"计算因子 ✅
   → 这需要策略的因子计算支持"注入一个 override 收盘价"（见下）
2. **下单价**：用竞价价（或竞价价 × (1+容忍)）→ 成交价即集合竞价价 ✅
3. **3 分钟预算**：
   - 面板重建：全市场 2000 只 × 因子计算 ≈ **1.5s** ✅ 远够
   - 模型重训：45 天 × 2000 只 LightGBM ≈ **0.4~2.5s** ✅ 够
   - 排名/下单：毫秒级
   → **总耗时 < 5s**，3 分钟绰绰有余 ✅（当前回测里每 20 天重建面板 + 每 5 天重训的节奏，
   在 paper 场景应改为**每天**重建面板 + 重训 → 仍在 3 分钟内 ✅）
4. **降级策略**：若 14:59 前算不完 → 回退到"用 T-1 收盘信号，T+1 开盘买"（= 现在的回测语义）→ 记录告警 ✅

### 8.3 需要改的代码

| 位置 | 改动 |
|---|---|
| `quantide/service/strategy_runtime.py` | ① 循环改为**按交易日 + 时段**驱动（不是每 2 秒）；② 引入 `ExecutionClock` 判断"现在是集合竞价窗口"；③ **按信号日去重**（同一信号日只决策一次）❗ |
| `quantide/strategies/example/ml_momentum_reversal.py` | ① 因子计算支持"用实时价覆盖当日 close"（`price_override` 参数）；② 面板构建支持"当日未收盘"的模式 |
| `quantide/service/sim_broker.py` | 撮合支持"集合竞价成交"标记（成交价=竞价价，成交量按竞价量） |
| `quantide/core/ports/market_data.py` | 已有 `QuoteSnapshot.high/low` ✅ 可表达竞价价 |
| 回测侧 | 新增 `fill_timing` 配置：`next_open`（默认）/ `auction_close` → **回测也能模拟集合竞价成交** ✅（用当日 close 成交） |

### 8.4 回测与 Paper 的一致性

要能验证 Paper 行为，必须在**回测里也能复现集合竞价成交**：
- `fill_timing="auction_close"`：信号用 T-1 收盘（回测里没有盘中数据）→ 成交用 **T 日收盘价**
- 与真实 Paper 的差异：真实 Paper 用 14:57 竞价价（≈收盘价）→ 偏差可接受（可用 `slippage_pct` 覆盖）
→ 这样回测报告里的数字才和 Paper 可比 ✅

---

## 9. 只支持模拟交易（Paper only）

| 现状 | 处置 |
|---|---|
| `deploy_to_live` → 券商网关 | **移除/禁用**（发行版不接实盘） |
| `GatewayBrokerAdapter` / `GatewayClient` | 保留代码但**默认不注册**（`livequote_mode=gateway` 时才启用 → 发行版默认 `none`） |
| `PaperBroker` | **强化为唯一执行路径** |
| 运行时模式 | `runtime_mode` 只允许 `backtest` / `paper`（去掉 `live`） |
| 撮合 | `PaperBroker` 消费 `MarketDataPort` 快照 ✅（已有） |

**Paper 的行情从哪来**（用户自备）：
- 默认内置 `TencentProvider`（免费、批量、够用）→ 开箱可跑 ✅
- 用户可换 `akshare` 参考实现 / 自带插件 / HTTP 桥

---

## 10. 实施顺序（建议）

| 阶段 | 内容 | 产出 | 预估 |
|---|---|---|---|
| **P0** | 存储性能改造：两级分区（年+月）+ 增量写 + 列裁剪 | 10x 数据不降速 | 2-3 天 |
| **P0** | `LimitPriceStore` + `IndexWeightStore` + `IndexBarsStore` 补 `index_code` | 三个新存储 | 2 天 |
| **P0** | Tushare 新增 `fetch_index_bars` / `fetch_index_weights` | 数据源能力 | 0.5 天 |
| **P1** | `DataSyncService` 统一编排（持久化队列 + 按月批量 + 断点续传） | 同步任务可靠 | 2-3 天 |
| **P1** | `MarketDataHub` + `TencentProvider` + `AkshareProvider`（参考实现）+ 插件/HTTP 桥 | 统一行情契约落地 | 2-3 天 |
| **P1** | Paper 链路：按日去重 + `ExecutionClock` + 集合竞价下单 | Paper 可用 | 3-5 天 |
| **P2** | 回测 `fill_timing="auction_close"` + 基准/超额指标 + 指数池选股 | 回测与 Paper 一致 | 2-3 天 |
| **P2** | 历史回补向导（2015-2023） | 干净样本外 | 1 天 |
| **P3** | 发行版打包：配置向导（tushare token、指数选择、行情源选择）、文档 | 可发行 | 2 天 |

---

## 11. 需要你拍板的点

1. **指数成分的历史覆盖**：中证1000 只有 7 期快照、中证2000 待确认 → 指数池策略在早期年份不可用。
   是否接受"指数池策略只支持有快照的区间"？还是要求"用全市场等权/市值代理"做兜底？
2. **回补起始年**：2015 还是 2010？（2010 起更久，但 Tushare 限频下 2010-2026 ≈ 4000 交易日 × 4 接口 ≈ 3 小时）
3. **集合竞价的容差**：下单用"竞价价"还是"竞价价 ×(1+ε)"（ε 默认 0.1%？），以及"算不完则回退 T+1 开盘"的阈值是否 2 分钟。
4. **是否保留 30 分钟数据**：我的建议是**不做全市场**，只提供"按需拉候选池分钟线"的工具；你认可吗？
5. **发行版是否强制要求 tushare token**：没有 token 就无法初始化（当前 `fetch_stock_list` 等都依赖它）→ 是否给"仅离线模式"（用已有 parquet，不做同步）？

---

## 12. 实施进展（2026-10-23）

### 12.1 已落地

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 存储两级分区（年+月）、目录裁剪读、增量写、`repartition_to_levels()` 迁移 | ✅ |
| P0 | `IndexWeightStore`（PIT 成分）/ `LimitPriceStore` / 指数日线 | ✅ |
| P0 | Tushare `fetch_index_bars` / `fetch_index_weights`（逐期绕过 7000 行上限） | ✅ |
| P0 | `Date`/`Datetime` 日期列归一化（`date_as_date` + `normalize_partition_schema`） | ✅ |
| P1 | `DataSyncService`（六阶段、持久化、断点续传、互斥、取消、按月批量） | ✅ |
| P1 | 行情管理页「手动补全」接入统一同步服务（可选指数代码、进度流） | ✅ |
| P1 | `MarketDataHub` 发布/订阅 + `AksharePublisher`（50 支示例） | ✅ |
| P1 | `ExecutionClock` + Paper 集合竞价下单（竞价价×(1+ε)）+ 策略按日去重 | ✅ |
| P2 | 回测 `fill_timing="auction_close"`（14:57 决策、收盘/竞价价成交） | ✅ |
| P2 | 指数基准 → 超额收益/Beta/Alpha/信息比率（`baseline_returns` 已存在，已接线） | ✅ |
| P2 | `universe_mode="index_pit"`（PIT 成分池，缺失时自动回退市值裁剪） | ✅ |
| P2 | 历史回补 2015-2026（日线 + 指数 + 成分） | ✅ |

### 12.2 实测数据覆盖（Pro）

- 个股日线：`2015-01-05 ~ 2026-10-19`，2863 个交易日，11,837,026 行，5835 只；
  各年行数与旧库 2024 年口径一致（2024=1,293,893）。
- 指数日线：沪深300 / 中证500 / 创业板指 / 上证50，均 `2015-01-05 ~ 2026-10-19`。
- 指数成分（PIT）：沪深300 / 中证500 / 中证1000 / 创业板指 **141 期**
  （2015-01-30 ~ 2026-09-30，逐月）；中证2000 **38 期**（2023-08-31 起）；
  上证50 到 2021-05（Tushare 侧停止更新）。
- 日线分区：`year=YYYY/month=MM`；2024Q1 读取只命中 3/37 个文件。
- 日市值/换手（daily_basic）：2015-2026，约 1175 万行。

### 12.3 回测口径说明

- `fill_timing="auction_close"`：日线回测在 14:57 触发 `on_bar`，策略使用**当日 bar**
  （日线用收盘价近似 14:57 竞价价）；撮合价=当日收盘价，买入限价=收盘×(1+ε)，
  卖出限价=收盘×(1-ε)。这是对"14:57 看竞价价→14:59 下单→15:00 成交"的日线近似，
  存在"14:57 价格 ≈ 收盘价"的轻微乐观偏差，已在文档中声明。
- `fill_timing="next_open"`（旧默认）：9:30 触发，用上一交易日数据决策，开盘价成交。
- `benchmark`（默认 `000300.SH`）：自动读取指数日线计算基准收益；覆盖不足时
  自动降级为无基准指标，不阻塞回测。
- `universe_mode="index_pit"` + `index_pool_codes`：按目标日 PIT 成分选池；
  成分库无覆盖时回退 `market_cap_trim`。

### 12.4 2025H1 实测（Pro，auction_close）

| 配置 | Total | Sharpe | MDD | 超额(年化) | Beta | Alpha |
|---|---|---|---|---|---|---|
| `market_cap_trim`（默认池） | +6.02% | 0.63 | -17.89% | +6.85% | 1.17 | +5.72% |
| `index_pit`（300+500） | +3.31% | 0.42 | -18.48% | +0.63% | 1.01 | +0.55% |
| `next_open`（旧口径） | +0.48% | 0.17 | -18.00% | -5.65% | 1.12 | -6.46% |

注：三者信号与成交口径不同，仅用于验证链路，不构成参数优劣结论。

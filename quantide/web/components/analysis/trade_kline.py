"""回测成交对 K 线弹窗组件。

按成交对展示个股 K 线与买卖点：
- 初始化窗口回看买入日之前 120 根 K 线；
- 有卖出的成交对额外展示卖出日之后一个月的走势；
- K 线与均线均为前复权口径（复权锚点为该股票最新一根 K 线）；
- 右侧面板展示交易信息与决策特征取值。
"""

import bisect
import datetime
import json
from typing import Any

import polars as pl
from fasthtml.common import Div, P, Script, Span

from quantide.core.enums import OrderSide
from quantide.data.models.daily_bars import daily_bars
from quantide.data.sqlite import db
from quantide.service.trade_pairs import TradePair, build_trade_pairs
from quantide.web.components.asset_label import resolve_asset_name

TRADE_KLINE_LOOKBACK_BARS = 120
TRADE_KLINE_POST_SELL_BARS = 20
TRADE_KLINE_HISTORY_CALENDAR_DAYS = 700
TRADE_KLINE_MA_PERIODS = (5, 10, 20, 30, 120, 250)
TRADE_KLINE_CHART_ID = "trade-kline-chart"
TRADE_KLINE_PANEL_ID = "trade-kline-panel"
TRADE_KLINE_FEATURE_LOOKBACK_DAYS = 150


def _trade_date(tm: Any) -> datetime.date | None:
    """从成交时间中提取交易日。"""
    if isinstance(tm, datetime.datetime):
        return tm.date()
    if isinstance(tm, datetime.date):
        return tm
    if isinstance(tm, str):
        try:
            return datetime.datetime.fromisoformat(tm).date()
        except ValueError:
            return None
    return None


def _load_raw_bars(asset: str, start: datetime.date, end: datetime.date) -> pl.DataFrame:
    """加载区间内的原始日线（含复权因子）。"""
    df = daily_bars.get_bars_in_range(
        start,
        end,
        assets=[asset],
        adjust=None,
        eager_mode=True,
    )
    if isinstance(df, pl.LazyFrame):
        df = df.collect()
    if df is None or df.height == 0:
        return pl.DataFrame()
    if "adjust" not in df.columns:
        df = df.with_columns(pl.lit(1.0).alias("adjust"))
    return df.with_columns(pl.col("date").cast(pl.Date).alias("date")).sort("date")


def _qfq_frame(raw: pl.DataFrame) -> tuple[pl.DataFrame, float]:
    """把原始日线换算为前复权口径。

    Args:
        raw: 含 ``adjust`` 复权因子的原始日线。

    Returns:
        tuple: (前复权日线, 复权锚点因子)。
    """
    if raw.is_empty():
        return raw, 1.0
    anchor = float(raw["adjust"][-1] or 1.0) or 1.0
    df = raw.with_columns(
        [
            (pl.col("adjust") / anchor * pl.col("open")).alias("open"),
            (pl.col("adjust") / anchor * pl.col("high")).alias("high"),
            (pl.col("adjust") / anchor * pl.col("low")).alias("low"),
            (pl.col("adjust") / anchor * pl.col("close")).alias("close"),
        ]
    )
    return df, anchor


def _qfq_price(
    raw: pl.DataFrame,
    anchor: float,
    price: float,
    day: datetime.date | None,
) -> float:
    """把成交价换算到前复权口径。"""
    if raw.is_empty() or not day:
        return price
    factor_rows = raw.filter(pl.col("date") <= pl.lit(day))
    if factor_rows.is_empty():
        return price
    factor = float(factor_rows["adjust"][-1] or 1.0) or 1.0
    return price * factor / anchor


def _build_markers(
    portfolio_id: str,
    asset: str,
    raw: pl.DataFrame,
    anchor: float,
    buy_tid: str,
    sell_tid: str | None,
) -> list[dict[str, Any]]:
    """收集该股票在窗口内的买卖点（换算到前复权口径）。"""
    markers: list[dict[str, Any]] = []
    trades_df = db.trades_all(portfolio_id)
    if trades_df is None or trades_df.is_empty():
        return markers
    for row in trades_df.filter(pl.col("asset") == asset).iter_rows(named=True):
        trade_dt = _trade_date(row.get("tm"))
        if trade_dt is None:
            continue
        side_value = int(row.get("side") or 0)
        tid = str(row.get("tid") or "")
        markers.append(
            {
                "tid": tid,
                "dt": trade_dt.isoformat(),
                "price": _qfq_price(
                    raw, anchor, float(row.get("price") or 0), trade_dt
                ),
                "raw_price": float(row.get("price") or 0),
                "shares": float(row.get("shares") or 0),
                "side": "sell" if side_value == int(OrderSide.SELL) else "buy",
                "side_value": side_value,
                "current": tid in {buy_tid, sell_tid},
                "focus": tid == buy_tid,
            }
        )
    markers.sort(key=lambda item: item["dt"])
    return markers


def _decision_features(
    asset: str,
    decision_date: datetime.date,
) -> list[tuple[str, float]] | None:
    """计算决策日该股票的模型特征取值。

    Args:
        asset: 证券代码。
        decision_date: 决策日。

    Returns:
        list | None: (特征名, 取值) 列表；策略不支持特征时返回 ``None``。
    """
    try:
        from quantide.strategies.example.ml_momentum_reversal import (
            FEATURE_COLS,
            compute_alpha_features,
        )
    except Exception:
        return None

    try:
        panel = daily_bars.get_bars_in_range(
            decision_date - datetime.timedelta(days=TRADE_KLINE_FEATURE_LOOKBACK_DAYS),
            decision_date,
            assets=None,
            adjust="qfq",
            eager_mode=True,
        )
        if isinstance(panel, pl.LazyFrame):
            panel = panel.collect()
        if panel is None or panel.is_empty():
            return None
        features = compute_alpha_features(panel)
        row = features.filter(
            (pl.col("asset") == asset) & (pl.col("date") == pl.lit(decision_date))
        )
        if row.is_empty():
            return None
        values = row.row(0, named=True)
        result: list[tuple[str, float]] = []
        for column in FEATURE_COLS:
            value = values.get(column)
            if value is None:
                continue
            try:
                result.append((column, float(value)))
            except (TypeError, ValueError):
                continue
        return result or None
    except Exception:
        return None


def _find_pair(
    portfolio_id: str,
    buy_tid: str,
    sell_tid: str | None,
) -> TradePair | None:
    """在成交对列表中定位目标成交对。"""
    for pair in build_trade_pairs(portfolio_id):
        if pair.buy_tid == buy_tid and (sell_tid is None or pair.sell_tid == sell_tid):
            return pair
    return None


def build_trade_kline_payload(
    portfolio_id: str,
    buy_tid: str,
    sell_tid: str | None = None,
) -> dict[str, Any] | None:
    """构建成交对对应的 K 线、买卖点与信息面板数据。

    Args:
        portfolio_id: 回测组合 ID。
        buy_tid: 买入成交 ID。
        sell_tid: 卖出成交 ID（可为空，表示未平仓）。

    Returns:
        dict | None: 弹窗数据；成交对不存在时返回 ``None``。
    """
    pair = _find_pair(portfolio_id, buy_tid, sell_tid)
    if pair is None:
        return None

    asset = pair.asset
    buy_dt = _trade_date(pair.buy_tm)
    sell_dt = _trade_date(pair.sell_tm) if pair.sell_tm else None
    if buy_dt is None:
        return None

    history_start = buy_dt - datetime.timedelta(days=TRADE_KLINE_HISTORY_CALENDAR_DAYS)
    store_end = daily_bars.end or buy_dt
    raw = _load_raw_bars(asset, history_start, max(store_end, buy_dt))
    if raw.is_empty():
        raw = _load_raw_bars(asset, history_start, buy_dt)
    if raw.is_empty():
        return None

    qfq, anchor = _qfq_frame(raw)

    # 主窗口：截至成交日（平仓则为卖出日）的最后 120 根 K 线
    window_end = buy_dt if sell_dt is None else max(sell_dt, buy_dt)
    pre = qfq.filter(pl.col("date") <= pl.lit(window_end))
    display_start = window_end
    if pre.height:
        display_start = pre.row(
            max(0, pre.height - TRADE_KLINE_LOOKBACK_BARS), named=True
        )["date"]
    if buy_dt < display_start:
        # 持仓周期超过窗口时，保证买入点可见
        display_start = buy_dt

    ma_exprs = []
    for period in TRADE_KLINE_MA_PERIODS:
        ma_exprs.append(
            pl.col("close")
            .rolling_mean(window_size=period, min_samples=period)
            .over("asset")
            .alias(f"ma{period}")
        )
    # 均线需在完整历史上计算后再截取窗口
    qfq = qfq.with_columns(ma_exprs)
    # 当日涨跌幅（前复权口径）
    qfq = qfq.with_columns(
        (
            (pl.col("close") / pl.col("close").shift(1).over("asset") - 1.0) * 100.0
        )
        .fill_null(0.0)
        .alias("pct")
    )

    window = qfq.filter(
        (pl.col("date") >= pl.lit(display_start)) & (pl.col("date") <= pl.lit(window_end))
    )
    post_trend_bars = 0
    if sell_dt is not None:
        # 卖出后最多再显示 20 根（不足则尽量显示）
        post = qfq.filter(pl.col("date") > pl.lit(window_end)).head(
            TRADE_KLINE_POST_SELL_BARS
        )
        if not post.is_empty():
            post_trend_bars = post.height
            window = pl.concat([window, post])

    bars: list[dict[str, Any]] = []
    for row in window.iter_rows(named=True):
        bars.append(
            {
                "dt": str(row.get("date")),
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
                "volume": float(row.get("volume") or 0),
                "pct": float(row.get("pct") or 0),
                "ma": {
                    str(period): row.get(f"ma{period}")
                    for period in TRADE_KLINE_MA_PERIODS
                },
            }
        )

    markers = _build_markers(
        portfolio_id, asset, raw, anchor, buy_tid, pair.sell_tid
    )
    if bars:
        first_dt = bars[0]["dt"]
        markers = [m for m in markers if m["dt"] >= first_dt]

    features = _decision_features(asset, buy_dt)

    return {
        "asset": asset,
        "name": resolve_asset_name(asset),
        "pair": {
            "buy_tid": pair.buy_tid,
            "sell_tid": pair.sell_tid,
            "buy_tm": str(pair.buy_tm),
            "sell_tm": str(pair.sell_tm) if pair.sell_tm else "",
            "buy_dt": buy_dt.isoformat(),
            "sell_dt": sell_dt.isoformat() if sell_dt else "",
            "shares": pair.shares,
            "buy_price": pair.buy_price,
            "sell_price": pair.sell_price,
            "buy_amount": pair.buy_amount,
            "sell_amount": pair.sell_amount,
            "fee": pair.fee,
            "pnl": pair.pnl,
            "return_rate": pair.return_rate,
            "holding_days": pair.holding_days,
            "status": pair.status,
            "unrealized_pnl": pair.unrealized_pnl,
        },
        "bars": bars,
        "markers": markers,
        "ma_periods": list(TRADE_KLINE_MA_PERIODS),
        "lookback_bars": TRADE_KLINE_LOOKBACK_BARS,
        "initial_bars": len(bars),
        "post_trend_bars": post_trend_bars,
        "post_start_dt": bars[len(bars) - post_trend_bars]["dt"] if post_trend_bars else "",
        "post_sell_trend": sell_dt is not None,
        "features": features or [],
    }


def _chart_script(payload: dict[str, Any]) -> str:
    """生成 ECharts 初始化脚本。"""
    bars = payload.get("bars") or []
    dates = [bar["dt"] for bar in bars]
    ohlc = [[bar["open"], bar["close"], bar["low"], bar["high"]] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ma_periods = payload.get("ma_periods") or list(TRADE_KLINE_MA_PERIODS)
    ma_series = {
        str(period): [
            None if bar["ma"].get(str(period)) is None else round(bar["ma"][str(period)], 4)
            for bar in bars
        ]
        for period in ma_periods
    }
    markers = payload.get("markers") or []
    date_index = {dt: index for index, dt in enumerate(dates)}

    buy_points: list[dict[str, Any]] = []
    sell_points: list[dict[str, Any]] = []
    marker_map: dict[str, list[dict[str, Any]]] = {}
    for marker in markers:
        index = date_index.get(marker["dt"])
        if index is None:
            position = bisect.bisect_right(dates, marker["dt"]) - 1
            if position < 0:
                continue
            if position == len(dates) - 1 and marker["dt"] > dates[-1] and not marker.get("current"):
                continue
            index = position
        point: dict[str, Any] = {"value": [index, marker["price"]], "trade": marker}
        if marker.get("focus"):
            point["label"] = {
                "show": True,
                "formatter": "本次买入",
                "position": "top",
                "color": "#111827",
                "fontSize": 10,
                "fontWeight": "bold",
            }
        elif marker.get("current"):
            point["label"] = {
                "show": True,
                "formatter": "本次卖出",
                "position": "bottom",
                "color": "#111827",
                "fontSize": 10,
                "fontWeight": "bold",
            }
        if marker["side"] == "buy":
            buy_points.append(point)
        else:
            sell_points.append(point)
        marker_map.setdefault(marker["dt"], []).append(marker)

    post_start_dt = payload.get("post_start_dt") or ""
    mark_area: list[list[dict[str, str]]] = []
    if post_start_dt and post_start_dt in dates:
        post_dates = [dt for dt in dates if dt >= post_start_dt]
        if post_dates:
            mark_area = [[{"xAxis": post_dates[0]}, {"xAxis": post_dates[-1]}]]

    legend = ["K线", "成交量", "买入", "卖出"] + [f"MA{p}" for p in ma_periods]
    ma_series_js = json.dumps(ma_series)
    legend_js = json.dumps(legend)

    return f"""
    (function () {{
        var el = document.getElementById('{TRADE_KLINE_CHART_ID}');
        if (!el || typeof echarts === 'undefined') {{ return; }}
        if (window.__tradeKlineResize) {{
            window.removeEventListener('resize', window.__tradeKlineResize);
        }}
        if (window.__tradeKlineChart) {{
            window.__tradeKlineChart.dispose();
        }}
        var dates = {json.dumps(dates)};
        var ohlc = {json.dumps(ohlc)};
        var volumes = {json.dumps(volumes)};
        var pcts = {json.dumps([round(bar.get('pct') or 0, 4) for bar in bars])};
        var maSeries = {ma_series_js};
        var markerMap = {json.dumps(marker_map)};
        var maPeriods = {json.dumps([str(p) for p in ma_periods])};
        var chart = echarts.init(el);
        window.__tradeKlineChart = chart;
        var series = [
            {{
                name: 'K线',
                type: 'candlestick',
                data: ohlc,
                itemStyle: {{
                    color: '#ef4444',
                    color0: '#22c55e',
                    borderColor: '#ef4444',
                    borderColor0: '#22c55e'
                }},
                markArea: {{
                    silent: true,
                    itemStyle: {{ color: 'rgba(34,197,94,0.10)' }},
                    data: {json.dumps(mark_area)}
                }}
            }},
            {{
                name: '成交量',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: volumes,
                itemStyle: {{ color: '#9ca3af' }}
            }},
            {{
                name: '买入',
                type: 'scatter',
                data: {json.dumps(buy_points, ensure_ascii=False)},
                symbol: 'arrow',
                symbolRotate: 0,
                symbolSize: 13,
                itemStyle: {{ color: '#ef4444' }}
            }},
            {{
                name: '卖出',
                type: 'scatter',
                data: {json.dumps(sell_points, ensure_ascii=False)},
                symbol: 'arrow',
                symbolRotate: 180,
                symbolSize: 13,
                itemStyle: {{ color: '#22c55e' }}
            }}
        ];
        var maColors = ['#f59e0b', '#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6', '#64748b'];
        for (var i = 0; i < maPeriods.length; i++) {{
            var period = maPeriods[i];
            series.push({{
                name: 'MA' + period,
                type: 'line',
                data: maSeries[period],
                smooth: true,
                showSymbol: false,
                lineStyle: {{ width: 1, color: maColors[i % maColors.length] }},
                itemStyle: {{ color: maColors[i % maColors.length] }}
            }});
        }}
        chart.setOption({{
            animation: false,
            backgroundColor: 'transparent',
            legend: {{ data: {legend_js}, top: 0, textStyle: {{ fontSize: 11 }} }},
            grid: [
                {{ left: 56, right: 20, top: 30, height: '58%' }},
                {{ left: 56, right: 20, top: '74%', height: '14%' }}
            ],
            tooltip: {{
                trigger: 'axis',
                axisPointer: {{ type: 'cross' }},
                formatter: function (params) {{
                    if (!params || !params.length) {{ return ''; }}
                    var idx = params[0].dataIndex;
                    var bar = ohlc[idx];
                    if (!bar) {{ return ''; }}
                    var lines = [];
                    lines.push(dates[idx]);
                    lines.push('开盘 ' + bar[0].toFixed(2));
                    lines.push('最高 ' + bar[3].toFixed(2));
                    lines.push('最低 ' + bar[2].toFixed(2));
                    lines.push('收盘 ' + bar[1].toFixed(2));
                    var pct = pcts[idx] || 0;
                    lines.push('涨跌幅 ' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%');
                    lines.push('成交量 ' + (volumes[idx] || 0).toLocaleString());
                    var marks = markerMap[dates[idx]] || [];
                    for (var i = 0; i < marks.length; i++) {{
                        var m = marks[i];
                        lines.push(
                            (m.side === 'buy' ? '买入 ' : '卖出 ') +
                            m.price.toFixed(2) + ' x ' + m.shares.toLocaleString()
                        );
                    }}
                    return lines.join('<br/>');
                }}
            }},
            xAxis: [
                {{ type: 'category', data: dates, axisLabel: {{ fontSize: 10 }}, splitLine: {{ show: false }} }},
                {{ type: 'category', gridIndex: 1, data: dates, axisLabel: {{ show: false }}, axisTick: {{ show: false }} }}
            ],
            yAxis: [
                {{ scale: true, axisLabel: {{ fontSize: 10 }}, splitLine: {{ lineStyle: {{ color: '#f3f4f6' }} }} }},
                {{ gridIndex: 1, scale: true, axisLabel: {{ show: false }}, splitLine: {{ show: false }} }}
            ],
            dataZoom: [
                {{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }},
                {{ type: 'slider', xAxisIndex: [0, 1], start: 0, end: 100, bottom: 2, height: 16 }}
            ],
            series: series
        }});
        var resize = function () {{
            if (window.__tradeKlineChart) {{ window.__tradeKlineChart.resize(); }}
        }};
        window.__tradeKlineResize = resize;
        window.addEventListener('resize', resize);
        setTimeout(resize, 0);
        setTimeout(resize, 150);
        setTimeout(resize, 450);
    }})();
    """


def _format_number(value: Any, digits: int = 2) -> str:
    """格式化面板数值。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _panel_row(label: str, value: str) -> Any:
    """构建面板的一行（标签 + 数值）。"""
    return Div(
        Span(label, cls="text-xs text-gray-500 truncate"),
        Span(value, cls="text-xs text-gray-900 font-medium text-right tabular-nums"),
        cls="flex items-center justify-between gap-2 py-0.5",
    )


def _render_trade_panel(payload: dict[str, Any]) -> Any:
    """构建右侧交易信息与决策特征面板。"""
    pair = payload.get("pair") or {}
    sections: list[Any] = []

    trade_rows = [
        _panel_row("买入价(实际成交)", _format_number(pair.get("buy_price"))),
        _panel_row("卖出价(实际成交)", _format_number(pair.get("sell_price"))),
        _panel_row("买入金额", _format_number(pair.get("buy_amount"))),
        _panel_row("费用", _format_number(pair.get("fee"))),
    ]
    if pair.get("status") == "closed":
        rate = pair.get("return_rate")
        trade_rows.append(
            _panel_row("收益率", "—" if rate is None else f"{float(rate) * 100:.2f}%")
        )
        trade_rows.append(
            _panel_row("持仓天数", "—" if pair.get("holding_days") is None else str(pair.get("holding_days")))
        )
    else:
        trade_rows.append(
            _panel_row("浮动盈亏", _format_number(pair.get("unrealized_pnl")))
        )
    sections.append(
        Div(
            Div("交易信息", cls="text-xs font-semibold text-gray-700 mb-1"),
            *trade_rows,
            cls="mb-3",
        )
    )

    features = payload.get("features") or []
    if features:
        feature_rows = [
            _panel_row(name, f"{value:.4f}") for name, value in features
        ]
        sections.append(
            Div(
                Div("决策特征（买入日）", cls="text-xs font-semibold text-gray-700 mb-1"),
                *feature_rows,
            )
        )
    else:
        sections.append(
            Div(
                Div("决策特征", cls="text-xs font-semibold text-gray-700 mb-1"),
                P("该策略未提供特征明细。", cls="text-xs text-gray-400"),
            )
        )

    return Div(
        *sections,
        id=TRADE_KLINE_PANEL_ID,
        cls="w-72 shrink-0 max-h-[520px] overflow-y-auto pl-3 border-l border-gray-200",
    )


def render_trade_kline_chart(payload: dict[str, Any]) -> Any:
    """渲染 K 线图容器与右侧信息面板。

    Args:
        payload: :func:`build_trade_kline_payload` 生成的数据。

    Returns:
        Any: 可直接放入弹窗主体的节点。
    """
    bars = payload.get("bars") or []
    pair = payload.get("pair") or {}

    if not bars:
        return Div(
            Div(
                Span(f"{payload.get('name', '')} {payload.get('asset', '')}", cls="text-sm font-semibold text-gray-900"),
                Span(
                    f"｜{str(pair.get('buy_tm', ''))[:19]} 买入",
                    cls="text-sm text-gray-600 ml-2",
                ),
                cls="flex items-center flex-wrap gap-1",
            ),
            P("该区间没有行情数据，无法绘制 K 线。", cls="text-sm text-gray-500 py-8 text-center"),
        )

    summary = Div(
        Span(
            f"K 线与均线为前复权口径，共 {len(bars)} 根"
            + (
                f"（主窗口 {payload.get('lookback_bars', 0)} 根 + 卖出后 {payload.get('post_trend_bars', 0)} 根）"
                if payload.get("post_sell_trend")
                else f"（回看 {payload.get('lookback_bars', 0)} 根）"
            ),
            cls="text-xs text-gray-500",
        ),
        cls="flex items-center flex-wrap gap-1 mb-3",
    )

    return Div(
        summary,
        Div(
            Div(id=TRADE_KLINE_CHART_ID, cls="w-full h-[520px]"),
            _render_trade_panel(payload),
            cls="flex gap-2 items-start",
        ),
        Script(_chart_script(payload)),
    )

"""回测成交对 K 线弹窗组件测试。"""

import datetime

import polars as pl
import pytest

from quantide.core.enums import OrderSide
from quantide.data.models.entities import Trade
from quantide.web.components.analysis import trade_kline as trade_kline_module
from quantide.web.components.analysis.trade_kline import (
    TRADE_KLINE_LOOKBACK_BARS,
    TRADE_KLINE_MA_PERIODS,
    TRADE_KLINE_POST_SELL_BARS,
    build_trade_kline_payload,
    render_trade_kline_chart,
)


def _html(node: object) -> str:
    return node.__html__()  # type: ignore[attr-defined]


def _trade(
    *,
    tid: str,
    portfolio_id: str = "pf-1",
    asset: str = "002078.SZ",
    side: OrderSide = OrderSide.BUY,
    tm: datetime.datetime,
    price: float = 12.5,
    shares: float = 100.0,
    fee: float = 5.0,
) -> Trade:
    return Trade(
        portfolio_id=portfolio_id,
        tid=tid,
        qtoid=f"q-{tid}",
        foid=f"f-{tid}",
        asset=asset,
        shares=shares,
        price=price,
        amount=price * shares,
        tm=tm,
        side=side,
        cid=f"c-{tid}",
        fee=fee,
    )


def _trades_frame(trades: list[Trade]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "tid": [t.tid for t in trades],
            "portfolio_id": [t.portfolio_id for t in trades],
            "asset": [t.asset for t in trades],
            "side": [int(t.side) for t in trades],
            "price": [t.price for t in trades],
            "shares": [t.shares for t in trades],
            "fee": [t.fee for t in trades],
            "tm": [t.tm for t in trades],
        }
    )


def _bars(start: datetime.date, end: datetime.date, adjust: float = 1.0) -> pl.DataFrame:
    dates: list[datetime.date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current)
        current += datetime.timedelta(days=1)
    count = len(dates)
    return pl.DataFrame(
        {
            "date": dates,
            "asset": ["002078.SZ"] * count,
            "open": [10.0] * count,
            "high": [13.0] * count,
            "low": [9.5] * count,
            "close": [12.0] * count,
            "volume": [1000.0] * count,
            "adjust": [adjust] * count,
        }
    )


def _install(
    monkeypatch,
    trades: list[Trade],
    bars: pl.DataFrame,
    store_end: datetime.date | None = None,
    features: list[tuple[str, float]] | None = None,
) -> None:
    monkeypatch.setattr(
        trade_kline_module.db, "trades_all", lambda portfolio_id: _trades_frame(trades)
    )
    monkeypatch.setattr(
        trade_kline_module.db,
        "query_positions",
        lambda **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(
        trade_kline_module.daily_bars,
        "get_bars_in_range",
        lambda *args, **kwargs: bars,
    )
    monkeypatch.setattr(
        type(trade_kline_module.daily_bars),
        "end",
        property(lambda self: store_end),
        raising=False,
    )
    monkeypatch.setattr(
        trade_kline_module,
        "_decision_features",
        lambda portfolio_id, asset, decision_date: (features, "策略选股池口径"),
    )


def test_build_payload_returns_none_for_unknown_pair(monkeypatch) -> None:
    _install(monkeypatch, [], _bars(datetime.date(2024, 1, 2), datetime.date(2024, 6, 3)))

    assert build_trade_kline_payload("pf-1", "missing") is None


def test_build_payload_for_open_position_has_no_sell_window(monkeypatch) -> None:
    trade = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    _install(
        monkeypatch,
        [trade],
        _bars(datetime.date(2023, 1, 2), datetime.date(2024, 6, 3)),
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")

    assert payload is not None
    assert payload["post_sell_trend"] is False
    assert payload["post_trend_bars"] == 0
    assert len(payload["bars"]) == TRADE_KLINE_LOOKBACK_BARS
    assert payload["bars"][-1]["dt"] == "2024-06-03"
    assert payload["pair"]["status"] == "open"


def test_build_payload_for_closed_pair_extends_one_month_after_sell(monkeypatch) -> None:
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    sell = _trade(
        tid="t2",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 6, 10, 14, 55),
        price=13.0,
    )
    _install(
        monkeypatch,
        [buy, sell],
        _bars(datetime.date(2023, 1, 2), datetime.date(2024, 7, 10)),
        store_end=datetime.date(2024, 7, 10),
    )

    payload = build_trade_kline_payload("pf-1", "t1", "t2")

    assert payload is not None
    assert payload["post_sell_trend"] is True
    assert payload["post_trend_bars"] > 0
    # 窗口 = 买入前 120 根 + 买入到卖出之间的 K 线 + 卖出后一个月
    assert len(payload["bars"]) >= TRADE_KLINE_LOOKBACK_BARS + payload["post_trend_bars"]
    assert payload["bars"][-1]["dt"] <= "2024-07-10"
    assert payload["bars"][-1]["dt"] > "2024-06-10"
    assert payload["pair"]["status"] == "closed"
    assert payload["pair"]["pnl"] == pytest.approx(100 * (13.0 - 12.5) - 10.0)


def test_build_payload_computes_moving_averages(monkeypatch) -> None:
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    _install(
        monkeypatch,
        [buy],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 3)),
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")

    assert payload is not None
    assert payload["ma_periods"] == list(TRADE_KLINE_MA_PERIODS)
    last_bar = payload["bars"][-1]
    for period in TRADE_KLINE_MA_PERIODS:
        assert last_bar["ma"][str(period)] == pytest.approx(12.0)


def test_build_payload_scales_trade_price_to_qfq(monkeypatch) -> None:
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30), price=12.5)
    _install(
        monkeypatch,
        [buy],
        _bars(datetime.date(2023, 1, 2), datetime.date(2024, 6, 3), adjust=2.0),
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")

    assert payload is not None
    marker = next(m for m in payload["markers"] if m["tid"] == "t1")
    # 复权锚点为最后一根 K 线，因子相同 → 前复权价与原始成交价一致
    assert marker["price"] == pytest.approx(12.5)
    assert marker["raw_price"] == pytest.approx(12.5)


def test_render_chart_emits_ma_series_and_info_panel(monkeypatch) -> None:
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    sell = _trade(
        tid="t2",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 6, 10, 14, 55),
        price=13.0,
    )
    _install(
        monkeypatch,
        [buy, sell],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 10)),
        store_end=datetime.date(2024, 6, 10),
        features=[("bias_5", 0.0123)],
    )

    payload = build_trade_kline_payload("pf-1", "t1", "t2")
    assert payload is not None
    html = _html(render_trade_kline_chart(payload))

    assert 'id="trade-kline-chart"' in html
    assert "echarts.init" in html
    assert "candlestick" in html
    for period in TRADE_KLINE_MA_PERIODS:
        assert f"MA{period}" in html
    assert "买入金额" in html
    assert "费用" in html
    assert "收益率" in html
    assert "决策特征" in html
    assert "bias_5" in html
    # 按要求不展示的字段
    assert "PnL" not in html
    assert "买入时间" not in html
    assert "卖出时间" not in html
    assert "数量" not in html
    assert "卖出金额" not in html


def test_render_chart_reports_missing_market_data() -> None:
    payload = {
        "asset": "002078.SZ",
        "name": "测试股",
        "pair": {
            "buy_tid": "t1",
            "sell_tid": "",
            "buy_tm": "2024-06-03 09:30:00",
            "sell_tm": "",
            "buy_dt": "2024-06-03",
            "sell_dt": "",
            "shares": 100,
            "buy_price": 12.5,
            "sell_price": None,
            "buy_amount": 1250,
            "sell_amount": 0,
            "fee": 5,
            "pnl": None,
            "return_rate": None,
            "holding_days": None,
            "status": "open",
            "unrealized_pnl": None,
        },
        "bars": [],
        "markers": [],
        "ma_periods": list(TRADE_KLINE_MA_PERIODS),
        "lookback_bars": TRADE_KLINE_LOOKBACK_BARS,
        "initial_bars": 0,
        "post_trend_bars": 0,
        "post_sell_trend": False,
        "features": [],
    }

    html = _html(render_trade_kline_chart(payload))

    assert "没有行情数据" in html
    assert "trade-kline-chart" not in html


def test_build_payload_scales_marker_when_factor_changes(monkeypatch) -> None:
    """复权因子变化时，成交点与 K 线使用同一前复权口径。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 3, 1, 9, 30), price=12.5)
    bars = _bars(datetime.date(2024, 1, 2), datetime.date(2024, 6, 3))
    # 前半段因子 1.0，2024-05-01 之后因子翻倍
    bars = bars.with_columns(
        pl.when(pl.col("date") >= pl.lit(datetime.date(2024, 5, 1)))
        .then(pl.lit(2.0))
        .otherwise(pl.lit(1.0))
        .alias("adjust")
    )
    _install(
        monkeypatch,
        [buy],
        bars,
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")

    assert payload is not None
    marker = next(m for m in payload["markers"] if m["tid"] == "t1")
    assert marker["raw_price"] == pytest.approx(12.5)
    # 锚点为最后一根 K 线因子 2.0，成交日因子 1.0 → 前复权价减半
    assert marker["price"] == pytest.approx(12.5 * 1.0 / 2.0)
    trade_bar = next(bar for bar in payload["bars"] if bar["dt"] == "2024-03-01")
    assert trade_bar["close"] == pytest.approx(12.0 * 1.0 / 2.0)


def test_build_payload_window_is_120_bars_plus_20_after_sell(monkeypatch) -> None:
    """主窗口 120 根（截至卖出日），卖出后再补最多 20 根。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    sell = _trade(
        tid="t2",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 6, 10, 14, 55),
        price=13.0,
    )
    _install(
        monkeypatch,
        [buy, sell],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 8, 30)),
        store_end=datetime.date(2024, 8, 30),
    )

    payload = build_trade_kline_payload("pf-1", "t1", "t2")

    assert payload is not None
    assert payload["post_trend_bars"] == TRADE_KLINE_POST_SELL_BARS
    assert len(payload["bars"]) == TRADE_KLINE_LOOKBACK_BARS + TRADE_KLINE_POST_SELL_BARS
    assert payload["bars"][-1]["dt"] == "2024-07-08"
    assert payload["post_start_dt"] == "2024-06-11"


def test_build_payload_post_sell_bars_limited_by_available_data(monkeypatch) -> None:
    """卖出后不足 20 根时尽量显示。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    sell = _trade(
        tid="t2",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 6, 10, 14, 55),
        price=13.0,
    )
    _install(
        monkeypatch,
        [buy, sell],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 14)),
        store_end=datetime.date(2024, 6, 14),
    )

    payload = build_trade_kline_payload("pf-1", "t1", "t2")

    assert payload is not None
    assert 0 < payload["post_trend_bars"] < TRADE_KLINE_POST_SELL_BARS
    assert len(payload["bars"]) == TRADE_KLINE_LOOKBACK_BARS + payload["post_trend_bars"]


def test_render_chart_tooltip_uses_two_decimals_and_daily_change(monkeypatch) -> None:
    """悬浮提示每行一个数值，保留两位小数，并给出当日涨跌幅。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30), price=12.345)
    _install(
        monkeypatch,
        [buy],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 3)),
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")
    assert payload is not None
    html = _html(render_trade_kline_chart(payload))

    assert "涨跌幅 " in html
    assert "toFixed(2)" in html
    assert "开盘 " in html
    assert "最高 " in html
    assert "最低 " in html
    assert "收盘 " in html
    assert "买入 " in html
    assert "卖出 " in html
    assert "lines.join('<br/>')" in html
    # 当日涨跌幅随 K 线一起下发
    assert "var pcts = [" in html


def test_render_panel_tabs_switches_between_features_and_shap(monkeypatch) -> None:
    """右侧面板为页签：默认特征页，可切到 SHAP 页。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    _install(
        monkeypatch,
        [buy],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 3)),
        store_end=datetime.date(2024, 6, 3),
    )

    payload = build_trade_kline_payload("pf-1", "t1")
    assert payload is not None
    payload["available_panels"] = ["features", "shap"]
    payload["active_panel"] = "features"

    tab_urls = {
        "shap": "/strategy/backtest/pf-1/trade/kline/modal?buy_tid=t1&panel=shap"
    }
    html = _html(render_trade_kline_chart(payload, tab_urls))
    assert 'hx-get="/strategy/backtest/pf-1/trade/kline/modal?buy_tid=t1&amp;panel=shap"' in html
    assert "特征" in html
    assert "SHAP" in html

    payload["active_panel"] = "shap"
    payload["attribution"] = {
        "asset": "002078.SZ",
        "date": datetime.date(2024, 6, 3),
        "engine": "LightGBM",
        "score": 1.2345,
        "baseline": -0.01,
        "rank": 7,
        "cross_section": 4505,
        "model_weight": 0.7,
        "prior_weight": 0.3,
        "features": [
            {"name": "rs_board_20d", "z": 1.2, "model": 0.01, "prior": 0.02, "total": 0.03}
        ],
    }
    html = _html(render_trade_kline_chart(payload, tab_urls))
    assert "归因" in html
    assert "rs_board_20d" in html
    assert "LightGBM" in html
    assert "#7 / 4505" in html
    # 归因页不再显示通用特征明细
    assert "bias_5" not in html


def test_shap_panel_shows_buy_and_sell_attribution(monkeypatch) -> None:
    """SHAP 面板同时给出买入侧与卖出侧归因。"""
    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    sell = _trade(
        tid="t2",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 6, 10, 14, 55),
        price=13.0,
    )
    _install(
        monkeypatch,
        [buy, sell],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 10)),
        store_end=datetime.date(2024, 6, 10),
    )

    payload = build_trade_kline_payload("pf-1", "t1", "t2")
    assert payload is not None
    payload["available_panels"] = ["features", "shap"]
    payload["active_panel"] = "shap"
    payload["attribution"] = {
        "asset": "002078.SZ",
        "date": datetime.date(2024, 6, 3),
        "engine": "LightGBM",
        "score": 1.5,
        "baseline": -0.1,
        "rank": 3,
        "cross_section": 100,
        "features": [{"name": "bias_5", "z": 1.0, "total": 0.05}],
    }
    payload["sell_attribution"] = {
        "asset": "002078.SZ",
        "date": datetime.date(2024, 6, 10),
        "engine": "LightGBM",
        "score": -0.8,
        "baseline": -0.1,
        "rank": 88,
        "cross_section": 100,
        "features": [{"name": "bias_5", "z": -1.2, "total": -0.06}],
    }

    html = _html(render_trade_kline_chart(payload, {}))

    assert "卖出时点评分" in html
    assert "#88 / 100" in html
    assert "#3 / 100" in html


def test_shap_attribution_only_computed_on_shap_tab(monkeypatch) -> None:
    """非 SHAP 页签时不应触发归因计算。"""
    calls: list[str] = []

    def _spy(portfolio_id, asset, decision_date, decision_tm=None):
        calls.append(asset)
        return {
            "asset": asset,
            "date": decision_date,
            "engine": "LightGBM",
            "score": 1.0,
            "baseline": 0.0,
            "rank": 1,
            "cross_section": 100,
            "features": [{"name": "bias_5", "z": 1.0, "total": 0.1}],
        }

    buy = _trade(tid="t1", tm=datetime.datetime(2024, 6, 3, 9, 30))
    _install(
        monkeypatch,
        [buy],
        _bars(datetime.date(2022, 1, 3), datetime.date(2024, 6, 3)),
        store_end=datetime.date(2024, 6, 3),
    )
    monkeypatch.setattr(
        trade_kline_module, "_portfolio_supports_attribution", lambda portfolio_id: True
    )
    monkeypatch.setattr(trade_kline_module, "_decision_attribution", _spy)

    payload = build_trade_kline_payload("pf-1", "t1")
    assert payload is not None
    assert payload["active_panel"] == "features"
    assert payload["attribution"] is None
    assert calls == []

    payload = build_trade_kline_payload("pf-1", "t1", panel="shap")
    assert payload is not None
    assert payload["active_panel"] == "shap"
    assert payload["attribution"] is not None
    assert calls == ["002078.SZ"]

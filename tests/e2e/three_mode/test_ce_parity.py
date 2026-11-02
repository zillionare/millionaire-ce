"""CE parity over actual HTTP, using independently calculated account results."""

import asyncio
import datetime
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import polars as pl
import pytest

from quantide.config.settings import get_timezone
from quantide.core.order_identity import client_order_id
from quantide.core.runtime.simulated_broker import SimulatedBrokerAdapter
from quantide.core.strategy import BaseStrategy
from quantide.data.models.calendar import calendar
from quantide.data.sqlite import db
from quantide.service.backtest_broker import BacktestBroker
from quantide.service.sim_broker import PaperBroker

pytestmark = [pytest.mark.e2e, pytest.mark.release_gate]
START = datetime.datetime(2024, 1, 2, 10, tzinfo=get_timezone())
SYMBOL = "000001.SZ"


class ScheduleStrategy(BaseStrategy):
    async def on_bar(self, tm):
        index = (tm.date() - START.date()).days
        if index == 0:
            await self.broker.buy(SYMBOL, 200, price=10, order_time=tm, timeout=0.001)
        else:
            await self.broker.sell(SYMBOL, 100, price=10 + index, order_time=tm, timeout=0.001)


class Feed:
    def __init__(self):
        self.frame = pl.DataFrame([{
            "date": START.date() + datetime.timedelta(days=index), "asset": SYMBOL,
            "open": 10.0 + index, "close": 10.0 + index, "high": 10.0 + index,
            "low": 10.0 + index, "volume": 10000.0, "amount": 100000.0,
            "adjust": 1.0, "is_st": False, "up_limit": 99.0, "down_limit": 1.0,
        } for index in range(3)])

    def get_price_for_match(self, asset, tm):
        return self.frame.filter((pl.col("asset") == asset) & (pl.col("date") == tm.date()))

    def get_trade_price_limits(self, asset, day):
        return 1.0, 99.0

    def get_bars(self, n, end=None, assets=None, **kwargs):
        return self.frame.filter(pl.col("date") <= end).tail(n)

    def get_bars_in_range(self, start, end=None, assets=None, **kwargs):
        return self.frame.filter(pl.col("date").is_between(start, end or start))

    def get_close_adjust_factor(self, assets, start, end):
        return self.get_bars_in_range(start, end).select("date", "asset", "close", "adjust")


@pytest.fixture
def counter_server(tmp_path):
    processes = []

    def start(**settings):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        database = tmp_path / "counter.db"
        source = (
            "import uvicorn; from quantide.service.simulated_counter_api import create_counter_app; "
            f"uvicorn.run(create_counter_app({str(database)!r}, **{settings!r}), host='127.0.0.1', port={port}, "
            "log_level='error')"
        )
        process = subprocess.Popen([sys.executable, "-c", source], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        processes.append(process)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with httpx.Client(trust_env=False, timeout=0.2) as client:
                    client.get(url + "/state").raise_for_status()
                return process, url
            except httpx.HTTPError:
                assert process.poll() is None, "counter exited during startup"
                time.sleep(0.02)
        raise AssertionError("counter did not start")

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("commission,stamp_tax,expected_cash", [
    (0.0, 0.0, [8000.0, 9100.0, 10300.0]),
    (0.001, 0.001, [7998.0, 9095.8, 10293.4]),
])
async def test_three_modes_match_manual_account_facts(
    tmp_path, counter_server, commission, stamp_tax, expected_cash
):
    calendar.load(Path(__file__).parents[2] / "assets" / "baseline_calendar.parquet")
    db.init(tmp_path / "app.db")
    _, url = counter_server(commission=commission, stamp_tax=stamp_tax)
    remote = SimulatedBrokerAdapter(url)
    paper = PaperBroker.create("parity-paper", principal=10000, commission=commission,
                               stamp_tax=stamp_tax, min_commission=0,
                               clock=type("Clock", (), {"now": lambda self: START})())
    backtest = BacktestBroker(START.date(), START.date() + datetime.timedelta(days=2),
                             "parity-backtest", Feed(), principal=10000, commission=commission,
                             stamp_duty=stamp_tax, transfer_fee=0, min_commission=0)
    strategies = [ScheduleStrategy(broker, {}) for broker in (backtest, paper, remote)]
    try:
        for index in range(3):
            tm = START + datetime.timedelta(days=index)
            backtest.set_clock(tm.replace(tzinfo=get_timezone()))
            paper.set_clock(tm)
            paper.settle_t1()
            remote.on_quote(tm, {SYMBOL: {"price": 10 + index}})
            for strategy in strategies:
                await strategy.on_bar(tm)
            paper.on_quote({SYMBOL: {"price": 10 + index}})
            cash = expected_cash[index]
            expected_shares = [200, 100, 0][index]
            for broker in (backtest, paper):
                assert broker.cash == pytest.approx(cash)
                positions = broker.positions
                if isinstance(positions, dict):
                    positions = positions.values()
                assert sum(position.shares for position in positions) == expected_shares
            assert remote.query_assets().cash == pytest.approx(cash)
            assert sum(position.shares for position in remote.query_positions()) == expected_shares
            assert remote.query_assets().total == pytest.approx(cash + expected_shares * (10 + index))
        expected_fees = [2000 * commission, 1100 * (commission + stamp_tax),
                         1200 * (commission + stamp_tax)]
        for portfolio in ("parity-paper", "parity-backtest"):
            trades = db.trades_all(portfolio_id=portfolio).sort("tm")
            assert trades["shares"].to_list() == [200, 100, 100]
            assert trades["price"].to_list() == [10, 11, 12]
            assert trades["fee"].to_list() == pytest.approx(expected_fees)
        assert [trade.shares for trade in remote.query_trades()] == [200, 100, 100]
        assert [trade.fee for trade in remote.query_trades()] == pytest.approx(expected_fees)
    finally:
        paper.close()
        remote.close()
        db.close()


@pytest.mark.asyncio
async def test_counter_duplicate_partial_cancel_and_abrupt_restart(counter_server):
    process, url = counter_server()
    broker = SimulatedBrokerAdapter(url)
    broker.on_quote(START, {SYMBOL: {"price": 10, "available_shares": 100}})
    token = client_order_id.set("durable-intent")
    try:
        await broker.buy(SYMBOL, 200, price=10, order_time=START)
        await broker.buy(SYMBOL, 200, price=10, order_time=START)
    finally:
        client_order_id.reset(token)
    assert broker.query_assets().cash == 9000
    assert len(broker.query_trades()) == 1
    process.kill()
    process.wait(timeout=10)
    broker.close()
    _, restarted_url = counter_server()
    broker = SimulatedBrokerAdapter(restarted_url)
    try:
        assert broker.find_order_by_client_id("durable-intent")["filled"] == 100
        await broker.cancel("durable-intent")
        broker.on_quote(START + datetime.timedelta(days=1), {SYMBOL: {"price": 11}})
        assert len(broker.query_trades()) == 1
        assert broker.query_positions()[0].avail == 100
        assert broker.query_assets().total == 10100
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_counter_bulk_cancel_accepts_strategy_order_side_enum(counter_server):
    from quantide.core.enums import OrderSide

    _, url = counter_server()
    broker = SimulatedBrokerAdapter(url)
    try:
        await broker.buy(SYMBOL, 100, price=9, order_time=START)
        await broker.buy(SYMBOL, 100, price=8, order_time=START)
        assert len(broker.query_orders("open")) == 2
        assert await broker.cancel_all_orders(OrderSide.SELL) == 0
        assert await broker.cancel_all_orders(OrderSide.BUY) == 2
        assert broker.query_orders("open") == []
        assert len(broker.query_orders("cancelled")) == 2
    finally:
        broker.close()

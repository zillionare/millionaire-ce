"""BrokerPort client for the CE loopback demonstration protocol."""

import datetime
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from quantide.core.enums import OrderSide
from quantide.core.order_identity import client_order_id
from quantide.core.ports.broker import (
    AssetView,
    CancelAck,
    ExecutionResult,
    OrderView,
    PositionView,
)
from quantide.data.models import Trade
from quantide.service.abstract_broker import AbstractBroker


class SimulatedBrokerAdapter(AbstractBroker):
    """Exercise real HTTP submission and reconciliation without enabling live mode."""

    supports_idempotent_orders = True

    def __init__(self, base_url, portfolio_id="simulated", market_data=None):
        super().__init__(portfolio_id=portfolio_id)
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("The built-in simulated counter must use loopback HTTP")
        self._client = httpx.Client(base_url=base_url, timeout=5, trust_env=False)
        self._market_data = market_data
        self._state()

    def close(self):
        """Close the client transport without changing the counter's account."""
        self._client.close()

    def _state(self):
        response = self._client.get("/state")
        response.raise_for_status()
        payload = response.json()
        if payload.get("mode") != "simulated":
            raise ValueError("Endpoint is not a CE simulated counter")
        return payload["state"]

    def _post(self, endpoint, payload):
        response = self._client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.json()

    def find_order_by_client_id(self, identity):
        return self._state()["orders"].get(identity)

    def on_quote(self, time, quotes):
        """Drive the simulation using replay input, rather than expected outputs."""
        return self._post("/quotes", {"time": time.isoformat(), "quotes": quotes})

    async def _order(self, side, asset, shares, price, order_time):
        identity = client_order_id.get() or uuid4().hex
        payload = {
            "client_order_id": identity,
            "side": side,
            "asset": asset,
            "shares": shares,
            "price": price,
            "time": order_time.isoformat() if order_time else "",
        }
        self._post("/orders", payload)
        return ExecutionResult(qt_oid=identity, trades=self.query_trades(identity))

    async def buy(self, asset, shares, price=0, order_time=None, timeout=0.5):
        return await self._order("buy", asset, shares, price, order_time)

    async def sell(self, asset, shares, price=0, order_time=None, timeout=0.5):
        return await self._order("sell", asset, shares, price, order_time)

    async def buy_amount(self, asset, amount, price=0, order_time=None, timeout=0.5):
        value = price or self.get_prices([asset]).get(asset, 0)
        shares = int(amount / value / 100) * 100 if value > 0 else 0
        if not shares:
            return ExecutionResult.empty()
        return await self.buy(asset, shares, price, order_time, timeout)

    async def buy_percent(self, asset, percent, price=0, order_time=None, timeout=0.5):
        return await self.buy_amount(asset, self.query_assets().total * percent, price, order_time)

    async def sell_amount(self, asset, amount, price=0, order_time=None, timeout=0.5):
        value = price or self.get_prices([asset]).get(asset, 0)
        shares = int(amount / value / 100) * 100 if value > 0 else 0
        if not shares:
            return ExecutionResult.empty()
        return await self.sell(asset, shares, price, order_time, timeout)

    async def sell_percent(self, asset, percent, price=0, order_time=None, timeout=0.5):
        position = next((item for item in self.query_positions() if item.asset == asset), None)
        if position is None:
            return ExecutionResult.empty()
        shares = (
            position.shares if percent >= 0.9999 else int(position.shares * percent / 100) * 100
        )
        if not shares:
            return ExecutionResult.empty()
        return await self.sell(asset, shares, price, order_time, timeout)

    async def trade_target_pct(self, asset, target_pct, price=0, order_time=None, timeout=0.5):
        positions = {position.asset: position for position in self.query_positions()}
        current = positions[asset].mv if asset in positions else 0
        delta = self.query_assets().total * target_pct - current
        operation = self.buy_amount if delta > 0 else self.sell_amount
        return await operation(asset, abs(delta), price, order_time, timeout)

    async def cancel(self, order_id):
        self._post("/cancel", order_id)
        return CancelAck(success=True)

    async def cancel_order(self, order_id):
        return await self.cancel(order_id)

    async def cancel_all_orders(self, side=None):
        orders = self._state()["orders"].values()
        side_name = getattr(side, "name", str(side)).lower() if side is not None else None
        pending = [
            order
            for order in orders
            if order["status"] in {"open", "partial"}
            and (side is None or order["request"]["side"] == side_name)
        ]
        for order in pending:
            await self.cancel(order["order_id"])
        return len(pending)

    def query_orders(self, status=None):
        return [
            OrderView(
                order_id=order["order_id"],
                asset=order["request"]["asset"],
                side=order["request"]["side"],
                shares=order["request"]["shares"],
                price=order["request"]["price"],
                status=order["status"],
                filled=order["filled"],
                tm=datetime.datetime.fromisoformat(order["request"]["time"])
                if order["request"]["time"]
                else datetime.datetime.min,
            )
            for order in self._state()["orders"].values()
            if status is None or order["status"] == status
            if status is None or order["status"] == status
        ]

    def query_assets(self):
        state = self._state()
        return AssetView(
            cash=state["cash"],
            total=state["total"],
            market_value=state["market_value"],
            frozen_cash=0,
            principal=state["principal"],
            dt=datetime.date.fromisoformat(state["day"]) if state["day"] else datetime.date.today(),
        )

    def query_positions(self):
        state = self._state()
        day = datetime.date.fromisoformat(state["day"]) if state["day"] else datetime.date.today()
        return [
            PositionView(
                asset=symbol,
                shares=position["shares"],
                avail=position["avail"],
                price=position["price"],
                dt=day,
                mv=position["shares"]
                * state["quotes"].get(symbol, {}).get("price", position["price"]),
            )
            for symbol, position in state["positions"].items()
        ]

    def query_trades(self, order_id=None):
        rows = self._state()["trades"]
        return [
            Trade(
                portfolio_id=self.portfolio_id,
                tid=row["trade_id"],
                qtoid=row["order_id"],
                foid="",
                cid="",
                asset=row["asset"],
                shares=row["shares"],
                price=row["price"],
                amount=row["amount"],
                fee=row["fee"],
                tm=datetime.datetime.fromisoformat(row["date"]),
                side=OrderSide.BUY if row["side"] == "buy" else OrderSide.SELL,
            )
            for row in rows
            if order_id is None or row["order_id"] == order_id
        ]

    def get_prices(self, assets):
        quotes = self._state()["quotes"]
        return {asset: quotes[asset]["price"] for asset in assets if asset in quotes}

    def get_history(self, *args, **kwargs):
        if self._market_data is None:
            raise RuntimeError(
                "Inject a historical market-data port for strategies requiring history"
            )
        return self._market_data.get_history(*args, **kwargs)

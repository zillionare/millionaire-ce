"""基于 pytdx 的全市场行情发布者（内部使用）。

与公开示例（:mod:`quantide.providers.akshare_publisher`，50 支标的）不同，
本发布者覆盖**全市场**，供自用环境使用。两者发布出去的字段结构完全一致，
Quantide 侧无需区分。

pytdx 走通达信协议，默认连本机 ``7709`` 行情端口；
连接参数可通过环境变量覆盖：

* ``PYTDX_HOST``（默认 127.0.0.1）
* ``PYTDX_PORT``（默认 7709）
* ``PYTDX_INTERVAL``（轮询间隔秒，默认 3.0）
"""

from __future__ import annotations

import datetime
import os
import threading
from typing import Any

from loguru import logger

from quantide.core.enums import Topics
from quantide.core.message import msg_hub

#: 默认轮询间隔（秒）
DEFAULT_INTERVAL = 3.0


class PytdxPublisher:
    """pytdx 全市场行情发布者。"""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        interval: float | None = None,
    ) -> None:
        """初始化发布者。

        Args:
            host: 通达信行情服务器地址。
            port: 行情端口。
            interval: 轮询间隔（秒）。
        """
        self.host = host or os.environ.get("PYTDX_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("PYTDX_PORT", "7709"))
        self.interval = float(
            interval or os.environ.get("PYTDX_INTERVAL", DEFAULT_INTERVAL)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: Any = None

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def _connect(self) -> Any:
        """建立 pytdx 连接。"""
        from pytdx.hq import TdxHq_API

        api = TdxHq_API(heartbeat=True)
        with api.connect(self.host, self.port):
            pass
        self._api = api
        logger.info("pytdx 已连接 {}:{}", self.host, self.port)
        return api

    def _disconnect(self) -> None:
        """断开连接。"""
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:  # pragma: no cover - 断开失败忽略
                pass
            self._api = None

    # ------------------------------------------------------------------
    # 拉取与发布
    # ------------------------------------------------------------------
    def fetch_snapshot(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """拉取指定标的的实时行情（统一结构）。"""
        if self._api is None:
            self._connect()
        api = self._api
        if api is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        now = datetime.datetime.now().isoformat()
        with api.connect(self.host, self.port):
            for symbol in symbols:
                market, number = _split_symbol(symbol)
                try:
                    data = api.get_security_quotes([(market, number)])
                except Exception as exc:
                    logger.debug("pytdx 拉取 {} 失败: {}", symbol, exc)
                    continue
                if not data:
                    continue
                item = data[0]
                price = _to_float(item.get("last"))
                if price is None or price <= 0:
                    continue
                result[symbol] = {
                    "price": price,
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "volume": _to_float(item.get("vol")),
                    "amount": _to_float(item.get("amount")),
                    "ts": now,
                }
        return result

    def publish_once(self, symbols: list[str]) -> int:
        """拉取并发布一次。"""
        try:
            snapshot = self.fetch_snapshot(symbols)
        except Exception as exc:
            logger.warning("pytdx 拉取行情失败: {}", exc)
            return 0
        if not snapshot:
            return 0
        msg_hub.publish(Topics.QUOTES_ALL.value, snapshot)
        return len(snapshot)

    def start(self, symbols: list[str]) -> None:
        """启动发布线程。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(list(symbols),), daemon=True, name="pytdx-publisher"
        )
        self._thread.start()
        logger.info("pytdx 行情发布者已启动，标的数 {}", len(symbols))

    def stop(self) -> None:
        """停止发布线程。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)
        self._disconnect()

    def _loop(self, symbols: list[str]) -> None:
        """轮询主循环。"""
        while not self._stop.is_set():
            count = self.publish_once(symbols)
            if count:
                logger.debug("已发布 {} 个标的的行情", count)
            self._stop.wait(self.interval)


def _split_symbol(symbol: str) -> tuple[int, str]:
    """统一代码转 pytdx 的 (market, code)。

    market: 0=深圳, 1=上海。
    """
    text = str(symbol).strip().upper()
    if "." in text:
        number, market = text.split(".", 1)
    else:
        number, market = text, "SH" if text.startswith(("5", "6", "9")) else "SZ"
    return (1 if market == "SH" else 0), number


def _to_float(value: Any) -> float | None:
    """任意值转 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

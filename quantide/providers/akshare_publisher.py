"""基于 AkShare 的行情发布者（发行版公开示例）。

这是**参考实现**：展示"如何把任意行情源接入 Quantide"的标准写法。
用户可以复制这个文件、替换数据源，保持发布出去的字段结构不变即可。

行为
----
* 周期性（默认 3 秒）拉取指定标的的实时行情；
* 把快照发布到消息总线 ``quotes.all``；
* 涨跌停价可选发布（``stk_limit`` 或本地日线）。

运行
----
```bash
pip install akshare
python -m quantide.providers.akshare_publisher --symbols 600519.SH,000001.SZ
```

在 Quantide 内部使用时，直接调用 :func:`AksharePublisher.start` 即可。
"""

from __future__ import annotations

import argparse
import datetime
import threading
import time
from typing import Any

from loguru import logger

from quantide.core.enums import Topics
from quantide.core.message import msg_hub

#: 默认轮询间隔（秒）
DEFAULT_INTERVAL = 3.0


def _to_market_symbol(code: str) -> str:
    """把 AkShare 的 6 位代码转成统一代码（600519 -> 600519.SH）。"""
    text = str(code).strip()
    if "." in text:
        return text.upper()
    if text.startswith(("5", "6", "9", "688", "11")):
        return f"{text}.SH"
    return f"{text}.SZ"


def _akshare_to_unified(code: str) -> str:
    """把 AkShare 代码（sh600519 / 600519）转统一代码。"""
    text = str(code).strip().upper()
    if text.startswith(("SH", "SZ", "BJ")):
        number, market = text[2:], text[:2]
        return f"{number}.{market}"
    return _to_market_symbol(text)


class AksharePublisher:
    """AkShare 行情发布者（示例实现）。

    Attributes:
        symbols: 统一代码列表，如 ``["600519.SH"]``。
        interval: 轮询间隔（秒）。
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        """初始化发布者。

        Args:
            symbols: 需要发布的标的；为空表示使用示例默认 50 支。
            interval: 轮询间隔（秒）。
        """
        self.symbols = list(symbols or DEFAULT_SYMBOLS)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 数据拉取
    # ------------------------------------------------------------------
    def _fetch_snapshot(self) -> dict[str, dict[str, Any]]:
        """拉取一次全市场快照并转成统一结构。"""
        import akshare as ak

        frame = ak.stock_zh_a_spot_em()
        wanted = {s.upper() for s in self.symbols}
        result: dict[str, dict[str, Any]] = {}
        now = datetime.datetime.now().isoformat()
        for row in frame.to_dict("records"):
            code = str(row.get("代码") or "").strip()
            if not code:
                continue
            symbol = _to_market_symbol(code)
            if wanted and symbol not in wanted:
                continue
            price = _to_float(row.get("最新价"))
            if price is None or price <= 0:
                continue
            result[symbol] = {
                "price": price,
                "open": _to_float(row.get("今开")),
                "high": _to_float(row.get("最高")),
                "low": _to_float(row.get("最低")),
                "volume": _to_float(row.get("成交量")),
                "amount": _to_float(row.get("成交额")),
                "ts": now,
            }
        return result

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish_once(self) -> int:
        """拉取并发布一次，返回发布的标的数。"""
        try:
            snapshot = self._fetch_snapshot()
        except Exception as exc:
            logger.warning("拉取行情失败: {}", exc)
            return 0
        if not snapshot:
            return 0
        msg_hub.publish(Topics.QUOTES_ALL.value, snapshot)
        return len(snapshot)

    def start(self) -> None:
        """启动发布线程。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="akshare-publisher"
        )
        self._thread.start()
        logger.info("AkShare 行情发布者已启动，标的数 {}", len(self.symbols))

    def stop(self) -> None:
        """停止发布线程。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    def _loop(self) -> None:
        """轮询主循环。"""
        while not self._stop.is_set():
            count = self.publish_once()
            if count:
                logger.debug("已发布 {} 个标的的行情", count)
            self._stop.wait(self.interval)


def _to_float(value: Any) -> float | None:
    """任意值转 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: 示例默认标的（50 支，覆盖沪深主板与创业板）
DEFAULT_SYMBOLS: list[str] = [
    "600519.SH", "601398.SH", "600036.SH", "601318.SH", "600030.SH",
    "600887.SH", "601988.SH", "600276.SH", "600900.SH", "601166.SH",
    "600585.SH", "601012.SH", "600031.SH", "601888.SH", "600438.SH",
    "600690.SH", "601668.SH", "600104.SH", "600050.SH", "601857.SH",
    "601088.SH", "600009.SH", "600028.SH", "601601.SH", "600011.SH",
    "000001.SZ", "000002.SZ", "000333.SZ", "000651.SZ", "000858.SZ",
    "002415.SZ", "002594.SZ", "000725.SZ", "002304.SZ", "000063.SZ",
    "000538.SZ", "002352.SZ", "300750.SZ", "300059.SZ", "300124.SZ",
    "300760.SZ", "601899.SH", "600309.SH", "000568.SZ", "002027.SZ",
    "600741.SH", "002049.SZ", "300015.SZ", "600893.SH", "000625.SZ",
]


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="AkShare 行情发布者（示例）")
    parser.add_argument(
        "--symbols",
        default="",
        help="逗号分隔的标的代码；为空则使用默认 50 支",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    publisher = AksharePublisher(symbols or None, interval=args.interval)
    publisher.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        publisher.stop()


if __name__ == "__main__":
    main()

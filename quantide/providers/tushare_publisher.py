"""Opt-in official Tushare realtime snapshots at minute frequency."""

import threading

from loguru import logger

from quantide.core.market_time import market_now
from quantide.core.scheduler import scheduler
from quantide.data.fetchers.tushare_realtime import TushareRealtimeFetcher
from quantide.providers.extensions import publish_quotes
from quantide.service.minute_bar_aggregator import MinuteBarAggregator
from quantide.service.scheduled_jobs import remove_owned_jobs

JOB_ID = "tushare_realtime_quotes"


class TusharePublisher:
    """Share the existing quote bus and preserve strict publisher shutdown ownership."""

    source_name = "tushare"

    def __init__(self, fetcher=None, market_data=None, calendar=None, clock=None):
        self._cancel = threading.Event()
        self._fetcher = fetcher or TushareRealtimeFetcher(cancel=self._cancel)
        self._market_data = market_data
        self._calendar = calendar
        self._clock = clock or market_now
        self._lock = threading.RLock()
        self._scheduled = False

    def start(self):
        with self._lock:
            if self._scheduled:
                return
            self._cancel.clear()
            scheduler.add_job(
                self.publish_once,
                "interval",
                seconds=60,
                id=JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            self._scheduled = True

    def stop(self):
        self._cancel.set()
        remove_owned_jobs(self, scheduler, (JOB_ID,))

    def publish_once(self):
        from quantide.data.models.calendar import calendar
        from quantide.service.market_data_hub import market_data_hub

        now = self._clock()
        with self._lock:
            if not self._scheduled or not MinuteBarAggregator.is_session(
                now.replace(second=0, microsecond=0)
            ):
                return 0
            if not (self._calendar or calendar).is_trade_day(now):
                return 0
            symbols = (self._market_data or market_data_hub).subscribed_symbols()
            symbols = [asset for asset in symbols if asset.endswith((".SH", ".SZ", ".BJ"))]
            if not symbols:
                return 0
        try:
            quotes = self._fetcher.fetch_quote_snapshot(symbols, now)
        except Exception:
            logger.warning("Tushare 实时行情读取失败，请检查账号权限、限频和数据源状态")
            return 0
        with self._lock:
            if not self._scheduled or self._cancel.is_set():
                return 0
            limits = {
                asset: {key: row[key] for key in ("up_limit", "down_limit")}
                for asset, row in quotes.items()
            }
            publish_quotes(quotes, limits)
            return len(quotes)

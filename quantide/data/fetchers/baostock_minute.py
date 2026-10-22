"""BaoStock 分钟线数据源适配器.

**BaoStock 使用约束（发行版必须遵守）**：

* 每日 API 请求不能超过 5 万次，超限进入黑名单；
* **不能并发连接**（单 socket，串行访问）；
* 本自然年度初次被黑名单会冻结 6 小时；本年多次被限，冻结时长 = 累计次数 × 6 小时；
* 解除后需等待约 5 分钟才可继续使用。

因此本适配器：

* **严格单线程串行**访问（内部锁 + 单会话）；
* 维护**每日请求预算**（默认 45000，留 10% 余量），超预算即停止并报错；
* 识别黑名单/限频错误（错误信息含"黑名单"/"限制"/"频率"），**立即停止**并把
  ``blacklisted_until`` 标记为 6 小时后，避免继续撞限。

分钟线历史实测边界：30 分钟线自 2020-01-02 起可用（日线可回溯到 2005）。
"""

from __future__ import annotations

import datetime
import threading
import time
from collections.abc import Iterable
from typing import Any

import pandas as pd

#: 单次请求的默认重试次数（网络抖动）
DEFAULT_RETRIES = 2
#: 每日请求预算（硬上限 5 万，留 10% 余量）
DEFAULT_DAILY_BUDGET = 45000
#: 识别为黑名单/限频的错误关键词
BLOCKED_KEYWORDS = ("黑名单", "限制", "频率超限", "冻结")


class BaostockThrottledError(RuntimeError):
    """达到请求预算或被限频（黑名单）时抛出。"""


class BaostockMinuteFetcher:
    """BaoStock 分钟线抓取器（串行 + 预算受限）。

    仅在需要分钟级历史时使用；日线请用 Tushare。
    """

    def __init__(
        self,
        daily_budget: int = DEFAULT_DAILY_BUDGET,
        min_history_date: datetime.date = datetime.date(2020, 1, 1),
    ) -> None:
        """初始化抓取器。

        Args:
            daily_budget: 每日 API 请求预算。
            min_history_date: 分钟线可用的最早日期（早于此日期无数据）。
        """
        self._daily_budget = int(daily_budget)
        self._min_history_date = min_history_date
        self._lock = threading.Lock()
        self._logged_in = False
        self._requests_today = 0
        self._budget_date: datetime.date = datetime.date.today()
        self._blacklisted_until: datetime.datetime | None = None

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------
    def _ensure_login(self) -> None:
        """确保已登录（BaoStock 单连接，登录失败会抛错）。"""
        if self._logged_in:
            return
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            raise BaostockThrottledError(f"BaoStock 登录失败: {lg.error_msg}")
        self._logged_in = True

    def close(self) -> None:
        """登出会话。"""
        with self._lock:
            if not self._logged_in:
                return
            try:
                import baostock as bs

                bs.logout()
            except Exception:  # pragma: no cover - 登出失败忽略
                pass
            self._logged_in = False

    def __enter__(self) -> BaostockMinuteFetcher:
        self._ensure_login()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 预算与黑名单
    # ------------------------------------------------------------------
    def _roll_budget(self) -> None:
        """跨日重置请求计数。"""
        today = datetime.date.today()
        if today != self._budget_date:
            self._budget_date = today
            self._requests_today = 0

    @property
    def requests_today(self) -> int:
        """今日已用请求数。"""
        return self._requests_today

    @property
    def budget_remaining(self) -> int:
        """今日剩余请求数。"""
        return max(0, self._daily_budget - self._requests_today)

    def _check_budget(self) -> None:
        """检查黑名单状态与每日预算。"""
        now = datetime.datetime.now()
        if self._blacklisted_until and now < self._blacklisted_until:
            remaining = self._blacklisted_until - now
            raise BaostockThrottledError(
                f"BaoStock 处于黑名单冻结期，剩余 {remaining}（解除后还需等待约 5 分钟）"
            )
        self._roll_budget()
        if self._requests_today >= self._daily_budget:
            raise BaostockThrottledError(
                f"BaoStock 今日请求预算已用尽（{self._requests_today}/{self._daily_budget}），"
                "请明日继续或调高预算"
            )

    def _consume(self) -> None:
        self._requests_today += 1

    def _handle_error(self, message: str) -> None:
        """处理抓取错误；限频/黑名单会标记 6 小时冻结期。"""
        if self._is_blocking(message):
            self._blacklisted_until = datetime.datetime.now() + datetime.timedelta(
                hours=6
            )
            raise BaostockThrottledError(
                f"BaoStock 触发黑名单/访问限制，暂停 6 小时: {message}"
            )
        raise BaostockThrottledError(f"BaoStock 请求失败: {message}")

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------
    @staticmethod
    def _to_baostock_code(asset: str) -> str:
        """把统一代码转成 BaoStock 格式（sh.600000 / sz.000001）。"""
        code = asset.strip().upper()
        if "." in code:
            number, market = code.split(".", 1)
            return f"{market.lower()}.{number}"
        if code.startswith(("5", "6", "9", "688", "11")):
            return f"sh.{code}"
        return f"sz.{code}"

    @staticmethod
    def _is_blocking(message: str) -> bool:
        """判断错误信息是否属于限频/黑名单。"""
        return any(keyword in message for keyword in BLOCKED_KEYWORDS)

    def fetch_minute_bars(
        self,
        asset: str,
        start_date: datetime.date,
        end_date: datetime.date,
        frequency: str = "30",
        adjust: str = "3",
    ) -> pd.DataFrame:
        """抓取单只股票的分钟线。

        Args:
            asset: 统一代码，如 ``600000.SH``。
            start_date: 起始日。
            end_date: 结束日。
            frequency: 分钟周期（``5`` / ``15`` / ``30`` / ``60``）。
            adjust: 复权标志（``1`` 后复权 / ``2`` 前复权 / ``3`` 不复权）。

        Returns:
            pd.DataFrame: 字段 date, time, asset, open, high, low, close, volume, amount。
        """
        if start_date < self._min_history_date:
            start_date = self._min_history_date
        with self._lock:
            self._check_budget()
            self._ensure_login()
            import baostock as bs

            last_error = ""
            for attempt in range(DEFAULT_RETRIES + 1):
                self._consume()
                try:
                    rs = bs.query_history_k_data_plus(
                        self._to_baostock_code(asset),
                        "date,time,code,open,high,low,close,volume,amount",
                        start_date=start_date.isoformat(),
                        end_date=end_date.isoformat(),
                        frequency=str(frequency),
                        adjustflag=str(adjust),
                    )
                except Exception as exc:
                    last_error = str(exc)
                    self._handle_error(last_error)
                    time.sleep(1.0 * (attempt + 1))
                    continue

                if rs.error_code != "0":
                    last_error = rs.error_msg
                    self._handle_error(last_error)
                    time.sleep(1.0 * (attempt + 1))
                    continue

                rows: list[list[str]] = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return pd.DataFrame()
                frame = pd.DataFrame(rows, columns=rs.fields)
                return self._normalize(frame, asset)

            raise BaostockThrottledError(f"BaoStock 抓取失败: {last_error}")

    def fetch_many(
        self,
        assets: Iterable[str],
        start_date: datetime.date,
        end_date: datetime.date,
        frequency: str = "30",
        adjust: str = "3",
        progress: Any = None,
    ) -> pd.DataFrame:
        """串行抓取多只股票的分钟线（受每日预算限制）。

        Args:
            assets: 统一代码列表。
            start_date: 起始日。
            end_date: 结束日。
            frequency: 分钟周期。
            adjust: 复权标志。
            progress: 进度回调 ``(done, total)``。

        Returns:
            pd.DataFrame: 合并后的分钟线。
        """
        assets = list(assets)
        frames: list[pd.DataFrame] = []
        for index, asset in enumerate(assets, start=1):
            frame = self.fetch_minute_bars(
                asset, start_date, end_date, frequency=frequency, adjust=adjust
            )
            if not frame.empty:
                frames.append(frame)
            if progress is not None:
                progress(index, len(assets))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def _normalize(frame: pd.DataFrame, asset: str) -> pd.DataFrame:
        """把 BaoStock 原始表转成统一字段。"""
        out = frame.rename(
            columns={
                "code": "asset",
                "vol": "volume",
            }
        )
        if "asset" in out.columns:
            out["asset"] = asset
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "time" in out.columns:
            out["dt"] = pd.to_datetime(
                out["time"].astype(str), format="%Y%m%d%H%M%S%f", errors="coerce"
            )
        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
        return out

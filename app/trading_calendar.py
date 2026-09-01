from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Iterable
from urllib.parse import urlparse

from .config import local_now


SSE_CLOSURE_SOURCE_2025 = (
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20241223_10767110.shtml"
)
SSE_CLOSURE_SOURCE_2026 = (
    "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"
)

# Only weekday closures need to be listed: weekends are rejected separately.
# These dates are copied from the exchange's annual closure notices above.
BUILTIN_CLOSED_DATES = frozenset({
    # 2025
    date(2025, 1, 1),
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),
    date(2025, 2, 3), date(2025, 2, 4),
    date(2025, 4, 4),
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5),
    date(2025, 6, 2),
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),
    # 2026
    date(2026, 1, 1), date(2026, 1, 2),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),
    date(2026, 4, 6),
    date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
    date(2026, 10, 6), date(2026, 10, 7),
})


class TradingCalendarError(ValueError):
    pass


@dataclass(frozen=True)
class FundOrderSchedule:
    submitted_at: datetime
    pricing_date: date
    estimated_confirm_date: date
    fund_kind: str
    confirmation_lag_trading_days: int
    rolled_after_cutoff: bool
    calendar_source: str


def _parse_dates(values: Iterable[str | date]) -> set[date]:
    parsed: set[date] = set()
    for value in values:
        try:
            item = value if isinstance(value, date) else date.fromisoformat(str(value))
        except ValueError as exc:
            raise TradingCalendarError(f"交易日历日期无效：{value}") from exc
        parsed.add(item)
    return parsed


def infer_fund_kind(*texts: str) -> str:
    combined = " ".join(str(value or "") for value in texts).upper()
    return "QDII" if "QDII" in combined or any(
        token in combined for token in ("纳斯达克", "标普", "海外", "恒生", "日经")
    ) else "普通场外"


class ChinaTradingCalendar:
    """China A-share trading calendar plus conservative OTC fund timing rules.

    Built-in dates are deliberately finite and source-labelled.  Later exchange
    notices can be applied through :meth:`update_overrides` without a schema
    migration.  Product-specific QDII overseas holidays remain outside this
    calendar and must still be confirmed from the fund announcement.
    """

    DEFAULT_CUTOFF = dt_time(15, 0)
    CONFIRMATION_LAGS = {"普通场外": 1, "QDII": 2}

    def __init__(
        self,
        *,
        extra_closed_dates: Iterable[str | date] = (),
        extra_open_dates: Iterable[str | date] = (),
        source: str = "",
    ):
        self.extra_closed_dates = frozenset(_parse_dates(extra_closed_dates))
        self.extra_open_dates = frozenset(_parse_dates(extra_open_dates))
        overlap = self.extra_closed_dates.intersection(self.extra_open_dates)
        if overlap:
            joined = "、".join(sorted(item.isoformat() for item in overlap))
            raise TradingCalendarError(f"交易日历同一天同时标为开市和休市：{joined}")
        self.source = source.strip() or f"上交所年度休市安排：{SSE_CLOSURE_SOURCE_2026}"

    @classmethod
    def from_db(cls, db) -> "ChinaTradingCalendar":
        def read_list(key: str) -> list[str]:
            raw = db.setting(key, "[]")
            try:
                value = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        try:
            return cls(
                extra_closed_dates=read_list("trading_calendar_closed_dates_json"),
                extra_open_dates=read_list("trading_calendar_open_dates_json"),
                source=db.setting("trading_calendar_source_url", ""),
            )
        except TradingCalendarError:
            # A corrupt local override must not turn a weekday into a false open
            # day.  Fall back to the source-backed built-in calendar.
            return cls()

    @classmethod
    def update_overrides(
        cls,
        db,
        *,
        closed_dates: Iterable[str | date],
        open_dates: Iterable[str | date] = (),
        source_url: str,
        retrieved_at: datetime | None = None,
    ) -> "ChinaTradingCalendar":
        parsed = urlparse(str(source_url or ""))
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise TradingCalendarError("交易日历更新必须附带HTTPS来源地址。")
        calendar = cls(
            extra_closed_dates=closed_dates,
            extra_open_dates=open_dates,
            source=source_url,
        )
        db.save_settings({
            "trading_calendar_closed_dates_json": json.dumps(
                sorted(item.isoformat() for item in calendar.extra_closed_dates), ensure_ascii=False,
            ),
            "trading_calendar_open_dates_json": json.dumps(
                sorted(item.isoformat() for item in calendar.extra_open_dates), ensure_ascii=False,
            ),
            "trading_calendar_source_url": source_url,
            "trading_calendar_updated_at": (retrieved_at or local_now()).isoformat(timespec="seconds"),
        })
        return calendar

    def is_trading_day(self, value: date) -> bool:
        if value in self.extra_open_dates:
            return True
        if value in self.extra_closed_dates or value in BUILTIN_CLOSED_DATES:
            return False
        return value.weekday() < 5

    def next_trading_day(self, value: date, *, include_current: bool = False) -> date:
        current = value if include_current else value + timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(current):
                return current
            current += timedelta(days=1)
        raise TradingCalendarError("无法在一年范围内找到下一个交易日，请更新交易日历。")

    def previous_trading_day(self, value: date, *, include_current: bool = False) -> date:
        current = value if include_current else value - timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(current):
                return current
            current -= timedelta(days=1)
        raise TradingCalendarError("无法在一年范围内找到上一个交易日，请更新交易日历。")

    def latest_completed_trading_day(
        self,
        as_of: datetime | None = None,
        *,
        market_close: dt_time | None = None,
    ) -> date:
        """Return the trading day whose official close should already exist.

        Before 15:00 on an open day the latest complete daily bar is the prior
        trading day.  After the close it is the current day.  Weekends and
        exchange holidays always roll back to the previous open day.
        """
        moment = as_of or local_now()
        close = market_close or self.DEFAULT_CUTOFF
        if self.is_trading_day(moment.date()) and moment.time() >= close:
            return moment.date()
        return self.previous_trading_day(moment.date(), include_current=False)

    def advance_trading_days(self, value: date, count: int) -> date:
        if count < 0:
            raise TradingCalendarError("交易日延迟不能为负数。")
        current = value
        for _ in range(count):
            current = self.next_trading_day(current)
        return current

    def expected_pricing_date(self, scheduled_date: date) -> date:
        return self.next_trading_day(scheduled_date, include_current=True)

    def first_available_pricing_date(
        self,
        scheduled_date: date,
        available_nav_dates: Iterable[str | date],
        *,
        not_after: date | None = None,
    ) -> date | None:
        expected = self.expected_pricing_date(scheduled_date)
        candidates = sorted(_parse_dates(available_nav_dates))
        return next(
            (
                item for item in candidates
                if item >= expected and (not_after is None or item <= not_after)
            ),
            None,
        )

    def estimated_confirm_date(self, pricing_date: date, fund_kind: str) -> date:
        kind = "QDII" if str(fund_kind).upper() == "QDII" else "普通场外"
        return self.advance_trading_days(pricing_date, self.CONFIRMATION_LAGS[kind])

    def resolve_order(
        self,
        submitted_at: datetime,
        *,
        fund_kind: str = "普通场外",
        cutoff: dt_time | None = None,
    ) -> FundOrderSchedule:
        cutoff = cutoff or self.DEFAULT_CUTOFF
        before_cutoff = submitted_at.time() < cutoff
        same_day = self.is_trading_day(submitted_at.date()) and before_cutoff
        pricing_date = (
            submitted_at.date()
            if same_day
            else self.next_trading_day(submitted_at.date(), include_current=False)
        )
        kind = "QDII" if str(fund_kind).upper() == "QDII" else "普通场外"
        lag = self.CONFIRMATION_LAGS[kind]
        return FundOrderSchedule(
            submitted_at=submitted_at,
            pricing_date=pricing_date,
            estimated_confirm_date=self.advance_trading_days(pricing_date, lag),
            fund_kind=kind,
            confirmation_lag_trading_days=lag,
            rolled_after_cutoff=not same_day,
            calendar_source=self.source,
        )

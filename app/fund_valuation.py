from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable

from .config import local_now, local_today
from .recurring import EastmoneyFundNavProvider


_VALUATION_LOCK = threading.Lock()


class FundValuationError(RuntimeError):
    pass


class FundValuationService:
    """Refresh published fund NAVs and value the user's current holdings.

    Open-ended funds do not have a reliable official intraday NAV.  This
    service therefore records the button-click time separately from each
    fund's published NAV date and never labels an old NAV as real time.
    """

    def __init__(self, db, provider: EastmoneyFundNavProvider | None = None):
        self.db = db
        self.provider = provider or EastmoneyFundNavProvider(
            timeout_seconds=int(float(db.setting("market_timeout_seconds", "20")))
        )

    def sync_recent(
        self,
        calendar_days: int | None = None,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        if not _VALUATION_LOCK.acquire(blocking=False):
            raise FundValuationError("另一项基金净值更新正在运行，请等待完成后再试。")
        try:
            return self._sync_recent_unlocked(
                calendar_days, stop_requested=stop_requested, progress=progress,
            )
        finally:
            _VALUATION_LOCK.release()

    def sync_holding_period(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh every NAV needed by the currently open holding segment."""
        if not _VALUATION_LOCK.acquire(blocking=False):
            raise FundValuationError("另一项基金净值更新正在运行，请等待完成后再试。")
        try:
            return self._sync_recent_unlocked(
                None, holding_period=True,
                stop_requested=stop_requested, progress=progress,
            )
        finally:
            _VALUATION_LOCK.release()

    def _sync_recent_unlocked(
        self,
        calendar_days: int | None,
        *,
        holding_period: bool = False,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        lookback = calendar_days
        if lookback is None:
            lookback = int(float(self.db.setting("fund_nav_lookback_days", "14")))
        lookback = max(5, min(90, int(lookback)))
        end = local_today()
        default_start = end - timedelta(days=lookback)
        holdings = self.db.rows(
            """SELECT h.id,h.fund_code,h.units,h.cost,h.confirmed_date,f.fund_name
               FROM holdings h JOIN funds f USING(fund_code)
               WHERE h.active=1 ORDER BY h.fund_code,h.id"""
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for holding in holdings:
            grouped[holding["fund_code"]].append(holding)

        now = local_now().isoformat(timespec="seconds")
        errors: list[str] = []
        fetched_funds = 0
        saved_navs = 0
        stopped = False
        with self.db.connect() as conn:
            for position, (code, items) in enumerate(grouped.items(), 1):
                if stop_requested and stop_requested():
                    stopped = True
                    break
                start = default_start
                if holding_period:
                    lot = self.db.row(
                        """SELECT MIN(COALESCE(
                                   NULLIF(t.nav_date,''),NULLIF(t.trade_date,''),
                                   NULLIF(o.pricing_date,''),NULLIF(o.trade_date,''),l.confirmed_date
                               )) AS segment_start
                           FROM cost_lots l
                           LEFT JOIN trades t ON t.id=l.trade_id
                           LEFT JOIN recurring_occurrences o ON o.id=l.occurrence_id
                          WHERE l.fund_code=? AND l.remaining_units>0""",
                        (code,),
                    )
                    raw_start = (lot or {}).get("segment_start") or min(str(item["confirmed_date"]) for item in items)
                    try:
                        start = date.fromisoformat(str(raw_start))
                    except ValueError:
                        start = default_start
                try:
                    navs, source_url = self.provider.fetch(code, start.isoformat(), end.isoformat())
                except Exception as exc:
                    errors.append(f"{code} {items[0]['fund_name']}：{exc}")
                    if progress:
                        progress(position, len(grouped), f"{code} 下载失败")
                    continue
                if not navs:
                    errors.append(f"{code} {items[0]['fund_name']}：净值源没有返回记录")
                    if progress:
                        progress(position, len(grouped), f"{code} 暂无净值")
                    continue
                fetched_funds += 1
                for nav_date, nav in sorted(navs.items()):
                    conn.execute(
                        """INSERT INTO fund_nav_history(
                               fund_code,nav_date,nav,source,source_url,fetched_at
                           ) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(fund_code,nav_date) DO UPDATE SET
                               nav=excluded.nav,source=excluded.source,
                               source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                        (code, nav_date, float(nav), self.provider.name, source_url, now),
                    )
                    saved_navs += 1
                latest_date = max(navs)
                latest_nav = float(navs[latest_date])
                conn.execute(
                    "UPDATE holdings SET latest_nav=?,nav_date=? WHERE fund_code=? AND active=1",
                    (latest_nav, latest_date, code),
                )
                if progress:
                    progress(position, len(grouped), f"{code} {items[0]['fund_name']}")

        summary = self.current_summary()
        if not stopped or fetched_funds:
            self.db.execute(
                """INSERT INTO fund_valuation_snapshots(
                       captured_at,official_value,total_cost,official_pnl,nav_dates_json,notes
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    now, summary["official_value"], summary["total_cost"], summary["official_pnl"],
                    json.dumps(summary["nav_dates"], ensure_ascii=False, sort_keys=True),
                    "按最新已公布单位净值核算；开放式基金不是盘中实时成交价。",
                ),
            )
            # A drawdown needs portfolio-equity observations, not just realized
            # exits.  Record the active strategy after every successful valuation
            # pass; non-active debt/QDII/cash holdings remain outside this metric.
            from .ledger import PortfolioLedgerService
            PortfolioLedgerService(self.db).record_equity_snapshot("正式净值更新")
        settings = {
            "fund_nav_last_sync_attempt_at": now,
            "fund_nav_last_sync_attempt_status": "已停止" if stopped else "已完成",
            "fund_nav_last_sync_error": "\n".join(errors)[:3000],
        }
        if not stopped:
            settings["fund_nav_last_sync_at"] = now
        self.db.save_settings(settings)
        return {
            "funds_total": len(grouped),
            "funds_updated": fetched_funds,
            "nav_rows_saved": saved_navs,
            "stopped": stopped,
            "errors": errors,
            "summary": summary,
            "daily_rows": self.daily_rows(None if holding_period else lookback),
        }

    def current_summary(self) -> dict[str, Any]:
        rows = self.db.rows(
            """SELECT h.fund_code,f.fund_name,SUM(h.units) AS units,SUM(h.cost) AS cost,
                      h.latest_nav,h.nav_date
               FROM holdings h JOIN funds f USING(fund_code)
               WHERE h.active=1
               GROUP BY h.fund_code,f.fund_name,h.latest_nav,h.nav_date
               ORDER BY h.fund_code"""
        )
        official_value = sum(float(row["units"]) * float(row["latest_nav"]) for row in rows)
        total_cost = sum(float(row["cost"]) for row in rows)
        nav_dates = {row["fund_code"]: row["nav_date"] or "未更新" for row in rows}
        valid_dates = sorted(value for value in nav_dates.values() if value != "未更新")
        return {
            "captured_at": local_now().isoformat(timespec="seconds"),
            "official_value": round(official_value, 6),
            "total_cost": round(total_cost, 6),
            "official_pnl": round(official_value - total_cost, 6),
            "nav_dates": nav_dates,
            "oldest_nav_date": valid_dates[0] if valid_dates else "",
            "latest_nav_date": valid_dates[-1] if valid_dates else "",
        }

    def daily_rows(self, calendar_days: int | None = None) -> list[dict[str, Any]]:
        global_start = ""
        if calendar_days is not None:
            calendar_days = max(5, min(3650, int(calendar_days)))
            global_start = (local_today() - timedelta(days=calendar_days)).isoformat()
        holdings = self.db.rows(
            """SELECT h.fund_code,f.fund_name,SUM(h.units) AS units,SUM(h.cost) AS cost,
                      MIN(h.confirmed_date) AS confirmed_date
               FROM holdings h JOIN funds f USING(fund_code)
               WHERE h.active=1 GROUP BY h.fund_code,f.fund_name ORDER BY h.fund_code"""
        )
        result: list[dict[str, Any]] = []
        for holding in holdings:
            lots = self.db.rows(
                """SELECT l.confirmed_date,
                          COALESCE(NULLIF(t.nav_date,''),NULLIF(t.trade_date,''),
                                   NULLIF(o.pricing_date,''),NULLIF(o.trade_date,''),l.confirmed_date)
                              AS pricing_date,
                          l.units AS original_units,l.remaining_units AS units,l.gross_amount
                     FROM cost_lots l
                     LEFT JOIN trades t ON t.id=l.trade_id
                     LEFT JOIN recurring_occurrences o ON o.id=l.occurrence_id
                    WHERE l.fund_code=? AND l.remaining_units>0
                    ORDER BY pricing_date,l.id""",
                (holding["fund_code"],),
            )
            lot_units = sum(float(row["units"]) for row in lots)
            lot_cost = sum(
                float(row["gross_amount"]) * float(row["units"])
                / max(float(row["original_units"]), 1e-12)
                for row in lots
            )
            use_lots = bool(lots) and abs(lot_units - float(holding["units"])) <= 0.011 \
                and abs(lot_cost - float(holding["cost"])) <= 0.011
            segment_start = min((str(row["pricing_date"]) for row in lots), default=str(holding["confirmed_date"]))
            start = max(global_start, segment_start) if global_start else segment_start
            history = self.db.rows(
                """SELECT nav_date,nav,source,fetched_at FROM fund_nav_history
                   WHERE fund_code=? AND nav_date>=? ORDER BY nav_date""",
                (holding["fund_code"], start),
            )
            previous_nav: float | None = None
            for item in history:
                if item["nav_date"] < segment_start:
                    continue
                nav = float(item["nav"])
                daily_change = None if previous_nav is None else nav - previous_nav
                daily_pct = None if previous_nav in (None, 0) else daily_change / previous_nav * 100
                if use_lots:
                    active_lots = [row for row in lots if row["pricing_date"] <= item["nav_date"]]
                    units = sum(float(row["units"]) for row in active_lots)
                    cost = sum(
                        float(row["gross_amount"]) * float(row["units"])
                        / max(float(row["original_units"]), 1e-12)
                        for row in active_lots
                    )
                    units_before_today = sum(
                        float(row["units"]) for row in lots if row["pricing_date"] < item["nav_date"]
                    )
                    basis = "逐笔定投累计"
                else:
                    units = float(holding["units"])
                    cost = float(holding["cost"])
                    units_before_today = units
                    basis = "汇总持仓回看"
                market_value = units * nav
                daily_pnl = None if daily_change is None else units_before_today * daily_change
                result.append({
                    "nav_date": item["nav_date"],
                    "fund_code": holding["fund_code"],
                    "fund_name": holding["fund_name"],
                    "nav": nav,
                    "daily_change_pct": daily_pct,
                    "daily_pnl_current_units": daily_pnl,
                    "market_value_current_units": market_value,
                    "pnl_vs_current_cost": market_value - cost,
                    "units_at_date": units,
                    "cost_at_date": cost,
                    "valuation_basis": basis,
                    "source": item["source"],
                    "fetched_at": item["fetched_at"],
                })
                previous_nav = nav
        return sorted(result, key=lambda row: (row["nav_date"], row["fund_code"]), reverse=True)

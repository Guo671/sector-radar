from __future__ import annotations

import html
import json
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from .config import local_now, local_today
from .ledger import LedgerError, PortfolioLedgerService
from .market_data import windows_curl_path
from .trading_calendar import ChinaTradingCalendar, infer_fund_kind


class RecurringInvestmentError(RuntimeError):
    """A recurring-investment operation could not be completed safely."""


_NAV_SYNC_LOCK = threading.Lock()
MAX_NAV_RESPONSE_BYTES = 5 * 1024 * 1024


def _money(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _nav(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _units(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_subscription(amount: float, nav: float, fee_pct: float) -> tuple[float, float]:
    """Return (fee, units) using the standard external-deduction subscription formula."""
    gross = _money(amount)
    price = _nav(nav)
    rate = Decimal(str(fee_pct)) / Decimal("100")
    if gross <= 0 or price <= 0 or rate < 0:
        raise RecurringInvestmentError("金额、净值必须大于0，买入费率不能为负数。")
    net = gross / (Decimal("1") + rate)
    fee = _money(gross - net)
    shares = _units((gross - fee) / price)
    if shares <= 0:
        raise RecurringInvestmentError("按当前金额、费用和净值计算出的确认份额为0。")
    return float(fee), float(shares)


def parse_eastmoney_nav_payload(raw: bytes, fund_code: str = "") -> dict[str, float]:
    """Parse Eastmoney's current JSON or legacy F10 HTML without executing JavaScript."""
    if len(raw) > MAX_NAV_RESPONSE_BYTES:
        raise RecurringInvestmentError(f"历史净值响应超过5MB安全上限（{fund_code or '未知基金'}）。")
    text = raw.decode("utf-8", errors="replace")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RecurringInvestmentError(
                f"历史净值JSON无法解析（{fund_code or '未知基金'}）。"
            ) from exc
        if not isinstance(payload, dict) or payload.get("ErrCode") not in (None, 0):
            raise RecurringInvestmentError(
                f"历史净值源返回错误（{fund_code or '未知基金'}）：{payload.get('ErrMsg') or payload.get('ErrCode')}"
            )
        data = payload.get("Data")
        rows = data.get("LSJZList") if isinstance(data, dict) else None
        result: dict[str, float] = {}
        for row in rows or []:
            try:
                parsed_date = date.fromisoformat(str(row.get("FSRQ") or "")).isoformat()
                value = float(row.get("DWJZ"))
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                result[parsed_date] = value
        # A successful response with an empty list means the official NAV has
        # not been published yet.  It is not a parser/network failure; callers
        # must keep the occurrence pending instead of alarming the user.
        if isinstance(rows, list):
            return result
    text = html.unescape(text.replace(r"\'", "'").replace(r'\"', '"').replace(r"\/", "/"))
    result: dict[str, float] = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        cleaned = [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells]
        if len(cleaned) < 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned[0]):
            continue
        try:
            parsed_date = date.fromisoformat(cleaned[0]).isoformat()
            value = float(cleaned[1].replace(",", ""))
        except ValueError:
            continue
        if value > 0:
            result[parsed_date] = value
    if not result:
        preview = re.sub(r"\s+", " ", text)[:160]
        raise RecurringInvestmentError(
            f"历史净值响应中没有可解析记录（{fund_code or '未知基金'}）：{preview or '空响应'}"
        )
    return result


class EastmoneyFundNavProvider:
    name = "eastmoney_f10_public"
    endpoint = "https://api.fund.eastmoney.com/f10/lsjz"

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
    ):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._requester = requester

    def fetch(self, fund_code: str, start_date: str, end_date: str) -> tuple[dict[str, float], str]:
        if not re.fullmatch(r"\d{6}", fund_code):
            raise RecurringInvestmentError("基金代码必须是6位数字。")
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            raise RecurringInvestmentError("历史净值结束日期不能早于开始日期。")
        if (end - start).days > 3660:
            raise RecurringInvestmentError("单次历史净值查询跨度不能超过10年。")
        def make_request(page_index: int) -> tuple[urllib.request.Request, str]:
            query = urllib.parse.urlencode({
                "fundCode": fund_code, "pageIndex": str(page_index), "pageSize": "200",
                "startDate": start_date, "endDate": end_date,
            })
            url = f"{self.endpoint}?{query}"
            return urllib.request.Request(url, headers={
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
            }), url

        request, first_url = make_request(1)
        raw = self._download(request, fund_code)
        values = parse_eastmoney_nav_payload(raw, fund_code)
        # The public endpoint currently caps every response at 20 rows even when
        # pageSize=200 is requested.  Without pagination every tracking-error
        # calculation was silently limited to 19 returns and displayed as
        # "样本不足".  Read the advertised TotalCount/PageSize and fetch all pages.
        try:
            envelope = json.loads(raw.decode("utf-8"))
            total = max(0, int(envelope.get("TotalCount") or len(values)))
            actual_page_size = max(1, int(envelope.get("PageSize") or max(1, len(values))))
            page_count = min(200, (total + actual_page_size - 1) // actual_page_size)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
            page_count = 1
        for page_index in range(2, page_count + 1):
            page_request, _ = make_request(page_index)
            page_values = parse_eastmoney_nav_payload(
                self._download(page_request, fund_code), fund_code,
            )
            if not page_values:
                break
            values.update(page_values)
        return values, first_url

    def _download(self, request: urllib.request.Request, fund_code: str) -> bytes:
        if self._requester:
            return self._requester(request, self.timeout_seconds)
        failures: list[str] = []
        for attempt in range(2):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds, context=ssl.create_default_context()
                ) as response:
                    return response.read(MAX_NAV_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise RecurringInvestmentError(f"历史净值源HTTP {exc.code}（{fund_code}）。") from exc
                failures.append(f"历史净值 Python HTTPS 第{attempt + 1}次：HTTP {exc.code}")
            except (urllib.error.URLError, ssl.SSLError, ConnectionError, OSError, TimeoutError) as exc:
                failures.append(f"历史净值 Python HTTPS 第{attempt + 1}次：{getattr(exc, 'reason', exc)}")
            if attempt == 0:
                time.sleep(0.35)
        curl = windows_curl_path()
        if curl:
            command = [
                curl, "--fail", "--silent", "--show-error", "--location", "--http1.1", "--ipv4",
                "--tlsv1.2", "--tls-max", "1.2",
                "--proto", "=https", "--proto-redir", "=https", "--max-filesize", str(MAX_NAV_RESPONSE_BYTES),
                "--connect-timeout", str(min(10, self.timeout_seconds)),
                "--max-time", str(self.timeout_seconds),
                "--header", "Accept-Encoding: identity",
                "--header", f"Referer: https://fundf10.eastmoney.com/jjjz_{fund_code}.html",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
                request.full_url,
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, timeout=self.timeout_seconds + 5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                )
                if completed.returncode == 0 and completed.stdout:
                    return completed.stdout
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
                failures.append(
                    f"历史净值 Windows系统curl（{curl}）退出码{completed.returncode}：{detail or '无错误正文'}"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"历史净值 Windows系统curl（{curl}）：{exc}")
        else:
            failures.append("历史净值 Windows系统curl：未找到")
        raise RecurringInvestmentError(
            f"无法连接历史净值源（{fund_code}）：{'；'.join(dict.fromkeys(failures)) or '未知网络错误'}"
        )


class RecurringInvestmentService:
    def __init__(
        self,
        db,
        provider: EastmoneyFundNavProvider | None = None,
        calendar: ChinaTradingCalendar | None = None,
    ):
        self.db = db
        self.provider = provider or EastmoneyFundNavProvider(
            timeout_seconds=int(float(db.setting("market_timeout_seconds", "20")))
        )
        self.calendar = calendar or ChinaTradingCalendar.from_db(db)

    def generate_due(self, through: date | None = None) -> int:
        through = through or local_today()
        created = 0
        now = local_now().isoformat(timespec="seconds")
        assume_deducted = str(
            self.db.setting("recurring_assume_all_deducted", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        plans = self.db.rows("SELECT * FROM recurring_plans WHERE enabled=1 ORDER BY id")
        with self.db.connect() as conn:
            for plan in plans:
                start = date.fromisoformat(plan["start_date"])
                end = date.fromisoformat(plan["end_date"]) if plan["end_date"] else through
                end = min(end, through)
                if end < start:
                    continue
                weekdays = {int(value) for value in str(plan["weekdays"]).split(",") if value.strip().isdigit()}
                if not weekdays:
                    weekdays = {0, 1, 2, 3, 4}
                current = start
                while current <= end:
                    frequency = str(plan["frequency"] or "交易日")
                    if frequency == "每日":
                        eligible = True
                    elif frequency == "交易日":
                        eligible = current.weekday() in weekdays and self.calendar.is_trading_day(current)
                    else:  # 每周：保留计划星期，休市时由净值校准顺延到真实定价日
                        eligible = current.weekday() in weekdays
                    if eligible:
                        cursor = conn.execute(
                            """INSERT OR IGNORE INTO recurring_occurrences(
                                   plan_id,fund_code,scheduled_date,trade_date,amount,status,notes,created_at,updated_at,
                                   deduction_confirmed,deduction_confirmed_at,deduction_confirmation_source
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                plan["id"], plan["fund_code"], current.isoformat(), current.isoformat(), plan["amount"],
                                "待净值" if assume_deducted else "待扣款核对",
                                (
                                    "按用户的全局定投规则视为已扣款；等待正式净值与合法确认日。"
                                    if assume_deducted else "计划到期；尚不能证明支付宝已实际扣款。"
                                ),
                                now, now, 1 if assume_deducted else 0,
                                now if assume_deducted else "",
                                "用户设置：全部定投均已扣款" if assume_deducted else "",
                            ),
                        )
                        created += max(0, cursor.rowcount)
                    current += timedelta(days=1)
            if assume_deducted:
                # The owner explicitly configured every generated scheduled
                # investment as an actual platform deduction.  Adopt older
                # unfinished occurrences too, but never revive a skipped row.
                conn.execute(
                    """UPDATE recurring_occurrences
                          SET deduction_confirmed=1,
                              deduction_confirmed_at=CASE
                                  WHEN COALESCE(deduction_confirmed_at,'')='' THEN ?
                                  ELSE deduction_confirmed_at END,
                              deduction_confirmation_source=CASE
                                  WHEN COALESCE(deduction_confirmation_source,'')='' THEN '用户设置：全部定投均已扣款'
                                  ELSE deduction_confirmation_source END,
                              status=CASE WHEN status='待扣款核对' THEN '待净值' ELSE status END,
                              updated_at=?
                        WHERE scheduled_date<=? AND status NOT IN ('已跳过','已入账')
                          AND COALESCE(deduction_confirmed,0)=0""",
                    (now, now, through.isoformat()),
                )
        return created

    def sync_pending_navs(
        self,
        occurrence_ids: list[int] | None = None,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int]:
        if not _NAV_SYNC_LOCK.acquire(blocking=False):
            raise RecurringInvestmentError("另一项定投净值校准正在运行，请等待完成后再试。")
        try:
            return self._sync_pending_navs_unlocked(
                occurrence_ids, stop_requested=stop_requested, progress=progress,
            )
        finally:
            _NAV_SYNC_LOCK.release()

    def _sync_pending_navs_unlocked(
        self,
        occurrence_ids: list[int] | None = None,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int]:
        where = "o.status IN ('待扣款核对','待净值','校准失败')"
        params: list[object] = []
        if occurrence_ids:
            marks = ",".join("?" for _ in occurrence_ids)
            where += f" AND o.id IN ({marks})"
            params.extend(occurrence_ids)
        rows = self.db.rows(
            f"""SELECT o.*,p.purchase_fee_pct,f.purchase_fee_pct AS fund_fee_pct,f.fund_name,
                       f.index_name AS fund_index_name,f.notes AS fund_notes
                FROM recurring_occurrences o
                JOIN recurring_plans p ON p.id=o.plan_id
                JOIN funds f ON f.fund_code=o.fund_code
                WHERE {where} ORDER BY p.fund_code,o.trade_date""", params,
        )
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["fund_code"], []).append(row)
        counts = {"checked": 0, "ready": 0, "missing": 0, "failed": 0, "shifted": 0, "stopped": False}
        now = local_now().isoformat(timespec="seconds")
        errors: list[str] = []
        for position, (code, items) in enumerate(grouped.items(), 1):
            if stop_requested and stop_requested():
                counts["stopped"] = True
                break
            expected_dates = {
                int(item["id"]): self.calendar.expected_pricing_date(date.fromisoformat(item["trade_date"]))
                for item in items
            }
            start = min(expected_dates.values()).isoformat()
            # A previously missing holiday/suspension NAV can legitimately appear
            # on a later pricing day.  Fetch through today so the occurrence can
            # move forward instead of remaining stuck on its original date forever.
            end = max(max(expected_dates.values()), local_today()).isoformat()
            cached_rows = self.db.rows(
                """SELECT nav_date,nav,source,source_url FROM fund_nav_history
                     WHERE fund_code=? AND nav_date BETWEEN ? AND ? ORDER BY nav_date""",
                (code, start, end),
            )
            cached_by_date = {str(row["nav_date"]): row for row in cached_rows}
            navs = {day: float(row["nav"]) for day, row in cached_by_date.items()}
            fetched_navs: dict[str, float] = {}
            source_url = str(cached_rows[-1].get("source_url") or "") if cached_rows else ""
            try:
                fetched_navs, fetched_url = self.provider.fetch(code, start, end)
                navs.update(fetched_navs)
                if fetched_url:
                    source_url = fetched_url
            except Exception as exc:
                if not navs:
                    errors.append(f"{code}：{exc}")
                    for item in items:
                        self.db.execute(
                            "UPDATE recurring_occurrences SET status='校准失败',notes=?,updated_at=? WHERE id=?",
                            (str(exc)[:500], now, item["id"]),
                        )
                        counts["failed"] += 1
                    if progress:
                        progress(position, len(grouped), f"{code} 下载失败")
                    continue
                errors.append(f"{code}：本次联网失败，已复用本机保存的正式净值（{exc}）")
            for item in items:
                counts["checked"] += 1
                scheduled = date.fromisoformat(item["trade_date"])
                actual_pricing = self.calendar.first_available_pricing_date(
                    scheduled, navs.keys(), not_after=local_today(),
                )
                pricing_date = actual_pricing.isoformat() if actual_pricing else ""
                value = navs.get(pricing_date) if pricing_date else None
                if value is None:
                    self.db.execute(
                        """UPDATE recurring_occurrences SET status='待净值',source=?,source_url=?,fetched_at=?,
                           notes=?,updated_at=? WHERE id=?""",
                        (
                            self.provider.name, source_url, now,
                            "计划日之后尚无可用正式净值；软件会在下次校准时自动寻找首个真实定价日。", now, item["id"],
                        ),
                    )
                    counts["missing"] += 1
                    continue
                fee_pct = item["purchase_fee_pct"]
                if fee_pct is None:
                    fee_pct = item["fund_fee_pct"]
                fee, units = calculate_subscription(float(item["amount"]), value, float(fee_pct or 0))
                fund_kind = infer_fund_kind(
                    item.get("fund_name") or "", item.get("fund_index_name") or "",
                    item.get("fund_notes") or "",
                )
                confirm_date = self.calendar.estimated_confirm_date(actual_pricing, fund_kind).isoformat()
                expected = expected_dates[int(item["id"])]
                shifted = actual_pricing != expected
                cached = cached_by_date.get(pricing_date, {})
                value_source = (
                    self.provider.name if pricing_date in fetched_navs
                    else str(cached.get("source") or self.provider.name)
                )
                value_url = (
                    source_url if pricing_date in fetched_navs
                    else str(cached.get("source_url") or source_url)
                )
                self.db.execute(
                    """INSERT INTO fund_nav_history(fund_code,nav_date,nav,source,source_url,fetched_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(fund_code,nav_date) DO UPDATE SET
                           nav=excluded.nav,source=excluded.source,
                           source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                    (code, pricing_date, value, value_source, value_url, now),
                )
                self.db.execute(
                    """UPDATE recurring_occurrences SET pricing_date=?,confirm_date=?,fee=?,nav=?,units=?,
                       status='可确认入账',source=?,source_url=?,fetched_at=?,notes=?,updated_at=? WHERE id=?""",
                    (
                        pricing_date, confirm_date, fee, value, units, value_source, value_url, now,
                        (
                            f"已用公开历史净值校准；{'计划日无净值，已顺延到首个真实定价日；' if shifted else ''}"
                            f"{fund_kind}默认按T+{2 if fund_kind == 'QDII' else 1}交易日估算确认日，"
                            "仍须以支付宝实际扣款、确认份额、确认日期和手续费为准。"
                        ), now, item["id"],
                    ),
                )
                counts["ready"] += 1
                if shifted:
                    counts["shifted"] += 1
            if progress:
                progress(position, len(grouped), f"{code} 已检查 {len(items)} 笔")
        settings = {
            "recurring_last_sync_attempt_at": now,
            "recurring_last_sync_attempt_status": "已停止" if counts["stopped"] else "已完成",
            "recurring_last_sync_error": "\n".join(errors)[:2000],
        }
        if not counts["stopped"]:
            settings["recurring_last_sync_at"] = now
        self.db.save_settings(settings)
        counts["auto_posted"] = self.auto_confirm_ready()
        return counts

    def auto_confirm_ready(self, through: date | None = None) -> int:
        """Post due recurring rows when the owner has disabled deduction review.

        Formal NAV and a non-future legal confirmation date remain mandatory;
        the preference only removes the repetitive "did Alipay deduct" click.
        """
        enabled = str(
            self.db.setting("recurring_assume_all_deducted", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return 0
        through = through or local_today()
        rows = self.db.rows(
            """SELECT id FROM recurring_occurrences
                 WHERE status='可确认入账' AND COALESCE(deduction_confirmed,0)=1
                   AND COALESCE(confirm_date,pricing_date,'')<>''
                   AND COALESCE(confirm_date,pricing_date)<=?
                 ORDER BY scheduled_date,id""",
            (through.isoformat(),),
        )
        posted = 0
        for row in rows:
            posted += self.confirm([int(row["id"])])
        return posted

    def confirm(self, occurrence_ids: list[int]) -> int:
        if not occurrence_ids:
            return 0
        now = local_now().isoformat(timespec="seconds")
        confirmed = 0
        with self.db.connect() as conn:
            ledger = PortfolioLedgerService(self.db)
            ledger._ensure_tables(conn)
            # Serialize the ready-check, trade creation, posting and occurrence
            # status change across processes.  A second click waits, then sees
            # ``已入账`` instead of creating a duplicate trade/cost lot.
            conn.execute("BEGIN IMMEDIATE")
            for occurrence_id in occurrence_ids:
                row = conn.execute(
                    """SELECT o.*,f.fund_name
                       FROM recurring_occurrences o
                       JOIN recurring_plans p ON p.id=o.plan_id
                       JOIN funds f ON f.fund_code=o.fund_code
                       WHERE o.id=?""", (occurrence_id,),
                ).fetchone()
                if not row:
                    raise RecurringInvestmentError(f"定投流水不存在：{occurrence_id}")
                if row["status"] == "已入账":
                    continue
                if row["status"] != "可确认入账" or row["nav"] <= 0 or row["units"] <= 0:
                    raise RecurringInvestmentError(f"{row['scheduled_date']}尚未达到可确认入账状态。")
                existing = conn.execute(
                    "SELECT * FROM trades WHERE recurring_occurrence_id=? ORDER BY id LIMIT 1",
                    (int(row["id"]),),
                ).fetchone()
                if existing:
                    active_posting = conn.execute(
                        "SELECT reversed_at FROM ledger_postings WHERE trade_id=?",
                        (int(existing["id"]),),
                    ).fetchone()
                    if active_posting and not str(active_posting["reversed_at"] or ""):
                        conn.execute(
                            """UPDATE recurring_occurrences
                                  SET status='已入账',trade_id=?,deduction_confirmed=1,
                                      deduction_confirmed_at=CASE
                                          WHEN COALESCE(deduction_confirmed_at,'')='' THEN ?
                                          ELSE deduction_confirmed_at END,
                                      deduction_confirmation_source=CASE
                                          WHEN COALESCE(deduction_confirmation_source,'')='' THEN '用户批量确认入账'
                                          ELSE deduction_confirmation_source END,
                                      updated_at=? WHERE id=?""",
                            (int(existing["id"]), now, now, int(row["id"])),
                        )
                        conn.execute(
                            """UPDATE decision_drafts
                                  SET status='已入账',trade_id=?,posted_at=?,updated_at=?,
                                      result_note='定投已按正式净值和确认日自动入账。'
                                WHERE recurring_occurrence_id=? AND status<>'已撤销'""",
                            (int(existing["id"]), now, now, int(row["id"])),
                        )
                        continue
                    if active_posting:
                        raise RecurringInvestmentError(
                            f"{row['scheduled_date']}对应交易已经撤销，不能再次自动入账；请新建人工更正记录。"
                        )
                    if conn.execute(
                        "SELECT 1 FROM cost_lots WHERE trade_id=?", (int(existing["id"]),)
                    ).fetchone():
                        raise RecurringInvestmentError(
                            f"{row['scheduled_date']}存在旧版成本批次但缺少统一过账，请先导出诊断。"
                        )
                    trade_id = int(existing["id"])
                else:
                    trade = conn.execute(
                        """INSERT INTO trades(
                               trade_date,fund_code,action,amount,units,nav,fee,rationale,ai_advice,result_pnl,
                               created_at,confirm_date,status,source,nav_date,source_url,
                               recurring_occurrence_id,counts_toward_active_capital,result_pnl_status
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["trade_date"], row["fund_code"], "定投", row["amount"], row["units"],
                            row["nav"], row["fee"], "定投计划自动生成；用户核对支付宝扣款后批量确认。",
                            "", None, now, row["confirm_date"] or row["pricing_date"], "待确认",
                            row["source"], row["pricing_date"], row["source_url"], row["id"], 0, "不适用",
                        ),
                    )
                    trade_id = int(trade.lastrowid)

                holding = conn.execute(
                    """SELECT counts_toward_active_capital FROM holdings
                         WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1""",
                    (row["fund_code"],),
                ).fetchone()
                # Scheduled investing (e.g. Nasdaq daily investing) is outside
                # the user's tactical 8,000-yuan pool unless this same fund is
                # already explicitly classified as a tactical holding.
                counts = 1 if holding and int(holding[0] or 0) else 0
                proposed = {
                    "trade_date": row["trade_date"],
                    "confirm_date": row["confirm_date"] or row["pricing_date"],
                    "fund_code": row["fund_code"],
                    "action": "定投",
                    "amount": float(row["amount"]),
                    "units": float(row["units"]),
                    "nav": float(row["nav"]),
                    "fee": float(row["fee"] or 0),
                    "nav_date": row["pricing_date"],
                    "status": "已确认",
                    "rationale": "定投计划自动生成；用户核对支付宝扣款后批量确认。",
                    "counts_toward_active_capital": counts,
                }
                try:
                    result = ledger.finalize_trade(
                        trade_id, proposed, conn=conn, tables_ready=True,
                    )
                except LedgerError as exc:
                    raise RecurringInvestmentError(str(exc)) from exc
                if not result.get("posted"):
                    raise RecurringInvestmentError(
                        f"{row['scheduled_date']}未完成统一账本过账：{result.get('reason','未知原因')}"
                    )
                conn.execute(
                    """UPDATE recurring_occurrences
                          SET status='已入账',trade_id=?,deduction_confirmed=1,
                              deduction_confirmed_at=CASE
                                  WHEN COALESCE(deduction_confirmed_at,'')='' THEN ?
                                  ELSE deduction_confirmed_at END,
                              deduction_confirmation_source=CASE
                                  WHEN COALESCE(deduction_confirmation_source,'')='' THEN '用户批量确认入账'
                                  ELSE deduction_confirmation_source END,
                              updated_at=? WHERE id=?""",
                    (trade_id, now, now, row["id"]),
                )
                conn.execute(
                    """UPDATE decision_drafts
                          SET status='已入账',trade_id=?,posted_at=?,updated_at=?,
                              result_note='定投已按正式净值和确认日自动入账。'
                        WHERE recurring_occurrence_id=? AND status<>'已撤销'""",
                    (trade_id, now, now, int(row["id"])),
                )
                confirmed += 1
        return confirmed

    def mark_skipped(self, occurrence_ids: list[int], note: str = "支付宝未扣款或用户确认跳过。") -> int:
        if not occurrence_ids:
            return 0
        marks = ",".join("?" for _ in occurrence_ids)
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            cursor = conn.execute(
                f"""UPDATE recurring_occurrences SET status='已跳过',notes=?,updated_at=?
                    WHERE status<>'已入账' AND id IN ({marks})""",
                [note, now, *occurrence_ids],
            )
            return max(0, cursor.rowcount)

    def auto_sync_due(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int] | None:
        self.generate_due()
        auto_posted = self.auto_confirm_ready()
        pending_rows = self.db.rows(
            """SELECT o.id FROM recurring_occurrences o JOIN recurring_plans p ON p.id=o.plan_id
               WHERE p.enabled=1 AND p.auto_sync=1
               AND o.status IN ('待扣款核对','待净值','校准失败') ORDER BY o.id"""
        )
        if not pending_rows:
            return {
                "checked": 0, "ready": 0, "missing": 0, "failed": 0,
                "shifted": 0, "stopped": False, "auto_posted": auto_posted,
            } if auto_posted else None
        last = self.db.setting("recurring_last_sync_at", "")
        hours = max(1.0, float(self.db.setting("recurring_auto_sync_hours", "6")))
        if last:
            try:
                if local_now() - datetime.fromisoformat(last) < timedelta(hours=hours):
                    return {
                        "checked": 0, "ready": 0, "missing": 0, "failed": 0,
                        "shifted": 0, "stopped": False, "auto_posted": auto_posted,
                    } if auto_posted else None
            except ValueError:
                pass
        result = self.sync_pending_navs(
            [row["id"] for row in pending_rows],
            stop_requested=stop_requested, progress=progress,
        )
        result["auto_posted"] = int(result.get("auto_posted", 0)) + auto_posted
        return result

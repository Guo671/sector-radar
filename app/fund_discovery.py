from __future__ import annotations

import html
import json
import math
import re
import ssl
import statistics
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any, Callable

from .config import local_now, local_today
from .market_data import CSIOfficialKlineProvider, windows_curl_path
from .recurring import EastmoneyFundNavProvider


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MIN_TRACKING_DAYS = 60
INDEX_NAME_OVERRIDES = {"000015": "上证红利指数"}


class FundDiscoveryError(RuntimeError):
    pass


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self.row = []
        elif tag.lower() in {"td", "th"} and self.row is not None:
            self._finish_cell()
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            self._finish_cell()
        elif tag == "tr" and self.row is not None:
            self._finish_cell()
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def _finish_cell(self):
        if self.row is not None and self.cell is not None:
            value = re.sub(r"\s+", " ", html.unescape("".join(self.cell))).strip()
            self.row.append(value)
            self.cell = None


def parse_fund_profile(page: str) -> dict[str, Any]:
    parser = _TableParser()
    parser.feed(page)
    fields: dict[str, str] = {}
    for row in parser.rows:
        for index in range(0, len(row) - 1, 2):
            key, value = row[index].strip(), row[index + 1].strip()
            if key and value and key not in fields:
                fields[key] = value

    def number(key: str) -> float | None:
        value = fields.get(key, "")
        if not value:
            value = next((item for label, item in fields.items() if key in label), "")
        match = re.search(r"(-?\d+(?:\.\d+)?)", value.replace(",", ""))
        return float(match.group(1)) if match else None

    size = number("净资产规模")
    date_match = re.search(r"(\d{4})年(\d{2})月(\d{2})日", fields.get("成立日期/规模", ""))
    inception = "-".join(date_match.groups()) if date_match else ""
    return {
        "full_name": fields.get("基金全称", ""),
        "target_index": fields.get("跟踪标的", ""),
        "benchmark": fields.get("业绩比较基准", ""),
        "inception_date": inception,
        "asset_size_billion": size,
        "management_fee_pct": number("管理费率"),
        "custody_fee_pct": number("托管费率"),
        "sales_fee_pct": number("销售服务费率"),
        "purchase_fee_pct": number("最高申购费率"),
        "max_redemption_fee_pct": number("最高赎回费率"),
    }


def parse_redemption_rate(page: str, holding_days: int) -> float | None:
    """Return the published redemption rate applicable to a holding period."""
    for minimum, maximum, rate in parse_redemption_tiers(page):
        if holding_days >= minimum and (maximum is None or holding_days <= maximum):
            return rate
    return None


def _redemption_term_range(value: str) -> tuple[int, int | None] | None:
    term = re.sub(r"\s+", "", html.unescape(value)).upper()
    term = term.replace("日", "天").replace("＜", "<").replace("＞", ">").replace("≦", "≤").replace("≧", "≥")
    term = term.replace("持有期限", "N").replace("持有时间", "N")
    def days(number: str, unit: str) -> int:
        value = int(number)
        return value * 365 if unit == "年" else value * 30 if unit == "月" else value

    bounds = [(days(number, unit), unit) for number, unit in re.findall(r"(\d+)(天|月|年)", term)]
    if not bounds:
        return None
    values = [item[0] for item in bounds]
    if "大于等于" in term and "小于" in term and len(values) >= 2:
        return values[0], values[1] - 1
    if re.search(r"(?:≤|<=)N(?:<|＜)", term) and len(values) >= 2:
        return values[0], values[1] - 1
    if "小于" in term or re.search(r"N(?:<|＜|≤)\d+", term):
        return 0, values[0] - (1 if "<" in term or "＜" in term or "小于" in term else 0)
    if "大于等于" in term or "以上" in term or re.search(r"N(?:≥|>=)\d+", term):
        return values[0], None
    if len(values) >= 2 and ("至" in term or "-" in term or "~" in term):
        return values[0], values[1]
    return None


def parse_redemption_tiers(page: str) -> list[tuple[int, int | None, float]]:
    """Extract the complete published redemption ladder from a public fee table."""
    parser = _TableParser()
    parser.feed(page)
    tiers: dict[int, tuple[int, int | None, float]] = {}
    for row in parser.rows:
        joined = " ".join(row)
        if "赎回费率" in joined or ("适用期限" in joined and "费率" in joined):
            continue
        if len(row) < 2:
            continue
        rate_match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", row[1])
        day_range = _redemption_term_range(row[0])
        if rate_match and day_range:
            minimum, maximum = day_range
            tiers[minimum] = (minimum, maximum, float(rate_match.group(1)))
    return [tiers[key] for key in sorted(tiers)]


def parse_small_purchase_rate(page: str) -> float | None:
    """Return the original public purchase rate for the smallest amount tier."""
    parser = _TableParser()
    parser.feed(page)
    headers = [
        index for index, row in enumerate(parser.rows)
        if len(row) >= 2 and "适用金额" in row[0] and "费率" in row[1]
    ]
    if not headers:
        return None
    # The last amount table before the redemption table is the purchase table;
    # earlier tables may describe the initial subscription period.
    start = headers[-1] + 1
    for row in parser.rows[start:]:
        if not row or "适用期限" in row[0] or "适用金额" in row[0]:
            break
        if len(row) < 2:
            continue
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", row[1])
        if match:
            return float(match.group(1))
    return None


def _normal(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value)).replace("收益率", "").replace("指数", "")


def _canonical_index_name(value: str) -> str:
    """Normalize official short names and fund-profile full index names."""
    result = _normal(value)
    for prefix in ("中证", "国证"):
        if result.startswith(prefix):
            result = result[len(prefix):]
            break
    for suffix in ("主题", "行业"):
        if result.endswith(suffix):
            result = result[:-len(suffix)]
    return result


def _share_class(name: str) -> str:
    match = re.search(r"(?:联接|指数|LOF|QDII|发起)?[（(]?[AERCI][）)]?$", name.upper())
    if match:
        letter = re.search(r"[AERCI]", match.group(0))
        return letter.group(0) if letter else ""
    return ""


@dataclass
class DiscoveryResult:
    run_id: int
    candidates: list[dict[str, Any]]
    warning: str
    queries: list[str]


class FundDiscoveryService:
    search_endpoint = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"

    def __init__(self, db, timeout_seconds: int | None = None):
        self.db = db
        self.timeout_seconds = max(8, int(timeout_seconds or float(db.setting("market_timeout_seconds", "20"))))

    def public_execution_snapshot(self, fund_code: str) -> dict[str, Any]:
        """Return a minimal, source-addressed public purchase-status snapshot."""
        code = str(fund_code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise FundDiscoveryError("当日申购复核需要6位基金代码。")
        matched: dict[str, Any] | None = None
        for item in self._search(code):
            base = item.get("FundBaseInfo") or {}
            if isinstance(base, str):
                try:
                    base = json.loads(base)
                except json.JSONDecodeError:
                    continue
            if not isinstance(base, dict):
                continue
            item_code = str(item.get("CODE") or base.get("FCODE") or "").strip()
            if item_code == code:
                matched = base
                break
        if matched is None:
            raise FundDiscoveryError(f"公开基金搜索没有返回基金{code}，原执行状态未改写。")
        return {
            "fund_code": code,
            "status": "公开渠道可申购" if str(matched.get("ISBUY") or "") == "1" else "不可申购",
            "source_url": f"https://fund.eastmoney.com/{code}.html",
        }

    def discover(
        self,
        universe_id: str,
        holding_days: int = 30,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DiscoveryResult:
        holding_days = max(7, min(1095, int(holding_days)))
        target = self.db.row(
            """SELECT u.index_code AS universe_id,u.index_name AS sector_name,
                      COALESCE(p.proxy_code,'') AS proxy_code,COALESCE(p.proxy_name,'') AS proxy_name,
                      COALESCE(p.mapping_kind,'') AS proxy_kind,COALESCE(p.security_id,'') AS security_id,
                      COALESCE(i.official_index_code,'') AS official_index_code,
                      COALESCE(i.official_index_name,'') AS official_index_name,
                      COALESCE(i.ant_index_code,'') AS ant_index_code,
                      COALESCE(i.ant_index_name,'') AS ant_index_name,
                      COALESCE(i.verification_status,'') AS investable_status,
                      COALESCE(i.mapping_relation,'') AS investable_relation
               FROM universe u LEFT JOIN proxy_mappings p ON p.universe_id=u.index_code
               LEFT JOIN investable_indices i ON i.id=(SELECT id FROM investable_indices x
                    WHERE x.universe_id=u.index_code AND x.active=1 ORDER BY x.display_order,x.id LIMIT 1)
               WHERE u.index_code=?""", (universe_id,),
        )
        if not target:
            raise FundDiscoveryError(f"找不到板块：{universe_id}")
        queries = []
        for value in (target["official_index_name"], target["ant_index_name"], INDEX_NAME_OVERRIDES.get(target["proxy_code"], ""), target["proxy_name"], target["sector_name"], target["official_index_code"]):
            value = str(value or "").strip()
            if value and not value.startswith("BK") and value not in queries:
                queries.append(value)
        if not queries:
            queries.append(target["sector_name"])

        discovered: dict[str, dict[str, Any]] = {}
        for query_position, query in enumerate(queries, 1):
            if stop_requested and stop_requested():
                raise FundDiscoveryError("用户已停止基金搜索；未保存不完整排名。")
            if progress:
                progress(query_position, max(1, len(queries)), f"搜索指数与基金：{query}")
            for item in self._search(query):
                base = item.get("FundBaseInfo") or {}
                code = str(item.get("CODE") or base.get("FCODE") or "")
                name = str(item.get("NAME") or base.get("SHORTNAME") or "")
                fund_type = str(base.get("FTYPE") or "")
                if not re.fullmatch(r"\d{6}", code) or "指数" not in fund_type:
                    continue
                if "ETF" in name.upper() and "联接" not in name and "LOF" not in name.upper():
                    continue
                discovered.setdefault(code, {
                    "fund_code": code, "fund_name": name, "company": str(base.get("JJGS") or ""),
                    "fund_type": fund_type, "buyable_status": "公开渠道可申购" if str(base.get("ISBUY") or "") == "1" else "公开渠道未显示可申购",
                    "min_purchase": base.get("MINSG"), "nav": base.get("DWJZ"), "nav_date": str(base.get("FSRQ") or ""),
                    "strategy_kind": "指数增强" if "增强" in name else "被动ETF联接" if "ETF" in name.upper() and "联接" in name else "被动指数",
                })
        if not discovered:
            raise FundDiscoveryError("公开基金搜索没有找到可比较的场外指数基金。")

        expected_name = _canonical_index_name(target.get("official_index_name") or INDEX_NAME_OVERRIDES.get(str(target.get("proxy_code") or ""), target.get("proxy_name") or target["sector_name"]))
        limited = sorted(
            discovered.values(),
            key=lambda row: (0 if expected_name and expected_name in _canonical_index_name(row["fund_name"]) else 1, row["fund_code"]),
        )[:40]
        pool = ThreadPoolExecutor(max_workers=6)
        futures = {pool.submit(self._enrich, row, target): row for row in limited}
        candidates = []
        try:
            for profile_position, future in enumerate(as_completed(futures), 1):
                if stop_requested and stop_requested():
                    for pending in futures:
                        pending.cancel()
                    raise FundDiscoveryError("用户已停止基金搜索；未保存不完整排名。")
                try:
                    row = future.result()
                except Exception:
                    row = None
                # This application is a short-holding A-share tool.  Never let an
                # A/E/I long-holding share class leak into the automatic shortlist.
                if row and row.get("share_class") == "C":
                    candidates.append(row)
                if progress:
                    progress(profile_position, max(1, len(futures)), f"核对基金资料：{futures[future]['fund_code']}")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if not candidates:
            raise FundDiscoveryError("找到了基金名称，但没有C类场外基金同时通过跟踪标的和资料完整性核对。")

        candidates.sort(key=lambda row: row["fund_code"])
        exact_candidates = [row for row in candidates if row["match_kind"] == "同一指数"][:20]
        similar_candidates = [row for row in candidates if row["match_kind"] != "同一指数"][:10]
        candidates = exact_candidates + similar_candidates
        if stop_requested and stop_requested():
            raise FundDiscoveryError("用户已停止基金搜索；未保存不完整排名。")
        if progress:
            progress(0, max(1, len(candidates)), "计算跟踪误差与持有期费率")
        self._attach_tracking_error(candidates, target)
        self._attach_horizon_fees(candidates, holding_days)
        for row in candidates:
            self._score(row, holding_days)
        candidates.sort(key=self._rank_key)
        candidates = candidates[:15]
        for rank, row in enumerate(candidates, 1):
            row["rank_no"] = rank
            if row["match_kind"] == "同一指数" and row.get("strategy_kind") != "指数增强" and rank == 1:
                row["recommendation"] = "被动同指数首选·下单前复核"
            elif row["match_kind"] == "同一指数":
                row["recommendation"] = "指数增强备选" if row.get("strategy_kind") == "指数增强" else "同指数备选"
            else:
                row["recommendation"] = "相似指数候补·不可当作同一标的"
        exact_count = sum(row["match_kind"] == "同一指数" for row in candidates)
        warning = "" if exact_count else "当前研究方向还没有已核验的正式可投指数；以下只能作为名称相近候补，不能视为同一标的。"
        now = local_now().isoformat(timespec="seconds")
        run_id = self.db.execute(
            """INSERT INTO fund_discovery_runs(
                   universe_id,sector_name,proxy_code,proxy_name,proxy_kind,holding_days,
                   query_terms_json,source_note,warning,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (universe_id, target["sector_name"], target["proxy_code"], target["proxy_name"], target["proxy_kind"],
             holding_days, json.dumps(queries, ensure_ascii=False),
             "东方财富公开基金搜索与基金档案用于候选、跟踪标的、费率、规模与净值整理；实际下单前仍须在所用平台复核。", warning, now),
        )
        with self.db.connect() as conn:
            for row in candidates:
                conn.execute(
                    """INSERT INTO fund_discovery_candidates(
                           run_id,rank_no,fund_code,fund_name,share_class,company,fund_type,target_index,strategy_kind,
                           match_kind,buyable_status,min_purchase,nav,nav_date,inception_date,asset_size_billion,
                           management_fee_pct,custody_fee_pct,sales_fee_pct,purchase_fee_pct,annual_cost_pct,
                           tracking_error_pct,tracking_days,redemption_fee_pct,horizon_cost_pct,recommendation,
                           score,reason,source_url,fee_url,fetched_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id,row["rank_no"],row["fund_code"],row["fund_name"],row["share_class"],row["company"],row["fund_type"],
                     row["target_index"],row.get("strategy_kind") or "被动指数",row["match_kind"],row["buyable_status"],row.get("min_purchase"),row.get("nav"),row["nav_date"],
                     row["inception_date"],row.get("asset_size_billion"),row.get("management_fee_pct"),row.get("custody_fee_pct"),
                     row.get("sales_fee_pct"),row.get("purchase_fee_pct"),row.get("annual_cost_pct"),row.get("tracking_error_pct"),
                     row.get("tracking_days",0),row.get("redemption_fee_pct"),row.get("horizon_cost_pct"),row["recommendation"],
                     row["score"],row["reason"],row["source_url"],row["fee_url"],now),
                )
        return DiscoveryResult(run_id, candidates, warning, queries)

    def _search(self, query: str) -> list[dict[str, Any]]:
        url = self.search_endpoint + "?" + urllib.parse.urlencode({"m": "1", "key": query})
        payload = json.loads(self._download(url).decode("utf-8", errors="replace"))
        if payload.get("ErrCode") != 0 or not isinstance(payload.get("Datas"), list):
            raise FundDiscoveryError(f"基金搜索返回异常：{query}")
        return payload["Datas"]

    def _enrich(self, row: dict[str, Any], target: dict[str, Any]) -> dict[str, Any] | None:
        code = row["fund_code"]
        source_url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
        page = self._download(source_url).decode("utf-8", errors="replace")
        profile = parse_fund_profile(page)
        target_index = profile.get("target_index") or ""
        if not target_index:
            return None
        expected = _canonical_index_name(target.get("official_index_name") or INDEX_NAME_OVERRIDES.get(str(target.get("proxy_code") or ""), target.get("proxy_name") or target.get("sector_name") or ""))
        actual = _canonical_index_name(target_index)
        proxy_code = str(target.get("proxy_code") or "")
        exact = bool(expected and actual and expected == actual and target.get("official_index_name"))
        sector_key = _normal(target.get("sector_name") or "")
        similar = exact or any(token in actual for token in self._keywords(sector_key))
        if not similar:
            return None
        row.update(profile)
        row["share_class"] = _share_class(row["fund_name"])
        row["match_kind"] = "同一指数" if exact else "相似可投指数"
        row["source_url"] = source_url
        row["fee_url"] = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
        fees = [profile.get(key) for key in ("management_fee_pct", "custody_fee_pct", "sales_fee_pct")]
        row["annual_cost_pct"] = round(sum(float(value) for value in fees), 4) if all(value is not None for value in fees) else None
        return row

    @staticmethod
    def _keywords(value: str) -> list[str]:
        words = [value]
        for suffix in ("生物", "行业", "主题", "板块"):
            if value.endswith(suffix) and len(value) > len(suffix):
                words.append(value[:-len(suffix)])
        if len(value) >= 4:
            words.extend(value[i:i+2] for i in range(len(value)-1))
        return [word for word in dict.fromkeys(words) if len(word) >= 2]

    def _attach_tracking_error(self, candidates: list[dict[str, Any]], target: dict[str, Any]):
        # A research proxy such as BK1643 is not the official investable index.
        # Comparing a fund against that proxy would produce a precise-looking but
        # invalid tracking error, so only calculate when the identity is exact.
        if "同一指数" not in str(target.get("investable_relation") or ""):
            return
        official_code = str(target.get("official_index_code") or "").upper()
        if not re.fullmatch(r"(?:\d{6}|H\d{5})", official_code):
            return
        official_security_id = f"CSI.{official_code}"
        bars = self.db.rows(
            """SELECT trade_date,close FROM market_bars WHERE security_id=?
               ORDER BY trade_date DESC LIMIT 140""", (official_security_id,),
        )
        bars = sorted(bars, key=lambda row: row["trade_date"])
        if len(bars) < 20:
            try:
                fetched = CSIOfficialKlineProvider(timeout_seconds=self.timeout_seconds).fetch(
                    official_security_id, 140,
                )
            except Exception:
                fetched = []
            if fetched:
                now = local_now().isoformat(timespec="seconds")
                self.db.executemany(
                    """INSERT INTO market_bars(
                           provider,security_id,trade_date,open,close,high,low,volume,amount,fetched_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(provider,security_id,trade_date) DO UPDATE SET
                           open=excluded.open,close=excluded.close,high=excluded.high,low=excluded.low,
                           volume=excluded.volume,amount=excluded.amount,fetched_at=excluded.fetched_at""",
                    [("csi_official", official_security_id, bar.trade_date, bar.open, bar.close,
                      bar.high, bar.low, bar.volume, bar.amount, now) for bar in fetched],
                )
                bars = [{"trade_date": bar.trade_date, "close": bar.close} for bar in fetched]
        if len(bars) < 20:
            return
        index_returns = {bars[i]["trade_date"]: float(bars[i]["close"])/float(bars[i-1]["close"])-1 for i in range(1,len(bars)) if float(bars[i-1]["close"])>0}
        start, end = bars[0]["trade_date"], bars[-1]["trade_date"]
        provider = EastmoneyFundNavProvider(timeout_seconds=self.timeout_seconds)
        for row in candidates:
            if row["match_kind"] != "同一指数":
                continue
            try:
                navs, _ = provider.fetch(row["fund_code"], start, end)
            except Exception:
                continue
            ordered = sorted(navs.items())
            fund_returns = {ordered[i][0]: ordered[i][1]/ordered[i-1][1]-1 for i in range(1,len(ordered)) if ordered[i-1][1]>0}
            common = sorted(set(index_returns) & set(fund_returns))
            row["tracking_days"] = len(common)
            if len(common) >= MIN_TRACKING_DAYS:
                differences = [fund_returns[d]-index_returns[d] for d in common]
                row["tracking_error_pct"] = round(statistics.pstdev(differences)*math.sqrt(250)*100, 4)

    def _attach_horizon_fees(self, candidates: list[dict[str, Any]], holding_days: int):
        def fetch(row: dict[str, Any]):
            try:
                page = self._download(row["fee_url"]).decode("utf-8", errors="replace")
                row["redemption_fee_pct"] = parse_redemption_rate(page, holding_days)
                purchase_rate = parse_small_purchase_rate(page)
                if purchase_rate is not None:
                    row["purchase_fee_pct"] = purchase_rate
            except Exception:
                row["redemption_fee_pct"] = None
            annual = row.get("annual_cost_pct")
            purchase = row.get("purchase_fee_pct")
            redemption = row.get("redemption_fee_pct")
            if annual is not None and purchase is not None and redemption is not None:
                row["horizon_cost_pct"] = round(
                    float(annual) * holding_days / 365 + float(purchase) + float(redemption), 4
                )
            else:
                row["horizon_cost_pct"] = None

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(fetch, candidates))

    @staticmethod
    def _rank_key(row: dict[str, Any]):
        strategy_order = 0 if row.get("strategy_kind") in {"被动ETF联接", "被动指数"} else 1
        return (0 if row["match_kind"] == "同一指数" else 1, strategy_order, -float(row["score"]), row["fund_code"])

    @staticmethod
    def _score(row: dict[str, Any], holding_days: int):
        exact = row["match_kind"] == "同一指数"
        index_points = 30.0 if exact else 15.0
        enhanced_penalty = -3.0 if row.get("strategy_kind") == "指数增强" else 0.0
        tracking = row.get("tracking_error_pct")
        tracking_points = max(0.0, 25.0-float(tracking)*5) if exact and tracking is not None else 0.0
        annual_value = row.get("annual_cost_pct")
        purchase_value = row.get("purchase_fee_pct")
        redemption_value = row.get("redemption_fee_pct")
        horizon_cost = row.get("horizon_cost_pct")
        cost_points = max(0.0, 20.0-float(horizon_cost)*12) if horizon_cost is not None else 0.0
        size_value = row.get("asset_size_billion")
        size = float(size_value) if size_value is not None else None
        size_points = min(10.0, max(0.0, math.log10(max(size, .1))+1)*4) if size is not None and size > 0 else 0.0
        inception = row.get("inception_date") or ""
        try:
            age_years = (local_today()-date.fromisoformat(inception)).days/365.25
        except ValueError:
            age_years = 0
        age_points = min(5.0, age_years/3*5)
        buyable_points = 10.0 if row["buyable_status"] == "公开渠道可申购" else 0.0
        score = index_points + tracking_points + cost_points + size_points + age_points + buyable_points + enhanced_penalty
        row["score"] = round(score, 2)
        track_days = int(row.get("tracking_days", 0) or 0)
        if not exact:
            track_text = "不同指数，跟踪误差不可横向比较"
        elif tracking is not None:
            track_text = f"近{track_days}个重合交易日年化跟踪误差{tracking:.2f}%"
        else:
            track_text = f"跟踪误差仅{track_days}个重合日（至少需{MIN_TRACKING_DAYS}日），本项0分"
        cost_text = f"年运作费约{float(annual_value):.2f}%" if annual_value is not None else "年运作费数据不完整，未计费用分"
        purchase_text = f"最高申购费{float(purchase_value):.2f}%" if purchase_value is not None else "申购费数据不完整，未计费用分"
        redemption_text = f"按{holding_days}天赎回费{float(redemption_value):.2f}%" if redemption_value is not None else "持有期赎回费未取得，未计完整成本分"
        horizon_text = f"持有期保守总成本约{float(horizon_cost):.2f}%" if horizon_cost is not None else "持有期总成本不完整"
        size_text = f"规模{size:.2f}亿元" if size is not None else "规模数据不完整，未计规模分"
        breakdown = (
            f"综合分{row['score']:.2f}=指数一致性{index_points:.2f}/30"
            f"＋跟踪质量{tracking_points:.2f}/25＋{holding_days}天成本{cost_points:.2f}/20"
            f"＋规模{size_points:.2f}/10＋成立年限{age_points:.2f}/5"
            f"＋可申购{buyable_points:.2f}/10"
            + (f"＋增强策略调整{enhanced_penalty:.2f}" if enhanced_penalty else "")
        )
        row["reason"] = f"{breakdown}。{row['match_kind']}；{track_text}；{cost_text}；{purchase_text}；{redemption_text}；{horizon_text}；{size_text}。缺失指标按0分，不会把缺失误当成0误差。"

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={
            "Accept": "application/json,text/html,*/*", "Accept-Encoding": "identity",
            "Connection": "close", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.1",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=ssl.create_default_context()) as response:
                raw = response.read(MAX_RESPONSE_BYTES+1)
        except Exception as first:
            curl = windows_curl_path()
            if not curl:
                raise FundDiscoveryError(f"公开基金资料连接失败：{first}") from first
            completed = subprocess.run(
                [curl,"--fail","--silent","--show-error","--location","--http1.1","--ipv4","--tlsv1.2","--tls-max","1.2",
                 "--proto","=https","--proto-redir","=https","--max-filesize",str(MAX_RESPONSE_BYTES),"--connect-timeout",str(min(10,self.timeout_seconds)),
                 "--max-time",str(self.timeout_seconds),"--header","Accept-Encoding: identity","--user-agent","Mozilla/5.0 SectorRadar/2.1",url],
                capture_output=True, timeout=self.timeout_seconds+5, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0), check=False,
            )
            if completed.returncode != 0 or not completed.stdout:
                raise FundDiscoveryError("公开基金资料连接失败，未写入任何候选。")
            raw = completed.stdout
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FundDiscoveryError("公开基金资料超过5MB安全上限。")
        return raw


class FundFeeSyncService:
    """Populate auditable redemption ladders for the local fund library."""

    def __init__(self, db):
        self.db = db
        self.downloader = FundDiscoveryService(db)

    def sync(
        self,
        fund_codes: list[str] | None = None,
        *,
        only_missing: bool = True,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        if fund_codes:
            placeholders = ",".join("?" for _ in fund_codes)
            funds = self.db.rows(f"SELECT * FROM funds WHERE fund_code IN ({placeholders}) ORDER BY fund_code", fund_codes)
        else:
            funds = self.db.rows("SELECT * FROM funds ORDER BY fund_code")
        updated = 0
        skipped = 0
        errors: list[str] = []
        stopped = False
        for index, fund in enumerate(funds, start=1):
            if stop_requested and stop_requested():
                stopped = True
                break
            code = str(fund["fund_code"])
            if progress:
                progress(index, len(funds), code)
            if only_missing and self.db.row("SELECT 1 FROM fee_tiers WHERE fund_code=? LIMIT 1", (code,)):
                skipped += 1
                continue
            url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
            try:
                page = self.downloader._download(url).decode("utf-8", errors="replace")
                tiers = parse_redemption_tiers(page)
                if not tiers:
                    raise FundDiscoveryError("公开费率页未识别到赎回阶梯")
            except Exception as exc:
                errors.append(f"{code} {fund['fund_name']}：{exc}")
                continue
            checked = local_now().isoformat(timespec="seconds")
            with self.db.connect() as conn:
                conn.execute("DELETE FROM fee_tiers WHERE fund_code=?", (code,))
                conn.executemany(
                    """INSERT INTO fee_tiers(
                           fund_code,min_days,max_days,redeem_fee_pct,source_url,checked_at
                       ) VALUES(?,?,?,?,?,?)""",
                    [(code, minimum, maximum, rate, url, checked) for minimum, maximum, rate in tiers],
                )
            updated += 1
        return {
            "total": len(funds), "updated": updated, "skipped": skipped,
            "errors": errors, "stopped": stopped,
        }

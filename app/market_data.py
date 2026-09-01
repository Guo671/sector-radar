from __future__ import annotations

import json
import http.client
import math
import os
import re
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import local_now, local_today
from .trading_calendar import ChinaTradingCalendar


def _transport_error_detail(exc: BaseException) -> str:
    """Return a bounded, useful description for public-market transport failures.

    ``http.client.IncompleteRead`` is raised by ``response.read`` when a server
    closes a response before the advertised Content-Length.  It is an
    ``HTTPException`` rather than an ``OSError``, so the former download loops
    allowed it to escape and abort the entire catalog job.  Keep the partial
    body out of logs; only report byte counts before retrying another transport.
    """
    if isinstance(exc, http.client.IncompleteRead):
        partial = getattr(exc, "partial", b"")
        received = len(partial) if isinstance(partial, (bytes, bytearray)) else 0
        expected = getattr(exc, "expected", None)
        if isinstance(expected, int) and expected >= 0:
            return f"响应被远端截断（本次已收到{received}字节，预计还缺{expected}字节）"
        return f"响应被远端截断（本次已收到{received}字节）"
    return str(getattr(exc, "reason", exc))


def windows_curl_path() -> str | None:
    """Return Microsoft's bundled curl before any third-party PATH copy.

    Git, Conda and developer tools frequently prepend their own OpenSSL-based
    curl.exe to PATH.  Calling ``shutil.which`` first therefore did not
    guarantee the second Windows TLS stack promised by the UI.
    """
    if not sys.platform.startswith("win"):
        return None
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if windows_root:
        candidates = [
            Path(windows_root) / "System32" / "curl.exe",
            Path(windows_root) / "Sysnative" / "curl.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


class MarketDataError(RuntimeError):
    """Raised when hard market data cannot pass the local quality gate."""


class MarketTransportError(MarketDataError):
    """All verified HTTPS attempts to one public provider failed."""

    def __init__(self, message: str, diagnostics: list[str] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


class MarketFallbackError(MarketDataError):
    """Every reviewed fallback route failed or lacked enough usable history."""

    def __init__(self, message: str, diagnostics: list[str] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


@dataclass(frozen=True)
class MarketBar:
    trade_date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float


@dataclass(frozen=True)
class ProxyMapping:
    universe_id: str
    provider: str
    security_id: str
    proxy_code: str
    proxy_name: str
    mapping_kind: str
    confidence: float
    rationale: str


@dataclass(frozen=True)
class ResolvedProxy:
    security_id: str
    proxy_code: str
    proxy_name: str
    mapping_kind: str
    confidence: float
    rationale: str


# These are not guessed at run time. They are reviewed exchange-traded funds
# whose official product pages state that they track the corresponding index.
# They are used only after every exact-index public route has failed validation.
CURATED_ETF_FALLBACKS: dict[str, ResolvedProxy] = {
    "2.932000": ResolvedProxy(
        "1.563300", "563300", "中证2000ETF华泰柏瑞", "ETF代理", 0.93,
        "中证2000精确指数免费源不可用时，使用明确跟踪中证2000指数的场内ETF；结果必须降级标注。",
    ),
    "2.930050": ResolvedProxy(
        "1.563080", "563080", "中证A50ETF易方达", "ETF代理", 0.94,
        "中证A50精确指数免费源不可用时，使用明确跟踪中证A50指数的场内ETF；结果必须降级标注。",
    ),
}


# The API test deliberately uses 18 broad indices plus two transparent proxies.
# It does not take the first 20 rows of the universe, which previously mixed in
# two unmapped Shenwan industries and made the test nondeterministic.
TEST20_IDS = tuple([f"U{i:03d}" for i in range(1, 19)] + ["U105", "U163"])


def default_proxy_mappings() -> list[ProxyMapping]:
    """Curated, reviewable mappings for the zero-cost 20-direction data test.

    ``security_id`` is Eastmoney's market-id/security-code form. The provider is
    intentionally labelled ``eastmoney_public`` because this is a public web
    interface rather than a contracted, SLA-backed market-data feed.
    """

    rows = [
        ("U001", "1.000001", "000001", "上证指数", "精确指数", 1.00, "目标就是上证综指。"),
        ("U002", "0.399001", "399001", "深证成指", "精确指数", 1.00, "目标就是深证成指。"),
        ("U003", "0.399006", "399006", "创业板指", "精确指数", 1.00, "目标就是创业板指。"),
        ("U004", "1.000688", "000688", "科创50", "精确指数", 1.00, "目标就是科创50指数。"),
        ("U005", "0.899050", "899050", "北证50", "精确指数", 1.00, "目标就是北证50指数。"),
        ("U006", "1.000300", "000300", "沪深300", "精确指数", 1.00, "目标就是沪深300。"),
        ("U007", "1.000905", "000905", "中证500", "精确指数", 1.00, "目标就是中证500。"),
        ("U008", "1.000906", "000906", "中证800", "精确指数", 1.00, "目标就是中证800。"),
        ("U009", "1.000852", "000852", "中证1000", "精确指数", 1.00, "目标就是中证1000。"),
        ("U010", "2.932000", "932000", "中证2000", "精确指数", 1.00, "目标就是中证2000；东方财富统一行情市场前缀为2。"),
        ("U011", "2.930050", "930050", "中证A50", "精确指数", 1.00, "目标就是中证A50；东方财富统一行情市场前缀为2。"),
        ("U012", "1.000016", "000016", "上证50", "精确指数", 1.00, "目标就是上证50。"),
        ("U013", "0.399330", "399330", "深证100", "精确指数", 1.00, "目标就是深证100。"),
        ("U014", "0.399673", "399673", "创业板50", "精确指数", 1.00, "目标就是创业板50。"),
        ("U015", "1.000698", "000698", "科创100", "精确指数", 1.00, "目标就是科创100。"),
        ("U016", "1.000985", "000985", "中证全指", "显式代理", 0.85, "免费源缺少万得全A，明确使用中证全指作全市场代理。"),
        ("U017", "0.399303", "399303", "国证2000", "精确指数", 1.00, "目标就是国证2000。"),
        ("U018", "1.000985", "000985", "中证全指", "精确指数", 1.00, "目标就是中证全指。"),
        ("U105", "1.000922", "000922", "中证红利", "显式代理", 0.92, "红利风格用中证红利作透明代理。"),
        ("U163", "1.518880", "518880", "华安黄金ETF", "ETF代理", 0.88, "黄金方向用流动性较高的境内黄金ETF价格代理。"),
    ]
    return [ProxyMapping(uid, "eastmoney_public", secid, code, name, kind, conf, why) for uid, secid, code, name, kind, conf, why in rows]


class CSIOfficialKlineProvider:
    """Daily history from the official CSI index performance endpoint.

    This route is required for H-prefixed industry codes, which the former
    Eastmoney/Tencent market-id scheme cannot represent reliably.
    """

    name = "csi_official"
    endpoint = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    max_response_bytes = 5 * 1024 * 1024
    browser_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
        min_request_interval_seconds: float = 4.0,
    ):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._requester = requester
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self._last_request_started_at = 0.0
        self._diagnostics_by_security: dict[str, list[str]] = {}
        self._python_transport_unavailable = False
        self._rate_limited_until = 0.0

    def fetch(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        match = re.fullmatch(r"CSI\.((?:\d{6}|H\d{5}))", str(security_id or ""), re.IGNORECASE)
        if not match:
            raise MarketDataError(f"中证官方证券ID格式无效：{security_id!r}。")
        code = match.group(1).upper()
        limit = max(30, min(500, int(limit)))
        end = local_today()
        start = end - timedelta(days=max(240, limit * 3))
        query = urllib.parse.urlencode({
            "indexCode": code,
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        })
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "close",
                "Origin": "https://www.csindex.com.cn",
                "Referer": "https://www.csindex.com.cn/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": self.browser_user_agent,
            },
        )
        self._pace_official_requests()
        raw = self._download(request, security_id)
        if len(raw) > self.max_response_bytes:
            raise MarketDataError(f"中证官方行情响应超过5MB安全上限（{code}）。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:100].decode("utf-8", errors="replace").strip()
            raise MarketDataError(f"中证官方行情不是有效JSON（{code}）：{preview or '空响应'}。") from exc
        if not isinstance(payload, dict) or str(payload.get("code") or "") != "200":
            raise MarketDataError(f"中证官方行情返回失败（{code}）：{payload.get('msg', '未知错误')}。")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise MarketDataError(f"中证官方行情没有返回日线列表（{code}）。")
        by_date: dict[str, MarketBar] = {}
        returned_names: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            returned_code = str(row.get("indexCode") or "").upper()
            if returned_code and returned_code != code:
                raise MarketDataError(f"中证官方行情返回代码{returned_code}，与请求{code}不一致。")
            try:
                raw_day = str(row.get("tradeDate") or "")
                day = date(int(raw_day[:4]), int(raw_day[4:6]), int(raw_day[6:8])).isoformat()
                bar = MarketBar(
                    trade_date=day,
                    open=float(row["open"]),
                    close=float(row["close"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    volume=float(row.get("tradingVol") or 0),
                    # CSI exposes tradingValue in CNY 100 million.  Ratios are
                    # invariant to the scale, but storing CNY keeps the field honest.
                    amount=float(row.get("tradingValue") or 0) * 100_000_000,
                )
            except (KeyError, TypeError, ValueError):
                continue
            by_date[day] = bar
            name = str(row.get("indexNameCnAll") or "").strip()
            if name:
                returned_names.add(name)
        bars = sorted(by_date.values(), key=lambda item: item.trade_date)[-limit:]
        bars = validate_bars(bars, security_id)
        name_note = f"（{next(iter(returned_names))}）" if len(returned_names) == 1 else ""
        transport_notes = self._diagnostics_by_security.get(security_id, [])
        self._diagnostics_by_security[security_id] = list(dict.fromkeys(
            transport_notes + [f"中证指数有限公司官网：成功{name_note}"]
        ))
        return bars

    def diagnostics_for(self, security_id: str) -> list[str]:
        return list(self._diagnostics_by_security.get(security_id, []))

    def _pace_official_requests(self) -> None:
        """Keep a full catalog scan below CSI's public-endpoint burst limit."""
        if self._requester is not None or self.min_request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_started_at
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_started_at = time.monotonic()

    def _download(self, request: urllib.request.Request, security_id: str) -> bytes:
        if self._requester:
            return self._requester(request, self.timeout_seconds)
        if time.monotonic() < self._rate_limited_until:
            failures = ["中证官网本轮已触发频率保护，已停止重复请求；已成功项目保留在本机缓存"]
            self._diagnostics_by_security[security_id] = failures
            raise MarketTransportError(
                f"中证官网正在限制连续访问（{security_id}）；请稍后续跑，软件只会补齐未完成项目。",
                failures,
            )
        failures: list[str] = []
        python_transport_failed = False
        python_http_rejected = False
        if self._python_transport_unavailable:
            failures.append("中证官网 Python HTTPS：本轮已熔断，直接使用Windows安全网络通道")
        else:
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(
                        request, timeout=self.timeout_seconds, context=ssl.create_default_context()
                    ) as response:
                        raw = response.read(self.max_response_bytes + 1)
                        if self._looks_like_rate_limit_page(raw):
                            failures.append("中证官网 Python HTTPS：收到频率保护页")
                            self._rate_limited_until = time.monotonic() + 300
                            break
                        if failures:
                            self._diagnostics_by_security[security_id] = list(dict.fromkeys(
                                failures + ["中证官网 Python HTTPS：重试后成功接收完整响应"]
                            ))
                        return raw
                except urllib.error.HTTPError as exc:
                    failures.append(f"中证官网 Python HTTPS 第{attempt + 1}次：HTTP {exc.code}")
                    python_http_rejected = True
                    if exc.code in {403, 429} and attempt == 0:
                        time.sleep(2.0)
                except (
                    urllib.error.URLError, ssl.SSLError, http.client.HTTPException,
                    ConnectionError, OSError, TimeoutError,
                ) as exc:
                    failures.append(f"中证官网 Python HTTPS 第{attempt + 1}次：{_transport_error_detail(exc)}")
                    python_transport_failed = True
                if attempt == 0:
                    time.sleep(0.35)
            # HTTP 403/429 proves that the TLS transport itself works.  Do not
            # permanently trip the Python circuit breaker for a server throttle.
            if python_transport_failed and not python_http_rejected:
                self._python_transport_unavailable = True
        curl = windows_curl_path()
        if curl:
            command = [
                curl, "--fail", "--silent", "--show-error", "--location", "--http1.1", "--ipv4",
                "--proto", "=https", "--proto-redir", "=https", "--max-filesize", str(self.max_response_bytes),
                "--connect-timeout", str(min(10, self.timeout_seconds)), "--max-time", str(self.timeout_seconds),
                "--header", "Accept: application/json, text/plain, */*",
                "--header", "Accept-Encoding: identity",
                "--header", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                "--header", "Origin: https://www.csindex.com.cn",
                "--header", "Referer: https://www.csindex.com.cn/",
                "--header", "Sec-Fetch-Dest: empty",
                "--header", "Sec-Fetch-Mode: cors",
                "--header", "Sec-Fetch-Site: same-origin",
                "--user-agent", self.browser_user_agent,
                request.full_url,
            ]
            for curl_attempt in range(3):
                try:
                    completed = subprocess.run(
                        command, capture_output=True, timeout=self.timeout_seconds + 5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                    )
                    if completed.returncode == 0 and completed.stdout:
                        if self._looks_like_rate_limit_page(completed.stdout):
                            failures.append("Windows系统curl：收到中证官网频率保护页")
                            self._rate_limited_until = time.monotonic() + 300
                            break
                        self._diagnostics_by_security[security_id] = list(dict.fromkeys(
                            failures + ["Windows系统curl：成功接收完整响应"]
                        ))
                        return completed.stdout
                    detail = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
                    failures.append(
                        f"Windows系统curl第{curl_attempt + 1}次退出码{completed.returncode}："
                        f"{detail or '无错误正文'}"
                    )
                    throttled = "403" in detail or "429" in detail
                    if throttled and curl_attempt < 2:
                        time.sleep((3.0, 10.0)[curl_attempt])
                        continue
                    if throttled:
                        self._rate_limited_until = time.monotonic() + 300
                    break
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failures.append(f"Windows系统curl：{exc}")
                    break
        message = "；".join(dict.fromkeys(failures))
        self._diagnostics_by_security[security_id] = failures
        raise MarketTransportError(f"无法连接中证官方行情源（{security_id}）：{message or '未知网络错误'}", failures)

    @staticmethod
    def _looks_like_rate_limit_page(raw: bytes) -> bool:
        preview = raw[:4096].lower()
        return b"<html" in preview and (
            b"<title>405" in preview or b"attack.jinxibei.com" in preview
        )


class EastmoneyKlineProvider:
    name = "eastmoney_public"
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    max_response_bytes = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
        fallback_provider: "ReviewedFallbackKlineProvider | TencentKlineProvider | None" = None,
    ):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._requester = requester
        self._fallback = fallback_provider if fallback_provider is not None else (
            ReviewedFallbackKlineProvider(timeout_seconds=self.timeout_seconds) if requester is None else None
        )
        self._primary_transport_unavailable = False
        self._source_by_security: dict[str, str] = {}
        self._activity_by_security: dict[str, str] = {}
        self._diagnostics_by_security: dict[str, list[str]] = {}
        self._csi = CSIOfficialKlineProvider(
            timeout_seconds=self.timeout_seconds,
            requester=requester,
        )

    def fetch(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        if re.fullmatch(r"CSI\.(?:\d{6}|H\d{5})", str(security_id or ""), re.IGNORECASE):
            bars = self._csi.fetch(security_id, limit)
            self._source_by_security[security_id] = self._csi.name
            self._activity_by_security[security_id] = "amount"
            self._diagnostics_by_security[security_id] = self._csi.diagnostics_for(security_id)
            return bars
        if not re.fullmatch(r"\d{1,3}\.(?:\d{6}|BK\d{4}|[A-Z][A-Z0-9]{3,11})", str(security_id or "")):
            raise MarketDataError(f"证券ID格式无效：{security_id!r}。")
        diagnostics: list[str] = []
        fallback_capable = bool(re.fullmatch(r"\d\.\d{6}", security_id))
        if self._primary_transport_unavailable and self._fallback is not None and fallback_capable:
            diagnostics.append("东方财富主源：本轮已触发网络熔断，跳过重复失败连接")
            return self._fetch_fallback(security_id, limit, diagnostics)
        try:
            bars = self._fetch_primary(security_id, limit)
            self._source_by_security[security_id] = self.name
            self._activity_by_security[security_id] = "amount"
            self._diagnostics_by_security[security_id] = ["东方财富主源：成功"]
            return bars
        except MarketTransportError as exc:
            diagnostics.extend(exc.diagnostics or [str(exc)])
            if self._fallback is None or not fallback_capable:
                self._diagnostics_by_security[security_id] = diagnostics
                raise
            # A full set of Python + Windows system-curl transport failures is
            # provider-wide, not security-code-specific. Avoid repeating the
            # same dead path another 18 times in this job.
            self._primary_transport_unavailable = True
            return self._fetch_fallback(security_id, limit, diagnostics)

    def _fetch_primary(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        limit = max(30, min(500, int(limit)))
        query = urllib.parse.urlencode({
            "secid": security_id,
            "klt": "101",
            "fqt": "1",
            "lmt": str(limit),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        })
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
            },
        )
        raw = self._download(request, security_id)
        if len(raw) > self.max_response_bytes:
            raise MarketDataError(f"行情源响应超过5MB安全上限（{security_id}）。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:100].decode("utf-8", errors="replace").strip()
            raise MarketDataError(f"行情源返回的不是有效JSON（{security_id}）：{preview or '空响应'}。") from exc
        if not isinstance(payload, dict):
            raise MarketDataError(f"行情源返回的JSON类型异常（{security_id}）。")
        data = payload.get("data")
        if not isinstance(data, dict):
            rc = payload.get("rc", "未知")
            message = payload.get("message") or payload.get("msg") or "未返回data"
            raise MarketDataError(
                f"行情源没有该证券ID的数据（{security_id}，rc={rc}，{message}）；"
                "请检查市场前缀与指数代码。"
            )
        returned_code = str(data.get("code") or "").strip()
        expected_code = security_id.split(".", 1)[1]
        if returned_code and returned_code != expected_code:
            raise MarketDataError(f"行情源返回代码{returned_code}，与请求{expected_code}不一致。")
        lines = data.get("klines")
        if not isinstance(lines, list):
            raise MarketDataError(f"行情源没有返回K线列表（{security_id}，rc={payload.get('rc', '未知')}）。")
        bars: list[MarketBar] = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            try:
                bar = MarketBar(
                    trade_date=date.fromisoformat(parts[0]).isoformat(),
                    open=float(parts[1]), close=float(parts[2]), high=float(parts[3]), low=float(parts[4]),
                    volume=float(parts[5]), amount=float(parts[6]),
                )
            except (TypeError, ValueError):
                continue
            bars.append(bar)
        return validate_bars(bars, security_id)

    def _fetch_fallback(self, security_id: str, limit: int, diagnostics: list[str]) -> list[MarketBar]:
        assert self._fallback is not None
        try:
            bars = self._fallback.fetch(security_id, limit)
        except MarketDataError as exc:
            fallback_details = getattr(exc, "diagnostics", None) or [str(exc)]
            diagnostics.extend(fallback_details)
            self._diagnostics_by_security[security_id] = diagnostics
            tail = fallback_details[-1] if fallback_details else str(exc)
            raise MarketFallbackError(
                f"东方财富主源与全部审核备用路线均失败（{security_id}）：{tail}", diagnostics
            ) from exc
        actual_source = (
            self._fallback.source_for(security_id)
            if hasattr(self._fallback, "source_for") else self._fallback.name
        )
        activity_field = (
            self._fallback.activity_field_for(security_id)
            if hasattr(self._fallback, "activity_field_for") else "volume"
        )
        self._source_by_security[security_id] = actual_source
        self._activity_by_security[security_id] = activity_field
        if hasattr(self._fallback, "diagnostics_for"):
            diagnostics.extend(self._fallback.diagnostics_for(security_id))
        if not any("成功" in item for item in diagnostics):
            activity_note = "；历史量能按成交量计算" if activity_field == "volume" else ""
            diagnostics.append(f"{actual_source}备用源：成功{activity_note}")
        self._diagnostics_by_security[security_id] = diagnostics
        return bars

    def source_for(self, security_id: str) -> str:
        return self._source_by_security.get(security_id, self.name)

    def activity_field_for(self, security_id: str) -> str:
        return self._activity_by_security.get(security_id, "amount")

    def diagnostics_for(self, security_id: str) -> list[str]:
        return list(self._diagnostics_by_security.get(security_id, []))

    def actual_security_id_for(self, security_id: str) -> str:
        if hasattr(self._fallback, "actual_security_id_for"):
            return self._fallback.actual_security_id_for(security_id)
        return security_id

    def resolved_proxy_for(self, security_id: str) -> ResolvedProxy | None:
        if hasattr(self._fallback, "resolved_proxy_for"):
            return self._fallback.resolved_proxy_for(security_id)
        return None

    def _download(self, request: urllib.request.Request, security_id: str) -> bytes:
        if self._requester:
            return self._requester(request, self.timeout_seconds)

        failures: list[str] = []
        for attempt in range(2):
            try:
                context = ssl.create_default_context()
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                    return response.read(self.max_response_bytes + 1)
            except urllib.error.HTTPError as exc:
                failures.append(f"东方财富 Python HTTPS 第{attempt + 1}次：HTTP {exc.code}")
            except (
                urllib.error.URLError, ssl.SSLError, http.client.HTTPException,
                ConnectionError, OSError, TimeoutError,
            ) as exc:
                failures.append(f"东方财富 Python HTTPS 第{attempt + 1}次：{_transport_error_detail(exc)}")
            if attempt == 0:
                time.sleep(0.35)

        # Prefer Microsoft's bundled curl, force IPv4 to avoid broken local
        # IPv6 routes, and keep certificate verification enabled.
        curl = windows_curl_path()
        if curl:
            command = [
                curl, "--fail", "--silent", "--show-error", "--location", "--http1.1", "--ipv4",
                "--tlsv1.2", "--tls-max", "1.2",
                "--proto", "=https", "--proto-redir", "=https", "--max-filesize", str(self.max_response_bytes),
                "--connect-timeout", str(min(10, self.timeout_seconds)),
                "--max-time", str(self.timeout_seconds),
                "--header", "Accept: application/json, text/plain, */*",
                "--header", "Accept-Encoding: identity",
                "--header", "Referer: https://quote.eastmoney.com/",
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
                    f"Windows系统curl（{curl}）退出码{completed.returncode}：{detail or '无错误正文'}"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"Windows系统curl（{curl}）：{exc}")
        else:
            failures.append("Windows系统curl：未找到")

        summary = "；".join(dict.fromkeys(failures))
        if "UNEXPECTED_EOF_WHILE_READING" in summary or "EOF occurred" in summary:
            raise MarketTransportError(
                f"东方财富行情TLS连接被远端提前关闭（{security_id}）。", failures
            )
        raise MarketTransportError(
            f"无法连接东方财富行情源（{security_id}）：{summary or '未知网络错误'}", failures
        )


class TencentKlineProvider:
    """Independent daily-K-line fallback for the curated 20-item test.

    Tencent's public response contains OHLC and volume but no historical
    turnover amount.  The pipeline therefore labels the resulting activity
    ratio as volume-based instead of silently pretending it is amount-based.
    """

    name = "tencent_public"
    endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    max_response_bytes = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
    ):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._requester = requester

    @staticmethod
    def symbol_for(security_id: str) -> str:
        if not re.fullmatch(r"\d\.\d{6}", str(security_id or "")):
            raise MarketDataError(f"证券ID格式无效：{security_id!r}；应为市场前缀.六位代码。")
        market, code = security_id.split(".", 1)
        if code.startswith(("8", "9")) and market == "0":
            return "bj" + code
        if market == "0":
            return "sz" + code
        if market in {"1", "2"}:
            return "sh" + code
        raise MarketDataError(f"腾讯备用源不支持市场前缀{market}（{security_id}）。")

    def fetch(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        symbol = self.symbol_for(security_id)
        limit = max(30, min(500, int(limit)))
        query = urllib.parse.urlencode({"param": f"{symbol},day,,,{limit},qfq"})
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": "https://stockapp.finance.qq.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
            },
        )
        raw = self._download(request, security_id)
        if len(raw) > self.max_response_bytes:
            raise MarketDataError(f"腾讯备用行情响应超过5MB安全上限（{security_id}）。")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:100].decode("utf-8", errors="replace").strip()
            raise MarketDataError(f"腾讯备用行情返回的不是有效JSON（{security_id}）：{preview or '空响应'}。") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise MarketDataError(
                f"腾讯备用行情返回失败（{security_id}，code={payload.get('code') if isinstance(payload, dict) else '类型异常'}）。"
            )
        data = payload.get("data")
        if not isinstance(data, dict) or symbol not in data or not isinstance(data[symbol], dict):
            returned = "、".join(str(key) for key in data) if isinstance(data, dict) else "无"
            raise MarketDataError(f"腾讯备用行情未按请求代码返回{symbol}（实际键：{returned or '无'}）。")
        item = data[symbol]
        lines = item.get("qfqday") or item.get("day")
        if not isinstance(lines, list):
            raise MarketDataError(f"腾讯备用行情没有返回日K线列表（{symbol}）。")
        qt = item.get("qt")
        if isinstance(qt, dict) and isinstance(qt.get(symbol), list) and len(qt[symbol]) > 2:
            returned_code = str(qt[symbol][2] or "").strip()
            expected_code = symbol[2:]
            if returned_code and returned_code != expected_code:
                raise MarketDataError(f"腾讯备用行情返回代码{returned_code}，与请求{expected_code}不一致。")
        bars: list[MarketBar] = []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 6:
                continue
            try:
                bars.append(MarketBar(
                    trade_date=date.fromisoformat(str(line[0])).isoformat(),
                    open=float(line[1]), close=float(line[2]), high=float(line[3]), low=float(line[4]),
                    volume=float(line[5]), amount=0.0,
                ))
            except (TypeError, ValueError):
                continue
        return validate_bars(bars, f"腾讯{symbol}")

    def _download(self, request: urllib.request.Request, security_id: str) -> bytes:
        if self._requester:
            return self._requester(request, self.timeout_seconds)
        failures: list[str] = []
        for attempt in range(2):
            try:
                context = ssl.create_default_context()
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                    return response.read(self.max_response_bytes + 1)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise MarketDataError(f"腾讯备用行情HTTP {exc.code}（{security_id}）。") from exc
                failures.append(f"腾讯 Python HTTPS 第{attempt + 1}次：HTTP {exc.code}")
            except (
                urllib.error.URLError, ssl.SSLError, http.client.HTTPException,
                ConnectionError, OSError, TimeoutError,
            ) as exc:
                failures.append(f"腾讯 Python HTTPS 第{attempt + 1}次：{_transport_error_detail(exc)}")
            if attempt == 0:
                time.sleep(0.35)
        curl = windows_curl_path()
        if curl:
            command = [
                curl, "--fail", "--silent", "--show-error", "--location", "--http1.1", "--ipv4",
                "--tlsv1.2", "--tls-max", "1.2", "--proto", "=https", "--proto-redir", "=https",
                "--max-filesize", str(self.max_response_bytes), "--connect-timeout", str(min(10, self.timeout_seconds)),
                "--max-time", str(self.timeout_seconds), "--header", "Accept: application/json, text/plain, */*",
                "--header", "Accept-Encoding: identity", "--header", "Referer: https://stockapp.finance.qq.com/",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0", request.full_url,
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, timeout=self.timeout_seconds + 5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                )
                if completed.returncode == 0 and completed.stdout:
                    return completed.stdout
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
                failures.append(f"腾讯 Windows系统curl退出码{completed.returncode}：{detail or '无错误正文'}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"腾讯 Windows系统curl：{exc}")
        else:
            failures.append("腾讯 Windows系统curl：未找到")
        raise MarketTransportError(f"无法连接腾讯备用行情源（{security_id}）。", failures)


class SohuKlineProvider:
    """Independent exact-security daily history used after Tencent validation fails."""

    name = "sohu_public"
    endpoint = "https://q.stock.sohu.com/hisHq"
    max_response_bytes = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
    ):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._requester = requester

    @staticmethod
    def symbol_for(security_id: str) -> str:
        if not re.fullmatch(r"\d\.\d{6}", str(security_id or "")):
            raise MarketDataError(f"证券ID格式无效：{security_id!r}；应为市场前缀.六位代码。")
        market, code = security_id.split(".", 1)
        is_index = (
            market == "2"
            or (market == "0" and code.startswith(("399", "899")))
            or (market == "1" and code.startswith("000"))
        )
        return ("zs_" if is_index else "cn_") + code

    def fetch(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        symbol = self.symbol_for(security_id)
        limit = max(30, min(500, int(limit)))
        # A bounded date range keeps the response small while comfortably
        # covering the requested number of A-share trading days.
        end = local_today()
        start = date.fromordinal(max(1, end.toordinal() - max(180, limit * 3)))
        query = urllib.parse.urlencode({
            "code": symbol,
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "stat": "1",
            "order": "D",
            "period": "d",
            "rt": "json",
        })
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": "https://q.stock.sohu.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
            },
        )
        raw = self._download(request, security_id)
        if len(raw) > self.max_response_bytes:
            raise MarketDataError(f"搜狐备用行情响应超过5MB安全上限（{security_id}）。")
        try:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # The numeric K-line body is ASCII, while Sohu's trailing
                # summary labels are still encoded as GBK/GB18030.
                text = raw.decode("gb18030")
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:100].decode("gb18030", errors="replace").strip()
            raise MarketDataError(f"搜狐备用行情返回的不是有效JSON（{security_id}）：{preview or '空响应'}。") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise MarketDataError(f"搜狐备用行情JSON结构异常（{security_id}）。")
        item = payload[0]
        if item.get("status") != 0:
            raise MarketDataError(
                f"搜狐备用行情没有{symbol}的日线（status={item.get('status', '未知')}，"
                f"{str(item.get('msg') or '无说明')[:120]}）。"
            )
        lines = item.get("hq")
        if not isinstance(lines, list):
            raise MarketDataError(f"搜狐备用行情没有返回日K线列表（{symbol}）。")
        bars: list[MarketBar] = []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 9:
                continue
            try:
                bars.append(MarketBar(
                    trade_date=date.fromisoformat(str(line[0])).isoformat(),
                    open=float(str(line[1]).replace(",", "")),
                    close=float(str(line[2]).replace(",", "")),
                    high=float(str(line[6]).replace(",", "")),
                    low=float(str(line[5]).replace(",", "")),
                    volume=float(str(line[7]).replace(",", "")),
                    amount=float(str(line[8]).replace(",", "")),
                ))
            except (TypeError, ValueError):
                continue
        # Sohu returns newest first. Normalize before applying the shared gate.
        bars.sort(key=lambda bar: bar.trade_date)
        return validate_bars(bars[-limit:], f"搜狐{symbol}")

    def _download(self, request: urllib.request.Request, security_id: str) -> bytes:
        if self._requester:
            return self._requester(request, self.timeout_seconds)
        failures: list[str] = []
        for attempt in range(2):
            try:
                context = ssl.create_default_context()
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                    return response.read(self.max_response_bytes + 1)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise MarketDataError(f"搜狐备用行情HTTP {exc.code}（{security_id}）。") from exc
                failures.append(f"搜狐 Python HTTPS 第{attempt + 1}次：HTTP {exc.code}")
            except (
                urllib.error.URLError, ssl.SSLError, http.client.HTTPException,
                ConnectionError, OSError, TimeoutError,
            ) as exc:
                failures.append(f"搜狐 Python HTTPS 第{attempt + 1}次：{_transport_error_detail(exc)}")
            if attempt == 0:
                time.sleep(0.35)
        curl = windows_curl_path()
        if curl:
            command = [
                curl, "--fail", "--silent", "--show-error", "--location", "--http1.1", "--ipv4",
                "--tlsv1.2", "--tls-max", "1.2", "--proto", "=https", "--proto-redir", "=https",
                "--max-filesize", str(self.max_response_bytes), "--connect-timeout", str(min(10, self.timeout_seconds)),
                "--max-time", str(self.timeout_seconds), "--header", "Accept: application/json, text/plain, */*",
                "--header", "Accept-Encoding: identity", "--header", "Referer: https://q.stock.sohu.com/",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0", request.full_url,
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, timeout=self.timeout_seconds + 5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                )
                if completed.returncode == 0 and completed.stdout:
                    return completed.stdout
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
                failures.append(f"搜狐 Windows系统curl退出码{completed.returncode}：{detail or '无错误正文'}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"搜狐 Windows系统curl：{exc}")
        else:
            failures.append("搜狐 Windows系统curl：未找到")
        raise MarketTransportError(f"无法连接搜狐备用行情源（{security_id}）。", failures)


class ReviewedFallbackKlineProvider:
    """Exact Tencent -> exact Sohu -> reviewed ETF proxy, with full provenance."""

    name = "reviewed_public_fallback"

    def __init__(
        self,
        *,
        timeout_seconds: int = 20,
        tencent: TencentKlineProvider | None = None,
        sohu: SohuKlineProvider | None = None,
    ):
        self.tencent = tencent or TencentKlineProvider(timeout_seconds=timeout_seconds)
        self.sohu = sohu or SohuKlineProvider(timeout_seconds=timeout_seconds)
        self._source_by_security: dict[str, str] = {}
        self._activity_by_security: dict[str, str] = {}
        self._resolved_by_security: dict[str, ResolvedProxy] = {}
        self._actual_security_by_security: dict[str, str] = {}
        self._diagnostics_by_security: dict[str, list[str]] = {}

    @staticmethod
    def _failure_details(label: str, exc: MarketDataError) -> list[str]:
        nested = getattr(exc, "diagnostics", None)
        if nested:
            return [f"{label}：{item}" for item in nested]
        return [f"{label}：{exc}"]

    def fetch(self, security_id: str, limit: int = 90) -> list[MarketBar]:
        diagnostics: list[str] = []
        try:
            bars = self.tencent.fetch(security_id, limit)
            self._remember(security_id, security_id, "tencent_public", "volume", diagnostics + ["腾讯精确证券：成功"])
            return bars
        except MarketDataError as exc:
            diagnostics.extend(self._failure_details("腾讯精确证券失败", exc))

        try:
            bars = self.sohu.fetch(security_id, limit)
            self._remember(security_id, security_id, "sohu_public", "amount", diagnostics + ["搜狐精确证券：成功"])
            return bars
        except MarketDataError as exc:
            diagnostics.extend(self._failure_details("搜狐精确证券失败", exc))

        resolved = CURATED_ETF_FALLBACKS.get(security_id)
        if resolved is not None:
            try:
                bars = self.tencent.fetch(resolved.security_id, limit)
                self._remember(
                    security_id, resolved.security_id, "tencent_etf_proxy", "volume",
                    diagnostics + [f"经审核ETF代理：{resolved.proxy_code} {resolved.proxy_name}成功"],
                    resolved,
                )
                return bars
            except MarketDataError as exc:
                diagnostics.extend(self._failure_details(f"ETF代理{resolved.proxy_code}失败", exc))
        else:
            diagnostics.append("没有为该证券配置经审核的二级代理")

        self._diagnostics_by_security[security_id] = diagnostics
        tail = diagnostics[-1] if diagnostics else "未知原因"
        raise MarketFallbackError(f"所有备用行情路线均失败（{security_id}）：{tail}", diagnostics)

    def _remember(
        self,
        requested_security_id: str,
        actual_security_id: str,
        source: str,
        activity_field: str,
        diagnostics: list[str],
        resolved: ResolvedProxy | None = None,
    ) -> None:
        self._source_by_security[requested_security_id] = source
        self._activity_by_security[requested_security_id] = activity_field
        self._actual_security_by_security[requested_security_id] = actual_security_id
        self._diagnostics_by_security[requested_security_id] = diagnostics
        if resolved is not None:
            self._resolved_by_security[requested_security_id] = resolved

    def source_for(self, security_id: str) -> str:
        return self._source_by_security.get(security_id, self.name)

    def activity_field_for(self, security_id: str) -> str:
        return self._activity_by_security.get(security_id, "amount")

    def actual_security_id_for(self, security_id: str) -> str:
        return self._actual_security_by_security.get(security_id, security_id)

    def resolved_proxy_for(self, security_id: str) -> ResolvedProxy | None:
        return self._resolved_by_security.get(security_id)

    def diagnostics_for(self, security_id: str) -> list[str]:
        return list(self._diagnostics_by_security.get(security_id, []))


def validate_bars(bars: Iterable[MarketBar], label: str = "") -> list[MarketBar]:
    ordered = list(bars)
    if len(ordered) < 21:
        raise MarketDataError(f"{label}有效交易日不足21个，无法计算20日收益。")
    dates = [bar.trade_date for bar in ordered]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise MarketDataError(f"{label}交易日重复或未按升序排列。")
    for bar in ordered:
        numbers = (bar.open, bar.close, bar.high, bar.low, bar.volume, bar.amount)
        if not all(math.isfinite(value) for value in numbers):
            raise MarketDataError(f"{label}{bar.trade_date}包含非有限数字。")
        if bar.close <= 0 or bar.open <= 0 or bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise MarketDataError(f"{label}{bar.trade_date}价格字段不合理。")
        if bar.volume < 0 or bar.amount < 0:
            raise MarketDataError(f"{label}{bar.trade_date}成交字段为负数。")
    return ordered


def compute_metric(
    mapping: ProxyMapping,
    bars: list[MarketBar],
    *,
    cache_hit: bool = False,
    data_source: str | None = None,
    activity_field: str = "amount",
) -> dict[str, Any]:
    bars = validate_bars(bars, mapping.proxy_code)
    closes = [bar.close for bar in bars]

    # Twenty one closes produce twenty close-to-close observations.  Keep the
    # value in daily percentage points (not annualised) so the ranking layer can
    # explain exactly what it is penalising.
    daily_returns_20d = [
        (current / previous - 1.0) * 100.0
        for previous, current in zip(closes[-21:-1], closes[-20:])
    ]
    volatility_20d = statistics.pstdev(daily_returns_20d)
    consecutive_up_days = 0
    for previous, current in reversed(list(zip(closes[-21:-1], closes[-20:]))):
        if current <= previous:
            break
        consecutive_up_days += 1

    def ret(days: int) -> float:
        return round((closes[-1] / closes[-1 - days] - 1.0) * 100.0, 4)

    use_volume = activity_field == "volume"
    activity = [bar.volume if use_volume else bar.amount for bar in bars]
    recent_amount = activity[-1]
    prior_amounts = [value for value in activity[-6:-1] if value > 0]
    baseline = statistics.fmean(prior_amounts) if prior_amounts else 0.0
    amount_ratio = recent_amount / baseline if baseline > 0 else 0.0
    activity_label = "成交量" if use_volume else "成交额"
    if amount_ratio >= 1.35:
        volume_signal = f"明显放量（{activity_label}为5日均值的{amount_ratio:.2f}倍）"
    elif amount_ratio <= 0.70 and amount_ratio > 0:
        volume_signal = f"明显缩量（{activity_label}为5日均值的{amount_ratio:.2f}倍）"
    elif amount_ratio > 0:
        volume_signal = f"量能正常（{activity_label}为5日均值的{amount_ratio:.2f}倍）"
    else:
        volume_signal = f"{activity_label}口径不可用"

    window = closes[-20:]
    low, high = min(window), max(window)
    position = 50.0 if high == low else (closes[-1] - low) / (high - low) * 100.0
    if position >= 80:
        position_signal = f"20日高位（价格区间位置{position:.0f}%）"
    elif position <= 20:
        position_signal = f"20日低位（价格区间位置{position:.0f}%）"
    else:
        position_signal = f"20日中位（价格区间位置{position:.0f}%）"
    return {
        "universe_id": mapping.universe_id,
        "proxy_code": mapping.proxy_code,
        "proxy_name": mapping.proxy_name,
        "d1": ret(1), "d3": ret(3), "d5": ret(5), "d10": ret(10), "d20": ret(20),
        "volume_signal": volume_signal,
        "breadth_signal": "待补充（尚未请求成分股宽度）",
        "position_20d": position_signal,
        "data_source": data_source or mapping.provider,
        "is_simulated": 0,
        "data_cutoff": bars[-1].trade_date,
        "mapping_kind": mapping.mapping_kind,
        "mapping_confidence": mapping.confidence,
        "amount_ratio_5d": round(amount_ratio, 4),
        "volatility_20d": round(volatility_20d, 4),
        "consecutive_up_days": consecutive_up_days,
        "breadth_kind": "not_requested",
        "breadth_pct": None,
        "data_quality": "PRICE_ONLY",
        "cache_hit": 1 if cache_hit else 0,
        "warnings_json": json.dumps(
            (["当前备用行情源不含历史成交额，量能按成交量计算"] if use_volume else [])
            + (["行情使用经审核ETF代理，不是目标指数点位"] if mapping.mapping_kind == "ETF代理" else [])
            + ["尚未计算成分股扩散度"],
            ensure_ascii=False,
        ),
    }


MARKET_STRUCTURE_KEYS = (
    "expected_data_cutoff",
    "freshness_ok",
    "volatility_20d",
    "consecutive_up_days",
    "breadth_kind",
    "breadth_pct",
)


def _true_breadth_pct(snapshot: dict[str, Any]) -> float | None:
    """Extract a real constituent advance ratio, never a momentum proxy."""
    raw = snapshot.get("breadth_pct")
    if raw not in (None, ""):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value) and 0.0 <= value <= 100.0:
            return round(value, 4)
    text = str(snapshot.get("breadth_signal") or "")
    if "多周期动量一致性" in text or "非成分股宽度" in text:
        return None
    match = re.search(r"成分股上涨\s*([0-9]+)\s*/\s*([0-9]+)", text)
    if not match:
        return None
    advanced, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or advanced < 0 or advanced > total:
        return None
    return round(advanced / total * 100.0, 4)


def _market_structure(metric: dict[str, Any]) -> dict[str, Any]:
    return {key: metric.get(key) for key in MARKET_STRUCTURE_KEYS}


def _with_market_structure(row: dict[str, Any]) -> dict[str, Any]:
    """Inflate compatibility JSON while the database migration is pending."""
    result = dict(row)
    try:
        components = json.loads(result.get("score_components_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        components = {}
    structure = components.get("market_structure") if isinstance(components, dict) else None
    if isinstance(structure, dict):
        for key in MARKET_STRUCTURE_KEYS:
            if key in structure:
                result[key] = structure[key]
    return result


class MarketDataPipeline:
    def __init__(
        self,
        db,
        provider: EastmoneyKlineProvider | None = None,
        breadth_provider=None,
        *,
        calendar: ChinaTradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.calendar = calendar or ChinaTradingCalendar.from_db(db)
        self._clock = clock or local_now
        self.provider = provider or EastmoneyKlineProvider(
            timeout_seconds=int(float(db.setting("market_timeout_seconds", "20")))
        )
        if breadth_provider is None:
            from .sector_resolver import ConstituentBreadthProvider
            breadth_provider = ConstituentBreadthProvider(
                timeout_seconds=int(float(db.setting("market_timeout_seconds", "20")))
            )
        self.breadth_provider = breadth_provider

    def expected_data_cutoff(self) -> str:
        """Latest A-share session whose official daily close should exist."""
        return self.calendar.latest_completed_trading_day(self._clock()).isoformat()

    def coverage(self, universe_ids: Iterable[str] | None = None) -> dict[str, Any]:
        wanted = set(
            [row["index_code"] for row in self.db.rows("SELECT index_code FROM universe")]
            if universe_ids is None else universe_ids
        )
        rows = self.db.rows(
            "SELECT * FROM proxy_mappings WHERE active=1 AND confidence>=0.75 AND mapping_kind<>'待核验'"
        )
        mapped = {row["universe_id"] for row in rows if row["universe_id"] in wanted}
        exact = {row["universe_id"] for row in rows if row["universe_id"] in wanted and row["mapping_kind"] in {"精确指数", "精确板块"}}
        return {
            "total": len(wanted), "mapped": len(mapped), "exact": len(exact),
            "missing": sorted(wanted - mapped), "coverage_pct": round(len(mapped) / max(1, len(wanted)) * 100, 2),
        }

    def populate_metrics(
        self,
        task_id: str,
        universe: list[dict[str, Any]],
        *,
        require_breadth: bool,
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        expected_cutoff = self.expected_data_cutoff()
        ids = [row["index_code"] for row in universe]
        placeholders = ",".join("?" for _ in ids)
        mappings = self.db.rows(
            f"""SELECT * FROM proxy_mappings WHERE active=1 AND confidence>=0.75
                AND mapping_kind<>'待核验' AND universe_id IN ({placeholders})""", ids
        ) if ids else []
        mapping_map = {row["universe_id"]: ProxyMapping(
            row["universe_id"], row["provider"], row["security_id"], row["proxy_code"], row["proxy_name"],
            row["mapping_kind"], float(row["confidence"]), row["rationale"],
        ) for row in mappings}
        missing = [row["index_code"] for row in universe if row["index_code"] not in mapping_map]
        if missing:
            preview = "、".join(missing[:12]) + ("等" if len(missing) > 12 else "")
            raise MarketDataError(
                f"可信代理映射不足：{len(universe) - len(missing)}/{len(universe)}；缺少{preview}。"
                "未确认的方向不会用名称搜索后自动猜测。"
            )

        fetched: dict[str, tuple[list[MarketBar], bool, str, str]] = {}
        board_snapshots: dict[str, dict[str, Any]] = {}
        failures: list[tuple[str, str]] = []
        fetch_errors: dict[str, str] = {}
        for index, item in enumerate(universe, start=1):
            mapping = mapping_map[item["index_code"]]
            if mapping.provider != self.provider.name:
                failures.append((mapping.universe_id, f"不支持的数据源{mapping.provider}"))
                continue
            if require_breadth and re.fullmatch(r"90\.BK\d{4}", mapping.security_id):
                if mapping.security_id not in board_snapshots:
                    try:
                        snapshot = None
                        last_snapshot_error: Exception | None = None
                        for snapshot_attempt in range(2):
                            try:
                                snapshot = self.breadth_provider.fetch(mapping.universe_id, mapping.security_id, "")
                                break
                            except Exception as exc:
                                last_snapshot_error = exc
                                if snapshot_attempt == 0:
                                    time.sleep(.35)
                        if snapshot is None and last_snapshot_error is not None:
                            raise last_snapshot_error
                        returns = (snapshot or {}).get("returns_pct") or {}
                        if not snapshot or not snapshot.get("data_cutoff") or any(
                            key not in returns for key in ("d1", "d3", "d5", "d10", "d20")
                        ):
                            raise MarketDataError("板块成分股快照缺少多周期收益或截止日期")
                        snapshot_cutoff = str(snapshot["data_cutoff"])[:10]
                        previous_cutoff = self.calendar.previous_trading_day(
                            date.fromisoformat(expected_cutoff)
                        ).isoformat()
                        if snapshot_cutoff not in {expected_cutoff, previous_cutoff}:
                            raise MarketDataError(
                                f"{mapping.universe_id}成分股快照截止日{snapshot['data_cutoff']}，"
                                f"既不是最近已完成交易日{expected_cutoff}，也不是上一交易日{previous_cutoff}"
                            )
                        snapshot["data_cutoff"] = snapshot_cutoff
                        if _true_breadth_pct(snapshot) is None:
                            raise MarketDataError(f"{mapping.universe_id}成分股快照缺少可核验的上涨家数比例")
                        board_snapshots[mapping.security_id] = snapshot
                        self._record_fetch(
                            task_id, mapping.universe_id, mapping.security_id,
                            snapshot.get("source", "eastmoney_board_constituents"), "成分股快照成功",
                            ["板块多周期收益按当前成分股等权平均计算，并保留真实成分股扩散度"],
                        )
                    except Exception as exc:
                        error = f"板块成分股快照失败：{exc}"
                        fetch_errors[mapping.security_id] = error
                        failures.append((mapping.universe_id, error))
                        self._record_fetch(
                            task_id, mapping.universe_id, mapping.security_id,
                            "eastmoney_board_constituents", "失败", [error],
                        )
                else:
                    snapshot = board_snapshots[mapping.security_id]
                    self._record_fetch(
                        task_id, mapping.universe_id, mapping.security_id,
                        snapshot.get("source", "eastmoney_board_constituents"), "复用成分股快照",
                        ["与本任务中另一方向共享同一透明板块，未重复联网"],
                    )
                if progress:
                    progress("板块快照", index, len(universe), mapping.proxy_name)
                continue
            if mapping.security_id not in fetched:
                if mapping.security_id in fetch_errors:
                    failures.append((mapping.universe_id, fetch_errors[mapping.security_id]))
                else:
                    task_cached, task_source = self._load_task_resume_cached_bars(
                        task_id, mapping.universe_id, mapping.security_id, expected_cutoff,
                    )
                    if task_cached:
                        activity_field = (
                            "volume" if task_source in {"tencent_public", "tencent_etf_proxy"} else "amount"
                        )
                        fetched[mapping.security_id] = (task_cached, True, task_source, activity_field)
                        self._record_fetch(
                            task_id, mapping.universe_id, mapping.security_id,
                            task_source, "复用任务断点行情",
                            [
                                "该证券已在本任务较早一次运行中成功取得；"
                                "复用同任务断点，未重复联网"
                            ],
                        )
                        if progress:
                            progress("行情下载", index, len(universe), mapping.proxy_name)
                        continue
                    recent, recent_source = self._load_recent_cached_bars(
                        mapping.security_id, expected_cutoff
                    )
                    if recent:
                        activity_field = (
                            "volume" if recent_source in {"tencent_public", "tencent_etf_proxy"} else "amount"
                        )
                        fetched[mapping.security_id] = (recent, True, recent_source, activity_field)
                        self._record_fetch(
                            task_id, mapping.universe_id, mapping.security_id,
                            recent_source, "复用刚取得的行情",
                            ["同一操作流程已联网取得该证券，避免重复请求和触发行情源限流"],
                        )
                        if progress:
                            progress("行情下载", index, len(universe), mapping.proxy_name)
                        continue
                    try:
                        bars = self.provider.fetch(mapping.security_id)
                        bars = validate_bars(bars, mapping.universe_id)
                        actual_source = (
                            self.provider.source_for(mapping.security_id)
                            if hasattr(self.provider, "source_for") else mapping.provider
                        )
                        activity_field = (
                            self.provider.activity_field_for(mapping.security_id)
                            if hasattr(self.provider, "activity_field_for") else "amount"
                        )
                        self._save_bars(actual_source, mapping.security_id, bars)
                        fetched[mapping.security_id] = (bars, False, actual_source, activity_field)
                        self._record_fetch(
                            task_id, mapping.universe_id, mapping.security_id, actual_source, "联网成功",
                            self._provider_diagnostics(mapping.security_id),
                        )
                    except MarketDataError as exc:
                        cached, cache_source = self._load_best_cached_bars(mapping.security_id)
                        diagnostics = self._provider_diagnostics(mapping.security_id) or getattr(exc, "diagnostics", [])
                        if self._cache_usable(cached, expected_cutoff):
                            activity_field = "volume" if cache_source in {"tencent_public", "tencent_etf_proxy"} else "amount"
                            fetched[mapping.security_id] = (cached, True, cache_source, activity_field)
                            self._record_fetch(
                                task_id, mapping.universe_id, mapping.security_id, cache_source, "缓存回退",
                                diagnostics + [f"联网失败后使用{cache_source}的新鲜缓存"],
                            )
                        else:
                            error = str(exc)
                            fetch_errors[mapping.security_id] = error
                            failures.append((mapping.universe_id, error))
                            self._record_fetch(
                                task_id, mapping.universe_id, mapping.security_id, "public_market", "失败",
                                diagnostics or [error],
                            )
            elif mapping.security_id in fetched:
                _bars, cached, actual_source, _activity = fetched[mapping.security_id]
                self._record_fetch(
                    task_id, mapping.universe_id, mapping.security_id, actual_source,
                    "复用同证券缓存" if cached else "复用同证券下载",
                    ["与本任务中另一方向共享同一透明代理，未重复联网"],
                )
            if progress:
                progress("行情下载", index, len(universe), mapping.proxy_name)
        if failures:
            if all("行情源TLS连接被远端提前关闭" in message for _uid, message in failures):
                affected = "、".join(uid for uid, _message in failures[:8])
                raise MarketDataError(
                    f"行情连接失败（{len(failures)}项，{affected}{'等' if len(failures) > 8 else ''}）："
                    "行情源TLS连接被远端提前关闭；Python HTTPS已重试，并已尝试Windows原生网络回退。"
                )
            grouped = Counter(message for _uid, message in failures)
            if len(grouped) == 1:
                message = next(iter(grouped))
                affected = "、".join(uid for uid, _message in failures[:8])
                count_text = f"{len(failures)}项"
                raise MarketDataError(f"行情连接失败（{count_text}，{affected}{'等' if len(failures) > 8 else ''}）：{message}")
            detail = "；".join(f"{uid}:{message}" for uid, message in failures[:6])
            if len(failures) > 6:
                detail += f"；另有{len(failures)-6}项失败"
            raise MarketDataError(detail)

        cutoffs = [str(bars[-1].trade_date)[:10] for bars, _cached, _source, _activity in fetched.values()]
        cutoffs.extend(str(snapshot["data_cutoff"])[:10] for snapshot in board_snapshots.values())
        cutoff_counts = Counter(cutoffs)
        expected_day = date.fromisoformat(expected_cutoff)
        previous_cutoff = self.calendar.previous_trading_day(expected_day).isoformat()
        future_cutoffs = sorted(day for day in cutoff_counts if day > expected_cutoff)
        if future_cutoffs:
            raise MarketDataError(
                f"行情出现未来交易日{'、'.join(future_cutoffs)}，当前应截至{expected_cutoff}；"
                "禁止用未来数据生成研究。"
            )
        common_cutoff = min(cutoff_counts)
        if common_cutoff not in {expected_cutoff, previous_cutoff}:
            raise MarketDataError(
                f"行情共同截止日为{common_cutoff}，但最近已完成交易日是{expected_cutoff}、"
                f"上一交易日是{previous_cutoff}；数据落后超过一交易日，禁止生成研究。"
            )

        # CSI official daily bars are published index by index after the close.
        # During that window one series can already contain today's bar while
        # another still ends on the previous session.  Never mix those dates:
        # align every K-line series to their newest common session.  Board
        # snapshots cannot be reconstructed historically, so a mixed board
        # snapshot remains a hard failure.
        if len(cutoff_counts) > 1:
            if board_snapshots and any(
                str(snapshot["data_cutoff"])[:10] != common_cutoff
                for snapshot in board_snapshots.values()
            ):
                detail = "、".join(f"{day}:{count}项" for day, count in sorted(cutoff_counts.items()))
                raise MarketDataError(
                    f"行情交易日不统一（{detail}），且成分股快照无法回溯到共同日期；禁止混用日期。"
                )
            for security_id, (bars, cached, source, activity_field) in list(fetched.items()):
                aligned = [bar for bar in bars if str(bar.trade_date)[:10] <= common_cutoff]
                if not aligned or str(aligned[-1].trade_date)[:10] != common_cutoff:
                    raise MarketDataError(
                        f"{security_id}没有共同交易日{common_cutoff}的行情；禁止拼接或猜测。"
                    )
                fetched[security_id] = (
                    validate_bars(aligned, security_id), cached, source, activity_field,
                )

        publication_pending = common_cutoff != expected_cutoff
        publication_warning = (
            f"中证官网日线仍在分批发布：最近已完成交易日为{expected_cutoff}，"
            f"本轮全部行情统一使用上一交易日{common_cutoff}；仅供研究观察，禁止生成买入金额。"
        ) if publication_pending else ""
        if len(cutoff_counts) > 1:
            raw_detail = "、".join(f"{day}:{count}项" for day, count in sorted(cutoff_counts.items()))
            publication_warning += (
                f" 原始序列截止日曾为{raw_detail}，已统一裁齐到{common_cutoff}，没有混用不同日期。"
            )

        metrics: list[dict[str, Any]] = []
        breadth_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        for item in universe:
            mapping = mapping_map[item["index_code"]]
            if mapping.security_id in board_snapshots:
                snapshot = board_snapshots[mapping.security_id]
                returns = snapshot["returns_pct"]
                breadth_pct = _true_breadth_pct(snapshot)
                metric = {
                    "universe_id": mapping.universe_id,
                    "proxy_code": mapping.proxy_code,
                    "proxy_name": mapping.proxy_name,
                    "d1": returns["d1"], "d3": returns["d3"], "d5": returns["d5"],
                    "d10": returns["d10"], "d20": returns["d20"],
                    "volume_signal": snapshot["volume_signal"],
                    "breadth_signal": snapshot["breadth_signal"],
                    "position_20d": snapshot["position_20d"],
                    "data_source": snapshot.get("source", "eastmoney_board_constituents"),
                    "is_simulated": 0,
                    "data_cutoff": str(snapshot["data_cutoff"])[:10],
                    "mapping_kind": mapping.mapping_kind,
                    "mapping_confidence": mapping.confidence,
                    "amount_ratio_5d": 0.0,
                    "volatility_20d": None,
                    "consecutive_up_days": None,
                    "breadth_kind": "constituent_snapshot",
                    "breadth_pct": breadth_pct,
                    "expected_data_cutoff": expected_cutoff,
                    "freshness_ok": str(snapshot["data_cutoff"])[:10] == expected_cutoff,
                    "data_quality": snapshot["data_quality"],
                    "cache_hit": 0,
                    "warnings_json": json.dumps([
                        "板块多周期收益为当前成分股等权平均，不冒充不可用的板块指数K线",
                        "板块成分股快照不提供历史量比，量能字段明确标注为不可审计",
                    ], ensure_ascii=False),
                }
                metrics.append(metric)
                continue
            bars, cache_hit, actual_source, activity_field = fetched[mapping.security_id]
            effective_mapping = self._resolved_mapping(mapping, actual_source)
            metric = compute_metric(
                effective_mapping, bars, cache_hit=cache_hit, data_source=actual_source,
                activity_field=activity_field,
            )
            metric["expected_data_cutoff"] = expected_cutoff
            metric["freshness_ok"] = metric["data_cutoff"] == expected_cutoff
            if require_breadth:
                key = (mapping.universe_id, effective_mapping.security_id)
                if self.breadth_provider.supports(*key):
                    if key not in breadth_cache:
                        try:
                            breadth_cache[key] = self.breadth_provider.fetch(
                                mapping.universe_id, effective_mapping.security_id, metric["data_cutoff"]
                            )
                        except Exception as exc:
                            raise MarketDataError(
                                f"{mapping.universe_id}成分股扩散度获取失败：{exc}。本轮在调用API前停止。"
                            ) from exc
                    breadth = breadth_cache[key]
                    if not breadth:
                        raise MarketDataError(f"{mapping.universe_id}没有可用成分股扩散度；API前停止。")
                    breadth_pct = _true_breadth_pct(breadth)
                    if breadth_pct is None:
                        raise MarketDataError(
                            f"{mapping.universe_id}成分股扩散度没有结构化上涨比例；API前停止。"
                        )
                    metric["breadth_signal"] = breadth["breadth_signal"]
                    metric["breadth_kind"] = "constituent_snapshot"
                    metric["breadth_pct"] = breadth_pct
                    metric["data_quality"] = breadth["data_quality"]
                    warnings = json.loads(metric["warnings_json"])
                    warnings = [warning for warning in warnings if warning != "尚未计算成分股扩散度"]
                    metric["warnings_json"] = json.dumps(warnings, ensure_ascii=False)
                else:
                    # Five-period index returns already have dedicated d1/d3/d5/d10/d20
                    # fields.  Putting a derived momentum count in ``breadth_signal``
                    # still invited callers (and models) to read it as breadth even
                    # when the prose said otherwise.  Missing constituent evidence is
                    # now represented as missing, without a numerical substitute.
                    metric["breadth_signal"] = (
                        "缺失：未取得可核验的真实成分股扩散度；"
                        "1/3/5/10/20日指数涨跌仅保留在独立收益字段"
                    )
                    metric["breadth_kind"] = "missing_unavailable"
                    metric["breadth_pct"] = None
                    metric["data_quality"] = "DISCLOSED_PRICE_PROXY"
                    warnings = json.loads(metric["warnings_json"])
                    warnings = [warning for warning in warnings if warning != "尚未计算成分股扩散度"]
                    warnings.append("真实成分股扩散度缺失；指数多周期涨跌未被用作替代值")
                    metric["warnings_json"] = json.dumps(warnings, ensure_ascii=False)
            metrics.append(metric)
        if publication_warning:
            for metric in metrics:
                try:
                    warnings = json.loads(metric.get("warnings_json") or "[]")
                except (TypeError, json.JSONDecodeError):
                    warnings = []
                if not isinstance(warnings, list):
                    warnings = []
                warnings.append(publication_warning)
                metric["warnings_json"] = json.dumps(
                    list(dict.fromkeys(str(item) for item in warnings if str(item).strip())),
                    ensure_ascii=False,
                )
                metric["freshness_ok"] = False
        self._save_metrics(task_id, metrics)
        return [
            _with_market_structure(row)
            for row in self.db.rows(
                "SELECT * FROM sector_metrics WHERE task_id=? ORDER BY universe_id", (task_id,)
            )
        ]

    def _save_bars(self, provider: str, security_id: str, bars: list[MarketBar]):
        now = self._clock().isoformat(timespec="seconds")
        self.db.executemany(
            """INSERT INTO market_bars(provider,security_id,trade_date,open,close,high,low,volume,amount,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,security_id,trade_date) DO UPDATE SET
               open=excluded.open,close=excluded.close,high=excluded.high,low=excluded.low,
               volume=excluded.volume,amount=excluded.amount,fetched_at=excluded.fetched_at""",
            [(provider, security_id, b.trade_date, b.open, b.close, b.high, b.low, b.volume, b.amount, now) for b in bars],
        )

    def _load_cached_bars(self, provider: str, security_id: str) -> list[MarketBar]:
        rows = self.db.rows(
            """SELECT trade_date,open,close,high,low,volume,amount FROM market_bars
               WHERE provider=? AND security_id=? ORDER BY trade_date""", (provider, security_id)
        )
        return [MarketBar(**row) for row in rows]

    def _load_best_cached_bars(self, security_id: str) -> tuple[list[MarketBar], str]:
        candidates: list[tuple[str, list[MarketBar]]] = []
        for provider in ("csi_official", "eastmoney_public", "tencent_public", "sohu_public", "tencent_etf_proxy"):
            bars = self._load_cached_bars(provider, security_id)
            if bars:
                candidates.append((provider, bars))
        if not candidates:
            return [], ""
        provider, bars = max(candidates, key=lambda item: item[1][-1].trade_date)
        return bars, provider

    def _load_recent_cached_bars(
        self, security_id: str, expected_cutoff: str
    ) -> tuple[list[MarketBar], str]:
        """Reuse a current trading-session series instead of redownloading it.

        The guide asks users to run the ten-item market check before the full
        scan.  Downloading those ten official CSI series again a few minutes
        later wastes time and consumes the public site's burst allowance.
        Same-day reuse also makes a failed full scan resumable: successful
        rows survive a site throttle and the next run only downloads missing
        rows.  A pre-close weekday fetch is deliberately refreshed after
        the close unless it already includes the latest completed session.
        """
        rows = self.db.rows(
            """SELECT provider,MAX(fetched_at) AS fetched_at FROM market_bars
               WHERE security_id=? GROUP BY provider ORDER BY fetched_at DESC""",
            (security_id,),
        )
        now = self._clock()
        for row in rows:
            try:
                fetched_at = datetime.fromisoformat(str(row.get("fetched_at") or ""))
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=now.tzinfo)
                fetched_at = fetched_at.astimezone(now.tzinfo)
            except (TypeError, ValueError):
                continue
            if fetched_at.date() != now.date() or fetched_at > now:
                continue
            provider = str(row.get("provider") or "")
            bars = self._load_cached_bars(provider, security_id)
            if not self._cache_usable(bars, expected_cutoff):
                continue
            return bars, provider
        return [], ""

    def _load_task_resume_cached_bars(
        self,
        task_id: str,
        universe_id: str,
        security_id: str,
        expected_cutoff: str,
    ) -> tuple[list[MarketBar], str]:
        """Reuse one successful prefix item when the *same* task is resumed.

        CSI publishes daily series asynchronously after the close.  A task can
        therefore have a valid previous-session series even though the live
        expected cutoff has already advanced.  General cache reuse deliberately
        refreshes that series for a new task, but a failed task must preserve its
        own snapshot and must not redownload its completed prefix.  No model call
        can occur before all metrics are assembled, so this rule cannot mix new
        market data into already-paid partial research.
        """
        attempt = self.db.row(
            """SELECT provider FROM market_fetch_attempts
               WHERE task_id=? AND universe_id=? AND security_id=?
                 AND outcome IN (
                    '联网成功','缓存回退','复用刚取得的行情',
                    '复用同证券缓存','复用同证券下载','复用任务断点行情'
                 )
               ORDER BY id DESC LIMIT 1""",
            (task_id, universe_id, security_id),
        )
        provider = str((attempt or {}).get("provider") or "").strip()
        if not provider:
            return [], ""
        bars = self._load_cached_bars(provider, security_id)
        try:
            valid = validate_bars(bars, f"任务{task_id}断点缓存")
            previous_cutoff = self.calendar.previous_trading_day(
                date.fromisoformat(expected_cutoff)
            ).isoformat()
        except (MarketDataError, TypeError, ValueError):
            return [], ""
        if valid[-1].trade_date not in {expected_cutoff, previous_cutoff}:
            return [], ""
        return valid, provider

    def _resolved_mapping(self, mapping: ProxyMapping, actual_source: str) -> ProxyMapping:
        resolved = None
        if hasattr(self.provider, "resolved_proxy_for"):
            resolved = self.provider.resolved_proxy_for(mapping.security_id)
        if resolved is None and actual_source == "tencent_etf_proxy":
            resolved = CURATED_ETF_FALLBACKS.get(mapping.security_id)
        if resolved is None:
            return mapping
        return ProxyMapping(
            mapping.universe_id,
            actual_source,
            resolved.security_id,
            resolved.proxy_code,
            resolved.proxy_name,
            resolved.mapping_kind,
            min(mapping.confidence, resolved.confidence),
            resolved.rationale,
        )

    def _provider_diagnostics(self, security_id: str) -> list[str]:
        if hasattr(self.provider, "diagnostics_for"):
            return list(self.provider.diagnostics_for(security_id))
        return []

    def _record_fetch(
        self,
        task_id: str,
        universe_id: str,
        security_id: str,
        provider: str,
        outcome: str,
        diagnostics: list[str],
    ) -> None:
        detail = "；".join(dict.fromkeys(str(item)[:500] for item in diagnostics if str(item).strip()))
        self.db.execute(
            """INSERT INTO market_fetch_attempts(
                   task_id,universe_id,security_id,provider,outcome,detail,created_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                task_id, universe_id, security_id, provider, outcome, detail[:3000],
                self._clock().isoformat(timespec="seconds"),
            ),
        )

    def _cache_usable(self, bars: list[MarketBar], expected_cutoff: str) -> bool:
        try:
            valid = validate_bars(bars, "缓存")
        except MarketDataError:
            return False
        return valid[-1].trade_date == expected_cutoff

    def _save_metrics(self, task_id: str, metrics: list[dict[str, Any]]):
        for metric in metrics:
            metric["freshness_state"] = (
                "fresh" if bool(metric.get("freshness_ok")) else "stale"
            )
            try:
                components = json.loads(metric.get("score_components_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                components = {}
            if not isinstance(components, dict):
                components = {}
            components["market_structure"] = _market_structure(metric)
            metric["score_components_json"] = json.dumps(
                components, ensure_ascii=False, separators=(",", ":")
            )
        columns = [
            "universe_id", "proxy_code", "proxy_name", "d1", "d3", "d5", "d10", "d20",
            "volume_signal", "breadth_signal", "position_20d", "data_source", "is_simulated",
            "data_cutoff", "mapping_kind", "mapping_confidence", "amount_ratio_5d", "data_quality",
            "cache_hit", "warnings_json", "volatility_20d", "consecutive_up_days",
            "breadth_kind", "breadth_pct", "expected_data_cutoff", "freshness_state",
            "score_components_json",
        ]
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sector_metrics WHERE task_id=?", (task_id,))
            conn.executemany(
                f"""INSERT INTO sector_metrics(task_id,{','.join(columns)})
                     VALUES (? ,{','.join('?' for _ in columns)})""",
                [(task_id, *(metric.get(column) for column in columns)) for metric in metrics],
            )

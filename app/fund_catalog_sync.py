from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import local_now
from .catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from .fund_discovery import (
    FundDiscoveryError,
    FundDiscoveryService,
    MAX_RESPONSE_BYTES,
    _canonical_index_name,
    _share_class,
    parse_fund_profile,
)
from .platform_catalog import PlatformCatalogService


CSI_PRODUCT_ENDPOINT = (
    "https://www.csindex.com.cn/csindex-home/index-list/queryByIndexCode/{code}"
)
FIELD_SYNC_ALGORITHM_VERSION = STRICT_FIELD_SYNC_ALGORITHM_VERSION
AUTOMATIC_FIELD_SOURCE_NOTE = (
    "公开基金搜索与F10档案：精确同指数、明确C类、被动、公开显示可申购。"
)
READONLY_HTTP_FALLBACK_HOSTS = frozenset({
    "fundsuggest.eastmoney.com",
    "fundf10.eastmoney.com",
})
READONLY_RELAY_HOST = "r.jina.ai"


class FundCatalogSyncError(RuntimeError):
    pass


@dataclass
class CatalogSyncReport:
    total: int = 0
    completed: int = 0
    skipped: int = 0
    products: int = 0
    exact_c_funds: int = 0
    official_root_products: int = 0
    confirmed_no_c: int = 0
    retired_indices: int = 0
    network_failures: int = 0
    pending_review: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)


def _failure_label(exc: Exception) -> str:
    """Classify retryable transport failures without mistaking bad data for no-C."""
    text = str(exc).lower()
    network_tokens = (
        "network", "timeout", "timed out", "tls", "ssl", "eof", "connection",
        "连接", "网络", "超时", "远程主机", "temporary failure", "http 429", "http 5",
    )
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, urllib.error.URLError)) or any(
        token in text for token in network_tokens
    ):
        return "网络失败"
    return "待复核"


def _labelled_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    prefix = f"[{_failure_label(exc)}]"
    return text if text.startswith(prefix) else f"{prefix} {text}"


def _pick(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _strategy_kind(name: str, fund_type: str = "") -> str:
    text = f"{name} {fund_type}".upper()
    if "增强" in text or "量化" in text:
        return "指数增强"
    if "ETF" in text and "联接" in text:
        return "被动ETF联接"
    if "ETF" in text:
        return "场内ETF"
    if "指数" in text or "INDEX" in text:
        return "被动指数"
    return "待分类"


def _canonical_fund_family_name(value: str) -> str:
    """Return an exact legal-fund-family key, ignoring only the share suffix.

    CSI's related-product endpoint commonly publishes the master/A code while a
    public fund search returns the sibling C code.  Both shares have the same legal
    full name on the F10 profile.  Comparing that full name to CSI's associated
    product is therefore stronger evidence than trying to guess whether a short
    index name such as ``科创芯片设计`` means the same thing as the F10 wording
    ``上证科创板芯片设计主题指数``.

    This intentionally does not use substrings or fuzzy similarity.  It normalises
    Unicode/punctuation and removes only an explicit terminal A/C/E/I/R/Y share
    marker, so a merely similar fund family cannot pass.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(
        r"(?i)(?:人民币)?(?:A|C|E|I|R|Y)(?:类|份额)?$",
        "",
        text,
    )
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text).casefold()


def _extract_product_rows(payload: Any) -> list[dict[str, Any]]:
    """Find the product array while tolerating harmless CSI response wrappers."""
    code_keys = {"productCode", "product_code", "fundCode", "fund_code", "code"}
    if isinstance(payload, list):
        dictionaries = [item for item in payload if isinstance(item, dict)]
        if dictionaries and any(code_keys.intersection(item) for item in dictionaries):
            return dictionaries
        for item in payload:
            rows = _extract_product_rows(item)
            if rows:
                return rows
    elif isinstance(payload, dict):
        preferred = ("data", "records", "rows", "list", "products", "result")
        for key in preferred:
            if key in payload:
                rows = _extract_product_rows(payload[key])
                if rows:
                    return rows
        for value in payload.values():
            rows = _extract_product_rows(value)
            if rows:
                return rows
    return []


class CSIOfficialProductService:
    """Synchronise CSI's complete related-product list, without execution side effects."""

    def __init__(self, db, *, downloader: Callable[[str], bytes] | None = None):
        self.db = db
        self._fund_service = FundDiscoveryService(db)
        self._downloader = downloader or self._fund_service._download

    def _download(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self._downloader(url)
            except Exception as exc:  # individual retries are intentionally bounded
                last = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
        raise FundCatalogSyncError(f"中证官网相关产品连接失败：{last}") from last

    @staticmethod
    def _normalise_products(index_code: str, raw_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        code = str(index_code or "").strip().upper()
        products: dict[str, dict[str, str]] = {}
        for row in raw_rows:
            product_code = _pick(
                row, "productCode", "product_code", "fundCode", "fund_code", "code",
            ).upper()
            # CSI also publishes overseas exchange identifiers such as
            # ``3134 HK EQUITY`` and ``00877 TT EQUITY``.  Preserve the official
            # identifier verbatim while still rejecting control characters and
            # arbitrary markup.
            if not re.fullmatch(r"[0-9A-Z][0-9A-Z ._\-]{0,63}", product_code):
                continue
            row_index = _pick(row, "indexCode", "index_code", "trackingIndexCode").upper()
            if row_index and row_index.removesuffix(".CSI") != code:
                continue
            fund_name = _pick(row, "fundName", "fund_name", "productName", "product_name", "name")
            fund_type = _pick(row, "fundType", "fund_type", "productType", "product_type")
            products[product_code] = {
                "product_code": product_code,
                "fund_name": fund_name,
                "share_class": _share_class(fund_name),
                "strategy_kind": _strategy_kind(fund_name, fund_type),
                "asset_class": _pick(row, "assetClass", "asset_class"),
                "fund_type": fund_type,
                "coverage": _pick(row, "coverage", "coverageRatio"),
                "fund_manager": _pick(row, "fundManager", "fund_manager", "manager"),
                "inception_date": _pick(row, "inceptionDate", "inception_date", "establishDate"),
                "exchange_name": _pick(row, "exchange", "exchangeName", "exchange_name"),
                "aum": _pick(row, "aum", "fundScale", "fund_scale"),
            }
        if not products:
            raise FundCatalogSyncError("中证官网没有返回可识别的相关产品代码；旧记录未被清空。")
        return sorted(products.values(), key=lambda item: item["product_code"])

    def fetch(self, index_code: str) -> tuple[list[dict[str, str]], str]:
        code = str(index_code or "").strip().upper()
        if not re.fullmatch(r"(?:\d{6}|H\d{5})", code):
            raise FundCatalogSyncError(f"不是有效的中证指数代码：{code}")
        url = CSI_PRODUCT_ENDPOINT.format(code=code)
        try:
            payload = json.loads(self._download(url).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FundCatalogSyncError("中证官网相关产品返回的不是有效JSON。") from exc
        if isinstance(payload, dict):
            status = str(payload.get("code") or payload.get("status") or "200")
            if status not in {"0", "200", "true", "True"}:
                raise FundCatalogSyncError(
                    f"中证官网相关产品返回失败状态：{status} {_pick(payload, 'msg', 'message')}"
                )
        return self._normalise_products(code, _extract_product_rows(payload)), url

    def _store(
        self,
        index: dict[str, Any],
        products: list[dict[str, str]],
        source_url: str,
        fetched_at: str,
    ) -> int:
        index_id = int(index["id"])
        index_code = str(index.get("official_index_code") or "")
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE csi_index_products SET active=0 WHERE investable_index_id=?",
                (index_id,),
            )
            for product in products:
                conn.execute(
                    """INSERT INTO csi_index_products(
                           investable_index_id,index_code,product_code,fund_name,share_class,
                           strategy_kind,asset_class,fund_type,coverage,fund_manager,inception_date,
                           exchange_name,aum,source_url,fetched_at,active)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(investable_index_id,product_code) DO UPDATE SET
                           index_code=excluded.index_code,fund_name=excluded.fund_name,
                           share_class=excluded.share_class,strategy_kind=excluded.strategy_kind,
                           asset_class=excluded.asset_class,fund_type=excluded.fund_type,
                           coverage=excluded.coverage,fund_manager=excluded.fund_manager,
                           inception_date=excluded.inception_date,exchange_name=excluded.exchange_name,
                           aum=excluded.aum,source_url=excluded.source_url,
                           fetched_at=excluded.fetched_at,active=1""",
                    (
                        index_id, index_code, product["product_code"], product["fund_name"],
                        product["share_class"], product["strategy_kind"], product["asset_class"],
                        product["fund_type"], product["coverage"], product["fund_manager"],
                        product["inception_date"], product["exchange_name"], product["aum"],
                        source_url, fetched_at,
                    ),
                )
            conn.execute(
                """INSERT INTO csi_product_sync_status(
                       investable_index_id,state,product_count,attempted_at,last_success_at,last_error,source_url)
                   VALUES(?,'已完成',?,?,?,?,?)
                   ON CONFLICT(investable_index_id) DO UPDATE SET
                       state='已完成',product_count=excluded.product_count,
                       attempted_at=excluded.attempted_at,last_success_at=excluded.last_success_at,
                       last_error='',source_url=excluded.source_url""",
                (index_id, len(products), fetched_at, fetched_at, "", source_url),
            )
        return len(products)

    def sync_one(self, index: dict[str, Any]) -> int:
        index_id = int(index["id"])
        index_code = str(index.get("official_index_code") or "")
        attempted = local_now().isoformat(timespec="seconds")
        try:
            products, source_url = self.fetch(index_code)
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    """INSERT INTO csi_product_sync_status(
                           investable_index_id,state,product_count,attempted_at,last_error,source_url)
                       VALUES(?,'失败',0,?,?,?)
                       ON CONFLICT(investable_index_id) DO UPDATE SET
                           state='失败',attempted_at=excluded.attempted_at,
                        last_error=excluded.last_error,source_url=excluded.source_url""",
                    (index_id, attempted, _labelled_error(exc), CSI_PRODUCT_ENDPOINT.format(code=index_code)),
                )
            raise
        return self._store(index, products, source_url, attempted)

    def import_snapshot(self, path: Path | str) -> CatalogSyncReport:
        """Import a checkpointed copy of unmodified official responses."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = payload.get("indices") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            raise FundCatalogSyncError("官方产品快照缺少indices对象。")
        by_code = {
            str(row["official_index_code"]).upper(): row
            for row in PlatformCatalogService(self.db).list_indices()
        }
        report = CatalogSyncReport(total=len(by_code))
        for code, entry in entries.items():
            index = by_code.get(str(code).upper())
            if not index or not isinstance(entry, dict) or entry.get("status") != "ok":
                continue
            try:
                products = self._normalise_products(code, entry.get("products") or [])
                source_url = CSI_PRODUCT_ENDPOINT.format(code=code)
                fetched_at = str(entry.get("retrieved_at") or local_now().isoformat(timespec="seconds"))
                report.products += self._store(index, products, source_url, fetched_at)
                report.completed += 1
            except Exception as exc:
                report.errors.append(f"{code}：{exc}")
        report.skipped = report.total - report.completed
        return report

    def sync_all(
        self,
        *,
        incomplete_only: bool = False,
        force_refresh: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CatalogSyncReport:
        indices = PlatformCatalogService(self.db).catalog_sync_indices(
            include_retired_no_c=force_refresh,
        )
        report = CatalogSyncReport(total=len(indices))
        for position, index in enumerate(indices, 1):
            if stop_requested and stop_requested():
                report.stopped = True
                break
            status = self.db.row(
                """SELECT s.state,s.product_count,
                          (SELECT COUNT(*) FROM csi_index_products p
                            WHERE p.investable_index_id=s.investable_index_id AND p.active=1) AS active_count
                     FROM csi_product_sync_status s WHERE s.investable_index_id=?""",
                (index["id"],),
            )
            # A stale status row must never hide a missing/partially deleted official
            # snapshot.  CSI returns at least one associated product for every active
            # catalog entry, so zero is not a valid completed official snapshot.
            if (
                incomplete_only and not force_refresh and status and status["state"] == "已完成"
                and int(status.get("product_count") or 0) > 0
                and int(status.get("active_count") or 0) == int(status.get("product_count") or 0)
            ):
                report.skipped += 1
                if progress:
                    progress(position, report.total, f"跳过已完成：{index['official_index_name']}")
                continue
            if progress:
                progress(position, report.total, f"官网产品：{index['official_index_name']}")
            try:
                count = self.sync_one(index)
                report.products += count
                report.official_root_products += count
                report.completed += 1
            except Exception as exc:
                if _failure_label(exc) == "网络失败":
                    report.network_failures += 1
                else:
                    report.pending_review += 1
                report.errors.append(
                    f"{index.get('official_index_code')} {index.get('official_index_name')}：{exc}"
                )
        now = local_now().isoformat(timespec="seconds")
        settings = {
            "csi_product_sync_last_attempt_at": now,
            "csi_product_sync_last_attempt_status": "已停止" if report.stopped else "已完成",
            "csi_product_sync_last_error": "\n".join(report.errors[:30]),
        }
        if not report.stopped:
            settings["csi_product_sync_last_at"] = now
        self.db.save_settings(settings)
        return report


class CSIFieldFundSyncService:
    """Lightweight batch discovery for exact, public-buyable, passive C shares."""

    def __init__(
        self,
        db,
        *,
        searcher: Callable[[str], list[dict[str, Any]]] | None = None,
        profile_loader: Callable[[str], dict[str, Any]] | None = None,
        readonly_http_downloader: Callable[[str], bytes] | None = None,
        readonly_relay_downloader: Callable[[str], bytes] | None = None,
        max_workers: int = 6,
    ):
        self.db = db
        self.discovery = FundDiscoveryService(db)
        self._searcher = searcher or self._search
        self._profile_loader = profile_loader or self._load_profile
        self._readonly_http_downloader = readonly_http_downloader or self._http_get
        self._readonly_relay_downloader = readonly_relay_downloader or self._relay_get
        self.max_workers = max(1, min(8, int(max_workers)))
        self._profile_cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _validated_public_url(url: str, *, required_scheme: str = "https") -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(str(url or ""))
        if (
            parsed.scheme.lower() != required_scheme
            or (parsed.hostname or "").lower() not in READONLY_HTTP_FALLBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise FundCatalogSyncError("只读网络兜底仅允许东方财富基金搜索与基金F10固定主机。")
        return parsed

    def _http_get(self, url: str) -> bytes:
        """GET one whitelisted public page over HTTP, without credentials or writes."""
        original = self._validated_public_url(url, required_scheme="http")
        allowed_host = (original.hostname or "").lower()
        # Keep this request deliberately identical to the minimal route that works
        # on the user's Windows/VPN combination.  Additional Connection/Accept
        # headers and the system curl route are rejected by this endpoint there.
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        last_error: Exception | None = None
        raw = b""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.discovery.timeout_seconds,
                ) as response:
                    final = urllib.parse.urlparse(response.geturl())
                    if (
                        final.scheme.lower() not in {"http", "https"}
                        or (final.hostname or "").lower() != allowed_host
                    ):
                        raise FundCatalogSyncError("东方财富只读页面最终地址离开白名单，已阻止。")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except FundCatalogSyncError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        if not raw:
            raise FundCatalogSyncError(f"东方财富HTTP只读兜底失败：{last_error}") from last_error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FundCatalogSyncError("东方财富公开基金资料超过5MB安全上限。")
        return raw

    @staticmethod
    def _canonical_source_url(url: str) -> tuple[str, str, int | None, str, tuple[tuple[str, str], ...]]:
        """Return a strict, credential-free identity for a relay source URL."""
        parsed = CSIFieldFundSyncService._validated_public_url(url, required_scheme="http")
        if parsed.fragment:
            raise FundCatalogSyncError("网页转发来源地址含有不允许的片段。")
        try:
            port = parsed.port
        except ValueError as exc:
            raise FundCatalogSyncError("网页转发来源地址端口无效。") from exc
        if port not in (None, 80):
            raise FundCatalogSyncError("网页转发来源地址端口不在白名单。")
        query = tuple(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            port,
            urllib.parse.unquote(parsed.path or "/"),
            query,
        )

    @staticmethod
    def _relay_url(original_http_url: str) -> str:
        # Validate before embedding the target in the relay path.  This prevents
        # callers from turning the relay into a generic proxy.
        CSIFieldFundSyncService._canonical_source_url(original_http_url)
        return f"https://{READONLY_RELAY_HOST}/{original_http_url}"

    def _relay_get(self, relay_url: str) -> bytes:
        """GET one jina.ai rendering; no cookies, credentials or request body."""
        parsed = urllib.parse.urlparse(str(relay_url or ""))
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() != READONLY_RELAY_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not parsed.path.startswith("/http://")
        ):
            raise FundCatalogSyncError("只读网页转发地址不在固定白名单。")
        embedded_target = relay_url.split(f"https://{READONLY_RELAY_HOST}/", 1)[-1]
        self._canonical_source_url(embedded_target)
        request = urllib.request.Request(
            relay_url,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        last_error: Exception | None = None
        raw = b""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.discovery.timeout_seconds,
                ) as response:
                    final = urllib.parse.urlparse(response.geturl())
                    if (
                        final.scheme.lower() != "https"
                        or (final.hostname or "").lower() != READONLY_RELAY_HOST
                    ):
                        raise FundCatalogSyncError("只读网页转发最终地址离开白名单，已阻止。")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except FundCatalogSyncError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        if not raw:
            raise FundCatalogSyncError(f"只读网页转发失败：{last_error}") from last_error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FundCatalogSyncError("网页转发基金资料超过5MB安全上限。")
        return raw

    @classmethod
    def _extract_relay_markdown(cls, raw: bytes, expected_http_url: str) -> bytes:
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise FundCatalogSyncError("只读网页转发没有返回数据。")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FundCatalogSyncError("网页转发基金资料超过5MB安全上限。")
        try:
            document = bytes(raw).decode("utf-8", errors="strict").lstrip("\ufeff")
        except UnicodeDecodeError as exc:
            raise FundCatalogSyncError("只读网页转发返回的不是UTF-8文本。") from exc
        source_matches = re.findall(r"(?im)^URL Source:\s*(\S.*?)\s*$", document)
        content_match = re.search(r"(?im)^Markdown Content:\s*$", document)
        first_source = re.search(r"(?im)^URL Source:\s*(\S.*?)\s*$", document)
        if len(source_matches) != 1 or first_source is None or content_match is None:
            raise FundCatalogSyncError("只读网页转发缺少唯一的URL Source或Markdown Content。")
        if first_source.start() > content_match.start():
            raise FundCatalogSyncError("只读网页转发来源标记位置异常。")
        announced = source_matches[0].strip().strip("<>")
        if cls._canonical_source_url(announced) != cls._canonical_source_url(expected_http_url):
            raise FundCatalogSyncError("只读网页转发的URL Source与请求目标不一致，已拒绝。")
        content = document[content_match.end():].lstrip("\r\n")
        if not content:
            raise FundCatalogSyncError("只读网页转发的Markdown Content为空。")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise FundCatalogSyncError("网页转发基金资料超过5MB安全上限。")
        return encoded

    @staticmethod
    def _parse_markdown_profile(page: str) -> dict[str, Any]:
        """Parse the three execution-critical fields from a Markdown table."""
        wanted = {"基金全称": "full_name", "跟踪标的": "target_index", "业绩比较基准": "benchmark"}
        fields: dict[str, str] = {}
        for line in str(page or "").splitlines():
            if "|" not in line:
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not cells or all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                continue
            clean = []
            for cell in cells:
                text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"^[*_`]+|[*_`]+$", "", text).strip()
                clean.append(re.sub(r"\s+", " ", text))
            for position in range(len(clean) - 1):
                key = clean[position].rstrip("：:").strip()
                output_key = wanted.get(key)
                value = clean[position + 1].strip()
                if output_key and value and output_key not in fields:
                    fields[output_key] = value
        if not all(fields.get(key) for key in ("full_name", "target_index", "benchmark")):
            raise FundCatalogSyncError("基金F10网页转发表格缺少基金全称、跟踪标的或业绩比较基准。")
        return fields

    def _download_public_readonly_result(self, https_url: str) -> tuple[bytes, str, bool]:
        parsed = self._validated_public_url(https_url, required_scheme="https")
        try:
            return self.discovery._download(https_url), https_url, False
        except Exception as https_error:
            # No API key, cookie, form body or other credential is attached.  The
            # downgrade is a GET-only compatibility path for the two exact public
            # Eastmoney hosts and is never used for CSI or DeepSeek endpoints.
            http_url = urllib.parse.urlunparse(parsed._replace(scheme="http"))
            try:
                raw = self._readonly_http_downloader(http_url)
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    raise FundCatalogSyncError("东方财富HTTP只读兜底没有返回数据。")
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise FundCatalogSyncError("东方财富公开基金资料超过5MB安全上限。")
                return bytes(raw), http_url, False
            except Exception as http_error:
                relay_url = self._relay_url(http_url)
                try:
                    relay_raw = self._readonly_relay_downloader(relay_url)
                    content = self._extract_relay_markdown(relay_raw, http_url)
                except Exception as relay_error:
                    raise FundCatalogSyncError(
                        "东方财富HTTPS与固定主机HTTP只读连接均失败，"
                        f"只读网页转发也失败：{relay_error}"
                    ) from http_error
                return content, http_url, True

    def _download_public_readonly(self, https_url: str) -> bytes:
        return self._download_public_readonly_result(https_url)[0]

    def _search(self, query: str) -> list[dict[str, Any]]:
        url = self.discovery.search_endpoint + "?" + urllib.parse.urlencode({"m": "1", "key": query})
        try:
            payload = json.loads(self._download_public_readonly(url).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FundCatalogSyncError(f"基金搜索返回的不是有效JSON：{query}") from exc
        if payload.get("ErrCode") != 0 or not isinstance(payload.get("Datas"), list):
            raise FundCatalogSyncError(f"基金搜索返回异常：{query}")
        return payload["Datas"]

    def _load_profile(self, fund_code: str) -> dict[str, Any]:
        url = f"https://fundf10.eastmoney.com/jbgk_{fund_code}.html"
        raw, source_url, relay_markdown = self._download_public_readonly_result(url)
        page = raw.decode("utf-8", errors="replace")
        profile = self._parse_markdown_profile(page) if relay_markdown else parse_fund_profile(page)
        profile["source_url"] = source_url
        return profile

    def _profile(self, fund_code: str) -> dict[str, Any]:
        with self._cache_lock:
            cached = self._profile_cache.get(fund_code)
        if cached is not None:
            return cached
        profile = self._profile_loader(fund_code)
        if not isinstance(profile, dict):
            raise FundCatalogSyncError(f"基金{fund_code}的F10档案不是结构化对象。")
        missing = [
            label for key, label in (
                ("full_name", "基金全称"),
                ("target_index", "跟踪标的"),
                ("benchmark", "业绩比较基准"),
            )
            if not str(profile.get(key) or "").strip()
        ]
        if missing:
            raise FundCatalogSyncError(
                f"基金{fund_code}的F10档案缺少{'、'.join(missing)}；"
                "可能是验证码、空表或页面结构变化，不能据此判定无C。"
            )
        with self._cache_lock:
            self._profile_cache[fund_code] = profile
        return profile

    @staticmethod
    def _candidate_from_search(item: dict[str, Any]) -> dict[str, Any] | None:
        base = item.get("FundBaseInfo") or {}
        if isinstance(base, str):
            try:
                base = json.loads(base)
            except json.JSONDecodeError:
                return None
        if not isinstance(base, dict):
            return None
        code = str(item.get("CODE") or base.get("FCODE") or "")
        name = str(item.get("NAME") or base.get("SHORTNAME") or "").strip()
        fund_type = str(base.get("FTYPE") or "")
        # Eastmoney commonly labels feeder funds as ``ETF联接基金`` rather than
        # ``指数型-股票``.  Requiring the literal word “指数” discarded every such
        # C-share search result and produced a false “已完成 0只”.
        type_text = f"{fund_type} {name}".upper()
        if not re.fullmatch(r"\d{6}", code) or not any(
            token in type_text for token in ("指数", "ETF", "联接", "LOF")
        ):
            return None
        if str(base.get("ISBUY") or "") != "1":
            return None
        upper = name.upper()
        if "ETF" in upper and "联接" not in name:
            return None
        return {"fund_code": code, "fund_name": name, "fund_type": fund_type}

    def _verify_candidate(
        self,
        candidate: dict[str, Any],
        expected_name: str,
        official_families: frozenset[str],
    ) -> dict[str, Any] | None:
        profile = self._profile(candidate["fund_code"])
        target_index = str(profile.get("target_index") or "").strip()
        short_class = _share_class(candidate["fund_name"])
        full_name = str(profile.get("full_name") or "").strip()
        full_class = _share_class(full_name)
        classes = {value for value in (short_class, full_class) if value}
        if classes != {"C"}:
            return None
        target_exact = bool(
            target_index and _canonical_index_name(target_index) == expected_name
        )
        family_key = _canonical_fund_family_name(full_name)
        official_family_exact = bool(
            family_key and family_key in official_families
        )
        if not target_exact and not official_family_exact:
            return None
        combined = f"{candidate['fund_name']} {full_name} {candidate['fund_type']} {profile.get('benchmark') or ''}"
        upper = combined.upper()
        if any(word in combined for word in ("增强", "量化", "主动")) or "QDII" in upper:
            return None
        if "ETF" in upper and "联接" not in combined:
            return None
        strategy = "被动ETF联接" if "ETF" in upper and "联接" in combined else "被动指数"
        return {
            "fund_code": candidate["fund_code"],
            "fund_name": full_name,
            "share_class": "C",
            "strategy_kind": strategy,
            "target_index": target_index,
            "source_url": str(profile.get("source_url") or f"https://fundf10.eastmoney.com/jbgk_{candidate['fund_code']}.html"),
        }

    def discover_one(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        index_id = int(index["id"])
        queries: list[str] = []
        official_source_count = 0
        for value in (
            index.get("official_index_name"), index.get("official_index_code"),
            index.get("research_name"),
        ):
            value = str(value or "").strip()
            if value and value not in queries:
                queries.append(value)
        official_families: set[str] = set()
        for product in self.db.rows(
            """SELECT fund_name,strategy_kind FROM csi_index_products
                 WHERE investable_index_id=? AND active=1 AND fund_name<>''
                 ORDER BY product_code""",
            (index_id,),
        ):
            official_source_count += 1
            value = str(product["fund_name"] or "").strip()
            family_key = _canonical_fund_family_name(value)
            if family_key:
                official_families.add(family_key)
            if value and value not in queries:
                queries.append(value)
            # CSI's official association often lists only the exchange ETF root.
            # Search that exact official name for its feeder C share, then still
            # require the fund F10 page to prove the precise target index.  This
            # broadens discovery without ever guessing a relationship.
            if value and str(product.get("strategy_kind") or "") == "场内ETF":
                for derived in (f"{value} 联接 C", f"{value} ETF联接 C"):
                    if derived not in queries:
                        queries.append(derived)
        for derived in (
            f"{index.get('official_index_name') or ''} ETF联接 C",
            f"{index.get('official_index_name') or ''} 指数 C",
            f"{index.get('official_index_code') or ''} ETF联接 C",
        ):
            derived = derived.strip()
            if derived and derived not in queries:
                queries.append(derived)

        discovered: dict[str, dict[str, Any]] = {}
        search_errors: list[str] = []
        raw_result_count = 0
        def run_search(query: str):
            try:
                return query, self._searcher(query), None
            except Exception as exc:
                return query, [], exc

        with ThreadPoolExecutor(max_workers=min(4, self.max_workers)) as pool:
            results = list(pool.map(run_search, queries))
        for query, rows, error in results:
            if error is not None:
                search_errors.append(f"{query}: {error}")
                continue
            raw_result_count += len(rows)
            for item in rows:
                candidate = self._candidate_from_search(item)
                if candidate:
                    discovered.setdefault(candidate["fund_code"], candidate)
        # Every official product name is a separate discovery route.  Silently
        # accepting a partial set would mark the index complete and make the next
        # "续跑" skip a C share that happened to be behind the failed query.
        if search_errors:
            raise FundCatalogSyncError("；".join(search_errors[:3]))
        if raw_result_count == 0:
            raise FundCatalogSyncError(
                f"公开基金搜索返回0条：查询{len(queries)}次，官网可用于发现的联接/指数/LOF根产品"
                f"{official_source_count}只；未标记完成，请检查搜索通道。"
            )
        if not discovered:
            raise FundCatalogSyncError(
                f"公开基金搜索返回{raw_result_count}条，但0条通过场外指数类型与公开可申购初筛；"
                f"查询{len(queries)}次、官网根产品{official_source_count}只。"
            )

        expected = _canonical_index_name(str(index.get("official_index_name") or ""))
        exact: list[dict[str, Any]] = []
        profile_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(
                    self._verify_candidate,
                    row,
                    expected,
                    frozenset(official_families),
                ): code
                for code, row in discovered.items()
            }
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:
                    profile_errors.append(f"{futures[future]}: {exc}")
                    continue
                if row:
                    exact.append(row)
        # The same completeness rule applies to F10 profiles: a failed profile may
        # be the one exact C share, so this index remains retryable and its previous
        # good mappings stay untouched.
        if profile_errors:
            raise FundCatalogSyncError("基金档案部分失败：" + "；".join(profile_errors[:5]))
        if not exact:
            official_status = self.db.row(
                """SELECT s.state,s.product_count,
                          (SELECT COUNT(*) FROM csi_index_products p
                            WHERE p.investable_index_id=s.investable_index_id
                              AND p.active=1) AS active_count
                     FROM csi_product_sync_status s
                    WHERE s.investable_index_id=?""",
                (index_id,),
            )
            official_complete = bool(
                official_status
                and official_status.get("state") == "已完成"
                and int(official_status.get("product_count") or 0) > 0
                and int(official_status.get("active_count") or 0)
                    == int(official_status.get("product_count") or 0)
            )
            if not official_complete:
                raise FundCatalogSyncError(
                    "中证官网关联产品快照未完整成功，不能把本次0只严格C写成无C结论。"
                )
            # Every search and every candidate profile completed successfully.
            # This is materially different from a network/parser failure: it is a
            # source-backed conclusion that no currently public-buyable exact
            # passive C share was found.
            return []
        return sorted(exact, key=lambda row: row["fund_code"])

    def sync_one(self, index: dict[str, Any]) -> int:
        attempted = local_now().isoformat(timespec="seconds")
        index_id = int(index["id"])
        try:
            funds = self.discover_one(index)
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    """INSERT INTO csi_fund_sync_status(
                           investable_index_id,state,exact_c_count,attempted_at,last_error,
                           algorithm_version)
                       VALUES(?,'失败',0,?,?,?)
                       ON CONFLICT(investable_index_id) DO UPDATE SET
                            state='失败',attempted_at=excluded.attempted_at,
                            last_error=excluded.last_error,
                            algorithm_version=excluded.algorithm_version""",
                    (index_id, attempted, _labelled_error(exc), FIELD_SYNC_ALGORITHM_VERSION),
                )
            raise

        with self.db.connect() as conn:
            # A fully successful discovery replaces only rows previously created by
            # this automatic field sync.  User-confirmed and manually curated rows
            # are deliberately preserved.  Failure paths return before this point.
            conn.execute(
                """UPDATE investable_funds SET active=0,radar_eligible=0
                     WHERE investable_index_id=? AND source_note=?""",
                (index_id, AUTOMATIC_FIELD_SOURCE_NOTE),
            )
            for rank, fund in enumerate(funds, 1):
                conn.execute(
                    """INSERT INTO fund_mappings(
                           universe_id,index_code,fund_code,fund_name,share_class,
                           buyable_status,source_url,verified_at)
                       VALUES(?,?,?,?,?,'公开可申购',?,?)
                       ON CONFLICT(universe_id,fund_code) DO UPDATE SET
                           index_code=excluded.index_code,fund_name=excluded.fund_name,
                           share_class='C',buyable_status='公开可申购',
                           source_url=excluded.source_url,verified_at=excluded.verified_at""",
                    (
                        index["universe_id"], index["official_index_code"], fund["fund_code"],
                        fund["fund_name"], "C", fund["source_url"], attempted,
                    ),
                )
                conn.execute(
                    """INSERT INTO investable_funds(
                           investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                           public_buyable_status,ant_buyable_status,quality_rank,source_url,
                           source_note,verified_at,active,passive_verified,platform_evidence_url,
                           platform_checked_at,radar_eligible,relation_kind,verification_algorithm)
                        VALUES(?,?,?,?,?,'公开渠道可申购','不作为CSI准入条件',?,?,?, ?,1,1,?,?,0,'同一指数',?)
                       ON CONFLICT(investable_index_id,fund_code) DO UPDATE SET
                           fund_name=excluded.fund_name,strategy_kind=excluded.strategy_kind,
                           share_class='C',public_buyable_status='公开渠道可申购',
                           ant_buyable_status='不作为CSI准入条件',quality_rank=excluded.quality_rank,
                           source_url=excluded.source_url,source_note=excluded.source_note,
                           verified_at=excluded.verified_at,active=1,passive_verified=1,
                           platform_evidence_url=excluded.platform_evidence_url,
                           platform_checked_at=excluded.platform_checked_at,
                            radar_eligible=0,relation_kind='同一指数',
                            verification_algorithm=excluded.verification_algorithm""",
                    (
                        index_id, fund["fund_code"], fund["fund_name"], fund["strategy_kind"], "C",
                        rank, fund["source_url"],
                        AUTOMATIC_FIELD_SOURCE_NOTE,
                        attempted, fund["source_url"], attempted,
                        FIELD_SYNC_ALGORITHM_VERSION,
                    ),
                )
            count = conn.execute(
                """SELECT COUNT(*) FROM investable_funds
                     WHERE investable_index_id=? AND active=1 AND relation_kind='同一指数'
                       AND UPPER(share_class)='C' AND passive_verified=1
                        AND strategy_kind IN ('被动ETF联接','被动指数')
                        AND verification_algorithm=?
                        AND public_buyable_status IN
                             ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')""",
                (index_id, FIELD_SYNC_ALGORITHM_VERSION),
            ).fetchone()[0]
            state = "已完成" if int(count) > 0 else "已确认无C"
            conclusion = "" if int(count) > 0 else (
                "[核验结论] 所有公开搜索与候选F10档案均已成功复核，"
                "未发现当前公开可申购、精确同指数、场外被动的C类份额；官网根产品仍完整保留。"
            )
            conn.execute(
                """INSERT INTO csi_fund_sync_status(
                       investable_index_id,state,exact_c_count,attempted_at,last_success_at,last_error,
                       algorithm_version)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(investable_index_id) DO UPDATE SET
                        state=excluded.state,exact_c_count=excluded.exact_c_count,
                        attempted_at=excluded.attempted_at,last_success_at=excluded.last_success_at,
                        last_error=excluded.last_error,
                        algorithm_version=excluded.algorithm_version""",
                (
                    index_id, state, int(count), attempted, attempted, conclusion,
                    FIELD_SYNC_ALGORITHM_VERSION,
                ),
            )
        catalog = PlatformCatalogService(self.db)
        if funds and int(index.get("active") or 0) != 1:
            catalog.restore_after_strict_c(index_id)
        catalog.refresh_admission(index_id)
        # A verified mapping is also an executable product identity.  Link it to
        # the normal fund library immediately so an AI recommendation can enter a
        # draft without asking the user to retype the same fund by hand.
        self.db.link_strict_funds_to_library(index_id)
        # Do not archive per item.  A network failure later in the same batch must
        # leave every no-C conclusion resumable and reviewable.  ``sync_all`` archives
        # only after the whole v5 audit has completed without any error.
        return len(funds)

    def sync_all(
        self,
        *,
        incomplete_only: bool = False,
        force_refresh: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CatalogSyncReport:
        indices = PlatformCatalogService(self.db).catalog_sync_indices(
            include_retired_no_c=force_refresh,
        )
        report = CatalogSyncReport(total=len(indices))
        # v5 validates every F10 critical field.  Older positive and negative rows
        # must both be re-audited because a HTTP-200 captcha/empty table could have
        # been mistaken for a valid profile.  Per-row algorithm_version makes an
        # interrupted v5 pass resumable without trusting older conclusions.
        effective_incomplete_only = bool(incomplete_only and not force_refresh)
        for position, index in enumerate(indices, 1):
            if stop_requested and stop_requested():
                report.stopped = True
                break
            status = self.db.row(
                "SELECT state,algorithm_version FROM csi_fund_sync_status WHERE investable_index_id=?",
                (index["id"],),
            )
            if (
                effective_incomplete_only and status
                and status.get("algorithm_version") == FIELD_SYNC_ALGORITHM_VERSION
                and status["state"] in {"已完成", "已确认无C"}
            ):
                report.skipped += 1
                if progress:
                    progress(position, report.total, f"跳过已完成：{index['official_index_name']}")
                continue
            if progress:
                progress(position, report.total, f"场外C类：{index['official_index_name']}")
            try:
                report.exact_c_funds += self.sync_one(index)
                report.completed += 1
                final = self.db.row(
                    "SELECT state FROM csi_fund_sync_status WHERE investable_index_id=?",
                    (index["id"],),
                )
                if final and final["state"] == "已确认无C":
                    report.confirmed_no_c += 1
            except Exception as exc:
                if _failure_label(exc) == "网络失败":
                    report.network_failures += 1
                else:
                    report.pending_review += 1
                report.errors.append(
                    f"{index.get('official_index_code')} {index.get('official_index_name')}：{exc}"
                )
        now = local_now().isoformat(timespec="seconds")
        settings = {
            "csi_fund_sync_last_attempt_at": now,
            "csi_fund_sync_last_attempt_status": "已停止" if report.stopped else "已完成",
            "csi_fund_sync_last_error": "\n".join(report.errors[:30]),
        }
        if not report.stopped:
            settings["csi_fund_sync_last_at"] = now
        if not report.stopped and not report.errors and report.completed + report.skipped == report.total:
            settings["csi_fund_sync_algorithm_version"] = FIELD_SYNC_ALGORITHM_VERSION
            catalog = PlatformCatalogService(self.db)
            report.retired_indices += catalog.retire_confirmed_no_c(
                required_algorithm=FIELD_SYNC_ALGORITHM_VERSION,
            )
            settings["csi_daily_scan_count"] = str(len(catalog.radar_universe()))
        self.db.save_settings(settings)
        return report

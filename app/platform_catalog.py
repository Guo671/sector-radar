from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from .config import local_now
from .catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION


CSI_CODE_RE = re.compile(r"^((?:\d{6}|H\d{5}))\.CSI$", re.IGNORECASE)
PASSIVE_KINDS = {"被动ETF联接", "被动指数"}
PUBLIC_BUYABLE_STATUSES = {
    "公开渠道可申购", "已核对可申购", "用户已实际买入", "公开资料显示可申购",
}
SUSPENDED_BUYABLE_MARKERS = ("暂停申购", "暂停大额申购", "不可申购", "封闭")
DEFAULT_EXECUTION_TTL_HOURS = 24.0
DEFAULT_EXECUTION_REFRESH_WORKERS = 6
MAX_EXECUTION_REFRESH_WORKERS = 8


@dataclass(frozen=True)
class AdmissionCheck:
    admitted: bool
    reasons: tuple[str, ...]

    @property
    def status(self) -> str:
        return "已准入雷达" if self.admitted else "候选·" + "；".join(self.reasons[:2])


@dataclass(frozen=True)
class FundExecutionCheck:
    executable: bool
    state: str
    reasons: tuple[str, ...]
    checked_at: str
    expires_at: str

    def require(self) -> None:
        if not self.executable:
            raise ValueError("基金当前不可执行：" + "；".join(self.reasons or (self.state,)))


class PlatformCatalogService:
    """Source of truth for the layered CSI industry universe.

    The class name is kept for database and UI compatibility.  It no longer means an
    Alipay catalog: a new scan sees only official ``XXXXXX.CSI`` or ``HXXXXX.CSI`` rows.
    Research admission requires official identity and trustworthy price history.
    Built-in rows whose official history audit failed remain visible as research
    candidates, but cannot enter the daily scan or consume API budget.
    """

    def __init__(self, db):
        self.db = db

    def list_indices(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        where = "1=1" if include_inactive else "i.active=1"
        rows = self.db.rows(
            f"""SELECT i.*,u.index_name AS research_name,u.category AS historical_category,
                       p.proxy_code,p.proxy_name,p.security_id,p.mapping_kind,
                       p.confidence AS proxy_confidence,COALESCE(p.active,0) AS proxy_active,
                       (SELECT COUNT(*) FROM investable_funds f
                         WHERE f.investable_index_id=i.id AND f.active=1) AS fund_total,
                       (SELECT COUNT(*) FROM investable_funds f
                         WHERE f.investable_index_id=i.id AND f.active=1
                           AND f.passive_verified=1
                           AND f.relation_kind='同一指数'
                           AND UPPER(f.share_class)='C'
                           AND f.strategy_kind IN ('被动ETF联接','被动指数')) AS passive_total,
                       (SELECT COUNT(*) FROM investable_funds f
                         WHERE f.investable_index_id=i.id AND f.active=1
                           AND f.radar_eligible=1) AS eligible_fund_total
                       ,(SELECT COUNT(*) FROM csi_index_products p2
                         WHERE p2.investable_index_id=i.id AND p2.active=1) AS official_product_total
                       ,COALESCE((SELECT s.state FROM csi_product_sync_status s
                         WHERE s.investable_index_id=i.id),'待同步') AS product_sync_state
                        ,COALESCE((SELECT s.state FROM csi_fund_sync_status s
                          WHERE s.investable_index_id=i.id),'待同步') AS fund_sync_state
                        ,COALESCE((SELECT s.attempted_at FROM csi_product_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS product_sync_attempted_at
                        ,COALESCE((SELECT s.last_success_at FROM csi_product_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS product_sync_success_at
                        ,COALESCE((SELECT s.last_error FROM csi_product_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS product_sync_detail
                        ,COALESCE((SELECT s.attempted_at FROM csi_fund_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS fund_sync_attempted_at
                        ,COALESCE((SELECT s.last_success_at FROM csi_fund_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS fund_sync_success_at
                        ,COALESCE((SELECT s.last_error FROM csi_fund_sync_status s
                          WHERE s.investable_index_id=i.id),'') AS fund_sync_detail
                   FROM investable_indices i
                  JOIN universe u ON u.index_code=i.universe_id
                  LEFT JOIN proxy_mappings p ON p.universe_id=i.universe_id
                 WHERE {where}
                  ORDER BY i.radar_enabled DESC,i.display_order,i.id"""
        )
        funds_by_index = self._funds_by_index([int(row["id"]) for row in rows])
        ttl_hours = self._execution_ttl_hours()
        for row in rows:
            product_count = int(row.get("official_product_total") or 0)
            fund_state = str(row.get("fund_sync_state") or "待同步")
            detail = str(row.get("fund_sync_detail") or "")
            row["official_root_state"] = "官方根产品已收录" if product_count else (
                "网络失败" if str(row.get("product_sync_detail") or "").startswith("[网络失败]")
                else "待复核" if str(row.get("product_sync_state") or "") == "失败"
                else "待同步"
            )
            row_funds = funds_by_index.get(int(row["id"]), [])
            fresh_eligible = sum(
                1 for fund in row_funds
                if self.evaluate_fund_execution(
                    fund, intended_amount=1, ttl_hours=ttl_hours,
                    require_same_day=True,
                ).executable
            )
            row["strict_catalog_fund_total"] = sum(
                1 for fund in row_funds if self.fund_has_strict_catalog_eligibility(fund)
            )
            row["fresh_eligible_fund_total"] = fresh_eligible
            if fund_state == "已完成":
                field_state = "已收录可执行C" if fresh_eligible else "C类待TTL复核"
            elif fund_state == "已确认无C":
                field_state = "已确认无C"
            elif fund_state == "失败" and detail.startswith("[网络失败]"):
                field_state = "网络失败"
            elif fund_state == "失败":
                field_state = "待复核"
            else:
                field_state = "官方根产品" if product_count else row["official_root_state"]
            row["fund_collection_state"] = field_state
            row["collection_state"] = field_state
        return rows

    def catalog_sync_indices(self, *, include_retired_no_c: bool = False) -> list[dict[str, Any]]:
        """Return the current catalog plus explicitly retired no-C rows for re-audit."""
        current = self.list_indices()
        if not include_retired_no_c:
            return current
        current_ids = {int(row["id"]) for row in current}
        retired_ids = {
            int(row["id"])
            for row in self.db.rows(
                """SELECT i.id FROM investable_indices i
                     JOIN csi_fund_sync_status s ON s.investable_index_id=i.id
                    WHERE i.active=0 AND s.state='已确认无C'
                      AND i.exclusion_reason LIKE '%没有严格同指数%场外基金%'"""
            )
        }
        historical = {
            int(row["id"]): row for row in self.list_indices(include_inactive=True)
            if int(row["id"]) in retired_ids
        }
        return current + [historical[key] for key in sorted(historical) if key not in current_ids]

    def restore_after_strict_c(self, index_id: int) -> None:
        """Restore a previously no-C row only after v5 has found a strict C share."""
        row = self.index(index_id)
        if not row:
            raise ValueError("找不到待恢复的CSI目录条目。")
        uid = str(row.get("universe_id") or "")
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE investable_indices
                      SET active=1,catalog_role='日扫代表',radar_enabled=0,
                          admission_status='候选·新C类已发现',exclusion_reason='等待全局准入复核'
                    WHERE id=?""",
                (int(index_id),),
            )
            conn.execute("UPDATE proxy_mappings SET active=1 WHERE universe_id=?", (uid,))
            conn.execute(
                """UPDATE universe SET status='CSI代表·等待严格准入复核'
                     WHERE index_code=?""",
                (uid,),
            )

    def index(self, index_id: int) -> dict[str, Any] | None:
        return self.db.row(
            """SELECT i.*,u.index_name AS research_name,u.category AS historical_category,
                      p.proxy_code,p.proxy_name,p.security_id,p.mapping_kind,
                      p.confidence AS proxy_confidence,COALESCE(p.active,0) AS proxy_active
                 FROM investable_indices i JOIN universe u ON u.index_code=i.universe_id
                 LEFT JOIN proxy_mappings p ON p.universe_id=i.universe_id
                WHERE i.id=?""",
            (int(index_id),),
        )

    def funds(self, index_id: int, eligible_only: bool = False) -> list[dict[str, Any]]:
        extra = " AND radar_eligible=1" if eligible_only else ""
        return self.db.rows(
            f"""SELECT * FROM investable_funds
                  WHERE investable_index_id=? AND active=1{extra}
                  ORDER BY CASE strategy_kind
                           WHEN '被动ETF联接' THEN 0 WHEN '被动指数' THEN 1 ELSE 2 END,
                           quality_rank,fund_code""",
            (int(index_id),),
        )

    def _funds_by_index(
        self, index_ids: list[int] | tuple[int, ...] | None = None, *, eligible_only: bool = False,
    ) -> dict[int, list[dict[str, Any]]]:
        """Load funds for many indices in one connection instead of N+1 queries."""
        ids = None if index_ids is None else sorted({int(value) for value in index_ids})
        if ids == []:
            return {}
        where = ["active=1"]
        params: list[Any] = []
        if eligible_only:
            where.append("radar_eligible=1")
        if ids is not None:
            where.append(f"investable_index_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        rows = self.db.rows(
            f"""SELECT * FROM investable_funds
                   WHERE {' AND '.join(where)}
                   ORDER BY investable_index_id,
                            CASE strategy_kind
                              WHEN '被动ETF联接' THEN 0 WHEN '被动指数' THEN 1 ELSE 2 END,
                            quality_rank,fund_code""",
            params,
        )
        result: dict[int, list[dict[str, Any]]] = {}
        for fund in rows:
            result.setdefault(int(fund["investable_index_id"]), []).append(fund)
        return result

    @staticmethod
    def fund_has_strict_catalog_eligibility(fund: dict[str, Any]) -> bool:
        """Return whether a fund is strong enough to keep an index in the radar.

        Catalog eligibility is deliberately different from *order-time* execution.
        The former is durable source evidence (exact same index, passive, C share,
        and a published buyable/structured-limit state); the latter additionally
        requires a fresh TTL check on the day the user actually places an order.
        Mixing those concepts made the whole catalog disappear every 24 hours.
        """
        if int(fund.get("active") or 0) != 1:
            return False
        if str(fund.get("relation_kind") or "").strip() != "同一指数":
            return False
        if str(fund.get("share_class") or "").strip().upper() != "C":
            return False
        if str(fund.get("strategy_kind") or "").strip() not in PASSIVE_KINDS:
            return False
        if int(fund.get("passive_verified") or 0) != 1:
            return False
        if str(fund.get("verification_algorithm") or "") != STRICT_FIELD_SYNC_ALGORITHM_VERSION:
            return False
        buyable = str(fund.get("public_buyable_status") or "").strip()
        if buyable in PUBLIC_BUYABLE_STATUSES:
            return True
        # A positive, structured purchase limit is still genuinely buyable.  It is
        # checked against the intended amount again at order time.
        match = re.fullmatch(
            r"限购(?:≤|不超过|上限)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", buyable,
        )
        return bool(match and float(match.group(1)) > 0)

    def strict_catalog_funds(self, index_id: int) -> list[dict[str, Any]]:
        """Return durable exact/passive/C mappings, independent of volatile TTL."""
        return self.strict_catalog_funds_by_index([int(index_id)]).get(int(index_id), [])

    def strict_catalog_funds_by_index(
        self, index_ids: list[int] | tuple[int, ...] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        """Return all durable strict-C mappings with one database round trip."""
        grouped = self._funds_by_index(index_ids)
        return {
            index_id: [fund for fund in funds if self.fund_has_strict_catalog_eligibility(fund)]
            for index_id, funds in grouped.items()
        }

    def strict_catalog_index_ids(
        self, index_ids: list[int] | tuple[int, ...] | None = None,
    ) -> set[int]:
        return {
            index_id for index_id, funds in self.strict_catalog_funds_by_index(index_ids).items()
            if funds
        }

    def official_products(self, index_id: int) -> list[dict[str, Any]]:
        """Return every currently published CSI association, including non-C products."""
        return self.db.rows(
            """SELECT * FROM csi_index_products
                 WHERE investable_index_id=? AND active=1
                 ORDER BY CASE strategy_kind
                          WHEN '被动ETF联接' THEN 0 WHEN '被动指数' THEN 1
                          WHEN '场内ETF' THEN 2 WHEN '指数增强' THEN 3 ELSE 4 END,
                          product_code""",
            (int(index_id),),
        )

    def check(self, index_id: int) -> AdmissionCheck:
        row = self.index(index_id)
        if not row:
            return AdmissionCheck(False, ("找不到目录条目",))
        return self._check_index_row(row)

    @staticmethod
    def _check_index_row(row: dict[str, Any]) -> AdmissionCheck:
        reasons: list[str] = []
        display_code = str(row.get("ant_index_code") or "").strip().upper()
        match = CSI_CODE_RE.fullmatch(display_code)
        if not match:
            reasons.append("代码不是中证代码.CSI")
        official_code = str(row.get("official_index_code") or "").strip()
        if match and official_code != match.group(1):
            reasons.append("CSI代码与官方代码不一致")
        if not str(row.get("official_index_name") or "").strip():
            reasons.append("缺少中证官方名称")
        official_url = str(row.get("official_source_url") or "").strip().lower()
        if "csindex.com.cn" not in official_url:
            reasons.append("缺少中证官方资料")
        if str(row.get("catalog_role") or "") != "日扫代表":
            reasons.append("不是当前日扫代表")
        if str(row.get("quote_history_status") or "") != "已核验至少20个交易日":
            reasons.append("行情历史不足20日")
        if (
            not str(row.get("security_id") or "").strip()
            or str(row.get("mapping_kind") or "") != "精确指数"
            or float(row.get("proxy_confidence") or 0) < 0.95
            or int(row.get("proxy_active") or 0) != 1
        ):
            reasons.append("缺少可信精确行情或行情映射已停用")
        return AdmissionCheck(not reasons, tuple(reasons))

    def _execution_ttl_hours(self, override: float | None = None) -> float:
        if override is not None:
            return max(1.0, float(override))
        try:
            return max(1.0, float(self.db.setting(
                "fund_execution_ttl_hours", str(DEFAULT_EXECUTION_TTL_HOURS),
            )))
        except (TypeError, ValueError):
            return DEFAULT_EXECUTION_TTL_HOURS

    def _execution_refresh_workers(self) -> int:
        """Return a deliberately small public-network concurrency limit."""
        try:
            configured = int(float(self.db.setting(
                "fund_execution_refresh_workers",
                str(DEFAULT_EXECUTION_REFRESH_WORKERS),
            )))
        except (TypeError, ValueError):
            configured = DEFAULT_EXECUTION_REFRESH_WORKERS
        return max(1, min(MAX_EXECUTION_REFRESH_WORKERS, configured))

    @staticmethod
    def _checked_datetime(fund: dict[str, Any]) -> datetime | None:
        parsed: list[datetime] = []
        for key in ("platform_checked_at", "verified_at"):
            raw = str(fund.get(key) or "").strip()
            if not raw:
                continue
            try:
                parsed.append(datetime.fromisoformat(raw))
            except ValueError:
                continue
        return max(parsed) if parsed else None

    def evaluate_fund_execution(
        self,
        fund: dict[str, Any],
        *,
        intended_amount: float = 0,
        as_of: datetime | None = None,
        ttl_hours: float | None = None,
        require_same_day: bool = False,
    ) -> FundExecutionCheck:
        reasons: list[str] = []
        if str(fund.get("relation_kind") or "").strip() != "同一指数":
            reasons.append("未核实为精确同一指数")
        if str(fund.get("share_class") or "").strip().upper() != "C":
            reasons.append("不是C类份额")
        if str(fund.get("strategy_kind") or "") not in PASSIVE_KINDS:
            reasons.append("不是场外被动指数或ETF联接")
        if int(fund.get("passive_verified") or 0) != 1:
            reasons.append("被动跟踪关系尚未核实")
        buyable = str(fund.get("public_buyable_status") or "").strip()
        if any(marker in buyable for marker in SUSPENDED_BUYABLE_MARKERS):
            reasons.append(f"公开状态为{buyable}")
        elif "限购" in buyable:
            limit_match = re.search(r"限购(?:≤|不超过|上限)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", buyable)
            limit = float(limit_match.group(1)) if limit_match else 0.0
            if limit <= 0:
                reasons.append("限购额度没有结构化记录，需按本次拟买金额人工核对")
            elif float(intended_amount or 0) <= 0:
                reasons.append(f"当前限购{limit:g}元，需传入本次拟买金额复核")
            elif float(intended_amount) > limit:
                reasons.append(f"本次拟买{float(intended_amount):g}元超过当前限购{limit:g}元")
        elif buyable not in PUBLIC_BUYABLE_STATUSES:
            reasons.append("公开可申购状态尚未核实")

        checked = self._checked_datetime(fund)
        now = as_of or local_now()
        if checked and checked.tzinfo is not None and now.tzinfo is None:
            checked = checked.replace(tzinfo=None)
        elif checked and checked.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        expires = checked + timedelta(hours=self._execution_ttl_hours(ttl_hours)) if checked else None
        if not checked:
            reasons.append("没有可申购复核时间")
        elif expires is not None and now > expires:
            reasons.append("可申购复核已过期，需轻量联网重查")
        elif require_same_day and checked.date() != now.date():
            reasons.append("不是当日可申购复核，收盘入账前必须轻量重查")

        if reasons:
            if any("限购" in reason for reason in reasons):
                state = "限购待核对"
            elif any("暂停" in reason or "不可申购" in reason for reason in reasons):
                state = "暂停申购"
            elif any("过期" in reason or "复核时间" in reason for reason in reasons):
                state = "TTL已过期"
            else:
                state = "不可执行"
        else:
            state = "可执行"
        return FundExecutionCheck(
            executable=not reasons,
            state=state,
            reasons=tuple(reasons),
            checked_at=checked.isoformat(timespec="seconds") if checked else "",
            expires_at=expires.isoformat(timespec="seconds") if expires else "",
        )

    def _fund_is_executable(self, fund: dict[str, Any]) -> bool:
        return self.evaluate_fund_execution(fund).executable

    def check_fund_execution(
        self,
        index_id: int,
        fund_code: str,
        *,
        intended_amount: float = 0,
        as_of: datetime | None = None,
        require_same_day: bool = False,
    ) -> FundExecutionCheck:
        fund = self.db.row(
            """SELECT * FROM investable_funds
                 WHERE investable_index_id=? AND fund_code=? AND active=1""",
            (int(index_id), str(fund_code).strip()),
        )
        if not fund:
            return FundExecutionCheck(False, "未收录", ("目录中没有该指数的这只基金",), "", "")
        return self.evaluate_fund_execution(
            fund, intended_amount=intended_amount, as_of=as_of,
            require_same_day=require_same_day,
        )

    def require_fund_execution(
        self,
        index_id: int,
        fund_code: str,
        *,
        intended_amount: float = 0,
        as_of: datetime | None = None,
        require_same_day: bool = True,
    ) -> FundExecutionCheck:
        result = self.check_fund_execution(
            index_id, fund_code, intended_amount=intended_amount, as_of=as_of,
            require_same_day=require_same_day,
        )
        result.require()
        return result

    def revalidate_fund_execution(
        self,
        index_id: int,
        fund_code: str,
        *,
        checker: Callable[[dict[str, Any]], dict[str, Any]],
        intended_amount: float = 0,
        as_of: datetime | None = None,
        refresh_admission: bool = True,
    ) -> FundExecutionCheck:
        fund = self.db.row(
            """SELECT * FROM investable_funds
                 WHERE investable_index_id=? AND fund_code=? AND active=1""",
            (int(index_id), str(fund_code).strip()),
        )
        if not fund:
            return FundExecutionCheck(False, "未收录", ("目录中没有该指数的这只基金",), "", "")
        evidence = checker(dict(fund))
        if not isinstance(evidence, dict):
            raise ValueError("轻量复核接口必须返回证据对象。")
        if str(evidence.get("fund_code") or fund_code).strip() != str(fund_code).strip():
            raise ValueError("轻量复核返回了另一只基金，已拒绝写入。")
        status = str(evidence.get("status") or "").strip()
        allowed = PUBLIC_BUYABLE_STATUSES.union({"暂停申购", "暂停大额申购", "限购", "不可申购"})
        if status not in allowed:
            raise ValueError("轻量复核缺少明确的可申购、暂停或限购结论。")
        if status == "限购":
            try:
                limit_amount = float(evidence.get("purchase_limit_amount") or 0)
            except (TypeError, ValueError):
                limit_amount = 0
            status = f"限购≤{limit_amount:g}元" if limit_amount > 0 else "限购（额度待核对）"
        source_url = str(evidence.get("source_url") or "").strip()
        parsed = urlparse(source_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("轻量复核必须附带HTTPS公开证据地址。")
        checked_at = as_of or local_now()
        stamp = checked_at.isoformat(timespec="seconds")
        self.db.execute(
            """UPDATE investable_funds SET public_buyable_status=?,platform_evidence_url=?,
                      platform_checked_at=?,verified_at=?
                  WHERE investable_index_id=? AND fund_code=? AND active=1""",
            (status, source_url, stamp, stamp, int(index_id), str(fund_code).strip()),
        )
        if refresh_admission:
            self.refresh_admission(int(index_id))
        return self.check_fund_execution(
            index_id, fund_code, intended_amount=intended_amount, as_of=checked_at,
            require_same_day=True,
        )

    def revalidate_radar_execution(
        self,
        *,
        checker: Callable[[dict[str, Any]], dict[str, Any]],
        progress: Callable[[int, int, str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Refresh one usable strict-C route per radar index without an AI call.

        Durable catalog admission and volatile order execution are intentionally
        separate.  This routine refreshes the latter just before a paid daily scan.
        Duplicate fund codes shared by multiple index rows reuse the same downloaded
        evidence, while every mapping row receives its own auditable timestamp.
        """
        now = as_of or local_now()
        universe = self.radar_universe(refresh_admission=False)
        strict_by_index = self.strict_catalog_funds_by_index(
            [int(row["platform_index_id"]) for row in universe]
        )
        evidence_cache: dict[str, dict[str, Any]] = {}
        error_cache: dict[str, Exception] = {}
        errors: list[str] = []
        error_keys: set[tuple[int, str]] = set()
        refreshed_codes: set[str] = set()
        attempted_codes: set[str] = set()
        total = len(universe)
        checked = 0
        stopped = False
        finalized: set[int] = set()
        rows_by_id = {int(row["platform_index_id"]): row for row in universe}

        def finish_index(index_id: int, ready: bool) -> None:
            nonlocal checked
            if index_id in finalized:
                return
            finalized.add(index_id)
            checked = len(finalized)
            if progress:
                row = rows_by_id[index_id]
                progress(
                    checked,
                    total,
                    f"{row.get('official_index_name') or row.get('index_name')}｜"
                    + ("当日可执行" if ready else "仍不可执行"),
                )

        # Preserve the original stop contract: when stop is already requested,
        # no public-network request is started and checked remains zero.
        if stop_requested and stop_requested():
            stopped = True
        else:
            for row in universe:
                index_id = int(row["platform_index_id"])
                funds = strict_by_index.get(index_id, [])
                ready = any(
                    self.evaluate_fund_execution(
                        fund, intended_amount=1, as_of=now, require_same_day=True,
                    ).executable
                    for fund in funds
                )
                if ready or not funds:
                    finish_index(index_id, ready)

        worker_count = self._execution_refresh_workers()
        max_candidates = max((len(funds) for funds in strict_by_index.values()), default=0)
        for candidate_rank in range(max_candidates):
            if stopped:
                break
            groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
            for row in universe:
                index_id = int(row["platform_index_id"])
                if index_id in finalized:
                    continue
                funds = strict_by_index.get(index_id, [])
                if candidate_rank >= len(funds):
                    finish_index(index_id, False)
                    continue
                fund = funds[candidate_rank]
                code = str(fund.get("fund_code") or "").strip()
                groups.setdefault(code, []).append((index_id, fund))

            grouped = list(groups.items())
            for offset in range(0, len(grouped), worker_count):
                if stop_requested and stop_requested():
                    stopped = True
                    break
                chunk = grouped[offset:offset + worker_count]
                network_items = [
                    (code, targets) for code, targets in chunk
                    if code not in evidence_cache and code not in error_cache
                ]
                if network_items:
                    with ThreadPoolExecutor(
                        max_workers=min(worker_count, len(network_items)),
                        thread_name_prefix="fund-execution",
                    ) as pool:
                        futures = {
                            pool.submit(checker, dict(targets[0][1])): code
                            for code, targets in network_items
                        }
                        attempted_codes.update(futures.values())
                        for future in as_completed(futures):
                            code = futures[future]
                            try:
                                evidence = future.result()
                            except Exception as exc:
                                error_cache[code] = exc
                            else:
                                evidence_cache[code] = evidence
                                refreshed_codes.add(code)

                # Public requests may run concurrently, but every SQLite write is
                # deliberately serialized here in the owning worker thread.
                for code, targets in chunk:
                    for index_id, _fund in targets:
                        if index_id in finalized:
                            continue
                        failure = error_cache.get(code)
                        if failure is not None:
                            key = (index_id, code)
                            if key not in error_keys:
                                row = rows_by_id[index_id]
                                errors.append(
                                    f"{row.get('ant_index_code') or row.get('index_code')} / "
                                    f"{code}：{failure}"
                                )
                                error_keys.add(key)
                            continue
                        evidence = evidence_cache.get(code)
                        try:
                            result = self.revalidate_fund_execution(
                                index_id,
                                code,
                                checker=lambda _fund, payload=evidence: payload,
                                intended_amount=1,
                                as_of=now,
                                refresh_admission=False,
                            )
                        except Exception as exc:
                            key = (index_id, code)
                            if key not in error_keys:
                                row = rows_by_id[index_id]
                                errors.append(
                                    f"{row.get('ant_index_code') or row.get('index_code')} / "
                                    f"{code}：{exc}"
                                )
                                error_keys.add(key)
                        else:
                            if result.executable:
                                finish_index(index_id, True)

                for _code, targets in chunk:
                    for index_id, _fund in targets:
                        if (
                            index_id not in finalized
                            and candidate_rank + 1 >= len(strict_by_index.get(index_id, []))
                        ):
                            finish_index(index_id, False)
                if stop_requested and stop_requested():
                    stopped = True
                    break

        if not stopped:
            for index_id in rows_by_id:
                if index_id not in finalized:
                    finish_index(index_id, False)
        self.refresh_admission()
        summary = self.execution_summary(as_of=now)
        summary.update({
            "checked": checked,
            "network_funds": len(refreshed_codes),
            "network_attempts": len(attempted_codes),
            "worker_count": worker_count,
            "errors": errors,
            "stopped": stopped,
        })
        return summary

    def execution_summary(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        """Return same-day execution coverage without mutating the catalog."""
        now = as_of or local_now()
        universe = self.radar_universe(refresh_admission=False)
        strict_by_index = self.strict_catalog_funds_by_index(
            [int(row["platform_index_id"]) for row in universe]
        )
        ready_ids: list[str] = []
        missing: list[dict[str, str]] = []
        for row in universe:
            funds = strict_by_index.get(int(row["platform_index_id"]), [])
            executable = any(
                self.evaluate_fund_execution(
                    fund, intended_amount=1, as_of=now, require_same_day=True,
                ).executable
                for fund in funds
            )
            if executable:
                ready_ids.append(str(row.get("index_code") or ""))
            else:
                missing.append({
                    "index_code": str(row.get("index_code") or ""),
                    "display_code": str(row.get("ant_index_code") or ""),
                    "index_name": str(row.get("official_index_name") or row.get("index_name") or ""),
                })
        return {
            "total": len(universe),
            "ready": len(ready_ids),
            "ready_ids": ready_ids,
            "missing": missing,
            "as_of": now.isoformat(timespec="seconds"),
        }

    def refresh_admission(self, index_id: int | None = None) -> dict[str, int]:
        # Changing one child can disable its broad parent (or re-enable that parent
        # after the child is removed).  A targeted refresh therefore includes the
        # selected node, its parent and its children; a full refresh covers all
        # active rows.  This keeps the graph consistent without re-evaluating 100
        # rows after every single fund update.
        if index_id is None:
            related_ids = [
                int(row["id"]) for row in self.db.rows(
                    "SELECT id FROM investable_indices WHERE active=1 ORDER BY display_order,id"
                )
            ]
        else:
            target = self.index(int(index_id))
            if not target or int(target.get("active") or 0) != 1:
                related_ids = []
            else:
                related_ids = [
                    int(row["id"]) for row in self.db.rows(
                        """SELECT id FROM investable_indices
                             WHERE active=1 AND (
                                   id=? OR official_index_code=?
                                   OR parent_official_code=?
                             ) ORDER BY display_order,id""",
                        (
                            int(index_id),
                            str(target.get("parent_official_code") or "__none__"),
                            str(target.get("official_index_code") or "__none__"),
                        ),
                    )
                ]
        all_rows = {int(row["id"]): row for row in self.list_indices()}
        rows = [all_rows[row_id] for row_id in related_ids if row_id in all_rows]
        strict_by_index = self.strict_catalog_funds_by_index(related_ids)
        ttl_hours = self._execution_ttl_hours()
        evaluations = []
        for row in rows:
            check = self._check_index_row(row)
            strict_funds = strict_by_index.get(int(row["id"]), [])
            eligible_codes = [
                fund["fund_code"] for fund in strict_funds
                if self.evaluate_fund_execution(
                    fund, intended_amount=1, ttl_hours=ttl_hours,
                    require_same_day=True,
                ).executable
            ]
            evaluations.append((row, check, strict_funds, eligible_codes))
        with self.db.connect() as conn:
            for row, check, strict_funds, eligible_codes in evaluations:
                active = int(row.get("active") or 0) == 1
                enabled = active and check.admitted and bool(strict_funds)
                admission_reasons = list(check.reasons)
                if not strict_funds:
                    admission_reasons.append("没有严格同指数、被动、C类且公开可申购的场外基金")
                conn.execute(
                    """UPDATE investable_indices
                          SET radar_enabled=?,admission_status=?,exclusion_reason=?
                        WHERE id=?""",
                    (
                        1 if enabled else 0,
                        "已准入雷达" if enabled else (
                            "候选·" + "；".join(admission_reasons[:2]) if active else "历史记录"
                        ),
                        "" if enabled else "；".join(admission_reasons),
                        int(row["id"]),
                    ),
                )
                conn.execute(
                    "UPDATE investable_funds SET radar_eligible=0 WHERE investable_index_id=?",
                    (int(row["id"]),),
                )
                if enabled and eligible_codes:
                    placeholders = ",".join("?" for _ in eligible_codes)
                    conn.execute(
                        f"""UPDATE investable_funds SET radar_eligible=1
                              WHERE investable_index_id=? AND fund_code IN ({placeholders})""",
                        (int(row["id"]), *eligible_codes),
                    )
                conn.execute(
                    """UPDATE universe SET has_buyable_fund=?,fund_code=?,status=?
                         WHERE index_code=?""",
                    (
                        1 if enabled and eligible_codes else 0,
                        eligible_codes[0] if enabled and eligible_codes else "",
                        "CSI代表·严格C可投" if enabled else "CSI代表·不进入扫描",
                        row["universe_id"],
                    ),
                )
            # Prefer the finer child whenever both levels otherwise qualify.  The
            # relation comes from curated CSI hierarchy metadata, never fuzzy names.
            parent_rows = conn.execute(
                """SELECT DISTINCT parent.id,parent.universe_id
                     FROM investable_indices parent
                     JOIN investable_indices child
                       ON child.parent_official_code=parent.official_index_code
                      AND child.id<>parent.id
                    WHERE parent.active=1 AND parent.radar_enabled=1
                      AND child.active=1 AND child.radar_enabled=1"""
            ).fetchall()
            for parent in parent_rows:
                conn.execute(
                    """UPDATE investable_indices
                          SET radar_enabled=0,admission_status='候选·已有更细叶子指数',
                              exclusion_reason='已有严格C且行情合格的更细子行业；为避免父子重复仓位，本指数不参与扫描。'
                        WHERE id=?""",
                    (int(parent["id"]),),
                )
                conn.execute(
                    "UPDATE investable_funds SET radar_eligible=0 WHERE investable_index_id=?",
                    (int(parent["id"]),),
                )
                conn.execute(
                    """UPDATE universe SET has_buyable_fund=0,fund_code='',status='CSI代表·父级去重'
                         WHERE index_code=?""",
                    (str(parent["universe_id"]),),
                )
            admitted = int(conn.execute(
                """SELECT COUNT(*) FROM investable_indices
                     WHERE active=1 AND radar_enabled=1 AND admission_status='已准入雷达'"""
            ).fetchone()[0])
            executable = int(conn.execute(
                """SELECT COUNT(*) FROM investable_indices i
                     WHERE i.active=1 AND i.radar_enabled=1
                       AND EXISTS(SELECT 1 FROM investable_funds f
                                   WHERE f.investable_index_id=i.id
                                     AND f.active=1 AND f.radar_eligible=1)"""
            ).fetchone()[0])
        # Keep the execution catalog and the ordinary transaction library joined.
        # This is idempotent and fills only blank/placeholder user fields.
        self.db.link_strict_funds_to_library(index_id)
        return {"checked": len(rows), "admitted": admitted, "executable": executable}

    def retire_confirmed_no_c(
        self,
        index_id: int | None = None,
        *,
        required_algorithm: str | None = None,
    ) -> int:
        """Archive only source-complete ``已确认无C`` rows.

        Network failures, incomplete searches and a mere TTL expiry are never
        destructive.  Rows are archived rather than physically deleted so old
        research remains readable and foreign-key history stays intact.
        """
        params: list[Any] = []
        extra = ""
        if index_id is not None:
            extra = " AND i.id=?"
            params.append(int(index_id))
        if required_algorithm:
            extra += " AND s.algorithm_version=?"
            params.append(str(required_algorithm))
        rows = self.db.rows(
            f"""SELECT i.id,i.universe_id
                   FROM investable_indices i
                   JOIN csi_fund_sync_status s ON s.investable_index_id=i.id
                  WHERE i.active=1 AND i.catalog_role='日扫代表'
                    AND s.state='已确认无C'{extra}""",
            params,
        )
        retired: list[dict[str, Any]] = []
        for row in rows:
            if not self.strict_catalog_funds(int(row["id"])):
                retired.append(row)
        if not retired:
            return 0
        ids = [int(row["id"]) for row in retired]
        universe_ids = [str(row["universe_id"]) for row in retired]
        id_marks = ",".join("?" for _ in ids)
        uid_marks = ",".join("?" for _ in universe_ids)
        with self.db.connect() as conn:
            conn.execute(
                f"""UPDATE investable_indices
                        SET active=0,radar_enabled=0,admission_status='历史记录',
                            catalog_role='历史记录',
                            exclusion_reason='已完整核验但没有严格同指数、被动、C类且公开可申购的场外基金；不再跟踪或扫描。'
                      WHERE id IN ({id_marks})""",
                ids,
            )
            conn.execute(
                f"UPDATE proxy_mappings SET active=0 WHERE universe_id IN ({uid_marks})",
                universe_ids,
            )
            conn.execute(
                f"""UPDATE universe SET has_buyable_fund=0,fund_code='',status='历史记录·无严格C'
                      WHERE index_code IN ({uid_marks})""",
                universe_ids,
            )
        return len(retired)

    def radar_universe(self, *, refresh_admission: bool = True) -> list[dict[str, Any]]:
        """Return only CSI leaves backed by at least one durable strict-C fund."""
        # Re-evaluate the volatile buyability TTL immediately before the catalog is
        # handed to a scan/task package.  A once-buyable fund must not remain an
        # executable recommendation merely because no UI page happened to refresh.
        if refresh_admission:
            self.refresh_admission()
        rows = self.db.rows(
            """SELECT u.index_code,i.official_index_name AS index_name,
                      COALESCE(NULLIF(i.industry_group,''),u.category) AS category,
                      CASE WHEN EXISTS(
                          SELECT 1 FROM investable_funds f WHERE f.investable_index_id=i.id
                           AND f.active=1 AND f.radar_eligible=1) THEN 1 ELSE 0 END AS has_buyable_fund,
                      COALESCE((SELECT f.fund_code FROM investable_funds f
                        WHERE f.investable_index_id=i.id AND f.active=1 AND f.radar_eligible=1
                        ORDER BY f.quality_rank,f.fund_code LIMIT 1),'') AS fund_code,
                      CASE WHEN EXISTS(
                          SELECT 1 FROM investable_funds f WHERE f.investable_index_id=i.id
                           AND f.active=1 AND f.radar_eligible=1)
                           THEN '研究与基金均可执行' ELSE '研究代表·基金待映射' END AS status,
                      ('CSI代表：'||i.ant_index_code||' '||i.official_index_name||'；'||
                       i.representative_reason) AS notes,
                      i.id AS platform_index_id,i.ant_index_code,
                      i.ant_index_code AS csi_index_code,i.official_index_name AS ant_index_name,
                      i.official_index_name AS csi_index_name,
                      i.official_index_code,i.official_index_name,i.representative_reason,
                      i.hierarchy_level,i.parent_official_code,i.allocation_cluster,
                      i.official_tracked_product
                 FROM investable_indices i
                 JOIN universe u ON u.index_code=i.universe_id
                WHERE i.active=1 AND i.radar_enabled=1 AND i.admission_status='已准入雷达'
                  AND i.catalog_role='日扫代表'
                  AND NOT EXISTS(
                      SELECT 1 FROM investable_indices child
                       WHERE child.active=1 AND child.radar_enabled=1
                         AND child.parent_official_code=i.official_index_code
                         AND child.id<>i.id
                  )
                ORDER BY i.display_order,i.id"""
        )
        strict_by_index = self.strict_catalog_funds_by_index(
            [int(row["platform_index_id"]) for row in rows]
        )
        ttl_hours = self._execution_ttl_hours()
        result: list[dict[str, Any]] = []
        for row in rows:
            strict_funds = strict_by_index.get(int(row["platform_index_id"]), [])
            if not strict_funds:
                continue
            fresh = [
                fund for fund in strict_funds
                if self.evaluate_fund_execution(
                    fund, intended_amount=1, ttl_hours=ttl_hours,
                    require_same_day=True,
                ).executable
            ]
            chosen = (fresh or strict_funds)[0]
            row["has_buyable_fund"] = 1 if fresh else 0
            row["fund_code"] = str(chosen.get("fund_code") or "") if fresh else ""
            row["status"] = (
                "研究与基金均可执行" if fresh else "可研究·下单前需刷新申购状态"
            )
            # Freeze the exact, same-index passive-C execution whitelist into
            # every scan/task item.  A lone default ``fund_code`` only proved
            # that *some* fund existed and let an AI return an unrelated or
            # A-class code.  Downstream validators now have the full whitelist
            # (and the source-backed canonical name) available without another
            # mutable catalog lookup.
            row["strict_c_funds"] = [
                {
                    "fund_code": str(fund.get("fund_code") or ""),
                    "fund_name": str(fund.get("fund_name") or ""),
                    "share_class": str(fund.get("share_class") or ""),
                    "strategy_kind": str(fund.get("strategy_kind") or ""),
                    "public_buyable_status": str(fund.get("public_buyable_status") or ""),
                    "source_url": str(
                        fund.get("platform_evidence_url") or fund.get("source_url") or ""
                    ),
                    "verified_at": str(
                        fund.get("platform_checked_at") or fund.get("verified_at") or ""
                    ),
                }
                for fund in fresh
            ]
            row["strict_c_fund_codes"] = [
                str(fund.get("fund_code") or "") for fund in fresh
                if str(fund.get("fund_code") or "")
            ]
            result.append(row)
        return result

    def coverage(self) -> dict[str, int]:
        row = self.db.row(
            """SELECT
                   (SELECT COUNT(*) FROM investable_indices WHERE active=1) AS candidates,
                   (SELECT COUNT(*) FROM investable_indices
                     WHERE active=1 AND radar_enabled=1 AND admission_status='已准入雷达') AS admitted,
                   (SELECT COUNT(*) FROM investable_indices i WHERE i.active=1 AND EXISTS(
                     SELECT 1 FROM investable_funds f WHERE f.investable_index_id=i.id
                       AND f.active=1 AND f.radar_eligible=1)) AS executable_indices,
                   (SELECT COUNT(*) FROM investable_funds f JOIN investable_indices i
                     ON i.id=f.investable_index_id WHERE i.active=1 AND f.active=1) AS funds,
                    (SELECT COUNT(*) FROM investable_funds f JOIN investable_indices i
                      ON i.id=f.investable_index_id WHERE i.active=1 AND f.active=1
                        AND f.passive_verified=1 AND f.relation_kind='同一指数'
                        AND UPPER(f.share_class)='C'
                        AND f.strategy_kind IN ('被动ETF联接','被动指数')) AS passive_funds,
                   (SELECT COUNT(*) FROM investable_funds f JOIN investable_indices i
                     ON i.id=f.investable_index_id WHERE i.active=1 AND f.active=1
                       AND f.radar_eligible=1) AS eligible_funds,
                   (SELECT COUNT(*) FROM csi_index_products p JOIN investable_indices i
                     ON i.id=p.investable_index_id WHERE i.active=1 AND p.active=1) AS official_products,
                   (SELECT COUNT(*) FROM investable_indices i WHERE i.active=1 AND EXISTS(
                     SELECT 1 FROM csi_index_products p
                      WHERE p.investable_index_id=i.id AND p.active=1)) AS official_covered_indices,
                   (SELECT COUNT(*) FROM csi_product_sync_status s JOIN investable_indices i
                     ON i.id=s.investable_index_id WHERE i.active=1 AND s.state='已完成') AS product_synced_indices,
                   (SELECT COUNT(*) FROM csi_fund_sync_status s JOIN investable_indices i
                      ON i.id=s.investable_index_id WHERE i.active=1
                       AND s.state IN ('已完成','已确认无C')) AS fund_synced_indices"""
        ) or {}
        return {key: int(value or 0) for key, value in row.items()}

    def add_candidate(
        self,
        *,
        industry_group: str,
        platform_code: str,
        platform_name: str,
        official_code: str,
        official_name: str,
        official_source_url: str,
        evidence_kind: str = "中证官方事实表",
        platform_source_url: str = "",
        note: str = "",
        security_id: str = "",
        proxy_code: str = "",
        proxy_name: str = "",
        mapping_kind: str = "精确指数",
        mapping_confidence: float = 0,
    ) -> int:
        code = platform_code.strip().upper()
        match = CSI_CODE_RE.fullmatch(code)
        if not industry_group.strip():
            raise ValueError("请填写行业分类。")
        if not match:
            raise ValueError("这里只允许中证代码加.CSI，例如000990.CSI或H30184.CSI。")
        if official_code.strip() != match.group(1):
            raise ValueError(".CSI前的代码必须与中证官方代码一致。")
        if not official_name.strip() or not platform_name.strip():
            raise ValueError("请填写中证官方指数全名。")
        if "csindex.com.cn" not in official_source_url.lower():
            raise ValueError("正式资料必须来自中证指数有限公司官网。")
        if not security_id.strip() or float(mapping_confidence or 0) < 0.95:
            raise ValueError("进入日扫前必须填写已经验证的精确行情证券ID，置信度至少0.95。")
        if self.db.row(
            "SELECT id FROM investable_indices WHERE UPPER(ant_index_code)=? AND active=1", (code,)
        ):
            raise ValueError(f"{code} 已经在当前目录中。")
        now = local_now().isoformat(timespec="seconds")
        uid = "C" + match.group(1)
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO universe(index_code,index_name,category,status,notes)
                   VALUES(?,?,?,'CSI日扫代表','用户新增后须通过全部CSI准入校验。')
                   ON CONFLICT(index_code) DO UPDATE SET index_name=excluded.index_name,
                       category=excluded.category,status=excluded.status""",
                (uid, official_name.strip(), industry_group.strip()),
            )
            cursor = conn.execute(
                """INSERT INTO investable_indices(
                       universe_id,official_index_code,official_index_name,ant_index_code,ant_index_name,
                       mapping_relation,mapping_confidence,verification_status,official_source_url,
                       ant_source_note,verified_at,active,display_order,industry_group,admission_status,
                       platform_evidence_kind,platform_source_url,platform_checked_at,radar_enabled,
                       exclusion_reason,catalog_role,representative_reason,quote_history_status,csi_methodology_version)
                   VALUES(?,?,?,?,?,'中证官方同一指数',1,'中证官方资料已核对',?,?,?,1,999,?,
                          '候选·待检查',?,?,?,0,'等待准入检查','日扫代表',?,
                          '已核验至少20个交易日','用户补充')""",
                (
                    uid, match.group(1), official_name.strip(), code, platform_name.strip(),
                    official_source_url.strip(), note.strip(), now, industry_group.strip(),
                    evidence_kind.strip() or "中证官方事实表",
                    platform_source_url.strip() or official_source_url.strip(), now,
                    note.strip() or "用户新增的CSI代表；需避免与现有行业重复。",
                ),
            )
            index_id = int(cursor.lastrowid)
            conn.execute(
                """INSERT INTO proxy_mappings(universe_id,provider,security_id,proxy_code,proxy_name,
                       mapping_kind,confidence,rationale,active,reviewed_at)
                   VALUES(?,'eastmoney_public',?,?,?,'精确指数',?,'用户已验证至少20日行情',1,?)
                   ON CONFLICT(universe_id) DO UPDATE SET security_id=excluded.security_id,
                       proxy_code=excluded.proxy_code,proxy_name=excluded.proxy_name,
                       mapping_kind='精确指数',confidence=excluded.confidence,active=1,
                       rationale=excluded.rationale,reviewed_at=excluded.reviewed_at""",
                (
                    uid, security_id.strip(), proxy_code.strip() or match.group(1),
                    proxy_name.strip() or official_name.strip(),
                    max(0.95, min(1.0, float(mapping_confidence))), now,
                ),
            )
        self.refresh_admission(index_id)
        return index_id

    def mark_platform_verified(
        self,
        index_id: int,
        *,
        platform_code: str,
        platform_name: str,
        evidence_kind: str,
        source_url: str = "",
        note: str = "",
    ) -> AdmissionCheck:
        """Compatibility wrapper for editing the official CSI identity."""
        code = platform_code.strip().upper()
        match = CSI_CODE_RE.fullmatch(code)
        if not match:
            raise ValueError("这里只允许中证代码加.CSI，例如000990.CSI或H30184.CSI。")
        row = self.index(index_id)
        if not row or str(row.get("official_index_code") or "") != match.group(1):
            raise ValueError("CSI展示代码必须与已登记的中证官方代码一致。")
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """UPDATE investable_indices SET ant_index_code=?,ant_index_name=?,
                      platform_evidence_kind=?,platform_source_url=?,ant_source_note=?,
                      platform_checked_at=?,verified_at=? WHERE id=?""",
            (
                code, platform_name.strip(), evidence_kind.strip() or "中证官方事实表",
                source_url.strip() or str(row.get("official_source_url") or ""), note.strip(),
                now, now, int(index_id),
            ),
        )
        self.refresh_admission(index_id)
        return self.check(index_id)

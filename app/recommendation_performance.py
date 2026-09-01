"""Auditable post-recommendation performance tracking.

The recommendation ledger is intentionally independent from the user's draft,
trade, holding and cash ledgers.  Recording a model opinion never means an
order was placed.  Outcomes are derived only from dated market/NAV rows; a
missing value or an unverified fee schedule remains unknown instead of being
silently converted to zero.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import urlparse

from .config import LOCAL_TZ, local_now, local_today
from .catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from .engine import UnknownFeeScheduleError, fee_rate_for_days
from .platform_catalog import PlatformCatalogService
from .ranking import (
    NEGATIVE_TILTS,
    POSITIVE_TILTS,
    RESEARCH_TILT_LABELS,
    recommendation_label,
)
from .recurring import EastmoneyFundNavProvider
from .trading_calendar import ChinaTradingCalendar
from .universe import UNIVERSE_VERSION


PERFORMANCE_HORIZONS = (1, 3, 5, 10, 20)
_SYNC_LOCK = threading.Lock()
_CSI_CODE_RE = re.compile(r"^(?:\d{6}|H\d{5})$", re.IGNORECASE)
_TIMING_STATUS_LABELS = frozenset(("可以买", "等待回调", "不宜介入"))
_EXECUTION_STATUS_LABELS = frozenset((
    "可执行", "基金待绑定", "申购状态待复核", "行情待更新", "数据待补齐", "不可执行",
))


def _moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed.astimezone(LOCAL_TZ)


def _direction_hit(label: str, value: float | None) -> int | None:
    """Evaluate a directional opinion without scoring the neutral tier.

    Positive tiers hit only on a strictly positive result; negative tiers hit
    only on a strictly negative result.  A zero return is a miss rather than a
    fabricated win, and ``观察`` has no directional denominator.
    """
    if value is None:
        return None
    if label in POSITIVE_TILTS:
        return int(value > 0)
    if label in NEGATIVE_TILTS:
        return int(value <= 0)
    return None


def _max_drawdown(values: list[float]) -> float | None:
    clean = [float(value) for value in values if value is not None and float(value) > 0]
    if len(clean) < 2:
        return None
    peak = clean[0]
    maximum = 0.0
    for value in clean:
        peak = max(peak, value)
        maximum = max(maximum, (peak - value) / peak * 100.0)
    return round(maximum, 6)


def capture_recommendation_observations(
    conn,
    task_id: str,
    recommendation_at: str | datetime,
    calendar: ChinaTradingCalendar,
) -> int:
    """Freeze one full current scan without mutating any execution ledger.

    All seven-tier audit rows are observed against their CSI index so ranking
    quality can be measured across the full score distribution.  A main-list
    row keeps the fund selected by the scan; every other row freezes the
    highest-ranked strict passive-C fund for that exact index as an explicitly
    labelled benchmark.  A benchmark is never presented as an AI-picked fund.
    """
    run = conn.execute(
        """SELECT task_id,data_cutoff,ranking_policy_version,ranking_policy_json,imported_at
             FROM radar_runs WHERE task_id=?""",
        (task_id,),
    ).fetchone()
    if not run:
        raise ValueError(f"无法记录推荐表现：研究任务{task_id}不存在。")
    moment = _moment(recommendation_at)
    pricing_date = calendar.resolve_order(moment, fund_kind="普通场外").pricing_date
    rows = conn.execute(
        """SELECT a.*,c.id AS candidate_id,c.rank_no AS candidate_rank,
                  c.fund_code,c.fund_name,c.index_code AS candidate_index_code,
                  m.data_source,m.expected_data_cutoff
             FROM radar_audit a
             LEFT JOIN candidates c
               ON c.task_id=a.task_id AND c.universe_id=a.universe_id
             LEFT JOIN sector_metrics m
               ON m.task_id=a.task_id AND m.universe_id=a.universe_id
            WHERE a.task_id=? ORDER BY a.local_rank,a.universe_id""",
        (task_id,),
    ).fetchall()
    strict_funds: dict[str, list[dict[str, Any]]] = {}
    for raw in conn.execute(
        """SELECT i.universe_id,f.fund_code,f.fund_name,f.quality_rank,
                  f.strategy_kind,f.share_class,f.public_buyable_status,
                  f.source_url,f.platform_evidence_url,f.verified_at,
                  f.platform_checked_at,f.active,f.passive_verified,
                  f.relation_kind,f.verification_algorithm
             FROM investable_indices i
             JOIN investable_funds f ON f.investable_index_id=i.id
            WHERE i.active=1 AND i.radar_enabled=1 AND f.active=1
              AND f.relation_kind='同一指数' AND UPPER(f.share_class)='C'
              AND f.strategy_kind IN ('被动ETF联接','被动指数')
              AND f.passive_verified=1 AND f.verification_algorithm=?
            ORDER BY i.universe_id,f.quality_rank,f.fund_code""",
        (STRICT_FIELD_SYNC_ALGORITHM_VERSION,),
    ).fetchall():
        fund = dict(raw)
        if PlatformCatalogService.fund_has_strict_catalog_eligibility(fund):
            strict_funds.setdefault(str(raw["universe_id"]), []).append(fund)
    now = local_now().isoformat(timespec="seconds")
    inserted = 0
    for raw in rows:
        row = dict(raw)
        catalog_funds = strict_funds.get(str(row.get("universe_id") or ""), [])
        candidate_fund_code = str(row.get("fund_code") or "").strip()
        candidate_fund_name = str(row.get("fund_name") or "").strip()
        if candidate_fund_code:
            catalog_fund = next(
                (
                    fund for fund in catalog_funds
                    if str(fund.get("fund_code") or "").strip() == candidate_fund_code
                ),
                None,
            )
            if not catalog_fund:
                raise ValueError(
                    f"无法记录推荐表现：{row.get('universe_id')}候选基金{candidate_fund_code}"
                    "不在同指数严格C类白名单。"
                )
            canonical_name = str(catalog_fund.get("fund_name") or "").strip()
            if candidate_fund_name != canonical_name:
                raise ValueError(
                    f"无法记录推荐表现：基金{candidate_fund_code}完整名称与白名单不一致。"
                )
            fund_code = candidate_fund_code
            fund_name = canonical_name
            fund_selection_kind = "主榜候选基金"
        else:
            catalog_fund = catalog_funds[0] if catalog_funds else {}
            fund_code = str(catalog_fund.get("fund_code") or "").strip()
            fund_name = str(catalog_fund.get("fund_name") or "").strip()
            fund_selection_kind = "同指数严格C基准" if fund_code else ""
        proxy_code = str(row.get("proxy_code") or "").strip()
        bare_proxy_code = proxy_code[:-4] if proxy_code.upper().endswith(".CSI") else proxy_code
        provider = str(row.get("data_source") or "").strip()
        security_id = ""
        if provider == "csi_official" and bare_proxy_code:
            security_id = f"CSI.{bare_proxy_code}"
        elif row.get("universe_id"):
            mapping = conn.execute(
                """SELECT provider,security_id FROM proxy_mappings
                    WHERE universe_id=? AND active=1""",
                (row["universe_id"],),
            ).fetchone()
            if mapping:
                provider = str(mapping["provider"] or provider)
                security_id = str(mapping["security_id"] or "")
        index_code = str(row.get("candidate_index_code") or "").strip()
        if not index_code and bare_proxy_code:
            index_code = f"{bare_proxy_code}.CSI" if provider == "csi_official" else bare_proxy_code
        snapshot = {
            "task_id": task_id,
            "universe_id": row.get("universe_id"),
            "rank_no": int(row.get("candidate_rank") or row.get("local_rank") or 0),
            "recommendation_score": float(row.get("recommendation_score") or 0),
            "research_tilt": str(row.get("research_tilt") or "观察"),
            "timing_status": str(row.get("timing_status") or ""),
            "execution_status": str(row.get("execution_status") or ""),
            "score_components": json.loads(str(row.get("score_components_json") or "{}")),
            "ranking_policy_version": str(run["ranking_policy_version"] or ""),
            "ranking_policy": json.loads(str(run["ranking_policy_json"] or "{}")),
            "candidate_fund_code": candidate_fund_code,
            "observed_fund_code": fund_code,
            "fund_selection_kind": fund_selection_kind,
            "fund_catalog_source_url": str(
                catalog_fund.get("platform_evidence_url") or catalog_fund.get("source_url") or ""
            ),
            "fund_catalog_verified_at": str(
                catalog_fund.get("platform_checked_at") or catalog_fund.get("verified_at") or ""
            ),
        }
        cur = conn.execute(
            """INSERT OR IGNORE INTO recommendation_observations(
                   task_id,source_candidate_id,universe_id,index_code,sector_name,
                   fund_code,fund_name,fund_selection_kind,rank_no,recommendation_score,research_tilt,
                   timing_status,execution_status,ranking_policy_version,
                   recommendation_at,data_cutoff,pricing_date,index_provider,
                   index_security_id,source_snapshot_json,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, row.get("candidate_id"), str(row.get("universe_id") or ""),
                index_code, str(row.get("sector_name") or ""),
                fund_code, fund_name, fund_selection_kind,
                int(row.get("candidate_rank") or row.get("local_rank") or 0),
                float(row.get("recommendation_score") or 0),
                str(row.get("research_tilt") or "观察"),
                str(row.get("timing_status") or ""), str(row.get("execution_status") or ""),
                str(run["ranking_policy_version"] or ""), moment.isoformat(timespec="seconds"),
                str(run["data_cutoff"] or "")[:10], pricing_date.isoformat(), provider,
                security_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "观察中" if provider and security_id else "缺少指数行情映射", now, now,
            ),
        )
        if not cur.rowcount:
            continue
        inserted += 1
        observation_id = int(cur.lastrowid)
        for horizon in PERFORMANCE_HORIZONS:
            target = calendar.advance_trading_days(pricing_date, horizon)
            conn.execute(
                """INSERT INTO recommendation_outcomes(
                       observation_id,horizon_sessions,target_date,status,reason
                   ) VALUES(?,?,?,?,?)""",
                (observation_id, horizon, target.isoformat(), "等待行情", "目标交易日尚未成熟或行情尚未发布"),
            )
    return inserted


def _canonical_json(value: Any) -> str:
    """Canonicalise a JSON value without treating malformed text as an empty object."""
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) < 10:
        raise ValueError("研究数据截止日为空。")
    return date.fromisoformat(text[:10]).isoformat()


def _finite_score(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field}不是有效分数。")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}不是有效分数。") from exc
    if not math.isfinite(score) or score < 0 or score > 100:
        raise ValueError(f"{field}必须是0至100之间的有限数字。")
    return score


def _https_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() == "https" and bool(parsed.hostname)


def _evidence_time(value: Any, *, not_after: datetime) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("证据缺少核验时间。")
    checked = _moment(text)
    if checked > not_after:
        raise ValueError("基金映射是在研究完成后才核验的，不能倒填为历史执行证据。")
    return checked.isoformat(timespec="seconds")


def _historical_candidate_fund_evidence(
    conn,
    *,
    universe_id: str,
    bare_index_code: str,
    fund_code: str,
    fund_name: str,
    imported_at: datetime,
) -> tuple[dict[str, Any] | None, str]:
    """Return exact pre-existing strict-C evidence for one frozen candidate.

    A later catalog row is not enough.  The mapping, passive/C classification,
    public-buyable evidence and their timestamps must all pre-date the original
    recommendation.  If any part is missing, the index opinion remains valid but
    the historical observation deliberately carries no executable fund.
    """
    rows = conn.execute(
        """SELECT i.universe_id,i.official_index_code,i.official_index_name,
                  i.official_source_url,i.verified_at AS index_verified_at,
                  i.platform_checked_at AS index_platform_checked_at,
                  f.fund_code,f.fund_name,f.strategy_kind,f.share_class,
                  f.public_buyable_status,f.quality_rank,f.source_url,
                  f.platform_evidence_url,f.verified_at AS fund_verified_at,
                  f.platform_checked_at AS fund_platform_checked_at,
                  f.active AS fund_active,f.passive_verified,f.relation_kind,
                  f.verification_algorithm
             FROM investable_indices i
             JOIN investable_funds f ON f.investable_index_id=i.id
            WHERE i.universe_id=? AND UPPER(i.official_index_code)=UPPER(?)
              AND f.fund_code=?
            ORDER BY f.active DESC,f.quality_rank,f.id""",
        (universe_id, bare_index_code, fund_code),
    ).fetchall()
    reasons: list[str] = []
    for raw in rows:
        evidence = dict(raw)
        strict_row = {
            "active": evidence.get("fund_active"),
            "relation_kind": evidence.get("relation_kind"),
            "share_class": evidence.get("share_class"),
            "strategy_kind": evidence.get("strategy_kind"),
            "passive_verified": evidence.get("passive_verified"),
            "verification_algorithm": evidence.get("verification_algorithm"),
            "public_buyable_status": evidence.get("public_buyable_status"),
        }
        try:
            if not PlatformCatalogService.fund_has_strict_catalog_eligibility(strict_row):
                raise ValueError("不是严格同指数、被动、C类且公开可申购的基金。")
            if str(evidence.get("fund_name") or "").strip() != fund_name:
                raise ValueError("基金完整名称与冻结候选不一致。")
            official_url = str(evidence.get("official_source_url") or "").strip()
            fund_url = str(
                evidence.get("platform_evidence_url") or evidence.get("source_url") or ""
            ).strip()
            if not _https_url(official_url) or not _https_url(fund_url):
                raise ValueError("指数或基金缺少HTTPS公开证据。")
            index_checked = _evidence_time(
                evidence.get("index_platform_checked_at") or evidence.get("index_verified_at"),
                not_after=imported_at,
            )
            fund_checked = _evidence_time(
                evidence.get("fund_platform_checked_at") or evidence.get("fund_verified_at"),
                not_after=imported_at,
            )
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
            continue
        return ({
            "universe_id": universe_id,
            "official_index_code": bare_index_code,
            "official_index_name": str(evidence.get("official_index_name") or ""),
            "official_source_url": official_url,
            "index_checked_at": index_checked,
            "fund_code": fund_code,
            "fund_name": fund_name,
            "strategy_kind": str(evidence.get("strategy_kind") or ""),
            "share_class": str(evidence.get("share_class") or ""),
            "public_buyable_status": str(evidence.get("public_buyable_status") or ""),
            "fund_source_url": fund_url,
            "fund_checked_at": fund_checked,
            "verification_algorithm": str(evidence.get("verification_algorithm") or ""),
        }, "")
    return None, (reasons[0] if reasons else "冻结候选没有同指数严格C类基金证据。")


def _validated_historical_v3_snapshot(conn, joined: Any) -> dict[str, Any]:
    """Validate one immutable V3 run independently from mutable derived tables."""
    row = dict(joined)
    task_id = str(row.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("研究任务缺少task_id。")
    try:
        payload = json.loads(str(row.get("raw_json") or ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("原始研究快照不是有效JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("原始研究快照不是JSON对象。")
    required_header = {
        "protocol_version": "3.0",
        "task_kind": "night",
        "task_id": task_id,
        "task_status": "COMPLETE",
        "universe_version": UNIVERSE_VERSION,
    }
    for field, expected in required_header.items():
        if str(payload.get(field) or "").strip() != expected:
            raise ValueError(f"{field}不是可恢复的完整V3值。")
    if str(row.get("mode") or "") != "完整扫描" \
            or str(row.get("job_status") or "") != "已完成" \
            or str(row.get("job_stage") or "") != "完成":
        raise ValueError("任务不是已完成的完整扫描。")
    if str(row.get("job_universe_version") or "") != UNIVERSE_VERSION:
        raise ValueError("任务冻结目录不是现行严格C类V3目录。")
    imported_at = _moment(str(row.get("imported_at") or ""))
    if str(row.get("research_date") or "") != imported_at.date().isoformat():
        raise ValueError("研究日期与原始导入时刻不一致。")
    universe_hash = str(row.get("universe_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", universe_hash, re.IGNORECASE):
        raise ValueError("任务冻结目录哈希无效。")
    try:
        frozen_ids = json.loads(str(row.get("universe_ids_json") or "[]"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("任务冻结目录ID列表无效。") from exc
    if not isinstance(frozen_ids, list) or not frozen_ids \
            or any(not isinstance(value, str) or not value.strip() for value in frozen_ids) \
            or len(frozen_ids) != len(set(frozen_ids)):
        raise ValueError("任务冻结目录ID列表为空、重复或格式错误。")
    total = len(frozen_ids)
    counts = (
        int(row.get("total_items") or 0), int(row.get("completed_items") or 0),
        int(row.get("universe_total") or 0), int(row.get("analyzed_total") or 0),
        int(payload.get("universe_total") or 0), int(payload.get("analyzed_total") or 0),
    )
    if any(value != total for value in counts):
        raise ValueError("任务、研究记录与原始快照的完整数量不一致。")
    policy_version = str(payload.get("ranking_policy_version") or "").strip()
    if not policy_version or any(str(value or "").strip() != policy_version for value in (
        row.get("ranking_policy_version"), row.get("job_ranking_policy_version"),
    )):
        raise ValueError("原始快照、任务与研究记录的排序算法版本不一致。")
    try:
        payload_policy = _canonical_json(payload.get("ranking_policy"))
        run_policy = _canonical_json(row.get("ranking_policy_json"))
        job_policy = _canonical_json(row.get("job_ranking_policy_json"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("排序策略快照不是有效JSON。") from exc
    if payload_policy != run_policy or payload_policy != job_policy:
        raise ValueError("原始快照、任务与研究记录的排序策略不一致。")
    data_cutoff = _snapshot_date(payload.get("data_cutoff"))
    if _snapshot_date(row.get("data_cutoff")) != data_cutoff:
        raise ValueError("研究记录与原始快照的数据截止日不一致。")

    raw_audit = payload.get("audit")
    if not isinstance(raw_audit, list) or len(raw_audit) != total:
        raise ValueError("原始V3审计明细数量不完整。")
    audit_ids = [str(item.get("id") or "").strip() for item in raw_audit if isinstance(item, dict)]
    if audit_ids != frozen_ids:
        raise ValueError("原始审计顺序或ID与任务冻结目录不一致。")
    audit_by_id: dict[str, dict[str, Any]] = {}
    audit_by_code: dict[str, dict[str, Any]] = {}
    local_ranks: list[int] = []
    for index, item in enumerate(raw_audit, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第{index}个审计项不是JSON对象。")
        universe_id = str(item.get("id") or "").strip()
        bare_code = str(item.get("proxy_code") or "").strip().upper().removesuffix(".CSI")
        if not _CSI_CODE_RE.fullmatch(bare_code) or bare_code in audit_by_code:
            raise ValueError(f"审计项{universe_id}的CSI代码无效或重复。")
        score = _finite_score(item.get("recommendation_score"), f"审计项{universe_id}推荐分")
        tilt = str(item.get("research_tilt") or "")
        if tilt not in RESEARCH_TILT_LABELS or tilt != recommendation_label(score):
            raise ValueError(f"审计项{universe_id}的七档结论与推荐分不一致。")
        if str(item.get("timing_status") or "") not in _TIMING_STATUS_LABELS:
            raise ValueError(f"审计项{universe_id}的择时状态无效。")
        if str(item.get("execution_status") or "") not in _EXECUTION_STATUS_LABELS:
            raise ValueError(f"审计项{universe_id}的执行状态无效。")
        components = item.get("score_components")
        if not isinstance(components, dict):
            raise ValueError(f"审计项{universe_id}缺少可解释评分明细。")
        component_score = _finite_score(
            components.get("recommendation_score"), f"审计项{universe_id}评分明细",
        )
        if not math.isclose(component_score, score, abs_tol=1e-6):
            raise ValueError(f"审计项{universe_id}的评分明细与推荐分不一致。")
        try:
            local_rank = int(item.get("local_rank"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"审计项{universe_id}缺少本地排名。") from exc
        local_ranks.append(local_rank)
        audit_by_id[universe_id] = item
        audit_by_code[bare_code] = item
    if sorted(local_ranks) != list(range(1, total + 1)):
        raise ValueError("原始审计本地排名不是完整且唯一的1至N。")

    metrics = [dict(metric) for metric in conn.execute(
        """SELECT universe_id,proxy_code,data_source,is_simulated,data_cutoff,
                  expected_data_cutoff,freshness_state
             FROM sector_metrics WHERE task_id=? ORDER BY id""",
        (task_id,),
    ).fetchall()]
    metric_by_id = {str(metric.get("universe_id") or ""): metric for metric in metrics}
    if len(metrics) != total or set(metric_by_id) != set(frozen_ids):
        raise ValueError("原始任务的行情快照不能完整覆盖冻结目录。")
    for universe_id, item in audit_by_id.items():
        metric = metric_by_id[universe_id]
        metric_code = str(metric.get("proxy_code") or "").strip().upper().removesuffix(".CSI")
        audit_code = str(item.get("proxy_code") or "").strip().upper().removesuffix(".CSI")
        if metric_code != audit_code or str(metric.get("data_source") or "") != "csi_official" \
                or int(metric.get("is_simulated") or 0) != 0 \
                or _snapshot_date(metric.get("data_cutoff")) != data_cutoff:
            raise ValueError(f"审计项{universe_id}没有同日、真实、官方CSI行情证据。")

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("原始候选列表不是JSON数组。")
    candidate_by_id: dict[str, dict[str, Any]] = {}
    candidate_ranks: list[int] = []
    for index, candidate in enumerate(raw_candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError(f"第{index}个原始候选不是JSON对象。")
        bare_code = str(candidate.get("index_code") or "").strip().upper().removesuffix(".CSI")
        audit_item = audit_by_code.get(bare_code)
        if not audit_item:
            raise ValueError(f"第{index}个原始候选不属于冻结审计目录。")
        universe_id = str(audit_item.get("id") or "")
        if universe_id in candidate_by_id:
            raise ValueError(f"原始候选{universe_id}重复。")
        for field in ("research_tilt", "timing_status", "execution_status"):
            if str(candidate.get(field) or "") != str(audit_item.get(field) or ""):
                raise ValueError(f"原始候选{universe_id}的{field}与审计项不一致。")
        candidate_score = _finite_score(
            candidate.get("recommendation_score"), f"原始候选{universe_id}推荐分",
        )
        if not math.isclose(
            candidate_score, float(audit_item["recommendation_score"]), abs_tol=1e-6,
        ):
            raise ValueError(f"原始候选{universe_id}的推荐分与审计项不一致。")
        if str(candidate.get("sector_name") or "").strip() != str(audit_item.get("name") or "").strip():
            raise ValueError(f"原始候选{universe_id}的指数名称与审计项不一致。")
        try:
            candidate_rank = int(candidate.get("rank"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"原始候选{universe_id}缺少候选排名。") from exc
        candidate_ranks.append(candidate_rank)
        candidate_by_id[universe_id] = candidate
    if sorted(candidate_ranks) != list(range(1, len(raw_candidates) + 1)):
        raise ValueError("原始候选排名不是完整且唯一的1至N。")
    return {
        "task_id": task_id,
        "payload": payload,
        "frozen_ids": frozen_ids,
        "audit_by_id": audit_by_id,
        "metric_by_id": metric_by_id,
        "candidate_by_id": candidate_by_id,
        "policy_version": policy_version,
        "policy": json.loads(payload_policy),
        "data_cutoff": data_cutoff,
        "imported_at": imported_at,
        "universe_hash": universe_hash,
    }


def recover_historical_v3_recommendation_observations(
    db,
    *,
    task_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Recover only independently verifiable, immutable, complete V3 snapshots.

    This is deliberately an explicit data-repair operation rather than a schema
    migration.  It never calls a network/API and never touches drafts, trades,
    holdings, cost lots or active cash.  Existing task/universe observations are
    protected by their unique key, so rerunning the recovery is idempotent.
    """
    selected = None if task_ids is None else [str(value).strip() for value in task_ids if str(value).strip()]
    where = ""
    params: list[Any] = []
    if selected is not None:
        if not selected:
            return {
                "tasks_checked": 0, "tasks_eligible": 0, "tasks_restored": 0,
                "observations_inserted": 0, "outcomes_inserted": 0,
                "candidate_funds_kept": 0, "candidate_funds_dropped": 0,
                "details": [],
            }
        where = f" WHERE r.task_id IN ({','.join('?' for _ in selected)})"
        params.extend(selected)
    calendar = ChinaTradingCalendar.from_db(db)
    now = local_now().isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "tasks_checked": 0, "tasks_eligible": 0, "tasks_restored": 0,
        "observations_inserted": 0, "outcomes_inserted": 0,
        "candidate_funds_kept": 0, "candidate_funds_dropped": 0,
        "details": [],
    }
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT r.*,j.mode,j.stage AS job_stage,j.status AS job_status,
                       j.total_items,j.completed_items,j.universe_version AS job_universe_version,
                       j.research_date,j.universe_ids_json,j.universe_hash,
                       j.ranking_policy_version AS job_ranking_policy_version,
                       j.ranking_policy_json AS job_ranking_policy_json
                  FROM radar_runs r JOIN analysis_jobs j ON j.task_id=r.task_id
                  {where} ORDER BY r.imported_at,r.task_id""",
            tuple(params),
        ).fetchall()
        for joined in rows:
            report["tasks_checked"] += 1
            task_id = str(joined["task_id"] or "")
            detail: dict[str, Any] = {
                "task_id": task_id, "status": "已排除", "reason": "", "inserted": 0,
                "candidate_funds_kept": 0, "candidate_funds_dropped": 0,
            }
            try:
                snapshot = _validated_historical_v3_snapshot(conn, joined)
            except (TypeError, ValueError) as exc:
                detail["reason"] = str(exc)
                report["details"].append(detail)
                continue
            report["tasks_eligible"] += 1
            recommendation_at = snapshot["imported_at"]
            pricing_date = calendar.resolve_order(
                recommendation_at, fund_kind="普通场外",
            ).pricing_date
            inserted_for_task = 0
            for universe_id in snapshot["frozen_ids"]:
                item = snapshot["audit_by_id"][universe_id]
                metric = snapshot["metric_by_id"][universe_id]
                candidate = snapshot["candidate_by_id"].get(universe_id)
                bare_code = str(item.get("proxy_code") or "").strip().upper().removesuffix(".CSI")
                fund_code = str((candidate or {}).get("fund_code") or "").strip()
                fund_name = str((candidate or {}).get("fund_name") or "").strip()
                fund_evidence = None
                fund_drop_reason = ""
                if fund_code or fund_name:
                    if not re.fullmatch(r"\d{6}", fund_code) or not fund_name:
                        fund_drop_reason = "冻结候选的基金代码或完整名称无效。"
                    else:
                        fund_evidence, fund_drop_reason = _historical_candidate_fund_evidence(
                            conn,
                            universe_id=universe_id,
                            bare_index_code=bare_code,
                            fund_code=fund_code,
                            fund_name=fund_name,
                            imported_at=recommendation_at,
                        )
                    if fund_evidence:
                        detail["candidate_funds_kept"] += 1
                    else:
                        detail["candidate_funds_dropped"] += 1
                        fund_code = ""
                        fund_name = ""
                source_snapshot = {
                    "capture_origin": "historical_v3_raw_snapshot_recovery",
                    "task_id": task_id,
                    "universe_hash": snapshot["universe_hash"],
                    "raw_audit": item,
                    "raw_candidate": candidate,
                    "ranking_policy_version": snapshot["policy_version"],
                    "ranking_policy": snapshot["policy"],
                    "fund_evidence": fund_evidence,
                    "fund_drop_reason": fund_drop_reason,
                    "metric_evidence": {
                        "provider": str(metric.get("data_source") or ""),
                        "proxy_code": str(metric.get("proxy_code") or ""),
                        "data_cutoff": str(metric.get("data_cutoff") or ""),
                        "expected_data_cutoff": str(metric.get("expected_data_cutoff") or ""),
                        "freshness_state": str(metric.get("freshness_state") or ""),
                    },
                }
                rank_no = int((candidate or {}).get("rank") or item.get("local_rank") or 0)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO recommendation_observations(
                           task_id,source_candidate_id,universe_id,index_code,sector_name,
                           fund_code,fund_name,fund_selection_kind,rank_no,recommendation_score,
                           research_tilt,timing_status,execution_status,ranking_policy_version,
                           recommendation_at,data_cutoff,pricing_date,index_provider,
                           index_security_id,source_snapshot_json,status,created_at,updated_at
                       ) VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, universe_id, f"{bare_code}.CSI", str(item.get("name") or ""),
                        fund_code, fund_name,
                        "原始V3候选基金（历史证据已闭合）" if fund_code else "",
                        rank_no, float(item["recommendation_score"]),
                        str(item.get("research_tilt") or ""), str(item.get("timing_status") or ""),
                        str(item.get("execution_status") or ""), snapshot["policy_version"],
                        recommendation_at.isoformat(timespec="seconds"), snapshot["data_cutoff"],
                        pricing_date.isoformat(), "csi_official", f"CSI.{bare_code}",
                        json.dumps(
                            source_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                        ),
                        "观察中", now, now,
                    ),
                )
                if not cur.rowcount:
                    continue
                inserted_for_task += 1
                observation_id = int(cur.lastrowid)
                for horizon in PERFORMANCE_HORIZONS:
                    target = calendar.advance_trading_days(pricing_date, horizon)
                    conn.execute(
                        """INSERT INTO recommendation_outcomes(
                               observation_id,horizon_sessions,target_date,status,reason
                           ) VALUES(?,?,?,?,?)""",
                        (
                            observation_id, horizon, target.isoformat(), "等待行情",
                            "由完整V3原始快照恢复；目标交易日尚未成熟或行情尚未发布",
                        ),
                    )
                    report["outcomes_inserted"] += 1
            detail["inserted"] = inserted_for_task
            detail["status"] = "已恢复" if inserted_for_task else "已存在"
            detail["reason"] = (
                f"完整V3快照{len(snapshot['frozen_ids'])}项；"
                f"历史候选基金保留{detail['candidate_funds_kept']}只、"
                f"证据不足留空{detail['candidate_funds_dropped']}只。"
            )
            if inserted_for_task:
                report["tasks_restored"] += 1
            report["observations_inserted"] += inserted_for_task
            report["candidate_funds_kept"] += detail["candidate_funds_kept"]
            report["candidate_funds_dropped"] += detail["candidate_funds_dropped"]
            report["details"].append(detail)
    return report


def _series(
    conn,
    table: str,
    value_column: str,
    where: str,
    params: tuple[Any, ...],
    start: str,
    end: str,
) -> list[tuple[str, float]]:
    date_column = "trade_date" if table == "market_bars" else "nav_date"
    rows = conn.execute(
        f"SELECT {date_column},{value_column} FROM {table} WHERE {where} "
        f"AND {date_column}>=? AND {date_column}<=? ORDER BY {date_column}",
        (*params, start, end),
    ).fetchall()
    return [(str(row[0]), float(row[1])) for row in rows if row[1] is not None]


def _verified_fee_rate(conn, fund_code: str, holding_days: int) -> tuple[float, float]:
    fund = conn.execute(
        """SELECT purchase_fee_pct,profile_source_url,profile_checked_at
             FROM funds WHERE fund_code=?""",
        (fund_code,),
    ).fetchone()
    if not fund or not str(fund["profile_source_url"] or "").strip() \
            or not str(fund["profile_checked_at"] or "").strip():
        raise UnknownFeeScheduleError("基金申购费率来源尚未核验")
    tiers = [dict(row) for row in conn.execute(
        """SELECT min_days,max_days,redeem_fee_pct,source_url,checked_at
             FROM fee_tiers WHERE fund_code=? ORDER BY min_days""",
        (fund_code,),
    ).fetchall()]
    rate = fee_rate_for_days(tiers, holding_days)
    matching = [
        row for row in tiers
        if holding_days >= int(row.get("min_days") or 0)
        and (row.get("max_days") is None or holding_days <= int(row["max_days"]))
    ]
    chosen = max(matching, key=lambda row: int(row.get("min_days") or 0)) if matching else None
    if not chosen or not str(chosen.get("source_url") or "").strip() \
            or not str(chosen.get("checked_at") or "").strip():
        raise UnknownFeeScheduleError("适用赎回费阶梯缺少可审计来源")
    return float(fund["purchase_fee_pct"] or 0), float(rate)


def refresh_recommendation_outcomes(db, *, as_of: date | None = None) -> dict[str, int]:
    """Recalculate every mature observation from locally cached official data."""
    today = as_of or local_today()
    evaluated_at = local_now().isoformat(timespec="seconds")
    counts = defaultdict(int)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT o.*,r.horizon_sessions,r.target_date
                 FROM recommendation_observations o
                 JOIN recommendation_outcomes r ON r.observation_id=o.id
                ORDER BY o.id,r.horizon_sessions"""
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            counts["total"] += 1
            if today < date.fromisoformat(str(row["target_date"])):
                counts["waiting"] += 1
                continue
            start_date = str(row["pricing_date"])
            end_date = str(row["target_date"])
            index_values = _series(
                conn, "market_bars", "close", "provider=? AND security_id=?",
                (row["index_provider"], row["index_security_id"]), start_date, end_date,
            ) if row.get("index_provider") and row.get("index_security_id") else []
            index_by_date = dict(index_values)
            index_return = None
            index_drawdown = None
            reason_parts: list[str] = []
            if start_date in index_by_date and end_date in index_by_date and index_by_date[start_date] > 0:
                index_return = (index_by_date[end_date] / index_by_date[start_date] - 1.0) * 100.0
                index_drawdown = _max_drawdown([value for _, value in index_values])
                counts["index_mature"] += 1
            else:
                reason_parts.append("指数起止行情尚未齐全")

            fund_start = fund_end = fund_gross = fund_net = fund_drawdown = None
            purchase_fee = redemption_fee = None
            holding_calendar_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
            fund_code = str(row.get("fund_code") or "").strip()
            if fund_code:
                nav_values = _series(
                    conn, "fund_nav_history", "nav", "fund_code=?", (fund_code,), start_date, end_date,
                )
                nav_by_date = dict(nav_values)
                start_evidence = conn.execute(
                    """SELECT source_url FROM fund_nav_history WHERE fund_code=? AND nav_date=?""",
                    (fund_code, start_date),
                ).fetchone()
                end_evidence = conn.execute(
                    """SELECT source_url FROM fund_nav_history WHERE fund_code=? AND nav_date=?""",
                    (fund_code, end_date),
                ).fetchone()
                if start_date not in nav_by_date or end_date not in nav_by_date:
                    reason_parts.append("执行基金起止净值尚未齐全")
                elif not start_evidence or not end_evidence \
                        or not str(start_evidence[0] or "").strip() or not str(end_evidence[0] or "").strip():
                    reason_parts.append("执行基金净值缺少来源地址")
                elif nav_by_date[start_date] <= 0:
                    reason_parts.append("执行基金起点净值无效")
                else:
                    fund_start = nav_by_date[start_date]
                    fund_end = nav_by_date[end_date]
                    fund_gross = (fund_end / fund_start - 1.0) * 100.0
                    fund_drawdown = _max_drawdown([value for _, value in nav_values])
                    try:
                        purchase_fee, redemption_fee = _verified_fee_rate(
                            conn, fund_code, holding_calendar_days,
                        )
                    except UnknownFeeScheduleError as exc:
                        reason_parts.append(str(exc))
                    else:
                        # The subscription amount is a gross cash outflow.  Its
                        # investable part is amount/(1+purchase rate), then the
                        # redemption fee is deducted from the final proceeds.
                        investable = 1.0 / (1.0 + purchase_fee / 100.0)
                        proceeds = investable * (fund_end / fund_start) * (1.0 - redemption_fee / 100.0)
                        fund_net = (proceeds - 1.0) * 100.0
                        counts["fund_mature"] += 1
            else:
                reason_parts.append("本行是全目录研究结论，没有执行基金推荐")

            if fund_net is not None:
                status = "费后结果已核算"
            elif index_return is not None and not fund_code:
                # A full-catalog research row can intentionally contain no
                # executable fund.  Its index result is finished, not a fund
                # result waiting for missing NAV or fee evidence.
                status = "指数结果已核算·无执行基金"
                counts["index_only_mature"] += 1
            elif index_return is not None:
                status = "指数参考已核算·基金待核算"
            else:
                status = "等待行情"
            conn.execute(
                """UPDATE recommendation_outcomes SET
                       actual_end_date=?,holding_calendar_days=?,
                       index_start_value=?,index_end_value=?,index_return_pct=?,index_max_drawdown_pct=?,
                       fund_start_nav=?,fund_end_nav=?,fund_gross_return_pct=?,purchase_fee_pct=?,
                       redemption_fee_pct=?,fund_net_return_pct=?,fund_max_drawdown_pct=?,
                       index_direction_hit=?,fund_direction_hit=?,status=?,reason=?,evaluated_at=?
                     WHERE observation_id=? AND horizon_sessions=?""",
                (
                    end_date if index_return is not None or fund_gross is not None else "",
                    holding_calendar_days, index_by_date.get(start_date), index_by_date.get(end_date),
                    index_return, index_drawdown, fund_start, fund_end, fund_gross, purchase_fee,
                    redemption_fee, fund_net, fund_drawdown,
                    _direction_hit(str(row["research_tilt"]), index_return),
                    _direction_hit(str(row["research_tilt"]), fund_net), status,
                    "；".join(reason_parts), evaluated_at, row["id"], row["horizon_sessions"],
                ),
            )
        conn.execute(
            """UPDATE recommendation_observations SET status=CASE
                   WHEN EXISTS(SELECT 1 FROM recommendation_outcomes r
                                WHERE r.observation_id=recommendation_observations.id
                                  AND r.horizon_sessions=20 AND r.status='费后结果已核算')
                     THEN '20日费后结果已核算'
                   WHEN EXISTS(SELECT 1 FROM recommendation_outcomes r
                                WHERE r.observation_id=recommendation_observations.id
                                  AND r.horizon_sessions=20
                                  AND r.status='指数结果已核算·无执行基金')
                     THEN '20日指数结果已核算·无执行基金'
                   WHEN EXISTS(SELECT 1 FROM recommendation_outcomes r
                                WHERE r.observation_id=recommendation_observations.id
                                  AND r.index_return_pct IS NOT NULL)
                     THEN '部分指数结果已核算'
                   WHEN index_provider='' OR index_security_id='' THEN '缺少指数行情映射'
                   ELSE '观察中' END,
                   updated_at=?""",
            (evaluated_at,),
        )
    return dict(counts)


def recommendation_performance_summary(
    db,
    *,
    horizon_sessions: int = 20,
    ranking_policy_version: str = "",
) -> dict[str, Any]:
    """Return fixed-grain KPIs; unknown observations never enter averages."""
    if horizon_sessions not in PERFORMANCE_HORIZONS:
        raise ValueError("推荐表现周期必须是1、3、5、10或20个交易日。")
    params: list[Any] = [horizon_sessions]
    policy_clause = ""
    if ranking_policy_version:
        policy_clause = " AND o.ranking_policy_version=?"
        params.append(ranking_policy_version)
    rows = db.rows(
        f"""SELECT o.*,
                    r.horizon_sessions,r.target_date,r.actual_end_date,r.holding_calendar_days,
                    r.index_start_value,r.index_end_value,r.index_return_pct,r.index_max_drawdown_pct,
                    r.fund_start_nav,r.fund_end_nav,r.fund_gross_return_pct,r.purchase_fee_pct,
                    r.redemption_fee_pct,r.fund_net_return_pct,r.fund_max_drawdown_pct,
                    r.index_direction_hit,r.fund_direction_hit,
                    r.status AS outcome_status,r.reason AS outcome_reason,r.evaluated_at
               FROM recommendation_observations o
             JOIN recommendation_outcomes r ON r.observation_id=o.id
            WHERE r.horizon_sessions=?{policy_clause}
            ORDER BY o.recommendation_at DESC,o.rank_no,o.id""",
        params,
    )

    def average(values: list[float]) -> float | None:
        return None if not values else round(sum(values) / len(values), 6)

    index_returns = [float(row["index_return_pct"]) for row in rows if row.get("index_return_pct") is not None]
    index_only_mature = [
        row for row in rows
        if row.get("index_return_pct") is not None
        and not str(row.get("fund_code") or "").strip()
    ]
    fund_returns = [float(row["fund_net_return_pct"]) for row in rows if row.get("fund_net_return_pct") is not None]
    fund_drawdowns = [float(row["fund_max_drawdown_pct"]) for row in rows if row.get("fund_max_drawdown_pct") is not None]
    holding_days = [int(row["holding_calendar_days"]) for row in rows if row.get("fund_net_return_pct") is not None]
    directional = [int(row["fund_direction_hit"]) for row in rows if row.get("fund_direction_hit") is not None]
    def grouped_metrics(group: str, selected: list[dict[str, Any]]) -> dict[str, Any]:
        mature = [row for row in selected if row.get("fund_net_return_pct") is not None]
        returns = [float(row["fund_net_return_pct"]) for row in mature]
        hits = [int(row["fund_direction_hit"]) for row in mature if row.get("fund_direction_hit") is not None]
        days = [float(row["holding_calendar_days"]) for row in mature if row.get("holding_calendar_days") is not None]
        drawdowns = [float(row["fund_max_drawdown_pct"]) for row in mature if row.get("fund_max_drawdown_pct") is not None]
        return {
            "group": group,
            "observations": len(selected),
            "fund_mature": len(mature),
            "average_fund_net_return_pct": average(returns),
            "fund_positive_rate_pct": average([100.0 if value > 0 else 0.0 for value in returns]),
            "direction_hit_rate_pct": average([float(value) * 100 for value in hits]),
            "average_holding_calendar_days": average(days),
            "average_fund_max_drawdown_pct": average(drawdowns),
        }

    by_tier: list[dict[str, Any]] = []
    tiers = ("非常推荐买", "推荐买", "有点推荐买", "观察", "不太推荐买", "不推荐买", "千万别买")
    for tier in tiers:
        selected = [row for row in rows if row.get("research_tilt") == tier]
        item = grouped_metrics(tier, selected)
        item["research_tilt"] = tier
        by_tier.append(item)
    by_score_band: list[dict[str, Any]] = []
    for low in range(90, -1, -10):
        high = 100 if low == 90 else low + 10
        selected = [
            row for row in rows
            if float(row.get("recommendation_score") or 0) >= low
            and (float(row.get("recommendation_score") or 0) <= high if high == 100
                 else float(row.get("recommendation_score") or 0) < high)
        ]
        label = f"{low}–{high}分" if high == 100 else f"{low}–<{high}分"
        item = grouped_metrics(label, selected)
        item.update({"score_low": low, "score_high": high})
        by_score_band.append(item)
    return {
        "horizon_sessions": horizon_sessions,
        "observations": len(rows),
        "index_mature": len(index_returns),
        "index_only_mature": len(index_only_mature),
        "fund_mature": len(fund_returns),
        "average_index_return_pct": average(index_returns),
        "average_fund_net_return_pct": average(fund_returns),
        "fund_positive_rate_pct": average([100.0 if value > 0 else 0.0 for value in fund_returns]),
        "direction_hit_rate_pct": average([float(value) * 100 for value in directional]),
        "average_holding_calendar_days": average([float(value) for value in holding_days]),
        "average_fund_max_drawdown_pct": average(fund_drawdowns),
        "maximum_fund_drawdown_pct": max(fund_drawdowns) if fund_drawdowns else None,
        "by_tier": by_tier,
        "by_score_band": by_score_band,
        "rows": rows,
        "definition": (
            "推荐时刻按15:00规则确定模拟定价日；指数与执行基金分开核算。"
            "没有执行基金的全目录研究在指数结果成熟后即视为研究核算完成，不冒充基金待核算。"
            "基金结果仅在起止净值和适用费率均有来源时计入；未知值不按0。"
        ),
    }


class RecommendationPerformanceService:
    """Background-callable NAV sync plus local outcome refresh."""

    def __init__(self, db, provider: EastmoneyFundNavProvider | None = None):
        self.db = db
        self.provider = provider or EastmoneyFundNavProvider(
            timeout_seconds=int(float(db.setting("market_timeout_seconds", "20")))
        )

    def refresh_cached(self, *, as_of: date | None = None) -> dict[str, int]:
        return refresh_recommendation_outcomes(self.db, as_of=as_of)

    def recover_verified_v3_snapshots(
        self,
        *,
        task_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Restore independently verifiable historical snapshots, locally only."""
        return recover_historical_v3_recommendation_observations(
            self.db, task_ids=task_ids,
        )

    def sync_candidate_navs(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        if not _SYNC_LOCK.acquire(blocking=False):
            raise RuntimeError("另一项推荐表现净值更新正在运行，请等待完成后再试。")
        try:
            rows = self.db.rows(
                """SELECT fund_code,MIN(pricing_date) AS start_date
                     FROM recommendation_observations
                    WHERE fund_code<>'' GROUP BY fund_code ORDER BY fund_code"""
            )
            now = local_now().isoformat(timespec="seconds")
            errors: list[str] = []
            saved = 0
            updated = 0
            for index, row in enumerate(rows, start=1):
                if stop_requested and stop_requested():
                    break
                code = str(row["fund_code"])
                start_date = date.fromisoformat(str(row["start_date"]))
                if start_date > local_today():
                    # A recommendation created after the fund cut-off starts
                    # on the next session.  Its NAV does not exist yet; this is
                    # a normal waiting state, not a network failure.
                    continue
                if progress:
                    progress(index, len(rows), code)
                try:
                    values, source_url = self.provider.fetch(
                        code, start_date.isoformat(), local_today().isoformat(),
                    )
                except Exception as exc:
                    errors.append(f"{code}：{exc}")
                    continue
                with self.db.connect() as conn:
                    for nav_date, nav in sorted(values.items()):
                        conn.execute(
                            """INSERT INTO fund_nav_history(
                                   fund_code,nav_date,nav,source,source_url,fetched_at
                               ) VALUES(?,?,?,?,?,?)
                               ON CONFLICT(fund_code,nav_date) DO UPDATE SET
                                   nav=excluded.nav,source=excluded.source,
                                   source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                            (code, nav_date, float(nav), self.provider.name, source_url, now),
                        )
                        saved += 1
                updated += 1
            fee_result: dict[str, Any] = {
                "total": 0, "updated": 0, "skipped": 0, "errors": [], "stopped": False,
            }
            if rows and not (stop_requested and stop_requested()):
                from .fund_discovery import FundFeeSyncService
                fee_result = FundFeeSyncService(self.db).sync(
                    [str(row["fund_code"]) for row in rows],
                    only_missing=True,
                    stop_requested=stop_requested,
                    progress=(
                        (lambda done, total, code: progress(done, total, f"费率 {code}"))
                        if progress else None
                    ),
                )
            # Fee evidence may have matured fund-net outcomes even when no NAV
            # row changed during this pass, so calculate once more afterwards.
            counts = refresh_recommendation_outcomes(self.db)
            return {
                "funds_total": len(rows), "funds_updated": updated,
                "nav_rows_saved": saved,
                "fee_funds_updated": int(fee_result.get("updated", 0)),
                "errors": [*errors, *fee_result.get("errors", [])], "outcomes": counts,
            }
        finally:
            _SYNC_LOCK.release()

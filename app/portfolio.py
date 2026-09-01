from __future__ import annotations

from collections import defaultdict
from typing import Any


ACTIVE_CAPITAL_LIMIT_DEFAULT = 8000.0
BUY_ACTIONS = {"买入", "定投", "转换转入"}
POSITIVE_RESEARCH_TILTS = {"非常推荐买", "推荐买", "有点推荐买"}


def infer_active_capital(fund: dict[str, Any] | None) -> bool:
    """Return whether a fund belongs to the user's A-share tactical pool.

    This is only a fallback for records that have no explicit classification.
    Persisted user choices must take precedence over this name-based inference.
    """
    fund = fund or {}
    text = " ".join(
        str(fund.get(key) or "")
        for key in ("fund_name", "index_code", "index_name", "correlation_bucket", "notes")
    ).upper()
    if any(word in text for word in ("债券", "货币", "余额宝", "纳斯达克", "NASDAQ", "QDII", "海外")):
        return False
    if "黄金" in text:
        return True
    index_code = str(fund.get("index_code") or "").upper()
    return bool(
        index_code.endswith(".CSI")
        or "ETF联接" in text
        or "被动指数" in text
        or ("指数" in text and "增强" not in text)
    )


def active_capital_holdings(db) -> list[dict[str, Any]]:
    return db.rows(
        """SELECT h.*,f.fund_name,f.index_code,f.index_name,f.correlation_bucket
             FROM holdings h JOIN funds f USING(fund_code)
            WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1
            ORDER BY h.fund_code,h.id"""
    )


def fund_exposure_identity(db, fund_code: str) -> dict[str, str]:
    """Resolve a fund to the official index used for exposure aggregation.

    Fund code is deliberately the last fallback.  Two different C-class funds
    tracking the same official index must consume one shared position limit.
    """
    code = str(fund_code or "").strip()
    mapped = db.row(
        """SELECT i.official_index_code,i.official_index_name,i.allocation_cluster,
                  COALESCE(NULLIF(f.relation_kind,''),'同一指数') AS relation_kind
             FROM investable_funds f JOIN investable_indices i ON i.id=f.investable_index_id
            WHERE f.fund_code=? AND f.active=1 AND i.active=1
            ORDER BY f.radar_eligible DESC,f.passive_verified DESC,f.quality_rank,f.id LIMIT 1""",
        (code,),
    )
    if mapped and str(mapped.get("relation_kind") or "") == "同一指数":
        official = str(mapped.get("official_index_code") or "").upper().strip()
        if official:
            return {
                "index_key": f"CSI:{official}",
                "index_name": str(mapped.get("official_index_name") or official),
                "bucket": str(mapped.get("allocation_cluster") or "").strip(),
            }
    mapping = db.row(
        """SELECT m.index_code,u.index_name AS sector_name
             FROM fund_mappings m LEFT JOIN universe u ON u.index_code=m.universe_id
            WHERE m.fund_code=? ORDER BY m.verified_at DESC,m.id DESC LIMIT 1""",
        (code,),
    )
    fund = db.row(
        "SELECT fund_code,index_code,index_name,correlation_bucket FROM funds WHERE fund_code=?",
        (code,),
    ) or {}
    index_code = str((mapping or {}).get("index_code") or fund.get("index_code") or "").upper().strip()
    index_name = str(fund.get("index_name") or (mapping or {}).get("sector_name") or "").strip()
    bucket = str(fund.get("correlation_bucket") or "").strip()
    key = f"INDEX:{index_code}" if index_code else f"NAME:{index_name}" if index_name else f"FUND:{code}"
    return {"index_key": key, "index_name": index_name or code, "bucket": bucket}


def candidate_exposure_identity(db, candidate: dict[str, Any]) -> dict[str, str]:
    universe_id = str(candidate.get("universe_id") or "").strip()
    if universe_id:
        row = db.row(
            """SELECT official_index_code,official_index_name,allocation_cluster,industry_group
                 FROM investable_indices WHERE universe_id=? AND active=1
                 ORDER BY radar_enabled DESC,display_order,id LIMIT 1""",
            (universe_id,),
        )
        if row and str(row.get("official_index_code") or "").strip():
            official = str(row["official_index_code"]).upper().strip()
            return {
                "index_key": f"CSI:{official}",
                "index_name": str(row.get("official_index_name") or candidate.get("sector_name") or official),
                "bucket": str(candidate.get("correlation_bucket") or row.get("allocation_cluster") or row.get("industry_group") or ""),
            }
    code = str(candidate.get("fund_code") or "").strip()
    if code:
        result = fund_exposure_identity(db, code)
        if candidate.get("correlation_bucket"):
            result["bucket"] = str(candidate["correlation_bucket"])
        return result
    index_code = str(candidate.get("index_code") or "").upper().strip()
    name = str(candidate.get("sector_name") or universe_id or index_code or "未识别指数")
    return {
        "index_key": f"INDEX:{index_code}" if index_code else f"UNIVERSE:{universe_id or name}",
        "index_name": name,
        "bucket": str(candidate.get("correlation_bucket") or ""),
    }


def active_index_exposures(db) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in active_capital_holdings(db):
        identity = fund_exposure_identity(db, row["fund_code"])
        key = identity["index_key"]
        item = grouped.setdefault(key, {
            **identity, "market_value": 0.0, "cost": 0.0, "fund_codes": [],
        })
        item["market_value"] += float(row.get("units") or 0) * float(row.get("latest_nav") or 0)
        item["cost"] += float(row.get("cost") or 0)
        item["fund_codes"].append(str(row["fund_code"]))
        if not item.get("bucket"):
            item["bucket"] = str(row.get("correlation_bucket") or "")
    return grouped


def _row_counts_as_active(row: dict[str, Any]) -> bool:
    # Zero is an explicit user choice, not a missing value.  Only old records
    # that truly lack the field may use the name/index heuristic.
    if "counts_toward_active_capital" in row and row.get("counts_toward_active_capital") is not None:
        return bool(int(row.get("counts_toward_active_capital") or 0))
    return infer_active_capital(row)


def active_reservations(
    db, *, exclude_trade_id: int | None = None,
    exclude_draft_id: int | None = None,
) -> dict[str, Any]:
    """Return live, not-yet-confirmed tactical buy reservations."""
    draft_sql = """SELECT d.*,f.index_code,f.index_name,f.correlation_bucket,f.notes
                     FROM decision_drafts d LEFT JOIN funds f USING(fund_code)
                    WHERE d.status='草稿' AND d.action IN ('买入','定投','转换转入')"""
    draft_params: list[Any] = []
    if exclude_draft_id is not None:
        draft_sql += " AND d.id<>?"
        draft_params.append(int(exclude_draft_id))
    draft_rows = db.rows(draft_sql, draft_params)

    pending_sql = """SELECT t.*,f.fund_name,f.index_code,f.index_name,f.correlation_bucket,f.notes
                       FROM trades t LEFT JOIN funds f USING(fund_code)
                      WHERE t.status='待确认' AND t.action IN ('买入','定投','转换转入')"""
    pending_params: list[Any] = []
    if exclude_trade_id is not None:
        pending_sql += " AND t.id<>?"
        pending_params.append(int(exclude_trade_id))
    pending_rows = db.rows(pending_sql, pending_params)
    by_index: dict[str, float] = defaultdict(float)
    by_bucket: dict[str, float] = defaultdict(float)
    count_by_bucket: dict[str, int] = defaultdict(int)
    draft_total = pending_total = 0.0
    for kind, rows in (("draft", draft_rows), ("pending", pending_rows)):
        for row in rows:
            if not _row_counts_as_active(row):
                continue
            raw_amount = row.get("planned_amount") if kind == "draft" else row.get("amount")
            amount = max(0.0, float(raw_amount or 0))
            identity = fund_exposure_identity(db, str(row.get("fund_code") or ""))
            by_index[identity["index_key"]] += amount
            bucket = identity.get("bucket") or str(row.get("correlation_bucket") or "") or identity["index_key"]
            by_bucket[bucket] += amount
            if amount > 0:
                count_by_bucket[bucket] += 1
            if kind == "draft":
                draft_total += amount
            else:
                pending_total += amount
    return {
        "drafts": round(draft_total, 6),
        "pending_trades": round(pending_total, 6),
        "total": round(draft_total + pending_total, 6),
        "by_index": dict(by_index),
        "by_bucket": dict(by_bucket),
        "count_by_bucket": dict(count_by_bucket),
    }


def active_capital_summary(
    db, *, exclude_trade_id: int | None = None,
    exclude_draft_id: int | None = None,
) -> dict[str, Any]:
    """Separate market exposure from deployable tactical cash.

    In the absence of an explicit cash baseline, the reliable fallback is the
    gross cost of currently open tactical positions.  Unlike market value this
    does not create imaginary cash when a fund falls or consume cash when it rises.
    Live drafts and pending confirmed buys reserve cash until cancelled/confirmed.
    """
    limit = float(db.setting("active_capital_limit", str(ACTIVE_CAPITAL_LIMIT_DEFAULT)))
    rows = active_capital_holdings(db)
    market_exposure = sum(float(row.get("units") or 0) * float(row.get("latest_nav") or 0) for row in rows)
    committed_cost = sum(max(0.0, float(row.get("cost") or 0)) for row in rows)
    stored_cash = db.row("SELECT value FROM settings WHERE key='active_cash_balance'")
    if stored_cash is not None:
        try:
            cash_before_reservations = float(stored_cash["value"])
            cash_basis = "主动资金现金账本余额＋存活草稿/待确认买单预留"
        except (TypeError, ValueError):
            # Do not turn a damaged persisted cash balance into imaginary cash.
            cash_before_reservations = limit - committed_cost
            cash_basis = "现金余额损坏；临时回退为上限减当前主动持仓成本"
    else:
        cash_before_reservations = limit - committed_cost
        cash_basis = "尚未建立现金账本；回退为上限减当前主动持仓成本"
    reservations = active_reservations(
        db,
        exclude_trade_id=exclude_trade_id,
        exclude_draft_id=exclude_draft_id,
    )
    cash_unclamped = cash_before_reservations - float(reservations["total"])
    return {
        "limit": limit,
        # Compatibility: ``used`` is exposure, while ``available`` is cash.
        "used": market_exposure,
        "market_exposure": market_exposure,
        "committed_cost": committed_cost,
        "cash_before_reservations": cash_before_reservations,
        "reserved": float(reservations["total"]),
        "reserved_drafts": float(reservations["drafts"]),
        "reserved_pending_trades": float(reservations["pending_trades"]),
        "cash_unclamped": cash_unclamped,
        "cash_available": max(0.0, cash_unclamped),
        "available": max(0.0, cash_unclamped),
        "over_limit": max(0.0, -cash_unclamped),
        "cash_basis": cash_basis,
        "reservations_by_index": reservations["by_index"],
        "reservations_by_bucket": reservations["by_bucket"],
        "reservation_counts_by_bucket": reservations["count_by_bucket"],
    }


def allocate_candidate_amounts(
    db,
    candidates: list[dict[str, Any]],
    *,
    release_existing_candidate_drafts: bool = False,
) -> list[dict[str, Any]]:
    """Apply one shared set of live capital and execution gates.

    The same routine is used when Pro output is assembled and when the UI
    recalculates candidate amounts.  It uses committed cost rather than market
    value, includes live drafts/pending buys, enforces single-index, correlated
    bucket and theme limits, and funds at most the configured number of rows in
    one correlation bucket.  Research tilt, timing and execution remain separate:
    a positive research view may legally receive zero yuan while waiting for a
    pullback or fresh data.
    """
    if not candidates:
        return []
    capital = active_capital_summary(db)
    exposures = active_index_exposures(db)
    active_limit = float(capital["limit"])
    single_limit = float(db.setting("single_position_limit", "2000"))
    bucket_limit = float(db.setting("correlation_bucket_limit", "3000"))
    max_themes = max(0, int(float(db.setting("max_active_themes", "4"))))
    funded_bucket_max = max(1, int(float(db.setting("ranking_funded_cluster_max", "1"))))
    confidence_min = float(db.setting("ranking_data_confidence_min", "70"))

    reservations_by_index = defaultdict(float, {
        str(key): float(value) for key, value in capital.get("reservations_by_index", {}).items()
    })
    reservations_by_bucket = defaultdict(float, {
        str(key): float(value) for key, value in capital.get("reservations_by_bucket", {}).items()
    })
    reservation_counts_by_bucket = defaultdict(int, {
        str(key): int(value) for key, value in capital.get("reservation_counts_by_bucket", {}).items()
    })
    available_cash = float(capital.get("cash_available", 0) or 0)

    # When editing/re-adding a displayed candidate, release its own existing
    # draft before recalculating the coherent candidate plan.  Otherwise the
    # row would appear to compete against its own reservation.
    if release_existing_candidate_drafts:
        candidate_ids = [int(row["id"]) for row in candidates if row.get("id") not in (None, "")]
        if candidate_ids:
            marks = ",".join("?" for _ in candidate_ids)
            released = db.rows(
                f"""SELECT d.candidate_id,d.fund_code,d.planned_amount
                       FROM decision_drafts d
                      WHERE d.status='草稿' AND d.action IN ('买入','定投','转换转入')
                        AND d.candidate_id IN ({marks})""",
                candidate_ids,
            )
            for draft in released:
                amount = max(0.0, float(draft.get("planned_amount") or 0))
                identity = fund_exposure_identity(db, str(draft.get("fund_code") or ""))
                bucket = identity.get("bucket") or identity["index_key"]
                reservations_by_index[identity["index_key"]] = max(
                    0.0, reservations_by_index[identity["index_key"]] - amount
                )
                reservations_by_bucket[bucket] = max(
                    0.0, reservations_by_bucket[bucket] - amount
                )
                if amount > 0:
                    reservation_counts_by_bucket[bucket] = max(
                        0, reservation_counts_by_bucket[bucket] - 1
                    )
                available_cash += amount

    index_totals = defaultdict(float)
    bucket_totals = defaultdict(float)
    themes: set[str] = set()
    for index_key, exposure in exposures.items():
        amount = max(0.0, float(exposure.get("cost") or 0))
        bucket = str(exposure.get("bucket") or index_key)
        index_totals[index_key] += amount
        bucket_totals[bucket] += amount
        if amount > 0:
            themes.add(index_key)
    for index_key, amount in reservations_by_index.items():
        index_totals[index_key] += max(0.0, float(amount))
        if amount > 0:
            themes.add(index_key)
    for bucket, amount in reservations_by_bucket.items():
        bucket_totals[bucket] += max(0.0, float(amount))

    # Existing live drafts and pending buys count as already funded choices.
    # This makes one-row actions obey the same per-bucket rule as the full UI
    # batch instead of letting a second same-bucket draft slip through.
    funded_by_bucket: defaultdict[str, int] = defaultdict(int, reservation_counts_by_bucket)
    allocated: list[dict[str, Any]] = []
    for source in candidates:
        row = dict(source)
        identity = candidate_exposure_identity(db, row)
        index_key = identity["index_key"]
        bucket = str(row.get("correlation_bucket") or identity.get("bucket") or index_key)
        row["correlation_bucket"] = bucket
        try:
            requested_pct = max(0.0, float(row.get("allocation_pct") or 0))
        except (TypeError, ValueError):
            requested_pct = 0.0
        original_amount = max(0.0, float(row.get("suggested_amount") or 0))
        target = active_limit * requested_pct / 100 if requested_pct > 0 else original_amount

        research_tilt = str(row.get("research_tilt") or "观察")
        timing = str(row.get("timing_status") or "等待回调")
        execution = str(row.get("execution_status") or "不可执行")
        fund_code = str(row.get("fund_code") or "").strip()
        confidence = float(row.get("data_confidence") or 0)
        freshness_ok = bool(row.get("freshness_ok", True))
        reason = ""
        if research_tilt not in POSITIVE_RESEARCH_TILTS:
            reason = "研究结论未达到正向三档"
        elif timing != "可以买":
            reason = f"择时状态为{timing or '等待确认'}"
        elif not freshness_ok:
            execution = "行情待更新"
            reason = "行情截止日不是最近已完成交易日"
        elif confidence < confidence_min:
            execution = "数据待补齐"
            reason = f"数据置信度{confidence:.1f}低于{confidence_min:.1f}"
        elif not fund_code:
            execution = "基金待绑定"
            reason = "没有严格可执行的被动C类基金"
        elif execution != "可执行":
            reason = execution or "不可执行"
        elif requested_pct <= 0 and original_amount <= 0:
            reason = "Pro没有给出正目标比例"
        elif funded_by_bucket[bucket] >= funded_bucket_max:
            reason = f"同一相关性桶最多{funded_bucket_max}项获得正金额"
        elif index_key not in themes and max_themes >= 0 and len(themes) >= max_themes:
            reason = f"同时主题数已达到{max_themes}项上限"

        amount = 0.0
        if not reason:
            desired = max(0.0, target - index_totals[index_key])
            room_single = max(0.0, single_limit - index_totals[index_key])
            room_bucket = max(0.0, bucket_limit - bucket_totals[bucket])
            amount = round(min(desired, room_single, room_bucket, available_cash), 2)
            if amount <= 0:
                limits = []
                if available_cash <= 0:
                    limits.append("实际未投入现金不足")
                if room_single <= 0:
                    limits.append("单指数额度已满")
                if room_bucket <= 0:
                    limits.append("相关性桶额度已满")
                if desired <= 0:
                    limits.append("当前成本与预占已达到目标")
                reason = "、".join(limits) or "没有可新增金额"
            else:
                available_cash -= amount
                index_totals[index_key] += amount
                bucket_totals[bucket] += amount
                themes.add(index_key)
                funded_by_bucket[bucket] += 1

        row["execution_status"] = execution
        row["execution_reason"] = reason
        row["suggested_amount"] = amount
        row["allocation_pct"] = round(amount / active_limit * 100, 2) if active_limit else 0.0
        allocated.append(row)
    return allocated

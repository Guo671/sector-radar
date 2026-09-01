from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import APP_VERSION, TASK_DIR, local_now
from .ranking import recommendation_label
from .universe import UNIVERSE_VERSION


PROTOCOL_VERSION = "SECTOR_RADAR_RESULT_V3"
START_MARKER = f"<<<{PROTOCOL_VERSION}>>>"
# Keep the historical marker literal.  It must remain readable after the app's
# configured protocol is upgraded to V3 (or a later writable protocol).
LEGACY_START_MARKER = "<<<SECTOR_RADAR_RESULT_V2>>>"
END_MARKER = "<<<END_SECTOR_RADAR_RESULT>>>"

RESEARCH_TILT_LABELS = (
    "非常推荐买",
    "推荐买",
    "有点推荐买",
    "观察",
    "不太推荐买",
    "不推荐买",
    "千万别买",
)
TIMING_STATUS_LABELS = ("可以买", "等待回调", "不宜介入")
EXECUTION_STATUS_LABELS = (
    "可执行",
    "基金待绑定",
    "申购状态待复核",
    "行情待更新",
    "数据待补齐",
    "不可执行",
)
AVOID_RISK_LEVELS = ("高", "中", "低")
# CSI publishes both six-digit numeric identifiers (for example 931071) and
# official H-series identifiers made of ``H`` plus five digits (for example
# H30217).  Both are first-class CSI identities and use the same .CSI display
# suffix throughout the catalog, market-data and fund-discovery pipelines.
CSI_DISPLAY_CODE_RE = re.compile(r"(?:\d{6}|H\d{5})\.CSI", re.IGNORECASE)
V3_CANDIDATE_SCORE_FIELDS = (
    "trend_score",
    "opportunity_score",
    "recommendation_score",
    "entry_score",
    "crowding_risk_score",
    "data_confidence",
)
# Every V3 audit row represents one researched catalog item, including the
# items Pro did not place on the final main list.  Requiring both the four
# independent dimensions and the two derived composites prevents the former
# state where 72/82 items had no user-visible conclusion at all.
V3_AUDIT_SCORE_FIELDS = (
    "trend_score",
    "entry_score",
    "crowding_risk_score",
    "data_confidence",
    "opportunity_score",
    "recommendation_score",
)
V3_OPTIONAL_SCORE_FIELDS = (
    "score",
    "base_score",
    "trend_score",
    "trend_opportunity",
    "entry_quality",
    "catalyst_score",
    "catalyst_quality",
    "relative_strength_score",
    "relative_strength",
    "volume_breadth_score",
    "volume_breadth_quality",
    "data_confidence_score",
    "crowding_risk",
)

# A task package may be pasted into an arbitrary web chat.  Export only values
# that describe the investment policy.  In particular, never serialise API
# credentials, encrypted blobs, provider endpoints, local paths, sync cursors or
# device-specific state merely because they happen to live in ``settings``.
TASK_PACKAGE_STRATEGY_SETTING_KEYS = frozenset({
    "active_capital_limit",
    "single_position_limit",
    "correlation_bucket_limit",
    "max_active_themes",
    "soft_stop_gross_pct",
    "hard_stop_gross_pct",
    "portfolio_pause_pct",
    "portfolio_stop_pct",
    "consecutive_loss_pause",
    "internal_cutoff",
    "risk_profile",
})


def task_package_strategy_settings(settings: dict[str, Any]) -> dict[str, str]:
    """Return the explicit, non-secret strategy subset safe for web task packs."""
    return {
        key: str(settings[key])
        for key in sorted(TASK_PACKAGE_STRATEGY_SETTING_KEYS)
        if key in settings and settings[key] not in (None, "")
    }


@dataclass
class ParseResult:
    ok: bool
    payload: dict[str, Any] | None
    errors: list[str]
    warnings: list[str]
    read_only: bool = False
    protocol_version: str = ""


def _clean_json_block(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _canonical_csi_code(row: dict[str, Any]) -> str:
    return str(
        row.get("csi_index_code")
        or row.get("ant_index_code")
        or row.get("official_index_code")
        or ""
    ).upper().strip()


def _validate_bounded_score(
    errors: list[str], value: Any, label: str, *, minimum: float = 0, maximum: float = 100
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label}不是有效数字。")
        return
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        errors.append(f"{label}必须是{minimum:g}—{maximum:g}之间的有限数字。")


def _result_block(text: str) -> tuple[str | None, str]:
    # V3 is the only writable protocol.  V2 remains parseable so old research
    # can still be displayed, but callers must respect ParseResult.read_only.
    markers = tuple(dict.fromkeys((START_MARKER, LEGACY_START_MARKER)))
    for marker in markers:
        if marker in text and END_MARKER in text.split(marker, 1)[1]:
            return marker, text.split(marker, 1)[1].split(END_MARKER, 1)[0]
    return None, ""


def parse_result(
    text: str,
    known_funds: set[str] | None = None,
    max_capital: float = 8000,
    expected_universe: list[dict[str, Any]] | None = None,
) -> ParseResult:
    errors: list[str] = []
    warnings: list[str] = []
    marker, raw = _result_block(text)
    if marker is None:
        return ParseResult(False, None, ["没有找到完整的板块雷达结果标记。"], [])
    try:
        payload = json.loads(_clean_json_block(raw))
    except json.JSONDecodeError as exc:
        return ParseResult(False, None, [f"JSON格式错误：第{exc.lineno}行，第{exc.colno}列。"], [])
    if not isinstance(payload, dict):
        return ParseResult(False, None, ["结果块顶层必须是JSON对象。"], [])
    payload_protocol = str(payload.get("protocol_version") or "").strip()
    is_v3 = payload_protocol == "3.0"
    is_legacy_v2 = payload_protocol == "2.0"
    if not is_v3 and not is_legacy_v2:
        errors.append("协议版本必须为3.0；仅历史2.0结果可按只读方式打开。")
    if is_legacy_v2:
        warnings.append("这是旧V2研究结果，仅供历史只读查看；不能生成行动、加入草稿或覆盖V3研究。")
    if is_v3 and marker != START_MARKER:
        errors.append("V3结果必须使用V3结果标记；不能借用历史V2标记写入。")
    if is_legacy_v2 and marker != LEGACY_START_MARKER:
        errors.append("历史V2结果必须使用固定V2标记，并且只能按只读方式打开。")
    required = {
        "protocol_version": str,
        "task_kind": str,
        "task_id": str,
        "task_status": str,
        "universe_version": str,
        "data_cutoff": str,
        "market_regime": str,
        "universe_total": int,
        "analyzed_total": int,
        "coverage_by_category": list,
        "research_method": str,
        "tool_calls_summary": list,
        "audit": list,
        "candidates": list,
        "avoid_list": list,
        "fund_updates": list,
        "questions_for_intraday": list,
        "risk_flags": list,
        "sources": list,
    }
    for key, kind in required.items():
        if key not in payload:
            errors.append(f"缺少字段：{key}")
        elif not isinstance(payload[key], kind):
            errors.append(f"字段{key}应为{kind.__name__}。")
    if errors:
        return ParseResult(
            False, payload, errors, warnings,
            read_only=is_legacy_v2, protocol_version=payload_protocol,
        )
    if expected_universe is None:
        return ParseResult(
            False,
            payload,
            ["导入结果时没有附带本轮CSI代表行业目录，无法判断模型是否完整覆盖；请重新生成任务包。"],
            warnings,
            read_only=is_legacy_v2,
            protocol_version=payload_protocol,
        )
    expected = expected_universe
    expected_map = {str(row["index_code"]): row for row in expected}
    expected_counts: dict[str, int] = {}
    for row in expected:
        category = str(row["category"])
        expected_counts[category] = expected_counts.get(category, 0) + 1
    if payload["task_kind"] != "night":
        errors.append("这里只接受夜间研究结果。")
    if payload["task_status"] != "COMPLETE":
        errors.append("任务未标记COMPLETE；不完整研究不能生成购买候选。")
    if payload["universe_version"] != UNIVERSE_VERSION:
        errors.append("研究宇宙版本不匹配。")
    if payload["universe_total"] != len(expected) or payload["analyzed_total"] != len(expected):
        errors.append(f"全市场覆盖不足：应完成{len(expected)}/{len(expected)}，实际{payload['analyzed_total']}/{payload['universe_total']}。")
    coverage = {str(row.get("category")): row for row in payload["coverage_by_category"] if isinstance(row, dict)}
    for category, count in expected_counts.items():
        item = coverage.get(category, {})
        if int(item.get("expected", -1) or -1) != count or int(item.get("analyzed", -1) or -1) != count:
            errors.append(f"分类覆盖不完整：{category}应为{count}/{count}。")
    if len(payload["research_method"].strip()) < 20:
        errors.append("research_method过短，没有说明数据来源和计算方法。")
    if not payload["tool_calls_summary"]:
        errors.append("tool_calls_summary为空，无法证明执行过数据或联网工具。")
    source_ids: set[str] = set()
    source_hosts: set[str] = set()
    if len(payload["sources"]) < 5:
        errors.append("来源不足5项。")
    for idx, source in enumerate(payload["sources"], start=1):
        if not isinstance(source, dict):
            errors.append(f"第{idx}个来源不是对象。")
            continue
        for key in ("id", "title", "url", "retrieved_at", "supports"):
            if not source.get(key):
                errors.append(f"第{idx}个来源缺少{key}。")
        if source.get("id"):
            source_ids.add(str(source["id"]))
        host = urlparse(str(source.get("url", ""))).hostname
        if host:
            source_hosts.add(host.lower())
    if len(source_hosts) < 3:
        errors.append("来源域名不足3个，缺少交叉核验。")
    if len(payload["audit"]) != len(expected):
        errors.append(f"逐项审计数量错误：应为{len(expected)}，实际{len(payload['audit'])}。")
    seen: set[str] = set()
    for idx, item in enumerate(payload["audit"], start=1):
        if not isinstance(item, dict):
            errors.append(f"第{idx}个审计项不是对象。")
            continue
        universe_id = str(item.get("id", ""))
        if universe_id in seen:
            errors.append(f"审计ID重复：{universe_id}")
        seen.add(universe_id)
        expected_item = expected_map.get(universe_id)
        if not expected_item:
            errors.append(f"出现研究宇宙之外的ID：{universe_id}")
            continue
        if item.get("name") != expected_item["index_name"] or item.get("category") != expected_item["category"]:
            errors.append(f"审计项{universe_id}的名称或分类被改动。")
        if item.get("status") != "ANALYZED":
            errors.append(f"审计项{universe_id}未完成分析。")
        for key in ("proxy_code", "proxy_name", "volume_signal", "breadth_signal", "position_20d", "crowding"):
            if not str(item.get(key, "")).strip():
                errors.append(f"审计项{universe_id}缺少{key}。")
        returns = item.get("returns_pct", {})
        for key in ("d1", "d3", "d5", "d10", "d20"):
            value = returns.get(key) if isinstance(returns, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"审计项{universe_id}的{key}不是有效数字。")
        refs = item.get("source_ids", [])
        if not isinstance(refs, list) or not refs:
            errors.append(f"审计项{universe_id}没有来源引用。")
        else:
            for ref in refs:
                if str(ref) not in source_ids:
                    errors.append(f"审计项{universe_id}引用了不存在的来源{ref}。")
        if is_v3:
            required_audit_fields = (
                "research_tilt", "timing_status", "execution_status",
                *V3_AUDIT_SCORE_FIELDS,
            )
            for key in required_audit_fields:
                if key not in item or item.get(key) in (None, "", []):
                    errors.append(f"审计项{universe_id}缺少{key}。")
            if item.get("research_tilt") not in RESEARCH_TILT_LABELS:
                errors.append(f"审计项{universe_id}的research_tilt不在七档枚举中。")
            if item.get("timing_status") not in TIMING_STATUS_LABELS:
                errors.append(f"审计项{universe_id}的timing_status无效。")
            if item.get("execution_status") not in EXECUTION_STATUS_LABELS:
                errors.append(f"审计项{universe_id}的execution_status无效。")
            for score_key in V3_AUDIT_SCORE_FIELDS:
                _validate_bounded_score(
                    errors, item.get(score_key), f"审计项{universe_id}的{score_key}"
                )
            recommendation_score = item.get("recommendation_score")
            if (
                isinstance(recommendation_score, (int, float))
                and not isinstance(recommendation_score, bool)
                and math.isfinite(float(recommendation_score))
                and item.get("research_tilt") != recommendation_label(float(recommendation_score))
            ):
                errors.append(
                    f"审计项{universe_id}的推荐分{float(recommendation_score):.3f}"
                    f"应对应“{recommendation_label(float(recommendation_score))}”，"
                    f"不能写成“{item.get('research_tilt')}”。"
                )
            for score_key in (*V3_CANDIDATE_SCORE_FIELDS, *V3_OPTIONAL_SCORE_FIELDS):
                if score_key in item and score_key not in V3_AUDIT_SCORE_FIELDS:
                    _validate_bounded_score(
                        errors, item.get(score_key), f"审计项{universe_id}的{score_key}"
                    )
    for universe_id in expected_map:
        if universe_id not in seen:
            errors.append(f"缺少审计ID：{universe_id}")
    if is_v3 and len(payload["candidates"]) > 10:
        errors.append(f"V3最终主榜最多10个候选，实际收到{len(payload['candidates'])}个。")
    codes: set[str] = set()
    candidate_ids: set[str] = set()
    allocated = 0.0
    for idx, candidate in enumerate(payload["candidates"], start=1):
        if not isinstance(candidate, dict):
            errors.append(f"第{idx}个候选不是对象。")
            continue
        required_candidate_fields = [
            "universe_id", "index_code", "sector_name", "thesis", "risk",
            "buy_conditions", "abandon_conditions",
        ]
        if is_v3:
            required_candidate_fields.extend((
                "research_tilt", "timing_status", "execution_status",
                *V3_CANDIDATE_SCORE_FIELDS,
            ))
        else:
            required_candidate_fields.append("status")
        for key in required_candidate_fields:
            if key not in candidate or candidate[key] in (None, "", []):
                errors.append(f"第{idx}个候选缺少{key}。")
        code = str(candidate.get("index_code", "")).upper().strip()
        if code in codes:
            errors.append(f"候选指数重复：{code}")
        codes.add(code)
        candidate_uid = str(candidate.get("universe_id") or "").strip()
        if candidate_uid in candidate_ids:
            errors.append(f"候选目录ID重复：{candidate_uid}")
        candidate_ids.add(candidate_uid)
        candidate_expected = expected_map.get(candidate_uid)
        csi_code = ""
        if not candidate_expected:
            errors.append(f"第{idx}个候选引用了本轮目录之外的ID：{candidate_uid}")
        else:
            csi_code = _canonical_csi_code(candidate_expected)
            if csi_code and code != csi_code:
                errors.append(
                    f"第{idx}个候选的指数代码应为本轮CSI代码{csi_code}，不能写成行情代理或内部ID。"
                )
            if is_v3 and not CSI_DISPLAY_CODE_RE.fullmatch(csi_code):
                errors.append(
                    f"第{idx}个候选对应目录缺少规范的中证.CSI代码"
                    "（六位数字或H加五位数字）。"
                )
            if str(candidate.get("sector_name") or "").strip() != str(candidate_expected.get("index_name") or "").strip():
                errors.append(f"第{idx}个候选的sector_name与本轮目录不一致。")
        if is_v3:
            if candidate.get("research_tilt") not in RESEARCH_TILT_LABELS:
                errors.append(f"第{idx}个候选的research_tilt不在七档枚举中。")
            if candidate.get("timing_status") not in TIMING_STATUS_LABELS:
                errors.append(f"第{idx}个候选的timing_status无效。")
            if candidate.get("execution_status") not in EXECUTION_STATUS_LABELS:
                errors.append(f"第{idx}个候选的execution_status无效。")
            for score_key in V3_CANDIDATE_SCORE_FIELDS:
                _validate_bounded_score(
                    errors, candidate.get(score_key), f"第{idx}个候选的{score_key}"
                )
            recommendation_score = candidate.get("recommendation_score")
            if (
                isinstance(recommendation_score, (int, float))
                and not isinstance(recommendation_score, bool)
                and math.isfinite(float(recommendation_score))
                and candidate.get("research_tilt") != recommendation_label(float(recommendation_score))
            ):
                errors.append(
                    f"第{idx}个候选的推荐分{float(recommendation_score):.3f}"
                    f"应对应“{recommendation_label(float(recommendation_score))}”，"
                    f"不能写成“{candidate.get('research_tilt')}”。"
                )
            for score_key in V3_OPTIONAL_SCORE_FIELDS:
                if score_key in candidate:
                    _validate_bounded_score(
                        errors, candidate.get(score_key), f"第{idx}个候选的{score_key}"
                    )
            if "score_adjustment" in candidate:
                _validate_bounded_score(
                    errors, candidate.get("score_adjustment"),
                    f"第{idx}个候选的score_adjustment", minimum=0, maximum=0,
                )
        try:
            allocated += float(candidate.get("suggested_amount", 0) or 0)
        except (TypeError, ValueError):
            errors.append(f"第{idx}个候选的suggested_amount不是数字。")
        if "allocation_pct" in candidate:
            try:
                allocation_pct = float(candidate.get("allocation_pct", 0) or 0)
                if not 0 <= allocation_pct <= 100:
                    errors.append(f"第{idx}个候选的allocation_pct超出0—100。")
            except (TypeError, ValueError):
                errors.append(f"第{idx}个候选的allocation_pct不是数字。")
        fund_code = str(candidate.get("fund_code", "")).strip()
        if fund_code and not re.fullmatch(r"\d{6}", fund_code):
            errors.append(f"基金代码格式错误：{fund_code}")
        if fund_code and known_funds is not None and fund_code not in known_funds:
            warnings.append(f"基金{fund_code}尚不在本地基金库，导入后需核验费率。")
        if fund_code and candidate_expected:
            raw_whitelist = candidate_expected.get("strict_c_funds")
            whitelist = {
                str(item.get("fund_code") or "").strip(): item
                for item in raw_whitelist
                if isinstance(raw_whitelist, list) and isinstance(item, dict)
                and str(item.get("fund_code") or "").strip()
            } if isinstance(raw_whitelist, list) else {}
            if fund_code not in whitelist:
                errors.append(
                    f"第{idx}个候选基金{fund_code}不在{csi_code or candidate_uid}本轮冻结的"
                    "同指数、被动、C类、可申购白名单中。"
                )
            else:
                expected_name = str(whitelist[fund_code].get("fund_name") or "").strip()
                actual_name = str(candidate.get("fund_name") or "").strip()
                if expected_name and actual_name != expected_name:
                    errors.append(
                        f"第{idx}个候选基金{fund_code}的完整名称应为“{expected_name}”。"
                    )
        if candidate_expected and not int(candidate_expected.get("has_buyable_fund") or 0):
            try:
                proposed = float(candidate.get("suggested_amount", 0) or 0)
            except (TypeError, ValueError):
                proposed = 0
            if fund_code or proposed > 0:
                errors.append(
                    f"{csi_code or candidate_uid}尚无已核验的同指数被动基金，只能研究观察，不能填写基金或买入金额。"
                )
    if allocated > max_capital:
        errors.append(f"候选建议金额合计{allocated:.0f}元，超过{max_capital:.0f}元上限。")
    if is_v3:
        avoid_ids: set[str] = set()
        avoid_codes: set[str] = set()
        for idx, avoided in enumerate(payload["avoid_list"], start=1):
            if not isinstance(avoided, dict):
                errors.append(f"第{idx}个回避项不是对象。")
                continue
            for key in ("universe_id", "index_code", "sector_name", "reason", "risk_level"):
                if key not in avoided or not str(avoided.get(key) or "").strip():
                    errors.append(f"第{idx}个回避项缺少{key}。")
            universe_id = str(avoided.get("universe_id") or "").strip()
            code = str(avoided.get("index_code") or "").upper().strip()
            if universe_id in avoid_ids:
                errors.append(f"回避项目录ID重复：{universe_id}")
            if code in avoid_codes:
                errors.append(f"回避项CSI代码重复：{code}")
            avoid_ids.add(universe_id)
            avoid_codes.add(code)
            expected_item = expected_map.get(universe_id)
            if not expected_item:
                errors.append(f"第{idx}个回避项引用了本轮目录之外的ID：{universe_id}")
            else:
                expected_code = _canonical_csi_code(expected_item)
                if not CSI_DISPLAY_CODE_RE.fullmatch(expected_code):
                    errors.append(
                        f"第{idx}个回避项对应目录缺少规范的中证.CSI代码"
                        "（六位数字或H加五位数字）。"
                    )
                if code != expected_code:
                    errors.append(f"第{idx}个回避项的指数代码应为本轮CSI代码{expected_code}。")
                if str(avoided.get("sector_name") or "").strip() != str(expected_item.get("index_name") or "").strip():
                    errors.append(f"第{idx}个回避项的sector_name与本轮目录不一致。")
            if avoided.get("risk_level") not in AVOID_RISK_LEVELS:
                errors.append(f"第{idx}个回避项的risk_level只能是高、中、低。")
    holding_actions = payload.get("holding_actions")
    if holding_actions is not None:
        if not isinstance(holding_actions, list):
            errors.append("holding_actions应为列表。")
        else:
            holding_codes: set[str] = set()
            for idx, action in enumerate(holding_actions, start=1):
                if not isinstance(action, dict):
                    errors.append(f"第{idx}个持仓动作不是对象。")
                    continue
                code = str(action.get("fund_code") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    errors.append(f"第{idx}个持仓动作基金代码无效：{code}")
                if code in holding_codes:
                    errors.append(f"持仓动作基金代码重复：{code}")
                holding_codes.add(code)
                if str(action.get("action") or "") not in {"持有", "继续定投", "减仓", "清仓"}:
                    errors.append(f"基金{code}的持仓动作无效。")
                try:
                    sell_pct = float(action.get("sell_pct", 0) or 0)
                    if not 0 <= sell_pct <= 100:
                        errors.append(f"基金{code}的卖出比例超出0—100。")
                except (TypeError, ValueError):
                    errors.append(f"基金{code}的卖出比例不是数字。")
    return ParseResult(
        not errors,
        payload,
        errors,
        warnings,
        read_only=is_legacy_v2,
        protocol_version=payload_protocol,
    )


def result_schema_example(universe: list[dict[str, Any]] | None = None) -> str:
    if universe is None:
        raise ValueError("生成结果协议时必须传入本轮CSI代表行业目录。")
    counts: dict[str, int] = {}
    for row in universe:
        counts[str(row["category"])] = counts.get(str(row["category"]), 0) + 1
    example = {
        "protocol_version": "3.0",
        "task_kind": "night",
        "task_id": "YYYY-MM-DD-night",
        "task_status": "COMPLETE",
        "universe_version": UNIVERSE_VERSION,
        "data_cutoff": "YYYY-MM-DD HH:MM +08:00",
        "market_regime": "趋势进攻/轮动震荡/全面防守",
        "universe_total": len(universe),
        "analyzed_total": len(universe),
        "coverage_by_category": [{"category": k, "expected": v, "analyzed": v} for k, v in counts.items()],
        "research_method": "说明实际使用的数据工具、行情来源、统一交易日和计算方法。",
        "tool_calls_summary": [{"tool": "", "purpose": "", "items": 0}],
        "audit": [{
            "id": row["index_code"], "name": row["index_name"], "category": row["category"],
            "proxy_code": "", "proxy_name": "", "returns_pct": {"d1": None, "d3": None, "d5": None, "d10": None, "d20": None},
            "volume_signal": "", "breadth_signal": "", "position_20d": "", "crowding": "", "catalyst": "",
            "research_tilt": "非常推荐买/推荐买/有点推荐买/观察/不太推荐买/不推荐买/千万别买",
            "timing_status": "可以买/等待回调/不宜介入",
            "execution_status": "可执行/基金待绑定/申购状态待复核/行情待更新/数据待补齐/不可执行",
            "trend_score": 0,
            "entry_score": 0,
            "crowding_risk_score": 0,
            "data_confidence": 0,
            "opportunity_score": 0,
            "recommendation_score": 0,
            "status": "ANALYZED", "source_ids": [], "note": "",
        } for row in universe],
        "market_summary": "简明结论",
        "candidates": [{
            "rank": 1,
            "universe_id": "本轮目录内部审计ID",
            "index_code": "中证.CSI代码（六位数字或H加五位数字）",
            "sector_name": "指数全名",
            "fund_code": "已核验同指数场外被动基金代码；没有则留空",
            "fund_name": "基金全名及份额类别",
            "research_tilt": "非常推荐买/推荐买/有点推荐买/观察/不太推荐买/不推荐买/千万别买",
            "timing_status": "可以买/等待回调/不宜介入",
            "execution_status": "可执行/基金待绑定/申购状态待复核/行情待更新/数据待补齐/不可执行",
            "trend_score": 0,
            "opportunity_score": 0,
            "recommendation_score": 0,
            "entry_score": 0,
            "crowding_risk_score": 0,
            "data_confidence": 0,
            "suggested_amount": 0,
            "correlation_bucket": "相关方向",
            "thesis": "选择逻辑",
            "catalyst": "催化",
            "crowding": "低/中/高及证据",
            "risk": "主要风险",
            "buy_conditions": "次日必须满足的条件",
            "abandon_conditions": "次日出现什么就放弃"
        }],
        "avoid_list": [{
            "universe_id": "本轮目录内部审计ID",
            "index_code": "中证.CSI代码（六位数字或H加五位数字）",
            "sector_name": "指数全名",
            "reason": "非空回避理由",
            "risk_level": "高/中/低",
        }],
        "fund_updates": [],
        "questions_for_intraday": ["当前涨跌幅", "今开", "最高", "最低", "成交额", "最近十分钟走势"],
        "risk_flags": [],
        "sources": [{"id": "S1", "title": "", "url": "", "retrieved_at": "", "supports": ""}]
    }
    return f"{START_MARKER}\n{json.dumps(example, ensure_ascii=False, indent=2)}\n{END_MARKER}"


def build_task_package(db, task_type: str = "night") -> tuple[str, Path]:
    from .platform_catalog import PlatformCatalogService
    from .services import latest_current_radar_run

    all_settings = db.settings()
    settings = task_package_strategy_settings(all_settings)
    funds = db.rows("SELECT * FROM funds ORDER BY fund_code")
    tiers = db.rows("SELECT fund_code,min_days,max_days,redeem_fee_pct FROM fee_tiers ORDER BY fund_code,min_days")
    holdings = db.rows(
        """SELECT h.*, f.fund_name, f.index_code, f.index_name, f.correlation_bucket,
                  f.purchase_fee_pct FROM holdings h JOIN funds f USING(fund_code)
           WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1
           ORDER BY h.sleeve,h.fund_code"""
    )
    universe = PlatformCatalogService(db).radar_universe()
    if not universe:
        raise ValueError(
            "当前没有通过准入的CSI代表行业指数，不能生成会退回历史固定板块池的任务包。"
            "请先在“中证代表目录”重新检查官方身份和精确行情映射。"
        )
    trades = db.rows("SELECT * FROM trades WHERE COALESCE(counts_toward_active_capital,0)=1 ORDER BY trade_date DESC,id DESC LIMIT 30")
    # Historical 186/66-item and cross-day research remains readable in the app,
    # but it must never be copied into a fresh web task as if it described today's
    # exact catalog.  Use the same actionable-run gate as the decision page.
    latest = latest_current_radar_run(db, require_today=True)
    latest_candidates = []
    if latest:
        latest_candidates = db.rows("SELECT * FROM candidates WHERE task_id=? ORDER BY rank_no", (latest["task_id"],))

    profile = {
        "software": f"🐱板块雷达 {APP_VERSION}",
        "task_type": task_type,
        "generated_at": local_now().isoformat(timespec="minutes"),
        "non_negotiable_constraints": [
            "仅买软件已经精确绑定到当前CSI指数的场外被动指数基金/ETF联接基金；不买个股、不买场内ETF。",
            "这是DeepSeek API不可用时的备用任务包；本文件必须让无记忆的新ChatGPT独立完成任务。",
            f"A股主动资金当前上限{settings.get('active_capital_limit', '8000')}元，但允许0元和不满仓；若软件设置以后改变，以设置值为准。",
            "盘中实时数字以用户14:40—14:47在实际购买平台看到的数据为准；14:52为内部截止。",
            "不得只分析少数热门板块；必须覆盖research_universe中的全部CSI叶子行业并报告覆盖数量。",
            "费率逐只基金计算，不得把所有基金默认成1.5%。",
            "所有基金必须写六位代码+完整名称+份额类别；无法核实则标记待核实。",
        ],
        "strategy": settings,
        "historical_guardrails": [
            "截至2026-08-04，账本口径总损益约-1402.45元；这说明新系统必须以费后结果评价，不能只看方向是否猜对。",
            "已结束持有段中，1—7天样本表现很差；若不足7日赎回费为1.5%，2—6天卖出默认只用于止损、逻辑失效或明显过热。",
            "过去大额亏损集中于少数重仓错误；必须检查单指数上限和高相关方向合计暴露。",
            "用户不接受先以4000元试运行，系统从第一天按当前8000元额度运行；但没有合格机会时可以0仓位。",
            "用户愿意买当天上涨的行业，但连续上涨、远离均线或成交高潮不能被当成自动买入理由。",
        ],
        "mandatory_coverage_matrix": [
            "能源", "原材料", "工业", "可选消费", "主要消费",
            "医药卫生", "金融地产", "信息技术", "通信服务", "公用事业",
        ],
        "fund_library": funds,
        "fee_tiers": tiers,
        "current_holdings": holdings,
        "research_universe": universe,
        "recent_trades": trades,
        "previous_run": latest,
        "previous_candidates": latest_candidates,
    }
    if task_type == "night":
        instruction = """
你是A股场外指数基金短周期研究员。请联网核查数据，完成今天以前的全市场横向扫描，为下一交易日准备候选。

研究边界与覆盖要求：
1. research_universe是软件本轮已经核验通过的CSI叶子行业目录；必须逐项覆盖并报告完成数，不得自行添加目录外指数，也不得退回历史186项固定池。
2. 目录已排除被子行业替代的宽泛父级；同一allocation_cluster中的近似子行业属于一个相关仓位簇，不得重复配置为多份独立仓位。
3. 输入中的index_code是内部审计ID；面向用户的候选代码必须填写同一项的csi_index_code，不能填写行情证券ID或内部ID。
4. strict_c_funds是该指数本轮冻结的唯一基金白名单；候选基金只能从同一目录项的这个列表中选择，并逐字复制六位代码和完整名称。has_buyable_fund=0或strict_c_funds为空时，基金代码必须留空、建议金额必须为0，不得临时联网猜相似指数、A类或未核验产品。
5. 同时识别“强势但未过热”和“热门但风险很大”；不能把当日涨幅榜直接当推荐榜。
6. 至少比较1/3/5/10/20交易日强弱、成交变化、位置、连续上涨和催化可靠性；公开指数没有成分表时必须明示价格代理，不得冒充成分股宽度。
7. 对基金费率、指数代码、基金代码等易变事实，优先引用指数公司、基金公司、证监会/交易所等权威来源。不要编造分钟行情。
8. 将市场判为趋势进攻、轮动震荡或全面防守；全部叶子行业都要完成审计与排序，最终行动候选只保留前10。没有合格买点或没有同指数基金时，把配置比例和金额设为0并明确原因。

先写一份人类可读的中文研究报告，最后严格输出结果块。结果块必须是有效JSON；不允许注释、尾逗号、Markdown表格或省略号。
"""
    elif task_type == "intraday":
        instruction = """
你是14:45盘中决策复核员。夜间候选和用户刚输入的盘中数据已在本任务包中。不要重新做泛泛市场综述。
逐个检查：当前涨跌幅、今开、最高、最低、成交额、最近十分钟走势、夜间买入条件、放弃条件、当天是否追高、相关板块总仓位、真实费率与可用资金。
给出买入/继续观察/放弃/卖出的明确建议和金额；不得超过资金及相关性上限。14:52以后原则上不开新仓。
先给简明说明，最后按相同协议输出有效JSON结果块。
"""
    elif task_type == "fund_check":
        instruction = """
你是基金资料核查员。只核查候选CSI指数对应的普通场外被动指数基金/ETF联接基金：基金代码、完整名称、份额类别、精确跟踪指数、基金规模、公开申购状态、申购限制、买入费和按自然日分档的赎回费。优先基金公司官方文件并附直接链接。相似指数只能列为候补，不得冒充同一指数。
输出人类可读报告，并在结果块fund_updates中提交建议更新；不要擅自改变本地账本。
"""
    else:
        instruction = """
上一次回复未通过软件校验。请根据本任务包中的错误清单，只修复格式与遗漏，不要改变已经有来源支持的研究结论。最后重新输出完整、有效的JSON结果块。
"""

    md = f"""# 🐱 板块雷达自包含AI任务包

> 这是一份无状态任务包。即使你没有任何历史对话或记忆，也必须仅依靠本文件完成任务。

## 本轮指令
{instruction.strip()}

## 权威本地状态（软件导出）
```json
{json.dumps(profile, ensure_ascii=False, indent=2)}
```

## 强制输出协议
    {result_schema_example(universe)}
"""
    stamp = local_now().strftime("%Y-%m-%d_%H%M")
    labels = {"night": "夜间全市场研究", "intraday": "1445盘中决策", "fund_check": "基金核查", "repair": "结果修复"}
    path = TASK_DIR / f"板块雷达_{labels.get(task_type, task_type)}_{stamp}.md"
    path.write_text(md, encoding="utf-8")
    return md, path


def repair_prompt(errors: list[str], original_text: str = "") -> str:
    compact_original = original_text[-12000:] if original_text else "（请使用原任务包中的研究内容）"
    return f"""上一次板块雷达结果未通过校验。请只修复以下问题，并重新输出一个完整结果块：

{chr(10).join('- ' + e for e in errors)}

必须从{START_MARKER}开始，以{END_MARKER}结束；中间只能放有效JSON。不得使用注释、尾逗号或省略号。

待修复的原结果：
{compact_original}
"""

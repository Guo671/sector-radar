"""Deterministic sector ranking for research, timing and execution separation.

The previous implementation used one additive score for momentum, data quality
and evidence completeness.  That made the strongest recent winners dominate the
candidate set even when they were already crowded.  This module deliberately
keeps four reader-facing dimensions separate and builds a diversified candidate
pool before the Pro review.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


RESEARCH_TILT_LABELS = (
    "非常推荐买",
    "推荐买",
    "有点推荐买",
    "观察",
    "不太推荐买",
    "不推荐买",
    "千万别买",
)
POSITIVE_TILTS = frozenset(RESEARCH_TILT_LABELS[:3])
NEGATIVE_TILTS = frozenset(RESEARCH_TILT_LABELS[4:])

DEFAULT_POLICY = {
    "trend_weight": 0.25,
    "entry_weight": 0.25,
    "catalyst_weight": 0.20,
    "relative_strength_weight": 0.15,
    "volume_breadth_weight": 0.15,
    "crowding_penalty": 0.45,
    "data_confidence_min": 70.0,
}
RANKING_FORMULA_VERSION = "four-dimension-v4"
CROWDING_NEUTRAL_POINT = 50.0


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def finite_number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def optional_finite_number(value: Any) -> float | None:
    """Parse a finite number without turning a missing observation into zero."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def crowding_score_adjustment(
    crowding_risk: float,
    coefficient: float,
    *,
    neutral_point: float = CROWDING_NEUTRAL_POINT,
) -> float:
    """Convert a 0-100 crowding level into a centred score adjustment.

    V3 subtracted ``coefficient * crowding`` and therefore removed 22.5
    points even for a neutral crowding reading of 50.  In production that
    shifted an entire 82-index round into negative tiers despite opportunity
    scores in the 60s and 70s.  V4 treats 50 as neutral: lower crowding earns
    a modest premium and higher crowding pays the symmetric penalty.  The
    separate extreme-crowding veto and high-position cap remain authoritative.
    """
    risk = clamp(crowding_risk)
    weight = max(0.0, finite_number(coefficient, 0.0))
    neutral = clamp(neutral_point)
    return weight * (neutral - risk)


def parse_position_20d(value: Any) -> float:
    """Parse an explicit price-range position, never an unrelated percentage."""
    text = str(value or "")
    match = re.search(
        r"(?:价格区间位置|区间位置|(?<!成分股)位置)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)%",
        text,
    )
    return clamp(finite_number(match.group(1), 50.0) if match else 50.0)


def parse_true_breadth(value: Any) -> float | None:
    """Return the constituent advance ratio, never a momentum proxy."""
    text = str(value or "")
    if "非成分股宽度" in text or "多周期动量一致性" in text:
        return None
    match = re.search(r"成分股上涨\s*([0-9]+)\s*/\s*([0-9]+)", text)
    if not match:
        return None
    advanced = finite_number(match.group(1), 0.0)
    total = finite_number(match.group(2), 0.0)
    return None if total <= 0 else clamp(advanced / total * 100.0)


def _normalised_level(value: Any, aliases: dict[str, float]) -> float | None:
    numeric = optional_finite_number(value)
    if numeric is not None:
        return clamp(numeric)
    key = re.sub(r"[\s_-]+", "", str(value or "").strip()).upper()
    return aliases.get(key)


_CATALYST_LEVELS = {
    "无": 10.0,
    "NONE": 10.0,
    "弱": 30.0,
    "LOW": 30.0,
    "WEAK": 30.0,
    "中": 55.0,
    "MEDIUM": 55.0,
    "MODERATE": 55.0,
    "强": 80.0,
    "HIGH": 80.0,
    "STRONG": 80.0,
    "极强": 95.0,
    "EXTREME": 95.0,
}

_CROWDING_LEVELS = {
    "低": 15.0,
    "LOW": 15.0,
    "中低": 30.0,
    "MEDIUMLOW": 30.0,
    "中": 50.0,
    "MEDIUM": 50.0,
    "中高": 70.0,
    "MEDIUMHIGH": 70.0,
    "高": 85.0,
    "HIGH": 85.0,
    "极高": 100.0,
    "EXTREME": 100.0,
}

_TRUE_BREADTH_KINDS = frozenset({
    "TRUECONSTITUENT",
    "TRUECONSTITUENTBREADTH",
    "TRUECOMPONENTBREADTH",
    "CONSTITUENT",
    "CONSTITUENTSNAPSHOT",
    "REALCONSTITUENT",
    "真实成分股",
    "成分股",
})


def _structured_true_breadth(metric: dict[str, Any]) -> tuple[float | None, str]:
    """Return breadth only when it is explicitly identified as constituent breadth."""
    raw_kind = str(metric.get("breadth_kind") or "").strip()
    if raw_kind:
        kind = re.sub(r"[\s_-]+", "", raw_kind).upper()
        if kind not in _TRUE_BREADTH_KINDS:
            return None, raw_kind
        breadth = optional_finite_number(metric.get("breadth_pct"))
        return (None if breadth is None else clamp(breadth)), raw_kind
    return parse_true_breadth(metric.get("breadth_signal")), "legacy_text"


def recommendation_label(score: float) -> str:
    """Map the final recommendation score to one and only one visible tier.

    A safety veto is deliberately *not* folded into this label.  Mixing the two
    concepts used to produce rows such as ``46.1 / 千万别买``: the number said
    "观察" while an opaque text gate silently replaced the tier.  Safety gates
    still block timing and pool admission, but are exposed separately together
    with their reason.
    """
    if score < 20:
        return "千万别买"
    if score < 35:
        return "不推荐买"
    if score < 45:
        return "不太推荐买"
    if score < 60:
        return "观察"
    if score < 70:
        return "有点推荐买"
    if score < 80:
        return "推荐买"
    return "非常推荐买"


def _position_entry_quality(position: float) -> float:
    points = (
        (0.0, 20.0), (15.0, 30.0), (35.0, 65.0), (60.0, 95.0),
        (75.0, 80.0), (90.0, 45.0), (100.0, 5.0),
    )
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if position <= right_x:
            width = right_x - left_x
            ratio = 0.0 if width <= 0 else (position - left_x) / width
            return clamp(left_y + (right_y - left_y) * ratio)
    return 5.0


def _source_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        value = json.loads(evidence.get("source_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _has_negative_catalyst(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(token in compact for token in ("暂无", "没有明确", "无明确", "未发现", "缺乏催化"))


_HARD_AVOID_TERMS = (
    "暂停申购", "终止上市", "重大违法", "流动性枯竭", "极端拥挤", "强烈回避",
)
_CLAUSE_BREAK_RE = re.compile(r"[，。；！？,.!?;：:\n]")
_NEGATED_BEFORE_RE = re.compile(
    r"(?:尚未|并未|未曾|从未|没有|并无|并非|不是|不属于|不属|无需|未|无|不)"
    r"(?:达到|到达|形成|出现|显现|呈现|属于|构成|存在|发生|发现|见到|观察到|"
    r"识别出|代表|意味着|说明|等于|算作|陷入|处于|建议|需要|应当|必须|值得|宜|显|见|现|有)?"
    r"(?:任何|明显|真正|所谓|严重|实质性)?$"
)
_NEGATED_AFTER_RE = re.compile(
    r"^(?:风险|迹象|状态|现象|观点|说法|结论|建议)?(?:尚未|并未|并非|不是|不|未|无)"
    r"(?:存在|出现|发现|形成|成立|明显|显著|真实|属实|构成|高)"
)


def _term_is_negated(text: str, start: int, end: int) -> bool:
    """Recognise a nearby Chinese negation without leaking across clauses."""
    prefix = _CLAUSE_BREAK_RE.split(text[:start])[-1][-24:]
    suffix = _CLAUSE_BREAK_RE.split(text[end:])[0][:16]
    return bool(_NEGATED_BEFORE_RE.search(prefix) or _NEGATED_AFTER_RE.search(suffix))


def hard_avoid_reason(
    evidence: dict[str, Any], *, structured_crowding: float | None = None,
) -> str:
    """Return an auditable *affirmative* safety-veto reason, or an empty string.

    Flash narratives are natural language.  A raw substring check cannot tell
    ``极端拥挤`` from ``尚未形成极端拥挤`` and was the direct cause of false
    ``千万别买`` labels.  Structured crowding remains authoritative at 95+, and
    textual gates are accepted only when the matched phrase is not negated in
    its own clause.
    """
    if structured_crowding is None:
        structured_crowding = _normalised_level(
            evidence.get("crowding_level"), _CROWDING_LEVELS,
        )
    if structured_crowding is not None and structured_crowding >= 95.0:
        return f"结构化拥挤度{structured_crowding:.1f}达到硬否决线95"
    for field in ("risk", "crowding", "catalyst"):
        compact = re.sub(r"\s+", "", str(evidence.get(field) or ""))
        for term in _HARD_AVOID_TERMS:
            for match in re.finditer(re.escape(term), compact):
                if not _term_is_negated(compact, match.start(), match.end()):
                    return f"{field}证据明确出现“{term}”"
    return ""


def _hard_avoid_text(evidence: dict[str, Any]) -> bool:
    """Backward-compatible boolean wrapper for callers that need only a gate."""
    return bool(hard_avoid_reason(evidence))


def timing_status_label(
    research_tilt: str,
    *,
    entry_quality: float,
    crowding_risk: float,
    hard_avoid: bool,
) -> str:
    """Keep timing separate from the score tier while enforcing safety gates."""
    if hard_avoid or research_tilt in NEGATIVE_TILTS:
        return "不宜介入"
    if research_tilt in POSITIVE_TILTS and entry_quality >= 70 and crowding_risk <= 55:
        return "可以买"
    if research_tilt in POSITIVE_TILTS or research_tilt == "观察":
        return "等待回调"
    return "不宜介入"


@dataclass(frozen=True)
class Scorecard:
    universe_id: str
    trend_opportunity: float
    entry_quality: float
    crowding_risk: float
    data_confidence: float
    catalyst_quality: float
    relative_strength: float
    volume_breadth: float
    opportunity_score: float
    recommendation_score: float
    research_tilt: str
    timing_status: str
    hard_avoid: bool
    position_20d_pct: float
    weighted_return: float
    true_breadth_pct: float | None
    freshness_ok: bool
    return_observations: int = 0
    volatility_20d: float | None = None
    consecutive_up_days: int | None = None
    breadth_kind: str = ""
    hard_avoid_reason: str = ""
    crowding_adjustment: float = 0.0
    crowding_neutral_point: float = CROWDING_NEUTRAL_POINT
    pre_cap_recommendation_score: float | None = None
    high_position_cap_applied: bool = False

    def as_components(self) -> dict[str, Any]:
        return {
            "trend_opportunity": round(self.trend_opportunity, 3),
            "entry_quality": round(self.entry_quality, 3),
            "crowding_risk": round(self.crowding_risk, 3),
            "crowding_adjustment": round(self.crowding_adjustment, 3),
            "crowding_neutral_point": round(self.crowding_neutral_point, 3),
            "data_confidence": round(self.data_confidence, 3),
            "catalyst_quality": round(self.catalyst_quality, 3),
            "relative_strength": round(self.relative_strength, 3),
            "volume_breadth": round(self.volume_breadth, 3),
            "opportunity_score": round(self.opportunity_score, 3),
            "recommendation_score": round(self.recommendation_score, 3),
            "research_tilt": self.research_tilt,
            "timing_status": self.timing_status,
            "position_20d_pct": round(self.position_20d_pct, 3),
            "weighted_return": round(self.weighted_return, 4),
            "true_breadth_pct": None if self.true_breadth_pct is None else round(self.true_breadth_pct, 3),
            "freshness_ok": self.freshness_ok,
            "return_observations": self.return_observations,
            "volatility_20d": None if self.volatility_20d is None else round(self.volatility_20d, 3),
            "consecutive_up_days": self.consecutive_up_days,
            "breadth_kind": self.breadth_kind,
            "hard_avoid": self.hard_avoid,
            "hard_avoid_reason": self.hard_avoid_reason,
            "pre_cap_recommendation_score": (
                None
                if self.pre_cap_recommendation_score is None
                else round(self.pre_cap_recommendation_score, 3)
            ),
            "high_position_cap_applied": self.high_position_cap_applied,
            "safety_parser_version": "negation-aware-v2",
            "formula_version": RANKING_FORMULA_VERSION,
        }


def _preliminary(metric: dict[str, Any], evidence: dict[str, Any], *, expected_cutoff: str | None) -> dict[str, Any]:
    raw_returns = [optional_finite_number(metric.get(key)) for key in ("d1", "d3", "d5", "d10", "d20")]
    return_observations = sum(value is not None for value in raw_returns)
    returns = [0.0 if value is None else value for value in raw_returns]
    d1, d3, d5, d10, d20 = returns
    weighted_return = sum(value * weight for value, weight in zip(returns, (0.10, 0.15, 0.20, 0.25, 0.30)))
    direction = clamp(50.0 + weighted_return * 4.0)
    persistence = (sum(value > 0 for value in returns) + (5 - return_observations) * 0.5) / 5.0 * 100.0
    recent_daily = d1
    medium_daily = d10 / 10.0
    acceleration = (
        clamp(50.0 + (recent_daily - medium_daily) * 18.0)
        if raw_returns[0] is not None and raw_returns[3] is not None
        else 50.0
    )
    trend = clamp(direction * 0.60 + persistence * 0.25 + acceleration * 0.15)

    structured_position = optional_finite_number(metric.get("position_20d_pct"))
    position = (
        clamp(structured_position)
        if structured_position is not None
        else parse_position_20d(metric.get("position_20d"))
    )
    entry = _position_entry_quality(position)
    prior_four_daily = (d5 - d1) / 4.0
    if all(raw_returns[index] is not None for index in (0, 2, 4)) and d20 > 0 and d1 > 0 and prior_four_daily < 0:
        entry += 12.0
    if raw_returns[0] is not None and raw_returns[4] is not None and position >= 90 and d1 < 0 < d20:
        entry -= 12.0
    if raw_returns[0] is not None and raw_returns[4] is not None and d20 < 0 and d1 < 0 and position <= 20:
        entry -= 12.0
    entry = clamp(entry)

    amount_ratio = finite_number(metric.get("amount_ratio_5d"), 0.0)
    position_risk = clamp((position - 60.0) / 40.0 * 35.0, 0.0, 35.0)
    acceleration_risk = (
        clamp((d1 - d20 / 20.0) * 8.0, 0.0, 20.0)
        if raw_returns[0] is not None and raw_returns[4] is not None
        else 0.0
    )
    volatility_20d = optional_finite_number(metric.get("volatility_20d"))
    volatility_risk = (
        0.0 if volatility_20d is None else clamp((volatility_20d - 12.0) / 28.0 * 20.0, 0.0, 20.0)
    )
    consecutive_raw = optional_finite_number(metric.get("consecutive_up_days"))
    consecutive_up_days = None if consecutive_raw is None else max(0, int(round(consecutive_raw)))
    positive_streak_risk = (
        0.0
        if consecutive_up_days is None
        else clamp((consecutive_up_days - 2.0) / 6.0 * 20.0, 0.0, 20.0)
    )
    volume_risk = clamp((amount_ratio - 1.35) * 20.0, 0.0, 15.0)
    crowding_text = " ".join(str(evidence.get(key) or "") for key in ("crowding", "risk"))
    text_risk = 10.0 if any(token in crowding_text for token in ("高拥挤", "过热", "极端", "拥挤度高")) else (
        5.0 if "拥挤" in crowding_text or "偏高" in crowding_text else 0.0
    )
    crowding_value = evidence.get("crowding_level")
    if crowding_value in (None, ""):
        crowding_value = metric.get("crowding_level")
    structured_crowding = _normalised_level(crowding_value, _CROWDING_LEVELS)
    quantitative_crowding = clamp(
        position_risk + acceleration_risk + positive_streak_risk + volume_risk + volatility_risk + text_risk
    )
    crowding = (
        quantitative_crowding
        if structured_crowding is None
        else clamp(max(quantitative_crowding, structured_crowding))
    )

    catalyst_text = str(evidence.get("catalyst") or "").strip()
    sources = _source_rows(evidence)
    catalyst_value = evidence.get("catalyst_strength")
    if catalyst_value in (None, ""):
        catalyst_value = metric.get("catalyst_strength")
    structured_catalyst = _normalised_level(catalyst_value, _CATALYST_LEVELS)
    if structured_catalyst is not None:
        catalyst = structured_catalyst
    elif not catalyst_text or _has_negative_catalyst(catalyst_text):
        catalyst = 30.0
    else:
        catalyst = 55.0 + min(25.0, len(sources) * 5.0) + min(10.0, len(catalyst_text) / 40.0 * 10.0)
    catalyst = clamp(catalyst)

    if amount_ratio <= 0:
        volume_quality = 40.0
    elif 0.85 <= amount_ratio <= 1.35:
        volume_quality = 90.0 - abs(amount_ratio - 1.10) * 40.0
    elif amount_ratio < 0.85:
        volume_quality = clamp(65.0 + amount_ratio * 20.0)
    else:
        volume_quality = clamp(80.0 - (amount_ratio - 1.35) * 45.0)
    true_breadth, breadth_kind = _structured_true_breadth(metric)
    breadth_quality = 50.0 if true_breadth is None else true_breadth
    volume_breadth = clamp(volume_quality * 0.50 + breadth_quality * 0.50)

    quality_name = str(metric.get("data_quality") or "").upper()
    if quality_name in {"COMPLETE", "COMPLETE_BOARD_SNAPSHOT"}:
        confidence = 80.0
    elif quality_name in {"DISCLOSED_PRICE_PROXY", "DISCLOSED_MOMENTUM_PROXY"}:
        confidence = 62.0
    else:
        confidence = 45.0
    if return_observations == 5:
        confidence += 5.0
    else:
        confidence -= (5 - return_observations) * 7.0
    if sources:
        confidence += min(10.0, len(sources) * 2.5)
    if true_breadth is not None:
        confidence += 5.0
    confidence += 2.0 if volatility_20d is not None else -2.0
    confidence += 2.0 if consecutive_up_days is not None else -2.0
    if structured_catalyst is not None:
        confidence += 2.0
    if structured_crowding is not None:
        confidence += 2.0
    cutoff = str(metric.get("data_cutoff") or "").strip()[:10]
    freshness_ok = bool(expected_cutoff and cutoff == expected_cutoff)
    if freshness_ok:
        confidence += 5.0
    elif expected_cutoff:
        confidence -= 35.0
    confidence = clamp(confidence)

    avoid_reason = hard_avoid_reason(
        evidence, structured_crowding=structured_crowding,
    )
    return {
        "universe_id": str(metric.get("universe_id") or ""),
        "trend": trend,
        "entry": entry,
        "crowding": crowding,
        "confidence": confidence,
        "catalyst": catalyst,
        "volume_breadth": volume_breadth,
        "position": position,
        "weighted_return": weighted_return,
        "true_breadth": true_breadth,
        "breadth_kind": breadth_kind,
        "freshness_ok": freshness_ok,
        "freshness_expected": bool(expected_cutoff),
        "hard_avoid": bool(avoid_reason),
        "hard_avoid_reason": avoid_reason,
        "return_observations": return_observations,
        "volatility_20d": volatility_20d,
        "consecutive_up_days": consecutive_up_days,
        "d1_observed": raw_returns[0] is not None,
        "d1": d1,
    }


def build_scorecards(
    metrics: Iterable[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]] | None = None,
    *,
    expected_cutoff: str | None,
    policy: dict[str, float] | None = None,
) -> dict[str, Scorecard]:
    metrics = list(metrics)
    policy = {**DEFAULT_POLICY, **(policy or {})}
    evidence_by_id = evidence_by_id or {}
    preliminary = []
    for metric in metrics:
        uid = str(metric.get("universe_id") or "")
        preliminary.append(_preliminary(metric, evidence_by_id.get(uid, {}), expected_cutoff=expected_cutoff))
    ordered = sorted(preliminary, key=lambda row: (row["weighted_return"], row["universe_id"]))
    denominator = max(1, len(ordered) - 1)
    relative = {row["universe_id"]: index / denominator * 100.0 for index, row in enumerate(ordered)}
    minimum_confidence = clamp(finite_number(policy.get("data_confidence_min"), 70.0))
    result: dict[str, Scorecard] = {}
    for row in preliminary:
        relative_strength = relative[row["universe_id"]]
        opportunity = clamp(
            row["trend"] * policy["trend_weight"]
            + row["entry"] * policy["entry_weight"]
            + row["catalyst"] * policy["catalyst_weight"]
            + relative_strength * policy["relative_strength_weight"]
            + row["volume_breadth"] * policy["volume_breadth_weight"]
        )
        crowding_adjustment = crowding_score_adjustment(
            row["crowding"], policy["crowding_penalty"],
        )
        pre_cap_recommendation = clamp(opportunity + crowding_adjustment)
        recommendation = pre_cap_recommendation
        breakout_confirmed = bool(
            row["position"] >= 95
            and row["d1_observed"]
            and 0.2 <= row["d1"] <= 2.5
            and row["true_breadth"] is not None
            and row["true_breadth"] >= 55
        )
        high_position_cap_applied = bool(
            row["position"] >= 95
            and not breakout_confirmed
            and recommendation > 59.0
        )
        if row["position"] >= 95 and not breakout_confirmed:
            recommendation = min(recommendation, 59.0)
        tilt = recommendation_label(recommendation)
        timing = timing_status_label(
            tilt,
            entry_quality=row["entry"],
            crowding_risk=row["crowding"],
            hard_avoid=row["hard_avoid"],
        )
        # Freshness and confidence are execution gates, not research/timing
        # labels.  The allocator will force zero yuan and set execution_status
        # to 行情待更新/数据待补齐 without overwriting this judgement.
        result[row["universe_id"]] = Scorecard(
            universe_id=row["universe_id"],
            trend_opportunity=round(row["trend"], 3),
            entry_quality=round(row["entry"], 3),
            crowding_risk=round(row["crowding"], 3),
            data_confidence=round(row["confidence"], 3),
            catalyst_quality=round(row["catalyst"], 3),
            relative_strength=round(relative_strength, 3),
            volume_breadth=round(row["volume_breadth"], 3),
            opportunity_score=round(opportunity, 3),
            recommendation_score=round(recommendation, 3),
            research_tilt=tilt,
            timing_status=timing,
            hard_avoid=row["hard_avoid"],
            position_20d_pct=round(row["position"], 3),
            weighted_return=round(row["weighted_return"], 4),
            true_breadth_pct=None if row["true_breadth"] is None else round(row["true_breadth"], 3),
            freshness_ok=row["freshness_ok"],
            return_observations=row["return_observations"],
            volatility_20d=None if row["volatility_20d"] is None else round(row["volatility_20d"], 3),
            consecutive_up_days=row["consecutive_up_days"],
            breadth_kind=row["breadth_kind"],
            hard_avoid_reason=row["hard_avoid_reason"],
            crowding_adjustment=round(crowding_adjustment, 3),
            crowding_neutral_point=CROWDING_NEUTRAL_POINT,
            pre_cap_recommendation_score=round(pre_cap_recommendation, 3),
            high_position_cap_applied=high_position_cap_applied,
        )
    return result


def pool_route_fit(card: Scorecard) -> tuple[str, float]:
    """Return the best eligible route and its multi-factor fit score."""
    if card.hard_avoid:
        return "不入候选池", 0.0
    fits: list[tuple[str, float]] = []
    if card.trend_opportunity >= 55 and card.crowding_risk <= 60 and card.entry_quality >= 45:
        fits.append((
            "趋势确认·不过热",
            card.trend_opportunity * 0.50 + (100.0 - card.crowding_risk) * 0.25 + card.entry_quality * 0.25,
        ))
    if card.entry_quality >= 65 and card.trend_opportunity >= 40:
        fits.append((
            "回调结束·重新转强",
            card.entry_quality * 0.55 + card.trend_opportunity * 0.25 + (100.0 - card.crowding_risk) * 0.20,
        ))
    if card.catalyst_quality >= 60 and card.crowding_risk <= 60:
        fits.append((
            "早期催化·低拥挤",
            card.catalyst_quality * 0.60 + (100.0 - card.crowding_risk) * 0.25 + card.entry_quality * 0.15,
        ))
    return max(fits, key=lambda item: item[1]) if fits else ("不入候选池", 0.0)


def classify_pool_route(card: Scorecard) -> str:
    """Assign one primary route before any quota is applied."""
    return pool_route_fit(card)[0]


def select_candidate_pool(
    scorecards: dict[str, Scorecard],
    clusters: dict[str, str],
    *,
    route_size: int = 10,
    minimum_size: int = 20,
    maximum_size: int = 30,
    max_per_cluster: int = 4,
    minimum_score: float = 45.0,
) -> list[dict[str, Any]]:
    """Create a quality-gated pool by round-robin over three primary routes.

    ``minimum_size`` is a target, not permission to admit an item below the
    absolute quality line.  A thin market therefore produces a smaller pool.
    """
    route_names = ("趋势确认·不过热", "回调结束·重新转强", "早期催化·低拥挤")
    routes: dict[str, list[Scorecard]] = {route: [] for route in route_names}
    route_fit_scores: dict[str, float] = {}
    for card in scorecards.values():
        if card.hard_avoid or card.recommendation_score < minimum_score:
            continue
        route, fit_score = pool_route_fit(card)
        if route in routes:
            routes[route].append(card)
            route_fit_scores[card.universe_id] = fit_score
    for route in route_names:
        routes[route].sort(key=lambda card: (
            -route_fit_scores[card.universe_id],
            -card.recommendation_score,
            card.universe_id,
        ))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    cluster_counts: dict[str, int] = {}

    def add(card: Scorecard, route: str) -> bool:
        if card.universe_id in seen or len(selected) >= maximum_size:
            return False
        cluster = str(clusters.get(card.universe_id) or card.universe_id)
        if cluster_counts.get(cluster, 0) >= max_per_cluster:
            return False
        seen.add(card.universe_id)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        selected.append({
            "universe_id": card.universe_id,
            "pool_route": route,
            "correlation_bucket": cluster,
            "recommendation_score": card.recommendation_score,
            "research_tilt": card.research_tilt,
            "timing_status": card.timing_status,
        })
        return True

    cursors = {route: 0 for route in route_names}
    admitted = {route: 0 for route in route_names}
    route_limit = max(0, int(route_size))
    while len(selected) < max(0, int(maximum_size)):
        progressed = False
        for route in route_names:
            if admitted[route] >= route_limit or len(selected) >= maximum_size:
                continue
            cards = routes[route]
            while cursors[route] < len(cards):
                card = cards[cursors[route]]
                cursors[route] += 1
                if add(card, route):
                    admitted[route] += 1
                    progressed = True
                    break
        if not progressed:
            break
    _ = minimum_size  # Deliberately soft: never trigger low-quality filler.
    return selected


def final_display_candidates(
    rows: Iterable[dict[str, Any]],
    *,
    maximum_count: int = 10,
    max_per_cluster: int = 2,
    minimum_score: float = 45.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep an absolute quality line and at most two rows per risk bucket."""
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (-finite_number(row.get("recommendation_score"), 0.0), str(row.get("universe_id") or "")),
    )
    for row in ordered:
        if finite_number(row.get("recommendation_score"), 0.0) < minimum_score:
            suppressed.append({**row, "suppression_reason": f"低于候选绝对线{minimum_score:.0f}分"})
            continue
        cluster = str(row.get("correlation_bucket") or row.get("universe_id") or "未分类")
        if counts.get(cluster, 0) >= max_per_cluster:
            suppressed.append({**row, "suppression_reason": f"同一相关性桶最多展示{max_per_cluster}项"})
            continue
        if len(kept) >= maximum_count:
            suppressed.append({**row, "suppression_reason": f"最终候选最多{maximum_count}项"})
            continue
        counts[cluster] = counts.get(cluster, 0) + 1
        kept.append(row)
    for rank, row in enumerate(kept, start=1):
        row["rank"] = rank
    return kept, suppressed

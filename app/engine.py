from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .config import local_today


@dataclass
class SaleEstimate:
    holding_days: int
    market_value: float
    purchase_fee: float
    redeem_fee: float
    redeem_fee_pct: float
    net_proceeds: float
    net_pnl: float
    gross_return_pct: float
    net_return_pct: float


@dataclass
class RiskResult:
    level: str
    decision: str
    reasons: list[str] = field(default_factory=list)


class UnknownFeeScheduleError(ValueError):
    """Raised when a redemption estimate would otherwise silently assume 0%."""


def holding_days(confirmed_date: str, sell_date: str | None = None) -> int:
    start = date.fromisoformat(confirmed_date[:10])
    end = date.fromisoformat((sell_date or local_today().isoformat())[:10])
    return max(1, (end - start).days + 1)


def fee_rate_for_days(tiers: list[dict[str, Any]], days: int) -> float:
    if not tiers:
        raise UnknownFeeScheduleError("赎回费阶梯尚未核验，不能按0费率计算费后盈亏。")
    candidates = []
    for tier in tiers:
        minimum = int(tier.get("min_days", 0))
        maximum = tier.get("max_days")
        if days >= minimum and (maximum is None or days <= int(maximum)):
            candidates.append((minimum, float(tier.get("redeem_fee_pct", 0))))
    if not candidates:
        raise UnknownFeeScheduleError(
            f"赎回费阶梯没有覆盖持有{days}天，不能按0费率计算费后盈亏。"
        )
    return max(candidates, key=lambda item: item[0])[1]


def estimate_sale(
    *,
    units: float,
    cost: float,
    latest_nav: float,
    confirmed_date: str,
    tiers: list[dict[str, Any]],
    purchase_fee_pct: float = 0.0,
    sell_date: str | None = None,
) -> SaleEstimate:
    days = holding_days(confirmed_date, sell_date)
    market_value = max(0.0, units) * max(0.0, latest_nav)
    purchase_fee = max(0.0, cost) * max(0.0, purchase_fee_pct) / 100.0
    redeem_pct = fee_rate_for_days(tiers, days)
    redeem_fee = market_value * redeem_pct / 100.0
    net_proceeds = market_value - redeem_fee
    # ``cost`` is the user's actual gross cash outflow and therefore already
    # includes any subscription fee.  The fee is displayed for explanation,
    # but subtracting it again would understate P&L.
    net_pnl = net_proceeds - cost
    gross_return = ((market_value / cost) - 1) * 100 if cost else 0.0
    net_return = (net_pnl / cost) * 100 if cost else 0.0
    return SaleEstimate(
        holding_days=days,
        market_value=market_value,
        purchase_fee=purchase_fee,
        redeem_fee=redeem_fee,
        redeem_fee_pct=redeem_pct,
        net_proceeds=net_proceeds,
        net_pnl=net_pnl,
        gross_return_pct=gross_return,
        net_return_pct=net_return,
    )


def estimate_sale_by_lots(
    *,
    units: float,
    cost: float,
    latest_nav: float,
    confirmed_date: str,
    tiers: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    purchase_fee_pct: float = 0.0,
    sell_date: str | None = None,
) -> SaleEstimate:
    """Estimate a full redemption while respecting every tracked cost lot.

    Old imported holdings may predate lot tracking. Their untracked remainder
    is kept as one legacy component; new recurring investments are assessed
    one by one using their own confirmation dates.
    """
    tracked_units = sum(max(0.0, float(lot.get("remaining_units", 0))) for lot in lots)
    tracked_cost = sum(
        max(0.0, float(lot.get("gross_amount", 0)))
        * (max(0.0, float(lot.get("remaining_units", 0))) / max(float(lot.get("units", 0)), 1e-12))
        for lot in lots
    )
    legacy_units = max(0.0, float(units) - tracked_units)
    legacy_cost = max(0.0, float(cost) - tracked_cost)
    components: list[SaleEstimate] = []
    if legacy_units > 1e-9 or legacy_cost > 1e-9:
        components.append(estimate_sale(
            units=legacy_units, cost=legacy_cost, latest_nav=latest_nav,
            confirmed_date=confirmed_date, tiers=tiers,
            purchase_fee_pct=purchase_fee_pct, sell_date=sell_date,
        ))
    for lot in lots:
        original_units = max(0.0, float(lot.get("units", 0)))
        remaining = max(0.0, float(lot.get("remaining_units", 0)))
        if original_units <= 0 or remaining <= 0:
            continue
        lot_cost = max(0.0, float(lot.get("gross_amount", 0))) * remaining / original_units
        components.append(estimate_sale(
            units=remaining, cost=lot_cost, latest_nav=latest_nav,
            confirmed_date=str(lot.get("confirmed_date", confirmed_date)), tiers=tiers,
            purchase_fee_pct=0.0, sell_date=sell_date,
        ))
    if not components:
        return estimate_sale(
            units=units, cost=cost, latest_nav=latest_nav, confirmed_date=confirmed_date,
            tiers=tiers, purchase_fee_pct=purchase_fee_pct, sell_date=sell_date,
        )
    market_value = sum(item.market_value for item in components)
    purchase_fee = sum(item.purchase_fee for item in components)
    redeem_fee = sum(item.redeem_fee for item in components)
    net_proceeds = market_value - redeem_fee
    net_pnl = net_proceeds - float(cost)
    gross_return = ((market_value / float(cost)) - 1) * 100 if cost else 0.0
    denominator = float(cost)
    return SaleEstimate(
        holding_days=min(item.holding_days for item in components),
        market_value=market_value,
        purchase_fee=purchase_fee,
        redeem_fee=redeem_fee,
        redeem_fee_pct=(redeem_fee / market_value * 100) if market_value else 0.0,
        net_proceeds=net_proceeds,
        net_pnl=net_pnl,
        gross_return_pct=gross_return,
        net_return_pct=(net_pnl / denominator * 100) if denominator else 0.0,
    )


def evaluate_holding(estimate: SaleEstimate, settings: dict[str, str], logic_valid: bool = True) -> RiskResult:
    soft = float(settings.get("soft_stop_gross_pct", -2.5))
    hard = float(settings.get("hard_stop_gross_pct", -4.0))
    if not logic_valid:
        return RiskResult("danger", "卖出/复核", ["原买入逻辑已经失效；手续费不能成为拖延止损的理由。"])
    if estimate.gross_return_pct <= hard:
        return RiskResult("danger", "硬止损", [f"毛收益{estimate.gross_return_pct:.2f}%已触及硬止损{hard:.2f}%。"])
    if estimate.gross_return_pct <= soft:
        return RiskResult("warning", "减仓或止损", [f"毛收益{estimate.gross_return_pct:.2f}%已触及软止损{soft:.2f}%。"])
    if estimate.net_return_pct >= 4.0:
        return RiskResult("profit", "保护利润", ["费后收益已超过4%，应结合板块拥挤度决定止盈或移动止损。"])
    return RiskResult("normal", "继续观察", ["尚未触发机械止损；仍需检查板块趋势和买入逻辑。"])


def evaluate_intraday(candidate: dict[str, Any], settings: dict[str, str]) -> RiskResult:
    change = candidate.get("current_change")
    if change is None:
        return RiskResult("normal", "待输入", ["缺少当前涨跌幅。"])
    change = float(change)
    high = candidate.get("high_change")
    low = candidate.get("low_change")
    trend = (candidate.get("short_trend") or "").strip()
    status = (candidate.get("status") or "").strip()
    amount = float(candidate.get("suggested_amount") or 0)
    reasons: list[str] = []

    if status in {"回避", "过热回避", "趋势破坏", "不可买"}:
        return RiskResult("danger", "放弃", [f"夜间研究状态为“{status}”。"])
    if amount > float(settings.get("single_position_limit", 2000)):
        reasons.append("建议金额超过单指数上限，必须压缩仓位。")
    if change <= -3.5:
        return RiskResult("danger", "放弃", [f"盘中跌幅{change:.2f}%过大，不能把急跌自动理解为低估。"] + reasons)
    if change >= 4.5:
        if trend == "回落" and high is not None and float(high) - change >= 1.0:
            return RiskResult("danger", "放弃", ["大涨后从高点明显回落，疑似冲高兑现。"] + reasons)
        return RiskResult("warning", "不追高", [f"盘中已上涨{change:.2f}%，默认不为短周期策略追入。"] + reasons)
    if -2.0 <= change <= 2.8 and trend in {"走高", "横盘", "低位回升"}:
        if high is not None and low is not None and float(high) > float(low):
            position = (change - float(low)) / (float(high) - float(low))
            if position < 0.45:
                return RiskResult("warning", "继续观察", ["当前仍处在日内区间下半部，承接确认不足。"] + reasons)
        return RiskResult("profit", "条件买入", ["涨幅未过热且短线承接尚可；仍须逐条核对AI买入条件。"] + reasons)
    if change < -2.0:
        return RiskResult("warning", "等待", ["跌幅较大且尚未形成足够的日内修复信号。"] + reasons)
    return RiskResult("normal", "观察", ["盘中结构不满足默认买入模板，请结合预案人工判断。"] + reasons)


def validate_allocation(
    proposals: list[dict[str, Any]], current_active: list[dict[str, Any]], settings: dict[str, str]
) -> RiskResult:
    capital_limit = float(settings.get("active_capital_limit", 8000))
    single_limit = float(settings.get("single_position_limit", 2000))
    bucket_limit = float(settings.get("correlation_bucket_limit", 3000))
    max_themes = int(float(settings.get("max_active_themes", 4)))
    def included(item: dict[str, Any]) -> bool:
        if "counts_toward_active_capital" not in item:
            return True
        return bool(int(item.get("counts_toward_active_capital") or 0))

    def amount_of(item: dict[str, Any]) -> float:
        for key in ("capital_amount", "cost", "amount", "value"):
            if item.get(key) is not None:
                return max(0.0, float(item.get(key) or 0))
        return 0.0

    proposal_items = [item for item in proposals if included(item)]
    current_items = [item for item in current_active if included(item)]
    items = [*proposal_items, *current_items]
    total = sum(amount_of(item) for item in items)
    bucket_totals: dict[str, float] = {}
    index_totals: dict[str, float] = {}
    index_names: dict[str, str] = {}
    reasons = []
    themes = set()
    proposal_indices = {
        str(item.get("index_key") or item.get("index_code") or item.get("fund_code") or "未分类").strip()
        for item in proposal_items
    }
    proposal_buckets = {
        str(
            item.get("bucket") or item.get("correlation_bucket")
            or item.get("index_key") or item.get("fund_code") or "未分类"
        ).strip()
        for item in proposal_items
    }
    for item in items:
        amount = amount_of(item)
        index_key = str(
            item.get("index_key") or item.get("index_code") or item.get("fund_code") or "未分类"
        ).strip()
        index_name = str(item.get("index_name") or item.get("name") or item.get("sector_name") or index_key)
        bucket = str(item.get("bucket") or item.get("correlation_bucket") or index_key).strip()
        themes.add(index_key)
        index_totals[index_key] = index_totals.get(index_key, 0) + amount
        index_names[index_key] = index_name
        bucket_totals[bucket] = bucket_totals.get(bucket, 0) + amount
    for index_key, amount in index_totals.items():
        if amount > single_limit and index_key in proposal_indices:
            reasons.append(
                f"同一指数“{index_names[index_key]}”合计{amount:.0f}元，超过单指数上限{single_limit:.0f}元。"
            )
    if total > capital_limit:
        reasons.append(f"主动资金合计{total:.0f}元，超过总上限{capital_limit:.0f}元。")
    for bucket, amount in bucket_totals.items():
        if amount > bucket_limit and bucket in proposal_buckets:
            reasons.append(f"相关性桶“{bucket}”合计{amount:.0f}元，超过上限{bucket_limit:.0f}元。")
    current_themes = {
        str(item.get("index_key") or item.get("index_code") or item.get("fund_code") or "未分类").strip()
        for item in current_items
    }
    if len(themes) > max_themes and len(themes) > len(current_themes):
        reasons.append(f"同时暴露于{len(themes)}个方向，超过{max_themes}个主题上限。")
    if reasons:
        return RiskResult("danger", "不通过", reasons)
    return RiskResult("profit", "通过", [f"主动资金计划合计{total:.0f}/{capital_limit:.0f}元。"])


def money(value: float | None) -> str:
    return "—" if value is None else f"¥{value:,.2f}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"

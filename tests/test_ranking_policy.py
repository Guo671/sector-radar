from __future__ import annotations

import unittest

from app.ranking import (
    Scorecard,
    build_scorecards,
    crowding_score_adjustment,
    final_display_candidates,
    hard_avoid_reason,
    parse_position_20d,
    recommendation_label,
    select_candidate_pool,
)


EXPECTED_CUTOFF = "2026-08-10"


def metric(universe_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "universe_id": universe_id,
        "d1": 1.0,
        "d3": 2.0,
        "d5": 3.0,
        "d10": 5.0,
        "d20": 8.0,
        "position_20d_pct": 60.0,
        "amount_ratio_5d": 1.1,
        "volatility_20d": 10.0,
        "consecutive_up_days": 2,
        "breadth_kind": "TRUE_CONSTITUENT",
        "breadth_pct": 65.0,
        "data_quality": "COMPLETE",
        "data_cutoff": EXPECTED_CUTOFF,
    }
    row.update(overrides)
    return row


def evidence(*, catalyst: object = "强", crowding: object = "低", with_source: bool = True) -> dict[str, object]:
    return {
        "catalyst_strength": catalyst,
        "crowding_level": crowding,
        "source_json": '[{"url":"https://example.test/source"}]' if with_source else "[]",
    }


def card(
    universe_id: str,
    *,
    trend: float,
    entry: float,
    risk: float,
    catalyst: float,
    score: float,
    hard_avoid: bool = False,
) -> Scorecard:
    return Scorecard(
        universe_id=universe_id,
        trend_opportunity=trend,
        entry_quality=entry,
        crowding_risk=risk,
        data_confidence=90.0,
        catalyst_quality=catalyst,
        relative_strength=70.0,
        volume_breadth=70.0,
        opportunity_score=max(
            0.0, min(100.0, score - crowding_score_adjustment(risk, 0.45)),
        ),
        recommendation_score=score,
        research_tilt=recommendation_label(score),
        timing_status="不宜介入" if hard_avoid else "可以买",
        hard_avoid=hard_avoid,
        position_20d_pct=60.0,
        weighted_return=2.0,
        true_breadth_pct=60.0,
        freshness_ok=True,
    )


class RankingPolicyTests(unittest.TestCase):
    def test_all_seven_research_levels_are_reachable_and_not_collapsed_to_observe(self) -> None:
        self.assertEqual(
            [recommendation_label(value) for value in (80, 70, 60, 45, 35, 20, 19.99)],
            [
                "非常推荐买", "推荐买", "有点推荐买", "观察",
                "不太推荐买", "不推荐买", "千万别买",
            ],
        )

    def test_negated_hard_avoid_phrases_do_not_override_score_tier(self) -> None:
        phrases = (
            "关注度提升但尚未形成极端拥挤。",
            "量能正常，未显极端拥挤",
            "交易热度适中，未出现极端拥挤。",
            "偏热但未极端拥挤",
            "公开资料显示并未暂停申购",
            "这不代表强烈回避",
        )
        for number, phrase in enumerate(phrases):
            universe_id = f"NEG-{number}"
            rows = build_scorecards(
                [metric(universe_id)],
                {universe_id: {**evidence(crowding=40), "crowding": phrase}},
                expected_cutoff=EXPECTED_CUTOFF,
            )
            with self.subTest(phrase=phrase):
                result = rows[universe_id]
                self.assertFalse(result.hard_avoid)
                self.assertEqual(result.hard_avoid_reason, "")
                self.assertEqual(result.research_tilt, recommendation_label(result.recommendation_score))

    def test_affirmative_safety_veto_is_separate_from_score_tier(self) -> None:
        rows = build_scorecards(
            [metric("BLOCKED")],
            {"BLOCKED": {**evidence(crowding=40), "risk": "基金已暂停申购，等待公告恢复。"}},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        result = rows["BLOCKED"]
        self.assertTrue(result.hard_avoid)
        self.assertIn("暂停申购", result.hard_avoid_reason)
        self.assertEqual(result.research_tilt, recommendation_label(result.recommendation_score))
        self.assertEqual(result.timing_status, "不宜介入")

    def test_structured_extreme_crowding_remains_a_hard_gate(self) -> None:
        rows = build_scorecards(
            [metric("EXTREME")],
            {"EXTREME": {**evidence(crowding=97), "crowding": "文字表述中性"}},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        result = rows["EXTREME"]
        self.assertTrue(result.hard_avoid)
        self.assertIn("达到硬否决线95", result.hard_avoid_reason)
        self.assertEqual(result.research_tilt, recommendation_label(result.recommendation_score))
        self.assertEqual(result.timing_status, "不宜介入")
        self.assertTrue(hard_avoid_reason({"crowding_level": 95}))

    def test_position_parser_does_not_treat_constituent_ratio_as_price_position(self) -> None:
        self.assertEqual(parse_position_20d("成分股20日上涨37/60（61.7%）"), 50.0)
        self.assertEqual(parse_position_20d("20日价格区间位置：83.5%"), 83.5)

    def test_structured_position_has_priority_over_legacy_text(self) -> None:
        rows = build_scorecards(
            [metric("A", position_20d_pct=72.0, position_20d="成分股上涨 9/10（90%）")],
            {"A": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertEqual(rows["A"].position_20d_pct, 72.0)

    def test_default_formula_matches_documented_starting_policy(self) -> None:
        result = build_scorecards(
            [metric("A"), metric("B", d1=-1, d3=-2, d5=-3, d10=-4, d20=-5)],
            {"A": evidence(), "B": evidence(catalyst="中")},
            expected_cutoff=EXPECTED_CUTOFF,
        )["A"]
        expected_opportunity = (
            result.trend_opportunity * 0.25
            + result.entry_quality * 0.25
            + result.catalyst_quality * 0.20
            + result.relative_strength * 0.15
            + result.volume_breadth * 0.15
        )
        expected_recommendation = max(0.0, min(
            100.0,
            expected_opportunity + crowding_score_adjustment(result.crowding_risk, 0.45),
        ))
        self.assertAlmostEqual(result.opportunity_score, expected_opportunity, delta=0.001)
        self.assertAlmostEqual(result.recommendation_score, expected_recommendation, delta=0.001)

    def test_user_policy_weights_are_applied_instead_of_a_hidden_fixed_formula(self) -> None:
        custom = {
            "trend_weight": 0.10,
            "entry_weight": 0.40,
            "catalyst_weight": 0.15,
            "relative_strength_weight": 0.10,
            "volume_breadth_weight": 0.25,
            "crowding_penalty": 0.60,
        }
        result = build_scorecards(
            [metric("A"), metric("B", d1=-1, d3=-2, d5=-3, d10=-4, d20=-5)],
            {"A": evidence(), "B": evidence(catalyst="中")},
            expected_cutoff=EXPECTED_CUTOFF,
            policy=custom,
        )["A"]
        expected_opportunity = (
            result.trend_opportunity * 0.10
            + result.entry_quality * 0.40
            + result.catalyst_quality * 0.15
            + result.relative_strength * 0.10
            + result.volume_breadth * 0.25
        )
        expected_recommendation = max(0.0, min(
            100.0,
            expected_opportunity + crowding_score_adjustment(result.crowding_risk, 0.60),
        ))
        self.assertAlmostEqual(result.opportunity_score, expected_opportunity, delta=0.001)
        self.assertAlmostEqual(result.recommendation_score, expected_recommendation, delta=0.001)

    def test_neutral_crowding_is_neutral_and_low_high_risk_are_symmetric(self) -> None:
        neutral = build_scorecards(
            [metric("NEUTRAL")], {"NEUTRAL": evidence(crowding=50)},
            expected_cutoff=EXPECTED_CUTOFF,
        )["NEUTRAL"]
        low = build_scorecards(
            [metric("LOW")], {"LOW": evidence(crowding=30)},
            expected_cutoff=EXPECTED_CUTOFF,
        )["LOW"]
        high = build_scorecards(
            [metric("HIGH")], {"HIGH": evidence(crowding=70)},
            expected_cutoff=EXPECTED_CUTOFF,
        )["HIGH"]
        self.assertAlmostEqual(
            neutral.recommendation_score, neutral.opportunity_score, delta=0.001,
        )
        self.assertAlmostEqual(low.crowding_adjustment, 9.0, delta=0.001)
        self.assertAlmostEqual(high.crowding_adjustment, -9.0, delta=0.001)
        self.assertGreater(low.recommendation_score, neutral.recommendation_score)
        self.assertLess(high.recommendation_score, neutral.recommendation_score)

    def test_production_like_ai_supply_chain_snapshot_does_not_collapse_or_chase_heat(self) -> None:
        production_rows = [
            metric(
                "CLOUD", d1=0.7164, d3=1.7351, d5=-0.5225,
                d10=12.6828, d20=-2.3595, position_20d_pct=76,
                breadth_kind="MOMENTUM_PROXY", breadth_pct=None,
            ),
            metric(
                "COMM", d1=0.1809, d3=3.3234, d5=2.125,
                d10=19.6721, d20=-9.1919, position_20d_pct=76,
                breadth_kind="MOMENTUM_PROXY", breadth_pct=None,
            ),
        ]
        production_evidence = {
            "CLOUD": evidence(catalyst=80, crowding=58),
            "COMM": evidence(catalyst=80, crowding=80),
        }
        # Relative strength is a whole-catalog percentile.  Surround the two
        # production examples with weaker and stronger rows so this regression
        # exercises realistic neighbouring percentiles instead of the 0/100
        # artefact produced by a two-row universe.
        for number, value in enumerate((-5, -3, -1, 0, 1, 2, 4, 5), start=1):
            universe_id = f"FILL-{number}"
            production_rows.append(metric(
                universe_id,
                d1=value, d3=value, d5=value, d10=value, d20=value,
            ))
            production_evidence[universe_id] = evidence(catalyst=55, crowding=50)
        rows = build_scorecards(
            production_rows,
            production_evidence,
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertIn(
            rows["CLOUD"].research_tilt,
            {"有点推荐买", "推荐买", "非常推荐买"},
        )
        self.assertGreater(
            rows["COMM"].trend_opportunity, rows["CLOUD"].trend_opportunity,
        )
        self.assertLess(
            rows["COMM"].recommendation_score, rows["CLOUD"].recommendation_score,
        )
        self.assertNotIn(
            rows["COMM"].research_tilt, {"推荐买", "非常推荐买"},
        )

    def test_extreme_recent_winner_is_penalized_below_a_cleaner_entry(self) -> None:
        rows = build_scorecards(
            [
                metric(
                    "HOT", d1=5, d3=12, d5=18, d10=28, d20=45,
                    position_20d_pct=98, amount_ratio_5d=2.0,
                    volatility_20d=45, consecutive_up_days=10, breadth_pct=75,
                ),
                metric(
                    "CLEAN", d1=0.7, d3=1.5, d5=2.5, d10=4, d20=7,
                    position_20d_pct=60, amount_ratio_5d=1.1,
                    volatility_20d=10, consecutive_up_days=2, breadth_pct=65,
                ),
            ],
            {
                "HOT": evidence(catalyst="极强", crowding="高"),
                "CLEAN": evidence(catalyst="强", crowding="低"),
            },
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertGreater(rows["HOT"].trend_opportunity, rows["CLEAN"].trend_opportunity)
        self.assertGreater(rows["HOT"].crowding_risk, rows["CLEAN"].crowding_risk)
        self.assertLess(rows["HOT"].recommendation_score, rows["CLEAN"].recommendation_score)
        self.assertEqual(rows["HOT"].timing_status, "等待回调")
        self.assertNotEqual(rows["HOT"].timing_status, "可以买")

    def test_missing_returns_reduce_confidence_instead_of_becoming_real_zeroes(self) -> None:
        rows = build_scorecards(
            [
                metric("COMPLETE"),
                metric("MISSING", d1=None, d3=None, d5=None, d10=None, d20=None),
            ],
            {"COMPLETE": evidence(), "MISSING": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertEqual(rows["COMPLETE"].return_observations, 5)
        self.assertEqual(rows["MISSING"].return_observations, 0)
        self.assertGreater(rows["COMPLETE"].data_confidence, rows["MISSING"].data_confidence)

    def test_only_explicit_constituent_breadth_is_accepted(self) -> None:
        rows = build_scorecards(
            [
                metric("TRUE", breadth_kind="TRUE_CONSTITUENT", breadth_pct=72.0),
                metric(
                    "PROXY",
                    breadth_kind="MOMENTUM_PROXY",
                    breadth_pct=92.0,
                    breadth_signal="成分股上涨 9/10",
                ),
            ],
            {"TRUE": evidence(), "PROXY": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertEqual(rows["TRUE"].true_breadth_pct, 72.0)
        self.assertIsNone(rows["PROXY"].true_breadth_pct)

    def test_market_pipeline_constituent_snapshot_is_true_breadth(self) -> None:
        rows = build_scorecards(
            [metric("SNAP", breadth_kind="constituent_snapshot", breadth_pct=61.5)],
            {"SNAP": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertEqual(rows["SNAP"].true_breadth_pct, 61.5)
        self.assertEqual(rows["SNAP"].breadth_kind, "constituent_snapshot")

    def test_structured_volatility_streak_crowding_and_catalyst_change_scores(self) -> None:
        rows = build_scorecards(
            [
                metric("COOL", volatility_20d=10.0, consecutive_up_days=2),
                metric("HOT", volatility_20d=48.0, consecutive_up_days=10),
                metric("WEAK"),
                metric("STRONG"),
            ],
            {
                "COOL": evidence(catalyst="中", crowding="低"),
                "HOT": evidence(catalyst="中", crowding="高"),
                "WEAK": evidence(catalyst="弱", crowding="低"),
                "STRONG": evidence(catalyst="极强", crowding="低"),
            },
            expected_cutoff=EXPECTED_CUTOFF,
        )
        self.assertGreater(rows["HOT"].crowding_risk, rows["COOL"].crowding_risk)
        self.assertLess(rows["HOT"].recommendation_score, rows["COOL"].recommendation_score)
        self.assertGreater(rows["STRONG"].catalyst_quality, rows["WEAK"].catalyst_quality)

    def test_freshness_and_confidence_are_separate_execution_gates(self) -> None:
        fresh = build_scorecards(
            [metric("TARGET"), metric("WEAK", d1=-2.0, d3=-3.0, d5=-4.0, d10=-6.0, d20=-8.0)],
            {"TARGET": evidence(), "WEAK": evidence(catalyst="弱")},
            expected_cutoff=EXPECTED_CUTOFF,
        )["TARGET"]
        stale = build_scorecards(
            [metric("TARGET", data_cutoff="2026-08-09"), metric("WEAK")],
            {"TARGET": evidence(), "WEAK": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )["TARGET"]
        low_confidence = build_scorecards(
            [metric("TARGET", data_quality="UNKNOWN"), metric("WEAK")],
            {"TARGET": evidence(with_source=False), "WEAK": evidence()},
            expected_cutoff=EXPECTED_CUTOFF,
        )["TARGET"]
        self.assertEqual(fresh.timing_status, "可以买")
        self.assertFalse(stale.freshness_ok)
        self.assertEqual(stale.timing_status, "可以买")
        self.assertLess(low_confidence.data_confidence, 70.0)
        self.assertEqual(low_confidence.timing_status, "可以买")

    def test_candidate_routes_are_primary_round_robin_and_never_low_quality_fill(self) -> None:
        scorecards: dict[str, Scorecard] = {}
        for index in range(8):
            scorecards[f"T{index}"] = card(
                f"T{index}", trend=82 - index, entry=50, risk=30, catalyst=40, score=70 - index / 10,
            )
            scorecards[f"P{index}"] = card(
                f"P{index}", trend=45, entry=88 - index, risk=35, catalyst=40, score=67 - index / 10,
            )
            scorecards[f"C{index}"] = card(
                f"C{index}", trend=35, entry=50, risk=25, catalyst=90 - index, score=65 - index / 10,
            )
        clusters = {universe_id: universe_id for universe_id in scorecards}
        selected = select_candidate_pool(
            scorecards,
            clusters,
            route_size=8,
            minimum_size=20,
            maximum_size=24,
        )
        self.assertEqual(len(selected), 24)
        self.assertEqual(
            [row["pool_route"] for row in selected[:3]],
            ["趋势确认·不过热", "回调结束·重新转强", "早期催化·低拥挤"],
        )
        self.assertEqual(len({row["universe_id"] for row in selected}), 24)

        sparse = {
            "GOOD": card("GOOD", trend=75, entry=55, risk=30, catalyst=40, score=65),
            "LOW": card("LOW", trend=80, entry=55, risk=30, catalyst=40, score=30),
            "NO_ROUTE": card("NO_ROUTE", trend=20, entry=40, risk=20, catalyst=30, score=80),
        }
        sparse_selected = select_candidate_pool(
            sparse,
            {key: key for key in sparse},
            minimum_size=20,
            maximum_size=30,
        )
        self.assertEqual([row["universe_id"] for row in sparse_selected], ["GOOD"])
        self.assertNotIn("综合补位", {row["pool_route"] for row in sparse_selected})

    def test_route_internal_order_uses_balanced_fit_not_raw_momentum(self) -> None:
        scorecards = {
            "RAW-MOMENTUM": card(
                "RAW-MOMENTUM", trend=95, entry=45, risk=60, catalyst=20, score=45,
            ),
            "BALANCED": card(
                "BALANCED", trend=75, entry=60, risk=20, catalyst=20, score=80,
            ),
        }
        selected = select_candidate_pool(
            scorecards,
            {key: key for key in scorecards},
            route_size=1,
            minimum_size=1,
            maximum_size=1,
        )
        self.assertEqual([row["universe_id"] for row in selected], ["BALANCED"])

    def test_final_display_uses_recommendation_score_and_dynamic_bucket_reason(self) -> None:
        rows = [
            {"universe_id": "A", "recommendation_score": 90, "score": 1, "correlation_bucket": "X"},
            {"universe_id": "B", "recommendation_score": 80, "score": 100, "correlation_bucket": "X"},
            {"universe_id": "C", "recommendation_score": 70, "score": 100, "correlation_bucket": "X"},
            {"universe_id": "D", "recommendation_score": 40, "score": 100, "correlation_bucket": "Y"},
        ]
        kept, suppressed = final_display_candidates(rows, maximum_count=3, max_per_cluster=2, minimum_score=45)
        self.assertEqual([row["universe_id"] for row in kept], ["A", "B"])
        reasons = {row["universe_id"]: row["suppression_reason"] for row in suppressed}
        self.assertEqual(reasons["C"], "同一相关性桶最多展示2项")
        self.assertEqual(reasons["D"], "低于候选绝对线45分")


if __name__ == "__main__":
    unittest.main()

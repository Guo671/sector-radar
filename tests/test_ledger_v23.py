from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.config import local_now
from app.database import Database
from app.decision_drafts import DecisionDraftError, DecisionDraftService
from app.engine import UnknownFeeScheduleError, fee_rate_for_days
from app.fund_valuation import FundValuationService
from app.ledger import LedgerError, PortfolioLedgerService
from app.orchestrator import ResearchOrchestrator
from app.platform_catalog import PlatformCatalogService
from app.portfolio import active_capital_summary, fund_exposure_identity
from app.services import (
    daily_portfolio_equity,
    daily_workflow_status,
    holding_segment_metrics,
    holding_segment_review,
    portfolio_equity_drawdown,
    realized_exit_trades,
    strategy_review_metrics,
)
from app.trading_calendar import ChinaTradingCalendar


class FixedNavProvider:
    name = "test_official_nav"

    def __init__(self, nav_date: str, nav: float):
        self.nav_date = nav_date
        self.nav = nav

    def fetch(self, _code: str, _start: str, _end: str):
        return {self.nav_date: self.nav}, "https://example.test/fund-nav"


class LedgerV23Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "ledger-v23.sqlite3")
        self.service = PortfolioLedgerService(self.db)
        self.now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO funds(
                   fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                   purchase_fee_pct,status,notes,updated_at
               ) VALUES('001632','天弘中证食品饮料ETF联接C','930653.CSI',
                        '中证食品饮料主题指数','C','食品饮料',0,'可买','',?)
               ON CONFLICT(fund_code) DO UPDATE SET
                   fund_name=excluded.fund_name,index_code=excluded.index_code,
                   index_name=excluded.index_name,share_class=excluded.share_class,
                   correlation_bucket=excluded.correlation_bucket,updated_at=excluded.updated_at""",
            (self.now,),
        )
        self.db.execute(
            """UPDATE investable_funds SET relation_kind='同一指数',share_class='C',
                      strategy_kind='被动ETF联接',passive_verified=1,
                      public_buyable_status='已核对可申购',platform_checked_at=?,verified_at=?,
                      verification_algorithm=?
                 WHERE fund_code='001632'""",
            (self.now, self.now, STRICT_FIELD_SYNC_ALGORITHM_VERSION),
        )
        self.db.save_settings({
            "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
        })
        food_index = self.db.row(
            "SELECT id FROM investable_indices WHERE official_index_code='930653' AND active=1"
        )
        if food_index:
            PlatformCatalogService(self.db).refresh_admission(int(food_index["id"]))

    def tearDown(self):
        self.tmp.cleanup()

    def _trade(self, action: str, amount: float, units: float, nav: float, *,
               nav_date: str = "2026-08-08", confirm_date: str = "2026-08-09",
               active: int = 1) -> int:
        return self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES(?,?,?,?,?,?,0,'测试',?,?,'已确认','测试',?,?)""",
            (
                nav_date, "001632", action, amount, units, nav, self.now,
                confirm_date, nav_date, active,
            ),
        )

    def test_buy_partial_sell_clear_fifo_cash_and_pricing_date(self):
        opening = float(self.db.setting("active_cash_balance"))
        buy1 = self._trade("买入", 1000, 100, 10, nav_date="2026-08-07")
        buy2 = self._trade("买入", 600, 50, 12, nav_date="2026-08-08")
        self.assertTrue(self.service.post_confirmed_trade(buy1)["posted"])
        self.assertTrue(self.service.post_confirmed_trade(buy2)["posted"])
        self.assertEqual(
            self.db.row("SELECT pricing_date FROM cost_lots WHERE trade_id=?", (buy1,))["pricing_date"],
            "2026-08-07",
        )
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening - 1600)

        sell = self._trade("卖出", 1320, 120, 11)
        posted = self.service.post_confirmed_trade(sell)
        self.assertAlmostEqual(posted["cost_basis"], 1240)
        self.assertAlmostEqual(posted["realized_pnl"], 80)
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(holding["units"], 30)
        self.assertAlmostEqual(holding["cost"], 360)
        trade = self.db.row("SELECT * FROM trades WHERE id=?", (sell,))
        self.assertEqual(trade["result_pnl_status"], "已核算")
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening - 280)

        clear = self._trade("清仓", 330, 30, 11)
        self.service.post_confirmed_trade(clear)
        self.assertIsNone(self.db.row("SELECT id FROM holdings WHERE fund_code='001632' AND active=1"))
        self.service.reverse_posting(clear, "测试撤销清仓")
        restored = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(restored["units"], 30)
        self.assertAlmostEqual(restored["cost"], 360)
        self.assertEqual(self.db.row("SELECT status FROM trades WHERE id=?", (clear,))["status"], "已撤销")

    def test_legacy_nonactive_holding_partial_sell_and_reversal_are_exact(self):
        """A migrated non-strategy holding can be sold without fabricated lots.

        The user's Huatai bond position predates the unified cost-lot ledger.  A
        partial sale must therefore consume a synthetic legacy component,
        reduce units and cost proportionally, leave the 8,000-yuan strategy
        cash ledger untouched, and still be fully reversible.
        """
        code = "999998"
        self.db.execute(
            """INSERT INTO funds(
                   fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                   purchase_fee_pct,status,notes,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (code, "旧持仓债券基金C", "", "", "C", "债券", 0, "持有", "测试", self.now),
        )
        holding_id = self.db.execute(
            """INSERT INTO holdings(
                   fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,
                   active,counts_toward_active_capital
               ) VALUES(?,?,?,?,?,?,?,?,1,0)""",
            (
                code, "2026-07-30", 5757.83, 6721.69, 1.1674, "2026-08-12",
                "其他", "旧版迁移且没有成本批次",
            ),
        )
        original = self.db.row(
            "SELECT units,cost,latest_nav,nav_date,active,counts_toward_active_capital "
            "FROM holdings WHERE id=?",
            (holding_id,),
        )
        cash_before = float(self.db.setting("active_cash_balance"))
        sold_units = 2878.92
        sale_amount = 3384.75
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES('2026-08-13 14:00:00',?,'卖出',?,?,1.1757,0,
                        '测试旧持仓部分卖出',?,'2026-08-14','已确认','测试',
                        '2026-08-13',0)""",
            (code, sale_amount, sold_units, self.now),
        )

        expected_cost = float(original["cost"]) * sold_units / float(original["units"])
        posted = self.service.post_confirmed_trade(trade_id)
        self.assertTrue(posted["posted"])
        self.assertFalse(posted["active_capital"])
        self.assertAlmostEqual(posted["cost_basis"], expected_cost)
        self.assertAlmostEqual(posted["realized_pnl"], sale_amount - expected_cost)
        remaining = self.db.row(
            "SELECT units,cost,active,counts_toward_active_capital FROM holdings WHERE id=?",
            (holding_id,),
        )
        self.assertAlmostEqual(remaining["units"], float(original["units"]) - sold_units)
        self.assertAlmostEqual(remaining["cost"], float(original["cost"]) - expected_cost)
        self.assertEqual((remaining["active"], remaining["counts_toward_active_capital"]), (1, 0))
        consumption = self.db.row(
            "SELECT lot_id,units,cost_basis FROM lot_consumptions WHERE exit_trade_id=?",
            (trade_id,),
        )
        self.assertIsNone(consumption["lot_id"])
        self.assertAlmostEqual(consumption["units"], sold_units)
        self.assertAlmostEqual(consumption["cost_basis"], expected_cost)
        self.assertIsNone(self.db.row(
            "SELECT id FROM active_cash_ledger WHERE trade_id=?", (trade_id,)
        ))
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), cash_before)

        reversed_result = self.service.reverse_posting(trade_id, "测试撤销部分卖出")
        self.assertTrue(reversed_result["reversed"])
        restored = self.db.row(
            "SELECT units,cost,latest_nav,nav_date,active,counts_toward_active_capital "
            "FROM holdings WHERE id=?",
            (holding_id,),
        )
        self.assertEqual(restored, original)
        self.assertIsNone(self.db.row(
            "SELECT id FROM lot_consumptions WHERE exit_trade_id=?", (trade_id,)
        ))
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), cash_before)

    def test_pending_trade_cannot_keep_a_guessed_confirmation_date(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES('2026-08-07','001632','买入',100,50,2,0,'测试',?,
                        '','待确认','测试','2026-08-07',1)""",
            (self.now,),
        )
        result = self.service.finalize_trade(
            trade_id, {"status": "待确认", "confirm_date": "2026-08-10"},
        )
        self.assertFalse(result["posted"])
        row = self.db.row("SELECT status,confirm_date FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(row, {"status": "待确认", "confirm_date": ""})

    def test_confirm_requires_real_confirmation_date_and_rolls_back_atomically(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES('2026-08-07','001632','买入',100,50,2,0,'测试',?,
                        '','待确认','测试','2026-08-07',1)""",
            (self.now,),
        )
        before = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        cash_before = float(self.db.setting("active_cash_balance"))
        with self.assertRaisesRegex(LedgerError, "必须填写.*实际确认日"):
            self.service.finalize_trade(trade_id, {"status": "已确认", "confirm_date": ""})
        after = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(after, before)
        self.assertFalse(self.service.is_posted(trade_id))
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), cash_before)

    def test_review_counts_zero_to_zero_holding_segments_not_each_partial_exit(self):
        buy1 = self._trade("买入", 1000, 100, 10, nav_date="2026-08-07")
        buy2 = self._trade("买入", 600, 50, 12, nav_date="2026-08-08")
        partial = self._trade("卖出", 1320, 120, 11, nav_date="2026-08-10")
        clear = self._trade("清仓", 330, 30, 11, nav_date="2026-08-11")
        for trade_id in (buy1, buy2, partial):
            self.service.post_confirmed_trade(trade_id)

        before_close = holding_segment_review(self.db)
        self.assertFalse(any(
            row["fund_code"] == "001632" for row in before_close["completed"]
        ))
        open_segment = next(
            row for row in before_close["open"] if row["fund_code"] == "001632"
        )
        self.assertAlmostEqual(open_segment["remaining_units"], 30)

        self.service.post_confirmed_trade(clear)
        review = holding_segment_review(self.db)
        segment = next(
            row for row in review["completed"] if row["fund_code"] == "001632"
        )
        self.assertEqual((segment["buy_count"], segment["exit_count"]), (2, 2))
        self.assertEqual(segment["exit_trade_ids"], [partial, clear])
        self.assertEqual((segment["start_date"], segment["end_date"]), (
            "2026-08-07", "2026-08-11",
        ))
        self.assertEqual(segment["holding_days"], 5)
        self.assertAlmostEqual(segment["invested_amount"], 1600)
        self.assertAlmostEqual(segment["exit_amount"], 1650)
        self.assertAlmostEqual(segment["result_pnl"], 50)
        self.assertAlmostEqual(segment["return_pct"], 3.125)

        metrics = holding_segment_metrics([segment])
        self.assertEqual((metrics["segment_count"], metrics["known_segment_count"]), (1, 1))
        self.assertAlmostEqual(metrics["pnl_total"], 50)
        self.assertAlmostEqual(metrics["win_rate_pct"], 100)

        second_buy = self._trade("买入", 200, 20, 10, nav_date="2026-08-12")
        second_clear = self._trade("清仓", 180, 20, 9, nav_date="2026-08-13")
        self.service.post_confirmed_trade(second_buy)
        self.service.post_confirmed_trade(second_clear)
        segments = [
            row for row in holding_segment_review(self.db)["completed"]
            if row["fund_code"] == "001632"
        ]
        self.assertEqual(len(segments), 2)
        self.assertEqual([row["result_pnl"] for row in segments], [50, -20])
        metrics = holding_segment_metrics(segments)
        self.assertAlmostEqual(metrics["win_rate_pct"], 50)
        self.assertAlmostEqual(metrics["profit_factor"], 2.5)

    def test_complete_segment_with_unknown_exit_pnl_is_not_counted_as_zero(self):
        buy = self._trade("买入", 500, 50, 10, nav_date="2026-08-07")
        clear = self._trade("清仓", 500, 50, 10, nav_date="2026-08-11")
        self.service.post_confirmed_trade(buy)
        self.service.post_confirmed_trade(clear)
        self.db.execute(
            "UPDATE trades SET result_pnl=NULL,result_pnl_status='待核算' WHERE id=?",
            (clear,),
        )
        segment = next(
            row for row in holding_segment_review(self.db)["completed"]
            if row["fund_code"] == "001632"
        )
        self.assertEqual(segment["pnl_status"], "待核算")
        self.assertIsNone(segment["result_pnl"])
        self.assertIn(f"#{clear}", segment["pnl_reason"])
        metrics = holding_segment_metrics([segment])
        self.assertEqual((metrics["known_segment_count"], metrics["unknown_segment_count"]), (0, 1))
        self.assertEqual(metrics["pnl_total"], 0)
        self.assertIsNone(metrics["win_rate_pct"])

    def test_legacy_orphan_cost_lots_can_reconcile_one_complete_segment(self):
        self.db.execute(
            "UPDATE holdings SET units=1452.41,cost=3000,active=0 WHERE fund_code='020341'"
        )
        for units, amount, source in (
            (1451.31, 3000.0, "历史持仓成本补录"),
            (1.10, 0.0, "支付宝活动赠送；赠送日期未知，清仓核对时补录"),
        ):
            self.db.execute(
                """INSERT INTO cost_lots(
                       fund_code,trade_id,occurrence_id,confirmed_date,pricing_date,
                       units,remaining_units,gross_amount,fee,net_invested,nav,source,created_at
                   ) VALUES('020341',NULL,NULL,'2026-07-03','2026-07-03',?,0,?,0,?,0,?,?)""",
                (units, amount, amount, source, self.now),
            )
        clear = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital,
                   result_pnl,result_pnl_status
               ) VALUES('2026-08-06','020341','清仓',3049.04,1452.41,2.0993,0,
                        '用户核对支付宝实际成交',?,'2026-08-06','已确认','支付宝实际成交',
                        '2026-08-06',1,49.04,'已核算')""",
            (self.now,),
        )
        self.db.execute(
            """INSERT INTO ledger_postings(
                   trade_id,posting_kind,critical_snapshot_json,metadata_json,posted_at,posting_seq
               ) VALUES(?,'卖出','{}','{"migration":"test"}',?,999)""",
            (clear, self.now),
        )
        review = holding_segment_review(self.db)
        gold = next(
            row for row in review["completed"] if row["fund_code"] == "020341"
        )
        self.assertEqual((gold["start_date"], gold["end_date"]), ("", "2026-08-06"))
        self.assertIsNone(gold["holding_days"])
        self.assertEqual(gold["buy_count"], 2)
        self.assertEqual(gold["exit_count"], 1)
        self.assertAlmostEqual(gold["invested_amount"], 3000)
        self.assertAlmostEqual(gold["exit_amount"], 3049.04)
        self.assertAlmostEqual(gold["result_pnl"], 49.04)
        self.assertEqual(gold["pnl_status"], "已核算")

    def test_posting_is_idempotent_and_critical_edits_require_reversal(self):
        trade_id = self._trade("买入", 500, 50, 10)
        self.assertTrue(self.service.post_confirmed_trade(trade_id)["posted"])
        self.assertFalse(self.service.post_confirmed_trade(trade_id)["posted"])
        row = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.service.assert_trade_mutation_allowed(trade_id, row)
        changed = dict(row)
        changed["amount"] = 501
        with self.assertRaises(LedgerError):
            self.service.assert_trade_mutation_allowed(trade_id, changed)
        self.assertTrue(self.service.reverse_posting(trade_id, "录入金额有误")["reversed"])
        self.assertIsNone(self.db.row("SELECT id FROM holdings WHERE fund_code='001632' AND active=1"))
        self.assertTrue(self.service.is_posted(trade_id))
        with self.assertRaises(LedgerError):
            self.service.assert_trade_mutation_allowed(trade_id, changed)

    def test_finalize_trade_is_atomic_when_hard_gate_rejects_posting(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES('2026-08-08','001632','买入',100,10,10,0,'原始待确认',?,
                        '','待确认','测试','2026-08-08',1)""",
            (self.now,),
        )
        before = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        proposed = {
            **before,
            "amount": 99999,
            "units": 9999.9,
            "confirm_date": "2026-08-09",
            "status": "已确认",
            "rationale": "这次保存必须随过账失败一起回滚",
        }
        with self.assertRaises(LedgerError):
            self.service.finalize_trade(trade_id, proposed)
        after = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(after["status"], "待确认")
        self.assertEqual(after["amount"], before["amount"])
        self.assertEqual(after["rationale"], before["rationale"])
        self.assertIsNone(self.db.row("SELECT 1 FROM ledger_postings WHERE trade_id=?", (trade_id,)))

    def test_holding_classification_cannot_be_overwritten_by_trade_flag(self):
        first = self._trade("买入", 500, 50, 10, active=0)
        self.service.post_confirmed_trade(first)
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertEqual(holding["counts_toward_active_capital"], 0)

        conflicting_buy = self._trade("买入", 100, 10, 10, active=1)
        with self.assertRaisesRegex(LedgerError, "资金分类|主动资金分类"):
            self.service.post_confirmed_trade(conflicting_buy)
        conflicting_exit = self._trade("卖出", 100, 10, 10, active=1)
        with self.assertRaisesRegex(LedgerError, "资金分类|主动资金分类"):
            self.service.post_confirmed_trade(conflicting_exit)
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertEqual(holding["counts_toward_active_capital"], 0)
        self.assertAlmostEqual(holding["units"], 50)

    def test_reclassify_then_reverse_buy_cannot_inflate_active_cash(self):
        opening = float(self.db.setting("active_cash_balance"))
        buy = self._trade("买入", 1000, 100, 10, active=1)
        self.service.post_confirmed_trade(buy)
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening - 1000)
        self.service.reclassify_holding(holding["id"], 0, "移出主动资金后纠正原交易")
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening)
        self.service.reverse_posting(buy, "纠正原买入")
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening)
        self.assertIsNone(self.db.row("SELECT id FROM holdings WHERE fund_code='001632' AND active=1"))

    def test_posting_sequence_blocks_later_trade_even_in_same_second(self):
        fixed = local_now()
        first = self._trade("买入", 100, 10, 10, active=0)
        second = self._trade("买入", 100, 10, 10, active=0)
        from unittest.mock import patch
        with patch("app.ledger.local_now", return_value=fixed):
            self.service.post_confirmed_trade(first)
            self.service.post_confirmed_trade(second)
        postings = self.db.rows(
            "SELECT trade_id,posted_at,posting_seq FROM ledger_postings WHERE trade_id IN (?,?) ORDER BY posting_seq",
            (first, second),
        )
        self.assertEqual(postings[0]["posted_at"], postings[1]["posted_at"])
        self.assertLess(postings[0]["posting_seq"], postings[1]["posting_seq"])
        with self.assertRaisesRegex(LedgerError, "后续交易"):
            self.service.reverse_posting(first, "不应越过后续交易")
        self.service.reverse_posting(second, "先撤后续交易")
        self.service.reverse_posting(first, "再撤前一交易")

    def test_explicit_nonactive_draft_does_not_reserve_active_cash(self):
        before = active_capital_summary(self.db)
        self.db.execute(
            """INSERT INTO decision_drafts(
                   trade_date,source_kind,source_key,fund_code,fund_name,action,planned_amount,
                   status,created_at,updated_at,counts_toward_active_capital
               ) VALUES('2026-08-09','用户手工','nonactive','001632','测试','买入',999,
                        '草稿',?,?,0)""",
            (self.now, self.now),
        )
        after = active_capital_summary(self.db)
        self.assertEqual(after["reserved"], before["reserved"])
        self.assertEqual(after["cash_available"], before["cash_available"])

    def test_exact_index_mapping_also_carries_correlation_bucket(self):
        identity = fund_exposure_identity(self.db, "001632")
        self.assertEqual(identity["index_key"], "CSI:930653")
        self.assertEqual(identity["bucket"], "H30177")

    def test_confirmed_buy_cannot_bypass_same_index_limit_with_live_draft(self):
        self.db.execute(
            """INSERT INTO decision_drafts(
                   trade_date,source_kind,source_key,fund_code,fund_name,action,planned_amount,
                   status,created_at,updated_at,counts_toward_active_capital
               ) VALUES('2026-08-09','用户手工','same-index-reserve','001632','测试','买入',1500,
                        '草稿',?,?,1)""",
            (self.now, self.now),
        )
        trade_id = self._trade("买入", 1000, 100, 10)
        with self.assertRaisesRegex(LedgerError, "同一指数|单指数"):
            self.service.post_confirmed_trade(trade_id)
        self.assertFalse(self.service.is_posted(trade_id))
        self.assertIsNone(
            self.db.row("SELECT id FROM cost_lots WHERE trade_id=?", (trade_id,))
        )

    def test_stale_execution_evidence_blocks_confirmed_active_buy(self):
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at='2020-01-01',verified_at='2020-01-01' "
            "WHERE fund_code='001632'"
        )
        trade_id = self._trade("买入", 100, 10, 10)
        with self.assertRaises(LedgerError):
            self.service.post_confirmed_trade(trade_id)
        self.assertIsNone(self.db.row("SELECT id FROM cost_lots WHERE trade_id=?", (trade_id,)))

    def test_user_confirmed_historical_buy_does_not_require_todays_execution_ttl(self):
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at='2026-08-17T01:00:00+08:00',"
            "verified_at='2026-08-17T01:00:00+08:00' WHERE fund_code='001632'"
        )
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital
               ) VALUES('2026-08-17','001632','买入',600,60,10,0,'支付宝已给出正式成交',?,
                        '','待确认','用户手工待确认','2026-08-18',1)""",
            (self.now,),
        )
        fixed = datetime.fromisoformat("2026-08-19T12:00:00+08:00")
        proposed = {
            **self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,)),
            "status": "已确认", "confirm_date": "2026-08-19",
        }
        with patch("app.ledger.local_now", return_value=fixed):
            result = self.service.finalize_trade(trade_id, proposed)
        self.assertTrue(result["posted"])
        self.assertIsNotNone(
            self.db.row("SELECT id FROM cost_lots WHERE trade_id=?", (trade_id,))
        )

    def test_posted_buy_fee_correction_is_audited_and_preserves_later_sale(self):
        buy = self._trade("买入", 1000, 100, 10, active=0)
        self.db.execute("UPDATE trades SET fee=100 WHERE id=?", (buy,))
        self.service.post_confirmed_trade(buy)
        sale = self._trade("卖出", 500, 50, 10, active=0)
        self.service.post_confirmed_trade(sale)
        holding_before = self.db.row(
            "SELECT units,cost FROM holdings WHERE fund_code='001632' AND active=1"
        )
        sale_before = self.db.row(
            "SELECT result_pnl,result_pnl_status FROM trades WHERE id=?", (sale,)
        )

        result = self.service.correct_posted_buy_fee(
            buy, 0, "误把部分交易金额填入费用；支付宝实际费用为0",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(self.db.row("SELECT fee FROM trades WHERE id=?", (buy,))["fee"], 0)
        lot = self.db.row(
            "SELECT fee,net_invested,remaining_units FROM cost_lots WHERE trade_id=?", (buy,)
        )
        self.assertEqual((lot["fee"], lot["net_invested"], lot["remaining_units"]), (0, 1000, 50))
        snapshot = json.loads(self.db.row(
            "SELECT critical_snapshot_json FROM ledger_postings WHERE trade_id=?", (buy,)
        )["critical_snapshot_json"])
        self.assertEqual(snapshot["fee"], 0)
        correction = self.db.row(
            "SELECT field_name,old_value,new_value,reason FROM trade_corrections WHERE trade_id=?",
            (buy,),
        )
        self.assertEqual(correction["field_name"], "fee")
        self.assertEqual((float(correction["old_value"]), float(correction["new_value"])), (100, 0))
        self.assertEqual(
            self.db.row("SELECT units,cost FROM holdings WHERE fund_code='001632' AND active=1"),
            holding_before,
        )
        self.assertEqual(
            self.db.row("SELECT result_pnl,result_pnl_status FROM trades WHERE id=?", (sale,)),
            sale_before,
        )
        again = self.service.correct_posted_buy_fee(buy, 0, "重复核对")
        self.assertFalse(again["changed"])
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM trade_corrections WHERE trade_id=?", (buy,))["n"],
            1,
        )

    def test_limit_adjustment_is_audited_external_flow(self):
        old = float(self.db.setting("active_capital_limit"))
        result = self.service.adjust_active_capital_limit(old + 1000, "测试扩充额度")
        self.assertEqual(result["new_limit"], old + 1000)
        event = self.db.row(
            "SELECT * FROM active_cash_ledger WHERE event_kind='CAPITAL_LIMIT_ADJUSTMENT' ORDER BY id DESC LIMIT 1"
        )
        self.assertAlmostEqual(event["amount"], 1000)
        snapshot = self.db.row("SELECT * FROM portfolio_equity_snapshots ORDER BY id DESC LIMIT 1")
        self.assertAlmostEqual(snapshot["external_flow_total"], 1000)

    def test_unknown_fee_schedule_never_silently_means_zero(self):
        with self.assertRaises(UnknownFeeScheduleError):
            fee_rate_for_days([], 10)

    def test_daily_valuation_starts_on_pricing_not_confirmation_date(self):
        buy = self._trade(
            "买入", 1000, 100, 10, nav_date="2026-08-07", confirm_date="2026-08-09"
        )
        self.service.post_confirmed_trade(buy)
        for day, nav in (("2026-08-07", 10), ("2026-08-08", 10.2)):
            self.db.execute(
                """INSERT INTO fund_nav_history(fund_code,nav_date,nav,source,source_url,fetched_at)
                   VALUES('001632',?,?,'测试','',?)""",
                (day, nav, self.now),
            )
        rows = FundValuationService(self.db).daily_rows(30)
        dates = [row["nav_date"] for row in rows if row["fund_code"] == "001632"]
        self.assertIn("2026-08-07", dates)

    def test_close_finalize_checks_catalog_and_keeps_explicit_nonactive_zero(self):
        today = local_now().date().isoformat()
        closed = datetime.fromisoformat(today + "T16:00:00")
        now = closed.isoformat(timespec="seconds")
        food_index = self.db.row(
            """SELECT id,universe_id FROM investable_indices
                 WHERE official_index_code='930653' AND active=1"""
        )
        self.assertIsNotNone(food_index)
        task_id = ResearchOrchestrator(self.db).create_job("完整扫描")
        job = self.db.row(
            "SELECT total_items,ranking_policy_version,ranking_policy_json FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        )
        self.db.execute(
            "UPDATE analysis_jobs SET status='已完成',stage='完成',completed_items=? WHERE task_id=?",
            (int(job["total_items"]), task_id),
        )
        self.db.execute(
            """INSERT INTO radar_runs(
                   task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
               ) VALUES(?,?,'测试',?,?,?,?,'测试',?,?)""",
            (
                task_id,
                ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(closed).isoformat(),
                int(job["total_items"]), int(job["total_items"]),
                job["ranking_policy_version"], job["ranking_policy_json"],
                json.dumps({"protocol_version":"3.0"}), now,
            ),
        )
        candidate = self.db.execute(
            """INSERT INTO candidates(
                   task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,status,
                   research_tilt,timing_status,execution_status,score,suggested_amount,
                   correlation_bucket,thesis,risk,allocation_pct,target_allocation_pct,data_confidence,updated_at
                ) VALUES(?,1,?,'930653.CSI','食品饮料','001632',
                         '天弘中证食品饮料ETF联接C','推荐买','推荐买','可以买','可执行',
                         90,800,'食品饮料','测试','测试',10,10,90,?)""",
            (task_id, food_index["universe_id"], now),
        )
        drafts = DecisionDraftService(self.db)
        drafts.add_candidate(candidate, as_of=closed)
        result = drafts.finalize_after_close(as_of=closed)
        self.assertEqual(result["trades"], 1)
        active_trade = self.db.row(
            "SELECT * FROM trades WHERE source LIKE '今日草稿%' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(active_trade["counts_toward_active_capital"], 1)

        drafts.add_manual(
            fund_code="020741", action="买入", planned_amount=100,
            pricing_date="2026-08-07", as_of=closed,
        )
        second = drafts.finalize_after_close(as_of=closed)
        self.assertEqual(second["trades"], 1)
        nonactive = self.db.row(
            "SELECT * FROM trades WHERE fund_code='020741' AND source LIKE '今日草稿%' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(nonactive["counts_toward_active_capital"], 0)

    def test_end_to_end_ai_draft_close_confirm_holding_partial_exit_and_review(self):
        """Exercise the same accounting boundary as the seven-step daily UI.

        This deliberately keeps the draft free of invented OTC-fund units.  The
        units and official NAV only enter through ``finalize_trade`` after the
        close-created pending trade is explicitly confirmed by the user.
        """
        # Keep this lifecycle deterministic.  Using the wall-clock date makes
        # the test legitimately fail on weekends/holidays because production
        # correctly rejects a non-trading NAV date.
        today = "2026-08-14"
        morning = datetime.fromisoformat(today + "T14:00:00")
        closed = datetime.fromisoformat(today + "T16:00:00")
        now = closed.isoformat(timespec="seconds")
        food_index = self.db.row(
            """SELECT id,universe_id FROM investable_indices
                 WHERE official_index_code='930653' AND active=1"""
        )
        self.assertIsNotNone(food_index)

        task_id = ResearchOrchestrator(self.db).create_job("完整扫描")
        job = self.db.row(
            "SELECT total_items,ranking_policy_version,ranking_policy_json "
            "FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        )
        self.db.execute(
            "UPDATE analysis_jobs SET status='已完成',stage='完成',completed_items=?,research_date=? "
            "WHERE task_id=?",
            (int(job["total_items"]), today, task_id),
        )
        self.db.execute(
            """INSERT INTO radar_runs(
                   task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
               ) VALUES(?,?,'测试',?,?,?,?,'贯通验收',?,?)""",
            (
                task_id,
                ChinaTradingCalendar.from_db(self.db)
                .latest_completed_trading_day(morning).isoformat(),
                int(job["total_items"]), int(job["total_items"]),
                job["ranking_policy_version"], job["ranking_policy_json"],
                json.dumps({"protocol_version": "3.0"}), now,
            ),
        )
        candidate_id = self.db.execute(
            """INSERT INTO candidates(
                   task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,status,
                   research_tilt,timing_status,execution_status,score,suggested_amount,
                   correlation_bucket,thesis,risk,allocation_pct,target_allocation_pct,
                   data_confidence,updated_at
               ) VALUES(?,1,?,'930653.CSI','食品饮料','001632',
                        '天弘中证食品饮料ETF联接C','推荐买','推荐买','可以买','可执行',
                        90,800,'食品饮料','贯通验收','测试风险',10,10,90,?)""",
            (task_id, food_index["universe_id"], now),
        )
        # The workflow below is replayed as of the fixed operation day, so its
        # job date and executable-fund evidence must use that same session.
        # `add_candidate(as_of=...)` must not consult the unrelated wall clock.
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at=?,verified_at=? "
            "WHERE fund_code='001632'",
            (now, now),
        )

        drafts = DecisionDraftService(self.db)
        from app.services import latest_current_radar_run, research_run_status
        self.assertIsNotNone(
            latest_current_radar_run(self.db, as_of=morning),
            research_run_status(self.db),
        )
        draft_id = drafts.add_candidate(candidate_id, as_of=morning)
        draft = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        self.assertEqual((draft["status"], draft["planned_units"]), ("草稿", 0))
        before_close = daily_workflow_status(self.db, as_of=morning)
        self.assertEqual(before_close["steps"][3]["status"], "存活 1 项")

        close_result = drafts.finalize_after_close(as_of=closed)
        self.assertEqual(close_result, {"trades": 1, "recurring": 0, "total": 1})
        pending = self.db.row(
            "SELECT * FROM trades WHERE source LIKE '今日草稿%' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual((pending["status"], pending["units"], pending["nav"]), ("待确认", 0, 0))
        self.assertIsNone(
            self.db.row("SELECT id FROM holdings WHERE fund_code='001632' AND active=1")
        )

        opening_cash = float(self.db.setting("active_cash_balance"))
        with patch("app.ledger.local_now", return_value=closed):
            buy = self.service.finalize_trade(
                int(pending["id"]),
                {
                    "trade_date": today,
                    "confirm_date": today,
                    "fund_code": "001632",
                    "action": "买入",
                    "amount": 800,
                    "units": 80,
                    "nav": 10,
                    "fee": 0,
                    "nav_date": today,
                    "status": "已确认",
                    "rationale": "用户核对支付宝正式确认结果",
                    "counts_toward_active_capital": 1,
                },
            )
        self.assertTrue(buy["posted"])
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(holding["units"], 80)
        self.assertAlmostEqual(holding["cost"], 800)
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening_cash - 800)

        sell_draft_id = drafts.add_manual(
            fund_code="001632", action="卖出", planned_amount=440,
            rationale="用户盘中决定部分卖出", pricing_date=today,
            as_of=closed, counts_toward_active_capital=1,
        )
        sell_close = drafts.finalize_after_close(as_of=closed)
        self.assertEqual(sell_close["trades"], 1)
        sell_pending = self.db.row(
            "SELECT * FROM trades WHERE id=(SELECT trade_id FROM decision_drafts WHERE id=?)",
            (sell_draft_id,),
        )
        with patch("app.ledger.local_now", return_value=closed):
            sell = self.service.finalize_trade(
                int(sell_pending["id"]),
                {
                    "trade_date": today,
                    "confirm_date": today,
                    "fund_code": "001632",
                    "action": "卖出",
                    "amount": 440,
                    "units": 40,
                    "nav": 11,
                    "fee": 0,
                    "nav_date": today,
                    "status": "已确认",
                    "rationale": "用户核对支付宝正式卖出结果",
                    "counts_toward_active_capital": 1,
                },
            )
        self.assertTrue(sell["posted"])
        self.assertAlmostEqual(sell["cost_basis"], 400)
        self.assertAlmostEqual(sell["realized_pnl"], 40)

        remaining = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(remaining["units"], 40)
        self.assertAlmostEqual(remaining["cost"], 400)
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening_cash - 360)
        lot = self.db.row(
            "SELECT * FROM cost_lots WHERE fund_code='001632' AND trade_id=?",
            (int(pending["id"]),),
        )
        self.assertAlmostEqual(lot["remaining_units"], 40)

        exits = [
            row for row in realized_exit_trades(self.db)
            if int(row["id"]) == int(sell_pending["id"])
        ]
        review = strategy_review_metrics(exits, 8000)
        self.assertEqual((review["trade_count"], review["unknown_trade_count"]), (1, 0))
        self.assertAlmostEqual(review["pnl_total"], 40)
        drawdown = portfolio_equity_drawdown(self.db)
        # Both snapshots were created on the same day.  They are one daily
        # observation, not two independent drawdown samples.
        self.assertEqual(drawdown["sample_count"], 1)
        self.assertIsNone(drawdown["max_drawdown_pct"])
        final_status = daily_workflow_status(self.db, as_of=closed)
        self.assertEqual(final_status["steps"][4]["status"], "已写入 2 项")
        self.assertIn(final_status["steps"][5]["state"], {"done", "ready"})

    def test_cross_day_drafts_require_explicit_carry_or_abandon_and_recurring_cannot_carry(self):
        drafts = DecisionDraftService(self.db)
        old = datetime(2026, 8, 7, 14, 0)
        today = datetime(2026, 8, 10, 10, 0)
        old_id = drafts.add_manual(
            fund_code="001632", action="买入", planned_amount=200,
            pricing_date="2026-08-07", as_of=old, counts_toward_active_capital=1,
        )
        self.assertEqual(drafts.list_for_day("2026-08-10"), [])
        self.assertEqual([row["id"] for row in drafts.list_stale_drafts("2026-08-10")], [old_id])
        self.assertAlmostEqual(active_capital_summary(self.db)["reserved"], 200)

        carried_id = drafts.carry_stale_draft(old_id, as_of=today)
        old_row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (old_id,))
        carried = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (carried_id,))
        self.assertEqual(old_row["status"], "已结转")
        self.assertEqual((carried["trade_date"], carried["status"]), ("2026-08-10", "草稿"))
        self.assertEqual([row["id"] for row in drafts.list_for_day("2026-08-10")], [carried_id])
        self.assertAlmostEqual(active_capital_summary(self.db)["reserved"], 200)

        abandoned_id = drafts.add_manual(
            fund_code="001632", action="买入", planned_amount=100,
            pricing_date="2026-08-07", as_of=old, counts_toward_active_capital=1,
        )
        drafts.abandon_stale_draft(abandoned_id, reason="周五没有下单", as_of=today)
        abandoned = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (abandoned_id,))
        self.assertEqual((abandoned["status"], abandoned["result_note"]), ("已放弃", "周五没有下单"))
        self.assertAlmostEqual(active_capital_summary(self.db)["reserved"], 200)

        self.assertEqual(drafts.ensure_today_recurring(as_of=old), 1)
        recurring = self.db.row(
            "SELECT * FROM decision_drafts WHERE trade_date='2026-08-07' AND recurring_occurrence_id IS NOT NULL"
        )
        with self.assertRaisesRegex(DecisionDraftError, "不能顺延"):
            drafts.carry_stale_draft(recurring["id"], as_of=today)
        drafts.abandon_stale_draft(recurring["id"], reason="当天未扣款", as_of=today)
        occurrence = self.db.row(
            "SELECT * FROM recurring_occurrences WHERE id=?", (recurring["recurring_occurrence_id"],)
        )
        self.assertEqual(occurrence["status"], "已跳过")

    def test_stale_recurring_deduction_links_one_explicit_pending_trade_without_posting(self):
        drafts = DecisionDraftService(self.db)
        old = datetime(2026, 8, 7, 14, 0)
        today = datetime(2026, 8, 10, 10, 0)
        self.assertEqual(drafts.ensure_today_recurring(as_of=old), 1)
        draft = self.db.row(
            "SELECT * FROM decision_drafts WHERE trade_date='2026-08-07' "
            "AND recurring_occurrence_id IS NOT NULL"
        )
        occurrence_id = int(draft["recurring_occurrence_id"])
        self.db.execute(
            """UPDATE recurring_occurrences
                  SET pricing_date='2026-08-07',confirm_date='2026-08-11',
                      nav=8.0,units=1.25,fee=0,status='可确认入账',
                      source='测试公开净值',source_url='https://example.test/nav',
                      fetched_at=?,updated_at=?
                WHERE id=?""",
            (self.now, self.now, occurrence_id),
        )
        pending_trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date,source_url,
                   counts_toward_active_capital,result_pnl_status
               ) VALUES('2026-08-07','014978','买入',10,0,0,0,
                        '用户此前手工登记',?,'','待确认','手工记录','','',0,'不适用')""",
            (self.now,),
        )
        holding_before = self.db.row(
            "SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1"
        )
        cash_before = float(self.db.setting("active_cash_balance"))
        counts_before = {
            "trades": self.db.row("SELECT COUNT(*) AS n FROM trades")["n"],
            "lots": self.db.row("SELECT COUNT(*) AS n FROM cost_lots")["n"],
            "postings": self.db.row("SELECT COUNT(*) AS n FROM ledger_postings")["n"],
        }

        preview = drafts.preview_stale_recurring_deduction(draft["id"], as_of=today)
        self.assertIsNone(preview["linked_trade"])
        self.assertEqual(
            [row["id"] for row in preview["matching_trades"]], [pending_trade_id]
        )
        with self.assertRaisesRegex(DecisionDraftError, "必须由用户明确"):
            drafts.acknowledge_stale_recurring_deduction(draft["id"], as_of=today)

        result = drafts.acknowledge_stale_recurring_deduction(
            draft["id"], link_trade_id=pending_trade_id, as_of=today,
        )
        self.assertFalse(result["already_confirmed"])
        self.assertEqual(result["linked_trade_id"], pending_trade_id)
        occurrence = self.db.row(
            "SELECT * FROM recurring_occurrences WHERE id=?", (occurrence_id,)
        )
        self.assertEqual(int(occurrence["deduction_confirmed"]), 1)
        self.assertEqual(occurrence["deduction_confirmation_source"], "用户在今日决策确认")
        self.assertEqual(occurrence["status"], "可确认入账")
        updated_draft = self.db.row(
            "SELECT * FROM decision_drafts WHERE id=?", (draft["id"],)
        )
        self.assertEqual(updated_draft["status"], "等待净值确认")
        self.assertEqual(updated_draft["trade_id"], pending_trade_id)
        linked = self.db.row("SELECT * FROM trades WHERE id=?", (pending_trade_id,))
        self.assertEqual(linked["action"], "定投")
        self.assertEqual(linked["status"], "待确认")
        self.assertEqual(linked["recurring_occurrence_id"], occurrence_id)
        self.assertEqual((linked["nav"], linked["units"]), (8.0, 1.25))
        self.assertEqual(linked["confirm_date"], "")
        self.assertEqual(
            self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1"),
            holding_before,
        )
        self.assertEqual(float(self.db.setting("active_cash_balance")), cash_before)
        self.assertEqual(
            {
                "trades": self.db.row("SELECT COUNT(*) AS n FROM trades")["n"],
                "lots": self.db.row("SELECT COUNT(*) AS n FROM cost_lots")["n"],
                "postings": self.db.row("SELECT COUNT(*) AS n FROM ledger_postings")["n"],
            },
            counts_before,
        )

        repeated = drafts.acknowledge_stale_recurring_deduction(draft["id"], as_of=today)
        self.assertTrue(repeated["already_confirmed"])
        self.assertEqual(repeated["linked_trade_id"], pending_trade_id)
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM trades")["n"], counts_before["trades"]
        )

    def test_stale_recurring_deduction_without_trade_waits_for_nav_and_does_not_guess(self):
        drafts = DecisionDraftService(self.db)
        old = datetime(2026, 8, 7, 14, 0)
        today = datetime(2026, 8, 10, 10, 0)
        self.assertEqual(drafts.ensure_today_recurring(as_of=old), 1)
        draft = self.db.row(
            "SELECT * FROM decision_drafts WHERE trade_date='2026-08-07' "
            "AND recurring_occurrence_id IS NOT NULL"
        )
        counts_before = {
            "trades": self.db.row("SELECT COUNT(*) AS n FROM trades")["n"],
            "holdings": self.db.row("SELECT COUNT(*) AS n FROM holdings")["n"],
            "lots": self.db.row("SELECT COUNT(*) AS n FROM cost_lots")["n"],
            "postings": self.db.row("SELECT COUNT(*) AS n FROM ledger_postings")["n"],
        }
        cash_before = float(self.db.setting("active_cash_balance"))

        result = drafts.acknowledge_stale_recurring_deduction(draft["id"], as_of=today)
        self.assertIsNone(result["linked_trade_id"])
        self.assertEqual(result["nav"], 0)
        self.assertEqual(result["units"], 0)
        self.assertIn("等待正式净值", result["settlement_status"])
        occurrence = self.db.row(
            "SELECT * FROM recurring_occurrences WHERE id=?",
            (draft["recurring_occurrence_id"],),
        )
        self.assertEqual(int(occurrence["deduction_confirmed"]), 1)
        self.assertEqual(occurrence["status"], "待扣款核对")
        updated_draft = self.db.row(
            "SELECT * FROM decision_drafts WHERE id=?", (draft["id"],)
        )
        self.assertEqual(updated_draft["status"], "等待净值确认")
        self.assertIsNone(updated_draft["trade_id"])
        self.assertEqual(float(self.db.setting("active_cash_balance")), cash_before)
        self.assertEqual(
            {
                "trades": self.db.row("SELECT COUNT(*) AS n FROM trades")["n"],
                "holdings": self.db.row("SELECT COUNT(*) AS n FROM holdings")["n"],
                "lots": self.db.row("SELECT COUNT(*) AS n FROM cost_lots")["n"],
                "postings": self.db.row("SELECT COUNT(*) AS n FROM ledger_postings")["n"],
            },
            counts_before,
        )

    def test_stale_recurring_deduction_never_rewrites_an_unsafe_linked_trade(self):
        drafts = DecisionDraftService(self.db)
        old = datetime(2026, 8, 7, 14, 0)
        today = datetime(2026, 8, 10, 10, 0)
        self.assertEqual(drafts.ensure_today_recurring(as_of=old), 1)
        draft = self.db.row(
            "SELECT * FROM decision_drafts WHERE trade_date='2026-08-07' "
            "AND recurring_occurrence_id IS NOT NULL"
        )
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date,
                   recurring_occurrence_id,counts_toward_active_capital,result_pnl_status
               ) VALUES('2026-08-07','014978','定投',10,1.23,8.1,0,
                        '不可覆盖的历史关联',?,'2026-08-11','已确认','测试',
                        '2026-08-07',?,0,'不适用')""",
            (self.now, draft["recurring_occurrence_id"]),
        )
        before = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        with self.assertRaisesRegex(DecisionDraftError, "不是可安全修改"):
            drafts.acknowledge_stale_recurring_deduction(draft["id"], as_of=today)
        after = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(after, before)
        occurrence = self.db.row(
            "SELECT * FROM recurring_occurrences WHERE id=?",
            (draft["recurring_occurrence_id"],),
        )
        self.assertEqual(int(occurrence["deduction_confirmed"]), 0)
        self.assertEqual(
            self.db.row("SELECT status FROM decision_drafts WHERE id=?", (draft["id"],))["status"],
            "草稿",
        )

    def test_schema_v39_has_recurring_deduction_and_trade_correction_audit(self):
        columns = {
            row["name"]: row for row in self.db.rows("PRAGMA table_info(recurring_occurrences)")
        }
        self.assertEqual(int(self.db.setting("schema_version")), 39)
        self.assertIn("deduction_confirmed", columns)
        self.assertIn("deduction_confirmed_at", columns)
        self.assertIn("deduction_confirmation_source", columns)
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM recurring_occurrences "
                "WHERE deduction_confirmed<>0 OR deduction_confirmed_at<>'' "
                "OR deduction_confirmation_source<>''"
            )["n"],
            0,
        )
        correction_columns = {
            row["name"] for row in self.db.rows("PRAGMA table_info(trade_corrections)")
        }
        self.assertTrue(
            {"trade_id", "field_name", "old_value", "new_value", "reason", "created_at"}
            .issubset(correction_columns)
        )

    def test_reclassifying_holding_updates_cash_and_writes_audited_events(self):
        holding_id = self.db.execute(
            """INSERT INTO holdings(
                   fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,active,
                   counts_toward_active_capital
               ) VALUES('001632','2026-08-07',100,1200,12,'2026-08-08','其他','测试',1,0)"""
        )
        opening = float(self.db.setting("active_cash_balance"))
        into_active = self.service.reclassify_holding(holding_id, 1, "改为主动短线持仓")
        self.assertEqual(into_active, {
            "changed": True, "old": 0, "new": 1, "cash_delta": -1200.0,
        })
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening - 1200)
        summary = active_capital_summary(self.db)
        self.assertAlmostEqual(summary["cash_before_reservations"], opening - 1200)
        event = self.db.row(
            """SELECT * FROM active_cash_ledger WHERE event_kind='HOLDING_RECLASSIFICATION'
               ORDER BY id DESC LIMIT 1"""
        )
        self.assertAlmostEqual(event["amount"], -1200)
        self.assertIn("改为主动", event["notes"])

        out_of_active = self.service.reclassify_holding(holding_id, 0, "移出主动策略")
        self.assertEqual(out_of_active["cash_delta"], 1200)
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), opening)
        events = self.db.rows(
            """SELECT amount,balance_after FROM active_cash_ledger
               WHERE event_kind='HOLDING_RECLASSIFICATION' ORDER BY id"""
        )
        self.assertEqual([row["amount"] for row in events], [-1200, 1200])
        self.assertAlmostEqual(events[-1]["balance_after"], opening)
        snapshot = self.db.row("SELECT * FROM portfolio_equity_snapshots ORDER BY id DESC LIMIT 1")
        self.assertIn("资金分类", snapshot["source"])

    def test_unknown_exit_pnl_remains_none_and_is_counted_as_unknown_review(self):
        unknown_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital,
                   result_pnl,result_pnl_status
               ) VALUES('2026-08-08','020341','卖出',500,200,2.5,0,'历史记录',?,
                        '2026-08-08','已确认','测试','2026-08-08',1,NULL,'待核算')""",
            (self.now,),
        )
        known_zero_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,counts_toward_active_capital,
                   result_pnl,result_pnl_status
               ) VALUES('2026-08-09','020341','卖出',100,40,2.5,0,'已完成核算',?,
                        '2026-08-09','已确认','测试','2026-08-09',1,0,'已核算')""",
            (self.now,),
        )
        exits = realized_exit_trades(self.db)
        selected = [row for row in exits if row["id"] in {unknown_id, known_zero_id}]
        unknown = next(row for row in selected if row["id"] == unknown_id)
        self.assertIsNone(unknown["result_pnl"])
        metrics = strategy_review_metrics(selected, 8000)
        self.assertEqual(metrics["trade_count"], 1)
        self.assertEqual(metrics["unknown_trade_count"], 1)
        self.assertEqual(metrics["pnl_total"], 0)
        self.assertEqual(metrics["cumulative"], [0.0])

    def test_nav_sync_writes_active_portfolio_equity_snapshot(self):
        before = self.db.row("SELECT COUNT(*) AS n FROM portfolio_equity_snapshots")["n"]
        nav_day = local_now().date().isoformat()
        FundValuationService(self.db, FixedNavProvider(nav_day, 2.0)).sync_recent(14)
        after = self.db.row("SELECT COUNT(*) AS n FROM portfolio_equity_snapshots")["n"]
        self.assertEqual(after, before + 1)
        snapshot = self.db.row("SELECT * FROM portfolio_equity_snapshots ORDER BY id DESC LIMIT 1")
        gold = self.db.row(
            "SELECT units FROM holdings WHERE fund_code='020341' AND active=1"
        )
        expected_active_value = float(gold["units"]) * 2.0
        self.assertEqual(snapshot["source"], "正式净值更新")
        self.assertAlmostEqual(snapshot["holdings_value"], expected_active_value)
        self.assertAlmostEqual(snapshot["active_cash"], float(self.db.setting("active_cash_balance")))
        self.assertAlmostEqual(
            snapshot["total_equity"], snapshot["holdings_value"] + snapshot["active_cash"]
        )

    def test_daily_portfolio_equity_uses_exact_nav_for_every_exchange_day(self):
        self.db.execute("UPDATE holdings SET active=0,counts_toward_active_capital=0")
        self.db.execute("DELETE FROM active_cash_ledger")
        self.db.execute("DELETE FROM portfolio_equity_snapshots")
        self.db.execute("DELETE FROM ledger_postings")
        self.db.execute("DELETE FROM cost_lots")
        self.db.execute("DELETE FROM trades")
        self.db.save_settings({"active_cash_balance": "7000"})
        self.db.execute(
            """INSERT INTO active_cash_ledger(
                   event_key,event_kind,trade_id,amount,balance_after,created_at,notes
               ) VALUES('test-opening','OPENING_BALANCE',NULL,8000,8000,
                        '2026-08-07T09:00:00+08:00','测试组合日线起点')"""
        )
        trade_id = self.db.execute(
            """INSERT INTO trades(
                   trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                   confirm_date,status,source,nav_date,source_url,counts_toward_active_capital
               ) VALUES('2026-08-07','001632','买入',1000,100,10,0,'日线测试',
                        '2026-08-10T10:00:00+08:00','2026-08-10','已确认','测试',
                        '2026-08-07','https://example.test/trade',1)"""
        )
        self.db.execute(
            """INSERT INTO ledger_postings(
                   trade_id,posting_kind,critical_snapshot_json,metadata_json,posted_at,posting_seq
               ) VALUES(?,'买入','{}','{}','2026-08-10T10:00:00+08:00',1)""",
            (trade_id,),
        )
        self.db.execute(
            """INSERT INTO active_cash_ledger(
                   event_key,event_kind,trade_id,amount,balance_after,created_at,notes
               ) VALUES('test-buy','CONFIRMED_BUY',?,-1000,7000,
                        '2026-08-10T10:00:00+08:00','测试买入')""",
            (trade_id,),
        )
        self.db.execute(
            """INSERT INTO holdings(
                   fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,
                   active,counts_toward_active_capital
               ) VALUES('001632','2026-08-10',100,1000,9.5,'2026-08-12',
                        '主动','日线测试',1,1)"""
        )
        for nav_day, nav in (
            ("2026-08-07", 10.0), ("2026-08-10", 12.0),
            ("2026-08-11", 9.0), ("2026-08-12", 9.5),
        ):
            self.db.execute(
                """INSERT INTO fund_nav_history(
                       fund_code,nav_date,nav,source,source_url,fetched_at
                   ) VALUES('001632',?,?,'测试正式净值','https://example.test/nav',
                            '2026-08-12T18:00:00+08:00')
                   ON CONFLICT(fund_code,nav_date) DO UPDATE SET
                       nav=excluded.nav,source=excluded.source,source_url=excluded.source_url""",
                (nav_day, nav),
            )
        with patch("app.services.local_now", return_value=datetime(2026, 8, 12, 16, 0)):
            daily = daily_portfolio_equity(self.db)
            drawdown = portfolio_equity_drawdown(self.db)
        self.assertEqual((daily["candidate_days"], daily["complete_days"]), (4, 4))
        self.assertEqual(
            [row["total_equity"] for row in daily["series"]],
            [8000.0, 8200.0, 7900.0, 7950.0],
        )
        self.assertEqual(drawdown["basis"], "正式净值日线＋主动现金账本")
        self.assertEqual(drawdown["sample_count"], 4)
        self.assertAlmostEqual(drawdown["max_drawdown_pct"], 300 / 8200 * 100)
        self.assertEqual((drawdown["peak_at"], drawdown["trough_at"]), (
            "2026-08-10", "2026-08-11",
        ))

        self.db.execute(
            "DELETE FROM fund_nav_history WHERE fund_code='001632' AND nav_date='2026-08-10'"
        )
        with patch("app.services.local_now", return_value=datetime(2026, 8, 12, 16, 0)):
            incomplete = daily_portfolio_equity(self.db)
        self.assertEqual((incomplete["candidate_days"], incomplete["complete_days"]), (4, 3))
        self.assertEqual(incomplete["missing_days"], [
            {"date": "2026-08-10", "missing": ["001632"]},
        ])

    def test_snapshot_drawdown_deduplicates_repeated_same_day_captures(self):
        self.db.execute("DELETE FROM active_cash_ledger")
        self.db.execute("DELETE FROM portfolio_equity_snapshots")
        for captured_at, equity in (
            ("2026-08-10T10:00:00+08:00", 8000),
            ("2026-08-10T18:00:00+08:00", 8200),
            ("2026-08-11T18:00:00+08:00", 8000),
        ):
            self.db.execute(
                """INSERT INTO portfolio_equity_snapshots(
                       captured_at,holdings_value,active_cash,total_equity,
                       external_flow_total,source
                   ) VALUES(?,0,0,?,0,'测试快照')""",
                (captured_at, equity),
            )
        drawdown = portfolio_equity_drawdown(self.db)
        self.assertEqual(drawdown["basis"], "已记录组合快照（每日只取最后一次）")
        self.assertEqual(drawdown["sample_count"], 2)
        self.assertAlmostEqual(drawdown["max_drawdown_pct"], 200 / 8200 * 100)


class V30LedgerMigrationTests(unittest.TestCase):
    def test_confirmed_legacy_trade_without_lot_is_sealed_without_reposting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pre-v30.sqlite3"
            db = Database(path)
            now = local_now().isoformat(timespec="seconds")
            trade_id = db.execute(
                """INSERT INTO trades(
                       trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                       confirm_date,status,source,nav_date,counts_toward_active_capital
                   ) VALUES('2026-07-30','020741','买入',1000,856.6,1.1674,0,
                            '旧版补录并已包含在现有持仓',?,'','已确认','旧版','',0)""",
                (now,),
            )
            before = db.row(
                "SELECT id,units,cost FROM holdings WHERE fund_code='020741' AND active=1 ORDER BY id LIMIT 1"
            )
            self.assertIsNotNone(before)
            db.execute("DELETE FROM ledger_postings WHERE trade_id=?", (trade_id,))
            db.execute("DELETE FROM cost_lots WHERE trade_id=?", (trade_id,))
            db.save_settings({"schema_version": "29"})

            migrated = Database(path)
            posting = migrated.row(
                "SELECT * FROM ledger_postings WHERE trade_id=?", (trade_id,)
            )
            self.assertIsNotNone(posting)
            metadata = json.loads(posting["metadata_json"])
            self.assertTrue(metadata["legacy_sealed"])
            self.assertTrue(PortfolioLedgerService(migrated).is_posted(trade_id))
            result = PortfolioLedgerService(migrated).post_confirmed_trade(trade_id)
            self.assertFalse(result["posted"])
            after = migrated.row("SELECT id,units,cost FROM holdings WHERE id=?", (before["id"],))
            self.assertEqual(after, before)


class V23FoodPurchaseMigrationTests(unittest.TestCase):
    @unittest.skip("Public release removed the private pre-release correction migration.")
    def test_false_weekend_confirmation_returns_to_pending_without_touching_other_trade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pre-v23.sqlite3"
            db = Database(path)
            now = "2026-08-09T12:00:00+08:00"
            # Simulate the user's state immediately before v23: no other tactical
            # position, and no cash ledger has yet been initialised.
            db.execute("UPDATE holdings SET counts_toward_active_capital=0")
            db.execute("DELETE FROM active_cash_ledger")
            db.execute("DELETE FROM portfolio_equity_snapshots")
            db.execute("DELETE FROM ledger_postings")
            db.execute(
                """DELETE FROM settings WHERE key IN (
                       'active_cash_balance','active_cash_initialized_at','active_cash_limit_baseline'
                   )"""
            )
            db.execute(
                """INSERT INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                       purchase_fee_pct,status,notes,updated_at
                   ) VALUES('001632','天弘中证食品饮料ETF联接C','930653.CSI',
                            '中证食品饮料主题指数','C','食品饮料',0,'可买','',?)
                   ON CONFLICT(fund_code) DO NOTHING""",
                (now,),
            )
            target_trade = db.execute(
                """INSERT INTO trades(
                       trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                       confirm_date,status,source,nav_date,counts_toward_active_capital
                   ) VALUES('2026-08-07 14:00:00','001632','买入',2000,1082.37,1.8478,0,
                            '用户14点下单',?,'2026-08-08','已确认','旧版错误确认',
                            '2026-08-07',1)""",
                (now,),
            )
            target_holding = db.execute(
                """INSERT INTO holdings(
                       fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,
                       active,counts_toward_active_capital
                   ) VALUES('001632','2026-08-08',1082.37,2000,1.8478,'2026-08-07',
                            '主动','旧版错误持仓',1,1)"""
            )
            db.execute(
                """INSERT INTO cost_lots(
                       fund_code,trade_id,confirmed_date,pricing_date,units,remaining_units,
                       gross_amount,fee,net_invested,nav,source,created_at
                   ) VALUES('001632',?,'2026-08-08','2026-08-07',1082.37,1082.37,
                            2000,0,2000,1.8478,'旧版错误批次',?)""",
                (target_trade, now),
            )

            # An unrelated transaction with the same apparent confirmation day
            # must survive byte-for-byte in its accounting fields.
            db.execute(
                """INSERT INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                       purchase_fee_pct,status,notes,updated_at
                   ) VALUES('654321','无关测试基金','','','C','其他',0,'持有','',?)""",
                (now,),
            )
            other_trade = db.execute(
                """INSERT INTO trades(
                       trade_date,fund_code,action,amount,units,nav,fee,rationale,created_at,
                       confirm_date,status,source,nav_date,counts_toward_active_capital
                   ) VALUES('2026-08-07 14:00:00','654321','买入',111,10,11.1,0,
                            '无关交易',?,'2026-08-08','已确认','测试','2026-08-07',0)""",
                (now,),
            )
            other_holding = db.execute(
                """INSERT INTO holdings(
                       fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,
                       active,counts_toward_active_capital
                   ) VALUES('654321','2026-08-08',10,111,11.1,'2026-08-07',
                            '其他','无关持仓',1,0)"""
            )
            other_lot = db.execute(
                """INSERT INTO cost_lots(
                       fund_code,trade_id,confirmed_date,pricing_date,units,remaining_units,
                       gross_amount,fee,net_invested,nav,source,created_at
                   ) VALUES('654321',?,'2026-08-08','2026-08-07',10,10,111,0,111,11.1,
                            '无关批次',?)""",
                (other_trade, now),
            )
            trade_fields = (
                "trade_date", "fund_code", "action", "amount", "units", "nav",
                "confirm_date", "status", "nav_date", "counts_toward_active_capital",
            )
            other_before = db.row("SELECT * FROM trades WHERE id=?", (other_trade,))
            lot_before = db.row("SELECT * FROM cost_lots WHERE id=?", (other_lot,))
            holding_before = db.row("SELECT * FROM holdings WHERE id=?", (other_holding,))

            db.save_settings({"schema_version": "22", "active_capital_limit": "8000"})
            migrated = Database(path)

            target = migrated.row("SELECT * FROM trades WHERE id=?", (target_trade,))
            self.assertEqual(target["trade_date"], "2026-08-07 14:00:00")
            self.assertEqual(target["nav_date"], "2026-08-07")
            self.assertEqual(target["confirm_date"], "")
            self.assertEqual(target["status"], "待确认")
            self.assertNotEqual(target["confirm_date"], "2026-08-10")
            self.assertEqual(target["amount"], 2000)
            self.assertEqual(target["counts_toward_active_capital"], 1)
            self.assertIsNone(
                migrated.row("SELECT id FROM cost_lots WHERE trade_id=?", (target_trade,))
            )
            self.assertIsNone(
                migrated.row("SELECT trade_id FROM ledger_postings WHERE trade_id=?", (target_trade,))
            )
            self.assertIsNone(
                migrated.row("SELECT id FROM holdings WHERE id=? AND active=1", (target_holding,))
            )

            capital = active_capital_summary(migrated)
            self.assertAlmostEqual(capital["cash_before_reservations"], 8000)
            self.assertAlmostEqual(capital["reserved_pending_trades"], 2000)
            self.assertAlmostEqual(capital["cash_available"], 6000)

            other_after = migrated.row("SELECT * FROM trades WHERE id=?", (other_trade,))
            self.assertEqual(
                tuple(other_after[field] for field in trade_fields),
                tuple(other_before[field] for field in trade_fields),
            )
            lot_after = migrated.row("SELECT * FROM cost_lots WHERE id=?", (other_lot,))
            holding_after = migrated.row("SELECT * FROM holdings WHERE id=?", (other_holding,))
            for field in (
                "fund_code", "trade_id", "confirmed_date", "pricing_date", "units",
                "remaining_units", "gross_amount", "fee", "net_invested", "nav",
            ):
                self.assertEqual(lot_after[field], lot_before[field])
            for field in (
                "fund_code", "confirmed_date", "units", "cost", "latest_nav", "nav_date",
                "active", "counts_toward_active_capital",
            ):
                self.assertEqual(holding_after[field], holding_before[field])


if __name__ == "__main__":
    unittest.main()

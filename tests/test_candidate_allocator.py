from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.decision_drafts import DecisionDraftService
from app.portfolio import allocate_candidate_amounts


class CandidateAllocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "allocator.sqlite3")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM decision_drafts")
            conn.execute("DELETE FROM lot_consumptions")
            conn.execute("DELETE FROM cost_lots")
            conn.execute("DELETE FROM active_cash_ledger")
            conn.execute("DELETE FROM ledger_postings")
            conn.execute("UPDATE recurring_occurrences SET trade_id=NULL")
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM holdings")
        self.db.save_settings({
            "active_capital_limit": "8000",
            "single_position_limit": "2000",
            "correlation_bucket_limit": "3000",
            "max_active_themes": "8",
            "ranking_funded_cluster_max": "1",
            "ranking_data_confidence_min": "70",
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fund(self, code: str, index_code: str, bucket: str) -> None:
        self.db.execute(
            """INSERT INTO funds(
                   fund_code,fund_name,index_code,index_name,share_class,
                   correlation_bucket,purchase_fee_pct,status,notes,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fund_code) DO UPDATE SET
                   index_code=excluded.index_code,index_name=excluded.index_name,
                   correlation_bucket=excluded.correlation_bucket""",
            (code, f"测试{code}", index_code, index_code, "C", bucket, 0, "可买", "", "2026-08-11T10:00:00"),
        )

    @staticmethod
    def candidate(
        row_id: int, code: str, index_code: str, bucket: str, *,
        tilt: str = "非常推荐买", timing: str = "可以买",
        execution: str = "可执行", confidence: float = 90,
        freshness_ok: bool = True, target_pct: float = 10,
    ) -> dict:
        return {
            "id": row_id,
            "universe_id": "",
            "index_code": index_code,
            "sector_name": index_code,
            "fund_code": code,
            "correlation_bucket": bucket,
            "research_tilt": tilt,
            "timing_status": timing,
            "execution_status": execution,
            "data_confidence": confidence,
            "freshness_ok": freshness_ok,
            "allocation_pct": target_pct,
            "suggested_amount": 0,
        }

    def test_zero_amount_never_overwrites_seven_level_research_view(self) -> None:
        self.fund("100001", "931001.CSI", "AI硬件")
        source = self.candidate(
            1, "100001", "931001.CSI", "AI硬件",
            tilt="推荐买", timing="等待回调",
        )
        result = allocate_candidate_amounts(self.db, [source])[0]
        self.assertEqual(result["research_tilt"], "推荐买")
        self.assertEqual(result["suggested_amount"], 0)
        self.assertIn("等待回调", result["execution_reason"])

    def test_stale_and_low_confidence_keep_research_view_but_block_money(self) -> None:
        self.fund("100002", "931002.CSI", "AI软件")
        self.fund("100003", "931003.CSI", "算力")
        rows = [
            self.candidate(2, "100002", "931002.CSI", "AI软件", tilt="非常推荐买", freshness_ok=False),
            self.candidate(3, "100003", "931003.CSI", "算力", tilt="有点推荐买", confidence=69),
        ]
        stale, weak = allocate_candidate_amounts(self.db, rows)
        self.assertEqual((stale["research_tilt"], stale["suggested_amount"]), ("非常推荐买", 0))
        self.assertEqual(stale["execution_status"], "行情待更新")
        self.assertEqual((weak["research_tilt"], weak["suggested_amount"]), ("有点推荐买", 0))
        self.assertEqual(weak["execution_status"], "数据待补齐")

    def test_one_bucket_can_fund_only_one_candidate(self) -> None:
        rows = []
        for number in range(3):
            code = f"10001{number}"
            self.fund(code, f"93101{number}.CSI", "半导体产业链")
            rows.append(self.candidate(10 + number, code, f"93101{number}.CSI", "半导体产业链"))
        result = allocate_candidate_amounts(self.db, rows)
        self.assertEqual(sum(float(row["suggested_amount"]) > 0 for row in result), 1)
        self.assertEqual(result[0]["suggested_amount"], 800)
        self.assertTrue(all(row["research_tilt"] == "非常推荐买" for row in result))

    def test_existing_same_bucket_draft_blocks_a_second_one_row_action(self) -> None:
        self.fund("100020", "931020.CSI", "光通信")
        self.fund("100021", "931021.CSI", "光通信")
        self.db.execute(
            """INSERT INTO decision_drafts(
                   trade_date,source_kind,source_key,fund_code,fund_name,action,
                   planned_amount,status,created_at,updated_at,pricing_date,
                   settlement_status,counts_toward_active_capital
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2026-08-11", "用户手工", "manual:existing", "100020", "已有草稿", "买入",
                500, "草稿", "2026-08-11T10:00:00", "2026-08-11T10:00:00",
                "2026-08-11", "待联网补全", 1,
            ),
        )
        result = allocate_candidate_amounts(
            self.db, [self.candidate(21, "100021", "931021.CSI", "光通信")],
            release_existing_candidate_drafts=True,
        )[0]
        self.assertEqual(result["suggested_amount"], 0)
        self.assertIn("最多1项", result["execution_reason"])


class CandidateCloseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "close.sqlite3")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM decision_drafts")
            conn.execute("DELETE FROM lot_consumptions")
            conn.execute("DELETE FROM cost_lots")
            conn.execute("DELETE FROM active_cash_ledger")
            conn.execute("DELETE FROM ledger_postings")
            conn.execute("UPDATE recurring_occurrences SET trade_id=NULL")
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM holdings")
        self.db.save_settings({
            "active_capital_limit": "8000",
            "single_position_limit": "2000",
            "correlation_bucket_limit": "3000",
            "max_active_themes": "8",
            "ranking_funded_cluster_max": "1",
            "ranking_data_confidence_min": "70",
        })
        self.now = "2026-08-11T15:05:00"
        self.db.execute(
            """INSERT INTO funds(
                   fund_code,fund_name,index_code,index_name,share_class,
                   correlation_bucket,purchase_fee_pct,status,notes,updated_at
               ) VALUES('001632','食品饮料联接C','930653.CSI','食品饮料','C',
                        '食品饮料',0,'可买','',?)
               ON CONFLICT(fund_code) DO UPDATE SET updated_at=excluded.updated_at""",
            (self.now,),
        )
        self.db.execute(
            """INSERT INTO radar_runs(
                   task_id,data_cutoff,market_regime,universe_total,
                   analyzed_total,summary,raw_json,imported_at
               ) VALUES('v3-close','2026-08-11','测试',1,1,'','{}',?)""",
            (self.now,),
        )
        index = self.db.row(
            "SELECT universe_id FROM investable_indices WHERE official_index_code='930653' AND active=1 LIMIT 1"
        )
        self.candidate_id = self.db.execute(
            """INSERT INTO candidates(
                   task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,status,
                   score,target_allocation_pct,suggested_amount,correlation_bucket,thesis,risk,
                   allocation_pct,research_tilt,timing_status,execution_status,data_confidence,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "v3-close", 1, index["universe_id"], "930653.CSI", "食品饮料", "001632",
                "食品饮料联接C", "推荐买", 75, 10, 800, "食品饮料", "测试", "测试", 10,
                "推荐买", "可以买", "可执行", 90, self.now,
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def draft(self, amount: float) -> int:
        return self.db.execute(
            """INSERT INTO decision_drafts(
                   trade_date,source_kind,source_key,task_id,candidate_id,fund_code,fund_name,
                   action,planned_amount,status,created_at,updated_at,pricing_date,
                   settlement_status,counts_toward_active_capital
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2026-08-11", "AI板块建议", "candidate:v3-close:C930653", "v3-close",
                self.candidate_id, "001632", "食品饮料联接C", "买入", amount, "草稿",
                self.now, self.now, "2026-08-11", "待联网补全", 1,
            ),
        )

    @patch("app.decision_drafts.PlatformCatalogService.require_fund_execution", return_value={})
    def test_close_reuses_live_candidate_amount_gate(self, _catalog_check) -> None:
        draft_id = self.draft(900)
        service = DecisionDraftService(self.db)
        blocked = service.validate_surviving_allocation(
            "2026-08-11", as_of=datetime(2026, 8, 11, 15, 5),
        )
        self.assertEqual(blocked.decision, "不通过")
        self.assertTrue(any("超过同一实时门槛允许的800.00元" in reason for reason in blocked.reasons))

        self.db.execute("UPDATE decision_drafts SET planned_amount=800 WHERE id=?", (draft_id,))
        allowed = service.validate_surviving_allocation(
            "2026-08-11", as_of=datetime(2026, 8, 11, 15, 5),
        )
        self.assertEqual(allowed.decision, "通过")


if __name__ == "__main__":
    unittest.main()

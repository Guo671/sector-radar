import json
import http.client
import ssl
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.config import APP_VERSION, data_root, local_now
from app import __version__
from app.csi_catalog import CSI_DAILY_TARGETS, CSI_REPRESENTATIVES
from app.database import Database
from app.deepseek import ConnectionReport, DeepSeekClient, DeepSeekError, ModelResult
from app.decision_drafts import DecisionDraftError, DecisionDraftService
from app.dpi import enable_high_dpi_awareness, normalize_tk_scaling
from app.engine import estimate_sale, estimate_sale_by_lots, fee_rate_for_days, holding_days, validate_allocation
from app.fund_valuation import FundValuationService
from app.fund_discovery import (
    FundDiscoveryService, _canonical_index_name, parse_fund_profile, parse_redemption_rate,
    parse_redemption_tiers, parse_small_purchase_rate,
)
from app.investable import DraftSettlementService, FundMetadataService, InvestableMappingService
from app.ledger import PortfolioLedgerService
from app.market_data import (
    CSIOfficialKlineProvider, EastmoneyKlineProvider, MarketBar, MarketDataError, MarketDataPipeline,
    MarketTransportError, ProxyMapping, ReviewedFallbackKlineProvider,
    SohuKlineProvider, TEST20_IDS, TencentKlineProvider,
    compute_metric, windows_curl_path,
)
from app.orchestrator import ResearchOrchestrator
from app.platform_catalog import PlatformCatalogService
from app.portfolio import active_capital_summary
from app.protocol import END_MARKER, START_MARKER, build_task_package, parse_result
from app.recurring import EastmoneyFundNavProvider, RecurringInvestmentError, RecurringInvestmentService, calculate_subscription, parse_eastmoney_nav_payload
from app.sector_resolver import ConstituentBreadthProvider, SectorMappingResolver, SectorResolveError
from app.services import (
    action_capital_context, daily_workflow_status, holding_recommendations_for_run, import_radar_payload,
    realized_exit_trades, remove_stale_capital_claim, save_action_advice, strategy_review_metrics,
)
from app.trading_calendar import ChinaTradingCalendar
from app.universe import UNIVERSE_VERSION, core_universe


def ensure_strict_scan_items(db, count=10):
    """Give batch-oriented tests enough executable CSI rows without fixed production counts."""
    catalog = PlatformCatalogService(db)
    suffix = 0
    while len(catalog.radar_universe()) < count:
        if suffix >= count * 3 + 10:
            raise AssertionError("测试严格目录未能收敛，禁止无限补位")
        code = f"938{900 + suffix:03d}"
        index_id = catalog.add_candidate(
            industry_group="测试补位", platform_code=f"{code}.CSI",
            platform_name=f"测试补位{suffix}", official_code=code,
            official_name=f"测试补位正式指数{suffix}",
            official_source_url=f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
            evidence_kind="中证官方事实表", security_id=f"2.{code}",
            proxy_code=code, proxy_name=f"测试补位正式指数{suffix}",
            mapping_confidence=0.99,
        )
        stamp = local_now().isoformat(timespec="seconds")
        db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,active,passive_verified,relation_kind,
                   verified_at,platform_checked_at,radar_eligible,verification_algorithm)
               VALUES(?,?,?,'被动ETF联接','C','公开渠道可申购',1,1,'同一指数',?,?,0,?)""",
            (
                index_id, f"88{suffix:04d}", f"测试补位ETF联接C{suffix}", stamp, stamp,
                STRICT_FIELD_SYNC_ALGORITHM_VERSION,
            ),
        )
        db.execute(
            "UPDATE investable_indices SET allocation_cluster=? WHERE id=?",
            (f"TEST_CLUSTER_{suffix}", index_id),
        )
        catalog.refresh_admission(index_id)
        suffix += 1
    return catalog.radar_universe()


class EngineTests(unittest.TestCase):
    def test_windows_safe_fixed_utc8_timezone(self):
        self.assertEqual(local_now().utcoffset(), timedelta(hours=8))
        self.assertEqual(APP_VERSION, "4.4.0-alpha.41")
        self.assertEqual(__version__, APP_VERSION)

    def test_packaged_data_root_stays_beside_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "板块雷达.exe"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)):
                self.assertEqual(data_root(), Path(folder) / "data")

    def test_dpi_bootstrap_is_safe_on_current_platform(self):
        result = enable_high_dpi_awareness()
        self.assertIsInstance(result, str)
        self.assertTrue(result)
        if not sys.platform.startswith("win"):
            self.assertEqual(result, "not-windows")

    def test_tk_scaling_is_capped_without_disabling_dpi_awareness(self):
        class FakeTk:
            def __init__(self):
                self.value = 3.2

            def call(self, *args):
                if len(args) == 2:
                    return self.value
                self.value = float(args[2])

        root = SimpleNamespace(tk=FakeTk())
        reported, applied = normalize_tk_scaling(root)
        self.assertEqual(reported, 3.2)
        self.assertEqual(applied, 2.25)
        self.assertEqual(root.tk.value, 2.25)

    def test_portable_manifest_declares_per_monitor_v2(self):
        manifest = Path(__file__).resolve().parents[1] / "sector_radar.manifest"
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("PerMonitorV2,PerMonitor", text)
        self.assertIn("true/pm", text)

    def test_fee_tier_boundaries(self):
        tiers = [{"min_days": 0, "max_days": 6, "redeem_fee_pct": 1.5}, {"min_days": 7, "max_days": None, "redeem_fee_pct": 0}]
        self.assertEqual(fee_rate_for_days(tiers, 6), 1.5)
        self.assertEqual(fee_rate_for_days(tiers, 7), 0)

    def test_sale_math_for_three_percent_gain(self):
        tiers = [{"min_days": 0, "max_days": 6, "redeem_fee_pct": 1.5}]
        result = estimate_sale(units=103, cost=100, latest_nav=1, confirmed_date="2026-08-01", sell_date="2026-08-03", tiers=tiers)
        self.assertAlmostEqual(result.net_pnl, 1.455, places=3)
        self.assertAlmostEqual(result.net_return_pct, 1.455, places=3)

    def test_holding_days_are_inclusive(self):
        self.assertEqual(holding_days("2026-08-01", "2026-08-07"), 7)

    def test_lot_aware_redemption_charges_only_the_young_batch(self):
        tiers = [{"min_days": 0, "max_days": 6, "redeem_fee_pct": 1.5}, {"min_days": 7, "max_days": None, "redeem_fee_pct": 0}]
        result = estimate_sale_by_lots(
            units=200, cost=200, latest_nav=1, confirmed_date="2026-07-01", tiers=tiers,
            lots=[{"confirmed_date": "2026-08-04", "units": 10, "remaining_units": 10, "gross_amount": 10}],
            sell_date="2026-08-05",
        )
        self.assertAlmostEqual(result.redeem_fee, 0.15, places=6)
        self.assertAlmostEqual(result.redeem_fee_pct, 0.075, places=6)

    def test_allocation_rejects_correlation_overload(self):
        settings = {"active_capital_limit": "8000", "single_position_limit": "2000", "correlation_bucket_limit": "3000", "max_active_themes": "4"}
        result = validate_allocation([
            {"name": "CPO", "amount": 2000, "correlation_bucket": "AI算力"},
            {"name": "通信", "amount": 2000, "correlation_bucket": "AI算力"},
        ], [], settings)
        self.assertEqual(result.decision, "不通过")

    def test_stale_ai_capital_number_is_removed_instead_of_shown_beside_live_amount(self):
        cleaned = remove_stale_capital_claim(
            "可用资金仅剩约1120元，且当前市场热点过度拥挤，等待回调。"
        )
        self.assertNotIn("1120", cleaned)
        self.assertEqual(cleaned, "当前市场热点过度拥挤，等待回调。")

    def test_legacy_ai_action_without_capital_snapshot_is_locked(self):
        context = action_capital_context(
            {"action_json": "{}"},
            {"active_limit": 8000, "active_value": 2000, "active_available": 6000},
        )
        self.assertTrue(context["stale"])
        self.assertEqual(context["current"]["available"], 6000)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_historical_seed_is_preserved_beside_leaf_only_csi_catalog(self):
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM universe WHERE index_code LIKE 'U%'")["n"], 186)
        catalog = PlatformCatalogService(self.db)
        live = catalog.radar_universe()
        # A new database contains historical/manual seed mappings, but none may
        # become executable until it has passed the current complete-profile audit.
        self.assertEqual(live, [])
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) n FROM investable_indices WHERE active=1 AND radar_enabled=1"
            )["n"],
            0,
        )
        self.assertTrue(all(item.official_tracked for item in CSI_REPRESENTATIVES))
        holding = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='020741' AND active=1")
        self.assertAlmostEqual(holding["units"], 5757.83, places=2)
        self.assertAlmostEqual(holding["cost"], 6721.69, places=2)
        trade = self.db.row("SELECT * FROM trades WHERE fund_code='020741' AND action='买入'")
        self.assertEqual(trade["trade_date"], "2026-07-30 23:00:00")
        self.assertAlmostEqual(trade["units"], 856.60, places=2)

    def test_simulation_runs_all_leaf_csi_industries_without_api(self):
        orchestrator = ResearchOrchestrator(self.db)
        expected = ensure_strict_scan_items(self.db, 3)
        task_id = orchestrator.create_job("无API模拟")
        result = orchestrator.run(task_id)
        self.assertEqual(result.status, "已完成（模拟）")
        self.assertEqual(result.completed_items, len(expected))
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM sector_metrics WHERE task_id=?", (task_id,))["n"],
            len(expected),
        )
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0)

    def test_live_scan_stops_before_api_when_metrics_missing(self):
        class MissingMarketPipeline:
            def populate_metrics(self, *_args, **_kwargs):
                raise MarketDataError("可信代理映射不足：测试缺失")

        ensure_strict_scan_items(self.db, 1)
        orchestrator = ResearchOrchestrator(self.db, market_pipeline=MissingMarketPipeline())
        task_id = orchestrator.create_job("完整扫描")
        result = orchestrator.run(task_id)
        self.assertEqual(result.status, "失败")
        self.assertIn("可信代理映射不足", result.message)
        self.assertEqual(result.estimated_cost_cny, 0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0)

    def test_current_schema_keeps_history_and_only_activates_strict_c_targets(self):
        self.assertGreaterEqual(int(self.db.setting("schema_version")), 24)
        columns = {row["name"] for row in self.db.rows("PRAGMA table_info(decision_drafts)")}
        self.assertTrue({"source_kind", "source_key", "status", "trade_id", "posted_at", "pricing_date", "settlement_nav"}.issubset(columns))
        self.assertTrue(self.db.row("SELECT name FROM sqlite_master WHERE type='table' AND name='csi_index_products'"))
        self.assertTrue(self.db.row("SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_postings'"))
        self.assertTrue(self.db.row("SELECT name FROM sqlite_master WHERE type='table' AND name='active_cash_ledger'"))
        self.assertTrue(self.db.row("SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio_equity_snapshots'"))
        lot_columns = {row["name"] for row in self.db.rows("PRAGMA table_info(cost_lots)")}
        self.assertIn("pricing_date", lot_columns)
        identity = self.db.row("SELECT * FROM investable_indices WHERE universe_id='U154'")
        self.assertEqual((identity["official_index_code"], identity["ant_index_code"]), ("399303", "860003.CNS"))
        funds = self.db.rows(
            """SELECT f.* FROM investable_funds f JOIN investable_indices i
                 ON i.id=f.investable_index_id WHERE i.universe_id='U154'"""
        )
        self.assertEqual(len(funds), 6)
        self.assertEqual({row["strategy_kind"] for row in funds}, {"被动ETF联接", "指数增强"})
        food = self.db.row("SELECT * FROM investable_indices WHERE universe_id='U025'")
        self.assertEqual(food["official_index_code"], "930653")
        food_funds = self.db.rows(
            """SELECT f.* FROM investable_funds f JOIN investable_indices i
                 ON i.id=f.investable_index_id WHERE i.universe_id='U025' ORDER BY f.quality_rank"""
        )
        self.assertEqual(food_funds[0]["fund_code"], "001632")
        self.assertEqual(food_funds[0]["ant_buyable_status"], "待平台复核")
        old_parent = self.db.row("SELECT * FROM investable_indices WHERE universe_id='C000986'")
        self.assertEqual((old_parent["active"], old_parent["radar_enabled"]), (0, 0))
        semiconductor = self.db.row(
            "SELECT * FROM investable_indices WHERE official_index_code='H30184' AND active=1"
        )
        self.assertIsNone(semiconductor)
        children = self.db.rows(
            """SELECT official_index_code,parent_official_code,hierarchy_level,allocation_cluster,active
                 FROM investable_indices
                WHERE official_index_code IN ('931743','950162')
                ORDER BY official_index_code"""
        )
        self.assertEqual([row["official_index_code"] for row in children], ["931743", "950162"])
        self.assertTrue(all(row["parent_official_code"] == "H30184" for row in children))
        self.assertTrue(all(row["hierarchy_level"] == 4 for row in children))
        self.assertTrue(all(row["allocation_cluster"] == "H30184" for row in children))
        live_ids = {row["index_code"] for row in PlatformCatalogService(self.db).radar_universe()}
        self.assertNotIn("C950162", live_ids)
        self.assertTrue(all(
            row["allocation_cluster"]
            for row in PlatformCatalogService(self.db).radar_universe()
        ))
        active_codes = {item.code for item in CSI_REPRESENTATIVES}
        self.assertFalse(any(item.parent_code in active_codes for item in CSI_REPRESENTATIVES))
        mappings = self.db.rows(
            """SELECT p.security_id FROM proxy_mappings p JOIN investable_indices i
                   ON i.universe_id=p.universe_id
                  WHERE i.active=1 AND i.radar_enabled=1 AND i.catalog_role='日扫代表'"""
        )
        live_count = len(PlatformCatalogService(self.db).radar_universe())
        self.assertEqual(len(mappings), live_count)
        self.assertTrue(all(row["security_id"].startswith("CSI.") for row in mappings))
        self.assertEqual(
            self.db.row(
                """SELECT COUNT(*) n FROM investable_indices
                    WHERE active=1 AND catalog_role='日扫代表' AND radar_enabled=1
                      AND official_tracked_product=1"""
            )["n"],
            live_count,
        )
        # 旧版“已确认无C”使用了错误的简称/全称字符串比较。升级时必须先保留
        # 为待复核、关闭扫描，不能在联网复核前直接归档；新算法完成复核后才会
        # 将真正无严格C的目标移出当前目录。
        pending_count = self.db.row(
            """SELECT COUNT(*) n FROM investable_indices
                WHERE active=1 AND catalog_role='日扫代表' AND radar_enabled=0"""
        )["n"]
        current_count = self.db.row(
            """SELECT COUNT(*) n FROM investable_indices
                WHERE active=1 AND catalog_role='日扫代表'"""
        )["n"]
        self.assertEqual(pending_count, current_count - live_count)
        self.assertGreater(pending_count, 0)
        self.assertIsNone(
            self.db.row(
                "SELECT id FROM investable_indices WHERE official_index_code='932580' AND active=1"
            )
        )

    def test_active_capital_excludes_bond_and_nasdaq_but_includes_gold(self):
        summary = active_capital_summary(self.db)
        gold = self.db.row("SELECT units,latest_nav,cost FROM holdings WHERE fund_code='020341'")
        self.assertAlmostEqual(summary["used"], gold["units"] * gold["latest_nav"])
        self.assertAlmostEqual(summary["committed_cost"], gold["cost"])
        self.assertAlmostEqual(summary["cash_before_reservations"], 8000 - gold["cost"])
        self.assertAlmostEqual(summary["available"], 8000 - gold["cost"])
        flags = {row["fund_code"]: row["counts_toward_active_capital"] for row in self.db.rows("SELECT * FROM holdings")}
        self.assertEqual((flags["020341"], flags["020741"], flags["014978"]), (1, 0, 0))

    def test_user_selected_pro_token_limit_survives_reopen(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.sqlite3"
        db = Database(path)
        db.save_settings({"api_pro_max_tokens": "80000"})
        reopened = Database(path)
        self.assertEqual(reopened.setting("api_pro_max_tokens"), "80000")
        reopened.save_settings({"api_pro_max_tokens": "6000"})
        self.assertEqual(Database(path).setting("api_pro_max_tokens"), "6000")

        columns = {row["name"] for row in self.db.rows("PRAGMA table_info(model_calls)")}
        self.assertIn("request_context_json", columns)
        self.db.save_settings({
            "api_test_batch_size": "5", "api_full_batch_size": "12",
            "api_test_max_output_tokens": "12000", "api_full_flash_max_output_tokens": "24000",
        })
        Database(self.db.path)
        self.assertEqual(self.db.setting("api_test_batch_size"), "5")
        self.assertEqual(self.db.setting("api_full_batch_size"), "12")
        self.assertEqual(self.db.setting("api_test_max_output_tokens"), "12000")
        self.assertEqual(self.db.setting("api_full_flash_max_output_tokens"), "24000")
        self.db.save_settings({"api_test_batch_size": "2"})
        Database(self.db.path)
        self.assertEqual(self.db.setting("api_test_batch_size"), "2")

        tables = {row["name"] for row in self.db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("fund_nav_history", tables)
        self.assertIn("fund_valuation_snapshots", tables)
        self.assertIn("fund_discovery_runs", tables)
        self.assertIn("fund_discovery_candidates", tables)
        discovery_columns = {row["name"] for row in self.db.rows("PRAGMA table_info(fund_discovery_candidates)")}
        self.assertTrue({"redemption_fee_pct", "horizon_cost_pct", "recommendation"} <= discovery_columns)

    def test_official_v4_capabilities_are_distinct_from_application_output_cap(self):
        self.assertEqual(self.db.setting("deepseek_v4_context_tokens"), "1000000")
        self.assertEqual(self.db.setting("deepseek_v4_max_output_tokens"), "384000")
        self.assertEqual(self.db.setting("api_pro_hard_output_cap_tokens"), "12000")


class FakeFundNavProvider:
    name = "test_nav"

    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.fail = fail
        self.calls = []

    def fetch(self, code, start_date, end_date):
        self.calls.append((code, start_date, end_date))
        if self.fail:
            raise RuntimeError("模拟净值源失败")
        return self.values.get(code, self.values), "https://example.test/fund-nav"


class FundDiscoveryTests(unittest.TestCase):
    def test_stop_before_first_query_never_starts_fund_network_search(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "discovery-stop.sqlite3")
            target = db.row("SELECT index_code FROM universe ORDER BY index_code LIMIT 1")
            service = FundDiscoveryService(db)
            calls = []
            service._search = lambda query: calls.append(query) or []
            with self.assertRaisesRegex(Exception, "用户已停止"):
                service.discover(target["index_code"], stop_requested=lambda: True)
            self.assertEqual(calls, [])

    def test_official_short_name_matches_full_fund_profile_index_name(self):
        self.assertEqual(
            _canonical_index_name("半导体材料设备"),
            _canonical_index_name("中证半导体材料设备主题指数"),
        )

    def test_profile_parser_keeps_zero_fees_distinct_from_missing_fees(self):
        complete = parse_fund_profile("""<table><tr><td>基金全称</td><td>测试指数基金C</td></tr>
            <tr><td>跟踪标的</td><td>测试指数</td></tr><tr><td>管理费率</td><td>0.50%</td></tr>
            <tr><td>托管费率</td><td>0.10%</td></tr><tr><td>销售服务费率</td><td>0.00%</td></tr>
            <tr><td>最高申购费率</td><td>0.00%</td></tr></table>""")
        missing = parse_fund_profile("<table><tr><td>基金全称</td><td>资料不全基金</td></tr></table>")
        self.assertEqual(complete["sales_fee_pct"], 0.0)
        self.assertEqual(complete["purchase_fee_pct"], 0.0)
        self.assertIsNone(missing["sales_fee_pct"])
        self.assertIsNone(missing["purchase_fee_pct"])

    def test_missing_cost_and_size_data_never_receive_zero_cost_bonus(self):
        complete = {"match_kind":"同一指数","tracking_error_pct":None,"annual_cost_pct":0.6,
                    "purchase_fee_pct":0.0,"asset_size_billion":10.0,"inception_date":"2020-01-01",
                    "buyable_status":"公开渠道可申购","tracking_days":0}
        missing = dict(complete, annual_cost_pct=None, purchase_fee_pct=None, asset_size_billion=None)
        FundDiscoveryService._score(complete, 30)
        FundDiscoveryService._score(missing, 30)
        self.assertGreater(complete["score"], missing["score"])
        self.assertIn("数据不完整", missing["reason"])
        self.assertIn("跟踪质量0.00/25", complete["reason"])
        self.assertIn("缺失指标按0分", complete["reason"])

    def test_redemption_fee_parser_uses_selected_holding_period(self):
        page = """<table>
            <tr><td>持有期限小于7日</td><td>1.50%</td></tr>
            <tr><td>持有期限大于等于7日，小于30日</td><td>0.10%</td></tr>
            <tr><td>持有期限大于等于30日</td><td>0.00%</td></tr>
        </table>"""
        self.assertEqual(parse_redemption_rate(page, 5), 1.5)
        self.assertEqual(parse_redemption_rate(page, 20), 0.1)
        self.assertEqual(parse_redemption_rate(page, 30), 0.0)

    def test_full_redemption_ladder_converts_year_boundaries_to_days(self):
        page = """<table><tr><th>适用期限</th><th>赎回费率</th></tr>
            <tr><td>小于7天</td><td>1.50%</td></tr>
            <tr><td>大于等于7天，小于1年</td><td>0.50%</td></tr>
            <tr><td>大于等于1年，小于2年</td><td>0.25%</td></tr>
            <tr><td>大于等于2年</td><td>0.00%</td></tr></table>"""
        self.assertEqual(parse_redemption_tiers(page), [(0,6,1.5),(7,364,.5),(365,729,.25),(730,None,0.0)])

    def test_purchase_fee_parser_uses_purchase_not_initial_subscription_table(self):
        page = """<table>
            <tr><th>适用金额</th><th>费率</th></tr><tr><td>小于100万元</td><td>1.00%</td></tr>
            <tr><th>适用金额</th><th>原费率|优惠费率</th></tr><tr><td>小于100万元</td><td>1.20% | 0.12%</td></tr>
            <tr><th>适用期限</th><th>赎回费率</th></tr><tr><td>小于7天</td><td>1.50%</td></tr>
        </table>"""
        self.assertEqual(parse_small_purchase_rate(page), 1.2)

    def test_same_index_is_always_ranked_before_similar_index(self):
        exact = {"match_kind": "同一指数", "score": 40, "fund_code": "000001"}
        similar = {"match_kind": "相似可投指数", "score": 99, "fund_code": "000002"}
        self.assertEqual(sorted([similar, exact], key=FundDiscoveryService._rank_key)[0], exact)


class StrategyReviewTests(unittest.TestCase):
    def test_summary_metrics_do_not_mislabel_realized_sequence_as_portfolio_drawdown(self):
        trades = [
            {"amount": 1000, "result_pnl": 100},
            {"amount": 800, "result_pnl": -200},
            {"amount": 600, "result_pnl": 50},
        ]
        result = strategy_review_metrics(trades, 8000)
        self.assertEqual(result["trade_count"], 3)
        self.assertEqual(result["amount_total"], 2400)
        self.assertEqual(result["pnl_total"], -50)
        self.assertAlmostEqual(result["win_rate_pct"], 200 / 3, places=6)
        self.assertAlmostEqual(result["profit_factor"], .75, places=6)
        self.assertIsNone(result["max_drawdown_pct"])
        self.assertAlmostEqual(
            result["realized_sequence_drawdown_pct"], 200 / 8100 * 100, places=6
        )

    def test_open_buy_with_zero_placeholder_pnl_is_not_a_realized_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "review.sqlite3")
            now = local_now().isoformat(timespec="seconds")
            db.execute(
                """INSERT INTO trades(
                       trade_date,fund_code,action,amount,units,nav,fee,rationale,result_pnl,
                       created_at,confirm_date,status,source,nav_date,counts_toward_active_capital)
                   VALUES('2026-08-08','020341','买入',2000,1000,2,0,'测试',0,?,
                          '2026-08-08','已确认','测试','2026-08-08',1)""",
                (now,),
            )
            rows = realized_exit_trades(db)
            self.assertTrue(all(row["action"] in {"卖出","清仓","转换","转换转出"} for row in rows))
            self.assertFalse(any(row["action"] == "买入" for row in rows))


class FundValuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "valuation.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_updates_official_navs_and_calculates_daily_pnl(self):
        provider = FakeFundNavProvider({
            "020341": {"2026-08-03": 2.00, "2026-08-04": 2.02, "2026-08-05": 2.01},
            "020741": {"2026-08-03": 1.16, "2026-08-04": 1.17, "2026-08-05": 1.18},
            "014978": {"2026-08-03": 7.80, "2026-08-04": 7.90, "2026-08-05": 8.00},
        })
        # Keep the fixed August fixture inside the requested history window as
        # the wall clock advances beyond the original release week.
        result = FundValuationService(self.db, provider).sync_recent(30)
        self.assertEqual(result["funds_updated"], 3)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM fund_nav_history")["n"], 9)
        holding = self.db.row("SELECT latest_nav,nav_date FROM holdings WHERE fund_code='020341'")
        self.assertEqual(holding["nav_date"], "2026-08-05")
        self.assertAlmostEqual(holding["latest_nav"], 2.01)
        row = next(item for item in result["daily_rows"] if item["fund_code"] == "020341" and item["nav_date"] == "2026-08-05")
        self.assertAlmostEqual(row["daily_pnl_current_units"], -14.5131, places=4)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM fund_valuation_snapshots")["n"], 1)

    def test_partial_failure_preserves_other_funds_and_reports_dates(self):
        class PartialProvider(FakeFundNavProvider):
            def fetch(self, code, start_date, end_date):
                if code == "020741":
                    raise RuntimeError("债券净值源暂不可用")
                return {"2026-08-05": 2.0}, "https://example.test/fund-nav"

        result = FundValuationService(self.db, PartialProvider()).sync_recent(14)
        self.assertEqual(result["funds_updated"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(result["summary"]["captured_at"])
        self.assertIn("020741", self.db.setting("fund_nav_last_sync_error"))

    def test_daily_dca_uses_only_units_and_cost_accumulated_by_each_nav_date(self):
        self.db.execute("DELETE FROM holdings WHERE fund_code='014978'")
        self.db.execute(
            "INSERT INTO holdings(fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,active) "
            "VALUES('014978','2026-07-16',2.51,20,8.10,'2026-07-17','定投',1)"
        )
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO cost_lots(fund_code,confirmed_date,units,remaining_units,gross_amount,fee,net_invested,nav,source,created_at) "
            "VALUES('014978','2026-07-16',1.26,1.26,10,0,10,7.9548,'test',?)", (now,)
        )
        self.db.execute(
            "INSERT INTO cost_lots(fund_code,confirmed_date,units,remaining_units,gross_amount,fee,net_invested,nav,source,created_at) "
            "VALUES('014978','2026-07-17',1.25,1.25,10,0,10,8.0,'test',?)", (now,)
        )
        self.db.execute(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,source,source_url,fetched_at) "
            "VALUES('014978','2026-07-16',7.9548,'test','',?),('014978','2026-07-17',8.0,'test','',?)", (now, now)
        )

        rows = [row for row in FundValuationService(self.db).daily_rows(90) if row["fund_code"] == "014978"]
        first = next(row for row in rows if row["nav_date"] == "2026-07-16")
        second = next(row for row in rows if row["nav_date"] == "2026-07-17")
        self.assertAlmostEqual(first["market_value_current_units"], 1.26 * 7.9548)
        self.assertAlmostEqual(first["pnl_vs_current_cost"], 1.26 * 7.9548 - 10)
        self.assertAlmostEqual(second["market_value_current_units"], 2.51 * 8.0)
        self.assertAlmostEqual(second["daily_pnl_current_units"], 1.26 * (8.0 - 7.9548))
        self.assertEqual(second["valuation_basis"], "逐笔定投累计")

    def test_holding_period_sync_starts_each_fund_at_its_open_segment(self):
        provider = FakeFundNavProvider({})
        FundValuationService(self.db, provider).sync_holding_period()
        starts = {code: start for code, start, _end in provider.calls}
        self.assertEqual(starts["014978"], "2026-07-16")
        self.assertEqual(starts["020741"], "2026-07-31")

    def test_stopped_valuation_starts_no_request_and_does_not_claim_completion(self):
        provider = FakeFundNavProvider({})
        before = self.db.row("SELECT COUNT(*) n FROM fund_valuation_snapshots")["n"]
        result = FundValuationService(self.db, provider).sync_holding_period(
            stop_requested=lambda: True,
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM fund_valuation_snapshots")["n"], before)
        self.assertEqual(self.db.setting("fund_nav_last_sync_at", ""), "")
        self.assertEqual(self.db.setting("fund_nav_last_sync_attempt_status"), "已停止")


class PortfolioLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "ledger.sqlite3")
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO funds(fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                   purchase_fee_pct,status,notes,updated_at) VALUES('001632','天弘中证食品饮料ETF联接C',
                   '930653.CSI','中证食品饮料指数','C','食品饮料',0,'可买','',?)
               ON CONFLICT(fund_code) DO NOTHING""", (now,),
        )
        self.db.execute(
            """UPDATE investable_funds SET relation_kind='同一指数',share_class='C',
                      strategy_kind='被动ETF联接',passive_verified=1,
                      public_buyable_status='已核对可申购',platform_checked_at=?,verified_at=?
                 WHERE fund_code='001632'""",
            (now, now),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmed_buy_posts_once_and_uses_active_capital_flag(self):
        now = local_now().isoformat(timespec="seconds")
        trade_id = self.db.execute(
            """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,ai_advice,
                   created_at,confirm_date,status,source,nav_date,counts_toward_active_capital)
               VALUES('2026-08-07','001632','买入',2000,1082.37,1.8478,0,'测试','',?,
                      '2026-08-08','已确认','测试','2026-08-07',1)""", (now,),
        )
        service = PortfolioLedgerService(self.db)
        first = service.post_confirmed_trade(trade_id)
        second = service.post_confirmed_trade(trade_id)
        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        holding = self.db.row("SELECT * FROM holdings WHERE fund_code='001632' AND active=1")
        self.assertAlmostEqual(holding["cost"], 2000)
        self.assertAlmostEqual(holding["units"], 1082.37)
        self.assertEqual(holding["counts_toward_active_capital"], 1)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM cost_lots WHERE trade_id=?", (trade_id,))["n"], 1)


class RecurringInvestmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_seeds_current_daily_plan_without_replaying_old_history(self):
        plan = self.db.row("SELECT * FROM recurring_plans WHERE fund_code='014978'")
        self.assertEqual(plan["amount"], 10)
        self.assertEqual(plan["start_date"], "2026-08-05")
        self.assertEqual(plan["weekdays"], "0,1,2,3,4")
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM recurring_occurrences")["n"], 0)

    def test_due_generation_is_weekday_only_and_idempotent(self):
        service = RecurringInvestmentService(self.db, FakeFundNavProvider())
        self.assertEqual(service.generate_due(date(2026, 8, 10)), 4)
        self.assertEqual(service.generate_due(date(2026, 8, 10)), 0)
        dates = [r["scheduled_date"] for r in self.db.rows("SELECT scheduled_date FROM recurring_occurrences ORDER BY scheduled_date")]
        self.assertEqual(dates, ["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"])

    def test_owner_policy_skips_deduction_review_and_auto_posts_when_due(self):
        self.db.save_settings({"recurring_assume_all_deducted": "1"})
        service = RecurringInvestmentService(
            self.db, FakeFundNavProvider({"2026-08-05": 8.0})
        )
        service.generate_due(date(2026, 8, 5))
        generated = self.db.row("SELECT * FROM recurring_occurrences")
        self.assertEqual(generated["status"], "待净值")
        self.assertEqual(int(generated["deduction_confirmed"]), 1)
        result = service.sync_pending_navs()
        posted = self.db.row("SELECT * FROM recurring_occurrences")
        self.assertEqual(result["auto_posted"], 1)
        self.assertEqual(posted["status"], "已入账")
        self.assertIsNotNone(posted["trade_id"])

    def test_saved_formal_nav_is_reused_when_network_is_temporarily_unavailable(self):
        class OfflineProvider(FakeFundNavProvider):
            def fetch(self, *_args, **_kwargs):
                raise RecurringInvestmentError("模拟断网")

        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO fund_nav_history(fund_code,nav_date,nav,source,source_url,fetched_at)
                 VALUES('014978','2026-08-05',8.0,'saved_public_nav','https://example.com/nav',?)""",
            (now,),
        )
        service = RecurringInvestmentService(self.db, OfflineProvider())
        service.generate_due(date(2026, 8, 5))
        result = service.sync_pending_navs()
        occurrence = self.db.row("SELECT * FROM recurring_occurrences")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["ready"], 1)
        self.assertEqual(occurrence["status"], "可确认入账")
        self.assertEqual(occurrence["source"], "saved_public_nav")

    def test_public_nav_parser_does_not_execute_javascript(self):
        raw = b'''var apidata={content:"<table><tr><td>2026-08-05</td><td class='tor bold'>8.1234</td><td>8.1234</td></tr></table>",records:1};'''
        self.assertEqual(parse_eastmoney_nav_payload(raw, "014978"), {"2026-08-05": 8.1234})

    def test_current_nav_json_parser_and_request_fields(self):
        raw = json.dumps({
            "Data": {"LSJZList": [
                {"FSRQ": "2026-08-05", "DWJZ": "8.1234"},
                {"FSRQ": "2026-08-04", "DWJZ": "8.0000"},
            ]},
            "ErrCode": 0,
        }).encode("utf-8")

        def requester(request, _timeout):
            parsed = urllib.parse.urlparse(request.full_url)
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(parsed.netloc, "api.fund.eastmoney.com")
            self.assertEqual(query["fundCode"], ["014978"])
            self.assertEqual(query["startDate"], ["2026-08-04"])
            self.assertEqual(query["endDate"], ["2026-08-05"])
            return raw

        values, _url = EastmoneyFundNavProvider(requester=requester).fetch(
            "014978", "2026-08-04", "2026-08-05"
        )
        self.assertEqual(values, {"2026-08-04": 8.0, "2026-08-05": 8.1234})

    def test_nav_provider_fetches_every_advertised_page(self):
        calls = []

        def requester(request, _timeout):
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)["pageIndex"][0])
            calls.append(page)
            start = (page-1)*20
            rows = [
                {"FSRQ": (date(2026, 8, 8)-timedelta(days=start+offset)).isoformat(), "DWJZ": str(1+(start+offset)/1000)}
                for offset in range(20)
            ]
            return json.dumps({
                "Data": {"LSJZList": rows}, "ErrCode": 0,
                "TotalCount": 40, "PageSize": 20, "PageIndex": page,
            }).encode("utf-8")

        values, _ = EastmoneyFundNavProvider(requester=requester).fetch(
            "014978", "2026-06-01", "2026-08-08",
        )
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(values), 40)

    def test_valid_empty_nav_response_means_not_published_instead_of_failure(self):
        raw = json.dumps({"Data": {"LSJZList": []}, "ErrCode": 0, "ErrMsg": None}).encode("utf-8")
        self.assertEqual(parse_eastmoney_nav_payload(raw, "014978"), {})

        service = RecurringInvestmentService(
            self.db, FakeFundNavProvider({})
        )
        service.generate_due(date(2026, 8, 5))
        result = service.sync_pending_navs()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.db.row("SELECT status FROM recurring_occurrences")["status"], "待净值")

    def test_stopped_recurring_sync_starts_no_request_and_keeps_pending_state(self):
        provider = FakeFundNavProvider({"2026-08-05": 8.0})
        service = RecurringInvestmentService(self.db, provider)
        service.generate_due(date(2026, 8, 5))
        result = service.sync_pending_navs(stop_requested=lambda: True)
        self.assertTrue(result["stopped"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.db.row("SELECT status FROM recurring_occurrences")["status"], "待扣款核对")
        self.assertEqual(self.db.setting("recurring_last_sync_at", ""), "")
        self.assertEqual(self.db.setting("recurring_last_sync_attempt_status"), "已停止")

    def test_nav_api_rejects_bad_ranges_and_oversized_payloads(self):
        provider = EastmoneyFundNavProvider(requester=lambda *_: b"")
        with self.assertRaisesRegex(RecurringInvestmentError, "结束日期"):
            provider.fetch("014978", "2026-08-05", "2026-08-04")
        with self.assertRaisesRegex(RecurringInvestmentError, "超过5MB"):
            parse_eastmoney_nav_payload(b"x" * (5 * 1024 * 1024 + 1), "014978")

    def test_nav_windows_fallback_uses_system_curl_ipv4_tls12_without_insecure(self):
        raw = b'''var apidata={content:"<table><tr><td>2026-08-05</td><td>8.1234</td></tr></table>"};'''
        completed = SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
        with patch("app.recurring.urllib.request.urlopen", side_effect=ssl.SSLError("EOF")), \
             patch("app.recurring.windows_curl_path", return_value=r"C:\\Windows\\System32\\curl.exe"), \
             patch("app.recurring.subprocess.run", return_value=completed) as run:
            values, _url = EastmoneyFundNavProvider(timeout_seconds=5).fetch(
                "014978", "2026-08-05", "2026-08-05"
            )
        self.assertEqual(values, {"2026-08-05": 8.1234})
        command = run.call_args.args[0]
        self.assertIn("--ipv4", command)
        self.assertIn("--tls-max", command)
        self.assertNotIn("--insecure", command)

    def test_sync_calculates_but_does_not_book_until_user_confirmation(self):
        provider = FakeFundNavProvider({"2026-08-05": 8.0})
        service = RecurringInvestmentService(self.db, provider)
        service.generate_due(date(2026, 8, 5))
        result = service.sync_pending_navs()
        occurrence = self.db.row("SELECT * FROM recurring_occurrences")
        self.assertEqual(result["ready"], 1)
        self.assertEqual(occurrence["status"], "可确认入账")
        self.assertEqual(occurrence["units"], 1.25)
        self.assertEqual(self.db.row("SELECT nav FROM fund_nav_history WHERE fund_code='014978' AND nav_date='2026-08-05'")["nav"], 8.0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM cost_lots")["n"], 0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM trades WHERE action='定投'")["n"], 0)

    def test_confirm_is_atomic_idempotent_and_preserves_one_cost_lot_per_day(self):
        service = RecurringInvestmentService(self.db, FakeFundNavProvider({"2026-08-05": 8.0}))
        service.generate_due(date(2026, 8, 5)); service.sync_pending_navs()
        occurrence = self.db.row("SELECT * FROM recurring_occurrences")
        before = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1")
        cash_before = float(self.db.setting("active_cash_balance"))
        self.assertEqual(service.confirm([occurrence["id"]]), 1)
        self.assertEqual(service.confirm([occurrence["id"]]), 0)
        after = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1")
        self.assertAlmostEqual(after["units"] - before["units"], 1.25, places=6)
        self.assertAlmostEqual(after["cost"] - before["cost"], 10.0, places=6)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM cost_lots")["n"], 1)
        trade = self.db.row("SELECT * FROM trades WHERE action='定投'")
        self.assertEqual(trade["status"], "已确认")
        self.assertEqual(trade["source"], "test_nav")
        posting = self.db.row("SELECT * FROM ledger_postings WHERE trade_id=?", (trade["id"],))
        self.assertIsNotNone(posting)
        lot = self.db.row("SELECT * FROM cost_lots WHERE trade_id=?", (trade["id"],))
        self.assertEqual(lot["occurrence_id"], occurrence["id"])
        self.assertEqual(lot["pricing_date"], "2026-08-05")
        self.assertEqual(trade["counts_toward_active_capital"], 0)
        self.assertAlmostEqual(float(self.db.setting("active_cash_balance")), cash_before)
        confirmed_occurrence = self.db.row(
            "SELECT * FROM recurring_occurrences WHERE id=?", (occurrence["id"],)
        )
        self.assertEqual(int(confirmed_occurrence["deduction_confirmed"]), 1)
        self.assertEqual(
            confirmed_occurrence["deduction_confirmation_source"], "用户批量确认入账"
        )
        self.assertTrue(confirmed_occurrence["deduction_confirmed_at"])

        PortfolioLedgerService(self.db).reverse_posting(trade["id"], "测试撤销定投入账")
        restored = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1")
        self.assertAlmostEqual(restored["units"], before["units"], places=6)
        self.assertAlmostEqual(restored["cost"], before["cost"], places=6)
        self.assertEqual(
            self.db.row("SELECT status FROM recurring_occurrences WHERE id=?", (occurrence["id"],))["status"],
            "已跳过",
        )

    def test_missing_nav_stays_pending_and_never_changes_holdings(self):
        service = RecurringInvestmentService(self.db, FakeFundNavProvider({"2026-08-04": 7.9}))
        service.generate_due(date(2026, 8, 5))
        before = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1")
        result = service.sync_pending_navs()
        after = self.db.row("SELECT units,cost FROM holdings WHERE fund_code='014978' AND active=1")
        self.assertEqual(result["missing"], 1)
        self.assertEqual(self.db.row("SELECT status FROM recurring_occurrences")["status"], "待净值")
        self.assertEqual(before, after)

    def test_subscription_formula_rounds_fee_and_units_like_a_fund_confirmation(self):
        fee, units = calculate_subscription(100, 1.2345, 0.12)
        self.assertEqual(fee, 0.12)
        self.assertEqual(units, 80.91)

class FakeMarketProvider:
    name = "eastmoney_public"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def fetch(self, security_id: str, limit: int = 90):
        self.calls.append(security_id)
        if self.fail:
            from app.market_data import MarketDataError
            raise MarketDataError("模拟网络失败")
        end = ChinaTradingCalendar().latest_completed_trading_day(local_now())
        start = end - timedelta(days=29)
        return [
            MarketBar(
                trade_date=(start + timedelta(days=i)).isoformat(),
                open=100 + i, close=100.5 + i, high=101 + i, low=99 + i,
                volume=1000 + i, amount=100000 + i * 1000,
            )
            for i in range(30)
        ]


class FixedCutoffMarketProvider:
    """Deterministic public provider for official-publication-delay tests."""

    name = "eastmoney_public"

    def __init__(self, default_cutoff: str, cutoff_by_security: dict[str, str] | None = None):
        self.default_cutoff = default_cutoff
        self.cutoff_by_security = cutoff_by_security or {}
        self.calls = []

    def fetch(self, security_id: str, limit: int = 90):
        self.calls.append(security_id)
        end = date.fromisoformat(self.cutoff_by_security.get(security_id, self.default_cutoff))
        calendar = ChinaTradingCalendar()
        days = []
        cursor = end
        while len(days) < 35:
            if calendar.is_trading_day(cursor):
                days.append(cursor)
            cursor -= timedelta(days=1)
        days.reverse()
        return [
            MarketBar(
                trade_date=day.isoformat(), open=100 + i, close=100.5 + i,
                high=101 + i, low=99 + i, volume=1000 + i,
                amount=100000 + i * 1000,
            )
            for i, day in enumerate(days)
        ]


class DecisionDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "drafts.sqlite3")
        self.db.execute("UPDATE recurring_plans SET enabled=0")
        self.service = DecisionDraftService(self.db)
        self.morning = datetime(2026, 8, 7, 11, 0)
        self.closed = datetime(2026, 8, 7, 15, 5)
        # Network/execution evidence is seeded with the real current clock so
        # job creation exercises today's production gate.  After creation the
        # isolated workflow is replayed consistently on `self.morning`.
        checked = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO funds(
                   fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                   purchase_fee_pct,status,notes,updated_at
               ) VALUES('001632','天弘中证食品饮料ETF联接C','930653.CSI',
                        '中证食品饮料主题指数','C','食品饮料',0,'可买','',?)
               ON CONFLICT(fund_code) DO UPDATE SET updated_at=excluded.updated_at""",
            (checked,),
        )
        self.db.execute(
            """UPDATE investable_funds SET relation_kind='同一指数',share_class='C',
                      strategy_kind='被动ETF联接',passive_verified=1,
                      public_buyable_status='已核对可申购',platform_checked_at=?,verified_at=?,
                      verification_algorithm=?
                 WHERE fund_code='001632' AND investable_index_id IN (
                       SELECT id FROM investable_indices
                        WHERE official_index_code='930653' AND active=1)""",
            (checked, checked, STRICT_FIELD_SYNC_ALGORITHM_VERSION),
        )
        self.db.save_settings({
            "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
        })
        self.food_index = self.db.row(
            """SELECT id,universe_id FROM investable_indices
                 WHERE official_index_code='930653' AND active=1"""
        )
        self.assertIsNotNone(self.food_index)
        PlatformCatalogService(self.db).refresh_admission(int(self.food_index["id"]))
        self.current_task_id = ResearchOrchestrator(self.db).create_job("完整扫描")
        job = self.db.row(
            "SELECT total_items,ranking_policy_version,ranking_policy_json FROM analysis_jobs WHERE task_id=?", (self.current_task_id,)
        )
        self.db.execute(
            "UPDATE analysis_jobs SET status='已完成',stage='完成',completed_items=total_items,"
            "research_date=? WHERE task_id=?",
            (self.morning.date().isoformat(), self.current_task_id),
        )
        self.db.execute(
            """INSERT INTO radar_runs(task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                self.current_task_id,
                ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(self.morning).isoformat(),
                "轮动震荡", int(job["total_items"]), int(job["total_items"]),
                job["ranking_policy_version"], job["ranking_policy_json"], "测试",
                json.dumps({"protocol_version":"3.0"}),
                self.morning.isoformat(timespec="seconds"),
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_candidate(self, *, amount=800.0, fund_code="001632"):
        now = self.morning.isoformat(timespec="seconds")
        target_pct = float(amount) / 8000 * 100
        return self.db.execute(
            """INSERT INTO candidates(task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,status,
               research_tilt,timing_status,execution_status,score,suggested_amount,correlation_bucket,
               thesis,risk,allocation_pct,target_allocation_pct,data_confidence,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.current_task_id, 1, self.food_index["universe_id"], "930653.CSI",
                "食品饮料", fund_code, "测试基金", "推荐买", "推荐买", "可以买", "可执行",
                90, amount, "食品饮料", "估值与景气改善", "回撤风险",
                target_pct, target_pct, 90, now,
            ),
        )

    def _seed_recurring_today(self):
        now = self.morning.isoformat(timespec="seconds")
        return self.db.execute(
            """INSERT INTO recurring_plans(fund_code,amount,frequency,weekdays,start_date,end_date,
               purchase_fee_pct,auto_sync,enabled,notes,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("020741", 10, "每日", "0,1,2,3,4", "2026-08-07", "2026-08-07", 0, 1, 1, "每日定投", now, now),
        )

    def test_ai_advice_becomes_draft_without_creating_trade(self):
        candidate_id = self._seed_candidate()
        first = self.service.add_candidate(candidate_id, as_of=self.morning)
        second = self.service.add_candidate(candidate_id, as_of=self.morning)
        self.assertEqual(first, second)
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (first,))
        self.assertEqual((row["source_kind"], row["action"], row["status"]), ("AI板块建议", "买入", "草稿"))
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM trades")["n"], 1)

    def test_ai_candidate_amount_recalculates_after_position_changes(self):
        self.db.execute(
            """UPDATE holdings SET active=1,counts_toward_active_capital=1,
                      units=500,latest_nav=2,cost=1000 WHERE fund_code='020341'"""
        )
        candidate_id = self._seed_candidate(amount=2000, fund_code="020341")
        # This amount-only unit test intentionally exercises the fund-identity
        # fallback rather than pretending gold belongs to the food index.
        self.db.execute(
            "UPDATE candidates SET allocation_pct=25,universe_id='' WHERE id=?",
            (candidate_id,),
        )
        row = self.db.row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        self.assertEqual(self.service.live_candidate_amount(row), 1000)
        # Market-value/NAV movement must not manufacture or consume cash room.
        self.db.execute("UPDATE holdings SET units=750 WHERE fund_code='020341' AND active=1")
        self.assertEqual(self.service.live_candidate_amount(row), 1000)
        # A real additional committed cost does reduce the incremental target.
        self.db.execute("UPDATE holdings SET cost=1500 WHERE fund_code='020341' AND active=1")
        self.assertEqual(self.service.live_candidate_amount(row), 500)

    def test_observe_or_unbound_ai_advice_cannot_become_trade_draft(self):
        with self.assertRaises(DecisionDraftError):
            self.service.add_candidate(self._seed_candidate(amount=0), as_of=self.morning)
        other = self._seed_candidate(amount=100, fund_code="")
        with self.assertRaises(DecisionDraftError):
            self.service.add_candidate(other, as_of=self.morning)

    def test_quantified_ai_holding_sale_can_be_copied_to_draft(self):
        self._seed_candidate()
        now = self.morning.isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO holding_recommendations(task_id,fund_code,fund_name,sleeve,market_value,
               position_pct_active,action,sell_pct,estimated_sell_amount,reason,risk,source,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.current_task_id, "020741", "持仓测试基金", "主动", 6800, 85, "减仓", 25, 1700,
             "降低集中度", "波动较高", "V4-Pro", now),
        )
        draft_id = self.service.add_holding_action(
            self.current_task_id, "020741", as_of=self.morning,
        )
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        self.assertEqual((row["source_kind"], row["action"], row["planned_pct"]), ("AI持仓建议", "卖出", 0))
        self.assertEqual(row["planned_units"], 0)
        self.assertGreater(row["planned_amount"], 0)
        self.assertIn("25.0%", row["ai_advice"])

    def test_stale_ai_holding_sale_cannot_be_copied_to_draft(self):
        self._seed_candidate()
        now = self.morning.isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO holding_recommendations(task_id,fund_code,fund_name,sleeve,market_value,
               position_pct_active,action,sell_pct,estimated_sell_amount,reason,risk,source,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.current_task_id, "020741", "持仓测试基金", "主动", 6800, 85, "减仓", 25, 1700,
             "降低集中度", "波动较高", "V4-Pro", now),
        )
        current_day = ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now())
        previous = ChinaTradingCalendar.from_db(self.db).previous_trading_day(current_day).isoformat()
        self.db.execute(
            "UPDATE radar_runs SET data_cutoff=? WHERE task_id=?",
            (previous, self.current_task_id),
        )
        with self.assertRaisesRegex(DecisionDraftError, "只能阅读"):
            self.service.add_holding_action(self.current_task_id, "020741", as_of=self.morning)
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM decision_drafts WHERE source_kind='AI持仓建议'")["n"], 0,
        )

    def test_manual_new_fund_is_marked_pending_verification_and_edit_is_in_place(self):
        draft_id = self.service.add_manual(
            fund_code="123456", fund_name="新基金测试", action="买入", planned_amount=100,
            rationale="盘中计划", as_of=self.morning,
        )
        self.service.update_draft(draft_id, fund_code="123456", fund_name="", action="买入", planned_amount=120)
        fund = self.db.row("SELECT status,name_status FROM funds WHERE fund_code='123456'")
        self.assertEqual((fund["status"], fund["name_status"]), ("待自动识别", "待自动识别"))
        self.assertEqual(self.db.row("SELECT planned_amount FROM decision_drafts WHERE id=?", (draft_id,))["planned_amount"], 120)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM decision_drafts")["n"], 1)

    def test_manual_new_fund_needs_only_code_amount_and_pricing_day(self):
        draft_id = self.service.add_manual(
            fund_code="654321", action="买入", planned_amount=88,
            pricing_date="2026-08-06", as_of=self.morning,
        )
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        self.assertEqual(row["fund_name"], "待自动识别（654321）")
        self.assertEqual(row["pricing_date"], "2026-08-06")
        self.assertEqual((row["planned_units"], row["planned_pct"]), (0, 0))

    def test_online_draft_enrichment_uses_published_nav_and_keeps_it_estimated(self):
        draft_id = self.service.add_manual(
            fund_code="020741", action="买入", planned_amount=100,
            pricing_date="2026-08-07", as_of=self.morning,
        )
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-07": 1.25})
        result = settlement.enrich([draft_id])
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        self.assertEqual(result.navs_resolved, 1)
        self.assertEqual(row["settlement_nav"], 1.25)
        self.assertEqual(row["settlement_units"], 80.0)
        self.assertIn("估算", row["settlement_status"])

    def test_stopped_fund_name_resolution_starts_no_request_and_preserves_pending_name(self):
        self.service.add_manual(
            fund_code="654321", action="买入", planned_amount=88,
            pricing_date="2026-08-06", as_of=self.morning,
        )
        metadata = FundMetadataService(self.db)
        with patch.object(metadata, "resolve_one") as resolve_one:
            resolved, errors = metadata.resolve_pending(stop_requested=lambda: True)
        self.assertEqual((resolved, errors), (0, []))
        resolve_one.assert_not_called()
        fund = self.db.row("SELECT fund_name,name_status FROM funds WHERE fund_code='654321'")
        self.assertEqual(fund, {"fund_name": "待自动识别（654321）", "name_status": "待自动识别"})

    def test_stopped_draft_enrichment_starts_no_request_and_preserves_draft(self):
        draft_id = self.service.add_manual(
            fund_code="020741", action="买入", planned_amount=100,
            pricing_date="2026-08-07", as_of=self.morning,
        )
        settlement = DraftSettlementService(self.db)
        provider = FakeFundNavProvider({"2026-08-07": 1.25})
        settlement.nav_provider = provider
        result = settlement.enrich([draft_id], stop_requested=lambda: True)
        self.assertTrue(result.stopped)
        self.assertEqual(result.checked, 0)
        self.assertEqual(provider.calls, [])
        row = self.db.row(
            "SELECT settlement_nav,settlement_units,settlement_status FROM decision_drafts WHERE id=?",
            (draft_id,),
        )
        self.assertEqual(
            row,
            {"settlement_nav": 0, "settlement_units": 0, "settlement_status": "待联网补全"},
        )

    def test_online_draft_enrichment_lazily_syncs_missing_redemption_fee(self):
        draft_id = self.service.add_manual(
            fund_code="020741", action="买入", planned_amount=100,
            pricing_date="2026-08-07", as_of=self.morning,
        )
        self.db.execute("DELETE FROM fee_tiers WHERE fund_code='020741'")

        class FakeFeeSync:
            def __init__(self, db):
                self.db = db

            def sync(self, fund_codes, *, only_missing=True):
                self.db.execute(
                    """INSERT INTO fee_tiers(
                           fund_code,min_days,max_days,redeem_fee_pct,source_url,checked_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (fund_codes[0], 0, 6, 1.5, "https://example.test/fee", "2026-08-07T16:00:00+08:00"),
                )
                return {"total": 1, "updated": 1, "skipped": 0, "errors": []}

        settlement = DraftSettlementService(self.db)
        settlement.fee_sync = FakeFeeSync(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-07": 1.25})
        result = settlement.enrich([draft_id])
        self.assertEqual(result.fee_schedules_resolved, 1)
        self.assertEqual(result.fee_schedule_failures, 0)
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM fee_tiers WHERE fund_code='020741'")["n"], 1,
        )

    def test_pending_trade_can_fetch_nav_and_estimate_shares_without_self_confirming(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-08-07", "020741", "买入", 2000, 0, 0, 0, "已下单待确认",
             self.morning.isoformat(), "", "待确认", "今日草稿", "2026-08-07"),
        )
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-07": 1.25})
        result = settlement.enrich_trade(trade_id)
        row = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual((result["nav"], result["units"]), (1.25, 1600.0))
        self.assertEqual((row["nav"], row["units"], row["status"]), (1.25, 1600.0, "待确认"))
        self.assertIn("估算", row["source"])

    def test_pending_buy_nav_fill_preserves_known_units_and_is_idempotent(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-08-07", "020741", "买入", 2000, 1599.25, 0, 0.12, "已取得实际份额",
             self.morning.isoformat(), "", "待确认", "用户手工待确认", "2026-08-07"),
        )
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-07": 1.25})
        first = settlement.enrich_trade(trade_id)
        second = settlement.enrich_trade(trade_id)
        row = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(first["units_basis"], "recorded")
        self.assertEqual(second["units_basis"], "recorded")
        self.assertEqual((row["units"], row["fee"], row["status"]), (1599.25, 0.12, "待确认"))
        self.assertEqual(row["source"].count("保留用户已填写的实际份额和费用"), 1)

    def test_bulk_pending_trade_enrichment_continues_and_never_confirms(self):
        ids = []
        for day in ("2026-08-07", "2026-08-08"):
            ids.append(self.db.execute(
                """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,
                       created_at,confirm_date,status,source,nav_date)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (day, "020741", "买入", 100, 0, 0, 0, "待确认", self.morning.isoformat(),
                 "", "待确认", "测试", day),
            ))
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-07": 1.25})
        result = settlement.enrich_pending_trades(ids)
        self.assertEqual((result.checked, result.resolved, result.waiting, result.failed), (2, 1, 1, 0))
        rows = self.db.rows(
            "SELECT status,confirm_date,nav FROM trades WHERE id IN (?,?) ORDER BY id", ids,
        )
        self.assertEqual([row["status"] for row in rows], ["待确认", "待确认"])
        self.assertEqual([row["confirm_date"] for row in rows], ["", ""])
        self.assertEqual([row["nav"] for row in rows], [1.25, 0])

    def test_stopped_pending_trade_enrichment_starts_no_request_and_preserves_trade(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-08-07", "020741", "买入", 2000, 0, 0, 0, "待确认",
             self.morning.isoformat(), "", "待确认", "测试", "2026-08-07"),
        )
        settlement = DraftSettlementService(self.db)
        provider = FakeFundNavProvider({"2026-08-07": 1.25})
        settlement.nav_provider = provider
        result = settlement.enrich_pending_trades(
            [trade_id], stop_requested=lambda: True,
        )
        self.assertTrue(result.stopped)
        self.assertEqual((result.checked, result.resolved), (0, 0))
        self.assertEqual(provider.calls, [])
        row = self.db.row("SELECT units,nav,status,confirm_date FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(row, {"units": 0, "nav": 0, "status": "待确认", "confirm_date": ""})

    def test_pending_sale_nav_fill_preserves_user_units_and_leaves_net_amount_pending(self):
        trade_id = self.db.execute(
            """INSERT INTO trades(trade_date,fund_code,action,amount,units,nav,fee,rationale,
                   created_at,confirm_date,status,source,nav_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-08-13", "020741", "卖出", 0, 2878.92, 0, 0, "14:00已提交赎回",
             self.morning.isoformat(), "", "待确认", "用户手工待确认", "2026-08-13"),
        )
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-13": 1.20})
        result = settlement.enrich_trade(trade_id)
        row = self.db.row("SELECT * FROM trades WHERE id=?", (trade_id,))
        self.assertEqual(result["units_basis"], "recorded")
        self.assertEqual(result["gross_estimate"], 3454.70)
        self.assertEqual((row["amount"], row["units"], row["nav"]), (0, 2878.92, 1.20))
        self.assertEqual(row["status"], "待确认")
        self.assertIn("保留用户填写", row["source"])

    def test_sale_draft_nav_fill_preserves_planned_units(self):
        draft_id = self.service.add_manual(
            fund_code="020741", action="卖出", planned_amount=1,
            pricing_date="2026-08-13", as_of=self.morning,
        )
        self.db.execute(
            "UPDATE decision_drafts SET planned_amount=0,planned_units=? WHERE id=?",
            (2878.92, draft_id),
        )
        settlement = DraftSettlementService(self.db)
        settlement.nav_provider = FakeFundNavProvider({"2026-08-13": 1.20})
        result = settlement.enrich([draft_id])
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        self.assertEqual(result.navs_resolved, 1)
        self.assertEqual((row["planned_units"], row["settlement_units"]), (2878.92, 2878.92))
        self.assertIn("保留已填卖出份额", row["settlement_status"])
        self.assertIn("3454.70", row["result_note"])

    def test_deleted_draft_never_enters_trade_log(self):
        candidate_id = self._seed_candidate()
        draft_id = self.service.add_candidate(candidate_id, as_of=self.morning)
        self.service.delete_draft(draft_id, as_of=self.morning)
        result = self.service.finalize_after_close(as_of=self.closed)
        self.assertEqual(result["total"], 0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM trades")["n"], 1)

    def test_finalize_is_blocked_before_close_and_idempotent_after_close(self):
        candidate_id = self._seed_candidate()
        draft_id = self.service.add_candidate(candidate_id, as_of=self.morning)
        simulated_check = self.closed.isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at=?,verified_at=? WHERE fund_code='001632'",
            (simulated_check, simulated_check),
        )
        with self.assertRaises(DecisionDraftError):
            self.service.finalize_after_close(as_of=self.morning)
        first = self.service.finalize_after_close(as_of=self.closed)
        second = self.service.finalize_after_close(as_of=self.closed)
        self.assertEqual(first, {"trades": 1, "recurring": 0, "total": 1})
        self.assertEqual(second, {"trades": 0, "recurring": 0, "total": 0})
        trade = self.db.row("SELECT * FROM trades WHERE id=(SELECT trade_id FROM decision_drafts WHERE id=?)", (draft_id,))
        self.assertEqual(trade["status"], "待确认")
        self.assertEqual(trade["nav"], 0)

    def test_recurring_draft_is_automatic_unique_and_waits_for_nav(self):
        self._seed_recurring_today()
        self.assertEqual(self.service.ensure_today_recurring(as_of=self.morning), 1)
        self.assertEqual(self.service.ensure_today_recurring(as_of=self.morning), 0)
        row = self.db.row("SELECT * FROM decision_drafts WHERE source_kind='自动定投'")
        self.assertEqual((row["action"], row["planned_amount"], row["status"]), ("定投", 10, "草稿"))
        result = self.service.finalize_after_close(as_of=self.closed)
        self.assertEqual(result, {"trades": 0, "recurring": 1, "total": 1})
        self.assertEqual(self.db.row("SELECT status FROM decision_drafts WHERE id=?", (row["id"],))["status"], "等待净值确认")

    def test_deleted_recurring_draft_is_skipped_and_not_recreated(self):
        self._seed_recurring_today()
        self.service.ensure_today_recurring(as_of=self.morning)
        row = self.db.row("SELECT * FROM decision_drafts WHERE source_kind='自动定投'")
        self.service.delete_draft(row["id"], as_of=self.morning)
        occurrence = self.db.row("SELECT status FROM recurring_occurrences WHERE id=?", (row["recurring_occurrence_id"],))
        self.assertEqual(occurrence["status"], "已跳过")
        self.assertEqual(self.service.ensure_today_recurring(as_of=self.morning), 0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM decision_drafts")["n"], 0)


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_test20_mapping_is_complete_and_explicit(self):
        placeholders = ",".join("?" for _ in TEST20_IDS)
        rows = self.db.rows(
            f"SELECT * FROM proxy_mappings WHERE universe_id IN ({placeholders}) AND active=1", TEST20_IDS
        )
        self.assertEqual(len(rows), 20)
        self.assertEqual({row["universe_id"] for row in rows}, set(TEST20_IDS))
        self.assertTrue(all(row["rationale"] for row in rows))
        by_id = {row["universe_id"]: row for row in rows}
        self.assertEqual(by_id["U010"]["security_id"], "2.932000")
        self.assertEqual(by_id["U011"]["security_id"], "2.930050")

    def test_completed_prefix_migration_does_not_overwrite_later_user_edits(self):
        self.db.execute("UPDATE proxy_mappings SET security_id='1.932000' WHERE universe_id='U010'")
        self.db.execute("UPDATE proxy_mappings SET security_id='custom.930050' WHERE universe_id='U011'")
        Database(self.db.path)
        self.assertEqual(self.db.row("SELECT security_id FROM proxy_mappings WHERE universe_id='U010'")["security_id"], "1.932000")
        self.assertEqual(self.db.row("SELECT security_id FROM proxy_mappings WHERE universe_id='U011'")["security_id"], "custom.930050")

    def test_metric_calculation_uses_trading_row_offsets(self):
        provider = FakeMarketProvider()
        bars = provider.fetch("1.000001")
        mapping = ProxyMapping("U001", provider.name, "1.000001", "000001", "上证指数", "精确指数", 1.0, "test")
        metric = compute_metric(mapping, bars)
        self.assertAlmostEqual(metric["d1"], (129.5 / 128.5 - 1) * 100, places=4)
        self.assertAlmostEqual(metric["d20"], (129.5 / 109.5 - 1) * 100, places=4)
        self.assertEqual(metric["data_quality"], "PRICE_ONLY")

    def test_sector_resolver_prefers_exact_board_and_accepts_curated_alias_only(self):
        def requester(url, _label, _timeout):
            term = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["input"][0]
            rows = {
                "白酒": [
                    {"Name": "白酒", "SecurityTypeName": "指数", "QuoteID": "1.930901", "Code": "930901"},
                    {"Name": "白酒", "SecurityTypeName": "板块", "QuoteID": "90.BK0896", "Code": "BK0896"},
                ],
                "CPO": [
                    {"Name": "CPO概念", "SecurityTypeName": "板块", "QuoteID": "90.BK1122", "Code": "BK1122"},
                    {"Name": "光通信", "SecurityTypeName": "板块", "QuoteID": "90.BK9999", "Code": "BK9999"},
                ],
            }.get(term, [])
            return {"QuotationCodeTable": {"Data": rows}}

        resolver = SectorMappingResolver(self.db, requester=requester)
        exact = resolver._resolve_one({"index_code": "U052", "index_name": "白酒", "category": "细分行业"})
        self.assertEqual(exact["security_id"], "90.BK0896")
        self.assertEqual(exact["mapping_kind"], "精确板块")
        alias = resolver._resolve_one({"index_code": "U108", "index_name": "CPO与光模块", "category": "主题"})
        self.assertEqual(alias["proxy_name"], "CPO概念")
        self.assertEqual(alias["mapping_kind"], "显式代理")

    def test_sector_resolver_rejects_ambiguous_or_unreviewed_fuzzy_names(self):
        payload = {"QuotationCodeTable": {"Data": [
            {"Name": "测试概念", "SecurityTypeName": "板块", "QuoteID": "90.BK1001"},
            {"Name": "测试主题", "SecurityTypeName": "板块", "QuoteID": "90.BK1002"},
        ]}}
        resolver = SectorMappingResolver(self.db, requester=lambda *_: payload)
        result = resolver._resolve_one({"index_code": "U999", "index_name": "测试", "category": "主题"})
        self.assertIsNone(result)

    def test_sector_resolver_stop_prevents_the_next_network_request(self):
        calls = []
        resolver = SectorMappingResolver(
            self.db, requester=lambda *args: calls.append(args) or {},
        )
        universe = [
            {"index_code": "X001", "index_name": "测试一", "category": "主题"},
            {"index_code": "X002", "index_name": "测试二", "category": "主题"},
        ]
        result = resolver.refresh(universe=universe, force=True, stop_requested=lambda: True)
        self.assertTrue(result["stopped"])
        self.assertEqual(calls, [])
        self.assertEqual(self.db.setting("mapping_last_refresh_at", ""), "")
        self.assertEqual(self.db.setting("mapping_last_refresh_attempt_status"), "已停止")

    def test_constituent_breadth_calculates_board_and_broad_index(self):
        def requester(url, _label, _timeout):
            changes = [3, 2, 1, 0, -1, -2]
            if "push2delay" in url:
                return {"data": {"diff": [
                    {"f3": value, "f127": value + .1, "f109": value + .2,
                     "f160": value + .3, "f110": value + .4}
                    for value in changes
                ]}}
            return {"result": {"data": [{"f3": value} for value in changes]}}

        provider = ConstituentBreadthProvider(requester=requester, timeout_seconds=5)
        board = provider.fetch("U052", "90.BK0896", "2026-08-05")
        broad = provider.fetch("U006", "1.000300", "2026-08-05")
        self.assertEqual(board["constituent_count"], 6)
        self.assertEqual(broad["constituent_count"], 6)
        self.assertIn("上涨3/6", board["breadth_signal"])
        self.assertEqual(board["data_quality"], "COMPLETE_BOARD_SNAPSHOT")
        self.assertEqual(broad["source"], "eastmoney_index_components")

    def test_constituent_breadth_rejects_too_few_valid_members(self):
        provider = ConstituentBreadthProvider(
            requester=lambda *_: {"data": {"diff": [{"f3": 1}, {"f3": None}, {"f3": -1}]}}
        )
        with self.assertRaisesRegex(SectorResolveError, "有效成分股涨跌幅仅2只"):
            provider.fetch("U052", "90.BK0896", "2026-08-05")

    def test_required_breadth_is_real_when_supported_and_explicitly_missing_otherwise(self):
        class FakeBreadth:
            def __init__(self):
                self.calls = 0

            def supports(self, _uid, security_id):
                return security_id.startswith("90.BK")

            def fetch(self, _uid, _security_id, cutoff):
                self.calls += 1
                if self.calls == 1:
                    raise SectorResolveError("模拟一次临时网络中断")
                return {
                    "breadth_signal": f"成分股上涨4/6（截止{cutoff}）",
                    "data_quality": "COMPLETE_BOARD_SNAPSHOT",
                    "source": "fake_components",
                    "constituent_count": 6,
                    "returns_pct": {"d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5},
                    "data_cutoff": ChinaTradingCalendar().latest_completed_trading_day(local_now()).isoformat(),
                    "position_20d": "成分股20日上涨4/6",
                    "volume_signal": "快照不提供历史量比",
                }

        provider = FakeMarketProvider()
        breadth = FakeBreadth()
        self.db.execute(
            "UPDATE proxy_mappings SET security_id='90.BK0896',proxy_code='BK0896',proxy_name='白酒',mapping_kind='精确板块' WHERE universe_id='U001'"
        )
        universe = self.db.rows("SELECT * FROM universe WHERE index_code='U001'")
        rows = MarketDataPipeline(self.db, provider=provider, breadth_provider=breadth).populate_metrics(
            "breadth-real", universe, require_breadth=True
        )
        self.assertEqual(breadth.calls, 2)
        self.assertEqual(rows[0]["data_quality"], "COMPLETE_BOARD_SNAPSHOT")
        self.assertIn("成分股上涨4/6", rows[0]["breadth_signal"])

        self.db.execute(
            "UPDATE proxy_mappings SET security_id='1.000001',proxy_code='000001',proxy_name='上证指数',mapping_kind='精确指数' WHERE universe_id='U001'"
        )
        rows = MarketDataPipeline(self.db, provider=provider, breadth_provider=breadth).populate_metrics(
            "breadth-proxy", universe, require_breadth=True
        )
        self.assertEqual(rows[0]["data_quality"], "DISCLOSED_PRICE_PROXY")
        self.assertEqual(rows[0]["breadth_kind"], "missing_unavailable")
        self.assertIsNone(rows[0]["breadth_pct"])
        self.assertIn("真实成分股扩散度", rows[0]["breadth_signal"])
        self.assertIn("缺失", rows[0]["breadth_signal"])
        self.assertNotIn("多周期动量一致性", rows[0]["breadth_signal"])

    def test_market_only_job_populates_ten_csi_industries_without_deepseek(self):
        ensure_strict_scan_items(self.db, 10)
        provider = FakeMarketProvider()
        pipeline = MarketDataPipeline(self.db, provider=provider)
        orchestrator = ResearchOrchestrator(self.db, market_pipeline=pipeline)
        task_id = orchestrator.create_job("20项行情测试")
        result = orchestrator.run(task_id)
        self.assertEqual(result.status, "行情测试完成")
        self.assertEqual(result.completed_items, 10)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM sector_metrics WHERE task_id=?", (task_id,))["n"], 10)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM market_fetch_attempts WHERE task_id=?", (task_id,))["n"], 10)
        self.assertEqual(len(provider.calls), 10)

    def test_full_market_update_uses_entire_frozen_catalog_without_deepseek(self):
        expected = ensure_strict_scan_items(self.db, 13)
        provider = FakeMarketProvider()
        pipeline = MarketDataPipeline(self.db, provider=provider)
        orchestrator = ResearchOrchestrator(self.db, market_pipeline=pipeline)

        task_id = orchestrator.create_job("全目录行情更新")
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        result = orchestrator.run(task_id)

        self.assertEqual(job["total_items"], len(expected))
        self.assertEqual(json.loads(job["universe_ids_json"]), [row["index_code"] for row in expected])
        self.assertEqual(result.status, "行情更新完成")
        self.assertEqual(result.completed_items, len(expected))
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM sector_metrics WHERE task_id=?", (task_id,))["n"], len(expected))
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0)
        self.assertEqual(len(provider.calls), len(expected))

    def test_full_market_update_stops_between_indices_and_keeps_real_progress(self):
        ensure_strict_scan_items(self.db, 5)
        provider = FakeMarketProvider()
        orchestrator = ResearchOrchestrator(
            self.db, market_pipeline=MarketDataPipeline(self.db, provider=provider),
        )
        task_id = orchestrator.create_job("全目录行情更新")

        def stop_after_first(_stage, completed, _total, _message):
            if completed == 1:
                orchestrator.request_stop()

        result = orchestrator.run(task_id, stop_after_first)

        self.assertEqual(result.status, "已停止")
        self.assertEqual(result.completed_items, 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0)
        self.assertIn("不会再发送新的联网或API请求", result.message)

    def test_same_task_resume_reuses_previous_session_prefix_without_redownload(self):
        expected = ensure_strict_scan_items(self.db, 10)
        clock = lambda: datetime(2026, 8, 11, 17, 59)
        first_provider = FixedCutoffMarketProvider("2026-08-10")
        first = ResearchOrchestrator(
            self.db,
            market_pipeline=MarketDataPipeline(self.db, first_provider, clock=clock),
        )
        task_id = first.create_job("20项行情测试")

        def stop_after_first(_stage, completed, _total, _message):
            if completed == 1:
                first.request_stop()

        stopped = first.run(task_id, stop_after_first)
        self.assertEqual(stopped.status, "已停止")
        self.assertEqual(len(first_provider.calls), 1)
        completed_security = first_provider.calls[0]

        resumed_provider = FixedCutoffMarketProvider("2026-08-10")
        resumed = ResearchOrchestrator(
            self.db,
            market_pipeline=MarketDataPipeline(self.db, resumed_provider, clock=clock),
        ).run(task_id)

        self.assertEqual(resumed.status, "行情测试完成")
        self.assertEqual(resumed.completed_items, len(expected))
        self.assertEqual(len(resumed_provider.calls), len(expected) - 1)
        self.assertNotIn(completed_security, resumed_provider.calls)
        reused = self.db.row(
            """SELECT COUNT(*) AS n FROM market_fetch_attempts
               WHERE task_id=? AND outcome='复用任务断点行情'""",
            (task_id,),
        )["n"]
        self.assertEqual(reused, 1)
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM model_calls WHERE task_id=?", (task_id,)
            )["n"],
            0,
        )

    def test_complete_scan_blocks_before_download_when_mapping_is_incomplete(self):
        current = ensure_strict_scan_items(self.db, 10)
        missing_id = current[0]["index_code"]
        provider = FakeMarketProvider()
        pipeline = MarketDataPipeline(self.db, provider=provider)
        orchestrator = ResearchOrchestrator(self.db, market_pipeline=pipeline)
        task_id = orchestrator.create_job("完整扫描")
        self.db.execute(
            "UPDATE proxy_mappings SET active=0 WHERE universe_id=?",
            (missing_id,),
        )
        # Disabling the quote route removes this index from the current strict
        # catalog.  The task snapshot must be rejected before any download rather
        # than silently shrinking the task to nine items.
        with self.assertRaisesRegex(DeepSeekError, "目录.*不一致|禁止续跑"):
            orchestrator.run(task_id)
        self.assertEqual(provider.calls, [])

    def test_public_provider_parser_rejects_no_fields_and_parses_valid_rows(self):
        start = date.today() - timedelta(days=29)
        lines = [
            f"{(start + timedelta(days=i)).isoformat()},{100+i},{100.5+i},{101+i},{99+i},{1000+i},{100000+i*1000},0,0,0,0"
            for i in range(30)
        ]
        payload = {"data": {"klines": lines}}

        def requester(request, _timeout):
            self.assertIn("secid=1.000001", request.full_url)
            self.assertIn("fields2=", request.full_url)
            return json.dumps(payload).encode("utf-8")

        bars = EastmoneyKlineProvider(requester=requester).fetch("1.000001")
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[-1].trade_date, date.today().isoformat())
        self.assertEqual(bars[-1].close, 129.5)

    def test_csi_official_identity_routes_numeric_and_h_codes_without_vendor_guessing(self):
        start = date.today() - timedelta(days=29)
        rows = [{
            "indexCode": "H11057", "indexNameCnAll": "中证石化产业指数",
            "tradeDate": (start + timedelta(days=i)).strftime("%Y%m%d"),
            "open": 100 + i, "close": 100.5 + i, "high": 101 + i, "low": 99 + i,
            "tradingVol": 1000 + i, "tradingValue": 2 + i / 10,
        } for i in range(30)]

        def requester(request, _timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            self.assertEqual(query["indexCode"], ["H11057"])
            self.assertEqual(request.get_header("Origin"), "https://www.csindex.com.cn")
            self.assertEqual(request.get_header("Sec-fetch-site"), "same-origin")
            self.assertIn("Chrome/", request.get_header("User-agent"))
            return json.dumps({"code": 200, "msg": "success", "data": rows}).encode("utf-8")

        provider = EastmoneyKlineProvider(requester=requester)
        bars = provider.fetch("CSI.H11057")
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[-1].close, 129.5)
        self.assertAlmostEqual(bars[-1].amount, 490_000_000, places=2)
        self.assertEqual(provider.source_for("CSI.H11057"), "csi_official")
        self.assertIn("中证指数有限公司官网：成功", "；".join(provider.diagnostics_for("CSI.H11057")))

        with self.assertRaisesRegex(MarketDataError, "中证官方证券ID格式无效"):
            CSIOfficialKlineProvider(requester=requester).fetch("2.H11057")

    def test_csi_rate_limit_page_trips_one_job_circuit_without_repeated_curl(self):
        blocked = b"<!DOCTYPE html><html><head><title>405</title></head></html>"
        completed = SimpleNamespace(returncode=0, stdout=blocked, stderr=b"")
        provider = CSIOfficialKlineProvider(timeout_seconds=5, min_request_interval_seconds=0)
        with patch("app.market_data.urllib.request.urlopen", side_effect=ssl.SSLError("EOF")), \
             patch("app.market_data.windows_curl_path", return_value=r"C:\\Windows\\System32\\curl.exe"), \
             patch("app.market_data.subprocess.run", return_value=completed) as run, \
             patch("app.market_data.time.sleep"):
            with self.assertRaisesRegex(MarketTransportError, "无法连接中证官方行情源"):
                provider.fetch("CSI.931248")
            with self.assertRaisesRegex(MarketTransportError, "正在限制连续访问"):
                provider.fetch("CSI.930598")
        self.assertEqual(run.call_count, 1)

    def test_csi_truncated_response_retries_then_uses_windows_curl(self):
        start = date.today() - timedelta(days=29)
        rows = [{
            "indexCode": "930632", "indexNameCnAll": "中证芯片产业指数",
            "tradeDate": (start + timedelta(days=i)).strftime("%Y%m%d"),
            "open": 100 + i, "close": 100.5 + i, "high": 101 + i, "low": 99 + i,
            "tradingVol": 1000 + i, "tradingValue": 2 + i / 10,
        } for i in range(30)]
        raw = json.dumps({"code": 200, "msg": "success", "data": rows}).encode("utf-8")
        completed = SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
        provider = CSIOfficialKlineProvider(timeout_seconds=5, min_request_interval_seconds=0)

        with patch(
            "app.market_data.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"", 4096),
        ) as urlopen, patch(
            "app.market_data.windows_curl_path",
            return_value=r"C:\\Windows\\System32\\curl.exe",
        ), patch(
            "app.market_data.subprocess.run", return_value=completed,
        ) as run, patch("app.market_data.time.sleep"):
            bars = provider.fetch("CSI.930632")

        self.assertEqual(len(bars), 30)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(run.call_count, 1)
        diagnostics = "；".join(provider.diagnostics_for("CSI.930632"))
        self.assertIn("响应被远端截断", diagnostics)
        self.assertIn("Windows系统curl：成功接收完整响应", diagnostics)

    def test_csi_truncated_response_without_fallback_becomes_market_error(self):
        provider = CSIOfficialKlineProvider(timeout_seconds=5, min_request_interval_seconds=0)
        with patch(
            "app.market_data.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial-body", 123),
        ), patch("app.market_data.windows_curl_path", return_value=None), \
             patch("app.market_data.time.sleep"):
            with self.assertRaises(MarketTransportError) as raised:
                provider.fetch("CSI.930632")

        self.assertIn("响应被远端截断", str(raised.exception))
        self.assertNotIn("partial-body", str(raised.exception))
        self.assertEqual(
            sum("响应被远端截断" in item for item in raised.exception.diagnostics), 2,
        )

    def test_tencent_fallback_maps_markets_parses_volume_and_rejects_wrong_key(self):
        self.assertEqual(TencentKlineProvider.symbol_for("1.000001"), "sh000001")
        self.assertEqual(TencentKlineProvider.symbol_for("0.399001"), "sz399001")
        self.assertEqual(TencentKlineProvider.symbol_for("0.899050"), "bj899050")
        self.assertEqual(TencentKlineProvider.symbol_for("2.932000"), "sh932000")
        start = date.today() - timedelta(days=29)
        rows = [
            [(start + timedelta(days=i)).isoformat(), str(100+i), str(100.5+i), str(101+i), str(99+i), str(1000+i)]
            for i in range(30)
        ]
        raw = json.dumps({"code": 0, "data": {"sh000001": {"qfqday": rows}}}).encode()
        provider = TencentKlineProvider(requester=lambda *_: raw)
        bars = provider.fetch("1.000001")
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[-1].amount, 0)
        wrong = json.dumps({"code": 0, "data": {"sz000001": {"day": rows}}}).encode()
        with self.assertRaisesRegex(MarketDataError, "未按请求代码"):
            TencentKlineProvider(requester=lambda *_: wrong).fetch("1.000001")

    def test_sohu_exact_parser_normalizes_descending_rows_and_rejects_missing_symbol(self):
        start = date.today() - timedelta(days=29)
        rows = [
            [(start + timedelta(days=i)).isoformat(), str(100+i), str(100.5+i), "0", "0%",
             str(99+i), str(101+i), str(1000+i), str(100000+i*1000)]
            for i in range(30)
        ]
        raw = json.dumps(
            [{"status": 0, "hq": list(reversed(rows)), "stat": ["累计:", "测试"]}],
            ensure_ascii=False,
        ).encode("gb18030")
        provider = SohuKlineProvider(requester=lambda *_: raw)
        bars = provider.fetch("0.899050")
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[0].trade_date, start.isoformat())
        self.assertEqual(bars[-1].close, 129.5)
        self.assertEqual(SohuKlineProvider.symbol_for("0.899050"), "zs_899050")
        self.assertEqual(SohuKlineProvider.symbol_for("1.518880"), "cn_518880")
        missing = json.dumps([{"status": 2, "msg": "non-existent"}]).encode()
        with self.assertRaisesRegex(MarketDataError, "没有zs_932000"):
            SohuKlineProvider(requester=lambda *_: missing).fetch("2.932000")

    def test_reviewed_fallback_prefers_exact_sohu_then_labels_curated_etf(self):
        bars = FakeMarketProvider().fetch("1.000001")

        class FakeTencent:
            def fetch(self, security_id, _limit=90):
                if security_id in {"1.563300", "1.563080"}:
                    return bars
                raise MarketDataError(f"{security_id}有效交易日不足21个")

        class FakeSohu:
            def fetch(self, security_id, _limit=90):
                if security_id == "0.899050":
                    return bars
                raise MarketDataError(f"没有{security_id}")

        fallback = ReviewedFallbackKlineProvider(tencent=FakeTencent(), sohu=FakeSohu())
        self.assertEqual(len(fallback.fetch("0.899050")), 30)
        self.assertEqual(fallback.source_for("0.899050"), "sohu_public")
        self.assertIsNone(fallback.resolved_proxy_for("0.899050"))
        self.assertEqual(len(fallback.fetch("2.932000")), 30)
        self.assertEqual(fallback.source_for("2.932000"), "tencent_etf_proxy")
        resolved = fallback.resolved_proxy_for("2.932000")
        self.assertEqual(resolved.proxy_code, "563300")
        self.assertEqual(resolved.mapping_kind, "ETF代理")
        self.assertIn("精确证券失败", "；".join(fallback.diagnostics_for("2.932000")))

    def test_pipeline_downgrades_proxy_metadata_when_exact_index_is_unavailable(self):
        bars = FakeMarketProvider().fetch("1.000001")

        class FakeTencent:
            def fetch(self, security_id, _limit=90):
                if security_id in {"1.563300", "1.563080"}:
                    return bars
                raise MarketDataError("精确指数覆盖不足")

        class FakeSohu:
            def fetch(self, security_id, _limit=90):
                if security_id == "0.899050":
                    return bars
                raise MarketDataError("精确指数不存在")

        fallback = ReviewedFallbackKlineProvider(tencent=FakeTencent(), sohu=FakeSohu())
        provider = EastmoneyKlineProvider(requester=lambda *_: b"{}", fallback_provider=fallback)
        with patch.object(provider, "_fetch_primary", side_effect=MarketTransportError("主源断开", ["EOF"])):
            pipeline = MarketDataPipeline(self.db, provider=provider)
            universe = self.db.rows("SELECT * FROM universe WHERE index_code IN ('U005','U010','U011') ORDER BY index_code")
            rows = pipeline.populate_metrics("route-test", universe, require_breadth=False)
        by_id = {row["universe_id"]: row for row in rows}
        self.assertEqual(by_id["U005"]["proxy_code"], "899050")
        self.assertEqual(by_id["U005"]["mapping_kind"], "精确指数")
        self.assertEqual(by_id["U010"]["proxy_code"], "563300")
        self.assertEqual(by_id["U010"]["mapping_kind"], "ETF代理")
        self.assertEqual(by_id["U011"]["proxy_code"], "563080")
        self.assertIn("不是目标指数点位", by_id["U011"]["warnings_json"])

    def test_eastmoney_transport_failure_uses_independent_fallback_and_circuit_breaker(self):
        class Fallback:
            name = "tencent_public"
            def __init__(self): self.calls = []
            def fetch(self, security_id, limit=90):
                self.calls.append(security_id)
                return FakeMarketProvider().fetch(security_id, limit)

        fallback = Fallback()
        provider = EastmoneyKlineProvider(requester=lambda *_: b"{}", fallback_provider=fallback)
        with patch.object(
            provider, "_fetch_primary",
            side_effect=MarketTransportError("主源断开", ["Python EOF", "Windows curl 35"]),
        ) as primary:
            first = provider.fetch("1.000001")
            second = provider.fetch("0.399001")
        self.assertEqual(len(first), 30)
        self.assertEqual(len(second), 30)
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.calls, ["1.000001", "0.399001"])
        self.assertEqual(provider.source_for("1.000001"), "tencent_public")
        self.assertIn("量能按成交量", "；".join(provider.diagnostics_for("1.000001")))

    def test_volume_fallback_is_labelled_and_not_presented_as_turnover_amount(self):
        bars = FakeMarketProvider().fetch("1.000001")
        mapping = ProxyMapping("U001", "eastmoney_public", "1.000001", "000001", "上证指数", "精确指数", 1, "test")
        metric = compute_metric(mapping, bars, data_source="tencent_public", activity_field="volume")
        self.assertEqual(metric["data_source"], "tencent_public")
        self.assertIn("成交量", metric["volume_signal"])
        self.assertIn("不含历史成交额", metric["warnings_json"])

    def test_windows_system_curl_is_preferred_over_path_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            system32 = Path(folder) / "System32"
            system32.mkdir()
            expected = system32 / "curl.exe"
            expected.write_bytes(b"")
            with patch("app.market_data.sys.platform", "win32"), \
                 patch.dict("app.market_data.os.environ", {"SystemRoot": folder}, clear=False):
                self.assertEqual(windows_curl_path(), str(expected))

    def test_public_provider_reports_upstream_rc_for_missing_security(self):
        raw = json.dumps({"rc": 102, "rt": 1, "data": None}).encode("utf-8")
        provider = EastmoneyKlineProvider(requester=lambda _request, _timeout: raw)
        with self.assertRaises(MarketDataError) as caught:
            provider.fetch("1.932000")
        self.assertIn("rc=102", str(caught.exception))
        self.assertIn("市场前缀", str(caught.exception))

    def test_public_provider_rejects_bad_security_id_wrong_code_and_oversized_response(self):
        with self.assertRaisesRegex(MarketDataError, "证券ID格式无效"):
            EastmoneyKlineProvider(requester=lambda *_: b"{}").fetch("https://example.com")
        start = date.today() - timedelta(days=20)
        rows = [f"{(start + timedelta(days=i)).isoformat()},1,1,1,1,1,1" for i in range(21)]
        wrong = json.dumps({"data": {"code": "999999", "klines": rows}}).encode()
        with self.assertRaisesRegex(MarketDataError, "与请求000001不一致"):
            EastmoneyKlineProvider(requester=lambda *_: wrong).fetch("1.000001")
        with self.assertRaisesRegex(MarketDataError, "超过5MB"):
            EastmoneyKlineProvider(requester=lambda *_: b"x" * (5 * 1024 * 1024 + 1)).fetch("1.000001")

    def test_windows_curl_fallback_keeps_certificate_verification_enabled(self):
        start = date.today() - timedelta(days=29)
        lines = [
            f"{(start + timedelta(days=i)).isoformat()},{100+i},{100.5+i},{101+i},{99+i},{1000+i},{100000+i*1000},0,0,0,0"
            for i in range(30)
        ]
        raw = json.dumps({"data": {"klines": lines}}).encode("utf-8")
        completed = SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
        with patch("app.market_data.urllib.request.urlopen", side_effect=ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")) as urlopen, \
             patch("app.market_data.windows_curl_path", return_value=r"C:\\Windows\\System32\\curl.exe"), \
             patch("app.market_data.subprocess.run", return_value=completed) as run:
            bars = EastmoneyKlineProvider(timeout_seconds=5).fetch("1.000001")
        self.assertEqual(len(bars), 30)
        self.assertEqual(urlopen.call_count, 2)
        command = run.call_args.args[0]
        self.assertIn("--http1.1", command)
        self.assertNotIn("-k", command)
        self.assertNotIn("--insecure", command)

    def test_failed_network_uses_recent_cache_and_marks_it(self):
        ensure_strict_scan_items(self.db, 10)
        first = ResearchOrchestrator(self.db, market_pipeline=MarketDataPipeline(self.db, FakeMarketProvider()))
        first_id = first.create_job("20项行情测试")
        self.assertEqual(first.run(first_id).status, "行情测试完成")

        failed_provider = FakeMarketProvider(fail=True)
        second = ResearchOrchestrator(self.db, market_pipeline=MarketDataPipeline(self.db, failed_provider))
        second_id = second.create_job("20项行情测试")
        result = second.run(second_id)
        self.assertEqual(result.status, "行情测试完成")
        cached = self.db.row(
            "SELECT COUNT(*) n FROM sector_metrics WHERE task_id=? AND cache_hit=1", (second_id,)
        )["n"]
        self.assertEqual(cached, 10)
        self.assertEqual(failed_provider.calls, [])
        outcomes = self.db.rows(
            "SELECT DISTINCT outcome FROM market_fetch_attempts WHERE task_id=?", (second_id,)
        )
        self.assertEqual({row["outcome"] for row in outcomes}, {"复用刚取得的行情"})

    def test_reviewed_etf_cache_keeps_downgraded_identity_after_restart(self):
        bars = FakeMarketProvider().fetch("2.932000")
        seeding = MarketDataPipeline(self.db, FakeMarketProvider())
        seeding._save_bars("tencent_etf_proxy", "2.932000", bars)
        pipeline = MarketDataPipeline(self.db, FakeMarketProvider(fail=True))
        universe = self.db.rows("SELECT * FROM universe WHERE index_code='U010'")
        rows = pipeline.populate_metrics("etf-cache-test", universe, require_breadth=False)
        self.assertEqual(rows[0]["cache_hit"], 1)
        self.assertEqual(rows[0]["data_source"], "tencent_etf_proxy")
        self.assertEqual(rows[0]["proxy_code"], "563300")
        self.assertEqual(rows[0]["mapping_kind"], "ETF代理")
        self.assertIn("不是目标指数点位", rows[0]["warnings_json"])

    def test_async_official_publication_dates_are_aligned_to_previous_session(self):
        clock = lambda: datetime(2026, 8, 11, 17, 59)
        universe = self.db.rows("SELECT * FROM universe WHERE index_code IN ('U001','U002') ORDER BY index_code")
        security_ids = self.db.rows(
            "SELECT universe_id,security_id FROM proxy_mappings WHERE universe_id IN ('U001','U002')"
        )
        first_security = security_ids[0]["security_id"]
        provider = FixedCutoffMarketProvider(
            "2026-08-11", {first_security: "2026-08-10"},
        )
        pipeline = MarketDataPipeline(self.db, provider, clock=clock)
        rows = pipeline.populate_metrics("mixed-cutoff", universe, require_breadth=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["data_cutoff"] for row in rows}, {"2026-08-10"})
        self.assertEqual({row["expected_data_cutoff"] for row in rows}, {"2026-08-11"})
        self.assertEqual({row["freshness_state"] for row in rows}, {"stale"})
        self.assertTrue(all("已统一裁齐" in row["warnings_json"] for row in rows))

    def test_uniform_previous_session_completes_market_test_without_api(self):
        ensure_strict_scan_items(self.db, 10)
        clock = lambda: datetime(2026, 8, 11, 17, 59)
        pipeline = MarketDataPipeline(
            self.db, FixedCutoffMarketProvider("2026-08-10"), clock=clock,
        )
        orchestrator = ResearchOrchestrator(self.db, market_pipeline=pipeline)
        task_id = orchestrator.create_job("20项行情测试")
        result = orchestrator.run(task_id)
        self.assertEqual(result.status, "行情测试完成")
        self.assertEqual(result.completed_items, 10)
        self.assertIn("官网仍在分批发布", result.message)
        self.assertIn("不可生成买入金额", result.message)
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 0,
        )
        states = self.db.rows(
            "SELECT DISTINCT freshness_state FROM sector_metrics WHERE task_id=?", (task_id,),
        )
        self.assertEqual({row["freshness_state"] for row in states}, {"stale"})

    def test_more_than_one_session_old_still_fails_atomically(self):
        clock = lambda: datetime(2026, 8, 11, 17, 59)
        universe = self.db.rows("SELECT * FROM universe WHERE index_code IN ('U001','U002') ORDER BY index_code")
        pipeline = MarketDataPipeline(
            self.db, FixedCutoffMarketProvider("2026-08-07"), clock=clock,
        )
        with self.assertRaises(MarketDataError) as caught:
            pipeline.populate_metrics("too-old-cutoff", universe, require_breadth=False)
        self.assertIn("落后超过一交易日", str(caught.exception))
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM sector_metrics WHERE task_id='too-old-cutoff'")["n"], 0)

    def test_future_cutoff_still_fails_atomically(self):
        clock = lambda: datetime(2026, 8, 11, 17, 59)
        universe = self.db.rows("SELECT * FROM universe WHERE index_code IN ('U001','U002') ORDER BY index_code")
        pipeline = MarketDataPipeline(
            self.db, FixedCutoffMarketProvider("2026-08-12"), clock=clock,
        )
        with self.assertRaises(MarketDataError) as caught:
            pipeline.populate_metrics("future-cutoff", universe, require_breadth=False)
        self.assertIn("未来交易日", str(caught.exception))
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM sector_metrics WHERE task_id='future-cutoff'")["n"], 0)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        ensure_strict_scan_items(self.db, 10)
        self.expected_universe = PlatformCatalogService(self.db).radar_universe()
        self.task_id = ResearchOrchestrator(self.db).create_job("完整扫描")
        self.db.execute(
            "UPDATE analysis_jobs SET status='已完成',stage='完成',completed_items=total_items "
            "WHERE task_id=?",
            (self.task_id,),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def valid_payload(self):
        universe = self.expected_universe
        policy = ResearchOrchestrator(self.db)._current_ranking_policy()
        counts = {}
        for row in universe:
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        sources = [
            {"id": "S1", "title": "指数公司A", "url": "https://a.example.com/1", "retrieved_at": "2026-08-05", "supports": "行情"},
            {"id": "S2", "title": "指数公司B", "url": "https://b.example.org/2", "retrieved_at": "2026-08-05", "supports": "成分"},
            {"id": "S3", "title": "基金公司", "url": "https://c.example.net/3", "retrieved_at": "2026-08-05", "supports": "基金"},
            {"id": "S4", "title": "公告", "url": "https://a.example.com/4", "retrieved_at": "2026-08-05", "supports": "催化"},
            {"id": "S5", "title": "交易所", "url": "https://b.example.org/5", "retrieved_at": "2026-08-05", "supports": "市场"},
        ]
        return {
            "protocol_version": "3.0", "task_kind": "night", "task_id": self.task_id, "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now()).isoformat() + " 15:00 +08:00",
            "ranking_policy_version": policy["version"], "ranking_policy": policy,
            "market_regime": "轮动震荡",
            "universe_total": len(universe), "analyzed_total": len(universe),
            "coverage_by_category": [{"category": k, "expected": v, "analyzed": v} for k, v in counts.items()],
            "research_method": "统一使用本地行情表计算五周期收益，并由Flash联网检索催化与风险。",
            "tool_calls_summary": [{"tool": "local_metrics", "purpose": "计算", "items": len(universe)}],
            "audit": [{
                "id": row["index_code"], "name": row["index_name"], "category": row["category"],
                "proxy_code": "TEST", "proxy_name": row["index_name"], "returns_pct": {"d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5},
                "volume_signal": "正常", "breadth_signal": "扩散", "position_20d": "中位", "crowding": "中",
                "catalyst": "测试", "status": "ANALYZED", "source_ids": ["S1"], "note": "",
                "local_rank": index, "pool_route": "趋势确认·不过热",
                "research_tilt": "有点推荐买", "timing_status": "可以买", "execution_status": "可执行",
                "trend_score": 75, "entry_score": 75, "crowding_risk_score": 25,
                "data_confidence": 90, "opportunity_score": 75, "recommendation_score": 64,
                "score_components": {"policy_version": policy["version"], "freshness_ok": True},
            } for index,row in enumerate(universe,start=1)],
            "market_summary": "测试", "candidates": [], "avoid_list": [], "fund_updates": [],
            "questions_for_intraday": ["涨跌幅"], "risk_flags": [], "sources": sources,
        }

    def wrap(self, payload):
        return f"报告正文\n{START_MARKER}\n{json.dumps(payload, ensure_ascii=False)}\n{END_MARKER}"

    def test_valid_result_imports_every_leaf_csi_audit_row(self):
        payload = self.valid_payload(); text = self.wrap(payload)
        parsed = parse_result(text, set(), 8000, self.expected_universe)
        self.assertTrue(parsed.ok, parsed.errors[:5])
        import_radar_payload(self.db, parsed.payload, text, parsed.warnings)
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM radar_audit")["n"],
            len(self.expected_universe),
        )

    def test_action_payload_limits_top_ten_and_forces_catalog_risk_clusters(self):
        checked_at = local_now().isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at=?,verified_at=?",
            (checked_at, checked_at),
        )
        self.expected_universe = PlatformCatalogService(self.db).radar_universe()
        payload = self.valid_payload(); text = self.wrap(payload)
        import_radar_payload(self.db, payload, text, [])
        holdings = self.db.rows("SELECT fund_code,sleeve FROM holdings WHERE active=1 AND counts_toward_active_capital=1 ORDER BY fund_code")
        ordered = sorted(self.expected_universe, key=lambda row: int(row["has_buyable_fund"]), reverse=True)
        self.db.executemany(
            """INSERT INTO sector_metrics(
                 task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                 volume_signal,breadth_signal,position_20d,data_source,is_simulated,
                 base_score,score_components_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    payload["task_id"], row["index_code"], row["index_code"], row["index_name"],
                    1, 2, 3, 4, 5, "正常", "扩散", "中位", "fixture", 0,
                    round(64 - index * 0.2, 3), json.dumps({
                        "fixture_order": index,
                        "policy_version": payload["ranking_policy_version"],
                        "freshness_ok": True,
                    }),
                )
                for index, row in enumerate(ordered)
            ],
        )
        decision = {
            "action_summary": "测试行动", "no_buy_reason": "",
            "candidates": [{
                "universe_id": row["index_code"], "score_adjustment": 0,
                "allocation_pct": 25 if index == 1 else (10 if index == 2 else 0),
                "thesis": f"方向{index}", "catalyst": "测试", "crowding": "中", "risk": "测试",
                "buy_conditions": "测试", "abandon_conditions": "测试",
                "correlation_bucket": "行业", "fund_code": row["fund_code"], "fund_name": "测试基金",
            } for index, row in enumerate(ordered[:10], start=1)],
            "holding_actions": [
                {"fund_code": row["fund_code"], "action": "减仓" if index == 0 else "持有", "sell_pct": 50 if index == 0 else 0, "reason": "测试", "risk": "测试"}
                for index, row in enumerate(holdings)
            ],
            "questions_for_intraday": [], "risk_flags": [],
        }
        action = ResearchOrchestrator(self.db)._normalize_action_payload(payload["task_id"], decision)
        self.assertEqual(len(action["candidates"]), 10)
        self.assertEqual(action["candidates"][0]["suggested_amount"], 2000)
        self.assertEqual(action["candidates"][0]["status"], "有点推荐买")
        self.assertEqual(
            action["candidates"][0]["correlation_bucket"],
            ordered[0]["allocation_cluster"],
        )
        self.assertNotEqual(action["candidates"][0]["correlation_bucket"], "行业")
        self.assertEqual(len(action["holding_actions"]), len(holdings))
        save_action_advice(self.db, payload["task_id"], action)
        run = self.db.row("SELECT * FROM radar_runs WHERE task_id=?", (payload["task_id"],))
        action_json = json.loads(run["action_json"])
        snapshot = action_json["capital_snapshot"]
        current_context = action_capital_context(run, {
            "active_limit": snapshot["limit"],
            "active_value": snapshot["used"],
            "active_available": snapshot["available"],
        })
        self.assertFalse(current_context["stale"])
        changed_context = action_capital_context(run, {
            "active_limit": snapshot["limit"],
            "active_value": snapshot["used"] + 2000,
            "active_available": snapshot["available"] - 2000,
        })
        self.assertTrue(changed_context["stale"])
        self.assertEqual(self.db.row("SELECT COUNT(*) n FROM candidates WHERE task_id=?", (payload["task_id"],))["n"], 10)
        recommendations = holding_recommendations_for_run(self.db, run)
        self.assertEqual(len(recommendations), len(holdings))
        self.assertAlmostEqual(recommendations[0]["estimated_sell_amount"], recommendations[0]["market_value"] * .5, places=2)

    def test_partial_universe_fails(self):
        payload = self.valid_payload(); payload["analyzed_total"] = 5
        parsed = parse_result(self.wrap(payload), set(), 8000, self.expected_universe)
        self.assertFalse(parsed.ok)
        self.assertTrue(any("覆盖不足" in error for error in parsed.errors))

    def test_missing_web_audit_fails(self):
        payload = self.valid_payload(); payload["tool_calls_summary"] = []
        self.assertFalse(parse_result(self.wrap(payload), set(), 8000, self.expected_universe).ok)

    def test_task_package_is_self_contained_and_v2(self):
        text, _ = build_task_package(self.db, "night")
        self.assertIn("无状态任务包", text)
        self.assertIn("current_holdings", text)
        self.assertIn(START_MARKER, text)
        self.assertIn(UNIVERSE_VERSION, text)


class DeepSeekClientTests(unittest.TestCase):
    def test_api_configuration_only_accepts_official_host_and_current_models(self):
        self.assertEqual(DeepSeekClient.validate_base_url("https://api.deepseek.com/"), "https://api.deepseek.com")
        self.assertEqual(DeepSeekClient.validate_base_url("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1")
        for bad in (
            "http://api.deepseek.com", "https://example.com", "https://api.deepseek.com.evil.test",
            "https://user:pass@api.deepseek.com", "https://api.deepseek.com/beta",
        ):
            with self.assertRaises(DeepSeekError, msg=bad):
                DeepSeekClient.validate_base_url(bad)
        self.assertEqual(
            DeepSeekClient.validate_models("deepseek-v4-flash", "deepseek-v4-pro"),
            ("deepseek-v4-flash", "deepseek-v4-pro"),
        )
        with self.assertRaises(DeepSeekError):
            DeepSeekClient.validate_models("deepseek-v4-pro", "deepseek-v4-pro")

    def test_connection_preflight_checks_models_and_balance_without_post(self):
        calls = []
        def requester(request, _timeout):
            calls.append((request.get_method(), request.full_url))
            if request.full_url.endswith("/models"):
                return json.dumps({"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]}).encode()
            return json.dumps({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "9.81"}]}).encode()
        report = DeepSeekClient("test-key", requester=requester).connection_report(
            "deepseek-v4-flash", "deepseek-v4-pro"
        )
        self.assertTrue(report.balance_available)
        self.assertEqual(report.balance_text(), "9.81 CNY")
        self.assertEqual([method for method, _ in calls], ["GET", "GET"])
        self.assertTrue(calls[0][1].endswith("/models"))
        self.assertTrue(calls[1][1].endswith("/user/balance"))

    def test_windows_curl_is_selected_before_get_and_post_without_key_leakage(self):
        secret = "sk-test-never-on-command-line"
        responses = iter([
            b'{"data":[{"id":"deepseek-v4-flash"},{"id":"deepseek-v4-pro"}]}\n__SECTOR_RADAR_HTTP_STATUS__:200',
            b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"9.63"}]}\n__SECTOR_RADAR_HTTP_STATUS__:200',
            b'{"status":"completed"}\n__SECTOR_RADAR_HTTP_STATUS__:200',
        ])
        invocations = []
        with tempfile.TemporaryDirectory() as folder:
            runtime_dir = Path(folder)

            def native_runner(command, **kwargs):
                invocations.append((command, kwargs))
                self.assertNotIn(secret, " ".join(command))
                self.assertNotIn("env", kwargs)
                self.assertIn(f"Authorization: Bearer {secret}".encode(), kwargs["input"])
                self.assertNotIn("--insecure", command)
                self.assertEqual(command[command.index("--retry") + 1], "0")
                body_files = list(runtime_dir.glob("deepseek-request-*.json.tmp"))
                if len(invocations) < 3:
                    self.assertEqual(body_files, [])
                else:
                    self.assertEqual(len(body_files), 1)
                    self.assertIn(b'"input": "x"', body_files[0].read_bytes())
                return SimpleNamespace(returncode=0, stdout=next(responses), stderr=b"")

            with patch("app.deepseek.subprocess.run", side_effect=native_runner):
                client = DeepSeekClient(secret, curl_path=r"C:\Windows\System32\curl.exe", runtime_dir=runtime_dir)
                report = client.connection_report("deepseek-v4-flash", "deepseek-v4-pro")
                result = client._call("POST", "/responses", {"model": "deepseek-v4-flash", "input": "x"})

            self.assertEqual(list(runtime_dir.glob("deepseek-request-*.json.tmp")), [])
            self.assertEqual(len(invocations), 3)
            self.assertEqual(report.transport, "Windows系统curl安全网络通道（Schannel）")
            self.assertEqual(result["status"], "completed")
            for _command, kwargs in invocations:
                self.assertEqual(kwargs["creationflags"], getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))

    def test_windows_post_transport_failure_is_never_automatically_retried(self):
        failed = SimpleNamespace(returncode=35, stdout=b"", stderr="安全连接被远端关闭".encode("gb18030"))
        with tempfile.TemporaryDirectory() as folder:
            with patch("app.deepseek.subprocess.run", return_value=failed) as runner:
                client = DeepSeekClient("sk-test", curl_path=r"C:\Windows\System32\curl.exe", runtime_dir=Path(folder))
                with self.assertRaisesRegex(DeepSeekError, "安全连接被远端关闭.*不会自动重发") as caught:
                    client._call("POST", "/responses", {"model": "deepseek-v4-flash"})
            runner.assert_called_once()
            self.assertNotIn("�", str(caught.exception))
            self.assertEqual(list(Path(folder).glob("deepseek-request-*.json.tmp")), [])

    def test_windows_get_transport_failure_is_reported_as_no_charge_preflight(self):
        failed = SimpleNamespace(returncode=35, stdout=b"", stderr=b"TLS connection closed unexpectedly")
        with patch("app.deepseek.subprocess.run", return_value=failed) as runner:
            client = DeepSeekClient("sk-test", curl_path=r"C:\Windows\System32\curl.exe")
            with self.assertRaisesRegex(DeepSeekError, "尚未发送计费请求"):
                client._call("GET", "/models")
        runner.assert_called_once()

    def test_cached_tokens_use_cache_hit_price_and_flash_payload_matches_official_shape(self):
        captured = {}
        response = {
            "status": "completed", "model": "deepseek-v4-flash",
            "output": [{"type": "web_search_call"}, {"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800}, "output_tokens": 100},
        }
        def requester(request, _timeout):
            captured.update(json.loads(request.data.decode()))
            return json.dumps(response).encode()
        result = DeepSeekClient("test-key", requester=requester).flash_search_json(
            instructions="return json", prompt="x", schema_name="x", schema={"type": "object"},
            input_price_per_million=10, cached_input_price_per_million=1, output_price_per_million=20,
        )
        self.assertAlmostEqual(result.estimated_cost_cny, (200 * 10 + 800 * 1 + 100 * 20) / 1_000_000)
        self.assertNotIn("strict", captured["text"]["format"])
        self.assertEqual(captured["tool_choice"], "auto")
        self.assertNotIn("max_tool_calls", captured)
        self.assertEqual(captured["user"], "sector-radar-local")

    def test_pro_nonstop_finish_reason_is_returned_for_audit_before_rejection(self):
        response = {
            "id": "chat-cut", "model": "deepseek-v4-pro",
            "choices": [{"finish_reason": "length", "message": {"content": "{\"partial\":"}}],
            "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "completion_tokens": 500,
                      "completion_tokens_details": {"reasoning_tokens": 300}},
        }
        result = DeepSeekClient("test-key", requester=lambda *_: json.dumps(response).encode()).pro_json(
            system="return json", prompt="json", input_price_per_million=10,
            cached_input_price_per_million=1, output_price_per_million=20,
        )
        self.assertEqual(result.finish_reason, "length")
        self.assertAlmostEqual(result.estimated_cost_cny, (400 * 10 + 600 * 1 + 500 * 20) / 1_000_000)
        with self.assertRaisesRegex(DeepSeekError, "长度或上下文上限"):
            result.json()

    def test_pro_stream_ignores_keepalive_and_reassembles_content_usage(self):
        captured = {}
        stream = b"\n".join([
            b": keep-alive",
            b'data: {"id":"chat-stream","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"think"},"finish_reason":null}],"usage":null}',
            b'data: {"id":"chat-stream","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"content":"{\\"ok\\":"},"finish_reason":null}],"usage":null}',
            b'data: {"id":"chat-stream","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"content":"true}"},"finish_reason":"stop"}],"usage":null}',
            b'data: {"id":"chat-stream","model":"deepseek-v4-pro","choices":[],"usage":{"prompt_tokens":1000,"prompt_cache_hit_tokens":600,"completion_tokens":500,"completion_tokens_details":{"reasoning_tokens":300}}}',
            b"data: [DONE]",
            b"",
        ])
        def requester(request, _timeout):
            captured.update(json.loads(request.data.decode()))
            return stream
        result = DeepSeekClient("test-key", requester=requester).pro_json(
            system="return json", prompt="json", input_price_per_million=10,
            cached_input_price_per_million=1, output_price_per_million=20,
        )
        self.assertTrue(captured["stream"])
        self.assertEqual(captured["stream_options"], {"include_usage": True})
        self.assertEqual(captured["thinking"], {"type": "enabled"})
        self.assertEqual(captured["reasoning_effort"], "max")
        self.assertNotIn("reasoning_effort", captured["thinking"])
        self.assertEqual(result.json(), {"ok": True})
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.reasoning_tokens, 300)
        self.assertTrue(result.raw["streamed"])
        self.assertEqual(result.raw["stream_chunk_count"], 4)
        self.assertAlmostEqual(result.estimated_cost_cny, (400 * 10 + 600 * 1 + 500 * 20) / 1_000_000)

    def test_pro_stream_without_done_is_rejected_without_automatic_retry(self):
        partial = b'data: {"id":"x","choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n'
        client = DeepSeekClient("test-key", requester=lambda *_: partial)
        with self.assertRaisesRegex(DeepSeekError, r"\[DONE\].*不会自动重发"):
            client.pro_json(system="return json", prompt="json")

    def test_windows_pro_stream_uses_progress_monitor_without_total_max_time(self):
        secret = "sk-stream-secret"
        stream = b"\n".join([
            b": keep-alive",
            b'data: {"id":"stream-win","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"content":"{}"},"finish_reason":"stop"}],"usage":null}',
            b'data: {"id":"stream-win","model":"deepseek-v4-pro","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
            b"data: [DONE]",
            b"",
        ])
        invocations = []

        class InputSink:
            def __init__(self):
                self.value = b""
            def write(self, value):
                self.value += value
            def close(self):
                pass

        class FakeProcess:
            def __init__(self, command, kwargs):
                self.command = command
                self.kwargs = kwargs
                self.input_sink = InputSink()
                self.stdin = self.input_sink
                self.returncode = 0
                Path(command[command.index("--output") + 1]).write_bytes(stream)
            def poll(self):
                return self.returncode
            def communicate(self, timeout=None):
                return (b"\n__SECTOR_RADAR_HTTP_STATUS__:200", b"")
            def kill(self):
                self.returncode = -9

        def fake_popen(command, **kwargs):
            process = FakeProcess(command, kwargs)
            invocations.append(process)
            return process

        with tempfile.TemporaryDirectory() as folder, patch("app.deepseek.subprocess.Popen", side_effect=fake_popen):
            runtime_dir = Path(folder)
            result = DeepSeekClient(
                secret, curl_path=r"C:\Windows\System32\curl.exe", runtime_dir=runtime_dir,
            ).pro_json(system="return json", prompt="json")
            self.assertEqual(list(runtime_dir.glob("*.tmp")), [])
        self.assertEqual(result.json(), {})
        self.assertEqual(len(invocations), 1)
        command = invocations[0].command
        self.assertNotIn("--max-time", command)
        self.assertIn("--no-buffer", command)
        self.assertNotIn(secret, " ".join(command))
        self.assertIn(f"Authorization: Bearer {secret}".encode(), invocations[0].input_sink.value)

    def test_pro_structure_mode_is_a_real_but_noninvestment_api_check(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "pro-smoke.sqlite3")
        class ProSmokeClient:
            calls = []
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ({"currency":"CNY","total_balance":"10"},))
            def pro_json(self, **kwargs):
                self.calls.append(kwargs)
                return ModelResult(
                    text=json.dumps({"ok": True, "checks": ["chat_completions", "thinking", "json_output"]}),
                    raw={"id":"pro-smoke", "model":"deepseek-v4-pro", "choices":[{"finish_reason":"stop"}]},
                    model="deepseek-v4-pro", input_tokens=50, output_tokens=30, estimated_cost_cny=.001,
                    response_kind="chat", finish_reason="stop",
                )
        client = ProSmokeClient(); orchestrator = ResearchOrchestrator(db, client)
        task_id = orchestrator.create_job("Pro结构测试")
        result = orchestrator.run(task_id)
        self.assertEqual(result.status, "测试完成")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["max_tokens"], 2000)
        self.assertEqual(db.row("SELECT COUNT(*) n FROM radar_runs")["n"], 0)
        self.assertEqual(db.row("SELECT finish_reason FROM model_calls WHERE task_id=?", (task_id,))["finish_reason"], "stop")

    def test_flash_requires_and_counts_web_search(self):
        response = {
            "model": "deepseek-v4-flash", "status": "completed", "output": [
                {"type": "web_search_call", "id": "w1"},
                {"type": "message", "content": [{"type": "output_text", "text": '{"items":[]}'}]},
            ], "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        requester = lambda _request, _timeout: json.dumps(response).encode()
        client = DeepSeekClient("test-key", requester=requester)
        result = client.flash_search_json(instructions="x", prompt="y", schema_name="z", schema={"type": "object"})
        self.assertEqual(result.web_search_calls, 1)
        self.assertEqual(result.json(), {"items": []})

    def test_flash_uses_last_completed_message_and_allows_partial_search_failure(self):
        expected = {"items": [{"id": "C1"}]}
        raw = {
            "id": "multi-message", "model": "deepseek-v4-flash", "status": "completed",
            "output": [
                {"type": "reasoning", "status": "completed"},
                {"type": "web_search_call", "id": "w-failed", "status": "failed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": "I'll provide the queries as a proper array."},
                ]},
                {"type": "web_search_call", "id": "w-ok", "status": "completed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": json.dumps(expected)},
                ]},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        result = DeepSeekClient._responses_result(
            raw, model="deepseek-v4-flash", input_price_per_million=1,
            cached_input_price_per_million=.02, output_price_per_million=2,
        )
        self.assertEqual(result.text, json.dumps(expected))
        self.assertEqual(result.json(), expected)
        self.assertEqual(result.web_search_calls, 2)

    def test_flash_rejects_when_every_explicit_search_failed(self):
        raw = {
            "id": "all-searches-failed", "model": "deepseek-v4-flash", "status": "completed",
            "output": [
                {"type": "web_search_call", "id": "w1", "status": "failed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": '{"items":[]}'},
                ]},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        result = DeepSeekClient._responses_result(
            raw, model="deepseek-v4-flash", input_price_per_million=1,
            cached_input_price_per_million=.02, output_price_per_million=2,
        )
        with self.assertRaisesRegex(DeepSeekError, "联网搜索全部失败"):
            result.json()

    def test_recovery_prefers_exact_last_search_message_over_bad_finalize(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "recovery.sqlite3")
        ensure_strict_scan_items(db, 10)
        orchestrator = ResearchOrchestrator(db)
        task_id = orchestrator.create_job("20方向测试")
        batch_ids = ["C931866", "C931752"]
        valid_data = {"items": [{"id": item_id} for item_id in batch_ids]}
        search_raw = {
            "id": "search", "status": "completed", "output": [
                {"type": "web_search_call", "status": "failed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": "preliminary text"},
                ]},
                {"type": "web_search_call", "status": "completed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": json.dumps(valid_data)},
                ]},
            ],
        }
        finalize_raw = {
            "id": "finalize", "status": "completed", "output": [
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": json.dumps({"items": [
                        {"id": "wrong"}, {"id": "wrong"}, {"id": "extra"},
                    ]})},
                ]},
            ],
        }
        orchestrator._record_model_call(
            task_id, "Flash联网检索",
            ModelResult(
                text="unused", raw=search_raw, model="deepseek-v4-flash",
                input_tokens=0, output_tokens=0, web_search_calls=2, response_kind="responses",
            ),
            context={"phase": "search", "batch_ids": batch_ids},
        )
        orchestrator._record_model_call(
            task_id, "Flash自动无搜索收尾",
            ModelResult(
                text="unused", raw=finalize_raw, model="deepseek-v4-flash",
                input_tokens=0, output_tokens=0, response_kind="responses",
            ),
            context={"phase": "finalize", "batch_ids": batch_ids},
        )
        recovery = orchestrator._flash_batch_recovery(task_id, batch_ids)
        self.assertEqual(recovery["phase"], "local_repair")
        self.assertEqual([item["id"] for item in recovery["data"]["items"]], batch_ids)
        self.assertEqual(recovery["failed_web_search_calls"], 1)

    def test_flash_finalize_replays_search_calls_with_tools_disabled(self):
        captured = {}
        response = {
            "id": "final", "model": "deepseek-v4-flash", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"items":[]}'}]}],
            "usage": {"input_tokens": 900, "output_tokens": 20},
        }
        def requester(request, _timeout):
            captured.update(json.loads(request.data.decode()))
            return json.dumps(response).encode()
        previous = {
            "id": "search-only", "status": "completed",
            "output": [
                {"type": "web_search_call", "id": "ws1", "status": "completed", "action": {"type": "search", "query": "A股"}},
                {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": "{bad json"}]},
            ],
        }
        result = DeepSeekClient("test-key", requester=requester).flash_finalize_json(
            instructions="json", prompt="research", previous_raw=previous,
            schema_name="x", schema={"type": "object"},
        )
        self.assertEqual(result.json(), {"items": []})
        self.assertEqual(captured["tool_choice"], "none")
        self.assertEqual(captured["reasoning"], {"effort": "none"})
        self.assertTrue(any(item.get("type") == "web_search_call" for item in captured["input"] if isinstance(item, dict)))
        self.assertFalse(any(item.get("type") == "message" for item in captured["input"] if isinstance(item, dict)))
        self.assertNotIn("previous_response_id", captured)

    def test_failed_finalize_bare_quotes_are_repaired_locally_without_api(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "repair.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        today = local_now().date().isoformat()
        invalid = (
            '{"items":[{"id":"U001","crowding":"提及"一利五率"，拥挤度正常",'
            '"catalyst":"从"国企重估"到"特色稳市"", "risk":"正常",'
            '"sources":[{"id":"S1","title":"从"国企重估"到"特色稳市"",'
            f'"url":"https://example.com/a","retrieved_at":"{today}","supports":"支持判断"}}]}}]}}'
        )
        search_raw = {
            "id": "search-bad-quotes", "status": "completed", "model": "deepseek-v4-flash",
            "output": [
                {"type": "web_search_call", "id": "ws1", "status": "completed"},
                {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": invalid}]},
            ],
        }
        finalize_raw = {
            "id": "finalize-same-bad-quotes", "status": "completed", "model": "deepseek-v4-flash",
            "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": invalid}]}],
        }
        orchestrator = ResearchOrchestrator(db)
        orchestrator._record_model_call(
            task_id, "Flash联网检索",
            ModelResult(invalid, search_raw, "deepseek-v4-flash", 100, 20, web_search_calls=1, estimated_cost_cny=.01),
            context={"phase": "search", "batch_ids": ["U001"]},
        )
        orchestrator._record_model_call(
            task_id, "Flash自动无搜索收尾",
            ModelResult(invalid, finalize_raw, "deepseek-v4-flash", 100, 20, estimated_cost_cny=.01),
            context={"phase": "finalize", "batch_ids": ["U001"], "parent_response_id": "search-bad-quotes"},
        )

        class NoApiClient:
            def flash_search_json(self, **_kwargs):
                raise AssertionError("local repair must not search")
            def flash_finalize_json(self, **_kwargs):
                raise AssertionError("local repair must not finalize again")

        universe = [db.row("SELECT * FROM universe WHERE index_code='U001'")]
        metrics = [{
            "universe_id": "U001", "proxy_code": "1.000001", "proxy_name": "测试代理",
            "d1": 1, "d3": 1, "d5": 1, "d10": 1, "d20": 1,
            "volume_signal": "正常", "breadth_signal": "正常", "position_20d": "中位",
        }]
        rows = ResearchOrchestrator(db, NoApiClient())._run_flash_batches(
            task_id, universe, metrics, 2.0, None,
        )
        self.assertEqual(len(rows), 1)
        self.assertIn('"一利五率"', rows[0]["crowding"])
        self.assertIn('"国企重估"', rows[0]["catalyst"])
        self.assertEqual(rows[0]["crowding_level"], 70.0)
        self.assertEqual(rows[0]["catalyst_strength"], 20.0)
        self.assertEqual(
            db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 2,
        )
        self.assertIsNotNone(db.row(
            "SELECT 1 FROM analysis_events WHERE task_id=? AND stage='Flash本地JSON修复'", (task_id,),
        ))

    def test_prefixed_fenced_bare_quotes_and_extra_sources_resume_without_api(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "fenced-repair.sqlite3")
        scan_items = ensure_strict_scan_items(db, 1)
        target = scan_items[0]
        target_id = target["index_code"]
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        today = local_now().date().isoformat()
        sources = [{
            "id": f"S{number}",
            "title": f"来源{number}",
            "url": f"https://example.com/source-{number}",
            "retrieved_at": today,
            "supports": "支持本项目判断",
        } for number in range(1, 4)]
        search_payload = json.dumps({"items": [{
            "id": target_id, "crowding": "提及自动驾驶强标，拥挤度正常",
            "crowding_level": 55, "catalyst": "政策催化明确",
            "catalyst_strength": 70, "risk": "注意高位波动",
            "sources": sources[:2],
        }]}, ensure_ascii=False)
        search_payload = search_payload.replace("自动驾驶强标", '自动驾驶"强标"')
        search_text = (
            "Based on my research, I have sufficient evidence.\n"
            "Compiling the final JSON.\n\n```json\n"
            f"{search_payload}\n```"
        )
        finalize_text = json.dumps({"items": [{
            "id": target_id, "crowding": "拥挤度正常", "crowding_level": 55,
            "catalyst": "政策催化明确", "catalyst_strength": 70,
            "risk": "注意高位波动", "sources": sources,
        }]}, ensure_ascii=False)
        search_raw = {
            "id": "search-prefixed-fenced", "status": "completed",
            "model": "deepseek-v4-flash", "output": [
                {"type": "web_search_call", "id": "ws1", "status": "completed"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": search_text},
                ]},
            ],
        }
        finalize_raw = {
            "id": "finalize-three-sources", "status": "completed",
            "model": "deepseek-v4-flash", "output": [{
                "type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": finalize_text},
                ],
            }],
        }
        orchestrator = ResearchOrchestrator(db)
        orchestrator._record_model_call(
            task_id, "Flash联网检索",
            ModelResult(search_text, search_raw, "deepseek-v4-flash", 100, 20,
                        web_search_calls=1, estimated_cost_cny=.01),
            context={"phase": "search", "batch_ids": [target_id]},
        )
        orchestrator._record_model_call(
            task_id, "Flash自动无搜索收尾",
            ModelResult(finalize_text, finalize_raw, "deepseek-v4-flash", 100, 20,
                        estimated_cost_cny=.01),
            context={"phase": "finalize", "batch_ids": [target_id],
                     "parent_response_id": "search-prefixed-fenced"},
        )
        recovery = orchestrator._flash_batch_recovery(task_id, [target_id])
        self.assertEqual(recovery["phase"], "local_repair")
        events_before = db.row(
            "SELECT COUNT(*) n FROM analysis_events WHERE task_id=?", (task_id,),
        )["n"]
        resume_plan = orchestrator.flash_resume_plan(task_id)
        self.assertEqual(resume_plan["already_saved_items"], 0)
        self.assertEqual(resume_plan["local_repair_items"], 1, resume_plan)
        self.assertEqual(resume_plan["local_repair_batches"], 1)
        self.assertEqual(resume_plan["finalize_only_items"], 0)
        self.assertEqual(resume_plan["new_search_items"], 0)
        self.assertEqual(resume_plan["blocked_items"], 0)
        self.assertTrue(resume_plan["same_research_day"])
        self.assertEqual(
            db.row("SELECT COUNT(*) n FROM analysis_events WHERE task_id=?", (task_id,))["n"],
            events_before,
        )

        class NoApiClient:
            def flash_search_json(self, **_kwargs):
                raise AssertionError("saved response must be reused")
            def flash_finalize_json(self, **_kwargs):
                raise AssertionError("saved response must not be finalized again")

        universe = [target]
        metrics = [{
            "universe_id": target_id, "proxy_code": "1.000001", "proxy_name": "测试代理",
            "d1": 1, "d3": 1, "d5": 1, "d10": 1, "d20": 1,
            "volume_signal": "正常", "breadth_signal": "正常", "position_20d": "中位",
        }]
        rows = ResearchOrchestrator(db, NoApiClient())._run_flash_batches(
            task_id, universe, metrics, 2.0, None,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(json.loads(rows[0]["source_json"])), 2)
        self.assertEqual(
            db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 2,
        )
        self.assertIsNotNone(db.row(
            "SELECT 1 FROM analysis_events WHERE task_id=? AND stage='Flash本地来源精简'",
            (task_id,),
        ))

    def test_failed_finalize_missing_narrative_field_resumes_locally_without_api(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "missing-field.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        today = local_now().date().isoformat()
        search_text = json.dumps({"items": [{
            "id": "U001", "crowing": "短线涨幅较大，拥挤度偏高",
            "catalyst": "近期催化已核验", "risk": "存在回撤风险",
            "sources": [{
                "id": "S1", "title": "公开来源", "url": "https://example.com/u001",
                "retrieved_at": today, "supports": "支持催化与风险判断",
            }],
        }]}, ensure_ascii=False)
        finalize_text = json.dumps({"items": [{
            "id": "U001", "name": "测试板块", "returns_pct": {"d1": 1, "d5": 5},
        }]}, ensure_ascii=False)
        search_raw = {
            "id": "search-missing", "status": "completed", "model": "deepseek-v4-flash",
            "output": [
                {"type": "web_search_call", "id": "ws1", "status": "completed"},
                {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": search_text}]},
            ],
        }
        finalize_raw = {
            "id": "finalize-missing", "status": "completed", "model": "deepseek-v4-flash",
            "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": finalize_text}]}],
        }
        orchestrator = ResearchOrchestrator(db)
        orchestrator._record_model_call(
            task_id, "Flash联网检索",
            ModelResult(search_text, search_raw, "deepseek-v4-flash", 100, 20, web_search_calls=1, estimated_cost_cny=.01),
            context={"phase": "search", "batch_ids": ["U001"]},
        )
        orchestrator._record_model_call(
            task_id, "Flash自动无搜索收尾",
            ModelResult(finalize_text, finalize_raw, "deepseek-v4-flash", 100, 20, estimated_cost_cny=.01),
            context={"phase": "finalize", "batch_ids": ["U001"], "parent_response_id": "search-missing"},
        )

        class NoApiClient:
            def flash_search_json(self, **_kwargs):
                raise AssertionError("missing text field must not trigger a new search")
            def flash_finalize_json(self, **_kwargs):
                raise AssertionError("completed finalize must be repaired locally")

        universe = [db.row("SELECT * FROM universe WHERE index_code='U001'")]
        metrics = [{
            "universe_id": "U001", "proxy_code": "1.000001", "proxy_name": "测试代理",
            "d1": 1, "d3": 1, "d5": 1, "d10": 1, "d20": 1,
            "volume_signal": "正常", "breadth_signal": "正常", "position_20d": "中位",
        }]
        rows = ResearchOrchestrator(db, NoApiClient())._run_flash_batches(task_id, universe, metrics, 2.0, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crowding"], "短线涨幅较大，拥挤度偏高")
        self.assertEqual(rows[0]["catalyst"], "近期催化已核验")
        self.assertEqual(rows[0]["crowding_level"], 70.0)
        self.assertEqual(rows[0]["catalyst_strength"], 20.0)
        self.assertEqual(db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"], 2)
        self.assertIsNotNone(db.row(
            "SELECT 1 FROM analysis_events WHERE task_id=? AND stage='Flash本地字段降级'", (task_id,),
        ))

    def test_flash_structured_levels_reject_invalid_values_and_keep_zero(self):
        today = local_now().date().isoformat()
        source = [{
            "id": "S1", "title": "公开来源", "url": "https://example.com/u001",
            "retrieved_at": today, "supports": "支持判断",
        }]
        base = {
            "id": "U001", "crowding": "不拥挤", "crowding_level": 0,
            "catalyst": "无催化", "catalyst_strength": 0,
            "risk": "常规风险", "sources": source,
        }
        clean = ResearchOrchestrator._validate_flash_items({"items": [base]}, {"U001"})
        self.assertEqual(clean[0]["crowding_level"], 0.0)
        self.assertEqual(clean[0]["catalyst_strength"], 0.0)
        for field, value in (
            ("crowding_level", None),
            ("crowding_level", True),
            ("crowding_level", 101),
            ("catalyst_strength", float("nan")),
            ("catalyst_strength", "80"),
        ):
            broken = dict(base)
            if value is None:
                broken.pop(field)
            else:
                broken[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(DeepSeekError):
                    ResearchOrchestrator._validate_flash_items({"items": [broken]}, {"U001"})

    def test_empty_json_reports_incomplete_status_tokens_and_cost(self):
        response = {
            "id": "resp-empty", "model": "deepseek-v4-flash", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"type": "web_search_call", "id": "w1"},
                {"type": "reasoning", "id": "r1"},
            ],
            "usage": {
                "input_tokens": 150000, "output_tokens": 7000,
                "output_tokens_details": {"reasoning_tokens": 7000},
            },
        }
        client = DeepSeekClient("test-key", requester=lambda _request, _timeout: json.dumps(response).encode())
        result = client.flash_search_json(instructions="json", prompt="x", schema_name="z", schema={"type": "object"})
        with self.assertRaises(DeepSeekError) as caught:
            result.json()
        message = str(caught.exception)
        self.assertIn("响应未完成", message)
        self.assertIn("status=incomplete", message)
        self.assertIn("reason=max_output_tokens", message)
        self.assertIn("reasoning_tokens=7000", message)
        self.assertIn("estimated_cost=", message)
        self.assertNotIn("第1行第1列", message)

    def test_failed_model_responses_and_inner_incomplete_items_remain_auditable(self):
        failed = {
            "id":"failed-r", "model":"deepseek-v4-flash", "status":"failed",
            "error":{"code":"upstream", "message":"temporary"}, "output":[],
            "usage":{"input_tokens":321, "output_tokens":9},
        }
        client = DeepSeekClient("test-key", requester=lambda *_: json.dumps(failed).encode())
        result = client.flash_search_json(instructions="json", prompt="x", schema_name="z", schema={"type":"object"})
        self.assertEqual(result.input_tokens, 321)
        self.assertGreater(result.estimated_cost_cny, 0)
        with self.assertRaisesRegex(DeepSeekError, "Flash响应失败"):
            result.json()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "failed.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("20方向测试")
        ResearchOrchestrator(db)._record_model_call(task_id, "Flash联网检索", result)
        row = db.row("SELECT response_status,input_tokens,estimated_cost_cny FROM model_calls WHERE task_id=?", (task_id,))
        self.assertEqual(row["response_status"], "failed")
        self.assertEqual(row["input_tokens"], 321)
        self.assertGreater(row["estimated_cost_cny"], 0)

        inner = ModelResult(
            text='{"items":[]}', raw={"status":"completed", "output":[{"type":"message", "status":"incomplete"}]},
            model="deepseek-v4-flash", input_tokens=10, output_tokens=2, response_kind="responses",
        )
        with self.assertRaisesRegex(DeepSeekError, "内部输出项未完成"):
            inner.json()

        pro_failed = {
            "id":"failed-chat", "model":"deepseek-v4-pro",
            "error":{"code":"resource", "message":"temporary"}, "choices":[],
            "usage":{"prompt_tokens":222, "completion_tokens":7},
        }
        pro = DeepSeekClient("test-key", requester=lambda *_: json.dumps(pro_failed).encode()).pro_json(
            system="json", prompt="x"
        )
        self.assertEqual(pro.input_tokens, 222)
        self.assertGreater(pro.estimated_cost_cny, 0)
        with self.assertRaisesRegex(DeepSeekError, "结束原因异常"):
            pro.json()

    def test_json_parser_accepts_markdown_fence_but_not_explanation(self):
        result = ModelResult(
            text='```json\n{"items": []}\n```', raw={"status": "completed"}, model="x",
            input_tokens=1, output_tokens=1,
        )
        self.assertEqual(result.json(), {"items": []})
        bad = ModelResult(
            text='说明如下：\n{"items": []}', raw={"status": "completed"}, model="x",
            input_tokens=1, output_tokens=1,
        )
        with self.assertRaises(DeepSeekError):
            bad.json()

    def test_flash_parser_accepts_one_prefixed_object_or_one_prefixed_fence(self):
        expected = {"items": [{"id": "C399807"}]}
        chinese = "基于检索结果，以下是证据汇总。\n\n" + json.dumps(expected, ensure_ascii=False)
        english_fence = (
            "Here is the requested structured result.\n"
            "```json\n" + json.dumps(expected, ensure_ascii=False) + "\n```"
        )

        self.assertEqual(ResearchOrchestrator._parse_flash_json_text(chinese), expected)
        self.assertEqual(ResearchOrchestrator._parse_flash_json_text(english_fence), expected)

    def test_flash_parser_removes_only_a_matching_empty_shadow_duplicate(self):
        complete = {
            "id": "C399807", "crowding": "拥挤度中等", "crowding_level": 55,
            "catalyst": "铁路设备更新", "catalyst_strength": 68, "risk": "投资不及预期",
            "sources": [{"id": "S1", "title": "来源", "url": "https://example.com/rail",
                         "retrieved_at": "2026-08-11", "supports": "支持判断"}],
        }
        shadow = {
            "id": "C399807", "crowding": "", "crowding_level": 55,
            "catalyst": "", "catalyst_strength": 68, "risk": "", "sources": [],
        }
        text = "检索已经完成。\n" + json.dumps({"items": [complete, shadow]}, ensure_ascii=False)

        parsed = ResearchOrchestrator._parse_flash_json_text(text)
        self.assertEqual(parsed, {"items": [complete]})
        self.assertEqual(
            [item["id"] for item in ResearchOrchestrator._validate_flash_items(parsed, {"C399807"})],
            ["C399807"],
        )

    def test_flash_parser_keeps_conflicting_duplicates_and_unknown_ids_for_strict_rejection(self):
        base = {
            "id": "C399807", "crowding": "中等", "crowding_level": 55,
            "catalyst": "催化一", "catalyst_strength": 68, "risk": "风险一",
            "sources": [{"id": "S1"}],
        }
        conflicting = {**base, "catalyst": "相互冲突的催化二"}
        duplicate_data = ResearchOrchestrator._parse_flash_json_text(
            json.dumps({"items": [base, conflicting]}, ensure_ascii=False)
        )
        self.assertEqual(len(duplicate_data["items"]), 2)
        with self.assertRaisesRegex(DeepSeekError, "数量错误|未知或重复ID"):
            ResearchOrchestrator._validate_flash_items(duplicate_data, {"C399807"})

        unknown_data = ResearchOrchestrator._parse_flash_json_text(
            json.dumps({"items": [{**base, "id": "C931755"}]}, ensure_ascii=False)
        )
        with self.assertRaisesRegex(DeepSeekError, "未知或重复ID：C931755"):
            ResearchOrchestrator._validate_flash_items(unknown_data, {"C399807"})

    def test_flash_parser_rejects_ambiguous_trailing_or_truncated_payloads(self):
        one = '{"items": []}'
        rejected = (
            "说明\n" + one + "\n" + one,
            "说明\n" + one + "\n这里还有补充解释。",
            '说明\n{"items":[',
        )
        for text in rejected:
            with self.subTest(text=text):
                self.assertIsNone(ResearchOrchestrator._parse_flash_json_text(text))

    def test_incomplete_flash_response_cannot_be_recovered_from_parseable_prefixed_json(self):
        result = ModelResult(
            text='说明如下：\n{"items": []}',
            raw={
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "status": "completed"}],
            },
            model="deepseek-v4-flash", input_tokens=10, output_tokens=2,
            response_kind="responses",
        )
        with self.assertRaisesRegex(DeepSeekError, "Flash响应未完成"):
            ResearchOrchestrator._prepare_flash_rows(
                ResearchOrchestrator.__new__(ResearchOrchestrator),
                "unused-task", result, set(), 1,
            )

    def test_api_test_uses_small_batches_low_reasoning_and_emits_wait_progress(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "test.sqlite3")
        ensure_strict_scan_items(db, 10)
        research_date = local_now().date().isoformat()

        class FakeMarketPipeline:
            def populate_metrics(self, task_id, universe, require_breadth=False, progress=None):
                return [{
                    "universe_id": item["index_code"], "proxy_code": item["index_code"],
                    "proxy_name": item["index_name"], "d1": 0.1, "d3": 0.2, "d5": 0.3,
                    "d10": 0.4, "d20": 0.5, "volume_signal": "正常",
                    "breadth_signal": "测试", "position_20d": "中位",
                } for item in universe]

        class FakeClient:
            def __init__(self):
                self.calls = []

            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ({"currency": "CNY", "total_balance": "10.00"},))

            def flash_search_json(self, **kwargs):
                self.calls.append(kwargs)
                compact = json.loads(kwargs["prompt"].splitlines()[-1])
                items = [{
                    "id": item["id"], "crowding": "低", "crowding_level": 20,
                    "catalyst": "测试", "catalyst_strength": 60, "risk": "测试",
                    "sources": [{
                        "id": "S1", "title": "来源", "url": f"https://example.com/{item['id']}",
                        "retrieved_at": research_date, "supports": "测试",
                    }],
                } for item in compact]
                raw = {
                    "status": "completed", "model": "deepseek-v4-flash",
                    "output": [{"type": "web_search_call"}, {"type": "message"}],
                }
                return ModelResult(
                    text=json.dumps({"items": items}), raw=raw, model="deepseek-v4-flash",
                    input_tokens=100, output_tokens=100, web_search_calls=1,
                    estimated_cost_cny=0.001,
                )

        client = FakeClient()
        progress = []
        orchestrator = ResearchOrchestrator(db, client, FakeMarketPipeline())
        task_id = orchestrator.create_job("20方向测试")
        result = orchestrator.run(task_id, lambda *args: progress.append(args))
        self.assertEqual(result.status, "测试完成")
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all(call["reasoning_effort"] == "none" for call in client.calls))
        self.assertTrue(all(research_date in call["instructions"] for call in client.calls))
        self.assertTrue(all(
            {"crowding_level", "catalyst_strength"}.issubset(
                call["schema"]["properties"]["items"]["items"]["required"]
            ) for call in client.calls
        ))
        self.assertTrue(all(
            call["schema"]["properties"]["items"]["items"]["properties"]["sources"]
                ["items"]["properties"]["retrieved_at"]["enum"] == [research_date]
            for call in client.calls
        ))
        self.assertEqual([len(json.loads(call["prompt"].splitlines()[-1])) for call in client.calls], [3, 3, 3, 1])
        self.assertTrue(any(args[1] == 0 and "第1/4批" in args[3] for args in progress))

        guarded_db = Database(Path(tmp.name) / "guarded.sqlite3")
        ensure_strict_scan_items(guarded_db, 10)
        guarded_db.save_settings({"api_test_cost_limit_cny": "0.0015"})
        guarded_client = FakeClient()
        guarded = ResearchOrchestrator(guarded_db, guarded_client, FakeMarketPipeline())
        guarded_task = guarded.create_job("20方向测试")
        guarded_result = guarded.run(guarded_task)
        self.assertEqual(guarded_result.status, "失败")
        self.assertEqual(len(guarded_client.calls), 0)
        self.assertIn("发送前预算不足", guarded_result.message)

    def test_flash_source_ids_are_global_and_bad_urls_are_rejected(self):
        research_date = local_now().date().isoformat()
        sources = ResearchOrchestrator._normalize_sources("U105", [
            {"id": "S1", "title": "A", "url": "https://example.com/a", "retrieved_at": research_date, "supports": "x"},
            {"id": "S1", "title": "重复URL", "url": "https://example.com/a", "retrieved_at": research_date, "supports": "x"},
            {"id": "S2", "title": "坏链接", "url": "javascript:alert(1)", "retrieved_at": research_date, "supports": "x"},
        ])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "U105-S1-1")

    def test_tool_only_flash_is_recorded_and_manual_resume_finalizes_without_researching_batch(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "tool-only.sqlite3")
        ensure_strict_scan_items(db, 10)
        db.save_settings({"api_auto_finalize_flash": "0"})
        today = local_now().date().isoformat()

        class FakeMarketPipeline:
            def populate_metrics(self, task_id, universe, require_breadth=False, progress=None):
                return [{
                    "universe_id": item["index_code"], "proxy_code": item["index_code"],
                    "proxy_name": item["index_name"], "d1": .1, "d3": .2, "d5": .3,
                    "d10": .4, "d20": .5, "volume_signal": "正常",
                    "breadth_signal": "测试", "position_20d": "中位",
                } for item in universe]

        class ToolOnlyClient:
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ({"currency":"CNY","total_balance":"10"},))
            def flash_search_json(self, **_kwargs):
                return ModelResult(
                    text="", raw={
                        "id":"search-only", "status":"completed", "model":"deepseek-v4-flash",
                        "output":[{"type":"web_search_call", "id":f"w{i}"} for i in range(9)],
                    }, model="deepseek-v4-flash", input_tokens=196713, output_tokens=1073,
                    web_search_calls=9, estimated_cost_cny=.0423, response_kind="responses",
                )

        task_id = ResearchOrchestrator(db).create_job("20方向测试")
        first = ResearchOrchestrator(db, ToolOnlyClient(), FakeMarketPipeline()).run(task_id)
        self.assertEqual(first.status, "失败")
        self.assertIn("续跑选中失败任务", first.message)
        call = db.row("SELECT * FROM model_calls WHERE task_id=?", (task_id,))
        self.assertEqual(json.loads(call["request_context_json"])["phase"], "search")
        expected_ids = [row["index_code"] for row in PlatformCatalogService(db).radar_universe()]
        self.assertEqual(json.loads(call["request_context_json"])["batch_ids"], expected_ids[:3])
        self.assertIn("web_search_call×9", first.message)

        class ResumeClient:
            def __init__(self):
                self.finalize_calls = []
                self.search_batches = []
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ({"currency":"CNY","total_balance":"10"},))
            @staticmethod
            def _result(kwargs, *, search_calls):
                compact = json.loads(kwargs["prompt"].splitlines()[-1])
                items = [{
                    "id": item["id"], "crowding":"低", "crowding_level":20,
                    "catalyst":"测试", "catalyst_strength":60, "risk":"测试",
                    "sources":[{"id":"S1", "title":"来源", "url":f"https://example.com/{item['id']}",
                                "retrieved_at":today, "supports":"测试"}],
                } for item in compact]
                output = ([{"type":"web_search_call"}] if search_calls else []) + [{"type":"message"}]
                return ModelResult(
                    text=json.dumps({"items":items}), raw={"id":"ok", "status":"completed", "output":output},
                    model="deepseek-v4-flash", input_tokens=100, output_tokens=100,
                    web_search_calls=search_calls, estimated_cost_cny=.001, response_kind="responses",
                )
            def flash_finalize_json(self, **kwargs):
                self.finalize_calls.append(kwargs)
                return self._result(kwargs, search_calls=0)
            def flash_search_json(self, **kwargs):
                batch = [item["id"] for item in json.loads(kwargs["prompt"].splitlines()[-1])]
                self.search_batches.append(batch)
                return self._result(kwargs, search_calls=1)

        resumed_client = ResumeClient()
        resumed = ResearchOrchestrator(db, resumed_client, FakeMarketPipeline()).run(task_id)
        self.assertEqual(resumed.status, "测试完成")
        self.assertEqual(len(resumed_client.finalize_calls), 1)
        self.assertEqual(resumed_client.search_batches[0], expected_ids[3:6])
        self.assertFalse(any(item in resumed_client.search_batches[0] for item in expected_ids[:3]))
        self.assertEqual(db.row("SELECT COUNT(*) n FROM sector_evidence WHERE task_id=?", (task_id,))["n"], 10)
        calls = db.rows("SELECT stage,web_search_calls FROM model_calls WHERE task_id=? ORDER BY id", (task_id,))
        self.assertEqual(calls[1]["stage"], "Flash无搜索收尾")
        self.assertEqual(calls[1]["web_search_calls"], 0)

    def test_prefixed_flash_json_is_accepted_locally_without_paid_finalize(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "auto-finalize.sqlite3")
        ensure_strict_scan_items(db, 10)
        today = local_now().date().isoformat()

        class FakeMarketPipeline:
            def populate_metrics(self, task_id, universe, require_breadth=False, progress=None):
                return [{
                    "universe_id": item["index_code"], "proxy_code": item["index_code"],
                    "proxy_name": item["index_name"], "d1": .1, "d3": .2, "d5": .3,
                    "d10": .4, "d20": .5, "volume_signal": "正常",
                    "breadth_signal": "测试", "position_20d": "中位",
                } for item in universe]

        class AutoFinalizeClient:
            def __init__(self):
                self.search_calls = 0
                self.finalize_calls = 0
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(
                    ("deepseek-v4-flash", "deepseek-v4-pro"), True,
                    ({"currency":"CNY", "total_balance":"10"},),
                )
            @staticmethod
            def valid_result(kwargs, *, search_calls, response_id):
                compact = json.loads(kwargs["prompt"].splitlines()[-1])
                items = [{
                    "id": item["id"], "crowding":"低", "crowding_level":20,
                    "catalyst":"测试", "catalyst_strength":60, "risk":"测试",
                    "sources":[{"id":"S1", "title":"来源", "url":f"https://example.com/{item['id']}",
                                "retrieved_at":today, "supports":"测试"}],
                } for item in compact]
                output = ([{"type":"web_search_call", "id":"w1", "status":"completed"}]
                          if search_calls else []) + [{"type":"message", "status":"completed"}]
                return ModelResult(
                    text=json.dumps({"items":items}),
                    raw={"id":response_id, "status":"completed", "output":output},
                    model="deepseek-v4-flash", input_tokens=100, output_tokens=100,
                    web_search_calls=search_calls, estimated_cost_cny=.001, response_kind="responses",
                )
            def flash_search_json(self, **kwargs):
                self.search_calls += 1
                if self.search_calls == 1:
                    valid = self.valid_result(kwargs, search_calls=1, response_id="search-invalid")
                    valid.text = "我已经完成搜索，下面给出JSON：\n" + valid.text
                    return valid
                return self.valid_result(kwargs, search_calls=1, response_id=f"search-{self.search_calls}")
            def flash_finalize_json(self, **kwargs):
                self.finalize_calls += 1
                return self.valid_result(kwargs, search_calls=0, response_id="finalized")

        client = AutoFinalizeClient()
        task_id = ResearchOrchestrator(db).create_job("20方向测试")
        result = ResearchOrchestrator(db, client, FakeMarketPipeline()).run(task_id)
        self.assertEqual(result.status, "测试完成")
        self.assertEqual(client.finalize_calls, 0)
        self.assertEqual(client.search_calls, 4)
        calls = db.rows(
            "SELECT stage,web_search_calls,request_context_json FROM model_calls WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call["stage"] == "Flash联网检索" for call in calls))
        self.assertTrue(all(call["web_search_calls"] == 1 for call in calls))
        self.assertIsNotNone(db.row(
            "SELECT 1 FROM analysis_events WHERE task_id=? AND stage='Flash本地JSON修复'",
            (task_id,),
        ))
        self.assertEqual(db.row(
            "SELECT COUNT(*) n FROM sector_evidence WHERE task_id=?", (task_id,)
        )["n"], 10)

    def test_failed_no_search_finalize_is_not_repeated(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "one-finalize.sqlite3")
        ensure_strict_scan_items(db, 10)
        db.save_settings({"api_auto_finalize_flash": "0"})
        class FakeMarketPipeline:
            def populate_metrics(self, task_id, universe, require_breadth=False, progress=None):
                return [{"universe_id":x["index_code"], "proxy_code":x["index_code"], "proxy_name":x["index_name"],
                         "d1":0, "d3":0, "d5":0, "d10":0, "d20":0, "volume_signal":"正常",
                         "breadth_signal":"测试", "position_20d":"中位"} for x in universe]
        class Initial:
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ())
            def flash_search_json(self, **_kwargs):
                return ModelResult(text="", raw={"id":"s", "status":"completed", "output":[{"type":"web_search_call"}]},
                                   model="deepseek-v4-flash", input_tokens=10, output_tokens=2,
                                   web_search_calls=1, estimated_cost_cny=.001, response_kind="responses")
        task_id = ResearchOrchestrator(db).create_job("20方向测试")
        self.assertEqual(ResearchOrchestrator(db, Initial(), FakeMarketPipeline()).run(task_id).status, "失败")
        class BadFinalize:
            def __init__(self): self.calls = 0
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ())
            def flash_finalize_json(self, **_kwargs):
                self.calls += 1
                return ModelResult(text="", raw={"id":"f", "status":"completed", "output":[]},
                                   model="deepseek-v4-flash", input_tokens=10, output_tokens=1,
                                   estimated_cost_cny=.001, response_kind="responses")
        bad = BadFinalize()
        self.assertEqual(ResearchOrchestrator(db, bad, FakeMarketPipeline()).run(task_id).status, "失败")
        self.assertEqual(bad.calls, 1)
        class NeverAgain:
            def __init__(self): self.calls = 0
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ())
            def flash_search_json(self, **_kwargs): self.calls += 1; raise AssertionError("must not search")
            def flash_finalize_json(self, **_kwargs): self.calls += 1; raise AssertionError("must not finalize twice")
        never = NeverAgain()
        third = ResearchOrchestrator(db, never, FakeMarketPipeline()).run(task_id)
        self.assertEqual(third.status, "失败")
        self.assertIn("不会再次循环扣费", third.message)
        self.assertEqual(never.calls, 0)

    def test_pro_failure_is_recorded_and_pro_budget_is_checked_before_send(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "test.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        universe = [db.row("SELECT * FROM universe WHERE index_code='U001'")]
        metrics = [{"universe_id": "U001", "d1": 1, "d3": 1, "d5": 1, "d10": 1, "d20": 1}]
        evidence = [{
            "task_id": task_id, "universe_id": "U001", "crowding": "低", "catalyst": "测试", "risk": "测试",
            "source_json": json.dumps([{"id": "U001-S1-1", "title": "来源", "url": "https://example.com/a",
                                        "retrieved_at": local_now().date().isoformat(), "supports": "测试"}]),
        }]
        class CutPro:
            def __init__(self):
                self.calls = 0
                self.kwargs = None
            def pro_json(self, **kwargs):
                self.calls += 1
                self.kwargs = kwargs
                return ModelResult(
                    text='{"partial":', raw={"id": "chat-cut", "choices": [{"finish_reason": "length"}]},
                    model="deepseek-v4-pro", input_tokens=100, output_tokens=100,
                    estimated_cost_cny=.01, response_kind="chat", finish_reason="length",
                )
        cut = CutPro(); orchestrator = ResearchOrchestrator(db, cut)
        with self.assertRaisesRegex(DeepSeekError, "长度或上下文上限"):
            orchestrator._run_pro(task_id, universe, metrics, evidence)
        row = db.row("SELECT * FROM model_calls WHERE task_id=?", (task_id,))
        self.assertEqual(row["finish_reason"], "length")
        self.assertEqual(row["response_id"], "chat-cut")
        compact_prompt = json.loads(cut.kwargs["prompt"])
        self.assertNotIn("universe", compact_prompt)
        self.assertNotIn("metrics", compact_prompt)
        self.assertNotIn("evidence", compact_prompt)
        self.assertEqual(len(compact_prompt["items"]), 1)
        compact_item = dict(zip(compact_prompt["item_columns"], compact_prompt["items"][0]))
        self.assertEqual(compact_item["source_ids"], ["U001-S1-1"])
        self.assertNotIn("sources", compact_item)
        self.assertEqual(len(compact_prompt["sources"]), 1)

        uncertain_db = Database(Path(tmp.name) / "uncertain.sqlite3")
        ensure_strict_scan_items(uncertain_db, 1)
        uncertain_task = ResearchOrchestrator(uncertain_db).create_job("完整扫描")
        class TransportCut:
            def pro_json(self, **_kwargs):
                raise DeepSeekError("模拟流式连接中断")
        with self.assertRaisesRegex(DeepSeekError, "模拟流式连接中断"):
            ResearchOrchestrator(uncertain_db, TransportCut())._run_pro(
                uncertain_task, universe, metrics, evidence,
            )
        uncertain = uncertain_db.row("SELECT * FROM model_calls WHERE task_id=?", (uncertain_task,))
        self.assertEqual(uncertain["response_status"], "transport_uncertain")
        self.assertGreater(uncertain["estimated_cost_cny"], 0)
        self.assertAlmostEqual(
            uncertain_db.row("SELECT estimated_cost_cny FROM analysis_jobs WHERE task_id=?", (uncertain_task,))["estimated_cost_cny"],
            uncertain["estimated_cost_cny"],
        )

        guarded = Database(Path(tmp.name) / "guarded.sqlite3")
        ensure_strict_scan_items(guarded, 1)
        guarded.save_settings({"api_full_cost_limit_cny": "0.01"})
        guarded_task = ResearchOrchestrator(guarded).create_job("完整扫描")
        never = CutPro()
        with self.assertRaisesRegex(DeepSeekError, "预算检查未通过"):
            ResearchOrchestrator(guarded, never)._run_pro(guarded_task, universe, metrics, evidence)
        self.assertEqual(never.calls, 0)

    def test_pro_can_reselect_from_quality_pool_and_application_builds_full_v3_audit(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "decision-only.sqlite3")
        db.save_settings({
            "api_pro_max_tokens": "80000",
            "api_pro_hard_output_cap_tokens": "7000",
        })
        universe = ensure_strict_scan_items(db, 10)[:10]
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        cutoff = ChinaTradingCalendar.from_db(db).latest_completed_trading_day(local_now()).isoformat()
        metrics = [{
            "universe_id": row["index_code"], "proxy_code": row["index_code"], "proxy_name": row["index_name"],
            "d1": 0.5, "d3": 1.2, "d5": 1.8, "d10": 2.5, "d20": 4.0 - index * 0.05,
            "volume_signal": "温和放量", "breadth_signal": "真实成分股上涨 6/10",
            "breadth_kind": "constituent_snapshot", "breadth_pct": 60.0,
            "position_20d": "价格区间位置60%", "position_20d_pct": 60.0,
            "amount_ratio_5d": 1.1, "volatility_20d": 14.0, "consecutive_up_days": 2,
            "data_quality": "COMPLETE", "data_cutoff": cutoff,
            "expected_data_cutoff": cutoff,
        } for index, row in enumerate(universe, start=1)]
        db.executemany(
            """INSERT INTO sector_metrics(
                 task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                 volume_signal,breadth_signal,position_20d,amount_ratio_5d,data_source,is_simulated,
                 data_cutoff,data_quality,volatility_20d,consecutive_up_days,breadth_kind,breadth_pct,
                 expected_data_cutoff
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                task_id, row["universe_id"], row["proxy_code"], row["proxy_name"],
                row["d1"], row["d3"], row["d5"], row["d10"], row["d20"],
                row["volume_signal"], row["breadth_signal"], row["position_20d"], row["amount_ratio_5d"],
                "fixture", 0, row["data_cutoff"], row["data_quality"], row["volatility_20d"],
                row["consecutive_up_days"], row["breadth_kind"], row["breadth_pct"], row["expected_data_cutoff"],
            ) for row in metrics],
        )
        hosts = ["a.example", "b.example", "c.example"]
        evidence = [{
            "task_id": task_id, "universe_id": row["index_code"], "crowding": "低", "crowding_level": "低",
            "catalyst": "产业订单改善", "catalyst_strength": "强", "risk": "常规波动",
            "source_json": json.dumps([{
                "id": f"{row['index_code']}-S1-1", "title": f"来源{index}",
                "url": f"https://{hosts[(index-1)%3]}/{index}",
                "retrieved_at": local_now().date().isoformat(), "supports": "测试证据",
            }], ensure_ascii=False),
        } for index, row in enumerate(universe, start=1)]
        universe_by_id = {row["index_code"]: row for row in universe}

        class DecisionOnlyPro:
            def __init__(self): self.kwargs = None
            def pro_json(self, **kwargs):
                self.kwargs = kwargs
                candidate_pool = json.loads(kwargs["prompt"])["candidate_pool"]
                decision = {
                    "market_regime": "轮动震荡", "market_summary": '从"国企"到市场',
                    "candidates": [{
                        "rank": index, "universe_id": item["universe_id"], "index_code": item["universe_id"],
                        "sector_name": universe_by_id[item["universe_id"]]["index_name"],
                        "score_adjustment": 5, "allocation_pct": 0, "timing_status": item["timing_status"],
                        "thesis": "测试逻辑",
                        "catalyst": "测试催化", "crowding": "中", "risk": "测试风险",
                        "buy_conditions": "条件满足", "abandon_conditions": "条件失效",
                        "fund_code": "", "fund_name": "", "correlation_bucket": "测试",
                    } for index, item in enumerate(candidate_pool[1::2], start=1)],
                    "avoid_list": [], "fund_updates": [],
                    "questions_for_intraday": ["当前涨跌幅"], "risk_flags": ["测试风险"],
                }
                return ModelResult(
                    text=json.dumps(decision, ensure_ascii=False),
                    raw={"id": "decision-ok", "choices": [{"finish_reason": "stop"}]},
                    model="deepseek-v4-pro", input_tokens=100, output_tokens=200,
                    estimated_cost_cny=.01, response_kind="chat", finish_reason="stop",
                )

        client = DecisionOnlyPro()
        payload, raw_text = ResearchOrchestrator(db, client)._run_pro(task_id, universe, metrics, evidence)
        self.assertEqual(client.kwargs["max_tokens"], 7000)
        self.assertIn("不要复制items、sources或生成audit", client.kwargs["system"])
        self.assertEqual(len(payload["audit"]), 10)
        self.assertLessEqual(len(payload["candidates"]), 5)
        self.assertTrue(all(row["score_adjustment"] == 0 for row in payload["candidates"]))
        self.assertTrue(all(row["research_tilt"] in {
            "非常推荐买", "推荐买", "有点推荐买", "观察", "不太推荐买", "不推荐买", "千万别买",
        } for row in payload["audit"]))
        self.assertEqual(len(payload["sources"]), 10)
        self.assertEqual(payload["market_summary"], '从"国企"到市场')
        parsed = parse_result(raw_text, set(), 8000, universe)
        self.assertTrue(parsed.ok, parsed.errors)
        context = json.loads(db.row(
            "SELECT request_context_json FROM model_calls WHERE task_id=?", (task_id,),
        )["request_context_json"])
        self.assertEqual(context["phase"], "pro_decision_only")
        self.assertEqual(context["local_audit_items"], 10)

    def test_resume_reuses_only_complete_same_day_real_metrics(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "same-day.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        universe = [db.row("SELECT * FROM universe WHERE index_code='U001'")]
        db.execute(
            """INSERT INTO sector_metrics(
                 task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                 volume_signal,breadth_signal,position_20d,data_source,is_simulated,data_cutoff,
                 expected_data_cutoff,freshness_state
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "U001", "000001", "上证指数", 1, 2, 3, 4, 5,
             "正常", "扩散", "中位", "test", 0,
             ChinaTradingCalendar.from_db(db).latest_completed_trading_day(local_now()).isoformat(),
             ChinaTradingCalendar.from_db(db).latest_completed_trading_day(local_now()).isoformat(),
             "fresh"),
        )
        orchestrator = ResearchOrchestrator(db)
        self.assertEqual(len(orchestrator._same_day_resume_metrics(task_id, universe)), 1)
        db.execute(
            "UPDATE sector_metrics SET data_cutoff=? WHERE task_id=?",
            ((ChinaTradingCalendar.from_db(db).latest_completed_trading_day(local_now()) - timedelta(days=1)).isoformat(), task_id),
        )
        self.assertEqual(orchestrator._same_day_resume_metrics(task_id, universe), [])

    def test_resume_freezes_stale_metrics_after_a_billable_request_was_reserved(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "same-day-paid.sqlite3")
        ensure_strict_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        universe = [db.row("SELECT * FROM universe WHERE index_code='U001'")]
        expected = ChinaTradingCalendar.from_db(db).latest_completed_trading_day(local_now())
        previous = ChinaTradingCalendar.from_db(db).previous_trading_day(expected).isoformat()
        db.execute(
            """INSERT INTO sector_metrics(
                 task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                 volume_signal,breadth_signal,position_20d,data_source,is_simulated,data_cutoff,
                 expected_data_cutoff,freshness_state
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "U001", "000001", "上证指数", 1, 2, 3, 4, 5,
             "正常", "扩散", "中位", "test", 0, previous, expected.isoformat(), "stale"),
        )
        orchestrator = ResearchOrchestrator(db)
        self.assertEqual(orchestrator._same_day_resume_metrics(task_id, universe), [])
        db.execute(
            "INSERT INTO model_calls(task_id,stage,model,created_at) VALUES(?,?,?,?)",
            (task_id, "Flash联网检索", "deepseek-v4-flash", local_now().isoformat(timespec="seconds")),
        )
        reused = orchestrator._same_day_resume_metrics(task_id, universe)
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["data_cutoff"], previous)

    def test_transport_uncertain_flash_task_is_not_automatically_retried(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "resume.sqlite3")
        ensure_strict_scan_items(db, 10)
        today = local_now().date().isoformat()
        class FakeMarketPipeline:
            def populate_metrics(self, task_id, universe, require_breadth=False, progress=None):
                return [{
                    "universe_id": item["index_code"], "proxy_code": item["index_code"], "proxy_name": item["index_name"],
                    "d1": .1, "d3": .2, "d5": .3, "d10": .4, "d20": .5,
                    "volume_signal": "正常", "breadth_signal": "测试", "position_20d": "中位",
                } for item in universe]
        class ResumeClient:
            def __init__(self, fail_on=0): self.calls=[]; self.fail_on=fail_on
            def connection_report(self, *_args, **_kwargs):
                return ConnectionReport(("deepseek-v4-flash", "deepseek-v4-pro"), True, ({"currency":"CNY","total_balance":"10"},))
            def flash_search_json(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail_on and len(self.calls) == self.fail_on:
                    raise DeepSeekError("模拟上游失败")
                compact = json.loads(kwargs["prompt"].splitlines()[-1])
                items = [{"id": x["id"], "crowding":"低", "crowding_level":20,
                          "catalyst":"测试", "catalyst_strength":60, "risk":"测试",
                          "sources":[{"id":"S1", "title":"来源", "url":f"https://example.com/{x['id']}",
                                      "retrieved_at":today, "supports":"测试"}]} for x in compact]
                return ModelResult(
                    text=json.dumps({"items":items}), raw={"id":f"r{len(self.calls)}", "status":"completed",
                    "output":[{"type":"web_search_call"},{"type":"message"}]}, model="deepseek-v4-flash",
                    input_tokens=100, output_tokens=100, web_search_calls=1, estimated_cost_cny=.001,
                    response_kind="responses",
                )
        first = ResumeClient(fail_on=2)
        orchestrator = ResearchOrchestrator(db, first, FakeMarketPipeline())
        task_id = orchestrator.create_job("20方向测试")
        failed_result = orchestrator.run(task_id)
        self.assertEqual(failed_result.status, "失败")
        self.assertEqual(failed_result.completed_items, 3)
        self.assertIn("已安全保存3/10项", failed_result.message)
        self.assertIn("本任务不能自动续跑", failed_result.message)
        self.assertNotIn("点击“续跑选中失败任务”", failed_result.message)
        uncertain_call = db.row(
            "SELECT response_status FROM model_calls WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        self.assertEqual(uncertain_call["response_status"], "transport_uncertain")
        self.assertEqual(db.row("SELECT COUNT(*) n FROM sector_evidence WHERE task_id=?", (task_id,))["n"], 3)
        second = ResumeClient()
        result = ResearchOrchestrator(db, second, FakeMarketPipeline()).run(task_id)
        self.assertEqual(result.status, "失败")
        self.assertEqual(len(second.calls), 0)
        self.assertIn("禁止自动重发", result.message)
        self.assertEqual(db.row("SELECT COUNT(*) n FROM sector_evidence WHERE task_id=?", (task_id,))["n"], 3)


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_primary_launchers_are_ascii_only(self):
        for name in (
            "START_SECTOR_RADAR.cmd",
            "BUILD_PORTABLE_EXE.cmd",
            "启动板块雷达.bat",
            "生成便携EXE.bat",
        ):
            data = (self.root / name).read_bytes()
            data.decode("ascii")

    def test_primary_launchers_use_windows_crlf_only(self):
        for name in (
            "START_SECTOR_RADAR.cmd",
            "BUILD_PORTABLE_EXE.cmd",
            "启动板块雷达.bat",
            "生成便携EXE.bat",
        ):
            data = (self.root / name).read_bytes()
            self.assertIn(b"\r\n", data, name)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), name)

    def test_each_primary_action_creates_a_txt_diagnostic(self):
        startup = (self.root / "START_SECTOR_RADAR.cmd").read_text(encoding="ascii")
        build = (self.root / "BUILD_PORTABLE_EXE.cmd").read_text(encoding="ascii")
        self.assertIn("STARTUP_DIAGNOSTIC.txt", startup)
        self.assertIn("BUILD_DIAGNOSTIC.txt", build)
        self.assertIn("pause", startup.lower())
        self.assertIn("pause", build.lower())

    def test_build_launcher_validates_real_python_candidates(self):
        build = (self.root / "BUILD_PORTABLE_EXE.cmd").read_text(encoding="ascii")
        self.assertIn("for %%C in (py.exe py.cmd python.exe python3.exe)", build)
        self.assertIn("sys.version_info >= (3, 11)", build)
        self.assertIn('"%PYTHON_CMD%" %PYTHON_PREFIX% sector_radar.pyw --self-test', build)
        self.assertLess(build.index("call :FIND_PYTHON"), build.index("if defined PYTHON_CMD"))
        self.assertIn("py.cmd", build[build.rindex(":FIND_PYTHON"):])

    def test_dpi_setup_precedes_tk_ui_import(self):
        entry = (self.root / "sector_radar.pyw").read_text(encoding="utf-8")
        self.assertLess(
            entry.index("DPI_MODE = enable_high_dpi_awareness()"),
            entry.index("from app.ui import main"),
        )

    def test_ui_uses_built_in_color_badges_and_explicit_deepseek_action(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("PAGE_META", text)
        self.assertIn("保存并连接 DeepSeek", text)
        self.assertIn("V4-Pro 正式版", text)
        self.assertIn("Pro正式版模型", text)
        self.assertIn("正式版计费口径", text)
        self.assertIn("官方能力为", text)
        self.assertIn("本软件为控制费用和超长JSON风险", text)
        self.assertIn("每次付费扫描前显示预算并要求你确认", text)
        self.assertIn("def _poll_close_backup(self,state):", text)
        self.assertIn("self.after(100, lambda state=close_state:self._poll_close_backup(state))", text)
        self.assertNotIn("self.after(0,lambda saved_error=error:self._finish_close", text)
        self.assertIn("def _poll_close_workers(self,state):", text)
        self.assertIn("无需再次点击关闭", text)
        self.assertIn('name="SectorRadar:关闭前安全备份"', text)
        self.assertNotIn("本次不会强行关闭；请等待任务结束后再关闭", text)
        self.assertNotIn('"扫描尚未收尾"', text)
        self.assertIn('prune_backups(stop_requested=stop.is_set)', text)
        self.assertIn('cancel=stop.set', text)
        self.assertIn('runtime_cleanup_last_at', text)
        self.assertNotIn("Segoe UI Emoji", text)

    def test_ui_groups_navigation_and_scan_actions_by_user_goal(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        for section in ("每天先看", "研究与选择", "账户与复盘", "帮助与安全"):
            self.assertIn(section, text)
        for phase in ("① 免费检查", "② 小额试跑", "③ 正式研究", "安全与诊断"):
            self.assertIn(phase, text)
        self.assertIn('self.nav_indicators[k].config', text)
        self.assertIn("def set_enabled(self, enabled: bool)", text)
        self.assertIn("def _ensure_nav_visible(self,key)", text)
        self.assertIn("① AI建议（参考·不记账）", text)
        self.assertIn("② 我的草稿（拟操作）", text)
        self.assertIn("self.work_tabs.select(self.draft_panel)", text)

    def test_stale_draft_ui_requires_an_explicit_real_world_outcome(self):
        ui_text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        service_text = (self.root / "app" / "decision_drafts.py").read_text(encoding="utf-8")
        button_help = ui_text[ui_text.index("BUTTON_HELP"):ui_text.index("NAV_HELP")]
        for label in ("确认原日已扣款", "仍打算今天操作", "确认原日未执行"):
            self.assertIn(label, ui_text)
            self.assertIn(label, button_help)
        self.assertIn("def acknowledge_selected_stale_recurring", ui_text)
        self.assertIn("def abandon_selected_stale_draft", ui_text)
        self.assertIn("def acknowledge_stale_recurring_deduction", service_text)
        self.assertNotIn('CuteButton(draft_actions,"↪️ 结转选中旧草稿"', ui_text)
        self.assertIn("不会提前增加持仓", ui_text)
        self.assertIn("不会扣主动策略现金", ui_text)

    def test_scan_scope_separates_durable_catalog_from_same_day_execution(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("长期严格C研究目录", text)
        self.assertIn("今日可执行覆盖", text)
        self.assertIn("长期收录不等于今日可下单", text)
        self.assertIn("覆盖不足不会创建付费任务", text)
        self.assertNotIn("本次当前可买目录", text)

    def test_every_action_button_has_a_persistent_small_help_line(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("class CuteButton(tk.Frame)", text)
        self.assertIn("BUTTON_HELP", text)
        self.assertIn('font=(FONT, FONT_SIZE["hint"])', text)
        self.assertIn('self.hint.pack(fill="x")', text)
        self.assertNotIn("步骤4：连接 DeepSeek", text)

    def test_scan_result_window_never_locks_the_main_application(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        dialog_start = text.index("def show_nonmodal_task_result")
        dialog_end = text.index("\n\nclass Card", dialog_start)
        dialog = text[dialog_start:dialog_end]
        finish_start = text.index("    def _finish_scan(self, result):")
        finish_end = text.index("\n    def stop_scan", finish_start)
        finish = text[finish_start:finish_end]
        self.assertNotIn("grab_set", dialog)
        self.assertNotIn("wait_window", dialog)
        self.assertIn('FONT_SIZE["section"]', dialog)
        self.assertNotIn("messagebox.showinfo", finish)
        self.assertIn("show_nonmodal_task_result", finish)

    def test_main_window_uses_readable_high_dpi_layout(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('self.state("zoomed")', text)
        self.assertIn("nav.pack_propagate(False)", text)
        self.assertNotIn("width=14,anchor=\"w\"", text)

    def test_review_layout_keeps_chart_and_segment_columns_readable(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("chart_height=max(280", text)
        self.assertIn("self.trade_tables.sash_place(0,int(width*0.45),1)", text)
        self.assertIn('(\"fund\",\"基金\",340)', text)
        self.assertIn("x=(left+right)/2 if len(cumulative)==1", text)
        self.assertIn("每个点代表一个完整持有段", text)
        self.assertIn("self.canvas.bbox(title_item)", text)
        self.assertIn("subtitle_box[3]", text)

    def test_physical_ui_audit_is_isolated_and_credential_free(self):
        text = (self.root / "tools" / "capture_fullscreen_ui.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            text.index("enable_high_dpi_awareness()"),
            text.index("from app.ui import PAGE_GUIDES"),
        )
        self.assertIn("source_conn.backup(destination_conn)", text)
        self.assertIn("key='deepseek_api_key_dpapi'", text)
        self.assertIn("source_database_modified\": False", text)
        self.assertIn("temporary_database_deleted\": True", text)
        self.assertIn("supplementary_captures", text)
        self.assertIn("05b-radar-score-detail.png", text)
        self.assertIn('"decision_drafts"', text)
        self.assertIn("decision.work_tabs.select(decision.draft_panel)", text)

    def test_ranking_replay_is_read_only_and_hash_guarded(self):
        text = (self.root / "tools" / "replay_ranking_snapshot.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("?mode=ro", text)
        self.assertIn('database_unchanged', text)
        self.assertIn('if before != after:', text)
        self.assertNotIn("Database(", text)

    def test_database_and_network_threads_are_owned_by_application(self):
        ui_text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        investable_text = (self.root / "app" / "investable.py").read_text(encoding="utf-8")
        # Normal jobs live in the central registry. The only second thread is
        # the explicitly owned closing-backup worker, whose handle is retained
        # in the shutdown state and polled by Tk before destroy().
        self.assertEqual(ui_text.count("threading.Thread("), 2)
        self.assertIn('name=f"SectorRadar:{label}"', ui_text)
        self.assertIn('name="SectorRadar:关闭前安全备份"', ui_text)
        self.assertIn('state["backup_state"]=close_state', ui_text)
        self.assertNotIn("threading.Thread(", investable_text)
        self.assertIn("resolve_pending_if_idle", investable_text)
        dashboard = ui_text[ui_text.index("class DashboardPage"):ui_text.index("class APIRadarPage")]
        self.assertIn(
            'self.app.start_background(worker,label="空闲基金名称补全",cancel=self._idle_name_stop.set)',
            dashboard,
        )

    def test_long_running_data_tasks_expose_progress_and_safe_stop_controls(self):
        ui_text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        for marker in (
            "停止补全", "停止映射核验", "停止联网任务", "停止联网",
            "停止净值更新", "停止校准",
        ):
            self.assertIn(marker, ui_text)
        for marker in (
            "resolve_progress", "catalog_progress", "network_progress",
            "fund_task_progress", "nav_progress", "pending_progress",
            "recurring_progress", "recommendation_progress",
        ):
            self.assertIn(marker, ui_text)
        self.assertGreaterEqual(ui_text.count("cancel=self."), 9)

    def test_recommendation_restore_and_recompute_never_block_tk(self):
        ui_text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        review = ui_text[ui_text.index("class ReviewPage"):ui_text.index("class HelpPage")]
        self.assertIn('self.app.start_background(worker,label="历史推荐快照恢复")', review)
        self.assertIn('self.app.start_background(worker,label="本地推荐表现重算")', review)
        self.assertIn('self._set_recommendation_task_mode("local")', review)
        self.assertIn('self._set_recommendation_task_mode("network")', review)

    def test_daily_seven_step_cards_use_wrapped_four_plus_three_layout(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('uniform="daily-workflow"', text)
        self.assertIn('grid_row, grid_column, columnspan = 0, (number - 1) * 3, 3', text)
        self.assertIn('grid_row, grid_column, columnspan = 1, (number - 5) * 4, 4', text)
        self.assertIn('label.config(wraplength=max(100, event.width - 20))', text)

    def test_alpha9_uses_extra_large_fonts_and_scrollable_wide_navigation(self):
        from app.ui import FONT_SIZE

        self.assertGreaterEqual(FONT_SIZE["body"], 15)
        self.assertGreaterEqual(FONT_SIZE["button"], 14)
        self.assertGreaterEqual(FONT_SIZE["title"], 30)
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("max(430, min(520, int(screen_w * 0.23)))", text)
        self.assertIn('nav_canvas.bind("<Enter>"', text)
        self.assertIn("class Pictogram", text)

    def test_card_defaults_can_be_overridden_without_duplicate_keywords(self):
        from app.ui import Card

        card = object.__new__(Card)
        with patch("tkinter.Frame.__init__", return_value=None) as frame_init:
            Card.__init__(card, "parent", bg="#FFF3DE", highlightbackground="#E9D4A9")
        _, kwargs = frame_init.call_args
        self.assertEqual(kwargs["bg"], "#FFF3DE")
        self.assertEqual(kwargs["highlightbackground"], "#E9D4A9")
        self.assertEqual(kwargs["highlightthickness"], 1)

    def test_windows_self_test_builds_the_real_hidden_application(self):
        entry = (self.root / "sector_radar.pyw").read_text(encoding="utf-8")
        self.assertIn("SectorRadarApp(start_hidden=True, database=database)", entry)
        self.assertIn("UI_PAGES=", entry)
        self.assertIn("actual_pages != expected_pages", entry)
        self.assertIn("Radar tab mismatch", entry)
        self.assertIn("Ranking settings missing", entry)

    def test_night_radar_exposes_main_catalog_alternatives_and_avoid_views(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        for label in (
            "① 最终主榜（最多10项）",
            "② 全目录七档结论",
            "③ 同类备选/候选池",
            "④ Pro终审回避（独立）",
        ):
            self.assertIn(label, text)
        self.assertIn("FROM radar_audit a WHERE a.task_id=? ORDER BY", text)
        self.assertNotIn("FROM radar_audit a WHERE a.task_id=? ORDER BY a.local_rank LIMIT 10", text)

    def test_score_explanation_uses_frozen_policy_and_separates_safety_cap(self):
        from app.ui import ranking_formula_text

        row = {
            "opportunity_score": 78.0,
            "recommendation_score": 59.0,
            "crowding_risk_score": 70.0,
        }
        components = {
            "crowding_adjustment": -6.2,
            "crowding_neutral_point": 50.0,
            "pre_cap_recommendation_score": 71.8,
            "high_position_cap_applied": True,
        }
        explanation = ranking_formula_text(
            row, components, {"crowding_penalty": 0.31}, "four-dimension-v4",
        )
        self.assertIn("基础推荐分71.8", explanation)
        self.assertIn("0.31×（中性50－拥挤风险70.0）", explanation)
        self.assertIn("拥挤调整-6.2", explanation)
        self.assertIn("执行59分上限，最终推荐分59.0", explanation)
        self.assertNotIn("0.45×", explanation)

        historical = ranking_formula_text(
            row, {}, {"crowding_penalty": 0.31}, "four-dimension-v3",
        )
        self.assertIn("0.31×拥挤风险70.0", historical)
        self.assertIn("历史v3公式", historical)

    def test_trade_detail_panel_explains_ledger_state_and_next_action(self):
        from app.ui import trade_detail_summary

        pending = {
            "id": 24, "fund_code": "020741", "fund_name": "华泰保兴安悦债券C",
            "action": "卖出", "status": "待确认", "trade_date": "2026-08-13",
            "nav_date": "2026-08-13", "confirm_date": "", "amount": 3384.75,
            "units": 2878.92, "nav": 1.1757, "fee": 0,
            "counts_toward_active_capital": 0, "result_pnl": None,
            "result_pnl_status": "待核算", "source": "用户手工待确认",
            "rationale": "最终到账以支付宝为准", "ai_advice": "",
        }
        title, facts, context, next_kind, next_text = trade_detail_summary(pending, posted=False)
        self.assertIn("020741 华泰保兴安悦债券C", title)
        self.assertIn("尚未过账", facts)
        self.assertIn("结果盈亏：待核算", facts)
        self.assertIn("用户手工待确认", context)
        self.assertEqual(next_kind, "ready")
        self.assertIn("对照支付宝核对", next_text)

        locked = dict(pending, status="已确认", confirm_date="2026-08-14", result_pnl=12.34, result_pnl_status="已核算")
        _title, locked_facts, _context, locked_kind, locked_next = trade_detail_summary(locked, posted=True)
        self.assertIn("已过账并锁定", locked_facts)
        self.assertIn("结果盈亏：", locked_facts)
        self.assertIn("12.34", locked_facts)
        self.assertEqual(locked_kind, "locked")
        self.assertIn("撤销已入账交易", locked_next)

    def test_every_page_has_a_key_operation_guide(self):
        from app.ui import PAGE_GUIDES

        expected = {"decision", "api", "market", "ai", "radar", "audit", "fundselect", "funds", "holdings", "trades", "recurring", "review", "help", "settings"}
        self.assertEqual(set(PAGE_GUIDES), expected)
        for title, instruction, note in PAGE_GUIDES.values():
            self.assertTrue(title.strip())
            self.assertGreaterEqual(len(instruction), 20)
            self.assertTrue(note.strip())

    def test_help_is_built_into_the_application(self):
        from app.ui import HELP_TOPICS

        self.assertGreaterEqual(len(HELP_TOPICS), 7)
        self.assertTrue(all(title.strip() and len(content) >= 80 for title, content in HELP_TOPICS))
        help_text = "\n".join(content for _title, content in HELP_TOPICS)
        self.assertNotIn("单文件 EXE", help_text)
        self.assertNotIn("G:\\SectorRadar", help_text)
        self.assertIn("路径会根据主程序所在位置自动确定", help_text)
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("class HelpPage", text)
        self.assertIn('(\"help\",\"使用指南\",HelpPage)', text)

    def test_release_build_is_one_app_folder_and_uses_portable_data(self):
        build = (self.root / "BUILD_PORTABLE_EXE.cmd").read_text(encoding="ascii")
        config = (self.root / "app" / "config.py").read_text(encoding="utf-8")
        self.assertIn("--onedir", build)
        self.assertNotIn("--onefile", build)
        self.assertIn('--contents-directory "_internal"', build)
        self.assertNotIn("--runtime-tmpdir", build)
        self.assertNotIn("G:\\SectorRadar", build)
        self.assertIn('app_root() / "data"', config)
        self.assertNotIn('os.getenv("APPDATA"', config)

    def test_night_radar_explains_how_results_are_created(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("去一键AI扫描", text)
        self.assertIn("生成网页版备用任务包", text)
        self.assertIn("它不会自动执行研究", text)
        self.assertIn("完整扫描全部CSI行业", text)

    def test_colorful_generated_icons_replace_system_emoji_rendering(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        self.assertIn("def make_action_icon", text)
        self.assertIn("class CuteSticker", text)
        self.assertIn('"compound": "left"', text)
        self.assertNotIn("Segoe UI Emoji", text)

    def test_sparse_api_history_does_not_expand_into_blank_panel(self):
        text = (self.root / "app" / "ui.py").read_text(encoding="utf-8")
        api_start = text.index("class APIRadarPage")
        market_start = text.index("class MarketDataPage")
        api_text = text[api_start:market_start]
        self.assertIn('show="headings", height=4', api_text)
        self.assertIn("task_scroll", api_text)
        self.assertIn('self.tree.bind("<MouseWheel>"', api_text)


if __name__ == "__main__":
    unittest.main()

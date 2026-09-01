import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.fund_catalog_sync import (
    CSIFieldFundSyncService,
    CSIOfficialProductService,
    FundCatalogSyncError,
    AUTOMATIC_FIELD_SOURCE_NOTE,
)
from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.fund_discovery import FundDiscoveryError
from app.platform_catalog import PlatformCatalogService


class FundCatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "catalog.sqlite3")
        self.catalog = PlatformCatalogService(self.db)
        self.index = self.catalog.list_indices()[0]

    def tearDown(self):
        self.folder.cleanup()

    def test_stop_before_first_index_never_starts_catalog_network_request(self):
        official_calls = []
        official = CSIOfficialProductService(
            self.db, downloader=lambda url: official_calls.append(url) or b"{}",
        ).sync_all(force_refresh=True, stop_requested=lambda: True)
        self.assertTrue(official.stopped)
        self.assertEqual(official.completed, 0)
        self.assertEqual(official_calls, [])
        self.assertEqual(self.db.setting("csi_product_sync_last_attempt_status"), "已停止")

        search_calls = []
        field = CSIFieldFundSyncService(
            self.db,
            searcher=lambda query: search_calls.append(query) or [],
            profile_loader=lambda _code: {},
        ).sync_all(force_refresh=True, stop_requested=lambda: True)
        self.assertTrue(field.stopped)
        self.assertEqual(field.completed, 0)
        self.assertEqual(search_calls, [])
        self.assertEqual(self.db.setting("csi_fund_sync_last_attempt_status"), "已停止")

    def test_official_products_are_complete_display_records_not_execution_records(self):
        code = self.index["official_index_code"]
        before_execution_rows = self.db.row(
            "SELECT COUNT(*) n FROM investable_funds WHERE investable_index_id=?",
            (self.index["id"],),
        )["n"]
        payload = {
            "code": "200", "msg": "Success", "success": True,
            "data": [
                {
                    "productCode": "510001", "fundName": "测试行业ETF",
                    "assetClass": "股票", "fundType": "ETF", "coverage": "完整",
                    "indexCode": code, "fundManager": "测试基金", "inceptionDate": "2020-01-01",
                    "exchange": "上海证券交易所", "aum": "12.3亿元",
                },
                {
                    "productCode": "000001", "fundName": "测试行业ETF联接C",
                    "assetClass": "股票", "fundType": "ETF联接", "coverage": "完整",
                    "indexCode": code, "fundManager": "测试基金", "inceptionDate": "2021-01-01",
                },
            ],
        }
        service = CSIOfficialProductService(
            self.db, downloader=lambda _url: json.dumps(payload, ensure_ascii=False).encode(),
        )
        self.assertEqual(service.sync_one(self.index), 2)
        products = self.catalog.official_products(self.index["id"])
        self.assertEqual({row["product_code"] for row in products}, {"510001", "000001"})
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) n FROM investable_funds WHERE investable_index_id=?",
                (self.index["id"],),
            )["n"],
            before_execution_rows,
        )
        self.assertEqual(self.catalog.coverage()["official_covered_indices"], 1)

    def test_failed_refresh_preserves_last_good_official_snapshot(self):
        code = self.index["official_index_code"]
        payload = {"code": "200", "data": [{
            "productCode": "510001", "fundName": "测试ETF", "indexCode": code,
        }]}
        CSIOfficialProductService(
            self.db, downloader=lambda _url: json.dumps(payload).encode(),
        ).sync_one(self.index)

        def broken(_url):
            raise OSError("temporary TLS failure")

        with self.assertRaises(FundCatalogSyncError):
            CSIOfficialProductService(self.db, downloader=broken).sync_one(self.index)
        products = self.catalog.official_products(self.index["id"])
        self.assertEqual([row["product_code"] for row in products], ["510001"])
        state = self.db.row(
            "SELECT * FROM csi_product_sync_status WHERE investable_index_id=?",
            (self.index["id"],),
        )
        self.assertEqual(state["state"], "失败")
        self.assertTrue(state["last_error"])

    def test_completed_status_does_not_skip_a_missing_official_snapshot(self):
        code = self.index["official_index_code"]
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO csi_product_sync_status(
                       investable_index_id,state,product_count,attempted_at)
                   VALUES(?,'已完成',1,'2026-08-09')""",
                (self.index["id"],),
            )
        payload = {"code": "200", "data": [{
            "productCode": "510001", "fundName": "测试ETF", "indexCode": code,
        }]}
        service = CSIOfficialProductService(
            self.db, downloader=lambda _url: json.dumps(payload).encode(),
        )
        with patch(
            "app.fund_catalog_sync.PlatformCatalogService.list_indices",
            return_value=[self.index],
        ):
            report = service.sync_all(incomplete_only=True)
        self.assertEqual(report.completed, 1)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(len(self.catalog.official_products(self.index["id"])), 1)

    def test_lightweight_c_sync_accepts_only_exact_passive_public_c_share(self):
        official_name = self.index["official_index_name"]
        search_rows = [
            {"CODE": "000001", "NAME": "严格同指数ETF联接C", "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"}},
            {"CODE": "000002", "NAME": "严格同指数ETF联接A", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
            {"CODE": "000003", "NAME": "严格同指数增强C", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
            {"CODE": "000004", "NAME": "相似指数ETF联接C", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
            {"CODE": "000005", "NAME": "严格同指数ETF联接C", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "0"}},
            {"CODE": "510006", "NAME": "严格同指数ETFC", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
        ]

        def searcher(_query):
            return search_rows

        def profile(code):
            target = "名称相似但不是同一指数" if code == "000004" else official_name
            names = {
                "000001": "严格同指数交易型开放式指数证券投资基金联接C",
                "000002": "严格同指数交易型开放式指数证券投资基金联接A",
                "000003": "严格同指数增强型证券投资基金C",
            }
            return {
                "full_name": names.get(code, "严格同指数ETF联接C"),
                "target_index": target,
                "benchmark": f"{target}收益率",
                "source_url": f"https://example.com/{code}",
            }

        service = CSIFieldFundSyncService(
            self.db, searcher=searcher, profile_loader=profile, max_workers=2,
        )
        self.assertEqual(service.sync_one(self.index), 1)
        funds = self.catalog.funds(self.index["id"])
        accepted = next(row for row in funds if row["fund_code"] == "000001")
        self.assertEqual(accepted["share_class"], "C")
        self.assertEqual(accepted["relation_kind"], "同一指数")
        self.assertEqual(accepted["radar_eligible"], 1)
        library = self.db.row("SELECT * FROM funds WHERE fund_code='000001'")
        self.assertIsNotNone(library)
        self.assertEqual(library["share_class"], "C")
        self.assertEqual(library["index_code"], self.index["ant_index_code"])
        self.assertNotEqual(library["correlation_bucket"], "待分类")

    def test_official_product_family_proves_exact_c_when_short_index_name_differs(self):
        index_id = int(self.index["id"])
        legal_family = (
            "天弘上证科创板芯片设计主题交易型开放式指数证券投资基金"
            "发起式联接基金"
        )
        self.db.execute(
            """INSERT INTO csi_index_products(
                   investable_index_id,index_code,product_code,fund_name,share_class,
                   strategy_kind,fund_type,source_url,fetched_at,active)
               VALUES(?,?,?,?,?,'被动指数','联接基金','https://www.csindex.com.cn/',
                      '2026-08-09',1)""",
            (
                index_id,
                self.index["official_index_code"],
                "027574",
                legal_family,
                "",
            ),
        )
        self.db.execute(
            """INSERT INTO csi_product_sync_status(
                   investable_index_id,state,product_count,attempted_at,last_success_at)
               VALUES(?,'已完成',1,'2026-08-09','2026-08-09')""",
            (index_id,),
        )
        rows = [{
            "CODE": "027575",
            "NAME": "天弘上证科创板芯片设计主题ETF发起联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: rows,
            profile_loader=lambda _code: {
                "full_name": legal_family,
                "target_index": "上证科创板芯片设计主题指数",
                "benchmark": "上证科创板芯片设计主题指数收益率",
                "source_url": "https://fundf10.eastmoney.com/jbgk_027575.html",
            },
        )
        # Deliberately make the catalog short label different from the F10 label.
        index = dict(self.index)
        index["official_index_name"] = "科创芯片设计"
        found = service.discover_one(index)
        self.assertEqual([row["fund_code"] for row in found], ["027575"])

    def test_official_product_family_does_not_accept_a_similar_fund_family(self):
        index_id = int(self.index["id"])
        self.db.execute(
            """INSERT INTO csi_index_products(
                   investable_index_id,index_code,product_code,fund_name,share_class,
                   strategy_kind,fund_type,source_url,fetched_at,active)
               VALUES(?,?,?,?,?,'被动指数','联接基金','https://www.csindex.com.cn/',
                      '2026-08-09',1)""",
            (
                index_id,
                self.index["official_index_code"],
                "027574",
                "甲基金公司上证科创板芯片设计主题指数证券投资基金",
                "",
            ),
        )
        self.db.execute(
            """INSERT INTO csi_product_sync_status(
                   investable_index_id,state,product_count,attempted_at,last_success_at)
               VALUES(?,'已完成',1,'2026-08-09','2026-08-09')
               ON CONFLICT(investable_index_id) DO UPDATE SET
                   state='已完成',product_count=1,last_success_at='2026-08-09'""",
            (index_id,),
        )
        rows = [{
            "CODE": "027575",
            "NAME": "乙基金公司上证科创板芯片设计主题指数C",
            "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: rows,
            profile_loader=lambda _code: {
                "full_name": "乙基金公司上证科创板芯片设计主题指数证券投资基金",
                "target_index": "名称相近但不是同一指数",
                "benchmark": "名称相近但不是同一指数收益率",
                "source_url": "https://fundf10.eastmoney.com/jbgk_027575.html",
            },
        )
        index = dict(self.index)
        index["official_index_name"] = "科创芯片设计"
        self.assertEqual(service.discover_one(index), [])

    def test_zero_usable_search_candidates_is_not_marked_complete(self):
        rows = [{
            "CODE": "999999", "NAME": "普通主动混合基金",
            "FundBaseInfo": {"FTYPE": "混合型", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db, searcher=lambda _query: rows, profile_loader=lambda _code: {},
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "0条通过"):
            service.sync_one(self.index)
        state = self.db.row(
            "SELECT state,last_error FROM csi_fund_sync_status WHERE investable_index_id=?",
            (self.index["id"],),
        )
        self.assertEqual(state["state"], "失败")
        self.assertIn("0条通过", state["last_error"])

    def test_http_200_empty_profile_is_retryable_not_no_c(self):
        rows = [{
            "CODE": "000001", "NAME": "严格同指数ETF联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db, searcher=lambda _query: rows, profile_loader=lambda _code: {},
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "F10档案缺少"):
            service.sync_one(self.index)
        state = self.db.row(
            "SELECT state,algorithm_version FROM csi_fund_sync_status WHERE investable_index_id=?",
            (self.index["id"],),
        )
        self.assertEqual(state["state"], "失败")
        self.assertEqual(state["algorithm_version"], STRICT_FIELD_SYNC_ALGORITHM_VERSION)
        self.assertEqual(self.catalog.index(self.index["id"])["active"], 1)

    def test_zero_exact_requires_complete_official_snapshot(self):
        self.db.execute(
            "DELETE FROM csi_product_sync_status WHERE investable_index_id=?",
            (self.index["id"],),
        )
        rows = [{
            "CODE": "000001", "NAME": "相似指数ETF联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: rows,
            profile_loader=lambda _code: {
                "full_name": "相似指数交易型开放式指数证券投资基金联接C",
                "target_index": "另一条指数",
                "benchmark": "另一条指数收益率",
                "source_url": "https://fund.example/000001",
            },
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "官网关联产品快照未完整"):
            service.sync_one(self.index)
        self.assertEqual(
            self.db.row(
                "SELECT state FROM csi_fund_sync_status WHERE investable_index_id=?",
                (self.index["id"],),
            )["state"],
            "失败",
        )

    def test_http_200_empty_profile_is_retryable_and_never_becomes_no_c(self):
        rows = [{
            "CODE": "000001", "NAME": "严格同指数ETF联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db, searcher=lambda _query: rows, profile_loader=lambda _code: {},
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "F10档案缺少"):
            service.sync_one(self.index)
        status = self.db.row(
            "SELECT state,algorithm_version FROM csi_fund_sync_status WHERE investable_index_id=?",
            (self.index["id"],),
        )
        self.assertEqual(status["state"], "失败")
        self.assertTrue(status["algorithm_version"])
        self.assertEqual(self.catalog.index(self.index["id"])["active"], 1)

    def test_no_c_requires_a_complete_official_product_snapshot(self):
        rows = [{
            "CODE": "000001", "NAME": "相似指数ETF联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: rows,
            profile_loader=lambda _code: {
                "full_name": "相似指数交易型开放式指数证券投资基金联接C",
                "target_index": "另一条完全不同的指数",
                "benchmark": "另一条完全不同的指数收益率",
                "source_url": "https://fundf10.eastmoney.com/jbgk_000001.html",
            },
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "官网关联产品快照未完整"):
            service.sync_one(self.index)
        self.assertEqual(
            self.db.row(
                "SELECT state FROM csi_fund_sync_status WHERE investable_index_id=?",
                (self.index["id"],),
            )["state"],
            "失败",
        )
        self.assertEqual(self.catalog.index(self.index["id"])["active"], 1)

    def test_eastmoney_https_is_preferred_then_two_hosts_may_use_readonly_http(self):
        fallback_urls = []
        search_payload = {
            "ErrCode": 0,
            "Datas": [{
                "CODE": "000001", "NAME": "严格同指数ETF联接C",
                "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
            }],
        }
        profile_html = f"""
        <table>
          <tr><td>基金全称</td><td>严格同指数交易型开放式指数证券投资基金联接C</td></tr>
          <tr><td>跟踪标的</td><td>{self.index['official_index_name']}</td></tr>
          <tr><td>业绩比较基准</td><td>{self.index['official_index_name']}收益率</td></tr>
        </table>
        """.encode("utf-8")

        def http_only(url):
            fallback_urls.append(url)
            if url.startswith("http://fundsuggest.eastmoney.com/"):
                return json.dumps(search_payload, ensure_ascii=False).encode("utf-8")
            if url == "http://fundf10.eastmoney.com/jbgk_000001.html":
                return profile_html
            raise AssertionError(f"unexpected fallback URL: {url}")

        service = CSIFieldFundSyncService(
            self.db, readonly_http_downloader=http_only, max_workers=2,
        )
        https_attempts = []

        def broken_https(url):
            https_attempts.append(url)
            raise FundDiscoveryError("SSL EOF")

        service.discovery._download = broken_https
        funds = service.discover_one(self.index)
        self.assertEqual([row["fund_code"] for row in funds], ["000001"])
        self.assertTrue(https_attempts)
        self.assertTrue(all(url.startswith("https://") for url in https_attempts))
        self.assertTrue(fallback_urls)
        self.assertTrue(all(
            url.startswith("http://fundsuggest.eastmoney.com/")
            or url.startswith("http://fundf10.eastmoney.com/")
            for url in fallback_urls
        ))

    def test_readonly_relay_is_third_layer_and_parses_json_and_f10_markdown(self):
        search_payload = {
            "ErrCode": 0,
            "Datas": [{
                "CODE": "000001", "NAME": "严格同指数ETF联接C",
                "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
            }],
        }
        http_attempts = []
        relay_attempts = []

        def broken_http(url):
            http_attempts.append(url)
            raise OSError("HTTP route unavailable")

        def relay(relay_url):
            relay_attempts.append(relay_url)
            prefix = "https://r.jina.ai/"
            self.assertTrue(relay_url.startswith(prefix))
            target = relay_url[len(prefix):]
            if target.startswith("http://fundsuggest.eastmoney.com/"):
                content = json.dumps(search_payload, ensure_ascii=False)
            elif target == "http://fundf10.eastmoney.com/jbgk_000001.html":
                content = "\n".join([
                    "| 基金全称 | 严格同指数交易型开放式指数证券投资基金联接C |",
                    "| --- | --- |",
                    f"| 跟踪标的 | {self.index['official_index_name']} |",
                    f"| 业绩比较基准 | {self.index['official_index_name']}收益率 |",
                ])
            else:
                raise AssertionError(f"unexpected relay target: {target}")
            return (
                f"Title: public page\nURL Source: {target}\nMarkdown Content:\n{content}"
            ).encode("utf-8")

        service = CSIFieldFundSyncService(
            self.db,
            readonly_http_downloader=broken_http,
            readonly_relay_downloader=relay,
            max_workers=2,
        )
        service.discovery._download = lambda _url: (_ for _ in ()).throw(
            FundDiscoveryError("HTTPS route unavailable")
        )
        funds = service.discover_one(self.index)
        self.assertEqual([row["fund_code"] for row in funds], ["000001"])
        self.assertEqual(
            funds[0]["source_url"],
            "http://fundf10.eastmoney.com/jbgk_000001.html",
        )
        self.assertTrue(http_attempts)
        self.assertTrue(relay_attempts)
        self.assertTrue(all(url.startswith("https://r.jina.ai/http://") for url in relay_attempts))

    def test_readonly_relay_rejects_tampered_url_source(self):
        relay_calls = []

        def tampered(relay_url):
            relay_calls.append(relay_url)
            return (
                "Title: tampered\n"
                "URL Source: http://fundsuggest.eastmoney.com/FundSearch/api/"
                "FundSearchAPI.ashx?m=1&key=other\n"
                "Markdown Content:\n{\"ErrCode\":0,\"Datas\":[]}"
            ).encode("utf-8")

        service = CSIFieldFundSyncService(
            self.db,
            readonly_http_downloader=lambda _url: (_ for _ in ()).throw(OSError("HTTP down")),
            readonly_relay_downloader=tampered,
        )
        service.discovery._download = lambda _url: (_ for _ in ()).throw(
            FundDiscoveryError("HTTPS down")
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "URL Source与请求目标不一致"):
            service._search("requested")
        self.assertEqual(len(relay_calls), 1)

    def test_readonly_http_fallback_rejects_every_non_whitelisted_host(self):
        called = []
        relay_called = []
        service = CSIFieldFundSyncService(
            self.db, readonly_http_downloader=lambda url: called.append(url) or b"bad",
            readonly_relay_downloader=lambda url: relay_called.append(url) or b"bad",
        )
        with self.assertRaisesRegex(FundCatalogSyncError, "固定主机"):
            service._download_public_readonly("https://example.com/public.json")
        self.assertEqual(called, [])
        self.assertEqual(relay_called, [])

    def test_v21_backfills_strict_c_execution_rows_to_compatibility_mapping(self):
        rows = self.db.rows(
            """SELECT i.universe_id,f.fund_code
                 FROM investable_indices i JOIN investable_funds f ON f.investable_index_id=i.id
                WHERE i.active=1 AND i.catalog_role='日扫代表' AND f.active=1
                  AND f.relation_kind='同一指数' AND UPPER(f.share_class)='C'
                  AND f.strategy_kind IN ('被动ETF联接','被动指数')
                  AND f.passive_verified=1
                  AND f.public_buyable_status IN
                      ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')"""
        )
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(self.db.row(
                "SELECT 1 FROM fund_mappings WHERE universe_id=? AND fund_code=?",
                (row["universe_id"], row["fund_code"]),
            ))

    def test_execution_gate_rejects_a_class_similar_and_enhanced_rows(self):
        index_id = int(self.index["id"])
        rows = [
            ("100001", "A类被动", "被动指数", "A", 1, "同一指数"),
            ("100002", "C类相似", "被动指数", "C", 1, "相似指数"),
            ("100003", "C类增强", "指数增强", "C", 0, "同一指数"),
            ("100004", "C类场内ETF", "场内ETF", "C", 1, "同一指数"),
        ]
        with self.db.connect() as conn:
            conn.executemany(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,active,passive_verified,radar_eligible,relation_kind)
                   VALUES(?,?,?,?,?,'公开渠道可申购',1,?,0,?)""",
                [(index_id, *row) for row in rows],
            )
        self.catalog.refresh_admission(index_id)
        funds = self.catalog.funds(index_id)
        self.assertTrue(funds)
        self.assertTrue(all(row["radar_eligible"] == 0 for row in funds))

    def test_failed_c_resync_keeps_existing_exact_mapping(self):
        index_id = int(self.index["id"])
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,active,passive_verified,radar_eligible,relation_kind)
               VALUES(?,'000001','既有C类','被动指数','C','公开渠道可申购',1,1,0,'同一指数')""",
            (index_id,),
        )

        def broken(_query):
            raise FundDiscoveryError("network down")

        service = CSIFieldFundSyncService(self.db, searcher=broken, profile_loader=lambda _code: {})
        with self.assertRaises(FundCatalogSyncError):
            service.sync_one(self.index)
        self.assertEqual(
            self.db.row(
                "SELECT active FROM investable_funds WHERE investable_index_id=? AND fund_code='000001'",
                (index_id,),
            )["active"],
            1,
        )

    def test_partial_profile_failure_is_retryable_and_keeps_old_mapping(self):
        index_id = int(self.index["id"])
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,active,passive_verified,radar_eligible,relation_kind,
                   source_note)
               VALUES(?,'000099','既有C类','被动指数','C','公开渠道可申购',1,1,0,
                      '同一指数',?)""",
            (index_id, AUTOMATIC_FIELD_SOURCE_NOTE),
        )
        rows = [
            {"CODE": "000001", "NAME": "同指数ETF联接C", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
            {"CODE": "000002", "NAME": "另一同指数ETF联接C", "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"}},
        ]

        def profile(code):
            if code == "000002":
                raise OSError("temporary profile failure")
            return {
                "full_name": "同指数ETF联接C",
                "target_index": self.index["official_index_name"],
                "source_url": "https://example.com/000001",
            }

        service = CSIFieldFundSyncService(
            self.db, searcher=lambda _query: rows, profile_loader=profile, max_workers=2,
        )
        with self.assertRaises(FundCatalogSyncError):
            service.sync_one(self.index)
        old = self.db.row(
            "SELECT active FROM investable_funds WHERE investable_index_id=? AND fund_code='000099'",
            (index_id,),
        )
        self.assertEqual(old["active"], 1)
        self.assertEqual(
            self.db.row(
                "SELECT state FROM csi_fund_sync_status WHERE investable_index_id=?",
                (index_id,),
            )["state"],
            "失败",
        )

    def test_successful_c_refresh_retires_only_old_automatic_rows(self):
        index_id = int(self.index["id"])
        with self.db.connect() as conn:
            conn.executemany(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,active,passive_verified,radar_eligible,relation_kind,
                       source_note)
                   VALUES(?,?,?,?,?,'公开渠道可申购',1,1,0,'同一指数',?)""",
                [
                    (index_id, "000098", "旧自动C", "被动指数", "C", AUTOMATIC_FIELD_SOURCE_NOTE),
                    (index_id, "000099", "用户核验C", "被动指数", "C", "用户人工核验"),
                ],
            )
        rows = [{
            "CODE": "000001", "NAME": "新同指数ETF联接C",
            "FundBaseInfo": {"FTYPE": "指数型-股票", "ISBUY": "1"},
        }]
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: rows,
            profile_loader=lambda _code: {
                "full_name": "新同指数ETF联接C",
                "target_index": self.index["official_index_name"],
                "benchmark": f"{self.index['official_index_name']}收益率",
                "source_url": "https://example.com/000001",
            },
        )
        self.assertEqual(service.sync_one(self.index), 1)
        states = {
            row["fund_code"]: row["active"]
            for row in self.db.rows(
                "SELECT fund_code,active FROM investable_funds WHERE investable_index_id=?",
                (index_id,),
            )
        }
        self.assertEqual(states["000098"], 0)
        self.assertEqual(states["000099"], 1)
        self.assertEqual(states["000001"], 1)

    def test_blank_relation_is_never_executable(self):
        index_id = int(self.index["id"])
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,active,passive_verified,radar_eligible,relation_kind)
               VALUES(?,'100009','关系未核验C','被动指数','C','公开渠道可申购',1,1,0,'')""",
            (index_id,),
        )
        self.catalog.refresh_admission(index_id)
        self.assertEqual(
            self.db.row(
                "SELECT radar_eligible FROM investable_funds WHERE investable_index_id=? AND fund_code='100009'",
                (index_id,),
            )["radar_eligible"],
            0,
        )


if __name__ == "__main__":
    unittest.main()

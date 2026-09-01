from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import BACKUP_DIR, DB_PATH, local_now, local_today
from .csi_catalog import (
    CSI_METHODOLOGY_URL,
    CSI_METHODOLOGY_VERSION,
    CSI_FUND_SEEDS,
    CSI_BY_CODE,
    CSI_DAILY_TARGETS,
    CSI_PARENT_REFERENCES,
    CSI_REPRESENTATIVES,
    CSI_REPRESENTATIVE_VERSION,
)
from .market_data import TEST20_IDS, default_proxy_mappings
from .universe import UNIVERSE_VERSION, core_universe
from .catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from .runtime_housekeeping import (
    automatic_backup_retention_plan,
    deployment_backup_retention_plan,
    prune_automatic_backup_sidecars,
    prune_deployment_backup_sidecars,
    valid_automatic_backup_file,
    valid_deployment_backup_file,
)


SCHEMA_VERSION = 39


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL is a persistent database-file setting.  Reissuing
        # ``PRAGMA journal_mode=WAL`` on every short-lived read connection can
        # wait for other readers/writers and used to create random 0.5-1s UI
        # stalls.  Establish it once when the Database object starts instead.
        wal_conn = sqlite3.connect(self.path, timeout=15)
        try:
            wal_conn.execute("PRAGMA journal_mode = WAL").fetchone()
            wal_conn.commit()
        finally:
            # sqlite3.Connection.__exit__ commits but does not close.  An
            # explicit close is required on Windows or temporary/restored
            # database files remain locked for the lifetime of this object.
            wal_conn.close()
        self._initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -16384")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS funds (
                    fund_code TEXT PRIMARY KEY,
                    fund_name TEXT NOT NULL,
                    index_code TEXT DEFAULT '',
                    index_name TEXT DEFAULT '',
                    share_class TEXT DEFAULT '',
                    correlation_bucket TEXT DEFAULT '',
                    purchase_fee_pct REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '可买',
                    notes TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fee_tiers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL REFERENCES funds(fund_code) ON DELETE CASCADE,
                    min_days INTEGER NOT NULL DEFAULT 0,
                    max_days INTEGER,
                    redeem_fee_pct REAL NOT NULL,
                    UNIQUE(fund_code, min_days)
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL REFERENCES funds(fund_code),
                    confirmed_date TEXT NOT NULL,
                    units REAL NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    latest_nav REAL NOT NULL DEFAULT 0,
                    nav_date TEXT DEFAULT '',
                    sleeve TEXT NOT NULL DEFAULT '主动',
                    thesis TEXT DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    fund_code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    units REAL NOT NULL DEFAULT 0,
                    nav REAL NOT NULL DEFAULT 0,
                    fee REAL NOT NULL DEFAULT 0,
                    rationale TEXT DEFAULT '',
                    ai_advice TEXT DEFAULT '',
                    result_pnl REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe (
                    index_code TEXT PRIMARY KEY,
                    index_name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '行业主题',
                    has_buyable_fund INTEGER NOT NULL DEFAULT 0,
                    fund_code TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待核查',
                    notes TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS radar_runs (
                    task_id TEXT PRIMARY KEY,
                    data_cutoff TEXT NOT NULL,
                    market_regime TEXT NOT NULL,
                    universe_total INTEGER NOT NULL DEFAULT 0,
                    analyzed_total INTEGER NOT NULL DEFAULT 0,
                    ranking_policy_version TEXT NOT NULL DEFAULT '',
                    ranking_policy_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT DEFAULT '',
                    raw_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES radar_runs(task_id) ON DELETE CASCADE,
                    rank_no INTEGER NOT NULL DEFAULT 0,
                    index_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    fund_code TEXT DEFAULT '',
                    fund_name TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT '观察',
                    score REAL NOT NULL DEFAULT 0,
                    target_allocation_pct REAL NOT NULL DEFAULT 0,
                    suggested_amount REAL NOT NULL DEFAULT 0,
                    correlation_bucket TEXT DEFAULT '',
                    thesis TEXT DEFAULT '',
                    catalyst TEXT DEFAULT '',
                    crowding TEXT DEFAULT '',
                    risk TEXT DEFAULT '',
                    buy_conditions TEXT DEFAULT '',
                    abandon_conditions TEXT DEFAULT '',
                    current_change REAL,
                    open_change REAL,
                    high_change REAL,
                    low_change REAL,
                    turnover REAL,
                    short_trend TEXT DEFAULT '',
                    manual_note TEXT DEFAULT '',
                    decision TEXT DEFAULT '待输入',
                    decision_reason TEXT DEFAULT '',
                    research_tilt TEXT NOT NULL DEFAULT '观察',
                    timing_status TEXT NOT NULL DEFAULT '等待回调',
                    execution_status TEXT NOT NULL DEFAULT '不可执行',
                    opportunity_score REAL NOT NULL DEFAULT 0,
                    entry_score REAL NOT NULL DEFAULT 0,
                    crowding_risk_score REAL NOT NULL DEFAULT 0,
                    data_confidence REAL NOT NULL DEFAULT 0,
                    pool_route TEXT NOT NULL DEFAULT '',
                    suppression_reason TEXT NOT NULL DEFAULT '',
                    execution_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS radar_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES radar_runs(task_id) ON DELETE CASCADE,
                    universe_id TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    proxy_code TEXT NOT NULL,
                    proxy_name TEXT NOT NULL,
                    d1 REAL NOT NULL,
                    d3 REAL NOT NULL,
                    d5 REAL NOT NULL,
                    d10 REAL NOT NULL,
                    d20 REAL NOT NULL,
                    volume_signal TEXT NOT NULL,
                    breadth_signal TEXT NOT NULL,
                    position_20d TEXT NOT NULL,
                    crowding TEXT NOT NULL,
                    catalyst TEXT DEFAULT '',
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT DEFAULT '',
                    local_rank INTEGER NOT NULL DEFAULT 0,
                    pool_route TEXT NOT NULL DEFAULT '',
                    research_tilt TEXT NOT NULL DEFAULT '观察',
                    timing_status TEXT NOT NULL DEFAULT '等待回调',
                    execution_status TEXT NOT NULL DEFAULT '研究完成',
                    opportunity_score REAL NOT NULL DEFAULT 0,
                    trend_score REAL NOT NULL DEFAULT 0,
                    entry_score REAL NOT NULL DEFAULT 0,
                    crowding_risk_score REAL NOT NULL DEFAULT 0,
                    data_confidence REAL NOT NULL DEFAULT 0,
                    recommendation_score REAL NOT NULL DEFAULT 0,
                    score_components_json TEXT NOT NULL DEFAULT '{}',
                    local_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, universe_id)
                );
                CREATE TABLE IF NOT EXISTS ai_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imported_at TEXT NOT NULL,
                    task_id TEXT DEFAULT '',
                    validation_status TEXT NOT NULL,
                    errors TEXT DEFAULT '',
                    raw_text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    mode TEXT NOT NULL DEFAULT '完整扫描',
                    stage TEXT NOT NULL DEFAULT '等待',
                    status TEXT NOT NULL DEFAULT '等待',
                    total_items INTEGER NOT NULL DEFAULT 0,
                    completed_items INTEGER NOT NULL DEFAULT 0,
                    cost_limit_cny REAL NOT NULL DEFAULT 0,
                    estimated_cost_cny REAL NOT NULL DEFAULT 0,
                    universe_version TEXT NOT NULL DEFAULT '',
                    research_date TEXT NOT NULL DEFAULT '',
                    universe_ids_json TEXT NOT NULL DEFAULT '[]',
                    universe_hash TEXT NOT NULL DEFAULT '',
                    ranking_policy_version TEXT NOT NULL DEFAULT '',
                    ranking_policy_json TEXT NOT NULL DEFAULT '{}',
                    model_config_json TEXT NOT NULL DEFAULT '{}',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    lease_heartbeat_at TEXT NOT NULL DEFAULT '',
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sector_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    proxy_code TEXT NOT NULL,
                    proxy_name TEXT NOT NULL,
                    d1 REAL NOT NULL,
                    d3 REAL NOT NULL,
                    d5 REAL NOT NULL,
                    d10 REAL NOT NULL,
                    d20 REAL NOT NULL,
                    volume_signal TEXT NOT NULL,
                    breadth_signal TEXT NOT NULL,
                    position_20d TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    base_score REAL NOT NULL DEFAULT 0,
                    score_components_json TEXT NOT NULL DEFAULT '{}',
                    volatility_20d REAL,
                    consecutive_up_days INTEGER,
                    breadth_kind TEXT NOT NULL DEFAULT 'unknown',
                    breadth_pct REAL,
                    expected_data_cutoff TEXT NOT NULL DEFAULT '',
                    freshness_state TEXT NOT NULL DEFAULT 'unknown',
                    UNIQUE(task_id, universe_id)
                );
                CREATE TABLE IF NOT EXISTS sector_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    crowding TEXT NOT NULL DEFAULT '',
                    crowding_level REAL,
                    catalyst TEXT NOT NULL DEFAULT '',
                    catalyst_strength REAL,
                    risk TEXT NOT NULL DEFAULT '',
                    source_json TEXT NOT NULL DEFAULT '[]',
                    web_search_calls INTEGER NOT NULL DEFAULT 0,
                    evidence_date TEXT NOT NULL DEFAULT '',
                    universe_version TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, universe_id)
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    web_search_calls INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_cny REAL NOT NULL DEFAULT 0,
                    response_kind TEXT NOT NULL DEFAULT '',
                    response_status TEXT NOT NULL DEFAULT '',
                    finish_reason TEXT NOT NULL DEFAULT '',
                    response_id TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    request_context_json TEXT NOT NULL DEFAULT '{}',
                    request_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fund_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    universe_id TEXT NOT NULL,
                    index_code TEXT DEFAULT '',
                    fund_code TEXT NOT NULL,
                    fund_name TEXT NOT NULL,
                    share_class TEXT DEFAULT '',
                    buyable_status TEXT NOT NULL DEFAULT '待核验',
                    source_url TEXT DEFAULT '',
                    verified_at TEXT DEFAULT '',
                    UNIQUE(universe_id, fund_code)
                );
                CREATE TABLE IF NOT EXISTS proxy_mappings (
                    universe_id TEXT PRIMARY KEY REFERENCES universe(index_code),
                    provider TEXT NOT NULL,
                    security_id TEXT NOT NULL,
                    proxy_code TEXT NOT NULL,
                    proxy_name TEXT NOT NULL,
                    mapping_kind TEXT NOT NULL DEFAULT '待核验',
                    confidence REAL NOT NULL DEFAULT 0,
                    rationale TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    reviewed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS market_bars (
                    provider TEXT NOT NULL,
                    security_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    close REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(provider, security_id, trade_date)
                );
                CREATE TABLE IF NOT EXISTS market_fetch_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    security_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recurring_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL REFERENCES funds(fund_code),
                    amount REAL NOT NULL,
                    frequency TEXT NOT NULL DEFAULT '交易日',
                    weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4',
                    start_date TEXT NOT NULL,
                    end_date TEXT DEFAULT '',
                    purchase_fee_pct REAL,
                    auto_sync INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recurring_occurrences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES recurring_plans(id) ON DELETE CASCADE,
                    fund_code TEXT NOT NULL DEFAULT '',
                    scheduled_date TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    pricing_date TEXT DEFAULT '',
                    confirm_date TEXT DEFAULT '',
                    amount REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    nav REAL NOT NULL DEFAULT 0,
                    units REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '待扣款核对',
                    source TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    fetched_at TEXT DEFAULT '',
                    trade_id INTEGER REFERENCES trades(id),
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, scheduled_date)
                );
                CREATE TABLE IF NOT EXISTS cost_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL REFERENCES funds(fund_code),
                    trade_id INTEGER REFERENCES trades(id),
                    occurrence_id INTEGER REFERENCES recurring_occurrences(id),
                    confirmed_date TEXT NOT NULL,
                    units REAL NOT NULL,
                    remaining_units REAL NOT NULL,
                    gross_amount REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    net_invested REAL NOT NULL,
                    nav REAL NOT NULL,
                    source TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(occurrence_id)
                );
                CREATE TABLE IF NOT EXISTS fund_nav_history (
                    fund_code TEXT NOT NULL REFERENCES funds(fund_code) ON DELETE CASCADE,
                    nav_date TEXT NOT NULL,
                    nav REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(fund_code, nav_date)
                );
                CREATE TABLE IF NOT EXISTS fund_valuation_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    official_value REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    official_pnl REAL NOT NULL,
                    nav_dates_json TEXT NOT NULL DEFAULT '{}',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS ledger_postings (
                    trade_id INTEGER PRIMARY KEY REFERENCES trades(id),
                    posting_kind TEXT NOT NULL,
                    critical_snapshot_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    posted_at TEXT NOT NULL,
                    reversed_at TEXT NOT NULL DEFAULT '',
                    reversal_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS lot_consumptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exit_trade_id INTEGER NOT NULL REFERENCES trades(id),
                    sequence_no INTEGER NOT NULL,
                    lot_id INTEGER REFERENCES cost_lots(id),
                    fund_code TEXT NOT NULL,
                    confirmed_date TEXT NOT NULL DEFAULT '',
                    units REAL NOT NULL,
                    cost_basis REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(exit_trade_id, sequence_no)
                );
                CREATE TABLE IF NOT EXISTS active_cash_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_kind TEXT NOT NULL,
                    trade_id INTEGER REFERENCES trades(id),
                    amount REAL NOT NULL,
                    balance_after REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS portfolio_equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    holdings_value REAL NOT NULL,
                    active_cash REAL NOT NULL,
                    total_equity REAL NOT NULL,
                    external_flow_total REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS recommendation_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    source_candidate_id INTEGER,
                    universe_id TEXT NOT NULL,
                    index_code TEXT NOT NULL DEFAULT '',
                    sector_name TEXT NOT NULL,
                    fund_code TEXT NOT NULL DEFAULT '',
                    fund_name TEXT NOT NULL DEFAULT '',
                    fund_selection_kind TEXT NOT NULL DEFAULT '',
                    rank_no INTEGER NOT NULL DEFAULT 0,
                    recommendation_score REAL NOT NULL DEFAULT 0,
                    research_tilt TEXT NOT NULL DEFAULT '观察',
                    timing_status TEXT NOT NULL DEFAULT '',
                    execution_status TEXT NOT NULL DEFAULT '',
                    ranking_policy_version TEXT NOT NULL DEFAULT '',
                    recommendation_at TEXT NOT NULL,
                    data_cutoff TEXT NOT NULL DEFAULT '',
                    pricing_date TEXT NOT NULL,
                    index_provider TEXT NOT NULL DEFAULT '',
                    index_security_id TEXT NOT NULL DEFAULT '',
                    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT '观察中',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, universe_id)
                );
                CREATE TABLE IF NOT EXISTS recommendation_outcomes (
                    observation_id INTEGER NOT NULL REFERENCES recommendation_observations(id) ON DELETE CASCADE,
                    horizon_sessions INTEGER NOT NULL,
                    target_date TEXT NOT NULL,
                    actual_end_date TEXT NOT NULL DEFAULT '',
                    holding_calendar_days INTEGER,
                    index_start_value REAL,
                    index_end_value REAL,
                    index_return_pct REAL,
                    index_max_drawdown_pct REAL,
                    fund_start_nav REAL,
                    fund_end_nav REAL,
                    fund_gross_return_pct REAL,
                    purchase_fee_pct REAL,
                    redemption_fee_pct REAL,
                    fund_net_return_pct REAL,
                    fund_max_drawdown_pct REAL,
                    index_direction_hit INTEGER,
                    fund_direction_hit INTEGER,
                    status TEXT NOT NULL DEFAULT '等待行情',
                    reason TEXT NOT NULL DEFAULT '',
                    evaluated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(observation_id, horizon_sessions)
                );
                """
            )
            stored_schema_row = conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            if stored_schema_row is None:
                stored_schema_version = 1
                self._set_default(conn, "schema_version", "1")
            else:
                try:
                    stored_schema_version = int(str(stored_schema_row[0]).strip())
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("数据库版本标记损坏；为保护用户账本，软件已停止自动迁移。") from exc
            if stored_schema_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本{stored_schema_version}高于当前软件支持的{SCHEMA_VERSION}；"
                    "为保护用户账本，软件不会降级写入。"
                )

            defaults = {
                "active_capital_limit": "8000",
                "single_position_limit": "2000",
                "correlation_bucket_limit": "3000",
                "max_active_themes": "4",
                "soft_stop_gross_pct": "-2.5",
                "hard_stop_gross_pct": "-4.0",
                "portfolio_pause_pct": "-6.0",
                "portfolio_stop_pct": "-10.0",
                "consecutive_loss_pause": "3",
                "internal_cutoff": "14:52",
                "risk_profile": "高风险承受；仅基金；不买个股、不买场内；主动资金上限8000元；允许不满仓。",
                "universe_version": UNIVERSE_VERSION,
                "deepseek_base_url": "https://api.deepseek.com",
                "deepseek_flash_model": "deepseek-v4-flash",
                "deepseek_pro_model": "deepseek-v4-pro",
                "deepseek_api_key_dpapi": "",
                "api_test_cost_limit_cny": "0.30",
                "api_pro_test_cost_limit_cny": "0.10",
                "api_full_cost_limit_cny": "2.00",
                "api_action_cost_limit_cny": "0.50",
                "flash_input_price_cny_per_million": "1.0",
                "flash_cached_input_price_cny_per_million": "0.02",
                "flash_output_price_cny_per_million": "2.0",
                "pro_input_price_cny_per_million": "3.0",
                "pro_cached_input_price_cny_per_million": "0.025",
                "pro_output_price_cny_per_million": "6.0",
                "deepseek_pricing_revision": "official-v4-2026-08-15",
                "deepseek_pricing_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
                "deepseek_v4_context_tokens": "1000000",
                "deepseek_v4_max_output_tokens": "384000",
                "api_timeout_seconds": "360",
                "api_test_batch_size": "3",
                "api_full_batch_size": "6",
                "api_test_max_output_tokens": "6000",
                "api_full_flash_max_output_tokens": "12000",
                "api_test_flash_reasoning_effort": "none",
                "api_full_flash_reasoning_effort": "low",
                "api_auto_finalize_flash": "1",
                "flash_reasoning_effort": "low",
                "api_pro_max_tokens": "12000",
                "api_pro_hard_output_cap_tokens": "12000",
                "pro_reasoning_effort": "max",
                "market_timeout_seconds": "20",
                "market_max_stale_calendar_days": "7",
                "market_primary_provider": "eastmoney_public",
                "recurring_nav_provider": "eastmoney_f10_public",
                "recurring_auto_sync_hours": "6",
                "recurring_last_sync_at": "",
                "recurring_last_sync_error": "",
                "fund_nav_lookback_days": "14",
                "fund_nav_last_sync_at": "",
                "fund_nav_last_sync_error": "",
                "fund_execution_refresh_workers": "6",
                "backup_keep_count": "14",
                "backup_keep_days": "30",
                "deployment_backup_keep_count": "3",
                "deployment_backup_keep_days": "30",
                "rollback_keep_count": "3",
                "rollback_keep_days": "30",
                "diagnostic_keep_count": "20",
                "diagnostic_keep_days": "180",
            }
            for key, value in defaults.items():
                self._set_default(conn, key, value)
            if conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0] == 0:
                self._seed(conn)
            migrations = [
                (2, self._migrate_v2), (3, self._migrate_v3), (4, self._migrate_v4),
                (5, self._migrate_v5), (6, self._migrate_v6), (7, self._migrate_v7),
                (8, self._migrate_v8), (9, self._migrate_v9), (10, self._migrate_v10),
                (11, self._migrate_v11), (12, self._migrate_v12), (13, self._migrate_v13),
                (14, self._migrate_v14), (15, self._migrate_v15), (16, self._migrate_v16),
                (17, self._migrate_v17), (18, self._migrate_v18), (19, self._migrate_v19),
                (20, self._migrate_v20), (21, self._migrate_v21), (22, self._migrate_v22),
                (23, self._migrate_v23), (24, self._migrate_v24),
                (25, self._migrate_v25), (26, self._migrate_v26),
                (27, self._migrate_v27), (28, self._migrate_v28),
                (29, self._migrate_v29), (30, self._migrate_v30),
                (31, self._migrate_v31),
                (32, self._migrate_v32),
                (33, self._migrate_v33),
                (34, self._migrate_v34),
                (35, self._migrate_v35),
                (36, self._migrate_v36),
                (37, self._migrate_v37),
                (38, self._migrate_v38),
                (39, self._migrate_v39),
            ]
            # Use the version captured before migration.  Older migration methods
            # historically wrote the then-current constant, so re-reading the
            # setting between steps would skip required intermediate upgrades.
            for target_version, migration in migrations:
                if stored_schema_version < target_version:
                    migration(conn)
            conn.execute(
                "UPDATE settings SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _set_default(conn, key: str, value: str):
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))

    def _seed(self, conn):
        """Leave every new user's portfolio and transaction history empty.

        Public reference indices and fund mappings are installed by the catalog
        migrations below.  Runtime holdings, recurring plans and trades are
        always created by the user, never by application bootstrap.
        """

    def _migrate_v2(self, conn):
        now = local_now().isoformat(timespec="seconds")
        self._set_default(conn, "schema_version", str(SCHEMA_VERSION))
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        if conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0] == 0:
            conn.executemany(
                """INSERT INTO universe(index_code,index_name,category,has_buyable_fund,fund_code,status,notes)
                   VALUES (?,?,?,0,'','待映射','v2.0.0固定研究宇宙')""",
                [(row["index_code"], row["index_name"], row["category"]) for row in core_universe()],
            )

    @staticmethod
    def _ensure_column(conn, table: str, column: str, declaration: str):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _migrate_v3(self, conn):
        now = local_now().isoformat(timespec="seconds")
        for name, declaration in [
            ("data_cutoff", "TEXT NOT NULL DEFAULT ''"),
            ("mapping_kind", "TEXT NOT NULL DEFAULT ''"),
            ("mapping_confidence", "REAL NOT NULL DEFAULT 0"),
            ("amount_ratio_5d", "REAL NOT NULL DEFAULT 0"),
            ("data_quality", "TEXT NOT NULL DEFAULT ''"),
            ("cache_hit", "INTEGER NOT NULL DEFAULT 0"),
            ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            self._ensure_column(conn, "sector_metrics", name, declaration)
        conn.executemany(
            """INSERT OR IGNORE INTO proxy_mappings(
                   universe_id,provider,security_id,proxy_code,proxy_name,mapping_kind,
                   confidence,rationale,active,reviewed_at
               ) VALUES (?,?,?,?,?,?,?,?,1,?)""",
            [
                (m.universe_id, m.provider, m.security_id, m.proxy_code, m.proxy_name,
                 m.mapping_kind, m.confidence, m.rationale, now)
                for m in default_proxy_mappings()
            ],
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v4(self, conn):
        """Repair the exact alpha.5 defaults without overwriting user edits."""
        now = local_now().isoformat(timespec="seconds")
        fixes = [
            ("U010", "1.932000", "2.932000", "目标就是中证2000；东方财富统一行情市场前缀为2。"),
            ("U011", "1.930050", "2.930050", "目标就是中证A50；东方财富统一行情市场前缀为2。"),
        ]
        for universe_id, old_security_id, new_security_id, rationale in fixes:
            conn.execute(
                """UPDATE proxy_mappings
                   SET security_id=?, rationale=?, reviewed_at=?
                   WHERE universe_id=? AND provider='eastmoney_public' AND security_id=?""",
                (new_security_id, rationale, now, universe_id, old_security_id),
            )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v5(self, conn):
        """Add auditable recurring-investment fields without altering old rows."""
        for name, declaration in [
            ("confirm_date", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT '已确认'"),
            ("source", "TEXT NOT NULL DEFAULT '手工录入'"),
            ("nav_date", "TEXT NOT NULL DEFAULT ''"),
            ("source_url", "TEXT NOT NULL DEFAULT ''"),
            ("recurring_occurrence_id", "INTEGER"),
        ]:
            self._ensure_column(conn, "trades", name, declaration)
        self._ensure_column(conn, "recurring_occurrences", "fund_code", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """UPDATE recurring_occurrences SET fund_code=(
                   SELECT fund_code FROM recurring_plans WHERE recurring_plans.id=recurring_occurrences.plan_id
               ) WHERE fund_code=''"""
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v6(self, conn):
        """Add complete Flash/Pro call auditing and repair only exact old API defaults."""
        for name, declaration in [
            ("response_kind", "TEXT NOT NULL DEFAULT ''"),
            ("response_status", "TEXT NOT NULL DEFAULT ''"),
            ("finish_reason", "TEXT NOT NULL DEFAULT ''"),
            ("response_id", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "model_calls", name, declaration)
        replacements = {
            "api_timeout_seconds": ("120", "360"),
            "pro_input_price_cny_per_million": ("3.0", "3.15"),
            "pro_output_price_cny_per_million": ("6.0", "6.30"),
            "api_test_max_output_tokens": ("7000", "12000"),
            "api_full_flash_max_output_tokens": ("12000", "24000"),
        }
        for key, (old, new) in replacements.items():
            conn.execute("UPDATE settings SET value=? WHERE key=? AND value=?", (new, key, old))
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v7(self, conn):
        """Record provider-level market transport diagnostics for safe export."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS market_fetch_attempts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   task_id TEXT NOT NULL,
                   universe_id TEXT NOT NULL,
                   security_id TEXT NOT NULL,
                   provider TEXT NOT NULL,
                   outcome TEXT NOT NULL,
                   detail TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v8(self, conn):
        """Make Flash tool continuations auditable and reduce per-call blast radius."""
        self._ensure_column(conn, "model_calls", "request_context_json", "TEXT NOT NULL DEFAULT '{}'")
        # alpha.13 did not store batch IDs.  Backfill only the newest recoverable
        # failed 20-direction call from its fixed order and old exact batch size,
        # so the user's already-paid search can be finalized once after upgrade.
        old_size_row = conn.execute("SELECT value FROM settings WHERE key='api_test_batch_size'").fetchone()
        try:
            old_test_size = int(float(old_size_row[0])) if old_size_row else 5
        except (TypeError, ValueError):
            old_test_size = 5
        if old_test_size == 5:
            jobs = conn.execute(
                """SELECT task_id FROM analysis_jobs
                   WHERE mode='20方向测试' AND status IN ('失败','已停止') ORDER BY id"""
            ).fetchall()
            for job in jobs:
                task_id = job[0]
                latest = conn.execute(
                    """SELECT id,request_context_json,response_status,web_search_calls FROM model_calls
                       WHERE task_id=? AND stage='Flash联网检索' ORDER BY id DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if not latest or (latest[1] or "{}").strip() not in {"", "{}"}:
                    continue
                if latest[2] != "completed" or int(latest[3] or 0) < 1:
                    continue
                completed_ids = {
                    row[0] for row in conn.execute(
                        "SELECT universe_id FROM sector_evidence WHERE task_id=?", (task_id,)
                    )
                }
                pending_ids = [uid for uid in TEST20_IDS if uid not in completed_ids]
                if not pending_ids:
                    continue
                context = {
                    "phase": "search",
                    "batch_ids": pending_ids[:old_test_size],
                    "legacy_inferred": "alpha13_fixed_test_order",
                }
                conn.execute(
                    "UPDATE model_calls SET request_context_json=? WHERE id=?",
                    (json.dumps(context, ensure_ascii=False), latest[0]),
                )
        replacements = {
            "api_test_batch_size": ("5", "3"),
            "api_full_batch_size": ("12", "6"),
            "api_test_max_output_tokens": ("12000", "6000"),
            "api_full_flash_max_output_tokens": ("24000", "12000"),
        }
        for key, (old, new) in replacements.items():
            conn.execute("UPDATE settings SET value=? WHERE key=? AND value=?", (new, key, old))
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v9(self, conn):
        """Store published NAV history and auditable button-time valuations."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fund_nav_history (
                fund_code TEXT NOT NULL REFERENCES funds(fund_code) ON DELETE CASCADE,
                nav_date TEXT NOT NULL,
                nav REAL NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(fund_code, nav_date)
            );
            CREATE TABLE IF NOT EXISTS fund_valuation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                official_value REAL NOT NULL,
                total_cost REAL NOT NULL,
                official_pnl REAL NOT NULL,
                nav_dates_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT ''
            );
            """
        )
        for key, value in {
            "fund_nav_lookback_days": "14",
            "fund_nav_last_sync_at": "",
            "fund_nav_last_sync_error": "",
        }.items():
            self._set_default(conn, key, value)
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v10(self, conn):
        """Store actionable buy allocations and per-holding sell percentages."""
        for name, declaration in [
            ("universe_id", "TEXT NOT NULL DEFAULT ''"),
            ("allocation_pct", "REAL NOT NULL DEFAULT 0"),
        ]:
            self._ensure_column(conn, "candidates", name, declaration)
        for name, declaration in [
            ("action_summary", "TEXT NOT NULL DEFAULT ''"),
            ("no_buy_reason", "TEXT NOT NULL DEFAULT ''"),
            ("action_json", "TEXT NOT NULL DEFAULT '{}'") ,
            ("action_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "radar_runs", name, declaration)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS holding_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES radar_runs(task_id) ON DELETE CASCADE,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL DEFAULT '',
                sleeve TEXT NOT NULL DEFAULT '',
                market_value REAL NOT NULL DEFAULT 0,
                position_pct_active REAL NOT NULL DEFAULT 0,
                action TEXT NOT NULL DEFAULT '持有',
                sell_pct REAL,
                estimated_sell_amount REAL,
                reason TEXT NOT NULL DEFAULT '',
                risk TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, fund_code)
            );
            """
        )
        self._set_default(conn, "api_action_cost_limit_cny", "0.50")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v11(self, conn):
        """Cache zero-API fund discovery and deterministic comparison results."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fund_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_id TEXT NOT NULL,
                sector_name TEXT NOT NULL,
                proxy_code TEXT NOT NULL DEFAULT '',
                proxy_name TEXT NOT NULL DEFAULT '',
                proxy_kind TEXT NOT NULL DEFAULT '',
                holding_days INTEGER NOT NULL DEFAULT 30,
                query_terms_json TEXT NOT NULL DEFAULT '[]',
                source_note TEXT NOT NULL DEFAULT '',
                warning TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fund_discovery_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES fund_discovery_runs(id) ON DELETE CASCADE,
                rank_no INTEGER NOT NULL,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                share_class TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                fund_type TEXT NOT NULL DEFAULT '',
                target_index TEXT NOT NULL DEFAULT '',
                match_kind TEXT NOT NULL DEFAULT '',
                buyable_status TEXT NOT NULL DEFAULT '',
                min_purchase REAL,
                nav REAL,
                nav_date TEXT NOT NULL DEFAULT '',
                inception_date TEXT NOT NULL DEFAULT '',
                asset_size_billion REAL,
                management_fee_pct REAL,
                custody_fee_pct REAL,
                sales_fee_pct REAL,
                purchase_fee_pct REAL,
                annual_cost_pct REAL,
                tracking_error_pct REAL,
                tracking_days INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                fee_url TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                UNIQUE(run_id, fund_code)
            );
            """
        )
        self._set_default(conn, "fund_discovery_holding_days", "30")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v12(self, conn):
        """Add holding-period costs and an explicit recommendation label."""
        self._ensure_column(conn, "fund_discovery_candidates", "redemption_fee_pct", "REAL")
        self._ensure_column(conn, "fund_discovery_candidates", "horizon_cost_pct", "REAL")
        self._ensure_column(conn, "fund_discovery_candidates", "recommendation", "TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v13(self, conn):
        """Persist intraday operation drafts separately from AI advice and confirmed trades."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT '用户手工',
                source_key TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER,
                holding_recommendation_id INTEGER,
                recurring_occurrence_id INTEGER,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                planned_amount REAL NOT NULL DEFAULT 0,
                planned_units REAL NOT NULL DEFAULT 0,
                planned_pct REAL NOT NULL DEFAULT 0,
                rationale TEXT NOT NULL DEFAULT '',
                ai_advice TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '草稿',
                result_note TEXT NOT NULL DEFAULT '',
                trade_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                posted_at TEXT NOT NULL DEFAULT '',
                UNIQUE(trade_date, source_key)
            );
            CREATE INDEX IF NOT EXISTS idx_decision_drafts_day_status
                ON decision_drafts(trade_date, status, id);
            """
        )
        self._set_default(conn, "decision_close_hour", "15")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v14(self, conn):
        """Separate research proxies from investable identities and defer OTC-fund settlement."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS investable_indices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_id TEXT NOT NULL REFERENCES universe(index_code),
                official_index_code TEXT NOT NULL DEFAULT '',
                official_index_name TEXT NOT NULL,
                ant_index_code TEXT NOT NULL DEFAULT '',
                ant_index_name TEXT NOT NULL DEFAULT '',
                mapping_relation TEXT NOT NULL DEFAULT '可投资替代',
                mapping_confidence REAL NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT '待支付宝核对',
                official_source_url TEXT NOT NULL DEFAULT '',
                ant_source_note TEXT NOT NULL DEFAULT '',
                verified_at TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                display_order INTEGER NOT NULL DEFAULT 1,
                UNIQUE(universe_id, official_index_code, ant_index_code)
            );
            CREATE INDEX IF NOT EXISTS idx_investable_indices_universe
                ON investable_indices(universe_id, active, display_order);
            CREATE TABLE IF NOT EXISTS investable_funds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investable_index_id INTEGER NOT NULL REFERENCES investable_indices(id) ON DELETE CASCADE,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                strategy_kind TEXT NOT NULL DEFAULT '被动指数',
                share_class TEXT NOT NULL DEFAULT '',
                public_buyable_status TEXT NOT NULL DEFAULT '待核验',
                ant_buyable_status TEXT NOT NULL DEFAULT '待支付宝核对',
                quality_rank INTEGER NOT NULL DEFAULT 999,
                source_url TEXT NOT NULL DEFAULT '',
                source_note TEXT NOT NULL DEFAULT '',
                verified_at TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(investable_index_id, fund_code)
            );
            CREATE INDEX IF NOT EXISTS idx_investable_funds_index_rank
                ON investable_funds(investable_index_id, active, quality_rank);
            """
        )
        for name, declaration in [
            ("name_status", "TEXT NOT NULL DEFAULT '已知'"),
            ("profile_source_url", "TEXT NOT NULL DEFAULT ''"),
            ("profile_checked_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "funds", name, declaration)
        self._ensure_column(conn, "fund_discovery_candidates", "strategy_kind", "TEXT NOT NULL DEFAULT '被动指数'")
        for name, declaration in [
            ("pricing_date", "TEXT NOT NULL DEFAULT ''"),
            ("nav_date", "TEXT NOT NULL DEFAULT ''"),
            ("settlement_nav", "REAL NOT NULL DEFAULT 0"),
            ("settlement_fee", "REAL NOT NULL DEFAULT 0"),
            ("settlement_units", "REAL NOT NULL DEFAULT 0"),
            ("settlement_status", "TEXT NOT NULL DEFAULT '待联网补全'"),
            ("settlement_source_url", "TEXT NOT NULL DEFAULT ''"),
            ("settlement_checked_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "decision_drafts", name, declaration)
        conn.execute("UPDATE decision_drafts SET pricing_date=trade_date WHERE pricing_date='' ")

        # U154 remains a historical research ID.  Keep official index identity
        # separate from platform display identity; neither is evidence that a
        # fund is currently purchasable.
        cursor = conn.execute(
            """INSERT OR IGNORE INTO investable_indices(
                   universe_id,official_index_code,official_index_name,ant_index_code,ant_index_name,
                   mapping_relation,mapping_confidence,verification_status,official_source_url,
                   ant_source_note,verified_at,active,display_order
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "U154", "399303", "国证2000指数", "860003.CNS", "国证2000主题",
                "小盘风格的历史候选映射", 0.70, "历史候选·需重新核验",
                "https://www.cnindex.com.cn/docs/gz_399303.pdf",
                "平台展示代码不是国证官方代码，发布版不把历史人工观察当作当前可买证据。",
                "", 1, 1,
            ),
        )
        index_row = conn.execute(
            """SELECT id FROM investable_indices
               WHERE universe_id='U154' AND official_index_code='399303' AND ant_index_code='860003.CNS'"""
        ).fetchone()
        if index_row:
            funds = [
                ("270026", "广发国证2000ETF联接A", "被动ETF联接", "A", 1,
                 "https://www.gffunds.com.cn/funds/?fundcode=270026"),
                ("010432", "广发国证2000ETF联接C", "被动ETF联接", "C", 2,
                 "https://www.gffunds.com.cn/funds/?fundcode=010432"),
                ("017547", "天弘国证2000指数增强A", "指数增强", "A", 11,
                 "https://static.cninfo.com.cn/finalpage/2025-01-21/1222386376.PDF"),
                ("017548", "天弘国证2000指数增强C", "指数增强", "C", 12,
                 "https://static.cninfo.com.cn/finalpage/2023-08-31/1217731417.PDF"),
                ("019318", "汇添富国证2000指数增强A", "指数增强", "A", 13,
                 "https://www.99fund.com/announcement/zx/upload/2026/20260330/58af9cc414104d4ebc7cece2c020b4fc.pdf"),
                ("019319", "汇添富国证2000指数增强C", "指数增强", "C", 14,
                 "https://www.99fund.com/announcement/zx/upload/2026/20260330/58af9cc414104d4ebc7cece2c020b4fc.pdf"),
            ]
            conn.executemany(
                """INSERT OR IGNORE INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,ant_buyable_status,quality_rank,source_url,source_note,
                       verified_at,active
                   ) VALUES(?,?,?,?,?,'公开资料显示可申购','待用户在支付宝核对',?,?,?,'',1)""",
                [(index_row[0], *row, "已核对跟踪国证2000；排名仅区分被动/增强，不构成收益保证。") for row in funds],
            )

        # Keep this public fund mapping as a catalog candidate.  Current platform
        # availability must be checked online; the index display code is not guessed.
        conn.execute(
            """INSERT OR IGNORE INTO investable_indices(
                   universe_id,official_index_code,official_index_name,ant_index_code,ant_index_name,
                   mapping_relation,mapping_confidence,verification_status,official_source_url,
                   ant_source_note,verified_at,active,display_order
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "U025", "930653", "中证食品饮料指数", "", "",
                "食品饮料行业的可投资映射", 0.90, "官方指数已核对·平台待复核",
                "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/930653factsheet.pdf",
                "基金与指数关系来自公开资料；平台当前可买状态仍需联网复核。",
                "", 1, 1,
            ),
        )
        food_index = conn.execute(
            """SELECT id FROM investable_indices
               WHERE universe_id='U025' AND official_index_code='930653'"""
        ).fetchone()
        if food_index:
            conn.executemany(
                """INSERT OR IGNORE INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,ant_buyable_status,quality_rank,source_url,source_note,
                       verified_at,active
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                [
                    (food_index[0], "001632", "天弘中证食品饮料ETF联接C", "被动ETF联接", "C",
                     "公开资料显示可申购", "待平台复核", 1,
                     "https://www.thfund.com.cn/fundinfo/001632",
                     "跟踪930653；平台当前可买性需在执行前复核。", ""),
                    (food_index[0], "001631", "天弘中证食品饮料ETF联接A", "被动ETF联接", "A",
                     "公开资料显示可申购", "待用户在支付宝核对", 2,
                     "https://yuebao.thfund.com.cn/notice/18207",
                     "同一联接基金A类；平台当前可买性和费率仍待核对。", ""),
                ],
            )
        self._set_default(conn, "fund_name_idle_resolve", "1")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v15(self, conn):
        """Make the live radar universe a strict, dynamic Ant/Alipay investable catalog.

        The old ``universe`` table is retained only because historical scan rows refer to
        its stable research IDs.  It is no longer the source of truth for a new scan.
        ``radar_enabled`` is deliberately conservative: a row is enabled only after the
        platform index identity, quote mapping and at least one passive off-exchange fund
        have all been independently evidenced.
        """
        for name, declaration in [
            ("industry_group", "TEXT NOT NULL DEFAULT ''"),
            ("admission_status", "TEXT NOT NULL DEFAULT '候选·证据未齐'"),
            ("platform_evidence_kind", "TEXT NOT NULL DEFAULT ''"),
            ("platform_source_url", "TEXT NOT NULL DEFAULT ''"),
            ("platform_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("radar_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("exclusion_reason", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "investable_indices", name, declaration)
        for name, declaration in [
            ("passive_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("platform_evidence_url", "TEXT NOT NULL DEFAULT ''"),
            ("platform_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("radar_eligible", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            self._ensure_column(conn, "investable_funds", name, declaration)
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_investable_indices_radar
                ON investable_indices(radar_enabled, admission_status, display_order);
            CREATE INDEX IF NOT EXISTS idx_investable_funds_radar
                ON investable_funds(investable_index_id, radar_eligible, active, quality_rank);
            """
        )

        # Retain historical candidates for reference, but never treat old manual
        # observations as current platform evidence.
        conn.execute(
            """UPDATE investable_indices SET
                   industry_group=CASE WHEN industry_group='' THEN '小盘风格' ELSE industry_group END,
                   platform_evidence_kind=CASE WHEN platform_evidence_kind='' THEN '历史候选' ELSE platform_evidence_kind END,
                   exclusion_reason=CASE WHEN exclusion_reason='' THEN
                     '已确认蚂蚁指数代码，但尚无一只被动ETF联接或普通被动指数基金通过平台可买核验。'
                     ELSE exclusion_reason END
               WHERE universe_id='U154' AND ant_index_code='860003.CNS'"""
        )
        conn.execute(
            """UPDATE investable_funds SET passive_verified=CASE
                       WHEN strategy_kind IN ('被动ETF联接','被动指数') THEN 1 ELSE 0 END
               WHERE investable_index_id IN (SELECT id FROM investable_indices WHERE universe_id='U154')"""
        )

        # Public product material establishes the fund/index relation, not current
        # availability on any particular sales platform.
        conn.execute(
            """UPDATE investable_indices SET
                   industry_group=CASE WHEN industry_group='' THEN '食品饮料' ELSE industry_group END,
                   platform_evidence_kind=CASE WHEN platform_evidence_kind='' THEN '公开产品资料' ELSE platform_evidence_kind END,
                   exclusion_reason=CASE WHEN exclusion_reason='' THEN
                     '公开资料已确认基金与指数关系，但销售平台当前可买状态尚未核验。'
                     ELSE exclusion_reason END
               WHERE universe_id='U025' AND official_index_code='930653'"""
        )
        conn.execute(
            """UPDATE investable_funds SET passive_verified=1,
                   platform_evidence_url=CASE WHEN fund_code='001632' THEN 'https://www.thfund.com.cn/fundinfo/001632' ELSE platform_evidence_url END
               WHERE investable_index_id IN (SELECT id FROM investable_indices WHERE universe_id='U025')
                 AND strategy_kind IN ('被动ETF联接','被动指数')"""
        )
        self._set_default(conn, "radar_catalog_rule", "ANT_STRICT_V1")
        self._set_default(conn, "platform_index_evidence_ttl_days", "180")
        self._set_default(conn, "platform_fund_buyability_ttl_days", "7")
        conn.execute("UPDATE settings SET value='ANT-INVESTABLE-DYNAMIC-V1' WHERE key='universe_version'")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v16(self, conn):
        """Create the retired v3.1 broad-industry catalog before v17 migration.

        This migration remains only so an older database can advance safely.  The
        following v17 migration deactivates every broad parent and activates the
        current leaf-only CSI catalog.
        """
        for name, declaration in [
            ("catalog_role", "TEXT NOT NULL DEFAULT '历史记录'"),
            ("parent_official_code", "TEXT NOT NULL DEFAULT ''"),
            ("representative_reason", "TEXT NOT NULL DEFAULT ''"),
            ("quote_history_status", "TEXT NOT NULL DEFAULT ''"),
            ("csi_methodology_version", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "investable_indices", name, declaration)

        # Retire the previous live catalog without deleting evidence or fund mappings.
        conn.execute(
            """UPDATE investable_indices
                  SET active=0,radar_enabled=0,admission_status='历史记录',catalog_role='历史记录',
                      exclusion_reason=CASE WHEN exclusion_reason='' THEN
                        '已由CSI叶子行业目录替代；仅保留供历史扫描复核。'
                        ELSE exclusion_reason||'；已由CSI叶子行业目录替代。' END
                WHERE universe_id NOT LIKE 'C______'"""
        )

        for item in CSI_PARENT_REFERENCES:
            conn.execute(
                """INSERT INTO universe(index_code,index_name,category,has_buyable_fund,fund_code,status,notes)
                   VALUES(?,?,?,0,'','CSI日扫代表',?)
                   ON CONFLICT(index_code) DO UPDATE SET index_name=excluded.index_name,
                       category=excluded.category,status=excluded.status,notes=excluded.notes""",
                (
                    item.universe_id,
                    item.name,
                    item.industry,
                    "只用于新扫描；旧186项与支付宝目录继续保留为历史，不参与本轮。",
                ),
            )
            existing = conn.execute(
                "SELECT id FROM investable_indices WHERE universe_id=? ORDER BY id LIMIT 1",
                (item.universe_id,),
            ).fetchone()
            values = (
                item.code, item.name, item.display_code, item.name, item.factsheet_url,
                item.reason, item.order, item.industry, item.factsheet_url,
                item.reason, CSI_METHODOLOGY_VERSION,
            )
            if existing:
                conn.execute(
                    """UPDATE investable_indices SET
                           official_index_code=?,official_index_name=?,ant_index_code=?,ant_index_name=?,
                           mapping_relation='中证官方同一指数',mapping_confidence=1,
                           verification_status='中证官方资料已核对',official_source_url=?,
                           ant_source_note=?,verified_at='2026-08-07',active=1,display_order=?,
                           industry_group=?,admission_status='已准入雷达',
                           platform_evidence_kind='中证官方事实表',platform_source_url=?,
                           platform_checked_at='2026-08-07',radar_enabled=1,exclusion_reason='',
                           catalog_role='日扫代表',parent_official_code='',representative_reason=?,
                           quote_history_status='已核验至少20个交易日',csi_methodology_version=?
                         WHERE id=?""",
                    (*values, int(existing[0])),
                )
            else:
                conn.execute(
                    """INSERT INTO investable_indices(
                           universe_id,official_index_code,official_index_name,ant_index_code,ant_index_name,
                           mapping_relation,mapping_confidence,verification_status,official_source_url,
                           ant_source_note,verified_at,active,display_order,industry_group,admission_status,
                           platform_evidence_kind,platform_source_url,platform_checked_at,radar_enabled,
                           exclusion_reason,catalog_role,parent_official_code,representative_reason,
                           quote_history_status,csi_methodology_version)
                       VALUES(?,?,?,?,?,'中证官方同一指数',1,'中证官方资料已核对',?,?,
                              '2026-08-07',1,?,?,'已准入雷达','中证官方事实表',?,
                              '2026-08-07',1,'','日扫代表','',?,'已核验至少20个交易日',?)""",
                    (item.universe_id, *values),
                )
            conn.execute(
                """INSERT INTO proxy_mappings(
                       universe_id,provider,security_id,proxy_code,proxy_name,mapping_kind,
                       confidence,rationale,active,reviewed_at)
                   VALUES(?,'eastmoney_public',?,?,?,'精确指数',1,?,1,'2026-08-07')
                   ON CONFLICT(universe_id) DO UPDATE SET provider=excluded.provider,
                       security_id=excluded.security_id,proxy_code=excluded.proxy_code,
                       proxy_name=excluded.proxy_name,mapping_kind=excluded.mapping_kind,
                       confidence=excluded.confidence,rationale=excluded.rationale,
                       active=1,reviewed_at=excluded.reviewed_at""",
                (
                    item.universe_id, item.security_id, item.code, item.name,
                    "中证官方代码与公开行情代码一致；已验证可取得至少20个交易日历史。",
                ),
            )

        for rank, fund in enumerate(CSI_FUND_SEEDS, start=1):
            item = CSI_BY_CODE.get(fund.index_code)
            if item is None:
                # v17 retires broad parents from the live scan.  Keep their
                # historical fund rows in upgraded databases, but do not seed a
                # parent back into a fresh leaf-only radar merely for compatibility.
                continue
            index_row = conn.execute(
                "SELECT id FROM investable_indices WHERE universe_id=? AND active=1 ORDER BY id LIMIT 1",
                (item.universe_id,),
            ).fetchone()
            if not index_row:
                continue
            index_id = int(index_row[0])
            conn.execute(
                """INSERT OR IGNORE INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                       purchase_fee_pct,status,notes,updated_at)
                   VALUES(?,?,?,?,?,?,0,'公开资料已核对·下单前复核',?,?)""",
                (
                    fund.fund_code, fund.fund_name, fund.index_code, item.name,
                    fund.share_class, item.industry,
                    "精确跟踪同一CSI指数；公开渠道于2026-08-07显示可申购，实际折扣与限购以用户下单页为准。",
                    "2026-08-07",
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO fund_mappings(
                       universe_id,index_code,fund_code,fund_name,share_class,buyable_status,
                       source_url,verified_at)
                   VALUES(?,?,?,?,?,'公开渠道可申购',?,'2026-08-07')""",
                (
                    item.universe_id, fund.index_code, fund.fund_code, fund.fund_name,
                    fund.share_class, fund.source_url,
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,ant_buyable_status,quality_rank,source_url,source_note,
                       verified_at,active,passive_verified,platform_evidence_url,
                       platform_checked_at,radar_eligible)
                   VALUES(?,?,?,?,?,'公开渠道可申购','不作为CSI准入条件',?,?,?,
                          '2026-08-07',1,1,?,'2026-08-07',1)""",
                (
                    index_id, fund.fund_code, fund.fund_name, fund.strategy_kind,
                    fund.share_class, rank, fund.source_url,
                    "公开基金档案核对跟踪标的与申购状态；用户实际平台状态仍需下单前复核。",
                    fund.source_url,
                ),
            )
            conn.execute(
                """UPDATE universe SET has_buyable_fund=1,
                       fund_code=CASE WHEN fund_code='' THEN ? ELSE fund_code END,
                       status='CSI代表·已有被动基金'
                     WHERE index_code=?""",
                (fund.fund_code, item.universe_id),
            )

        settings = {
            "radar_catalog_rule": "CSI_LEVEL1_REPRESENTATIVE_V1_RETIRED",
            "universe_version": CSI_REPRESENTATIVE_VERSION,
            "csi_methodology_url": CSI_METHODOLOGY_URL,
            "csi_methodology_version": CSI_METHODOLOGY_VERSION,
            "csi_daily_scan_count": str(len(CSI_REPRESENTATIVES)),
        }
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(settings.items()),
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v17(self, conn):
        """Replace broad parents with official CSI leaf-industry targets.

        Any industry that has a monitored child is excluded from the live radar.  Its
        code remains only as a parent label.  Allocation clusters add a second guard
        for highly overlapping sibling themes such as AI/compute/data-centre indices.
        """
        self._ensure_column(conn, "investable_indices", "hierarchy_level", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "investable_indices", "official_tracked_product", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "investable_indices", "allocation_cluster", "TEXT NOT NULL DEFAULT ''")

        active_ids = [item.universe_id for item in CSI_REPRESENTATIVES]
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"""UPDATE investable_indices
                   SET active=0,radar_enabled=0,admission_status='历史记录',catalog_role='历史记录',
                       exclusion_reason=CASE WHEN exclusion_reason='' THEN
                         '已由更细的CSI叶子行业替代；仅保留作分类与历史复核。'
                         ELSE exclusion_reason END
                 WHERE catalog_role='日扫代表' AND universe_id NOT IN ({placeholders})""",
            active_ids,
        )

        for item in CSI_REPRESENTATIVES:
            conn.execute(
                """INSERT INTO universe(index_code,index_name,category,has_buyable_fund,fund_code,status,notes)
                   VALUES(?,?,?,0,'','CSI行业日扫',?)
                   ON CONFLICT(index_code) DO UPDATE SET index_name=excluded.index_name,
                       category=excluded.category,status=excluded.status,notes=excluded.notes""",
                (
                    item.universe_id,
                    item.name,
                    item.industry,
                    f"CSI细分行业{len(CSI_REPRESENTATIVES)}目标；父级{item.parent_code or '无'}；配置簇{item.allocation_cluster}。",
                ),
            )
            existing = conn.execute(
                "SELECT id FROM investable_indices WHERE universe_id=? ORDER BY id LIMIT 1",
                (item.universe_id,),
            ).fetchone()
            common = (
                item.code,
                item.name,
                item.display_code,
                item.name,
                item.factsheet_url,
                item.reason,
                item.order,
                item.industry,
                item.factsheet_url,
                item.parent_code,
                item.reason,
                CSI_METHODOLOGY_VERSION,
                item.hierarchy_level,
                1 if item.official_tracked else 0,
                item.allocation_cluster,
            )
            if existing:
                conn.execute(
                    """UPDATE investable_indices SET
                           official_index_code=?,official_index_name=?,ant_index_code=?,ant_index_name=?,
                           mapping_relation='中证官方同一指数',mapping_confidence=1,
                           verification_status='中证官方编制方案已核对',official_source_url=?,
                           ant_source_note=?,verified_at='2026-08-08',active=1,display_order=?,
                           industry_group=?,admission_status='已准入雷达',
                           platform_evidence_kind='中证官方编制方案',platform_source_url=?,
                           platform_checked_at='2026-08-08',radar_enabled=1,exclusion_reason='',
                           catalog_role='日扫代表',parent_official_code=?,representative_reason=?,
                           quote_history_status='已核验至少20个交易日',csi_methodology_version=?,
                           hierarchy_level=?,official_tracked_product=?,allocation_cluster=? WHERE id=?""",
                    (*common, int(existing[0])),
                )
            else:
                conn.execute(
                    """INSERT INTO investable_indices(
                           universe_id,official_index_code,official_index_name,ant_index_code,ant_index_name,
                           mapping_relation,mapping_confidence,verification_status,official_source_url,
                           ant_source_note,verified_at,active,display_order,industry_group,admission_status,
                           platform_evidence_kind,platform_source_url,platform_checked_at,radar_enabled,
                           exclusion_reason,catalog_role,parent_official_code,representative_reason,
                           quote_history_status,csi_methodology_version,hierarchy_level,
                           official_tracked_product,allocation_cluster)
                       VALUES(?,?,?,?,?,'中证官方同一指数',1,'中证官方编制方案已核对',?,?,
                              '2026-08-08',1,?,?,'已准入雷达','中证官方编制方案',?,
                              '2026-08-08',1,'','日扫代表',?,?,'已核验至少20个交易日',?,?,?,?)""",
                    (item.universe_id, *common),
                )
            conn.execute(
                """INSERT INTO proxy_mappings(
                       universe_id,provider,security_id,proxy_code,proxy_name,mapping_kind,
                       confidence,rationale,active,reviewed_at)
                   VALUES(?,'eastmoney_public',?,?,?,'精确指数',1,?,1,'2026-08-08')
                   ON CONFLICT(universe_id) DO UPDATE SET provider=excluded.provider,
                       security_id=excluded.security_id,proxy_code=excluded.proxy_code,
                       proxy_name=excluded.proxy_name,mapping_kind=excluded.mapping_kind,
                       confidence=excluded.confidence,rationale=excluded.rationale,
                       active=1,reviewed_at=excluded.reviewed_at""",
                (
                    item.universe_id,
                    item.security_id,
                    item.code,
                    item.name,
                    "中证官方代码与东方财富公开行情身份一致；中证.CSI展示代码（六位数字或H加五位数字）与行情证券ID分栏保存。",
                ),
            )

        settings = {
            "radar_catalog_rule": "CSI_VERIFIED_SCANNABLE_100_V5",
            "universe_version": CSI_REPRESENTATIVE_VERSION,
            "csi_methodology_url": CSI_METHODOLOGY_URL,
            "csi_methodology_version": CSI_METHODOLOGY_VERSION,
            "csi_daily_scan_count": str(len(CSI_REPRESENTATIVES)),
            "csi_leaf_target_count": str(len(CSI_REPRESENTATIVES)),
            "csi_parent_scan_count": "0",
        }
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(settings.items()),
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v18(self, conn):
        """Unify the active-capital ledger and repair confirmed purchases."""
        self._ensure_column(conn, "holdings", "counts_toward_active_capital", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "trades", "counts_toward_active_capital", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "decision_drafts", "counts_toward_active_capital", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "fee_tiers", "source_url", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "fee_tiers", "checked_at", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """UPDATE funds SET share_class='C'
                 WHERE COALESCE(share_class,'')='' AND UPPER(TRIM(fund_name)) LIKE '%C'"""
        )
        conn.execute(
            """UPDATE holdings SET counts_toward_active_capital=CASE
                   WHEN fund_code IN (SELECT fund_code FROM funds WHERE
                        UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%债券%'
                     OR UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%货币%'
                     OR UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%余额宝%'
                     OR UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%纳斯达克%'
                     OR UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%QDII%'
                     OR UPPER(fund_name||' '||index_name||' '||correlation_bucket) LIKE '%海外%') THEN 0
                   WHEN fund_code IN (SELECT fund_code FROM funds WHERE
                        fund_name||' '||index_name||' '||correlation_bucket LIKE '%黄金%'
                     OR UPPER(index_code) LIKE '%.CSI'
                     OR fund_name LIKE '%ETF联接%'
                     OR (fund_name LIKE '%指数%' AND fund_name NOT LIKE '%增强%')) THEN 1
                   ELSE CASE WHEN sleeve='主动' THEN 1 ELSE 0 END END"""
        )
        conn.execute(
            """UPDATE trades SET counts_toward_active_capital=COALESCE((
                   SELECT CASE WHEN UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%债券%'
                                    OR UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%货币%'
                                    OR UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%余额宝%'
                                    OR UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%纳斯达克%'
                                    OR UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%QDII%'
                                    OR UPPER(f.fund_name||' '||f.index_name||' '||f.correlation_bucket) LIKE '%海外%' THEN 0
                               WHEN f.fund_name||' '||f.index_name||' '||f.correlation_bucket LIKE '%黄金%'
                                    OR UPPER(f.index_code) LIKE '%.CSI' OR f.fund_name LIKE '%ETF联接%'
                                    OR (f.fund_name LIKE '%指数%' AND f.fund_name NOT LIKE '%增强%') THEN 1 ELSE 0 END
                     FROM funds f WHERE f.fund_code=trades.fund_code),0)"""
        )
        conn.execute(
            """UPDATE decision_drafts SET counts_toward_active_capital=COALESCE((
                   SELECT counts_toward_active_capital FROM trades WHERE trades.id=decision_drafts.trade_id),
                   CASE WHEN source_kind='AI建议' THEN 1 ELSE counts_toward_active_capital END)"""
        )
        conn.execute(
            """UPDATE investable_funds SET active=0,radar_eligible=0
                 WHERE UPPER(COALESCE(share_class,''))<>'C'"""
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_lots_trade_id ON cost_lots(trade_id) WHERE trade_id IS NOT NULL")

        # Alpha 18 knew about confirmed trades but did not post them to holdings.
        # Repair only recent, complete buys for funds that have no live holding, so
        # historical aggregate positions cannot be duplicated by the migration.
        missing = conn.execute(
            """SELECT t.*,f.fund_name FROM trades t JOIN funds f USING(fund_code)
                WHERE t.status='已确认' AND t.action IN ('买入','定投','转换转入')
                  AND t.amount>0 AND t.units>0 AND t.nav>0 AND t.created_at>='2026-08-07'
                  AND NOT EXISTS(SELECT 1 FROM cost_lots l WHERE l.trade_id=t.id)
                  AND NOT EXISTS(SELECT 1 FROM holdings h WHERE h.fund_code=t.fund_code AND h.active=1)
                ORDER BY t.id"""
        ).fetchall()
        now = local_now().isoformat(timespec="seconds")
        for row in missing:
            counts = int(row["counts_toward_active_capital"] or 0)
            sleeve = "主动" if counts else ("定投" if row["action"] == "定投" else "其他")
            conn.execute(
                """INSERT INTO holdings(
                       fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,active,
                       counts_toward_active_capital) VALUES(?,?,?,?,?,?,?,?,1,?)""",
                (row["fund_code"], row["confirm_date"] or row["trade_date"], row["units"], row["amount"],
                 row["nav"], row["nav_date"] or row["trade_date"], sleeve,
                 f"由历史已确认交易#{row['id']}自动修复入账", counts),
            )
            conn.execute(
                """INSERT INTO cost_lots(
                       fund_code,trade_id,occurrence_id,confirmed_date,units,remaining_units,gross_amount,
                       fee,net_invested,nav,source,created_at) VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?)""",
                (row["fund_code"], row["id"], row["confirm_date"] or row["trade_date"], row["units"],
                 row["units"], row["amount"], row["fee"], max(0.0, row["amount"]-row["fee"]), row["nav"],
                 "v18已确认交易修复", now),
            )
        self._set_default(conn, "active_capital_logic_version", "2")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v19(self, conn):
        """Keep official CSI identities and exclude unscannable research leaves.

        v17 stored every CSI code as an Eastmoney ``2.xxxxxx`` id and marked
        research-only leaves as if 20-day history had been verified.  On the
        user's Windows network this sent 89 items into invalid fallback routes.
        The official ``CSI.code`` identity now selects the CSI provider directly;
        leaves without an official tracked product remain in the directory but
        are not admitted to the paid daily scan.
        """
        conn.execute(
            """UPDATE proxy_mappings
                  SET security_id='CSI.' || COALESCE((
                        SELECT i.official_index_code FROM investable_indices i
                         WHERE i.universe_id=proxy_mappings.universe_id
                           AND i.active=1 ORDER BY i.id LIMIT 1
                      ), proxy_code),
                      provider='eastmoney_public',mapping_kind='精确指数',confidence=1,
                      rationale='保留中证官方CSI身份；由中证官网精确日线提供行情，禁止把H代码或93系列猜成交易所代码。',
                      reviewed_at='2026-08-08',active=1
                WHERE universe_id IN (
                      SELECT universe_id FROM investable_indices
                       WHERE active=1 AND catalog_role='日扫代表'
                )"""
        )
        conn.execute(
            """UPDATE investable_indices
                  SET quote_history_status=CASE WHEN official_tracked_product=1
                          THEN '已核验至少20个交易日' ELSE '官方日线不足20日' END,
                      radar_enabled=CASE WHEN official_tracked_product=1 THEN 1 ELSE 0 END,
                      admission_status=CASE WHEN official_tracked_product=1
                          THEN '已准入雷达' ELSE '候选·行情历史不足20日；中证未标记已有跟踪产品' END,
                      exclusion_reason=CASE WHEN official_tracked_product=1 THEN ''
                          ELSE '保留在100项目录供研究；未通过20日行情与跟踪产品硬门槛，不进入每日扫描。' END
                WHERE active=1 AND catalog_role='日扫代表'"""
        )
        settings = {
            "csi_daily_scan_count": str(len(CSI_DAILY_TARGETS)),
            "csi_leaf_target_count": str(len(CSI_REPRESENTATIVES)),
            "csi_quote_identity_version": "CSI_OFFICIAL_V1",
        }
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(settings.items()),
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v20(self, conn):
        """Admit only the fully verified 100-index CSI catalog.

        V5 removes every research-only placeholder from the active catalog.  Each
        current row has an official tracking product and more than 20 official
        daily bars.  Retired identities stay in the database as inactive history
        so old scan results remain readable; no user ledger, holding, fund, draft,
        API setting, or cached historical result is deleted.
        """
        if len(CSI_DAILY_TARGETS) != len(CSI_REPRESENTATIVES):
            raise RuntimeError("V5正式目录仍包含不可扫描项目，拒绝迁移。")

        active_ids = [item.universe_id for item in CSI_REPRESENTATIVES]
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"""UPDATE investable_indices
                    SET active=0,radar_enabled=0,admission_status='历史记录',catalog_role='历史记录',
                        exclusion_reason=CASE WHEN exclusion_reason='' THEN
                          'V5目录已用具备跟踪产品及充足官方日线的指数替换；旧记录仅供历史复核。'
                          ELSE exclusion_reason END
                  WHERE catalog_role='日扫代表' AND universe_id NOT IN ({placeholders})""",
            active_ids,
        )
        conn.execute(
            f"""UPDATE investable_indices
                    SET active=1,radar_enabled=1,official_tracked_product=1,
                        admission_status='已准入雷达',exclusion_reason='',catalog_role='日扫代表',
                        quote_history_status='已核验至少20个交易日',
                        verification_status='中证官网跟踪产品与官方日线均已核对',
                        platform_checked_at='2026-08-09',verified_at='2026-08-09'
                  WHERE universe_id IN ({placeholders})""",
            active_ids,
        )
        conn.execute(
            f"""UPDATE proxy_mappings
                    SET security_id='CSI.' || proxy_code,provider='eastmoney_public',
                        mapping_kind='精确指数',confidence=1,active=1,reviewed_at='2026-08-09',
                        rationale='中证官网同一指数身份；V5已核验跟踪产品及充足官方日线。'
                  WHERE universe_id IN ({placeholders})""",
            active_ids,
        )
        settings = {
            "radar_catalog_rule": "CSI_VERIFIED_SCANNABLE_100_V5",
            "universe_version": CSI_REPRESENTATIVE_VERSION,
            "csi_daily_scan_count": str(len(CSI_DAILY_TARGETS)),
            "csi_leaf_target_count": str(len(CSI_REPRESENTATIVES)),
            "csi_research_only_count": "0",
            "csi_catalog_validation_date": "2026-08-09",
            "csi_catalog_product_gate": "CSI_OFFICIAL_TRACKING_PRODUCT_AND_20D_HISTORY",
        }
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(settings.items()),
        )
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v21(self, conn):
        """Keep every official CSI association without making every product executable.

        ``investable_funds`` is the short-holding execution layer.  The CSI website
        also lists exchange ETFs, A/E/I shares and enhanced products; putting those
        rows in the execution table made "recorded" and "buyable" impossible to
        distinguish.  The official product directory therefore has its own table.
        A successful per-index refresh replaces only that index's active snapshot,
        so a network failure can never erase an earlier good result.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS csi_index_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investable_index_id INTEGER NOT NULL
                    REFERENCES investable_indices(id) ON DELETE CASCADE,
                index_code TEXT NOT NULL,
                product_code TEXT NOT NULL,
                fund_name TEXT NOT NULL DEFAULT '',
                share_class TEXT NOT NULL DEFAULT '',
                strategy_kind TEXT NOT NULL DEFAULT '待分类',
                asset_class TEXT NOT NULL DEFAULT '',
                fund_type TEXT NOT NULL DEFAULT '',
                coverage TEXT NOT NULL DEFAULT '',
                fund_manager TEXT NOT NULL DEFAULT '',
                inception_date TEXT NOT NULL DEFAULT '',
                exchange_name TEXT NOT NULL DEFAULT '',
                aum TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(investable_index_id, product_code)
            );
            CREATE INDEX IF NOT EXISTS idx_csi_index_products_index_active
                ON csi_index_products(investable_index_id, active, product_code);
            CREATE TABLE IF NOT EXISTS csi_product_sync_status (
                investable_index_id INTEGER PRIMARY KEY
                    REFERENCES investable_indices(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT '待同步',
                product_count INTEGER NOT NULL DEFAULT 0,
                attempted_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS csi_fund_sync_status (
                investable_index_id INTEGER PRIMARY KEY
                    REFERENCES investable_indices(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT '待同步',
                exact_c_count INTEGER NOT NULL DEFAULT 0,
                attempted_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._ensure_column(
            conn, "investable_funds", "relation_kind",
            "TEXT NOT NULL DEFAULT '同一指数'",
        )
        # Old manual-save code could accidentally put a similar-index candidate in
        # the current index's relation list.  New writes carry relation_kind and the
        # execution gate below rejects anything except an exact relation.
        conn.execute(
            """UPDATE investable_funds SET relation_kind='同一指数'
                 WHERE COALESCE(relation_kind,'')=''"""
        )
        # Reuse only exact, passive C-share evidence when an old catalog row and
        # the current CSI row have the same official index code.  Broad parents,
        # A/E shares, enhanced funds and similar-index candidates are not copied.
        conn.execute(
            """INSERT OR IGNORE INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,ant_buyable_status,quality_rank,source_url,
                   source_note,verified_at,active,passive_verified,platform_evidence_url,
                   platform_checked_at,radar_eligible,relation_kind)
               SELECT current_i.id,f.fund_code,f.fund_name,f.strategy_kind,'C',
                      CASE WHEN f.public_buyable_status IN
                           ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')
                           THEN '公开渠道可申购' ELSE f.public_buyable_status END,
                      '不作为CSI准入条件',f.quality_rank,f.source_url,
                      '由相同中证正式指数代码的历史目录安全迁移；只迁移场外被动C类。',
                      f.verified_at,1,1,f.platform_evidence_url,f.platform_checked_at,0,'同一指数'
                 FROM investable_indices current_i
                 JOIN investable_indices old_i
                   ON old_i.official_index_code=current_i.official_index_code
                  AND old_i.id<>current_i.id
                 JOIN investable_funds f ON f.investable_index_id=old_i.id
                WHERE current_i.active=1 AND current_i.catalog_role='日扫代表'
                  AND UPPER(COALESCE(f.share_class,''))='C'
                  AND f.strategy_kind IN ('被动ETF联接','被动指数')
                  AND COALESCE(f.passive_verified,0)=1"""
        )
        # A completed single-index comparison is also valid evidence when it is
        # exact, passive, C class and publicly buyable.  This recovers already-paid
        # or already-downloaded work instead of asking the user to repeat it.
        conn.execute(
            """INSERT OR IGNORE INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,ant_buyable_status,quality_rank,source_url,
                   source_note,verified_at,active,passive_verified,platform_evidence_url,
                   platform_checked_at,radar_eligible,relation_kind)
               SELECT i.id,c.fund_code,c.fund_name,c.strategy_kind,'C','公开渠道可申购',
                      '不作为CSI准入条件',MIN(c.rank_no),c.source_url,
                      '复用基金优选中已经完成的精确同指数C类公开资料核验。',
                      MAX(c.fetched_at),1,1,c.source_url,MAX(c.fetched_at),0,'同一指数'
                 FROM investable_indices i
                 JOIN fund_discovery_runs r ON r.universe_id=i.universe_id
                 JOIN fund_discovery_candidates c ON c.run_id=r.id
                WHERE i.active=1 AND i.catalog_role='日扫代表'
                  AND c.match_kind='同一指数' AND UPPER(c.share_class)='C'
                  AND c.strategy_kind IN ('被动ETF联接','被动指数')
                  AND c.buyable_status='公开渠道可申购'
                GROUP BY i.id,c.fund_code,c.fund_name,c.strategy_kind,c.source_url"""
        )
        # Keep the compatibility mapping table in step with the strict execution
        # layer.  The current catalog's first three exact C shares were already in
        # ``investable_funds`` before v21, but the directory/fund-selection pages
        # still read ``fund_mappings`` and therefore displayed them as missing.
        conn.execute(
            """INSERT OR IGNORE INTO fund_mappings(
                   universe_id,index_code,fund_code,fund_name,share_class,
                   buyable_status,source_url,verified_at)
               SELECT i.universe_id,i.official_index_code,f.fund_code,f.fund_name,'C',
                      '公开可申购',f.source_url,
                      COALESCE(NULLIF(f.verified_at,''),NULLIF(f.platform_checked_at,''),'')
                 FROM investable_indices i
                 JOIN investable_funds f ON f.investable_index_id=i.id
                WHERE i.active=1 AND i.catalog_role='日扫代表' AND f.active=1
                  AND f.relation_kind='同一指数'
                  AND UPPER(COALESCE(f.share_class,''))='C'
                  AND f.strategy_kind IN ('被动ETF联接','被动指数')
                  AND COALESCE(f.passive_verified,0)=1
                  AND f.public_buyable_status IN
                      ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')"""
        )
        conn.execute(
            """UPDATE investable_funds
                  SET radar_eligible=CASE WHEN relation_kind='同一指数'
                       AND UPPER(COALESCE(share_class,''))='C'
                       AND strategy_kind IN ('被动ETF联接','被动指数')
                       AND passive_verified=1
                       AND public_buyable_status IN ('公开渠道可申购','已核对可申购','用户已实际买入')
                       AND investable_index_id IN (SELECT id FROM investable_indices
                             WHERE active=1 AND radar_enabled=1 AND admission_status='已准入雷达')
                       THEN 1 ELSE 0 END"""
        )
        self._set_default(conn, "csi_product_sync_last_at", "")
        self._set_default(conn, "csi_product_sync_last_error", "")
        self._set_default(conn, "csi_product_sync_source", "中证官网相关产品")
        conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _migrate_v22(self, conn):
        """Freeze research inputs without rewriting any user-owned capital data."""
        for name, declaration in [
            ("universe_version", "TEXT NOT NULL DEFAULT ''"),
            ("research_date", "TEXT NOT NULL DEFAULT ''"),
            ("universe_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("universe_hash", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "analysis_jobs", name, declaration)
        for name, declaration in [
            ("evidence_date", "TEXT NOT NULL DEFAULT ''"),
            ("universe_version", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "sector_evidence", name, declaration)
        for name, declaration in [
            ("base_score", "REAL NOT NULL DEFAULT 0"),
            ("score_components_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            self._ensure_column(conn, "sector_metrics", name, declaration)
        # Alpha 24 briefly raised this default.  Correct that historical default
        # once, while leaving every other user-selected value untouched.
        conn.execute(
            "UPDATE settings SET value='12000' "
            "WHERE key='api_pro_max_tokens' AND value IN ('64000','80000')"
        )

    def _migrate_v23(self, conn):
        """Install the immutable portfolio ledger and separate pricing dates."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_postings (
                trade_id INTEGER PRIMARY KEY REFERENCES trades(id),
                posting_kind TEXT NOT NULL,
                critical_snapshot_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                posted_at TEXT NOT NULL,
                reversed_at TEXT NOT NULL DEFAULT '',
                reversal_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS lot_consumptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exit_trade_id INTEGER NOT NULL REFERENCES trades(id),
                sequence_no INTEGER NOT NULL,
                lot_id INTEGER REFERENCES cost_lots(id),
                fund_code TEXT NOT NULL,
                confirmed_date TEXT NOT NULL DEFAULT '',
                units REAL NOT NULL,
                cost_basis REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(exit_trade_id, sequence_no)
            );
            CREATE TABLE IF NOT EXISTS active_cash_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                event_kind TEXT NOT NULL,
                trade_id INTEGER REFERENCES trades(id),
                amount REAL NOT NULL,
                balance_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS portfolio_equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                holdings_value REAL NOT NULL,
                active_cash REAL NOT NULL,
                total_equity REAL NOT NULL,
                external_flow_total REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._ensure_column(conn, "cost_lots", "pricing_date", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "trades", "result_pnl_status", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE cost_lots SET pricing_date=confirmed_date WHERE COALESCE(pricing_date,'')=''"
        )

        now = local_now().isoformat(timespec="seconds")
        # Backfill immutable fingerprints only for trades whose portfolio posting is
        # already evidenced by a cost lot, or for a reconciled exit.  Incomplete
        # legacy rows remain editable/pending instead of being falsely certified.
        posted_rows = conn.execute(
            """SELECT DISTINCT t.* FROM trades t
                 LEFT JOIN cost_lots l ON l.trade_id=t.id
                WHERE COALESCE(t.status,'已确认')='已确认'
                  AND (l.id IS NOT NULL OR
                       (t.action IN ('卖出','清仓','转换','转换转出') AND t.result_pnl IS NOT NULL))
                ORDER BY t.id"""
        ).fetchall()
        for row in posted_rows:
            item = dict(row)
            kind = "买入" if str(item.get("action") or "") in {"买入", "定投", "转换转入"} else "卖出"
            critical = {
                key: item.get(key) for key in (
                    "trade_date", "confirm_date", "nav_date", "fund_code", "action",
                    "amount", "units", "nav", "fee", "counts_toward_active_capital",
                )
            }
            conn.execute(
                """INSERT OR IGNORE INTO ledger_postings(
                       trade_id,posting_kind,critical_snapshot_json,metadata_json,posted_at)
                   VALUES(?,?,?,?,?)""",
                (int(item["id"]), kind, json.dumps(critical, ensure_ascii=False, sort_keys=True),
                 json.dumps({"migration": "v23", "evidence": "legacy_cost_lot_or_reconciled_exit"}, ensure_ascii=False), now),
            )

        conn.execute(
            """UPDATE trades SET result_pnl_status=CASE
                   WHEN action NOT IN ('卖出','清仓','转换','转换转出') THEN '不适用'
                   WHEN result_pnl IS NULL THEN '待核算'
                   WHEN ABS(result_pnl)>0.0000001 THEN '已核算'
                   WHEN id IN (SELECT trade_id FROM ledger_postings WHERE posting_kind='卖出') THEN '已核算'
                   ELSE '待核算' END
                 WHERE COALESCE(result_pnl_status,'')=''"""
        )

        initialized = conn.execute(
            "SELECT value FROM settings WHERE key='active_cash_initialized_at'"
        ).fetchone()
        if not initialized or not str(initialized[0] or "").strip():
            limit_row = conn.execute(
                "SELECT value FROM settings WHERE key='active_capital_limit'"
            ).fetchone()
            limit_value = float(limit_row[0] if limit_row else 8000)
            committed = float(conn.execute(
                """SELECT COALESCE(SUM(cost),0) FROM holdings
                     WHERE active=1 AND COALESCE(counts_toward_active_capital,0)=1"""
            ).fetchone()[0] or 0)
            balance = round(limit_value - committed, 6)
            conn.execute(
                """INSERT OR IGNORE INTO active_cash_ledger(
                       event_key,event_kind,amount,balance_after,created_at,notes)
                   VALUES(?,?,?,?,?,?)""",
                ("OPENING_BALANCE:v23", "OPENING_BALANCE", balance, balance, now,
                 f"v23按当前主动持仓成本初始化：额度{limit_value:.2f}-已投入{committed:.2f}"),
            )
            for key, value in (
                ("active_cash_balance", str(balance)),
                ("active_cash_initialized_at", now),
                ("active_cash_limit_baseline", str(limit_value)),
            ):
                conn.execute(
                    """INSERT INTO settings(key,value) VALUES(?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (key, value),
                )

        self._set_default(conn, "fund_execution_ttl_hours", "24")
        self._set_default(conn, "trading_calendar_closed_dates_json", "[]")
        self._set_default(conn, "trading_calendar_open_dates_json", "[]")
        self._set_default(conn, "trading_calendar_source_url", "")
        self._set_default(conn, "trading_calendar_updated_at", "")

    def _migrate_v24(self, conn):
        """Archive conclusively uninvestable radar rows without deleting history.

        This is a one-time policy migration.  It deliberately does *not* archive a
        row merely because fund evidence is old or a network refresh failed.  Only
        a completed field audit recorded as ``已确认无C`` may remove a row from the
        current catalog, and even then the database identity stays intact for old
        research foreign keys.
        """
        structurally_strict = """
            EXISTS(
                SELECT 1 FROM investable_funds f
                 WHERE f.investable_index_id=investable_indices.id
                   AND f.active=1 AND f.relation_kind='同一指数'
                   AND UPPER(COALESCE(f.share_class,''))='C'
                   AND f.strategy_kind IN ('被动ETF联接','被动指数')
                   AND COALESCE(f.passive_verified,0)=1
                   AND (
                       f.public_buyable_status IN (
                           '公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购'
                       )
                       OR f.public_buyable_status GLOB '限购*[1-9]*元'
                   )
            )
        """
        algorithm_row = conn.execute(
            "SELECT value FROM settings WHERE key='csi_fund_sync_algorithm_version'"
        ).fetchone()
        algorithm_version = str(algorithm_row[0] if algorithm_row else "")
        # v3 compared abbreviated CSI labels with the long F10 target name by
        # string equality.  That produced false ``已确认无C`` conclusions (for
        # example 科创芯片设计), so startup must never archive those rows.  Only a
        # database already audited by the official-family v4 algorithm may retire
        # rows during migration; otherwise the new sync service rechecks them first.
        retired = []
        if algorithm_version == "exact-passive-c-v4-official-family":
            retired = conn.execute(
                f"""SELECT id,universe_id FROM investable_indices
                      WHERE active=1 AND catalog_role='日扫代表'
                        AND EXISTS(
                            SELECT 1 FROM csi_fund_sync_status s
                             WHERE s.investable_index_id=investable_indices.id
                               AND s.state='已确认无C'
                        )
                        AND NOT ({structurally_strict})"""
            ).fetchall()
        if retired:
            ids = [int(row["id"]) for row in retired]
            universe_ids = [str(row["universe_id"]) for row in retired]
            id_marks = ",".join("?" for _ in ids)
            uid_marks = ",".join("?" for _ in universe_ids)
            conn.execute(
                f"""UPDATE investable_indices
                        SET active=0,radar_enabled=0,admission_status='历史记录',
                            catalog_role='历史记录',
                            exclusion_reason='已完整核验但没有严格同指数、被动、C类且公开可申购的场外基金；不再跟踪或扫描。'
                      WHERE id IN ({id_marks})""",
                ids,
            )
            conn.execute(
                f"UPDATE proxy_mappings SET active=0 WHERE universe_id IN ({uid_marks})",
                universe_ids,
            )
            conn.execute(
                f"""UPDATE universe SET has_buyable_fund=0,fund_code='',status='历史记录·无严格C'
                      WHERE index_code IN ({uid_marks})""",
                universe_ids,
            )

        # Recompute scan admission from durable evidence, never from the volatile
        # 24-hour order-execution TTL.  User-owned mappings and fund fields are not
        # rewritten by this migration.
        conn.execute(
            f"""UPDATE investable_indices
                    SET radar_enabled=CASE WHEN active=1
                         AND catalog_role='日扫代表' AND ({structurally_strict}) THEN 1 ELSE 0 END,
                        admission_status=CASE WHEN active=0 THEN '历史记录'
                         WHEN catalog_role='日扫代表' AND ({structurally_strict}) THEN '已准入雷达'
                         ELSE '候选·没有严格C类可投资基金' END,
                        exclusion_reason=CASE WHEN active=0 THEN exclusion_reason
                         WHEN catalog_role='日扫代表' AND ({structurally_strict}) THEN ''
                         ELSE '没有严格同指数、被动、C类且公开可申购的场外基金；不进入扫描。' END
                  WHERE catalog_role IN ('日扫代表','历史记录')"""
        )
        scan_count = int(conn.execute(
            """SELECT COUNT(*) FROM investable_indices
                 WHERE active=1 AND catalog_role='日扫代表' AND radar_enabled=1
                   AND admission_status='已准入雷达'"""
        ).fetchone()[0] or 0)
        settings = {
            "radar_catalog_rule": "CSI_STRICT_EXACT_PASSIVE_C_V2",
            "universe_version": "CSI-STRICT-C-EXECUTABLE-V2",
            "csi_daily_scan_count": str(scan_count),
            "csi_catalog_product_gate": "EXACT_INDEX_PASSIVE_C_PUBLIC_BUYABLE_AND_20D_HISTORY",
        }
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(settings.items()),
        )

    def _migrate_v25(self, conn):
        """Version every field-fund conclusion and invalidate old research hashes.

        A global algorithm flag is insufficient for an interrupted audit: some rows
        may have been checked by the new parser while others still carry an older
        conclusion.  The per-row version makes continuation exact and prevents an
        HTTP-200 captcha/empty profile produced by an older parser from being trusted.
        """
        self._ensure_column(
            conn,
            "csi_fund_sync_status",
            "algorithm_version",
            "TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            conn,
            "investable_funds",
            "verification_algorithm",
            "TEXT NOT NULL DEFAULT ''",
        )
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("radar_catalog_rule", "CSI_STRICT_EXACT_PASSIVE_C_V3"),
                ("universe_version", "CSI-STRICT-C-EXECUTABLE-V3"),
            ],
        )

    def _migrate_v26(self, conn):
        """Add indexes used by interactive pages and resumable research.

        These indexes contain no derived user data and do not rewrite any
        settings, holdings or transactions.  They keep UI refreshes bounded as
        research history and market evidence grow.
        """
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_status_date
                ON analysis_jobs(status,mode,research_date,id);
            CREATE INDEX IF NOT EXISTS idx_radar_runs_imported
                ON radar_runs(imported_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidates_task_rank
                ON candidates(task_id,rank_no,id);
            CREATE INDEX IF NOT EXISTS idx_holding_recommendations_task
                ON holding_recommendations(task_id,sleeve,fund_code);
            CREATE INDEX IF NOT EXISTS idx_analysis_events_task
                ON analysis_events(task_id,id);
            CREATE INDEX IF NOT EXISTS idx_sector_metrics_task
                ON sector_metrics(task_id,universe_id);
            CREATE INDEX IF NOT EXISTS idx_sector_evidence_task
                ON sector_evidence(task_id,universe_id);
            CREATE INDEX IF NOT EXISTS idx_model_calls_task
                ON model_calls(task_id,id);
            CREATE INDEX IF NOT EXISTS idx_market_fetch_attempts_task
                ON market_fetch_attempts(task_id,id);
            CREATE INDEX IF NOT EXISTS idx_fund_mappings_code
                ON fund_mappings(fund_code,verified_at,id);
            CREATE INDEX IF NOT EXISTS idx_investable_funds_code
                ON investable_funds(fund_code,active,radar_eligible,quality_rank);
            CREATE INDEX IF NOT EXISTS idx_holdings_active_capital
                ON holdings(active,counts_toward_active_capital,fund_code);
            CREATE INDEX IF NOT EXISTS idx_trades_status_action
                ON trades(status,action,trade_date,id);
            CREATE INDEX IF NOT EXISTS idx_cost_lots_open
                ON cost_lots(fund_code,remaining_units,confirmed_date,id);
            CREATE INDEX IF NOT EXISTS idx_decision_drafts_status_action
                ON decision_drafts(status,action,trade_date,id);
            """
        )

    def _migrate_v27(self, conn):
        """Separate research tilt, timing, execution and four score dimensions."""
        for table in ("analysis_jobs", "radar_runs"):
            self._ensure_column(
                conn, table, "ranking_policy_version", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                conn, table, "ranking_policy_json", "TEXT NOT NULL DEFAULT '{}'"
            )
        for name, declaration in [
            ("volatility_20d", "REAL"),
            ("consecutive_up_days", "INTEGER"),
            ("breadth_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("breadth_pct", "REAL"),
            ("expected_data_cutoff", "TEXT NOT NULL DEFAULT ''"),
            ("freshness_state", "TEXT NOT NULL DEFAULT 'unknown'"),
        ]:
            self._ensure_column(conn, "sector_metrics", name, declaration)
        for name, declaration in [
            ("research_tilt", "TEXT NOT NULL DEFAULT '观察'"),
            ("timing_status", "TEXT NOT NULL DEFAULT '等待回调'"),
            ("execution_status", "TEXT NOT NULL DEFAULT '不可执行'"),
            ("opportunity_score", "REAL NOT NULL DEFAULT 0"),
            ("entry_score", "REAL NOT NULL DEFAULT 0"),
            ("crowding_risk_score", "REAL NOT NULL DEFAULT 0"),
            ("data_confidence", "REAL NOT NULL DEFAULT 0"),
            ("pool_route", "TEXT NOT NULL DEFAULT ''"),
            ("suppression_reason", "TEXT NOT NULL DEFAULT ''"),
            ("target_allocation_pct", "REAL NOT NULL DEFAULT 0"),
            ("execution_reason", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "candidates", name, declaration)
        for name, declaration in [
            ("local_rank", "INTEGER NOT NULL DEFAULT 0"),
            ("pool_route", "TEXT NOT NULL DEFAULT ''"),
            ("research_tilt", "TEXT NOT NULL DEFAULT '观察'"),
            ("timing_status", "TEXT NOT NULL DEFAULT '等待回调'"),
            ("execution_status", "TEXT NOT NULL DEFAULT '研究完成'"),
            ("opportunity_score", "REAL NOT NULL DEFAULT 0"),
            ("trend_score", "REAL NOT NULL DEFAULT 0"),
            ("entry_score", "REAL NOT NULL DEFAULT 0"),
            ("crowding_risk_score", "REAL NOT NULL DEFAULT 0"),
            ("data_confidence", "REAL NOT NULL DEFAULT 0"),
            ("recommendation_score", "REAL NOT NULL DEFAULT 0"),
            ("score_components_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("local_reason", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "radar_audit", name, declaration)
        for key, value in {
            "ranking_trend_weight": "0.25",
            "ranking_entry_weight": "0.25",
            "ranking_catalyst_weight": "0.20",
            "ranking_relative_strength_weight": "0.15",
            "ranking_volume_breadth_weight": "0.15",
            "ranking_crowding_penalty": "0.45",
            "ranking_data_confidence_min": "70",
            "ranking_candidate_min_score": "45",
            "ranking_pool_min": "20",
            "ranking_pool_max": "30",
            "ranking_pool_cluster_max": "4",
            "ranking_display_cluster_max": "2",
            "ranking_funded_cluster_max": "1",
            "ranking_policy_version": "four-dimension-v2",
        }.items():
            self._set_default(conn, key, value)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_radar_audit_task_rank "
            "ON radar_audit(task_id,local_rank,universe_id)"
        )

    def _migrate_v28(self, conn):
        """Invalidate same-day results produced by the first four-dimension formula.

        The version is a software-owned algorithm identifier, not a user tuning
        parameter.  Only the known old/empty identifiers are advanced; every
        user-editable weight, threshold and capital rule remains untouched.
        """
        conn.execute(
            """UPDATE settings SET value='four-dimension-v2'
                 WHERE key='ranking_policy_version'
                   AND (TRIM(value)='' OR value='four-dimension-v1')"""
        )
        self._set_default(conn, "ranking_policy_version", "four-dimension-v2")

    def _migrate_v29(self, conn):
        """Persist the two Flash judgements used by the four-dimension formula.

        Earlier builds stored only the model's narrative catalyst/crowding text,
        so production scoring could not reproduce the structured values exercised
        by tests.  Nullable columns preserve every historical row; only new V3
        research is allowed to drive current decisions.
        """
        self._ensure_column(conn, "sector_evidence", "crowding_level", "REAL")
        self._ensure_column(conn, "sector_evidence", "catalyst_strength", "REAL")
        conn.execute(
            """UPDATE settings SET value='four-dimension-v3'
                 WHERE key='ranking_policy_version'
                   AND (TRIM(value)='' OR value IN ('four-dimension-v1','four-dimension-v2'))"""
        )
        self._set_default(conn, "ranking_policy_version", "four-dimension-v3")

    def _migrate_v30(self, conn):
        """Seal legacy facts and make posting/recurring order deterministic.

        A confirmed row from an older build must never be treated as a fresh
        instruction merely because that build did not yet write
        ``ledger_postings``.  Such rows are preserved as immutable historical
        facts without touching holdings or cash a second time.  New recurring
        confirmations use a unique occurrence link and the unified ledger.
        """
        self._ensure_column(conn, "ledger_postings", "posting_seq", "INTEGER")
        conn.execute("DROP INDEX IF EXISTS ux_ledger_postings_sequence")
        ordered = conn.execute(
            "SELECT trade_id FROM ledger_postings ORDER BY posted_at,trade_id"
        ).fetchall()
        for sequence, row in enumerate(ordered, 1):
            conn.execute(
                "UPDATE ledger_postings SET posting_seq=? WHERE trade_id=?",
                (sequence, int(row[0])),
            )

        sequence = len(ordered)
        now = local_now().isoformat(timespec="seconds")
        legacy_rows = conn.execute(
            """SELECT t.* FROM trades t
                 WHERE COALESCE(t.status,'')='已确认'
                   AND t.action IN ('买入','定投','转换转入','卖出','清仓','转换','转换转出')
                   AND NOT EXISTS(
                       SELECT 1 FROM ledger_postings p WHERE p.trade_id=t.id
                   )
                 ORDER BY t.id"""
        ).fetchall()
        critical_fields = (
            "trade_date", "fund_code", "action", "amount", "units", "nav", "fee",
            "confirm_date", "nav_date", "status", "counts_toward_active_capital",
        )
        for row in legacy_rows:
            item = dict(row)
            sequence += 1
            has_lot = bool(conn.execute(
                "SELECT 1 FROM cost_lots WHERE trade_id=?", (int(item["id"]),)
            ).fetchone())
            has_holding = bool(conn.execute(
                "SELECT 1 FROM holdings WHERE fund_code=?", (str(item["fund_code"]),)
            ).fetchone())
            kind = "买入" if str(item.get("action") or "") in {"买入", "定投", "转换转入"} else "卖出"
            critical = {key: item.get(key) for key in critical_fields}
            metadata = {
                "migration": "v30",
                "legacy_sealed": True,
                "reversible": False,
                "evidence": {
                    "cost_lot_present": has_lot,
                    "same_fund_holding_present": has_holding,
                    "realized_pnl_present": item.get("result_pnl") is not None,
                },
                "note": "旧版已确认记录只读封存；未重新增加持仓、成本或现金。",
            }
            conn.execute(
                """INSERT INTO ledger_postings(
                       trade_id,posting_kind,critical_snapshot_json,metadata_json,
                       posted_at,reversed_at,reversal_reason,posting_seq
                   ) VALUES(?,?,?,?,?,'','',?)""",
                (
                    int(item["id"]), kind,
                    json.dumps(critical, ensure_ascii=False, sort_keys=True),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(item.get("created_at") or now), sequence,
                ),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_postings_sequence "
            "ON ledger_postings(posting_seq)"
        )

        # Old builds did not enforce one trade per recurring occurrence.  Keep
        # the occurrence's recorded trade (or the earliest row) as canonical;
        # preserve duplicate trades as history but detach their ambiguous link.
        duplicates = conn.execute(
            """SELECT recurring_occurrence_id FROM trades
                 WHERE recurring_occurrence_id IS NOT NULL
                 GROUP BY recurring_occurrence_id HAVING COUNT(*)>1"""
        ).fetchall()
        for duplicate in duplicates:
            occurrence_id = int(duplicate[0])
            occurrence = conn.execute(
                "SELECT trade_id FROM recurring_occurrences WHERE id=?", (occurrence_id,)
            ).fetchone()
            trade_ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT id FROM trades WHERE recurring_occurrence_id=? ORDER BY id",
                    (occurrence_id,),
                ).fetchall()
            ]
            canonical = int(occurrence[0]) if occurrence and occurrence[0] in trade_ids else trade_ids[0]
            conn.execute(
                "UPDATE trades SET recurring_occurrence_id=NULL "
                "WHERE recurring_occurrence_id=? AND id<>?",
                (occurrence_id, canonical),
            )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_recurring_occurrence
                 ON trades(recurring_occurrence_id)
               WHERE recurring_occurrence_id IS NOT NULL"""
        )

    def _migrate_v31(self, conn):
        """Freeze billable request settings and add cross-process idempotency."""
        for name, declaration in (
            ("model_config_json", "TEXT NOT NULL DEFAULT '{}'") ,
            ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
            ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("lease_heartbeat_at", "TEXT NOT NULL DEFAULT ''"),
            ("lease_generation", "INTEGER NOT NULL DEFAULT 0"),
        ):
            self._ensure_column(conn, "analysis_jobs", name, declaration)
        self._ensure_column(conn, "model_calls", "request_key", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS ux_model_calls_request_key
                 ON model_calls(task_id,request_key)
               WHERE request_key<>''"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_jobs_lease "
            "ON analysis_jobs(status,lease_expires_at,task_id)"
        )

    def _migrate_v32(self, conn):
        """Link every current strict-C execution mapping into the fund library."""
        self._link_strict_funds_to_library(conn)

    def _migrate_v33(self, conn):
        """Repair score tiers and negated safety-veto phrases without another API call.

        The old parser searched for raw substrings.  It therefore treated phrases
        such as ``尚未形成极端拥挤`` as an affirmative hard veto and overwrote a
        46-point ``观察`` tier with ``千万别买``.  The original Flash/Pro responses
        remain in ``model_calls``/``ai_imports`` for audit.  This migration only
        corrects deterministic local derivatives in score components, displayed
        audit rows and the assembled run payload.
        """
        from .ranking import (
            finite_number,
            hard_avoid_reason,
            recommendation_label,
            timing_status_label,
        )

        rows = conn.execute(
            """SELECT a.id AS audit_id,a.task_id,a.universe_id,
                      a.recommendation_score,a.research_tilt,a.timing_status,
                      a.entry_score,a.crowding_risk_score,a.score_components_json,
                      e.id AS evidence_id,e.crowding_level,e.crowding AS evidence_crowding,
                      e.risk AS evidence_risk,e.catalyst AS evidence_catalyst
                 FROM radar_audit a
                 JOIN radar_runs r ON r.task_id=a.task_id
                 LEFT JOIN sector_evidence e
                   ON e.task_id=a.task_id AND e.universe_id=a.universe_id
                WHERE r.ranking_policy_version='four-dimension-v3'"""
        ).fetchall()
        changes_by_task: dict[str, dict[str, dict[str, Any]]] = {}
        cleared_false_vetoes = 0
        corrected_tiers = 0
        for raw in rows:
            row = dict(raw)
            try:
                components = json.loads(str(row.get("score_components_json") or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(components, dict) or not components:
                continue
            score = finite_number(
                row.get("recommendation_score"),
                finite_number(components.get("recommendation_score"), 0.0),
            )
            expected_tilt = recommendation_label(score)
            old_hard = bool(components.get("hard_avoid", False))
            if row.get("evidence_id") is not None:
                evidence = {
                    "crowding_level": row.get("crowding_level"),
                    "crowding": row.get("evidence_crowding"),
                    "risk": row.get("evidence_risk"),
                    "catalyst": row.get("evidence_catalyst"),
                }
                reason = hard_avoid_reason(evidence)
                new_hard = bool(reason)
            else:
                # A historical web import may not have local Flash evidence.
                # Never silently release an existing safety block that cannot be
                # replayed; only separate it from the visible score tier.
                new_hard = old_hard
                reason = str(components.get("hard_avoid_reason") or "")
                if new_hard and not reason:
                    reason = "历史硬否决缺少本地证据，保守保留安全拦截"
            entry = finite_number(
                components.get("entry_quality"), finite_number(row.get("entry_score"), 0.0)
            )
            crowding = finite_number(
                components.get("crowding_risk"),
                finite_number(row.get("crowding_risk_score"), 0.0),
            )
            expected_timing = timing_status_label(
                expected_tilt,
                entry_quality=entry,
                crowding_risk=crowding,
                hard_avoid=new_hard,
            )
            changed = (
                str(row.get("research_tilt") or "") != expected_tilt
                or str(row.get("timing_status") or "") != expected_timing
                or old_hard != new_hard
                or str(components.get("hard_avoid_reason") or "") != reason
            )
            if not changed:
                continue
            if old_hard and not new_hard:
                cleared_false_vetoes += 1
            if str(row.get("research_tilt") or "") != expected_tilt:
                corrected_tiers += 1
            components.update({
                "recommendation_score": round(score, 3),
                "research_tilt": expected_tilt,
                "timing_status": expected_timing,
                "hard_avoid": new_hard,
                "hard_avoid_reason": reason,
                "safety_parser_version": "negation-aware-v2",
            })
            components_json = json.dumps(
                components, ensure_ascii=False, separators=(",", ":")
            )
            local_reason = "本地四维可复算评分；已校正分数分档与否定句硬否决语义"
            conn.execute(
                """UPDATE radar_audit
                      SET research_tilt=?,timing_status=?,score_components_json=?,local_reason=?
                    WHERE id=?""",
                (expected_tilt, expected_timing, components_json, local_reason, row["audit_id"]),
            )
            conn.execute(
                """UPDATE sector_metrics
                      SET base_score=?,score_components_json=?
                    WHERE task_id=? AND universe_id=?""",
                (score, components_json, row["task_id"], row["universe_id"]),
            )
            conn.execute(
                """UPDATE candidates
                      SET status=?,research_tilt=?
                    WHERE task_id=? AND universe_id=?""",
                (
                    expected_tilt, expected_tilt,
                    row["task_id"], row["universe_id"],
                ),
            )
            changes_by_task.setdefault(str(row["task_id"]), {})[
                str(row["universe_id"])
            ] = {
                "research_tilt": expected_tilt,
                "timing_status": expected_timing,
                "score_components": components,
            }

        # ``radar_runs.raw_json`` is the locally assembled presentation payload,
        # not the immutable provider response.  Keep it consistent so alternative
        # views and exports cannot resurrect the repaired mismatch.  The Pro
        # avoid_list is intentionally untouched: it is an independent qualitative
        # review and no longer overwrites the score-derived seven-tier conclusion.
        for task_id, changes in changes_by_task.items():
            stored = conn.execute(
                "SELECT raw_json FROM radar_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            if not stored:
                continue
            try:
                payload = json.loads(str(stored[0] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for field, identity in (
                ("audit", "id"),
                ("candidates", "universe_id"),
                ("same_bucket_alternatives", "universe_id"),
            ):
                items = payload.get(field)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    change = changes.get(str(item.get(identity) or ""))
                    if not change:
                        continue
                    item["research_tilt"] = change["research_tilt"]
                    if field == "candidates":
                        item["status"] = change["research_tilt"]
                    else:
                        item["timing_status"] = change["timing_status"]
                    if field == "audit":
                        item["score_components"] = change["score_components"]
            conn.execute(
                "UPDATE radar_runs SET raw_json=? WHERE task_id=?",
                (json.dumps(payload, ensure_ascii=False), task_id),
            )
        conn.executemany(
            """INSERT INTO settings(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            [
                ("ranking_safety_parser_version", "negation-aware-v2"),
                ("ranking_false_veto_repairs", str(cleared_false_vetoes)),
                ("ranking_tier_repairs", str(corrected_tiers)),
            ],
        )

    def _migrate_v34(self, conn):
        """Add an immutable AI-recommendation observation ledger.

        This ledger is deliberately separate from trades, drafts, holdings and
        active cash.  It measures what the model said without ever pretending
        that the user placed an order.  Historical rows are not reconstructed:
        older candidates may already have been edited by a later action-advice
        pass, so backfilling them would manufacture evidence.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recommendation_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                source_candidate_id INTEGER,
                universe_id TEXT NOT NULL,
                index_code TEXT NOT NULL DEFAULT '',
                sector_name TEXT NOT NULL,
                fund_code TEXT NOT NULL DEFAULT '',
                fund_name TEXT NOT NULL DEFAULT '',
                fund_selection_kind TEXT NOT NULL DEFAULT '',
                rank_no INTEGER NOT NULL DEFAULT 0,
                recommendation_score REAL NOT NULL DEFAULT 0,
                research_tilt TEXT NOT NULL DEFAULT '观察',
                timing_status TEXT NOT NULL DEFAULT '',
                execution_status TEXT NOT NULL DEFAULT '',
                ranking_policy_version TEXT NOT NULL DEFAULT '',
                recommendation_at TEXT NOT NULL,
                data_cutoff TEXT NOT NULL DEFAULT '',
                pricing_date TEXT NOT NULL,
                index_provider TEXT NOT NULL DEFAULT '',
                index_security_id TEXT NOT NULL DEFAULT '',
                source_snapshot_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT '观察中',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, universe_id)
            );
            CREATE TABLE IF NOT EXISTS recommendation_outcomes (
                observation_id INTEGER NOT NULL REFERENCES recommendation_observations(id) ON DELETE CASCADE,
                horizon_sessions INTEGER NOT NULL,
                target_date TEXT NOT NULL,
                actual_end_date TEXT NOT NULL DEFAULT '',
                holding_calendar_days INTEGER,
                index_start_value REAL,
                index_end_value REAL,
                index_return_pct REAL,
                index_max_drawdown_pct REAL,
                fund_start_nav REAL,
                fund_end_nav REAL,
                fund_gross_return_pct REAL,
                purchase_fee_pct REAL,
                redemption_fee_pct REAL,
                fund_net_return_pct REAL,
                fund_max_drawdown_pct REAL,
                index_direction_hit INTEGER,
                fund_direction_hit INTEGER,
                status TEXT NOT NULL DEFAULT '等待行情',
                reason TEXT NOT NULL DEFAULT '',
                evaluated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(observation_id, horizon_sessions)
            );
            CREATE INDEX IF NOT EXISTS idx_recommendation_observations_time
                ON recommendation_observations(recommendation_at, ranking_policy_version, research_tilt);
            CREATE INDEX IF NOT EXISTS idx_recommendation_observations_fund
                ON recommendation_observations(fund_code, pricing_date);
            CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_status
                ON recommendation_outcomes(status, target_date, horizon_sessions);
            """
        )

    def _migrate_v35(self, conn):
        """Advance the software-owned ranking identifier without rewriting history.

        V4 centres crowding at 50 instead of charging a neutral reading as a
        full penalty.  Existing v3 jobs and snapshots remain immutable and can
        still be read, but their version mismatch prevents them from being
        resumed or used to generate a current action.  User-tuned weights,
        thresholds and capital settings are deliberately preserved.
        """
        conn.execute(
            """UPDATE settings SET value='four-dimension-v4'
                 WHERE key='ranking_policy_version'
                   AND (TRIM(value)='' OR value IN (
                       'four-dimension-v1','four-dimension-v2','four-dimension-v3'
                   ))"""
        )
        self._set_default(conn, "ranking_policy_version", "four-dimension-v4")

    def _migrate_v36(self, conn):
        """Remove only repeated legacy exclusion-reason clauses.

        Older releases could append the same software-owned migration note on
        every startup.  Keep the first spelling and every distinct clause so a
        user's own explanation is never inferred, replaced, or discarded.
        """
        changed = 0
        rows = conn.execute(
            """SELECT id, exclusion_reason
                 FROM investable_indices
                WHERE exclusion_reason LIKE '%；%'"""
        ).fetchall()
        for index_id, raw_reason in rows:
            original = str(raw_reason or "")
            clauses: list[str] = []
            seen: set[str] = set()
            for raw_clause in original.split("；"):
                clause = raw_clause.strip()
                if not clause:
                    continue
                comparison_key = clause.rstrip("。； \t\r\n")
                if comparison_key in seen:
                    continue
                seen.add(comparison_key)
                clauses.append(clause)
            cleaned = "；".join(clauses)
            if cleaned == original:
                continue
            conn.execute(
                "UPDATE investable_indices SET exclusion_reason=? WHERE id=?",
                (cleaned, int(index_id)),
            )
            changed += 1
        self._set_default(
            conn,
            "migration_v36_reason_rows_deduplicated",
            str(changed),
        )

    def _migrate_v37(self, conn):
        """Adopt official V4-Pro prices without overwriting user tuning.

        The preview release used 3.15/0.03/6.30 CNY per million tokens.  The
        official pricing page now lists 3/0.025/6.  Update only when all three
        stored values still exactly match the software-owned preview triplet;
        one customised field protects the complete triplet from migration.
        """
        old_prices = {
            "pro_input_price_cny_per_million": "3.15",
            "pro_cached_input_price_cny_per_million": "0.03",
            "pro_output_price_cny_per_million": "6.30",
        }
        new_prices = {
            "pro_input_price_cny_per_million": "3.0",
            "pro_cached_input_price_cny_per_million": "0.025",
            "pro_output_price_cny_per_million": "6.0",
        }
        keys = tuple(old_prices)
        placeholders = ",".join("?" for _ in keys)
        current = dict(conn.execute(
            f"SELECT key,value FROM settings WHERE key IN ({placeholders})", keys,
        ))
        updated = int(all(current.get(key) == value for key, value in old_prices.items()))
        if updated:
            conn.executemany(
                "UPDATE settings SET value=? WHERE key=?",
                [(new_prices[key], key) for key in keys],
            )
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("deepseek_pricing_revision", "official-v4-2026-08-15"),
                ("deepseek_pricing_source_url", "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"),
                ("migration_v37_official_pro_prices_updated", str(updated)),
            ],
        )
        self._set_default(conn, "fund_execution_refresh_workers", "6")

    def _migrate_v38(self, conn):
        """Persist the user's confirmation of an actual recurring deduction.

        A deduction is not the same thing as a confirmed fund transaction.
        These fields let the application remember that Alipay really deducted
        the scheduled amount on the original day while NAV, final units,
        confirmation date and fees remain pending.  Existing rows are left
        unconfirmed; migrations must never infer a real payment from a draft.
        """
        for name, declaration in [
            ("deduction_confirmed", "INTEGER NOT NULL DEFAULT 0"),
            ("deduction_confirmed_at", "TEXT NOT NULL DEFAULT ''"),
            ("deduction_confirmation_source", "TEXT NOT NULL DEFAULT ''"),
        ]:
            self._ensure_column(conn, "recurring_occurrences", name, declaration)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recurring_deduction_state "
            "ON recurring_occurrences(deduction_confirmed,status,scheduled_date,id)"
        )

    def _migrate_v39(self, conn):
        """Record narrow, auditable corrections to already-posted trades.

        Posted transactions remain immutable by default.  A platform fee is a
        special case: correcting it does not change the confirmed cash amount,
        units, NAV, holding units or cash ledger.  Keeping the before/after
        values in a separate table prevents a one-field correction from
        silently erasing the original evidence or requiring later trades to be
        reversed and recreated.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trade_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL REFERENCES trades(id),
                field_name TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trade_corrections_trade
            ON trade_corrections(trade_id,id);
            """
        )

    @staticmethod
    def _link_strict_funds_to_library(conn, index_id: int | None = None) -> int:
        params: list[Any] = [STRICT_FIELD_SYNC_ALGORITHM_VERSION]
        extra = ""
        if index_id is not None:
            extra = " AND i.id=?"
            params.append(int(index_id))
        rows = conn.execute(
            f"""SELECT f.*,i.ant_index_code,i.official_index_name,
                       i.industry_group,i.allocation_cluster
                  FROM investable_funds f
                  JOIN investable_indices i ON i.id=f.investable_index_id
                 WHERE i.active=1 AND f.active=1
                   AND f.relation_kind='同一指数' AND UPPER(f.share_class)='C'
                   AND f.strategy_kind IN ('被动ETF联接','被动指数')
                   AND f.passive_verified=1 AND f.verification_algorithm=?
                   AND (f.public_buyable_status IN
                        ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')
                        OR f.public_buyable_status GLOB '限购*元'){extra}
                 ORDER BY i.display_order,f.quality_rank,f.fund_code""",
            params,
        ).fetchall()
        now = local_now().isoformat(timespec="seconds")
        inserted = 0
        for raw in rows:
            row = dict(raw)
            code = str(row.get("fund_code") or "").strip()
            if not code:
                continue
            exists = conn.execute(
                "SELECT 1 FROM funds WHERE fund_code=?", (code,)
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO funds(
                           fund_code,fund_name,index_code,index_name,share_class,
                           correlation_bucket,purchase_fee_pct,status,notes,updated_at,
                           name_status,profile_source_url,profile_checked_at
                       ) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                    (
                        code,
                        str(row.get("fund_name") or code),
                        str(row.get("ant_index_code") or ""),
                        str(row.get("official_index_name") or ""),
                        "C",
                        str(row.get("allocation_cluster") or row.get("industry_group") or "待分类"),
                        "严格C目录·费率待核验",
                        "由严格同指数、场外被动C类目录自动衔接；下单前仍需当天申购复核。",
                        now,
                        "已自动识别",
                        str(row.get("source_url") or ""),
                        str(row.get("verified_at") or now),
                    ),
                )
                inserted += 1
                continue
            # Existing rows may contain user-owned status, notes, names and fee
            # choices.  Only fill fields that are demonstrably blank/placeholder.
            conn.execute(
                """UPDATE funds SET
                       fund_name=CASE WHEN TRIM(fund_name)='' OR name_status IN ('待自动识别','识别失败')
                                      THEN ? ELSE fund_name END,
                       index_code=CASE WHEN TRIM(index_code)='' THEN ? ELSE index_code END,
                       index_name=CASE WHEN TRIM(index_name)='' THEN ? ELSE index_name END,
                       share_class=CASE WHEN TRIM(share_class)='' THEN 'C' ELSE share_class END,
                       correlation_bucket=CASE WHEN TRIM(correlation_bucket)='' OR correlation_bucket='待分类'
                                               THEN ? ELSE correlation_bucket END,
                       profile_source_url=CASE WHEN TRIM(profile_source_url)='' THEN ? ELSE profile_source_url END,
                       profile_checked_at=CASE WHEN TRIM(profile_checked_at)='' THEN ? ELSE profile_checked_at END
                     WHERE fund_code=?""",
                (
                    str(row.get("fund_name") or code),
                    str(row.get("ant_index_code") or ""),
                    str(row.get("official_index_name") or ""),
                    str(row.get("allocation_cluster") or row.get("industry_group") or "待分类"),
                    str(row.get("source_url") or ""),
                    str(row.get("verified_at") or now),
                    code,
                ),
            )
        return inserted

    def link_strict_funds_to_library(self, index_id: int | None = None) -> int:
        """Public runtime counterpart of migration v32."""
        with self.connect() as conn:
            return self._link_strict_funds_to_library(conn, index_id)

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def settings(self) -> dict[str, str]:
        with self.connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}

    def save_settings(self, values: dict[str, Any]):
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, str(v)) for k, v in values.items()],
            )

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            r = conn.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]):
        with self.connect() as conn:
            conn.executemany(sql, rows)

    def recover_interrupted_jobs(self) -> int:
        """Convert process-crash leftovers into resumable failed tasks on startup."""
        now = local_now().isoformat(timespec="seconds")
        message = (
            "上次软件进程在任务结束前退出；启动时已把任务恢复为失败状态。"
            "已入库证据和请求审计均保留，续跑仍会遵守幂等与不明计费拦截。"
        )
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT task_id,error FROM analysis_jobs WHERE status='运行中'"
            ).fetchall()
            for row in rows:
                old_error = str(row["error"] or "").strip()
                error = message if not old_error else f"{old_error}；{message}"
                conn.execute(
                    """UPDATE analysis_jobs
                          SET status='失败',stage='启动恢复',error=?,updated_at=?,
                              lease_owner='',lease_expires_at='',lease_heartbeat_at=''
                        WHERE task_id=? AND status='运行中'""",
                    (error[:4000], now, row["task_id"]),
                )
            return len(rows)

    def backup(
        self, destination: Path | None = None, *, prune_automatic: bool = True,
    ) -> Path:
        automatic = destination is None
        stamp = local_now().strftime("%Y%m%d_%H%M%S_%f")
        destination = Path(destination) if destination is not None else BACKUP_DIR / f"板块雷达备份_{stamp}.sqlite3"
        if destination.resolve() == self.path.resolve():
            raise ValueError("备份目标不能覆盖正在使用的数据库。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        check = sqlite3.connect(destination)
        try:
            if str(check.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                check.close()
                destination.unlink(missing_ok=True)
                raise RuntimeError("新备份未通过SQLite完整性检查，已丢弃损坏副本。")
        finally:
            try:check.close()
            except Exception:pass
        if automatic and prune_automatic:
            self.prune_backups()
        return destination

    def prune_backups(
        self,
        stop_requested: Callable[[], bool] | None = None,
        *,
        include_deployment: bool = False,
    ) -> dict[str, Any]:
        """Bound proven backups while preserving daily restore points.

        Deployment snapshots require an explicit user-initiated cleanup.  The
        automatic startup/backup maintenance path deliberately leaves them
        untouched so a newly published version cannot silently delete its own
        rollback evidence.
        """
        try:
            keep_count = max(3, min(100, int(float(self.setting("backup_keep_count", "14")))))
        except (TypeError, ValueError):
            keep_count = 14
        try:
            keep_days = max(1, min(3650, int(float(self.setting("backup_keep_days", "30")))))
        except (TypeError, ValueError):
            keep_days = 30
        try:
            deployment_keep_count = max(2, min(20, int(float(
                self.setting("deployment_backup_keep_count", "3")
            ))))
        except (TypeError, ValueError):
            deployment_keep_count = 3
        try:
            deployment_keep_days = max(7, min(3650, int(float(
                self.setting("deployment_backup_keep_days", "30")
            ))))
        except (TypeError, ValueError):
            deployment_keep_days = 30
        root = BACKUP_DIR.resolve()
        sidecars = prune_automatic_backup_sidecars(root, stop_requested)
        plan = automatic_backup_retention_plan(
            root,
            keep_count=keep_count,
            keep_days=keep_days,
            now=local_now().timestamp(),
            stop_requested=stop_requested,
        )
        removed = 0
        removed_bytes = 0
        removed_names: list[str] = []
        removed_reasons: dict[str, str] = {}
        errors = [*sidecars["errors"], *plan["errors"]]
        for path, reason in plan["removable"]:
            if stop_requested is not None and stop_requested():
                break
            try:
                resolved = path.resolve(strict=True)
                # Revalidate immediately before deletion.  A file changed
                # after planning becomes protected rather than disposable.
                if resolved.parent != root or not valid_automatic_backup_file(
                    resolved, root, stop_requested,
                ):
                    continue
                size = resolved.stat().st_size
                resolved.unlink()
                removed += 1
                removed_bytes += size
                removed_names.append(path.name)
                removed_reasons[path.name] = reason
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
        deployment_sidecars: dict[str, Any] = {
            "removed": 0, "bytes_freed": 0, "removed_names": [],
            "protected": 0, "errors": [], "stopped": False,
        }
        deployment_plan: dict[str, Any] = {
            "valid": [], "retained_latest": [], "retained_daily": [],
            "removable": [], "errors": [], "stopped": False,
        }
        deployment_removed = 0
        deployment_removed_bytes = 0
        deployment_removed_names: list[str] = []
        deployment_removed_reasons: dict[str, str] = {}
        if include_deployment and not (
            bool(plan.get("stopped"))
            or bool(sidecars.get("stopped"))
            or bool(stop_requested is not None and stop_requested())
        ):
            deployment_sidecars = prune_deployment_backup_sidecars(
                root, stop_requested,
            )
            deployment_plan = deployment_backup_retention_plan(
                root,
                keep_count=deployment_keep_count,
                keep_days=deployment_keep_days,
                now=local_now().timestamp(),
                stop_requested=stop_requested,
            )
            errors.extend(deployment_sidecars["errors"])
            errors.extend(deployment_plan["errors"])
            for path, reason in deployment_plan["removable"]:
                if stop_requested is not None and stop_requested():
                    break
                try:
                    resolved = path.resolve(strict=True)
                    if resolved.parent != root or not valid_deployment_backup_file(
                        resolved, root, stop_requested,
                    ):
                        continue
                    size = resolved.stat().st_size
                    resolved.unlink()
                    deployment_removed += 1
                    deployment_removed_bytes += size
                    deployment_removed_names.append(path.name)
                    deployment_removed_reasons[path.name] = reason
                except OSError as exc:
                    errors.append(f"{path.name}: {exc}")
        return {
            "kept": len(plan["valid"]) - removed,
            "removed": removed,
            "removed_bytes": removed_bytes,
            "retained_latest": len(plan["retained_latest"]),
            "retained_daily": len(plan["retained_daily"]),
            "removed_names": removed_names,
            "removed_reasons": removed_reasons,
            "sidecars_removed": int(sidecars["removed"]),
            "sidecar_bytes_freed": int(sidecars["bytes_freed"]),
            "sidecar_removed_names": list(sidecars["removed_names"]),
            "protected_sidecars": int(sidecars["protected"]),
            "deployment_cleanup_requested": bool(include_deployment),
            "deployment_kept": (
                len(deployment_plan["valid"]) - deployment_removed
                if include_deployment else 0
            ),
            "deployment_removed": deployment_removed,
            "deployment_removed_bytes": deployment_removed_bytes,
            "deployment_retained_latest": len(deployment_plan["retained_latest"]),
            "deployment_retained_daily": len(deployment_plan["retained_daily"]),
            "deployment_removed_names": deployment_removed_names,
            "deployment_removed_reasons": deployment_removed_reasons,
            "deployment_sidecars_removed": int(deployment_sidecars["removed"]),
            "deployment_sidecar_bytes_freed": int(
                deployment_sidecars["bytes_freed"]
            ),
            "deployment_sidecar_removed_names": list(
                deployment_sidecars["removed_names"]
            ),
            "deployment_protected_sidecars": int(
                deployment_sidecars["protected"]
            ),
            "errors": errors,
            "stopped": (
                bool(sidecars.get("stopped"))
                or bool(plan.get("stopped"))
                or bool(deployment_sidecars.get("stopped"))
                or bool(deployment_plan.get("stopped"))
                or bool(stop_requested is not None and stop_requested())
            ),
        }

    def export_json(self, destination: Path) -> Path:
        tables = [
            "settings", "funds", "fee_tiers", "holdings", "trades", "universe", "radar_runs", "radar_audit", "candidates",
            "analysis_jobs", "analysis_events", "sector_metrics", "sector_evidence", "model_calls", "fund_mappings",
            "proxy_mappings", "market_bars", "market_fetch_attempts", "recurring_plans", "recurring_occurrences", "cost_lots",
            "fund_nav_history", "fund_valuation_snapshots",
            "ledger_postings", "lot_consumptions", "active_cash_ledger", "portfolio_equity_snapshots",
            "recommendation_observations", "recommendation_outcomes",
            "fund_discovery_runs", "fund_discovery_candidates",
            "decision_drafts", "investable_indices", "investable_funds",
            "csi_index_products", "csi_product_sync_status", "csi_fund_sync_status",
        ]
        payload = {"exported_at": local_now().isoformat(timespec="seconds"), "schema_version": SCHEMA_VERSION}
        for table in tables:
            payload[table] = self.rows(f"SELECT * FROM {table}")
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination

    @staticmethod
    def _validate_restore_source(source: Path) -> int:
        test = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        try:
            required = {r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"settings", "funds", "holdings", "candidates"}.issubset(required):
                raise ValueError("这不是有效的板块雷达数据库备份。")
            if str(test.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                raise ValueError("所选备份未通过SQLite完整性检查。")
            schema_row = test.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            try:
                return int(str(schema_row[0])) if schema_row else 1
            except (TypeError, ValueError) as exc:
                raise ValueError("所选备份的数据库版本标记损坏。") from exc
        finally:
            test.close()

    @staticmethod
    def _copy_sqlite_database(source: Path, destination: Path) -> None:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()

    @staticmethod
    def _sqlite_integrity_ok(path: Path) -> bool:
        conn = sqlite3.connect(path)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        finally:
            conn.close()

    @staticmethod
    def _checkpoint_and_exclusive(path: Path) -> None:
        conn = sqlite3.connect(path, timeout=15)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.execute("BEGIN EXCLUSIVE")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _enable_wal(path: Path) -> None:
        conn = sqlite3.connect(path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.commit()
        finally:
            conn.close()

    def restore(self, source: Path):
        source = Path(source).resolve()
        target = self.path.resolve()
        if source == target:
            raise ValueError("不能把正在使用的数据库选作恢复来源。")
        if not source.is_file():
            raise FileNotFoundError("找不到所选数据库备份。")
        source_schema = self._validate_restore_source(source)
        if source_schema > SCHEMA_VERSION:
            raise ValueError(
                f"备份数据库版本{source_schema}高于本软件支持的{SCHEMA_VERSION}，禁止降级覆盖。"
            )
        safety = self.backup()
        restore_temp = target.with_name(
            f".{target.name}.restore-{local_now().strftime('%Y%m%d%H%M%S%f')}.tmp"
        )
        rollback_temp = target.with_name(
            f".{target.name}.rollback-{local_now().strftime('%Y%m%d%H%M%S%f')}.tmp"
        )
        try:
            self._copy_sqlite_database(source, restore_temp)
            if not self._sqlite_integrity_ok(restore_temp):
                raise ValueError("恢复临时副本未通过完整性检查。")
            self._checkpoint_and_exclusive(target)
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            restore_temp.replace(target)
            self._enable_wal(target)
            self._initialize()
            with self.connect() as verified:
                if str(verified.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                    raise RuntimeError("恢复后的数据库未通过完整性检查。")
        except Exception:
            restore_temp.unlink(missing_ok=True)
            self._copy_sqlite_database(safety, rollback_temp)
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            rollback_temp.replace(target)
            self._enable_wal(target)
            self._initialize()
            raise
        finally:
            restore_temp.unlink(missing_ok=True)
            rollback_temp.unlink(missing_ok=True)
        return safety


def today_text() -> str:
    return local_today().isoformat()

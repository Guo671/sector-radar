from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from threading import Event, local
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

from .config import local_now
from .deepseek import DeepSeekClient, DeepSeekError, ModelResult
from .market_data import MarketDataPipeline
from .portfolio import active_capital_holdings, active_capital_summary, allocate_candidate_amounts
from .platform_catalog import PlatformCatalogService
from .protocol import END_MARKER, START_MARKER, parse_result
from .ranking import (
    DEFAULT_POLICY,
    POSITIVE_TILTS,
    RANKING_FORMULA_VERSION,
    RESEARCH_TILT_LABELS,
    build_scorecards,
    final_display_candidates,
    select_candidate_pool,
)
from .services import _catalog_snapshot_hash, import_radar_payload, save_action_advice
from .trading_calendar import ChinaTradingCalendar
from .universe import UNIVERSE_VERSION


ProgressCallback = Callable[[str, int, int, str], None]
ACTION_CANDIDATE_COUNT = 10
RANKING_POLICY_KEYS = {
    "trend_weight": "ranking_trend_weight",
    "entry_weight": "ranking_entry_weight",
    "catalyst_weight": "ranking_catalyst_weight",
    "relative_strength_weight": "ranking_relative_strength_weight",
    "volume_breadth_weight": "ranking_volume_breadth_weight",
    "crowding_penalty": "ranking_crowding_penalty",
}
MARKET_TEST_MODES = {"20项行情测试", "10项行情测试"}
FULL_MARKET_MODE = "全目录行情更新"
MARKET_ONLY_MODES = MARKET_TEST_MODES | {FULL_MARKET_MODE}
FLASH_TEST_MODES = {"20方向测试", "10方向测试"}
LEASE_TTL_SECONDS = 15 * 60
MODEL_SETTING_DEFAULTS = {
    "deepseek_flash_model": "deepseek-v4-flash",
    "deepseek_pro_model": "deepseek-v4-pro",
    "flash_input_price_cny_per_million": "1",
    "flash_cached_input_price_cny_per_million": "0.02",
    "flash_output_price_cny_per_million": "2",
    "pro_input_price_cny_per_million": "3.0",
    "pro_cached_input_price_cny_per_million": "0.025",
    "pro_output_price_cny_per_million": "6.0",
    "api_test_batch_size": "3",
    "api_full_batch_size": "6",
    "api_test_max_output_tokens": "6000",
    "api_full_flash_max_output_tokens": "12000",
    "api_test_flash_reasoning_effort": "none",
    "api_full_flash_reasoning_effort": "low",
    "api_auto_finalize_flash": "1",
    "api_pro_max_tokens": "12000",
    "api_pro_hard_output_cap_tokens": "12000",
    "pro_reasoning_effort": "max",
}


@dataclass
class JobResult:
    task_id: str
    status: str
    completed_items: int
    total_items: int
    estimated_cost_cny: float
    message: str


@dataclass
class ActionAdviceResult:
    task_id: str
    status: str
    estimated_cost_cny: float
    buy_count: int
    holding_count: int
    message: str


@dataclass(frozen=True)
class FullScanCostEstimate:
    """Read-only estimate for one clean full scan.

    ``budget_limit_cny`` is a dispatch guard, not a price quote.  The estimate
    deliberately keeps the most recent same-size real token usage separate
    from a conservative clean-run reservation so the confirmation dialog does
    not make the old mistake of presenting the configured limit as the likely
    charge.
    """

    item_count: int
    flash_batch_count: int
    pro_call_count: int
    expected_low_cny: float
    expected_high_cny: float
    conservative_clean_run_cny: float
    budget_limit_cny: float
    historical_repriced_cny: float | None
    historical_task_id: str
    flash_model: str
    pro_model: str
    pricing_revision: str
    pricing_source_url: str

    def confirmation_text(self) -> str:
        history = (
            f"最近同规模成功轮次按当前官方单价重算约¥{self.historical_repriced_cny:.2f}；"
            if self.historical_repriced_cny is not None
            else "尚无同规模成功轮次，以下范围按当前批次结构和保守Token假设计算；"
        )
        return (
            f"正式模型：{self.flash_model} + {self.pro_model}\n"
            f"当前{self.item_count}项预计发送{self.flash_batch_count}个Flash批次 + "
            f"{self.pro_call_count}个Pro终审。\n"
            f"{history}\n"
            f"本轮常见预计约¥{self.expected_low_cny:.2f}—¥{self.expected_high_cny:.2f}；"
            f"一次正常完成的保守预留约¥{self.conservative_clean_run_cny:.2f}。\n"
            f"发请求预算阈值为¥{self.budget_limit_cny:.2f}，它不是预计花费。"
        )


class TaskCancelled(DeepSeekError):
    pass


class ResearchOrchestrator:
    def __init__(
        self,
        db,
        client: DeepSeekClient | None = None,
        market_pipeline: MarketDataPipeline | None = None,
        cancel_event: Event | None = None,
    ):
        self.db = db
        self.client = client
        self.market_pipeline = market_pipeline or MarketDataPipeline(db)
        self.cancel_event = cancel_event or Event()
        self._lease_context = local()

    def request_stop(self) -> None:
        self.cancel_event.set()

    def _current_model_config(self) -> dict[str, str]:
        return {
            key: str(self.db.setting(key, default))
            for key, default in MODEL_SETTING_DEFAULTS.items()
        }

    @staticmethod
    def _usage_cost_at_current_prices(
        row: dict[str, Any], input_price: float, cached_price: float, output_price: float,
    ) -> float:
        input_tokens = max(0, int(row.get("input_tokens") or 0))
        cached_tokens = min(input_tokens, max(0, int(row.get("cached_tokens") or 0)))
        output_tokens = max(0, int(row.get("output_tokens") or 0))
        return (
            (input_tokens - cached_tokens) * input_price
            + cached_tokens * cached_price
            + output_tokens * output_price
        ) / 1_000_000

    def estimate_full_scan_cost(self, item_count: int) -> FullScanCostEstimate:
        """Estimate a clean run without creating a task or contacting DeepSeek."""
        count = int(item_count)
        if count <= 0:
            raise ValueError("完整扫描项目数必须大于0。")
        config = self._current_model_config()

        def integer_setting(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(float(config.get(name, str(default))))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        batch_size = integer_setting("api_full_batch_size", 6, 1, 12)
        flash_batches = math.ceil(count / batch_size)
        flash_max_output = integer_setting(
            "api_full_flash_max_output_tokens", 12000, 2000, 32000,
        )
        pro_configured_output = integer_setting("api_pro_max_tokens", 12000, 4000, 12000)
        pro_hard_output = integer_setting(
            "api_pro_hard_output_cap_tokens", 12000, 4000, 12000,
        )
        pro_max_output = min(pro_configured_output, pro_hard_output)
        flash_input_price = float(config["flash_input_price_cny_per_million"])
        flash_cached_price = float(config["flash_cached_input_price_cny_per_million"])
        flash_output_price = float(config["flash_output_price_cny_per_million"])
        pro_input_price = float(config["pro_input_price_cny_per_million"])
        pro_cached_price = float(config["pro_cached_input_price_cny_per_million"])
        pro_output_price = float(config["pro_output_price_cny_per_million"])
        budget_limit = max(0.0, float(self.db.setting("api_full_cost_limit_cny", "2.00")))

        history = self.db.row(
            """SELECT task_id,total_items
                 FROM analysis_jobs
                WHERE mode='完整扫描' AND status='已完成'
                  AND total_items=? AND completed_items=total_items
                ORDER BY id DESC LIMIT 1""",
            (count,),
        )
        calls = self.db.rows(
            """SELECT stage,model,input_tokens,output_tokens,cached_tokens,response_status
                 FROM model_calls WHERE task_id=? AND response_status='completed'
                ORDER BY id""",
            (history["task_id"],),
        ) if history else []
        flash_rows = [
            row for row in calls
            if str(row.get("model") or "") == config["deepseek_flash_model"]
            or str(row.get("stage") or "").startswith("Flash")
        ]
        pro_rows = [
            row for row in calls
            if str(row.get("model") or "") == config["deepseek_pro_model"]
            or str(row.get("stage") or "").startswith("Pro")
        ]

        historical_repriced: float | None = None
        historical_task_id = ""
        if history and flash_rows and pro_rows:
            historical_task_id = str(history["task_id"])
            flash_costs = [
                self._usage_cost_at_current_prices(
                    row, flash_input_price, flash_cached_price, flash_output_price,
                )
                for row in flash_rows
            ]
            pro_costs = [
                self._usage_cost_at_current_prices(
                    row, pro_input_price, pro_cached_price, pro_output_price,
                )
                for row in pro_rows
            ]
            baseline = sum(flash_costs) / len(flash_costs) * flash_batches + sum(pro_costs)
            historical_repriced = baseline
            average_flash_input = sum(
                max(0, int(row.get("input_tokens") or 0)) for row in flash_rows
            ) / len(flash_rows)
            average_pro_input = sum(
                max(0, int(row.get("input_tokens") or 0)) for row in pro_rows
            ) / len(pro_rows)
        else:
            # Deliberately cautious local assumptions for the first run.  They
            # affect only display and never relax the real per-request guard.
            average_flash_input = 18_000.0
            average_pro_input = 80_000.0
            baseline = (
                flash_batches * (
                    average_flash_input * flash_input_price
                    + 4_000 * flash_output_price
                )
                + average_pro_input * pro_input_price
                + 6_000 * pro_output_price
            ) / 1_000_000

        expected_low = max(0.01, baseline * 0.90)
        expected_high = max(expected_low, baseline * 1.40)
        conservative = (
            flash_batches * (
                math.ceil(average_flash_input * 1.20) * flash_input_price
                + flash_max_output * flash_output_price
            )
            + math.ceil(average_pro_input * 1.20) * pro_input_price
            + pro_max_output * pro_output_price
        ) / 1_000_000
        return FullScanCostEstimate(
            item_count=count,
            flash_batch_count=flash_batches,
            pro_call_count=1,
            expected_low_cny=round(expected_low, 4),
            expected_high_cny=round(expected_high, 4),
            conservative_clean_run_cny=round(conservative, 4),
            budget_limit_cny=round(budget_limit, 4),
            historical_repriced_cny=(
                round(historical_repriced, 4) if historical_repriced is not None else None
            ),
            historical_task_id=historical_task_id,
            flash_model=config["deepseek_flash_model"],
            pro_model=config["deepseek_pro_model"],
            pricing_revision=str(self.db.setting("deepseek_pricing_revision", "")),
            pricing_source_url=str(self.db.setting("deepseek_pricing_source_url", "")),
        )

    def _model_config_for_job(self, task_id: str) -> dict[str, str]:
        row = self.db.row(
            "SELECT model_config_json FROM analysis_jobs WHERE task_id=?", (task_id,)
        ) or {}
        raw = str(row.get("model_config_json") or "").strip()
        try:
            config = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise DeepSeekError("任务冻结的模型设置JSON已损坏，禁止发送API请求。") from exc
        if isinstance(config, dict) and all(key in config for key in MODEL_SETTING_DEFAULTS):
            return {key: str(config[key]) for key in MODEL_SETTING_DEFAULTS}
        calls = self.db.row(
            "SELECT COUNT(*) AS n FROM model_calls WHERE task_id=?", (task_id,)
        )
        if int((calls or {}).get("n") or 0) > 0:
            raise DeepSeekError(
                "该旧任务没有冻结模型与计价设置，但已经发生过API调用；为避免续跑混用模型，"
                "只能历史查看或新建任务。"
            )
        config = self._current_model_config()
        self.db.execute(
            "UPDATE analysis_jobs SET model_config_json=? WHERE task_id=?",
            (json.dumps(config, ensure_ascii=False, sort_keys=True), task_id),
        )
        return config

    def _acquire_job_lease(self, task_id: str) -> str:
        owner = uuid4().hex
        now = local_now()
        now_text = now.isoformat(timespec="seconds")
        expires = (now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT lease_owner,lease_expires_at FROM analysis_jobs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                raise ValueError("找不到扫描任务。")
            current_owner = str(row["lease_owner"] or "")
            current_expiry = str(row["lease_expires_at"] or "")
            if current_owner and current_expiry > now_text:
                raise DeepSeekError(
                    "同一研究任务正在另一个软件窗口或后台线程中运行；已阻止重复扫描和重复扣费。"
                )
            cursor = conn.execute(
                """UPDATE analysis_jobs
                      SET lease_owner=?,lease_expires_at=?,lease_heartbeat_at=?,
                          lease_generation=COALESCE(lease_generation,0)+1
                    WHERE task_id=? AND (
                          COALESCE(lease_owner,'')='' OR COALESCE(lease_expires_at,'')<=?
                    )""",
                (owner, expires, now_text, task_id, now_text),
            )
            if cursor.rowcount != 1:
                raise DeepSeekError("任务租约刚被其他进程取得；已停止本次重复运行。")
        self._lease_context.task_id = task_id
        self._lease_context.owner = owner
        return owner

    def _release_job_lease(self, task_id: str, owner: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE analysis_jobs
                      SET lease_owner='',lease_expires_at='',lease_heartbeat_at=''
                    WHERE task_id=? AND lease_owner=?""",
                (task_id, owner),
            )
        if getattr(self._lease_context, "task_id", None) == task_id:
            self._lease_context.task_id = None
            self._lease_context.owner = None

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("用户已要求停止；软件不会再发送新的联网或API请求。已经在途的请求无法保证撤销，其用量仍会审计。")

    def _universe_hash(self, universe_ids: list[str]) -> str:
        return _catalog_snapshot_hash(self.db, universe_ids)

    @staticmethod
    def _validated_ranking_policy(policy: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize a frozen ranking policy.

        Data confidence is deliberately a gate, not a positive score component.
        Invalid settings fail closed before an API request instead of silently
        changing the meaning of a completed research round.
        """
        normalized: dict[str, Any] = {}
        for name, default in DEFAULT_POLICY.items():
            try:
                value = float(policy.get(name, default))
            except (TypeError, ValueError) as exc:
                raise DeepSeekError(f"评分策略{name}不是数字，任务未启动。") from exc
            if not math.isfinite(value) or value < 0:
                raise DeepSeekError(f"评分策略{name}必须是非负有限数，任务未启动。")
            normalized[name] = value
        weight_names = tuple(name for name in RANKING_POLICY_KEYS if name.endswith("_weight"))
        weight_sum = sum(float(normalized[name]) for name in weight_names)
        if abs(weight_sum - 1.0) > 1e-6:
            raise DeepSeekError(f"五项机会权重之和必须为1，当前为{weight_sum:.6f}，任务未启动。")

        integer_rules = {
            "pool_min": (20, 30),
            "pool_max": (20, 30),
            "pool_cluster_max": (1, 20),
            "display_cluster_max": (1, 10),
            "funded_cluster_max": (1, 10),
        }
        for name, (low, high) in integer_rules.items():
            try:
                raw_value = float(policy.get(name))
            except (TypeError, ValueError) as exc:
                raise DeepSeekError(f"评分策略{name}不是整数，任务未启动。") from exc
            if not math.isfinite(raw_value) or not raw_value.is_integer():
                raise DeepSeekError(f"评分策略{name}必须是整数，任务未启动。")
            value = int(raw_value)
            if not low <= value <= high:
                raise DeepSeekError(f"评分策略{name}必须在{low}到{high}之间，任务未启动。")
            normalized[name] = value
        if normalized["pool_min"] > normalized["pool_max"]:
            raise DeepSeekError("候选池最小数量不能大于最大数量，任务未启动。")
        if normalized["display_cluster_max"] > normalized["pool_cluster_max"]:
            raise DeepSeekError("主榜同桶上限不能大于候选池同桶上限，任务未启动。")
        if normalized["funded_cluster_max"] > normalized["display_cluster_max"]:
            raise DeepSeekError("正金额同桶上限不能大于主榜同桶上限，任务未启动。")

        for name in ("data_confidence_min", "candidate_min_score"):
            try:
                value = float(policy.get(name))
            except (TypeError, ValueError) as exc:
                raise DeepSeekError(f"评分策略{name}不是数字，任务未启动。") from exc
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise DeepSeekError(f"评分策略{name}必须在0到100之间，任务未启动。")
            normalized[name] = value
        version = str(policy.get("version") or "").strip()
        if not version:
            raise DeepSeekError("评分策略缺少版本号，任务未启动。")
        if version != RANKING_FORMULA_VERSION:
            raise DeepSeekError(
                f"评分策略版本为{version}，当前算法为{RANKING_FORMULA_VERSION}；"
                "旧公式任务只能历史查看，不能续跑或生成行动。"
            )
        normalized["version"] = version
        return normalized

    def _current_ranking_policy(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            name: self.db.setting(setting, str(DEFAULT_POLICY[name]))
            for name, setting in RANKING_POLICY_KEYS.items()
        }
        raw.update({
            "data_confidence_min": self.db.setting("ranking_data_confidence_min", "70"),
            "candidate_min_score": self.db.setting("ranking_candidate_min_score", "45"),
            "pool_min": self.db.setting("ranking_pool_min", "20"),
            "pool_max": self.db.setting("ranking_pool_max", "30"),
            "pool_cluster_max": self.db.setting("ranking_pool_cluster_max", "4"),
            "display_cluster_max": self.db.setting("ranking_display_cluster_max", "2"),
            "funded_cluster_max": self.db.setting("ranking_funded_cluster_max", "1"),
            "version": self.db.setting("ranking_policy_version", RANKING_FORMULA_VERSION),
        })
        return self._validated_ranking_policy(raw)

    def _policy_for_job(self, task_id: str) -> dict[str, Any]:
        row = self.db.row(
            "SELECT ranking_policy_version,ranking_policy_json FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        ) or {}
        version = str(row.get("ranking_policy_version") or "").strip()
        raw_json = str(row.get("ranking_policy_json") or "").strip()
        if not version or not raw_json:
            raise DeepSeekError("该任务没有冻结四维评分策略，属于旧算法任务；只能历史查看，不能续跑或生成行动。")
        try:
            policy = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise DeepSeekError("任务冻结的四维评分策略已损坏，禁止续跑。") from exc
        if not isinstance(policy, dict):
            raise DeepSeekError("任务冻结的四维评分策略不是JSON对象，禁止续跑。")
        policy["version"] = version
        return self._validated_ranking_policy(policy)

    def _expected_data_cutoff(self, metrics: list[dict[str, Any]]) -> str:
        calendar_cutoff = (
            ChinaTradingCalendar.from_db(self.db)
            .latest_completed_trading_day(local_now())
            .isoformat()
        )
        # The live calendar remains authoritative.  If a task crosses 15:00,
        # metrics fetched before the close can carry the previous expected day;
        # do not crash after Flash has already incurred a fee.  Scoring against
        # the live cutoff marks those rows stale, keeps the research readable,
        # and lets the execution gate force every suggested amount to zero.
        return calendar_cutoff

    @staticmethod
    def _snapshot_ids_for_mode(mode: str, universe: list[dict[str, Any]]) -> list[str]:
        ids = [str(row.get("index_code") or "").strip() for row in universe]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("当前研究目录含空白或重复ID，已停止创建任务。")
        if mode == "Pro结构测试":
            return []
        if mode in MARKET_TEST_MODES | FLASH_TEST_MODES:
            return ids[:10]
        return ids

    def _load_job_universe(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """Restore the exact immutable input snapshot for one research job."""
        task_id = str(job.get("task_id") or "")
        version = str(job.get("universe_version") or "").strip()
        research_date = str(job.get("research_date") or "").strip()
        raw_ids = str(job.get("universe_ids_json") or "").strip()
        stored_hash = str(job.get("universe_hash") or "").strip()
        if not version or not research_date or not raw_ids or not stored_hash:
            raise DeepSeekError(
                f"任务{task_id or '<未知>'}属于旧版，未固化研究目录快照；为避免把旧任务套到新目录，禁止续跑。请新建任务。"
            )
        if version != UNIVERSE_VERSION:
            raise DeepSeekError(
                f"任务目录版本为{version}，当前为{UNIVERSE_VERSION}；禁止跨目录续跑，请新建任务。"
            )
        today = local_now().date().isoformat()
        if research_date != today:
            raise DeepSeekError(
                f"任务研究日为{research_date}，今天为{today}；新闻证据和行情不能跨日复用，请新建任务。"
            )
        try:
            universe_ids = json.loads(raw_ids)
        except json.JSONDecodeError as exc:
            raise DeepSeekError("任务目录快照JSON损坏，禁止续跑。") from exc
        if (
            not isinstance(universe_ids, list)
            or any(not isinstance(value, str) or not value.strip() for value in universe_ids)
            or len(universe_ids) != len(set(universe_ids))
        ):
            raise DeepSeekError("任务目录快照含空白、重复或非法ID，禁止续跑。")
        if self._universe_hash(universe_ids) != stored_hash:
            raise DeepSeekError("任务目录快照校验失败，禁止续跑。")
        if str(job.get("mode") or "") != "Pro结构测试" and int(job.get("total_items") or 0) != len(universe_ids):
            raise DeepSeekError("任务总数与固化目录快照不一致，禁止续跑。")
        # A task must keep using the exact ranking policy frozen when it was
        # created.  Legacy tasks without a snapshot remain readable history but
        # cannot be resumed under a silently different algorithm.
        self._policy_for_job(task_id)
        current = PlatformCatalogService(self.db).radar_universe()
        expected_ids = self._snapshot_ids_for_mode(str(job.get("mode") or ""), current)
        if universe_ids != expected_ids:
            raise DeepSeekError(
                "任务固化目录与当前同版本目录的顺序或成员不一致；为避免漏项、错项和旧66项切片，禁止续跑，请新建任务。"
            )
        current_map = {str(row.get("index_code") or ""): row for row in current}
        try:
            return [current_map[value] for value in universe_ids]
        except KeyError as exc:
            raise DeepSeekError(f"任务目录项目{exc.args[0]}已不在当前目录，禁止续跑。") from exc

    def validate_current_job(self, task_id: str) -> dict[str, Any]:
        """Public, read-only compatibility check for services and UI actions."""
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if not job:
            raise DeepSeekError("找不到扫描任务。")
        universe = self._load_job_universe(job)
        return {"job": job, "universe": universe}

    def flash_resume_plan(self, task_id: str) -> dict[str, Any]:
        """Explain a failed Flash resume without mutating state or calling an API.

        A user should know which pending items can be recovered entirely from
        already-paid responses and which items still need a new request before
        granting permission to resume.  This preview deliberately reuses the
        same frozen catalog, batch reconstruction and response recovery logic
        as the real run, but performs no inserts, events or network calls.
        """
        checked = self.validate_current_job(task_id)
        job = checked["job"]
        universe = checked["universe"]
        if str(job.get("mode") or "") not in FLASH_TEST_MODES | {"完整扫描"}:
            raise DeepSeekError("该任务不是可续跑的Flash研究任务。")
        model_config = self._model_config_for_job(task_id)
        setting_name = "api_test_batch_size" if job["mode"] in FLASH_TEST_MODES else "api_full_batch_size"
        default_size = 3 if job["mode"] in FLASH_TEST_MODES else 6
        try:
            batch_size = int(float(model_config.get(setting_name, str(default_size))))
        except (TypeError, ValueError):
            batch_size = default_size
        batch_size = max(1, min(12, batch_size))
        existing_ids = {
            str(row.get("universe_id") or "").strip()
            for row in self._current_evidence(task_id)
        }
        pending = [item for item in universe if str(item.get("index_code") or "") not in existing_ids]
        batches = self._flash_pending_batches(task_id, pending, batch_size)
        plan = {
            "task_id": task_id,
            "total_items": len(universe),
            "already_saved_items": len(existing_ids),
            "pending_items": len(pending),
            "local_repair_items": 0,
            "local_repair_batches": 0,
            "finalize_only_items": 0,
            "finalize_only_batches": 0,
            "new_search_items": 0,
            "new_search_batches": 0,
            "blocked_items": 0,
            "blocked_batches": 0,
            "blocked_reasons": [],
            "batch_phases": [],
            "pro_may_run": str(job.get("mode") or "") == "完整扫描",
            "same_research_day": str(job.get("research_date") or "") == local_now().date().isoformat(),
        }
        for batch in batches:
            batch_ids = [str(item.get("index_code") or "").strip() for item in batch]
            recovery = self._flash_batch_recovery(task_id, batch_ids)
            phase = str((recovery or {}).get("phase") or "new_search")
            plan["batch_phases"].append({"ids": batch_ids, "phase": phase})
            item_count = len(batch_ids)
            if phase == "local_repair":
                plan["local_repair_items"] += item_count
                plan["local_repair_batches"] += 1
            elif phase == "search":
                plan["finalize_only_items"] += item_count
                plan["finalize_only_batches"] += 1
            elif phase in {"transport_uncertain", "finalize"}:
                plan["blocked_items"] += item_count
                plan["blocked_batches"] += 1
                reason = (
                    "请求发送状态不明，禁止自动重发"
                    if phase == "transport_uncertain"
                    else "已做过一次收尾仍未通过，禁止循环调用"
                )
                if reason not in plan["blocked_reasons"]:
                    plan["blocked_reasons"].append(reason)
            else:
                plan["new_search_items"] += item_count
                plan["new_search_batches"] += 1
        return plan

    def create_job(self, mode: str = "完整扫描") -> str:
        if mode not in MARKET_ONLY_MODES | FLASH_TEST_MODES | {"Pro结构测试", "完整扫描", "无API模拟"}:
            raise ValueError("未知扫描模式。")
        stamp = local_now().strftime("%Y%m%d-%H%M%S")
        catalog = PlatformCatalogService(self.db)
        catalog.refresh_admission()
        catalog_universe = catalog.radar_universe()
        catalog_total = len(catalog_universe)
        if mode != "Pro结构测试" and catalog_total == 0:
            raise ValueError(
                "当前CSI代表行业目录为0项。请先在“中证代表目录”重新检查官方代码与精确行情；"
                "软件不会退回旧186项固定池凑数。"
            )
        if mode == "完整扫描":
            execution = catalog.execution_summary()
            if int(execution["ready"]) != int(execution["total"]):
                missing = execution.get("missing") or []
                preview = "、".join(
                    str(row.get("display_code") or row.get("index_name") or "待核验")
                    for row in missing[:6]
                )
                raise ValueError(
                    f"付费扫描前置门槛未通过：今天可执行{execution['ready']}/{execution['total']}项。"
                    "请先到“中证代表目录”点击“刷新今日可申购”；"
                    f"未覆盖示例：{preview or '无'}。软件尚未创建任务，也没有调用DeepSeek。"
                )
        suffix = (
            "marketall" if mode == FULL_MARKET_MODE else "market" if mode in MARKET_TEST_MODES else "test" if mode in FLASH_TEST_MODES else
            {"Pro结构测试": "protest", "完整扫描": "fullcatalog", "无API模拟": "simulation"}[mode]
        )
        task_id = f"{stamp}-{suffix}-{uuid4().hex[:6]}"
        universe_ids = self._snapshot_ids_for_mode(mode, catalog_universe)
        total = 1 if mode == "Pro结构测试" else len(universe_ids)
        if mode in MARKET_ONLY_MODES | {"无API模拟"}:
            cost_limit = 0.0
        elif mode in FLASH_TEST_MODES:
            cost_limit = float(self.db.setting("api_test_cost_limit_cny", "0.30"))
        elif mode == "Pro结构测试":
            cost_limit = float(self.db.setting("api_pro_test_cost_limit_cny", "0.10"))
        else:
            cost_limit = float(self.db.setting("api_full_cost_limit_cny", "2.00"))
        now = local_now().isoformat(timespec="seconds")
        ranking_policy = self._current_ranking_policy()
        ranking_policy_json = json.dumps(
            ranking_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        model_config_json = json.dumps(
            self._current_model_config(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.db.execute(
            """INSERT INTO analysis_jobs(task_id,mode,stage,status,total_items,completed_items,cost_limit_cny,
               estimated_cost_cny,universe_version,research_date,universe_ids_json,universe_hash,
               ranking_policy_version,ranking_policy_json,model_config_json,error,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, mode, "等待", "等待", total, 0, cost_limit, 0.0,
                UNIVERSE_VERSION, local_now().date().isoformat(),
                json.dumps(universe_ids, ensure_ascii=False, separators=(",", ":")),
                self._universe_hash(universe_ids), ranking_policy["version"], ranking_policy_json,
                model_config_json, "", now, now,
            ),
        )
        return task_id

    def run(self, task_id: str, progress: ProgressCallback | None = None) -> JobResult:
        owner = self._acquire_job_lease(task_id)
        try:
            return self._run_with_lease(task_id, progress)
        finally:
            self._release_job_lease(task_id, owner)

    def _run_with_lease(self, task_id: str, progress: ProgressCallback | None = None) -> JobResult:
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if not job:
            raise ValueError("找不到扫描任务。")
        if job["mode"] == "Pro结构测试":
            return self._run_pro_structure_test(task_id, progress)
        universe = self._load_job_universe(job)
        if not universe:
            raise DeepSeekError("CSI代表行业目录为0项；任务已停止且没有调用API。")
        simulated = job["mode"] == "无API模拟"
        try:
            if job["status"] in {"已完成", "测试完成", "行情测试完成", "行情更新完成", "已完成（模拟）"}:
                return JobResult(
                    task_id, job["status"], int(job["completed_items"]), int(job["total_items"]),
                    float(job["estimated_cost_cny"]), "该任务已经完成，不会重复调用API。",
                )
            model_config = self._model_config_for_job(task_id)
            self._check_cancelled()
            saved_evidence_count = len(self._current_evidence(task_id))
            reusable_metrics = [] if simulated else self._same_day_resume_metrics(task_id, universe)
            self._set_job(
                task_id, stage="行情数据", status="运行中",
                completed=saved_evidence_count if reusable_metrics else 0, error="",
            )
            self._event(task_id, "INFO", "行情数据", f"载入CSI叶子行业目标{len(universe)}项；宽泛父级、历史186项与支付宝目录均未参与。")
            if simulated:
                metrics = self._seed_simulated_metrics(task_id, universe)
            elif reusable_metrics:
                metrics = reusable_metrics
                self._event(
                    task_id, "INFO", "行情数据",
                    f"续跑复用同日已校验行情{len(metrics)}/{len(universe)}项；未重新下载公开行情。",
                )
            else:
                def market_progress(stage: str, completed: int, total: int, message: str) -> None:
                    # Public market downloads are also cancellable between
                    # indices.  Persist the processed count before notifying the
                    # UI so a stop request cannot make a partly completed task
                    # appear to have done nothing.  Downloaded bars are already
                    # cached item by item and can be reused by a new zero-API job.
                    self._set_job(task_id, completed=completed)
                    self._notify(progress, stage, completed, total, message)
                    self._check_cancelled()

                metrics = self.market_pipeline.populate_metrics(
                    task_id, universe, require_breadth=job["mode"] == "完整扫描", progress=market_progress
                )
            actual_cutoffs = {
                str(row.get("data_cutoff") or "").strip()[:10]
                for row in metrics if str(row.get("data_cutoff") or "").strip()
            }
            expected_cutoffs = {
                str(row.get("expected_data_cutoff") or "").strip()[:10]
                for row in metrics if str(row.get("expected_data_cutoff") or "").strip()
            }
            publication_pending = (
                len(actual_cutoffs) == 1
                and len(expected_cutoffs) == 1
                and actual_cutoffs != expected_cutoffs
            )
            actual_cutoff = next(iter(actual_cutoffs), "")
            expected_cutoff = next(iter(expected_cutoffs), "")
            metric_message = (
                f"已复用同日行情；保留研究证据{saved_evidence_count}/{len(universe)}项。"
                if reusable_metrics else "五周期硬数据已就绪。"
            )
            if publication_pending:
                metric_message = (
                    f"中证官网仍在分批发布：本轮统一截至{actual_cutoff}，应截至{expected_cutoff}；"
                    "研究可继续，但全部买入金额强制为0。"
                )
                self._event(task_id, "WARNING", "行情发布中", metric_message)
            self._notify(progress, "行情数据", len(metrics), len(universe), metric_message)
            if simulated:
                self._set_job(task_id, stage="严格校验", status="已完成（模拟）", completed=len(universe))
                self._event(task_id, "INFO", "严格校验", "无API模拟通过；不会生成购买建议。")
                return JobResult(task_id, "已完成（模拟）", len(universe), len(universe), 0.0, "模拟管线通过，未调用API、未生成购买建议。")

            if job["mode"] in MARKET_ONLY_MODES:
                cutoff = metrics[0].get("data_cutoff", "") if metrics else ""
                cached = sum(int(row.get("cache_hit", 0) or 0) for row in metrics)
                source_counts: dict[str, int] = {}
                for row in metrics:
                    source = str(row.get("data_source") or "未知")
                    source_counts[source] = source_counts.get(source, 0) + 1
                source_labels = {
                    "csi_official": "中证官网精确",
                    "eastmoney_public": "东方财富精确",
                    "tencent_public": "腾讯精确",
                    "sohu_public": "搜狐精确",
                    "tencent_etf_proxy": "腾讯·审核ETF代理",
                }
                source_text = "、".join(
                    f"{source_labels.get(name, name)} {count}项"
                    for name, count in sorted(source_counts.items())
                )
                full_update = job["mode"] == FULL_MARKET_MODE
                success_status = "行情更新完成" if full_update else "行情测试完成"
                scope_text = f"全目录{len(universe)}项" if full_update else f"{len(universe)}项"
                self._set_job(task_id, stage="行情校验", status=success_status, completed=len(universe))
                market_test_note = (
                    f"{scope_text}结构与日期一致性校验通过；中证官网仍在分批发布，"
                    f"本轮统一截止{cutoff}（应截止{expected_cutoff}）；"
                    "本轮不可生成买入金额，稍后重跑会重新联网刷新；"
                    if publication_pending else f"{scope_text}行情更新通过；统一截止{cutoff}；"
                )
                self._event(
                    task_id, "WARNING" if publication_pending else "INFO", "行情校验",
                    f"{market_test_note}来源{source_text}；缓存回退{cached}项；未调用API。",
                )
                # Mature recommendation observations from the newly cached
                # closes.  This is a local calculation and must never turn a
                # zero-API market refresh into a model request.
                try:
                    from .recommendation_performance import refresh_recommendation_outcomes
                    outcome_counts = refresh_recommendation_outcomes(self.db)
                    if outcome_counts.get("index_mature"):
                        self._event(
                            task_id, "INFO", "推荐验证",
                            f"已用本地行情重算推荐结果：指数成熟{outcome_counts['index_mature']}条；未调用DeepSeek。",
                        )
                except Exception as exc:
                    # Outcome tracking is derived analytics.  A damaged or
                    # incomplete observation must be visible in diagnostics,
                    # but cannot invalidate an otherwise successful market job.
                    self._event(task_id, "WARNING", "推荐验证", f"推荐结果本地重算失败：{exc}")
                return JobResult(
                    task_id, success_status, len(universe), len(universe), 0.0,
                    f"{market_test_note}来源{source_text}；"
                    f"缓存回退{cached}项；未调用DeepSeek。",
                )

            completed_pro = self._model_call_for_request(
                task_id,
                self._stable_request_key("pro-decision", {"logical_call": "once"}),
                legacy_phase="pro_decision_only",
                legacy_stage="Pro终审",
            )
            needs_new_api = (
                len(self._current_evidence(task_id)) < len(universe)
                or (job["mode"] == "完整扫描" and completed_pro is None)
            )
            if needs_new_api:
                if job["mode"] == "完整扫描":
                    execution = PlatformCatalogService(self.db).execution_summary()
                    ready = set(execution.get("ready_ids") or [])
                    expected = {str(row.get("index_code") or "") for row in universe}
                    missing = sorted(expected - ready)
                    if missing:
                        raise DeepSeekError(
                            f"付费请求前再次核验失败：{len(expected)-len(missing)}/{len(expected)}项"
                            "具备今天的基金执行证据。请先免费刷新今日可申购状态；"
                            f"未覆盖：{'、'.join(missing[:8])}。未发送新的API请求。"
                        )
                if not self.client:
                    raise DeepSeekError("尚未建立DeepSeek客户端。")
                self._set_job(task_id, stage="API预检", status="运行中")
                self._notify(progress, "API预检", 0, len(universe), "正在核对官方模型清单与余额；此步骤不产生Token费用。")
                report = self.client.connection_report(
                    model_config["deepseek_flash_model"],
                    model_config["deepseek_pro_model"],
                    require_pro=job["mode"] == "完整扫描",
                )
                self._event(
                    task_id, "INFO", "API预检",
                    f"模型与余额可用；余额{report.balance_text()}；网络通道{report.transport or '系统默认'}。",
                )
            else:
                self._event(
                    task_id, "INFO", "本地恢复",
                    "本任务所需付费响应已存在本地审计记录；跳过联网预检，只执行校验与入库恢复。",
                )
            self._check_cancelled()
            saved_evidence_count = len(self._current_evidence(task_id))
            self._set_job(
                task_id, stage="Flash联网检索", status="运行中", completed=saved_evidence_count,
            )
            evidence = self._run_flash_batches(task_id, universe, metrics, float(job["cost_limit_cny"]), progress)
            expected_evidence_ids = {str(row.get("index_code") or "") for row in universe}
            actual_evidence_ids = {str(row.get("universe_id") or "") for row in evidence}
            if len(evidence) != len(universe) or actual_evidence_ids != expected_evidence_ids:
                raise DeepSeekError(f"Flash证据覆盖不足：{len(evidence)}/{len(universe)}。")
            if job["mode"] in FLASH_TEST_MODES:
                self._set_job(task_id, stage="严格校验", status="测试完成", completed=len(universe))
                self._event(task_id, "INFO", "严格校验", "10方向API测试通过；按设计不生成购买建议。")
                final = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
                return JobResult(task_id, "测试完成", len(universe), len(universe), float(final["estimated_cost_cny"]), "10方向联网测试通过；未进入Pro终审。")

            self._set_job(task_id, stage="Pro终审", status="运行中", completed=len(universe))
            payload, raw_text = self._run_pro(task_id, universe, metrics, evidence)
            expected = [
                {
                    "index_code": u["index_code"], "index_name": u["index_name"],
                    "category": u["category"], "csi_index_code": u.get("csi_index_code", ""),
                    "has_buyable_fund": int(u.get("has_buyable_fund") or 0),
                    "strict_c_funds": list(u.get("strict_c_funds") or []),
                    "strict_c_fund_codes": list(u.get("strict_c_fund_codes") or []),
                }
                for u in universe
            ]
            known = {row["fund_code"] for row in self.db.rows("SELECT fund_code FROM funds")}
            parsed = parse_result(raw_text, known, float(self.db.setting("active_capital_limit", "8000")), expected)
            if not parsed.ok:
                raise DeepSeekError("Pro结果未通过V3校验：" + "；".join(parsed.errors[:12]))
            import_radar_payload(self.db, payload, raw_text, parsed.warnings)
            self._set_job(task_id, stage="完成", status="已完成", completed=len(universe))
            final = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
            self._event(task_id, "INFO", "完成", f"完整扫描已入库，候选{len(payload.get('candidates', []))}个。")
            return JobResult(task_id, "已完成", len(universe), len(universe), float(final["estimated_cost_cny"]), "完整扫描与Pro终审通过。")
        except Exception as exc:
            stopped = isinstance(exc, TaskCancelled)
            status = "已停止" if stopped else "失败"
            message = str(exc)
            current_progress = self.db.row(
                "SELECT completed_items FROM analysis_jobs WHERE task_id=?", (task_id,)
            ) or {}
            saved = (
                int(current_progress.get("completed_items") or 0)
                if job["mode"] in MARKET_ONLY_MODES
                else len(self._current_evidence(task_id))
            )
            uncertain_transport = any(
                marker in message for marker in ("传输状态不明", "禁止自动重发", "可能已经计费")
            )
            if not stopped and saved > 0 and uncertain_transport:
                message += (
                    f" 已安全保存{saved}/{len(universe)}项；因本批计费状态不明，本任务不能自动续跑。"
                )
            elif not stopped and saved > 0 and "续跑选中失败任务" not in message:
                message += (
                    f" 已安全保存{saved}/{len(universe)}项；关闭弹窗后选中本任务，"
                    "点击“续跑选中失败任务”即可从未完成批次继续，不会重做已入库项目。"
                )
            self._set_job(task_id, status=status, completed=saved, error=message)
            self._event(task_id, "WARNING" if stopped else "ERROR", status, message)
            current = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
            return JobResult(task_id, status, int(current["completed_items"]), int(current["total_items"]), float(current["estimated_cost_cny"]), message)

    def _run_pro_structure_test(self, task_id: str, progress: ProgressCallback | None) -> JobResult:
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if job["status"] == "测试完成":
            return JobResult(task_id, "测试完成", 1, 1, float(job["estimated_cost_cny"]), "Pro结构测试已经完成，不会重复调用。")
        try:
            model_config = self._model_config_for_job(task_id)
            system_text = (
                "你是API结构自检器。只返回JSON，不做投资判断。JSON格式示例："
                '{"ok":true,"checks":["chat_completions","thinking","json_output"]}。'
            )
            prompt_text = "请执行最小结构自检并返回json。"
            model = model_config["deepseek_pro_model"]
            max_tokens = 2000
            reasoning_effort = "high"
            input_price = float(model_config["pro_input_price_cny_per_million"])
            cached_price = float(model_config["pro_cached_input_price_cny_per_million"])
            output_price = float(model_config["pro_output_price_cny_per_million"])
            request_key = self._stable_request_key("pro-structure", {"logical_call": "once"})
            fingerprint = self._request_fingerprint({
                "system": system_text,
                "prompt": prompt_text,
                "model": model,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
            })
            result = self._recover_model_result(
                task_id, request_key, request_fingerprint=fingerprint,
                legacy_phase="pro_structure", legacy_stage="Pro结构测试",
            )
            if result is None:
                if not self.client:
                    raise DeepSeekError("尚未建立DeepSeek客户端。")
                self._set_job(task_id, stage="API预检", status="运行中", completed=0, error="")
                self._notify(progress, "API预检", 0, 1, "核对Pro模型和余额；此步骤不产生Token费用。")
                report = self.client.connection_report(
                    model_config["deepseek_flash_model"], model, require_pro=True,
                )
                self._event(
                    task_id, "INFO", "API预检",
                    f"Pro模型与余额可用；余额{report.balance_text()}；网络通道{report.transport or '系统默认'}。",
                )
                projected_cost = self._projected_call_cost(
                    len(system_text) + len(prompt_text), max_tokens, input_price, output_price,
                )
                spent = self._job_cost(task_id)
                if spent + projected_cost > float(job["cost_limit_cny"]):
                    raise DeepSeekError(
                        f"Pro结构测试发送前预算不足：已用¥{spent:.4f}，预留¥{projected_cost:.4f}，"
                        f"上限¥{float(job['cost_limit_cny']):.2f}。未发送请求。"
                    )
                self._check_cancelled()
                reservation_id = self._reserve_model_call(
                    task_id, "Pro结构测试", model, projected_cost,
                    request_key=request_key,
                    context={
                        "phase": "pro_structure",
                        "request_state": "reserved_before_send",
                        "request_fingerprint": fingerprint,
                        "max_tokens": max_tokens,
                    },
                )
                self._set_job(task_id, stage="Pro结构测试", status="运行中")
                self._notify(progress, "Pro结构测试", 0, 1, "正在验证Chat Completions、思考模式、JSON输出、用量和结束原因。")
                try:
                    result = self.client.pro_json(
                        system=system_text,
                        prompt=prompt_text,
                        model=model,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                        input_price_per_million=input_price,
                        cached_input_price_per_million=cached_price,
                        output_price_per_million=output_price,
                    )
                except Exception as exc:
                    self._mark_reserved_model_call_uncertain(reservation_id, exc)
                    self._set_job(task_id, cost=self._job_cost(task_id))
                    raise DeepSeekError(
                        f"Pro结构测试传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
                    ) from exc
                self._finalize_reserved_model_call(reservation_id, result)
                self._set_job(task_id, cost=self._job_cost(task_id))
            else:
                self._set_job(task_id, stage="Pro结构测试本地恢复", status="运行中", completed=0, error="")
                self._event(
                    task_id, "INFO", "Pro结构测试本地恢复",
                    "已从本地审计记录重放先前成功响应，未连接API、未产生第二次费用。",
                )
            if self._job_cost(task_id) > float(job["cost_limit_cny"]):
                raise DeepSeekError("Pro结构测试实际估算费用超过预算阈值；响应已审计但不标记通过。")
            data = result.json()
            checks = data.get("checks")
            if data.get("ok") is not True or not isinstance(checks, list) or not checks:
                raise DeepSeekError("Pro结构测试JSON缺少ok=true或checks数组。")
            self._set_job(task_id, stage="严格校验", status="测试完成", completed=1)
            final = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
            self._event(task_id, "INFO", "严格校验", "Pro最小真实调用通过；未生成或导入投资建议。")
            return JobResult(task_id, "测试完成", 1, 1, float(final["estimated_cost_cny"]), "Pro结构API测试通过；未生成投资建议。")
        except Exception as exc:
            stopped = isinstance(exc, TaskCancelled)
            status = "已停止" if stopped else "失败"
            self._set_job(task_id, status=status, error=str(exc))
            self._event(task_id, "WARNING" if stopped else "ERROR", status, str(exc))
            current = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
            return JobResult(task_id, status, int(current["completed_items"]), 1, float(current["estimated_cost_cny"]), str(exc))

    def _same_day_resume_metrics(self, task_id: str, universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reuse a valid same-task snapshot when doing so is safe.

        Before the first billable call, a publication-lagged snapshot is
        refreshed so a later retry can become current.  Once any API request has
        been reserved or evidence saved, the task snapshot is frozen: replacing
        its bars would mix old paid evidence with new market data.
        """
        rows = self.db.rows("SELECT * FROM sector_metrics WHERE task_id=? ORDER BY universe_id", (task_id,))
        if len(rows) != len(universe):
            return []
        expected_ids = {str(row.get("index_code") or "") for row in universe}
        if {str(row.get("universe_id") or "") for row in rows} != expected_ids:
            return []
        if any(bool(row.get("is_simulated")) for row in rows):
            return []
        expected_cutoff = self.market_pipeline.expected_data_cutoff()
        cutoffs = {str(row.get("data_cutoff") or "").strip()[:10] for row in rows}
        expected_values = {str(row.get("expected_data_cutoff") or "").strip()[:10] for row in rows}
        if len(cutoffs) != 1 or len(expected_values) != 1 or "" in cutoffs or "" in expected_values:
            return []
        frozen_expected = next(iter(expected_values))
        try:
            frozen_previous = (
                self.market_pipeline.calendar
                .previous_trading_day(date.fromisoformat(frozen_expected))
                .isoformat()
            )
        except (TypeError, ValueError):
            return []
        if not cutoffs.issubset({frozen_expected, frozen_previous}):
            return []
        text_fields = ("proxy_code", "proxy_name", "volume_signal", "breadth_signal", "position_20d")
        number_fields = ("d1", "d3", "d5", "d10", "d20")
        for row in rows:
            if any(not str(row.get(field) or "").strip() for field in text_fields):
                return []
            if any(isinstance(row.get(field), bool) or not isinstance(row.get(field), (int, float)) for field in number_fields):
                return []
        current_and_fresh = (
            cutoffs == {expected_cutoff}
            and expected_values == {expected_cutoff}
            and all(str(row.get("freshness_state") or "fresh") in {"fresh", ""} for row in rows)
        )
        if current_and_fresh:
            return rows
        paid_or_evidenced = bool(
            int((self.db.row(
                "SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,),
            ) or {}).get("n") or 0)
            or int((self.db.row(
                "SELECT COUNT(*) n FROM sector_evidence WHERE task_id=?", (task_id,),
            ) or {}).get("n") or 0)
        )
        return rows if paid_or_evidenced else []

    def _current_evidence(self, task_id: str) -> list[dict[str, Any]]:
        """Return only evidence created for this job's immutable research day/catalog."""
        job = self.db.row(
            "SELECT research_date,universe_version FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        ) or {}
        research_date = str(job.get("research_date") or "").strip()
        version = str(job.get("universe_version") or "").strip()
        if not research_date or not version:
            return []
        return self.db.rows(
            """SELECT * FROM sector_evidence
               WHERE task_id=? AND evidence_date=? AND universe_version=?
               ORDER BY universe_id""",
            (task_id, research_date, version),
        )

    def _persist_local_scores(
        self,
        task_id: str,
        metrics: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> tuple[dict[str, float], dict[str, Any]]:
        evidence_map = {str(row.get("universe_id") or ""): row for row in evidence}
        policy = self._policy_for_job(task_id)
        expected_cutoff = self._expected_data_cutoff(metrics)
        cards = build_scorecards(
            metrics,
            evidence_map,
            expected_cutoff=expected_cutoff,
            policy=policy,
        )
        expected_ids = {str(row.get("universe_id") or "") for row in metrics}
        if set(cards) != expected_ids or "" in cards:
            raise DeepSeekError("四维评分没有完整覆盖本轮行情，拒绝进入Pro终审。")
        score_map: dict[str, float] = {}
        updates = []
        for metric in metrics:
            universe_id = str(metric.get("universe_id") or "")
            card = cards[universe_id]
            components = card.as_components()
            components["policy_version"] = policy["version"]
            components["expected_data_cutoff"] = expected_cutoff
            base_score = round(float(card.recommendation_score), 3)
            score_map[universe_id] = base_score
            metric["base_score"] = base_score
            metric["score_components_json"] = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
            updates.append((base_score, metric["score_components_json"], task_id, universe_id))
        self.db.executemany(
            "UPDATE sector_metrics SET base_score=?,score_components_json=? WHERE task_id=? AND universe_id=?",
            updates,
        )
        return score_map, cards

    @staticmethod
    def _local_top_ids(score_map: dict[str, float], count: int = ACTION_CANDIDATE_COUNT) -> list[str]:
        return [
            universe_id for universe_id, _ in sorted(
                score_map.items(), key=lambda row: (-float(row[1]), row[0])
            )[:count]
        ]

    def _stored_score_map(self, task_id: str) -> dict[str, float]:
        policy = self._policy_for_job(task_id)
        rows = self.db.rows(
            "SELECT universe_id,base_score,score_components_json FROM sector_metrics WHERE task_id=?",
            (task_id,),
        )
        score_map: dict[str, float] = {}
        for row in rows:
            raw = str(row.get("score_components_json") or "").strip()
            if raw in {"", "{}"}:
                raise DeepSeekError("本轮缺少可复算的本地评分组件，拒绝让AI直接给分。")
            try:
                components = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DeepSeekError("本轮本地评分组件JSON损坏，拒绝生成候选。") from exc
            if str(components.get("policy_version") or "") != str(policy["version"]):
                raise DeepSeekError("本轮评分组件与冻结策略版本不一致，拒绝生成候选。")
            value = float(row.get("base_score") or 0)
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise DeepSeekError("本轮本地基础分无效，拒绝生成候选。")
            score_map[str(row.get("universe_id") or "")] = value
        return score_map

    def _normalize_ranked_scores(
        self,
        candidates: list[dict[str, Any]],
        score_map: dict[str, float],
        *,
        allowed_ids: set[str] | None = None,
        maximum_count: int = ACTION_CANDIDATE_COUNT,
    ) -> list[dict[str, Any]]:
        allowed = set(score_map) if allowed_ids is None else set(allowed_ids)
        if len(candidates) > maximum_count:
            raise DeepSeekError(f"Pro终审最多选择{maximum_count}项，结果未入库。")
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in candidates:
            if not isinstance(raw, dict):
                raise DeepSeekError("AI候选不是JSON对象。")
            item = dict(raw)
            universe_id = str(item.get("universe_id") or "").strip()
            if universe_id not in allowed or universe_id not in score_map:
                raise DeepSeekError(f"Pro终审候选{universe_id}不在本轮20—30项候选池内，结果未入库。")
            if universe_id in seen_ids:
                raise DeepSeekError(f"Pro终审候选重复：{universe_id}，结果未入库。")
            seen_ids.add(universe_id)
            final_score = round(float(score_map[universe_id]), 3)
            item["base_score"] = score_map[universe_id]
            # Pro selects and explains; it no longer changes the deterministic
            # numeric score.  This removes the former ±5 subjective layer.
            item["score_adjustment"] = 0.0
            item["score"] = final_score
            normalized.append(item)
        normalized.sort(key=lambda row: (-float(row["score"]), str(row["universe_id"])))
        for rank, item in enumerate(normalized, start=1):
            item["rank"] = rank
        return normalized

    def _strict_executable_funds(self, universe_ids: list[str]) -> list[dict[str, Any]]:
        """Fresh, exact, passive C-class mappings for this frozen catalog only."""
        clean_ids = [str(value).strip() for value in universe_ids if str(value).strip()]
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        rows = self.db.rows(
            f"""SELECT i.universe_id,i.official_index_code AS index_code,
                       i.allocation_cluster,f.*
                  FROM investable_funds f
                  JOIN investable_indices i ON i.id=f.investable_index_id
                 WHERE i.active=1 AND i.radar_enabled=1 AND f.active=1
                   AND f.radar_eligible=1 AND f.relation_kind='同一指数'
                   AND UPPER(COALESCE(f.share_class,''))='C'
                   AND f.strategy_kind IN ('被动ETF联接','被动指数')
                   AND COALESCE(f.passive_verified,0)=1
                   AND f.public_buyable_status IN
                       ('公开渠道可申购','已核对可申购','用户已实际买入','公开资料显示可申购')
                   AND i.universe_id IN ({placeholders})
                 ORDER BY i.universe_id,f.quality_rank,f.fund_code""",
            clean_ids,
        )
        catalog = PlatformCatalogService(self.db)
        return [
            row for row in rows
            if catalog.evaluate_fund_execution(
                row, intended_amount=1, require_same_day=True,
            ).executable
        ]

    def _run_flash_batches(
        self,
        task_id: str,
        universe: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        cost_limit: float,
        progress: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        metric_map = {row["universe_id"]: row for row in metrics}
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,)) or {}
        model_config = self._model_config_for_job(task_id)
        test_mode = job.get("mode") in FLASH_TEST_MODES
        research_date = str(job.get("research_date") or "").strip()
        universe_version = str(job.get("universe_version") or "").strip()
        if research_date != local_now().date().isoformat() or universe_version != UNIVERSE_VERSION:
            raise DeepSeekError("任务研究日或目录版本已失效；禁止复用证据或发送新的API请求，请新建任务。")
        setting_name = "api_test_batch_size" if test_mode else "api_full_batch_size"
        default_size = 3 if test_mode else 6
        try:
            batch_size = int(float(model_config.get(setting_name, str(default_size))))
        except (TypeError, ValueError):
            batch_size = default_size
        batch_size = max(1, min(12, batch_size))
        total_batches = math.ceil(len(universe) / batch_size)
        max_output_setting = "api_test_max_output_tokens" if test_mode else "api_full_flash_max_output_tokens"
        max_output_default = 6000 if test_mode else 12000
        try:
            max_output_tokens = int(float(model_config.get(max_output_setting, str(max_output_default))))
        except (TypeError, ValueError):
            max_output_tokens = max_output_default
        max_output_tokens = max(2000, min(32000, max_output_tokens))
        effort_setting = "api_test_flash_reasoning_effort" if test_mode else "api_full_flash_reasoning_effort"
        reasoning_effort = str(
            model_config.get(effort_setting, "none" if test_mode else "low")
        ).strip().lower()
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            reasoning_effort = "none" if test_mode else "low"
        existing_rows = self._current_evidence(task_id)
        existing_ids = {row["universe_id"] for row in existing_rows}
        pending = [item for item in universe if item["index_code"] not in existing_ids]
        completed = len(existing_ids)
        if completed:
            self._event(task_id, "INFO", "Flash联网检索", f"续跑复用已通过证据{completed}项；只处理剩余{len(pending)}项。")
            self._set_job(task_id, completed=completed)
            self._notify(progress, "Flash联网检索", completed, len(universe), f"已复用{completed}项，不重复扣费。")
        pending_batches = self._flash_pending_batches(task_id, pending, batch_size)
        total_batches = len(pending_batches)
        for batch_index, batch in enumerate(pending_batches, start=1):
            self._check_cancelled()
            compact = []
            for item in batch:
                metric = metric_map[item["index_code"]]
                compact.append({
                    "id": item["index_code"], "name": item["index_name"], "category": item["category"],
                    "proxy_code": metric["proxy_code"], "proxy_name": metric["proxy_name"],
                    "returns_pct": {k: metric[k] for k in ("d1", "d3", "d5", "d10", "d20")},
                    "volume_signal": metric["volume_signal"], "breadth_signal": metric["breadth_signal"],
                    "breadth_kind": metric.get("breadth_kind", ""),
                    "breadth_pct": metric.get("breadth_pct"),
                    "position_20d": metric["position_20d"], "data_quality": metric.get("data_quality", ""),
                })
            schema = {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array", "minItems": len(batch), "maxItems": len(batch),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 32},
                                "crowding": {"type": "string", "minLength": 1, "maxLength": 400},
                                "crowding_level": {"type": "number", "minimum": 0, "maximum": 100},
                                "catalyst": {"type": "string", "minLength": 1, "maxLength": 400},
                                "catalyst_strength": {"type": "number", "minimum": 0, "maximum": 100},
                                "risk": {"type": "string", "minLength": 1, "maxLength": 400},
                                "sources": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "object", "properties": {
                                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                                    "url": {"type": "string", "minLength": 8, "maxLength": 2000},
                                    "retrieved_at": {"type": "string", "enum": [research_date]},
                                    "supports": {"type": "string", "minLength": 1, "maxLength": 800},
                                }, "required": ["id", "title", "url", "retrieved_at", "supports"], "additionalProperties": False}},
                            },
                            "required": [
                                "id", "crowding", "crowding_level", "catalyst",
                                "catalyst_strength", "risk", "sources",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"], "additionalProperties": False,
            }
            instructions = (
                f"你是A股板块证据检索员。当前研究日和本次联网检索日均为{research_date}；"
                "不得根据收益率、指数点位或文章日期反推成其他研究日期。"
                "硬行情已经由本地程序计算；只联网核查催化、拥挤和风险，不得改写硬数据。"
                "只有breadth_kind明确为constituent_snapshot且breadth_pct为0—100数值时才存在真实成分股扩散度；"
                "其余情况均为扩散度缺失，不得根据returns_pct或任何指数涨跌推断多数成分股上涨。"
                "每个方向必须先使用web_search核查相对于当前研究日的近期公开资料；"
                "优先使用最近30天资料，较早资料只能作为背景，不能单独证明当前催化或拥挤。"
                f"每个来源的retrieved_at必须精确填写{research_date}，它表示本次检索日期，绝不是文章发布日期。"
                "获得足够证据后立即停止搜索并生成最终JSON。"
                "不要反复搜索同一问题；每个方向最多进行2次有针对性的搜索。"
                "crowding_level与catalyst_strength必须分别给出0—100的数值，不得写中文等级："
                "前者0表示几乎不拥挤、50表示中等、100表示极端拥挤；"
                "后者0表示没有近期可核验催化、50表示中等、100表示极强且有直接来源支持。"
                "必须只返回符合所给JSON Schema的JSON对象，不得输出Markdown或解释文字。"
                'JSON外形示例：{"items":[{"id":"指数ID","crowding":"简短判断","crowding_level":72,'
                '"catalyst":"简短催化","catalyst_strength":64,'
                '"risk":"简短风险","sources":[{"id":"S1","title":"来源标题","url":"https://...",'
                '"retrieved_at":"YYYY-MM-DD","supports":"该来源支持什么"}]}]}。'
                "字段名必须精确写crowding，禁止拼成crowing；不得回显输入中的returns_pct等硬行情字段来替代证据字段。"
            )
            prompt = (
                f"研究日={research_date}。逐项联网检索并返回JSON证据；不得把研究日猜成历史行情发生日；"
                "每个ID恰好一次，每项保留1—2个最直接的http(s)来源，文字务必简洁。\n"
                + json.dumps(compact, ensure_ascii=False)
            )
            batch_ids = [item["index_code"] for item in batch]
            expected_ids = set(batch_ids)
            recovery = self._flash_batch_recovery(task_id, batch_ids)
            if recovery and recovery["phase"] == "transport_uncertain":
                raise DeepSeekError(
                    "本批Flash请求的传输状态不明，服务端可能已经计费；软件禁止自动重发。"
                    "请先导出诊断并人工核对模型账单，再新建任务。"
                )
            if recovery and recovery["phase"] == "local_repair":
                prepared_rows = self._prepare_flash_data(
                    task_id, recovery["data"], expected_ids, int(recovery["web_search_calls"]),
                )
                self._event(
                    task_id, "INFO", "Flash本地JSON修复",
                    f"第{batch_index}/{total_batches}批复用已付费响应，在本机修复JSON或补入明确的保守待核验标记并通过严格校验；"
                    "复用原联网证据，未调用API。",
                )
                self.db.executemany(
                    """INSERT INTO sector_evidence(task_id,universe_id,crowding,crowding_level,catalyst,
                       catalyst_strength,risk,source_json,web_search_calls,evidence_date,universe_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(task_id,universe_id) DO UPDATE SET
                       crowding=excluded.crowding,crowding_level=excluded.crowding_level,
                       catalyst=excluded.catalyst,catalyst_strength=excluded.catalyst_strength,risk=excluded.risk,
                       source_json=excluded.source_json,web_search_calls=excluded.web_search_calls,
                       evidence_date=excluded.evidence_date,universe_version=excluded.universe_version""",
                    prepared_rows,
                )
                completed += len(batch)
                current_cost = self._job_cost(task_id)
                self._set_job(task_id, completed=completed)
                self._notify(
                    progress, "Flash本地JSON修复", completed, len(universe),
                    f"本批未调用API；累计估算费用¥{current_cost:.4f}",
                )
                if current_cost > cost_limit and completed < len(universe):
                    raise DeepSeekError(
                        f"本地修复已保存，但累计估算费用¥{current_cost:.4f}超过预算阈值¥{cost_limit:.2f}；"
                        "不会发送下一批。"
                    )
                self._check_cancelled()
                continue
            if recovery and recovery["phase"] == "finalize":
                raise DeepSeekError(
                    "这一批已经执行过一次无搜索收尾但仍未通过严格校验；软件不会再次循环扣费。"
                    "请新建10方向测试，或导出该任务诊断后复核。"
                )
            input_price = float(model_config["flash_input_price_cny_per_million"])
            cached_price = float(model_config["flash_cached_input_price_cny_per_million"])
            output_price = float(model_config["flash_output_price_cny_per_million"])
            flash_model = model_config["deepseek_flash_model"]
            if recovery:
                wait_message = (
                    f"第{batch_index}/{total_batches}批，复用已完成的{recovery['web_search_calls']}次搜索，"
                    "只做JSON收尾（不会再次联网搜索）"
                )
                stage = "Flash无搜索收尾"
                self._event(task_id, "INFO", stage, wait_message)
                self._notify(progress, stage, completed, len(universe), wait_message)
                audit_search_calls = int(recovery["web_search_calls"])
                call_context = {
                    "phase": "finalize", "batch_ids": batch_ids,
                    "parent_response_id": str(recovery["raw"].get("id") or ""),
                    "research_date": research_date, "universe_version": universe_version,
                }
                call_context["request_fingerprint"] = self._request_fingerprint({
                    "instructions": instructions,
                    "prompt": prompt,
                    "schema": schema,
                    "model": flash_model,
                    "max_output_tokens": max_output_tokens,
                    "previous_response_id": call_context["parent_response_id"],
                })
                request_key = self._stable_request_key("flash-finalize", {
                    "batch_ids": batch_ids,
                    "research_date": research_date,
                    "universe_version": universe_version,
                    "parent_response_id": call_context["parent_response_id"],
                })
                projected_cost = self._projected_call_cost(
                    len(instructions) + len(prompt) + len(json.dumps(recovery["raw"], ensure_ascii=False)),
                    max_output_tokens, input_price, output_price,
                )
                spent = self._job_cost(task_id)
                if spent + projected_cost > cost_limit:
                    raise DeepSeekError(
                        f"无搜索收尾发送前预算不足：已用¥{spent:.4f}，预留¥{projected_cost:.4f}，"
                        f"上限¥{cost_limit:.2f}。未发送请求。"
                    )
                reservation_id = self._reserve_model_call(
                    task_id, stage, flash_model, projected_cost,
                    request_key=request_key, context=call_context,
                )
                try:
                    result = self.client.flash_finalize_json(
                        instructions=instructions,
                        prompt=prompt,
                        previous_raw=recovery["raw"],
                        schema_name="sector_evidence_batch",
                        schema=schema,
                        model=flash_model,
                        max_output_tokens=max_output_tokens,
                        input_price_per_million=input_price,
                        cached_input_price_per_million=cached_price,
                        output_price_per_million=output_price,
                    )
                except Exception as exc:
                    self._mark_reserved_model_call_uncertain(reservation_id, exc)
                    self._set_job(task_id, cost=self._job_cost(task_id))
                    raise DeepSeekError(
                        f"Flash无搜索收尾传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
                    ) from exc
                self._finalize_reserved_model_call(reservation_id, result)
            else:
                wait_message = (
                    f"第{batch_index}/{total_batches}批，正在等待DeepSeek联网检索"
                    f"（本批{len(batch)}项；通常需1—3分钟，请勿重复点击）"
                )
                stage = "Flash联网检索"
                self._event(task_id, "INFO", stage, wait_message)
                self._notify(progress, stage, completed, len(universe), wait_message)
                call_context = {
                    "phase": "search", "batch_ids": batch_ids,
                    "research_date": research_date, "universe_version": universe_version,
                }
                call_context["request_fingerprint"] = self._request_fingerprint({
                    "instructions": instructions,
                    "prompt": prompt,
                    "schema": schema,
                    "model": flash_model,
                    "max_output_tokens": max_output_tokens,
                    "reasoning_effort": reasoning_effort,
                })
                request_key = self._stable_request_key("flash-search", {
                    "batch_ids": batch_ids,
                    "research_date": research_date,
                    "universe_version": universe_version,
                })
                projected_cost = self._projected_call_cost(
                    len(instructions) + len(prompt), max_output_tokens, input_price, output_price,
                )
                spent = self._job_cost(task_id)
                if spent + projected_cost > cost_limit:
                    raise DeepSeekError(
                        f"Flash发送前预算不足：已用¥{spent:.4f}，预留¥{projected_cost:.4f}，"
                        f"上限¥{cost_limit:.2f}。未发送请求。"
                    )
                reservation_id = self._reserve_model_call(
                    task_id, stage, flash_model, projected_cost,
                    request_key=request_key, context=call_context,
                )
                try:
                    result = self.client.flash_search_json(
                        instructions=instructions,
                        prompt=prompt,
                        schema_name="sector_evidence_batch",
                        schema=schema,
                        model=flash_model,
                        max_output_tokens=max_output_tokens,
                        reasoning_effort=reasoning_effort,
                        input_price_per_million=input_price,
                        cached_input_price_per_million=cached_price,
                        output_price_per_million=output_price,
                    )
                except Exception as exc:
                    self._mark_reserved_model_call_uncertain(reservation_id, exc)
                    self._set_job(task_id, cost=self._job_cost(task_id))
                    raise DeepSeekError(
                        f"Flash联网检索传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
                    ) from exc
                self._finalize_reserved_model_call(reservation_id, result)
                audit_search_calls = result.web_search_calls
            self._set_job(task_id, cost=self._job_cost(task_id))
            current_cost = self._job_cost(task_id)
            if recovery and result.web_search_calls:
                raise DeepSeekError(
                    "无搜索收尾意外返回新的web_search_call；本次响应与费用已审计，结果拒绝入库。"
                )
            if audit_search_calls < 1:
                raise DeepSeekError(
                    "Flash响应没有web_search_call审计记录，已拒绝作为联网研究结果；"
                    + result.diagnostic_summary()
                )
            try:
                prepared_rows = self._prepare_flash_rows(task_id, result, expected_ids, audit_search_calls)
            except DeepSeekError as initial_error:
                _, completed_searches, failed_searches = self._web_search_status_counts(result.raw)
                if failed_searches and completed_searches < 1:
                    raise DeepSeekError(
                        f"{initial_error} 本批没有任何成功的联网搜索，软件不会付费调用无搜索收尾，"
                        "也不会自动重发本批。"
                    ) from initial_error
                auto_finalize = str(model_config.get("api_auto_finalize_flash", "1")).strip().lower() not in {
                    "0", "false", "no", "off",
                }
                if not recovery and not auto_finalize and result.web_search_calls >= 1:
                    raise DeepSeekError(
                        f"{initial_error} 本批搜索结果和费用已保存；"
                        "请选中本任务点击“续跑选中失败任务”，软件只会尝试一次无搜索JSON收尾。"
                    ) from initial_error
                if recovery or result.web_search_calls < 1:
                    raise
                spent = self._job_cost(task_id)
                projected_finalize = self._projected_call_cost(
                    len(instructions) + len(prompt) + len(json.dumps(result.raw, ensure_ascii=False)),
                    max_output_tokens, input_price, output_price,
                )
                if spent >= cost_limit or spent + projected_finalize > cost_limit:
                    raise DeepSeekError(
                        f"{initial_error} 自动无搜索收尾预计会使累计费用达到约"
                        f"¥{spent + projected_finalize:.4f}，超过预算阈值¥{cost_limit:.2f}；"
                        "软件未发送收尾请求。可在核对诊断后调整预算并手工续跑。"
                    ) from initial_error
                self._check_cancelled()
                finalize_stage = "Flash自动无搜索收尾"
                wait_message = (
                    f"第{batch_index}/{total_batches}批搜索已完成但结果未通过严格JSON校验；"
                    "自动复用原搜索结果，只尝试一次禁用搜索的JSON收尾"
                )
                self._event(task_id, "INFO", finalize_stage, wait_message)
                self._notify(progress, finalize_stage, completed, len(universe), wait_message)
                finalize_context = {
                    "phase": "finalize",
                    "batch_ids": batch_ids,
                    "parent_response_id": str(result.raw.get("id") or ""),
                    "automatic": True,
                    "research_date": research_date, "universe_version": universe_version,
                }
                finalize_context["request_fingerprint"] = self._request_fingerprint({
                    "instructions": instructions,
                    "prompt": prompt,
                    "schema": schema,
                    "model": flash_model,
                    "max_output_tokens": max_output_tokens,
                    "previous_response_id": finalize_context["parent_response_id"],
                })
                finalize_request_key = self._stable_request_key("flash-finalize", {
                    "batch_ids": batch_ids,
                    "research_date": research_date,
                    "universe_version": universe_version,
                    "parent_response_id": finalize_context["parent_response_id"],
                })
                finalize_reservation_id = self._reserve_model_call(
                    task_id, finalize_stage, flash_model, projected_finalize,
                    request_key=finalize_request_key, context=finalize_context,
                )
                try:
                    finalized = self.client.flash_finalize_json(
                        instructions=instructions,
                        prompt=prompt,
                        previous_raw=result.raw,
                        schema_name="sector_evidence_batch",
                        schema=schema,
                        model=flash_model,
                        max_output_tokens=max_output_tokens,
                        input_price_per_million=input_price,
                        cached_input_price_per_million=cached_price,
                        output_price_per_million=output_price,
                    )
                except Exception as exc:
                    self._mark_reserved_model_call_uncertain(finalize_reservation_id, exc)
                    self._set_job(task_id, cost=self._job_cost(task_id))
                    raise DeepSeekError(
                        f"Flash自动收尾传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
                    ) from exc
                self._finalize_reserved_model_call(finalize_reservation_id, finalized)
                self._set_job(task_id, cost=self._job_cost(task_id))
                current_cost = self._job_cost(task_id)
                if finalized.web_search_calls:
                    raise DeepSeekError(
                        "自动无搜索收尾意外返回新的web_search_call；本次响应与费用已审计，结果拒绝入库。"
                    )
                try:
                    prepared_rows = self._prepare_flash_rows(
                        task_id, finalized, expected_ids, audit_search_calls
                    )
                except DeepSeekError as finalize_error:
                    raise DeepSeekError(
                        "自动无搜索JSON收尾已经执行一次但仍未通过严格校验；"
                        "软件不会再次循环调用。请导出任务诊断后复核。"
                        f"原始校验错误：{initial_error}；收尾校验错误：{finalize_error}"
                    ) from finalize_error
            self.db.executemany(
                """INSERT INTO sector_evidence(task_id,universe_id,crowding,crowding_level,catalyst,
                   catalyst_strength,risk,source_json,web_search_calls,evidence_date,universe_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id,universe_id) DO UPDATE SET
                   crowding=excluded.crowding,crowding_level=excluded.crowding_level,
                   catalyst=excluded.catalyst,catalyst_strength=excluded.catalyst_strength,risk=excluded.risk,
                   source_json=excluded.source_json,web_search_calls=excluded.web_search_calls,
                   evidence_date=excluded.evidence_date,universe_version=excluded.universe_version""",
                prepared_rows,
            )
            completed += len(batch)
            self._set_job(task_id, completed=completed)
            self._notify(progress, "Flash联网检索", completed, len(universe), f"估算费用¥{current_cost:.4f}")
            if current_cost > cost_limit and completed < len(universe):
                raise DeepSeekError(
                    f"当前在途批次返回后累计估算费用¥{current_cost:.4f}超过预算阈值¥{cost_limit:.2f}；"
                    "本批有效证据已保留，但不会发送下一批。"
                )
            if current_cost > cost_limit:
                self._event(task_id, "WARNING", "费用审计", f"最终在途批次使估算费用达到¥{current_cost:.4f}，超过预算阈值¥{cost_limit:.2f}。")
            self._check_cancelled()
        return self._current_evidence(task_id)

    def _prepare_flash_rows(
        self,
        task_id: str,
        result: ModelResult,
        expected_ids: set[str],
        audit_search_calls: int,
    ) -> list[tuple[Any, ...]]:
        """Strictly validate one Flash batch before any evidence is written."""
        # Completion/tool-call integrity is never repairable.  Check it before
        # looking at the text so a locally parseable fragment cannot conceal an
        # incomplete or failed upstream response.
        result.require_complete()
        data = self._parse_flash_json_text(result.text)
        if data is None:
            # Preserve ModelResult's detailed diagnostic for callers and the UI.
            result.json()
            raise DeepSeekError("Flash正文无法安全提取唯一JSON对象。")
        cleaned = ModelResult._clean_json_text(result.text)
        try:
            direct = json.loads(cleaned)
        except json.JSONDecodeError:
            direct = None
        direct_count = len(direct.get("items", [])) if isinstance(direct, dict) and isinstance(direct.get("items"), list) else -1
        parsed_count = len(data.get("items", [])) if isinstance(data.get("items"), list) else -1
        if not isinstance(direct, dict) or direct_count != parsed_count:
            self._event(
                task_id, "INFO", "Flash本地JSON修复",
                "已从模型正文中安全提取唯一完整JSON对象，并仅清理可证明无信息的同ID重复影子项；"
                "随后仍执行项目数、ID、字段、来源与研究日的全部严格校验，未调用API。",
            )
        prepared_rows = self._prepare_flash_data(task_id, data, expected_ids, audit_search_calls)
        _, completed_searches, failed_searches = self._web_search_status_counts(result.raw)
        if failed_searches:
            self._event(
                task_id, "WARNING", "Flash部分搜索降级",
                f"本批有{failed_searches}次联网搜索失败，但另有{completed_searches}次搜索成功，"
                "且最后一段JSON正文已通过项目数、ID、字段、来源网址和研究日的全部严格校验；"
                "已采用有效结果，没有再次调用API。",
            )
        return prepared_rows

    def _prepare_flash_data(
        self,
        task_id: str,
        data: dict[str, Any],
        expected_ids: set[str],
        audit_search_calls: int,
    ) -> list[tuple[Any, ...]]:
        job = self.db.row(
            "SELECT research_date,universe_version FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        ) or {}
        research_date = str(job.get("research_date") or "").strip()
        universe_version = str(job.get("universe_version") or "").strip()
        if research_date != local_now().date().isoformat() or universe_version != UNIVERSE_VERSION:
            raise DeepSeekError("Flash证据所属研究日或目录版本无效，拒绝入库。")
        data = self._trim_excess_flash_sources(task_id, data, expected_ids)
        data = self._fill_missing_flash_text_fields(task_id, data, expected_ids)
        items = self._validate_flash_items(data, expected_ids)
        prepared_rows: list[tuple[Any, ...]] = []
        for item in items:
            normalized_sources = self._normalize_sources(
                item["id"], item["sources"], expected_date=research_date
            )
            prepared_rows.append((
                task_id, item["id"], item["crowding"], item["crowding_level"],
                item["catalyst"], item["catalyst_strength"], item["risk"],
                json.dumps(normalized_sources, ensure_ascii=False), audit_search_calls,
                research_date, universe_version,
            ))
        return prepared_rows

    def _trim_excess_flash_sources(
        self, task_id: str, data: dict[str, Any], expected_ids: set[str],
    ) -> dict[str, Any]:
        """Keep the first two already-paid direct sources when a model returns more.

        The Flash contract asks for one or two direct sources.  Returning a
        third valid source does not make the research unusable, and paying for
        another model call merely to delete it is wasteful.  This normalization
        is intentionally narrow: it never creates or edits a source, never
        changes item membership, and all retained sources still pass the strict
        URL/date/content checks below.
        """
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return data
        trimmed: list[str] = []
        normalized_items: list[Any] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                normalized_items.append(raw_item)
                continue
            item = dict(raw_item)
            item_id = str(item.get("id") or "").strip()
            sources = item.get("sources")
            if item_id in expected_ids and isinstance(sources, list) and len(sources) > 2:
                item["sources"] = sources[:2]
                trimmed.append(f"{item_id}:{len(sources)}→2")
            normalized_items.append(item)
        if trimmed:
            self._event(
                task_id,
                "WARNING",
                "Flash本地来源精简",
                "模型为部分项目返回了超过2条直接来源；软件已按模型原顺序保留前2条，"
                "随后仍执行项目ID、网址、研究日及字段完整性校验，未再次调用API。"
                f" 项目：{', '.join(trimmed)}。",
            )
        return {**data, "items": normalized_items}

    def _fill_missing_flash_text_fields(
        self, task_id: str, data: dict[str, Any], expected_ids: set[str],
    ) -> dict[str, Any]:
        """Fill omitted legacy fields locally with explicit conservative values.

        New requests require both structured levels in their JSON schema.  This
        fallback exists only so an already-paid response produced by an older
        schema can be recovered without another API call.  It never upgrades an
        uncertain item into a positive research conclusion.  Present-but-invalid
        values are left untouched and rejected by strict validation below.
        """
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return data
        replacements = {
            "crowding": "公开资料或模型输出未明确给出拥挤度；标记为待核验，按较高拥挤风险保守处理。",
            "catalyst": "未提供可核验的近期催化；标记为无已证实催化，不据此买入。",
            "risk": "未单列风险；标记为资料不完整，终审应按高不确定性处理。",
        }
        repaired = {**data, "items": []}
        filled: list[str] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                repaired["items"].append(raw_item)
                continue
            item = dict(raw_item)
            item_id = str(item.get("id") or "").strip()
            if item_id in expected_ids:
                # DeepSeek occasionally drops one "d" and returns `crowing`.
                # This is an unambiguous key correction; the model's original
                # value is preserved byte-for-byte and no conclusion is added.
                if not str(item.get("crowding") or "").strip() and str(item.get("crowing") or "").strip():
                    item["crowding"] = item["crowing"]
                    filled.append(f"{item_id}.crowing→crowding")
                for field, marker in replacements.items():
                    if not str(item.get(field) or "").strip():
                        item[field] = marker
                        filled.append(f"{item_id}.{field}")
                for field, narrative in (
                    ("crowding_level", str(item.get("crowding") or "")),
                    ("catalyst_strength", str(item.get("catalyst") or "")),
                ):
                    raw_value = item.get(field)
                    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                        item[field] = self._conservative_flash_level(field, narrative)
                        filled.append(f"{item_id}.{field}={item[field]:.0f}")
            repaired["items"].append(item)
        if filled:
            self._event(
                task_id, "WARNING", "Flash本地字段降级",
                "模型漏写字段，已在本机填入保守待核验文字/数值（不伪造正向结论、不调用API）："
                + "、".join(filled),
            )
        return repaired

    @staticmethod
    def _conservative_flash_level(field: str, narrative: str) -> float:
        """Accept only an explicit legacy grade; never keyword-guess prose."""
        text = str(narrative or "").strip().lower()
        compact = re.sub(r"[\s。；;，,！!]+", "", text)
        if field == "crowding_level":
            aliases = {
                "极高": 95.0, "极端": 95.0, "高": 80.0, "中高": 70.0,
                "中": 50.0, "中等": 50.0, "中低": 30.0, "低": 15.0,
            }
            exact = compact
            match = re.fullmatch(r"(?:拥挤度|拥挤等级|crowding(?:_level)?)[：:=]?(极高|极端|高|中高|中等|中|中低|低)", compact)
            default = 70.0
        else:
            aliases = {
                "极强": 95.0, "强": 80.0, "中": 55.0, "中等": 55.0,
                "弱": 30.0, "无": 10.0, "暂无": 10.0,
            }
            exact = compact
            match = re.fullmatch(r"(?:催化强度|催化等级|catalyst_strength)[：:=]?(极强|强|中等|中|弱|无|暂无)", compact)
            default = 20.0
        grade = match.group(1) if match else exact
        return aliases.get(grade, default)

    @staticmethod
    def _repair_unescaped_json_quotes(text: str) -> dict[str, Any] | None:
        """Repair only bare quotes inside JSON strings, then parse normally.

        This deliberately does not add missing braces, remove fields, or guess
        values.  Schema and domain validation still run after parsing.
        """
        value = ModelResult._clean_json_text(text)
        if not value:
            return None
        repaired: list[str] = []
        in_string = False
        escaped = False
        changed = False
        length = len(value)
        for index, char in enumerate(value):
            if not in_string:
                repaired.append(char)
                if char == '"':
                    in_string = True
                continue
            if escaped:
                repaired.append(char)
                escaped = False
                continue
            if char == "\\":
                repaired.append(char)
                escaped = True
                continue
            if char != '"':
                repaired.append(char)
                continue
            next_index = index + 1
            while next_index < length and value[next_index].isspace():
                next_index += 1
            next_char = value[next_index] if next_index < length else ""
            if next_char in {":", ",", "}", "]", ""}:
                repaired.append(char)
                in_string = False
            else:
                repaired.append('\\"')
                changed = True
        if not changed or in_string:
            return None
        try:
            parsed = json.loads("".join(repaired))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _responses_text(raw: dict[str, Any]) -> str:
        messages: list[str] = []
        output = raw.get("output")
        for item in output if isinstance(output, list) else []:
            if (
                not isinstance(item, dict)
                or item.get("type") != "message"
                or str(item.get("status") or "") not in {"", "completed"}
            ):
                continue
            parts: list[str] = []
            for part in item.get("content", []) or []:
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                    continue
                part_text = part.get("text", "")
                if isinstance(part_text, dict):
                    part_text = part_text.get("value", "")
                parts.append(str(part_text or ""))
            message = "".join(parts).strip()
            if message:
                messages.append(message)
        if messages:
            return messages[-1]
        return str(raw.get("output_text") or "").strip()

    @staticmethod
    def _web_search_status_counts(raw: dict[str, Any]) -> tuple[int, int, int]:
        """Return total, usable-completed and explicitly failed search calls."""
        total = completed = failed = unmarked = 0
        output = raw.get("output")
        for item in output if isinstance(output, list) else []:
            if not isinstance(item, dict) or item.get("type") != "web_search_call":
                continue
            total += 1
            status = str(item.get("status") or "")
            if status == "completed":
                completed += 1
            elif status:
                failed += 1
            else:
                unmarked += 1
        # Older saved responses did not expose per-call status.  They remain
        # usable only when none of their calls explicitly failed.
        if not failed:
            completed += unmarked
        return total, completed, failed

    @staticmethod
    def _flash_data_has_exact_ids(data: dict[str, Any] | None, expected_ids: set[str]) -> bool:
        if not isinstance(data, dict):
            return False
        items = data.get("items")
        if not isinstance(items, list) or len(items) != len(expected_ids):
            return False
        ids = [
            str(item.get("id") or "").strip()
            for item in items if isinstance(item, dict)
        ]
        return len(ids) == len(expected_ids) and len(set(ids)) == len(ids) and set(ids) == expected_ids

    @classmethod
    def _parse_flash_json_text(cls, text: str) -> dict[str, Any] | None:
        """Parse one unambiguous Flash JSON object and remove only empty shadows.

        Flash occasionally prefixes an otherwise valid schema response with a
        short sentence (or a single `````json`` fence).  Accepting arbitrary
        fragments would be unsafe, so an embedded object is accepted only when
        it is the sole object, has no trailing prose, and the prefix contains no
        braces or additional code fences.  Domain/schema validation still runs
        after this method.
        """
        cleaned = ModelResult._clean_json_text(text)
        parsed: dict[str, Any] | None = None
        if cleaned:
            try:
                candidate = json.loads(cleaned)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate
            else:
                repaired = cls._repair_unescaped_json_quotes(cleaned)
                if isinstance(repaired, dict):
                    parsed = repaired
        if parsed is None:
            value = str(text or "").strip().lstrip("\ufeff")
            # A paid Flash response may contain a short explanatory sentence
            # followed by one fenced JSON object.  `_clean_json_text` cannot
            # remove that prefix, while `raw_decode` cannot repair an
            # unescaped quote inside the object.  Isolate this narrowly-defined
            # shape first, then reuse the deterministic quote repair.  The
            # full schema, IDs, dates and sources are still validated later.
            fenced_match = re.fullmatch(
                r"(?s)(?P<prefix>.*?)```(?:json)?\s*(?P<body>\{.*\})\s*```",
                value,
                flags=re.IGNORECASE,
            )
            if fenced_match:
                prefix = fenced_match.group("prefix").strip()
                body = fenced_match.group("body").strip()
                if (
                    len(prefix) <= 2000
                    and not any(token in prefix for token in ("{", "}", "```"))
                    and "```" not in body
                ):
                    try:
                        candidate = json.loads(body)
                    except json.JSONDecodeError:
                        candidate = cls._repair_unescaped_json_quotes(body)
                    if isinstance(candidate, dict):
                        parsed = candidate
        if parsed is None:
            value = str(text or "").strip().lstrip("\ufeff")
            decoder = json.JSONDecoder()
            candidates: list[dict[str, Any]] = []
            for start, char in enumerate(value):
                if char != "{":
                    continue
                try:
                    candidate, end = decoder.raw_decode(value, start)
                except json.JSONDecodeError:
                    continue
                if not isinstance(candidate, dict):
                    continue
                prefix = value[:start].strip()
                suffix = value[end:].strip()
                fenced = False
                for marker in ("```json", "```"):
                    if prefix.lower().endswith(marker):
                        prefix = prefix[:-len(marker)].strip()
                        fenced = True
                        break
                if fenced:
                    if suffix != "```":
                        continue
                elif suffix:
                    continue
                # A second/truncated object or another code block makes the
                # response ambiguous.  Plain explanatory prose is harmless.
                if len(prefix) > 2000 or any(token in prefix for token in ("{", "}", "```")):
                    continue
                candidates.append(candidate)
            if len(candidates) != 1:
                return None
            parsed = candidates[0]
        return cls._remove_flash_shadow_duplicates(parsed)

    @staticmethod
    def _remove_flash_shadow_duplicates(data: dict[str, Any]) -> dict[str, Any]:
        """Collapse only byte-equivalent duplicates or a provably empty shadow.

        A shadow may carry the same two numeric levels but no narrative or
        source.  It contributes no new claim.  Any conflicting/non-empty
        duplicate is deliberately retained so `_validate_flash_items` rejects
        it as an unknown-or-duplicate ID.
        """
        items = data.get("items")
        if not isinstance(items, list):
            return data
        allowed = {
            "id", "crowding", "crowding_level", "catalyst",
            "catalyst_strength", "risk", "sources",
        }

        def is_empty_shadow(shadow: Any, complete: Any) -> bool:
            if not isinstance(shadow, dict) or not isinstance(complete, dict):
                return False
            if set(shadow) - allowed:
                return False
            if str(shadow.get("id") or "").strip() != str(complete.get("id") or "").strip():
                return False
            if any(str(shadow.get(field) or "").strip() for field in ("crowding", "catalyst", "risk")):
                return False
            if shadow.get("sources") != []:
                return False
            if not all(str(complete.get(field) or "").strip() for field in ("crowding", "catalyst", "risk")):
                return False
            if not isinstance(complete.get("sources"), list) or not complete.get("sources"):
                return False
            for field in ("crowding_level", "catalyst_strength"):
                left, right = shadow.get(field), complete.get(field)
                if (
                    isinstance(left, bool) or isinstance(right, bool)
                    or not isinstance(left, (int, float)) or not isinstance(right, (int, float))
                    or not math.isfinite(float(left)) or not math.isfinite(float(right))
                    or float(left) != float(right)
                ):
                    return False
            return True

        normalized: list[Any] = []
        positions: dict[str, int] = {}
        for item in items:
            item_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            if not item_id or item_id not in positions:
                positions[item_id] = len(normalized)
                normalized.append(item)
                continue
            prior_index = positions[item_id]
            prior = normalized[prior_index]
            if item == prior or is_empty_shadow(item, prior):
                continue
            if is_empty_shadow(prior, item):
                normalized[prior_index] = item
                continue
            normalized.append(item)
        return {**data, "items": normalized}

    @staticmethod
    def _merge_flash_recovery(
        search_data: dict[str, Any] | None, finalize_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep the finalizer output but restore required fields it dropped.

        Both inputs are already-paid model responses for the exact same batch.
        Sources are copied only from those responses; this method never creates
        a URL, title or research statement.
        """
        if not isinstance(search_data, dict):
            return finalize_data
        search_items = search_data.get("items")
        finalize_items = finalize_data.get("items")
        if not isinstance(search_items, list) or not isinstance(finalize_items, list):
            return finalize_data
        search_by_id = {
            str(item.get("id") or "").strip(): item
            for item in search_items if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        merged = {**finalize_data, "items": []}
        for raw_item in finalize_items:
            if not isinstance(raw_item, dict):
                merged["items"].append(raw_item)
                continue
            item = dict(raw_item)
            prior = search_by_id.get(str(item.get("id") or "").strip())
            if prior:
                for field in (
                    "crowding", "crowing", "crowding_level", "catalyst",
                    "catalyst_strength", "risk", "sources",
                ):
                    value = item.get(field)
                    prior_value = prior.get(field)
                    if field == "sources":
                        missing = not isinstance(value, list) or not value
                        prior_present = isinstance(prior_value, list) and bool(prior_value)
                    else:
                        missing = value is None or (isinstance(value, str) and not value.strip())
                        prior_present = not (
                            prior_value is None
                            or (isinstance(prior_value, str) and not prior_value.strip())
                        )
                    if missing and prior_present:
                        item[field] = prior_value
            merged["items"].append(item)
        return merged

    @staticmethod
    def _validate_flash_items(data: dict[str, Any], expected_ids: set[str]) -> list[dict[str, Any]]:
        items = data.get("items")
        if not isinstance(items, list):
            raise DeepSeekError("Flash JSON的items必须是数组。")
        if len(items) != len(expected_ids):
            raise DeepSeekError(f"Flash批次数量错误：期望{len(expected_ids)}项，实际{len(items)}项。")
        clean: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise DeepSeekError(f"Flash第{index}项不是JSON对象。")
            item_id = str(item.get("id", "")).strip()
            if item_id not in expected_ids or item_id in seen:
                raise DeepSeekError(f"Flash出现未知或重复ID：{item_id or '<空>'}。")
            seen.add(item_id)
            normalized = {"id": item_id}
            for field in ("crowding", "catalyst", "risk"):
                value = str(item.get(field, "")).strip()
                if not value:
                    raise DeepSeekError(f"Flash项目{item_id}缺少{field}。")
                if len(value) > 400:
                    raise DeepSeekError(f"Flash项目{item_id}的{field}异常过长。")
                normalized[field] = value
            for field in ("crowding_level", "catalyst_strength"):
                value = item.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise DeepSeekError(f"Flash项目{item_id}的{field}必须是0—100的数值。")
                number = float(value)
                if not math.isfinite(number) or not 0 <= number <= 100:
                    raise DeepSeekError(f"Flash项目{item_id}的{field}必须是0—100的有限数值。")
                normalized[field] = number
            if not isinstance(item.get("sources"), list):
                raise DeepSeekError(f"Flash项目{item_id}的sources必须是数组。")
            if not 1 <= len(item["sources"]) <= 2:
                raise DeepSeekError(f"Flash项目{item_id}必须保留1—2个直接来源。")
            normalized["sources"] = item["sources"]
            clean.append(normalized)
        if seen != expected_ids:
            raise DeepSeekError(f"Flash批次ID不完整：缺少{sorted(expected_ids - seen)}。")
        return clean

    @classmethod
    def _flash_recovery_raw_is_complete(cls, raw: dict[str, Any]) -> bool:
        """Apply the normal response integrity gate before local recovery."""
        total_searches, _, _ = cls._web_search_status_counts(raw)
        result = ModelResult(
            text=cls._responses_text(raw), raw=raw,
            model=str(raw.get("model") or "deepseek-v4-flash"),
            input_tokens=0, output_tokens=0,
            web_search_calls=total_searches, response_kind="responses",
        )
        try:
            result.require_complete()
        except DeepSeekError:
            return False
        return True

    def _flash_batch_recovery(self, task_id: str, batch_ids: list[str]) -> dict[str, Any] | None:
        """Return one auditable continuation opportunity for this exact batch.

        A search response may have tool calls but no usable JSON.  New tasks try
        one automatic tool-disabled finalize inside the existing budget.  This
        recovery path remains for older tasks or when auto-finalize was disabled.
        A failed finalize is never looped again.
        """
        rows = self.db.rows(
            """SELECT request_context_json,response_json,web_search_calls,response_status FROM model_calls
               WHERE task_id=? AND stage LIKE 'Flash%' ORDER BY id DESC""",
            (task_id,),
        )
        for row in rows:
            try:
                context = json.loads(row.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(context, dict)
                and context.get("batch_ids") == batch_ids
                and str(row.get("response_status") or "") in {"reserved_before_send", "transport_uncertain"}
            ):
                return {"phase": "transport_uncertain"}
        expected_ids = set(batch_ids)
        recovered_finalize: dict[str, Any] | None = None
        finalize_seen = False
        recovered_search_raw: dict[str, Any] | None = None
        recovered_search_data: dict[str, Any] | None = None
        recovered_search_calls = 0
        recovered_failed_searches = 0
        for row in rows:
            try:
                context = json.loads(row.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(context, dict) or context.get("batch_ids") != batch_ids:
                continue
            phase = str(context.get("phase") or "")
            if phase == "finalize":
                finalize_seen = True
                if recovered_finalize is not None:
                    continue
                try:
                    raw = json.loads(row.get("response_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(raw, dict) and self._flash_recovery_raw_is_complete(raw):
                    recovered_finalize = self._parse_flash_json_text(self._responses_text(raw))
                continue
            if (
                phase != "search"
                or recovered_search_raw is not None
                or int(row.get("web_search_calls") or 0) < 1
            ):
                continue
            try:
                raw = json.loads(row.get("response_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and self._flash_recovery_raw_is_complete(raw):
                _, completed_searches, failed_searches = self._web_search_status_counts(raw)
                if completed_searches < 1:
                    continue
                recovered_search_raw = raw
                recovered_search_data = self._parse_flash_json_text(self._responses_text(raw))
                recovered_search_calls = int(row.get("web_search_calls") or 0)
                recovered_failed_searches = failed_searches

        search_exact = self._flash_data_has_exact_ids(recovered_search_data, expected_ids)
        finalize_exact = self._flash_data_has_exact_ids(recovered_finalize, expected_ids)
        if recovered_search_calls and (search_exact or finalize_exact):
            if search_exact and finalize_exact:
                recovered_data = self._merge_flash_recovery(recovered_search_data, recovered_finalize)
            elif search_exact:
                recovered_data = recovered_search_data
            else:
                recovered_data = recovered_finalize
            return {
                "phase": "local_repair",
                "data": recovered_data,
                "web_search_calls": recovered_search_calls,
                "failed_web_search_calls": recovered_failed_searches,
            }
        if finalize_seen:
            return {"phase": "finalize"}
        if recovered_search_raw is not None:
            return {
                "phase": "search",
                "raw": recovered_search_raw,
                "web_search_calls": recovered_search_calls,
            }
        return None

    def _flash_pending_batches(
        self,
        task_id: str,
        pending: list[dict[str, Any]],
        default_size: int,
    ) -> list[list[dict[str, Any]]]:
        """Keep a saved failed batch intact, then use the current smaller size."""
        if not pending:
            return []
        pending_ids = [item["index_code"] for item in pending]
        first_size = default_size
        rows = self.db.rows(
            """SELECT request_context_json FROM model_calls
               WHERE task_id=? AND stage LIKE 'Flash%' ORDER BY id DESC""",
            (task_id,),
        )
        for row in rows:
            try:
                context = json.loads(row.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            saved_ids = context.get("batch_ids") if isinstance(context, dict) else None
            if (
                isinstance(saved_ids, list) and saved_ids
                and pending_ids[:len(saved_ids)] == saved_ids
                and str(context.get("phase") or "") in {"search", "finalize"}
            ):
                first_size = len(saved_ids)
                break
        batches = [pending[:first_size]]
        for start in range(first_size, len(pending), default_size):
            batches.append(pending[start:start + default_size])
        return [batch for batch in batches if batch]

    @staticmethod
    def _normalize_sources(
        universe_id: str,
        sources: list[dict[str, Any]],
        *,
        expected_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Make batch-local model source IDs globally unique and reject unusable URLs."""
        normalized: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                continue
            url = str(source.get("url", "")).strip()
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password or url in seen_urls:
                continue
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
                continue
            try:
                if ipaddress.ip_address(hostname).is_private or ipaddress.ip_address(hostname).is_loopback:
                    continue
            except ValueError:
                pass
            title = str(source.get("title", "")).strip()
            supports = str(source.get("supports", "")).strip()
            retrieved_at = str(source.get("retrieved_at", "")).strip()
            expected_date = expected_date or local_now().date().isoformat()
            try:
                date.fromisoformat(retrieved_at)
            except ValueError:
                continue
            if retrieved_at != expected_date or not title or not supports:
                continue
            seen_urls.add(url)
            original_id = str(source.get("id", "")).strip() or f"S{index}"
            normalized.append({
                "id": f"{universe_id}-{original_id}-{index}",
                "title": title[:500],
                "url": url,
                "retrieved_at": retrieved_at,
                "supports": supports[:800],
            })
        if not normalized:
            raise DeepSeekError(f"Flash项目{universe_id}没有可用的http(s)证据链接。")
        return normalized

    def run_action_advice(self, task_id: str) -> ActionAdviceResult:
        owner = self._acquire_job_lease(task_id)
        try:
            return self._run_action_advice_with_lease(task_id)
        finally:
            self._release_job_lease(task_id, owner)

    def _run_action_advice_with_lease(self, task_id: str) -> ActionAdviceResult:
        """Create one user-authorized, low-output Pro action pass from a completed run."""
        run = self.db.row("SELECT * FROM radar_runs WHERE task_id=?", (task_id,))
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if not run or not job or job.get("status") != "已完成":
            raise DeepSeekError("只能为已经完成并入库的本轮CSI代表行业研究补全今日行动。")
        compatibility = self.validate_current_job(task_id)
        universe = compatibility["universe"]
        try:
            stored_payload = json.loads(run.get("raw_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeepSeekError("本轮研究原始结果损坏，拒绝生成今日行动。") from exc
        expected_cutoff = ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now()).isoformat()
        policy = self._policy_for_job(task_id)
        if (
            str(stored_payload.get("universe_version") or "") != UNIVERSE_VERSION
            or str(stored_payload.get("protocol_version") or "") != "3.0"
            or str(stored_payload.get("data_cutoff") or "")[:10] != expected_cutoff
            or str(stored_payload.get("ranking_policy_version") or "") != str(policy["version"])
            or int(run.get("universe_total") or 0) != len(universe)
        ):
            raise DeepSeekError("本轮结果不是今天当前目录的完整研究，拒绝生成今日行动。")
        audit = self.db.rows("SELECT * FROM radar_audit WHERE task_id=? ORDER BY universe_id", (task_id,))
        expected_audit_ids = {str(row.get("index_code") or "") for row in universe}
        actual_audit_ids = {str(row.get("universe_id") or "") for row in audit}
        if len(audit) != len(universe) or actual_audit_ids != expected_audit_ids:
            raise DeepSeekError("逐项审计不完整，拒绝生成今日行动。")
        holdings = active_capital_holdings(self.db)
        capital = active_capital_summary(self.db)
        active_limit = capital["limit"]
        active_used = capital["used"]
        score_map = self._stored_score_map(task_id)
        if set(score_map) != expected_audit_ids:
            raise DeepSeekError("本轮本地评分没有完整覆盖固化目录，拒绝生成今日行动。")
        candidate_pool_ids = [
            str(row.get("universe_id") or "") for row in audit
            if str(row.get("pool_route") or "").strip()
        ]
        score_rows = {
            str(row.get("universe_id") or ""): row for row in self.db.rows(
                "SELECT universe_id,base_score,score_components_json FROM sector_metrics WHERE task_id=?",
                (task_id,),
            )
        }
        compact_audit = [[
            row.get("universe_id"), row.get("sector_name"), row.get("category"),
            row.get("proxy_code"), row.get("proxy_name"), row.get("d1"), row.get("d3"),
            row.get("d5"), row.get("d10"), row.get("d20"), row.get("volume_signal"),
            row.get("breadth_signal"), row.get("position_20d"), row.get("crowding"),
            row.get("catalyst"), row.get("note"), score_map[str(row.get("universe_id") or "")],
            json.loads(score_rows[str(row.get("universe_id") or "")].get("score_components_json") or "{}"),
            row.get("local_rank"), row.get("pool_route"), row.get("research_tilt"),
            row.get("timing_status"), row.get("execution_status"), row.get("recommendation_score"),
        ] for row in audit]
        compact_holdings = []
        for row in holdings:
            value = float(row.get("units") or 0) * float(row.get("latest_nav") or 0)
            compact_holdings.append({
                "fund_code": row.get("fund_code"), "fund_name": row.get("fund_name"),
                "index_name": row.get("index_name"), "correlation_bucket": row.get("correlation_bucket"),
                "sleeve": row.get("sleeve"), "confirmed_date": row.get("confirmed_date"),
                "cost": row.get("cost"), "market_value": round(value, 2),
                "position_pct_of_active_limit": round(value / active_limit * 100, 2) if active_limit else 0,
                "thesis": row.get("thesis"),
            })
        old_payload = stored_payload
        action_fund_mappings = [{
            "universe_id": row.get("universe_id"),
            "index_code": row.get("index_code"),
            "fund_code": row.get("fund_code"),
            "fund_name": row.get("fund_name"),
            "share_class": row.get("share_class"),
            "strategy_kind": row.get("strategy_kind"),
            "buyable_status": row.get("public_buyable_status"),
            "checked_at": row.get("platform_checked_at") or row.get("verified_at"),
            "source_url": row.get("platform_evidence_url") or row.get("source_url"),
        } for row in self._strict_executable_funds(sorted(expected_audit_ids))]
        prompt = {
            "task_id": task_id,
            "market_regime": run.get("market_regime"),
            "market_summary": run.get("summary"),
            "existing_avoid_list": old_payload.get("avoid_list", []),
            "existing_risk_flags": old_payload.get("risk_flags", []),
            "constraints": {
                "active_capital_limit": active_limit,
                "active_capital_used": round(active_used, 2),
                "active_capital_available": round(float(capital.get("cash_available", capital.get("available", 0)) or 0), 2),
                "single_position_limit": float(self.db.setting("single_position_limit", "2000")),
                "candidate_count_maximum": ACTION_CANDIDATE_COUNT,
                "candidate_count_minimum": 0,
                "allocation_pct_basis": "active_capital_limit",
                "candidate_ids_must_belong_to_pool": candidate_pool_ids,
                "absolute_candidate_score_minimum": float(policy["candidate_min_score"]),
                "final_score_formula": "软件冻结四维分数；Pro只选择和解释，不改分",
            },
            "audit_columns": [
                "universe_id", "sector_name", "category", "proxy_code", "proxy_name",
                "d1", "d3", "d5", "d10", "d20", "volume_signal", "breadth_signal",
                "position_20d", "crowding", "catalyst", "risk_note", "base_score", "score_components",
                "local_rank", "pool_route", "research_tilt", "timing_status", "execution_status",
                "recommendation_score",
            ],
            "audit": compact_audit,
            "candidate_pool_ids": candidate_pool_ids,
            "holdings": compact_holdings,
            "verified_fund_mappings": action_fund_mappings,
        }
        prompt_text = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
        model_config = self._model_config_for_job(task_id)
        system_text = self._daily_action_system()
        request_key = self._stable_request_key("daily-action", {"logical_call": "once"})
        existing_call = self._model_call_for_request(
            task_id, request_key, legacy_phase="daily_action", legacy_stage="今日行动Pro"
        )
        result = self._obtain_daily_action_result(
            task_id=task_id,
            job=job,
            prompt_text=prompt_text,
            system_text=system_text,
            model_config=model_config,
            request_key=request_key,
            existing_call=existing_call,
        )
        if result is None:
            raise DeepSeekError("今日行动没有取得可审计响应；软件不会绕过幂等占位直接发送。")
        reservation_id = None  # 发送、预留与本地重放均由上方统一入口处理。
        try:
            result = result if result is not None else self.client.pro_json(
                system=(
                    "你是板块雷达今日行动模型。只使用输入中的本轮CSI叶子行业审计和当前持仓。"
                    "只返回JSON对象，键为action_summary、no_buy_reason、candidates、holding_actions、"
                    f"questions_for_intraday、risk_flags。candidates可返回0到{ACTION_CANDIDATE_COUNT}项；每项包含"
                    "universe_id、timing_status、allocation_pct、correlation_bucket、thesis、catalyst、"
                    "crowding、risk、buy_conditions、abandon_conditions、fund_code、fund_name。"
                    "allocation_pct必须按输入中的active_capital_limit计算，单项不超过25；可以返回空清单。"
                    "correlation_bucket必须使用输入中该指数的allocation_cluster；不得把同一父级的宽泛行业与子行业当成两份仓位。"
                    "即使没有核验基金，也要评价板块并给目标比例，fund_code和fund_name留空。"
                    "候选ID只能来自candidate_pool_ids，可自由换人和少选；不得选择池外方向或返回主观总分。"
                    "holding_actions必须逐只覆盖holdings，每项包含fund_code、action、sell_pct、reason、risk；"
                    "action只能为持有、继续定投、减仓、清仓，前两者sell_pct=0，减仓为1到99，清仓=100。"
                    "卖出比例是该基金自身持仓的百分比，不要计算卖出金额，软件会按本地市值计算。"
                    "timing_status只能为可以买、等待回调、不宜介入；若所有方向allocation_pct均为0，"
                    "no_buy_reason必须明确说明今日为什么不买。"
                ),
                prompt=prompt_text,
                model=model_config["deepseek_pro_model"], max_tokens=max_tokens,
                reasoning_effort="high", input_price_per_million=input_price,
                cached_input_price_per_million=float(model_config["pro_cached_input_price_cny_per_million"]),
                output_price_per_million=output_price,
            )
        except Exception as exc:
            if reservation_id is not None:
                self._mark_reserved_model_call_uncertain(reservation_id, exc)
            self._set_job(task_id, cost=self._job_cost(task_id))
            raise
        if reservation_id is not None:
            self._finalize_reserved_model_call(reservation_id, result)
            self._set_job(task_id, cost=self._job_cost(task_id))
        try:
            decision = result.json()
        except DeepSeekError as exc:
            decision = self._repair_unescaped_json_quotes(result.text) if result.finish_reason == "stop" and "不是有效JSON" in str(exc) else None
            if decision is None:
                raise
        action = self._normalize_action_payload(task_id, decision)
        save_action_advice(self.db, task_id, action)
        buy_count = sum(float(row.get("suggested_amount", 0) or 0) > 0 for row in action["candidates"])
        self._event(task_id, "INFO", "今日行动Pro", f"今日行动已入库：板块清单{len(action['candidates'])}项，可买{buy_count}项，持仓动作{len(action['holding_actions'])}项。")
        return ActionAdviceResult(
            task_id, "已完成", self._job_cost(task_id), buy_count, len(action["holding_actions"]),
            f"今日行动已生成：主榜{len(action['candidates'])}项，可买{buy_count}项，持仓动作{len(action['holding_actions'])}项。",
        )

    @staticmethod
    def _daily_action_system() -> str:
        return (
            "你是板块雷达今日行动模型。只使用输入中的本轮CSI叶子行业审计和当前持仓。"
            "只返回JSON对象，键为action_summary、no_buy_reason、candidates、holding_actions、"
            f"questions_for_intraday、risk_flags。candidates可返回0到{ACTION_CANDIDATE_COUNT}项；每项包含"
            "universe_id、timing_status、allocation_pct、correlation_bucket、thesis、catalyst、"
            "crowding、risk、buy_conditions、abandon_conditions、fund_code、fund_name。"
            "allocation_pct必须按输入中的active_capital_limit计算，单项不超过25；可以返回空清单。"
            "correlation_bucket必须使用输入中该指数的allocation_cluster；不得把同一父级的宽泛行业与子行业当成两份仓位。"
            "即使没有核验基金，也要评价板块并给目标比例，fund_code和fund_name留空。"
            "候选ID只能来自candidate_pool_ids，可自由换人和少选；不得选择池外方向或返回主观总分。"
            "holding_actions必须逐只覆盖holdings，每项包含fund_code、action、sell_pct、reason、risk；"
            "action只能为持有、继续定投、减仓、清仓，前两者sell_pct=0，减仓为1到99，清仓=100。"
            "卖出比例是该基金自身持仓的百分比，不要计算卖出金额，软件会按本地市值计算。"
            "timing_status只能为可以买、等待回调、不宜介入；若所有方向allocation_pct均为0，"
            "no_buy_reason必须明确说明今日为什么不买。"
        )

    def _obtain_daily_action_result(
        self,
        *,
        task_id: str,
        job: dict[str, Any],
        prompt_text: str,
        system_text: str,
        model_config: dict[str, str],
        request_key: str,
        existing_call: dict[str, Any] | None,
    ) -> ModelResult:
        model = model_config["deepseek_pro_model"]
        reasoning_effort = "high"
        input_price = float(model_config["pro_input_price_cny_per_million"])
        cached_price = float(model_config["pro_cached_input_price_cny_per_million"])
        output_price = float(model_config["pro_output_price_cny_per_million"])
        if existing_call:
            try:
                context = json.loads(existing_call.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                context = {}
            try:
                max_tokens = int(context.get("max_tokens") or 8000)
            except (TypeError, ValueError):
                max_tokens = 8000
            fingerprint = self._request_fingerprint({
                "system": system_text,
                "prompt": prompt_text,
                "model": model,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
            })
            result = self._recover_model_result(
                task_id,
                request_key,
                request_fingerprint=fingerprint,
                legacy_phase="daily_action",
                legacy_stage="今日行动Pro",
            )
            if result is None:
                raise DeepSeekError("今日行动审计记录存在但无法恢复；软件不会自动重发。")
            self._event(
                task_id,
                "INFO",
                "今日行动本地恢复",
                "已从本地审计记录重放先前成功的今日行动响应；未再次连接API、未产生第二次费用。",
            )
            return result

        if not self.client:
            raise DeepSeekError("尚未建立DeepSeek客户端。")
        self.client.connection_report(
            model_config["deepseek_flash_model"], model, require_pro=True,
        )
        projected_input_cost = math.ceil(len(prompt_text) * 1.2) * input_price / 1_000_000
        remaining_job = float(job["cost_limit_cny"]) - self._job_cost(task_id)
        action_limit = float(self.db.setting("api_action_cost_limit_cny", "0.50"))
        available_budget = min(remaining_job, action_limit)
        safety_reserve = min(0.03, max(0.01, available_budget * 0.03))
        affordable_output = math.floor(
            max(0.0, available_budget - projected_input_cost - safety_reserve) * 1_000_000 / output_price
        )
        if affordable_output < 3000:
            minimum = projected_input_cost + 3000 * output_price / 1_000_000
            raise DeepSeekError(
                f"今日行动发送前预算不足：最低预计约¥{minimum:.4f}，本轮可用预算¥{available_budget:.4f}。未发送请求。"
            )
        max_tokens = min(8000, affordable_output)
        projected_cost = projected_input_cost + max_tokens * output_price / 1_000_000
        fingerprint = self._request_fingerprint({
            "system": system_text,
            "prompt": prompt_text,
            "model": model,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        })
        self._event(
            task_id,
            "INFO",
            "今日行动Pro",
            f"用户确认补全今日行动；输入{len(prompt_text)}字符，最多约¥{projected_cost:.4f}，输出上限{max_tokens} tokens。",
        )
        self._check_cancelled()
        reservation_id = self._reserve_model_call(
            task_id,
            "今日行动Pro",
            model,
            projected_cost,
            request_key=request_key,
            context={
                "phase": "daily_action",
                "request_state": "reserved_before_send",
                "request_fingerprint": fingerprint,
                "prompt_chars": len(prompt_text),
                "max_tokens": max_tokens,
            },
        )
        try:
            result = self.client.pro_json(
                system=system_text,
                prompt=prompt_text,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                input_price_per_million=input_price,
                cached_input_price_per_million=cached_price,
                output_price_per_million=output_price,
            )
        except Exception as exc:
            self._mark_reserved_model_call_uncertain(reservation_id, exc)
            self._set_job(task_id, cost=self._job_cost(task_id))
            raise DeepSeekError(
                f"今日行动传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
            ) from exc
        self._finalize_reserved_model_call(reservation_id, result)
        self._set_job(task_id, cost=self._job_cost(task_id))
        return result

    def _normalize_action_payload(self, task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict):
            raise DeepSeekError("今日行动不是JSON对象，结果未入库。")
        candidates = decision.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > ACTION_CANDIDATE_COUNT:
            raise DeepSeekError(f"今日行动最多给出{ACTION_CANDIDATE_COUNT}个排序板块，结果未入库。")
        score_map = self._stored_score_map(task_id)
        audit = {str(row["universe_id"]): row for row in self.db.rows(
            "SELECT * FROM radar_audit WHERE task_id=?", (task_id,)
        )}
        if set(score_map) != set(audit):
            raise DeepSeekError("本轮基础评分与逐项审计范围不一致，结果未入库。")
        pool_ids = {uid for uid, row in audit.items() if str(row.get("pool_route") or "").strip()}
        candidates = self._normalize_ranked_scores(
            candidates, score_map, allowed_ids=pool_ids, maximum_count=ACTION_CANDIDATE_COUNT,
        )
        mappings = {
            (str(row["universe_id"]), str(row["fund_code"])): row
            for row in self._strict_executable_funds(sorted(audit))
        }
        active_limit = float(self.db.setting("active_capital_limit", "8000"))
        single_limit = float(self.db.setting("single_position_limit", "2000"))
        max_single_pct = single_limit / active_limit * 100 if active_limit > 0 else 0
        policy = self._policy_for_job(task_id)
        normalized_candidates = []
        seen: set[str] = set()
        for item in candidates:
            if not isinstance(item, dict):
                raise DeepSeekError("今日行动板块项不是JSON对象，结果未入库。")
            uid = str(item.get("universe_id") or "").strip()
            if uid not in audit or uid in seen:
                raise DeepSeekError(f"今日行动引用未知或重复板块{uid}，结果未入库。")
            seen.add(uid)
            try:
                requested_pct = max(0.0, min(max_single_pct, float(item.get("allocation_pct", 0) or 0)))
            except (TypeError, ValueError) as exc:
                raise DeepSeekError(f"板块{uid}的配置比例不是数字，结果未入库。") from exc
            fund_code = str(item.get("fund_code") or "").strip()
            if fund_code and (uid, fund_code) not in mappings:
                fund_code = ""
            row = audit[uid]
            try:
                components = json.loads(row.get("score_components_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                components = {}
            freshness_ok = bool(components.get("freshness_ok", False))
            confidence = float(row.get("data_confidence") or 0)
            timing_status = str(item.get("timing_status") or row.get("timing_status") or "等待回调")
            if timing_status not in {"可以买", "等待回调", "不宜介入"}:
                raise DeepSeekError(f"板块{uid}的择时状态无效，结果未入库。")
            if not freshness_ok:
                execution_status = "行情待更新"
            elif confidence < float(policy["data_confidence_min"]):
                execution_status = "数据待补齐"
            elif not fund_code:
                execution_status = "基金待绑定"
            else:
                execution_status = "可执行"
            platform = self.db.row(
                """SELECT ant_index_code,official_index_code,allocation_cluster
                     FROM investable_indices
                    WHERE universe_id=? AND active=1 ORDER BY id LIMIT 1""",
                (uid,),
            ) or {}
            normalized_candidates.append({
                "universe_id": uid,
                "index_code": str(
                    platform.get("ant_index_code")
                    or (
                        f"{platform.get('official_index_code')}.CSI"
                        if str(platform.get("official_index_code") or "").isdigit()
                        else platform.get("official_index_code")
                    )
                    or ""
                ).upper(),
                "sector_name": str(row.get("sector_name") or ""), "fund_code": fund_code,
                "fund_name": str(mappings[(uid, fund_code)].get("fund_name") or "") if fund_code else "",
                "status": str(row.get("research_tilt") or "观察"),
                "research_tilt": str(row.get("research_tilt") or "观察"),
                "timing_status": timing_status, "execution_status": execution_status,
                "base_score": float(item.get("base_score", 0) or 0),
                "score_adjustment": 0.0,
                "score": float(item.get("score", 0) or 0),
                "recommendation_score": float(row.get("recommendation_score") or 0),
                "opportunity_score": float(row.get("opportunity_score") or 0),
                "trend_score": float(row.get("trend_score") or 0),
                "entry_score": float(row.get("entry_score") or 0),
                "crowding_risk_score": float(row.get("crowding_risk_score") or 0),
                "data_confidence": confidence, "freshness_ok": freshness_ok,
                "target_allocation_pct": requested_pct, "allocation_pct": requested_pct,
                "suggested_amount": 0.0,
                "correlation_bucket": str(platform.get("allocation_cluster") or row.get("category") or ""),
                "pool_route": str(row.get("pool_route") or ""),
                "thesis": str(item.get("thesis") or ""), "catalyst": str(item.get("catalyst") or row.get("catalyst") or ""),
                "crowding": str(item.get("crowding") or row.get("crowding") or ""), "risk": str(item.get("risk") or ""),
                "buy_conditions": str(item.get("buy_conditions") or ""),
                "abandon_conditions": str(item.get("abandon_conditions") or ""),
            })
        normalized_candidates, _suppressed = final_display_candidates(
            normalized_candidates,
            maximum_count=ACTION_CANDIDATE_COUNT,
            max_per_cluster=int(policy["display_cluster_max"]),
            minimum_score=float(policy["candidate_min_score"]),
        )
        normalized_candidates = allocate_candidate_amounts(self.db, normalized_candidates)
        actions = decision.get("holding_actions")
        if not isinstance(actions, list):
            raise DeepSeekError("今日行动缺少逐只持仓动作，结果未入库。")
        normalized_holdings = self._normalize_holding_actions(actions, require_all=True)
        positive = any(row["suggested_amount"] > 0 for row in normalized_candidates)
        no_buy_reason = str(decision.get("no_buy_reason") or "").strip()
        if not positive and not no_buy_reason:
            no_buy_reason = "本轮候选没有同时通过研究、择时、数据、基金与仓位门槛，因此今日不新增主动仓位。"
        return {
            "action_summary": str(decision.get("action_summary") or "").strip() or "今日行动已完成。",
            "no_buy_reason": no_buy_reason, "candidates": normalized_candidates,
            "holding_actions": normalized_holdings,
            "questions_for_intraday": decision.get("questions_for_intraday", []),
            "risk_flags": decision.get("risk_flags", []),
        }

    @staticmethod
    def _pro_decision_system(structure_test: bool) -> str:
        return (
            "你是板块雷达终审模型。只可使用输入中的本地硬数据和Flash联网证据。"
            "不要复制items、sources或生成audit，软件会从已校验数据在本机组装这些内容。"
            "只返回一个JSON对象，包含market_regime、market_summary、action_summary、no_buy_reason、"
            "candidates、holding_actions、avoid_list、fund_updates、questions_for_intraday、risk_flags。"
            f"market_regime只能是趋势进攻、轮动震荡或全面防守之一。正常完整扫描中candidates可返回0到{ACTION_CANDIDATE_COUNT}个方向；"
            "10是上限，不得为凑数加入低于绝对线的项目。每项必须包含"
            "universe_id、index_code、sector_name、fund_code、fund_name、timing_status、"
            "allocation_pct、correlation_bucket、thesis、catalyst、crowding、risk、buy_conditions、"
            "abandon_conditions。allocation_pct是占active_capital_limit的百分比，单项不得超过25；"
            "没有经核验基金时fund_code和fund_name留空，但仍可给板块目标比例并标记基金待绑定。"
            "candidates的universe_id必须来自candidate_pool，可自由取舍，不必保留本地原前10；不得选择池外方向。"
            "不得返回或修改任何主观总分；软件会保留本地四维分数、七档研究结论并做最终去重。"
            "只允许从输入的叶子行业中选择；宽泛父级不在研究池。correlation_bucket必须逐字使用该项的allocation_cluster，"
            "同一allocation_cluster下的多个候选必须按一个相关仓位组理解，不能借子行业拆分绕过仓位限制。"
            "holding_actions必须逐只覆盖holdings，每项含fund_code、action、sell_pct、reason、risk；"
            "action只能为持有、继续定投、减仓、清仓，前两者sell_pct=0，清仓=100。"
            "timing_status只能为可以买、等待回调、不宜介入。若没有正比例买入，no_buy_reason必须明确说明原因。"
            "avoid_list最多12项，每项必须是对象并含universe_id、index_code、sector_name、reason、risk_level；"
            "risk_level只能为高、中、低，且universe_id必须来自本轮目录。其他列表保持简短。"
            + ("这是结构测试，不得生成购买候选，candidates必须为空。" if structure_test else "")
        )

    def _obtain_pro_decision_result(
        self,
        *,
        task_id: str,
        job: dict[str, Any],
        universe_count: int,
        audit_count: int,
        prompt_text: str,
        system_text: str,
        model: str,
        desired_output_tokens: int,
        reasoning_effort: str,
        input_price: float,
        cached_price: float,
        output_price: float,
        request_key: str,
        existing_call: dict[str, Any] | None,
    ) -> ModelResult:
        if existing_call:
            try:
                context = json.loads(existing_call.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                context = {}
            try:
                max_tokens = int(context.get("max_tokens") or desired_output_tokens)
            except (TypeError, ValueError):
                max_tokens = desired_output_tokens
            fingerprint = self._request_fingerprint({
                "system": system_text,
                "prompt": prompt_text,
                "model": model,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
            })
            result = self._recover_model_result(
                task_id,
                request_key,
                request_fingerprint=fingerprint,
                legacy_phase="pro_decision_only",
                legacy_stage="Pro终审",
            )
            if result is None:
                raise DeepSeekError("Pro终审审计记录存在但无法恢复；软件不会自动重发。")
            self._event(
                task_id,
                "INFO",
                "Pro终审本地恢复",
                "已从本地审计记录重放先前成功的Pro响应；未再次连接API、未产生第二次费用。",
            )
            return result

        if not self.client:
            raise DeepSeekError("尚未建立DeepSeek客户端。")
        remaining = float(job["cost_limit_cny"]) - self._job_cost(task_id)
        projected_tokens = max(1, math.ceil(len(prompt_text) * 1.2))
        projected_input_cost = projected_tokens * input_price / 1_000_000
        minimum_output_tokens = 4000
        safety_reserve = min(0.05, max(0.01, remaining * 0.03))
        affordable_output_tokens = math.floor(
            max(0.0, remaining - projected_input_cost - safety_reserve) * 1_000_000 / output_price
        )
        if affordable_output_tokens < minimum_output_tokens:
            minimum_cost = projected_input_cost + minimum_output_tokens * output_price / 1_000_000
            raise DeepSeekError(
                f"Pro终审发送前预算检查未通过：压缩后的输入和最低完整输出预计仍需约¥{minimum_cost:.4f}，"
                f"当前剩余¥{remaining:.4f}。未发送Pro请求。"
            )
        max_tokens = min(desired_output_tokens, affordable_output_tokens)
        projected_cost = projected_input_cost + max_tokens * output_price / 1_000_000
        fingerprint = self._request_fingerprint({
            "system": system_text,
            "prompt": prompt_text,
            "model": model,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        })
        self._event(
            task_id,
            "INFO",
            "Pro终审",
            f"发送前预算检查：{universe_count}项输入已合并去重为{len(prompt_text)}字符；"
            f"逐项审计由本机组装，Pro只返回终审判断；剩余¥{remaining:.4f}，"
            f"保守估算本次至多约¥{projected_cost:.4f}，输出上限{max_tokens} tokens。",
        )
        self._check_cancelled()
        reservation_id = self._reserve_model_call(
            task_id,
            "Pro终审",
            model,
            projected_cost,
            request_key=request_key,
            context={
                "phase": "pro_decision_only",
                "request_state": "reserved_before_send",
                "request_fingerprint": fingerprint,
                "prompt_chars": len(prompt_text),
                "max_tokens": max_tokens,
                "local_audit_items": audit_count,
            },
        )
        try:
            result = self.client.pro_json(
                system=system_text,
                prompt=prompt_text,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                input_price_per_million=input_price,
                cached_input_price_per_million=cached_price,
                output_price_per_million=output_price,
            )
        except Exception as exc:
            self._mark_reserved_model_call_uncertain(reservation_id, exc)
            self._set_job(task_id, cost=self._job_cost(task_id))
            raise DeepSeekError(
                f"Pro终审传输状态不明，服务端可能已经计费；禁止自动重发。原始错误：{exc}"
            ) from exc
        self._finalize_reserved_model_call(reservation_id, result)
        self._set_job(task_id, cost=self._job_cost(task_id))
        return result

    def _run_pro(self, task_id, universe, metrics, evidence) -> tuple[dict[str, Any], str]:
        """Ask Pro for judgement only; assemble the bulky audit locally.

        Metrics and Flash evidence have already passed deterministic validation.  Asking
        the model to copy every audited row back into the final JSON wastes many
        of output tokens and can hit the model context limit.  Pro now returns only the
        genuinely judgemental portion of the result.  The application then joins that
        decision with the already-validated rows and runs the strict V3 validator.
        """
        job_mode = (self.db.row("SELECT mode FROM analysis_jobs WHERE task_id=?", (task_id,)) or {}).get("mode", "完整扫描")
        structure_test = job_mode != "完整扫描"
        holdings = active_capital_holdings(self.db)
        evidence_map = {str(row.get("universe_id") or ""): dict(row) for row in evidence}
        score_map, scorecards = self._persist_local_scores(task_id, metrics, evidence)
        # Build the prompt only after scores have been written back into metrics;
        # the former order copied base_score=0 into the Pro prompt and raw audit.
        metric_map = {str(row.get("universe_id") or ""): dict(row) for row in metrics}
        policy = self._policy_for_job(task_id)
        cluster_map = {
            str(row.get("index_code") or ""): str(
                row.get("allocation_cluster") or row.get("category") or row.get("index_code") or "未分类"
            )
            for row in universe
        }
        pool_rows = [] if structure_test else select_candidate_pool(
            scorecards,
            cluster_map,
            route_size=max(8, min(12, math.ceil(int(policy["pool_max"]) / 3))),
            minimum_size=int(policy["pool_min"]),
            maximum_size=int(policy["pool_max"]),
            max_per_cluster=int(policy["pool_cluster_max"]),
            minimum_score=float(policy["candidate_min_score"]),
        )
        candidate_pool_ids = [str(row.get("universe_id") or "") for row in pool_rows]
        candidate_pool_set = set(candidate_pool_ids)
        if len(candidate_pool_ids) != len(candidate_pool_set):
            raise DeepSeekError("本地候选池出现重复项目，已在Pro调用前停止。")
        local_rank_map = {
            universe_id: rank
            for rank, (universe_id, _score) in enumerate(
                sorted(score_map.items(), key=lambda item: (-float(item[1]), item[0])), start=1
            )
        }
        fund_mappings = self._strict_executable_funds(
            [str(row.get("index_code") or "") for row in universe]
        )
        item_columns = [
            "id", "name", "category", "csi_index_code", "official_index_code",
            "parent_official_code", "allocation_cluster",
            "proxy_code", "proxy_name",
            "d1", "d3", "d5", "d10", "d20", "volume_signal", "breadth_signal",
            "breadth_kind", "breadth_pct",
            "position_20d", "amount_ratio_5d", "crowding", "catalyst", "risk", "source_ids",
            "data_source", "data_cutoff", "mapping_kind", "mapping_confidence", "data_quality",
                "base_score", "score_components",
            "cache_hit", "buyable_available", "buyable_fund_code", "buyable_status", "buyable_notes",
        ]
        source_columns = ["id", "title", "url", "retrieved_at", "supports"]
        sources: dict[str, dict[str, Any]] = {}
        compact_items = []
        audit = []
        for universe_row in universe:
            universe_id = str(universe_row.get("index_code") or "")
            metric = metric_map.get(universe_id, {})
            evidence_item = evidence_map.get(universe_id, {})
            components = json.loads(metric.get("score_components_json") or "{}")
            try:
                item_sources = json.loads(evidence_item.get("source_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                item_sources = []
            source_ids = []
            for source in item_sources:
                if not isinstance(source, dict) or not source.get("id"):
                    continue
                source_id = str(source["id"])
                source_ids.append(source_id)
                sources[source_id] = {key: source.get(key, "") for key in source_columns}
            compact_items.append([
                universe_id,
                universe_row.get("index_name", ""),
                universe_row.get("category", ""),
                universe_row.get("csi_index_code", universe_row.get("ant_index_code", "")),
                universe_row.get("official_index_code", ""),
                universe_row.get("parent_official_code", ""),
                universe_row.get("allocation_cluster", ""),
                metric.get("proxy_code", ""),
                metric.get("proxy_name", ""),
                metric.get("d1"), metric.get("d3"), metric.get("d5"), metric.get("d10"), metric.get("d20"),
                metric.get("volume_signal", ""),
                metric.get("breadth_signal", ""),
                metric.get("breadth_kind", ""),
                metric.get("breadth_pct"),
                metric.get("position_20d", ""),
                metric.get("amount_ratio_5d"),
                evidence_item.get("crowding", ""),
                evidence_item.get("catalyst", ""),
                evidence_item.get("risk", ""),
                source_ids,
                metric.get("data_source", ""),
                metric.get("data_cutoff", ""),
                metric.get("mapping_kind", ""),
                metric.get("mapping_confidence", ""),
                metric.get("data_quality", ""),
                metric.get("base_score", 0),
                components,
                bool(metric.get("cache_hit")),
                bool(universe_row.get("has_buyable_fund")),
                universe_row.get("fund_code", ""),
                universe_row.get("status", ""),
                universe_row.get("notes", ""),
            ])
            audit.append({
                "id": universe_id,
                "name": universe_row.get("index_name", ""),
                "category": universe_row.get("category", ""),
                "proxy_code": metric.get("proxy_code", ""),
                "proxy_name": metric.get("proxy_name", ""),
                "returns_pct": {
                    "d1": metric.get("d1"), "d3": metric.get("d3"), "d5": metric.get("d5"),
                    "d10": metric.get("d10"), "d20": metric.get("d20"),
                },
                "volume_signal": metric.get("volume_signal", ""),
                "breadth_signal": metric.get("breadth_signal", ""),
                "breadth_kind": metric.get("breadth_kind", ""),
                "breadth_pct": metric.get("breadth_pct"),
                "position_20d": metric.get("position_20d", ""),
                "crowding": evidence_item.get("crowding", ""),
                "catalyst": evidence_item.get("catalyst", ""),
                "base_score": metric.get("base_score", 0),
                "score_components": components,
                "local_rank": local_rank_map.get(universe_id, 0),
                "pool_route": next(
                    (str(row.get("pool_route") or "") for row in pool_rows if row.get("universe_id") == universe_id),
                    "",
                ),
                "research_tilt": components.get("research_tilt", "观察"),
                "timing_status": components.get("timing_status", "等待回调"),
                "execution_status": "研究完成",
                "opportunity_score": components.get("opportunity_score", 0),
                "trend_score": components.get("trend_opportunity", 0),
                "entry_score": components.get("entry_quality", 0),
                "crowding_risk_score": components.get("crowding_risk", 0),
                "data_confidence": components.get("data_confidence", 0),
                "recommendation_score": metric.get("base_score", 0),
                "freshness_ok": bool(components.get("freshness_ok", False)),
                "status": "ANALYZED",
                "source_ids": source_ids,
                "note": f"主要风险：{evidence_item.get('risk', '')}",
            })
        active_limit = float(self.db.setting("active_capital_limit", "8000"))
        compact_holdings = []
        for row in holdings:
            item = {
                key: row.get(key)
                for key in (
                    "fund_code", "fund_name", "index_name", "correlation_bucket", "confirmed_date",
                    "units", "cost", "latest_nav", "nav_date", "sleeve", "thesis",
                )
            }
            market_value = float(row.get("units") or 0) * float(row.get("latest_nav") or 0)
            item["market_value"] = round(market_value, 2)
            item["position_pct_of_active_limit"] = round(market_value / active_limit * 100, 2) if active_limit else 0
            compact_holdings.append(item)
        compact_mappings = [{
            "universe_id": row.get("universe_id"), "index_code": row.get("index_code"),
            "fund_code": row.get("fund_code"), "fund_name": row.get("fund_name"),
            "share_class": row.get("share_class"),
            "buyable_status": row.get("public_buyable_status"),
            "source_url": row.get("platform_evidence_url") or row.get("source_url"),
            "verified_at": row.get("platform_checked_at") or row.get("verified_at"),
        } for row in fund_mappings]
        prompt = {
            "task_id": task_id,
            "universe_version": UNIVERSE_VERSION,
            "constraints": {
                "only_off_exchange_funds": True, "no_stocks": True, "no_exchange_traded_etf_orders": True,
                "active_capital_limit": float(self.db.setting("active_capital_limit", "8000")),
                "single_position_limit": float(self.db.setting("single_position_limit", "2000")),
                "correlation_bucket_limit": float(self.db.setting("correlation_bucket_limit", "3000")),
                "candidate_count_maximum": 0 if structure_test else ACTION_CANDIDATE_COUNT,
                "candidate_count_minimum": 0,
                "candidate_ids_must_belong_to_pool": candidate_pool_ids,
                "absolute_candidate_score_minimum": float(policy["candidate_min_score"]),
                "score_formula": "软件本地四维推荐分；Pro只选择、判断时机和解释，不得改分",
                "maximum_displayed_per_correlation_bucket": int(policy["display_cluster_max"]),
                "maximum_positive_amount_per_correlation_bucket": int(policy["funded_cluster_max"]),
            },
            "ranking_policy": policy,
            "candidate_pool": pool_rows,
            "item_columns": item_columns,
            "items": compact_items,
            "source_columns": source_columns,
            "sources": [[source.get(key, "") for key in source_columns] for source in sources.values()],
            "holdings": compact_holdings,
            "fund_mappings": compact_mappings,
        }
        prompt_text = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
        model_config = self._model_config_for_job(task_id)
        try:
            configured_max_tokens = int(float(model_config["api_pro_max_tokens"]))
        except (TypeError, ValueError):
            configured_max_tokens = 12000
        try:
            hard_output_cap = int(float(model_config["api_pro_hard_output_cap_tokens"]))
        except (TypeError, ValueError):
            hard_output_cap = 12000
        hard_output_cap = max(4000, min(12000, hard_output_cap))
        # The response contains decisions only.  A hard ceiling prevents another
        # 80k-token copy loop even when an older database still stores that setting.
        desired_output_tokens = max(4000, min(hard_output_cap, configured_max_tokens))
        reasoning_effort = str(model_config["pro_reasoning_effort"]).strip().lower()
        if reasoning_effort not in {"high", "max"}:
            reasoning_effort = "max"
        model = model_config["deepseek_pro_model"]
        input_price = float(model_config["pro_input_price_cny_per_million"])
        cached_price = float(model_config["pro_cached_input_price_cny_per_million"])
        output_price = float(model_config["pro_output_price_cny_per_million"])
        system_text = self._pro_decision_system(structure_test)
        request_key = self._stable_request_key("pro-decision", {"logical_call": "once"})
        existing_call = self._model_call_for_request(
            task_id, request_key, legacy_phase="pro_decision_only", legacy_stage="Pro终审"
        )
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        result = self._obtain_pro_decision_result(
            task_id=task_id,
            job=job,
            universe_count=len(universe),
            audit_count=len(audit),
            prompt_text=prompt_text,
            system_text=system_text,
            model=model,
            desired_output_tokens=desired_output_tokens,
            reasoning_effort=reasoning_effort,
            input_price=input_price,
            cached_price=cached_price,
            output_price=output_price,
            request_key=request_key,
            existing_call=existing_call,
        )
        if result is None:
            raise DeepSeekError("Pro终审没有取得可审计响应；软件不会绕过幂等占位直接发送。")
        reservation_id = None  # 发送、预留与本地重放均由上方统一入口处理。
        try:
            result = result if result is not None else self.client.pro_json(
                system=(
                "你是板块雷达终审模型。只可使用输入中的本地硬数据和Flash联网证据。"
                "不要复制items、sources或生成audit，软件会从已校验数据在本机组装这些内容。"
                "只返回一个JSON对象，包含market_regime、market_summary、action_summary、no_buy_reason、"
                "candidates、holding_actions、avoid_list、fund_updates、questions_for_intraday、risk_flags。"
                f"market_regime只能是趋势进攻、轮动震荡或全面防守之一。正常完整扫描中candidates可返回0到{ACTION_CANDIDATE_COUNT}个方向；"
                "10是上限，不得为凑数加入低于绝对线的项目。每项必须包含"
                "universe_id、index_code、sector_name、fund_code、fund_name、timing_status、"
                "allocation_pct、correlation_bucket、thesis、catalyst、crowding、risk、buy_conditions、"
                "abandon_conditions。allocation_pct是占active_capital_limit的百分比，单项不得超过25；"
                "没有经核验基金时fund_code和fund_name留空，但仍可给板块目标比例并标记基金待绑定。"
                "fund_code与fund_name必须逐字取自fund_mappings中相同universe_id的记录，禁止跨指数、A类、相似指数或自行联网猜基金。"
                "candidates的universe_id必须来自candidate_pool，可自由取舍，不必保留本地原前10；不得选择池外方向。"
                "不得返回或修改任何主观总分；软件会保留本地四维分数、七档研究结论并做最终去重。"
                "只允许从输入的叶子行业中选择；宽泛父级不在研究池。correlation_bucket必须逐字使用该项的allocation_cluster，"
                "同一allocation_cluster下的多个候选必须按一个相关仓位组理解，不能借子行业拆分绕过仓位限制。"
                "holding_actions必须逐只覆盖holdings，每项含fund_code、action、sell_pct、reason、risk；"
                "action只能是持有、继续定投、减仓、清仓，前两者sell_pct=0，清仓=100。"
                "timing_status只能为可以买、等待回调、不宜介入。若没有正比例买入，no_buy_reason必须明确说明原因。"
                "avoid_list最多12项，每项必须是对象并含universe_id、index_code、sector_name、reason、risk_level；"
                "risk_level只能为高、中、低，且universe_id必须来自本轮目录。其他列表保持简短。"
                + ("这是结构测试，不得生成购买候选，candidates必须为空。" if structure_test else "")
                ),
                prompt=prompt_text,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                input_price_per_million=input_price,
                cached_input_price_per_million=cached_price,
                output_price_per_million=output_price,
            )
        except Exception as exc:
            if reservation_id is not None:
                self._mark_reserved_model_call_uncertain(reservation_id, exc)
            self._set_job(task_id, cost=self._job_cost(task_id))
            raise
        if reservation_id is not None:
            self._finalize_reserved_model_call(reservation_id, result)
            self._set_job(task_id, cost=self._job_cost(task_id))
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if self._job_cost(task_id) > float(job["cost_limit_cny"]):
            raise DeepSeekError("Pro调用后总估算费用超过本次上限，结果不入库。")
        try:
            decision = result.json()
        except DeepSeekError as exc:
            decision = None
            if result.finish_reason == "stop" and "不是有效JSON" in str(exc):
                decision = self._repair_unescaped_json_quotes(result.text)
            if decision is None:
                raise
            self._event(
                task_id, "INFO", "Pro终审",
                "Pro判断正文存在字符串内部未转义双引号；已在本机仅转义该字符并继续严格校验，未再次调用API。",
            )
        payload = self._assemble_pro_payload(
            task_id, universe, metrics, audit, list(sources.values()), decision,
        )
        raw_text = START_MARKER + "\n" + json.dumps(payload, ensure_ascii=False) + "\n" + END_MARKER
        return payload, raw_text

    def _assemble_pro_payload(self, task_id, universe, metrics, audit, sources, decision) -> dict[str, Any]:
        if not isinstance(decision, dict):
            raise DeepSeekError("Pro终审判断不是JSON对象，结果未入库。")
        regime = str(decision.get("market_regime") or "").strip()
        if regime not in {"趋势进攻", "轮动震荡", "全面防守"}:
            raise DeepSeekError("Pro终审缺少有效market_regime，结果未入库。")
        summary = str(decision.get("market_summary") or "").strip()
        if not summary:
            raise DeepSeekError("Pro终审缺少market_summary，结果未入库。")
        list_fields = (
            "candidates", "holding_actions", "avoid_list", "fund_updates", "questions_for_intraday", "risk_flags",
        )
        normalized: dict[str, list[Any]] = {}
        for field in list_fields:
            value = decision.get(field, [])
            if not isinstance(value, list):
                raise DeepSeekError(f"Pro终审字段{field}不是列表，结果未入库。")
            normalized[field] = value
        job = self.db.row("SELECT mode FROM analysis_jobs WHERE task_id=?", (task_id,)) or {}
        maximum_count = 0 if job.get("mode") == "Pro结构测试" else ACTION_CANDIDATE_COUNT
        if len(normalized["candidates"]) > maximum_count:
            raise DeepSeekError(f"Pro终审候选最多为{maximum_count}项，结果未入库。")
        pool_ids = {
            str(row.get("id") or "")
            for row in audit if str(row.get("pool_route") or "").strip()
        }
        if normalized["candidates"]:
            normalized["candidates"] = self._normalize_ranked_scores(
                normalized["candidates"], self._stored_score_map(task_id),
                allowed_ids=pool_ids, maximum_count=maximum_count,
            )
        if len(normalized["avoid_list"]) > 12:
            raise DeepSeekError("Pro终审回避清单超过12项，结果未入库。")

        known_ids = {str(row.get("index_code") or "") for row in universe}
        universe_map = {str(row.get("index_code") or ""): row for row in universe}
        audit_map = {str(row.get("id") or ""): row for row in audit}
        verified_mappings: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._strict_executable_funds(sorted(known_ids)):
            verified_mappings[(str(row.get("universe_id") or ""), str(row.get("fund_code") or ""))] = row
        policy = self._policy_for_job(task_id)
        active_limit = float(self.db.setting("active_capital_limit", "8000"))
        single_limit = float(self.db.setting("single_position_limit", "2000"))
        max_single_pct = single_limit / active_limit * 100 if active_limit > 0 else 0
        seen_candidates: set[str] = set()
        prepared_candidates: list[dict[str, Any]] = []
        for candidate in normalized["candidates"]:
            if not isinstance(candidate, dict):
                raise DeepSeekError("Pro终审候选不是JSON对象，结果未入库。")
            universe_id = str(candidate.get("universe_id") or "").strip()
            if universe_id not in known_ids:
                raise DeepSeekError(f"Pro终审候选引用未知研究项{universe_id}，结果未入库。")
            if universe_id in seen_candidates:
                raise DeepSeekError(f"Pro终审候选重复：{universe_id}，结果未入库。")
            seen_candidates.add(universe_id)
            raw_allocation = candidate.get("allocation_pct")
            if raw_allocation in (None, ""):
                raw_allocation = float(candidate.get("suggested_amount", 0) or 0) / active_limit * 100 if active_limit else 0
            allocation_pct = max(0.0, min(max_single_pct, float(raw_allocation or 0)))
            candidate.setdefault("sector_name", universe_map[universe_id].get("index_name", ""))
            candidate["index_code"] = str(
                universe_map[universe_id].get("csi_index_code")
                or universe_map[universe_id].get("ant_index_code")
                or universe_map[universe_id].get("official_index_code")
                or universe_map[universe_id].get("index_code")
                or ""
            )
            candidate["correlation_bucket"] = str(
                universe_map[universe_id].get("allocation_cluster")
                or universe_map[universe_id].get("category")
                or "未分类"
            )
            assessment = audit_map[universe_id]
            fund_code = str(candidate.get("fund_code") or "").strip()
            if fund_code and (universe_id, fund_code) not in verified_mappings:
                candidate["fund_code"] = ""
                candidate["fund_name"] = ""
            elif fund_code:
                candidate["fund_name"] = str(
                    verified_mappings[(universe_id, fund_code)].get("fund_name") or ""
                )
            executable = bool(
                int(universe_map[universe_id].get("has_buyable_fund") or 0)
                and str(candidate.get("fund_code") or "").strip()
            )
            timing_status = str(candidate.get("timing_status") or assessment.get("timing_status") or "等待回调")
            if timing_status not in {"可以买", "等待回调", "不宜介入"}:
                raise DeepSeekError(f"候选{universe_id}的timing_status无效，结果未入库。")
            freshness_ok = bool(assessment.get("freshness_ok", False))
            confidence = float(assessment.get("data_confidence") or 0)
            if not freshness_ok:
                execution_status = "行情待更新"
            elif confidence < float(policy["data_confidence_min"]):
                execution_status = "数据待补齐"
            elif not executable:
                execution_status = "基金待绑定"
            else:
                execution_status = "可执行"
            candidate.update({
                "status": str(assessment.get("research_tilt") or "观察"),
                "research_tilt": str(assessment.get("research_tilt") or "观察"),
                "timing_status": timing_status,
                "execution_status": execution_status,
                "target_allocation_pct": round(allocation_pct, 2),
                "allocation_pct": round(allocation_pct, 2),
                "suggested_amount": 0.0,
                "pool_route": str(assessment.get("pool_route") or ""),
                "opportunity_score": float(assessment.get("opportunity_score") or 0),
                "trend_score": float(assessment.get("trend_score") or 0),
                "entry_score": float(assessment.get("entry_score") or 0),
                "crowding_risk_score": float(assessment.get("crowding_risk_score") or 0),
                "data_confidence": confidence,
                "recommendation_score": float(assessment.get("recommendation_score") or 0),
                "freshness_ok": freshness_ok,
            })
            prepared_candidates.append(candidate)

        kept_candidates, suppressed_candidates = final_display_candidates(
            prepared_candidates,
            maximum_count=maximum_count,
            max_per_cluster=int(policy["display_cluster_max"]),
            minimum_score=float(policy["candidate_min_score"]),
        )
        normalized["candidates"] = allocate_candidate_amounts(self.db, kept_candidates)

        normalized["holding_actions"] = self._normalize_holding_actions(normalized["holding_actions"])

        normalized_avoid: list[dict[str, Any]] = []
        seen_avoid: set[str] = set()
        for item in normalized["avoid_list"]:
            if not isinstance(item, dict):
                raise DeepSeekError("Pro终审回避清单必须逐项返回结构化对象，不能返回字符串。")
            universe_id = str(item.get("universe_id") or "").strip()
            if universe_id not in known_ids or universe_id in seen_avoid:
                raise DeepSeekError(f"Pro终审回避项引用未知或重复板块{universe_id}，结果未入库。")
            reason = str(item.get("reason") or "").strip()
            risk_level = str(item.get("risk_level") or "").strip()
            if not reason:
                raise DeepSeekError(f"Pro终审回避项{universe_id}缺少具体原因，结果未入库。")
            if risk_level not in {"高", "中", "低"}:
                raise DeepSeekError(f"Pro终审回避项{universe_id}的风险等级无效，结果未入库。")
            seen_avoid.add(universe_id)
            source = universe_map[universe_id]
            normalized_avoid.append({
                "universe_id": universe_id,
                "index_code": str(
                    source.get("csi_index_code") or source.get("ant_index_code")
                    or source.get("official_index_code") or ""
                ).upper(),
                "sector_name": str(source.get("index_name") or ""),
                "reason": reason,
                "risk_level": risk_level,
            })
        normalized["avoid_list"] = normalized_avoid

        executable_universe_ids = {
            str(universe_id) for universe_id, _fund_code in verified_mappings
        }
        for assessment in audit:
            universe_id = str(assessment.get("id") or "")
            confidence = float(assessment.get("data_confidence") or 0)
            if not bool(assessment.get("freshness_ok", False)):
                assessment["execution_status"] = "行情待更新"
            elif confidence < float(policy["data_confidence_min"]):
                assessment["execution_status"] = "数据待补齐"
            elif universe_id not in executable_universe_ids:
                assessment["execution_status"] = "基金待绑定"
            else:
                assessment["execution_status"] = "可执行"

        selected_ids = {str(row.get("universe_id") or "") for row in normalized["candidates"]}
        suppressed_by_id = {
            str(row.get("universe_id") or ""): str(row.get("suppression_reason") or "")
            for row in suppressed_candidates
        }
        same_bucket_alternatives = []
        for universe_id in sorted(
            pool_ids - selected_ids,
            key=lambda uid: (-float(audit_map[uid].get("recommendation_score") or 0), uid),
        ):
            assessment = audit_map[universe_id]
            source = universe_map[universe_id]
            same_bucket_alternatives.append({
                "universe_id": universe_id,
                "index_code": str(
                    source.get("csi_index_code") or source.get("ant_index_code")
                    or source.get("official_index_code") or ""
                ).upper(),
                "sector_name": str(source.get("index_name") or ""),
                "correlation_bucket": str(
                    source.get("allocation_cluster") or source.get("category") or "未分类"
                ),
                "pool_route": str(assessment.get("pool_route") or ""),
                "research_tilt": str(assessment.get("research_tilt") or "观察"),
                "timing_status": str(assessment.get("timing_status") or "等待回调"),
                "execution_status": str(assessment.get("execution_status") or "不可执行"),
                "recommendation_score": float(assessment.get("recommendation_score") or 0),
                "suppression_reason": suppressed_by_id.get(universe_id, "Pro未选入最终主榜"),
            })

        category_counts: dict[str, int] = {}
        for row in universe:
            category = str(row.get("category") or "")
            category_counts[category] = category_counts.get(category, 0) + 1
        cutoffs = [str(row.get("data_cutoff") or "").strip() for row in metrics]
        data_cutoff = max((value for value in cutoffs if value), default=local_now().isoformat(timespec="minutes"))
        no_buy_reason = str(decision.get("no_buy_reason") or "").strip()
        if not any(float(row.get("suggested_amount") or 0) > 0 for row in normalized["candidates"]):
            if not no_buy_reason:
                no_buy_reason = (
                    "本轮没有同时通过研究档位、入场时机、行情新鲜度、数据置信度、"
                    "基金可执行性与仓位硬门槛的正金额候选。"
                )
        payload = {
            "protocol_version": "3.0",
            "task_kind": "night",
            "task_id": task_id,
            "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": data_cutoff,
            "market_regime": regime,
            "universe_total": len(universe),
            "analyzed_total": len(universe),
            "coverage_by_category": [
                {"category": category, "expected": count, "analyzed": count}
                for category, count in category_counts.items()
            ],
            "research_method": (
                "软件以最近已完成交易日的1/3/5/10/20日硬行情、量能、价格区间位置，以及可得时的真实"
                "成分股扩散度为基础；没有可核验成分表时明确显示扩散度缺失，绝不使用指数涨跌代替。软件本地按冻结策略"
                "计算趋势机会、入场性价比、拥挤风险和数据置信度，先经三路构建20—30项质量池；"
                "Pro只能在池内自由选择不超过10项并补充判断，不能改分或在池外发明指数。"
            ),
            "ranking_policy_version": policy["version"],
            "ranking_policy": policy,
            "tool_calls_summary": [
                {"tool": "本地行情管线", "purpose": "五周期行情、量能、位置、可得时的真实扩散度及缺失标记、基础评分", "items": len(universe)},
                {"tool": "DeepSeek V4 Flash", "purpose": "逐项联网证据检索", "items": len(universe)},
                {"tool": "DeepSeek V4 Pro", "purpose": "市场状态、候选与风险终审", "items": len(universe)},
            ],
            "audit": audit,
            "market_summary": summary,
            "action_summary": str(decision.get("action_summary") or summary),
            "no_buy_reason": no_buy_reason,
            "candidates": normalized["candidates"],
            "same_bucket_alternatives": same_bucket_alternatives,
            "holding_actions": normalized["holding_actions"],
            "avoid_list": normalized["avoid_list"],
            "fund_updates": normalized["fund_updates"],
            "questions_for_intraday": normalized["questions_for_intraday"],
            "risk_flags": normalized["risk_flags"],
            "sources": sources,
        }
        return payload

    def _normalize_holding_actions(self, actions: list[dict[str, Any]], *, require_all: bool = False) -> list[dict[str, Any]]:
        holdings = {str(row["fund_code"]): row for row in active_capital_holdings(self.db)}
        aliases = {
            "HOLD": "持有", "持有": "持有", "CONTINUE_DCA": "继续定投", "继续定投": "继续定投",
            "REDUCE": "减仓", "减仓": "减仓", "SELL_ALL": "清仓", "清仓": "清仓",
        }
        normalized = []
        seen: set[str] = set()
        for item in actions:
            if not isinstance(item, dict):
                raise DeepSeekError("Pro持仓动作不是JSON对象，结果未入库。")
            code = str(item.get("fund_code") or "").strip()
            if code not in holdings or code in seen:
                raise DeepSeekError(f"Pro持仓动作引用未知或重复基金{code}，结果未入库。")
            action = aliases.get(str(item.get("action") or "").strip())
            if not action:
                raise DeepSeekError(f"基金{code}的持仓动作无效，结果未入库。")
            try:
                sell_pct = float(item.get("sell_pct", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise DeepSeekError(f"基金{code}的卖出比例不是数字，结果未入库。") from exc
            if action in {"持有", "继续定投"}:
                sell_pct = 0.0
            elif action == "清仓":
                sell_pct = 100.0
            else:
                sell_pct = max(1.0, min(99.0, sell_pct))
            seen.add(code)
            normalized.append({
                "fund_code": code, "fund_name": holdings[code].get("fund_name", ""),
                "action": action, "sell_pct": round(sell_pct, 2),
                "reason": str(item.get("reason") or "").strip(),
                "risk": str(item.get("risk") or "").strip(),
            })
        if require_all and seen != set(holdings):
            missing = "、".join(sorted(set(holdings) - seen))
            raise DeepSeekError(f"Pro持仓动作未覆盖全部当前持仓：{missing}，结果未入库。")
        return normalized

    def _seed_simulated_metrics(self, task_id, universe):
        for item in universe:
            seed = int(hashlib.sha256(item["index_code"].encode()).hexdigest()[:8], 16)
            values = [round(((seed >> shift) % 1201 - 600) / 100, 2) for shift in (0, 3, 6, 9, 12)]
            self.db.execute(
                """INSERT OR REPLACE INTO sector_metrics(task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                   volume_signal,breadth_signal,position_20d,data_source,is_simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (task_id, item["index_code"], f"SIM-{item['index_code']}", item["index_name"], *values, "模拟量能", "模拟扩散", "模拟位置", "SIMULATION_ONLY"),
            )
        return self.db.rows("SELECT * FROM sector_metrics WHERE task_id=? ORDER BY universe_id", (task_id,))

    @staticmethod
    def _stable_request_key(phase: str, logical_request: Any) -> str:
        canonical = json.dumps(
            logical_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return f"{phase}:{digest}"

    @staticmethod
    def _request_fingerprint(request_payload: Any) -> str:
        canonical = json.dumps(
            request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _model_call_for_request(
        self,
        task_id: str,
        request_key: str,
        *,
        legacy_phase: str = "",
        legacy_stage: str = "",
    ) -> dict[str, Any] | None:
        row = self.db.row(
            "SELECT * FROM model_calls WHERE task_id=? AND request_key=? ORDER BY id DESC LIMIT 1",
            (task_id, request_key),
        )
        if row:
            return row
        if not legacy_phase and not legacy_stage:
            return None
        for candidate in self.db.rows(
            "SELECT * FROM model_calls WHERE task_id=? ORDER BY id DESC", (task_id,)
        ):
            if legacy_stage and str(candidate.get("stage") or "") == legacy_stage:
                return candidate
            try:
                context = json.loads(candidate.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(context, dict) and str(context.get("phase") or "") == legacy_phase:
                return candidate
        return None

    def _model_result_from_call(self, row: dict[str, Any]) -> ModelResult:
        try:
            raw = json.loads(row.get("response_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeepSeekError("本地已付费响应JSON已损坏；为避免重复扣费，软件不会自动重发。") from exc
        if not isinstance(raw, dict) or not raw:
            raise DeepSeekError("本地已付费响应为空；为避免重复扣费，软件不会自动重发。")
        response_kind = str(row.get("response_kind") or "")
        choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if response_kind.startswith("chat") or choices:
            text = str(message.get("content") or "").strip()
            finish_reason = str(row.get("finish_reason") or choice.get("finish_reason") or "")
            response_kind = "chat"
        else:
            text = self._responses_text(raw)
            finish_reason = str(row.get("finish_reason") or "")
            response_kind = response_kind or "responses"
        return ModelResult(
            text=text,
            raw=raw,
            model=str(row.get("model") or raw.get("model") or ""),
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            cached_tokens=int(row.get("cached_tokens") or 0),
            reasoning_tokens=int(row.get("reasoning_tokens") or 0),
            web_search_calls=int(row.get("web_search_calls") or 0),
            estimated_cost_cny=float(row.get("estimated_cost_cny") or 0),
            response_kind=response_kind,
            finish_reason=finish_reason,
        )

    def _recover_model_result(
        self,
        task_id: str,
        request_key: str,
        *,
        request_fingerprint: str = "",
        legacy_phase: str = "",
        legacy_stage: str = "",
    ) -> ModelResult | None:
        row = self._model_call_for_request(
            task_id, request_key, legacy_phase=legacy_phase, legacy_stage=legacy_stage
        )
        if not row:
            return None
        try:
            context = json.loads(row.get("request_context_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            context = {}
        saved_fingerprint = str(context.get("request_fingerprint") or "") if isinstance(context, dict) else ""
        if request_fingerprint and saved_fingerprint and saved_fingerprint != request_fingerprint:
            raise DeepSeekError(
                "同一任务的已审计请求与当前输入指纹不同；为避免混用数据或重复扣费，"
                "软件拒绝续发，请新建研究任务。"
            )
        status = str(row.get("response_status") or "").strip().lower()
        if status in {"reserved_before_send", "transport_uncertain"}:
            raise DeepSeekError(
                "该付费请求已经预留且发送状态不明，服务端可能已经计费；"
                "软件禁止自动重发。请导出任务诊断并核对账单。"
            )
        if status in {"failed", "cancelled", "incomplete"}:
            raise DeepSeekError(
                f"该付费请求已有不可重发的审计状态“{status}”；请新建任务，软件不会自动重复发送。"
            )
        return self._model_result_from_call(row)

    def _record_model_call(
        self,
        task_id: str,
        stage: str,
        result: ModelResult,
        *,
        context: dict[str, Any] | None = None,
    ):
        response_status = str(result.raw.get("status") or "")
        response_id = str(result.raw.get("id") or "")
        self.db.execute(
            """INSERT INTO model_calls(task_id,stage,model,input_tokens,output_tokens,cached_tokens,reasoning_tokens,
               web_search_calls,estimated_cost_cny,response_kind,response_status,finish_reason,response_id,response_json,
               request_context_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, stage, result.model, result.input_tokens, result.output_tokens, result.cached_tokens,
             result.reasoning_tokens, result.web_search_calls, result.estimated_cost_cny, result.response_kind,
             response_status, result.finish_reason, response_id,
             json.dumps(result.raw, ensure_ascii=False), json.dumps(context or {}, ensure_ascii=False),
             local_now().isoformat(timespec="seconds")),
        )
        self._set_job(task_id, cost=self._job_cost(task_id))

    @staticmethod
    def _projected_call_cost(
        input_characters: int,
        max_output_tokens: int,
        input_price_per_million: float,
        output_price_per_million: float,
    ) -> float:
        """Conservative pre-send reservation used by every billable Flash call."""
        projected_input_tokens = max(1, math.ceil(max(0, input_characters) * 1.2))
        return max(
            0.0001,
            projected_input_tokens * float(input_price_per_million) / 1_000_000
            + max(0, int(max_output_tokens)) * float(output_price_per_million) / 1_000_000,
        )

    def _reserve_model_call(
        self,
        task_id: str,
        stage: str,
        model: str,
        projected_cost_cny: float,
        *,
        request_key: str,
        context: dict[str, Any] | None = None,
    ) -> int:
        reservation = {
            "request_state": "reserved_before_send",
            "notice": "发送前按上限预留；成功后替换为服务端真实Token费用，传输状态不明时保留。",
        }
        if not str(request_key or "").strip():
            raise ValueError("付费请求必须具有稳定幂等键。")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id,response_status FROM model_calls WHERE task_id=? AND request_key=?",
                (task_id, request_key),
            ).fetchone()
            if existing:
                raise DeepSeekError(
                    "同一付费请求已经存在本地审计记录；软件已阻止重复发送。"
                    "请续跑以重放已完成响应，或导出诊断核对状态不明的请求。"
                )
            cursor = conn.execute(
                """INSERT INTO model_calls(task_id,stage,model,input_tokens,output_tokens,cached_tokens,reasoning_tokens,
                   web_search_calls,estimated_cost_cny,response_kind,response_status,finish_reason,response_id,response_json,
                   request_context_json,request_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, stage, model, 0, 0, 0, 0, 0, float(projected_cost_cny), "chat-stream",
                 "reserved_before_send", "", "", json.dumps(reservation, ensure_ascii=False),
                 json.dumps(context or {}, ensure_ascii=False), request_key,
                 local_now().isoformat(timespec="seconds")),
            )
            call_id = int(cursor.lastrowid)
        self._set_job(task_id, cost=self._job_cost(task_id))
        return call_id

    def _mark_reserved_model_call_uncertain(self, call_id: int, exc: Exception):
        safe_error = str(exc)[:1600]
        self.db.execute(
            "UPDATE model_calls SET response_status=?,response_json=? WHERE id=?",
            ("transport_uncertain", json.dumps({
                "request_state": "transport_uncertain",
                "error": safe_error,
                "notice": "费用上限预留不会自动释放；必须人工核对后再决定是否重试。",
            }, ensure_ascii=False), int(call_id)),
        )

    def _finalize_reserved_model_call(self, call_id: int, result: ModelResult):
        response_status = str(result.raw.get("status") or "completed")
        response_id = str(result.raw.get("id") or "")
        self.db.execute(
            """UPDATE model_calls SET model=?,input_tokens=?,output_tokens=?,cached_tokens=?,reasoning_tokens=?,
               web_search_calls=?,estimated_cost_cny=?,response_kind=?,response_status=?,finish_reason=?,response_id=?,
               response_json=? WHERE id=?""",
            (result.model, result.input_tokens, result.output_tokens, result.cached_tokens, result.reasoning_tokens,
             result.web_search_calls, result.estimated_cost_cny, result.response_kind, response_status,
             result.finish_reason, response_id, json.dumps(result.raw, ensure_ascii=False), int(call_id)),
        )

    def _job_cost(self, task_id: str) -> float:
        row = self.db.row("SELECT COALESCE(SUM(estimated_cost_cny),0) AS cost FROM model_calls WHERE task_id=?", (task_id,))
        return float(row["cost"] if row else 0)

    def _set_job(self, task_id: str, *, stage=None, status=None, completed=None, cost=None, error=None):
        now = local_now()
        values = {"updated_at": now.isoformat(timespec="seconds")}
        if stage is not None: values["stage"] = stage
        if status is not None: values["status"] = status
        if completed is not None: values["completed_items"] = int(completed)
        if cost is not None: values["estimated_cost_cny"] = float(cost)
        if error is not None: values["error"] = error
        lease_task = getattr(self._lease_context, "task_id", None)
        lease_owner = getattr(self._lease_context, "owner", None)
        if lease_task == task_id and lease_owner:
            values["lease_heartbeat_at"] = now.isoformat(timespec="seconds")
            values["lease_expires_at"] = (
                now + timedelta(seconds=LEASE_TTL_SECONDS)
            ).isoformat(timespec="seconds")
            sql = (
                "UPDATE analysis_jobs SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE task_id=? AND lease_owner=?"
            )
            with self.db.connect() as conn:
                cursor = conn.execute(sql, (*values.values(), task_id, lease_owner))
                if cursor.rowcount != 1:
                    raise DeepSeekError("任务运行租约已经丢失；软件已停止后续API请求，避免重复扣费。")
            return
        sql = "UPDATE analysis_jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE task_id=?"
        self.db.execute(sql, (*values.values(), task_id))

    def _event(self, task_id: str, level: str, stage: str, message: str):
        self.db.execute(
            "INSERT INTO analysis_events(task_id,created_at,level,stage,message) VALUES (?,?,?,?,?)",
            (task_id, local_now().isoformat(timespec="seconds"), level, stage, message),
        )

    @staticmethod
    def _notify(callback, stage, completed, total, message):
        if callback:
            callback(stage, completed, total, message)

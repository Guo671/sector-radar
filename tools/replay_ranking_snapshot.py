"""Read-only replay of the current ranking formula against a completed scan.

This verifier deliberately bypasses ``Database`` so opening an older formal
database cannot run a migration.  It is safe to point at the daily database:
the file is opened with SQLite ``mode=ro`` and its SHA-256 must be identical
before and after the replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranking import (  # noqa: E402
    RANKING_FORMULA_VERSION,
    build_scorecards,
    select_candidate_pool,
)


AI_CHAIN_MARKERS = (
    "人工智能", "AI", "云计算", "软件", "数据", "算力", "服务器",
    "通信", "光通信", "半导体", "芯片", "电子", "机器人",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def readonly_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def latest_completed_task(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM analysis_jobs
         WHERE mode='完整扫描' AND status='已完成'
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("数据库中没有已完成的完整扫描，无法回放")
    return row


def replay(database: Path, task_id: str = "") -> dict[str, Any]:
    before = file_hash(database)
    connection = sqlite3.connect(readonly_uri(database), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        job = (
            connection.execute(
                "SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,),
            ).fetchone()
            if task_id
            else latest_completed_task(connection)
        )
        if job is None:
            raise RuntimeError(f"找不到任务：{task_id}")
        task_id = str(job["task_id"])
        metrics = [
            dict(row) for row in connection.execute(
                "SELECT * FROM sector_metrics WHERE task_id=? ORDER BY universe_id",
                (task_id,),
            )
        ]
        evidence_rows = [
            dict(row) for row in connection.execute(
                "SELECT * FROM sector_evidence WHERE task_id=? ORDER BY universe_id",
                (task_id,),
            )
        ]
        if not metrics:
            raise RuntimeError(f"任务{task_id}没有行情快照")
        evidence = {str(row["universe_id"]): row for row in evidence_rows}
        if set(evidence) != {str(row["universe_id"]) for row in metrics}:
            raise RuntimeError("行情与Flash证据集合不一致，拒绝用残缺快照回放")
        try:
            policy = json.loads(job["ranking_policy_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("任务冻结评分策略不是有效JSON") from exc
        expected_cutoffs = {
            str(row.get("expected_data_cutoff") or row.get("data_cutoff") or "")
            for row in metrics
        }
        if len(expected_cutoffs) != 1:
            raise RuntimeError(f"同一任务出现多个行情截止日：{sorted(expected_cutoffs)}")
        expected_cutoff = expected_cutoffs.pop()
        cards = build_scorecards(
            metrics,
            evidence,
            expected_cutoff=expected_cutoff,
            policy=policy,
        )
        catalog_rows = [
            dict(row) for row in connection.execute(
                """
                SELECT universe_id,official_index_code,official_index_name,
                       allocation_cluster
                  FROM investable_indices
                 WHERE universe_id IN (
                       SELECT universe_id FROM sector_metrics WHERE task_id=?
                 )
                """,
                (task_id,),
            )
        ]
        catalog = {str(row["universe_id"]): row for row in catalog_rows}
        clusters = {
            universe_id: str(row.get("allocation_cluster") or universe_id)
            for universe_id, row in catalog.items()
        }
        pool = select_candidate_pool(
            cards,
            clusters,
            route_size=int(policy.get("pool_route_size", 10) or 10),
            minimum_size=int(policy.get("pool_min", 20) or 20),
            maximum_size=int(policy.get("pool_max", 30) or 30),
            max_per_cluster=int(policy.get("pool_cluster_max", 4) or 4),
            minimum_score=float(policy.get("candidate_min_score", 45.0) or 45.0),
        )
        old_rows = [
            dict(row) for row in connection.execute(
                """
                SELECT universe_id,recommendation_score,research_tilt
                  FROM radar_audit WHERE task_id=?
                """,
                (task_id,),
            )
        ]
        old_scores = [float(row["recommendation_score"] or 0) for row in old_rows]
        new_scores = [card.recommendation_score for card in cards.values()]
        pool_ids = {str(row["universe_id"]) for row in pool}
        ai_rows = []
        for universe_id, card in cards.items():
            name = str(catalog.get(universe_id, {}).get("official_index_name") or universe_id)
            if not any(marker.casefold() in name.casefold() for marker in AI_CHAIN_MARKERS):
                continue
            ai_rows.append({
                "universe_id": universe_id,
                "index_code": catalog.get(universe_id, {}).get("official_index_code") or "",
                "name": name,
                "recommendation_score": card.recommendation_score,
                "research_tilt": card.research_tilt,
                "timing_status": card.timing_status,
                "crowding_risk": card.crowding_risk,
                "in_pool": universe_id in pool_ids,
            })
        ai_rows.sort(key=lambda row: (-float(row["recommendation_score"]), row["universe_id"]))
        result = {
            "database": str(database.resolve()),
            "task_id": task_id,
            "source_policy_version": str(job["ranking_policy_version"] or ""),
            "replay_policy_version": RANKING_FORMULA_VERSION,
            "expected_data_cutoff": expected_cutoff,
            "metric_rows": len(metrics),
            "evidence_rows": len(evidence_rows),
            "old": {
                "minimum": min(old_scores) if old_scores else None,
                "mean": (sum(old_scores) / len(old_scores)) if old_scores else None,
                "maximum": max(old_scores) if old_scores else None,
                "tiers": dict(Counter(str(row["research_tilt"] or "") for row in old_rows)),
            },
            "replay": {
                "minimum": min(new_scores),
                "mean": sum(new_scores) / len(new_scores),
                "maximum": max(new_scores),
                "tiers": dict(Counter(card.research_tilt for card in cards.values())),
                "hard_avoid_count": sum(card.hard_avoid for card in cards.values()),
            },
            "candidate_pool": {
                "count": len(pool),
                "routes": dict(Counter(str(row["pool_route"]) for row in pool)),
                "distinct_clusters": len({str(row["correlation_bucket"]) for row in pool}),
                "hard_avoid_count": sum(cards[row["universe_id"]].hard_avoid for row in pool),
            },
            "ai_supply_chain": ai_rows,
        }
    finally:
        connection.close()
    after = file_hash(database)
    result["database_sha256_before"] = before
    result["database_sha256_after"] = after
    result["database_unchanged"] = before == after
    if before != after:
        raise RuntimeError("只读回放前后数据库哈希变化，验收失败")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = replay(args.database, args.task_id)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

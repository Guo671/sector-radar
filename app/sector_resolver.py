from __future__ import annotations

import json
import math
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import local_now


MAX_RESPONSE_BYTES = 6 * 1024 * 1024
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


class SectorResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchAlias:
    term: str
    desired_name: str
    confidence: float
    rationale: str


CURATED_SEARCH_ALIASES: dict[str, SearchAlias] = {
    "U063": SearchAlias("钢铁", "钢铁", .86, "钢铁冶炼使用钢铁板块作透明行业代理。"),
    "U079": SearchAlias("中药", "中药Ⅱ", .96, "目标为申万中药细分行业。"),
    "U094": SearchAlias("保险", "保险Ⅱ", .96, "目标为申万保险细分行业。"),
    "U108": SearchAlias("CPO", "CPO概念", .94, "CPO与光模块使用CPO概念板块。"),
    "U126": SearchAlias("消费电子", "消费电子", .92, "消费电子主题使用同名板块或指数。"),
    "U132": SearchAlias("软件开发", "软件开发", .90, "软件服务使用软件开发板块作透明代理。"),
    "U134": SearchAlias("传媒", "传媒", .86, "传媒娱乐使用传媒板块作透明代理。"),
    "U136": SearchAlias("CRO", "CRO", .94, "CRO与医药外包使用CRO板块。"),
    "U137": SearchAlias("医疗器械", "医疗器械", .92, "医疗器械主题使用同名板块。"),
    "U139": SearchAlias("人脑工程", "人脑工程", .86, "脑机接口使用人脑工程概念作透明代理。"),
    "U140": SearchAlias("新能源车", "新能源车", .94, "新能源汽车使用新能源车板块。"),
    "U142": SearchAlias("光伏设备", "光伏设备", .92, "光伏使用光伏设备板块。"),
    "U143": SearchAlias("风电设备", "风电设备", .92, "风电使用风电设备板块，以取得可审计成分股快照。"),
    "U144": SearchAlias("储能", "储能概念", .90, "储能使用储能概念板块以取得成分股宽度。"),
    "U145": SearchAlias("红利指数", "红利指数", .94, "红利风格使用上证红利指数。"),
    "U146": SearchAlias("红利指数", "红利指数", .84, "红利低波免费精确历史行情不可用，使用可核验的上证红利指数作透明代理。"),
    "U149": SearchAlias("红利指数", "红利指数", .86, "高股息免费精确历史行情不可用，使用可核验的上证红利指数作透明代理。"),
    "U150": SearchAlias("价值股", "价值股", .82, "价值风格使用价值股板块作透明代理。"),
    "U151": SearchAlias("成长股", "高成长股", .82, "成长风格使用高成长股板块作透明代理。"),
    "U152": SearchAlias("沪深300", "沪深300", .76, "质量优势免费精确历史行情不可用，使用大盘核心资产宽基作低置信透明代理。"),
    "U153": SearchAlias("创业低波", "创业低波", .80, "通用低波动使用可核验的创业板低波动指数作透明代理。"),
    "U154": SearchAlias("小盘股", "小盘股", .84, "小盘风格使用小盘股板块。"),
    "U155": SearchAlias("大盘股", "大盘股", .84, "大盘风格使用大盘股板块。"),
    "U156": SearchAlias("微盘股", "微盘股", .90, "微盘风格使用微盘股板块。"),
    "U157": SearchAlias("高贝塔", "创业高贝", .80, "高贝塔使用创业板高贝塔指数作透明代理。"),
    "U158": SearchAlias("ESG", "ESG100", .84, "ESG使用ESG100指数作透明代理。"),
    "U159": SearchAlias("央企创新", "央企创新", .80, "央企科技引领免费精确历史行情不可用，使用央企创新指数作透明代理。"),
    "U160": SearchAlias("央企创新", "央企创新", .86, "央企创新驱动使用央企创新指数。"),
    "U164": SearchAlias("贵金属", "贵金属", .84, "白银独立板块成分过少，使用贵金属行业板块作透明产业代理。"),
    "U167": SearchAlias("小金属", "小金属", .82, "稀土独立板块成分过少，使用小金属行业板块作透明产业代理。"),
    "U169": SearchAlias("能源金属", "能源金属", .82, "钴独立板块成分过少，使用能源金属行业板块作透明产业代理。"),
    "U170": SearchAlias("能源金属", "能源金属", .82, "镍独立板块成分过少，使用能源金属行业板块作透明产业代理。"),
    "U171": SearchAlias("核能核电", "核能核电", .78, "铀矿缺少境内精确指数，使用核能核电产业链作低置信透明代理。"),
    "U172": SearchAlias("油气资源", "油气资源", .90, "油气方向使用同名油气资源板块，以取得可审计成分股快照。"),
    "U173": SearchAlias("煤炭", "煤炭", .82, "煤炭商品映射使用煤炭股票板块。"),
    "U174": SearchAlias("钢铁", "钢铁", .82, "钢铁商品映射使用钢铁股票板块。"),
    "U175": SearchAlias("基础化工", "基础化工", .82, "化工商品映射使用基础化工股票板块。"),
    "U176": SearchAlias("粮食", "粮食概念", .82, "粮食农业使用粮食概念板块。"),
    "U177": SearchAlias("生猪", "生猪养殖", .88, "猪产业使用生猪养殖板块。"),
    "U179": SearchAlias("深证成指", "深证成指", .76, "深圳国企免费精确历史行情不可用，使用深圳市场宽基作低置信区域代理。"),
    "U180": SearchAlias("粤港澳", "粤港自贸", .82, "粤港澳大湾区使用粤港自贸板块作透明代理。"),
    "U183": SearchAlias("成渝特区", "成渝特区", .86, "成渝经济圈使用成渝特区板块，以取得可审计成分股快照。"),
    "U184": SearchAlias("海南自贸", "海南自贸", .90, "海南自贸区使用海南自贸板块。"),
    "U185": SearchAlias("新疆板块", "新疆板块", .88, "新疆振兴使用新疆地域板块。"),
    "U186": SearchAlias("西藏板块", "西藏板块", .88, "西藏振兴使用西藏地域板块。"),
}


INDEX_COMPONENT_TYPES = {
    "U004": 4, "U005": 10, "U006": 1, "U007": 3,
    "U009": 7, "U010": 13, "U011": 5, "U012": 2,
}


def normalize_board_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(概念|主题|产业|商品映射)$", "", text)
    text = re.sub(r"[ⅡⅢⅣⅤ]+$", "", text)
    return re.sub(r"[与和、·\s\-_（）()]", "", text)


def _system_curl() -> str | None:
    native = Path(os.getenv("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if native.is_file():
        return str(native)
    return shutil.which("curl.exe") or shutil.which("curl")


def download_json(url: str, label: str, timeout_seconds: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SectorRadar/2.0",
    })
    failures: list[str] = []
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds, context=ssl.create_default_context()) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SectorResolveError(f"{label}响应超过6MB安全上限。")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise SectorResolveError(f"{label}返回类型异常。")
            return payload
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            failures.append(f"Python HTTPS第{attempt + 1}次：{getattr(exc, 'reason', exc)}")
            if attempt == 0:
                time.sleep(.25)
    curl = _system_curl()
    if curl:
        command = [
            curl, "--fail", "--silent", "--show-error", "--location", "--ipv4",
            "--connect-timeout", str(min(10, timeout_seconds)), "--max-time", str(timeout_seconds),
            "--max-filesize", str(MAX_RESPONSE_BYTES), "--header", "Accept-Encoding: identity",
            "--user-agent", "Mozilla/5.0 SectorRadar/2.0", url,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=timeout_seconds + 5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
            if completed.returncode == 0 and completed.stdout:
                payload = json.loads(completed.stdout.decode("utf-8"))
                if isinstance(payload, dict):
                    return payload
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
            failures.append(f"系统curl：{detail or '空响应'}")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            failures.append(f"系统curl：{exc}")
    raise SectorResolveError(f"{label}下载失败：{'；'.join(failures[-4:])}")


class SectorMappingResolver:
    endpoint = "https://searchapi.eastmoney.com/api/suggest/get"

    def __init__(self, db, requester: Callable[[str, str, int], dict[str, Any]] | None = None):
        self.db = db
        self.timeout_seconds = int(float(db.setting("market_timeout_seconds", "20")))
        self.requester = requester or download_json

    def refresh(
        self,
        universe: list[dict[str, Any]] | None = None,
        *,
        force: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        universe = universe or self.db.rows("SELECT * FROM universe ORDER BY index_code")
        existing = {
            row["universe_id"]: row for row in self.db.rows(
                "SELECT * FROM proxy_mappings WHERE active=1 AND confidence>=.75 AND mapping_kind<>'待核验'"
            )
        }
        resolved = skipped = 0
        errors: list[str] = []
        unresolved: list[str] = []
        stopped = False
        for index, item in enumerate(universe, start=1):
            if stop_requested and stop_requested():
                stopped = True
                break
            uid = item["index_code"]
            if not force and uid in existing:
                skipped += 1
                if progress:
                    progress(index, len(universe), f"{uid} 已有可信映射")
                continue
            try:
                mapping = self._resolve_one(item)
                if mapping is None:
                    unresolved.append(f"{uid} {item['index_name']}")
                else:
                    self._save(mapping)
                    resolved += 1
            except Exception as exc:
                errors.append(f"{uid} {item['index_name']}：{exc}")
            if progress:
                progress(index, len(universe), f"{uid} {item['index_name']}")
        coverage = self.db.rows(
            "SELECT universe_id FROM proxy_mappings WHERE active=1 AND confidence>=.75 AND mapping_kind<>'待核验'"
        )
        covered = len({row["universe_id"] for row in coverage})
        now = local_now().isoformat(timespec="seconds")
        settings = {
            "mapping_last_refresh_attempt_at": now,
            "mapping_last_refresh_attempt_status": "已停止" if stopped else "已完成",
            "mapping_last_refresh_error": "\n".join(errors + unresolved)[:5000],
        }
        if not stopped:
            settings["mapping_last_refresh_at"] = now
        self.db.save_settings(settings)
        return {
            "total": len(universe), "resolved": resolved, "skipped": skipped,
            "covered": covered, "errors": errors, "unresolved": unresolved,
            "stopped": stopped,
        }

    def _resolve_one(self, item: dict[str, Any]) -> dict[str, Any] | None:
        uid = item["index_code"]
        target = item["index_name"]
        category = item["category"]
        alias = CURATED_SEARCH_ALIASES.get(uid)
        term = alias.term if alias else target
        query = urllib.parse.urlencode({
            "input": term, "type": "14", "token": SEARCH_TOKEN, "count": "30",
        })
        url = f"{self.endpoint}?{query}"
        payload = self.requester(url, f"{uid}板块搜索", self.timeout_seconds)
        rows = (((payload.get("QuotationCodeTable") or {}).get("Data")) or [])
        candidates = [
            row for row in rows
            if row.get("SecurityTypeName") in {"板块", "指数"}
            and re.fullmatch(r"\d{1,3}\.(?:\d{6}|BK\d{4}|[A-Z][A-Z0-9]{3,11})", str(row.get("QuoteID") or ""))
        ]
        chosen = None
        if alias:
            exact = [row for row in candidates if row.get("Name") == alias.desired_name]
            chosen = self._prefer_candidate(exact, category)
        else:
            exact = [row for row in candidates if row.get("Name") == target]
            chosen = self._prefer_candidate(exact, category)
            if chosen is None:
                normalized = [row for row in candidates if normalize_board_name(row.get("Name", "")) == normalize_board_name(target)]
                if len({row.get("Name") for row in normalized}) == 1:
                    chosen = self._prefer_candidate(normalized, category)
        if chosen is None:
            return None
        is_alias = alias is not None
        security_type = chosen.get("SecurityTypeName")
        mapping_kind = "显式代理" if is_alias else ("精确板块" if security_type == "板块" else "精确指数")
        confidence = alias.confidence if alias else (1.0 if chosen.get("Name") == target else .96)
        rationale = alias.rationale if alias else (
            f"东方财富证券搜索返回唯一{'同名' if chosen.get('Name') == target else '规范化同名'}{security_type}；"
            "保存代码、名称和类型供后续审计。"
        )
        return {
            "universe_id": uid,
            "provider": "eastmoney_public",
            "security_id": chosen["QuoteID"],
            "proxy_code": chosen.get("UnifiedCode") or chosen.get("Code") or chosen["QuoteID"].split(".", 1)[1],
            "proxy_name": chosen.get("Name") or target,
            "mapping_kind": mapping_kind,
            "confidence": confidence,
            "rationale": rationale,
        }

    @staticmethod
    def _prefer_candidate(candidates: list[dict[str, Any]], category: str) -> dict[str, Any] | None:
        if not candidates:
            return None
        if category != "宽基":
            board = next((row for row in candidates if row.get("SecurityTypeName") == "板块"), None)
            if board:
                return board
        return candidates[0]

    def _save(self, mapping: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT INTO proxy_mappings(
                   universe_id,provider,security_id,proxy_code,proxy_name,mapping_kind,
                   confidence,rationale,active,reviewed_at
               ) VALUES(?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(universe_id) DO UPDATE SET
                   provider=excluded.provider,security_id=excluded.security_id,
                   proxy_code=excluded.proxy_code,proxy_name=excluded.proxy_name,
                   mapping_kind=excluded.mapping_kind,confidence=excluded.confidence,
                   rationale=excluded.rationale,active=1,reviewed_at=excluded.reviewed_at""",
            (
                mapping["universe_id"], mapping["provider"], mapping["security_id"],
                mapping["proxy_code"], mapping["proxy_name"], mapping["mapping_kind"],
                mapping["confidence"], mapping["rationale"], local_now().isoformat(timespec="seconds"),
            ),
        )


class ConstituentBreadthProvider:
    """Calculate current advance/decline breadth for boards and key broad indices."""

    def __init__(self, requester: Callable[[str, str, int], dict[str, Any]] | None = None, timeout_seconds: int = 20):
        self.requester = requester or download_json
        self.timeout_seconds = max(5, int(timeout_seconds))

    def supports(self, universe_id: str, security_id: str) -> bool:
        return bool(re.fullmatch(r"90\.BK\d{4}", security_id)) or universe_id in INDEX_COMPONENT_TYPES

    def fetch(self, universe_id: str, security_id: str, data_cutoff: str) -> dict[str, Any] | None:
        if re.fullmatch(r"90\.BK\d{4}", security_id):
            board_code = security_id.split(".", 1)[1]
            query = urllib.parse.urlencode({
                "pn": "1", "pz": "500", "po": "1", "np": "1", "fltt": "2", "invt": "2",
                "fid": "f3", "fs": f"b:{board_code}+f:!50",
                "fields": "f2,f3,f12,f14,f109,f110,f127,f160,f124",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            })
            url = f"https://push2delay.eastmoney.com/api/qt/clist/get?{query}"
            payload = self.requester(url, f"{universe_id}成分股", self.timeout_seconds)
            rows = ((payload.get("data") or {}).get("diff")) or []
            source = "eastmoney_board_constituents"
        elif universe_id in INDEX_COMPONENT_TYPES:
            type_id = INDEX_COMPONENT_TYPES[universe_id]
            params = {
                "reportName": "RPT_INDEX_TS_COMPONENT",
                "columns": "SECUCODE,SECURITY_CODE,TYPE,SECURITY_NAME_ABBR,CLOSE_PRICE",
                "quoteColumns": "f2,f3", "quoteType": "0", "source": "WEB", "client": "WEB",
                "filter": f'(TYPE="{type_id}")', "pageNumber": "1", "pageSize": "500",
                "sortColumns": "SECURITY_CODE", "sortTypes": "-1",
            }
            url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
            payload = self.requester(url, f"{universe_id}指数成分股", self.timeout_seconds)
            rows = (((payload.get("result") or {}).get("data")) or [])
            source = "eastmoney_index_components"
        else:
            return None
        changes: list[float] = []
        horizon_fields = {"d1": "f3", "d3": "f127", "d5": "f109", "d10": "f160", "d20": "f110"}
        horizon_values: dict[str, list[float]] = {key: [] for key in horizon_fields}
        timestamps: list[int] = []
        for row in rows:
            value = row.get("f3")
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                changes.append(number)
            if source == "eastmoney_board_constituents":
                for key, field in horizon_fields.items():
                    try:
                        horizon = float(row.get(field))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(horizon):
                        horizon_values[key].append(horizon)
                try:
                    timestamp = int(row.get("f124"))
                except (TypeError, ValueError):
                    timestamp = 0
                if timestamp > 0:
                    timestamps.append(timestamp)
        if len(changes) < 5:
            raise SectorResolveError(f"{universe_id}有效成分股涨跌幅仅{len(changes)}只。")
        advances = sum(value > 0 for value in changes)
        declines = sum(value < 0 for value in changes)
        flat = len(changes) - advances - declines
        strong = sum(value >= 2 for value in changes)
        ratio = advances / len(changes) * 100
        average = sum(changes) / len(changes)
        result = {
            "breadth_signal": (
                f"成分股上涨{advances}/{len(changes)}（{ratio:.1f}%），下跌{declines}，平盘{flat}，"
                f"涨幅≥2%共{strong}只，平均{average:+.2f}%（截止{data_cutoff}）"
            ),
            "data_quality": "COMPLETE",
            "source": source,
            "constituent_count": len(changes),
        }
        if source == "eastmoney_board_constituents":
            too_short = {key: len(values) for key, values in horizon_values.items() if len(values) < 5}
            if too_short:
                detail = "、".join(f"{key}:{count}只" for key, count in too_short.items())
                raise SectorResolveError(f"{universe_id}多周期成分股数据不足（{detail}）。")
            if timestamps:
                cutoff = datetime.fromtimestamp(max(timestamps), tz=local_now().tzinfo).date().isoformat()
            else:
                cutoff = data_cutoff
            d20_values = horizon_values["d20"]
            d20_positive = sum(value > 0 for value in d20_values)
            result.update({
                "returns_pct": {
                    key: round(sum(values) / len(values), 4)
                    for key, values in horizon_values.items()
                },
                "data_cutoff": cutoff,
                "position_20d": (
                    f"成分股20日上涨{d20_positive}/{len(d20_values)}"
                    f"（{d20_positive / len(d20_values) * 100:.1f}%）"
                ),
                "volume_signal": "成分股快照源未提供可审计的板块历史量比",
                "data_quality": "COMPLETE_BOARD_SNAPSHOT",
            })
            result["breadth_signal"] = (
                f"成分股上涨{advances}/{len(changes)}（{ratio:.1f}%），下跌{declines}，平盘{flat}，"
                f"涨幅≥2%共{strong}只，平均{average:+.2f}%（截止{cutoff}）"
            )
        return result

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse


DEFAULT_BASE_URL = "https://api.deepseek.com"
OFFICIAL_API_HOSTS = {"api.deepseek.com"}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
WINDOWS_CURL_STATUS_MARKER = b"\n__SECTOR_RADAR_HTTP_STATUS__:"


class DeepSeekError(RuntimeError):
    pass


class _WindowsTransportError(RuntimeError):
    pass


class _WindowsHTTPError(RuntimeError):
    def __init__(self, status: int, body: bytes):
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.body = body


@dataclass(frozen=True)
class ConnectionReport:
    models: tuple[str, ...]
    balance_available: bool
    balances: tuple[dict[str, str], ...]
    transport: str = ""

    def balance_text(self) -> str:
        values = []
        for item in self.balances:
            currency = str(item.get("currency", "")).strip()
            total = str(item.get("total_balance", "")).strip()
            if currency and total:
                values.append(f"{total} {currency}")
        return "、".join(values) if values else "接口未返回余额明细"


@dataclass
class ModelResult:
    text: str
    raw: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    web_search_calls: int = 0
    estimated_cost_cny: float = 0.0
    response_kind: str = ""
    finish_reason: str = ""

    def output_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        output = self.raw.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "?")
                counts[item_type] = counts.get(item_type, 0) + 1
        return counts

    @staticmethod
    def _clean_json_text(text: str) -> str:
        value = str(text or "").strip().lstrip("\ufeff")
        if value.startswith("```json"):
            value = value[7:].lstrip("\r\n")
        elif value.startswith("```"):
            value = value[3:].lstrip("\r\n")
        if value.endswith("```"):
            value = value[:-3].rstrip()
        return value

    @staticmethod
    def estimate_cost(
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        *,
        input_price_per_million: float,
        cached_input_price_per_million: float,
        output_price_per_million: float,
    ) -> float:
        cached = max(0, min(int(cached_tokens), int(input_tokens)))
        uncached = max(0, int(input_tokens) - cached)
        return (
            uncached * float(input_price_per_million)
            + cached * float(cached_input_price_per_million)
            + max(0, int(output_tokens)) * float(output_price_per_million)
        ) / 1_000_000

    def diagnostic_summary(self) -> str:
        status = str(self.raw.get("status") or "")
        incomplete = self.raw.get("incomplete_details")
        reason = ""
        if isinstance(incomplete, dict):
            reason = str(incomplete.get("reason") or incomplete.get("message") or "")
        error = self.raw.get("error")
        if not reason and isinstance(error, dict):
            reason = str(error.get("message") or error.get("code") or "")
        elif not reason and error:
            reason = str(error)
        output_counts = self.output_type_counts()
        parts = []
        if status:
            parts.append(f"status={status}")
        if self.finish_reason:
            parts.append(f"finish_reason={self.finish_reason}")
        if reason:
            parts.append(f"reason={reason}")
        if output_counts:
            parts.append("output=" + ",".join(f"{key}×{value}" for key, value in output_counts.items()))
        parts.extend([
            f"input_tokens={self.input_tokens}",
            f"output_tokens={self.output_tokens}",
            f"cached_tokens={self.cached_tokens}",
            f"reasoning_tokens={self.reasoning_tokens}",
            f"web_search_calls={self.web_search_calls}",
            f"estimated_cost=¥{self.estimated_cost_cny:.4f}",
        ])
        return "；".join(parts)

    def require_complete(self) -> None:
        if self.response_kind == "responses":
            status = str(self.raw.get("status") or "")
            if status != "completed":
                label = {
                    "incomplete": "Flash响应未完成",
                    "failed": "Flash响应失败",
                    "in_progress": "Flash响应仍处于进行中",
                }.get(status, "Flash响应状态异常")
                raise DeepSeekError(f"{label}（{self.diagnostic_summary()}）。本次用量已记录，结果未写入研究证据。")
            output = self.raw.get("output")
            bad_items = []
            failed_web_searches = 0
            completed_web_searches = 0
            completed_messages = 0
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "")
                    item_status = str(item.get("status") or "")
                    if item_type == "web_search_call":
                        if item_status == "completed":
                            completed_web_searches += 1
                        elif item_status:
                            failed_web_searches += 1
                        continue
                    if item_type == "message" and item_status in {"", "completed"}:
                        completed_messages += 1
                    if item_status and item_status != "completed":
                        bad_items.append(f"{item_type or '?'}:{item_status}")
            if bad_items:
                raise DeepSeekError(
                    "Flash顶层虽为completed，但内部输出项未完成（" + ",".join(bad_items[:8]) + "；"
                    + self.diagnostic_summary() + "）。本次用量已记录，结果未写入研究证据。"
                )
            if failed_web_searches and (completed_web_searches < 1 or completed_messages < 1):
                raise DeepSeekError(
                    "Flash顶层虽为completed，但联网搜索全部失败或没有最终正文（"
                    f"web_search_call失败{failed_web_searches}次、成功{completed_web_searches}次；"
                    + self.diagnostic_summary() + "）。本次用量已记录，结果未写入研究证据。"
                )
        elif self.response_kind == "chat":
            finish = self.finish_reason
            if finish != "stop":
                label = {
                    "length": "Pro输出达到长度或上下文上限，JSON可能被截断",
                    "content_filter": "Pro输出被内容安全过滤",
                    "tool_calls": "Pro意外返回工具调用，未返回终审JSON",
                    "insufficient_system_resource": "Pro因上游推理资源不足而中断",
                }.get(finish, "Pro响应结束原因异常")
                raise DeepSeekError(f"{label}（{self.diagnostic_summary()}）。本次用量已记录，结果未入库。")

    def json(self) -> dict[str, Any]:
        self.require_complete()
        cleaned = self._clean_json_text(self.text)
        if not cleaned:
            if self.response_kind == "responses" and self.web_search_calls:
                raise DeepSeekError(
                    "DeepSeek已完成联网工具阶段，但没有生成最终正文（" + self.diagnostic_summary() + "）。"
                    "本次搜索与用量已保存；没有写入研究证据或正式决策。"
                )
            raise DeepSeekError(
                "DeepSeek返回空正文（" + self.diagnostic_summary() + "）。"
                "本次响应及用量已保存在任务数据库；没有写入研究证据或正式决策。"
            )
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            preview = " ".join(cleaned[:160].splitlines())
            raise DeepSeekError(
                f"DeepSeek正文不是有效JSON：第{exc.lineno}行第{exc.colno}列；"
                f"长度{len(cleaned)}；开头={preview!r}；{self.diagnostic_summary()}。"
            ) from exc
        if not isinstance(value, dict):
            raise DeepSeekError("模型JSON顶层必须是对象。")
        return value


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 360,
        requester: Callable[[urllib.request.Request, int], bytes] | None = None,
        curl_path: str | None = None,
        runtime_dir: Path | None = None,
    ):
        if not api_key.strip():
            raise DeepSeekError("尚未填写DeepSeek API Key。")
        self.api_key = api_key.strip()
        if "\r" in self.api_key or "\n" in self.api_key:
            raise DeepSeekError("API Key格式异常：不得包含换行。")
        self.base_url = self.validate_base_url(base_url)
        self.timeout_seconds = max(30, min(900, int(timeout_seconds)))
        self._requester = requester
        self._curl_path = None if requester else (curl_path or self._find_windows_curl())
        self._runtime_dir = Path(runtime_dir) if runtime_dir else self._default_runtime_dir()

    @staticmethod
    def _find_windows_curl() -> str | None:
        if os.name != "nt":
            return None
        windows_root = os.environ.get("SystemRoot", r"C:\Windows")
        for candidate in (
            Path(windows_root) / "System32" / "curl.exe",
            Path(windows_root) / "Sysnative" / "curl.exe",
        ):
            if candidate.is_file():
                return str(candidate)
        return shutil.which("curl.exe")

    @staticmethod
    def _default_runtime_dir() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "data" / "runtime" / "deepseek"
        return Path(__file__).resolve().parents[1] / "data" / "runtime" / "deepseek"

    def transport_label(self) -> str:
        if self._requester:
            return "自定义测试通道"
        if self._curl_path:
            return "Windows系统curl安全网络通道（Schannel）"
        return "Python系统证书网络通道"

    @staticmethod
    def _curl_config_value(value: str) -> str:
        if "\r" in value or "\n" in value:
            raise _WindowsTransportError("curl安全配置包含非法换行")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def _decode_process_error(raw: bytes) -> str:
        for encoding in ("utf-8", "gb18030", "utf-16le"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def _windows_request(self, method: str, url: str, data: bytes | None) -> bytes:
        if not self._curl_path:
            raise _WindowsTransportError("Windows系统安全网络通道不可用")
        body_path: Path | None = None
        config_lines = [
            "url = " + self._curl_config_value(url),
            "request = " + self._curl_config_value(method),
            "header = " + self._curl_config_value(f"Authorization: Bearer {self.api_key}"),
            "header = " + self._curl_config_value("Accept: application/json"),
            "header = " + self._curl_config_value("Content-Type: application/json"),
            "header = " + self._curl_config_value("User-Agent: SectorRadar/2.0.0"),
        ]
        if data is not None:
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="deepseek-request-", suffix=".json.tmp", dir=self._runtime_dir,
            )
            body_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
            config_lines.append(
                "data-binary = " + self._curl_config_value("@" + body_path.as_posix())
            )
        wire = ("\n".join(config_lines) + "\n").encode("utf-8")
        command = [
            self._curl_path,
            "--config", "-",
            "--silent", "--show-error",
            "--http1.1", "--ipv4", "--tlsv1.2",
            "--proto", "=https",
            "--connect-timeout", "30",
            "--max-time", str(self.timeout_seconds),
            "--max-filesize", str(MAX_RESPONSE_BYTES + 1),
            "--retry", "0",
            "--write-out", WINDOWS_CURL_STATUS_MARKER.decode("ascii") + "%{http_code}",
        ]
        try:
            completed = subprocess.run(
                command,
                input=wire,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds + 15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _WindowsTransportError(f"Windows curl等待超过{self.timeout_seconds}秒") from exc
        except OSError as exc:
            raise _WindowsTransportError(str(exc).replace(self.api_key, "[REDACTED]")) from exc
        finally:
            if body_path is not None:
                try:
                    body_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if completed.returncode != 0:
            detail = self._decode_process_error(completed.stderr).replace(self.api_key, "[REDACTED]")
            detail = " ".join(detail.split())[:800]
            raise _WindowsTransportError(
                f"curl退出码{completed.returncode}：{detail or '未返回错误正文'}"
            )
        raw, separator, status_bytes = completed.stdout.rpartition(WINDOWS_CURL_STATUS_MARKER)
        if not separator:
            raise _WindowsTransportError("Windows curl没有返回HTTP状态")
        try:
            status = int(status_bytes.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _WindowsTransportError("Windows curl返回了无效HTTP状态") from exc
        if not 100 <= status <= 599:
            raise _WindowsTransportError(f"Windows curl返回了异常HTTP状态{status}")
        if status >= 400:
            raise _WindowsHTTPError(status, raw)
        return raw

    def _windows_streaming_request(self, method: str, url: str, data: bytes) -> bytes:
        """Run a long SSE request with an inactivity timeout, not a total timeout.

        DeepSeek keeps queued streaming requests alive with SSE comments.  Writing
        curl's response to a file lets us observe every new byte without placing
        the API key or the large request body on the command line.  The configured
        timeout is therefore reset whenever the response file grows.
        """
        if not self._curl_path:
            raise _WindowsTransportError("Windows系统安全网络通道不可用")
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        body_descriptor, body_name = tempfile.mkstemp(
            prefix="deepseek-request-", suffix=".json.tmp", dir=self._runtime_dir,
        )
        response_descriptor, response_name = tempfile.mkstemp(
            prefix="deepseek-stream-", suffix=".sse.tmp", dir=self._runtime_dir,
        )
        body_path = Path(body_name)
        response_path = Path(response_name)
        os.close(response_descriptor)
        with os.fdopen(body_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
        config_lines = [
            "url = " + self._curl_config_value(url),
            "request = " + self._curl_config_value(method),
            "header = " + self._curl_config_value(f"Authorization: Bearer {self.api_key}"),
            "header = " + self._curl_config_value("Accept: text/event-stream"),
            "header = " + self._curl_config_value("Content-Type: application/json"),
            "header = " + self._curl_config_value("User-Agent: SectorRadar/2.0.0"),
            "data-binary = " + self._curl_config_value("@" + body_path.as_posix()),
        ]
        wire = ("\n".join(config_lines) + "\n").encode("utf-8")
        command = [
            self._curl_path,
            "--config", "-",
            "--silent", "--show-error", "--no-buffer",
            "--http1.1", "--ipv4", "--tlsv1.2",
            "--proto", "=https",
            "--connect-timeout", "30",
            "--max-filesize", str(MAX_RESPONSE_BYTES + 1),
            "--retry", "0",
            "--output", str(response_path),
            "--write-out", WINDOWS_CURL_STATUS_MARKER.decode("ascii") + "%{http_code}",
        ]
        process: subprocess.Popen[bytes] | None = None
        raw = b""
        stdout = b""
        stderr = b""
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.stdin is None:
                raise _WindowsTransportError("Windows curl标准输入不可用")
            process.stdin.write(wire)
            process.stdin.close()
            process.stdin = None
            last_size = response_path.stat().st_size
            last_progress = time.monotonic()
            started = last_progress
            hard_limit_seconds = 4 * 60 * 60
            while process.poll() is None:
                now = time.monotonic()
                try:
                    current_size = response_path.stat().st_size
                except OSError:
                    current_size = last_size
                if current_size != last_size:
                    last_size = current_size
                    last_progress = now
                if current_size > MAX_RESPONSE_BYTES:
                    process.kill()
                    raise _WindowsTransportError("DeepSeek流式响应超过32MB安全上限")
                if now - last_progress > self.timeout_seconds:
                    process.kill()
                    raise _WindowsTransportError(
                        f"DeepSeek流式响应连续{self.timeout_seconds}秒没有收到任何新字节"
                    )
                if now - started > hard_limit_seconds:
                    process.kill()
                    raise _WindowsTransportError("DeepSeek流式请求超过4小时安全上限")
                time.sleep(0.25)
            stdout, stderr = process.communicate(timeout=15)
            raw = response_path.read_bytes()
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
            raise _WindowsTransportError("Windows curl结束后15秒仍未退出") from exc
        except OSError as exc:
            if process is not None and process.poll() is None:
                process.kill()
            raise _WindowsTransportError(str(exc).replace(self.api_key, "[REDACTED]")) from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            for temporary_path in (body_path, response_path):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if process is None:
            raise _WindowsTransportError("Windows curl未能启动")
        if process.returncode != 0:
            detail = self._decode_process_error(stderr).replace(self.api_key, "[REDACTED]")
            detail = " ".join(detail.split())[:800]
            raise _WindowsTransportError(
                f"curl退出码{process.returncode}：{detail or '未返回错误正文'}"
            )
        _unused, separator, status_bytes = stdout.rpartition(WINDOWS_CURL_STATUS_MARKER)
        if not separator:
            raise _WindowsTransportError("Windows curl没有返回HTTP状态")
        try:
            status = int(status_bytes.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _WindowsTransportError("Windows curl返回了无效HTTP状态") from exc
        if not 100 <= status <= 599:
            raise _WindowsTransportError(f"Windows curl返回了异常HTTP状态{status}")
        if status >= 400:
            raise _WindowsHTTPError(status, raw)
        return raw

    def _parse_chat_stream(self, raw: bytes) -> dict[str, Any]:
        stripped = raw.lstrip()
        if stripped.startswith(b"{"):
            try:
                value = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeepSeekError("DeepSeek返回了无法解析的JSON响应。") from exc
            if not isinstance(value, dict):
                raise DeepSeekError("DeepSeek响应顶层不是JSON对象。")
            return value

        response_id = ""
        model = ""
        role = "assistant"
        finish_reason = ""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        chunk_count = 0
        saw_done = False
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                continue
            data_line = line[5:].strip()
            if data_line == b"[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(data_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeepSeekError("DeepSeek流式响应包含无法解析的数据块。") from exc
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                detail = self._safe_error_detail(json.dumps(chunk, ensure_ascii=False).encode("utf-8"))
                raise DeepSeekError(f"DeepSeek业务错误：{detail}")
            chunk_count += 1
            response_id = str(chunk.get("id") or response_id)
            model = str(chunk.get("model") or model)
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            choice = choices[0]
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice.get("finish_reason") or "")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("role"):
                role = str(delta["role"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
        if not saw_done:
            raise DeepSeekError(
                "DeepSeek流式响应在[DONE]完成标记前中断；上游是否已产生费用无法由本机确认，软件不会自动重发。"
            )
        if chunk_count == 0:
            raise DeepSeekError("DeepSeek流式响应没有返回任何有效数据块。")
        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        return {
            "id": response_id,
            "model": model,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
            "streamed": True,
            "stream_chunk_count": chunk_count,
        }

    @staticmethod
    def validate_base_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in OFFICIAL_API_HOSTS:
            raise DeepSeekError("API地址只允许DeepSeek官方HTTPS地址：https://api.deepseek.com。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DeepSeekError("API地址不得包含账号、密码、查询参数或片段。")
        path = parsed.path.rstrip("/")
        if path not in {"", "/v1"}:
            raise DeepSeekError("API地址路径只允许为空或/v1。")
        return urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))

    @staticmethod
    def validate_models(flash_model: str, pro_model: str) -> tuple[str, str]:
        flash = str(flash_model or "").strip()
        pro = str(pro_model or "").strip()
        if flash != "deepseek-v4-flash":
            raise DeepSeekError("Flash模型必须是deepseek-v4-flash；Responses API目前不支持Pro。")
        if pro != "deepseek-v4-pro":
            raise DeepSeekError("Pro模型必须是deepseek-v4-pro。")
        return flash, pro

    def _safe_error_detail(self, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace")[:1600].replace(self.api_key, "[REDACTED]")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return " ".join(text.split())[:800]
        if isinstance(value, dict):
            error = value.get("error", value)
            if isinstance(error, dict):
                return "；".join(str(error.get(k, "")) for k in ("code", "type", "message") if error.get(k))[:800]
        return str(value)[:800]

    def _call(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}{endpoint}"
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SectorRadar/2.0.0",
            },
        )
        try:
            if self._requester:
                raw = self._requester(request, self.timeout_seconds)
            elif self._curl_path:
                if endpoint == "/chat/completions" and payload and payload.get("stream") is True:
                    raw = self._windows_streaming_request(method, url, data or b"")
                else:
                    raw = self._windows_request(method, url, data)
            else:
                context = ssl.create_default_context()
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
        except _WindowsHTTPError as exc:
            detail = self._safe_error_detail(exc.body)
            label = {
                400: "请求格式错误", 401: "API Key认证失败", 402: "API余额不足",
                422: "请求参数不受支持", 429: "请求过于频繁",
                500: "DeepSeek服务器错误", 503: "DeepSeek服务过载",
            }.get(exc.status, "DeepSeek接口错误")
            retry = "；这是上游临时错误，可稍后手工重试，软件不会自动重复扣费调用" if exc.status in {429, 500, 503} else ""
            raise DeepSeekError(f"{label}（HTTP {exc.status}）：{detail}{retry}") from exc
        except urllib.error.HTTPError as exc:
            detail = self._safe_error_detail(exc.read())
            label = {
                400: "请求格式错误", 401: "API Key认证失败", 402: "API余额不足",
                422: "请求参数不受支持", 429: "请求过于频繁",
                500: "DeepSeek服务器错误", 503: "DeepSeek服务过载",
            }.get(exc.code, "DeepSeek接口错误")
            retry = "；这是上游临时错误，可稍后手工重试，软件不会自动重复扣费调用" if exc.code in {429, 500, 503} else ""
            raise DeepSeekError(f"{label}（HTTP {exc.code}）：{detail}{retry}") from exc
        except urllib.error.URLError as exc:
            billing_note = (
                "这是不计Token费用的连接预检，尚未发送计费请求。"
                if method == "GET" else
                "上游是否已产生费用无法由本机确认；软件不会自动重发可能计费的请求。"
            )
            raise DeepSeekError(f"无法连接DeepSeek：{exc.reason}。{billing_note}") from exc
        except _WindowsTransportError as exc:
            billing_note = (
                "这是不计Token费用的连接预检，尚未发送计费请求。"
                if method == "GET" else
                "上游是否已产生费用无法由本机确认；软件不会自动重发可能计费的请求。"
            )
            raise DeepSeekError(f"Windows系统网络通道无法连接DeepSeek：{exc}。{billing_note}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise DeepSeekError(
                f"DeepSeek请求在{self.timeout_seconds}秒无有效网络进展后超时。"
                "上游是否已经产生费用无法由本机确认，因此软件不会自动重发。"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeepSeekError("DeepSeek响应超过32MB安全上限，已停止解析。")
        if endpoint == "/chat/completions" and payload and payload.get("stream") is True:
            result = self._parse_chat_stream(raw)
        else:
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                preview = " ".join(raw[:240].decode("utf-8", errors="replace").split())
                raise DeepSeekError(f"DeepSeek返回了无法解析的JSON响应；长度{len(raw)}，开头={preview!r}。") from exc
        if not isinstance(result, dict):
            raise DeepSeekError("DeepSeek响应顶层不是JSON对象。")
        if result.get("error") and endpoint not in {"/responses", "/chat/completions"}:
            detail = self._safe_error_detail(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            raise DeepSeekError(f"DeepSeek业务错误：{detail}")
        return result

    def test_connection(self) -> list[str]:
        result = self._call("GET", "/models")
        models = result.get("data", [])
        return [str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")]

    def connection_report(self, flash_model: str, pro_model: str, *, require_pro: bool = True) -> ConnectionReport:
        flash, pro = self.validate_models(flash_model, pro_model)
        models = tuple(self.test_connection())
        required = {flash} | ({pro} if require_pro else set())
        missing = sorted(required - set(models))
        if missing:
            raise DeepSeekError("账号当前模型清单缺少：" + "、".join(missing))
        balance = self._call("GET", "/user/balance")
        available = bool(balance.get("is_available"))
        infos = tuple(dict(item) for item in balance.get("balance_infos", []) if isinstance(item, dict))
        report = ConnectionReport(
            models=models,
            balance_available=available,
            balances=infos,
            transport=self.transport_label(),
        )
        if not available:
            raise DeepSeekError("DeepSeek余额状态为不可用；请先充值或检查账户。")
        return report

    def flash_search_json(
        self,
        *,
        instructions: str,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        model: str = "deepseek-v4-flash",
        max_output_tokens: int = 12000,
        reasoning_effort: str = "low",
        input_price_per_million: float = 1.0,
        cached_input_price_per_million: float = 0.02,
        output_price_per_million: float = 2.0,
    ) -> ModelResult:
        if model != "deepseek-v4-flash":
            raise DeepSeekError("Responses API只能调用deepseek-v4-flash。")
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise DeepSeekError("Flash推理强度不是官方支持值。")
        payload = {
            "model": model,
            "instructions": instructions,
            "input": prompt,
            "tools": [{"type": "web_search"}],
            # A named web_search choice forces another search whenever the model
            # chooses its next action.  With server-side tools this can finish as
            # a list of search calls and no assistant message.  auto still lets
            # the prompt require research, while allowing the final JSON message.
            "tool_choice": "auto",
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": int(max_output_tokens),
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema}},
            "user": "sector-radar-local",
        }
        raw = self._call("POST", "/responses", payload)
        return self._responses_result(
            raw,
            model=model,
            input_price_per_million=input_price_per_million,
            cached_input_price_per_million=cached_input_price_per_million,
            output_price_per_million=output_price_per_million,
        )

    def flash_finalize_json(
        self,
        *,
        instructions: str,
        prompt: str,
        previous_raw: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
        model: str = "deepseek-v4-flash",
        max_output_tokens: int = 6000,
        input_price_per_million: float = 1.0,
        cached_input_price_per_million: float = 0.02,
        output_price_per_million: float = 2.0,
    ) -> ModelResult:
        """Finish a tool-only Flash response without performing another search.

        DeepSeek's Responses API is stateless and does not support
        previous_response_id.  Its documented continuation mechanism is to pass
        web_search_call items back as input; the server restores their search
        results.  This method deliberately sets tool_choice=none and is only
        used after a user manually resumes a failed task.
        """
        if model != "deepseek-v4-flash":
            raise DeepSeekError("Responses API只能调用deepseek-v4-flash。")
        if str(previous_raw.get("status") or "") != "completed":
            raise DeepSeekError("只有status=completed的Flash搜索响应可以执行无搜索收尾。")
        prior_output = previous_raw.get("output")
        recoverable_items = [
            dict(item) for item in prior_output if isinstance(item, dict)
            and str(item.get("type") or "") in {"reasoning", "web_search_call"}
        ] if isinstance(prior_output, list) else []
        if not any(item.get("type") == "web_search_call" for item in recoverable_items):
            raise DeepSeekError("旧响应没有可复用的web_search_call，不能执行无搜索收尾。")
        continuation_input: list[dict[str, Any]] = [
            {"role": "user", "content": prompt},
            *recoverable_items,
            {
                "role": "user",
                "content": (
                    "联网检索已经完成。禁止再次调用任何工具；请只利用上面的检索结果，"
                    "立即重新生成符合既定JSON Schema的最终JSON对象，不得复制旧的无效正文，"
                    "不得输出Markdown或解释；字符串内容里的英文双引号必须正确转义。"
                ),
            },
        ]
        payload = {
            "model": model,
            "instructions": instructions,
            "input": continuation_input,
            "tools": [{"type": "web_search"}],
            "tool_choice": "none",
            "reasoning": {"effort": "none"},
            "max_output_tokens": int(max_output_tokens),
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema}},
            "user": "sector-radar-local",
        }
        raw = self._call("POST", "/responses", payload)
        return self._responses_result(
            raw,
            model=model,
            input_price_per_million=input_price_per_million,
            cached_input_price_per_million=cached_input_price_per_million,
            output_price_per_million=output_price_per_million,
        )

    @staticmethod
    def _responses_result(
        raw: dict[str, Any],
        *,
        model: str,
        input_price_per_million: float,
        cached_input_price_per_million: float,
        output_price_per_million: float,
    ) -> ModelResult:
        output = raw.get("output", [])
        message_texts: list[str] = []
        search_calls = 0
        for item in output if isinstance(output, list) else []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", ""))
            if item_type == "web_search_call":
                search_calls += 1
            if item_type != "message" or str(item.get("status") or "") not in {"", "completed"}:
                continue
            message_parts: list[str] = []
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    part_text = part.get("text", "")
                    if isinstance(part_text, dict):
                        part_text = part_text.get("value", "")
                    message_parts.append(str(part_text or ""))
            message_text = "".join(message_parts).strip()
            if message_text:
                message_texts.append(message_text)
        response_text = message_texts[-1] if message_texts else ""
        if not response_text and isinstance(raw.get("output_text"), str):
            response_text = raw["output_text"].strip()
        usage = raw.get("usage", {}) if isinstance(raw.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        input_details = usage.get("input_tokens_details", {}) or {}
        output_details = usage.get("output_tokens_details", {}) or {}
        cached_tokens = int(input_details.get("cached_tokens", 0) or 0)
        return ModelResult(
            text=response_text, raw=raw, model=str(raw.get("model", model)),
            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
            reasoning_tokens=int(output_details.get("reasoning_tokens", 0) or 0),
            web_search_calls=search_calls, response_kind="responses",
            estimated_cost_cny=ModelResult.estimate_cost(
                input_tokens, output_tokens, cached_tokens,
                input_price_per_million=input_price_per_million,
                cached_input_price_per_million=cached_input_price_per_million,
                output_price_per_million=output_price_per_million,
            ),
        )

    def pro_json(
        self,
        *,
        system: str,
        prompt: str,
        model: str = "deepseek-v4-pro",
        max_tokens: int = 64000,
        reasoning_effort: str = "max",
        input_price_per_million: float = 3.0,
        cached_input_price_per_million: float = 0.025,
        output_price_per_million: float = 6.0,
    ) -> ModelResult:
        if model != "deepseek-v4-pro":
            raise DeepSeekError("Pro终审必须调用deepseek-v4-pro。")
        if reasoning_effort not in {"high", "max"}:
            raise DeepSeekError("Pro推理强度只允许high或max。")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system + "\n必须返回一个有效JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            # DeepSeek's OpenAI-compatible V4 contract keeps the thinking
            # switch and effort at different levels.  Nesting effort under
            # ``thinking`` is tolerated by some gateways but can be ignored,
            # silently falling back to the service default.
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort,
            "response_format": {"type": "json_object"},
            "max_tokens": int(max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
            "user_id": "sector-radar-local",
        }
        raw = self._call("POST", "/chat/completions", payload)
        choices = raw.get("choices", [])
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        text = str(message.get("content") or "").strip()
        finish_reason = str(choice.get("finish_reason") or "")
        usage = raw.get("usage", {}) if isinstance(raw.get("usage"), dict) else {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
        cached_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        details = usage.get("completion_tokens_details", {}) or {}
        return ModelResult(
            text=text, raw=raw, model=str(raw.get("model", model)), input_tokens=input_tokens,
            output_tokens=output_tokens, cached_tokens=cached_tokens,
            reasoning_tokens=int(details.get("reasoning_tokens", 0) or 0),
            response_kind="chat", finish_reason=finish_reason,
            estimated_cost_cny=ModelResult.estimate_cost(
                input_tokens, output_tokens, cached_tokens,
                input_price_per_million=input_price_per_million,
                cached_input_price_per_million=cached_input_price_per_million,
                output_price_per_million=output_price_per_million,
            ),
        )

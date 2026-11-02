from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib import error, request


@dataclass(slots=True)
class CodexWorkerResult:
    thread_id: str
    final_response: str
    duration_ms: int | None = None


class CodexWorkerError(RuntimeError):
    pass


def _json_request(url: str, payload: dict | None, timeout_seconds: int) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        raise CodexWorkerError(str(detail or f"Codex Worker 返回 {exc.code}")) from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise CodexWorkerError("Codex Worker 当前不可用，请检查本机 Codex 登录和服务状态") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexWorkerError("Codex Worker 返回了无法解析的结果") from exc
    if not isinstance(result, dict):
        raise CodexWorkerError("Codex Worker 返回格式无效")
    return result


async def codex_worker_health(base_url: str, timeout_seconds: int = 3) -> dict:
    return await asyncio.to_thread(_json_request, f"{base_url.rstrip('/')}/health", None, timeout_seconds)


async def run_codex_worker(
    base_url: str,
    *,
    prompt: str,
    thread_id: str | None,
    output_schema: dict | None,
    operation: str,
    timeout_seconds: int,
    mentions: list[dict] | None = None,
) -> CodexWorkerResult:
    payload = {
        "prompt": prompt,
        "thread_id": thread_id,
        "output_schema": output_schema,
        "operation": operation,
        "mentions": mentions or [],
    }
    result = await asyncio.to_thread(
        _json_request,
        f"{base_url.rstrip('/')}/run",
        payload,
        timeout_seconds,
    )
    returned_thread_id = result.get("thread_id")
    final_response = result.get("final_response")
    if not isinstance(returned_thread_id, str) or not returned_thread_id:
        raise CodexWorkerError("Codex Worker 未返回会话标识")
    if not isinstance(final_response, str) or not final_response:
        raise CodexWorkerError("Codex 没有生成可用的回复")
    duration_ms = result.get("duration_ms")
    return CodexWorkerResult(
        thread_id=returned_thread_id,
        final_response=final_response,
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
    )


TASK_DRAFT_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft"],
    "properties": {
        "draft": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "title",
                "description",
                "priority",
                "due_date",
                "participant_ids",
                "task_kind",
                "task_spec",
                "requires_code_change",
                "implementation_summary",
                "change_summary",
            ],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                "due_date": {"type": "string"},
                "participant_ids": {"type": "array", "items": {"type": "string"}},
                "task_kind": {"type": "string"},
                "task_spec": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["workflow_type", "deliverables", "steps", "fields"],
                    "properties": {
                        "workflow_type": {"type": "string"},
                        "deliverables": {"type": "array", "items": {"type": "string"}},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["label", "description"],
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                            },
                        },
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["key", "label", "type", "required", "options"],
                                "properties": {
                                    "key": {"type": "string"},
                                    "label": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": ["text", "textarea", "number", "date", "select", "checkbox", "file"],
                                    },
                                    "required": {"type": "boolean"},
                                    "options": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                    },
                },
                "requires_code_change": {"type": "boolean"},
                "implementation_summary": {"type": "string"},
                "change_summary": {"type": "string"},
            },
        },
    },
}


TASK_IMPLEMENTATION_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assistant_message", "summary", "changed_files", "test_summary", "ready_to_publish"],
    "properties": {
        "assistant_message": {"type": "string"},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "test_summary": {"type": "string"},
        "ready_to_publish": {"type": "boolean"},
    },
}

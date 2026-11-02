from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, MentionInput, Sandbox, TextInput
from pydantic import BaseModel, Field


WORKSPACE = Path(os.getenv("CODEX_WORKSPACE_ROOT", "/workspace")).resolve()
TASK_FILES = Path(os.getenv("CODEX_TASK_FILE_ROOT", "/workspace/.medcontour-task-files")).resolve()
CODEX_HOME = Path(os.getenv("CODEX_HOME", "/home/codex/.codex")).resolve()
MODEL = os.getenv("CODEX_MODEL", "").strip()
RUN_TIMEOUT_SECONDS = int(os.getenv("CODEX_RUN_TIMEOUT_SECONDS", "600"))
CLI_VERSION = version("openai-codex-cli-bin")
run_lock = asyncio.Lock()

app = FastAPI(title="MedContour Codex Worker", docs_url=None, redoc_url=None)


def has_content(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


class FileMention(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=2000)


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    thread_id: str | None = Field(default=None, max_length=200)
    output_schema: dict | None = None
    operation: Literal["chat", "draft", "implement"] = "chat"
    mentions: list[FileMention] = Field(default_factory=list, max_length=20)


class RunResponse(BaseModel):
    thread_id: str
    final_response: str
    duration_ms: int | None = None


@app.get("/health")
def health():
    auth_file = CODEX_HOME / "auth.json"
    config_file = CODEX_HOME / "config.toml"
    configured = has_content(auth_file)
    credential_source = os.getenv("CODEX_CREDENTIAL_SOURCE", "unknown")
    if not configured:
        credential_source = "none"
    elif credential_source in {"none", "unknown"}:
        credential_source = "project-auth-file"
    return {
        "status": "ok",
        "configured": configured,
        "config_ready": config_file.is_file(),
        "credential_source": credential_source,
        "runtime": f"codex-cli {CLI_VERSION}",
        "workspace_ready": WORKSPACE.is_dir(),
        "workspace_writable": WORKSPACE.is_dir() and os.access(WORKSPACE, os.W_OK),
        "task_files_ready": TASK_FILES.is_dir() and os.access(TASK_FILES, os.R_OK),
        "model": MODEL or "default",
    }


def developer_instructions(operation: str) -> str:
    shared = """
你是 MedContour 平台内的任务发布工程代理。所有工作均在本机完成。
必须保留现有影像标注和已发布协作任务的行为；任何能力扩展都采用增量、向后兼容和版本化方式。
不得删除已有任务数据、历史修订或原始附件。不得读取或输出 .env、auth.json、患者原始影像或无关敏感数据。
任务草稿必须适用于多人协作，并清楚说明交付目标、参与者和处理步骤。
""".strip()
    if operation == "implement":
        return shared + "\n你现在可以修改 /workspace 中的源码、安装依赖并运行测试。只实现当前任务所需的最小增量能力，并保留旧版本入口。"
    return shared + "\n本轮只分析需求和本机暂存附件，生成结构化任务草稿；不要修改任何源码或运行部署命令。"


@app.post("/run", response_model=RunResponse)
async def run_codex(body: RunRequest):
    if not has_content(CODEX_HOME / "auth.json"):
        raise HTTPException(status_code=503, detail="请填写 codex-worker/auth.json")
    if not WORKSPACE.is_dir():
        raise HTTPException(status_code=503, detail="Codex 工作区尚未挂载")

    # Docker already provides the process boundary. A nested read-only Codex
    # sandbox relies on bubblewrap/user namespaces, which Docker Desktop does
    # not expose to this container and causes every file tool call to hang.
    # Task files stay protected by their read-only bind mount below /workspace.
    sandbox = Sandbox.full_access
    config = CodexConfig(cwd=str(WORKSPACE), env={"CODEX_HOME": str(CODEX_HOME)})
    common = {
        "approval_mode": ApprovalMode.deny_all,
        "cwd": str(WORKSPACE),
        "sandbox": sandbox,
    }
    if body.operation != "chat":
        common["developer_instructions"] = developer_instructions(body.operation)
    if MODEL:
        common["model"] = MODEL

    async with run_lock:
        try:
            async with AsyncCodex(config) as codex:
                if body.thread_id:
                    thread = await codex.thread_resume(body.thread_id, **common)
                else:
                    thread = await codex.thread_start(**common)
                run_input = [TextInput(body.prompt)]
                for mention in body.mentions:
                    path = Path(mention.path).resolve()
                    if TASK_FILES not in path.parents or not path.is_file():
                        raise HTTPException(status_code=422, detail=f"附件路径无效：{mention.name}")
                    run_input.append(MentionInput(name=mention.name, path=str(path)))
                run_options = {
                    "approval_mode": ApprovalMode.deny_all,
                    "cwd": str(WORKSPACE),
                    "sandbox": sandbox,
                    **({"model": MODEL} if MODEL else {}),
                }
                if body.output_schema is not None:
                    run_options["output_schema"] = body.output_schema
                result = await asyncio.wait_for(
                    thread.run(run_input[0].text if len(run_input) == 1 else run_input, **run_options),
                    timeout=RUN_TIMEOUT_SECONDS,
                )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Codex 本轮处理超时") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Codex 执行失败：{str(exc)[:500]}") from exc

    response = result.final_response or ""
    if body.output_schema is not None:
        try:
            json.loads(response)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="Codex 未返回符合任务发布协议的结构化结果") from exc
    return RunResponse(thread_id=thread.id, final_response=response, duration_ms=result.duration_ms)

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from pdf2zh_next.config.cli_env_model import CLIEnvSettingsModel
from pdf2zh_next.config.main import ConfigManager
from pdf2zh_next.config.model import SettingsModel
from pdf2zh_next.high_level import do_translate_async_stream


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_base_output_dir() -> Path:
    # Prefer PDF2ZH_OUTPUT, fallback to ./output
    output_dir = os.environ.get("PDF2ZH_OUTPUT", None)
    if not output_dir:
        output_dir = str(Path.cwd() / "output")
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class TaskResult:
    original_pdf_path: str | None = None
    mono_pdf_path: str | None = None
    dual_pdf_path: str | None = None
    total_seconds: float | None = None


@dataclass
class TaskState:
    task_id: str
    status: str = "QUEUED"  # QUEUED|RUNNING|SUCCESS|ERROR|CANCELED
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    output_dir: Path | None = None
    upload_path: Path | None = None
    result: TaskResult | None = None
    sse_queue: asyncio.Queue | None = None
    worker: asyncio.Task | None = None


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._lock = asyncio.Lock()

    async def create(self, state: TaskState) -> None:
        async with self._lock:
            self._tasks[state.task_id] = state

    async def get(self, task_id: str) -> TaskState | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update(self, task_id: str, **kwargs) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for k, v in kwargs.items():
                setattr(task, k, v)
            task.updated_at = _utc_now()

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.worker and not task.worker.done():
                task.worker.cancel()
            task.status = "CANCELED"
            task.updated_at = _utc_now()
            return True


task_registry = TaskRegistry()

app = FastAPI(title="pdf2zh-next API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


async def _build_settings_from_options(options: dict[str, Any] | None, output_dir: Path) -> SettingsModel:
    cm = ConfigManager()
    env_vars = cm.parse_env_vars()
    options = options or {}
    # Allow both JSON string and dict in form field
    merged_cli: dict[str, Any] = {}
    if options:
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid options JSON: {e}")
        if not isinstance(options, dict):
            raise HTTPException(status_code=400, detail="options must be dict or JSON string")
        # parse_dict_vars will normalize keys internally (convert to uppercase)
        merged_cli = cm.parse_dict_vars(dict_vars=options)
    merged = cm.merge_settings([merged_cli, env_vars])
    
    # Debug logging
    logger.debug(f"Options from request: {options}")
    logger.debug(f"Parsed CLI settings: {merged_cli}")
    logger.debug(f"Merged settings: {merged}")
    
    try:
        # Build CLI model then convert to SettingsModel
        cli_model = CLIEnvSettingsModel(**merged)
        settings = cli_model.to_settings_model()
        # Override output dir with task-specific path
        settings.translation.output = str(output_dir)
        return settings
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid options: {e}")


async def _run_translation(task: TaskState, settings: SettingsModel) -> None:
    assert task.upload_path is not None
    queue: asyncio.Queue = asyncio.Queue()
    task.sse_queue = queue
    try:
        await task_registry.update(task.task_id, status="RUNNING")
        async for event in do_translate_async_stream(settings, str(task.upload_path)):
            # sanitize event for SSE (avoid non-JSON-serializable objects)
            sanitized_event = event
            if isinstance(event, dict) and event.get("type") == "finish":
                translate_result = event.get("translate_result")
                if translate_result is not None:
                    # Support both object with attributes and dict
                    def _attr(o: Any, name: str):
                        try:
                            return getattr(o, name)
                        except Exception:
                            return None

                    original_pdf_path = (
                        str(_attr(translate_result, "original_pdf_path"))
                        if _attr(translate_result, "original_pdf_path")
                        else (
                            str(translate_result.get("original_pdf_path"))
                            if isinstance(translate_result, dict)
                            and translate_result.get("original_pdf_path")
                            else None
                        )
                    )
                    mono_pdf_path = (
                        str(_attr(translate_result, "mono_pdf_path"))
                        if _attr(translate_result, "mono_pdf_path")
                        else (
                            str(translate_result.get("mono_pdf_path"))
                            if isinstance(translate_result, dict)
                            and translate_result.get("mono_pdf_path")
                            else None
                        )
                    )
                    dual_pdf_path = (
                        str(_attr(translate_result, "dual_pdf_path"))
                        if _attr(translate_result, "dual_pdf_path")
                        else (
                            str(translate_result.get("dual_pdf_path"))
                            if isinstance(translate_result, dict)
                            and translate_result.get("dual_pdf_path")
                            else None
                        )
                    )
                    total_seconds_val = _attr(translate_result, "total_seconds")
                    if total_seconds_val is None and isinstance(translate_result, dict):
                        total_seconds_val = translate_result.get("total_seconds")
                    total_seconds = (
                        float(total_seconds_val) if total_seconds_val is not None else None
                    )

                    # Set task result
                    task.result = TaskResult(
                        original_pdf_path=original_pdf_path,
                        mono_pdf_path=mono_pdf_path,
                        dual_pdf_path=dual_pdf_path,
                        total_seconds=total_seconds,
                    )

                    # Replace event's translate_result with JSON-friendly summary
                    sanitized_event = {
                        **event,
                        "translate_result": {
                            "original_pdf_path": original_pdf_path,
                            "mono_pdf_path": mono_pdf_path,
                            "dual_pdf_path": dual_pdf_path,
                            "total_seconds": total_seconds,
                        },
                    }

            # push to SSE
            await queue.put(sanitized_event)
            # update progress snapshot
            if isinstance(sanitized_event, dict) and sanitized_event.get("type") == "progress":
                await task_registry.update(task.task_id, progress=sanitized_event)
            if isinstance(sanitized_event, dict) and sanitized_event.get("type") == "finish":
                await task_registry.update(task.task_id, status="SUCCESS")
                break
            if isinstance(sanitized_event, dict) and sanitized_event.get("type") == "error":
                await task_registry.update(task.task_id, status="ERROR", error=sanitized_event)
                break
    except asyncio.CancelledError:
        await task_registry.update(task.task_id, status="CANCELED")
        raise
    except Exception as e:
        logger.exception("Unexpected error in translation task")
        await task_registry.update(task.task_id, status="ERROR", error={"type": "error", "error": str(e)})
    finally:
        # Signal SSE consumers to close
        if task.sse_queue:
            await task.sse_queue.put({"type": "end"})


@app.post("/v1/translate")
async def submit_translate(
    file: UploadFile = File(...),
    options: str | None = Form(None),
):
    task_id = uuid.uuid4().hex
    base_dir = _get_base_output_dir()
    task_dir = base_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    upload_path = task_dir / (file.filename or f"input-{task_id}.pdf")
    if not upload_path.suffix.lower().endswith(".pdf"):
        upload_path = upload_path.with_suffix(".pdf")

    # Save upload
    try:
        content = await file.read()
        upload_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {e}")

    state = TaskState(task_id=task_id, status="QUEUED", output_dir=task_dir, upload_path=upload_path)
    await task_registry.create(state)

    # Build settings
    settings = await _build_settings_from_options(options, output_dir=task_dir)
    # Inject input file
    settings.basic.input_files = {str(upload_path)}

    # Validate eagerly and start worker
    try:
        settings.validate_settings()
    except Exception as e:
        await task_registry.update(task_id, status="ERROR", error={"type": "error", "error": str(e)})
        raise HTTPException(status_code=400, detail=f"Invalid settings: {e}")

    worker = asyncio.create_task(_run_translation(state, settings), name=f"translate-{task_id}")
    await task_registry.update(task_id, worker=worker)
    return {"task_id": task_id}


@app.get("/v1/translate/{task_id}/status")
async def get_status(task_id: str):
    task = await task_registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "progress": task.progress,
        "error": task.error,
    }


@app.get("/v1/translate/{task_id}/events")
async def sse_events(task_id: str):
    task = await task_registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        # Late consumers: create queue if missing
        if task.sse_queue is None:
            task.sse_queue = asyncio.Queue()
        while True:
            event = await task.sse_queue.get()
            if isinstance(event, dict) and event.get("type") == "end":
                yield {"event": "end", "data": json.dumps({"task_id": task_id, "status": task.status})}
                break
            yield {"event": event.get("type", "progress"), "data": json.dumps(event)}

    return EventSourceResponse(event_generator())


@app.get("/v1/translate/{task_id}/result")
async def download_result(task_id: str, type: str = "mono"):
    task = await task_registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "SUCCESS" or not task.result:
        return JSONResponse(status_code=409, content={"detail": "Task not finished"})

    path: str | None
    if type == "mono":
        path = task.result.mono_pdf_path
    elif type == "dual":
        path = task.result.dual_pdf_path
    else:
        raise HTTPException(status_code=400, detail="type must be mono or dual")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="File not found")
    filename = Path(path).name
    return FileResponse(path, filename=filename, media_type="application/pdf")


@app.delete("/v1/translate/{task_id}")
async def cancel_task(task_id: str):
    ok = await task_registry.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": "CANCELED"}


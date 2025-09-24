from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from celery import Celery
from minio import Minio
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential


# =========================
# Environment configuration
# =========================
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv(
    "CELERY_RESULT_BACKEND", "redis://localhost:6379/0"
)

TRANSLATE_API_BASE = os.getenv("TRANSLATE_API_BASE", "http://localhost:8000")
# Optional: Provide default engine options here (will be merged per task)
TRANSLATE_OPTIONS_JSON = os.getenv("TRANSLATE_OPTIONS_JSON", "")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("1", "true", "yes")
MINIO_BUCKET_IN = os.getenv("MINIO_BUCKET_IN", "pdf-input")
MINIO_BUCKET_OUT = os.getenv("MINIO_BUCKET_OUT", "pdf-output")


app = Celery("pdf2zh_next_celery", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)


def _get_minio() -> Minio:
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


@dataclass
class TranslateResultKeys:
    mono_key: str | None
    dual_key: str | None


async def _post_translate(client: httpx.AsyncClient, file_bytes: bytes, filename: str, options: dict[str, Any]) -> str:
    files = {"file": (filename, file_bytes, "application/pdf")}
    data = {"options": json.dumps(options)} if options else {}
    resp = await client.post(f"{TRANSLATE_API_BASE}/v1/translate", files=files, data=data, timeout=None)
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    return task_id


@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(60))
async def _wait_success(client: httpx.AsyncClient, task_id: str) -> None:
    resp = await client.get(f"{TRANSLATE_API_BASE}/v1/translate/{task_id}/status")
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status == "SUCCESS":
        return
    if status in ("ERROR", "CANCELED"):
        raise RuntimeError(f"Task {task_id} ended with status {status}")
    raise RuntimeError("NOT_READY")


async def _download_result(client: httpx.AsyncClient, task_id: str, result_type: str) -> bytes | None:
    resp = await client.get(
        f"{TRANSLATE_API_BASE}/v1/translate/{task_id}/result",
        params={"type": result_type},
    )
    if resp.status_code == 409:
        return None
    resp.raise_for_status()
    return resp.content


def _derive_output_keys(object_name: str) -> tuple[str, str]:
    base = object_name.rsplit(".", 1)[0]
    return f"{base}.mono.pdf", f"{base}.dual.pdf"


@app.task(name="translate_minio_object")
def translate_minio_object(
    object_name: str,
    *,
    bucket_in: str | None = None,
    bucket_out: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """
    Celery Task: Download PDF from MinIO, call translate API, upload results back to MinIO.

    Args:
        object_name: MinIO object key in input bucket
        bucket_in: Optional override for input bucket
        bucket_out: Optional override for output bucket
        options: Optional options dict for API (e.g. {"OPENAI": true, ...})

    Returns:
        {"task_id": str, "mono_key": str|None, "dual_key": str|None}
    """
    # Merge global options from env
    merged_options: dict[str, Any] = {}
    if TRANSLATE_OPTIONS_JSON:
        try:
            merged_options.update(json.loads(TRANSLATE_OPTIONS_JSON))
        except Exception:
            pass
    if options:
        merged_options.update(options)

    bucket_in = bucket_in or MINIO_BUCKET_IN
    bucket_out = bucket_out or MINIO_BUCKET_OUT

    minio_client = _get_minio()
    if not minio_client.bucket_exists(bucket_out):
        minio_client.make_bucket(bucket_out)

    # Download file
    response = minio_client.get_object(bucket_in, object_name)
    try:
        file_bytes = response.read()
    finally:
        response.close()
        response.release_conn()

    # Call API
    filename = object_name.split("/")[-1] or "input.pdf"
    task_id: str
    mono_data: bytes | None = None
    dual_data: bytes | None = None
    t0 = time.time()
    try:
        async def _run() -> tuple[str, bytes | None, bytes | None]:
            async with httpx.AsyncClient(timeout=None) as client:
                tid = await _post_translate(client, file_bytes, filename, merged_options)
                await _wait_success(client, tid)
                mono = await _download_result(client, tid, "mono")
                dual = await _download_result(client, tid, "dual")
                return tid, mono, dual

        task_id, mono_data, dual_data = asyncio.run(_run())
    except Exception as e:
        raise RuntimeError(f"API translation failed: {e}")

    # Upload results
    mono_key, dual_key = _derive_output_keys(object_name)
    if mono_data:
        minio_client.put_object(
            bucket_out,
            mono_key,
            data=bytes(mono_data),
            length=len(mono_data),
            content_type="application/pdf",
        )
    else:
        mono_key = None

    if dual_data:
        minio_client.put_object(
            bucket_out,
            dual_key,
            data=bytes(dual_data),
            length=len(dual_data),
            content_type="application/pdf",
        )
    else:
        dual_key = None

    return {"task_id": task_id, "mono_key": mono_key, "dual_key": dual_key, "seconds": round(time.time() - t0, 2)}


if __name__ == "__main__":
    # Simple local test (requires running API & MinIO)
    # Enqueue a task manually:
    # celery -A examples.celery_minio_worker:app worker --loglevel=info
    # Then run this module to send task:
    result = translate_minio_object.delay(
        object_name=os.getenv("TEST_OBJECT", "sample.pdf"),
        options=json.loads(os.getenv("TEST_OPTIONS", "{}")),
    )
    print("Task queued:", result.id)



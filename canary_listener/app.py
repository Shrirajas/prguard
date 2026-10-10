"""Canary listener: records proof that PR code ran and what it could see.

Keeps env var NAMES and the planted fake canary only; every other field is
dropped. Run it bound to localhost:
    uvicorn canary_listener.app:app --host 127.0.0.1 --port 9999
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request

BACKEND_URL = os.environ.get("PRGUARD_BACKEND_URL", "http://127.0.0.1:8000")
EXPECTED_CANARY = "canary-demo-0000"

app = FastAPI(title="PRGuard canary listener")
history: list[dict[str, Any]] = []


@app.post("/canary")
async def receive(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    names = body.get("env_names") if isinstance(body, dict) else None
    canary = body.get("canary") if isinstance(body, dict) else None
    record = {
        "ts": time.time(),
        "ran": True,
        "env_names": [str(n)[:80] for n in names[:200]] if isinstance(names, list) else [],
        # Only ever echo the known fake marker, never an arbitrary value.
        "canary": EXPECTED_CANARY if canary == EXPECTED_CANARY else None,
    }
    history.append(record)
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.post(f"{BACKEND_URL}/internal/canary", json=record)
    except httpx.HTTPError:
        pass  # the backend may be down; the record is still kept here
    return {"ok": True}


@app.get("/canary")
async def list_history() -> list[dict[str, Any]]:
    return history


@app.delete("/canary")
async def clear() -> dict[str, Any]:
    history.clear()
    return {"ok": True}

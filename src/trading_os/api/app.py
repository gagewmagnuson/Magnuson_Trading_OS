"""
FastAPI serving layer — the V1 read-only API (ROADMAP V1; DEC-021).

Repo path: src/trading_os/api/app.py

This module is the ASGI application object. In this first slice it exposes only
an unauthenticated liveness probe (`GET /v1/health`) — no auth, no database —
so we can confirm the app boots and serves over HTTP before wiring the DEC-021
auth facade (deps.py) or any data routers.

The API is READ-ONLY (ARCHITECTURE.md): it never writes facts. All writes go
through ingest connectors; API-key minting is a separate admin CLI. Every data
endpoint added later takes an `as_of` parameter and reads only through the
*_asof() paths — but none of that exists yet in this skeleton.

Run locally (from repo root, venv active, PYTHONPATH=src):
    uvicorn trading_os.api.app:app --reload
Then:
    curl -s localhost:8000/v1/health      -> {"status":"ok"}
    open http://localhost:8000/docs        (auto-generated OpenAPI UI)
"""
from __future__ import annotations

from fastapi import Depends, FastAPI

from trading_os.api.deps import Consumer, require_consumer
from trading_os.api.routers import bars, fundamentals

app = FastAPI(
    title="Magnuson Trading OS — Serving API",
    version="0.1.0",
    description="Point-in-time-correct, read-only market-data API. "
                "Every data endpoint accepts an as_of parameter.",
)

app.include_router(bars.router)
app.include_router(fundamentals.router)

@app.get("/v1/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness probe. Unauthenticated by design; no database access.

    Confirms the process is up and serving. It deliberately does NOT check
    Postgres or the Parquet lake — a readiness check that probes those
    dependencies is a later, separate endpoint so liveness and readiness stay
    distinct.
    """
    return {"status": "ok"}

@app.get("/v1/whoami", tags=["ops"])
def whoami(consumer: Consumer = Depends(require_consumer)) -> dict[str, object]:
    """Authenticated probe. Requires a valid, active API key; returns the
    consumer behind it. This is the auth analog of /v1/health — it proves the
    DEC-021 auth dependency gates a real endpoint end-to-end. Every data router
    added later declares the same Depends(require_consumer) and is thereby
    unreachable without a valid key.
    """
    return {"consumer_id": consumer.consumer_id, "label": consumer.label}
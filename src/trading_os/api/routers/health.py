"""
Health router — operational metadata surface (V1 UI + monitoring contract).

Repo path: src/trading_os/api/routers/health.py

Publishes platform health as facts (freshness, job status, DQ results) for ANY
consumer — the UI is the first client, but monitors, the Research OS, and agents
consume the same contract. Freshness is reported as facts (last_batch_at,
expected_frequency, lag_seconds), never as an opinion; each consumer applies its
own thresholds. Read-only; reads meta.ingest_batch / meta.dq_result /
ref.data_source through the same auth + connection deps as every data router.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, Query

from trading_os.api.deps import Consumer, get_conn, require_consumer
from trading_os.api.models import (
    DQResultItem, HealthSummary, JobRun, PingResponse, SourceHealth,
)

router = APIRouter(prefix="/v1/health", tags=["health"])

# Expected cadence per source kind — a FACT about the source, not a policy.
# Consumers decide what lag is "too much."



def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(__file__),
        ).decode().strip()
    except Exception:  # noqa: BLE001
        return None


@router.get("/ping", response_model=PingResponse)
def ping(conn: psycopg.Connection = Depends(get_conn)) -> PingResponse:
    """Liveness + readiness: process is up, and can it reach Postgres."""
    db_ok = True
    try:
        conn.execute("select 1").fetchone()
    except Exception:  # noqa: BLE001
        db_ok = False
    return PingResponse(
        status="ok", server_time=datetime.now(timezone.utc),
        version="0.1.0", git_sha=_git_sha(), db_connected=db_ok,
    )


def _sources(conn) -> list[SourceHealth]:
    """One row per registered pipeline (meta.pipeline_definition), with freshness
    joined from meta.ingest_batch on (source_id, dataset). Declared, not observed:
    a pipeline that stopped running still appears (stale/never-run)."""
    rows = conn.execute(
        """
        select ds.name, p.dataset, p.kind, p.cadence, p.retired, p.critical,
               (select max(b.finished_at) from meta.ingest_batch b
                 where b.source_id = p.source_id and b.dataset = p.dataset
                   and b.status = 'succeeded'),
               (select b.status from meta.ingest_batch b
                 where b.source_id = p.source_id and b.dataset = p.dataset
                 order by b.started_at desc limit 1)
          from meta.pipeline_definition p
          join ref.data_source ds on ds.source_id = p.source_id
         order by p.critical desc, ds.name, p.dataset
        """
    ).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for name, dataset, kind, cadence, retired, critical, last_at, last_status in rows:
        lag = int((now - last_at).total_seconds()) if last_at else None
        out.append(SourceHealth(
            name=name, dataset=dataset, kind=kind,
            last_batch_at=last_at, last_status=last_status,
            expected_frequency=cadence, lag_seconds=lag,
            retired=retired, critical=critical,
        ))
    return out


def _jobs(conn, limit: int, only_failed: bool = False) -> list[JobRun]:
    where = "where status = 'failed'" if only_failed else ""
    rows = conn.execute(
        f"""
        select batch_id, dataset, source_id, status, started_at, finished_at,
               rows_in, rows_out, code_version, error
          from meta.ingest_batch {where}
         order by started_at desc limit %s
        """,
        (limit,),
    ).fetchall()
    return [JobRun(
        batch_id=r[0], dataset=r[1], source_id=r[2], status=r[3],
        started_at=r[4], finished_at=r[5], rows_in=r[6], rows_out=r[7],
        code_version=r[8], error=r[9],
    ) for r in rows]


def _dq(conn, limit: int, only_failed: bool = False) -> list[DQResultItem]:
    where = "where r.passed = false" if only_failed else ""
    rows = conn.execute(
        f"""
        select r.result_id, c.name, r.batch_id, r.run_at, r.passed,
               r.observed->>'severity', (r.observed->>'count')::int, r.details
          from meta.dq_result r
          join meta.data_quality_check c on c.check_id = r.check_id
          {where}
         order by r.run_at desc limit %s
        """,
        (limit,),
    ).fetchall()
    return [DQResultItem(
        result_id=r[0], check_name=r[1], batch_id=r[2], run_at=r[3],
        passed=r[4], severity=r[5], count=r[6], details=r[7],
    ) for r in rows]


@router.get("/sources", response_model=list[SourceHealth])
def sources(consumer: Consumer = Depends(require_consumer),
            conn: psycopg.Connection = Depends(get_conn)) -> list[SourceHealth]:
    """Per-pipeline freshness: one row per registered (source, dataset), last
    successful batch, cadence, lag, and retired/critical flags."""
    return _sources(conn)


@router.get("/jobs", response_model=list[JobRun])
def jobs(limit: int = Query(50, ge=1, le=500),
         consumer: Consumer = Depends(require_consumer),
         conn: psycopg.Connection = Depends(get_conn)) -> list[JobRun]:
    """Recent ingest batches, newest first."""
    return _jobs(conn, limit)


@router.get("/dq", response_model=list[DQResultItem])
def dq(limit: int = Query(50, ge=1, le=500),
       consumer: Consumer = Depends(require_consumer),
       conn: psycopg.Connection = Depends(get_conn)) -> list[DQResultItem]:
    """Recent data-quality results, newest first."""
    return _dq(conn, limit)


@router.get("/summary", response_model=HealthSummary)
def summary(consumer: Consumer = Depends(require_consumer),
            conn: psycopg.Connection = Depends(get_conn)) -> HealthSummary:
    """Top-level operator view: per-source freshness, recent jobs, and current
    alerts (recent failed jobs + failed DQ checks)."""
    return HealthSummary(
        generated_at=datetime.now(timezone.utc),
        sources=_sources(conn),
        recent_jobs=_jobs(conn, 20),
        recent_failures=_jobs(conn, 20, only_failed=True),
        recent_dq_failures=_dq(conn, 20, only_failed=True),
    )
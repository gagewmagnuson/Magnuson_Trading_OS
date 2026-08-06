"""
Catalog router — the data dictionary (V1 UI + agent semantic layer).

Repo path: src/trading_os/api/routers/catalog.py

Publishes the machine-readable catalog: every feature's definition, version,
inputs, and PIT semantics, plus the available datasets. This doubles as the
human-readable version of the agent semantic layer (blueprint §7/§8) — an agent
reading /catalog/features learns that realized_vol20 means "annualized std of 20
daily log returns, PIT as of query date." Read-only; same auth as data routers.
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends

from trading_os.api.deps import Consumer, get_conn, require_consumer
from trading_os.api.models import DatasetItem, FeatureDefinitionItem

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

# The datasets the platform publishes. Static today; grows as sources are added
# (options, sentiment, news, ...). Establishing the endpoint now per the frozen
# contract (additive-only).
_DATASETS = [
    DatasetItem(name="bars", description="EOD price bars (unadjusted stored, adjusted on read), PIT.", storage="parquet_lake"),
    DatasetItem(name="gold", description="Derived features (returns, moving averages, vol, momentum), PIT.", storage="parquet_lake"),
    DatasetItem(name="macro", description="Macro series with ALFRED vintages (revision-aware).", storage="postgres"),
    DatasetItem(name="fundamentals", description="SEC filing facts, PIT by acceptance time.", storage="postgres"),
    DatasetItem(name="universe", description="Point-in-time index membership (survivorship-free).", storage="postgres"),
]


@router.get("/features", response_model=list[FeatureDefinitionItem])
def features(consumer: Consumer = Depends(require_consumer),
             conn: psycopg.Connection = Depends(get_conn)) -> list[FeatureDefinitionItem]:
    """Every registered feature: name, version, spec, inputs, PIT semantics,
    implementing code_ref. The feature data dictionary + agent semantic layer."""
    rows = conn.execute(
        """
        select name, version, description, spec, inputs, pit_semantics, code_ref
          from meta.feature_definition
         where deprecated_at is null
         order by name, version
        """
    ).fetchall()
    return [FeatureDefinitionItem(
        name=r[0], version=r[1], description=r[2], spec=r[3],
        inputs=list(r[4]) if r[4] else None, pit_semantics=r[5], code_ref=r[6],
    ) for r in rows]


@router.get("/datasets", response_model=list[DatasetItem])
def datasets(consumer: Consumer = Depends(require_consumer)) -> list[DatasetItem]:
    """The datasets the platform publishes, with storage location."""
    return _DATASETS
"""
Trading-calendar connector (V0).

Populates ref.exchange + ref.trading_session from the `exchange_calendars` OSS
library for the XNYS (NYSE) session schedule, which defines the US equity/ETF
trading day (DEC-008 asset scope).

DELIBERATE TEMPLATE DEVIATION: unlike the vendor-feed connectors (edgar, fred,
openfigi), this one has no bronze landing and no parser. exchange_calendars is a
deterministic, versioned OSS library — there is no raw vendor payload to store
immutably (DEC-012 does not apply). Reproducibility comes from pinning the
library version (pyproject.toml) and recording it in meta.ingest_batch.params.
See client.py for the full rationale.
"""
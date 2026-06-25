"""
Alpaca EOD price-bar connector.

The first permanent Parquet producer (DEC-003): daily OHLCV bars land in the
silver Parquet lake, UNADJUSTED (DEC-004 — adjust on read once corporate actions
arrive). Batch lineage is written to Postgres meta.ingest_batch; the bars
themselves never touch Postgres. Symbols resolve to security_id via
resolve-and-skip (DEC-017) — this connector never creates an identity.

Bitemporal: every bar carries knowledge_time (= ingest fetch time, the moment we
could first have known it). A PIT read takes the latest knowledge_time <= as_of
per (security_id, session_date).
"""
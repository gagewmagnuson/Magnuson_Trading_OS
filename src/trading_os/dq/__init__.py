"""
Read-only data-quality diagnostics for the bars lake.

DQ asks questions; the engine answers them (consumes DuckDBStore, never writes).
Report-only: no schema, no persistence, no mutation. A future data-health
dashboard can consume the same structured results.

Two tiers (see SCHEMA.md / ROADMAP V1):
  FAIL   (run exits non-zero): coverage < 99%, OHLC sanity violations,
         duplicate (security_id, session_date, knowledge_time), listing-era
         gap leakage (a per-security-window invariant; nonzero => DQ bug).
  REPORT (never fails): interior session gaps, zero-volume bars, freshness.
"""
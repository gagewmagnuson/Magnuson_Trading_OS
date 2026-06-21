-- 006_backfill_cohort_source.sql
-- One-time provenance cleanup (DEC-017).
--
-- sec.security.source_id means the CREATOR of the identity. The 5 EDGAR
-- validation cohort (DEC-011) were created by the EDGAR connector, but the V0
-- OpenFIGI enrichment used to overwrite source_id, leaving them as OPENFIGI.
-- This restores their true creator. Enrichment provenance lives in
-- meta.ingest_batch (the OPENFIGI-sourced batch), not on the security row.
--
-- Idempotent: re-running sets the same 5 rows to SEC_EDGAR again (no-op).
-- After this, source_id means "creator" with zero exceptions:
--   coverage securities -> UNIVERSE,  legacy cohort -> SEC_EDGAR.
update sec.security
   set source_id = (select source_id from ref.data_source where name = 'SEC_EDGAR')
 where security_id in (
     sec.resolve_ticker('AAPL',  current_date),
     sec.resolve_ticker('MSFT',  current_date),
     sec.resolve_ticker('GOOGL', current_date),
     sec.resolve_ticker('AMZN',  current_date),
     sec.resolve_ticker('JPM',   current_date)
 );
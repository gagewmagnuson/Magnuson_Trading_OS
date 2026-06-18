"""
OpenFIGI connector — security identity enrichment for the Trading OS.

Purpose: replace the placeholder securities the EDGAR connector created (NULL
FIGI, generic description) with real, FIGI-anchored identities. FIGI is the
external immutable identifier that survives ticker changes and disambiguates
ticker reuse, which is the entire reason a security master exists (DEC-002).

Identity hierarchy:
    security_id  (internal immutable surrogate; THE join key)
        anchored by FIGI (external immutable)
        resolved through effective-dated TICKER / CUSIP / ISIN

Pipeline (same template as EDGAR/FRED):
    client  -> POST tickers to OpenFIGI mapping API, cache raw JSON to bronze
    parser  -> select the COMPOSITE FIGI + metadata from the response
    writer  -> ENRICH existing sec.security rows in place (reference data, not
               a fact table -> no append-only trigger; UPDATE is legitimate)
    cli     -> orchestrate the seeded ticker list

Notes:
  * sec.security is REFERENCE data: enriching identity metadata in place is a
    correction, not a historical revision. Existing fundamental_fact rows keep
    pointing at the same stable security_id.
  * No API key needed for the 5-ticker validation set (under OpenFIGI's keyless
    rate limit). A key slots into the .env pattern when scaling to a universe.
  * Bronze is immutable (DEC-012).
"""
"""
EDGAR connector — SEC Company Facts ingestion for the Trading OS.

Template for all future connectors. Pipeline:

    client   -> download raw Company Facts JSON to lake/bronze/ (immutable)
    parser   -> read bronze from disk, yield typed raw facts
    mapper   -> resolve us-gaap tags to canonical concepts (priority + confidence)
    writer   -> bitemporal append into Postgres; log unmapped/conflicts; batch row
    cli      -> orchestrate, scoped to the DEC-011 five-ticker validation set

Governing rules (see docs/DECISIONS.md):
  * Bronze is immutable (DEC-012): parse from disk, never re-download to "fix".
  * knowledge_time = SEC filing acceptance timestamp, never wall clock.
  * Append-only: corrections are new rows; never UPDATE/DELETE facts.
  * Map total-only for lease concepts; unmapped tags are logged, not dropped.
  * Five-ticker scope (DEC-011) is enforced in config until validation passes.
"""
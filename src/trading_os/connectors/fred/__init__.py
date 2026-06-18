"""
FRED/ALFRED connector — point-in-time macroeconomic data for the Trading OS.

Follows the same template as the EDGAR connector. Pipeline:

    client   -> download raw ALFRED vintage JSON to lake/bronze/ (immutable)
    parser   -> read bronze from disk, yield typed vintage observations
    writer   -> bitemporal append into macro.observation; batch row
    cli      -> orchestrate the seeded series list

Why ALFRED, not FRED (DEC-005):
  The default FRED endpoint returns only the LATEST value of each series. That
  would bake today's revised GDP into history as if it were known years ago —
  lookahead bias. ALFRED returns VINTAGES: every version of every observation
  with the real-time window it was the published figure. realtime_start maps to
  macro.observation.vintage_date (the knowledge_time axis); a revised series
  produces multiple bitemporal rows, exactly like EDGAR restatements.

Governing rules:
  * Bronze is immutable (DEC-012): parse from disk, never re-download to "fix".
  * vintage_date = ALFRED realtime_start = knowledge_time.
  * Append-only: revisions are new rows; never UPDATE/DELETE observations.
  * Full vintage history captured from day one (never delete).
"""
-- 009_seed_bars_dq_checks.sql
-- Seed the data-quality check definitions for the bars_eod dataset.
--
-- meta.data_quality_check holds check DEFINITIONS (what a check is); meta.dq_result
-- holds per-batch RESULTS (what a run observed). Until now both were empty — the
-- bars write path collected SkippedBar/ParseAnomaly objects but never persisted
-- them. These two checks let the bars CLIs record one summary dq_result per check
-- per batch.
--
-- The check's `spec` carries its acceptance policy as DATA (not hardcoded in the
-- writer): fail_fraction is the share of a batch that may be anomalous before the
-- batch is considered a genuine failure. A handful of skipped holiday/malformed
-- rows in a large batch is expected and healthy (passed=true, severity=warn); a
-- large share signals something actually wrong (passed=false, severity=error).
--
-- Idempotent: on conflict on the unique name, refresh the spec/severity.

insert into meta.data_quality_check (name, dataset, severity, spec, enabled) values
(
    'bars_non_session_date',
    'bars_eod',
    'warn',
    '{"description": "EOD bars dated to a non-session day (holiday/weekend), skipped by the writer''s knowledge_time gate. A few are normal for deep vendor history; many signal a calendar or vendor-date problem.", "source": "bars.writer.SkippedBar", "fail_fraction": 0.01}'::jsonb,
    true
),
(
    'bars_malformed_row',
    'bars_eod',
    'warn',
    '{"description": "Vendor rows with missing/non-numeric OHLCV or an unparseable date, skipped by the parser. Rare for a clean vendor; a spike signals a feed/schema change.", "source": "connectors.*.bars.ParseAnomaly", "fail_fraction": 0.01}'::jsonb,
    true
)
on conflict (name) do update
    set dataset  = excluded.dataset,
        severity = excluded.severity,
        spec     = excluded.spec,
        enabled  = excluded.enabled;
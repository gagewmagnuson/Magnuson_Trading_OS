-- 010_seed_gold_features.sql
-- Register the Phase 1 Gold features in meta.feature_definition (DEC-028).
--
-- Each row DOCUMENTS and VERSIONS a feature; it does not execute it (DEC-028
-- Decision 5 — a publisher, not an engine). The spec describes only the
-- MATHEMATICS of the feature — NOT the adjustment semantics of the prices it
-- consumes. Adjustment is a property of the input dataset (the canonical research
-- read surface, bars_eod_asof at its configured adjustment, currently 'split'),
-- documented once at the Gold-dataset level, not repeated per feature — SMA is
-- "the arithmetic mean over a window" regardless of what prices feed it.
--
-- inputs = the datasets/features consumed (all Phase 1 features consume bars_eod).
-- pit_semantics = how as-of applies to THIS feature's computation.
-- code_ref = the implementing function (trading_os.features.<fn>).
--
-- Idempotent: on conflict (name, version) refresh the descriptive fields.

insert into meta.feature_definition
    (name, version, description, spec, inputs, pit_semantics, code_ref) values
(
    'return_1d', 1, 'One-session simple return.',
    '{"function":"simple_return","period":1,"formula":"close[T]/close[T-1] - 1"}'::jsonb,
    array['bars_eod'],
    'Value at session T uses close[T] and close[T-1]; null for the first session.',
    'trading_os.features.simple_return'
),
(
    'log_return_1d', 1, 'One-session log return.',
    '{"function":"log_return","period":1,"formula":"ln(close[T]/close[T-1])"}'::jsonb,
    array['bars_eod'],
    'Value at session T uses close[T] and close[T-1]; null for the first session.',
    'trading_os.features.log_return'
),
(
    'sma20', 1, '20-session simple moving average of close.',
    '{"function":"sma","window":20,"formula":"arithmetic mean of close[T-19..T]"}'::jsonb,
    array['bars_eod'],
    'Value at T uses close[T-19..T]; null until 20 sessions exist.',
    'trading_os.features.sma'
),
(
    'sma50', 1, '50-session simple moving average of close.',
    '{"function":"sma","window":50,"formula":"arithmetic mean of close[T-49..T]"}'::jsonb,
    array['bars_eod'],
    'Value at T uses close[T-49..T]; null until 50 sessions exist.',
    'trading_os.features.sma'
),
(
    'ema20', 1, '20-span exponential moving average of close (adjust=false).',
    '{"function":"ema","span":20,"alpha":"2/(span+1)","formula":"EWMA of close, recursive from close[0]"}'::jsonb,
    array['bars_eod'],
    'Value at T uses all close[0..T] with exponentially decaying weight; seeded at close[0] (EWMA is defined from the first observation, so no null warm-up).',
    'trading_os.features.ema'
),
(
    'realized_vol20', 1, 'Annualized 20-session realized volatility of daily log returns.',
    '{"function":"realized_vol","window":20,"ddof":1,"annualization":"sqrt(252)","formula":"stddev(logret[T-19..T]) * sqrt(252)"}'::jsonb,
    array['bars_eod'],
    'Value at T uses the 20 log returns over close[T-20..T]; null until 21 closes exist. Sample std (ddof=1).',
    'trading_os.features.realized_vol'
),
(
    'roc20', 1, '20-session rate of change (price momentum).',
    '{"function":"roc","window":20,"formula":"close[T]/close[T-20] - 1"}'::jsonb,
    array['bars_eod'],
    'Value at T uses close[T] and close[T-20]; null for the first 20 sessions.',
    'trading_os.features.roc'
),
(
    'momentum_12_1', 1, 'Academic 12-1 momentum: ~12-month return excluding the most recent ~1 month.',
    '{"function":"momentum_12_1","skip":21,"lookback":252,"formula":"close[T-21]/close[T-252] - 1"}'::jsonb,
    array['bars_eod'],
    'Value at T uses close[T-21] and close[T-252] (skips the most recent 21 sessions to avoid short-term reversal); null until 252 prior sessions exist.',
    'trading_os.features.momentum_12_1'
)
on conflict (name, version) do update
    set description   = excluded.description,
        spec          = excluded.spec,
        inputs        = excluded.inputs,
        pit_semantics = excluded.pit_semantics,
        code_ref      = excluded.code_ref;
-- 011_pipeline_definition.sql
-- meta.pipeline_definition — the operational registry of recurring data pipelines.
--
-- A "pipeline" is a (source, dataset) unit that should run on a cadence. This is
-- OPERATIONAL metadata (how the OS runs), sitting beside ingest_batch/dq_result —
-- not reference data. The Health API reports one row per registered pipeline,
-- joining freshness from meta.ingest_batch on (source_id, dataset).
--
-- Declared, not observed: a pipeline that STOPS running still appears (as stale/
-- never-run), which is the operational signal an observed-only view would hide.
--
-- cadence      — expected frequency (a fact; consumers apply their own thresholds)
-- enabled      — is this pipeline currently supposed to run
-- retired      — permanently decommissioned (e.g. Alpaca EOD, superseded by Tiingo)
-- critical     — does its failure threaten downstream correctness (for "any critical
--                pipeline unhealthy?" queries later)
--
-- SCOPE (V1, bounded — see KI-004): only VENDOR-INGESTION pipelines that write
-- ingest_batch are registered. Internal workflows (Gold refresh, DQ, registry
-- refresh) log to incremental.log but not ingest_batch yet; promoting them to
-- first-class pipelines is deferred (KI-004).

create table if not exists meta.pipeline_definition (
    pipeline_id   bigint generated always as identity primary key,
    source_id     bigint not null references ref.data_source(source_id),
    dataset       text   not null,
    cadence       text   not null check (cadence in ('daily','weekly','monthly','on-demand')),
    description   text,
    enabled       boolean not null default true,
    retired       boolean not null default false,
    critical      boolean not null default false,
    created_at    timestamptz not null default now(),
    unique (source_id, dataset)
);

-- Seed the current vendor-ingestion pipelines, matched exactly to the
-- (source_id, dataset) pairs present in meta.ingest_batch.
insert into meta.pipeline_definition
    (source_id, dataset, cadence, description, enabled, retired, critical) values
(8, 'bars_eod',                  'daily',      'Tiingo EOD price bars — the moat clock.',       true,  false, true),
(8, 'corporate_actions',         'on-demand',  'Tiingo splits/dividends for on-read adjustment.', true, false, true),
(2, 'macro.observation',         'weekly',     'FRED macro series with ALFRED vintages.',       true,  false, false),
(1, 'fund.fundamental_fact',     'weekly',     'SEC EDGAR company facts (PIT by acceptance).',  true,  false, false),
(5, 'univ.universe_membership',  'on-demand',  'S&P 500 PIT membership intervals.',             true,  false, false),
(5, 'sec.security',              'on-demand',  'Universe-driven security master seeding.',      true,  false, false),
(3, 'sec.security',              'on-demand',  'OpenFIGI symbology enrichment.',                true,  false, false),
(4, 'ref.trading_session',       'on-demand',  'Exchange calendar sessions.',                   true,  false, false),
(6, 'bars_eod',                  'daily',      'Alpaca EOD — retired, superseded by Tiingo (DEC-026).', false, true, false)
on conflict (source_id, dataset) do update
    set cadence     = excluded.cadence,
        description = excluded.description,
        enabled     = excluded.enabled,
        retired     = excluded.retired,
        critical    = excluded.critical;
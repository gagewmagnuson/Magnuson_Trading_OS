-- 012_pipeline_kind.sql
-- Move `kind` from ref.data_source (per-vendor) to meta.pipeline_definition
-- (per-pipeline). A vendor can feed multiple pipelines of different kinds —
-- Tiingo serves both prices (bars_eod) and corporate_actions — so kind is a
-- property of the pipeline, not the source. The health API reads p.kind.

alter table meta.pipeline_definition
    add column if not exists kind text;

-- Backfill each pipeline's true kind (by source + dataset).
update meta.pipeline_definition p
   set kind = case
       when p.dataset = 'bars_eod'                 then 'prices'
       when p.dataset = 'corporate_actions'        then 'corporate_actions'
       when p.dataset = 'macro.observation'        then 'macro'
       when p.dataset = 'fund.fundamental_fact'    then 'fundamentals'
       when p.dataset = 'univ.universe_membership' then 'universe'
       when p.dataset = 'sec.security'             then 'reference'
       when p.dataset = 'ref.trading_session'      then 'reference'
       else 'reference'
   end;

alter table meta.pipeline_definition
    alter column kind set not null;
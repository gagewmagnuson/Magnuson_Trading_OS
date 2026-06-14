-- =====================================================================
-- Trading OS — V0 Schema (PostgreSQL 15+)
-- Repo path: /schema/postgres/001_v0.sql
--
-- GOVERNING PRINCIPLE (do not violate without amending DECISIONS.md):
--   Fact tables are APPEND-ONLY and BITEMPORAL.
--     * event_time   = when the fact refers to (period end, ex-date, bar date)
--     * knowledge_time = when we could first have known it (filing ts, vintage)
--   Corrections/restatements are NEW ROWS with a later knowledge_time.
--   No UPDATE, no DELETE on fact tables. Ever.
--   A point-in-time query is therefore: "rows where knowledge_time <= :as_of,
--   take the latest per business key." This guarantees no lookahead bias and
--   full reproducibility.
--
-- STORE BOUNDARY (see SCHEMA.md):
--   PostgreSQL  = system of record for reference + low/medium-volume facts
--                 (this file). Constraints, transactions, easy as-of queries.
--   Parquet lake = high-volume time series (price bars) + raw vendor landing.
--   DuckDB       = analytical engine over Parquet; can ATTACH this Postgres
--                 for cross-store as-of joins. No data is duplicated.
--
-- SCALE-LATER GUARANTEE:
--   All join keys are surrogate internal IDs (never tickers, never reused).
--   Bitemporal columns are named identically here and in Parquet, so any
--   table can be exported to Parquet / loaded into ClickHouse with the same
--   semantics and the same as-of SQL. Nothing here forces a future redesign.
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- Extensions & namespaces
-- ---------------------------------------------------------------------
create extension if not exists pgcrypto;   -- gen_random_uuid(), if needed

create schema if not exists ref;    -- reference data, calendars, sources
create schema if not exists sec;     -- security master / symbology
create schema if not exists corp;    -- corporate actions
create schema if not exists macro;   -- macro series + vintages
create schema if not exists fund;    -- PIT fundamentals
create schema if not exists univ;    -- universe / index membership
create schema if not exists meta;    -- catalog, lineage, data quality

comment on schema sec  is 'Security master. The internal security_id is the ONLY safe join key. Never join on ticker.';
comment on schema meta is 'Metadata catalog: ingest lineage, feature definitions, data-quality state, migrations.';

-- ---------------------------------------------------------------------
-- Append-only guard: attach to every FACT table to block UPDATE/DELETE.
-- ---------------------------------------------------------------------
create or replace function meta.deny_mutation()
returns trigger language plpgsql as $$
begin
  raise exception
    'Table %.% is append-only. % is not permitted; record a correction as a NEW row with a later knowledge_time.',
    tg_table_schema, tg_table_name, tg_op;
end;
$$;

-- =====================================================================
-- META: catalog, lineage, data quality  (built first; everything references it)
-- =====================================================================

-- Every ingest run gets a row here. Every fact row points back to its batch.
-- This is the backbone of reproducibility: given a batch_id you can replay
-- exactly which source, params, and knowledge_time produced a set of rows.
create table meta.ingest_batch (
    batch_id          bigint generated always as identity primary key,
    source_id         bigint not null,                 -- FK added after ref.data_source exists
    dataset           text   not null,                 -- e.g. 'fund.fundamental_fact'
    started_at        timestamptz not null default now(),
    finished_at       timestamptz,
    status            text   not null default 'running'
                       check (status in ('running','succeeded','failed','partial')),
    knowledge_time    timestamptz not null,            -- knowledge_time assigned to facts in this batch
    rows_in           bigint,
    rows_out          bigint,
    params            jsonb  not null default '{}'::jsonb,  -- query window, vendor cursor, etc.
    code_version      text,                            -- git sha of the connector that ran
    error             text,
    created_at        timestamptz not null default now()
);
comment on table  meta.ingest_batch is 'One row per ingestion run. Source of lineage and reproducibility.';
comment on column meta.ingest_batch.knowledge_time is 'The knowledge_time stamped onto facts produced by this batch.';

-- Versioned analytics/feature definitions. A backtest records which version
-- it used; changing a definition creates a NEW version (never mutate v1).
create table meta.feature_definition (
    feature_id        bigint generated always as identity primary key,
    name              text   not null,
    version           int    not null,
    description       text   not null,
    spec              jsonb  not null,                 -- formula/params, machine-readable
    inputs            text[] not null default '{}',    -- datasets/features consumed
    pit_semantics     text   not null,                 -- how as-of is applied
    code_ref          text,                            -- git sha / module path
    created_at        timestamptz not null default now(),
    deprecated_at     timestamptz,
    unique (name, version)
);
comment on table meta.feature_definition is 'Versioned, reproducible analytics definitions. Append a new version; never edit a released one.';

-- Data-quality checks and their results (freshness, gaps, null rates, anomalies).
create table meta.data_quality_check (
    check_id          bigint generated always as identity primary key,
    name              text not null unique,
    dataset           text not null,
    severity          text not null default 'warn' check (severity in ('info','warn','error')),
    spec              jsonb not null,
    enabled           boolean not null default true
);

create table meta.dq_result (
    result_id         bigint generated always as identity primary key,
    check_id          bigint not null references meta.data_quality_check(check_id),
    batch_id          bigint references meta.ingest_batch(batch_id),
    run_at            timestamptz not null default now(),
    passed            boolean not null,
    observed          jsonb,                           -- metrics captured
    details           text
);
create index dq_result_check_run_idx on meta.dq_result (check_id, run_at desc);

-- Schema migration ledger (so the team and the agent share one truth).
create table meta.schema_migration (
    version           text primary key,
    applied_at        timestamptz not null default now(),
    description       text
);

-- =====================================================================
-- REF: data sources, exchanges, calendar
-- =====================================================================

create table ref.data_source (
    source_id         bigint generated always as identity primary key,
    name              text not null unique,            -- 'SEC_EDGAR','FRED','ALPACA','OPENFIGI'
    kind              text not null,                   -- 'fundamentals','macro','prices','reference'
    is_redistributable boolean not null default false, -- legal flag: can output be resold?
    base_url          text,
    license_notes     text,
    created_at        timestamptz not null default now()
);
comment on column ref.data_source.is_redistributable is 'TRUE only for public-domain sources (EDGAR, FRED). Gates any future monetization of raw data.';

alter table meta.ingest_batch
    add constraint ingest_batch_source_fk
    foreign key (source_id) references ref.data_source(source_id);

create table ref.exchange (
    exchange_id       bigint generated always as identity primary key,
    mic               text not null unique,            -- ISO 10383 Market Identifier Code, e.g. 'XNAS','XNYS','ARCX'
    name              text not null,
    country           text,
    timezone          text not null                    -- IANA tz, e.g. 'America/New_York'
);

-- Minimal calendar. The `exchange_calendars` Python lib is the runtime source
-- of truth; this table caches sessions for SQL-side joins/resampling.
create table ref.trading_session (
    exchange_id       bigint not null references ref.exchange(exchange_id),
    session_date      date   not null,
    open_utc          timestamptz not null,
    close_utc         timestamptz not null,
    is_half_day       boolean not null default false,
    primary key (exchange_id, session_date)
);

-- =====================================================================
-- SEC: security master / symbology  (the ONLY safe join key is security_id)
-- =====================================================================

-- One row per distinct security entity. security_id is internal, surrogate,
-- and NEVER reused — even after delisting. This is what every other table
-- joins on, which makes ticker reuse harmless.
create table sec.security (
    security_id       bigint generated always as identity primary key,
    figi              text,                            -- OpenFIGI: stable anchor identifier
    composite_figi    text,
    security_type     text not null check (security_type in ('EQUITY','ETF')),
    description       text,
    currency          text not null default 'USD',
    country           text,
    is_active         boolean not null default true,   -- derived: any open listing?
    first_seen        timestamptz not null default now(),
    source_id         bigint references ref.data_source(source_id),
    unique (figi)
);
comment on table sec.security is 'Canonical security entity. security_id is stable, surrogate, never reused. JOIN ON THIS, not on ticker.';

-- Effective-dated identifiers (ticker, CUSIP, ISIN). Tickers change and get
-- reused; identifiers are bitemporal so resolution is correct as of any date.
create table sec.security_identifier (
    identifier_id     bigint generated always as identity primary key,
    security_id       bigint not null references sec.security(security_id),
    id_type           text   not null check (id_type in ('TICKER','CUSIP','ISIN','FIGI')),
    id_value          text   not null,
    valid_from        date   not null,                 -- event-time: when this id became effective
    valid_to          date,                            -- null = still effective
    knowledge_time    timestamptz not null default now(),
    batch_id          bigint references meta.ingest_batch(batch_id),
    check (valid_to is null or valid_to > valid_from)
);
create index sec_ident_lookup_idx
    on sec.security_identifier (id_type, upper(id_value), valid_from desc);
create index sec_ident_secid_idx
    on sec.security_identifier (security_id, id_type);

-- Listings on an exchange. Delistings are recorded by setting valid_to, never
-- by deleting the row (survivorship-bias protection).
create table sec.listing (
    listing_id        bigint generated always as identity primary key,
    security_id       bigint not null references sec.security(security_id),
    exchange_id       bigint not null references ref.exchange(exchange_id),
    ticker            text   not null,
    valid_from        date   not null,
    valid_to          date,                            -- null = currently listed
    knowledge_time    timestamptz not null default now(),
    batch_id          bigint references meta.ingest_batch(batch_id),
    check (valid_to is null or valid_to > valid_from)
);
create index sec_listing_secid_idx on sec.listing (security_id);

-- =====================================================================
-- CORP: corporate actions  (append-only; adjusted prices computed on read)
-- =====================================================================

create table corp.corporate_action (
    action_id         bigint generated always as identity primary key,
    security_id       bigint not null references sec.security(security_id),
    action_type       text   not null check (action_type in
                         ('SPLIT','CASH_DIVIDEND','STOCK_DIVIDEND','SPINOFF',
                          'MERGER','SYMBOL_CHANGE','RIGHTS','OTHER')),
    declared_date     date,                            -- when announced (knowledge-relevant)
    ex_date           date not null,                   -- event-time anchor for price adjustment
    record_date       date,
    pay_date          date,
    -- split: numerator/denominator (2-for-1 => 2/1). dividend: cash_amount.
    split_from        numeric,
    split_to          numeric,
    cash_amount       numeric,
    currency          text default 'USD',
    knowledge_time    timestamptz not null default now(),
    source_id         bigint references ref.data_source(source_id),
    batch_id          bigint references meta.ingest_batch(batch_id)
);
create index corp_action_secid_ex_idx on corp.corporate_action (security_id, ex_date);
comment on table corp.corporate_action is 'Raw corporate actions. Store unadjusted prices elsewhere; derive adjustment factors from these and apply ON READ.';

-- =====================================================================
-- MACRO: series + vintages (ALFRED-style point-in-time macro)
-- =====================================================================

create table macro.series (
    series_id         text primary key,                -- FRED id, e.g. 'CPIAUCSL'
    title             text not null,
    units             text,
    frequency         text,                            -- 'D','W','M','Q','A'
    seasonal_adj      text,
    source_id         bigint references ref.data_source(source_id)
);

-- Each (series, obs_date) can have MANY rows — one per release/revision.
-- vintage_date IS the knowledge_time: what the number "was" when published.
create table macro.observation (
    obs_id            bigint generated always as identity primary key,
    series_id         text not null references macro.series(series_id),
    obs_date          date not null,                   -- event-time: period the value refers to
    value             numeric,                         -- numeric, never float
    vintage_date      date not null,                   -- knowledge_time (date the value was knowable)
    realtime_start    date,                            -- ALFRED fidelity
    realtime_end      date,
    batch_id          bigint references meta.ingest_batch(batch_id),
    unique (series_id, obs_date, vintage_date)
);
create index macro_obs_asof_idx on macro.observation (series_id, obs_date, vintage_date desc);
comment on table macro.observation is 'Bitemporal macro. Multiple rows per (series,obs_date) = revisions. As-of = latest vintage_date <= cutoff.';

-- =====================================================================
-- FUND: point-in-time fundamentals (EDGAR/Sharadar)
-- =====================================================================

-- Canonical concept dictionary: normalizes vendor/XBRL tags to one vocabulary.
-- period_type is DERIVED, not stored: a concept is a flow when its facts carry
-- a period_start, and an instant (balance-sheet) when they do not. See DEC-009.
create table fund.concept (
    concept_id        bigint generated always as identity primary key,
    canonical_name    text not null unique,            -- 'revenue','net_income','total_assets'
    statement         text not null check (statement in ('income','balance','cashflow','other')),
    description       text,
    research_status   text not null default 'experimental'
                       check (research_status in ('core','experimental','deprecated','custom')),
    expected_unit     text                             -- 'USD','USD/shares','shares'; NULL = no unit constraint
);
comment on column fund.concept.research_status is
    'Governance status. Models filter to core by default. New concepts start experimental; promote via DECISIONS.md amendment.';
comment on column fund.concept.expected_unit is
    'Expected unit for this concept (USD, USD/shares, shares). Connector rejects facts whose reported unit does not match. NULL = no unit constraint.';

-- Maps raw source tags (e.g. us-gaap:Revenues) to a canonical concept.
-- priority: lower = higher precedence. Connector walks aliases in priority
-- order and takes the first tag present in the filing (DEC-009).
-- NOTE: these aliases are Company-Facts-API-shaped (taxonomy + tag). A future
-- raw-XBRL path (V2/V3) gets its own alias rows discriminated by source_id.
create table fund.concept_alias (
    alias_id          bigint generated always as identity primary key,
    concept_id        bigint not null references fund.concept(concept_id),
    source_id         bigint not null references ref.data_source(source_id),
    source_tag        text not null,
    priority          int not null default 100,        -- lower = higher precedence
    unique (source_id, source_tag)
);
comment on column fund.concept_alias.priority is
    'Tag resolution order within a concept. Lower number = higher priority. Connector takes first matching tag in priority order.';

-- One row per filing. filed_at (acceptance timestamp) is the knowledge_time
-- driver for every fact in the filing — NOT the period end date.
create table fund.filing (
    filing_id         bigint generated always as identity primary key,
    security_id       bigint not null references sec.security(security_id),
    form_type         text not null,                   -- '10-K','10-Q','8-K'
    period_end_date   date not null,                   -- event-time
    fiscal_period     text,                            -- 'Q1','Q2','Q3','FY'
    filed_at          timestamptz not null,            -- SEC acceptance datetime = knowledge_time
    accession_number  text,                            -- EDGAR accession
    url               text,
    source_id         bigint references ref.data_source(source_id),
    batch_id          bigint references meta.ingest_batch(batch_id),
    unique (accession_number)
);
create index fund_filing_secid_idx on fund.filing (security_id, period_end_date);

-- Unknown XBRL tags from EDGAR filings. NEVER discarded. Reviewed periodically;
-- promoted to concept_alias via governance amendment (DEC-009).
create table fund.unmapped_tag (
    unmapped_id       bigint generated always as identity primary key,
    filing_id         bigint not null references fund.filing(filing_id),
    tag               text not null,
    value             numeric,
    unit              text,
    context_ref       text,                            -- XBRL contextRef attribute
    logged_at         timestamptz not null default now()
);
create index unmapped_tag_filing_idx on fund.unmapped_tag(filing_id);
create index unmapped_tag_tag_idx    on fund.unmapped_tag(tag);

-- Rule #4 (DEC-009): when a filing reports TWO valid, mapped tags for the same
-- concept/period and they materially disagree, do NOT guess. Log the conflict,
-- skip the fact, continue ingest. This audit trail is what makes the OS
-- institution-grade: a strange model result years from now can be traced here.
create table fund.concept_conflict (
    conflict_id       bigint generated always as identity primary key,
    filing_id         bigint not null references fund.filing(filing_id),
    concept_id        bigint not null references fund.concept(concept_id),
    period_start      date,
    period_end_date   date not null,
    tag_a             text not null,
    value_a           numeric,
    tag_b             text not null,
    value_b           numeric,
    unit              text,
    logged_at         timestamptz not null default now()
);
create index concept_conflict_filing_idx  on fund.concept_conflict(filing_id);
create index concept_conflict_concept_idx on fund.concept_conflict(concept_id);
comment on table fund.concept_conflict is
    'Two valid mapped tags for one concept/period that materially disagree. Logged, not resolved. Connector skips the fact and continues. Reviewed via governance.';

-- The facts. Restatements arrive as NEW rows tied to a later filing
-- (later filed_at). Never overwrite a prior reported value.
-- period_start distinguishes flows from instants and is part of a flow fact's
-- identity: a 3-month and a 12-month revenue for the same period_end are
-- DIFFERENT facts, stored side by side, never derived from one another (DEC-009).
create table fund.fundamental_fact (
    fact_id           bigint generated always as identity primary key,
    security_id       bigint not null references sec.security(security_id),
    filing_id         bigint not null references fund.filing(filing_id),
    concept_id        bigint not null references fund.concept(concept_id),
    period_start      date,                            -- NULL = instant (balance sheet); NOT NULL = flow (income/cash flow). Duration = identity of a flow fact.
    period_end_date   date not null,                   -- event-time (denormalized from filing for fast as-of)
    fiscal_period     text,
    value             numeric,                         -- numeric, never float
    unit              text,                            -- 'USD','shares','USD/share'
    filed_at          timestamptz not null,            -- knowledge_time (denormalized from filing)
    is_restatement    boolean not null default false,
    batch_id          bigint references meta.ingest_batch(batch_id)
);
-- The critical as-of index: per (security, concept, period_end, period_start),
-- newest-known first. period_start is in the key because duration is part of a
-- flow fact's identity.
create index fund_fact_asof_idx
    on fund.fundamental_fact (security_id, concept_id, period_end_date, period_start, filed_at desc);

-- =====================================================================
-- UNIV: universe / index membership (bitemporal)
-- =====================================================================

create table univ.universe (
    universe_id       bigint generated always as identity primary key,
    code              text not null unique,            -- 'SP500','RUSSELL2000','CUSTOM_LIQUID_US'
    description       text
);

-- Membership has BOTH a validity interval (event-time: when the security was
-- in the index) AND a knowledge_time (when we learned of the membership).
create table univ.universe_membership (
    membership_id     bigint generated always as identity primary key,
    universe_id       bigint not null references univ.universe(universe_id),
    security_id       bigint not null references sec.security(security_id),
    valid_from        date not null,                   -- event-time: entered index
    valid_to          date,                            -- null = still a member
    knowledge_time    timestamptz not null default now(),
    batch_id          bigint references meta.ingest_batch(batch_id),
    check (valid_to is null or valid_to > valid_from)
);
create index univ_member_asof_idx
    on univ.universe_membership (universe_id, valid_from, knowledge_time desc);

-- =====================================================================
-- AS-OF HELPERS  (the only correct way to read facts)
-- =====================================================================

-- Resolve a ticker to its internal security_id as of a date. ALWAYS do this
-- before joining anything; never join on the ticker string directly.
create or replace function sec.resolve_ticker(p_ticker text, p_as_of date)
returns bigint language sql stable as $$
    select security_id
    from sec.security_identifier
    where id_type = 'TICKER'
      and upper(id_value) = upper(p_ticker)
      and valid_from <= p_as_of
      and (valid_to is null or valid_to > p_as_of)
    order by valid_from desc
    limit 1
$$;

-- Point-in-time fundamentals: latest value known on/before p_as_of,
-- per (security, concept, period_end, period_start). period_start is in the
-- key so a quarterly fact and an annual fact for the same period_end are
-- returned as distinct rows rather than one shadowing the other.
create or replace function fund.fundamentals_asof(p_as_of timestamptz)
returns table (
    security_id     bigint,
    concept_id      bigint,
    period_start    date,
    period_end_date date,
    fiscal_period   text,
    value           numeric,
    unit            text,
    filed_at        timestamptz
) language sql stable as $$
    select distinct on (security_id, concept_id, period_end_date, period_start)
           security_id, concept_id, period_start, period_end_date, fiscal_period, value, unit, filed_at
    from fund.fundamental_fact
    where filed_at <= p_as_of
    order by security_id, concept_id, period_end_date, period_start, filed_at desc
$$;

-- Point-in-time macro: latest vintage on/before p_as_of, per (series, obs_date).
create or replace function macro.observations_asof(p_as_of date)
returns table (
    series_id   text,
    obs_date    date,
    value       numeric,
    vintage_date date
) language sql stable as $$
    select distinct on (series_id, obs_date)
           series_id, obs_date, value, vintage_date
    from macro.observation
    where vintage_date <= p_as_of
    order by series_id, obs_date, vintage_date desc
$$;

-- Point-in-time universe membership.
create or replace function univ.members_asof(p_universe_code text, p_as_of date)
returns table (security_id bigint) language sql stable as $$
    select m.security_id
    from univ.universe_membership m
    join univ.universe u on u.universe_id = m.universe_id
    where u.code = p_universe_code
      and m.knowledge_time::date <= p_as_of
      and m.valid_from <= p_as_of
      and (m.valid_to is null or m.valid_to > p_as_of)
$$;

-- =====================================================================
-- ATTACH APPEND-ONLY GUARDS to fact tables
-- =====================================================================
do $$
declare t text;
begin
  foreach t in array array[
    'sec.security_identifier','sec.listing',
    'corp.corporate_action',
    'macro.observation',
    'fund.filing','fund.fundamental_fact',
    'univ.universe_membership',
    'meta.dq_result'
  ] loop
    execute format(
      'create trigger %s_no_mutation before update or delete on %s
         for each row execute function meta.deny_mutation();',
      replace(t, '.', '_'), t);
  end loop;
end;
$$;

insert into meta.schema_migration(version, description)
values ('v0.1.0', 'Trading OS V0: bitemporal core, security master, corp actions, macro vintages, PIT fundamentals + concept dictionary (research_status, expected_unit, priority, period_start, unmapped_tag, concept_conflict), catalog, universe.');

commit;

-- =====================================================================
-- USAGE NOTES
--   * Read facts ONLY through the *_asof helpers (or the same DISTINCT ON
--     pattern). Selecting raw rows without a knowledge_time filter will
--     reintroduce lookahead bias.
--   * To "correct" data: INSERT a new row with a later knowledge_time. The
--     append-only triggers will block any UPDATE/DELETE.
--   * period_start is part of a flow fact's identity. NULL = instant (balance
--     sheet); NOT NULL = flow. Never derive annual from quarters — store both
--     as reported, each with its own [period_start, period_end_date].
--   * Price bars do NOT live here — they live in the Parquet lake (see
--     SCHEMA.md) with identical bitemporal columns, queried via DuckDB.
-- =====================================================================
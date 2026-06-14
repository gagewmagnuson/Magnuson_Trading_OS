# ROADMAP.md — Trading OS Phase Plan

Each version has a concrete trigger for completion and a defined gate before
the next phase begins. Scope does not expand mid-phase without an explicit
decision recorded in `DECISIONS.md`.

---

## V0 — Foundations (current phase)

**Goal:** Prove the bitemporal model works end-to-end on real data.
No serving API. No price bars. No model consuming the data.

**Gate to V1:** Every V0 deliverable below is checked off, and a DuckDB
cross-store PIT query runs correctly against live EDGAR and FRED data.

### Deliverables

- [ ] Governing documents written and reviewed (VISION, ARCHITECTURE, ROADMAP, DECISIONS, SCHEMA)
- [ ] `schema/postgres/001_trading_os_v0.sql` applied and verified
- [ ] Parquet lake directory structure initialized
- [ ] DuckDB attaches Postgres; a cross-store `*_asof()` query runs correctly
- [ ] Security master seeded via OpenFIGI — US equities and ETFs
- [ ] Trading calendar populated via `exchange_calendars`
- [ ] FRED / ALFRED macro ingest connector (free, public)
  - Stores vintages: multiple rows per `(series_id, obs_date)` are correct
  - `macro.observations_asof()` tested for PIT correctness
- [ ] SEC EDGAR fundamentals ingest connector (free, public)
  - Concept dictionary design proposal written and approved before mapping data is written
  - `fund.fundamentals_asof()` tested for PIT correctness (knowledge_time = acceptance timestamp)
- [ ] `meta.ingest_batch` written for every ingest run
- [ ] Data-quality checks: freshness alerts, null-rate thresholds
- [ ] At least one test per connector covering lookahead-bias correctness

**Out of scope for V0 (do not add):**
Price bars, options, futures, FX, crypto, news, the serving API, the UI,
any paid data source, Dagster (cron is sufficient), multi-tenant anything.

---

## V1 — First Complete Data Layer

**Goal:** A model or agent outside this system can consume clean, PIT-correct
data via a stable API. EOD and minute price history is available for US
equities and ETFs.

**Trigger to start:** V0 gate is passed.
**Gate to V2:** A live or paper trading model successfully queries V1 data via
the serving API for at least 30 days without data correctness issues.

### Planned scope

- Alpaca free-tier EOD price bar ingest (unadjusted storage; adjust on read)
- Alpaca free-tier minute bar ingest (unadjusted; same bitemporal model as EOD)
- `bars_eod` and `bars_minute` Parquet silver layers with correct `knowledge_time`
- Corporate action ingest — splits and dividends — and the on-read adjustment function
- Universe membership snapshots (`univ.members_asof()`)
- FastAPI serving layer — read-only, `as_of` parameter on every endpoint
- API key / consumer authentication (simple, not multi-tenant)
- UI: data-health dashboard and catalog explorer (internal diagnostics only)
- Expanded data-quality coverage for price bars
- Dagster orchestration (replace cron if DAG complexity warrants it)

**Still out of scope for V1:**
Options, futures, FX, crypto, news, real-time streaming, paid APIs,
cloud infrastructure.

---

## V2 — Derivatives Foundation

**Goal:** Options data is available for V1-covered underlyings.

**Trigger to start:** V1 gate is passed (model consuming V1 data for 30 days).
**Gate to V3:** Options data passes PIT correctness tests and at least one
model consumes it.

### Planned scope

- Options chain ingest (source TBD; must be free or cost-justified at this stage)
- Greeks computed on read (not stored)
- Options schema added to Postgres; full bitemporal model applies
- Expiration calendar integration

**DEC-008 status at V2:** revisit asset-scope policy with a new DECISIONS.md entry.

---

## V3 — Real-Time and Additional Asset Classes

**Goal:** Streaming ingest, sub-day data, and non-equity asset classes.

**Trigger to start:** V2 gate is passed and there is a clear model use case
requiring real-time data.

### Planned scope (subject to revision)

- Streaming infrastructure (Kafka or equivalent — requires explicit decision)
- Futures and FX data (source TBD)
- Crypto data (source TBD)
- News / NLP pipeline (source TBD)
- ClickHouse migration path for high-volume Postgres fact tables if latency warrants it
- Cloud infrastructure if scale warrants it
- Multi-tenant entitlements if team size warrants it

V3 scope is intentionally loose. The exact shape depends on which models
are running on V1/V2 data and what data gaps they expose. Do not build
V3 components speculatively.

---

## Amendment log

(Add entries here when a phase gate or scope item changes. Do not edit the
original entries — append only.)

| Date | Item | Change | Reason |
|---|---|---|---|
| 2026-06-13 | V1 scope | Added minute-bar ingest (moved from V3) | Alpaca free tier provides minute bars; intraday research is a priority given PDT rule removal |

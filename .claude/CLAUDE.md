Trading OS — Claude Code Standing Instructions

This file is read automatically at the start of every Claude Code session.
It is a binding governing document. All instructions here apply unless
explicitly overridden by the developer in that session.


What this project is

A Bloomberg-like financial intelligence platform: a centralized data layer
for systematic trading research. It gathers, normalizes, enriches, stores,
and serves financial data via API.

This project is NOT and will NEVER contain:


Trading models or signals
Execution engines
Portfolio management systems
Order routing


Models, agents, backtests, and execution live entirely outside this system
and consume its data via API.


The governing documents (read before doing anything)

Before any session involving implementation, read these files in order:


docs/DECISIONS.md — hard constraints, never violate
docs/ARCHITECTURE.md — system design, do not redesign without approval
docs/VISION.md — purpose and goals
docs/ROADMAP.md — what is in scope for the current phase
docs/SCHEMA.md — data model, bitemporal rules, store boundaries


If any of these files do not exist yet, say so and do not proceed with
implementation until they are created.


The workflow: propose first, implement second

For any non-trivial task (new module, new schema, new connector, new
dependency), follow this sequence:


Read the relevant governing documents
Propose the approach: what you will build, what files you will
create or modify, what dependencies you will add
Wait for explicit approval ("approved", "go ahead", "looks good")
Implement only what was approved
Report what was done and flag anything unexpected


Do not skip step 3. "I'll just do it quickly" is not a reason to skip approval.


Hard constraints (do not violate for any reason)

Architecture


The bitemporal model is frozen. event_time + knowledge_time on every
fact. Append-only fact tables. No UPDATE, no DELETE on fact tables.
Join on security_id (internal surrogate). Never join on ticker strings.
Read facts only through *_asof() helpers or the DISTINCT ON pattern.
Never query fact tables without a knowledge_time filter.
Unadjusted prices only in storage. Adjust on read from corporate actions.


Dependencies


Do not add a new dependency without proposing it and getting approval.
The approved V0 stack is: Python, PostgreSQL, DuckDB, Parquet, Polars,
Dagster (or cron for V0), FastAPI, exchange_calendars, OpenFIGI API.
Do not add: Redis, Kafka, Kubernetes, cloud infrastructure, paid APIs,
ClickHouse, Spark, or any streaming infrastructure. These are V3+ concerns.


Budget


Zero marginal cost until proof of concept. Every data source must be
free (EDGAR, FRED/ALFRED, Alpaca free tier, OpenFIGI, exchange_calendars).
Flag any step that would incur cost.


Code quality


Every function that reads from a fact table must have an as_of parameter.
Every ingest function must write a meta.ingest_batch row (started,
finished, status, rows_in, rows_out, knowledge_time).
Schema migrations go in schema/postgres/ as numbered SQL files.
Tests go in tests/. Do not consider a component done without at least
one test covering the PIT (point-in-time) correctness of its reads.



What is in scope for V0 (current phase)

V0 is foundations only. The deliverables are:


 Governing documents (VISION, ARCHITECTURE, ROADMAP, DECISIONS)
 PostgreSQL schema applied and verified (schema/postgres/001_v0.sql)
 Parquet lake directory structure initialized
 DuckDB cross-store query verified
 Security master seeded via OpenFIGI (US equities/ETFs)
 Trading calendar populated via exchange_calendars
 FRED/ALFRED macro ingestion connector (free, public)
 SEC EDGAR fundamentals ingestion connector (free, public)
 Data-quality checks for freshness and null rates
 *_asof() functions tested for lookahead-bias correctness


V0 does NOT include: price bars, options, futures, FX, crypto, news, the
serving API, the UI, or any paid data source.


The concept dictionary is a protected component

The fund.concept / fund.concept_alias tables require special care.
XBRL tag mapping is complex; a wrong mapping silently corrupts all
fundamental analysis downstream.

Do not:


Create placeholder concept mappings
Auto-generate concept mappings without review
Assume XBRL tags are interchangeable
Mark this component "done" with fewer than the core income statement,
balance sheet, and cash flow concepts fully reviewed


Before implementing: produce a design proposal covering (a) what the
initial concept vocabulary will be, (b) how raw us-gaap tags map to it,
(c) how custom company extensions are handled, (d) how to add new concepts
later. Wait for approval before writing any mapping data.


If you are uncertain

Say so. Do not guess at architecture. Do not invent data source APIs.
Do not assume a vendor's schema — ask the developer to provide a sample
response, or fetch the documented schema from the official source.

The developer's rule: Claude proposes. Developer approves.
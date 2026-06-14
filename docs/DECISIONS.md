DECISIONS.md — Trading OS Architectural Decisions

This file is the authoritative record of design decisions that have been made
and reviewed. Claude Code treats every entry here as a frozen constraint.

To amend a decision: add a new dated entry explaining the change and why.
Do not delete or edit prior entries — they are the audit trail.


DEC-001 — Bitemporal, append-only fact model

Date: 2026-06

Status: Frozen

Every fact table carries two time axes:


event_time — when the fact refers to (period end, bar date, ex-date)
knowledge_time — when it was first knowable (filing acceptance ts, vintage)


Facts are immutable. Corrections are new rows with a later knowledge_time.
UPDATE and DELETE are blocked by database triggers on all fact tables.

Rationale: This is the only model that guarantees no lookahead bias in
backtests. It cannot be retrofitted later without reprocessing all history.
Getting it wrong means all research built on this platform is silently wrong.

Consequences:


Read-through *_asof() helpers only. Never query fact tables without a
knowledge_time filter.
The meta.ingest_batch table records every run's knowledge_time for
full replay and audit.


Do not change this decision.


DEC-002 — Internal surrogate security_id as the universal join key

Date: 2026-06

Status: Frozen

sec.security.security_id is the only safe join key across all tables.
Tickers are identifiers stored in sec.security_identifier with effective
dates. They are never used as join keys.

Rationale: Tickers are reused and reassigned when companies delist and
new companies list under the same symbol. Joining on ticker creates silent
data corruption that is extremely hard to detect. Surrogate keys make this
impossible.

Consequences:


Always resolve ticker → security_id via sec.resolve_ticker(ticker, as_of_date)
before joining anything.
Delisted securities retain their security_id forever (survivorship-bias
protection).



DEC-003 — Store boundary: Postgres + Parquet + DuckDB

Date: 2026-06

Status: Frozen for V0/V1

StoreHoldsPostgreSQLreference data, security master, corp actions, macro, fundamentals, catalogParquet lakeprice bars (EOD + minute), raw vendor bronze landingDuckDBanalytical engine over Parquet; attaches Postgres for cross-store joins

Rationale: Parquet is free, portable, columnar, and append-friendly for
bars. Postgres gives transactions and constraints for reference/fact data.
DuckDB is zero-ops and reads both. No data duplication across stores.

Scale-later path: When any Postgres fact table outgrows comfortable query
latency, export it to Parquet with identical columns and point DuckDB at it.
The *_asof() semantics are unchanged. This is an operational migration, not
a schema redesign.

Do not add: Redis, ClickHouse, Kafka, Spark, or any streaming
infrastructure before V3. These are not current constraints.


DEC-004 — Unadjusted price storage; adjust on read

Date: 2026-06

Status: Frozen

Price bars are stored unadjusted. Adjusted prices are computed on read
by applying cumulative split/dividend factors derived from corp.corporate_action.

Rationale: Storing adjusted prices bakes future corporate actions into
historical bars that would not have been adjusted at that time. This makes
it impossible to reconstruct what a chart looked like on a past date. The
unadjusted + action-event model is the only PIT-correct approach.


DEC-005 — FRED vintages via ALFRED

Date: 2026-06

Status: Frozen

Macro observations are stored with vintage_date = the date the value was
published/revised (ALFRED realtime_start). Multiple rows per
(series_id, obs_date) are expected and correct — one per revision.

Rationale: Macro series are revised. GDP "as released" on 2020-01-30 is
a different number from the "final revised" GDP for that period. A PIT-correct
macro model must see only the vintage that was available on the research date.


DEC-006 — The concept dictionary is a protected component

Date: 2026-06

Status: Frozen

The fund.concept / fund.concept_alias tables map raw XBRL tags to a
canonical vocabulary. This mapping is critical and cannot be auto-generated.

Protected constraints:


No placeholder mappings. Every entry must be reviewed.
No assumption that similar XBRL tag names refer to the same concept.
Custom company extensions (non-us-gaap tags) must be explicitly handled,
not silently dropped.
A design proposal must be approved before any mapping data is written.


Required proposal content before implementation:


The initial canonical concept vocabulary (income, balance, cashflow core)
Mapping strategy for standard us-gaap tags
Handling strategy for custom/extension tags
How restatements interact with mappings
How new concepts are added without breaking existing queries



DEC-007 — Zero marginal cost until proof of concept

Date: 2026-06

Status: Active

All data sources in V0 and V1 must be free (EDGAR, FRED/ALFRED, Alpaca free
tier, OpenFIGI, exchange_calendars). No paid APIs, no cloud infrastructure,
no SaaS tooling with per-seat or per-call costs.

Trigger to revisit: A live or paper trading model successfully consumes
V1 data via the API. At that point, capital allocation for data improvement
is appropriate.


DEC-008 — Options and futures are out of scope for V0 and V1

Date: 2026-06

Status: Active

Asset scope for V0/V1: US equities, ETFs, macro (FRED). Options are V2.
Futures, FX, and crypto are V3.

Rationale: Single-developer bandwidth. Equities + macro is sufficient
to build and validate multiple systematic models. Expanding scope before
the core is validated wastes time and creates maintenance surface.


Amendment log

(Add entries here when a decision is changed, not by editing the original.)

Date Decision amended Change Reason————
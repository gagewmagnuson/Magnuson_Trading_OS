SCHEMA.md — Trading OS Data Model (V0)

This is the canonical description of the V0 data model. It is the first and
most important entry in DECISIONS.md: no implementation may violate it
without an explicit, recorded amendment. The DDL lives in
/schema/postgres/001_trading_os_v0.sql.


1. The one rule everything rests on: bitemporal, append-only

Every fact carries two time axes:

AxisMeaningExamplesevent_timewhen the fact refers toperiod end, ex-date, bar date, macro periodknowledge_timewhen you could first have known itSEC acceptance timestamp, FRED vintage, vendor publish/fetch time

Facts are immutable. A correction or restatement is a new row with a
later knowledge_time, never an update. A point-in-time read is always:


rows where knowledge_time <= :as_of, then take the latest per business key.



This is the entire defense against lookahead bias, and it's what makes a
backtest run today reproduce bit-for-bit in two years. The append-only
triggers in the DDL physically block UPDATE/DELETE on fact tables so this
can't be violated by accident — or by an over-eager coding agent.

Read facts only through the *_asof() helpers (fund.fundamentals_asof,
macro.observations_asof, univ.members_asof) or the same DISTINCT ON
pattern. A raw SELECT without a knowledge_time filter silently
reintroduces lookahead.


2. Store boundary — what lives where, and why

StoreHoldsWhyPostgreSQLreference data, security master, corporate actions, macro, PIT fundamentals, universe, the metadata catalog (this DDL)Constraints, transactions, foreign keys, trivial as-of SQL. Volumes here are modest (millions of rows), well within Postgres.Parquet lakehigh-volume time series (price bars), raw vendor landing (bronze)Columnar, cheap, portable, append-friendly. Queried directly — never loaded into Postgres.DuckDBthe analytical engineReads Parquet in place and can ATTACH Postgres, so cross-store as-of joins work with no data duplication.

Nothing is duplicated across stores. Postgres is the system of record for
reference/facts; Parquet is the system of record for bars; DuckDB is compute.


3. The Parquet layer (bars live here, not in Postgres)

Bars are the one V0 dataset deliberately kept out of Postgres — they're the
highest-volume table and belong in columnar storage. They use the same
bitemporal column names as the Postgres facts, so the as-of logic is
identical.

lake/
  bronze/                                  # raw, immutable, exactly as vendor sent
    {source}/{dataset}/dt=YYYY-MM-DD/batch={batch_id}.parquet
  silver/
    bars_eod/                              # normalized, unadjusted
      year=YYYY/part-*.parquet
    bars_minute/
      year=YYYY/month=MM/part-*.parquet

silver/bars_eod columns:

columntypenotessecurity_idint64the internal id — never tickersession_datedateevent-timeopen/high/low/closedecimalprices are decimal, never floatvolumeint64unadjustedknowledge_timetimestamp (UTC)when the bar became knowablebatch_idint64lineage → meta.ingest_batch

Prices are stored unadjusted. Adjusted prices are computed on read from
corp.corporate_action (cumulative product of split ratios and dividend
factors as of the query date). Storing both raw prices and the action history
means you can reconstruct exactly what a chart looked like on any past date —
and you never bake a not-yet-existing split into history.


4. DuckDB: cross-store, point-in-time queries

DuckDB is the glue. It reads the Parquet bars and attaches Postgres for the
reference/fundamental tables, so a single query can be fully point-in-time
across both stores.

sql-- one-time, zero-cost: attach Postgres read-only
ATTACH 'postgres:dbname=tradingos host=localhost' AS pg (TYPE postgres, READ_ONLY);

-- "Close prices on 2021-06-30 for everything that was in the S&P 500
--  AS KNOWN on that date" — fully PIT, no lookahead.
WITH members AS (
    SELECT security_id
    FROM pg.univ.members_asof('SP500', DATE '2021-06-30')
)
SELECT b.security_id, b.close
FROM read_parquet('lake/silver/bars_eod/year=2021/*.parquet') b
JOIN members m USING (security_id)
WHERE b.session_date = DATE '2021-06-30'
  AND b.knowledge_time <= TIMESTAMP '2021-06-30 23:59:59+00';

The serving API (later) wraps exactly this pattern behind a single as_of
parameter, so models and agents get PIT correctness for free.


5. Why this scales to a 5-person team and a columnar store — with no redesign

The implementation sequence is solo/zero-budget, but the design is not
simplified. The scale-later guarantees:


Surrogate internal keys everywhere. Joins are on security_id, never
on tickers (which get reused and reassigned). Ticker churn can never
corrupt a join.
Identical bitemporal columns across stores. knowledge_time /
event_time mean the same thing in Postgres and in Parquet, so the as-of
SQL is portable. Migrating fund.fundamental_fact to Parquet/ClickHouse
later is an export + repoint — not a rewrite.
Append-only + lineage. ClickHouse and other columnar engines are
append-optimized; this model maps onto them natively. Every row already
carries batch_id for replay.
Date-based partitioning is already the Parquet layout, so partition
pruning works at any scale.
Multi-user is a config change, not a schema change. Per-consumer API
keys and the catalog already namespace access; a 5-person team adds
readers, not redesign.


The migration trigger is concrete: when a Postgres fact table (likely
fund.fundamental_fact or bars_minute volume) outgrows comfortable query
latency, export it to Parquet with the same columns and point DuckDB/ClickHouse
at it. The *_asof logic is unchanged.


6. What V0 deliberately does NOT include

Per the roadmap, V0 is foundations only. Not here yet (and that's correct):
options/greeks, futures/FX/crypto, news/NLP, real-time/streaming, the serving
API, the UI, multi-tenant entitlements. Adding any of them must not require
changing this core — verify that before building, and record the decision.


7. Governance hook (adopt the proposes/approves workflow)

Before letting Claude Code generate substantial code, seed the repo with the
governing documents and treat them as binding:


VISION.md — the data-layer mission (the reframe: not "rival Bloomberg").
ARCHITECTURE.md — medallion + store boundary + bitemporal rule.
ROADMAP.md — V0→V4 sequence.
DECISIONS.md — this schema is entry #1; log every later choice with date + rationale.


Rule for the agent: no implementation may violate these documents without an
explicit amendment recorded in DECISIONS.md. That single guardrail prevents
the slow architectural drift that otherwise creeps in over weeks of
agent-assisted coding.
# ARCHITECTURE.md — Trading OS System Design

Read `DECISIONS.md` before this file. Every structural choice here has a
corresponding frozen decision. Do not redesign without an amendment entry there.

## Component map

```
 Data Sources (public, free)
   EDGAR · FRED/ALFRED · OpenFIGI · Alpaca (free tier) · exchange_calendars
          │
          ▼
 Ingest Connectors  (Python, one per source)
   └─ write bronze Parquet + meta.ingest_batch rows
          │
          ├──────────────────────────┐
          ▼                          ▼
   PostgreSQL                  Parquet Lake
   (reference & facts)         (bars & bronze)
          │                          │
          └──────────┬───────────────┘
                     ▼
                  DuckDB
              (analytical engine)
                     │
                     ▼
              FastAPI  (V1+)
              Serving API
                     │
                     ▼
         Models · Agents · Notebooks
```

## Store boundary (DEC-003)

Three stores. Each holds exactly one class of data. Nothing is duplicated.

| Store | Holds | Why |
|---|---|---|
| **PostgreSQL** | Security master, identifiers, corporate actions, trading calendar, FRED macro facts, SEC fundamentals, universe membership, ingest catalog | Transactions, constraints, foreign keys. Volumes are modest (millions of rows). Point-in-time SQL (`DISTINCT ON`, window functions) is natural here. |
| **Parquet lake** | Price bars (EOD, minute), raw vendor bronze landing | Columnar, cheap, portable, append-friendly. Partitioned by date. No Postgres involved. |
| **DuckDB** | Analytical engine only — no data of its own | Reads Parquet in place; attaches Postgres via the official extension. Cross-store point-in-time joins with no ETL and no data movement. |

DuckDB is the glue, not a store. All analytical queries go through it.

## Parquet lake structure

```
lake/
  bronze/                         # raw, immutable, exactly as received
    {source}/{dataset}/dt=YYYY-MM-DD/batch={batch_id}.parquet
  silver/
    bars_eod/year=YYYY/part-*.parquet
    bars_minute/year=YYYY/month=MM/part-*.parquet
```

Bronze is the raw landing zone — never modified after write. Silver is
normalized and ready for queries. The `batch_id` column in every silver
file links back to `meta.ingest_batch` for full lineage.

## The bitemporal model (DEC-001)

Every fact in every table carries two time axes:

- **`event_time`** — when the fact refers to (period end, bar date, ex-date,
  macro observation date)
- **`knowledge_time`** — when the fact first became knowable (SEC acceptance
  timestamp, FRED vintage publish date, vendor delivery time)

Facts are immutable. A correction is a new row with a later `knowledge_time`.
`UPDATE` and `DELETE` are blocked by database triggers on all fact tables.

A point-in-time read always follows the same pattern:

```sql
-- rows where knowledge_time <= :as_of,
-- then take the most recent per business key
SELECT DISTINCT ON (security_id, period_end)
       *
FROM fund.fundamental_fact
WHERE knowledge_time <= :as_of
ORDER BY security_id, period_end, knowledge_time DESC;
```

The `*_asof()` stored functions (`fund.fundamentals_asof`,
`macro.observations_asof`, `univ.members_asof`) encapsulate this pattern.
All application code reads through them. Raw `SELECT` without a
`knowledge_time` filter is prohibited.

## Security master and identifier resolution (DEC-002)

`sec.security.security_id` is an internal integer surrogate — the only
safe join key across all tables. Tickers are stored in
`sec.security_identifier` with effective dates and are never used as join keys.

To join on a ticker:

```sql
-- resolve first, then join
SELECT security_id
FROM sec.resolve_ticker('AAPL', DATE '2020-06-30');
```

Delisted securities retain their `security_id` permanently (survivorship-bias
protection). Ticker reuse — a new company listing under a retired symbol —
creates a new `security_id` with a new effective range; the old one is
unchanged.

## Ingest pipeline

Each connector follows the same contract:

1. Write raw response to `lake/bronze/` as Parquet, partitioned by date.
2. Normalize to the silver / Postgres schema.
3. Write a `meta.ingest_batch` row with: `source`, `dataset`, `started`,
   `finished`, `status`, `rows_in`, `rows_out`, `knowledge_time`.
4. Set `knowledge_time` to the source's authoritative publish timestamp
   (EDGAR acceptance timestamp, ALFRED `realtime_start`). Never use the
   current wall clock unless no authoritative timestamp exists.

Connectors are idempotent. Re-running a connector for a date that has
already been ingested must produce no duplicate rows (use
`INSERT ... ON CONFLICT DO NOTHING` or equivalent Parquet dedup).

## Orchestration

V0: cron or Dagster (lightweight, local). No distributed scheduler.
V1+: Dagster if the DAG complexity warrants it.

Forbidden until V3: Kafka, Flink, Spark, Airflow on Kubernetes, or any
streaming infrastructure.

## Serving API (V1+)

FastAPI. Every endpoint accepts an `as_of` date parameter. The API
translates it into `knowledge_time <= :as_of` filters applied at the
DuckDB/Postgres layer. Consumers never interact with raw SQL.

The API is read-only. All writes go through ingest connectors.

## Technology stack

| Layer | Choice | Constraint |
|---|---|---|
| Language | Python | — |
| OLTP / facts | PostgreSQL | Do not swap for V0/V1 |
| Columnar / bars | Parquet | Do not add ClickHouse before V3 |
| Analytical engine | DuckDB | — |
| Data wrangling | Polars | — |
| Serving | FastAPI | V1+ |
| Orchestration | cron → Dagster | No Airflow/Prefect/Kubernetes |
| Identifier API | OpenFIGI | Free tier |
| Macro | FRED / ALFRED | Free, public |
| Fundamentals | SEC EDGAR | Free, public |
| Prices | Alpaca free tier | Free, upgrade later |
| Calendar | exchange_calendars | — |

Do not add Redis, ClickHouse, Kafka, Spark, Kubernetes, or any paid API
before V3, and only then with an explicit DECISIONS.md entry.

## What this architecture explicitly cannot do (until the noted version)

| Capability | Earliest version |
|---|---|
| Real-time / streaming ingest | V3 |
| Options data | V2 |
| Futures, FX, crypto | V3 |
| News / NLP | V3 |
| Multi-tenant entitlements | V3+ |
| Cloud infrastructure | V3+ |
| Serving API | V1 |
| UI (data-health dashboard, catalog explorer) | V1 |

Adding any of these must not require changing the core schema. Verify that
before building, and record the decision.

# VISION.md — Trading OS Mission and Scope

## What this system is

Trading OS is a **financial data infrastructure layer** for systematic research.
It gathers data from authoritative public sources, normalizes it to a single
point-in-time-correct model, and serves it via API to models, agents, and
backtests that live entirely outside this system.

It is not a trading platform. It has no opinion about what to buy or sell.

## The problem it solves

Every serious quantitative research effort eventually hits the same wall:
data that looks clean until you run a backtest, at which point lookahead bias
appears in subtle, hard-to-detect forms — revised macro figures treated as
if they existed at period-start, corporate actions baked into historical
prices before they occurred, fundamentals from a filing date treated as
known at the period-end date the filing covers.

The usual fix is to bolt on corrections after the fact. That approach is fragile
and never fully trusted.

Trading OS fixes this at the foundation: every fact is stored with two time
axes (`event_time` and `knowledge_time`), facts are immutable (corrections
are new rows), and every read is point-in-time by construction. A backtest
run today will reproduce bit-for-bit in two years because the data layer
preserves exactly what was knowable on any past date.

## The hard boundary

**In scope — always:**
- Gathering, normalizing, and storing financial data
- Point-in-time-correct reads via a stable API
- Security master: identifier resolution and corporate-action history
- Fundamental data (income statement, balance sheet, cash flow)
- Macro data (FRED/ALFRED vintages)
- Trading calendars and exchange schedules
- Data quality: freshness checks, null-rate monitoring, PIT correctness tests

**Out of scope — permanently:**
- Trading models and signals
- Execution engines and order routing
- Portfolio management or position tracking
- Backtesting frameworks
- Risk analytics
- Visualization or UI beyond internal diagnostics

Models, agents, and execution infrastructure are consumers of this system.
They call its API. They do not live inside it, and this system does not know
or care what they do with the data.

## Who uses it and how

The primary consumers are:
1. **Backtesting frameworks** — query a point-in-time snapshot of the universe,
   prices, fundamentals, and macro variables as of a historical date.
2. **Live/paper trading models** — pull the current state of any dataset via
   the serving API.
3. **Research notebooks** — ad-hoc DuckDB queries across the full history.

All three consume data through the same point-in-time query semantics.
The API accepts an `as_of` date; the data layer guarantees no information
beyond that date bleeds into the result.

## Guiding principles

**Correctness over convenience.** Lookahead bias is invisible until it isn't.
Every design decision that makes a query easier to write but introduces the
possibility of lookahead is the wrong decision.

**Infrastructure, not application.** This system holds no views. It does not
decide which factors matter or which assets to trade. It provides the data
and steps aside.

**Free until proven.** Zero marginal cost until a live or paper model
successfully consumes V1 data. No paid APIs, no cloud infrastructure,
no SaaS tooling until the foundation is validated.

**Designed to scale, built to start.** The schema and bitemporal rules are
those of a mature data platform. The implementation is solo and zero-budget.
The two are not in conflict: get the model right at V0 and the scale-up
is an operational migration, not a redesign.

**Propose, approve, implement.** No non-trivial change — new connector,
new schema, new dependency — proceeds without an explicit design proposal
and explicit approval. `DECISIONS.md` is the audit trail.

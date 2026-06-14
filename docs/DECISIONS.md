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

## DEC-009 — Concept dictionary architecture
**Date:** 2026-06  
**Status:** Frozen

The `fund.concept` / `fund.concept_alias` tables are a governance artifact,
not a code artifact. Claude Code may implement the schema and connector
plumbing. Claude Code may NOT write mapping data (concept rows or alias rows)
without an explicit human review step.

### Canonical vocabulary: 20 core concepts

Intentionally minimal. Accuracy over breadth — 20 concepts at 99% confidence
beats 500 at 80%. All 20 ship with `research_status = 'core'`.

**Income Statement:** revenue, cost_of_revenue, gross_profit, operating_income,
net_income, eps_basic, eps_diluted, shares_basic, shares_diluted

**Balance Sheet:** total_assets, total_liabilities, total_equity,
cash_and_equivalents, total_debt, goodwill

**Cash Flow:** cfo, capex, cff

**Computed on read (no XBRL mapping):** ebitda (operating_income + D&A),
fcf (cfo + capex)

### research_status on every concept

| Status | Meaning |
|---|---|
| core | Fully reviewed, high-confidence mapping, used in production |
| experimental | Under review, mapping tentative, not yet trusted |
| deprecated | No longer used; retained for historical query compatibility |
| custom | Company-specific extension, not a standard XBRL concept |

New concepts ship as `experimental` until promoted via a DECISIONS.md
amendment. Models and agents filter to `core` by default.

### Tag mapping strategy

Each concept has a priority-ordered list of acceptable us-gaap tags in
`fund.concept_alias` (lower `priority` = higher precedence). The EDGAR
connector walks the list and takes the first tag present in the filing.
Initial priority lists are populated only after human review — never
auto-generated.

### Custom / extension tag handling

Tags not in `fund.concept_alias` are NEVER discarded. They are written to
`fund.unmapped_tag` with full context, reviewed periodically, and promoted
to `concept_alias` via a DECISIONS.md amendment. This makes the dictionary
a compounding asset.

### Restatements

Restatements do not affect mappings. A restated filing is a new `fund.filing`
row with a later `filed_at`; the same mappings apply to both, and
`fund.fundamentals_asof()` selects the correct version via `knowledge_time`.

### Adding new concepts

Propose in chat → record in DECISIONS.md → add to `fund.concept` as
`experimental` → add mappings → add a lookahead-bias test → promote to `core`
after validation. Existing queries are never affected.

### What Claude Code MAY and MAY NOT do

**MAY:** implement schema changes, EDGAR connector plumbing (parsing, storage,
batch rows), unmapped_tag logging, `*_asof()` queries.

**MAY NOT:** write rows into `fund.concept` or `fund.concept_alias` without
review, decide tag→concept mappings autonomously, or mark any concept as
`core` without approval.

## DEC-010 — Concept seed mappings and scope decisions
**Date:** 2026-06  
**Status:** Frozen

Records the human-reviewed mapping decisions in `003_concept_seed.sql`.
The seed is a governance artifact; these are the judgment calls behind it.

### V0 vocabulary: 25 concepts

**22 directly-mapped, reported (research_status = core):** revenue,
cost_of_revenue, gross_profit, operating_income, net_income,
depreciation_amortization, eps_basic, eps_diluted, shares_basic,
shares_diluted, total_assets, total_liabilities, total_equity,
cash_and_equivalents, goodwill, debt_current, debt_noncurrent,
operating_lease_liability, finance_lease_liability, cfo, capex, cff.

**3 computed-on-read (core, NO XBRL aliases):**
- total_debt = debt_current + debt_noncurrent (leases excluded by default)
- ebitda = operating_income + depreciation_amortization
- fcf = cfo + capex (capex stored negative)

### Mapping confidence

`fund.concept_alias.mapping_confidence` (HIGH/MEDIUM/LOW) is populated for
every alias. The connector may quarantine LOW-confidence values; none ship
LOW in V0. Confidence is for audit: it records whether a mapping was obvious
or a judgment call.

### Specific decisions

**Debt as components, not total.** total_debt is computed, never mapped.
The reported components debt_current and debt_noncurrent are stored as
source truth. Storing a single "total debt" tag would be an interpretation
and would discard the maturity profile future models need.

**Leases stored separately from debt, and total-tag only.**
operating_lease_liability and finance_lease_liability are distinct reported
concepts (institutional investors disagree on whether leases are debt, so
the OS does not pre-decide). Each maps ONLY its total us-gaap tag. Component
tags (…Current / …Noncurrent) are deliberately omitted: mixing a total tag
with component tags risks storing a partial liability as if it were whole.
Companies reporting only components will show NULL lease liability, and the
components surface in fund.unmapped_tag for future review. Current/noncurrent
lease concepts may be added later as experimental.

**depreciation_amortization basis.** Defaults to the cash-flow add-back tag
(DepreciationDepletionAndAmortization), which is the economically correct
add-back for EBITDA. The income-statement tag (DepreciationAndAmortization)
is a MEDIUM fallback that can exclude D&A embedded in COGS.

**net_income = attributable to parent.** Maps NetIncomeLoss first; ProfitLoss
(includes noncontrolling interests) is a MEDIUM fallback. Per-share and
equity-holder analysis expects parent-only.

**total_equity = attributable to parent.** Maps StockholdersEquity only. The
NCI-inclusive tag is deliberately NOT mapped. Valuation factors (P/B, ROE,
residual income) conventionally use parent-only equity.

**cash_and_equivalents excludes restricted cash.** Maps
CashAndCashEquivalentsAtCarryingValue only; the restricted-cash-inclusive
tag is deliberately NOT mapped (different economic scope).

**gross_profit and operating_income: direct disclosure only.** Mapped to
their direct tags. When a company does not report them directly, they are
left MISSING rather than derived. Models may compute fallbacks downstream.

**revenue: modern-taxonomy-first.** ASC 606 tag ranked first, with
pre-606/deprecated tags as lower-priority fallbacks. A pre-2018 filing may
resolve to a lower-priority tag; this is intended.

### Open item

ebitda is now computable. No further D&A work required for V0. The
current/noncurrent lease split remains a future expansion, not a V0 gap.

Amendment log

(Add entries here when a decision is changed, not by editing the original.)

Date Decision amended Change Reason————
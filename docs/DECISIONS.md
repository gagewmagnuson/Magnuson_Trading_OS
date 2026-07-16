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

## DEC-011 — Validation cohort before scaling
**Date:** 2026-06  
**Status:** Active

The first EDGAR ingestion target is a fixed five-ticker cohort:
AAPL, MSFT, GOOGL, AMZN, JPM. The connector enforces this in
`config.VALIDATION_TICKERS`; the CLI refuses any ticker outside the set.

Success criteria before scaling beyond the cohort:
- Company Facts downloads to immutable bronze
- Aliases resolve; mapped count is non-trivial
- Units validate against expected_unit
- No lookahead violations; fundamentals_asof() returns correct as-of values
- Restatements stored as multiple bitemporal rows, not overwrites
- Conflicts limited to genuine same-confidence / unresolved-hierarchy cases

Scaling to the full market requires amending config.VALIDATION_TICKERS and
recording the change here. JPM is intentionally included so a bank's tag
profile (different from the tech names) is exercised during validation.

**Rationale:** Validating ingestion correctness on five known issuers is far
cheaper than discovering a systemic mapping or bitemporal bug after a
market-wide run.

---

## DEC-012 — Bronze is immutable; parse from disk
**Date:** 2026-06  
**Status:** Frozen

Raw SEC Company Facts responses are written to
`lake/bronze/edgar/companyfacts/` exactly as received and are NEVER modified.
The pipeline is: download to bronze → parse from disk → normalize → Postgres.

A parser or mapping fix re-runs the parser against stored bronze; it does NOT
re-download from SEC. Re-downloading is only for capturing genuinely new
filings, and lands as a new dated bronze file (never an overwrite).

**Rationale:** This is what makes the lake a true source-of-truth layer rather
than a transient cache. If a mapping bug, taxonomy change, or parsing mistake
is found later, the original source data is still on disk to replay. SEC
Company Facts JSON is small; storage cost is effectively zero, and missing
source truth is expensive.

---

## DEC-013 — Per-concept conflict policy (prefer_higher_confidence)
**Date:** 2026-06  
**Status:** Frozen

When two DIFFERENT mapped tags for the same concept/period materially
disagree WITHIN one filing, resolution depends on a per-concept flag,
`fund.concept.prefer_higher_confidence`:

- **true** — the unique highest-confidence tag wins silently; no conflict
  logged. Used only where the priority ordering encodes a PROVEN economic
  scope hierarchy. Initial set: debt_current, debt_noncurrent
  (LongTermDebtNoncurrent is a proven subset of LongTermDebt).
- **false** (default) — any material disagreement logs a
  fund.concept_conflict and quarantines the value (missing > wrong).
  Includes depreciation_amortization and both lease concepts, whose tag
  relationships are not yet empirically confirmed as scope hierarchies.

Same-confidence disagreement ALWAYS logs a conflict regardless of the flag:
that is genuine ambiguity, not a resolved hierarchy.

Cross-filing disagreement is NOT a conflict — it is a restatement, stored
as multiple bitemporal rows distinguished by filed_at (knowledge_time) and
resolved by fundamentals_asof(). Conflict detection operates only within a
single accession.

**Expansion rule:** a concept may be promoted to true only after observing
its tag relationships across a broad sample and confirming a scope hierarchy.
Record any promotion as a dated amendment here.

## DEC-014 — Flow concepts must be duration-filtered on read
**Date:** 2026-06  
**Status:** Frozen

SEC Company Facts reports flow concepts (income-statement and cash-flow items)
at MULTIPLE durations sharing the same period_end: the 3-month quarter, the
6/9-month year-to-date cumulatives, and the 12-month annual all coexist. They
are correctly stored as distinct facts, distinguished by period_start (which is
part of a flow fact's identity, per DEC-009).

Consequence for consumers: a query that selects a flow concept by period_end
ALONE will return a mix of quarterly, YTD, and annual values and silently
compute wrong results. Models MUST filter by duration.

Canonical duration filter (period_end_date - period_start, in days):
- Annual:    BETWEEN 350 AND 380
- Quarterly: BETWEEN 85 AND 95

Duration is computed from (period_end_date - period_start), NOT inferred from
the fiscal_period label, which Company Facts populates inconsistently.

Instant concepts (balance-sheet items) carry period_start = NULL and need no
duration filter; their identity is period_end alone.

This rule is enforced by the PIT test suite (tests/test_pit_fundamentals.py)
and must be applied by every downstream factor/model query. The future serving
API should expose duration (or period_type) as a first-class query parameter so
consumers cannot accidentally mix durations.

## DEC-015 — Market-observed macro series are single-vintage
**Date:** 2026-06  
**Status:** Frozen

Macro series fall into two natures under ONE canonical data model
(macro.observation with obs_date + vintage_date):

- **Revisable statistics** (GDP, CPI, PPI, unemployment, payrolls, housing,
  credit spreads) get benchmark/seasonal revisions. The connector captures
  FULL ALFRED vintage history; revisions are multiple rows distinguished by
  vintage_date. This is the lookahead-bias protection of DEC-005.

- **Non-revisable market series** (Treasury yields DGS3MO/DGS2/DGS10, the
  T10Y2Y spread) are market observations. The value was known on the
  observation date and is never restated, so there is exactly ONE vintage:
  vintage_date = obs_date. These are fetched from plain FRED (no realtime
  params); ALFRED has no meaningful revision history for them.

The schema, the *_asof() logic, the API, and the PIT tests are IDENTICAL for
both. The only difference is that one nature naturally has many vintages and
the other has one. Consumers neither know nor care.

The `revisable` flag lives in the connector's SERIES config. Classifying a new
series is a governance act: market-quote/price-like series are non-revisable;
estimated/surveyed/seasonally-adjusted statistics are revisable. Record the
classification when adding a series.

## DEC-016 — Corporate-action ingest is V1, not V0 (store is V0 and shipped)
**Date:** 2026-06
**Status:** Frozen

Resolves an ambiguity created by the blueprint's "corporate-actions store +
adjustment engine" V0 line, which bundles two separately-phased components:

- The corporate-actions **store** — the `corp.corporate_action` table — is V0
  and has shipped in `001_v0.sql`, protected by the no-mutation triggers.
- Corporate-action **ingest** (populating splits + cash dividends) and the
  **on-read adjustment function** are V1, exactly as ROADMAP.md already states.
  They are NOT added to V0.

A working conversation had drifted toward pulling a shrunk raw-event ingest into
V0. That drift is explicitly rejected here. No ROADMAP amendment is required:
the committed ROADMAP already places this work in V1, so this entry records the
resolution and closes the drift in the audit trail rather than changing scope.

Rationale: Corporate actions have no consumer in V0. Their purpose is on-read
price adjustment, and there are no price bars until V1 (Alpaca free tier,
DEC-007). Ingesting raw events in V0 would store data that cannot be validated
against its actual use — adjustment factors and adjusted-price reconstruction —
until V1. Building ingest + adjustment together in V1, alongside the bars they
serve, lets the full chain (event → cumulative factor → adjusted price) be
tested end-to-end in one PIT-correct slice.

Consequence: V0 now closes with exactly two remaining deliverables —
(6) trading-calendar population via `exchange_calendars` into
`ref.trading_session`, and (7) the DuckDB cross-store PIT query proof. When both
pass, the V0 gate in ROADMAP.md is met.

## DEC-017 — Identity creation is owned solely by the universe/security-master layer
**Date:** 2026-06
**Status:** Active

The universe/security-master layer is the SOLE creator of `sec.security`
identities. Every other connector resolves an existing identity and never
creates one:

- **Universe layer** — creates `sec.security` + its `TICKER` identifier from a
  coverage manifest; sets `cik` when resolvable. The only code path that inserts
  into `sec.security`.
- **OpenFIGI** — enriches existing identities (FIGI, composite_figi,
  description, country); never inserts.
- **EDGAR / Alpaca bars / corporate actions** — **resolve-and-skip**: if a
  ticker is not already in the master, warn, skip it, count it in the run
  summary, and continue. Never create, never crash, never skip silently. This
  changes EDGAR's writer from resolve-or-create to resolve-only
  (`resolve_security` returns `None` on a miss).

This refines DEC-002 (which set surrogate/FIGI identity but did not name the
creation owner). After this decision exactly one code path inserts into
`sec.security` — the universe writer. The only other insert anywhere in the repo
is the synthetic, rolled-back fixture in `test_security_master.py`, which never
persists.

**`source_id` means the CREATOR of the identity, not the last system to touch
it.** It answers "why does this identity exist": for coverage securities that is
`UNIVERSE`; for the 5 legacy EDGAR cohort it is `SEC_EDGAR` (restored by
`006_backfill_cohort_source.sql`). OpenFIGI therefore does NOT overwrite
`source_id` on enrichment; enrichment provenance lives in `meta.ingest_batch`
(the `OPENFIGI`-sourced batch), not on the security row.

**CIK** is a first-class nullable column on `sec.security` (added in
`005_add_cik.sql`): an issuer-level, immutable SEC key, not effective-dated.
Share classes of one issuer share a CIK.

**Onboarding contract — how a security enters the system:**
`coverage manifest -> universe layer creates identity (+CIK) -> OpenFIGI
enriches (FIGI) -> connectors attach fundamentals / bars / actions
(resolve-and-skip)`.

**Out of scope of this decision:** the *contents* of the coverage universe. The
current coverage set (a large-cap US equity SEED universe from SPY holdings —
explicitly NOT authoritative S&P 500 membership — plus a curated ETF set) is
operational CONFIG in committed manifests (`universe/manifests/`, declared by
`registry.csv`), changeable without amending this DEC. DEC-011's
`VALIDATION_TICKERS` (the EDGAR *fundamentals* gate) is likewise separate and
unchanged: identity coverage and fundamentals coverage stay decoupled.

## DEC-018 — Canonical corporate-action knowledge_time = ingest time

Corporate actions ingested by a canonical connector are stamped with
`knowledge_time = ingest fetch time`, not the ex-date or the market announcement
date. Rationale: the lake's knowledge axis has always meant "when THIS SYSTEM
first knew," not "when the market theoretically knew." Announcement timestamps
vary by vendor and reintroduce ambiguity; ingest time is unambiguous and aligns
with how the bars backfill stamped knowledge_time. The ex_date remains the
price-effect anchor used by the adjustment engine. (The bootstrap loader uses
ex_date-based knowledge_time as a documented bootstrap simplification.)

## DEC-019 — Corporate-action source precedence (read-time, per action)

Multiple sources (BOOTSTRAP, TIINGO, and future SEC/Polygon/MANUAL) may hold an
action for the same (security_id, action_type, ex_date). Because the store is
append-only, conflicting or duplicate rows COEXIST and are never deleted or
mutated. Resolution occurs exclusively at READ time, in
`DuckDBStore._resolve_source_precedence`, per (security_id, action_type,
ex_date): the highest-precedence source's row is kept.

Precedence: MANUAL > SEC > TIINGO > BOOTSTRAP.

This deduplicates identical-payload multi-source actions (so a factor is never
applied twice) and, where payloads differ, selects the highest-precedence
source. Payload DISAGREEMENTS between sources are surfaced by the
corporate-actions DQ check, not silently reconciled at read time.

Principle (broader than this decision): **no connector ever mutates or
invalidates another connector's rows.** Sources are additive; ownership is
explicit via source_id; conflicts are resolved by read-time precedence, never by
write-time deletion.

## DEC-020 — Institutional market data as foundational infrastructure post-PoC

After successful validation of the Trading OS data architecture (adjustment
engine Validators A & B, DQ checks, corporate-actions connector), paid
institutional-quality data services are justified as operational dependencies.
Infrastructure costs should be driven by validated platform capability rather
than speculative development. This retires the "$0 until proof of concept"
constraint, which is now satisfied. The principle is vendor-neutral: it holds if
Tiingo is later replaced by Polygon, FactSet, Refinitiv, ICE, or another
provider — Tiingo remains one swappable connector (source_id-attributed), not a
permanent dependency.

## DEC-021 — API-key consumer authentication via a simple lookup table
**Date:** 2026-07
**Status:** Active

The V1 serving API (FastAPI, read-only, `as_of` on every endpoint) authenticates
consumers with per-consumer API keys stored in a new table, `meta.api_consumer`.

**Storage.** One row per consumer: `consumer_id`, `label`, `key_hash`,
`key_prefix`, `is_active`, `created_at`, and `revoked_at` (audit). The raw API
key is NEVER stored — only its SHA-256 hex hash. A key is minted by an admin CLI
that prints the raw key exactly once and persists only the hash, mirroring how
GitHub/Stripe issue tokens.

**Why a table, not env vars.** Per-consumer keys are in the V1 scope line, and
the governing principle is "simplify the implementation sequence, not the
architecture" — do not make choices that permanently constrain future scale.
Env-based keys would need ripping out to support a second consumer or a 3–5
person research team; a lookup table supports multiple consumers today with no
redesign.

**Explicitly NOT multi-tenant.** This is simple authentication — hash the
presented key, look it up, check `is_active` — NOT authorization, roles, scopes,
per-consumer entitlements, or row-level access. The ROADMAP says "simple, not
multi-tenant," and multi-tenant entitlements remain a V3+ item (ARCHITECTURE.md
capability table). `meta.api_consumer` is a credential lookup, nothing more.

**Hashing choice.** API keys are high-entropy random tokens (256-bit), so a fast
cryptographic hash (SHA-256) with a unique-index lookup is the correct, standard
approach. Password KDFs (bcrypt/argon2) defend low-entropy human passwords
against offline brute force — a threat model that does not apply to random
tokens — so they are deliberately not used here.

**Mutability.** `meta.api_consumer` is reference/config data, not an append-only
fact table, so it is intentionally NOT covered by the no-mutation triggers in
`001_v0.sql`: revoking a key sets `is_active = false` (and stamps `revoked_at`);
a row is never deleted. Minting/revoking keys is an admin CLI action, not a
serving-API operation — the serving API itself remains strictly read-only.

**Consequence.** New migration `schema/postgres/007_api_consumer.sql` creates the
table. The read facade (`src/trading_os/api/deps.py`) validates the
`Authorization: Bearer <key>` header against it on every request; an absent,
unknown, or inactive key returns 401.

## DEC-022 — Serving API uses connection-per-request Postgres access (pool deferred)
**Date:** 2026-07
**Status:** Active

The V1 serving API opens a short-lived, read-only psycopg connection per
request (in `src/trading_os/api/deps.py::get_conn`) and closes it when the
request completes. It does NOT use a connection pool.

**Why per-request now.** V1 load is a single paper/live model issuing periodic
`as_of` reads over 30 days (the V1→V2 gate). Per-request connections are correct
under that load, add zero dependencies, and keep the first slice minimal. A pool
(`psycopg_pool`) optimizes the per-request connection-open cost and caps total
Postgres connections under sustained concurrency — a real benefit only once
multiple consumers hit the API simultaneously, which V1 does not have.

**Not a scale trap.** Routers depend on an abstract connection dependency
(`Depends(get_conn)`), never on how the connection is produced. Swapping to a
pool is a change contained entirely within `deps.py` — no router, endpoint
contract, or `*_asof()` path changes. This is the project principle in action:
simplify the implementation sequence, not the architecture.

**Trigger to revisit.** Adopt `psycopg_pool` (pinned, with its own DECISIONS
amendment) when a second concurrent consumer is onboarded, or when
connection-open latency or Postgres connection-count limits are observed under
load — whichever comes first. Until then, per-request stands.

**Read-only enforcement.** `get_conn` sets the connection read-only as
defense-in-depth: the serving API must never write (ARCHITECTURE.md), and a
read-only connection makes an accidental write in a router fail at the database.
Key minting/revocation is a separate admin path on its own writable connection,
never through the serving API.

## DEC-023 — Serving-API response contract (verified by tests/test_api_bars.py)
**Date:** 2026-07
**Status:** Active

The V1 serving API's JSON response contract, established by `/v1/bars/{symbol}`
and mirrored by every future data endpoint. Verified end-to-end by
`tests/test_api_bars.py` before being recorded here — this documents behavior
that is tested, not merely intended.

**Envelope holds identity; rows are data-only.** A response is an envelope
carrying the resolved identity (`symbol`, `security_id`) and query echo
(`as_of`, `adjustment`, `start`, `end`, `count`) plus a data array. Identity is
NOT repeated on each row. `count` always equals the length of the array so
consumers never compute it.

**`as_of` is optional and defaults to latest known.** Omitting `as_of` returns
data as known at request time (today, end-of-day UTC). A pinned `as_of` is
fully reproducible: identical pinned requests return byte-identical responses
(`test_pinned_as_of_is_reproducible`). Consumers requiring reproducibility MUST
pin `as_of`; the default is a convenience for live "latest" reads, not a
reproducible query.

**`knowledge_time` is exposed on every row.** Each row carries the bitemporal
`knowledge_time`, and every returned row satisfies `knowledge_time <= as_of`
(the PIT guarantee, asserted by `test_pit_knowledge_time_not_after_as_of`).
Exposing it lets consumers reason about and verify point-in-time correctness
rather than trust it blindly.

**Deterministic ordering.** Time series are returned in a defined order
(bars: ascending by `session_date`), so consumers never depend on incidental
ordering. Asserted by `test_bars_ascending_by_session_date`.

**Adjustment / derived semantics are parameters, not endpoint logic.** On-read
transformations (e.g. price `adjustment`) are parameters passed INTO the read
layer; the API computes nothing and mutates nothing. Invalid enum values are
rejected as 422 by FastAPI; an unresolved identifier is 404; a valid identifier
with no data in range is 200 with `count: 0`.

**Status-code conventions.** 401 (missing/invalid/inactive key, DEC-021), 404
(identifier unresolved as of `as_of`), 422 (malformed/invalid parameters), 200
(success, including empty result sets).

**Deferred note — TestClient httpx dependency.** Starlette deprecates `httpx`
under `TestClient` in favor of `httpx2`. `httpx==0.28.1` works and all tests
pass; revisit only when Starlette removes `httpx` support. Tracked here to keep
it off the active backlog.

## DEC-024 — knowledge_time = earliest honest availability (EOD bars + corporate actions); silver rebuild
**Date:** 2026-07
**Status:** Active

Adopting Tiingo Power as the historical EOD price backbone (see DEC-025) forces a
latent question the post-2016-only lake never exposed: what does `knowledge_time`
mean for a fact ingested long after it was knowable? This DEC settles it as one
principle applied per-dataset, and sanctions rebuilding the silver bars layer to
enforce it uniformly. It refines — does not overturn — DEC-018.

**Governing principle.** `knowledge_time` is the EARLIEST MOMENT THE INFORMATION
WAS OBJECTIVELY KNOWABLE, not when this system fetched it. Ingest time is the
fallback ONLY where true availability is genuinely ambiguous. SCHEMA.md already
defines the axis as "when you could first have known it"; this DEC makes the
implementation match that definition instead of stamping ingest wall-clock.

**Why this refines DEC-018 rather than contradicting it.** DEC-018 stamped
corporate actions at ingest time and asserted the axis "has always meant when
THIS SYSTEM first knew." That reasoning is correct only where availability is
ambiguous (vendor announcement timestamps vary). It is wrong where availability
is objective. The axis's true meaning is *earliest honest availability*; ingest
time is its degenerate case, not its definition. DEC-018's scope is hereby
AMENDED: its ingest-time rule governs FORWARD-CAPTURED corporate actions only.

**Rule 1 — EOD bars (all sources).** `knowledge_time` = the security's exchange
close on `session_date`, expressed in UTC (derived via `exchange_calendars`; the
per-exchange session close, not midnight, to avoid the anti-conservative
"knowable at 00:00" error). This is DATASET-scoped, not source-scoped: Alpaca and
Tiingo bars carry identical semantics, so the two remain interchangeable under
DEC-019 precedence. The source must never determine the meaning of the column.

**Rule 2 — backfilled corporate actions.** `knowledge_time` = the `ex_date`
(expressed as its session close, UTC). A split/dividend's price effect and public
knowability are anchored at ex-date — market-objective, like a bar's close. This
is REQUIRED for correctness, not cosmetics: `bars_eod_asof(as_of, adjustment=...)`
filters BOTH bars and actions by `knowledge_time <= as_of`. If backfilled actions
kept ingest-time (2026) knowledge while bars became historically visible, every
historical adjusted read would silently return RAW prices (no action knowable at
a past as_of) — the exact lookahead-shadow this DEC exists to close. (Note: the
original bootstrap loader's ex-date stamping, called a "simplification" in
DEC-018, was in fact the more PIT-correct choice for historical research.)

**Rule 3 — forward capture (both datasets).** From each dataset's capture epoch
forward, facts are fetched near their availability moment, so market-availability
and ingest time approximately coincide; ingest-time stamping remains acceptable
for forward-captured corporate actions (DEC-018 survives, scoped to capture).

**Bars are bitemporal, not static.** Raw OHLCV revises rarely but does revise
(vendor corrections; Tiingo runs an error-checking framework that updates prices).
A correction is a NEW ROW with a later `knowledge_time` — never an overwrite —
exactly as fundamentals restatements are handled. The transformation logic must
not assume bars are correction-free.

**Captured vs reconstructed history (the honest limitation).** A bar backfilled in
2026 for session 1995 is stamped with 1995 market-close knowability, but its VALUE
is the best CURRENT vintage — if a bad 1995 print was corrected in 2019, the
original erroneous print is unrecoverable. This is RECONSTRUCTED history
(backfilled from current vintages), not CAPTURED history (recorded as it happened,
the irreplaceable moat of blueprint §11). Both are PIT-usable; they are not
epistemically identical. Each dataset therefore records a **capture epoch**: before
it, `knowledge_time` is reconstructed availability; from it forward, captured. The
epoch is documented so no consumer or model mistakes reconstructed history for
captured history. System-fetch provenance is NOT lost either way: `batch_id →
meta.ingest_batch` still records when this system actually retrieved every row, so
"when did our system know" remains answerable through lineage.

**Silver rebuild sanctioned.** The silver bars layer is REBUILT from immutable
bronze under Rule 1. This does not violate append-only: bronze is the immutable
replay source, silver is reproducible derived state (DEC-012); re-deriving silver
with corrected transformation logic is replaying history with better logic, not
editing history. Fact tables and their `deny_mutation` triggers are untouched.
The rebuild is validated (Alpaca-vs-Tiingo overlap comparison, 2016+; see DEC-025)
and existing bars PIT/DQ tests (e.g. `test_revision_not_visible_early`) are updated
IN THE SAME CHANGE to assert the new semantics, not discovered as failures later.

**Consequence.** After this DEC, `bars_eod_asof(as_of=<any historical date>)`
returns bars — and their correctly-knowable corporate actions — as objectively
knowable at that date, making true SCHEMA.md's promise to "reconstruct exactly
what a chart looked like on any past date." This closes the bars half of the
knowledge_time-semantics gap (review W2/§9.2) for the whole dataset, historical
and forward, across all sources.

## DEC-025 — Tiingo is the canonical historical EOD source; Alpaca is an independent corroborating source
**Date:** 2026-07
**Status:** Active

Tiingo Power becomes the authoritative source of historical end-of-day equity
prices. Alpaca is retained — not as a fallback, but as an independent
corroborating operational source. This decision is justified by EMPIRICAL
INVESTIGATION of the two vendors' actual coverage, not by which one is paid for.

**Why Tiingo is canonical (architectural reasons, demonstrated — not "because we
pay for it").** A direct probe of the live Tiingo Power key established, as fact:
- **Delisted coverage.** CELG (acquired 2019), ATVI (acquired 2023), and AABA
  (liquidated ~2019) each return full historical daily bars. Prices for securities
  that no longer trade are available — the survivorship-bias killer that blueprint
  §2/§11 identify as core to the moat, and that Alpaca cannot provide.
- **Depth.** Coverage runs to each security's listing (AAPL metadata: start
  1980-12-12); 1990 bars return on request. Alpaca's Basic plan begins in 2016.
- **Honest coverage windows.** Tiingo per-ticker metadata reports truthful
  start/end dates, where `end` is the delisting date (CELG 2019-11-22, ATVI
  2023-10-13) — raw material for honest security-master `valid_from`/`valid_to`
  (addresses review W3) and corroboration of index-membership intervals.
- **Corporate-action consistency.** Tiingo is already the canonical corporate-
  action source; making it the price source too means prices and actions come from
  one internally consistent vendor, which strengthens the adjustment engine.

None of these is a business rationale. "We pay for it" would rot as a
justification; "we probed it and it uniquely supplies delisted depth with honest
coverage windows" is durable and re-verifiable. DEC-020 merely removed the budget
constraint that would previously have blocked this; it is not the reason.

**Alpaca's role (independent corroboration, not a spare tire).** Alpaca remains
in scheduled ingestion as: the execution / paper-trading broker; the minute-bar
source (Tiingo IEX intraday is a ~6-month rolling window on this key — see
limitations); and an INDEPENDENT second EOD feed whose ongoing agreement with
Tiingo is itself a data-quality signal. Multiple independent sources strengthen
the system (blueprint §12 vendor-concentration mitigation); this is a feature, not
a contingency. Both raw feeds are retained immutably in bronze and remain
replayable.

**Read-time precedence (DEC-019).** Where both sources cover a session (2016+),
Tiingo is authoritative on read; Alpaca is the corroborating source. Semantics are
identical across sources (DEC-024, Rule 1: knowledge_time is dataset-scoped, not
source-scoped), so the two are interchangeable and precedence is a clean choice of
authority, not a reconciliation of conflicting meanings.

**History floor: none.** Ingest ALL available history from each security's earliest
available trading day (IPO onward), not an arbitrary cutoff. This follows the
project's spine — store all raw facts, filter at query time (a consumer wanting
post-1990 data passes `start=`; it is not an ingestion policy). Storage cost is
trivial; an artificial floor is a policy baked into immutable history that a future
model cannot undo.

**Identity: creates none (this phase).** The Tiingo EOD connector ingests deep
history for securities ALREADY in the master, via resolve-and-skip (DEC-017). It
writes only bars for existing `security_id`s and creates no identities. Expanding
the master with delisted securities (using Tiingo coverage windows + index
membership) is a SEPARATE, later hardening milestone with its own identity and
symbology work — deliberately not bundled here.

**Validation is a required deliverable.** The connector rollout is not complete
until an Alpaca-vs-Tiingo overlap comparison (2016+, meaningful sample) is run and
documented: OHLC agreement, volume, raw-vs-adjusted, missing sessions. High
agreement (target ~99.99%+ on raw prices) is the empirical evidence that promotes
Tiingo from "probed a few tickers" to "demonstrated canonical." Disagreement is
itself a finding to surface before it can corrupt research. This mirrors the
Validator-A/Validator-B rigor used for the corporate-actions milestone.

**Honest limitations (what this decision does NOT solve).**
- **Not intraday.** Tiingo IEX intraday history is a short rolling window on this
  key; minute bars remain a separate V1 item with a separate source (Alpaca 2016+).
- **Not symbology.** Tiingo supplies delisted prices and coverage windows, but not
  clean point-in-time ticker→identity mapping (ticker reuse/renames). That
  reconciliation remains the hardening milestone's work.
- **Not W2 by itself.** PIT correctness of the historical backfill depends entirely
  on DEC-024 (knowledge_time = market-close availability for bars, ex-date for
  backfilled actions). This DEC chooses the source; DEC-024 makes it PIT-correct.

**Consequence.** With DEC-024 + DEC-025, the platform gains decades of PIT-correct,
delisted-inclusive EOD history from a source already integrated for corporate
actions — directly strengthening the moat (survivorship-free historical research)
and unblocking trustworthy cross-sectional work and the security-master expansion.
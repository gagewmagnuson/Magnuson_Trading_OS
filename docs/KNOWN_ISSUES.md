# Known Issues & Deferred Investigations

Honest record of quantified, understood issues consciously deferred — not silent
gaps. Each entry: what it is, its scope, its impact, and why it's deferred.

---

## KI-001 — Identifier read semantics: is `resolve_ticker` knowledge_time-aware?

**Status:** Open investigation (deferred pending Research OS need)
**Discovered:** 2026-08, during Gold-layer validation (three-security test).

### The finding
`sec.security_identifier` is append-only (the `meta.deny_mutation` trigger blocks
UPDATE/DELETE), and the schema USAGE NOTES prescribe: *"Read facts ONLY through the
`*_asof` helpers … To 'correct' data: INSERT a new row with a later
knowledge_time."* This is the intended bitemporal correction model.

However, `sec.resolve_ticker(ticker, as_of)` does **not** filter or dedup on
`knowledge_time` — it selects by interval containment ordered by `valid_from desc`.
So appended corrections would create overlapping intervals the resolver doesn't
know to supersede.

**The open question (not an assumed defect):** is `resolve_ticker` *intended* to be
a knowledge_time-aware canonical reader, or merely an operational helper used during
ingestion? Does an `_asof` equivalent already exist or need introducing? The USAGE
NOTES say "`*_asof` helpers," and `resolve_ticker` is not named that — so it may not
be intended to carry the same guarantee. This deserves a dedicated investigation,
not a reflexive resolver change (which would ripple through universe membership,
ingestion, validation — everything). Any change likely warrants its own DEC.

### Why deferred
Changing the canonical resolver is a foundational, wide-blast-radius change. The
correct trigger for it is a demonstrated Research OS need, not preemptive
re-architecture (blueprint §12: infra-as-procrastination). Revisit if/when research
reveals identifier-correction semantics actually matter.

---

## KI-002 — Identity/bar boundary: delisted securities' final session (151 rows)

**Status:** Data blemish, deferred (fix blocked by KI-001).
**Scope:** 151 delisted securities, exactly one row each (their last trading day).

The survivorship expansion set each delisted security's identifier `valid_to` to its
last-trade date. Intervals are half-open `[valid_from, valid_to)`, so the final
trading session falls *outside* the interval — `resolve_ticker` returns null on that
one day, and Gold shows a null `symbol` for that row. The bar is real and correct;
only the symbol denormalization is null on the boundary day.

**Impact:** negligible — one day (the delisting day) per delisted security has a null
symbol in Gold. `security_id` remains correct; features are unaffected. The clean fix
(append a corrected interval) depends on resolving KI-001 (append-correction only
works if the reader dedups on knowledge_time).

---

## KI-003 — Security 634 misclassified (Echo Global Logistics → EchoStar)

**Status:** Single-row data error, deferred (fix blocked by KI-001).
**Scope:** one security (security_id 634).

The survivorship expansion recorded security_id 634 as "Echo Global Logistics Inc"
with `valid_from 2009-10-02`. The ECHO ticker/bars actually belong to **EchoStar
Corp** — listed ~2008, still active. The bars (2008–2026) are one clean continuous
company (verified: no price discontinuity); only the identity *label* and
`valid_from` are wrong. Accounts for 442 of the 593 total boundary-violation rows
(bars from 2008-01-02 to 2009-10-01 predating the wrong `valid_from`).

**Impact:** wrong description string + null symbol for the pre-2009-10-02 rows in
Gold. Bars and features are correct. Fix (append a corrected identity row) depends on
KI-001.

---

**Aggregate:** 593 Gold rows across 152 securities carry a null `symbol` (out of
~6M+ rows). All understood, quantified, and non-corrupting — Gold surfaces the
upstream inconsistency honestly rather than hiding it. None affect feature values or
`security_id` correctness.


---

## KI-004 — Promote internal workflows to first-class health pipelines

**Status:** Deferred enhancement (V1 ships without it).

The Health UI reports **vendor ingestion** pipelines (Tiingo bars, FRED, EDGAR,
universe) because those write structured `meta.ingest_batch` rows the health API
queries for freshness. Internal workflows — **Gold refresh**, DQ runs, registry
refresh — currently log to `incremental.log` (human-readable text) but do NOT emit
structured `ingest_batch` metadata, so they can't participate in the freshness
model and don't appear as pipelines in the console.

**Impact:** if an internal job (e.g. Gold refresh) fails, the launchd wrapper exits
non-zero and the failure is in `incremental.log` — investigable, but not visible in
the Health UI. Adequate for an attended development platform; insufficient for
long-run unattended operation.

**The enhancement (a cohesive later piece):**
- Gold refresh emits `ingest_batch` metadata.
- DQ runs emit `ingest_batch` metadata.
- Registry/universe refresh emit `ingest_batch` metadata.
- Health UI reports vendor and internal pipelines uniformly (via `meta.pipeline_definition`).

Deferred as operational maturity (alerts, dependency graphs, retry status belong
here too) — a clean follow-on after the Research OS, not a V1 blocker.

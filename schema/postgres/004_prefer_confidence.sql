-- =====================================================================
-- Trading OS — 004_prefer_confidence.sql
-- Repo path: /schema/postgres/004_prefer_confidence.sql
-- Run AFTER 001_v0.sql and 003_concept_seed.sql.
--
-- PURPOSE (DEC-013)
--   Per-concept conflict policy. When two mapped tags for the same
--   concept/period disagree WITHIN one filing:
--     * prefer_higher_confidence = true  -> the unique highest-confidence
--       tag wins silently (the priority order already encodes a known
--       economic scope hierarchy; logging would re-litigate a settled
--       decision and flood the conflict log with expected noise).
--     * prefer_higher_confidence = false -> ANY disagreement logs a
--       fund.concept_conflict and the value is quarantined (missing > wrong).
--
--   Same-confidence disagreement ALWAYS logs a conflict regardless of the
--   flag: that is genuine ambiguity, not a resolved hierarchy.
--
-- INITIAL ROLLOUT
--   true  : debt_current, debt_noncurrent
--           (LongTermDebtNoncurrent vs LongTermDebt is a proven scope subset)
--   false : everything else, including depreciation_amortization and the
--           lease concepts, until their tag relationships are empirically
--           confirmed as scope hierarchies across a broad sample.
-- =====================================================================

begin;

alter table fund.concept
    add column if not exists prefer_higher_confidence boolean not null default false;
comment on column fund.concept.prefer_higher_confidence is
    'Conflict policy (DEC-013). true = unique highest-confidence tag wins silently on intra-filing disagreement (proven scope hierarchy). false = any disagreement logs a conflict. Same-confidence disagreement always conflicts.';

update fund.concept
   set prefer_higher_confidence = true
 where canonical_name in ('debt_current', 'debt_noncurrent');

insert into meta.schema_migration(version, description)
values ('v0.4.0',
        'Per-concept conflict policy: prefer_higher_confidence flag on fund.concept; enabled for debt_current and debt_noncurrent only (DEC-013).')
on conflict (version) do nothing;

commit;

-- Verify:
--   select canonical_name, prefer_higher_confidence
--   from fund.concept where prefer_higher_confidence order by canonical_name;
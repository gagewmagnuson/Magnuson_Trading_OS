-- =====================================================================
-- Trading OS — 003_concept_seed.sql
-- Repo path: /schema/postgres/003_concept_seed.sql
-- Run AFTER 001_v0.sql.
--
-- PURPOSE
--   Seed the canonical concept dictionary (fund.concept) and its
--   Company-Facts-API us-gaap mappings (fund.concept_alias) for V0.
--
-- GOVERNANCE (DEC-009)
--   Human-reviewed governance artifact, NOT auto-generated. Rules:
--     * Economic correctness beats commonality.
--     * Modern taxonomy (ASC 606 / ASC 842) beats deprecated tags.
--     * Direct disclosure beats derived values.
--     * Missing beats wrong: when uncertain, prefer NO mapping + log.
--     * Never mix a TOTAL tag and its COMPONENT tags in one concept:
--       a component present without the total would store a partial
--       value as if it were whole. Map total-only; log components.
--
-- SHAPE
--   Company-Facts-API-shaped: each source_tag is a us-gaap concept name
--   as it appears under facts["us-gaap"][tag]. The connector walks a
--   concept's aliases in ascending priority and takes the first tag
--   present in a filing.
--
-- CONFIDENCE
--   mapping_confidence: HIGH = unambiguous; MEDIUM = judgment call;
--   LOW = placeholder pending validation. (None shipped LOW in V0.)
--
-- PERIOD SEMANTICS (DEC-009)
--   FLOW concepts carry period_start; INSTANT concepts do not.
--   period_type is DERIVED from period_start, not stored.
--
-- COMPUTED CONCEPTS
--   total_debt, ebitda, fcf have concept rows (documented + queryable)
--   but ZERO aliases. Computed on read from the reported concepts named
--   in their descriptions. Connector must never map a tag to them.
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- 0. Schema addition: mapping_confidence on fund.concept_alias.
-- ---------------------------------------------------------------------
alter table fund.concept_alias
    add column if not exists mapping_confidence text not null default 'HIGH'
    check (mapping_confidence in ('HIGH','MEDIUM','LOW'));
comment on column fund.concept_alias.mapping_confidence is
    'HIGH = unambiguous; MEDIUM = judgment call; LOW = placeholder pending validation. Connectors may quarantine LOW-confidence values.';

-- ---------------------------------------------------------------------
-- 1. EDGAR data source (concept_alias.source_id references this).
-- ---------------------------------------------------------------------
insert into ref.data_source (name, kind, is_redistributable, base_url, license_notes)
values ('SEC_EDGAR', 'fundamentals', true,
        'https://data.sec.gov',
        'US-government public domain. Company Facts API: /api/xbrl/companyfacts/CIK##########.json')
on conflict (name) do nothing;

-- ---------------------------------------------------------------------
-- 2. Concepts.
--    Reported concepts: research_status='core', carry aliases.
--    Computed (total_debt, ebitda, fcf): 'core', NO aliases.
--    expected_unit: 'USD' | 'USD/shares' | 'shares' | NULL.
--    FLOW concepts carry period_start; INSTANT concepts do not.
-- ---------------------------------------------------------------------

-- ---- Income statement (FLOW) ----
insert into fund.concept (canonical_name, statement, description, research_status, expected_unit) values
('revenue',                  'income', 'Total net revenue / sales for the period. FLOW. ASC 606 preferred.',                'core', 'USD'),
('cost_of_revenue',          'income', 'Cost of goods/services sold for the period. FLOW.',                                 'core', 'USD'),
('gross_profit',             'income', 'Gross profit for the period. FLOW. Direct disclosure only; missing if not reported directly (do NOT derive here).', 'core', 'USD'),
('operating_income',         'income', 'Operating income/loss for the period. FLOW. Direct disclosure only; missing if not reported directly.', 'core', 'USD'),
('net_income',               'income', 'Net income attributable to common shareholders for the period. FLOW.',              'core', 'USD'),
('depreciation_amortization','income', 'Depreciation, depletion & amortization for the period (cash-flow add-back basis). FLOW. Feeds ebitda.', 'core', 'USD'),
('eps_basic',                'income', 'Basic earnings per share for the period. FLOW.',                                     'core', 'USD/shares'),
('eps_diluted',              'income', 'Diluted earnings per share for the period. FLOW.',                                   'core', 'USD/shares'),
('shares_basic',             'income', 'Weighted-average basic shares outstanding for the period. FLOW.',                   'core', 'shares'),
('shares_diluted',           'income', 'Weighted-average diluted shares outstanding for the period. FLOW.',                 'core', 'shares')
on conflict (canonical_name) do nothing;

-- ---- Balance sheet (INSTANT) ----
insert into fund.concept (canonical_name, statement, description, research_status, expected_unit) values
('total_assets',             'balance', 'Total assets at period end. INSTANT.',                                            'core', 'USD'),
('total_liabilities',        'balance', 'Total liabilities at period end. INSTANT.',                                       'core', 'USD'),
('total_equity',             'balance', 'Stockholders equity attributable to parent at period end. INSTANT.',             'core', 'USD'),
('cash_and_equivalents',     'balance', 'Cash and cash equivalents at period end (excludes restricted cash). INSTANT.',  'core', 'USD'),
('goodwill',                 'balance', 'Goodwill at period end. INSTANT.',                                                'core', 'USD'),
('debt_current',             'balance', 'Current/short-term debt at period end. INSTANT. Reported component; total_debt derives from this.', 'core', 'USD'),
('debt_noncurrent',          'balance', 'Long-term (noncurrent) debt at period end. INSTANT. Reported component.',        'core', 'USD'),
('operating_lease_liability','balance', 'TOTAL operating lease liability (ASC 842) at period end. INSTANT. Total-tag only; missing if only current/noncurrent components are reported (those are logged to unmapped_tag).', 'core', 'USD'),
('finance_lease_liability',  'balance', 'TOTAL finance lease liability (ASC 842) at period end. INSTANT. Total-tag only; missing if only components reported.', 'core', 'USD')
on conflict (canonical_name) do nothing;

-- ---- Cash flow (FLOW) ----
insert into fund.concept (canonical_name, statement, description, research_status, expected_unit) values
('cfo',   'cashflow', 'Net cash provided by/used in operating activities for the period. FLOW.',                          'core', 'USD'),
('capex', 'cashflow', 'Capital expenditures for the period (store as reported; typically negative). FLOW.',              'core', 'USD'),
('cff',   'cashflow', 'Net cash provided by/used in financing activities for the period. FLOW.',                         'core', 'USD')
on conflict (canonical_name) do nothing;

-- ---- Computed-on-read (NO aliases; documented formula only) ----
insert into fund.concept (canonical_name, statement, description, research_status, expected_unit) values
('total_debt', 'balance', 'COMPUTED on read = debt_current + debt_noncurrent. Leases excluded by default; models may add operating_lease_liability / finance_lease_liability. No XBRL alias.', 'core', 'USD'),
('ebitda',     'income',  'COMPUTED on read = operating_income + depreciation_amortization. No XBRL alias.',              'core', 'USD'),
('fcf',        'cashflow','COMPUTED on read = cfo + capex (capex stored negative). No XBRL alias.',                       'core', 'USD')
on conflict (canonical_name) do nothing;

-- ---------------------------------------------------------------------
-- 3. Aliases (Company-Facts us-gaap tags), priority-ordered.
-- ---------------------------------------------------------------------
do $$
declare
    v_src bigint;
    v_cid bigint;
begin
    select source_id into v_src from ref.data_source where name = 'SEC_EDGAR';

    -- ----- revenue -----
    select concept_id into v_cid from fund.concept where canonical_name = 'revenue';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'RevenueFromContractWithCustomerExcludingAssessedTax', 10, 'HIGH'),
      (v_cid, v_src, 'RevenueFromContractWithCustomerIncludingAssessedTax', 20, 'MEDIUM'),
      (v_cid, v_src, 'Revenues',                                            30, 'MEDIUM'),
      (v_cid, v_src, 'SalesRevenueNet',                                     40, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- cost_of_revenue -----
    select concept_id into v_cid from fund.concept where canonical_name = 'cost_of_revenue';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'CostOfRevenue',               10, 'HIGH'),
      (v_cid, v_src, 'CostOfGoodsAndServicesSold',  20, 'HIGH'),
      (v_cid, v_src, 'CostOfGoodsSold',             30, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- gross_profit (direct only) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'gross_profit';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'GrossProfit', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- operating_income (direct only) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'operating_income';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'OperatingIncomeLoss', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- net_income (parent only) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'net_income';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'NetIncomeLoss', 10, 'HIGH'),
      (v_cid, v_src, 'ProfitLoss',    20, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- depreciation_amortization (cash-flow add-back basis) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'depreciation_amortization';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'DepreciationDepletionAndAmortization',   10, 'HIGH'),
      (v_cid, v_src, 'DepreciationAmortizationAndAccretionNet', 20, 'MEDIUM'),
      (v_cid, v_src, 'DepreciationAndAmortization',            30, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- eps_basic -----
    select concept_id into v_cid from fund.concept where canonical_name = 'eps_basic';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'EarningsPerShareBasic', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- eps_diluted -----
    select concept_id into v_cid from fund.concept where canonical_name = 'eps_diluted';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'EarningsPerShareDiluted', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- shares_basic -----
    select concept_id into v_cid from fund.concept where canonical_name = 'shares_basic';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'WeightedAverageNumberOfSharesOutstandingBasic', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- shares_diluted -----
    select concept_id into v_cid from fund.concept where canonical_name = 'shares_diluted';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'WeightedAverageNumberOfDilutedSharesOutstanding', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- total_assets -----
    select concept_id into v_cid from fund.concept where canonical_name = 'total_assets';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'Assets', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- total_liabilities -----
    select concept_id into v_cid from fund.concept where canonical_name = 'total_liabilities';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'Liabilities', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- total_equity (attributable to parent) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'total_equity';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'StockholdersEquity', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- cash_and_equivalents (excludes restricted cash) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'cash_and_equivalents';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'CashAndCashEquivalentsAtCarryingValue', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- goodwill -----
    select concept_id into v_cid from fund.concept where canonical_name = 'goodwill';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'Goodwill', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- debt_current -----
    select concept_id into v_cid from fund.concept where canonical_name = 'debt_current';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'DebtCurrent',         10, 'HIGH'),
      (v_cid, v_src, 'LongTermDebtCurrent', 20, 'MEDIUM'),
      (v_cid, v_src, 'ShortTermBorrowings', 30, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- debt_noncurrent -----
    select concept_id into v_cid from fund.concept where canonical_name = 'debt_noncurrent';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'LongTermDebtNoncurrent', 10, 'HIGH'),
      (v_cid, v_src, 'LongTermDebt',           20, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- operating_lease_liability (TOTAL tag only) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'operating_lease_liability';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'OperatingLeaseLiability', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- finance_lease_liability (TOTAL tag only) -----
    select concept_id into v_cid from fund.concept where canonical_name = 'finance_lease_liability';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'FinanceLeaseLiability', 10, 'HIGH')
    on conflict (source_id, source_tag) do nothing;

    -- ----- cfo -----
    select concept_id into v_cid from fund.concept where canonical_name = 'cfo';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'NetCashProvidedByUsedInOperatingActivities', 10, 'HIGH'),
      (v_cid, v_src, 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations', 20, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- capex -----
    select concept_id into v_cid from fund.concept where canonical_name = 'capex';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'PaymentsToAcquirePropertyPlantAndEquipment', 10, 'HIGH'),
      (v_cid, v_src, 'PaymentsToAcquireProductiveAssets',          20, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

    -- ----- cff -----
    select concept_id into v_cid from fund.concept where canonical_name = 'cff';
    insert into fund.concept_alias (concept_id, source_id, source_tag, priority, mapping_confidence) values
      (v_cid, v_src, 'NetCashProvidedByUsedInFinancingActivities', 10, 'HIGH'),
      (v_cid, v_src, 'NetCashProvidedByUsedInFinancingActivitiesContinuingOperations', 20, 'MEDIUM')
    on conflict (source_id, source_tag) do nothing;

end;
$$;

-- ---------------------------------------------------------------------
-- 4. Record migration.
-- ---------------------------------------------------------------------
insert into meta.schema_migration(version, description)
values ('v0.3.0',
        'Concept seed: mapping_confidence column; EDGAR source; 24 V0 concepts (21 reported core incl. depreciation_amortization, 3 computed) and priority-ordered us-gaap aliases.')
on conflict (version) do nothing;

commit;

-- =====================================================================
-- REVIEW NOTES / OPEN ITEMS (carry into DECISIONS.md as DEC-010)
--   * depreciation_amortization defaults to the cash-flow add-back tag
--     (DepreciationDepletionAndAmortization); the IS-line tag is a MEDIUM
--     fallback that can exclude COGS-embedded D&A. ebitda is now computable.
--   * Lease concepts map TOTAL tags only. Companies reporting only
--     current/noncurrent components will show NULL lease liability and the
--     components will appear in unmapped_tag. Add experimental
--     current/noncurrent lease concepts later if needed.
--   * total_equity = parent-only (StockholdersEquity). APPROVED.
--   * cash_and_equivalents excludes restricted cash. APPROVED.
-- =====================================================================
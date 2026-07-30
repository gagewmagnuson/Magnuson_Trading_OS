import csv

rows = list(csv.DictReader(open('corroborated_report.csv')))
by_ticker = {r['ticker']: r for r in rows}

# Manual decisions from review (the only human judgments layered on the buckets):
THIN_WINDOW_REUSE = {"HOT", "HONA", "PEAK", "WRK", "SOLS"}   # in add:overlap_ok -> EXCLUDE
VERIFIED_ADD_FROM_21 = {   # passthrough:REVIEW that are verified adds (honest windows)
    "H", "MIR", "DXC", "AAL", "NE",                              # new_identity (5)
    "AN", "CE", "COV", "FL", "FMC", "GAS", "GGP", "HP", "KSU", "MXIM", "OI", "RIG",  # continuous (12)
}
DEFER_FROM_21 = {"CBE", "MEE", "ESV", "HRS"}   # manual_review / unavailable within the 21

# Build the definitive add set from the data.
overlap = {t for t, r in by_ticker.items() if r['corroboration'] == 'add:overlap_ok'}
add_set = (overlap - THIN_WINDOW_REUSE) | VERIFIED_ADD_FROM_21

# Reconciliation of ALL 702.
review = {t for t, r in by_ticker.items()
          if r['corroboration'] in ('review:modern_reuse', 'review:blank_window')}
review |= THIN_WINDOW_REUSE            # the 5 demoted from overlap
review |= {"CBE", "MEE"}               # deferred from the 21
unavailable = {t for t, r in by_ticker.items() if r['corroboration'] == 'passthrough:unavailable'}
translate = {t for t, r in by_ticker.items() if r['corroboration'] == 'passthrough:translate'}
add_set |= translate                   # the 2 dot-class adds have real windows

print("=== Migration A add-set reconciliation ===")
print(f"  overlap:overlap_ok:        {len(overlap)}")
print(f"    minus thin-window reuse: -{len(THIN_WINDOW_REUSE & overlap)}")
print(f"  plus verified-add from 21: +{len(VERIFIED_ADD_FROM_21)}")
print(f"  plus dot-class translate:  +{len(translate)}")
print(f"  ---------------------------------")
print(f"  TOTAL AUTO-ADD:            {len(add_set)}")
print()
print("=== full 702 reconciliation (must sum to 702) ===")
buckets = {
    "add": len(add_set),
    "review (reuse/blank/thin/deferred)": len(review),
    "unavailable": len(unavailable),
}
# sanity: every ticker accounted for exactly once
all_accounted = add_set | review | unavailable
print(f"  add:         {buckets['add']}")
print(f"  review:      {buckets['review (reuse/blank/thin/deferred)']}")
print(f"  unavailable: {buckets['unavailable']}")
print(f"  ----------------")
print(f"  SUM:         {sum(buckets.values())}")
print(f"  distinct accounted: {len(all_accounted)}  (should equal 702)")
overlap_check = add_set & review
print(f"  add∩review (must be 0):   {len(overlap_check)}  {sorted(overlap_check) if overlap_check else ''}")
missing = set(by_ticker) - all_accounted
print(f"  unaccounted (must be 0):  {len(missing)}  {sorted(missing)[:10] if missing else ''}")
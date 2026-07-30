import csv
from datetime import date

def d(s):
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None

rows = [r for r in csv.DictReader(open('corroborated_report.csv'))
        if r['corroboration'] == 'add:overlap_ok']

buckets = {"<6mo": 0, "6-12mo": 0, "1-3yr": 0, "3-10yr": 0, ">10yr": 0, "open/unknown": 0}
thin = []
for r in rows:
    ts, te = d(r['tiingo_start']), d(r['tiingo_end'])
    if ts is None or te is None:
        buckets["open/unknown"] += 1
        continue
    days = (te - ts).days
    if days < 182:
        buckets["<6mo"] += 1; thin.append((r['ticker'], r['tiingo_start'][:7], r['tiingo_end'][:7], r['tiingo_name'][:26]))
    elif days < 365: buckets["6-12mo"] += 1; thin.append((r['ticker'], r['tiingo_start'][:7], r['tiingo_end'][:7], r['tiingo_name'][:26]))
    elif days < 3*365: buckets["1-3yr"] += 1
    elif days < 10*365: buckets["3-10yr"] += 1
    else: buckets[">10yr"] += 1

print("coverage duration within add:overlap_ok (292 total):")
for k, v in buckets.items():
    print(f"  {k:14} {v}")
print(f"\nthin windows (<12mo), {len(thin)} total:")
for t in thin:
    print(f"  {t[0]:7} {t[1]}..{t[2]}  {t[3]}")
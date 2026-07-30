import csv
rows = list(csv.DictReader(open('working/candidate_survivorship_20260726.csv')))
flags = ['ETF','FUND','TRUST','ISHARES','SPDR','PROSHARES','DAILY','2X','3X','BULL','BEAR','LTD','PLC','ADR']
print("=== names with suspicious keywords (eyeball these) ===")
for r in rows:
    n = (r['name'] or '').upper()
    if any(k in n for k in flags):
        print(f"  {r['ticker']:7} {r['valid_from']}..{r['valid_to'] or 'open':10} {r['name'][:40]}  [{r['decision']}]")
print("\n=== 12 shortest-coverage rows (thin-window check) ===")
from datetime import date
def dur(r):
    try:
        s = date.fromisoformat(r['valid_from'])
        e = date.fromisoformat(r['valid_to']) if r['valid_to'].strip() else date(2026,7,1)
        return (e-s).days
    except: return 999999
for r in sorted(rows, key=dur)[:12]:
    print(f"  {r['ticker']:7} {r['valid_from']}..{r['valid_to'] or 'open':10} {dur(r):>6}d {r['name'][:36]}")
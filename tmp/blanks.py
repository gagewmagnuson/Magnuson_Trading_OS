import csv

rows = list(csv.DictReader(open('corroborated_report.csv')))

NEW_IDENTITY = {"H", "MIR", "DXC", "AAL", "NE"}
CONTINUOUS = {"AN", "CE", "COV", "FL", "FMC", "GAS", "GGP", "HP", "KSU", "MXIM", "OI", "RIG"}
THIN = {"HOT", "HONA", "PEAK", "WRK", "SOLS"}
admit_corr = {"add:overlap_ok", "passthrough:translate"}

print("=== admitted rows with BLANK tiingo_start ===")
for r in rows:
    t = r['ticker']
    admitted = (t in NEW_IDENTITY or t in CONTINUOUS or r['corroboration'] in admit_corr) and t not in THIN
    if admitted and not r['tiingo_start'].strip():
        print("  {:8} corr={:20} tiingo={:16} name={}".format(
            t, r['corroboration'], r['tiingo'], r['tiingo_name'][:28]))

print()
print("=== ALL dot-containing tickers in the file (any bucket) ===")
for r in rows:
    if '.' in r['ticker']:
        print("  {:8} corr={:20} start={!r} name={}".format(
            r['ticker'], r['corroboration'], r['tiingo_start'][:10], r['tiingo_name'][:26]))
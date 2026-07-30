import csv
rows = list(csv.DictReader(open('working/candidate_survivorship_20260729.csv')))
no_from = [r['ticker'] for r in rows if not r['valid_from'].strip()]
bad_order = [r['ticker'] for r in rows if r['valid_to'].strip() and r['valid_from'] >= r['valid_to']]
pulled = {"HOT","HONA","PEAK","WRK","SOLS","CAM","GENZ","LLL","NVLS","PCL","TSS","EDS"}
leaked = [r['ticker'] for r in rows if r['ticker'] in pulled]
print('total rows:', len(rows))
print('blank valid_from (must be 0):', len(no_from), no_from)
print('valid_from >= valid_to (must be 0):', len(bad_order), bad_order)
print('pulled reusers that leaked back in (must be 0):', leaked)
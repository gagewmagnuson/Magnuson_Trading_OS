import csv
rows = list(csv.DictReader(open('classify_delisted_report.csv')))
add_no_start = [r for r in rows if r['recommendation'] == 'add' and not r['tiingo_start'].strip()]
add_with_start = [r for r in rows if r['recommendation'] == 'add' and r['tiingo_start'].strip()]
print('add rows total:', sum(1 for r in rows if r['recommendation'] == 'add'))
print('  add WITH tiingo_start:', len(add_with_start))
print('  add WITHOUT tiingo_start:', len(add_no_start))
print('sample add-without-start:')
for r in add_no_start[:8]:
    print('  {:8} tiingo={:16} start={!r} end={!r} name={!r}'.format(
        r['ticker'], r['tiingo'], r['tiingo_start'], r['tiingo_end'], r['tiingo_name'][:30]))
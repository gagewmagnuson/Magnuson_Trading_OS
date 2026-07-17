"""Throwaway: is Tiingo's raw close unadjusted? Check AAPL around the 2020-08-31 4:1 split."""
import json, urllib.parse, urllib.request
from trading_os.config import settings

TOKEN = settings.tiingo_key()
url = "https://api.tiingo.com/tiingo/daily/aapl/prices?" + urllib.parse.urlencode({
    "startDate": "2020-08-27", "endDate": "2020-09-02",
    "token": TOKEN, "format": "json", "resampleFreq": "daily",
})
req = urllib.request.Request(url, headers={"accept": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    rows = json.loads(r.read())

print(f"{'date':12} {'raw_close':>10} {'adjClose':>10} {'splitFactor':>12}")
for row in rows:
    d = row.get("date", "")[:10]
    print(f"{d:12} {str(row.get('close')):>10} {str(row.get('adjClose')):>10} {str(row.get('splitFactor')):>12}")
"""
Classify unresolved historical S&P tickers before any identity creation (read-only).

Repo path: src/trading_os/universe/classify_delisted.py
Run:  PYTHONPATH=src python -m trading_os.universe.classify_delisted

The security master holds only surviving securities. The sp500 membership file
lists every historical constituent; ~700 of its tickers don't resolve today —
the survivorship gap. Before creating identities for them (unrecoverable once
written), this tool produces ONE comprehensive report per unresolved ticker:

  - Tiingo availability + honest listing window (canonical symbol, then the
    Tiingo share-class translation on 404)
  - SEC CIK where the issuer is still a filer (defunct issuers won't resolve —
    that's expected, recorded as blank)
  - multi-interval reuse flag: the ticker appears in >1 disjoint sp500 interval,
    the signal of possible ticker reuse across two different companies
  - a recommendation: add | translate | unavailable | REVIEW (conflict)

It writes nothing to the database. Output: a CSV for review + a category summary.
The conflicts (REVIEW rows) are the point — those are where a wrong automated
call would silently merge two companies, so they are flagged for human decision,
never auto-resolved.
"""
from __future__ import annotations

import csv
import json
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

import psycopg

from trading_os.config import settings
from trading_os.connectors.tiingo.client import TiingoClient
from trading_os.connectors.tiingo.symbols import to_tiingo_symbol
from . import config as ucfg

SP500_INTERVALS_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
OUT_CSV = Path("classify_delisted_report.csv")


@dataclass
class Row:
    ticker: str
    intervals: int                # count of disjoint sp500 date ranges for this ticker
    multi_interval: bool          # >1 -> possible reuse
    sp500_first: str
    sp500_last: str
    tiingo: str                   # 'ok' | 'ok(translated)' | '404' | 'error'
    tiingo_symbol: str            # the symbol that actually worked (if any)
    tiingo_start: str
    tiingo_end: str
    tiingo_name: str
    cik: str
    recommendation: str           # 'add' | 'translate' | 'unavailable' | 'REVIEW'


def _load_sp500_intervals() -> dict[str, list[tuple[str, str]]]:
    """ticker -> list of (start_date, end_date) intervals from the sp500 file."""
    raw = urllib.request.urlopen(SP500_INTERVALS_URL).read().decode()
    by_ticker: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in csv.DictReader(raw.splitlines()):
        by_ticker[r["ticker"]].append((r["start_date"], r.get("end_date", "") or ""))
    return by_ticker


def _load_sec_ciks(cfg: ucfg.UniverseConfig) -> dict[str, str]:
    """ticker (upper) -> CIK (zero-padded 10) from SEC company_tickers.json.
    Current filers only; defunct issuers legitimately absent."""
    req = urllib.request.Request(
        cfg.company_tickers_url, headers={"User-Agent": cfg.sec_user_agent}
    )
    data = json.loads(urllib.request.urlopen(req).read())
    out: dict[str, str] = {}
    for row in data.values():
        out[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return out


def _tiingo_lookup(client: TiingoClient, ticker: str) -> tuple[str, str, str, str, str]:
    """Return (status, used_symbol, start, end, name). Try canonical, then the
    Tiingo share-class translation on 404."""
    for used in (ticker, to_tiingo_symbol(ticker)):
        try:
            m = client.fetch_metadata(used)
        except RuntimeError as e:
            if "404" in str(e):
                if used != ticker:
                    return "404", "", "", "", ""   # both forms 404
                continue                            # try translated form
            return "error", "", "", "", str(e)[:60]
        start = (m.get("startDate") or "")[:10]
        end = (m.get("endDate") or "")[:10]
        status = "ok" if used == ticker else "ok(translated)"
        return status, used, start, end, (m.get("name") or "")[:60]
    return "404", "", "", "", ""


def _unresolved_tickers(conn: psycopg.Connection, by_ticker: dict) -> list[str]:
    out = []
    for t in sorted(by_ticker):
        r = conn.execute("select sec.resolve_ticker(%s, current_date)", (t,)).fetchone()
        if r[0] is None:
            out.append(t)
    return out


def _recommend(tiingo_status: str, multi: bool) -> str:
    if multi:
        return "REVIEW"                    # possible reuse -> human decides, never auto
    if tiingo_status == "ok":
        return "add"
    if tiingo_status == "ok(translated)":
        return "translate"
    return "unavailable"                   # 404 / error -> no priceable data, don't invent


def classify() -> list[Row]:
    cfg = ucfg.UniverseConfig()
    by_ticker = _load_sp500_intervals()
    ciks = _load_sec_ciks(cfg)
    client = TiingoClient()

    with psycopg.connect(settings.pg_conninfo()) as conn:
        unresolved = _unresolved_tickers(conn, by_ticker)

    print(f"[classify] {len(unresolved)} unresolved tickers to classify...")
    rows: list[Row] = []
    for i, t in enumerate(unresolved, 1):
        intervals = by_ticker[t]
        starts = sorted(s for s, _ in intervals)
        ends = sorted((e for _, e in intervals if e), reverse=True)
        status, used, tstart, tend, tname = _tiingo_lookup(client, t)
        multi = len(intervals) > 1
        rows.append(Row(
            ticker=t, intervals=len(intervals), multi_interval=multi,
            sp500_first=starts[0] if starts else "",
            sp500_last=ends[0] if ends else "",
            tiingo=status, tiingo_symbol=used, tiingo_start=tstart,
            tiingo_end=tend, tiingo_name=tname,
            cik=ciks.get(t.upper(), ""),
            recommendation=_recommend(status, multi),
        ))
        if i % 50 == 0 or i == len(unresolved):
            print(f"[classify] {i}/{len(unresolved)}", flush=True)
    return rows


def main() -> int:
    rows = classify()
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.recommendation] += 1
    tiingo_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        tiingo_counts[r.tiingo] += 1

    print("\n=== classification summary ===")
    print(f"  total unresolved: {len(rows)}")
    print("  by recommendation:")
    for k in ("add", "translate", "unavailable", "REVIEW"):
        print(f"    {k:12} {counts.get(k, 0)}")
    print("  by Tiingo status:")
    for k, v in sorted(tiingo_counts.items()):
        print(f"    {k:16} {v}")
    print(f"  multi-interval (possible reuse): {sum(1 for r in rows if r.multi_interval)}")
    print(f"  CIK resolved:  {sum(1 for r in rows if r.cik)}")
    print(f"\n  report -> {OUT_CSV.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
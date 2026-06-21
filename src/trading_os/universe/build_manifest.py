"""
Generic holdings-CSV -> coverage-manifest builder.

Turns any vendor holdings/constituent CSV into the standard manifest format
(ticker, security_type). One tool for SP500 (SPY holdings), Russell 1000 (IWB
holdings), NASDAQ-100, etc. — one invocation per source, no per-index builders.
The output is just data; the universe engine consumes manifests generically and
never imports this builder.

Usage:
    PYTHONPATH=src python -m trading_os.universe.build_manifest \
        --input ~/Downloads/holdings-daily-us-en-spy.csv \
        --out src/trading_os/universe/manifests/sp500.csv \
        --ticker-col Ticker --security-type EQUITY [--min-count 480]

Then register it (once) in manifests/registry.csv:
    sp500,sp500.csv,EQUITY

The holdings CSV usually has metadata rows above the real header; this finds the
header row containing --ticker-col, extracts that column, drops non-equity lines
(cash/derivatives), de-dupes, and writes the manifest. Verify the printed count.

Repo path: src/trading_os/universe/build_manifest.py
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# Non-security line items that appear in holdings files (cash management, etc).
_SKIP = {"", "-", "--", "CASH", "USD", "CASH_USD", "USD CASH", "US DOLLAR"}

# Bloomberg-style placeholder identifiers (e.g. "2602335D"): a run of digits
# ending in a single letter, used for positions without a permanent ticker
# (spinoff stubs, when-issued, pending listings). Security-master quality rule:
# these must never become sec.security identities.
_PLACEHOLDER_RE = re.compile(r"^\d+[A-Z]$")


def _find_header(lines: list[str], ticker_col: str) -> int | None:
    target = ticker_col.strip().lower()
    for i, line in enumerate(lines):
        cells = [c.strip().strip('"').lower() for c in line.split(",")]
        if target in cells:
            return i
    return None


def build(input_path: Path, ticker_col: str, security_type: str,
          out_path: Path, min_count: int = 0) -> int:
    with input_path.open(newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()

    hdr = _find_header(lines, ticker_col)
    if hdr is None:
        print(f"[build] could not find a header row containing column "
              f"'{ticker_col}'. Open the file and pass the correct --ticker-col.",
              file=sys.stderr)
        return 1

    tickers: list[str] = []
    seen: set[str] = set()
    for row in csv.DictReader(lines[hdr:]):
        raw = (row.get(ticker_col) or "").strip().upper()
        if raw in _SKIP or " " in raw:
            continue
        if _PLACEHOLDER_RE.match(raw):
            continue
        # Tickers are short alnum, optionally with class punctuation (. - /).
        if not raw or not all(c.isalnum() or c in ".-/" for c in raw):
            continue
        # Tickers are short alnum, optionally with class punctuation (. - /).
        if not raw or not all(c.isalnum() or c in ".-/" for c in raw):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        tickers.append(raw)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "security_type"])
        for t in tickers:
            w.writerow([t, security_type])

    print(f"[build] wrote {len(tickers)} rows ({security_type}) -> {out_path}")
    if len(tickers) == 0:
        print("[build] WARNING: 0 tickers extracted. Check --ticker-col and the file.",
              file=sys.stderr)
    elif min_count and len(tickers) < min_count:
        print(f"[build] WARNING: got {len(tickers)} tickers, below --min-count "
              f"{min_count}. Check --ticker-col and that this is the right file.",
              file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build a coverage manifest from a vendor holdings CSV."
    )
    p.add_argument("--input", required=True, type=Path,
                   help="path to the downloaded holdings CSV")
    p.add_argument("--out", required=True, type=Path,
                   help="output manifest path, e.g. .../manifests/sp500.csv")
    p.add_argument("--ticker-col", default="Ticker",
                   help="name of the ticker column in the holdings file (default: Ticker)")
    p.add_argument("--security-type", default="EQUITY",
                   help="security_type to stamp on every row (default: EQUITY)")
    p.add_argument("--min-count", type=int, default=0,
                   help="warn if fewer than this many tickers are extracted")
    args = p.parse_args(argv)
    if not args.input.exists():
        print(f"[build] input not found: {args.input}", file=sys.stderr)
        return 1
    return build(args.input, args.ticker_col, args.security_type.strip().upper(),
                 args.out, args.min_count)


if __name__ == "__main__":
    raise SystemExit(main())
"""
CLI entry point. Orchestrates the five-ticker validation ingest (DEC-011).

Usage (from repo root, with the venv active):
    python -m trading_os.connectors.edgar.cli            # all five tickers
    python -m trading_os.connectors.edgar.cli --ticker AAPL
    python -m trading_os.connectors.edgar.cli --dry-run  # download+parse, no DB writes

Hard scope guard: --ticker must be one of the DEC-011 validation set. Any other
value is refused. This is the structural enforcement of the "validate before
scaling" decision; lifting it requires editing config.VALIDATION_TICKERS and
amending DECISIONS.md.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg

from .client import EdgarClient
from .config import VALIDATION_TICKERS, EdgarConfig
from .mapper import ConceptMapper
from .models import MappedFact, UnmappedTag
from .parser import parse_bronze
from .writer import FactWriter


def _load_alias_rows(conn: psycopg.Connection) -> list[dict]:
    cur = conn.execute(
        """
        select a.source_tag, a.concept_id, c.canonical_name,
               a.priority, a.mapping_confidence, c.expected_unit,
               c.prefer_higher_confidence
        from fund.concept_alias a
        join fund.concept c on c.concept_id = a.concept_id
        join ref.data_source s on s.source_id = a.source_id
        where s.name = 'SEC_EDGAR'
        """
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def ingest_ticker(ticker: str, cik: str, conn: psycopg.Connection,
                  config: EdgarConfig, mapper: ConceptMapper,
                  dry_run: bool) -> dict:
    client = EdgarClient(config)
    ref = client.fetch_company_facts(ticker, cik)

    raw_facts = list(parse_bronze(ref))
    mapped: list[MappedFact] = []
    unmapped: list[UnmappedTag] = []
    for rf in raw_facts:
        result = mapper.map_fact(rf)
        (mapped if isinstance(result, MappedFact) else unmapped).append(result)

    summary = {
        "ticker": ticker, "bronze": ref.path,
        "raw": len(raw_facts), "mapped": len(mapped), "unmapped": len(unmapped),
    }
    if dry_run:
        summary["mode"] = "dry-run (no DB writes)"
        return summary

    writer = FactWriter(conn, config)
    kt = datetime.now(timezone.utc)
    batch_id = writer.open_batch(
        dataset="fund.fundamental_fact",
        knowledge_time=kt,
        params={"ticker": ticker, "cik": cik, "bronze": ref.path},
    )
    try:
        security_id = writer.resolve_or_create_security(ticker, batch_id)
        written, conflicts = writer.write_facts(security_id, mapped, batch_id)

        # Log unmapped against the most recent filing for this security.
        last_filing = conn.execute(
            "select filing_id from fund.filing where security_id=%s order by filed_at desc limit 1",
            (security_id,),
        ).fetchone()
        if last_filing:
            writer.log_unmapped(last_filing[0], unmapped)

        writer.close_batch(batch_id, "succeeded", rows_in=len(raw_facts), rows_out=written)
        conn.commit()
        summary.update(written=written, conflicts=len(conflicts), batch_id=batch_id)
    except Exception as e:  # noqa: BLE001 — we want to record any failure
        conn.rollback()
        # Best-effort: mark the batch failed in a fresh transaction.
        try:
            writer.close_batch(batch_id, "failed", rows_in=len(raw_facts),
                               rows_out=0, error=str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EDGAR Company Facts connector (V0).")
    p.add_argument("--ticker", help="single ticker from the validation set")
    p.add_argument("--dry-run", action="store_true",
                   help="download + parse + map, but write nothing to Postgres")
    args = p.parse_args(argv)

    if args.ticker:
        t = args.ticker.upper()
        if t not in VALIDATION_TICKERS:
            print(f"Refused: {t} is outside the DEC-011 validation set "
                  f"{sorted(VALIDATION_TICKERS)}. Amend config + DECISIONS.md to expand.",
                  file=sys.stderr)
            return 2
        targets = {t: VALIDATION_TICKERS[t]}
    else:
        targets = dict(VALIDATION_TICKERS)

    config = EdgarConfig()
    with psycopg.connect(config.pg_conninfo) as conn:
        mapper = ConceptMapper.from_rows(_load_alias_rows(conn))
        for ticker, cik in targets.items():
            try:
                s = ingest_ticker(ticker, cik, conn, config, mapper, args.dry_run)
                print(f"[ok] {ticker}: " +
                      ", ".join(f"{k}={v}" for k, v in s.items() if k != "ticker"))
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {ticker}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
Parse an immutable bronze Company Facts file into typed RawFacts.

Reads from disk ONLY (DEC-012): never touches the network. A future parsing
fix re-runs this against stored bronze, not a fresh download.

Company Facts JSON shape:
  {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
      "us-gaap": {
        "Revenues": {
          "label": "...", "description": "...",
          "units": {
            "USD": [
              {"start":"2019-01-01","end":"2019-03-31","val":58015000000,
               "accn":"0000320193-19-000066","fy":2019,"fp":"Q2",
               "form":"10-Q","filed":"2019-05-01","frame":"CY2019Q1"},
              ...
            ]
          }
        }
      },
      "dei": { ... }
    }
  }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator

from .config import ACCEPTED_FORMS, ACCEPTED_TAXONOMIES
from .models import BronzeRef, RawFact


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s)


def parse_bronze(ref: BronzeRef) -> Iterator[RawFact]:
    """
    Yield one RawFact per accepted Company Facts entry.

    Filtering applied here:
      * taxonomy in ACCEPTED_TAXONOMIES (us-gaap only in V0)
      * form in ACCEPTED_FORMS (10-K / 10-Q and their amendments)
    Entries missing 'end', 'val', 'accn', or 'filed' are skipped (malformed).
    """
    doc = json.loads(Path(ref.path).read_text())
    facts = doc.get("facts", {})

    for taxonomy, tags in facts.items():
        if taxonomy not in ACCEPTED_TAXONOMIES:
            continue
        for tag, body in tags.items():
            units = body.get("units", {})
            for unit, entries in units.items():
                for e in entries:
                    form = e.get("form")
                    if form not in ACCEPTED_FORMS:
                        continue
                    end = e.get("end")
                    val = e.get("val")
                    accn = e.get("accn")
                    filed = e.get("filed")
                    if end is None or val is None or accn is None or filed is None:
                        continue
                    try:
                        yield RawFact(
                            taxonomy=taxonomy,
                            tag=tag,
                            unit=unit,
                            value=float(val),
                            period_start=_parse_date(e.get("start")),
                            period_end=_parse_date(end),
                            form=form,
                            fiscal_year=e.get("fy"),
                            fiscal_period=e.get("fp"),
                            accession=accn,
                            filed_date=_parse_date(filed),
                        )
                    except (ValueError, TypeError):
                        # Unparseable value/date — skip rather than corrupt.
                        continue
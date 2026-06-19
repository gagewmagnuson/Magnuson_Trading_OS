"""
Trading-calendar connector configuration.

Repo path: src/trading_os/connectors/calendars/config.py

V0 scope: a single exchange calendar — XNYS (NYSE) — which defines the US
equity/ETF trading day (DEC-008). XNAS/ARCX share XNYS's regular session
schedule and add no architectural value yet; they can be added later as extra
ref.exchange rows once a model needs venue-specific sessions. Same discipline as
the 5-ticker / 12-series cohorts: validate first, scale later.

Date horizon is EXPLICITLY PINNED, never the library's rolling default. With no
bounds, get_calendar('XNYS') returns roughly a 20-year window anchored on today,
so the populated table would change run-to-run. Pinning fixed bounds makes the
same config produce the same table (reproducibility).

START = 1962-01-01 — the earliest the library is FAITHFUL for XNYS in practice
(pre-1952 Saturday sessions are not modeled) and well before any data we can
currently join against for free (FRED macro reaches the 1940s; EDGAR the 1990s).
Deep equity-price history is a paid, post-POC concern (DEC-007); 1962 is harmless
headroom, not an unlock on its own.

END = 2030-12-31 — a fixed forward horizon. Bump this one line and re-run to
extend; the upsert is idempotent, so re-running never duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The V0 exchange: (mic, name, country, IANA timezone).
#   mic      : ISO 10383 Market Identifier Code (coincides with the
#              exchange_calendars calendar code for XNYS).
#   timezone : IANA tz, stored on ref.exchange for local-time reasoning.
XNYS_MIC = "XNYS"
XNYS_NAME = "New York Stock Exchange"
XNYS_COUNTRY = "US"
XNYS_TZ = "America/New_York"

# exchange_calendars calendar code to request for the above MIC.
CALENDAR_CODE = "XNYS"

# Explicitly pinned, reproducible horizon (NOT the library's rolling default).
START = date(1962, 1, 1)
END = date(2030, 12, 31)


@dataclass(frozen=True)
class CalendarsConfig:
    mic: str = XNYS_MIC
    name: str = XNYS_NAME
    country: str = XNYS_COUNTRY
    timezone: str = XNYS_TZ
    calendar_code: str = CALENDAR_CODE
    start: date = START
    end: date = END
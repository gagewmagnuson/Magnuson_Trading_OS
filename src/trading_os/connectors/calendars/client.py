"""
Trading-calendar source: the `exchange_calendars` OSS library.

DELIBERATE TEMPLATE DEVIATION (no bronze, no parser): the standard template is
client -> parser -> [mapper] -> writer -> cli with an immutable bronze landing
(DEC-012). This connector omits bronze and parser because exchange_calendars is
a DETERMINISTIC, versioned OSS library, not a vendor feed — there is no raw
payload to land. Reproducibility comes from (a) pinning the library version in
pyproject.toml and (b) recording it in meta.ingest_batch.params each run. The
schema agrees: the library is the runtime source of truth; ref.trading_session
is a regenerable cache. This module does client+parser combined.
"""
from __future__ import annotations

import exchange_calendars as xcals

from .config import CalendarsConfig
from .models import ExchangeMeta, SessionRow


class CalendarClient:
    def __init__(self, config: CalendarsConfig):
        self.config = config

    @staticmethod
    def library_version() -> str:
        return xcals.__version__

    def exchange_meta(self) -> ExchangeMeta:
        c = self.config
        return ExchangeMeta(mic=c.mic, name=c.name, country=c.country,
                            timezone=c.timezone)

    def sessions(self) -> list[SessionRow]:
        cal = xcals.get_calendar(
            self.config.calendar_code,
            start=self.config.start.isoformat(),
            end=self.config.end.isoformat(),
        )
        early = set(cal.early_closes)
        rows: list[SessionRow] = []
        for label, row in cal.schedule.iterrows():
            rows.append(
                SessionRow(
                    session_date=label.date(),
                    open_utc=row["open"].to_pydatetime(),
                    close_utc=row["close"].to_pydatetime(),
                    is_half_day=label in early,
                )
            )
        return rows
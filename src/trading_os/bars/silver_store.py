"""
Generic silver-deployment infrastructure for the bars dataset (DEC-024, DEC-026).

Repo path: src/trading_os/bars/silver_store.py

Source-agnostic staging and atomic swap for the canonical silver bars layer. It
knows nothing about any vendor — it operates on a silver directory and its staging
sibling. A producer (a connector backfill, a rebuild) writes into staging, the
result is validated, and staging is atomically swapped in for live with the prior
silver preserved as a timestamped backup.

This is the deployment primitive both the Tiingo backfill and (after the deferred
cleanup) the Alpaca rebuild converge on. Until that cleanup, connectors/alpaca/
rebuild.py retains its own equivalent implementation — a tracked, intentional
duplication, not an accident.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def staging_dir(silver_dir: Path) -> Path:
    """The staging sibling of a silver dir (e.g. .../bars_eod -> .../bars_eod_staging)."""
    return silver_dir.parent / f"{silver_dir.name}_staging"


def clear_staging(silver_dir: Path) -> Path:
    """Ensure a fresh, empty staging dir (a build is deterministic; start clean)."""
    st = staging_dir(silver_dir)
    st.mkdir(parents=True, exist_ok=True)
    for f in st.glob("*.parquet"):
        f.unlink()
    return st


def silver_glob(d: Path) -> str:
    return f"{d.as_posix()}/*.parquet"


def swap_staging_into_live(silver_dir: Path) -> Path:
    """Replace live silver with staging: rename live -> timestamped backup, then
    staging -> live. Rolls the first rename back if the second fails, so the lake
    is never left without a live silver dir. Returns the backup path.

    (A microsecond window exists between the two renames; acceptable for a manual
    single-operator deploy with no concurrent readers. Symlink-swap is the upgrade
    if that ever changes.)
    """
    staging = staging_dir(silver_dir)
    if not staging.exists():
        raise RuntimeError("no staging directory to swap; run a build first")
    backup = silver_dir.parent / f"{silver_dir.name}_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    if silver_dir.exists():
        silver_dir.rename(backup)
    try:
        staging.rename(silver_dir)
    except Exception:
        if backup.exists():
            backup.rename(silver_dir)  # roll back
        raise
    return backup
"""
Shared credential & settings helper for the Trading OS.

Single source of truth for how the system reads secrets and connection info.
Every connector imports from here instead of touching os.environ directly, so
the credential-loading mechanism can evolve (shell export -> .env file ->
production secrets manager) without changing any connector code.

Resolution order for any key:
  1. A real environment variable (set by shell export, systemd, Docker, or a
     cloud secrets manager in production).
  2. A .env file at the repo root (developer convenience; gitignored).

In production the .env file simply won't exist, and real environment variables
injected by the deployment environment take over. No code changes required.

Repo path: src/trading_os/config/settings.py
"""
from __future__ import annotations

import os
from pathlib import Path

# Load a repo-root .env into os.environ if present. python-dotenv does NOT
# override variables that are already set, so a real env var always wins over
# the .env file — which is exactly the precedence we want for production.
try:
    from dotenv import load_dotenv
    # Find the repo root by walking up until we see a .git dir (or fall back
    # to cwd). This makes the loader work regardless of where Python is invoked.
    _here = Path(__file__).resolve()
    _root = next(
        (p for p in _here.parents if (p / ".git").exists()),
        Path.cwd(),
    )
    load_dotenv(_root / ".env")
except ImportError:
    # python-dotenv not installed: rely solely on real environment variables.
    # Connectors will still work in any environment where vars are set directly.
    pass


class MissingCredential(RuntimeError):
    """Raised when a required secret is absent from the environment and .env."""


def get(name: str, default: str | None = None) -> str | None:
    """Read a setting; return default (or None) if unset."""
    return os.environ.get(name, default)


def require(name: str) -> str:
    """
    Read a REQUIRED secret. Raises a clear, actionable error if missing,
    rather than letting a None propagate into a confusing failure deep in a
    connector.
    """
    val = os.environ.get(name)
    if not val:
        raise MissingCredential(
            f"{name} is not set. Add it to your shell environment "
            f"(export {name}=...) or to the repo-root .env file. "
            f"See .env.example for the expected keys."
        )
    return val


# --- Named accessors for known credentials/settings -------------------------
# Adding a new credential is a one-line addition here; connectors call these
# rather than hard-coding env var names, so renames happen in one place.

def fred_api_key() -> str:
    return require("TRADING_OS_FRED_KEY")


def pg_conninfo() -> str:
    # Postgres connection. Defaults to the local dev database.
    return get("TRADING_OS_PG", "dbname=tradingos")


def lake_root() -> str:
    return get("TRADING_OS_LAKE", "lake")


def sec_user_agent() -> str:
    return get("TRADING_OS_SEC_UA", "Magnuson Trading OS admin@example.com")
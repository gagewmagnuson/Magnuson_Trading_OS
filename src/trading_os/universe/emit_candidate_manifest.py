"""
Emit a CANDIDATE coverage manifest for the survivorship expansion (read-only, deterministic).

Repo path: src/trading_os/universe/emit_candidate_manifest.py
Run:  PYTHONPATH=src python -m trading_os.universe.emit_candidate_manifest

Consumes the CORROBORATED classification (corroborated_report.csv) and the
version-controlled human decisions, and emits a self-auditing candidate manifest
under working/. The candidate is NOT part of the coverage registry: a human
reviews it, then copies the lean production columns into
manifests/002_survivorship_expansion.csv (+ a registry row). Only the approved
manifest feeds UniverseWriter.

Admission (Migration A — conservative, confidence over recall):
  - add:overlap_ok            (Tiingo coverage overlaps the S&P-membership window)
      MINUS 5 thin-window reusers found in review (HOT/HONA/PEAK/WRK/SOLS)
  - verified continuous (12)  and verified new-identity (5) from the 21 REVIEW cases
  - dot-class translations (2)
Everything else is EXCLUDED (deferred to human review or unavailable) and recorded
with a reason. Honest window: valid_from = Tiingo startDate (never fabricated);
valid_to = Tiingo endDate unless still trading -> open.

Two artifacts:
  candidate_survivorship_<date>.csv           -- production columns + provenance
  candidate_survivorship_<date>_summary.txt   -- reconciled counts, timestamp, hashes
"""
from __future__ import annotations

import csv
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CORROBORATED_CSV = Path("corroborated_report.csv")
WORKING_DIR = Path("working")
ACTIVE_TOLERANCE_DAYS = 7
CLASSIFIER_VERSION = "corroborate-v1 (overlap-based, CIK/name dropped)"

# The 5 thin-window reusers found in manual review of the add bucket (excluded).
THIN_WINDOW_REUSE = {"HOT", "HONA", "PEAK", "WRK", "SOLS"}

# The 21 REVIEW decisions (verified against company history). ADD = continuous |
# new_identity; both admitted with Tiingo's honest window (identical handling, the
# distinction is documentary). DEFER = manual_review | unavailable (excluded).
NEW_IDENTITY = {
    "H": "Harcourt (pre-2009) vs Hyatt IPO 2009",
    "MIR": "Mirant (1997-2003) vs Mirion SPAC 2021",
    "DXC": "CSC-era vs DXC Technology 2017 merger",
    "AAL": "pre-2005 holder vs American Airlines Group",
    "NE": "Noble Corp reorganized entity 2021",
}
CONTINUOUS = {
    "AN": "AutoNation; index churn", "CE": "Celanese; index churn",
    "COV": "Covidien; Tyco spin 2007 -> Medtronic 2015", "FL": "Foot Locker; index churn",
    "FMC": "FMC Corp; index churn", "GAS": "AGL Resources -> Southern 2016",
    "GGP": "General Growth Properties; index churn", "HP": "Helmerich & Payne; index churn",
    "KSU": "Kansas City Southern -> CP 2021", "MXIM": "Maxim -> ADI 2021",
    "OI": "O-I Glass; index churn", "RIG": "Transocean; index churn",
}
DEFER_21 = {
    "CBE": "Tiingo metadata window blank -> defer",
    "MEE": "Tiingo metadata window blank -> defer",
    "ESV": "Tiingo 404; no priceable data",
    "HRS": "Tiingo 404; no priceable data",
}


def _d(s: str | None) -> date | None:
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None


def _valid_to(tiingo_end: str) -> str:
    end = _d(tiingo_end)
    if end is None:
        return ""
    if end >= datetime.now(timezone.utc).date() - timedelta(days=ACTIVE_TOLERANCE_DAYS):
        return ""                       # still trading -> open bound
    return end.isoformat()


def _classify(r: dict) -> tuple[bool, str, str]:
    """(admit, decision, rationale) for one corroborated row."""
    t = r["ticker"]
    corr = r["corroboration"]
    if t in THIN_WINDOW_REUSE:
        return False, "excluded_reuse", "thin-window reuser (manual review)"
    if t in NEW_IDENTITY:
        return True, "new_identity", NEW_IDENTITY[t]
    if t in CONTINUOUS:
        return True, "verified_continuous", CONTINUOUS[t]
    if t in DEFER_21:
        kind = "manual_review" if t in ("CBE", "MEE") else "unavailable"
        return False, f"excluded_{kind}", DEFER_21[t]
    if corr == "add:overlap_ok":
        return True, "overlap_ok", "Tiingo coverage overlaps S&P membership window"
    if corr == "passthrough:translate":
        if not r.get("tiingo_start", "").strip():
            return False, "excluded_blank", "dot-class ticker; translated but no Tiingo window"
        return True, "translate", "dot-class ticker; add via Tiingo symbol translation"
    if corr == "review:modern_reuse":
        return False, "excluded_reuse", "Tiingo coverage begins after S&P exit (reuse)"
    if corr == "review:blank_window":
        return False, "excluded_blank", "no Tiingo coverage window; cannot date"
    if corr == "passthrough:unavailable":
        return False, "excluded_unavailable", "Tiingo 404; no priceable data"
    if corr == "passthrough:REVIEW":
        return False, "excluded_review", "unadjudicated REVIEW (should be in overrides)"
    return False, "excluded_other", f"unhandled corroboration={corr}"


def emit() -> Path:
    if not CORROBORATED_CSV.exists():
        raise SystemExit(f"missing {CORROBORATED_CSV}; run corroborate first.")
    rows = list(csv.DictReader(CORROBORATED_CSV.open()))

    WORKING_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    manifest_path = WORKING_DIR / f"candidate_survivorship_{stamp}.csv"
    summary_path = WORKING_DIR / f"candidate_survivorship_{stamp}_summary.txt"

    admitted: list[dict] = []
    decision_counts: dict[str, int] = {}
    for r in rows:
        admit, decision, rationale = _classify(r)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        if admit:
            # honest-date invariant: an admitted row MUST have a Tiingo start.
            if not r.get("tiingo_start", "").strip():
                raise SystemExit(
                    f"ADMITTED row {r['ticker']!r} has no tiingo_start — cannot write "
                    f"an honest valid_from. Aborting (fix classification)."
                )
            admitted.append({
                "ticker": r["ticker"],
                "security_type": "EQUITY",
                "name": r.get("tiingo_name", ""),
                "valid_from": r["tiingo_start"][:10],
                "valid_to": _valid_to(r.get("tiingo_end", "")),
                "decision": decision,          # provenance (review aid; not read by loader)
                "rationale": rationale,        # provenance
            })

    admitted.sort(key=lambda d: d["ticker"])
    fieldnames = ["ticker", "security_type", "name", "valid_from", "valid_to",
                  "decision", "rationale"]
    with manifest_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(admitted)

    manifest_bytes = manifest_path.read_bytes()
    sha = hashlib.sha256(manifest_bytes).hexdigest()[:16]

    # reconciled breakdown
    add_by = {}
    for a in admitted:
        add_by[a["decision"]] = add_by.get(a["decision"], 0) + 1
    excl = {k: v for k, v in decision_counts.items() if k.startswith("excluded_")}

    lines = [
        "Candidate Manifest Summary",
        "--------------------------",
        f"generated_at (UTC):   {datetime.now(timezone.utc).isoformat()}",
        f"classifier version:   {CLASSIFIER_VERSION}",
        f"source:               {CORROBORATED_CSV.name} ({len(rows)} rows)",
        f"manifest sha256[:16]: {sha}",
        f"manifest file:        {manifest_path.name}",
        "",
        "Admitted (candidate rows):",
        f"  historical overlap:      {add_by.get('overlap_ok', 0)}",
        f"  verified continuous:     {add_by.get('verified_continuous', 0)}",
        f"  verified new-identity:   {add_by.get('new_identity', 0)}",
        f"  ticker translations:     {add_by.get('translate', 0)}",
        f"  TOTAL CANDIDATE ROWS:    {len(admitted)}",
        "",
        "Excluded:",
        f"  modern reuse:            {excl.get('excluded_reuse', 0)}",
        f"  blank window:            {excl.get('excluded_blank', 0)}",
        f"  manual review (defer):   {excl.get('excluded_manual_review', 0)}",
        f"  unavailable:             {excl.get('excluded_unavailable', 0)}",
        f"  other:                   {sum(v for k, v in excl.items() if k not in ('excluded_reuse','excluded_blank','excluded_manual_review','excluded_unavailable'))}",
        "",
        f"  TOTAL EXCLUDED:          {sum(excl.values())}",
        f"  RECONCILES TO:           {len(admitted) + sum(excl.values())} (should equal {len(rows)})",
    ]
    summary = "\n".join(lines)
    summary_path.write_text(summary + "\n")
    print(summary)
    print(f"\n  -> {manifest_path}")
    print(f"  -> {summary_path}")
    print("\n  REVIEW the candidate, then copy the production columns "
          "(ticker,security_type,name,valid_from,valid_to) into "
          "manifests/002_survivorship_expansion.csv + a registry row.")
    return manifest_path


if __name__ == "__main__":
    raise SystemExit(emit())
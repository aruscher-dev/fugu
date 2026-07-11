#!/usr/bin/env python3
"""Phase 6 -- evaluation report (PLAN.md phase table: "ACPL, blunder rate,
win-rate, illegal-move rate"). Pure aggregation over Phase 5's raw
per-condition game records at logs/phase5_matches/<condition>.json
(gitignored, written by scripts/phase5_baseline_and_fugu_matches.py) -- no
GPU/model access needed, same collect-then-aggregate split as Phase 3
(collect) -> Phase 4 (aggregate + train).

Does NOT compute a live "majority-vote" condition: PLAN.md's Verification
section names it alongside Open-Fugu/solo/random-routing, but Phase 5 only
ever plays the 5 conditions described in its "Cloud dev routine additions
(2026-07-11)" STATUS.md write-up (which flagged this gap explicitly as
worth a second look). A real majority-vote condition needs per-ply
cross-worker comparison at IDENTICAL positions -- a different game loop
than Phase 5's independent single-orchestrator-per-game structure -- so it
can't be reconstructed after the fact from the 3 solo conditions' games
(their move sequences diverge from ply 1 onward; they were never played on
synced positions). Reported here as an explicit gap (`majority_vote` is
always null, with a `majority_vote_note` explaining why) rather than
silently omitted or faked from mismatched data. A live majority-vote
condition, if still wanted, is future work (a Phase 5 extension, or a new
Phase 6.5) -- not something this aggregation-only phase can retrofit.

Run standalone: python3 scripts/phase6_eval_report.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from open_fugu.eval.aggregate_metrics import build_report, format_markdown_table  # noqa: E402

RESULTS_DIR = PROJECT_DIR / "logs" / "phase5_matches"  # gitignored, host-specific
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked -- small summaries only

MAJORITY_VOTE_NOTE = (
    "not computed -- a real majority-vote condition needs per-ply cross-worker "
    "comparison at identical positions, which Phase 5 does not play (its solo "
    "conditions' games diverge from ply 1 onward); see this script's module "
    "docstring"
)


def load_conditions() -> dict:
    conditions = {}
    if not RESULTS_DIR.exists():
        return conditions
    for path in sorted(RESULTS_DIR.glob("*.json")):
        r = json.loads(path.read_text())
        conditions[path.stem] = r.get("detail", {}).get("games", [])
    return conditions


def write_report(report: dict, verdict: str, note: str) -> None:
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "note": note,
        "majority_vote": None,
        "majority_vote_note": MAJORITY_VOTE_NOTE,
        **report,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    out_path = REPORTS_DIR / "phase6_eval_report.json"
    out_path.write_text(json.dumps(out, indent=2))
    out_path.chmod(0o600)

    md_path = REPORTS_DIR / "phase6_eval_report.md"
    md_path.write_text(format_markdown_table(out))
    md_path.chmod(0o600)


def main():
    phase5_summary_path = REPORTS_DIR / "phase5_summary.json"
    if not phase5_summary_path.exists():
        print("Phase 5 hasn't produced reports/phase5_summary.json yet -- nothing to report on.")
        write_report({}, verdict="NO_DATA",
                      note="reports/phase5_summary.json not found -- Phase 5 hasn't run yet.")
        return

    phase5_summary = json.loads(phase5_summary_path.read_text())
    conditions = load_conditions()
    if not conditions:
        print("logs/phase5_matches/ has no per-condition result files yet -- nothing to report on.")
        write_report({}, verdict="NO_DATA",
                      note="logs/phase5_matches/ has no per-condition result files yet.")
        return

    report = build_report(conditions)
    verdict = "COMPLETE" if phase5_summary.get("verdict") == "COMPLETE" else "PARTIAL"
    write_report(report, verdict=verdict,
                 note=(f"built from {len(conditions)} condition(s) present in "
                       f"logs/phase5_matches/ at report time; Phase 5's own summary "
                       f"verdict was '{phase5_summary.get('verdict')}'"))
    print(f"Verdict: {verdict} ({len(conditions)} condition(s)): "
          f"reports/phase6_eval_report.{{json,md}}")


if __name__ == "__main__":
    main()

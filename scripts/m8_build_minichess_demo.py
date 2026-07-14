#!/usr/bin/env python3
"""m8 -- build the interactive HTML demo (PLAN.md minichess phase table: "the
actual deliverable"). Pure aggregation/rendering over m7's already-written,
tracked per-condition game records at reports/minichess_demo/<condition>.json
(NOT logs/ -- per PLAN.md's own instruction those files are tracked so a
future GPU-less session can build this demo directly from them) -- no
GPU/model access needed, same collect-then-build split as Phase 3/4 and m4/m5
before it, and the same "runs synchronously, no tmux, no gpu_spend_approved
gate" reasoning as Phase 6's aggregation-only report (advance_m8 in
orchestrate.py only reads data a dev session already approved collecting).

Requires ONLY `pyffish` (open_fugu.minichess.board.GardnerBoard's real
move-generator, needed to replay each game's FEN timeline for the board
animation -- see open_fugu.minichess.demo_render) -- unlike m7 itself, this
does NOT need bin/fairy-stockfish (no search/eval happens here, only replay
of already-recorded moves) or the GPU (no worker inference happens here
either), so this can run on a host that has m7's output checked out but
isn't currently doing GPU work, or (per demo_render.py's own module
docstring) even in a no-GPU dev sandbox that can `pip install pyffish`.

Run standalone: python3 scripts/m8_build_minichess_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from open_fugu.minichess.demo_render import CONDITION_ORDER, build_demo_data, render_html  # noqa: E402

REPORTS_DIR = PROJECT_DIR / "reports"  # tracked -- small summaries + the demo itself
DEMO_DIR = REPORTS_DIR / "minichess_demo"  # m7's tracked per-condition game records live here too
M7_SUMMARY_PATH = REPORTS_DIR / "m7_summary.json"
DEMO_HTML_PATH = DEMO_DIR / "index.html"
M8_SUMMARY_PATH = REPORTS_DIR / "m8_summary.json"


def load_conditions() -> dict:
    """{condition_id: [game dict, ...]} for every condition whose
    reports/minichess_demo/<condition>.json file exists at build time --
    mirrors phase6_eval_report.py's load_conditions()/build_report() split
    (this function stays agnostic to whether that's "enough" data; main()
    decides the verdict from m7's own summary instead, same reasoning
    Phase 6 gives for not re-deriving completeness from the raw files)."""
    conditions = {}
    for cid in CONDITION_ORDER:
        path = DEMO_DIR / f"{cid}.json"
        if path.exists():
            conditions[cid] = json.loads(path.read_text())
    return conditions


def write_summary(verdict: str, note: str, n_conditions: int, n_games: int) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "note": note,
        "n_conditions": n_conditions,
        "n_games": n_games,
        "demo_html": str(DEMO_HTML_PATH.relative_to(PROJECT_DIR)) if verdict == "COMPLETE" else None,
    }
    M8_SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    M8_SUMMARY_PATH.chmod(0o600)


def main():
    if not M7_SUMMARY_PATH.exists():
        print("m7 hasn't produced reports/m7_summary.json yet -- nothing to build the demo from.")
        write_summary("NO_DATA", "reports/m7_summary.json not found -- m7 hasn't run yet.", 0, 0)
        return

    m7_summary = json.loads(M7_SUMMARY_PATH.read_text())
    conditions = load_conditions()
    if not conditions:
        print("reports/minichess_demo/ has no per-condition game files yet -- nothing to build.")
        write_summary("NO_DATA", "reports/minichess_demo/<condition>.json files not found yet.", 0, 0)
        return

    n_games = sum(len(g) for g in conditions.values())
    demo_data = build_demo_data(conditions)
    html = render_html(demo_data, generated_at=datetime.now(timezone.utc).isoformat())

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    DEMO_DIR.chmod(0o700)
    DEMO_HTML_PATH.write_text(html)
    DEMO_HTML_PATH.chmod(0o600)

    verdict = "COMPLETE" if m7_summary.get("verdict") == "COMPLETE" else "PARTIAL"
    write_summary(
        verdict,
        note=(f"built from {len(conditions)}/{len(CONDITION_ORDER)} condition(s) present in "
              f"reports/minichess_demo/ at build time ({n_games} games total); m7's own summary "
              f"verdict was '{m7_summary.get('verdict')}'. Re-run this script any time m7's "
              f"per-condition files change -- it always rewrites reports/minichess_demo/index.html "
              f"from scratch (cheap: pure JSON + pyffish replay, no GPU/model/engine-binary access)."),
        n_conditions=len(conditions),
        n_games=n_games,
    )
    print(f"Verdict: {verdict} ({len(conditions)}/{len(CONDITION_ORDER)} condition(s), "
          f"{n_games} games): wrote {DEMO_HTML_PATH.relative_to(PROJECT_DIR)}")


if __name__ == "__main__":
    main()

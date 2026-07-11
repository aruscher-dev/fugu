"""Phase 6 -- evaluation report: turns Phase 5's raw per-condition game
records (chess_green_agent.py's EvalResult-shaped per-game dicts, persisted
at logs/phase5_matches/<condition>.json by
scripts/phase5_baseline_and_fugu_matches.py) into the Fugu-paper-style
comparison PLAN.md's Verification section calls for (ACPL, blunder rate,
win-rate, illegal-move rate; Open-Fugu vs. each solo worker vs.
random-routing).

Pure-logic module -- no torch/A2A/chess import needed, since every input
field it reads (`result`, `llm_color`, `termination`, `legal_move_rate`,
`mean_acpl`, `blunder_rate`) is already a plain value in the per-game dict
chess_green_agent.py writes. Same split every earlier phase drew between
GPU-dependent code and pure-logic code that a no-GPU sandbox can actually
unit-test against hand-constructed dicts shaped like the real thing.
"""
from __future__ import annotations

from typing import Optional

SOLO_PREFIX = "solo_"


def game_outcome(game: dict) -> str:
    """LLM-perspective outcome for one game dict (chess_green_agent.py's
    per-game summary shape: `result` is a chess.Board.result()-style string
    -- "1-0"/"0-1"/"1/2-1/2"/"*" -- and `llm_color` is "white"/"black").
    "*" (harness.py's `termination == "max_plies"`, i.e. the ply cap was hit
    with no decisive result) is its own "unresolved" bucket, kept distinct
    from "draw" since it reflects the eval's ply budget, not gameplay
    reaching an actual drawn position."""
    result = game.get("result")
    color = game.get("llm_color")
    if result == "1/2-1/2":
        return "draw"
    if result not in ("1-0", "0-1"):
        return "unresolved"
    llm_won = (result == "1-0" and color == "white") or (result == "0-1" and color == "black")
    return "win" if llm_won else "loss"


def aggregate_condition(games: list) -> dict:
    """One condition's list of per-game dicts -> aggregate metrics. Mirrors
    the "skip None, mean of what exists" discipline
    write_phase0_5_summary/write_m2_summary in orchestrate.py already use
    for mean_legal_move_rate, extended here to mean_acpl/mean_blunder_rate
    plus outcome/illegal-move-rate bucketing over every game."""
    n_games = len(games)
    if n_games == 0:
        return {
            "n_games": 0, "mean_legal_move_rate": None, "mean_acpl": None,
            "mean_blunder_rate": None, "win_rate": None, "draw_rate": None,
            "loss_rate": None, "unresolved_rate": None, "illegal_move_rate": None,
        }

    def _mean(key: str) -> Optional[float]:
        vals = [g[key] for g in games if g.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    outcomes = [game_outcome(g) for g in games]
    illegal = sum(1 for g in games if g.get("termination") == "illegal_move")

    return {
        "n_games": n_games,
        "mean_legal_move_rate": _mean("legal_move_rate"),
        "mean_acpl": _mean("mean_acpl"),
        "mean_blunder_rate": _mean("blunder_rate"),
        "win_rate": outcomes.count("win") / n_games,
        "draw_rate": outcomes.count("draw") / n_games,
        "loss_rate": outcomes.count("loss") / n_games,
        "unresolved_rate": outcomes.count("unresolved") / n_games,
        "illegal_move_rate": illegal / n_games,
    }


COMPARISON_KEYS = ["mean_acpl", "mean_blunder_rate", "win_rate", "illegal_move_rate"]


def build_report(conditions: dict) -> dict:
    """conditions: {condition_id: [game dicts]} for every condition whose
    result file exists at report time -- callers decide whether that's
    "enough" to treat the report as final; this function makes no
    COMPLETE/PARTIAL judgment itself, mirroring Phase 3-collects/
    Phase-4-aggregates' split where the aggregator stays agnostic to how
    much upstream data exists.

    Adds one derived comparison beyond the raw per-condition aggregates:
    `vs_solo_mean` on every non-solo condition (`random_routing`,
    `open_fugu_sft`, or any future orchestrator condition) -- its delta
    against the plain average of the solo-worker baselines on each of
    COMPARISON_KEYS, which is the comparison PLAN.md's Verification section
    ("Open-Fugu vs. each solo worker vs. random-routing") is actually asking
    for. Sign convention: value - solo_mean, so positive means "higher than
    the solo average" (good for win_rate, bad for mean_acpl/
    mean_blunder_rate/illegal_move_rate) -- left for the reader/Phase 6's
    markdown table to interpret per-metric rather than baked into the sign.
    """
    per_condition = {cid: aggregate_condition(games) for cid, games in conditions.items()}

    solo_ids = [cid for cid in per_condition if cid.startswith(SOLO_PREFIX)]
    solo_metrics = [per_condition[cid] for cid in solo_ids]

    def _solo_mean(key: str) -> Optional[float]:
        vals = [m[key] for m in solo_metrics if m.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    solo_mean = {k: _solo_mean(k) for k in COMPARISON_KEYS} if solo_ids else {}

    for cid, metrics in per_condition.items():
        if cid in solo_ids:
            continue
        vs_solo = {}
        for k in COMPARISON_KEYS:
            baseline, value = solo_mean.get(k), metrics.get(k)
            vs_solo[k] = (value - baseline) if (baseline is not None and value is not None) else None
        metrics["vs_solo_mean"] = vs_solo

    return {
        "per_condition": per_condition,
        "solo_mean": solo_mean,
        "n_conditions_reported": len(per_condition),
    }


METRIC_COLUMNS = [
    ("n_games", "Games"),
    ("mean_legal_move_rate", "Legal-move rate"),
    ("mean_acpl", "Mean ACPL"),
    ("mean_blunder_rate", "Blunder rate"),
    ("win_rate", "Win rate"),
    ("draw_rate", "Draw rate"),
    ("loss_rate", "Loss rate"),
    ("unresolved_rate", "Unresolved rate"),
    ("illegal_move_rate", "Illegal-move rate"),
]


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def format_markdown_table(report: dict) -> str:
    """Human-readable rendering of write_report()'s combined dict (build_report()'s
    output plus the generated_at/verdict/majority_vote_note wrapper
    scripts/phase6_eval_report.py adds) -- the actual "evaluation report"
    deliverable PLAN.md's phase table names; reports/phase6_eval_report.json
    is this same data's machine-readable twin."""
    per_condition = report.get("per_condition", {})
    lines = [
        "# Open-Fugu Phase 6 evaluation report",
        "",
        f"Generated: {report.get('generated_at', 'unknown')}",
        f"Verdict: {report.get('verdict', 'unknown')}",
        "",
        "| Condition | " + " | ".join(label for _, label in METRIC_COLUMNS) + " |",
        "|" + "---|" * (len(METRIC_COLUMNS) + 1),
    ]
    for cid in sorted(per_condition):
        metrics = per_condition[cid]
        row = [cid] + [_fmt(metrics.get(key)) for key, _ in METRIC_COLUMNS]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    note = report.get("majority_vote_note")
    if note:
        lines.append(f"**Majority-vote condition**: {note}")
        lines.append("")
    return "\n".join(lines)

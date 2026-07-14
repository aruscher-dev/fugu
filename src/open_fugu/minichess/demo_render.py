"""m8 -- interactive HTML demo (Claude Artifact) built from m7's fixed-eval-suite
per-game records (reports/minichess_demo/<condition>.json). PLAN.md's minichess
phase table calls this "the actual deliverable": "animated board, stage
tabs/side-by-side comparison across the 3 coordination checkpoints,
routing-distribution + ACPL charts, step/play controls".

Pure-logic module other than open_fugu.minichess.board.GardnerBoard, which
needs only `pyffish` (a pure move-generator/FEN library, no GPU or engine
binary) -- reused directly to replay each game's real move sequence rather
than reimplementing 5x5 move application in JavaScript, so the board timeline
stays byte-identical to what m1-m7 already proved against real pyffish
legality checking (same "reuse already-proven code, don't duplicate board
logic" reasoning m1/m3/m6 already established for this track). The rendered
HTML's own board-drawing JS only ever needs to parse a FEN string into
squares, never apply a move -- a much smaller, safer surface to hand-write in
JS than full move legality/application.

aggregate_condition() is imported from Phase 6's open_fugu.eval.aggregate_metrics
rather than reimplemented -- it already computes exactly the
win/draw/loss/unresolved/illegal-move rates and mean ACPL/blunder-rate this
phase needs, over the same game-dict shape (game_outcome() reads `result`/
`llm_color`, both present in every m7 game record). build_report()'s
`vs_solo_mean` machinery is NOT reused, since it assumes Phase 6's
solo-worker-vs-orchestrator naming convention (a "solo_" prefix) that
doesn't apply to m8's three routing-checkpoint conditions -- aggregate_condition()
is called directly per condition instead.
"""
from __future__ import annotations

import json
from typing import Optional

from open_fugu.eval.aggregate_metrics import aggregate_condition
from open_fugu.minichess.board import GardnerBoard

CONDITION_ORDER = ["m3_random", "m5_sft", "m6_cmaes"]
CONDITION_LABELS = {
    "m3_random": "Random routing (checkpoint #1)",
    "m5_sft": "SFT-trained routing (checkpoint #2)",
    "m6_cmaes": "CMA-ES-evolved routing (checkpoint #3)",
}
CONDITION_SHORT_LABELS = {
    "m3_random": "Random (m3)",
    "m5_sft": "SFT (m5)",
    "m6_cmaes": "CMA-ES (m6)",
}

# Long reasoning-distill raw replies (<think> blocks) could otherwise bloat a
# single self-contained HTML file unpredictably across 12 games x ~24 plies --
# this only truncates what's DISPLAYED in the demo; the full untruncated text
# always stays in reports/minichess_demo/<condition>.json, m8 never rewrites it.
RAW_REPLY_MAX_CHARS = 2000


def build_game_timeline(game: dict) -> dict:
    """Replay one m7 game record's fixed opening + interleaved LLM/engine
    moves through a real GardnerBoard, producing a FEN snapshot after every
    ply so the demo can animate the actual position, not just the final one.

    Chronological order mirrors harness.play_blindfold_vs_engine's own game
    loop exactly: opening moves, then (iff it isn't the LLM's turn right
    after the opening -- the same `board.turn != llm_color` parity check
    harness.py itself uses) one leading engine reply, then for each LLM ply:
    the LLM's own move (board unchanged if illegal -- harness never pushes
    an illegal move before terminating the game), and -- only if that ply
    was legal AND didn't end the game -- the engine's next move, consumed
    from game["engine_moves"] in order (see harness.py's engine_move_log=
    parameter, added this session specifically so this replay is possible;
    GameResult.plies on its own only ever records the LLM's moves).
    """
    board = GardnerBoard()
    for mv in game.get("opening_uci_moves", []):
        board.push_uci(mv)

    engine_moves = list(game.get("engine_moves", []))
    engine_idx = 0
    llm_is_white = game.get("llm_color") == "white"
    if board.turn != llm_is_white and engine_idx < len(engine_moves):
        board.push_uci(engine_moves[engine_idx])
        engine_idx += 1

    start_fen = board.fen()
    frames = []
    for ply in game.get("plies", []):
        if ply.get("legal") and ply.get("move_uci"):
            board.push_uci(ply["move_uci"])
        raw_reply = ply.get("raw_reply") or ""
        truncated = len(raw_reply) > RAW_REPLY_MAX_CHARS
        frames.append({
            "ply": ply.get("ply"),
            "mover": ply.get("mover"),
            "worker_id": ply.get("worker_id"),
            "move_uci": ply.get("move_uci"),
            "legal": bool(ply.get("legal")),
            "centipawn_loss": ply.get("centipawn_loss"),
            "is_blunder": bool(ply.get("is_blunder")),
            "is_mistake": bool(ply.get("is_mistake")),
            "raw_reply": (raw_reply[:RAW_REPLY_MAX_CHARS] + "…") if truncated else raw_reply,
            "raw_reply_truncated": truncated,
            "fen": board.fen(),
        })
        if not ply.get("legal"):
            break  # harness terminates the game immediately -- no engine reply follows
        if board.is_game_over():
            break  # checkmate/stalemate/draw -- no engine reply follows either
        if engine_idx < len(engine_moves):
            board.push_uci(engine_moves[engine_idx])
            engine_idx += 1

    return {
        "opening": game.get("opening"),
        "opening_uci_moves": game.get("opening_uci_moves"),
        "llm_color": game.get("llm_color"),
        "result": game.get("result"),
        "termination": game.get("termination"),
        "outcome": game.get("outcome"),
        "legal_move_rate": game.get("legal_move_rate"),
        "mean_acpl": game.get("mean_acpl"),
        "start_fen": start_fen,
        "frames": frames,
    }


def routing_distribution(games: list) -> dict:
    """{worker_id: {"n_plies": int, "n_legal": int}} across every attempted
    ply in this condition's games -- counts every ply the router actually
    dispatched to a worker, including illegal ones (the routing choice was
    still made and is still the signal this chart is meant to show), not
    just the legal subset aggregate_condition()'s own metrics report on."""
    dist: dict = {}
    for g in games:
        for ply in g.get("plies", []):
            wid = ply.get("worker_id")
            if wid is None:
                continue
            entry = dist.setdefault(wid, {"n_plies": 0, "n_legal": 0})
            entry["n_plies"] += 1
            if ply.get("legal"):
                entry["n_legal"] += 1
    return dist


def build_demo_data(games_by_condition: dict) -> dict:
    """games_by_condition: {condition_id: [game dict, ...]} (m7's
    reports/minichess_demo/<condition>.json, already-parsed) -> the full
    JSON payload the rendered HTML embeds and drives its UI from."""
    conditions = {}
    for cid in CONDITION_ORDER:
        games = games_by_condition.get(cid, [])
        conditions[cid] = {
            "id": cid,
            "label": CONDITION_LABELS[cid],
            "short_label": CONDITION_SHORT_LABELS[cid],
            "metrics": aggregate_condition(games),
            "routing_distribution": routing_distribution(games),
            "games": [build_game_timeline(g) for g in games],
        }
    return {"conditions": conditions}


# --- HTML rendering ---------------------------------------------------------
# Self-contained: no external CSS/JS/font/network dependency, so the file
# opens correctly straight off disk and is ready to paste into the Artifact
# tool as-is if a future session wants to publish it that way.

def render_html(demo_data: dict, generated_at: Optional[str] = None) -> str:
    data_json = json.dumps(demo_data)
    # JSON can legally contain "</script>" inside a string (e.g. a raw LLM
    # reply describing HTML) -- escape the one sequence that would otherwise
    # prematurely close the embedded <script> tag when the browser parses it.
    data_json_safe = data_json.replace("</", "<\\/")
    generated_line = f"Generated {generated_at}" if generated_at else ""
    return _HTML_TEMPLATE.replace("__DEMO_DATA_JSON__", data_json_safe).replace(
        "__GENERATED_LINE__", generated_line
    )


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open-Fugu -- Gardner Minichess coordination demo</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --border: #ddd8d0; --text: #1f1c18;
  --muted: #6b6459; --accent: #8b5cf6; --accent-2: #0891b2;
  --win: #16a34a; --loss: #dc2626; --draw: #d97706; --unresolved: #6b6459;
  --sq-light: #f0e6d2; --sq-dark: #b58863; --sq-border: #00000022;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17151b; --panel: #211e27; --border: #37323f; --text: #eeeaf3;
    --muted: #9c94ab; --accent: #a78bfa; --accent-2: #22d3ee;
    --win: #4ade80; --loss: #f87171; --draw: #fbbf24; --unresolved: #9c94ab;
    --sq-light: #4b4458; --sq-dark: #2c2836; --sq-border: #00000055;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
h1 { font-size: 1.4rem; margin: 0 0 2px; }
h2 { font-size: 1rem; margin: 0 0 10px; color: var(--muted); font-weight: 600; }
.subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px; margin-bottom: 16px;
}
.tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.tab, .mode-btn {
  background: var(--panel); border: 1px solid var(--border); color: var(--text);
  padding: 8px 14px; border-radius: 999px; cursor: pointer; font-size: 0.85rem;
  font-weight: 600;
}
.tab.active, .mode-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
.stat-tile { background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.stat-tile .v { font-size: 1.3rem; font-weight: 700; }
.stat-tile .l { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
.bar-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 0.82rem; }
.bar-label { width: 150px; flex-shrink: 0; color: var(--muted); }
.bar-track { flex: 1; background: var(--bg); border-radius: 6px; height: 14px; overflow: hidden; border: 1px solid var(--border); }
.bar-fill { height: 100%; background: var(--accent-2); }
.bar-fill.illegal { background: var(--loss); }
.bar-count { width: 90px; text-align: right; color: var(--muted); flex-shrink: 0; }
.game-list { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.game-chip {
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; cursor: pointer;
  font-size: 0.8rem; background: var(--bg);
}
.game-chip.active { border-color: var(--accent); background: var(--accent); color: #fff; }
.outcome-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
.outcome-win { background: var(--win); } .outcome-loss { background: var(--loss); }
.outcome-draw { background: var(--draw); } .outcome-unresolved { background: var(--unresolved); }
.board-area { display: flex; gap: 24px; flex-wrap: wrap; }
.board-wrap { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.board {
  display: grid; grid-template-columns: repeat(5, 46px); grid-template-rows: repeat(5, 46px);
  border: 2px solid var(--border); border-radius: 4px; overflow: hidden;
}
.board.small { grid-template-columns: repeat(5, 28px); grid-template-rows: repeat(5, 28px); }
.sq { display: flex; align-items: center; justify-content: center; font-size: 30px; user-select: none; border: 1px solid var(--sq-border); }
.board.small .sq { font-size: 18px; }
.sq.light { background: var(--sq-light); } .sq.dark { background: var(--sq-dark); }
.controls { display: flex; align-items: center; gap: 8px; }
.controls button {
  background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 0.9rem;
}
.controls input[type=range] { flex: 1; min-width: 140px; }
.ply-info { flex: 1; min-width: 280px; font-size: 0.85rem; }
.ply-info .row { margin: 4px 0; }
.ply-info .k { color: var(--muted); display: inline-block; width: 110px; }
.raw-reply {
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.72rem;
  white-space: pre-wrap; max-height: 220px; overflow-y: auto; margin-top: 6px;
}
.side-by-side { display: flex; gap: 20px; flex-wrap: wrap; }
.badge { display: inline-block; border-radius: 6px; padding: 1px 7px; font-size: 0.72rem; font-weight: 600; }
.badge.legal { background: var(--win); color: #06240f; }
.badge.illegal { background: var(--loss); color: #2a0505; }
.badge.blunder { background: var(--draw); color: #2a1a02; margin-left: 4px; }
.empty-note { color: var(--muted); font-size: 0.85rem; padding: 20px; text-align: center; }
</style>
</head>
<body>
<h1>Open-Fugu -- Gardner Minichess coordination demo</h1>
<div class="subtitle">
  Evolution of the Fugu orchestrator's routing policy across three
  coordination checkpoints, on real blindfold 5x5 chess games against
  Fairy-Stockfish. __GENERATED_LINE__
</div>

<div class="tabs" id="mode-tabs">
  <button class="mode-btn active" data-mode="single">Per-checkpoint view</button>
  <button class="mode-btn" data-mode="compare">Side-by-side (same opening)</button>
</div>

<div id="single-view"></div>
<div id="compare-view" style="display:none"></div>

<script id="fugu-demo-data" type="application/json">__DEMO_DATA_JSON__</script>
<script>
(function () {
  var DATA = JSON.parse(document.getElementById("fugu-demo-data").textContent);
  var CONDITION_ORDER = Object.keys(DATA.conditions);
  var PIECE_GLYPHS = {
    K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘", P: "♙",
    k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟"
  };
  var FILES = ["a", "b", "c", "d", "e"];

  function fenToGrid(fen) {
    // pyffish FEN: 5 ranks separated by '/', rank5 (Black's back rank) first,
    // rank1 (White's back rank) last -- same convention as standard FEN, just
    // 5 files/ranks instead of 8. grid[0] is rank5 (top row), grid[4] is rank1.
    var placement = (fen || "").split(" ")[0] || "";
    var rows = placement.split("/");
    var grid = [];
    for (var r = 0; r < rows.length; r++) {
      var row = [];
      var chars = rows[r].split("");
      for (var i = 0; i < chars.length; i++) {
        var c = chars[i];
        if (/[1-9]/.test(c)) {
          for (var e = 0; e < parseInt(c, 10); e++) row.push(null);
        } else {
          row.push(c);
        }
      }
      grid.push(row);
    }
    return grid;
  }

  function renderBoard(container, fen, opts) {
    opts = opts || {};
    container.innerHTML = "";
    container.className = "board" + (opts.small ? " small" : "");
    var grid = fenToGrid(fen);
    for (var r = 0; r < grid.length; r++) {
      for (var c = 0; c < (grid[r] || []).length; c++) {
        var sq = document.createElement("div");
        var isLight = (r + c) % 2 === 0;
        sq.className = "sq " + (isLight ? "light" : "dark");
        var piece = grid[r][c];
        if (piece && PIECE_GLYPHS[piece]) sq.textContent = PIECE_GLYPHS[piece];
        container.appendChild(sq);
      }
    }
  }

  function outcomeClass(outcome) {
    return "outcome-" + (outcome || "unresolved");
  }

  function fmtPct(v) { return v === null || v === undefined ? "n/a" : (v * 100).toFixed(0) + "%"; }
  function fmtNum(v, d) {
    if (v === null || v === undefined) return "n/a";
    return (typeof d === "number") ? v.toFixed(d) : String(v);
  }

  // ---- Per-checkpoint (single condition) view -----------------------------

  var singleView = document.getElementById("single-view");
  var activeCondition = CONDITION_ORDER[0];
  var activeGameIdx = 0;
  var activeFrame = 0; // 0 = start_fen (before any ply)
  var playTimer = null;

  function conditionData() { return DATA.conditions[activeCondition]; }
  function currentGame() {
    var games = conditionData().games;
    return games.length ? games[Math.min(activeGameIdx, games.length - 1)] : null;
  }

  function buildSingleView() {
    singleView.innerHTML = "";

    var tabs = document.createElement("div");
    tabs.className = "tabs";
    CONDITION_ORDER.forEach(function (cid) {
      var b = document.createElement("button");
      b.className = "tab" + (cid === activeCondition ? " active" : "");
      b.textContent = DATA.conditions[cid].short_label;
      b.onclick = function () {
        activeCondition = cid; activeGameIdx = 0; activeFrame = 0; stopPlay();
        buildSingleView();
      };
      tabs.appendChild(b);
    });
    singleView.appendChild(tabs);

    var cond = conditionData();

    var statsPanel = document.createElement("div");
    statsPanel.className = "panel";
    var h2 = document.createElement("h2");
    h2.textContent = cond.label + " -- aggregate metrics (" + cond.metrics.n_games + " games)";
    statsPanel.appendChild(h2);
    var grid = document.createElement("div");
    grid.className = "stat-grid";
    var tiles = [
      ["Win rate", fmtPct(cond.metrics.win_rate)],
      ["Draw rate", fmtPct(cond.metrics.draw_rate)],
      ["Loss rate", fmtPct(cond.metrics.loss_rate)],
      ["Unresolved", fmtPct(cond.metrics.unresolved_rate)],
      ["Legal-move rate", fmtPct(cond.metrics.mean_legal_move_rate)],
      ["Illegal-move rate", fmtPct(cond.metrics.illegal_move_rate)],
      ["Mean ACPL", fmtNum(cond.metrics.mean_acpl, 0)],
      ["Blunder rate", fmtPct(cond.metrics.mean_blunder_rate)]
    ];
    tiles.forEach(function (t) {
      var tile = document.createElement("div");
      tile.className = "stat-tile";
      tile.innerHTML = '<div class="v">' + t[1] + '</div><div class="l">' + t[0] + '</div>';
      grid.appendChild(tile);
    });
    statsPanel.appendChild(grid);
    singleView.appendChild(statsPanel);

    var routingPanel = document.createElement("div");
    routingPanel.className = "panel";
    var h2b = document.createElement("h2");
    h2b.textContent = "Routing distribution";
    routingPanel.appendChild(h2b);
    var dist = cond.routing_distribution;
    var totalPlies = Object.keys(dist).reduce(function (s, k) { return s + dist[k].n_plies; }, 0) || 1;
    Object.keys(dist).sort().forEach(function (wid) {
      var d = dist[wid];
      var row = document.createElement("div");
      row.className = "bar-row";
      var pct = (d.n_plies / totalPlies) * 100;
      row.innerHTML =
        '<div class="bar-label">' + wid + '</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + pct.toFixed(1) + '%"></div></div>' +
        '<div class="bar-count">' + d.n_plies + ' plies (' + d.n_legal + ' legal)</div>';
      routingPanel.appendChild(row);
    });
    if (Object.keys(dist).length === 0) {
      var note = document.createElement("div");
      note.className = "empty-note";
      note.textContent = "No routed plies recorded for this condition yet.";
      routingPanel.appendChild(note);
    }
    singleView.appendChild(routingPanel);

    var gamesPanel = document.createElement("div");
    gamesPanel.className = "panel";
    var h2c = document.createElement("h2");
    h2c.textContent = "Games (one per fixed opening)";
    gamesPanel.appendChild(h2c);

    if (!cond.games.length) {
      var empty = document.createElement("div");
      empty.className = "empty-note";
      empty.textContent = "No games recorded for this condition yet -- m7 hasn't produced this file, or this checkpoint's upstream training phase hasn't completed.";
      gamesPanel.appendChild(empty);
      singleView.appendChild(gamesPanel);
      return;
    }

    var chipRow = document.createElement("div");
    chipRow.className = "game-list";
    cond.games.forEach(function (g, idx) {
      var chip = document.createElement("div");
      chip.className = "game-chip" + (idx === activeGameIdx ? " active" : "");
      chip.innerHTML = '<span class="outcome-dot ' + outcomeClass(g.outcome) + '"></span>' +
        g.opening + " (" + g.llm_color + ")";
      chip.onclick = function () { activeGameIdx = idx; activeFrame = 0; stopPlay(); buildSingleView(); };
      chipRow.appendChild(chip);
    });
    gamesPanel.appendChild(chipRow);

    var boardArea = document.createElement("div");
    boardArea.className = "board-area";

    var boardWrap = document.createElement("div");
    boardWrap.className = "board-wrap";
    var boardEl = document.createElement("div");
    boardWrap.appendChild(boardEl);

    var controls = document.createElement("div");
    controls.className = "controls";
    controls.style.width = "100%";
    var prevBtn = document.createElement("button"); prevBtn.textContent = "◀";
    var playBtn = document.createElement("button"); playBtn.textContent = "▶ Play";
    var nextBtn = document.createElement("button"); nextBtn.textContent = "▶";
    var slider = document.createElement("input");
    slider.type = "range"; slider.min = 0;
    var frameLabel = document.createElement("span");
    frameLabel.style.fontSize = "0.75rem"; frameLabel.style.color = "var(--muted)";

    var game = currentGame();
    var maxFrame = game ? game.frames.length : 0;
    slider.max = maxFrame;
    slider.value = activeFrame;

    function renderFrame() {
      game = currentGame();
      if (!game) return;
      var fen = activeFrame === 0 ? game.start_fen : game.frames[activeFrame - 1].fen;
      renderBoard(boardEl, fen, {});
      slider.value = activeFrame;
      frameLabel.textContent = activeFrame + " / " + game.frames.length;
      renderPlyInfo();
    }

    prevBtn.onclick = function () { stopPlay(); activeFrame = Math.max(0, activeFrame - 1); renderFrame(); };
    nextBtn.onclick = function () { stopPlay(); activeFrame = Math.min(maxFrame, activeFrame + 1); renderFrame(); };
    slider.oninput = function () { stopPlay(); activeFrame = parseInt(slider.value, 10); renderFrame(); };
    playBtn.onclick = function () {
      if (playTimer) { stopPlay(); return; }
      playBtn.textContent = "⏸ Pause";
      playTimer = setInterval(function () {
        if (activeFrame >= maxFrame) { stopPlay(); return; }
        activeFrame += 1; renderFrame();
      }, 900);
    };

    controls.appendChild(prevBtn);
    controls.appendChild(playBtn);
    controls.appendChild(nextBtn);
    controls.appendChild(slider);
    controls.appendChild(frameLabel);
    boardWrap.appendChild(controls);
    boardArea.appendChild(boardWrap);

    var plyInfo = document.createElement("div");
    plyInfo.className = "ply-info";
    boardArea.appendChild(plyInfo);

    function renderPlyInfo() {
      plyInfo.innerHTML = "";
      if (!game) return;
      if (activeFrame === 0) {
        plyInfo.innerHTML =
          '<div class="row"><span class="k">Opening</span>' + game.opening + '</div>' +
          '<div class="row"><span class="k">LLM plays</span>' + game.llm_color + '</div>' +
          '<div class="row"><span class="k">Book moves</span>' + game.opening_uci_moves.join(" ") + '</div>';
        return;
      }
      var f = game.frames[activeFrame - 1];
      var badge = f.legal
        ? '<span class="badge legal">legal</span>'
        : '<span class="badge illegal">illegal -- game over</span>';
      if (f.is_blunder) badge += '<span class="badge blunder">blunder</span>';
      plyInfo.innerHTML =
        '<div class="row"><span class="k">Ply</span>' + f.ply + ' (' + f.mover + ')</div>' +
        '<div class="row"><span class="k">Routed to</span>' + (f.worker_id || "n/a") + '</div>' +
        '<div class="row"><span class="k">Move</span>' + (f.move_uci || "(none extracted)") + ' ' + badge + '</div>' +
        '<div class="row"><span class="k">Centipawn loss</span>' + fmtNum(f.centipawn_loss) + '</div>';
      if (activeFrame === maxFrame) {
        plyInfo.innerHTML += '<div class="row"><span class="k">Result</span>' + game.result +
          ' (' + game.termination + ')</div>';
      }
      if (f.raw_reply) {
        var pre = document.createElement("div");
        pre.className = "raw-reply";
        pre.textContent = f.raw_reply + (f.raw_reply_truncated ? "\n\n[truncated for demo display]" : "");
        plyInfo.appendChild(pre);
      }
    }

    gamesPanel.appendChild(boardArea);
    singleView.appendChild(gamesPanel);
    renderFrame();
  }

  function stopPlay() {
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
    var pb = singleView.querySelector(".controls button:nth-child(2)");
    if (pb) pb.textContent = "▶ Play";
  }

  // ---- Side-by-side (same opening, all 3 conditions) view -----------------

  var compareView = document.getElementById("compare-view");
  var compareOpeningIdx = 0;
  var compareFrame = 0;
  var comparePlayTimer = null;

  function allOpenings() {
    var names = [];
    CONDITION_ORDER.forEach(function (cid) {
      DATA.conditions[cid].games.forEach(function (g) {
        if (names.indexOf(g.opening) === -1) names.push(g.opening);
      });
    });
    return names;
  }

  function gameForOpening(cid, opening) {
    var games = DATA.conditions[cid].games;
    for (var i = 0; i < games.length; i++) if (games[i].opening === opening) return games[i];
    return null;
  }

  function buildCompareView() {
    compareView.innerHTML = "";
    var openings = allOpenings();
    if (!openings.length) {
      compareView.innerHTML = '<div class="panel"><div class="empty-note">No games recorded yet in any condition.</div></div>';
      return;
    }
    if (compareOpeningIdx >= openings.length) compareOpeningIdx = 0;
    var opening = openings[compareOpeningIdx];

    var panel = document.createElement("div");
    panel.className = "panel";
    var h2 = document.createElement("h2");
    h2.textContent = "Same opening across all 3 checkpoints: " + opening;
    panel.appendChild(h2);

    var chipRow = document.createElement("div");
    chipRow.className = "game-list";
    openings.forEach(function (o, idx) {
      var chip = document.createElement("div");
      chip.className = "game-chip" + (idx === compareOpeningIdx ? " active" : "");
      chip.textContent = o;
      chip.onclick = function () { compareOpeningIdx = idx; compareFrame = 0; stopComparePlay(); buildCompareView(); };
      chipRow.appendChild(chip);
    });
    panel.appendChild(chipRow);

    var maxFrame = 0;
    CONDITION_ORDER.forEach(function (cid) {
      var g = gameForOpening(cid, opening);
      if (g) maxFrame = Math.max(maxFrame, g.frames.length);
    });

    var controls = document.createElement("div");
    controls.className = "controls";
    var prevBtn = document.createElement("button"); prevBtn.textContent = "◀";
    var playBtn = document.createElement("button"); playBtn.textContent = "▶ Play";
    var nextBtn = document.createElement("button"); nextBtn.textContent = "▶";
    var slider = document.createElement("input");
    slider.type = "range"; slider.min = 0; slider.max = maxFrame; slider.value = compareFrame;
    var frameLabel = document.createElement("span");
    frameLabel.style.fontSize = "0.75rem"; frameLabel.style.color = "var(--muted)";

    var sideBySide = document.createElement("div");
    sideBySide.className = "side-by-side";

    function renderCompareFrame() {
      sideBySide.innerHTML = "";
      CONDITION_ORDER.forEach(function (cid) {
        var g = gameForOpening(cid, opening);
        var wrap = document.createElement("div");
        wrap.className = "board-wrap";
        var label = document.createElement("div");
        label.style.fontSize = "0.78rem"; label.style.fontWeight = "600";
        label.textContent = DATA.conditions[cid].short_label;
        wrap.appendChild(label);
        var boardEl = document.createElement("div");
        wrap.appendChild(boardEl);
        var meta = document.createElement("div");
        meta.style.fontSize = "0.72rem"; meta.style.color = "var(--muted)";
        if (!g) {
          renderBoard(boardEl, null, { small: true });
          meta.textContent = "no data";
        } else {
          var f = compareFrame === 0 ? null : g.frames[Math.min(compareFrame, g.frames.length) - 1];
          var fen = compareFrame === 0 ? g.start_fen : (g.frames[Math.min(compareFrame - 1, g.frames.length - 1)] || {}).fen || g.start_fen;
          renderBoard(boardEl, fen, { small: true });
          if (f) {
            meta.textContent = "ply " + f.ply + ": " + f.worker_id + " " + (f.move_uci || "?") +
              (f.legal ? "" : " (illegal)");
          } else {
            meta.textContent = "opening";
          }
        }
        wrap.appendChild(meta);
        sideBySide.appendChild(wrap);
      });
      slider.value = compareFrame;
      frameLabel.textContent = compareFrame + " / " + maxFrame;
    }

    prevBtn.onclick = function () { stopComparePlay(); compareFrame = Math.max(0, compareFrame - 1); renderCompareFrame(); };
    nextBtn.onclick = function () { stopComparePlay(); compareFrame = Math.min(maxFrame, compareFrame + 1); renderCompareFrame(); };
    slider.oninput = function () { stopComparePlay(); compareFrame = parseInt(slider.value, 10); renderCompareFrame(); };
    playBtn.onclick = function () {
      if (comparePlayTimer) { stopComparePlay(); return; }
      playBtn.textContent = "⏸ Pause";
      comparePlayTimer = setInterval(function () {
        if (compareFrame >= maxFrame) { stopComparePlay(); return; }
        compareFrame += 1; renderCompareFrame();
      }, 900);
    };

    controls.appendChild(prevBtn);
    controls.appendChild(playBtn);
    controls.appendChild(nextBtn);
    controls.appendChild(slider);
    controls.appendChild(frameLabel);

    panel.appendChild(sideBySide);
    panel.appendChild(controls);
    compareView.appendChild(panel);
    renderCompareFrame();
  }

  function stopComparePlay() {
    if (comparePlayTimer) { clearInterval(comparePlayTimer); comparePlayTimer = null; }
    var pb = compareView.querySelector(".controls button:nth-child(2)");
    if (pb) pb.textContent = "▶ Play";
  }

  // ---- Mode switch ---------------------------------------------------------

  document.querySelectorAll("#mode-tabs .mode-btn").forEach(function (btn) {
    btn.onclick = function () {
      document.querySelectorAll("#mode-tabs .mode-btn").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      var mode = btn.getAttribute("data-mode");
      stopPlay(); stopComparePlay();
      document.getElementById("single-view").style.display = mode === "single" ? "" : "none";
      document.getElementById("compare-view").style.display = mode === "compare" ? "" : "none";
      if (mode === "compare") buildCompareView();
    };
  });

  buildSingleView();
})();
</script>
</body>
</html>
"""

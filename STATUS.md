# Open-Fugu — Status (living document)

Last updated: 2026-07-07, on the original host, mid-Phase-0/0.5, right before migrating
to a new SSH host (another user needed the original machine and machine-sharing was
ruled out). See `PLAN.md` for the full approved plan this implements.

## What's actually done (verified working on the ORIGINAL host)

- [x] Project scaffold at (originally) `/Data/alfred.ruscher/open_fugu`, `chmod 700`
      throughout, symlinked from `~/open_fugu` for convenience.
- [x] `scripts/disk_guard.py` — tracks project dir size + incremental HF-cache growth
      against a 100GB cap (80GB warn). Baseline HF-cache manifest was written
      (`config/hf_cache_manifest_baseline.json`, **gitignored, host-specific, must be
      regenerated on the new host** via `write_baseline_manifest()` BEFORE any new model
      downloads there).
- [x] Dependencies installed into the project venv: `bitsandbytes`, `python-chess`
      (→ `chess` 1.11.2), `cma` 4.4.4, `fastapi`, `uvicorn`, `httpx`, `pyyaml`, `pandas`,
      `scipy`, `ag2` 0.14.0 (autogen). Verified NO downgrade of the pre-existing
      torch 2.11.0+cu128 / transformers 5.8.1 / peft 0.19.1 / trl 0.29.1 stack.
- [x] Stockfish 18 (avx2 build) downloaded and working via `bin/stockfish-wrapper.sh`
      (LD_LIBRARY_PATH workaround for old system libstdc++ — **host-specific, re-verify
      on new host**, see PLAN.md). Confirmed working end-to-end with `python-chess`
      (`chess.engine.SimpleEngine.popen_uci`).
- [x] `vendor/llm_chess` — fresh clone of `maxim-saplin/llm_chess` upstream (gitignored;
      re-clone with `git clone https://github.com/maxim-saplin/llm_chess.git vendor/llm_chess`
      on the new host).
- [x] `src/open_fugu/reward/stockfish_scorer.py` — `StockfishScorer` class: per-move
      centipawn-loss scoring (`score_move`), best-move query (`best_move`), blunder/
      mistake thresholds (300/100cp). Reusable across floor-check, SFT data collection,
      and final eval.
- [x] `src/open_fugu/chess_blindfold/harness.py` — **custom-built** blindfold chess game
      loop implementing the Fugu paper's exact Appendix B.2 / Listing 1 protocol (fixed
      opening given once, then only the opponent's last UCI move each turn, no board/FEN/
      legal-move-list ever shown). Deliberately NOT built on top of vendored llm_chess's
      `AutoGen`/`ConversableAgent` machinery — see "Design decision" below for why.
- [x] `src/open_fugu/models/local_worker.py` — `LocalWorker` class loading any HF chat
      model via transformers + bitsandbytes NF4 4-bit, `.generate(messages)` taking a
      plain OpenAI-style message list. `CANDIDATE_WORKERS` dict maps short ids to HF repo
      ids for the whole planned pool (mid + small tier).
- [x] `scripts/phase0_5_blindfold_floor_check.py` — written but **NOT YET RUN** (session
      interrupted by the host migration). Plays N short blindfold games per candidate
      worker vs. skill-limited local Stockfish, reports legal-move rate / ACPL / blunder
      rate per worker to `logs/phase0_5_floor_check/<worker>.json`.
- [x] Git repo initialized locally, 2 commits, pushed to
      `https://github.com/Warsea12-ai/fugu.git` (private) once SSH auth was set up.

## Design decision worth knowing: why NOT to reuse llm_chess's AutoGen agents

`vendor/llm_chess`'s default game loop (`llm_chess.py` + `custom_agents.py`) uses an
action-DSL: the LLM is told it can call `get_current_board`, `get_legal_moves`, or
`make_move <uci>`, mediated by an `AutoReplyAgent` proxy. This is **NOT blindfold** —
those actions exist specifically so models CAN see the board, which is the opposite of
what Fugu's Appendix B.2 tests. Also, `proxy_agent.clear_history()` is called after every
move, so if a model wants to know what its opponent played, its *only* option in that
framework is to call `get_current_board` again — there's no lightweight "just tell me the
last move" path.

Reproducing the paper's actual protocol (single continuous session, only the opponent's
raw UCI move ever injected, model must track state from conversational memory alone)
was simpler to build fresh (`chess_blindfold/harness.py`, ~150 lines) than to bend
AutoGen's action-parsing to do something it wasn't designed for. The harness's `move_fn`
is a plain callable `(messages: list[dict]) -> str`, so it works identically whether the
move source is a local `LocalWorker`, a future router HTTP call, or (for testing) a
hardcoded stub.

**Reusable find in `custom_agents.py`**: `NonGameAgent` already implements a
network-of-networks pattern (query N LLMs, synthesize with one more) — this is a
ready-made **majority-vote/ensemble baseline** for Phase 5/6 evaluation, if the
non-blindfold interaction style is ever wanted for a non-blindfold comparison arm.

## Exact next steps for a fresh session on the new host

1. **Re-verify the ground truth section of PLAN.md on the new machine** — GPU, disk
   quotas (check home-dir quota FIRST, this bit us once already), HF cache location/
   contents, venv setup, Stockfish libstdc++ compatibility. Do not assume any of it
   carries over just because the new host is "the same power."
2. `git clone` this repo (`https://github.com/Warsea12-ai/fugu.git`) onto the new host,
   `chmod 700` everything immediately after clone.
3. Recreate the venv and reinstall dependencies (see PLAN.md's package list). Re-run
   `scripts/disk_guard.py`'s `write_baseline_manifest()` BEFORE downloading/using any
   models, so the 100GB cap tracks only this project's new usage on the new host.
4. Re-clone `vendor/llm_chess` (gitignored, see command above).
5. Re-download or locate Stockfish 18 for the new host's CPU, wire up
   `bin/stockfish-wrapper.sh` (may not need the libstdc++ workaround at all on a newer
   distro — try the bare binary first).
6. Verify at least 2-3 candidate worker models load via `LocalWorker` (either already
   cached on the new host, or fresh-downloaded — check disk budget first for the latter).
7. **Run `scripts/phase0_5_blindfold_floor_check.py`** — this was the very next planned
   action before the migration. It's fully written and should be ready to run as-is
   (paths are all relative to the project dir via `Path(__file__).resolve()`).
8. Still pending from the original plan, not yet started: `state.json` +
   `scripts/orchestrate.py` (phase-resumability driver) and the user-crontab install
   for autonomous progression independent of any SSH session staying connected.

## Constraints to keep honoring on the new host

- One machine at a time (no simultaneous execution across hosts).
- `chmod 700` every created directory, `600` every created file, `umask 077` before any
  bulk creation.
- 100GB disk cap via `disk_guard.py`, warn at 80GB, refuse + ask at 100GB.
- Long GPU jobs go in `tmux -d` sessions; the phase-advancement loop (once built) runs
  via plain host `cron`, not via any agent-session-dependent scheduling mechanism.

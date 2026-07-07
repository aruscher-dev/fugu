# Open-Fugu — Status (living document)

Last updated: 2026-07-07, on the new host (`lotte.polytechnique.fr`), Phase 0 complete,
Phase 0.5 running autonomously. See `PLAN.md` for the full approved plan this implements.

## What's actually done (verified working on THIS host, lotte.polytechnique.fr)

- [x] Repo cloned to `/Data/alfred.ruscher/fugu`, `chmod 700` throughout (dirs 700,
      files 600). Home-dir NFS quota re-checked: same 30GB hard cap, ~28GB already used
      (shared across hosts via `alpha.polytechnique.fr:/students`) — confirms project must
      stay on `/Data` (1TB local disk, 713GB free at time of writing), never `$HOME`.
- [x] Venv at `/Data/.venv` (shared base image: torch 2.11.0+cu128, transformers 5.8.1,
      peft 0.19.1, trl 0.29.1 already present, exact same versions as the original host).
      Installed this project's extra deps: `bitsandbytes` 0.49.2, `chess` 1.11.2 (via
      `python-chess`), `cma` 4.4.4, `ag2` 0.14.0 (autogen) — `fastapi`/`uvicorn`/`httpx`/
      `pyyaml`/`pandas`/`scipy` were already present. No downgrade of the base ML stack.
- [x] `scripts/disk_guard.py` baseline manifest regenerated for this host's
      `/Data/.hf_cache` (only 4 unrelated video-model dirs, 6.4GB — none of the chess
      worker LLMs are pre-cached here, unlike the original host).
- [x] `vendor/llm_chess` re-cloned fresh from `maxim-saplin/llm_chess`.
- [x] Stockfish 18 — this host has AVX-512 (better than the original host's AVX2-only),
      used the `stockfish-ubuntu-x86-64-avx512` build. Same `GLIBCXX_3.4.30' not found`
      issue as the original host; same fix works (`/usr/local/gcc-15.1.0/lib64` also
      exists here — shared cluster software). `bin/stockfish-wrapper.sh` updated
      accordingly and reverified end-to-end with `python-chess`.
- [x] **`state.json` + `scripts/orchestrate.py` + `scripts/status.py` +
      `scripts/install_crontab.sh` — the autonomous-resumption mechanism, built this
      session.** `orchestrate.py` reads `state.json`, advances the first non-done phase
      by one idempotent step, launches long GPU work into a detached `tmux` session, and
      exits (never blocks). A plain user crontab entry (`*/15 * * * *`, no root) runs it
      unattended — installed and confirmed active via `crontab -l`. This is what makes
      progress survive SSH/laptop disconnects: cron and tmux are both host daemons,
      independent of any agent/Claude Code session.
- [x] Phase 0.5 floor check **launched** (tmux session `openfugu-phase0_5`, log at
      `logs/phase0_5_floor_check.log`) — downloading + testing `qwen2.5-7b`, `mistral-7b`,
      `deepseek-r1-distill-qwen-7b` (none pre-cached on this host, ~15GB download each).
      Cron will detect completion and mark phase 0.5 `done` in `state.json` automatically;
      or run `scripts/status.py` any time for a manual check.

## IMPORTANT limitation of the autonomous mechanism — read this before assuming too much

`cron` + `tmux` keep **already-written, already-launched** work running/retrying without
any agent session. They **cannot write new code**. Phases with no script yet (1, 2, 3+ —
see `state.json`) will make `orchestrate.py` log "no script yet" and exit cleanly, idling
harmlessly, until a human/agent development session writes that phase's script. So:
- Long GPU jobs (like the current Phase 0.5 download+eval): survive disconnects AND
  Claude-Code-session/token gaps, no action needed.
- Building Phase 1 onward (router server, SFT data collection, SVF training, eval): needs
  an active Claude Code session again — there's no way around that with the constraint of
  staying on this one host and not using any Anthropic-managed remote scheduling mechanism
  (which was explicitly rejected earlier as violating "one host at a time").

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

## Exact next steps for the next session (on this host, or handoff to a new one)

1. Check `scripts/status.py` output first — is Phase 0.5 done? If yes, read the gate
   verdict (`mean_legal_move_rate` per worker in `logs/phase0_5_floor_check/*.json`) before
   deciding whether Phase 3+ is worth the GPU-hours.
2. If Phase 0.5 passed the floor: write `src/open_fugu/models/worker_backend.py` (LRU
   swap manager for mid-tier workers) and `src/open_fugu/models/router_server.py`
   (FastAPI, OpenAI-compatible `/v1/chat/completions`, random-routing dummy orchestrator
   first) — this is Phase 1, register `advance_phase_1` in `orchestrate.py` once written.
3. If migrating hosts again: re-run steps 1-7 from the previous version of this doc
   (still accurate), `git pull`, and re-run `scripts/install_crontab.sh` on the new host.

## Constraints to keep honoring

- One machine at a time (no simultaneous execution across hosts).
- `chmod 700` every created directory, `600` every created file, `umask 077` before any
  bulk creation.
- 100GB disk cap via `disk_guard.py`, warn at 80GB, refuse + ask at 100GB.
- Long GPU jobs go in `tmux -d` sessions; phase-advancement runs via plain host `cron`
  (installed, `*/15 * * * *`), not via any agent-session-dependent scheduling mechanism.

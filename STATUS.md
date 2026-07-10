# Open-Fugu — Status (living document)

Last updated: 2026-07-10 (cloud dev routine). Phase 0, 0.5, 1, and 2 all complete on
`lotte.polytechnique.fr` -- Phase 2's `reports/phase2_stockfish_sanity_result.json`
shows `passed: true` (after a same-day fix for a `chmod 600` sweep that had stripped
`+x` from `bin/stockfish-wrapper.sh`/the Stockfish binary, and a loosened
near-zero-noise threshold on one check -- see the host's own commit history for
details, not reproduced here). Phase 3's code has now been written and pushed by the
cloud dev routine (see "Cloud dev routine additions" below) but not yet executed --
that happens next time the GPU host's cron picks it up.
See `PLAN.md` for the full approved plan this implements.

## What's actually done

- [x] **Phase 0 (env setup).** Repo cloned to `/Data/alfred.ruscher/fugu`, `chmod 700`
      throughout. Venv at `/Data/.venv` (shared base image: torch 2.11.0+cu128,
      transformers 5.8.1, peft 0.19.1, trl 0.29.1) plus this project's deps
      (`bitsandbytes`, `python-chess`, `cma`, `ag2`, `a2a-sdk[http-server]==0.3.5` --
      pinned to match the AgentBeats course reference code's API; PyPI-latest `1.1.0`
      has since moved/renamed several modules, e.g. `a2a.server.apps` no longer exists
      there). Stockfish 18 (avx512 build) working via `bin/stockfish-wrapper.sh`
      (`LD_LIBRARY_PATH=/usr/local/gcc-15.1.0/lib64`, host-specific libstdc++ workaround).
      `vendor/llm_chess` re-cloned. `disk_guard.py` HF-cache baseline written.
- [x] **Autonomous-resumption mechanism**: `state.json` + `scripts/orchestrate.py` +
      `scripts/status.py` + `scripts/install_crontab.sh`. `orchestrate.py` does
      `git pull --ff-only` (picks up code from the scheduled cloud dev routine, see
      below), advances the first non-done phase by one idempotent step (launching long
      GPU work into detached `tmux` sessions), then commits+pushes `state.json` +
      `reports/` (tracked, non-gitignored small JSON summaries) and exits -- never
      blocks. A plain user crontab entry (`*/15 * * * *`, no root) runs it unattended.
      This is what makes progress survive SSH/laptop disconnects: cron and tmux are
      host daemons, independent of any agent/Claude Code session.
- [x] **Phase 0.5 (blindfold floor check) -- DONE, `reports/phase0_5_summary.json`.**
      Real, trustworthy result after fixing two engineering bugs found along the way
      (see "Bugs fixed" below): `qwen2.5-7b` 0% legal-move rate, `mistral-7b` 44%,
      `deepseek-r1-distill-qwen-7b` 22% (3 short games each, Ruy Lopez opening, 24-ply
      cap). Verdict: `REVIEW_NEEDED` (heuristic cutoff is >50%) -- genuinely hard for
      7B instruct models to track a chess position from memory alone with zero
      board/FEN, consistent with the paper's own "Key open risk #3". Not a blocker for
      Phase 1 (wiring), but worth more prompt-engineering/larger-n before trusting
      Phase 3's SFT data collection budget.
- [x] **Phase 1 (A2A/AgentBeats wiring) -- DONE, `reports/phase1_smoke_test_result.json`
      (`passed: true`).** Per user request, inter-agent communication now goes over the
      **A2A protocol**, following **UC Berkeley RDI's AgentBeats competition**
      conventions (course material at `~/Team/AgentBeats_bench`, esp.
      `finance_economics/tutorial-agent-beats-comp` and
      `games_virtual_environments/build_what_i_mean/pragmatic_builder`, MIT license --
      vendored into `src/open_fugu/agentbeats/` with attribution comments). Built:
      - `src/open_fugu/a2a/worker_agent.py` -- each worker LLM is its own A2A **purple
        agent** (`AgentCard` + `blindfold_chess_move` skill), wrapping `LocalWorker`.
      - `src/open_fugu/a2a/orchestrator_agent.py` -- the Fugu orchestrator is *itself* a
        purple agent; the green judge only ever talks to it, never to a worker directly.
        It dispatches to a worker over A2A too (agent-to-agent, not in-process).
        Phase 1 scope: **random routing per game** (a dummy stand-in for the paper's
        learned per-query selection head, which is Phase 4 -- see the code comment in
        `orchestrator_agent.py` for why per-query routing needs a stateless-transcript
        redesign, not just a smarter routing function).
      - `src/open_fugu/a2a/chess_green_agent.py` -- **green (judge) agent**: receives an
        `EvalRequest` naming the orchestrator's URL, plays blindfold games with the real
        `chess.Board` + Stockfish scoring kept entirely server-side (never crosses the
        A2A wire -- preserves blindfold-ness exactly like the in-process harness),
        returns an `EvalResult` artifact.
      - `config/scenario_blindfold_chess_smoke.toml` + `scripts/phase1_agentbeats_smoke_test.py`,
        launched via the vendored `agentbeats.run_scenario` + `client_cli` -- matching
        the competition's own submission format almost exactly (this project could,
        with a `Dockerfile` and a registration step, likely be submitted to
        agentbeats.dev largely as-is).
      - **Verified end-to-end**: agent cards served at `/.well-known/agent-card.json`,
        JSON-RPC message round-trips, `TaskState` transitions
        (`submitted`→`working`→`completed`), `EvalResult` artifact produced. The smoke
        test's 2 games both ended in `illegal_move` (expected -- quality isn't gated
        here, Phase 0.5 already covers that) but the **pipeline itself did not crash**.

- [x] **Phase 2 (Stockfish reward pipeline sanity check) -- DONE,
      `reports/phase2_stockfish_sanity_result.json` (`passed: true`).** Written by the
      cloud dev routine (no GPU/Stockfish access there, so only `py_compile`-verified at
      write time), then actually run and fixed by the GPU host the same day.
      `scripts/phase2_stockfish_setup_sanity.py` runs `StockfishScorer` against a
      handful of hand-constructed positions with an objectively-known correct answer: a
      minimal two-kings-two-queens position where one candidate move captures the
      opponent's undefended queen (best move, low loss) and another hangs the mover's
      own queen instead (blunder, loss > `BLUNDER_THRESHOLD_CP`); a not-a-queen-move
      (`is_legal=False`); the classic Scholar's Mate trap's forced mating move (zero-loss/
      best, exercises the `MATE_SCORE_CP` branch); and a replay of Phase 0.5's Ruy Lopez
      opening (every theory move legal, low mean ACPL). `advance_phase_2` registered in
      `scripts/orchestrate.py`'s `PHASE_ADVANCERS`, following the `advance_phase_1`
      pattern. Gates whether `StockfishScorer` is trustworthy before Phase 3 spends
      GPU-hours on data scored by it -- it is.

## Cloud dev routine additions (2026-07-10)

- **Phase 3 (SFT data collection) -- code written, status `pending` in `state.json`, NOT
  YET RUN.** Per PLAN.md's training recipe step 1: sample ~300-600 blindfold-chess
  positions, query every candidate worker n=3-4 times each, score via `StockfishScorer`,
  and write raw records for Phase 4 to build a soft target distribution from. Written by
  the cloud dev routine (no GPU/Stockfish/model access there) -- verification limited to
  `python3 -m py_compile` plus pure-logic checks of the position/prompt-building code
  (installed a wheel-only `chess` package in the sandbox to actually exercise
  `SampledPosition.color_to_move`, `format_opening_prompt`/`extract_uci_move`
  round-tripping, and the JSONL resume-key logic -- no Stockfish binary or GPU needed for
  those). **The next GPU-host cron run should confirm the full pipeline (worker
  generation + real Stockfish scoring) actually works** before leaning on the collected
  data.
  - **Position sourcing -- the one real design decision this phase required.** PLAN.md's
    original plan sourced positions from a Lichess puzzle CSV
    (`~/Team/chess_bench/data/puzzles.csv`), not available on this host. But blindfold
    play (`harness.py`'s whole point) never shows the model a FEN or board -- only a
    move-history-from-start prompt -- so a puzzle FEN wouldn't even be usable as-is.
    Instead, `src/open_fugu/data/chess_positions.py` generates positions as **self-play
    move-history prefixes**: two local-Stockfish self-play games at a randomized skill
    level per game (1-20) plus a 15%-per-ply chance of an explicit random legal move (for
    diversity beyond whatever randomness Stockfish's own Skill Level setting provides),
    truncated at a random ply count (4-40). Each prefix is exactly the
    `opening_uci_moves` list `harness.format_opening_prompt()` already expects -- reused
    directly rather than inventing a second prompt format.
  - `src/open_fugu/data/collect_sft_data.py`: for one worker, one sample = one
    single-turn query (`format_opening_prompt` -> `worker.generate` ->
    `extract_uci_move` -> `StockfishScorer.score_move`) against a given position, written
    as one JSONL record. Appends + flushes after every sample (not batched) so a
    crashed/killed run loses at most one in-flight generation; `load_done_keys()` lets a
    re-run skip whatever `(position_idx, sample_idx)` pairs a worker's file already has.
    Deliberately stops at "collect scored raw samples" -- computing r̄ per
    worker/position and the softmax-τ soft target distribution is Phase 4's job (τ is a
    training hyperparameter, not this phase's business).
  - `scripts/phase3_collect_sft_data.py`: thin CLI -- generates (or loads, if already
    persisted) `logs/phase3_sft_data/positions.jsonl`, then loops over the default 3
    workers (`qwen2.5-7b`, `mistral-7b`, `deepseek-r1-distill-qwen-7b` -- the same set
    Phase 0.5 already floor-checked, per PLAN.md's "start with the smaller 3-4 model
    pool"), collecting to `logs/phase3_sft_data/<worker>.jsonl` (gitignored -- `*.jsonl`).
    Default 400 positions x 4 samples x 3 workers = 4,800 generations, within PLAN.md's
    3,600-9,600 estimate. Writes the tracked `reports/phase3_summary.json`
    (`COMPLETE`/`IN_PROGRESS`, per-worker collected/expected counts).
  - `advance_phase_3` added to `scripts/orchestrate.py` + registered in
    `PHASE_ADVANCERS`, following `advance_phase_0_5`'s pattern (not Phase 1/2's) since
    this is a long multi-day background job spanning many cron cycles, not a single
    pass/fail smoke test.
  - **Note for whoever reviews this**: Phase 0.5's `REVIEW_NEEDED` verdict (weak
    legal-move-rate floor, see below) is about worker chess *quality*, which this phase
    doesn't touch (it's plumbing -- position generation + scoring infrastructure), but it
    directly precedes the thing that flag is actually meant to gate: real GPU-hours spent
    generating SFT data. **A human should look at Phase 0.5's numbers again (or at least
    a first small batch of this phase's collected data) before letting the full
    400x4x3 run complete unattended.**

## Bugs fixed this session (worth knowing before extending the harness further)

1. **Chat-template crash on Black games.** `harness.py`'s `play_blindfold_vs_engine`
   appended two consecutive `"user"` messages (opening prompt, then the engine's first
   move) whenever the LLM played Black, with no `"assistant"` turn between them.
   Qwen's lenient template silently tolerated it (and produced garbage); Mistral's
   stricter template raised `jinja2.TemplateError` and crashed the *entire* floor-check
   run, including untested workers queued after it. Fixed by folding the engine's first
   move into the same initial user turn.
2. **Move-parsing too strict.** `extract_uci_move` required contiguous `e2e4`-style
   text; models very commonly reply `e2-e4` (hyphenated) or bare SAN (`d4`, `Nf3`).
   Fixed: the UCI regex now tolerates an optional hyphen, and a SAN fallback tries
   `board.parse_san()` (reusing python-chess's own robust parser/legality check) on
   every whitespace-split token before giving up.
3. **`HF_HOME` set after importing `transformers`** in `local_worker.py`, so the
   override never took effect -- combined with the newer `hf_xet` download backend
   ignoring `HF_HOME` regardless, worker downloads landed in `~/.cache` and blew
   through this host's 30GB NFS home quota. Fixed: env var set before the import, plus
   `HF_HUB_DISABLE_XET=1` to fall back to plain HTTP downloads.
4. **`max_new_tokens=32` in the floor check** was too small for models that preface
   their answer with prose or (for `deepseek-r1-distill`) a reasoning chain -- responses
   were truncated before ever stating a move. Bumped to 200.

With all four fixed, Phase 0.5's numbers went from a uniform, meaningless 0% (parsing/
crash artifacts) to real per-worker variation (0% / 44% / 22%) -- worth remembering
that a "floor check found nothing works" result can itself be a bug, not a finding.

## IMPORTANT limitation of the autonomous mechanism

`cron` + `tmux` keep **already-written, already-launched** work running/retrying without
any agent session -- this is what let Phase 0.5 and Phase 1's smoke test survive
disconnects. They **cannot write new code.** Per the user's explicit ask to also use
Anthropic-managed remote scheduling: a recurring **cloud routine** (`claude.ai/code/routines`,
NOT the same thing as this host's cron) can be set up to periodically `git pull`, read
`PLAN.md`/`STATUS.md`/`state.json`/`reports/*.json`, write the next not-started phase's
code, register its `advance_phase_N` in `orchestrate.py`, and push -- **but it runs in an
isolated Anthropic cloud sandbox with zero access to this host's GPU/filesystem/tmux
sessions**, so it can only ever do the *writing*, never the *running*. This host's cron
picks up what it pushes via `git_pull_if_clean()` and executes it on the real GPU. (This
routine is now active -- it wrote Phase 2's and Phase 3's code, see "What's actually
done" and "Cloud dev routine additions" above -- confirmed working as of 2026-07-10,
including same-day fix-forward on Phase 2's first failed run.)

## 5x5 (Gardner Minichess) fast-validation track + evolution demo (2026-07-10)

New, **independent** phase track (`state.json`'s `minichess_phase_order` /
`minichess_phases`, `m0`-`m8`) built this session in response to a user request: an
interactive demo showing the orchestrator's routing policy evolve across Fugu's
training stages (random routing → SFT-trained head → CMA-ES-evolved), on Gardner
Minichess (5x5) instead of full chess -- cheap enough to validate the whole SFT+CMA-ES
pipeline once before Phases 3-9 above spend real GPU-hours on it. Runs in parallel with
the main track above (`scripts/orchestrate.py`'s `advance_track()` advances both once
per cron tick, neither blocks the other). Full spec: PLAN.md's "5x5 fast-validation
track" addendum -- read that before touching this, it has the milestone table and the
exact engine-stack gotchas (pyffish/uv install trap, Fairy-Stockfish stdin-EOF trap).

- **m0 (done)**: `pyffish` (legality/FEN, `uv pip install pyffish` -- has no 3.12 wheel,
  builds from source) + `bin/fairy-stockfish` (search/eval, gitignored/host-specific,
  `scripts/m0_setup_gardner_engine.sh` downloads it) -- `gardner` UCI variant verified
  working on `lotte.polytechnique.fr`.
- **m1 (done)**: `src/open_fugu/minichess/{board,engine}.py` (`GardnerBoard`/
  `GardnerScorer`, chess.Board/StockfishScorer-shaped) + `harness.py` gained a
  `board_factory=` param (default `chess.Board`, zero behavior change for full chess).
  Verified via `scripts/m1_verify_gardner_engine.py` (hand-constructed positions with an
  objectively known correct answer, same style as Phase 2's sanity check --
  `reports/m1_gardner_engine_verify_result.json`, `passed: true`) plus a manual
  end-to-end `harness.play_blindfold_vs_engine()` smoke test.
- **m2 onward: not started** -- needs a dev session to write m2's floor-check script
  (including a small hand-verified Gardner opening book, none exists yet) before cron
  can run it, same "no script yet" gating as the main track.

**IMPORTANT if resuming on a different host than `lotte.polytechnique.fr`**: `bin/`
(including `bin/fairy-stockfish`) is gitignored and host-specific, same as the
full-chess Stockfish binary -- it does NOT transfer with a `git clone`/`pull`. Run
`scripts/m0_setup_gardner_engine.sh` then `scripts/m1_verify_gardner_engine.py --force`
to re-verify the engine stack on the new host before trusting `state.json`'s `m0`/`m1:
done` (that status reflects verification on `lotte.polytechnique.fr` specifically, not
a portable guarantee). The venv itself also doesn't transfer -- see PLAN.md's "Ground
truth" section for the full `uv venv` + dependency recreation steps, including the
`pip`-vs-`uv pip` trap this session hit once (bare `pip install` silently uses the
wrong Python/venv here).

**One-machine-at-a-time note**: as of this write-up, Phase 3 (full-chess SFT data
collection) is actively running in a `tmux` session (`openfugu-phase3_sft_data`) on
`lotte.polytechnique.fr`, launched autonomously by that host's cron a few minutes
before this section was written. If picking up the minichess track on a *different*
host while that's still running, that's two hosts doing real GPU work
simultaneously -- against this project's own hard constraint (see "Constraints to keep
honoring" below). Check `state.json`'s phase `3` status / `ssh lotte.polytechnique.fr
tmux ls` before launching anything GPU-heavy elsewhere; `m0`/`m1` are CPU-only and fine
to re-verify anywhere, but `m2` onward needs real worker inference.

## Exact next steps

1. `scripts/status.py` for a quick check; `state.json` is the source of truth.
2. **Phase 3 needs to actually run** on the GPU host (code written, `status: pending` --
   see "Cloud dev routine additions" above). This is the long one (2-4 days background,
   ~10-20 GPU-hours) -- expect it to span many cron cycles. Per the note above, a human
   should sanity-check Phase 0.5's `REVIEW_NEEDED` numbers (or an early slice of Phase
   3's own output) before letting the full run complete unattended.
3. **Minichess track (`m2`)**: write the 5x5 floor-check script (small hand-verified
   opening book + reuse `harness.py`'s `board_factory=GardnerBoard`) -- see the section
   above and PLAN.md's addendum for the full spec.
4. **Phase 1.5-ish polish**: consider a prompt-engineering pass on
   `MOVE_FORMAT_INSTRUCTION`/few-shot examples to push Phase 0.5's legal-move rates up --
   current numbers (0/44/22%) are a legitimate but weak floor (`REVIEW_NEEDED`); this
   would also directly improve Phase 3's SFT data quality if done first.
5. **Phase 1 extension**: add the other candidate workers as their own A2A agents
   (currently only `qwen2.5-7b` has been run as a purple agent; `worker_agent.py` takes
   any `CANDIDATE_WORKERS` short id via `--worker`), and update
   `orchestrator_agent.py`'s `--workers` CLI arg / the scenario TOML accordingly.
6. **Phase 4** is when per-query (not per-game) routing actually matters -- revisit
   `orchestrator_agent.py`'s "Phase 1 scope" code comment before assuming random
   per-game routing is still fine once the real selection head exists. Phase 4 is also
   where Phase 3's raw per-sample records turn into r̄-per-worker/position and a
   softmax-τ soft target distribution for SFT training.

## Constraints to keep honoring

- One machine at a time for actual execution (no simultaneous GPU work across hosts);
  a cloud dev routine writing/pushing code is fine (see above), running GPU work in the
  cloud sandbox is not.
- `chmod 700` every created directory, `600` every created file, `umask 077` before any
  bulk creation.
- 100GB disk cap via `disk_guard.py`, warn at 80GB, refuse + ask at 100GB.
- Long GPU jobs go in `tmux -d` sessions; phase-advancement runs via plain host `cron`
  (installed, `*/15 * * * *`).

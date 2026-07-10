# Open-Fugu — Status (living document)

Last updated: 2026-07-10 (cloud dev routine). Phase 0, 0.5, 1, and 2 all complete on
`lotte.polytechnique.fr` -- Phase 2's `reports/phase2_stockfish_sanity_result.json`
shows `passed: true` (after a same-day fix for a `chmod 600` sweep that had stripped
`+x` from `bin/stockfish-wrapper.sh`/the Stockfish binary, and a loosened
near-zero-noise threshold on one check -- see the host's own commit history for
details, not reproduced here). Phase 3's code has now been written and pushed by the
cloud dev routine, but **it will not auto-launch**: `advance_phase_3` now requires an
explicit human sign-off flag (`state.json`'s `phases["3"].gpu_spend_approved`,
currently `false`) before it will tmux-launch the actual data collection run -- see
"Phase 3 needs a human decision before it can run" below, this is important.
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

## Phase 3 needs a human decision before it can run

Phase 0.5's floor check (`reports/phase0_5_summary.json`) came back **`REVIEW_NEEDED`**:
mean legal-move rate 0% (`qwen2.5-7b`), 44% (`mistral-7b`), 22%
(`deepseek-r1-distill-qwen-7b`). Phase 3 is exactly the ~10-20 GPU-hour spend (PLAN.md's
training-recipe cost estimate) that this gate exists to protect -- unlike Phase 2, which
only sanity-checked the reward *scorer* and was safe to auto-run regardless of worker
quality.

An earlier pass at this phase's code (see "Cloud dev routine additions" below) registered
`advance_phase_3` the same way as every prior phase -- i.e. it would have auto-launched
on the very next cron tick, since the host's cron runs every 15 minutes unattended and
nothing was checking the `REVIEW_NEEDED` verdict at all. That's been tightened:
`advance_phase_3` now checks `state.json`'s `phases["3"].gpu_spend_approved` flag
(currently `false`) and returns `"blocked"` with a clear printed message instead of
tmux-launching if it isn't `true`. This is a code-level gate, not just a note in this
doc -- important given cron runs unattended and would otherwise burn real GPU-hours
collecting SFT data dominated by illegal-move noise for at least `qwen2.5-7b`.

**To unblock Phase 3**, a human should:
1. Decide whether 0%/44%/22% is an acceptable starting point (the paper's own "Key open
   risk #3" suggests some weakness here is expected for 7B models playing genuinely
   blindfold), or do the prompt-engineering pass in "Exact next steps" below first and
   re-run Phase 0.5 (`--force`) to see if the numbers improve.
2. Once satisfied, edit `state.json` and set `phases["3"].gpu_spend_approved` to `true`
   (leave `status` as `"pending"` -- `orchestrate.py` doesn't gate on `status`, only on
   this flag). The next cron tick will then tmux-launch
   `scripts/phase3_collect_sft_data.py` normally.

## Cloud dev routine additions (2026-07-10)

- **Phase 3 (SFT data collection) -- code written, status `pending` +
  `gpu_spend_approved: false` in `state.json`, NOT AUTO-LAUNCHABLE (see "Phase 3 needs a
  human decision" above).** Per PLAN.md's training recipe step 1: sample ~300-600 blindfold-chess
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
    pass/fail smoke test -- **plus a `gpu_spend_approved` sign-off gate on top, see
    "Phase 3 needs a human decision before it can run" above.**

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

## Exact next steps

1. `scripts/status.py` for a quick check; `state.json` is the source of truth.
2. **A human needs to decide on Phase 3** -- see "Phase 3 needs a human decision before it
   can run" above. Either set `phases["3"].gpu_spend_approved = true` in `state.json` to
   approve as-is, or do the prompt-engineering pass in (3) below first and re-run Phase
   0.5 to see if the floor-check numbers improve before spending the ~10-20 GPU-hour SFT
   data collection budget. Nothing runs until this flag is set.
3. **Phase 1.5-ish polish**: consider a prompt-engineering pass on
   `MOVE_FORMAT_INSTRUCTION`/few-shot examples to push Phase 0.5's legal-move rates up --
   current numbers (0/44/22%) are a legitimate but weak floor (`REVIEW_NEEDED`); this
   would also directly improve Phase 3's SFT data quality if done first.
4. **Phase 1 extension**: add the other candidate workers as their own A2A agents
   (currently only `qwen2.5-7b` has been run as a purple agent; `worker_agent.py` takes
   any `CANDIDATE_WORKERS` short id via `--worker`), and update
   `orchestrator_agent.py`'s `--workers` CLI arg / the scenario TOML accordingly.
5. **Phase 4** is when per-query (not per-game) routing actually matters -- revisit
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

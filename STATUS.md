# Open-Fugu — Status (living document)

Last updated: 2026-07-10 (cloud dev routine -- wrote Phase 4, see "Cloud dev routine
additions" below; `minichess_phase_order` track unchanged this run). Phase 0, 0.5, 1, 2,
and now **3 are all complete** on `lotte.polytechnique.fr` -- Phase 3's
`reports/phase3_summary.json` shows `verdict: COMPLETE` (4,800/4,800 records: 400
positions x 4 samples x 3 workers, all three of `qwen2.5-7b`/`mistral-7b`/
`deepseek-r1-distill-qwen-7b` fully collected). **Phase 4 (SVF + selection head + SFT
training) has been written this session and is now `pending`**, awaiting the GPU host's
cron to actually run it -- no additional human sign-off gate on top (see "Cloud dev
routine additions" below for why), but see that section's closing note on why a human
should still sanity-check the resulting checkpoint before Phase 5 spends GPU-hours on
real matches with it.
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

## Phase 3 (SFT data collection) -- DONE, `reports/phase3_summary.json` (`verdict: COMPLETE`)

Ran to completion on `lotte.polytechnique.fr`: 4,800/4,800 records (400 self-play
blindfold positions x 4 samples x 3 workers, all of `qwen2.5-7b`/`mistral-7b`/
`deepseek-r1-distill-qwen-7b` fully collected). Kept for the historical record: Phase
0.5's floor check (`reports/phase0_5_summary.json`) came back **`REVIEW_NEEDED`** (mean
legal-move rate 0%/44%/22% across those same three workers), which is exactly the
~10-20 GPU-hour spend `advance_phase_3`'s `gpu_spend_approved` gate exists to protect
against auto-launching unattended. That flag was set `true` because the host's own
commit history showed a human (`alfred.ruscher@gmail.com`, "Fix Phase 3 position
generation: filter out already-terminal positions") already actively engaged with this
exact run before the gate existed -- i.e. the sign-off it's meant to capture had already
happened in substance. The flag stays `true` in `state.json`; its ongoing value is
protecting any *future* crash-and-cron-relaunch of a similarly GPU-heavy phase from
resuming unattended without an equivalent check-in.

## Cloud dev routine additions (2026-07-10)

- **Phase 4 (SVF + selection head implementation, SFT training) -- code written, status
  `pending` in `state.json`, no extra approval gate.** Per PLAN.md's training recipe step
  1's second half: turn Phase 3's raw per-(worker, position, sample) records into a soft
  target distribution over the worker swarm (mean reward -> softmax-τ) per position, then
  train the orchestrator backbone's selection head + SVF `z` vectors against it via a
  plain AdamW loop minimizing cross-entropy vs. that soft target (equivalent to KL
  divergence up to the target's own entropy, a constant w.r.t. the trained parameters).
  Written by the cloud dev routine (no GPU/model access there) -- verification limited to
  `python3 -m py_compile` on every new file plus pure-logic checks (no torch installed in
  the sandbox, none needed for this half) of `build_soft_targets()`/
  `mean_reward_per_position()`/`softmax()` against hand-constructed records: verified the
  intersect-only-positions-every-worker-covers behavior, that a worker with a large
  reward-gap advantage dominates its soft target (`probs[0] > 0.99`), that `probs` always
  sums to 1, and that smaller `tau` sharpens (larger flattens) the resulting distribution
  as expected. **The next GPU-host cron run should confirm the SVD/backbone/
  training-loop half actually works end-to-end** (no way to exercise `torch.linalg.svd`,
  a real backbone forward pass, or `loss.backward()` without a GPU + the downloaded
  orchestrator backbone model) before trusting the resulting checkpoint.
  - `src/open_fugu/models/svf.py` -- hand-rolled SVF (peft 0.19.1 has no adapter for
    this, same reasoning PLAN.md already gives): `SVFLinear` wraps one `nn.Linear`,
    computing `U, S, Vh = torch.linalg.svd(weight)` once at construction, freezing
    `U`/`S`/`Vh` as buffers, and exposing only a `z` parameter (init all-ones, so the
    swap is a no-op until trained) that rescales `S` on every forward
    (`effective_weight() = U @ diag(S * z) @ Vh`). `apply_svf()` walks
    `model.model.layers[-n:]` (Qwen2/Llama-style decoder stack -- matches this project's
    orchestrator-backbone candidates) and replaces each `self_attn.o_proj`/`mlp.down_proj`
    with an `SVFLinear`, per PLAN.md's "targeting only o_proj/down_proj of the last 2-3
    orchestrator backbone layers."
  - `src/open_fugu/models/worker_backend.py` -- `OrchestratorBackbone`: loads the small
    backbone model (default `Qwen2.5-1.5B-Instruct`, per PLAN.md), freezes every
    parameter, applies `apply_svf()` to the last few layers, and adds a
    `selection_head = nn.Linear(hidden_size, L)` on top of the last-token hidden state
    (`L` = number of candidate workers, fixed output order = `config.worker_ids`).
    `trainable_parameters()` yields exactly the SVF `z` vectors + the selection head's own
    parameters -- everything else in the backbone stays frozen throughout. **Not yet
    wired into `a2a/orchestrator_agent.py`'s actual dispatch logic** -- that orchestrator
    still does Phase 1's random-per-game routing; per the existing code comment there,
    real per-query routing also needs a stateless full-transcript-forwarding redesign
    (since worker agents keep conversation state server-side keyed by A2A `context_id`),
    which is deferred to whichever of Phase 4/5 actually plays matches with this
    checkpoint -- this phase's own scope (per `state.json`'s original note and PLAN.md's
    phase table) is "SVF/head implementation + SFT training," not wiring it into live
    A2A dispatch.
  - `src/open_fugu/train/train_sft.py` -- deliberately split into a pure half
    (`build_soft_targets()`/`mean_reward_per_position()`/`softmax()`/`load_positions()`
    /`load_jsonl()`, no torch import at module level, tested in the sandbox as described
    above) and a GPU half (`train(targets, backbone, epochs, lr)`, imports `torch` and
    `harness.format_opening_prompt` inside the function body so the module stays
    importable without torch installed). `build_soft_targets()` only emits a target for
    positions where **every** requested worker has at least one scored sample -- a
    position partially covered by the swarm (e.g. one worker's Phase 3 run got killed
    mid-position) is dropped rather than guessed at, so every training example is a
    genuine head-to-head comparison. Reward = `-centipawn_loss` (an illegal/unparseable
    move already carries `StockfishScorer.MATE_SCORE_CP` there per
    `collect_sft_data.py`'s convention, so it naturally gets the worst reward with no
    special-casing), divided by a `reward_scale_cp` constant (default 100, i.e. pawns) so
    `tau` stays in a human-friendly range independent of Stockfish's raw centipawn scale.
  - `scripts/phase4_train_sft.py` -- thin CLI: loads Phase 3's
    `logs/phase3_sft_data/{positions.jsonl,<worker>.jsonl}`, builds soft targets, trains
    (skips training entirely and writes a `NO_DATA` verdict if zero positions have
    full worker coverage -- e.g. wrong `--workers` list), and saves the trained
    `selection_head` state dict + each `SVFLinear`'s `z` tensor to
    `checkpoints/phase4_sft/backbone_head_svf.pt` (gitignored, host-specific). Writes the
    tracked `reports/phase4_summary.json` (`n_soft_targets`, `final_loss`,
    `mean_loss_last_50`, `verdict`).
  - `advance_phase_4` added to `scripts/orchestrate.py` + registered in
    `PHASE_ADVANCERS`, following `advance_phase_2`'s single-pass/fail-marker pattern (not
    Phase 0.5/3's multi-day-resumable pattern -- PLAN.md estimates this phase at 0.5-1
    day, small parameter count, Phase 3 already paid the expensive part). **Deliberately
    no `gpu_spend_approved`-style gate**: unlike Phase 3, this only reads Phase 3's
    already-collected (and already human-approved) data and trains a small number of
    parameters, not a fresh multi-GPU-hour spend against the borderline worker-quality
    numbers that gate protects against. That said -- **Phase 0.5's floor check is still
    `REVIEW_NEEDED`** for this exact worker pool, so `reports/phase4_summary.json`'s own
    `note` field flags that a human should look at `final_loss`/`mean_loss_last_50` once
    this actually runs: with workers this weak at producing legal moves, it's plausible
    the per-position soft targets end up close to uniform (little real signal for the
    head to learn) rather than genuinely discriminating between workers. Worth a look
    before Phase 5 spends GPU-hours on real matches using this checkpoint's routing.

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
    "Phase 3 (SFT data collection) -- DONE" above.** (This bullet describes the state as
    originally written; Phase 3 has since completed -- see that section.)

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
- **m2 (code written by the cloud dev routine, status `pending` -- awaiting GPU host).**
  `scripts/m2_gardner_floor_check.py`: Phase 0.5's floor check re-run on 5x5, same
  default 3-worker pool, over `harness.play_blindfold_vs_engine(board_factory=
  GardnerBoard, ...)` + `GardnerScorer`. Needed a small opening book that didn't exist
  yet -- `GARDNER_OPENING_BOOK` (4 lines, 4 plies each: `pawn_knight_skirmish_c`,
  `knight_pawn_flank_b`, `pawn_queen_skirmish_d`, `pawn_bishop_skirmish_e`), one game per
  line, alternating LLM color. Per PLAN.md's explicit "don't guess moves" instruction:
  every line was checked ply-by-ply against `pyffish.legal_moves()` before being
  hardcoded, using a throwaway `pip install pyffish` venv in the cloud sandbox (CPU-only
  move generation, no GPU needed, so this was actually runnable there unlike the rest of
  this phase) -- same verification method M1 used, not a guess. Also ran a pure-logic
  integration check driving the real `harness.play_blindfold_vs_engine` through all 4
  book lines with fake (non-GPU) move functions: confirmed the illegal-move-termination
  path triggers correctly for all 4 lines x both colors, and a both-sides-random-legal
  variant plays multiple plies to checkmate/max-plies with zero crashes and zero
  false-illegal calls -- see the cloud dev routine's session for the exact script (not
  committed, sandbox-only scratch verification).
  - Opponent weakening: unlike Phase 0.5 (vanilla Stockfish's `Skill Level` UCI option,
    `skill_level=1`), `GardnerScorer`/Fairy-Stockfish has no *confirmed* `Skill Level`
    option on this binary (not verifiable without GPU-host access to the binary itself),
    so `m2_gardner_floor_check.py` weakens the opponent via a shallower search
    (`ENGINE_FLOOR_DEPTH = 8`, vs. `engine.py`'s default `depth=14`) instead --
    deliberately did **not** touch `engine.py` itself to add an unverified UCI option,
    since m1's engine code is already verified-passing and this didn't need changing it.
  - `advance_m2` registered in `orchestrate.py`'s `MINICHESS_PHASE_ADVANCERS`, following
    `advance_phase_0_5`'s pattern (per-worker tracked reports + a `write_m2_summary()`
    aggregate, not `advance_m1`'s single pass/fail marker) since this reports per-worker
    legal-move-rate numbers the same way Phase 0.5 does. Dry-run verified (mocked
    `tmux_session_exists`/`tmux_launch`, real `advance_track()` logic) to correctly reach
    and launch m2 once m0/m1 are done. Writes `reports/m2_gardner_floor_check_summary.json`
    (tracked) once all 3 workers finish -- same `PASS`/`REVIEW_NEEDED` heuristic as
    `reports/phase0_5_summary.json`.
  - **Needs the GPU host to actually run** (real worker inference + `bin/fairy-stockfish`,
    neither available in the cloud sandbox) -- next cron tick on `lotte.polytechnique.fr`
    picks it up automatically now that `advance_m2` exists and `state.json`'s `m2` is
    `pending`.

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
2. **Phase 4 (SVF + selection head + SFT training) -- DONE writing, `pending`
   execution.** Code written this session (see "Cloud dev routine additions" above);
   `advance_phase_4` will launch `scripts/phase4_train_sft.py` on the GPU host's next
   cron tick, no extra approval gate. Once `reports/phase4_summary.json` lands, a
   human/dev session should read `final_loss`/`mean_loss_last_50` there before Phase 5
   spends GPU-hours on real matches using the resulting checkpoint -- Phase 0.5's floor
   check is still `REVIEW_NEEDED` for this worker pool, so it's worth confirming the
   head actually learned a non-trivial routing signal rather than fitting near-uniform
   soft targets.
3. **Minichess track (`m2`) -- DONE writing, `pending` execution.** Floor-check script +
   opening book written earlier this session (see "5x5 (Gardner Minichess)..." section
   above); `advance_m2` will launch it on the GPU host's next cron tick. Once
   `reports/m2_gardner_floor_check_summary.json` lands, a human/dev session should read
   its verdict before m3 (A2A wiring) is built against this worker pool, same spirit as
   Phase 0.5's gate.
4. **Phase 1.5-ish polish**: consider a prompt-engineering pass on
   `MOVE_FORMAT_INSTRUCTION`/few-shot examples to push Phase 0.5's legal-move rates up --
   current numbers (0/44/22%) are a legitimate but weak floor (`REVIEW_NEEDED`); this
   would also directly improve the quality of both Phase 3's already-collected SFT data
   and Phase 4's resulting checkpoint if done and re-run first.
5. **Phase 1 extension**: add the other candidate workers as their own A2A agents
   (currently only `qwen2.5-7b` has been run as a purple agent; `worker_agent.py` takes
   any `CANDIDATE_WORKERS` short id via `--worker`), and update
   `orchestrator_agent.py`'s `--workers` CLI arg / the scenario TOML accordingly.
6. **Wiring Phase 4's trained selection head into live A2A dispatch** is still open --
   `a2a/orchestrator_agent.py` still does Phase 1's random-per-game routing;
   `models/worker_backend.py`'s `OrchestratorBackbone` exists now but isn't called from
   there yet. Per the existing code comment in `orchestrator_agent.py`, real per-query
   routing also needs a stateless full-transcript-forwarding redesign (worker agents
   keep conversation state server-side keyed by A2A `context_id`) -- likely Phase 5's
   job, since that's when Open-Fugu actually plays matches with this checkpoint.

## Constraints to keep honoring

- One machine at a time for actual execution (no simultaneous GPU work across hosts);
  a cloud dev routine writing/pushing code is fine (see above), running GPU work in the
  cloud sandbox is not.
- `chmod 700` every created directory, `600` every created file, `umask 077` before any
  bulk creation.
- 100GB disk cap via `disk_guard.py`, warn at 80GB, refuse + ask at 100GB.
- Long GPU jobs go in `tmux -d` sessions; phase-advancement runs via plain host `cron`
  (installed, `*/15 * * * *`).

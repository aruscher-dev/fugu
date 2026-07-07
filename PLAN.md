# Open-Fugu: open-source replication/improvement of Sakana Fugu for strategic games

*(This is the approved project plan, copied verbatim from
`~/.claude/plans/modular-seeking-bee.md` on the original machine so it travels
with the repo. See `STATUS.md` for what's actually been done and what a fresh
session on a new host needs to do next.)*

## Context

Sakana AI's "Fugu" (arXiv:2606.21228) is a family of *learned LLM orchestrators*: a small
backbone model computes a hidden state, a lightweight selection head turns it into logits
over a pool of worker LLMs, and the query is dispatched to the chosen worker. Training is
two-stage: (1) SFT of the head + singular-value-fine-tuned (SVF) backbone weights against a
soft target distribution built from per-worker reward rankings, (2) sep-CMA-ES to directly
maximize end-to-end task reward. Fugu's own "strategic game mechanics" demonstration
(Appendix B.2) is blindfold chess: models play from memory only (opening move list, then
only ever the opponent's last UCI move, no board/FEN, no legal-move list), scored via
Stockfish ACPL/blunder rate against Elo-anchored baselines.

Fugu's own worker pool is closed frontier models (Gemini/Claude/GPT). The goal here is to
reproduce the same orchestration recipe with an **open-source worker pool**, running
entirely on a single local GPU (originally an RTX 3090, 24GB), and demonstrate it on
blindfold chess (matching the paper's own game demo) plus a couple of games in the
`gtbench` harness. The original machine had directly relevant infrastructure at
`~/Team/llm_chess`, `~/Team/chess_bench`, and `~/Team/gtbench` (see "Ground truth"
below) — worth checking whether the new host has an equivalent `~/Team` checkout, but
none of it is required (this repo vendors what it needs).

**Hard constraints from the user, binding for the whole project:**
- **One machine at a time.** Work happens on exactly one SSH host at a time — no
  simultaneous execution across two machines, no spawning remote-isolated agents.
  (Originally stated as "no other SSH than this one"; updated when the user needed to
  move off a machine another person was actively using — the constraint is "one host at
  a time," not "this specific host forever.")
- **Privacy on created folders.** Every directory this project creates gets restrictive
  permissions (owner-only: `chmod 700` dirs / `600` files) — shared hosts mean shared
  `/Data` and home directories.
- **100GB disk budget, with a warning before exceeding it.** A disk-usage guard tracks the
  project's own footprint (not any pre-existing shared HF cache) and must warn (not
  silently proceed) before any operation would push cumulative usage past 100GB.
- **Autonomous resumption on credit/quota reset.** Since this is a multi-day project and
  the operating agent (me) may hit its own usage limits mid-run, work must be structured
  in resumable, checkpointed phases with idempotent scripts, so a periodic scheduled
  wakeup (or plain host `cron`) can check status and continue rather than restart. A
  cloud-scheduled dev routine (writes/pushes code only, no GPU/host access) may
  supplement this for the "write new phase code" half of resumption; the "run it on the
  GPU" half still has to be this host's own cron+tmux (see STATUS.md's "IMPORTANT
  limitation" section for exactly where that split falls).
- **Inter-agent communication over A2A, AgentBeats-style.** All agent-to-agent
  interaction (worker↔orchestrator, orchestrator↔green judge) uses the A2A protocol,
  following UC Berkeley RDI's AgentBeats competition conventions as closely as
  reasonable, rather than bespoke in-process calls or a custom HTTP API. See
  "Architecture" below for the concrete purple/green agent breakdown.

## Ground truth from the ORIGINAL host (verify all of this again on the new machine!)

- GPU: 1x RTX 3090, 24GB, currently idle (~132MiB used by desktop). Shared host — check
  `nvidia-smi` before big runs, don't assume continued exclusivity.
- `/Data` had 676GB free; HF cache at `/Data/.hf_cache` (148GB, pre-existing, shared across
  this student's other projects) already contained **Qwen2.5-7B-Instruct**,
  **Mistral-7B-Instruct-v0.3**, **DeepSeek-R1-Distill-Qwen-7B**,
  **DeepSeek-R1-Distill-Llama-8B**, **Qwen3-8B**, **Qwen2.5-3B-Instruct**,
  **NousResearch/Hermes-3-Llama-3.1-8B** — zero download needed for the worker pool
  *on that machine*. **This cache does NOT transfer with the repo** — on a new host,
  either it has its own pre-existing cache (check `$HF_HOME` / `~/.cache/huggingface`)
  or these will need a fresh download (a few GB each, ~30GB+ total for the 7-8B tier).
- **Critical gotcha discovered the hard way: home directory NFS quota.** The original
  host's home dir (`alpha.polytechnique.fr:/students`) had a hard 30GB quota, already at
  28.6GB used — `git clone` failed with "Disk quota exceeded" until the whole project was
  relocated to `/Data/alfred.ruscher/open_fugu` (676GB filesystem) with a `~/open_fugu`
  symlink back for convenience. **Check the new host's home quota before doing anything
  — `quota -s` — and put this project on whatever large/scratch filesystem exists there
  (equivalent of `/Data`), not directly under `$HOME`, if quota is tight.**
- Working Python 3.12 venv (was at `/Data/.venv`, symlinked `~/.venv`) with
  torch 2.11+cu128, transformers 5.8.1, peft 0.19.1, trl 0.29.1, accelerate — **this venv
  does not transfer**; recreate on the new host (`uv venv` + `uv pip install torch
  transformers peft trl accelerate bitsandbytes python-chess ag2 cma fastapi uvicorn
  httpx pyyaml pandas scipy` — see exact versions actually used in the commit history /
  below). Managed by `uv` — installs go through `uv add`/`uv pip install`, not raw pip.
- **Do not install vllm into this venv** — its pinned torch range would likely force a
  downgrade and break the validated transformers/peft/trl stack. transformers+bitsandbytes
  also gives direct hidden-state access, which the orchestrator needs anyway.
- Local Stockfish: downloaded the official Stockfish 18 Linux avx2 static build from
  `github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar`.
  **On the original host it failed to run** (`GLIBCXX_3.4.30' not found` — system
  libstdc++ too old) until launched with `LD_LIBRARY_PATH=/usr/local/gcc-15.1.0/lib64`
  (a newer libstdc++ bundled with that host's gcc-15 install) — see
  `bin/stockfish-wrapper.sh`. **This exact path is host-specific and will very likely
  need to change** — on a new host, either the system libstdc++ is new enough already
  (try running `bin/stockfish` directly first) or you'll need to find/install an
  equivalent newer libstdc++ and update the wrapper script's `LD_LIBRARY_PATH`.
- Vendored `vendor/llm_chess` (fresh clone of `maxim-saplin/llm_chess` upstream) — this
  IS in git history if pushed with the vendor dir included, but the current `.gitignore`
  excludes `vendor/` (it's its own separate upstream git repo, re-clonable with one
  command) — re-run `git clone https://github.com/maxim-saplin/llm_chess.git vendor/llm_chess`
  on the new host.
- peft 0.19.1 has no SVF (singular-value fine-tuning) support — all its adapters (LoRA,
  AdaLoRA, IA3, etc.) add a new low-rank ΔW rather than reparameterizing-and-freezing the
  base weight's own SVD. SVF must be hand-rolled: one-time `torch.linalg.svd` per target
  `nn.Linear`, freeze `U`/`Vh` as buffers, train only a `z` vector rescaling `S`. This
  mirrors SakanaAI's real *Transformer²* codebase (`SakanaAI/self-adaptive-llms`), a solid
  concrete precedent to follow. **Not yet implemented as of this writing.**

## Architecture

- **Worker pool — a real swarm, mixing sizes/specializations (per user request):**
  - *Mid workers (7-8B):* Qwen2.5-7B-Instruct, Mistral-7B-Instruct-v0.3,
    DeepSeek-R1-Distill-Qwen-7B, DeepSeek-R1-Distill-Llama-8B.
  - *Small workers (1-3B, adds swarm breadth cheaply):* Qwen2.5-3B-Instruct,
    Qwen2.5-1.5B-Instruct, Llama-3.2-3B-Instruct, Gemma-2-2B-it.
  - L≈6-8 workers total instead of 3-4 — closer to Fugu's premise that orchestration
    gains come from combining genuinely complementary, differently-skilled models.
    Start the SFT/eval pipeline with the smaller 3-4 model pool to keep the first
    milestone tractable, then re-run Phase 3 onward with the full L≈6-8 swarm as an
    explicit "does scale-of-swarm help" experiment.
  - Served locally via transformers + bitsandbytes NF4 (4-bit). Small workers (~1-2GB
    each in 4-bit) can stay resident continuously; an LRU swap manager governs the 7-8B
    mid workers (~4.5-5.5GB each), keeping at most 2 hot at once.
- **Orchestrator backbone:** small open model (Qwen2.5-1.5B-Instruct to start; fall back
  to Qwen2.5-3B-Instruct if 1.5B's hidden states don't discriminate workers well).
- **Selection head:** `nn.Linear(hidden_size, L)` on the backbone's last-token hidden
  state — logits over the L workers, dispatch by argmax (eval) / softmax-τ sample (SFT
  data collection).
- **SVF adaptation:** hand-rolled, targeting only `o_proj`/`down_proj` of the last 2-3
  orchestrator backbone layers.
- **Inter-agent communication: A2A protocol, AgentBeats-style (added Phase 1, per user
  request).** Originally planned as a bespoke FastAPI OpenAI-compatible router server;
  replaced with a proper agent-to-agent architecture matching UC Berkeley RDI's
  AgentBeats competition conventions (course material at
  `~/Team/AgentBeats_bench`, esp. `finance_economics/tutorial-agent-beats-comp` and
  `games_virtual_environments/build_what_i_mean/pragmatic_builder` as the reference
  implementations this project's `src/open_fugu/agentbeats/` vendors from, MIT license):
  - Each worker LLM is its own **A2A purple agent** (`a2a/worker_agent.py`), serving an
    `AgentCard` + a `blindfold_chess_move` skill, built on `a2a-sdk` (pinned `0.3.5` to
    match the course reference code's API — the PyPI-latest `1.1.0` has since renamed/
    moved several modules, e.g. `a2a.server.apps` no longer exists there).
  - The Fugu orchestrator is *itself* an A2A purple agent (`a2a/orchestrator_agent.py`):
    the green judge only ever talks to it, never to a worker directly; internally it
    dispatches to a worker over A2A too (agent-to-agent, not an in-process call). Phase 1
    scope: random routing per game. Real per-query routing (matching the paper) needs
    the trained selection head (Phase 4) and a stateless full-transcript-forwarding
    redesign, since worker agents keep conversation state server-side keyed by A2A
    `context_id` — switching workers mid-game would silently drop context otherwise.
  - Evaluation (Phase 0.5 floor check, Phase 5 matches) is modeled as an AgentBeats
    **green (judge) agent** (`a2a/chess_green_agent.py`): receives an `EvalRequest`
    naming the orchestrator's URL, plays blindfold games with the real board/Stockfish
    scoring kept entirely server-side (never crosses the A2A wire, preserving
    blindfold-ness), returns an `EvalResult` artifact.
  - Scenarios are TOML-driven (`config/scenario_blindfold_chess_smoke.toml`) and launched
    via the vendored `agentbeats.run_scenario` + `client_cli`, matching the competition's
    own submission format (green agent + participants + config) — this also means the
    project could, with a Dockerfile and a registration step, be submitted to the actual
    AgentBeats platform (agentbeats.dev) largely as-is.

## Training recipe

1. **SFT stage** (single-step, cheap): sample ~300-600 chess positions (originally
   planned from `~/Team/chess_bench/data/puzzles.csv` — 10k Lichess FEN+solution
   puzzles; re-source equivalent puzzle data on the new host, e.g. the Lichess puzzle
   database is public), query every worker n=3-4 times each (capped `max_new_tokens`
   ~128-256 — reasoning-distill workers ramble otherwise), score via the Stockfish
   scorer (centipawn-loss-vs-best), average into r̄ per worker/position, softmax-τ into
   a soft target distribution, train head+SVF-z via KL divergence (plain AdamW custom
   loop). Rough cost: 300-600 positions × 3-4 samples × 4 workers ≈ 3,600-9,600
   generations ≈ 7.5-20 GPU-hours.
2. **Evolutionary stage (sep-CMA-ES), explicitly a stretch goal**: full blindfold games
   as rollouts, reward blending win/loss/draw with graded `-ACPL`. Validate the loop
   first on `gtbench`'s cheap `kuhn_poker` before spending chess GPU-hours on it.

## Directory layout — `open_fugu/` (chmod 700 at creation, and every subdir as created)

```
open_fugu/
  PLAN.md, STATUS.md          # this file + the living status doc
  pyproject.toml, .env (HF_HOME=, STOCKFISH_PATH=, ROUTER_PORT=)
  state.json                   # phase status for orchestrate.py
  config/{workers,orchestrator,sft,cmaes}.yaml, scenario_blindfold_chess_smoke.toml
  reports/     # tracked (NOT gitignored) -- small JSON summaries so the cloud dev
               # routine can see gate verdicts without access to this host's logs/
  vendor/llm_chess/             # fresh clone + blindfold-mode patch (gitignored, re-clone)
  bin/{stockfish, stockfish-wrapper.sh}   # gitignored, host-specific binary
  src/open_fugu/
    models/{local_worker, worker_backend,svf}.py
    reward/stockfish_scorer.py
    chess_blindfold/harness.py       # sync + async (play_blindfold_vs_engine[_async])
    agentbeats/   # vendored MIT-licensed AgentBeats tutorial SDK helpers (models,
                  # client, tool_provider, green_executor, run_scenario, client_cli)
    a2a/          # this project's own agents, built on the vendored SDK above:
      worker_agent.py         # purple agent wrapping one LocalWorker
      orchestrator_agent.py   # purple agent-of-agents (Fugu backbone; random routing
                               # in Phase 1, learned selection head from Phase 4)
      chess_green_agent.py    # green judge: blindfold chess vs. Stockfish over A2A
    data/{chess_positions,collect_sft_data}.py
    train/{train_sft,train_cmaes}.py
    eval/{run_eval_matches,aggregate_metrics}.py
    gtbench_ext/{local_transformers_model,orchestrator_router_model}.py
  scripts/
    disk_guard.py, phase0_5_blindfold_floor_check.py,
    phase1_agentbeats_smoke_test.py, phase2_stockfish_setup_sanity.py,
    phase3_collect_sft_data.py, phase4_train_sft.py,
    phase5_baseline_and_fugu_matches.py, phase6_eval_report.py,
    phase7_cmaes_kuhn_pilot.py (stretch), phase8_cmaes_chess_pilot.py (stretch),
    phase9_gtbench_extension.py (stretch),
    orchestrate.py, status.py, install_crontab.sh
  logs/        # gitignored — tmux session stdout+stderr per phase
  checkpoints/ # gitignored — SFT head+SVF weights, CMA-ES generation snapshots
```

## Phase sequencing (rough estimates)

| Phase | Content | Est. wall-clock |
|---|---|---|
| 0 | Env setup (deps, HF_HOME, Stockfish binary, clone llm_chess, disk_guard.py, chmod 700 everything) | 0.5-1 day |
| 0.5 | **Floor check**: 5-10 quick blindfold games per worker — gate viability before committing GPU-hours | 0.5 day |
| 1 | Worker/orchestrator/green judge as A2A agents (AgentBeats-style, random-routing dummy orchestrator first) | 1-2 days |
| 2 | Stockfish reward pipeline + sanity check against known games | 0.5-1 day |
| 3 | SFT data collection (300-600 positions × 3-4 samples/worker) | 2-4 days background (~10-20 GPU-hrs) |
| 4 | SVF/head implementation + SFT training | 0.5-1 day |
| 5 | Baseline + Open-Fugu blindfold matches (5 conditions × ~25 games) | 1-3 days background (~25 GPU-hrs) |
| 6 | Evaluation report: ACPL, blunder rate, win-rate, illegal-move rate | 0.5-1 day |
| **— Milestone: Open-Fugu v0 (SFT-only) complete —** ||
| 7 (stretch) | CMA-ES pilot on `kuhn_poker` | 2-5 days |
| 8 (stretch) | CMA-ES on truncated blindfold chess | open-ended, explicitly under-converged |
| 9 (stretch) | gtbench extension (`connect_four`/`breakthrough` + `kuhn_poker`) | 2-4 days |

## User constraints — concrete implementation

- **Privacy**: every setup script runs `umask 077` before creating anything, then
  `chmod 700` on the project root and every subdirectory it creates. No files world- or
  group-readable.
- **One machine at a time**: all execution happens on exactly one host per session; no
  simultaneous cross-host runs.
- **100GB disk cap with warning**: `scripts/disk_guard.py` computes project directory
  size plus the incremental growth of the HF cache caused by *this project's* new
  downloads (tracked via a baseline manifest taken before Phase 0 downloads), compares
  against a configurable cap (default 100GB) with a warn threshold at 80GB. At the cap it
  refuses to proceed and asks for explicit confirmation rather than silently continuing.
- **Autonomous resumption, independent of the laptop/SSH connection staying open**:
  `state.json` + idempotent `scripts/phaseN_*.py` (each checks for its own output
  artifact, skips if already done). Long jobs launched via `tmux new-session -d`
  (survives SSH/VSCode disconnects as long as the host stays up). `scripts/orchestrate.py`
  reads `state.json`, finds the first non-done phase, resumes/restarts as needed.
  **The driver of `orchestrate.py` must not depend on the agent's own session staying
  alive** — install a plain **user crontab entry** (`crontab -e`, no root needed) that
  runs `orchestrate.py` on a fixed interval, fully decoupled from any particular SSH
  session, VSCode connection, or agent session.

## Verification

- Phase 0.5 floor check must show open-source workers can produce mostly-legal blindfold
  moves above a random-move floor before continuing — if not, report as a
  negative/limiting result, not silently pushed through.
- Phase 6 report reproduces Fugu-paper-style metrics (ACPL, blunder rate, win probability
  trajectories) for Open-Fugu vs. each solo worker vs. random-routing vs. majority-vote.
- Any CMA-ES results (stretch phases) are explicitly reported as scoped proof-of-concept.

## Key open risks

1. SVF has no library support — hand-rolled, anchored to SakanaAI's real Transformer²
   precedent, but still a genuine implementation risk.
2. CMA-ES sample efficiency vs. single-GPU rollout cost is the core research risk.
3. Unknown whether 7-8B open models can play legal blindfold chess above a random floor
   at all — gated early (Phase 0.5).
4. Shared host: GPU/disk/HF-cache may be shared with other users — check `nvidia-smi`
   before big runs, respect the 100GB project-disk cap.

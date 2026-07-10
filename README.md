# Open-Fugu

An open-source-worker replication/improvement of Sakana AI's **Fugu**
(arXiv:2606.21228) — a learned LLM orchestrator that routes queries across a pool of
worker models — applied to strategic game mechanics (blindfold chess, matching the
paper's own Appendix B.2 demo, plus a couple of games from `gtbench`).

Where Fugu's own worker pool is closed frontier models (Gemini/Claude/GPT), this project
reproduces the same orchestration recipe (SFT distillation of a routing head + optional
sep-CMA-ES) with a fully open-source worker pool (Qwen2.5, Mistral, DeepSeek-R1-Distill,
Llama-3.2, Gemma-2), running locally on a single consumer GPU.

- **`PLAN.md`** — the full approved project plan: architecture, training recipe, phase
  sequencing, constraints. Includes a **"5x5 fast-validation track" addendum**: a
  second, independent phase track (Gardner Minichess, `state.json`'s
  `minichess_phase_order`/`m0`-`m8`) building an interactive demo of the orchestrator's
  routing policy evolving across training stages (random → SFT → CMA-ES) — cheap enough
  to validate the full SFT+CMA-ES pipeline before the main track's Phases 3-9 spend
  GPU-hours on full chess. Runs in parallel with the main track, not behind it.
- **`STATUS.md`** — living status doc: what's done, what's next, host-specific gotchas
  discovered along the way. Read this first when picking the project back up.

See `STATUS.md` for exact next steps if you're resuming this on a new machine.
`bin/` (Stockfish + Fairy-Stockfish binaries) and the Python venv are gitignored and
host-specific — they do NOT transfer with `git clone`/`pull`. Re-run `scripts/
m0_setup_gardner_engine.sh` (minichess track) and see `PLAN.md`'s "Ground truth" section
(main track, `uv venv` + dependency recreation) before trusting anything `state.json`
marks `done` on a host it wasn't verified on.

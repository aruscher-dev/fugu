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
  sequencing, constraints.
- **`STATUS.md`** — living status doc: what's done, what's next, host-specific gotchas
  discovered along the way. Read this first when picking the project back up.

See `STATUS.md` for exact next steps if you're resuming this on a new machine.

"""Thin wrapper for loading an open-weight chat model locally (transformers +
bitsandbytes NF4) and generating a reply from a plain user/assistant message
list. Deliberately not vllm (see project plan: avoids destabilizing the
validated torch/transformers/peft/trl versions already installed).
"""
from __future__ import annotations

import os

# Must be set before any huggingface_hub/transformers import -- both cache the
# resolved HF_HOME at import time, so setting this any later is silently
# ignored. HF_HUB_DISABLE_XET turns off the newer "xet" fast-download backend,
# whose native (Rust) cache-path default ignores HF_HOME entirely and writes
# to ~/.cache/huggingface/xet regardless -- that blew through this host's NFS
# home quota during Phase 0.5. Plain HTTP download is slower but reliably
# respects HF_HOME.
os.environ.setdefault("HF_HOME", "/Data/.hf_cache")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from dataclasses import dataclass
from typing import List, Optional

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


@dataclass
class LocalWorkerConfig:
    model_id: str
    load_in_4bit: bool = True
    max_new_tokens: int = 200
    temperature: float = 0.7
    device_map: str = "cuda:0"


class LocalWorker:
    def __init__(self, config: LocalWorkerConfig):
        self.config = config
        quant_config = None
        if config.load_in_4bit:
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        self.tokenizer = AutoTokenizer.from_pretrained(config.model_id)
        self.model = AutoModelForCausalLM.from_pretrained(
            config.model_id,
            quantization_config=quant_config,
            torch_dtype=torch.bfloat16,
            device_map=config.device_map,
        )
        self.model.eval()
        # Required for batched causal-LM generation: right-padding would shift
        # each row's "next token to predict" position relative to its real
        # content, silently corrupting every padded row's output. Most chat
        # models have no dedicated pad token -- eos doubling as pad is the
        # standard workaround (pad_token_id is still passed as eos below, so
        # generation still stops correctly per-row).
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "left"

    def generate_batch(self, batch_messages: List[List[dict]],
                        max_new_tokens: Optional[int] = None) -> List[str]:
        """Batched twin of generate() -- runs len(batch_messages) independent
        single-turn conversations through ONE model.generate() call instead
        of one call per conversation.

        2026-07-12: added because batch-size-1 autoregressive decoding is
        memory-bandwidth-bound, not compute-bound -- each generated token
        re-streams the full model's weights through the GPU regardless of
        how much compute it does with them, so a lone generate() call leaves
        most of a 24GB GPU's compute idle. Batching amortizes that weight
        traffic across many sequences at once, giving genuinely higher
        throughput rather than just spreading the same work over more wall-
        clock time. Only safe to use where the conversations in the batch
        are truly independent of each other (e.g. Phase 3's per-position
        single-turn SFT queries) -- NOT for stepping multiple live games
        forward together, since each ply within a game depends on the
        opponent's actual last move.
        """
        prompts = [
            self.tokenizer.apply_chat_template(m, tokenize=False, add_generation_prompt=True)
            for m in batch_messages
        ]
        inputs = self.tokenizer(prompts, return_tensors="pt", padding=True).to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or self.config.max_new_tokens,
                do_sample=self.config.temperature > 0,
                temperature=max(self.config.temperature, 1e-5),
                pad_token_id=self.tokenizer.pad_token_id,
            )
        prompt_len = inputs["input_ids"].shape[1]  # uniform across the batch -- left-padded
        return [
            self.tokenizer.decode(out[i][prompt_len:], skip_special_tokens=True)
            for i in range(len(batch_messages))
        ]

    def generate(self, messages: List[dict], max_new_tokens: Optional[int] = None) -> str:
        return self.generate_batch([messages], max_new_tokens=max_new_tokens)[0]

    def unload(self):
        del self.model
        torch.cuda.empty_cache()


# Candidate worker pool, keyed by short id -> HF repo id. All confirmed present
# in the shared /Data/.hf_cache (zero download) except where noted.
CANDIDATE_WORKERS = {
    "qwen2.5-7b": "Qwen/Qwen2.5-7B-Instruct",
    "mistral-7b": "mistralai/Mistral-7B-Instruct-v0.3",
    "deepseek-r1-distill-qwen-7b": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek-r1-distill-llama-8b": "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    "hermes-3-llama-3.1-8b": "NousResearch/Hermes-3-Llama-3.1-8B",
    "qwen2.5-3b": "Qwen/Qwen2.5-3B-Instruct",
    # Small swarm tier (per user request) -- not yet confirmed cached, will
    # download on first use (small, checked against disk_guard beforehand).
    "qwen2.5-1.5b": "Qwen/Qwen2.5-1.5B-Instruct",
    "llama-3.2-3b": "meta-llama/Llama-3.2-3B-Instruct",
    "gemma-2-2b": "google/gemma-2-2b-it",
}

# 2026-07-12 fix: these workers emit a <think>...</think> chain-of-thought
# block before their final answer. Every call site (Phase 0.5's floor check,
# Phase 3's SFT collection, the A2A worker agent) was capping max_new_tokens
# at 200-256 -- sized for a plain instruct model's one-line answer -- which
# routinely truncates a reasoning model's generation before it ever reaches
# the final move. That's a generation-budget bug independent of prompt
# wording, and plausibly explains a meaningful share of
# deepseek-r1-distill-qwen-7b's 22% legal-move rate in Phase 0.5's original
# run (reports/phase0_5_summary.json) -- not just "the model doesn't know
# chess." See scaled_max_new_tokens() below.
REASONING_WORKER_IDS = {"deepseek-r1-distill-qwen-7b", "deepseek-r1-distill-llama-8b"}


def scaled_max_new_tokens(worker_id: str, base: int) -> int:
    """Reasoning-distill workers (REASONING_WORKER_IDS) get a much larger
    generation budget than `base` (sized for a plain instruct model's short
    answer) so their <think> block has room to finish before the final move
    -- call this at every LocalWorker.generate() call site instead of
    passing a bare max_new_tokens=<n> literal."""
    return base * 6 if worker_id in REASONING_WORKER_IDS else base

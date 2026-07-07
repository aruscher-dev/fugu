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

    def generate(self, messages: List[dict], max_new_tokens: Optional[int] = None) -> str:
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or self.config.max_new_tokens,
                do_sample=self.config.temperature > 0,
                temperature=max(self.config.temperature, 1e-5),
                pad_token_id=self.tokenizer.eos_token_id,
            )
        new_tokens = out[0][inputs["input_ids"].shape[1]:]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True)

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

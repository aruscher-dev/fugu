#!/usr/bin/env python3
"""Quick human-readable status report: phase state, running tmux sessions,
disk usage. Safe to run any time; makes no changes."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from disk_guard import current_usage  # noqa: E402

state = json.loads((PROJECT_DIR / "state.json").read_text())

print("=== Open-Fugu status ===")
print(f"host: {state.get('host')}")
print(f"last orchestrate run: {state.get('last_orchestrate_run')}")
print()
for phase_id in state["phase_order"]:
    phase = state["phases"][phase_id]
    print(f"  phase {phase_id:>4}: {phase['status']:<12} {phase.get('note', '')}")

print()
sessions = subprocess.run(["tmux", "ls"], capture_output=True, text=True)
print("tmux sessions:")
print(sessions.stdout.strip() or "  (none)")

print()
usage = current_usage()
print(f"disk usage: project={usage['project_bytes']/1024**3:.1f}GB, "
      f"new-HF-downloads={usage['hf_new_bytes']/1024**3:.1f}GB, "
      f"total={usage['total_bytes']/1024**3:.1f}GB (cap 100GB)")

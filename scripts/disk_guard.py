#!/usr/bin/env python3
"""Disk usage guard for the Open-Fugu project.

Tracks two things against a combined cap (default 100GB, warn at 80GB):
  1. Everything under ~/open_fugu itself (logs, checkpoints, SFT data, vendored code).
  2. The *incremental* growth of the shared HF cache (/Data/.hf_cache) caused by this
     project's own new model downloads, measured against a manifest of what was already
     cached before this project started (the pre-existing ~148GB cache is NOT counted).

Usage:
    from disk_guard import check_disk_budget
    check_disk_budget()                      # raise/warn based on current usage
    check_disk_budget(pending_bytes=5_000_000_000)  # also account for an imminent write

Run directly for a human-readable report:
    python scripts/disk_guard.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
HF_CACHE = Path(os.environ.get("HF_HOME", "/Data/.hf_cache"))
MANIFEST_PATH = PROJECT_DIR / "config" / "hf_cache_manifest_baseline.json"

CAP_BYTES = 100 * 1024**3   # 100GB hard cap
WARN_BYTES = 80 * 1024**3   # 80GB warn threshold


class DiskBudgetExceeded(RuntimeError):
    pass


def _du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    out = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _list_model_dirs(cache_root: Path) -> dict:
    """Map of 'models--org--name' -> size in bytes, for everything under hub/."""
    hub = cache_root / "hub"
    sizes = {}
    if not hub.exists():
        return sizes
    for entry in hub.iterdir():
        if entry.is_dir() and entry.name.startswith("models--"):
            sizes[entry.name] = _du_bytes(entry)
    return sizes


def write_baseline_manifest():
    """Call once, at Phase 0, BEFORE any new model downloads for this project."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = _list_model_dirs(HF_CACHE)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    os.chmod(MANIFEST_PATH, 0o600)
    print(f"Baseline HF cache manifest written: {len(manifest)} model dirs, "
          f"{sum(manifest.values()) / 1024**3:.1f}GB")
    return manifest


def _project_new_hf_bytes() -> int:
    """Bytes added to the HF cache since the baseline manifest was taken."""
    if not MANIFEST_PATH.exists():
        # No baseline yet -- treat everything as pre-existing (don't over-count).
        return 0
    baseline = json.loads(MANIFEST_PATH.read_text())
    current = _list_model_dirs(HF_CACHE)
    new_bytes = 0
    for name, size in current.items():
        if name not in baseline:
            new_bytes += size
        elif size > baseline[name]:
            new_bytes += size - baseline[name]
    return new_bytes


def current_usage() -> dict:
    project_bytes = _du_bytes(PROJECT_DIR)
    hf_new_bytes = _project_new_hf_bytes()
    return {
        "project_bytes": project_bytes,
        "hf_new_bytes": hf_new_bytes,
        "total_bytes": project_bytes + hf_new_bytes,
    }


def check_disk_budget(pending_bytes: int = 0, cap_bytes: int = CAP_BYTES,
                       warn_bytes: int = WARN_BYTES) -> dict:
    usage = current_usage()
    projected = usage["total_bytes"] + pending_bytes

    if projected >= cap_bytes:
        raise DiskBudgetExceeded(
            f"Disk budget EXCEEDED: current {usage['total_bytes']/1024**3:.1f}GB "
            f"(+{pending_bytes/1024**3:.1f}GB pending) would reach "
            f"{projected/1024**3:.1f}GB, over the {cap_bytes/1024**3:.0f}GB cap. "
            f"Refusing to proceed automatically -- confirm with the user before "
            f"continuing, or free up space / raise the cap explicitly."
        )
    if projected >= warn_bytes:
        print(
            f"[disk_guard] WARNING: usage at {projected/1024**3:.1f}GB of "
            f"{cap_bytes/1024**3:.0f}GB cap (project={usage['project_bytes']/1024**3:.1f}GB, "
            f"new-HF-downloads={usage['hf_new_bytes']/1024**3:.1f}GB).",
            file=sys.stderr,
        )
    return usage


if __name__ == "__main__":
    if not MANIFEST_PATH.exists():
        print("No baseline manifest yet -- run write_baseline_manifest() during Phase 0 first.")
    usage = current_usage()
    print(json.dumps({k: f"{v/1024**3:.2f}GB" for k, v in usage.items()}, indent=2))
    try:
        check_disk_budget()
        print("OK: within budget.")
    except DiskBudgetExceeded as e:
        print(f"OVER BUDGET: {e}")
        sys.exit(1)

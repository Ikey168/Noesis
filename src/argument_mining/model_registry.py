"""
Pinned pretrained model registry (#959).

The pretrained backends (zero-shot NLI for stance/frames/claim-links, the
ClaimBuster-style claim detector) are pinned **by name and revision in one
place** — this module — and fetched by ``python3 -m
src.argument_mining.fetch_models`` (``make models``). The fetch resolves
each pin to an immutable commit and records it, with the benchmark
provenance fields, in ``models/pins.lock.json``; a silently drifted
upstream model is the pretrained-world equivalent of a corrupted
checkpoint, so :func:`verify_pins` reports any cache/lock divergence and
the fetch CLI surfaces it loudly.

Env overrides (``NOESIS_NLI_MODEL``, ``NOESIS_CLAIM_MODEL``) are honoured
so a user can evaluate an alternative model; the lock file then records
what was actually fetched.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = _REPO_ROOT / "models" / "pins.lock.json"

#: the single source of truth for which pretrained models the backends use.
#: revision "main" means "not yet frozen"; the first fetch resolves it to an
#: immutable commit in the lock file, which then acts as the operative pin.
PINS: Dict[str, Dict[str, Any]] = {
    "nli": {
        "env": "NOESIS_NLI_MODEL",
        "default": "cross-encoder/nli-deberta-v3-base",
        "revision": "main",
        "serves": ["stance (#954)", "frames (#955)", "claim links (#964)"],
    },
    "claim": {
        "env": "NOESIS_CLAIM_MODEL",
        "default": "Nithiwat/mdeberta-v3-base_claimbuster",
        "revision": "main",
        "serves": ["claim detection (#956)"],
    },
}


def resolved_pins() -> Dict[str, Dict[str, Any]]:
    """The pins with env overrides applied."""
    resolved = {}
    for key, pin in PINS.items():
        resolved[key] = {
            **pin,
            "model": os.environ.get(pin["env"], pin["default"]),
        }
    return resolved


def read_lock(path: Optional[Path] = None) -> Dict[str, Any]:
    lock_path = Path(path or LOCK_PATH)
    if not lock_path.exists():
        return {}
    try:
        return json.loads(lock_path.read_text())
    except ValueError:
        return {}


def write_lock(entries: Dict[str, Any], path: Optional[Path] = None) -> Path:
    lock_path = Path(path or LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(entries, indent=2, sort_keys=True))
    return lock_path


def verify_pins(path: Optional[Path] = None) -> List[str]:
    """Divergences between the operative pins and the lock file.

    Returns human-readable warnings: a pinned model that was never fetched,
    or a lock entry whose model name no longer matches the pin (drift after
    an env change or a registry edit). Empty list = consistent.
    """
    lock = read_lock(path)
    warnings: List[str] = []
    for key, pin in resolved_pins().items():
        entry = lock.get(key)
        if entry is None:
            warnings.append(
                f"{key}: pinned model {pin['model']!r} has not been fetched"
                " (run `make models`)"
            )
            continue
        if entry.get("model") != pin["model"]:
            warnings.append(
                f"{key}: lock has {entry.get('model')!r} but the operative pin"
                f" is {pin['model']!r} — refetch to update the lock"
            )
    return warnings


def backend_status() -> Dict[str, str]:
    """Active prediction mode per wrapper — the startup log line's data.

    Instantiates the wrappers (cheap when no model loads) and reports what
    each would actually use right now, so silently degraded installs stop
    happening.
    """
    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.models import ClaimDetector, StanceClassifier

    return {
        "claims": ClaimDetector().prediction_mode,
        "stance": StanceClassifier().prediction_mode,
        "frames": FrameClassifier().prediction_mode,
    }


def fetch_models(
    downloader: Optional[Any] = None,
    lock_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch every pinned model into the local cache and write the lock.

    ``downloader(model, revision) -> {"revision": <resolved>, "path": <dir>}``
    is injectable for tests; the default uses ``huggingface_hub``'s
    ``snapshot_download`` (idempotent and resumable — an interrupted fetch
    re-run completes the missing files).
    """
    if downloader is None:
        def downloader(model: str, revision: str) -> Dict[str, str]:
            from huggingface_hub import snapshot_download

            path = snapshot_download(repo_id=model, revision=revision)
            resolved = Path(path).name  # snapshots/<commit-sha>
            return {"revision": resolved, "path": str(path)}

    lock = read_lock(lock_path)
    summary: Dict[str, Any] = {"fetched": [], "failed": []}
    for key, pin in resolved_pins().items():
        try:
            result = downloader(pin["model"], pin["revision"])
        except Exception as exc:  # noqa: BLE001 - CLI boundary, keep going
            summary["failed"].append({"backend": key, "model": pin["model"],
                                      "error": str(exc)})
            continue
        lock[key] = {
            "model": pin["model"],
            "requested_revision": pin["revision"],
            "resolved_revision": result["revision"],
            "path": result.get("path"),
            "serves": pin["serves"],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        summary["fetched"].append({"backend": key, "model": pin["model"],
                                   "revision": result["revision"]})
    summary["lock_path"] = str(write_lock(lock, lock_path))
    summary["warnings"] = verify_pins(lock_path)
    return summary

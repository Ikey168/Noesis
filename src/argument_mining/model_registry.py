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
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = _REPO_ROOT / "models" / "pins.lock.json"

#: The single source of truth for which pretrained models the backends use.
#: Revisions are immutable Hugging Face commit ids. Updating a model is a
#: deliberate registry + lock-file change; mutable branches are never pins.
PINS: Dict[str, Dict[str, Any]] = {
    "nli": {
        "env": "NOESIS_NLI_MODEL",
        "default": "cross-encoder/nli-deberta-v3-base",
        "revision": "6c749ce3425cd33b46d187e45b92bbf96ee12ec7",
        "serves": ["stance (#954)", "frames (#955)", "claim links (#964)"],
    },
    "claim": {
        "env": "NOESIS_CLAIM_MODEL",
        "default": "Nithiwat/mdeberta-v3-base_claimbuster",
        "revision": "f0d23ebd02e98325f19419eee10637f9167f8a47",
        "serves": ["claim detection (#956)"],
    },
}

_TOKENIZER_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "spm.model",
    "sentencepiece.bpe.model",
    "vocab.*",
    "merges.txt",
]


def inference_files(kind: str) -> List[str]:
    """Repository files needed for the pinned PyTorch inference backend.

    Some upstream model repositories also publish several multi-hundred-MB
    ONNX exports and duplicate framework checkpoints. Fetching the complete
    snapshot makes ``make models`` download gigabytes that Noesis never opens.
    The pin identifies which single weight format the selected backend ships.
    """
    if kind == "nli":
        return [*_TOKENIZER_FILES, "model.safetensors"]
    if kind == "claim":
        return [*_TOKENIZER_FILES, "pytorch_model.bin"]
    raise KeyError(f"unknown model backend {kind!r}")


def _has_inference_weights(kind: str, snapshot: Path) -> bool:
    expected = "model.safetensors" if kind == "nli" else "pytorch_model.bin"
    weight = snapshot / expected
    return weight.is_file() and weight.stat().st_size > 0


def resolved_pins() -> Dict[str, Dict[str, Any]]:
    """The pins with env overrides applied."""
    resolved = {}
    for key, pin in PINS.items():
        resolved[key] = {
            **pin,
            "model": os.environ.get(pin["env"], pin["default"]),
            "revision": os.environ.get(f"{pin['env']}_REVISION", pin["revision"]),
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


def verify_pins(path: Optional[Path] = None, *, require_cache: bool = False) -> List[str]:
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
            continue
        if entry.get("requested_revision") != pin["revision"]:
            warnings.append(
                f"{key}: lock revision {entry.get('requested_revision')!r} does not"
                f" match registry revision {pin['revision']!r} — refetch"
            )
            continue
        resolved = str(entry.get("resolved_revision", ""))
        if len(resolved) != 40 or any(c not in "0123456789abcdef" for c in resolved):
            warnings.append(f"{key}: lock does not contain an immutable commit revision")
            continue
        if require_cache and cached_model_path(key, path=path) is None:
            warnings.append(f"{key}: lock is valid but weights are absent from the local cache")
    return warnings


def cached_model_path(kind: str, *, path: Optional[Path] = None) -> Optional[Path]:
    """Return a verified local model snapshot without performing network I/O."""
    pin = resolved_pins().get(kind)
    entry = read_lock(path).get(kind)
    if not pin or not entry or entry.get("model") != pin["model"]:
        return None
    if entry.get("requested_revision") != pin["revision"]:
        return None
    revision = entry.get("resolved_revision")
    if not revision:
        return None
    try:
        from huggingface_hub import snapshot_download

        snapshot = snapshot_download(
            repo_id=pin["model"], revision=revision, local_files_only=True,
            allow_patterns=inference_files(kind),
        )
    except Exception:
        return None
    snapshot_path = Path(snapshot)
    return snapshot_path if _has_inference_weights(kind, snapshot_path) else None


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
    default_downloader = downloader is None

    lock = read_lock(lock_path)
    summary: Dict[str, Any] = {"fetched": [], "failed": []}
    for key, pin in resolved_pins().items():
        try:
            if default_downloader:
                from huggingface_hub import snapshot_download

                downloaded = snapshot_download(
                    repo_id=pin["model"], revision=pin["revision"],
                    allow_patterns=inference_files(key),
                )
                downloaded_path = Path(downloaded)
                if not _has_inference_weights(key, downloaded_path):
                    raise OSError(
                        f"{key}: download completed without its required weight file"
                    )
                result = {
                    "revision": downloaded_path.name,
                    "path": str(downloaded_path),
                }
            else:
                assert downloader is not None
                result = downloader(pin["model"], pin["revision"])
        except Exception as exc:  # noqa: BLE001 - CLI boundary, keep going
            summary["failed"].append({"backend": key, "model": pin["model"],
                                      "error": str(exc)})
            continue
        lock[key] = {
            "model": pin["model"],
            "requested_revision": pin["revision"],
            "resolved_revision": result["revision"],
            "serves": pin["serves"],
        }
        summary["fetched"].append({"backend": key, "model": pin["model"],
                                   "revision": result["revision"]})
    summary["lock_path"] = str(write_lock(lock, lock_path))
    summary["warnings"] = verify_pins(lock_path)
    return summary

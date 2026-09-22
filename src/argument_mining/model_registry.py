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
    from src.argument_mining.frames import get_frame_classifier
    from src.argument_mining.models import get_claim_detector, get_stance_classifier

    return {
        "claims": get_claim_detector().prediction_mode,
        "stance": get_stance_classifier().prediction_mode,
        "frames": get_frame_classifier().prediction_mode,
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


# Evaluation candidates are opt-in; production PINS and default fetching are unchanged.
# Immutable revisions resolved from upstream metadata on 2026-09-07.
OPTIONAL_PINS = {
    "minilm-reranker": ("cross-encoder/ms-marco-MiniLM-L6-v2", "233902d25c440f23af6f7d6e94d2946bac0bee0a", "apache-2.0"),
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41", "apache-2.0"),
    "align-en": ("facebook/wav2vec2-base-960h", "22aad52d435eb6dbaf354bdad9b0da84ce7d6156", "apache-2.0"),
    "align-de": ("jonatasgrosman/wav2vec2-large-xlsr-53-german", "4b8a02957378d0f2da2ef74091156b032c485a89", "apache-2.0"),
    "paddle-det": ("PaddlePaddle/PP-OCRv5_mobile_det", "0d63e78e2b680928f6b1747d76a08db6e645efb7", "apache-2.0"),
    "paddle-latin": ("PaddlePaddle/latin_PP-OCRv5_mobile_rec", "ab2cd5cc5fa6309be2e5acdfe66eca2c2c127d57", "apache-2.0"),
    "e5": ("intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3", "mit"),
    "bge-m3": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181", "mit"),
    "qwen3-reranker": ("Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473", "apache-2.0"),
    "mdeberta": ("MoritzLaurer/mDeBERTa-v3-base-mnli-xnli", "8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c", "mit"),
    "gliner2": ("fastino/gliner2-multi-v1", "c6296e25603e4d31f68ef8a9f4edb73421d1e45a", "apache-2.0"),
    "lightonocr": ("lightonai/LightOnOCR-2-1B", "c97bd377f04481830395218fa8951df9deaba756", "apache-2.0"),
    "sat": ("segment-any-text/sat-3l-sm", "137da054051ad9f1eac42025f758db4ac9f22535", "mit"),
    "proposal": ("HuggingFaceTB/SmolLM2-135M-Instruct", "12fd25f77366fa6b3b4b768ec3050bf629380bac", "apache-2.0"),
}


def optional_model_spec(kind: str) -> dict:
    """An immutable optional pin; no environment-mutable branches."""
    model, revision, license_id = OPTIONAL_PINS[kind]
    return {"kind": kind, "model": model, "revision": revision, "license": license_id}


def optional_model_path(kind: str, *, download: bool = False, max_download_bytes: int = 4_000_000_000):
    """Resolve cached weights, or explicitly fetch under a previewed byte ceiling."""
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        if download:
            raise
        return None
    spec = optional_model_spec(kind)
    patterns = ["config.json", "*token*.json", "*processor*.json", "vocab.json", "vocab.txt", "merges.txt",
                "*.safetensors", "pytorch_model*.bin", "*.model", "1_Pooling/config.json",
                "encoder_config/config.json", "modules.json", "config_sentence_transformers.json",
                "sentence_bert_config.json", "colbert_linear.pt", "sparse_linear.pt",
                "inference.pdiparams", "inference.json", "inference.yml"]
    if download:
        from fnmatch import fnmatch
        info = HfApi().model_info(spec["model"], revision=spec["revision"], files_metadata=True)
        sizes = [f.size for f in info.siblings if any(fnmatch(f.rfilename, p) for p in patterns)]
        if not sizes or any(size is None for size in sizes) or sum(sizes) > max_download_bytes:
            raise ValueError("model download exceeds its explicit byte ceiling or has unknown sizes")
    try:
        path = Path(snapshot_download(repo_id=spec["model"], revision=spec["revision"],
                                      local_files_only=not download, allow_patterns=patterns))
    except Exception:
        if download:
            raise
        return None
    if path.name != spec["revision"] or not any(path.glob("*.safetensors")) and not any(path.glob("pytorch_model*.bin")) and not (path / "inference.pdiparams").is_file():
        return None
    if download:
        lock = read_lock()
        lock["optional:" + kind] = {"model": spec["model"], "requested_revision": spec["revision"],
                                    "resolved_revision": spec["revision"], "serves": ["opt-in evaluation"]}
        write_lock(lock)
    return path

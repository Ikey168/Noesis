"""
Noesis Argument-Mining Dataset Inspector — MCP server.

Token-efficient tools for inspecting the labelled argument-mining dataset
produced by issue #109 (`scripts/build_am_dataset.py`).

Tools:
  get_stats()                          -> dataset statistics from stats.json (no Parquet I/O)
  get_schema()                         -> label schema documentation from schema.md
  label_distribution(task, split?)     -> label counts + percentages for claims/stance/frames
  sample_examples(task, n?, split?,    -> sample N examples with optional label / source_type
                  source_type?,           filter — returns compact summaries, not full rows
                  label?)
  check_criteria()                     -> re-verify all acceptance criteria live from the files

Design constraints (shared with other MCP servers in this repo):
  * Lazy Parquet imports inside tools. Top-level imports are stdlib + fastmcp only.
  * Always return summaries (capped at MAX_ROWS). Never return full Parquet payloads.
  * stats.json is read directly for aggregate queries — avoids loading any Parquet file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("noesis-dataset")

DATA_DIR = REPO_ROOT / "data" / "argument_mining"
MAX_ROWS = 20  # Never return more than this many example rows in one call

_VALID_TASKS = ("claims", "stance", "frames")
_VALID_SPLITS = ("train", "val", "test")
_VALID_SOURCE_TYPES = ("news", "blog", "paper", "transcript", "book", "note")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stats() -> dict:
    stats_path = DATA_DIR / "stats.json"
    if not stats_path.exists():
        return {"error": f"stats.json not found at {DATA_DIR}. Run scripts/build_am_dataset.py first."}
    return json.loads(stats_path.read_text())


def _load_parquet(task: str, columns: Optional[list] = None):
    """Lazy Parquet load — raises ValueError for unknown task."""
    import pandas as pd
    parquet_path = DATA_DIR / f"{task}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{task}.parquet not found at {DATA_DIR}. Run scripts/build_am_dataset.py first."
        )
    return pd.read_parquet(parquet_path, columns=columns)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_stats() -> dict:
    """Return dataset statistics from stats.json.

    Instant — reads a small JSON file, never loads Parquet.
    Returns total count, per-source-type breakdown, split sizes, label
    distributions, the explicitly simulated noise diagnostic, and criteria.
    """
    return _stats()


@mcp.tool
def get_schema() -> str:
    """Return the full label schema documentation from schema.md.

    Covers column definitions, label enumerations, split ratios,
    class distribution notes, and the acceptance criteria.
    """
    schema_path = DATA_DIR / "schema.md"
    if not schema_path.exists():
        return f"schema.md not found at {DATA_DIR}."
    return schema_path.read_text()


@mcp.tool
def label_distribution(
    task: str,
    split: Optional[str] = None,
) -> dict[str, Any]:
    """Return label counts and percentages for a given task.

    Args:
        task:  One of "claims", "stance", "frames".
        split: Optional filter — "train", "val", or "test". Omit for all splits.

    Returns a compact dict with counts, percentages, and total rows examined.
    Reads only the label and split columns from Parquet — minimal I/O.
    """
    if task not in _VALID_TASKS:
        return {"error": f"Unknown task '{task}'. Choose from: {_VALID_TASKS}"}
    if split and split not in _VALID_SPLITS:
        return {"error": f"Unknown split '{split}'. Choose from: {_VALID_SPLITS}"}

    label_col = {"claims": "is_claim", "stance": "stance", "frames": "frames"}.get(task)

    try:
        cols = [label_col, "split"] if label_col != "frames" else ["frames", "split"]
        df = _load_parquet(task, columns=cols)
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    if split:
        df = df[df["split"] == split]

    total = len(df)
    if total == 0:
        return {"task": task, "split": split, "total": 0, "distribution": {}}

    if task == "frames":
        # Frames are JSON lists — count individual frame occurrences
        import json as _json
        frame_counts: dict[str, int] = {}
        for val in df["frames"]:
            labels = _json.loads(val) if isinstance(val, str) else list(val)
            for lbl in labels:
                frame_counts[lbl] = frame_counts.get(lbl, 0) + 1
        dist = {
            lbl: {"count": cnt, "pct": round(cnt / total * 100, 1)}
            for lbl, cnt in sorted(frame_counts.items(), key=lambda x: -x[1])
        }
    else:
        counts = df[label_col].value_counts().to_dict()
        dist = {
            str(lbl): {"count": int(cnt), "pct": round(int(cnt) / total * 100, 1)}
            for lbl, cnt in sorted(counts.items(), key=lambda x: -x[1])
        }

    return {
        "task": task,
        "split": split or "all",
        "total_rows": total,
        "distribution": dist,
    }


@mcp.tool
def sample_examples(
    task: str,
    n: int = 5,
    split: Optional[str] = None,
    source_type: Optional[str] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Sample N examples from a task dataset with optional filters.

    Args:
        task:        One of "claims", "stance", "frames".
        n:           Number of examples to return (max 20).
        split:       Optional filter — "train", "val", or "test".
        source_type: Optional content-type filter (news/blog/paper/transcript/book/note).
        label:       Optional label filter.
                     - claims: "0" or "1"
                     - stance: "supportive" / "critical" / "neutral" / "ambiguous"
                     - frames: frame name — returns examples that include that frame

    Returns compact example summaries (id, source_type, text preview, label).
    Text is truncated to 120 characters to keep responses concise.
    """
    if task not in _VALID_TASKS:
        return {"error": f"Unknown task '{task}'. Choose from: {_VALID_TASKS}"}
    if split and split not in _VALID_SPLITS:
        return {"error": f"Unknown split '{split}'. Choose from: {_VALID_SPLITS}"}
    if source_type and source_type not in _VALID_SOURCE_TYPES:
        return {"error": f"Unknown source_type '{source_type}'. Choose from: {_VALID_SOURCE_TYPES}"}

    n = min(n, MAX_ROWS)

    try:
        df = _load_parquet(task)
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    # Apply filters
    if split:
        df = df[df["split"] == split]
    if source_type:
        df = df[df["source_type"] == source_type]
    if label:
        if task == "claims":
            df = df[df["is_claim"] == int(label)]
        elif task == "stance":
            df = df[df["stance"] == label]
        elif task == "frames":
            import json as _json
            mask = df["frames"].apply(
                lambda v: label in (_json.loads(v) if isinstance(v, str) else list(v))
            )
            df = df[mask]

    total_matching = len(df)
    if total_matching == 0:
        return {"task": task, "filters": {"split": split, "source_type": source_type, "label": label},
                "total_matching": 0, "examples": []}

    sample = df.sample(min(n, total_matching), random_state=42)

    label_col = {"claims": "is_claim", "stance": "stance", "frames": "frames"}.get(task)

    examples = []
    for _, row in sample.iterrows():
        entry: dict[str, Any] = {
            "id": row["id"],
            "source_type": row["source_type"],
            "text": row["text"][:120] + ("…" if len(row["text"]) > 120 else ""),
        }
        if label_col:
            entry["label"] = row[label_col]
        if task == "stance":
            entry["topic"] = row.get("topic", "")
        examples.append(entry)

    return {
        "task": task,
        "filters": {k: v for k, v in
                    {"split": split, "source_type": source_type, "label": label}.items()
                    if v is not None},
        "total_matching": total_matching,
        "returned": len(examples),
        "examples": examples,
    }


@mcp.tool
def check_criteria() -> dict[str, Any]:
    """Re-verify all issue-#109 acceptance criteria live from the Parquet files.

    Re-reads the actual Parquet files to compute counts and reports the
    generated-label noise diagnostic separately from human evaluation.
    Returns each criterion with its measured value and pass/fail status.
    """
    try:
        claims_df = _load_parquet("claims", columns=["source_type", "split"])
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    total = len(claims_df)
    by_type = claims_df.groupby("source_type").size().to_dict()
    by_split = claims_df.groupby("split").size().to_dict()

    noise_path = DATA_DIR / "noise_robustness_subset.parquet"
    noise: dict[str, Any] = {
        "method": "generated labels compared with random perturbations; not human IAA"
    }
    if noise_path.exists():
        import pandas as pd
        from sklearn.metrics import cohen_kappa_score
        noise_df = pd.read_parquet(noise_path)
        noise["n"] = len(noise_df)
        if "generated_claim" in noise_df.columns:
            noise["similarity_kappa_claim"] = round(
                float(cohen_kappa_score(noise_df["generated_claim"], noise_df["perturbed_claim"])), 4
            )
        if "generated_stance" in noise_df.columns:
            noise["similarity_kappa_stance"] = round(
                float(cohen_kappa_score(noise_df["generated_stance"], noise_df["perturbed_stance"])), 4
            )
    else:
        noise["error"] = "noise_robustness_subset.parquet not found"

    return {
        "total_examples": {"value": total, "pass": total >= 5000},
        "by_source_type": {
            k: {"count": int(v), "pass": int(v) >= 500}
            for k, v in sorted(by_type.items())
        },
        "split_distribution": {k: int(v) for k, v in by_split.items()},
        "simulated_noise_robustness": noise,
        "human_evaluation": _stats().get("human_evaluation", {
            "status": "not_collected",
            "note": "Use scripts/human_annotation.py; never substitute simulation for annotators.",
        }),
    }


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

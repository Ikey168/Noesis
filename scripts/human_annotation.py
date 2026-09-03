#!/usr/bin/env python3
"""Prepare and validate a genuinely human-labelled argument-mining set.

This workflow never invents agreement. ``export`` samples sentences from the
unified, ingested ``documents`` table and creates two independently shuffled,
blinded assignment files. ``finalize`` accepts completed files, checks that
two distinct annotator ids labelled the same items, computes real agreement,
and publishes gold rows only when every disagreement has an adjudication.

Examples::

    python scripts/human_annotation.py export --db data/noesis.duckdb --size 750
    python scripts/human_annotation.py finalize \
      --a data/argument_mining/human_eval/annotator_a.csv \
      --b data/argument_mining/human_eval/annotator_b.csv \
      --adjudication data/argument_mining/human_eval/adjudication.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SOURCE_TYPES = ("news", "blog", "paper", "transcript", "book", "note")
STANCE = ("supportive", "critical", "neutral", "ambiguous")


def _is_example_url(url: str) -> bool:
    """Return true only for reserved example hosts, not substring matches."""
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    reserved = ("example", "example.com")
    return any(hostname == host or hostname.endswith(f".{host}") for host in reserved)
FRAMES = ("economic", "security", "humanitarian", "legal", "political", "scientific", "other")
FIELDS = (
    "item_id", "document_id", "source_type", "source_url", "text",
    "annotator_id", "is_claim", "stance", "frames", "notes",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def export_assignments(db_path: Path, out: Path, size: int, seed: int) -> dict:
    """Export a source-stratified sample from actual ingested documents."""
    import duckdb

    from src.argument_mining.dataset import sentences_from_document
    from services.ingest.common.document_model import Document

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        cols = [d[0] for d in conn.execute("DESCRIBE documents").fetchall()]
        rows = conn.execute(
            "SELECT * FROM documents WHERE content IS NOT NULL AND length(trim(content)) > 0"
        ).fetchall()
    finally:
        conn.close()

    candidates: dict[str, list[dict[str, str]]] = {kind: [] for kind in SOURCE_TYPES}
    for values in rows:
        row = dict(zip(cols, values))
        url = str(row.get("url") or "")
        doc_id = str(row.get("document_id") or "")
        # The repository seed corpus and test fixtures are not real evaluation
        # material. A human may still annotate private documents with no URL.
        if _is_example_url(url) or doc_id.startswith("art-"):
            continue
        source_type = row.get("source_type")
        if source_type not in candidates:
            continue
        doc = Document(
            document_id=doc_id,
            source_type=source_type,
            language=row.get("language") or "en",
            ingested_at=int(row.get("ingested_at") or 0),
            url=row.get("url"), title=row.get("title"), content=row.get("content"),
        )
        for index, sentence in enumerate(sentences_from_document(doc)):
            if 25 <= len(sentence) <= 1000:
                item_id = hashlib.sha256(f"{doc_id}:{index}:{sentence}".encode()).hexdigest()[:20]
                candidates[source_type].append({
                    "item_id": item_id, "document_id": doc_id,
                    "source_type": source_type, "source_url": url, "text": sentence,
                    "annotator_id": "", "is_claim": "", "stance": "",
                    "frames": "", "notes": "",
                })

    rng = random.Random(seed)
    selected: list[dict[str, str]] = []
    target_each = max(1, size // len(SOURCE_TYPES))
    for source_type in SOURCE_TYPES:
        rng.shuffle(candidates[source_type])
        selected.extend(candidates[source_type][:target_each])
    remaining = [row for kind in SOURCE_TYPES for row in candidates[kind] if row not in selected]
    rng.shuffle(remaining)
    selected.extend(remaining[: max(0, size - len(selected))])
    if len(selected) < size:
        raise ValueError(
            f"only {len(selected)} eligible real sentences found; requested {size}. "
            "Ingest more material or explicitly choose a smaller pilot."
        )

    a_rows = list(selected)
    b_rows = list(selected)
    random.Random(seed + 1).shuffle(a_rows)
    random.Random(seed + 2).shuffle(b_rows)
    a_path, b_path = out / "annotator_a.csv", out / "annotator_b.csv"
    _write_csv(a_path, a_rows)
    _write_csv(b_path, b_rows)
    manifest = {
        "status": "awaiting_two_independent_annotators",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "requested_size": size,
        "n_items": len(selected),
        "per_source_type": dict(Counter(r["source_type"] for r in selected)),
        "source_database": str(db_path),
        "assignments": {
            "annotator_a.csv": _sha256(a_path), "annotator_b.csv": _sha256(b_path)
        },
        "synthetic_data": False,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _read_completed(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    ids = {r.get("annotator_id", "").strip() for r in rows}
    if len(ids) != 1 or not next(iter(ids), ""):
        raise ValueError(f"{path}: every row must carry one non-empty annotator_id")
    annotator = next(iter(ids))
    by_item: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("is_claim") not in {"0", "1"}:
            raise ValueError(f"{path}: {row.get('item_id')} has invalid is_claim")
        if row.get("stance") not in STANCE:
            raise ValueError(f"{path}: {row.get('item_id')} has invalid stance")
        frames = {x.strip() for x in row.get("frames", "").split("|") if x.strip()}
        if not frames or not frames.issubset(FRAMES):
            raise ValueError(f"{path}: {row.get('item_id')} has invalid frames")
        by_item[row["item_id"]] = row
    return annotator, by_item


def _kappa(a: list[Any], b: list[Any]) -> float:
    from sklearn.metrics import cohen_kappa_score

    return round(float(cohen_kappa_score(a, b)), 4)


def finalize(a_path: Path, b_path: Path, adjudication: Path | None, out: Path) -> dict:
    """Validate completed assignments and, after adjudication, emit human gold."""
    annotator_a, a = _read_completed(a_path)
    annotator_b, b = _read_completed(b_path)
    if annotator_a == annotator_b:
        raise ValueError("annotator_id values must identify two different people")
    if set(a) != set(b):
        raise ValueError("assignment files do not contain exactly the same item ids")

    ids = sorted(a)
    report: dict[str, Any] = {
        "status": "needs_adjudication",
        "n_examples": len(ids),
        "annotators": [annotator_a, annotator_b],
        "kappa_claim": _kappa([a[i]["is_claim"] for i in ids], [b[i]["is_claim"] for i in ids]),
        "kappa_stance": _kappa([a[i]["stance"] for i in ids], [b[i]["stance"] for i in ids]),
        "kappa_frames_binary_macro": round(sum(
            _kappa(
                [str(int(frame in a[i]["frames"].split("|"))) for i in ids],
                [str(int(frame in b[i]["frames"].split("|"))) for i in ids],
            ) for frame in FRAMES
        ) / len(FRAMES), 4),
        "disagreements": sum(
            1 for i in ids if any(a[i][field] != b[i][field]
                                  for field in ("is_claim", "stance", "frames"))
        ),
        "synthetic_data": False,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }

    if adjudication is not None:
        _adjudicator, decisions = _read_completed(adjudication)
        if set(decisions) != set(ids):
            raise ValueError("adjudication must contain every assignment item")
        import pandas as pd

        gold = []
        for item_id in ids:
            row = decisions[item_id]
            gold.append({
                "id": item_id, "document_id": row["document_id"],
                "source_type": row["source_type"], "source_url": row["source_url"],
                "text": row["text"], "is_claim": int(row["is_claim"]),
                "stance": row["stance"], "frames": row["frames"],
            })
        out.mkdir(parents=True, exist_ok=True)
        gold_path = out / "human_gold.parquet"
        pd.DataFrame(gold).to_parquet(gold_path, index=False)
        report.update(status="complete", gold_sha256=_sha256(gold_path))

    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--out", type=Path, default=REPO / "data/argument_mining/human_eval")
    export.add_argument("--size", type=int, default=750, choices=range(500, 1001))
    export.add_argument("--seed", type=int, default=2026)
    finish = sub.add_parser("finalize")
    finish.add_argument("--a", type=Path, required=True)
    finish.add_argument("--b", type=Path, required=True)
    finish.add_argument("--adjudication", type=Path)
    finish.add_argument("--out", type=Path, default=REPO / "data/argument_mining/human_eval")
    args = parser.parse_args()
    try:
        result = (export_assignments(args.db, args.out, args.size, args.seed)
                  if args.command == "export" else
                  finalize(args.a, args.b, args.adjudication, args.out))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

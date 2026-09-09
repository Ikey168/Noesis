"""Exact directed relation evaluation from published CrossRE token annotations."""

import hashlib
import json
from pathlib import Path

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "tests/fixtures/published_ner/relations-manifest.json"
)


def score_relations(predicted, expected):
    def key(row):
        endpoints = []
        for side in ("head", "tail"):
            span = row[side]
            if (
                type(span["start"]) is not int
                or type(span["end"]) is not int
                or not 0 <= span["start"] < span["end"]
            ):
                raise ValueError("exact relation endpoints required")
            endpoints.extend((span["start"], span["end"]))
        return (row.get("source_id"), row["label"], *endpoints)

    pred, gold = {key(r) for r in predicted}, {key(r) for r in expected}
    tp = len(pred & gold)
    precision, recall = tp / len(pred) if pred else 0, tp / len(gold) if gold else 0
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall)
        if precision + recall
        else 0,
        "correct": tp,
        "predicted": len(pred),
        "expected": len(gold),
    }


def prepare(path):
    spec = json.loads(MANIFEST.read_text())
    if path.stat().st_size != spec["size_bytes"]:
        raise ValueError("published relation size mismatch")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
        raise ValueError("published relation digest mismatch")
    records = [json.loads(line) for line in raw.decode().splitlines()]
    labels = sorted({r[4] for row in records for r in row["relations"]})
    schema = {
        label.replace("-", "_"): label.replace("-", " ")
        + " relation from the first named entity to the second"
        for label in labels
    }
    rows = []
    for row in records:
        tokens, relations = row["sentence"], row["relations"]
        if not relations or len(tokens) > 40 or any(any(r[6:]) for r in relations):
            continue
        text = " ".join(tokens)
        offsets, cursor = [], 0
        for token in tokens:
            offsets.append(cursor)
            cursor += len(token) + 1

        def span(start, end, tokens=tokens, offsets=offsets):
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end < len(tokens)
            ):
                raise ValueError("invalid published relation token span")
            return {"start": offsets[start], "end": offsets[end] + len(tokens[end])}

        expected = [
            {
                "label": r[4].replace("-", "_"),
                "head": span(r[0], r[1]),
                "tail": span(r[2], r[3]),
                "native_label": r[4],
                "native_flags": r[6:],
                "native_explanation": r[5],
            }
            for r in relations
        ]
        rows.append(
            {
                "id": row["doc_key"],
                "text": text,
                "source_revision": hashlib.sha256(text.encode()).hexdigest(),
                "language": "en",
                "source": "CrossRE politics test",
                "label_origin": "published-human-annotation",
                "expected": expected,
                "relation_schema": schema,
            }
        )
        if len(rows) == 10:
            break
    if len(rows) != 10:
        raise ValueError("ten eligible relation examples required")
    return spec, rows

"""Frozen published XNLI selection and actual baseline/candidate inference."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

LABELS = ("entailment", "neutral", "contradiction")
MANIFEST = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/published_nli/manifest.json"
)


def select_pairs(splits, *, per_class=20):
    if type(per_class) is not int or not 1 <= per_class <= 20:
        raise ValueError("bounded class count required")
    output, validation_groups = [], set()
    for split in ("validation", "test"):
        german, english = splits["de-" + split], splits["en-" + split]
        if len(german) != len(english):
            raise ValueError("translated split lengths differ")
        counts = Counter()
        for index, (de, en) in enumerate(zip(german, english, strict=True)):
            if (
                de["label"] != en["label"]
                or type(en["label"]) is not int
                or en["label"] not in range(3)
            ):
                raise ValueError("translated native labels are not aligned")
            group = hashlib.sha256(en["premise"].encode()).hexdigest()
            if (
                counts[en["label"]] >= per_class
                or split == "test"
                and group in validation_groups
            ):
                continue
            counts[en["label"]] += 1
            if split == "validation":
                validation_groups.add(group)
            for language, row in (("de", de), ("en", en)):
                output.append(
                    {
                        "id": f"xnli:{language}:{split}:{index}",
                        "group_id": group,
                        "split": split,
                        "premise": row["premise"],
                        "hypothesis": row["hypothesis"],
                        "labels": [LABELS[row["label"]]],
                        "label_origin": "independent-human",
                        "source": "facebook/xnli",
                        "domain": "xnli-multigenre",
                        "language": language,
                    }
                )
            if all(counts[k] == per_class for k in range(3)):
                break
        if not all(counts[k] == per_class for k in range(3)):
            raise ValueError(
                "not enough independent groups for the frozen class sample"
            )
    return output


def prepare(directory):
    import duckdb

    manifest = json.loads(MANIFEST.read_text())
    splits = {}
    db = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        for filename, spec in manifest["files"].items():
            path = directory / filename
            if (
                path.stat().st_size != spec["bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]
            ):
                raise ValueError("frozen XNLI input changed")
            cursor = db.execute(
                "SELECT premise,hypothesis,label FROM read_parquet(?)", [str(path)]
            )
            splits[filename.removesuffix(".parquet")] = [
                dict(zip(("premise", "hypothesis", "label"), row, strict=True))
                for row in cursor.fetchall()
            ]
    finally:
        db.close()
    return manifest, select_pairs(splits)


def prepare_quote_probe(directory):
    """An additional published reported-speech case, outside calibration."""
    import duckdb

    _, selected = prepare(directory)
    validation_groups = {r["group_id"] for r in selected}
    db = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        pairs = {
            language: db.execute(
                "SELECT premise,hypothesis,label FROM read_parquet(?)",
                [str(directory / (language + "-test.parquet"))],
            ).fetchall()
            for language in ("de", "en")
        }
    finally:
        db.close()
    for index, (german, english) in enumerate(
        zip(pairs["de"], pairs["en"], strict=True)
    ):
        group = hashlib.sha256(english[0].encode()).hexdigest()
        if group in validation_groups or not (
            any(mark in english[0] + english[1] for mark in ('"', "“", "``"))
            or " said " in " " + english[0].casefold() + " "
        ):
            continue
        if german[2] != english[2]:
            raise ValueError("quotation translations have different labels")
        return [
            {
                "id": f"xnli:{language}:test:{index}",
                "group_id": group,
                "split": "test",
                "premise": row[0],
                "hypothesis": row[1],
                "labels": [LABELS[row[2]]],
                "label_origin": "independent-human",
                "source": "facebook/xnli",
                "domain": "xnli-reported-speech-challenge",
                "language": language,
            }
            for language, row in (("de", german), ("en", english))
        ]
    raise ValueError("no independent published quotation case found")


def run(payload):
    import torch

    from src.argument_mining.model_registry import optional_model_spec, resolved_pins
    from src.evaluation.mining_runtime import MultilingualNLI
    from src.kb.nli import TransformersNLI

    rows, kind = payload["rows"], payload["backend"]
    probes = payload.get("task_probes", {})
    if (
        kind not in {"baseline", "mdeberta"}
        or not 0 <= len(rows) <= 240
        or not rows
        and not probes
    ):
        raise ValueError("bounded NLI benchmark required")
    if (
        not isinstance(probes, dict)
        or not set(probes) <= {"stance", "frames"}
        or any(
            not isinstance(items, list) or not 1 <= len(items) <= 16
            for items in probes.values()
        )
    ):
        raise ValueError("bounded separate stance/frame probes required")
    if any(
        not isinstance(r.get(k), str) or len(r[k]) > 32000
        for r in rows
        for k in ("premise", "hypothesis")
    ):
        raise ValueError("bounded premise/hypothesis pairs required")
    started = time.monotonic()
    backend = TransformersNLI() if kind == "baseline" else MultilingualNLI()
    loaded = time.monotonic()
    if set(backend._id2label.values()) != set(LABELS):
        raise ValueError("all native NLI output labels must be explicit")

    def probabilities(pairs):
        if kind == "mdeberta":
            return backend.probabilities(pairs, batch_size=8)
        values = backend._bounded_inputs([p for p, _ in pairs], [h for _, h in pairs])
        with torch.inference_mode():
            raw = torch.softmax(backend._model(**values).logits, dim=-1).tolist()
        if len(raw) != len(pairs) or any(
            len(r) != 3 or any(not math.isfinite(v) for v in r) for r in raw
        ):
            raise ValueError("invalid baseline NLI probability output")
        return [{backend._id2label[i]: v for i, v in enumerate(r)} for r in raw]

    output = []
    for row in rows:
        start = time.monotonic()
        scores = probabilities([(row["premise"], row["hypothesis"])])[0]
        output.append(
            {
                **{k: v for k, v in row.items() if k not in {"premise", "hypothesis"}},
                "input_sha256": hashlib.sha256(
                    json.dumps(
                        [row["premise"], row["hypothesis"]], ensure_ascii=False
                    ).encode()
                ).hexdigest(),
                "scores": [scores[label] for label in LABELS],
                "elapsed_s": time.monotonic() - start,
            }
        )
    batches = []
    for offset in range(0, min(16, len(rows)), 8):
        selected = rows[offset : offset + 8]
        start = time.monotonic()
        scores = probabilities([(r["premise"], r["hypothesis"]) for r in selected])
        duration = time.monotonic() - start
        maximum_difference = max(
            abs(scores[j][label] - output[offset + j]["scores"][i])
            for j in range(len(selected))
            for i, label in enumerate(LABELS)
        )
        batches.append(
            {
                "batch_size": len(selected),
                "elapsed_s": duration,
                "max_probability_difference_from_single_pair": maximum_difference,
            }
        )
    tasks = task_probes(backend, probes) if probes else {}
    return {
        "backend": kind,
        "model": resolved_pins()["nli"]
        if kind == "baseline"
        else optional_model_spec("mdeberta"),
        "model_load_s": loaded - started,
        "rows": output,
        "batch_probes": batches,
        "task_probes": tasks,
        "configuration": {
            "premise_first": True,
            "max_pair_tokens": 512,
            "truncation": False,
            "main_batch_size": 1,
            "device": "cpu",
            "dtype": str(backend._model.dtype),
            "class_order": LABELS,
        },
        "corpus_sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def task_probes(backend, probes):
    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.model_diagnostics import diagnose
    from src.argument_mining.models import StanceClassifier

    output = {}
    for task, inputs in probes.items():
        templates = {
            "stance": StanceClassifier.NLI_TEMPLATES,
            "frames": FrameClassifier.NLI_TEMPLATES,
        }[task]
        rows = []
        for record in inputs:
            if (
                not isinstance(record.get("text"), str)
                or len(record["text"]) > 32000
                or not isinstance(record.get("topic", "the issue"), str)
                or len(record.get("topic", "the issue")) > 2000
            ):
                raise ValueError("bounded task text and explicit target required")
            if not isinstance(record.get("labels"), list) or not set(
                record["labels"]
            ) <= set(templates):
                raise ValueError("task labels must match the actual hypothesis schema")
            pairs = [
                (
                    record["text"],
                    template.format(topic=record.get("topic", "the issue")),
                )
                for template in templates.values()
            ]
            start = time.monotonic()
            scores = backend.entailment_scores(pairs, batch_size=8)
            rows.append(
                {
                    "id": record["id"],
                    "labels": record["labels"],
                    "scores": scores,
                    "source_type": record["source_type"],
                    "domain": "not annotated",
                    "language": "not annotated",
                    "target": record.get("topic", "issue framing"),
                    "input_sha256": hashlib.sha256(record["text"].encode()).hexdigest(),
                    "elapsed_s": time.monotonic() - start,
                }
            )
        output[task] = {
            "templates": templates,
            "rows": rows,
            "metrics": diagnose(rows, list(templates), task=task, legacy=True),
            "label_origin": "existing Noesis benchmark labels, not independent EX-05 human collection",
            "language_coverage": "not certified; corpus has no language annotations",
            "readiness": "unsupported pending independent task-specific evaluation",
        }
    return output

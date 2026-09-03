"""Reproducible, provenance-preserving human evaluation for argument mining.

The module deliberately separates three kinds of data:

* immutable source examples sampled from real ingested documents;
* two independently completed, prediction-blind annotation assignments;
* adjudicated gold rows which retain both raw judgements.

It never generates labels or treats model output as an annotation.  A complete
gold set therefore remains an external human-produced artifact, while every
software-controlled transition is deterministic and strictly validated.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = "noesis-human-eval-v1"
SAMPLER_VERSION = "stratified-sentence-v1"
SOURCE_TYPES = ("news", "blog", "paper", "transcript", "book", "note")
STANCE_LABELS = ("supportive", "critical", "neutral", "ambiguous")
FRAME_LABELS = (
    "economic",
    "security",
    "humanitarian",
    "legal",
    "political",
    "scientific",
    "other",
)
DIFFICULT_TAGS = (
    "attribution",
    "hedging",
    "mixed_stance",
    "negation",
    "numeric",
    "question",
    "quoted_speech",
)
MIN_COMPLETE_EXAMPLES = 500
DEFAULT_MIN_PER_SOURCE = 25

ITEM_FIELDS = (
    "item_id",
    "item_digest",
    "document_id",
    "source_type",
    "language",
    "source_url",
    "title",
    "topic",
    "length_bucket",
    "difficult_tags",
    "license",
    "redistribution",
    "text",
)
ANNOTATION_FIELDS = (
    *ITEM_FIELDS,
    "assignment_id",
    "annotator_id",
    "annotated_at",
    "is_claim",
    "stance",
    "frames",
    "notes",
)
LABEL_FIELDS = ("is_claim", "stance", "frames")

_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|nor|without|failed?|deny|denied)\b", re.IGNORECASE
)
_HEDGE = re.compile(
    r"\b(?:may|might|could|possibly|probably|apparently|reportedly)\b", re.IGNORECASE
)
_ATTRIBUTION = re.compile(
    r"\b(?:said|says|reported|according to|wrote|claimed|told)\b", re.IGNORECASE
)
_MIXED = re.compile(
    r"\b(?:but|however|although|while|despite|on the other hand)\b", re.IGNORECASE
)


class HumanEvalError(ValueError):
    """A human-evaluation artifact violates the published protocol."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _item_payload(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(row.get(field, ""))
        for field in ITEM_FIELDS
        if field != "item_digest"
    }


def item_digest(row: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical(_item_payload(row)))


def dataset_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    values = sorted(item_digest(row) for row in rows)
    return _sha256_bytes(_canonical(values))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in ANNOTATION_FIELDS})


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _example_host(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return (
        hostname == "example"
        or hostname == "example.com"
        or hostname.endswith(".example.com")
    )


def _length_bucket(text: str) -> str:
    words = len(_WORD.findall(text))
    if words < 15:
        return "short"
    if words < 40:
        return "medium"
    return "long"


def _difficult_tags(text: str) -> tuple[str, ...]:
    tags = set()
    if _ATTRIBUTION.search(text):
        tags.add("attribution")
    if _HEDGE.search(text):
        tags.add("hedging")
    if _MIXED.search(text):
        tags.add("mixed_stance")
    if _NEGATION.search(text):
        tags.add("negation")
    if re.search(r"\b\d+(?:[.,]\d+)?(?:%|\b)", text):
        tags.add("numeric")
    if "?" in text:
        tags.add("question")
    if any(mark in text for mark in ('"', "“", "”", "‘", "’")):
        tags.add("quoted_speech")
    return tuple(sorted(tags))


def _topic(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = metadata.get("topic") or metadata.get("subject")
    if not value:
        topics = metadata.get("topics")
        if isinstance(topics, list) and topics:
            value = topics[0]
    value = str(value or row.get("title") or "general subject").strip()
    return value[:200] or "general subject"


def _privacy(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[str, str]:
    tags = {str(tag).casefold() for tag in metadata.get("tags", []) if tag}
    private = bool(metadata.get("private")) or "private" in tags
    consented = bool(metadata.get("human_eval_consent"))
    if private and not consented:
        return "prohibited", "private_without_human_eval_consent"
    if consented:
        return "consented", ""
    return "source-terms", ""


def _candidate_rows(
    db_path: Path, *, languages: set[str]
) -> tuple[list[dict[str, str]], Counter]:
    import duckdb

    from services.ingest.common.document_model import Document
    from src.argument_mining.dataset import sentences_from_document

    exclusions: Counter = Counter()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        description = conn.execute("DESCRIBE documents").fetchall()
        columns = [row[0] for row in description]
        raw_rows = conn.execute(
            "SELECT * FROM documents WHERE content IS NOT NULL AND length(trim(content)) > 0"
        ).fetchall()
    except Exception as exc:
        raise HumanEvalError(
            f"could not read a unified documents table: {exc}"
        ) from exc
    finally:
        conn.close()

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_text: set[str] = set()
    for values in raw_rows:
        row = dict(zip(columns, values))
        metadata = _metadata(row.get("metadata"))
        document_id = str(row.get("document_id") or "")
        source_type = str(row.get("source_type") or "")
        language = str(row.get("language") or "und").casefold()
        url = str(row.get("url") or "")
        if source_type not in SOURCE_TYPES:
            exclusions["unsupported_source_type"] += 1
            continue
        if language not in languages:
            exclusions["language_not_selected"] += 1
            continue
        if _example_host(url) or document_id.startswith(("art-", "test-", "fixture-")):
            exclusions["example_or_fixture"] += 1
            continue
        if metadata.get("synthetic") or metadata.get("generated"):
            exclusions["synthetic"] += 1
            continue
        redistribution, reason = _privacy(row, metadata)
        if redistribution == "prohibited":
            exclusions[reason] += 1
            continue
        document = Document(
            document_id=document_id,
            source_type=source_type,
            language=language,
            ingested_at=int(row.get("ingested_at") or 0),
            url=row.get("url"),
            title=row.get("title"),
            content=row.get("content"),
            metadata=metadata,
        )
        for sentence_index, sentence in enumerate(sentences_from_document(document)):
            text = sentence.strip()
            if not 25 <= len(text) <= 1_500:
                exclusions["sentence_length"] += 1
                continue
            normalized_text = " ".join(text.casefold().split())
            text_fingerprint = _sha256_bytes(normalized_text.encode())
            if text_fingerprint in seen_text:
                exclusions["duplicate_sentence"] += 1
                continue
            seen_text.add(text_fingerprint)
            raw_key = f"{document_id}:{sentence_index}:{text}"
            item_id = _sha256_bytes(raw_key.encode("utf-8"))[:20]
            if item_id in seen:
                exclusions["duplicate_sentence"] += 1
                continue
            seen.add(item_id)
            difficult = _difficult_tags(text)
            candidate = {
                "item_id": item_id,
                "document_id": document_id,
                "source_type": source_type,
                "language": language,
                "source_url": url,
                "title": str(row.get("title") or "")[:500],
                "topic": _topic(row, metadata),
                "length_bucket": _length_bucket(text),
                "difficult_tags": "|".join(difficult),
                "license": str(metadata.get("license") or "unknown")[:200],
                "redistribution": redistribution,
                "text": text,
            }
            candidate["item_digest"] = item_digest(candidate)
            candidates.append(candidate)
    return candidates, exclusions


def _select_stratified(
    candidates: list[dict[str, str]],
    *,
    size: int,
    seed: int,
    minimum_per_source: int,
    max_per_document: int,
) -> list[dict[str, str]]:
    if size <= 0:
        raise HumanEvalError("sample size must be positive")
    rng = random.Random(seed)
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    rank = {row["item_id"]: index for index, row in enumerate(shuffled)}
    selected: list[dict[str, str]] = []
    selected_ids: set[str] = set()
    document_counts: Counter = Counter()

    def add(row: dict[str, str]) -> bool:
        if row["item_id"] in selected_ids:
            return False
        if document_counts[row["document_id"]] >= max_per_document:
            return False
        selected.append(row)
        selected_ids.add(row["item_id"])
        document_counts[row["document_id"]] += 1
        return True

    for source_type in SOURCE_TYPES:
        pool = [row for row in shuffled if row["source_type"] == source_type]
        added = 0
        for row in pool:
            if add(row):
                added += 1
            if added == minimum_per_source:
                break
        if added < minimum_per_source:
            raise HumanEvalError(
                f"source slice {source_type!r} has {added} eligible examples after the "
                f"per-document cap; {minimum_per_source} required"
            )

    dimensions = ("source_type", "language", "length_bucket")
    counts = {field: Counter(row[field] for row in selected) for field in dimensions}
    difficult_counts = Counter(
        tag for row in selected for tag in row["difficult_tags"].split("|") if tag
    )
    while len(selected) < size:
        available = [
            row
            for row in shuffled
            if row["item_id"] not in selected_ids
            and document_counts[row["document_id"]] < max_per_document
        ]
        if not available:
            raise HumanEvalError(
                f"only {len(selected)} eligible examples remain after stratification; "
                f"requested {size}. Ingest more diverse real material or run a pilot."
            )

        def priority(row: Mapping[str, str]) -> tuple[Any, ...]:
            tags = [tag for tag in row["difficult_tags"].split("|") if tag]
            difficult_score = min(
                (difficult_counts[tag] for tag in tags), default=math.inf
            )
            return (
                *(counts[field][row[field]] for field in dimensions),
                difficult_score,
                rank[row["item_id"]],
            )

        chosen = min(available, key=priority)
        add(chosen)
        for field in dimensions:
            counts[field][chosen[field]] += 1
        for tag in chosen["difficult_tags"].split("|"):
            if tag:
                difficult_counts[tag] += 1
    return selected


def _slice_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    materialized = list(rows)
    output = {
        field: dict(
            sorted(
                Counter(
                    str(row.get(field) or "unknown") for row in materialized
                ).items()
            )
        )
        for field in ("source_type", "language", "length_bucket")
    }
    output["difficult_tags"] = dict(
        sorted(
            Counter(
                tag
                for row in materialized
                for tag in str(row.get("difficult_tags") or "").split("|")
                if tag
            ).items()
        )
    )
    return output


def export_assignments(
    db_path: Path,
    out: Path,
    size: int,
    seed: int,
    *,
    languages: Iterable[str] = ("en",),
    minimum_per_source: int = DEFAULT_MIN_PER_SOURCE,
    max_per_document: int = 5,
    pilot: bool = False,
) -> dict[str, Any]:
    """Create two independently ordered, prediction-blind assignments."""
    if not pilot and not MIN_COMPLETE_EXAMPLES <= size <= 1_000:
        raise HumanEvalError("full evaluation size must be between 500 and 1000")
    if pilot and not 12 <= size < MIN_COMPLETE_EXAMPLES:
        raise HumanEvalError("pilot size must be between 12 and 499")
    languages_set = {
        language.strip().casefold() for language in languages if language.strip()
    }
    if not languages_set:
        raise HumanEvalError("at least one language must be selected")
    if minimum_per_source < 1:
        raise HumanEvalError("minimum_per_source must be positive")
    if minimum_per_source * len(SOURCE_TYPES) > size:
        raise HumanEvalError("minimum source quotas exceed the requested sample size")
    if max_per_document < 1:
        raise HumanEvalError("max_per_document must be positive")

    candidates, exclusions = _candidate_rows(db_path, languages=languages_set)
    selected = _select_stratified(
        candidates,
        size=size,
        seed=seed,
        minimum_per_source=minimum_per_source,
        max_per_document=max_per_document,
    )
    out.mkdir(parents=True, exist_ok=True)
    assignment_paths: dict[str, Path] = {}
    immutable_digest = dataset_digest(selected)
    for arm, offset in (("a", 1), ("b", 2)):
        rows = []
        for item in selected:
            row = {
                **item,
                "assignment_id": _sha256_bytes(
                    f"{PROTOCOL_VERSION}:{arm}:{item['item_id']}".encode()
                )[:20],
                "annotator_id": "",
                "annotated_at": "",
                "is_claim": "",
                "stance": "",
                "frames": "",
                "notes": "",
            }
            rows.append(row)
        random.Random(seed + offset).shuffle(rows)
        path = out / f"annotator_{arm}.csv"
        _write_csv(path, rows)
        assignment_paths[path.name] = path

    manifest = {
        "protocol": PROTOCOL_VERSION,
        "sampler": SAMPLER_VERSION,
        "status": "pilot_awaiting_annotations"
        if pilot
        else "awaiting_two_independent_annotators",
        "created_at": _utc_now(),
        "seed": seed,
        "pilot": pilot,
        "requested_size": size,
        "n_items": len(selected),
        "languages": sorted(languages_set),
        "minimum_per_source": minimum_per_source,
        "max_per_document": max_per_document,
        "slice_counts": _slice_counts(selected),
        "exclusions": dict(sorted(exclusions.items())),
        "immutable_dataset_sha256": immutable_digest,
        "assignment_templates": {
            name: {
                "file_sha256": file_sha256(path),
                "immutable_dataset_sha256": immutable_digest,
            }
            for name, path in assignment_paths.items()
        },
        "source_database": {
            "name": db_path.name,
            "path_disclosed": False,
        },
        "predictions_exposed": False,
        "synthetic_data": False,
        "privacy": {
            "private_without_consent_excluded": True,
            "assignment_artifacts_may_contain_source_text": True,
            "commit_assignments": False,
        },
    }
    _write_json(out / "manifest.json", manifest)
    _write_json(
        out / "status.json",
        {
            "protocol": PROTOCOL_VERSION,
            "status": manifest["status"],
            "n_items": len(selected),
            "immutable_dataset_sha256": immutable_digest,
            "required_next_step": "Have two different people complete annotator_a.csv and annotator_b.csv independently.",
        },
    )
    return manifest


def _parse_timestamp(value: str, *, path: Path, item_id: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HumanEvalError(f"{path}: {item_id} has invalid annotated_at") from exc
    if parsed.tzinfo is None:
        raise HumanEvalError(f"{path}: {item_id} annotated_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _normalize_frames(value: str, *, path: Path, item_id: str) -> str:
    frames = {frame.strip().casefold() for frame in value.split("|") if frame.strip()}
    if not frames or not frames.issubset(FRAME_LABELS):
        raise HumanEvalError(f"{path}: {item_id} has invalid frames")
    if "other" in frames and len(frames) > 1:
        raise HumanEvalError(
            f"{path}: {item_id} cannot combine other with another frame"
        )
    return "|".join(sorted(frames))


def read_completed_assignment(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """Read one completed assignment, rejecting drift and duplicate rows."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != ANNOTATION_FIELDS:
            raise HumanEvalError(
                f"{path}: columns must exactly match protocol {PROTOCOL_VERSION}; "
                "model predictions and extra fields are forbidden"
            )
        rows = list(reader)
    if not rows:
        raise HumanEvalError(f"{path}: assignment is empty")
    annotators = {row["annotator_id"].strip() for row in rows}
    if len(annotators) != 1 or not next(iter(annotators), ""):
        raise HumanEvalError(f"{path}: every row must carry one non-empty annotator_id")
    annotator = next(iter(annotators))
    by_item: dict[str, dict[str, str]] = {}
    assignment_ids: set[str] = set()
    for raw in rows:
        row = {field: str(raw.get(field) or "").strip() for field in ANNOTATION_FIELDS}
        item_id = row["item_id"]
        if not item_id or item_id in by_item:
            raise HumanEvalError(f"{path}: duplicate or empty item_id {item_id!r}")
        if not row["assignment_id"] or row["assignment_id"] in assignment_ids:
            raise HumanEvalError(f"{path}: duplicate or empty assignment_id")
        assignment_ids.add(row["assignment_id"])
        if row["item_digest"] != item_digest(row):
            raise HumanEvalError(
                f"{path}: immutable source fields changed for {item_id}"
            )
        if row["is_claim"] not in {"0", "1"}:
            raise HumanEvalError(f"{path}: {item_id} has invalid is_claim")
        stance = row["stance"].casefold()
        if stance not in STANCE_LABELS:
            raise HumanEvalError(f"{path}: {item_id} has invalid stance")
        row["stance"] = stance
        row["frames"] = _normalize_frames(row["frames"], path=path, item_id=item_id)
        row["annotated_at"] = _parse_timestamp(
            row["annotated_at"], path=path, item_id=item_id
        )
        by_item[item_id] = row
    return annotator, by_item


def _load_manifest(path: Path, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HumanEvalError(f"could not read manifest {path}: {exc}") from exc
    if manifest.get("protocol") != PROTOCOL_VERSION:
        raise HumanEvalError(f"{path}: unsupported human-eval protocol")
    if manifest.get("synthetic_data") is not False:
        raise HumanEvalError(f"{path}: synthetic data cannot become human gold")
    digest = dataset_digest(rows)
    if digest != manifest.get("immutable_dataset_sha256"):
        raise HumanEvalError("completed assignments do not match the sampled manifest")
    return manifest


def _labels_equal(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    return all(left[field] == right[field] for field in LABEL_FIELDS)


def _cohen_kappa(left: list[str], right: list[str]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    labels = sorted(set(left) | set(right))
    observed = sum(a == b for a, b in zip(left, right)) / len(left)
    expected = sum(
        (left.count(label) / len(left)) * (right.count(label) / len(right))
        for label in labels
    )
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else None
    return round((observed - expected) / (1.0 - expected), 4)


def _bootstrap_kappa(left: list[str], right: list[str], *, seed: int) -> dict[str, Any]:
    """Return a deterministic percentile interval without hiding small samples."""
    if len(left) < 10:
        return {
            "status": "undersized",
            "n": len(left),
            "minimum_n": 10,
            "lower": None,
            "upper": None,
        }
    rng = random.Random(seed)
    estimates = []
    for _ in range(500):
        positions = [rng.randrange(len(left)) for _ in left]
        value = _cohen_kappa(
            [left[index] for index in positions],
            [right[index] for index in positions],
        )
        if value is not None:
            estimates.append(value)
    if len(estimates) < 25:
        return {
            "status": "not_estimable",
            "n": len(left),
            "lower": None,
            "upper": None,
        }
    estimates.sort()
    return {
        "status": "reported",
        "method": "percentile_bootstrap",
        "iterations": 500,
        "n": len(left),
        "lower": round(estimates[int(0.025 * (len(estimates) - 1))], 4),
        "upper": round(estimates[int(0.975 * (len(estimates) - 1))], 4),
    }


def _agreement_metrics(
    a: Mapping[str, Mapping[str, str]], b: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    ids = sorted(a)
    claim_left = [a[item]["is_claim"] for item in ids]
    claim_right = [b[item]["is_claim"] for item in ids]
    stance_left = [a[item]["stance"] for item in ids]
    stance_right = [b[item]["stance"] for item in ids]
    frame_pairs = {
        frame: (
            [str(int(frame in a[item]["frames"].split("|"))) for item in ids],
            [str(int(frame in b[item]["frames"].split("|"))) for item in ids],
        )
        for frame in FRAME_LABELS
    }
    frame_kappas = {
        frame: _cohen_kappa(left, right) for frame, (left, right) in frame_pairs.items()
    }
    valid_frame_kappas = [value for value in frame_kappas.values() if value is not None]
    return {
        "n": len(ids),
        "exact_agreement": round(
            sum(_labels_equal(a[item], b[item]) for item in ids) / len(ids), 4
        ),
        "claim_kappa": _cohen_kappa(claim_left, claim_right),
        "claim_kappa_confidence_interval_95": _bootstrap_kappa(
            claim_left, claim_right, seed=2101
        ),
        "stance_kappa": _cohen_kappa(stance_left, stance_right),
        "stance_kappa_confidence_interval_95": _bootstrap_kappa(
            stance_left, stance_right, seed=2102
        ),
        "frame_kappa_by_label": frame_kappas,
        "frame_kappa_confidence_interval_95_by_label": {
            frame: _bootstrap_kappa(left, right, seed=2200 + index)
            for index, (frame, (left, right)) in enumerate(frame_pairs.items())
        },
        "frame_kappa_macro": (
            round(sum(valid_frame_kappas) / len(valid_frame_kappas), 4)
            if valid_frame_kappas
            else None
        ),
    }


def _agreement_slices(
    a: Mapping[str, Mapping[str, str]], b: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field in ("source_type", "language", "length_bucket"):
        groups: dict[str, list[str]] = defaultdict(list)
        for item_id, row in a.items():
            groups[row[field] or "unknown"].append(item_id)
        output[field] = {}
        for value, ids in sorted(groups.items()):
            if len(ids) < 10:
                output[field][value] = {
                    "n": len(ids),
                    "status": "undersized",
                    "minimum_n": 10,
                }
            else:
                output[field][value] = {
                    "status": "reported",
                    **_agreement_metrics(
                        {item: a[item] for item in ids}, {item: b[item] for item in ids}
                    ),
                }
    return output


def _interval_text(interval: Mapping[str, Any]) -> str:
    if interval.get("status") != "reported":
        return interval.get("status", "not_estimable")
    return f"{interval['lower']:.4f}–{interval['upper']:.4f}"


def _agreement_markdown(report: Mapping[str, Any]) -> str:
    metrics = report["agreement"]
    lines = [
        "# Human annotation agreement",
        "",
        f"- Protocol: `{report['protocol']}`",
        f"- Examples: {report['n_examples']}",
        f"- Disagreements: {report['disagreements']}",
        "- Data kind: real-human assignments (not simulated perturbations)",
        "",
        "| Task | Cohen's kappa | 95% interval | N |",
        "| --- | ---: | --- | ---: |",
        (
            f"| claim | {metrics['claim_kappa']} | "
            f"{_interval_text(metrics['claim_kappa_confidence_interval_95'])} | {metrics['n']} |"
        ),
        (
            f"| stance | {metrics['stance_kappa']} | "
            f"{_interval_text(metrics['stance_kappa_confidence_interval_95'])} | {metrics['n']} |"
        ),
    ]
    for label, value in metrics["frame_kappa_by_label"].items():
        interval = metrics["frame_kappa_confidence_interval_95_by_label"][label]
        lines.append(
            f"| frame:{label} | {value} | {_interval_text(interval)} | {metrics['n']} |"
        )
    lines += ["", "## Slices", ""]
    for dimension, values in report["agreement_slices"].items():
        lines.append(f"### {dimension}")
        lines.append("")
        for value, row in values.items():
            if row["status"] == "undersized":
                lines.append(
                    f"- `{value}`: undersized (n={row['n']}, minimum={row['minimum_n']})"
                )
            else:
                lines.append(
                    f"- `{value}`: claim κ={row['claim_kappa']}, "
                    f"stance κ={row['stance_kappa']} (n={row['n']})"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def analyze_assignments(
    a_path: Path,
    b_path: Path,
    *,
    manifest_path: Path,
    out: Path,
    write_adjudication: bool = True,
    guideline_changes: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate two blind assignments and prepare disagreements for review."""
    annotator_a, a = read_completed_assignment(a_path)
    annotator_b, b = read_completed_assignment(b_path)
    if annotator_a == annotator_b:
        raise HumanEvalError("annotator_id values must identify two different people")
    if set(a) != set(b):
        raise HumanEvalError(
            "assignment files do not contain exactly the same item ids"
        )
    for item_id in a:
        if item_digest(a[item_id]) != item_digest(b[item_id]):
            raise HumanEvalError(f"assignment source fields disagree for {item_id}")
    manifest = _load_manifest(manifest_path, a.values())
    recorded_changes = [
        change.strip() for change in guideline_changes if change.strip()
    ]
    if manifest.get("pilot") and (recorded_changes or not manifest.get("pilot_review")):
        manifest["pilot_review"] = {
            "reviewed_at": _utc_now(),
            "n_disagreements": sum(
                not _labels_equal(a[item_id], b[item_id]) for item_id in a
            ),
            "guideline_changes": recorded_changes,
            "guidance_reviewed": True,
        }
        _write_json(manifest_path, manifest)
    disagreements = [
        item_id for item_id in sorted(a) if not _labels_equal(a[item_id], b[item_id])
    ]
    adjudication_rows = []
    for item_id in disagreements:
        source = a[item_id]
        adjudication_rows.append(
            {
                **{field: source[field] for field in ITEM_FIELDS},
                "assignment_id": _sha256_bytes(
                    f"{PROTOCOL_VERSION}:adjudication:{item_id}".encode()
                )[:20],
                "annotator_id": "",
                "annotated_at": "",
                "is_claim": "",
                "stance": "",
                "frames": "",
                "notes": (
                    f"A={a[item_id]['is_claim']}/{a[item_id]['stance']}/{a[item_id]['frames']}; "
                    f"B={b[item_id]['is_claim']}/{b[item_id]['stance']}/{b[item_id]['frames']}"
                ),
            }
        )
    out.mkdir(parents=True, exist_ok=True)
    adjudication_path = out / "adjudication.csv"
    if write_adjudication:
        _write_csv(adjudication_path, adjudication_rows)
    report = {
        "protocol": PROTOCOL_VERSION,
        "status": "needs_adjudication" if disagreements else "ready_to_finalize",
        "analyzed_at": _utc_now(),
        "synthetic_data": False,
        "n_examples": len(a),
        "annotators": [annotator_a, annotator_b],
        "disagreements": len(disagreements),
        "agreement": _agreement_metrics(a, b),
        "agreement_slices": _agreement_slices(a, b),
        "slice_counts": _slice_counts(a.values()),
        "immutable_dataset_sha256": manifest["immutable_dataset_sha256"],
        "pilot_review": manifest.get("pilot_review"),
        "adjudication_assignment": str(adjudication_path),
        "adjudication_sha256": (
            file_sha256(adjudication_path) if write_adjudication else None
        ),
    }
    _write_json(out / "agreement.json", report)
    (out / "agreement.md").write_text(_agreement_markdown(report), encoding="utf-8")
    _write_json(
        out / "status.json",
        {
            "protocol": PROTOCOL_VERSION,
            "status": report["status"],
            "n_examples": len(a),
            "disagreements": len(disagreements),
            "required_next_step": (
                "A third independent person must complete adjudication.csv."
                if disagreements
                else "Run finalize again to publish the preserved gold artifact."
            ),
        },
    )
    return report


def _split(document_id: str, seed: int) -> str:
    value = int(_sha256_bytes(f"{seed}:{document_id}".encode())[:8], 16) % 100
    if value < 70:
        return "train"
    if value < 85:
        return "dev"
    return "test"


def _write_gold_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import duckdb

    columns = list(rows[0])
    conn = duckdb.connect()
    try:
        definitions = ", ".join(f'"{column}" VARCHAR' for column in columns)
        conn.execute(f"CREATE TABLE gold ({definitions})")
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO gold VALUES ({placeholders})",
            [[str(row.get(column, "")) for column in columns] for row in rows],
        )
        escaped = str(path).replace("'", "''")
        conn.execute(f"COPY gold TO '{escaped}' (FORMAT PARQUET)")
    finally:
        conn.close()


def _update_benchmark_status(out: Path, status: Mapping[str, Any]) -> None:
    stats_path = out.parent / "stats.json"
    if not stats_path.exists():
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except ValueError:
        return
    stats["human_evaluation"] = dict(status)
    _write_json(stats_path, stats)


def finalize(
    a_path: Path,
    b_path: Path,
    adjudication_path: Path | None,
    out: Path,
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    """Publish human gold only after strict dual-label and adjudication checks."""
    report = analyze_assignments(
        a_path,
        b_path,
        manifest_path=manifest_path,
        out=out,
        write_adjudication=False,
        guideline_changes=(),
    )
    annotator_a, a = read_completed_assignment(a_path)
    annotator_b, b = read_completed_assignment(b_path)
    disagreements = {
        item_id for item_id in a if not _labels_equal(a[item_id], b[item_id])
    }
    decisions: dict[str, dict[str, str]] = {}
    adjudicator = None
    if disagreements:
        if adjudication_path is None:
            return report
        adjudicator, decisions = read_completed_assignment(adjudication_path)
        if adjudicator in {annotator_a, annotator_b}:
            raise HumanEvalError(
                "the adjudicator must be distinct from both annotators"
            )
        if set(decisions) != disagreements:
            raise HumanEvalError(
                "adjudication must contain exactly the disagreement items"
            )
        for item_id in disagreements:
            if item_digest(decisions[item_id]) != item_digest(a[item_id]):
                raise HumanEvalError(
                    f"adjudication source fields changed for {item_id}"
                )
    elif adjudication_path is not None:
        raise HumanEvalError("no adjudication file is needed when all labels agree")

    manifest = _load_manifest(manifest_path, a.values())
    gold: list[dict[str, Any]] = []
    for item_id in sorted(a):
        chosen = decisions.get(item_id, a[item_id])
        if a[item_id]["redistribution"] == "prohibited":
            raise HumanEvalError(
                f"private text is prohibited in the gold artifact: {item_id}"
            )
        gold.append(
            {
                **{field: a[item_id][field] for field in ITEM_FIELDS},
                "split": _split(a[item_id]["document_id"], int(manifest["seed"])),
                "is_claim": chosen["is_claim"],
                "stance": chosen["stance"],
                "frames": chosen["frames"],
                "annotator_a_id": annotator_a,
                "annotator_a_at": a[item_id]["annotated_at"],
                "annotator_a_is_claim": a[item_id]["is_claim"],
                "annotator_a_stance": a[item_id]["stance"],
                "annotator_a_frames": a[item_id]["frames"],
                "annotator_b_id": annotator_b,
                "annotator_b_at": b[item_id]["annotated_at"],
                "annotator_b_is_claim": b[item_id]["is_claim"],
                "annotator_b_stance": b[item_id]["stance"],
                "annotator_b_frames": b[item_id]["frames"],
                "adjudicator_id": adjudicator or "not_required",
                "adjudicated_at": decisions.get(item_id, {}).get("annotated_at", ""),
                "was_disagreement": str(item_id in disagreements).lower(),
            }
        )

    out.mkdir(parents=True, exist_ok=True)
    jsonl_path = out / "human_gold.jsonl"
    jsonl_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in gold
        ),
        encoding="utf-8",
    )
    parquet_path = out / "human_gold.parquet"
    _write_gold_parquet(parquet_path, gold)
    complete = not manifest.get("pilot") and len(gold) >= MIN_COMPLETE_EXAMPLES
    status = {
        "protocol": PROTOCOL_VERSION,
        "status": "complete" if complete else "pilot_complete",
        "completed_at": _utc_now(),
        "synthetic_data": False,
        "n_examples": len(gold),
        "n_disagreements": len(disagreements),
        "annotators": [annotator_a, annotator_b],
        "adjudicator": adjudicator or "not_required",
        "slice_counts": _slice_counts(gold),
        "split_counts": dict(sorted(Counter(row["split"] for row in gold).items())),
        "agreement": report["agreement"],
        "agreement_slices": report["agreement_slices"],
        "immutable_dataset_sha256": manifest["immutable_dataset_sha256"],
        "gold_jsonl_sha256": file_sha256(jsonl_path),
        "gold_parquet_sha256": file_sha256(parquet_path),
        "privacy": {
            "prohibited_private_rows": 0,
            "source_text_present": True,
            "redistribution_values": sorted({row["redistribution"] for row in gold}),
        },
    }
    _write_json(out / "results.json", status)
    _write_json(out / "status.json", status)
    _update_benchmark_status(out, status)
    return status


def load_gold(path: Path) -> list[dict[str, Any]]:
    """Load and hash-check the canonical JSONL gold artifact."""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise HumanEvalError(f"{path}:{number}: invalid JSON") from exc
        if row.get("item_digest") != item_digest(row):
            raise HumanEvalError(f"{path}:{number}: immutable item digest mismatch")
        rows.append(row)
    if not rows:
        raise HumanEvalError(f"{path}: gold artifact is empty")
    if len({row["item_id"] for row in rows}) != len(rows):
        raise HumanEvalError(f"{path}: duplicate gold item ids")
    normalized_texts = [" ".join(str(row["text"]).casefold().split()) for row in rows]
    if len(set(normalized_texts)) != len(normalized_texts):
        raise HumanEvalError(f"{path}: duplicate gold source text")
    return rows


__all__ = [
    "ANNOTATION_FIELDS",
    "DEFAULT_MIN_PER_SOURCE",
    "DIFFICULT_TAGS",
    "FRAME_LABELS",
    "ITEM_FIELDS",
    "MIN_COMPLETE_EXAMPLES",
    "PROTOCOL_VERSION",
    "SOURCE_TYPES",
    "STANCE_LABELS",
    "HumanEvalError",
    "analyze_assignments",
    "dataset_digest",
    "export_assignments",
    "file_sha256",
    "finalize",
    "item_digest",
    "load_gold",
    "read_completed_assignment",
]

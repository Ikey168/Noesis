"""Published token annotations mapped to exact spans without generating labels."""

import hashlib
import json
import time
from pathlib import Path

LABELS = {
    "de": {"PER": "person", "ORG": "organisation", "LOC": "location"},
    "en": {
        "person": "person",
        "politician": "person",
        "organisation": "organisation",
        "politicalparty": "organisation",
        "country": "location",
        "location": "location",
    },
}
MANIFEST = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/published_ner/manifest.json"
)


def parse_records(raw, language, *, label_mapping=None):
    """Offsets refer to space-joined original tokens, not unknown article whitespace."""
    mapping = LABELS[language] if label_mapping is None else label_mapping
    tokens, columns, source = [], [], None
    output = []

    def flush():
        if not tokens:
            return
        text = " ".join(tokens)
        spans, ignored = [], set()
        starts, cursor = [], 0
        for token in tokens:
            starts.append(cursor)
            cursor += len(token) + 1
        for level in range(len(columns[0])):
            active = None
            for i in range(len(tokens) + 1):
                tag = columns[i][level] if i < len(tokens) else "O"
                if tag != "O" and (len(tag) < 3 or tag[:2] not in {"B-", "I-"}):
                    raise ValueError("malformed BIO annotation")
                if active and (
                    tag == "O" or tag.startswith("B-") or tag[2:] != active[0]
                ):
                    label, begin = active
                    if label in mapping:
                        spans.append(
                            {
                                "label": mapping[label],
                                "start": starts[begin],
                                "end": starts[i - 1] + len(tokens[i - 1]),
                            }
                        )
                    else:
                        ignored.add(label)
                    active = None
                if tag.startswith("I-") and active is None:
                    raise ValueError("orphan BIO continuation")
                if tag.startswith("B-"):
                    active = (tag[2:], i)
        unique = {(s["label"], s["start"], s["end"]): s for s in spans}
        output.append(
            {
                "text": text,
                "expected": list(unique.values()),
                "unsupported_labels": sorted(ignored),
                "source_locator": source,
                "native_sentence_index": len(output),
                "token_count": len(tokens),
            }
        )
        tokens.clear()
        columns.clear()

    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith("#"):
            flush()
            source = line[1:].strip()
            continue
        parts = line.split("\t")
        while parts and parts[-1] == "":
            parts.pop()
        if language == "de":
            if len(parts) != 4 or not parts[0].isdigit():
                raise ValueError("invalid German token annotation")
            token, tags = parts[1], parts[2:]
        else:
            if len(parts) != 2:
                raise ValueError("invalid English token annotation")
            token, tags = parts[0], parts[1:]
        if not token:
            raise ValueError("empty source token")
        tokens.append(token)
        columns.append(tags)
    flush()
    return output


def prepare(directory, *, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("bounded selection required")
    sources = json.loads(MANIFEST.read_text())
    rows, selection = [], {}
    for name, source in sources.items():
        path = directory / name
        if path.stat().st_size != source["size_bytes"]:
            raise ValueError("published dataset size mismatch")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("published dataset digest mismatch")
        candidates = parse_records(raw, source["language"])
        selected = []
        for row in candidates:
            if (
                row["unsupported_labels"]
                or not row["expected"]
                or row["token_count"] > 80
            ):
                continue
            text = row["text"]
            selected.append(
                {
                    **row,
                    "id": name + ":" + str(row["native_sentence_index"]),
                    "source_revision": hashlib.sha256(text.encode()).hexdigest(),
                    "language": source["language"],
                    "source": source["source"],
                    "label_origin": source["label_origin"],
                }
            )
            if len(selected) == limit:
                break
        if len(selected) != limit:
            raise ValueError("insufficient eligible published annotations")
        rows.extend(selected)
        selection[name] = {
            "available_sentences": len(candidates),
            "selected": len(selected),
            "rule": "First eligible positive test sentences, at most 80 tokens, all native labels within declared mapped ontology; selected before inference",
        }
    return sources, selection, rows


def run(payload):
    from src.evaluation.benchmark_runtime import score_output
    from src.evaluation.model_backends import GLiNERBackend, bounded_texts

    kind, rows = payload["backend"], payload["rows"]
    if (
        kind not in {"gliner2", "metadata-spacy", "language-spacy"}
        or not 1 <= len(rows) <= 100
    ):
        raise ValueError("bounded backend and records required")
    bounded_texts([r["text"] for r in rows], max_records=100, max_chars=8000)
    for row in rows:
        if (
            row["source_revision"] != hashlib.sha256(row["text"].encode()).hexdigest()
            or row["label_origin"] != "published-human-annotation"
        ):
            raise ValueError(
                "source revision and published annotation provenance required"
            )
    start = time.monotonic()
    if kind == "gliner2":
        model = GLiNERBackend()
        version = model.spec
    else:
        import spacy

        from src.argument_mining import metadata

        models = {"en": metadata._get_nlp()}
        if models["en"] is None:
            raise ValueError("existing spaCy actor model unavailable")
        if kind == "language-spacy":
            models["de"] = spacy.load("de_core_news_sm")
        version = {
            k: {"name": v.meta["name"], "version": v.meta["version"]}
            for k, v in models.items()
        }
    loaded = time.monotonic()
    result = []
    for row in rows:
        before = time.monotonic()
        if kind == "gliner2":
            if row.get("relation_schema"):
                prediction = model.extract_schema(
                    row["text"],
                    {"relations": row["relation_schema"]},
                    source_id=row["id"],
                    source_revision=row["source_revision"],
                    language=row["language"],
                )["relations"]
            elif row.get("entity_schema"):
                prediction = model.extract_schema(
                    row["text"],
                    {"entities": row["entity_schema"]},
                    source_id=row["id"],
                    source_revision=row["source_revision"],
                    language=row["language"],
                )["entities"]
            else:
                prediction = model.extract(
                    row["text"],
                    ["person", "organisation", "location"],
                    source_id=row["id"],
                    source_revision=row["source_revision"],
                    language=row["language"],
                )["entities"]
        else:
            nlp = models.get(row["language"], models["en"])
            mapping = {
                "PERSON": "person",
                "PER": "person",
                "ORG": "organisation",
                "GPE": "location",
                "FAC": "location",
                "LOC": "location",
            }
            prediction = [
                {"label": mapping[e.label_], "start": e.start_char, "end": e.end_char}
                for e in nlp(row["text"]).ents
                if e.label_ in mapping
                and (
                    kind != "metadata-spacy"
                    or (
                        e.label_ in {"PERSON", "ORG", "GPE", "FAC", "NORP"}
                        and len(e.text.strip()) >= 2
                    )
                )
            ]
        elapsed = time.monotonic() - before
        from src.evaluation.published_relations import score_relations

        result.append(
            {
                **{k: v for k, v in row.items() if k != "text"},
                "predicted": prediction,
                "elapsed_s": elapsed,
                "metrics": score_relations(prediction, row["expected"])
                if row.get("relation_schema")
                else score_output("spans", prediction, row["expected"]),
            }
        )
    return {
        "backend": kind,
        "model": version,
        "model_load_s": loaded - start,
        "rows": result,
        "span_coordinate_system": "Unicode character offsets in space-joined original published tokens",
        "default_changed": False,
        "confidence_is_correctness": False,
    }


def prepare_politics(directory, *, limit=20):
    sources, _, _ = prepare(directory, limit=limit)
    name = "crossner-politics-test.txt"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "config/extraction_schemas/politics.json"
        )
        .resolve()
        .read_text()
    )["entities"]
    records = parse_records(
        (directory / name).read_bytes(), "en", label_mapping={k: k for k in schema}
    )
    rows = []
    for row in records:
        if row["unsupported_labels"] or not row["expected"] or row["token_count"] > 80:
            continue
        rows.append(
            {
                **row,
                "id": name + ":" + str(row["native_sentence_index"]),
                "source_revision": hashlib.sha256(row["text"].encode()).hexdigest(),
                "language": "en",
                "source": sources[name]["source"],
                "label_origin": sources[name]["label_origin"],
                "entity_schema": schema,
            }
        )
        if len(rows) == limit:
            break
    if len(rows) != limit:
        raise ValueError("insufficient eligible politics annotations")
    return (
        {name: sources[name]},
        {
            "rule": "First twenty eligible positive test sentences up to 80 tokens; preserve all nine original political entity labels"
        },
        rows,
    )

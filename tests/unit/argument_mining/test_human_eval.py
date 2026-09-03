"""Protocol tests use temporary fixture judgements, never claimed human data."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator, ValidationError

from src.argument_mining.human_eval import (
    ANNOTATION_FIELDS,
    FRAME_LABELS,
    SOURCE_TYPES,
    HumanEvalError,
    analyze_assignments,
    export_assignments,
    finalize,
    item_digest,
    load_gold,
    read_completed_assignment,
)
from src.argument_mining.human_eval_metrics import (
    PREDICTION_FIELDS,
    evaluate_predictions,
    predict_gold,
)


def _database(path: Path) -> None:
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE documents (
            document_id TEXT, source_type TEXT, language TEXT, ingested_at BIGINT,
            url TEXT, title TEXT, content TEXT, metadata TEXT
        )
        """
    )
    for source_index, source_type in enumerate(SOURCE_TYPES):
        for document_index in range(2):
            number = source_index * 2 + document_index
            sentences = [
                f"Source {source_type} document {number} reports that output rose by {number + 11}% this year.",
                f"Analysts said source {source_type} document {number} might improve, but risks remain significant.",
                f"Would the policy described in source {source_type} document {number} actually help local residents?",
            ]
            conn.execute(
                "INSERT INTO documents VALUES (?, ?, 'en', 1, ?, ?, ?, ?)",
                [
                    f"real-{source_type}-{document_index}",
                    source_type,
                    f"https://publisher.test/{source_type}/{document_index}",
                    f"Topic {source_type}",
                    " ".join(sentences),
                    json.dumps(
                        {"topic": f"policy for {source_type}", "license": "test"}
                    ),
                ],
            )
    conn.execute(
        "INSERT INTO documents VALUES ('private-real', 'news', 'en', 1, NULL, "
        "'Private', 'A private source reports a material change in public policy.', "
        "'{\"private\": true}')"
    )
    conn.execute(
        "INSERT INTO documents VALUES ('public-example', 'news', 'en', 1, "
        "'https://example.com/story', 'Example', "
        "'An example page reports a material change in public policy.', '{}')"
    )
    conn.close()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _complete(
    path: Path,
    annotator: str,
    *,
    disagreement_item: str | None = None,
) -> None:
    rows = _read_csv(path)
    positions = {
        item_id: index
        for index, item_id in enumerate(sorted(row["item_id"] for row in rows))
    }
    for row in rows:
        index = positions[row["item_id"]]
        row.update(
            {
                "annotator_id": annotator,
                "annotated_at": "2026-09-03T10:15:00+02:00",
                "is_claim": "1" if index % 2 else "0",
                "stance": "supportive" if index % 2 else "neutral",
                "frames": "scientific" if index % 2 else "political|legal",
                "notes": "fixture judgement",
            }
        )
        if row["item_id"] == disagreement_item:
            row["stance"] = "critical"
    _write_csv(path, rows)


@pytest.fixture
def exported(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "documents.duckdb"
    output = tmp_path / "evaluation"
    _database(database)
    manifest = export_assignments(
        database,
        output,
        18,
        1729,
        minimum_per_source=2,
        max_per_document=2,
        pilot=True,
    )
    assert manifest["predictions_exposed"] is False
    assert manifest["synthetic_data"] is False
    assert set(manifest["slice_counts"]["source_type"]) == set(SOURCE_TYPES)
    assert manifest["exclusions"]["private_without_human_eval_consent"] == 1
    assert manifest["exclusions"]["example_or_fixture"] == 1
    return database, output


def test_export_is_deterministic_and_blind(
    exported: tuple[Path, Path], tmp_path: Path
) -> None:
    database, first = exported
    second = tmp_path / "second"
    export_assignments(
        database,
        second,
        18,
        1729,
        minimum_per_source=2,
        max_per_document=2,
        pilot=True,
    )
    first_rows = _read_csv(first / "annotator_a.csv")
    second_rows = _read_csv(second / "annotator_a.csv")
    assert [row["item_id"] for row in first_rows] == [
        row["item_id"] for row in second_rows
    ]
    assert tuple(first_rows[0]) == ANNOTATION_FIELDS
    assert all("prediction" not in field for field in first_rows[0])
    assert all(not row["is_claim"] and not row["stance"] for row in first_rows)


def test_assignment_validation_rejects_drift_and_duplicates(
    exported: tuple[Path, Path],
) -> None:
    _, output = exported
    assignment = output / "annotator_a.csv"
    _complete(assignment, "fixture-a")
    read_completed_assignment(assignment)

    rows = _read_csv(assignment)
    rows[0]["text"] += " changed"
    _write_csv(assignment, rows)
    with pytest.raises(HumanEvalError, match="immutable source fields changed"):
        read_completed_assignment(assignment)

    rows[0]["text"] = rows[1]["text"]
    rows[0]["item_id"] = rows[1]["item_id"]
    rows[0]["item_digest"] = item_digest(rows[0])
    _write_csv(assignment, rows)
    with pytest.raises(HumanEvalError, match="duplicate or empty item_id"):
        read_completed_assignment(assignment)


def test_analyze_and_finalize_preserve_raw_judgements(
    exported: tuple[Path, Path],
) -> None:
    _, output = exported
    a_path = output / "annotator_a.csv"
    b_path = output / "annotator_b.csv"
    disagreement = _read_csv(a_path)[0]["item_id"]
    _complete(a_path, "fixture-a")
    _complete(b_path, "fixture-b", disagreement_item=disagreement)

    report = analyze_assignments(
        a_path,
        b_path,
        manifest_path=output / "manifest.json",
        out=output,
        guideline_changes=["Fixture review clarified attributed claims."],
    )
    assert report["status"] == "needs_adjudication"
    assert report["disagreements"] == 1
    assert report["pilot_review"]["guidance_reviewed"] is True
    assert report["pilot_review"]["guideline_changes"]
    assert report["agreement"]["claim_kappa_confidence_interval_95"]["status"] in {
        "reported",
        "not_estimable",
    }
    adjudication = output / "adjudication.csv"
    adjudication_rows = _read_csv(adjudication)
    assert [row["item_id"] for row in adjudication_rows] == [disagreement]
    _complete(adjudication, "fixture-adjudicator")
    completed_before = adjudication.read_bytes()

    status = finalize(
        a_path,
        b_path,
        adjudication,
        output,
        manifest_path=output / "manifest.json",
    )
    assert adjudication.read_bytes() == completed_before
    assert status["status"] == "pilot_complete"
    assert status["n_examples"] == 18
    assert status["privacy"]["prohibited_private_rows"] == 0
    gold = load_gold(output / "human_gold.jsonl")
    decided = next(row for row in gold if row["item_id"] == disagreement)
    assert decided["annotator_a_stance"] != decided["annotator_b_stance"]
    assert decided["stance"] in {
        decided["annotator_a_stance"],
        decided["annotator_b_stance"],
    }
    documents_by_split: dict[str, set[str]] = {}
    for row in gold:
        documents_by_split.setdefault(row["document_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in documents_by_split.values())


def _gold(path: Path, n: int = 24) -> list[dict[str, str]]:
    rows = []
    for index in range(n):
        row = {
            "item_id": f"gold-{index}",
            "document_id": f"document-{index}",
            "source_type": SOURCE_TYPES[index % len(SOURCE_TYPES)],
            "language": "en" if index % 3 else "de",
            "source_url": f"https://source.test/{index}",
            "title": f"Evaluation topic {index}",
            "topic": f"policy {index}",
            "length_bucket": ("short", "medium", "long")[index % 3],
            "difficult_tags": "negation" if index % 2 else "numeric",
            "license": "test",
            "redistribution": "consented",
            "text": f"Unique evaluation sentence {index} reports a change of {index + 10} percent.",
            "split": "test",
            "is_claim": str(index % 2),
            "stance": ("supportive", "critical", "neutral", "ambiguous")[index % 4],
            "frames": FRAME_LABELS[index % len(FRAME_LABELS)],
        }
        row["item_digest"] = item_digest(row)
        rows.append(row)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return rows


def _predictions(
    path: Path, gold: list[dict[str, str]], *, model: str, errors: int
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_FIELDS)
        writer.writeheader()
        for index, row in enumerate(gold):
            wrong = index < errors
            writer.writerow(
                {
                    "item_id": row["item_id"],
                    "model_id": model,
                    "claim_label": str(1 - int(row["is_claim"]))
                    if wrong
                    else row["is_claim"],
                    "claim_confidence": "0.8",
                    "claim_abstained": "false",
                    "stance_label": "neutral" if wrong else row["stance"],
                    "stance_confidence": "0.75",
                    "stance_abstained": "false",
                    "frames": "other" if wrong else row["frames"],
                    "frame_confidence": "0.7",
                    "frame_abstained": "false",
                }
            )


def test_model_report_covers_metrics_slices_and_promotion(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.jsonl"
    gold = _gold(gold_path)
    baseline_predictions = tmp_path / "baseline.csv"
    candidate_predictions = tmp_path / "candidate.csv"
    _predictions(baseline_predictions, gold, model="baseline-v1", errors=8)
    _predictions(candidate_predictions, gold, model="candidate-v2", errors=0)
    out = tmp_path / "reports"
    experiment = tmp_path / "experiment.json"
    experiment.write_text(
        json.dumps(
            {
                "development_partition": "human dev",
                "candidate": "calibrated thresholds v2",
                "test_tuning": False,
            }
        )
    )

    baseline = evaluate_predictions(
        gold_path, baseline_predictions, out, minimum_slice_n=4
    )
    candidate = evaluate_predictions(
        gold_path,
        candidate_predictions,
        out,
        baseline_path=out / "baseline-v1.human-eval.json",
        experiment_path=experiment,
        minimum_slice_n=4,
    )
    assert baseline["tasks"]["stance"]["metrics"]["confusion_matrix"]
    assert baseline["tasks"]["frame"]["selective"]["calibration"]["bins"]
    assert baseline["tasks"]["claim"]["threshold_curve"]
    assert (
        baseline["tasks"]["stance"]["metrics"]["confidence_interval_95"]["status"]
        == "reported"
    )
    assert candidate["promotion_gate"]["accepted"] is True
    assert candidate["experiment"]["test_tuning"] is False
    assert candidate["human_and_synthetic_separated"] is True
    assert candidate["slices"]["language"]["de"]["n"] == 8


def test_prediction_ids_and_contract_are_strict(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.jsonl"
    gold = _gold(gold_path, n=12)
    predictions = tmp_path / "predictions.csv"
    _predictions(predictions, gold[:-1], model="incomplete", errors=0)
    with pytest.raises(HumanEvalError, match="match the test split exactly"):
        evaluate_predictions(gold_path, predictions, tmp_path / "reports")
    with pytest.raises(HumanEvalError, match="thresholds must be between"):
        predict_gold(gold_path, predictions, stance_threshold=1.1)

    root = Path(__file__).parents[3]
    schema = json.loads(
        (root / "contracts/schemas/jsonschema/noesis-human-eval-v1.json").read_text()
    )
    status = json.loads(
        (root / "data/argument_mining/human_eval/status.json").read_text()
    )
    Draft7Validator(schema).validate(status)
    invalid = json.loads(
        (
            root
            / "contracts/examples/noesis-human-eval-v1/invalid-simulated-human.json"
        ).read_text()
    )
    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(invalid)

"""Published labels survive bounded dataset selection without synthetic filling."""

import hashlib
import json
import zipfile

import duckdb
import pytest

from scripts import evaluate_published_retrieval as benchmark


def test_selection_preserves_judgments_and_rejects_whole_overlong_case(
    tmp_path, monkeypatch
):
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE cases(question VARCHAR, positive_ctxs STRUCT(text VARCHAR[]), hard_negative_ctxs STRUCT(text VARCHAR[]))"
    )
    conn.execute(
        "INSERT INTO cases VALUES (?, ?, ?)",
        ["excluded question", {"text": ["x" * 1001]}, {"text": ["short negative"]}],
    )
    for i in range(8):
        conn.execute(
            "INSERT INTO cases VALUES (?, ?, ?)",
            [
                f"Frage {i}",
                {"text": [f"positive {i}"]},
                {"text": [f"negative {i}"] + (["x" * 1001] if i == 0 else [])},
            ],
        )
    parquet = tmp_path / "germandpr-test.parquet"
    conn.execute("COPY cases TO ? (FORMAT PARQUET)", [str(parquet)])
    conn.close()
    with zipfile.ZipFile(tmp_path / "scifact.zip", "w") as archive:
        archive.writestr(
            "scifact/corpus.jsonl",
            "\n".join(
                json.dumps(
                    {"_id": str(i), "title": f"Paper {i}", "text": f"Abstract {i}"}
                )
                for i in range(100)
            ),
        )
        archive.writestr(
            "scifact/queries.jsonl",
            "\n".join(
                json.dumps({"_id": str(i), "text": f"Query {i}"}) for i in range(8)
            ),
        )
        archive.writestr(
            "scifact/qrels/test.tsv",
            "query-id\tcorpus-id\tscore\n"
            + "\n".join(f"{i}\t{i}\t1" for i in range(8)),
        )
    monkeypatch.setattr(
        benchmark,
        "SOURCES",
        {
            name: {"sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
            for name in benchmark.SOURCES
        },
    )
    first = benchmark.prepare(tmp_path, lambda text: len(text) <= 1000)
    assert first == benchmark.prepare(tmp_path, lambda text: len(text) <= 1000)
    assert len(first["queries"]) == 16
    assert len(first["documents"]) == 96
    assert first["queries"][0]["omitted_overlong_hard_negatives"] == 1
    assert "excluded question" not in {q["text"] for q in first["queries"]}
    assert "short negative" not in {d["text"] for d in first["documents"]}
    for query in first["queries"]:
        assert sum(query["judgments"].values()) == 1
        assert set(query["judgments"]) <= {d["id"] for d in first["documents"]}
    assert first["queries"][8]["judgments"] == {"scifact:0": 1}
    parquet.write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        benchmark.prepare(tmp_path, lambda _: True)


def test_missing_dataset_is_an_explicit_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "verified_bytes", lambda *_: b"")
    # Missing source files remain an explicit failure, never an empty successful run.
    with pytest.raises(duckdb.IOException):
        benchmark.prepare(tmp_path, lambda _: False)


def test_metric_crosscheck_detects_incorrect_scores():
    pytest.importorskip("ir_measures")
    results = [{"id": "unjudged"}, {"id": "relevant"}]
    judgments = {"relevant": 1, "known-negative": 0}
    row = {
        "query_id": "q",
        "judgments": judgments,
        "job": {"result": {"results": results}},
        "metrics": {
            str(k): benchmark.score_output("ranking", results, judgments, {"k": k})
            for k in (5, 10, 30)
        },
    }
    report = {"runs": [row]}
    assert benchmark.verify_metrics(report)["checked_scores"] == 9
    row["metrics"]["5"]["ndcg_at_k"] = 1.0
    with pytest.raises(ValueError, match="disagrees"):
        benchmark.verify_metrics(report)

import sys
from types import SimpleNamespace

import pytest


def test_bge_streams_corpus_and_scores_each_representation(monkeypatch):
    pytest.importorskip("duckdb")
    pytest.importorskip("numpy")
    from src.evaluation import model_backends
    from src.evaluation.retrieval_batch import run

    batches = []

    class Model:
        space_id = "a" * 64

        def encode(self, texts):
            batches.append(len(texts))
            return [
                {
                    "space_id": self.space_id,
                    "dense": [1.0, 0.0] if text in {"query", "0"} else [0.0, 1.0],
                    "sparse": {
                        "term": 1.0 if text == "query" else 2.0 if text == "1" else 0.0
                    },
                    "multivector": [[0.0, 1.0]]
                    if text in {"query", "1"}
                    else [[1.0, 0.0]],
                }
                for text in texts
            ]

    monkeypatch.setattr(model_backends, "BGEBackend", Model)
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=None)
    )
    result = run(
        {
            "backend": "bge-m3",
            "documents": [
                {
                    "id": str(i),
                    "revision": "revision",
                    "text": str(i),
                    "source": "fixture",
                }
                for i in range(9)
            ],
            "queries": [
                {
                    "id": "q",
                    "text": "query",
                    "language_pair": "de-en",
                    "judgments": {"1": 1},
                }
            ],
        }
    )
    assert batches == [4, 4, 1, 1]
    winners = {
        r["mode"]: r["job"]["result"]["results"][0]["id"] for r in result["runs"]
    }
    assert winners == {"dense": "0", "dense-sparse": "1", "multivector": "1"}
    assert result["serialized_index_bytes"] > 0
    assert result["production_index_modified"] is False

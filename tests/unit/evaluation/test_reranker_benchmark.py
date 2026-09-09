import hashlib

import pytest

from src.evaluation.reranker_benchmark import validate


def payload():
    return {
        "backend": "qwen3-reranker",
        "queries": [
            {
                "id": "q1",
                "text": "question",
                "candidates": [
                    {
                        "id": "d1",
                        "text": "evidence",
                        "revision": hashlib.sha256(b"evidence").hexdigest(),
                    }
                ],
            }
        ],
    }


def test_revision_bound_candidate_pool():
    value = payload()
    assert validate(value)[2] == 2
    value["queries"][0]["candidates"][0]["text"] = "changed evidence"
    with pytest.raises(ValueError, match="revision"):
        validate(value)


def test_duplicate_candidates_cannot_bias_ranking():
    value = payload()
    rows = value["queries"][0]["candidates"]
    rows.append(dict(rows[0]))
    with pytest.raises(ValueError, match="unique"):
        validate(value)


@pytest.mark.parametrize("batch", [True, 0, 9, 1.5])
def test_invalid_batches_rejected_before_model_loading(batch):
    value = payload()
    value["batch_size"] = batch
    with pytest.raises(ValueError, match="batch size"):
        validate(value)

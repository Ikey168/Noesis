"""Native-call boundary tests with explicit test doubles, not model quality claims."""

from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from services.embeddings.provider import EmbeddingProvider
from src.argument_mining.model_registry import OPTIONAL_PINS, optional_model_spec
from src.evaluation.model_backends import (
    BGEBackend,
    E5Backend,
    GLiNERBackend,
    ModelSpaceIndex,
    QwenReranker,
    SaTSegmenter,
    representation_scores,
    space_identity,
)
from src.evaluation.runtime_errors import BackendError

REVISION = "a" * 40


class Tokenizer:
    def encode(self, text, **kwargs):
        return list(range(len(text.split()) + 2))


class SentenceModel:
    tokenizer = Tokenizer()

    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, texts, **kwargs):
        self.seen = texts
        assert kwargs["normalize_embeddings"]
        return np.array([[1.0, 0.0] for _ in texts])


def test_e5_prefixes_provider_empty_and_token_limits():
    model = SentenceModel()
    provider = EmbeddingProvider("e5", model=model)
    assert provider.embed_texts(["Beleg"]).shape == (1, 2)
    assert model.seen == ["passage: Beleg"]
    assert provider.embed_queries(["Frage"]).shape == (1, 2)
    assert model.seen == ["query: Frage"]
    assert provider.embed_texts([]).shape == (0, 2)
    backend = E5Backend(model=model, max_tokens=8)
    with pytest.raises(BackendError, match="split"):
        backend.embed_texts(["word " * 10])
    with pytest.raises(BackendError):
        space_identity("model", "main")


def test_bge_native_modalities_and_no_space_mixing():
    class Model:
        tokenizer = Tokenizer()

        def encode(self, texts, **kwargs):
            assert kwargs["return_colbert_vecs"] and kwargs["return_sparse"]
            return {
                "dense_vecs": [[1.0, 0.0]],
                "lexical_weights": [{"12": 0.5}],
                "colbert_vecs": [[[1.0, 0.0], [0.0, 1.0]]],
            }

    backend = BGEBackend(model=Model())
    row = backend.encode(["Beleg"])[0]
    assert representation_scores(row, row) == {
        "dense": 1.0,
        "sparse": 0.25,
        "multivector": 1.0,
    }
    with pytest.raises(BackendError, match="compatibility"):
        representation_scores(row, {**row, "space_id": "x"})


def test_index_restart_revision_delete_and_equal_dimension_rejection(tmp_path):
    path = str(tmp_path / "index.duckdb")
    space = space_identity("test", REVISION, mode="dense")
    conn = duckdb.connect(path)
    idx = ModelSpaceIndex(conn, space, max_records=1)
    row = {"space_id": space, "dense": [1.0, 0.0]}
    idx.upsert("d1", "r1", row, provenance={"source": "s1"})
    idx.upsert("d1", "r2", row, provenance={"source": "s1"})
    with pytest.raises(BackendError):
        idx.upsert("d2", "r1", row, provenance={})
    conn.close()
    conn = duckdb.connect(path)
    idx = ModelSpaceIndex(conn, space)
    result = idx.search(row, limit=30)[0]
    assert result["revision"] == "r2" and result["provenance"] == {"source": "s1"}
    with pytest.raises(BackendError):
        idx.search({**row, "space_id": space_identity("different", REVISION)})
    idx.remove("d1")
    assert idx.search(row) == []
    conn.close()


def test_gliner_executes_native_span_api_and_rejects_changed_text():
    class Model:
        def extract_entities(self, text, labels, **kwargs):
            assert kwargs == {"include_spans": True, "include_confidence": True}
            return {
                "entities": {
                    "person": [
                        {"start": 0, "end": 6, "text": "Müller", "confidence": 0.9}
                    ]
                }
            }

    result = GLiNERBackend(model=Model()).extract(
        "Müller", ["person"], source_id="s", source_revision="r", language="de"
    )
    assert result["entities"][0]["end"] == 6
    assert result["model_revision"] == optional_model_spec("gliner2")["revision"]
    with pytest.raises(ValueError):
        GLiNERBackend(model=Model()).extract(
            "Berlin", ["person"], source_id="s", source_revision="r", language="de"
        )


def test_sat_preserves_original_offsets_and_rejects_tail_loss():
    class Model:
        def split(self, text, **kwargs):
            return ["Müller. ", "Weiter!"]

    rows = SaTSegmenter(model=Model()).segment("Müller. Weiter!")
    assert [(r["start"], r["end"]) for r in rows] == [(0, 8), (8, 15)]
    with pytest.raises(BackendError, match="tail"):
        SaTSegmenter(model=Model()).segment("Müller. Weiter! Lost")


def test_qwen_uses_last_token_yes_no_logits_and_stable_ids():
    torch = pytest.importorskip("torch")

    class QTokenizer:
        def encode(self, text, **kwargs):
            if text in {"yes", "no"}:
                return [2 if text == "yes" else 1]
            assert "<Instruct>:" in text and "</think>" in text
            return [0, 0, 0]

        def pad(self, rows, **kwargs):
            return {
                "input_ids": torch.tensor([row["input_ids"] for row in rows]),
                "attention_mask": torch.tensor([row["attention_mask"] for row in rows]),
            }

    class Model:
        device = "cpu"

        def __call__(self, **kwargs):
            assert kwargs["logits_to_keep"] == 1
            assert kwargs["use_cache"] is False
            output = torch.zeros((len(kwargs["input_ids"]), 1, 4))
            output[:, -1, 2] = 2
            return SimpleNamespace(logits=output)

    backend = QwenReranker(model=Model(), tokenizer=QTokenizer())
    rows = backend.rank_records(
        "query",
        [
            {"id": "a", "text": "yes", "source": "one"},
            {"id": "b", "text": "yes", "source": "two"},
        ],
    )
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[0]["relevance_score"] == pytest.approx(0.880797, rel=1e-5)
    assert not rows[0]["support_verified"]


def test_all_optional_models_have_immutable_pins():
    for key in OPTIONAL_PINS:
        spec = optional_model_spec(key)
        assert len(space_identity(spec["model"], spec["revision"])) == 64


def test_qwen_rejects_overlong_prompt_before_inference():
    pytest.importorskip("torch")

    class Tokenizer:
        def encode(self, text, **kwargs):
            if text in {"yes", "no"}:
                return [2 if text == "yes" else 1]
            return list(range(65))

    class Model:
        def __call__(self, **kwargs):
            pytest.fail("overlong input reached model")

    backend = QwenReranker(model=Model(), tokenizer=Tokenizer(), max_tokens=64)
    with pytest.raises(BackendError, match="no silent truncation"):
        backend.predict([("query", "document")])

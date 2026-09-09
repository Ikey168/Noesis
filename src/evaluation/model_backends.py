"""Executable optional model backends; production defaults are unchanged.

Weights come only from immutable pins in argument_mining.model_registry. Fetching
is a separate explicit operation. Use runtime_jobs for process deadlines around
large model operations. Missing models fail closed, never as fixture predictions.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.evaluation.runtime_errors import BackendError


def model_path(kind: str) -> tuple[str, dict[str, Any]]:
    from src.argument_mining.model_registry import (
        optional_model_path,
        optional_model_spec,
    )

    spec = optional_model_spec(kind)
    path = optional_model_path(kind)
    if path is None:
        raise BackendError(
            "model_unavailable", f"fetch the pinned {kind} snapshot explicitly"
        )
    return str(path), spec


def bounded_texts(
    texts: Sequence[str], *, max_records: int = 256, max_chars: int = 262144
) -> list[str]:
    if isinstance(texts, (str, bytes)) or len(texts) > max_records:
        raise BackendError("input_limit", "too many model inputs")
    if any(not isinstance(text, str) or len(text) > max_chars for text in texts):
        raise BackendError("input_limit", "model input must be bounded text")
    return list(texts)


def space_identity(model: str, revision: str, **configuration: Any) -> str:
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise BackendError(
            "unpinned_model", "a full immutable model revision is required"
        )
    raw = json.dumps([model, revision, configuration], sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def check_tokens(tokenizer: Any, texts: Sequence[str], max_tokens: int) -> None:
    for text in texts:
        if (
            len(tokenizer.encode(text, add_special_tokens=True, truncation=False))
            > max_tokens
        ):
            raise BackendError(
                "token_limit",
                "split the source into revision-linked chunks before inference",
            )


class E5Backend:
    """SentenceTransformer execution with mandatory query/passage prefixes."""

    def __init__(self, *, model=None, revision=None, max_tokens=512, device="cpu"):
        from src.argument_mining.model_registry import optional_model_spec

        spec = optional_model_spec("e5")
        self.revision = revision or spec["revision"]
        if type(max_tokens) is not int or not 8 <= max_tokens <= 512:
            raise BackendError("token_limit", "E5 supports at most 512 input tokens")
        self.max_tokens = max_tokens
        self.space_id = space_identity(
            spec["model"],
            self.revision,
            prefix="query:/passage:",
            normalized=True,
            max_tokens=max_tokens,
        )
        if model is None:
            from sentence_transformers import SentenceTransformer

            path, _ = model_path("e5")
            if self.revision != spec["revision"]:
                raise BackendError(
                    "unpinned_model",
                    "change the registry pin before selecting another model revision",
                )
            model = SentenceTransformer(
                path, device=device, local_files_only=True, trust_remote_code=False
            )
        self.model = model
        self.model.max_seq_length = max_tokens

    def _encode(self, texts, prefix):
        import numpy as np

        prepared = [prefix + text for text in bounded_texts(texts)]
        check_tokens(self.model.tokenizer, prepared, self.max_tokens)
        if not prepared:
            return np.empty((0, self.dim()), dtype=np.float32)
        vectors = np.asarray(
            self.model.encode(
                prepared,
                batch_size=8,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        )
        if (
            vectors.shape != (len(prepared), self.dim())
            or not np.isfinite(vectors).all()
        ):
            raise BackendError("invalid_model_output", "invalid E5 embedding matrix")
        return vectors

    def embed_texts(self, texts):
        return self._encode(texts, "passage: ")

    def embed_queries(self, texts):
        return self._encode(texts, "query: ")

    def dim(self):
        return int(self.model.get_sentence_embedding_dimension())

    def name(self):
        return "multilingual-e5-small:" + self.revision

    def count_tokens(self, text):
        return len(
            self.model.tokenizer.encode(
                "passage: " + text, add_special_tokens=True, truncation=False
            )
        )

    def token_limit(self):
        return self.max_tokens

    def tokenizer_identity(self):
        return {
            "name": "intfloat/multilingual-e5-small",
            "revision": self.revision,
            "max_tokens": self.max_tokens,
            "space_id": self.space_id,
            "special_tokens_included": True,
        }


class BGEBackend:
    """Execute all three BGEM3FlagModel representations; never mix index spaces."""

    def __init__(self, *, model=None, revision=None, max_tokens=512, device="cpu"):
        from src.argument_mining.model_registry import optional_model_spec

        spec = optional_model_spec("bge-m3")
        self.revision = revision or spec["revision"]
        if type(max_tokens) is not int or not 8 <= max_tokens <= 8192:
            raise BackendError("token_limit", "invalid BGE token limit")
        self.max_tokens = max_tokens
        self.space_id = space_identity(
            spec["model"],
            self.revision,
            max_tokens=max_tokens,
            modalities=["dense", "sparse", "multivector"],
        )
        if model is None:
            from FlagEmbedding import BGEM3FlagModel

            path, _ = model_path("bge-m3")
            if self.revision != spec["revision"]:
                raise BackendError(
                    "unpinned_model",
                    "change the registry pin before selecting another model revision",
                )
            model = BGEM3FlagModel(path, use_fp16=device != "cpu", devices=device)
        self.model = model

    def encode(self, texts):
        import numpy as np

        texts = bounded_texts(texts, max_records=128)
        check_tokens(self.model.tokenizer, texts, self.max_tokens)
        if not texts:
            return []
        output = self.model.encode(
            texts,
            batch_size=4,
            max_length=self.max_tokens,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=True,
        )
        if any(
            len(output.get(key, [])) != len(texts)
            for key in ("dense_vecs", "lexical_weights", "colbert_vecs")
        ):
            raise BackendError("invalid_model_output", "BGE lost input alignment")
        rows = []
        for dense, sparse, multi in zip(
            output["dense_vecs"],
            output["lexical_weights"],
            output["colbert_vecs"],
            strict=True,
        ):
            dense, multi = np.asarray(dense), np.asarray(multi)
            if dense.ndim != 1 or multi.ndim != 2 or len(multi) > self.max_tokens:
                raise BackendError(
                    "invalid_model_output", "invalid BGE representations"
                )
            if not np.isfinite(dense).all() or not np.isfinite(multi).all():
                raise BackendError(
                    "invalid_model_output", "non-finite BGE representation"
                )
            weights = {str(key): float(value) for key, value in sparse.items()}
            if any(not math.isfinite(value) or value < 0 for value in weights.values()):
                raise BackendError("invalid_model_output", "invalid BGE sparse weights")
            rows.append(
                {
                    "space_id": self.space_id,
                    "dense": dense.tolist(),
                    "sparse": weights,
                    "multivector": multi.tolist(),
                }
            )
        return rows

    def embed_texts(self, texts):
        import numpy as np

        rows = self.encode(texts)
        return (
            np.asarray([row["dense"] for row in rows])
            if rows
            else np.empty((0, self.dim()))
        )

    embed_queries = embed_texts

    def dim(self):
        return 1024

    def name(self):
        return "bge-m3:" + self.revision

    def count_tokens(self, text):
        return len(
            self.model.tokenizer.encode(text, add_special_tokens=True, truncation=False)
        )

    def token_limit(self):
        return self.max_tokens

    def tokenizer_identity(self):
        return {
            "name": "BAAI/bge-m3",
            "revision": self.revision,
            "space_id": self.space_id,
            "max_tokens": self.max_tokens,
            "special_tokens_included": True,
        }


def representation_scores(query, document):
    """Dense dot, learned sparse dot, and query-token mean MaxSim, separately."""
    import numpy as np

    if query.get("space_id") != document.get("space_id") or not query.get("space_id"):
        raise BackendError(
            "embedding_space_mismatch",
            "reindex; equal dimensions do not establish compatibility",
        )
    q, d = np.asarray(query["dense"]), np.asarray(document["dense"])
    if (
        q.ndim != 1
        or not q.size
        or q.shape != d.shape
        or not np.isfinite(q).all()
        or not np.isfinite(d).all()
    ):
        raise BackendError("invalid_model_output", "incompatible dense vectors")
    sparse = sum(
        float(value) * float(document.get("sparse", {}).get(token, 0))
        for token, value in query.get("sparse", {}).items()
    )
    if not math.isfinite(sparse):
        raise BackendError("invalid_model_output", "invalid sparse similarity")
    qm, dm = (
        np.asarray(query.get("multivector", [])),
        np.asarray(document.get("multivector", [])),
    )
    multi = None
    if qm.size and dm.size:
        if qm.ndim != 2 or dm.ndim != 2 or qm.shape[1] != dm.shape[1]:
            raise BackendError("invalid_model_output", "incompatible multivectors")
        if (
            max(len(qm), len(dm)) > 8192
            or not np.isfinite(qm).all()
            or not np.isfinite(dm).all()
        ):
            raise BackendError("input_limit", "invalid multivector input")
        # Avoid allocating an unbounded query-token by document-token matrix.
        multi = float(
            sum(
                (qm[i : i + 16] @ dm.T).max(axis=1).sum() for i in range(0, len(qm), 16)
            )
            / len(qm)
        )
    return {"dense": float(q @ d), "sparse": float(sparse), "multivector": multi}


class ModelSpaceIndex:
    """Bounded DuckDB evaluation index with explicit model-space and revision keys.

    This transparent exact index is for comparative measurements, not a claim
    that Noesis' production vector store supports sparse or multi-vector search.
    """

    def __init__(self, conn, space_id, *, max_records=10000):
        if (
            not isinstance(space_id, str)
            or len(space_id) != 64
            or not 1 <= max_records <= 10000
        ):
            raise ValueError("invalid index identity or bound")
        self.conn, self.space_id, self.max_records = conn, space_id, max_records
        conn.execute(
            "CREATE TABLE IF NOT EXISTS optional_model_index(space_id TEXT, id TEXT, revision TEXT, payload TEXT, PRIMARY KEY(space_id,id))"
        )

    def upsert(self, identity, revision, representation, *, provenance):
        if representation.get("space_id") != self.space_id:
            raise BackendError("embedding_space_mismatch", "cannot mix model spaces")
        if not identity or not revision or not isinstance(provenance, Mapping):
            raise ValueError("source identity, revision and provenance required")
        representation_scores(representation, representation)
        payload = json.dumps(
            {"representation": representation, "provenance": dict(provenance)},
            allow_nan=False,
        )
        if len(payload.encode()) > 16 * 1024 * 1024:
            raise BackendError("input_limit", "index row too large")
        count = self.conn.execute(
            "SELECT count(*) FROM optional_model_index WHERE space_id=?",
            [self.space_id],
        ).fetchone()[0]
        exists = self.conn.execute(
            "SELECT 1 FROM optional_model_index WHERE space_id=? AND id=?",
            [self.space_id, identity],
        ).fetchone()
        if not exists and count >= self.max_records:
            raise BackendError("input_limit", "evaluation index record limit")
        self.conn.execute(
            "INSERT OR REPLACE INTO optional_model_index VALUES (?,?,?,?)",
            [self.space_id, identity, revision, payload],
        )

    def remove(self, identity):
        self.conn.execute(
            "DELETE FROM optional_model_index WHERE space_id=? AND id=?",
            [self.space_id, identity],
        )

    def search(self, representation, *, limit=20, weights=None):
        if representation.get("space_id") != self.space_id:
            raise BackendError(
                "embedding_space_mismatch", "query model space differs from index"
            )
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid result bound")
        weights = weights or {"dense": 1.0}
        if (
            not set(weights) <= {"dense", "sparse", "multivector"}
            or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                for v in weights.values()
            )
            or not sum(weights.values())
        ):
            raise ValueError("explicit finite nonnegative score weights required")
        rows = []
        identities = self.conn.execute(
            "SELECT id,revision FROM optional_model_index WHERE space_id=? ORDER BY id",
            [self.space_id],
        ).fetchall()
        for identity, revision in identities:
            # Multi-vector payloads can be several MiB each. Read one record at
            # a time instead of materializing the whole corpus into Python.
            payload = self.conn.execute(
                "SELECT payload FROM optional_model_index WHERE space_id=? AND id=?",
                [self.space_id, identity],
            ).fetchone()[0]
            value = json.loads(payload)
            scores = representation_scores(representation, value["representation"])
            if any(scores[key] is None for key in weights):
                raise BackendError(
                    "unsupported_representation",
                    "requested scoring mode was not indexed",
                )
            rows.append(
                {
                    "id": identity,
                    "revision": revision,
                    "scores": scores,
                    "score": sum(
                        scores[key] * weight for key, weight in weights.items()
                    ),
                    "provenance": value["provenance"],
                    "space_id": self.space_id,
                }
            )
        return sorted(rows, key=lambda row: (-row["score"], row["id"]))[:limit]


class QwenReranker:
    """Dedicated yes/no causal-LM relevance scoring, not CrossEncoder guessing."""

    PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. "
        'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    )
    SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, *, model=None, tokenizer=None, max_tokens=2048, device="cpu"):
        from src.argument_mining.model_registry import optional_model_spec

        self.spec = optional_model_spec("qwen3-reranker")
        if type(max_tokens) is not int or not 64 <= max_tokens <= 8192:
            raise ValueError("Qwen reranker token budget must be 64..8192")
        self.max_tokens = max_tokens
        if model is None or tokenizer is None:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            path, _ = model_path("qwen3-reranker")
            tokenizer = AutoTokenizer.from_pretrained(
                path,
                padding_side="left",
                local_files_only=True,
                trust_remote_code=False,
            )
            model = (
                AutoModelForCausalLM.from_pretrained(
                    path,
                    local_files_only=True,
                    trust_remote_code=False,
                    dtype=torch.float32 if str(device) == "cpu" else "auto",
                )
                .to(device)
                .eval()
            )
        self.model, self.tokenizer = model, tokenizer
        tokenizer.padding_side = "left"
        self.no_id, self.yes_id = [
            tokenizer.encode(word, add_special_tokens=False) for word in ("no", "yes")
        ]
        if len(self.no_id) != 1 or len(self.yes_id) != 1 or self.no_id == self.yes_id:
            raise BackendError(
                "label_mapping", "yes/no must map to distinct single tokens"
            )

    def predict(self, pairs, *, batch_size=4):
        import torch

        if type(batch_size) is not int or not 1 <= batch_size <= 32 or len(pairs) > 256:
            raise BackendError("input_limit", "reranking batch/candidate limit")
        scores = []
        for start in range(0, len(pairs), batch_size):
            rows = []
            for query, document in pairs[start : start + batch_size]:
                bounded_texts([query, document])
                prompt = (
                    self.PREFIX
                    + "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n<Query>: "
                    + query
                    + "\n<Document>: "
                    + document
                    + self.SUFFIX
                )
                ids = self.tokenizer.encode(
                    prompt, add_special_tokens=False, truncation=False
                )
                if len(ids) > self.max_tokens:
                    raise BackendError(
                        "token_limit",
                        "reranker input exceeds configured limit; no silent truncation",
                    )
                rows.append({"input_ids": ids, "attention_mask": [1] * len(ids)})
            inputs = self.tokenizer.pad(rows, padding=True, return_tensors="pt")
            inputs = {key: value.to(self.model.device) for key, value in inputs.items()}
            with torch.inference_mode():
                logits = self.model(**inputs, logits_to_keep=1, use_cache=False).logits[
                    :, -1, [self.no_id[0], self.yes_id[0]]
                ]
                scores.extend(torch.softmax(logits, dim=-1)[:, 1].tolist())
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores):
            raise BackendError("invalid_model_output", "non-finite relevance score")
        return scores

    def rank_records(self, query, candidates, *, limit=None):
        if len({item["id"] for item in candidates}) != len(candidates):
            raise ValueError("unique candidate IDs required")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("invalid result limit")
        scores = self.predict(
            [(query, item.get("text", item.get("content", ""))) for item in candidates]
        )
        rows = [
            {
                **item,
                "relevance_score": score,
                "input_rank": rank,
                "model_revision": self.spec["revision"],
                "support_verified": False,
            }
            for rank, (item, score) in enumerate(zip(candidates, scores, strict=True))
        ]
        return sorted(
            rows, key=lambda row: (-row["relevance_score"], row["input_rank"])
        )[:limit]


class GLiNERBackend:
    """Execute GLiNER2 with native exact spans and immutable model attribution."""

    def __init__(self, *, model=None, max_chars=8000):
        from src.argument_mining.model_registry import optional_model_spec

        self.spec = optional_model_spec("gliner2")
        if type(max_chars) is not int or not 1 <= max_chars <= 262144:
            raise ValueError("invalid extraction character limit")
        self.max_chars = max_chars
        if model is None:
            from gliner2 import GLiNER2

            path, _ = model_path("gliner2")
            model = GLiNER2.from_pretrained(
                str(path), local_files_only=True, map_location="cpu"
            )
        self.model = model

    def extract_schema(
        self, text, schema, *, source_id, source_revision, language, threshold=0.5
    ):
        from src.evaluation.gliner_schema import extract_schema

        return extract_schema(
            self,
            text,
            schema,
            source_id=source_id,
            source_revision=source_revision,
            language=language,
            threshold=threshold,
        )

    def extract(self, text, labels, *, source_id, source_revision, language):
        from src.evaluation.workflow_review import adapt_entity_spans

        bounded_texts([text], max_chars=self.max_chars)
        if (
            not source_id
            or not source_revision
            or not language
            or not 1 <= len(labels) <= 64
        ):
            raise ValueError("bounded label schema and source provenance required")
        if len(set(labels)) != len(labels) or any(
            not isinstance(v, str) or not v.strip() for v in labels
        ):
            raise ValueError("nonempty distinct labels required")
        raw = self.model.extract_entities(
            text, list(labels), include_spans=True, include_confidence=True
        )
        entities = []
        for label, values in raw["entities"].items():
            for value in values:
                if (
                    not isinstance(value, dict)
                    or not {"start", "end", "text"} <= value.keys()
                ):
                    raise BackendError(
                        "span_unavailable",
                        "backend did not return exact source offsets",
                    )
                entities.append({**value, "label": label})
        return adapt_entity_spans(
            source_id=source_id,
            source_revision=source_revision,
            text=text,
            model=self.spec["model"],
            model_revision=self.spec["revision"],
            language=language,
            entities=entities,
            supported_labels=labels,
        )


class SaTSegmenter:
    """Model-backed segmentation, reconstructing exact source character offsets."""

    def __init__(self, *, model=None, threshold=0.5):
        if not 0 < threshold < 1:
            raise ValueError("sentence threshold must lie between zero and one")
        self.threshold = threshold
        if model is None:
            from wtpsplit import SaT

            path, _ = model_path("sat")
            model = SaT(path)
        self.model = model

    def segment(self, text):
        bounded_texts([text])
        pieces = list(self.model.split(text, threshold=self.threshold))
        cursor, rows = 0, []
        for piece in pieces:
            if not isinstance(piece, str) or not piece:
                raise BackendError("span_unavailable", "invalid segmentation output")
            start = text.find(piece, cursor)
            if start < 0 or text[cursor:start].strip():
                raise BackendError(
                    "span_unavailable", "segmenter changed or omitted source text"
                )
            end = start + len(piece)
            rows.append({"text": piece, "start": start, "end": end})
            cursor = end
        if text[cursor:].strip():
            raise BackendError("span_unavailable", "segmenter omitted source tail")
        return rows

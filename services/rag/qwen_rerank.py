"""Deadline-enforced optional Qwen adapter for the existing reranking interface."""

from __future__ import annotations

import math

from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_jobs import execute_job


class BoundedQwenReranker:
    """Run the dedicated causal-LM scorer in a disposable bounded process.

    Explicit selection enables this adapter. Default rerankers and score fusion
    are unchanged; raw relevance scores never count as evidence entailment.
    """

    def __init__(
        self,
        *,
        timeout_s=15.0,
        max_tokens=2048,
        max_rss_bytes=4 * 1024**3,
        executor=None,
    ):
        self.timeout_s, self.max_tokens, self.max_rss_bytes = (
            timeout_s,
            max_tokens,
            max_rss_bytes,
        )
        self.executor = executor or execute_job
        self.is_enabled = True
        self.last_diagnostics = None

    def rerank(
        self,
        query,
        candidates,
        top_k=None,
        score_fusion="rerank_only",
        fusion_weight=0.7,
        require_model=False,
        *,
        cancelled=None,
    ):
        from services.rag.rerank import RerankResult

        if (
            len(candidates) > 256
            or top_k is not None
            and (type(top_k) is not int or top_k < 1)
        ):
            raise ValueError("invalid reranking candidate/result bound")
        if (
            score_fusion not in {"rerank_only", "weighted", "max", "product"}
            or not 0 <= fusion_weight <= 1
        ):
            raise ValueError("invalid score-fusion configuration")
        records = [
            {
                **item,
                "id": str(item.get("id") or f"rank:{rank}"),
                "text": (
                    item.get("title", "")
                    + " "
                    + item.get("content", item.get("text", ""))
                ).strip(),
                "original_index": rank,
            }
            for rank, item in enumerate(candidates)
        ]
        if len({row["id"] for row in records}) != len(records):
            raise ValueError("unique candidate IDs required")
        if not candidates:
            return []
        result = self.executor(
            "qwen3-reranker",
            {"query": query, "candidates": records, "max_tokens": self.max_tokens},
            timeout_s=self.timeout_s,
            max_rss_bytes=self.max_rss_bytes,
            cancelled=cancelled,
        )
        self.last_diagnostics = {
            key: value for key, value in result.items() if key != "result"
        }
        if result["status"] != "completed":
            # Never silently turn model unavailability into an apparent learned
            # ranking. HybridRetriever already exposes partial-source failures.
            raise BackendError(
                result.get("failure_code", "reranker_unavailable"),
                "optional reranker failed; inspect diagnostics",
            )
        output = []
        rows = result["result"]["results"]
        if len(rows) != len(records) or {r["id"] for r in rows} != {
            r["id"] for r in records
        }:
            raise BackendError(
                "invalid_model_output", "reranker dropped or changed candidate IDs"
            )
        for row in rows:
            original = int(row["original_index"])
            if not 0 <= original < len(records) or row["id"] != records[original]["id"]:
                raise BackendError(
                    "invalid_model_output", "reranker changed input alignment"
                )
            item = candidates[original]
            old = float(
                item.get("score", item.get("similarity_score", item.get("rank", 0.0)))
            )
            new = float(row["relevance_score"])
            if not math.isfinite(old) or not math.isfinite(new) or not 0 <= new <= 1:
                raise BackendError("invalid_model_output", "invalid reranking score")
            score = {
                "rerank_only": new,
                "weighted": fusion_weight * old + (1 - fusion_weight) * new,
                "max": max(old, new),
                "product": old * new,
            }[score_fusion]
            output.append(
                RerankResult(
                    original,
                    old,
                    new,
                    score,
                    item.get("content", ""),
                    item.get("title", ""),
                    item.get("source", ""),
                    item.get("url", ""),
                )
            )
        return sorted(
            output, key=lambda item: (-item.final_score, item.original_index)
        )[:top_k]

    def get_model_info(self):
        from src.argument_mining.model_registry import optional_model_spec

        return {
            **optional_model_spec("qwen3-reranker"),
            "is_enabled": True,
            "execution": "bounded-local-process",
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
            "diagnostics": self.last_diagnostics,
        }

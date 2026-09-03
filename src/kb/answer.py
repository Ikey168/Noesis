"""Deterministic, extractive answers over a resolved knowledge-domain backing.

The engine deliberately does not generate prose.  It selects already-extracted
claims (or, when no claims exist, document titles), preserves their evidence,
and renders only those statements.  That makes the offline path reproducible
and prevents an optional language model from introducing uncited facts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.osint.evidence import citation, render_state

ANSWER_CONTRACT_VERSION = "noesis-answer-v1"
PREDICTION_MODE = "deterministic-extractive"
METHOD = "deterministic token-overlap evidence planning with extractive rendering"
ASSUMPTIONS = [
    "token overlap is a relevance heuristic and does not establish factual truth",
    "a citation proves where a statement appeared, not that the statement is true",
    "source independence is currently distinct source identity, not reporting origin",
    "contradicted means relevant conflicting evidence exists, not that Noesis adjudicated truth",
]

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "say",
    "says",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if token not in _STOPWORDS and (len(token) > 1 or token.isdigit())
    }


def _relevance(question_tokens: set[str], text: str) -> float:
    candidate = _tokens(text)
    if not question_tokens or not candidate:
        return 0.0
    overlap = question_tokens & candidate
    if not overlap:
        return 0.0
    query_coverage = len(overlap) / len(question_tokens)
    candidate_precision = len(overlap) / len(candidate)
    return round(0.8 * query_coverage + 0.2 * candidate_precision, 6)


def _stable_id(question: str, kind: str, identity: str) -> str:
    source = f"{question.strip().casefold()}|{kind}|{identity}"
    return "stmt:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _document_map(
    documents: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(document["document_id"]): dict(document)
        for document in documents
        if document.get("document_id")
    }


def _locator(
    item: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    *,
    excerpt: str | None = None,
    visibility: str = "public",
) -> dict[str, Any]:
    document_id = item.get("document_id")
    document = documents.get(str(document_id), {}) if document_id else {}
    source = item.get("source") or item.get("source_id") or document.get("source_id")
    url = item.get("url") or document.get("url")
    resolved = bool(document_id and (document or item.get("source") or item.get("url")))
    result = citation(document_id, source, url, resolved=resolved)
    result.update(
        {
            "path": item.get("path") or result.get("path"),
            "title": item.get("title") or document.get("title"),
            "excerpt": excerpt or item.get("claim_text") or item.get("excerpt"),
            "claim_id": item.get("claim_id"),
            "visibility": visibility,
        }
    )
    return result


def _dedupe_locators(
    locators: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for locator in locators:
        marker = (
            locator.get("document_id"),
            locator.get("claim_id"),
            locator.get("path"),
            locator.get("excerpt"),
        )
        if marker not in seen:
            seen.add(marker)
            found.append(locator)
    return found


def _independence(evidence: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(evidence)
    sources = {
        str(row.get("source")).strip().casefold()
        for row in rows
        if row.get("cited") and row.get("source") not in {None, "", "unknown"}
    }
    unresolved = sum(
        1
        for row in rows
        if row.get("cited") and row.get("source") in {None, "", "unknown"}
    )
    return {
        "n": len(rows),
        "publication_count": sum(1 for row in rows if row.get("cited")),
        "independent_source_count": len(sources),
        "unresolved_count": unresolved,
        "method": "distinct-source",
        "assumptions": [
            "documents with the same normalized source identity count once",
            "syndication and common reporting origin are not yet inferred",
        ],
    }


def _citation_index(
    clusters: Iterable[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    visibility: str,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        for member in cluster.get("citations", []):
            claim_id = member.get("claim_id")
            if claim_id:
                index[str(claim_id)] = _locator(
                    member, documents, visibility=visibility
                )
    return index


def _quantitative_check(
    backing: Any, claim_id: str | None
) -> dict[str, Any] | None:
    if not claim_id or not hasattr(backing, "quantitative_check"):
        return None
    return backing.quantitative_check(str(claim_id))


def _integrity_evidence(
    backing: Any,
    evidence: Iterable[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    document_ids = [
        str(row["document_id"])
        for row in evidence
        if row.get("cited") and row.get("document_id")
    ]
    visibility = (
        "private"
        if "private" in {str(tag).casefold() for tag in backing.definition.tags}
        else "public"
    )
    if not document_ids:
        return {
            "n": 0,
            "status": "not_available",
            "findings": [],
            "method": "integrity ledger aggregation v1",
            "assumptions": ["no resolved source document was available to inspect"],
        }
    ledger = backing.integrity_evidence(document_ids)
    findings = []
    for finding in ledger.get("findings", []):
        locators = [
            _locator(locator, documents, visibility=visibility)
            for locator in finding.get("evidence", [])
            if isinstance(locator, dict)
        ]
        findings.append(
            {
                "kind": str(finding.get("kind") or "unknown"),
                "severity": str(finding.get("severity") or "review"),
                "evidence": _dedupe_locators(locators),
            }
        )
    available = any(
        view.get("status") != "not_found" for view in ledger.get("documents", [])
    )
    return {
        "n": int(ledger.get("n") or 0),
        "status": "findings" if findings else ("no_findings" if available else "not_available"),
        "findings": findings,
        "method": str(ledger.get("method") or "integrity ledger aggregation v1"),
        "assumptions": [str(item) for item in ledger.get("assumptions", [])],
    }


def _claim_statement(
    question: str,
    cluster: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    claim_index: Mapping[str, dict[str, Any]],
    backing: Any,
) -> dict[str, Any]:
    representative = dict(cluster.get("representative") or {})
    visibility = (
        "private"
        if "private" in {str(tag).casefold() for tag in backing.definition.tags}
        else "public"
    )
    claim_id = str(representative.get("claim_id") or cluster.get("cluster_id"))
    text = str(representative.get("claim_text") or "").strip()
    supporting = _dedupe_locators(
        _locator(member, documents, visibility=visibility)
        for member in cluster.get("citations", [])
    )
    contradicting: list[dict[str, Any]] = []
    for conflict in cluster.get("contradictions", []):
        conflict_id = str(conflict.get("claim_id") or "")
        resolved = claim_index.get(conflict_id)
        contradicting.append(
            dict(resolved)
            if resolved
            else _locator(
                conflict,
                documents,
                excerpt=conflict.get("claim_text"),
                visibility=visibility,
            )
        )
    contradicting = _dedupe_locators(contradicting)

    quantitative = _quantitative_check(backing, representative.get("claim_id"))
    cited_support = any(row.get("cited") for row in supporting)
    cited_contradiction = any(row.get("cited") for row in contradicting)
    stored_verdict = str(representative.get("verdict") or "").casefold()
    if cited_contradiction:
        verdict = "contradicted"
    elif quantitative and quantitative.get("verdict") in {
        "supported",
        "contradicted",
        "unverifiable",
    }:
        verdict = quantitative["verdict"]
    elif stored_verdict in {"supported", "contradicted", "unverifiable"}:
        verdict = stored_verdict
    elif cited_support:
        verdict = "supported"
    else:
        verdict = "unverifiable"

    independence = _independence(supporting)
    confidence = representative.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = None
    observed = quantitative.get("observed") if quantitative else None
    integrity = _integrity_evidence(
        backing, [*supporting, *contradicting], documents
    )
    return {
        "id": _stable_id(question, "claim", claim_id),
        "claim_id": representative.get("claim_id"),
        "text": text,
        "verdict": verdict,
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
        "citation_state": render_state(independence["independent_source_count"]),
        "corroboration": independence,
        "prediction_mode": representative.get("prediction_mode") or "not_available",
        "confidence": float(confidence) if confidence is not None else None,
        "confidence_scope": (
            "claim_extraction" if confidence is not None else "not_available"
        ),
        "interval": observed if isinstance(observed, dict) else None,
        "quantitative_check": quantitative,
        "integrity": integrity,
        "n": len(supporting) + len(contradicting),
        "method": "extractive selection of an existing claim cluster",
        "assumptions": list(ASSUMPTIONS),
    }


def _document_statement(
    question: str,
    document: Mapping[str, Any],
    backing: Any,
) -> dict[str, Any]:
    document_id = str(document.get("document_id"))
    visibility = (
        "private"
        if "private" in {str(tag).casefold() for tag in backing.definition.tags}
        else "public"
    )
    locator = _locator(
        document,
        {document_id: document},
        excerpt=document.get("title"),
        visibility=visibility,
    )
    supporting = [locator]
    independence = _independence(supporting)
    cited = locator.get("cited") is True
    return {
        "id": _stable_id(question, "document", document_id),
        "claim_id": None,
        "text": str(document.get("title") or "Untitled source document"),
        "verdict": "supported" if cited else "unverifiable",
        "supporting_evidence": supporting,
        "contradicting_evidence": [],
        "citation_state": render_state(independence["independent_source_count"]),
        "corroboration": independence,
        "prediction_mode": PREDICTION_MODE,
        "confidence": None,
        "confidence_scope": "not_available",
        "interval": None,
        "quantitative_check": None,
        "integrity": _integrity_evidence(
            backing, supporting, {document_id: document}
        ),
        "n": 1,
        "method": "extractive selection of a source-document title",
        "assumptions": list(ASSUMPTIONS),
    }


def _refusal_statement(question: str) -> dict[str, Any]:
    return {
        "id": _stable_id(question, "refusal", "insufficient-evidence"),
        "claim_id": None,
        "text": "No relevant evidence was found in the selected domain.",
        "verdict": "unverifiable",
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "citation_state": "uncited",
        "corroboration": {
            "n": 0,
            "publication_count": 0,
            "independent_source_count": 0,
            "unresolved_count": 0,
            "method": "distinct-source",
            "assumptions": ["no relevant candidate passed the evidence-plan threshold"],
        },
        "prediction_mode": PREDICTION_MODE,
        "confidence": None,
        "confidence_scope": "not_available",
        "interval": None,
        "quantitative_check": None,
        "integrity": {
            "n": 0,
            "status": "not_available",
            "findings": [],
            "method": "integrity ledger aggregation v1",
            "assumptions": ["no evidence document was available to inspect"],
        },
        "n": 0,
        "method": "explicit insufficient-evidence refusal",
        "assumptions": list(ASSUMPTIONS),
    }


def _render_statement(statement: Mapping[str, Any]) -> str:
    cited = [
        str(row.get("path"))
        for row in statement.get("supporting_evidence", [])
        if row.get("cited") and row.get("path")
    ]
    contradicted = [
        str(row.get("path"))
        for row in statement.get("contradicting_evidence", [])
        if row.get("cited") and row.get("path")
    ]
    if not cited:
        suffix = "uncited — unverifiable"
    else:
        suffix = f"{statement['verdict']}; evidence: {', '.join(cited)}"
        if contradicted:
            suffix += f"; contradicting: {', '.join(contradicted)}"
    return f"- {statement['text']} — {suffix}"


def build_answer(
    backing: Any,
    question: str,
    *,
    limit: int = 5,
    minimum_relevance: float = 0.34,
) -> dict[str, Any]:
    """Build a ``noesis-answer-v1`` payload from one resolved backing."""
    question = question.strip()
    question_tokens = _tokens(question)
    documents = backing.documents(limit=500)
    documents_by_id = _document_map(documents)
    clusters = backing.claims(limit=500)
    visibility = (
        "private"
        if "private" in {str(tag).casefold() for tag in backing.definition.tags}
        else "public"
    )
    claim_index = _citation_index(clusters, documents_by_id, visibility)

    ranked_claims = []
    for cluster in clusters:
        representative = cluster.get("representative") or {}
        score = _relevance(question_tokens, str(representative.get("claim_text") or ""))
        if score > 0.0 and score >= minimum_relevance:
            identity = str(cluster.get("cluster_id") or representative.get("claim_id"))
            ranked_claims.append((score, identity, cluster))
    ranked_claims.sort(key=lambda item: (-item[0], item[1]))

    selected: list[tuple[str, float, str, Mapping[str, Any]]] = [
        ("claim", score, identity, cluster)
        for score, identity, cluster in ranked_claims[:limit]
    ]
    eligible_count = len(ranked_claims)
    if not selected:
        ranked_documents = []
        for document in documents:
            text = " ".join(
                str(document.get(field) or "") for field in ("title", "content")
            )
            score = _relevance(question_tokens, text)
            if score > 0.0 and score >= minimum_relevance:
                ranked_documents.append(
                    (score, str(document.get("document_id") or ""), document)
                )
        ranked_documents.sort(key=lambda item: (-item[0], item[1]))
        eligible_count = len(ranked_documents)
        selected = [
            ("document", score, identity, document)
            for score, identity, document in ranked_documents[:limit]
        ]

    statements = []
    for kind, _score, _identity, candidate in selected:
        if kind == "claim":
            statements.append(
                _claim_statement(
                    question, candidate, documents_by_id, claim_index, backing
                )
            )
        else:
            statements.append(_document_statement(question, candidate, backing))

    refused = not statements
    if refused:
        statements = [_refusal_statement(question)]
    partial_reasons = []
    if not refused:
        if any(statement["verdict"] == "unverifiable" for statement in statements):
            partial_reasons.append("one_or_more_statements_unverifiable")
        if any(
            locator.get("cited") is not True
            for statement in statements
            for field in ("supporting_evidence", "contradicting_evidence")
            for locator in statement[field]
        ):
            partial_reasons.append("one_or_more_evidence_locators_unresolved")
        if eligible_count > limit:
            partial_reasons.append("output_budget_exhausted")
    rendered = "\n".join(_render_statement(statement) for statement in statements)
    return {
        "answer_contract": ANSWER_CONTRACT_VERSION,
        "question": question,
        "answer_status": (
            "refused" if refused else ("partial" if partial_reasons else "answered")
        ),
        "statements": statements,
        "rendered": rendered,
        "refusal": (
            {
                "code": "insufficient_evidence",
                "message": "No relevant evidence passed the deterministic relevance threshold.",
            }
            if refused
            else None
        ),
        "partial_reasons": partial_reasons,
        "evidence_plan": {
            "n": len(clusters) + len(documents),
            "method": METHOD,
            "assumptions": list(ASSUMPTIONS),
            "minimum_relevance": float(minimum_relevance),
            "question_tokens": sorted(question_tokens),
            "claim_clusters_considered": len(clusters),
            "documents_considered": len(documents),
            "selected": [
                {"kind": kind, "id": identity, "relevance": score}
                for kind, score, identity, _candidate in selected
            ],
        },
        "n": len(statements),
        "method": METHOD,
        "assumptions": list(ASSUMPTIONS),
        "prediction_mode": PREDICTION_MODE,
    }

"""Cross-domain knowledge retrieval, answers, and equivalence links.

The single-domain ``noesis-kb-v1`` calls remain unchanged.  This module
coordinates several resolved :class:`~src.kb.backing.DomainBacking` objects,
normalizes their *ranks* with reciprocal-rank fusion (never their incomparable
raw scores), and keeps domain/backing provenance on every result.

Private domains are fail-closed.  An explicit private-domain request needs
``include_private=True``, an authenticated principal, and an existing domain
grant.  ``all_authorized=True`` silently omits domains the principal cannot
read while reporting the omission and its reason in the scope receipt.
"""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

CROSS_DOMAIN_CONTRACT = "noesis-cross-domain-v1"
RRF_K = 60
MAX_DOMAINS = 50
MAX_LIMIT = 100


class CrossDomainError(ValueError):
    """Stable error raised at the cross-domain orchestration boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _private(backing: Any) -> bool:
    return "private" in {
        str(tag).casefold() for tag in backing.definition.tags
    }


def _has_private_grant(conn: Any, principal_id: str, domain: str) -> bool:
    from src.kb.watches import ensure_watch_schema

    ensure_watch_schema(conn)
    return (
        conn.execute(
            "SELECT 1 FROM claim_watch_domain_grants"
            " WHERE principal_id = ? AND domain = ?",
            [principal_id, domain],
        ).fetchone()
        is not None
    )


def _limits(limit: Any, per_domain_limit: Any) -> tuple[int, int]:
    try:
        total = int(limit)
        per_domain = int(per_domain_limit)
    except (TypeError, ValueError) as exc:
        raise CrossDomainError(
            "bad_request", "limit and per_domain_limit must be integers"
        ) from exc
    if not 1 <= total <= MAX_LIMIT:
        raise CrossDomainError(
            "bad_request", f"limit must be between 1 and {MAX_LIMIT}"
        )
    if not 1 <= per_domain <= MAX_LIMIT:
        raise CrossDomainError(
            "bad_request",
            f"per_domain_limit must be between 1 and {MAX_LIMIT}",
        )
    return total, per_domain


def resolve_scope(
    registry: Any,
    *,
    conn: Any = None,
    domains: Sequence[str] | None = None,
    all_authorized: bool = False,
    principal_id: str | None = None,
    include_private: bool = False,
    limit: int = 20,
    per_domain_limit: int = 20,
) -> tuple[list[tuple[str, Any]], dict[str, Any]]:
    """Resolve and authorize an ordered domain scope.

    Exactly one scope form is accepted: an explicit non-empty ``domains``
    list, or ``all_authorized=True``.  Duplicate explicit names are rejected
    because order participates in deterministic tie-breaking.
    """

    from src.kb.registry import DomainConfigError

    total, per_domain = _limits(limit, per_domain_limit)
    explicit = domains is not None
    if explicit == bool(all_authorized):
        raise CrossDomainError(
            "bad_request",
            "provide either a non-empty domains list or all_authorized=true",
        )
    if include_private and not str(principal_id or "").strip():
        raise CrossDomainError(
            "unauthorized",
            "include_private requires an authenticated principal",
        )
    if explicit:
        if isinstance(domains, (str, bytes)) or not domains:
            raise CrossDomainError(
                "bad_request", "domains must be a non-empty list of names"
            )
        requested = [str(name).strip() for name in domains]
        if any(not name for name in requested):
            raise CrossDomainError(
                "bad_request", "domain names must be non-empty"
            )
        if len(requested) > MAX_DOMAINS:
            raise CrossDomainError(
                "bad_request", f"at most {MAX_DOMAINS} domains may be queried"
            )
        if len(requested) != len(set(requested)):
            raise CrossDomainError(
                "bad_request", "domains must be unique and explicitly ordered"
            )
    else:
        requested = list(registry.names())

    resolved: list[tuple[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for name in requested:
        try:
            backing = registry.resolve(name, conn=conn)
        except DomainConfigError as exc:
            raise CrossDomainError("unknown_domain", str(exc)) from exc
        if _private(backing):
            if not include_private:
                if explicit:
                    raise CrossDomainError(
                        "unauthorized",
                        f"private domain {name!r} requires include_private=true",
                    )
                excluded.append({"domain": name, "reason": "private_not_requested"})
                continue
            principal = str(principal_id).strip()
            if not _has_private_grant(backing.conn, principal, name):
                if explicit:
                    raise CrossDomainError(
                        "unauthorized",
                        f"principal is not authorized for domain {name!r}",
                    )
                excluded.append({"domain": name, "reason": "not_authorized"})
                continue
        resolved.append((name, backing))

    models = {
        name: backing.definition.embedding_model for name, backing in resolved
    }
    scope = {
        "mode": "explicit" if explicit else "all_authorized",
        "requested_domains": requested if explicit else None,
        "selected_domains": [name for name, _backing in resolved],
        "excluded_domains": excluded,
        "global_limit": total,
        "per_domain_limit": per_domain,
        "embedding_models": models,
        "embedding_models_compatible": len(set(models.values())) <= 1,
        "ranking": {
            "method": "reciprocal-rank-fusion",
            "rrf_k": RRF_K,
            "score_scope": "cross-domain ordering only; not a probability",
        },
        "domains": [
            {
                "domain": name,
                "backing": backing.backing_type,
                "embedding_model": backing.definition.embedding_model,
                "status": "selected",
            }
            for name, backing in resolved
        ],
    }
    return resolved, scope


def _domain_failure(name: str, backing: Any, exc: Exception) -> dict[str, Any]:
    return {
        "domain": name,
        "backing": backing.backing_type,
        "code": "domain_unavailable",
        "message": str(exc),
    }


def _mark_scope_status(
    scope: dict[str, Any], domain: str, status: str
) -> None:
    for entry in scope["domains"]:
        if entry["domain"] == domain:
            entry["status"] = status
            return


def search_across(
    resolved: Sequence[tuple[str, Any]],
    scope: Mapping[str, Any],
    query: str,
) -> dict[str, Any]:
    """Search resolved domains and merge their results with deterministic RRF."""

    if not isinstance(query, str) or not query.strip():
        raise CrossDomainError("bad_request", "query must be non-empty")
    receipt = copy.deepcopy(dict(scope))
    per_domain_limit = int(receipt["per_domain_limit"])
    global_limit = int(receipt["global_limit"])
    failures: list[dict[str, Any]] = []
    aggregated: dict[str, dict[str, Any]] = {}
    observed_at = int(time.time() * 1000)

    for name, backing in resolved:
        try:
            hits = backing.search(query.strip(), limit=per_domain_limit)
        except Exception as exc:  # noqa: BLE001 - backing isolation boundary
            failures.append(_domain_failure(name, backing, exc))
            _mark_scope_status(receipt, name, "unavailable")
            continue
        _mark_scope_status(receipt, name, "ok")
        for rank, raw in enumerate(hits, start=1):
            row = dict(raw)
            document_id = str(row.get("document_id") or "")
            if not document_id:
                continue
            contribution = 1.0 / (RRF_K + rank)
            entry = aggregated.setdefault(
                document_id,
                {
                    **row,
                    "document_id": document_id,
                    "domains": [],
                    "retrieval": [],
                    "rank_score": 0.0,
                    "score_kind": "reciprocal-rank-fusion",
                },
            )
            if name not in entry["domains"]:
                entry["domains"].append(name)
            entry["rank_score"] += contribution
            entry["retrieval"].append(
                {
                    "domain": name,
                    "backing": backing.backing_type,
                    "rank": rank,
                    "rrf_contribution": round(contribution, 8),
                    "domain_score": row.get("domain_score"),
                    "domain_method": row.get("domain_method"),
                    "as_of_ms": observed_at,
                }
            )

    results = list(aggregated.values())
    for row in results:
        row["rank_score"] = round(float(row["rank_score"]), 8)
        row["retrieval"].sort(key=lambda item: receipt["selected_domains"].index(item["domain"]))
    domain_order = {
        name: index for index, name in enumerate(receipt["selected_domains"])
    }
    results.sort(
        key=lambda row: (
            -row["rank_score"],
            min(domain_order[name] for name in row["domains"]),
            row["document_id"],
        )
    )
    results = results[:global_limit]
    return {
        "cross_domain_contract": CROSS_DOMAIN_CONTRACT,
        "operation": "search",
        "query": query.strip(),
        "scope": receipt,
        "results": results,
        "partial_failures": failures,
        "n": len(results),
    }


class _DisjointSet:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent.setdefault(value, value)
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            first, second = sorted((a, b))
            self.parent[second] = first


def _claim_components(conn: Any, claim_ids: set[str], domains: set[str]) -> _DisjointSet:
    groups = _DisjointSet(claim_ids)
    if not claim_ids:
        return groups
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'claim_links'"
    ).fetchone()
    if exists is None:
        return groups
    rows = conn.execute(
        "SELECT domain_a, claim_a, domain_b, claim_b FROM claim_links"
        " WHERE relation = 'duplicate'"
    ).fetchall()
    for domain_a, claim_a, domain_b, claim_b in rows:
        if (
            domain_a in domains
            and domain_b in domains
            and str(claim_a) in claim_ids
            and str(claim_b) in claim_ids
        ):
            groups.union(str(claim_a), str(claim_b))
    return groups


def _annotate_evidence(
    rows: Iterable[Mapping[str, Any]], domain: str, backing: str
) -> list[dict[str, Any]]:
    annotated = []
    for raw in rows:
        row = copy.deepcopy(dict(raw))
        row["domains"] = sorted({*row.get("domains", []), domain})
        row["backings"] = sorted({*row.get("backings", []), backing})
        annotated.append(row)
    return annotated


def _merge_evidence(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for raw in rows:
        row = copy.deepcopy(dict(raw))
        marker = (
            row.get("document_id"),
            row.get("claim_id"),
            row.get("path"),
            row.get("excerpt"),
        )
        if marker not in merged:
            merged[marker] = row
            order.append(marker)
        else:
            current = merged[marker]
            current["domains"] = sorted(
                set(current.get("domains", [])) | set(row.get("domains", []))
            )
            current["backings"] = sorted(
                set(current.get("backings", [])) | set(row.get("backings", []))
            )
    return [merged[marker] for marker in order]


def _merge_integrity(statements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    assumptions: list[str] = []
    available = False
    n = 0
    for statement in statements:
        integrity = statement.get("integrity") or {}
        n += int(integrity.get("n") or 0)
        available = available or integrity.get("status") != "not_available"
        for finding in integrity.get("findings", []):
            if finding not in findings:
                findings.append(copy.deepcopy(finding))
        for assumption in integrity.get("assumptions", []):
            if assumption not in assumptions:
                assumptions.append(str(assumption))
    return {
        "n": n,
        "status": "findings" if findings else ("no_findings" if available else "not_available"),
        "findings": findings,
        "method": "cross-domain integrity receipt union v1",
        "assumptions": assumptions or ["no evidence document was available to inspect"],
    }


def answer_across(
    resolved: Sequence[tuple[str, Any]],
    scope: Mapping[str, Any],
    question: str,
    *,
    minimum_relevance: float = 0.34,
) -> dict[str, Any]:
    """Build one Answer v1 payload from several independently scoped domains."""

    if not isinstance(question, str) or not question.strip():
        raise CrossDomainError("bad_request", "question must be non-empty")
    if len(question) > 5_000:
        raise CrossDomainError(
            "bad_request", "question must be at most 5000 characters"
        )
    try:
        threshold = float(minimum_relevance)
    except (TypeError, ValueError) as exc:
        raise CrossDomainError(
            "bad_request", "minimum_relevance must be numeric"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise CrossDomainError(
            "bad_request", "minimum_relevance must be between 0 and 1"
        )

    from src.kb.answer import (
        ANSWER_CONTRACT_VERSION,
        ASSUMPTIONS,
        PREDICTION_MODE,
        _independence,
        _refusal_statement,
        _render_statement,
        _stable_id,
        _tokens,
        build_answer,
    )

    receipt = copy.deepcopy(dict(scope))
    per_domain_limit = int(receipt["per_domain_limit"])
    global_limit = int(receipt["global_limit"])
    failures: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, str]] = []
    domain_runs: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for domain_order, (name, backing) in enumerate(resolved):
        started = int(time.time() * 1000)
        try:
            payload = build_answer(
                backing,
                question,
                limit=per_domain_limit,
                minimum_relevance=threshold,
            )
        except Exception as exc:  # noqa: BLE001 - backing isolation boundary
            failures.append(_domain_failure(name, backing, exc))
            _mark_scope_status(receipt, name, "unavailable")
            continue
        status = str(payload.get("answer_status"))
        _mark_scope_status(receipt, name, "ok")
        selected = payload.get("evidence_plan", {}).get("selected", [])
        domain_runs.append(
            {
                "domain": name,
                "backing": backing.backing_type,
                "as_of_ms": started,
                "status": status,
                "statement_count": 0 if status == "refused" else len(payload.get("statements", [])),
                "claim_clusters_considered": int(payload.get("evidence_plan", {}).get("claim_clusters_considered", 0)),
                "documents_considered": int(payload.get("evidence_plan", {}).get("documents_considered", 0)),
            }
        )
        if status == "refused":
            coverage_gaps.append({"domain": name, "reason": "no_relevant_evidence"})
            continue
        for rank, statement in enumerate(payload.get("statements", []), start=1):
            selection = selected[rank - 1] if rank <= len(selected) else {}
            item = copy.deepcopy(dict(statement))
            item["supporting_evidence"] = _annotate_evidence(
                item.get("supporting_evidence", []), name, backing.backing_type
            )
            item["contradicting_evidence"] = _annotate_evidence(
                item.get("contradicting_evidence", []), name, backing.backing_type
            )
            candidates.append(
                {
                    "domain": name,
                    "backing": backing,
                    "backing_type": backing.backing_type,
                    "domain_order": domain_order,
                    "rank": rank,
                    "rrf": 1.0 / (RRF_K + rank),
                    "selection": selection,
                    "statement": item,
                }
            )

    claim_ids = {
        str(item["statement"]["claim_id"])
        for item in candidates
        if item["statement"].get("claim_id")
    }
    conn = resolved[0][1].conn if resolved else None
    components = (
        _claim_components(conn, claim_ids, set(receipt["selected_domains"]))
        if conn is not None
        else _DisjointSet(claim_ids)
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        statement = item["statement"]
        claim_id = statement.get("claim_id")
        if claim_id:
            key = "claim:" + components.find(str(claim_id))
        else:
            cited = statement.get("supporting_evidence", [])
            document_id = cited[0].get("document_id") if cited else statement["id"]
            key = "document:" + str(document_id)
        grouped[key].append(item)

    support_by_claim: dict[str, dict[str, Any]] = {}
    for item in candidates:
        for locator in item["statement"].get("supporting_evidence", []):
            if locator.get("claim_id") and locator.get("cited"):
                support_by_claim[str(locator["claim_id"])] = locator

    ranked_groups = []
    for key, items in grouped.items():
        score = sum(float(item["rrf"]) for item in items)
        best = min(
            items,
            key=lambda item: (
                item["rank"],
                item["domain_order"],
                str(item["statement"].get("claim_id") or item["statement"]["id"]),
            ),
        )
        ranked_groups.append((score, key, best, items))
    ranked_groups.sort(key=lambda row: (-row[0], row[1]))

    statements: list[dict[str, Any]] = []
    selected_receipts: list[dict[str, Any]] = []
    for rank_score, key, best, items in ranked_groups[:global_limit]:
        item_statements = [item["statement"] for item in items]
        statement = copy.deepcopy(best["statement"])
        supporting = _merge_evidence(
            locator
            for candidate in item_statements
            for locator in candidate.get("supporting_evidence", [])
        )
        contradicting_rows = []
        for candidate in item_statements:
            for locator in candidate.get("contradicting_evidence", []):
                replacement = support_by_claim.get(str(locator.get("claim_id") or ""))
                contradicting_rows.append(replacement or locator)
        contradicting = _merge_evidence(contradicting_rows)
        statement["id"] = _stable_id(question, "cross-domain", key)
        statement["supporting_evidence"] = supporting
        statement["contradicting_evidence"] = contradicting
        statement["domains"] = sorted({item["domain"] for item in items})
        statement["backings"] = sorted({item["backing_type"] for item in items})
        statement["corroboration"] = _independence(supporting, conn)
        from src.osint.evidence import render_state

        statement["citation_state"] = render_state(
            statement["corroboration"]["independent_source_count"]
        )
        if any(locator.get("cited") for locator in contradicting):
            statement["verdict"] = "contradicted"
        statement["integrity"] = _merge_integrity(item_statements)
        statement["n"] = len(supporting) + len(contradicting)
        statement["method"] = "cross-domain equivalence grouping with extractive selection"
        statement["assumptions"] = list(dict.fromkeys([
            *statement.get("assumptions", []),
            "equivalent claims are grouped only by explicit duplicate links or stable claim identity",
        ]))
        statements.append(statement)
        selection = best.get("selection") or {}
        selected_receipts.append(
            {
                "kind": str(selection.get("kind") or ("claim" if statement.get("claim_id") else "document")),
                "id": str(selection.get("id") or key),
                "relevance": float(selection.get("relevance") or 0.0),
                "domains": statement["domains"],
                "backings": statement["backings"],
                "rank_score": round(rank_score, 8),
            }
        )

    refused = not statements
    if refused:
        refusal = _refusal_statement(question.strip())
        refusal["text"] = "No relevant evidence was found in the selected domains."
        statements = [refusal]

    partial_reasons: list[str] = []
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
        if len(ranked_groups) > global_limit:
            partial_reasons.append("output_budget_exhausted")
        if failures:
            partial_reasons.append("one_or_more_domains_unavailable")

    rendered = "\n".join(_render_statement(statement) for statement in statements)
    considered_claims = sum(run["claim_clusters_considered"] for run in domain_runs)
    considered_documents = sum(run["documents_considered"] for run in domain_runs)
    return {
        "cross_domain_contract": CROSS_DOMAIN_CONTRACT,
        "operation": "answer",
        "answer_contract": ANSWER_CONTRACT_VERSION,
        "question": question.strip(),
        "answer_status": "refused" if refused else ("partial" if partial_reasons else "answered"),
        "statements": statements,
        "rendered": rendered,
        "refusal": (
            {
                "code": "insufficient_evidence",
                "message": "No selected domain produced evidence above the deterministic relevance threshold.",
            }
            if refused
            else None
        ),
        "partial_reasons": partial_reasons,
        "partial_failures": failures,
        "scope": receipt,
        "evidence_plan": {
            "n": considered_claims + considered_documents,
            "method": "per-domain deterministic planning followed by reciprocal-rank fusion",
            "assumptions": [
                *ASSUMPTIONS,
                "rank fusion compares ordering, not raw backing scores",
            ],
            "minimum_relevance": threshold,
            "question_tokens": sorted(_tokens(question)),
            "claim_clusters_considered": considered_claims,
            "documents_considered": considered_documents,
            "selected": selected_receipts,
            "scope": receipt,
            "domain_runs": domain_runs,
            "coverage_gaps": coverage_gaps,
            "partial_failures": failures,
        },
        "n": len(statements),
        "method": "cross-domain reciprocal-rank fusion with extractive rendering",
        "assumptions": [
            *ASSUMPTIONS,
            "rank fusion compares ordering, not raw backing scores",
        ],
        "prediction_mode": PREDICTION_MODE,
    }


def _stable_link_id(kind: str, relation: str, endpoints: Sequence[Mapping[str, Any]]) -> str:
    identity = "|".join(
        f"{row.get('domain')}:{row.get('object_id')}" for row in endpoints
    )
    digest = hashlib.sha256(f"{kind}|{relation}|{identity}".encode()).hexdigest()[:20]
    return f"xlink:{digest}"


def links_across(
    resolved: Sequence[tuple[str, Any]],
    scope: Mapping[str, Any],
    *,
    kind: str | None = None,
    relation: str | None = None,
) -> dict[str, Any]:
    """Return inspectable entity equivalences and cross-domain claim links."""

    if kind not in (None, "entity", "claim"):
        raise CrossDomainError("bad_request", "kind must be entity or claim")
    allowed_relations = {"equivalent", "duplicate", "supports", "contradicts", "supersedes"}
    if relation is not None and relation not in allowed_relations:
        raise CrossDomainError(
            "bad_request", f"relation must be one of {sorted(allowed_relations)}"
        )
    receipt = copy.deepcopy(dict(scope))
    observed_at = int(time.time() * 1000)
    global_limit = int(receipt["global_limit"])
    failures: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    entity_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    claim_evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}

    from src.kb.answer import _document_map, _locator
    from src.kb.entities import normalize_surface, resolve

    for name, backing in resolved:
        try:
            entities = backing.entities()
            documents = _document_map(backing.documents(limit=100_000))
            claims = backing.claims(limit=100_000)
        except Exception as exc:  # noqa: BLE001 - backing isolation boundary
            failures.append(_domain_failure(name, backing, exc))
            _mark_scope_status(receipt, name, "unavailable")
            continue
        _mark_scope_status(receipt, name, "ok")
        for entity in entities:
            display = str(entity.get("name") or entity.get("entity") or "").strip()
            if not display:
                continue
            resolved_entity = resolve(backing.conn, display)
            canonical_id = str(
                entity.get("canonical_id")
                or (resolved_entity or {}).get("canonical_id")
                or f"raw:{normalize_surface(display)}"
            )
            entity_groups[canonical_id].append(
                {
                    "domain": name,
                    "object_id": canonical_id,
                    "name": display,
                    "aliases": list(entity.get("aliases") or [display]),
                    "mentions": int(entity.get("mentions") or 0),
                    "resolution_method": (resolved_entity or {}).get("method") or "exact-normalize",
                    "resolution_confidence": (resolved_entity or {}).get("score", 1.0),
                }
            )
        visibility = "private" if _private(backing) else "public"
        for cluster in claims:
            for member in cluster.get("citations", []):
                claim_id = str(member.get("claim_id") or "")
                if claim_id:
                    claim_evidence[(name, claim_id)] = [
                        _annotate_evidence(
                            [_locator(member, documents, visibility=visibility)],
                            name,
                            backing.backing_type,
                        )[0]
                    ]

    if kind in (None, "entity") and relation in (None, "equivalent"):
        for canonical_id, members in entity_groups.items():
            # A normalized raw surface is not an identity decision.  Only the
            # canonicalization ledger (including manual aliases) can justify
            # an equivalence across otherwise isolated domains.
            if canonical_id.startswith("raw:"):
                continue
            if len({member["domain"] for member in members}) < 2:
                continue
            members.sort(key=lambda row: receipt["selected_domains"].index(row["domain"]))
            confidence = min(float(member["resolution_confidence"]) for member in members)
            endpoints = [
                {"domain": member["domain"], "object_id": member["object_id"]}
                for member in members
            ]
            output.append(
                {
                    "link_id": _stable_link_id("entity", "equivalent", endpoints),
                    "kind": "entity",
                    "relation": "equivalent",
                    "endpoints": endpoints,
                    "confidence": confidence,
                    "method": "canonical entity alias resolution",
                    "prediction_mode": "deterministic-or-manual",
                    "model_version": None,
                    "run_id": None,
                    "as_of_ms": observed_at,
                    "evidence": members,
                    "reversible": True,
                }
            )

    conn = resolved[0][1].conn if resolved else None
    if kind in (None, "claim") and conn is not None:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'claim_links'"
        ).fetchone()
        if exists is not None:
            rows = conn.execute(
                "SELECT domain_a, claim_a, domain_b, claim_b, relation, score,"
                " method, prediction_mode, confidence, model_version, run_id"
                " FROM claim_links ORDER BY claim_a, claim_b, relation"
            ).fetchall()
            selected = set(receipt["selected_domains"])
            for row in rows:
                domain_a, claim_a, domain_b, claim_b, link_relation = row[:5]
                if domain_a == domain_b or domain_a not in selected or domain_b not in selected:
                    continue
                if relation is not None and relation != link_relation:
                    continue
                endpoints = [
                    {"domain": domain_a, "object_id": claim_a},
                    {"domain": domain_b, "object_id": claim_b},
                ]
                output.append(
                    {
                        "link_id": _stable_link_id("claim", link_relation, endpoints),
                        "kind": "claim",
                        "relation": link_relation,
                        "endpoints": endpoints,
                        "similarity": float(row[5]),
                        "method": row[6],
                        "prediction_mode": row[7],
                        "confidence": float(row[8]) if row[8] is not None else None,
                        "model_version": row[9],
                        "run_id": row[10],
                        "as_of_ms": observed_at,
                        "evidence": _merge_evidence([
                            *claim_evidence.get((domain_a, str(claim_a)), []),
                            *claim_evidence.get((domain_b, str(claim_b)), []),
                        ]),
                        "reversible": True,
                    }
                )

    output.sort(key=lambda row: (row["kind"], row["relation"], row["link_id"]))
    output = output[:global_limit]
    return {
        "cross_domain_contract": CROSS_DOMAIN_CONTRACT,
        "operation": "links",
        "scope": receipt,
        "links": output,
        "partial_failures": failures,
        "n": len(output),
    }


def record_manual_claim_equivalence(
    conn: Any,
    domain_a: str,
    claim_a: str,
    domain_b: str,
    claim_b: str,
    *,
    actor: str,
) -> dict[str, Any]:
    """Record a reviewer's reversible duplicate link between two claims."""

    from src.kb.claim_links import _upsert_link, ensure_claim_link_schema

    if not all(str(value).strip() for value in (domain_a, claim_a, domain_b, claim_b, actor)):
        raise CrossDomainError("bad_request", "domains, claims, and actor are required")
    ensure_claim_link_schema(conn)
    run_id = f"manual-{uuid.uuid4().hex}"
    wrote = _upsert_link(
        conn,
        (domain_a, claim_a),
        (domain_b, claim_b),
        "duplicate",
        1.0,
        f"manual-correction:{actor}",
        "human-reviewed",
        1.0,
        "manual-v1",
        run_id,
    )
    return {"run_id": run_id, "written": wrote, "relation": "duplicate"}


def unlink_manual_claim_equivalence(
    conn: Any,
    claim_a: str,
    claim_b: str,
) -> bool:
    """Remove one manual duplicate link; model-produced links are untouched."""

    left, right = sorted((str(claim_a), str(claim_b)))
    row = conn.execute(
        "SELECT method FROM claim_links"
        " WHERE claim_a = ? AND claim_b = ? AND relation = 'duplicate'",
        [left, right],
    ).fetchone()
    if row is None:
        return False
    if not str(row[0]).startswith("manual-correction:"):
        raise CrossDomainError(
            "conflict", "only manually reviewed equivalence links can be unlinked directly"
        )
    conn.execute(
        "DELETE FROM claim_links"
        " WHERE claim_a = ? AND claim_b = ? AND relation = 'duplicate'",
        [left, right],
    )
    return True


__all__ = [
    "CROSS_DOMAIN_CONTRACT",
    "CrossDomainError",
    "answer_across",
    "links_across",
    "record_manual_claim_equivalence",
    "resolve_scope",
    "search_across",
    "unlink_manual_claim_equivalence",
]

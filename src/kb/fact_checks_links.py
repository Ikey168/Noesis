"""Fact-checks linked to news articles, OSINT corroboration, claim timelines and source identities (#2659, FC07).

Every link points at a specific fact-check record revision (per source) and at
a specific target revision, and records its basis:

* ``news-article`` (basis ``citation``) - an appearance or first appearance the
  fact-check cites equals a news document's URL under the ``wa-canon-v1`` rules
  (:mod:`src.kb.web_archive_identity`); the target revision is the document's
  latest correction-ledger revision (:mod:`src.ingestion.corrections`), else its
  content hash;
* ``claim-timeline`` (basis ``accepted-match``) - an accepted FC06 claim match
  links the fact-check to the current state of that claim's timeline
  (:mod:`src.kb.claim_timelines`);
* ``osint-corroboration`` (basis ``accepted-match``) - the same accepted match
  links the fact-check to the OSINT corroboration of that claim
  (:func:`src.osint.corroboration.corroborate`); the link keeps a digest of the
  corroboration output it saw and never copies a verdict;
* ``source-identity`` (basis ``accepted-match``) - an accepted publisher match
  links the publisher's fact-checks to the source identity revision.

Appearance URLs with no document on record, accepted matches whose target is
missing, and absent providers are reported, never dropped. Social-platform
appearances (stored as digests) are never resolved. No link infers a verdict on
the linked claim.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.fact_checks_identity import FactCheckIdentity
from src.kb.fact_checks_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-fact-check-link-v1"
LINK_KINDS = ("news-article", "claim-timeline", "osint-corroboration", "source-identity")
NOTICE = ("a link states a cited URL or a reviewed match; it is not a verdict on the linked claim, article or "
          "source")
_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, record_key TEXT NOT NULL,
  source_id TEXT NOT NULL, record_revision_id TEXT NOT NULL, target_key TEXT NOT NULL, target_namespace TEXT,
  target_revision TEXT, basis_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "record_key", "source_id", "record_revision_id", "target_key",
            "target_namespace", "target_revision", "basis_json", "created_by", "created_at_ms")
# Keys a corroboration output may carry that would read as a verdict; never copied into a link.
_VERDICT_KEYS = ("factcheck_verdict", "verdict", "credibility_grade")


def _view(row) -> dict[str, Any]:
    value = dict(zip(_COLUMNS, row))
    value["basis"] = json.loads(value.pop("basis_json"))
    return {"contract": CONTRACT, **value, "notice": NOTICE}


def document_revision(conn: Any, document_id: str) -> str | None:
    if table_exists(conn, "document_revisions"):
        row = conn.execute("SELECT max(revision) FROM document_revisions WHERE document_id=?", [document_id]).fetchone()
        if row and row[0] is not None:
            return f"document-revision:{document_id}:{int(row[0])}"
    row = conn.execute("SELECT content_hash FROM documents WHERE document_id=?", [document_id]).fetchone()
    return f"content-hash:{row[0]}" if row and row[0] else None


def documents_by_url(conn: Any) -> dict[str, list[dict[str, Any]]]:
    """News documents keyed by the ``wa-canon-v1`` canonical form of their URL (and canonical URL)."""
    from src.kb.web_archive_identity import canonical_key

    out: dict[str, list[dict[str, Any]]] = {}
    if not table_exists(conn, "documents"):
        return out
    for document_id, url, canonical_url, title in conn.execute(
            "SELECT document_id, url, canonical_url, title FROM documents ORDER BY document_id").fetchall():
        for candidate in {u for u in (url, canonical_url) if u}:
            out.setdefault(canonical_key(candidate), []).append(
                {"document_id": document_id, "url": url, "title": title})
    return out


class FactCheckLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.identity = FactCheckIdentity(conn, now=now, initialize=initialize)
        self.store = self.identity.store
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "fact_check_links")

    def _insert(self, namespace: str, kind: str, view: Mapping[str, Any], target_key: str,
                target_namespace: str | None, target_revision: str | None, basis: Mapping[str, Any],
                principal_id: str) -> tuple[dict[str, Any], bool]:
        link_id = "fc-link:" + digest([namespace, kind, view["revision_id"], target_key, target_namespace,
                                       target_revision])[:24]
        created = not self.conn.execute("SELECT 1 FROM fact_check_links WHERE namespace=? AND link_id=?",
                                        [namespace, link_id]).fetchone()
        if created:
            self.conn.execute("INSERT INTO fact_check_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, kind, view["record_key"], view["source_id"], view["revision_id"],
                               target_key, target_namespace, target_revision, canonical(basis), principal_id,
                               self.now()])
        row = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM fact_check_links WHERE namespace=? AND "
                                "link_id=?", [namespace, link_id]).fetchone()
        return _view(row), created

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
             timeline_namespace: str | None = None, source_identity_namespace: str | None = None) -> dict[str, Any]:
        """Create every citation and accepted-match link for the current fact-check revisions; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        views = [v for v in self.store.records(namespace, scopes=scopes, kinds=["fact-check"], include_absent=False)]
        links, created, missing, unavailable = [], 0, [], []

        def add(*args) -> None:
            nonlocal created
            link, new = self._insert(namespace, *args, principal_id)
            links.append(link)
            created += int(new)

        # news articles, by citation
        documents = documents_by_url(self.conn)
        if not table_exists(self.conn, "documents"):
            unavailable.append({"provider": "news.core", "reason": "no documents store"})
        for view in views:
            claim = view["record"]["fields"]["claims"][0]
            cited = [("appearance", a) for a in claim.get("appearances") or []]
            if claim.get("first_appearance"):
                cited.append(("first_appearance", claim["first_appearance"]))
            for role, entry in cited:
                if entry.get("platform_post"):
                    continue  # stored as a digest (FC01); never resolved to an account or post
                hits = documents.get(entry["url_canonical"]) or []
                if not hits:
                    missing.append({"kind": "news-article", "record_key": view["record_key"],
                                    "source_id": view["source_id"], "cited_url": entry["url"],
                                    "reason": "no document on record for the cited URL"})
                for document in hits:
                    add("news-article", view, document["document_id"], None,
                        document_revision(self.conn, document["document_id"]),
                        {"kind": "citation", "role": role, "cited_url": entry["url"],
                         "url_canonical": entry["url_canonical"], "document_url": document["url"],
                         "url_rules": "wa-canon-v1", "canonical_rules": entry.get("canonical_rules") or []})
        # accepted claim matches: claim timelines and OSINT corroboration
        accepted = self.identity.accepted(namespace, "claim", scopes=scopes)
        by_record = {}
        for match in accepted:
            by_record.setdefault(match["left_key"], []).append(match)
        timeline_ns = timeline_namespace or namespace
        timelines = table_exists(self.conn, "claim_timeline_current")
        if accepted and not timelines:
            unavailable.append({"provider": "claim-timelines", "reason": "no claim timeline store"})
        corroboration = table_exists(self.conn, "argument_claims")
        if accepted and not corroboration:
            unavailable.append({"provider": "osint.corroboration", "reason": "no argument claim layer"})
        for view in views:
            for match in by_record.get(view["record_key"], []):
                basis = {"kind": "accepted-match", "candidate_id": match["candidate_id"],
                         "decision_id": match["decision_id"], "method": match["method"],
                         "reviewer": match["reviewer"]}
                claim_id = match["right_key"]
                if timelines:
                    row = self.conn.execute("SELECT state_id, revision FROM claim_timeline_current WHERE namespace=? "
                                            "AND claim_id=?", [timeline_ns, claim_id]).fetchone()
                    if row:
                        add("claim-timeline", view, claim_id, timeline_ns, row[0], {**basis, "timeline_revision":
                                                                                    int(row[1])})
                    else:
                        missing.append({"kind": "claim-timeline", "record_key": view["record_key"],
                                        "claim_id": claim_id, "reason": "no timeline state for the matched claim"})
                if corroboration:
                    from src.osint.corroboration import METHOD, corroborate

                    try:
                        result = corroborate(self.conn, claim_id)
                    except Exception as exc:  # noqa: BLE001 - corroboration layers are optional; report absence
                        result = {"error": getattr(exc, "code", type(exc).__name__)}
                    if result.get("error"):
                        missing.append({"kind": "osint-corroboration", "record_key": view["record_key"],
                                        "claim_id": claim_id, "reason": str(result["error"])})
                        continue
                    seen = {k: v for k, v in result.items() if k not in _VERDICT_KEYS}
                    add("osint-corroboration", view, claim_id, None, "corroboration:" + digest(seen)[:20],
                        {**basis, "corroboration_method": METHOD})
        # accepted publisher matches: source identities
        publisher_matches = {m["left_key"]: m for m in self.identity.accepted(namespace, "publisher", scopes=scopes)}
        if publisher_matches and not table_exists(self.conn, "source_identity_current"):
            unavailable.append({"provider": "source-identity", "reason": "no source identity store"})
        for view in views:
            match = publisher_matches.get(view["publisher_key"])
            if not match or not table_exists(self.conn, "source_identity_current"):
                continue
            row = self.conn.execute("SELECT revision_id FROM source_identity_current WHERE source_id=?",
                                    [match["right_key"]]).fetchone()
            if not row:
                missing.append({"kind": "source-identity", "record_key": view["record_key"],
                                "source_identity": match["right_key"], "reason": "source identity not on record"})
                continue
            add("source-identity", view, match["right_key"], source_identity_namespace or namespace, row[0],
                {"kind": "accepted-match", "candidate_id": match["candidate_id"], "decision_id": match["decision_id"],
                 "method": match["method"], "reviewer": match["reviewer"]})
        return {"status": "linked" if links else "none_linked", "created": created, "links": links,
                "missing_targets": missing, "unavailable": unavailable, "notice": NOTICE}

    def links(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None, record_key: str | None = None,
              target_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM fact_check_links WHERE namespace=? AND (? IS NULL OR "
            "link_kind=?) AND (? IS NULL OR record_key=?) AND (? IS NULL OR target_key=?) ORDER BY link_kind, "
            "record_key, source_id, target_key", [namespace, kind, kind, record_key, record_key, target_key,
                                                  target_key]).fetchall()
        return [_view(r) for r in rows]

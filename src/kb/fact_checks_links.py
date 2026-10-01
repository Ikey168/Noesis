"""Fact-checks linked to news articles, argument claims, OSINT corroboration and claim timelines (#2659, FC07).

A link is recorded only on a stated basis and always points at a specific
fact-check revision and a specific target revision:

* ``citation`` - a news document whose URL (``documents.url`` or
  ``canonical_url``, canonicalised with the versioned ``wa-canon-v1`` rules) is
  the review URL or an appearance URL the fact-check published; the target
  revision is the document's content hash;
* ``accepted-match`` - an argument claim reached through an accepted FC06
  claim match; from it, the OSINT corroboration result for that claim
  (:func:`src.osint.corroboration.corroborate`, referenced by a digest and its
  source counts only) and the claim's current timeline state
  (:mod:`src.kb.claim_timelines`, by state id).

No verdict is inferred for a linked claim or article: a link says that a
publisher's fact-check cites or was matched to it, nothing more. Missing
providers and targets (no documents table, a cited URL not ingested, no
timeline state) are reported, never dropped silently.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from src.ingestion.fact_checks_sources import canonical_url, url_rule
from src.kb.fact_checks_identity import FactCheckIdentity, claim_key
from src.kb.fact_checks_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    FactCheckError,
    FactChecksStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

LINK_KINDS = ("news-article", "argument-claim", "osint-corroboration", "claim-timeline")
BASES = ("citation", "shared-identifier", "accepted-match")
_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, record_key TEXT NOT NULL,
  source_id TEXT NOT NULL, revision_id TEXT NOT NULL, target_key TEXT NOT NULL, target_revision TEXT,
  basis TEXT NOT NULL, detail_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "record_key", "source_id", "revision_id", "target_key", "target_revision",
            "basis", "detail_json", "created_by", "created_at_ms")
NOTICE = "a link records a citation or an accepted match; no verdict is inferred for the linked record"


class FactCheckLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FactChecksStore(conn, initialize=initialize, now=self.now)
        self.identity = FactCheckIdentity(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _add(self, namespace, kind, row, target_key, target_revision, basis, detail, principal_id) -> bool:
        link_id = "fc-link:" + digest([namespace, kind, row["revision_id"], target_key, target_revision])[:24]
        if self.conn.execute("SELECT 1 FROM fact_check_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return False
        self.conn.execute("INSERT INTO fact_check_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, kind, row["record_key"], row["source_id"], row["revision_id"],
                           target_key, target_revision, basis, canonical(detail), principal_id, self.now()])
        return True

    def _documents(self) -> dict[str, list[dict[str, Any]]]:
        by_url: dict[str, list[dict[str, Any]]] = {}
        for document in self.identity._documents():
            if document.get("source_type", "news") != "news":
                continue  # only news articles; the runtime's own copies of fact-check pages are not targets
            for url in {document.get("url"), document.get("canonical_url")} - {None, ""}:
                by_url.setdefault(canonical_url(url)[0], []).append(document)
        return by_url

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
             timeline_namespace: str | None = None) -> dict[str, Any]:
        """Record every link the current fact-check revisions support; idempotent, reports what is missing."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        authorize(namespace, scopes, READ_SCOPE)
        created = dict.fromkeys(LINK_KINDS, 0)
        unavailable, missing = [], []
        rows = [r for r in self.store.records(namespace, scopes=scopes, kinds=["fact-check"])
                if r["status"] == "published"]
        documents = self._documents()
        if not table_exists(self.conn, "documents"):
            unavailable.append({"provider": "news.core", "target": "documents", "reason": "no news documents table"})
        for row in rows:
            fields = row["record"]["fields"]
            cited = [("review", fields["review_url"])] + [
                ("appearance", url) for claim in fields.get("claims") or [] for url in claim.get("appearance_urls") or []]
            for role, url in sorted(set(cited)):
                key, rules = canonical_url(url)
                targets = documents.get(key) or []
                if not targets:
                    missing.append({"record_key": row["record_key"], "revision_id": row["revision_id"],
                                    "kind": "news-article", "url": url, "role": role,
                                    "reason": "no ingested news article with this URL"})
                for document in targets:
                    created["news-article"] += self._add(
                        namespace, "news-article", row, f"document:{document['document_id']}",
                        document.get("content_hash"), "citation",
                        {"role": role, "url_as_published": url, "url_canonical": key, "rules_applied": rules,
                         "canonicalisation": url_rule()["version"], "document_url": document.get("url")},
                        principal_id)
        # accepted claim matches -> argument claims, their corroboration and their timelines
        accepted = self.identity.accepted(namespace, scopes=scopes, kind="claim-argument")
        by_record = {r["record_key"]: r for r in rows}
        for match in accepted:
            record_key = match["left_key"].split("#claim=", 1)[0]
            row = by_record.get(record_key)
            if row is None:
                missing.append({"record_key": record_key, "kind": "argument-claim",
                                "reason": "the fact-check is not currently published"})
                continue
            if not any(claim_key(record_key, c.get("claim_text_as_quoted")) == match["left_key"]
                       for c in row["record"]["fields"].get("claims") or []):
                missing.append({"record_key": record_key, "kind": "argument-claim", "match_id": match["match_id"],
                                "reason": "the matched claim is not in the current revision; re-review the match"})
                continue
            claim_id = match["right_key"].split(":", 1)[1]
            detail = {"match_id": match["match_id"], "method": match["method"], "confidence": match["confidence"],
                      "reviewer": match["reviewer"], "claim_key": match["left_key"]}
            created["argument-claim"] += self._add(namespace, "argument-claim", row, match["right_key"],
                                                   match["decision_id"] or match["match_id"], "accepted-match",
                                                   detail, principal_id)
            corroboration = self._corroboration(claim_id)
            if corroboration is None:
                unavailable.append({"provider": "osint.core", "target": f"osint-corroboration:{claim_id}",
                                    "reason": "no OSINT corroboration result for the claim"})
            else:
                created["osint-corroboration"] += self._add(
                    namespace, "osint-corroboration", row, f"osint-corroboration:{claim_id}",
                    corroboration["result_digest"], "accepted-match", {**detail, **corroboration}, principal_id)
            state = self._timeline(timeline_namespace or namespace, claim_id)
            if state is None:
                missing.append({"record_key": record_key, "kind": "claim-timeline", "claim_id": claim_id,
                                "reason": "no claim timeline state for the claim"})
            else:
                created["claim-timeline"] += self._add(
                    namespace, "claim-timeline", row, f"claim-timeline:{claim_id}", state["state_id"],
                    "accepted-match", {**detail, "timeline_revision": state["revision"]}, principal_id)
        return {"status": "linked" if any(created.values()) or self.links(namespace, scopes=scopes) else
                "nothing_to_link", "created": created, "unavailable": unavailable, "missing_targets": missing,
                "notice": NOTICE}

    def _corroboration(self, claim_id: str) -> dict[str, Any] | None:
        """A reference to the OSINT corroboration result: digest and source counts, never a verdict."""
        if not table_exists(self.conn, "argument_claims"):
            return None
        try:
            from src.osint.corroboration import corroborate

            result = corroborate(self.conn, claim_id)
        except Exception:  # noqa: BLE001 - the OSINT provider is optional; absent means no link
            return None
        if not isinstance(result, dict) or result.get("error"):
            return None
        return {"result_digest": digest(result)[:24],
                "supporting_entries": len(result.get("support") or []),
                "contradicting_entries": len(result.get("contradict") or []),
                "note": "a reference to osint.core corroboration; its grades are not copied or restated"}

    def _timeline(self, namespace: str, claim_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "claim_timeline_current"):
            return None
        row = self.conn.execute("SELECT state_id, revision FROM claim_timeline_current WHERE namespace=? AND "
                                "claim_id=?", [namespace, claim_id]).fetchone()
        return {"state_id": row[0], "revision": int(row[1])} if row else None

    def links(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None, record_key: str | None = None,
              target_key: str | None = None) -> list[dict[str, Any]]:
        import json

        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "fact_check_links"):
            return []
        if kind is not None and kind not in LINK_KINDS:
            raise FactCheckError("invalid_request", f"kind is one of {LINK_KINDS}")
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM fact_check_links WHERE namespace=? AND (? IS NULL OR "
            "link_kind=?) AND (? IS NULL OR record_key=?) AND (? IS NULL OR target_key=?) ORDER BY link_kind, "
            "record_key, target_key, link_id",
            [namespace, kind, kind, record_key, record_key, target_key, target_key]).fetchall()
        out = []
        for row in rows:
            view = dict(zip(_COLUMNS, row))
            view["detail"] = json.loads(view.pop("detail_json"))
            view["notice"] = NOTICE
            out.append(view)
        return out

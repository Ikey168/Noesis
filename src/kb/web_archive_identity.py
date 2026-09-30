"""URL canonicalisation and reviewable capture-to-citation matching for web archives (#2226, WA08).

Canonicalisation is explicit and versioned (``CANONICALISATION_VERSION``); every
rule has an id, and a match records which rules made the cited URL and the
capture's URI-R agree. Exact URI-R matches are distinguished from canonicalised
and redirect-derived matches. Only exact matches are accepted automatically;
every other match is a reviewable identity decision, and a rejected match is
never used for pinning. Archive-reported redirects are recorded as published and
never followed to substitute a different page.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

CANONICALISATION_VERSION = "wa-canon-v1"
TRACKING_PARAMETERS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_ga", "_gl",
})
TRACKING_PREFIXES = ("utm_",)
# Ordered, versioned rules. Changing one requires a new CANONICALISATION_VERSION.
RULES = (
    ("lowercase-scheme-host", "scheme and host compare case-insensitively"),
    ("scheme-equivalence", "http and https name the same page"),
    ("drop-default-port", ":80 for http and :443 for https are dropped"),
    ("strip-www", "a leading 'www.' host label is dropped"),
    ("strip-trailing-slash", "a trailing '/' on a non-root path is dropped; an empty path is '/'"),
    ("drop-tracking-parameters", "utm_* and click-id query parameters are dropped"),
    ("sort-query-parameters", "query parameters compare in sorted order"),
    ("drop-fragment", "the fragment is not sent to servers and is dropped"),
)
RULE_IDS = tuple(rule for rule, _ in RULES)


def canonicalize(url: str) -> tuple[str, list[str]]:
    """Return the canonical key of ``url`` and the ids of the rules that changed it."""
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    applied: list[str] = []
    scheme = parts.scheme
    host = parts.hostname or ""
    if scheme != scheme.lower() or (parts.netloc.split("@")[-1].split(":")[0] != host):
        applied.append("lowercase-scheme-host")
    scheme = scheme.lower()
    if scheme in {"http", "https"}:
        if scheme == "http":
            applied.append("scheme-equivalence")
        scheme = "https"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and (port, parts.scheme.lower()) in {(80, "http"), (443, "https")}:
        applied.append("drop-default-port")
        port = None
    if host.startswith("www."):
        applied.append("strip-www")
        host = host[4:]
    netloc = host + (f":{port}" if port is not None else "")
    path = quote(unquote(parts.path or ""), safe="/:@!$&'()*+,;=-._~%")
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        applied.append("strip-trailing-slash")
        path = path.rstrip("/") or "/"
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMETERS
            and not k.lower().startswith(TRACKING_PREFIXES)]
    if len(kept) != len(pairs):
        applied.append("drop-tracking-parameters")
    ordered = sorted(kept)
    if ordered != kept:
        applied.append("sort-query-parameters")
    if parts.fragment:
        applied.append("drop-fragment")
    canonical = urlunsplit((scheme, netloc, path, urlencode(ordered), ""))
    return canonical, [rule for rule in RULE_IDS if rule in applied]


def canonical_key(url: str) -> str:
    return canonicalize(url)[0]


def match_rules(cited_url: str, uri_r: str) -> list[str]:
    """Rules that had to apply to either side for the two URLs to agree."""
    return sorted(set(canonicalize(cited_url)[1]) | set(canonicalize(uri_r)[1]),
                  key=RULE_IDS.index)


def classify(cited_url: str, capture: dict[str, Any]) -> dict[str, Any] | None:
    """How a capture relates to a cited URL: exact, canonicalised, redirect-derived or no match."""
    if capture["uri_r"] == cited_url:
        return {"match_kind": "exact", "rules": []}
    if canonical_key(capture["uri_r"]) == canonical_key(cited_url):
        return {"match_kind": "canonicalised", "rules": match_rules(cited_url, capture["uri_r"])}
    redirect = capture.get("archive_redirect")
    if redirect and redirect.get("location") and canonical_key(redirect["location"]) == canonical_key(cited_url):
        # The archive published a redirect from the capture's URI-R to the cited
        # URL. Recorded as published; the redirect is never followed.
        return {"match_kind": "redirect-derived",
                "rules": ["archive-reported-redirect", *match_rules(cited_url, redirect["location"])]}
    return None


def candidates(cited_url: str, captures: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for capture in captures:
        kind = classify(cited_url, capture)
        if kind:
            found.append({**kind, "capture": capture})
    return found


DECISIONS = ("accept", "reject")


class CaptureMatcher:
    """Reviewable capture-to-citation matches in the citation preservation store (no new identity store)."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.citation_preservation import CitationPreservationStore

        self.store = CitationPreservationStore(conn, initialize=initialize, now=now)
        self.conn, self.now = conn, self.store.now

    def _redirect_captures(self, namespace: str) -> list[dict[str, Any]]:
        if not self.store._web_archive_ready():
            return []
        rows = self.conn.execute(
            "SELECT payload_json FROM web_archive_captures WHERE namespace=? AND payload_json LIKE ? "
            "ORDER BY capture_id LIMIT 5000", [namespace, '%"archive_redirect":{%']).fetchall()
        return [json.loads(r[0]) for r in rows]

    def propose(self, namespace: str, citation_id: str, cited_url: str, *, principal_id: str,
                scopes: set[str]) -> dict[str, Any]:
        """Propose matches for a cited URL. Exact matches are accepted; every other match awaits review."""
        from src.kb.citation_preservation import (
            MATCH_CONTRACT,
            READ_SCOPE,
            WRITE_SCOPE,
            CitationPreservationError,
            _canonical,
            _digest,
            _require,
        )

        _require(scopes, WRITE_SCOPE)
        if not str(citation_id).strip() or not str(cited_url).strip():
            raise CitationPreservationError("invalid_match", "citation id and cited URL are required")
        pool = {c["capture_id"]: c for c in self.store.captures_for_url(namespace, cited_url, scopes={READ_SCOPE})}
        for capture in self._redirect_captures(namespace):
            pool.setdefault(capture["capture_id"], capture)
        proposed = []
        for found in candidates(cited_url, sorted(pool.values(), key=lambda c: c["capture_id"])):
            capture = found["capture"]
            match_id = "web-archive-match:" + _digest([namespace, citation_id, capture["capture_id"]])[:24]
            existing = self.conn.execute("SELECT payload_json FROM web_archive_matches WHERE match_id=?",
                                         [match_id]).fetchone()
            if existing:
                proposed.append({**json.loads(existing[0]), "idempotent": True})
                continue
            now = self.now()
            payload = {
                "contract": MATCH_CONTRACT, "match_id": match_id, "namespace": namespace,
                "citation_id": citation_id, "cited_url": cited_url, "capture_id": capture["capture_id"],
                "uri_r": capture["uri_r"], "archive_id": capture["archive_id"],
                "match_kind": found["match_kind"], "rules": found["rules"],
                "rule_version": CANONICALISATION_VERSION, "archive_redirect": capture.get("archive_redirect"),
                "state": "accepted" if found["match_kind"] == "exact" else "pending_review",
                "proposed_by": principal_id, "proposed_at_ms": now, "review": None,
            }
            self.conn.execute("INSERT INTO web_archive_matches VALUES (?,?,?,?,?,?,?,?)",
                              [match_id, namespace, citation_id, capture["capture_id"], found["match_kind"],
                               payload["state"], _canonical(payload), now])
            self.store._audit(namespace, "propose-match", match_id, principal_id,
                              {"match_kind": found["match_kind"]}, now)
            proposed.append({**payload, "idempotent": False})
        return {"namespace": namespace, "citation_id": citation_id, "cited_url": cited_url,
                "rule_version": CANONICALISATION_VERSION, "matches": proposed,
                "pending_review": sum(1 for m in proposed if m["state"] == "pending_review")}

    def match(self, namespace: str, match_id: str, *, scopes: set[str]) -> dict[str, Any]:
        from src.kb.citation_preservation import READ_SCOPE, CitationPreservationError, _require

        _require(scopes, READ_SCOPE)
        row = self.conn.execute("SELECT payload_json FROM web_archive_matches WHERE namespace=? AND match_id=?",
                                [namespace, match_id]).fetchone() if self.store._web_archive_ready() else None
        if not row:
            raise CitationPreservationError("match_not_found", "capture match was not found")
        return json.loads(row[0])

    def matches(self, namespace: str, citation_id: str, *, scopes: set[str]) -> list[dict[str, Any]]:
        from src.kb.citation_preservation import READ_SCOPE, _require

        _require(scopes, READ_SCOPE)
        if not self.store._web_archive_ready():
            return []
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload_json FROM web_archive_matches WHERE namespace=? AND citation_id=? "
            "ORDER BY created_at_ms, match_id", [namespace, citation_id]).fetchall()]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: set[str]) -> dict[str, Any]:
        """Confirm or reject a non-exact match with a reason, by someone other than its proposer."""
        from src.kb.citation_preservation import (
            READ_SCOPE,
            WRITE_SCOPE,
            CitationPreservationError,
            _canonical,
            _require,
        )

        _require(scopes, WRITE_SCOPE)
        if decision not in DECISIONS or not str(reason or "").strip():
            raise CitationPreservationError("invalid_review", "decide accept or reject with a reason")
        match = self.match(namespace, match_id, scopes=set(scopes) | {READ_SCOPE})
        if match["match_kind"] == "exact":
            raise CitationPreservationError("exact_match", "exact URI-R matches are not reviewable")
        if match["state"] != "pending_review":
            raise CitationPreservationError("already_reviewed", "the match was already reviewed")
        if match["proposed_by"] == principal_id and "operator" not in scopes:
            raise CitationPreservationError("self_review", "a match is reviewed by someone other than its proposer")
        now = self.now()
        match.update(state="accepted" if decision == "accept" else "rejected",
                     review={"decision": decision, "reason": reason, "reviewer_id": principal_id,
                             "reviewed_at_ms": now})
        self.conn.execute("UPDATE web_archive_matches SET state=?, payload_json=? WHERE match_id=?",
                          [match["state"], _canonical(match), match_id])
        self.store._audit(namespace, "review-match", match_id, principal_id, {"decision": decision}, now)
        return match

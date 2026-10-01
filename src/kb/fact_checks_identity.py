"""Claimants, claims and publishers matched through reviewable assertions (#2659, FC06).

Every subject stays what the publisher published - a claimant as named, a claim
as quoted, a publisher site - and links to other owners' records are *proposed*
as match assertions that a reviewer accepts, rejects or reverts. Nothing is
merged and nothing is accepted automatically; subjects without an accepted
match stay visible as ``unmatched``.

Three kinds of match, published identifiers before names:

* ``claimant-entity`` (:mod:`src.kb.entities` ``canonical_entities``):
  ``published-identifier`` when a ``sameAs`` identifier the publisher published
  for the claimant (e.g. a Wikidata URL or QID) is an alias surface of a
  canonical entity; ``alias-name`` when the claimant's name as named resolves
  through the entity aliases (weak). Generic claimants ("social media users",
  "viral image") are never proposed. Accepted and reverted claimant matches are
  also :class:`src.kb.entity_history.EntityHistoryStore` decisions;
* ``claim-argument`` (``argument_claims``): ``appearance-url`` when the
  claim's document URL is an appearance URL the fact-check published;
  ``review-url`` when the legacy ``argument_claims.factcheck_url`` names this
  review (a stored lookup, still only a proposal); ``lexical-overlap`` using the
  lexical fallback of :mod:`src.kb.claim_links` (weak);
* ``publisher-source`` (:mod:`src.kb.source_identity`): ``published-domain``
  when a reviewed domain alias of a source identity equals the publisher site.

Matches carry method, evidence and confidence and point at the fact-check
revision they were proposed from. Proposing is idempotent; a rejected or
reverted match is proposed again only with new evidence.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.fact_checks_sources import canonical_url, slug
from src.kb.fact_checks_records import (
    CLAIMANT_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    FactCheckError,
    FactChecksStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-fact-check-match-v1"
KINDS = ("claimant-entity", "claim-argument", "publisher-source")
STATES = ("proposed", "accepted", "rejected", "reverted")
CONFIDENCE = {"published-identifier": 0.9, "published-domain": 0.8, "appearance-url": 0.7, "review-url": 0.6,
              "alias-name": 0.4, "lexical-overlap": 0.3}
LEXICAL_THRESHOLD = 0.5
GENERIC_CLAIMANTS = frozenset({
    "social media", "social media users", "social media posts", "multiple sources", "viral image", "viral post",
    "viral video", "facebook posts", "facebook users", "twitter users", "x users", "instagram posts", "tiktok users",
    "whatsapp messages", "online posts", "bloggers", "various", "unknown", "multiple people", "internet users",
})
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, match_kind TEXT NOT NULL, left_key TEXT NOT NULL,
  left_revision_id TEXT, right_key TEXT NOT NULL, method TEXT NOT NULL, confidence DOUBLE NOT NULL,
  evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""


def normalise_claim(text: Any) -> str:
    """Case, whitespace, quote and punctuation folding (for grouping and search only, never for display)."""
    folded = re.sub(r"[\"'“”‘’«»]", "", str(text or "").casefold())
    return " ".join(re.sub(r"[^\w\s%.,-]", " ", folded).replace(",", " ").split()).strip(" .")


def claim_key(record_key: str, text: Any) -> str:
    return f"{record_key}#claim={digest(normalise_claim(text))[:16]}"


def claimant_key(name: Any) -> str:
    return f"fact-checks:claimant:{slug(name)}"


def publisher_key(site: Any) -> str:
    return f"fact-checks:publisher-site:{site}"


def is_generic(name: Any) -> bool:
    from src.kb.entities import normalize_surface

    norm = normalize_surface(str(name or ""))
    return not norm or norm in GENERIC_CLAIMANTS or norm.endswith((" users", " posts"))


def _qid(value: str) -> str | None:
    match = re.search(r"(?:wikidata\.org/(?:wiki|entity)/)(Q\d+)$", value)
    return match.group(1) if match else None


class FactCheckIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FactChecksStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "fact_check_matches")

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Claimants, claims and publisher sites as published, each citing the fact-check revisions behind it."""
        claimants: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        publishers: dict[str, dict[str, Any]] = {}
        for row in self.store.records(namespace, scopes=scopes):
            record, fields = row["record"], row["record"]["fields"]
            cite = {"record_key": row["record_key"], "source_id": row["source_id"], "revision_id": row["revision_id"]}
            site = record.get("publisher_site")
            if site:
                entry = publishers.setdefault(site, {"key": publisher_key(site), "site": site, "names": set(),
                                                     "cited": []})
                name = (fields.get("publisher") or {}).get("name_as_published") or fields.get("name_as_published")
                if name:
                    entry["names"].add(name)
                entry["cited"].append(cite)
            if row["record_kind"] != "fact-check" or row["status"] != "published":
                continue
            for claim in fields.get("claims") or []:
                text = claim.get("claim_text_as_quoted")
                if text:
                    entry = claims.setdefault(claim_key(row["record_key"], text), {
                        "key": claim_key(row["record_key"], text), "record_key": row["record_key"],
                        "claim_text_as_quoted": text, "appearance_urls": set(), "review_url": fields["review_url"],
                        "cited": []})
                    entry["appearance_urls"].update(claim.get("appearance_urls") or [])
                    if claim.get("first_appearance_url"):
                        entry["appearance_urls"].add(claim["first_appearance_url"])
                    entry["cited"].append(cite)
                name = claim.get("claimant_as_named")
                if name:
                    entry = claimants.setdefault(claimant_key(name), {
                        "key": claimant_key(name), "names": set(), "same_as": set(), "types": set(),
                        "generic": is_generic(name), "cited": []})
                    entry["names"].add(name)
                    entry["same_as"].update(claim.get("claimant_same_as") or [])
                    if claim.get("claimant_type_as_published"):
                        entry["types"].add(claim["claimant_type_as_published"])
                    entry["cited"].append(cite)

        def finish(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for item in items:
                item = {k: sorted(v) if isinstance(v, set) else v for k, v in item.items()}
                seen, cited = set(), []
                for c in item["cited"]:
                    if c["revision_id"] not in seen:
                        seen.add(c["revision_id"])
                        cited.append(c)
                item["cited"] = cited[:20]
                out.append(item)
            return sorted(out, key=lambda s: s["key"])

        return {"claimants": finish(claimants.values()), "claims": finish(claims.values()),
                "publishers": finish(publishers.values())}

    # ------------------------------------------------------------------ targets (other owners, all optional)

    def _entity_aliases(self) -> dict[str, list[tuple[str, str | None]]]:
        if not (table_exists(self.conn, "entity_aliases") and table_exists(self.conn, "canonical_entities")):
            return {}
        rows = self.conn.execute(
            "SELECT a.surface_form, a.canonical_id, c.preferred_name FROM entity_aliases a JOIN canonical_entities c "
            "ON c.canonical_id=a.canonical_id ORDER BY a.surface_form, a.canonical_id").fetchall()
        out: dict[str, list[tuple[str, str | None]]] = {}
        for surface, canonical_id, name in rows:
            out.setdefault(str(surface), []).append((canonical_id, name))
        return out

    def _documents(self) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "documents"):
            return []
        columns = {r[0] for r in self.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='documents'").fetchall()}
        wanted = [c for c in ("document_id", "url", "canonical_url", "content_hash", "source_type", "title")
                  if c in columns]
        if "document_id" not in wanted or not {"url", "canonical_url"} & columns:
            return []
        rows = self.conn.execute("SELECT " + ", ".join(wanted) + " FROM documents ORDER BY document_id").fetchall()
        return [dict(zip(wanted, r)) for r in rows]

    def _argument_claims(self) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "argument_claims"):
            return []
        columns = {r[0] for r in self.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='argument_claims'").fetchall()}
        select = ["claim_id", "claim_text", "document_id"] + (["factcheck_url"] if "factcheck_url" in columns else [])
        rows = self.conn.execute("SELECT " + ", ".join(select) + " FROM argument_claims ORDER BY claim_id").fetchall()
        return [dict(zip(select, r)) for r in rows]

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                source_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable match assertions; idempotent, never an automatic merge or acceptance."""
        from src.kb.claim_links import _jaccard, _tokens
        from src.kb.entities import normalize_surface

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []
        claimant_allowed = "operator" in scopes or CLAIMANT_SCOPE in scopes
        # claimants -> canonical entities (identifier first, then name)
        aliases = self._entity_aliases()
        if not aliases:
            unavailable.append({"provider": "news.core", "target": "canonical_entities",
                                "reason": "no canonical entities or aliases in the warehouse"})
        elif not claimant_allowed:
            unavailable.append({"provider": "news.fact-checks", "target": "canonical_entities",
                                "reason": f"claimant matching needs {CLAIMANT_SCOPE} (FC01)"})
        for subject in subjects["claimants"] if aliases and claimant_allowed else []:
            if subject["generic"]:
                continue  # never proposed: a generic attribution is not an identity
            matched_by_identifier = False
            for same in subject["same_as"]:
                for surface in {same.casefold(), (_qid(same) or "").casefold()} - {""}:
                    for canonical_id, name in aliases.get(surface, []):
                        matched_by_identifier = True
                        offered.append(self._offer(namespace, "claimant-entity", subject["key"], subject["cited"][0],
                                                   canonical_id, "published-identifier", {
                                                       "identifier_as_published": same, "alias_surface": surface,
                                                       "entity_name": name, "names_as_named": subject["names"]},
                                                   principal_id))
            if matched_by_identifier:
                continue
            for name in subject["names"]:
                for canonical_id, preferred in aliases.get(normalize_surface(name), []):
                    offered.append(self._offer(namespace, "claimant-entity", subject["key"], subject["cited"][0],
                                               canonical_id, "alias-name", {
                                                   "name_as_named": name, "entity_name": preferred,
                                                   "types_as_published": subject["types"],
                                                   "note": "an equal name is a weak signal; a reviewer decides"},
                                               principal_id))
        # claims -> argument claims (published URLs first, then lexical overlap)
        arguments = self._argument_claims()
        documents = {d["document_id"]: d for d in self._documents()}
        if not arguments:
            unavailable.append({"provider": "news.core", "target": "argument_claims",
                                "reason": "no argument claims in the warehouse"})
        for subject in subjects["claims"]:
            appearances = {canonical_url(u)[0]: u for u in subject["appearance_urls"]}
            review = canonical_url(subject["review_url"])[0]
            tokens = _tokens(subject["claim_text_as_quoted"])
            for argument in arguments:
                document = documents.get(argument["document_id"]) or {}
                urls = {canonical_url(u)[0] for u in (document.get("url"), document.get("canonical_url")) if u}
                shared = sorted(set(appearances) & urls)
                if shared:
                    method, evidence = "appearance-url", {"appearance_url": appearances[shared[0]],
                                                         "document_id": argument["document_id"],
                                                         "rule": "wa-canon-v1 canonical URL equality"}
                elif argument.get("factcheck_url") and canonical_url(argument["factcheck_url"])[0] == review:
                    method, evidence = "review-url", {"factcheck_url_on_claim": argument["factcheck_url"],
                                                      "note": "stored by the legacy lookup; not a reviewed match"}
                else:
                    overlap = _jaccard(tokens, _tokens(str(argument.get("claim_text") or "")))
                    if overlap < LEXICAL_THRESHOLD:
                        continue
                    method, evidence = "lexical-overlap", {"jaccard": round(overlap, 3),
                                                           "threshold": LEXICAL_THRESHOLD,
                                                           "note": "shared words are a weak signal; a reviewer "
                                                                   "decides"}
                offered.append(self._offer(namespace, "claim-argument", subject["key"], subject["cited"][0],
                                           f"argument-claim:{argument['claim_id']}", method, {
                                               **evidence, "claim_text_as_quoted": subject["claim_text_as_quoted"],
                                               "argument_claim_text": argument.get("claim_text")}, principal_id))
        # publishers -> source identities by published domain
        if table_exists(self.conn, "source_alias_decisions"):
            from src.kb.source_identity import READ_SCOPE as SOURCE_READ
            from src.kb.source_identity import SourceIdentityStore

            sources = SourceIdentityStore(self.conn, initialize=False)
            for subject in subjects["publishers"]:
                resolved = sources.resolve_alias(source_namespace or namespace, "domain", subject["site"],
                                                 scopes={SOURCE_READ})
                for match in resolved["matches"]:
                    offered.append(self._offer(namespace, "publisher-source", subject["key"], subject["cited"][0],
                                               match["source_id"], "published-domain", {
                                                   "site_as_published": subject["site"],
                                                   "alias_decision_id": match["decision_id"],
                                                   "ambiguous": resolved["ambiguous"],
                                                   "names_as_published": subject["names"]}, principal_id))
        else:
            unavailable.append({"provider": "news.core", "target": "source_identities",
                                "reason": "no source identities in the warehouse"})
        return {"proposed": sorted({o["match_id"] for o in offered if o["change"]}),
                "matches": self.matches(namespace, scopes=scopes), "unavailable": unavailable,
                "notice": "match assertions are proposals; nothing is merged or accepted automatically"}

    def _offer(self, namespace, kind, left_key, cited, right_key, method, evidence, principal_id) -> dict[str, Any]:
        match_id = "fc-match:" + digest([namespace, kind, left_key, right_key])[:24]
        evidence = [{**evidence, "method": method, "left": {"record_key": cited["record_key"],
                                                             "revision_id": cited["revision_id"],
                                                             "source_id": cited["source_id"]}}]
        now = self.now()
        row = self.conn.execute("SELECT state, method, evidence_json, history_json FROM fact_check_matches WHERE "
                                "namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO fact_check_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, match_id, kind, left_key, cited["revision_id"], right_key, method, CONFIDENCE[method],
                 canonical(evidence), "proposed", None, principal_id, now,
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": now}])])
            return {"match_id": match_id, "change": "created"}
        state, old_method, old_evidence, history = row[0], row[1], json.loads(row[2]), json.loads(row[3])
        stronger = CONFIDENCE[method] > CONFIDENCE[old_method]
        fresh = digest(evidence) != digest(old_evidence) or method != old_method
        if state == "proposed" and stronger:
            change = "upgraded"
        elif state in {"rejected", "reverted"} and fresh:
            change = "reproposed"
        else:
            return {"match_id": match_id, "change": None}
        history.append({"state": "proposed", "by": principal_id, "at_ms": now, "change": change,
                        "previous_state": state, "previous_method": old_method, "previous_evidence": old_evidence})
        self.conn.execute(
            "UPDATE fact_check_matches SET state='proposed', decision_id=NULL, method=?, confidence=?, evidence_json=?, "
            "left_revision_id=?, history_json=? WHERE namespace=? AND match_id=?",
            [method, CONFIDENCE[method], canonical(evidence), cited["revision_id"], canonical(history), namespace,
             match_id])
        return {"match_id": match_id, "change": change}

    # ------------------------------------------------------------------ review and reads

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, match_kind, left_key, left_revision_id, right_key, method, confidence, evidence_json, "
            "state, decision_id, created_by, created_at_ms, history_json FROM fact_check_matches WHERE namespace=? "
            "AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise FactCheckError("not_found", "no fact-check match with that id")
        view = dict(zip(("match_id", "match_kind", "left_key", "left_revision_id", "right_key", "method",
                         "confidence"), row[:7]))
        history = json.loads(row[12])
        last = history[-1]
        return {"contract": CONTRACT, "namespace": namespace, **view, "evidence": json.loads(row[7]),
                "state": row[8],
                "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                                 "reverted": "reverted", "proposed": "unreviewed-candidate"}[row[8]],
                "decision_id": row[9], "created_by": row[10], "created_at_ms": row[11],
                "reviewer": last.get("by") if row[8] != "proposed" else None, "reason": last.get("reason"),
                "history": history, "notice": "a reviewable match assertion; records are never merged"}

    def matches(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None, key: str | None = None,
                state: str | None = None) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM fact_check_matches WHERE namespace=? AND (? IS NULL OR match_kind=?) AND "
            "(? IS NULL OR state=?) AND (? IS NULL OR left_key=? OR right_key=? OR left_key LIKE ?) "
            "ORDER BY match_kind, left_key, right_key",
            [namespace, kind, kind, state, state, key, key, key, f"{key}#%" if key else None]).fetchall()
        views = [self._row(namespace, r[0]) for r in rows]
        if "operator" not in scopes and CLAIMANT_SCOPE not in scopes:
            views = [v for v in views if v["match_kind"] != "claimant-entity"]
        return views

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposed match; claimant matches are also entity identity decisions."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise FactCheckError("invalid_decision", "accept or reject with a reason")
        match = self._row(namespace, match_id)
        self._claimant_guard(match, scopes)
        if match["state"] != "proposed":
            raise FactCheckError("invalid_state", f"match is {match['state']}; propose again to re-review")
        decision_id = None
        if match["match_kind"] in {"claimant-entity", "publisher-source"}:
            decision_id = self._decide(namespace, match, "match" if decision == "accept" else "non-match", reason,
                                       principal_id)
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected", decision_id,
                                principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str, scopes: Iterable[str]
               ) -> dict[str, Any]:
        """Undo an accepted or rejected decision; the match becomes ``reverted`` and records stay intact."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise FactCheckError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        self._claimant_guard(match, scopes)
        if match["state"] not in {"accepted", "rejected"}:
            raise FactCheckError("invalid_state", "only an accepted or rejected match can be reverted")
        decision_id = None
        if match["decision_id"]:
            from src.kb.entity_history import EntityHistoryStore

            undo = EntityHistoryStore(self.conn, now=self.now).undo(
                namespace, match["decision_id"], reviewer_id=principal_id, principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES)
            decision_id = undo["decision_id"]
        return self._transition(namespace, match, "reverted", decision_id, principal_id, reason.strip())

    @staticmethod
    def _claimant_guard(match: Mapping[str, Any], scopes: set[str]) -> None:
        if match["match_kind"] == "claimant-entity" and "operator" not in scopes and CLAIMANT_SCOPE not in scopes:
            raise FactCheckError("unauthorized", f"claimant matches need {CLAIMANT_SCOPE} (FC01)")

    def _decide(self, namespace, match, decision_type, reason, principal_id) -> str:
        from src.kb.entity_history import EntityHistoryStore

        history = EntityHistoryStore(self.conn, now=self.now)
        subjects = [match["left_key"], match["right_key"]]
        for subject in subjects:
            history.register_entity(namespace, subject, [subject], principal_id=principal_id,
                                    scopes=_ENTITY_HISTORY_SCOPES)
        recorded = history.decide(
            namespace, decision_type, subjects,
            {"match_id": match["match_id"], "method": match["method"], "confidence": match["confidence"],
             "evidence": match["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "news.fact-checks", "records": subjects},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"fact-check-match:{namespace}:{match['match_id']}")
        return recorded["decision_id"]

    def _transition(self, namespace, match, state, decision_id, principal_id, reason) -> dict[str, Any]:
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                       "decision_id": decision_id}]
        self.conn.execute("UPDATE fact_check_matches SET state=?, decision_id=?, history_json=? WHERE namespace=? "
                          "AND match_id=?", [state, decision_id, canonical(history), namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    def accepted(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None, key: str | None = None
                 ) -> list[dict[str, Any]]:
        return self.matches(namespace, scopes=scopes, kind=kind, key=key, state="accepted")

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Subjects without an accepted match, visible as unmatched (claimants only with the claimant scope)."""
        scopes = set(scopes)
        subjects = self.subjects(namespace, scopes=scopes)
        accepted = {m["left_key"] for m in self.matches(namespace, scopes=scopes | {CLAIMANT_SCOPE}, state="accepted")}
        out = {kind: [{"key": s["key"], "state": "unmatched", **({"names": s["names"]} if "names" in s else {}),
                       **({"claim_text_as_quoted": s["claim_text_as_quoted"]} if "claim_text_as_quoted" in s
                          else {})}
                      for s in subjects[kind] if s["key"] not in accepted]
               for kind in ("claims", "publishers")}
        if "operator" in scopes or CLAIMANT_SCOPE in scopes:
            out["claimants"] = [{"key": s["key"], "names": s["names"], "generic": s["generic"], "state": "unmatched"}
                                for s in subjects["claimants"] if s["key"] not in accepted]
        else:
            out["claimants"] = {"withheld": f"claimants are listed with {CLAIMANT_SCOPE} (FC01)"}
        return out

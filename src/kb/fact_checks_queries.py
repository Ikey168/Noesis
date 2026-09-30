"""Fact-checks of a claim or claimant as of a date, and fact-checks that cite a news article (#2659, FC08, FC09).

Both answers read revisions as they were in force on the as-of date (by the
publisher's own dates) and as known at the end of that day, and cite every
fact-check revision they use with its source, record revision and as-of times.

* :meth:`FactCheckQueries.for_claim_or_claimant` - a claim is an
  ``argument_claims`` id reached only through **accepted** FC06 claim matches; a
  claimant is a canonical entity reached through accepted claimant matches, or the
  claimant's name exactly as a publisher published it (a filter on the published
  text, labelled as such, never a match). Ratings are shown verbatim with any
  publisher scale; when publishers rated the same claim differently their ratings
  are shown **side by side** and never reconciled. Each fact-check carries its
  publisher's IFCN status at the review date as then known.
* :meth:`FactCheckQueries.citing` - a news article or URL is compared with each
  fact-check's appearance and first-appearance URLs under the ``wa-canon-v1``
  rules (stated in the answer); a social-platform appearance, stored as a digest,
  matches only when the caller supplies its URL. Archived captures of the URL near
  each review date are added from the web-archive store when present.

Nothing here issues a truth verdict, normalises a rating or matches a claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.fact_checks_sources import EXCLUSIONS, MINIMISATION_POLICY, URL_RULES
from src.kb.fact_checks_identity import FactCheckIdentity, claimant_key
from src.kb.fact_checks_records import (
    READ_SCOPE,
    FactCheckError,
    authorize,
    day_ms,
    forbidden_keys,
    iso,
    table_exists,
)

ANSWER_CONTRACT = "noesis-fact-check-answer-v1"
NOTICE = ("Ratings are each publisher's own, shown verbatim; Noesis issues no verdict and does not reconcile "
          "differing ratings.")


def _today_ms(conn: Any) -> int:
    import time

    del conn
    return int(time.time() * 1000)


def url_rule(url: str) -> dict[str, Any]:
    from src.ingestion.fact_checks_sources import canonical_url
    from src.kb.web_archive_identity import RULES

    canonical, applied = canonical_url(url)
    return {"rules": URL_RULES, "input": url, "canonical_url": canonical, "applied": applied,
            "rule_text": [f"{rule}: {text}" for rule, text in RULES],
            "social_platforms": "appearances on social platforms are stored as the SHA-256 of this canonical URL and "
                                "match only when the same URL is supplied"}


class FactCheckQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.identity = FactCheckIdentity(conn, initialize=False)
        self.store = self.identity.store

    # ------------------------------------------------------------------ shared

    @staticmethod
    def _window(as_of: str | None, known_at: str | None = None) -> tuple[str | None, int | None]:
        """The publication cut-off day (publisher dates) and, optionally, the end of the day records were known by."""
        day = str(as_of)[:10] if as_of else None
        return day, day_ms(str(known_at)[:10]) if known_at else None

    def _publisher_status(self, namespace: str, publisher_key: str, review_day: str | None, known_at: int | None,
                          scopes: set[str]) -> dict[str, Any]:
        if not self.store.ready() or not self.conn.execute(
                "SELECT 1 FROM fact_check_records WHERE namespace=? AND record_kind='publisher' LIMIT 1",
                [namespace]).fetchone():
            return {"status": "unavailable", "reason": "no IFCN signatory listing acquired (fact-checks-ifcn feature "
                                                       "absent or not run)"}
        rows = self.store.as_of(namespace, publisher_key, scopes=scopes, day=review_day, known_at_ms=known_at)
        if not rows:
            return {"status": "no_signatory_record", "reason": "no IFCN signatory record for this publisher's domain "
                                                               "in force on the review date as then known"}
        row = rows[0]
        fields = row["record"]["fields"]
        if row["status"] != "published":
            return {"status": row["status"], "in_force_since": row["in_force_since"], "citation": row["citation"]}
        return {"status": fields["ifcn_status"], "status_as_published": fields["status_as_published"],
                "status_dates": fields["status_dates"], "in_force_since": row["in_force_since"],
                "signatory_name_as_published": fields["name_as_published"], "citation": row["citation"]}

    def _fact_checks(self, namespace: str, record_keys: Iterable[str], *, day: str | None, known_at: int | None,
                     scopes: set[str]) -> list[dict[str, Any]]:
        out = []
        for key in sorted(set(record_keys)):
            revisions = [r for r in self.store.as_of(namespace, key, scopes=scopes, day=day, known_at_ms=known_at)
                         if r["status"] == "published"]
            if not revisions:
                continue
            first = revisions[0]["record"]["fields"]
            claim = first["claims"][0]
            publisher_name = next((r["record"]["fields"]["publisher"]["name_as_published"] for r in revisions
                                   if r["record"]["fields"]["publisher"]["name_as_published"]), None)
            review_day = max((r["record"]["fields"]["review_day"] or "" for r in revisions), default="") or None
            out.append({
                "record_key": key, "review_key": revisions[0]["review_key"],
                "publisher": {"name_as_published": publisher_name,
                              "domain": first["publisher"]["domain"], "publisher_key": revisions[0]["publisher_key"]},
                "review_url": first["review_url"], "review_title": first["review_title"],
                "claim": {"text_as_quoted": claim["claim_text"], "claimant_as_named": claim.get("claimant"),
                          "claim_date": claim.get("claim_date")},
                "as_published_by_source": [{
                    "source_id": r["source_id"], "provider": r["provider"],
                    "review_date": r["record"]["fields"]["review_date"],
                    "rating_as_published": r["record"]["fields"]["claims"][0]["rating"],
                    "language": r["record"]["fields"]["language"], "in_force_since": r["in_force_since"],
                    "citation": r["citation"]} for r in revisions],
                "publisher_status_at_review": self._publisher_status(namespace, revisions[0]["publisher_key"],
                                                                     review_day, known_at, scopes),
            })
        return out

    @staticmethod
    def _side_by_side(fact_checks: list[dict[str, Any]]) -> dict[str, Any]:
        by_claim: dict[str, list[dict[str, Any]]] = {}
        for item in fact_checks:
            by_claim.setdefault(item["claim"]["text_as_quoted"], []).append(item)
        groups = []
        for text, items in sorted(by_claim.items()):
            ratings = [{"publisher": i["publisher"]["name_as_published"], "domain": i["publisher"]["domain"],
                        "review_url": i["review_url"],
                        "ratings_as_published": sorted({(s["rating_as_published"]["text"] or "")
                                                        for s in i["as_published_by_source"]}),
                        "scales_as_published": sorted({s["rating_as_published"]["scale_as_published"]
                                                       for s in i["as_published_by_source"]
                                                       if s["rating_as_published"]["scale_as_published"]})}
                       for i in items]
            texts = {t.strip().casefold() for r in ratings for t in r["ratings_as_published"] if t}
            groups.append({"claim_as_quoted": text, "publishers": ratings,
                           "rating_texts_differ": len({r["domain"] for r in ratings}) > 1 and len(texts) > 1})
        return {"groups": groups, "basis": "verbatim rating text per publisher, compared as text only; no rating is "
                                           "normalised, ranked or reconciled"}

    def _envelope(self, query: str, namespace: str, day: str | None, known_at: int | None,
                  fact_checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        answer = {
            "contract": ANSWER_CONTRACT, "query": query, "namespace": namespace, "as_of": day,
            "known_at": iso(known_at) if known_at is not None else None,
            "status": "answered" if fact_checks else "none_on_record", "fact_checks": fact_checks,
            "side_by_side": self._side_by_side(fact_checks), "exclusions": list(EXCLUSIONS),
            "minimisation": MINIMISATION_POLICY, "notice": NOTICE, **extra,
        }
        if forbidden_keys(answer):
            raise FactCheckError("assessment_forbidden", "an answer may not carry a verdict or normalised rating")
        return answer

    # ------------------------------------------------------------------ FC08

    def for_claim_or_claimant(self, namespace: str, *, scopes: Iterable[str], claim_id: str | None = None,
                              claimant: str | None = None, as_of: str | None = None,
                              known_at: str | None = None) -> dict[str, Any]:
        """Fact-checks published by ``as_of`` of an argument claim or a claimant, ratings verbatim and cited.

        ``as_of`` is a cut-off on the publishers' own dates (the review revision in force that day); ``known_at``
        additionally restricts the answer to what had been acquired by the end of that day (default: everything).
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if bool(claim_id) == bool(claimant):
            raise FactCheckError("invalid_request", "name a claim_id or a claimant")
        day, known_ms = self._window(as_of, known_at)
        keys: set[str] = set()
        used, unreviewed = [], []
        if claim_id:
            for match in self.identity.candidates(namespace, scopes=scopes, match_kind="claim", key=claim_id):
                (used if match["state"] == "accepted" else unreviewed).append(match)
            keys = {m["left_key"] for m in used}
            subject = {"claim_id": claim_id, "basis": "accepted claim matches (FC06) only"}
        else:
            subjects = {s["subject_key"]: s for s in self.identity.subjects(namespace, scopes=scopes)["claimant"]}
            if str(claimant).startswith("ent-"):
                for match in self.identity.candidates(namespace, scopes=scopes, match_kind="claimant", key=claimant):
                    (used if match["state"] == "accepted" else unreviewed).append(match)
                claimant_subjects = {m["left_key"] for m in used}
                basis = "accepted claimant matches (FC06) only"
            else:
                key = claimant if str(claimant).startswith("fact-check:claimant:") else claimant_key(
                    {"name_as_published": claimant})
                claimant_subjects = {key} if key else set()
                basis = "the claimant as a publisher named it (a filter on published text, not an identity match)"
            for key in claimant_subjects:
                keys |= set((subjects.get(key) or {}).get("fact_checks") or [])
            subject = {"claimant": claimant, "claimant_subjects": sorted(claimant_subjects), "basis": basis}
        fact_checks = self._fact_checks(namespace, keys, day=day, known_at=known_ms, scopes=scopes)
        return self._envelope(
            "claim" if claim_id else "claimant", namespace, day, known_ms, fact_checks, subject=subject,
            identity={"accepted": [{"candidate_id": m["candidate_id"], "record": m["left_key"],
                                    "method": m["method"], "decision_id": m["decision_id"],
                                    "reviewer": m["reviewer"]} for m in used],
                      "unreviewed_candidates_not_used": [m["candidate_id"] for m in unreviewed
                                                         if m["state"] == "proposed"]})

    # ------------------------------------------------------------------ FC09

    def _document_url(self, document_id: str) -> str:
        if not table_exists(self.conn, "documents"):
            raise FactCheckError("provider_absent", "no news documents store")
        row = self.conn.execute("SELECT coalesce(canonical_url, url) FROM documents WHERE document_id=?",
                                [document_id]).fetchone()
        if not row or not row[0]:
            raise FactCheckError("not_found", "no document with a URL under that id")
        return str(row[0])

    def _archives(self, namespace: str, url: str, review_day: str | None, scopes: set[str]) -> dict[str, Any]:
        if not table_exists(self.conn, "web_archive_captures"):
            return {"status": "unavailable", "reason": "no web-archive capture store (platform.web-archives absent)"}
        from src.kb.citation_preservation import READ_SCOPE as CITATION_READ
        from src.kb.web_archive_queries import page_as_of

        if CITATION_READ not in scopes and "operator" not in scopes:
            return {"status": "unavailable", "reason": f"{CITATION_READ} is required to read archived captures"}
        try:
            answer = page_as_of(self.conn, namespace, url, review_day or iso(_today_ms(self.conn)), scopes=scopes)
        except Exception as exc:  # noqa: BLE001 - archives are optional; report, never fail the answer
            return {"status": "unavailable", "reason": getattr(exc, "code", type(exc).__name__)}
        return {"status": answer["status"], "statement": answer["statement"], "closest": answer["closest"],
                "cites": answer["cites"]}

    def citing(self, namespace: str, *, scopes: Iterable[str], url: str | None = None, document_id: str | None = None,
               as_of: str | None = None, known_at: str | None = None) -> dict[str, Any]:
        """Fact-checks that cite a news article or URL as an appearance, with the matching rule and archives."""
        from src.ingestion.fact_checks_sources import url_digest

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if bool(url) == bool(document_id):
            raise FactCheckError("invalid_request", "name a url or a document_id")
        target = url or self._document_url(str(document_id))
        rule = url_rule(target)
        wanted, wanted_hash = rule["canonical_url"], url_digest(target)
        day, known_ms = self._window(as_of, known_at)
        views = self.store.records(namespace, scopes=scopes, kinds=["fact-check"], known_at_ms=known_ms)
        matched: dict[str, list[dict[str, Any]]] = {}
        for view in views:
            claim = view["record"]["fields"]["claims"][0]
            entries = [("appearance", a) for a in claim.get("appearances") or []]
            if claim.get("first_appearance"):
                entries.append(("first_appearance", claim["first_appearance"]))
            for role, entry in entries:
                hit = (entry.get("url_canonical") == wanted if not entry.get("platform_post")
                       else entry.get("url_sha256") == wanted_hash)
                if hit:
                    matched.setdefault(view["record_key"], []).append(
                        {"role": role, "source_id": view["source_id"], "revision_id": view["revision_id"],
                         "match": "canonical-url" if not entry.get("platform_post") else "url-digest",
                         "cited_as_published": entry.get("url")})
        fact_checks = self._fact_checks(namespace, matched, day=day, known_at=known_ms, scopes=scopes)
        for item in fact_checks:
            item["cites_target_as"] = matched[item["record_key"]]
            review_days = sorted({s["review_date"][:10] for s in item["as_published_by_source"] if s["review_date"]})
            item["archived_captures"] = self._archives(namespace, target, review_days[0] if review_days else None,
                                                       scopes)
        return self._envelope("citing", namespace, day, known_ms, fact_checks, subject={
            "url": url, "document_id": document_id, "url_rule": rule})

    # ------------------------------------------------------------------ evidence bundle

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the fact-check revision behind them (source, record revision and as-of times)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []
        for item in answer.get("fact_checks") or []:
            for entry in item["as_published_by_source"]:
                citation = entry["citation"]
                bibliography.setdefault(citation["revision_id"], {
                    "id": citation["revision_id"],
                    "text": f"{item['publisher']['name_as_published']}, {item['review_title'] or 'review'} "
                            f"({entry['review_date']}), {item['review_url']}; via {citation['provider']} (source "
                            f"{citation['source_id']}, record {citation['record_key']}, revision "
                            f"{citation['revision_no']}, in force since {entry['in_force_since']}, observed "
                            f"{citation['observed_at']}, {citation['evidence_origin']} evidence)"})
                rating = entry["rating_as_published"]
                assertions.append({
                    "id": f"{item['record_key']}@{citation['source_id']}", "kind": "sourced",
                    "text": f"{item['publisher']['name_as_published']} rated the claim \"{item['claim']['text_as_quoted']}"
                            f"\" as \"{rating['text']}\"" + (f" ({rating['scale_as_published']})"
                                                             if rating.get("scale_as_published") else "")
                            + " (the publisher's rating, verbatim)",
                    "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                      "id": citation["record_key"], "revision": citation["revision_id"],
                                      "locator": {"url": citation["locator"]}}],
                    "citations": [citation["revision_id"]]})
            status = item.get("publisher_status_at_review") or {}
            if status.get("citation"):
                citation = status["citation"]
                bibliography.setdefault(citation["revision_id"], {
                    "id": citation["revision_id"],
                    "text": f"IFCN signatory listing, {citation['record_key']} (source {citation['source_id']}, "
                            f"revision {citation['revision_no']}, "
                            f"in force since {status.get('in_force_since')}, observed {citation['observed_at']}, "
                            f"{citation['evidence_origin']} evidence), {citation['locator']}"})
                assertions.append({
                    "id": f"{item['record_key']}#publisher-status", "kind": "sourced",
                    "text": f"IFCN status of {item['publisher']['domain']} at the review date: "
                            f"{status.get('status_as_published') or status.get('status')}",
                    "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                      "id": citation["record_key"], "revision": citation["revision_id"],
                                      "locator": {"url": citation["locator"]}}],
                    "citations": [citation["revision_id"]]})
        subject = answer.get("subject") or {}
        title = subject.get("claim_id") or subject.get("claimant") or subject.get("url") or subject.get("document_id")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"Fact-checks for {title} as of "
                                                                             f"{answer.get('as_of') or 'latest'}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS),
                "notice": NOTICE}

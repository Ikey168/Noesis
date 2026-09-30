"""Fact-checks of a claim or claimant as of a date, and fact-checks citing a news article (#2659, FC08, FC09).

Answers are read-only views over the fact-check store, the reviewed matches of
:mod:`src.kb.fact_checks_identity` and (optionally) the web-archive captures
of the citation preservation store:

* every rating is shown exactly as published - the textual rating and any
  numeric value with the publisher's own best/worst scale - and ratings from
  different publishers are shown side by side, never merged, averaged or
  mapped onto a common scale;
* a claim reaches fact-checks only through an *accepted* claim match; unreviewed
  candidates are listed separately and never answer. A text query is a search
  over quoted claims, labelled as such, not a match;
* "as of" a date is the revision each source had published by then (review or
  status date) - later reviews are left out and counted; the publisher's IFCN
  status is the signatory revision in effect on the review date;
* URL lookups use the versioned ``wa-canon-v1`` canonicalisation and say which
  rules applied; archived captures of the URL are listed when the web-archives
  provider holds them, otherwise their absence is reported;
* every answer cites each fact-check revision with its source, revision number
  and observation time.

No truth verdict by Noesis and no normalised rating appear in any answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.fact_checks_sources import EXCLUSIONS, canonical_url, url_rule
from src.kb.fact_checks_identity import (
    FactCheckIdentity,
    claim_key,
    claimant_key,
    normalise_claim,
)
from src.kb.fact_checks_records import (
    CLAIMANT_SCOPE,
    READ_SCOPE,
    FactCheckError,
    FactChecksStore,
    authorize,
    table_exists,
)

ANSWER_CONTRACT = "noesis-fact-checks-answer-v1"
NOTICE = ("Ratings are the publishers' own, quoted as published with their scales; Noesis gives no verdict and does "
          "not normalise ratings.")


class FactCheckQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = FactChecksStore(conn, initialize=False)
        self.identity = FactCheckIdentity(conn, initialize=False)

    # ------------------------------------------------------------------ building blocks

    def publisher_status(self, namespace: str, site: str | None, day: str | None, *, scopes: Iterable[str]
                         ) -> dict[str, Any]:
        """The IFCN signatory status in effect on a day for the signatory whose website domain is ``site``."""
        if not site:
            return {"status": "no_publisher_site"}
        rows = self.store.records(namespace, scopes=scopes, kinds=["publisher"], publisher_site=site)
        if not rows:
            return {"status": "no_signatory_record_for_site", "site": site,
                    "note": "absence from the acquired listing is not a statement about the publisher"}
        out = []
        for row in rows:
            in_effect = self.store.as_of(namespace, row["record_key"], day, scopes=scopes, source_id=row["source_id"])
            if not in_effect:
                history = self.store.history(namespace, row["record_key"], scopes=scopes, source_id=row["source_id"])
                out.append({"status": "status_unknown_on_date", "record_key": row["record_key"], "day": day,
                            "earliest_known": FactChecksStore.effective_day(history[0]) if history else None})
                continue
            revision = in_effect[0]
            fields = revision["record"]["fields"]
            out.append({
                "status": "known", "record_key": row["record_key"], "day": day,
                "name_as_published": fields.get("name_as_published"),
                "status_as_published": fields.get("status_as_published") if revision["status"] == "published"
                else None, "listing_status": revision["status"],
                "status_date_as_published": fields.get("status_date_as_published"),
                "status_date_label": fields.get("status_date_label"),
                "basis": "shared identifier: the signatory's published website domain equals the review's publisher "
                         "site",
                "citation": revision["citation"]})
        return {"status": "answered", "site": site, "signatories": out}

    def _claims_view(self, claims: Iterable[Mapping[str, Any]], record_key: str, keys: set[str] | None
                     ) -> list[dict[str, Any]]:
        out = []
        for claim in claims:
            key = claim_key(record_key, claim.get("claim_text_as_quoted"))
            if keys is not None and key not in keys:
                continue
            out.append({"claim_key": key, **dict(claim)})
        return out

    def _items(self, namespace: str, record_keys: dict[str, set[str] | None], as_of: str | None, *,
               scopes: set[str], basis: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """One item per fact-check with every source's revision in effect as of the date."""
        items, later = [], 0
        for record_key in sorted(record_keys):
            revisions = self.store.as_of(namespace, record_key, as_of, scopes=scopes)
            if not revisions:
                later += 1
                continue
            base = revisions[0]["record"]
            fields = base["fields"]
            assertions = []
            for revision in revisions:
                rf = revision["record"]["fields"]
                assertions.append({
                    "source_id": revision["source_id"], "provider": revision["record"]["provider"],
                    "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
                    "status": revision["status"], "review_date": rf.get("review_date"),
                    "claims": self._claims_view(rf.get("claims") or [], record_key, record_keys[record_key]),
                    "rating_scale_note": rf.get("rating_scale_note"), "sd_license": rf.get("sd_license"),
                    "citation": revision["citation"]})
            names = [(r["record"]["fields"].get("publisher") or {}).get("name_as_published") for r in revisions]
            publisher = {**(fields.get("publisher") or {}),
                         "name_as_published": next((n for n in names if n), None)}
            items.append({
                "record_key": record_key, "publisher": publisher, "review_url": fields.get("review_url"),
                "review_title": fields.get("review_title"), "language": base.get("language"),
                "review_date": max((a["review_date"] or "" for a in assertions), default=None) or None,
                "withdrawn_by_source": all(a["status"] != "published" for a in assertions),
                "source_assertions": assertions,
                "publisher_status_at_review": self.publisher_status(
                    namespace, (fields.get("publisher") or {}).get("site"), fields.get("review_date"),
                    scopes=scopes),
                "basis": basis.get(record_key) or basis.get("*")})
        return items, later

    @staticmethod
    def side_by_side(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Ratings of the same quoted claim by different publishers, each verbatim; never merged or averaged."""
        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            for assertion in item["source_assertions"]:
                if assertion["status"] != "published":
                    continue
                for claim in assertion["claims"]:
                    group = groups.setdefault(normalise_claim(claim.get("claim_text_as_quoted")), {
                        "claim_texts_as_quoted": set(), "ratings": {}})
                    group["claim_texts_as_quoted"].add(claim.get("claim_text_as_quoted"))
                    rating = claim.get("rating") or {}
                    group["ratings"].setdefault((item["record_key"], assertion["source_id"]), {
                        "publisher": (item.get("publisher") or {}).get("name_as_published"),
                        "publisher_site": (item.get("publisher") or {}).get("site"),
                        "record_key": item["record_key"], "source_id": assertion["source_id"],
                        "revision_id": assertion["revision_id"], "review_date": assertion["review_date"],
                        "claimant_as_named": claim.get("claimant_as_named"), **rating})
        out = []
        for _, group in sorted(groups.items()):
            ratings = sorted(group["ratings"].values(), key=lambda r: (r["publisher_site"] or "", r["source_id"]))
            out.append({"claim_texts_as_quoted": sorted(t for t in group["claim_texts_as_quoted"] if t),
                        "ratings_as_published": ratings,
                        "publishers": sorted({r["publisher_site"] for r in ratings if r["publisher_site"]}),
                        "distinct_textual_ratings": sorted({r.get("textual_rating") or "" for r in ratings} - {""}),
                        "grouping_basis": "identical quoted claim text after case, whitespace and punctuation "
                                          "folding; not a reviewed claim match"})
        return out

    def _answer(self, namespace, query, key, as_of, items, later, **extra) -> dict[str, Any]:
        return {"contract": ANSWER_CONTRACT, "namespace": namespace, "query": query, "key": key,
                "as_of": str(as_of)[:10] if as_of else None,
                "status": "answered" if items else "none_on_record", "fact_checks": items,
                "ratings_side_by_side": self.side_by_side(items), "later_reviews_not_shown": later,
                "exclusions": list(EXCLUSIONS), "notice": NOTICE, **extra}

    # ------------------------------------------------------------------ FC08

    def for_claim(self, namespace: str, *, scopes: Iterable[str], claim_id: str | None = None,
                  text: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        """Fact-checks of an argument claim (accepted matches only) or of a quoted-claim text search."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if bool(claim_id) == bool(text):
            raise FactCheckError("invalid_request", "give an argument claim id or a claim text, not both")
        keys: dict[str, set[str] | None] = {}
        basis: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if claim_id:
            target = f"argument-claim:{claim_id}"
            for match in self.identity.matches(namespace, scopes=scopes, kind="claim-argument", key=target):
                record_key = match["left_key"].split("#claim=", 1)[0]
                if match["state"] == "accepted":
                    keys.setdefault(record_key, set()).add(match["left_key"])
                    basis[record_key] = {"kind": "accepted-match", "match_id": match["match_id"],
                                         "method": match["method"], "reviewer": match["reviewer"]}
            extra["unreviewed_candidates"] = [
                {"match_id": m["match_id"], "claim_key": m["left_key"], "method": m["method"],
                 "confidence": m["confidence"], "state": m["state"]}
                for m in self.identity.matches(namespace, scopes=scopes, kind="claim-argument", key=target)
                if m["state"] == "proposed"]
            extra["matching"] = "accepted claim matches only; candidates are listed, never answered"
        else:
            needle = normalise_claim(text)
            if len(needle) < 3:
                raise FactCheckError("invalid_request", "a claim search needs at least three characters")
            for row in self.store.records(namespace, scopes=scopes, kinds=["fact-check"]):
                for claim in row["record"]["fields"].get("claims") or []:
                    if needle in normalise_claim(claim.get("claim_text_as_quoted")):
                        keys.setdefault(row["record_key"], set()).add(
                            claim_key(row["record_key"], claim.get("claim_text_as_quoted")))
            basis["*"] = {"kind": "text-search", "normalised_query": needle,
                          "note": "a search over quoted claim texts, not a reviewed claim match"}
        items, later = self._items(namespace, keys, as_of, scopes=scopes, basis=basis)
        return self._answer(namespace, "claim", claim_id or text, as_of, items, later, **extra)

    def for_claimant(self, namespace: str, claimant: str, *, scopes: Iterable[str], as_of: str | None = None
                     ) -> dict[str, Any]:
        """Fact-checks of a claimant as named, or of every claimant accepted as matching a canonical entity."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if "operator" not in scopes and CLAIMANT_SCOPE not in scopes:
            raise FactCheckError("unauthorized", f"claimant queries need {CLAIMANT_SCOPE} (FC01)")
        wanted: set[str] = set()
        basis: dict[str, Any] = {}
        if str(claimant).startswith("ent-"):
            for match in self.identity.accepted(namespace, scopes=scopes, kind="claimant-entity", key=claimant):
                wanted.add(match["left_key"])
            basis["*"] = {"kind": "accepted-match", "entity": claimant}
        else:
            wanted.add(claimant_key(claimant))
            basis["*"] = {"kind": "claimant-as-named", "note": "the claimant name as the publisher named it"}
        keys: dict[str, set[str] | None] = {}
        for row in self.store.records(namespace, scopes=scopes, kinds=["fact-check"]):
            for claim in row["record"]["fields"].get("claims") or []:
                if claim.get("claimant_as_named") and claimant_key(claim["claimant_as_named"]) in wanted:
                    keys.setdefault(row["record_key"], set()).add(
                        claim_key(row["record_key"], claim.get("claim_text_as_quoted")))
        items, later = self._items(namespace, keys, as_of, scopes=scopes, basis=basis)
        return self._answer(namespace, "claimant", claimant, as_of, items, later, claimant_keys=sorted(wanted))

    # ------------------------------------------------------------------ FC09

    def citing(self, namespace: str, *, scopes: Iterable[str], url: str | None = None,
               document_id: str | None = None, as_of: str | None = None, archive_namespace: str | None = None
               ) -> dict[str, Any]:
        """Fact-checks whose revision in effect cites a URL (or a news document's URL) as appearance or review."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if bool(url) == bool(document_id):
            raise FactCheckError("invalid_request", "give a URL or a document id, not both")
        urls = [url] if url else []
        if document_id:
            if not table_exists(self.conn, "documents"):
                raise FactCheckError("provider_unavailable", "no news documents table (news.core absent)")
            row = self.conn.execute("SELECT url, canonical_url FROM documents WHERE document_id=?",
                                    [document_id]).fetchone()
            if row is None:
                raise FactCheckError("not_found", "no news document with that id")
            urls = sorted({u for u in row if u})
        canonical_keys = {}
        for item in urls:
            key, rules = canonical_url(item)
            canonical_keys[key] = {"url": item, "rules_applied": rules}
        roles: dict[str, dict[str, set[str]]] = {}
        for key in canonical_keys:
            for hit in self.store.by_url(namespace, key, scopes=scopes):
                roles.setdefault(hit["record_key"], {}).setdefault(hit["revision_id"], set()).add(hit["role"])
        keys: dict[str, set[str] | None] = {k: None for k in roles}
        items, later = self._items(namespace, keys, as_of, scopes=scopes, basis={"*": {"kind": "citation"}})
        kept = []
        for item in items:
            cited = {rid: sorted(r) for rid, r in roles[item["record_key"]].items()}
            item["source_assertions"] = [a for a in item["source_assertions"] if a["revision_id"] in cited]
            if not item["source_assertions"]:
                later += 1  # the revision in effect on the date does not cite the URL
                continue
            for assertion in item["source_assertions"]:
                assertion["cites_as"] = cited[assertion["revision_id"]]
            kept.append(item)
        return self._answer(namespace, "citing", url or document_id, as_of, kept, later,
                            url_matching={**url_rule(), "input": canonical_keys},
                            archived_captures=self._captures(archive_namespace or namespace, urls, scopes))

    def _captures(self, namespace: str, urls: list[str], scopes: set[str]) -> dict[str, Any]:
        if not table_exists(self.conn, "web_archive_captures"):
            return {"status": "unavailable", "provider": "platform.web-archives",
                    "reason": "no web-archive captures in the warehouse"}
        from src.kb.citation_preservation import READ_SCOPE as CITATION_READ
        from src.kb.citation_preservation import CitationPreservationStore

        if "operator" not in scopes and CITATION_READ not in scopes:
            return {"status": "unavailable", "provider": "platform.web-archives",
                    "reason": f"{CITATION_READ} is needed to list archived captures"}
        store = CitationPreservationStore(self.conn, initialize=False)
        captures = []
        for url in urls:
            for capture in store.captures_for_url(namespace, url, scopes=scopes | {CITATION_READ}):
                captures.append({"url": url, "capture_id": capture.get("capture_id"),
                                 "archive_id": capture.get("archive_id"), "uri_m": capture.get("uri_m"),
                                 "memento_datetime": capture.get("memento_datetime")})
        return {"status": "answered" if captures else "no_capture_on_record", "captures": captures}

    # ------------------------------------------------------------------ evidence

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the fact-check revision behind it (source, revision and as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []
        for item in answer.get("fact_checks") or []:
            publisher = (item.get("publisher") or {}).get("name_as_published") or \
                (item.get("publisher") or {}).get("site")
            for source in item["source_assertions"]:
                citation = source["citation"]
                bibliography.setdefault(citation["revision_id"], {
                    "id": citation["revision_id"],
                    "text": f"{publisher}, {item.get('review_title') or item['review_url']} ({citation['provider']} "
                            f"source {citation['source_id']}, record revision {citation['revision_no']}, observed "
                            f"{citation['observed_at']}, {citation['evidence_origin']} evidence), "
                            f"{citation['locator']}"})
                for claim in source["claims"]:
                    rating = claim.get("rating") or {}
                    scale = "" if rating.get("rating_value") is None else (
                        f" ({rating.get('rating_value')} on the publisher's scale {rating.get('worst_rating')}-"
                        f"{rating.get('best_rating')})")
                    assertions.append({
                        "id": f"{citation['revision_id']}:{claim['claim_key']}", "kind": "sourced",
                        "text": f"{publisher} rated the claim “{claim.get('claim_text_as_quoted')}”"
                                + (f" by {claim['claimant_as_named']}" if claim.get("claimant_as_named") else "")
                                + f" as “{rating.get('textual_rating')}”{scale}, as published on "
                                  f"{source.get('review_date') or 'an unpublished date'}.",
                        "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                          "id": item["record_key"], "revision": citation["revision_id"],
                                          "locator": {"section": claim["claim_key"]}}],
                        "citations": [citation["revision_id"]], "as_of": answer.get("as_of")})
        return {"sections": [{"id": answer.get("query", "answer"),
                              "title": f"Fact-checks for {answer.get('key')} as of {answer.get('as_of') or 'latest'}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS), "notice": NOTICE}

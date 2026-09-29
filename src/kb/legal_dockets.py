"""Court dockets, docket entries, opinions and parties on the Legal work model (#2218, CJ02, CJ03, CJ09).

A CourtListener docket or opinion cluster is projected onto the existing
:mod:`src.kb.legal` records rather than a parallel model:

* **Work** (``legal_works``) - a docket (``work_kind`` ``docket``) keyed by its
  CourtListener docket id with the court id, docket number and PACER case id as
  identifiers and the case name as published as its title; an opinion cluster
  (``work_kind`` ``decision``) keyed by its cluster id with its published
  reporter citations, docket id and opinion ids as identifiers.
* **Expression** (``legal_expressions``) - the docket record, or one per
  opinion of a cluster (lead, concurrence, dissent, per curiam; English).
* **Version** (``legal_versions``) - one per acquired revision (the provider's
  ``date_modified`` and content hash). Opinion text is split into paragraph
  passages (``legal_passages``) with ``opinion_id``/``paragraph`` locators; the
  decision date is a ``decision`` fact (``legal_facts``).

Docket-specific parts live beside them, keyed by the Legal version they
belong to: ``legal_docket_revisions`` (court, docket number, case name,
filing/termination dates, source revision, retrieval time),
``legal_docket_entries`` (number, date, description verbatim and linked
documents with availability - never mirrored), ``legal_docket_parties`` (the
CJ01 minimised party fields only: role(s) as published, the organisation /
natural-person flag, organisation names; natural persons as docket-scoped
pseudonyms) and ``legal_opinion_revisions`` / ``legal_opinions`` (citations,
the disposition quoted verbatim, author or per curiam, type, cited opinions).
An updated docket is a new revision; earlier revisions stay queryable.

Case outcomes exist only as quoted text with a locator; nothing derives a
win/loss label, predicts an outcome or gives legal advice.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.kb.courts_justice import (
    DOCKET_ANSWER_CONTRACT,
    READ_SCOPE,
    CourtsJusticeError,
    authorize,
    canonical,
    day,
    digest,
    load,
)

PROVIDER = "courtlistener"
JURISDICTION = "US"
NOTICE = ("Dockets, entries and decisions as the court record published them; dispositions are quoted text with a "
          "locator, never a win/loss label, prediction or legal advice.")
_DDL = """
CREATE TABLE IF NOT EXISTS legal_docket_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, version_id TEXT NOT NULL,
  record_key TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL, court_id TEXT NOT NULL,
  courtlistener_docket_id BIGINT NOT NULL, docket_number TEXT NOT NULL, case_name TEXT, date_filed DATE,
  date_terminated DATE, source_modified TEXT, content_sha256 TEXT NOT NULL, revision_no INTEGER NOT NULL,
  run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, evidence_origin TEXT, locator TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_docket_entries (
  revision_id TEXT NOT NULL, ordinal INTEGER NOT NULL, entry_id BIGINT, entry_number INTEGER, date_filed DATE,
  description TEXT, documents_json TEXT NOT NULL, PRIMARY KEY(revision_id, ordinal)
);
CREATE TABLE IF NOT EXISTS legal_docket_parties (
  revision_id TEXT NOT NULL, ordinal INTEGER NOT NULL, party_type TEXT NOT NULL, name_as_published TEXT,
  pseudonym TEXT, roles_json TEXT NOT NULL, party_key TEXT, PRIMARY KEY(revision_id, ordinal)
);
CREATE TABLE IF NOT EXISTS legal_opinion_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, record_key TEXT NOT NULL,
  source_id TEXT, provider TEXT NOT NULL, cluster_id BIGINT NOT NULL, docket_id BIGINT, court_id TEXT,
  case_name TEXT, date_filed DATE, disposition TEXT, precedential_status TEXT, citations_json TEXT NOT NULL,
  source_modified TEXT, content_sha256 TEXT NOT NULL, revision_no INTEGER NOT NULL, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, evidence_origin TEXT, locator TEXT NOT NULL, record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_opinions (
  revision_id TEXT NOT NULL, opinion_id BIGINT NOT NULL, version_id TEXT NOT NULL, opinion_type TEXT,
  author_str TEXT, per_curiam BOOLEAN, opinions_cited_json TEXT NOT NULL, text_sha256 TEXT,
  paragraph_count INTEGER NOT NULL, PRIMARY KEY(revision_id, opinion_id)
);
CREATE TABLE IF NOT EXISTS legal_court_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, unit_index)
);
"""


def _content(record: Mapping[str, Any]) -> str:
    return digest({k: v for k, v in record.items() if k != "evidence_origin"})


def docket_key(docket_id: Any) -> str:
    return f"courts:docket:courtlistener:{int(docket_id)}"


def cluster_key(cluster_id: Any) -> str:
    return f"courts:cluster:courtlistener:{int(cluster_id)}"


def court_key(court_id: Any) -> str:
    return f"courts:court:courtlistener:{court_id}"


def _as_of(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise CourtsJusticeError("invalid_request", "as_of must be YYYY-MM-DD") from exc


class LegalDocketStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        from src.kb.legal import LegalStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.legal = LegalStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ projection

    def _work(self, namespace: str, kind: str, native_id: str, identifiers: Mapping[str, Any], body: Any,
              title: Any, run_id: str) -> str:
        work_id = "legal-work:" + digest([namespace, PROVIDER, JURISDICTION, native_id])[:24]
        self.conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [work_id, namespace, PROVIDER, JURISDICTION, kind, kind, native_id, "{}", body, title,
                           run_id, self.now()])
        row = self.conn.execute("SELECT identifiers_json FROM legal_works WHERE work_id=?", [work_id]).fetchone()
        known = load(row[0], {})
        for key, value in identifiers.items():
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                known[key] = sorted({*known.get(key, []), *[str(v) for v in value]})
            else:
                known[key] = str(value)
        self.conn.execute("UPDATE legal_works SET identifiers_json=?, title=coalesce(?, title) WHERE work_id=?",
                          [canonical(known), title, work_id])
        return work_id

    def _version(self, namespace: str, work_id: str, native_expression: str, native_version: str,
                 text_sha: str | None, record: Mapping[str, Any], passages: Sequence[tuple[Mapping[str, Any], str]],
                 source_id: str | None, run_id: str, observed: int) -> str:
        expression_id = "legal-expression:" + digest([work_id, "en", native_expression])[:24]
        self.conn.execute("INSERT OR IGNORE INTO legal_expressions VALUES (?,?,?,?,?,?)",
                          [expression_id, namespace, work_id, "en", native_expression, record.get("title")])
        version_id = "legal-version:" + digest([expression_id, native_version, text_sha])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO legal_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING version_id",
            [version_id, namespace, work_id, expression_id, native_version, text_sha,
             canonical({k: v for k, v in record.items() if k != "fields"}), False,
             "captured-text" if passages else "metadata-only", len(passages), None, source_id, run_id,
             observed]).fetchall()
        if inserted:
            from src.kb.legal import _locator_key

            for ordinal, (locator, text) in enumerate(passages):
                self.conn.execute("INSERT INTO legal_passages VALUES (?,?,?,?,?)",
                                  [version_id, ordinal, _locator_key(locator) + "|" + canonical(locator),
                                   canonical(locator), text])
        return version_id

    def _fact(self, namespace: str, work_id: str, version_id: str, kind: str, value: str | None,
              observed: int) -> None:
        if not value:
            return
        fact_id = "legal-fact:" + digest([version_id, kind, value])[:24]
        self.conn.execute("INSERT OR IGNORE INTO legal_facts VALUES (?,?,?,?,?,?,?,?,?)",
                          [fact_id, namespace, work_id, version_id, kind, value, "source-reported", None, observed])

    def _next_revision(self, table: str, namespace: str, record_key: str, content: str) -> int | None:
        rows = self.conn.execute(f"SELECT content_sha256, revision_no FROM {table} WHERE namespace=? AND "
                                 "record_key=? ORDER BY revision_no", [namespace, record_key]).fetchall()
        if any(row[0] == content for row in rows):
            return None  # a replay or an unchanged re-read adds nothing
        return (rows[-1][1] + 1) if rows else 1

    def _docket(self, namespace, record, *, run_id, source_id, observed) -> bool:
        fields = dict(record["fields"])
        content = _content(record)
        revision_no = self._next_revision("legal_docket_revisions", namespace, record["record_key"], content)
        if revision_no is None:
            return False
        native_id = f"courtlistener:docket:{fields['courtlistener_docket_id']}"
        work_id = self._work(namespace, "docket", native_id, {
            "courtlistener_docket_id": fields["courtlistener_docket_id"], "court_id": fields["court_id"],
            "docket_number": fields["docket_number"], "pacer_case_id": fields.get("pacer_case_id")},
            fields["court_id"], fields.get("case_name"), run_id)
        version_id = self._version(namespace, work_id, native_id, str(fields.get("date_modified") or content),
                                   content, record, [], source_id, run_id, observed)
        self._fact(namespace, work_id, version_id, "document", fields.get("date_filed"), observed)
        revision_id = "legal-docket-revision:" + digest([namespace, record["record_key"], content])[:24]
        self.conn.execute(
            "INSERT INTO legal_docket_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [revision_id, namespace, work_id, version_id, record["record_key"], source_id, record["provider"],
             fields["court_id"], int(fields["courtlistener_docket_id"]), fields["docket_number"],
             fields.get("case_name"), fields.get("date_filed"), fields.get("date_terminated"),
             fields.get("date_modified"), content, revision_no, run_id, observed, record.get("evidence_origin"),
             record["locator"], canonical(record)])
        for ordinal, entry in enumerate(fields.get("entries") or []):
            self.conn.execute("INSERT INTO legal_docket_entries VALUES (?,?,?,?,?,?,?)",
                              [revision_id, ordinal, entry.get("entry_id"), entry.get("entry_number"),
                               entry.get("date_filed"), entry.get("description"),
                               canonical(entry.get("documents") or [])])
        for party in fields.get("parties") or []:
            if party["party_type"] == "natural_person" and (party.get("name_as_published") or party.get("party_key")):
                raise CourtsJusticeError("minimisation_violation", "a natural-person party carries a name or key")
            self.conn.execute("INSERT INTO legal_docket_parties VALUES (?,?,?,?,?,?,?)",
                              [revision_id, party["ordinal"], party["party_type"], party.get("name_as_published"),
                               party.get("pseudonym"), canonical(party.get("roles") or []), party.get("party_key")])
        return True

    def _cluster(self, namespace, record, *, run_id, source_id, observed) -> bool:
        fields = dict(record["fields"])
        content = _content(record)
        revision_no = self._next_revision("legal_opinion_revisions", namespace, record["record_key"], content)
        if revision_no is None:
            return False
        native_id = f"courtlistener:cluster:{fields['courtlistener_cluster_id']}"
        work_id = self._work(namespace, "decision", native_id, {
            "courtlistener_cluster_id": fields["courtlistener_cluster_id"],
            "courtlistener_docket_id": fields.get("courtlistener_docket_id"), "court_id": fields.get("court_id"),
            "citations": list(fields.get("citations") or []),
            "courtlistener_opinion_ids": [o["opinion_id"] for o in fields.get("opinions") or []]},
            fields.get("court_id"), fields.get("case_name"), run_id)
        revision_id = "legal-opinion-revision:" + digest([namespace, record["record_key"], content])[:24]
        self.conn.execute(
            "INSERT INTO legal_opinion_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [revision_id, namespace, work_id, record["record_key"], source_id, record["provider"],
             int(fields["courtlistener_cluster_id"]), fields.get("courtlistener_docket_id"), fields.get("court_id"),
             fields.get("case_name"), fields.get("date_filed"), fields.get("disposition"),
             fields.get("precedential_status"), canonical(fields.get("citations") or []),
             fields.get("date_modified"), content, revision_no, run_id, observed, record.get("evidence_origin"),
             record["locator"], canonical(record)])
        for opinion in fields.get("opinions") or []:
            passages = [({"opinion_id": opinion["opinion_id"], "paragraph": n + 1}, text)
                        for n, text in enumerate(opinion.get("paragraphs") or [])]
            version_id = self._version(namespace, work_id, f"courtlistener:opinion:{opinion['opinion_id']}",
                                       str(opinion.get("date_modified") or opinion.get("text_sha256")),
                                       opinion.get("text_sha256"), record, passages, source_id, run_id, observed)
            self._fact(namespace, work_id, version_id, "decision", fields.get("date_filed"), observed)
            self.conn.execute("INSERT OR IGNORE INTO legal_opinions VALUES (?,?,?,?,?,?,?,?,?)",
                              [revision_id, int(opinion["opinion_id"]), version_id, opinion.get("type"),
                               opinion.get("author_str"), bool(opinion.get("per_curiam")),
                               canonical(opinion.get("opinions_cited") or []), opinion.get("text_sha256"),
                               len(passages)])
        return True

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str, source_id: str | None,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, int]:
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = {"docket_revisions": 0, "opinion_revisions": 0, "unchanged": 0}
        self.conn.execute("BEGIN")
        try:
            for record in records:
                record = dict(record)
                if record["record_kind"] == "docket":
                    changed = self._docket(namespace, record, run_id=run_id, source_id=source_id, observed=observed)
                    counts["docket_revisions" if changed else "unchanged"] += 1
                elif record["record_kind"] == "opinion-cluster":
                    changed = self._cluster(namespace, record, run_id=run_id, source_id=source_id, observed=observed)
                    counts["opinion_revisions" if changed else "unchanged"] += 1
                else:
                    raise CourtsJusticeError("invalid_record", "not a docket or opinion-cluster record")
            if receipt and source_id:
                self.conn.execute("INSERT OR REPLACE INTO legal_court_receipts VALUES (?,?,?,?,?)",
                                  [namespace, run_id, source_id, int(receipt.get("unit_index") or 0),
                                   canonical(dict(receipt))])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    # ------------------------------------------------------------------ reads

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute("SELECT source_id, unit_index, receipt_json FROM legal_court_receipts WHERE "
                                 "namespace=? AND run_id=? ORDER BY source_id, unit_index",
                                 [namespace, run_id]).fetchall()
        return [{"source_id": r[0], "unit_index": r[1], **load(r[2], {})} for r in rows]

    @staticmethod
    def _cite_docket(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"record_key": row["record_key"], "revision_id": row["revision_id"],
                "revision_no": row["revision_no"], "work_id": row["work_id"], "version_id": row["version_id"],
                "source_id": row["source_id"], "provider": row["provider"],
                "source_revision": row["source_modified"], "retrieved_at_ms": row["observed_at_ms"],
                "evidence_origin": row["evidence_origin"], "locator": row["locator"]}

    _DOCKET_COLUMNS = ("revision_id", "namespace", "work_id", "version_id", "record_key", "source_id", "provider",
                       "court_id", "courtlistener_docket_id", "docket_number", "case_name", "date_filed",
                       "date_terminated", "source_modified", "content_sha256", "revision_no", "run_id",
                       "observed_at_ms", "evidence_origin", "locator")

    def docket_revisions(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._DOCKET_COLUMNS)} FROM legal_docket_revisions WHERE namespace=? AND "
            "record_key=? ORDER BY revision_no", [namespace, record_key]).fetchall()
        return [{**dict(zip(self._DOCKET_COLUMNS, r)), "date_filed": r[11] and str(r[11]),
                 "date_terminated": r[12] and str(r[12])} for r in rows]

    @staticmethod
    def _revision_day(row: Mapping[str, Any]) -> str:
        from datetime import datetime, timezone

        return day(row["source_modified"]) or datetime.fromtimestamp(row["observed_at_ms"] / 1000,
                                                                     tz=timezone.utc).date().isoformat()

    def docket_as_of(self, namespace: str, record_key: str, as_of: str | None) -> dict[str, Any] | None:
        """The docket revision current at ``as_of`` (the provider's modification date), entries filed by then."""
        revisions = self.docket_revisions(namespace, record_key)
        cutoff = _as_of(as_of)
        eligible = [r for r in revisions if cutoff is None or self._revision_day(r) <= cutoff]
        if not eligible:
            return None
        row = max(eligible, key=lambda r: (self._revision_day(r), r["revision_no"]))
        entries = [{"entry_number": e[0], "date_filed": e[1] and str(e[1]), "description": e[2],
                    "documents": load(e[3], []), "document_policy": "linked, not mirrored",
                    "quoted_from": {"revision_id": row["revision_id"], "locator": row["locator"],
                                    "entry_number": e[0]}}
                   for e in self.conn.execute(
                       "SELECT entry_number, date_filed, description, documents_json FROM legal_docket_entries "
                       "WHERE revision_id=? ORDER BY ordinal", [row["revision_id"]]).fetchall()
                   if cutoff is None or e[1] is None or str(e[1]) <= cutoff]
        parties = [{"party_type": p[0], "name_as_published": p[1], "pseudonym": p[2], "roles": load(p[3], []),
                    "party_key": p[4]}
                   for p in self.conn.execute("SELECT party_type, name_as_published, pseudonym, roles_json, "
                                              "party_key FROM legal_docket_parties WHERE revision_id=? ORDER BY "
                                              "ordinal", [row["revision_id"]]).fetchall()]
        terminated = row["date_terminated"] if row["date_terminated"] and (
            cutoff is None or row["date_terminated"] <= cutoff) else None
        return {"record_key": record_key, "court_id": row["court_id"], "docket_number": row["docket_number"],
                "case_name_as_published": row["case_name"], "date_filed": row["date_filed"],
                "date_terminated": terminated, "entries": entries, "parties": parties,
                "revision": self._cite_docket(row), "later_revisions": sum(
                    1 for r in revisions if r["revision_no"] > row["revision_no"]),
                "citation": self._cite_docket(row)}

    _CLUSTER_COLUMNS = ("revision_id", "work_id", "record_key", "source_id", "provider", "cluster_id", "docket_id",
                        "court_id", "case_name", "date_filed", "disposition", "precedential_status",
                        "citations_json", "source_modified", "revision_no", "observed_at_ms", "evidence_origin",
                        "locator")

    def cluster_revisions(self, namespace: str, *, record_key: str | None = None, docket_id: int | None = None,
                          court_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._CLUSTER_COLUMNS)} FROM legal_opinion_revisions WHERE namespace=? AND "
            "(? IS NULL OR record_key=?) AND (? IS NULL OR docket_id=?) AND (? IS NULL OR court_id=?) "
            "ORDER BY record_key, revision_no",
            [namespace, record_key, record_key, docket_id, docket_id, court_id, court_id]).fetchall()
        return [{**dict(zip(self._CLUSTER_COLUMNS, r)), "date_filed": r[9] and str(r[9]),
                 "citations": load(r[12], [])} for r in rows]

    def decision_as_of(self, namespace: str, record_key: str, as_of: str | None) -> dict[str, Any] | None:
        cutoff = _as_of(as_of)
        # A decision is on record from its filing date; the revision current then is the latest one the provider had
        # modified by that date, else the first acquired revision (a publication-day correction stands in).
        revisions = [r for r in self.cluster_revisions(namespace, record_key=record_key)
                     if cutoff is None or (r["date_filed"] and r["date_filed"] <= cutoff)]
        if not revisions:
            return None
        current = [r for r in revisions if cutoff is None or self._revision_day(r) <= cutoff]
        row = max(current, key=lambda r: r["revision_no"]) if current else min(revisions,
                                                                               key=lambda r: r["revision_no"])
        opinions = [{"opinion_id": o[0], "type": o[1], "author": o[2] or None, "per_curiam": bool(o[3]),
                     "opinions_cited": load(o[4], []), "version_id": o[5]}
                    for o in self.conn.execute("SELECT opinion_id, opinion_type, author_str, per_curiam, "
                                               "opinions_cited_json, version_id FROM legal_opinions WHERE "
                                               "revision_id=? ORDER BY opinion_id", [row["revision_id"]]).fetchall()]
        cite = {"record_key": record_key, "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                "work_id": row["work_id"], "source_id": row["source_id"], "provider": row["provider"],
                "source_revision": row["source_modified"], "retrieved_at_ms": row["observed_at_ms"],
                "evidence_origin": row["evidence_origin"], "locator": row["locator"]}
        return {"record_key": record_key, "cluster_id": row["cluster_id"], "docket_id": row["docket_id"],
                "court_id": row["court_id"], "case_name_as_published": row["case_name"],
                "date_filed": row["date_filed"], "citations_as_published": row["citations"],
                "precedential_status": row["precedential_status"],
                "disposition": {"quoted": row["disposition"], "locator": {"field": "disposition",
                                                                         "url": row["locator"],
                                                                         "revision_id": row["revision_id"]}}
                if row["disposition"] else {"quoted": None, "note": "the cluster states no disposition"},
                "opinions": opinions, "citation": cite}

    # ------------------------------------------------------------------ answers (CJ09)

    def _answer(self, namespace: str, query: Mapping[str, Any], as_of: str | None, dockets: list[dict[str, Any]],
                decisions: list[dict[str, Any]], connected_by: Mapping[str, Any]) -> dict[str, Any]:
        status = "answered" if dockets or decisions else "no_docket_on_record"
        return {"contract": DOCKET_ANSWER_CONTRACT, "namespace": namespace, "query": dict(query),
                "as_of": _as_of(as_of), "status": status, "dockets": dockets, "decisions": decisions,
                "connected_by": dict(connected_by),
                "coverage": "only the declared, acquired CourtListener selection is searched; absence here is not "
                            "absence of litigation",
                "notice": NOTICE}

    def dockets_for_court(self, namespace: str, court_id: str, *, scopes: Iterable[str],
                          as_of: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        keys = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT record_key FROM legal_docket_revisions WHERE namespace=? AND court_id=? ORDER BY 1",
            [namespace, court_id]).fetchall()]
        dockets = [d for d in (self.docket_as_of(namespace, k, as_of) for k in keys) if d]
        decisions = [d for d in (self.decision_as_of(namespace, k, as_of) for k in sorted({
            r["record_key"] for r in self.cluster_revisions(namespace, court_id=court_id)})) if d]
        return self._answer(namespace, {"court_id": court_id}, as_of, dockets, decisions,
                            {"basis": "court id as published (CourtListener court id)",
                             "court_key": court_key(court_id)})

    def dockets_for_party(self, namespace: str, entity_id: str, *, scopes: Iterable[str],
                          as_of: str | None = None) -> dict[str, Any]:
        """Dockets of an organisational party reached through an *accepted* identity match only."""
        from src.kb.courts_justice_identity import CourtsIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        text = str(entity_id or "").strip()
        if not text:
            raise CourtsJusticeError("invalid_request", "give the canonical entity id of an organisational party")
        if text.startswith("natural person") or re.fullmatch(r"[A-Z][a-z]+( [A-Z][a-z.]+)+", text):
            raise CourtsJusticeError("natural_person_not_a_query_key",
                                     "parties are queried by an accepted organisation identity, never by a person's "
                                     "name or pseudonym")
        links = CourtsIdentity(self.conn, initialize=False, now=self.now).accepted_parties(namespace, text,
                                                                                         scopes=scopes)
        by_docket: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            by_docket.setdefault(link["docket_key"], []).append(link)
        dockets = []
        for key in sorted(by_docket):
            answer = self.docket_as_of(namespace, key, as_of)
            if answer:
                dockets.append({**answer, "party_links": by_docket[key]})
        decisions = []
        for docket in dockets:
            docket_id = int(docket["record_key"].rsplit(":", 1)[1])
            for key in sorted({r["record_key"] for r in self.cluster_revisions(namespace, docket_id=docket_id)}):
                decision = self.decision_as_of(namespace, key, as_of)
                if decision:
                    decisions.append(decision)
        return self._answer(namespace, {"entity_id": text}, as_of, dockets, decisions,
                            {"basis": "accepted identity match (reviewable, reversible)",
                             "matches": [{"candidate_id": link["candidate_id"], "party_key": link["party_key"],
                                          "reviewer": link["reviewer"]} for link in links]})

    def dockets_for_provision(self, namespace: str, provision: str, *, scopes: Iterable[str],
                              as_of: str | None = None) -> dict[str, Any]:
        """Dockets and decisions whose entries or opinions cite a provision (exact citation links only)."""
        from src.kb.legal_court_citations import CourtCitations, parse_us_citations

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parsed = [c for c in parse_us_citations(provision) if c["kind"] in {"statute", "regulation"}]
        if len(parsed) != 1:
            raise CourtsJusticeError("invalid_request", "give one statutory or regulatory citation, e.g. "
                                                        "'42 U.S.C. § 1983'")
        target = parsed[0]
        links = CourtCitations(self.conn, initialize=False).links_to(namespace, target["key"])
        cutoff = _as_of(as_of)
        dockets: dict[str, dict[str, Any]] = {}
        decisions: dict[str, dict[str, Any]] = {}
        for link in links:
            if link["citing_kind"] == "docket-entry":
                answer = self.docket_as_of(namespace, link["citing_record_key"], as_of)
                if not answer:
                    continue
                current = answer["revision"]["revision_id"]
                if link["citing_revision_id"] != current:
                    continue  # the link is re-derived for every revision; the one current at as_of is cited
                entry = next((e for e in answer["entries"] if e["entry_number"] == link["locator"].get(
                    "entry_number")), None)
                if entry is None:
                    continue
                item = dockets.setdefault(answer["record_key"], {**answer, "citing_entries": []})
                item["citing_entries"].append({"entry_number": entry["entry_number"],
                                               "date_filed": entry["date_filed"], "quoted": link["raw"],
                                               "locator": link["locator"], "link_id": link["link_id"]})
            else:
                decision = self.decision_as_of(namespace, link["citing_record_key"], as_of)
                if not decision or decision["citation"]["revision_id"] != link["citing_revision_id"]:
                    continue
                item = decisions.setdefault(decision["record_key"], {**decision, "citing_passages": []})
                item["citing_passages"].append({"quoted": link["raw"], "locator": link["locator"],
                                                "link_id": link["link_id"]})
        del cutoff
        return self._answer(namespace, {"provision": target["normalized"], "provision_key": target["key"]}, as_of,
                            [dockets[k] for k in sorted(dockets)], [decisions[k] for k in sorted(decisions)],
                            {"basis": "exact citation parse of the entry description or opinion text",
                             "target_work_ids": sorted({link["target_work_id"] for link in links
                                                        if link["target_work_id"]})})

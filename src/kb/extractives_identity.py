"""Companies, commodities and projects matched through reviewable identity (#2653, EX06).

Nothing is merged and nothing is accepted automatically: every match carries its method, evidence and confidence
and is ``proposed`` until a reviewer accepts or rejects it; an accepted or rejected match can be ``reverted``.
Published identifiers are used before names, and a subject without an accepted match stays visible as
``unmatched``.

* **Companies** (EITI company records, current revision) are offered against the legal entities of an ownership
  namespace through the shared reviewable state machine
  (:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted and reverted decisions are
  :class:`src.kb.entity_history.EntityHistoryStore` decisions): ``exact-identifier`` when the report publishes an
  identifier an ownership record carries (with the declared scheme equivalences, e.g. a KvK number is the GLEIF
  ``RA000463`` registration number and a Companies House number the ``RA000585`` one), else ``name-jurisdiction``
  on equal normalized names - **low evidence**, never auto-accepted. Payers the report marks as individuals are
  never offered (their names are withheld). Candidate keys start with ``extractives:`` (in the ownership
  ``FOREIGN_KEY_PREFIXES``), so these links never regroup ownership entities. Without ownership records every
  company is reported unmatched with ``ownership_absent``.
* **Commodities** map to HS headings only through a published concordance recorded with its citation
  (:meth:`ExtractivesIdentity.import_concordance`), method ``published-concordance``; no HS code is guessed from a
  commodity name.
* **Projects** map to infrastructure assets (:class:`src.kb.infrastructure_assets.InfrastructureStore`) only through
  a published identifier both sides state (``shared-identifier``) or equal published coordinates
  (``published-coordinates``, lower confidence); never through a name. Without infrastructure records the proposal
  reports ``provider_absent``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.extractives_records import (
    MATCH_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    load,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
SCHEME_ALIASES = {"nl-kvk": {"nl-kvk", "ra:RA000463"}, "gb-coh": {"gb-coh", "ra:RA000585"}, "lei": {"lei"}}
CONFIDENCE = {"published-concordance": 0.9, "shared-identifier": 0.9, "published-coordinates": 0.6}
STATES = ("proposed", "accepted", "rejected", "reverted")
COORDINATE_TOLERANCE = 0.0001
_DDL = """
CREATE TABLE IF NOT EXISTS extractives_concordances (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, label TEXT NOT NULL, publisher TEXT NOT NULL,
  citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, concordance_id)
);
CREATE TABLE IF NOT EXISTS extractives_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_revision_id TEXT, target_kind TEXT NOT NULL, target_namespace TEXT, target_id TEXT NOT NULL,
  target_revision_id TEXT, method TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, history_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
"""
NOTICE = "a reviewable identity decision; extractives records are never merged or rewritten"


def _scheme_set(scheme: str) -> set[str]:
    for group in SCHEME_ALIASES.values():
        if scheme in group:
            return group
    return {scheme}


def _value(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def _float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


class ExtractivesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ExtractivesStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    # ============================================================== companies

    def company_subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Current company records as reported (individual payers excluded: their names are withheld)."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for view in self.store.records(namespace, record_types=("company",)):
            body = view["record"]
            if body.get("natural_person"):
                continue
            out.append({"key": body["record_key"], "name_as_published": body["name_as_published"],
                        "identifiers": body.get("identifiers") or [], "country": body.get("country"),
                        "report_key": view["report_key"], "revision_id": view["revision_id"]})
        return sorted(out, key=lambda s: s["key"])

    def propose_companies(self, namespace: str, *, ownership_namespace: str, principal_id: str,
                          scopes: Iterable[str]) -> dict[str, Any]:
        """Identifier candidates first, name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        entities = []
        if table_exists(self.conn, "ownership_records"):
            authorize(ownership_namespace, scopes, "knowledge:ownership:read")
            entities = self.service._entities(ownership_namespace, principal_id, scopes)
        if not entities:
            return {"status": "ownership_absent", "proposed": [], "candidates": [],
                    "unmatched": self.unmatched_companies(namespace, scopes=scopes),
                    "note": "no ownership records are held; every company stays as reported, unmatched"}
        offered = []
        for subject in self.company_subjects(namespace, scopes=scopes):
            left = {"record_key": subject["key"], "name_as_published": subject["name_as_published"],
                    "revision_id": subject["revision_id"], "ownership_namespace": ownership_namespace}
            published = {(scheme, _value(i["value"])) for i in subject["identifiers"]
                         for scheme in _scheme_set(i["scheme"])}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"]}
                right_entity = body.get("canonical_entity_id") or entity_for(body["record_key"])
                shared = [i for i in body.get("identifiers") or [] if (i["scheme"], _value(i["value"])) in published]
                if shared:
                    offered.append(self._offer(namespace, subject, body["record_key"], right_entity,
                                               "exact-identifier", {"identifiers": shared, "left": left,
                                                                    "right": right}, principal_id, scopes))
                    continue
                if _name(body.get("name")) != _name(subject["name_as_published"]):
                    continue
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction", {
                    "normalized_name": _name(body.get("name")), "left": left, "right": right, "low_evidence": True,
                    "country_basis": "the EITI country is the implementing country, not the company's jurisdiction",
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"status": "proposed",
                "proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.company_candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched_companies(namespace, scopes=scopes)}

    def _offer(self, namespace, subject, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=subject["key"], right_key=right_key,
                                  left_entity=entity_for(subject["key"]), right_entity=right_entity, basis=basis,
                                  evidence=[{**evidence, "method": basis}], principal_id=principal_id, scopes=scopes)

    @staticmethod
    def company_view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        left, right = candidate["left_key"], candidate["right_key"]
        subject, other = (left, right) if left.startswith("extractives:") else (right, left)
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        return {"contract": MATCH_CONTRACT, "kind": "company", "candidate_id": candidate["candidate_id"],
                "state": candidate["state"], "method": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "subject_key": subject, "ownership_key": other,
                "ownership_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": NOTICE}

    def company_candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                           ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=subject_key)
                if c["left_key"].startswith("extractives:") or c["right_key"].startswith("extractives:")]
        return [self.company_view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.company_candidates(namespace, scopes=scopes)):
            raise ExtractivesError("not_found", "no extractives company candidate with that id")

    def review_company(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, scopes)
        try:
            return self.company_view(self.service.review(namespace, candidate_id, decision, reason,
                                                         principal_id=principal_id, scopes=scopes))
        except OwnershipError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc

    def revert_company(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, scopes)
        try:
            return self.company_view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                         scopes=scopes))
        except OwnershipError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc

    def unmatched_companies(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        views = self.company_candidates(namespace, scopes=scopes)
        out = []
        for subject in self.company_subjects(namespace, scopes=scopes):
            mine = [v for v in views if v["subject_key"] == subject["key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "name_as_published": subject["name_as_published"],
                        "report_key": subject["report_key"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def accepted_company_links(self, namespace: str, ownership_keys: Iterable[str], *, scopes: Iterable[str]
                               ) -> list[dict[str, Any]]:
        """Accepted, unreverted company matches whose ownership side is one of the given record keys or entities."""
        wanted = set(ownership_keys)
        return [{"candidate_id": v["candidate_id"], "subject_key": v["subject_key"],
                 "ownership_key": v["ownership_key"], "method": v["method"], "low_evidence": v["low_evidence"],
                 "confidence": v["confidence"], "reviewer": v["reviewer"], "reviewed_at_ms": v["reviewed_at_ms"],
                 "decision_id": v["decision_id"]}
                for v in self.company_candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and ({v["ownership_key"], v["ownership_entity"]} & wanted)]

    # ============================================================== commodity and project matches

    def import_concordance(self, namespace: str, table: Mapping[str, Any], *, principal_id: str,
                           scopes: Iterable[str]) -> dict[str, Any]:
        """Record a published commodity-to-HS correspondence with its citation; idempotent by content."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        table = dict(table)
        citation = dict(table.get("citation") or {})
        if not str(citation.get("url") or "").startswith("https://") or not citation.get("published_on"):
            raise ExtractivesError("invalid_concordance", "a concordance cites its https URL and publication date")
        if not table.get("label") or not table.get("publisher"):
            raise ExtractivesError("invalid_concordance", "a concordance names its label and publisher")
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("commodity") or not str(row.get("hs_code") or "").isdigit() or not row.get("hs_edition"):
                raise ExtractivesError("invalid_concordance", "each row names a commodity, an HS code and edition")
            if row.get("mapping_type") not in {"1:1", "1:n", "n:1", "n:n"}:
                raise ExtractivesError("invalid_concordance", "each row states its relationship as published")
            rows.append({k: row.get(k) for k in ("commodity", "hs_code", "hs_edition", "label", "mapping_type",
                                                  "note")})
        if not rows:
            raise ExtractivesError("invalid_concordance", "a concordance states at least one row")
        concordance_id = "extractives-concordance:" + digest([namespace, table["label"], citation, rows])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO extractives_concordances VALUES (?,?,?,?,?,?,?,?) RETURNING concordance_id",
            [namespace, concordance_id, table["label"], table["publisher"], canonical(citation), canonical(rows),
             principal_id, self.now()]).fetchall()
        return {"concordance_id": concordance_id, "created": bool(inserted), "rows": len(rows)}

    def concordances(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "extractives_concordances"):
            return []
        rows = self.conn.execute(
            "SELECT concordance_id, label, publisher, citation_json, rows_json, recorded_by FROM "
            "extractives_concordances WHERE namespace=? ORDER BY created_at_ms, concordance_id", [namespace]).fetchall()
        return [{"concordance_id": r[0], "label": r[1], "publisher": r[2], "citation": load(r[3], {}),
                 "rows": load(r[4], []), "recorded_by": r[5]} for r in rows]

    def _match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, kind, subject_key, subject_revision_id, target_kind, target_namespace, target_id, "
            "target_revision_id, method, confidence, evidence_json, state, history_json, created_at_ms, "
            "updated_at_ms FROM extractives_matches WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "no extractives match with that id")
        history = load(row[12], [])
        last = history[-1]
        return {"contract": MATCH_CONTRACT, "match_id": row[0], "kind": row[1], "subject_key": row[2],
                "subject_revision_id": row[3], "target": {"kind": row[4], "namespace": row[5], "id": row[6],
                                                          "revision_id": row[7]},
                "method": row[8], "confidence": row[9], "evidence": load(row[10], {}), "state": row[11],
                "reviewer": last.get("by") if row[11] != "proposed" else None,
                "reviewed_at": iso_from_ms(last.get("at_ms")) if row[11] != "proposed" else None,
                "reason": last.get("reason"), "history": history, "created_at": iso_from_ms(row[13]),
                "notice": NOTICE}

    def _propose(self, namespace, kind, subject_key, subject_revision_id, target_kind, target_namespace, target_id,
                 target_revision_id, method, evidence, principal_id) -> tuple[str, str | None]:
        match_id = "extractives-match:" + digest([namespace, kind, subject_key, target_kind, target_id])[:24]
        row = self.conn.execute("SELECT state, evidence_json, history_json FROM extractives_matches WHERE "
                                "namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        stamp = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO extractives_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, match_id, kind, subject_key, subject_revision_id, target_kind, target_namespace,
                 target_id, target_revision_id, method, CONFIDENCE[method], canonical(evidence), "proposed",
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": stamp}]), stamp, stamp])
            return match_id, "created"
        state, old, history = row[0], load(row[1], {}), load(row[2], [])
        if state in {"rejected", "reverted"} and digest(old) != digest(evidence):
            history.append({"state": "proposed", "by": principal_id, "at_ms": stamp, "change": "reproposed",
                            "previous_state": state})
            self.conn.execute("UPDATE extractives_matches SET state='proposed', evidence_json=?, history_json=?, "
                              "updated_at_ms=? WHERE namespace=? AND match_id=?",
                              [canonical(evidence), canonical(history), stamp, namespace, match_id])
            return match_id, "reproposed"
        return match_id, None

    def propose_commodities(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Commodity codes of the held series to HS headings through recorded published concordances only."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        codes = sorted({s["commodity"]["code"] for s in self.store.find_series(namespace)})
        proposed, unmatched = [], []
        tables = self.concordances(namespace)
        for code in codes:
            found = False
            for table in tables:
                for index, row in enumerate(table["rows"]):
                    if row["commodity"] != code:
                        continue
                    found = True
                    target = f"hs:{row['hs_edition']}:{row['hs_code']}"
                    match_id, change = self._propose(
                        namespace, "commodity", f"commodity:{code}", None, "hs-code", None, target, None,
                        "published-concordance", {"concordance_id": table["concordance_id"], "row": index,
                                                  "row_as_published": row, "citation": table["citation"],
                                                  "publisher": table["publisher"]}, principal_id)
                    if change:
                        proposed.append(match_id)
            if not found:
                unmatched.append({"subject_key": f"commodity:{code}", "status": "unmatched",
                                  "note": "no recorded published concordance names this commodity; no HS code is "
                                          "guessed"})
        return {"proposed": proposed, "unmatched": unmatched,
                "matches": self.matches(namespace, kind="commodity")}

    def propose_projects(self, namespace: str, *, infrastructure_namespace: str, principal_id: str,
                         scopes: Iterable[str]) -> dict[str, Any]:
        """Projects to infrastructure assets by a shared published identifier or equal published coordinates."""
        from src.kb.infrastructure_assets import InfrastructureStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        projects = self.store.records(namespace, record_types=("project",))
        if not table_exists(self.conn, "infra_assets"):
            return {"status": "provider_absent", "proposed": [],
                    "unmatched": [{"subject_key": p["record_key"], "status": "unmatched"} for p in projects],
                    "note": "no infrastructure records are held; projects stay as reported"}
        infra = InfrastructureStore(self.conn, initialize=False)
        assets = []
        for asset in infra.assets(infrastructure_namespace, scopes=scopes):
            revisions = infra.revisions(infrastructure_namespace, asset["asset_id"], scopes=scopes)
            if revisions:
                assets.append((asset, revisions[-1]))
        proposed, unmatched = [], []
        for view in projects:
            body = view["record"]
            ours = {(i["scheme"], i["value"]) for i in body.get("identifiers") or []}
            coords = dict(body.get("coordinates") or {})
            lat, lon = _float(coords.get("lat")), _float(coords.get("lon"))
            found = False
            for asset, revision in assets:
                record = revision["record"]
                shared = [i for i in record.get("identifiers") or [] if (i["scheme"], i["value"]) in ours]
                method, evidence = None, None
                if shared:
                    method, evidence = "shared-identifier", {"identifiers": shared}
                elif lat is not None and lon is not None and (record.get("geometry") or {}).get("type") == "Point":
                    alon, alat = (record["geometry"].get("coordinates") or [None, None])[:2]
                    if alat is not None and abs(float(alat) - lat) <= COORDINATE_TOLERANCE and \
                            abs(float(alon) - lon) <= COORDINATE_TOLERANCE:
                        method, evidence = "published-coordinates", {"project": coords,
                                                                     "asset": record["geometry"]["coordinates"],
                                                                     "tolerance_deg": COORDINATE_TOLERANCE}
                if method is None:
                    continue
                found = True
                match_id, change = self._propose(
                    namespace, "project", body["record_key"], view["revision_id"], "infrastructure-asset",
                    infrastructure_namespace, asset["asset_id"], revision["revision_id"], method,
                    {**evidence, "project_name_as_published": body.get("name_as_published"),
                     "asset_name": record.get("name"), "asset_provider": asset["provider"]}, principal_id)
                if change:
                    proposed.append(match_id)
            if not found:
                unmatched.append({"subject_key": body["record_key"], "status": "unmatched",
                                  "note": "no published identifier or coordinates shared with an asset; names are "
                                          "never matched"})
        return {"status": "proposed", "proposed": proposed, "unmatched": unmatched,
                "matches": self.matches(namespace, kind="project")}

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise ExtractivesError("invalid_decision", "accept or reject with a reason")
        match = self._match(namespace, match_id)
        if match["state"] != "proposed":
            raise ExtractivesError("invalid_transition", f"a {match['state']} match cannot be reviewed")
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected", reason,
                                principal_id)

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise ExtractivesError("invalid_decision", "a revert needs a reason")
        match = self._match(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise ExtractivesError("invalid_transition", "only an accepted or rejected match can be reverted")
        return self._transition(namespace, match, "reverted", reason, principal_id)

    def _transition(self, namespace, match, state, reason, principal_id):
        history = list(match["history"]) + [{"state": state, "by": principal_id, "at_ms": self.now(),
                                             "reason": reason, "previous_state": match["state"]}]
        self.conn.execute("UPDATE extractives_matches SET state=?, history_json=?, updated_at_ms=? WHERE namespace=? "
                          "AND match_id=?", [state, canonical(history), self.now(), namespace, match["match_id"]])
        return self._match(namespace, match["match_id"])

    def matches(self, namespace: str, *, kind: str | None = None, subject_key: str | None = None,
                state: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "extractives_matches"):
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM extractives_matches WHERE namespace=? AND (? IS NULL OR kind=?) AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR state=?) ORDER BY kind, subject_key, target_id",
            [namespace, kind, kind, subject_key, subject_key, state, state]).fetchall()
        return [self._match(namespace, r[0]) for r in rows]

    def accepted_hs_codes(self, namespace: str, commodity: str) -> list[dict[str, Any]]:
        return [{"hs_edition": m["target"]["id"].split(":")[1], "hs_code": m["target"]["id"].split(":")[2],
                 "match_id": m["match_id"], "method": m["method"], "reviewer": m["reviewer"]}
                for m in self.matches(namespace, kind="commodity", subject_key=f"commodity:{commodity}",
                                      state="accepted")]


def read_identity(conn: Any, namespace: str, scopes) -> ExtractivesIdentity:
    authorize(namespace, scopes, READ_SCOPE)
    return ExtractivesIdentity(conn, initialize=False)


__all__ = ["CONFIDENCE", "NAME_BASES", "SCHEME_ALIASES", "ExtractivesIdentity", "read_identity"]

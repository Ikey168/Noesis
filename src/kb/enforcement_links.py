"""Links from enforcement records to other packs by citation, shared identifier or accepted match (#2651, EN08).

Every link records its **basis** and points at a specific **revision** on
both sides where the target store keeps revisions:

* ``citation`` - legal bases and related references parsed exactly from the
  text the regulator published (:func:`parse_references`): the US Code (the
  courts feature's parser) and the named US Acts at their fixed codification
  (Securities Act, Securities Exchange Act, Advisers Act, Investment Company
  Act, Clean Air Act, EPCRA, Clean Water Act, RCRA, ...), UK Acts
  (Financial Services and Markets Act 2000), GDPR articles cited in the
  Article 60 register, FCA Handbook provisions and Principles (kept as
  unresolved references - no provider holds the Handbook), Commission
  competition case numbers (the Corporate Ownership competition feature) and
  court docket numbers of related court cases and appeals (the Legal courts
  feature);
* ``shared_identifier`` - a CIK the regulator published for a respondent,
  equal to a Market issuer alias (the Market filings of that issuer are
  listed by accession);
* ``accepted_match`` - an accepted EN07 identity decision from a respondent
  to an ownership entity (never a proposed one).

A target that no acquired record carries stays ``unresolved`` with its source
text; a provider that is not installed makes the link
``provider_unavailable``; nothing is dropped. No causal relation between an
action and a market event is inferred and the citing relationship is never
characterised.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from typing import Any

from src.kb.enforcement import (
    READ_SCOPE,
    WRITE_SCOPE,
    EnforcementStore,
    authorize,
    table_exists,
)
from src.kb.enforcement_records import canonical, digest

LINK_CONTRACT = "noesis-enforcement-link-v1"
BASES = ("citation", "shared_identifier", "accepted_match", "published_coordinates")
# Named Acts at their fixed codification (title, first section); a named section of one is an exact reference.
US_ACTS = {
    "securities act": ("15", "77a"), "securities exchange act": ("15", "78a"), "exchange act": ("15", "78a"),
    "investment advisers act": ("15", "80b-1"), "advisers act": ("15", "80b-1"),
    "investment company act": ("15", "80a-1"),
    "caa": ("42", "7401"), "clean air act": ("42", "7401"), "cwa": ("33", "1251"), "clean water act": ("33", "1251"),
    "rcra": ("42", "6901"), "epcra": ("42", "11001"), "cercla": ("42", "9601"), "sdwa": ("42", "300f"),
    "tsca": ("15", "2601"), "fifra": ("7", "136"),
}
UK_ACTS = {"Financial Services and Markets Act 2000": "ukpga/2000/8",
           "Money Laundering Regulations 2017": "uksi/2017/692"}
GDPR_CELEX = "32016R0679"
_US_ACT = re.compile(r"(Securities Exchange Act|Securities Act|Exchange Act|Investment Advisers Act|Advisers Act|"
                     r"Investment Company Act|Clean Air Act|Clean Water Act)", re.IGNORECASE)
_ECHO_LAW = re.compile(r"^(CAA|CWA|RCRA|EPCRA|CERCLA|SDWA|TSCA|FIFRA)\b")
_GDPR = re.compile(r"^Article\s+(\d{1,2})\b")
_HANDBOOK = re.compile(r"^(Principle\s+\d{1,2}|(?:SYSC|COBS|MAR|PRIN|DEPP|SUP|CONC|ICOBS|MCOB|CASS|DISP|APER|COCON|FIT|"
                       r"GEN|MLR)\s+\d+(?:\.\d+)*[RGED]?)$")


def _uk_forms(ident: str) -> list[str]:
    _kind, year, number = ident.split("/")
    return [ident, f"http://www.legislation.gov.uk/{ident}", f"https://www.legislation.gov.uk/{ident}",
            f"{year} c. {number}"]


def parse_references(text: Any, *, authority: str | None = None) -> list[dict[str, Any]]:
    """Exact statute, regulation, rule and case references in one published legal basis or reference text."""
    from src.kb.competition_citations import parse_references as competition_references
    from src.kb.legal_court_citations import parse_us_citations

    text = str(text or "").strip()
    found: list[dict[str, Any]] = []
    for item in parse_us_citations(text):
        if item["kind"] == "statute":
            found.append({"kind": "statute", "raw": item["raw"], "key": item["key"],
                          "forms": [item["key"], f"usc:{item['title']}"], "provision": item["provision"]})
    if not found:
        act = _US_ACT.search(text) or _ECHO_LAW.search(text)
        if act:
            title, section = US_ACTS[act.group(1).lower()]
            found.append({"kind": "statute", "raw": text, "key": f"usc:{title}:{section}",
                          "forms": [f"usc:{title}:{section}", f"usc:{title}"],
                          "provision": f"{act.group(1)} as codified at {title} U.S.C. § {section} et seq."})
    for name, ident in UK_ACTS.items():
        if name in text:
            found.append({"kind": "statute", "raw": name, "key": f"uk:{ident}", "forms": _uk_forms(ident),
                          "provision": name})
    if authority and authority.startswith("eu-sa-"):
        match = _GDPR.match(text)
        if match:
            found.append({"kind": "regulation_article", "raw": text, "key": f"celex:{GDPR_CELEX}",
                          "forms": [GDPR_CELEX, f"CELEX:{GDPR_CELEX}"],
                          "provision": f"Article {match.group(1)} GDPR"})
    if _HANDBOOK.match(text):
        found.append({"kind": "handbook_provision", "raw": text, "key": f"fca-handbook:{text}", "forms": [],
                      "provision": text})
    for item in competition_references(text):
        if item["kind"] == "case":
            found.append({"kind": "competition_case", "raw": item["raw"], "key": item["key"], "forms": item["forms"],
                          "authority": item.get("authority"), "case_number": item.get("case_number")})
    unique: dict[str, dict[str, Any]] = {}
    for item in found:
        unique.setdefault(item["key"], item)
    return list(unique.values())


def basis_keys(action: dict[str, Any]) -> set[str]:
    """The exact reference keys of an action's published legal bases (used by the authority/basis queries)."""
    return {c["key"] for text in action.get("legal_bases") or [] for c in parse_references(
        text, authority=action.get("authority"))}


_DDL = """
CREATE TABLE IF NOT EXISTS enforcement_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citing_record_key TEXT NOT NULL, citing_kind TEXT NOT NULL,
  citing_revision_id TEXT NOT NULL, field TEXT NOT NULL, raw TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_key TEXT NOT NULL, target_revision_id TEXT, status TEXT NOT NULL, basis TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
"""
_COLUMNS = ("link_id", "citing_record_key", "citing_kind", "citing_revision_id", "field", "raw", "target_kind",
            "target_key", "target_revision_id", "status", "basis", "evidence_json")


class EnforcementLinks:
    """Link actions and respondents to Legal works, competition cases, court dockets, Market issuers and entities."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- resolution

    def _legal(self, legal_namespace: str, citation: dict[str, Any], scopes) -> dict[str, Any]:
        if not citation["forms"]:
            return {"status": "unresolved", "basis_note": "no Legal provider holds this rulebook; kept as source text"}
        if not table_exists(self.conn, "legal_works"):
            return {"status": "provider_unavailable", "basis_note": "no Legal store in this deployment"}
        from src.kb.legal import LegalError, LegalStore

        legal = LegalStore(self.conn, initialize=False)
        legal_scopes = set(scopes) | {"knowledge:legal:read", f"namespace:{legal_namespace}:read"}
        tried = []
        for form in citation["forms"]:
            try:
                answer = legal.lookup(legal_namespace, scopes=legal_scopes, identifier=form)
            except LegalError:
                continue
            tried.append({"form": form, "status": answer["status"]})
            works = sorted({w["work_id"] for w in answer["works"]})
            if len(works) == 1:
                return {"status": "resolved", "target_key": works[0], "basis_note": f"exact identifier {form}",
                        "tried": tried}
            if works:
                return {"status": "ambiguous", "basis_note": f"several Legal works carry {form}", "works": works,
                        "tried": tried}
        return {"status": "unresolved", "basis_note": "no acquired Legal work carries this reference", "tried": tried}

    def _competition(self, competition_namespace: str, citation: dict[str, Any]) -> dict[str, Any]:
        if citation.get("authority") != "ec":
            return {"status": "unresolved", "basis_note": "exact case reference of an authority not acquired"}
        if not table_exists(self.conn, "ownership_records"):
            return {"status": "provider_unavailable", "basis_note": "the competition feature's store is absent"}
        key = f"competition:case:ec:{citation['case_number']}"
        row = self.conn.execute(
            "SELECT v.revision_id FROM ownership_records r JOIN ownership_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id AND v.revision=r.current_revision WHERE "
            "r.namespace=? AND r.record_key=?", [competition_namespace, key]).fetchone()
        if row:
            return {"status": "resolved", "target_key": key, "target_revision_id": row[0],
                    "basis_note": "exact case number"}
        return {"status": "unresolved", "basis_note": "the cited case is not acquired"}

    def _docket(self, docket: dict[str, Any]) -> dict[str, Any]:
        if not table_exists(self.conn, "legal_docket_revisions"):
            return {"status": "provider_unavailable", "basis_note": "the Legal courts feature's store is absent"}
        rows = self.conn.execute(
            "SELECT record_key, revision_id, court_id FROM legal_docket_revisions WHERE docket_number=? "
            "ORDER BY revision_no DESC", [docket["docket_number"]]).fetchall()
        keys = sorted({r[0] for r in rows})
        if len(keys) == 1:
            return {"status": "resolved", "target_key": keys[0], "target_revision_id": rows[0][1],
                    "basis_note": "exact docket number", "court_id": rows[0][2]}
        if keys:
            return {"status": "ambiguous", "basis_note": "several dockets carry this number", "dockets": keys}
        return {"status": "unresolved", "basis_note": "no acquired docket carries this number"}

    def _market(self, cik: str) -> dict[str, Any]:
        if not table_exists(self.conn, "market_instrument_alias_assertions"):
            return {"status": "provider_unavailable", "basis_note": "the Market instruments store is absent"}
        normalized = re.sub(r"\D", "", cik).zfill(10)
        rows = self.conn.execute(
            "SELECT DISTINCT object_id, revision_id FROM market_instrument_alias_assertions WHERE scheme='cik' AND "
            "normalized_value=? AND object_type='issuer' ORDER BY revision_id", [normalized]).fetchall()
        issuers = sorted({r[0] for r in rows})
        if len(issuers) != 1:
            return {"status": "ambiguous" if issuers else "unresolved",
                    "basis_note": "several Market issuers carry this CIK" if issuers else
                    "no Market issuer carries this CIK", "issuers": issuers}
        filings = []
        if table_exists(self.conn, "market_financial_fact_revisions"):
            filings = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT filing_accession FROM market_financial_fact_revisions WHERE issuer_id=? "
                "ORDER BY 1", [issuers[0]]).fetchall()]
        return {"status": "resolved", "target_key": issuers[0], "target_revision_id": rows[-1][1],
                "basis_note": "the CIK the regulator published equals the Market issuer alias",
                "filings": filings}

    # ------------------------------------------------------------------ link

    def _insert(self, namespace: str, view: dict[str, Any], field: str, raw: str, target_kind: str, key: str,
                basis: str, resolved: dict[str, Any]) -> int:
        body = view["record"]
        link_id = "enforcement-link:" + digest([namespace, view["revision_id"], field, target_kind, key])[:24]
        if self.conn.execute("SELECT 1 FROM enforcement_links WHERE link_id=?", [link_id]).fetchone():
            return 0
        evidence = {k: v for k, v in resolved.items() if k not in {"status", "target_key", "target_revision_id"}}
        self.conn.execute("INSERT INTO enforcement_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [link_id, namespace, body["record_key"], body["kind"], view["revision_id"], field, raw,
                           target_kind, resolved.get("target_key") or key, resolved.get("target_revision_id"),
                           resolved["status"], basis, canonical(evidence), self.now()])
        return 1

    def link(self, namespace: str, *, legal_namespace: str = "global", competition_namespace: str = "competition",
             ownership_namespace: str | None = None, scopes: Iterable[str]) -> dict[str, Any]:
        """Parse every current action and respondent; idempotent per citing revision; re-resolves open links."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        store = EnforcementStore(self.conn, initialize=False)
        created = 0
        for view in store.views(namespace, ("enforcement_action",)):
            body = view["record"]
            for field in ("legal_bases", "related_references"):
                for text in body.get(field) or []:
                    for citation in parse_references(text, authority=body["authority"]):
                        if citation["kind"] == "competition_case":
                            resolved = self._competition(competition_namespace, citation)
                            target_kind = "competition_case"
                        else:
                            resolved = self._legal(legal_namespace, citation, scopes)
                            target_kind = "legal_work"
                        resolved["provision"] = citation.get("provision")
                        created += self._insert(namespace, view, field, text, target_kind, citation["key"],
                                                "citation", resolved)
            for docket in body.get("court_cases") or []:
                created += self._insert(namespace, view, "court_cases", docket["docket_number"], "court_docket",
                                        f"docket:{docket['docket_number']}", "citation",
                                        {**self._docket(docket), "court_as_published": docket.get("court")})
            for facility in body.get("facilities") or []:
                if facility.get("frs_registry_id"):
                    created += self._insert(
                        namespace, view, "facilities", facility["frs_registry_id"], "facility",
                        f"frs:{facility['frs_registry_id']}", "shared_identifier",
                        {"status": "unresolved", "basis_note": "FRS registry id as published; places are reached "
                                                               "only through it or the published coordinates",
                         "latitude_as_published": facility.get("latitude_as_published"),
                         "longitude_as_published": facility.get("longitude_as_published")})
        for view in store.views(namespace, ("appeal",)):
            if view["record"].get("reference"):
                created += self._insert(namespace, view, "reference", view["record"]["reference"], "court_docket",
                                        f"docket:{view['record']['reference']}", "citation",
                                        self._docket({"docket_number": view["record"]["reference"]}))
        for view in store.views(namespace, ("respondent",)):
            for identifier in view["record"].get("identifiers") or []:
                if identifier["scheme"] == "sec-cik":
                    created += self._insert(namespace, view, "identifiers", identifier["value"], "market_issuer",
                                            f"cik:{identifier['value']}", "shared_identifier",
                                            self._market(identifier["value"]))
        created += self._accepted(namespace, store, ownership_namespace, scopes)
        # Targets acquired after a link was made resolve now (exact identity only).
        for link_id, target_kind, raw, key, citing in self.conn.execute(
                "SELECT link_id, target_kind, raw, target_key, citing_record_key FROM enforcement_links WHERE "
                "namespace=? AND status IN ('unresolved', 'provider_unavailable')", [namespace]).fetchall():
            resolved = None
            if target_kind == "court_docket":
                resolved = self._docket({"docket_number": raw})
            elif target_kind == "market_issuer":
                resolved = self._market(raw)
            elif target_kind in {"legal_work", "competition_case"}:
                action = store.by_key(namespace, citing)
                authority = action["record"].get("authority") if action else None
                parsed = [c for c in parse_references(raw, authority=authority) if c["key"] == key]
                if parsed:
                    resolved = (self._competition(competition_namespace, parsed[0]) if target_kind ==
                                "competition_case" else self._legal(legal_namespace, parsed[0], scopes))
            if resolved and resolved["status"] == "resolved":
                self.conn.execute("UPDATE enforcement_links SET status='resolved', target_key=?, "
                                  "target_revision_id=? WHERE link_id=?",
                                  [resolved.get("target_key") or key, resolved.get("target_revision_id"), link_id])
        return {"created": created, "links": self.links(namespace)}

    def _accepted(self, namespace: str, store: EnforcementStore, ownership_namespace: str | None, scopes) -> int:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return 0
        from src.kb.enforcement_identity import EnforcementIdentity

        created = 0
        identity = EnforcementIdentity(self.conn, initialize=False)
        for match in identity.all_accepted(namespace, scopes=scopes):
            view = store.by_key(namespace, match["subject_key"])
            if view is None:
                continue
            target_revision = None
            if ownership_namespace and table_exists(self.conn, "ownership_records"):
                row = self.conn.execute(
                    "SELECT v.revision_id FROM ownership_records r JOIN ownership_record_revisions v ON "
                    "v.namespace=r.namespace AND v.record_id=r.record_id AND v.revision=r.current_revision WHERE "
                    "r.namespace=? AND r.record_key=?", [ownership_namespace, match["ownership_key"]]).fetchone()
                target_revision = row[0] if row else None
            created += self._insert(namespace, view, "identity", match["ownership_key"], "ownership_entity",
                                    match["ownership_key"], "accepted_match",
                                    {"status": "resolved", "target_key": match["ownership_key"],
                                     "target_revision_id": target_revision, "candidate_id": match["candidate_id"],
                                     "decision_id": match["decision_id"], "method": match["method"],
                                     "low_evidence": match["low_evidence"], "reviewer": match["reviewer"]})
        return created

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, status: str | None = None, citing_record_key: str | None = None,
              target_kind: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "enforcement_links"):
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM enforcement_links WHERE namespace=? AND (? IS NULL OR status=?) AND "
            "(? IS NULL OR citing_record_key=?) AND (? IS NULL OR target_kind=?) "
            "ORDER BY citing_record_key, citing_revision_id, link_id",
            [namespace, status, status, citing_record_key, citing_record_key, target_kind, target_kind]).fetchall()
        out = []
        for row in rows:
            item = dict(zip(_COLUMNS, row))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            out.append({"contract": LINK_CONTRACT, **item})
        return out

    def list_links(self, namespace: str, *, scopes, status: str | None = None,
                   citing_record_key: str | None = None, target_kind: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        links = self.links(namespace, status=status, citing_record_key=citing_record_key, target_kind=target_kind)
        return {"links": links, "not_resolved": [link for link in links if link["status"] != "resolved"],
                "notice": "links by exact citation, published identifier or accepted identity match only; missing "
                          "providers and targets are reported, nothing is linked by topic or name similarity and no "
                          "causal relation to a market event is inferred"}

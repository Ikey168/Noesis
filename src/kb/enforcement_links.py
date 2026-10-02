"""Enforcement records linked to other packs by exact citation, shared identifier or accepted match (#2651, EN08).

Citations are parsed exactly from the text a regulator published (an action's
legal bases, charges and related references, an appeal's court docket) - never
by topic or name similarity:

* **US securities statutes** ``Section 17(a) of the Securities Act of 1933``,
  ``Section 10(b) of the Securities Exchange Act of 1934``, ``Section 206(2)
  of the Investment Advisers Act`` at their fixed 15 U.S.C. codification,
  **SEC rules** ``Rule 10b-5`` (17 C.F.R. § 240.10b-5) and US Code / CFR
  citations (the courts feature's parser);
* **US environmental statutes** named by ECHO (Clean Air Act, Clean Water Act,
  RCRA, SDWA, CERCLA, TSCA, FIFRA, EPCRA) at their codification;
* **FCA Handbook** provisions (``SYSC 6.1.1R``, ``PRIN 2.1.1R``), the
  numbered Principles for Businesses and sections of FSMA 2000;
* **GDPR articles** (``Article 5(1)(f)``, in an EDPB register entry or with
  "GDPR"), and EU acts, UK Acts, OJ references and competition case numbers
  through the competition feature's parser
  (:func:`src.kb.competition_citations.parse_references`).

:class:`EnforcementLinks` then links every current record revision to:

* **Legal works** (``legal.works``) by exact identifier - basis ``citation``;
* **competition cases** (``ownership.competition``) by a cited case number -
  basis ``citation``;
* **court dockets** (``legal.courts``) by a cited docket number of the action
  or of an appeal - basis ``citation``;
* **market filings** by CIK: SEC EDGAR filer records that carry the CIK a
  respondent publishes - basis ``shared_identifier``;
* **ownership entities** through an accepted EN07 match - basis
  ``accepted_match``.

Every link names the citing record revision and the target revision where one
exists. A missing provider (no Legal, courts, competition or ownership store)
is reported as ``provider_unavailable``; a missing target as ``unresolved``;
both keep the source text and are re-resolved on later runs. Nothing is
linked by inference: no causal link between an action and market events is
drawn.
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
# Fixed 15 U.S.C. codification of the sections SEC actions cite most; other sections stay unresolved statutes.
SECURITIES_SECTIONS = {
    "securities": {"5": "77e", "11": "77k", "12": "77l", "17": "77q"},
    "exchange": {"9": "78i", "10": "78j", "13": "78m", "14": "78n", "15": "78o", "16": "78p", "20": "78t",
                 "21": "78u", "30a": "78dd-1"},
    "advisers": {"203": "80b-3", "204": "80b-4", "206": "80b-6", "207": "80b-7"},
    "company": {"17": "80a-17", "34": "80a-33"},
}
_SEC_ACT = re.compile(r"Sections?\s+(\d+[A-Za-z]?)((?:\([a-z0-9]+\))*)\s+of\s+the\s+(Securities\s+Act(?:\s+of\s+1933)?|"
                      r"(?:Securities\s+)?Exchange\s+Act(?:\s+of\s+1934)?|(?:Investment\s+)?Advisers\s+Act(?:\s+of\s+"
                      r"1940)?|Investment\s+Company\s+Act(?:\s+of\s+1940)?)")
_SEC_RULE = re.compile(r"\bRules?\s+(\d{1,2}[a-z]{0,2}\d?-\d{1,2}[a-z]?)((?:\([a-z0-9]+\))*)")
ENVIRONMENTAL_ACTS = {
    "Clean Air Act": ("42", "7401"), "Clean Water Act": ("33", "1251"),
    "Resource Conservation and Recovery Act": ("42", "6901"), "Safe Drinking Water Act": ("42", "300f"),
    "CERCLA": ("42", "9601"), "Toxic Substances Control Act": ("15", "2601"),
    "Federal Insecticide, Fungicide, and Rodenticide Act": ("7", "136"),
    "Emergency Planning and Community Right-to-Know Act": ("42", "11001"),
}
_ENV_ACT = re.compile("(" + "|".join(re.escape(name) for name in ENVIRONMENTAL_ACTS) + r")(?:\s+(?:Section|§)\s*"
                      r"([\w().-]+))?")
_FCA_RULE = re.compile(r"\b(PRIN|SYSC|COBS|MAR|SUP|DEPP|CASS|COCON|APER|ICOBS|MCOB|CONC|DISP|PERG|FIT|BCOBS)\s+"
                       r"(\d+(?:\.\d+){1,3})([RGED])?\b")
_PRINCIPLE = re.compile(r"\bPrinciples?\s+(\d{1,2})\b(?:\s+of\s+the\s+Authority's\s+Principles\s+for\s+Businesses)?")
_FSMA = re.compile(r"\bsection\s+(\d+[A-Z]?)\s+of\s+the\s+(?:Act|Financial\s+Services\s+and\s+Markets\s+Act\s+2000)")
_GDPR = re.compile(r"\bArticles?\s+(\d{1,2})((?:\(\d+\))?(?:\([a-z]\))?)(\s+(?:of\s+the\s+)?(?:GDPR|General\s+Data\s+"
                   r"Protection\s+Regulation))?")
_DOCKET = re.compile(r"^\s*(\d{1,2}:\d{2}-[a-z]{2}-\d{3,6})")
GDPR_CELEX = "32016R0679"


def _norm_docket(value: Any) -> str:
    match = _DOCKET.match(str(value or ""))
    return match.group(1) if match else re.sub(r"\s+", "", str(value or "")).lower()


def parse_legal_bases(text: Any, *, context: str | None = None) -> list[dict[str, Any]]:
    """Exact statute, rule, Handbook, GDPR, EU-act and case references with their offsets, in text order."""
    text = str(text or "")
    found: list[dict[str, Any]] = []

    def add(kind: str, match: re.Match, key: str, forms: list[str], **extra: Any) -> None:
        found.append({"kind": kind, "raw": match.group(0).strip(), "start": match.start(), "end": match.end(),
                      "key": key, "forms": forms, **extra})

    for match in _SEC_ACT.finditer(text):
        act = match.group(3).lower()
        family = ("advisers" if "advisers" in act else "company" if "company" in act else
                  "exchange" if "exchange" in act else "securities")
        section = SECURITIES_SECTIONS[family].get(match.group(1).lower())
        provision = f"{match.group(1)}{match.group(2)}"
        if section:
            add("statute", match, f"usc:15:{section}", [f"usc:15:{section}", "usc:15"],
                provision=f"15 U.S.C. § {section}{match.group(2)}", act_provision=provision)
        else:
            add("statute", match, f"us-securities-act:{family}:{match.group(1).lower()}", [], provision=provision)
    for match in _SEC_RULE.finditer(text):
        add("regulation", match, f"cfr:17:240.{match.group(1)}", [f"cfr:17:240.{match.group(1)}", "cfr:17"],
            provision=f"17 C.F.R. § 240.{match.group(1)}{match.group(2)}")
    from src.kb.legal_court_citations import parse_us_citations

    for item in parse_us_citations(text):
        if item["kind"] in {"statute", "regulation"}:
            found.append({"kind": item["kind"], "raw": item["raw"], "start": item["start"], "end": item["end"],
                          "key": item["key"], "forms": [item["key"], item["key"].rsplit(":", 1)[0]],
                          "provision": item["normalized"]})
    for match in _ENV_ACT.finditer(text):
        title, section = ENVIRONMENTAL_ACTS[match.group(1)]
        add("statute", match, f"us-act:{match.group(1).lower().replace(' ', '-')}" +
            (f":{match.group(2)}" if match.group(2) else ""), [f"usc:{title}:{section}", f"usc:{title}"],
            provision=f"{match.group(1)}" + (f" § {match.group(2)}" if match.group(2) else ""),
            codified_at=f"{title} U.S.C. § {section} et seq.")
    for match in _FCA_RULE.finditer(text):
        rule = f"{match.group(1)} {match.group(2)}{match.group(3) or ''}"
        add("rule", match, f"fca-handbook:{rule}", [f"fca-handbook:{rule}"], provision=rule)
    for match in _PRINCIPLE.finditer(text):
        add("rule", match, f"fca-handbook:PRIN 2.1.1R:principle-{match.group(1)}",
            ["fca-handbook:PRIN 2.1.1R", "fca-handbook:PRIN"],
            provision=f"Principle {match.group(1)} (PRIN 2.1.1R)")
    for match in _FSMA.finditer(text):
        add("statute", match, f"uk:ukpga/2000/8:s{match.group(1)}", ["ukpga/2000/8",
                                                                       "http://www.legislation.gov.uk/ukpga/2000/8"],
            provision=f"Financial Services and Markets Act 2000, section {match.group(1)}")
    if context == "gdpr" or "GDPR" in text or "General Data Protection Regulation" in text:
        for match in _GDPR.finditer(text):
            if context != "gdpr" and not match.group(3):
                continue
            add("legal_act", match, f"celex:{GDPR_CELEX}:art{match.group(1)}", [GDPR_CELEX, f"CELEX:{GDPR_CELEX}"],
                celex=GDPR_CELEX, provision=f"Article {match.group(1)}{match.group(2)} GDPR")
    from src.kb.competition_citations import parse_references

    for item in parse_references(text):
        if any(f["start"] <= item["start"] < f["end"] for f in found):
            continue
        if item["kind"] == "treaty_article" or (item["kind"] == "statute" and item["key"].startswith("usc:")):
            continue  # TFEU articles are not enforcement bases here; US Code is parsed above
        found.append(item)
    unique: dict[tuple[int, str], dict[str, Any]] = {}
    for item in found:
        unique.setdefault((item["start"], item["key"]), item)
    return sorted(unique.values(), key=lambda c: (c["start"], c["kind"]))


_DDL = """
CREATE TABLE IF NOT EXISTS enforcement_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citing_record_key TEXT NOT NULL, citing_kind TEXT NOT NULL,
  citing_revision_id TEXT NOT NULL, field TEXT NOT NULL, raw TEXT NOT NULL, target_pack TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_key TEXT NOT NULL, status TEXT NOT NULL, target_record TEXT,
  target_revision TEXT, basis TEXT NOT NULL, detail TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""
_COLUMNS = ("link_id", "citing_record_key", "citing_kind", "citing_revision_id", "field", "raw", "target_pack",
            "target_kind", "target_key", "status", "target_record", "target_revision", "basis", "detail",
            "evidence_json")


def record_citations(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(field, citation) pairs an enforcement record states, parsed exactly."""
    out: list[tuple[str, dict[str, Any]]] = []
    context = "gdpr" if body.get("source", {}).get("provider") == "edpb-art60" else None
    for field in ("legal_bases", "charges_as_published", "related_references"):
        for text in body.get(field) or []:
            out += [(field, c) for c in parse_legal_bases(text, context=context)]
    unique: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for field, item in out:
        unique.setdefault(("legal" if field != "related_references" else field, item["key"]), (field, item))
    return list(unique.values())


class EnforcementLinks:
    """Link enforcement records to Legal works, competition cases, court dockets, filings and ownership entities."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ resolvers

    def _legal(self, legal_namespace: str, citation: dict[str, Any], scopes) -> dict[str, Any]:
        if not table_exists(self.conn, "legal_works"):
            return {"status": "provider_unavailable", "detail": "no Legal store (legal.works) in this deployment"}
        if not citation.get("forms"):
            return {"status": "unresolved", "detail": "no fixed codification is recorded for this reference"}
        from src.kb.legal import LegalError, LegalStore

        legal = LegalStore(self.conn, initialize=False)
        legal_scopes = set(scopes) | {"knowledge:legal:read", f"namespace:{legal_namespace}:read"}
        tried = []
        for form in citation["forms"]:
            try:
                answer = legal.lookup(legal_namespace, scopes=legal_scopes, identifier=form)
            except LegalError:
                continue
            tried.append(form)
            works = sorted({w["work_id"] for w in answer["works"]})
            if len(works) == 1:
                return {"status": "resolved", "target_record": works[0], "detail": f"exact identifier {form}",
                        "evidence": {"tried": tried}}
            if works:
                return {"status": "ambiguous", "detail": f"several Legal works carry {form}",
                        "evidence": {"works": works, "tried": tried}}
        return {"status": "unresolved", "detail": "no acquired Legal work carries this reference",
                "evidence": {"tried": tried}}

    def _competition(self, competition_namespace: str, citation: dict[str, Any]) -> dict[str, Any]:
        if citation.get("authority") != "ec":
            return {"status": "unresolved", "detail": "exact case reference of an authority whose cases are not "
                                                      "keyed by this reference"}
        if not table_exists(self.conn, "ownership_records"):
            return {"status": "provider_unavailable", "detail": "no competition store (ownership.competition)"}
        from src.kb.competition import CompetitionStore
        from src.kb.competition_records import case_key

        key = case_key("ec", citation["case_number"])
        view = CompetitionStore(self.conn, initialize=False).by_key(competition_namespace, key)
        if view is None:
            return {"status": "unresolved", "detail": "the cited case is not acquired"}
        return {"status": "resolved", "target_record": key, "target_revision": view["revision_id"],
                "detail": "exact case number"}

    def _docket(self, courts_namespace: str, docket_number: str) -> dict[str, Any]:
        if not table_exists(self.conn, "legal_docket_revisions"):
            return {"status": "provider_unavailable", "detail": "no court docket store (legal.courts feature)"}
        wanted = _norm_docket(docket_number)
        rows = self.conn.execute("SELECT record_key, revision_id, docket_number FROM legal_docket_revisions WHERE "
                                 "namespace=? ORDER BY record_key, revision_no", [courts_namespace]).fetchall()
        latest: dict[str, tuple[str, str]] = {}
        for key, revision_id, number in rows:
            if _norm_docket(number) == wanted:
                latest[key] = (revision_id, number)
        if len(latest) == 1:
            key, (revision_id, _) = next(iter(latest.items()))
            return {"status": "resolved", "target_record": key, "target_revision": revision_id,
                    "detail": "exact docket number"}
        if latest:
            return {"status": "ambiguous", "detail": "several dockets carry this number (different courts)",
                    "evidence": {"dockets": sorted(latest)}}
        return {"status": "unresolved", "detail": "the cited docket is not acquired"}

    def _filings(self, ownership_namespace: str, cik: str, scopes) -> dict[str, Any]:
        if not table_exists(self.conn, "ownership_records"):
            return {"status": "provider_unavailable", "detail": "no market filing records (SEC EDGAR filers) acquired"}
        from src.kb.ownership_store import OwnershipError, OwnershipStore
        from src.kb.ownership_store import authorize as ownership_authorize

        try:
            ownership_authorize(ownership_namespace, set(scopes), "knowledge:ownership:read")
        except OwnershipError:
            return {"status": "provider_unavailable", "detail": "no read access to the ownership namespace"}
        answer = OwnershipStore(self.conn, initialize=False).lookup(ownership_namespace, "sec-cik", cik,
                                                                    principal_id=None, scopes={"operator"})
        filers = [m for m in answer["matches"] if m["record"]["source"]["provider"] == "sec-edgar"]
        if len(filers) == 1:
            return {"status": "resolved", "target_record": filers[0]["record"]["record_key"],
                    "target_revision": filers[0]["revision_id"], "detail": f"shared identifier CIK {cik}"}
        if filers:
            return {"status": "ambiguous", "detail": "several filer records carry this CIK",
                    "evidence": {"records": [f["record"]["record_key"] for f in filers]}}
        return {"status": "unresolved", "detail": "no SEC EDGAR filer record carries this CIK"}

    # ---------------------------------------------------------------- write

    def _insert(self, namespace, view, field, raw, target_pack, target_kind, target_key, basis, resolved) -> int:
        body = view["record"]
        link_id = "enforcement-link:" + digest([namespace, view["revision_id"], field, target_pack, target_key])[:24]
        if self.conn.execute("SELECT 1 FROM enforcement_links WHERE link_id=?", [link_id]).fetchone():
            return 0
        self.conn.execute(
            "INSERT INTO enforcement_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [link_id, namespace, body["record_key"], body["kind"], view["revision_id"], field, raw, target_pack,
             target_kind, target_key, resolved["status"], resolved.get("target_record"),
             resolved.get("target_revision"), basis, resolved["detail"], canonical(resolved.get("evidence") or {}),
             self.now()])
        return 1

    def link(self, namespace: str, *, scopes: Iterable[str], legal_namespace: str = "global",
             competition_namespace: str = "competition", courts_namespace: str = "global",
             ownership_namespace: str | None = "ownership") -> dict[str, Any]:
        """Link every current record revision; idempotent per citing revision, and re-resolves open links."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        store = EnforcementStore(self.conn, initialize=False)
        created = 0
        for view in store.views(namespace, ("enforcement_action", "appeal", "respondent")):
            body = view["record"]
            if body["kind"] == "enforcement_action":
                for field, citation in record_citations(body):
                    if citation["kind"] == "case":
                        resolved = self._competition(competition_namespace, citation)
                        created += self._insert(namespace, view, field, citation["raw"], "ownership.competition",
                                                "competition_case", citation["key"], "citation", resolved)
                    elif citation["kind"] != "oj":
                        resolved = self._legal(legal_namespace, citation, scopes)
                        resolved.setdefault("evidence", {})["provision"] = citation.get("provision")
                        created += self._insert(namespace, view, field, citation["raw"], "legal.works", "legal_work",
                                                citation["key"], "citation", resolved)
                for case in body.get("court_cases") or []:
                    resolved = self._docket(courts_namespace, case["docket_number"])
                    created += self._insert(namespace, view, "court_cases", case["docket_number"], "legal.courts",
                                            "court_docket", f"docket:{_norm_docket(case['docket_number'])}",
                                            "citation", resolved)
            elif body["kind"] == "appeal" and (body.get("court_docket") or {}).get("docket_number"):
                number = body["court_docket"]["docket_number"]
                created += self._insert(namespace, view, "court_docket", number, "legal.courts", "court_docket",
                                        f"docket:{_norm_docket(number)}", "citation",
                                        self._docket(courts_namespace, number))
            elif body["kind"] == "respondent" and ownership_namespace:
                for identifier in body.get("identifiers") or []:
                    if identifier.get("scheme") == "sec-cik":
                        created += self._insert(namespace, view, "identifiers", identifier["value"], "market.filings",
                                                "sec_filer", f"sec-cik:{identifier['value'].lstrip('0')}",
                                                "shared_identifier",
                                                self._filings(ownership_namespace, identifier["value"], scopes))
        if ownership_namespace:
            created += self._accepted(namespace, store, scopes)
        created += self._reresolve(namespace, scopes, legal_namespace=legal_namespace,
                                   competition_namespace=competition_namespace, courts_namespace=courts_namespace,
                                   ownership_namespace=ownership_namespace)
        return {"created": created, **self.list_links(namespace, scopes=scopes)}

    def _accepted(self, namespace: str, store: EnforcementStore, scopes: set[str]) -> int:
        """One link per accepted EN07 match, pointing at the respondent revision and the identity decision."""
        from src.kb.enforcement_identity import EnforcementIdentity

        created = 0
        for candidate in EnforcementIdentity(self.conn, initialize=False).candidates(namespace, scopes=scopes):
            if candidate["state"] != "accepted":
                continue
            view = store.by_key(namespace, candidate["subject_key"])
            if view is None:
                continue
            created += self._insert(namespace, view, "identity", candidate["subject_key"], "ownership.core",
                                    "ownership_entity", candidate["ownership_key"], "accepted_match",
                                    {"status": "resolved", "target_record": candidate["ownership_key"],
                                     "target_revision": candidate["decision_id"],
                                     "detail": f"accepted {candidate['method']} match by {candidate['reviewer']}",
                                     "evidence": {"candidate_id": candidate["candidate_id"],
                                                  "low_evidence": candidate["low_evidence"]}})
        # A reverted or rejected match no longer links: its link is marked withdrawn, never deleted.
        accepted = {c["candidate_id"] for c in EnforcementIdentity(self.conn, initialize=False).candidates(
            namespace, scopes=scopes) if c["state"] == "accepted"}
        for link_id, evidence in self.conn.execute(
                "SELECT link_id, evidence_json FROM enforcement_links WHERE namespace=? AND basis='accepted_match' "
                "AND status='resolved'", [namespace]).fetchall():
            if json.loads(evidence).get("candidate_id") not in accepted:
                self.conn.execute("UPDATE enforcement_links SET status='withdrawn', detail='the identity match was "
                                  "reverted or rejected' WHERE link_id=?", [link_id])
        return created

    def _reresolve(self, namespace: str, scopes: set[str], **namespaces: Any) -> int:
        """Targets acquired (or providers installed) after a link was made resolve now; exact identity only."""
        changed = 0
        for link_id, raw, target_pack, target_key, status in self.conn.execute(
                "SELECT link_id, raw, target_pack, target_key, status FROM enforcement_links WHERE namespace=? AND "
                "status IN ('unresolved', 'provider_unavailable')", [namespace]).fetchall():
            if target_pack == "legal.courts":
                resolved = self._docket(namespaces["courts_namespace"], raw)
            elif target_pack == "market.filings":
                if not namespaces["ownership_namespace"]:
                    continue
                resolved = self._filings(namespaces["ownership_namespace"], raw, scopes)
            else:
                parsed = [c for c in parse_legal_bases(raw, context="gdpr") if c["key"] == target_key]
                if not parsed:
                    continue
                resolved = (self._competition(namespaces["competition_namespace"], parsed[0])
                            if target_pack == "ownership.competition" else
                            self._legal(namespaces["legal_namespace"], parsed[0], scopes))
            if resolved["status"] != status:
                # Resolved now, or the provider is installed and the target is still missing (unresolved).
                self.conn.execute("UPDATE enforcement_links SET status=?, target_record=?, target_revision=?, "
                                  "detail=? WHERE link_id=?", [resolved["status"], resolved.get("target_record"),
                                                               resolved.get("target_revision"), resolved["detail"],
                                                               link_id])
                changed += resolved["status"] == "resolved"
        return changed

    # ----------------------------------------------------------------- read

    def links(self, namespace: str, *, status: str | None = None, citing_record_key: str | None = None,
              target_pack: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "enforcement_links"):
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM enforcement_links WHERE namespace=? AND (? IS NULL OR status=?) AND "
            "(? IS NULL OR citing_record_key=?) AND (? IS NULL OR target_pack=?) ORDER BY citing_record_key, "
            "citing_revision_id, link_id",
            [namespace, status, status, citing_record_key, citing_record_key, target_pack, target_pack]).fetchall()
        out = []
        for row in rows:
            item = dict(zip(_COLUMNS, row))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            out.append({"contract": LINK_CONTRACT, **item})
        return out

    def list_links(self, namespace: str, *, scopes: Iterable[str], status: str | None = None,
                   citing_record_key: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        links = self.links(namespace, status=status, citing_record_key=citing_record_key)
        return {"links": links,
                "unresolved": [link for link in links if link["status"] in {"unresolved", "ambiguous"}],
                "providers_unavailable": sorted({link["target_pack"] for link in links
                                                 if link["status"] == "provider_unavailable"}),
                "notice": "links by exact citation, shared published identifier or accepted identity match only; "
                          "missing providers and targets are reported, never dropped; no causal link between an "
                          "action and market events is drawn"}

"""Exact legal and case citations in competition records (#2217, CS08).

References are parsed exactly from the text an authority published - an EC
decision's legal basis, a CMA page body, an FTC or DOJ page and its document
titles - never by topic or name similarity:

* **EU acts** ``Council Regulation (EC) No 139/2004``, ``Commission Regulation
  (EU) No 651/2014``, ``Regulation (EU) 2022/1925``, ``Directive 2014/104/EU``
  (CELEX ``3yyyyRnnnn`` / ``3yyyyLnnnn``), literal CELEX numbers and ELI URIs;
* **Treaty articles** ``Article 101 TFEU``, ``Article 107(3)(c) TFEU`` (key
  ``tfeu:<article>``; resolved through the per-article CELEX forms
  ``12016E<nnn>``/``12012E<nnn>``/``12008E<nnn>``);
* **UK Acts** ``Enterprise Act 2002``, ``Competition Act 1998``,
  ``Enterprise and Regulatory Reform Act 2013``, ``Digital Markets,
  Competition and Consumers Act 2024`` (their legislation.gov.uk identifiers)
  and ``legislation.gov.uk/ukpga/yyyy/n`` links;
* **US statutes** ``15 U.S.C. § 18`` (the courts feature's parser,
  :func:`src.kb.legal_court_citations.parse_us_citations`) and the named
  sections of the Sherman, Clayton and FTC Acts at their fixed codification
  (``Section 7 of the Clayton Act`` is ``15 U.S.C. § 18``);
* **OJ references** ``OJ C 401, 1.10.2025, p. 5``;
* **Cases** Commission case numbers ``M.nnnnn``, ``AT.nnnnn``, ``SA.nnnnn``
  and CMA case references ``ME/nnnn/yy``.

A reference that no acquired Legal work or case carries stays an
**unresolved** citation with its source text. Links record the citing record
revision; the citing relationship is never characterised.
"""

from __future__ import annotations

import re
from typing import Any

_EU_REG_OLD = re.compile(r"(?:(Council|Commission)\s+)?(Regulation|Directive|Decision)\s+\((EC|EU|EEC|Euratom)\)\s+"
                         r"No\.?\s*(\d{1,4})/(\d{2,4})")
_EU_NEW = re.compile(r"(?:(Council|Commission)\s+)?(Regulation|Directive|Decision)\s+\((EU)\)\s+(\d{4})/(\d{1,4})")
_EU_DIRECTIVE = re.compile(r"(Directive)\s+(\d{2,4})/(\d{1,4})/(EC|EU|EEC)")
_CELEX = re.compile(r"(?<![\w/])(3\d{4}[RLDM]\d{4,5})(?![\w])")
_ELI = re.compile(r"https?://data\.europa\.eu/eli/[A-Za-z0-9_./-]+[A-Za-z0-9/]")
_TFEU = re.compile(r"Articles?\s+(\d{2,3})((?:\(\d+\))?(?:\([a-z]\))?)\s+(?:of\s+the\s+)?"
                   r"(TFEU|Treaty\s+on\s+the\s+Functioning\s+of\s+the\s+European\s+Union)")
_OJ = re.compile(r"OJ\s+([LC])\s+(\d{1,4}[A-Z]?),\s+(\d{1,2}\.\d{1,2}\.\d{4}),\s+p\.\s+(\d+)")
_EC_CASE = re.compile(r"(?<![\w.])(M|AT|SA)\.(\d{3,6})(?![\w.]*\d)")
_CMA_REF = re.compile(r"(?<![\w/])(ME|CE|MKT)/(\d{4})/(\d{2})(?![\w/])")
_UK_LINK = re.compile(r"legislation\.gov\.uk/(ukpga|uksi)/(\d{4})/(\d+)")
UK_ACTS = {
    "Enterprise Act 2002": "ukpga/2002/40",
    "Competition Act 1998": "ukpga/1998/41",
    "Enterprise and Regulatory Reform Act 2013": "ukpga/2013/24",
    "Digital Markets, Competition and Consumers Act 2024": "ukpga/2024/13",
    "Subsidy Control Act 2022": "ukpga/2022/23",
}
_UK_ACT = re.compile("(" + "|".join(re.escape(name) for name in UK_ACTS) + ")")
# Fixed codification of the named US antitrust sections (15 U.S.C.); a named section is an exact reference.
US_NAMED_SECTIONS = {("sherman", "1"): "1", ("sherman", "2"): "2", ("clayton", "7"): "18", ("clayton", "8"): "19",
                     ("clayton", "7a"): "18a", ("ftc", "5"): "45", ("ftc", "13(b)"): "53"}
_US_NAMED = re.compile(r"Section\s+(\d+[a-z]?(?:\([a-z]\))?)\s+of\s+the\s+(Sherman|Clayton|FTC|Federal\s+Trade\s+"
                       r"Commission)\s+Act")


def _celex_forms(celex: str) -> list[str]:
    return [celex, f"CELEX:{celex}"]


def _uk_forms(ident: str) -> list[str]:
    kind, year, number = ident.split("/")
    return [ident, f"http://www.legislation.gov.uk/{ident}", f"https://www.legislation.gov.uk/{ident}",
            f"{year} c. {number}"]


def _eu_act(kind: str, year: str, number: str) -> str:
    letter = {"Regulation": "R", "Directive": "L", "Decision": "D"}[kind]
    full_year = year if len(year) == 4 else ("19" + year if int(year) > 50 else "20" + year)
    return f"3{full_year}{letter}{int(number):04d}"


def parse_references(text: Any) -> list[dict[str, Any]]:
    """Exact legal-act, treaty-article, statute, OJ and case references with their offsets, in text order."""
    text = str(text or "")
    found: list[dict[str, Any]] = []

    def add(kind: str, match: re.Match, key: str, forms: list[str], **extra: Any) -> None:
        found.append({"kind": kind, "raw": match.group(0).strip(), "start": match.start(), "end": match.end(),
                      "key": key, "forms": forms, **extra})

    for match in _EU_REG_OLD.finditer(text):
        celex = _eu_act(match.group(2), match.group(5), match.group(4))
        add("legal_act", match, f"celex:{celex}", _celex_forms(celex), celex=celex)
    for match in _EU_NEW.finditer(text):
        celex = _eu_act(match.group(2), match.group(4), match.group(5))
        add("legal_act", match, f"celex:{celex}", _celex_forms(celex), celex=celex)
    for match in _EU_DIRECTIVE.finditer(text):
        celex = _eu_act("Directive", match.group(2), match.group(3))
        add("legal_act", match, f"celex:{celex}", _celex_forms(celex), celex=celex)
    for match in _CELEX.finditer(text):
        if any(f["start"] <= match.start() < f["end"] for f in found):
            continue
        add("legal_act", match, f"celex:{match.group(1)}", _celex_forms(match.group(1)), celex=match.group(1))
    for match in _ELI.finditer(text):
        add("legal_act", match, f"eli:{match.group(0).split('data.europa.eu/')[-1]}",
            [match.group(0), match.group(0).split("data.europa.eu/")[-1]])
    for match in _TFEU.finditer(text):
        article = match.group(1)
        add("treaty_article", match, f"tfeu:{article}",
            [f"12016E{int(article):03d}", f"12012E{int(article):03d}", f"12008E{int(article):03d}"],
            provision=f"Article {article}{match.group(2)} TFEU")
    for match in _UK_ACT.finditer(text):
        ident = UK_ACTS[match.group(1)]
        add("statute", match, f"uk:{ident}", _uk_forms(ident))
    for match in _UK_LINK.finditer(text):
        ident = "/".join(match.groups())
        add("statute", match, f"uk:{ident}", _uk_forms(ident))
    from src.kb.legal_court_citations import parse_us_citations

    for item in parse_us_citations(text):
        if item["kind"] == "statute":
            found.append({"kind": "statute", "raw": item["raw"], "start": item["start"], "end": item["end"],
                          "key": item["key"], "forms": [item["key"], f"usc:{item['title']}"],
                          "provision": item["provision"]})
    for match in _US_NAMED.finditer(text):
        act = match.group(2).split()[0].lower()
        act = "ftc" if act in {"ftc", "federal"} else act
        section = US_NAMED_SECTIONS.get((act, match.group(1).lower()))
        if section:
            add("statute", match, f"usc:15:{section}", [f"usc:15:{section}", "usc:15"],
                provision=f"15 U.S.C. § {section}")
    for match in _OJ.finditer(text):
        add("oj", match, "oj:" + re.sub(r"\s+", "", match.group(0)).lower(), [match.group(0)])
    for match in _EC_CASE.finditer(text):
        number = f"{match.group(1)}.{match.group(2)}"
        add("case", match, f"case:ec:{number}", [number], authority="ec", case_number=number)
    for match in _CMA_REF.finditer(text):
        add("case", match, f"case:uk-cma-ref:{match.group(0)}", [match.group(0)], authority="uk-cma",
            case_number=match.group(0))
    unique: dict[tuple[int, str], dict[str, Any]] = {}
    for item in found:
        unique.setdefault((item["start"], item["key"]), item)
    return sorted(unique.values(), key=lambda c: (c["start"], c["kind"]))


LINK_CONTRACT = "noesis-competition-citation-link-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS competition_citation_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citing_record_key TEXT NOT NULL, citing_kind TEXT NOT NULL,
  citing_revision_id TEXT NOT NULL, field TEXT NOT NULL, raw TEXT NOT NULL, citation_kind TEXT NOT NULL,
  target_key TEXT NOT NULL, status TEXT NOT NULL, target_case_key TEXT, target_work_id TEXT, legal_namespace TEXT,
  basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
"""
_COLUMNS = ("link_id", "citing_record_key", "citing_kind", "citing_revision_id", "field", "raw", "citation_kind",
            "target_key", "status", "target_case_key", "target_work_id", "legal_namespace", "basis", "evidence_json")


def record_citations(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(field, citation) pairs a competition record states, parsed exactly."""
    out: list[tuple[str, dict[str, Any]]] = []
    for field in ("legal_references", "related_case_numbers"):
        for text in body.get(field) or []:
            out += [(field, c) for c in parse_references(text)]
    citation = body.get("citation") or {}
    for field in ("celex", "oj_reference", "eli"):
        if citation.get(field):
            out += [(f"citation.{field}", c) for c in parse_references(citation[field])]
    if body["kind"] == "state_aid_award" and body.get("sa_number"):
        out += [("sa_number", c) for c in parse_references(body["sa_number"]) if c["kind"] == "case"]
    unique: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for field, item in out:
        unique.setdefault((field, item["key"]), (field, item))
    return list(unique.values())


class CompetitionCitations:
    """Link cases, decision documents and awards to cited Legal works and competition cases by exact citation."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        import time

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _resolve(self, namespace: str, legal_namespace: str, citation: dict[str, Any], scopes) -> dict[str, Any]:
        from src.kb.competition import table_exists
        from src.kb.competition_records import case_key

        if citation["kind"] == "case":
            if citation.get("authority") != "ec":
                return {"status": "unresolved", "basis": "exact case reference; no acquired case carries this "
                                                         "authority reference"}
            key = case_key("ec", citation["case_number"])
            row = self.conn.execute("SELECT 1 FROM ownership_records WHERE namespace=? AND record_key=?",
                                    [namespace, key]).fetchone()
            if row:
                return {"status": "resolved", "target_case_key": key, "basis": "exact case number"}
            return {"status": "unresolved", "basis": "the cited case is not acquired in this namespace"}
        if not table_exists(self.conn, "legal_works"):
            return {"status": "legal_unavailable", "basis": "no Legal store in this deployment"}
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
                return {"status": "resolved", "target_work_id": works[0], "basis": f"exact identifier {form}",
                        "tried": tried}
            if works:
                return {"status": "ambiguous", "basis": f"several Legal works carry {form}", "works": works,
                        "tried": tried}
        return {"status": "unresolved", "basis": "no acquired Legal work carries this reference", "tried": tried}

    def link(self, namespace: str, *, legal_namespace: str = "global", scopes) -> dict[str, Any]:
        """Parse every current competition record; idempotent per citing revision, and re-resolves open links."""
        from src.kb.competition import WRITE_SCOPE, CompetitionStore, authorize
        from src.kb.ownership_records import canonical, digest

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        store = CompetitionStore(self.conn, initialize=False)
        created = 0
        for view in store.views(namespace, ("competition_case", "decision_document", "state_aid_award")):
            body = view["record"]
            for field, citation in record_citations(body):
                link_id = "competition-citation:" + digest([namespace, view["revision_id"], field,
                                                            citation["key"]])[:24]
                if self.conn.execute("SELECT 1 FROM competition_citation_links WHERE link_id=?", [link_id]).fetchone():
                    continue
                resolved = self._resolve(namespace, legal_namespace, citation, scopes)
                evidence = {k: v for k, v in resolved.items() if k not in {"status", "basis"}}
                evidence["provision"] = citation.get("provision")
                self.conn.execute(
                    "INSERT INTO competition_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [link_id, namespace, body["record_key"], body["kind"], view["revision_id"], field,
                     citation["raw"], citation["kind"], citation["key"], resolved["status"],
                     resolved.get("target_case_key"), resolved.get("target_work_id"),
                     legal_namespace if citation["kind"] != "case" else None, resolved["basis"], canonical(evidence),
                     self.now()])
                created += 1
        # Targets acquired after a link was made resolve now (exact identity only).
        for link_id, raw, key in self.conn.execute(
                "SELECT link_id, raw, target_key FROM competition_citation_links WHERE namespace=? AND "
                "status IN ('unresolved', 'legal_unavailable')", [namespace]).fetchall():
            parsed = [c for c in parse_references(raw) if c["key"] == key]
            if parsed:
                resolved = self._resolve(namespace, legal_namespace, parsed[0], scopes)
                if resolved["status"] == "resolved":
                    self.conn.execute("UPDATE competition_citation_links SET status='resolved', target_case_key=?, "
                                      "target_work_id=?, basis=? WHERE link_id=?",
                                      [resolved.get("target_case_key"), resolved.get("target_work_id"),
                                       resolved["basis"], link_id])
        return {"created": created, "links": self.links(namespace)}

    def links(self, namespace: str, *, status: str | None = None, citing_record_key: str | None = None,
              target_case_key: str | None = None) -> list[dict[str, Any]]:
        import json

        from src.kb.competition import table_exists

        if not table_exists(self.conn, "competition_citation_links"):
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM competition_citation_links WHERE namespace=? AND (? IS NULL OR "
            "status=?) AND (? IS NULL OR citing_record_key=?) AND (? IS NULL OR target_case_key=?) "
            "ORDER BY citing_record_key, citing_revision_id, link_id",
            [namespace, status, status, citing_record_key, citing_record_key, target_case_key,
             target_case_key]).fetchall()
        out = []
        for row in rows:
            item = dict(zip(_COLUMNS, row))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            out.append({"contract": LINK_CONTRACT, **item})
        return out

    def list_links(self, namespace: str, *, scopes, status: str | None = None,
                   citing_record_key: str | None = None) -> dict[str, Any]:
        from src.kb.competition import READ_SCOPE, authorize

        authorize(namespace, scopes, READ_SCOPE)
        links = self.links(namespace, status=status, citing_record_key=citing_record_key)
        return {"links": links, "unresolved": [link for link in links if link["status"] != "resolved"],
                "notice": "exact citation links only; nothing is linked by topic or name similarity and the citing "
                          "relationship is not characterised"}

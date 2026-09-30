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

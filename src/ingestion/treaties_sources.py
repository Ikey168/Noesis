"""Treaties and treaty actions acquisition for the Legal pack (#2581, TR01, TR03-TR05).

The Legal pack's treaties provider (``legal.treaties``) adds one native
connector, ``treaties``, registered beside the Legal adapters of
:mod:`src.ingestion.legal_sources` in the ``legal-research`` source pack. Each
source reads one bounded, declared selection from one documented provider and
emits ``noesis-treaty-record-v1`` records as the provider published them:

* ``untc-status-html`` - the UN Treaty Collection status page of a multilateral
  treaty deposited with the Secretary-General (MTDSG), keyed by chapter and
  treaty number: the treaty header (place and date of adoption, entry into
  force, UNTS registration, text reference), the participant table
  (signature and consent to be bound with the published suffixes), the
  declarations, reservations and objections verbatim with their anchors and
  the numbered notes verbatim. **Gated on the TR01 licence decision**: the UN
  terms reserve reuse and compilation of the online collection to written
  permission, so the recorded decision is ``declined`` and the adapter refuses
  to fetch until an operator records an accepted decision with its reference.
* ``cellar-agreement-sparql`` - EU international agreements (CELEX sector 2)
  through the existing :class:`src.ingestion.legal_sources.CellarLegalAdapter`
  (its bounded SPARQL query and grouping are reused unchanged): the language
  expressions are grouped into one treaty record per CELEX, and one more
  bounded query on the same host reads the signature date and the EU acts
  CELLAR links to the agreement (``cdm:work_cites_work`` in either direction,
  plus any operator-declared CDM relation IRIs), recorded as citations.
* ``coe-chart-html`` - the Council of Europe Treaty Office chart of signatures
  and ratifications of one CETS/ETS treaty and its declarations page: per state
  or organisation, signature, ratification/accession/succession, entry into
  force and denunciation as charted; reservations, declarations, objections,
  withdrawals and denunciations verbatim with their dates.

Every page is one selection unit and is all-or-nothing; a response from another
host is a network-policy failure. Receipts name every request, status and
response digest. ``PROVIDER_CONTRACTS``, ``LICENCE_DECISIONS``,
``MINIMISATION``, ``BOUNDED_COVERAGE`` and ``LIVE_VERIFICATION`` are the
machine-readable copy of the TR01 audit
(``docs/development/treaties-evidence/source-audit.md``).

Nothing here gives legal advice, infers an obligation or compliance, or
interprets the legal effect of a reservation; treaty texts are linked, never
stored, and no natural-person field is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.treaties_records import (
    RECORD_CONTRACT,
    TreatiesError,
    action_key,
    participant_key,
    treaty_key,
    validate_record,
)

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECEIPT_CONTRACT = "noesis-treaty-acquisition-receipt-v1"
CONNECTOR = "treaties"
MAX_UNITS = 20
REVIEW_BOUNDARY = ("Treaty and treaty-action records are kept as the depositary or publisher published them. "
                   "Reservations, declarations and objections are quoted verbatim; nothing here is legal advice, "
                   "an inference of obligations or compliance, or an interpretation of a reservation's legal "
                   "effect, and treaty texts are linked, not reproduced.")
FORMATS: dict[str, dict[str, Any]] = {
    "untc-status-html": {"provider": "untc", "unit": "treaties", "feature": "treaties-untc"},
    "cellar-agreement-sparql": {"provider": "cellar", "unit": "agreements", "feature": "treaties-eu"},
    "coe-chart-html": {"provider": "coe-treaty-office", "unit": "treaties", "feature": "treaties-coe"},
}
UNTC_SITE = "https://treaties.un.org"
COE_SITE = "https://www.coe.int/en/web/conventions"
EURLEX_SITE = "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:"
UNTC_ATTRIBUTION = "Source: United Nations Treaty Collection (reuse only under written permission of the United Nations)."
CELLAR_ATTRIBUTION = "Source: Publications Office of the European Union, CELLAR (reuse authorised, Commission Decision 2011/833/EU)."
COE_ATTRIBUTION = "Source: Council of Europe Treaty Office (reproduction authorised with acknowledgement of the source)."

# TR01 access decisions. The official pages could not be fetched from this runtime (egress blocked); terms and
# endpoints were read from search-result extracts of the official pages on 2026-09-30 and every item marked
# ``verify`` must be checked against the live pages and a real response before a dated live run (TR13, #2645).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "untc": {
        "publisher": "United Nations, Office of Legal Affairs, Treaty Section (United Nations Treaty Collection)",
        "endpoints": ["/Pages/ViewDetails.aspx?mtdsg_no={chapter-number}&chapter={chapter}&clang=_en"],
        "formats": ["untc-status-html"],
        "authentication": "none (public web pages; no documented API or bulk export found - verify)",
        "rate_limits": "none published (verify); one status page per declared treaty per run",
        "identifiers": ["MTDSG chapter and treaty number (e.g. XVIII-10)", "UNTS registration number as published",
                        "UNTS volume and page reference as published"],
        "revisions": "the page's 'Status as at' stamp is the depositary revision; a changed page is a new revision "
                     "of each changed record; an action the page no longer lists is a removed-by-source revision",
        "licence": "UN terms of use: materials may be downloaded for personal, non-commercial use without any right "
                   "to redistribute, compile or create derivative works; the online UN Treaty Collection is stated "
                   "to be proprietary and reusable only with prior written permission (treaty texts themselves are "
                   "stated to be public domain) - verify on the live pages",
        "attribution": UNTC_ATTRIBUTION,
        "access_decision": "declined",
        "reason": "compiling the status database into Noesis is reuse that the UN terms reserve to written "
                  "permission; the adapter and parser exist (fixture-tested) but refuse to fetch until an operator "
                  "records an accepted licence decision with its permission reference",
    },
    "cellar": {
        "publisher": "Publications Office of the European Union (CELLAR SPARQL endpoint)",
        "endpoints": [("https://publications.europa.eu/webapi/rdf/sparql (the Legal pack's CELLAR adapter query, "
                      "plus one agreement query on the same host)")],
        "formats": ["cellar-agreement-sparql"],
        "authentication": "none",
        "rate_limits": "no published quota for the public SPARQL endpoint (verify); one bounded query per page, "
                       "1-20 CELEX numbers per source",
        "identifiers": ["CELEX (sector 2 international agreements)", "ELI where published",
                        "CELLAR work, expression, manifestation and item URIs", "linked acts' CELEX"],
        "revisions": "CELLAR states no status stamp: a changed result set (a new expression, a new linked act, a "
                     "corrected date) is a new revision dated by the acquisition",
        "licence": "EUR-Lex/CELLAR legal documents may be reused for commercial or non-commercial purposes with "
                   "acknowledgement (Commission Decision 2011/833/EU; EUR-Lex legal notice) - verify",
        "attribution": CELLAR_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "reuses the Legal pack's CELLAR adapter (prior live evidence for its base query); the agreement "
                  "query's cdm:resource_legal_date_signature property and any declared relation IRIs are verify",
    },
    "coe-treaty-office": {
        "publisher": "Council of Europe Treaty Office (Directorate of Legal Advice and Public International Law)",
        "endpoints": ["/full-list?module=signatures-by-treaty&treatynum={number}",
                      "/full-list?module=declarations-by-treaty&treatynum={number}"],
        "formats": ["coe-chart-html"],
        "authentication": "none (public web pages; no documented API found - verify)",
        "rate_limits": "none published (verify); two pages per declared treaty per run",
        "identifiers": ["CETS/ETS number", "state or organisation as charted"],
        "revisions": "the chart's 'Status as of' date is the depositary revision; a changed chart or declaration is "
                     "a new revision; denunciations and withdrawals are actions, never deletions",
        "licence": "reproduction of material on Council of Europe websites is authorised for private use and for "
                   "informational and educational uses relating to the Council's work, with the source "
                   "acknowledged; commercial use needs prior permission; only the CETS printed texts are authentic "
                   "(Treaty Office legal notice) - verify",
        "attribution": COE_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "public documented charts; the HTML structure used by the parser is authored from the published "
                  "chart layout and must be verified against a real page",
    },
}
LICENCE_DECISIONS = {
    "untc": {"status": "declined", "recorded": "2026-09-30",
             "reference": "docs/development/treaties-evidence/source-audit.md#un-treaty-collection-licence-decision"},
    "cellar": {"status": "accepted", "recorded": "2026-09-30",
               "reference": "docs/development/treaties-evidence/source-audit.md#access-decisions"},
    "coe-treaty-office": {"status": "accepted", "recorded": "2026-09-30",
                          "reference": "docs/development/treaties-evidence/source-audit.md#access-decisions"},
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"],
               "note": ("not acquired: written permission required (TR01)" if contract["access_decision"] == "declined"
                        else "no dated live run from this runtime; offline fixtures only (TR13, #2645)")}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# TR01 data-minimisation decision (also docs/development/treaties-evidence/source-audit.md).
MINIMISATION = {
    "participants": "states, international organisations and the EU as the source names them; the source's "
                    "published codes only (e.g. CELLAR country authority codes)",
    "stored_verbatim": [("reservations, declarations, objections, withdrawals and denunciations as the depositary "
                        "published them"), "footnotes and notes as published", "titles as published"],
    "never_stored": ["names of signatories, representatives or officials as separate fields", "contact details",
                     "treaty full texts (linked, not reproduced)"],
    "person_text": "official texts may mention an office holder; they are kept verbatim, never parsed into person "
                   "records, never used as a query key or subscription target",
    "retention": "revisions are kept for as long as the namespace retains Legal records; corrections and removals "
                 "by the source are revisions, never deletions",
    "access": "knowledge:legal:read with namespace read access; identity review needs knowledge:legal:review",
}
# TR01 bounded first coverage: nothing implies complete coverage of any depositary.
BOUNDED_COVERAGE = {
    "untc": "the declared MTDSG treaties (at most 20 per source); fixtures: one fictional convention XXIX-99 "
            "adopted in 2098 - not acquired live under the declined licence decision",
    "cellar": "the declared EU international agreements (1-20 CELEX per source) and 1-24 languages; fixtures: one "
              "fictional agreement 22099A0101(01) with its concluding decision",
    "coe-treaty-office": "the declared CETS/ETS treaties (at most 20 per source) and every state or organisation "
                         "the chart lists; fixtures: one fictional convention CETS No. 990 (2098-2099)",
    "periods": "whatever period the declared pages cover; as-of answers use the published dates only",
}
UNTC_SUFFIXES = {"a": "accession", "d": "succession", "A": "acceptance", "AA": "approval", "c": "formal-confirmation"}
COE_NOTES = {"a": "accession", "su": "succession", "s": "definitive-signature"}
KNOWN_ORGANISATIONS = {"european union": "regional-economic-integration-organisation",
                       "european community": "regional-economic-integration-organisation",
                       "european atomic energy community": "regional-economic-integration-organisation"}
STATEMENT_KINDS = {"reservation": "reservation", "reservations": "reservation", "declaration": "declaration",
                   "declarations": "declaration", "objection": "objection", "objections": "objection",
                   "withdrawal": "withdrawal", "denunciation": "denunciation", "communication": "communication",
                   "notification": "communication"}
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}
_CITATIONS = (
    ("cets", re.compile(r"\b(?:CETS|ETS)\s+No\.?\s*(\d{1,4})\b")),
    ("unts-registration", re.compile(r"\bUNTS\s+(?:registration\s+)?No\.?\s*(\d{1,7})\b")),
    ("untc-mtdsg", re.compile(r"\bMTDSG\s+([IVXL]+-\d{1,3}(?:-[a-z])?)\b")),
)


class TreatiesFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def _verbatim(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    return text or None


def parse_date(value: Any) -> str | None:
    """ISO date of a published date ('1 Mar 2098', '1 March 2098', '01/03/2098', '30-09-2098', ISO); else None."""
    text = _clean(value) or ""
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    parts = None
    if match:
        parts = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    elif match := re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", text):
        parts = (int(match.group(3)), int(match.group(2)), int(match.group(1)))
    elif match := re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b", text):
        month = _MONTHS.get(match.group(2)[:3].casefold())
        parts = (int(match.group(3)), month, int(match.group(1))) if month else None
    if not parts:
        return None
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def citations_in(text: Any) -> list[dict[str, str]]:
    """Exact published catalogue citations in a text (CETS/ETS No., UNTS registration No., MTDSG number)."""
    out = []
    for scheme, pattern in _CITATIONS:
        for match in pattern.finditer(str(text or "")):
            item = {"scheme": scheme, "value": match.group(1), "as_written": match.group(0)}
            if item not in out:
                out.append(item)
    return out


def _participant(name: str, *, provider: str, kind: str | None = None, codes: Sequence[Mapping[str, str]] = ()
                 ) -> dict[str, Any]:
    stated = kind or KNOWN_ORGANISATIONS.get(name.casefold())
    return {"name_as_published": name, "key": participant_key(provider, name),
            "kind": stated or "state",
            "kind_basis": "stated by the source's section" if kind else (
                "the organisation as the source names it" if stated else "listed as a participant; no organisation "
                                                                          "is named"),
            "published_codes": [dict(c) for c in codes]}


def _record(fmt: str, kind: str, record_key: str, *, treaty: str, title: Any, locator: str, depositary_revision: Any,
            depositary_date: str | None, fields: Mapping[str, Any], published_at: Any = None) -> dict[str, Any]:
    spec = FORMATS[fmt]
    record = {"contract": RECORD_CONTRACT, "format": fmt, "provider": spec["provider"], "record_kind": kind,
              "record_key": record_key, "treaty_key": treaty, "title": _clean(title) or record_key,
              "locator": locator, "depositary_revision": _clean(depositary_revision),
              "depositary_date": depositary_date, "published_at": parse_date(published_at) if published_at else None,
              "fields": dict(fields)}
    try:
        return validate_record(record)
    except TreatiesError as exc:
        raise TreatiesFormatError("schema_drift" if exc.code != "minimisation_violation" else exc.code,
                                  str(exc)) from exc


def _soup(raw: bytes):
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    except UnicodeDecodeError as exc:
        raise TreatiesFormatError("schema_drift", "page is not UTF-8") from exc


def _cell_text(cell) -> tuple[str, list[str]]:
    """A cell's text without its footnote markers, and the markers."""
    markers = []
    for sup in cell.find_all("sup"):
        marker = _clean(sup.get_text())
        if marker:
            markers.append(marker)
        sup.extract()
    return _clean(cell.get_text(" ")) or "", markers


def _action(fmt, native, treaty, participant, action_type, qualifier, *, locator, revision, revision_date,
            as_published, action_date=None, deposit_date=None, effective_date=None, text=None, anchor=None,
            footnotes=(), extra=None):
    fields = {"participant": participant, "action_type": action_type, "action_type_as_published": as_published,
              "action_date": action_date, "deposit_date": deposit_date, "effective_date": effective_date,
              "date_status": "as published" if (action_date or deposit_date or effective_date) else "undated",
              "text": text, "text_anchor": anchor, "footnotes": list(footnotes), **dict(extra or {})}
    key = action_key(FORMATS[fmt]["provider"], native, participant["name_as_published"], action_type, qualifier)
    return _record(fmt, "treaty-action", key, treaty=treaty,
                   title=f"{participant['name_as_published']}: {as_published}", locator=locator,
                   depositary_revision=revision, depositary_date=revision_date, fields=fields)


# ----------------------------------------------------------------- UN Treaty Collection


def parse_untc(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One UNTC status page into a treaty record and its participant actions (authored shape; verify live)."""
    fmt = "untc-status-html"
    soup = _soup(responses["status"])
    native = str(unit["mtdsg_no"])
    locator = f"{UNTC_SITE}/Pages/ViewDetails.aspx?" + urlencode(
        {"mtdsg_no": native, "chapter": unit["chapter"], "clang": "_en"})
    stamp_node = soup.find(id="statusAsAt")
    title_node = soup.find(id="treatyTitle")
    if stamp_node is None or title_node is None:
        raise TreatiesFormatError("schema_drift", "the UNTC page has no 'Status as at' stamp or treaty title")
    stamp = _clean(stamp_node.get_text(" "))
    revision_date = parse_date(stamp)
    title = re.sub(r"^\d+[a-z]?\.\s*", "", _clean(title_node.get_text(" ")) or "")
    header: dict[str, str] = {}
    details = soup.find(id="treatyDetails")
    for row in details.find_all("tr") if details else []:
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            header[(_clean(cells[0].get_text()) or "").rstrip(":").casefold()] = _clean(cells[1].get_text(" ")) or ""
    notes = []
    notes_node = soup.find(id="notes")
    for item in notes_node.find_all("li") if notes_node else []:
        marker, text = None, _verbatim(item.get_text(" "))
        sup = item.find("sup")
        if sup is not None:
            marker = _clean(sup.get_text())
            sup.extract()
            text = _verbatim(item.get_text(" "))
        notes.append({"marker": marker, "text": text, "anchor": {"section": "Notes", "id": item.get("id")}})
    by_marker = {n["marker"]: n for n in notes if n["marker"]}
    table = soup.find(id="participants")
    if table is None:
        raise TreatiesFormatError("schema_drift", "the UNTC page has no participant table")
    rows = table.find_all("tr")
    headings = [(_clean(c.get_text(" ")) or "").casefold() for c in rows[0].find_all(["th", "td"])] if rows else []
    if not headings or headings[0] != "participant":
        raise TreatiesFormatError("schema_drift", "the participant table does not start with 'Participant'")
    treaty = treaty_key("untc", native)
    actions: list[dict[str, Any]] = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if not cells:
            continue
        name, markers = _cell_text(cells[0])
        if not name:
            continue
        participant = _participant(name, provider="untc")
        footnotes = [by_marker[m] for m in markers if m in by_marker]
        for heading, cell in zip(headings[1:], cells[1:]):
            text, cell_markers = _cell_text(cell)
            if not text:
                continue
            cell_notes = footnotes + [by_marker[m] for m in cell_markers if m in by_marker]
            suffix = re.search(r"\s(AA|A|a|d|c)$", text)
            if heading.startswith("signature"):
                action_type, label = "signature", "Signature"
            elif heading.startswith("ratification"):
                action_type = UNTC_SUFFIXES[suffix.group(1)] if suffix else "ratification"
                label = f"{action_type.replace('-', ' ').capitalize()} ({suffix.group(1)})" if suffix else "Ratification"
            elif "denunciation" in heading:
                action_type, label = "denunciation", "Denunciation"
            elif "withdrawal" in heading:
                action_type, label = "withdrawal", "Withdrawal"
            else:
                continue
            actions.append(_action(fmt, native, treaty, participant, action_type, "table", locator=locator,
                                   revision=stamp, revision_date=revision_date, as_published=label,
                                   action_date=parse_date(text), extra={"date_as_published": text,
                                                                        "column_as_published": heading},
                                   anchor={"section": "participants", "participant": name, "column": heading},
                                   footnotes=cell_notes))
    statements: dict[str, dict[str, Any]] = {}
    for section_node in soup.find_all(["h2", "h3"]):
        section = _clean(section_node.get_text(" ")) or ""
        lowered = section.casefold()
        if not (lowered.startswith(("declarations", "reservations", "objections"))):
            continue
        default = "objection" if lowered.startswith("objections") else "declaration-or-reservation"
        section_note = None
        for sibling in section_node.find_next_siblings():
            if sibling.name in {"h2", "h3"}:
                break
            if "note" in (sibling.get("class") or []):
                section_note = _verbatim(sibling.get_text(" "))
                continue
            if "participant-block" not in (sibling.get("class") or []):
                continue
            name_node = sibling.find(class_="participant")
            name = _clean(name_node.get_text(" ")) if name_node else None
            if not name:
                raise TreatiesFormatError("schema_drift", f"a block under {section!r} names no participant")
            participant = _participant(name, provider="untc")
            stated_date = None
            ordinal: dict[tuple[str, str], int] = {}
            for paragraph in sibling.find_all("p"):
                if paragraph is name_node:
                    continue
                if "date" in (paragraph.get("class") or []):
                    stated_date = parse_date(paragraph.get_text(" "))
                    continue
                label_node = paragraph.find(["em", "i"])
                label = _clean(label_node.get_text(" ")).rstrip(":") if label_node else None
                action_type = STATEMENT_KINDS.get((label or "").casefold(), default)
                link = paragraph.find("a", href=re.compile(r"^#"))
                text = _verbatim(paragraph.get_text())
                qualifier_date = stated_date or "undated"
                n = ordinal[(action_type, qualifier_date)] = ordinal.get((action_type, qualifier_date), 0) + 1
                record = _action(
                    fmt, native, treaty, participant, action_type, f"{qualifier_date}:{n}", locator=locator,
                    revision=stamp, revision_date=revision_date, as_published=label or section,
                    action_date=stated_date, text=text,
                    anchor={"section": section, "participant": name, "paragraph": n, "id": paragraph.get("id")},
                    extra={"section_note": section_note,
                           "withdraws": "a statement published in this section (see the text)"
                           if action_type == "withdrawal" else None,
                           "objected": {"anchor": link["href"][1:], "action_key": None,
                                        "basis": "the source links this objection to that anchor"}
                           if (link and action_type == "objection") else None})
                actions.append(record)
                if paragraph.get("id"):
                    statements[paragraph["id"]] = record
    for record in actions:
        objected = record["fields"].get("objected")
        if objected and objected["anchor"] in statements:
            objected["action_key"] = statements[objected["anchor"]]["record_key"]
    registration = header.get("registration", "")
    identifiers = [{"scheme": "untc-mtdsg", "value": native}, {"scheme": "untc-chapter", "value": str(unit["chapter"])}]
    unts = re.search(r"No\.\s*(\d+)", registration)
    if unts:
        identifiers.append({"scheme": "unts-registration", "value": unts.group(1)})
    fields = {
        "identifiers": identifiers, "title_as_published": title,
        "depositary": {"name": "Secretary-General of the United Nations",
                       "basis": "the collection lists multilateral treaties deposited with the Secretary-General"},
        "adoption": {"place": header.get("place"), "date": parse_date(header.get("date")),
                     "date_as_published": header.get("date")},
        "entry_into_force": {"date": parse_date(header.get("entry into force")),
                             "as_published": header.get("entry into force") or None},
        "registration_as_published": registration or None, "status_as_published": header.get("status") or None,
        "text_reference_as_published": header.get("text") or None,
        "text_policy": "linked, not stored", "text_url": locator, "footnotes": notes,
        "cross_references": citations_in(title), "action_keys": sorted(a["record_key"] for a in actions),
        "attribution": UNTC_ATTRIBUTION,
    }
    return [_record(fmt, "treaty", treaty, treaty=treaty, title=title, locator=locator, depositary_revision=stamp,
                    depositary_date=revision_date, fields=fields, published_at=header.get("date")), *actions]


# ----------------------------------------------------------------- Council of Europe Treaty Office


def parse_coe(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A Treaty Office chart and declarations page into a treaty record and its actions (authored shape; verify)."""
    fmt = "coe-chart-html"
    native = str(int(unit["number"])).zfill(3)
    chart = _soup(responses["chart"])
    locator = f"{COE_SITE}/full-list?module=signatures-by-treaty&treatynum={native}"
    decl_locator = f"{COE_SITE}/full-list?module=declarations-by-treaty&treatynum={native}"
    stamp_node, title_node = chart.find(id="status-as-of"), chart.find(id="treaty-title")
    if stamp_node is None or title_node is None:
        raise TreatiesFormatError("schema_drift", "the chart has no 'Status as of' date or treaty title")
    stamp = _clean(stamp_node.get_text(" "))
    revision_date = parse_date(stamp)
    title = _clean(title_node.get_text(" "))
    reference = _clean((chart.find(id="treaty-reference") or title_node).get_text(" "))
    stated = citations_in(reference)
    if not any(c["scheme"] == "cets" and c["value"].zfill(3) == native for c in stated):
        raise TreatiesFormatError("schema_drift", "the chart is not the requested CETS/ETS treaty")
    details: dict[str, list[str]] = {}
    table = chart.find(id="treaty-details")
    for row in table.find_all("tr") if table else []:
        cells = [_clean(c.get_text(" ")) or "" for c in row.find_all(["td", "th"])]
        if cells:
            details[cells[0].casefold()] = cells[1:]

    def detail(row: str, label: str) -> str | None:
        for value in details.get(row, []):
            if value.casefold().startswith(label.casefold() + ":"):
                return _clean(value.split(":", 1)[1])
        return None

    treaty = treaty_key("coe-treaty-office", native)
    actions: list[dict[str, Any]] = []
    for heading in chart.find_all("h3"):
        section = _clean(heading.get_text(" ")) or ""
        lowered = section.casefold()
        kind = ("international-organisation" if "organisation" in lowered else "state") if (
            "member" in lowered or "organisation" in lowered or "states" in lowered) else None
        grid = heading.find_next_sibling("table")
        if kind is None or grid is None:
            continue
        rows = grid.find_all("tr")
        columns = [(_clean(c.get_text(" ")) or "").casefold().rstrip(".") for c in rows[0].find_all(["th", "td"])]
        if not columns or columns[0] not in {"states", "state", "international organisations", "organisations"}:
            raise TreatiesFormatError("schema_drift", f"the chart table under {section!r} has no States column")
        for row in rows[1:]:
            cells = [_clean(c.get_text(" ")) or "" for c in row.find_all("td")]
            if not cells or not cells[0]:
                continue
            values = dict(zip(columns, cells))
            name = cells[0]
            org_kind = KNOWN_ORGANISATIONS.get(name.casefold()) if kind != "state" else None
            participant = _participant(name, provider="coe-treaty-office", kind=org_kind or kind)
            participant["section_as_published"] = section
            notes = [n for n in re.split(r"[\s,;]+", values.get("notes", "")) if n]
            anchor = {"section": section, "participant": name}
            common = {"locator": locator, "revision": stamp, "revision_date": revision_date}
            if values.get("signature"):
                action_type = "definitive-signature" if "s" in notes else "signature"
                actions.append(_action(fmt, native, treaty, participant, action_type, "chart", **common,
                                       as_published="Signature" + (" (s)" if "s" in notes else ""),
                                       action_date=parse_date(values["signature"]), anchor={**anchor,
                                                                                            "column": "signature"},
                                       extra={"date_as_published": values["signature"], "notes_as_published": notes}))
            if values.get("ratification"):
                consent = next((COE_NOTES[n] for n in notes if n in COE_NOTES and n != "s"), "ratification")
                actions.append(_action(fmt, native, treaty, participant, consent, "chart", **common,
                                       as_published="Ratification" + (f" ({notes[0]})" if consent != "ratification"
                                                                      else ""),
                                       action_date=parse_date(values["ratification"]),
                                       deposit_date=parse_date(values["ratification"]),
                                       effective_date=parse_date(values.get("entry into force")),
                                       anchor={**anchor, "column": "ratification"},
                                       extra={"date_as_published": values["ratification"],
                                              "notes_as_published": notes}))
            if values.get("entry into force"):
                actions.append(_action(fmt, native, treaty, participant, "entry-into-force", "chart", **common,
                                       as_published="Entry into Force",
                                       action_date=parse_date(values["entry into force"]),
                                       effective_date=parse_date(values["entry into force"]),
                                       anchor={**anchor, "column": "entry into force"},
                                       extra={"date_as_published": values["entry into force"]}))
            if values.get("denunciation"):
                actions.append(_action(fmt, native, treaty, participant, "denunciation", "chart", **common,
                                       as_published="Denunciation", action_date=parse_date(values["denunciation"]),
                                       anchor={**anchor, "column": "denunciation"},
                                       extra={"date_as_published": values["denunciation"]}))
            flags = {k: bool(values.get(k)) for k in ("r", "d", "a", "t", "c", "o") if k in values}
            if flags and actions and actions[-1]["fields"]["participant"]["key"] == participant["key"]:
                for item in actions:
                    if item["fields"]["participant"]["key"] == participant["key"]:
                        item["fields"]["chart_flags_as_published"] = flags
    ordinal: dict[tuple[str, str, str], int] = {}
    statements = _soup(responses["declarations"]).find_all(class_="declaration")
    for block in statements:
        kind_node, state_node = block.find(class_="declaration-kind"), block.find(class_="declaration-state")
        if kind_node is None or state_node is None:
            raise TreatiesFormatError("schema_drift", "a declaration block has no kind or state line")
        kind_line = _clean(kind_node.get_text(" ")) or ""
        name = _clean(state_node.get_text(" ")) or ""
        action_type = STATEMENT_KINDS.get(kind_line.split(" ", 1)[0].casefold())
        if action_type is None:
            raise TreatiesFormatError("schema_drift", f"unknown declaration kind {kind_line!r}")
        deposited = re.search(r"(?:deposited|registered at the Secretariat General|dated)\s+on\s+(.+?)(?:\s+-\s+|$)",
                              kind_line)
        deposit_date = parse_date(deposited.group(1)) if deposited else None
        period = block.find(class_="declaration-period")
        period_text = _clean(period.get_text(" ")) if period else None
        period_dates = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", period_text or "")
        articles = block.find(class_="declaration-articles")
        text_node = block.find(class_="declaration-text")
        text = _verbatim(text_node.get_text("\n")) if text_node else None
        participant = _participant(name, provider="coe-treaty-office",
                                   kind=KNOWN_ORGANISATIONS.get(name.casefold()) or "state")
        qualifier = deposit_date or "undated"
        n = ordinal[(name, action_type, qualifier)] = ordinal.get((name, action_type, qualifier), 0) + 1
        withdraws = None
        if action_type == "withdrawal":
            target = re.search(r"withdrawal of (?:the |a )?(\w+)", kind_line, re.IGNORECASE)
            withdraws = (target.group(1).casefold().rstrip("s") if target else None)
        actions.append(_action(
            fmt, native, treaty, participant, action_type, f"{qualifier}:{n}", locator=decl_locator, revision=stamp,
            revision_date=revision_date, as_published=kind_line, action_date=deposit_date, deposit_date=deposit_date,
            effective_date=parse_date(period_dates[0]) if period_dates else None, text=text,
            anchor={"section": "declarations", "participant": name, "id": block.get("id")},
            extra={"period_covered_as_published": period_text,
                   "period_end": parse_date(period_dates[1]) if len(period_dates) > 1 else None,
                   "articles_as_published": _clean(articles.get_text(" ").split(":", 1)[-1]) if articles else None,
                   "withdraws": withdraws, "objected": None}))
    fields = {
        "identifiers": [{"scheme": "cets", "value": native}], "title_as_published": title,
        "reference_as_published": reference,
        "depositary": {"name": "Secretary General of the Council of Europe",
                       "basis": "the Treaty Office charts treaties of the Council of Europe Treaty Series"},
        "adoption": {"place": detail("opening of the treaty", "place"),
                     "date": parse_date(detail("opening of the treaty", "date")),
                     "date_as_published": detail("opening of the treaty", "date"),
                     "event_as_published": "Opening of the treaty"},
        "entry_into_force": {"date": parse_date(detail("entry into force", "date")),
                             "conditions_as_published": detail("entry into force", "conditions"),
                             "as_published": "; ".join(details.get("entry into force", [])) or None},
        "text_policy": "linked, not stored", "text_url": f"{COE_SITE}/full-list?module=treaty-detail&treatynum={native}",
        "footnotes": [], "cross_references": [c for c in stated if c["scheme"] != "cets"],
        "action_keys": sorted(a["record_key"] for a in actions), "attribution": COE_ATTRIBUTION,
    }
    return [_record(fmt, "treaty", treaty, treaty=treaty, title=title, locator=locator, depositary_revision=stamp,
                    depositary_date=revision_date, fields=fields,
                    published_at=detail("opening of the treaty", "date")), *actions]


# ----------------------------------------------------------------- CELLAR (EU international agreements)

CDM = "http://publications.europa.eu/ontology/cdm#"
DEFAULT_RELATIONS = (CDM + "work_cites_work",)


def agreement_query(celex_ids: Sequence[str], relations: Sequence[str], *, limit: int) -> str:
    """The bounded agreement query: signature date and the acts CELLAR links to each selected agreement."""
    values = " ".join(json.dumps(v) + "^^<http://www.w3.org/2001/XMLSchema#string>" for v in celex_ids)
    filters = ", ".join(f"<{r}>" for r in relations)
    return (
        "PREFIX cdm: <" + CDM + ">\n"
        "SELECT DISTINCT ?celex ?signature ?direction ?relation ?actCelex ?actDate WHERE {\n"
        " VALUES ?celex { " + values + " }\n"
        " ?work cdm:resource_legal_id_celex ?celex .\n"
        " OPTIONAL { ?work cdm:resource_legal_date_signature ?signature }\n"
        " OPTIONAL {\n"
        "  { ?act ?relation ?work . BIND(\"act-to-agreement\" AS ?direction) } UNION\n"
        "  { ?work ?relation ?act . BIND(\"agreement-to-act\" AS ?direction) }\n"
        "  FILTER (?relation IN (" + filters + "))\n"
        "  ?act cdm:resource_legal_id_celex ?actCelex .\n"
        "  OPTIONAL { ?act cdm:work_date_document ?actDate }\n"
        " }\n"
        "}\nORDER BY ?celex ?actCelex ?relation ?direction ?signature ?actDate\nLIMIT " + str(int(limit))
    )


def _bindings(raw: bytes, what: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        rows = payload["results"]["bindings"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise TreatiesFormatError("schema_drift", f"{what} is not a SPARQL JSON result") from exc
    if not isinstance(rows, list):
        raise TreatiesFormatError("schema_drift", f"{what} has no bindings list")
    return [{k: dict(v).get("value") for k, v in row.items() if isinstance(v, Mapping)} for row in rows]


def parse_cellar(legal_records: Sequence[Mapping[str, Any]], agreement_raw: bytes, selection: Mapping[str, Any],
                 *, retrieved: str | None) -> list[dict[str, Any]]:
    """Group the CELLAR adapter's expression records per CELEX and add the agreement facts and linked acts."""
    fmt = "cellar-agreement-sparql"
    grouped: dict[str, dict[str, Any]] = {}
    for record in legal_records:
        fields = dict(record["fields"])
        entry = grouped.setdefault(fields["celex"], {"work": fields["work"], "expressions": {}, "eli": set(),
                                                     "effective": set(), "document": set(), "published": set(),
                                                     "relations": []})
        language = str(fields.get("language_identity") or "").rsplit("/", 1)[-1] or "und"
        expression = entry["expressions"].setdefault(fields["expression"], {
            "language": language, "expression": fields["expression"], "title_as_published": _clean(record["title"]),
            "manifestations": []})
        if fields.get("manifestation"):
            item = {"manifestation": fields["manifestation"], "format": fields.get("format"), "item": fields.get("item")}
            if item not in expression["manifestations"]:
                expression["manifestations"].append(item)
        entry["eli"] |= set(fields.get("eli_identifiers") or [])
        entry["effective"] |= set(fields.get("effective_dates") or [])
        entry["document"] |= set(fields.get("document_dates") or [])
        entry["published"] |= set(fields.get("publication_dates") or [])
        for relation in record.get("relationships") or []:
            if relation not in entry["relations"]:
                entry["relations"].append(dict(relation))
    facts: dict[str, dict[str, Any]] = {}
    for row in _bindings(agreement_raw, "the agreement query"):
        if row.get("celex") not in selection["agreements"]:
            raise TreatiesFormatError("schema_drift", "an agreement row is not a selected CELEX")
        entry = facts.setdefault(row["celex"], {"signature": set(), "acts": []})
        if row.get("signature"):
            entry["signature"].add(row["signature"][:10])
        if row.get("actCelex"):
            act = {"celex": row["actCelex"], "relation": row.get("relation"), "direction": row.get("direction"),
                   "document_date": parse_date(row.get("actDate")),
                   "url": EURLEX_SITE + row["actCelex"],
                   "basis": "CELLAR states this relation between the act and the agreement; its role is not inferred"}
            if act not in entry["acts"]:
                entry["acts"].append(act)
    out = []
    for celex in selection["agreements"]:
        if celex not in grouped:
            continue  # not published in the selected languages: reported by the receipt, never invented
        entry, fact = grouped[celex], facts.get(celex, {"signature": set(), "acts": []})
        expressions = sorted(entry["expressions"].values(), key=lambda e: (e["language"], e["expression"]))
        for expression in expressions:
            expression["manifestations"].sort(key=lambda m: (str(m["format"]), str(m["manifestation"])))
        english = next((e for e in expressions if e["language"] == "ENG"), expressions[0])
        title = english["title_as_published"] or celex
        treaty = treaty_key("cellar", celex)
        signature = sorted(fact["signature"])
        identifiers = [{"scheme": "celex", "value": celex}, {"scheme": "cellar-work", "value": entry["work"]}] + [
            {"scheme": "eli", "value": eli} for eli in sorted(entry["eli"])]
        cross = []
        for expression in expressions:
            for citation in citations_in(expression["title_as_published"]):
                item = {**citation, "basis": f"citation in the published title ({expression['language']} expression)"}
                if not any(c["scheme"] == item["scheme"] and c["value"] == item["value"] for c in cross):
                    cross.append(item)
        fields = {
            "identifiers": identifiers, "title_as_published": title,
            "depositary": {"name": None, "basis": "CELLAR states no depositary in the bounded query"},
            "adoption": {"place": None, "date": None, "document_dates": sorted(entry["document"])},
            "signature": {"dates": signature, "date": signature[0] if len(signature) == 1 else None,
                          "basis": "cdm:resource_legal_date_signature as CELLAR states it"},
            "conclusion": {"date": None, "basis": "CELLAR states no conclusion date property in the bounded query; "
                                                  "the linked acts are cited with their own document dates"},
            "entry_into_force": {"dates": sorted(entry["effective"]),
                                 "date": min(entry["effective"]) if len(entry["effective"]) == 1 else None,
                                 "basis": "cdm:resource_legal_date_entry-into-force as CELLAR states it (several "
                                          "dates are kept, never collapsed)"},
            "publication_dates": sorted(entry["published"]),
            "expressions": expressions, "eu_acts": sorted(fact["acts"], key=lambda a: (a["celex"], str(a["relation"]),
                                                                                    str(a["direction"]))),
            "cellar_relations": sorted(entry["relations"], key=lambda r: (str(r["relation"]), str(r["target"]))),
            "text_policy": "linked, not stored", "text_url": EURLEX_SITE + celex, "footnotes": [],
            "cross_references": cross, "action_keys": [], "attribution": CELLAR_ATTRIBUTION,
        }
        out.append(_record(fmt, "treaty", treaty, treaty=treaty, title=title, locator=EURLEX_SITE + celex,
                           depositary_revision=None, depositary_date=None, fields=fields,
                           published_at=min(entry["published"]) if entry["published"] else None))
    del retrieved
    return out


# ----------------------------------------------------------------- selection and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt == "cellar-agreement-sparql":
        celex = list(selection.get("agreements") or [])
        languages = list(selection.get("languages") or [])
        if not 1 <= len(celex) <= MAX_UNITS or any(not re.fullmatch(r"2\d{4}[A-Z]\d{4}(\(\d{2}\))?", c) for c in celex):
            raise SourcePackError("invalid_manifest", f"CELLAR agreement selections name 1-{MAX_UNITS} sector-2 CELEX")
        if not 1 <= len(languages) <= 24 or any(not re.fullmatch(r"[A-Z]{3}", lang) for lang in languages):
            raise SourcePackError("invalid_manifest", "CELLAR agreement selections name 1-24 three-letter languages")
        for relation in selection.get("act_relations") or []:
            if not re.fullmatch(r"http://publications\.europa\.eu/ontology/cdm#[A-Za-z0-9_-]{3,120}", str(relation)):
                raise SourcePackError("invalid_manifest", "act relations are CDM property IRIs")
        return [{"agreements": celex, "languages": languages}]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get("treaties") or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a treaties selection names 1-{MAX_UNITS} treaties")
    for unit in units:
        if fmt == "untc-status-html" and (not re.fullmatch(r"[IVXL]+-\d{1,3}(-[a-z])?", str(unit.get("mtdsg_no")))
                                          or not str(unit.get("chapter") or "").isdigit()):
            raise SourcePackError("invalid_manifest", "UNTC units name mtdsg_no (e.g. XVIII-10) and chapter")
        if fmt == "coe-chart-html" and not re.fullmatch(r"\d{1,4}", str(unit.get("number") or "")):
            raise SourcePackError("invalid_manifest", "Council of Europe units name a CETS/ETS number")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one UNTC or Council of Europe unit."""
    if fmt == "untc-status-html":
        return {"status": ("/Pages/ViewDetails.aspx", {"mtdsg_no": unit["mtdsg_no"], "chapter": unit["chapter"],
                                                       "clang": "_en"})}
    if fmt == "coe-chart-html":
        number = str(int(unit["number"])).zfill(3)
        return {"chart": ("/full-list", {"module": "signatures-by-treaty", "treatynum": number}),
                "declarations": ("/full-list", {"module": "declarations-by-treaty", "treatynum": number})}
    raise SourcePackError("invalid_manifest", f"{fmt} is not a page-per-unit format")


def treaties_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("treaties") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "treaties sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live", "declined"}:
        raise SourcePackError("invalid_manifest", "treaties sources state their LIVE_VERIFICATION status")
    decision = dict(declared.get("licence_decision") or {})
    if decision.get("status") not in {"accepted", "declined"} or not decision.get("reference"):
        raise SourcePackError("invalid_manifest", "treaties sources reference their TR01 licence decision")
    if (decision["status"] == "declined") != (declared["live_verification"] == "declined"):
        raise SourcePackError("invalid_manifest", "a declined licence decision is the declined live status")
    _units(fmt, dict(declared.get("selection") or {}))
    return declared


class TreatiesAdapter:
    """Fetch one declared unit per page (UNTC, Council of Europe) or the agreement selection (CELLAR)."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret
        self.source = json.loads(json.dumps(source))
        self.declared = treaties_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.selection = dict(self.declared.get("selection") or {})
        self.units = _units(self.format, self.selection)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "treaties": {"provider": self.provider, "format": self.format, "units": len(self.units),
                         "licence_decision": dict(self.declared["licence_decision"])},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "treaties runs fetch the declared selection only")
        decision = self.declared["licence_decision"]
        if decision["status"] != "accepted":
            raise SourcePackError("licence_declined", f"{self.provider} is not acquired under the recorded licence "
                                                      f"decision ({decision['reference']})")

    def _get(self, path: str, params: Mapping[str, Any], *, url: str | None = None,
             headers: Mapping[str, str] | None = None) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = url or endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        if (urlsplit(url).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "treaties sources fetch only from their declared host")
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered, headers=dict(headers or {"Accept": "text/html"}),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "treaties response was served from another host")
        status = int(response.get("status", 200))
        headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        name = "sparql" if "query" in ordered else path + ("?" + urlencode(ordered) if ordered else "")
        return raw, {"path": name, "status": status, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def _cellar(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Reuse the Legal pack's CELLAR adapter for the expression rows, then read the agreement facts."""
        from src.ingestion.legal_sources import CellarLegalAdapter

        requests: list[dict[str, Any]] = []
        origins: list[str] = []

        def recording(**kwargs):
            response = self.transport(**kwargs)
            origins.append("fixture" if response.get("origin") == "fixture" else "live")
            return response

        cellar_source = {**self.source, "connector": "cellar",
                         "legal": {"selection": {"celex": self.selection["agreements"],
                                                 "languages": self.selection["languages"],
                                                 "page_size": int(self.selection.get("page_size") or 100)}}}
        base = CellarLegalAdapter(cellar_source, transport=recording)
        legal_records, cursor = [], None
        for page_no in range(int(self.source["budgets"]["max_pages"])):
            page = base.fetch_page({"operation": min(self.source["operations"]), "parameters": {},
                                    "limit": int(self.source["budgets"]["max_results"])}, cursor=cursor)
            legal_records += [dict(item["legal_record"]) for item in page.records]
            requests.append({"name": f"expressions:{page_no}", "path": "sparql", "status": page.receipt["status"],
                             "sha256": page.receipt["response_sha256"], "query_sha256": page.receipt["query_sha256"],
                             "bytes": int(page.bytes_read), "origin": origins[-1] if origins else "live"})
            cursor = page.next_cursor
            if cursor is None:
                break
        else:
            raise SourcePackError("budget_exhausted", "the CELLAR expression rows need more pages than the budget")
        relations = list(DEFAULT_RELATIONS) + [r for r in self.selection.get("act_relations") or []
                                               if r not in DEFAULT_RELATIONS]
        query = agreement_query(self.selection["agreements"], relations,
                                limit=int(self.selection.get("page_size") or 100))
        raw, receipt = self._get("", {"query": query, "format": "application/sparql-results+json"},
                                 url=self.source["endpoint"], headers={"Accept": "application/sparql-results+json"})
        if len(_bindings(raw, "the agreement query")) >= int(self.selection.get("page_size") or 100):
            raise SourcePackError("budget_exhausted", "the agreement query filled its page; the unit is not truncated")
        requests.append({"name": "agreement", **receipt, "query_sha256": _digest(query)})
        try:
            records = parse_cellar(legal_records, raw, self.selection, retrieved=None)
        except TreatiesFormatError as exc:
            raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        return records, requests

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        if self.format == "cellar-agreement-sparql":
            records, requests = self._cellar()
        else:
            responses, requests = {}, []
            for name, (path, params) in requests_for(self.format, unit).items():
                raw, receipt = self._get(path, params)
                responses[name] = raw
                requests.append({"name": name, **receipt})
            try:
                records = (parse_untc if self.format == "untc-status-html" else parse_coe)(responses, unit)
            except TreatiesFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": RECEIPT_CONTRACT, "source_id": self.source["source_id"], "provider": self.provider,
            "format": self.format, "unit_index": index, "unit": unit, "requests": requests, "records": len(records),
            "treaties": sorted({r["treaty_key"] for r in records}), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"],
            "licence_decision": dict(self.declared["licence_decision"]), "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({"id": record["record_key"], "title": record["title"], "url": record["locator"],
                        "language": "en", "published_at": record["published_at"],
                        "updated_at": record["depositary_date"],
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "treaty_record": record, "treaty_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(int(r["bytes"]) for r in requests), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: TreatiesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); CELLAR queries by kind and offset."""
    by_key = {page["request"]: page for page in pages}

    def key_of(url: str, params: Mapping[str, Any]) -> str:
        parts = urlsplit(url)
        query = str(params.get("query") or "")
        if query:
            if "resource_legal_date_signature" in query:
                return parts.path + "#agreement"
            offset = re.search(r"OFFSET (\d+)\s*$", query)
            return parts.path + f"#expressions@{offset.group(1) if offset else 0}"
        encoded = urlencode(sorted(dict(params or {}).items()))
        return parts.path + ("?" + encoded if encoded else "")

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = key_of(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = TreatiesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    for _ in range(len(adapter.units)):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "FORMATS", "LICENCE_DECISIONS",
    "LIVE_VERIFICATION", "MINIMISATION", "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY",
    "TreatiesAdapter", "TreatiesFormatError", "agreement_query", "citations_in", "fixture_transport", "parse_cellar",
    "parse_coe", "parse_date", "parse_untc", "replay_native_fixture", "requests_for", "treaties_declaration",
]

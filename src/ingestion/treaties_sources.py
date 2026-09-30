"""UN Treaty Collection, EU international agreements (CELLAR) and Council of Europe treaty acquisition (#2581).

One native connector, ``treaties``, reads a bounded, declared selection of
treaties from one depositary or catalogue per source and emits
``noesis-treaty-record-v1`` records exactly as that source published them
(TR01 audit: ``docs/development/treaties-evidence/source-audit.md``):

* ``untc-status-html`` - the UN Treaty Collection status page of a
  multilateral treaty deposited with the Secretary-General, keyed by UNTC
  chapter and treaty number (``mtdsg_no``, e.g. ``XXVII-7``): the treaty
  record (title, adoption, entry-into-force conditions, UNTS registration, the
  "status as at" stamp), one participant record per participant as published,
  one treaty-action record per participant and action (signature, definitive
  signature, ratification, acceptance, approval, accession, succession) with
  the column heading and suffix verbatim, and one treaty-statement record per
  declaration, reservation, objection and footnote, verbatim with its anchor
  and linked to the action (and, for an objection, to the objected statement
  where the page links it);
* ``cellar-agreement-sparql`` - an EU international agreement from the CELLAR
  SPARQL endpoint, keyed by CELEX (and ELI where published). The work and its
  language expressions are read with the Legal pack's reviewed CELLAR query
  (:func:`src.ingestion.regional_providers.cellar_query` and
  :func:`~src.ingestion.regional_providers.parse_cellar_results`); a second
  bounded query reads the agreement's dates as CELLAR states them, its
  contracting parties (authority-table codes) and the EU acts that point at it
  (predicate verbatim), recorded as citations. Language versions are
  ``treaty-expression`` records, never separate treaties;
* ``coe-treaty-html`` - the Council of Europe Treaty Office chart of
  signatures and ratifications and the declarations list of one treaty, keyed
  by CETS/ETS number and state (member or non-member as published):
  signatures, ratifications, entry into force per state, denunciations kept as
  actions (never deletions) and reservations, declarations and objections
  verbatim with their dates.

The HTML element structure the parsers read is the one recorded in the
authored fixtures and **must be verified live** (``LIVE_VERIFICATION``); no
dated live run has been made from this runtime.

**Minimisation (TR01).** Participants are states, international organisations
and the EU. No field names a natural person: signatory, plenipotentiary and
contact names are never extracted into a field, a key, a participant or a
match; any such field is rejected by :func:`minimisation_violations` here and
again by the store at write time. Verbatim statements are the depositary's
publication of a state's act and are kept as published; they are never indexed
by person.

A unit is all-or-nothing: a page larger than the declared limits is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest. Nothing here gives legal advice, infers obligations or compliance,
interprets the legal effect of a reservation or redistributes treaty text
(texts are referenced by link only).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-treaty-record-v1"
MINIMISATION_POLICY = "treaties-minimisation-v1"
CONNECTOR = "treaties"
MAX_UNITS = 20
MAX_PARTICIPANTS = 250
MAX_STATEMENTS = 1000
REVIEW_BOUNDARY = ("Records are what each depositary or catalogue published. No legal advice, no inference of "
                   "obligations or compliance, no interpretation of the legal effect of reservations, and no "
                   "treaty-text redistribution beyond what each source licenses (texts are linked, not mirrored).")
UN_ATTRIBUTION = "Source: United Nations Treaty Collection (treaties.un.org), status as published by the depositary."
EU_ATTRIBUTION = "Source: Publications Office of the European Union, CELLAR (reuse under Commission Decision " \
                 "2011/833/EU)."
COE_ATTRIBUTION = "Source: Council of Europe Treaty Office (www.coe.int/conventions)."

FORMATS: dict[str, dict[str, Any]] = {
    "untc-status-html": {"provider": "untc", "unit": "treaties", "keyed": False, "depositary": "UN Secretary-General"},
    "cellar-agreement-sparql": {"provider": "eu-cellar", "unit": "agreements", "keyed": False,
                                "depositary": "as stated per agreement"},
    "coe-treaty-html": {"provider": "coe-treaty-office", "unit": "treaties", "keyed": False,
                        "depositary": "Secretary General of the Council of Europe"},
}
PROVIDERS = tuple(sorted({spec["provider"] for spec in FORMATS.values()}))
# provider -> the segment its record keys carry (treaties:<segment>:...)
KEY_SEGMENT = {"untc": "untc", "eu-cellar": "eu-cellar", "coe-treaty-office": "coe"}
RECORD_KINDS = ("treaty", "treaty-expression", "participant", "treaty-action", "treaty-statement")
ACTION_TYPES = ("signature", "definitive-signature", "ratification", "acceptance", "approval", "accession",
                "succession", "consent-to-be-bound", "entry-into-force", "denunciation", "withdrawal",
                "territorial-application", "provisional-application", "other")
CONSENT_TYPES = frozenset({"definitive-signature", "ratification", "acceptance", "approval", "accession",
                           "succession", "consent-to-be-bound"})
EXIT_TYPES = frozenset({"denunciation", "withdrawal"})
STATEMENT_KINDS = ("reservation", "declaration", "objection", "communication", "withdrawal-of-reservation",
                   "footnote")
PARTICIPANT_TYPES = ("state", "international-organisation", "eu", "unknown")

# TR01 access decisions. Endpoints, element structures and terms are recorded from the sources' public
# documentation as known without network access; every item marked ``verify`` must be checked before a dated live
# run (TR13, #2645). The audit did not re-read the terms pages live.
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "untc": {
        "publisher": "United Nations, Office of Legal Affairs, Treaty Section (United Nations Treaty Collection)",
        "endpoints": [("https://treaties.un.org/Pages/ViewDetails.aspx?src=TREATY&mtdsg_no={chapter}-{number}"
                       "&chapter={chapter_number}&clang=_en")],
        "formats": ["untc-status-html"],
        "authentication": "none; no key is issued or handled",
        "rate_limits": "none documented (verify); one status page per declared treaty, at most "
                       f"{MAX_UNITS} treaties per source, sequential requests only",
        "identifiers": ["UNTC chapter and treaty number (mtdsg_no, e.g. XXVII-7)",
                        "UNTS registration number and volume as published",
                        "depositary notification (C.N.) references as published in notes"],
        "revisions": "the page states 'Status as at' (date and time); a changed page is a new revision of every "
                     "record whose content changed, with the status stamp as the depositary's revision; a row or "
                     "statement the page no longer shows becomes a 'no-longer-published' revision, never a deletion; "
                     "footnotes announcing corrections, withdrawals of reservations or territorial changes are "
                     "kept verbatim",
        "licence": "UN website terms of use: content may be reproduced for non-commercial use with attribution; "
                   "status data are official depositary information (verify the current wording, including the "
                   "commercial-use clause); treaty texts (PDF) are linked, never mirrored",
        "attribution": UN_ATTRIBUTION,
        "personal_data": "none in the status tables; statements may name officials in their verbatim text",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the element structure recorded in the authored fixture; the live "
                  "page's element ids, the date formats and the status stamp must be verified; no documented API",
    },
    "eu-cellar": {
        "publisher": "Publications Office of the European Union (CELLAR SPARQL endpoint)",
        "endpoints": ["https://publications.europa.eu/webapi/rdf/sparql"],
        "formats": ["cellar-agreement-sparql"],
        "authentication": "none",
        "rate_limits": "fair use; queries time out after 60 s (verify); two bounded queries per declared agreement",
        "identifiers": ["CELEX (sector 2, international agreements, e.g. 22099A0101(01))", "ELI where published",
                        "CELLAR work/expression/manifestation/item URIs",
                        "authority-table country and corporate-body codes for contracting parties"],
        "revisions": "CELLAR states no revision stamp per work in the bounded query; the work's last modification "
                     "date is read where published (verify the property) and otherwise the acquisition is the "
                     "revision; corrigenda are separate works linked by an explicit CDM triple",
        "licence": "Commission Decision 2011/833/EU: reuse authorised with acknowledgement of the source; the "
                   "official texts are referenced by item URI and never mirrored",
        "attribution": EU_ATTRIBUTION,
        "personal_data": "none in the bounded query (agent metadata is not queried)",
        "access_decision": "unverified-live",
        "reason": "the work/expression query is the Legal pack's reviewed CELLAR query (prior live evidence "
                  "2026-09-09); the agreement-date, contracting-party and act-relation predicates are recorded "
                  "from CDM documentation and must be verified live",
        "predicates_to_verify": ["cdm:resource_legal_date_signature", "cdm:resource_legal_date_entry-into-force",
                                 "cdm:resource_legal_date_end-of-validity", "cdm:work_date_document",
                                 "cdm:agreement_international_has_contracting_party (verify)",
                                 "cdm:resource_legal_based_on_resource_legal (inverse, from the EU act)",
                                 "cdm:work_cites_work (inverse)"],
    },
    "coe-treaty-office": {
        "publisher": "Council of Europe Treaty Office",
        "endpoints": ["https://www.coe.int/en/web/conventions/full-list?module=signatures-by-treaty&treatynum={cets}",
                      ("https://www.coe.int/en/web/conventions/full-list?module=declarations-by-treaty"
                       "&numSte={cets}&codeNature=0")],
        "formats": ["coe-treaty-html"],
        "authentication": "none",
        "rate_limits": "none documented (verify); two pages per declared treaty, sequential requests only",
        "identifiers": ["CETS/ETS number (three digits, e.g. 005)",
                        "state name as published, member or non-member of the Council of Europe as published"],
        "revisions": "the chart states 'Status as of' (date); a changed chart or declaration is a new revision; "
                     "denunciations are actions with their dates, never deletions; a row the chart no longer "
                     "shows becomes a 'no-longer-published' revision",
        "licence": "Council of Europe website terms: reproduction of official texts and data authorised with the "
                   "source cited, except for commercial purposes without permission (verify)",
        "attribution": COE_ATTRIBUTION,
        "personal_data": "declarations may name officials or authorities designated under a treaty (names and "
                         "contact details of designated authorities are published for some conventions)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the element structure recorded in the authored fixture; the "
                  "live page structure and parameters must be verified; no documented API",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
                                                              "authored fixtures only (#2645)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (TR01): nothing implies complete coverage of a chapter, a treaty series or a state.
BOUNDED_COVERAGE = {
    "untc": f"the multilateral treaties named in the selection by UNTC chapter and number, at most {MAX_UNITS} per "
            f"source, at most {MAX_PARTICIPANTS} participants and {MAX_STATEMENTS} statements per treaty; first "
            "selection: human-rights, environment and trade-related chapters (IV, XXVII, X) named explicitly",
    "eu-cellar": f"the EU international agreements named by CELEX, at most {MAX_UNITS} per source and at most three "
                 "language expressions (ENG, FRA, DEU) per agreement; linked acts limited to the relations the "
                 "second query declares",
    "coe-treaty-office": f"the Council of Europe treaties named by CETS/ETS number, at most {MAX_UNITS} per source; "
                         "all states (members and non-members) the chart lists",
    "period": "the status as published at acquisition time; earlier statuses only as revisions this runtime has "
              "itself observed (no back-filled history)",
}
# TR01 data-minimisation decision (docs/development/treaties-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "subjects": "participants are states, international organisations and the EU as each source names them; no "
                "natural person is ever a participant, a key, a match candidate or a monitor target",
    "stored": ["participant name as published", "codes the source publishes", "action type and dates as published",
               "verbatim statement text (the depositary's publication of a state's act)", "anchors and footnotes"],
    "never_stored": ["signatory, plenipotentiary or representative names as separate fields",
                     "contact details (e-mail, telephone, postal address) of designated authorities",
                     "any per-person index or profile"],
    "redacted": "contact details of designated authorities published inside CoE declarations are replaced by "
                "'[contact details withheld: TR01]' before a record exists; the rest of the statement is verbatim",
    "retention": "retained with the treaty record; revisions are immutable and superseded, never purged; no "
                 "personal identifier is stored as a field, so nothing personal remains to purge",
    "query_scope": "knowledge:legal:read (the Legal pack read scope); verbatim statements are returned only in "
                   "treaty and participant answers, never searchable by a person's name",
}
PERSONAL_FIELD_KEYS = frozenset({
    "signatory", "signatory_name", "signed_by", "plenipotentiary", "representative", "representative_name",
    "person", "person_name", "official_name", "contact", "contact_name", "email", "e_mail", "telephone", "phone",
    "fax", "postal_address",
})
_CONTACT = re.compile(r"(?:\b(?:tel|phone|telephone|fax)\.?\s*:?\s*\+?[\d ()./-]{6,}|[\w.+-]+@[\w-]+\.[\w.-]+)", re.IGNORECASE)
CONTACT_WITHHELD = "[contact details withheld: TR01]"


class TreatiesFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "none"


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def day(value: Any) -> str | None:
    """An ISO day from ``2 Mar 2090``, ``02/03/2090``, ``15-01-2099`` or an ISO date/time; else ``None``."""
    text = clean(value) or ""
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        parts = (int(match[1]), int(match[2]), int(match[3]))
    else:
        match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b", text)
        if match and match[2].casefold() in _MONTHS:
            parts = (int(match[3]), _MONTHS[match[2].casefold()], int(match[1]))
        else:
            match = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", text)
            if not match:
                return None
            parts = (int(match[3]), int(match[2]), int(match[1]))
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def stamp(value: Any) -> str | None:
    """A sortable depositary revision stamp (ISO date plus time when published) from a 'status as at' text."""
    text = clean(value) or ""
    iso = day(text)
    if not iso:
        return None
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    return f"{iso}T{int(match[1]):02d}:{match[2]}:{match[3] or '00'}" if match else iso


def treaty_key(provider: str, native_id: Any) -> str:
    return f"treaties:{provider}:treaty:{str(native_id).strip()}"


def participant_key(provider: str, name_or_code: Any) -> str:
    return f"treaties:{provider}:participant:{slug(name_or_code)}"


def action_key(treaty: str, participant: str, action_type: str, sequence: int) -> str:
    native = treaty.split(":", 3)[3]
    return f"{treaty.rsplit(':treaty:', 1)[0]}:action:{native}:{participant.rsplit(':', 1)[-1]}:{action_type}:{sequence}"


def statement_key(treaty: str, participant: str | None, kind: str, anchor_or_seq: Any) -> str:
    native = treaty.split(":", 3)[3]
    who = participant.rsplit(":", 1)[-1] if participant else "depositary"
    return f"{treaty.rsplit(':treaty:', 1)[0]}:statement:{native}:{who}:{kind}:{slug(anchor_or_seq)}"


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of any personal field (signatory, representative, contact) anywhere in a record."""
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in PERSONAL_FIELD_KEYS:
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(dict(record), "$")
    fields = dict(record.get("fields") or {})
    if record.get("record_kind") == "participant" and fields.get("participant_type") not in PARTICIPANT_TYPES:
        found.append("$.fields.participant_type")
    text = str(fields.get("text_verbatim") or "")
    if _CONTACT.search(text):
        found.append("$.fields.text_verbatim (contact details)")
    return found


def withhold_contacts(text: str | None) -> tuple[str | None, bool]:
    if not text:
        return text, False
    replaced = _CONTACT.sub(CONTACT_WITHHELD, text)
    return replaced, replaced != text


def _record(fmt: str, kind: str, record_key: str, *, treaty: str, title: Any, locator: str,
            fields: Mapping[str, Any], native_revision: Any, participant: str | None = None) -> dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise TreatiesFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://"):
        raise TreatiesFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT, "format": fmt, "provider": FORMATS[fmt]["provider"], "record_kind": kind,
        "record_key": record_key, "treaty_key": treaty, "participant_key": participant,
        "native_revision": clean(native_revision), "revision_order": clean(native_revision) or "",
        "title": clean(title) or record_key, "locator": locator, "publication_status": "published",
        "minimisation": MINIMISATION_POLICY, "fields": dict(fields),
    }


# ------------------------------------------------------------------ a minimal HTML tree


class Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: Mapping[str, Any], parent: Node | None) -> None:
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs), [], parent

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return " ".join(" ".join(parts).split())

    def own_text(self, skip: tuple[str, ...] = ("sup",)) -> str:
        parts = [c if isinstance(c, str) else ("" if c.tag in skip else c.own_text(skip)) for c in self.children]
        return " ".join(" ".join(parts).split())

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: str | None = None, **attrs: str) -> list[Node]:
        out = []
        for node in self.iter():
            if tag and node.tag != tag:
                continue
            if all((c in (node.attrs.get("class") or "").split()) if k == "class_" else node.attrs.get(k) == c
                   for k, c in attrs.items()):
                out.append(node)
        return out

    def find(self, tag: str | None = None, **attrs: str) -> Node | None:
        found = self.find_all(tag, **attrs)
        return found[0] if found else None

    def get(self, key: str) -> str | None:
        value = self.attrs.get(key)
        return None if value is None else str(value)


class _TreeBuilder(HTMLParser):
    VOID = frozenset({"br", "img", "hr", "meta", "link", "input"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(raw: bytes) -> Node:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TreatiesFormatError("schema_drift", "response is not UTF-8 HTML") from exc
    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


def _required(node: Node | None, what: str) -> Node:
    if node is None:
        raise TreatiesFormatError("schema_drift", f"page has no {what}")
    return node


def _info_table(table: Node) -> dict[str, Node]:
    out = {}
    for row in table.find_all("tr"):
        head, cell = row.find("th"), row.find("td")
        if head is not None and cell is not None:
            out[slug(head.text())] = cell
    return out


def _footnote_refs(cell: Node) -> list[str]:
    refs = []
    for link in cell.find_all("a"):
        href = link.get("href") or ""
        if href.startswith("#"):
            refs.append(href[1:])
    return refs


# ------------------------------------------------------------------ UNTC

UNTC_SITE = "https://treaties.un.org"
# Consent-column suffixes as the UNTC status tables print them (verify live).
UNTC_SUFFIX = {"": "ratification", "a": "accession", "d": "succession", "A": "acceptance", "AA": "approval",
               "s": "definitive-signature"}
_UNTC_ID = re.compile(r"^([IVXL]+)-(\d{1,3}(?:-[a-z])?)$")


def untc_url(unit: Mapping[str, Any]) -> str:
    return (f"{UNTC_SITE}/Pages/ViewDetails.aspx?src=TREATY&mtdsg_no={unit['mtdsg_no']}&chapter={unit['chapter']}"
            "&clang=_en")


def _untc_date_suffix(text: str) -> tuple[str | None, str]:
    """(ISO day, suffix) from a status cell such as ``10 May 2092 a``; the suffix stays verbatim."""
    value = clean(text) or ""
    match = re.match(r"^(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})\s*([A-Za-z]{0,2})$", value)
    if not match:
        return None, value
    return day(match[1]), match[2]


def parse_untc(raw: bytes, unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "untc-status-html"
    root = parse_html(raw)
    mtdsg = str(unit["mtdsg_no"])
    chapter_roman, number = mtdsg.split("-", 1)
    key = treaty_key("untc", mtdsg)
    locator = untc_url(unit)
    header = _required(root.find(id="treaty-header"), "treaty header")
    title_node = _required(header.find(class_="treaty-title"), "treaty title")
    status_node = _required(header.find(class_="status-date"), "'Status as at' stamp")
    status_text = clean(status_node.text())
    revision = stamp(status_text)
    if not revision:
        raise TreatiesFormatError("schema_drift", "the 'Status as at' stamp carries no date")
    info = _info_table(_required(header.find("table", id="treaty-info"), "treaty information table"))
    notes: dict[str, dict[str, Any]] = {}
    notes_block = root.find(id="notes")
    for note in (notes_block.find_all("p", class_="note") if notes_block else []):
        anchor = note.get("id") or f"note-{len(notes) + 1}"
        notes[anchor] = {"anchor": anchor, "marker": clean((note.find("sup") or note).text()) if note.find("sup")
                         else None, "text_verbatim": note.own_text()}
    adoption = clean(info["place-and-date-of-adoption"].text()) if "place-and-date-of-adoption" in info else None
    eif = clean(info["entry-into-force"].text()) if "entry-into-force" in info else None
    registration = clean(info["registration"].text()) if "registration" in info else None
    unts = re.search(r"No\.\s*(\d+)", registration or "")
    texts = [{"label": clean(a.text()), "url": a.get("href")} for a in (info.get("text") or Node("x", {}, None))
             .find_all("a") if str(a.get("href") or "").startswith("https://")]
    cross = [{"scheme": "untc-mtdsg", "value": mtdsg, "as_published": mtdsg, "basis": "native identifier"}]
    if unts:
        cross.append({"scheme": "unts-registration", "value": unts[1], "as_published": registration,
                      "basis": "registration as published"})
    title = clean(title_node.text())
    title = re.sub(r"^\d+[a-z]?\.\s*", "", title or "") or None
    for match in re.finditer(r"\b(?:CETS|ETS)\s+No\.\s*(\d{1,3})", title or ""):
        cross.append({"scheme": "cets", "value": f"{int(match[1]):03d}", "as_published": match[0],
                      "basis": "citation in the published title"})
    records = [_record(fmt, "treaty", key, treaty=key, title=title, locator=locator, native_revision=revision, fields={
        "identifiers": {"untc_mtdsg": mtdsg, "untc_chapter": chapter_roman, "untc_number": number,
                        "unts_registration": unts[1] if unts else None, "celex": None, "eli": None, "cets": None},
        "title_as_published": title, "depositary": FORMATS[fmt]["depositary"],
        "adoption": {"as_published": adoption, "date": day(adoption)},
        "entry_into_force": {"conditions_as_published": eif, "date": day(eif)},
        "registration_as_published": registration, "status_as_published": status_text,
        "status_summary_as_published": clean(info["status"].text()) if "status" in info else None,
        "text_references": texts, "text_policy": "linked, not mirrored",
        "footnotes": [notes[a] for a in sorted(notes)], "cross_references": cross, "citations": [],
        "attribution": UN_ATTRIBUTION})]
    table = _required(root.find("table", id="participants"), "participants table")
    rows = table.find_all("tr")
    headings = [clean(h.text()) for h in rows[0].find_all("th")] if rows else []
    if len(headings) < 3 or not (headings[0] or "").startswith("Participant"):
        raise TreatiesFormatError("schema_drift", "participants table has an unexpected header")
    participants: dict[str, str] = {}
    body_rows = [r for r in rows[1:] if r.find("td") is not None]
    if len(body_rows) > MAX_PARTICIPANTS:
        raise TreatiesFormatError("input_limit", "more participants than the declared bound")
    for row in body_rows:
        cells = row.find_all("td")
        name = clean(cells[0].own_text())
        if not name:
            raise TreatiesFormatError("schema_drift", "a participant row has no name")
        pkey = participant_key("untc", name)
        participants[name] = pkey
        refs = _footnote_refs(cells[0])
        ptype = "eu" if name.casefold() == "european union" else "unknown"
        records.append(_record(fmt, "participant", pkey, treaty=key, title=name, locator=locator,
                               native_revision=revision, participant=pkey, fields={
                                   "name_as_published": name, "participant_type": ptype,
                                   "participant_type_as_published": None, "codes": [],
                                   "note": "UNTC publishes participant names only; no code is stated"}))
        for column, cell in enumerate(cells[1:3], start=1):
            text = clean(cell.own_text())
            if not text:
                continue
            date, suffix = _untc_date_suffix(text)
            if column == 1:
                action_type = "definitive-signature" if suffix == "s" else "signature"
            else:
                action_type = UNTC_SUFFIX.get(suffix, "other")
            fields = {"participant_as_published": name, "action_type": action_type,
                      "action_type_as_published": headings[column] + (f" [{suffix}]" if suffix else ""),
                      "sequence": 1, "date_text_as_published": text,
                      # UNTC prints the signature date and the date the instrument was deposited.
                      "action_date": date if column == 1 else None,
                      "deposit_date": date if column == 2 else None, "effective_date": None,
                      "date_status": "stated" if date else "unclear",
                      "footnote_refs": sorted(set(refs + _footnote_refs(cell))),
                      "footnotes": [notes[r] for r in sorted(set(refs + _footnote_refs(cell))) if r in notes]}
            akey = action_key(key, pkey, action_type, 1)
            records.append(_record(fmt, "treaty-action", akey, treaty=key, title=f"{name}: {action_type}",
                                   locator=locator, native_revision=revision, participant=pkey, fields=fields))
    records += _untc_statements(root, key, locator, revision, participants, fmt)
    referring: dict[str, list[str]] = {}
    for record in records:
        for ref in record["fields"].get("footnote_refs") or []:
            if record["participant_key"] and record["participant_key"] not in referring.setdefault(ref, []):
                referring[ref].append(record["participant_key"])
    for anchor, note in sorted(notes.items()):
        skey = statement_key(key, None, "footnote", anchor)
        records.append(_record(fmt, "treaty-statement", skey, treaty=key, title=f"Note {note['marker'] or anchor}",
                               locator=f"{locator}#{anchor}", native_revision=revision, fields={
                                   "statement_kind": "footnote", "statement_kind_as_published": "Note",
                                   "participant_as_published": None, "action_key": None, "anchor": anchor,
                                   "text_verbatim": note["text_verbatim"], "objects_to_statement_key": None,
                                   "objects_to_as_published": None, "made_on": day(note["text_verbatim"]),
                                   "articles_as_published": None,
                                   "refers_to_participants": sorted(referring.get(anchor) or []),
                                   "reference_basis": "the participant's row carries the note marker"}))
    return records


def _consent_action(records: list[dict[str, Any]], pkey: str) -> str | None:
    for record in records:
        if record["record_kind"] == "treaty-action" and record["participant_key"] == pkey and \
                record["fields"]["action_type"] in CONSENT_TYPES:
            return record["record_key"]
    return None


def _untc_statements(root: Node, key: str, locator: str, revision: str, participants: Mapping[str, str],
                     fmt: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    anchors: dict[str, str] = {}
    blocks = []
    for section_id, default_kind in (("declarations", "declaration"), ("objections", "objection"),
                                     ("communications", "communication")):
        section = root.find(id=section_id)
        if section is None:
            continue
        for group in section.find_all(class_="participant-statements"):
            name = clean(group.get("data-participant"))
            for index, paragraph in enumerate(group.find_all("p", class_="statement"), start=1):
                blocks.append((name, default_kind, index, paragraph))
    if len(blocks) > MAX_STATEMENTS:
        raise TreatiesFormatError("input_limit", "more statements than the declared bound")
    for name, default_kind, index, paragraph in blocks:
        pkey = participants.get(name or "")
        if pkey is None:
            raise TreatiesFormatError("schema_drift", f"statement names a participant not in the table: {name!r}")
        kind_published = clean(paragraph.get("data-kind")) or default_kind.capitalize()
        kind = slug(kind_published)
        kind = kind if kind in STATEMENT_KINDS else default_kind
        anchor = paragraph.get("id") or f"{slug(name)}-{default_kind}-{index}"
        anchors[anchor] = statement_key(key, pkey, kind, anchor)
        text, withheld = withhold_contacts(paragraph.own_text())
        out.append(_record(fmt, "treaty-statement", anchors[anchor], treaty=key, participant=pkey,
                           title=f"{name}: {kind_published}", locator=f"{locator}#{anchor}", native_revision=revision,
                           fields={"statement_kind": kind, "statement_kind_as_published": kind_published,
                                   "participant_as_published": name, "action_key": None, "anchor": anchor,
                                   "text_verbatim": text, "contact_details_withheld": withheld,
                                   "objects_to_statement_key": None,
                                   "objects_to_as_published": clean(paragraph.get("data-objects-to")),
                                   "made_on": day(paragraph.get("data-date")) if paragraph.get("data-date") else None,
                                   "made_upon_as_published": clean(paragraph.get("data-upon")),
                                   "articles_as_published": None}))
    return out


def link_statements(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Link each statement to its participant's consent action and each objection to the anchor it names."""
    by_anchor = {r["fields"].get("anchor"): r["record_key"] for r in records if r["record_kind"] == "treaty-statement"}
    for record in records:
        if record["record_kind"] != "treaty-statement" or not record["participant_key"]:
            continue
        fields = record["fields"]
        if fields.get("action_key") is None and fields.get("action_link_basis") != "not stated by the source":
            fields["action_key"] = _consent_action(records, record["participant_key"])
            fields["action_link_basis"] = fields.get("action_link_basis") or (
                "the section states statements were made upon consent to be bound unless otherwise indicated")
        target = fields.get("objects_to_as_published")
        if target:
            fields["objects_to_statement_key"] = by_anchor.get(target)
            fields["objection_link"] = "linked by the source" if target in by_anchor else \
                "the source names an anchor not on this page"
    return records


# ------------------------------------------------------------------ Council of Europe

COE_SITE = "https://www.coe.int"


def coe_urls(unit: Mapping[str, Any]) -> dict[str, str]:
    cets = unit["cets"]
    return {"chart": f"{COE_SITE}/en/web/conventions/full-list?module=signatures-by-treaty&treatynum={cets}",
            "declarations": f"{COE_SITE}/en/web/conventions/full-list?module=declarations-by-treaty&numSte={cets}"
                            "&codeNature=0"}


def parse_coe(chart_raw: bytes, declarations_raw: bytes, unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "coe-treaty-html"
    cets = str(unit["cets"])
    key = treaty_key("coe", cets)
    urls = coe_urls(unit)
    chart = parse_html(chart_raw)
    header = _required(chart.find(id="treaty-header"), "treaty header")
    title = clean(_required(header.find(class_="treaty-title"), "treaty title").text())
    status_text = clean(_required(header.find(class_="status-date"), "'Status as of' stamp").text())
    revision = stamp(status_text)
    if not revision:
        raise TreatiesFormatError("schema_drift", "the 'Status as of' stamp carries no date")
    info = _info_table(_required(header.find("table", id="treaty-info"), "treaty information table"))

    def cell(name: str) -> str | None:
        return clean(info[name].text()) if name in info else None

    opening = cell("opening-for-signature")
    eif = cell("entry-into-force")
    records = [_record(fmt, "treaty", key, treaty=key, title=title, locator=urls["chart"], native_revision=revision,
                       fields={
                           "identifiers": {"untc_mtdsg": None, "untc_chapter": None, "untc_number": None,
                                           "unts_registration": None, "celex": None, "eli": None, "cets": cets},
                           "title_as_published": title, "depositary": FORMATS[fmt]["depositary"],
                           "adoption": {"as_published": opening, "date": day(opening)},
                           "entry_into_force": {"conditions_as_published": eif, "date": day(eif)},
                           "registration_as_published": None, "status_as_published": status_text,
                           "status_summary_as_published": cell("status"),
                           "treaty_series_as_published": cell("treaty-series"),
                           "text_references": [{"label": clean(a.text()), "url": a.get("href")} for a in
                                               (info.get("text") or Node("x", {}, None)).find_all("a")
                                               if str(a.get("href") or "").startswith("https://")],
                           "text_policy": "linked, not mirrored", "footnotes": [],
                           "cross_references": [{"scheme": "cets", "value": cets, "as_published": f"CETS No. {cets}",
                                                 "basis": "native identifier"}],
                           "citations": [], "attribution": COE_ATTRIBUTION})]
    table = _required(chart.find("table", id="signatures"), "chart of signatures and ratifications")
    rows = [r for r in table.find_all("tr") if r.find("td") is not None]
    if len(rows) > MAX_PARTICIPANTS:
        raise TreatiesFormatError("input_limit", "more states than the declared bound")
    participants: dict[str, str] = {}
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            raise TreatiesFormatError("schema_drift", "a chart row has fewer than five cells")
        name = clean(cells[0].own_text())
        if not name:
            raise TreatiesFormatError("schema_drift", "a chart row has no state")
        membership = clean(row.get("data-membership"))
        pkey = participant_key("coe", name)
        participants[name] = pkey
        records.append(_record(fmt, "participant", pkey, treaty=key, title=name, locator=urls["chart"],
                               native_revision=revision, participant=pkey, fields={
                                   "name_as_published": name, "participant_type": "eu" if name.casefold() ==
                                   "european union" else "state" if membership in {"member", "non-member"}
                                   else "unknown",
                                   "participant_type_as_published": membership, "codes": [],
                                   "note": "the chart lists states by name and membership; no code is stated"}))
        notes_text = clean(cells[4].text())
        actions = [("signature", "Signature", cells[1], "action_date"),
                   ("ratification", "Ratification", cells[2], "deposit_date"),
                   ("entry-into-force", "Entry into force", cells[3], "effective_date")]
        for action_type, heading, node, date_field in actions:
            text = clean(node.text())
            if not text:
                continue
            date = day(text)
            fields = {"participant_as_published": name, "action_type": action_type,
                      "action_type_as_published": heading, "sequence": 1, "date_text_as_published": text,
                      "action_date": None, "deposit_date": None, "effective_date": None,
                      "date_status": "stated" if date else "unclear", "notes_as_published": notes_text,
                      "footnote_refs": [], "footnotes": []}
            fields[date_field] = date
            records.append(_record(fmt, "treaty-action", action_key(key, pkey, action_type, 1), treaty=key,
                                   title=f"{name}: {heading}", locator=urls["chart"], native_revision=revision,
                                   participant=pkey, fields=fields))
        for index, match in enumerate(re.finditer(r"(Denunciation|Withdrawal)[^:]*:\s*([^;]+)", notes_text or ""),
                                      start=1):
            action_type = "denunciation" if match[1] == "Denunciation" else "withdrawal"
            date_text = clean(match[2])
            records.append(_record(fmt, "treaty-action", action_key(key, pkey, action_type, index), treaty=key,
                                   title=f"{name}: {match[1]}", locator=urls["chart"], native_revision=revision,
                                   participant=pkey, fields={
                                       "participant_as_published": name, "action_type": action_type,
                                       "action_type_as_published": clean(match[0]), "sequence": index,
                                       "date_text_as_published": date_text, "action_date": None,
                                       "deposit_date": day(date_text), "effective_date": None,
                                       "date_status": "stated" if day(date_text) else "unclear",
                                       "notes_as_published": notes_text, "footnote_refs": [], "footnotes": []}))
        effective = re.search(r"[Ee]ffective(?: date)?:\s*([^;]+)", notes_text or "")
        if effective:
            for record in records:
                if record["participant_key"] == pkey and record["fields"].get("action_type") in EXIT_TYPES:
                    record["fields"]["effective_date"] = day(effective[1])
    declarations = parse_html(declarations_raw)
    statements = declarations.find_all("div", class_="declaration")
    if len(statements) > MAX_STATEMENTS:
        raise TreatiesFormatError("input_limit", "more declarations than the declared bound")
    counters: dict[tuple[str, str], int] = {}
    for block in statements:
        name = clean(block.get("data-state"))
        pkey = participants.get(name or "")
        if pkey is None:
            raise TreatiesFormatError("schema_drift", f"a declaration names a state not in the chart: {name!r}")
        kind_published = clean(block.get("data-kind")) or "Declaration"
        kind = slug(kind_published)
        kind = kind if kind in STATEMENT_KINDS else "communication"
        counters[(pkey, kind)] = counters.get((pkey, kind), 0) + 1
        anchor = block.get("id") or f"{slug(name)}-{kind}-{counters[(pkey, kind)]}"
        head = clean((block.find("p", class_="decl-head") or Node("x", {}, None)).text())
        upon = re.search(r"instrument of (ratification|acceptance|approval|accession)", head or "", re.IGNORECASE)
        link_basis = f"the heading names the instrument of {upon[1].lower()}" if upon else "not stated by the source"
        body = "\n".join(p.text() for p in block.find_all("p", class_="decl-text"))
        text, withheld = withhold_contacts(body)
        records.append(_record(fmt, "treaty-statement", statement_key(key, pkey, kind, anchor), treaty=key,
                               participant=pkey, title=f"{name}: {kind_published}",
                               locator=f"{urls['declarations']}#{anchor}", native_revision=revision, fields={
                                   "statement_kind": kind, "statement_kind_as_published": kind_published,
                                   "participant_as_published": name, "action_key": None, "anchor": anchor,
                                   "text_verbatim": text, "contact_details_withheld": withheld,
                                   "heading_as_published": head, "made_on": day(head),
                                   "action_link_basis": link_basis,
                                   "objects_to_statement_key": None,
                                   "objects_to_as_published": clean(block.get("data-objects-to")),
                                   "period_as_published": clean((block.find("p", class_="decl-period") or
                                                                 Node("x", {}, None)).text()),
                                   "articles_as_published": clean((block.find("p", class_="decl-articles") or
                                                                   Node("x", {}, None)).text())}))
    return link_statements(records)


# ------------------------------------------------------------------ CELLAR (EU international agreements)

CELLAR_LANGUAGES = ("ENG", "FRA", "DEU")
_CELEX_AGREEMENT = re.compile(r"^2\d{4}[A-Z]\d{4}\(\d{2}\)$|^2\d{4}[A-Z]\d{4}$")
# EU-act -> agreement relations read by the second query (predicate recorded verbatim; verify live).
CELLAR_ACT_RELATIONS = ("http://publications.europa.eu/ontology/cdm#resource_legal_based_on_resource_legal",
                        "http://publications.europa.eu/ontology/cdm#work_cites_work")
CELLAR_DATE_PREDICATES = {
    "document": "http://publications.europa.eu/ontology/cdm#work_date_document",
    "signature": "http://publications.europa.eu/ontology/cdm#resource_legal_date_signature",
    "entry_into_force": "http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force",
    "end_of_validity": "http://publications.europa.eu/ontology/cdm#resource_legal_date_end-of-validity",
}
CELLAR_PARTY_PREDICATE = "http://publications.europa.eu/ontology/cdm#agreement_international_has_contracting_party"


def cellar_agreement_query(celex: str) -> str:
    """The bounded query for one agreement's dates, contracting parties and the EU acts that point at it."""
    if not _CELEX_AGREEMENT.fullmatch(celex):
        raise ValueError("an international agreement is selected by its sector-2 CELEX number")
    value = json.dumps(celex) + "^^<http://www.w3.org/2001/XMLSchema#string>"
    dates = " ".join(f"<{p}>" for p in CELLAR_DATE_PREDICATES.values())
    relations = ", ".join(f"<{p}>" for p in CELLAR_ACT_RELATIONS)
    return ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "SELECT DISTINCT ?work ?celex ?eli ?date_predicate ?date ?party ?act ?act_celex ?act_date ?relation\n"
            "WHERE {\n"
            f" VALUES ?celex {{ {value} }}\n"
            " ?work cdm:resource_legal_id_celex ?celex .\n"
            " OPTIONAL { ?work cdm:resource_legal_eli ?eli }\n"
            f" OPTIONAL {{ VALUES ?date_predicate {{ {dates} }} ?work ?date_predicate ?date }}\n"
            f" OPTIONAL {{ ?work <{CELLAR_PARTY_PREDICATE}> ?party }}\n"
            " OPTIONAL { ?act ?relation ?work . ?act cdm:resource_legal_id_celex ?act_celex .\n"
            f"            FILTER (?relation IN ({relations}))\n"
            "            OPTIONAL { ?act cdm:work_date_document ?act_date } }\n"
            "}\nORDER BY ?date_predicate ?party ?act_celex ?relation\nLIMIT 500")


def _bindings(raw: bytes, what: str) -> tuple[list[dict[str, str]], Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TreatiesFormatError("schema_drift", f"{what} is not SPARQL JSON") from exc
    rows = ((payload if isinstance(payload, Mapping) else {}).get("results") or {}).get("bindings")
    if not isinstance(rows, list):
        raise TreatiesFormatError("schema_drift", f"{what} has no bindings")
    return [{k: str(v.get("value")) for k, v in row.items() if isinstance(v, Mapping)} for row in rows], payload


def parse_cellar(expressions_raw: bytes, agreement_raw: bytes, unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    from src.ingestion.regional_providers import ProviderError, parse_cellar_results

    fmt = "cellar-agreement-sparql"
    celex = str(unit["celex"])
    key = treaty_key("eu-cellar", celex)
    locator = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
    rows, payload = _bindings(expressions_raw, "the CELLAR expression result")
    try:
        grouped = parse_cellar_results(payload, celex_ids=[celex], languages=CELLAR_LANGUAGES, offset=0,
                                       limit=max(1, len(rows)))
    except ProviderError as exc:
        raise TreatiesFormatError("schema_drift", f"CELLAR expressions: {exc}") from exc
    facts, _ = _bindings(agreement_raw, "the CELLAR agreement result")
    if not facts:
        raise TreatiesFormatError("schema_drift", "CELLAR states nothing for the selected agreement")
    if any(f.get("celex") != celex for f in facts):
        raise TreatiesFormatError("source_identity", "CELLAR row does not match the selected agreement")
    work = facts[0].get("work")
    eli = sorted({f["eli"] for f in facts if f.get("eli")})
    dates: dict[str, list[str]] = {}
    for fact in facts:
        for name, predicate in CELLAR_DATE_PREDICATES.items():
            if fact.get("date_predicate") == predicate and fact.get("date"):
                dates.setdefault(name, [])
                if fact["date"] not in dates[name]:
                    dates[name].append(fact["date"])
    parties = sorted({f["party"] for f in facts if f.get("party")})
    citations = []
    for fact in facts:
        if fact.get("act_celex"):
            item = {"relation_as_published": fact.get("relation"), "celex": fact["act_celex"],
                    "act_uri": fact.get("act"), "act_document_date": day(fact.get("act_date")),
                    "direction": "the EU act points at the agreement", "basis": "explicit CDM triple"}
            if item not in citations:
                citations.append(item)
    by_language: dict[str, dict[str, Any]] = {}
    title = None
    for record in grouped["records"]:
        fields = record["fields"]
        language = str(fields.get("language_identity") or "").rsplit("/", 1)[-1]
        entry = by_language.setdefault(language, {"language": language, "expression_uri": fields["expression"],
                                                  "title_as_published": record.get("title"), "manifestations": []})
        if fields.get("manifestation"):
            entry["manifestations"].append({"format": fields.get("format"), "manifestation": fields["manifestation"],
                                            "item": fields.get("item")})
        if language == "ENG" and record.get("title") and record.get("title") != celex:
            title = record["title"]
    title = title or next((e["title_as_published"] for e in by_language.values()), None) or celex

    def first(name: str) -> str | None:
        values = dates.get(name) or []
        return day(values[0]) if values else None

    cross = [{"scheme": "celex", "value": celex, "as_published": celex, "basis": "native identifier"}]
    for match in re.finditer(r"\b(?:CETS|ETS)\s+No\.?\s*(\d{1,3})", title):
        cross.append({"scheme": "cets", "value": f"{int(match[1]):03d}", "as_published": match[0],
                      "basis": "citation in the published title"})
    revision = None  # CELLAR states no per-work revision stamp in the bounded query
    treaty = _record(fmt, "treaty", key, treaty=key, title=title, locator=locator, native_revision=revision, fields={
        "identifiers": {"untc_mtdsg": None, "untc_chapter": None, "untc_number": None, "unts_registration": None,
                        "celex": celex, "eli": eli[0] if eli else None, "cets": None},
        "eli_identifiers": eli, "work_uri": work, "title_as_published": title,
        "depositary": None, "adoption": {"as_published": None, "date": first("document")},
        "entry_into_force": {"conditions_as_published": None, "date": first("entry_into_force"),
                             "all_dates_as_stated": [day(d) for d in dates.get("entry_into_force") or []]},
        "dates_as_stated": {name: [day(d) or d for d in values] for name, values in sorted(dates.items())},
        "registration_as_published": None, "status_as_published": None, "status_summary_as_published": None,
        "text_references": [{"label": f"EUR-Lex {celex}", "url": locator}], "text_policy": "linked, not mirrored",
        "footnotes": [], "cross_references": cross, "citations": citations,
        "contracting_parties_as_published": parties, "revision_basis": "CELLAR states no per-work revision stamp "
        "in the bounded query; each acquisition whose bindings differ is a new revision",
        "attribution": EU_ATTRIBUTION})
    records = [treaty]
    for language, entry in sorted(by_language.items()):
        records.append(_record(fmt, "treaty-expression", f"{key}:expression:{language}", treaty=key,
                               title=entry["title_as_published"] or title, locator=locator, native_revision=revision,
                               fields={**entry, "note": "a language version of the agreement (an expression of the "
                                                        "same work), never a separate treaty"}))
    eu = participant_key("eu-cellar", "EU")
    records.append(_record(fmt, "participant", eu, treaty=key, title="European Union", locator=locator,
                           native_revision=revision, participant=eu, fields={
                               "name_as_published": "European Union", "participant_type": "eu",
                               "participant_type_as_published": "the EU as author of the agreement record",
                               "codes": [{"scheme": "eu-authority-corporate-body", "value": "EU"}]}))
    for uri in parties:
        code = uri.rstrip("/").rsplit("/", 1)[-1]
        scheme = "eu-authority-country" if "/authority/country/" in uri else "eu-authority-corporate-body"
        pkey = participant_key("eu-cellar", code)
        if pkey == eu:
            continue
        codes = [{"scheme": scheme, "value": code}]
        if scheme == "eu-authority-country" and re.fullmatch(r"[A-Z]{3}", code):
            codes.append({"scheme": "iso3166-1-alpha3", "value": code,
                          "basis": "the EU country authority table code as published (ISO 3166-1 alpha-3 based)"})
        records.append(_record(fmt, "participant", pkey, treaty=key, title=code, locator=uri if uri.startswith(
            "https://") else locator, native_revision=revision, participant=pkey, fields={
                "name_as_published": code, "participant_type": "state" if scheme == "eu-authority-country"
                else "international-organisation", "participant_type_as_published": scheme, "codes": codes,
                "authority_uri": uri}))
    for action_type, name in (("signature", "signature"), ("entry-into-force", "entry_into_force")):
        for index, value in enumerate(dates.get(name) or [], start=1):
            fields = {"participant_as_published": "European Union", "action_type": action_type,
                      "action_type_as_published": CELLAR_DATE_PREDICATES[name], "sequence": index,
                      "date_text_as_published": value, "action_date": day(value) if action_type == "signature"
                      else None, "deposit_date": None,
                      "effective_date": day(value) if action_type == "entry-into-force" else None,
                      "date_status": "stated" if day(value) else "unclear", "footnote_refs": [], "footnotes": [],
                      "note": "the date as CELLAR states it for the agreement work; the EU is its author"}
            records.append(_record(fmt, "treaty-action", action_key(key, eu, action_type, index), treaty=key,
                                   title=f"European Union: {action_type}", locator=locator, native_revision=revision,
                                   participant=eu, fields=fields))
    return records


# ------------------------------------------------------------------ units and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = list(selection.get(key) or [])
    if set(selection) - {key} or not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a treaties selection names 1-{MAX_UNITS} {key}")
    out = []
    for unit in units:
        unit = dict(unit)
        if fmt == "untc-status-html":
            if not _UNTC_ID.fullmatch(str(unit.get("mtdsg_no") or "")) or not 1 <= int(unit.get("chapter") or 0) <= 30:
                raise SourcePackError("invalid_manifest", "a UNTC treaty names its mtdsg_no (e.g. XXVII-7) and chapter")
        elif fmt == "coe-treaty-html":
            if not re.fullmatch(r"\d{3}", str(unit.get("cets") or "")):
                raise SourcePackError("invalid_manifest", "a Council of Europe treaty names its three-digit CETS number")
        elif not _CELEX_AGREEMENT.fullmatch(str(unit.get("celex") or "")):
            raise SourcePackError("invalid_manifest", "an EU agreement names its sector-2 CELEX number")
        out.append(unit)
    return out


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named requests (path relative to the endpoint host, parameters) for one unit."""
    if fmt == "untc-status-html":
        return {"status": ("/Pages/ViewDetails.aspx", {"src": "TREATY", "mtdsg_no": unit["mtdsg_no"],
                                                        "chapter": int(unit["chapter"]), "clang": "_en"})}
    if fmt == "coe-treaty-html":
        return {"chart": ("/en/web/conventions/full-list", {"module": "signatures-by-treaty",
                                                            "treatynum": unit["cets"]}),
                "declarations": ("/en/web/conventions/full-list", {"module": "declarations-by-treaty",
                                                                   "numSte": unit["cets"], "codeNature": 0})}
    if fmt == "cellar-agreement-sparql":
        from src.ingestion.regional_providers import cellar_query

        return {"expressions": ("/webapi/rdf/sparql", {"query": cellar_query([unit["celex"]],
                                                                             languages=CELLAR_LANGUAGES, offset=0,
                                                                             limit=100),
                                                       "format": "application/sparql-results+json"}),
                "agreement": ("/webapi/rdf/sparql", {"query": cellar_agreement_query(unit["celex"]),
                                                     "format": "application/sparql-results+json"})}
    raise SourcePackError("invalid_manifest", f"unknown treaties format {fmt!r}")


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt == "untc-status-html":
        records = link_statements(parse_untc(responses["status"], unit))
    elif fmt == "coe-treaty-html":
        records = parse_coe(responses["chart"], responses["declarations"], unit)
    elif fmt == "cellar-agreement-sparql":
        records = parse_cellar(responses["expressions"], responses["agreement"], unit)
    else:
        raise TreatiesFormatError("schema_drift", f"unknown treaties format {fmt!r}")
    keys = [r["record_key"] for r in records]
    if len(set(keys)) != len(keys):
        raise TreatiesFormatError("schema_drift", "a page yields the same record key twice")
    for record in records:
        if minimisation_violations(record):
            raise TreatiesFormatError("minimisation_violation", f"{record['record_key']} carries a personal field")
    return records


def treaties_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("treaties") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "treaties sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "treaties sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "treaties sources declare the TR01 minimisation policy")
    _units(fmt, dict(declared.get("selection") or {}))
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "treaty sources are unauthenticated; no secret is declared")
    return declared


class TreatiesAdapter:
    """Fetch one declared treaty per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # no treaty source takes a credential
        self.source = json.loads(json.dumps(source))
        self.declared = treaties_declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
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
                         "minimisation": MINIMISATION_POLICY},
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

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").casefold()
        url = f"{parts.scheme}://{parts.netloc}{path}"
        ordered = dict(sorted(params.items()))
        response = self.transport(url=url, params=ordered,
                                  headers={"Accept": "text/html, application/sparql-results+json"},
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
            raise SourcePackError("rate_limited", "source is temporarily rate limiting",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"source returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        shown = dict(ordered)
        if "query" in shown:  # a SPARQL query is receipted by its digest, not verbatim
            shown["query"] = "sha256:" + hashlib.sha256(str(shown["query"]).encode()).hexdigest()[:16]
        query = urlencode(sorted(shown.items()))
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests = {}, []
        for name, (path, params) in requests_for(self.format, unit).items():
            raw, receipt = self._get(path, params)
            responses[name] = raw
            requests.append({"name": name, **receipt})
        try:
            records = parse_unit(self.format, responses, unit)
        except TreatiesFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        treaty = next(r["record_key"] for r in records if r["record_kind"] == "treaty")
        receipt = {
            "contract": "noesis-treaty-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "minimisation": MINIMISATION_POLICY,
            "complete_for": [treaty], "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({"id": record["record_key"], "title": record["title"], "url": record["locator"],
                        "language": "en", "published_at": None, "updated_at": record["native_revision"],
                        "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                        "treaty_record": record, "treaty_receipt": receipt})
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: TreatiesAdapter}


def request_key(url: str, params: Mapping[str, Any] | None) -> str:
    """The key an authored native page is stored under: path plus sorted query (a SPARQL query by its digest)."""
    parts = urlsplit(url)
    params = dict(params or {})
    if "query" in params:
        params["query"] = "sha256:" + hashlib.sha256(str(params["query"]).encode()).hexdigest()[:16]
    query = urlencode(sorted(params.items()))
    return parts.path + ("?" + query if query else "")


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by :func:`request_key`; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = request_key(url, params)
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "FORMATS", "LIVE_VERIFICATION", "MINIMISATION",
    "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "REVIEW_BOUNDARY", "TreatiesAdapter", "TreatiesFormatError",
    "fixture_transport", "minimisation_violations", "parse_unit", "replay_native_fixture", "request_key",
    "requests_for", "treaties_declaration",
]

"""Citations inside engineering-safety records, and links to their owners by exact identifier (ES11, #2072).

Extraction reads the verbatim texts a record revision states (required
actions, applicability, findings, probable causes, recommendation texts,
defect-investigation summaries) and keeps each citation with its span:

* CFR parts and sections (``14 CFR 39.13``) and EU acts (``Regulation (EU)
  2018/1139``, ``Regulation (EU) No 748/2012``) -> ``legal.works``
  (:class:`src.kb.legal.LegalStore`) by exact identifier;
* standards (ASTM, SAE, RTCA, API, ASME, NFPA, ISO, IEC, EN) ->
  ``technology.standards`` by exact reference;
* FAA AD numbers (``AD 2024-11-03``) and EASA AD numbers (``EASA AD
  2026-0123R1``) -> directive records of this pack;
* NTSB (``A-26-015``) and CSB (``2024-02-I-TX-R3``) recommendation numbers ->
  recommendation records of this pack;
* NHTSA recall campaign numbers (``26V123000``) -> the Products safety
  feature's NHTSA recall notices (#1916), which own recalls;
* service bulletins -> kept as references; no owner stores them.

Identifier boundaries are the shared ones from
:mod:`src.kb.legal_citations` (``IDENT_START``/``IDENT_END``): a match never
starts or ends inside a word or an identifier path, so path separators
(``/``) continue an identifier. An unresolved citation keeps its text and is
never dropped or resolved by topic similarity. A link records the citing
revision and the cited record's revision it was made against.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.legal_citations import IDENT_END, IDENT_START

CITATION_KINDS = ("legal", "standard", "directive", "recommendation", "recall", "service_bulletin")

_FAA_AD = re.compile(IDENT_START + r"(?:FAA\s+)?AD\s+(?P<num>\d{4}-\d{2}-\d{2})(?P<rev>R\d+)?" + IDENT_END)
_EASA_AD = re.compile(IDENT_START + r"EASA\s+AD\s+(?:No\.?\s+)?(?P<num>\d{4}-\d{4})(?P<rev>R\d+)?" + IDENT_END)
_NTSB_REC = re.compile(IDENT_START + r"(?P<num>[AHMPRI]-\d{2}-\d{1,3})" + IDENT_END)
_CSB_REC = re.compile(IDENT_START + r"(?P<num>\d{4}-\d{2}-I-[A-Z]{2}-R\d+)" + IDENT_END)
_RECALL = re.compile(IDENT_START + r"(?P<num>\d{2}[VETCIX]\d{6})" + IDENT_END)
_CFR = re.compile(
    IDENT_START
    + r"(?P<title>\d{1,2})\s+CFR\s+(?:[Pp]arts?\s+)?(?P<part>\d{1,4})(?:\.(?P<section>\d{1,5}))?"
    + IDENT_END
)
_EU_ACT = re.compile(
    IDENT_START
    + r"(?P<form>Regulation|Directive|Decision)\s+(?:\((?P<tag>EU|EC|EEC|Euratom)\)\s+)?(?P<no>No\.?\s+)?"
    r"(?P<a>\d{1,4})/(?P<b>\d{1,4})"
    + IDENT_END
)
_STANDARD = re.compile(
    IDENT_START
    + r"(?P<ref>(?:ASTM|SAE|RTCA|API|ASME|NFPA|ISO|IEC|EN)(?:/(?:ISO|IEC))?\s+"
    r"(?:(?:RP|Std|Spec|DO|AS|ARP|AMS|MP|B|J)\s?-?)?[A-Z]?\d+[A-Za-z0-9.\-]*(?::\d{4})?)"
)
_SERVICE_BULLETIN = re.compile(
    IDENT_START + r"(?:[Ss]ervice\s+[Bb]ulletin|SB)\s+(?:No\.?\s+)?(?P<num>[A-Z0-9][A-Z0-9-]*\d[A-Z0-9-]*)" + IDENT_END
)
_ACT_LETTER = {"Regulation": "R", "Directive": "L", "Decision": "D"}


def standard_key(value: str) -> str:
    """Exact-reference key for a standard, shared with the Products safety feature."""
    from src.kb.product_safety import standard_key as shared

    return shared(value)


def _celex(match: re.Match[str]) -> str | None:
    a, b = match.group("a"), match.group("b")
    numbered_first = bool(match.group("no")) or (
        match.group("form") == "Regulation" and match.group("tag") in {"EC", "EEC", "Euratom"}
    )
    year, number = (b, a) if numbered_first else (a, b)
    if len(year) == 2:
        year = ("19" if int(year) > 50 else "20") + year
    if len(year) != 4:
        return None
    return f"3{year}{_ACT_LETTER[match.group('form')]}{int(number):04d}"


def extract(text: str, locator: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Citations a text states, verbatim with their span inside the located text; nothing inferred."""
    text = str(text or "")
    found: list[dict[str, Any]] = []

    def add(kind: str, match: re.Match[str], key: str, identifiers: dict[str, Any], raw: str | None = None) -> None:
        raw = raw if raw is not None else match.group(0)
        found.append(
            {
                "kind": kind,
                "raw": raw,
                "reference_key": key,
                "identifiers": identifiers,
                "locator": {**dict(locator), "span": [match.start(), match.start() + len(raw)]},
            }
        )

    easa_spans = []
    for match in _EASA_AD.finditer(text):
        easa_spans.append((match.start(), match.end()))
        identifiers = {"provider": "easa-ad", "native_id": match.group("num")}
        if match.group("rev"):
            identifiers["revision_label"] = match.group("rev")
        add("directive", match, f"easa-ad:{match.group('num')}", identifiers)
    for match in _FAA_AD.finditer(text):
        if any(start <= match.start() < end for start, end in easa_spans):
            continue
        add("directive", match, f"faa-ad:{match.group('num')}", {"provider": "faa-ad", "native_id": match.group("num")})
    for pattern, provider in ((_NTSB_REC, "ntsb"), (_CSB_REC, "csb")):
        for match in pattern.finditer(text):
            add("recommendation", match, f"{provider}:{match.group('num')}",
                {"provider": provider, "native_id": match.group("num")})
    for match in _RECALL.finditer(text):
        add("recall", match, f"nhtsa:{match.group('num')}", {"provider": "nhtsa", "campaign_number": match.group("num")})
    for match in _CFR.finditer(text):
        cited = f"{match.group('title')} CFR {match.group('part')}" + (
            f".{match.group('section')}" if match.group("section") else "")
        add("legal", match, "cfr:" + cited.replace(" CFR ", ":"), {"cfr": cited})
    for match in _EU_ACT.finditer(text):
        celex = _celex(match)
        if celex:
            add("legal", match, f"celex:{celex}",
                {"celex": celex, "celex_basis": "derived from the act's official number"})
    for match in _STANDARD.finditer(text):
        raw = match.group("ref").rstrip(".-")
        end = match.start() + len(raw)
        if end < len(text) and (text[end].isalnum() or text[end] in "_"):
            continue  # the reference runs into a word: not a standard reference
        add("standard", match, standard_key(raw), {"reference": raw}, raw=raw)
    for match in _SERVICE_BULLETIN.finditer(text):
        add("service_bulletin", match, f"service-bulletin:{match.group('num')}", {"number": match.group("num")})
    return found


STANDARDS_READ_SCOPE = "knowledge:standards:read"
LEGAL_READ_SCOPE = "knowledge:legal:read"
PRODUCTS_READ_SCOPE = "knowledge:products:read"


def _table(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def link_citations(conn: Any, namespace: str, *, scopes: Iterable[str], principal_id: str,
                   standards_namespace: str | None = None, legal_namespace: str | None = None,
                   products_namespace: str | None = None) -> dict[str, Any]:
    """Resolve citations by exact identifier; idempotent, append-only and revision-aware.

    Directive and recommendation numbers always resolve against this pack's records. Standards, legal works and
    NHTSA recalls resolve only when their owner's namespace is named, and each named owner needs its read scope.
    A link names the citing revision and the cited record's revision at link time; when the cited record gains a
    revision, linking again adds a link to it and keeps the earlier one.
    """
    from src.kb.engineering_safety_records import (
        WRITE_SCOPE,
        EngineeringSafetyError,
        authorize,
        digest,
        load,
        record_id_for,
        require,
    )
    from src.kb.engineering_safety_store import EngineeringSafetyStore

    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    store = EngineeringSafetyStore(conn, initialize=False)
    store.require_ready()
    standards: dict[str, list[tuple[str, str, str]]] | None = None
    legal = None
    if standards_namespace:
        require(scopes, STANDARDS_READ_SCOPE)
        authorize(standards_namespace, scopes, STANDARDS_READ_SCOPE)
        standards = {}
        if _table(conn, "standard_revisions"):
            for native_id, reference, revision_id in conn.execute(
                    "SELECT r.native_id, r.reference, r.revision_id FROM standard_current c JOIN standard_revisions r "
                    "ON r.revision_id=c.revision_id WHERE c.namespace=?", [standards_namespace]).fetchall():
                standards.setdefault(standard_key(reference), []).append((native_id, reference, revision_id))
    if legal_namespace:
        require(scopes, LEGAL_READ_SCOPE)
        if _table(conn, "legal_works"):
            from src.kb.legal import LegalStore

            legal = LegalStore(conn, initialize=False)
    if products_namespace:
        require(scopes, PRODUCTS_READ_SCOPE)
        authorize(products_namespace, scopes, PRODUCTS_READ_SCOPE)
    created, unresolved = [], []
    now = int(store.now())
    for citation_id, revision_id, kind, raw, key, identifiers in conn.execute(
            "SELECT citation_id, revision_id, kind, raw, reference_key, identifiers_json FROM es_citations "
            "WHERE namespace=? ORDER BY citation_id", [namespace]).fetchall():
        identifiers = load(identifiers, {})
        target: tuple[str, str, str, str | None, str | None, str] | None = None
        if kind in {"directive", "recommendation"}:
            record_kind = "directive" if kind == "directive" else "safety_recommendation"
            record_id = record_id_for(namespace, identifiers["provider"], record_kind, identifiers["native_id"])
            current = store.current_revision_id(namespace, record_id)
            if current:
                target = ("engineering-safety-record", namespace, record_id, current,
                          f"{identifiers['provider']} {identifiers['native_id']}", identifiers["native_id"])
        elif kind == "recall" and products_namespace and _table(conn, "product_safety_current"):
            from src.kb.product_safety import notice_id_for

            notice_id = notice_id_for(products_namespace, "nhtsa", identifiers["campaign_number"])
            row = conn.execute("SELECT revision_id FROM product_safety_current WHERE namespace=? AND notice_id=?",
                               [products_namespace, notice_id]).fetchone()
            if row:
                target = ("product-safety-notice", products_namespace, notice_id, row[0],
                          f"NHTSA recall {identifiers['campaign_number']}", identifiers["campaign_number"])
        elif kind == "standard" and standards is not None:
            hits = standards.get(key) or []
            if len(hits) == 1:
                target = ("standard", str(standards_namespace), f"standard:{hits[0][0]}", hits[0][2], hits[0][1],
                          hits[0][1])
        elif kind == "legal" and legal is not None:
            from src.kb.legal import LegalError

            for label in ("celex", "cfr"):
                value = identifiers.get(label)
                if not value:
                    continue
                try:
                    found = legal.lookup(str(legal_namespace), scopes=scopes, identifier=value)
                except LegalError as exc:
                    raise EngineeringSafetyError(getattr(exc, "code", "unauthorized"), str(exc)) from exc
                if found["status"] == "found":
                    work = found["works"][0]
                    target = ("legal-work", str(legal_namespace), work["work_id"], None, work.get("title"), value)
                    break
        if target is None:
            unresolved.append({"citation_id": citation_id, "revision_id": revision_id, "kind": kind, "raw": raw,
                               "note": "kept as the cited text; never resolved by topic similarity"})
            continue
        target_kind, target_namespace, target_id, target_revision, label, identifier = target
        link_id = "es-citation-link:" + digest([namespace, citation_id, target_id, target_revision])[:24]
        inserted = conn.execute(
            "INSERT OR IGNORE INTO es_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, citation_id, revision_id, target_kind, target_namespace, target_id, target_revision,
             label, "cited", identifier, principal_id, now]).fetchall()
        if inserted:
            created.append({"link_id": link_id, "citation_id": citation_id, "revision_id": revision_id, "raw": raw,
                            "target_kind": target_kind, "target_id": target_id,
                            "target_revision_id": target_revision})
    if created:
        store.bump(namespace)
    return {
        "namespace": namespace,
        "linked": created,
        "unresolved": unresolved,
        "owners_consulted": {k: v for k, v in {"standards": standards_namespace, "legal": legal_namespace,
                                                "products": products_namespace}.items() if v},
        "policy": "exact identifiers only (AD, recommendation and campaign numbers, standard references, CELEX and "
        "CFR); an unresolved citation keeps its text; links name the citing and the cited revision",
    }


def extract_all(items: Iterable[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Citations across several located texts, one per (reference, raw text, locator)."""
    seen: set[tuple[str, str, str]] = set()
    result = []
    for text, locator in items:
        for citation in extract(text, locator):
            key = (citation["reference_key"], citation["raw"], repr(sorted(citation["locator"].items())))
            if key not in seen:
                seen.add(key)
                result.append(citation)
    return result

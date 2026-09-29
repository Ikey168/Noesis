"""Sanctions-list acquisition for the Legal pack's ``sanctions`` feature (#1907, S01/S03/S04).

One native connector, ``sanctions-list``, fetches one documented,
machine-readable full-list publication per source and parses it into a
*snapshot*: the list's publication date, the file digest and every entry as the
list states it. Four documented formats are supported:

* ``eu-fsf-xml-1.1`` - the EU consolidated financial sanctions list (FSF XML 1.1);
* ``un-sc-xml`` - the UN Security Council Consolidated List XML;
* ``ofac-sdn-xml`` - the OFAC Sanctions List Service legacy SDN XML;
* ``uk-sanctions-list-xml`` - the FCDO UK Sanctions List XML.

Lists are never merged: each entry keeps its own list and per-list identifier
(EU reference number, UN permanent reference number, OFAC UID, UK unique ID),
names, identifiers, addresses and remarks exactly as written. Nothing here
compares entries across lists, scores a name, or states a screening or
compliance result; listing revisions and delistings are derived later, by
comparing successive snapshots in :mod:`src.kb.sanctions`.

A snapshot is all-or-nothing: an entry count above the run's result budget is a
``budget_exhausted`` failure rather than a truncated snapshot, because a partial
list would read as delistings.

``PROVIDER_CONTRACTS`` records the S01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason). The EU Sanctions Map
has no documented machine-readable interface and is not scraped.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
SNAPSHOT_CONTRACT = "noesis-sanctions-snapshot-v1"
CONNECTOR = "sanctions-list"
LISTS = ("eu", "un", "ofac", "uk")
FORMATS = {
    "eu-fsf-xml-1.1": "eu",
    "un-sc-xml": "un",
    "ofac-sdn-xml": "ofac",
    "uk-sanctions-list-xml": "uk",
}
PARTY_KINDS = ("person", "entity", "vessel", "aircraft", "unknown")
IDENTIFIER_KINDS = (
    "passport",
    "national_id",
    "imo",
    "lei",
    "registration_number",
    "tax_id",
    "swift_bic",
    "call_sign",
    "other",
)
MAX_ENTRIES = 50_000
REVIEW_BOUNDARY = (
    "List statements only: no screening result, sanctions or AML compliance determination or legal "
    "advice, and a similar name is never treated as the same party."
)

# S01 access decisions. Terms and endpoints below are recorded from the
# providers' published documentation; items marked ``verify`` still need a
# check against the live terms before a live run is accepted.
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eu-fsf": {
        "list_id": "eu",
        "publisher": "European Commission (DG FISMA), Financial Sanctions Files (FSF)",
        "access": "Public full-list file download (XML 1.1, also CSV/PDF) linked from data.europa.eu; the documented "
        "public download token is part of the published URL, not a credential",
        "format": "eu-fsf-xml-1.1",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one full file)",
        "cadence": "republished whenever a Council or Commission act amends a listing (often several times a month)",
        "publication_model": "full snapshots only; no delta file",
        "revisions": "not expressed in the file; derived by comparing successive snapshots",
        "delistings": "an entry disappears from the next file; the file carries no delisting record",
        "identifiers": [
            "EU reference number (euReferenceNumber)",
            "FSF logicalId",
            "UN reference (unitedNationId)",
        ],
        "aliases": [
            "nameAlias (wholeName, language, strong flag)",
            "identification (passport, id, regnumber, ...)",
            "birthdate",
            "citizenship",
            "address",
        ],
        "legal_act_reference": "per-entry regulation element: numberTitle, publicationUrl (EUR-Lex CELEX link), "
        "programme, entry into force; resolved to CELLAR works by exact CELEX",
        "terms": "Commission reuse policy (Decision 2011/833/EU); verify the FSF page's own notice",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "eu-sanctions-map": {
        "list_id": "eu",
        "publisher": "European Commission, EU Sanctions Map (sanctionsmap.eu)",
        "access": "interactive web application; no documented machine-readable interface was found",
        "access_decision": "not-implemented",
        "reason": "no documented machine-readable interface is verified; the site is never scraped. Programme and "
        "legal-act context comes from the FSF file and CELLAR instead",
    },
    "un-sc": {
        "list_id": "un",
        "publisher": "United Nations Security Council",
        "access": "Public consolidated list XML (scsanctions.un.org/resources/xml/en/consolidated.xml); HTML and PDF "
        "renderings exist",
        "format": "un-sc-xml",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one full file)",
        "cadence": "updated after each committee listing, amendment or delisting",
        "publication_model": "full snapshots only (dateGenerated); press releases announce changes separately",
        "revisions": "VERSIONNUM and LAST_DAY_UPDATED per entry; revision chain derived by snapshot comparison",
        "delistings": "entry removed from the next file; delisting press releases are not parsed",
        "identifiers": ["permanent reference number (REFERENCE_NUMBER)", "DATAID"],
        "aliases": [
            "INDIVIDUAL_ALIAS/ENTITY_ALIAS with QUALITY (Good/Low)",
            "NAME_ORIGINAL_SCRIPT",
            "INDIVIDUAL_DOCUMENT (passport, national ID)",
            "date and place of birth",
            "addresses",
        ],
        "legal_act_reference": "regime (UN_LIST_TYPE) only; the Security Council resolutions are not CELLAR works "
        "and are kept as source citation strings",
        "terms": "United Nations website terms of use; verify",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet; confirm the download does not redirect cross-host",
    },
    "ofac-sls": {
        "list_id": "ofac",
        "publisher": "U.S. Department of the Treasury, Office of Foreign Assets Control (Sanctions List Service)",
        "access": "Sanctions List Service exports (legacy SDN.XML; SDN_ADVANCED.XML and delta files also exist)",
        "format": "ofac-sdn-xml",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one full file)",
        "cadence": "published with each OFAC action (Recent Actions)",
        "publication_model": "full snapshots; SLS also publishes delta files that are not used here",
        "revisions": "Publish_Date per file; the legacy XML carries no designation dates; revisions are derived",
        "delistings": "entry removed from the next file",
        "identifiers": ["OFAC UID (uid)", "programme tags (programList)"],
        "aliases": [
            "akaList with type and category (strong/weak)",
            "idList (passport, IMO, registration, ...)",
            "dateOfBirthList",
            "addressList",
            "vesselInfo",
        ],
        "legal_act_reference": "programme tags only; executive orders and statutes are not CELLAR works",
        "terms": "U.S. government work (public domain); verify SLS terms",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; SLS downloads may be served through a cross-host redirect, which the "
        "runtime transport refuses (network_policy) until a same-host export URL is verified",
    },
    "uk-sanctions-list": {
        "list_id": "uk",
        "publisher": "UK Foreign, Commonwealth & Development Office (UK Sanctions List)",
        "access": "Public UK Sanctions List downloads (XML, CSV, ODS) on sanctionslist.fcdo.gov.uk",
        "format": "uk-sanctions-list-xml",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "pagination": "none (one full file)",
        "cadence": "updated with each designation, variation or revocation",
        "publication_model": "full snapshots only",
        "revisions": "LastUpdated and DateDesignated per entry; revision chain derived by snapshot comparison",
        "delistings": "entry removed from the next file",
        "identifiers": [
            "unique ID (UniqueID)",
            "OFSI group ID",
            "UN reference number where UN-derived",
        ],
        "aliases": [
            "Names with NameType and AliasStrength",
            "NonLatinNames with script and language",
            "passport and national identifier details",
            "ship IMO numbers",
            "addresses",
        ],
        "legal_act_reference": "regime name; UK regulations made under SAMLA 2018 are not CELLAR works and stay "
        "source citation strings",
        "terms": "Open Government Licence v3.0; verify on the FCDO page",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; verify the current download URL and XML schema before a live run",
    },
}


class SanctionsFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _xml(raw: bytes):
    import defusedxml.ElementTree as ET

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 100_000_000:
        raise SanctionsFormatError("input_limit", "list file is missing or oversized")
    try:
        return ET.fromstring(raw, forbid_entities=True, forbid_external=True)
    except Exception as exc:  # noqa: BLE001 - any XML failure is provider drift
        raise SanctionsFormatError(
            "schema_drift", "list file is not well-formed XML"
        ) from exc


def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _children(node, name: str) -> list:
    return [child for child in node if _tag(child) == name]


def _child(node, name: str):
    found = _children(node, name)
    return found[0] if found else None


def _text(node, name: str | None = None) -> str | None:
    target = node if name is None else _child(node, name)
    if target is None:
        return None
    value = " ".join("".join(target.itertext()).split())
    return value or None


def _values(node, container: str, item: str = "VALUE") -> list[str]:
    holder = _child(node, container)
    return (
        []
        if holder is None
        else [v for v in (_text(c) for c in _children(holder, item)) if v]
    )


def _iso_day(value: Any) -> str | None:
    """A full calendar date from ISO or DD/MM/YYYY / MM/DD/YYYY text; partial dates stay None."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    return None


def _day_from(fmt: str, value: Any) -> str | None:
    try:
        return datetime.strptime(str(value or "").strip(), fmt).date().isoformat()
    except ValueError:
        return None


def _identifier(
    kind_text: Any, value: Any, *, country: Any = None, note: Any = None
) -> dict[str, Any] | None:
    number = " ".join(str(value or "").split())
    if not number:
        return None
    label = str(kind_text or "").strip()
    lowered = label.casefold()
    if "passport" in lowered:
        kind = "passport"
    elif (
        lowered in {"id", "nationalid", "national id", "national identification number"}
        or "national id" in lowered
        or "identification number" in lowered
    ):
        kind = "national_id"
    elif "imo" in lowered or re.fullmatch(r"IMO\s*\d{7}", number, re.I):
        kind = "imo"
    elif (
        lowered in {"lei", "legal entity number"}
        or "legal entity identifier" in lowered
    ):
        kind = "lei"
    elif "registration" in lowered or lowered in {
        "regnumber",
        "company number",
        "business registration number",
    }:
        kind = "registration_number"
    elif "tax" in lowered or lowered in {"inn", "tin"}:
        kind = "tax_id"
    elif "swift" in lowered or "bic" in lowered:
        kind = "swift_bic"
    elif "call sign" in lowered:
        kind = "call_sign"
    else:
        kind = "other"
    if kind == "imo":
        digits = re.sub(r"\D", "", number)
        number = digits if len(digits) == 7 else number
    return {
        "kind": kind,
        "value": number,
        "source_type": label or None,
        "country": " ".join(str(country or "").split()) or None,
        "note": " ".join(str(note or "").split()) or None,
    }


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    """Deterministic entry shape: sorted, de-duplicated lists; the order a list writes is not a statement."""
    for key in (
        "names",
        "identifiers",
        "addresses",
        "programmes",
        "legal_basis",
        "dates_of_birth",
        "nationalities",
        "remarks",
    ):
        items = entry.get(key) or []
        unique = {
            json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in items
        }
        entry[key] = [unique[k] for k in sorted(unique)]
    if entry["party_kind"] not in PARTY_KINDS:
        entry["party_kind"] = "unknown"
    if not entry["entry_id"]:
        raise SanctionsFormatError(
            "schema_drift", "list entry has no per-list identifier"
        )
    return entry


def _entry(
    list_id: str, entry_id: Any, party_kind: str, **fields: Any
) -> dict[str, Any]:
    base = {
        "list_id": list_id,
        "entry_id": " ".join(str(entry_id or "").split()),
        "party_kind": party_kind,
        "names": [],
        "identifiers": [],
        "addresses": [],
        "programmes": [],
        "legal_basis": [],
        "dates_of_birth": [],
        "nationalities": [],
        "remarks": [],
        "listed_on": None,
        "amended_on": None,
        "cross_references": {},
    }
    base.update(fields)
    return _clean(base)


# ------------------------------------------------------------------ EU FSF


def _celex_from(url: Any) -> str | None:
    match = re.search(r"CELEX(?::|%3A)([0-9A-Z()._-]{5,50})", str(url or ""), re.I)
    return match.group(1).upper() if match else None


def _eli_from(url: Any) -> str | None:
    match = re.search(r"(https?://data\.europa\.eu/eli/[^\s\"<>]+)", str(url or ""))
    return match.group(1) if match else None


def parse_eu_fsf(raw: bytes) -> dict[str, Any]:
    root = _xml(raw)
    if _tag(root) != "export":
        raise SanctionsFormatError("schema_drift", "EU FSF file root must be <export>")
    published = _iso_day(root.get("generationDate"))
    if not published:
        raise SanctionsFormatError("schema_drift", "EU FSF file lacks a generationDate")
    entries = []
    for node in _children(root, "sanctionEntity"):
        subject = _child(node, "subjectType")
        code = str(
            (subject.get("code") if subject is not None else "") or ""
        ).casefold()
        kind = {"person": "person", "enterprise": "entity", "vessel": "vessel"}.get(
            code, "unknown"
        )
        names = [
            {
                "name": " ".join(str(a.get("wholeName") or "").split()),
                "kind": "name",
                "quality": "strong"
                if str(a.get("strong")).lower() == "true"
                else "weak"
                if str(a.get("strong")).lower() == "false"
                else None,
                "script": None,
                "language": (a.get("nameLanguage") or None),
            }
            for a in _children(node, "nameAlias")
            if str(a.get("wholeName") or "").strip()
        ]
        identifiers = [
            i
            for i in (
                _identifier(
                    x.get("identificationTypeDescription")
                    or x.get("identificationTypeCode"),
                    x.get("number"),
                    country=x.get("countryIso2Code") or x.get("countryDescription"),
                    note=x.get("remark"),
                )
                for x in _children(node, "identification")
            )
            if i
        ]
        births = [
            b.get("birthdate")
            or "-".join(
                p
                for p in (b.get("year"), b.get("monthOfYear"), b.get("dayOfMonth"))
                if p
            )
            for b in _children(node, "birthdate")
        ]
        addresses = [
            {
                "text": ", ".join(
                    p
                    for p in (
                        a.get("street"),
                        a.get("poBox"),
                        a.get("zipCode"),
                        a.get("city"),
                        a.get("region"),
                        a.get("place"),
                    )
                    if p
                )
                or None,
                "country": a.get("countryIso2Code")
                or a.get("countryDescription")
                or None,
            }
            for a in _children(node, "address")
        ]
        programmes, bases = [], []
        for regulation in _children(node, "regulation"):
            url = _text(regulation, "publicationUrl")
            if regulation.get("programme"):
                programmes.append({"code": regulation.get("programme"), "name": None})
            role = (
                "amending-act"
                if str(regulation.get("regulationType") or "").casefold() == "amendment"
                else "listing-act"
            )
            bases.append(
                {
                    "citation": regulation.get("numberTitle") or url or "unnamed act",
                    "role": role,
                    "celex": _celex_from(url),
                    "eli": _eli_from(url),
                    "url": url,
                    "regulation_type": regulation.get("regulationType"),
                    "publication_date": _iso_day(regulation.get("publicationDate")),
                    "entry_into_force": _iso_day(regulation.get("entryIntoForceDate")),
                }
            )
        entries.append(
            _entry(
                "eu",
                node.get("euReferenceNumber") or f"logical:{node.get('logicalId')}",
                kind,
                names=names,
                identifiers=identifiers,
                addresses=[a for a in addresses if a["text"] or a["country"]],
                programmes=programmes,
                legal_basis=bases,
                dates_of_birth=[b for b in births if b],
                nationalities=[
                    c.get("countryIso2Code") or c.get("countryDescription")
                    for c in _children(node, "citizenship")
                    if c.get("countryIso2Code") or c.get("countryDescription")
                ],
                remarks=[t for t in (_text(r) for r in _children(node, "remark")) if t],
                listed_on=_iso_day(node.get("designationDate")),
                cross_references={
                    k: v
                    for k, v in {
                        "fsf_logical_id": node.get("logicalId"),
                        "un_reference": node.get("unitedNationId") or None,
                    }.items()
                    if v
                },
            )
        )
    return {
        "list_id": "eu",
        "format": "eu-fsf-xml-1.1",
        "publication_date": published,
        "entries": entries,
    }


# ------------------------------------------------------------------ UN


def parse_un_sc(raw: bytes) -> dict[str, Any]:
    root = _xml(raw)
    if _tag(root) != "CONSOLIDATED_LIST":
        raise SanctionsFormatError(
            "schema_drift", "UN file root must be <CONSOLIDATED_LIST>"
        )
    published = _iso_day(root.get("dateGenerated"))
    if not published:
        raise SanctionsFormatError("schema_drift", "UN file lacks dateGenerated")
    entries = []
    for group, item, kind in (
        ("INDIVIDUALS", "INDIVIDUAL", "person"),
        ("ENTITIES", "ENTITY", "entity"),
    ):
        holder = _child(root, group)
        for node in [] if holder is None else _children(holder, item):
            primary = " ".join(
                p
                for p in (
                    _text(node, f)
                    for f in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")
                )
                if p
            )
            names = [
                {
                    "name": primary,
                    "kind": "primary",
                    "quality": None,
                    "script": None,
                    "language": None,
                }
            ]
            if _text(node, "NAME_ORIGINAL_SCRIPT"):
                names.append(
                    {
                        "name": _text(node, "NAME_ORIGINAL_SCRIPT"),
                        "kind": "original-script",
                        "quality": None,
                        "script": "original",
                        "language": None,
                    }
                )
            for alias in _children(node, f"{item}_ALIAS"):
                if _text(alias, "ALIAS_NAME"):
                    quality = (_text(alias, "QUALITY") or "").casefold() or None
                    names.append(
                        {
                            "name": _text(alias, "ALIAS_NAME"),
                            "kind": "alias",
                            "quality": quality,
                            "script": None,
                            "language": None,
                        }
                    )
            identifiers = [
                i
                for i in (
                    _identifier(
                        _text(doc, "TYPE_OF_DOCUMENT"),
                        _text(doc, "NUMBER"),
                        country=_text(doc, "ISSUING_COUNTRY"),
                        note=_text(doc, "NOTE"),
                    )
                    for doc in _children(node, f"{item}_DOCUMENT")
                )
                if i
            ]
            births = [
                _text(b, "DATE") or _text(b, "YEAR")
                for b in _children(node, f"{item}_DATE_OF_BIRTH")
            ]
            addresses = [
                {
                    "text": ", ".join(
                        p
                        for p in (
                            _text(a, "STREET"),
                            _text(a, "CITY"),
                            _text(a, "STATE_PROVINCE"),
                        )
                        if p
                    )
                    or None,
                    "country": _text(a, "COUNTRY"),
                }
                for a in _children(node, f"{item}_ADDRESS")
            ]
            regime = _text(node, "UN_LIST_TYPE")
            entries.append(
                _entry(
                    "un",
                    _text(node, "REFERENCE_NUMBER"),
                    kind,
                    names=[n for n in names if n["name"]],
                    identifiers=identifiers,
                    addresses=[a for a in addresses if a["text"] or a["country"]],
                    programmes=[{"code": regime, "name": None}] if regime else [],
                    legal_basis=[],
                    dates_of_birth=[b for b in births if b],
                    nationalities=_values(node, "NATIONALITY"),
                    remarks=[t for t in (_text(node, "COMMENTS1"),) if t],
                    listed_on=_iso_day(_text(node, "LISTED_ON")),
                    amended_on=max(
                        (
                            d
                            for d in (
                                _iso_day(v) for v in _values(node, "LAST_DAY_UPDATED")
                            )
                            if d
                        ),
                        default=None,
                    ),
                    cross_references={
                        k: v
                        for k, v in {
                            "un_data_id": _text(node, "DATAID"),
                            "un_version": _text(node, "VERSIONNUM"),
                        }.items()
                        if v
                    },
                )
            )
    return {
        "list_id": "un",
        "format": "un-sc-xml",
        "publication_date": published,
        "entries": entries,
    }


# ------------------------------------------------------------------ OFAC


def parse_ofac_sdn(raw: bytes) -> dict[str, Any]:
    root = _xml(raw)
    if _tag(root) != "sdnList":
        raise SanctionsFormatError("schema_drift", "OFAC file root must be <sdnList>")
    info = _child(root, "publshInformation")
    published = (
        _day_from("%m/%d/%Y", _text(info, "Publish_Date")) if info is not None else None
    )
    if not published:
        raise SanctionsFormatError("schema_drift", "OFAC file lacks Publish_Date")
    entries = []
    for node in _children(root, "sdnEntry"):
        sdn_type = (_text(node, "sdnType") or "").casefold()
        kind = {
            "individual": "person",
            "entity": "entity",
            "vessel": "vessel",
            "aircraft": "aircraft",
        }.get(sdn_type, "unknown")
        primary = ", ".join(
            p for p in (_text(node, "lastName"), _text(node, "firstName")) if p
        )
        names = [
            {
                "name": primary,
                "kind": "primary",
                "quality": None,
                "script": None,
                "language": None,
            }
        ]
        akas = _child(node, "akaList")
        for aka in [] if akas is None else _children(akas, "aka"):
            name = ", ".join(
                p for p in (_text(aka, "lastName"), _text(aka, "firstName")) if p
            )
            if name:
                names.append(
                    {
                        "name": name,
                        "kind": (_text(aka, "type") or "a.k.a.").casefold(),
                        "quality": (_text(aka, "category") or "").casefold() or None,
                        "script": None,
                        "language": None,
                    }
                )
        ids = _child(node, "idList")
        identifiers = [
            i
            for i in (
                _identifier(
                    _text(x, "idType"),
                    _text(x, "idNumber"),
                    country=_text(x, "idCountry"),
                )
                for x in ([] if ids is None else _children(ids, "id"))
            )
            if i
        ]
        vessel = _child(node, "vesselInfo")
        if vessel is not None and _text(vessel, "callSign"):
            identifiers.append(
                _identifier(
                    "Call Sign",
                    _text(vessel, "callSign"),
                    country=_text(vessel, "vesselFlag"),
                )
            )
        births = _child(node, "dateOfBirthList")
        addresses = _child(node, "addressList")
        programs = _child(node, "programList")
        entries.append(
            _entry(
                "ofac",
                _text(node, "uid"),
                kind,
                names=[n for n in names if n["name"]],
                identifiers=identifiers,
                addresses=[
                    a
                    for a in (
                        {
                            "text": ", ".join(
                                p
                                for p in (
                                    _text(x, "address1"),
                                    _text(x, "city"),
                                    _text(x, "stateOrProvince"),
                                )
                                if p
                            )
                            or None,
                            "country": _text(x, "country"),
                        }
                        for x in (
                            [] if addresses is None else _children(addresses, "address")
                        )
                    )
                    if a["text"] or a["country"]
                ],
                programmes=[
                    {"code": p, "name": None}
                    for p in (
                        []
                        if programs is None
                        else [_text(c) for c in _children(programs, "program")]
                    )
                    if p
                ],
                dates_of_birth=[
                    d
                    for d in (
                        []
                        if births is None
                        else [
                            _text(b, "dateOfBirth")
                            for b in _children(births, "dateOfBirthItem")
                        ]
                    )
                    if d
                ],
                remarks=[t for t in (_text(node, "remarks"),) if t],
                cross_references={
                    k: v
                    for k, v in {
                        "vessel_type": _text(vessel, "vesselType")
                        if vessel is not None
                        else None
                    }.items()
                    if v
                },
            )
        )
    return {
        "list_id": "ofac",
        "format": "ofac-sdn-xml",
        "publication_date": published,
        "entries": entries,
    }


# ------------------------------------------------------------------ UK


def parse_uk_sanctions_list(raw: bytes) -> dict[str, Any]:
    root = _xml(raw)
    if _tag(root) != "Designations":
        raise SanctionsFormatError(
            "schema_drift", "UK Sanctions List root must be <Designations>"
        )
    generated = _text(root, "DateGenerated")
    published = _iso_day(generated) or _day_from("%d/%m/%Y", generated)
    if not published:
        raise SanctionsFormatError(
            "schema_drift", "UK Sanctions List lacks DateGenerated"
        )
    entries = []
    for node in _children(root, "Designation"):
        kind = {"individual": "person", "entity": "entity", "ship": "vessel"}.get(
            (_text(node, "IndividualEntityShip") or "").casefold(), "unknown"
        )
        names = []
        holder = _child(node, "Names")
        for name in [] if holder is None else _children(holder, "Name"):
            parts = [_text(name, f"Name{i}") for i in (1, 2, 3, 4, 5)]
            full = " ".join(p for p in parts if p) or ""
            full = (full + " " + (_text(name, "Name6") or "")).strip()
            name_type = (_text(name, "NameType") or "").casefold()
            names.append(
                {
                    "name": full,
                    "kind": "primary"
                    if name_type == "primary name"
                    else name_type or "alias",
                    "quality": (_text(name, "AliasStrength") or "").casefold() or None,
                    "script": None,
                    "language": None,
                }
            )
        non_latin = _child(node, "NonLatinNames")
        for name in [] if non_latin is None else _children(non_latin, "NonLatinName"):
            names.append(
                {
                    "name": _text(name, "NameNonLatinScript"),
                    "kind": "non-latin",
                    "quality": None,
                    "script": _text(name, "NonLatinScriptType"),
                    "language": _text(name, "NonLatinScriptLanguage"),
                }
            )
        identifiers, births, nationalities = [], [], []
        details = _child(node, "IndividualDetails")
        for person in [] if details is None else _children(details, "Individual"):
            births += _values(person, "DOBs", "DOB")
            nationalities += _values(person, "Nationalities", "Nationality")
            for container, item, number, info, label in (
                (
                    "PassportDetails",
                    "PassportDetail",
                    "PassportNumber",
                    "PassportAdditionalInformation",
                    "Passport",
                ),
                (
                    "NationalIdentifierDetails",
                    "NationalIdentifierDetail",
                    "NationalIdentifierNumber",
                    "NationalIdentifierAdditionalInformation",
                    "National Identifier",
                ),
            ):
                box = _child(person, container)
                for item_node in [] if box is None else _children(box, item):
                    found = _identifier(
                        label, _text(item_node, number), note=_text(item_node, info)
                    )
                    if found:
                        identifiers.append(found)
        ships = _child(node, "ShipDetails")
        for ship in [] if ships is None else _children(ships, "Ship"):
            identifiers += [
                i
                for i in (
                    _identifier("IMO number", v)
                    for v in _values(ship, "IMONumbers", "IMONumber")
                )
                if i
            ]
        entity = _child(node, "EntityDetails")
        if entity is not None:
            identifiers += [
                i
                for i in (
                    _identifier("Business Registration Number", v)
                    for v in _values(
                        entity,
                        "BusinessRegistrationNumbers",
                        "BusinessRegistrationNumber",
                    )
                )
                if i
            ]
        addresses = []
        box = _child(node, "Addresses")
        for address in [] if box is None else _children(box, "Address"):
            text = ", ".join(
                p for p in (_text(address, f"AddressLine{i}") for i in range(1, 7)) if p
            )
            addresses.append(
                {"text": text or None, "country": _text(address, "AddressCountry")}
            )
        regime = _text(node, "RegimeName")
        births = [_day_from("%d/%m/%Y", b) or b for b in births]
        entries.append(
            _entry(
                "uk",
                _text(node, "UniqueID"),
                kind,
                names=[n for n in names if n["name"]],
                identifiers=identifiers,
                addresses=[a for a in addresses if a["text"] or a["country"]],
                programmes=[{"code": regime, "name": regime}] if regime else [],
                dates_of_birth=births,
                nationalities=nationalities,
                remarks=[
                    t
                    for t in (
                        _text(node, "OtherInformation"),
                        _text(node, "UKStatementofReasons"),
                    )
                    if t
                ],
                listed_on=_iso_day(_text(node, "DateDesignated"))
                or _day_from("%d/%m/%Y", _text(node, "DateDesignated")),
                amended_on=_iso_day(_text(node, "LastUpdated"))
                or _day_from("%d/%m/%Y", _text(node, "LastUpdated")),
                cross_references={
                    k: v
                    for k, v in {
                        "ofsi_group_id": _text(node, "OFSIGroupID"),
                        "un_reference": _text(node, "UNReferenceNumber"),
                        "designation_source": _text(node, "DesignationSource"),
                        "sanctions_imposed": _text(node, "SanctionsImposed"),
                    }.items()
                    if v
                },
            )
        )
    return {
        "list_id": "uk",
        "format": "uk-sanctions-list-xml",
        "publication_date": published,
        "entries": entries,
    }


PARSERS: dict[str, Callable[[bytes], dict[str, Any]]] = {
    "eu-fsf-xml-1.1": parse_eu_fsf,
    "un-sc-xml": parse_un_sc,
    "ofac-sdn-xml": parse_ofac_sdn,
    "uk-sanctions-list-xml": parse_uk_sanctions_list,
}


def parse_snapshot(format_id: str, raw: bytes) -> dict[str, Any]:
    """Parse one full-list publication into a snapshot; duplicate per-list identifiers are drift."""
    if format_id not in PARSERS:
        raise SanctionsFormatError(
            "unsupported_format", f"unsupported sanctions list format {format_id!r}"
        )
    parsed = PARSERS[format_id](raw)
    ids = [entry["entry_id"] for entry in parsed["entries"]]
    if len(ids) != len(set(ids)):
        raise SanctionsFormatError("schema_drift", "list repeats a per-list identifier")
    if len(ids) > MAX_ENTRIES:
        raise SanctionsFormatError(
            "input_limit", "list exceeds the snapshot entry ceiling"
        )
    parsed["entries"] = sorted(parsed["entries"], key=lambda e: e["entry_id"])
    parsed["file_sha256"] = hashlib.sha256(raw).hexdigest()
    parsed["entries_sha256"] = _digest(parsed["entries"])
    parsed["entry_count"] = len(ids)
    parsed["contract"] = SNAPSHOT_CONTRACT
    return parsed


# ------------------------------------------------------------------ runtime adapter


def sanctions_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("sanctions") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or declared.get("list") != FORMATS[fmt]:
        raise SourcePackError(
            "invalid_mapping",
            "sanctions sources declare a supported format and its list",
        )
    return declared


class SanctionsListAdapter:
    """Fetch one full-list file from the declared endpoint and emit every entry of the snapshot."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = sanctions_declaration(self.source)
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only,
            # a byte ceiling and the source's timeout.
            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "sanctions": {
                "list": self.declared["list"],
                "format": self.declared["format"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "sanctions runs fetch the declared file, not ad-hoc queries",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        if cursor is not None:
            raise SourcePackError(
                "cursor_drift",
                "a sanctions list is one full file; there is no next page",
            )
        url = self.source["endpoint"]
        host = (urlsplit(url).hostname or "").casefold()
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": "application/xml, text/xml"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "sanctions list was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "list file exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"list download refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"list provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError(
                "schema_drift", f"list download returned HTTP {status}"
            )
        try:
            snapshot = parse_snapshot(self.declared["format"], raw)
        except SanctionsFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if snapshot["entry_count"] > limit:
            # Never a truncated snapshot: missing entries would read as delistings.
            raise SourcePackError(
                "budget_exhausted", "list has more entries than the run's result budget"
            )
        header = {
            k: snapshot[k]
            for k in (
                "contract",
                "list_id",
                "format",
                "publication_date",
                "file_sha256",
                "entries_sha256",
                "entry_count",
            )
        }
        records = []
        for entry in snapshot["entries"]:
            primary = next(
                (n["name"] for n in entry["names"] if n["kind"] in {"primary", "name"}),
                None,
            )
            records.append(
                {
                    "id": f"{entry['list_id']}:{entry['entry_id']}",
                    "title": f"{entry['list_id'].upper()} {entry['entry_id']}: {primary or 'unnamed entry'}",
                    "url": url,
                    "language": "en",
                    "published_at": snapshot["publication_date"],
                    "content": json.dumps(entry, sort_keys=True, ensure_ascii=False),
                    "sanctions_snapshot": header,
                    "sanctions_entry": entry,
                }
            )
        receipt = {
            "status": status,
            "list_id": snapshot["list_id"],
            "publication_date": snapshot["publication_date"],
            "file_sha256": snapshot["file_sha256"],
            "entries": snapshot["entry_count"],
            "final_page": True,
        }
        return RuntimePage(tuple(records), None, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: SanctionsListAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored list files keyed by URL path (+ query)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = SanctionsListAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    page = adapter.fetch_page(
        {
            "operation": min(source["operations"]),
            "parameters": {},
            "limit": int(source["budgets"]["max_results"]),
        },
        cursor=None,
    )
    return [dict(item) for item in page.records]

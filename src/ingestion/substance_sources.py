"""Substance identity and regulatory sources for the Chemicals and Substances pack (#2212, CH01 and CH03-CH06).

Four providers run as sources of the ``chemicals-substances`` source pack
(``config/source_packs/chemicals.json``, connector ``substances``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each
under a recorded access contract (:data:`PROVIDER_CONTRACTS`):

* **PubChem PUG-REST** (``pubchem``, #2290) - compound identity only: CID,
  InChI, InChIKey, preferred and IUPAC names, and depositor synonyms (CAS
  numbers among them, every distinct one kept and flagged as a conflict when
  depositors disagree), plus the record's modification date as its version.
* **ECHA CHEM - CLP** (``echa-clp``, #2294) - harmonised classifications
  (CLP Annex VI) by index number, one dated revision per ATP that introduced
  or amended them, and notified C&L inventory aggregates quoted as published.
* **ECHA CHEM - REACH** (``echa-reach``, #2296) - registration status, SVHC
  Candidate List events, Annex XIV authorisation and Annex XVII restriction
  entries, each event dated with its legal act; removals are events.
* **US EPA CompTox** (``comptox``, #2299) - DTXSID identity and ToxValDB
  data points as published, through the CTX API with an API key held as the
  ``NOESIS_COMPTOX_API_KEY`` secret reference.

Every selection is explicit (the bounded substance set of
``docs/roadmaps/chemicals-substances-source-audit.md``): one page per
selected substance, never an enumeration. Only identity and regulatory-status
fields are read; :data:`EXCLUDED_FIELDS` (synthesis, preparation, reaction
and manufacturing content, bioassays, dossier contents) are never parsed and,
when a response carries them, are reported as dropped in the page receipt.

Every provider is ``unverified-live`` until a dated live run (#2317); request
paths and response field names marked *verify* come from public documentation
(PubChem, CTX) or are authored in the shape of ECHA CHEM's undocumented
portal JSON and must be checked before a live run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.substances_records import (
    SubstanceError,
    cas_valid,
    ec_valid,
    identifier_key,
    statement,
)

CONNECTOR = "substances"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("pubchem", "echa-clp", "echa-reach", "comptox")
PROVIDER_HOSTS = {
    "pubchem": ("pubchem.ncbi.nlm.nih.gov",),
    "echa-clp": ("chem.echa.europa.eu",),
    "echa-reach": ("chem.echa.europa.eu",),
    "comptox": ("api-ccte.epa.gov",),
}
ECHA_ATTRIBUTION = "Source: European Chemicals Agency, https://echa.europa.eu/ (ECHA CHEM)"
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pubchem": {
        "publisher": "U.S. National Library of Medicine, NCBI (PubChem)",
        "access": "PUG-REST, anonymous HTTPS GET",
        "endpoints": ["/rest/pug/compound/cid/{cid}/property/Title,IUPACName,MolecularFormula,InChI,InChIKey/JSON",
                      "/rest/pug/compound/cid/{cid}/synonyms/JSON",
                      "/rest/pug/compound/cid/{cid}/dates/JSON?dates_type=modification (verify)"],
        "authentication": "none",
        "licence": "NCBI/NLM data policy: PubChem data are generally public; individual depositor records may carry "
                   "their own terms (verify per depositor before redistribution)",
        "terms_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        "attribution": "Source: PubChem, National Library of Medicine (https://pubchem.ncbi.nlm.nih.gov/)",
        "rate_limits": "at most 5 requests per second and 400 per minute; dynamic throttling via the "
                       "X-Throttling-Control header (verify)",
        "revision_behaviour": "a compound record changes in place; its modification date (dates operation) is the "
                              "record version, and each distinct payload is a new stored revision",
        "access_decision": "unverified-live",
        "fields": ["CID", "Title", "IUPACName", "MolecularFormula", "InChI", "InChIKey", "Synonym",
                   "ModificationDate"],
    },
    "echa-clp": {
        "publisher": "European Chemicals Agency (ECHA CHEM)",
        "access": "ECHA CHEM portal JSON for one substance at a time (undocumented; verify), or the documented "
                  "Annex VI table and C&L inventory downloads",
        "endpoints": ["/api-substance/v1/substance/{echa_id} (verify)",
                      "/api-cnl-inventory/v1/harmonised/{echa_id} (verify)",
                      "/api-cnl-inventory/v1/notified/{echa_id} (verify)"],
        "authentication": "none",
        "licence": "ECHA legal notice: reproduction authorised with acknowledgement of the source; no endorsement "
                   "implied; bulk or commercial reuse subject to the notice (verify)",
        "terms_url": "https://echa.europa.eu/legal-notice",
        "attribution": ECHA_ATTRIBUTION,
        "rate_limits": "none published; the pack keeps to one substance per page and the source budget",
        "revision_behaviour": "Annex VI entries change only by an adaptation to technical progress (ATP): each ATP "
                              "adds a dated revision (publication and application dates) and the prior "
                              "classification stays; notified aggregates change continuously and are dated by the "
                              "inventory's as-of date",
        "access_decision": "unverified-live",
        "fields": ["ec", "cas", "indexNumbers", "name", "entryType", "harmonised history (ATP, act, dates, hazard "
                   "class and category, H-statements)", "notified aggregates (classification, notifier count)"],
    },
    "echa-reach": {
        "publisher": "European Chemicals Agency (ECHA CHEM)",
        "access": "ECHA CHEM portal JSON for one substance at a time (undocumented; verify), or the documented "
                  "Candidate List, Authorisation List and Restriction List downloads",
        "endpoints": ["/api-substance/v1/substance/{echa_id} (verify)",
                      "/api-dossier-list/v1/registrations/{echa_id} (verify)",
                      "/api-lists/v1/candidate-list/{echa_id} (verify)",
                      "/api-lists/v1/authorisation-list/{echa_id} (verify)",
                      "/api-lists/v1/restriction-list/{echa_id} (verify)"],
        "authentication": "none",
        "licence": "ECHA legal notice: reproduction authorised with acknowledgement of the source (verify)",
        "terms_url": "https://echa.europa.eu/legal-notice",
        "attribution": ECHA_ATTRIBUTION,
        "rate_limits": "none published; one substance per page within the source budget",
        "revision_behaviour": "the Candidate List is updated about twice a year (inclusion date per entry, later "
                              "amendments and removals dated); Annex XIV and XVII entries change by amending "
                              "regulation, each amendment dated with its act; registration status carries its "
                              "last-updated date",
        "access_decision": "unverified-live",
        "fields": ["registration status, type, tonnage band, last updated", "candidate-list events (date, reason, "
                   "decision)", "Annex XIV entry (sunset, latest application date, act)",
                   "Annex XVII entry (conditions verbatim, act, dates)"],
    },
    "comptox": {
        "publisher": "U.S. Environmental Protection Agency (CompTox Chemicals Dashboard, CTX APIs)",
        "access": "CTX Chemical and Hazard APIs, HTTPS GET with an x-api-key header",
        "endpoints": ["/chemical/detail/search/by-dtxsid/{dtxsid}", "/hazard/toxval/search/by-dtxsid/{dtxsid} (verify)"],
        "authentication": "API key requested from EPA, held as the NOESIS_COMPTOX_API_KEY secret reference; never "
                          "stored in manifests, receipts or records",
        "licence": "U.S. Government work; EPA data are generally public domain; third-party ToxValDB sources keep "
                   "their own citation requirements (verify)",
        "terms_url": "https://www.epa.gov/comptox-tools/computational-toxicology-and-exposure-apis",
        "attribution": "Source: U.S. EPA CompTox Chemicals Dashboard / CTX APIs (https://comptox.epa.gov/dashboard/)",
        "rate_limits": "per-key limits set by EPA (verify); the pack keeps to one substance per page",
        "revision_behaviour": "data points belong to a ToxValDB release; the release (data version) is recorded with "
                              "every data point and a changed value is a new revision",
        "access_decision": "unverified-live",
        "fields": ["dtxsid", "dtxcid", "casrn", "preferredName", "inchikey", "toxval (type, qualifier, numeric, "
                   "units, study type, route, species, source, reference, year)"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "note": "no dated live run from this runtime; offline fixtures only (#2317)"}
    for provider in PROVIDERS
}
# Fields never acquired. Parsers read an explicit allow-list; anything below that a response carries is dropped
# and named in the page receipt.
EXCLUDED_FIELDS = (
    "synthesis", "synthesis references", "preparation", "methods of manufacturing", "manufacturing process",
    "reactions", "reaction", "precursors", "bioassay", "bioactivity", "dossier contents", "use and manufacturing",
)
MAX_SYNONYMS = 200
_CAS_TEXT = re.compile(r"^\d{2,7}-\d{2}-\d$")
_EC_TEXT = re.compile(r"^(?:EC|EINECS|ELINCS)\s*[: ]?\s*(\d{3}-\d{3}-\d)$", re.IGNORECASE)
_ECHA_ID = re.compile(r"^100\.\d{3}\.\d{3}$")
_DTXSID = re.compile(r"^DTXSID\d{7,12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class SubstanceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if _DATE.match(text) else None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def dropped_fields(payload: Any) -> list[str]:
    """Excluded keys a response carries (never parsed); reported, never stored."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(key))
                label = re.sub(r"[_\s-]+", " ", spaced).strip().casefold()
                if label in EXCLUDED_FIELDS or any(label.startswith(f) for f in ("synthesis", "preparation")):
                    found.add(str(key))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return sorted(found)


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("substances") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"substances sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a substances source selects 1..max_pages substances explicitly")
    for entry in entries:
        if provider == "pubchem" and not identifier_key("cid", entry.get("cid")):
            raise SourcePackError("invalid_manifest", "PubChem selections name a CID")
        group_entry = provider == "echa-clp" and identifier_key("index", entry.get("index_number")) \
            and not entry.get("echa_id")
        if provider.startswith("echa") and not group_entry and not _ECHA_ID.fullmatch(str(entry.get("echa_id") or "")):
            raise SourcePackError("invalid_manifest", "ECHA selections name an ECHA substance id (100.xxx.xxx), or "
                                                      "an Annex VI index number for a CLP group entry")
        if provider == "comptox" and not _DTXSID.fullmatch(str(entry.get("dtxsid") or "")):
            raise SourcePackError("invalid_manifest", "CompTox selections name a DTXSID")
        for scheme, check in (("cas", cas_valid), ("ec", ec_valid)):
            if entry.get(scheme) and not check(entry[scheme]):
                raise SourcePackError("invalid_manifest", f"selection {scheme} {entry[scheme]!r} fails its check digit")
    return provider, entries


def requests_for(provider: str, entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    """(role, path, query) for one selected substance; paths are relative to the source endpoint."""
    if provider == "pubchem":
        cid = str(entry["cid"])
        return [("properties", f"/compound/cid/{cid}/property/Title,IUPACName,MolecularFormula,InChI,InChIKey/JSON", {}),
                ("synonyms", f"/compound/cid/{cid}/synonyms/JSON", {}),
                ("dates", f"/compound/cid/{cid}/dates/JSON", {"dates_type": "modification"})]
    echa_id = quote(str(entry.get("echa_id") or ""), safe=".")
    if provider == "echa-clp" and not entry.get("echa_id"):
        # A CLP group entry (e.g. "lead compounds") has an index number but no EC, CAS or substance id.
        return [("harmonised", f"/api-cnl-inventory/v1/harmonised/by-index/{entry['index_number']}", {})]
    if provider == "echa-clp":
        return [("substance", f"/api-substance/v1/substance/{echa_id}", {}),
                ("harmonised", f"/api-cnl-inventory/v1/harmonised/{echa_id}", {}),
                ("notified", f"/api-cnl-inventory/v1/notified/{echa_id}", {})]
    if provider == "echa-reach":
        return [("substance", f"/api-substance/v1/substance/{echa_id}", {}),
                ("registrations", f"/api-dossier-list/v1/registrations/{echa_id}", {}),
                ("candidate-list", f"/api-lists/v1/candidate-list/{echa_id}", {}),
                ("authorisation-list", f"/api-lists/v1/authorisation-list/{echa_id}", {}),
                ("restriction-list", f"/api-lists/v1/restriction-list/{echa_id}", {})]
    dtxsid = str(entry["dtxsid"])
    return [("detail", f"/chemical/detail/search/by-dtxsid/{dtxsid}", {}),
            ("toxval", f"/hazard/toxval/search/by-dtxsid/{dtxsid}", {})]


# ------------------------------------------------------------------ parsers


def _source(url: str, locator: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "locator": locator, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "evidence_origin": origin, **{k: v for k, v in extra.items() if v is not None}}


def _kind_from_inchi(inchi: str | None) -> str:
    if not inchi:
        return "unknown"
    formula = inchi.split("/")[1] if "/" in inchi else ""
    return "multi-component" if "." in formula else "single-component"


def _identifier(provider, subject, scheme, value, source, *, conflict=False, extra=None):
    key = identifier_key(scheme, value)
    published = {"scheme": scheme, "value": str(value), **({"conflict": True} if conflict else {}),
                 **({"malformed": True} if key is None and scheme not in {"synonym", "preferred-name", "iupac-name"}
                    else {}), **(extra or {})}
    return statement("identifier", provider, subject, f"{scheme}:{value}", published, source=source)


def parse_pubchem(responses: Mapping[str, Any], urls: Mapping[str, str], *, origin: str) -> list[dict[str, Any]]:
    """Identity statements for one CID: identifiers, names and depositor synonyms as published."""
    try:
        (props,) = responses["properties"]["PropertyTable"]["Properties"]
    except (KeyError, TypeError, ValueError) as exc:
        raise SubstanceFormatError("schema_drift", "PubChem property table is not one compound") from exc
    cid = str(props["CID"])
    inchi = _text(props.get("InChI"))
    info = (((responses.get("synonyms") or {}).get("InformationList") or {}).get("Information") or [{}])[0]
    synonyms = [str(s) for s in info.get("Synonym") or []][:MAX_SYNONYMS]
    dates = (((responses.get("dates") or {}).get("InformationList") or {}).get("Information") or [{}])[0]
    modified = dates.get("ModificationDate") or {}
    version = (f"{modified.get('Year'):04d}-{modified.get('Month'):02d}-{modified.get('Day'):02d}"
               if all(isinstance(modified.get(k), int) for k in ("Year", "Month", "Day")) else None)
    subject = {"key": f"pubchem:cid:{cid}", "kind": _kind_from_inchi(inchi), "name": _text(props.get("Title"))}
    src = _source(urls["properties"], "/PropertyTable/Properties/0", "pubchem", origin, record_version=version)
    syn_src = _source(urls["synonyms"], "/InformationList/Information/0/Synonym", "pubchem", origin,
                      record_version=version)
    out = [statement("substance", "pubchem", subject, "compound",
                     {"cid": cid, "title": subject["name"], "iupac_name": _text(props.get("IUPACName")),
                      "molecular_formula": _text(props.get("MolecularFormula")), "synonym_count": len(synonyms),
                      "synonyms_truncated": len(info.get("Synonym") or []) > MAX_SYNONYMS},
                     source=src, effective_from=version, date_basis="PubChem modification date" if version else None)]
    out.append(_identifier("pubchem", subject, "cid", cid, src))
    for scheme, field in (("inchi", "InChI"), ("inchikey", "InChIKey"), ("preferred-name", "Title"),
                          ("iupac-name", "IUPACName")):
        if _text(props.get(field)):
            out.append(_identifier("pubchem", subject, scheme, props[field], src))
    cas = [s for s in synonyms if _CAS_TEXT.fullmatch(s)]
    valid_cas = sorted({c for c in cas if cas_valid(c)})
    ecs = sorted({m.group(1) for s in synonyms if (m := _EC_TEXT.fullmatch(s))})
    for number in sorted(set(cas)):
        # Depositors disagree when more than one valid CAS number is attached; all are kept, none is chosen.
        out.append(_identifier("pubchem", subject, "cas", number, syn_src, conflict=len(valid_cas) > 1,
                               extra={"from": "depositor synonym"}))
    for number in ecs:
        out.append(_identifier("pubchem", subject, "ec", number, syn_src, conflict=len(ecs) > 1,
                               extra={"from": "depositor synonym"}))
    for value in sorted({s for s in synonyms if _DTXSID.fullmatch(s)}):
        out.append(_identifier("pubchem", subject, "dtxsid", value, syn_src, extra={"from": "depositor synonym"}))
    for value in sorted(set(synonyms) - set(cas) - {m for m in synonyms if _EC_TEXT.fullmatch(m)}
                        - {s for s in synonyms if _DTXSID.fullmatch(s)}):
        out.append(_identifier("pubchem", subject, "synonym", value, syn_src))
    return out


_ECHA_KINDS = {"substance": "single-component", "mono-constituent": "single-component",
               "multi-constituent": "multi-component", "uvcb": "mixture", "group": "group", "category": "group",
               "salt": "salt", "isomer": "isomer"}


def _echa_subject(payload: Mapping[str, Any]) -> dict[str, Any]:
    echa_id = str(payload.get("echaId") or "")
    if not _ECHA_ID.fullmatch(echa_id):
        raise SubstanceFormatError("schema_drift", "ECHA substance payload lacks its ECHA id")
    kind = _ECHA_KINDS.get(str(payload.get("entryType") or "").casefold(), "unknown")
    return {"key": f"echa:substance:{echa_id}", "kind": kind, "name": _text(payload.get("name"))}


def _echa_identity(provider, payload, url, origin):
    subject = _echa_subject(payload)
    src = _source(url, "/", provider, origin, record_version=_day(payload.get("lastUpdated")))
    out = [statement("substance", provider, subject, "substance",
                     {"echa_id": payload["echaId"], "name": subject["name"], "entry_type": payload.get("entryType"),
                      "group_members_note": _text(payload.get("groupNote"))}, source=src,
                     effective_from=_day(payload.get("lastUpdated")), date_basis="ECHA last updated")]
    out.append(_identifier(provider, subject, "echa-substance-id", payload["echaId"], src))
    if subject["name"]:
        out.append(_identifier(provider, subject, "preferred-name", subject["name"], src))
    for scheme, field in (("ec", "ecNumbers"), ("cas", "casNumbers"), ("index", "indexNumbers")):
        values = [str(v) for v in payload.get(field) or []]
        for index, value in enumerate(values):
            out.append(_identifier(provider, subject, scheme, value,
                                   {**src, "locator": f"/{field}/{index}"}, conflict=scheme != "index" and len(values) > 1))
    return subject, out


def _act(raw: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any] | None:
    if not raw or not _text(raw.get("title")):
        return None
    return {"title": str(raw["title"]), "celex": _text(raw.get("celex")), "eli": _text(raw.get("eli")),
            "atp": _text(raw.get("atp")), "entry": _text(extra.get("entry") or raw.get("entry")),
            "locator": _text(extra.get("locator") or raw.get("locator"))}


def _hazards(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"hazard_class_category": str(h.get("hazardClassAndCategory") or ""),
             "hazard_statement": _text(h.get("hazardStatement")),
             "as_published": {k: h[k] for k in sorted(h) if k in {"hazardClassAndCategory", "hazardStatement",
                                                                    "specificConcentrationLimit", "mFactor", "note"}}}
            for h in items]


def _group_identity(harmonised: Mapping[str, Any], url: str, origin: str):
    (entry,) = harmonised.get("entries") or [None]
    index_number = str((entry or {}).get("indexNumber") or "")
    if not identifier_key("index", index_number):
        raise SubstanceFormatError("schema_drift", "a group entry payload names exactly one index number")
    name = _text(entry.get("internationalChemicalIdentification"))
    subject = {"key": f"echa:index:{index_number}",
               "kind": _ECHA_KINDS.get(str(entry.get("entryType") or "group").casefold(), "group"), "name": name}
    src = _source(url, "/entries/0", "echa-clp", origin)
    out = [statement("substance", "echa-clp", subject, "annex-vi-entry",
                     {"index_number": index_number, "name": name, "entry_type": entry.get("entryType") or "group",
                      "group_members_note": _text(entry.get("groupNote"))}, source=src),
           _identifier("echa-clp", subject, "index", index_number, src)]
    if name:
        out.append(_identifier("echa-clp", subject, "preferred-name", name, src))
    return subject, out


def parse_echa_clp(responses: Mapping[str, Any], urls: Mapping[str, str], *, origin: str) -> list[dict[str, Any]]:
    if "substance" in responses:
        subject, out = _echa_identity("echa-clp", responses["substance"], urls["substance"], origin)
    else:
        subject, out = _group_identity(responses.get("harmonised") or {}, urls["harmonised"], origin)
    harmonised = responses.get("harmonised") or {}
    for e_index, entry in enumerate(harmonised.get("entries") or []):
        index_number = str(entry.get("indexNumber") or "")
        for h_index, revision in enumerate(entry.get("history") or []):
            event = {"introduced": "inclusion", "amended": "amendment", "deleted": "removal"}.get(
                str(revision.get("change") or "").casefold(), "amendment")
            act = _act(revision.get("legalAct"), entry=f"Annex VI, Table 3, index {index_number}",
                       locator=f"Annex VI Part 3 Table 3 index No {index_number}")
            if act is None:
                raise SubstanceFormatError("schema_drift", "a harmonised revision names no legal act")
            out.append(statement(
                "classification", "echa-clp", subject, f"harmonised:{index_number}",
                {"kind": "harmonised", "index_number": index_number, "international_name":
                 _text(entry.get("internationalChemicalIdentification")),
                 "hazard_classes": _hazards(revision.get("classifications") or []),
                 "notes": [str(n) for n in revision.get("notes") or []],
                 "published_on": _day(revision.get("publishedOn")), "change": revision.get("change")},
                source=_source(urls["harmonised"], f"/entries/{e_index}/history/{h_index}", "echa-clp", origin),
                event=event, effective_from=_day(revision.get("appliesFrom")),
                date_basis="date of application stated by the ATP", legal_act=act))
    notified = responses.get("notified") or {}
    as_of = _day(notified.get("asOf"))
    for n_index, aggregate in enumerate(notified.get("aggregates") or []):
        hazards = _hazards(aggregate.get("classifications") or [])
        key = "notified:" + hashlib.sha256(json.dumps(hazards, sort_keys=True).encode()).hexdigest()[:16]
        out.append(statement(
            "classification", "echa-clp", subject, key,
            {"kind": "notified", "hazard_classes": hazards, "notifiers": aggregate.get("numberOfNotifiers"),
             "joint_entry": aggregate.get("jointEntry"), "quoted": True,
             "note": "a notified (self-)classification aggregate as published; not a harmonised classification"},
            source=_source(urls["notified"], f"/aggregates/{n_index}", "echa-clp", origin), event="notified",
            effective_from=as_of, date_basis="C&L inventory as-of date",
            legal_act={"title": "Regulation (EC) No 1272/2008, Article 40 (C&L inventory notification)",
                       "celex": "32008R1272", "eli": None, "atp": None, "entry": "Article 40", "locator": "Article 40"}))
    return out


_EVENTS = {"inclusion": "inclusion", "included": "inclusion", "amendment": "amendment", "amended": "amendment",
           "removal": "removal", "removed": "removal", "deleted": "removal"}


def parse_echa_reach(responses: Mapping[str, Any], urls: Mapping[str, str], *, origin: str) -> list[dict[str, Any]]:
    subject, out = _echa_identity("echa-reach", responses["substance"], urls["substance"], origin)
    for r_index, item in enumerate((responses.get("registrations") or {}).get("registrations") or []):
        published = {"status": _text(item.get("status")) or "unknown",
                     "registration_type": _text(item.get("registrationType")),
                     "tonnage_band": _text(item.get("tonnageBand")), "last_updated": _day(item.get("lastUpdated"))}
        out.append(statement(
            "registration", "echa-reach", subject, f"registration:{published['registration_type'] or r_index}",
            published, source=_source(urls["registrations"], f"/registrations/{r_index}", "echa-reach", origin),
            event="status", effective_from=published["last_updated"], date_basis="ECHA last updated"))
    lists = (("candidate-list", "candidate_listing"), ("authorisation-list", "authorisation"),
             ("restriction-list", "restriction"))
    for role, record_type in lists:
        for e_index, entry in enumerate((responses.get(role) or {}).get("entries") or []):
            number = _text(entry.get("entryNumber")) or _text(entry.get("entryId")) or str(e_index)
            for v_index, event in enumerate(entry.get("events") or []):
                kind = _EVENTS.get(str(event.get("type") or "").casefold())
                if kind is None:
                    raise SubstanceFormatError("schema_drift", f"unknown {role} event type {event.get('type')!r}")
                locator = f"/entries/{e_index}/events/{v_index}"
                src = _source(urls[role], locator, "echa-reach", origin)
                if record_type == "candidate_listing":
                    published = {"reason": _text(event.get("reason")) or "not stated", "decision": _text(
                        event.get("decision")), "entry_id": number, "date": _day(event.get("date"))}
                    out.append(statement(record_type, "echa-reach", subject, f"svhc:{number}", published, source=src,
                                         event=kind, effective_from=_day(event.get("date")),
                                         date_basis="Candidate List inclusion/update date",
                                         legal_act=_act(event.get("legalAct"))))
                elif record_type == "authorisation":
                    act = _act(event.get("legalAct"), entry=f"Annex XIV entry {number}",
                               locator=f"Regulation (EC) No 1907/2006 Annex XIV entry {number}")
                    published = {"entry_number": number, "sunset_date": _day(event.get("sunsetDate")),
                                 "latest_application_date": _day(event.get("latestApplicationDate")),
                                 "intrinsic_property": _text(event.get("intrinsicProperty")),
                                 "exempted_uses": _text(event.get("exemptedUses")),
                                 "review_periods": _text(event.get("reviewPeriods"))}
                    out.append(statement(record_type, "echa-reach", subject, f"annex-xiv:{number}", published,
                                         source=src, event=kind, effective_from=_day(event.get("date")),
                                         date_basis="date of the amending act's entry into force", legal_act=act))
                else:
                    act = _act(event.get("legalAct"), entry=f"Annex XVII entry {number}",
                               locator=f"Regulation (EC) No 1907/2006 Annex XVII entry {number}")
                    published = {"entry_number": number, "conditions": str(event.get("conditions") or ""),
                                 "designation": _text(entry.get("designation")),
                                 "applies_from": _day(event.get("appliesFrom"))}
                    out.append(statement(record_type, "echa-reach", subject, f"annex-xvii:{number}", published,
                                         source=src, event=kind,
                                         effective_from=_day(event.get("appliesFrom")) or _day(event.get("date")),
                                         date_basis="date the conditions apply from", legal_act=act))
    return out


def parse_comptox(responses: Mapping[str, Any], urls: Mapping[str, str], *, origin: str,
                  data_version: str | None) -> list[dict[str, Any]]:
    detail = responses["detail"] or {}
    dtxsid = str(detail.get("dtxsid") or "")
    if not _DTXSID.fullmatch(dtxsid):
        raise SubstanceFormatError("schema_drift", "CompTox detail lacks a DTXSID")
    subject = {"key": f"comptox:dtxsid:{dtxsid}", "kind": "unknown", "name": _text(detail.get("preferredName"))}
    src = _source(urls["detail"], "/", "comptox", origin, data_version=data_version)
    out = [statement("substance", "comptox", subject, "chemical",
                     {"dtxsid": dtxsid, "preferred_name": subject["name"], "dtxcid": _text(detail.get("dtxcid"))},
                     source=src)]
    for scheme, field in (("dtxsid", "dtxsid"), ("dtxcid", "dtxcid"), ("cas", "casrn"), ("inchikey", "inchikey"),
                          ("preferred-name", "preferredName")):
        if _text(detail.get(field)):
            out.append(_identifier("comptox", subject, scheme, detail[field], src))
    for index, point in enumerate(responses.get("toxval") or []):
        native = _text(point.get("toxvalId"))
        if native is None:
            raise SubstanceFormatError("schema_drift", "a ToxValDB data point lacks its toxvalId")
        reference = " / ".join(x for x in (_text(point.get("longRef")), _text(point.get("year"))) if x) or "not stated"
        published = {
            "endpoint": _text(point.get("toxvalType")) or "not stated",
            "value": _text(point.get("toxvalNumeric")) or "not stated",
            "qualifier": _text(point.get("toxvalNumericQualifier")),
            "unit": _text(point.get("toxvalUnits")) or "not stated",
            "study_type": _text(point.get("studyType")), "exposure_route": _text(point.get("exposureRoute")),
            "species": _text(point.get("speciesCommon")), "study_reference": reference,
            "data_source": " / ".join(x for x in (_text(point.get("source")), _text(point.get("subsource"))) if x)
            or "not stated", "toxval_id": native}
        out.append(statement("data_point", "comptox", subject, f"toxval:{native}", published,
                             source=_source(urls["toxval"], f"/{index}", "comptox", origin,
                                            data_version=data_version)))
    return out


# ------------------------------------------------------------------ runtime adapter


class SubstanceSourceAdapter:
    """One page per selected substance on the runtime's default transport (same host, byte ceiling, timeout)."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        self.declared = dict(self.source["substances"])
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "substances": {"provider": self.provider, "selected": len(self.entries)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "substance runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json"}
        if self.provider == "comptox":
            if not self._secret:
                raise SourcePackError("authentication_failed", "the CompTox API key secret is not configured")
            headers["x-api-key"] = self._secret
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        if status == 404:
            return status, None, url, origin
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        try:
            return status, json.loads(raw.decode("utf-8")), url, origin
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not JSON") from exc

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        responses, urls, dropped, origins, missing = {}, {}, [], set(), []
        size = 0
        for role, path, query in requests_for(self.provider, entry):
            status, body, url, origin = self._get(path, query)
            origins.add(origin)
            urls[role] = url
            if body is None:
                missing.append(role)
                continue
            size += len(json.dumps(body))
            dropped += [f"{role}:{name}" for name in dropped_fields(body)]
            responses[role] = body
        primary = requests_for(self.provider, entry)[0][0]
        label = {k: entry[k] for k in sorted(entry) if k in {"cid", "echa_id", "index_number", "dtxsid", "cas", "ec",
                                                              "inchikey", "label"}}
        origin = "live" if "live" in origins else "fixture"
        if primary in missing:
            outcome, statements = "not_found", []
        else:
            try:
                if self.provider == "pubchem":
                    statements = parse_pubchem(responses, urls, origin=origin)
                elif self.provider == "echa-clp":
                    statements = parse_echa_clp(responses, urls, origin=origin)
                elif self.provider == "echa-reach":
                    statements = parse_echa_reach(responses, urls, origin=origin)
                else:
                    statements = parse_comptox(responses, urls, origin=origin,
                                               data_version=_text(self.declared.get("data_version")))
            except (SubstanceFormatError, SubstanceError, KeyError, TypeError) as exc:
                raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
            outcome = "found"
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in statements:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({
                "id": f"{item['subject']['key']}|{item['record_type']}|{item['record_key']}|"
                      + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": f"{item['subject'].get('name') or item['subject']['key']}: {item['record_type']}",
                "url": item["source"]["url"], "language": "en", "content": content, "substance_record": item})
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": outcome,
                   "missing": missing, "statements": len(records), "excluded_fields_dropped": sorted(set(dropped)),
                   "evidence_origin": origin, "final_page": index + 1 >= len(self.entries)}
        next_cursor = str(index + 1) if index + 1 < len(self.entries) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: SubstanceSourceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = SubstanceSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                     secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "CONNECTOR", "EXCLUDED_FIELDS", "FIXTURE_SECRET", "LIVE_VERIFICATION", "PROVIDERS",
    "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "SubstanceFormatError", "SubstanceSourceAdapter", "dropped_fields",
    "fixture_transport", "parse_comptox", "parse_echa_clp", "parse_echa_reach", "parse_pubchem",
    "replay_native_fixture", "requests_for", "selection_entries",
]

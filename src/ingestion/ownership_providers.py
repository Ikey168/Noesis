"""Corporate-ownership source contracts, fail-closed parsers and bounded runtime adapters.

Coverage is deliberately bounded to explicitly selected companies, filings
and statement files; nothing here implies comprehensive coverage of any
registry. Every provider has a recorded access contract
(:data:`PROVIDER_CONTRACTS`); where machine access is unsupported or its terms
forbid automated retrieval the contract says ``not-implemented`` with a reason,
and no scraper exists.

* **GLEIF Level 2** is acquired by the existing ``gleif`` connector
  (:mod:`src.ingestion.lei_sources`) into the existing ``market.lei`` owner
  (:mod:`src.kb.lei`); :func:`parse_gleif_level2` projects the stored LEI
  record's parent relationships, reporting exceptions and successor pointer as
  ownership records whose source is that LEI record.
* **UK Companies House** (``companies-house`` connector): profile, officers,
  PSC, PSC statements, exemptions and filing history through the public data
  API with an API key (HTTP Basic).
* **SEC EDGAR** (``sec-edgar-ownership`` connector): submissions and company
  facts JSON on ``data.sec.gov`` and explicitly selected Schedule 13D/13G
  primary documents on ``www.sec.gov``, with a declared User-Agent under the
  fair-access policy.
* **Open Ownership BODS** (``bods`` connector): explicitly selected published
  BODS 0.2/0.3 statement files; per-source coverage is recorded and unsupported
  sources are counted as not implemented.

Every adapter runs inside :class:`~src.ingestion.source_pack_runtime.SourcePackRuntime`
(cursors, budgets, retries, receipts, document evidence). Parsers never invent
values: anything absent stays unknown. Ownership assertions are what a source
states; no beneficial-ownership, sanctions or AML determination is inferred.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_records import PART_CONTRACT, OwnershipRecordError, record

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
FIXTURE_SECRET = "fixture-secret-not-a-credential"
PROVIDER_HOSTS = {
    "gleif": {"api.gleif.org"},
    "companies-house": {"api.company-information.service.gov.uk"},
    "sec-edgar": {"data.sec.gov", "www.sec.gov"},
    "open-ownership": {"bods-data.openownership.org"},
    "opencorporates": {"api.opencorporates.com"},
    "handelsregister": set(),
    "unternehmensregister": set(),
    "bris": set(),
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "gleif": {
        "status": "implemented",
        "connector": "gleif (existing, src/ingestion/lei_sources.py) into market.lei (src/kb/lei.py)",
        "documentation": "https://www.gleif.org/en/lei-data/gleif-api",
        "access": "GLEIF API v1 JSON:API: /lei-records/{lei}, /direct-parent-relationship, "
                  "/ultimate-parent-relationship, /direct-parent-reporting-exception, "
                  "/ultimate-parent-reporting-exception",
        "authentication": "none",
        "rate_limits": "GLEIF publishes a fair-use request limit; the runtime honours HTTP 429 Retry-After and the "
                       "source budget (max_pages/max_bytes); no higher rate is assumed",
        "pagination": "one part of one selected LEI per runtime page (cursor = work index)",
        "cadence": "daily at most; LOUs update lastUpdateDate per record",
        "terms": "LEI data is published under CC0 (https://www.gleif.org/en/about/open-data)",
        "retained_evidence": "raw JSON:API response digest per part; LEI record revisions in lei_revisions",
        "identifiers": ["lei", "registration authority id + registeredAs (register number)"],
        "coverage": "explicitly selected LEIs (1-50 per source); Level 1 record and Level 2 relationships/exceptions",
        "unavailable_fallback": "HTTP 404 on a part is 'none reported'; transport failures leave earlier observations "
                                "untouched and the run receipt records the failure code",
        "cost": "free",
    },
    "companies-house": {
        "status": "implemented",
        "connector": "companies-house",
        "documentation": "https://developer.company-information.service.gov.uk/",
        "access": "Companies House public data REST API: /company/{n}, /officers, "
                  "/persons-with-significant-control, /persons-with-significant-control-statements, /exemptions, "
                  "/filing-history",
        "authentication": "API key from a registered application, sent as the HTTP Basic username "
                          "(required secret NOESIS_COMPANIES_HOUSE_API_KEY)",
        "rate_limits": "documented as 600 requests per five minutes per key; the adapter spaces live requests "
                       "(min_interval_s) and honours HTTP 429",
        "pagination": "items_per_page/start_index, followed within the source page budget",
        "cadence": "daily at most per selected company",
        "terms": "Public register data; reuse under the Companies House terms of the public data API. Document "
                 "images are not downloaded; filing references keep the document metadata link only",
        "retained_evidence": "each API response retained as the runtime document (native JSON) with its SHA-256",
        "identifiers": ["gb-coh company number", "officer appointment id", "PSC id"],
        "coverage": "explicitly selected company numbers only; no bulk product or streaming API is used",
        "unavailable_fallback": "401 fails the source as authentication_failed; 404 on a part is 'none reported'; "
                                "earlier revisions stay current",
        "cost": "free with registration",
    },
    "sec-edgar": {
        "status": "implemented",
        "connector": "sec-edgar-ownership",
        "documentation": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "access": "data.sec.gov submissions (/submissions/CIK##########.json) and company facts "
                  "(/api/xbrl/companyfacts/CIK##########.json); explicitly selected Schedule 13D/13G primary "
                  "documents under www.sec.gov/Archives/edgar/data",
        "authentication": "none; a declared User-Agent with contact details is required by the fair-access policy "
                          "(required secret NOESIS_SEC_CONTACT)",
        "rate_limits": "fair-access policy: at most 10 requests per second; the adapter spaces live requests",
        "pagination": "submissions 'recent' block only (older 'files' pages are listed, not followed); bounded "
                      "by max_filings",
        "cadence": "daily at most",
        "terms": "SEC public data; fair-access policy (https://www.sec.gov/os/accessing-edgar-data)",
        "retained_evidence": "submissions/companyfacts JSON and primary-document XML retained with SHA-256",
        "identifiers": ["cik", "ticker (as listed by SEC)", "lei (when the submissions record carries one)"],
        "coverage": "selected CIKs; beneficial-ownership forms (Schedule 13D/13G, Forms 3/4/5) are filing "
                    "references; only selected XML Schedule 13D/13G cover pages are parsed",
        "unavailable_fallback": "unparsed filings stay references; failures recorded per run receipt",
        "cost": "free",
    },
    "open-ownership": {
        "status": "implemented",
        "connector": "bods",
        "documentation": "https://www.openownership.org/en/topics/beneficial-ownership-data-standard/",
        "access": "published Beneficial Ownership Data Standard (BODS 0.2/0.3) statement files, JSON array or "
                  "JSON Lines, explicitly selected by path on the declared host",
        "authentication": "none",
        "rate_limits": "bounded by the source byte and page budget; one file per work item",
        "pagination": "statement offset within a selected file (cursor)",
        "cadence": "per published dataset release; no more than weekly",
        "terms": "licence stated per statement in publicationDetails.license and recorded on each record; "
                 "person statements are kept owner-scoped",
        "retained_evidence": "the statements of each page retained as the runtime document with the file SHA-256",
        "identifiers": ["BODS statementID", "GB-COH", "XI-LEI and other org-id schemes carried by statements"],
        "coverage": "selected files; only sources listed in publisher_coverage as implemented are projected",
        "publisher_coverage": {
            "GB Persons Of Significant Control Register": {
                "status": "implemented", "jurisdiction": "GB", "update_cadence": "as republished by Open Ownership",
                "license": "as stated per statement (publicationDetails.license)"},
            "Denmark Central Business Register (Centrale Virksomhedsregister [CVR])": {
                "status": "not-implemented", "reason": "licence and personal-data terms not reviewed for reuse"},
            "Slovakia Public Sector Partners Register (Register partnerov verejného sektora)": {
                "status": "not-implemented", "reason": "licence and personal-data terms not reviewed for reuse"},
        },
        "host_note": "host and dataset path layout are declared for bounded selection and are unverified from "
                     "this runtime (egress blocked); verify before any live run",
        "unavailable_fallback": "unsupported BODS versions and sources are counted and skipped, never guessed",
        "cost": "free",
    },
    "opencorporates": {
        "status": "reused",
        "connector": "existing regional provider (#1483, src/ingestion/regional_providers.py)",
        "documentation": "https://api.opencorporates.com/documentation/API-Reference",
        "access": "company search/lookup through the existing regional provider",
        "authentication": "API token",
        "role": "aggregator; enrichment and identity evidence only, never an authoritative substitute for a "
                "national register",
        "identifiers": ["opencorporates jurisdiction_code + company_number"],
        "coverage": "whatever the existing regional provider acquired; not re-acquired here",
        "cost": "per OpenCorporates plan",
    },
    "handelsregister": {
        "status": "not-implemented",
        "documentation": "https://www.handelsregister.de/",
        "access": "web portal of the German federal states; register extracts (AD/CD/HD) retrieved interactively",
        "authentication": "none for search; extracts per portal rules",
        "terms": "portal terms restrict automated and bulk retrieval; no documented machine API",
        "cost": "extracts free of charge since 2022 per portal, interactive only",
        "reason": "no supported machine access; a scraper would breach the access terms. A user-supplied official "
                  "extract can be recorded as a registration with its document reference "
                  "(OwnershipStore.record_register_document)",
    },
    "unternehmensregister": {
        "status": "not-implemented",
        "documentation": "https://www.unternehmensregister.de/",
        "access": "web portal (Bundesanzeiger Verlag) for publications and register data",
        "authentication": "account for some documents",
        "terms": "no documented public API; automated retrieval is not offered",
        "cost": "fees for some documents",
        "reason": "no supported machine access; not scraped. Official documents may be recorded manually",
    },
    "bris": {
        "status": "not-implemented",
        "documentation": "https://e-justice.europa.eu/topics/registers-business-insolvency-land/business-registers-search-company-eu_en",
        "access": "Business Registers Interconnection System via the European e-Justice portal search",
        "authentication": "none for search; national registers set document fees",
        "terms": "interactive portal; no public machine API for third parties is documented",
        "cost": "per national register",
        "reason": "no supported machine access; not scraped. Official documents may be recorded manually",
    },
}
# Which identifiers each source carries and where they meet src/kb/lei.py and
# the OpenCorporates enrichment (#1483). Reconciliation proposes, never merges.
IDENTIFIER_OVERLAP = {
    "lei": {"carried_by": ["gleif", "open-ownership (XI-LEI)", "sec-edgar (submissions.lei, often null)",
                           "market instrument master (issuer identifiers)"],
            "owner": "src/kb/lei.py (market.lei)"},
    "register_number": {"carried_by": ["gleif (registeredAt + registeredAs)", "companies-house (company_number)",
                                       "open-ownership (GB-COH)", "opencorporates (jurisdiction + company_number)"],
                        "owner": "src/kb/lei.py propose_registry_links for OpenCorporates; ownership identity "
                                 "candidates for Companies House and BODS"},
    "cik": {"carried_by": ["sec-edgar", "market instrument master (issuer alias scheme 'cik')"],
            "owner": "src/domains/market/instruments.py resolve_identifier"},
    "ticker": {"carried_by": ["sec-edgar (tickers as listed)", "market instrument master (listing ticker)"],
               "owner": "market instrument master; tickers never identify a security by themselves"},
}
# GLEIF registration-authority codes mapped to an identifier scheme. The
# mapping is configured, never inferred; extend it from GLEIF's RA list.
REGISTRATION_AUTHORITIES = {"RA000585": ("GB", "gb-coh", "Companies House")}
# From the dated bounded run in docs/development/ownership-evidence/live-check-2026-09-27.json
# (scripts/ownership_live_check.py). No provider has been verified live.
_LIVE_RUN = "docs/development/ownership-evidence/live-check-2026-09-27.json"
_BLOCKED = "host probe: egress proxy refused CONNECT (403)"
LIVE_VERIFICATION = {
    "gleif": {"status": "unverified-live", "last_run": "2026-09-27", "result": "failed",
              "failure_code": "source_unavailable", "cause": _BLOCKED, "evidence": _LIVE_RUN},
    "companies-house": {"status": "unverified-live", "last_run": "2026-09-27", "result": "not-sent",
                        "failure_code": "credential_missing",
                        "cause": "NOESIS_COMPANIES_HOUSE_API_KEY not configured; " + _BLOCKED, "evidence": _LIVE_RUN},
    "sec-edgar": {"status": "unverified-live", "last_run": "2026-09-27", "result": "not-sent",
                  "failure_code": "credential_missing",
                  "cause": "NOESIS_SEC_CONTACT (fair-access User-Agent contact) not configured; " + _BLOCKED,
                  "evidence": _LIVE_RUN},
    "open-ownership": {"status": "unverified-live", "last_run": "2026-09-27", "result": "not-attempted",
                       "failure_code": "no_verified_dataset_path", "cause": _BLOCKED, "evidence": _LIVE_RUN},
}
LIVE_VERIFICATION.update({
    "opencorporates": {"status": "reused", "note": "verified (or not) by the existing regional provider"},
    **{p: {"status": "not-implemented", "note": PROVIDER_CONTRACTS[p]["reason"]}
       for p in ("handelsregister", "unternehmensregister", "bris")},
})
BENEFICIAL_OWNERSHIP_FORMS = frozenset({
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G",
    "SCHEDULE 13G/A", "3", "3/A", "4", "4/A", "5", "5/A",
})
_CH_BANDS = {
    "25-to-50-percent": {"min": "25", "max": "50", "min_inclusive": False, "max_inclusive": True},
    "50-to-75-percent": {"min": "50", "max": "75", "min_inclusive": False, "max_inclusive": False},
    "75-to-100-percent": {"min": "75", "max": "100", "min_inclusive": True, "max_inclusive": True},
    "more-than-25-percent": {"min": "25", "max": None, "min_inclusive": False, "max_inclusive": None},
}
_BODS_INTERESTS = {
    "shareholding": "shareholding", "voting-rights": "voting_rights", "appointment-of-board": "appoint_directors",
    "significant-influence-or-control": "significant_influence", "influence-or-control": "significant_influence",
    "other-influence-or-control": "other_control", "senior-managing-official": "other_control",
    "settlor-of-trust": "other_control", "trustee-of-trust": "other_control", "protector-of-trust": "other_control",
    "beneficiary-of-trust": "other_control", "rights-to-surplus-assets-on-dissolution": "other_control",
    "rights-to-profit-or-income": "other_control", "rights-granted-by-contract": "other_control",
    "conditional-rights-granted-by-contract": "other_control", "unknown-interest": "other_control",
    "unpublished-interest": "other_control",
}
_BODS_SCHEMES = {"GB-COH": "gb-coh", "XI-LEI": "lei", "US-SEC": "sec-cik"}
# Stated country names on PSC identification mapped to ISO 3166 codes; a name
# outside this table leaves the jurisdiction unknown rather than guessed.
_COUNTRY_NAMES = {
    "england": "GB", "wales": "GB", "scotland": "GB", "northern ireland": "GB", "united kingdom": "GB",
    "england and wales": "GB", "netherlands": "NL", "the netherlands": "NL", "germany": "DE", "ireland": "IE",
    "luxembourg": "LU", "france": "FR", "united states": "US", "usa": "US",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:120] or "unnamed"


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)[:10]
    return text if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text) else None


def _validity(start: Any, end: Any, *, open_when_missing: bool) -> dict[str, Any]:
    start, end = _date(start), _date(end)
    return {"from": start, "to": end, "from_status": "stated" if start else "unknown",
            "to_status": "stated" if end else ("open" if open_when_missing else "unknown")}


# ------------------------------------------------------------------ GLEIF (O03)


def parse_gleif_level2(level2: Mapping[str, Any], *, lei_namespace: str) -> list[dict[str, Any]]:
    """Project one stored LEI (``LeiStore.level2``) into ownership records.

    The LEI record revision is the source of every record; the Level 1 record
    itself stays in ``src.kb.lei`` and is not copied beyond the entity summary.
    """
    lei, current = level2["lei"], level2.get("record")
    if current is None:
        return []
    attributes = dict(current["attributes"])
    entity = dict(attributes.get("entity") or {})
    registration = dict(attributes.get("registration") or {})
    key = f"gleif:lei:{lei}"
    base = {"provider": "gleif", "provider_record_id": lei, "url": f"https://search.gleif.org/#/record/{lei}",
            "revision": current["revision_id"], "raw_sha256": current["raw_sha256"],
            "retrieved_at_ms": int(current["observed_at_ms"]), "license": "CC0 (GLEIF open data)",
            "note": f"LEI record in src.kb.lei namespace {lei_namespace}; provider revision "
                    f"{current.get('provider_revision') or 'unknown'}"}
    identifiers = [{"scheme": "lei", "value": lei, "authority": "GLEIF"}]
    authority = dict(entity.get("registeredAt") or {}).get("id")
    number = entity.get("registeredAs")
    if authority and number:
        jurisdiction, scheme, register = REGISTRATION_AUTHORITIES.get(authority, (None, f"ra:{authority}", authority))
        identifiers.append({"scheme": scheme, "value": str(number), "authority": authority})
    legal_name = dict(entity.get("legalName") or {}).get("name") or f"LEI {lei}"
    records = [record(
        "legal_entity", key, {**base, "locator": {"json_pointer": "/data/attributes/entity"}},
        name=legal_name, jurisdiction=entity.get("jurisdiction"), identifiers=identifiers,
        entity_type=entity.get("category"), status=entity.get("status"),
        other_names=[{"name": n.get("name"), "type": n.get("type")} for n in entity.get("otherNames") or []],
        founding_date=_date(entity.get("creationDate")),
        addresses=[{"type": "legal", **dict(entity.get("legalAddress") or {})},
                   {"type": "headquarters", **dict(entity.get("headquartersAddress") or {})}],
        native={"registration_status": registration.get("status"),
                "corroboration": registration.get("corroborationLevel")},
    )]
    if authority and number:
        records.append(record(
            "registration", f"gleif:registration:{lei}", {**base, "locator": {"json_pointer": "/data/attributes/entity/registeredAs"}},
            entity_key=key, register=REGISTRATION_AUTHORITIES.get(authority, (None, None, authority))[2],
            number=str(number), jurisdiction=entity.get("jurisdiction"), status=entity.get("status"),
            native={"registration_authority_id": authority,
                    "note": "registration-authority pointer from the LEI record, not a register extract"},
        ))
    for parent in level2.get("parents") or []:
        level = parent["level"]
        period = next((p for p in parent["periods"] if p.get("type") == "RELATIONSHIP_PERIOD"), None) or {}
        status = parent.get("relationship_status")
        records.append(record(
            "ownership_assertion", f"gleif:parent:{level}:{lei}:{parent['parent_lei']}",
            {**base, "statement_id": parent["assertion_id"],
             "locator": {"json_pointer": f"/data/attributes/relationship ({level}-parent-relationship)"}},
            subject_key=key, assertion_kind=f"{level}_parent",
            holder={"key": f"gleif:lei:{parent['parent_lei']}", "kind": "entity"},
            validity=_validity(period.get("startDate"), period.get("endDate"),
                               open_when_missing=bool(period) and status == "ACTIVE"),
            relationship_status=status, basis="accounting consolidation as reported to GLEIF",
            native={"relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY" if level == "direct" else
                    "IS_ULTIMATELY_CONSOLIDATED_BY", "periods": parent["periods"],
                    "registration": parent["registration"], "first_observed_run": parent["run_id"]},
        ))
    seen_levels = set()
    for item in level2.get("reporting") or []:
        # lei_reporting keeps one row per run; the first observation per level
        # is the stable statement, so re-runs do not mint new revisions.
        if item["kind"] != "reporting_exception" or item["level"] in seen_levels:
            continue
        seen_levels.add(item["level"])
        detail = dict(item.get("detail") or {})
        records.append(record(
            "ownership_assertion", f"gleif:exception:{item['level']}:{lei}", {
                **base, "locator": {"json_pointer": f"/data/attributes ({item['level']}-parent-reporting-exception)"}},
            subject_key=key, assertion_kind="reporting_exception", holder=None,
            reporting_exception={"level": item["level"], "category": str(item.get("reason") or "UNSPECIFIED"),
                                 "reason": detail.get("reference"), "native_text": detail.get("category")},
            validity=_validity(None, None, open_when_missing=False),
            native={"exception": detail, "first_observed_run": item["run_id"]},
        ))
    successor = dict(entity.get("successorEntity") or {})
    if successor.get("lei") or successor.get("name"):
        records.append(record(
            "corporate_event", f"gleif:succession:{lei}", {**base, "locator": {"json_pointer": "/data/attributes/entity/successorEntity"}},
            entity_key=key, event_type="succession",
            related_entity_key=f"gleif:lei:{successor['lei']}" if successor.get("lei") else None,
            event_date=None, date_status="unknown",
            description=f"GLEIF names {successor.get('lei') or successor.get('name')} as successor entity; "
                        f"entity status {entity.get('status')}, registration status {registration.get('status')}",
        ))
    return records


# ------------------------------------------------------- Companies House (O04)


def _ch_source(company: str, part: str, raw_sha: str, pointer: str, url_path: str) -> dict[str, Any]:
    return {"provider": "companies-house", "provider_record_id": f"{company}:{part}",
            "url": "https://find-and-update.company-information.service.gov.uk" + url_path,
            "raw_sha256": raw_sha, "locator": {"json_pointer": pointer}, "license": "Companies House public data"}


def _ch_company(company: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]{8}", company or ""):
        raise SourcePackError("unbounded_source", "company numbers are eight characters")
    return company


def parse_ch_profile(payload: Mapping[str, Any], *, company: str, raw_sha: str) -> list[dict[str, Any]]:
    if payload.get("company_number") != company or not payload.get("company_name"):
        raise SourcePackError("schema_drift", "Companies House profile lacks the requested company")
    key = f"companies-house:gb-coh:{company}"
    source = _ch_source(company, "profile", raw_sha, "", f"/company/{company}")
    records = [
        record("legal_entity", key, source, name=payload["company_name"], jurisdiction="GB",
               identifiers=[{"scheme": "gb-coh", "value": company, "authority": "Companies House"}],
               entity_type=payload.get("type"), status=payload.get("company_status"),
               other_names=[{"name": p.get("name"), "type": "previous", "effective_from": p.get("effective_from"),
                             "ceased_on": p.get("ceased_on")} for p in payload.get("previous_company_names") or []],
               founding_date=_date(payload.get("date_of_creation")),
               dissolution_date=_date(payload.get("date_of_cessation")),
               addresses=[{"type": "registered-office", **dict(payload.get("registered_office_address") or {})}],
               native={"jurisdiction": payload.get("jurisdiction"), "etag": payload.get("etag")}),
        record("registration", f"companies-house:registration:{company}", {**source, "locator": {"json_pointer": "/company_number"}},
               entity_key=key, register="Companies House", number=company, jurisdiction=payload.get("jurisdiction"),
               status=payload.get("company_status"), registered_on=_date(payload.get("date_of_creation")),
               dissolved_on=_date(payload.get("date_of_cessation"))),
    ]
    if payload.get("date_of_creation"):
        records.append(record("corporate_event", f"companies-house:incorporation:{company}",
                              {**source, "locator": {"json_pointer": "/date_of_creation"}}, entity_key=key,
                              event_type="incorporation", event_date=_date(payload["date_of_creation"]),
                              description=f"Incorporated as {payload.get('type') or 'company'} in "
                                          f"{payload.get('jurisdiction') or 'an unstated jurisdiction'}"))
    for index, previous in enumerate(payload.get("previous_company_names") or []):
        ceased = _date(previous.get("ceased_on"))
        records.append(record("corporate_event", f"companies-house:name-change:{company}:{index}",
                              {**source, "locator": {"json_pointer": f"/previous_company_names/{index}"}},
                              entity_key=key, event_type="name_change", event_date=ceased,
                              date_status="stated" if ceased else "unknown",
                              description=f"Ceased to use the name {previous.get('name')}"))
    if payload.get("date_of_cessation"):
        records.append(record("corporate_event", f"companies-house:dissolution:{company}",
                              {**source, "locator": {"json_pointer": "/date_of_cessation"}}, entity_key=key,
                              event_type="dissolution", event_date=_date(payload["date_of_cessation"]),
                              description=f"Status {payload.get('company_status')}"))
    return records


def parse_ch_officers(payload: Mapping[str, Any], *, company: str, raw_sha: str, offset: int = 0) -> list[dict[str, Any]]:
    if not isinstance(payload.get("items"), list):
        raise SourcePackError("schema_drift", "officer list has no items")
    key, records = f"companies-house:gb-coh:{company}", []
    for index, item in enumerate(payload["items"]):
        if not item.get("name") or not item.get("officer_role"):
            raise SourcePackError("schema_drift", "officer item lacks name or role")
        link = str(dict(dict(item.get("links") or {}).get("officer") or {}).get("appointments") or "")
        officer_id = (re.search(r"/officers/([^/]+)/", link) or [None, None])[1] or _sha(json.dumps(item, sort_keys=True).encode())[:16]
        appointed, before = _date(item.get("appointed_on")), _date(item.get("appointed_before"))
        records.append(record(
            "officer_role", f"companies-house:officer:{company}:{officer_id}:{item['officer_role']}",
            _ch_source(company, "officers", raw_sha, f"/items/{index}", f"/company/{company}/officers"),
            entity_key=key, officer={"name": item["name"], "key": f"companies-house:officer:{officer_id}",
                                     "kind": "person" if "corporate" not in item["officer_role"] else "entity"},
            role=item["officer_role"], appointed_on=appointed or before,
            appointed_status="stated" if appointed else ("before" if before else "unknown"),
            resigned_on=_date(item.get("resigned_on")),
            native={"occupation": item.get("occupation"), "nationality": item.get("nationality"),
                    "page_offset": offset},
        ))
    return records


def _psc_holder(item: Mapping[str, Any], company: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    kind = str(item.get("kind") or "")
    link = str(dict(item.get("links") or {}).get("self") or "")
    psc_id = link.rstrip("/").split("/")[-1] or _sha(json.dumps(item, sort_keys=True).encode())[:16]
    ident = dict(item.get("identification") or {})
    if kind.startswith("individual"):
        return {"key": f"companies-house:psc:{company}:{psc_id}", "name": item.get("name"), "kind": "person"}, [], psc_id
    if kind.startswith(("corporate-entity", "legal-person")):
        number = str(ident.get("registration_number") or "").replace(" ", "").upper()
        uk = re.search(r"companies house|england|wales|scotland|united kingdom", " ".join(
            str(ident.get(k) or "") for k in ("place_registered", "country_registered", "legal_authority")), re.I)
        if number and uk and re.fullmatch(r"[A-Z0-9]{8}", number):
            return {"key": f"companies-house:gb-coh:{number}", "name": item.get("name"), "kind": "entity"}, [], psc_id
        holder_key = f"companies-house:psc:{company}:{psc_id}"
        country = str(ident.get("country_registered") or "").strip().lower()
        entity = record("legal_entity", holder_key, _ch_source(company, "psc", "", "", f"/company/{company}/persons-with-significant-control"),
                        name=item.get("name") or "unnamed corporate PSC", jurisdiction=_COUNTRY_NAMES.get(country),
                        identifiers=[{"scheme": "register", "value": number, "authority": ident.get("place_registered")}]
                        if number else [], entity_type=ident.get("legal_form"),
                        native={"identification": ident})
        return {"key": holder_key, "name": item.get("name"), "kind": "entity"}, [entity], psc_id
    return {"key": None, "name": item.get("name"), "kind": "unknown"}, [], psc_id


def ch_nature(nature: str) -> tuple[str, dict[str, Any] | None]:
    """Map a PSC nature of control to an assertion kind; bands stay bands."""
    base = re.sub(r"-as-(trust|firm)$", "", nature)
    base = re.sub(r"-registered-overseas-entity$", "", base)
    for prefix, kind in (("ownership-of-shares-", "shareholding"), ("voting-rights-", "voting_rights")):
        if base.startswith(prefix):
            band = _CH_BANDS.get(base[len(prefix):])
            return kind, ({"band": band, "native": nature} if band else None)
    if base.startswith("right-to-appoint-and-remove"):
        return "appoint_directors", None
    if base.startswith("significant-influence-or-control"):
        return "significant_influence", None
    return "other_control", None


def parse_ch_psc(payload: Mapping[str, Any], *, company: str, raw_sha: str, offset: int = 0) -> list[dict[str, Any]]:
    if not isinstance(payload.get("items"), list):
        raise SourcePackError("schema_drift", "PSC list has no items")
    key, records = f"companies-house:gb-coh:{company}", []
    for index, item in enumerate(payload["items"]):
        natures = item.get("natures_of_control")
        if not isinstance(natures, list) or not natures:
            raise SourcePackError("schema_drift", "PSC item lacks natures_of_control")
        holder, extra, psc_id = _psc_holder(item, company)
        for entity in extra:
            entity["source"]["raw_sha256"] = raw_sha
            entity["source"]["locator"] = {"json_pointer": f"/items/{index}/identification"}
            records.append(entity)
        for nature in natures:
            kind, share = ch_nature(str(nature))
            records.append(record(
                "ownership_assertion", f"companies-house:psc:{company}:{psc_id}:{nature}",
                _ch_source(company, "psc", raw_sha, f"/items/{index}/natures_of_control",
                           f"/company/{company}/persons-with-significant-control"),
                subject_key=key, assertion_kind=kind, holder=holder, share=share,
                validity=_validity(item.get("notified_on"), item.get("ceased_on"), open_when_missing=True),
                statement_date=_date(item.get("notified_on")), basis="PSC register entry as filed with Companies House",
                native={"nature_of_control": nature, "psc_kind": item.get("kind"), "page_offset": offset},
            ))
    return records


def parse_ch_psc_statements(payload: Mapping[str, Any], *, company: str, raw_sha: str) -> list[dict[str, Any]]:
    if not isinstance(payload.get("items"), list):
        raise SourcePackError("schema_drift", "PSC statement list has no items")
    key, records = f"companies-house:gb-coh:{company}", []
    for index, item in enumerate(payload["items"]):
        statement = item.get("statement")
        if not statement:
            raise SourcePackError("schema_drift", "PSC statement lacks its statement code")
        records.append(record(
            "ownership_assertion", f"companies-house:psc-statement:{company}:{statement}:{item.get('notified_on')}",
            _ch_source(company, "psc-statements", raw_sha, f"/items/{index}/statement",
                       f"/company/{company}/persons-with-significant-control"),
            subject_key=key, assertion_kind="reporting_exception", holder=None,
            reporting_exception={"level": "psc", "category": statement, "reason": None, "native_text": statement},
            validity=_validity(item.get("notified_on"), item.get("ceased_on"), open_when_missing=True),
            statement_date=_date(item.get("notified_on")),
            native={"linked_psc_name": item.get("linked_psc_name")},
        ))
    return records


def parse_ch_exemptions(payload: Mapping[str, Any], *, company: str, raw_sha: str) -> list[dict[str, Any]]:
    exemptions = payload.get("exemptions")
    if not isinstance(exemptions, dict):
        raise SourcePackError("schema_drift", "exemptions response lacks exemptions")
    key, records = f"companies-house:gb-coh:{company}", []
    for name, exemption in sorted(exemptions.items()):
        for index, item in enumerate(dict(exemption or {}).get("items") or []):
            category = exemption.get("exemption_type") or name
            records.append(record(
                "ownership_assertion", f"companies-house:exemption:{company}:{category}:{item.get('exempt_from')}",
                _ch_source(company, "exemptions", raw_sha, f"/exemptions/{name}/items/{index}", f"/company/{company}"),
                subject_key=key, assertion_kind="reporting_exception", holder=None,
                reporting_exception={"level": "psc", "category": category, "reason": "exemption from PSC disclosure",
                                     "native_text": name},
                validity=_validity(item.get("exempt_from"), item.get("exempt_to"), open_when_missing=True),
            ))
    return records


def parse_ch_filing_history(payload: Mapping[str, Any], *, company: str, raw_sha: str, offset: int = 0) -> list[dict[str, Any]]:
    if not isinstance(payload.get("items"), list):
        raise SourcePackError("schema_drift", "filing history has no items")
    key, records = f"companies-house:gb-coh:{company}", []
    for index, item in enumerate(payload["items"]):
        if not item.get("transaction_id") or not item.get("type"):
            raise SourcePackError("schema_drift", "filing item lacks transaction id or type")
        document = str(dict(item.get("links") or {}).get("document_metadata") or "")
        records.append(record(
            "filing_reference", f"companies-house:filing:{company}:{item['transaction_id']}",
            _ch_source(company, "filing-history", raw_sha, f"/items/{index}", f"/company/{company}/filing-history"),
            entity_key=key, form_type=item["type"], accession_number=item["transaction_id"],
            filing_date=_date(item.get("date")), document_url=document if document.startswith("https://") else None,
            description=" / ".join(str(v) for v in (item.get("category"), item.get("description")) if v),
            parsed=False, native={"description_values": item.get("description_values"), "page_offset": offset},
        ))
    return records


# ------------------------------------------------------------ SEC EDGAR (O05)


def cik10(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or len(digits) > 10:
        raise SourcePackError("unbounded_source", "CIKs are up to ten digits")
    return digits.zfill(10)


def _sec_source(cik: str, part: str, raw_sha: str, pointer: str, url: str) -> dict[str, Any]:
    return {"provider": "sec-edgar", "provider_record_id": f"{cik}:{part}", "url": url, "raw_sha256": raw_sha,
            "locator": {"json_pointer": pointer}, "license": "SEC public data"}


def _archive_url(cik: str, accession: str, document: str | None) -> str | None:
    if not document:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"


def parse_edgar_submissions(payload: Mapping[str, Any], *, cik: str, raw_sha: str, max_filings: int = 50) -> list[dict[str, Any]]:
    recent = dict(dict(payload.get("filings") or {}).get("recent") or {})
    if not payload.get("name") or cik10(payload.get("cik")) != cik or not isinstance(recent.get("accessionNumber"), list):
        raise SourcePackError("schema_drift", "submissions record lacks name, CIK or recent filings")
    key = f"sec-edgar:cik:{cik}"
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    identifiers = [{"scheme": "sec-cik", "value": cik, "authority": "SEC"}]
    identifiers += [{"scheme": "ticker", "value": t, "authority": (payload.get("exchanges") or [None] * (i + 1))[i]}
                    for i, t in enumerate(payload.get("tickers") or []) if t]
    if payload.get("lei"):
        identifiers.append({"scheme": "lei", "value": payload["lei"], "authority": "as carried by SEC submissions"})
    # EDGAR state codes mix US states with foreign codes (e.g. X0); the native
    # code is kept and no jurisdiction is inferred from it.
    records = [record("legal_entity", key, _sec_source(cik, "submissions", raw_sha, "", url),
                      name=payload["name"], jurisdiction=None, identifiers=identifiers,
                      entity_type=payload.get("entityType"),
                      other_names=[{"name": f.get("name"), "type": "former", "from": f.get("from"), "to": f.get("to")}
                                   for f in payload.get("formerNames") or []],
                      native={"sic": payload.get("sic"), "sic_description": payload.get("sicDescription"),
                              "state_of_incorporation": payload.get("stateOfIncorporation")})]
    for index, former in enumerate(payload.get("formerNames") or []):
        ended = _date(former.get("to"))
        records.append(record("corporate_event", f"sec-edgar:name-change:{cik}:{index}",
                              _sec_source(cik, "submissions", raw_sha, f"/formerNames/{index}", url), entity_key=key,
                              event_type="name_change", event_date=ended, date_status="stated" if ended else "unknown",
                              description=f"Former name {former.get('name')} as listed by SEC"))
    columns = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "primaryDocDescription")
    count = len(recent["accessionNumber"])
    if any(len(recent.get(c) or []) != count for c in columns):
        raise SourcePackError("schema_drift", "recent filing columns differ in length")
    for index in range(min(count, max_filings)):
        accession, form = recent["accessionNumber"][index], recent["form"][index]
        records.append(record(
            "filing_reference", f"sec-edgar:filing:{accession}",
            _sec_source(cik, "submissions", raw_sha, f"/filings/recent/accessionNumber/{index}", url),
            entity_key=key, form_type=form, accession_number=accession, filing_date=_date(recent["filingDate"][index]),
            period_of_report=_date(recent["reportDate"][index]),
            document_url=_archive_url(cik, accession, recent["primaryDocument"][index]),
            description=(recent["primaryDocDescription"][index] or form)
                        + (" (beneficial-ownership form; reference only unless selected for parsing)"
                           if form in BENEFICIAL_OWNERSHIP_FORMS else ""),
            parsed=False,
        ))
    return records


def parse_edgar_companyfacts(payload: Mapping[str, Any], *, cik: str, raw_sha: str) -> list[dict[str, Any]]:
    """Shares-outstanding facts cite their filings; each cited accession becomes a reference."""
    if cik10(payload.get("cik")) != cik or not isinstance(payload.get("facts"), dict):
        raise SourcePackError("schema_drift", "company facts lack CIK or facts")
    key, url = f"sec-edgar:cik:{cik}", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    shares = dict(dict(dict(payload["facts"].get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {}).get("units") or {})
    records, seen = [], set()
    for index, fact in enumerate(shares.get("shares") or []):
        accession = fact.get("accn")
        if not accession or accession in seen:
            continue
        seen.add(accession)
        records.append(record(
            "filing_reference", f"sec-edgar:facts-filing:{accession}",
            _sec_source(cik, "companyfacts", raw_sha, f"/facts/dei/EntityCommonStockSharesOutstanding/units/shares/{index}", url),
            entity_key=key, form_type=str(fact.get("form") or "unknown"), accession_number=accession,
            filing_date=_date(fact.get("filed")), period_of_report=_date(fact.get("end")),
            description=f"XBRL company facts cite this filing: dei:EntityCommonStockSharesOutstanding = "
                        f"{fact.get('val')} shares as of {fact.get('end')}",
            parsed=False, native={"fact": "dei:EntityCommonStockSharesOutstanding", "value": str(fact.get("val"))},
        ))
    return records


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(element: Any, *names: str) -> str | None:
    wanted = set(names)
    for child in element.iter():
        if _local(child.tag) in wanted and (child.text or "").strip():
            return child.text.strip()
    return None


_PERSON_CONTAINERS = ("coverPageHeaderReportingPersonDetails", "reportingPersonInfo", "reportingPersonDetails")


def parse_schedule_13dg(raw: bytes, *, cik: str, accession: str, form: str, document_url: str, raw_sha: str) -> list[dict[str, Any]]:
    """Cover-page figures of an XML Schedule 13D/13G become assertions; anything else stays a reference.

    Element names follow the EDGAR Schedule 13D/13G XML technical
    specification as read for this adapter; they are matched by local name
    and are unverified against live filings (see LIVE_VERIFICATION). A
    document without recognizable cover-page fields is kept as an unparsed
    filing reference, never guessed.
    """
    subject = f"sec-edgar:cik:{cik}"
    source = {"provider": "sec-edgar", "provider_record_id": f"{accession}:primary_doc", "url": document_url,
              "raw_sha256": raw_sha, "license": "SEC public data"}
    persons = []
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        root = None
    if root is not None:
        issuer_cik = _first_text(root, "issuerCik", "issuerCIK")
        if issuer_cik and cik10(issuer_cik) != cik:
            raise SourcePackError("schema_drift", "Schedule 13D/13G names another issuer")
        event = _date(_first_text(root, "dateOfEvent", "eventDateRequiresFilingThisStatement"))
        security_class = _first_text(root, "securitiesClassTitle", "classOfSecurities", "titleOfClassOfSecurities")
        for index, container in enumerate(e for e in root.iter() if _local(e.tag) in _PERSON_CONTAINERS):
            name = _first_text(container, "reportingPersonName", "nameOfReportingPerson")
            percent = _first_text(container, "classPercent", "percentOfClass")
            if not name or percent is None:
                continue
            percent = percent.replace("%", "").strip()
            if not re.fullmatch(r"\d{1,3}(\.\d+)?", percent):
                continue
            shares = (_first_text(container, "aggregateAmountOwned", "reportingPersonBeneficiallyOwnedAggregateNumberOfShares") or "").replace(",", "")
            persons.append({"index": index, "name": name, "percent": percent,
                            "shares": shares if re.fullmatch(r"\d+(\.\d+)?", shares) else None,
                            "cik": _first_text(container, "reportingPersonCIK", "reportingPersonCik"),
                            "type": _first_text(container, "typeOfReportingPerson"),
                            "powers": {k: _first_text(container, k) for k in (
                                "soleVotingPower", "sharedVotingPower", "soleDispositivePower", "sharedDispositivePower")},
                            "event": event, "class": security_class})
    # Keyed apart from the submissions index entry for the same accession: the
    # index and the primary document are two statements about one filing.
    records = [record("filing_reference", f"sec-edgar:document:{accession}", {**source, "locator": {"xpath": "/"}},
                      entity_key=subject, form_type=form, accession_number=accession, document_url=document_url,
                      description=f"{form} primary document"
                                  + ("; cover-page ownership figures parsed" if persons else "; not parsed (no recognizable cover-page fields)"),
                      parsed=bool(persons), filers=[p["name"] for p in persons] or None)]
    for person in persons:
        is_person = (person["type"] or "").upper() == "IN"
        # A reporting person named on a cover page is its own source record; it
        # is linked to that filer's submissions entity only by reviewed identity.
        holder_key = f"sec-edgar:reporting-person:{cik10(person['cik']) if person['cik'] else _slug(person['name'])}"
        records.append(record(
            "person" if is_person else "legal_entity", holder_key,
            {**source, "locator": {"xpath": f"//{_PERSON_CONTAINERS[0]}[{person['index'] + 1}]"}},
            name=person["name"], identifiers=[{"scheme": "sec-cik", "value": cik10(person["cik"])}] if person["cik"] else [],
            **({} if is_person else {"jurisdiction": None}),
            native={"type_of_reporting_person": person["type"], "named_in": accession}))
        records.append(record(
            "ownership_assertion", f"sec-edgar:13dg:{accession}:{person['index']}",
            {**source, "statement_id": accession, "locator": {"xpath": f"//{_PERSON_CONTAINERS[0]}[{person['index'] + 1}]",
                                                             "quote": f"{person['percent']}%"}},
            subject_key=subject, assertion_kind="shareholding",
            holder={"key": holder_key, "name": person["name"], "kind": "person" if is_person else "entity"},
            share={"exact": person["percent"], "shares": person["shares"], "class": person["class"],
                   "native": "percent of class as reported on the cover page"},
            validity=_validity(person["event"], None, open_when_missing=False), statement_date=person["event"],
            basis=f"{form} cover page, as reported by the filer",
            native={"form": form, "voting_and_dispositive_power": person["powers"],
                    "note": "beneficial ownership as reported by the filer under the Exchange Act; not a Noesis "
                            "determination"},
        ))
    return records


# ---------------------------------------------------------- Open Ownership (O06)


def _bods_scheme(identifier: Mapping[str, Any]) -> dict[str, Any] | None:
    scheme = str(identifier.get("scheme") or "")
    value = identifier.get("id")
    if not value:
        return None
    return {"scheme": _BODS_SCHEMES.get(scheme, scheme.lower() or "unknown"), "value": str(value),
            "authority": identifier.get("schemeName")}


def _bods_source(statement: Mapping[str, Any], *, url: str, raw_sha: str, index: int) -> dict[str, Any]:
    details = dict(statement.get("publicationDetails") or {})
    source = dict(statement.get("source") or {})
    types = source.get("type") or []
    return {"provider": "open-ownership", "provider_record_id": str(statement["statementID"]), "url": url,
            "statement_id": str(statement["statementID"]),
            "publisher": dict(details.get("publisher") or {}).get("name"),
            "source_type": ",".join(types) if isinstance(types, list) else str(types),
            "license": details.get("license"), "raw_sha256": raw_sha,
            "locator": {"json_pointer": f"/{index}"}, "note": source.get("description")}


def _bods_share(share: Any) -> dict[str, Any] | None:
    if not isinstance(share, dict) or not share:
        return None
    if share.get("exact") is not None:
        return {"exact": str(share["exact"]), "native": share}
    low = share.get("minimum", share.get("exclusiveMinimum"))
    high = share.get("maximum", share.get("exclusiveMaximum"))
    if low is None and high is None:
        return None
    return {"band": {"min": None if low is None else str(low), "max": None if high is None else str(high),
                     "min_inclusive": None if low is None else "minimum" in share,
                     "max_inclusive": None if high is None else "maximum" in share}, "native": share}


def parse_bods(statements: Sequence[Mapping[str, Any]], *, url: str, raw_sha: str, offset: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project BODS 0.2/0.3 statements; unsupported sources and versions are counted, not guessed."""
    publisher_coverage = PROVIDER_CONTRACTS["open-ownership"]["publisher_coverage"]
    records: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {"publishers": {}, "not_implemented": {}, "unsupported_version": 0, "statements": 0}
    for position, statement in enumerate(statements):
        index = offset + position
        coverage["statements"] += 1
        if not isinstance(statement, dict) or "recordType" in statement or not statement.get("statementID"):
            coverage["unsupported_version"] += 1
            continue
        details = dict(statement.get("publicationDetails") or {})
        publisher = dict(details.get("publisher") or {}).get("name") or "unknown publisher"
        description = dict(statement.get("source") or {}).get("description") or "unstated source"
        support = publisher_coverage.get(description, {"status": "not-implemented", "reason": "source not reviewed"})
        if support["status"] != "implemented":
            coverage["not_implemented"][description] = coverage["not_implemented"].get(description, 0) + 1
            continue
        tally = coverage["publishers"].setdefault(publisher, {})
        tally[description] = tally.get(description, 0) + 1
        source = _bods_source(statement, url=url, raw_sha=raw_sha, index=index)
        key = f"open-ownership:statement:{statement['statementID']}"
        annotations = statement.get("annotations")
        kind = statement.get("statementType")
        try:
            if kind == "entityStatement":
                jurisdiction = dict(statement.get("incorporatedInJurisdiction") or {})
                records.append(record(
                    "legal_entity", key, source, name=statement.get("name") or "unnamed entity",
                    jurisdiction=jurisdiction.get("code"),
                    identifiers=[i for i in (_bods_scheme(x) for x in statement.get("identifiers") or []) if i],
                    entity_type=statement.get("entityType"), founding_date=_date(statement.get("foundingDate")),
                    dissolution_date=_date(statement.get("dissolutionDate")),
                    native={"statement_date": statement.get("statementDate"), "annotations": annotations,
                            "replaces_statements": statement.get("replacesStatements")}))
            elif kind == "personStatement":
                names = statement.get("names") or [{}]
                records.append(record(
                    "person", key, source, owner_scoped=True, name=names[0].get("fullName") or "unnamed person",
                    identifiers=[i for i in (_bods_scheme(x) for x in statement.get("identifiers") or []) if i],
                    nationalities=[n.get("code") for n in statement.get("nationalities") or []],
                    native_kind=statement.get("personType"),
                    native={"statement_date": statement.get("statementDate"), "annotations": annotations}))
            elif kind == "ownershipOrControlStatement":
                subject = dict(statement.get("subject") or {}).get("describedByEntityStatement")
                party = dict(statement.get("interestedParty") or {})
                if not subject:
                    raise SourcePackError("schema_drift", "ownership statement lacks a subject entity statement")
                subject_key = f"open-ownership:statement:{subject}"
                if party.get("unspecified"):
                    unspecified = dict(party["unspecified"])
                    records.append(record(
                        "ownership_assertion", f"{key}:unspecified", source, subject_key=subject_key,
                        assertion_kind="reporting_exception", holder=None,
                        reporting_exception={"level": "any", "category": str(unspecified.get("reason") or "unknown"),
                                             "reason": unspecified.get("description"), "native_text": None},
                        validity=_validity(None, None, open_when_missing=False),
                        statement_date=_date(statement.get("statementDate")),
                        native={"annotations": annotations}))
                    continue
                person = party.get("describedByPersonStatement")
                holder = {"key": f"open-ownership:statement:{person or party.get('describedByEntityStatement')}",
                          "kind": "person" if person else "entity"}
                if not person and not party.get("describedByEntityStatement"):
                    holder = {"key": None, "name": party.get("name") or "unidentified party", "kind": "unknown"}
                for number, interest in enumerate(statement.get("interests") or [{}]):
                    itype = str(interest.get("type") or "unknown-interest")
                    records.append(record(
                        "ownership_assertion", f"{key}:interest:{number}", source, owner_scoped=bool(person) or None,
                        subject_key=subject_key, assertion_kind=_BODS_INTERESTS.get(itype, "other_control"),
                        holder=holder, share=_bods_share(interest.get("share")),
                        validity=_validity(interest.get("startDate"), interest.get("endDate"), open_when_missing=False),
                        statement_date=_date(statement.get("statementDate")),
                        basis="BODS ownership-or-control statement as published",
                        native={"interest_type": itype, "details": interest.get("details"),
                                "direct_or_indirect": interest.get("interestLevel") or interest.get("directOrIndirect"),
                                "beneficial_ownership_or_control_as_stated": interest.get("beneficialOwnershipOrControl"),
                                "annotations": annotations}))
            else:
                coverage["unsupported_version"] += 1
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", f"BODS statement {statement['statementID']}: {exc}") from exc
    return records, coverage


def load_bods(raw: bytes) -> list[Any]:
    text = raw.decode("utf-8")
    stripped = text.lstrip()
    try:
        if stripped.startswith("["):
            value = json.loads(text)
            if not isinstance(value, list):
                raise ValueError
            return value
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError as exc:
        raise SourcePackError("schema_drift", "BODS file is neither a JSON array nor JSON Lines") from exc


# --------------------------------------------------------- runtime adapters


class _WorkAdapter:
    """One selected work item (and one page of it) per runtime page."""

    accepts_transport = True
    provider = ""
    min_interval_s = 0.0

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.selection = dict(self.source.get("ownership") or {})
        self.live = transport is None
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.secret, self.sleep, self._last = secret, sleep, 0.0
        endpoint_host = urlsplit(self.source["endpoint"]).hostname
        if endpoint_host not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("network_policy", f"{self.provider} sources use their declared official host")
        self.work = self._work()
        if not self.work:
            raise SourcePackError("unbounded_source", "select at least one item")
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "ownership": {"work": len(self.work), "provider": self.provider},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _work(self) -> list[tuple[str, ...]]:
        raise NotImplementedError

    def _scope(self) -> str:
        return _sha(json.dumps({"endpoint": self.source["endpoint"], "work": self.work}, sort_keys=True).encode())

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def _get(self, url: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any], bytes]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        if urlsplit(url).hostname not in PROVIDER_HOSTS[self.provider] or not url.startswith("https://"):
            raise SourcePackError("network_policy", "requests stay on the provider's declared hosts")
        if self.live and self.min_interval_s:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
        response = self.transport(url=url, params=dict(params or {}), headers={**self._headers(), **dict(headers or {})},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in (401, 403):
            raise SourcePackError("authentication_failed", f"{self.provider} rejected the credential or user agent")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400 and status != 404:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        return status, response_headers, raw

    def _json(self, raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise SourcePackError("schema_drift", f"{self.provider} returned malformed JSON") from exc

    def _envelope(self, subject: str, part: str, raw: bytes, native: Any, records: list[dict[str, Any]], *,
                  outcome: str = "returned", coverage: Mapping[str, Any] | None = None,
                  page: int = 0) -> dict[str, Any]:
        # Each page of a paginated part is its own document identity.
        suffix = f"@{page}" if page else ""
        return {"id": f"{self.provider}:{subject}:{part}{suffix}", "title": f"{self.provider} {part} {subject}{suffix}",
                "language": "en", "url": self.source["endpoint"],
                "ownership_part": {"contract": PART_CONTRACT, "provider": self.provider, "part": part,
                                   "subject": subject, "raw_sha256": _sha(raw), "native": native,
                                   "records": records, "outcome": outcome, "coverage": dict(coverage or {})}}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"} or dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "ownership runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index, start = int(state.get("i", 0)), int(state.get("start", 0))
        if index >= len(self.work):
            return RuntimePage((), None, 0, receipt={"status": 200})
        limit = max(1, min(int(request.get("limit") or 100), 100))
        envelope, size, next_start, status = self._fetch(self.work[index], start=start, limit=limit)
        if next_start is not None:
            state = {"i": index, "start": next_start}
        else:
            state = {"i": index + 1, "start": 0}
        more = state["i"] < len(self.work)
        next_cursor = json.dumps({**state, "scope": self._scope()}, sort_keys=True) if more else None
        part = envelope["ownership_part"]
        return RuntimePage((envelope,), next_cursor, size, receipt={
            "status": status, "outcome": part["outcome"], "provider": self.provider, "part": part["part"],
            "subject": part["subject"], "response_sha256": part["raw_sha256"], "work_index": index,
            "work_size": len(self.work), "records": len(part["records"])})

    def _fetch(self, item: tuple[str, ...], *, start: int, limit: int):
        raise NotImplementedError


class CompaniesHouseAdapter(_WorkAdapter):
    provider = "companies-house"
    min_interval_s = 0.5
    PARTS = ("profile", "officers", "psc", "psc-statements", "exemptions", "filing-history")
    PATHS = {"profile": "/company/{n}", "officers": "/company/{n}/officers",
             "psc": "/company/{n}/persons-with-significant-control",
             "psc-statements": "/company/{n}/persons-with-significant-control-statements",
             "exemptions": "/company/{n}/exemptions", "filing-history": "/company/{n}/filing-history"}
    LISTS = {"officers", "psc", "psc-statements", "filing-history"}

    def _work(self):
        companies = [_ch_company(str(c).upper()) for c in self.selection.get("companies") or []]
        if len(companies) > 25:
            raise SourcePackError("unbounded_source", "companies-house sources select 1-25 companies")
        parts = list(self.selection.get("parts") or self.PARTS)
        if set(parts) - set(self.PARTS):
            raise SourcePackError("invalid_mapping", f"companies-house parts are drawn from {self.PARTS}")
        return [(c, p) for c in companies for p in parts]

    def _headers(self):
        if not self.secret:
            raise SourcePackError("authentication_failed", "Companies House requires an API key")
        token = base64.b64encode(f"{self.secret}:".encode()).decode()
        return {"Accept": "application/json", "Authorization": f"Basic {token}"}

    def _fetch(self, item, *, start, limit):
        company, part = item
        url = self.source["endpoint"].rstrip("/") + self.PATHS[part].format(n=company)
        params = {"items_per_page": str(limit), "start_index": str(start)} if part in self.LISTS else {}
        status, _, raw = self._get(url, params)
        if status == 404:
            return self._envelope(company, part, raw, None, [], outcome="none_reported"), len(raw), None, status
        payload = self._json(raw)
        sha = _sha(raw)
        parser = {"profile": lambda: parse_ch_profile(payload, company=company, raw_sha=sha),
                  "officers": lambda: parse_ch_officers(payload, company=company, raw_sha=sha, offset=start),
                  "psc": lambda: parse_ch_psc(payload, company=company, raw_sha=sha, offset=start),
                  "psc-statements": lambda: parse_ch_psc_statements(payload, company=company, raw_sha=sha),
                  "exemptions": lambda: parse_ch_exemptions(payload, company=company, raw_sha=sha),
                  "filing-history": lambda: parse_ch_filing_history(payload, company=company, raw_sha=sha, offset=start)}[part]
        try:
            records = parser()
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc
        next_start = None
        if part in self.LISTS:
            total = payload.get("total_results", payload.get("total_count"))
            fetched = start + len(payload.get("items") or [])
            if isinstance(total, int) and fetched < total and payload.get("items"):
                next_start = fetched
        return self._envelope(company, part, raw, payload, records, page=start,
                              coverage={"start_index": start, "total": payload.get("total_results", payload.get("total_count"))}), len(raw), next_start, status


class EdgarAdapter(_WorkAdapter):
    provider = "sec-edgar"
    min_interval_s = 0.12

    def _work(self):
        ciks = [cik10(c) for c in self.selection.get("ciks") or []]
        filings = list(self.selection.get("filings") or [])
        if len(ciks) > 25 or len(filings) > 25:
            raise SourcePackError("unbounded_source", "sec-edgar sources select 1-25 CIKs and 0-25 filings")
        work = [(c, p) for c in ciks for p in ("submissions", "companyfacts")]
        for filing in filings:
            accession = str(filing.get("accession") or "")
            document = str(filing.get("document") or "")
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", document):
                raise SourcePackError("unbounded_source", "filings select an accession number and document name")
            work.append((cik10(filing.get("cik")), "document", accession, document, str(filing.get("form") or "unknown")))
        return work

    def _headers(self):
        if not self.secret or "@" not in str(self.secret) and self.secret != FIXTURE_SECRET:
            raise SourcePackError("authentication_failed", "SEC fair access requires a declared contact user agent")
        return {"Accept": "application/json", "User-Agent": f"Noesis corporate-ownership research {self.secret}"}

    def _fetch(self, item, *, start, limit):
        cik, part = item[0], item[1]
        host = self.source["endpoint"].rstrip("/")
        if part == "submissions":
            url = f"{host}/submissions/CIK{cik}.json"
        elif part == "companyfacts":
            url = f"{host}/api/xbrl/companyfacts/CIK{cik}.json"
        else:
            url = _archive_url(cik, item[2], item[3])
        status, _, raw = self._get(url)
        if status == 404:
            return self._envelope(cik, part, raw, None, [], outcome="none_reported"), len(raw), None, status
        sha = _sha(raw)
        try:
            if part == "document":
                records = parse_schedule_13dg(raw, cik=cik, accession=item[2], form=item[4], document_url=url, raw_sha=sha)
                native = raw.decode("utf-8", "replace")[:200_000]
            else:
                payload = self._json(raw)
                native = payload
                records = (parse_edgar_submissions(payload, cik=cik, raw_sha=sha,
                                                   max_filings=int(self.selection.get("max_filings") or 50))
                           if part == "submissions" else parse_edgar_companyfacts(payload, cik=cik, raw_sha=sha))
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc
        subject = cik if part != "document" else f"{cik}:{item[2]}"
        return self._envelope(subject, part, raw, native, records), len(raw), None, status


class BodsAdapter(_WorkAdapter):
    provider = "open-ownership"

    def _work(self):
        files = list(self.selection.get("files") or [])
        if len(files) > 10 or any(not re.fullmatch(r"/[A-Za-z0-9._/-]{1,300}", str(f)) or ".." in str(f) for f in files):
            raise SourcePackError("unbounded_source", "bods sources select 1-10 statement files by path")
        return [(str(f),) for f in files]

    def _fetch(self, item, *, start, limit):
        path = item[0]
        url = self.source["endpoint"].rstrip("/") + path
        status, _, raw = self._get(url)
        if status == 404:
            return self._envelope(path, "statements", raw, None, [], outcome="none_reported"), len(raw), None, status
        statements = load_bods(raw)
        page = statements[start:start + limit]
        records, coverage = parse_bods(page, url=url, raw_sha=_sha(raw), offset=start)
        coverage.update({"offset": start, "total": len(statements)})
        next_start = start + len(page) if start + len(page) < len(statements) else None
        return (self._envelope(f"{path}#{start}", "statements", raw, page, records, coverage=coverage),
                len(json.dumps(page).encode()), next_start, status)


ADAPTERS = {"companies-house": CompaniesHouseAdapter, "sec-edgar-ownership": EdgarAdapter, "bods": BodsAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored native responses by URL path; unlisted paths are HTTP 404."""
    by_path = {(page["request"], page.get("start_index")): page for page in pages}

    def transport(*, url, params, headers, timeout):
        del timeout
        path = urlsplit(url).path
        page = by_path.get((path, dict(params or {}).get("start_index"))) or by_path.get((path, None))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        if page.get("require_header") and page["require_header"] not in {str(k) for k in headers}:
            return {"status": 401, "headers": {}, "content": b""}
        body = page.get("body")
        if isinstance(body, (dict, list)):
            content = json.dumps(body).encode()
        elif page.get("jsonl") is not None:
            content = "\n".join(json.dumps(line) for line in page["jsonl"]).encode()
        else:
            content = (body or "").encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}), "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])),
                                            secret=FIXTURE_SECRET)
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records

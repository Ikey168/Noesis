"""OpenCorporates company identity and officers as ownership records (#2887).

OpenCorporates aggregates national registers; it is enrichment and identity
evidence, never an authoritative substitute for the register (see
``PROVIDER_CONTRACTS["opencorporates"]`` and closed issue #1483, whose
regional provider keeps search and document enrichment). This adapter looks
up explicitly selected registrations through API v0.4
(``/companies/{jurisdiction_code}/{company_number}``) with the key in
``NOESIS_OPENCORPORATES_API_KEY`` and emits:

* a ``legal_entity`` (provider ``opencorporates``) carrying the
  OpenCorporates identifier and, for configured jurisdictions only, the
  national register number (``gb-coh``, ``dk-cvr``), so identity review can
  propose a cross-referenced match to register records; nothing is merged;
* one ``officer_role`` per officer embedded in the response, as published.

Every record names OpenCorporates as publisher and links the company page
(ODbL attribution; verify against the key's plan). The API token is sent as a
query parameter and is never stored. Field names follow the v0.4 reference
as read for this adapter and are unverified against live responses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from src.ingestion.ownership_providers import (
    FIXTURE_SECRET,
    _date,
    _sha,
    _WorkAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_records import OwnershipRecordError, record

LICENCE = "ODbL 1.0, attribution: from OpenCorporates (verify against the key's plan)"
# OpenCorporates jurisdiction -> the national register scheme its company_number is, configured not inferred.
REGISTER_SCHEMES = {"gb": ("gb-coh", "Companies House"), "dk": ("dk-cvr", "Erhvervsstyrelsen (CVR)")}
_JURISDICTION = re.compile(r"[a-z]{2}(?:_[a-z0-9]{1,6})?")
_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,60}")


def entity_key(jurisdiction: str, number: str) -> str:
    return f"opencorporates:company:{jurisdiction}:{number}"


def parse_opencorporates_company(payload: Mapping[str, Any], *, jurisdiction: str, number: str,
                                 raw_sha: str) -> list[dict[str, Any]]:
    company = dict(dict(payload.get("results") or {}).get("company") or {})
    if company.get("jurisdiction_code") != jurisdiction or str(company.get("company_number")) != number \
            or not company.get("name"):
        raise SourcePackError("schema_drift", "OpenCorporates response lacks the requested registration")
    key = entity_key(jurisdiction, number)
    page = company.get("opencorporates_url")
    url = page if isinstance(page, str) and page.startswith("https://") else \
        f"https://opencorporates.com/companies/{jurisdiction}/{quote(number, safe='')}"
    base = {"provider": "opencorporates", "provider_record_id": f"{jurisdiction}/{number}", "url": url,
            "raw_sha256": raw_sha, "license": LICENCE, "publisher": "OpenCorporates", "source_type": "aggregator",
            "note": "aggregator record; the national register stays authoritative"}
    identifiers = [{"scheme": "opencorporates", "value": f"{jurisdiction}/{number}", "authority": "OpenCorporates"}]
    country = jurisdiction.split("_")[0]
    if country in REGISTER_SCHEMES and "_" not in jurisdiction:
        scheme, authority = REGISTER_SCHEMES[country]
        identifiers.append({"scheme": scheme, "value": number, "authority": f"{authority}, as carried by OpenCorporates"})
    source_info = dict(company.get("source") or {})
    records = [record(
        "legal_entity", key, {**base, "locator": {"json_pointer": "/results/company"}}, name=company["name"],
        jurisdiction=jurisdiction.upper().replace("_", "-"), identifiers=identifiers,
        entity_type=company.get("company_type"), status=company.get("current_status"),
        other_names=[{"name": p.get("company_name"), "type": "previous", "from": p.get("start_date"),
                      "to": p.get("end_date")} for p in company.get("previous_names") or [] if isinstance(p, dict)],
        founding_date=_date(company.get("incorporation_date")), dissolution_date=_date(company.get("dissolution_date")),
        addresses=[{"type": "registered", "text": company.get("registered_address_in_full")}]
        if company.get("registered_address_in_full") else [],
        native={"inactive": company.get("inactive"), "branch": company.get("branch"),
                "registry_url": company.get("registry_url"), "retrieved_at": company.get("retrieved_at"),
                "updated_at": company.get("updated_at"),
                "register_source": {k: source_info.get(k) for k in ("publisher", "url", "retrieved_at")}})]
    for index, item in enumerate(company.get("officers") or []):
        officer = dict(dict(item or {}).get("officer") or {})
        if not officer.get("name") or not officer.get("position"):
            raise SourcePackError("schema_drift", "OpenCorporates officer lacks name or position")
        officer_id = str(officer.get("id") or _sha(json.dumps(officer, sort_keys=True).encode())[:16])
        start = _date(officer.get("start_date"))
        try:
            records.append(record(
                "officer_role", f"opencorporates:officer:{jurisdiction}:{number}:{officer_id}:{_slug(officer['position'])}",
                {**base, "locator": {"json_pointer": f"/results/company/officers/{index}/officer"}},
                entity_key=key, officer={"name": officer["name"], "key": f"opencorporates:officer:{officer_id}",
                                         "kind": "unknown"},
                role=str(officer["position"]), appointed_on=start, appointed_status="stated" if start else "unknown",
                resigned_on=_date(officer.get("end_date")),
                native={"inactive": officer.get("inactive"), "opencorporates_url": officer.get("opencorporates_url")}))
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc
    return records


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:80] or "officer"


class OpenCorporatesAdapter(_WorkAdapter):
    provider = "opencorporates"
    min_interval_s = 1.0

    def _work(self):
        registrations = list(self.selection.get("registrations") or [])
        if len(registrations) > 25:
            raise SourcePackError("unbounded_source", "opencorporates sources select 1-25 registrations")
        work = []
        for item in registrations:
            jurisdiction = str(dict(item).get("jurisdiction_code") or "")
            number = str(dict(item).get("company_number") or "")
            if not _JURISDICTION.fullmatch(jurisdiction) or not _NUMBER.fullmatch(number):
                raise SourcePackError("unbounded_source", "registrations name a jurisdiction_code and company_number")
            work.append((jurisdiction, number))
        return work

    def _headers(self):
        if not self.secret:
            raise SourcePackError("authentication_failed", "OpenCorporates requires an API token")
        return {"Accept": "application/json"}

    def _fetch(self, item, *, start, limit):
        jurisdiction, number = item
        self._headers()
        url = f"{self.source['endpoint'].rstrip('/')}/companies/{jurisdiction}/{quote(number, safe='')}"
        status, _, raw = self._get(url, {"api_token": self.secret})
        subject = f"{jurisdiction}/{number}"
        if status == 404:
            return self._envelope(subject, "company", raw, None, [], outcome="none_reported"), len(raw), None, status
        payload = self._json(raw)
        try:
            records = parse_opencorporates_company(payload, jurisdiction=jurisdiction, number=number, raw_sha=_sha(raw))
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc
        return self._envelope(subject, "company", raw, payload, records), len(raw), None, status


ADAPTERS = {"opencorporates": OpenCorporatesAdapter}


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


__all__ = [
    "ADAPTERS",
    "OpenCorporatesAdapter",
    "entity_key",
    "fixture_transport",
    "parse_opencorporates_company",
    "replay_native_fixture",
]

"""US FEC and UK Electoral Commission campaign-finance acquisition for the Political pack (#2209, CF01, CF03-CF06).

One native connector, ``campaign-finance``, reads a bounded, declared selection
from one documented provider per source and emits
``noesis-campaign-finance-record-v1`` records exactly as the regulator
published them:

* ``openfec-committee-history-json`` - OpenFEC ``/committee/{id}/history/``:
  one committee registration (Form 1) state per two-year period, a dated
  revision of the committee record (designation, type, party, affiliated
  committee and candidate ids as published);
* ``openfec-candidate-history-json`` - OpenFEC ``/candidate/{id}/history/``:
  the candidate's registration per two-year period (name, office, state,
  district, party, election years as published);
* ``openfec-filings-json`` - OpenFEC ``/committee/{id}/filings/``: every
  filing version of a committee in a cycle with its amendment indicator,
  ``amendment_chain``, ``most_recent`` flag (stored as published, never
  inferred) and summary totals as reported;
* ``openfec-schedule-a-json`` / ``openfec-schedule-b-json`` - itemised receipts
  and disbursements keyed by file number, schedule and ``sub_id``; memo items
  are labelled and kept distinct; individual counterparties are minimised;
* ``openfec-schedule-e-json`` - independent expenditures with the
  support/oppose indicator and candidate id verbatim; 24/48-hour notices and
  the periodic report covering the same expenditure are separate assertions;
* ``ukec-donations-csv`` / ``ukec-spending-csv`` - Electoral Commission search
  exports keyed by ``ECRef`` and ``RegulatedEntityId``; donor status and company
  numbers as published; individual donors minimised.

**Data minimisation (CF01).** Natural persons - FEC entity types ``IND`` and
``CAN``, Commission donor status ``Individual`` - keep only amount, date,
filing version, schedule, memo flag, type and the regulator's line reference.
Their name, address, city, ZIP or postcode, employer, occupation, donor or
contributor id and per-person aggregates are dropped *here*, before any record,
document or receipt exists, and listed under ``minimisation.withheld``. The
store refuses any individual item that still carries one of them.

A unit is all-or-nothing: a list longer than the declared page bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest; the API key travels in the ``X-Api-Key`` header and never appears in a
receipt or a record. Nothing here scores influence, infers undisclosed
funding or profiles a donor.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-campaign-finance-record-v1"
MINIMISATION_POLICY = "campaign-finance-minimisation-v1"
CONNECTOR = "campaign-finance"
PER_PAGE = 100
MAX_PAGES_PER_UNIT = 5
UK_ROW_CAP = 2000
MAX_UNITS = 50
FEC_ATTRIBUTION = "Federal Election Commission data (US government work); 52 U.S.C. 30111(a)(4) applies."
EC_ATTRIBUTION = "Contains Electoral Commission data licensed under the Open Government Licence v3.0."
REVIEW_BOUNDARY = ("Records are what the regulator published. No influence score, 'dark money' inference, "
                   "undisclosed-funding inference or individual-donor profiling.")

# format -> provider, jurisdiction, the selection list it reads and whether a secret (API key) is required
FORMATS: dict[str, dict[str, Any]] = {
    "openfec-committee-history-json": {"provider": "openfec", "jurisdiction": "US", "unit": "committees",
                                       "keyed": True},
    "openfec-candidate-history-json": {"provider": "openfec", "jurisdiction": "US", "unit": "candidates",
                                       "keyed": True},
    "openfec-filings-json": {"provider": "openfec", "jurisdiction": "US", "unit": "committee_cycles", "keyed": True},
    "openfec-schedule-a-json": {"provider": "openfec", "jurisdiction": "US", "unit": "committee_cycles",
                                "keyed": True},
    "openfec-schedule-b-json": {"provider": "openfec", "jurisdiction": "US", "unit": "committee_cycles",
                                "keyed": True},
    "openfec-schedule-e-json": {"provider": "openfec", "jurisdiction": "US", "unit": "committee_cycles",
                                "keyed": True},
    "ukec-donations-csv": {"provider": "uk-electoral-commission", "jurisdiction": "GB",
                           "unit": "regulated_entities", "keyed": False},
    "ukec-spending-csv": {"provider": "uk-electoral-commission", "jurisdiction": "GB",
                          "unit": "regulated_entities", "keyed": False},
}
RECORD_KINDS = ("committee", "candidate", "regulated-entity", "filing", "contribution", "expenditure",
                "independent-expenditure")

# CF01 access decisions. Endpoints, fields and terms are recorded from the providers' published documentation as
# known without network access; every item marked ``verify`` must be checked before a dated live run (CF14, #2529).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "openfec": {
        "publisher": "US Federal Election Commission (OpenFEC API v1)",
        "endpoints": ["/v1/committee/{committee_id}/history/", "/v1/candidate/{candidate_id}/history/",
                      "/v1/committee/{committee_id}/filings/", "/v1/schedules/schedule_a/",
                      "/v1/schedules/schedule_b/", "/v1/schedules/schedule_e/"],
        "formats": [f for f, spec in FORMATS.items() if spec["provider"] == "openfec"],
        "authentication": "api.data.gov key (required-secret NOESIS_OPENFEC_API_KEY) sent as the X-Api-Key header, "
        "never as the api_key query parameter, in a URL or in a receipt; DEMO_KEY is never used",
        "rate_limits": "1,000 requests per hour per key (7,200 on request); HTTP 429 with Retry-After (verify)",
        "pagination": "page/per_page <= 100 for history and filings; keyset last_index + last sort value for "
        "schedules; at most 5 pages per unit, a longer list is budget_exhausted, never truncated",
        "identifiers": ["committee ID", "candidate ID", "file_number (one per filing version)", "image_number",
                        "sub_id / transaction_id per line item"],
        "revisions": "every filing version has its own file_number; amendment_indicator N/A/T, amendment_chain "
        "(original to this version), most_recent and most_recent_file_number stored as published; a changed "
        "flag is a new revision of that version's record; line items are keyed by the file number they were "
        "reported in",
        "licence": "US government work, public domain; 52 U.S.C. 30111(a)(4) forbids using individual contributor "
        "information for soliciting contributions or commercial purposes (the FEC salts reports to detect misuse); "
        "individual contributor names and addresses are never stored (verify the current wording)",
        "attribution": FEC_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parsers in the documented v1 JSON shape; amendment_chain, most_recent, "
        "is_notice, support_oppose_indicator and the keyset field names must be verified",
    },
    "fec-bulk": {
        "publisher": "US Federal Election Commission (bulk data downloads)",
        "endpoints": ["https://www.fec.gov/data/browse-data/?tab=bulk-data"],
        "formats": [],
        "authentication": "none",
        "rate_limits": "none documented",
        "pagination": "whole-cycle ZIP files",
        "identifiers": ["CMTE_ID", "CAND_ID", "FILE_NUM", "SUB_ID", "TRAN_ID"],
        "revisions": "files are regenerated; AMNDT_IND per row",
        "licence": "as for OpenFEC, including 52 U.S.C. 30111(a)(4)",
        "attribution": FEC_ATTRIBUTION,
        "access_decision": "documented-not-acquired",
        "reason": "a whole-cycle file cannot be bounded to the declared committees; reserved for the CF14 live "
        "cross-check of API totals",
    },
    "uk-electoral-commission": {
        "publisher": "The Electoral Commission (donations and spending search, CSV export)",
        "endpoints": ["https://search.electoralcommission.org.uk/api/csv/Donations",
                      "https://search.electoralcommission.org.uk/api/csv/Spending"],
        "formats": ["ukec-donations-csv", "ukec-spending-csv"],
        "authentication": "none",
        "rate_limits": "undocumented (verify); one request per declared unit",
        "pagination": "rows/start; an export with UK_ROW_CAP rows is budget_exhausted, never truncated",
        "identifiers": ["ECRef per donation or spending item", "RegulatedEntityId", "DonorId",
                        "CompanyRegistrationNumber"],
        "revisions": "a published item is corrected in place: the same ECRef with different values is a revision "
        "linked to its predecessor; a late-reported donation appears in a later export and is flagged as observed "
        "after its return was first acquired; the export publishes no revision stamp",
        "licence": "Open Government Licence v3.0 (verify that the search data is covered); individual donors are "
        "personal data under the UK GDPR and are minimised",
        "attribution": EC_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parsers for the documented CSV columns; the export paths, the regulated-entity "
        "filter parameter and the spending columns must be verified",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (CF01): nothing implies complete coverage of a cycle, a party or an election.
BOUNDED_COVERAGE = {
    "us": "the committees, candidates and (committee, two-year period) pairs named in each source's selection; "
    "at most 50 units per source and 5 pages of 100 rows per unit; every filing version (original, amendments, "
    "terminations) of a declared committee cycle",
    "gb": "the regulated entities named in each selection with a declared date window of at most one year; at most "
    f"{UK_ROW_CAP} rows per unit",
    "contests": "contests reached through accepted identity matches with the elections feature's records",
}
# CF01 donor data-minimisation decision (docs/development/campaign-finance-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "natural_persons": {"fec_entity_types": ["IND", "CAN"], "ukec_donor_statuses": ["Individual"]},
    "stored_for_individuals": ["amount as reported", "date as reported", "filing version and schedule", "memo flag",
                               "receipt, disbursement or donation type", "regulator line reference (sub_id, "
                               "transaction_id, image number, ECRef)", "entity type or donor status as published",
                               "IsAggregation flag as published"],
    "never_stored_for_individuals": ["name", "street address", "city", "ZIP or postcode", "employer", "occupation",
                                     "donor or contributor id", "per-person year-to-date aggregate", "memo text"],
    "aggregated_as_published": "filing summary totals exactly as reported; never aggregated per person",
    "matching": "individual donors and payees are never matched, linked or expanded; they stay unmatched",
    "query_scope": "knowledge:political:campaign-finance:individual-items:read (in addition to the read scope) "
    "to see minimised individual items; otherwise they are counted per filing version",
    "retention": "retained with their filing version; no personal identifier is stored, so nothing personal "
    "remains to purge; no automatic expiry in the first coverage",
    "organisations": "committees, PACs, parties, companies, trade unions and other organisations are stored as "
    "published; Commission spending suppliers (status unpublished) are kept as published",
}
FEC_INDIVIDUAL_TYPES = frozenset({"IND", "CAN"})
UK_INDIVIDUAL_STATUSES = frozenset({"individual"})
# Keys that must never appear on a natural-person counterparty or anywhere in an individual line item's fields.
INDIVIDUAL_FORBIDDEN_KEYS = frozenset({
    "name", "first_name", "last_name", "middle_name", "prefix", "suffix", "street", "street_1", "street_2",
    "address", "city", "zip", "zip_code", "postcode", "employer", "occupation", "donor_id", "contributor_id",
    "aggregate", "aggregate_ytd", "memo_text", "purpose_of_visit",
})
_FEC_PERSON_SUFFIXES = ("name", "first_name", "last_name", "middle_name", "prefix", "suffix", "street_1",
                        "street_2", "city", "zip", "employer", "occupation", "id", "aggregate_ytd")
FEC_TOTALS = ("total_receipts", "total_disbursements", "cash_on_hand_beginning_period", "cash_on_hand_end_period",
              "total_individual_contributions", "debts_owed_by_committee", "debts_owed_to_committee",
              "net_contributions", "net_operating_expenditures")


class CampaignFinanceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "none"


def _day(value: Any) -> str | None:
    """An ISO day from an ISO date/time or the Commission's ``dd/mm/yyyy``."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        try:
            return datetime(int(match[3]), int(match[2]), int(match[1])).date().isoformat()
        except ValueError:
            return None
    return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignFinanceFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


def _results(payload: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise CampaignFinanceFormatError("schema_drift", f"{what} response has no results list")
    if not all(isinstance(r, Mapping) for r in payload["results"]):
        raise CampaignFinanceFormatError("schema_drift", f"{what} results are not objects")
    return [dict(r) for r in payload["results"]]


def committee_key(committee_id: Any) -> str:
    return f"campaign-finance:fec:committee:{str(committee_id).strip().upper()}"


def candidate_key(candidate_id: Any) -> str:
    return f"campaign-finance:fec:candidate:{str(candidate_id).strip().upper()}"


def fec_filing_key(file_number: Any) -> str:
    return f"campaign-finance:fec:filing:{int(file_number)}"


def fec_filing_group(file_number: Any) -> str:
    return f"campaign-finance:fec:filing-group:{int(file_number)}"


def ukec_entity_key(entity_id: Any) -> str:
    return f"campaign-finance:ukec:entity:{str(entity_id).strip()}"


def ukec_filing_key(kind: str, entity_id: Any, label: Any) -> str:
    return f"campaign-finance:ukec:filing:{kind}:{str(entity_id).strip()}:{slug(label)}"


def _order(*parts: Any) -> str:
    """A sortable revision-order string: integers zero-padded, other parts verbatim."""
    out = []
    for part in parts:
        if isinstance(part, int) or (isinstance(part, str) and part.isdigit()):
            out.append(f"{int(part):012d}")
        else:
            out.append(str(part or ""))
    return "|".join(out)


def _record(fmt: str, kind: str, record_key: str, *, title: Any, locator: str, fields: Mapping[str, Any],
            native_revision: Any = None, revision_order: str = "", effective_on: Any = None,
            committee: str | None = None, filing: str | None = None, filing_group: str | None = None,
            minimisation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if kind not in RECORD_KINDS:
        raise CampaignFinanceFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://"):
        raise CampaignFinanceFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "jurisdiction": spec["jurisdiction"],
        "record_kind": kind,
        "record_key": record_key,
        "committee_key": committee,
        "filing_key": filing,
        "filing_group": filing_group,
        "native_revision": _clean(native_revision),
        "revision_order": revision_order,
        "effective_on": _day(effective_on) if effective_on else None,
        "title": _clean(title) or record_key,
        "locator": locator,
        "minimisation": dict(minimisation or {"policy": MINIMISATION_POLICY, "counterparty": None, "withheld": []}),
        "fields": dict(fields),
    }


# ------------------------------------------------------------------ minimisation


def fec_counterparty(row: Mapping[str, Any], prefix: str, *, committee_field: str | None = None
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(counterparty, minimisation) for a contributor / recipient / payee as the FEC published it."""
    entity_type = _clean(row.get("entity_type"))
    entity_type = entity_type.upper() if entity_type else None
    if entity_type in FEC_INDIVIDUAL_TYPES or row.get("is_individual") is True and entity_type is None:
        withheld = sorted(f"{prefix}_{s}" for s in _FEC_PERSON_SUFFIXES if _clean(row.get(f"{prefix}_{s}")))
        withheld += [k for k in ("memo_text",) if _clean(row.get(k))]
        return ({"kind": "natural-person", "entity_type": entity_type or "IND", "name": None},
                {"policy": MINIMISATION_POLICY, "counterparty": "natural-person", "withheld": withheld})
    committee_id = _clean(row.get(committee_field)) if committee_field else None
    counterparty = {
        "kind": "organisation", "entity_type": entity_type, "name": _clean(row.get(f"{prefix}_name")),
        "city": _clean(row.get(f"{prefix}_city")), "state": _clean(row.get(f"{prefix}_state")),
        "committee_id": committee_id.upper() if committee_id else None,
    }
    if not counterparty["name"] and not committee_id:
        raise CampaignFinanceFormatError("schema_drift", "an organisational counterparty has no name or committee id")
    return counterparty, {"policy": MINIMISATION_POLICY, "counterparty": "organisation", "withheld": []}


def is_natural_person(record: Mapping[str, Any]) -> bool:
    return (record.get("minimisation") or {}).get("counterparty") == "natural-person"


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of personal fields an individual item still carries (empty when the record honours CF01)."""
    if not is_natural_person(record):
        counterparty = (record.get("fields") or {}).get("counterparty") or {}
        return ["$.fields.counterparty.kind"] if counterparty.get("kind") == "natural-person" else []
    found = []
    counterparty = (record.get("fields") or {}).get("counterparty") or {}
    if counterparty.get("kind") != "natural-person":
        found.append("$.fields.counterparty.kind")
    if counterparty.get("name") is not None:
        found.append("$.fields.counterparty.name")
    extra = set(counterparty) - {"kind", "entity_type", "status", "name"}
    found += [f"$.fields.counterparty.{k}" for k in sorted(extra)]

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if path == "$.fields.counterparty" and key == "name":
                    continue
                bare = str(key).casefold()
                stem = re.sub(r"^(contributor|recipient|payee|donor)_", "", bare)
                if bare in INDIVIDUAL_FORBIDDEN_KEYS or stem in INDIVIDUAL_FORBIDDEN_KEYS:
                    if item not in (None, "", [], {}):
                        found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(record.get("fields") or {}, "$.fields")
    return sorted(set(found))


# ------------------------------------------------------------------ FEC parsers


def _fec_url(path: str) -> str:
    return "https://www.fec.gov" + path


def _https(value: Any) -> str | None:
    text = _clean(value)
    return text if text and text.startswith("https://") else None


def parse_committee_history(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-committee-history-json"
    committee_id = str(unit["id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "committee history")]:
        if str(row.get("committee_id") or "").upper() != committee_id:
            raise CampaignFinanceFormatError("schema_drift", "history row names another committee")
        cycle = int(row.get("cycle") or 0)
        if not cycle:
            raise CampaignFinanceFormatError("schema_drift", "committee history row has no cycle")
        fields = {
            "committee_id": committee_id, "cycle": cycle, "name": _clean(row.get("name")),
            "committee_type": _clean(row.get("committee_type")),
            "committee_type_full": _clean(row.get("committee_type_full")),
            "designation": _clean(row.get("designation")), "designation_full": _clean(row.get("designation_full")),
            "organization_type": _clean(row.get("organization_type")), "party": _clean(row.get("party")),
            "state": _clean(row.get("state")), "filing_frequency": _clean(row.get("filing_frequency")),
            "affiliated_committee_name": _clean(row.get("affiliated_committee_name")),
            "candidate_ids": sorted(str(c).upper() for c in row.get("candidate_ids") or []),
            "first_file_date": _day(row.get("first_file_date")), "last_file_date": _day(row.get("last_file_date")),
            "last_f1_date": _day(row.get("last_f1_date")),
            "withheld_as_personal": sorted(k for k in ("treasurer_name", "custodian_name_full", "street_1",
                                                       "street_2") if _clean(row.get(k))),
        }
        out.append(_record(fmt, "committee", committee_key(committee_id), title=fields["name"],
                           locator=_fec_url(f"/data/committee/{committee_id}/?cycle={cycle}"), fields=fields,
                           native_revision=f"cycle:{cycle}", revision_order=_order(cycle, fields["last_f1_date"]),
                           effective_on=fields["last_f1_date"] or f"{cycle - 1}-01-01",
                           committee=committee_key(committee_id)))
    if not out:
        raise CampaignFinanceFormatError("schema_drift", f"no registration history for committee {committee_id}")
    return out


def parse_candidate_history(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-candidate-history-json"
    candidate_id = str(unit["id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "candidate history")]:
        if str(row.get("candidate_id") or "").upper() != candidate_id:
            raise CampaignFinanceFormatError("schema_drift", "history row names another candidate")
        period = int(row.get("two_year_period") or 0)
        if not period:
            raise CampaignFinanceFormatError("schema_drift", "candidate history row has no two-year period")
        fields = {
            "candidate_id": candidate_id, "two_year_period": period, "name": _clean(row.get("name")),
            "office": _clean(row.get("office")), "office_full": _clean(row.get("office_full")),
            "state": _clean(row.get("state")), "district": _clean(row.get("district")),
            "party": _clean(row.get("party")), "party_full": _clean(row.get("party_full")),
            "candidate_status": _clean(row.get("candidate_status")),
            "incumbent_challenge": _clean(row.get("incumbent_challenge")),
            "election_years": sorted(int(y) for y in row.get("election_years") or []),
            "cycles": sorted(int(y) for y in row.get("cycles") or []),
            "load_date": _day(row.get("load_date")),
            "withheld_as_personal": sorted(k for k in ("address_street_1", "address_street_2", "address_zip")
                                           if _clean(row.get(k))),
        }
        out.append(_record(fmt, "candidate", candidate_key(candidate_id), title=fields["name"],
                           locator=_fec_url(f"/data/candidate/{candidate_id}/?cycle={period}"), fields=fields,
                           native_revision=f"period:{period}", revision_order=_order(period, fields["load_date"]),
                           effective_on=fields["load_date"] or f"{period - 1}-01-01"))
    if not out:
        raise CampaignFinanceFormatError("schema_drift", f"no registration history for candidate {candidate_id}")
    return out


def _file_number(row: Mapping[str, Any]) -> int:
    try:
        return int(row["file_number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignFinanceFormatError("schema_drift", "a filing or line item has no file_number") from exc


def parse_filings(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-filings-json"
    committee_id = str(unit["committee_id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "filings")]:
        if str(row.get("committee_id") or "").upper() != committee_id:
            raise CampaignFinanceFormatError("schema_drift", "filing names another committee")
        number = _file_number(row)
        chain = [int(n) for n in row.get("amendment_chain") or []]
        if chain and chain[-1] != number:
            raise CampaignFinanceFormatError("schema_drift", f"amendment chain of {number} does not end with it")
        most_recent_number = row.get("most_recent_file_number")
        if not isinstance(row.get("most_recent"), bool):
            raise CampaignFinanceFormatError("schema_drift", f"filing {number} does not publish its most_recent flag")
        fields = {
            "committee_id": committee_id, "file_number": number, "form_type": _clean(row.get("form_type")),
            "report_type": _clean(row.get("report_type")), "report_type_full": _clean(row.get("report_type_full")),
            "report_year": row.get("report_year"), "cycle": row.get("cycle"),
            "coverage_start_date": _day(row.get("coverage_start_date")),
            "coverage_end_date": _day(row.get("coverage_end_date")), "receipt_date": _day(row.get("receipt_date")),
            "amendment_indicator": _clean(row.get("amendment_indicator")),
            "amendment_indicator_full": _clean(row.get("amendment_indicator_full")),
            "amendment_chain": chain, "most_recent": row["most_recent"],
            "most_recent_file_number": int(most_recent_number) if most_recent_number is not None else None,
            "previous_file_number": int(row["previous_file_number"]) if row.get("previous_file_number") else None,
            "is_amended": row.get("is_amended"),
            "beginning_image_number": _clean(row.get("beginning_image_number")),
            "totals_as_reported": {k: row[k] for k in FEC_TOTALS if row.get(k) is not None},
            "document_description": _clean(row.get("document_description")),
            "pdf_url": _https(row.get("pdf_url")), "csv_url": _https(row.get("csv_url")),
        }
        if not fields["receipt_date"]:
            raise CampaignFinanceFormatError("schema_drift", f"filing {number} has no receipt date")
        locator = _https(row.get("fec_url")) or fields["pdf_url"] or \
            f"https://docquery.fec.gov/cgi-bin/forms/{committee_id}/{number}/"
        out.append(_record(fmt, "filing", fec_filing_key(number),
                           title=f"{committee_id} {fields['form_type']} {fields['report_type'] or ''} "
                                 f"{fields['report_year'] or ''} (file {number})",
                           locator=locator, fields=fields,
                           native_revision=f"most-recent-file:{fields['most_recent_file_number'] or number}",
                           revision_order=_order(fields["most_recent_file_number"] or number),
                           effective_on=fields["receipt_date"], committee=committee_key(committee_id),
                           filing=fec_filing_key(number), filing_group=fec_filing_group(chain[0] if chain else number)))
    return out


def _item_base(row: Mapping[str, Any], schedule: str) -> dict[str, Any]:
    sub_id = _clean(row.get("sub_id")) or _clean(row.get("transaction_id"))
    if not sub_id:
        raise CampaignFinanceFormatError("schema_drift", "a line item has no sub_id or transaction_id")
    memo_code = _clean(row.get("memo_code"))
    return {
        "schedule": schedule, "file_number": _file_number(row), "sub_id": sub_id,
        "transaction_id": _clean(row.get("transaction_id")), "image_number": _clean(row.get("image_number")),
        "line_number": _clean(row.get("line_number")), "filing_form": _clean(row.get("filing_form")),
        "report_type": _clean(row.get("report_type")), "report_year": row.get("report_year"),
        "amendment_indicator": _clean(row.get("amendment_indicator")),
        "memo": memo_code == "X", "memo_code": memo_code,
    }


def _item_record(fmt: str, kind: str, schedule_key: str, base: Mapping[str, Any], committee_id: str,
                 fields: Mapping[str, Any], minimisation: Mapping[str, Any], row: Mapping[str, Any],
                 effective: Any) -> dict[str, Any]:
    number = base["file_number"]
    key = f"campaign-finance:fec:{schedule_key}:{number}:{base['sub_id']}"
    locator = _https(row.get("pdf_url")) or f"https://docquery.fec.gov/cgi-bin/forms/{committee_id}/{number}/"
    counterparty = fields.get("counterparty") or {}
    title = f"Schedule {base['schedule']} line {base['line_number'] or ''} in file {number}" + (
        f" ({counterparty['name']})" if counterparty.get("name") else "")
    return _record(fmt, kind, key, title=title, locator=locator, fields={**base, **fields},
                   revision_order=_order(number), effective_on=effective, committee=committee_key(committee_id),
                   filing=fec_filing_key(number), minimisation=minimisation)


def parse_schedule_a(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-schedule-a-json"
    committee_id = str(unit["committee_id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "schedule A")]:
        if str(row.get("committee_id") or "").upper() != committee_id:
            raise CampaignFinanceFormatError("schema_drift", "receipt names another committee")
        base = _item_base(row, "A")
        counterparty, minimisation = fec_counterparty(row, "contributor", committee_field="contributor_id")
        fields = {
            "committee_id": committee_id, "two_year_transaction_period": row.get("two_year_transaction_period"),
            "amount_as_reported": row.get("contribution_receipt_amount"),
            "date_as_reported": _day(row.get("contribution_receipt_date")),
            "receipt_type": _clean(row.get("receipt_type")),
            "receipt_type_description": _clean(row.get("receipt_type_desc")),
            "election_type": _clean(row.get("election_type")), "counterparty": counterparty,
        }
        if counterparty["kind"] == "organisation":
            fields["memo_text"] = _clean(row.get("memo_text"))
            fields["contributor_aggregate_ytd"] = row.get("contributor_aggregate_ytd")
        out.append(_item_record(fmt, "contribution", "sa", base, committee_id, fields, minimisation, row,
                                fields["date_as_reported"]))
    return out


def parse_schedule_b(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-schedule-b-json"
    committee_id = str(unit["committee_id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "schedule B")]:
        if str(row.get("committee_id") or "").upper() != committee_id:
            raise CampaignFinanceFormatError("schema_drift", "disbursement names another committee")
        base = _item_base(row, "B")
        counterparty, minimisation = fec_counterparty(row, "recipient", committee_field="recipient_committee_id")
        fields = {
            "committee_id": committee_id, "two_year_transaction_period": row.get("two_year_transaction_period"),
            "amount_as_reported": row.get("disbursement_amount"),
            "date_as_reported": _day(row.get("disbursement_date")),
            "purpose_as_reported": _clean(row.get("disbursement_description")),
            "category_as_reported": _clean(row.get("disbursement_purpose_category")),
            "counterparty": counterparty,
        }
        if counterparty["kind"] == "organisation":
            fields["memo_text"] = _clean(row.get("memo_text"))
        out.append(_item_record(fmt, "expenditure", "sb", base, committee_id, fields, minimisation, row,
                                fields["date_as_reported"]))
    return out


NOTICE_FORMS = frozenset({"F24", "F5-24", "F5-48"})


def parse_schedule_e(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "openfec-schedule-e-json"
    committee_id = str(unit["committee_id"]).upper()
    out = []
    for row in [r for page in pages for r in _results(page, "schedule E")]:
        if str(row.get("committee_id") or "").upper() != committee_id:
            raise CampaignFinanceFormatError("schema_drift", "independent expenditure names another spender")
        base = _item_base(row, "E")
        indicator = row.get("support_oppose_indicator")
        if indicator in (None, ""):
            raise CampaignFinanceFormatError("schema_drift", "an independent expenditure states no support/oppose")
        counterparty, minimisation = fec_counterparty(row, "payee")
        report_type = str(row.get("report_type") or "").upper()
        notice = row.get("is_notice") is True or report_type in {"24", "48"} or base["filing_form"] in NOTICE_FORMS
        fields = {
            "committee_id": committee_id, "cycle": row.get("cycle"),
            "source_assertion": "24/48-hour notice" if notice else "periodic report",
            "is_notice_as_published": row.get("is_notice"),
            "support_oppose_indicator": indicator,
            "candidate_id": row.get("candidate_id"), "candidate_name": row.get("candidate_name"),
            "candidate_office": row.get("candidate_office"),
            "candidate_office_state": row.get("candidate_office_state"),
            "candidate_office_district": row.get("candidate_office_district"),
            "amount_as_reported": row.get("expenditure_amount"),
            "expenditure_date": _day(row.get("expenditure_date")),
            "dissemination_date": _day(row.get("dissemination_date")),
            "purpose_as_reported": _clean(row.get("expenditure_description")),
            "election_type": _clean(row.get("election_type")), "counterparty": counterparty,
        }
        effective = fields["dissemination_date"] or fields["expenditure_date"]
        out.append(_item_record(fmt, "independent-expenditure", "se", base, committee_id, fields, minimisation, row,
                                effective))
    return out


# ------------------------------------------------------------------ Electoral Commission parsers

_EC_BOOL = {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False}


def _ec_bool(value: Any) -> bool | None:
    return _EC_BOOL.get(str(value or "").strip().casefold())


def _ec_amount(value: Any) -> str | None:
    text = re.sub(r"[£,\s]", "", str(value or ""))
    if not text:
        return None
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise CampaignFinanceFormatError("schema_drift", f"not a published amount: {value!r}")
    return f"{float(text):.2f}"


def _csv_rows(raw: bytes, required: Sequence[str]) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CampaignFinanceFormatError("schema_drift", "export is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise CampaignFinanceFormatError("schema_drift", f"export lacks columns {missing}")
    rows = [dict(r) for r in reader]
    if len(rows) >= UK_ROW_CAP:
        raise CampaignFinanceFormatError("input_limit", "export reached the row cap; the unit is not truncated")
    return rows


_EC_DONATION_COLUMNS = ("ECRef", "RegulatedEntityId", "RegulatedEntityName", "RegulatedEntityType", "Value",
                        "AcceptedDate", "DonorName", "DonorStatus", "ReportingPeriodName")
_EC_SPENDING_COLUMNS = ("ECRef", "RegulatedEntityId", "RegulatedEntityName", "RegulatedEntityType",
                        "ElectionName", "ExpenseCategoryName", "SupplierName", "Amount", "DateIncurred")
_EC_BASE = "https://search.electoralcommission.org.uk/English"


def _ec_entity(fmt: str, row: Mapping[str, str]) -> dict[str, Any]:
    entity_id = _clean(row.get("RegulatedEntityId"))
    fields = {"regulated_entity_id": entity_id, "name": _clean(row.get("RegulatedEntityName")),
              "regulated_entity_type": _clean(row.get("RegulatedEntityType")),
              "register": _clean(row.get("RegisterName"))}
    return _record(fmt, "regulated-entity", ukec_entity_key(entity_id), title=fields["name"],
                   locator=f"{_EC_BASE}/Registrations/{entity_id}", fields=fields,
                   committee=ukec_entity_key(entity_id))


def _ec_split(rows: list[dict[str, str]], unit: Mapping[str, Any]) -> tuple[list[dict[str, str]], int]:
    wanted = str(unit["id"]).strip()
    kept = [r for r in rows if str(r.get("RegulatedEntityId") or "").strip() == wanted]
    refs = [r["ECRef"] for r in kept]
    if any(not _clean(ref) for ref in refs) or len(set(refs)) != len(refs):
        raise CampaignFinanceFormatError("schema_drift", "every item has one unique ECRef")
    return kept, len(rows) - len(kept)


def parse_ec_donations(raw: bytes, unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "ukec-donations-csv"
    rows, _ = _ec_split(_csv_rows(raw, _EC_DONATION_COLUMNS), unit)
    if not rows:
        return []
    entity = _ec_entity(fmt, rows[0])
    entity_id = entity["fields"]["regulated_entity_id"]
    periods: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        periods.setdefault(_clean(row.get("ReportingPeriodName")) or "unstated period", []).append(row)
    out = [entity]
    for period, members in sorted(periods.items()):
        key = ukec_filing_key("donations", entity_id, period)
        out.append(_record(fmt, "filing", key, title=f"{entity['fields']['name']} donations: {period}",
                           locator=f"{_EC_BASE}/Search/Donations?regulatedEntityId={entity_id}",
                           fields={"return_kind": "donations", "regulated_entity_id": entity_id,
                                   "reporting_period": period, "register": entity["fields"]["register"],
                                   "items_in_export": sorted(r["ECRef"] for r in members),
                                   "totals_as_reported": None,
                                   "totals_note": "the Commission's search export publishes items, not return "
                                                  "totals; no total is reported"},
                           committee=entity["record_key"], filing=key, filing_group=key))
        for row in members:
            status = _clean(row.get("DonorStatus"))
            individual = str(status or "").casefold() in UK_INDIVIDUAL_STATUSES
            if individual:
                withheld = sorted(k for k in ("DonorName", "Postcode", "DonorId", "PurposeOfVisit")
                                  if _clean(row.get(k)))
                counterparty = {"kind": "natural-person", "status": status, "name": None}
                minimisation = {"policy": MINIMISATION_POLICY, "counterparty": "natural-person", "withheld": withheld}
            else:
                counterparty = {"kind": "organisation", "status": status, "name": _clean(row.get("DonorName")),
                                "company_registration_number": _clean(row.get("CompanyRegistrationNumber")),
                                "postcode": _clean(row.get("Postcode")), "donor_id": _clean(row.get("DonorId"))}
                if not counterparty["name"]:
                    raise CampaignFinanceFormatError("schema_drift", f"{row['ECRef']}: donor has no name")
                minimisation = {"policy": MINIMISATION_POLICY, "counterparty": "organisation", "withheld": []}
            fields = {
                "schedule": "donation", "ec_ref": row["ECRef"], "regulated_entity_id": entity_id,
                "amount_as_reported": _ec_amount(row.get("Value")), "currency": "GBP",
                "value_as_published": _clean(row.get("Value")), "accepted_date": _day(row.get("AcceptedDate")),
                "received_date": _day(row.get("ReceivedDate")), "reported_date": _day(row.get("ReportedDate")),
                "reporting_period": period, "donation_type": _clean(row.get("DonationType")),
                "nature_of_donation": _clean(row.get("NatureOfDonation")),
                "is_sponsorship": _ec_bool(row.get("IsSponsorship")), "is_bequest": _ec_bool(row.get("IsBequest")),
                "is_aggregation": _ec_bool(row.get("IsAggregation")),
                "is_reported_pre_poll": _ec_bool(row.get("IsReportedPrePoll")),
                "donation_action": _clean(row.get("DonationAction")),
                "accounting_unit_name": _clean(row.get("AccountingUnitName")), "memo": False,
                "counterparty": counterparty,
            }
            if not individual:
                fields["purpose_of_visit"] = _clean(row.get("PurposeOfVisit"))
            title = f"Donation {row['ECRef']} to {entity['fields']['name']}" + (
                f" from {counterparty['name']}" if counterparty.get("name") else "")
            out.append(_record(fmt, "contribution", f"campaign-finance:ukec:donation:{row['ECRef']}", title=title,
                               locator=f"{_EC_BASE}/Donations/{row['ECRef']}", fields=fields,
                               effective_on=fields["accepted_date"], committee=entity["record_key"], filing=key,
                               minimisation=minimisation))
    return out


def parse_ec_spending(raw: bytes, unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "ukec-spending-csv"
    rows, _ = _ec_split(_csv_rows(raw, _EC_SPENDING_COLUMNS), unit)
    if not rows:
        return []
    entity = _ec_entity(fmt, rows[0])
    entity_id = entity["fields"]["regulated_entity_id"]
    elections: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        elections.setdefault(_clean(row.get("ElectionName")) or "unstated election", []).append(row)
    out = [entity]
    for election, members in sorted(elections.items()):
        key = ukec_filing_key("spending", entity_id, election)
        out.append(_record(fmt, "filing", key, title=f"{entity['fields']['name']} spending return: {election}",
                           locator=f"{_EC_BASE}/Search/Spending?regulatedEntityId={entity_id}",
                           fields={"return_kind": "spending", "regulated_entity_id": entity_id,
                                   "election_name": election, "register": entity["fields"]["register"],
                                   "items_in_export": sorted(r["ECRef"] for r in members),
                                   "totals_as_reported": None,
                                   "totals_note": "the Commission's search export publishes items, not return "
                                                  "totals; no total is reported"},
                           committee=entity["record_key"], filing=key, filing_group=key))
        for row in members:
            fields = {
                "schedule": "spending", "ec_ref": row["ECRef"], "regulated_entity_id": entity_id,
                "election_name": election, "amount_as_reported": _ec_amount(row.get("Amount")), "currency": "GBP",
                "value_as_published": _clean(row.get("Amount")), "date_incurred": _day(row.get("DateIncurred")),
                "date_paid": _day(row.get("DatePaid")), "category_as_reported": _clean(row.get("ExpenseCategoryName")),
                "memo": False,
                "counterparty": {"kind": "organisation", "status": None, "name": _clean(row.get("SupplierName")),
                                 "note": "supplier status is not published; kept as published"},
            }
            out.append(_record(fmt, "expenditure", f"campaign-finance:ukec:spending:{row['ECRef']}",
                               title=f"Spending {row['ECRef']} by {entity['fields']['name']}",
                               locator=f"{_EC_BASE}/Spending/{row['ECRef']}", fields=fields,
                               effective_on=fields["date_incurred"], committee=entity["record_key"], filing=key,
                               minimisation={"policy": MINIMISATION_POLICY, "counterparty": "organisation",
                                             "withheld": []}))
    return out


# ------------------------------------------------------------------ units and requests

_FEC_ID = re.compile(r"^[CHSP][0-9A-Z]{8}$")


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a campaign-finance selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if key in {"committees", "candidates"} and not _FEC_ID.fullmatch(str(unit.get("id") or "").upper()):
            raise SourcePackError("invalid_manifest", f"not an FEC id: {unit.get('id')!r}")
        if key == "committee_cycles":
            if not _FEC_ID.fullmatch(str(unit.get("committee_id") or "").upper()):
                raise SourcePackError("invalid_manifest", "a committee cycle names an FEC committee id")
            cycle = int(unit.get("cycle") or 0)
            if cycle % 2 or cycle < 1980:
                raise SourcePackError("invalid_manifest", "a committee cycle names an even two-year period")
        if key == "regulated_entities":
            start, end = _day(unit.get("from")), _day(unit.get("to"))
            if not re.fullmatch(r"\d{1,8}", str(unit.get("id") or "")) or not start or not end or end < start:
                raise SourcePackError("invalid_manifest", "a regulated entity names its id and a date window")
            if (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days > 366:
                raise SourcePackError("invalid_manifest", "a Commission date window is at most one year")
    return units


# format -> (path builder, base params, keyset sort field or None for offset paging)
def requests_for(fmt: str, unit: Mapping[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """The request path (relative to the endpoint), its parameters and the paging style for one unit."""
    if fmt == "openfec-committee-history-json":
        return f"/committee/{str(unit['id']).upper()}/history/", {"per_page": PER_PAGE, "sort": "-cycle"}, None
    if fmt == "openfec-candidate-history-json":
        return (f"/candidate/{str(unit['id']).upper()}/history/",
                {"per_page": PER_PAGE, "sort": "-two_year_period"}, None)
    committee_id = str(unit.get("committee_id") or "").upper()
    cycle = int(unit.get("cycle") or 0)
    if fmt == "openfec-filings-json":
        return f"/committee/{committee_id}/filings/", {"cycle": cycle, "per_page": PER_PAGE,
                                                       "sort": "receipt_date"}, None
    if fmt == "openfec-schedule-a-json":
        return "/schedules/schedule_a/", {"committee_id": committee_id, "two_year_transaction_period": cycle,
                                          "per_page": PER_PAGE, "sort": "contribution_receipt_date"}, \
            "last_contribution_receipt_date"
    if fmt == "openfec-schedule-b-json":
        return "/schedules/schedule_b/", {"committee_id": committee_id, "two_year_transaction_period": cycle,
                                          "per_page": PER_PAGE, "sort": "disbursement_date"}, "last_disbursement_date"
    if fmt == "openfec-schedule-e-json":
        return "/schedules/schedule_e/", {"committee_id": committee_id, "cycle": cycle, "per_page": PER_PAGE,
                                          "sort": "expenditure_date"}, "last_expenditure_date"
    if fmt in {"ukec-donations-csv", "ukec-spending-csv"}:
        path = "/api/csv/Donations" if fmt == "ukec-donations-csv" else "/api/csv/Spending"
        return path, {"regulatedEntityId": int(unit["id"]), "from": _day(unit["from"]), "to": _day(unit["to"]),
                      "rows": UK_ROW_CAP, "start": 0,
                      "sort": "AcceptedDate" if fmt == "ukec-donations-csv" else "DateIncurred",
                      "order": "asc"}, "csv"
    raise SourcePackError("invalid_manifest", f"unknown campaign-finance format {fmt!r}")


_JSON_PARSERS: dict[str, Callable[[Sequence[Any], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "openfec-committee-history-json": parse_committee_history,
    "openfec-candidate-history-json": parse_candidate_history,
    "openfec-filings-json": parse_filings,
    "openfec-schedule-a-json": parse_schedule_a,
    "openfec-schedule-b-json": parse_schedule_b,
    "openfec-schedule-e-json": parse_schedule_e,
}


def parse_unit(fmt: str, responses: Sequence[bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse the responses of one unit; every emitted record honours the minimisation decision."""
    if fmt == "ukec-donations-csv":
        records = parse_ec_donations(responses[0], unit)
    elif fmt == "ukec-spending-csv":
        records = parse_ec_spending(responses[0], unit)
    elif fmt in _JSON_PARSERS:
        records = _JSON_PARSERS[fmt]([_json(raw) for raw in responses], unit)
    else:
        raise CampaignFinanceFormatError("schema_drift", f"unknown campaign-finance format {fmt!r}")
    for record in records:
        if minimisation_violations(record):
            raise CampaignFinanceFormatError("minimisation_violation", f"{record['record_key']} carries personal "
                                                                       "fields of an individual")
    return records


def campaign_finance_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("campaign_finance") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "campaign-finance sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "campaign-finance sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "campaign-finance sources declare the CF01 minimisation policy")
    _units(fmt, dict(declared.get("selection") or {}))
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed campaign-finance formats declare a required secret")
    return declared


class CampaignFinanceAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = campaign_finance_declaration(self.source)
        self.format = self.declared["format"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "campaign_finance": {"provider": self.declared["provider"], "format": self.format,
                                 "units": len(self.units), "keyed": bool(FORMATS[self.format]["keyed"]),
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
            raise SourcePackError("parameter_forbidden", "campaign-finance runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, text/csv"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "this campaign-finance source needs its API key")
            headers["X-Api-Key"] = self.secret
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "campaign-finance response was served from another host")
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
        query = urlencode(sorted(params.items()))
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def _collect(self, unit: Mapping[str, Any]) -> tuple[list[bytes], list[dict[str, Any]]]:
        """All pages of one unit (all-or-nothing within MAX_PAGES_PER_UNIT)."""
        path, params, paging = requests_for(self.format, unit)
        if paging == "csv":
            raw, receipt = self._get(path, params)
            return [raw], [receipt]
        pages, receipts, seen = [], [], 0
        request = {**params, "page": 1} if paging is None else dict(params)
        while True:
            raw, receipt = self._get(path, request)
            pages.append(raw)
            receipts.append(receipt)
            try:
                payload = _json(raw)
                items = _results(payload, self.format)
            except CampaignFinanceFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
            seen += len(items)
            pagination = dict(payload.get("pagination") or {})
            count = pagination.get("count")
            if paging is None:
                more = int(pagination.get("page") or request["page"]) < int(pagination.get("pages") or 1)
                following = {**request, "page": int(request["page"]) + 1}
            else:
                last = dict(pagination.get("last_indexes") or {})
                more = bool(last) and len(items) >= PER_PAGE and (count is None or seen < int(count))
                following = {**params, **{k: v for k, v in last.items() if k in {"last_index", paging}}}
            if not more:
                if count is not None and int(count) > seen:
                    raise SourcePackError("budget_exhausted", "the provider states more rows than it returned")
                return pages, receipts
            if len(pages) >= MAX_PAGES_PER_UNIT:
                raise SourcePackError("budget_exhausted", "unit is longer than its page bound; never truncated")
            request = following

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests = self._collect(unit)
        try:
            records = parse_unit(self.format, responses, unit)
        except CampaignFinanceFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": "noesis-campaign-finance-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.declared["provider"], "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "minimisation": MINIMISATION_POLICY,
            "individual_items_minimised": sum(1 for r in records if is_natural_person(r)),
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"], "language": "en",
                "published_at": record["effective_on"], "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "campaign_finance_record": record, "campaign_finance_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: CampaignFinanceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
        key = parts.path + ("?" + query if query else "")
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
    adapter = CampaignFinanceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                     secret=FIXTURE_SECRET)
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
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "PROVIDER_CONTRACTS",
    "RECORD_CONTRACT",
    "REVIEW_BOUNDARY",
    "CampaignFinanceAdapter",
    "CampaignFinanceFormatError",
    "campaign_finance_declaration",
    "fixture_transport",
    "minimisation_violations",
    "parse_unit",
    "replay_native_fixture",
    "requests_for",
]

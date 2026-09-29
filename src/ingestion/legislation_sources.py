"""US Congress and UK Parliament legislation acquisition for the Political pack (#2208, LT01, LT03-LT06).

One native connector, ``legislation``, reads a bounded, declared selection
from one documented provider per source and emits ``noesis-legislation-record-v1``
records exactly as the provider published them:

* ``congress-gov-bill-json`` - congress.gov API v3: a bill with its actions
  (date, code and text verbatim, with the recorded votes the action names),
  sponsor, cosponsors (join and withdrawal dates), public-law citations and
  CRS summaries (labelled as CRS summaries, never as the bill's legal effect);
* ``congress-gov-house-vote-json`` - congress.gov House roll-call votes with
  every member's position keyed by bioguide ID as published;
* ``senate-lis-vote-xml`` - Senate roll-call vote XML from senate.gov (LIS
  member ids as published; congress.gov publishes no Senate votes);
* ``govinfo-bills-package-json`` - GovInfo BILLS package summaries: version
  code, issue date and a content hash of the official text, which is
  referenced, never stored or re-summarised;
* ``govinfo-billstatus-xml`` - GovInfo BILLSTATUS bulk XML, kept as its own
  source assertion beside congress.gov (disagreements are shown, never
  reconciled);
* ``uk-bills-json`` - UK Parliament Bills API: a bill with sponsors, stages
  with sittings and publications (Royal Assent only when published);
* ``uk-commons-division-json`` / ``uk-lords-division-json`` - Commons and
  Lords divisions with ayes/noes (contents/not contents) and tellers per
  member id;
* ``uk-hansard-debate-json`` - Hansard debate sections as references with
  locators and contribution ids; transcripts are never mirrored.

A division or debate is linked to a bill only by the source's own reference;
the bill an operator declared it for is recorded as an unlinked *candidate*
that a reviewer accepts or rejects. Nothing here predicts passage, scores
members or rates ideology.

Every page is one selection unit (a bill, vote, package, division or
debate). A unit is all-or-nothing: an actions or cosponsors list longer than
the declared page size is ``budget_exhausted``, never truncated, and a
redirect to another host is a network-policy failure. Receipts name every
request path, status and response digest; API keys travel in a header and
never appear in a receipt or a record.

``PROVIDER_CONTRACTS`` records the LT01 access decisions and
``LIVE_VERIFICATION`` their live status (``unverified-live`` until a dated
live run is recorded under ``docs/development/legislation-evidence/``).
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.domains.political.legislation_mapping import (
    RECORD_CONTRACT,
    REVIEW_BOUNDARY,
    US_VERSION_CODES,
    LegislationMappingError,
    member_key,
    uk_bill_key,
    us_bill_key,
)
from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "legislation"
PAGE_SIZE = 250
# format -> provider, jurisdiction, the selection list it reads and whether a secret (API key) is required
FORMATS: dict[str, dict[str, Any]] = {
    "congress-gov-bill-json": {"provider": "congress-gov", "jurisdiction": "US", "unit": "bills", "keyed": True},
    "congress-gov-house-vote-json": {"provider": "congress-gov", "jurisdiction": "US", "unit": "votes",
                                     "keyed": True},
    "senate-lis-vote-xml": {"provider": "senate-lis", "jurisdiction": "US", "unit": "votes", "keyed": False},
    "govinfo-bills-package-json": {"provider": "govinfo", "jurisdiction": "US", "unit": "packages", "keyed": True},
    "govinfo-billstatus-xml": {"provider": "govinfo", "jurisdiction": "US", "unit": "bills", "keyed": False},
    "uk-bills-json": {"provider": "uk-bills", "jurisdiction": "GB", "unit": "bills", "keyed": False},
    "uk-commons-division-json": {"provider": "uk-commons-votes", "jurisdiction": "GB", "unit": "divisions",
                                 "keyed": False},
    "uk-lords-division-json": {"provider": "uk-lords-votes", "jurisdiction": "GB", "unit": "divisions",
                               "keyed": False},
    "uk-hansard-debate-json": {"provider": "uk-hansard", "jurisdiction": "GB", "unit": "debates", "keyed": False},
}
MAX_UNITS = 50
OGL_ATTRIBUTION = "Contains Parliamentary information licensed under the Open Parliament Licence v3.0."
US_ATTRIBUTION = "US federal government work (17 U.S.C. 105): public domain; source credited, no endorsement implied."

# LT01 access decisions. Endpoints, fields and terms are recorded from the providers' published documentation as
# known without network access; every item marked ``verify`` must be checked against the live documentation, terms
# and a real response before a dated live run is accepted (LT13, #2458).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "congress-gov": {
        "publisher": "Library of Congress (congress.gov API v3)",
        "endpoints": ["/v3/bill/{congress}/{type}/{number}", "/v3/bill/{congress}/{type}/{number}/actions",
                      "/v3/bill/{congress}/{type}/{number}/cosponsors",
                      "/v3/bill/{congress}/{type}/{number}/summaries",
                      "/v3/house-vote/{congress}/{session}/{roll}",
                      "/v3/house-vote/{congress}/{session}/{roll}/members"],
        "formats": ["congress-gov-bill-json", "congress-gov-house-vote-json"],
        "authentication": "api.data.gov key (required-secret NOESIS_CONGRESS_GOV_API_KEY) sent as the X-Api-Key "
        "header, never in a URL or receipt",
        "rate_limits": "5,000 requests per hour per key (verify); one bounded selection per run",
        "pagination": "offset/limit with limit <= 250; a list longer than one page is budget_exhausted, never "
        "truncated",
        "identifiers": ["congress + bill type + bill number", "action code", "bioguide ID",
                        "chamber + congress + session + roll-call number", "public law number"],
        "revisions": "bill updateDate (and updateDateIncludingText); a changed updateDate or content is a new "
        "document revision; actions carry actionDate, actionCode and text verbatim",
        "licence": "US government work, public domain in the US; attribution requested (verify the API terms)",
        "attribution": US_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parsers in the documented v3 JSON shape; the House vote endpoints are "
        "documented as beta and their field names (bioguideID, voteCast, legislationType) must be verified",
    },
    "senate-lis": {
        "publisher": "United States Senate (Legislative Information System roll-call vote XML)",
        "endpoints": ["/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml"],
        "formats": ["senate-lis-vote-xml"],
        "authentication": "none",
        "rate_limits": "undocumented; one request per declared vote",
        "pagination": "none (one file per vote)",
        "identifiers": ["congress + session + vote number", "LIS member id (not a bioguide ID)",
                        "the measure the vote names (document type and number)"],
        "revisions": "modify_date in the file; a changed file is a new document revision",
        "licence": "US government work, public domain (verify the senate.gov notice)",
        "attribution": US_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "congress.gov publishes no Senate roll calls; fixture-verified parser for the documented XML "
        "shape; element names and the URL pattern must be verified",
    },
    "govinfo": {
        "publisher": "US Government Publishing Office (GovInfo API and bulk data)",
        "endpoints": ["https://api.govinfo.gov/packages/{packageId}/summary",
                      "https://api.govinfo.gov/packages/{packageId}/xml",
                      "https://www.govinfo.gov/bulkdata/BILLSTATUS/{congress}/{type}/BILLSTATUS-{congress}{type}{number}.xml"],
        "formats": ["govinfo-bills-package-json", "govinfo-billstatus-xml"],
        "authentication": "api.data.gov key (required-secret NOESIS_GOVINFO_API_KEY, X-Api-Key header) for the "
        "API; bulk data needs none",
        "rate_limits": "api.data.gov default 1,000 requests per hour per key (verify)",
        "pagination": "none per package; one BILLSTATUS file per bill",
        "identifiers": ["GovInfo package id BILLS-{congress}{type}{number}{version}", "version code",
                        "BILLSTATUS bill identity"],
        "revisions": "package lastModified; BILLSTATUS updateDate; each text version is its own package",
        "licence": "US government work, public domain",
        "attribution": US_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified; the summary field names (billVersion, dateIssued, download.xmlLink) and the "
        "BILLSTATUS element names must be verified against a live response",
    },
    "uk-bills": {
        "publisher": "UK Parliament (Bills API)",
        "endpoints": ["https://bills-api.parliament.uk/api/v1/Bills/{billId}",
                      "https://bills-api.parliament.uk/api/v1/Bills/{billId}/Stages",
                      "https://bills-api.parliament.uk/api/v1/Bills/{billId}/Publications"],
        "formats": ["uk-bills-json"],
        "authentication": "none",
        "rate_limits": "undocumented (verify); one bounded selection per run",
        "pagination": "Stages take Skip/Take (Take <= 250); a longer list is budget_exhausted",
        "identifiers": ["bill id (+ introduced and included session ids)", "bill stage id", "stage type id",
                        "sitting id", "publication id", "member id"],
        "revisions": "bill lastUpdate; stage sittings and publications change as Parliament records them; each "
        "change is a new document revision",
        "licence": "Open Parliament Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser in the documented JSON shape; field casing must be verified",
    },
    "uk-commons-votes": {
        "publisher": "UK Parliament (Commons Votes API)",
        "endpoints": ["https://commonsvotes-api.parliament.uk/data/division/{divisionId}.json"],
        "formats": ["uk-commons-division-json"],
        "authentication": "none",
        "rate_limits": "undocumented (verify)",
        "pagination": "none per division",
        "identifiers": ["division id", "division number", "member id"],
        "revisions": "PublicationUpdated; a corrected division list is a new document revision",
        "licence": "Open Parliament Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified; the division payload states no bill, so the bill is an unlinked candidate "
        "until reviewed (verify whether a bill reference field exists)",
    },
    "uk-lords-votes": {
        "publisher": "UK Parliament (Lords Votes API)",
        "endpoints": ["https://lordsvotes-api.parliament.uk/data/Divisions/{divisionId}"],
        "formats": ["uk-lords-division-json"],
        "authentication": "none",
        "rate_limits": "undocumented (verify)",
        "pagination": "none per division",
        "identifiers": ["division id", "division number", "member id"],
        "revisions": "a corrected division list is a new document revision",
        "licence": "Open Parliament Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified; as for the Commons, no bill reference is relied on (verify)",
    },
    "uk-hansard": {
        "publisher": "UK Parliament (Hansard API)",
        "endpoints": ["https://hansard-api.parliament.uk/debates/debate/{debateSectionExtId}.json"],
        "formats": ["uk-hansard-debate-json"],
        "authentication": "none",
        "rate_limits": "undocumented (verify)",
        "pagination": "none per debate section",
        "identifiers": ["debate section external id", "contribution external id", "member id"],
        "revisions": "a corrected or re-published section is a new document revision (references only)",
        "licence": "Open Parliament Licence v3.0",
        "attribution": OGL_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified; contributions are stored as references with locators, never as text; the "
        "section payload states no bill id, so debates are candidates until reviewed (verify)",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (LT01): nothing implies complete coverage of a Congress or a session.
BOUNDED_COVERAGE = {
    "us": "the bills (congress, type, number), roll calls (chamber, congress, session, number) and GovInfo packages "
    "named in each source's selection; at most max_pages units per run and 250 actions or cosponsors per bill",
    "gb": "the Bills API bill ids named in the selection with their stages and publications, and the divisions and "
    "Hansard sections declared for them; at most max_pages units per run",
}


class LegislationFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def _day(value: Any) -> str | None:
    """An ISO day from an ISO date/time, or ``Month D, YYYY[, hh:mm AM]`` as senate.gov writes it."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    match = re.match(r"^([A-Za-z]+) (\d{1,2}), (\d{4})", text)
    if match:
        try:
            return datetime.strptime(f"{match[1]} {match[2]} {match[3]}", "%B %d %Y").date().isoformat()
        except ValueError:
            return None
    return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegislationFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


def _xml(raw: bytes) -> ET.Element:
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw:
        raise LegislationFormatError("schema_drift", "XML with a document type declaration is refused")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise LegislationFormatError("schema_drift", "response is not well-formed XML") from exc


def _x(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    return _clean(found.text) if found is not None and found.text is not None else None


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LegislationFormatError("schema_drift", f"{what} is not an object")
    return dict(value)


def _list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LegislationFormatError("schema_drift", f"{what} is not a list")
    return value


def _complete(payload: Mapping[str, Any], items: list[Any], what: str) -> None:
    """A list page must be the whole list: a stated larger count or a next link means it would be truncated."""
    pagination = dict(payload.get("pagination") or {})
    count = pagination.get("count", payload.get("totalResults"))
    if pagination.get("next") or (count is not None and int(count) > len(items)):
        raise LegislationFormatError("input_limit", f"{what} is longer than one page; the unit is not truncated")


def _record(fmt: str, kind: str, record_key: str, *, bill_key: str | None, link: Mapping[str, Any],
            native_revision: Any, title: Any, locator: str, published_at: Any, fields: Mapping[str, Any]
            ) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if not str(locator or "").startswith("https://"):
        raise LegislationFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "jurisdiction": spec["jurisdiction"],
        "record_kind": kind,
        "record_key": record_key,
        "bill_key": bill_key,
        "bill_link": dict(link),
        "native_revision": _clean(native_revision),
        "title": _clean(title) or record_key,
        "locator": locator,
        "published_at": _day(published_at) if published_at else None,
        "fields": dict(fields),
    }


# ------------------------------------------------------------------ members


def _us_member(item: Mapping[str, Any], *, bioguide_field: str = "bioguideId") -> dict[str, Any]:
    bioguide = _clean(item.get(bioguide_field))
    if not bioguide:
        raise LegislationFormatError("schema_drift", "a member lacks the bioguide ID the provider publishes")
    first, last = _clean(item.get("firstName")), _clean(item.get("lastName"))
    return {
        "member_key": member_key("us-bioguide", bioguide),
        "scheme": "us-bioguide",
        "member_id": bioguide,
        "name_as_published": _clean(item.get("fullName")) or " ".join(x for x in (first, last) if x),
        "first_name": first,
        "last_name": last,
        "party": _clean(item.get("party") or item.get("voteParty")),
        "state": _clean(item.get("state") or item.get("voteState")),
        "district": _clean(item.get("district")),
    }


def _uk_member(item: Mapping[str, Any], *, house: str | None = None) -> dict[str, Any]:
    member_id = item.get("memberId", item.get("MemberId"))
    if member_id in (None, ""):
        raise LegislationFormatError("schema_drift", "a member lacks the Parliament member id")
    return {
        "member_key": member_key("uk-parliament", member_id),
        "scheme": "uk-parliament",
        "member_id": str(member_id),
        "name_as_published": _clean(item.get("name") or item.get("Name")),
        "party": _clean(item.get("party") or item.get("Party")),
        "constituency": _clean(item.get("memberFrom") or item.get("MemberFrom")),
        "house": _clean(item.get("house") or house),
    }


# ------------------------------------------------------------------ congress.gov


def _us_bill_key(congress: Any, bill_type: Any, number: Any) -> str:
    try:
        return us_bill_key(congress, bill_type, number)
    except LegislationMappingError as exc:
        raise LegislationFormatError("schema_drift", str(exc)) from exc


def _actions(items: list[Any]) -> list[dict[str, Any]]:
    """Actions oldest first. Providers list them newest first (verify), so same-day actions keep the reverse of the
    provider's order; nothing is reordered by code or text."""
    actions = []
    for item in reversed(items):
        item = _mapping(item, "action")
        day = _day(item.get("actionDate"))
        if not day or not _clean(item.get("text")):
            raise LegislationFormatError("schema_drift", "an action lacks its date or text")
        votes = []
        for vote in _list(item.get("recordedVotes"), "recorded votes"):
            vote = _mapping(vote, "recorded vote")
            votes.append({"chamber": _clean(vote.get("chamber")), "congress": vote.get("congress"),
                          "session": vote.get("sessionNumber"), "roll_number": vote.get("rollNumber"),
                          "date": _day(vote.get("date")), "url": _clean(vote.get("url"))})
        source_system = item.get("sourceSystem") or {}
        actions.append({
            "action_date": day,
            "action_time": _clean(item.get("actionTime")),
            "action_code": _clean(item.get("actionCode")),
            "text": str(item["text"]).strip(),
            "type": _clean(item.get("type")),
            "source_system": _clean(source_system.get("name") if isinstance(source_system, Mapping) else None),
            "recorded_votes": votes,
        })
    return sorted(actions, key=lambda a: (a["action_date"], a["action_time"] or ""))  # stable within a day


def _cosponsors(items: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        item = _mapping(item, "cosponsor")
        joined = _day(item.get("sponsorshipDate"))
        if not joined:
            raise LegislationFormatError("schema_drift", "a cosponsor lacks its sponsorship date")
        rows.append({**_us_member(item), "sponsorship_date": joined,
                     "withdrawn_date": _day(item.get("sponsorshipWithdrawnDate")),
                     "original_cosponsor": bool(item.get("isOriginalCosponsor"))})
    return sorted(rows, key=lambda r: (r["sponsorship_date"], r["member_id"]))


def _laws(items: list[Any]) -> list[dict[str, Any]]:
    laws = []
    for item in items:
        item = _mapping(item, "law")
        number = _clean(item.get("number"))
        if number:
            laws.append({"type": _clean(item.get("type")) or "Public Law", "number": number,
                         "citation": f"Pub. L. {number}" if "public" in str(item.get("type") or "public").lower()
                         else f"Pvt. L. {number}"})
    return laws


def parse_congress_bill(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    bill = _mapping(_json(responses["bill"]).get("bill"), "bill")
    key = _us_bill_key(bill.get("congress"), bill.get("type"), bill.get("number"))
    actions_payload = _mapping(_json(responses["actions"]), "actions")
    action_items = _list(actions_payload.get("actions"), "actions")
    _complete(actions_payload, action_items, "actions")
    cosponsor_payload = _mapping(_json(responses["cosponsors"]), "cosponsors")
    cosponsor_items = _list(cosponsor_payload.get("cosponsors"), "cosponsors")
    _complete(cosponsor_payload, cosponsor_items, "cosponsors")
    summaries = []
    for item in _list(_mapping(_json(responses["summaries"]), "summaries").get("summaries"), "summaries"):
        item = _mapping(item, "summary")
        summaries.append({
            "label": "CRS summary",
            "notice": "a Congressional Research Service summary; it is not the bill's text or its legal effect",
            "version_code": _clean(item.get("versionCode")),
            "action_date": _day(item.get("actionDate")),
            "action_desc": _clean(item.get("actionDesc")),
            "update_date": _clean(item.get("updateDate")),
            "text": str(item.get("text") or ""),
        })
    congress, bill_type, number = int(bill["congress"]), str(bill["type"]).lower(), int(bill["number"])
    sponsors = [{**_us_member(_mapping(s, "sponsor")), "role": "sponsor"}
                for s in _list(bill.get("sponsors"), "sponsors")]
    if not sponsors:
        raise LegislationFormatError("schema_drift", "a bill states no sponsor")
    return [_record(
        "congress-gov-bill-json", "us-bill", key, bill_key=key, link={"basis": "record-identity"},
        native_revision=bill.get("updateDateIncludingText") or bill.get("updateDate"),
        title=bill.get("title"),
        locator=f"https://www.congress.gov/bill/{congress}th-congress/"
        f"{'house' if bill_type.startswith('h') else 'senate'}-bill/{number}",
        published_at=bill.get("introducedDate"),
        fields={
            "congress": congress, "bill_type": bill_type, "bill_number": number,
            "title": _clean(bill.get("title")),
            "introduced_date": _day(bill.get("introducedDate")),
            "origin_chamber": _clean(bill.get("originChamber")),
            "update_date": _clean(bill.get("updateDate")),
            "update_date_including_text": _clean(bill.get("updateDateIncludingText")),
            "sponsors": sponsors,
            "cosponsors": _cosponsors(cosponsor_items),
            "actions": _actions(action_items),
            "laws": _laws(_list(bill.get("laws"), "laws")),
            "crs_summaries": sorted(summaries, key=lambda s: (s["action_date"] or "", s["version_code"] or "")),
        },
    )]


_US_POSITIONS = {"yea": "Yea", "aye": "Yea", "nay": "Nay", "no": "Nay", "present": "Present",
                 "not voting": "Not Voting"}


def _position(value: Any) -> str:
    text = _clean(value)
    if not text:
        raise LegislationFormatError("schema_drift", "a member position is missing")
    return text  # stored exactly as published; _US_POSITIONS is only used to validate the vocabulary


def parse_house_vote(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    vote = _mapping(_json(responses["vote"]).get("houseRollCallVote"), "house roll call vote")
    members_payload = _mapping(_json(responses["members"]).get("houseRollCallVoteMemberVotes"),
                               "house roll call member votes")
    congress, session, roll = int(vote["congress"]), int(vote["sessionNumber"]), int(vote["rollCallNumber"])
    positions = []
    for item in _list(members_payload.get("results"), "member votes"):
        item = _mapping(item, "member vote")
        cast = _position(item.get("voteCast"))
        if cast.casefold() not in _US_POSITIONS:
            raise LegislationFormatError("schema_drift", f"unknown vote position {cast!r}")
        positions.append({**_us_member(item, bioguide_field="bioguideID"), "position": cast})
    measure = None
    link: dict[str, Any] = {"basis": "none"}
    bill_key = None
    if vote.get("legislationType") and vote.get("legislationNumber"):
        try:
            bill_key = us_bill_key(congress, vote["legislationType"], vote["legislationNumber"])
            link = {"basis": "source-reference", "field": "legislationType/legislationNumber"}
        except LegislationMappingError:
            bill_key = None  # amendments and other measures stay unlinked
        measure = f"{vote['legislationType']} {vote['legislationNumber']}"
    key = f"us-roll-call:{congress}-house-{session}-{roll}"
    return [_record(
        "congress-gov-house-vote-json", "us-roll-call", key, bill_key=bill_key, link=link,
        native_revision=vote.get("updateDate"), title=f"House roll call {roll} ({congress}th Congress, session "
        f"{session}): {_clean(vote.get('voteQuestion')) or ''}",
        locator=f"https://clerk.house.gov/Votes/{_day(vote.get('startDate'))[:4]}{roll}"
        if _day(vote.get("startDate")) else "https://clerk.house.gov/Votes",
        published_at=vote.get("startDate"),
        fields={
            "chamber": "House", "congress": congress, "session": session, "roll_number": roll,
            "date": _day(vote.get("startDate")), "question": _clean(vote.get("voteQuestion")),
            "result": _clean(vote.get("result")), "vote_type": _clean(vote.get("voteType")),
            "measure": measure, "update_date": _clean(vote.get("updateDate")),
            "totals_as_published": [dict(t) for t in _list(vote.get("votePartyTotal"), "party totals")],
            "positions": sorted(positions, key=lambda p: p["member_id"]),
        },
    )]


_SENATE_TYPES = {"h.r.": "hr", "s.": "s", "h.j.res.": "hjres", "s.j.res.": "sjres", "h.con.res.": "hconres",
                 "s.con.res.": "sconres", "h.res.": "hres", "s.res.": "sres"}


def parse_senate_vote(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    root = _xml(responses["vote"])
    if root.tag != "roll_call_vote":
        raise LegislationFormatError("schema_drift", "not a Senate roll_call_vote document")
    congress, session, number = (int(_x(root, t) or 0) for t in ("congress", "session", "vote_number"))
    if not (congress and session and number):
        raise LegislationFormatError("schema_drift", "Senate vote identity is incomplete")
    positions = []
    for member in root.findall("members/member"):
        lis = _x(member, "lis_member_id")
        cast = _position(_x(member, "vote_cast"))
        if cast.casefold() not in _US_POSITIONS:
            raise LegislationFormatError("schema_drift", f"unknown vote position {cast!r}")
        positions.append({
            "member_key": member_key("us-lis", lis), "scheme": "us-lis", "member_id": lis,
            "name_as_published": _x(member, "member_full"), "first_name": _x(member, "first_name"),
            "last_name": _x(member, "last_name"), "party": _x(member, "party"), "state": _x(member, "state"),
            "district": None, "position": cast,
        })
    document = root.find("document")
    doc_type = (_x(document, "document_type") or "").casefold().replace(" ", "")
    doc_number = _x(document, "document_number")
    bill_key, link = None, {"basis": "none"}
    if doc_type in _SENATE_TYPES and doc_number:
        bill_key = _us_bill_key(_x(document, "document_congress") or congress, _SENATE_TYPES[doc_type], doc_number)
        link = {"basis": "source-reference", "field": "document/document_type+document_number"}
    date = _day(_x(root, "vote_date"))
    key = f"us-roll-call:{congress}-senate-{session}-{number}"
    return [_record(
        "senate-lis-vote-xml", "us-roll-call", key, bill_key=bill_key, link=link,
        native_revision=_x(root, "modify_date"),
        title=f"Senate roll call {number} ({congress}th Congress, session {session}): "
        f"{_x(root, 'vote_question_text') or ''}",
        locator=f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/"
        f"vote_{congress}_{session}_{number:05d}.xml",
        published_at=date,
        fields={
            "chamber": "Senate", "congress": congress, "session": session, "roll_number": number,
            "date": date, "question": _x(root, "vote_question_text"), "result": _x(root, "vote_result"),
            "measure": _x(document, "document_name"), "update_date": _x(root, "modify_date"),
            "totals_as_published": [{child.tag: _clean(child.text) for child in root.find("count") or []}],
            "positions": sorted(positions, key=lambda p: p["member_id"]),
            "member_id_note": "Senate files publish LIS member ids, not bioguide IDs",
        },
    )]


# ------------------------------------------------------------------ GovInfo


_PACKAGE = re.compile(r"^BILLS-(\d{1,3})([a-z]+?)(\d{1,5})([a-z]{2,4})$")


def parse_govinfo_package(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    summary = _mapping(_json(responses["summary"]), "package summary")
    package_id = _clean(summary.get("packageId")) or ""
    match = _PACKAGE.fullmatch(package_id)
    if not match:
        raise LegislationFormatError("schema_drift", f"not a BILLS package id: {package_id!r}")
    version = _clean(summary.get("billVersion")) or match[4]
    if version.casefold() != match[4]:
        raise LegislationFormatError("schema_drift", "package id and billVersion disagree")
    bill_key = _us_bill_key(match[1], match[2], match[3])
    text = responses["text"]
    download = dict(summary.get("download") or {})
    return [_record(
        "govinfo-bills-package-json", "us-text-version", f"us-text-version:{package_id}", bill_key=bill_key,
        link={"basis": "record-identity", "field": "packageId"},
        native_revision=summary.get("lastModified"), title=summary.get("title"),
        locator=_clean(summary.get("detailsLink")) or f"https://www.govinfo.gov/app/details/{package_id}",
        published_at=summary.get("dateIssued"),
        fields={
            "package_id": package_id, "version_code": version.casefold(),
            "version_label": US_VERSION_CODES.get(version.casefold(), "unlisted version code"),
            "date_issued": _day(summary.get("dateIssued")), "last_modified": _clean(summary.get("lastModified")),
            "content_sha256": hashlib.sha256(text).hexdigest(), "content_bytes": len(text),
            "text_locator": _clean(download.get("xmlLink")) or _clean(download.get("txtLink")),
            "formats": sorted(k for k, v in download.items() if v),
            "text_retention": "referenced by locator and hash; the text is not stored or summarised",
        },
    )]


def parse_billstatus(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    root = _xml(responses["billstatus"])
    bill = root.find("bill")
    if root.tag != "billStatus" or bill is None:
        raise LegislationFormatError("schema_drift", "not a BILLSTATUS document")
    number = _x(bill, "number") or _x(bill, "billNumber")
    bill_type = _x(bill, "type") or _x(bill, "billType")
    congress = _x(bill, "congress")
    key = _us_bill_key(congress, bill_type, number)
    actions = []
    for item in bill.findall("actions/item"):
        actions.append({
            "actionDate": _x(item, "actionDate"), "actionTime": _x(item, "actionTime"),
            "actionCode": _x(item, "actionCode"), "text": _x(item, "text"), "type": _x(item, "type"),
            "sourceSystem": {"name": _x(item, "sourceSystem/name")},
            "recordedVotes": [{"chamber": _x(v, "chamber"), "congress": _x(v, "congress"),
                               "sessionNumber": _x(v, "sessionNumber"), "rollNumber": _x(v, "rollNumber"),
                               "date": _x(v, "date"), "url": _x(v, "url")}
                              for v in item.findall("recordedVotes/recordedVote")],
        })

    def people(path: str) -> list[dict[str, Any]]:
        return [{child.tag: _clean(child.text) for child in item} for item in bill.findall(path)]

    sponsors = [{**_us_member(s), "role": "sponsor"} for s in people("sponsors/item")]
    cosponsors = _cosponsors([{**c, "isOriginalCosponsor": (c.get("isOriginalCosponsor") or "").lower() == "true"}
                              for c in people("cosponsors/item")])
    versions = [{"type": _x(item, "type"), "date": _day(_x(item, "date")),
                 "urls": [_clean(u.text) for u in item.findall("formats/item/url") if u.text]}
                for item in bill.findall("textVersions/item")]
    laws = _laws(people("laws/item"))
    congress_no, kind, bill_no = int(congress), str(bill_type).lower(), int(number)
    return [_record(
        "govinfo-billstatus-xml", "us-bill-status", f"us-bill-status:{congress_no}-{kind}-{bill_no}",
        bill_key=key, link={"basis": "record-identity"}, native_revision=_x(bill, "updateDate"),
        title=_x(bill, "title"),
        locator=f"https://www.govinfo.gov/bulkdata/BILLSTATUS/{congress_no}/{kind}/"
        f"BILLSTATUS-{congress_no}{kind}{bill_no}.xml",
        published_at=_x(bill, "introducedDate"),
        fields={
            "congress": congress_no, "bill_type": kind, "bill_number": bill_no, "title": _x(bill, "title"),
            "introduced_date": _day(_x(bill, "introducedDate")), "update_date": _x(bill, "updateDate"),
            "sponsors": sponsors, "cosponsors": cosponsors, "actions": _actions(actions), "laws": laws,
            "text_versions": versions,
        },
    )]


# ------------------------------------------------------------------ UK Parliament


def _bill_candidate(unit: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """A division or debate names no bill itself: the bill the operator declared is an unlinked candidate."""
    if unit.get("bill_id") is None:
        return None, {"basis": "none"}
    return None, {"basis": "none", "candidate_bill_key": uk_bill_key(unit["bill_id"]),
                  "candidate_basis": "operator-selection",
                  "note": "the source states no bill; a reviewer accepts or rejects this candidate link"}


def parse_uk_bill(responses: Mapping[str, bytes]) -> list[dict[str, Any]]:
    bill = _mapping(_json(responses["bill"]), "bill")
    bill_id = bill.get("billId")
    key = uk_bill_key(bill_id)
    stages_payload = _mapping(_json(responses["stages"]), "stages")
    stage_items = _list(stages_payload.get("items"), "stages")
    _complete(stages_payload, stage_items, "stages")
    publications_payload = _mapping(_json(responses["publications"]), "publications")
    publication_items = _list(publications_payload.get("publications"), "publications")
    sponsors = []
    for item in _list(bill.get("sponsors"), "sponsors"):
        item = _mapping(item, "sponsor")
        organisation = item.get("organisation") or {}
        if item.get("member"):
            sponsors.append({**_uk_member(_mapping(item["member"], "sponsor member")), "role": "sponsor",
                             "organisation": _clean(organisation.get("name")) if organisation else None,
                             "sort_order": item.get("sortOrder")})
        elif organisation:
            sponsors.append({"member_key": None, "scheme": None, "member_id": None,
                             "name_as_published": _clean(organisation.get("name")), "role": "sponsor-organisation",
                             "organisation": _clean(organisation.get("name")), "sort_order": item.get("sortOrder")})
    locator = f"https://bills.parliament.uk/bills/{int(bill_id)}"
    stages, first_sitting = [], None
    for item in stage_items:
        item = _mapping(item, "stage")
        sittings = [{"sitting_id": s.get("id"), "date": _day(s.get("date"))}
                    for s in (_mapping(s, "sitting") for s in _list(item.get("stageSittings"), "sittings"))]
        dates = sorted(s["date"] for s in sittings if s["date"])
        if dates and (first_sitting is None or dates[0] < first_sitting):
            first_sitting = dates[0]
        stages.append(_record(
            "uk-bills-json", "uk-stage", f"uk-stage:{int(bill_id)}-{int(item['id'])}", bill_key=key,
            link={"basis": "record-identity", "field": "billStage.billId"},
            native_revision=_digest(item)[:16], title=f"{bill.get('shortTitle')}: {item.get('description')} "
            f"({item.get('house')})",
            locator=f"{locator}/stages/{int(item['id'])}", published_at=dates[0] if dates else None,
            fields={"bill_id": int(bill_id), "bill_stage_id": int(item["id"]), "stage_id": item.get("stageId"),
                    "description": _clean(item.get("description")), "abbreviation": _clean(item.get("abbreviation")),
                    "house": _clean(item.get("house")), "session_id": item.get("sessionId"),
                    "sort_order": item.get("sortOrder"),
                    "sittings": sorted(sittings, key=lambda s: (s["date"] or "", str(s["sitting_id"])))},
        ))
    publications = []
    for item in publication_items:
        item = _mapping(item, "publication")
        links = [{"url": _clean(link.get("url")), "content_type": _clean(link.get("contentType")),
                  "title": _clean(link.get("title"))} for link in _list(item.get("links"), "links")]
        kind = _clean((item.get("publicationType") or {}).get("name"))
        publications.append(_record(
            "uk-bills-json", "uk-publication", f"uk-publication:{int(bill_id)}-{int(item['id'])}", bill_key=key,
            link={"basis": "record-identity", "field": "publication.billId"},
            native_revision=_digest(item)[:16], title=item.get("title"),
            locator=next((lk["url"] for lk in links if str(lk["url"] or "").startswith("https://")),
                         f"{locator}/publications"),
            published_at=item.get("displayDate"),
            fields={"bill_id": int(bill_id), "publication_id": int(item["id"]), "title": _clean(item.get("title")),
                    "publication_type": kind, "display_date": _day(item.get("displayDate")),
                    "house": _clean(item.get("house")), "links": links},
        ))
    royal_assent = [s["fields"]["sittings"] for s in stages if "royal assent" in
                    str(s["fields"]["description"] or "").casefold()]
    bill_record = _record(
        "uk-bills-json", "uk-bill", key, bill_key=key, link={"basis": "record-identity"},
        native_revision=bill.get("lastUpdate"), title=bill.get("shortTitle"), locator=locator,
        published_at=first_sitting,
        fields={
            "bill_id": int(bill_id), "short_title": _clean(bill.get("shortTitle")),
            "long_title": _clean(bill.get("longTitle")),
            "introduced_session_id": bill.get("introducedSessionId"),
            "included_session_ids": list(bill.get("includedSessionIds") or []),
            "originating_house": _clean(bill.get("originatingHouse")),
            "current_house": _clean(bill.get("currentHouse")),
            "last_update": _clean(bill.get("lastUpdate")),
            "bill_withdrawn": _clean(bill.get("billWithdrawn")), "defeated": bool(bill.get("isDefeated")),
            "is_act": bool(bill.get("isAct")),
            "introduced_date": first_sitting,
            "royal_assent": "published" if royal_assent and royal_assent[0] else "not published",
            "sponsors": sponsors,
            "stage_count": len(stages), "publication_count": len(publications),
        },
    )
    return [bill_record, *stages, *publications]


def _division(fmt: str, payload: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, Any]:
    commons = fmt == "uk-commons-division-json"

    def get(commons_key: str, lords_key: str) -> Any:
        return payload.get(commons_key if commons else lords_key)

    division_id = get("DivisionId", "divisionId")
    if division_id in (None, "") or int(division_id) != int(unit["id"]):
        raise LegislationFormatError("schema_drift", "division payload does not match the declared division")
    groups = ({"aye": "Ayes", "no": "Noes", "aye_teller": "AyeTellers", "no_teller": "NoTellers"} if commons else
              {"content": "contents", "not_content": "notContents", "content_teller": "contentTellers",
               "not_content_teller": "notContentTellers"})
    house = "Commons" if commons else "Lords"
    positions = []
    for position, field in groups.items():
        for item in _list(payload.get(field), field):
            positions.append({**_uk_member(_mapping(item, "division member"), house=house), "position": position})
    bill_key, link = _bill_candidate(unit)
    date = _day(get("Date", "date"))
    counts = ({"ayes": payload.get("AyeCount"), "noes": payload.get("NoCount")} if commons else
              {"contents": payload.get("contentCount"), "not_contents": payload.get("notContentCount")})
    base = ("https://votes.parliament.uk/votes/commons/division/" if commons
            else "https://votes.parliament.uk/votes/lords/division/")
    return _record(
        fmt, "uk-division", f"uk-division:{house.lower()}-{int(division_id)}", bill_key=bill_key, link=link,
        native_revision=get("PublicationUpdated", "publicationUpdated") or _digest(payload)[:16],
        title=get("Title", "title"), locator=f"{base}{int(division_id)}", published_at=date,
        fields={"house": house, "division_id": int(division_id), "number": get("Number", "number"),
                "date": date, "title": _clean(get("Title", "title")), "counts_as_published": counts,
                "publication_updated": _clean(get("PublicationUpdated", "publicationUpdated")),
                "positions": sorted(positions, key=lambda p: (p["position"], p["member_id"]))},
    )


def parse_commons_division(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_division("uk-commons-division-json", _mapping(_json(responses["division"]), "division"), unit)]


def parse_lords_division(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_division("uk-lords-division-json", _mapping(_json(responses["division"]), "division"), unit)]


def parse_hansard_debate(responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _mapping(_json(responses["debate"]), "debate")
    overview = _mapping(payload.get("Overview"), "debate overview")
    ext_id = _clean(overview.get("ExtId"))
    if not ext_id or ext_id != str(unit["ext_id"]) or not re.fullmatch(r"[A-Za-z0-9-]+", ext_id):
        raise LegislationFormatError("schema_drift", "debate section does not match the declared external id")
    date, house = _day(overview.get("Date")), _clean(overview.get("House")) or "Commons"
    locator = f"https://hansard.parliament.uk/{house}/{date}/debates/{ext_id}"
    contributions = []
    for item in _list(payload.get("Items"), "items"):
        item = _mapping(item, "item")
        if str(item.get("ItemType") or "").casefold() != "contribution":
            continue
        member_id = item.get("MemberId")
        contributions.append({
            "contribution_id": _clean(item.get("ExternalId")),
            "member_key": member_key("uk-parliament", member_id) if member_id else None,
            "member_id": str(member_id) if member_id else None,
            "attributed_to": _clean(item.get("AttributedTo")),
            "locator": f"{locator}#contribution-{_clean(item.get('ExternalId'))}",
        })  # the contribution text ("Value") is deliberately not retained
    bill_key, link = _bill_candidate(unit)
    return [_record(
        "uk-hansard-debate-json", "uk-debate-reference", f"uk-debate:{ext_id}", bill_key=bill_key, link=link,
        native_revision=_digest([overview, [c["contribution_id"] for c in contributions]])[:16],
        title=overview.get("Title"), locator=locator, published_at=date,
        fields={"ext_id": ext_id, "title": _clean(overview.get("Title")), "date": date, "house": house,
                "location": _clean(overview.get("Location")), "contributions": contributions,
                "retention": "reference and locator only; the transcript is not mirrored"},
    )]


# ------------------------------------------------------------------ units and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in selection.get(key) or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a legislation selection names 1-{MAX_UNITS} {key}")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Named request paths (relative to the endpoint) and parameters for one selection unit."""
    page = {"format": "json", "limit": PAGE_SIZE}
    if fmt == "congress-gov-bill-json":
        base = f"/bill/{int(unit['congress'])}/{str(unit['type']).lower()}/{int(unit['number'])}"
        return {"bill": (base, {"format": "json"}), "actions": (f"{base}/actions", page),
                "cosponsors": (f"{base}/cosponsors", page), "summaries": (f"{base}/summaries", page)}
    if fmt == "congress-gov-house-vote-json":
        base = f"/house-vote/{int(unit['congress'])}/{int(unit['session'])}/{int(unit['roll'])}"
        return {"vote": (base, {"format": "json"}), "members": (f"{base}/members", {"format": "json", "limit": 500})}
    if fmt == "senate-lis-vote-xml":
        congress, session, roll = int(unit["congress"]), int(unit["session"]), int(unit["roll"])
        return {"vote": (f"/vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml", {})}
    if fmt == "govinfo-bills-package-json":
        package = str(unit["id"])
        if not _PACKAGE.fullmatch(package):
            raise SourcePackError("invalid_manifest", f"not a BILLS package id: {package!r}")
        return {"summary": (f"/packages/{package}/summary", {}), "text": (f"/packages/{package}/xml", {})}
    if fmt == "govinfo-billstatus-xml":
        congress, kind, number = int(unit["congress"]), str(unit["type"]).lower(), int(unit["number"])
        return {"billstatus": (f"/{congress}/{kind}/BILLSTATUS-{congress}{kind}{number}.xml", {})}
    if fmt == "uk-bills-json":
        bill_id = int(unit["id"])
        return {"bill": (f"/Bills/{bill_id}", {}), "stages": (f"/Bills/{bill_id}/Stages", {"Take": PAGE_SIZE}),
                "publications": (f"/Bills/{bill_id}/Publications", {})}
    if fmt == "uk-commons-division-json":
        return {"division": (f"/division/{int(unit['id'])}.json", {})}
    if fmt == "uk-lords-division-json":
        return {"division": (f"/Divisions/{int(unit['id'])}", {})}
    if fmt == "uk-hansard-debate-json":
        ext = str(unit["ext_id"])
        if not re.fullmatch(r"[A-Za-z0-9-]+", ext):
            raise SourcePackError("invalid_manifest", "a Hansard external id is alphanumeric")
        return {"debate": (f"/debates/debate/{ext}.json", {})}
    raise SourcePackError("invalid_manifest", f"unknown legislation format {fmt!r}")


_PARSERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "congress-gov-bill-json": lambda r, u: parse_congress_bill(r),
    "congress-gov-house-vote-json": lambda r, u: parse_house_vote(r),
    "senate-lis-vote-xml": lambda r, u: parse_senate_vote(r),
    "govinfo-bills-package-json": lambda r, u: parse_govinfo_package(r),
    "govinfo-billstatus-xml": lambda r, u: parse_billstatus(r),
    "uk-bills-json": lambda r, u: parse_uk_bill(r),
    "uk-commons-division-json": parse_commons_division,
    "uk-lords-division-json": parse_lords_division,
    "uk-hansard-debate-json": parse_hansard_debate,
}


def parse_unit(fmt: str, responses: Mapping[str, bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    if fmt not in _PARSERS:
        raise LegislationFormatError("schema_drift", f"unknown legislation format {fmt!r}")
    return _PARSERS[fmt](responses, unit)


def legislation_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("legislation") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "legislation sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "legislation sources state their LIVE_VERIFICATION status")
    _units(fmt, dict(declared.get("selection") or {}))
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed legislation formats declare a required secret")
    return declared


class LegislationAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = legislation_declaration(self.source)
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
            "legislation": {"provider": self.declared["provider"], "format": self.format, "units": len(self.units),
                            "keyed": bool(FORMATS[self.format]["keyed"])},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "legislation runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, application/xml"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "this legislation source needs its API key secret")
            headers["X-Api-Key"] = self.secret
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "legislation response was served from another host")
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
        except (LegislationFormatError, LegislationMappingError) as exc:
            code = getattr(exc, "code", "schema_drift")
            raise SourcePackError("budget_exhausted" if code == "input_limit" else "schema_drift",
                                  f"{code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": "noesis-legislation-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.declared["provider"], "format": self.format, "unit_index": index,
            "unit": unit, "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"],
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": f"{record['record_key']}", "title": record["title"], "url": record["locator"],
                "language": "en", "published_at": record["published_at"],
                "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "legislation_record": record, "legislation_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: LegislationAdapter}


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
    adapter = LegislationAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
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
    "PROVIDER_CONTRACTS",
    "REVIEW_BOUNDARY",
    "LegislationAdapter",
    "LegislationFormatError",
    "fixture_transport",
    "legislation_declaration",
    "parse_unit",
    "replay_native_fixture",
    "requests_for",
]

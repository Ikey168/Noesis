"""SEC Form 13F information tables and structured Schedule 13D/13G as ownership assertions (#2883).

The ``sec-edgar-holdings`` connector reads explicitly selected filings on
``www.sec.gov/Archives/edgar/data`` and parses them fail-closed into
``noesis-ownership-record-v2`` records (:mod:`src.kb.ownership_records`):

* **Form 13F-HR / 13F-HR/A**: the cover page (``primary_doc.xml``: filing
  manager, report period, amendment number and type) and the information
  table (one ``reported_holding`` per row: issuer name, CUSIP, class, shares
  or principal, value, put/call, investment discretion, other managers,
  sole/shared/none voting authority). A 13F row states a quarter-end
  position, never a percentage or control.
* **Schedule 13D / 13G and amendments** in the XML format EDGAR requires for
  filings made since 2024-12-18: one ``shareholding`` per reporting person
  with percent of class, aggregate amount, sole/shared voting and dispositive
  power, the cover-page group box and the other group members as stated.

Every assertion carries the ``filing`` it comes from (accession, form, dates)
and its place on the amendment chain. An amendment is a new record on that
chain; nothing it restates overwrites the original filing's records, and
:mod:`src.kb.ownership_holdings` selects the statement in force as of a date.
Issuers, filing managers and reporting persons are recorded per filing with
the identifiers the filing states (issuer CIK, CUSIP issuer number, filer
CIK), so a later filing never rewrites what an earlier one said about an
entity; they resolve to each other and to submissions records only through
reviewable identity candidates (:mod:`src.kb.ownership_identity`: the same
CIK is an ``exact-identifier`` candidate, the same CUSIP issuer number a
``cross-referenced-identifier`` one). Amendment chains are keyed by the CIKs
the filings state, independent of identity review.

Natural persons (``typeOfReportingPerson`` ``IN``) follow the pack's person
handling: by default the person record and the person's assertion are
owner-scoped and the person's name is withheld from every other record (group
member lists, filer lists). With the opt-in person-research mode
(``NOESIS_PERSON_RESEARCH``, :func:`src.config.env.person_research_enabled`)
they are stored unscoped under ``ownership-person-research-v1`` like
organisations.

Fair access: requests carry the declared User-Agent built from the pack's
``NOESIS_SEC_CONTACT`` secret, are spaced by the inherited
:class:`~src.ingestion.ownership_providers.EdgarAdapter` interval (well under
ten per second), honour HTTP 429, and each document is fetched once per run
(information-table pages are cut from one cached response). Element names
follow the EDGAR technical specifications as read for this adapter and are
**unverified against live filings** (``LIVE_VERIFICATION``); see
``docs/development/ownership-evidence/sec-holdings-source-audit.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

from src.ingestion.ownership_providers import (
    FIXTURE_SECRET,
    EdgarAdapter,
    _archive_url,
    _sha,
    _slug,
    cik10,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_records import (
    PERSON_POLICY_DEFAULT,
    PERSON_POLICY_RESEARCH,
    OwnershipRecordError,
    record_v2,
)

CONNECTOR = "sec-edgar-holdings"
FORMS_13F = frozenset({"13F-HR", "13F-HR/A"})
FORMS_13DG = frozenset({"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"})
FORMS = FORMS_13F | FORMS_13DG
MAX_FILINGS = 25
MAX_ROWS = 20_000
LICENSE = "SEC public data"
# Value units: SEC amended Form 13F so that filings made on or after 2023-01-03 report value rounded to the
# nearest dollar instead of thousands (verify: Release 34-95148). Earlier filings stay in thousands.
VALUE_IN_DOLLARS_FROM = "2023-01-03"
SEC_HOLDINGS_CONTRACT: dict[str, Any] = {
    "status": "implemented",
    "connector": CONNECTOR,
    "documentation": "https://www.sec.gov/submit-filings/technical-specifications",
    "access": "explicitly selected filings under www.sec.gov/Archives/edgar/data/{cik}/{accession}/: Form 13F-HR(/A) "
              "primary_doc.xml plus its information-table XML, and Schedule 13D/13G(/A) primary_doc.xml",
    "authentication": "none; a declared User-Agent with contact details is required by the fair-access policy "
                      "(required secret NOESIS_SEC_CONTACT, as for sec-edgar-ownership)",
    "rate_limits": "fair-access policy: at most 10 requests per second; live requests are spaced by the EDGAR "
                   "adapter interval, HTTP 429 is honoured and each document is fetched once per run",
    "pagination": "one selected filing per work item; information-table rows are paged from one cached response",
    "cadence": "per filing (13F quarterly within 45 days of quarter end; Schedule 13D/13G per event and amendment)",
    "terms": "SEC public data; fair-access policy (https://www.sec.gov/os/accessing-edgar-data)",
    "retained_evidence": "each primary document and information table retained as the runtime document with SHA-256",
    "identifiers": ["accession number", "filer CIK", "issuer CIK (13D/13G)", "CUSIP (and its issuer number)",
                    "FIGI when stated"],
    "coverage": "explicitly selected 13F-HR and structured (XML) Schedule 13D/13G filings, 1-25 per source; pre-2024 "
                "HTML/text schedules, Forms 3/4/5 and 13F notices stay references from sec-edgar-ownership",
    "unavailable_fallback": "a 404 is 'none reported'; a document that is not the expected XML stays an unparsed "
                            "filing reference; a row count that differs from the stated table total fails the source",
    "cost": "free",
}
LIVE_VERIFICATION = {"status": "unverified-live", "last_run": None, "result": "not-attempted",
                     "cause": "element names follow the EDGAR technical specifications as read for this adapter; no "
                              "live filing was fetched from this runtime (egress to www.sec.gov blocked)"}


# ------------------------------------------------------------------ helpers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: Any, *names: str) -> Any:
    wanted = set(names)
    for child in element.iter():
        if _local(child.tag) in wanted:
            return child
    return None


def _text(element: Any, *names: str) -> str | None:
    wanted = set(names)
    if element is None:
        return None
    for child in element.iter():
        if _local(child.tag) in wanted and (child.text or "").strip():
            return child.text.strip()
    return None


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in element.iter() if _local(child.tag) == name]


def _xml(raw: bytes) -> Any:
    """Parse a filing document; DTDs and entity declarations are refused, never expanded."""
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise SourcePackError("schema_drift", "SEC XML documents carry no DTD or entity declarations")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None


def _iso_date(value: str | None) -> str | None:
    """ISO, MM-DD-YYYY or MM/DD/YYYY as stated; anything else stays unknown."""
    if not value:
        return None
    text = value.strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{2})[-/](\d{2})[-/](\d{4})", text)
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else None


def _amount(value: str | None) -> str | None:
    text = (value or "").replace(",", "").strip()
    return text if re.fullmatch(r"\d+(\.\d+)?", text) else None


def _bool(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    return True if text in {"true", "y", "yes", "1"} else False if text in {"false", "n", "no", "0"} else None


def _enum(value: str | None, allowed: Mapping[str, str], field: str) -> str | None:
    if value is None:
        return None
    key = value.strip().upper()
    if key not in allowed:
        raise SourcePackError("schema_drift", f"{field} {value!r} is not a value the specification defines")
    return allowed[key]


def _person_research() -> bool:
    from src.config.env import person_research_enabled

    return person_research_enabled()


def _source(accession: str, document: str, url: str, raw_sha: str, locator: Mapping[str, Any]) -> dict[str, Any]:
    return {"provider": "sec-edgar", "provider_record_id": f"{accession}:{document}", "url": url,
            "statement_id": accession, "raw_sha256": raw_sha, "license": LICENSE, "locator": dict(locator)}


def _value_unit(filing_date: str | None, period: str | None) -> str | None:
    if filing_date:
        return "USD" if filing_date >= VALUE_IN_DOLLARS_FROM else "USD-thousands"
    # Without the filing date only a period after the change is unambiguous.
    return "USD" if period and period >= "2022-12-31" else None


# ------------------------------------------------------------------ Form 13F


def parse_13f_cover(raw: bytes, *, cik: str, accession: str, form: str) -> dict[str, Any]:
    """The 13F cover page: manager, report period, amendment and stated table totals."""
    root = _xml(raw)
    if root is None or _find(root, "coverPage") is None:
        raise SourcePackError("schema_drift", "13F primary document lacks a cover page")
    stated_form = _text(root, "submissionType")
    if stated_form and stated_form.upper() != form:
        raise SourcePackError("schema_drift", f"13F primary document states {stated_form}, selected as {form}")
    filer_cik = _text(_find(root, "filer"), "cik")  # the credentials' confirmation code is never read
    if filer_cik and cik10(filer_cik) != cik:
        raise SourcePackError("schema_drift", "13F primary document names another filer")
    cover = _find(root, "coverPage")
    is_amendment = _bool(_text(cover, "isAmendment"))
    if is_amendment is None:
        is_amendment = form.endswith("/A")
    if is_amendment != form.endswith("/A"):
        raise SourcePackError("schema_drift", "13F cover page amendment flag contradicts the form type")
    amendment_type = _enum(_text(cover, "amendmentType"), {"RESTATEMENT": "restatement",
                                                          "NEW HOLDINGS": "new_holdings"}, "13F amendmentType")
    period = _iso_date(_text(root, "periodOfReport") or _text(root, "reportCalendarOrQuarter"))
    manager = _find(cover, "filingManager")
    summary = _find(root, "summaryPage")
    return {
        "manager_name": _text(manager, "name"), "period": period, "is_amendment": is_amendment,
        "amendment_number": _amount(_text(cover, "amendmentNo")) if is_amendment else None,
        "amendment_type": amendment_type if is_amendment else None,
        "amends_accession": _text(root, "previousAccessionNumber") if is_amendment else None,
        "report_type": _text(cover, "reportType"),
        "table_entry_total": _amount(_text(summary, "tableEntryTotal")),
        "table_value_total": _amount(_text(summary, "tableValueTotal")),
        "other_included_managers": _amount(_text(summary, "otherIncludedManagersCount")),
        "signature_date": _iso_date(_text(_find(root, "signatureBlock"), "signatureDate")),
    }


def parse_13f_table(raw: bytes) -> list[dict[str, Any]]:
    """Information-table rows exactly as stated (strings); a malformed row fails the document."""
    root = _xml(raw)
    if root is None or _local(root.tag) != "informationTable":
        raise SourcePackError("schema_drift", "13F information table is not an informationTable document")
    rows = []
    for index, row in enumerate(_children(root, "infoTable")):
        if index >= MAX_ROWS:
            raise SourcePackError("unbounded_source", f"13F information tables are read up to {MAX_ROWS} rows")
        amounts = _find(row, "shrsOrPrnAmt")
        voting = _find(row, "votingAuthority")
        cusip = (_text(row, "cusip") or "").upper()
        amount = _amount(_text(amounts, "sshPrnamt"))
        if not re.fullmatch(r"[0-9A-Z*@#]{9}", cusip) or amount is None:
            raise SourcePackError("schema_drift", f"13F information-table row {index + 1} lacks a CUSIP or amount")
        rows.append({
            "index": index, "issuer_name": _text(row, "nameOfIssuer"), "title_of_class": _text(row, "titleOfClass"),
            "cusip": cusip, "figi": _text(row, "figi"), "value": _amount(_text(row, "value")), "amount": amount,
            "amount_type": _enum(_text(amounts, "sshPrnamtType"), {"SH": "SH", "PRN": "PRN"}, "13F sshPrnamtType"),
            "put_call": _enum(_text(row, "putCall"), {"PUT": "Put", "CALL": "Call"}, "13F putCall"),
            "investment_discretion": _enum(_text(row, "investmentDiscretion"),
                                           {"SOLE": "SOLE", "DFND": "DFND", "OTR": "OTR"}, "13F investmentDiscretion"),
            "other_managers": [m.text.strip() for m in _children(row, "otherManager") if (m.text or "").strip()] or None,
            "voting_authority": None if voting is None else {
                key.lower(): _amount(_text(voting, key)) for key in ("Sole", "Shared", "None")},
        })
    if rows and any(r["amount_type"] is None for r in rows):
        raise SourcePackError("schema_drift", "13F information-table rows state SH or PRN")
    return rows


def manager_key(accession: str) -> str:
    """The filing manager as named in one 13F filing (per-filing; grouped across filings by reviewed identity)."""
    return f"sec-edgar:13f:{accession}:manager"


def records_13f(cover: Mapping[str, Any], rows: list[dict[str, Any]], *, cik: str, accession: str, form: str,
                primary_url: str, table_url: str, primary_sha: str, table_sha: str, filing_date: str | None,
                start: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    """Records for one page of a 13F filing; page 0 also carries the manager and the filing reference."""
    cik = cik10(cik)
    holder = manager_key(accession)
    period = cover["period"]
    amendment = {"is_amendment": cover["is_amendment"], "number": cover["amendment_number"],
                 "type": cover["amendment_type"], "amends_accession": cover["amends_accession"],
                 "chain_key": f"13f:manager-cik:{cik}:period:{period or 'unstated'}",
                 "chain_basis": "filing manager CIK and report period as stated on the cover page"}
    filing = {"accession_number": accession, "form_type": form, "filing_date": filing_date, "period_of_report": period,
              "event_date": None, "document_url": table_url, "amendment": amendment}
    unit = _value_unit(filing_date, period)
    records = []
    if start == 0:
        name = cover["manager_name"] or f"13F filer CIK {cik}"
        records.append(record_v2(
            "legal_entity", holder, _source(accession, "primary_doc.xml", primary_url, primary_sha,
                                            {"xpath": "//coverPage/filingManager/name"}),
            name=name, jurisdiction=None, identifiers=[{"scheme": "sec-cik", "value": cik, "authority": "SEC"}],
            entity_type="institutional investment manager (Form 13F filer)",
            native={"named_in": accession, "note": "Form 13F does not state the manager's legal form or jurisdiction"}))
        records.append(record_v2(
            "filing_reference", f"sec-edgar:holdings-filing:{accession}",
            _source(accession, "primary_doc.xml", primary_url, primary_sha, {"xpath": "//coverPage"}),
            entity_key=holder, form_type=form, accession_number=accession, filing_date=filing_date,
            period_of_report=period, document_url=primary_url,
            description=f"{form} for the period {period or 'unstated'}: {len(rows)} information-table rows parsed"
                        + (f"; amendment {cover['amendment_number'] or '(number not stated)'}, "
                           f"{cover['amendment_type'] or 'type not stated'}" if cover["is_amendment"] else ""),
            parsed=True, filers=[name], amendment=amendment,
            native={"report_type": cover["report_type"], "table_entry_total": cover["table_entry_total"],
                    "table_value_total": cover["table_value_total"], "value_unit": unit,
                    "other_included_managers": cover["other_included_managers"],
                    "information_table": table_url, "information_table_sha256": table_sha}))
    page = rows[start:] if limit is None else rows[start:start + limit]
    first_names: dict[str, str] = {}
    for row in rows:
        first_names.setdefault(row["cusip"][:6], row["issuer_name"] or f"issuer CUSIP {row['cusip'][:6]}")
    issuers_seen = set()
    for row in page:
        issuer6 = row["cusip"][:6]
        issuer = f"sec-edgar:13f:{accession}:issuer:{issuer6}"
        locator = {"xpath": f"//infoTable[{row['index'] + 1}]", "quote": row["cusip"]}
        if issuer6 not in issuers_seen:
            issuers_seen.add(issuer6)
            stated = sorted({r["issuer_name"] for r in rows if r["cusip"][:6] == issuer6 and r["issuer_name"]}
                            - {first_names[issuer6]})
            records.append(record_v2(
                "legal_entity", issuer, _source(accession, "information_table", table_url, table_sha, locator),
                name=first_names[issuer6], jurisdiction=None,
                identifiers=[{"scheme": "cusip-issuer", "value": issuer6,
                              "authority": "CUSIP issuer number as reported in Form 13F"}],
                other_names=[{"name": n, "type": "as stated on another row"} for n in stated] or None,
                native={"named_in": accession, "note": "issuer as named by the filing manager; resolves to other "
                                                      "records only through reviewed identity candidates"}))
        records.append(record_v2(
            "ownership_assertion", f"sec-edgar:13f:{accession}:row:{row['index']}",
            _source(accession, "information_table", table_url, table_sha, locator),
            subject_key=issuer, assertion_kind="reported_holding",
            holder={"key": holder, "name": cover["manager_name"], "kind": "entity"},
            validity={"from": None, "to": None, "from_status": "unknown", "to_status": "unknown"},
            statement_date=period, basis=f"{form} information table as reported by the institutional investment "
                                         "manager for its report period",
            filing=filing,
            position={"issuer_name": row["issuer_name"], "title_of_class": row["title_of_class"], "cusip": row["cusip"],
                      "figi": row["figi"], "amount": row["amount"], "amount_type": row["amount_type"],
                      "value": row["value"], "value_unit": unit if row["value"] is not None else None,
                      "put_call": row["put_call"], "investment_discretion": row["investment_discretion"],
                      "other_managers": row["other_managers"], "voting_authority": row["voting_authority"]},
            native={"note": "a quarter-end position as reported; not a percentage, control or beneficial-ownership "
                            "determination"}))
    return records


# --------------------------------------------------- Schedule 13D / 13G (XML)


_PERSON_CONTAINERS = ("reportingPersonInfo", "coverPageHeaderReportingPersonDetails")


def parse_13dg(raw: bytes, *, form: str) -> dict[str, Any] | None:
    """The cover pages of a structured Schedule 13D/13G as stated; ``None`` when the document is not that XML."""
    root = _xml(raw)
    if root is None:
        return None
    persons_xml = [e for e in root.iter() if _local(e.tag) in _PERSON_CONTAINERS]
    if not persons_xml:
        return None
    stated_form = _text(root, "submissionType")
    if stated_form and stated_form.upper() != form:
        raise SourcePackError("schema_drift", f"Schedule document states {stated_form}, selected as {form}")
    issuer = _find(root, "issuerInfo")
    issuer_cik = _text(issuer, "issuerCIK", "issuerCik")
    if not issuer_cik:
        raise SourcePackError("schema_drift", "Schedule 13D/13G cover page lacks the issuer CIK")
    persons = []
    for index, element in enumerate(persons_xml):
        name = _text(element, "reportingPersonName")
        if not name:
            raise SourcePackError("schema_drift", f"reporting person {index + 1} has no name")
        percent = (_text(element, "percentOfClass", "classPercent") or "").replace("%", "").strip()
        types = sorted({t.text.strip().upper() for t in _children(element, "typeOfReportingPerson")
                        if (t.text or "").strip()})
        group = (_text(element, "memberOfGroup", "memberGroup") or "").strip().lower()
        persons.append({
            "index": index, "container": _local(element.tag), "name": name,
            "cik": _text(element, "reportingPersonCIK", "reportingPersonCik"),
            "no_cik_stated": _bool(_text(element, "reportingPersonNoCIK")),
            "percent": percent if re.fullmatch(r"\d{1,3}(\.\d+)?", percent) else None,
            "percent_as_stated": _text(element, "percentOfClass", "classPercent"),
            "aggregate": _amount(_text(element, "aggregateAmountOwned",
                                       "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")),
            "powers": {key: _amount(_text(element, tag)) for key, tag in (
                ("sole_voting", "soleVotingPower"), ("shared_voting", "sharedVotingPower"),
                ("sole_dispositive", "soleDispositivePower"), ("shared_dispositive", "sharedDispositivePower"))},
            "types": types, "natural_person": "IN" in types,
            "member_of_group": group if group in {"a", "b"} else None, "member_of_group_as_stated": group or None,
            "citizenship_or_organization": _text(element, "citizenshipOrOrganization"),
        })
    amendment_no = _amount(_text(root, "amendmentNo"))
    return {
        "issuer_cik": cik10(issuer_cik), "issuer_name": _text(issuer, "issuerName"),
        "issuer_cusip": (_text(issuer, "issuerCUSIP", "issuerCusip", "issuerCusipNumber") or "").upper() or None,
        "class": _text(root, "securitiesClassTitle"),
        "event_date": _iso_date(_text(root, "dateOfEvent", "eventDateRequiresFilingThisStatement")),
        "is_amendment": form.endswith("/A"), "amendment_number": amendment_no,
        "amends_accession": _text(root, "previousAccessionNumber"),
        "five_percent_or_less": _bool(_text(root, "classOwnership5PercentOrLess")),
        "persons": persons,
    }


def records_13dg(parsed: Mapping[str, Any] | None, *, accession: str, form: str, url: str, raw_sha: str,
                 filing_date: str | None) -> list[dict[str, Any]]:
    research = _person_research()
    if parsed is None:
        return [record_v2(
            "filing_reference", f"sec-edgar:holdings-filing:{accession}",
            _source(accession, "primary_doc.xml", url, raw_sha, {"xpath": "/"}), entity_key=f"sec-edgar:filing:{accession}",
            form_type=form, accession_number=accession, filing_date=filing_date, document_url=url,
            description=f"{form} primary document is not the structured XML cover page; not parsed", parsed=False,
            amendment={"is_amendment": form.endswith("/A"), "chain_key": f"13dg:unparsed:{accession}",
                       "chain_basis": "not parsed"})]
    issuer = f"sec-edgar:13dg:{accession}:issuer"
    cusip = parsed["issuer_cusip"]
    identifiers = [{"scheme": "sec-cik", "value": parsed["issuer_cik"], "authority": "issuer CIK as stated"}]
    if cusip and re.fullmatch(r"[0-9A-Z*@#]{6,9}", cusip):
        identifiers.append({"scheme": "cusip-issuer", "value": cusip[:6],
                            "authority": "CUSIP issuer number as stated on the cover page"})
    source = lambda locator: _source(accession, "primary_doc.xml", url, raw_sha, locator)
    records = [record_v2("legal_entity", issuer, source({"xpath": "//issuerInfo"}),
                         name=parsed["issuer_name"] or f"issuer CIK {parsed['issuer_cik']}", jurisdiction=None,
                         identifiers=identifiers,
                         native={"named_in": accession, "class_cusip": cusip,
                                 "note": "issuer as named on the cover page; resolves to other records only through "
                                         "reviewed identity candidates"})]
    # Per-filing keys: each filing's statement about a reporting person is its own record; filings by one CIK are
    # grouped only through reviewed (exact sec-cik) identity candidates, never by rewriting an earlier record.
    keys = [f"sec-edgar:13dg:{accession}:reporting-person:{person['index']}" for person in parsed["persons"]]
    group_a = [i for i, p in enumerate(parsed["persons"]) if p["member_of_group"] == "a"]
    filers = []
    for person, key in zip(parsed["persons"], keys):
        natural = person["natural_person"]
        withheld = natural and not research
        if not withheld:
            filers.append(person["name"])
        xpath = f"//{person['container']}[{person['index'] + 1}]"
        policy = {} if not natural else {
            "person_policy": PERSON_POLICY_RESEARCH if research else PERSON_POLICY_DEFAULT,
            "owner_scoped": not research}
        holder_identifiers = [{"scheme": "sec-cik", "value": cik10(person["cik"]), "authority": "SEC"}] if person["cik"] else []
        native_holder = {"type_of_reporting_person": person["types"], "named_in": accession,
                         "citizenship_or_organization": person["citizenship_or_organization"]}
        if natural:
            records.append(record_v2("person", key, source({"xpath": xpath}), name=person["name"],
                                     identifiers=holder_identifiers, native=native_holder, **policy))
        else:
            records.append(record_v2("legal_entity", key, source({"xpath": xpath}), name=person["name"],
                                     jurisdiction=None, identifiers=holder_identifiers, native=native_holder))
        chain_holder = (f"reporting-person-cik:{cik10(person['cik'])}" if person["cik"]
                        else f"reporting-person-name:{_slug(person['name'])}")
        amendment = {"is_amendment": parsed["is_amendment"], "number": parsed["amendment_number"],
                     "type": None, "amends_accession": parsed["amends_accession"],
                     "chain_key": f"13dg:issuer-cik:{parsed['issuer_cik']}:{chain_holder}",
                     "chain_basis": "issuer CIK and reporting-person CIK as stated" if person["cik"] else
                                    "issuer CIK and reporting-person name as stated (no CIK on the cover page)"}
        members = []
        if person["member_of_group"] == "a":
            for other in group_a:
                if other == person["index"]:
                    continue
                peer = parsed["persons"][other]
                hidden = peer["natural_person"] and not research
                members.append({"key": None if hidden else keys[other], "name": None if hidden else peer["name"],
                                "kind": "person" if peer["natural_person"] else "entity", "withheld": hidden})
        group = {"member_of_group": person["member_of_group"], "members": members,
                 "basis": "cover-page row 2: (a) member of a group, (b) not, as checked by the filer"}
        share = None
        if person["percent"] is not None:
            share = {"exact": person["percent"], "shares": person["aggregate"], "class": parsed["class"],
                     "native": "percent of class as reported on the cover page"}
        records.append(record_v2(
            "ownership_assertion", f"sec-edgar:13dg:{accession}:reporting-person:{person['index']}:stake",
            source({"xpath": xpath, "quote": person["percent_as_stated"] or ""}),
            subject_key=issuer, assertion_kind="shareholding",
            holder={"key": key, "name": person["name"], "kind": "person" if natural else "entity"},
            share=share, validity={"from": parsed["event_date"], "to": None,
                                   "from_status": "stated" if parsed["event_date"] else "unknown", "to_status": "unknown"},
            statement_date=parsed["event_date"], basis=f"{form} cover page, as reported by the filer",
            filing={"accession_number": accession, "form_type": form, "filing_date": filing_date,
                    "period_of_report": None, "event_date": parsed["event_date"], "document_url": url,
                    "amendment": amendment},
            powers={**person["powers"], "aggregate_amount": person["aggregate"]}, group=group,
            native={"member_of_group_as_stated": person["member_of_group_as_stated"],
                    "five_percent_or_less": parsed["five_percent_or_less"],
                    "note": "beneficial ownership as reported by the filer under the Exchange Act; group members' "
                            "figures overlap and are never summed; not a Noesis determination"},
            **policy))
    records.append(record_v2(
        "filing_reference", f"sec-edgar:holdings-filing:{accession}", source({"xpath": "/"}), entity_key=issuer,
        form_type=form, accession_number=accession, filing_date=filing_date, period_of_report=parsed["event_date"],
        document_url=url, parsed=True, filers=filers or None,
        description=f"{form} cover pages parsed: {len(parsed['persons'])} reporting person(s)"
                    + ("; natural-person names withheld (owner-scoped)" if len(filers) < len(parsed["persons"]) else ""),
        amendment={"is_amendment": parsed["is_amendment"], "number": parsed["amendment_number"], "type": None,
                   "amends_accession": parsed["amends_accession"],
                   "chain_key": f"13dg:issuer-cik:{parsed['issuer_cik']}:filing",
                   "chain_basis": "issuer CIK as stated; per-person chains are on each assertion"}))
    return records


# ------------------------------------------------------------- runtime adapter


class SecHoldingsAdapter(EdgarAdapter):
    """One selected 13F or Schedule 13D/13G filing per work item, paced and identified like the EDGAR adapter."""

    def _work(self):
        filings = list(self.selection.get("filings") or [])
        if not 1 <= len(filings) <= MAX_FILINGS:
            raise SourcePackError("unbounded_source", f"{CONNECTOR} sources select 1-{MAX_FILINGS} filings")
        work = []
        for filing in filings:
            accession = str(filing.get("accession") or "")
            document = str(filing.get("document") or "")
            table = str(filing.get("information_table") or "")
            form = str(filing.get("form") or "").upper()
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", document):
                raise SourcePackError("unbounded_source", "filings select an accession number and document name")
            if form not in FORMS:
                raise SourcePackError("invalid_mapping", f"{CONNECTOR} parses {', '.join(sorted(FORMS))}")
            if (form in FORMS_13F) != bool(table) or (table and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", table)):
                raise SourcePackError("invalid_mapping", "13F filings name their information-table document; "
                                                         "schedules do not")
            filing_date = filing.get("filing_date")
            if filing_date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(filing_date)):
                raise SourcePackError("invalid_mapping", "filing_date is YYYY-MM-DD as listed by EDGAR")
            work.append((cik10(filing.get("cik")), form, accession, document, table, filing_date or ""))
        return work

    def _cached(self, url: str) -> tuple[int, bytes]:
        cache = self.__dict__.setdefault("_documents", {})
        if url not in cache:
            status, _, raw = self._get(url)
            cache[url] = (status, raw)
        return cache[url]

    def _fetch(self, item, *, start, limit):
        cik, form, accession, document, table, filing_date = item
        filing_date = filing_date or None
        primary_url = _archive_url(cik, accession, document)
        status, primary = self._cached(primary_url)
        subject = f"{cik}:{accession}"
        if status == 404:
            return self._envelope(subject, "filing", primary, None, [], outcome="none_reported"), len(primary), None, status
        primary_sha = _sha(primary)
        try:
            if form in FORMS_13DG:
                records = records_13dg(parse_13dg(primary, form=form), accession=accession, form=form, url=primary_url,
                                       raw_sha=primary_sha, filing_date=filing_date)
                native = primary.decode("utf-8", "replace")[:200_000]
                coverage = {"parsed": records[-1]["parsed"], "persons": sum(r["kind"] == "ownership_assertion"
                                                                            for r in records)}
                return (self._envelope(subject, "schedule-13dg", primary, native, records, coverage=coverage),
                        len(primary), None, status)
            cover = parse_13f_cover(primary, cik=cik, accession=accession, form=form)
            table_url = _archive_url(cik, accession, table)
            table_status, table_raw = self._cached(table_url)
            if table_status == 404:
                raise SourcePackError("schema_drift", "the selected 13F information table is not in the filing")
            rows = parse_13f_table(table_raw)
            stated = cover["table_entry_total"]
            if stated is not None and int(float(stated)) != len(rows):
                raise SourcePackError("schema_drift", f"13F information table has {len(rows)} rows; the summary page "
                                                      f"states {stated}")
            records = records_13f(cover, rows, cik=cik, accession=accession, form=form, primary_url=primary_url,
                                  table_url=table_url, primary_sha=primary_sha, table_sha=_sha(table_raw),
                                  filing_date=filing_date, start=start, limit=limit)
        except OwnershipRecordError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc
        next_start = start + limit if start + limit < len(rows) else None
        page_rows = rows[start:start + limit]
        native = {"cover": cover, "rows": page_rows}
        if start == 0:
            native["primary_document"] = primary.decode("utf-8", "replace")[:200_000]
        coverage = {"offset": start, "rows": len(page_rows), "total_rows": len(rows),
                    "table_entry_total_stated": stated, "primary_sha256": primary_sha}
        size = (len(primary) if start == 0 else 0) + len(table_raw) * len(page_rows) // max(len(rows), 1)
        return (self._envelope(subject, "13f-information-table", table_raw, native, records, coverage=coverage,
                               page=start), size, next_start, status)


ADAPTERS = {CONNECTOR: SecHoldingsAdapter}


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Replay authored native responses through the real adapter (source-pack conformance)."""
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


__all__ = ["ADAPTERS", "CONNECTOR", "FIXTURE_SECRET", "LIVE_VERIFICATION", "SEC_HOLDINGS_CONTRACT",
           "SecHoldingsAdapter", "fixture_transport", "parse_13dg", "parse_13f_cover", "parse_13f_table",
           "records_13dg", "records_13f", "replay_native_fixture"]

"""US Congress and UK Parliament records mapped onto the existing legislative dossier model (#2208, LT02).

This is the sibling mapping module :class:`src.domains.political.legislative_dossiers.LegislativeDossierStore`
consults for the legislation sources; there is no second dossier store. Every
acquired record (a ``noesis-legislation-record-v1`` record, see
:mod:`src.ingestion.legislation_sources`) is committed as one official-record
document revision (``political:<source_id>:<record_key>``) and a dossier cites
those revisions exactly as it cites Bundestag DIP and EUR-Lex documents.

Identity uses official identifiers only:

* a US bill is ``us-bill:<congress>-<type>-<number>`` (congress.gov / GovInfo
  bill type codes, lower case);
* a UK bill is ``uk-bill:<Bills API bill id>``; the session it was introduced
  in and every session it was carried into are kept beside the key as the
  Bills API states them;
* a text version keeps its GovInfo package id, version code and publication
  date; a roll call is keyed by chamber, congress, session and roll number; a
  division by house and division id; a debate reference by its Hansard
  section external id.

Mapping onto the dossier's stage vocabulary is a coarse *category* only
(``proposal``, ``amendment``, ``vote``, ``adoption``, ``publication``) plus the
additive ``debate`` category for Hansard references. The source's own action
text, action code, version code or stage description is kept verbatim in the
stage's ``legislation`` detail; nothing here states legal effect.

Contract note: ``noesis-legislative-dossier-v1`` gains the jurisdictions ``US``
and ``GB``, the stage category ``debate`` and an optional per-stage
``legislation`` object. These are additive: Bundestag and EUR-Lex dossiers
serialise byte-for-byte as before, so the contract version is unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

RECORD_CONTRACT = "noesis-legislation-record-v1"
JURISDICTIONS = frozenset({"DE", "EU", "US", "GB"})
# Source-pack source id -> (provider, jurisdiction). The ids are the ``official-political-records`` entries.
LEGISLATION_SOURCES: dict[str, tuple[str, str]] = {
    "us-congress-gov-bills": ("congress-gov", "US"),
    "us-congress-gov-house-votes": ("congress-gov", "US"),
    "us-senate-roll-calls": ("senate-lis", "US"),
    "us-govinfo-bills": ("govinfo", "US"),
    "us-govinfo-billstatus": ("govinfo", "US"),
    "uk-parliament-bills": ("uk-bills", "GB"),
    "uk-commons-divisions": ("uk-commons-votes", "GB"),
    "uk-lords-divisions": ("uk-lords-votes", "GB"),
    "uk-hansard-debates": ("uk-hansard", "GB"),
}
RECORD_KINDS = (
    "us-bill",
    "us-bill-status",
    "us-text-version",
    "us-roll-call",
    "uk-bill",
    "uk-stage",
    "uk-publication",
    "uk-division",
    "uk-debate-reference",
)
US_BILL_TYPES = ("hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres")
# GovInfo / congress.gov text version codes and their published labels.
US_VERSION_CODES = {
    "ih": "Introduced in House",
    "is": "Introduced in Senate",
    "rih": "Referral Instructions House",
    "ris": "Referral Instructions Senate",
    "rfh": "Referred in House",
    "rfs": "Referred in Senate",
    "rh": "Reported in House",
    "rs": "Reported in Senate",
    "rch": "Reference Change House",
    "rcs": "Reference Change Senate",
    "rdh": "Received in House",
    "rds": "Received in Senate",
    "pch": "Placed on Calendar House",
    "pcs": "Placed on Calendar Senate",
    "cph": "Considered and Passed House",
    "cps": "Considered and Passed Senate",
    "eh": "Engrossed in House",
    "es": "Engrossed in Senate",
    "eah": "Engrossed Amendment House",
    "eas": "Engrossed Amendment Senate",
    "ath": "Agreed to House",
    "ats": "Agreed to Senate",
    "enr": "Enrolled Bill",
}
_PROPOSAL_VERSIONS = frozenset({"ih", "is", "rih", "ris", "rfh", "rfs", "rch", "rcs", "rdh", "rds", "pch", "pcs"})
_ADOPTION_VERSIONS = frozenset({"enr"})
_KIND_STAGE = {
    "us-bill": "proposal",
    "us-bill-status": "proposal",
    "us-roll-call": "vote",
    "uk-bill": "proposal",
    "uk-publication": "publication",
    "uk-division": "vote",
    "uk-debate-reference": "debate",
}
# Additive stage category for Hansard references; never counted as a missing stage.
EXTRA_STAGES = ("debate",)
REVIEW_BOUNDARY = (
    "Bills, text versions, actions, stages, sponsors, votes and debate references as published: no passage "
    "prediction, member scoring, ideology rating, and no summary presented as the bill's legal effect."
)


class LegislationMappingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_legislation_source(source_id: str) -> bool:
    return source_id in LEGISLATION_SOURCES


def us_bill_key(congress: Any, bill_type: Any, number: Any) -> str:
    kind = re.sub(r"[^a-z]", "", str(bill_type or "").casefold())
    if kind not in US_BILL_TYPES:
        raise LegislationMappingError("invalid_identifier", f"unknown US bill type {bill_type!r}")
    try:
        congress_no, bill_no = int(str(congress).strip()), int(str(number).strip())
    except ValueError as exc:
        raise LegislationMappingError("invalid_identifier", "US bill congress and number are integers") from exc
    if not 1 <= congress_no <= 999 or not 1 <= bill_no <= 99999:
        raise LegislationMappingError("invalid_identifier", "US bill congress or number out of range")
    return f"us-bill:{congress_no}-{kind}-{bill_no}"


def uk_bill_key(bill_id: Any) -> str:
    try:
        value = int(str(bill_id).strip())
    except ValueError as exc:
        raise LegislationMappingError("invalid_identifier", "a UK Bills API bill id is an integer") from exc
    if value < 1:
        raise LegislationMappingError("invalid_identifier", "a UK Bills API bill id is positive")
    return f"uk-bill:{value}"


def bill_jurisdiction(bill_key: str) -> str:
    if bill_key.startswith("us-bill:"):
        return "US"
    if bill_key.startswith("uk-bill:"):
        return "GB"
    raise LegislationMappingError("invalid_identifier", "not a US or UK bill key")


def member_key(scheme: str, native_id: Any) -> str:
    """The record key of one member as a source identifies them (bioguide, Senate LIS or UK Parliament id)."""
    if scheme not in {"us-bioguide", "us-lis", "uk-parliament"}:
        raise LegislationMappingError("invalid_identifier", f"unknown member scheme {scheme!r}")
    text = str(native_id or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9]+", text):
        raise LegislationMappingError("invalid_identifier", "member identifier is missing or malformed")
    return f"legislation:member:{scheme}:{text}"


def version_stage(version_code: str) -> str:
    code = str(version_code or "").casefold()
    if code in _PROPOSAL_VERSIONS:
        return "proposal"
    if code in _ADOPTION_VERSIONS:
        return "adoption"
    return "amendment"


def uk_stage_category(description: str) -> str:
    text = str(description or "").casefold()
    if "royal assent" in text:
        return "adoption"
    if any(word in text for word in ("committee", "report", "amendment", "ping pong")):
        return "amendment"
    return "proposal"


def stage_for(record: Mapping[str, Any]) -> str:
    kind = record.get("record_kind")
    if kind == "us-text-version":
        return version_stage(record["fields"].get("version_code"))
    if kind == "uk-stage":
        return uk_stage_category(record["fields"].get("description"))
    if kind not in _KIND_STAGE:
        raise LegislationMappingError("unsupported_stage", f"record kind {kind!r} has no stage mapping")
    return _KIND_STAGE[kind]


def record_from_payload(content: str) -> dict[str, Any]:
    """The acquired record a committed legislation document carries as its content."""
    try:
        record = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise LegislationMappingError("invalid_source", "legislation document content is not a record") from exc
    if not isinstance(record, Mapping) or record.get("contract") != RECORD_CONTRACT:
        raise LegislationMappingError("invalid_source", f"legislation document does not carry {RECORD_CONTRACT}")
    if record.get("record_kind") not in RECORD_KINDS:
        raise LegislationMappingError("invalid_source", "unknown legislation record kind")
    return dict(record)


def event_date(record: Mapping[str, Any]) -> str | None:
    """The source date a record is placed at on a timeline (introduction, vote, sitting, issue or debate day)."""
    fields = record.get("fields") or {}
    kind = record["record_kind"]
    if kind in {"us-bill", "us-bill-status"}:
        return fields.get("introduced_date")
    if kind == "us-text-version":
        return fields.get("date_issued")
    if kind in {"us-roll-call", "uk-division", "uk-debate-reference"}:
        return fields.get("date")
    if kind == "uk-stage":
        dates = sorted(s["date"] for s in fields.get("sittings") or [] if s.get("date"))
        return dates[0] if dates else None
    if kind == "uk-publication":
        return fields.get("display_date")
    if kind == "uk-bill":
        return fields.get("introduced_date")
    return None


def stage_detail(record: Mapping[str, Any]) -> dict[str, Any]:
    """The jurisdiction-specific part of a dossier stage: the record as published, with its identity."""
    return {
        "record_kind": record["record_kind"],
        "record_key": record["record_key"],
        "provider": record["provider"],
        "bill_key": record.get("bill_key"),
        "bill_link": dict(record.get("bill_link") or {}),
        "native_revision": record.get("native_revision"),
        "evidence_origin": record.get("evidence_origin"),
        "fields": dict(record.get("fields") or {}),
        "source_assertion": "as published by the provider; not reconciled with other providers",
    }


def political_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    """``metadata.political`` for the committed document: the procedure link only where the source states it."""
    link = dict(record.get("bill_link") or {})
    political: dict[str, Any] = {"legislation_record_kind": record["record_kind"]}
    if record.get("bill_key") and link.get("basis") in {"record-identity", "source-reference"}:
        political["procedure_id"] = record["bill_key"]
    elif link.get("candidate_bill_key"):
        political["candidate_procedure_id"] = link["candidate_bill_key"]
    day = event_date(record)
    if day:
        political["event_at"] = day
    if record.get("evidence_origin") == "fixture":
        political["fixture"] = True
    return political


def document_for(record: Mapping[str, Any], source_id: str, *, observed_at_ms: int) -> dict[str, Any]:
    """A ``document-ingest-v1`` payload for one acquired record (the committed official-record revision)."""
    if source_id not in LEGISLATION_SOURCES:
        raise LegislationMappingError("unsupported_source", f"{source_id!r} is not a legislation source")
    provider, jurisdiction = LEGISLATION_SOURCES[source_id]
    if record.get("jurisdiction") != jurisdiction or record.get("provider") != provider:
        raise LegislationMappingError("source_mismatch", "record provider or jurisdiction disagrees with its source")
    from src.kb.temporal import parse_source_time

    published = record.get("published_at") or event_date(record)
    created = parse_source_time(published, field="published_at")[0] if published else None
    content = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "document_id": f"political:{source_id}:{record['record_key']}",
        "source_type": "note",
        "language": "en",
        "ingested_at": int(observed_at_ms),
        "created_at": created,
        "source_id": source_id,
        "url": record["locator"],
        "title": str(record.get("title") or record["record_key"]),
        "content": content,
        "metadata": {
            "tags": ["political", "legislation", jurisdiction.casefold()],
            "jurisdiction": jurisdiction,
            "document_type": record["record_kind"].replace("-", "_"),
            "official_identifier": record["record_key"],
            "canonical_url": record["locator"],
            "source_manifest_id": source_id,
            "political": json.dumps(political_metadata(record), sort_keys=True),
        },
    }


__all__ = [
    "EXTRA_STAGES",
    "JURISDICTIONS",
    "LEGISLATION_SOURCES",
    "RECORD_CONTRACT",
    "RECORD_KINDS",
    "REVIEW_BOUNDARY",
    "US_BILL_TYPES",
    "US_VERSION_CODES",
    "LegislationMappingError",
    "bill_jurisdiction",
    "document_for",
    "event_date",
    "is_legislation_source",
    "member_key",
    "political_metadata",
    "record_from_payload",
    "stage_detail",
    "stage_for",
    "uk_bill_key",
    "uk_stage_category",
    "us_bill_key",
    "version_stage",
]

"""Native EU/German acquisition and validated public export import.

CTIS and Berlin legal-portal exports use explicit import paths, not invented
private REST endpoints. Official publisher transport, native field locators,
source snapshots, registry versions and document lifecycle stay separate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import time
import zipfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from defusedxml import ElementTree as ET

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import (
    CapturedResponse,
    DurableHTTP,
    ProviderError,
    canonical,
    digest,
)
from src.ingestion.snapshots import SnapshotStore

SCHEMA_VERSION = "noesis-native-regional-v1"
PROVIDER_HOSTS = {
    "ctis": {"euclinicaltrials.eu", "www.euclinicaltrials.eu"},
    "drks": {"drks.de", "www.drks.de"},
    "cellar": {"publications.europa.eu", "op.europa.eu", "eur-lex.europa.eu"},
    "german-courts": {"www.rechtsprechung-im-internet.de"},
    "berlin-law": {"gesetze.berlin.de", "www.berlin.de"},
    "opencorporates": {"api.opencorporates.com", "opencorporates.com"},
    "opensanctions": {
        "api.opensanctions.org",
        "www.opensanctions.org",
        "data.opensanctions.org",
    },
    "ema": {"www.ema.europa.eu"},
    "bfarm": {"www.bfarm.de"},
}
COVERAGE = {
    "ctis": "CTIS public records only; missing public fields/documents are not evidence of absence",
    "drks": "DRKS registrations; registration, protocol, result and publication are distinct evidence",
    "cellar": "Explicit CELLAR selections and language/manifestation versions; current law is not inferred",
    "german-courts": "Official published federal decisions, not all German case law",
    "berlin-law": "Selected official Berlin publications/imports; historical versions remain historical",
    "opencorporates": "Provider enrichment, not an official registry substitute; registry jurisdiction is not headquarters",
    "opensanctions": "Review candidates only; provider presence or similarity is not identity or misconduct",
    "ema": "EMA website records; not all national authorisations and not openFDA API parity",
    "bfarm": "BfArM safety communications; PEI material and causal conclusions are not inferred",
}


def _official_url(provider, url):
    parsed = urlsplit(str(url))
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.hostname not in PROVIDER_HOSTS[provider]
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
    ):
        raise ProviderError(
            "source_identity",
            "record must identify its declared official/provider origin",
        )
    # Official historic XML indexes use HTTP identifiers. Preserve the native
    # identifier separately and fetch their documented HTTPS representation.
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))


def _string(value):
    return " ".join(str(value or "").split())


def _date(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y%m%d", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()  # noqa: DTZ007 - native calendar dates have no time zone
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(value).isoformat()
    except (ValueError, TypeError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _record(
    provider,
    identity,
    title,
    *,
    native,
    source_url,
    kind,
    fields=None,
    sections=(),
    language="de",
    relationships=(),
    publication_date=None,
    updated_at=None,
):
    if not isinstance(identity, str) or not identity or len(identity) > 2000:
        raise ProviderError("schema_drift", "native stable record identity is missing")
    return {
        "contract": SCHEMA_VERSION,
        "provider": provider,
        "provider_id": identity,
        "kind": kind,
        "title": _string(title) or identity,
        "source_url": _official_url(provider, source_url),
        "language": language,
        "published_at": _date(publication_date),
        "updated_at": _date(updated_at),
        "fields": dict(fields or {}),
        "missing_fields": sorted(
            k for k, v in (fields or {}).items() if v is None or v == "" or v == []
        ),
        "sections": list(sections),
        "relationships": list(relationships),
        "native": native,
        "coverage_notice": COVERAGE[provider],
        "review_required": provider == "opensanctions",
        "is_current_law": None
        if provider in {"cellar", "berlin-law", "german-courts"}
        else False,
    }


def _bounded_xml(raw, max_bytes=20_000_000):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
        raise ProviderError("input_limit", "XML input is missing or oversized")
    # defusedxml permits harmless DOCTYPE declarations, but never resolves an
    # external DTD or an entity. Official court XML has a public external DTD.
    return ET.fromstring(raw, forbid_entities=True, forbid_external=True)


def _xml_sections(root):
    output = []

    def walk(node, path, paragraph_number=None):
        name = node.tag.rsplit("}", 1)[-1]
        if name == "p":
            text = " ".join("".join(node.itertext()).split())
            if text:
                output.append(
                    {
                        "text": text,
                        "locator": {
                            "kind": "xml-path",
                            "path": path,
                            "id": node.get("id"),
                            **(
                                {"paragraph_number": paragraph_number}
                                if paragraph_number
                                else {}
                            ),
                        },
                    }
                )
        counts = {}
        for child in node:
            key = child.tag
            counts[key] = counts.get(key, 0) + 1
            number = paragraph_number
            if name == "dl" and key.rsplit("}", 1)[-1] == "dd":
                label = node.find("dt")
                number = (
                    _string("".join(label.itertext())) if label is not None else None
                )
            walk(
                child, path + "/" + key.rsplit("}", 1)[-1] + f"[{counts[key]}]", number
            )

    walk(root, "/" + root.tag.rsplit("}", 1)[-1] + "[1]")
    if len(output) > 10000:
        raise ProviderError("input_limit", "too many source paragraphs")
    return output


def parse_court_download(raw):
    """Parse official XML or a bounded single-XML RII ZIP without extraction."""
    if raw.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1 or not members[0].filename.endswith(".xml"):
                raise ProviderError(
                    "unsupported_archive",
                    "court download must contain exactly one XML member",
                )
            member = members[0]
            path = PurePosixPath(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.flag_bits & 1
                or member.file_size > 10_000_000
                or member.file_size > max(1, member.compress_size) * 200
            ):
                raise ProviderError(
                    "archive_limit", "unsafe or oversized court archive"
                )
            with archive.open(member) as stream:
                raw = stream.read(10_000_001)
    root = _bounded_xml(raw, 10_000_000)
    if root.tag != "dokument":
        raise ProviderError("schema_drift", "expected official RII dokument XML")
    occurrences = {}
    for child in root:
        if child.tag not in {
            "tenor",
            "tatbestand",
            "gruende",
            "entscheidungsgruende",
            "leitsatz",
            "sonstosatz",
        }:
            occurrences.setdefault(child.tag, []).append(
                _string("".join(child.itertext()))
            )
    scalar = {key: " ".join(values) for key, values in occurrences.items()}
    identity = scalar.get("doknr")
    if not identity or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ProviderError("schema_drift", "court document has no stable doknr")
    sections = _xml_sections(root)
    if not sections:
        raise ProviderError(
            "empty_source", "court publication has no readable paragraphs"
        )
    fields = {
        "court": " ".join(
            filter(
                None,
                [
                    scalar.get("gertyp"),
                    scalar.get("gerort"),
                    scalar.get("spruchkoerper"),
                ],
            )
        ),
        "docket_number": scalar.get("aktenzeichen"),
        "ecli": scalar.get("ecli") or None,
        "decision_date": _date(scalar.get("entsch-datum")),
        "decision_type": scalar.get("doktyp"),
        "cited_norms": scalar.get("norm"),
        "prior_instances": scalar.get("vorinstanz"),
    }
    return _record(
        "german-courts",
        identity,
        scalar.get("titelzeile"),
        native={
            "fields": scalar,
            "field_occurrences": occurrences,
            "xml_sha256": hashlib.sha256(raw).hexdigest(),
        },
        source_url=f"https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-{identity}.xml",
        kind="court-decision",
        fields=fields,
        sections=sections,
        publication_date=scalar.get("entsch-datum"),
    )


def parse_court_index(
    raw, *, offset=0, limit=100, court=None, since=None, max_index_bytes=20_000_000
):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or type(max_index_bytes) is not int
        or not 1 <= max_index_bytes <= 100_000_000
    ):
        raise ValueError("bounded index window required")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_index_bytes:
        raise ProviderError("input_limit", "XML input is missing or oversized")
    # The national index is substantially larger than a page of results. Retain
    # only the selected window instead of materializing the whole XML tree and
    # another dictionary for every decision.
    selected, total, root = [], 0, None
    for event, element in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
        if root is None:
            root = element
            if root.tag != "items":
                raise ProviderError("schema_drift", "expected official RII items index")
        if event != "end" or element.tag != "item":
            continue
        row = {node.tag: _string(node.text) for node in element}
        root.clear()
        if court and court not in row.get("gericht", ""):
            continue
        if since and row.get("modified", "") < since:
            continue
        row["url"] = _official_url("german-courts", row.get("link", ""))
        if not re.fullmatch(
            r"/jportal/docs/bsjrs/jb-[A-Za-z0-9_-]+\.zip", urlsplit(row["url"]).path
        ):
            raise ProviderError("schema_drift", "unexpected court download path")
        if offset <= total < offset + limit:
            selected.append(row)
        total += 1
    return {
        "records": selected,
        "next_offset": offset + limit if offset + limit < total else None,
        "index_sha256": hashlib.sha256(raw).hexdigest(),
        "total_selected": total,
    }


def _pointer(value, pointer):
    if (
        not isinstance(pointer, str)
        or not pointer.startswith("/")
        or len(pointer) > 1000
    ):
        raise ValueError("explicit RFC6901 JSON pointer required")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def parse_registry_export(
    provider,
    raw,
    *,
    format,
    field_map,
    record_path="",
    language="de",
    source_url,
    export_schema_version,
    max_records=1000,
):
    """Import documented CTIS/DRKS public exports with an explicit field mapping.

    No official REST schema is guessed. The source format and exact column/JSON
    pointer mapping are validated and fingerprinted for each operator-selected
    export version. Unknown fields survive in the captured native record.
    """
    if (
        provider not in {"ctis", "drks"}
        or not export_schema_version
        or not 1 <= max_records <= 1000
        or not 0 < len(raw) <= 20_000_000
    ):
        raise ValueError("bounded explicit clinical registry export required")
    _official_url(provider, source_url)
    required = {
        "id",
        "title",
        "registry_status",
        "sponsor",
        "countries",
        "sites",
        "registered_at",
        "updated_at",
        "results",
        "documents",
        "identifiers",
    }
    if (
        not isinstance(field_map, dict)
        or not {"id", "title"} <= field_map.keys()
        or not field_map.keys() <= required
    ):
        raise ValueError("unsupported or incomplete registry field mapping")
    if format == "json":
        data = json.loads(raw)
        records = _pointer(data, record_path) if record_path else data
        if isinstance(records, dict):
            records = [records]
    elif format == "csv":
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or len(reader.fieldnames) != len(
            set(reader.fieldnames)
        ):
            raise ProviderError("schema_drift", "duplicate or absent CSV columns")
        if not all(name in reader.fieldnames for name in field_map.values()):
            raise ProviderError(
                "schema_drift", "configured registry export columns changed"
            )
        records = []
        for row in reader:
            if None in row or len(records) >= max_records:
                raise ProviderError("input_limit", "malformed or oversized CSV export")
            records.append(row)
    else:
        raise ValueError("supported registry export formats are JSON and CSV")
    if not isinstance(records, list) or not 1 <= len(records) <= max_records:
        raise ProviderError(
            "schema_drift", "registry export must contain a bounded record array"
        )
    mapped, seen = [], set()
    mapping_hash = digest(
        {
            "fields": field_map,
            "record_path": record_path,
            "format": format,
            "schema": export_schema_version,
        }
    )
    for index, native in enumerate(records):
        if not isinstance(native, dict):
            raise ProviderError("schema_drift", "registry rows must be objects")
        values = {
            key: _pointer(native, pointer) if format == "json" else native.get(pointer)
            for key, pointer in field_map.items()
        }
        identity = str(values.get("id") or "")
        pattern = (
            r"DRKS[0-9]{8}"
            if provider == "drks"
            else r"[0-9]{4}-[0-9]{6}-[0-9]{2}(?:-[0-9]{2})?"
        )
        if (
            not re.fullmatch(pattern, identity)
            or identity in seen
            or not values.get("title")
        ):
            raise ProviderError(
                "schema_drift", "missing/duplicate native registry identity or title"
            )
        seen.add(identity)
        fields = {
            key: values.get(key)
            for key in required - {"id", "title", "registered_at", "updated_at"}
        }
        refs = fields.get("identifiers")
        relationships = [
            {
                "relation": "explicit-cross-registry-or-publication-id",
                "identifier": ref,
                "review_required": True,
            }
            for ref in (refs if isinstance(refs, list) else [refs])
            if ref
        ]
        record = _record(
            provider,
            identity,
            values["title"],
            native=native,
            source_url=source_url,
            kind="trial-registration",
            fields=fields,
            language=language,
            relationships=relationships,
            publication_date=values.get("registered_at"),
            updated_at=values.get("updated_at"),
        )
        record.update(
            field_locators={
                key: {"format": format, "record_index": index, "field": pointer}
                for key, pointer in field_map.items()
            },
            mapping_hash=mapping_hash,
            export_schema_version=export_schema_version,
            evidence_class="registration-not-results",
        )
        mapped.append(record)
    return mapped


def parse_ctis_search_csv(raw, *, source_url, max_records=1000):
    """Parse the documented CTIS 2.2.5 public search-results CSV download."""
    mapping = {
        "id": "Trial number",
        "title": "Title of the trial",
        "registry_status": "Overall trial status",
        "sponsor": "Sponsor/Co-Sponsors",
        "updated_at": "Last updated",
    }
    records = parse_registry_export(
        "ctis",
        raw,
        format="csv",
        field_map=mapping,
        language="en",
        source_url=source_url,
        export_schema_version="ctis-public-2.2.5-search-csv",
        max_records=max_records,
    )
    for record in records:
        native = record["native"]
        if (
            not {"Location(s) and recruitment status", "Decision date", "Trial results"}
            <= native.keys()
        ):
            raise ProviderError(
                "schema_drift", "CTIS public CSV profile columns changed"
            )
        location_text = native["Location(s) and recruitment status"] or ""
        matches = list(re.finditer(r"(?:^|,\s*)([^,:]+):", location_text))
        locations = []
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(location_text)
            )
            locations.append(
                {
                    "country": match.group(1).strip(),
                    "recruitment_status": location_text[match.end() : end].strip(),
                }
            )
        if location_text and (not matches or matches[0].start() != 0):
            raise ProviderError(
                "schema_drift", "unrecognized CTIS country/status field"
            )
        availability = (native["Trial results"] or "").strip().lower()
        record["fields"].update(
            decision_date=_date(native["Decision date"]),
            countries=[value["country"] for value in locations],
            country_recruitment_statuses=locations,
            results_available={"yes": True, "no": False}.get(availability),
            results_availability_native=native["Trial results"],
            site_coverage="search CSV contains country statuses, not individual trial sites",
            document_coverage="search CSV contains availability flags, not result/protocol documents",
        )
        record["missing_fields"] = sorted(
            key
            for key, value in record["fields"].items()
            if value is None or value == "" or value == []
        )
        record["native_profile"] = "ctis-public-2.2.5-search-csv"
    return records


def parse_drks_public_json(raw, *, source_url, language="de"):
    """Import one native DRKS public JSON download with locale-aware fields."""
    if language not in {"de", "en"}:
        raise ValueError("DRKS public description language must be de or en")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 20_000_000:
        raise ProviderError("input_limit", "bounded DRKS export required")
    native = json.loads(raw)
    if not isinstance(native, dict):
        raise ProviderError("schema_drift", "one native DRKS trial object required")
    descriptions = native.get("trialDescriptions")
    if not isinstance(descriptions, list):
        raise ProviderError("schema_drift", "DRKS descriptions are missing")
    selected = [
        index
        for index, row in enumerate(descriptions)
        if isinstance(row, dict) and row.get("idLocale", {}).get("locale") == language
    ]
    if len(selected) != 1:
        raise ProviderError(
            "schema_drift", "DRKS selected locale missing or duplicated"
        )
    index = selected[0]
    mapping = {
        "id": "/drksId",
        "title": f"/trialDescriptions/{index}/title",
        "registry_status": "/recruitment/status",
        "registered_at": "/registrationDrks",
        "updated_at": "/lastUpdate",
        "sites": "/recruitment/institutes",
    }
    contacts = native.get("trialContacts", [])
    for contact_index, contact in enumerate(contacts):
        if contact.get("idContactIdType", {}).get("type") == "PRIMARY_SPONSOR":
            mapping["sponsor"] = f"/trialContacts/{contact_index}/contact/affiliation"
            break
    record = parse_registry_export(
        "drks",
        raw,
        format="json",
        field_map=mapping,
        language=language,
        source_url=source_url,
        export_schema_version="drks-public-json-2026-09",
    )[0]
    recruitment = native.get("recruitment") or {}
    results = native.get("trialResults") or {}
    summaries = [
        row.get("briefSummaryOfResultsDescription")
        for row in results.get("trialResultsDescriptions", [])
        if row.get("idLocale", {}).get("locale") == language
        and row.get("briefSummaryOfResultsDescription")
    ]
    record["fields"].update(
        countries=[
            row["idCountry"]["code"] for row in recruitment.get("countries", [])
        ],
        results=summaries or None,
        registry_record_status=native.get("trialStatus"),
        registered_at=_date(native.get("registrationDrks")),
        secondary_identifiers=native.get("secondaryIds"),
        publication_references=results.get("publications", []),
    )
    secondary = native.get("secondaryIds") or {}
    ctis_id = secondary.get("otherPrimaryRegisterId")
    if (
        str(secondary.get("otherPrimaryRegisterName", "")).upper() == "CTIS"
        and isinstance(ctis_id, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{6}-[0-9]{2}-[0-9]{2}", ctis_id)
    ):
        record["relationships"].append(
            {
                "relation": "explicit-cross-registry-or-publication-id",
                "identifier": ctis_id,
                "review_required": True,
            }
        )
    record["sections"] = [
        {
            "text": descriptions[index][key],
            "locator": {
                "kind": "json-pointer",
                "pointer": f"/trialDescriptions/{index}/{key}",
            },
        }
        for key in ("title", "summary", "scientificSummary")
        if descriptions[index].get(key)
    ]
    record["missing_fields"] = sorted(
        key
        for key, value in record["fields"].items()
        if value is None or value == "" or value == []
    )
    return [record]


def parse_drks_who_xml(raw, *, source_url, max_records=1000):
    """Import DRKS' public WHO-XML format; empty results stay missing."""
    root = _bounded_xml(raw)
    local = lambda node: node.tag.rsplit("}", 1)[-1]
    trials = [node for node in root.iter() if local(node).lower() == "trial"]
    if not trials and local(root).lower() == "trial":
        trials = [root]
    if not 1 <= len(trials) <= max_records:
        raise ProviderError(
            "schema_drift", "WHO XML trial records are absent or oversized"
        )
    output = []
    for trial in trials:
        values = {
            local(node): _string("".join(node.itertext()))
            for node in trial.iter()
            if len(node) == 0
        }
        identity = (
            values.get("trial_id") or values.get("TrialID") or values.get("primary_id")
        )
        if not identity or not re.fullmatch(r"DRKS[0-9]{8}", identity):
            raise ProviderError(
                "schema_drift", "WHO export is not an identified DRKS record"
            )
        output.append(
            _record(
                "drks",
                identity,
                values.get("public_title") or values.get("scientific_title"),
                native=values,
                source_url=source_url,
                kind="trial-registration",
                fields={
                    "registry_status": values.get("recruitment_status"),
                    "sponsor": values.get("primary_sponsor"),
                    "countries": values.get("countries"),
                    "sites": None,
                    "results": values.get("results_summary"),
                    "registration_date": values.get("date_registration"),
                },
                publication_date=values.get("date_registration"),
                updated_at=values.get("last_updated"),
            )
        )
    return output


def parse_ema(payload, *, ids=None, offset=0, limit=100):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise ValueError("bounded EMA page required")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderError(
            "schema_drift", "expected EMA website JSON meta/data envelope"
        )
    records = []
    for index, native in enumerate(payload["data"]):
        identity = native.get("ema_product_number")
        if ids is not None and identity not in ids:
            continue
        if not identity or not native.get("medicine_url"):
            raise ProviderError(
                "schema_drift", "EMA medicine native identity/URL missing"
            )
        record = _record(
            "ema",
            identity,
            native.get("name_of_medicine"),
            native=native,
            source_url=native["medicine_url"],
            kind="medicine-regulatory-metadata",
            language="en",
            fields={
                "authorisation_status": native.get("medicine_status"),
                "category": native.get("category"),
                "substance": native.get("active_substance"),
                "revision_number": native.get("revision_number"),
                "authorisation_date": _date(native.get("marketing_authorisation_date")),
                "decision_date": _date(native.get("european_commission_decision_date")),
                "atc_code": native.get("atc_code_human"),
            },
            publication_date=native.get("first_published_date"),
            updated_at=native.get("last_updated_date"),
        )
        record["native_locator"] = f"/data/{index}"
        records.append(record)
    return {
        "records": records[offset : offset + limit],
        "next_offset": offset + limit if offset + limit < len(records) else None,
        "provider_release": payload.get("meta", {}),
        "coverage_notice": COVERAGE["ema"],
    }


def parse_bfarm_feed(raw, *, limit=100):
    root = _bounded_xml(raw)
    if root.tag != "rss" or not 1 <= limit <= 1000:
        raise ProviderError("schema_drift", "expected a bounded BfArM RSS feed")
    channel = root.find("channel")
    if channel is None:
        raise ProviderError("schema_drift", "RSS channel missing")
    output, seen = [], {}
    for index, item in enumerate(channel.findall("item")[:limit]):
        values = {child.tag: _string(child.text) for child in item}
        url = _official_url("bfarm", values.get("link"))
        identity = values.get("guid") or url
        if identity in seen:
            if seen[identity] != values:
                raise ProviderError(
                    "duplicate_record", "conflicting BfArM entries share one identity"
                )
            continue
        seen[identity] = values
        title = values.get("title", "")
        # Extract only the literal heading before ':'; no claim about which
        # product caused an adverse event is created by name normalization.
        heading = re.match(
            r"^(Rote-Hand-Brief|Informationsbrief)\s+(?:zu|über)\s+([^:]+)",
            title,
            flags=re.IGNORECASE,
        )
        named = heading.group(2).strip() if heading else None
        notice_type = (
            "Rote-Hand-Brief"
            if re.search(r"\bRote-Hand-Brief\b", title, re.IGNORECASE)
            else "Informationsbrief"
            if re.search(r"\bInformationsbrief\b", title, re.IGNORECASE)
            else "unspecified"
        )
        output.append(
            _record(
                "bfarm",
                identity,
                title,
                native=values,
                source_url=url,
                kind="medicine-safety-notice",
                publication_date=values.get("pubDate"),
                fields={
                    "notice_type": notice_type,
                    "named_product_or_substance_text": named,
                    "publishing_authority": "BfArM",
                    "causality_assessed": False,
                    "feed_locator": f"/rss/channel/item[{index + 1}]",
                    "description": values.get("description"),
                },
                sections=[
                    {
                        "text": values.get("description", ""),
                        "locator": {"kind": "rss-description", "item_index": index},
                    }
                ],
            )
        )
    return {
        "records": output,
        "feed_title": channel.findtext("title"),
        "reuse_notice": channel.findtext("copyright"),
        "truncated": len(channel.findall("item")) > limit,
        "coverage_notice": COVERAGE["bfarm"],
    }


def html_paragraphs(raw):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for element in soup.select("script,style,nav,footer,header,noscript"):
        element.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    sections = []
    for index, element in enumerate(main.select("h1,h2,h3,p,li,td")):
        text = element.get_text(" ", strip=True)
        if text:
            sections.append(
                {
                    "text": text,
                    "locator": {
                        "kind": "html-selector",
                        "tag": element.name,
                        "index_in_selected_main_nodes": index,
                        "id": element.get("id"),
                        "precision": "element-selection; not exact byte offsets",
                    },
                }
            )
    if len(sections) > 10000:
        raise ProviderError("input_limit", "too many HTML paragraphs")
    return sections


def _validate_linked_format(raw, source_url):
    if urlsplit(source_url).path.lower().endswith(".pdf") and not raw.startswith(
        b"%PDF"
    ):
        raise ProviderError(
            "unexpected_document_format",
            "the selected PDF URL returned non-PDF bytes; capture retained without publishing document evidence",
        )


def _parse_captured_pdf(raw, expected_sha256, *, max_pages=250):
    """Bound all regional PDF parsing by the shared worker resource limits."""
    from src.evaluation.runtime_jobs import execute_job

    with tempfile.TemporaryDirectory(prefix="noesis-regional-pdf-") as directory:
        path = Path(directory) / "captured.pdf"
        path.write_bytes(raw)
        payload = {"path": str(path), "sha256": expected_sha256}
        if max_pages != 250:
            payload["max_pages"] = max_pages
        job = execute_job("pdf-pymupdf", payload, timeout_s=120, max_rss_bytes=1024**3)
    if job.get("status") != "completed":
        raise ProviderError(
            job.get("failure_code") or "parser_" + job.get("status", "failed"),
            "linked PDF parsing did not complete; capture retained for retry",
        )
    parsed = job.get("result", {})
    if parsed.get("original_sha256") != expected_sha256:
        raise ProviderError("source_changed", "linked PDF parser digest mismatch")
    sections = [
        {
            "text": block["text"],
            "locator": {
                "kind": "pdf-block",
                "page": block["page"],
                "bbox": block["bbox"],
            },
        }
        for block in parsed.get("locators", [])
        if block.get("text", "").strip()
    ]
    receipt = {
        "backend": "pymupdf",
        "version": parsed.get("version"),
        "configuration": parsed.get("configuration"),
    }
    return sections, receipt, job


def parse_trial_document(
    provider,
    raw,
    *,
    source_url,
    trial_id,
    document_id,
    document_kind,
    title,
    language,
):
    """Import an explicitly selected document from a public CTIS/DRKS export."""
    pattern = (
        r"DRKS[0-9]{8}"
        if provider == "drks"
        else r"[0-9]{4}-[0-9]{6}-[0-9]{2}(?:-[0-9]{2})?"
    )
    if (
        provider not in {"ctis", "drks"}
        or not isinstance(trial_id, str)
        or not re.fullmatch(pattern, trial_id)
        or not isinstance(document_id, str)
        or not 1 <= len(document_id) <= 500
        or document_kind
        not in {"registration-detail", "protocol", "results", "publication"}
        or language not in {"de", "en"}
        or not isinstance(raw, bytes)
        or not 0 < len(raw) <= 20_000_000
    ):
        raise ValueError(
            "bounded trial document and explicit native identity, role and language required"
        )
    _official_url(provider, source_url)
    _validate_linked_format(raw, source_url)
    receipt = None
    if raw.startswith(b"%PDF"):
        sections, receipt, _ = _parse_captured_pdf(raw, hashlib.sha256(raw).hexdigest())
    elif raw.lstrip().startswith(b"<"):
        sections = html_paragraphs(raw)
    else:
        raise ProviderError(
            "unsupported_format", "trial document export must be PDF or HTML"
        )
    if not sections:
        raise ProviderError(
            "unavailable_text", "trial document has no extractable text"
        )
    return _record(
        provider,
        trial_id + "/document/" + document_id,
        title,
        native={
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            **({"parser_receipt": receipt} if receipt else {}),
        },
        source_url=source_url,
        kind="trial-" + document_kind,
        language=language,
        sections=sections,
        fields={
            "trial_id": trial_id,
            "document_role": document_kind,
            "identity_origin": "operator-selected official export document",
        },
        relationships=[
            {
                "relation": "document-of",
                "provider_id": trial_id,
                "basis": "explicit operator-selected export association",
            }
        ],
    )


def parse_berlin_juris_xml(raw, *, source_url, historical=None):
    """Import the documented juris XML export without loading its external DTD."""
    from lxml import etree

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 20_000_000:
        raise ProviderError("input_limit", "bounded Berlin XML export required")
    if type(historical) not in {bool, type(None)}:
        raise ValueError("explicit historical or unknown state required")
    root = etree.fromstring(
        raw,
        parser=etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
            remove_comments=True,
            remove_pis=True,
        ),
    )
    if any(isinstance(node, etree._Entity) for node in root.iter()):
        raise ProviderError(
            "unsupported_format", "unresolved XML entities are not evidence"
        )
    if root.tag != "dokumente" or not root.get("doknr"):
        raise ProviderError("schema_drift", "native Berlin document identity required")
    norms, sections = [], []
    for norm in root.findall("norm"):
        identity = norm.get("doknr")
        metadata = norm.find("metadaten")
        body = norm.find("textdaten")
        if not identity or metadata is None or body is None:
            raise ProviderError("schema_drift", "incomplete Berlin norm export")
        values = {
            node.tag: " ".join("".join(node.itertext()).split()) for node in metadata
        }
        norms.append({"official_id": identity, "metadata": values})
        for section in _xml_sections(body):
            section["locator"]["official_norm_id"] = identity
            section["locator"]["norm_label"] = values.get("enbez")
            sections.append(section)
    if not norms or not sections or norms[0]["official_id"] != root.get("doknr"):
        raise ProviderError(
            "unavailable_text", "Berlin export has no identified legal text"
        )
    top = norms[0]["metadata"]
    kind = {"Gesetz": "law", "Verordnung": "regulation"}.get(top.get("dokumenttyp"))
    if kind is None:
        raise ProviderError(
            "unsupported_format", "unsupported Berlin XML document kind"
        )
    return [
        _record(
            "berlin-law",
            root.get("doknr"),
            top.get("titel") or top.get("langue"),
            source_url=source_url,
            kind=kind,
            sections=sections,
            native={"original_sha256": hashlib.sha256(raw).hexdigest(), "norms": norms},
            fields={
                "jurisdiction": "DE-BE",
                "historical": historical,
                "enactment_date": _date(top.get("ausfertigung-datum")),
                "effective_from": _date(top.get("gueltigab")),
                "effective_until": _date(top.get("gueltigbis")),
                "gazette_reference": top.get("fundstelle"),
                "native_norm_count": len(norms),
                "amendment_notice": " ".join(
                    root.xpath(".//table[@class='standangaben']//td/text()")
                ),
                "metadata_origin": "native-juris-XML",
            },
        )
    ]


def parse_berlin_juris_html(raw, *, source_url, official_id):
    """Select judgment passages from a captured, rendered public portal page."""
    from bs4 import BeautifulSoup

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 20_000_000:
        raise ProviderError("input_limit", "bounded Berlin HTML capture required")
    if not re.fullmatch(r"NJRE[0-9]+", official_id) or official_id not in urlsplit(
        source_url
    ).path.split("/"):
        raise ProviderError("source_identity", "court identity must match source URL")
    soup = BeautifulSoup(raw.decode("utf-8"), "html.parser")
    body = soup.select_one(".docLayoutText.doktyp-juris-r")
    if body is None:
        raise ProviderError("unavailable_text", "rendered judgment body is missing")
    metadata = {}
    for row in soup.select("table tr"):
        label, value = row.find("th"), row.find("td")
        if label and value:
            metadata[label.get_text(" ", strip=True).rstrip(":")] = value.get_text(
                " ", strip=True
            )
    roles = {
        "Leitsatz": "official-headnote",
        "Tenor": "disposition",
        "Gründe": "reasons",
        "Tatbestand": "facts",
        "Entscheidungsgründe": "reasons",
        "Abweichende Meinung": "dissent",
    }
    sections, editorial, role = [], [], None
    for index, node in enumerate(body.find_all(["h3", "h4", "p"])):
        text = node.get_text(" ", strip=True)
        if node.name in {"h3", "h4"}:
            role = roles.get(text)
            continue
        if not text:
            continue
        if role is None:
            editorial.append(text)
            continue
        parent = node.find_parent("dl", class_="RspDL")
        number = (
            parent.find("dt").get_text(" ", strip=True)
            if parent and parent.find("dt")
            else None
        )
        match = re.search(r"([0-9]+)$", number or "")
        sections.append(
            {
                "text": text,
                "locator": {
                    "kind": "rendered-html-paragraph",
                    "container": ".docLayoutText.doktyp-juris-r",
                    "index_in_headings_and_paragraphs": index,
                    "paragraph_number": match.group(1) if match else None,
                    "section_role": role,
                },
            }
        )
    if (
        not sections
        or not metadata.get("Gericht")
        or not metadata.get("Entscheidungsdatum")
    ):
        raise ProviderError(
            "schema_drift", "judgment passages and native metadata required"
        )
    return [
        _record(
            "berlin-law",
            official_id,
            metadata.get("Gericht", "") + " " + metadata.get("Aktenzeichen", ""),
            source_url=source_url,
            kind="court-decision",
            sections=sections,
            native={
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "capture_format": "rendered-portal-HTML",
                "metadata": metadata,
                "editorial_or_unclassified_text": editorial,
            },
            fields={
                "court": metadata.get("Gericht"),
                "case_number": metadata.get("Aktenzeichen"),
                "ecli": metadata.get("ECLI"),
                "decision_date": _date(metadata.get("Entscheidungsdatum")),
                "jurisdiction": "DE-BE",
                "historical": None,
                "editorial_text_excluded_from_evidence": True,
            },
        )
    ]


def parse_berlin_publication(
    raw,
    *,
    source_url,
    official_id,
    kind,
    title,
    publication_date=None,
    effective_from=None,
    historical=None,
    amendments=(),
):
    if kind not in {"law", "regulation", "court-decision", "gazette"} or type(
        historical
    ) not in {bool, type(None)}:
        raise ValueError(
            "distinct Berlin record kind and explicit historical/unknown state required"
        )
    if (
        not 0 < len(raw) <= 20_000_000
        or not official_id
        or not isinstance(amendments, (list, tuple))
    ):
        raise ValueError(
            "bounded official publication with stable official identity required"
        )
    _official_url("berlin-law", source_url)
    _validate_linked_format(raw, source_url)
    parser_receipt = None
    if raw.startswith(b"%PDF"):
        sections, parser_receipt, _ = _parse_captured_pdf(
            raw, hashlib.sha256(raw).hexdigest(), max_pages=100
        )
    else:
        sections = html_paragraphs(raw)
    if not sections:
        raise ProviderError(
            "unavailable_text",
            "official source has no extractable text; OCR is a separate optional stage",
        )
    return _record(
        "berlin-law",
        official_id,
        title,
        native={
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            **({"parser_receipt": parser_receipt} if parser_receipt else {}),
        },
        source_url=source_url,
        kind=kind,
        sections=sections,
        publication_date=publication_date,
        fields={
            "jurisdiction": "DE-BE",
            "effective_from": _date(effective_from),
            "historical": historical,
            "metadata_origin": "explicit operator-supplied official publication identifiers",
        },
        relationships=list(amendments),
    )


def cellar_query(celex_ids, *, languages=("DEU", "ENG"), offset=0, limit=100):
    """The bounded CELLAR SPARQL query for explicit CELEX selections."""
    if not 1 <= len(celex_ids) <= 20 or any(
        not re.fullmatch(r"[0-9A-Z()._-]{5,50}", value) for value in celex_ids
    ):
        raise ValueError("bounded validated CELEX selections required")
    if (
        not 1 <= len(languages) <= 24
        or any(not re.fullmatch(r"[A-Z]{3}", language) for language in languages)
        or not 0 <= offset <= 100000
        or not 1 <= limit <= 1000
    ):
        raise ValueError("bounded CELLAR language/window selection required")
    # CELLAR stores CELEX as xsd:string; its endpoint distinguishes these
    # from untyped VALUES literals when matching this predicate.
    values = " ".join(
        json.dumps(value) + "^^<http://www.w3.org/2001/XMLSchema#string>"
        for value in celex_ids
    )
    langs = ",".join(
        "<http://publications.europa.eu/resource/authority/language/" + value + ">"
        for value in languages
    )
    query = (
        """PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?work ?celex ?eli ?ecli ?expression ?language ?title ?manifestation ?format ?item ?document_date ?journal ?published ?effective ?predicate ?related
WHERE {
 VALUES ?celex { %s }
 ?work cdm:resource_legal_id_celex ?celex .
 OPTIONAL { ?work cdm:resource_legal_eli ?eli }
 OPTIONAL { ?work cdm:case-law_ecli ?ecli }
 ?expression cdm:expression_belongs_to_work ?work ; cdm:expression_uses_language ?language .
 FILTER (?language IN (%s))
 OPTIONAL { ?expression cdm:expression_title ?title }
 OPTIONAL { ?manifestation cdm:manifestation_manifests_expression ?expression ; cdm:manifestation_type ?format .
            OPTIONAL { ?item cdm:item_belongs_to_manifestation ?manifestation } }
 OPTIONAL { ?work cdm:work_date_document ?document_date }
 OPTIONAL { ?work cdm:resource_legal_published_in_official-journal ?journal .
            ?journal cdm:work_date_document ?published }
 OPTIONAL { ?work cdm:resource_legal_date_entry-into-force ?effective }
 OPTIONAL { ?work ?predicate ?related . FILTER (?predicate IN (
   cdm:work_cites_work, cdm:resource_legal_amends_resource_legal, cdm:resource_legal_corrects_resource_legal)) }
}
ORDER BY ?work ?expression ?manifestation ?item ?predicate ?related ?eli ?ecli ?document_date ?journal ?published ?effective ?title ?format
LIMIT %d OFFSET %d""".replace("%s", values, 1)
        .replace("%s", langs, 1)
        .replace("%d", str(limit), 1)
        .replace("%d", str(offset), 1)
    )
    return query


def parse_cellar_results(payload, *, celex_ids, languages, offset=0, limit=100):
    """Group CELLAR SPARQL bindings into work/expression/manifestation records."""
    rows = (payload.get("results", {}) if isinstance(payload, dict) else {}).get("bindings")
    if not isinstance(rows, list) or len(rows) > limit:
        raise ProviderError("schema_drift", "invalid CELLAR SPARQL result envelope")
    grouped = {}
    for binding in rows:
        native = {
            key: value.get("value")
            for key, value in binding.items()
            if isinstance(value, dict)
        }
        if (
            native.get("celex") not in celex_ids
            or not native.get("work")
            or not native.get("expression")
            or str(native.get("language", "")).rsplit("/", 1)[-1] not in languages
        ):
            raise ProviderError(
                "source_identity",
                "CELLAR row does not match requested work/expression",
            )
        identity = (
            native.get("item")
            or native.get("manifestation")
            or native["expression"]
        )
        key = (identity, native.get("language"))
        relationship = (
            {
                "relation": native["predicate"],
                "target": native.get("related"),
                "basis": "explicit-CDM-triple",
            }
            if native.get("predicate")
            else None
        )
        if key in grouped:
            if relationship and relationship not in grouped[key]["relationships"]:
                grouped[key]["relationships"].append(relationship)
            grouped[key]["native"]["bindings"].append(binding)
            for field in ("eli", "ecli"):
                if (
                    native.get(field)
                    and native[field]
                    not in grouped[key]["fields"][field + "_identifiers"]
                ):
                    grouped[key]["fields"][field + "_identifiers"].append(
                        native[field]
                    )
            continue
        language_code = {"DEU": "de", "ENG": "en"}.get(
            str(native.get("language", "")).rsplit("/", 1)[-1], "und"
        )
        grouped[key] = _record(
            "cellar",
            identity,
            native.get("title") or native["celex"],
            native={"bindings": [binding]},
            source_url=identity,
            kind="legal-expression-manifestation",
            language=language_code,
            publication_date=native.get("published"),
            fields={
                "work": native["work"],
                "celex": native["celex"],
                "expression": native["expression"],
                "manifestation": native.get("manifestation"),
                "item": native.get("item"),
                "format": native.get("format"),
                "language_identity": native.get("language"),
                "effective_from": native.get("effective"),
                "eli": native.get("eli"),
                "ecli": native.get("ecli"),
                "eli_identifiers": [native["eli"]] if native.get("eli") else [],
                "ecli_identifiers": [native["ecli"]] if native.get("ecli") else [],
                "relationships_coverage": "bounded-query-page",
                "selection_offset": offset,
                "selection_limit": limit,
                "current_law_verified": False,
            },
            relationships=[relationship] if relationship else [],
        )
    for record in grouped.values():
        bindings = record["native"]["bindings"]
        for native_key, field in (
            ("effective", "effective_dates"),
            ("published", "publication_dates"),
            ("document_date", "document_dates"),
        ):
            record["fields"][field] = sorted(
                {
                    binding[native_key]["value"]
                    for binding in bindings
                    if binding.get(native_key, {}).get("value")
                }
            )
        effective = record["fields"]["effective_dates"]
        published = record["fields"]["publication_dates"]
        record["fields"]["effective_from"] = (
            effective[0] if len(effective) == 1 else None
        )
        record["published_at"] = (
            _date(published[0]) if len(published) == 1 else None
        )
        record["missing_fields"] = sorted(
            key
            for key, value in record["fields"].items()
            if value is None or value == "" or value == []
        )
    return {
        "records": list(grouped.values()),
        "next_offset": offset + limit if len(rows) == limit else None,
        "coverage": "bounded-selection",
    }


class RegionalEvidenceStore:
    """Store native records through the existing DocumentStore/revision pipeline."""

    def __init__(self, conn):
        self.conn = conn
        self.store, self.snapshots = DocumentStore(conn), SnapshotStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS regional_observation_receipts(observation_id TEXT PRIMARY KEY,namespace TEXT,provider TEXT,document_ids_json TEXT,receipt_json TEXT)"
        )

    @staticmethod
    def authorize(namespace, principal_id, scopes):
        if (
            not principal_id
            or "operator" not in scopes
            and (
                "knowledge:ingestion:execute" not in scopes
                or f"namespace:{namespace}:write" not in scopes
            )
        ):
            raise ProviderError(
                "unauthorized",
                "current ingestion and namespace-write authorization required",
            )

    def ingest(
        self,
        records,
        captured,
        *,
        namespace,
        principal_id,
        scopes,
        reuse_notice,
        manage_transaction=True,
    ):
        self.authorize(namespace, principal_id, scopes)
        if (
            not reuse_notice
            or not isinstance(records, list)
            or not 1 <= len(records) <= 1000
        ):
            raise ValueError("bounded records and explicit reuse notice required")
        if hashlib.sha256(captured.content).hexdigest() != captured.receipt.get(
            "digest"
        ):
            raise ProviderError("source_changed", "native capture digest mismatch")
        observation_id = (
            "regional-observation:"
            + digest([namespace, captured.receipt["digest"], records, reuse_notice])[
                :32
            ]
        )
        previous = self.conn.execute(
            "SELECT receipt_json FROM regional_observation_receipts WHERE observation_id=? AND namespace=?",
            [observation_id, namespace],
        ).fetchone()
        if previous:
            # Replaying an older capture must never restore it as the current
            # document after a later observation has committed.
            return {**json.loads(previous[0]), "replayed": True}
        seen, documents = set(), []
        for record in records:
            if record.get("contract") != SCHEMA_VERSION:
                raise ProviderError(
                    "schema_drift", "unsupported regional record contract"
                )
            provider = record["provider"]
            url = _official_url(provider, record["source_url"])
            # Work, expression and manifestation are deliberately not collapsed.
            identity = digest(
                [
                    namespace,
                    provider,
                    record["provider_id"],
                    record["language"],
                    record["kind"],
                ]
            )
            document_id = "regional:" + identity[:32]
            if document_id in seen:
                raise ProviderError(
                    "duplicate_record",
                    "one acquisition batch has duplicate native identities",
                )
            seen.add(document_id)
            content = "\n\n".join(
                part["text"] for part in record["sections"] if part.get("text")
            ) or canonical(record["native"])
            fields = {
                key: value
                for key, value in record.items()
                if key not in {"sections", "native"}
            }
            documents.append(
                Document(
                    document_id=document_id,
                    source_type="web",
                    source_id=provider + ":" + record["provider_id"],
                    language=record["language"],
                    ingested_at=captured.receipt["observed_at_ms"],
                    url=url,
                    title=record["title"],
                    content=content,
                    metadata={
                        "regional_contract": SCHEMA_VERSION,
                        "namespace": namespace,
                        "provider_record_json": canonical(fields),
                        "native_fields_json": canonical(record["native"]),
                        "source_locators_json": canonical(record["sections"]),
                        "source_license": reuse_notice,
                        "native_capture_sha256": captured.receipt["digest"],
                        "content_coverage": "captured-text"
                        if record["sections"]
                        else "metadata-only",
                        "review_required": record.get("review_required", False),
                        "registry_status_is_source_lifecycle": False,
                    },
                )
            )
        if manage_transaction:
            self.conn.execute("BEGIN")
        try:
            outcome = self.store.upsert(documents)
            if outcome.invalid:
                raise ProviderError(
                    "document_validation",
                    "native evidence failed existing document validation",
                )
            receipt = {
                "observation_id": observation_id,
                "native_capture": captured.receipt,
                "document_ids": [doc.document_id for doc in documents],
                "record_versions": [digest(record) for record in records],
                "source_refs": [
                    {
                        "document_id": change["document_id"],
                        "revision_id": change["revision_id"],
                    }
                    for change in outcome.changes
                ],
                "reuse_notice": reuse_notice,
                "no_automatic_identity_merge": True,
            }
            self.conn.execute(
                "INSERT INTO regional_observation_receipts VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    observation_id,
                    namespace,
                    records[0]["provider"],
                    canonical(receipt["document_ids"]),
                    canonical(receipt),
                ],
            )
            if manage_transaction:
                self.conn.execute("COMMIT")
        except Exception:
            if manage_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return receipt

    def import_bytes(
        self,
        provider,
        raw,
        records,
        *,
        source_url,
        namespace,
        principal_id,
        scopes,
        reuse_notice,
        observed_at_ms=None,
        manage_transaction=True,
    ):
        self.authorize(namespace, principal_id, scopes)
        if (
            provider not in PROVIDER_HOSTS
            or not 0 < len(raw) <= 20_000_000
            or any(r["provider"] != provider for r in records)
        ):
            raise ValueError("bounded provider-specific import required")
        url = _official_url(provider, source_url)
        stamp = int(time.time() * 1000) if observed_at_ms is None else observed_at_ms
        if type(stamp) is not int or stamp < 0:
            raise ValueError(
                "observation time must be an explicit nonnegative millisecond timestamp"
            )
        snapshot = self.snapshots.snapshot_bytes(
            url, raw, stamp, content_type="application/octet-stream", final_url=url
        )
        return self.ingest(
            records,
            CapturedResponse(
                raw,
                {
                    "digest": snapshot["digest"],
                    "snapshot": snapshot,
                    "observed_at_ms": stamp,
                    "execution": "explicit-public-export-import",
                },
            ),
            namespace=namespace,
            principal_id=principal_id,
            scopes=scopes,
            reuse_notice=reuse_notice,
            manage_transaction=manage_transaction,
        )


class RegionalClient:
    COURT_INDEX = "https://www.rechtsprechung-im-internet.de/rii-toc.xml"
    EMA_MEDICINES = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
    EMA_DOCUMENTS = "https://www.ema.europa.eu/en/documents/report/documents-output-epar_documents_json-report_en.json"
    BFARM_FEED = "https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/DE/Pharmakovigilanz/Rote-Hand-Briefe/RSSNewsfeed.xml?nn=591002"
    BERLIN_GAZETTE = "https://www.berlin.de/sen/justiz/service/gesetze-und-verordnungen/artikel.261829.php"
    CELLAR_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"

    def __init__(
        self,
        http: DurableHTTP,
        *,
        principal_id,
        credential=None,
        per_request_cost_micros=0,
    ):
        if (
            http.provider not in PROVIDER_HOSTS
            or not http.hosts <= PROVIDER_HOSTS[http.provider]
        ):
            raise ValueError(
                "native client requires an exact provider-specific host policy"
            )
        if http.provider == "opencorporates" and not credential:
            raise ProviderError(
                "credential_unavailable",
                "this provider requires an explicitly supplied credential",
            )
        if type(per_request_cost_micros) is not int or per_request_cost_micros < 0:
            raise ValueError("explicit request cost ceiling required")
        self.http, self.principal_id, self.credential, self.price = (
            http,
            principal_id,
            credential,
            per_request_cost_micros,
        )

    def _fetch(self, key, url, **kwargs):
        return self.http.request(
            key,
            url,
            principal_id=self.principal_id,
            max_cost_micros=self.price,
            **kwargs,
        )

    def court_index(
        self,
        observation,
        *,
        offset=0,
        limit=100,
        court=None,
        since=None,
        max_index_bytes=20_000_000,
    ):
        self._provider("german-courts")
        if type(max_index_bytes) is not int or not 1 <= max_index_bytes <= 100_000_000:
            raise ValueError("court index byte ceiling must be from one to 100 MB")
        response = self._fetch(
            observation + ":index",
            self.COURT_INDEX,
            max_bytes=max_index_bytes,
            timeout_s=60,
        )
        return parse_court_index(
            response.content,
            offset=offset,
            limit=limit,
            court=court,
            since=since,
            max_index_bytes=max_index_bytes,
        ), response

    def court_decision(self, identity, observation):
        self._provider("german-courts")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
            raise ValueError("invalid official court document ID")
        response = self._fetch(
            observation + ":" + identity,
            f"https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-{identity}.zip",
            max_bytes=10_000_000,
        )
        record = parse_court_download(response.content)
        if record["provider_id"] != identity:
            raise ProviderError(
                "source_identity", "court response returned another decision"
            )
        return [record], response

    def ema_medicines(self, observation, *, ids=None, offset=0, limit=100):
        self._provider("ema")
        response = self._fetch(
            observation + ":medicines", self.EMA_MEDICINES, max_bytes=15_000_000
        )
        return parse_ema(response.json(), ids=ids, offset=offset, limit=limit), response

    def ema_documents(
        self, observation, medicine_id, *, limit=100, max_index_bytes=20_000_000
    ):
        self._provider("ema")
        if not medicine_id or not 1 <= limit <= 1000:
            raise ValueError("bounded medicine document selection required")
        if type(max_index_bytes) is not int or not 1 <= max_index_bytes <= 100_000_000:
            raise ValueError(
                "EMA document index byte ceiling must be from one to 100 MB"
            )
        response = self._fetch(
            observation + ":documents",
            self.EMA_DOCUMENTS,
            max_bytes=max_index_bytes,
            timeout_s=60,
        )
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("schema_drift", "expected EMA document meta/data JSON")
        documents = []
        for native in payload["data"]:
            if native.get("ema_product_number") != medicine_id:
                continue
            url = _official_url("ema", native.get("document_url"))
            translations = native.get("translations", {})
            documents.append(
                {
                    "url": url,
                    "language": "en",
                    "medicine_id": medicine_id,
                    "title": native.get("name"),
                    "publication_date": _date(native.get("first_published_date")),
                    "updated_at": _date(native.get("last_updated_date")),
                    "native": native,
                }
            )
            if isinstance(translations, dict):
                for language, translated in translations.items():
                    documents.append(
                        {
                            "url": _official_url("ema", translated),
                            "language": language,
                            "medicine_id": medicine_id,
                            "translation_of": url,
                            "native": native,
                        }
                    )
        return {
            "documents": documents[:limit],
            "truncated": len(documents) > limit,
        }, response

    def bfarm_notices(self, observation, *, limit=100):
        self._provider("bfarm")
        response = self._fetch(
            observation + ":feed", self.BFARM_FEED, max_bytes=2_000_000
        )
        return parse_bfarm_feed(response.content, limit=limit), response

    def linked_documents(self, source_url, observation, *, limit=10):
        """Acquire a publisher page, discover bounded official-host document links."""
        from bs4 import BeautifulSoup

        if not 1 <= limit <= 100:
            raise ValueError("document selection limit exceeded")
        url = _official_url(self.http.provider, source_url)
        captured = self._fetch(observation + ":page", url, max_bytes=2_000_000)
        soup = BeautifulSoup(captured.content, "html.parser")
        links, seen = [], set()
        for node in soup.select("a[href]"):
            value = urljoin(url, node["href"])
            if (
                not urlsplit(value).path.lower().endswith((".pdf", ".xml", ".html"))
                or value in seen
            ):
                continue
            if urlsplit(value).hostname not in self.http.hosts:
                continue
            if (
                ".pdf" not in urlsplit(value).path.lower()
                and self.http.provider != "cellar"
            ):
                continue
            seen.add(value)
            links.append(
                {
                    "url": _official_url(self.http.provider, value),
                    "title": node.get_text(" ", strip=True),
                    "link_locator": {"href": node["href"]},
                    "origin_page": url,
                }
            )
        return {
            "documents": links[:limit],
            "truncated": len(links) > limit,
            "page_sections": html_paragraphs(captured.content),
        }, captured

    def acquire_document(
        self,
        source_url,
        observation,
        *,
        language,
        parent_id,
        kind="linked-regulatory-document",
    ):
        url = _official_url(self.http.provider, source_url)
        captured = self._fetch(
            observation + ":document",
            url,
            headers={
                "Accept": "application/pdf, text/html, application/xhtml+xml, application/xml;q=0.9, */*;q=0.1"
            },
            max_bytes=20_000_000,
        )
        raw = captured.content
        _validate_linked_format(raw, url)
        sections = []
        parser_receipt = None
        if raw.startswith(b"%PDF"):
            sections, parser_receipt, job = _parse_captured_pdf(
                raw, captured.receipt["digest"]
            )
            # Timing belongs to the observation, not versioned source content.
            captured = CapturedResponse(
                raw,
                {
                    **captured.receipt,
                    "parser_runtime": {
                        key: value for key, value in job.items() if key != "result"
                    },
                },
            )
        elif raw.lstrip().startswith(b"<"):
            sections = html_paragraphs(raw)
        else:
            raise ProviderError(
                "unsupported_format", "linked document is not PDF/HTML/XML"
            )
        if not sections:
            raise ProviderError(
                "unavailable_text",
                "captured document requires a separately configured OCR/parser",
            )
        record = _record(
            self.http.provider,
            url,
            parent_id,
            native={
                "original_sha256": captured.receipt["digest"],
                **({"parser_receipt": parser_receipt} if parser_receipt else {}),
            },
            source_url=url,
            kind=kind,
            language=language,
            sections=sections,
            relationships=[
                {
                    "relation": "document-of",
                    "provider_id": parent_id,
                    "basis": "explicit source link",
                }
            ],
        )
        return [record], captured

    def company(self, jurisdiction, number, observation):
        self._provider("opencorporates")
        if not re.fullmatch(
            r"[a-z]{2}(?:_[a-z0-9]+)?", jurisdiction
        ) or not re.fullmatch(r"[A-Za-z0-9 ._-]{1,100}", number):
            raise ValueError("native registry jurisdiction and number required")
        response = self._fetch(
            observation + ":company",
            "https://api.opencorporates.com/v0.4/companies/"
            + jurisdiction
            + "/"
            + quote(number, safe=""),
            secret_params={"api_token": self.credential},
        )
        native = response.json().get("results", {}).get("company")
        record = self._company_record(native)
        if (
            record["fields"]["jurisdiction"] != jurisdiction
            or str(record["fields"]["registry_number"]) != number
        ):
            raise ProviderError(
                "source_identity", "company lookup returned another registry identity"
            )
        return [record], response

    def companies(self, query, observation, *, jurisdiction="de", page=1, limit=20):
        self._provider("opencorporates")
        if (
            not query
            or len(query) > 1000
            or not re.fullmatch(r"[a-z]{2}(?:_[a-z0-9]+)?", jurisdiction)
            or not 1 <= page <= 100
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid company search bounds")
        response = self._fetch(
            observation + f":companies:{page}",
            "https://api.opencorporates.com/v0.4/companies/search",
            params={
                "q": query,
                "jurisdiction_code": jurisdiction,
                "page": page,
                "per_page": limit,
            },
            secret_params={"api_token": self.credential},
        )
        native = response.json().get("results", {})
        rows = native.get("companies")
        if not isinstance(rows, list) or len(rows) > limit:
            raise ProviderError(
                "schema_drift", "invalid native company search response"
            )
        return {
            "records": [self._company_record(row.get("company")) for row in rows],
            "next_page": page + 1
            if page < int(native.get("total_pages", page))
            else None,
        }, response

    @staticmethod
    def _company_record(native):
        if (
            not isinstance(native, dict)
            or not native.get("jurisdiction_code")
            or not native.get("company_number")
        ):
            raise ProviderError("schema_drift", "native company identity missing")
        identity = native["jurisdiction_code"] + ":" + str(native["company_number"])
        return _record(
            "opencorporates",
            identity,
            native.get("name"),
            native=native,
            source_url=native.get("opencorporates_url")
            or f"https://opencorporates.com/companies/{native['jurisdiction_code']}/{quote(str(native['company_number']), safe='')}",
            kind="company-enrichment",
            language="en",
            updated_at=native.get("updated_at"),
            fields={
                "jurisdiction": native["jurisdiction_code"],
                "registry_number": native["company_number"],
                "registered_address": native.get("registered_address"),
                "registry_url": native.get("registry_url"),
                "inactive": native.get("inactive"),
                "company_type": native.get("company_type"),
                "previous_names": native.get("previous_names"),
                "source": native.get("source"),
            },
        )

    def sanctions_dataset(
        self,
        names,
        observation,
        *,
        artifact_version,
        dataset="eu_fsf",
        previous_entity_ids=(),
        limit=20,
    ):
        """Select review candidates locally from the documented public EU bulk file."""
        self._provider("opensanctions")
        if (
            dataset != "eu_fsf"
            or not isinstance(artifact_version, str)
            or not re.fullmatch(r"[0-9]{14}-[A-Za-z0-9_-]{1,40}", artifact_version)
            or not isinstance(names, list)
            or not 1 <= len(names) <= 20
            or any(
                not isinstance(name, str) or not name.strip() or len(name) > 300
                for name in names
            )
            or not isinstance(previous_entity_ids, (list, tuple))
            or len(previous_entity_ids) > 100
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
                for value in previous_entity_ids
            )
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError(
                "bounded EU dataset names, prior identities and selection limit required"
            )
        response = self._fetch(
            observation + ":bulk",
            "https://data.opensanctions.org/artifacts/eu_fsf/"
            + artifact_version
            + "/entities.ftm.json",
            headers={"Accept": "application/x-ndjson, application/json"},
            max_bytes=20_000_000,
            timeout_s=60,
        )
        normalize = lambda value: " ".join(value.casefold().split())
        requested = {normalize(name) for name in names}
        selected, seen = [], set()
        matched_count = 0
        for line in response.content.splitlines():
            if not line.strip():
                continue
            native = json.loads(line)
            identity = native.get("id") if isinstance(native, dict) else None
            properties = (
                native.get("properties", {}) if isinstance(native, dict) else None
            )
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", identity)
                or identity in seen
                or len(seen) >= 100000
                or not isinstance(properties, dict)
            ):
                raise ProviderError("schema_drift", "invalid or duplicate bulk entity")
            seen.add(identity)
            name_values, alias_values = (
                properties.get("name", []),
                properties.get("alias", []),
            )
            if any(
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                for values in (name_values, alias_values)
            ):
                raise ProviderError(
                    "schema_drift", "bulk entity names must be string arrays"
                )
            aliases = name_values + alias_values
            matches = bool(
                requested.intersection(normalize(value) for value in aliases)
            )
            if matches:
                matched_count += 1
            if (matches and matched_count <= limit) or identity in previous_entity_ids:
                selected.append(native)
        if not seen:
            raise ProviderError(
                "schema_drift", "empty bulk capture cannot establish removals"
            )
        missing = sorted(set(previous_entity_ids) - seen)
        records = []
        for native in selected + [
            {"id": identity, "properties": {}, "derived_absence": True}
            for identity in missing
        ]:
            identity = native["id"]
            absent = identity in missing
            records.append(
                _record(
                    "opensanctions",
                    dataset + ":" + identity,
                    native.get("caption") or identity,
                    native=native,
                    source_url="https://www.opensanctions.org/entities/"
                    + quote(identity, safe="")
                    + "/",
                    kind="potential-entity-match",
                    language="en",
                    updated_at=native.get("last_change"),
                    fields={
                        "query_id": "bulk:" + dataset,
                        "entity_id": identity,
                        "originating_lists": native.get("datasets", [dataset]),
                        "selected_dataset": dataset,
                        "dataset_jurisdiction": "EU",
                        "artifact_version": artifact_version,
                        "dataset_presence": "absent-from-complete-capture"
                        if absent
                        else "present",
                        "properties": native.get("properties", {}),
                        "schema": native.get("schema"),
                        "first_seen": native.get("first_seen"),
                        "last_seen": native.get("last_seen"),
                        "target": native.get("target"),
                        "provider_score": None,
                        "score_semantics": "bulk file has no provider matching score; local exact casefolded name/alias selection is not identity evidence",
                        "automatic_merge": False,
                        "absence_is_not_global_delisting": True,
                    },
                )
            )
        return {
            "records": records,
            "review_required": True,
            "scanned_entities": len(seen),
            "matched_entities": matched_count,
            "selection_truncated": matched_count > limit,
            "missing_from_selected_dataset": missing,
            "automatic_merge": False,
        }, response

    def sanctions_match(
        self, queries, observation, *, dataset="default", limit=5, threshold=0.7
    ):
        self._provider("opensanctions")
        if not self.credential:
            raise ProviderError(
                "credential_unavailable",
                "hosted matching requires an explicit API credential",
            )
        if (
            not re.fullmatch(r"[a-z0-9_]+", dataset)
            or not isinstance(queries, dict)
            or not 1 <= len(queries) <= 20
            or not 1 <= limit <= 100
            or not 0 <= threshold <= 1
        ):
            raise ValueError("invalid sanctions query bounds")
        for key, query in queries.items():
            if (
                not key
                or query.get("schema") not in {"Person", "Company", "Organization"}
                or not isinstance(query.get("properties"), dict)
            ):
                raise ValueError("explicit native entity schema/properties required")
            if len(canonical(query)) > 16000:
                raise ValueError("sanctions query input limit")
        response = self._fetch(
            observation + ":matches",
            "https://api.opensanctions.org/match/" + dataset,
            method="POST",
            params={"limit": limit, "threshold": threshold},
            body={"queries": queries},
            secret_headers={"Authorization": "ApiKey " + self.credential},
        )
        responses = response.json().get("responses")
        if not isinstance(responses, dict) or set(responses) != set(queries):
            raise ProviderError(
                "schema_drift", "sanctions results do not align to all query IDs"
            )
        records = []
        for query_id, result in responses.items():
            matches = result.get("results")
            if not isinstance(matches, list) or len(matches) > limit:
                raise ProviderError("schema_drift", "invalid sanctions result limit")
            for native in matches:
                identity = native.get("id")
                if not identity or not re.fullmatch(r"[A-Za-z0-9_.:-]+", identity):
                    raise ProviderError(
                        "schema_drift", "native sanctions identity missing"
                    )
                properties = native.get("properties", {})
                record = _record(
                    "opensanctions",
                    query_id + ":" + identity,
                    native.get("caption") or identity,
                    native=native,
                    source_url="https://www.opensanctions.org/entities/"
                    + quote(identity, safe="")
                    + "/",
                    kind="potential-entity-match",
                    language="en",
                    fields={
                        "query_id": query_id,
                        "entity_id": identity,
                        "schema": native.get("schema"),
                        "originating_lists": native.get("datasets"),
                        "provider_score": native.get("score"),
                        "score_semantics": "provider matching score, not probability of identity or misconduct",
                        "properties": properties,
                        "first_seen": native.get("first_seen"),
                        "last_seen": native.get("last_seen"),
                        "target": native.get("target"),
                        "automatic_merge": False,
                    },
                    updated_at=native.get("last_change"),
                )
                records.append(record)
        return {
            "records": records,
            "responses": {
                k: {"result_count": len(v["results"])} for k, v in responses.items()
            },
            "review_required": True,
            "automatic_merge": False,
        }, response

    def cellar(
        self, celex_ids, observation, *, languages=("DEU", "ENG"), offset=0, limit=100
    ):
        self._provider("cellar")
        query = cellar_query(celex_ids, languages=languages, offset=offset, limit=limit)
        response = self._fetch(
            observation + f":cellar:{offset}",
            self.CELLAR_SPARQL,
            params={"query": query, "format": "application/sparql-results+json"},
            headers={"Accept": "application/sparql-results+json"},
            max_bytes=5_000_000,
        )
        result = parse_cellar_results(
            response.json(), celex_ids=celex_ids, languages=languages, offset=offset, limit=limit
        )
        return {**result, "query_sha256": digest(query)}, response

    def _provider(self, expected):
        if self.http.provider != expected:
            raise ProviderError(
                "provider_mismatch",
                "operation does not belong to the configured provider",
            )

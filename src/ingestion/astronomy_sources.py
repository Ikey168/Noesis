"""Astronomy and Space acquisition for the Astronomy pack (#2149, AS03-AS06).

One native connector, ``astronomy``, fetches the declared documents of a
source one page at a time on the runtime's same-host transport and parses them
into ``noesis-astronomy-record-v1`` records (:mod:`src.kb.astronomy_records`),
projected by :class:`src.kb.astronomy_store.AstronomyProjector`. Documented
shapes (AS01, ``docs/development/astronomy-evidence/source-audit.md``):

* ``mpc-identifier-json`` - the MPC designation identifier API: a small body's
  primary designation, number, name and the secondary designations the MPC
  identifies with it (each an ``identification``);
* ``mpcorb-dat`` - MPCORB fixed-width orbit lines (reference, epoch, elements,
  U, observations, oppositions, arc, computer, last observation);
* ``jpl-sbdb-json`` - the JPL SBDB API object and orbit (``orbit_id``,
  ``soln_date``, epoch, elements with units, arc, condition code);
* ``jpl-sentry-json`` - the JPL Sentry API object summary or its removal, quoted;
* ``exoplanet-tap-csv`` - NASA Exoplanet Archive TAP sync CSV for ``ps``,
  ``pscomppars``, ``toi`` or ``cumulative`` through declared columns;
* ``exoplanet-removed-csv`` - the archive's removed/retracted planets listing;
* ``gcat-launch-tsv``, ``gcat-satcat-tsv``, ``gcat-orgs-tsv`` - GCAT tables;
* ``celestrak-satcat-csv`` - CelesTrak SATCAT;
* ``swpc-alerts-json`` - NOAA SWPC ``products/alerts.json``.

Bounded coverage (AS01): only the declared objects, hosts, launch years or
organisation codes are emitted; every other row is counted in the page receipt
(``out_of_scope``) and never dropped silently. A row that cannot be parsed
becomes a ``rejection`` record, which the runtime quarantines. Rows never
inherit values from the row before them. A complete listing is declared per
document; a listing with a rejected row whose identifier is unknown is never
treated as complete. SWPC's feed is a rolling window and never complete.

Parsers never compute: orbit elements, figures and codes are the source's
text; no TLE/GP data is read; SWPC messages are kept verbatim and only their
structured header fields (message code, serial number, issue time, NOAA scale,
cancel/extension serial numbers) are read. ``PROVIDER_CONTRACTS`` records the
AS01 access decisions; every implemented source is ``unverified-live`` until a
dated live run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.kb.astronomy_records import (
    CONTRACT,
    PROVIDERS,
    AstronomyError,
    decimal_text,
    designation_key,
    iso_day,
    iso_time,
    jd_of_day,
    jd_text,
    normalize_circular,
    normalize_cospar,
    normalize_designation,
    normalize_norad,
    object_name_key,
    parse_reference,
    validate_record,
)
from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "astronomy"
FORMATS = {
    "mpc-identifier-json": "mpc-designations",
    "mpcorb-dat": "mpc-orbits",
    "jpl-sbdb-json": "jpl-sbdb",
    "jpl-sentry-json": "jpl-sentry",
    "exoplanet-tap-csv": "nasa-exoplanet-archive",
    "exoplanet-removed-csv": "nasa-exoplanet-archive",
    "gcat-launch-tsv": "gcat",
    "gcat-satcat-tsv": "gcat",
    "gcat-orgs-tsv": "gcat",
    "celestrak-satcat-csv": "celestrak-satcat",
    "swpc-alerts-json": "noaa-swpc",
}
LISTINGS = ("complete", "partial")
MAX_ROWS = 50_000
ATTRIBUTION = {
    "mpc-designations": "This research has made use of data and/or services provided by the International "
    "Astronomical Union's Minor Planet Center.",
    "mpc-orbits": "This research has made use of data and/or services provided by the International Astronomical "
    "Union's Minor Planet Center.",
    "jpl-sbdb": "Courtesy NASA/JPL-Caltech (JPL Small-Body Database).",
    "jpl-sentry": "Courtesy NASA/JPL-Caltech (JPL Sentry).",
    "nasa-exoplanet-archive": "This research has made use of the NASA Exoplanet Archive, which is operated by the "
    "California Institute of Technology, under contract with NASA under the Exoplanet Exploration Program.",
    "gcat": "GCAT (General Catalog of Artificial Space Objects), Jonathan C. McDowell, planet4589.org/space/gcat.",
    "celestrak-satcat": "SATCAT data courtesy of CelesTrak (T.S. Kelso).",
    "noaa-swpc": "NOAA Space Weather Prediction Center.",
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "mpc-designations": {
        "publisher": "IAU Minor Planet Center, designation identifier API",
        "endpoint": "https://data.minorplanetcenter.net/api/query-identifier",
        "format": "mpc-identifier-json",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the live API expects the identifier in a GET body (verify), which the "
        "runtime's GET-only transport does not send",
    },
    "mpc-orbits": {
        "publisher": "IAU Minor Planet Center, MPCORB",
        "endpoint": "https://minorplanetcenter.net/iau/MPCORB/",
        "format": "mpcorb-dat",
        "access_decision": "unverified-live",
        "reason": "fixture-verified fixed-width parser; no dated live run yet",
    },
    "jpl-sbdb": {
        "publisher": "NASA/JPL Small-Body Database API",
        "endpoint": "https://ssd-api.jpl.nasa.gov/sbdb.api",
        "format": "jpl-sbdb-json",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "jpl-sentry": {
        "publisher": "NASA/JPL Sentry impact monitoring API (quoted only)",
        "endpoint": "https://ssd-api.jpl.nasa.gov/sentry.api",
        "format": "jpl-sentry-json",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the removed-object response shape is an assumption (verify)",
    },
    "nasa-exoplanet-archive": {
        "publisher": "NASA Exoplanet Archive (TAP: ps, pscomppars, toi, cumulative; removed planets)",
        "endpoint": "https://exoplanetarchive.ipac.caltech.edu/TAP/sync",
        "format": "exoplanet-tap-csv",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser through declared columns; the removed-planets listing's columns are an "
        "assumption (verify)",
    },
    "gcat": {
        "publisher": "GCAT, Jonathan C. McDowell",
        "endpoint": "https://planet4589.org/space/gcat/tsv/",
        "format": "gcat-launch-tsv",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser through declared columns; no dated live run yet",
    },
    "celestrak-satcat": {
        "publisher": "CelesTrak SATCAT",
        "endpoint": "https://celestrak.org/pub/satcat.csv",
        "format": "celestrak-satcat-csv",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "noaa-swpc": {
        "publisher": "NOAA Space Weather Prediction Center",
        "endpoint": "https://services.swpc.noaa.gov/products/alerts.json",
        "format": "swpc-alerts-json",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "jpl-horizons": {
        "publisher": "NASA/JPL Horizons",
        "access_decision": "link-only",
        "reason": "Horizons generates ephemerides; Noesis never generates positions, and SBDB states the solution ID",
    },
    "mpc-orbit-api": {
        "publisher": "IAU Minor Planet Center, orbit API",
        "access_decision": "link-only",
        "reason": "MPCORB lines carry the reference, epoch, arc and observation count v1 needs",
    },
    "space-track": {
        "publisher": "Space-Track.org (18th SDS)",
        "access_decision": "not-implemented",
        "reason": "account-restricted redistribution terms (verify); CelesTrak SATCAT and GCAT cover v1",
    },
    "esa-neocc": {
        "publisher": "ESA NEO Coordination Centre",
        "access_decision": "not-implemented",
        "reason": "a second risk listing needs its own terms review and listing-date semantics (verify)",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live"
        if contract["access_decision"] == "unverified-live"
        else contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# GCAT Launch_Code second character -> launch_outcome (AS01; verify against GCAT's launch-code documentation).
GCAT_OUTCOMES = {"S": "success", "F": "failure", "P": "partial"}
TOI_DISPOSITIONS = {
    "CP": "confirmed",
    "KP": "confirmed",
    "PC": "candidate",
    "APC": "candidate",
    "FP": "false_positive",
    "FA": "false_alarm",
}
KOI_DISPOSITIONS = {
    "CONFIRMED": "confirmed",
    "CANDIDATE": "candidate",
    "FALSE POSITIVE": "false_positive",
}
# Default declared columns per tabular format (the documented headers); a declaration may override any of them.
DEFAULT_COLUMNS: dict[str, dict[str, str]] = {
    "ps": {
        "pl_name": "pl_name",
        "hostname": "hostname",
        "default_flag": "default_flag",
        "pl_refname": "pl_refname",
        "disc_refname": "disc_refname",
        "discoverymethod": "discoverymethod",
        "disc_year": "disc_year",
        "disc_facility": "disc_facility",
        "pl_controv_flag": "pl_controv_flag",
        "rowupdate": "rowupdate",
        "tic_id": "tic_id",
        "gaia_id": "gaia_id",
        "toi": "toi",
        "pl_orbper": "pl_orbper",
        "pl_orbpererr1": "pl_orbpererr1",
        "pl_orbpererr2": "pl_orbpererr2",
        "pl_rade": "pl_rade",
        "pl_radj": "pl_radj",
        "pl_bmasse": "pl_bmasse",
        "pl_orbsmax": "pl_orbsmax",
    },
    "pscomppars": {
        "pl_name": "pl_name",
        "hostname": "hostname",
        "rowupdate": "rowupdate",
        "tic_id": "tic_id",
        "gaia_id": "gaia_id",
        "pl_orbper": "pl_orbper",
        "pl_rade": "pl_rade",
        "pl_radj": "pl_radj",
        "pl_bmasse": "pl_bmasse",
        "pl_orbsmax": "pl_orbsmax",
        "st_teff": "st_teff",
        "st_rad": "st_rad",
        "st_mass": "st_mass",
    },
    "toi": {
        "toi": "toi",
        "tid": "tid",
        "tfopwg_disp": "tfopwg_disp",
        "rowupdate": "rowupdate",
        "pl_orbper": "pl_orbper",
        "pl_rade": "pl_rade",
    },
    "cumulative": {
        "kepoi_name": "kepoi_name",
        "kepid": "kepid",
        "kepler_name": "kepler_name",
        "koi_disposition": "koi_disposition",
        "koi_pdisposition": "koi_pdisposition",
        "rowupdate": "rowupdate",
        "koi_period": "koi_period",
    },
    "removed": {
        "pl_name": "pl_name",
        "hostname": "hostname",
        "removal_date": "removal_date",
        "note": "note",
        "reference": "reference",
    },
    "gcat-launch-tsv": {
        "launch_tag": "#Launch_Tag",
        "launch_date": "Launch_Date",
        "lv_type": "LV_Type",
        "variant": "Variant",
        "flight": "Flight",
        "mission": "Mission",
        "site": "Launch_Site",
        "pad": "Launch_Pad",
        "agency": "Agency",
        "launch_code": "Launch_Code",
    },
    "gcat-satcat-tsv": {
        "jcat": "#JCAT",
        "satcat": "Satcat",
        "launch_tag": "Launch_Tag",
        "piece": "Piece",
        "type": "Type",
        "name": "Name",
        "ldate": "LDate",
        "ddate": "DDate",
        "status": "Status",
        "owner": "Owner",
    },
    "gcat-orgs-tsv": {
        "code": "#Code",
        "state_code": "StateCode",
        "type": "Type",
        "class": "Class",
        "short_name": "ShortName",
        "name": "Name",
        "location": "Location",
        "longitude": "Longitude",
        "latitude": "Latitude",
        "parent": "Parent",
    },
    "celestrak-satcat-csv": {
        "name": "OBJECT_NAME",
        "object_id": "OBJECT_ID",
        "norad": "NORAD_CAT_ID",
        "object_type": "OBJECT_TYPE",
        "status": "OPS_STATUS_CODE",
        "owner": "OWNER",
        "launch_date": "LAUNCH_DATE",
        "launch_site": "LAUNCH_SITE",
        "decay_date": "DECAY_DATE",
    },
}
PARAMETER_UNITS = {
    "pl_orbper": "d",
    "pl_rade": "R_earth",
    "pl_radj": "R_jupiter",
    "pl_bmasse": "M_earth",
    "pl_orbsmax": "au",
    "st_teff": "K",
    "st_rad": "R_sun",
    "st_mass": "M_sun",
    "koi_period": "d",
}


class AstronomyFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cell(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split()).strip()
    return None if text.casefold() in {"", "-", "null", "none", "nan"} else text


def _text(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 50_000_000:
        raise AstronomyFormatError("input_limit", "document is missing or oversized")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AstronomyFormatError(
            "schema_drift", "document is not UTF-8 text"
        ) from exc
    if text.lstrip().startswith("<") and not text.lstrip().startswith("<?xml"):
        raise AstronomyFormatError(
            "schema_drift", "an HTML page was returned instead of the declared document"
        )
    return text.lstrip("﻿")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(_text(raw))
    except ValueError as exc:
        raise AstronomyFormatError("schema_drift", "document is not JSON") from exc


def read_table(
    text: str, columns: Mapping[str, str], *, delimiter: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows keyed by declared field names (with 1-based row numbers) and the declared columns absent.

    Comment lines (``#`` followed by a space, as in GCAT's update line) are skipped; the header may start with
    ``#`` (GCAT's ``#Launch_Tag``).
    """
    lines = [
        line for line in text.splitlines() if line.strip() and not line.startswith("# ")
    ]
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise AstronomyFormatError("schema_drift", "table has no header row") from exc
    positions = {name.strip(): index for index, name in enumerate(header)}
    mapping = {field: positions.get(name) for field, name in columns.items()}
    missing = sorted(field for field, index in mapping.items() if index is None)
    rows = []
    for number, cells in enumerate(reader, start=1):
        if number > MAX_ROWS:
            raise AstronomyFormatError("input_limit", "table exceeds the row bound")
        row: dict[str, Any] = {"_row": number}
        for field, index in mapping.items():
            row[field] = (
                _cell(cells[index])
                if index is not None and index < len(cells)
                else None
            )
        rows.append(row)
    return rows, missing


def _source(
    provider: str,
    source_record_id: str,
    document: Mapping[str, Any],
    locator: str,
    published_at: str | None = None,
) -> dict[str, Any]:
    source = {
        "provider": provider,
        "source_record_id": source_record_id,
        "url": document["url"],
        "locator": locator,
        "document": document["label"],
        "attribution": ATTRIBUTION[provider],
    }
    if published_at:
        source["published_at"] = published_at
    return source


class _Result:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.counts = {"rows": 0, "out_of_scope": 0}

    def add(self, record: Mapping[str, Any], *, row: Any) -> None:
        try:
            self.records.append(validate_record(record))
            self.seen.add(record["source"]["source_record_id"])
        except AstronomyError as exc:
            self.reject(
                "invalid_record",
                str(exc),
                row=row,
                source_record_id=record["source"]["source_record_id"],
            )

    def reject(
        self, code: str, reason: str, *, row: Any, source_record_id: str | None = None
    ) -> None:
        item = {"code": code, "reason": reason[:300], "row": row}
        if source_record_id:
            item["source_record_id"] = source_record_id
        self.rejected.append(item)


def _declared_designations(declared: Mapping[str, Any]) -> set[str]:
    return {k for k in (designation_key(o) for o in declared.get("objects") or []) if k}


# ------------------------------------------------------------------ MPC


def parse_mpc_identifier(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    """Designations and identifications from the MPC designation identifier API response."""
    data = _json(raw)
    result = _Result()
    provider = "mpc-designations"
    if isinstance(data, Mapping) and "found" in data:
        entries = [(document.get("object") or "", data)]
    elif isinstance(data, Mapping):
        entries = sorted(data.items())
    else:
        raise AstronomyFormatError(
            "schema_drift", "identifier response is not an object"
        )
    wanted = _declared_designations(declared)
    for query, entry in entries:
        result.counts["rows"] += 1
        if not isinstance(entry, Mapping) or not entry.get("found"):
            result.reject(
                "not_found", f"the MPC does not identify {query!r}", row=query
            )
            continue
        permid = _cell(entry.get("permid"))
        primary = _cell(entry.get("unpacked_primary_provisional_designation"))
        primary_norm = normalize_designation(primary) if primary else None
        number = normalize_designation(permid) if permid else None
        if primary_norm is None and number is None:
            result.reject(
                "invalid_record",
                "the response states no primary designation",
                row=query,
            )
            continue
        secondaries = [
            s
            for s in (
                _cell(v)
                for v in entry.get("unpacked_secondary_provisional_designations") or []
            )
            if s
        ]
        keys = {designation_key(v) for v in [primary, permid, *secondaries] if v}
        if wanted and not keys & wanted:
            result.counts["out_of_scope"] += 1
            continue
        anchor = (primary_norm or number)["unpacked"]
        locator = f"query-identifier:{query}"
        object_type = entry.get("object_type")
        object_kind = _cell(
            object_type[0]
            if isinstance(object_type, list) and object_type
            else object_type
        )
        body: dict[str, Any] = {
            "kind": "small_body",
            "source": _source(provider, "small_body|" + anchor, document, locator),
            "primary_designation": anchor,
            "packed": (primary_norm or number).get("packed"),
            "number": number["number"] if number else None,
            "name": _cell(entry.get("name")),
            "object_kind": object_kind,
            "stated_designations": sorted(
                normalize_designation(s)["unpacked"] for s in secondaries
            ),
        }
        result.add(body, row=query)
        designations = [(primary_norm, None)] if primary_norm else []
        designations += [(normalize_designation(s), anchor) for s in secondaries]
        if number:
            designations.append((number, anchor if primary_norm else None))
        for norm, belongs in designations:
            result.add(
                {
                    "kind": "designation",
                    "source": _source(
                        provider, "designation|" + norm["unpacked"], document, locator
                    ),
                    "designation": norm["unpacked"],
                    "packed": norm.get("packed"),
                    "designation_kind": norm["kind"],
                    "object_designation": belongs,
                },
                row=query,
            )
        stated = {
            designation_key(i.get("designation")): i
            for i in entry.get("identifications") or []
            if isinstance(i, Mapping)
        }
        for secondary in secondaries:
            norm = normalize_designation(secondary)
            if primary_norm is None or norm["unpacked"] == anchor:
                continue
            announced = stated.get(designation_key(secondary)) or {}
            reference = _cell(announced.get("reference"))
            result.add(
                {
                    "kind": "identification",
                    "source": _source(
                        provider,
                        "identification|" + norm["unpacked"],
                        document,
                        locator,
                        iso_day(announced.get("date")),
                    ),
                    "designation": norm["unpacked"],
                    "identified_with": anchor,
                    "permanent": number["unpacked"] if number else None,
                    "announced_in": normalize_circular(reference) or reference,
                    "announced_on": iso_day(announced.get("date")),
                },
                row=query,
            )
    return result


def _packed_epoch(text: str) -> str | None:
    """MPC packed date (``K2692`` = 2026-09-02) as an ISO date."""
    match = re.fullmatch(r"([IJKL])(\d{2})([1-9A-C])([1-9A-V])", text.strip())
    if not match:
        return None
    century = {"I": 18, "J": 19, "K": 20, "L": 21}[match.group(1)]
    month = int(match.group(3), 16) if match.group(3) in "ABC" else int(match.group(3))
    day = int(match.group(4), 36)
    return iso_day(f"{century}{match.group(2)}-{month:02d}-{day:02d}")


def parse_mpcorb(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    """MPCORB fixed-width lines (column positions per the MPC's MPCORB format description)."""
    text = _text(raw)
    lines = text.splitlines()
    if any(line.startswith("-----") for line in lines):
        lines = lines[
            next(i for i, line in enumerate(lines) if line.startswith("-----")) + 1 :
        ]
    result = _Result()
    wanted = _declared_designations(declared)
    provider = "mpc-orbits"
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        result.counts["rows"] += 1
        packed = line[0:7].strip()
        norm = normalize_designation(packed)
        if norm is None or norm["kind"] not in {"provisional", "permanent", "survey"}:
            result.reject(
                "schema_drift",
                "line does not start with a packed designation",
                row=number,
            )
            continue
        readable = line[166:194].strip()
        keys = {norm["unpacked"]}
        match = re.match(r"\((\d+)\)\s*(.*)$", readable)
        if match and match.group(2):
            keys.add(designation_key(match.group(2)))
        elif readable:
            keys.add(designation_key(readable))
        if wanted and not keys & wanted:
            result.counts["out_of_scope"] += 1
            continue
        epoch_packed = line[20:25].strip()
        epoch_day = _packed_epoch(epoch_packed)
        reference = line[107:116].strip()
        if not epoch_day or not reference:
            result.reject(
                "invalid_record",
                "line states no packed epoch or orbit reference",
                row=number,
                source_record_id=f"{packed}|{reference}|{epoch_packed}",
            )
            continue
        elements = {}
        for name, start, end, unit in (
            ("M", 26, 35, "deg"),
            ("w", 37, 46, "deg"),
            ("om", 48, 57, "deg"),
            ("i", 59, 68, "deg"),
            ("e", 70, 79, None),
            ("n", 80, 91, "deg/d"),
            ("a", 92, 103, "au"),
            ("H", 8, 13, "mag"),
            ("G", 14, 19, None),
        ):
            value = decimal_text(line[start:end])
            if value is not None:
                elements[name] = {"value": value, **({"unit": unit} if unit else {})}
        arc_text = line[127:136].strip()
        last_obs = iso_day(line[194:202].strip())
        arc: dict[str, Any] = {}
        if arc_text:
            arc["stated"] = arc_text
            days = re.fullmatch(r"(\d+)\s+days", arc_text)
            if days:
                arc["days"] = days.group(1)
        if last_obs:
            arc["last_obs"] = last_obs
        n_obs = line[117:122].strip()
        n_opp = line[123:126].strip()
        uncertainty = line[105:106].strip()
        source_record_id = f"{packed}|{reference}|{epoch_packed}"
        result.add(
            {
                "kind": "orbit_solution",
                "source": _source(
                    provider, source_record_id, document, f"line {number}"
                ),
                "object_designation": norm["unpacked"],
                "publisher": "MPC",
                "solution_id": reference,
                "reference": reference,
                "epoch": {
                    "jd": jd_of_day(epoch_day),
                    "calendar": epoch_day,
                    "scale": "TT",
                    "stated": epoch_packed,
                },
                "elements": elements,
                "arc": arc or None,
                "n_obs_used": int(n_obs) if n_obs.isdigit() else None,
                "n_opp": int(n_opp) if n_opp.isdigit() else None,
                "uncertainty": {"parameter": "U", "value": uncertainty}
                if uncertainty
                else None,
                "computer": line[150:160].strip() or None,
            },
            row=number,
        )
    return result


# ------------------------------------------------------------------ JPL


def _unit(value: Any) -> str | None:
    text = _cell(value)
    if text is None:
        return None
    return {
        "AU": "au",
        "au": "au",
        "deg": "deg",
        "d": "d",
        "deg/d": "deg/d",
        "km": "km",
    }.get(text, text)


def parse_sbdb(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    data = _json(raw)
    result = _Result()
    provider = "jpl-sbdb"
    result.counts["rows"] += 1
    if not isinstance(data, Mapping) or "object" not in data:
        message = data.get("message") if isinstance(data, Mapping) else None
        result.reject(
            "not_found",
            f"SBDB returned no object ({message or 'no message'})",
            row=document["label"],
        )
        return result
    obj, orbit = data.get("object") or {}, data.get("orbit") or {}
    des = _cell(obj.get("des"))
    if not des:
        result.reject(
            "invalid_record", "SBDB object states no designation", row=document["label"]
        )
        return result
    norm = normalize_designation(des)
    fullname = _cell(obj.get("fullname")) or ""
    stated = set()
    paren = re.search(r"\(([^)]+)\)\s*$", fullname)
    if paren:
        stated.add(normalize_designation(paren.group(1))["unpacked"])
    for alt in obj.get("des_alt") or []:
        for value in alt.values() if isinstance(alt, Mapping) else [alt]:
            if isinstance(value, str) and normalize_designation(value):
                stated.add(normalize_designation(value)["unpacked"])
    stated.discard(norm["unpacked"])
    wanted = _declared_designations(declared)
    if (
        wanted
        and not ({norm["unpacked"]} | {designation_key(s) for s in stated}) & wanted
    ):
        result.counts["out_of_scope"] += 1
        return result
    name = None
    named = re.match(r"^\s*\d+\s+(.+?)\s*(?:\(|$)", fullname)
    if norm["kind"] == "permanent" and named:
        name = named.group(1)
    locator = "sbdb.api?sstr=" + (document.get("object") or des)
    result.add(
        {
            "kind": "small_body",
            "source": _source(
                provider, "small_body|" + norm["unpacked"], document, locator
            ),
            "primary_designation": norm["unpacked"],
            "packed": norm.get("packed"),
            "number": norm.get("number"),
            "name": name,
            "object_kind": _cell(obj.get("kind")),
            "stated_designations": sorted(stated),
            "spk_id": _cell(obj.get("spkid")),
        },
        row=document["label"],
    )
    orbit_id = _cell(orbit.get("orbit_id"))
    if not orbit:
        return result
    elements = {}
    for element in orbit.get("elements") or []:
        value = decimal_text(element.get("value"))
        if element.get("name") and value is not None:
            elements[element["name"]] = {
                "value": value,
                **(
                    {"unit": _unit(element.get("units"))}
                    if _unit(element.get("units"))
                    else {}
                ),
                **(
                    {"sigma": decimal_text(element.get("sigma"))}
                    if decimal_text(element.get("sigma")) is not None
                    else {}
                ),
            }
    epoch = jd_text(orbit.get("epoch"))
    if not orbit_id or not epoch or not elements:
        result.reject(
            "invalid_record",
            "SBDB orbit states no orbit_id, epoch or elements",
            row=document["label"],
            source_record_id=f"{norm['unpacked']}|{orbit_id}",
        )
        return result
    n_obs = orbit.get("n_obs_used")
    condition = _cell(orbit.get("condition_code"))
    arc = {
        k: v
        for k, v in (
            ("first_obs", iso_day(orbit.get("first_obs"))),
            ("last_obs", iso_day(orbit.get("last_obs"))),
            ("days", decimal_text(orbit.get("data_arc"))),
        )
        if v
    }
    result.add(
        {
            "kind": "orbit_solution",
            "source": _source(
                provider,
                f"{norm['unpacked']}|{orbit_id}",
                document,
                locator,
                iso_time(orbit.get("soln_date")) or iso_day(orbit.get("soln_date")),
            ),
            "object_designation": norm["unpacked"],
            "publisher": "JPL",
            "solution_id": orbit_id,
            "epoch": {"jd": epoch, "scale": "TDB"},
            "elements": elements,
            "arc": arc or None,
            "n_obs_used": int(n_obs) if str(n_obs or "").isdigit() else None,
            "uncertainty": {"parameter": "condition_code", "value": condition}
            if condition
            else None,
            "computed_at": iso_time(orbit.get("soln_date"))
            or iso_day(orbit.get("soln_date")),
            "computer": _cell(orbit.get("producer")),
            "orbit_class": _cell((obj.get("orbit_class") or {}).get("code")),
        },
        row=document["label"],
    )
    return result


SENTRY_FIGURES = (
    "ip",
    "ps_cum",
    "ps_max",
    "ts_max",
    "n_imp",
    "v_inf",
    "h",
    "diameter",
    "range",
    "last_obs",
)


def parse_sentry(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    """A Sentry object summary quoted as published, or its removal (listing status ``removed``)."""
    data = _json(raw)
    result = _Result()
    result.counts["rows"] += 1
    provider = "jpl-sentry"
    if not isinstance(data, Mapping):
        raise AstronomyFormatError("schema_drift", "Sentry response is not an object")
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else None
    des = (
        _cell((summary or {}).get("des"))
        or _cell(data.get("des"))
        or _cell(document.get("object"))
    )
    if not des:
        result.reject(
            "invalid_record", "Sentry response names no object", row=document["label"]
        )
        return result
    norm = normalize_designation(des)
    wanted = _declared_designations(declared)
    if wanted and norm["unpacked"] not in wanted:
        result.counts["out_of_scope"] += 1
        return result
    locator = "sentry.api?des=" + des
    if data.get("removed"):
        removed = iso_time(data["removed"]) or iso_day(data["removed"])
        result.add(
            {
                "kind": "impact_risk_listing",
                "source": _source(
                    provider, "sentry|" + norm["unpacked"], document, locator, removed
                ),
                "object_designation": norm["unpacked"],
                "publisher": "JPL Sentry",
                "listing_status": "removed",
                "removed_at": removed,
            },
            row=document["label"],
        )
        return result
    if summary is None:
        result.reject(
            "schema_drift",
            "Sentry response states neither a summary nor a removal",
            row=document["label"],
        )
        return result
    listed = iso_time(summary.get("cdate")) or iso_day(summary.get("cdate"))
    figures = {
        k: str(summary[k]).strip() for k in SENTRY_FIGURES if _cell(summary.get(k))
    }
    result.add(
        {
            "kind": "impact_risk_listing",
            "source": _source(
                provider, "sentry|" + norm["unpacked"], document, locator, listed
            ),
            "object_designation": norm["unpacked"],
            "publisher": "JPL Sentry",
            "listing_status": "listed",
            "listing_date": listed,
            "figures": figures or None,
            "method": _cell(summary.get("method")),
        },
        row=document["label"],
    )
    return result


# ------------------------------------------------------------------ NASA Exoplanet Archive


def _hosts(declared: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {object_name_key(h["name"]): h for h in declared.get("hosts") or []}


def _quantities(row: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    out = {}
    for name in names:
        value = decimal_text(row.get(name))
        if value is None:
            continue
        item: dict[str, Any] = {"value": value}
        if PARAMETER_UNITS.get(name):
            item["unit"] = PARAMETER_UNITS[name]
        for err in ("err1", "err2"):
            if decimal_text(row.get(name + err)) is not None:
                item[err] = decimal_text(row.get(name + err))
        out[name] = item
    return out


def _flag(value: Any) -> bool:
    return str(value or "").strip() in {"1", "true", "True"}


def parse_exoplanet_tap(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    table = declared.get("table")
    if table not in {"ps", "pscomppars", "toi", "cumulative"}:
        raise AstronomyFormatError("schema_drift", "the declaration names no TAP table")
    columns = {**DEFAULT_COLUMNS[table], **dict(declared.get("columns") or {})}
    rows, missing = read_table(_text(raw), columns, delimiter=",")
    result = _Result()
    result.counts["missing_columns"] = missing  # type: ignore[assignment]
    hosts = _hosts(declared)
    tics = {str(h["tic"]) for h in hosts.values() if h.get("tic")}
    kepids = {str(h["kepid"]) for h in hosts.values() if h.get("kepid")}
    provider = "nasa-exoplanet-archive"
    for row in rows:
        result.counts["rows"] += 1
        number = row["_row"]
        if table in {"ps", "pscomppars"}:
            name, host = row.get("pl_name"), row.get("hostname")
            if not name or not host:
                result.reject(
                    "invalid_record", "row states no pl_name or hostname", row=number
                )
                continue
            if hosts and object_name_key(host) not in hosts:
                result.counts["out_of_scope"] += 1
                continue
            asserted = iso_time(row.get("rowupdate")) or iso_day(row.get("rowupdate"))
            identifiers = {
                k: v
                for k, v in (
                    ("tic", _id(row.get("tic_id"), "TIC")),
                    ("gaia_dr3", _id(row.get("gaia_id"), "Gaia DR3")),
                    ("toi", row.get("toi")),
                )
                if v
            }
            reference = (
                parse_reference(row.get("pl_refname")) if table == "ps" else None
            )
            label = (
                (reference or {}).get("bibcode")
                or (reference or {}).get("label")
                or (reference or {}).get("text")
            )
            if table == "ps" and not label:
                result.reject(
                    "invalid_record", "a ps row states no pl_refname", row=number
                )
                continue
            key = f"ps|{name}|{label}" if table == "ps" else f"pscomppars|{name}"
            discovery = {
                k: v
                for k, v in (
                    ("method", row.get("discoverymethod")),
                    ("year", row.get("disc_year")),
                    ("facility", row.get("disc_facility")),
                )
                if v
            }
            disc_ref = parse_reference(row.get("disc_refname"))
            if disc_ref:
                discovery["reference"] = disc_ref
            result.add(
                {
                    "kind": "exoplanet",
                    "source": _source(
                        provider, key, document, f"{table} row {number}", asserted
                    ),
                    "name": name,
                    "host": host,
                    "source_table": table,
                    "composite": table == "pscomppars",
                    "default_set": _flag(row.get("default_flag"))
                    if table == "ps"
                    else None,
                    "reference": reference,
                    "identifiers": identifiers or None,
                    "parameters": _quantities(
                        row,
                        ("pl_orbper", "pl_rade", "pl_radj", "pl_bmasse", "pl_orbsmax"),
                    )
                    or None,
                    "discovery": discovery or None,
                },
                row=number,
            )
            if table == "ps" and _flag(row.get("default_flag")):
                controversial = _flag(row.get("pl_controv_flag"))
                result.add(
                    {
                        "kind": "exoplanet_status_assertion",
                        "source": _source(
                            provider,
                            f"ps|{name}",
                            document,
                            f"ps row {number}",
                            asserted,
                        ),
                        "object_name": name,
                        "object_scheme": "pl_name",
                        "host": host,
                        "source_table": "ps",
                        "native_disposition": "default_flag=1"
                        + (";pl_controv_flag=1" if controversial else ""),
                        "disposition": "controversial"
                        if controversial
                        else "confirmed",
                        "reference": reference,
                        "asserted_at": asserted,
                        "identifiers": identifiers or None,
                    },
                    row=number,
                )
            if table == "pscomppars":
                result.add(
                    {
                        "kind": "host_star",
                        "source": _source(
                            provider,
                            f"pscomppars|{host}",
                            document,
                            f"pscomppars row {number}",
                            asserted,
                        ),
                        "name": host,
                        "source_table": "pscomppars",
                        "identifiers": {
                            k: v for k, v in identifiers.items() if k != "toi"
                        }
                        or None,
                        "parameters": _quantities(row, ("st_teff", "st_rad", "st_mass"))
                        or None,
                    },
                    row=number,
                )
        elif table == "toi":
            toi, tid = row.get("toi"), row.get("tid")
            if not toi:
                result.reject("invalid_record", "row states no toi", row=number)
                continue
            if tics and str(tid) not in tics:
                result.counts["out_of_scope"] += 1
                continue
            native = row.get("tfopwg_disp")
            if not native:
                result.reject(
                    "invalid_record",
                    "row states no tfopwg_disp",
                    row=number,
                    source_record_id=f"toi|{toi}",
                )
                continue
            host = next(
                (h["name"] for h in hosts.values() if str(h.get("tic")) == str(tid)),
                None,
            )
            result.add(
                {
                    "kind": "exoplanet_status_assertion",
                    "source": _source(
                        provider,
                        f"toi|{toi}",
                        document,
                        f"toi row {number}",
                        iso_time(row.get("rowupdate")) or iso_day(row.get("rowupdate")),
                    ),
                    "object_name": toi,
                    "object_scheme": "toi",
                    "host": host,
                    "source_table": "toi",
                    "native_disposition": native,
                    "disposition": TOI_DISPOSITIONS.get(native.upper()),
                    "asserted_at": iso_time(row.get("rowupdate"))
                    or iso_day(row.get("rowupdate")),
                    "identifiers": {"tic": _id(tid, "TIC")} if tid else None,
                },
                row=number,
            )
        else:
            koi, kepid = row.get("kepoi_name"), row.get("kepid")
            if not koi:
                result.reject("invalid_record", "row states no kepoi_name", row=number)
                continue
            if kepids and str(kepid) not in kepids:
                result.counts["out_of_scope"] += 1
                continue
            native = row.get("koi_disposition")
            if not native:
                result.reject(
                    "invalid_record",
                    "row states no koi_disposition",
                    row=number,
                    source_record_id=f"cumulative|{koi}",
                )
                continue
            host = next(
                (
                    h["name"]
                    for h in hosts.values()
                    if str(h.get("kepid")) == str(kepid)
                ),
                None,
            )
            identifiers = {
                k: v
                for k, v in (("kepid", kepid), ("pl_name", row.get("kepler_name")))
                if v
            }
            result.add(
                {
                    "kind": "exoplanet_status_assertion",
                    "source": _source(
                        provider,
                        f"cumulative|{koi}",
                        document,
                        f"cumulative row {number}",
                        iso_time(row.get("rowupdate")) or iso_day(row.get("rowupdate")),
                    ),
                    "object_name": koi,
                    "object_scheme": "koi",
                    "host": host,
                    "source_table": "cumulative",
                    "native_disposition": native,
                    "disposition": KOI_DISPOSITIONS.get(native.upper()),
                    "asserted_at": iso_time(row.get("rowupdate"))
                    or iso_day(row.get("rowupdate")),
                    "identifiers": identifiers or None,
                },
                row=number,
            )
    return result


def _id(value: Any, prefix: str) -> str | None:
    text = _cell(value)
    if not text:
        return None
    return text if text.upper().startswith(prefix.upper()) else f"{prefix} {text}"


def parse_exoplanet_removed(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    """The archive's removed/retracted planets listing: a ``retracted`` assertion as the archive states it."""
    columns = {**DEFAULT_COLUMNS["removed"], **dict(declared.get("columns") or {})}
    rows, missing = read_table(_text(raw), columns, delimiter=",")
    result = _Result()
    result.counts["missing_columns"] = missing  # type: ignore[assignment]
    hosts = _hosts(declared)
    for row in rows:
        result.counts["rows"] += 1
        name, host = row.get("pl_name"), row.get("hostname")
        if not name:
            result.reject("invalid_record", "row states no pl_name", row=row["_row"])
            continue
        if hosts and object_name_key(host) not in hosts:
            result.counts["out_of_scope"] += 1
            continue
        removed = iso_day(row.get("removal_date"))
        result.add(
            {
                "kind": "exoplanet_status_assertion",
                "source": _source(
                    "nasa-exoplanet-archive",
                    f"removed|{name}",
                    document,
                    f"removed row {row['_row']}",
                    removed,
                ),
                "object_name": name,
                "object_scheme": "pl_name",
                "host": host,
                "source_table": "removed",
                "native_disposition": "removed",
                "disposition": "retracted",
                "asserted_at": removed,
                "note": row.get("note"),
                "reference": parse_reference(row.get("reference")),
            },
            row=row["_row"],
        )
    return result


# ------------------------------------------------------------------ GCAT and CelesTrak


def _gcat_when(value: Any) -> tuple[str | None, str | None]:
    """GCAT dates (``2026 Jan 15 1234:56``, ``2026 Jan 15``, with ``?`` for uncertainty): ISO value and verbatim."""
    text = _cell(value)
    if not text:
        return None, None
    clean = text.replace("?", "").strip()
    match = re.match(r"^(\d{4} [A-Za-z]{3}\s+\d{1,2})\s+(\d{2})(\d{2})", clean)
    if match:
        return iso_time(f"{match.group(1)} {match.group(2)}{match.group(3)} UTC"), text
    return iso_day(clean), text


def _in_years(value: Any, years: set[str]) -> bool:
    return not years or str(value or "")[:4] in years


def parse_gcat(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any], fmt: str
) -> _Result:
    columns = {**DEFAULT_COLUMNS[fmt], **dict(declared.get("columns") or {})}
    rows, missing = read_table(_text(raw), columns, delimiter="\t")
    result = _Result()
    result.counts["missing_columns"] = missing  # type: ignore[assignment]
    years = {str(y) for y in declared.get("years") or []}
    codes = {str(c).upper() for c in declared.get("codes") or []}
    for row in rows:
        result.counts["rows"] += 1
        number = row["_row"]
        if fmt == "gcat-launch-tsv":
            tag = row.get("launch_tag")
            if not tag:
                result.reject("invalid_record", "row states no launch tag", row=number)
                continue
            if not _in_years(tag, years):
                result.counts["out_of_scope"] += 1
                continue
            time, stated = _gcat_when(row.get("launch_date"))
            code = row.get("launch_code")
            agencies = sorted(
                {a for a in re.split(r"[/,]", row.get("agency") or "") if a.strip()}
            )
            result.add(
                {
                    "kind": "launch",
                    "source": _source(
                        "gcat", "launch|" + tag, document, f"launch row {number}"
                    ),
                    "launch_tag": tag,
                    "launch_class": code[0] if code else None,
                    "time": time,
                    "stated_time": stated,
                    "vehicle": row.get("lv_type"),
                    "variant": row.get("variant"),
                    "flight": row.get("flight"),
                    "mission": row.get("mission"),
                    "site_code": row.get("site"),
                    "pad": row.get("pad"),
                    "agency_codes": agencies or None,
                    "native_code": code,
                },
                row=number,
            )
            if code:
                result.add(
                    {
                        "kind": "launch_outcome",
                        "source": _source(
                            "gcat", "outcome|" + tag, document, f"launch row {number}"
                        ),
                        "launch_tag": tag,
                        "native_code": code,
                        "outcome": GCAT_OUTCOMES.get(code[1:2].upper()),
                    },
                    row=number,
                )
        elif fmt == "gcat-satcat-tsv":
            jcat, tag = row.get("jcat"), row.get("launch_tag")
            if not jcat:
                result.reject("invalid_record", "row states no JCAT", row=number)
                continue
            if not _in_years(tag, years):
                result.counts["out_of_scope"] += 1
                continue
            launched, _ = _gcat_when(row.get("ldate"))
            decayed, stated_decay = _gcat_when(row.get("ddate"))
            cospar = (
                normalize_cospar(f"{tag}{row.get('piece') or ''}")
                if tag and row.get("piece")
                else None
            )
            result.add(
                {
                    "kind": "orbital_object",
                    "source": _source(
                        "gcat", "object|" + jcat, document, f"satcat row {number}"
                    ),
                    "jcat": jcat,
                    "norad": normalize_norad(row.get("satcat")),
                    "cospar": cospar,
                    "launch_tag": tag,
                    "name": row.get("name"),
                    "object_type": row.get("type"),
                    "owner": row.get("owner"),
                    "status": row.get("status"),
                    "launch_date": launched[:10] if launched else None,
                    "decay_date": decayed,
                    "stated_decay_date": stated_decay,
                },
                row=number,
            )
        else:
            code = row.get("code")
            if not code or not row.get("name"):
                result.reject(
                    "invalid_record",
                    "row states no organisation code or name",
                    row=number,
                )
                continue
            if codes and code.upper() not in codes:
                result.counts["out_of_scope"] += 1
                continue
            result.add(
                {
                    "kind": "space_organisation",
                    "source": _source(
                        "gcat", "org|" + code, document, f"orgs row {number}"
                    ),
                    "code": code,
                    "name": row["name"],
                    "short_name": row.get("short_name"),
                    "state_code": row.get("state_code"),
                    "org_type": row.get("type"),
                    "org_class": row.get("class"),
                    "location": row.get("location"),
                    "latitude": decimal_text(row.get("latitude")),
                    "longitude": decimal_text(row.get("longitude")),
                    "parent": row.get("parent"),
                },
                row=number,
            )
    return result


def parse_celestrak_satcat(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    """CelesTrak SATCAT catalogue fields only; no orbital element sets are read."""
    columns = {
        **DEFAULT_COLUMNS["celestrak-satcat-csv"],
        **dict(declared.get("columns") or {}),
    }
    rows, missing = read_table(_text(raw), columns, delimiter=",")
    result = _Result()
    result.counts["missing_columns"] = missing  # type: ignore[assignment]
    years = {str(y) for y in declared.get("years") or []}
    for row in rows:
        result.counts["rows"] += 1
        norad = normalize_norad(row.get("norad"))
        if not norad:
            result.reject(
                "invalid_record",
                "row states no NORAD catalogue number",
                row=row["_row"],
            )
            continue
        if not _in_years(row.get("object_id"), years):
            result.counts["out_of_scope"] += 1
            continue
        result.add(
            {
                "kind": "orbital_object",
                "source": _source(
                    "celestrak-satcat", norad, document, f"row {row['_row']}"
                ),
                "norad": norad,
                "cospar": normalize_cospar(row.get("object_id")),
                "name": row.get("name"),
                "object_type": row.get("object_type"),
                "owner": row.get("owner"),
                "status": row.get("status"),
                "launch_date": iso_day(row.get("launch_date")),
                "site": row.get("launch_site"),
                "decay_date": iso_day(row.get("decay_date")),
            },
            row=row["_row"],
        )
    return result


# ------------------------------------------------------------------ NOAA SWPC

_HEADER = re.compile(
    r"^(Space Weather Message Code|Serial Number|Issue Time|NOAA Scale|Cancel Serial Number|"
    r"Extension to Serial Number)\s*:\s*(.+?)\s*$",
    re.M,
)
_TITLE_KINDS = (
    ("EXTENDED WARNING", "extended_warning"),
    ("CANCEL WATCH", "cancel_watch"),
    ("CANCEL WARNING", "cancel_warning"),
    ("CANCEL ALERT", "cancel_alert"),
    ("CANCEL SUMMARY", "cancel_summary"),
    ("WATCH", "watch"),
    ("WARNING", "warning"),
    ("ALERT", "alert"),
    ("SUMMARY", "summary"),
)
_CODE_KINDS = {"ALT": "alert", "WAR": "warning", "WAT": "watch", "SUM": "summary"}


def parse_swpc_message(message: str) -> dict[str, Any]:
    """Only the structured header fields of an SWPC message (never the prose)."""
    fields: dict[str, list[str]] = {}
    for key, value in _HEADER.findall(message.replace("\r\n", "\n")):
        fields.setdefault(key, []).append(value)
    lines = [line.strip() for line in message.replace("\r\n", "\n").split("\n")]
    header_end = next(
        (i for i, line in enumerate(lines) if line.startswith("Issue Time")), -1
    )
    title = (
        next((line for line in lines[header_end + 1 :] if line), None)
        if header_end >= 0
        else None
    )
    code = (fields.get("Space Weather Message Code") or [None])[0]
    kind = "other"
    if title and ":" in title:
        prefix = title.split(":", 1)[0].strip().upper()
        kind = next((k for p, k in _TITLE_KINDS if prefix == p), "other")
    if kind == "other" and code:
        kind = _CODE_KINDS.get(code[:3].upper(), "other")
    scales = sorted(
        {
            m.group(0)
            for v in fields.get("NOAA Scale") or []
            for m in [re.match(r"[GSR][1-5]", v)]
            if m
        }
    )
    references = {
        k: v
        for k, v in (
            ("cancels", (fields.get("Cancel Serial Number") or [None])[0]),
            ("extends", (fields.get("Extension to Serial Number") or [None])[0]),
        )
        if v
    }
    return {
        "message_code": code,
        "serial": (fields.get("Serial Number") or [None])[0],
        "issue_time": iso_time((fields.get("Issue Time") or [None])[0]),
        "product_kind": kind,
        "scales": scales or None,
        "references": references or None,
        "title": title,
    }


def parse_swpc_alerts(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Result:
    data = _json(raw)
    if not isinstance(data, list):
        raise AstronomyFormatError(
            "schema_drift", "alerts.json is not a list of products"
        )
    result = _Result()
    for index, item in enumerate(data, start=1):
        result.counts["rows"] += 1
        if (
            not isinstance(item, Mapping)
            or not item.get("product_id")
            or not item.get("message")
        ):
            result.reject(
                "invalid_record", "product states no product_id or message", row=index
            )
            continue
        header = parse_swpc_message(str(item["message"]))
        serial = header.pop("serial")
        issue_time = header.get("issue_time") or iso_time(item.get("issue_datetime"))
        if not serial or not issue_time:
            result.reject(
                "invalid_record",
                "message header states no serial number or issue time",
                row=index,
            )
            continue
        product_id = str(item["product_id"]).strip()
        header["issue_time"] = issue_time
        result.add(
            {
                "kind": "space_weather_product",
                "source": _source(
                    "noaa-swpc",
                    f"{product_id}|{serial}",
                    document,
                    f"item {index}",
                    issue_time,
                ),
                "product_id": product_id,
                "serial": serial,
                "message": str(item["message"]),
                **header,
            },
            row=index,
        )
    return result


# ------------------------------------------------------------------ dispatch


def parse_document(
    fmt: str, raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    """Parse one declared document; returns records, rejections, counts, the listing and the file digest."""
    if fmt == "mpc-identifier-json":
        result = parse_mpc_identifier(raw, declared, document)
    elif fmt == "mpcorb-dat":
        result = parse_mpcorb(raw, declared, document)
    elif fmt == "jpl-sbdb-json":
        result = parse_sbdb(raw, declared, document)
    elif fmt == "jpl-sentry-json":
        result = parse_sentry(raw, declared, document)
    elif fmt == "exoplanet-tap-csv":
        result = parse_exoplanet_tap(raw, declared, document)
    elif fmt == "exoplanet-removed-csv":
        result = parse_exoplanet_removed(raw, declared, document)
    elif fmt in {"gcat-launch-tsv", "gcat-satcat-tsv", "gcat-orgs-tsv"}:
        result = parse_gcat(raw, declared, document, fmt)
    elif fmt == "celestrak-satcat-csv":
        result = parse_celestrak_satcat(raw, declared, document)
    elif fmt == "swpc-alerts-json":
        result = parse_swpc_alerts(raw, declared, document)
    else:
        raise AstronomyFormatError("schema_drift", f"unknown format {fmt!r}")
    counts = dict(result.counts)
    missing = counts.pop("missing_columns", [])
    return {
        "records": result.records,
        "rejected": result.rejected,
        "counts": counts,
        "seen": sorted(result.seen),
        "missing_columns": missing,
        "file_sha256": _sha(raw),
        "listing": _listing(fmt, declared),
    }


def _listing(fmt: str, declared: Mapping[str, Any]) -> dict[str, Any]:
    """The kinds and scope keys a complete document of this format bounds (empty: never a removal listing)."""
    if fmt == "exoplanet-tap-csv" and declared.get("table") in {"ps", "pscomppars"}:
        return {
            "kinds": ["exoplanet", "exoplanet_status_assertion", "host_star"],
            "scope_keys": sorted(
                "host:" + object_name_key(h["name"])
                for h in declared.get("hosts") or []
            ),
            "id_prefix": declared["table"] + "|",
        }
    return {"kinds": [], "scope_keys": []}


def astronomy_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("astronomy") or {})
    if declared.get("format") not in FORMATS:
        raise SourcePackError(
            "invalid_mapping", f"astronomy format is one of {sorted(FORMATS)}"
        )
    if (
        declared.get("provider") != FORMATS[declared["format"]]
        or declared["provider"] not in PROVIDERS
    ):
        raise SourcePackError(
            "invalid_mapping", "the declared provider does not publish this format"
        )
    if source.get("mapping", {}).get("target_schema") != CONTRACT:
        raise SourcePackError("invalid_mapping", f"astronomy sources map to {CONTRACT}")
    documents = declared.get("documents") or []
    if not documents or len(documents) > 60:
        raise SourcePackError(
            "invalid_mapping", "astronomy sources declare 1-60 documents"
        )
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    for document in documents:
        parts = urlsplit(str(document.get("url") or ""))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_mapping",
                "astronomy documents are HTTPS URLs on the endpoint host",
            )
        if document.get("listing", "partial") not in LISTINGS or not document.get(
            "label"
        ):
            raise SourcePackError(
                "invalid_mapping",
                f"documents carry a label and a listing in {LISTINGS}",
            )
        if (
            declared["format"] == "swpc-alerts-json"
            and document.get("listing", "partial") != "partial"
        ):
            raise SourcePackError(
                "invalid_mapping",
                "the SWPC feed is a rolling window, never a complete listing",
            )
    if len(declared.get("objects") or []) > 50 or len(declared.get("hosts") or []) > 25:
        raise SourcePackError(
            "unbounded_source",
            "declared objects (50) or hosts (25) exceed the v1 bound",
        )
    return declared


class AstronomyAdapter:
    """Fetch the declared documents one page at a time on the runtime's same-host transport."""

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
        self.declared = astronomy_declaration(self.source)
        if transport is None:
            from functools import partial

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
            "astronomy": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "documents": [d["label"] for d in self.declared["documents"]],
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
                "astronomy runs fetch the declared documents only",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = self.declared["documents"]
        try:
            index = 0 if cursor is None else int(cursor)
        except ValueError as exc:
            raise SourcePackError(
                "cursor_drift", "cursor is not a document index"
            ) from exc
        if not 0 <= index < len(documents):
            raise SourcePackError(
                "cursor_drift", "cursor is outside the declared documents"
            )
        document = documents[index]
        url = document["url"]
        host = (urlsplit(url).hostname or "").casefold()
        fmt = self.declared["format"]
        accept = (
            "application/json"
            if fmt.endswith("-json")
            else "text/plain, text/csv, text/tab-separated-values"
        )
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": accept},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "document was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "document exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"document refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400 and not (
            status == 404 and fmt in {"jpl-sbdb-json", "jpl-sentry-json"}
        ):
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        try:
            parsed = parse_document(fmt, raw, self.declared, document)
        except AstronomyFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(parsed["records"]) > limit:
            # A truncated listing would read as removals; the page fails instead.
            raise SourcePackError(
                "budget_exhausted",
                "document has more records than the run's result budget",
            )
        records = []
        for record in parsed["records"]:
            source = record["source"]
            records.append(
                {
                    "id": f"{source['provider']}:{record['kind']}:{source['source_record_id']}",
                    "title": f"{record['kind']}: {source['source_record_id']}"[:300],
                    "url": source.get("url") or url,
                    "language": "en",
                    "published_at": source.get("published_at"),
                    "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                    "astronomy_record": record,
                }
            )
        for item in parsed["rejected"]:
            records.append(
                {
                    "id": f"{self.declared['provider']}:rejected:{document['label']}:{item['row']}",
                    "url": url,
                    "rejection": {k: item[k] for k in ("code", "reason", "row")},
                }
            )
        listing_kind = document.get("listing", "partial")
        unidentified = [r for r in parsed["rejected"] if not r.get("source_record_id")]
        scope = parsed["listing"]
        complete = (
            listing_kind == "complete"
            and not unidentified
            and bool(scope["kinds"])
            and bool(scope["scope_keys"])
        )
        receipt = {
            "status": status,
            "document": document["label"],
            "format": fmt,
            "file_sha256": parsed["file_sha256"],
            "source_as_of": document.get("source_as_of"),
            "counts": {
                **parsed["counts"],
                "records": len(parsed["records"]),
                "rejected": len(parsed["rejected"]),
            },
            "missing_columns": parsed["missing_columns"],
            "listing": {
                "complete": complete,
                "kind": listing_kind,
                "kinds": scope["kinds"],
                "scope_keys": scope["scope_keys"],
                **({"id_prefix": scope["id_prefix"]} if scope.get("id_prefix") else {}),
                "seen": sorted(
                    set(parsed["seen"])
                    | {
                        r["source_record_id"]
                        for r in parsed["rejected"]
                        if r.get("source_record_id")
                    }
                ),
                **(
                    {
                        "incomplete_reason": f"{len(unidentified)} row(s) rejected without a stated identifier"
                    }
                    if unidentified and listing_kind == "complete"
                    else {}
                ),
            },
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: AstronomyAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored documents keyed by URL path (+ query)."""
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
    adapter = AstronomyAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            return records

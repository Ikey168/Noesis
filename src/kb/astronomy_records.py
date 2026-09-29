"""Astronomy and Space records: what each publisher states, versioned (#2149, AS02).

One record contract, ``noesis-astronomy-record-v1``, owned by
:mod:`src.kb.astronomy_store`. Every record is *what one source publishes*:

* ``small_body`` - primary designation, number and name as published;
* ``designation`` - one designation (provisional, permanent, survey, comet or
  name), its packed form and, when stated, when it was assigned;
* ``identification`` - one designation the MPC links to a primary designation,
  with the announcing circular or date where the source states it;
* ``orbit_solution`` - publisher, solution ID, epoch (exact Julian date text),
  elements with their native units, observation arc, observation count,
  uncertainty parameter and computation time;
* ``impact_risk_listing`` - a published risk listing (JPL Sentry) with its
  listing or removal date and the published figures quoted as text;
* ``exoplanet`` / ``host_star`` - a parameter set as one archive table and
  reference state it (a composite set is labelled as the archive's composite);
* ``exoplanet_status_assertion`` - the archive's disposition per table, the
  native code verbatim beside its mapping;
* ``space_organisation`` - a GCAT organisation (agencies, providers, launch
  sites) as published;
* ``launch`` / ``launch_outcome`` - a launch and its outcome as coded by the
  source (the native code verbatim);
* ``orbital_object`` - COSPAR and NORAD identifiers, status and decay date;
* ``space_weather_product`` - an SWPC alert, watch, warning or summary with its
  serial number, issue time, NOAA scale and the message text verbatim.

Rules:

* **Nothing computed by Noesis.** Every key is declared per kind; a record with
  an undeclared key (an orbit, ephemeris, risk figure or disposition Noesis
  would have produced) or from a publisher outside :data:`PROVIDERS` is
  refused. Numbers stay the text the source printed.
* **Absent is absent.** A value the source does not state is left out of the
  record (never ``None`` or ``"None"``) and named in ``unknowns``.
* Dates are ISO (``YYYY-MM-DD``), times ISO UTC (``YYYY-MM-DDTHH:MM:SSZ``);
  epochs are exact decimal Julian-date text, never floats.
* Units: records keep the native unit. :func:`normalise_quantity` converts at
  read time through :mod:`src.integrations.units`, so a record's hash never
  depends on whether a converter is installed.

Designations normalise deterministically (:func:`normalize_designation`)
between the MPC packed and unpacked forms; the same function is used by
parsers, the store, identity, queries and monitors.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CONTRACT = "noesis-astronomy-record-v1"
READ_SCOPE = "knowledge:astronomy:read"
WRITE_SCOPE = "knowledge:astronomy:write"
REVIEW_SCOPE = "knowledge:astronomy:review"
DEFAULT_NAMESPACE = "astronomy"
KINDS = (
    "small_body",
    "designation",
    "identification",
    "orbit_solution",
    "impact_risk_listing",
    "exoplanet",
    "host_star",
    "exoplanet_status_assertion",
    "space_organisation",
    "launch",
    "launch_outcome",
    "orbital_object",
    "space_weather_product",
)
PROVIDERS: dict[str, tuple[str, ...]] = {
    "mpc-designations": ("small_body", "designation", "identification"),
    "mpc-orbits": ("orbit_solution",),
    "jpl-sbdb": ("small_body", "designation", "orbit_solution"),
    "jpl-sentry": ("impact_risk_listing",),
    "nasa-exoplanet-archive": ("exoplanet", "host_star", "exoplanet_status_assertion"),
    "gcat": ("space_organisation", "launch", "launch_outcome", "orbital_object"),
    "celestrak-satcat": ("orbital_object",),
    "noaa-swpc": ("space_weather_product",),
}
# The pack's provider (capability) that answers for each record kind.
DOMAIN = {
    "small_body": "small-bodies",
    "designation": "small-bodies",
    "identification": "small-bodies",
    "orbit_solution": "small-bodies",
    "impact_risk_listing": "small-bodies",
    "exoplanet": "exoplanets",
    "host_star": "exoplanets",
    "exoplanet_status_assertion": "exoplanets",
    "space_organisation": "launches",
    "launch": "launches",
    "launch_outcome": "launches",
    "orbital_object": "launches",
    "space_weather_product": "space-weather",
}
DESIGNATION_KINDS = ("provisional", "permanent", "survey", "comet", "name")
DISPOSITIONS = (
    "confirmed",
    "candidate",
    "false_positive",
    "false_alarm",
    "retracted",
    "controversial",
)
OUTCOMES = ("success", "partial", "failure")
SWPC_KINDS = (
    "alert",
    "watch",
    "warning",
    "extended_warning",
    "summary",
    "cancel_watch",
    "cancel_warning",
    "cancel_alert",
    "cancel_summary",
    "other",
)
EXOPLANET_TABLES = ("ps", "pscomppars", "toi", "cumulative", "removed")
PUBLISHERS = {"mpc-orbits": "MPC", "jpl-sbdb": "JPL"}
NOTICE = (
    "Records quote what each publisher states, with its revision and as-of time. Noesis determines no orbit, "
    "issues no impact-risk verdict, validates no exoplanet and gives no operational space-weather advice."
)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/schemas/jsonschema/noesis-astronomy-record-v1.json"
)
DAY_MS = 86_400_000


class AstronomyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise AstronomyError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], scope: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and scope not in scopes:
        raise AstronomyError("unauthorized", f"{scope} is required")


# ------------------------------------------------------------------ dates


_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        )
    )
}


def iso_day(value: Any) -> str | None:
    """``YYYY-MM-DD`` from ISO dates/times, ``YYYYMMDD`` or ``YYYY Mon DD``; ``None`` when not a full date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).date().isoformat()
            if value.tzinfo
            else value.date().isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        parts = match.groups()
    else:
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
        if match:
            parts = match.groups()
        else:
            match = re.match(r"^(\d{4})\s+([A-Za-z]{3})\s+(\d{1,2})\b", text)
            if not match or match.group(2).lower() not in _MONTHS:
                return None
            parts = (
                match.group(1),
                f"{_MONTHS[match.group(2).lower()]:02d}",
                f"{int(match.group(3)):02d}",
            )
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
    except ValueError:
        return None


def iso_time(value: Any) -> str | None:
    """``YYYY-MM-DDTHH:MM:SSZ`` (UTC) from an ISO time, ``YYYY-MM-DD HH:MM[:SS[.fff]]`` or ``YYYY Mon DD HHMM UTC``."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]00:?00| ?UTC)?$",
        text,
    )
    if match:
        day, hour, minute, second = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4) or "00",
        )
    else:
        match = re.match(
            r"^(\d{4})\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):?(\d{2})(?:\s*UTC)?$", text
        )
        if not match or match.group(2).lower() not in _MONTHS:
            return None
        day = f"{match.group(1)}-{_MONTHS[match.group(2).lower()]:02d}-{int(match.group(3)):02d}"
        hour, minute, second = match.group(4), match.group(5), "00"
    try:
        stamp = datetime.fromisoformat(f"{day}T{hour}:{minute}:{second}+00:00")
    except ValueError:
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def end_of_day_ms(day: str) -> int:
    return day_ms(day) + DAY_MS - 1


def time_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def clock_ms(value: str) -> int:
    """A stated date counts as the end of that day (UTC); a stated time as that instant."""
    return time_ms(value) if "T" in value else end_of_day_ms(value)


def observed_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def cutoffs(as_of: Any, acquired_by_ms: int | None = None) -> dict[str, Any]:
    """The knowledge cutoff an answer states: published by the end of ``as_of`` (UTC), acquired by ``acquired_by_ms``."""
    if isinstance(as_of, int) and not isinstance(as_of, bool):
        public = int(as_of)
        label = datetime.fromtimestamp(public / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        day = iso_day(as_of)
        stamp = iso_time(as_of) if as_of is not None and "T" in str(as_of) else None
        if stamp:
            public, label = time_ms(stamp), stamp
        elif day:
            public, label = end_of_day_ms(day), day
        else:
            raise AstronomyError(
                "invalid_request",
                "as_of is a date (YYYY-MM-DD), an ISO time or epoch milliseconds",
            )
    result: dict[str, Any] = {"as_of": label, "published_by_ms": public}
    if acquired_by_ms is not None:
        result["acquired_by_ms"] = int(acquired_by_ms)
    return result


# ------------------------------------------------------------------ Julian dates (exact)


def jd_of_day(day: str) -> str:
    """The Julian date of 0h on a calendar (Gregorian) date as exact decimal text (``2461000.5``)."""
    y, m, d = (int(p) for p in day.split("-"))
    a = (14 - m) // 12
    yy, mm = y + 4800 - a, m + 12 * a - 3
    jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
    return f"{jdn - 1}.5"


def jd_text(value: Any) -> str | None:
    """Exact decimal text of a stated Julian date (no float round trip); ``None`` when not a positive number."""
    text = decimal_text(value)
    return text if text is not None and Decimal(text) > 0 else None


def decimal_text(value: Any) -> str | None:
    """The source's number as canonical decimal text (``"1.50"`` -> ``"1.5"``); ``None`` when not a number."""
    if value is None:
        return None
    text = str(value).strip().replace("D", "E").replace("d", "e")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", "0"} else normalized


# ------------------------------------------------------------------ MPC designations

_CENTURY = {"I": 18, "J": 19, "K": 20, "L": 21}
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_HALF_MONTH = "ABCDEFGHJKLMNOPQRSTUVWXY"  # I is not used
_ORDER = "ABCDEFGHJKLMNOPQRSTUVWXYZ"  # I is not used
_SURVEYS = {"P-L": "PL", "T-1": "T1", "T-2": "T2", "T-3": "T3"}


def _pack_number(number: int) -> str:
    if number < 100_000:
        return f"{number:05d}"
    if number < 620_000:
        return _B62[number // 10_000] + f"{number % 10_000:04d}"
    rest, out = number - 620_000, ""
    for _ in range(4):
        rest, digit = divmod(rest, 62)
        out = _B62[digit] + out
    return "~" + out


def _unpack_number(packed: str) -> int | None:
    if re.fullmatch(r"\d{5}", packed):
        return int(packed)
    if re.fullmatch(r"[A-Za-z]\d{4}", packed) and _B62.index(packed[0]) >= 10:
        return _B62.index(packed[0]) * 10_000 + int(packed[1:])
    if re.fullmatch(r"~[0-9A-Za-z]{4}", packed):
        value = 0
        for ch in packed[1:]:
            value = value * 62 + _B62.index(ch)
        return 620_000 + value
    return None


def _cycle_pack(cycle: int) -> str:
    if cycle < 100:
        return f"{cycle:02d}"
    return _B62[cycle // 10] + str(cycle % 10)


def normalize_designation(value: Any) -> dict[str, Any] | None:
    """The unpacked and packed forms of an MPC designation (packed or unpacked input), or ``None``.

    Returns ``{"unpacked", "packed", "kind"}`` (``packed`` absent for comets and
    names). Numbers unpack as ``"(700001)"``. A name is returned as ``kind:
    name`` with its whitespace collapsed; it links nothing on its own.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    # Numbered: "(700001)", "700001" or a packed number.
    match = re.fullmatch(r"\(?(\d{1,7})\)?", text)
    if match and int(match.group(1)) > 0:
        number = int(match.group(1))
        return {
            "unpacked": f"({number})",
            "packed": _pack_number(number),
            "kind": "permanent",
            "number": number,
        }
    number = _unpack_number(text) if len(text) == 5 else None
    if number:
        return {
            "unpacked": f"({number})",
            "packed": _pack_number(number),
            "kind": "permanent",
            "number": number,
        }
    # Unpacked provisional: "2026 AB12".
    match = re.fullmatch(r"(\d{4}) ?([A-HJ-Y])([A-HJ-Z])(\d{0,3})", text.upper())
    if match and 1800 <= int(match.group(1)) <= 2199:
        year, half, order, cycle = (
            match.group(1),
            match.group(2),
            match.group(3),
            int(match.group(4) or 0),
        )
        century = next(k for k, v in _CENTURY.items() if v == int(year[:2]))
        packed = f"{century}{year[2:]}{half}{_cycle_pack(cycle)}{order}"
        return {
            "unpacked": f"{year} {half}{order}{cycle or ''}",
            "packed": packed,
            "kind": "provisional",
        }
    # Packed provisional: "K26A12B".
    match = re.fullmatch(r"([IJKL])(\d{2})([A-HJ-Y])([0-9A-Za-z])(\d)([A-HJ-Z])", text)
    if match:
        century, yy, half, c1, c2, order = match.groups()
        cycle = _B62.index(c1) * 10 + int(c2)
        year = f"{_CENTURY[century]}{yy}"
        return {
            "unpacked": f"{year} {half}{order}{cycle or ''}",
            "packed": text,
            "kind": "provisional",
        }
    # Survey: "2040 P-L" / packed "PLS2040".
    match = re.fullmatch(r"(\d{4}) (P-L|T-[123])", text.upper())
    if match:
        return {
            "unpacked": f"{match.group(1)} {match.group(2)}",
            "packed": _SURVEYS[match.group(2)] + "S" + match.group(1),
            "kind": "survey",
        }
    match = re.fullmatch(r"(PL|T1|T2|T3)S(\d{4})", text)
    if match:
        survey = next(k for k, v in _SURVEYS.items() if v == match.group(1))
        return {
            "unpacked": f"{match.group(2)} {survey}",
            "packed": text,
            "kind": "survey",
        }
    # Comets: "C/2026 A1", "P/2026 B2", "12P".
    match = re.fullmatch(r"([CPDXAI])/(\d{4}) ([A-HJ-Y]\d{1,3})(-[A-Z])?", text.upper())
    if match:
        return {"unpacked": text.upper(), "kind": "comet"}
    match = re.fullmatch(r"(\d{1,4})([PDI])(?:-[A-Z]+)?", text.upper())
    if match:
        return {"unpacked": text.upper(), "kind": "comet"}
    return {"unpacked": text, "kind": "name"}


def designation_key(value: Any) -> str | None:
    """The one equivalence every lookup, link and monitor uses: the unpacked designation (names casefolded)."""
    normalized = normalize_designation(value)
    if normalized is None:
        return None
    if normalized["kind"] == "name":
        return "name:" + normalized["unpacked"].casefold()
    return normalized["unpacked"]


def fold(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^0-9a-z]+", " ", text).strip()


def object_name_key(value: Any) -> str:
    """Exoplanet, host and catalogue names compared case- and punctuation-insensitively (``Kepler-22 b``)."""
    return fold(value).replace(" ", "")


def normalize_cospar(value: Any) -> str | None:
    """``YYYY-NNNP{PP}`` (upper case), from ``2026-001A`` or GCAT's ``2026-001 A`` (piece separate)."""
    text = re.sub(r"\s+", "", str(value or "")).upper()
    match = re.fullmatch(r"(\d{4})-?(\d{3})([A-Z]{1,3})", text)
    return f"{match.group(1)}-{match.group(2)}{match.group(3)}" if match else None


def normalize_norad(value: Any) -> str | None:
    text = str(value or "").strip()
    return str(int(text)) if re.fullmatch(r"\d{1,9}", text) and int(text) > 0 else None


def normalize_bibcode(value: Any) -> str | None:
    """A 19-character ADS bibcode (dots kept, URL-decoded); ``None`` when the text is not one."""
    from urllib.parse import unquote

    text = unquote(str(value or "").strip())
    return (
        text
        if re.fullmatch(
            r"\d{4}[A-Za-z&.]{5}[0-9A-Za-z.]{4}[A-Za-z.][0-9.]{4}[A-Z.]", text
        )
        else None
    )


def normalize_doi(value: Any) -> str | None:
    from urllib.parse import unquote

    text = unquote(str(value or "").strip())
    text = (
        re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", text, flags=re.I)
        .strip()
        .rstrip(".,;")
    )
    return text.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", text) else None


def parse_reference(text: Any) -> dict[str, Any] | None:
    """A cited reference as stated: verbatim text plus the bibcode and DOI it links, if any. ``None`` when empty.

    Handles the archive's ``<a refstr=... href=https://ui.adsabs.harvard.edu/abs/<bibcode>/abstract>`` form and
    ``doi.org`` links. Nothing is looked up; a bibcode and a DOI are paired only because the text states both.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    reference: dict[str, Any] = {"text": raw}
    label = re.sub(r"<[^>]+>", "", raw).strip()
    if label and label != raw:
        reference["label"] = label
    ads = re.search(r"adsabs\.harvard\.edu/abs/([^/\s\"'>]+)", raw)
    if ads and normalize_bibcode(ads.group(1)):
        reference["bibcode"] = normalize_bibcode(ads.group(1))
    doi = re.search(r"(?:doi\.org/|doi:)(10\.\d{4,9}/[^\s\"'<>]+)", raw, flags=re.I)
    if doi and normalize_doi(doi.group(1)):
        reference["doi"] = normalize_doi(doi.group(1))
    href = re.search(r"href=[\"']?(https?://[^\s\"'>]+)", raw)
    if href:
        reference["url"] = href.group(1)
    return reference


def normalize_circular(value: Any) -> str | None:
    """MPC circular references normalised: ``MPEC 2026-C99``, ``MPC 123456``, ``MPO 123456``."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    match = re.fullmatch(r"(?:MPEC ?)?(\d{4})-([A-Z]\d{1,3})", text)
    if match and text.startswith(("MPEC", "1", "2")):
        return f"MPEC {match.group(1)}-{match.group(2)}"
    match = re.fullmatch(r"E(\d{4})-([A-Z]\d{1,3})", text)
    if match:
        return f"MPEC {match.group(1)}-{match.group(2)}"
    match = re.fullmatch(r"(MPC|MPO|MPS) ?(\d{1,7})", text)
    if match:
        return f"{match.group(1)} {int(match.group(2))}"
    return None


# ------------------------------------------------------------------ units (read time only)

# Native unit -> (pint expression, target) for conversions the Pint default registry defines.
_PHYSICAL = {
    "au": ("astronomical_unit", "kilometer"),
    "d": ("day", "day"),
    "deg": ("degree", "degree"),
    "deg/d": ("degree / day", "degree / day"),
    "km": ("kilometer", "kilometer"),
    "yr": ("year", "day"),
}
# IAU 2015 Resolution B3 nominal equatorial radii (verify); masses have no nominal definition and stay native.
_NOMINAL_LENGTH_M = {"R_earth": "6378100", "R_jupiter": "71492000"}


def normalise_quantity(value: str, unit: str | None) -> dict[str, Any]:
    """Native value and unit kept; a normalised value added through :mod:`src.integrations.units` when defined.

    Returns ``{"value", "unit", "normalised": {...}}`` or a ``normalisation`` status saying why none exists
    (``no_unit``, ``no_definition``, ``unavailable`` when Pint is not installed).
    """
    result: dict[str, Any] = {"value": value, **({"unit": unit} if unit else {})}
    if not unit:
        result["normalisation"] = "no_unit"
        return result
    try:
        from src.integrations.units import convert_physical, convert_registered
    except (
        ImportError
    ):  # pragma: no cover - the module itself has no import-time dependency
        result["normalisation"] = "unavailable"
        return result
    try:
        if unit in _PHYSICAL:
            source, target = _PHYSICAL[unit]
            converted = convert_physical(value, source, target, precision=6)
        elif unit in _NOMINAL_LENGTH_M:
            length = {"dimension": {"length": 1}, "offset": "0"}
            converted = convert_registered(
                value,
                {**length, "unit_id": unit, "factor": _NOMINAL_LENGTH_M[unit]},
                {**length, "unit_id": "km", "factor": "1000"},
                precision=3,
            )
            converted["result"]["unit"] = "kilometer"
        else:
            result["normalisation"] = "no_definition"
            return result
    except ImportError:
        result["normalisation"] = "unavailable"
        result["reason"] = (
            "the optional Pint dependency is not installed; the native value stands"
        )
        return result
    except Exception as exc:  # noqa: BLE001 - a conversion failure leaves the native value
        result["normalisation"] = "failed"
        result["reason"] = str(getattr(exc, "code", type(exc).__name__))
        return result
    output = converted.get("result") or converted.get("output") or {}
    result["normalised"] = {
        "value": output.get("value"),
        "unit": output.get("unit"),
        "via": "src.integrations.units",
    }
    return result


# ------------------------------------------------------------------ validation

_SOURCE = {
    "provider",
    "source_record_id",
    "url",
    "locator",
    "published_at",
    "retrieved_at_ms",
    "attribution",
    "document",
}
_REFERENCE = {"text", "label", "bibcode", "doi", "url", "circular"}
_KIND_FIELDS: dict[str, dict[str, set[str]]] = {
    "small_body": {
        "required": {"primary_designation"},
        "optional": {
            "packed",
            "number",
            "name",
            "object_kind",
            "stated_designations",
            "spk_id",
        },
    },
    "designation": {
        "required": {"designation", "designation_kind"},
        "optional": {"packed", "object_designation", "assigned_at"},
    },
    "identification": {
        "required": {"designation", "identified_with"},
        "optional": {"permanent", "announced_in", "announced_on"},
    },
    "orbit_solution": {
        "required": {
            "object_designation",
            "publisher",
            "solution_id",
            "epoch",
            "elements",
        },
        "optional": {
            "arc",
            "n_obs_used",
            "n_opp",
            "uncertainty",
            "computed_at",
            "computer",
            "reference",
            "orbit_class",
        },
    },
    "impact_risk_listing": {
        "required": {"object_designation", "publisher", "listing_status"},
        "optional": {"listing_date", "removed_at", "figures", "method"},
    },
    "exoplanet": {
        "required": {"name", "source_table", "composite"},
        "optional": {
            "host",
            "reference",
            "parameters",
            "identifiers",
            "discovery",
            "default_set",
        },
    },
    "host_star": {
        "required": {"name", "source_table"},
        "optional": {"identifiers", "parameters", "reference"},
    },
    "exoplanet_status_assertion": {
        "required": {
            "object_name",
            "object_scheme",
            "source_table",
            "native_disposition",
        },
        "optional": {
            "host",
            "disposition",
            "reference",
            "asserted_at",
            "note",
            "identifiers",
        },
    },
    "space_organisation": {
        "required": {"code", "name"},
        "optional": {
            "short_name",
            "state_code",
            "org_type",
            "org_class",
            "location",
            "latitude",
            "longitude",
            "parent",
        },
    },
    "launch": {
        "required": {"launch_tag"},
        "optional": {
            "launch_class",
            "time",
            "stated_time",
            "vehicle",
            "variant",
            "flight",
            "mission",
            "site_code",
            "pad",
            "agency_codes",
            "native_code",
            "cospar_launch",
        },
    },
    "launch_outcome": {
        "required": {"launch_tag", "native_code"},
        "optional": {"outcome"},
    },
    "orbital_object": {
        "required": set(),
        "optional": {
            "cospar",
            "norad",
            "jcat",
            "name",
            "object_type",
            "owner",
            "status",
            "launch_date",
            "decay_date",
            "launch_tag",
            "site",
            "stated_decay_date",
        },
    },
    "space_weather_product": {
        "required": {"product_id", "serial", "issue_time", "product_kind", "message"},
        "optional": {"message_code", "scales", "references", "title"},
    },
}
# What a record should state; missing ones are listed in unknowns (never filled in).
_EXPECTED = {
    "small_body": ("name", "number"),
    "designation": ("assigned_at",),
    "identification": ("announced_in", "announced_on"),
    "orbit_solution": ("arc", "n_obs_used", "uncertainty", "computed_at"),
    "impact_risk_listing": ("listing_date",),
    "exoplanet": ("host", "reference"),
    "host_star": (),
    "exoplanet_status_assertion": ("disposition", "reference", "asserted_at"),
    "space_organisation": (),
    "launch": ("time", "vehicle", "site_code"),
    "launch_outcome": ("outcome",),
    "orbital_object": ("cospar", "norad", "status"),
    "space_weather_product": ("message_code", "scales"),
}


def _fail(message: str) -> None:
    raise AstronomyError("invalid_record", message)


def _text(value: Any, field: str, limit: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} is bounded non-empty text as the source states it")
    return value


def _clean(value: Any) -> Any:
    """Drop absent values recursively; ``None``, empty strings and the string ``"None"`` are absence."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            cleaned = _clean(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    if isinstance(value, list):
        return [c for c in (_clean(v) for v in value) if c is not None]
    if value is None or (
        isinstance(value, str) and value.strip() in {"", "None", "null"}
    ):
        return None
    return value


def _check_reference(ref: Any, field: str) -> None:
    if not isinstance(ref, Mapping) or set(ref) - _REFERENCE or not ref.get("text"):
        _fail(f"{field} is a stated reference with its verbatim text")


def _check_day(value: Any, field: str) -> None:
    if iso_day(value) != value:
        _fail(f"{field} is an ISO date")


def _check_when(value: Any, field: str) -> None:
    if not (iso_day(value) == value or iso_time(value) == value):
        _fail(f"{field} is an ISO date or an ISO UTC time")


def _check_quantities(values: Any, field: str) -> None:
    if not isinstance(values, Mapping) or not values:
        _fail(f"{field} maps names to stated values")
    for name, item in values.items():
        if not isinstance(item, Mapping) or set(item) - {
            "value",
            "unit",
            "sigma",
            "err1",
            "err2",
            "limit",
        }:
            _fail(f"{field}.{name} is a stated value with its native unit")
        for key in ("value", "sigma", "err1", "err2"):
            if key in item and decimal_text(item[key]) != item[key]:
                _fail(f"{field}.{name}.{key} is the source's number as decimal text")


def compute_unknowns(record: Mapping[str, Any]) -> list[str]:
    missing = [f for f in _EXPECTED[record["kind"]] if f not in record]
    if "published_at" not in record["source"]:
        missing.append("source.published_at")
    return sorted(missing)


def validate_record(record: Any) -> dict[str, Any]:
    """Validate one record as its source states it; returns the canonical record with ``contract`` and ``unknowns``.

    Refuses undeclared keys, publishers outside :data:`PROVIDERS`, kinds a provider does not publish, numbers
    that are not the source's text and dispositions or outcomes outside the published vocabulary.
    """
    if not isinstance(record, Mapping):
        _fail("a record is an object")
    record = _clean(dict(record))
    record.pop("unknowns", None)
    record.pop("contract", None)
    kind = record.get("kind")
    if kind not in KINDS:
        _fail(f"kind is one of {KINDS}")
    source = record.get("source")
    if not isinstance(source, Mapping) or set(source) - _SOURCE:
        _fail("source states provider, source_record_id and locator fields only")
    provider = source.get("provider")
    if provider not in PROVIDERS:
        _fail("records come from a declared publisher; Noesis publishes none")
    if kind not in PROVIDERS[provider]:
        _fail(f"{provider} does not publish {kind} records")
    _text(source.get("source_record_id"), "source.source_record_id", 500)
    if "published_at" in source:
        _check_when(source["published_at"], "source.published_at")
    if "retrieved_at_ms" in source and (
        type(source["retrieved_at_ms"]) is not int or source["retrieved_at_ms"] < 0
    ):
        _fail("source.retrieved_at_ms is epoch milliseconds")
    fields = _KIND_FIELDS[kind]
    extra = set(record) - {"kind", "source"} - fields["required"] - fields["optional"]
    if extra:
        _fail(
            f"undeclared fields for {kind}: {sorted(extra)} (a record never holds a Noesis-computed value)"
        )
    missing = fields["required"] - set(record)
    if missing:
        _fail(f"{kind} needs {sorted(missing)}")
    if kind == "small_body":
        _text(record["primary_designation"], "primary_designation", 100)
        if "number" in record and (
            type(record["number"]) is not int or record["number"] < 1
        ):
            _fail("number is the positive integer the source states")
        if "stated_designations" in record and not all(
            isinstance(d, str) for d in record["stated_designations"]
        ):
            _fail("stated_designations lists designation text")
    elif kind == "designation":
        if record["designation_kind"] not in DESIGNATION_KINDS:
            _fail(f"designation_kind is one of {DESIGNATION_KINDS}")
        if "assigned_at" in record:
            _check_day(record["assigned_at"], "assigned_at")
    elif kind == "identification":
        if "announced_on" in record:
            _check_day(record["announced_on"], "announced_on")
        if designation_key(record["designation"]) == designation_key(
            record["identified_with"]
        ):
            _fail("an identification links two different designations")
    elif kind == "orbit_solution":
        if (
            record["publisher"] not in PUBLISHERS.values()
            or PUBLISHERS.get(provider) != record["publisher"]
        ):
            _fail("publisher is the source's own publisher (MPC or JPL)")
        epoch = record["epoch"]
        if (
            not isinstance(epoch, Mapping)
            or set(epoch) - {"jd", "calendar", "scale", "stated"}
            or jd_text(epoch.get("jd")) != epoch.get("jd")
        ):
            _fail("epoch states an exact Julian date as decimal text")
        _check_quantities(record["elements"], "elements")
        arc = record.get("arc")
        if arc is not None and (
            not isinstance(arc, Mapping)
            or set(arc) - {"first_obs", "last_obs", "days", "stated"}
        ):
            _fail("arc states first_obs, last_obs, days or the stated arc")
        for key in ("n_obs_used", "n_opp"):
            if key in record and (type(record[key]) is not int or record[key] < 0):
                _fail(f"{key} is the count the source states")
        uncertainty = record.get("uncertainty")
        if uncertainty is not None and (
            not isinstance(uncertainty, Mapping)
            or set(uncertainty) != {"parameter", "value"}
        ):
            _fail(
                "uncertainty names the source's parameter (U or condition_code) and its value"
            )
        if "computed_at" in record:
            _check_when(record["computed_at"], "computed_at")
    elif kind == "impact_risk_listing":
        if record["listing_status"] not in {"listed", "removed"}:
            _fail("listing_status is listed or removed as published")
        for key in ("listing_date", "removed_at"):
            if key in record:
                _check_when(record[key], key)
        figures = record.get("figures")
        if figures is not None and (
            not isinstance(figures, Mapping)
            or not all(isinstance(v, str) for v in figures.values())
        ):
            _fail("figures are quoted as the published text")
    elif kind in {"exoplanet", "host_star"}:
        if record["source_table"] not in EXOPLANET_TABLES:
            _fail(f"source_table is one of {EXOPLANET_TABLES}")
        if kind == "exoplanet" and type(record["composite"]) is not bool:
            _fail("composite says whether the archive labels the set its composite")
        if (
            kind == "exoplanet"
            and record["composite"]
            and record["source_table"] != "pscomppars"
        ):
            _fail("only the archive's composite table is labelled composite")
        if "parameters" in record:
            _check_quantities(record["parameters"], "parameters")
        if "reference" in record:
            _check_reference(record["reference"], "reference")
        if "identifiers" in record and not all(
            isinstance(v, str) for v in record["identifiers"].values()
        ):
            _fail("identifiers are the source-stated cross-identifiers")
    elif kind == "exoplanet_status_assertion":
        if record["source_table"] not in EXOPLANET_TABLES:
            _fail(f"source_table is one of {EXOPLANET_TABLES}")
        if record["object_scheme"] not in {"pl_name", "toi", "koi"}:
            _fail("object_scheme is pl_name, toi or koi")
        if "disposition" in record and record["disposition"] not in DISPOSITIONS:
            _fail(f"disposition is one of {DISPOSITIONS} as published")
        if "reference" in record:
            _check_reference(record["reference"], "reference")
        if "asserted_at" in record:
            _check_when(record["asserted_at"], "asserted_at")
    elif kind == "launch":
        if "time" in record:
            _check_when(record["time"], "time")
        if "agency_codes" in record and not all(
            isinstance(c, str) for c in record["agency_codes"]
        ):
            _fail("agency_codes lists the source's codes")
    elif kind == "launch_outcome":
        if "outcome" in record and record["outcome"] not in OUTCOMES:
            _fail(f"outcome is one of {OUTCOMES} as coded by the source")
    elif kind == "orbital_object":
        if not any(k in record for k in ("cospar", "norad", "jcat")):
            _fail("an orbital object states a COSPAR, NORAD or GCAT identifier")
        if (
            "cospar" in record
            and normalize_cospar(record["cospar"]) != record["cospar"]
        ):
            _fail("cospar is a normalised international designator")
        if "norad" in record and normalize_norad(record["norad"]) != record["norad"]:
            _fail("norad is the catalogue number without padding")
        for key in ("launch_date", "decay_date"):
            if key in record:
                _check_when(record[key], key)
    elif kind == "space_weather_product":
        if record["product_kind"] not in SWPC_KINDS:
            _fail(f"product_kind is one of {SWPC_KINDS}")
        _check_when(record["issue_time"], "issue_time")
        _text(record["message"], "message")
        references = record.get("references")
        if references is not None and (
            not isinstance(references, Mapping)
            or set(references) - {"cancels", "extends"}
        ):
            _fail("references name the serial numbers a product cancels or extends")
        if "scales" in record and not all(
            re.fullmatch(r"[GSR][1-5]", s) for s in record["scales"]
        ):
            _fail("scales are NOAA scale levels (G1-G5, S1-S5, R1-R5) as stated")
    record["contract"] = CONTRACT
    record["unknowns"] = compute_unknowns(record)
    return record


def semantic(record: Mapping[str, Any]) -> dict[str, Any]:
    """The source-independent content a revision is compared by (no locators, documents, clocks or unknowns)."""
    body = {k: v for k, v in record.items() if k not in {"unknowns", "contract"}}
    source = dict(body.get("source") or {})
    body["source"] = {
        k: source[k]
        for k in ("provider", "source_record_id", "published_at")
        if k in source
    }
    return body


def record_id(namespace: str, kind: str, provider: str, source_record_id: str) -> str:
    return "astro:" + digest([namespace, kind, provider, source_record_id])[:24]


def object_keys(record: Mapping[str, Any]) -> list[str]:
    """Every source-stated key a record can be looked up by (the shared equivalence, never fuzzy)."""
    kind, keys = record["kind"], set()

    def des(value: Any) -> None:
        key = designation_key(value)
        if key:
            keys.add("des:" + key)

    if kind == "small_body":
        des(record["primary_designation"])
        if "number" in record:
            des(str(record["number"]))
        if "name" in record:
            des(record["name"])
        for value in record.get("stated_designations") or []:
            des(value)
    elif kind == "designation":
        des(record["designation"])
        if "object_designation" in record:
            des(record["object_designation"])
    elif kind == "identification":
        des(record["designation"])
        des(record["identified_with"])
        if "permanent" in record:
            des(record["permanent"])
    elif kind in {"orbit_solution", "impact_risk_listing"}:
        des(record["object_designation"])
    elif kind in {"exoplanet", "host_star"}:
        keys.add(
            ("planet:" if kind == "exoplanet" else "host:")
            + object_name_key(record["name"])
        )
        if record.get("host"):
            keys.add("host:" + object_name_key(record["host"]))
        for scheme, value in (record.get("identifiers") or {}).items():
            keys.add(f"{scheme}:{object_name_key(value)}")
    elif kind == "exoplanet_status_assertion":
        prefix = {"pl_name": "planet", "toi": "toi", "koi": "koi"}[
            record["object_scheme"]
        ]
        keys.add(f"{prefix}:{object_name_key(record['object_name'])}")
        if record.get("host"):
            keys.add("host:" + object_name_key(record["host"]))
        for scheme, value in (record.get("identifiers") or {}).items():
            keys.add(f"{scheme}:{object_name_key(value)}")
    elif kind == "space_organisation":
        keys.add("org:" + record["code"].upper())
    elif kind in {"launch", "launch_outcome"}:
        keys.add("launch:" + record["launch_tag"])
        for code in record.get("agency_codes") or []:
            keys.add("org:" + code.upper())
        if record.get("site_code"):
            keys.add("org:" + record["site_code"].upper())
    elif kind == "orbital_object":
        for key, prefix in (
            ("cospar", "cospar"),
            ("norad", "norad"),
            ("jcat", "jcat"),
            ("launch_tag", "launch"),
        ):
            if record.get(key):
                keys.add(f"{prefix}:{record[key]}")
    elif kind == "space_weather_product":
        keys.add("swpc:" + record["product_id"])
    return sorted(keys)


# ------------------------------------------------------------------ schema registry


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any
) -> list[dict[str, Any]]:
    """Register the record contract as a schema module in the shared registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "astronomy-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": schema(),
        "owner": "astronomy.small-bodies",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {
            "kind": "imported",
            "source": f"contracts/schemas/jsonschema/{CONTRACT}.json",
        },
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            "astronomy-schema:astronomy-record:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]

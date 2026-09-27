"""Official election-result acquisition for the Political ``elections`` feature (#1908, L01/L03/L04).

One native connector, ``election-results``, fetches one documented,
machine-readable result publication per source and parses it into a
*release*: its publication date, vintage kind, file digest and one *contest*
per reporting unit and ballot, with the figures exactly as published. Four
documented formats are supported:

* ``de-btw-kerg-csv`` - the Bundeswahlleiterin "Leitformat" result file
  (``kerg2.csv``): one row per area, group and vote (first/second vote), with
  the preliminary (``vorläufig``) or final (``endgültig``) state stated in the
  file header;
* ``de-be-wahlkreis-csv`` - Berlin Landeswahlleitung result tables (one row per
  area and vote type, one column per party, the ``Ergebnisstand`` per row);
* ``gb-ec-candidates-csv`` - UK Electoral Commission candidate-level results
  per Westminster constituency (ONS codes);
* ``us-medsl-county-csv`` - MIT Election Data and Science Lab county returns
  (``countypres``-style: FIPS codes, vote ``mode`` and a dataset ``version``).

Figures are never recomputed: counts, published shares, turnout, invalid
votes and mandates are copied as the file states them (a missing figure stays
missing), and vote modes are never summed. Jurisdiction rules (first/second
vote, first-past-the-post, county reporting and certification authority) come
from the source manifest and are kept as contest attributes cited to the
source; nothing normalises vote-share semantics across jurisdictions.
Constituency identifiers keep the provider's scheme and value (Wahlkreis
number, ONS code, five-digit FIPS) and carry the provider's geometry
reference (layer and vintage) - geometry itself is stored only through the
Geospatial feature store.

A release is all-or-nothing: more contests than the run's result budget is a
``budget_exhausted`` failure rather than a truncated release.

``PROVIDER_CONTRACTS`` records the L01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-election-release-v1"
CONNECTOR = "election-results"
VINTAGE_KINDS = ("preliminary", "certified", "recount", "corrected")
# format id -> provider it belongs to, the jurisdiction and whether the file states its own vintage kind
FORMATS: dict[str, dict[str, Any]] = {
    "de-btw-kerg-csv": {
        "provider": "bundeswahlleiterin",
        "jurisdiction": "DE",
        "states_vintage": True,
    },
    "de-be-wahlkreis-csv": {
        "provider": "berlin-landeswahlleitung",
        "jurisdiction": "DE-BE",
        "states_vintage": True,
    },
    "gb-ec-candidates-csv": {
        "provider": "uk-electoral-commission",
        "jurisdiction": "GB",
        "states_vintage": False,
    },
    "us-medsl-county-csv": {
        "provider": "mit-election-lab",
        "jurisdiction": "US",
        "states_vintage": False,
    },
}
# Identifier schemes of reporting units; each keeps the provider's value, normalised the same way everywhere.
UNIT_SCHEMES = (
    "de-bund",
    "de-land",
    "de-bt-wahlkreis",
    "de-be-land",
    "de-be-bezirk",
    "de-be-wahlkreis",
    "gb-ons-pcon",
    "us-fips-county",
    "us-medsl-county-name",
)
MAX_CONTESTS = 50_000
MAX_ROWS = 2_000_000
REVIEW_BOUNDARY = (
    "Published figures only, as each authority released them: preliminary and certified results stay separate "
    "vintages, a poll is never a result, and nothing predicts seats or outcomes, aggregates polls into one number "
    "or reads news framing as a cause."
)

# L01 access decisions. Terms, endpoints and file layouts below are recorded from
# the providers' published documentation as known without network access; every
# item marked ``verify`` must be checked against the live terms and files before a
# dated live run is accepted (#2016).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "bundeswahlleiterin": {
        "publisher": "Die Bundeswahlleiterin (Federal Returning Officer, Germany)",
        "delivers": ["results", "constituency-geometry-reference"],
        "access": "Open-data CSV result files per Bundestag election (Leitformat kerg/kerg2) and constituency "
        "shapefiles per election, linked from bundeswahlleiterin.de/service/opendata (verify the per-election "
        "directory and whether the preliminary file is replaced in place by the final one)",
        "format": "de-btw-kerg-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "identifiers": [
            "Wahlkreis number (1-299, per boundary vintage)",
            "Land number (1-16)",
            "party short names as published (no stable party code)",
        ],
        "vintages": "preliminary result on election night (vorläufiges Ergebnis), final result after the "
        "Bundeswahlausschuss determines it (endgültiges Ergebnis); the file header states which (verify wording)",
        "rules": "mixed-member proportional: first vote (Erststimme) for a constituency candidate by plurality, "
        "second vote (Zweitstimme) for a Land list; recounts are ordered by the district returning officers and "
        "enter the final result; corrections after certification go through the Bundestag's scrutiny procedure",
        "corrections": "a changed final figure is a new publication of the file (observed as a new vintage)",
        "cadence": "per election; preliminary within hours, final within about three weeks",
        "geometry": "Wahlkreis shapefiles per election (ESRI shapefile, ETRS89/UTM zone 32N; verify CRS per vintage)",
        "terms": "Datenlizenz Deutschland - Namensnennung 2.0 with source attribution (verify on the imprint)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the kerg2 column names, header comment and date line must be "
        "verified against a published file",
    },
    "berlin-landeswahlleitung": {
        "publisher": "Landeswahlleitung Berlin / Amt für Statistik Berlin-Brandenburg (wahlen-berlin.de)",
        "delivers": ["results", "constituency-geometry-reference"],
        "access": "Result tables per election as CSV/XLSX on wahlen-berlin.de and mirrored on daten.berlin.de "
        "(verify the CSV layout, delimiter and whether both portals serve the same file)",
        "format": "de-be-wahlkreis-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "identifiers": [
            "Wahlkreis number within the Land (as published)",
            "Bezirk number (01-12)",
        ],
        "vintages": "vorläufiges Ergebnis then endgültiges Ergebnis; repeat elections (Wiederholungswahl) are "
        "separate elections, never corrections of the original",
        "rules": "Abgeordnetenhaus: first vote per Wahlkreis (plurality), second vote for Land or Bezirk lists",
        "corrections": "republished tables (observed as new vintages)",
        "cadence": "per election",
        "geometry": "Wahlkreis and Bezirk boundaries on daten.berlin.de / FIS-Broker WFS (verify layer ids and "
        "vintage per election)",
        "terms": "CC BY 3.0 DE for daten.berlin.de datasets (verify per dataset)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; column names and the Ergebnisstand column must be verified",
    },
    "daten-berlin": {
        "publisher": "Land Berlin open-data portal (daten.berlin.de)",
        "delivers": ["results (mirror)", "constituency-geometry-reference"],
        "access": "Catalogue (CKAN) records pointing at the Landeswahlleitung files and FIS-Broker geometry "
        "services; used for dataset discovery and licence metadata, results are acquired from the Landeswahlleitung "
        "file the record links (verify)",
        "format": None,
        "authentication": "none",
        "identifiers": ["CKAN dataset id"],
        "terms": "per-dataset licence in the catalogue record (verify)",
        "access_decision": "not-implemented",
        "reason": "catalogue only: no second copy of the Berlin results is acquired; geometry is taken from the "
        "Geospatial pack's existing Berlin collections",
    },
    "uk-electoral-commission": {
        "publisher": "The Electoral Commission (United Kingdom)",
        "delivers": ["results"],
        "access": "Results and turnout data per election as CSV/XLSX downloads from electoralcommission.org.uk "
        "research-reports-and-data/electoral-data (verify the per-election file URL and column names)",
        "format": "gb-ec-candidates-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per run",
        "identifiers": [
            "ONS constituency code (E14/W07/S14/N06)",
            "party register name and abbreviation as published",
        ],
        "vintages": "results as declared by returning officers; the Commission's file is published after the "
        "declarations (certified); recounts happen before declaration and are not separate publications",
        "rules": "first-past-the-post single-member constituencies; returning officer declares the result",
        "corrections": "a corrected file is a new publication (observed as a correction vintage)",
        "cadence": "per election",
        "geometry": "ONS Westminster Parliamentary Constituency boundaries (e.g. PCON July 2024), not published by "
        "the Commission",
        "terms": "Open Government Licence v3.0 (verify on the Commission's copyright notice)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the file layout must be verified; the publication date is taken from "
        "the HTTP Last-Modified header because the file states none",
    },
    "mit-election-lab": {
        "publisher": "MIT Election Data and Science Lab (MEDSL)",
        "delivers": ["results (compiled)"],
        "access": "Dataset releases on Harvard Dataverse (e.g. county presidential returns 2000-2020, "
        "countypres) as CSV; Dataverse file access redirects to a storage host, which the runtime's same-host "
        "redirect policy refuses - a live run needs the direct file URL (verify)",
        "format": "us-medsl-county-csv",
        "authentication": "none",
        "rate_limits": "Dataverse API limits (verify)",
        "identifiers": [
            "county FIPS (five digits)",
            "state postal code",
            "dataset version (YYYYMMDD)",
        ],
        "vintages": "MEDSL compiles state-certified returns; each dataset version is a source revision and a "
        "changed figure in a new version is a correction",
        "rules": "US reporting units are counties (and county-equivalents); certification authority is per state; "
        "vote modes (election day, early, absentee, provisional, TOTAL) are reported as published and never summed",
        "corrections": "new dataset versions",
        "cadence": "irregular releases after certification",
        "geometry": "US Census TIGER/Line county boundaries by vintage (not published by MEDSL)",
        "terms": "Dataverse terms and the dataset's licence (CC0 is typical for MEDSL datasets; verify per "
        "dataset) with the requested citation; retention follows those terms",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; live access needs a direct, same-host file URL",
    },
    "parlgov": {
        "publisher": "ParlGov (Döring, Huber and Manow)",
        "delivers": ["party-reference-data", "cabinet-reference-data"],
        "access": "Release CSV/SQLite downloads (verify current release)",
        "format": None,
        "identifiers": ["ParlGov party id", "election id"],
        "terms": "CC BY-SA (verify the licence version of the current release); share-alike obligations must be "
        "reviewed before storing derived records",
        "access_decision": "not-implemented",
        "reason": "party reference data is not needed for results; party identity uses reviewable decisions; "
        "share-alike terms need a decision before any reuse",
    },
    "wahlrecht-de": {
        "publisher": "wahlrecht.de (Wilko Zicht, Martin Fehndrich)",
        "delivers": ["poll-listing"],
        "access": "HTML tables of published polls per institute (wahlrecht.de/umfragen)",
        "format": None,
        "identifiers": ["institute", "publication date"],
        "terms": "no redistribution licence stated (verify in writing)",
        "access_decision": "not-implemented",
        "reason": "an aggregator listing without confirmed redistribution terms; polls are imported from their "
        "publishers' own releases instead, link-only unless the publisher's terms allow the figures",
    },
    "poll-publisher-release": {
        "publisher": "each poll publisher (institute or commissioning outlet)",
        "delivers": ["poll-readings"],
        "access": "a publisher's own release file or table, imported per release through the polls connector "
        "with a column map; terms recorded per series",
        "format": "poll-release-csv",
        "identifiers": ["publisher", "fieldwork window", "question"],
        "terms": "per publisher; a series whose terms do not allow redistribution is stored as link-only metadata "
        "(publisher, method, fieldwork dates, sample size, URL) without figures",
        "access_decision": "unverified-live",
        "reason": "operator-supplied release files only; no crawler",
    },
}


class ElectionFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def slug(value: Any) -> str:
    """A stable key part for a published label (letters of any script kept, punctuation dropped)."""
    return "-".join(re.sub(r"[\W_]+", " ", str(value or "").casefold()).split())


def normalize_unit_id(scheme: str, value: Any) -> str | None:
    """One normal form per scheme, used for results and geometry alike (``075`` and ``75`` are one Wahlkreis)."""
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "N/A", "NULL"}:
        return None
    if scheme in {"de-bt-wahlkreis", "de-land", "de-bund", "de-be-bezirk"}:
        digits = re.sub(r"\.0+$", "", text)
        if not digits.isdigit():
            return None
        return str(int(digits)).zfill(3 if scheme == "de-bt-wahlkreis" else 2)
    if scheme == "us-fips-county":
        digits = re.sub(r"\.0+$", "", text)
        if not digits.isdigit() or len(digits) > 5:
            return None
        return digits.zfill(5)
    if scheme == "gb-ons-pcon":
        code = text.upper().replace(" ", "")
        return code if re.fullmatch(r"[ENSW]\d{8}", code) else None
    return " ".join(text.upper().split())


def _count(value: Any) -> int | None:
    text = str(value or "").strip().replace(" ", "").replace(" ", "")
    if not text or text in {"-", "."}:
        return None
    text = re.sub(r"\.0+$", "", text)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):  # German thousands separator
        text = text.replace(".", "")
    text = text.replace(",", "")
    if not text.isdigit():
        raise ElectionFormatError("schema_drift", f"count {value!r} is not an integer")
    return int(text)


def _share(value: Any) -> str | None:
    """A published percentage as a decimal string, exactly as published (German comma decimal accepted)."""
    text = str(value or "").strip().replace("%", "").replace(" ", "")
    if not text or text == "-":
        return None
    text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ElectionFormatError("schema_drift", f"share {value!r} is not a decimal")
    return text


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return (
                datetime.strptime(
                    text[:10] if pattern != "%Y%m%d" else text[:8], pattern
                )
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    raise ElectionFormatError("schema_drift", f"date {value!r} is not recognised")


def _csv(
    raw: bytes, *, delimiter: str, required: Sequence[str]
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    comments, body = [], []
    for line in text.splitlines():
        if line.startswith("#"):
            comments.append(line.lstrip("#").strip())
        elif line.strip():
            body.append(line)
    if len(body) > MAX_ROWS:
        raise ElectionFormatError(
            "input_limit", "file has more rows than the parser accepts"
        )
    reader = csv.DictReader(io.StringIO("\n".join(body)), delimiter=delimiter)
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [c for c in required if c not in header]
    if missing:
        raise ElectionFormatError("schema_drift", f"missing columns {missing}")
    rows = [
        {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        for row in reader
    ]
    return header, rows, comments


def _vintage_from_text(texts: Sequence[str]) -> str | None:
    found = set()
    for text in texts:
        folded = str(text or "").casefold()
        if "vorläufig" in folded or "vorlaeufig" in folded:
            found.add("preliminary")
        if "endgültig" in folded or "endgueltig" in folded:
            found.add("certified")
    if len(found) > 1:
        raise ElectionFormatError(
            "schema_drift", "file states both a preliminary and a final result"
        )
    return next(iter(found), None)


def _stand(comments: Sequence[str]) -> tuple[str | None, str | None]:
    for line in comments:
        match = re.search(
            r"Stand:?\s*(\d{2}\.\d{2}\.\d{4})(?:[ ,]+(\d{2}:\d{2}))?", line
        )
        if match:
            day = _day(match.group(1))
            return day, f"{day}T{match.group(2)}:00" if match.group(2) else None
    return None, None


def _entry(kind: str, name: Any, **fields: Any) -> dict[str, Any]:
    label = _clean(name)
    if not label:
        raise ElectionFormatError("schema_drift", "result entry has no name")
    key = f"{kind}:{slug(label)}"
    return {"key": key, "kind": kind, "name": label, **fields}


def _contest(
    election_id: str,
    unit: Mapping[str, Any],
    ballot: str,
    totals: Mapping[str, Any],
    entries: list[dict[str, Any]],
    remarks: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "contest_key": f"{election_id}|{unit['scheme']}:{unit['native_id']}|{ballot}",
        "election_id": election_id,
        "unit": dict(unit),
        "ballot": ballot,
        "figures": {
            "totals": dict(totals),
            "entries": sorted(entries, key=lambda e: (e["key"], e.get("mode") or "")),
            "remarks": sorted({r for r in remarks if r}),
        },
    }


# ----------------------------------------------------------------------------------------- Bundeswahlleiterin

_KERG_REQUIRED = (
    "Wahlart",
    "Wahltag",
    "Gebietsart",
    "Gebietsnummer",
    "Gebietsname",
    "UegGebietsart",
    "UegGebietsnummer",
    "Gruppenart",
    "Gruppenname",
    "Stimme",
    "Anzahl",
)
_KERG_UNITS = {"bund": "de-bund", "land": "de-land", "wahlkreis": "de-bt-wahlkreis"}
_KERG_SYSTEM = {
    "wahlberechtigte": "electorate",
    "wählende": "voters",
    "waehlende": "voters",
    "wähler": "voters",
    "ungültige": "invalid",
    "ungueltige": "invalid",
    "gültige": "valid",
    "gueltige": "valid",
}
_BALLOTS = {"1": "first-vote", "2": "second-vote"}


def parse_kerg(raw: bytes) -> dict[str, Any]:
    _, rows, comments = _csv(raw, delimiter=";", required=_KERG_REQUIRED)
    if not rows:
        raise ElectionFormatError("schema_drift", "result file has no rows")
    kinds = {r["Wahlart"] for r in rows}
    days = {r["Wahltag"] for r in rows}
    if len(kinds) != 1 or len(days) != 1:
        raise ElectionFormatError(
            "schema_drift", "a result file covers exactly one election"
        )
    election_date = _day(days.pop())
    wahlart = kinds.pop()
    election_id = f"de-{wahlart.casefold()}:{election_date}"
    published_on, published_at = _stand(comments)
    units: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        scheme = _KERG_UNITS.get(row["Gebietsart"].casefold())
        if scheme is None:
            raise ElectionFormatError(
                "schema_drift", f"unknown area kind {row['Gebietsart']!r}"
            )
        native = normalize_unit_id(scheme, row["Gebietsnummer"])
        if native is None:
            raise ElectionFormatError("schema_drift", "area has no number")
        parent_scheme = _KERG_UNITS.get(row["UegGebietsart"].casefold())
        unit = units.setdefault(
            (scheme, native),
            {
                "unit": {
                    "scheme": scheme,
                    "native_id": native,
                    "name": _clean(row["Gebietsname"]),
                    "kind": row["Gebietsart"].casefold(),
                    "parent": None
                    if parent_scheme is None
                    else {
                        "scheme": parent_scheme,
                        "native_id": normalize_unit_id(
                            parent_scheme, row["UegGebietsnummer"]
                        ),
                    },
                },
                "shared": {},
                "ballots": {},
            },
        )
        group, name, vote = (
            row["Gruppenart"].casefold(),
            row["Gruppenname"],
            row["Stimme"].strip(),
        )
        count = _count(row["Anzahl"])
        share = _share(row.get("Prozent"))
        if group == "system-gruppe":
            field = _KERG_SYSTEM.get(name.strip().casefold().split(" ")[0])
            if field is None:
                continue  # other published system rows are not figures this record carries
            if vote in _BALLOTS:
                ballot = unit["ballots"].setdefault(
                    _BALLOTS[vote], {"totals": {}, "entries": [], "remarks": []}
                )
                ballot["totals"][field] = count
            else:
                unit["shared"][field] = count
            continue
        if vote not in _BALLOTS:
            raise ElectionFormatError(
                "schema_drift", "a party or candidate row names its vote (1 or 2)"
            )
        kind = {"partei": "party", "einzelbewerber": "candidate"}.get(group)
        if kind is None:
            raise ElectionFormatError(
                "schema_drift", f"unknown group kind {row['Gruppenart']!r}"
            )
        ballot = unit["ballots"].setdefault(
            _BALLOTS[vote], {"totals": {}, "entries": [], "remarks": []}
        )
        entry = _entry(
            kind,
            name,
            party=_clean(name) if kind == "party" else None,
            votes=count,
            share_published=share,
            mode=None,
        )
        if row.get("Mandate", "").strip():
            entry["mandates_published"] = _count(row["Mandate"])
        ballot["entries"].append(entry)
        if row.get("Bemerkung"):
            ballot["remarks"].append(row["Bemerkung"])
    contests = []
    for item in units.values():
        for ballot_name, ballot in sorted(item["ballots"].items()):
            totals = {
                "electorate": None,
                "voters": None,
                "invalid": None,
                "valid": None,
            }
            totals.update(item["shared"])
            totals.update(ballot["totals"])
            contests.append(
                _contest(
                    election_id,
                    item["unit"],
                    ballot_name,
                    totals,
                    ballot["entries"],
                    ballot["remarks"],
                )
            )
    return {
        "format": "de-btw-kerg-csv",
        "stated_vintage": _vintage_from_text(comments),
        "published_on": published_on,
        "published_at": published_at,
        "elections": [
            {
                "election_id": election_id,
                "native_id": f"{wahlart}:{election_date}",
                "kind": {"BT": "bundestag"}.get(wahlart, wahlart.casefold()),
                "name": next(
                    (c.split(" - ")[0] for c in comments if "wahl" in c.casefold()),
                    None,
                ),
                "election_date": election_date,
                "jurisdiction": "DE",
            }
        ],
        "contests": contests,
    }


# ----------------------------------------------------------------------------------------- Berlin

_BERLIN_REQUIRED = (
    "Ergebnisstand",
    "Wahl",
    "Wahltag",
    "Stand",
    "Stimmart",
    "Gebietsart",
    "Gebietsnummer",
    "Gebietsname",
    "Wahlberechtigte",
    "Wählende",
    "Ungültige Stimmen",
    "Gültige Stimmen",
)
_BERLIN_UNITS = {
    "land": "de-be-land",
    "bezirk": "de-be-bezirk",
    "wahlkreis": "de-be-wahlkreis",
}
_BERLIN_BALLOTS = {"erststimme": "first-vote", "zweitstimme": "second-vote"}


def parse_berlin(raw: bytes) -> dict[str, Any]:
    header, rows, comments = _csv(raw, delimiter=";", required=_BERLIN_REQUIRED)
    if not rows:
        raise ElectionFormatError("schema_drift", "result file has no rows")
    parties = header[header.index("Gültige Stimmen") + 1 :]
    if not parties:
        raise ElectionFormatError("schema_drift", "result file has no party columns")
    states = {r["Ergebnisstand"] for r in rows}
    vintage = _vintage_from_text(states)
    if len(states) != 1 or vintage is None:
        raise ElectionFormatError(
            "schema_drift", "one file states one Ergebnisstand (vorläufig or endgültig)"
        )
    elections = {(r["Wahl"], r["Wahltag"]) for r in rows}
    if len(elections) != 1:
        raise ElectionFormatError(
            "schema_drift", "a result file covers exactly one election"
        )
    wahl, day = elections.pop()
    election_date = _day(day)
    election_id = f"de-be-{wahl.casefold()}:{election_date}"
    stands = sorted({_day(r["Stand"]) for r in rows if r["Stand"]})
    contests, seen = [], set()
    for row in rows:
        scheme = _BERLIN_UNITS.get(row["Gebietsart"].casefold())
        ballot = _BERLIN_BALLOTS.get(row["Stimmart"].casefold())
        if scheme is None or ballot is None:
            raise ElectionFormatError("schema_drift", "unknown area kind or vote type")
        native = normalize_unit_id(scheme, row["Gebietsnummer"])
        if native is None or (scheme, native, ballot) in seen:
            raise ElectionFormatError("schema_drift", "area number missing or repeated")
        seen.add((scheme, native, ballot))
        unit = {
            "scheme": scheme,
            "native_id": native,
            "name": _clean(row["Gebietsname"]),
            "kind": row["Gebietsart"].casefold(),
            "parent": None
            if not row.get("Bezirksnummer") or scheme != "de-be-wahlkreis"
            else {
                "scheme": "de-be-bezirk",
                "native_id": normalize_unit_id("de-be-bezirk", row["Bezirksnummer"]),
            },
        }
        totals = {
            "electorate": _count(row["Wahlberechtigte"]),
            "voters": _count(row["Wählende"]),
            "invalid": _count(row["Ungültige Stimmen"]),
            "valid": _count(row["Gültige Stimmen"]),
        }
        entries = [
            _entry(
                "party",
                party,
                party=_clean(party),
                votes=_count(row[party]),
                share_published=None,
                mode=None,
            )
            for party in parties
            if row.get(party, "") != ""
        ]
        contests.append(_contest(election_id, unit, ballot, totals, entries))
    return {
        "format": "de-be-wahlkreis-csv",
        "stated_vintage": vintage,
        "published_on": stands[-1] if stands else None,
        "published_at": None,
        "elections": [
            {
                "election_id": election_id,
                "native_id": f"{wahl}:{election_date}",
                "kind": {
                    "AGH": "abgeordnetenhaus",
                    "BVV": "bezirksverordnetenversammlung",
                }.get(wahl, wahl.casefold()),
                "name": next((c for c in comments if "wahl" in c.casefold()), None),
                "election_date": election_date,
                "jurisdiction": "DE-BE",
            }
        ],
        "contests": contests,
    }


# ----------------------------------------------------------------------------------------- UK Electoral Commission

_UK_REQUIRED = (
    "ONS ID",
    "Constituency name",
    "Country name",
    "Candidate first name",
    "Candidate surname",
    "Party name",
    "Votes",
)


def parse_uk_ec(raw: bytes, *, election: Mapping[str, Any]) -> dict[str, Any]:
    _, rows, _ = _csv(raw, delimiter=",", required=_UK_REQUIRED)
    if not rows:
        raise ElectionFormatError("schema_drift", "result file has no rows")
    election_id = f"gb-{slug(election['kind'])}:{election['date']}"
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        native = normalize_unit_id("gb-ons-pcon", row["ONS ID"])
        if native is None:
            raise ElectionFormatError(
                "schema_drift", f"ONS code {row['ONS ID']!r} is malformed"
            )
        item = grouped.setdefault(
            native,
            {
                "unit": {
                    "scheme": "gb-ons-pcon",
                    "native_id": native,
                    "name": _clean(row["Constituency name"]),
                    "kind": "constituency",
                    "parent": {
                        "scheme": "gb-country",
                        "native_id": _clean(row["Country name"]),
                    },
                },
                "totals": {},
                "entries": [],
            },
        )
        for column, field in (
            ("Electorate", "electorate"),
            ("Valid votes", "valid"),
            ("Invalid votes", "invalid"),
        ):
            if row.get(column, "") != "":
                value = _count(row[column])
                if item["totals"].get(field, value) != value:
                    raise ElectionFormatError(
                        "schema_drift", f"rows of one constituency disagree on {column}"
                    )
                item["totals"][field] = value
        name = " ".join(
            p for p in (row["Candidate first name"], row["Candidate surname"]) if p
        )
        elected = str(row.get("Elected", "")).strip().casefold()
        item["entries"].append(
            _entry(
                "candidate",
                name,
                party=_clean(row["Party name"]),
                party_abbreviation=_clean(row.get("Party abbreviation")),
                votes=_count(row["Votes"]),
                share_published=_share(row.get("Share")),
                elected_published=None
                if not elected
                else elected in {"yes", "true", "1", "y"},
                mode=None,
            )
        )
    contests = []
    for item in grouped.values():
        totals = {
            "electorate": None,
            "voters": None,
            "invalid": None,
            "valid": None,
            **item["totals"],
        }
        contests.append(
            _contest(election_id, item["unit"], "single", totals, item["entries"])
        )
    return {
        "format": "gb-ec-candidates-csv",
        "stated_vintage": None,
        "published_on": None,
        "published_at": None,
        "elections": [
            {
                "election_id": election_id,
                "native_id": str(election.get("native_id") or election_id),
                "kind": str(election["kind"]),
                "name": election.get("name"),
                "election_date": _day(election["date"]),
                "jurisdiction": "GB",
            }
        ],
        "contests": contests,
    }


# ----------------------------------------------------------------------------------------- MIT Election Lab

_MEDSL_REQUIRED = (
    "year",
    "state_po",
    "county_name",
    "county_fips",
    "office",
    "candidate",
    "party",
    "candidatevotes",
    "totalvotes",
    "version",
    "mode",
)


def parse_medsl(
    raw: bytes,
    *,
    selection: Mapping[str, Any] | None = None,
    election_dates: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _, rows, _ = _csv(raw, delimiter=",", required=_MEDSL_REQUIRED)
    selection = dict(selection or {})
    years = {str(y) for y in selection.get("years") or []}
    states = {str(s).upper() for s in selection.get("states") or []}
    rows = [
        r
        for r in rows
        if (not years or r["year"] in years)
        and (not states or r["state_po"].upper() in states)
    ]
    if not rows:
        raise ElectionFormatError("schema_drift", "no rows in the selection")
    versions = sorted({r["version"] for r in rows})
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    elections = {}
    for row in rows:
        year, office = row["year"], row["office"]
        election_id = f"us-{slug(office)}:{year}"
        elections[election_id] = {
            "election_id": election_id,
            "native_id": f"{office}:{year}",
            "kind": slug(office),
            "name": f"{office.title()} {year}",
            "election_date": _day((election_dates or {}).get(year))
            if (election_dates or {}).get(year)
            else None,
            "jurisdiction": "US",
        }
        fips = normalize_unit_id("us-fips-county", row["county_fips"])
        scheme = "us-fips-county" if fips else "us-medsl-county-name"
        native = fips or normalize_unit_id(
            scheme, f"{row['state_po']}:{row['county_name']}"
        )
        item = grouped.setdefault(
            (election_id, scheme, native),
            {
                "unit": {
                    "scheme": scheme,
                    "native_id": native,
                    "name": _clean(row["county_name"]),
                    "kind": "county",
                    "parent": {
                        "scheme": "us-state-po",
                        "native_id": row["state_po"].upper(),
                    },
                },
                "totals": {},
                "entries": [],
                "election_id": election_id,
            },
        )
        total = _count(row["totalvotes"])
        mode = _clean(row["mode"]) or "TOTAL"
        # totalvotes is the county's total for the office as MEDSL publishes it per row; kept per mode, never summed.
        known = item["totals"].setdefault("total_votes_by_mode", {})
        if known.get(mode, total) != total:
            raise ElectionFormatError(
                "schema_drift", "rows of one county and mode disagree on totalvotes"
            )
        known[mode] = total
        item["entries"].append(
            _entry(
                "candidate",
                row["candidate"],
                party=_clean(row["party"]),
                votes=_count(row["candidatevotes"]),
                share_published=None,
                mode=mode,
            )
        )
    contests = []
    for item in grouped.values():
        pairs = [(e["key"], e["mode"]) for e in item["entries"]]
        if len(pairs) != len(set(pairs)):
            raise ElectionFormatError(
                "schema_drift", "a county repeats a candidate and mode"
            )
        contests.append(
            _contest(
                item["election_id"],
                item["unit"],
                "office",
                item["totals"],
                item["entries"],
            )
        )
    return {
        "format": "us-medsl-county-csv",
        "stated_vintage": None,
        "published_on": _day(versions[-1]),
        "published_at": None,
        "dataset_versions": versions,
        "elections": sorted(elections.values(), key=lambda e: e["election_id"]),
        "contests": contests,
    }


def _last_modified(headers: Mapping[str, Any]) -> tuple[str | None, str | None]:
    value = headers.get("last-modified")
    if not value:
        return None, None
    try:
        stamp = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None, None
    return stamp.date().isoformat(), stamp.replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def parse_release(
    format_id: str,
    raw: bytes,
    *,
    declared: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse one result file; the vintage kind comes from the file when it states one, else the manifest."""
    if format_id not in FORMATS:
        raise ElectionFormatError(
            "schema_drift", f"unknown result format {format_id!r}"
        )
    if format_id == "de-btw-kerg-csv":
        release = parse_kerg(raw)
    elif format_id == "de-be-wahlkreis-csv":
        release = parse_berlin(raw)
    elif format_id == "gb-ec-candidates-csv":
        release = parse_uk_ec(raw, election=dict(declared.get("election") or {}))
    else:
        release = parse_medsl(
            raw,
            selection=declared.get("selection"),
            election_dates=declared.get("election_dates"),
        )
    stated, wanted = release.pop("stated_vintage"), declared.get("vintage")
    if stated and wanted and stated != wanted:
        raise ElectionFormatError(
            "schema_drift",
            f"file states a {stated} result but the source declares {wanted}",
        )
    kind = stated or wanted
    if kind not in VINTAGE_KINDS:
        raise ElectionFormatError(
            "schema_drift", "release states no vintage kind (preliminary or certified)"
        )
    if release["published_on"] is None:
        release["published_on"], release["published_at"] = _last_modified(
            {str(k).casefold(): v for k, v in dict(headers or {}).items()}
        )
    if release["published_on"] is None:
        raise ElectionFormatError("schema_drift", "release states no publication date")
    if len(release["contests"]) > MAX_CONTESTS:
        raise ElectionFormatError(
            "input_limit", "release has more contests than the parser accepts"
        )
    keys = [c["contest_key"] for c in release["contests"]]
    if len(set(keys)) != len(keys):
        raise ElectionFormatError("schema_drift", "release repeats a contest")
    release.update(
        {
            "contract": RELEASE_CONTRACT,
            "provider": FORMATS[format_id]["provider"],
            "jurisdiction": FORMATS[format_id]["jurisdiction"],
            "vintage_kind": kind,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "contests_sha256": _digest(release["contests"]),
            "contest_count": len(release["contests"]),
        }
    )
    return release


def elections_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("elections") or {})
    format_id = declared.get("format")
    if format_id not in FORMATS or FORMATS[format_id]["provider"] != declared.get(
        "provider"
    ):
        raise SourcePackError(
            "invalid_manifest",
            "election sources declare a matching provider and format",
        )
    if declared.get("vintage") is not None and declared["vintage"] not in VINTAGE_KINDS:
        raise SourcePackError("invalid_manifest", "unknown vintage kind")
    if not FORMATS[format_id]["states_vintage"] and declared.get("vintage") is None:
        raise SourcePackError(
            "invalid_manifest",
            "a source whose file states no vintage kind declares one",
        )
    if format_id == "gb-ec-candidates-csv":
        election = dict(declared.get("election") or {})
        if not election.get("kind") or not election.get("date"):
            raise SourcePackError(
                "invalid_manifest",
                "a UK result source names its election kind and date",
            )
    if not str(dict(declared.get("rules") or {}).get("source_url") or "").startswith(
        "https://"
    ):
        raise SourcePackError(
            "invalid_manifest", "jurisdiction rules cite their source (https)"
        )
    for scheme, ref in dict(declared.get("geometry") or {}).items():
        if scheme not in UNIT_SCHEMES or not {
            "provider",
            "layer",
            "vintage",
            "crs",
        } <= set(dict(ref)):
            raise SourcePackError(
                "invalid_manifest",
                "a geometry reference names provider, layer, vintage and CRS",
            )
    return declared


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


class ElectionResultsAdapter:
    """Fetch one declared result file on the runtime's default transport and emit one record per contest."""

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
        self.declared = elections_declaration(self.source)
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling and the timeout.
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
            "elections": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
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
                "election runs fetch the declared file, not ad-hoc queries",
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        if cursor is not None:
            raise SourcePackError(
                "cursor_drift", "a result release is one file; there is no next page"
            )
        url = self.source["endpoint"]
        host = (urlsplit(url).hostname or "").casefold()
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": "text/csv, text/plain, application/octet-stream"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "result file was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "result file exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"result download refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"result provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError(
                "schema_drift", f"result download returned HTTP {status}"
            )
        try:
            release = parse_release(
                self.declared["format"], raw, declared=self.declared, headers=headers
            )
        except ElectionFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(release["contests"]) > limit:
            # Never a truncated release: a missing contest would read as a contest without results.
            raise SourcePackError(
                "budget_exhausted",
                "release has more contests than the run's result budget",
            )
        rules = dict(self.declared.get("rules") or {})
        geometry = {
            k: dict(v) for k, v in dict(self.declared.get("geometry") or {}).items()
        }
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "jurisdiction": release["jurisdiction"],
            "vintage_kind": release["vintage_kind"],
            "authority": self.declared.get("authority") or self.source.get("publisher"),
            "published_on": release["published_on"],
            "published_at": release["published_at"],
            "file_sha256": release["file_sha256"],
            "contests_sha256": release["contests_sha256"],
            "contest_count": release["contest_count"],
            "dataset_versions": release.get("dataset_versions"),
            "selection": self.declared.get("selection"),
            "elections": release["elections"],
            "rules": rules,
            "geometry": geometry,
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": "fixture"
            if response.get("origin") == "fixture"
            else "live",
            "url": url,
        }
        records = []
        for contest in release["contests"]:
            unit = contest["unit"]
            records.append(
                {
                    "id": contest["contest_key"],
                    "title": f"{contest['election_id']} {unit.get('name') or unit['native_id']} ({contest['ballot']}, "
                    f"{release['vintage_kind']})",
                    "url": url,
                    "language": "en",
                    "published_at": release["published_on"],
                    "content": json.dumps(contest, sort_keys=True, ensure_ascii=False),
                    "election_release": header,
                    "election_contest": contest,
                }
            )
        receipt = {
            "status": status,
            "provider": release["provider"],
            "vintage_kind": release["vintage_kind"],
            "published_on": release["published_on"],
            "file_sha256": release["file_sha256"],
            "contests": len(records),
            "evidence_origin": header["evidence_origin"],
            "final_page": True,
        }
        return RuntimePage(tuple(records), None, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: ElectionResultsAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored result files keyed by URL path (+ query); responses are marked as fixture evidence."""
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
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = ElectionResultsAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    page = adapter.fetch_page(
        {
            "operation": min(source["operations"]),
            "parameters": {},
            "limit": int(source["budgets"]["max_results"]),
        },
        cursor=None,
    )
    return [dict(item) for item in page.records]


def day_ms(day: str, at: str | None = None) -> int:
    """Epoch milliseconds (UTC) of a publication date or date-time."""
    stamp = (
        datetime.fromisoformat(at)
        if at
        else datetime.combine(date.fromisoformat(day), datetime.min.time())
    )
    from datetime import timezone

    return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)

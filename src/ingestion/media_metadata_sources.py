"""Bounded book, music and authority metadata acquisition for Cultural Collections (#2225, MM01 and MM03-MM06).

Companion of :mod:`src.ingestion.cultural_sources`. Five providers run as
``media-metadata`` connector sources of the scientific source pack
(``config/source_packs/scientific.json``, operation ``media``) through
:mod:`src.ingestion.source_pack_runtime`. Licence acceptance, budgets,
receipts, checkpoints and quarantine apply unchanged. Every source pins an
explicit selection of 1-50 identifiers, and each selector is one provider
request. There is no free-text search against a provider.

* **Open Library** (``open-library``, MM03): works, editions and authors by
  OLID, and editions by ISBN. The Open Library ``revision`` is the revision
  marker. ``/type/redirect`` and ``/type/delete`` records are history.
  Covers, scans, excerpts and descriptions are dropped at acquisition.
  Operator-cut dump slices are imported through
  :meth:`src.kb.media_metadata.MediaMetadataStore.import_dump_slice`.
* **MusicBrainz** (``musicbrainz``, MM04): recordings, releases, works and
  artists by MBID from web service 2. Only CC0 core data is kept.
  Annotations, tags, genres, ratings and user data are dropped and reported
  in the receipt. A merged MBID answers with the surviving entity, and that
  answer is recorded as a redirect revision of the old MBID.
* **Wikidata** (``wikidata``, MM05): items by QID through ``wbgetentities``.
  The revision ID is the marker. External-identifier statements keep their
  rank and references, deprecated statements are kept, and redirects and
  missing items are history.
* **Deutsche Nationalbibliothek** (``dnb``, MM06): GND authorities (SRU
  ``authorities``) and catalogue records (SRU ``dnb``) in MARC21-xml.
* **Library of Congress** (``loc``, MM06): LCNAF names (id.loc.gov) and
  catalogue records (lccn.loc.gov) in MARCXML. For both MARC providers,
  MARC 005 is the revision marker, and deleted or replaced headings are
  history.

The access decisions are recorded in :data:`PROVIDER_CONTRACTS` and in
``docs/development/cultural-evidence/media-metadata-source-audit.md``. Every
provider is ``unverified-live`` until a dated live run (#2513). The fixtures
are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.media_metadata import (
    CONTRACT,
    MediaMetadataError,
    normalize_identifier,
    validate_statement,
)

CONNECTOR = "media-metadata"
OPERATION = "media"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("open-library", "musicbrainz", "wikidata", "dnb", "loc")
MAX_SELECTION = 50
USER_AGENT = "Noesis-media-metadata/1.0 (+https://github.com/Ikey168/Noesis)"
PROVIDER_HOSTS = {
    "open-library": ("openlibrary.org",),
    "musicbrainz": ("musicbrainz.org",),
    "wikidata": ("www.wikidata.org",),
    "dnb": ("services.dnb.de",),
    "loc": ("id.loc.gov", "lccn.loc.gov"),
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "open-library": {
        "publisher": "Open Library (Internet Archive)",
        "documentation": "https://openlibrary.org/developers/api",
        "access": "GET /works/{OLID}.json, /books/{OLID}.json, /authors/{OLID}.json and /isbn/{ISBN}.json; monthly "
        "dumps on archive.org only as operator-cut named slices",
        "authentication": "none; an identifying User-Agent (app name, contact) is required; the contact comes from "
        "the optional NOESIS_MEDIA_METADATA_CONTACT secret",
        "rate_limits": "at most 1 request per second from this runtime (identified clients)",
        "licence": "catalogue data published for reuse (openlibrary.org/developers/licensing; operator confirms "
        "before redistribution); covers and scans are governed separately and excluded",
        "revision_semantics": "revision (integer per edit) and last_modified; /type/redirect with location and "
        "/type/delete are history revisions",
        "min_interval_ms": 1000,
        "record_classes": ["work", "edition", "creator"],
        "status": "unverified-live",
    },
    "musicbrainz": {
        "publisher": "MetaBrainz Foundation (MusicBrainz)",
        "documentation": "https://musicbrainz.org/doc/MusicBrainz_API",
        "access": "GET /ws/2/{recording,release,work,artist}/{MBID}?fmt=json&inc=... (web service 2)",
        "authentication": "none; a meaningful User-Agent (app/version (contact)) is required",
        "rate_limits": "1 request per second on average per IP; HTTP 503 means throttled",
        "licence": "core data CC0 1.0; supplementary data (annotations, tags, genres, ratings, edit history, user "
        "data) CC BY-NC-SA 3.0 and excluded; Cover Art Archive excluded",
        "revision_semantics": "no per-entity edit marker in WS/2: the revision marker is the digest of the stored "
        "CC0 core payload, ordered by acquisition; a merged MBID answers with the surviving entity (redirect)",
        "min_interval_ms": 1100,
        "record_classes": ["recording", "release", "work", "creator"],
        "status": "unverified-live",
    },
    "wikidata": {
        "publisher": "Wikimedia Foundation (Wikidata)",
        "documentation": "https://www.wikidata.org/w/api.php?action=help&modules=wbgetentities",
        "access": "GET /w/api.php?action=wbgetentities&ids={QID}&props=info|labels|claims&format=json; no JSON dump "
        "processing and no unbounded SPARQL",
        "authentication": "none; Wikimedia User-Agent policy and maxlag honoured",
        "rate_limits": "serial requests; HTTP 429 or maxlag errors stop the run",
        "licence": "CC0 1.0 (structured data)",
        "revision_semantics": "lastrevid (revision ID) and modified; redirects and missing items are history; "
        "deprecated statements keep their rank",
        "min_interval_ms": 500,
        "record_classes": ["authority-link", "creator"],
        "status": "unverified-live",
    },
    "dnb": {
        "publisher": "Deutsche Nationalbibliothek",
        "documentation": "https://www.dnb.de/EN/Professionell/Metadatendienste/metadatendienste_node.html",
        "access": "SRU 1.1 https://services.dnb.de/sru/authorities (GND, query nid=) and /sru/dnb (catalogue, "
        "query idn= or num=), recordSchema=MARC21-xml, at most 5 records per request",
        "authentication": "none for SRU (verify: an access token was required before 2023)",
        "rate_limits": "no published quota; serial requests with an identifying User-Agent",
        "licence": "CC0 1.0 for DNB metadata; digitised tables of contents, covers and full texts excluded",
        "revision_semantics": "MARC 005; leader/05 d is deleted; 682 with a (DE-588) $0 is a redirect "
        "(Umlenkung, verify)",
        "min_interval_ms": 500,
        "record_classes": ["creator", "work", "edition", "authority-link"],
        "status": "unverified-live",
    },
    "loc": {
        "publisher": "Library of Congress",
        "documentation": "https://id.loc.gov/techcenter/",
        "access": "GET https://id.loc.gov/authorities/names/{LCCN}.marcxml.xml (LCNAF) and "
        "https://lccn.loc.gov/{LCCN}/marcxml (catalogue)",
        "authentication": "none; an identifying User-Agent",
        "rate_limits": "at most 1 request per second from this runtime",
        "licence": "no known copyright restrictions on Library of Congress metadata (US government work; verify for "
        "contributed records)",
        "revision_semantics": "MARC 005; leader/05 d or s is deleted or replaced; 682 and 010 $z name the "
        "replacing heading",
        "min_interval_ms": 1000,
        "record_classes": ["creator", "work", "edition", "authority-link"],
        "status": "unverified-live",
    },
}
LICENCES = {
    "open-library": {"id": "open-library-data", "scope": "Open Library catalogue data (no covers or scans)",
                     "attribution": "Open Library (openlibrary.org)"},
    "musicbrainz": {"id": "cc0-1.0", "scope": "MusicBrainz core data only", "attribution": "MusicBrainz"},
    "wikidata": {"id": "cc0-1.0", "scope": "Wikidata structured data", "attribution": "Wikidata"},
    "dnb": {"id": "cc0-1.0", "scope": "Deutsche Nationalbibliothek metadata", "attribution": "Deutsche Nationalbibliothek"},
    "loc": {"id": "loc-no-known-restrictions", "scope": "Library of Congress metadata",
            "attribution": "Library of Congress"},
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "intended": "live-verified",
               "note": "fixture-verified parser; no dated live run from this runtime (#2513)"}
    for provider in PROVIDERS
}
EXCLUDED_RECORD_CLASSES = [
    "cover images (Open Library covers, Cover Art Archive), audio and video",
    "full text, scans, excerpts and descriptions (ocaid, excerpts, description, first_sentence)",
    "MusicBrainz supplementary data: annotations, tags, genres, ratings, user tags and ratings, edit history",
    "popularity data (ratings, reading-log counts, listen counts)",
    "catalogue rights notes as rights determinations (MARC 506/540)",
    "identifier schemes outside the audited list",
]
EXCLUDED_SOURCES = {
    "wikidata-dumps-and-sparql": "no bulk processing; lookups go by named QID",
    "worldcat-viaf-api": "WorldCat is licensed; VIAF IDs are kept only as published identifier strings",
    "dnb-bulk-and-culturegraph": "bulk mirrors; SRU per identifier covers the bounded scope",
    "loc-z3950-classification-web": "superseded Z39.50/legacy SRU; Classification Web is a subscription product",
    "discogs": "non-core data under a mixed licence; outside the selected providers",
}
BOUNDED_COVERAGE = {
    "selectors": {"open-library": ["olid", "isbn"], "musicbrainz": ["entity + mbid"],
                  "wikidata": ["qid (optional revision)"], "dnb": ["gnd", "idn", "isbn"],
                  "loc": ["lcnaf (id.loc.gov)", "lccn (lccn.loc.gov)"]},
    "selection_size": f"1-{MAX_SELECTION} selectors per source; one request per selector",
    "dump_slices": "Open Library dumps only as operator-cut slices of one pinned dump, at most 500 named keys",
    "fixtures": ["a book work with two editions and its author across Open Library, Wikidata, DNB and LoC",
                 "a recording with an ISRC, its work, release and artist across MusicBrainz and Wikidata",
                 "a merged MBID, a deprecated GND record, a conflicting ISBN and an unknown identifier"],
}
# Wikidata external-identifier properties kept as sourced assertions (MM05).
WIKIDATA_PROPERTIES = {
    "P212": "isbn13", "P957": "isbn10", "P1243": "isrc", "P1827": "iswc", "P434": "mbid", "P435": "mbid",
    "P436": "mbid", "P4404": "mbid", "P5813": "mbid", "P227": "gnd", "P244": "lcnaf", "P1144": "lccn",
    "P648": "olid", "P214": "viaf", "P213": "isni", "P243": "oclc",
}
WIKIDATA_CREATORS = {"P50": "author", "P175": "performer", "P86": "composer"}
WIKIDATA_DESCRIBES = {
    "Q5": "creator", "Q215380": "creator", "Q2088357": "creator", "Q7725634": "work", "Q47461344": "work",
    "Q571": "work", "Q105543609": "work", "Q2188189": "work", "Q7366": "work", "Q3331189": "edition",
    "Q482994": "release", "Q134556": "release", "Q273057": "release",
}
_MB_EXCLUDED = ("annotation", "rating", "tags", "genres", "user-tags", "user-genres", "user-rating", "aliases-user",
                "cover-art-archive")
_OL_EXCLUDED = ("covers", "cover_edition", "ocaid", "ia_box_id", "ia_loaded_id", "excerpts", "description",
                "first_sentence", "links", "notes", "table_of_contents", "ratings", "source_records")
_OL_IDENTIFIERS = {"wikidata": "wikidata", "lccn": "lccn", "viaf": "viaf", "isni": "isni", "oclc": "oclc"}
_MB_URL_AUTHORITIES = (
    (re.compile(r"wikidata\.org/wiki/(Q[1-9]\d*)"), "wikidata"),
    (re.compile(r"d-nb\.info/gnd/([0-9X\-]+)"), "gnd"),
    (re.compile(r"id\.loc\.gov/authorities/names/([a-z]{1,3}\d+)"), "lcnaf"),
    (re.compile(r"viaf\.org/viaf/(\d+)"), "viaf"),
    (re.compile(r"openlibrary\.org/(?:works|books|authors)/(OL\d+[AWM])"), "olid"),
)
MARC = "{http://www.loc.gov/MARC21/slim}"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    text = str(value).strip()
    return text or None


def _statement(source: str, record_type: str, native_id: str, *, revision: Mapping[str, Any], url: str | None,
               status: str = "active", redirect_to: str | None = None, titles=(), names=(), identifiers=(),
               relations=(), creators=(), dates=(), attributes: Mapping[str, Any] | None = None,
               describes: str | None = None, excluded: Sequence[str] = ()) -> dict[str, Any]:
    value = {
        "contract": CONTRACT, "record_type": record_type, "source": source, "native_id": native_id, "url": url,
        "revision": dict(revision), "status": status, "redirect_to": redirect_to, "titles": list(titles),
        "names": list(names), "identifiers": list(identifiers), "relations": list(relations),
        "creators": list(creators), "dates": list(dates), "licence": dict(LICENCES[source]),
        "attributes": dict(attributes or {}), "excluded_fields": sorted(set(excluded)),
    }
    if describes:
        value["describes"] = describes
    try:
        return validate_statement(value)
    except MediaMetadataError as exc:
        raise SourcePackError("schema_drift", exc.message) from exc


def _ident(scheme: str, value: Any, role: str = "asserted", **extra: Any) -> dict[str, Any] | None:
    text = _text(value)
    if text is None:
        return None
    item = {"scheme": scheme, "value": text, "role": role}
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def _dedupe(items: Sequence[Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    seen, result = set(), []
    for item in items:
        if item is None:
            continue
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(dict(item))
    return result


# ------------------------------------------------------------------ Open Library (MM03)

_OL_KIND = {"/type/work": "work", "/type/edition": "edition", "/type/author": "creator"}
_OL_PREFIX = {"works": "work", "books": "edition", "authors": "creator"}


def parse_open_library(body: Mapping[str, Any], url: str) -> tuple[list[dict[str, Any]], list[str]]:
    """One Open Library JSON record -> statements (a redirect or delete becomes a history revision)."""
    if not isinstance(body, Mapping) or not _text(body.get("key")):
        raise SourcePackError("schema_drift", "Open Library record lacks its key")
    key = str(body["key"])
    parts = key.strip("/").split("/")
    if len(parts) != 2 or parts[0] not in _OL_PREFIX:
        raise SourcePackError("schema_drift", f"unexpected Open Library key {key}")
    olid = parts[1]
    kind_key = dict(body.get("type") or {}).get("key")
    record_type = _OL_KIND.get(kind_key) or _OL_PREFIX[parts[0]]
    revision_no = body.get("revision")
    if not str(revision_no or "").isdigit():
        raise SourcePackError("schema_drift", "Open Library record lacks its revision")
    modified = _text(dict(body.get("last_modified") or {}).get("value"))
    revision = {"marker": str(revision_no), "basis": "ol-revision", "order": f"{int(revision_no):010d}",
                "date": modified[:10] if modified else None, "declared": modified}
    excluded = [k for k in body if k in _OL_EXCLUDED]
    self_id = _ident("olid", olid, "self")
    if kind_key in {"/type/redirect", "/type/delete"}:
        location = _text(body.get("location"))
        target = location.strip("/").split("/")[-1] if location else None
        status = "redirected" if kind_key == "/type/redirect" and target else "deleted"
        return [_statement("open-library", record_type, olid, revision=revision, url=url, status=status,
                           redirect_to=target if status == "redirected" else None, identifiers=[self_id],
                           relations=[{"type": "replaced_by", "target": {"source": "open-library",
                                                                          "native_id": target}}] if target else [],
                           excluded=excluded)], excluded
    identifiers = [self_id]
    identifiers += [_ident("isbn10", v, locator=f"/isbn_10/{i}") for i, v in enumerate(body.get("isbn_10") or [])]
    identifiers += [_ident("isbn13", v, locator=f"/isbn_13/{i}") for i, v in enumerate(body.get("isbn_13") or [])]
    identifiers += [_ident("lccn", v, locator=f"/lccn/{i}") for i, v in enumerate(body.get("lccn") or [])]
    identifiers += [_ident("oclc", v, locator=f"/oclc_numbers/{i}") for i, v in enumerate(body.get("oclc_numbers") or [])]
    for group in ("identifiers", "remote_ids"):
        published = body.get(group) if isinstance(body.get(group), Mapping) else {}
        for name, values in sorted(published.items()):
            if name not in _OL_IDENTIFIERS:
                excluded.append(f"{group}/{name}")
                continue
            for i, v in enumerate(values if isinstance(values, list) else [values]):
                identifiers.append(_ident(_OL_IDENTIFIERS[name], v, locator=f"/{group}/{name}/{i}"))
    title = _text(body.get("title"))
    titles = [{"value": title + (f": {body['subtitle']}" if _text(body.get("subtitle")) else ""), "language": None}] \
        if title else []
    names = [{"value": _text(body.get("name")), "language": None}] if _text(body.get("name")) else []
    relations, creators = [], []
    for item in body.get("works") or []:
        work = _text(dict(item).get("key"))
        if work:
            relations.append({"type": "edition_of", "target": {"source": "open-library",
                                                                "native_id": work.split("/")[-1]}})
    for item in body.get("authors") or []:
        author = dict(item).get("author") if isinstance(dict(item).get("author"), Mapping) else item
        author_key = _text(dict(author or {}).get("key"))
        if author_key:
            relations.append({"type": "author", "target": {"source": "open-library",
                                                            "native_id": author_key.split("/")[-1]}})
            creators.append({"name": author_key.split("/")[-1], "role": "author",
                             "ref": {"scheme": "olid", "value": author_key.split("/")[-1]}})
    if _text(body.get("by_statement")) and not creators:
        creators.append({"name": _text(body["by_statement"]), "role": "author", "ref": None})
    dates = [{"kind": kind, "original": _text(body.get(field))}
             for field, kind in (("publish_date", "published"), ("first_publish_date", "first-published"),
                                 ("birth_date", "born"), ("death_date", "died")) if _text(body.get(field))]
    return [_statement("open-library", record_type, olid, revision=revision, url=url, titles=titles, names=names,
                       identifiers=_dedupe(identifiers), relations=relations, creators=creators, dates=dates,
                       excluded=excluded)], sorted(excluded)


# ------------------------------------------------------------------ MusicBrainz (MM04)

_MB_TYPES = {"recording": "recording", "release": "release", "work": "work", "artist": "creator"}
MB_INCLUDES = {"recording": "isrcs+artist-credits+work-rels+url-rels", "release": "recordings+artist-credits",
               "work": "url-rels+artist-rels", "artist": "url-rels"}


def _mb_credits(entity: Mapping[str, Any], role: str) -> tuple[list[dict], list[dict]]:
    creators, relations = [], []
    for credit in entity.get("artist-credit") or []:
        artist = dict(dict(credit).get("artist") or {})
        name = _text(dict(credit).get("name")) or _text(artist.get("name"))
        mbid = _text(artist.get("id"))
        if name:
            creators.append({"name": name, "role": role, "ref": {"scheme": "mbid", "value": mbid} if mbid else None})
        if mbid:
            relations.append({"type": role, "target": {"source": "musicbrainz", "native_id": mbid, "label": name}})
    return creators, relations


def parse_musicbrainz(entity_type: str, body: Mapping[str, Any], url: str,
                      requested: str) -> tuple[list[dict[str, Any]], list[str]]:
    if entity_type not in _MB_TYPES:
        raise SourcePackError("invalid_mapping", f"MusicBrainz entity {entity_type!r} is not in the bounded scope")
    if not isinstance(body, Mapping) or not _text(body.get("id")):
        raise SourcePackError("schema_drift", "MusicBrainz response lacks an id")
    excluded = sorted(k for k in body if k in _MB_EXCLUDED)
    core = {k: v for k, v in body.items() if k not in _MB_EXCLUDED}
    mbid = str(core["id"]).casefold()
    revision = {"marker": "mb-core:" + _digest(core)[:24], "basis": "mb-core-digest", "order": None, "date": None,
                "declared": None}
    record_type = _MB_TYPES[entity_type]
    identifiers = [_ident("mbid", mbid, "self")]
    identifiers += [_ident("isrc", v, locator=f"/isrcs/{i}") for i, v in enumerate(core.get("isrcs") or [])]
    identifiers += [_ident("iswc", v, locator=f"/iswcs/{i}") for i, v in enumerate(core.get("iswcs") or [])]
    identifiers += [_ident("isni", v, locator=f"/isnis/{i}") for i, v in enumerate(core.get("isnis") or [])]
    if _text(core.get("barcode")):
        identifiers.append(_ident("barcode", core["barcode"], locator="/barcode"))
    relations, creators = [], []
    for index, relation in enumerate(core.get("relations") or []):
        relation = dict(relation)
        target_type = relation.get("target-type")
        if target_type == "url":
            resource = str(dict(relation.get("url") or {}).get("resource") or "")
            for pattern, scheme in _MB_URL_AUTHORITIES:
                if match := pattern.search(resource):
                    identifiers.append(_ident(scheme, match.group(1), locator=f"/relations/{index}/url/resource"))
        elif target_type == "work":
            work = dict(relation.get("work") or {})
            if _text(work.get("id")):
                relations.append({"type": "recording_of", "target": {"source": "musicbrainz", "native_id": work["id"],
                                                                     "label": _text(work.get("title"))}})
        elif target_type == "artist" and relation.get("type") in {"composer", "lyricist", "writer"}:
            artist = dict(relation.get("artist") or {})
            if _text(artist.get("name")):
                creators.append({"name": artist["name"], "role": relation["type"],
                                 "ref": {"scheme": "mbid", "value": artist["id"]} if _text(artist.get("id")) else None})
                if _text(artist.get("id")):
                    relations.append({"type": "composer", "target": {"source": "musicbrainz",
                                                                     "native_id": artist["id"],
                                                                     "label": artist["name"]}})
    credits, credit_relations = _mb_credits(core, "performer")
    creators += credits
    relations += credit_relations
    for medium_index, medium in enumerate(core.get("media") or []):
        for track in dict(medium).get("tracks") or []:
            recording = dict(dict(track).get("recording") or {})
            if _text(recording.get("id")):
                relations.append({"type": "has_track", "target": {"source": "musicbrainz",
                                                                  "native_id": recording["id"],
                                                                  "label": _text(recording.get("title"))},
                                  "attributes": {"medium": medium_index + 1, "position": dict(track).get("position")}})
    group = dict(core.get("release-group") or {})
    if _text(group.get("id")):
        relations.append({"type": "release_group", "target": {"source": "musicbrainz", "native_id": group["id"]}})
    title = _text(core.get("title"))
    name = _text(core.get("name"))
    dates = [{"kind": kind, "original": _text(value)} for kind, value in (
        ("released", core.get("date")), ("first-released", core.get("first-release-date")),
        ("begin", dict(core.get("life-span") or {}).get("begin")),
        ("end", dict(core.get("life-span") or {}).get("end"))) if _text(value)]
    attributes = {k: core[k] for k in ("length", "status", "type", "country", "disambiguation") if core.get(k)}
    statements = [_statement("musicbrainz", record_type, mbid, revision=revision,
                             url=f"https://musicbrainz.org/{entity_type}/{mbid}",
                             titles=[{"value": title, "language": None}] if title else [],
                             names=[{"value": name, "language": None}] if name else [],
                             identifiers=_dedupe(identifiers), relations=relations, creators=creators, dates=dates,
                             attributes=attributes, excluded=excluded)]
    requested = requested.casefold()
    if requested != mbid:
        # A merged MBID answers with the surviving entity: the requested MBID gets a redirect revision.
        statements.append(_statement(
            "musicbrainz", record_type, requested, url=f"https://musicbrainz.org/{entity_type}/{requested}",
            revision={"marker": f"redirect:{mbid}", "basis": "provider-redirect", "order": None, "date": None,
                      "declared": None},
            status="redirected", redirect_to=mbid, identifiers=[_ident("mbid", requested, "self")],
            relations=[{"type": "replaced_by", "target": {"source": "musicbrainz", "native_id": mbid}}]))
    return statements, excluded


# ------------------------------------------------------------------ Wikidata (MM05)


def _snak_value(snak: Mapping[str, Any]) -> str | None:
    value = dict(dict(snak).get("datavalue") or {}).get("value")
    if isinstance(value, Mapping):
        if "id" in value:
            return str(value["id"])
        if "time" in value:
            return str(value["time"])
        return None
    return _text(value)


def parse_wikidata(body: Mapping[str, Any], url: str, requested: str) -> tuple[list[dict[str, Any]], list[str]]:
    entities = body.get("entities") if isinstance(body, Mapping) else None
    if not isinstance(entities, Mapping) or not entities:
        raise SourcePackError("schema_drift", "Wikidata response lacks entities")
    requested = requested.upper()
    statements = []
    for key, entity in sorted(entities.items()):
        entity = dict(entity)
        if "missing" in entity:
            statements.append(_statement(
                "wikidata", "authority-link", requested, url=f"https://www.wikidata.org/wiki/{requested}",
                revision={"marker": "missing", "basis": "provider-redirect", "order": None, "date": None,
                          "declared": None},
                status="deleted", identifiers=[_ident("wikidata", requested, "self")], describes="unknown"))
            continue
        qid = str(entity.get("id") or key).upper()
        revid, modified = entity.get("lastrevid"), _text(entity.get("modified"))
        if not str(revid or "").isdigit():
            raise SourcePackError("schema_drift", f"Wikidata entity {qid} lacks lastrevid")
        revision = {"marker": str(revid), "basis": "wikidata-revid", "order": f"{int(revid):012d}",
                    "date": modified[:10] if modified else None, "declared": modified}
        claims = entity.get("claims") if isinstance(entity.get("claims"), Mapping) else {}
        instance_of = [v for c in claims.get("P31") or [] if (v := _snak_value(dict(c).get("mainsnak") or {}))]
        describes = next((WIKIDATA_DESCRIBES[q] for q in instance_of if q in WIKIDATA_DESCRIBES), "unknown")
        identifiers = [_ident("wikidata", qid, "self")]
        creators, relations, dates = [], [], []
        for prop, values in sorted(claims.items()):
            for index, claim in enumerate(values or []):
                claim = dict(claim)
                value = _snak_value(dict(claim.get("mainsnak") or {}))
                if value is None:
                    continue
                rank = claim.get("rank") if claim.get("rank") in {"preferred", "normal", "deprecated"} else None
                refs = [{"hash": ref.get("hash"),
                         "snaks": {p: [_snak_value(s) for s in snaks] for p, snaks in sorted(
                             dict(ref.get("snaks") or {}).items())}}
                        for ref in claim.get("references") or []]
                if prop in WIKIDATA_PROPERTIES:
                    identifiers.append(_ident(WIKIDATA_PROPERTIES[prop], value, property=prop, rank=rank,
                                              references=refs, locator=f"/claims/{prop}/{index}"))
                elif prop in WIKIDATA_CREATORS:
                    creators.append({"name": value, "role": WIKIDATA_CREATORS[prop],
                                     "ref": {"scheme": "wikidata", "value": value}})
                    relations.append({"type": WIKIDATA_CREATORS[prop],
                                      "target": {"source": "wikidata", "native_id": value},
                                      "attributes": {"property": prop, "rank": rank}})
                elif prop == "P629":
                    relations.append({"type": "edition_of", "target": {"source": "wikidata", "native_id": value},
                                      "attributes": {"property": prop, "rank": rank}})
                elif prop == "P577":
                    dates.append({"kind": "published", "original": value})
        labels = dict(entity.get("labels") or {})
        texts = [{"value": str(dict(label).get("value")), "language": lang}
                 for lang, label in sorted(labels.items()) if _text(dict(label).get("value"))]
        record_type = "creator" if describes == "creator" else "authority-link"
        attributes = {"instance_of": instance_of}
        statements.append(_statement(
            "wikidata", record_type, qid, revision=revision, url=f"https://www.wikidata.org/wiki/{qid}",
            titles=texts if record_type != "creator" else [], names=texts if record_type == "creator" else [],
            identifiers=_dedupe(identifiers), relations=relations, creators=creators, dates=dates,
            attributes=attributes, describes=describes))
        redirect = dict(entity.get("redirects") or {})
        source_id = _text(redirect.get("from")) or (requested if requested != qid else None)
        if source_id and source_id.upper() != qid:
            statements.append(_statement(
                "wikidata", record_type, source_id.upper(), url=f"https://www.wikidata.org/wiki/{source_id.upper()}",
                revision={"marker": f"redirect:{qid}@{revid}", "basis": "provider-redirect", "order": None,
                          "date": revision["date"], "declared": modified},
                status="redirected", redirect_to=qid, identifiers=[_ident("wikidata", source_id, "self")],
                relations=[{"type": "replaced_by", "target": {"source": "wikidata", "native_id": qid}}],
                describes=describes))
    return statements, []


# ------------------------------------------------------------------ MARC21 (DNB and LoC, MM06)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def marc_records(raw: bytes) -> list[ET.Element]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourcePackError("schema_drift", "MARC response is not XML") from exc
    records = [el for el in root.iter() if _local(el.tag) == "record" and any(_local(c.tag) == "leader" for c in el)]
    if _local(root.tag) == "searchRetrieveResponse":
        diagnostics = [el for el in root.iter() if _local(el.tag) == "diagnostic"]
        if diagnostics and not records:
            raise SourcePackError("schema_drift", "SRU answered with a diagnostic")
    elif not records:
        raise SourcePackError("schema_drift", "MARCXML response holds no MARC record")
    return records


def _fields(record: ET.Element) -> tuple[str, dict[str, str], list[tuple[str, str, str, list[tuple[str, str]]]]]:
    leader, controls, data = "", {}, []
    for child in record:
        name = _local(child.tag)
        if name == "leader":
            leader = child.text or ""
        elif name == "controlfield":
            controls[child.get("tag", "")] = (child.text or "").strip()
        elif name == "datafield":
            subs = [(s.get("code", ""), (s.text or "").strip()) for s in child if _local(s.tag) == "subfield"]
            data.append((child.get("tag", ""), child.get("ind1", " "), child.get("ind2", " "), subs))
    return leader, controls, data


def _subs(field, code: str) -> list[str]:
    return [v for c, v in field[3] if c == code and v]


def _marc_revision(controls: Mapping[str, str]) -> dict[str, Any]:
    stamp = controls.get("005") or ""
    if not re.fullmatch(r"\d{14}(\.\d)?", stamp):
        raise SourcePackError("schema_drift", "MARC record lacks a 005 control date")
    return {"marker": stamp, "basis": "marc-005", "order": stamp,
            "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}", "declared": stamp}


def _authority_ref(value: str) -> dict[str, str] | None:
    if value.startswith("(DE-588)"):
        return {"scheme": "gnd", "value": value[8:]}
    if match := re.search(r"id\.loc\.gov/authorities/names/([a-z]{1,3}\d+)", value):
        return {"scheme": "lcnaf", "value": match.group(1)}
    if match := re.search(r"d-nb\.info/gnd/([0-9X\-]+)", value):
        return {"scheme": "gnd", "value": match.group(1)}
    return None


def parse_marc(raw: bytes, provider: str, catalogue: str, url: str) -> list[dict[str, Any]]:
    """DNB (GND authorities, catalogue) and LoC (LCNAF, catalogue) MARC21-xml records."""
    statements = []
    for record in marc_records(raw):
        leader, controls, data = _fields(record)
        revision = _marc_revision(controls)
        tags = {f[0] for f in data}
        identifiers: list[dict[str, Any] | None] = []
        native = None
        if provider == "dnb":
            gnd = next((v[8:] for f in data if f[0] == "035" for v in _subs(f, "a") if v.startswith("(DE-588)")), None)
            gnd = gnd or next((v for f in data if f[0] == "024" and "gnd" in _subs(f, "2") for v in _subs(f, "a")),
                              None)
            idn = controls.get("001")
            if catalogue == "authorities":
                native = gnd
                identifiers += [_ident("gnd", gnd, "self"), _ident("idn", idn)]
            else:
                native = idn
                identifiers += [_ident("idn", idn, "self")]
        else:
            lccn = next((v for f in data if f[0] == "010" for v in _subs(f, "a")), None)
            native = lccn.replace(" ", "") if lccn else None
            identifiers.append(_ident("lcnaf" if catalogue == "authorities" else "lccn", native, "self"))
            identifiers += [_ident("lccn", v, locator="010$z") for f in data if f[0] == "010" for v in _subs(f, "z")]
        if not native:
            raise SourcePackError("schema_drift", f"{provider} MARC record lacks its identifier")
        for field in data:
            if field[0] == "024":
                schemes = _subs(field, "2")
                scheme = {"gnd": "gnd", "viaf": "viaf", "wikidata": "wikidata", "isni": "isni", "lccn": "lccn",
                          "isrc": "isrc"}.get(schemes[0] if schemes else "")
                if field[1] == "0":
                    scheme = "isrc"
                if scheme and not (provider == "dnb" and scheme == "gnd" and catalogue == "authorities"):
                    identifiers += [_ident(scheme, v, locator="024$a") for v in _subs(field, "a")]
            elif field[0] == "020":
                for value in _subs(field, "a"):
                    isbn = re.sub(r"[^0-9Xx\-]", "", value.split(" ", 1)[0])
                    identifiers.append(_ident("isbn13" if len(re.sub(r"\D", "", isbn)) == 13 else "isbn10",
                                              isbn, locator="020$a"))
            elif field[0] == "035" and provider == "loc":
                identifiers += [_ident("oclc", v, locator="035$a") for v in _subs(field, "a") if "OCoLC" in v]
        titles, names, creators, relations, dates = [], [], [], [], []
        if catalogue == "authorities":
            heading = next((f for f in data if f[0] in {"100", "110", "111", "130"}), None)
            if heading is None:
                record_type = "authority-link"
            else:
                record_type = "work" if heading[0] == "130" else "creator"
                label = " ".join(_subs(heading, "a") + _subs(heading, "t")).strip(" ,.")
                if label:
                    (titles if record_type == "work" else names).append({"value": label, "language": None})
                dates += [{"kind": "life-dates", "original": d} for d in _subs(heading, "d")]
                if heading[0] == "130" or _subs(heading, "t"):
                    record_type = "work"
            names += [{"value": " ".join(_subs(f, "a")).strip(" ,."), "language": None, "kind": "variant"}
                      for f in data if f[0] in {"400", "410", "411"} and _subs(f, "a")]
        else:
            record_type = "edition"
            for field in data:
                if field[0] == "245":
                    label = " ".join(_subs(field, "a") + _subs(field, "b")).strip(" /:;,.")
                    if label:
                        titles.append({"value": label, "language": None})
                elif field[0] in {"100", "110", "700", "710"}:
                    name = " ".join(_subs(field, "a")).strip(" ,.")
                    refs = [r for v in _subs(field, "0") if (r := _authority_ref(v))]
                    if name:
                        role = (_subs(field, "4") or _subs(field, "e") or ["author" if field[0] == "100" else
                                                                           "contributor"])[0]
                        creators.append({"name": name, "role": role, "ref": refs[0] if refs else None})
                elif field[0] in {"260", "264"}:
                    dates += [{"kind": "published", "original": d.strip(" .")} for d in _subs(field, "c")]
        status, redirect_to = "active", None
        if len(leader) > 5 and leader[5] in {"d", "s", "x"}:
            status = "deleted"
        for field in data:
            if field[0] == "682":
                targets = [r for v in _subs(field, "0") if (r := _authority_ref(v))]
                if targets:
                    status, redirect_to = "redirected", targets[0]["value"]
                elif status == "active":
                    status = "deprecated"
        if provider == "loc" and status == "deleted" and redirect_to is None:
            replaced = [v for f in data if f[0] == "010" for v in _subs(f, "z")]
            if replaced:
                status = "deprecated"
        if redirect_to:
            relations.append({"type": "replaced_by", "target": {"source": provider, "native_id": redirect_to}})
        excluded = sorted({f"{t}" for t in tags & {"506", "540", "856"}})
        statements.append(_statement(
            provider, record_type, native, revision=revision, url=url, status=status, redirect_to=redirect_to,
            titles=titles, names=names, identifiers=_dedupe(identifiers), relations=relations, creators=creators,
            dates=dates, attributes={"catalogue": catalogue, "leader_status": leader[5:6] or None},
            excluded=[f"MARC {t}" for t in excluded]))
    return statements


# ------------------------------------------------------------------ selections


def _isbn_key(value: Any) -> str | None:
    for scheme in ("isbn13", "isbn10"):
        if (key := normalize_identifier(scheme, value)["key"]) is not None:
            return key
    return None


def _host(source: Mapping[str, Any]) -> str:
    return (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("media_metadata") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_mapping", f"media-metadata sources declare a provider in {PROVIDERS}")
    host = _host(source)
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_mapping", f"{provider} is fetched from {PROVIDER_HOSTS[provider]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= MAX_SELECTION:
        raise SourcePackError("unbounded_source",
                              f"media-metadata sources need an explicit selection of 1-{MAX_SELECTION} selectors")
    path = urlsplit(str(source["endpoint"])).path.rstrip("/")
    for entry in entries:
        keys = set(entry) - {"label"}
        if provider == "open-library":
            ok = (keys == {"olid"} and normalize_identifier("olid", entry["olid"])["valid"]) or (
                keys == {"isbn"} and _isbn_key(entry["isbn"]) is not None)
        elif provider == "musicbrainz":
            ok = keys == {"entity", "mbid"} and entry["entity"] in _MB_TYPES and \
                normalize_identifier("mbid", entry["mbid"])["valid"]
        elif provider == "wikidata":
            ok = keys in ({"qid"}, {"qid", "revision"}) and normalize_identifier("wikidata", entry["qid"])["valid"] \
                and str(entry.get("revision", "1")).isdigit()
        elif provider == "dnb":
            catalogue = path.rsplit("/", 1)[-1]
            ok = (catalogue == "authorities" and keys == {"gnd"} and normalize_identifier("gnd", entry["gnd"])["valid"]) \
                or (catalogue == "dnb" and keys == {"idn"} and normalize_identifier("idn", entry["idn"])["valid"]) \
                or (catalogue == "dnb" and keys == {"isbn"} and _isbn_key(entry["isbn"]) is not None)
        else:
            ok = (host == "id.loc.gov" and keys == {"lcnaf"} and normalize_identifier("lcnaf", entry["lcnaf"])["valid"]) \
                or (host == "lccn.loc.gov" and keys == {"lccn"} and normalize_identifier("lccn", entry["lccn"])["valid"])
        if not ok:
            raise SourcePackError("invalid_mapping", f"{provider} selector {entry!r} is not an audited identifier "
                                                     "for this endpoint")
    return provider, entries


def request_for(provider: str, source: Mapping[str, Any], entry: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """(path, query) of one selected page, relative to the source endpoint's host."""
    if provider == "open-library":
        if "olid" in entry:
            olid = str(entry["olid"]).upper()
            prefix = {"W": "works", "M": "books", "A": "authors"}[olid[-1]]
            return f"/{prefix}/{olid}.json", {}
        return f"/isbn/{re.sub(r'[^0-9X]', '', str(entry['isbn']).upper())}.json", {}
    if provider == "musicbrainz":
        return (f"/ws/2/{entry['entity']}/{str(entry['mbid']).casefold()}",
                {"fmt": "json", "inc": MB_INCLUDES[entry["entity"]]})
    if provider == "wikidata":
        query = {"action": "wbgetentities", "ids": str(entry["qid"]).upper(), "props": "info|labels|claims",
                 "format": "json", "maxlag": "5"}
        if "revision" in entry:
            query["revision"] = str(entry["revision"])
        return "/w/api.php", query
    if provider == "dnb":
        path = urlsplit(str(source["endpoint"])).path.rstrip("/")
        clause = (f"nid={entry['gnd']}" if "gnd" in entry else f"idn={entry['idn']}" if "idn" in entry
                  else f"num={re.sub(r'[^0-9X]', '', str(entry['isbn']).upper())}")
        return path, {"version": "1.1", "operation": "searchRetrieve", "query": clause,
                      "recordSchema": "MARC21-xml", "maximumRecords": "5"}
    if "lcnaf" in entry:
        return f"/authorities/names/{quote(str(entry['lcnaf']).replace(' ', ''))}.marcxml.xml", {}
    return f"/{quote(str(entry['lccn']).replace(' ', ''))}/marcxml", {}


# ------------------------------------------------------------------ runtime adapter


class MediaMetadataAdapter:
    """One page per selected identifier on the runtime's same-host HTTPS transport, paced per provider."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        # No provider needs a credential; the optional NOESIS_MEDIA_METADATA_CONTACT secret is the contact the
        # Open Library, MusicBrainz and Wikimedia User-Agent policies ask for.
        self._contact = str(secret).strip() if secret else None
        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        self._paced = transport is None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._last: float | None = None
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "media_metadata": {"provider": self.provider, "selection_size": len(self.entries),
                               "record_contract": CONTRACT,
                               "min_interval_ms": PROVIDER_CONTRACTS[self.provider]["min_interval_ms"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _pace(self) -> None:
        if not self._paced:
            return
        interval = PROVIDER_CONTRACTS[self.provider]["min_interval_ms"] / 1000
        if self._last is not None:
            wait = interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        parts = urlsplit(self.source["endpoint"])
        base = f"{parts.scheme}://{parts.netloc}{path}"
        agent = USER_AGENT if not self._contact else USER_AGENT[:-1] + f"; {self._contact})"
        headers = {"Accept": "application/json, application/xml", "User-Agent": agent}
        self._pace()
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or base)).hostname or "").casefold()
        if final_host not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("network_policy", "media metadata was served from another host")
        status = int(response.get("status", 200))
        folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429 or (status == 503 and self.provider == "musicbrainz"):
            raise SourcePackError("rate_limited", f"{self.provider} asked this client to slow down",
                                  retry_after_ms=_retry_after_ms(folded.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.provider} refused the request (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400 and status not in {404, 410}:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        return status, raw, base + ("?" + urlencode(sorted(query.items())) if query else "")

    def _json(self, raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourcePackError("schema_drift", f"{self.provider} returned a non-JSON body") from exc

    def _parse(self, entry: Mapping[str, Any], raw: bytes, url: str) -> tuple[list[dict[str, Any]], list[str]]:
        if self.provider == "open-library":
            statements, dropped = parse_open_library(self._json(raw), url)
            if "isbn" in entry and not any(
                    _isbn_key(entry["isbn"]) == normalize_identifier(i["scheme"], i["value"])["key"]
                    for s in statements for i in s["identifiers"]):
                raise SourcePackError("schema_drift", "Open Library answered an edition without the requested ISBN")
            return statements, dropped
        if self.provider == "musicbrainz":
            payload = self._json(raw)
            if isinstance(payload, Mapping) and payload.get("error"):
                return [], []
            return parse_musicbrainz(entry["entity"], payload, url, str(entry["mbid"]))
        if self.provider == "wikidata":
            payload = self._json(raw)
            if isinstance(payload, Mapping) and payload.get("error"):
                code = str(dict(payload["error"]).get("code") or "")
                if code == "maxlag":
                    raise SourcePackError("rate_limited", "Wikidata is lagged (maxlag)", retry_after_ms=5000)
                if code in {"no-such-entity"}:
                    return [], []
                raise SourcePackError("schema_drift", f"Wikidata answered error {code}")
            statements, dropped = parse_wikidata(payload, url, str(entry["qid"]))
            if "revision" in entry and not any(s["revision"]["marker"] == str(entry["revision"]) for s in statements):
                raise SourcePackError("schema_drift", "Wikidata answered another revision")
            return statements, dropped
        dnb_authority = self.provider == "dnb" and urlsplit(self.source["endpoint"]).path.rstrip("/").endswith(
            "authorities")
        catalogue = "authorities" if dnb_authority or "lcnaf" in entry else "catalogue"
        statements = parse_marc(raw, self.provider, catalogue, url)
        return statements, sorted({f for s in statements for f in s.get("excluded_fields") or []})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "media runs use the pinned selection, not ad-hoc parameters")
        scope_hash = _digest({"endpoint": self.source["endpoint"], "selection": self.entries})
        try:
            state = {"index": 0, "scope": scope_hash} if cursor is None else json.loads(cursor)
            index = int(state["index"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourcePackError("cursor_drift", "media cursor is not a valid checkpoint") from exc
        if state.get("scope") != scope_hash:
            raise SourcePackError("cursor_drift", "media cursor belongs to a different selection")
        if index >= len(self.entries):
            return RuntimePage((), None, 0, receipt={"status": 200, "selection_index": index})
        entry = self.entries[index]
        path, query = request_for(self.provider, self.source, entry)
        status, raw, url = self._get(path, query)
        statements, dropped = ([], []) if status in {404, 410} else self._parse(entry, raw, url)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("response_too_large", "page has more records than the run's result budget")
        records = [self._record(s) for s in statements]
        next_cursor = (json.dumps({"index": index + 1, "scope": scope_hash}, sort_keys=True)
                       if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt={
            "status": status, "provider": self.provider, "selection_index": index, "selector": entry,
            "selection_size": len(self.entries), "scope_hash": scope_hash,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "selector_outcome": "returned" if statements else "not_found", "statements": len(records),
            "excluded_fields_dropped": dropped, "live_verification": LIVE_VERIFICATION[self.provider]["status"],
            "min_interval_ms": PROVIDER_CONTRACTS[self.provider]["min_interval_ms"], "final_page": next_cursor is None,
        })

    def _record(self, statement: Mapping[str, Any]) -> dict[str, Any]:
        label = next((t["value"] for t in statement["titles"] + statement["names"]), statement["native_id"])
        return {
            "id": f"{statement['source']}:{statement['native_id']}@{statement['revision']['marker']}",
            "title": f"{label} ({statement['source']} {statement['record_type']}, "
                     f"revision {statement['revision']['marker']})",
            "url": statement.get("url") or self.source["endpoint"],
            "language": "und",
            **({"updated_at": statement["revision"]["date"]} if statement["revision"].get("date") else {}),
            "content": json.dumps({k: statement[k] for k in ("record_type", "source", "native_id", "titles", "names",
                                                             "identifiers", "creators", "dates")},
                                  sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "media_metadata": dict(statement),
        }


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: MediaMetadataAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by host, path and sorted query."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.netloc + parts.path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (b"" if body is None else body.encode() if isinstance(body, str)
                   else json.dumps(body, ensure_ascii=False).encode())
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = MediaMetadataAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "EXCLUDED_RECORD_CLASSES", "EXCLUDED_SOURCES", "FIXTURE_SECRET",
    "LICENCES", "LIVE_VERIFICATION", "MediaMetadataAdapter", "OPERATION", "PROVIDERS", "PROVIDER_CONTRACTS",
    "WIKIDATA_PROPERTIES", "fixture_transport", "parse_marc", "parse_musicbrainz", "parse_open_library",
    "parse_wikidata", "replay_native_fixture", "request_for", "selection_entries",
]

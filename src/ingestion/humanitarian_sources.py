"""Bounded, receipted humanitarian source acquisition (HR03-HR06, #2239 #2244 #2249 #2254).

One native source-pack connector, ``humanitarian``, driven by
``src/ingestion/source_pack_runtime.py`` with the ``humanitarian`` source pack
(``config/source_packs/humanitarian.json``). Each source declares its provider
and a bounded selection; the adapter builds only the requests the selection
allows, enforces the caps and returns ``noesis-humanitarian-record-v1``
records that the runtime projects into :mod:`src.kb.humanitarian_store`.

* **ReliefWeb** (HR03): reports (situation reports and appeals) and disaster
  entries, keyed by ReliefWeb ids, with the declared ``appname``. Publishing
  organisations, dates and country/disaster tags are stored verbatim; the body
  is referenced by its URL, never mirrored.
* **HDX** (HR04): CKAN ``package_search`` metadata keyed by dataset and
  resource ids; each metadata or resource change becomes a dataset revision.
  HXL hashtags are read from at most two header rows of eligible resources and
  stored as published; untagged columns are marked ``untagged``. HDX Connect,
  private datasets and datasets without an open reuse licence are metadata-only
  and none of their resources is read.
* **UCDP** (HR05): GED final releases and Candidate events keyed by event id and
  dataset version; ``where_prec``, ``date_prec``, violence type, actor labels
  and ``best``/``low``/``high`` stored verbatim. A complete final release emits
  an ``event_release`` record so a dropped candidate becomes a revision.
* **ACLED** (HR06): gated on the HR01 licence decision. The recorded decision
  is ``declined``: the adapter refuses to fetch (``licence_declined``) and
  queries list ACLED as "not acquired (licence)". With an accepted decision,
  a decision reference and a credential it would acquire ACLED's own coding.

Free-text fields that can name individuals (UCDP headlines and article text,
``where_description``, ACLED ``notes``) are dropped at parse time. Nothing is
merged across coders, summed or estimated.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-humanitarian-record-v1"
FIXTURE_SECRET = "fixture-humanitarian-credential"
AUDIT = "docs/development/humanitarian-evidence/source-audit.md"
PROVIDERS = ("reliefweb", "hdx", "ucdp-ged", "ucdp-candidate", "acled")
PROVIDER_HOSTS = {
    "reliefweb": frozenset({"api.reliefweb.int"}),
    "hdx": frozenset({"data.humdata.org"}),
    "ucdp-ged": frozenset({"ucdpapi.pcr.uu.se"}),
    "ucdp-candidate": frozenset({"ucdpapi.pcr.uu.se"}),
    "acled": frozenset({"acleddata.com"}),
}
# Hard ceilings the adapter enforces on top of the source-pack budgets.
CAPS = {"reliefweb_limit": 100, "hdx_rows": 50, "hdx_hxl_resources": 10, "ucdp_pagesize": 1000,
        "window_days": 366, "acled_limit": 500, "hxl_header_bytes": 65_536}
ACLED_DECISION = {
    "status": "declined",
    "recorded": "2026-09-29",
    "reference": AUDIT + "#acled-licence-and-access-decision",
    "terms_url": "https://acleddata.com/terms-of-use/",
    "reason": "the terms restrict redistribution of raw or near-raw data and require a separate licence for "
              "commercial use; storing events and exporting them in evidence bundles would exceed the licence",
    "query_notice": "not acquired (licence)",
}
PROVIDER_CONTRACTS = {
    "reliefweb": {
        "documentation": "https://apidoc.reliefweb.int/",
        "access": "ReliefWeb API v2 JSON (reports, disasters); declared appname on every request",
        "authentication": "none; appname registration (verify whether pre-approval is mandatory)",
        "licence": "per report source; ReliefWeb attribution; bodies referenced by URL, never mirrored",
        "rate_limits": "1000 calls/day, 1000 entries per call (verify)",
        "paging": "limit/offset",
        "revision_model": "date.changed per report or disaster; a changed entry is a new revision",
        "status": "unverified-live",
    },
    "hdx": {
        "documentation": "https://data.humdata.org/faq and https://hxlstandard.org/",
        "access": "HDX CKAN action API package_search; resource header rows for HXL tags",
        "authentication": "none for public datasets",
        "licence": "per dataset (stored per revision); HDX Connect/private/non-open datasets are metadata-only",
        "rate_limits": "no published hard limit (verify)",
        "paging": "rows/start",
        "revision_model": "metadata_modified plus each resource's last_modified and hash",
        "status": "unverified-live",
    },
    "ucdp-ged": {
        "documentation": "https://ucdpapi.pcr.uu.se/ and https://ucdp.uu.se/downloads/",
        "access": "UCDP API gedevents/<GED version>",
        "authentication": "access token header x-ucdp-access-token (verify whether mandatory)",
        "licence": "CC BY 4.0 with the UCDP dataset citation (verify)",
        "rate_limits": "pagesize up to 1000 (verify)",
        "paging": "pagesize/page with TotalPages",
        "revision_model": "the dataset version is the release; each release of an event id is a revision",
        "status": "unverified-live",
    },
    "ucdp-candidate": {
        "documentation": "https://ucdp.uu.se/downloads/",
        "access": "UCDP API gedevents/<candidate version>",
        "authentication": "as UCDP GED",
        "licence": "as UCDP GED; candidate events are provisional",
        "rate_limits": "as UCDP GED",
        "paging": "as UCDP GED",
        "revision_model": "monthly candidate versions, replaced or dropped by the next final GED release",
        "status": "unverified-live",
    },
    "acled": {
        "documentation": "https://acleddata.com/",
        "access": "ACLED API (credentialed); not used",
        "authentication": "myACLED account and API credential",
        "licence": "restricted redistribution; separate commercial licence",
        "status": "declined",
        "decision": ACLED_DECISION,
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "declined" if provider == "acled" else "unverified-live", "checked": None, "evidence": None,
               "note": "no dated live run yet (HR14, #2293)" if provider != "acled" else ACLED_DECISION["query_notice"]}
    for provider in PROVIDERS
}
REPORT_FORMATS = {"Situation Report": "situation_report", "Appeal": "appeal", "Flash Appeal": "appeal"}
OPEN_LICENCES = frozenset({"cc-by", "cc-by-sa", "cc-by-igo", "cc0", "odc-by", "odc-odbl", "odc-pddl", "hdx-pddl"})
PERSONAL_HXL = {"#contact": {"name", "email", "phone"}, "#beneficiary": set(), "#indiv": set()}
WHERE_PREC = {1: "exact location", 2: "within about 25 km of a known point", 3: "second-order administrative division",
              4: "first-order administrative division", 5: "linear feature or larger area", 6: "country only",
              7: "international waters or airspace"}
DATE_PREC = {1: "exact date", 2: "within 2-6 days", 3: "within a week", 4: "within a month (8-30 days)",
             5: "more than a month"}
VIOLENCE_TYPE = {1: "state-based conflict", 2: "non-state conflict", 3: "one-sided violence"}
DROPPED_UCDP_FIELDS = ("source_headline", "source_original", "source_article", "where_description")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days


def _wrap(record: dict[str, Any]) -> dict[str, Any]:
    """Runtime envelope: stable document identity per record; the store owns revisions."""

    return {"id": f"{record['source']}:{record['record_type']}:{record['source_id']}", "title": record["title"],
            "url": record.get("source_url"), "published_at": record["as_of"], "language": "en",
            "humanitarian_record": record}


# --------------------------------------------------------------------------- ReliefWeb (HR03)


def _names(items: Any, *keys: str) -> list[dict[str, Any]]:
    return [{k: item.get(k) for k in keys if item.get(k) is not None} for item in items or [] if isinstance(item, Mapping)]


def parse_reliefweb_reports(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records, skipped = [], 0
    for item in payload.get("data") or []:
        fields = dict(item.get("fields") or {})
        formats = [f.get("name") for f in fields.get("format") or [] if f.get("name")]
        kinds = [REPORT_FORMATS[f] for f in formats if f in REPORT_FORMATS]
        if not kinds:
            skipped += 1
            continue
        dates = dict(fields.get("date") or {})
        changed = dates.get("changed") or dates.get("created")
        url = fields.get("url_alias") or fields.get("url")
        records.append({
            "contract": RECORD_CONTRACT, "record_type": "appeal" if "appeal" in kinds else "situation_report",
            "source": "reliefweb", "source_id": str(fields.get("id") or item.get("id")), "revision": str(changed),
            "as_of": changed, "title": fields.get("title"), "source_url": url,
            "attribution": "ReliefWeb (OCHA); content from the listed sources",
            "publishers": _names(fields.get("source"), "id", "name", "shortname"),
            "report_date": dates.get("original") or dates.get("created"),
            "formats": formats, "language": [lang.get("code") for lang in fields.get("language") or []],
            "disasters": _names(fields.get("disaster"), "id", "name", "glide"),
            "places": [{"name": c.get("name"), "code": c.get("iso3"), "scheme": "iso3", "level": "country",
                        "role": "primary" if c.get("primary") or (fields.get("primary_country") or {}).get("iso3") == c.get("iso3")
                        else "tagged"}
                       for c in fields.get("country") or []],
            "body": {"locator": url, "mirrored": False},
            "unknowns": [] if dates.get("original") else ["report_date is the ReliefWeb posting date (no original date)"],
        })
    return records, {"out_of_scope_format": skipped}


def parse_reliefweb_disasters(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records = []
    for item in payload.get("data") or []:
        fields = dict(item.get("fields") or {})
        dates = dict(fields.get("date") or {})
        changed = dates.get("changed") or dates.get("created")
        records.append({
            "contract": RECORD_CONTRACT, "record_type": "crisis", "source": "reliefweb",
            "source_id": str(fields.get("id") or item.get("id")), "revision": str(changed), "as_of": changed,
            "title": fields.get("name"), "name": fields.get("name"), "glide": fields.get("glide"),
            "status": fields.get("status"), "event_date": dates.get("event"),
            "types": [t.get("name") for t in fields.get("type") or [] if t.get("name")],
            "source_url": fields.get("url_alias") or fields.get("url"), "attribution": "ReliefWeb (OCHA)",
            "places": [{"name": c.get("name"), "code": c.get("iso3"), "scheme": "iso3", "level": "country"}
                       for c in fields.get("country") or []],
            "unknowns": [] if fields.get("glide") else ["no GLIDE number published"],
        })
    return records, {}


# --------------------------------------------------------------------------- HDX + HXL (HR04)


def parse_hxl_header(text: str) -> dict[str, Any]:
    """HXL hashtag row among the first two rows; columns stored as published, untagged ones marked."""

    rows = list(csv.reader(io.StringIO(text)))[:2]
    for index, row in enumerate(rows):
        cells = [c.strip() for c in row]
        filled = [c for c in cells if c]
        if filled and all(c.startswith("#") for c in filled):
            headers = rows[0] if index == 1 else [None] * len(cells)
            columns = []
            for position, cell in enumerate(cells):
                header = headers[position].strip() if position < len(headers) and headers[position] else None
                if not cell:
                    columns.append({"header": header, "hashtag": None, "attributes": [], "status": "untagged",
                                    "personal_data_tag": False})
                    continue
                hashtag, *attributes = [part.strip() for part in cell.split("+")]
                hashtag = hashtag.lower()
                personal = hashtag in PERSONAL_HXL and (not PERSONAL_HXL[hashtag] or bool(
                    PERSONAL_HXL[hashtag] & {a.lower() for a in attributes}))
                columns.append({"header": header, "hashtag": hashtag, "attributes": [a.lower() for a in attributes],
                                "status": "tagged", "personal_data_tag": personal})
            return {"status": "tagged", "hashtag_row": index + 1, "columns": columns}
    return {"status": "untagged", "columns": [{"header": c.strip() or None, "hashtag": None, "attributes": [],
                                               "status": "untagged", "personal_data_tag": False}
                                              for c in (rows[0] if rows else [])]}


def hdx_access(package: Mapping[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    licence_id = str(package.get("license_id") or "").lower() or None
    reuse = "open" if licence_id in OPEN_LICENCES else "unknown" if not licence_id else "restricted"
    access = "hdx-connect" if package.get("is_requestdata_type") else "private" if package.get("private") else "public"
    reasons = []
    if access != "public":
        reasons.append(f"{access} dataset: metadata only, no resource is read")
    if reuse != "open":
        reasons.append(f"licence {licence_id or 'not stated'} is not an open reuse licence: metadata only")
    return access, {"id": licence_id, "title": package.get("license_title"), "reuse": reuse}, reasons


def parse_hdx_package(package: Mapping[str, Any], headers: Mapping[str, Any]) -> dict[str, Any]:
    access, licence, reasons = hdx_access(package)
    metadata_only = bool(reasons)
    resources = []
    for resource in package.get("resources") or []:
        read = headers.get(str(resource.get("id")))
        hxl = {"status": "not-read", "columns": [], "reason": read.get("reason") if isinstance(read, Mapping)
               and read.get("reason") else ("metadata-only dataset" if metadata_only else "not an HXL resource")}
        if isinstance(read, Mapping) and read.get("text") is not None and not metadata_only:
            hxl = {**parse_hxl_header(read["text"]), "header_sha256": read["sha256"]}
        resources.append({"resource_id": str(resource.get("id")), "name": resource.get("name"),
                          "format": resource.get("format"), "url": resource.get("url"),
                          "last_modified": resource.get("last_modified"), "hash": resource.get("hash") or None,
                          "size": resource.get("size"), "hxl": hxl,
                          "personal_data_tags": any(c.get("personal_data_tag") for c in hxl.get("columns") or [])})
    organization = dict(package.get("organization") or {})
    modified = package.get("metadata_modified")
    return {
        "contract": RECORD_CONTRACT, "record_type": "dataset", "source": "hdx", "source_id": str(package.get("id")),
        "revision": str(modified), "as_of": modified, "title": package.get("title") or package.get("name"),
        "name": package.get("name"), "source_url": f"https://data.humdata.org/dataset/{package.get('name')}",
        "attribution": f"HDX; dataset by {organization.get('title') or organization.get('name') or 'the stated organisation'}",
        "organization": {"id": organization.get("id"), "name": organization.get("name"), "title": organization.get("title")},
        "licence": licence, "access": access, "metadata_only": metadata_only, "metadata_only_reasons": reasons,
        "dataset_date": package.get("dataset_date"), "tags": sorted(t.get("name") for t in package.get("tags") or []),
        "groups": sorted(g.get("name") for g in package.get("groups") or []),
        "places": [{"name": g.get("title") or g.get("name"), "code": str(g.get("name") or "").upper() or None,
                    "scheme": "hdx-group", "level": "country"} for g in package.get("groups") or []],
        "resources": resources,
        "unknowns": [f"resource {r['resource_id']} publishes no hash" for r in resources if not r["hash"]],
    }


# --------------------------------------------------------------------------- UCDP (HR05)


def _precision(code: Any, scheme: str, labels: Mapping[int, str]) -> dict[str, Any]:
    try:
        label = labels.get(int(code))
    except (TypeError, ValueError):
        label = None
    return {"code": code, "scheme": scheme, "label": label}


def parse_ucdp_event(item: Mapping[str, Any], *, provider: str, version: str, as_of: str, url: str) -> dict[str, Any]:
    item = {k: v for k, v in item.items() if k not in DROPPED_UCDP_FIELDS}
    published = item.get("latitude") is not None and item.get("longitude") is not None
    places = [{"name": item.get("country"), "code": str(item.get("country_id")) if item.get("country_id") is not None
               else None, "scheme": "gw", "level": "country"}]
    for level in ("adm_1", "adm_2"):
        if item.get(level):
            places.append({"name": item[level], "code": None, "scheme": "ucdp-adm",
                           "level": "admin1" if level == "adm_1" else "admin2"})
    return {
        "contract": RECORD_CONTRACT, "record_type": "conflict_event", "source": provider, "coding_source": provider,
        "source_id": str(item.get("id")), "revision": version, "dataset_version": version, "as_of": as_of,
        "coding_status": "final" if provider == "ucdp-ged" else "candidate",
        "title": f"UCDP event {item.get('id')} ({item.get('dyad_name') or item.get('conflict_name') or 'unnamed dyad'})",
        "source_url": url, "attribution": "Uppsala Conflict Data Program (UCDP), GED/Candidate as versioned",
        "relid": item.get("relid"), "event_clarity": item.get("event_clarity"),
        "precision": {"where": _precision(item.get("where_prec"), "ucdp-where_prec", WHERE_PREC),
                      "when": _precision(item.get("date_prec"), "ucdp-date_prec", DATE_PREC),
                      "type": _precision(item.get("type_of_violence"), "ucdp-type_of_violence", VIOLENCE_TYPE)},
        "conflict": {"id": item.get("conflict_new_id"), "name": item.get("conflict_name")},
        "dyad": {"id": item.get("dyad_new_id"), "name": item.get("dyad_name")},
        "actors": [a for a in ({"role": "side_a", "label": item.get("side_a"), "coder_id": item.get("side_a_new_id")},
                               {"role": "side_b", "label": item.get("side_b"), "coder_id": item.get("side_b_new_id")})
                   if a["label"]],
        "counts": {k: item.get(k) for k in ("best", "low", "high", "deaths_a", "deaths_b", "deaths_civilians",
                                             "deaths_unknown") if k in item},
        "date_start": item.get("date_start"), "date_end": item.get("date_end"),
        "location": {"latitude": item.get("latitude"), "longitude": item.get("longitude"), "published": published,
                     "country_code": str(item.get("country_id")) if item.get("country_id") is not None else None,
                     "adm_1": item.get("adm_1"), "adm_2": item.get("adm_2"), "priogrid_gid": item.get("priogrid_gid")},
        "sources": {"number_of_sources": item.get("number_of_sources"), "source_office": item.get("source_office"),
                    "source_date": item.get("source_date")},
        "places": places,
        "unknowns": [] if published else ["no coordinates published"],
    }


# --------------------------------------------------------------------------- ACLED (HR06, gated)


def parse_acled_event(item: Mapping[str, Any], *, as_of: str, url: str) -> dict[str, Any]:
    item = {k: v for k, v in item.items() if k not in {"notes", "source"}}
    published = item.get("latitude") not in (None, "") and item.get("longitude") not in (None, "")
    places = [{"name": item.get("country"), "code": item.get("iso3") or None, "scheme": "iso3" if item.get("iso3")
               else "name-only", "level": "country"}]
    places += [{"name": item[k], "code": None, "scheme": "acled-admin", "level": k}
               for k in ("admin1", "admin2", "admin3") if item.get(k)]
    return {
        "contract": RECORD_CONTRACT, "record_type": "conflict_event", "source": "acled", "coding_source": "acled",
        "source_id": str(item.get("event_id_cnty")), "revision": str(item.get("timestamp")), "as_of": as_of,
        "dataset_version": f"acled-timestamp-{item.get('timestamp')}", "coding_status": "final",
        "title": f"ACLED event {item.get('event_id_cnty')} ({item.get('event_type')})", "source_url": url,
        "attribution": "Armed Conflict Location & Event Data (ACLED)",
        "precision": {"where": {"code": item.get("geo_precision"), "scheme": "acled-geo_precision", "label": None},
                      "when": {"code": item.get("time_precision"), "scheme": "acled-time_precision", "label": None},
                      "type": {"code": item.get("sub_event_type"), "scheme": "acled-sub_event_type",
                               "label": item.get("event_type")}},
        "actors": [{"role": role, "label": item.get(role)} for role in ("actor1", "assoc_actor_1", "actor2", "assoc_actor_2")
                   if item.get(role)],
        "counts": {"fatalities": item.get("fatalities")},
        "date_start": item.get("event_date"), "date_end": item.get("event_date"),
        "location": {"latitude": item.get("latitude"), "longitude": item.get("longitude"), "published": published,
                     "country_code": item.get("iso3"), "location_name": item.get("location")},
        "places": places, "unknowns": [],
    }


# --------------------------------------------------------------------------- adapter


class HumanitarianAdapter:
    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        config = dict(self.source.get("humanitarian") or {})
        self.provider = str(config.get("provider") or "")
        if self.provider not in PROVIDERS:
            raise SourcePackError("invalid_source", f"unknown humanitarian provider {self.provider!r}")
        self.selection = dict(config.get("selection") or {})
        self.config = config
        self.secret = secret
        self.execution = "injected" if transport is not None else "network"
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.max_pages = int(source["budgets"]["max_pages"])
        self._ucdp_ids: dict[int, list[str]] = {}
        self._validate()
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "humanitarian": {"provider": self.provider, "selection": self.selection, "caps": CAPS},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    # -------------------------------------------------------------- bounds

    def _validate(self) -> None:
        s = self.selection
        host = urlsplit(self.source["endpoint"]).hostname
        if host not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("unsafe_endpoint", f"{self.provider} endpoint must be on {sorted(PROVIDER_HOSTS[self.provider])}")
        if self.provider == "reliefweb":
            if not str(self.config.get("appname") or "").strip():
                raise SourcePackError("invalid_source", "ReliefWeb requests declare an appname")
            if s.get("resource") not in {"reports", "disasters"} or not s.get("countries"):
                raise SourcePackError("unbounded_source", "ReliefWeb selections name reports/disasters and countries")
            if not 1 <= int(s.get("limit") or 0) <= CAPS["reliefweb_limit"]:
                raise SourcePackError("unbounded_source", f"ReliefWeb limit is 1-{CAPS['reliefweb_limit']}")
        elif self.provider == "hdx":
            if not s.get("groups") or not 1 <= int(s.get("rows") or 0) <= CAPS["hdx_rows"]:
                raise SourcePackError("unbounded_source", f"HDX selections name groups and rows 1-{CAPS['hdx_rows']}")
            if not 0 <= int(s.get("max_hxl_resources", 0)) <= CAPS["hdx_hxl_resources"]:
                raise SourcePackError("unbounded_source", f"at most {CAPS['hdx_hxl_resources']} HXL header reads")
        elif self.provider in {"ucdp-ged", "ucdp-candidate"}:
            release = dict(s.get("release") or {})
            if not s.get("version") or not release.get("published_on") or not s.get("country"):
                raise SourcePackError("unbounded_source", "UCDP selections pin a version, its release date and a country")
            if not 1 <= int(s.get("pagesize") or 0) <= CAPS["ucdp_pagesize"]:
                raise SourcePackError("unbounded_source", f"UCDP pagesize is 1-{CAPS['ucdp_pagesize']}")
            if not s.get("start_date") or not s.get("end_date") or not 0 <= _days(s["start_date"], s["end_date"]) <= CAPS["window_days"]:
                raise SourcePackError("unbounded_source", f"UCDP windows are closed and at most {CAPS['window_days']} days")
        elif self.provider == "acled":
            decision = dict(self.config.get("licence_decision") or {})
            if not decision.get("reference"):
                raise SourcePackError("invalid_source", "the ACLED entry references the HR01 licence decision")

    def _get(self, url: str, params: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> tuple[int, bytes]:
        host = urlsplit(url).hostname
        if host not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("unsafe_endpoint", "request host is outside the provider's declared hosts")
        response = self.transport(url=url, params=dict(params), headers=dict(headers or {}),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds the source byte budget")
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.provider} returned HTTP {status}")
        if status == 429:
            raise SourcePackError("rate_limited", f"{self.provider} returned HTTP 429")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        return status, raw

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise SourcePackError("schema_drift", "response is not JSON") from exc

    def _page(self, records, next_cursor, raw_total, receipt):
        from src.ingestion.source_pack_runtime import RuntimePage

        return RuntimePage(tuple(_wrap(r) for r in records), next_cursor, raw_total,
                           receipt={"provider": self.provider, "execution": self.execution, **receipt})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "humanitarian selections are declared in the source pack")
        page = int(cursor or 0)
        if page >= self.max_pages:
            raise SourcePackError("budget_exhausted", "page ceiling reached")
        fetch = {"reliefweb": self._fetch_reliefweb, "hdx": self._fetch_hdx, "ucdp-ged": self._fetch_ucdp,
                 "ucdp-candidate": self._fetch_ucdp, "acled": self._fetch_acled}[self.provider]
        return fetch(page)

    # -------------------------------------------------------------- providers

    def _fetch_reliefweb(self, page: int):
        s = self.selection
        resource = s["resource"]
        limit = int(s["limit"])
        params: dict[str, Any] = {"appname": self.config["appname"], "limit": limit, "offset": page * limit,
                                  "profile": "full", "filter[operator]": "AND",
                                  "filter[conditions][0][field]": "country.iso3",
                                  "filter[conditions][0][value]": ",".join(s["countries"])}
        if s.get("disaster_ids"):
            params["filter[conditions][1][field]"] = "disaster.id"
            params["filter[conditions][1][value]"] = ",".join(str(d) for d in s["disaster_ids"])
        if s.get("date_from"):
            params["filter[conditions][2][field]"] = "date.created"
            params["filter[conditions][2][value][from]"] = s["date_from"]
            params["filter[conditions][2][value][to]"] = s.get("date_to") or s["date_from"]
        url = self.source["endpoint"].rstrip("/") + "/" + resource
        status, raw = self._get(url, params)
        payload = self._json(raw)
        if not isinstance(payload, Mapping) or "data" not in payload:
            raise SourcePackError("schema_drift", "ReliefWeb response has no data list")
        records, skipped = (parse_reliefweb_reports if resource == "reports" else parse_reliefweb_disasters)(payload)
        total = int(payload.get("totalCount") or 0)
        more = (page + 1) * limit < total and page + 1 < self.max_pages
        return self._page(records, str(page + 1) if more else None, len(raw), {
            "status": status, "url": url, "page_sha256": _sha(raw), "offset": page * limit, "total": total,
            "records": len(records), **skipped, "truncated": (page + 1) * limit < total and not more})

    def _hxl_header(self, resource: Mapping[str, Any]) -> dict[str, Any]:
        url = str(resource.get("url") or "")
        if urlsplit(url).hostname not in PROVIDER_HOSTS["hdx"]:
            return {"reason": "resource hosted outside data.humdata.org; not read"}
        _, raw = self._get(url, {}, {"Range": f"bytes=0-{CAPS['hxl_header_bytes'] - 1}"})
        lines = raw[: CAPS["hxl_header_bytes"]].decode("utf-8", errors="replace").splitlines()[:2]
        text = "\n".join(lines)
        return {"text": text, "sha256": _sha(text.encode())}

    def _fetch_hdx(self, page: int):
        s = self.selection
        rows = int(s["rows"])
        fq = " OR ".join(f"groups:{g}" for g in s["groups"])
        params = {"fq": fq, "rows": rows, "start": page * rows, "sort": "metadata_modified desc"}
        if s.get("query"):
            params["q"] = s["query"]
        url = self.source["endpoint"].rstrip("/") + "/package_search"
        status, raw = self._get(url, params)
        payload = self._json(raw)
        result = dict(payload.get("result") or {}) if isinstance(payload, Mapping) and payload.get("success") else None
        if result is None:
            raise SourcePackError("schema_drift", "HDX package_search did not succeed")
        budget = int(s.get("max_hxl_resources", 0))
        read, records = 0, []
        for package in result.get("results") or []:
            _, _, reasons = hdx_access(package)
            headers: dict[str, Any] = {}
            hxl_dataset = "hxl" in {str(t.get("name")).lower() for t in package.get("tags") or []}
            for resource in package.get("resources") or []:
                if reasons or not hxl_dataset or str(resource.get("format") or "").upper() != "CSV":
                    continue
                if read >= budget:
                    headers[str(resource.get("id"))] = {"reason": "HXL header budget reached for this run"}
                    continue
                headers[str(resource.get("id"))] = self._hxl_header(resource)
                read += 1
            records.append(parse_hdx_package(package, headers))
        total = int(result.get("count") or 0)
        more = (page + 1) * rows < total and page + 1 < self.max_pages
        return self._page(records, str(page + 1) if more else None, len(raw), {
            "status": status, "url": url, "page_sha256": _sha(raw), "start": page * rows, "total": total,
            "records": len(records), "hxl_headers_read": read, "truncated": (page + 1) * rows < total and not more})

    def _fetch_ucdp(self, page: int):
        s = self.selection
        version = str(s["version"])
        url = self.source["endpoint"].rstrip("/") + f"/gedevents/{version}"
        params = {"pagesize": int(s["pagesize"]), "page": page, "Country": s["country"],
                  "StartDate": s["start_date"], "EndDate": s["end_date"]}
        headers = {"x-ucdp-access-token": self.secret} if self.secret else {}
        status, raw = self._get(url, params, headers)
        payload = self._json(raw)
        if not isinstance(payload, Mapping) or "Result" not in payload:
            raise SourcePackError("schema_drift", "UCDP response has no Result list")
        as_of = str(s["release"]["published_on"])
        locator = f"{url}?pagesize={params['pagesize']}&page={page}&Country={s['country']}"
        records = [parse_ucdp_event(item, provider=self.provider, version=version, as_of=as_of, url=locator)
                   for item in payload.get("Result") or []]
        self._ucdp_ids[page] = [r["source_id"] for r in records]
        total_pages = int(payload.get("TotalPages") or 1)
        if total_pages > self.max_pages:
            raise SourcePackError("budget_exhausted", "UCDP release has more pages than the source allows; narrow "
                                                      "the window or raise the declared page budget")
        more = page + 1 < total_pages
        complete = not more and set(self._ucdp_ids) == set(range(total_pages))
        if not more and self.provider == "ucdp-ged":
            ids = sorted({i for chunk in self._ucdp_ids.values() for i in chunk})
            records.append({
                "contract": RECORD_CONTRACT, "record_type": "event_release", "source": "ucdp-ged",
                "coding_source": "ucdp-ged", "source_id": f"ged-{version}-{s['country']}-{s['start_date']}-{s['end_date']}",
                "revision": version, "dataset_version": version, "as_of": as_of,
                "title": f"UCDP GED {version} for GW {s['country']}, {s['start_date']} to {s['end_date']}",
                "source_url": url, "attribution": "Uppsala Conflict Data Program (UCDP)", "complete": complete,
                "country": str(s["country"]), "window": {"start": s["start_date"], "end": s["end_date"]},
                "event_ids": ids, "places": [], "unknowns": [] if complete else
                ["release read incompletely in this run; no candidate is marked dropped"],
            })
        return self._page(records, str(page + 1) if more else None, len(raw), {
            "status": status, "url": url, "page_sha256": _sha(raw), "page": page, "total_pages": total_pages,
            "total": payload.get("TotalCount"), "records": len(records),
            "dropped_free_text_fields": list(DROPPED_UCDP_FIELDS)})

    def _fetch_acled(self, page: int):
        decision = dict(self.config.get("licence_decision") or {})
        if decision.get("status") != "accepted":
            raise SourcePackError("licence_declined", "ACLED is not acquired under the recorded licence decision "
                                                      f"({decision.get('reference')})")
        if not self.secret:
            raise SourcePackError("authentication_failed", "an accepted ACLED decision still needs a credential")
        s = self.selection
        limit = min(int(s.get("limit") or 0), CAPS["acled_limit"])
        if not limit or not s.get("country") or not s.get("start_date") or not s.get("end_date") or \
                _days(s["start_date"], s["end_date"]) > CAPS["window_days"]:
            raise SourcePackError("unbounded_source", "ACLED selections name a country, a closed window and a limit")
        url = self.source["endpoint"].rstrip("/") + "/acled/read"
        params = {"_format": "json", "country": s["country"], "event_date": f"{s['start_date']}|{s['end_date']}",
                  "event_date_where": "BETWEEN", "limit": limit, "page": page + 1}
        status, raw = self._get(url, params, {"Authorization": f"Bearer {self.secret}"})
        payload = self._json(raw)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            raise SourcePackError("schema_drift", "ACLED response has no data list")
        from src.kb.humanitarian_records import iso

        records = [parse_acled_event(item, as_of=iso(int(item.get("timestamp") or 0) * 1000), url=url) for item in data]
        more = len(data) == limit and page + 1 < self.max_pages
        return self._page(records, str(page + 1) if more else None, len(raw), {
            "status": status, "url": url, "page_sha256": _sha(raw), "page": page + 1, "records": len(records),
            "licence_decision": decision, "dropped_free_text_fields": ["notes", "source"]})


ADAPTERS = {"humanitarian": HumanitarianAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native responses by URL and parameter subset (first match wins)."""

    served = [dict(p) for p in pages]

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        for page in served:
            wanted = {k: str(v) for k, v in dict(page.get("params") or {}).items()}
            if page["url"] == url and all(str(params.get(k)) == v for k, v in wanted.items()):
                body = page.get("json")
                content = json.dumps(body).encode() if body is not None else str(page.get("text") or "").encode()
                return {"status": int(page.get("status", 200)), "headers": {}, "content": content}
        return {"status": 404, "headers": {}, "content": b""}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = HumanitarianAdapter(source, transport=fixture_transport(list(fixture["native_pages"])), secret=FIXTURE_SECRET)
    records, cursor = [], None
    request = {"operation": min(source["operations"]), "parameters": {}}
    while True:
        page = adapter.fetch_page(request, cursor=cursor)
        records += [dict(r) for r in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records

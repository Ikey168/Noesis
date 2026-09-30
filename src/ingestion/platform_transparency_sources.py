"""Platform transparency acquisition for the OSINT pack (#2580, SP01, SP03-SP06).

One native connector, ``platform-transparency``, reads a bounded, declared
selection from one documented provider per source and emits
``noesis-platform-transparency-record-v1`` records exactly as the platform or
the regulator's database published them:

* ``dsa-sor-daily-dump-csv`` - the EU DSA Transparency Database daily dump of
  one platform for one day in its *light* version
  (``/explore-data/download/sor-{platform}-{date}-light.zip`` and its
  ``.zip.sha1``): one ``statement-of-reasons`` record per row, keyed by platform
  and statement ``uuid``, and one ``dump-release`` record naming the dump
  version (platform, date, version, published SHA-1, rows) that every
  statement cites;
* ``meta-ads-archive-json`` - Meta Ad Library API ``/ads_archive`` for
  ``POLITICAL_AND_ISSUE_ADS`` of declared page ids, countries and delivery
  window: ``ad`` records with the page as the advertiser as declared, the
  ``bylines`` disclaimer as the funding entity as declared, delivery dates, and
  spend and impression ranges with currency exactly as published; one
  ``advertiser`` record per page and one ``listing`` record per unit;
* ``google-political-ads-bigquery-json`` - the ``google_political_ads`` public
  dataset (``advertiser_stats`` and ``creative_stats``) read through the
  BigQuery REST API with a fixed, parameterised query per declared advertiser
  and region, plus the table's ``lastModifiedTime`` as the data refresh date:
  ``advertiser`` records with the identifiers Google publishes
  (``public_ids_list``), ``ad`` records with the spend range in the declared
  currency and the impression bucket verbatim, and one ``listing`` per unit.

Lumen takedown notices are **not implemented** (SP06): research access is an
individually issued token under the Lumen API Terms of Use, no token is held
and the terms could not be read for this audit; ``PROVIDER_CONTRACTS["lumen"]``
records the reason. The record kind ``takedown-notice`` and its minimisation
rule exist so that a granted access can be added without a new store.

**Data minimisation (SP01).** Nothing here is user-level: no platform user
identifier (the DSA ``platform_uid``/``puid``), no notifier identity
(``source_identity``), no free-text explanation or ``*_other`` field, no
targeting, demographic or regional delivery breakdown and no creative text is
requested or stored; they are dropped *here* and listed under
``minimisation.withheld``. The store refuses any record that still carries
one. Spend and impression ranges are stored exactly as published and never
converted into a midpoint or any other point estimate.

A unit is all-or-nothing: a response longer than its declared bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest; access tokens travel in the ``Authorization`` header and never appear
in a receipt, a record or a locator. Nothing here profiles users, collects
private content or infers coordinated behaviour.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-platform-transparency-record-v1"
RECEIPT_CONTRACT = "noesis-platform-transparency-acquisition-receipt-v1"
MINIMISATION_POLICY = "platform-transparency-minimisation-v1"
CONNECTOR = "platform-transparency"
MAX_UNITS = 31
DSA_ROW_CAP = 20000
META_PAGE_SIZE = 100
META_MAX_PAGES = 5
META_MAX_PAGE_IDS = 10
META_MAX_WINDOW_DAYS = 366
GOOGLE_ROW_CAP = 500
REVIEW_BOUNDARY = ("Records are what the platforms and the DSA Transparency Database published. No user-level "
                   "profiling, no collection of private content, no inference of coordinated behaviour and no "
                   "conversion of spend or impression ranges into point estimates.")
RECORD_KINDS = ("statement-of-reasons", "dump-release", "ad", "advertiser", "listing", "takedown-notice")
PROVIDERS = ("dsa-transparency-db", "meta-ad-library", "google-political-ads", "lumen")

# format -> provider, the selection list it reads, and whether a secret is required
FORMATS: dict[str, dict[str, Any]] = {
    "dsa-sor-daily-dump-csv": {"provider": "dsa-transparency-db", "unit": "dumps", "keyed": False},
    "meta-ads-archive-json": {"provider": "meta-ad-library", "unit": "pages", "keyed": True},
    "google-political-ads-bigquery-json": {"provider": "google-political-ads", "unit": "advertisers",
                                           "keyed": True},
}

# SP01 access decisions. Every item marked ``verify`` must be checked against the live documentation, terms and a
# real response before a dated live run (SP14, #2650). Sources: docs/development/platform-transparency-evidence/.
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "dsa-transparency-db": {
        "publisher": "European Commission, DSA Transparency Database (statements of reasons under Art. 17 and 24(5) "
                     "DSA)",
        "endpoints": ["https://transparency.dsa.ec.europa.eu/explore-data/download/sor-{platform}-{date}-light.zip",
                      "https://transparency.dsa.ec.europa.eu/explore-data/download/sor-{platform}-{date}-light.zip.sha1"],
        "formats": ["dsa-sor-daily-dump-csv"],
        "authentication": "none for the daily dumps; the database API is a submission API for platforms "
                          "(authenticated) and a research search API is announced as future work in the project "
                          "README, so reads use the dumps",
        "rate_limits": "none documented for the dump downloads (verify); one dump and one checksum per unit",
        "revisions": "a statement is immutable once submitted and keyed by uuid; a daily dump is identified by "
                     "platform slug, date and version (full|light) and its published SHA-1; a republished dump with "
                     "another checksum is a new revision of the dump-release record, a changed row a new revision "
                     "of the statement",
        "redirects": "the download route answers with a redirect to the archive's storage URL on another host "
                     "(controller redirectToArchiveUrl); the runtime refuses cross-host redirects, so the storage "
                     "host must be verified and declared before a live run (verify)",
        "licence": "the Commission's reuse policy for its documents (Decision 2011/833/EU) is expected to apply; "
                   "the terms page could not be read for this audit (verify)",
        "attribution": "Source: European Commission, DSA Transparency Database.",
        "access_decision": "unverified-live",
        "reason": "field names, dump routes and version names read from the published source code of the "
                  "database (github.com/digital-services-act/transparency-database); the Commission's pages were "
                  "not reachable from this runtime",
    },
    "meta-ad-library": {
        "publisher": "Meta Platforms, Ad Library API (ads_archive)",
        "endpoints": ["https://graph.facebook.com/{api_version}/ads_archive"],
        "formats": ["meta-ads-archive-json"],
        "authentication": "a user access token of a developer who completed Meta's identity and location "
                          "confirmation (required-secret NOESIS_META_AD_LIBRARY_TOKEN), sent as the Authorization "
                          "bearer header, never in a URL, receipt or record (verify that the header is accepted); "
                          "ad_snapshot_url embeds the token and is never stored",
        "rate_limits": "Graph API application rate limits (verify); at most 5 pages of 100 ads per unit",
        "revisions": "no revision stamp is published; each acquisition of a declared unit is compared with the "
                     "stored revision: a change is a new revision, an ad no longer returned for the same unit is a "
                     "'not-returned' revision (never a deletion)",
        "licence": "Meta Platform Terms and the Ad Library API terms (not readable from this runtime; verify "
                   "redistribution limits before any export beyond citations)",
        "attribution": "Source: Meta Ad Library.",
        "access_decision": "unverified-live",
        "reason": "request and field names follow Meta's own script repository "
                  "(github.com/facebookresearch/Ad-Library-API-Script-Repository); the developer documentation and "
                  "terms pages were not reachable from this runtime",
    },
    "google-political-ads": {
        "publisher": "Google, political advertising transparency data (BigQuery public dataset "
                     "bigquery-public-data.google_political_ads)",
        "endpoints": ["https://bigquery.googleapis.com/bigquery/v2/projects/{billing_project}/queries",
                      ("https://bigquery.googleapis.com/bigquery/v2/projects/bigquery-public-data/datasets/"
                       "google_political_ads/tables/{table}")],
        "formats": ["google-political-ads-bigquery-json"],
        "authentication": "an OAuth access token for the operator's own Google Cloud billing project "
                          "(required-secret NOESIS_GOOGLE_BIGQUERY_TOKEN) as the Authorization bearer header; query "
                          "costs fall on that project (verify the free tier)",
        "rate_limits": "BigQuery API quotas (verify); two table-metadata reads and two queries per unit",
        "revisions": "the tables are 'lifetime' snapshots refreshed several times a day (secondary source, verify); "
                     "the table lastModifiedTime is stored per record as the data refresh date; a changed row is a "
                     "new revision; an ad no longer returned for the same unit is a 'not-returned' revision",
        "licence": "Google Cloud Public Datasets terms and the Transparency Report terms (not readable from this "
                   "runtime; verify)",
        "attribution": "Source: Google Political Ads Transparency Report.",
        "access_decision": "unverified-live",
        "reason": "the legacy bundle google-political-ads-transparency-bundle.zip on storage.googleapis.com is an "
                  "empty placeholder (last modified 2022-06-02), so the BigQuery tables are the documented route; "
                  "column names (spend_range_min_<currency>, spend_range_max_<currency>, impressions, "
                  "public_ids_list) are taken from a secondary source and must be verified",
    },
    "lumen": {
        "publisher": "Berkman Klein Center, Lumen database (takedown notices)",
        "endpoints": ["https://lumendatabase.org/notices/{id}.json", "https://lumendatabase.org/notices/search.json"],
        "formats": [],
        "authentication": "an individually issued research token (X-Authentication-Token), requested from the Lumen "
                          "team; searches are disabled without it",
        "rate_limits": "about one request per second with a token (Lumen API documentation)",
        "revisions": "notices carry dates sent and received; redactions are applied by Lumen and must be preserved "
                     "as published",
        "licence": "Lumen API Terms of Use: tokens are for research use only (lumendatabase.org/pages/api_terms, "
                   "not readable from this runtime)",
        "attribution": "Source: Lumen database.",
        "access_decision": "not-implemented",
        "reason": "no research token has been granted to this deployment and the API Terms of Use could not be "
                  "read, so it cannot be established that storing and citing notice fields is permitted; notices "
                  "name senders and recipients who may be individuals. The takedown-notice record kind and its "
                  "minimisation rule exist for a future grant",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "note": ("not implemented: " + contract["reason"]) if contract["access_decision"] == "not-implemented"
        else "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (SP01): nothing implies complete coverage of a platform, an advertiser or an election.
BOUNDED_COVERAGE = {
    "dsa": f"the light daily dumps of the platforms and days named in each selection (at most {MAX_UNITS} "
           f"platform-days per source and {DSA_ROW_CAP} statements per dump; a larger dump is budget_exhausted)",
    "meta": f"POLITICAL_AND_ISSUE_ADS of at most {META_MAX_PAGE_IDS} declared page ids per unit, for declared "
            f"countries and a delivery window of at most {META_MAX_WINDOW_DAYS} days; at most {META_MAX_PAGES} "
            f"pages of {META_PAGE_SIZE} ads per unit",
    "google": f"the declared advertiser ids per region; at most {GOOGLE_ROW_CAP} creatives per advertiser",
    "lumen": "not implemented",
    "elections": "elections reached only through accepted identity matches with the Political elections feature",
}
# SP01 data-minimisation decision (docs/development/platform-transparency-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "stored": {
        "statement-of-reasons": ["uuid", "platform name",
                                 "decision type fields (visibility, monetary, provision, account) and end dates",
                                 "account type as published", "decision ground and its reference URL",
                                 "legal or contractual ground", "category, additional categories and specification",
                                 "content type, language and date", "product EAN", "application date",
                                 "automated detection and automated decision as published", "created_at"],
        "ad": ["ad id", "page id and page name (advertiser as declared)", "bylines (funding entity as declared)",
               "delivery start and stop", "spend and impression ranges with currency as published",
               "publisher platforms", "languages",
               "Google advertiser id and name, ad type, regions, date range, impression bucket and spend range"],
    },
    "never_stored": ["platform_uid / puid (a platform's identifier of the user's content or account)",
                     "source_identity (the notifier)", "decision_facts and free-text explanations",
                     "free-text *_other fields", "territorial scope rows of the full dump",
                     "ad creative text and ad_snapshot_url (it embeds the access token)",
                     "demographic, regional or audience-size delivery breakdowns",
                     "age, gender and geographic targeting criteria", "any user, viewer or account identifier",
                     "takedown-notice bodies and infringing URLs"],
    "ranges": "spend and impression ranges are stored as the published bounds (and the published bucket text); "
              "they are never converted to a midpoint, a sum of midpoints or any other point estimate",
    "persons": "advertisers and funding entities are stored as the platform published them (legally required "
               "disclosures); they are matched only to organisational records (committees, regulated entities, "
               "party lists, lobbying registrants and clients, legal entities), never to a natural person's record",
    "retention": "retained with their source revision; no user identifier is stored, so nothing user-level "
                 "remains to purge; no automatic expiry in the first coverage",
    "query_scope": "knowledge:osint:platform-transparency:read for records and answers; identity review needs "
                   "knowledge:ownership:review; there is no scope that returns a withheld field",
    "takedown_notices": "if Lumen access is ever granted: notice id, dates, topics, jurisdictions, action taken and "
                        "the recipient as published; a sender only when Lumen publishes it as an organisation; "
                        "redactions kept verbatim; never the notice body or infringing URLs",
}
# Keys that must never appear anywhere in a record's fields (SP01).
WITHHELD_KEYS = frozenset({
    "platform_uid", "puid", "source_identity", "decision_facts", "illegal_content_explanation",
    "incompatible_content_explanation", "decision_visibility_other", "decision_monetary_other",
    "category_specification_other", "content_type_other", "territorial_scope", "ad_snapshot_url", "access_token",
    "ad_creative_body", "ad_creative_bodies", "ad_creative_link_caption", "ad_creative_link_captions",
    "ad_creative_link_description", "ad_creative_link_descriptions", "ad_creative_link_title",
    "ad_creative_link_titles", "demographic_distribution", "delivery_by_region", "region_distribution",
    "estimated_audience_size", "potential_reach", "age_targeting", "gender_targeting", "geo_targeting_included",
    "geo_targeting_excluded", "user_id", "account_id", "viewer", "body", "infringing_urls",
})
# Keys that would carry a point estimate, a profile or a coordination reading; no record or answer may contain them.
FORBIDDEN_KEYS = frozenset({
    "midpoint", "spend_midpoint", "impressions_midpoint", "point_estimate", "estimated_spend",
    "estimated_impressions", "spend_estimate", "impressions_estimate", "coordination", "coordinated",
    "coordination_score", "cohort", "user_profile", "profile", "risk_score", "score", "influence",
})
DSA_LIGHT_COLUMNS = (
    "uuid", "decision_visibility", "decision_visibility_other", "end_date_visibility_restriction",
    "decision_monetary", "decision_monetary_other", "end_date_monetary_restriction", "decision_provision",
    "end_date_service_restriction", "decision_account", "end_date_account_restriction", "account_type",
    "decision_ground", "decision_ground_reference_url", "illegal_content_legal_ground", "incompatible_content_ground",
    "incompatible_content_illegal", "category", "category_addition", "category_specification",
    "category_specification_other", "content_type", "content_type_other", "content_language", "content_date",
    "content_id_ean", "application_date", "source_type", "source_identity", "automated_detection",
    "automated_decision", "platform_name", "platform_uid", "created_at",
)
DSA_REQUIRED = ("uuid", "decision_ground", "category", "content_type", "automated_detection", "automated_decision",
                "platform_name", "created_at")
DSA_DECISION_TYPES = ("decision_visibility", "decision_monetary", "decision_provision", "decision_account")
DSA_WITHHELD_COLUMNS = ("source_identity", "platform_uid", "decision_visibility_other", "decision_monetary_other",
                        "category_specification_other", "content_type_other", "illegal_content_explanation",
                        "incompatible_content_explanation", "decision_facts", "territorial_scope")
META_FIELDS = ("id", "page_id", "page_name", "bylines", "ad_creation_time", "ad_delivery_start_time",
               "ad_delivery_stop_time", "currency", "spend", "impressions", "publisher_platforms", "languages")
GOOGLE_CREATIVE_COLUMNS = ("ad_id", "ad_url", "ad_type", "regions", "advertiser_id", "advertiser_name",
                           "date_range_start", "date_range_end", "num_of_days", "impressions",
                           "first_served_timestamp", "last_served_timestamp")
GOOGLE_ADVERTISER_COLUMNS = ("advertiser_id", "advertiser_name", "public_ids_list", "regions", "elections",
                             "total_creatives")
GOOGLE_DATASET = "bigquery-public-data.google_political_ads"


class PlatformTransparencyFormatError(ValueError):
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
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _published_list(value: Any) -> list[str]:
    """A multi-valued column as published: a JSON array, else a comma-separated list, else one value."""
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def _https(value: Any) -> str | None:
    text = _clean(value)
    return text if text and text.startswith("https://") else None


# ------------------------------------------------------------------ keys


def platform_key(platform: str) -> str:
    return f"platform-transparency:platform:{slug(platform)}"


def statement_key(platform: str, uuid: str) -> str:
    return f"platform-transparency:dsa:sor:{slug(platform)}:{str(uuid).strip().lower()}"


def dump_key(platform: str, day: str, version: str) -> str:
    return f"platform-transparency:dsa:dump:{slug(platform)}:{day}:{version}"


def meta_ad_key(ad_id: Any) -> str:
    return f"platform-transparency:meta:ad:{str(ad_id).strip()}"


def meta_advertiser_key(page_id: Any) -> str:
    return f"platform-transparency:meta:advertiser:{str(page_id).strip()}"


def meta_funder_key(bylines: Any, country: Any) -> str:
    return f"platform-transparency:meta:funder:{slug(bylines)}:{str(country or 'xx').strip().lower()}"


def google_ad_key(ad_id: Any) -> str:
    return f"platform-transparency:google:ad:{str(ad_id).strip()}"


def google_advertiser_key(advertiser_id: Any) -> str:
    return f"platform-transparency:google:advertiser:{str(advertiser_id).strip()}"


def listing_key(unit_key: str) -> str:
    return f"platform-transparency:listing:{unit_key}"


def _record(fmt: str | None, provider: str, kind: str, record_key: str, *, platform: str, title: Any, locator: str,
            fields: Mapping[str, Any], native_revision: Any = None, revision_order: str = "",
            effective_on: Any = None, source_as_of: Any = None, unit_key: str | None = None,
            advertiser_key: str | None = None, dump: str | None = None, withheld: Sequence[str] = ()
            ) -> dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise PlatformTransparencyFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://") or "access_token" in str(locator):
        raise PlatformTransparencyFormatError("schema_drift", f"{record_key} has no clean HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": provider,
        "platform": slug(platform),
        "record_kind": kind,
        "record_key": record_key,
        "unit_key": unit_key,
        "advertiser_key": advertiser_key,
        "dump_key": dump,
        "native_revision": _clean(native_revision),
        "revision_order": revision_order,
        "effective_on": _day(effective_on),
        "source_as_of": _clean(source_as_of),
        "title": _clean(title) or record_key,
        "locator": locator,
        "minimisation": {"policy": MINIMISATION_POLICY, "withheld": sorted(set(withheld))},
        "fields": dict(fields),
    }


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of withheld or forbidden keys a record still carries (empty when it honours SP01)."""
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                bare = str(key).casefold()
                if bare in FORBIDDEN_KEYS or (bare in WITHHELD_KEYS and item not in (None, "", [], {})):
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(record.get("fields") or {}, "$.fields")
    if record.get("record_kind") == "takedown-notice":
        fields = record.get("fields") or {}
        for side in ("sender", "recipient"):
            party = fields.get(side) or {}
            if party.get("kind") not in {"organisation", "withheld"}:
                found.append(f"$.fields.{side}.kind")
            if party.get("kind") == "withheld" and party.get("name") is not None:
                found.append(f"$.fields.{side}.name")
    return sorted(set(found))


# ------------------------------------------------------------------ DSA daily dumps


def _dump_rows(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Rows of every CSV part in a dump zip (parts may themselves be zipped); returns rows and member names."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PlatformTransparencyFormatError("schema_drift", "dump is not a zip archive") from exc
    rows: list[dict[str, str]] = []
    members: list[str] = []

    def read_csv(name: str, data: bytes) -> None:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PlatformTransparencyFormatError("schema_drift", f"{name} is not UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        missing = [c for c in DSA_REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise PlatformTransparencyFormatError("schema_drift", f"{name} lacks columns {missing}")
        for row in reader:
            rows.append(dict(row))
            if len(rows) > DSA_ROW_CAP:
                raise PlatformTransparencyFormatError("input_limit", "dump exceeds the statement cap; the unit is "
                                                                     "not truncated")

    for info in sorted(archive.infolist(), key=lambda i: i.filename):
        if info.is_dir():
            continue
        data = archive.read(info)
        members.append(info.filename)
        if info.filename.endswith(".zip"):
            inner = zipfile.ZipFile(io.BytesIO(data))
            for part in sorted(inner.namelist()):
                if part.endswith(".csv"):
                    members.append(f"{info.filename}/{part}")
                    read_csv(part, inner.read(part))
        elif info.filename.endswith(".csv"):
            read_csv(info.filename, data)
    if not members:
        raise PlatformTransparencyFormatError("schema_drift", "dump holds no CSV part")
    return rows, members


def parse_sha1(raw: bytes) -> str:
    match = re.search(r"\b([0-9a-fA-F]{40})\b", raw.decode("utf-8", errors="replace"))
    if not match:
        raise PlatformTransparencyFormatError("schema_drift", "checksum file carries no SHA-1")
    return match[1].lower()


def parse_dsa_dump(raw: bytes, checksum: bytes, unit: Mapping[str, Any], endpoint: str) -> list[dict[str, Any]]:
    fmt = "dsa-sor-daily-dump-csv"
    platform, day, version = slug(unit["platform"]), str(unit["date"]), "light"
    published_sha1 = parse_sha1(checksum)
    actual_sha1 = hashlib.sha1(raw).hexdigest()
    if actual_sha1 != published_sha1:
        raise PlatformTransparencyFormatError("checksum_mismatch", "dump does not match its published SHA-1")
    rows, members = _dump_rows(raw)
    base = endpoint.rstrip("/")
    dkey = dump_key(platform, day, version)
    out = []
    seen = set()
    for row in rows:
        uuid = _clean(row.get("uuid"))
        if not uuid or uuid in seen:
            raise PlatformTransparencyFormatError("schema_drift", "every statement has one unique uuid")
        seen.add(uuid)
        if slug(row.get("platform_name")) != platform:  # a per-platform dump names its platform on every row
            raise PlatformTransparencyFormatError("schema_drift", f"{uuid} names another platform")
        withheld = [c for c in DSA_WITHHELD_COLUMNS if _clean(row.get(c))]
        fields: dict[str, Any] = {"uuid": uuid.lower(), "platform_name": _clean(row.get("platform_name"))}
        for column in DSA_DECISION_TYPES:
            fields[column] = _published_list(row.get(column))
        for column in ("end_date_visibility_restriction", "end_date_monetary_restriction",
                       "end_date_service_restriction", "end_date_account_restriction", "account_type",
                       "decision_ground", "decision_ground_reference_url", "illegal_content_legal_ground",
                       "incompatible_content_ground", "incompatible_content_illegal", "category",
                       "category_specification", "content_language", "content_date", "content_id_ean",
                       "application_date", "source_type", "automated_detection", "automated_decision", "created_at"):
            fields[column] = _clean(row.get(column))
        fields["category_addition"] = _published_list(row.get("category_addition"))
        fields["content_type"] = _published_list(row.get("content_type"))
        fields["dump"] = {"platform": platform, "date": day, "version": version}
        out.append(_record(fmt, "dsa-transparency-db", "statement-of-reasons", statement_key(platform, uuid),
                           platform=platform, title=f"Statement of reasons {uuid.lower()} ({fields['platform_name']})",
                           locator=f"{base}/statement/{uuid.lower()}", fields=fields,
                           native_revision=f"dump:{platform}:{day}:{version}", revision_order=day,
                           effective_on=fields["application_date"] or fields["created_at"], source_as_of=day,
                           dump=dkey, withheld=withheld))
    release = _record(fmt, "dsa-transparency-db", "dump-release", dkey, platform=platform,
                      title=f"DSA Transparency Database daily dump sor-{platform}-{day}-{version}",
                      locator=f"{base}/explore-data/download/sor-{platform}-{day}-{version}.zip",
                      fields={"platform": platform, "date": day, "version": version, "sha1_as_published": published_sha1,
                              "sha1_verified": True, "members": members, "statements": len(out),
                              "statement_keys": sorted(r["record_key"] for r in out)},
                      native_revision=f"sha1:{published_sha1}", revision_order=day, effective_on=day,
                      source_as_of=day, dump=dkey)
    return [release, *out]


# ------------------------------------------------------------------ Meta Ad Library


def _meta_range(value: Any, what: str, ad_id: str) -> dict[str, Any] | None:
    """A published InsightsRangeValue kept exactly: the bounds as published (an absent upper bound stays absent)."""
    if value in (None, {}):
        return None
    if not isinstance(value, Mapping) or "lower_bound" not in value:
        raise PlatformTransparencyFormatError("schema_drift", f"ad {ad_id}: {what} is not a published range")
    return {"lower_bound": str(value["lower_bound"]),
            "upper_bound": str(value["upper_bound"]) if value.get("upper_bound") is not None else None,
            "as_published": {k: value[k] for k in sorted(value)}}


def meta_unit_key(unit: Mapping[str, Any]) -> str:
    return "meta:" + ",".join(sorted(str(p) for p in unit["page_ids"])) + ":" + ",".join(
        sorted(str(c).upper() for c in unit["countries"])) + f":{unit['from']}:{unit['to']}"


def parse_meta_ads(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "meta-ads-archive-json"
    unit_key = meta_unit_key(unit)
    declared = {str(p) for p in unit["page_ids"]}
    countries = sorted(str(c).upper() for c in unit["countries"])
    ads, advertisers = [], {}
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("data"), list):
            raise PlatformTransparencyFormatError("schema_drift", "ads_archive response has no data list")
        for row in page["data"]:
            ad_id = _clean(row.get("id"))
            page_id = _clean(row.get("page_id"))
            if not ad_id or not page_id:
                raise PlatformTransparencyFormatError("schema_drift", "an ad has no id or page_id")
            if page_id not in declared:
                raise PlatformTransparencyFormatError("schema_drift", f"ad {ad_id} names an undeclared page")
            withheld = sorted(k for k in row if k not in META_FIELDS)
            bylines = _clean(row.get("bylines"))
            fields = {
                "ad_id": ad_id, "page_id": page_id,
                "advertiser_as_declared": _clean(row.get("page_name")),
                "funding_entity_as_declared": bylines,
                "ad_creation_time": _clean(row.get("ad_creation_time")),
                "ad_delivery_start_time": _clean(row.get("ad_delivery_start_time")),
                "ad_delivery_stop_time": _clean(row.get("ad_delivery_stop_time")),
                "currency": _clean(row.get("currency")),
                "spend": _meta_range(row.get("spend"), "spend", ad_id),
                "impressions": _meta_range(row.get("impressions"), "impressions", ad_id),
                "publisher_platforms": sorted(str(p) for p in row.get("publisher_platforms") or []),
                "languages": sorted(str(p) for p in row.get("languages") or []),
                "ad_reached_countries_requested": countries, "listing_state": "listed",
            }
            ads.append(_record(fmt, "meta-ad-library", "ad", meta_ad_key(ad_id), platform="meta",
                               title=f"Meta ad {ad_id} by {fields['advertiser_as_declared'] or page_id}",
                               locator=f"https://www.facebook.com/ads/library/?id={ad_id}", fields=fields,
                               effective_on=fields["ad_delivery_start_time"], unit_key=unit_key,
                               advertiser_key=meta_advertiser_key(page_id), withheld=withheld))
            entry = advertisers.setdefault(page_id, {"names": set(), "bylines": set()})
            if fields["advertiser_as_declared"]:
                entry["names"].add(fields["advertiser_as_declared"])
            if bylines:
                entry["bylines"].add(bylines)
    out = []
    for page_id, entry in sorted(advertisers.items()):
        out.append(_record(fmt, "meta-ad-library", "advertiser", meta_advertiser_key(page_id), platform="meta",
                           title=f"Meta advertiser page {page_id}",
                           locator=f"https://www.facebook.com/ads/library/?view_all_page_id={page_id}",
                           fields={"page_id": page_id, "names_as_declared": sorted(entry["names"]),
                                   "funding_entities_as_declared": sorted(entry["bylines"]), "countries": countries},
                           unit_key=unit_key, advertiser_key=meta_advertiser_key(page_id)))
    out += sorted(ads, key=lambda r: r["record_key"])
    out.append(_listing(fmt, "meta-ad-library", "meta", unit_key, ads, unit))
    return out


def _listing(fmt: str, provider: str, platform: str, unit_key: str, ads: Sequence[Mapping[str, Any]],
             unit: Mapping[str, Any], source_as_of: Any = None, revision_order: str = "") -> dict[str, Any]:
    return _record(fmt, provider, "listing", listing_key(unit_key), platform=platform,
                   title=f"Ads returned for {unit_key}",
                   locator=("https://www.facebook.com/ads/library/" if provider == "meta-ad-library"
                            else "https://adstransparency.google.com/political"),
                   fields={"unit": {k: unit[k] for k in sorted(unit) if k != "label"},
                           "ad_keys": sorted(a["record_key"] for a in ads), "complete": True},
                   unit_key=unit_key, source_as_of=source_as_of, revision_order=revision_order)


# ------------------------------------------------------------------ Google political ads (BigQuery)


def google_unit_key(unit: Mapping[str, Any]) -> str:
    return f"google:{unit['id']}:{str(unit['region']).upper()}"


def _bq_rows(payload: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or payload.get("jobComplete") is not True:
        raise PlatformTransparencyFormatError("schema_drift", f"{what} query did not complete")
    if payload.get("pageToken"):
        raise PlatformTransparencyFormatError("input_limit", f"{what} has more rows than one bounded page")
    names = [f.get("name") for f in (payload.get("schema") or {}).get("fields") or []]
    if not names:
        raise PlatformTransparencyFormatError("schema_drift", f"{what} response has no schema")
    rows = []
    for row in payload.get("rows") or []:
        cells = [c.get("v") if isinstance(c, Mapping) else None for c in row.get("f") or []]
        if len(cells) != len(names):
            raise PlatformTransparencyFormatError("schema_drift", f"{what} row does not match its schema")
        rows.append(dict(zip(names, cells)))
    if int(payload.get("totalRows") or len(rows)) != len(rows):
        raise PlatformTransparencyFormatError("input_limit", f"{what} states more rows than it returned")
    return rows


def _refreshed(table: Any) -> tuple[str, str]:
    try:
        ms = int(str((table or {}).get("lastModifiedTime")))
    except (TypeError, ValueError) as exc:
        raise PlatformTransparencyFormatError("schema_drift", "table metadata has no lastModifiedTime") from exc
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z"), f"{ms:015d}"


def parse_google(advertiser_table: Any, creative_table: Any, advertiser_result: Any, creative_result: Any,
                 unit: Mapping[str, Any], currency: str) -> list[dict[str, Any]]:
    fmt = "google-political-ads-bigquery-json"
    unit_key = google_unit_key(unit)
    advertiser_id = str(unit["id"])
    adv_refreshed, adv_order = _refreshed(advertiser_table)
    refreshed, order = _refreshed(creative_table)
    advertisers = _bq_rows(advertiser_result, "advertiser_stats")
    if len(advertisers) > 1:
        raise PlatformTransparencyFormatError("schema_drift", "advertiser_stats returned more than one row")
    out = []
    if advertisers:
        row = advertisers[0]
        if str(row.get("advertiser_id")) != advertiser_id:
            raise PlatformTransparencyFormatError("schema_drift", "advertiser_stats names another advertiser")
        spend_column = f"spend_{currency}"
        fields = {"advertiser_id": advertiser_id, "advertiser_name": _clean(row.get("advertiser_name")),
                  "public_ids_as_published": _clean(row.get("public_ids_list")),
                  "public_ids": _published_list(row.get("public_ids_list")),
                  "regions": _published_list(row.get("regions")),
                  "elections_as_published": _clean(row.get("elections")),
                  "total_creatives_as_published": _clean(row.get("total_creatives")),
                  "spend_as_published": {"currency": currency.upper(), "value": _clean(row.get(spend_column))}}
        out.append(_record(fmt, "google-political-ads", "advertiser", google_advertiser_key(advertiser_id),
                           platform="google", title=f"Google political advertiser {fields['advertiser_name']}",
                           locator=f"https://adstransparency.google.com/advertiser/{advertiser_id}?region="
                                   f"{str(unit['region']).upper()}",
                           fields=fields, native_revision=f"refreshed:{adv_refreshed}", revision_order=adv_order,
                           source_as_of=adv_refreshed, unit_key=unit_key,
                           advertiser_key=google_advertiser_key(advertiser_id)))
    ads = []
    for row in _bq_rows(creative_result, "creative_stats"):
        ad_id = _clean(row.get("ad_id"))
        if not ad_id or str(row.get("advertiser_id")) != advertiser_id:
            raise PlatformTransparencyFormatError("schema_drift", "a creative has no id or names another advertiser")
        low, high = row.get(f"spend_range_min_{currency}"), row.get(f"spend_range_max_{currency}")
        withheld = sorted(k for k in row if k not in GOOGLE_CREATIVE_COLUMNS and not k.startswith("spend_range_"))
        fields = {
            "ad_id": ad_id, "advertiser_id": advertiser_id,
            "advertiser_as_declared": _clean(row.get("advertiser_name")),
            "funding_entity_as_declared": None,
            "funding_entity_note": "Google publishes the verified advertiser; it states no separate funding entity",
            "ad_type": _clean(row.get("ad_type")), "regions": _published_list(row.get("regions")),
            "date_range_start": _day(row.get("date_range_start")), "date_range_end": _day(row.get("date_range_end")),
            "num_of_days": _clean(row.get("num_of_days")),
            "first_served_timestamp": _clean(row.get("first_served_timestamp")),
            "last_served_timestamp": _clean(row.get("last_served_timestamp")),
            "impressions": {"as_published": _clean(row.get("impressions"))},
            "spend": {"lower_bound": _clean(low), "upper_bound": _clean(high), "currency": currency.upper(),
                      "as_published": {f"spend_range_min_{currency}": low, f"spend_range_max_{currency}": high}},
            "listing_state": "listed",
        }
        locator = _https(row.get("ad_url")) or (f"https://adstransparency.google.com/advertiser/{advertiser_id}/"
                                                f"creative/{ad_id}")
        ads.append(_record(fmt, "google-political-ads", "ad", google_ad_key(ad_id), platform="google",
                           title=f"Google political ad {ad_id} by {fields['advertiser_as_declared']}",
                           locator=locator, fields=fields, native_revision=f"refreshed:{refreshed}",
                           revision_order=order, effective_on=fields["date_range_start"], source_as_of=refreshed,
                           unit_key=unit_key, advertiser_key=google_advertiser_key(advertiser_id),
                           withheld=withheld))
        if len(ads) > GOOGLE_ROW_CAP:
            raise PlatformTransparencyFormatError("input_limit", "advertiser has more creatives than its bound")
    out += sorted(ads, key=lambda r: r["record_key"])
    out.append(_listing(fmt, "google-political-ads", "google", unit_key, ads, unit, source_as_of=refreshed,
                        revision_order=order))
    return out


def google_queries(unit: Mapping[str, Any], currency: str) -> dict[str, dict[str, Any]]:
    """The two fixed, parameterised Standard SQL queries of one unit (advertiser and its creatives in a region)."""
    if not re.fullmatch(r"[a-z]{3}", currency):
        raise SourcePackError("invalid_manifest", "a Google currency is a three-letter code")
    params = [{"name": "advertiser_id", "parameterType": {"type": "STRING"},
               "parameterValue": {"value": str(unit["id"])}},
              {"name": "region", "parameterType": {"type": "STRING"},
               "parameterValue": {"value": str(unit["region"]).upper()}}]
    creative_columns = ", ".join(GOOGLE_CREATIVE_COLUMNS + (f"spend_range_min_{currency}",
                                                            f"spend_range_max_{currency}"))
    advertiser_columns = ", ".join(GOOGLE_ADVERTISER_COLUMNS + (f"spend_{currency}",))

    def body(sql: str) -> dict[str, Any]:
        return {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
                "maxResults": GOOGLE_ROW_CAP, "timeoutMs": 20000}

    return {
        "advertiser": body(f"SELECT {advertiser_columns} FROM `{GOOGLE_DATASET}.advertiser_stats` "
                           "WHERE advertiser_id = @advertiser_id AND STRPOS(regions, @region) > 0"),
        "creatives": body(f"SELECT {creative_columns} FROM `{GOOGLE_DATASET}.creative_stats` "
                          "WHERE advertiser_id = @advertiser_id AND STRPOS(regions, @region) > 0 ORDER BY ad_id"),
    }


# ------------------------------------------------------------------ units and requests

_META_PAGE = re.compile(r"^\d{1,20}$")
_GOOGLE_ADVERTISER = re.compile(r"^AR\d{10,30}$")


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) for u in selection.get(key) or [] if isinstance(u, Mapping)]
    if not 1 <= len(units) <= MAX_UNITS or len(units) != len(selection.get(key) or []):
        raise SourcePackError("invalid_manifest", f"a platform-transparency selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if key == "dumps":
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(unit.get("platform") or "")) or unit["platform"] == \
                    "global":
                raise SourcePackError("invalid_manifest", "a dump names one platform slug (the global dump is "
                                                          "not a bounded selection)")
            if not _day(unit.get("date")):
                raise SourcePackError("invalid_manifest", "a dump names its day")
        if key == "pages":
            ids = [str(p) for p in unit.get("page_ids") or []]
            if not 1 <= len(ids) <= META_MAX_PAGE_IDS or not all(_META_PAGE.fullmatch(p) for p in ids):
                raise SourcePackError("invalid_manifest", f"a Meta unit names 1-{META_MAX_PAGE_IDS} numeric page ids")
            countries = [str(c) for c in unit.get("countries") or []]
            if not countries or not all(re.fullmatch(r"[A-Z]{2}", c) for c in countries):
                raise SourcePackError("invalid_manifest", "a Meta unit names ISO country codes")
            start, end = _day(unit.get("from")), _day(unit.get("to"))
            if not start or not end or end < start or (date.fromisoformat(end) - date.fromisoformat(start)).days > \
                    META_MAX_WINDOW_DAYS:
                raise SourcePackError("invalid_manifest", f"a Meta delivery window is at most {META_MAX_WINDOW_DAYS} "
                                                          "days")
        if key == "advertisers":
            if not _GOOGLE_ADVERTISER.fullmatch(str(unit.get("id") or "")):
                raise SourcePackError("invalid_manifest", f"not a Google advertiser id: {unit.get('id')!r}")
            if not re.fullmatch(r"[A-Z]{2}", str(unit.get("region") or "")):
                raise SourcePackError("invalid_manifest", "a Google unit names one region code")
    return units


def platform_transparency_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("platform_transparency") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "platform-transparency sources declare a matching provider and "
                                                  "format")
    if PROVIDER_CONTRACTS[declared["provider"]]["access_decision"] == "not-implemented":
        raise SourcePackError("invalid_manifest", "this provider is recorded as not implemented")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "platform-transparency sources state their LIVE_VERIFICATION "
                                                  "status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "platform-transparency sources declare the SP01 minimisation "
                                                  "policy")
    selection = dict(declared.get("selection") or {})
    _units(fmt, selection)
    if fmt == "meta-ads-archive-json" and not re.fullmatch(r"v\d+\.\d+", str(selection.get("api_version") or "")):
        raise SourcePackError("invalid_manifest", "a Meta selection declares its Graph API version")
    if fmt == "google-political-ads-bigquery-json":
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", str(selection.get("billing_project") or "")):
            raise SourcePackError("invalid_manifest", "a Google selection declares the operator's billing project")
        google_queries(_units(fmt, selection)[0], str(selection.get("currency") or ""))
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed platform-transparency formats declare a required secret")
    return declared


def _default_transport(max_bytes: int) -> Callable[..., Mapping[str, Any]]:
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    def transport(*, url, params, headers, timeout, method="GET", body=None):
        if method == "GET":
            return HTTPSPageAdapter._request(url=url, params=params, headers=headers, timeout=timeout,
                                             max_bytes=max_bytes)
        import urllib.error
        import urllib.request

        from src.ingestion.source_packs import _validate_endpoint

        _validate_endpoint(url, "platform-transparency-query")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {"status": response.status, "headers": dict(response.headers),
                        "content": response.read(max_bytes + 1), "final_url": response.geturl()}
        except urllib.error.HTTPError as exc:
            try:
                return {"status": exc.code, "headers": dict(exc.headers or {}), "content": b""}
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            raise SourcePackError("source_unavailable", "query endpoint is unavailable") from exc

    return transport


class PlatformTransparencyAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        self.source = json.loads(json.dumps(source))
        self.declared = platform_transparency_declaration(self.source)
        self.format = self.declared["format"]
        self.selection = dict(self.declared.get("selection") or {})
        self.units = _units(self.format, self.selection)
        self.secret = secret
        self.transport = transport or _default_transport(int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "platform_transparency": {"provider": self.declared["provider"], "format": self.format,
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
            raise SourcePackError("parameter_forbidden", "platform-transparency runs fetch the declared selection "
                                                         "only")

    def _send(self, path: str, params: Mapping[str, Any], *, method: str = "GET",
              body: Mapping[str, Any] | None = None, host_path: bool = False) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, application/zip, text/plain"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "this platform-transparency source needs its token")
            headers["Authorization"] = f"Bearer {self.secret}"
        payload = None
        if body is not None:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
                                  **({"method": method, "body": payload} if method != "GET" else {}))
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "platform-transparency response was served from another host")
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
        receipt = {"method": method, "path": urlsplit(url).path + ("?" + query if query else ""), "status": status,
                   "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                   "origin": "fixture" if response.get("origin") == "fixture" else "live"}
        if payload is not None:
            receipt["body_sha256"] = hashlib.sha256(payload).hexdigest()
        return raw, receipt

    @staticmethod
    def _json(raw: bytes, what: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", f"{what} response is not valid UTF-8 JSON") from exc

    def _collect(self, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.format == "dsa-sor-daily-dump-csv":
            name = f"sor-{unit['platform']}-{unit['date']}-light.zip"
            raw, first = self._send(f"/explore-data/download/{name}", {})
            checksum, second = self._send(f"/explore-data/download/{name}.sha1", {})
            records = parse_dsa_dump(raw, checksum, unit, self.source["endpoint"])
            return records, [first, second]
        if self.format == "meta-ads-archive-json":
            params = {"ad_type": "POLITICAL_AND_ISSUE_ADS",
                      "ad_reached_countries": json.dumps(sorted(unit["countries"])),
                      "search_page_ids": json.dumps(sorted(str(p) for p in unit["page_ids"])),
                      "ad_delivery_date_min": unit["from"], "ad_delivery_date_max": unit["to"],
                      "fields": ",".join(META_FIELDS), "limit": META_PAGE_SIZE}
            path = f"/{self.selection['api_version']}/ads_archive"
            pages, receipts, request = [], [], dict(params)
            while True:
                raw, receipt = self._send(path, request)
                payload = self._json(raw, "ads_archive")
                pages.append(payload)
                receipts.append(receipt)
                after = ((payload.get("paging") or {}).get("cursors") or {}).get("after") \
                    if isinstance(payload, Mapping) else None
                more = bool((payload.get("paging") or {}).get("next")) if isinstance(payload, Mapping) else False
                if not more or not after:
                    return parse_meta_ads(pages, unit), receipts
                if len(pages) >= META_MAX_PAGES:
                    raise SourcePackError("budget_exhausted", "unit is longer than its page bound; never truncated")
                request = {**params, "after": after}
        currency = str(self.selection["currency"])
        project = self.selection["billing_project"]
        tables = {}
        receipts = []
        for table in ("advertiser_stats", "creative_stats"):
            raw, receipt = self._send(f"/projects/bigquery-public-data/datasets/google_political_ads/tables/{table}",
                                      {})
            tables[table] = self._json(raw, table)
            receipts.append(receipt)
        results = {}
        for name, body in google_queries(unit, currency).items():
            raw, receipt = self._send(f"/projects/{project}/queries", {}, method="POST", body=body)
            results[name] = self._json(raw, name)
            receipts.append(receipt)
        records = parse_google(tables["advertiser_stats"], tables["creative_stats"], results["advertiser"],
                               results["creatives"], unit, currency)
        return records, receipts

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        try:
            records, requests = self._collect(unit)
        except PlatformTransparencyFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        for record in records:
            if minimisation_violations(record):
                raise SourcePackError("schema_drift", f"minimisation_violation: {record['record_key']}")
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        receipt = {
            "contract": RECEIPT_CONTRACT, "source_id": self.source["source_id"],
            "provider": self.declared["provider"], "format": self.format, "unit_index": index,
            "unit": {k: unit[k] for k in sorted(unit)}, "requests": requests, "records": len(records),
            "evidence_origin": origin, "live_verification": self.declared["live_verification"],
            "minimisation": MINIMISATION_POLICY,
            "withheld_fields": sorted({w for r in records for w in r["minimisation"]["withheld"]}),
            "dump_versions": [{"dump_key": r["record_key"], "sha1": r["fields"]["sha1_as_published"],
                               "statements": r["fields"]["statements"]}
                              for r in records if r["record_kind"] == "dump-release"],
            "source_as_of": sorted({r["source_as_of"] for r in records if r.get("source_as_of")}),
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"], "language": "und",
                "published_at": record["effective_on"], "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "platform_transparency_record": record, "platform_transparency_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-token"
ADAPTERS = {CONNECTOR: PlatformTransparencyAdapter}


def request_key(method: str, url_path: str, params: Mapping[str, Any] | None, body: bytes | None = None) -> str:
    query = urlencode(sorted(dict(params or {}).items()))
    key = url_path + ("?" + query if query else "")
    if method != "GET":
        key = f"{method} {key} " + hashlib.sha256(body or b"").hexdigest()[:16]
    return key


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by method, URL path, sorted query and body digest; marked as fixture evidence.

    A page body is JSON (an object), text, or ``body_base64`` for binary payloads such as a dump zip.
    """
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, method="GET", body=None):
        del headers, timeout
        key = request_key(method, urlsplit(url).path, params, body)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            payload = page.get("body")
            content = payload.encode() if isinstance(payload, str) else b"" if payload is None else \
                json.dumps(payload).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = PlatformTransparencyAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
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


def source_contracts() -> dict[str, Any]:
    return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
            "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "review_boundary": REVIEW_BOUNDARY}


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
    "PlatformTransparencyAdapter",
    "PlatformTransparencyFormatError",
    "fixture_transport",
    "google_queries",
    "minimisation_violations",
    "platform_transparency_declaration",
    "replay_native_fixture",
    "request_key",
    "source_contracts",
]

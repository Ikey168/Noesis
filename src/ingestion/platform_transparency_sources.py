"""Platform transparency acquisition for the OSINT pack (#2580, SP01, SP03-SP06).

One native connector, ``platform-transparency``, reads a bounded, declared
selection from one documented provider per source and emits
``noesis-platform-transparency-record-v1`` records exactly as the platform or
the database published them:

* ``dsa-sor-dump-csv`` - the EU DSA Transparency Database daily dumps, one
  declared (platform, day, variant) file per unit: a ``dump-release`` record
  (file name, variant, byte digest - the dump version) and one
  ``statement-of-reasons`` record per row, keyed by platform and statement
  UUID, with decision type, ground, category, content type and the
  automated-detection and automated-decision flags as published;
* ``meta-ad-library-json`` - the Meta Ad Library API (``/ads_archive``) for
  political and issue ads of one declared page, reached countries and
  delivery window per unit: ``ad`` records (advertiser page, funding entity
  as declared in ``bylines``, delivery times, spend and impression ranges and
  currency as published) and one ``advertiser`` record per page;
* ``google-political-ads-bundle`` - Google's political ads transparency bundle
  (advertiser and creative statistics CSVs), filtered to the declared
  advertiser ids: ``advertiser`` records (published public ids such as FEC
  committee ids, elections and totals as published) and ``ad`` records with
  spend ranges and impression buckets exactly as published and the bundle's
  data refresh time stored on every record;
* ``lumen-notices-json`` - Lumen takedown notices for one declared recipient
  and date window, under researcher access only: ``takedown-notice`` records
  with the fields the access terms allow, redactions preserved as published.

**Data minimisation (SP01, ``platform-transparency-minimisation-v1``).** Free
text that may carry personal data (DSA decision facts and explanations, the
platform's content identifier, the notifier's identity; ad creative bodies and
links; audience demographic and regional distributions; Lumen notice bodies,
work descriptions and URLs) is dropped *here*, before any record, document or
receipt exists, and listed under ``minimisation.withheld``; the store refuses
a record that still carries one of them. Access tokens travel in headers only
and are stripped from any published URL (Meta ``ad_snapshot_url``).

A unit is all-or-nothing: more rows or pages than the declared bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest, never a credential. Nothing here profiles users, collects private
content, infers coordinated behaviour or converts a published range into a
point estimate.
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
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-platform-transparency-record-v1"
RECEIPT_CONTRACT = "noesis-platform-transparency-acquisition-receipt-v1"
MINIMISATION_POLICY = "platform-transparency-minimisation-v1"
CONNECTOR = "platform-transparency"
DECISION = "docs/development/platform-transparency-evidence/source-audit.md#decisions"
MAX_UNITS = 20
DSA_ROW_CAP = 5000
META_PER_PAGE = 100
META_MAX_PAGES = 5
LUMEN_PER_PAGE = 50
LUMEN_MAX_PAGES = 4
GOOGLE_ADVERTISER_CAP = 20
GOOGLE_CREATIVE_CAP = 2000
REVIEW_BOUNDARY = ("Records are what the platform or database published. No user-level profiling, no collection of "
                   "private content, no inference of coordinated behaviour and no conversion of spend or impression "
                   "ranges into point estimates.")

# format -> provider, platform-level fields and whether a secret is required
FORMATS: dict[str, dict[str, Any]] = {
    "dsa-sor-dump-csv": {"provider": "dsa-transparency-database", "keyed": False, "feature":
                         "platform-transparency-dsa"},
    "meta-ad-library-json": {"provider": "meta-ad-library", "keyed": True, "feature": "platform-transparency-meta"},
    "google-political-ads-bundle": {"provider": "google-political-ads", "keyed": False,
                                    "feature": "platform-transparency-google"},
    "lumen-notices-json": {"provider": "lumen", "keyed": True, "feature": "platform-transparency-lumen"},
}
PROVIDER_HOSTS = {
    "dsa-transparency-database": ("dsa-sor-data-dumps.s3.eu-central-1.amazonaws.com",),
    "meta-ad-library": ("graph.facebook.com",),
    "google-political-ads": ("storage.googleapis.com",),
    "lumen": ("lumendatabase.org",),
}
RECORD_KINDS = ("dump-release", "statement-of-reasons", "advertiser", "ad", "takedown-notice")

# SP01 access decisions, recorded from the providers' published documentation as known without network access
# (the audit could not reach the sources from this runtime); every item marked ``verify`` must be checked before a
# dated live run (SP14, #2650).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "dsa-transparency-database": {
        "publisher": "European Commission, DSA Transparency Database (Article 24(5) DSA statements of reasons)",
        "endpoints": ["https://transparency.dsa.ec.europa.eu/data-download (index of daily dumps)",
                      ("https://dsa-sor-data-dumps.s3.eu-central-1.amazonaws.com/sor-{platform}-{YYYY-MM-DD}-"
                       "{full|light}.zip (verify the file naming and host)"),
                      ("https://transparency.dsa.ec.europa.eu/api/v1/ (submission API for platforms; not a read "
                       "API for this use)")],
        "formats": ["dsa-sor-dump-csv"],
        "authentication": "none for the dumps",
        "rate_limits": "none documented for the dump host (verify); one declared file per unit",
        "pagination": "one ZIP per (platform, day, variant), CSV members possibly nested one ZIP level deep; a "
                      f"file with more than {DSA_ROW_CAP} rows is budget_exhausted, never truncated",
        "identifiers": ["statement uuid", "platform_uid", "dump file name"],
        "revisions": "statements are immutable once submitted; a dump file republished with different bytes is a "
                     "new dump-release revision, and a statement that changes (or is no longer present) in a "
                     "republished dump is a new revision of that statement, never a deletion",
        "licence": "Commission reuse policy (Decision 2011/833/EU), CC BY 4.0 for Commission data unless stated "
                   "otherwise (verify the Transparency Database terms)",
        "attribution": "Source: European Commission, DSA Transparency Database",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the documented CSV columns of the light dumps; the file naming, the "
                  "nesting of CSV parts and the value vocabularies must be verified",
    },
    "meta-ad-library": {
        "publisher": "Meta Platforms, Ad Library API (Graph API /ads_archive)",
        "endpoints": [("https://graph.facebook.com/{version}/ads_archive?ad_type=POLITICAL_AND_ISSUE_ADS&"
                       "ad_reached_countries=[..]&search_page_ids=[..]&ad_delivery_date_min=..&"
                       "ad_delivery_date_max=..&fields=.. (verify the Graph API version)")],
        "formats": ["meta-ad-library-json"],
        "authentication": "a user access token of an identity-confirmed developer account (required-secret "
                          "NOESIS_META_AD_LIBRARY_TOKEN) sent as an Authorization: Bearer header, never as the "
                          "access_token query parameter, in a record or in a receipt; the token is stripped from the "
                          "published ad_snapshot_url",
        "rate_limits": "Graph API application and user rate limits (X-App-Usage / X-Business-Use-Case-Usage); HTTP "
                       "429 or error code 4/17/613 is rate_limited (verify)",
        "pagination": f"cursor paging (paging.cursors.after), limit {META_PER_PAGE}; at most {META_MAX_PAGES} pages "
                      "per unit, a longer listing is budget_exhausted, never truncated",
        "identifiers": ["ad archive id", "page_id"],
        "revisions": "an ad's published fields change while it runs (stop time, spend and impression ranges); a "
                     "changed ad is a new revision; an ad no longer returned for the same declared selection is a "
                     "not-returned revision (stated as observed absence, never a deletion)",
        "licence": "Meta Platform Terms and Ad Library API terms: research and transparency use; do not sell or "
                   "use for advertising or profiling (verify the current terms)",
        "attribution": "Source: Meta Ad Library",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser in the documented JSON shape; the field list, range encodings and "
                  "the Graph API version must be verified with an identity-confirmed token",
    },
    "google-political-ads": {
        "publisher": "Google, Political advertising transparency report (Ads Transparency Center)",
        "endpoints": [("https://storage.googleapis.com/transparencyreport/google-political-ads-transparency-"
                       "bundle.zip (verify)"),
                      ("BigQuery public dataset bigquery-public-data.google_political_ads (documented, not used: it "
                       "needs a billed Google Cloud project)")],
        "formats": ["google-political-ads-bundle"],
        "authentication": "none",
        "rate_limits": "none documented; one bundle request per run",
        "pagination": "one ZIP bundle; rows are filtered to the declared advertiser ids before anything is kept; "
                      f"more than {GOOGLE_CREATIVE_CAP} creative rows for the declared advertisers is "
                      "budget_exhausted",
        "identifiers": ["Advertiser_ID (AR...)", "Ad_ID (CR...)",
                        "Public_IDs_List (e.g. FEC committee ids) as published"],
        "revisions": "the bundle is regenerated (the updated file states the refresh time, stored on every record); "
                     "a changed row is a new revision; an ad no longer listed for a declared advertiser is a "
                     "not-returned revision",
        "licence": "Google Transparency Report data: reuse with attribution (verify the terms of the report)",
        "attribution": "Source: Google Political Advertising Transparency Report",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the documented CSV columns; the bundle size (large) requires an "
                  "operator byte budget, and the member names and spend-range columns must be verified",
    },
    "lumen": {
        "publisher": "Lumen (Berkman Klein Center), takedown notice database",
        "endpoints": [("https://lumendatabase.org/notices/search.json?recipient_name=..&date_received_facet=.. "
                       "(verify the facet parameter names)")],
        "formats": ["lumen-notices-json"],
        "authentication": "researcher API token (required-secret NOESIS_LUMEN_API_TOKEN) in the "
                          "X-Authentication-Token header; granted by Lumen on application only",
        "rate_limits": "per-token limits set by Lumen (verify); one recipient and window per unit",
        "pagination": f"page/per_page <= {LUMEN_PER_PAGE}; at most {LUMEN_MAX_PAGES} pages per unit, a longer "
                      "listing is budget_exhausted",
        "identifiers": ["notice id"],
        "revisions": "Lumen may redact or update a notice; a changed notice is a new revision with the redactions "
                     "as published",
        "licence": "Lumen researcher terms: research use; no republication of notice contents or URLs beyond the "
                   "public notice page (verify)",
        "attribution": "Source: Lumen database (lumendatabase.org)",
        "access_decision": "gated-not-granted",
        "reason": "researcher access has not been granted to this deployment; the connector is implemented and "
                  "fixture-tested but refuses to run without the operator's own researcher token and reports the "
                  "source as unavailable; live coverage stays not implemented until access is granted",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"],
               "note": "no dated live run from this runtime; offline fixtures only (#2650)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
EXCLUDED_SOURCES = {
    "meta-ad-library-report-csv": "the Ad Library Report aggregates spend per advertiser and region; it is "
                                  "documented, not acquired: the per-ad API answers the bounded questions",
    "tiktok-ad-library": "research API access is application-gated and its terms forbid redistribution; not in the "
                         "first coverage",
    "x-ads-repository": "no political-ad repository with stable machine access at the time of the audit",
}
BOUNDED_COVERAGE = {
    "dsa": "declared (platform, day) light dumps, at most 20 units per source and "
           f"{DSA_ROW_CAP} statements per unit; aggregates only over stored statements with the window stated",
    "meta": "declared advertiser pages with reached countries and a delivery window of at most 366 days, at most "
            f"20 units and {META_MAX_PAGES * META_PER_PAGE} ads per unit",
    "google": f"at most {GOOGLE_ADVERTISER_CAP} declared advertiser ids from one bundle, at most "
              f"{GOOGLE_CREATIVE_CAP} creative rows",
    "lumen": "one declared recipient platform and a date window of at most 92 days per unit, only under researcher "
             "access",
}
# SP01 data-minimisation decision (docs/development/platform-transparency-evidence/source-audit.md#minimisation).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "stored": {
        "statement-of-reasons": ["platform name and uid", "statement uuid",
                                 "decision types (visibility, monetary, provision, account) as published",
                                 "decision ground and its legal or terms reference",
                                 "category and specification", "content type and language",
                                 "account type", "source type", "automated detection and automated decision flags",
                                 "territorial scope", "content, application, end and creation dates"],
        "ad": ["platform", "ad id", "advertiser as declared (page or advertiser name and id)",
               "funding entity as declared (bylines)", "delivery dates",
               "spend and impression ranges and currency exactly as published",
               "languages, publisher platforms, regions and ad-level targeting as published",
               "a token-free public locator"],
        "advertiser": ["platform", "advertiser id and name as published", "published public ids (e.g. FEC)",
                       "elections and totals as published"],
        "takedown-notice": ["notice id", "type", "title as published",
                            "sender, principal and recipient names as published (Lumen's redactions preserved)",
                            "dates", "topics", "jurisdictions",
                            "action taken", "counts of works and URLs"],
    },
    "never_stored": {
        "statement-of-reasons": ["decision_facts", "illegal_content_explanation", "incompatible_content_explanation",
                                 "puid (the platform's content identifier)", "source_identity (the notifier)",
                                 "content URLs"],
        "ad": ["ad creative bodies, link titles, captions and descriptions", "demographic_distribution",
               "delivery_by_region", "access tokens (stripped from ad_snapshot_url)"],
        "takedown-notice": ["notice body", "work descriptions", "infringing and copyrighted URLs",
                            "sender addresses, e-mail or telephone"],
    },
    "individuals": "no field identifies a user of a platform; advertisers and funding entities are stored as the "
                   "platform published their political-ad disclosure; identity matching targets organisation "
                   "records only (committees, lobbying registrants and clients, legal entities) - a natural-person "
                   "record (a candidate) is never a match target, and a name matching only a person stays unmatched",
    "query_scope": "knowledge:osint:platform-transparency:read for statements, ads and advertisers; Lumen notices "
                   "also need knowledge:osint:platform-transparency:notices:read (researcher terms), otherwise they "
                   "are counted, never returned",
    "retention": "revisions are retained with their source run; the stored fields carry no personal identifier, so "
                 "nothing personal remains to purge; no automatic expiry in the first coverage",
}
# Keys that may never appear in a stored record's fields (checked at any depth).
FORBIDDEN_FIELD_KEYS = frozenset({
    "decision_facts", "illegal_content_explanation", "incompatible_content_explanation", "puid", "source_identity",
    "url", "content_url", "ad_creative_bodies", "ad_creative_link_titles", "ad_creative_link_captions",
    "ad_creative_link_descriptions", "demographic_distribution", "delivery_by_region", "access_token", "body",
    "works", "infringing_urls", "copyrighted_urls", "sender_address", "email", "phone", "telephone",
})
DSA_LIST_COLUMNS = ("decision_visibility", "territorial_scope", "content_type", "category_addition",
                    "category_specification", "content_language")
DSA_KEPT = ("uuid", "platform_name", "platform_uid", "decision_visibility", "decision_visibility_other",
            "end_date_visibility_restriction", "decision_monetary", "decision_monetary_other",
            "end_date_monetary_restriction", "decision_provision", "end_date_service_restriction",
            "decision_account", "end_date_account_restriction", "account_type", "decision_ground",
            "decision_ground_reference_url", "illegal_content_legal_ground", "incompatible_content_ground",
            "incompatible_content_illegal", "category", "category_addition", "category_specification",
            "content_type", "content_type_other", "content_language", "content_date", "territorial_scope",
            "application_date", "source_type", "automated_detection", "automated_decision", "created_at")
DSA_DROPPED = ("decision_facts", "illegal_content_explanation", "incompatible_content_explanation", "puid",
               "source_identity", "content_id_ean", "url")
META_FIELDS = ("id", "page_id", "page_name", "bylines", "ad_creation_time", "ad_delivery_start_time",
               "ad_delivery_stop_time", "spend", "impressions", "currency", "estimated_audience_size", "languages",
               "publisher_platforms", "ad_snapshot_url")
META_DROPPED = ("ad_creative_bodies", "ad_creative_link_titles", "ad_creative_link_captions",
                "ad_creative_link_descriptions", "demographic_distribution", "delivery_by_region")
GOOGLE_MEMBERS = {"advertisers": "google-political-ads-advertiser-stats.csv",
                  "creatives": "google-political-ads-creative-stats.csv",
                  "updated": "google-political-ads-updated.csv"}
LUMEN_DROPPED = ("body", "works", "sender_address", "sender_email", "infringing_urls", "copyrighted_urls")
_FEC_ID = re.compile(r"\b[CHSP]\d{8}\b")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PlatformTransparencyFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "none"


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def strip_token(url: Any) -> str | None:
    """A published URL without any access token in its query (Meta's ad_snapshot_url carries one)."""
    text = clean(url)
    if not text:
        return None
    parts = urlsplit(text)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.casefold() != "access_token"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def forbidden_paths(value: Any, path: str = "$") -> list[str]:
    """Paths of keys that the SP01 minimisation decision never stores."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_FIELD_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_paths(item, f"{path}[{index}]")
    return found


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Forbidden keys in a record's fields and any access token left in a stored string."""
    found = forbidden_paths(record.get("fields") or {}, "$.fields")
    if "access_token=" in json.dumps(record, ensure_ascii=False, default=str):
        found.append("$..access_token")
    return found


# ------------------------------------------------------------------ record keys


def statement_key(platform_uid: Any, uuid: Any) -> str:
    return f"platform-transparency:dsa:sor:{slug(platform_uid)}:{str(uuid).strip()}"


def dump_key(platform_uid: Any, day: str, variant: str) -> str:
    return f"platform-transparency:dsa:dump:{slug(platform_uid)}:{day}:{variant}"


def advertiser_key(platform: str, advertiser_id: Any) -> str:
    return f"platform-transparency:{platform}:advertiser:{str(advertiser_id).strip()}"


def ad_key(platform: str, ad_id: Any) -> str:
    return f"platform-transparency:{platform}:ad:{str(ad_id).strip()}"


def notice_key(notice_id: Any) -> str:
    return f"platform-transparency:lumen:notice:{str(notice_id).strip()}"


def _record(fmt: str, kind: str, record_key: str, *, platform: str, title: Any, locator: str,
            fields: Mapping[str, Any], selection_key: str, native_revision: Any = None, revision_order: str = "",
            effective_on: Any = None, advertiser: str | None = None, data_as_of: Any = None,
            withheld: Sequence[str] = ()) -> dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise PlatformTransparencyFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://"):
        raise PlatformTransparencyFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": FORMATS[fmt]["provider"],
        "platform": platform,
        "record_kind": kind,
        "record_key": record_key,
        "advertiser_key": advertiser,
        "selection_key": selection_key,
        "native_revision": clean(native_revision),
        "revision_order": revision_order,
        "effective_on": _day(effective_on),
        "data_as_of": clean(data_as_of),
        "listing_status": "listed",
        "title": clean(title) or record_key,
        "locator": locator,
        "minimisation": {"policy": MINIMISATION_POLICY, "withheld": sorted(set(withheld))},
        "fields": dict(fields),
    }


# ------------------------------------------------------------------ DSA Transparency Database


def dsa_file_name(unit: Mapping[str, Any]) -> str:
    return f"sor-{slug(unit['platform'])}-{unit['date']}-{unit.get('variant') or 'light'}.zip"


def _csv_members(raw: bytes, depth: int = 0) -> list[tuple[str, bytes]]:
    """CSV members of a ZIP (nested ZIP parts followed one level); plain CSV bytes are one member."""
    if not raw.startswith(b"PK"):
        return [("dump.csv", raw)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PlatformTransparencyFormatError("schema_drift", "dump is not a readable ZIP file") from exc
    out = []
    for info in sorted(archive.infolist(), key=lambda i: i.filename):
        if info.is_dir():
            continue
        body = archive.read(info)
        if info.filename.lower().endswith(".zip") and depth == 0:
            out += _csv_members(body, depth + 1)
        elif info.filename.lower().endswith(".csv"):
            out.append((info.filename, body))
    return out


def _dsa_value(column: str, value: Any) -> Any:
    text = clean(value)
    if text is None:
        return None
    if column in DSA_LIST_COLUMNS and text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        return [str(v) for v in parsed] if isinstance(parsed, list) else text
    return text


def parse_dsa_dump(raw: bytes, unit: Mapping[str, Any], url: str) -> list[dict[str, Any]]:
    fmt = "dsa-sor-dump-csv"
    variant = str(unit.get("variant") or "light")
    platform = str(unit["platform"])
    selection = f"dsa:{slug(platform)}:{unit['date']}:{variant}"
    name = dsa_file_name(unit)
    rows: list[dict[str, str]] = []
    members = _csv_members(raw)
    for _, body in members:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PlatformTransparencyFormatError("schema_drift", "dump CSV is not UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "uuid" not in reader.fieldnames or "platform_uid" not in reader.fieldnames:
            raise PlatformTransparencyFormatError("schema_drift", "dump CSV has no uuid / platform_uid columns")
        rows += [dict(r) for r in reader]
        if len(rows) > DSA_ROW_CAP:
            raise PlatformTransparencyFormatError("input_limit", "dump has more rows than the unit bound")
    dump = _record(fmt, "dump-release", dump_key(platform, unit["date"], variant), platform=platform,
                   title=f"DSA Transparency Database dump {name}", locator=url,
                   fields={"file_name": name, "platform_uid": platform, "day": unit["date"], "variant": variant,
                           "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                           "csv_members": [m for m, _ in members], "statements": len(rows)},
                   selection_key=selection, native_revision=hashlib.sha256(raw).hexdigest()[:16],
                   revision_order=unit["date"], effective_on=unit["date"], data_as_of=unit["date"])
    out = [dump]
    seen = set()
    for row in rows:
        uid = clean(row.get("platform_uid"))
        if slug(uid) != slug(platform):
            continue  # another platform's row in a shared file: never stored
        uuid = clean(row.get("uuid"))
        if not uuid:
            raise PlatformTransparencyFormatError("schema_drift", "a statement has no uuid")
        if uuid in seen:
            raise PlatformTransparencyFormatError("schema_drift", f"statement {uuid} appears twice in one dump")
        seen.add(uuid)
        fields = {c: _dsa_value(c, row.get(c)) for c in DSA_KEPT}
        fields["dump_key"] = dump["record_key"]
        withheld = [c for c in DSA_DROPPED if clean(row.get(c))]
        out.append(_record(fmt, "statement-of-reasons", statement_key(uid, uuid), platform=clean(
            row.get("platform_name")) or platform, title=f"Statement of reasons {uuid} ({row.get('platform_name')})",
            locator=f"https://transparency.dsa.ec.europa.eu/statement/{uuid}", fields=fields,
            selection_key=selection, native_revision=uuid, revision_order=unit["date"],
            effective_on=fields.get("application_date") or fields.get("created_at"), data_as_of=unit["date"],
            withheld=withheld))
    return out


# ------------------------------------------------------------------ Meta Ad Library


def _range(value: Any, currency: Any = None) -> dict[str, Any] | None:
    """A published range kept verbatim: lower and upper bound strings (upper may be open)."""
    if not isinstance(value, Mapping):
        return None
    out = {"lower_bound": clean(value.get("lower_bound")), "upper_bound": clean(value.get("upper_bound"))}
    if currency is not None:
        out["currency"] = clean(currency)
    return out


def parse_meta(payloads: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "meta-ad-library-json"
    page_id = str(unit["page_id"])
    selection = f"meta:{page_id}:{','.join(sorted(unit['countries']))}:{unit['delivery_date_min']}:" \
                f"{unit['delivery_date_max']}"
    advertiser = advertiser_key("meta", page_id)
    ads, names, bylines = [], set(), set()
    for payload in payloads:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise PlatformTransparencyFormatError("schema_drift", "ads_archive response has no data list")
        for item in payload["data"]:
            if not isinstance(item, Mapping) or not clean(item.get("id")):
                raise PlatformTransparencyFormatError("schema_drift", "ads_archive item has no id")
            if str(item.get("page_id")) != page_id:
                continue  # never store another advertiser's ad
            withheld = [k for k in META_DROPPED if item.get(k)]
            funding = [clean(b) for b in (item.get("bylines") if isinstance(item.get("bylines"), list)
                                          else [item.get("bylines")]) if clean(b)]
            names.add(clean(item.get("page_name")) or page_id)
            bylines.update(funding)
            fields = {
                "ad_id": clean(item["id"]), "page_id": page_id, "advertiser_as_declared": clean(item.get("page_name")),
                "funding_entity_as_declared": funding, "ad_creation_time": clean(item.get("ad_creation_time")),
                "delivery_start": clean(item.get("ad_delivery_start_time")),
                "delivery_stop": clean(item.get("ad_delivery_stop_time")),
                "spend_range_as_published": _range(item.get("spend"), item.get("currency")),
                "impressions_range_as_published": _range(item.get("impressions")),
                "estimated_audience_size_as_published": _range(item.get("estimated_audience_size")),
                "currency": clean(item.get("currency")), "languages": list(item.get("languages") or []),
                "publisher_platforms": list(item.get("publisher_platforms") or []),
                "reached_countries_selected": sorted(unit["countries"]),
                "snapshot_url": strip_token(item.get("ad_snapshot_url")),
                "election_id_declared": unit.get("election_id"),
            }
            ads.append(_record(fmt, "ad", ad_key("meta", item["id"]), platform="meta",
                               title=f"Meta ad {item['id']} by {fields['advertiser_as_declared']}",
                               locator=f"https://www.facebook.com/ads/library/?id={item['id']}", fields=fields,
                               selection_key=selection, advertiser=advertiser,
                               revision_order=fields["delivery_stop"] or fields["delivery_start"] or "",
                               effective_on=fields["delivery_start"], withheld=withheld))
    out = [_record(fmt, "advertiser", advertiser, platform="meta", title=f"Meta page {page_id}",
                   locator=f"https://www.facebook.com/ads/library/?view_all_page_id={page_id}",
                   fields={"advertiser_id": page_id, "names_as_published": sorted(names),
                           "funding_entities_as_declared": sorted(bylines), "public_ids_as_published": [],
                           "country_selection": sorted(unit["countries"])},
                   selection_key=selection, advertiser=advertiser)] if ads else []
    return out + ads


# ------------------------------------------------------------------ Google political ads


def _zip_member(archive: zipfile.ZipFile, name: str) -> str | None:
    for info in archive.infolist():
        if info.filename.rsplit("/", 1)[-1] == name:
            return archive.read(info).decode("utf-8-sig")
    return None


def parse_google_bundle(raw: bytes, unit: Mapping[str, Any], url: str) -> list[dict[str, Any]]:
    fmt = "google-political-ads-bundle"
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PlatformTransparencyFormatError("schema_drift", "bundle is not a readable ZIP file") from exc
    texts = {k: _zip_member(archive, v) for k, v in GOOGLE_MEMBERS.items()}
    if texts["advertisers"] is None or texts["creatives"] is None:
        raise PlatformTransparencyFormatError("schema_drift", "bundle lacks the advertiser or creative stats member")
    refreshed = None
    if texts["updated"]:
        rows = list(csv.DictReader(io.StringIO(texts["updated"])))
        refreshed = clean(rows[0].get("Report_Data_Updated_Time")) if rows else None
    wanted = {str(a) for a in unit["advertisers"]}
    mappings = {clean(m.get("label_as_published")): m.get("election_id") for m in unit.get("elections") or []}
    out: list[dict[str, Any]] = []
    found = set()
    for row in csv.DictReader(io.StringIO(texts["advertisers"])):
        aid = clean(row.get("Advertiser_ID"))
        if aid not in wanted:
            continue  # rows of undeclared advertisers are never stored
        found.add(aid)
        public_ids = [p.strip() for p in str(row.get("Public_IDs_List") or "").replace(";", ",").split(",")
                      if p.strip()]
        elections = [e.strip() for e in str(row.get("Elections") or "").replace(";", ",").split(",") if e.strip()]
        fields = {"advertiser_id": aid, "names_as_published": [clean(row.get("Advertiser_Name"))],
                  "public_ids_as_published": public_ids,
                  "fec_committee_ids_as_published": sorted({m for p in public_ids for m in _FEC_ID.findall(p)}),
                  "regions_as_published": clean(row.get("Regions")), "elections_as_published": elections,
                  "election_ids_declared": sorted({mappings[e] for e in elections if mappings.get(e)}),
                  "total_creatives_as_published": clean(row.get("Total_Creatives")),
                  "spend_usd_as_published": clean(row.get("Spend_USD")),
                  "spend_note": "a total as Google published it; not a sum computed here",
                  "funding_entities_as_declared": [], "country_selection": [unit.get("region")] if unit.get(
                      "region") else []}
        out.append(_record(fmt, "advertiser", advertiser_key("google", aid), platform="google",
                           title=f"Google advertiser {aid}",
                           locator=f"https://adstransparency.google.com/advertiser/{aid}", fields=fields,
                           selection_key=f"google:{aid}", advertiser=advertiser_key("google", aid),
                           native_revision=refreshed, revision_order=refreshed or "", data_as_of=refreshed))
    missing = sorted(wanted - found)
    creatives = 0
    advertisers = {r["fields"]["advertiser_id"]: r for r in out}
    for row in csv.DictReader(io.StringIO(texts["creatives"])):
        aid = clean(row.get("Advertiser_ID"))
        if aid not in wanted:
            continue
        creatives += 1
        if creatives > GOOGLE_CREATIVE_CAP:
            raise PlatformTransparencyFormatError("input_limit", "more creative rows than the selection bound")
        ad_id = clean(row.get("Ad_ID"))
        if not ad_id:
            raise PlatformTransparencyFormatError("schema_drift", "a creative row has no Ad_ID")
        spend = {}
        for column, value in row.items():
            match = re.fullmatch(r"Spend_Range_(Min|Max)_([A-Z]{3})", str(column))
            if match and clean(value) is not None:
                spend.setdefault(match.group(2), {"currency": match.group(2)})[
                    "lower_bound" if match.group(1) == "Min" else "upper_bound"] = clean(value)
        advertiser = advertisers.get(aid)
        fields = {
            "ad_id": ad_id, "ad_type": clean(row.get("Ad_Type")), "advertiser_id": aid,
            "advertiser_as_declared": clean(row.get("Advertiser_Name")), "funding_entity_as_declared": [],
            "regions_as_published": clean(row.get("Regions")),
            "delivery_start": clean(row.get("Date_Range_Start")), "delivery_stop": clean(row.get("Date_Range_End")),
            "first_served": clean(row.get("First_Served_Timestamp")),
            "last_served": clean(row.get("Last_Served_Timestamp")),
            "impressions_bucket_as_published": clean(row.get("Impressions")),
            "spend_bucket_usd_as_published": clean(row.get("Spend_USD")),
            "spend_ranges_as_published": [spend[c] for c in sorted(spend)],
            "age_targeting_as_published": clean(row.get("Age_Targeting")),
            "gender_targeting_as_published": clean(row.get("Gender_Targeting")),
            "geo_targeting_included_as_published": clean(row.get("Geo_Targeting_Included")),
            "geo_targeting_excluded_as_published": clean(row.get("Geo_Targeting_Excluded")),
            "election_ids_declared": (advertiser or {}).get("fields", {}).get("election_ids_declared", []),
            "data_refreshed_at": refreshed,
        }
        out.append(_record(fmt, "ad", ad_key("google", ad_id), platform="google",
                           title=f"Google political ad {ad_id} by {fields['advertiser_as_declared']}",
                           locator=clean(row.get("Ad_URL")) or f"https://adstransparency.google.com/advertiser/{aid}"
                                                                f"/creative/{ad_id}",
                           fields=fields, selection_key=f"google:{aid}", advertiser=advertiser_key("google", aid),
                           native_revision=refreshed, revision_order=refreshed or "",
                           effective_on=fields["delivery_start"], data_as_of=refreshed))
    if missing:
        for aid in missing:
            out.append(_record(fmt, "advertiser", advertiser_key("google", aid), platform="google",
                               title=f"Google advertiser {aid} (not in the bundle)",
                               locator=f"https://adstransparency.google.com/advertiser/{aid}",
                               fields={"advertiser_id": aid, "names_as_published": [], "public_ids_as_published": [],
                                       "fec_committee_ids_as_published": [], "elections_as_published": [],
                                       "election_ids_declared": [], "funding_entities_as_declared": [],
                                       "note": "the declared advertiser id is not listed in this bundle"},
                               selection_key=f"google:{aid}", advertiser=advertiser_key("google", aid),
                               native_revision=refreshed, revision_order=refreshed or "", data_as_of=refreshed))
    del url
    return out


# ------------------------------------------------------------------ Lumen


def parse_lumen(payloads: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "lumen-notices-json"
    selection = f"lumen:{slug(unit['recipient'])}:{unit['from']}:{unit['to']}"
    out = []
    for payload in payloads:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("notices"), list):
            raise PlatformTransparencyFormatError("schema_drift", "Lumen response has no notices list")
        for notice in payload["notices"]:
            if not isinstance(notice, Mapping) or notice.get("id") in (None, ""):
                raise PlatformTransparencyFormatError("schema_drift", "a Lumen notice has no id")
            works = notice.get("works") if isinstance(notice.get("works"), list) else []
            urls = sum(len(w.get("infringing_urls") or []) for w in works if isinstance(w, Mapping))
            withheld = [k for k in LUMEN_DROPPED if notice.get(k)]
            if works:
                withheld.append("works")
            fields = {
                "notice_id": str(notice["id"]), "type": clean(notice.get("type")),
                "title_as_published": clean(notice.get("title")),
                "sender_name_as_published": clean(notice.get("sender_name")),
                "principal_name_as_published": clean(notice.get("principal_name")),
                "recipient_name_as_published": clean(notice.get("recipient_name")),
                "date_sent": clean(notice.get("date_sent")), "date_received": clean(notice.get("date_received")),
                "topics": [str(t) for t in notice.get("topics") or []],
                "jurisdictions": [str(j) for j in notice.get("jurisdictions") or []],
                "action_taken": clean(notice.get("action_taken")), "works_count": len(works),
                "infringing_url_count": urls,
                "redactions_as_published": sorted({v for v in (notice.get("title"), notice.get("sender_name"),
                                                               notice.get("principal_name"))
                                                   if isinstance(v, str) and ("[" in v and "]" in v)}),
            }
            out.append(_record(fmt, "takedown-notice", notice_key(notice["id"]), platform=clean(
                notice.get("recipient_name")) or unit["recipient"], title=fields["title_as_published"],
                locator=f"https://lumendatabase.org/notices/{notice['id']}", fields=fields, selection_key=selection,
                revision_order=clean(notice.get("date_received")) or "", effective_on=notice.get("date_received"),
                withheld=withheld))
    return out


# ------------------------------------------------------------------ units and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = [dict(u) for u in selection.get("units") or []]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a platform-transparency selection names 1-{MAX_UNITS} units")
    for unit in units:
        if fmt == "dsa-sor-dump-csv":
            if not clean(unit.get("platform")) or not _DAY.fullmatch(str(unit.get("date") or "")) or \
                    unit.get("variant", "light") not in {"light", "full"}:
                raise SourcePackError("invalid_manifest", "a DSA unit names a platform, a day and light or full")
        elif fmt == "meta-ad-library-json":
            if not re.fullmatch(r"\d{1,20}", str(unit.get("page_id") or "")) or not unit.get("countries") or \
                    not all(re.fullmatch(r"[A-Z]{2}", str(c)) for c in unit["countries"]):
                raise SourcePackError("invalid_manifest", "a Meta unit names one page id and reached countries")
            start, end = _day(unit.get("delivery_date_min")), _day(unit.get("delivery_date_max"))
            if not start or not end or end < start or _days(start, end) > 366:
                raise SourcePackError("invalid_manifest", "a Meta unit names a delivery window of at most 366 days")
        elif fmt == "google-political-ads-bundle":
            advertisers = unit.get("advertisers") or []
            if not 1 <= len(advertisers) <= GOOGLE_ADVERTISER_CAP or not all(
                    re.fullmatch(r"AR\d{6,24}", str(a)) for a in advertisers):
                raise SourcePackError("invalid_manifest", f"a Google unit names 1-{GOOGLE_ADVERTISER_CAP} "
                                                          "advertiser ids (AR...)")
        elif fmt == "lumen-notices-json":
            start, end = _day(unit.get("from")), _day(unit.get("to"))
            if not clean(unit.get("recipient")) or not start or not end or end < start or _days(start, end) > 92:
                raise SourcePackError("invalid_manifest", "a Lumen unit names a recipient and at most 92 days")
    if fmt == "google-political-ads-bundle" and len(units) != 1:
        raise SourcePackError("invalid_manifest", "a Google source reads one bundle (one unit)")
    return units


def _days(start: str, end: str) -> int:
    from datetime import date

    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def requests_for(fmt: str, unit: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The request path (relative to the endpoint) and parameters of a unit's first request."""
    if fmt == "dsa-sor-dump-csv":
        return "/" + dsa_file_name(unit), {}
    if fmt == "meta-ad-library-json":
        return "/ads_archive", {
            "ad_type": "POLITICAL_AND_ISSUE_ADS", "ad_active_status": "ALL",
            "ad_reached_countries": json.dumps(sorted(unit["countries"])),
            "search_page_ids": json.dumps([str(unit["page_id"])]),
            "ad_delivery_date_min": unit["delivery_date_min"], "ad_delivery_date_max": unit["delivery_date_max"],
            "fields": ",".join(META_FIELDS), "limit": META_PER_PAGE}
    if fmt == "google-political-ads-bundle":
        return "/transparencyreport/google-political-ads-transparency-bundle.zip", {}
    if fmt == "lumen-notices-json":
        return "/notices/search.json", {"recipient_name": unit["recipient"],
                                        "date_received_facet": f"{unit['from']}..{unit['to']}",
                                        "per_page": LUMEN_PER_PAGE, "page": 1, "sort_by": "date_received asc"}
    raise SourcePackError("invalid_manifest", f"unknown platform-transparency format {fmt!r}")


def declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("platform_transparency") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "platform-transparency sources declare a matching provider and "
                                                  "format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live", "gated-not-granted"}:
        raise SourcePackError("invalid_manifest", "platform-transparency sources state their LIVE_VERIFICATION")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "platform-transparency sources declare the SP01 minimisation policy")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", f"{declared['provider']} is fetched from "
                                                  f"{PROVIDER_HOSTS[declared['provider']][0]} only")
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed platform-transparency formats declare a required secret")
    _units(fmt, dict(declared.get("selection") or {}))
    return declared


class PlatformTransparencyAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = declaration(self.source)
        self.format = self.declared["format"]
        self.provider = self.declared["provider"]
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
            "platform_transparency": {"provider": self.provider, "format": self.format, "units": len(self.units),
                                      "keyed": bool(FORMATS[self.format]["keyed"]),
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

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, text/csv, application/zip"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", f"the {self.provider} source needs its access token "
                                                               f"({PROVIDER_CONTRACTS[self.provider]['access_decision']})")
            if self.provider == "lumen":
                headers["X-Authentication-Token"] = self.secret
            else:
                headers["Authorization"] = f"Bearer {self.secret}"
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
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
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not valid UTF-8 JSON") from exc

    def _collect(self, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """All requests of one unit (all-or-nothing within its page bound), parsed into records."""
        path, params = requests_for(self.format, unit)
        if self.format in {"dsa-sor-dump-csv", "google-political-ads-bundle"}:
            raw, receipt = self._get(path, params)
            url = self.source["endpoint"].rstrip("/") + path
            parser = parse_dsa_dump if self.format == "dsa-sor-dump-csv" else parse_google_bundle
            return parser(raw, unit, url), [receipt]
        payloads, receipts = [], []
        request = dict(params)
        bound = META_MAX_PAGES if self.format == "meta-ad-library-json" else LUMEN_MAX_PAGES
        while True:
            raw, receipt = self._get(path, request)
            payload = self._json(raw)
            payloads.append(payload)
            receipts.append(receipt)
            if not isinstance(payload, Mapping):
                raise SourcePackError("schema_drift", "response is not a JSON object")
            if self.format == "meta-ad-library-json":
                paging = dict(payload.get("paging") or {})
                after = dict(paging.get("cursors") or {}).get("after")
                more = bool(paging.get("next")) and bool(after)
                following = {**params, "after": after}
            else:
                meta = dict(payload.get("meta") or {})
                more = int(meta.get("current_page") or request["page"]) < int(meta.get("total_pages") or 1)
                following = {**request, "page": int(request["page"]) + 1}
            if not more:
                break
            if len(payloads) >= bound:
                raise SourcePackError("budget_exhausted", "unit is longer than its page bound; never truncated")
            request = following
        parser = parse_meta if self.format == "meta-ad-library-json" else parse_lumen
        return parser(payloads, unit), receipts

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
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
        listings = sorted({(r["selection_key"], r["record_kind"]) for r in records
                           if r["record_kind"] in {"ad", "statement-of-reasons", "takedown-notice"}})
        if self.format == "google-political-ads-bundle":
            listings = sorted(set(listings) | {(f"google:{a}", "ad") for a in unit["advertisers"]})
        elif not listings:
            listings = [(_empty_selection(self.format, unit), _listed_kind(self.format))]
        receipt = {
            "contract": RECEIPT_CONTRACT, "source_id": self.source["source_id"], "provider": self.provider,
            "format": self.format, "unit_index": index, "unit": unit, "requests": requests, "records": len(records),
            "evidence_origin": origin, "live_verification": self.declared["live_verification"],
            "minimisation": MINIMISATION_POLICY,
            "withheld_fields": sorted({w for r in records for w in r["minimisation"]["withheld"]}),
            "complete_listings": [{"selection_key": s, "record_kind": k} for s, k in listings],
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"], "language": "en",
                "published_at": record["effective_on"], "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "platform_transparency_record": record,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


def _listed_kind(fmt: str) -> str:
    return {"dsa-sor-dump-csv": "statement-of-reasons", "lumen-notices-json": "takedown-notice"}.get(fmt, "ad")


def _empty_selection(fmt: str, unit: Mapping[str, Any]) -> str:
    if fmt == "meta-ad-library-json":
        return f"meta:{unit['page_id']}:{','.join(sorted(unit['countries']))}:{unit['delivery_date_min']}:" \
               f"{unit['delivery_date_max']}"
    if fmt == "lumen-notices-json":
        return f"lumen:{slug(unit['recipient'])}:{unit['from']}:{unit['to']}"
    return f"dsa:{slug(unit['platform'])}:{unit['date']}:{unit.get('variant') or 'light'}"


FIXTURE_SECRET = "fixture-token-not-a-real-credential"
ADAPTERS = {CONNECTOR: PlatformTransparencyAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence.

    A page body is text, a JSON value, or ``body_base64`` for binary (ZIP) responses.
    """
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
        key = parts.path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
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
    """The SP01 decisions as data."""
    return {"contract": "noesis-platform-transparency-source-contracts-v1",
            "decision_document": "docs/development/platform-transparency-evidence/source-audit.md",
            "providers": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
            "bounded_coverage": BOUNDED_COVERAGE, "excluded": EXCLUDED_SOURCES, "minimisation": MINIMISATION,
            "review_boundary": REVIEW_BOUNDARY}


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "EXCLUDED_SOURCES", "FIXTURE_SECRET", "FORMATS", "LIVE_VERIFICATION",
    "MINIMISATION", "MINIMISATION_POLICY", "PROVIDER_CONTRACTS", "RECORD_CONTRACT", "RECORD_KINDS",
    "REVIEW_BOUNDARY", "PlatformTransparencyAdapter", "PlatformTransparencyFormatError", "declaration",
    "fixture_transport", "minimisation_violations", "parse_dsa_dump", "parse_google_bundle", "parse_lumen",
    "parse_meta", "replay_native_fixture", "requests_for", "source_contracts", "strip_token",
]

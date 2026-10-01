"""Offline harness for the OSINT platform-transparency features (#2580): authored responses through the real adapter.

Every file under ``tests/fixtures/platform_transparency`` is authored in the
provider's documented shape (DSA light-dump CSV, Meta ``/ads_archive`` JSON,
Google political ads bundle CSVs, Lumen ``search.json``) and names fictional
platforms, pages, advertisers and notices only; placeholder personal text
exists to prove the SP01 minimisation. Nothing here is live coverage.
Responses go through :class:`PlatformTransparencyAdapter` (the connector the
runtime compiles) and :class:`PlatformTransparencyProjector`.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import duckdb

from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
    GOOGLE_MEMBERS,
    PlatformTransparencyAdapter,
    fixture_transport,
    requests_for,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.platform_transparency_records import PlatformTransparencyProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/platform_transparency"
PACK = ROOT / "config/source_packs/osint-platform-transparency.json"
NS = "global"
OWN_NS = "ownership"
READ = "knowledge:osint:platform-transparency:read"
WRITE = "knowledge:osint:platform-transparency:write"
NOTICES = "knowledge:osint:platform-transparency:notices:read"
SCOPES = {
    READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write", "knowledge:ownership:read", "knowledge:ownership:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:political:lobbying:read",
    "knowledge:political:elections:read", "knowledge:political:campaign-finance:read",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review", NOTICES}
READ_ONLY = {READ, f"namespace:{NS}:read"}
PLATFORM = "exampla-social"
CANDIDATE_COMMITTEE = "campaign-finance:fec:committee:C00999901"
GOOGLE_CAMPAIGN = "AR10000000000000000001"
GOOGLE_UTILITIES = "AR10000000000000000002"
GOOGLE_PARTY = "AR10000000000000000003"
GOOGLE_ABSENT = "AR10000000000000000004"
META_HOLDINGS = "999000001"
META_FUND = "999000002"
META_PERSON = "999000003"
US_ELECTION = "us-us-president:2099"
UK_ELECTION = "gb-general:2099-05-07"
T1 = 4_098_124_800_000  # 2099-11-12T00:00Z, observation time of the first acquisition (fixed for as-of answers)
T2 = 4_099_766_400_000  # 2099-12-01T00:00Z, observation time of the second acquisition
# source id -> ordered (unit, [fixture files per request]) pairs, as declared in the pack
UNITS: dict[str, list[tuple[dict, list[str]]]] = {
    "dsa-sor-dumps": [
        ({"platform": PLATFORM, "date": "2099-05-01", "variant": "light"}, ["dsa_exampla-social_2099-05-01.csv"]),
        ({"platform": PLATFORM, "date": "2099-05-02", "variant": "light"}, ["dsa_exampla-social_2099-05-02.csv"]),
        ({"platform": "northwind-video", "date": "2099-05-01", "variant": "light"},
         ["dsa_northwind-video_2099-05-01.csv"]),
    ],
    "meta-ad-library-political": [
        ({"page_id": META_HOLDINGS, "countries": ["GB"], "delivery_date_min": "2099-04-01",
          "delivery_date_max": "2099-05-07", "election_id": UK_ELECTION},
         ["meta_ads_999000001_GB_p1.json", "meta_ads_999000001_GB_p2.json"]),
        ({"page_id": META_FUND, "countries": ["US"], "delivery_date_min": "2099-09-01",
          "delivery_date_max": "2099-11-03", "election_id": US_ELECTION}, ["meta_ads_999000002_US_p1.json"]),
        ({"page_id": META_PERSON, "countries": ["GB"], "delivery_date_min": "2099-04-01",
          "delivery_date_max": "2099-05-07"}, ["meta_ads_999000003_GB_p1.json"]),
    ],
    "google-political-ads": [
        ({"advertisers": [GOOGLE_CAMPAIGN, GOOGLE_UTILITIES, GOOGLE_PARTY, GOOGLE_ABSENT],
          "elections": [{"label_as_published": "US Federal", "election_id": US_ELECTION},
                        {"label_as_published": "UK Parliamentary General Election 2099",
                         "election_id": UK_ELECTION}]}, ["google-bundle"]),
    ],
    "lumen-notices": [
        ({"recipient": "Exampla Social", "from": "2099-05-01", "to": "2099-05-31"},
         ["lumen_exampla-social_2099-05.json"]),
    ],
}
SOURCES = list(UNITS)
FORMATS = {"dsa-sor-dumps": "dsa-sor-dump-csv", "meta-ad-library-political": "meta-ad-library-json",
           "google-political-ads": "google-political-ads-bundle", "lumen-notices": "lumen-notices-json"}
ENDPOINTS = {"dsa-sor-dumps": "https://dsa-sor-data-dumps.s3.eu-central-1.amazonaws.com",
             "meta-ad-library-political": "https://graph.facebook.com/v21.0",
             "google-political-ads": "https://storage.googleapis.com", "lumen-notices": "https://lumendatabase.org"}
_ZIP_TIME = (2099, 1, 1, 0, 0, 0)


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def fixture_path(filename: str, version: str = "v1") -> Path:
    later = FIXTURES / "v2" / filename
    return later if version == "v2" and later.exists() else FIXTURES / filename


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, body)
    return buffer.getvalue()


def dsa_zip(filename: str, version: str = "v1") -> bytes:
    """A light dump as the Commission ships it: a ZIP holding a ZIP part holding the CSV (deterministic bytes)."""
    csv_name = filename.replace(".csv", "-00000.csv")
    inner = _zip([(csv_name, fixture_path(filename, version).read_bytes())])
    return _zip([(filename.replace(".csv", "-part-00000.zip"), inner)])


def google_bundle(version: str = "v1") -> bytes:
    files = {"advertisers": "google_advertiser_stats.csv", "creatives": "google_creative_stats.csv",
             "updated": "google_updated.csv"}
    return _zip([(f"google-political-ads-transparency-bundle/{GOOGLE_MEMBERS[k]}",
                  fixture_path(v, version).read_bytes()) for k, v in files.items()])


def native_pages(source_id: str, version: str = "v1") -> list[dict]:
    """The native responses for every declared unit, keyed by the request the adapter makes."""
    fmt = FORMATS[source_id]
    prefix = urlsplit(ENDPOINTS[source_id]).path
    pages = []
    for unit, files in UNITS[source_id]:
        path, params = requests_for(fmt, unit)
        if fmt == "dsa-sor-dump-csv":
            pages.append({"request": prefix + path, "status": 200, "headers": {"Content-Type": "application/zip"},
                          "body_base64": base64.b64encode(dsa_zip(files[0], version)).decode()})
            continue
        if fmt == "google-political-ads-bundle":
            pages.append({"request": prefix + path, "status": 200, "headers": {"Content-Type": "application/zip"},
                          "body_base64": base64.b64encode(google_bundle(version)).decode()})
            continue
        request = dict(params)
        for filename in files:
            path_ = fixture_path(filename, version)
            if not path_.exists() or (version == "v2" and filename.endswith("_p2.json")
                                      and not _has_next(fixture_path(files[0], version))):
                break
            body = json.loads(path_.read_text())
            query = urlencode(sorted(request.items()))
            pages.append({"request": prefix + path + ("?" + query if query else ""), "status": 200,
                          "headers": {"Content-Type": "application/json"}, "body": body})
            if fmt == "meta-ad-library-json":
                request = {**params, "after": body.get("paging", {}).get("cursors", {}).get("after")}
            else:
                request = {**request, "page": int(request["page"]) + 1}
    return pages


def _has_next(path: Path) -> bool:
    return bool(json.loads(path.read_text()).get("paging", {}).get("next"))


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the provider's documented shape; every platform, page, advertiser, funding "
                "entity, notice and statement is fictional, and placeholder personal text is discarded by the "
                "parser (SP01 minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-parties", "minimised-free-text"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None,
            secret: str | None = FIXTURE_SECRET) -> PlatformTransparencyAdapter:
    return PlatformTransparencyAdapter(item or source(source_id),
                                       transport=fixture_transport(native_pages(source_id, version)), secret=secret)


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None,
          observed_at_ms: int | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    at = observed_at_ms if observed_at_ms is not None else (T1 if version == "v1" else T2)
    projector = PlatformTransparencyProjector(conn, now=lambda: at)
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        outcomes += projector.project_page(run_id=run_id or f"run:{source_id}:{version}", manifest=None,
                                           source=item, records=page.records, documents=[],
                                           page_receipt=page.receipt, principal_id="operator")
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id)


def load_other_packs(conn) -> None:
    """Campaign finance (FEC committees, UK Commission), lobbying registers, elections and Corporate Ownership."""
    from tests.unit import campaign_finance_harness as cf

    cf.load_all(conn)
    cf.load_ownership(conn, OWN_NS)
    cf.load_lobbying(conn)
    cf.load_elections(conn)


def accepted_world(*, others: bool = True, version: str = "v1"):
    """Everything loaded and every exact-identifier or name candidate accepted by a reviewer, except the lobbying
    client candidates of the utilities association, which stay unreviewed."""
    from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

    conn = connection()
    load_all(conn)
    if version == "v2":
        load_all(conn, version="v2", run_id="run:v2")
    if others:
        load_other_packs(conn)
    identity = PlatformTransparencyIdentity(conn)
    proposed = identity.propose(NS, principal_id="alice", scopes=SCOPES,
                                ownership_namespace=OWN_NS if others else None)
    for candidate in proposed["candidates"]:
        if any(":client:" in r for r in candidate["records"]):
            continue
        identity.review(NS, candidate["candidate_id"], "accept", "fixture review", principal_id="rev",
                        scopes=REVIEW_SCOPES)
    return conn

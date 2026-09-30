"""Offline harness for the OSINT platform-transparency features (#2580): authored responses replayed through the adapter.

Every file under ``tests/fixtures/platform_transparency`` is authored in the
provider's documented shape (DSA Transparency Database light dump CSV columns,
Meta ``ads_archive`` JSON, BigQuery REST ``tables.get`` and ``jobs.query``
responses) and names fictional platforms, pages, advertisers and funders only;
the withheld columns carry synthetic placeholders that the parser discards.
Nothing here is live coverage. Dump zips are built deterministically (stored,
fixed timestamps) from the CSV files. Responses go through
:class:`PlatformTransparencyAdapter` (the connector the runtime compiles) and
:class:`PlatformTransparencyProjector`.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
    META_FIELDS,
    META_PAGE_SIZE,
    PlatformTransparencyAdapter,
    fixture_transport,
    google_queries,
    request_key,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.platform_transparency_records import PlatformTransparencyProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/platform_transparency"
PACK = ROOT / "config/source_packs/osint.json"
NS = "osint"
OWN_NS = "ownership"
CF_NS = "global"
READ = "knowledge:osint:platform-transparency:read"
WRITE = "knowledge:osint:platform-transparency:write"
SCOPES = {
    READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write", f"namespace:{CF_NS}:read", f"namespace:{CF_NS}:write", "knowledge:ownership:read",
    "knowledge:ownership:write", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
    "knowledge:political:lobbying:read", "knowledge:political:elections:read",
    "knowledge:political:campaign-finance:read", "knowledge:source-identity:read", "knowledge:source-identity:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
DSA = "platform-transparency-dsa-sor"
META = "platform-transparency-meta-ads"
GOOGLE = "platform-transparency-google-political-ads"
SOURCES = [DSA, META, GOOGLE]
FUND, CAMPAIGN, CIVIC = "AR00000000000000000001", "AR00000000000000000002", "AR00000000000000000003"
PARTY_PAGE, HOLDINGS_PAGE, AFFAIRS_PAGE = "100000000000001", "100000000000002", "100000000000003"
V1_MS, V2_MS = 4_083_955_200_000, 4_085_164_800_000  # 2099-06-01 and 2099-06-15, when each acquisition was recorded
SOURCE_PACK_FIXTURES = {DSA: "osint-platform-transparency-dsa.json", META: "osint-platform-transparency-meta.json",
                        GOOGLE: "osint-platform-transparency-google.json"}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def fixture_path(filename: str, version: str = "v1") -> Path:
    if version == "v2" and (FIXTURES / "v2" / filename).exists():
        return FIXTURES / "v2" / filename
    return FIXTURES / filename


def dump_zip(csv_text: str, name: str) -> bytes:
    """A deterministic dump archive: one stored CSV part with a fixed timestamp."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo(name.replace(".zip", "-00000.csv"), date_time=(2099, 1, 1, 0, 0, 0))
        archive.writestr(info, csv_text.encode())
    return buffer.getvalue()


def _page(key: str, *, body=None, body_base64=None, content_type="application/json") -> dict:
    page = {"request": key, "status": 200, "headers": {"Content-Type": content_type}}
    if body_base64 is not None:
        page["body_base64"] = body_base64
    else:
        page["body"] = body
    return page


def native_pages(source_id: str, version: str = "v1") -> list[dict]:
    """The native responses for every declared unit, keyed by the request the adapter makes."""
    item = source(source_id)
    selection = item["platform_transparency"]["selection"]
    base = urlsplit(item["endpoint"]).path.rstrip("/")
    pages = []
    if source_id == DSA:
        for unit in selection["dumps"]:
            name = f"sor-{unit['platform']}-{unit['date']}-light.zip"
            raw = dump_zip(fixture_path(f"dsa_sor_{unit['platform']}_{unit['date']}.csv", version).read_text(), name)
            pages.append(_page(request_key("GET", f"{base}/explore-data/download/{name}", {}),
                               body_base64=base64.b64encode(raw).decode(), content_type="application/zip"))
            pages.append(_page(request_key("GET", f"{base}/explore-data/download/{name}.sha1", {}),
                               body=f"{hashlib.sha1(raw).hexdigest()}  {name}\n",
                               content_type="text/plain"))
        return pages
    if source_id == META:
        (unit,) = selection["pages"]
        params = {"ad_type": "POLITICAL_AND_ISSUE_ADS", "ad_reached_countries": json.dumps(sorted(unit["countries"])),
                  "search_page_ids": json.dumps(sorted(unit["page_ids"])), "ad_delivery_date_min": unit["from"],
                  "ad_delivery_date_max": unit["to"], "fields": ",".join(META_FIELDS), "limit": META_PAGE_SIZE}
        path = f"/{selection['api_version']}/ads_archive"
        first = json.loads(fixture_path("meta_ads_archive_gb_page1.json", version).read_text())
        pages.append(_page(request_key("GET", path, params), body=first))
        after = ((first.get("paging") or {}).get("cursors") or {}).get("after")
        if (first.get("paging") or {}).get("next"):
            second = json.loads(fixture_path("meta_ads_archive_gb_page2.json", version).read_text())
            pages.append(_page(request_key("GET", path, {**params, "after": after}), body=second))
        return pages
    currency = selection["currency"]
    for table in ("advertiser_stats", "creative_stats"):
        pages.append(_page(request_key("GET", f"{base}/projects/bigquery-public-data/datasets/google_political_ads/"
                                              f"tables/{table}", {}),
                           body=json.loads(fixture_path(f"google_table_{table}.json", version).read_text())))
    for unit in selection["advertisers"]:
        bodies = google_queries(unit, currency)
        for name, prefix in (("advertiser", "google_advertiser"), ("creatives", "google_creatives")):
            payload = json.dumps(bodies[name], sort_keys=True, separators=(",", ":")).encode()
            pages.append(_page(request_key("POST", f"{base}/projects/{selection['billing_project']}/queries", {},
                                           payload),
                               body=json.loads(fixture_path(f"{prefix}_{unit['id']}.json", version).read_text())))
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the provider's documented shape (the year 2099, example.invalid and the "
                "Example/Sample names are placeholders); every platform, page, advertiser and funder is fictional, "
                "and withheld columns (notifier identity, platform content id, free text, snapshot URL, demographic "
                "breakdown) carry synthetic placeholders that the parser discards (SP01 minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-parties", "minimised-fields", "ranges-as-published"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None) -> PlatformTransparencyAdapter:
    return PlatformTransparencyAdapter(item or source(source_id),
                                       transport=fixture_transport(native_pages(source_id, version)),
                                       secret=FIXTURE_SECRET)


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None,
          observed_at_ms: int | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = PlatformTransparencyProjector(conn)
    if observed_at_ms is not None:
        projector.store.now = lambda: observed_at_ms
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 600}, cursor=cursor)
        outcomes += projector.project_page(run_id=run_id or f"run:{source_id}:{version}", manifest=None, source=item,
                                           records=page.records, documents=[], page_receipt=page.receipt,
                                           principal_id="operator")
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id,
              observed_at_ms=V1_MS if version == "v1" else V2_MS)


def load_ownership(conn, namespace: str = OWN_NS):
    """A synthetic Corporate Ownership record carrying the name a Meta funding entity declares (GB)."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    records = [record("legal_entity", "gb-coh:09990002", {"provider": "companies-house",
                                                          "provider_record_id": "09990002"},
                      name="Example Holdings Ltd", jurisdiction="GB",
                      identifiers=[{"scheme": "gb-coh", "value": "09990002"}])]
    return OwnershipStore(conn).apply(namespace, records, run_id="ownership-fixture", observed_at_ms=0,
                                      principal_id="ownership-loader")


def load_campaign_finance(conn) -> None:
    """The Political campaign-finance fixtures (#2209) through that feature's own adapter and projector."""
    from tests.unit import campaign_finance_harness as cf

    cf.load_all(conn)


def load_lobbying(conn) -> None:
    from tests.unit import lobbying_harness as lob

    lob.apply(conn, "uk-orcl", "uk_orcl_2099-04-30.csv")


def load_elections(conn) -> None:
    """The fictional UK general election (#1908) with its published name."""
    from tests.unit import elections_harness as eh

    item = eh.source("gb")
    item["elections"]["election"]["name"] = "UK Parliamentary General Election 2099"
    eh.apply(conn, "gb", eh.UK, item=item)
    eh.apply(conn, "us", eh.US)


def world(*, elections: bool = True, lobbying: bool = True, ownership: bool = True, campaign_finance: bool = True,
          version: str = "v1"):
    conn = connection()
    load_all(conn)
    if version == "v2":
        load_all(conn, version="v2", run_id="run:v2")
    if campaign_finance:
        load_campaign_finance(conn)
    if ownership:
        load_ownership(conn)
    if lobbying:
        load_lobbying(conn)
    if elections:
        load_elections(conn)
    return conn


def link_campaign_finance_contests(conn) -> dict:
    """The campaign-finance feature's own reviewed candidate matches and contest links (#2209), as that feature's
    reviewer would leave them, so that an accepted committee match can reach a contest by citation."""
    from src.kb.campaign_finance_identity import CampaignFinanceIdentity
    from src.kb.campaign_finance_links import CampaignFinanceLinks
    from tests.unit import campaign_finance_harness as cf

    identity = CampaignFinanceIdentity(conn)
    proposed = identity.propose(CF_NS, principal_id="alice", scopes=cf.SCOPES)
    for candidate in proposed["candidates"]:
        records = " ".join(candidate["records"])
        if candidate["state"] == "proposed" and ":candidate:" in records and "99003" not in records:
            identity.review(CF_NS, candidate["candidate_id"], "accept", "fixture review", principal_id="rev",
                            scopes=cf.REVIEW_SCOPES)
    return CampaignFinanceLinks(conn).link_contests(CF_NS, principal_id="alice", scopes=cf.SCOPES)


def accepted_world(**kwargs):
    """Everything loaded and every proposed identity candidate accepted by a reviewer."""
    from src.kb.platform_transparency_identity import PlatformTransparencyIdentity

    conn = world(**kwargs)
    identity = PlatformTransparencyIdentity(conn)
    proposed = identity.propose(NS, principal_id="alice", scopes=SCOPES,
                                ownership_namespace=OWN_NS if kwargs.get("ownership", True) else None,
                                campaign_finance_namespace=CF_NS, lobbying_namespace=CF_NS,
                                elections_namespace=CF_NS)
    for candidate in proposed["candidates"]:
        identity.review(NS, candidate["candidate_id"], "accept", "fixture review", principal_id="rev",
                        scopes=REVIEW_SCOPES)
    return conn

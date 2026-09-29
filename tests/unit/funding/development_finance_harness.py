"""Offline harness for the Funding & Grants development-finance feature (#1932).

Every file under ``tests/fixtures/development_finance`` is authored in the
provider's documented shape (IATI activity standard 2.03 XML, World Bank
Projects API v2 JSON, OECD SDMX-CSV) and names fictional organisations,
activities and amounts only. IATI and World Bank pages go through the real
clients on an injected :class:`DurableHTTP` transport (receipts say
``execution: injected``, i.e. fixture evidence); CRS series go through the real
source-pack adapter and projector. Nothing here is live coverage.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.development_finance_sources import (
    PROVIDER_HOSTS,
    DevelopmentFinanceAdapter,
    IATIDatastoreClient,
    WorldBankProjectsClient,
    fixture_request,
    fixture_transport,
)
from src.ingestion.provider_execution import DurableHTTP
from src.ingestion.source_packs import validate_source_pack
from src.kb.development_finance import DevelopmentFinanceProjector
from src.kb.development_finance_acquisition import acquire_iati, acquire_world_bank

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/development_finance"
PACK = ROOT / "config/source_packs/economic.json"
NS = "aid"
NOTICE = "Authored offline fixture; not a live capture"
READ = "knowledge:funding:development-finance:read"
WRITE = "knowledge:funding:development-finance:write"
REVIEW = "knowledge:funding:development-finance:review"
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:funding:read",
    "namespace:grants:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:schema:register",
    "knowledge:schema:read",
    "knowledge:projects:read",
    "knowledge:projects:write",
}
REVIEW_SCOPES = SCOPES | {
    REVIEW,
    "knowledge:ownership:review",
    "knowledge:geospatial:review",
}
READ_ONLY = {READ, f"namespace:{NS}:read"}
FDPA = "XM-DAC-99901"
NGO = "XI-IATI-FICTNGO"
EMPTY_IATI = b'<?xml version="1.0"?><iati-activities version="2.03"/>'
CRS_SOURCE = "oecd-crs-development-finance"


def ms(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


class Web:
    """Routes IATI queries and World Bank requests to fixture files; swap routes to simulate revisions or outages."""

    def __init__(self) -> None:
        self.iati: dict[str, str | int | bytes] = {}
        self.world_bank: str | int = "wb_projects_2098-04.json"
        self.calls: list[dict] = []

    def transport(self, *, method, url, params, body, headers, timeout_s, max_bytes):
        del method, body, timeout_s, max_bytes
        self.calls.append(
            {"url": url, "params": dict(params), "headers": sorted(headers)}
        )
        host = urlsplit(url).hostname
        if host == "api.iatistandard.org":
            if int(params.get("start", "0")) > 0:
                return {
                    "status": 200,
                    "headers": {"Content-Type": "application/xml"},
                    "content": EMPTY_IATI,
                }
            target = next((v for k, v in self.iati.items() if k in params["q"]), None)
        else:
            target = self.world_bank
        if target is None:
            return {"status": 404, "headers": {}, "content": b"not found"}
        if isinstance(target, int):
            return {"status": target, "headers": {}, "content": b"unavailable"}
        if isinstance(target, bytes):  # an edited copy of a fixture, served as-is
            return {
                "status": 200,
                "headers": {"Content-Type": "application/xml"},
                "content": target,
            }
        return {
            "status": 200,
            "headers": {
                "Content-Type": "application/xml"
                if target.endswith(".xml")
                else "application/json"
            },
            "content": (FIXTURES / target).read_bytes(),
        }


class Env:
    def __init__(
        self, path: str | None = None, now_iso: str = "2098-03-05T08:00:00"
    ) -> None:
        self.conn = duckdb.connect(path or ":memory:")
        self.web = Web()
        self.clock = ms(now_iso)
        self.budgets = 0

    def now(self) -> int:
        return self.clock

    def at(self, iso: str) -> "Env":
        self.clock = ms(iso)
        return self

    def http(self, provider: str) -> DurableHTTP:
        self.budgets += 1
        return DurableHTTP(
            self.conn,
            budget_id=f"{provider}-{self.budgets}",
            provider=provider,
            principal_id="ingest",
            allowed_hosts=PROVIDER_HOSTS[provider],
            reuse_notice=NOTICE,
            max_requests=50,
            transport=self.web.transport,
            now=self.now,
        )

    def iati(
        self,
        file: str | int | bytes,
        *,
        publishers=(FDPA,),
        observation: str,
        rows: int = 100,
        **selection,
    ):
        key = " OR ".join(f'"{p}"' for p in sorted(publishers)) if publishers else None
        self.web.iati = {key or "": file}
        client = IATIDatastoreClient(
            self.http("iati-datastore"),
            principal_id="ingest",
            subscription_key="fixture-subscription-key-0001",
        )
        chosen = {"publishers": list(publishers)} if publishers else {}
        chosen.update(selection)
        return acquire_iati(
            client,
            chosen,
            namespace=NS,
            scopes=SCOPES,
            observation=observation,
            reuse_notice=NOTICE,
            rows=rows,
        )

    def world_bank(
        self,
        file: str | int = "wb_projects_2098-04.json",
        *,
        observation: str,
        countries=("KE", "UG", "TZ"),
    ):
        self.web.world_bank = file
        client = WorldBankProjectsClient(
            self.http("world-bank-projects"), principal_id="ingest"
        )
        return acquire_world_bank(
            client,
            {"countries": list(countries)},
            namespace=NS,
            scopes=SCOPES,
            observation=observation,
            reuse_notice=NOTICE,
        )

    def crs(self, file: str, *, run_id: str | None = None, release: dict | None = None):
        item = crs_source(release=release)
        document = item["development_finance"]["documents"][0]
        adapter = DevelopmentFinanceAdapter(
            item,
            transport=fixture_transport(
                [
                    {
                        "request": fixture_request(document),
                        "status": 200,
                        "headers": {"Content-Type": "text/csv"},
                        "body": (FIXTURES / file).read_text(),
                    }
                ]
            ),
        )
        page = adapter.fetch_page(
            {"operation": "release", "parameters": {}, "limit": 50}, cursor=None
        )
        projector = DevelopmentFinanceProjector(self.conn)
        projector.store.now = self.now
        return projector.project_page(
            run_id=run_id or f"run:{file}",
            manifest=None,
            source=item,
            records=page.records,
            documents=[],
            page_receipt=page.receipt,
            principal_id="operator",
        )[0]

    def load(self):
        """The journey's first state: publisher A (March), publisher B (April), World Bank projects, CRS January."""
        self.at("2098-03-05T08:00:00").iati(
            "iati_fdpa_2098-03.xml", observation="r1:fdpa"
        )
        self.at("2098-04-05T08:00:00").iati(
            "iati_fictngo_2098-04.xml", publishers=(NGO,), observation="r1:ngo"
        )
        self.at("2098-04-06T08:00:00").world_bank(observation="r1:wb")
        self.at("2099-01-20T08:00:00").crs("crs_deu_ken_140_2099-01.csv")
        return self


def edited(name: str, *replacements: tuple[str, str]) -> bytes:
    text = (FIXTURES / name).read_text()
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text.encode()


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def crs_source(*, release: dict | None = None) -> dict:
    item = copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == CRS_SOURCE)
    )
    item["development_finance"]["namespace"] = NS
    if release is not None:
        item["development_finance"]["documents"][0]["release"] = release
    return item


def activity_key(conn, publisher_ref: str, identifier: str) -> str:
    from src.kb.development_finance import activity_key as key
    from src.kb.development_finance import publisher_id

    return key(publisher_id("iati", publisher_ref), identifier)


def seed_ownership(conn) -> None:
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    source = {"provider": "companies-house", "provider_record_id": "99000001"}
    OwnershipStore(conn).apply(
        NS,
        [
            record(
                "legal_entity",
                "companies-house:99000001",
                source,
                name="FICTIONAL WATER WORKS LTD",
                jurisdiction="GB",
                identifiers=[{"scheme": "gb-coh", "value": "99000001"}],
            ),
            # Two unrelated entities sharing a name: a name alone never picks one of them.
            record(
                "legal_entity",
                "companies-house:99000002",
                {**source, "provider_record_id": "99000002"},
                name="Fictional Learning Trust",
                jurisdiction="GB",
                identifiers=[{"scheme": "gb-coh", "value": "99000002"}],
            ),
            record(
                "legal_entity",
                "companies-house:99000003",
                {**source, "provider_record_id": "99000003"},
                name="Fictional Learning Trust",
                jurisdiction="GB",
                identifiers=[{"scheme": "gb-coh", "value": "99000003"}],
            ),
        ],
        run_id="own",
        observed_at_ms=1,
        principal_id="p",
    )


def seed_open_call(conn, *, now: int) -> None:
    """A fictional funding call whose funder id is the IATI organisation identifier of publisher A's funder."""
    from src.kb.funding_opportunities import FundingOpportunityStore
    from src.kb.funding_records import record

    FundingOpportunityStore(conn, now=lambda: now).ingest(
        "grants",
        "eu-ft",
        [
            record(
                "eu-ft",
                "call",
                "FICT-CALL-2099-01",
                "Fictional water innovation call 2099",
                source_url="https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/fict-call-2099-01.json",
                authority={
                    "kind": "funder",
                    "name": "Fictional Development Partnership Agency",
                },
                funder={"name": "Fictional Development Partnership Agency", "id": FDPA},
                status={"native": "open", "asserted": "open"},
                deadlines=[
                    {
                        "kind": "submission",
                        "text": "1 June 2099",
                        "date": "2099-06-01",
                        "timezone": None,
                        "instant": None,
                    }
                ],
            ),
        ],
        observation_id="fixture-call",
        observed_at_ms=now,
        scopes={
            "knowledge:funding:write",
            "knowledge:ingestion:execute",
            "namespace:grants:write",
        },
    )


def register_places(conn, *, now: int) -> None:
    from src.kb.geospatial import GeospatialStore

    geo = GeospatialStore(conn, now=lambda: now)
    for code, name in (("KE", "Kenya"), ("UG", "Uganda")):
        geo.register_place(
            "global",
            name,
            "country",
            names=[{"value": name, "language": "en", "kind": "canonical"}],
            source_ids={"iso3166-1-alpha2": code},
            parent_ids=[],
            principal_id="system",
            scopes={"knowledge:geospatial:write"},
            place_key=f"fixture:{code}",
        )

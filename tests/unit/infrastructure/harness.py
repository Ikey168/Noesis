"""Offline harness for the Geospatial infrastructure feature: acquire authored fixtures through the real adapters."""

from __future__ import annotations

from src.ingestion import infrastructure_sources as src
from tests.unit.infrastructure import fixture_builder as fb

NS = "infrastructure"
SCOPES = {"knowledge:infrastructure:read", "knowledge:infrastructure:write", "knowledge:infrastructure:review",
          f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{NS}:admin",
          "knowledge:subscriptions:read", "knowledge:subscriptions:write",
          "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
          "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ownership:review",
          "knowledge:energy:read", "namespace:energy:read", "knowledge:legal:read", "namespace:legal:read",
          "knowledge:environment:read", "namespace:environment:read",
          "namespace:ownership:read", "namespace:ownership:write"}


def acquire(conn, name, *, namespace=NS, scopes=SCOPES, selection=None):
    provider, default = fb.selection(name)
    fixture = fb.load(name)
    return src.acquire(conn, provider, selection or default, namespace=namespace, scopes=scopes,
                       principal_id="analyst", fetch=src.fixture_transport(fixture["native_pages"]),
                       now=src.fixture_clock(fixture))


def acquire_all(conn, names=None, **kwargs):
    return {name: acquire(conn, name, **kwargs) for name in names or list(fb.SELECTIONS)}


OWNERSHIP_NS = "ownership"


def load_ownership(conn, namespace=OWNERSHIP_NS):
    """Synthetic Corporate Ownership legal entities: one with an LEI and a Wikidata QID, two name-only ones."""

    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    def entity(key, name, jurisdiction, identifiers):
        return record("legal_entity", key, {"provider": "gleif", "provider_record_id": key.split(":", 1)[1]},
                      name=name, jurisdiction=jurisdiction, identifiers=identifiers)

    records = [
        entity("lei:529900FIXTURE0000001", "Fixture Energie AG", "DE",
               [{"scheme": "lei", "value": "529900FIXTURE0000001"}, {"scheme": "wikidata", "value": "Q999001"}]),
        entity("lei:529900FIXTURE0000002", "Fixture Gastransport GmbH", "DE",
               [{"scheme": "lei", "value": "529900FIXTURE0000002"}]),
        entity("lei:529900FIXTURE0000003", "Fixture Holding SE", "LU",
               [{"scheme": "lei", "value": "529900FIXTURE0000003"}]),
    ]
    return OwnershipStore(conn).apply(namespace, records, run_id="ownership-fixture", observed_at_ms=0,
                                      principal_id="ownership-loader")


def load_legal(conn, namespace="legal"):
    """A synthetic legal work whose identifier is the docket the EIA LNG layer cites."""

    from src.kb.legal import REGIONAL_CONTRACT, LegalStore

    return LegalStore(conn).project(namespace, [{
        "contract": REGIONAL_CONTRACT, "provider": "berlin-law", "provider_id": "CP99-001-000", "kind": "normative",
        "language": "en", "title": "Fixture order authorising the Fixture LNG Terminal",
        "fields": {"enactment_date": "2020-01-01"}}], run_id="legal-fixture", source_id="legal-fixture")


ENV_SCOPES = {"knowledge:environment:write", "knowledge:environment:read", "namespace:environment:write",
              "namespace:environment:read"}


def load_environment(conn, namespace="environment"):
    """Two synthetic facilities: one citing the GPPD id of Fixture Lignite Plant Nord, one sharing only a name."""

    from src.kb import environment_records as er
    from src.kb.environment_store import EnvironmentStore

    return EnvironmentStore(conn).apply(namespace, [
        er.facility("eea-industry", "F-1", "Fixture Lignite Plant Nord", source_url="https://industry.eea.europa.eu/",
                    geometry=None, operator={"name": "Fixture Energie AG"}, identifiers={"gppd_idnr": "DEU9990001"}),
        er.facility("eea-industry", "F-2", "Fixture Kraftwerk Alt", source_url="https://industry.eea.europa.eu/",
                    geometry=None, operator={"name": None})], run_id="env", principal_id="env", scopes=ENV_SCOPES)

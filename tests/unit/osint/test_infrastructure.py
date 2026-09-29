"""infrastructure_pivot (OX08, #2048): organization-keyed, cited, person-refusing."""

from __future__ import annotations

import pytest

from src.ingestion import crtsh, rdap
from src.osint.infrastructure import CAVEAT, classify_identifier, infrastructure_pivot
from tests.unit.osint.test_infrastructure_sources import (
    NS,
    SCOPES,
    Clock,
    transport_for,
)

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def pivot_world():
    from src.kb.source_identity import SourceIdentityStore

    conn = duckdb.connect()
    clock = Clock()
    store = SourceIdentityStore(conn, now=clock)
    ids = {}
    for key, name, domain in (
        ("news", "Exampla News", "exampla-news.example"),
        ("sister", "Sister Daily", "sister-daily.example"),
    ):
        ident = store.register(
            NS, "publication", name, principal_id="analyst", scopes=SCOPES
        )
        store.decide_alias(
            NS,
            ident["source_id"],
            "domain",
            domain,
            reason="publisher masthead domain",
            reviewer_id="reviewer",
            scopes=SCOPES,
        )
        ids[key] = ident["source_id"]
    r = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="r1",
        transport=transport_for("rdap-domain-exampla-news.json"),
        now=clock,
    )
    rdap.project_rdap(
        conn, NS, r["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    c = crtsh.acquire_crtsh(
        conn,
        "exampla-news.example",
        request_id="c1",
        transport=transport_for("crtsh-exampla-news.json"),
        now=clock,
    )
    crtsh.project_crtsh(
        conn, NS, c["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    yield conn, ids, r["observation_id"], c["observation_id"]
    conn.close()


@pytest.mark.parametrize(
    "identifier,form",
    [
        ("editor@exampla-news.example", "email"),
        ("@exampla_editor", "handle"),
        ("192.0.2.44", "ip_address"),
        ("2001:db8::44", "ip_address"),
        ("[2001:db8::44]", "ip_address"),
        ("person:jr", "person_id"),
        ("username:jrivera", "username"),
    ],
)
def test_person_keyed_identifiers_are_refused(pivot_world, identifier, form):
    conn, *_ = pivot_world
    assert classify_identifier(identifier) == form
    out = infrastructure_pivot(conn, identifier, namespace=NS)
    assert out["status"] == "person_identifier_refused"
    assert out["identifier_form"] == form and out["paths"] == []


def test_domain_pivots_to_registrant_org_and_shared_infrastructure(pivot_world):
    conn, ids, rdap_obs, ct_obs = pivot_world
    out = infrastructure_pivot(conn, "exampla-news.example", namespace=NS)
    assert out["status"] == "ok"
    assert out["resolution"]["source_id"] == ids["news"]
    by_reach = {p["reached"]: p for p in out["paths"]}
    sister = by_reach[ids["sister"]]
    [hop] = sister["hops"]
    assert (
        hop["relationship_type"] == "shared-infrastructure"
        and hop["status"] == "probable"
    )
    assert hop["citations"][0]["observation_id"] == ct_obs
    assert hop["citations"][0]["certificate_ids"] == ["5000002"]
    owner = next(
        p for p in out["paths"] if p["hops"][0]["relationship_type"] == "ownership"
    )
    assert owner["reached_identity"]["display_name"] == "Exampla Media Holdings Ltd"
    assert owner["hops"][0]["citations"][0]["observation_id"] == rdap_obs
    assert all(p["cited"] for p in out["paths"])
    assert out["caveat"] == CAVEAT and "CDN" in out["caveat"]
    assert not {"same_operator", "verdict", "attribution"} & set(out)


def test_organization_source_id_pivots_back_to_its_domains(pivot_world):
    conn, ids, *_ = pivot_world
    first = infrastructure_pivot(conn, "exampla-news.example", namespace=NS)
    org = next(
        p["reached"]
        for p in first["paths"]
        if p["hops"][0]["relationship_type"] == "ownership"
    )
    out = infrastructure_pivot(conn, org, namespace=NS, max_depth=2)
    reached = {p["reached"] for p in out["paths"]}
    assert (
        ids["news"] in reached and ids["sister"] in reached
    )  # org -> news -> sister (probable)
    two_hop = next(p for p in out["paths"] if p["reached"] == ids["sister"])
    assert two_hop["statuses"] == ["as-registered", "probable"]


def test_depth_is_bounded(pivot_world):
    conn, ids, *_ = pivot_world
    first = infrastructure_pivot(conn, "exampla-news.example", namespace=NS)
    org = next(
        p["reached"]
        for p in first["paths"]
        if p["hops"][0]["relationship_type"] == "ownership"
    )
    out = infrastructure_pivot(conn, org, namespace=NS, max_depth=1)
    assert {p["reached"] for p in out["paths"]} == {ids["news"]}
    assert infrastructure_pivot(conn, org, namespace=NS, max_depth=99)["max_depth"] == 3


def test_unknown_domain_and_missing_layer(pivot_world):
    conn, *_ = pivot_world
    assert (
        infrastructure_pivot(conn, "nowhere.example", namespace=NS)["status"]
        == "not_found"
    )
    assert (
        infrastructure_pivot(conn, "*.exampla-news.example", namespace=NS)["status"]
        == "wildcard_refused"
    )
    empty = duckdb.connect()
    assert (
        infrastructure_pivot(empty, "exampla-news.example")["status"] == "not_available"
    )

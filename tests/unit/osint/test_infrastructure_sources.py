"""RDAP (OX05 #2045) and crt.sh (OX06 #2046) acquisition and projection, offline.

Transports replay hand-written fixtures under ``tests/fixtures/osint``; nothing
leaves the process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion import crtsh, osint_observations, rdap
from src.ingestion.osint_observations import ObservationError, normalize_domain
from src.kb.source_identity import (
    READ_SCOPE,
    REVIEW_SCOPE,
    RELATIONSHIP_TYPES,
    WRITE_SCOPE,
    SourceIdentityStore,
)

duckdb = pytest.importorskip("duckdb")
FIX = Path(__file__).resolve().parents[2] / "fixtures/osint"
NS = "osint"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE}


def transport_for(name, calls=None, status=200):
    body = (FIX / name).read_bytes()

    def transport(*, url, params, headers, timeout, max_bytes):
        if calls is not None:
            calls.append(
                {
                    "url": url,
                    "params": params,
                    "timeout": timeout,
                    "max_bytes": max_bytes,
                }
            )
        return {"status": status, "content": body, "final_url": url}

    return transport


class Clock:
    def __init__(self):
        self.value = 1_790_000_000_000

    def __call__(self):
        self.value += 1000
        return self.value


@pytest.fixture()
def world():
    conn = duckdb.connect()
    clock = Clock()
    store = SourceIdentityStore(conn, now=clock)
    ids = {}
    for key, name, domain in (
        ("news", "Exampla News", "exampla-news.example"),
        ("sister", "Sister Daily", "sister-daily.example"),
        ("other", "Unrelated Wire", "unrelated.example"),
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
    yield conn, store, ids, clock
    conn.close()


# --------------------------------------------------------------------- input


@pytest.mark.parametrize(
    "value,code",
    [
        ("*.exampla-news.example", "wildcard_refused"),
        ("%.exampla-news.example", "wildcard_refused"),
        ("editor@exampla-news.example", "person_identifier_refused"),
        ("192.0.2.10", "ip_lookup_refused"),
        ("2001:db8::1", "ip_lookup_refused"),
        ("https://exampla-news.example/a", "invalid_domain"),
        ("localhost", "invalid_domain"),
    ],
)
def test_only_one_explicit_domain_is_accepted(value, code):
    with pytest.raises(ObservationError) as exc:
        normalize_domain(value)
    assert exc.value.code == code


def test_domain_is_normalized():
    assert normalize_domain("WWW.Exampla-News.Example.") == "exampla-news.example"


# ---------------------------------------------------------------------- RDAP


def test_rdap_parse_keeps_registrar_and_organization_only():
    facts = rdap.parse_rdap_domain(
        (FIX / "rdap-domain-exampla-news.json").read_bytes(), "exampla-news.example"
    )
    assert facts["registrar"] == {"name": "Fixture Registrar GmbH", "iana_id": "9999"}
    assert facts["registrant_organization"] == "Exampla Media Holdings Ltd"
    assert [e["action"] for e in facts["events"]] == [
        "registration",
        "last changed",
        "expiration",
    ]
    blob = json.dumps(facts)
    for leaked in (
        "Alex Fixture-Person",
        "alex@",
        "domains@",
        "abuse@",
        "Fixture Street",
        "tel:",
        "Abuse Desk",
    ):
        assert leaked not in blob


def test_individual_registrant_yields_only_registrar_fields(world):
    conn, _store, _ids, clock = world
    receipt = rdap.acquire_rdap(
        conn,
        "private-blog.example",
        request_id="r-ind",
        transport=transport_for("rdap-domain-individual-registrant.json"),
        now=clock,
    )
    assert receipt["status"] == "acquired"
    stored = osint_observations.get_observation(conn, receipt["observation_id"])
    facts = stored["facts"]
    assert facts["registrant_organization"] is None
    assert facts["registrant_withheld"] == "natural-person registrant dropped"
    assert facts["registrar"]["name"] == "Budget Names Inc"
    row = conn.execute("SELECT record_json FROM osint_observations").fetchone()[0]
    for leaked in ("Robin", "robin@", "Home Lane", "555.0100", "Springfield"):
        assert leaked not in row
    assert stored["raw_stored"] is False


def test_rdap_receipt_is_bounded_and_replayable(world):
    conn, _store, _ids, clock = world
    calls = []
    transport = transport_for("rdap-domain-exampla-news.json", calls)
    first = rdap.acquire_rdap(
        conn, "exampla-news.example", request_id="r1", transport=transport, now=clock
    )
    assert first["status"] == "acquired" and first["retries"] == 0
    assert first["redistribution"] == "provider-specific"
    assert calls == [
        {
            "url": "https://rdap.org/domain/exampla-news.example",
            "params": {},
            "timeout": 10.0,
            "max_bytes": 5_000_000,
        }
    ]
    again = rdap.acquire_rdap(
        conn, "exampla-news.example", request_id="r1", transport=transport, now=clock
    )
    assert (
        again == first and len(calls) == 1
    )  # replayed from the receipt, no second request
    with pytest.raises(ObservationError):
        rdap.acquire_rdap(
            conn,
            "sister-daily.example",
            request_id="r1",
            transport=transport,
            now=clock,
        )
    record = osint_observations.get_observation(conn, first["observation_id"])
    assert record["contract"] == "osint-observation-v1"
    assert (
        record["archive_at"]
        and record["temporal_semantics"] == "fetch-and-archive-observation"
    )


def test_rdap_failure_is_receipted_not_retried(world):
    conn, _store, _ids, clock = world
    calls = []
    receipt = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="r-503",
        transport=transport_for("rdap-domain-exampla-news.json", calls, status=503),
        now=clock,
    )
    assert receipt["status"] == "failed" and len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM osint_observations").fetchone()[0] == 0


def test_rdap_projects_revisions_and_ownership_side_by_side(world):
    conn, store, ids, clock = world
    other_owner = store.register(
        NS, "organization", "Rival Owner Group", principal_id="analyst", scopes=SCOPES
    )
    prior = store.relate(
        NS,
        other_owner["source_id"],
        ids["news"],
        "ownership",
        principal_id="analyst",
        scopes=SCOPES,
        evidence=[{"citation": "annual-report-2020"}],
    )
    receipt = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="r1",
        transport=transport_for("rdap-domain-exampla-news.json"),
        now=clock,
    )
    out = rdap.project_rdap(
        conn, NS, receipt["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    assert out["status"] == "projected" and out["source_identity"] == ids["news"]
    history = store.get(NS, ids["news"], scopes={READ_SCOPE}, include_history=True)[
        "revisions"
    ]
    assert len(history) == 2
    native = history[-1]["native_ids"]
    assert native["rdap:registration"] == "2011-03-14T09:00:00Z"
    assert native["rdap:registrar"] == "Fixture Registrar GmbH"
    assert native["rdap:observation"] == receipt["observation_id"]
    rel = out["ownership_relationship"]
    assert (
        rel["relationship_type"] == "ownership" and rel["to_source_id"] == ids["news"]
    )
    assert rel["evidence"][0]["observation_id"] == receipt["observation_id"]
    assert "not proof" in rel["policy"]["caveat"]
    # The conflicting ownership statement stays, side by side.
    assert [r["relationship_id"] for r in out["side_by_side"]] == [
        prior["relationship_id"]
    ]
    assert store._relationship(prior["relationship_id"])["lifecycle"] == "active"


def test_rdap_projection_without_a_source_identity_stores_only(world):
    conn, _store, _ids, clock = world
    receipt = rdap.acquire_rdap(
        conn,
        "private-blog.example",
        request_id="r2",
        transport=transport_for("rdap-domain-individual-registrant.json"),
        now=clock,
    )
    out = rdap.project_rdap(
        conn, NS, receipt["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    assert out["status"] == "no_source_identity"


def test_acquire_for_source_is_bounded_to_its_domains(world):
    conn, store, ids, clock = world
    calls = []
    out = rdap.acquire_for_source(
        conn,
        NS,
        ids["news"],
        request_id="batch",
        transport=transport_for("rdap-domain-exampla-news.json", calls),
        now=clock,
    )
    assert [r["domain"] for r in out["receipts"]] == ["exampla-news.example"]
    lone = store.register(
        NS, "publication", "No Domain Yet", principal_id="analyst", scopes=SCOPES
    )
    assert (
        rdap.acquire_for_source(conn, NS, lone["source_id"], request_id="b2")["status"]
        == "no_domain"
    )
    assert len(calls) == 1


def test_acquisition_is_logged_in_the_investigation_trail(world):
    conn, _store, _ids, clock = world
    from src.osint import investigation_audit
    from src.provisioning import store as prov

    prov.ensure_schema(conn)
    conn.execute(
        "INSERT INTO provisioned_kgs (name, description, status) VALUES ('vetting', 'source vetting', 'deployed')"
    )
    receipt = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="r-inv",
        investigation="vetting",
        transport=transport_for("rdap-domain-exampla-news.json"),
        now=clock,
    )
    trail = investigation_audit(conn, "vetting")["audit_trail"]
    assert trail[-1]["event"] == "osint-observation"
    assert trail[-1]["detail"]["observation_id"] == receipt["observation_id"]


def test_trace_artifact_shows_the_observation_chain(world):
    conn, _store, ids, clock = world
    from src.osint import trace_artifact

    receipt = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="r1",
        transport=transport_for("rdap-domain-exampla-news.json"),
        now=clock,
    )
    rdap.project_rdap(
        conn, NS, receipt["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    trace = trace_artifact(conn, document_id=receipt["observation_id"])
    stages = [s["stage"] for s in trace["chain"]]
    assert stages == ["source", "acquisition", "observation", "projection"]
    assert trace["cited"] is True
    assert trace["chain"][0]["source_pack_source"] == "rdap-domain"
    assert trace["chain"][1]["request_id"] == "r1"
    projected = trace["chain"][3]
    assert ids["news"] in {r["source_id"] for r in projected["identity_revisions"]}
    assert projected["relationships"]


# -------------------------------------------------------------------- crt.sh


def test_crtsh_parse_dedupes_drops_email_sans_and_uncovered_certificates():
    facts = crtsh.parse_crtsh(
        (FIX / "crtsh-exampla-news.json").read_bytes(),
        "exampla-news.example",
        max_results=100,
    )
    assert [c["crtsh_id"] for c in facts["certificates"]] == [
        "5000001",
        "5000002",
        "5000003",
    ]
    assert "newsdesk@exampla-news.example" not in json.dumps(facts)
    assert facts["certificates"][0]["issuer"] == "C=US, O=Fixture CA, CN=Fixture CA R1"
    assert facts["certificates"][1]["not_after"] == "2024-08-30T23:59:59"


def test_crtsh_request_is_one_exact_domain(world):
    conn, _store, _ids, clock = world
    calls = []
    receipt = crtsh.acquire_crtsh(
        conn,
        "exampla-news.example",
        request_id="c1",
        transport=transport_for("crtsh-exampla-news.json", calls),
        now=clock,
    )
    assert (
        receipt["status"] == "acquired" and receipt["redistribution"] == "locator-only"
    )
    assert calls[0]["url"] == "https://crt.sh/"
    assert calls[0]["params"] == {"q": "exampla-news.example", "output": "json"}
    with pytest.raises(ObservationError):
        crtsh.acquire_crtsh(
            conn,
            "%.exampla-news.example",
            request_id="c2",
            transport=transport_for("crtsh-exampla-news.json"),
            now=clock,
        )


def test_crtsh_projects_history_and_a_probable_shared_infrastructure_relation(world):
    conn, store, ids, clock = world
    assert "shared-infrastructure" in RELATIONSHIP_TYPES
    receipt = crtsh.acquire_crtsh(
        conn,
        "exampla-news.example",
        request_id="c1",
        transport=transport_for("crtsh-exampla-news.json"),
        now=clock,
    )
    out = crtsh.project_crtsh(
        conn, NS, receipt["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    native = store.get(NS, ids["news"], scopes={READ_SCOPE})["native_ids"]
    assert native["ct:certificate_count"] == "3"
    assert native["ct:first_not_before"] == "2023-01-10T00:00:00"
    assert native["ct:observation"] == receipt["observation_id"]
    [relation] = out["shared_infrastructure"]
    assert relation["relationship_type"] == "shared-infrastructure"
    assert {relation["from_source_id"], relation["to_source_id"]} == {
        ids["news"],
        ids["sister"],
    }
    assert relation["policy"]["status"] == "probable"
    assert "CDN" in relation["policy"]["caveat"]
    assert relation["evidence"][0]["certificate_ids"] == ["5000002"]
    # "unrelated.example" is a source domain but no certificate of this domain
    # covers it; single-domain certificates create no relation.
    others = {relation["to_source_id"], relation["from_source_id"]}
    assert ids["other"] not in others


def test_single_domain_certificates_create_no_relation(world):
    conn, _store, _ids, clock = world
    rows = [
        r
        for r in json.loads((FIX / "crtsh-exampla-news.json").read_text())
        if r["id"] != 5000002
    ]

    def transport(**_kwargs):
        return {"status": 200, "content": json.dumps(rows).encode()}

    receipt = crtsh.acquire_crtsh(
        conn, "exampla-news.example", request_id="c9", transport=transport, now=clock
    )
    out = crtsh.project_crtsh(
        conn, NS, receipt["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    assert out["status"] == "projected" and out["shared_infrastructure"] == []


def test_shared_infrastructure_is_not_an_independence_edge():
    from src.kb.source_identity import INDEPENDENCE_EDGES

    assert "shared-infrastructure" not in INDEPENDENCE_EDGES


def test_source_pack_fixtures_match_the_parsers():
    root = Path(__file__).resolve().parents[3]
    for name, parse in (("osint-rdap-domain", None), ("osint-crt-sh", None)):
        fixture = json.loads(
            (root / f"tests/fixtures/source_packs/{name}.json").read_text()
        )
        rebuilt = []
        for path in fixture["native_responses"]:
            raw = (root / path).read_bytes()
            if name == "osint-crt-sh":
                rebuilt.append(
                    crtsh.parse_crtsh(raw, "exampla-news.example", max_results=100)
                )
            else:
                domain = json.loads(raw)["ldhName"].lower()
                rebuilt.append(rdap.parse_rdap_domain(raw, domain))
        assert rebuilt == fixture["normalized"]

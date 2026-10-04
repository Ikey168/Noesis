"""Links to OSINT source identities by stated domain and to Vulnerabilities by stated CVE id (II08, #2786)."""

from __future__ import annotations

import copy

import pytest

from src.kb.internet_infrastructure_links import InfrastructureLinks, summarise
from src.kb.internet_infrastructure_records import InfrastructureRecordError
from src.kb.internet_infrastructure_store import InternetInfrastructureStore
from tests.unit import internet_infrastructure_harness as h


@pytest.fixture()
def conn():
    value = h.connection()
    h.load_all(value)
    yield value
    value.close()


def test_osint_links_rest_on_the_stated_domain_of_an_existing_source_identity_and_pin_both_revisions(conn):
    source_id = h.register_source_identity(conn)
    before = conn.execute("SELECT count(*) FROM source_identity_revisions").fetchone()[0]
    result = InfrastructureLinks(conn).link_osint(h.NS, principal_id="op", scopes=h.SCOPES)
    links = result["links"]
    assert summarise(links) == {"linked": 4}  # the RDAP domain and three certificates of example.org
    for link in links:
        assert link["basis"] == "stated-domain" and link["revision_id"].startswith("ii-rev:")
        assert link["target"]["source_id"] == source_id
        assert link["target"]["source_identity_revision_id"].startswith("source-identity-revision:")
        assert "shared addresses" in link["evidence"]["note"]
    # This provider writes no source identities.
    assert conn.execute("SELECT count(*) FROM source_identity_revisions").fetchone()[0] == before


def test_missing_osint_provider_or_target_is_reported_not_dropped(conn):
    absent = InfrastructureLinks(conn).link_osint(h.NS, principal_id="op", scopes=h.SCOPES)["links"]
    assert summarise(absent) == {"provider_absent": 4}
    h.register_source_identity(conn, domain="other.example.org", name="Other (fictional)")
    held = InfrastructureLinks(conn).link_osint(h.NS, principal_id="op", scopes=h.SCOPES)["links"]
    assert summarise(held) == {"target_not_held": 4}
    with pytest.raises(InfrastructureRecordError):
        InfrastructureLinks(conn).link_osint(h.NS, principal_id="op",
                                             scopes={h.WRITE, "namespace:global:write"})


def test_vulnerability_links_only_where_a_source_states_a_cve_id(conn):
    links = InfrastructureLinks(conn)
    first = links.link_vulnerabilities(h.NS, principal_id="op", scopes=h.SCOPES)
    assert first["links"] == [] and first["stated_cve_ids"] == 0 and "no record states a CVE id" in first["note"]
    assert first["provider_state"] == "provider_absent"
    # A record that states a CVE id (none of the first-coverage sources does) links, or reports the absence.
    store = InternetInfrastructureStore(conn)
    page = h.fetch("ct")[0]
    header = copy.deepcopy(page[0]["ii_unit"])
    items = [copy.deepcopy(r["ii_item"]) for r in page]
    items[1]["content"]["cve_ids"] = ["CVE-2099-0001"]
    header["unit_sha256"] = "e" * 64
    store.apply_unit(h.NS, header, items, source_id="ct-log-list", run_id="r", principal_id="svc", scopes=h.SCOPES,
                     retrieved_at_ms=h.FIRST_RETRIEVAL + 1)
    stated = links.link_vulnerabilities(h.NS, principal_id="op", scopes=h.SCOPES)
    assert stated["stated_cve_ids"] == 1 and summarise(stated["links"]) == {"provider_absent": 1}
    from src.kb.vulnerabilities import VulnerabilityStore  # creates the vuln_* tables

    VulnerabilityStore(conn)
    held = links.link_vulnerabilities(h.NS, principal_id="op", scopes=h.SCOPES)
    assert summarise(held["links"]) == {"target_not_held": 1}
    assert held["links"][0]["basis"] == "stated-cve-id"

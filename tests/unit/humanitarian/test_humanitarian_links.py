"""HR08 (#2266): links by explicit citation, shared identifier or accepted identity match; revision-aware."""

from __future__ import annotations

import pytest

from src.kb.humanitarian_identity import HumanitarianIdentity
from src.kb.humanitarian_links import HumanitarianLinks
from src.kb.humanitarian_records import HumanitarianError
from src.kb.humanitarian_store import HumanitarianStore
from tests.unit.humanitarian.harness import NS, REVIEWER_SCOPES, SCOPES, world

ARTICLE_URL = "https://news.example.org/2098/05/fixture-khartoum"


def seed_news(conn):
    from src.ingestion.document_store import DocumentStore

    DocumentStore(conn)
    conn.execute("INSERT INTO documents (document_id, source_type, url, title, content_hash) VALUES (?,?,?,?,?)",
                 ["news:fixture-1", "news", ARTICLE_URL, "Fixture wire: clashes reported", "c0ffee"])


def seed_population(conn):
    from src.kb.demographics import DemographicStore

    DemographicStore(conn)
    conn.execute("INSERT INTO demographic_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [NS, "series:sdn-pop", "fixture-statistics", "POP", "population_stock", "{}", "SDN", "Sudan",
                  "level:country", "def:pop", "persons", "persons", "annual", "release:1", 1])
    conn.execute("INSERT INTO demographic_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [NS, "vintage:sdn-pop-2098", "series:sdn-pop", "release:1", 4_070_000_000_000, "published", 1,
                  "retrieved", "def:pop", "hash", "[]", "{}", 1, 1])


@pytest.fixture()
def linked():
    value = world()
    seed_news(value.conn)
    seed_population(value.conn)
    return value, HumanitarianLinks(value.conn)


def test_explicit_citation_points_at_both_revisions(linked):
    value, links = linked
    link = links.cite(NS, "reliefweb:situation_report:9900001", "news-article", ARTICLE_URL,
                      citation={"locator": "https://reliefweb.int/report/sudan/fixture-9900001#sources"},
                      principal_id="alice", scopes=SCOPES)
    assert link["basis"] == "explicit-citation" and link["state"] == "linked"
    assert link["record_revision_id"].startswith("hum-rev:") and link["target_revision"] == "content:c0ffee"
    with pytest.raises(HumanitarianError):
        links.cite(NS, "reliefweb:situation_report:9900001", "news-article", ARTICLE_URL, citation={},
                   principal_id="alice", scopes=SCOPES)
    value.run("reliefweb-reports-sdn", revised=True)
    checked = links.links(NS, scopes=SCOPES, record_key="reliefweb:situation_report:9900001")["links"][0]
    assert checked["record_revision_id"] == link["record_revision_id"]  # still the cited revision
    assert checked["check"]["record_revised_since"] is True


def test_missing_targets_are_reported_as_broken_not_dropped(linked):
    _, links = linked
    broken = links.cite(NS, "reliefweb:appeal:9900002", "osint-event-dossier", "dossier:missing",
                        citation={"locator": "appeal annex"}, principal_id="alice", scopes=SCOPES)
    assert broken["state"] == "broken" and broken["reason"]
    hazard = links.link_shared_identifiers(NS, principal_id="alice", scopes=SCOPES)
    assert hazard["links"] == [] and {u["glide"] for u in hazard["unlinked"]} == {"CE-2098-000001-SDN"}
    listed = links.links(NS, scopes=SCOPES)
    assert [b["link_id"] for b in listed["broken"]] == [broken["link_id"]]


def test_glide_shared_identifier_links_to_a_hazard_event(linked):
    _, links = linked
    result = links.link_shared_identifiers(NS, principal_id="alice", scopes=SCOPES,
                                           hazard_events={"CE-2098-000001-SDN": "hazard:fixture-1"})
    assert {(link["record_key"], link["basis"]) for link in result["links"]} == {
        ("reliefweb:crisis:99001", "shared-identifier"), ("reliefweb:situation_report:9900001", "shared-identifier"),
        ("reliefweb:appeal:9900002", "shared-identifier")}
    assert all(link["evidence"]["identifier"] == {"scheme": "glide", "value": "CE-2098-000001-SDN"}
               for link in result["links"])


def test_population_attaches_only_through_an_accepted_place_match_with_its_vintage(linked):
    value, links = linked
    with pytest.raises(HumanitarianError) as caught:
        links.attach_population(NS, "reliefweb:situation_report:9900001", "series:sdn-pop", principal_id="alice",
                                scopes=SCOPES)
    assert caught.value.code == "no_basis"
    identity = HumanitarianIdentity(value.conn)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    iso3 = identity.assertions(NS, scopes=SCOPES, subject_key="place-ref:iso3:SDN", target_kind="geospatial-place")[0]
    identity.review(NS, iso3["assertion_id"], "accept", "ISO3 equals COD-AB admin 0", principal_id="bob",
                    scopes=REVIEWER_SCOPES)
    link = links.attach_population(NS, "reliefweb:situation_report:9900001", "series:sdn-pop", principal_id="alice",
                                   scopes=SCOPES)
    assert link["basis"] == "accepted-identity-match" and link["target_revision"] == "vintage:sdn-pop-2098"
    assert link["evidence"]["assertion_id"] == iso3["assertion_id"]
    assert "no per-capita value is computed" in link["notice"]
    assert "per_capita" not in str(link) and "rate" not in link["evidence"]
    store = HumanitarianStore(value.conn, initialize=False)
    assert store.revision(NS, "reliefweb:situation_report:9900001", scopes=SCOPES)  # record untouched

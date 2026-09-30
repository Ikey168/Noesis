"""Offline institution-to-statistics acceptance for the Science education-statistics feature (ED13, #2441).

The pinned IPEDS, ETER, UNESCO UIS, OECD Education at a Glance and Eurostat R&D fixtures (authored in the documented
shapes; every institution, identifier and value is fictional) replay through the real ``education-statistics``
adapter - OECD and Eurostat through the SDMX connector - and the runtime's projector, with the feature selected in the
Science composition plan and sockets blocked. A ROR institution and a country reach cited statistics with vintages,
definitions, comparability notes and linked Science and Funding records. Offline evidence only, never live coverage
(``docs/development/education-evidence/``).
"""

from __future__ import annotations

import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.education_identity import EducationIdentity
from src.kb.education_monitoring import EducationMonitor
from src.kb.education_statistics import (
    EducationLinks,
    EducationProjector,
    EducationQueries,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from tests.unit import education_harness as h
from tests.unit.composition.test_migration import _migrated

R1, R2 = "https://ror.org/0zfs01a23", "https://ror.org/0zmc02b34"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def figures(answer, concept):
    group = next(c for c in answer["concepts"] if c["concept"] == concept)
    return {(p["period"], f["provider"], f["indicator"]["code"]): f for p in group["periods"] for f in p["figures"]}


def all_figures(answer):
    return [f for c in answer["concepts"] for p in c["periods"] for f in p["figures"]]


def test_ror_institution_and_country_to_cited_statistics_with_vintages_and_links():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("science", bundles["science"]["version"], features=["education-statistics"])
    assert coordinator.activate("education-acceptance")["status"] == "published"
    assert feature_enabled(conn)
    assert PROJECTORS["noesis-education-statistic-record-v1"](conn).__class__ is EducationProjector

    # Pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "education-statistics"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": ours})
    assert replay["valid"] and replay["coverage"]["verified"] == 5

    # First releases, then the IPEDS final release, a later UIS version and a Eurostat update.
    h.load_all(conn, revisions=True)
    status = readiness(conn)
    assert status["selected"] and status["providers"]["ipeds"]["releases"] == 6
    assert {p["live_verification"] for p in status["providers"].values()} == {"unverified-live"}

    # ROR identity: published identifiers are exact, the IPEDS name match waits for review.
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"]
    by = {(m["subject"]["code"], m["ror_id"]): m for m in proposed}
    assert by[("DE0001", R2)]["basis"] == "ror-external-id" and by[("DE0001", R2)]["state"] == "exact"
    queries = EducationQueries(conn)
    assert queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1)["status"] == "none_on_record"
    identity.review(h.NS, by[("100001", R1)]["match_id"], "accept", "same name and city", principal_id="reviewer",
                    scopes=h.SCOPES)
    identity.review(h.NS, by[("100001", "https://ror.org/0zfs04d56")]["match_id"], "reject", "other city",
                    principal_id="reviewer", scopes=h.SCOPES)

    # A ROR institution reaches cited statistics; the as-of date picks the provisional or the final vintage.
    spring = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1, as_of="2099-06-30")
    autumn = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1, as_of="2099-12-31")
    assert autumn["identity"]["basis"][0]["basis"] == "name-location"
    assert autumn["identity"]["basis"][0]["state"] == "accepted"
    early = figures(spring, "enrolment")[("2098", "ipeds", "EFTOTLT")]
    late = figures(autumn, "enrolment")[("2098", "ipeds", "EFTOTLT")]
    assert (early["value"], early["release"]["release_stage"]) == ("25000", "provisional")
    assert (late["value"], late["release"]["release_stage"]) == ("25140", "final")
    assert [v["value"] for v in late["vintages"]] == ["25000", "25140"]
    assert late["definition"]["label"] == "Grand total" and late["unit"] == {"unit": "persons"}
    cited = {c["release_id"] for c in autumn["citations"]}
    assert all(f["citation"]["release_id"] in cited for f in all_figures(autumn))

    # A suppressed value keeps its code; ETER's confidential and missing values keep theirs.
    finance = figures(queries.institution(h.NS, scopes=h.READ_ONLY, scheme="ipeds-unitid", code="100002"),
                      "finance")[("FY2098", "ipeds", "F1D01")]
    assert (finance["status"], finance["special_code"], finance["value"]) == ("suppressed", "-2", None)
    eter = queries.institution(h.NS, scopes=h.READ_ONLY, scheme="eter-id", code="DE0002")
    assert {(w["status"], w["special_code"]) for w in eter["withheld"]} == {("confidential", "c"), ("missing", "m")}
    assert eter["identity"]["ror_changes"][0]["kind"] == "successor"

    # Linked Science and Funding records through the confirmed ROR match; the institution statistics stand beside.
    ids = h.seed_science_and_funding(conn)
    links = EducationLinks(conn)
    for kind, record_id in (("funding_opportunity", ids["award"]), ("scholarly_work", ids["paper"])):
        link = links.link(h.NS, subject={"ror": R2}, target={"kind": kind, "record_id": record_id},
                          citation={"source": "fixture record", "locator": "ror field",
                                    "identifier": {"scheme": "ror", "value": R2}},
                          principal_id="analyst", scopes=h.SCOPES)
        assert link["basis"]["method"] == "confirmed-ror-match" and link["target_status"] == "resolved"
    profile = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R2)
    assert profile["status"] == "answered" and len(profile["linked_records"]) == 2
    assert {c["concept"] for c in profile["concepts"]} == {"enrolment", "staff", "finance"}
    # An institution with no statistics is none on record; one without Science or Funding records has no links.
    nothing = queries.institution(h.NS, scopes=h.READ_ONLY, scheme="ipeds-unitid", code="199999")
    assert nothing["status"] == "none_on_record" and nothing["linked_records"] == []
    assert queries.institution(h.NS, scopes=h.READ_ONLY, scheme="eter-id", code="FR0001")["linked_records"] == []

    # A country reaches each source side by side: UIS and OECD expenditure, never merged; notes quoted.
    germany = queries.country(h.NS, scopes=h.READ_ONLY, country="DEU", as_of="2099-12-31")
    expenditure = figures(germany, "education_expenditure")
    assert expenditure[("2096", "unesco-uis", "XGDP.5T8")]["value"] == "1.2"
    assert expenditure[("2096", "oecd-eag", "EXP_INST_GDP")]["value"] == "1.3"
    ratio = figures(germany, "enrolment_ratio")[("2097", "unesco-uis", "GER.5T8")]
    assert ratio["value"] == "74.6" and [v["value"] for v in ratio["vintages"]] == ["74", "74.6"]
    gerd = figures(germany, "rd_expenditure")[("2096", "eurostat-rd", "GERD_HES")]
    assert gerd["comparability_notes"][0]["text"] == "break in time series"
    assert gerd["unit"]["currency"] == "EUR" and gerd["value"] == "21000.5"
    assert forbidden_keys(germany) == [] and forbidden_keys(profile) == []

    # Re-ingestion adds nothing and a restarted monitor replays without duplicates.
    before = conn.execute("SELECT count(*) FROM edu_vintages").fetchone()[0]
    h.load_all(conn, revisions=True)
    assert conn.execute("SELECT count(*) FROM edu_vintages").fetchone()[0] == before
    monitor = EducationMonitor(conn)
    watch = monitor.create(h.NS, "fsu", watch={"ror": R1}, principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert "revised_value" in {n["kind"] for n in first["notifications"]}
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

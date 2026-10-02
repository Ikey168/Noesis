"""IP12 (#2643): offline country-to-poverty-and-inequality acceptance for the Society bundle's society.income.

One journey, no network: the pinned PIP, EU-SILC and OECD IDD fixtures (and their recorded revisions) run through
the real adapters and projector; a country resolves to a Geospatial place through reviewed identity; each source's
poverty and inequality figures come back side by side as of a release, with definitions, vintages and comparability
notes; the series history shows revisions and PPP revisions; Demographics and Labour links are cited with pinned
revisions; a subject with no records answers "none on record"; the exclusions and the minimisation decision hold.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_links import IncomeLinks
from src.kb.income_distribution_queries import IncomeQueries
from src.kb.income_distribution_records import PERSONAL_DATA_FIELDS, IncomeError
from src.kb.society_bundle import export_profile, income_profile
from tests.unit import demographics_harness as dh
from tests.unit import income_distribution_harness as h
from tests.unit import labour_harness as lh

SCOPES = h.SCOPES | {"knowledge:demographics:read", "knowledge:labour:read"}


@pytest.fixture()
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the acceptance journey must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_country_to_cited_poverty_and_inequality_figures_from_each_source(offline):
    conn = h.connection()
    # Acquisition (first releases, then the recorded later releases) through the real adapters.
    h.load_all(conn, revisions=True)
    dh.apply(conn, "eurostat", 0)
    lh.load_all(conn)
    places = h.register_places(conn, keys=("de",))

    # Reviewable identity: proposed by one principal, accepted by another; nothing auto-merged.
    identity = IncomeIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="analyst", scopes=SCOPES)
    assert {a["state"] for a in proposed["proposed"]} == {"proposed"}
    assert [u["code"] for u in proposed["unmatched"]] == ["ECA"]
    for assertion in proposed["proposed"]:
        identity.review(h.NS, assertion["assertion_id"], "accept", "published identifier", principal_id="reviewer",
                        scopes=SCOPES)
    related = identity.propose_related(h.NS, principal_id="analyst", scopes=SCOPES)["proposed"]
    assert related and all(a["evidence"]["merge"] is False for a in related)

    # Cross-pack links by basis, pinned to revisions on both sides.
    links = IncomeLinks(conn)
    links.link_demographics(h.NS, principal_id="svc", scopes=SCOPES)
    links.link_labour(h.NS, principal_id="svc", scopes=SCOPES)
    linked = [link for link in links.links(h.NS, scopes=h.READ_ONLY) if link["state"] == "linked"]
    assert {link["kind"] for link in linked} == {"denominator", "labour"}
    assert {link["basis"] for link in linked} >= {"shared-identifier", "accepted-match"}
    assert all(link["vintage_id"] and link["target"]["vintage_id"] for link in linked)

    # As-of answers: each source side by side, as released by the date, never combined.
    profile = income_profile(conn, h.NS, scopes=SCOPES, place_id=places["de"], as_of="2099-01-31")
    headcount = profile["answers"]["poverty_headcount"]
    assert {r["provider"] for r in headcount["results"]} == {"pip", "eurostat-silc", "oecd-idd"}
    assert headcount["combined_value"] is None
    lines = {(r["provider"], json.dumps(r["poverty_line"], sort_keys=True), r["ppp_base_year"])
             for r in headcount["results"]}
    assert len(lines) == len(headcount["results"]) == 4  # PIP 2017 and 2021 PPP, EU-SILC 60 %, OECD 50 %
    for row in headcount["results"]:
        assert row["definition"]["content"]["welfare_concept"] == row["welfare_concept"]
        assert row["citation"]["vintage_id"] and row["citation"]["as_of"] and row["citation"]["provider"]
        assert row["citation"]["live_verification"] == "unverified-live"
    silc = next(r for r in headcount["results"] if r["provider"] == "eurostat-silc")
    assert silc["vintage"]["release_at"] == "2098-06-10T11:00:00Z"
    later = income_profile(conn, h.NS, scopes=SCOPES, place_id=places["de"], as_of="2099-07-01")
    silc_later = next(r for r in later["answers"]["poverty_headcount"]["results"] if r["provider"] == "eurostat-silc")
    assert silc_later["vintage"]["release_at"] == "2099-05-12T11:00:00Z"

    # Revision history with comparability notes; unknown pairs said so.
    queries = IncomeQueries(conn)
    pip = h.series(conn, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    history = queries.series_history(h.NS, pip["series_id"], scopes=h.READ_ONLY)
    assert [v["release_label"] for v in history["vintages"]] == ["20980915_2017_01_02_PROD",
                                                                 "20990320_2017_02_02_PROD"]
    assert history["pairs"][0]["notes"][0]["kind"] == "ppp_revision"
    silc_history = queries.series_history(h.NS, silc["series_id"], scopes=h.READ_ONLY)
    assert silc_history["pairs"][0]["comparability"] == "comparability_unknown"

    # A subject with no records.
    nothing = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="poverty_headcount",
                                          area={"scheme": "iso3166-1-alpha3", "code": "ZZX"})
    assert nothing["results"] == [] and len(nothing["none_on_record"]) == 3

    # Cited evidence-bundle export: every figure with source, record revision and as-of time.
    bundle = export_profile(later, created_at_ms=h.SECOND_RETRIEVAL)
    assert not verify_bundle(bundle).errors
    cited = [o["payload"] for o in bundle["objects"] if o["payload"].get("kind") == "income-series-vintage"]
    assert cited and all(c["citation"]["source"] and c["citation"]["record_revision"] and c["citation"]["as_of"]
                         for c in cited)

    # Exclusions and the minimisation decision.
    text = json.dumps(later)
    assert not any(f'"{field}"' in text for field in PERSONAL_DATA_FIELDS)
    for forbidden in ("nowcast", "gap_filled", "blended_value", "harmonised_value"):
        assert f'"{forbidden}"' not in text
    assert "blending PIP, EU-SILC and OECD figures into one series" in later["exclusions"]
    item = json.loads(json.dumps(h.fetch("silc")[0][0]["income_item"]))
    item["observations"][0]["attributes"]["respondent_id"] = "R-1"
    header = h.fetch("silc")[0][0]["income_release"]
    result = h.store(conn).apply_release(h.NS, {**header, "file_sha256": "0" * 64}, [item], source_id="x",
                                         run_id="leak", principal_id="svc", scopes=h.SCOPES)
    assert result["rejected"][0]["code"] == "personal_data"
    with pytest.raises(IncomeError):
        queries.indicator_for_place(h.NS, scopes={"knowledge:income:read"}, concept="gini", place_id=places["de"])

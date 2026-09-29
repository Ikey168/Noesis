"""AF09: commodity-and-place series as of a date, with vintages, flags and revisions (#2358)."""

from __future__ import annotations

import pytest

from src.kb.agrifood_identity import AgrifoodIdentity
from src.kb.agrifood_queries import AgrifoodQueries, as_of_ms
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    assert item.later()["status"] == "complete"
    identity = AgrifoodIdentity(item.conn, now=item.tick)
    identity.register_places(principal_id="curator", scopes=h.ALL)
    for candidate in identity.propose(NS, principal_id="matcher", scopes=h.WRITE)["crosswalks"]:
        pair = {candidate["left"]["scheme"], candidate["right"]["scheme"]}
        if pair <= {"faostat-item", "nass-commodity", "psd-commodity"}:
            identity.review(NS, candidate["crosswalk_id"], "accept", "same crop", principal_id="rev", scopes=h.REVIEW)
    yield item
    item.conn.close()


def _by(series, provider, kind):
    return [s for s in series if s["provider"] == provider and s["measure"]["kind"] == kind]


def test_series_per_source_side_by_side_as_of_dates(env):
    queries = AgrifoodQueries(env.conn)
    answer = queries.series_as_of(NS, commodity="maize", place="United States", scopes=h.READ, as_of="2025-01-31")
    assert answer["status"] == "found" and answer["place"]["status"] == "resolved"
    assert set(answer["sources"]) == {"faostat", "nass-quickstats", "fas-psd"}  # never blended
    (fao,) = _by(answer["series"], "faostat", "production")
    assert fao["commodity"]["match"] == "query" and fao["unit"] == "t" and fao["period_type"] == "calendar-year"
    figure_2023 = next(o for o in fao["observations"] if o["period"] == "2023")
    # Before the March 2025 domain update, the December 2024 vintage is the one published.
    assert figure_2023["value"] == "389694460" and figure_2023["vintage"]["released_at"] == "2024-12-18"
    assert figure_2023["revised_after_as_of"] is True
    assert figure_2023["flag"]["code"] == "A" and figure_2023["citation"]["attribution"].startswith("Source: FAO")
    (nass,) = _by(answer["series"], "nass-quickstats", "production")
    assert nass["commodity"]["match"] == "equivalent" and nass["commodity"]["via"]
    assert nass["place"] == {"scheme": "iso3166-1", "code": "US", "label": "United States", "level": "national"}
    psd = _by(answer["series"], "fas-psd", "supply_distribution")
    assert {s["period_type"] for s in psd} == {"marketing-year"}
    (psd_prod,) = [s for s in psd if s["measure"]["element"] == "Production"]
    assert psd_prod["observations"] == [] and len(psd_prod["later_vintages"]) == 2  # PSD released after the date
    later = queries.series_as_of(NS, commodity="maize", place="fao-area:231", scopes=h.READ, as_of="2025-09-30")
    (fao_later,) = _by(later["series"], "faostat", "production")
    assert next(o for o in fao_later["observations"] if o["period"] == "2023")["value"] == "389667000"
    (psd_sep,) = [s for s in later["series"] if s["provider"] == "fas-psd" and s["measure"]["element"] == "Production"]
    assert {o["period"]: (o["estimate_type"], o["value"]) for o in psd_sep["observations"]} == {
        "2024": ("publisher-estimate", "377633"), "2025": ("publisher-projection", "425260")}
    october = queries.series_as_of(NS, commodity="psd-commodity:0440000", place="psd-country:US", scopes=h.READ,
                                   as_of="2025-10-31", measures=["supply_distribution"])
    (psd_oct,) = [s for s in october["series"] if s["measure"]["element"] == "Production"]
    assert next(o for o in psd_oct["observations"] if o["period"] == "2025")["value"] == "427000"
    early = queries.series_as_of(NS, commodity="faostat-item:56", place="fao-area:231", scopes=h.READ,
                                 as_of="2020-01-01")
    assert early["status"] == "no_vintage_as_of"


def test_missing_and_withheld_values_are_shown_as_such(env):
    queries = AgrifoodQueries(env.conn)
    county = queries.series_as_of(NS, commodity="corn", place="us-fips:19169", scopes=h.READ)
    (series,) = county["series"]
    (figure,) = series["observations"]
    assert figure["status"] == "withheld" and figure["value"] is None and figure["value_text"] == "(D)"
    price = queries.series_as_of(NS, commodity="faostat-item:56", place="fao-area:231", scopes=h.READ,
                                 measures=["producer_price"])
    (fao_price,) = [s for s in price["series"] if s["provider"] == "faostat"]
    missing = next(o for o in fao_price["observations"] if o["period"] == "2023")
    assert missing["status"] == "missing" and missing["value"] is None and missing["flag"]["label"] == "Missing value"
    none = queries.series_as_of(NS, commodity="soya beans", place="Germany", scopes=h.READ)
    assert none["status"] == "none_on_record" and "not a statement" in none["notice"]


def test_revision_history_lists_every_vintage_with_release_dates(env):
    queries = AgrifoodQueries(env.conn)
    answer = queries.series_as_of(NS, commodity="nass-commodity:CORN", place="iso3166-1:US", scopes=h.READ)
    (nass,) = _by(answer["series"], "nass-quickstats", "production")
    history = queries.revisions(NS, nass["series_id"], scopes=h.READ, period="2023")
    assert [f["value"] for f in history["figures"]["2023"]] == ["15340520000", "15341000000"]
    assert [f["vintage"]["released_at"][:10] for f in history["figures"]["2023"]] == ["2024-01-12", "2024-09-30"]
    changed = [c for comparison in history["comparisons"] for c in comparison["changes"]]
    assert [(c["period_key"], c["change"], c["delta"]) for c in changed] == [("2023", "value_revised", "480000")]
    assert as_of_ms("2024-01-12") > as_of_ms("2024-01-12T12:00:00")

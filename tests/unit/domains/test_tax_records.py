"""Tax transparency records: editions as vintages, model estimates kept apart, link-only public CbCR (#2889)."""

from __future__ import annotations

import copy
import json

import pytest

from src.kb.tax_records import (
    CONTRACT,
    TaxError,
    check_item,
    check_reference,
    readiness,
    register_schemas,
)
from src.kb.tax_store import TaxStore
from tests.unit import tax_harness as h


def _cbcr_item() -> dict:
    return copy.deepcopy(h.fetch("cbcr")[0][0]["tax_item"])


def _etr_item() -> dict:
    return copy.deepcopy(next(r["tax_item"] for page in h.fetch("rates") for r in page
                              if r["tax_item"]["nature"] == "model_estimate"))


def test_the_record_schema_validates_fixture_items_and_registers():
    from jsonschema import Draft7Validator

    schema = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())
    Draft7Validator.check_schema(schema)
    for name in h.SOURCES:
        for page in h.fetch(name):
            for record in page:
                assert not list(Draft7Validator(schema).iter_errors(record["tax_item"])), name
    estimate = _etr_item()
    estimate.pop("method")
    assert list(Draft7Validator(schema).iter_errors(estimate))
    conn = h.connection()
    assert register_schemas(conn, principal_id="svc", scopes={"knowledge:schema:register"})


def test_write_time_rules_refuse_relabelled_estimates_derived_values_and_personal_fields():
    check_item(_cbcr_item())
    check_item(_etr_item())
    relabelled = _etr_item()
    relabelled["nature"] = "published_statistic"
    with pytest.raises(TaxError, match="never relabelled"):
        check_item(relabelled)
    no_method = _etr_item()
    no_method["method"] = None
    with pytest.raises(TaxError, match="keeps its method"):
        check_item(no_method)
    estimated_statistic = _cbcr_item()
    estimated_statistic["observations"][0]["status"] = "estimated"
    with pytest.raises(TaxError, match="has status reported"):
        check_item(estimated_statistic)
    derived = _cbcr_item()
    derived["own_effective_tax_rate"] = "14.8"
    with pytest.raises(TaxError) as refused:
        check_item(derived)
    assert refused.value.code == "derived_value"
    personal = _etr_item()
    personal["billionaire_name"] = "A. Person"
    with pytest.raises(TaxError) as leaked:
        check_item(personal)
    assert leaked.value.code == "personal_data"
    no_partner = _cbcr_item()
    no_partner["counterpart"] = {"code": "not_applicable"}
    with pytest.raises(TaxError, match="partner-jurisdiction grouping"):
        check_item(no_partner)


def test_editions_append_vintages_published_statistics_reach_the_economics_storage_and_estimates_never_do():
    conn = h.connection()
    h.load_all(conn)
    first = h.series_by(conn, provider="oecd-cts-cbcr", jurisdiction="DEU", counterpart="_T", indicator="PBT")
    assert first["economic_series"] and first["nature"] == "published_statistic"
    etr = h.series_by(conn, provider="oecd-cts-rates", jurisdiction="NLD", indicator="EATR")
    assert etr["economic_series"] is None and etr["nature"] == "model_estimate"
    stored = {r[0] for r in conn.execute("SELECT DISTINCT series_id FROM dataset_observations").fetchall()}
    assert first["series_id"] in stored and etr["series_id"] not in stored
    estimates = {r[0] for r in conn.execute("SELECT series_id FROM tax_series WHERE nature='model_estimate'")
                 .fetchall()}
    assert estimates and not estimates & stored

    results = {name: h.apply(conn, name, revised=True, retrieved_at_ms=h.SECOND_RETRIEVAL) for name in h.REVISIONS}
    (cbcr,) = results["cbcr"]
    assert cbcr["status"] == "applied" and cbcr["removed_series"] == 1 and cbcr["edition"].startswith(
        "Corporate Tax Statistics 2025")
    cit, etr_release = results["rates"]
    assert cit["unchanged_series"] == 3 and cit["vintages"] == 0  # identical re-publication adds no vintage
    assert etr_release["vintages"] == 6
    store = TaxStore(conn, initialize=False)
    vintages = store.vintage_rows(h.NS, first["series_id"])
    assert [v["edition"][:31] for v in vintages] == ["Corporate Tax Statistics 2024 e", "Corporate Tax Statistics 2025 e"]
    revised = vintages[1]["changes"]["revised"]
    assert revised == [{"period": "2092", "before": {"value_text": "398765.4", "value": "398765.4",
                                                     "status": "reported",
                                                     "flags": {"CONF_STATUS": "F", "OBS_STATUS": "P"}},
                        "after": {"value_text": "399001.2", "value": "399001.2", "status": "reported",
                                  "flags": {"CONF_STATUS": "F", "OBS_STATUS": "A"}}}]
    assert vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    nld_eatr = store.vintage_rows(h.NS, etr["series_id"])
    assert "method_change" in nld_eatr[1]["changes"] and nld_eatr[1]["method"]["description"] != \
        nld_eatr[0]["method"]["description"]
    removed = h.series_by(conn, provider="oecd-cts-cbcr", jurisdiction="NLD", counterpart="FOR", indicator="EMP")
    assert removed["current_status"] == "removed"
    # As of a date between the editions, the 2024 edition is selected; afterwards the 2025 edition.
    early = store.values(h.NS, first["series_id"], as_of_ms=h.day_ms("2025-01-01"))
    late = store.values(h.NS, first["series_id"], as_of_ms=h.day_ms("2025-12-31"))
    assert early["citation"]["edition"].startswith("Corporate Tax Statistics 2024")
    assert late["citation"]["edition"].startswith("Corporate Tax Statistics 2025")
    before = store.values(h.NS, first["series_id"], as_of_ms=h.day_ms("2024-01-01"))
    assert before["status"] == "unavailable" and before["reason"] == "no_edition_by_as_of"
    # Re-applying an edition is unchanged; a stale edition after a newer one is refused.
    again = h.apply(conn, "cbcr", revised=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert again[0]["status"] == "unchanged"
    with pytest.raises(TaxError) as stale:
        TaxStore(conn).apply_release(h.NS, {**h.fetch("cbcr")[0][0]["tax_release"], "file_sha256": "0" * 64,
                                      "published_on": "2025-01-01"},
                                     [r["tax_item"] for r in h.fetch("cbcr")[0]], source_id="x", run_id="r",
                                     principal_id="svc", scopes={"operator"}, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert stale.value.code == "stale_release"


def test_an_edition_cannot_be_dated_after_its_retrieval_and_a_failure_reads_as_stale():
    conn = h.connection()
    records = h.fetch("offshore")[0]
    with pytest.raises(TaxError, match="dated after its retrieval"):
        TaxStore(conn).apply_release(h.NS, records[0]["tax_release"], [r["tax_item"] for r in records],
                                     source_id="eutax-offshore-wealth", run_id="r", principal_id="svc",
                                     scopes={"operator"}, retrieved_at_ms=h.day_ms("2024-01-01"))
    h.load_all(conn)
    store = TaxStore(conn)
    store.record_failure(h.NS, "eutax-offshore-wealth", code="source_unavailable", run_id="r2",
                         source_id="eutax-offshore-wealth", scopes={"operator"})
    state = store.provider_state(h.NS, "eutax-offshore-wealth")
    assert state["stale"] is True and state["reason"] == "source_unavailable"
    report = readiness(conn)
    assert report["providers"]["eutax-offshore-wealth"]["stale"] is True
    assert report["providers"]["tjn-cthi"]["access_decision"] == "not-implemented"
    assert report["providers"]["public-cbcr"]["access_decision"] == "reference-only"
    with pytest.raises(TaxError) as denied:
        store.apply_release(h.NS, records[0]["tax_release"], [], source_id=None, run_id="r", principal_id="x",
                            scopes={"knowledge:tax:read", "namespace:global:read"})
    assert denied.value.code == "unauthorized"


def test_public_cbcr_references_are_link_only_revisioned_and_refuse_figures():
    conn = h.connection()
    recorded = h.record_references(conn)
    assert {r["status"] for r in recorded["references"]} == {"recorded"} and recorded["status"] == "reference-only"
    store = TaxStore(conn, initialize=False)
    reports = store.company_reports(h.NS)
    assert len(reports) == 3 and all(r["status"] == "reference-only" and "not stored" in r["figures"]
                                     for r in reports)
    holdings = next(r for r in reports if r["company_name_as_published"] == "Exampla Holdings plc")
    assert holdings["identifiers"] == [{"scheme": "lei", "value": "213800EXAMPLAHOLDS95"}]
    assert holdings["reference_key"] == "tax-cbcr:report:exampla-holdings-plc:2025-01-01_2025-12-31"
    # The same references again are unchanged; a corrected URL is a new revision, the earlier one kept.
    assert {r["status"] for r in h.record_references(conn)["references"]} == {"unchanged"}
    corrected = {**h.references()[0], "report_url": holdings["report_url"].replace(".pdf", "-corrected.pdf")}
    (result,) = store.record_company_reports(h.NS, [corrected], principal_id="operator", scopes=h.SCOPES)["references"]
    assert result["status"] == "revised" and result["revision_id"].endswith(":r2")
    history = store.company_reports(h.NS, reference_key=holdings["reference_key"], history=True)
    assert [r["revision"] for r in history] == [1, 2]
    with_figures = {**h.references()[1], "profit_before_tax": "1000000"}
    with pytest.raises(TaxError) as refused:
        store.record_company_reports(h.NS, [h.references()[2], with_figures], principal_id="operator",
                                     scopes=h.SCOPES)
    assert refused.value.code == "reference_only"
    assert len(store.company_reports(h.NS)) == 3  # the whole call was refused
    early = {**h.references()[2], "fiscal_year": {"start": "2023-01-01", "end": "2023-12-31"}}
    with pytest.raises(TaxError, match="2024-06-22"):
        check_reference(early)
    with pytest.raises(TaxError, match="https"):
        check_reference({**h.references()[2], "report_url": "http://www.example.org/report.pdf"})

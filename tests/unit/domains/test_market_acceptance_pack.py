import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_market_acceptance_pack_distinguishes_fixture_live_and_human_evidence():
    pack = json.loads((ROOT / "config/market/acceptance_packs/fixture-pack.json").read_text())
    assert pack["status"] == "fixture_verified_only"
    assert [item["ticker"] for item in pack["representative_universe"]["issuers"]] == [
        "MSFT", "ORCL", "CRM", "ADBE", "NOW"
    ]
    assert pack["representative_universe"]["fixture_only"] is True
    assert {item["status"] for item in pack["checks"]} >= {"passed", "pending_human_review"}
    assert any(item["name"] == "five_company_historical_journey" for item in pack["checks"])
    assert any(item["name"] == "historical_brief_export_restart_rights" for item in pack["checks"])
    assert pack["live_source_pack"]["status"] == "live_partial"
    assert {row["provider"]: row["status"] for row in pack["live_source_pack"]["providers"]} == {
        "sec_edgar": "live_verified",
        "fred": "live_verified",
        "fmp": "live_incomplete",
    }
    assert "live" not in pack["status"]


def test_live_sec_statement_pack_is_separate_and_self_consistent():
    pack = json.loads(
        (ROOT / "config/market/acceptance_packs/live-sec-statements.json").read_text()
    )
    assert pack["evidence_kind"] == "live_provider"
    assert pack["provider"] == "sec_edgar"
    rows = pack["rows"]
    assert {row["ticker"] for row in rows} == {"MSFT", "ORCL", "CRM", "ADBE", "NOW"}
    assert pack["totals"]["filings"] == len(rows)
    assert pack["totals"]["matched"] == sum(row["matched"] for row in rows)
    consistent = all(
        row["status"] == "consistent" and row["mismatched"] == 0 for row in rows
    )
    assert (pack["status"] == "live_verified") == consistent
    # A live SEC pack says nothing about licensed price vendors or analysts.
    assert any("analyst" in item for item in pack["limitations"])


def test_live_sec_materials_pack_records_unavailable_licensed_sources():
    pack = json.loads(
        (ROOT / "config/market/acceptance_packs/live-sec-materials.json").read_text()
    )
    assert pack["evidence_kind"] == "live_provider"
    assert {row["ticker"] for row in pack["rows"]} == {"MSFT", "ORCL", "CRM", "ADBE", "NOW"}
    for row in pack["rows"]:
        assert set(row["unavailable"]) == {
            "earnings_call_transcript", "analyst_consensus", "institutional_holdings_13f"
        }
    assert (pack["status"] == "live_verified") == all(
        row["status"] == "acquired" and row["coverage"]["earnings_release"] > 0 for row in pack["rows"]
    )


def test_live_macro_pack_includes_authenticated_fred_vintage_and_calendar_without_secrets():
    path = ROOT / "config/market/acceptance_packs/live-macro-sources.json"
    raw = path.read_text()
    pack = json.loads(raw)
    fred = next(row for row in pack["providers"] if row["provider"] == "fred")

    assert fred["status"] == "live_verified"
    assert fred["diagnostics"] == []
    assert {row["preserved"]["vintage_basis"] for row in fred["series"]} == {
        "fred_current_realtime_period",
        "explicit_alfred_realtime_period",
    }
    assert all(row["observations"] > 0 for row in fred["series"])
    assert fred["release_calendar"]["release_ids"] == ["10"]
    assert fred["release_calendar"]["date_count"] > 0
    assert fred["release_calendar"]["release_time_precision"] == "date_only"
    assert "api_key" not in raw.casefold()


def test_live_fmp_pack_records_partial_scope_and_pending_rights_without_secrets():
    manifest_path = (
        ROOT / "config/market/acceptance_packs/live-fmp-sample-manifest.json"
    )
    evaluation_path = ROOT / "config/market/acceptance_packs/live-price-provider.json"
    raw = manifest_path.read_text()
    manifest = json.loads(raw)
    evaluation = json.loads(evaluation_path.read_text())

    assert manifest["evidence_kind"] == "live_provider"
    assert manifest["provider"] == "fmp"
    active = {
        row["ticker"]
        for row in manifest["instruments"]
        if row["identity_kind"] == "active"
    }
    assert active == {"MSFT", "ORCL", "CRM", "ADBE", "NOW"}
    assert sum(row["identity_kind"] == "delisted" for row in manifest["instruments"]) >= 2
    assert all(value == "pending" for value in manifest["rights"].values())
    assert any(
        row["coverage"]["prices"]["status"] == "available"
        for row in manifest["instruments"]
    )
    assert any(
        row["coverage"]["prices"]["status"] == "unavailable"
        for row in manifest["instruments"]
    )
    assert evaluation["status"] == "live_incomplete"
    checks = {row["name"]: row["status"] for row in evaluation["checks"]}
    assert checks["authorized_sample_access"] == "passed"
    assert checks["rights_answers_explicit"] == "failed"
    assert "apikey" not in raw.casefold()


def test_asset_class_index_never_promotes_fixture_only_expansions():
    index = json.loads(
        (ROOT / "config/market/acceptance_packs/asset-class-index.json").read_text()
    )
    rows = {row["asset_class"]: row for row in index["asset_classes"]}

    assert index["status"] == "partial"
    assert set(index["required_checks"]) == {
        "contract",
        "reference_calculation",
        "historical_replay",
        "entitlement",
        "live_source",
        "user_workflow",
    }
    assert rows["us_equities_eod"]["live_source_status"] == "incomplete"
    assert rows["economic_releases"]["live_source_status"] == "verified_for_recorded_sample"
    for asset_class in (
        "international_equities",
        "fixed_income",
        "fx_commodities",
        "options_derivatives",
        "digital_assets",
        "intraday",
    ):
        assert rows[asset_class]["fixture_status"] == "verified"
        assert rows[asset_class]["live_source_status"] == "credential_blocked"
        assert rows[asset_class]["user_workflow_status"] == "not_run"

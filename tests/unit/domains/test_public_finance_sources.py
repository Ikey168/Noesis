"""Public-finance acquisition: source contracts, documented formats, transport policy and runtime (#1919-#1958)."""

from __future__ import annotations

import json
from functools import partial

import pytest

from src.ingestion.public_finance_sources import (
    PROVIDER_CONTRACTS,
    PublicFinanceAdapter,
    PublicFinanceFormatError,
    parse_release,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import public_finance_harness as h


def _document(key: str, index: int = 0) -> dict:
    return h.source(key)["public_finance"]["documents"][index]


def test_every_provider_has_an_access_decision_and_imf_terms_are_decided_before_acquisition():
    decisions = {k: v["access_decision"] for k, v in PROVIDER_CONTRACTS.items()}
    assert decisions == {
        "bundeshaushalt": "unverified-live",
        "berlin-senfin": "unverified-live",
        "eu-fts": "unverified-live",
        "eu-budget-pages": "not-implemented",
        "eurostat-gfs": "unverified-live",
        "bundesrechnungshof": "not-implemented",
        "imf-gfs": "not-implemented",
    }
    assert PROVIDER_CONTRACTS["imf-gfs"]["terms_decision"].startswith("blocked")
    for contract in PROVIDER_CONTRACTS.values():
        assert contract["reason"] and contract["terms"] and contract["accounting_basis"]
    doc = (
        h.ROOT / "docs/roadmaps/economics-public-finance-source-audit.md"
    ).read_text()
    for name in (
        "Bundeshaushalt",
        "Berlin",
        "Financial Transparency System",
        "Eurostat",
        "Bundesrechnungshof",
        "IMF GFS",
        "Unavailable-access fallback",
        "_verify_",
    ):
        assert name in doc


def test_the_source_pack_declares_pinned_fixtures_that_replay_offline():
    manifest = h.manifest()
    # 1.2.0 added the public-finance sources; 1.3.0 adds the demographics feature's sources (#1914); 1.4.0 adds the
    # Funding development-finance feature's OECD CRS source (#1932) and keeps every earlier source verbatim.
    assert manifest["version"] == "1.4.0"
    sources = {
        s["source_id"]: s
        for s in manifest["sources"]
        if s["connector"] == "public-finance"
    }
    assert set(sources) == set(h.SOURCES.values())
    for item in sources.values():
        assert item["mapping"]["target_schema"] == "noesis-public-finance-record-v1"
        assert item["auth"] == {"kind": "none"} and item["health"]["required"] is False
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert report["valid"], report
    by_id = {s["source_id"]: s for s in report["sources"]}
    assert (
        by_id["bundeshaushalt-open-data"]["records"] == 12
    )  # three lines x four documents
    assert by_id["eurostat-government-finance"]["records"] == 1


def test_bundeshaushalt_keeps_the_hierarchy_amount_text_unit_and_blank_amounts_as_published():
    release = parse_release(
        "de-bundeshaushalt-csv",
        h.body("de_bund_2099_soll.csv").encode(),
        declared={},
        document=_document("bund"),
    )
    assert release["published_on"] == "2098-12-01" and release["item_count"] == 3
    grant, study, revenue = release["figures"]
    assert grant["codes"] == {"einzelplan": "98", "kapitel": "9801", "titel": "68101"}
    assert (
        grant["labels"]["titel"] == "Zuschüsse an Beispielträger"
        and grant["side"] == "expenditure"
    )
    assert (
        grant["amount_text"] == "125.000"
        and grant["amount"] == "125000"
        and grant["unit"] == "1.000 EUR"
    )
    assert (
        study["amount"] == "4250.5"
        and grant["figure_kind"] == "plan"
        and grant["plan_key"] == "haushaltsplan"
    )
    assert (
        revenue["side"] == "revenue"
        and revenue["amount_text"] == ""
        and revenue["amount"] is None
    )


def test_a_file_in_a_variant_shape_is_refused_never_partly_read():
    both = (
        h.body("de_bund_2099_soll.csv")
        .replace(";Soll\n", ";Soll;Ist\n")
        .replace(";125.000\n", ";125.000;1\n")
    )
    with pytest.raises(
        PublicFinanceFormatError, match="undeclared amount columns: Ist"
    ):
        parse_release(
            "de-bundeshaushalt-csv",
            both.encode(),
            declared={},
            document=_document("bund"),
        )
    for bad, message in (
        (
            h.body("de_bund_2099_soll.csv").replace("1.000 EUR", "1.000 USD"),
            "not a declared unit",
        ),
        (
            h.body("de_bund_2099_soll.csv").replace("# Stand: 01.12.2098\n", ""),
            "no publication date",
        ),
        (
            h.body("de_bund_2099_soll.csv").replace(";A;125.000", ";X;125.000"),
            "not documented",
        ),
        (
            h.body("de_bund_2099_soll.csv").replace("Titel;", "Tit;", 1),
            "missing columns",
        ),
    ):
        with pytest.raises(PublicFinanceFormatError, match=message):
            parse_release(
                "de-bundeshaushalt-csv",
                bad.encode(),
                declared={},
                document=_document("bund"),
            )
    berlin = h.body(h.BERLIN).replace(";2099;Ist;", ";2099;Nachtrag;")
    declared = h.source("berlin")["public_finance"]
    with pytest.raises(PublicFinanceFormatError, match="undeclared BetragTyp"):
        parse_release(
            "de-be-haushalt-csv",
            berlin.encode(),
            declared=declared,
            document=declared["documents"][0],
        )


def test_berlin_lines_carry_the_bereich_and_the_district_it_stands_for():
    declared = h.source("berlin")["public_finance"]
    release = parse_release(
        "de-be-haushalt-csv",
        h.body(h.BERLIN).encode(),
        declared=declared,
        document=declared["documents"][0],
    )
    main, _, mitte_plan, mitte_ist, fk = release["figures"]
    assert main["district"] is None and main["codes"]["bereich"] == "30"
    assert mitte_plan["district"] == {"bereich": "31", "code": "001", "name": "Mitte"}
    assert (mitte_plan["figure_kind"], mitte_ist["figure_kind"]) == ("plan", "outturn")
    assert mitte_ist["amount"] == "765432.10" and fk["district"]["code"] == "002"


def test_fts_rows_keep_beneficiary_line_programme_kind_and_use_last_modified():
    document = _document("fts")
    release = parse_release(
        "eu-fts-csv",
        h.body(h.FTS).encode(),
        declared={},
        document=document,
        headers={"Last-Modified": h.FTS_LAST_MODIFIED},
    )
    assert release["published_on"] == "2100-06-30" and release["item_count"] == 4
    first, second, third, paid = release["payments"]
    assert (
        first["budget_line"] == "99010201"
        and first["budget_line_name"] == "Fictional Research Programme"
    )
    assert (
        first["programme"] == "Fictional Horizon Programme"
        and first["payment_kind"] == "commitment"
    )
    assert (
        first["amount"] == "1250000.00"
        and first["beneficiary_identifiers"][0]["value"] == "DE999999999"
    )
    assert (
        paid["payment_kind"] == "payment"
        and paid["position_key"] == first["position_key"]
    )
    assert third["budget_line"] == "99030100"
    with pytest.raises(PublicFinanceFormatError, match="no publication date"):
        parse_release(
            "eu-fts-csv",
            h.body(h.FTS).encode(),
            declared={},
            document=document,
            headers={},
        )
    odd = h.body(h.FTS).replace(",Payment,", ",Reimbursement,")
    with pytest.raises(PublicFinanceFormatError, match="commitment or payment"):
        parse_release(
            "eu-fts-csv",
            odd.encode(),
            declared={},
            document=document,
            headers={"Last-Modified": h.FTS_LAST_MODIFIED},
        )


def test_fts_rows_without_a_position_key_stay_apart():
    text = h.body(h.FTS).replace("FTS-2099-000001", "").replace("FTS-2099-000002", "")
    release = parse_release(
        "eu-fts-csv",
        text.encode(),
        declared={},
        document=_document("fts"),
        headers={"Last-Modified": h.FTS_LAST_MODIFIED},
    )
    keys = [p["payment_key"] for p in release["payments"]]
    assert len(set(keys)) == 4


def test_gfs_goes_through_the_eurostat_connector_with_one_series_per_filtered_dimension_set():
    document = _document("gfs")
    release = parse_release(
        "eurostat-gfs-jsonstat",
        h.body(h.GFS_APRIL).encode(),
        declared={},
        document=document,
        url="https://ec.europa.eu/x",
    )
    series = release["series"]
    assert (
        series["series_id"]
        == "estat:gov_10a_main:DE:na_item=TE:sector=S13:unit=MIO_EUR"
    )
    assert (
        release["published_on"] == "2025-04-22"
        and series["metadata"]["vintage_basis"] == "eurostat_dataset_updated"
    )
    assert (
        "acquired_at_ms" not in series["metadata"]
    )  # the projection stamps retrieval time, not the parse
    other = {**document, "filters": {**document["filters"], "na_item": "TR"}}
    cube = h.body(h.GFS_APRIL).replace('"TE"', '"TR"')
    assert (
        parse_release(
            "eurostat-gfs-jsonstat", cube.encode(), declared={}, document=other, url="u"
        )["series"]["series_id"]
        != series["series_id"]
    )
    with pytest.raises(PublicFinanceFormatError, match="geography"):
        parse_release(
            "eurostat-gfs-jsonstat",
            h.body(h.GFS_APRIL).replace('"DE"', '"FR"').encode(),
            declared={},
            document=document,
            url="u",
        )
    undated = json.loads(h.body(h.GFS_APRIL))
    undated.pop("updated")
    with pytest.raises(PublicFinanceFormatError, match="updated timestamp"):
        parse_release(
            "eurostat-gfs-jsonstat",
            json.dumps(undated).encode(),
            declared={},
            document=document,
            url="u",
        )
    two = json.loads(h.body(h.GFS_APRIL))
    two["dimension"]["sector"]["category"]["index"] = {"S13": 0, "S1311": 1}
    two["size"][2] = 2
    unfiltered = {**document, "filters": {"unit": "MIO_EUR", "na_item": "TE"}}
    with pytest.raises(PublicFinanceFormatError, match="unfiltered dimensions"):
        parse_release(
            "eurostat-gfs-jsonstat",
            json.dumps(two).encode(),
            declared={},
            document=unfiltered,
            url="u",
        )


def test_the_eurostat_connector_default_identity_is_unchanged():
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    connector = EurostatConnector(http_get=lambda _u: h.body(h.GFS_APRIL))
    plain = next(
        iter(
            connector.discover(
                {"dataset": "gov_10a_main", "geography": "DE", "unit": "MIO_EUR"}
            )
        )
    )
    assert "series_key" not in plain.metadata and plain.metadata["filters"] == {
        "unit": "MIO_EUR"
    }
    (record,) = connector.parse(connector.fetch(plain))
    assert record.series_id == "estat:gov_10a_main:DE"
    with pytest.raises(ValueError):
        next(
            iter(
                connector.discover(
                    {"dataset": "x", "geography": "DE", "series_key": "other"}
                )
            )
        )


def test_the_adapter_uses_the_runtime_default_transport_and_refuses_other_hosts():
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    adapter = PublicFinanceAdapter(h.source("bund"))
    assert (
        isinstance(adapter.transport, partial)
        and adapter.transport.func is HTTPSPageAdapter._request
    )
    with pytest.raises(SourcePackError) as refused:
        h.fetch(
            "bund",
            0,
            "de_bund_2099_soll.csv",
            final_url="https://mirror.example/hh.csv",
        )
    assert refused.value.code == "network_policy"
    item = h.source("bund")
    item["public_finance"]["documents"][0]["url"] = "https://mirror.example/hh.csv"
    with pytest.raises(SourcePackError) as invalid:
        PublicFinanceAdapter(item)
    assert invalid.value.code == "invalid_manifest"


def test_http_failures_budgets_and_cursors_are_classified():
    adapter, _ = h.pages("bund", [(0, "de_bund_2099_soll.csv")])
    request = {"operation": "release", "parameters": {}, "limit": 2}
    with pytest.raises(SourcePackError) as small:
        adapter.fetch_page(request, cursor=None)
    assert small.value.code == "budget_exhausted"  # never a truncated release
    with pytest.raises(SourcePackError) as drift:
        adapter.fetch_page({**request, "limit": 100}, cursor="9")
    assert drift.value.code == "cursor_drift"
    with pytest.raises(SourcePackError) as asked:
        adapter.fetch_page({**request, "parameters": {"q": "x"}}, cursor=None)
    assert asked.value.code == "parameter_forbidden"
    for status, code in (
        (429, "rate_limited"),
        (503, "source_unavailable"),
        (404, "schema_drift"),
        (403, "authentication_failed"),
    ):
        adapter, item = h.pages("bund", [(0, "de_bund_2099_soll.csv")])
        adapter.transport = lambda **kw: {
            "status": status,
            "headers": {},
            "content": b"",
        }
        with pytest.raises(SourcePackError) as failed:
            adapter.fetch_page({**request, "limit": 100}, cursor=None)
        assert failed.value.code == code


def test_documents_are_paged_one_release_each_and_marked_as_fixture_evidence():
    adapter, _ = h.pages("bund", list(enumerate(h.BUND_FILES)))
    cursor, seen = None, []
    while True:
        page = adapter.fetch_page(
            {"operation": "release", "parameters": {}, "limit": 100}, cursor=cursor
        )
        seen.append(
            (
                page.receipt["document"],
                page.receipt["published_on"],
                page.receipt["evidence_origin"],
            )
        )
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [
        ("Haushaltsplan (Soll)", "2098-12-01", "fixture"),
        ("1. Nachtragshaushalt (Soll)", "2099-06-15", "fixture"),
        ("Ist (vorläufig)", "2100-01-20", "fixture"),
        ("Haushaltsrechnung (Ist)", "2100-04-30", "fixture"),
    ]


def test_the_source_pack_runtime_runs_a_budget_source_into_the_record_owner():
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore
    from src.kb.public_finance import PublicFinanceStore

    conn = h.connection()
    manifest = h.manifest()
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=1
    )
    clock = iter(range(10, 10_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    adapters = runtime.fixture_adapters(manifest["pack_id"], h.ROOT)
    for key in ("bund", "gfs"):
        source_id = h.SOURCES[key]
        runtime.accept_license(manifest["pack_id"], source_id, principal_id="operator")
        result = runtime.run(
            {
                "pack_id": manifest["pack_id"],
                "run_key": key,
                "operation": "release",
                "source_ids": [source_id],
                "max_results": 1000,
                "max_bytes": 20_000_000,
                "timeout_ms": 60_000,
            },
            principal_id="operator",
            adapters={source_id: adapters[source_id]},
            dns_resolver=lambda _host: ["8.8.8.8"],
        )
        assert result["status"] == "complete", result
    store = PublicFinanceStore(conn, initialize=False)
    releases = store.releases("global", provider="bundeshaushalt")
    assert [r["published_on"] for r in releases] == [
        "2098-12-01",
        "2099-06-15",
        "2100-01-20",
        "2100-04-30",
    ]
    assert {r["evidence_origin"] for r in releases} == {"fixture"}
    assert len(store.lines("global", scheme="de-bund-haushalt")) == 3
    assert (
        conn.execute("SELECT count(*) FROM economic_release_snapshots").fetchone()[0]
        == 1
    )
    conn.close()

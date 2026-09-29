"""BaFin and Bundesanzeiger acquisition through the real adapters (#2106, BF03-BF06)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domains.market.bafin_notices import BafinNoticeStore, current_per_chain
from src.ingestion.bafin_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    BafinNoticeAdapter,
    fixture_transport,
    parse_chain,
    parse_dealings,
    parse_voting_rights,
    replay_native_fixture,
)
from src.ingestion.source_pack_runtime import RuntimeAdapterFactory
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    validate_source_pack,
)
from tests.unit import bafin_harness as h

ROOT = Path(__file__).resolve().parents[3]


def declared(key):
    return h.fictional(key, list(h.STAGES[key].values())[0])["bafin"]


def document(key):
    return declared(key)["documents"][0]


def test_every_source_has_a_recorded_access_decision_and_is_unverified_live():
    implemented = {
        "bafin-voting-rights",
        "bafin-managers-transactions",
        "bundesanzeiger-short-positions",
        "bafin-company-database",
        "bafin-warnings-measures",
    }
    assert {
        p
        for p, c in PROVIDER_CONTRACTS.items()
        if c["access_decision"] == "unverified-live"
    } == implemented
    assert (
        PROVIDER_CONTRACTS["unternehmensregister"]["access_decision"]
        == "not-implemented"
    )
    assert PROVIDER_CONTRACTS["news-wires"]["access_decision"] == "not-implemented"
    assert all(LIVE_VERIFICATION[p]["status"] == "unverified-live" for p in implemented)
    audit = (
        ROOT / "docs/development/bafin-notices-evidence/source-audit.md"
    ).read_text()
    assert (
        "(verify)" in audit
        and "Bounded coverage" in audit
        and "public_and_acquired" in audit
    )


def test_the_production_pack_validates_and_replays_its_pinned_fixtures_offline():
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    assert manifest["pack_id"] == "bafin-capital-market-notices" and manifest[
        "domains"
    ] == ["market"]
    assert all(s["connector"] == "bafin-notices" for s in manifest["sources"])
    report = SourcePackConformance(ROOT).offline(manifest)
    assert report["valid"], report
    dealings = next(
        s
        for s in manifest["sources"]
        if s["source_id"] == "bafin-managers-transactions"
    )
    assert dealings["bafin"]["retention_policy"] == "withdraw-person-data"
    # The production issuer set is out of scope for every authored row: nothing fictional is stored for it.
    voting = next(
        s for s in manifest["sources"] if s["source_id"] == "bafin-voting-rights"
    )
    fixture = json.loads((ROOT / voting["fixture"]["path"]).read_text())
    assert replay_native_fixture(voting, fixture) == []


def test_voting_rights_keep_the_chain_in_order_and_the_basis_of_every_percentage():
    raw = (h.FIXTURES / "voting_rights_2026-04-20.csv").read_bytes()
    parsed = parse_voting_rights(raw, declared("voting"), document("voting"))
    assert parsed["counts"] == {"rows": 3, "out_of_scope": 1, "outside_window": 0}
    notices = {n["source"]["source_id"]: n for n in parsed["notices"]}
    corrected = notices["VR-2026-0007"]
    assert [m["name"] for m in corrected["chain"]] == [
        "Fiktiva Holding SE",
        "Fiktiva Beteiligungs GmbH",
        "Fiktiva Invest GmbH",
    ]
    assert corrected["chain"][2] == {
        "position": 3,
        "name": "Fiktiva Invest GmbH",
        "voting_rights_pct": "5.21",
        "instruments_pct": None,
        "total_pct": "5.21",
    }
    assert corrected["correction_of"] == {"source_id": "VR-2026-0001"}
    assert corrected["percentages"] == {
        "s33": "5.21",
        "s38_1_1": "0",
        "s38_1_2": None,
        "s38": "0",
        "s39": "5.21",
    }
    nordlicht = notices["VR-2026-0009"]
    assert (
        nordlicht["percentages"]["s38"] == "1"
        and nordlicht["percentages"]["s39"] == "4.05"
    )
    assert nordlicht["previous_percentages"] == {"s39": "2.95"}
    assert {c["statute"] for c in nordlicht["legal_basis"]} == {"WpHG"}
    assert (
        parse_chain("-") == []
        and parse_chain("A | 3,01 | - | 3,01")[0]["total_pct"] == "3.01"
    )


def test_rows_without_their_own_values_never_inherit_them_and_bad_rows_are_rejected_not_dropped():
    header = (h.FIXTURES / "voting_rights_2026-03-10.csv").read_text().splitlines()[0]
    body = (
        header
        + "\n"
        + ";".join(
            [
                "VR-X",
                "Musterwerke AG",
                "DE000MSTR015",
                "",
                "Fiktiva Holding SE",
                "juristische Person",
                "",
                "",
                "5 %",
                "5,00",
                "",
                "",
                "",
                "5,00",
                "",
                "02.03.2026",
                "",
                "05.03.2026",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        + "\n"
        + ";".join(
            [
                "VR-Y",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        + "\n"
    )
    parsed = parse_voting_rights(body.encode(), declared("voting"), document("voting"))
    notice = parsed["notices"][0]
    # An invalid ISIN keeps its raw text and the issuer is in scope by its declared name.
    assert (
        notice["issuer"]["isin"] == h.ISSUER
        and notice["issuer"]["isin_stated"] == "DE000MSTR015"
    )
    assert notice["native"] == {"issuer_isin_basis": "bounded-set-name"}
    assert notice["chain"] == [] and notice["event_date"] == "2026-03-02"
    assert parsed["rejected"] == [
        {
            "row": 2,
            "source_id": "VR-Y",
            "code": "invalid_row",
            "reason": "row names no notifier",
        }
    ]


def test_aggregated_dealings_keep_every_trade_and_the_aggregate_as_published():
    raw = (h.FIXTURES / "dealings_2026-04-10.csv").read_bytes()
    parsed = parse_dealings(raw, declared("dealings"), document("dealings"))
    assert parsed["counts"]["out_of_scope"] == 1
    notice = next(
        n for n in parsed["notices"] if n["source"]["source_id"] == "DD-2026-0101"
    )
    assert [(t["price"], t["volume"]) for t in notice["trades"]] == [
        ("12.5", "1000"),
        ("12.6", "500"),
    ]
    assert notice["aggregate"] == {
        "price": "12.5333",
        "volume": "1500",
        "currency": "EUR",
    }
    assert notice["transaction_date"] == "2026-03-10" and notice["venue"] == "XETRA"
    assert notice["person"] == {
        "name": "Dr. Erika Musterfrau",
        "kind": "natural_person",
        "role": "Vorstand (Vorsitzende)",
        "closely_associated": False,
    }
    assert notice["instrument"] == {"type": "Aktie", "isin": h.ISSUER}
    assert any(c.get("celex") == "32014R0596" for c in notice["legal_basis"])


def test_rows_sharing_an_id_but_naming_different_people_are_rejected():
    lines = (h.FIXTURES / "dealings_2026-04-10.csv").read_text().splitlines()
    lines[2] = lines[2].replace("Dr. Erika Musterfrau", "Someone Else")
    parsed = parse_dealings(
        ("\n".join(lines) + "\n").encode(), declared("dealings"), document("dealings")
    )
    assert "DD-2026-0101" not in {n["source"]["source_id"] for n in parsed["notices"]}
    assert parsed["rejected"][0]["row"] == [1, 2]


def test_short_positions_record_publication_end_and_the_current_listing():
    conn = h.connection()
    receipts = h.acquire(conn, "shorts", "2026-04-10")
    assert (
        receipts[0]["listing"]["complete"] is False
        and receipts[1]["listing"]["complete"] is True
    )
    assert receipts[0]["counts"]["out_of_scope"] == 1
    h.acquire(conn, "shorts", "2026-05-01")
    views = BafinNoticeStore(conn).visible(h.NS, kinds=("net_short_position",))[
        "notices"
    ]
    ended = [v for v in views if v["notice"]["publication_ended"]]
    assert [
        (v["notice"]["position_pct"], v["notice"]["position_date"]) for v in ended
    ] == [("0.48", "2026-04-20")]
    assert all(v["publication_basis"] == "first-observed" for v in views)


def test_company_database_changes_are_revisions_bounded_to_declared_ids():
    conn = h.connection()
    receipts = h.acquire(conn, "company", "2026-04-10")
    assert (
        receipts[0]["counts"]["out_of_scope"] == 1
    )  # Beispiel Bank AG is not declared
    h.acquire(conn, "company", "2026-07-01")
    store = BafinNoticeStore(conn)
    view = store.visible(h.NS, kinds=("authorised_entity",))["notices"][0]
    history = store.history(h.NS, view["notice_id"])
    assert [r["change"] for r in history] == ["new", "revised"]
    assert {(lic["type"], lic["end"]) for lic in view["notice"]["licences"]} == {
        ("Anlageberatung", "2026-06-30"),
        ("Finanzportfolioverwaltung", None),
    }


def test_warnings_keep_entity_strings_legal_basis_and_same_host_links_only():
    conn = h.connection()
    receipts = h.acquire(conn, "warnings", "2026-05-15")
    assert all(
        r["listing"]["complete"] is False for r in receipts
    )  # RSS is never a complete listing
    views = {
        v["notice"]["title"]: v["notice"]
        for v in BafinNoticeStore(conn).visible(h.NS)["notices"]
    }
    warning = views["Warnung vor der Fiktiva Invest GmbH: Identitätsmissbrauch"]
    assert warning["named_entities"] == ["Fiktiva Invest GmbH"]
    assert [(c["statute"], c["path"]) for c in warning["legal_basis"]] == [
        ("KWG", "§37/abs4")
    ]
    sonnenschein = views["Warnung vor der Sonnenschein Trading Ltd"]
    assert sonnenschein["url"].startswith(
        "https://www.bafin.de/"
    )  # http link on the same host, upgraded
    general = views["Verbraucherhinweis: Vorsicht bei Anlageangeboten"]
    assert general["named_entities"] == [] and "named_entities" in general["unknowns"]
    measure = views["Maßnahme gegen die Beispiel Treuhand GmbH: Bußgeld"]
    assert {c.get("celex") for c in measure["legal_basis"]} >= {"32014R0596"}
    assert ("WpHG", "§120/abs12") in {
        (c.get("statute"), c.get("path")) for c in measure["legal_basis"]
    }


def test_the_adapter_uses_the_runtime_transport_and_refuses_other_hosts_and_bad_declarations():
    source = h.fictional("voting", ["voting_rights_2026-03-10.csv"])
    compiled = RuntimeAdapterFactory().compile(source)
    assert isinstance(compiled, BafinNoticeAdapter)
    assert (
        compiled.transport.func.__qualname__ == "HTTPSPageAdapter._request"
    )  # same-host redirects only
    pages = h.pages(["voting_rights_2026-03-10.csv"])
    pages[0]["final_url"] = "https://evil.example/fixture/voting_rights_2026-03-10.csv"
    adapter = BafinNoticeAdapter(source, transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 10}, cursor=None
        )
    assert caught.value.code == "network_policy"
    html = h.pages(["voting_rights_2026-03-10.csv"])
    html[0]["body"] = "<html><body>Sitzung abgelaufen</body></html>"
    with pytest.raises(SourcePackError) as caught:
        BafinNoticeAdapter(source, transport=fixture_transport(html)).fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 10}, cursor=None
        )
    assert caught.value.code == "schema_drift"
    later = h.fictional("voting", ["voting_rights_2026-04-20.csv"])
    with pytest.raises(SourcePackError) as caught:
        BafinNoticeAdapter(
            later,
            transport=fixture_transport(h.pages(["voting_rights_2026-04-20.csv"])),
        ).fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 1}, cursor=None
        )
    assert caught.value.code == "budget_exhausted"  # never a truncated listing
    wrong_host = h.fictional("voting", ["voting_rights_2026-03-10.csv"])
    wrong_host["bafin"]["documents"][0]["url"] = (
        "https://www.bundesanzeiger.de/elsewhere.csv"
    )
    with pytest.raises(SourcePackError):
        BafinNoticeAdapter(h._rehash(wrong_host))
    rss = h.fictional("warnings", ["warnings.rss"])
    rss["bafin"]["documents"][0]["listing"] = "complete"
    with pytest.raises(SourcePackError, match="never a complete listing"):
        BafinNoticeAdapter(h._rehash(rss))
    bad_isin = h.fictional("voting", ["voting_rights_2026-03-10.csv"])
    bad_isin["bafin"]["issuers"] = [{"isin": "DE000MSTR015", "name": "Musterwerke AG"}]
    with pytest.raises(SourcePackError, match="check digits"):
        BafinNoticeAdapter(h._rehash(bad_isin))


def test_rejected_rows_reach_the_runtime_as_rejections_to_quarantine():
    header = (
        (h.FIXTURES / "short_positions_current_2026-04-10.csv")
        .read_text()
        .splitlines()[0]
    )
    pages = [
        {
            "request": "/fixture/short_positions_current_2026-04-10.csv",
            "status": 200,
            "body": header + "\n;Musterwerke AG;DE000MSTR014;0,70;01.04.2026\n",
        }
    ]
    source = h.fictional("shorts", ["short_positions_current_2026-04-10.csv"])
    page = BafinNoticeAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 10}, cursor=None
    )
    assert [r["rejection"]["code"] for r in page.records] == ["invalid_row"]
    assert page.receipt["counts"]["rejected"] == 1


def test_reacquiring_unchanged_exports_adds_nothing():
    conn = h.connection()
    h.acquire_all(conn)
    before = conn.execute("SELECT count(*) FROM bafin_notice_revisions").fetchone()[0]
    listing = conn.execute(
        "SELECT count(*) FROM bafin_listing_observations"
    ).fetchone()[0]
    for key, stages in h.STAGES.items():
        h.acquire(conn, key, sorted(stages)[-1], run_id=f"again:{key}")
    assert (
        conn.execute("SELECT count(*) FROM bafin_notice_revisions").fetchone()[0]
        == before
    )
    assert (
        conn.execute("SELECT count(*) FROM bafin_listing_observations").fetchone()[0]
        == listing
    )
    assert (
        len(
            current_per_chain(
                BafinNoticeStore(conn).visible(
                    h.NS, kinds=("voting_rights_notification",)
                )["notices"]
            )
        )
        == 2
    )


def test_a_notice_the_store_would_refuse_is_a_rejection_and_the_rest_of_the_page_is_kept():
    text = (h.FIXTURES / "voting_rights_2026-04-20.csv").read_text()
    text = text.replace(
        ";3 %;3,05;1,00;", ";3 %;150,00;1,00;"
    )  # an impossible stated percentage
    source = h.fictional("voting", ["bad.csv"])
    pages = [{"request": "/fixture/bad.csv", "status": 200, "body": text}]
    page = BafinNoticeAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    notices = [
        r["bafin_notice"]["source"]["source_id"]
        for r in page.records
        if r.get("bafin_notice")
    ]
    rejections = [r["rejection"] for r in page.records if r.get("rejection")]
    assert notices == ["VR-2026-0007"]
    assert (
        rejections[0]["code"] == "invalid_notice"
        and "percentage" in rejections[0]["reason"]
    )


def test_an_unidentified_rejected_row_keeps_a_complete_listing_from_reading_as_removals():
    conn = h.connection()
    h.acquire(conn, "shorts", "2026-04-10")
    header = (
        (h.FIXTURES / "short_positions_current_2026-05-01.csv")
        .read_text()
        .splitlines()[0]
    )
    # The current list now has an unreadable row: it could be Kurzfrist's position, so nothing is marked.
    body = (
        header
        + "\nZeitwert Partners Ltd;Musterwerke AG;DE000MSTR014;0,71;05.04.2026\n;Musterwerke AG;;;\n"
    )
    source = h.fictional("shorts", ["short_positions_current_2026-05-01.csv"])
    pages = [
        {
            "request": "/fixture/short_positions_current_2026-05-01.csv",
            "status": 200,
            "body": body,
        }
    ]
    page = BafinNoticeAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    assert page.receipt["listing"]["complete"] is False
    assert (
        "rejected without a stated identifier"
        in page.receipt["listing"]["incomplete_reason"]
    )
    from src.domains.market.bafin_notices import BafinNoticeProjector

    BafinNoticeProjector(conn).project_page(
        run_id="partial",
        manifest={},
        source=source,
        records=page.records,
        documents=[{"ingested_at": h.ms("2026-05-01")}],
        page_receipt=dict(page.receipt),
        principal_id=h.PRINCIPAL,
    )
    assert (
        conn.execute("SELECT count(*) FROM bafin_listing_observations").fetchone()[0]
        == 0
    )

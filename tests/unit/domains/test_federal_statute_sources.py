"""Federal law source adapters and parsers (#2105, FL01/FL03-FL05): fictional fixtures, real transports, no network."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.federal_law_formats import (
    FormatError,
    bgbl_key_from_eli,
    parse_bgbl_act,
    parse_bgbl_feed,
    parse_gii_statute,
    parse_gii_toc,
    parse_legaldocml_statute,
    parse_ris_search,
)
from src.ingestion.legal_sources import (
    PROVIDER_CONTRACTS,
    BgblActAdapter,
    GiiStatuteAdapter,
    fixture_transport,
    legal_declaration,
)
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import federal_statutes_harness as h

F = h.FIXTURES


def test_audit_records_every_source_decision_and_the_validity_semantics():
    audit = (
        h.ROOT / "docs/development/federal-statutes-evidence/source-audit.md"
    ).read_text()
    for name in (
        "gesetze-im-internet.de",
        "rechtsinformationen.bund.de",
        "recht.bund.de",
        "bgbl.de",
        "Bundestag DIP",
        "link-only",
        "not implemented",
        "source_stated",
        "observed",
        "verify",
    ):
        assert name in audit, name
    for statute in ("BGB", "HGB", "GmbHG", "AktG", "WpHG", "KWG", "VwVfG", "GG"):
        assert f"`{statute}`" in audit
    assert {
        c["validity"].split(":")[0]
        for c in PROVIDER_CONTRACTS.values()
        if "validity" in c
    } == {
        "observed",
        "source_stated",
        "promulgation date and the entry-into-force article as published; instructions "
        "never applied",
    }


def test_gii_statute_parses_provisions_stand_and_footnotes_without_inventing_validity():
    record = parse_gii_statute(
        (F / "gii_mphg_2030-06-01.xml").read_bytes(),
        source_url="https://x/mphg/xml.zip",
        jurabk="MPHG",
    )
    fields = record["fields"]
    assert fields["validity_basis"] == "observed" and "validity_from" not in fields
    assert fields["stand"] == [
        {
            "type": "Stand",
            "comment": "Zuletzt geändert durch Art. 1 G v. 12.3.2030 I Nr. 45",
            "checked": "ja",
        }
    ]
    assert (
        fields["ausfertigung_date"] == "2029-05-04" and record["is_current_law"] is None
    )
    paths = [s["locator"]["path"] for s in record["sections"]]
    assert {
        "§1/abs1",
        "§1/abs2",
        "§5/abs1",
        "§5/abs2",
        "§8",
        "§8a",
        "gliederung/010",
    } <= set(paths)
    assert any(
        s["locator"]["kind"] == "statute-footnote" and "Umsetzung" in s["text"]
        for s in record["sections"]
    )
    assert "None" not in json.dumps(record, ensure_ascii=False).replace("null", "")
    with pytest.raises(FormatError) as caught:
        parse_gii_statute(
            (F / "gii_mphg_2030-06-01.xml").read_bytes(), source_url="x", jurabk="BGB"
        )
    assert caught.value.code == "source_identity"


def test_gii_toc_and_ris_search_select_only_exact_statutes():
    toc = parse_gii_toc((F / "gii_toc.xml").read_bytes())
    assert [t["path"] for t in toc] == ["mphg", "mbg"]
    expressions = parse_ris_search(
        (F / "ris_search_mphg.json").read_bytes(), jurabk="MPHG"
    )
    assert [e["temporal_coverage"] for e in expressions] == [
        "2029-06-01/2030-03-31",
        "2030-04-01/2030-12-31",
    ]
    assert (
        parse_ris_search(
            (F / "ris_search_mphg.json").read_bytes(),
            jurabk="MPHG",
            eli_work="eli/bund/bgbl-1/2099/1",
        )
        == []
    )
    open_ended = parse_ris_search(
        json.dumps(
            {
                "member": [
                    {
                        "item": {
                            "abbreviation": "X",
                            "legislationIdentifier": "eli/w",
                            "workExample": {
                                "legislationIdentifier": "eli/w/e",
                                "temporalCoverage": "2030-01-01/..",
                                "encoding": [
                                    {
                                        "contentUrl": "/x.xml",
                                        "encodingFormat": "application/xml",
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        ).encode(),
        jurabk="X",
    )
    assert (
        open_ended[0]["validity_to"] is None and open_ended[0]["date_modified"] is None
    )


def test_legaldocml_maps_to_the_same_provision_paths_and_scopes_plain_paragraph_headings():
    expression = parse_ris_search(
        (F / "ris_search_mphg.json").read_bytes(), jurabk="MPHG"
    )[1]
    record = parse_legaldocml_statute(
        (F / "ris_mphg_2030-04-01.xml").read_bytes(),
        expression=expression,
        jurabk="MPHG",
        source_url="x",
    )
    by_path = {s["locator"]["path"]: s["text"] for s in record["sections"]}
    # § 8a is written as plain paragraphs after the structured § 8: it opens its own provision.
    assert (
        by_path["§8"] == "Die Aufsichtsstelle überwacht die Einhaltung dieses Gesetzes."
    )
    assert by_path["§8a"].startswith("Für Erwerbe vor dem 1. April 2030")
    assert record["fields"]["validity_basis"] == "source_stated"
    assert (record["fields"]["validity_from"], record["fields"]["validity_to"]) == (
        "2030-04-01",
        "2030-12-31",
    )
    assert record["fields"]["amended_by"] == ["bgbl-1/2030/nr-45"]
    other = dict(expression, eli_expression="eli/bund/bgbl-1/2029/101/2031-01-01/1/deu")
    with pytest.raises(FormatError):
        parse_legaldocml_statute(
            (F / "ris_mphg_2030-04-01.xml").read_bytes(),
            expression=other,
            jurabk="MPHG",
            source_url="x",
        )


def test_bgbl_act_instructions_entry_into_force_and_statements():
    act = parse_bgbl_act(
        (F / "bgbl_2030_45.xml").read_bytes(),
        key="bgbl-1/2030/nr-45",
        statutes=[h.MPHG],
        source_url="x",
    )
    fields = act["fields"]
    assert (
        fields["bgbl_citation"] == "BGBl. 2030 I Nr. 45"
        and fields["promulgation_date"] == "2030-03-14"
    )
    assert (
        fields["entry_into_force"][0]["text"]
        == "Dieses Gesetz tritt am 1. April 2030 in Kraft."
    )
    assert [
        (a["item"], a["status"], a["provision"], a["action"])
        for a in fields["amendments"]
    ] == [
        ("1.", "ambiguous", None, "insert"),
        ("2.", "resolved", "§5", "amend"),
        ("2. a)", "resolved", "§5/abs2", "replace"),
        ("3.", "resolved", "§8a", "insert"),
        ("1.", "statute_not_in_set", None, "repeal"),
    ]
    assert all(a["instruction"] and a["locator"]["path"] for a in fields["amendments"])
    assert fields["touched_statutes"] == ["mphg"]
    assert any(s["locator"]["kind"] == "act-note" for s in act["sections"])
    with pytest.raises(FormatError) as caught:
        parse_bgbl_act(
            (F / "bgbl_2030_45.xml").read_bytes(),
            key="bgbl-1/2030/nr-46",
            statutes=[h.MPHG],
            source_url="x",
        )
    assert caught.value.code == "source_identity"
    listing = parse_bgbl_feed((F / "bgbl_listing.xml").read_bytes())
    assert [e["key"] for e in listing] == [
        "bgbl-1/2030/nr-45",
        "bgbl-1/2030/nr-46",
        "bgbl-1/2022/nr-999",
    ]
    assert bgbl_key_from_eli("eli/bund/bgbl-1/2002/s42/x") == "bgbl-1/2002/s-42"


def test_gii_adapter_pages_one_statute_per_page_with_outcomes():
    item = h.fictional("gii")
    item["legal"]["selection"]["statutes"].append(
        {"jurabk": "ABCG", "gii_path": "abcg"}
    )
    records, receipts = h.drain("gii", h.gii_pages("2030-03-01"), item)
    assert (
        len(records) == 1
        and records[0]["legal_record"]["provider"] == "gesetze-im-internet"
    )
    assert [r.get("outcome") for r in receipts] == [None, "returned", "not_found"]
    assert receipts[2]["reason"] == "not_in_toc"


def test_federal_adapters_fetch_only_from_their_declared_host():
    pages = h.gii_pages("2030-03-01")
    pages[0]["body"] = pages[0]["body"].replace(
        "http://www.gesetze-im-internet.de/mphg", "https://mirror.example.org/mphg"
    )
    with pytest.raises(SourcePackError) as caught:
        h.drain("gii", pages)
    assert caught.value.code == "network_policy"
    item = h.fictional("bgbl")
    item["legal"]["selection"]["document_path"] = (
        "//evil.example.org/{part}/{year}/{number}.xml"
    )
    with pytest.raises(SourcePackError) as refused:
        h.drain("bgbl", h.bgbl_pages(), item)
    assert refused.value.code == "network_policy"


def test_default_transport_refuses_cross_host_redirects():
    from src.ingestion.source_pack_runtime import _validate_redirect

    adapter = GiiStatuteAdapter(h.fictional("gii"))
    assert (
        adapter.transport.func is HTTPSPageAdapter._request
    )  # the runtime's same-host redirect transport
    with pytest.raises(SourcePackError):
        _validate_redirect(
            "https://www.gesetze-im-internet.de/gii-toc.xml",
            "https://evil.example.org/gii-toc.xml",
        )


def test_bgbl_adapter_acquires_only_acts_touching_the_statute_set_and_keeps_history_link_only():
    records, receipts = h.drain("bgbl", h.bgbl_pages())
    assert [r["legal_record"]["provider_id"] for r in records] == ["bgbl-1/2030/nr-45"]
    assert receipts[0]["index"]["link_only_historical"] == ["bgbl-1/2022/nr-999"]
    assert [r.get("outcome") for r in receipts[1:]] == ["returned", "seen_not_acquired"]
    item = h.fictional("bgbl")
    item["legal"]["selection"]["items"] = [{"part": 1, "year": 2022, "number": 999}]
    with pytest.raises(SourcePackError) as caught:
        legal_declaration(item)
    assert "link-only" in str(caught.value)


@pytest.mark.parametrize("key", ["gii", "ris", "bgbl"])
def test_declarations_are_bounded(key):
    item = h.fictional(key)
    item["legal"]["selection"]["statutes"] = [{"jurabk": f"S{i}G"} for i in range(21)]
    with pytest.raises(SourcePackError):
        legal_declaration(item)


def test_ris_adapter_returns_source_stated_versions_and_missing_expressions_are_skipped():
    pages = h.ris_pages()
    records, receipts = h.drain("ris", pages)
    assert [r["legal_record"]["fields"]["validity_from"] for r in records] == [
        "2029-06-01",
        "2030-04-01",
    ]
    missing = copy.deepcopy(pages)
    missing[-1]["status"] = 404
    records, receipts = h.drain("ris", missing)
    assert len(records) == 1 and receipts[0]["expressions"][1]["status"] == 404


def test_pinned_production_fixtures_replay_offline_to_their_hashes():
    value = json.loads(h.PACK.read_text())
    value["sources"] = [
        s
        for s in value["sources"]
        if s["source_id"] in h.SOURCE_IDS.values() and s["connector"] != "rii"
    ]
    result = SourcePackConformance(h.ROOT).offline(value)
    assert result["valid"] and {s["records"] for s in result["sources"]} == {0}
    bgbl = next(s for s in value["sources"] if s["connector"] == "recht-bund")
    adapter = BgblActAdapter(
        json.loads(
            json.dumps(
                {
                    **bgbl,
                    "operations": ["records"],
                    "source_hash": "x",
                    "mapping": {},
                    "extractor_versions": [],
                }
            )
        ),
        transport=fixture_transport(
            json.loads((h.ROOT / bgbl["fixture"]["path"]).read_text())["native_pages"]
        ),
    )
    first = adapter.fetch_page(
        {"operation": "records", "parameters": {}, "limit": 10}, cursor=None
    )
    assert first.receipt["queue_size"] == 2

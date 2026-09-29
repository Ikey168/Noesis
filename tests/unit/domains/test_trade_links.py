"""Trade flows linked to sanctions measures and ownership records by citation only (#2548)."""

from __future__ import annotations

import json

import pytest

from src.kb.sanctions_trade import SanctionsTrade
from src.kb.trade_flows import TradeError, TradeFlowStore, forbidden_keys
from tests.unit import trade_harness as h

OWNERSHIP = h.SCOPES | {"knowledge:ownership:read"}
AREA_TABLE = {
    "table_id": "fixture-area-measure",
    "title": "Authored measure citing HS 2022 codes for one partner area",
    "source": {"citation": "Authored fixture standing in for a published annex (not a real measure)",
               "url": "https://eur-lex.europa.eu/", "published": "2099-01-01"},
    "rows": [
        {"control_code": "X.854143", "product_code": "854143", "product_scheme": "HS6",
         "classification_vintage": "HS2022", "partner_areas": ["156"]},
        {"control_code": "X.300215", "product_code": "300215", "product_scheme": "HS6",
         "classification_vintage": "HS2022", "partner_areas": ["156"]},
    ],
}


def record_measures(conn):
    trade = SanctionsTrade(conn)
    table = json.loads((h.ROOT / "tests/fixtures/sanctions/dual_use_cn_correlation.json").read_text())
    trade.record_correlations(h.NS, table, principal_id="op", scopes=h.SCOPES)
    trade.record_correlations(h.NS, AREA_TABLE, principal_id="op", scopes=h.SCOPES)


def test_a_missing_sanctions_provider_is_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    from src.kb.trade_links import TradeLinks

    result = TradeLinks(conn).link_sanctions(h.NS, principal_id="op", scopes=h.SCOPES)
    assert result["status"] == "provider_absent" and result["links"] == []


def test_sanctions_links_rest_on_cited_codes_and_areas_and_point_at_vintages():
    from src.kb.trade_links import TradeLinks

    conn = h.connection()
    h.load_all(conn, revisions=True)
    record_measures(conn)
    links = TradeLinks(conn)
    result = links.link_sanctions(h.NS, principal_id="op", scopes=h.SCOPES)
    assert result["status"] == "linked" and "lookup aid" in result["notice"]
    store = TradeFlowStore(conn)
    by_series = {}
    for link in result["links"]:
        by_series.setdefault(link["series_id"], []).append(link)
    described = {
        sid: (store.series(h.NS, sid)["provider"], store.series(h.NS, sid)["reporter"]["code"],
              store.series(h.NS, sid)["product"]["code"], store.series(h.NS, sid)["flow"]["direction"])
        for sid in by_series
    }
    # 1C350 (no area cited): Comtrade HS 293090 and Comext CN8 29309098, by the same code and by the CN structure.
    assert ("un-comtrade", "276", "293090", "export") in described.values()
    assert ("eurostat-comext", "DE", "29309098", "import") in described.values()
    comext = next(sid for sid, d in described.items() if d == ("eurostat-comext", "DE", "29309098", "import"))
    methods = {link["basis"]["product"]["method"] for link in by_series[comext]}
    assert methods == {"same-code", "cn8-within-cited-hs6"}
    assert all(link["basis"]["area"]["method"] == "none-cited" for link in by_series[comext])
    # The area measure cites China: DE-CN and CN-DE flows of 854143, and the HS2017 mirror via the concordance.
    area_links = [link for link in result["links"] if link["target"]["table_id"] == "fixture-area-measure"]
    assert area_links and {link["basis"]["area"]["cited"][0] for link in area_links} == {"156"}
    assert not any(described[link["series_id"]][0] == "eurostat-comext" for link in area_links)
    concordance = [link for link in area_links if link["basis"]["product"]["method"] == "concordance"]
    assert concordance and all(link["basis"]["product"]["exact"] is False for link in concordance)
    assert concordance[0]["basis"]["product"]["concordance"]["label"].startswith("WITS concordance HS 2022")
    assert concordance[0]["basis"]["product"]["flow_classification"]["vintage"] == "HS2017"
    # Links point at specific vintages: the revised German export series has one link per vintage.
    (revised,) = store.find_series(h.NS, reporter_codes=["276"], product_codes=["854143"], flow_direction="export")
    revised_links = [link for link in area_links if link["series_id"] == revised["series_id"]]
    assert len({link["vintage_id"] for link in revised_links}) == 2
    assert revised_links[0]["vintage_source_revision"]["provider"] == "un-comtrade"
    # A cited code with no acquired flow is reported, not dropped.
    assert [m["product_code"] for m in result["unmatched_measures"]] == ["300215"]
    assert forbidden_keys(result) == []
    again = links.link_sanctions(h.NS, principal_id="op", scopes=h.SCOPES)
    assert again["created"] == [] and len(again["links"]) == len(result["links"])
    assert set(links.sanctioned_series(h.NS, control_code="1c350")) >= {comext}


def test_ownership_links_need_an_explicit_citation_and_missing_targets_are_reported():
    from src.kb.ownership_store import OwnershipStore
    from src.kb.trade_links import TradeLinks

    conn = h.connection()
    h.apply(conn, "comtrade", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    (series,) = store.find_series(h.NS, reporter_codes=["276"], product_codes=["854143"], flow_direction="import")
    vintage = store.vintage_rows(h.NS, series["series_id"])[0]
    links = TradeLinks(conn)
    kwargs = dict(series_id=series["series_id"], vintage_id=vintage["vintage_id"], ownership_namespace="own",
                  record_id="own:entity:1", statement="The importer is named in the cited customs notice",
                  principal_id="alice")
    with pytest.raises(TradeError) as caught:
        links.link_ownership(h.NS, citation={"source": "customs notice"}, scopes=OWNERSHIP, **kwargs)
    assert caught.value.code == "citation_required"
    absent = links.link_ownership(h.NS, citation={"source": "customs notice", "locator": "p. 2"},
                                  scopes=OWNERSHIP, **kwargs)
    assert absent["target_status"] == "provider_absent" and absent["basis"]["method"] == "explicit-citation"
    OwnershipStore(conn)
    missing = links.link_ownership(h.NS, citation={"source": "customs notice", "locator": "p. 3"},
                                   scopes=OWNERSHIP, **kwargs)
    assert missing["target_status"] == "target_missing"
    conn.execute(
        "INSERT INTO ownership_records VALUES ('own', 'own:entity:1', 'legal_entity', 'k', 'companies-house', "
        "NULL, 1, NULL, 0)"
    )
    resolved = links.link_ownership(h.NS, citation={"source": "customs notice", "locator": "p. 4"},
                                    scopes=OWNERSHIP, **kwargs)
    assert resolved["target_status"] == "resolved" and resolved["vintage_id"] == vintage["vintage_id"]
    with pytest.raises(TradeError) as caught:
        links.link_ownership(h.NS, citation={"source": "x", "locator": "y"}, scopes=h.SCOPES, **kwargs)
    assert caught.value.code == "unauthorized"
    listed = links.links(h.NS, scopes=h.READ_ONLY, series_id=series["series_id"], kind="ownership")
    assert {link["target_status"] for link in listed} == {"provider_absent", "target_missing", "resolved"}
    withdrawn = links.withdraw(h.NS, resolved["link_id"], "cited notice corrected", principal_id="bob",
                               scopes=h.SCOPES)
    assert withdrawn["state"] == "withdrawn"
    assert len(links.links(h.NS, scopes=h.READ_ONLY, kind="ownership")) == 2

"""Offline multi-category lookup, match and compare acceptance for the Products expansion (PX11, #2103).

The display fixtures, two appliance groups (EPREL + Open Icecat overlap) and
one component category from the ``implement`` BMEcat sources run through the
real source-pack runtime, with the bundle's optional features off and on. Per
category the journey is lookup -> match proposal and review -> comparison
(with a refused cross-category comparison) -> datasheet citation. Offline
evidence only; live coverage is PX12 (#2104).
"""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.kb.products import (
    ProductError,
    ProductStore,
    acquire_documents,
    document_policy,
    readiness,
)
from tests.unit.domains import test_products_expansion_sources as expansion
from tests.unit.domains import test_products_pack as displays

SCOPES = displays.SCOPES
FEATURES = ["appliances", "components", "safety"]


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def deployment(features: list[str] | None):
    conn = duckdb.connect(":memory:")
    if features is not None:
        from tests.unit.composition.test_migration import _migrated

        _, coordinator, bundles, _ = _migrated(conn)
        coordinator.select(
            "products", bundles["products"]["version"], features=features
        )
        assert coordinator.activate("products-expansion")["status"] == "published"
    value, runtime = displays.install(conn)
    assert displays.run(runtime, value, "displays")["status"] == "complete"
    assert (
        expansion.run(runtime, value, "appliances", expansion.APPLIANCES, "models")[
            "status"
        ]
        == "complete"
    )
    assert (
        expansion.run(runtime, value, "components", expansion.COMPONENTS, "components")[
            "status"
        ]
        == "complete"
    )
    return conn, ProductStore(conn)


def model(store, provider, designation, category):
    return expansion.variant(store, provider, designation, category)["model_id"]


def accept(store, candidate, reason):
    return store.review_match(
        "global",
        candidate["match_id"],
        "accepted",
        reason,
        scopes=SCOPES,
        principal_id="reviewer",
    )


def pdf_pages(pages):
    return lambda content, media: {
        "state": "extracted",
        "chars": 10 * pages,
        "pages": [
            {"page": n, "chars": 10, "heading": f"Section {n}"}
            for n in range(1, pages + 1)
        ],
    }


@pytest.mark.parametrize(
    "features", [None, [], FEATURES], ids=["uncomposed", "features-off", "features-on"]
)
def test_multi_category_lookup_match_compare_and_cite(features):
    conn, store = deployment(features)
    try:
        state = readiness(conn, secrets=lambda _name: "k")
        assert state["features"] == {
            f: bool(features) and f in features for f in FEATURES
        }
        assert state["sources"]["bmecat-capatronic-mlcc"]["family"] == "component"
        assert (
            state["sources"]["eprel-washing-machines"]["category"]
            == "household-washing-machines"
        )
        assert (
            state["providers"]["eprel"]["source_id"] == "eprel-displays"
        )  # displays keep their representative
        _displays_unchanged(store)
        _washing_machines(conn, store)
        _refrigerating_appliances(store)
        _capacitors(conn, store)
    finally:
        conn.close()


def _displays_unchanged(store):
    candidates = store.propose_matches(
        "global", scopes=SCOPES, principal_id="m", category="electronic displays"
    )
    names = {
        v["model_id"]: v["designation"]
        for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]
    }
    assert sorted(
        (names[c["left_model_id"]], names[c["right_model_id"]], c["candidate_state"])
        for c in candidates["candidates"]
    ) == [
        ("EX-24F1", "EX-24F1", "proposed"),
        ("EX-24F1B", "EX-24F1", "ambiguous"),
        ("EX-27Q4", "EX-27Q4", "proposed"),
        ("EX-32U8", "EX-32U8", "proposed"),
        ("EX-32U8UK", "EX-32U8", "ambiguous"),
    ]


def _washing_machines(conn, store):
    category = "household washing machines"
    found = store.lookup(
        "global",
        scopes=SCOPES,
        brand="hausmark",
        designation="wm 8e14",
        category=category,
    )
    assert [(v["provider"], v["category_id"]) for v in found["variants"]] == [
        ("eprel", "household-washing-machines"),
        ("icecat", "household-washing-machines"),
    ]
    proposal = store.propose_matches(
        "global", scopes=SCOPES, principal_id="m", category=category
    )
    assert proposal["category"] == "household-washing-machines"
    states = {
        (
            c["evidence"][0].get("left")
            if c["evidence"][0]["kind"] == "designation"
            else "suffix"
        ): c["candidate_state"]
        for c in proposal["candidates"]
    }
    assert states == {
        "WM-8E14": "proposed",
        "WM-9E16": "proposed",
        "suffix": "ambiguous",
    }
    for candidate in proposal["candidates"]:
        if candidate["candidate_state"] == "proposed":
            assert {e["kind"] for e in candidate["evidence"]} >= {
                "designation",
                "rated_capacity_kg",
            }
            accept(store, candidate, "designation and rated capacity agree")
    wm8 = model(store, "icecat", "WM-8E14", category)
    wm9 = model(store, "icecat", "WM-9E16", category)
    result = store.compare("global", [wm8, wm9], scopes=SCOPES, category=category)
    rows = {(r["attribute"], r["mode"]): r for r in result["rows"]}
    assert [r["attribute"] for r in result["rows"]][:3] == [
        "rated_capacity",
        "energy_class",
        "energy_consumption_100_cycles",
    ]
    assert [c["value"] for c in rows[("rated_capacity", None)]["cells"]] == [
        "8.0",
        "9.0",
    ]
    energy = rows[("energy_consumption_100_cycles", None)]
    # 0.54 kWh per cycle (Icecat) and 54 kWh per 100 cycles (EPREL) are one declaration after exact conversion.
    assert energy["comparable"] is True and [c["value"] for c in energy["cells"]] == [
        "49.00",
        "54.00",
    ]
    assert sorted(v["native_unit"] for v in energy["cells"][1]["values"]) == [
        "kWh/100cycles",
        "kWh/cycle",
    ]
    assert (
        rows[("energy_class", None)]["not_comparable_reason"]
        == "label scheme unknown or different"
    )
    for row in result["rows"]:
        for cell in row["cells"]:
            for value in cell["values"]:
                assert value["evidence"]["revision_id"].startswith("product-revision:")
                assert value["evidence"]["locator"]
    assert "not a buying recommendation" in result["notice"]
    # Datasheet citation: the EPREL product information sheet, retained under a permissive policy, by page.
    eprel = expansion.variant(store, "eprel", "WM-8E14", category)
    links = {d["kind"]: d for d in store.documents("global", eprel["variant_id"])}
    sheet = links["product-information-sheet"]
    assert document_policy(conn, "global", eprel["variant_id"])["retain"] is False
    pdf = {
        "status": 200,
        "headers": {"Content-Type": "application/pdf"},
        "content": b"%PDF-1.7 wm fiche",
    }
    fetched = acquire_documents(
        store,
        "global",
        eprel["variant_id"],
        scopes=SCOPES,
        principal_id="op",
        policy={"retain": True, "allowed_hosts": ["eprel.ec.europa.eu"]},
        transport=displays.DocumentServer({sheet["url"]: pdf}),
        extractor=pdf_pages(3),
    )
    assert {d["kind"]: d["state"] for d in fetched["documents"]}[
        "product-information-sheet"
    ] == "retained"
    citation = store.cite_document(
        "global", sheet["link_id"], scopes=SCOPES, page=2, section="  Energy  class "
    )
    assert citation["page_verified"] and citation["locator"] == {
        "page": 2,
        "section": "Energy class",
    }
    assert citation["page_count"] == 3 and citation["content_sha256"]
    with pytest.raises(ProductError) as caught:
        store.cite_document("global", sheet["link_id"], scopes=SCOPES, page=4)
    assert caught.value.code == "invalid_locator"
    with pytest.raises(ProductError) as caught:
        store.cite_document("global", "product-document:unknown", scopes=SCOPES)
    assert caught.value.code == "not_found"


def _refrigerating_appliances(store):
    category = "refrigerating appliances"
    proposal = store.propose_matches(
        "global", scopes=SCOPES, principal_id="m", category=category
    )
    pairs = sorted(
        (c["evidence"][0]["left"], c["candidate_state"], c["confidence"])
        for c in proposal["candidates"]
    )
    assert pairs == [("NK-FR300", "proposed", "high"), ("NK-FR360", "proposed", "high")]
    # The EPREL fridge registered as "Hausmark WM-8E14" never pairs with the washing machine of that designation.
    everything = store.propose_matches("global", scopes=SCOPES, principal_id="m")[
        "candidates"
    ]
    by_model = {
        v["model_id"]: v
        for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]
    }
    assert all(
        by_model[c["left_model_id"]]["category_id"]
        == by_model[c["right_model_id"]]["category_id"]
        for c in everything
    )
    for candidate in proposal["candidates"]:
        accept(store, candidate, "designation, volume and annual energy agree")
    fr360 = model(store, "icecat", "NK-FR360", category)
    fr300 = model(store, "icecat", "NK-FR300", category)
    result = store.compare("global", [fr360, fr300], scopes=SCOPES)
    rows = {(r["attribute"], r["mode"]): r for r in result["rows"]}
    volume = rows[("total_volume", None)]
    assert (
        volume["comparable"] is False and volume["cells"][1]["state"] == "conflict"
    )  # 300 L and 301 L stay apart
    assert [c["value"] for c in rows[("annual_energy_consumption", None)]["cells"]] == [
        "212",
        "190",
    ]
    climate = rows[("climate_class", None)]["cells"][0]
    assert sorted(v["normalization_state"] for v in climate["values"]) == [
        "normalized",
        "not_in_vocabulary",
    ]
    washing = model(store, "icecat", "WM-8E14", "household washing machines")
    with pytest.raises(ProductError) as caught:
        store.compare("global", [fr360, washing], scopes=SCOPES)
    assert caught.value.code == "mixed_categories"
    with pytest.raises(ProductError) as caught:
        store.compare(
            "global",
            [fr360, fr300],
            scopes=SCOPES,
            category="household washing machines",
        )
    assert caught.value.code == "category_mismatch"
    with pytest.raises(ProductError) as caught:
        store.compare("global", [fr360, fr300], scopes=SCOPES, attributes=["diagonal"])
    assert caught.value.code == "unknown_attribute"
    cited = store.cite_document(
        "global",
        store.documents(
            "global",
            expansion.variant(store, "icecat", "NK-FR360", category)["variant_id"],
        )[0]["link_id"],
        scopes=SCOPES,
        page=1,
    )
    assert (
        cited["availability"] == "link" and cited["page_verified"] is False
    )  # never retained: cited as a link


def _capacitors(conn, store):
    category = "multilayer ceramic capacitors"
    component = store.lookup_component(
        "global", scopes=SCOPES, mpn="CX0805C0G471J101", category=category
    )
    [entry] = component["components"]
    assert entry["manufacturer"] == ["CAPATRONIC GMBH", "Capatronic GmbH"]
    lifecycle = [
        v["lifecycle_status"] for v in entry["variants"] if v["lifecycle_status"]
    ]
    assert [
        (s["value"], s["declared"], s["date"], s["source_id"]) for s in lifecycle
    ] == [("nrnd", "NRND", "2026-05-04", "bmecat-capatronic-mlcc")]
    proposal = store.propose_matches(
        "global", scopes=SCOPES, principal_id="m", category=category
    )
    outcome = {
        c["evidence"][0]["left"]: (c["candidate_state"], c["reasons"])
        for c in proposal["candidates"]
    }
    assert outcome == {
        "CX0603X7R104K500": ("proposed", []),
        "CX0805C0G471J101": ("proposed", []),
        "CX1206X5R106M250": ("contradicted", ["rated voltage differs by 9.000 V"]),
    }
    for candidate in proposal["candidates"]:
        if candidate["candidate_state"] == "contradicted":
            with pytest.raises(ProductError) as caught:
                accept(store, candidate, "same part number")
            assert caught.value.code == "contradicted_match"
        else:
            assert (
                candidate["evidence"][0]["manufacturer"]["basis"] == "normalised name"
            )
            accept(
                store,
                candidate,
                "same manufacturer, MPN, capacitance, voltage and case",
            )
    cx0603 = model(store, "bmecat:capatronic", "CX0603X7R104K500", category)
    cx0805 = model(store, "bmecat:capatronic", "CX0805C0G471J101", category)
    result = store.compare("global", [cx0603, cx0805], scopes=SCOPES, category=category)
    rows = {(r["attribute"], r["mode"]): r for r in result["rows"]}
    capacitance = rows[("capacitance", None)]
    assert (
        capacitance["comparable"] is True
        and capacitance["tolerance_attribute"] == "capacitance_tolerance"
    )
    assert [c["value"] for c in capacitance["cells"]] == ["100000.000", "470.000"]
    tolerances = [
        [
            (t["provider"], t["normalized_value"], t["normalized_unit"])
            for t in c["tolerance"]
        ]
        for c in capacitance["cells"]
    ]
    assert tolerances == [
        [
            ("bmecat:capatronic", "10.00", "percent"),
            ("bmecat:voltaria", "10.00", "percent"),
        ],
        [("bmecat:capatronic", "5.00", "percent")],
    ]
    assert all(
        t["evidence"]["locator"]["xpath"]
        for c in capacitance["cells"]
        for t in c["tolerance"]
    )
    assert [c["value"] for c in rows[("dielectric", None)]["cells"]] == [
        "X7R",
        "C0G",
    ]  # NP0 read as C0G
    assert ("package", "eia-imperial") in rows and (
        "package",
        "eia-metric",
    ) in rows  # modes never merged
    assert "drop-in replacement" in result["notice"]
    assert all("rank" not in row and "best" not in row for row in result["rows"])
    with pytest.raises(ProductError) as caught:
        store.compare(
            "global",
            [cx0603, model(store, "icecat", "NK-FR360", "refrigerating appliances")],
            scopes=SCOPES,
        )
    assert caught.value.code == "mixed_categories"
    # The manufacturer's datasheet link is followed only as the source's terms allow (retain: false here).
    variant = expansion.variant(
        store, "bmecat:capatronic", "CX0603X7R104K500", category
    )
    policy = document_policy(conn, "global", variant["variant_id"])
    server = displays.DocumentServer({})
    result = acquire_documents(
        store,
        "global",
        variant["variant_id"],
        scopes=SCOPES,
        principal_id="op",
        policy=policy,
        transport=server,
    )
    assert [d["state"] for d in result["documents"]] == [
        "link_only"
    ] and server.calls == []
    datasheet = store.documents("global", variant["variant_id"])[0]
    cited = store.cite_document(
        "global", datasheet["link_id"], scopes=SCOPES, section="Ordering code"
    )
    assert cited["kind"] == "datasheet" and cited["locator"] == {
        "section": "Ordering code"
    }
    assert cited["record_locator"]["xpath"].endswith("/MIME_INFO/MIME[2]")

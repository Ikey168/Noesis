"""Products expansion acquisition: EPREL groups (PX03), Open Icecat categories (PX04), BMEcat components (PX06).

Every payload is an authored envelope or catalogue for fictional brands and
manufacturers (``tests/fixtures/source_packs/products-*-{washing-machines,
refrigerating-appliances}.json``, ``products-bmecat-*.json``), replayed through
the real adapters and the source-pack runtime. No network.
"""

from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.product_sources import (
    COMPONENT_SOURCE_DECISIONS,
    PROVIDER_CONTRACTS,
    BmecatComponentAdapter,
    EprelProductAdapter,
    IcecatProductAdapter,
    component_links,
    fixture_transport,
    parse_bmecat,
)
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import SourcePackError, SourcePackStore
from src.kb.products import ProductStore
from tests.unit.domains import test_products_pack as displays

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = {
    "eprel-washing-machines": "products-eprel-washing-machines.json",
    "eprel-refrigerating-appliances": "products-eprel-refrigerating-appliances.json",
    "icecat-washing-machines": "products-icecat-washing-machines.json",
    "icecat-refrigerating-appliances": "products-icecat-refrigerating-appliances.json",
    "bmecat-capatronic-mlcc": "products-bmecat-capatronic.json",
    "bmecat-voltaria-mlcc": "products-bmecat-voltaria.json",
}
APPLIANCES = [k for k in FIXTURES if not k.startswith("bmecat")]
COMPONENTS = ["bmecat-capatronic-mlcc", "bmecat-voltaria-mlcc"]
SCOPES = displays.SCOPES


def pages(source_id: str) -> list[dict]:
    path = ROOT / "tests/fixtures/source_packs" / FIXTURES[source_id]
    return copy.deepcopy(json.loads(path.read_text())["native_pages"])


def adapter(
    cls,
    source_id,
    native_pages=None,
    *,
    secret="fixture-credential",
    transport=None,
    **overrides,
):
    item = copy.deepcopy(displays.source(displays.manifest(), source_id))
    item["product"].update(overrides)
    return cls(
        item,
        transport=transport or fixture_transport(native_pages or pages(source_id)),
        secret=secret,
    )


def every_page(instance, operation="models"):
    result, cursor = [], None
    while True:
        page = instance.fetch_page(
            {"operation": operation, "parameters": {}, "limit": 20}, cursor=cursor
        )
        result.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return result


def records(instance, operation="models"):
    return [
        r["product_record"]
        for page in every_page(instance, operation)
        for r in page.records
    ]


def run(runtime, value, key, source_ids, operation):
    request = {
        "pack_id": value["pack_id"],
        "run_key": key,
        "operation": operation,
        "max_results": 50,
        "max_bytes": 20_000_000,
        "timeout_ms": 60_000,
        "source_ids": source_ids,
    }
    return runtime.run(
        request,
        principal_id="operator",
        adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
        dns_resolver=displays.PUBLIC_DNS,
        secret_resolver=lambda _ref: "fixture-credential",
    )


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, runtime = displays.install(conn)
    appliances = run(runtime, value, "appliances-1", APPLIANCES, "models")
    components = run(runtime, value, "components-1", COMPONENTS, "components")
    yield conn, value, runtime, (appliances, components), ProductStore(conn)
    conn.close()


def variant(store, provider, designation, category=None):
    return next(
        v
        for v in store.lookup("global", scopes=SCOPES, limit=100, category=category)[
            "variants"
        ]
        if v["provider"] == provider and v["designation"] == designation
    )


# --------------------------------------------------------------------- contracts


def test_source_contracts_record_the_px01_decisions():
    assert PROVIDER_CONTRACTS["bmecat"]["status"] == "unverified-live"
    assert {
        name: item["decision"] for name, item in COMPONENT_SOURCE_DECISIONS.items()
    } == {
        "bmecat": "implement",
        "octopart": "link-only",
        "digikey": "link-only",
        "mouser": "not implemented",
        "manufacturer-web": "not implemented",
    }
    assert all("verify" in item for item in COMPONENT_SOURCE_DECISIONS.values())
    links = component_links("Capatronic GmbH", "CX0603X7R104K500/T&R")
    assert [link["provider"] for link in links] == ["octopart", "digikey"]
    assert all(
        link["cached_data"] is None and link["decision"] == "link-only"
        for link in links
    )
    assert (
        links[1]["url"]
        == "https://www.digikey.com/en/products/result?keywords=CX0603X7R104K500%2FT%26R"
    )
    assert component_links("Capatronic", " ") == []


def test_every_new_source_defaults_to_the_runtime_same_host_transport():
    for source_id, cls in (
        ("eprel-washing-machines", EprelProductAdapter),
        ("icecat-refrigerating-appliances", IcecatProductAdapter),
        ("bmecat-capatronic-mlcc", BmecatComponentAdapter),
    ):
        item = copy.deepcopy(displays.source(displays.manifest(), source_id))
        instance = cls(item)
        assert (
            isinstance(instance.transport, partial)
            and instance.transport.func is HTTPSPageAdapter._request
        )
        assert instance.transport.keywords["max_bytes"] == item["budgets"]["max_bytes"]


# ------------------------------------------------------------------------ EPREL


def test_eprel_groups_map_through_the_registry_with_their_label_scheme():
    washing = records(adapter(EprelProductAdapter, "eprel-washing-machines"))
    assert [r["designation"] for r in washing] == [
        "WM-8E14",
        "WM-9E16",
        "WM-8E14X",
        "WM-7C12",
    ]
    first = washing[0]
    by = {a["native_name"]: a for a in first["attributes"]}
    assert by["energyConsumption100"]["native_unit"] == "kWh/100cycles"
    assert by["ratedCapacity"] == {
        "attribute": "rated_capacity",
        "mode": None,
        "native_name": "ratedCapacity",
        "native_value": 8,
        "native_unit": "kg",
        "label_scheme": "EU_2019_2014",
        "locator": {"json_pointer": "/ratedCapacity"},
    }
    # A field the registry does not map is kept as the supplier registered it.
    assert first["source_fields"] == [
        {
            "native_name": "programmeDurationRated",
            "native_value": "3:39",
            "locator": {"json_pointer": "/programmeDurationRated"},
        }
    ]
    assert (
        washing[3]["record_status"] == "withdrawn"
        and washing[3]["native_status"] == "WITHDRAWN"
    )
    assert first["documents"][0]["url"].endswith(
        "/washingmachines2019/2200001/fiches?language=EN"
    )
    fridges = records(adapter(EprelProductAdapter, "eprel-refrigerating-appliances"))
    climate = next(
        a for a in fridges[0]["attributes"] if a["attribute"] == "climate_class"
    )
    assert climate["native_value"] == [
        "SN",
        "N",
        "ST",
        "T",
    ]  # a list is kept, never dropped
    assert {r["label_scheme"] for r in fridges} == {"EU_2019_2016"}


def test_eprel_groups_keep_the_display_access_rules():
    with pytest.raises(SourcePackError) as caught:
        every_page(adapter(EprelProductAdapter, "eprel-washing-machines", secret=None))
    assert caught.value.code == "authentication_failed"
    outcomes = [
        p.receipt["model_outcome"]
        for p in every_page(adapter(EprelProductAdapter, "eprel-washing-machines"))
    ]
    assert outcomes == ["returned", "returned", "returned", "returned", "not_found"]
    with pytest.raises(
        SourcePackError
    ) as caught:  # a group the category does not register
        every_page(
            adapter(
                EprelProductAdapter,
                "eprel-washing-machines",
                category={
                    "label": "household washing machines",
                    "product_group": "dishwashers2019",
                },
            )
        )
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter(
            EprelProductAdapter,
            "eprel-washing-machines",
            category={"label": "washers", "product_group": "x"},
        )
    assert caught.value.code == "invalid_mapping"


# ----------------------------------------------------------------------- Icecat


def test_icecat_categories_map_features_with_per_feature_units():
    washing = records(adapter(IcecatProductAdapter, "icecat-washing-machines"))
    first, second = washing
    units = {a["attribute"]: a["native_unit"] for a in first["attributes"]}
    assert units == {
        "rated_capacity": "kg",
        "energy_class": None,
        "energy_consumption_100_cycles": "kWh/100cycles",
        "water_consumption_cycle": "L/cycle",
        "max_spin_speed": "rpm",
        "noise_spinning": "dB(A)",
    }
    assert first["source_fields"][0]["native_name"] == "Drum volume"
    assert first["source_fields"][0]["native_unit_sign"] == "l"
    per_cycle = next(
        a
        for a in second["attributes"]
        if a["native_name"].endswith("per cycle (washing)")
    )
    assert (per_cycle["attribute"], per_cycle["native_unit"]) == (
        "energy_consumption_100_cycles",
        "kWh/cycle",
    )
    assert all(r["category"]["in_pinned_category"] for r in washing)
    fridges = records(adapter(IcecatProductAdapter, "icecat-refrigerating-appliances"))
    annual = {
        r["designation"]: next(
            a["native_unit"]
            for a in r["attributes"]
            if a["attribute"] == "annual_energy_consumption"
        )
        for r in fridges
    }
    assert annual == {"NK-FR360": "kWh/annum", "NK-FR300": "kWh/annum"}
    assert all(g["state"] == "valid" for r in fridges for g in r["identifiers"]["gtin"])


# ----------------------------------------------------------------------- BMEcat


def test_bmecat_manufacturer_catalogue_yields_components_with_published_lifecycle():
    counting = []
    transport = fixture_transport(pages("bmecat-capatronic-mlcc"))

    def once(**kwargs):
        counting.append(kwargs["url"])
        return transport(**kwargs)

    instance = adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", transport=once)
    result = every_page(instance, "components")
    assert (
        len(counting) == 1
    )  # one bounded download per run, every selector read from it
    assert [p.receipt["model_outcome"] for p in result] == ["returned"] * 4 + [
        "not_found"
    ]
    assert [p.bytes_read for p in result][1:] == [0, 0, 0, 0] and result[
        0
    ].bytes_read > 0
    found = [r["product_record"] for p in result for r in p.records]
    by = {r["designation"]: r for r in found}
    active = by["CX0603X7R104K500"]
    assert (
        active["provider"] == "bmecat:capatronic"
        and active["assertion_kind"] == "manufacturer-published-data"
    )
    assert (
        active["brand"] == "Capatronic GmbH"
    )  # the header supplier is the manufacturer
    assert active["identifiers"]["component_key"] == "capatronic|CX0603X7R104K500"
    assert "distributor_skus" not in active["identifiers"]
    assert active["lifecycle_status"] == {
        "declared": "Active",
        "value": "active",
        "source": "Capatronic GmbH (manufacturer BMEcat catalogue)",
        "provider": "bmecat:capatronic",
        "date": "2026-05-04",
        "date_declared": "2026-05-04",
        "date_basis": "catalogue generation date",
        "locator": {
            "xpath": "/BMECAT/T_NEW_CATALOG/PRODUCT[1]/PRODUCT_DETAILS/PRODUCT_STATUS[1]"
        },
    }
    assert [
        by[k]["lifecycle_status"]["value"]
        for k in ("CX0805C0G471J101", "CX1206X5R106M250")
    ] == ["nrnd", "last-time-buy"]
    # A merchandising status is not a lifecycle status: kept as a source field, nothing inferred.
    new = by["CX0402C0G1R0C500"]
    assert "lifecycle_status" not in new
    assert any(
        f["native_name"] == "PRODUCT_STATUS[new_product]" and f["native_value"] == "New"
        for f in new["source_fields"]
    )
    assert active["documents"] == [
        {
            "kind": "datasheet",
            "url": "https://data.capatronic.example/bmecat/datasheets/cx0603x7r.pdf",
            "media_type": "application/pdf",
            "language": "eng",
            "title": "Datasheet",
            "locator": {"xpath": "/BMECAT/T_NEW_CATALOG/PRODUCT[1]/MIME_INFO/MIME[2]"},
        }
    ]  # the image is not a document
    assert [f["native_name"] for f in active["source_fields"]] == ["Termination"]
    assert [g["state"] for g in active["identifiers"]["gtin"]] == ["valid"]
    text = json.dumps(found)
    assert (
        "PRICE" not in text and "0.012" not in text and "QUANTITY_MIN" not in text
    )  # never stored


def test_bmecat_supplier_catalogue_keeps_skus_as_aliases_and_identity_as_published():
    found = records(
        adapter(BmecatComponentAdapter, "bmecat-voltaria-mlcc"), "components"
    )
    by = {r["provider_record_id"]: r for r in found}
    assert set(by) == {"VC-100234", "VC-100235", "VC-100236", "VC-100240"}
    first = by["VC-100234"]
    assert first["assertion_kind"] == "supplier-catalogue-content"
    assert first["identifiers"]["distributor_skus"] == [
        {"provider": "bmecat:voltaria", "value": "VC-100234"}
    ]
    assert first["identifiers"]["component_key"] == "capatronic|CX0603X7R104K500"
    assert by["VC-100235"]["brand"] == "CAPATRONIC GMBH"  # published spelling kept
    assert (
        by["VC-100240"]["identifiers"]["component_key"] == "othercap|CX0603X7R104K500"
    )
    assert all("lifecycle_status" not in r for r in found)
    units = {a["native_name"]: a["native_unit"] for a in first["attributes"]}
    assert (
        units["Capacitance"] == "uF" and units["Rated voltage"] == "V"
    )  # UN/ECE VLT sign mapped


def test_bmecat_failures_are_classified_and_entities_refused():
    base = pages("bmecat-capatronic-mlcc")

    def failing(**changes):
        native = copy.deepcopy(base)
        native[0].update(changes)
        with pytest.raises(SourcePackError) as caught:
            every_page(
                adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", native),
                "components",
            )
        return caught.value.code

    assert failing(status=404, body=None) == "source_unavailable"
    assert failing(status=403, body=None) == "authentication_failed"
    assert (
        failing(status=429, body=None, headers={"Retry-After": "3"}) == "rate_limited"
    )
    assert failing(body="<BMECAT><HEADER>") == "schema_drift"
    assert failing(body="<CATALOGUE/>") == "schema_drift"
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE b [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        "<BMECAT><HEADER><CATALOG/></HEADER><T_NEW_CATALOG>&b;</T_NEW_CATALOG></BMECAT>"
    )
    assert failing(body=bomb) == "schema_drift"
    assert (
        failing(final_url="https://mirror.example.org/catalogue.xml")
        == "network_policy"
    )
    with pytest.raises(SourcePackError):
        parse_bmecat(b"not xml")


def test_bmecat_selection_requires_components_roles_and_publishers():
    with pytest.raises(SourcePackError) as caught:
        adapter(
            BmecatComponentAdapter,
            "bmecat-capatronic-mlcc",
            selection=[{"brand": "X", "product_code": "Y"}],
        )
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter(
            BmecatComponentAdapter,
            "bmecat-capatronic-mlcc",
            selection=[{"manufacturer": "X", "mpn": "n/a"}],
        )
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", role="distributor")
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter(
            BmecatComponentAdapter,
            "bmecat-capatronic-mlcc",
            category={"label": "household washing machines"},
        )
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(
        SourcePackError
    ) as caught:  # appliance sources never pin a component category
        adapter(
            IcecatProductAdapter,
            "icecat-washing-machines",
            category={
                "label": "multilayer ceramic capacitors",
                "provider_category_id": "1",
            },
        )
    assert caught.value.code == "invalid_mapping"


def test_bmecat_deletion_withdraws_and_price_changes_add_no_revision():
    native = pages("bmecat-capatronic-mlcc")
    repriced = copy.deepcopy(native)
    repriced[0]["body"] = repriced[0]["body"].replace(
        "<PRICE_AMOUNT>0.012", "<PRICE_AMOUNT>0.019"
    )
    first = records(
        adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", native), "components"
    )
    second = records(
        adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", repriced),
        "components",
    )
    assert [r["raw_sha256"] for r in first] == [r["raw_sha256"] for r in second]
    deleted = copy.deepcopy(native)
    deleted[0]["body"] = deleted[0]["body"].replace(
        '<PRODUCT mode="new"><SUPPLIER_PID>CX0805',
        '<PRODUCT mode="delete"><SUPPLIER_PID>CX0805',
    )
    by = {
        r["designation"]: r
        for r in records(
            adapter(BmecatComponentAdapter, "bmecat-capatronic-mlcc", deleted),
            "components",
        )
    }
    assert by["CX0805C0G471J101"]["record_status"] == "withdrawn"
    assert by["CX0603X7R104K500"]["record_status"] == "published"


def test_records_follow_the_product_record_schema():
    validator = jsonschema.Draft7Validator(
        json.loads(
            (
                ROOT / "contracts/schemas/jsonschema/noesis-product-record-v1.json"
            ).read_text()
        )
    )
    for source_id, cls, operation in (
        *(
            (
                s,
                EprelProductAdapter if s.startswith("eprel") else IcecatProductAdapter,
                "models",
            )
            for s in APPLIANCES
        ),
        *((s, BmecatComponentAdapter, "components") for s in COMPONENTS),
    ):
        for record in records(adapter(cls, source_id), operation):
            assert not list(validator.iter_errors(record)), source_id


# ------------------------------------------------------------------- projection


def test_runtime_projects_each_source_with_selection_outcomes(loaded):
    conn, _, _, (appliances, components), store = loaded
    assert appliances["status"] == components["status"] == "complete"
    projection = {
        s["source_id"]: (
            s["projection"]["projected_models"],
            s["projection"]["refresh_advanced"],
        )
        for s in appliances["sources"] + components["sources"]
    }
    assert projection == {
        "eprel-washing-machines": (5, True),
        "eprel-refrigerating-appliances": (3, True),
        "icecat-washing-machines": (2, True),
        "icecat-refrigerating-appliances": (2, True),
        "bmecat-capatronic-mlcc": (5, True),
        "bmecat-voltaria-mlcc": (4, True),
    }
    outcomes = {
        o["outcome"] for o in store.selection_outcomes("global", components["run_id"])
    }
    assert outcomes == {"returned", "not_found"}
    assert (
        store.lookup("global", scopes=SCOPES, category="refrigerating appliances")[
            "count"
        ]
        == 5
    )
    assert (
        conn.execute("SELECT count(*) FROM source_pack_quarantine").fetchone()[0] == 0
    )


def test_reacquiring_unchanged_sources_adds_nothing(loaded):
    conn, value, runtime, _, _ = loaded
    tables = (
        "product_identities",
        "product_revisions",
        "product_assertions",
        "product_document_links",
        "product_lifecycle",
    )
    before = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
    }
    run(runtime, value, "appliances-2", APPLIANCES, "models")
    run(runtime, value, "components-2", COMPONENTS, "components")
    assert {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
    } == before


def test_one_providers_records_of_two_categories_never_share_a_model(loaded):
    _, _, _, _, store = loaded
    washing = variant(store, "eprel", "WM-8E14", "household washing machines")
    fridge = variant(store, "eprel", "WM-8E14", "refrigerating appliances")
    assert washing["model_id"] != fridge["model_id"]
    inspected = store.inspect("global", fridge["model_id"], scopes=SCOPES)
    assert [v["category_id"] for v in inspected["variants"]] == [
        "refrigerating-appliances"
    ]


def test_components_are_identified_by_manufacturer_and_mpn_with_sku_aliases(loaded):
    _, _, _, _, store = loaded
    maker = variant(store, "bmecat:capatronic", "CX0603X7R104K500")
    supplier = next(
        v
        for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]
        if v["provider_record_id"] == "VC-100234"
    )
    other = next(
        v
        for v in store.lookup("global", scopes=SCOPES, limit=100)["variants"]
        if v["provider_record_id"] == "VC-100240"
    )
    assert (
        maker["model_id"] != supplier["model_id"]
    )  # provider-scoped models, joined only by reviewed matches
    model = store.inspect("global", maker["model_id"], scopes=SCOPES)
    assert model["variants"][0]["lifecycle_status"]["value"] == "active"
    answer = store.lookup_component("global", scopes=SCOPES, mpn="cx0603x7r104k500")
    keys = [c["component_key"] for c in answer["components"]]
    assert keys == ["capatronic|CX0603X7R104K500", "othercap|CX0603X7R104K500"]
    capatronic = answer["components"][0]
    assert {v["provider"] for v in capatronic["variants"]} == {
        "bmecat:capatronic",
        "bmecat:voltaria",
    }
    assert [v["lifecycle_status"] is not None for v in capatronic["variants"]] == [
        True,
        False,
    ]
    assert [link["provider"] for link in capatronic["links"]] == ["octopart", "digikey"]
    by_sku = store.lookup_component("global", scopes=SCOPES, sku="VC-100234")
    assert [c["component_key"] for c in by_sku["components"]] == [
        "capatronic|CX0603X7R104K500"
    ]
    scoped = store.lookup_component(
        "global", scopes=SCOPES, mpn="CX0603X7R104K500", manufacturer="Othercap Inc"
    )
    assert [c["component_key"] for c in scoped["components"]] == [
        "othercap|CX0603X7R104K500"
    ]
    assert other["variant_id"] in {
        v["variant_id"] for v in scoped["components"][0]["variants"]
    }
    assert store.lookup_component("global", scopes=SCOPES, mpn="CX9999")["count"] == 0


def test_manufacturer_links_are_reviewed_identity_decisions(loaded):
    conn, _, _, _, store = loaded
    from src.kb.entity_history import EXECUTE_SCOPE, REVIEW_SCOPE, WRITE_SCOPE
    from src.kb.products import manufacturers_equivalent

    assert (
        manufacturers_equivalent(conn, "Othercap Inc.", "Capatronic", "global") is None
    )
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:capatronic', 'Capatronic GmbH', 'ORG')"
    )
    scopes = SCOPES | {WRITE_SCOPE, REVIEW_SCOPE}
    from src.kb.products import ProductError

    with pytest.raises(ProductError) as caught:
        store.decide_manufacturer_link(
            "global",
            "Capatronic",
            "ent:capatronic",
            "match",
            "catalogue header",
            scopes=SCOPES,
            principal_id="r",
        )
    assert caught.value.code == "unauthorized"
    with pytest.raises(ProductError) as caught:
        store.decide_manufacturer_link(
            "global",
            "Nobody Ltd",
            "ent:capatronic",
            "match",
            "x",
            scopes=scopes,
            principal_id="r",
        )
    assert caught.value.code == "not_found"
    with pytest.raises(ProductError) as caught:
        store.decide_manufacturer_link(
            "global",
            "Capatronic",
            "ent:unknown",
            "match",
            "x",
            scopes=scopes,
            principal_id="r",
        )
    assert caught.value.code == "not_found"
    first = store.decide_manufacturer_link(
        "global",
        "Capatronic",
        "ent:capatronic",
        "match",
        "catalogue header",
        scopes=scopes,
        principal_id="r",
    )
    second = store.decide_manufacturer_link(
        "global",
        "Othercap Inc.",
        "ent:capatronic",
        "match",
        "reviewer asserts the same company (test)",
        scopes=scopes,
        principal_id="r",
    )
    assert first["merged"] is False
    assert (
        manufacturers_equivalent(conn, "Othercap", "Capatronic GmbH", "global")
        == "reviewed links to ent:capatronic"
    )
    assert (
        store.lookup_component(
            "global", scopes=SCOPES, mpn="CX0603X7R104K500", manufacturer="Capatronic"
        )["count"]
        == 2
    )
    with pytest.raises(ProductError):
        store.revert_manufacturer_link(
            "global", second["link_id"], scopes=scopes, principal_id="r"
        )
    reverted = store.revert_manufacturer_link(
        "global", second["link_id"], scopes=scopes | {EXECUTE_SCOPE}, principal_id="r"
    )
    assert reverted["status"] == "reverted"
    assert (
        manufacturers_equivalent(conn, "Othercap", "Capatronic GmbH", "global") is None
    )
    component = store.lookup_component(
        "global", scopes=SCOPES, mpn="CX0603X7R104K500", manufacturer="Capatronic"
    )["components"][0]
    assert component["manufacturer_resolution"]["link"]["entity_id"] == "ent:capatronic"


def test_reads_before_any_run_are_not_ready():
    from src.kb.products import ProductError

    conn = duckdb.connect(":memory:")
    SourcePackStore(conn)
    store = ProductStore(conn, initialize=False)
    for call in (
        lambda: store.lookup("global", scopes=SCOPES),
        lambda: store.lookup_component("global", scopes=SCOPES, mpn="X"),
        lambda: store.compare("global", ["a", "b"], scopes=SCOPES),
        lambda: store.cite_document("global", "product-document:x", scopes=SCOPES),
    ):
        with pytest.raises(ProductError) as caught:
            call()
        assert caught.value.code == "not_ready"
    conn.close()

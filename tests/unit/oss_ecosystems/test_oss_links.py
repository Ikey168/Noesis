"""Advisories and inventories beside OSS histories and graphs, by citation only (OS09)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.domains.technical.inventory import InventoryStore
from src.kb.oss_ecosystem_graph import dependency_graph_as_of
from src.kb.oss_ecosystem_links import OssLinks
from src.kb.oss_ecosystem_store import OssStoreError
from src.kb.vulnerability_identity import VulnerabilityIdentity
from tests.unit import vulnerability_harness as vh
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h

ROOT = Path(__file__).resolve().parents[3]
TECH = {"knowledge:technical:read"}
OSS_MODULES = [
    *sorted((ROOT / "src/kb").glob("oss_*.py")),
    ROOT / "src/ingestion/oss_ecosystem_sources.py",
]


def _counts(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'vuln_%' "
            "OR table_name LIKE 'technical_%' ORDER BY table_name"
        ).fetchall()
    ]
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


@pytest.fixture(scope="module")
def world():
    conn = h.world()
    vh.osv(conn, "osv_PYSEC-2099-1.json", "PYSEC-2099-1", at=vh.DAY["01-11"])
    return conn


def test_package_advisories_cite_technology_revisions_without_copying(world):
    before = _counts(world)
    answer = OssLinks(world).package_advisories(
        h.NS, "fixture-parser", ecosystem="pypi", version="1.1.0", scopes=h.READ | TECH
    )
    cited = answer["citations"][0]
    assert cited["cve_id"] == "CVE-2099-0001"
    assert cited["advisories"][0]["source"] == "osv" and cited["advisories"][0][
        "revision_id"
    ].startswith("vuln-")
    assert cited["ranges"][0]["range_check"]["state"] == "within_cited_range"
    assert "verdict" in answer["notice"] and answer["owner"].startswith(
        "technology.vulnerabilities"
    )
    text = h.all_statements(world)
    assert (
        "PYSEC-2099-1" not in text and "CVE-2099-0001" not in text
    )  # nothing copied into the OSS store
    assert _counts(world) == before
    with pytest.raises(Exception) as caught:
        OssLinks(world).package_advisories(
            h.NS, "fixture-parser", ecosystem="pypi", scopes=h.READ
        )
    assert getattr(caught.value, "code", None) == "unauthorized"


def test_accepted_component_matches_are_reused_never_proposed(world):
    identity = VulnerabilityIdentity(world)
    offered = identity.offer(
        h.NS,
        component_key="package:pkg:pypi:fixture-parser",
        target_key="advisory-package:osv:PyPI:fixture-parser",
        basis="exact-package-coordinate",
        evidence=[{"fixture": True}],
        principal_id="alice",
        scopes=vh.WRITE,
        coordinate="pkg:pypi:fixture-parser",
    )
    identity.review(
        h.NS,
        offered["match_id"],
        "accept",
        "same coordinate",
        principal_id="rev",
        scopes=vh.REVIEW,
    )
    answer = OssLinks(world).package_advisories(
        h.NS, "pkg:pypi:fixture-parser", scopes=h.READ | TECH
    )
    assert [m["match_id"] for m in answer["accepted_component_matches"]] == [
        offered["match_id"]
    ]
    graph = dependency_graph_as_of(
        world, h.NS, "pkg:pypi:fixture-parser", "1.1.0", fb.DATES["d2"], scopes=h.READ
    )
    per_node = OssLinks(world).graph_advisories(
        graph, scopes=h.READ | TECH, vulnerability_namespace=h.NS
    )
    states = {n["node"]: n["state"] for n in per_node["nodes"]}
    assert states == {
        "pkg:pypi:fixture-parser@1.1.0": "advisories-found",
        "pkg:pypi:fixture-tokens@1.3.0": "unknown",
    }


def test_an_inventory_is_compared_with_the_graph_and_yanked_pins_are_listed(world):
    inventory = InventoryStore(world).import_inventory(
        "fixture-parser==1.1.0\nfixture-tokens==1.2.0\n",
        "requirements.txt",
        owner_id="alice",
    )
    before = _counts(world)
    answer = OssLinks(world).compare_inventory(
        h.NS,
        inventory["inventory_id"],
        "fixture-parser",
        "1.1.0",
        ecosystem="pypi",
        owner_id="alice",
        scopes=h.READ | TECH,
        inventory_date=fb.DATES["d2"],
    )
    flagged = {e["coordinate"]: e for e in answer["pinned_but_yanked_or_deprecated"]}
    assert (
        flagged["pkg:pypi:fixture-parser"]["reason"]
        == "Broken wheel metadata; use 1.0.0"
    )
    assert [
        (d["coordinate"], d["pinned"], d["graph_resolved"])
        for d in answer["differences"]
    ] == [("pkg:pypi:fixture-tokens", "1.2.0", "1.3.0")]
    assert _counts(world) == before
    with pytest.raises(OssStoreError) as caught:
        OssLinks(world).compare_inventory(
            h.NS,
            inventory["inventory_id"],
            "fixture-parser",
            "1.1.0",
            ecosystem="pypi",
            owner_id="mallory",
            scopes=h.READ | TECH,
            inventory_date=fb.DATES["d2"],
        )
    assert caught.value.code == "not_found"


def test_no_oss_module_writes_vulnerability_inventory_or_technical_tables():
    writes = re.compile(
        r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|DELETE\s+FROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+(\w+)"
        r"|UPDATE\s+(\w+)\s+SET",
        re.IGNORECASE,
    )
    found = 0
    for path in OSS_MODULES:
        for match in writes.finditer(path.read_text()):
            table = match.group(1) or match.group(2)
            found += 1
            assert table.startswith("oss_"), (path.name, table)
    assert found > 5

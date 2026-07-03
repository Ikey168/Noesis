"""M9.2: the domain-pack registry. A packaged pack publishes to the registry and
is discoverable and versioned there; versions are immutable and the latest
resolves."""

from pathlib import Path

import pytest

from src.domains import pack_registry
from src.domains.pack_format import PackFormatError, PackManifest
from src.domains.pack_registry import PackRegistryError

REPO = Path(__file__).resolve().parents[3]


def _manifest(version="1.0.0", desc="Energy pack"):
    return PackManifest(
        name="energy",
        version=version,
        description=desc,
        source_types=["news"],
        ui_flags={"energy": True},
        planner_keywords={"trend": ["grid", "outage"]},
    )


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "registry")


def test_publish_then_discover_and_get(root):
    pub = pack_registry.publish(_manifest(), root=root)
    assert pub["name"] == "energy" and pub["version"] == "1.0.0"

    # Discoverable.
    found = pack_registry.discover(root)
    assert len(found) == 1
    assert found[0]["name"] == "energy"
    assert found[0]["latest_version"] == "1.0.0"

    # Retrievable, with contents intact.
    got = pack_registry.get("energy", root=root)
    assert got is not None
    assert got.planner_keywords == {"trend": ["grid", "outage"]}


def test_versioning_keeps_all_versions_and_resolves_latest(root):
    pack_registry.publish(_manifest("1.0.0"), root=root)
    pack_registry.publish(_manifest("1.2.0"), root=root)
    pack_registry.publish(_manifest("1.10.0"), root=root)  # semver: 10 > 2

    assert pack_registry.versions("energy", root=root) == ["1.10.0", "1.2.0", "1.0.0"]
    assert pack_registry.latest_version("energy", root=root) == "1.10.0"
    # get with no version resolves the latest; a pinned version resolves exactly.
    assert pack_registry.get("energy", root=root).version == "1.10.0"
    assert pack_registry.get("energy", "1.0.0", root=root).version == "1.0.0"


def test_versions_are_immutable(root):
    pack_registry.publish(_manifest("1.0.0", desc="first"), root=root)
    with pytest.raises(PackRegistryError):
        pack_registry.publish(_manifest("1.0.0", desc="second"), root=root)
    # The original is untouched.
    assert pack_registry.get("energy", "1.0.0", root=root).description == "first"
    # force overwrites.
    pack_registry.publish(_manifest("1.0.0", desc="second"), root=root, force=True)
    assert pack_registry.get("energy", "1.0.0", root=root).description == "second"


def test_publish_refuses_invalid_manifest(root):
    bad = _manifest()
    bad.version = "nope"
    with pytest.raises(PackFormatError):
        pack_registry.publish(bad, root=root)


def test_publish_from_a_packaged_path(root, tmp_path):
    # The shipped example pack publishes straight from disk.
    src = REPO / "packs" / "energy" / "pack.json"
    pub = pack_registry.publish_path(str(src), root=root)
    assert pub["name"] == "energy"
    assert pack_registry.get("energy", root=root).name == "energy"


def test_missing_pack_reads_back_as_none(root):
    assert pack_registry.get("ghost", root=root) is None
    assert pack_registry.versions("ghost", root=root) == []
    assert pack_registry.list_packs(root) == []


def test_unpublish(root):
    pack_registry.publish(_manifest("1.0.0"), root=root)
    pack_registry.publish(_manifest("2.0.0"), root=root)
    assert pack_registry.unpublish("energy", "1.0.0", root=root) is True
    assert pack_registry.versions("energy", root=root) == ["2.0.0"]
    assert pack_registry.unpublish("energy", root=root) is True  # whole pack
    assert pack_registry.list_packs(root) == []


def test_default_root_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NOESIS_PACK_REGISTRY", str(tmp_path / "envreg"))
    assert pack_registry.default_root() == str(tmp_path / "envreg")
    pack_registry.publish(_manifest(), root=None)
    assert "energy" in pack_registry.list_packs(None)

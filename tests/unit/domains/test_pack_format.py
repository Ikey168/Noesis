"""M9.1: the distributable domain-pack format. A pack packages into
noesis-pack-v1 and its contents validate against the manifest contract; an
invalid manifest is caught and never written."""

import json
from pathlib import Path

import pytest

from src.domains import pack_format
from src.domains.pack_format import (
    PackFormatError,
    PackManifest,
    load_manifest,
    package_pack,
    validate_manifest,
)

REPO = Path(__file__).resolve().parents[3]


def _energy_manifest() -> PackManifest:
    return PackManifest(
        name="energy",
        version="1.0.0",
        description="Energy-sector pack",
        source_types=["news"],
        ui_flags={"energy": True},
        panels=[
            {
                "type": "energy_outages",
                "title": "Grid outages",
                "description": "Outages over time.",
                "endpoint": None,
                "facets": ["events", "trend"],
                "tables": ["news_articles"],
                "ui_flag": "energy",
                "default_span": 6,
                "topic_param": "topic",
            }
        ],
        planner_keywords={"trend": ["grid", "outage", "megawatt"]},
        enrichers=[
            {
                "name": "energy_tag",
                "kind": "keyword_tag",
                "description": "Tag energy docs.",
                "source_types": ["news"],
                "params": {"field": "content", "label": "energy", "keywords": ["grid", "outage"]},
            }
        ],
        provisioning_templates=[
            {
                "name": "energy_kg",
                "description": "Energy KG",
                "ontology": {"entities": ["utility"]},
                "sources": ["Energy Wire"],
                "backend": "table-prefix",
            }
        ],
    )


def test_valid_manifest_validates_clean():
    assert validate_manifest(_energy_manifest().to_dict()) == []


def test_manifest_round_trips_through_dict():
    manifest = _energy_manifest()
    rebuilt = PackManifest.from_dict(manifest.to_dict())
    assert rebuilt.to_dict() == manifest.to_dict()


def test_package_then_load_round_trip(tmp_path):
    path = package_pack(_energy_manifest(), str(tmp_path / "energy"))
    assert path.endswith("pack.json")
    loaded = load_manifest(str(tmp_path / "energy"))  # by directory
    assert loaded.name == "energy" and loaded.version == "1.0.0"
    # Loading the file path directly works too.
    assert load_manifest(path).to_dict() == _energy_manifest().to_dict()


def test_package_refuses_invalid_manifest(tmp_path):
    bad = _energy_manifest()
    bad.version = "not-a-version"
    with pytest.raises(PackFormatError):
        package_pack(bad, str(tmp_path / "bad"))
    assert not (tmp_path / "bad" / "pack.json").exists()  # nothing written


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(pack_format="wrong"), "pack_format"),
        (lambda d: d.update(name="Energy Pack!"), "name"),
        (lambda d: d.update(version="1.0"), "version"),
        (lambda d: d.update(source_types=["martian"]), "source_types"),
        (lambda d: d.update(ui_flags={"energy": "yes"}), "ui_flags"),
        (lambda d: d["panels"][0].update(facets=["not_a_facet"]), "facets"),
        (lambda d: d["panels"][0].update(default_span=99), "default_span"),
        (lambda d: d["enrichers"][0].update(kind="run_python"), "kind"),
        (lambda d: d["enrichers"][0]["params"].pop("keywords"), "keywords"),
        (lambda d: d["provisioning_templates"][0].update(backend="mysql"), "backend"),
        (lambda d: d["provisioning_templates"][0].update(name="BadName"), "name"),
    ],
)
def test_validation_catches_malformed_fields(mutate, needle):
    data = _energy_manifest().to_dict()
    mutate(data)
    errors = validate_manifest(data)
    assert any(needle in e for e in errors), (needle, errors)


def test_empty_pack_is_rejected():
    data = PackManifest(name="empty", version="1.0.0").to_dict()
    errors = validate_manifest(data)
    assert any("at least one capability" in e for e in errors)


def test_shipped_energy_pack_is_valid():
    # The distributable artifact under packs/ must always be contract-valid.
    path = REPO / "packs" / "energy" / "pack.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert validate_manifest(data) == []
    loaded = load_manifest(str(path))
    assert loaded.name == "energy"
    assert loaded.pack_format == pack_format.PACK_FORMAT

"""Pack taxonomy gate (ADR-004): every bundle and provider sits in the taxonomy.

``packs/taxonomy.json`` classifies each bundle by one domain and each provider
by record shapes and optional themes. A provider inherits its bundle's domain
unless it names its own. These tests keep the overlay complete, free of stale
entries and closed at the top level.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKS = ROOT / "packs"
TAXONOMY = json.loads((PACKS / "taxonomy.json").read_text())

# The top level is closed. Adding a domain needs a decision record superseding
# ADR-004; update this set in the same change.
ADR_004_DOMAINS = {
    "governance-law",
    "society-population",
    "economy-markets",
    "earth-environment",
    "science-knowledge",
    "health",
    "technology",
    "culture-leisure",
    "information-investigation",
}


def _bundle_dirs() -> set[str]:
    return {path.name for path in PACKS.iterdir() if path.is_dir() and not path.name.startswith((".", "_"))}


def _provider_bundles() -> dict[str, str]:
    """Provider id -> bundle directory, from the descriptors on disk."""

    found: dict[str, str] = {}
    for path in sorted(PACKS.glob("*/providers/*.json")):
        provider = json.loads(path.read_text())["id"]
        assert provider not in found, f"provider {provider} is declared by two bundles"
        found[provider] = path.parent.parent.name
    return found


def _infrastructure() -> set[str]:
    return set(TAXONOMY["infrastructure"]["packs"])


def _effective_domain(provider: str, bundle: str) -> str | None:
    entry = TAXONOMY["providers"].get(provider) or {}
    return entry.get("domain") or (TAXONOMY["packs"].get(bundle) or {}).get("domain")


def test_domains_are_the_closed_set_from_adr_004():
    assert set(TAXONOMY["domains"]) == ADR_004_DOMAINS


def test_every_bundle_has_exactly_one_domain_or_is_infrastructure():
    bundles = _bundle_dirs()
    classified = set(TAXONOMY["packs"])
    infrastructure = _infrastructure()

    assert not classified & infrastructure, "a bundle is both infrastructure and in a domain"
    assert bundles - classified - infrastructure == set(), "bundles missing from packs/taxonomy.json"
    assert (classified | infrastructure) - bundles == set(), "taxonomy names bundles that do not exist"
    for bundle, entry in TAXONOMY["packs"].items():
        assert entry["domain"] in TAXONOMY["domains"], bundle


def test_every_subject_provider_is_classified_and_no_entry_is_stale():
    on_disk = _provider_bundles()
    classified = TAXONOMY["providers"]
    infrastructure = _infrastructure()

    assert set(classified) - set(on_disk) == set(), "taxonomy names providers that do not exist"
    missing = {p for p, bundle in on_disk.items() if bundle not in infrastructure and p not in classified}
    assert missing == set(), "providers missing from packs/taxonomy.json"


def test_provider_entries_use_declared_shapes_themes_and_domains():
    on_disk = _provider_bundles()
    for provider, entry in TAXONOMY["providers"].items():
        bundle = on_disk[provider]
        shapes = entry.get("shapes") or []
        themes = entry.get("themes") or []
        assert shapes, f"{provider} names no record shape"
        assert len(set(shapes)) == len(shapes) and len(set(themes)) == len(themes), provider
        assert set(shapes) <= set(TAXONOMY["record_shapes"]), provider
        assert set(themes) <= set(TAXONOMY["themes"]), provider
        assert set(entry) <= {"domain", "shapes", "themes"}, provider
        if "domain" in entry:
            assert entry["domain"] in TAXONOMY["domains"], provider
            assert entry["domain"] != (TAXONOMY["packs"].get(bundle) or {}).get("domain"), (
                f"{provider} repeats its bundle's domain; drop the override")
        assert _effective_domain(provider, bundle), f"{provider} has no domain"


def test_every_domain_shape_and_theme_is_in_use():
    on_disk = _provider_bundles()
    used_domains = {entry["domain"] for entry in TAXONOMY["packs"].values()}
    used_domains |= {_effective_domain(p, on_disk[p]) for p in TAXONOMY["providers"]}
    used_shapes = {s for entry in TAXONOMY["providers"].values() for s in entry["shapes"]}
    used_themes = {t for entry in TAXONOMY["providers"].values() for t in entry.get("themes") or []}

    assert set(TAXONOMY["domains"]) - used_domains == set(), "empty domains are not a to-do list"
    assert set(TAXONOMY["record_shapes"]) - used_shapes == set()
    assert set(TAXONOMY["themes"]) - used_themes == set()

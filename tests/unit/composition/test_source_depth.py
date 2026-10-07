"""Source depth gate (ADR-006): every covered subdomain's publishers are computed from the source packs.

``packs/depth.json`` names, per classified provider, the source-pack sources it acquires, any publishers it reads
outside source packs (each with a file that quotes them), the providers it derives from, or why it has none. Every
source in every source pack resolves to exactly one publisher in the registry; a republisher counts as the publisher it
derives from. A subdomain's depth is the number of distinct publishers across the providers that name it. Covered
subdomains below the minimum depth are the depth program's thin table, and these tests keep the two equal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TAXONOMY = json.loads((ROOT / "packs/taxonomy.json").read_text())
DEPTH = json.loads((ROOT / "packs/depth.json").read_text())
PROGRAM = ROOT / "docs/roadmaps/source-depth-program.md"
SOURCE_PACK_FILES = sorted((ROOT / "config/source_packs").glob("*.json")) + sorted(ROOT.glob("packs/*/source_packs/*.json"))


def _source_publishers() -> dict[str, set[str | None]]:
    """Source id -> the publisher strings the source packs give it (one source can sit in several packs)."""

    found: dict[str, set[str | None]] = {}
    for path in SOURCE_PACK_FILES:
        for source in json.loads(path.read_text()).get("sources", []):
            found.setdefault(source["source_id"], set()).add(source.get("publisher"))
    return found


SOURCES = _source_publishers()


def _resolve(source_id: str, publisher: str | None) -> list[str]:
    explicit = [pid for pid, entry in DEPTH["publishers"].items() if source_id in entry.get("sources", [])]
    if explicit:
        return explicit
    if publisher is None:
        return []
    return [pid for pid, entry in DEPTH["publishers"].items()
            if any(publisher.startswith(prefix) for prefix in entry.get("match", []))]


def _origin(publisher: str) -> str:
    return DEPTH["publishers"][publisher].get("derived_from") or publisher


def _provider_publishers(provider: str, seen: frozenset[str] = frozenset()) -> set[str]:
    assert provider not in seen, f"derives_from cycle through {provider}"
    entry = DEPTH["providers"][provider]
    publishers = set()
    for source_id in entry.get("sources", []):
        for publisher in SOURCES[source_id]:
            publishers.update(_origin(p) for p in _resolve(source_id, publisher))
    publishers.update(_origin(p["id"]) for p in entry.get("publishers", []))
    for parent in entry.get("derives_from", []):
        publishers |= _provider_publishers(parent, seen | {provider})
    return publishers


def depth_by_subdomain() -> dict[str, set[str]]:
    """Covered subdomain -> the distinct origin publishers behind it."""

    depth: dict[str, set[str]] = {}
    for provider, entry in TAXONOMY["providers"].items():
        for subdomain in entry["subdomains"]:
            depth.setdefault(subdomain, set()).update(_provider_publishers(provider))
    return depth


def test_top_level_is_closed():
    assert set(DEPTH) == {"depth_format", "version", "description", "minimum_depth", "publishers", "providers"}
    assert DEPTH["depth_format"] == 1
    assert isinstance(DEPTH["minimum_depth"], int) and DEPTH["minimum_depth"] >= 2


def test_every_classified_provider_has_a_depth_entry_and_no_entry_is_stale():
    assert set(DEPTH["providers"]) == set(TAXONOMY["providers"])


def test_provider_entries_are_well_formed():
    for provider, entry in DEPTH["providers"].items():
        assert set(entry) <= {"sources", "publishers", "derives_from", "none"}, provider
        if "none" in entry:
            assert set(entry) == {"none"} and entry["none"].strip(), f"{provider}: 'none' carries a reason and stands alone"
            continue
        assert entry.get("sources") or entry.get("publishers") or entry.get("derives_from"), provider
        sources = entry.get("sources", [])
        assert len(set(sources)) == len(sources), f"{provider} lists a source twice"
        for source_id in sources:
            assert source_id in SOURCES, f"{provider} names {source_id}, which no source pack declares"
        for parent in entry.get("derives_from", []):
            assert parent in DEPTH["providers"] and parent != provider, f"{provider} derives from unknown {parent}"


def test_publishers_read_outside_source_packs_are_quoted_by_the_named_file():
    for provider, entry in DEPTH["providers"].items():
        for item in entry.get("publishers", []):
            assert set(item) == {"id", "evidence", "quote"}, provider
            assert item["id"] in DEPTH["publishers"], f"{provider} names unknown publisher {item['id']}"
            path = ROOT / item["evidence"]
            assert path.is_file(), f"{provider}: {item['evidence']} does not exist"
            assert item["quote"] in path.read_text(), f"{provider}: {item['evidence']} does not quote {item['quote']!r}"


def test_every_source_resolves_to_exactly_one_publisher():
    unresolved, ambiguous = [], []
    for source_id, publishers in SOURCES.items():
        resolved = {tuple(_resolve(source_id, p)) for p in publishers}
        if any(len(r) == 0 for r in resolved):
            unresolved.append((source_id, sorted(map(str, publishers))))
        elif any(len(r) > 1 for r in resolved) or len(resolved) > 1:
            ambiguous.append((source_id, sorted(resolved)))
    assert unresolved == [], f"add a publisher (match prefix or explicit source) for {unresolved}"
    assert ambiguous == [], f"a source must resolve to one publisher: {ambiguous}"


def test_publisher_registry_is_well_formed_and_used():
    used: set[str] = set()
    for source_id, publishers in SOURCES.items():
        for publisher in publishers:
            used.update(_resolve(source_id, publisher))
    for entry in DEPTH["providers"].values():
        used.update(item["id"] for item in entry.get("publishers", []))
    for pid, entry in DEPTH["publishers"].items():
        assert set(entry) <= {"name", "match", "sources", "derived_from"} and entry["name"], pid
        for source_id in entry.get("sources", []):
            assert source_id in SOURCES, f"{pid} claims unknown source {source_id}"
        origin = entry.get("derived_from")
        if origin:
            assert origin in DEPTH["publishers"], f"{pid} derives from unknown {origin}"
            assert "derived_from" not in DEPTH["publishers"][origin], f"{pid}: derive from the origin directly"
            used.add(origin)
    assert set(DEPTH["publishers"]) - used == set(), "registry entries no source or provider uses"


def test_every_provider_with_sources_has_a_publisher():
    for provider, entry in DEPTH["providers"].items():
        if "none" not in entry:
            assert _provider_publishers(provider), f"{provider} resolves to no publisher"


def test_thin_subdomains_match_the_program_thin_table():
    """A covered subdomain below the minimum depth is thin; the program's thin table lists exactly those."""

    depth = depth_by_subdomain()
    thin = {s for s, publishers in depth.items() if len(publishers) < DEPTH["minimum_depth"]}
    listed = re.findall(r"^\| `([a-z0-9-]+)` \|", PROGRAM.read_text(), re.MULTILINE)

    assert len(listed) == len(set(listed)), "a subdomain appears twice in the thin table"
    assert set(listed) - thin == set(), "thin table lists subdomains that now meet the minimum depth; remove their rows"
    assert thin - set(listed) == set(), "thin subdomains missing from the thin table"


def test_depth_two_subdomains_match_the_program_watchlist():
    """Subdomains exactly at the minimum depth are listed, so a lost publisher is visible before a row turns thin."""

    depth = depth_by_subdomain()
    at_minimum = {s for s, publishers in depth.items() if len(publishers) == DEPTH["minimum_depth"]}
    listed = re.findall(r"^\| _([a-z0-9-]+)_ \|", PROGRAM.read_text(), re.MULTILINE)

    assert len(listed) == len(set(listed)), "a subdomain appears twice in the watchlist"
    assert set(listed) == at_minimum, "the depth-two watchlist differs from the computed depth"

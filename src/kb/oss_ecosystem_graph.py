"""Dependency graphs as of a date: declared-constraint resolution with stated semantics (OS07).

A pure, bounded query over the OSS store. Each declared constraint of a
release is resolved to the highest release of the dependency that was
published on or before the date, acquired by the knowledge cutoff and
available then under the ecosystem's own rules:

* **PyPI** - PEP 440 specifiers; yanked releases are skipped unless the
  constraint pins them with ``==``/``===`` (PEP 592);
* **npm** - node-semver ranges; deprecated releases stay eligible (npm installs
  them with a warning), unpublished ones do not;
* **Cargo** - Cargo requirements; yanked releases are skipped;
* **Maven** - a plain version is a soft requirement that resolves to itself;
  ranges resolve to the highest match. No yank exists.

This is *not* an observed install: there is no lockfile, conflict resolution
or deduplication by an ecosystem resolver, and each edge is resolved on its
own. Scopes other than those requested (by default ``runtime``) and peer or
optional dependencies are reported, not resolved; environment markers are
reported and evaluated only when an environment is given. Every edge that
cannot be resolved - unsatisfiable, unsupported syntax, a package outside the
acquired selection, the depth or node limit - is listed with its reason.

Where deps.dev published a graph for the root release it is shown beside
Noesis's resolution with the differences listed; it is never merged.
Pinned inventories (``src/domains/technical/inventory.py``) stay the owner of
observed lockfiles. Every answer carries a replayable receipt pinning the
release-state and dependency revisions it used.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.oss_ecosystem_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    SCOPES,
    canonical,
    coordinate,
    digest,
)
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError, authorize
from src.kb.oss_ecosystem_versions import (
    UnsupportedConstraint,
    exact_pin,
    normalise_version,
    satisfies,
    sort_key,
)
from src.kb.oss_ecosystem_views import (
    AVAILABLE_STATES,
    cutoff_ms,
    iso,
    registry_source,
    release_states,
)

GRAPH_CONTRACT = "noesis-oss-dependency-graph-v1"
MAX_DEPTH = 5
MAX_NODES = 200
SEMANTICS = (
    "declared-constraint resolution, not an observed lockfile: each declared constraint is resolved on its own to "
    "the highest release published on or before the date, acquired by the knowledge cutoff and available then "
    "(PyPI: yanked skipped unless pinned with ==/=== per PEP 592; Cargo: yanked skipped; npm: deprecated eligible, "
    "unpublished not; Maven: a soft requirement resolves to the declared version, a range to its highest match); "
    "no conflict resolution, deduplication or lockfile of an ecosystem resolver is applied"
)


def _available(
    ecosystem: str, release: Mapping[str, Any], constraint: str | None
) -> tuple[bool, str | None]:
    state = release["state"]
    if state in AVAILABLE_STATES:
        return True, None
    if state == "yanked":
        pinned = exact_pin(ecosystem, constraint)
        if (
            ecosystem == "pypi"
            and pinned is not None
            and normalise_version("pypi", pinned) == release["version"]
        ):
            return True, "yanked, but pinned exactly (PEP 592)"
        return False, "yanked"
    return False, state


def _marker(
    entry: Mapping[str, Any], environment: Mapping[str, str] | None
) -> tuple[bool, str | None]:
    marker = entry.get("marker")
    if not marker or entry.get("extra"):
        return True, None
    if environment is None:
        return True, "environment marker reported, not evaluated"
    from packaging.markers import InvalidMarker, Marker, UndefinedEnvironmentName

    try:
        return bool(
            Marker(marker).evaluate(dict(environment))
        ), "environment marker evaluated"
    except (InvalidMarker, UndefinedEnvironmentName) as exc:
        return True, f"environment marker could not be evaluated: {exc}"


class DependencyGraphs:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = OssEcosystemStore(conn, initialize=False)

    def _dependency_set(
        self,
        namespace: str,
        coord: str,
        version: str,
        *,
        at_ms: int,
        acquired_by_ms: int,
    ):
        records = self.store.records(
            namespace,
            record_type="declared_dependency_set",
            source=registry_source(coord),
            coordinate=coord,
            version=version,
        )
        if not records:
            return None
        return self.store.current(
            records[0]["record_id"], acquired_by_ms=acquired_by_ms, at_ms=at_ms
        ) or self.store.current(records[0]["record_id"], acquired_by_ms=acquired_by_ms)

    def graph(
        self,
        namespace: str,
        package: str,
        version: str,
        date: Any,
        *,
        scopes: Iterable[str],
        ecosystem: str | None = None,
        depth: int = 3,
        max_nodes: int = MAX_NODES,
        include_scopes: Iterable[str] = ("runtime",),
        environment: Mapping[str, str] | None = None,
        acquired_by: Any = None,
    ) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready(namespace)
        root_coord = (
            package
            if str(package).startswith("pkg:")
            else coordinate(str(ecosystem or ""), package)
        )
        eco = root_coord.split(":")[1]
        if not 0 <= int(depth) <= MAX_DEPTH or not 1 <= int(max_nodes) <= MAX_NODES:
            raise OssStoreError(
                "bad_request", f"depth is 0-{MAX_DEPTH} and max_nodes 1-{MAX_NODES}"
            )
        wanted = sorted(set(include_scopes))
        if set(wanted) - set(SCOPES):
            raise OssStoreError("bad_request", f"scopes are {SCOPES}")
        at = cutoff_ms(date)
        acquired = cutoff_ms(acquired_by, default=at)
        root_version = normalise_version(eco, version)
        params = {
            "namespace": namespace,
            "package": root_coord,
            "version": root_version,
            "date": str(date),
            "at_ms": at,
            "acquired_by_ms": acquired,
            "depth": int(depth),
            "max_nodes": int(max_nodes),
            "scopes": wanted,
            "environment": dict(environment) if environment else None,
        }
        cache: dict[str, list[dict[str, Any]]] = {}

        def releases(coord: str) -> list[dict[str, Any]]:
            if coord not in cache:
                cache[coord] = release_states(
                    self.store,
                    namespace,
                    coord,
                    source=registry_source(coord),
                    at_ms=at,
                    acquired_by_ms=acquired,
                )
            return cache[coord]

        pins: set[str] = set()
        gaps: list[dict[str, Any]] = []
        root = next(
            (r for r in releases(root_coord) if r["version"] == root_version), None
        )
        nodes: dict[str, dict[str, Any]] = {}
        edges, unresolved, reported = [], [], []
        if root is None or root["state"] in {"not_yet_published", "unknown"}:
            unresolved.append(
                {
                    "from": None,
                    "to": root_coord,
                    "constraint": root_version,
                    "reason": "root_not_known"
                    if root is None
                    else f"root_{root['state']}",
                }
            )
        else:
            pins.add(root["revision_id"])
            if root.get("gap"):
                gaps.append({"node": f"{root_coord}@{root_version}", **root["gap"]})
            root_key = f"{root_coord}@{root_version}"
            nodes[root_key] = {
                "id": root_key,
                "coordinate": root_coord,
                "version": root_version,
                "depth": 0,
                "state": root["state"],
                "release_revision_id": root["revision_id"],
            }
            queue = deque([(root_coord, root_version, 0)])
            while queue:
                coord, ver, level = queue.popleft()
                node_id = f"{coord}@{ver}"
                deps = self._dependency_set(
                    namespace, coord, ver, at_ms=at, acquired_by_ms=acquired
                )
                if deps is None:
                    nodes[node_id]["dependencies"] = "not_acquired"
                    unresolved.append(
                        {
                            "from": node_id,
                            "to": None,
                            "constraint": None,
                            "reason": "declared dependencies of this release were not acquired",
                        }
                    )
                    continue
                pins.add(deps["revision_id"])
                nodes[node_id]["dependency_set_revision_id"] = deps["revision_id"]
                for entry in deps["statement"]["entries"]:
                    edge = {
                        "from": node_id,
                        "to": entry.get("coordinate"),
                        "name": entry["name"],
                        "constraint": entry.get("constraint"),
                        "scope": entry["scope"],
                        **({"marker": entry["marker"]} if entry.get("marker") else {}),
                    }
                    if entry["scope"] not in wanted:
                        reported.append(
                            {
                                **edge,
                                "reason": f"{entry['scope']} dependency reported, not resolved",
                            }
                        )
                        continue
                    keep, note = _marker(entry, environment)
                    if note:
                        edge["marker_note"] = note
                    if not keep:
                        reported.append(
                            {**edge, "reason": "excluded by its environment marker"}
                        )
                        continue
                    if entry.get("unsupported") or not entry.get("coordinate"):
                        unresolved.append(
                            {**edge, "reason": "unsupported dependency coordinate"}
                        )
                        continue
                    target = entry["coordinate"]
                    target_eco = target.split(":")[1]
                    if level + 1 > int(depth):
                        unresolved.append({**edge, "reason": f"depth_limit ({depth})"})
                        continue
                    candidates = releases(target)
                    if not candidates:
                        unresolved.append(
                            {
                                **edge,
                                "reason": "package not acquired (outside the declared selection)",
                            }
                        )
                        continue
                    if entry.get("constraint") is None:
                        unresolved.append(
                            {
                                **edge,
                                "reason": "no version declared (managed by a parent POM or BOM, "
                                "which v1 does not read)"
                                if target_eco == "maven"
                                else "no constraint declared",
                            }
                        )
                        continue
                    chosen, considered, error = None, [], None
                    for release in candidates:
                        if release["state"] in {"not_yet_published", "unknown"}:
                            continue
                        try:
                            if not satisfies(
                                target_eco, entry["constraint"], release["version"]
                            ):
                                continue
                        except UnsupportedConstraint as exc:
                            error = exc
                            break
                        ok, why = _available(target_eco, release, entry["constraint"])
                        considered.append(
                            {
                                "version": release["version"],
                                "state": release["state"],
                                **({"note": why} if why else {}),
                            }
                        )
                        if ok and (
                            chosen is None
                            or sort_key(target_eco, release["version"])
                            > sort_key(target_eco, chosen["version"])
                        ):
                            chosen = {**release, **({"note": why} if why else {})}
                    if error is not None:
                        unresolved.append({**edge, "reason": f"{error.code}: {error}"})
                        continue
                    if chosen is None:
                        unresolved.append(
                            {
                                **edge,
                                "reason": "unsatisfiable: no release published by the date and "
                                "available then satisfies the constraint",
                                "matching_but_unavailable": considered,
                            }
                        )
                        continue
                    target_id = f"{target}@{chosen['version']}"
                    edge.update(
                        {
                            "to": target_id,
                            "resolved_version": chosen["version"],
                            "release_revision_id": chosen["revision_id"],
                        }
                    )
                    if chosen.get("note"):
                        edge["note"] = chosen["note"]
                    if target_id not in nodes:
                        if len(nodes) >= int(max_nodes):
                            unresolved.append(
                                {**edge, "reason": f"node_limit ({max_nodes})"}
                            )
                            continue
                        nodes[target_id] = {
                            "id": target_id,
                            "coordinate": target,
                            "version": chosen["version"],
                            "depth": level + 1,
                            "state": chosen["state"],
                            "release_revision_id": chosen["revision_id"],
                        }
                        if chosen.get("gap"):
                            gaps.append({"node": target_id, **chosen["gap"]})
                        queue.append((target, chosen["version"], level + 1))
                    pins.add(chosen["revision_id"])
                    edges.append(edge)
        published = self._published(
            namespace, root_coord, root_version, nodes, acquired
        )
        core = {
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges, key=canonical),
            "unresolved": sorted(unresolved, key=canonical),
            "reported_not_resolved": sorted(reported, key=canonical),
        }
        receipt = {
            "contract": GRAPH_CONTRACT,
            "parameters": params,
            "pinned_revisions": sorted(pins),
            "result_digest": digest(core),
        }
        receipt["receipt_id"] = "oss-graph:" + digest(receipt)[:24]
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "dependency_graph_as_of",
            "namespace": namespace,
            "root": {"coordinate": root_coord, "version": root_version},
            "date": str(date),
            "knowledge_cutoff_ms": acquired,
            "knowledge_cutoff": iso(acquired),
            "generation": self.store.generation(namespace, acquired_by_ms=acquired),
            "semantics": SEMANTICS,
            "scopes_resolved": wanted,
            "limits": {"depth": int(depth), "max_nodes": int(max_nodes)},
            **core,
            "gaps": gaps,
            "published_graphs": published,
            "receipt": receipt,
            "notice": "no quality, health, popularity or trust verdict is expressed",
        }

    def _published(self, namespace, root_coord, root_version, nodes, acquired):
        records = self.store.records(
            namespace,
            record_type="published_dependency_graph",
            coordinate=root_coord,
            version=root_version,
        )
        out = []
        ours = {n["coordinate"]: n["version"] for n in nodes.values() if n["depth"] > 0}
        eco = root_coord.split(":")[1]
        for record in records:
            revision = self.store.current(record["record_id"], acquired_by_ms=acquired)
            if revision is None:
                continue
            graph = revision["statement"]
            theirs = {}
            for node in graph["nodes"]:
                if node.get("relation") == "SELF" or not node.get("name"):
                    continue
                try:
                    theirs[coordinate(eco, node["name"])] = normalise_version(
                        eco, str(node["version"])
                    )
                except Exception:  # noqa: BLE001 - a node outside the canonical coordinates stays unmatched
                    continue
            differences = []
            for coord in sorted(set(ours) | set(theirs)):
                if ours.get(coord) != theirs.get(coord):
                    differences.append(
                        {
                            "coordinate": coord,
                            "noesis": ours.get(coord),
                            record["source"]: theirs.get(coord),
                        }
                    )
            out.append(
                {
                    "source": record["source"],
                    "revision_id": revision["revision_id"],
                    "observed_at_ms": revision["observed_at_ms"],
                    "semantics": graph["semantics"],
                    "nodes": graph["nodes"],
                    "edges": graph["edges"],
                    "differences": differences,
                }
            )
        return out

    def replay(
        self, receipt: Mapping[str, Any], *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Recompute a graph from its receipt's parameters; the pinned revisions and the result must match."""

        params = dict(receipt["parameters"])
        missing = [
            r for r in receipt["pinned_revisions"] if self.store.revision(r) is None
        ]
        again = self.graph(
            params["namespace"],
            params["package"],
            params["version"],
            params["date"],
            scopes=scopes,
            depth=params["depth"],
            max_nodes=params["max_nodes"],
            include_scopes=params["scopes"],
            environment=params["environment"],
            acquired_by=params["acquired_by_ms"],
        )
        return {
            "receipt_id": receipt["receipt_id"],
            "matched": not missing
            and again["receipt"]["result_digest"] == receipt["result_digest"]
            and again["receipt"]["pinned_revisions"] == receipt["pinned_revisions"],
            "missing_revisions": missing,
            "replayed_receipt_id": again["receipt"]["receipt_id"],
        }


def dependency_graph_as_of(
    conn: Any,
    namespace: str,
    package: str,
    version: str,
    date: Any,
    *,
    scopes: Iterable[str],
    **options: Any,
) -> dict[str, Any]:
    return DependencyGraphs(conn).graph(
        namespace, package, version, date, scopes=scopes, **options
    )


__all__ = [
    "GRAPH_CONTRACT",
    "MAX_DEPTH",
    "MAX_NODES",
    "SEMANTICS",
    "DependencyGraphs",
    "dependency_graph_as_of",
]

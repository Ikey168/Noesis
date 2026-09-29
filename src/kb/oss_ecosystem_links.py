"""Advisories and inventories beside OSS package histories and graphs, by citation only (OS09).

Advisories come from ``technology.vulnerabilities`` through its read API
(:meth:`VulnerabilityQueries.component_advisories` for the exact canonical
coordinate, plus accepted component matches from
:class:`src.kb.vulnerability_identity.VulnerabilityIdentity`). Each advisory is
cited by source, native id, series, revision and content digest; the OSS pack
stores no advisory data and never proposes CVE or CPE matches.

A technical inventory (``src/domains/technical/inventory.py``, the owner of
observed lockfiles) can be compared with the declared-constraint graph of the
same root: each pinned entry is shown with the release state its registry
stated on the inventory date (yanked, deprecated, unpublished) and the version
the graph resolved, differences listed. Nothing here writes a vulnerability,
inventory or technical table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.oss_ecosystem_graph import DependencyGraphs
from src.kb.oss_ecosystem_records import ANSWER_CONTRACT, READ_SCOPE, coordinate
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError, authorize
from src.kb.oss_ecosystem_versions import (
    UnsupportedConstraint,
    compare,
    normalise_version,
)
from src.kb.oss_ecosystem_views import iso, registry_source, release_states

TECHNICAL_READ = "knowledge:technical:read"


def _range_check(
    ecosystem: str, version: str, item: Mapping[str, Any]
) -> dict[str, Any]:
    """Where a version sits against the cited OSV events, by the ecosystem's ordering; not a verdict."""

    if item.get("range_type") not in {"ECOSYSTEM", "SEMVER"}:
        return {
            "state": "not_checked",
            "reason": f"{item.get('range_type')} ranges are read by the Technology tools",
        }
    events = list(item.get("events") or [])
    try:
        inside = False
        for event in events:
            if "introduced" in event and (
                str(event["introduced"]) == "0"
                or compare(ecosystem, version, str(event["introduced"])) >= 0
            ):
                inside = True
            if (
                "fixed" in event
                and compare(ecosystem, version, str(event["fixed"])) >= 0
            ):
                inside = False
            if (
                "last_affected" in event
                and compare(ecosystem, version, str(event["last_affected"])) > 0
            ):
                inside = False
            if (
                "limit" in event
                and compare(ecosystem, version, str(event["limit"])) >= 0
            ):
                inside = False
    except UnsupportedConstraint as exc:
        return {"state": "not_checked", "reason": str(exc)}
    return {
        "state": "within_cited_range" if inside else "outside_cited_range",
        "note": "a position against the cited range events, not a verdict",
    }


class OssLinks:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = OssEcosystemStore(conn, initialize=False)

    def _vulnerabilities_ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='vuln_revisions'"
            ).fetchone()
        )

    def advisories_for(
        self,
        vulnerability_namespace: str,
        coord: str,
        *,
        scopes: Iterable[str],
        version: str | None = None,
        as_of: Any = None,
    ) -> dict[str, Any]:
        from src.kb.vulnerability_identity import VulnerabilityIdentity
        from src.kb.vulnerability_queries import VulnerabilityQueries

        if not self._vulnerabilities_ready():
            raise OssStoreError(
                "not_ready", "technology.vulnerabilities has no acquired advisory yet"
            )
        answer = VulnerabilityQueries(self.conn).component_advisories(
            vulnerability_namespace, scopes=scopes, coordinate=coord, as_of=as_of
        )
        eco = coord.split(":")[1]
        citations = []
        for item in answer["vulnerabilities"]:
            ranges = []
            for rng in item.get("ranges") or []:
                entry = {
                    "cites": rng.get("cites"),
                    "range_type": rng.get("range_type"),
                    "package": rng.get("package"),
                    "events": rng.get("events"),
                }
                if version is not None:
                    entry["range_check"] = _range_check(
                        eco, normalise_version(eco, version), rng
                    )
                ranges.append(entry)
            citations.append(
                {
                    "cve_id": item["identity"].get("cve_id"),
                    "advisories": [
                        {
                            k: a.get(k)
                            for k in (
                                "source",
                                "native_id",
                                "series_id",
                                "revision_id",
                                "revision_no",
                                "content_digest",
                                "lifecycle",
                                "modified",
                            )
                        }
                        for a in item.get("advisories") or []
                    ],
                    "ranges": ranges,
                }
            )
        matches = [
            {
                "match_id": m["match_id"],
                "target_key": m["target_key"],
                "decision_id": m.get("decision_id"),
            }
            for m in VulnerabilityIdentity(
                self.conn, initialize=False
            ).accepted_targets(vulnerability_namespace, coord)
        ]
        return {
            "coordinate": coord,
            "state": answer["state"],
            "citations": citations,
            "accepted_component_matches": matches,
            **({"reason": answer["reason"]} if answer.get("reason") else {}),
            "owner": "technology.vulnerabilities (cited, not copied)",
        }

    def package_advisories(
        self,
        namespace: str,
        package: str,
        *,
        scopes: Iterable[str],
        ecosystem: str | None = None,
        version: str | None = None,
        vulnerability_namespace: str | None = None,
        as_of: Any = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready(namespace)
        coord = (
            package
            if str(package).startswith("pkg:")
            else coordinate(str(ecosystem or ""), package)
        )
        body = self.advisories_for(
            vulnerability_namespace or namespace,
            coord,
            scopes=scopes,
            version=version,
            as_of=as_of,
        )
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "package_advisories",
            "namespace": namespace,
            "vulnerability_namespace": vulnerability_namespace or namespace,
            "version": version,
            **body,
            **({"as_of": str(as_of)} if as_of is not None else {}),
            "generation": self.store.generation(namespace),
            "notice": "advisories are cited from technology.vulnerabilities; no exploitability or risk verdict",
        }

    def graph_advisories(
        self,
        graph: Mapping[str, Any],
        *,
        scopes: Iterable[str],
        vulnerability_namespace: str,
    ) -> dict[str, Any]:
        """Advisory citations per node of an as-of graph."""

        out = []
        for node in graph["nodes"]:
            cited = self.advisories_for(
                vulnerability_namespace,
                node["coordinate"],
                scopes=scopes,
                version=node["version"],
            )
            out.append(
                {
                    "node": node["id"],
                    **{
                        k: cited[k]
                        for k in ("state", "citations", "accepted_component_matches")
                    },
                }
            )
        return {"graph_receipt_id": graph["receipt"]["receipt_id"], "nodes": out}

    def compare_inventory(
        self,
        namespace: str,
        inventory_id: str,
        package: str,
        version: str,
        *,
        owner_id: str,
        scopes: Iterable[str],
        ecosystem: str | None = None,
        inventory_date: Any = None,
        depth: int = 3,
    ) -> dict[str, Any]:
        """A pinned inventory beside the as-of graph of the same root, with pins yanked or deprecated then."""

        from src.domains.technical.inventory import InventoryError, InventoryStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if TECHNICAL_READ not in scopes and "operator" not in scopes:
            raise OssStoreError(
                "unauthorized", f"{TECHNICAL_READ} is required to read an inventory"
            )
        self.store.require_ready(namespace)
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='technical_inventories'"
        ).fetchone():
            raise OssStoreError("not_ready", "no technical inventory has been imported")
        try:
            page = InventoryStore(self.conn, initialize=False).inspect(
                inventory_id, owner_id=owner_id, limit=100
            )
        except InventoryError as exc:
            raise OssStoreError(exc.code, str(exc)) from exc
        imported = self.conn.execute(
            "SELECT imported_at_ms FROM technical_inventories WHERE inventory_id=?",
            [inventory_id],
        ).fetchone()[0]
        date = inventory_date or iso(int(imported))
        graph = DependencyGraphs(self.conn).graph(
            namespace,
            package,
            version,
            date,
            scopes=scopes,
            ecosystem=ecosystem,
            depth=depth,
        )
        resolved = {n["coordinate"]: n["version"] for n in graph["nodes"]}
        from src.kb.oss_ecosystem_views import cutoff_ms

        at = cutoff_ms(date)
        entries, flagged = [], []
        for entry in page["entries"]:
            if entry.get("status") != "pinned" or not entry.get("coordinate"):
                entries.append(
                    {
                        "entry": entry["name"],
                        "status": entry.get("status"),
                        "reason": "not a pinned entry",
                    }
                )
                continue
            coord, pinned = (
                entry["coordinate"],
                normalise_version(entry["ecosystem"], entry["version"]),
            )
            state = next(
                (
                    r
                    for r in release_states(
                        self.store,
                        namespace,
                        coord,
                        source=registry_source(coord),
                        at_ms=at,
                        acquired_by_ms=at,
                    )
                    if r["version"] == pinned
                ),
                None,
            )
            item = {
                "coordinate": coord,
                "pinned": pinned,
                "graph_resolved": resolved.get(coord),
                "release_state_on_inventory_date": state["state"]
                if state
                else "not_acquired",
            }
            if state and state.get("reason"):
                item["reason"] = state["reason"]
            if state:
                item["release_revision_id"] = state["revision_id"]
            if state and state["state"] in {
                "yanked",
                "deprecated",
                "unpublished",
                "not_observed",
            }:
                flagged.append(item)
            entries.append(item)
        differences = [
            e
            for e in entries
            if e.get("coordinate") and e["graph_resolved"] != e["pinned"]
        ]
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "inventory_beside_graph",
            "namespace": namespace,
            "inventory_id": inventory_id,
            "inventory_hash": page["inventory_hash"],
            "inventory_date": date,
            "entries": entries,
            "pinned_but_yanked_or_deprecated": flagged,
            "differences": differences,
            "graph": {
                "receipt": graph["receipt"],
                "semantics": graph["semantics"],
                "unresolved": graph["unresolved"],
            },
            "knowledge_cutoff_ms": graph["knowledge_cutoff_ms"],
            "generation": graph["generation"],
            "notice": "the inventory is an observed lockfile owned by the Technology pack; the graph is a "
            "declared-constraint resolution; neither is a verdict",
        }


__all__ = ["OssLinks"]

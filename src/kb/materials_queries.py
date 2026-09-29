"""Material lookup and the cited property dossier (MT09-MT10, #2087, #2088).

``lookup_material`` finds acquired records by record key, source ID or
formula, with composition group, phase group and pending matches.
``material_properties`` is the dossier: for every value its unit (native and
normalised), conditions, method provenance, uncertainty as published, dataset
release, locator and runtime document, and its citations split into
value-level and dataset-level, resolved by DOI or standard designation only.
Values from different sources are listed side by side, never combined.
"""

from __future__ import annotations

from typing import Any

from src.kb import materials_records as mr
from src.kb.materials_citations import CitationResolver
from src.kb.materials_comparison import MaterialsComparison
from src.kb.materials_identity import KEY_PREFIX, group_of, phase_groups
from src.kb.materials_records import READ_SCOPE
from src.kb.materials_store import (
    MaterialsError,
    MaterialsStore,
    authorize,
    table_exists,
)

DOSSIER_CONTRACT = "noesis-material-dossier-v1"


class MaterialsQueries:
    def __init__(self, conn):
        self.conn = conn
        self.store = MaterialsStore(conn, initialize=False)
        self.comparison = MaterialsComparison(conn)

    def lookup(self, namespace, query, *, scopes, as_of_ms=None):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        text = str(query or "").strip()
        if not text:
            raise MaterialsError(
                "invalid_request", "query is a formula, a record key or a source ID"
            )
        entries = self.store.entries(namespace, as_of_ms=as_of_ms)
        exact = [
            e
            for e in entries
            if text
            in {
                f"{KEY_PREFIX}{e['provider']}:{e['native_id']}",
                e["native_id"],
                e["entry_id"],
            }
        ]
        basis = "record key or source ID"
        if not exact:
            try:
                formula = mr.material(text)["reduced_formula"]
            except mr.MaterialRecordError:
                formula = None
            exact = [e for e in entries if formula and e["reduced_formula"] == formula]
            basis = "reduced formula (composition level)"
        groups = phase_groups(self.conn, namespace)
        pending = {}
        if table_exists(self.conn, "ownership_identity_candidates"):
            for left, right, state in self.conn.execute(
                "SELECT left_key, right_key, state FROM ownership_identity_candidates WHERE namespace=? AND "
                "left_key LIKE ? AND right_key LIKE ?",
                [namespace, KEY_PREFIX + "%", KEY_PREFIX + "%"],
            ).fetchall():
                if state == "proposed":
                    pending.setdefault(left, []).append(right)
                    pending.setdefault(right, []).append(left)
        records = []
        for item in exact:
            entry = self.store.entry(namespace, item["entry_id"], as_of_ms=as_of_ms)
            content = entry["content"]
            records.append(
                {
                    k: v
                    for k, v in {
                        "record_key": entry["record_key"],
                        "provider": entry["provider"],
                        "native_id": entry["native_id"],
                        "material": content["material"],
                        "structure": content.get("structure"),
                        "status": content["status"],
                        "identifiers": content.get("identifiers"),
                        "release": entry["release"],
                        "locator": content["locator"],
                        "phase_group": group_of(
                            groups, entry["provider"], entry["native_id"]
                        ),
                        "composition_group": entry["reduced_formula"],
                        "pending_matches": sorted(pending.get(entry["record_key"], []))
                        or None,
                    }.items()
                    if v is not None
                }
            )
        return {
            "query": text,
            "basis": basis,
            "records": records,
            "n": len(records),
            "notice": "records are listed per source; a composition group is not a phase identity",
            "snapshot_id": self.store.snapshot_id(namespace, as_of_ms=as_of_ms),
        }

    def properties(
        self,
        namespace,
        material,
        *,
        scopes,
        prop=None,
        as_of_ms=None,
        standards_namespace=None,
    ):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if prop is not None:
            mr.property_definition(prop)
        entry_ids, scope = self.comparison.resolve(
            namespace, material, as_of_ms=as_of_ms
        )
        resolver = CitationResolver(
            self.conn, standards_namespace=standards_namespace, scopes=scopes
        )
        values = self.comparison._values(namespace, entry_ids, prop, as_of_ms)
        by_property: dict[str, list[dict[str, Any]]] = {}
        datasets: dict[str, dict[str, Any]] = {}
        for value in sorted(
            values,
            key=lambda v: (
                v["property"],
                v["provider"],
                v["native_id"],
                v["series_key"],
            ),
        ):
            entry = self.store.entry(namespace, value["entry_id"], as_of_ms=as_of_ms)
            view = self.comparison.view(value)
            view["citations"] = resolver.split(value.get("references") or [], [])
            view.pop("references", None)
            view["retrieved_at"] = entry["content"]["retrieved_at"]
            by_property.setdefault(value["property"], []).append(view)
            datasets.setdefault(
                value["provider"],
                {
                    "provider": value["provider"],
                    "release": value["release"],
                    "citations": resolver.split(
                        [], entry["content"].get("references") or []
                    )["dataset_level"],
                },
            )
        structures = []
        for eid in entry_ids:
            entry = self.store.entry(namespace, eid, as_of_ms=as_of_ms)
            content = entry["content"]
            if content.get("structure"):
                structures.append(
                    {
                        "record_key": entry["record_key"],
                        "structure": content["structure"],
                        "release": entry["release"],
                        "locator": content["locator"],
                        "citations": resolver.split(
                            content.get("references") or [], []
                        )["value_level"],
                    }
                )
        properties = [
            {
                "property": mr.property_definition(p),
                "values": items,
                "note": "values from different sources and methods are listed side by side; use "
                "compare_material_property for condition-aware alignment",
            }
            for p, items in sorted(by_property.items())
        ]
        return {
            "contract": DOSSIER_CONTRACT,
            "material": material,
            "scope": scope,
            "properties": properties,
            "structures": structures,
            "datasets": sorted(datasets.values(), key=lambda d: d["provider"]),
            "method": "cited source values with exact unit normalisation; citations by DOI or designation only",
            "assumptions": [
                "no value is averaged, merged or estimated",
                "unstated conditions are shown as unstated",
                "an unresolved citation stays the reference string the source printed",
            ],
            "n": len(values),
            "snapshot_id": self.store.snapshot_id(namespace, as_of_ms=as_of_ms),
            **({"as_of_ms": as_of_ms} if as_of_ms is not None else {}),
        }

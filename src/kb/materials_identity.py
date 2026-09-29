"""Reviewable matching of material records across sources (MT08, #2086).

Two identity levels, never confused:

* **composition level** - entries with the same reduced formula. This is a
  grouping, labelled as such, and never a claim that two records describe the
  same phase: rutile and anatase share a composition.
* **phase level** - a reviewed match between two entries of the same
  composition and the same space group. Candidates are generated
  deterministically from stated evidence only:

  - ``cross-referenced-identifier``: both records state the same ICSD
    collection code (or one states the other's COD ID), cited with the fields;
  - ``structure-similarity``: equal space group and volume per atom within
    :data:`VOLUME_TOLERANCE` (relative, exact arithmetic). Site-level
    structure matching needs the optional ``pymatgen`` structure matcher and
    retained sites; neither is used, so the evidence says the comparison is
    lattice-level only (fail closed: without both volumes there is no
    structural candidate at all).

  Two entries whose space groups differ are never proposed, whatever else
  they share, so polymorphs are never merged; an unknown space group yields
  no structural candidate. Candidates enter the shared reviewable state
  machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`): they
  stay ``proposed`` until a reviewer accepts or rejects them, a stronger basis
  upgrades a pending weaker one, and a revert never reactivates an earlier
  decision. Nothing is merged: every record keeps its own identifiers.

:func:`phase_groups` is the single equivalence definition every lookup,
comparison and release watch uses.
"""

from __future__ import annotations

import importlib.util
import re
from fractions import Fraction
from typing import Any

from src.kb.materials_records import READ_SCOPE
from src.kb.materials_store import (
    MaterialsError,
    MaterialsStore,
    authorize,
    record_key,
    table_exists,
)

KEY_PREFIX = "materials:entry:"
VOLUME_TOLERANCE = Fraction(10, 100)
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"


def space_group_symbol(symbol: Any) -> str | None:
    """Hermann-Mauguin symbol compared without spaces, underscores or origin choice ('P 42/m n m' == 'P4_2/mnm')."""

    if not symbol:
        return None
    text = str(symbol).split(":")[0]
    return re.sub(r"[\s_]", "", text).casefold() or None


def same_space_group(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    """True/False when both records state comparable space groups, None when unknown."""

    if left.get("number") is not None and right.get("number") is not None:
        return left["number"] == right["number"]
    a, b = (
        space_group_symbol(left.get("symbol")),
        space_group_symbol(right.get("symbol")),
    )
    if a and b:
        return a == b
    return None


def icsd_codes(identifiers: dict[str, Any]) -> set[str]:
    values = identifiers.get("icsd") or []
    values = values if isinstance(values, list) else [values]
    return {
        re.sub(r"\D", "", str(v)).lstrip("0")
        for v in values
        if re.sub(r"\D", "", str(v))
    }


def site_matcher_available() -> bool:
    return importlib.util.find_spec("pymatgen") is not None


def _volume_per_atom(structure: dict[str, Any]) -> Fraction | None:
    volume, sites = (
        (structure.get("volume") or {}).get("value"),
        structure.get("nsites"),
    )
    if volume is None or not sites:
        return None
    return Fraction(volume) / int(sites)


class MaterialsIdentity:
    def __init__(self, conn, *, now=None, initialize=True):
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = MaterialsStore(conn, initialize=initialize, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    def subjects(self, namespace, *, as_of_ms=None):
        result = []
        for item in self.store.entries(namespace, as_of_ms=as_of_ms):
            current = self.store.entry(namespace, item["entry_id"], as_of_ms=as_of_ms)
            content = current["content"]
            structure = content.get("structure") or {}
            result.append(
                {
                    "record_key": current["record_key"],
                    "entry_id": item["entry_id"],
                    "provider": item["provider"],
                    "native_id": item["native_id"],
                    "reduced_formula": item["reduced_formula"],
                    "space_group": structure.get("space_group") or {},
                    "method_class": structure.get("method_class"),
                    "volume_per_atom": _volume_per_atom(structure),
                    "icsd": icsd_codes(content.get("identifiers") or {}),
                    "release": current["release"]["label"],
                    "version": current["version"],
                }
            )
        return result

    def composition_groups(self, namespace, *, as_of_ms=None):
        groups: dict[str, list[str]] = {}
        for subject in self.subjects(namespace, as_of_ms=as_of_ms):
            groups.setdefault(subject["reduced_formula"], []).append(
                subject["record_key"]
            )
        return {
            formula: {
                "level": "composition",
                "records": sorted(keys),
                "note": "same reduced formula only; not a phase identity (polymorphs share it)",
            }
            for formula, keys in sorted(groups.items())
        }

    def _evidence(self, left, right):
        """(basis, evidence) for one pair, or None when nothing stated supports a phase-level match."""

        same = same_space_group(left["space_group"], right["space_group"])
        if same is False:
            return None  # different space groups: polymorphs or different phases, never proposed

        def side(s):
            return {
                "record_key": s["record_key"],
                "provider": s["provider"],
                "native_id": s["native_id"],
                "release": s["release"],
                "version": s["version"],
                "space_group": s["space_group"],
            }

        shared = left["icsd"] & right["icsd"]
        cod = {s["native_id"] for s in (left, right) if s["provider"] == "cod"}
        cod_cited = {
            c
            for s in (left, right)
            for c in cod
            if s["provider"] != "cod" and c in s["icsd"]
        }
        if shared or cod_cited:
            return "cross-referenced-identifier", {
                "kind": "source-stated cross-reference",
                "icsd": sorted(shared),
                "cod": sorted(cod_cited),
                "fields": ["database_IDs.icsd", "icsd", "icsd_id"],
                "left": side(left),
                "right": side(right),
                "space_group_check": "equal" if same else "unknown",
                "note": "the sources cite the same experimental structure; accept with that citation as evidence",
            }
        if same is None:
            return None
        a, b = left["volume_per_atom"], right["volume_per_atom"]
        if a is None or b is None:
            return None  # fail closed: no structural evidence without both volumes
        relative = abs(a - b) / max(a, b)
        if relative > VOLUME_TOLERANCE:
            return None
        return "structure-similarity", {
            "kind": "phase-level structural similarity",
            "left": side(left),
            "right": side(right),
            "space_group_check": "equal",
            "volume_per_atom": [str(float(a)), str(float(b))],
            "relative_volume_difference": f"{relative.numerator}/{relative.denominator}",
            "tolerance": f"relative volume per atom <= {VOLUME_TOLERANCE}",
            "site_comparison": "not performed: sites are not retained"
            + (
                ""
                if site_matcher_available()
                else " and the optional structure matcher is not installed"
            ),
            "note": "lattice-level evidence; a reviewer decides",
        }

    def propose(self, namespace, *, principal_id, scopes, as_of_ms=None):
        """Offer phase-level candidates within each composition group; nothing is linked until reviewed."""

        from src.kb.ownership_store import canonical_entity_id

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        subjects = self.subjects(namespace, as_of_ms=as_of_ms)
        by_formula: dict[str, list[dict[str, Any]]] = {}
        for subject in subjects:
            by_formula.setdefault(subject["reduced_formula"], []).append(subject)
        offered, polymorphs = [], []
        for formula, group in sorted(by_formula.items()):
            group = sorted(group, key=lambda s: s["record_key"])
            for i, left in enumerate(group):
                for right in group[i + 1 :]:
                    if left["provider"] == right["provider"]:
                        continue  # one source's two records are two records
                    found = self._evidence(left, right)
                    if found is None:
                        if (
                            same_space_group(left["space_group"], right["space_group"])
                            is False
                        ):
                            polymorphs.append(
                                {
                                    "reduced_formula": formula,
                                    "records": [
                                        left["record_key"],
                                        right["record_key"],
                                    ],
                                    "reason": "different space groups: never matched at phase level",
                                }
                            )
                        continue
                    basis, evidence = found
                    offered.append(
                        self.service.offer(
                            namespace,
                            left_key=left["record_key"],
                            right_key=right["record_key"],
                            left_entity=canonical_entity_id(left["record_key"]),
                            right_entity=canonical_entity_id(right["record_key"]),
                            basis=basis,
                            evidence=[{**evidence, "reduced_formula": formula}],
                            principal_id=principal_id,
                            scopes=scopes,
                        )
                    )
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
            "not_proposed_polymorphs": polymorphs,
            "composition_groups": self.composition_groups(namespace, as_of_ms=as_of_ms),
            "semantics": "candidates stay proposed until reviewed; composition groups are not phase identities",
        }

    def candidates(self, namespace, *, scopes, record=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM ownership_identity_candidates WHERE namespace=? AND left_key LIKE ? "
            "AND right_key LIKE ? AND (? IS NULL OR left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, KEY_PREFIX + "%", KEY_PREFIX + "%", record, record, record],
        ).fetchall()
        return [self.service._row(namespace, r[0]) for r in rows]

    def review(
        self, namespace, candidate_id, decision, reason, *, principal_id, scopes
    ):
        self._own(namespace, candidate_id)
        return self.service.review(
            namespace,
            candidate_id,
            decision,
            reason,
            principal_id=principal_id,
            scopes=scopes,
        )

    def revert(self, namespace, candidate_id, reason, *, principal_id, scopes):
        self._own(namespace, candidate_id)
        return self.service.revert(
            namespace, candidate_id, reason, principal_id=principal_id, scopes=scopes
        )

    def _own(self, namespace, candidate_id):
        self.store.require_ready()
        row = (
            self.conn.execute(
                "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? "
                "AND candidate_id=?",
                [namespace, candidate_id],
            ).fetchone()
            if table_exists(self.conn, "ownership_identity_candidates")
            else None
        )
        if row is None or not (
            row[0].startswith(KEY_PREFIX) and row[1].startswith(KEY_PREFIX)
        ):
            raise MaterialsError("not_found", "not a materials identity candidate")


def phase_groups(conn, namespace) -> dict[str, str]:
    """record_key -> phase-group representative over accepted, unreverted materials matches.

    The one equivalence definition used by lookups, comparisons and release
    watches. A record without an accepted match is its own group.
    """

    if not table_exists(conn, "ownership_identity_candidates"):
        return {}
    rows = conn.execute(
        "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? "
        "AND state='accepted' AND left_key LIKE ? AND right_key LIKE ? ORDER BY candidate_id",
        [namespace, KEY_PREFIX + "%", KEY_PREFIX + "%"],
    ).fetchall()
    parent: dict[str, str] = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for left, right in rows:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
    return {key: find(key) for key in list(parent)}


def group_of(groups: dict[str, str], provider: str, native_id: str) -> str:
    key = record_key(provider, native_id)
    return groups.get(key, key)


__all__ = ["MaterialsIdentity", "phase_groups", "group_of", "same_space_group"]

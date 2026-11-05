"""The ``offshore_leaks`` section of the holding-structure dossier (#2888, #2890).

:mod:`src.kb.ownership_structure_dossier` resolves this provider lazily
(``OFFSHORE_LEAKS_PROVIDER``). It reads the ICIJ Offshore Leaks records that
the Corporate Ownership graph joins to the structure's identity cluster
(:func:`src.kb.offshore_leaks.offshore_section`): ICIJ nodes reach the
structure only through accepted, unreverted identity decisions, never by name.

Every line cites its ownership record revision, the leak dataset and the ICIJ
node id, and carries ICIJ's caveat that inclusion does not imply illegal
conduct. ICIJ's own ``same_as`` / ``similar`` links are quoted, and proposed
identity candidates are listed apart, never joined. The section is ``inert``
while the optional ``offshore-leaks`` feature is off.
"""

from __future__ import annotations

from typing import Any

from src.kb.offshore_leaks import CAVEAT, NO_VERDICT, feature_enabled, offshore_section
from src.kb.ownership_store import OwnershipError


def _cite(namespace: str, citation: dict[str, Any]) -> dict[str, Any]:
    out = {"id": f"offshore_leaks:{citation['record_id']}@{citation['revision']}", "namespace": namespace,
           "record": str(citation["record_id"]), "revision": citation["revision"]}
    for key in ("provider", "dataset", "provider_record_id", "url", "locator"):
        if citation.get(key) is not None:
            out[key] = citation[key]
    return out


def structure_section(ctx: Any) -> dict[str, Any]:
    """Offshore Leaks lines for a structure: its ICIJ entity records and the officer/intermediary roles naming it."""
    if not feature_enabled(ctx.conn):
        raise OwnershipError("inert", "the corporate-ownership offshore-leaks feature is not enabled; "
                                      "not the same as empty")
    namespace = ctx.section_namespace or ctx.namespace
    section = offshore_section(ctx.conn, namespace, ctx.root, principal_id=ctx.principal_id, scopes=ctx.scopes)
    reached = {"basis": "ownership-identity-cluster", "root": section["entity"],
               "note": "an ICIJ node joins the structure only through accepted identity decisions"}
    lines = []
    for record in section["records"]:
        lines.append({"statement": f"ICIJ {record['citation'].get('dataset') or 'Offshore Leaks'} node "
                                   f"{record['node_id']}: {record.get('name')} ({record.get('node_type')}, "
                                   f"jurisdiction as published: {record.get('jurisdiction_as_published')})",
                      "reached_by": reached, "citation": _cite(namespace, record["citation"]),
                      "data": {"kind": record["kind"], "status": record.get("status"), "caveat": CAVEAT}})
    for role in section["roles"]:
        counterparty = role.get("counterparty") or {}
        lines.append({"statement": f"ICIJ {role.get('dataset') or 'Offshore Leaks'}: "
                                   f"{counterparty.get('name') or counterparty.get('key') or 'withheld'} "
                                   f"\"{role['role_as_published']}\" ({role['direction']})",
                      "reached_by": reached, "citation": _cite(namespace, role["citation"]),
                      "data": {"direction": role["direction"], "rel_type": role.get("rel_type"),
                               "node_ids": role.get("node_ids"), "start_date": role.get("start_date"),
                               "end_date": role.get("end_date"), "caveat": CAVEAT}})
    pending = [{**c, "note": "a proposed candidate, not an identity decision"}
               for c in section["identity_candidates"] if c.get("state") == "proposed"]
    unknowns = []
    if section["redacted_person_links"]:
        unknowns.append(f"{section['redacted_person_links']} link(s) to owner-scoped natural persons are redacted "
                        "for this principal")
    return {"lines": lines, "pending": pending, "unknowns": unknowns,
            "quoted_links": section["quoted_links"], "datasets": section["datasets"],
            "caveat": CAVEAT, "notice": NO_VERDICT}

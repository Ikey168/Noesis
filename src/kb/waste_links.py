"""Waste records linked to Chemicals, Products and environment.core facilities by citation or accepted match (WC07).

Track #2740. Three kinds of link, each recording its **basis** and pinning **specific record revisions** on both sides
(the waste vintage - a series vintage or a transfer row's ``environment_vintages`` row - in force when the link was
made, and the target's revision):

* ``chemicals`` - only where a **published identifier** matches: a CAS number a waste document cites (for example an
  entry of the E-PRTR pollutant list) equal to a CAS number a ``chemicals.substances`` record publishes
  (``substance_identifiers``); basis ``shared-identifier``. Names are never matched.
* ``products`` - by **citation** only: the packaging and WEEE datasets (``env_waspac``, ``env_waselee``) the Eurostat
  documents cite as later documents are resolved by their exact URL to a document link the Products provider holds
  (``product_document_links``); basis ``citation``. Nothing is acquired from those datasets here.
* ``facility`` - each transfer row to the ``environment.core`` facility record its INSPIRE id was **accepted** as
  (WC06); basis ``accepted-match``. Unmatched INSPIRE ids stay visible as ``target_not_held``.

A missing provider is ``provider_absent``; a provider without the target is ``target_not_held`` - reported, never
dropped. No value is combined and no indicator is derived from a linked record.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.waste_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    WasteError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)
from src.kb.waste_store import WasteStore

CONTRACT = "noesis-waste-link-v1"
KINDS = ("chemicals", "products", "facility")
BASES = ("shared-identifier", "citation", "accepted-match")
STATES = ("linked", "provider_absent", "target_not_held")
NO_DERIVATION = "A link records what is related and on which basis; no value is combined and no indicator derived."
CHEMICALS_READ = "knowledge:substances:read"
PRODUCTS_READ = "knowledge:products:read"
PRODUCT_DATASETS = ("env_waspac", "env_waselee")
_DDL = """
CREATE TABLE IF NOT EXISTS waste_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  vintage_id TEXT, kind TEXT NOT NULL, basis TEXT, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


class WasteLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = WasteStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def _subjects(self, namespace: str) -> list[dict[str, Any]]:
        """Every waste record with its current revision and the references its source document states."""
        out = []
        for series in self.store.find_series(namespace):
            published = [v for v in self.store.vintage_rows(namespace, series["series_id"]) if v["status"] == "published"]
            if not published:
                continue
            definition = self.store.definition(namespace, published[-1]["definition_id"]) or {"content": {}}
            out.append({"subject_kind": "series", "subject_id": series["series_id"],
                        "vintage_id": published[-1]["vintage_id"], "provider": series["provider"],
                        "references": list(definition["content"].get("references") or [])})
        for row in self.store.transfer_rows(namespace):
            vintages = [v for v in self.store.transfer_vintages(namespace, row["row_id"]) if v["status"] == "published"]
            if not vintages:
                continue
            release = self.store.release(namespace, vintages[-1]["release_id"]) if vintages[-1]["release_id"] else {}
            out.append({"subject_kind": "transfer_row", "subject_id": row["row_id"],
                        "vintage_id": vintages[-1]["vintage_id"], "provider": "eea-industry-waste-transfers",
                        "inspire_id": row["inspire_id"],
                        "references": list(dict(release.get("document") or {}).get("references") or [])})
        return out

    def _put(self, namespace, subject, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "waste-link:" + digest([namespace, subject["subject_id"], subject["vintage_id"], kind, reference,
                                          target, state])[:24]
        if self.conn.execute("SELECT 1 FROM waste_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return self.link(namespace, link_id), False
        self.conn.execute("INSERT INTO waste_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, subject["subject_kind"], subject["subject_id"], subject["vintage_id"],
                           kind, basis, canonical(reference), None if target is None else canonical(target), state,
                           canonical({**evidence, "note": NO_DERIVATION}), principal_id, self.now()])
        return self.link(namespace, link_id), True

    @staticmethod
    def _require(scopes: set[str], scope: str) -> None:
        if scope not in scopes and "operator" not in scopes:
            raise WasteError("unauthorized", f"{scope} is required to read the linked provider")

    # ------------------------------------------------------------------ chemicals

    def link_chemicals(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       chemicals_namespace: str | None = None) -> dict[str, Any]:
        """Link records to Chemicals substances that publish the same CAS number a waste document cites."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        chemicals_namespace = chemicals_namespace or namespace
        held = table_exists(self.conn, "substance_identifiers")
        if held:
            self._require(scopes, CHEMICALS_READ)
        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for subject in self._subjects(namespace):
            for reference in subject["references"]:
                if str(reference.get("scheme") or "").casefold() != "cas":
                    continue
                if not held:
                    link, new = self._put(namespace, subject, "chemicals", None, reference, None, "provider_absent",
                                          {"reason": "chemicals.substances is not composed (substance_identifiers)"},
                                          principal_id)
                    out["missing"] += [link["link_id"]] if new else []
                    continue
                from src.kb.substances_records import identifier_key
                from src.kb.substances_store import SubstanceStore

                key = identifier_key("cas", reference.get("identifier"))
                substances = SubstanceStore(self.conn, initialize=False)
                subjects = substances.find_subjects(chemicals_namespace, "cas", key) if key else []
                if not subjects:
                    reason = "no Chemicals substance publishes this CAS number" if key else "malformed CAS number"
                    link, new = self._put(namespace, subject, "chemicals", "shared-identifier", reference, None,
                                          "target_not_held", {"reason": reason}, principal_id)
                    out["missing"] += [link["link_id"]] if new else []
                    continue
                for subject_key in subjects:
                    identifiers = [i for i in substances.identifiers(chemicals_namespace, subject_key)
                                   if i["scheme"] == "cas" and i["value_key"] == key]
                    target = {"kind": "substance", "provider_id": "chemicals.substances",
                              "namespace": chemicals_namespace, "subject_key": subject_key,
                              "revision_ids": sorted({i["revision_id"] for i in identifiers})}
                    link, new = self._put(namespace, subject, "chemicals", "shared-identifier", reference, target,
                                          "linked", {"identifier": {"scheme": "cas", "value": key},
                                                     "rule": "the published CAS number matches; names are never "
                                                             "matched"}, principal_id)
                    out["linked"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ products

    def link_products(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                      products_namespace: str | None = None) -> dict[str, Any]:
        """Resolve the cited packaging and WEEE datasets by exact URL to a Products document link (citation only)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        products_namespace = products_namespace or namespace
        held = table_exists(self.conn, "product_document_links")
        if held:
            self._require(scopes, PRODUCTS_READ)
        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for subject in self._subjects(namespace):
            for reference in subject["references"]:
                if reference.get("identifier") not in PRODUCT_DATASETS:
                    continue
                if not held:
                    link, new = self._put(namespace, subject, "products", "citation", reference, None,
                                          "provider_absent", {"reason": "the Products provider is not composed "
                                                                        "(product_document_links)"}, principal_id)
                    out["missing"] += [link["link_id"]] if new else []
                    continue
                rows = self.conn.execute(
                    "SELECT link_id, variant_id, first_revision_id, kind, url FROM product_document_links WHERE "
                    "namespace=? AND url=? ORDER BY link_id", [products_namespace, reference.get("url")]).fetchall()
                if not rows:
                    link, new = self._put(namespace, subject, "products", "citation", reference, None,
                                          "target_not_held", {"reason": "no Products record cites this dataset URL; "
                                                                        "kept as the published citation"},
                                          principal_id)
                    out["missing"] += [link["link_id"]] if new else []
                    continue
                for row in rows:
                    target = {"kind": "product-document-link", "provider_id": "products", "link_id": row[0],
                              "variant_id": row[1], "revision_id": row[2], "url": row[4]}
                    link, new = self._put(namespace, subject, "products", "citation", reference, target, "linked",
                                          {"rule": "the cited dataset URL matches exactly; nothing is acquired from "
                                                   "the cited dataset here"}, principal_id)
                    out["linked"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ facilities

    def link_facilities(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Link each transfer row to the environment.core facility record its INSPIRE id was accepted as."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "environment_records")
        identity = None
        if table_exists(self.conn, "waste_identity_assertions"):
            from src.kb.waste_identity import WasteIdentity

            identity = WasteIdentity(self.conn, initialize=False, now=self.now)
        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for subject in (s for s in self._subjects(namespace) if s["subject_kind"] == "transfer_row"):
            reference = {"scheme": "inspire-id", "identifier": subject["inspire_id"]}
            if not held:
                link, new = self._put(namespace, subject, "facility", None, reference, None, "provider_absent",
                                      {"reason": "environment.core is not composed"}, principal_id)
                out["missing"] += [link["link_id"]] if new else []
                continue
            state = identity.facility_for(namespace, subject["inspire_id"]) if identity else {"state": "not_reviewed"}
            if state.get("target") is None:
                link, new = self._put(namespace, subject, "facility", "accepted-match", reference, None,
                                      "target_not_held", {"identity_state": state["state"],
                                                          "reason": state.get("reason") or "no accepted facility "
                                                                                           "match"}, principal_id)
                out["missing"] += [link["link_id"]] if new else []
                continue
            link, new = self._put(namespace, subject, "facility", "accepted-match", reference, state["target"],
                                  "linked", {"assertion_id": state["assertion_id"]}, principal_id)
            out["linked"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, subject_kind, subject_id, vintage_id, kind, basis, reference_json, target_json, state, "
            "evidence_json, created_by FROM waste_links WHERE namespace=? AND link_id=?",
            [namespace, link_id]).fetchone()
        if row is None:
            raise WasteError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "subject_kind": row[1], "subject_id": row[2],
                "vintage_id": row[3], "kind": row[4], "basis": row[5], "reference": load(row[6], {}),
                "target": load(row[7], None), "state": row[8], "evidence": load(row[9], {}), "created_by": row[10]}

    def links(self, namespace: str, *, scopes: Iterable[str], subject_id: str | None = None, kind: str | None = None,
              state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "waste_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM waste_links WHERE namespace=? AND (? IS NULL OR subject_id=?) AND (? IS NULL OR "
            "kind=?) AND (? IS NULL OR state=?) ORDER BY subject_id, kind, link_id",
            [namespace, subject_id, subject_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


def provider_presence(conn: Any) -> dict[str, str]:
    """Which linked providers are composed here (``available``) or not (``provider_absent``)."""
    tables = {"chemicals.substances": "substance_identifiers", "products": "product_document_links",
              "environment.core": "environment_records"}
    return {name: "available" if table_exists(conn, table) else "provider_absent" for name, table in tables.items()}


def mapping_summary(links: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for link in links:
        summary[f"{link['kind']}:{link['state']}"] = summary.get(f"{link['kind']}:{link['state']}", 0) + 1
    return summary


__all__ = ["BASES", "CONTRACT", "KINDS", "NO_DERIVATION", "STATES", "WasteLinks", "mapping_summary",
           "provider_presence"]

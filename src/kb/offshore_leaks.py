"""ICIJ Offshore Leaks in Corporate Ownership: projection, reviewable identity and the dossier section (#2888).

The optional ``offshore-leaks`` feature of the Corporate Ownership bundle
(default off). Records come from :mod:`src.ingestion.offshore_leaks` (offline
source-pack replay) or from a bulk-release subset (:func:`project_bulk_subset`)
and live in the one ownership record store. This module adds:

* :func:`propose_identity` - ICIJ's own ``same_as``/``similar`` links, quoted
  as evidence, and name or registry-number matches to registry entities, all
  *offered* as reviewable candidates through the shared identity state machine.
  ``similar`` links and name-only matches use the ``similar-name`` basis, which
  can never be accepted; nothing is accepted automatically.
* :func:`offshore_section` / :func:`build_dossier_with_offshore_leaks` - the
  offshore links of an entity (its leak-dataset records, the roles naming it,
  quoted links and registered addresses), each cited to the ownership record
  revision, the leak dataset and the ICIJ node id, with ICIJ's caveat.
* :func:`report_section` - the same lines as sourced statements for the
  ownership dossier export.

No wrongdoing, AML, sanctions or risk verdict is produced; every output passes
:func:`src.ingestion.offshore_leaks.guard`.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from src.ingestion.offshore_leaks import (
    CAVEAT,
    NO_VERDICT,
    PROVIDER,
    guard,
    map_subgraph,
    person_research,
)
from src.kb.ownership_records import ENTITY_KINDS, WRITE_SCOPE
from src.kb.ownership_store import (
    OwnershipError,
    OwnershipStore,
    authorize,
    canonical_entity_id,
)

FEATURE = "offshore-leaks"
BUNDLE = "corporate-ownership"
SECTION_CONTRACT = "noesis-ownership-offshore-leaks-section-v1"
_REL_KEY = re.compile(rf"^{re.escape(PROVIDER)}:rel:([^:]+):([^:]+):([^:]+):")


def feature_enabled(conn: Any) -> bool:
    """Whether the optional ``offshore-leaks`` feature is selected in the active composition plan (default off)."""
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON "
            "g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return FEATURE in ((plan.get("features") or {}).get(BUNDLE) or [])


# -------------------------------------------------------------- projection


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def project_bulk_subset(conn: Any, namespace: str, release_dir: str | Path, *, principal_id: str,
                        scopes: Iterable[str], now: Callable[[], int] | None = None) -> dict[str, Any]:
    """Project a bulk-runner release subset (``icij-offshore-leaks``) into the ownership store.

    The subset holds the selected node and relationship rows verbatim; the
    archive's SHA-256 from the runner's completion marker is recorded on each
    record and the bulk manifest id is the run id.
    """
    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    release_dir = Path(release_dir)
    manifest = json.loads((release_dir / "manifest.json").read_text())
    if manifest.get("provider") not in (None, "icij-offshore-leaks"):
        raise OwnershipError("invalid_request", "the release is not an ICIJ Offshore Leaks bulk release")
    subset = release_dir / "subset"
    selections = _jsonl(subset / "selection.jsonl.gz")
    if not selections:
        raise OwnershipError("invalid_request", "the release has no processed selection; run the bulk adapter first")
    chosen = selections[-1]
    nodes = {}
    for row in _jsonl(subset / "nodes.jsonl.gz"):
        nodes[row["node_id"]] = {**row["row"], "_file": row["file"], "_type": row["node_type"], "_line": row["line"]}
    edges = [{**row["row"], "_line": row["line"]} for row in _jsonl(subset / "relationships.jsonl.gz")]
    sha = None
    for marker in sorted((release_dir / "files").glob("*.json")):
        sha = (json.loads(marker.read_text()).get("digests") or {}).get("sha256") or sha
    subgraph = {"selection": chosen["selection"], "seeds": chosen["seeds"], "nodes": nodes, "relationships": edges,
                "missing_nodes": chosen["missing_nodes"], "truncated": chosen["truncated"]}
    from src.ingestion.offshore_leaks import FILES

    records, coverage = map_subgraph(subgraph, file_shas={name: sha for name in FILES} if sha else {},
                                     release=str(manifest.get("release_id") or release_dir.name))
    store = OwnershipStore(conn, **({"now": now} if now else {}))
    counts = store.apply(namespace, records, run_id=f"bulk:{manifest.get('manifest_id') or release_dir.name}",
                         observed_at_ms=store.now(), principal_id=principal_id)
    return guard({"counts": counts, "coverage": coverage, "release_id": manifest.get("release_id"),
                  "manifest_id": manifest.get("manifest_id"), "caveat": CAVEAT, "notice": NO_VERDICT})


# ------------------------------------------------------------------ identity


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0")


def _ref(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    return {"record_key": body["record_key"], "record_id": view["record_id"], "revision": view["revision"],
            "provider": body["source"]["provider"], "provider_record_id": body["source"].get("provider_record_id")}


def propose_identity(conn: Any, namespace: str, *, principal_id: str, scopes: Iterable[str],
                     now: Callable[[], int] | None = None) -> dict[str, Any]:
    """Offer ICIJ links and registry matches as reviewable identity candidates; never accepts any."""
    from src.kb.entities import normalize_surface
    from src.kb.ownership_identity import CONFIDENCE, OwnershipIdentityService

    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    service = OwnershipIdentityService(conn, **({"now": now} if now else {}))
    views = [v for v in service.store.records(namespace, principal_id=principal_id, scopes=scopes, kinds=ENTITY_KINDS)
             if not v.get("redacted")]
    icij = {v["record"]["record_key"]: v for v in views if v["record"]["source"]["provider"] == PROVIDER}
    registry = [v for v in views if v["record"]["source"]["provider"] != PROVIDER and v["record"]["kind"] == "legal_entity"]
    research = person_research()
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    withheld = {"person_links": 0, "other_end_not_acquired": 0}

    def offer(left: str, right: str, basis: str, evidence: dict[str, Any]) -> None:
        key = tuple(sorted((left, right)))
        current = pairs.get(key)
        if current is None or CONFIDENCE[basis] > CONFIDENCE[current["basis"]]:
            pairs[key] = {"basis": basis, "evidence": [evidence]}
        elif current["basis"] == basis and evidence not in current["evidence"]:
            current["evidence"].append(evidence)

    for key, view in sorted(icij.items()):
        body = view["record"]
        for link in body.get("native", {}).get("icij_links") or []:
            if not link.get("identity_basis") or link.get("direction") != "outgoing":
                continue
            other = icij.get(link["other_key"])
            if other is None:
                withheld["other_end_not_acquired"] += 1
                continue
            if "person" in (body["kind"], other["record"]["kind"]) and not research:
                withheld["person_links"] += 1
                continue
            offer(key, link["other_key"], link["identity_basis"], {
                "source": PROVIDER, "rel_type": link["rel_type"], "link_as_published": link["link"],
                "dataset": link["dataset"], "node_id_start": link["node_id_start"], "node_id_end": link["node_id_end"],
                "locator": link["locator"], "records": [_ref(view), _ref(other)],
                "note": "ICIJ's own link, quoted; a candidate for review, never accepted automatically",
                "caveat": CAVEAT})
    for key, view in sorted(icij.items()):
        body = view["record"]
        if body["kind"] != "legal_entity":
            continue
        names = {normalize_surface(n) for n in [body["name"], *(body.get("other_names") or [])] if n}
        ruc = _norm(body.get("native", {}).get("ibcRUC"))
        for target in registry:
            other = target["record"]
            ours, theirs = body.get("jurisdiction"), other.get("jurisdiction")
            theirs = str(theirs or "").split("-")[0].upper() or None
            if ours and theirs and ours != theirs:
                continue
            target_key = other["record_key"]
            if ruc and any(_norm(i.get("value")) == ruc for i in other.get("identifiers") or []):
                offer(key, target_key, "unqualified-identifier", {
                    "source": PROVIDER, "ibcRUC_as_published": body["native"]["ibcRUC"],
                    "records": [_ref(view), _ref(target)], "jurisdictions": [ours, theirs],
                    "note": "a registry number as it appears in a leak dataset; the reviewer must establish the "
                            "issuing register", "caveat": CAVEAT})
            if normalize_surface(other["name"]) in names:
                basis = "name-jurisdiction" if ours and theirs else "similar-name"
                offer(key, target_key, basis, {
                    "source": PROVIDER, "names": [body["name"], other["name"]], "jurisdictions": [ours, theirs],
                    "records": [_ref(view), _ref(target)],
                    "note": "equal normalized names are a weak signal, never an identity", "caveat": CAVEAT})
    results = []
    for (left, right), proposal in sorted(pairs.items()):
        outcome = service.offer(namespace, left_key=left, right_key=right, left_entity=canonical_entity_id(left),
                                right_entity=canonical_entity_id(right), basis=proposal["basis"],
                                evidence=proposal["evidence"], principal_id=principal_id, scopes=scopes)
        results.append({**outcome, "left_key": left, "right_key": right, "basis": proposal["basis"]})
    return guard({"offered": results, "withheld": withheld, "person_research": research,
                  "note": "candidates only; acceptance is a reviewer's identity decision", "caveat": CAVEAT})


# ------------------------------------------------------------------- dossier


def _citation(view: Mapping[str, Any]) -> dict[str, Any]:
    body = view["record"]
    source = body["source"]
    native = body.get("native") or {}
    return {"record_id": view["record_id"], "revision": view["revision"], "provider": source["provider"],
            "dataset": native.get("dataset"), "provider_record_id": source.get("provider_record_id"),
            "url": source.get("url"), "locator": source.get("locator")}


def offshore_section(conn: Any, namespace: str, entity: str, *, principal_id: str | None, scopes: Iterable[str],
                     known_at_ms: int | None = None) -> dict[str, Any]:
    """The offshore links of an entity cluster, each cited to its record revision, dataset and node id."""
    from src.kb.ownership_graph import OwnershipGraph

    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=set(scopes), known_at_ms=known_at_ms)
    root = graph.cluster(entity)
    members = set(graph.members(root))
    member_nodes = {k.rsplit(":", 1)[1] for k in members if k.startswith(f"{PROVIDER}:node:")}
    records, roles, links, addresses, redacted = [], [], [], [], 0
    for view in graph.views:
        body = view["record"]
        if body.get("source", {}).get("provider") != PROVIDER:
            continue
        if view.get("redacted"):
            match = _REL_KEY.match(body.get("record_key") or "")
            if match and {match.group(1), match.group(3)} & member_nodes:
                redacted += 1
            continue
        if body["kind"] in ENTITY_KINDS and body["record_key"] in members:
            native = body.get("native") or {}
            records.append({"record_key": body["record_key"], "kind": body["kind"], "name": body.get("name"),
                            "node_id": body["record_key"].rsplit(":", 1)[1], "node_type": native.get("node_type"),
                            "jurisdiction_as_published": native.get("jurisdiction_as_published"),
                            "status": body.get("status"), "citation": _citation(view), "caveat": CAVEAT})
            for link in native.get("icij_links") or []:
                links.append({"from": body["record_key"], "rel_type": link["rel_type"],
                              "link_as_published": link["link"], "direction": link["direction"],
                              "other_key": link["other_key"], "identity_basis": link.get("identity_basis"),
                              "dataset": link["dataset"], "node_id_start": link["node_id_start"],
                              "node_id_end": link["node_id_end"], "citation": _citation(view),
                              "note": "quoted; not an identity decision" if link.get("identity_basis") else "quoted",
                              "caveat": CAVEAT})
            for address in body.get("addresses") or []:
                addresses.append({**address, "entity": body["record_key"], "citation": _citation(view),
                                  "caveat": CAVEAT})
        elif body["kind"] == "officer_role":
            officer = body.get("officer") or {}
            if body["entity_key"] in members:
                direction, counterparty = "names-this-entity", officer
            elif officer.get("key") in members:
                name = next((v["record"].get("name") for v in graph.entities.get(body["entity_key"], [])
                             if not v.get("redacted")), None)
                direction, counterparty = "this-entity-holds-role", {"key": body["entity_key"], "name": name,
                                                                     "kind": "entity"}
            else:
                continue
            native = body.get("native") or {}
            roles.append({"direction": direction, "role_as_published": body["role"], "rel_type": native.get("rel_type"),
                          "counterparty": counterparty, "entity_key": body["entity_key"],
                          "officer_key": officer.get("key"), "start_date": body.get("appointed_on"),
                          "end_date": body.get("resigned_on"), "dataset": native.get("dataset"),
                          "node_ids": {"officer": native.get("officer_node_id"), "entity": native.get("entity_node_id")},
                          "citation": _citation(view), "caveat": CAVEAT})
    from src.kb.ownership_identity import OwnershipIdentityService

    candidates = []
    service = OwnershipIdentityService(conn, initialize=False)
    if service._ready():
        seen = set()
        for key in sorted(members | {r["officer_key"] for r in roles if r.get("officer_key")}):
            for candidate in service.candidates(namespace, scopes=set(scopes), record_key=key):
                if candidate["candidate_id"] in seen:
                    continue
                if not any(str(k).startswith(f"{PROVIDER}:") for k in (candidate["left_key"], candidate["right_key"])):
                    continue
                seen.add(candidate["candidate_id"])
                candidates.append({k: candidate[k] for k in ("candidate_id", "left_key", "right_key", "basis",
                                                             "confidence", "state")})
    section = {
        "contract": SECTION_CONTRACT, "feature": FEATURE, "namespace": namespace, "entity": root,
        "status": "assembled" if records or roles else "no_offshore_links",
        "records": sorted(records, key=lambda r: r["record_key"]),
        "roles": sorted(roles, key=lambda r: (r["direction"], str(r["entity_key"]), str(r["officer_key"]),
                                              r["role_as_published"])),
        "quoted_links": sorted(links, key=lambda r: (r["from"], r["rel_type"], r["other_key"])),
        "registered_addresses": addresses,
        "identity_candidates": sorted(candidates, key=lambda c: c["candidate_id"]),
        "redacted_person_links": redacted,
        "datasets": sorted({str(x["citation"]["dataset"]) for x in records + roles if x["citation"]["dataset"]}),
        "caveat": CAVEAT, "notice": NO_VERDICT,
    }
    return guard(section)


def build_dossier_with_offshore_leaks(conn: Any, namespace: str, scheme: str, value: str, *,
                                      principal_id: str | None, scopes: Iterable[str], as_of: str | None = None,
                                      evidence_kind: str = "unspecified", **kwargs: Any) -> dict[str, Any]:
    """The ownership dossier plus its ``offshore_leaks`` section."""
    from src.kb.ownership_dossier import build_dossier

    dossier = build_dossier(conn, namespace, scheme, value, principal_id=principal_id, scopes=scopes, as_of=as_of,
                            evidence_kind=evidence_kind, **kwargs)
    if dossier.get("status") != "assembled":
        return dossier
    section = offshore_section(conn, namespace, dossier["identity"]["root"], principal_id=principal_id,
                               scopes=scopes, known_at_ms=kwargs.get("known_at_ms"))
    return guard({**dossier, "offshore_leaks": section})


def report_section(section: Mapping[str, Any], cite: Callable[[str, int], tuple[str, int]],
                   statement: Callable[..., dict[str, Any]], namespace: str) -> dict[str, Any]:
    """Sourced statements for the ownership dossier export (every line cites a record revision)."""
    lines = []
    for item in section.get("records") or []:
        c = item["citation"]
        lines.append(statement(f"offshore:record:{c['record_id']}",
                               f"ICIJ Offshore Leaks ({c['dataset']}, node {item['node_id']}) lists {item['name']} "
                               f"as an {item['node_type']} node. {CAVEAT}",
                               [cite(c["record_id"], c["revision"])], namespace))
    for item in section.get("roles") or []:
        c = item["citation"]
        lines.append(statement(f"offshore:role:{c['record_id']}",
                               f"ICIJ Offshore Leaks ({c['dataset']}, nodes {item['node_ids']['officer']} -> "
                               f"{item['node_ids']['entity']}) states '{item['role_as_published']}' "
                               f"({item['rel_type']}). {CAVEAT}",
                               [cite(c["record_id"], c["revision"])], namespace))
    for index, item in enumerate(section.get("quoted_links") or []):
        c = item["citation"]
        lines.append(statement(f"offshore:link:{index}:{c['record_id']}",
                               f"ICIJ Offshore Leaks ({item['dataset']}) links node {item['node_id_start']} to node "
                               f"{item['node_id_end']} as '{item['link_as_published']}' ({item['rel_type']}); quoted, "
                               f"not an identity decision. {CAVEAT}",
                               [cite(c["record_id"], c["revision"])], namespace))
    if not lines:
        lines = [{"id": "offshore:none", "kind": "commentary", "dependencies": [], "citations": [],
                  "text": "No ICIJ Offshore Leaks records are linked to this entity in the acquired selection."}]
    return guard({"id": "offshore-leaks", "title": "Offshore leaks (ICIJ)", "assertions": lines})

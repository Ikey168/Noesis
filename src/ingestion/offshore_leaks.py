"""ICIJ Offshore Leaks Database: provider contract, bounded selection and Corporate Ownership mapping (#2888).

The International Consortium of Investigative Journalists (ICIJ) publishes the
Offshore Leaks Database (Offshore Leaks, Panama Papers, Bahamas Leaks, Paradise
Papers, Pandora Papers) as one bulk ZIP of CSV files: one file per node type
(entities, officers, intermediaries, addresses, others) and one file of
relationships. The audit is ``docs/development/offshore-leaks-evidence/``;
file names, column names and the download URL are recorded there as
*to verify* against the release actually downloaded.

What this module does, and what it never does:

* **Bounded selection only.** A run names seed node ids, or a dataset
  (``sourceID``) / jurisdiction filter with an explicit ``max_nodes`` cap; the
  selection adds the relationships touching the seeds and their other ends.
  Nothing selects the whole database. Truncation is reported, never silent.
* **What the leak dataset states, quoted.** Entity nodes become ``legal_entity``
  records; officer and intermediary nodes become ``person`` or ``legal_entity``
  records (a configured legal-form token or an explicit override decides, and
  the basis is recorded; otherwise a node is treated as a natural person, the
  more protective choice). ``officer_of`` and ``intermediary_of`` edges become
  ``officer_role`` records whose ``role`` is ICIJ's relationship label
  verbatim. They are never ownership or control assertions: the data states no
  percentages and nothing here feeds the parent or control graph.
  ``registered_address`` edges attach the address text to an entity record;
  a natural person's address is never stored. ICIJ's own ``same_as`` /
  ``similar`` links, and every other relationship type, are quoted on the
  node record (``native.icij_links``); identity links are only ever *offered*
  as reviewable candidates (:mod:`src.kb.offshore_leaks`), never accepted.
* **Natural persons** follow the ownership store's owner-scoped handling
  (stored per acquiring principal, redacted for everyone else). With the opt-in
  person-research mode (``NOESIS_PERSON_RESEARCH``) person records and the
  roles naming them are stored namespace-visible under
  :data:`PERSON_POLICY`, and ICIJ's person-to-person links may be offered for
  review; such records are refused on write when the mode is off.
* **No verdicts.** Every record's source carries ICIJ's caveat (:data:`CAVEAT`)
  and the dataset and node id it came from. Nothing derived from the import
  is a wrongdoing, AML, sanctions or risk verdict; :func:`guard` refuses any
  output carrying such a key.

The source-pack adapter (connector ``icij-offshore-leaks``) replays authored CSV
extracts offline; live acquisition goes through the bulk-release runner
(``src/ingestion/bulk/adapters/icij_offshore_leaks.py``) and
:func:`src.kb.offshore_leaks.project_bulk_subset`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_records import PART_CONTRACT, OwnershipRecordError, record

PROVIDER = "icij-offshore-leaks"
CONNECTOR = "icij-offshore-leaks"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
FIXTURE_SECRET = None
PUBLISHER = "International Consortium of Investigative Journalists (ICIJ)"
NODE_URL = "https://offshoreleaks.icij.org/nodes/{node_id}"
BULK_URL = "https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip"
LICENSE = ("ICIJ Offshore Leaks Database: Open Database License (ODbL) for the database, Creative Commons "
           "Attribution-ShareAlike for its contents; cite the International Consortium of Investigative Journalists")
# ICIJ's stated caution. The first sentence is quoted from ICIJ's database pages; the rest paraphrases
# ICIJ's statement that inclusion is not intended to suggest or imply improper conduct (exact wording: verify).
CAVEAT = ("ICIJ: \"There are legitimate uses for offshore companies and trusts.\" Inclusion of a person or entity "
          "in the ICIJ Offshore Leaks Database is not intended to suggest or imply that they have broken the law "
          "or otherwise acted improperly. Presence in a leak dataset is not evidence of wrongdoing, and many "
          "people and entities share the same or similar names.")
NO_VERDICT = ("Records quote what a leak dataset states. No wrongdoing, AML, sanctions or risk verdict is derived "
              "from the import.")
PERSON_POLICY = "offshore-leaks-person-research-v1"
NODE_FILES = {
    "nodes-entities.csv": "entity",
    "nodes-officers.csv": "officer",
    "nodes-intermediaries.csv": "intermediary",
    "nodes-addresses.csv": "address",
    "nodes-others.csv": "other",
}
RELATIONSHIPS_FILE = "relationships.csv"
FILES = tuple(NODE_FILES) + (RELATIONSHIPS_FILE,)
REQUIRED_COLUMNS = {
    "entity": {"node_id", "name", "sourceID"},
    "officer": {"node_id", "name", "sourceID"},
    "intermediary": {"node_id", "name", "sourceID"},
    "address": {"node_id", "sourceID"},
    "other": {"node_id", "name", "sourceID"},
    "relationship": {"node_id_start", "node_id_end", "rel_type", "sourceID"},
}
ROLE_RELS = frozenset({"officer_of", "intermediary_of"})
ADDRESS_RELS = frozenset({"registered_address"})
# ICIJ's own identity-like links, quoted and only ever offered for review.
SAME_RELS = frozenset({"same_as", "same_name_as", "same_id_as", "same_company_as", "same_intermediary_as",
                       "probably_same_officer_as"})
SIMILAR_RELS = frozenset({"similar", "similar_company_as"})
IDENTITY_BASIS = {**{r: "source-stated-link" for r in SAME_RELS}, **{r: "similar-name" for r in SIMILAR_RELS}}
MAX_SEEDS = 200
MAX_NODES = 1000
DEFAULT_MAX_NODES = 200
# A legal-form token in an officer or intermediary name classifies the node as a
# legal entity; the token found is recorded. Configured, never inferred further.
LEGAL_FORM_TOKENS = (
    "LIMITED", "LTD", "INC", "INCORPORATED", "CORP", "CORPORATION", "COMPANY", "LLC", "LLP", "LP", "PLC",
    "SA", "S A", "AG", "GMBH", "BV", "NV", "SARL", "SRL", "SPA", "PTE", "PTY",
    "TRUST", "TRUSTEES", "FOUNDATION", "STIFTUNG", "ANSTALT", "BANK", "HOLDINGS", "HOLDING", "SERVICES",
    "NOMINEES", "ASSOCIATES", "PARTNERS", "SECRETARIES", "MANAGEMENT", "INVESTMENTS", "GROUP", "BEARER",
)
# ICIJ jurisdiction descriptions mapped to ISO 3166-1 alpha-2. A description
# outside the table leaves the jurisdiction unknown (the text stays in native).
JURISDICTION_NAMES = {
    "british virgin islands": "VG", "panama": "PA", "bahamas": "BS", "seychelles": "SC", "samoa": "WS",
    "niue": "NU", "anguilla": "AI", "hong kong": "HK", "united kingdom": "GB", "malta": "MT",
    "cayman islands": "KY", "bermuda": "BM", "cook islands": "CK", "isle of man": "IM", "jersey": "JE",
    "guernsey": "GG", "cyprus": "CY", "mauritius": "MU", "belize": "BZ", "singapore": "SG", "luxembourg": "LU",
    "liechtenstein": "LI", "switzerland": "CH", "ireland": "IE", "netherlands": "NL", "marshall islands": "MH",
}
FORBIDDEN_KEYS = frozenset({
    "wrongdoing", "wrongdoing_verdict", "verdict", "guilt", "guilty", "illegal", "illegality", "criminal",
    "crime", "fraud", "fraudulent", "suspicious", "suspicion", "money_laundering", "aml", "aml_flag",
    "aml_verdict", "aml_risk", "sanctions", "sanctioned", "sanctions_verdict", "sanctions_match", "risk",
    "risk_score", "risk_level", "red_flag", "red_flags", "tax_evasion", "determination", "compliance_verdict",
})
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                       "NOV", "DEC"), start=1)}

PROVIDER_CONTRACT: dict[str, Any] = {
    "status": "implemented",
    "connector": "icij-offshore-leaks (offline extract replay) and the bulk-release adapter icij-offshore-leaks",
    "documentation": "https://offshoreleaks.icij.org/pages/database",
    "access": "one bulk ZIP of CSV files (nodes per type plus relationships) from the ICIJ download page; "
              "file names and columns as recorded in the audit (verify against the downloaded release)",
    "authentication": "none",
    "rate_limits": "one archive download per release through the bulk runner's quota ledger and byte budget",
    "pagination": "none; a bounded selection of seed nodes and their relationships per run",
    "cadence": "per published release (irregular; the database is updated when an investigation adds data)",
    "terms": LICENSE + ". ICIJ cautions that there are legitimate uses for offshore companies and trusts and that "
             "inclusion does not imply illegal or improper conduct; the caveat travels on every record",
    "retained_evidence": "the bulk manifest and filtered subset (gzipped JSONL) per release; per-file SHA-256 on "
                         "every record; ownership record revisions",
    "identifiers": ["icij node_id (per release; ICIJ node page https://offshoreleaks.icij.org/nodes/<id>)",
                    "sourceID (leak dataset name, verbatim)", "ibcRUC (registry number as leaked, native only)"],
    "coverage": "explicitly selected seed node ids (1-200) or a sourceID/jurisdiction filter with max_nodes (<= 1000); "
                "one relationship hop; never the whole database",
    "personal_data": "officers and intermediaries without a legal-form token are natural persons: stored "
                     "owner-scoped (redacted for others) unless NOESIS_PERSON_RESEARCH is on; a person's address "
                     "is never stored; person-to-person identity links are offered only in person-research mode",
    "identity": "ICIJ same_as/similar links are quoted and offered as reviewable candidates (similar links can "
                "never be accepted); registry matches are candidates only; nothing is merged or auto-accepted",
    "exclusions": "no wrongdoing, AML, sanctions or risk verdict; leak roles are never ownership or control "
                  "assertions; no scraping of the ICIJ web application",
    "unavailable_fallback": "a missing file or column fails the run as schema_drift; earlier records stay current",
    "cost": "free",
}
LIVE_VERIFICATION = {
    "status": "unverified-live", "last_run": None, "result": "not-attempted",
    "failure_code": "no_verified_download",
    "cause": "offshoreleaks.icij.org and offshoreleaks-data.icij.org are not reachable from this runtime; the "
             "download URL, file names and columns are recorded as 'verify' in the audit",
    "evidence": "docs/development/offshore-leaks-evidence/source-audit.md",
}


class OffshoreLeaksError(ValueError):
    def __init__(self, code: str, message: str, *, paths: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.paths = list(paths)


# ------------------------------------------------------------------ guards


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Paths of keys that would carry a wrongdoing, AML, sanctions or risk verdict."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def guard(value: Any) -> Any:
    """Refuse any import output that carries a verdict key; return it unchanged otherwise."""
    found = forbidden_keys(value)
    if found:
        raise OffshoreLeaksError("verdict_forbidden", "offshore-leaks output never carries a wrongdoing, AML, "
                                 "sanctions or risk verdict", paths=found)
    return value


def person_research() -> bool:
    from src.config.env import person_research_enabled

    return person_research_enabled()


def check_person_policy(records: Iterable[Mapping[str, Any]]) -> None:
    """Records stored under the person-research policy are refused while the mode is off."""
    if person_research():
        return
    for item in records:
        if dict(item.get("native") or {}).get("person_policy") == PERSON_POLICY:
            raise OffshoreLeaksError("minimisation_violation", "a record stored under the person-research policy "
                                     "is refused while NOESIS_PERSON_RESEARCH is off")


# ------------------------------------------------------------------ parsing


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_csv(raw: bytes, kind: str, name: str) -> list[dict[str, str]]:
    """Rows of one CSV file; the required columns must be present (schema drift otherwise)."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as exc:
        raise SourcePackError("schema_drift", f"{name} is empty") from exc
    missing = REQUIRED_COLUMNS[kind] - set(header)
    if missing:
        raise SourcePackError("schema_drift", f"{name} lacks columns: {', '.join(sorted(missing))}")
    return [dict(zip(header, (v.strip() for v in values))) for values in reader if any(v.strip() for v in values)]


def iter_csv(handle: Any, kind: str, name: str) -> Iterator[dict[str, str]]:
    """Streaming variant over a binary handle (bulk archives)."""
    text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.reader(text)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return
    missing = REQUIRED_COLUMNS[kind] - set(header)
    if missing:
        raise SourcePackError("schema_drift", f"{name} lacks columns: {', '.join(sorted(missing))}")
    for values in reader:
        if any(v.strip() for v in values):
            yield dict(zip(header, (v.strip() for v in values)))


def parse_date(value: Any) -> str | None:
    """ISO dates and ICIJ's ``DD-MON-YYYY`` form; anything else stays unknown (the text stays in native)."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        return text
    match = re.fullmatch(r"(\d{1,2})-([A-Z]{3})-(\d{4})", text)
    if match and match.group(2) in _MONTHS:
        return f"{match.group(3)}-{_MONTHS[match.group(2)]:02d}-{int(match.group(1)):02d}"
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def jurisdiction_code(description: Any) -> str | None:
    return JURISDICTION_NAMES.get(str(description or "").strip().casefold())


def legal_form(name: str) -> str | None:
    words = re.sub(r"[^A-Z0-9&]+", " ", str(name).upper()).split()
    joined = " " + " ".join(words) + " "
    for token in LEGAL_FORM_TOKENS:
        if f" {token} " in joined:
            return token
    if "&" in words:
        return "&"
    return None


# ---------------------------------------------------------------- selection


def validate_selection(selection: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a bounded selection; a selection that does not bound the import is refused."""
    node_ids = sorted({str(n).strip() for n in selection.get("node_ids") or [] if str(n).strip()})
    datasets = sorted({str(d).strip() for d in selection.get("datasets") or [] if str(d).strip()})
    jurisdictions = sorted({str(j).strip().upper() for j in selection.get("jurisdictions") or [] if str(j).strip()})
    if any(not re.fullmatch(r"[0-9A-Za-z_.-]{1,40}", n) for n in node_ids):
        raise SourcePackError("unbounded_source", "node ids are ICIJ node_id values")
    if len(node_ids) > MAX_SEEDS:
        raise SourcePackError("unbounded_source", f"select at most {MAX_SEEDS} seed node ids")
    if not node_ids and not (datasets or jurisdictions):
        raise SourcePackError("unbounded_source", "select seed node_ids, or datasets/jurisdictions with max_nodes")
    if not node_ids and selection.get("max_nodes") is None:
        raise SourcePackError("unbounded_source", "a dataset or jurisdiction filter needs an explicit max_nodes")
    try:
        max_nodes = int(selection.get("max_nodes") or DEFAULT_MAX_NODES)
        hops = int(selection.get("hops", 1))
    except (TypeError, ValueError) as exc:
        raise SourcePackError("unbounded_source", "max_nodes and hops are integers") from exc
    if not 1 <= max_nodes <= MAX_NODES or hops not in (0, 1):
        raise SourcePackError("unbounded_source", f"max_nodes is 1-{MAX_NODES} and hops is 0 or 1")
    overrides = {str(k): str(v) for k, v in dict(selection.get("node_kinds") or {}).items()}
    if set(overrides.values()) - {"person", "legal_entity"}:
        raise SourcePackError("invalid_mapping", "node_kinds overrides are person or legal_entity")
    return {"node_ids": node_ids, "datasets": datasets, "jurisdictions": jurisdictions, "max_nodes": max_nodes,
            "hops": hops, "node_kinds": dict(sorted(overrides.items()))}


def _seed(row: Mapping[str, str], kind: str, selection: Mapping[str, Any]) -> bool:
    if row.get("node_id") in set(selection["node_ids"]):
        return True
    if kind != "entity" or not (selection["datasets"] or selection["jurisdictions"]):
        return False
    if selection["datasets"] and row.get("sourceID") not in selection["datasets"]:
        return False
    return not (selection["jurisdictions"]
                and str(row.get("jurisdiction") or "").upper() not in selection["jurisdictions"])


def select_subgraph(rows: Callable[[str], Iterable[Mapping[str, str]]],
                    selection: Mapping[str, Any]) -> dict[str, Any]:
    """Select seeds, the relationships touching them and their other ends, within ``max_nodes``.

    ``rows(file_name)`` yields the rows of one file (an absent file yields
    nothing). Three passes: seeds, relationships, node rows.
    """
    selection = validate_selection(selection)
    cap = selection["max_nodes"]
    seeds: list[str] = []
    truncated = False
    for name, kind in NODE_FILES.items():
        for row in rows(name):
            if _seed(row, kind, selection) and row["node_id"] not in seeds:
                if len(seeds) >= cap:
                    truncated = True
                    continue
                seeds.append(row["node_id"])
    wanted = set(seeds)
    edges: list[dict[str, str]] = []
    neighbours: list[str] = []
    if selection["hops"]:
        for index, row in enumerate(rows(RELATIONSHIPS_FILE)):
            start, end = row.get("node_id_start"), row.get("node_id_end")
            if start not in wanted and end not in wanted:
                continue
            for other in (start, end):
                if other not in wanted and other not in neighbours:
                    if len(seeds) + len(neighbours) >= cap:
                        truncated = True
                        continue
                    neighbours.append(other)
            edges.append({**row, "_line": index + 2})
    included = wanted | set(neighbours)
    nodes: dict[str, dict[str, str]] = {}
    for name, kind in NODE_FILES.items():
        for index, row in enumerate(rows(name)):
            if row.get("node_id") in included and row["node_id"] not in nodes:
                nodes[row["node_id"]] = {**row, "_file": name, "_type": kind, "_line": index + 2}
    return {"selection": selection, "seeds": seeds, "nodes": nodes, "relationships": edges,
            "missing_nodes": sorted(included - set(nodes)), "truncated": truncated}


# ------------------------------------------------------------------ mapping


def node_key(node_id: str) -> str:
    return f"{PROVIDER}:node:{node_id}"


def _source(row: Mapping[str, Any], *, record_id: str, file_name: str, line: int, file_sha: str | None,
            node_id: str, release: str | None) -> dict[str, Any]:
    return {"provider": PROVIDER, "provider_record_id": record_id, "url": NODE_URL.format(node_id=node_id),
            "publisher": PUBLISHER, "source_type": f"leak dataset: {row.get('sourceID') or 'unstated'}",
            "license": LICENSE, "raw_sha256": file_sha, "revision": release,
            "locator": {"field": file_name, "line": line}, "note": CAVEAT}


def _entity_kind(node: Mapping[str, str], selection: Mapping[str, Any]) -> tuple[str, str]:
    override = selection["node_kinds"].get(node["node_id"])
    if override:
        return override, "selection override"
    if node["_type"] in {"entity", "other"}:
        return "legal_entity", f"ICIJ {node['_type']} node"
    token = legal_form(node.get("name") or "")
    if token:
        return "legal_entity", f"legal-form token '{token}' in the {node['_type']} name"
    return "person", f"ICIJ {node['_type']} node without a legal-form token (treated as a natural person)"


def map_subgraph(subgraph: Mapping[str, Any], *, file_shas: Mapping[str, str] | None = None,
                 release: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Ownership records and coverage for one selected subgraph (see the module docstring)."""
    selection = subgraph["selection"]
    file_shas = dict(file_shas or {})
    research = person_research()
    nodes: Mapping[str, Mapping[str, str]] = subgraph["nodes"]
    kinds = {nid: _entity_kind(n, selection) for nid, n in nodes.items() if n["_type"] != "address"}
    links: dict[str, list[dict[str, Any]]] = {nid: [] for nid in kinds}
    addresses: dict[str, list[dict[str, Any]]] = {nid: [] for nid in kinds}
    roles: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "selection": dict(selection), "seeds": list(subgraph["seeds"]), "truncated": subgraph["truncated"],
        "missing_nodes": list(subgraph["missing_nodes"]), "nodes": {}, "datasets": {}, "relationships": {},
        "role_links": 0, "quoted_links": 0, "addresses_attached": 0, "person_addresses_withheld": 0,
        "relationships_with_unselected_end": 0, "person_research": research, "caveat": CAVEAT,
        "notice": NO_VERDICT,
    }
    for node in nodes.values():
        coverage["nodes"][node["_type"]] = coverage["nodes"].get(node["_type"], 0) + 1
        dataset = node.get("sourceID") or "unstated"
        coverage["datasets"][dataset] = coverage["datasets"].get(dataset, 0) + 1
    for edge in subgraph["relationships"]:
        rel = edge.get("rel_type") or "unstated"
        coverage["relationships"][rel] = coverage["relationships"].get(rel, 0) + 1
        start, end = edge.get("node_id_start"), edge.get("node_id_end")
        label = edge.get("link") or rel
        quoted = {"rel_type": rel, "link": label, "dataset": edge.get("sourceID") or None,
                  "node_id_start": start, "node_id_end": end, "start_date": edge.get("start_date") or None,
                  "end_date": edge.get("end_date") or None, "status": edge.get("status") or None,
                  "locator": {"field": RELATIONSHIPS_FILE, "line": edge["_line"]}}
        if start not in nodes or end not in nodes:
            coverage["relationships_with_unselected_end"] += 1
            continue
        if rel in ROLE_RELS and start in kinds and end in kinds:
            roles.append({**quoted, "_edge": edge})
            continue
        if rel in ADDRESS_RELS and start in kinds and nodes[end]["_type"] == "address":
            if kinds[start][0] == "person":
                coverage["person_addresses_withheld"] += 1
                continue
            address = nodes[end]
            addresses[start].append({"address": address.get("address") or address.get("name") or None,
                                     "countries": address.get("countries") or None,
                                     "country_codes": address.get("country_codes") or None,
                                     "icij_address_node": end, "link": label, "dataset": edge.get("sourceID") or None,
                                     "locator": quoted["locator"]})
            coverage["addresses_attached"] += 1
            continue
        if start in kinds and end in kinds:
            other = {"other_key": node_key(end), "identity_basis": IDENTITY_BASIS.get(rel)}
            links[start].append({**quoted, **other, "direction": "outgoing"})
            links[end].append({**quoted, "other_key": node_key(start), "identity_basis": IDENTITY_BASIS.get(rel),
                               "direction": "incoming"})
            coverage["quoted_links"] += 1
        else:
            coverage["relationships_with_unselected_end"] += 1
    records: list[dict[str, Any]] = []
    try:
        for nid in sorted(kinds):
            node = nodes[nid]
            kind, basis = kinds[nid]
            source = _source(node, record_id=f"{node.get('sourceID') or 'unstated'}|node:{nid}",
                             file_name=node["_file"], line=node["_line"], file_sha=file_shas.get(node["_file"]),
                             node_id=nid, release=release)
            identifiers = [{"scheme": "icij-node", "value": nid, "authority": "ICIJ Offshore Leaks Database"}]
            native = {"node_type": node["_type"], "dataset": node.get("sourceID") or None,
                      "valid_until": node.get("valid_until") or None, "note": node.get("note") or None,
                      "classification_basis": basis, "icij_links": sorted(links[nid], key=_link_order),
                      "countries_as_published": node.get("countries") or None,
                      "country_codes_as_published": node.get("country_codes") or None}
            if kind == "person":
                if research:
                    native["person_policy"] = PERSON_POLICY
                records.append(record("person", node_key(nid), source, owner_scoped=not research,
                                      name=node.get("name") or "unnamed officer", identifiers=identifiers,
                                      native_kind=f"icij {node['_type']}", native=native))
                continue
            entity_addresses = list(addresses[nid])
            if node.get("address"):
                entity_addresses.insert(0, {"address": node["address"], "icij_address_node": None,
                                            "link": "address column", "dataset": node.get("sourceID") or None,
                                            "locator": {"field": node["_file"], "line": node["_line"]}})
            native.update({
                "jurisdiction_as_published": node.get("jurisdiction") or None,
                "jurisdiction_description": node.get("jurisdiction_description") or None,
                "incorporation_date_as_published": node.get("incorporation_date") or None,
                "inactivation_date": node.get("inactivation_date") or None,
                "struck_off_date": node.get("struck_off_date") or None, "dorm_date": node.get("dorm_date") or None,
                "service_provider": node.get("service_provider") or None,
                "ibcRUC": node.get("ibcRUC") or None, "internal_id": node.get("internal_id") or None,
            })
            other_names = [n for n in (node.get("original_name"), node.get("former_name")) if n]
            records.append(record(
                "legal_entity", node_key(nid), source, name=node.get("name") or "unnamed entity",
                jurisdiction=jurisdiction_code(node.get("jurisdiction_description")), identifiers=identifiers,
                entity_type=node.get("company_type") or None, status=node.get("status") or None,
                other_names=other_names or None, founding_date=parse_date(node.get("incorporation_date")),
                addresses=entity_addresses or None, native=native))
        for role in sorted(roles, key=_link_order):
            edge = role.pop("_edge")
            start, end = role["node_id_start"], role["node_id_end"]
            officer_kind = kinds[start][0]
            person = officer_kind == "person"
            digest = hashlib.sha256(json.dumps([edge.get(k) for k in sorted(edge) if k != "_line"]).encode()).hexdigest()
            source = _source(edge, record_id=f"{edge.get('sourceID') or 'unstated'}|rel:{start}->{end}|{role['rel_type']}",
                             file_name=RELATIONSHIPS_FILE, line=edge["_line"],
                             file_sha=file_shas.get(RELATIONSHIPS_FILE), node_id=end, release=release)
            native = {"rel_type": role["rel_type"], "link": role["link"], "dataset": role["dataset"],
                      "status": role["status"], "start_date_as_published": role["start_date"],
                      "end_date_as_published": role["end_date"], "officer_node_id": start, "entity_node_id": end,
                      "officer_node_type": nodes[start]["_type"],
                      "note": "ICIJ relationship label quoted verbatim; not an ownership or control assertion"}
            if person and research:
                native["person_policy"] = PERSON_POLICY
            records.append(record(
                "officer_role", f"{PROVIDER}:rel:{start}:{role['rel_type']}:{end}:{digest[:12]}", source,
                owner_scoped=(person and not research) or None, entity_key=node_key(end),
                officer={"name": nodes[start].get("name") or "unnamed", "key": node_key(start),
                         "kind": "person" if person else "entity"},
                role=role["link"], appointed_on=parse_date(role["start_date"]),
                resigned_on=parse_date(role["end_date"]), native=native))
            coverage["role_links"] += 1
    except OwnershipRecordError as exc:
        raise SourcePackError("mapping_failed", f"offshore-leaks mapping: {exc}") from exc
    check_person_policy(records)
    guard(records)
    coverage["records"] = len(records)
    return records, guard(coverage)


def _link_order(item: Mapping[str, Any]) -> tuple:
    return (str(item.get("rel_type")), str(item.get("node_id_start")), str(item.get("node_id_end")),
            str(item.get("locator")))


# --------------------------------------------------- source-pack adapter


class OffshoreLeaksAdapter:
    """Replays an authored or bulk-produced CSV extract; one selected subgraph per run.

    Live runs are refused: the release is a single archive acquired through the
    bulk-release runner and projected with ``project_bulk_subset``.
    """

    accepts_transport = True
    PAGE_RECORDS = 100

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        self.source = json.loads(json.dumps(source))
        self.selection_raw = dict(self.source.get("ownership") or {})
        self.transport = transport
        if urlsplit(self.source["endpoint"]).hostname != urlsplit(BULK_URL).hostname:
            raise SourcePackError("network_policy", "offshore-leaks sources use the declared ICIJ download host")
        self.base = str(self.selection_raw.get("extract") or "")
        if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", self.base) or ".." in self.base:
            raise SourcePackError("unbounded_source", "offshore-leaks sources name one extract path")
        self.selection = validate_selection(dict(self.selection_raw.get("offshore_leaks") or {}))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "ownership": {"provider": PROVIDER, "selection": self.selection},
        }
        self._pages: list[dict[str, Any]] | None = None

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _sha(json.dumps({"extract": self.base, "selection": self.selection}, sort_keys=True).encode())

    def _files(self) -> tuple[dict[str, bytes], dict[str, str]]:
        if self.transport is None:
            raise SourcePackError("bulk_release_required", "the ICIJ release is one archive: acquire it with the "
                                  "bulk runner (icij-offshore-leaks) and project it with project_bulk_subset")
        raw: dict[str, bytes] = {}
        limit = int(self.definition["limits"]["max_bytes"])
        for name in FILES:
            url = self.source["endpoint"].rstrip("/") + self.base.rstrip("/") + "/" + name
            response = self.transport(url=url, params={}, headers={"Accept": "text/csv"},
                                      timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
            status = int(response.get("status", 200))
            content = response.get("content", b"")
            body = content.encode() if isinstance(content, str) else bytes(content)
            if status == 404:
                if name in {"nodes-entities.csv", RELATIONSHIPS_FILE}:
                    raise SourcePackError("schema_drift", f"the extract lacks {name}")
                continue
            if status >= 400:
                raise SourcePackError("source_unavailable", f"extract file {name} returned HTTP {status}")
            if len(body) > limit:
                raise SourcePackError("response_too_large", "extract file exceeds the source byte limit")
            raw[name] = body
        return raw, {name: _sha(body) for name, body in raw.items()}

    def _build(self) -> list[dict[str, Any]]:
        raw, shas = self._files()
        parsed = {name: read_csv(body, NODE_FILES.get(name, "relationship"), name) for name, body in raw.items()}
        subgraph = select_subgraph(lambda name: parsed.get(name, []), self.selection)
        records, coverage = map_subgraph(subgraph, file_shas=shas, release=self.selection_raw.get("release"))
        chunks = [records[i:i + self.PAGE_RECORDS] for i in range(0, len(records), self.PAGE_RECORDS)] or [[]]
        size = sum(len(b) for b in raw.values())
        pages = []
        for number, chunk in enumerate(chunks):
            suffix = f"@{number}" if number else ""
            pages.append({"id": f"{PROVIDER}:{self.base}:{self._scope()[:12]}{suffix}",
                          "title": f"ICIJ Offshore Leaks extract {self.base}{suffix}", "language": "en",
                          "url": self.source["endpoint"],
                          "ownership_part": {"contract": PART_CONTRACT, "provider": PROVIDER, "part": "offshore-leaks",
                                             "subject": self.base, "raw_sha256": _sha(json.dumps(shas, sort_keys=True).encode()),
                                             "native": {"files": shas, "page": number, "pages": len(chunks)},
                                             "records": chunk, "outcome": "returned" if records else "none_selected",
                                             "coverage": coverage if number == 0 else {"page": number},
                                             "_size": size if number == 0 else 0}})
        return pages

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"} or dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "offshore-leaks runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        if self._pages is None:
            self._pages = self._build()
        index = int(state.get("i", 0))
        if index >= len(self._pages):
            return RuntimePage((), None, 0, receipt={"status": 200})
        envelope = json.loads(json.dumps(self._pages[index]))
        size = envelope["ownership_part"].pop("_size")
        more = index + 1 < len(self._pages)
        next_cursor = json.dumps({"i": index + 1, "scope": self._scope()}, sort_keys=True) if more else None
        part = envelope["ownership_part"]
        return RuntimePage((envelope,), next_cursor, size, receipt={
            "status": 200, "outcome": part["outcome"], "provider": PROVIDER, "part": part["part"],
            "subject": part["subject"], "response_sha256": part["raw_sha256"], "work_index": index,
            "work_size": len(self._pages), "records": len(part["records"])})


ADAPTERS = {CONNECTOR: OffshoreLeaksAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored CSV extract files by URL path; unlisted paths are HTTP 404."""
    by_path = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        page = by_path.get(urlsplit(url).path)
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        body = page.get("body")
        content = "\n".join(body) + "\n" if isinstance(body, list) else str(body or "")
        return {"status": int(page.get("status", 200)), "headers": {"content-type": "text/csv"},
                "content": content.encode()}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = OffshoreLeaksAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records

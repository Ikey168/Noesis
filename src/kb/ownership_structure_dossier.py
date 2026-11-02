"""One view of a holding structure and its links (``noesis-ownership-structure-dossier-v1``, #2890).

A structure dossier starts from one explicit identifier of an ownership entity
(as :func:`src.kb.ownership_store.OwnershipStore.lookup`; a name lookup returns
candidates, never a pick) and composes the *existing* record owners into
sections. Nothing is copied into a new store and nothing is summed across
sources: every line is what one owner returns, cited to that owner's record
revision. Sections (see :data:`SECTIONS`):

========================  =====================================================  ===============================
section                   record owner composed                                  reached through
========================  =====================================================  ===============================
``classification``        :mod:`src.kb.ownership_structures`                     the entity's own record keys
``ownership``             :class:`src.kb.ownership_graph.OwnershipGraph`         accepted ownership identity
``sec_holdings``          :mod:`src.kb.ownership_holdings` (13F, 13D/13G)        accepted ownership identity
``bafin_holdings``        :meth:`BafinQueries.holdings_of_holder`                accepted identity decisions
``lobbying``              :meth:`LobbyingQueries.registrant_declarations`        accepted identity decisions
``political_donations``   :class:`src.kb.campaign_finance_links.CampaignFinanceLinks`  links resting on accepted decisions
``public_payments``       :meth:`PublicFinanceStore.payments` (EU FTS, budgets)  accepted identity decisions
``competition``           :mod:`src.kb.competition_queries` (cases, state aid)   accepted identity decisions
``procurement``           :meth:`ProcurementIdentityService.award_history`       reviewed party links (exact ids)
``sanctions``             :meth:`SanctionsQueries.statement_as_of`               accepted identity decisions
``offshore_leaks``        :mod:`src.kb.offshore_leaks_links` (ICIJ, #2888)      accepted ownership identity
========================  =====================================================  ===============================

Each section reports a status: ``assembled`` (cited lines), ``none_on_record``
(the owner answered and nothing is linked - not a statement that nothing
exists), ``inert`` (the owning feature is not enabled: not the same as empty),
``not_available`` (no provider is installed), ``not_requested`` (no namespace
was given for it), or the owner's own refusal code (e.g. ``unauthorized``).
Proposed identity candidates are listed apart under ``pending`` and never
joined. A line without a citation is refused, so every section cites its own
records.

**Extension point.** :func:`register_section` installs a provider for a
section; a provider is ``provider(ctx: SectionContext) -> dict`` returning
``{"lines": [...], "pending": [...], "unknowns": [...], "notice": str}``, each
line carrying ``statement``, ``reached_by`` and a ``citation`` with ``id``,
``namespace``, ``record`` and ``revision``. A provider may also be given as a
``"module:function"`` path that is imported lazily: the ``offshore_leaks``
section points at :data:`OFFSHORE_LEAKS_PROVIDER` and reports
``not_available`` until that function exists; #2888 provides it in
:mod:`src.kb.offshore_leaks_links` (``inert`` while the offshore-leaks
feature is off).

Excluded by construction (:data:`FORBIDDEN_KEYS` is checked on every dossier):
no influence score, no wrongdoing verdict, no risk rating and no cross-source
total. Natural persons follow person-research mode: a dossier for a ``person``
record is refused unless ``NOESIS_PERSON_RESEARCH`` is on, and the record
owners keep their own person rules inside each section.

:func:`export_structure_bundle` writes the dossier as a verifiable
``noesis-evidence-bundle-v1``: the dossier as root receipt, one claim per
section and one evidence object per cited record revision; sections that are
not assembled are listed as omissions, so the bundle is ``partial`` rather
than silently complete.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.kb.ownership_records import READ_SCOPE, digest
from src.kb.ownership_store import OwnershipError, OwnershipStore

CONTRACT = "noesis-ownership-structure-dossier-v1"
EVIDENCE_CONTRACT = "noesis-ownership-structure-evidence-v1"
OFFSHORE_LEAKS_PROVIDER = "src.kb.offshore_leaks_links:structure_section"
NOTICE = ("Each section is what its own record owner states, cited to its record revisions and reached only through "
          "accepted identity decisions or exact identifiers. Sections are side by side and never summed. This is not "
          "an influence score, a wrongdoing verdict or a beneficial-ownership, sanctions or AML determination.")
EXCLUSIONS = ("influence scores", "wrongdoing or corruption verdicts", "risk ratings",
              "totals across sources", "structure types inferred from names")
FORBIDDEN_KEYS = frozenset({
    "influence_score", "influence", "influence_rating", "risk_score", "risk_rating", "wrongdoing", "verdict",
    "guilt", "guilty", "culpability", "corruption_score", "suspicion_score", "suspicious", "quid_pro_quo",
    "conflict_of_interest_score", "total_across_sources", "cross_source_total", "grand_total",
})
LIVE = ("assembled", "none_on_record")


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Paths of keys a structure dossier never carries (influence, verdicts, risk, cross-source totals)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


# ------------------------------------------------------------------ context and registry


@dataclass
class SectionContext:
    """What a section provider gets: the resolved structure and the caller's request for its section."""

    conn: Any
    namespace: str
    root: str
    member_keys: list[str]
    entity_ids: list[str]
    identifiers: list[dict[str, Any]]
    as_of: str | None
    principal_id: str | None
    scopes: set[str]
    graph: Any
    person: bool
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def section_namespace(self) -> str | None:
        value = self.request.get("namespace")
        return str(value) if value else None


@dataclass(frozen=True)
class SectionSpec:
    name: str
    title: str
    owner: str
    provider: Callable[[SectionContext], dict[str, Any]] | str | None
    enabled: Callable[[Any, str | None], bool] | None = None
    feature: str | None = None
    needs_namespace: bool = True


SECTIONS: dict[str, SectionSpec] = {}


def register_section(spec: SectionSpec) -> None:
    """Install or replace a section provider (e.g. ``offshore_leaks`` once #2888 lands)."""
    SECTIONS[spec.name] = spec


def unregister_section(name: str) -> SectionSpec | None:
    return SECTIONS.pop(name, None)


def _resolve_provider(spec: SectionSpec) -> Callable[[SectionContext], dict[str, Any]] | None:
    if spec.provider is None or callable(spec.provider):
        return spec.provider
    module, _, attribute = str(spec.provider).partition(":")
    try:
        return getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError):
        return None


def _gate(module: str, attribute: str = "feature_enabled", *args: Any) -> Callable[[Any, str | None], bool]:
    def enabled(conn: Any, namespace: str | None) -> bool:
        check = getattr(importlib.import_module(module), attribute)
        return bool(check(conn, *args) if args else check(conn, namespace))
    return enabled


def _campaign_finance_enabled(conn: Any, namespace: str | None) -> bool:
    from src.kb.campaign_finance_records import FEATURES, feature_enabled

    del namespace
    return any(feature_enabled(conn, feature) for feature in FEATURES.values())


def _competition_enabled(conn: Any, namespace: str | None) -> bool:
    from src.kb.competition import feature_enabled

    del namespace
    return feature_enabled(conn)


# ------------------------------------------------------------------ helpers


def _cite(section: str, namespace: str, record: Any, revision: Any, **extra: Any) -> dict[str, Any]:
    return {"id": f"{section}:{record}@{revision}", "namespace": namespace, "record": str(record),
            "revision": revision, **{k: v for k, v in extra.items() if v is not None}}


def _identity_links(ctx: SectionContext, namespace: str, prefixes: tuple[str, ...]
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accepted (and, apart, proposed) identity candidates joining the structure's record keys to another owner's keys.

    Reads the shared reviewable state machine's candidates in the other owner's namespace; only ``accepted``
    candidates join, ``proposed`` ones are returned to be listed apart, rejected and reverted ones never join.
    """
    if not ctx.conn.execute("SELECT 1 FROM information_schema.tables "
                            "WHERE table_name='ownership_identity_candidates'").fetchone():
        return [], []
    keys = sorted(set(ctx.member_keys) | set(ctx.entity_ids))
    marks = ",".join("?" * len(keys))
    rows = ctx.conn.execute(
        "SELECT candidate_id, left_key, right_key, basis, state, decision_id FROM ownership_identity_candidates "
        f"WHERE namespace=? AND state IN ('accepted', 'proposed') AND (left_key IN ({marks}) OR right_key IN ({marks})) "
        "ORDER BY candidate_id", [namespace, *keys, *keys]).fetchall()
    accepted, pending = [], []
    for candidate_id, left, right, basis, state, decision_id in rows:
        mine, other = (left, right) if left in keys else (right, left)
        if not other.startswith(prefixes):
            continue
        item = {"candidate_id": candidate_id, "record_key": other, "ownership_key": mine, "basis": basis,
                "decision_id": decision_id}
        (accepted if state == "accepted" else pending).append(item)
    return accepted, pending


def _reached(link: Mapping[str, Any]) -> dict[str, Any]:
    return {"basis": "accepted-identity-decision", "candidate_id": link["candidate_id"],
            "decision_id": link.get("decision_id"), "match_basis": link.get("basis"),
            "ownership_key": link.get("ownership_key")}


def _pending(pending: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**p, "state": "proposed", "note": "a proposed candidate, not an identity decision: listed apart and never "
                                               "joined until a reviewer accepts it"} for p in pending]


def _graph_cite(ctx: SectionContext, section: str, edge: Mapping[str, Any]) -> dict[str, Any]:
    source = edge.get("source") or {}
    return _cite(section, ctx.namespace, edge["record_id"], edge["revision"], revision_id=edge.get("revision_id"),
                 provider=source.get("provider"), url=source.get("url"), locator=source.get("locator"))


def _share_text(share: Mapping[str, Any] | None) -> str:
    share = share or {}
    if share.get("exact") is not None:
        return f"{share['exact']}%"
    band = share.get("band") or {}
    if band:
        return f"{band.get('min') if band.get('min') is not None else '?'}-" \
               f"{band.get('max') if band.get('max') is not None else '?'}% band"
    return "no percentage stated"


# ------------------------------------------------------------------ section providers


def _classification_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.ownership_structures import StructureClassifications

    found = StructureClassifications(ctx.conn, initialize=False).classification(ctx.namespace, ctx.member_keys,
                                                                                scopes=ctx.scopes)
    lines = []
    for item in found["accepted"]:
        citation = item["citation"]
        if citation["kind"] == "ownership-record":
            cite = _cite("classification", ctx.namespace, citation["record_id"], citation["revision"],
                         revision_id=citation.get("revision_id"), provider=citation.get("provider"),
                         url=citation.get("url"), locator={"field": citation["field"], **dict(
                             citation.get("locator") or {})})
        else:
            cite = _cite("classification", ctx.namespace, citation["reference"], citation["sha256"],
                         provider=citation.get("publisher"), url=citation.get("url"), locator=citation["locator"])
        lines.append({"statement": f"{item['structure_type']} as stated ({item['basis']}): \"{item['stated_value']}\"",
                      "reached_by": {"basis": "own-record", "entity_key": item["entity_key"],
                                     "classification_id": item["classification_id"],
                                     "decision_id": (item["decision"] or {}).get("decision_id"),
                                     "reviewer": (item["decision"] or {}).get("by")},
                      "citation": cite,
                      "data": {k: item[k] for k in ("classification_id", "entity_key", "structure_type", "basis",
                                                    "stated_value")}})
    return {"lines": lines, "pending": [{k: p[k] for k in ("classification_id", "entity_key", "structure_type",
                                                          "basis", "stated_value")} for p in found["pending"]],
            "classification": {k: found[k] for k in ("status", "structure_types", "differ")},
            "not_in_force": found["not_in_force"], "notice": found["notice"]}


def _ownership_section(ctx: SectionContext) -> dict[str, Any]:
    graph, root = ctx.graph, ctx.root
    lines, unknowns = [], []

    def edge_line(direction: str, relation: str, edge: Mapping[str, Any], counterpart: str) -> dict[str, Any]:
        holder = edge.get("holder") or {}
        return {"statement": f"{(edge.get('source') or {}).get('provider')} states {edge['assertion_kind']} "
                             f"({_share_text(edge.get('share'))}); as-of status {edge['as_of_status']}",
                "reached_by": {"basis": "ownership-graph", "direction": direction, "relation": relation,
                               "identity": graph.identity},
                "citation": _graph_cite(ctx, "ownership", edge),
                "data": {"direction": direction, "relation": relation, "assertion_kind": edge["assertion_kind"],
                         "subject": edge["subject"], "holder_entity": holder.get("entity"),
                         "holder_name": None if edge.get("redacted") else holder.get("name"),
                         "counterpart": counterpart, "share": edge.get("share"), "validity": edge.get("validity"),
                         "as_of_status": edge["as_of_status"], "control_basis": edge.get("control_basis"),
                         "withheld": bool(edge.get("redacted"))}}

    for name in graph.describe(root)["names"]:
        if name["redacted"]:
            continue
        lines.append({"statement": f"{name['provider']} records {name['name']} ({name['record_key']})",
                      "reached_by": {"basis": "ownership-graph", "relation": "entity record"},
                      "citation": _cite("ownership", ctx.namespace, name["record_id"], name["revision"],
                                        provider=name["provider"]),
                      "data": {"relation": "entity record", "record_key": name["record_key"], "name": name["name"],
                               "jurisdiction": name["jurisdiction"]}})
    parents = graph.direct_parents(root, ctx.as_of)
    ultimate = graph.ultimate_parents(root, ctx.as_of)
    for relation, result in (("direct parent or controller", parents), ("ultimate parent", ultimate)):
        for group in result["groups"]:
            lines.extend(edge_line("up", relation, e, group["holder"]) for e in group["assertions"])
        for edge in result["reporting_exceptions"]:
            lines.append({**edge_line("up", "reporting exception", edge, ""),
                          "statement": f"{edge['source']['provider']} states a reporting exception "
                                       f"{edge['reporting_exception']['category']}"})
    for group in (parents.get("other_holdings") or {}).get("minority_as_stated") or []:
        lines.append(edge_line("up", "minority holder as stated", group, (group.get("holder") or {}).get("entity")))
    for edge in (parents.get("other_holdings") or {}).get("person_or_unidentified_holders") or []:
        lines.append(edge_line("up", "person or unidentified holder", edge, ""))
    subsidiaries = graph.subsidiaries(root, ctx.as_of)
    for item in subsidiaries["subsidiaries"]:
        lines.extend(edge_line("down", "subsidiary or controlled entity", e, item["subject"]) for e in item["assertions"])
    for item in subsidiaries["minority_holdings_as_stated"]:
        lines.extend(edge_line("down", "minority holding as stated", e, item["subject"]) for e in item["assertions"])
    chain = graph.control_chain(root, ctx.as_of)
    conflicts = [{"slot": slot, **c} for slot, r in (("direct_parents", parents), ("ultimate_parents", ultimate))
                 for c in r["conflicts"]]
    if not parents["groups"] and not parents["reporting_exceptions"]:
        unknowns.append("no acquired source states a direct parent or a reporting exception")
    return {"lines": lines, "unknowns": unknowns, "conflicts": conflicts,
            "control_chain": {"tops": chain["tops"], "cycles": chain["cycles"], "truncated": chain["truncated"],
                              "note": "chain tops are a derived path, not a stated ultimate parent"},
            "notice": parents["notice"]}


def _sec_holdings_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.ownership_holdings import holdings

    answer = holdings(ctx.graph, ctx.root, ctx.as_of)
    lines = []
    for line in answer["institutional_holdings"] + answer["beneficial_ownership"]:
        citation = line["citation"]
        lines.append({"statement": f"{citation.get('form_type')} {citation['accession_number']}: {line['statement']} "
                                   f"in {line['subject']} as of {line['reference_date']}",
                      "reached_by": {"basis": "ownership-graph", "relation": "holdings of this filer",
                                     "identity": ctx.graph.identity},
                      "citation": _cite("sec_holdings", ctx.namespace, citation["record_id"], citation["revision"],
                                        revision_id=citation.get("revision_id"), provider=citation.get("provider"),
                                        url=citation.get("document_url"), locator=citation.get("locator"),
                                        accession_number=citation["accession_number"]),
                      "data": {k: line.get(k) for k in ("statement", "subject", "reference_date", "share", "position",
                                                        "powers", "group", "chain")}})
    return {"lines": lines, "withheld": answer["withheld"], "superseded": answer["superseded"],
            "pending": answer["identity"]["pending"], "notice": answer["filing_notice"]}


def _bafin_holdings_section(ctx: SectionContext) -> dict[str, Any]:
    from src.domains.market.bafin_queries import HOLDER_PREFIXES, BafinQueries

    namespace = ctx.section_namespace
    accepted, pending = _identity_links(ctx, namespace, HOLDER_PREFIXES)
    queries = BafinQueries(ctx.conn)
    lines = []
    for link in accepted:
        answer = queries.holdings_of_holder(namespace, ctx.as_of or date.today().isoformat(), scopes=ctx.scopes,
                                            party=link["record_key"])
        for group in answer["issuers"]:
            for item in group["holdings"] + group["notified_below_lowest_threshold"]:
                lines.append({"statement": f"voting-rights notification of {item['publication_date']}: "
                                           + "; ".join(f"{p['basis']} {p['percentage']}%" for p in item["positions"]
                                                       if p.get("percentage") is not None),
                              "reached_by": _reached(link),
                              "citation": _cite("bafin_holdings", namespace, item["notice_id"], item["revision_id"],
                                                provider=item["source"].get("provider"), url=item["source"].get("url"),
                                                locator=item["source"].get("locator")),
                              "data": {"issuer": group["issuer"], "role": item.get("role"),
                                       "positions": item["positions"], "event_date": item.get("event_date"),
                                       "stale": item["stale"]}})
    return {"lines": lines, "pending": _pending(pending),
            "notice": "each percentage beside its WpHG basis, never summed across issuers or bases"}


def _lobbying_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.lobbying import READ_SCOPE as LOBBYING_READ
    from src.kb.lobbying import authorize
    from src.kb.lobbying_queries import LobbyingQueries

    namespace = ctx.section_namespace
    authorize(namespace, ctx.scopes, LOBBYING_READ)
    accepted, pending = _identity_links(ctx, namespace, ("lobbying:",))
    queries = LobbyingQueries(ctx.conn)
    lines, unknowns = [], []
    for link in accepted:
        registrant, _, client = link["record_key"].partition(":client:")
        _, register, native_id = registrant.split(":", 2)
        answer = queries.registrant_declarations(namespace, scopes=ctx.scopes, register=register, native_id=native_id,
                                                 as_of=ctx.as_of)
        for record in answer["registrants"]:
            declaration = record["in_force"]
            if declaration is None:
                unknowns.append(f"{link['record_key']}: no declaration in force as of {ctx.as_of or 'today'}")
                continue
            citation = declaration["citation"]
            role = "declared client" if client else "registrant"
            lines.append({"statement": f"{declaration['register_label']} {declaration['native_id']}: "
                                       f"{declaration['name_as_filed']} ({role}), effective {declaration['effective_on']}",
                          "reached_by": _reached(link),
                          "citation": _cite("lobbying", namespace, declaration["record_key"],
                                            citation["register_revision_id"], revision_no=citation["revision_no"],
                                            provider=declaration["register"], source_id=citation.get("source_id")),
                          "data": {"role": role, "register": declaration["register"],
                                   "declared_interests": declaration["declared_interests"],
                                   "spend_ranges_as_filed": declaration["spend_ranges"],
                                   "clients": [c.get("name") for c in declaration["clients"]],
                                   "period": declaration["period"], "unknown": declaration["unknown"]}})
    return {"lines": lines, "pending": _pending(pending), "unknowns": unknowns,
            "notice": "declarations as filed; spend ranges stay ranges and are never summed"}


def _donations_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.campaign_finance_links import NOTICE as LINK_NOTICE
    from src.kb.campaign_finance_links import CampaignFinanceLinks
    from src.kb.campaign_finance_queries import CampaignFinanceQueries

    namespace = ctx.section_namespace
    links = CampaignFinanceLinks(ctx.conn, initialize=False)
    queries = CampaignFinanceQueries(ctx.conn)
    lines, seen = [], set()
    for key in ctx.member_keys:
        for link in links.links(namespace, scopes=ctx.scopes, kind="ownership", target_key=key):
            if link["link_id"] in seen:
                continue
            seen.add(link["link_id"])
            rows = links.store.records(namespace, scopes=ctx.scopes, record_keys=[link["record_key"]])
            item = queries.item_view(rows[0], ctx.scopes) if rows else None
            reached = {"basis": "accepted-identity-decision",
                       "candidate_id": link["basis"].get("identity_candidate_id"), "link_id": link["link_id"],
                       "match_basis": link["basis"].get("method"), "ownership_key": key}
            if item is None:
                statement, data = "a reported item linked to this entity (details withheld for this principal)", {
                    "withheld": True}
            else:
                statement = (f"{item['record_kind']} of {item.get('amount_as_reported')} {item.get('currency') or ''} "
                             f"reported in {item['filing_key']}").replace("  ", " ")
                data = {k: item.get(k) for k in ("record_kind", "committee_key", "filing_key", "amount_as_reported",
                                                 "currency", "date_as_reported", "donation_type", "reporting_period")}
            lines.append({"statement": statement, "reached_by": reached,
                          "citation": _cite("political_donations", namespace, link["record_key"],
                                            link["record_revision_id"], filing_key=link.get("filing_key"),
                                            filing_revision_id=link.get("filing_revision_id"),
                                            provider=(item or {}).get("citation", {}).get("provider")),
                          "data": data})
    return {"lines": lines, "notice": LINK_NOTICE + "; amounts are as reported per item and never summed"}


def _public_payments_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.public_finance import READ_SCOPE as PF_READ
    from src.kb.public_finance import PublicFinanceStore, authorize
    from src.kb.public_finance_identity import KEY_PREFIX
    from src.kb.public_finance_queries import citation

    namespace = ctx.section_namespace
    authorize(namespace, ctx.scopes, PF_READ)
    accepted, pending = _identity_links(ctx, namespace, (KEY_PREFIX,))
    store = PublicFinanceStore(ctx.conn, initialize=False)
    lines = []
    for link in accepted:
        for payment in store.payments(namespace, beneficiary_key=link["record_key"]):
            cite = citation(payment)
            lines.append({"statement": f"{payment['provider']} {payment['payment_kind'] or 'payment'} of "
                                       f"{payment['amount_text']} {payment['currency'] or ''} "
                                       f"({payment['fiscal_year']}, {payment['programme'] or 'programme not stated'})",
                          "reached_by": _reached(link),
                          "citation": _cite("public_payments", namespace, payment["payment_id"], payment["revision_no"],
                                            provider=cite["provider"], url=cite["source_url"],
                                            release_id=cite["release_id"], file_sha256=cite["file_sha256"],
                                            document=cite["document"]),
                          "data": {k: payment.get(k) for k in ("beneficiary", "fiscal_year", "payment_kind",
                                                               "accounting_basis", "programme", "budget_line",
                                                               "amount_text", "amount", "currency", "published_on")}})
    return {"lines": lines, "pending": _pending(pending),
            "notice": "published commitment or payment rows as stated; amounts are never summed across rows or "
                      "providers"}


def _competition_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.competition_queries import awards_for_beneficiary, cases_for_company

    namespace = ctx.section_namespace
    cases = cases_for_company(ctx.conn, namespace, ctx.root, ownership_namespace=ctx.namespace, scopes=ctx.scopes,
                              as_of=ctx.as_of, principal_id=ctx.principal_id)
    awards = awards_for_beneficiary(ctx.conn, namespace, ctx.root, ownership_namespace=ctx.namespace,
                                    scopes=ctx.scopes, include_unknowns=False, principal_id=ctx.principal_id)
    lines = []
    for row in cases["cases"]:
        lines.append({"statement": f"{row['authority']} case {row['case_number']} ({row['instrument_as_published']}) "
                                   f"names the entity as {row['party']['role_as_published']}",
                      "reached_by": {"basis": "accepted-identity-decision", **row["matched_through"]},
                      "citation": _cite("competition", namespace, row["case_key"], row["cites"]["case_revision_id"],
                                        party_revision_id=row["cites"]["party_revision_id"],
                                        stage_revision_id=row["cites"]["stage_revision_id"], url=row.get("case_url")),
                      "data": {"kind": "case", "authority": row["authority"], "case_number": row["case_number"],
                               "stage_as_of": (row["stage_as_of"] or {}).get("stage_as_published")}})
    for row in awards["awards"]:
        lines.append({"statement": f"state-aid award {row['award_id']} ({row['member_state']}): "
                                   f"{row['amount_as_published'] or row['amount_range_as_published']} "
                                   f"{row['currency'] or ''}".rstrip(),
                      "reached_by": {"basis": "accepted-identity-decision", **row["matched_through"]},
                      "citation": _cite("competition", namespace, row["award_key"], row["revision_id"],
                                        provider=(row.get("source") or {}).get("provider"),
                                        url=(row.get("source") or {}).get("url")),
                      "data": {"kind": "state_aid_award", **{k: row.get(k) for k in (
                          "member_state", "granting_authority", "sa_number", "amount_as_published",
                          "amount_range_as_published", "currency", "award_date", "status")}}})
    # The competition owner's per-currency computed view is not carried: this dossier never totals.
    return {"lines": lines, "unknowns": [u["unknown"] + f" ({u['award_key']})" for u in awards["unknowns"]],
            "notice": cases["notice"]}


def _procurement_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.procurement_identity import ProcurementIdentityService

    namespace = ctx.section_namespace
    service = ProcurementIdentityService(ctx.conn, initialize=False)
    lines, seen = [], set()
    for entity_id in ctx.entity_ids:
        for row in service.award_history(namespace, scopes=ctx.scopes, supplier_entity=entity_id):
            if row["award_key"] in seen:
                continue
            seen.add(row["award_key"])
            value = row.get("value") or {}
            lines.append({"statement": f"{row['provider']} {row['stage']} {row['notice_id']} of {row['date']}: "
                                       f"buyer {row['buyer']['name']}, value "
                                       f"{value.get('amount', 'not stated')} {value.get('currency') or ''}".rstrip(),
                          "reached_by": {"basis": "reviewed-party-link", "entity_id": entity_id},
                          "citation": _cite("procurement", namespace, row["award_key"], row["notice_id"],
                                            provider=row["provider"], url=row.get("source_url")),
                          "data": {k: row.get(k) for k in ("stage", "date", "cpv", "value", "status", "procedure_id")}})
    return {"lines": lines, "notice": "award history of reviewed party links, shown as context; it never shows that "
                                      "a payment was made or that a procedure is open"}


def _sanctions_section(ctx: SectionContext) -> dict[str, Any]:
    from src.kb.sanctions import READ_SCOPE as LEGAL_READ
    from src.kb.sanctions import authorize
    from src.kb.sanctions_queries import SanctionsQueries
    from src.osint.dossier import _designation_line

    namespace = ctx.section_namespace
    authorize(namespace, ctx.scopes, LEGAL_READ)
    accepted, pending = _identity_links(ctx, namespace, ("sanctions:",))
    queries = SanctionsQueries(ctx.conn)
    lines = []
    for link in accepted:
        row = ctx.conn.execute("SELECT designation_id FROM sanctions_designations WHERE namespace=? AND record_key=?",
                               [namespace, link["record_key"]]).fetchone()
        if row is None:
            continue
        statement = queries.statement_as_of(namespace, row[0], ctx.as_of or date.today().isoformat(),
                                            legal_namespace=ctx.request.get("legal_namespace") or namespace)
        line = _designation_line(statement, {"basis": "accepted-identity-decision", "via": link["candidate_id"]},
                                 person=ctx.person)
        citation = line["citation"]
        lines.append({"statement": f"{line['list_id']} entry {line['list_entry_id']}: {line['status']} as of "
                                   f"{line['as_of']}",
                      "reached_by": _reached(link),
                      "citation": _cite("sanctions", namespace, link["record_key"],
                                        citation.get("revision_id") or (citation.get("snapshot") or {}).get(
                                            "snapshot_id", "unknown"), snapshot=citation.get("snapshot")),
                      "data": {k: line.get(k) for k in ("list_id", "list_entry_id", "party_kind", "status", "as_of",
                                                        "programmes", "legal_basis")}})
    return {"lines": lines, "pending": _pending(pending),
            "notice": "what each list states as of the date, lists side by side; not a sanctions determination"}


register_section(SectionSpec("classification", "Structure classification", "src.kb.ownership_structures",
                             _classification_section, needs_namespace=False))
register_section(SectionSpec("ownership", "Ownership up and down", "src.kb.ownership_graph", _ownership_section,
                             needs_namespace=False))
register_section(SectionSpec("sec_holdings", "SEC 13F and Schedule 13D/13G holdings", "src.kb.ownership_holdings",
                             _sec_holdings_section, needs_namespace=False))
register_section(SectionSpec("bafin_holdings", "BaFin voting-rights holdings", "src.domains.market.bafin_queries",
                             _bafin_holdings_section, _gate("src.domains.market.bafin_notices"),
                             feature="market: bafin-notices"))
register_section(SectionSpec("lobbying", "Lobbying declarations", "src.kb.lobbying_queries", _lobbying_section,
                             _gate("src.kb.lobbying"), feature="political: lobbying"))
register_section(SectionSpec("political_donations", "Political donations", "src.kb.campaign_finance_links",
                             _donations_section, _campaign_finance_enabled,
                             feature="political: campaign-finance-us / campaign-finance-uk"))
register_section(SectionSpec("public_payments", "Public money: EU FTS and budget payments",
                             "src.kb.public_finance", _public_payments_section, _gate("src.kb.public_finance"),
                             feature="economics: public-finance"))
register_section(SectionSpec("competition", "Competition cases and state aid", "src.kb.competition_queries",
                             _competition_section, _competition_enabled, feature="corporate-ownership: competition"))
register_section(SectionSpec("procurement", "Procurement awards", "src.kb.procurement_identity",
                             _procurement_section, _gate("src.kb.procurement_bundle", "is_enabled"),
                             feature="public procurement bundle"))
register_section(SectionSpec("sanctions", "Sanctions designations", "src.kb.sanctions_queries", _sanctions_section,
                             _gate("src.kb.sanctions"), feature="legal: sanctions"))
# ICIJ Offshore Leaks (#2888): resolved lazily from src.kb.offshore_leaks_links; ``not_available`` without it.
register_section(SectionSpec("offshore_leaks", "Offshore Leaks (ICIJ)", "src.kb.offshore_leaks_links",
                             OFFSHORE_LEAKS_PROVIDER, feature="offshore leaks (#2888)"))


# ------------------------------------------------------------------ assembly


def _validate_lines(name: str, lines: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    for line in lines:
        citation = line.get("citation") or {}
        if not all(citation.get(k) not in (None, "") for k in ("id", "namespace", "record", "revision")):
            raise OwnershipError("uncited_line", f"section {name} returned a line without a record citation")
        if not line.get("reached_by"):
            raise OwnershipError("uncited_line", f"section {name} returned a line without how it was reached")
    return [dict(line) for line in lines]


def _run(spec: SectionSpec, ctx: SectionContext) -> dict[str, Any]:
    base = {"section": spec.name, "title": spec.title, "owner": spec.owner, "feature": spec.feature,
            "namespace": ctx.section_namespace if spec.needs_namespace else ctx.namespace,
            "lines": [], "citations": [], "pending": [], "unknowns": []}
    provider = _resolve_provider(spec)
    if provider is None:
        return {**base, "status": "not_available",
                "reason": f"no provider for this section is installed ({spec.owner}); not the same as empty"}
    if spec.enabled is not None and not spec.enabled(ctx.conn, ctx.section_namespace):
        return {**base, "status": "inert",
                "reason": f"the {spec.feature or spec.name} feature is not enabled; not the same as empty"}
    if spec.needs_namespace and not ctx.section_namespace:
        return {**base, "status": "not_requested", "reason": "no namespace was given for this section"}
    try:
        result = dict(provider(ctx))
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not code:
            raise
        return {**base, "status": str(code), "reason": str(exc)}
    lines = _validate_lines(spec.name, list(result.pop("lines", [])))
    section = {**base, **result, "lines": lines,
               "citations": sorted({line["citation"]["id"] for line in lines}),
               "status": "assembled" if lines else "none_on_record"}
    if not lines:
        section["note"] = "no linked record on file: not a statement that none exists"
    return section


def build_structure_dossier(conn: Any, namespace: str, scheme: str, value: str, *, principal_id: str | None,
                            scopes: Iterable[str], as_of: str | None = None,
                            sections: Mapping[str, Mapping[str, Any]] | None = None,
                            include: Iterable[str] | None = None, known_at_ms: int | None = None,
                            evidence_kind: str = "unspecified") -> dict[str, Any]:
    """Assemble the structure dossier for one identifier.

    ``sections`` maps a section name to its request, at least ``{"namespace": ...}`` for the sections that read
    another owner's namespace (``legal_namespace`` is also read by ``sanctions``); ``include`` limits which sections
    run (all registered ones by default).
    """
    from src.config.env import person_research_enabled
    from src.kb.ownership_graph import OwnershipGraph

    scopes = set(scopes)
    store = OwnershipStore(conn, initialize=False)
    found = store.lookup(namespace, scheme, value, principal_id=principal_id, scopes=scopes)
    if found["status"] == "not_found":
        raise OwnershipError("not_found", "no acquired entity carries this identifier in this namespace")
    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes, known_at_ms=known_at_ms)
    clusters = sorted({graph.cluster(m["record"]["record_key"]) for m in found["matches"]})
    if found["status"] == "candidates" or len(clusters) > 1:
        return {"contract": CONTRACT, "status": "needs_selection", "query": {"scheme": scheme, "value": value},
                "candidates": [graph.describe(c) for c in clusters],
                "note": "the identifier matches more than one entity; choose one by an exact identifier. Noesis "
                        "never picks by name."}
    root = clusters[0]
    members = graph.members(root)
    bodies = [v["record"] for k in members for v in graph.entities.get(k, []) if not v.get("redacted")]
    person = bool(bodies) and all(b["kind"] == "person" for b in bodies)
    if person and not person_research_enabled():
        raise OwnershipError("person_research_required", "a structure dossier for a natural person is assembled only "
                                                         "in person-research mode (NOESIS_PERSON_RESEARCH)")
    identifiers = sorted(({"scheme": i.get("scheme"), "value": i.get("value")} for b in bodies
                          for i in b.get("identifiers") or []), key=lambda i: (str(i["scheme"]), str(i["value"])))
    entity_ids = sorted({b["canonical_entity_id"] for b in bodies if b.get("canonical_entity_id")} | {
        "lei:" + "".join(ch for ch in str(i["value"]).upper() if ch.isalnum())
        for i in identifiers if i["scheme"] == "lei"})
    requests = {k: dict(v or {}) for k, v in dict(sections or {}).items()}
    unknown_sections = sorted(set(requests) - set(SECTIONS))
    if unknown_sections:
        raise OwnershipError("invalid_request", f"unknown sections: {', '.join(unknown_sections)}")
    wanted = list(SECTIONS) if include is None else [s for s in SECTIONS if s in set(include)]
    assembled = {}
    for name in wanted:
        ctx = SectionContext(conn, namespace, root, members, entity_ids, identifiers, as_of, principal_id, scopes,
                             graph, person, requests.get(name, {}))
        assembled[name] = _run(SECTIONS[name], ctx)
    classification = (assembled.get("classification") or {}).get("classification") or {
        "status": "unclassified", "structure_types": [], "differ": False}
    dossier = {
        "contract": CONTRACT, "status": "assembled", "namespace": namespace,
        "query": {"scheme": scheme, "value": value}, "as_of": as_of, "known_at_ms": known_at_ms,
        "entity": graph.describe(root), "person": person,
        "structure": classification,
        "identity": {"root": root, "members": members, "entity_ids": entity_ids, "accepted": graph.identity},
        "sections": assembled,
        "coverage": {name: s["status"] for name, s in assembled.items()},
        "exclusions": list(EXCLUSIONS), "notice": NOTICE,
        "evidence": {"kind": evidence_kind},
        "pins": {"ownership_records": dict(sorted(graph.pins.items())), "identity": graph.identity},
    }
    leaked = forbidden_keys(dossier)
    if leaked:
        raise OwnershipError("forbidden_output", "a structure dossier never carries " + ", ".join(leaked))
    dossier["dossier_hash"] = digest({k: v for k, v in dossier.items() if k != "dossier_hash"})
    return dossier


# ------------------------------------------------------------------ export


def verify_dossier(dossier: Mapping[str, Any]) -> None:
    if dossier.get("contract") != CONTRACT or dossier.get("status") != "assembled":
        raise OwnershipError("invalid_request", "only an assembled structure dossier can be exported")
    if digest({k: v for k, v in dossier.items() if k != "dossier_hash"}) != dossier.get("dossier_hash"):
        raise OwnershipError("integrity_failed", "the dossier does not match its hash")
    leaked = forbidden_keys(dossier)
    if leaked:
        raise OwnershipError("forbidden_output", "a structure dossier never carries " + ", ".join(leaked))


def export_structure_bundle(conn: Any, dossier: Mapping[str, Any], *, principal_id: str | None,
                            scopes: Iterable[str], created_at_ms: int | None = None) -> dict[str, Any]:
    """The dossier as a ``noesis-evidence-bundle-v1``: root receipt, one claim per section, one evidence per citation.

    Ownership-store citations are re-read at their pinned revision and must still carry the same record hash.
    """
    from src.evidence_bundle import EvidenceBundleBuilder

    verify_dossier(dossier)
    scopes = set(scopes)
    store = OwnershipStore(conn, initialize=False)
    builder = EvidenceBundleBuilder("receipt", {
        "domain": "corporate-ownership", "kind": "structure-dossier", "namespace": dossier["namespace"],
        "query": dossier["query"], "as_of": dossier["as_of"], "dossier_hash": dossier["dossier_hash"]},
        created_at_ms=created_at_ms)
    claims = []
    for name, section in dossier["sections"].items():
        evidence = []
        for line in section["lines"]:
            citation = line["citation"]
            if section["owner"] in {"src.kb.ownership_graph", "src.kb.ownership_holdings"}:
                view = store.get(citation["namespace"], citation["record"], principal_id=principal_id, scopes=scopes,
                                 revision=int(citation["revision"]))
                pinned = dossier["pins"]["ownership_records"].get(citation["record"])
                if pinned is not None and pinned != view["revision"]:
                    raise OwnershipError("integrity_failed", "a cited ownership revision is not the pinned one")
            evidence.append(builder.add_object("evidence", {
                "contract": EVIDENCE_CONTRACT, "section": name, "owner": section["owner"], "citation": citation,
                "reached_by": line["reached_by"],
                "locator": {"document_id": f"{citation['namespace']}:{citation['record']}@{citation['revision']}",
                            "record": citation["record"], "revision": citation["revision"],
                            "namespace": citation["namespace"], "source": citation.get("provider"),
                            "url": citation.get("url"), "cited": True}},
                object_id=f"structure-evidence:{citation['id']}"))
        claims.append(builder.add_object("claim", {
            "citation_state": "cited" if evidence else "uncited",
            "section": name, "title": section["title"], "status": section["status"], "owner": section["owner"],
            "statements": [{"text": line["statement"], "citation": line["citation"]["id"]} for line in section["lines"]],
            "pending": section.get("pending") or [], "unknowns": section.get("unknowns") or [],
            "reason": section.get("reason")}, object_id=f"structure-section:{name}", references=evidence))
        if section["status"] not in LIVE:
            builder.add_omission(f"section {name} is {section['status']}: {section.get('reason') or ''}".strip(),
                                 object_id=f"structure-section:{name}")
    builder.add_object("receipt", dict(dossier), object_id=f"structure-dossier:{dossier['dossier_hash'][:32]}",
                       references=claims, root=True)
    return builder.build()


__all__ = [
    "CONTRACT", "EVIDENCE_CONTRACT", "FORBIDDEN_KEYS", "OFFSHORE_LEAKS_PROVIDER", "READ_SCOPE", "SECTIONS",
    "SectionContext", "SectionSpec", "build_structure_dossier", "export_structure_bundle", "forbidden_keys",
    "register_section", "unregister_section", "verify_dossier",
]

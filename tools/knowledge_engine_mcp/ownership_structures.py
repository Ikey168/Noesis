"""Corporate Ownership holding-structure entry points (#2890): classification and the structure dossier.

Registered through :mod:`tools.knowledge_engine_mcp.ownership` and gated by
the Corporate Ownership bundle's enablement like every ownership tool. A
structure type (family office, private foundation, trust/trustee, holding
company, SPV) is recorded only as a source states it, with a citation, and
classifies an entity only once a reviewer accepts it; names are never read.
The dossier composes the existing record owners section by section, each
cited and reached through accepted identity decisions or exact identifiers;
sections whose feature is off report ``inert``. No influence score, no
wrongdoing verdict and no total across sources.
"""

STRUCTURE_WRITES = {
    "propose_ownership_structure_classification",
    "propose_ownership_structure_classifications_from_records",
    "review_ownership_structure_classification",
    "revert_ownership_structure_classification",
}
STRUCTURE_READS = {
    "list_ownership_structure_classifications",
    "build_ownership_structure_dossier",
    "export_ownership_structure_dossier",
}
STRUCTURE_TOOLS = STRUCTURE_WRITES | STRUCTURE_READS
READ = "knowledge:ownership:read"
WRITE = "knowledge:ownership:write"
REVIEW = "knowledge:ownership:review"
STRUCTURE_SCOPES = {
    "propose_ownership_structure_classification": [WRITE],
    "propose_ownership_structure_classifications_from_records": [WRITE],
    "review_ownership_structure_classification": [REVIEW],
    "revert_ownership_structure_classification": [REVIEW],
    "list_ownership_structure_classifications": [READ],
    "build_ownership_structure_dossier": [READ],
    "export_ownership_structure_dossier": [READ],
}


def register(mcp, safe, context):
    from src.kb.ownership_bundle import require_enabled

    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope=READ):
        def run(conn):
            require_enabled(conn, namespace)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    def service(conn, write=False):
        from src.kb.ownership_structures import StructureClassifications

        return StructureClassifications(conn, initialize=write)

    @mcp.tool()
    def propose_ownership_structure_classification(namespace: str, entity_key: str, structure_type: str, basis: str,
                                                   stated_value: str, citation: dict) -> dict:
        """Propose a holding-structure type for an ownership entity exactly as a source states it.

        structure_type: family_office, private_foundation, trust, trustee, holding_company or
        special_purpose_vehicle. basis: register-legal-form, self-description or declared-category (never a
        name). citation: {"kind": "ownership-record", "record_id", "revision", "field"} where that revision states
        stated_value at that field (e.g. entity_type or entity_type.subtype), or {"kind": "document", "reference",
        "sha256", "locator", "quote", "url"?} for an official document whose quote contains stated_value. Stays
        proposed until a reviewer accepts it.
        """
        return gated(namespace, lambda conn: service(conn, True).propose(
            namespace, entity_key=entity_key, structure_type=structure_type, basis=basis, stated_value=stated_value,
            citation=citation, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def propose_ownership_structure_classifications_from_records(namespace: str) -> dict:
        """Offer proposals where an entity record's stated legal form equals a listed vocabulary entry exactly
        (e.g. a BODS 0.4 arrangement subtype "trust"); no substring or name matching; each needs review."""
        return gated(namespace, lambda conn: service(conn, True).propose_from_records(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def review_ownership_structure_classification(namespace: str, classification_id: str, decision: str,
                                                  reason: str) -> dict:
        """Accept or reject a proposed classification with a reason (knowledge:ownership:review)."""
        return gated(namespace, lambda conn: service(conn).review(
            namespace, classification_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=REVIEW)

    @mcp.tool()
    def revert_ownership_structure_classification(namespace: str, classification_id: str, reason: str) -> dict:
        """Undo an accepted or rejected classification; nothing is deleted and the history stays auditable."""
        return gated(namespace, lambda conn: service(conn).revert(
            namespace, classification_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def list_ownership_structure_classifications(namespace: str, entity_key: str | None = None,
                                                 state: str | None = None) -> dict:
        """Classifications with basis, stated value, citation, state and decision; an entity with no accepted one
        is unclassified."""
        def run(conn):
            items = service(conn).list(namespace, scopes=who()[1], state=state,
                                       entity_keys=[entity_key] if entity_key else None)
            result = {"classifications": items}
            if entity_key:
                result["entity"] = service(conn).classification(namespace, [entity_key], scopes=who()[1])
            return result
        return gated(namespace, run)

    def _dossier(conn, namespace, scheme, value, as_of, sections, include, evidence_kind):
        from src.kb.ownership_structure_dossier import build_structure_dossier

        principal, scopes = who()
        return build_structure_dossier(conn, namespace, scheme, value, principal_id=principal, scopes=scopes,
                                       as_of=as_of, sections=sections, include=include, evidence_kind=evidence_kind)

    @mcp.tool()
    def build_ownership_structure_dossier(namespace: str, scheme: str, value: str, as_of: str | None = None,
                                          sections: dict | None = None, include: list[str] | None = None,
                                          evidence_kind: str = "unspecified") -> dict:
        """One view of a holding structure: its stated classification, ownership up and down, SEC and BaFin
        holdings, lobbying declarations, political donations, EU FTS/budget payments, competition and state aid,
        procurement awards, sanctions designations and an offshore-leaks extension point.

        sections maps a section name to {"namespace": ...} for the sections that read another owner's namespace
        (lobbying, political_donations, public_payments, bafin_holdings, competition, procurement, sanctions,
        offshore_leaks). Each section cites its own records and is reached only through accepted identity decisions
        or exact identifiers; sections whose feature is off are "inert", a missing provider "not_available". Never
        summed across sources; no influence score or wrongdoing verdict.
        """
        return gated(namespace, lambda conn: _dossier(conn, namespace, scheme, value, as_of, sections, include,
                                                      evidence_kind))

    @mcp.tool()
    def export_ownership_structure_dossier(namespace: str, scheme: str, value: str, as_of: str | None = None,
                                           sections: dict | None = None, include: list[str] | None = None,
                                           evidence_kind: str = "unspecified") -> dict:
        """The structure dossier as a verifiable noesis-evidence-bundle-v1: the dossier as root receipt, one claim per
        section, one evidence object per cited record revision; sections not assembled are listed as omissions."""
        from src.kb.ownership_structure_dossier import export_structure_bundle

        def run(conn):
            dossier = _dossier(conn, namespace, scheme, value, as_of, sections, include, evidence_kind)
            if dossier.get("status") != "assembled":
                return dossier
            return export_structure_bundle(conn, dossier, principal_id=who()[0], scopes=who()[1])
        return gated(namespace, run)

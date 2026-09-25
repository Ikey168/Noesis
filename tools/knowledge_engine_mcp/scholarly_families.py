"""Paper-family navigation, review, lifecycle and exact citation tools."""

from src.domains.research.paper_families import PaperFamilyStore, READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE


PAPER_FAMILY_WRITES = {
    "create_paper_family", "add_paper_family_member", "correct_paper_family_relation",
    "review_paper_family_relation",
    "remove_paper_family_member", "attach_paper_family_notice", "review_paper_family_notice_target",
    "select_paper_family_citation",
}


def register(mcp, safe, context):
    def call(method, namespace, *args, write=False, review=False, **kwargs):
        principal_id, scopes = context()
        return safe(
            lambda conn: getattr(PaperFamilyStore(conn, initialize=write), method)(
                namespace, *args, principal_id=principal_id, scopes=scopes, **kwargs
            ),
            write=write, required_scope=REVIEW_SCOPE if review else WRITE_SCOPE if write else READ_SCOPE,
        )

    @mcp.tool()
    def create_paper_family(namespace: str, family_key: str, root_member: dict) -> dict:
        """Create a versioned scholarly family from an exact accessible document revision."""
        return call("create", namespace, family_key, root_member, write=True)

    @mcp.tool()
    def inspect_paper_family(namespace: str, family_id: str, revision: int | None = None,
                             limit: int = 50, offset: int = 0) -> dict:
        """Inspect a bounded family page with exact source versions and access types."""
        return call("inspect", namespace, family_id, revision=revision, limit=limit, offset=offset)

    @mcp.tool()
    def add_paper_family_member(namespace: str, family_id: str, command_key: str, member: dict,
                                source_member_id: str, relation_type: str, provenance: dict,
                                expected_revision: int) -> dict:
        """Add an explicit provider-linked member or a reviewable inferred candidate."""
        return call("add_member", namespace, family_id, command_key, member,
                    source_member_id=source_member_id, relation_type=relation_type,
                    provenance=provenance, expected_revision=expected_revision, write=True)

    @mcp.tool()
    def correct_paper_family_relation(namespace: str, family_id: str, command_key: str,
                                      relation_id: str, relation_type: str, provenance: dict,
                                      rationale: str, expected_revision: int) -> dict:
        """Supersede a mistaken relation with a corrected provider link or review candidate."""
        return call("correct_relation", namespace, family_id, command_key, relation_id,
                    relation_type, provenance, rationale, expected_revision=expected_revision, write=True)

    @mcp.tool()
    def review_paper_family_relation(namespace: str, family_id: str, command_key: str,
                                     relation_id: str, decision: str, rationale: str,
                                     expected_revision: int) -> dict:
        """Record an independent reviewer decision on a candidate family link."""
        return call("review_relation", namespace, family_id, command_key, relation_id,
                    decision, rationale, expected_revision=expected_revision, write=True, review=True)

    @mcp.tool()
    def remove_paper_family_member(namespace: str, family_id: str, command_key: str,
                                   member_id: str, rationale: str, expected_revision: int) -> dict:
        """Reverse a family assignment while retaining earlier versions and citations."""
        return call("remove_member", namespace, family_id, command_key, member_id,
                    rationale, expected_revision=expected_revision, write=True)

    @mcp.tool()
    def attach_paper_family_notice(namespace: str, family_id: str, command_key: str,
                                   notice_id: str, expected_revision: int) -> dict:
        """Attach a retained Crossref notice to its evidenced member or mark target unresolved."""
        return call("attach_notice", namespace, family_id, command_key, notice_id,
                    expected_revision=expected_revision, write=True)

    @mcp.tool()
    def review_paper_family_notice_target(namespace: str, family_id: str, command_key: str,
                                          notice_id: str, member_id: str, rationale: str,
                                          expected_revision: int) -> dict:
        """Record an independent review of an unresolved notice target."""
        return call("review_notice_target", namespace, family_id, command_key, notice_id,
                    member_id, rationale, expected_revision=expected_revision, write=True, review=True)

    @mcp.tool()
    def select_paper_family_citation(namespace: str, family_id: str, command_key: str,
                                     member_id: str, expected_revision: int,
                                     locator: dict | None = None) -> dict:
        """Pin one exact accessible member and locator for a new citation selection."""
        return call("select_citation", namespace, family_id, command_key, member_id,
                    expected_revision=expected_revision, locator=locator, write=True)

    @mcp.tool()
    def compare_paper_family_members(namespace: str, family_id: str, member_ids: list[str]) -> dict:
        """Compare two to ten selected family versions and their member-level notices."""
        return call("compare", namespace, family_id, member_ids)

    @mcp.tool()
    def export_paper_family(namespace: str, family_id: str) -> dict:
        """Export selected bibliography entries and version-specific lifecycle history."""
        return call("export", namespace, family_id)

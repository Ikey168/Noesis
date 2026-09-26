"""Mathematics in Research: zbMATH Open literature, OEIS sequences and pinned formal-library declarations.

Acquisition runs through the research-discovery source pack (``zbmath-open``,
``oeis-sequences``, ``mathlib4-fib``, ``afp-zeckendorf``). Formal queries name
an immutable commit; links are explicit (from a source) or reviewable
candidates, never an automatic identity between a paper and a formal theorem.
"""

MATH_WRITES = {"propose_math_links", "review_math_link", "record_math_object", "extract_math_text_objects"}
MATH_TOOLS = MATH_WRITES | {"math_source_contracts", "search_mathematics", "inspect_math_literature",
                            "inspect_oeis_sequence", "inspect_formal_declaration", "formal_declaration_dependencies",
                            "compare_formal_snapshots", "export_formal_references", "math_links",
                            "inspect_math_object"}
MATH_SCOPES = {"math_source_contracts": [], "review_math_link": ["knowledge:mathematics:review"]}


def required_scopes(tool_name, mutability):
    return MATH_SCOPES.get(
        tool_name, ["knowledge:mathematics:write" if mutability == "write" else "knowledge:mathematics:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.mathematics import MathStore

        return MathStore(conn)

    read = "knowledge:mathematics:read"

    @mcp.tool()
    def math_source_contracts() -> dict:
        """zbMATH Open, OEIS and formal-library access, terms and identifiers; reused and deferred providers."""
        from src.ingestion.math_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def search_mathematics(query: str, kinds: list[str] | None = None, limit: int = 20) -> dict:
        """Search papers, OEIS sequences (A-number, name or comma-separated terms) and formal declarations."""
        return safe(lambda conn: store(conn).search(query, scopes=who()[1], kinds=kinds, limit=limit),
                    required_scope=read)

    @mcp.tool()
    def inspect_math_literature(provider: str, provider_id: str) -> dict:
        """One literature record with its observed revisions and explicit/candidate links."""
        return safe(lambda conn: store(conn).literature(provider, provider_id, scopes=who()[1]), required_scope=read)

    @mcp.tool()
    def inspect_oeis_sequence(a_number: str) -> dict:
        """One OEIS entry (terms, references, revision) with its links."""
        return safe(lambda conn: store(conn).sequence(a_number, scopes=who()[1]), required_scope=read)

    @mcp.tool()
    def inspect_formal_declaration(library: str, name: str, commit: str) -> dict:
        """A formal declaration at an exact commit: statement span, permalink, dependencies and links."""
        return safe(lambda conn: store(conn).declaration(library, name, commit, scopes=who()[1]), required_scope=read)

    @mcp.tool()
    def formal_declaration_dependencies(library: str, commit: str, name: str) -> dict:
        """Explicit module imports and lexical statement references for a declaration or module at a commit."""
        return safe(lambda conn: store(conn).dependencies(library, commit, name, scopes=who()[1]),
                    required_scope=read)

    @mcp.tool()
    def compare_formal_snapshots(library: str, from_commit: str, to_commit: str) -> dict:
        """Declarations added, removed or changed between two commits, rename candidates and import changes."""
        return safe(lambda conn: store(conn).compare_snapshots(library, from_commit, to_commit, scopes=who()[1]),
                    required_scope=read)

    @mcp.tool()
    def export_formal_references(library: str, commit: str, names: list[str]) -> dict:
        """A reproducible, hashed bundle of declaration references pinned to one commit."""
        return safe(lambda conn: store(conn).export_references(library, commit, names, scopes=who()[1]),
                    required_scope=read)

    @mcp.tool()
    def math_links(kind: str, subject_id: str, include_rejected: bool = False) -> dict:
        """Links of a literature record, sequence, declaration or object with basis, evidence and review state."""
        return safe(lambda conn: store(conn).links(kind, subject_id, scopes=who()[1],
                                                   include_rejected=include_rejected), required_scope=read)

    @mcp.tool()
    def inspect_math_object(object_id: str) -> dict:
        """A source-backed mathematical object (exact span, method, confidence) and its notation aliases."""
        return safe(lambda conn: store(conn).object(object_id, scopes=who()[1]), required_scope=read)

    @mcp.tool()
    def propose_math_links() -> dict:
        """Record explicit links from source citations/identifiers and bounded reviewable candidates."""
        return safe(lambda conn: store(conn).propose_links(scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:mathematics:write")

    @mcp.tool()
    def review_math_link(link_id: str, decision: str, note: str = "") -> dict:
        """Accept or reject a candidate link; explicit links are not reviewable."""
        return safe(lambda conn: store(conn).review_link(link_id, decision, scopes=who()[1], principal_id=who()[0],
                                                         note=note),
                    write=True, required_scope="knowledge:mathematics:review")

    @mcp.tool()
    def record_math_object(kind: str, source_kind: str, source_ref: dict, exact_text: str, method: str,
                           confidence: float | None = None, alias_of: str | None = None,
                           notation: str | None = None) -> dict:
        """Record an expression, definition, theorem, proof or notation alias with its exact source span."""
        return safe(lambda conn: store(conn).record_object(kind, source_kind, source_ref, exact_text, method=method,
                                                           confidence=confidence, scopes=who()[1],
                                                           principal_id=who()[0], alias_of=alias_of,
                                                           notation=notation),
                    write=True, required_scope="knowledge:mathematics:write")

    @mcp.tool()
    def extract_math_text_objects(provider: str, provider_id: str) -> dict:
        """Extract candidate expressions and named results (with exact spans) from a literature record's text."""
        return safe(lambda conn: store(conn).extract_text_objects(provider, provider_id, scopes=who()[1],
                                                                  principal_id=who()[0]),
                    write=True, required_scope="knowledge:mathematics:write")

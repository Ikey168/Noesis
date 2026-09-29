"""Legal federal-statutes feature entry points: provisions as of a date, comparisons, amendment acts, citations.

Acquisition runs through the shared source-pack tools (pack ``legal-research``:
``gii-federal-statutes``, ``ris-federal-statute-versions``,
``bgbl-federal-promulgations``). Every answer says which kind of evidence it
rests on: a source-stated version carries the validity interval the source
states; an observed version is the text seen on a date with no validity
stated. No tool states that a provision is in force, applies amending
instructions, or gives legal advice.
"""

FEDERAL_WRITES = {
    "link_amendment_dossiers",
    "create_statute_monitor",
    "run_statute_monitor",
}
FEDERAL_READS = {
    "get_federal_provision",
    "compare_provision_versions",
    "list_amendment_acts",
    "decisions_citing_provision",
    "resolve_statutory_citation",
    "list_federal_statute_versions",
    "poll_statute_monitor",
}
FEDERAL_TOOLS = FEDERAL_WRITES | FEDERAL_READS
FEDERAL_SCOPES = {
    # Linking reads Bundestag DIP dossiers and writes Legal dossier links.
    "link_amendment_dossiers": [
        "knowledge:legal:write",
        "knowledge:political:dossier:read",
    ],
    "create_statute_monitor": ["knowledge:legal:read", "knowledge:subscriptions:write"],
    # Running reads the monitor (subscriptions:read) and records its evaluation (subscriptions:write).
    "run_statute_monitor": [
        "knowledge:legal:read",
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    "poll_statute_monitor": ["knowledge:legal:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return FEDERAL_SCOPES.get(
        tool_name,
        ["knowledge:legal:write" if mutability == "write" else "knowledge:legal:read"],
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def federal(conn, *, initialize=False):
        from src.kb.legal import LegalStore
        from src.kb.legal_federal import FederalStatutes

        return FederalStatutes(LegalStore(conn, initialize=initialize))

    def checked(tool_name, operation):
        """Run with every scope the tool always uses present (not only the first)."""

        def run(conn):
            from src.kb.legal import LegalError

            missing = [
                s for s in FEDERAL_SCOPES.get(tool_name, []) if s not in who()[1]
            ]
            if missing and "operator" not in who()[1]:
                raise LegalError("unauthorized", f"{', '.join(missing)} required")
            return operation(conn)

        return run

    @mcp.tool()
    def get_federal_provision(
        namespace: str, statute: str, provision: str, as_of: str
    ) -> dict:
        """A federal statute provision's cited text as of a date, labelled by its evidence.

        `provision` is a path (`§5/abs2`) or a citation (`§ 5 Abs. 2`). Returns the source-stated version whose stated
        validity covers the date; otherwise the nearest observed version on or before it, labelled "observed on
        <date>, validity not stated"; otherwise "no version on record". Differing sources for the date are returned
        side by side as a conflict. Never states that the provision is in force.
        """
        return safe(
            lambda conn: federal(conn).provision_as_of(
                namespace, statute, provision, as_of, scopes=who()[1]
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def compare_provision_versions(
        namespace: str, statute: str, provision: str, left: str, right: str
    ) -> dict:
        """Provision-level diff between two versions (version ids) or two dates, with the amendment acts the later
        version itself names. Source changes only, not a legal-effect assessment."""
        return safe(
            lambda conn: federal(conn).compare_provision(
                namespace, statute, provision, left, right, scopes=who()[1]
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def list_amendment_acts(
        namespace: str, statute: str | None = None, provision: str | None = None
    ) -> dict:
        """Acquired Federal Law Gazette amendment acts (optionally touching one statute or provision): BGBl citation,
        promulgation date, entry-into-force text, instructions with locators, DIP dossier and EU implementation
        links. Instructions are never applied."""
        return safe(
            lambda conn: federal(conn).list_amendment_acts(
                namespace, scopes=who()[1], statute=statute, provision=provision
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def decisions_citing_provision(
        namespace: str,
        statute: str,
        provision: str | None = None,
        court: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """Federal court decisions that explicitly cite a provision (or any provision of the statute), with the
        decision locator, the provision version selected for the decision date and its evidence basis."""
        return safe(
            lambda conn: federal(conn).decisions_citing(
                namespace,
                statute,
                provision,
                scopes=who()[1],
                court=court,
                date_from=date_from,
                date_to=date_to,
            ),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def resolve_statutory_citation(namespace: str, citation: str) -> dict:
        """Parse a German statutory citation into statute, provision paths, offsets and a.F./n.F. hints. Unknown
        abbreviations stay unresolved; nothing is guessed."""
        return safe(
            lambda conn: federal(conn).resolve(namespace, citation, scopes=who()[1]),
            required_scope="knowledge:legal:read",
        )

    @mcp.tool()
    def list_federal_statute_versions(namespace: str, statute: str) -> dict:
        """Every version of a statute with its evidence basis: stated validity (source-stated) or sightings
        (observed), the source's 'Stand' notes and stated amendment references."""

        def run(conn):
            from src.kb.legal import READ_SCOPE, _authorize

            _authorize(namespace, who()[1], READ_SCOPE, write=False)
            store = federal(conn)
            store._require_ready()
            work = store.statute_work(namespace, statute)
            from src.kb.legal_federal import SEMANTICS

            return {
                **work,
                "versions": store.versions(namespace, work["work_id"]),
                "semantics": SEMANTICS,
            }

        return safe(run, required_scope="knowledge:legal:read")

    @mcp.tool()
    def link_amendment_dossiers(
        namespace: str, dossier_namespace: str | None = None
    ) -> dict:
        """Link acquired amendment acts to Bundestag DIP dossiers whose stages state the act's BGBl citation (exact
        citation match only); unmatched acts stay unlinked with the reason."""
        return safe(
            checked(
                "link_amendment_dossiers",
                lambda conn: federal(conn).link_amendment_dossiers(
                    namespace,
                    scopes=who()[1],
                    principal_id=who()[0],
                    dossier_namespace=dossier_namespace,
                ),
            ),
            write=True,
            required_scope="knowledge:legal:write",
        )

    @mcp.tool()
    def create_statute_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        statute: str | None = None,
        provision: str | None = None,
        act: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a statute, a provision or an amendment act (BGBl citation); no new scheduler."""
        from src.kb.legal_statute_monitoring import StatuteMonitor

        return safe(
            checked(
                "create_statute_monitor",
                lambda conn: StatuteMonitor(conn).create(
                    namespace,
                    request_key,
                    watch=watch,
                    statute=statute,
                    provision=provision,
                    act=act,
                    principal_id=who()[0],
                    scopes=who()[1],
                    delivery=delivery,
                ),
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def run_statute_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a statute monitor at a committed watermark: new amendment acts, changed observed text, new or
        corrected source-stated versions and newly citing decisions, each cited with a provision-level diff."""
        from src.kb.legal_statute_monitoring import StatuteMonitor

        return safe(
            checked(
                "run_statute_monitor",
                lambda conn: StatuteMonitor(conn).run(
                    subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
                ),
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_statute_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a statute monitor's events through the subscription delivery path."""
        from src.kb.legal_statute_monitoring import StatuteMonitor

        return safe(
            checked(
                "poll_statute_monitor",
                lambda conn: StatuteMonitor(conn, initialize=False).poll(
                    subscription_id,
                    principal_id=who()[0],
                    scopes=who()[1],
                    cursor=cursor,
                ),
            ),
            required_scope="knowledge:subscriptions:read",
        )

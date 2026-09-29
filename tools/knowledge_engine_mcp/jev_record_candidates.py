"""Record-backed Jev methodology and identity candidate tools."""

import os


def register(mcp, safe, context):
    def advisor(conn, *, initialize=True):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.jev_record_candidates import JevRecordCandidateAdvisor, RecordInputResolver

        runtime = DecisionRuntime(
            conn, client=TypeSafeClient(), initialize=initialize,
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None),
            input_resolver=RecordInputResolver(conn),
        )
        return JevRecordCandidateAdvisor(conn, runtime)

    def suggest(method, namespace, run_id, policy, max_cost_usd_micros,
                network_approved, max_attempts, deadline_s, *args):
        principal_id, scopes = context()
        return safe(
            lambda conn: getattr(advisor(conn), method)(
                namespace, *args, run_id, principal_id=principal_id, scopes=scopes,
                allow_remote=network_approved, policy=policy,
                max_attempts=max_attempts, max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s),
            write=True, required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def suggest_jev_methodology_record(namespace: str, statement_id: str, run_id: str,
                                       policy: dict, max_cost_usd_micros: int,
                                       network_approved: bool = False, max_attempts: int = 1,
                                       deadline_s: float = 30) -> dict:
        """Categorize an exact methodology statement and source passage as advice."""
        return suggest("suggest_methodology", namespace, run_id, policy, max_cost_usd_micros,
                       network_approved, max_attempts, deadline_s, statement_id)

    @mcp.tool()
    def suggest_jev_entity_identity(namespace: str, reference_id: str,
                                    candidate_ids: list[str], run_id: str, policy: dict,
                                    max_cost_usd_micros: int, network_approved: bool = False,
                                    max_attempts: int = 1, deadline_s: float = 30) -> dict:
        """Assess one to ten registered entity candidates without merging them."""
        return suggest("suggest_entity_identity", namespace, run_id, policy,
                       max_cost_usd_micros, network_approved, max_attempts,
                       deadline_s, reference_id, candidate_ids)

    @mcp.tool()
    def suggest_jev_source_identity(namespace: str, reference_id: str,
                                    candidate_ids: list[str], run_id: str, policy: dict,
                                    max_cost_usd_micros: int, network_approved: bool = False,
                                    max_attempts: int = 1, deadline_s: float = 30) -> dict:
        """Assess current source-identity candidates without inferring ownership."""
        return suggest("suggest_source_identity", namespace, run_id, policy,
                       max_cost_usd_micros, network_approved, max_attempts,
                       deadline_s, reference_id, candidate_ids)

    @mcp.tool()
    def accept_jev_source_alias_review(namespace: str, run_id: str,
                                       accepted_candidate_id: str, reason: str) -> dict:
        """Record a human-confirmed alias through source-identity review."""
        principal_id, scopes = context()
        return safe(
            lambda conn: advisor(conn).accept_source_alias(
                namespace, run_id, accepted_candidate_id, reason,
                principal_id=principal_id, scopes=scopes),
            write=True, required_scope="knowledge:source-identity:review",
        )

    @mcp.tool()
    def queue_jev_entity_merge_review(namespace: str, run_id: str,
                                      accepted_candidate_id: str, reason: str) -> dict:
        """Queue a human-confirmed merge proposal for existing admin review."""
        principal_id, scopes = context()
        return safe(
            lambda conn: advisor(conn).queue_entity_merge_review(
                namespace, run_id, accepted_candidate_id, reason,
                principal_id=principal_id, scopes=scopes),
            write=True, required_scope="knowledge:entity-history:review",
        )

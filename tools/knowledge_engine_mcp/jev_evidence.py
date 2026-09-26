"""Public source-bound suggestions for retrieval and evidence checks."""

from __future__ import annotations

import os


def register(mcp, safe, context):
    def runtime(conn):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime

        return DecisionRuntime(
            conn,
            client=TypeSafeClient(),
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None
            ),
        )

    def suggestion(
        adapter,
        namespace,
        run_id,
        *args,
        policy,
        max_cost_usd_micros,
        network_approved,
        deadline_s,
        **kwargs,
    ):
        principal_id, scopes = context()
        return safe(
            lambda conn: adapter(
                runtime(conn),
                namespace,
                run_id,
                *args,
                principal_id=principal_id,
                scopes=scopes,
                allow_remote=network_approved,
                policy=policy,
                max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s,
                **kwargs,
            ),
            write=True,
            required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def suggest_jev_shortlist_rerank(
        namespace: str,
        run_id: str,
        query: str,
        candidates: list[dict],
        policy: dict,
        max_cost_usd_micros: int,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Rerank an exact-source shortlist while retaining all candidates on failure."""
        from src.kb.jev_evidence_adapters import suggest_shortlist_rerank

        return suggestion(
            suggest_shortlist_rerank,
            namespace,
            run_id,
            query,
            candidates,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_answer_support(
        namespace: str,
        run_id: str,
        statement: str,
        citation: dict,
        policy: dict,
        max_cost_usd_micros: int,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Assess a statement against one exact cited passage without adding sources."""
        from src.kb.jev_evidence_adapters import suggest_answer_support

        return suggestion(
            suggest_answer_support,
            namespace,
            run_id,
            statement,
            citation,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_claim_relation(
        namespace: str,
        run_id: str,
        claim_a: dict,
        claim_b: dict,
        similarity: float,
        policy: dict,
        max_cost_usd_micros: int,
        duplicate_threshold: float = 0.88,
        window_relations: list[str] | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest a bounded pair relation without writing claim graph edges."""
        from src.kb.jev_evidence_adapters import suggest_claim_relation

        return suggestion(
            suggest_claim_relation,
            namespace,
            run_id,
            claim_a,
            claim_b,
            similarity=similarity,
            duplicate_threshold=duplicate_threshold,
            window_relations=window_relations,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

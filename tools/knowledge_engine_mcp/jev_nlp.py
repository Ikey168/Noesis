"""Public, source-bound Jev suggestions for local NLP workflows."""

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

    def suggestion(adapter, namespace, run_id, *args, policy, max_cost_usd_micros,
                   network_approved, deadline_s, **kwargs):
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
    def suggest_jev_claim_presence(
        namespace: str,
        run_id: str,
        source_ref: dict,
        sentence_index: int,
        policy: dict,
        max_cost_usd_micros: int,
        calibration_policy: dict | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest claim presence for one captured sentence boundary."""
        from src.argument_mining.jev_nlp_adapters import suggest_claim_presence

        return suggestion(
            suggest_claim_presence,
            namespace,
            run_id,
            source_ref,
            sentence_index=sentence_index,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
            calibration_policy=calibration_policy,
        )

    @mcp.tool()
    def suggest_jev_checkworthiness(
        namespace: str,
        run_id: str,
        source_ref: dict,
        locator: dict,
        claim_id: str,
        topic: str,
        policy: dict,
        max_cost_usd_micros: int,
        priority_policy_id: str = "checkworthiness-impact-60-testability-40-v1",
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Rank an existing factual claim without changing claim or truth state."""
        from src.argument_mining.jev_nlp_adapters import suggest_checkworthiness

        return suggestion(
            suggest_checkworthiness,
            namespace,
            run_id,
            source_ref,
            locator,
            claim_id=claim_id,
            topic=topic,
            priority_policy_id=priority_policy_id,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_sentiment(
        namespace: str,
        run_id: str,
        source_ref: dict,
        locator: dict,
        target: str,
        policy: dict,
        max_cost_usd_micros: int,
        calibration_policy: dict | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest target sentiment without changing local trend aggregates."""
        from src.argument_mining.jev_nlp_adapters import suggest_sentiment

        return suggestion(
            suggest_sentiment,
            namespace,
            run_id,
            source_ref,
            locator,
            target=target,
            calibration_policy=calibration_policy,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_attribution(
        namespace: str,
        run_id: str,
        source_ref: dict,
        statement_locator: dict,
        candidates: list[dict],
        policy: dict,
        max_cost_usd_micros: int,
        calibration_policy: dict | None = None,
        authors: list[str] | None = None,
        byline: str | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest attribution only among extracted candidates, none or uncertain."""
        from src.argument_mining.jev_nlp_adapters import suggest_attribution

        return suggestion(
            suggest_attribution,
            namespace,
            run_id,
            source_ref,
            statement_locator,
            candidates,
            calibration_policy=calibration_policy,
            authors=authors if authors is not None else (),
            byline=byline,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_stance(
        namespace: str,
        run_id: str,
        source_ref: dict,
        topic: str,
        sentence_index: int,
        policy: dict,
        max_cost_usd_micros: int,
        calibration_policy: dict | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest stance for an exact sentence, topic and nearby source context."""
        from src.argument_mining.jev_adapters import suggest_stance

        return suggestion(
            suggest_stance,
            namespace,
            run_id,
            source_ref,
            topic=topic,
            sentence_index=sentence_index,
            calibration_policy=calibration_policy,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

    @mcp.tool()
    def suggest_jev_frames(
        namespace: str,
        run_id: str,
        source_ref: dict,
        document_kind: str,
        policy: dict,
        max_cost_usd_micros: int,
        calibration_policy: dict | None = None,
        network_approved: bool = False,
        deadline_s: float = 30,
    ) -> dict:
        """Suggest multilabel editorial frames with complete source coverage."""
        from src.argument_mining.jev_adapters import suggest_frames

        return suggestion(
            suggest_frames,
            namespace,
            run_id,
            source_ref,
            document_kind=document_kind,
            calibration_policy=calibration_policy,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            network_approved=network_approved,
            deadline_s=deadline_s,
        )

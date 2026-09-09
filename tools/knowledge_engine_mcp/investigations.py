"""Public investigation workflow tools using the server's authenticated context."""

from src.kb.citation_alerts import CitationAlertStore
from src.kb.investigation_comparisons import InvestigationComparisonStore
from src.kb.investigation_templates import InvestigationTemplateStore

TEMPLATE_WRITES = {
    "create_investigation_template",
    "revise_investigation_template",
    "archive_investigation_template",
    "instantiate_investigation_template",
}
TEMPLATE_READS = {
    "inspect_investigation_template",
    "list_investigation_templates",
    "preview_investigation_template",
}
ALERT_WRITES = {
    "subscribe_cited_evidence",
    "set_cited_evidence_subscription_status",
    "evaluate_cited_evidence_subscription",
    "acknowledge_cited_evidence_alert",
}
ALERT_READS = {"inspect_cited_evidence_subscription", "poll_cited_evidence_alerts"}
COMPARISON_WRITES = {"create_investigation_comparison"}
COMPARISON_READS = {
    "inspect_investigation_comparison",
    "export_investigation_comparison",
}


def register(mcp, safe, context):
    def call(store, method, *args, write=False, family="projects", **kwargs):
        required = f"knowledge:{family}:{'write' if write else 'read'}"
        return safe(
            lambda c: getattr(store(c, initialize=write), method)(
                *args, **kwargs, principal_id=context()[0], scopes=context()[1]
            ),
            write=write,
            required_scope=required,
        )

    @mcp.tool()
    def create_investigation_template(
        namespace: str, request_key: str, definition: dict
    ) -> dict:
        """Create a versioned, scoped template for recurring investigations."""
        return call(
            InvestigationTemplateStore,
            "create",
            namespace,
            request_key,
            definition,
            write=True,
        )

    @mcp.tool()
    def inspect_investigation_template(
        namespace: str, template_id: str, revision: int | None = None
    ) -> dict:
        """Read an exact template revision under current access rules."""
        return call(
            InvestigationTemplateStore,
            "inspect",
            namespace,
            template_id,
            revision=revision,
        )

    @mcp.tool()
    def list_investigation_templates(namespace: str, limit: int = 50) -> dict:
        """List accessible investigation templates in a namespace."""
        return call(InvestigationTemplateStore, "list", namespace, limit=limit)

    @mcp.tool()
    def revise_investigation_template(
        namespace: str, template_id: str, expected_revision: int, definition: dict
    ) -> dict:
        """Append a template revision without changing existing projects."""
        return call(
            InvestigationTemplateStore,
            "revise",
            namespace,
            template_id,
            expected_revision,
            definition=definition,
            write=True,
        )

    @mcp.tool()
    def archive_investigation_template(
        namespace: str, template_id: str, expected_revision: int
    ) -> dict:
        """Archive a template and prevent new instantiations."""
        return call(
            InvestigationTemplateStore,
            "revise",
            namespace,
            template_id,
            expected_revision,
            archive=True,
            write=True,
        )

    @mcp.tool()
    def preview_investigation_template(
        namespace: str, template_id: str, revision: int, parameters: dict
    ) -> dict:
        """Preview questions, report outline and pinned source readiness without acquisition."""
        import os

        return call(
            InvestigationTemplateStore,
            "preview",
            namespace,
            template_id,
            revision,
            parameters,
            secret_available=lambda name: bool(os.environ.get(name)),
        )

    @mcp.tool()
    def instantiate_investigation_template(
        namespace: str,
        template_id: str,
        revision: int,
        parameters: dict,
        request_key: str,
        budget: dict,
    ) -> dict:
        """Create an independent project from a pinned template and caller budget."""
        return call(
            InvestigationTemplateStore,
            "instantiate",
            namespace,
            template_id,
            revision,
            parameters,
            request_key,
            budget,
            write=True,
        )

    @mcp.tool()
    def subscribe_cited_evidence(
        target: dict, request_key: str, categories: list[str], batch_size: int = 50
    ) -> dict:
        """Subscribe to a pinned project/report's citation changes using the local inbox."""
        return call(
            CitationAlertStore,
            "create",
            target,
            request_key,
            categories,
            batch_size,
            write=True,
            family="subscriptions",
        )

    @mcp.tool()
    def inspect_cited_evidence_subscription(subscription_id: str) -> dict:
        """Inspect citation monitoring settings and recheck target access."""
        return call(
            CitationAlertStore, "inspect", subscription_id, family="subscriptions"
        )

    @mcp.tool()
    def set_cited_evidence_subscription_status(
        subscription_id: str, status: str
    ) -> dict:
        """Pause, resume (active), or unsubscribe (deleted) from citation monitoring."""
        return call(
            CitationAlertStore,
            "set_status",
            subscription_id,
            status,
            write=True,
            family="subscriptions",
        )

    @mcp.tool()
    def evaluate_cited_evidence_subscription(subscription_id: str) -> dict:
        """Evaluate committed evidence changes and append deduplicated local alerts."""
        return call(
            CitationAlertStore,
            "evaluate",
            subscription_id,
            write=True,
            family="subscriptions",
        )

    @mcp.tool()
    def poll_cited_evidence_alerts(subscription_id: str, cursor: str = "") -> dict:
        """Read a bounded alert batch with durable cursor, impact links and acknowledgments."""
        return call(
            CitationAlertStore,
            "poll",
            subscription_id,
            cursor=cursor,
            family="subscriptions",
        )

    @mcp.tool()
    def acknowledge_cited_evidence_alert(subscription_id: str, event_id: str) -> dict:
        """Acknowledge an alert without approving evidence or modifying reports."""
        return call(
            CitationAlertStore,
            "acknowledge",
            subscription_id,
            event_id,
            write=True,
            family="subscriptions",
        )

    @mcp.tool()
    def create_investigation_comparison(
        namespace: str, request_key: str, left: dict, right: dict
    ) -> dict:
        """Persist an evidence-linked comparison of two pinned completed project runs."""
        return call(
            InvestigationComparisonStore,
            "create",
            namespace,
            request_key,
            left,
            right,
            write=True,
        )

    @mcp.tool()
    def inspect_investigation_comparison(namespace: str, comparison_id: str) -> dict:
        """Reopen an immutable comparison and report current historical availability."""
        return call(InvestigationComparisonStore, "inspect", namespace, comparison_id)

    @mcp.tool()
    def export_investigation_comparison(namespace: str, comparison_id: str) -> dict:
        """Export an authorized structured comparison and deterministic report Markdown."""
        return call(InvestigationComparisonStore, "export", namespace, comparison_id)

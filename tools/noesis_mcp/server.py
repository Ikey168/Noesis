"""Default Noesis MCP gateway.

This is the small, supported agent-facing surface. Specialist MCP servers remain
available for advanced workflows, but clients should connect here first.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from src.mcp_host.transport import run_server

GATEWAY_CONTRACT = "noesis-gateway-v1"
mcp = FastMCP("noesis")


def _error(code: str, message: str, repair: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if repair:
        payload["repair"] = repair
    return {"contract": GATEWAY_CONTRACT, "error": payload}


def _run(operation):
    from src.gateway import GatewayError
    from src.kb.contract import KBContractError
    from src.noesis_cli.config import ConfigError

    try:
        return operation()
    except GatewayError as exc:
        return _error(exc.code, str(exc), exc.repair)
    except KBContractError as exc:
        return _error(exc.code, str(exc))
    except ConfigError as exc:
        return _error("configuration_error", str(exc), "noesis init")
    except Exception as exc:  # noqa: BLE001 - MCP boundary
        code = getattr(exc, "code", None)
        return _error(str(code or "internal"), str(exc))


def _runtime():
    from src.noesis_cli.config import load_config, open_warehouse

    config = load_config()
    return config, open_warehouse(config)


def _domain(config, requested: str | None) -> str:
    from src.gateway import resolve_domain

    return resolve_domain(config, requested)


@mcp.tool()
def domains() -> dict[str, Any]:
    """List configured knowledge domains and their backing types."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            return contract.kb_domains(conn=conn, config_path=config.domains)
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def add(
    source: str,
    domain: str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Add one local file or HTTP(S) URL and process it through extract/index."""

    def operation():
        from src.gateway import add_source
        from src.noesis_cli.config import load_config

        config = load_config()
        return {
            "contract": GATEWAY_CONTRACT,
            "data": add_source(
                config,
                source,
                domain=domain,
                language=language,
            ),
        }

    return _run(operation)


@mcp.tool()
def search(
    query: str,
    domain: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search the selected/default domain and return cited document rows."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            return contract.kb_search(
                selected,
                query,
                limit,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def ask(
    question: str,
    domain: str | None = None,
    limit: int = 5,
    minimum_relevance: float = 0.34,
) -> dict[str, Any]:
    """Answer from Noesis evidence with statement-level citations."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            return contract.kb_answer(
                selected,
                question,
                limit,
                minimum_relevance,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def brief(
    domains: list[str] | None = None,
    since: str | None = None,
    budget: int = 15,
) -> dict[str, Any]:
    """Build a cited change brief across the requested knowledge domains."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = domains or [_domain(config, None)]
            return contract.kb_brief(
                selected,
                since,
                budget,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def documents(
    domain: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List domain documents, newest ingestion first."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            return contract.kb_documents(
                selected,
                since,
                limit,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def claims(
    domain: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List clustered claims with citations and contradiction metadata."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            return contract.kb_claims(
                selected,
                since,
                limit,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def inspect_source(
    document_id: str,
    domain: str | None = None,
) -> dict[str, Any]:
    """Inspect one visible document together with its integrity evidence."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            rows = contract.kb_documents(
                selected,
                None,
                100_000,
                conn=conn,
                config_path=config.domains,
            )
            document = next(
                (
                    item
                    for item in rows.get("data", [])
                    if item.get("document_id") == document_id
                ),
                None,
            )
            if document is None:
                from src.gateway import GatewayError

                raise GatewayError(
                    "not_found",
                    f"document {document_id!r} is not visible in {selected!r}",
                )
            integrity = contract.kb_integrity(
                selected,
                document_id,
                conn=conn,
                config_path=config.domains,
            )
            return {
                "contract": GATEWAY_CONTRACT,
                "domain": selected,
                "data": {
                    "document": document,
                    "integrity": integrity.get("data"),
                },
            }
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def coverage(domain: str | None = None) -> dict[str, Any]:
    """Report source, freshness, backing, and corpus coverage for a domain."""

    def operation():
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            return contract.kb_coverage(
                selected,
                conn=conn,
                config_path=config.domains,
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def watch(
    action: str = "list",
    domain: str | None = None,
    watch_id: str | None = None,
    selector_type: str | None = None,
    selector_value: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    event_types: list[str] | None = None,
    stale_after_ms: int = 86_400_000,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create/list/poll/pause/resume/delete a durable evidence watch."""

    def operation():
        from src.gateway import GatewayError
        from src.kb import contract

        config, conn = _runtime()
        try:
            selected = _domain(config, domain) if domain else None
            normalized = action.strip().lower()
            if normalized == "list":
                return contract.watch_list(
                    config.principal,
                    selected,
                    conn=conn,
                    config_path=config.domains,
                )
            if normalized == "create":
                if not selector_type or not selector_value:
                    raise GatewayError(
                        "bad_request",
                        "watch create requires selector_type and selector_value",
                    )
                return contract.watch_create(
                    _domain(config, domain),
                    config.principal,
                    {"type": selector_type, "value": selector_value},
                    event_types,
                    stale_after_ms,
                    conn=conn,
                    config_path=config.domains,
                )
            if not watch_id:
                raise GatewayError(
                    "bad_request",
                    f"watch {normalized} requires watch_id",
                )
            if normalized == "poll":
                return contract.watch_poll(
                    watch_id,
                    config.principal,
                    cursor,
                    limit,
                    event_types,
                    conn=conn,
                )
            if normalized == "pause":
                return contract.watch_pause(watch_id, config.principal, conn=conn)
            if normalized == "resume":
                return contract.watch_resume(watch_id, config.principal, conn=conn)
            if normalized == "delete":
                return contract.watch_delete(
                    watch_id,
                    config.principal,
                    confirm,
                    conn=conn,
                )
            raise GatewayError(
                "bad_request",
                "watch action must be create, list, poll, pause, resume, or delete",
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def inbox(
    action: str = "list",
    domain: str | None = None,
    only_unprocessed: bool = True,
    limit: int = 50,
    item_id: str | None = None,
    request_key: str | None = None,
    intent: str = "Review new information",
    url: str | None = None,
    name: str | None = None,
    source_kind: str = "rss_atom",
    session_id: str | None = None,
    expected_revision: int | None = None,
    decision: str | None = None,
    command_key: str | None = None,
) -> dict[str, Any]:
    """Review, refresh, subscribe, or triage the owner-scoped Awareness inbox."""

    def operation():
        from src.gateway import GatewayError
        from src.kb.intake_inbox import IntakeInboxStore

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            scopes = {"operator"}
            store = IntakeInboxStore(conn)
            normalized = action.strip().lower()
            if normalized == "list":
                return store.list(
                    selected,
                    principal_id=config.principal,
                    scopes=scopes,
                    only_unprocessed=only_unprocessed,
                    limit=limit,
                )
            if normalized == "subscriptions":
                return store.subscriptions(
                    selected,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "refresh":
                return store.refresh(
                    selected,
                    principal_id=config.principal,
                    scopes=scopes,
                    limit_per_feed=min(limit, 100),
                )
            if normalized == "subscribe":
                if not url or not name:
                    raise GatewayError(
                        "bad_request", "inbox subscribe requires url and name"
                    )
                return store.subscribe(
                    selected,
                    url,
                    name,
                    source_kind,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "inspect":
                if not item_id:
                    raise GatewayError("bad_request", "inbox inspect requires item_id")
                return store.inspect(
                    selected,
                    item_id,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "start":
                if not request_key:
                    raise GatewayError(
                        "bad_request", "inbox start requires request_key"
                    )
                return store.start_awareness(
                    selected,
                    request_key,
                    intent=intent,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "triage":
                if (
                    not session_id
                    or not item_id
                    or not decision
                    or not command_key
                    or expected_revision is None
                ):
                    raise GatewayError(
                        "bad_request",
                        "inbox triage requires session_id, item_id, decision, "
                        "command_key, and expected_revision",
                    )
                return store.triage_awareness(
                    selected,
                    session_id,
                    item_id,
                    command_key,
                    expected_revision=expected_revision,
                    decision=decision,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            raise GatewayError(
                "bad_request",
                "inbox action must be list, subscriptions, subscribe, refresh, "
                "inspect, start, or triage",
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def explore(
    action: str = "list",
    domain: str | None = None,
    session_id: str | None = None,
    request_key: str | None = None,
    intent: str = "Explore related evidence",
    url: str | None = None,
    title: str | None = None,
    note: str = "",
    content: str | None = None,
    fetch_readable: bool = False,
    saved: bool = False,
    expected_revision: int | None = None,
    command_key: str | None = None,
    suggestion_id: str | None = None,
    decision: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Start or continue an Exploration trail and inspect related-source suggestions."""

    def operation():
        from src.gateway import GatewayError
        from src.kb.intake_exploration import IntakeExplorationStore
        from src.kb.intake_modes import IntakeStore

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            scopes = {"operator"}
            sessions = IntakeStore(conn)
            store = IntakeExplorationStore(conn)
            normalized = action.strip().lower()
            if normalized == "list":
                return sessions.list(
                    selected,
                    principal_id=config.principal,
                    scopes=scopes,
                    mode="Exploration",
                    limit=min(limit, 100),
                )
            if normalized == "start":
                if not request_key:
                    raise GatewayError(
                        "bad_request", "explore start requires request_key"
                    )
                return sessions.create(
                    selected,
                    "Exploration",
                    request_key,
                    intent=intent,
                    inputs={},
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "inspect":
                if not session_id:
                    raise GatewayError(
                        "bad_request", "explore inspect requires session_id"
                    )
                return sessions.inspect(
                    selected,
                    session_id,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "capture":
                if (
                    not session_id
                    or not command_key
                    or expected_revision is None
                    or not url
                ):
                    raise GatewayError(
                        "bad_request",
                        "explore capture requires session_id, command_key, "
                        "expected_revision, and url",
                    )
                return store.capture(
                    selected,
                    session_id,
                    command_key,
                    expected_revision=expected_revision,
                    url=url,
                    title=title or url,
                    note=note,
                    saved=saved,
                    content=content,
                    fetch_readable=fetch_readable,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "suggest":
                if not session_id:
                    raise GatewayError(
                        "bad_request", "explore suggest requires session_id"
                    )
                return store.related_sources(
                    selected,
                    session_id,
                    principal_id=config.principal,
                    scopes=scopes,
                    limit=limit,
                )
            if normalized in {"follow", "dismiss"}:
                if (
                    not session_id
                    or not suggestion_id
                    or not command_key
                    or expected_revision is None
                ):
                    raise GatewayError(
                        "bad_request",
                        "explore follow/dismiss requires session_id, suggestion_id, "
                        "command_key, and expected_revision",
                    )
                return store.decide_related_source(
                    selected,
                    session_id,
                    suggestion_id,
                    command_key,
                    expected_revision=expected_revision,
                    decision=normalized,
                    saved=saved,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            raise GatewayError(
                "bad_request",
                "explore action must be list, start, inspect, capture, suggest, "
                "follow, or dismiss",
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def research(
    action: str = "list",
    question: str | None = None,
    domain: str | None = None,
    request_key: str | None = None,
    session_id: str | None = None,
    expected_revision: int | None = None,
    command_key: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Manage bounded Deep Research workflow sessions over Noesis evidence."""

    def operation():
        from src.gateway import GatewayError
        from src.kb.intake_modes import IntakeStore

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            scopes = {"operator"}
            store = IntakeStore(conn)
            normalized = action.strip().lower()
            if normalized == "list":
                return store.list(
                    selected,
                    principal_id=config.principal,
                    scopes=scopes,
                    mode="Deep Research",
                    limit=min(limit, 100),
                )
            if normalized == "start":
                if not question or not request_key:
                    raise GatewayError(
                        "bad_request",
                        "research start requires question and request_key",
                    )
                return store.create(
                    selected,
                    "Deep Research",
                    request_key,
                    intent=question,
                    inputs={"question": question},
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized == "inspect":
                if not session_id:
                    raise GatewayError(
                        "bad_request", "research inspect requires session_id"
                    )
                return store.inspect(
                    selected,
                    session_id,
                    principal_id=config.principal,
                    scopes=scopes,
                )
            if normalized in {"pause", "resume", "complete", "cancel"}:
                if (
                    not session_id
                    or not command_key
                    or expected_revision is None
                ):
                    raise GatewayError(
                        "bad_request",
                        "research lifecycle actions require session_id, command_key, "
                        "and expected_revision",
                    )
                return store.command(
                    selected,
                    session_id,
                    command_key,
                    expected_revision=expected_revision,
                    action=normalized,
                    payload={},
                    principal_id=config.principal,
                    scopes=scopes,
                )
            raise GatewayError(
                "bad_request",
                "research action must be list, start, inspect, pause, resume, "
                "complete, or cancel",
            )
        finally:
            conn.close()

    return _run(operation)


@mcp.tool()
def export(
    kind: str,
    domain: str | None = None,
    question: str | None = None,
    claim_id: str | None = None,
    document_id: str | None = None,
    include_private: bool = False,
) -> dict[str, Any]:
    """Return an answer, claim, or integrity Evidence Bundle inline."""

    def operation():
        from src.evidence_bundle import export_answer, export_claim, export_integrity
        from src.gateway import GatewayError
        from src.kb import contract
        from src.kb.registry import load_registry

        config, conn = _runtime()
        try:
            selected = _domain(config, domain)
            definition = load_registry(config.domains).get(selected)
            private = "private" in {tag.casefold() for tag in definition.tags}
            if private and not include_private:
                raise GatewayError(
                    "private_evidence_excluded",
                    f"domain {selected!r} is private; set include_private=true explicitly",
                )
            normalized = kind.strip().lower()
            if normalized == "answer":
                if not question:
                    raise GatewayError("bad_request", "answer export requires question")
                source = contract.kb_answer(
                    selected,
                    question,
                    conn=conn,
                    config_path=config.domains,
                )
                return export_answer(
                    source,
                    inputs={"domain": selected, "question": question},
                    include_private=include_private,
                )
            if normalized == "claim":
                if not claim_id:
                    raise GatewayError("bad_request", "claim export requires claim_id")
                visible = contract.kb_claims(
                    selected,
                    limit=100_000,
                    conn=conn,
                    config_path=config.domains,
                )
                claim_ids = {
                    row.get("claim_id")
                    for cluster in visible.get("data", [])
                    for row in cluster.get("citations", [])
                }
                if claim_id not in claim_ids:
                    raise GatewayError("not_found", f"claim {claim_id!r} is not visible")
                return export_claim(
                    conn,
                    claim_id,
                    visibility="private" if private else "public",
                    include_private=include_private,
                )
            if normalized == "integrity":
                if not document_id:
                    raise GatewayError(
                        "bad_request",
                        "integrity export requires document_id",
                    )
                contract.kb_integrity(
                    selected,
                    document_id,
                    conn=conn,
                    config_path=config.domains,
                )
                return export_integrity(
                    conn,
                    document_id,
                    visibility="private" if private else "public",
                    include_private=include_private,
                )
            raise GatewayError(
                "bad_request",
                "export kind must be answer, claim, or integrity",
            )
        finally:
            conn.close()

    return _run(operation)


if __name__ == "__main__":
    run_server(mcp)

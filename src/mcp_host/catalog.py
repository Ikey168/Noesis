"""Generated, authorization-aware catalog of registered Noesis MCP capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.mcp_host.config import (
    DEFAULT_MCP_JSON,
    LEGACY_SERVER_ALIASES,
    REPO_ROOT,
    _is_project_server,
)

CATALOG_CONTRACT = "noesis-mcp-catalog-v1"
CATALOG_VERSION = 1
CATALOG_ARTIFACT = REPO_ROOT / "contracts/generated/noesis-mcp-catalog-v1.json"
DOMAIN_CONFIG = REPO_ROOT / "config/domains.yml"
PACK_CONFIG = REPO_ROOT / "config/domain_packs.json"

STATES = frozenset(
    {"available", "degraded", "empty", "disabled", "unauthorized", "unavailable"}
)
DEFAULT_SCOPES = frozenset({"public", "knowledge:read"})
SENSITIVE_SERVERS = frozenset({"lineage_mcp", "provisioning_mcp", "security_mcp"})
MUTATION_PREFIXES = (
    "attach_",
    "commit_",
    "compute_",
    "create_",
    "declare_",
    "delete_",
    "deprecate_",
    "define_",
    "disable_",
    "enable_",
    "execute_",
    "harvest_",
    "ingest_",
    "register_",
    "reverse_",
    "rollback_",
    "run_",
    "set_",
    "subscribe_",
    "trigger_",
    "unsubscribe_",
    "update_",
)
MUTATION_NAMES = frozenset(
    {
        "kg_attach_pipeline",
        "kg_attach_sources",
        "kg_deploy",
        "kg_ingest",
        "kg_teardown",
        "watch_create",
        "watch_delete",
        "watch_pause",
        "watch_resume",
        "watch_scan",
        "resolve_event_report",
        "accept_source_pack_license",
        "cancel_source_pack_run",
        "cancel_maintenance_job",
        "install_source_pack",
        "retry_source_pack_quarantine",
        "retry_maintenance_job",
        "pause_maintenance_schedule",
        "resume_maintenance_schedule",
        "recover_stale_maintenance_jobs",
        "begin_research_snapshot",
        "renew_research_snapshot",
        "close_research_snapshot",
        "assess_epistemic_statement",
        "review_epistemic_status",
        "register_epistemic_taxonomy",
        "revise_hypothesis_workspace",
        "branch_hypothesis_workspace",
        "retire_hypothesis_workspace",
        "link_hypothesis_evidence",
        "retract_hypothesis_evidence",
    }
)


class CatalogError(ValueError):
    """A catalog registration or filtering request is malformed."""


def _read_registration(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read MCP registration {path}: {exc}") from exc
    if not isinstance(payload.get("mcpServers"), dict):
        raise CatalogError("MCP registration must contain an mcpServers object")
    return payload


def _server_path(entry: Mapping[str, Any], root: Path) -> Path | None:
    if not _is_project_server(dict(entry)):
        return None
    args = entry.get("args") or ()
    candidate = root / str(args[0])
    # Registrations copied for tests or deployments may live elsewhere while
    # retaining repository-relative entry points.
    return candidate if candidate.exists() else REPO_ROOT / str(args[0])


def _module_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    return f"noesis_catalog_{path.parent.name}_{digest}"


async def _inspect_server(
    path: Path,
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    if not path.exists():
        return None, [], "registered server entry point does not exist"
    try:
        spec = importlib.util.spec_from_file_location(_module_name(path), path)
        if spec is None or spec.loader is None:
            return None, [], "server module cannot be loaded"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mcp = getattr(module, "mcp", None)
        if mcp is None:
            return None, [], "server module does not export mcp"
        discovered = await mcp.get_tools()
        tools = []
        for name, tool in sorted(discovered.items()):
            tools.append(
                {
                    "name": str(name),
                    "description": str(tool.description or "").strip(),
                    "input_schema": dict(tool.parameters or {"type": "object"}),
                    "output_schema": dict(
                        tool.output_schema
                        or {"type": "object", "additionalProperties": True}
                    ),
                }
            )
        return str(getattr(mcp, "name", "") or ""), tools, None
    except Exception as exc:  # noqa: BLE001 - isolate optional MCP server imports
        return None, [], f"{type(exc).__name__}: {exc}"[:300]


def _mutability(name: str) -> str:
    if name in MUTATION_NAMES or name.startswith(MUTATION_PREFIXES):
        return "write"
    return "read"


def _cost(name: str, mutability: str) -> tuple[str, str]:
    expensive = (
        "semantic",
        "forecast",
        "cluster",
        "centrality",
        "communities",
        "ingest",
        "run_stage",
        "trigger",
    )
    if any(token in name for token in expensive):
        return "high", "batch"
    if mutability == "write" or any(
        token in name for token in ("search", "query", "graph")
    ):
        return "medium", "interactive"
    return "low", "interactive"


def _required_data(server_stem: str, tool_name: str) -> list[str]:
    if server_stem == "catalog_mcp":
        return ["mcp-registration", "domain-registry"]
    if server_stem == "transactions_mcp":
        return ["knowledge-transaction-store"]
    if server_stem == "schema_registry_mcp":
        return ["knowledge-schema-registry"]
    if server_stem == "federation_mcp":
        return ["federated-knowledge-sources"]
    if server_stem == "subscriptions_mcp":
        return ["knowledge-subscription-store"]
    if server_stem == "namespaces_mcp":
        return ["portable-namespace-store"]
    if server_stem == "memory_mcp":
        return ["knowledge-memory-store"]
    if server_stem == "knowledge_engine_mcp":
        return ["knowledge-engine-runtime"]
    if server_stem == "contract_mcp":
        return ["contract-schemas"]
    if server_stem == "schema_mcp":
        return ["application-schema"]
    if server_stem == "domain_packs_mcp":
        return ["pack-registry"]
    if server_stem == "lineage_mcp":
        return ["lineage-backend"]
    if server_stem == "monitoring_mcp":
        return ["metrics-store"]
    if server_stem == "security_mcp":
        return ["local-security-state"]
    if server_stem == "blog_mcp":
        return ["subscription-store"]
    if server_stem == "dataset_mcp":
        return ["argument-dataset"]
    if server_stem == "provisioning_mcp":
        return ["namespace-registry"]
    if server_stem == "kb_mcp" and tool_name == "kb_domains":
        return ["domain-registry"]
    return ["warehouse"]


def _required_scopes(server_stem: str, mutability: str, tool_name: str) -> list[str]:
    if server_stem == "transactions_mcp":
        if tool_name.startswith("commit_"):
            return ["knowledge:transaction:commit"]
        if tool_name.startswith("rollback_"):
            return ["knowledge:transaction:rollback"]
        if tool_name.startswith("preview_"):
            return ["knowledge:transaction:preview"]
        return ["knowledge:transaction:read"]
    if server_stem == "schema_registry_mcp":
        if tool_name.startswith(("register_", "declare_")):
            return ["knowledge:schema:register"]
        if tool_name.startswith("deprecate_"):
            return ["knowledge:schema:deprecate"]
        if tool_name.startswith(("define_", "execute_", "rollback_")):
            return ["knowledge:schema:migrate"]
        if tool_name.startswith("validate_"):
            return ["knowledge:schema:validate"]
        return ["knowledge:schema:read"]
    if server_stem == "federation_mcp":
        return ["knowledge:federation:read"]
    if server_stem == "subscriptions_mcp":
        if tool_name.startswith(("create_", "update_", "pause_", "resume_", "delete_")):
            return ["knowledge:subscriptions:write"]
        if tool_name.startswith("pending_"):
            return ["knowledge:subscriptions:deliver"]
        return ["knowledge:subscriptions:read"]
    if server_stem == "namespaces_mcp":
        if tool_name.startswith(("import_", "preview_")):
            return ["knowledge:namespace:import"]
        return ["knowledge:namespace:export"]
    if server_stem == "memory_mcp":
        if tool_name.startswith(
            ("remember_", "correct_", "forget_", "consolidate_", "import_")
        ):
            return ["knowledge:memory:write"]
        if tool_name.startswith(("set_", "apply_")):
            return ["knowledge:memory:admin"]
        return ["knowledge:memory:read"]
    if server_stem == "knowledge_engine_mcp":
        if tool_name == "execute_hypothesis_research_plan":
            return ["knowledge:hypothesis:execute"]
        if tool_name in {
            "create_hypothesis_workspace",
            "revise_hypothesis_workspace",
            "branch_hypothesis_workspace",
            "retire_hypothesis_workspace",
            "link_hypothesis_evidence",
            "retract_hypothesis_evidence",
            "create_hypothesis_research_plan",
        }:
            return ["knowledge:hypothesis:write"]
        if tool_name in {
            "get_hypothesis_workspace",
            "compare_hypotheses",
            "get_hypothesis_research_plan",
            "export_hypothesis_workspace",
            "replay_hypothesis_workspace",
        }:
            return ["knowledge:hypothesis:read"]
        if tool_name in {
            "assess_epistemic_statement",
            "register_epistemic_taxonomy",
        }:
            return ["knowledge:epistemic:write"]
        if tool_name == "review_epistemic_status":
            return ["knowledge:epistemic:review"]
        if tool_name in {
            "classify_epistemic_statement",
            "get_epistemic_assessment",
            "search_epistemic_assessments",
            "explain_epistemic_assessment",
            "list_epistemic_taxonomies",
        }:
            return ["knowledge:epistemic:read"]
        if tool_name in {
            "begin_research_snapshot",
            "renew_research_snapshot",
            "close_research_snapshot",
        }:
            return ["knowledge:snapshot:write"]
        if tool_name in {
            "inspect_research_snapshot",
            "research_snapshot_pins",
            "research_snapshot_health",
        }:
            return ["knowledge:snapshot:read"]
        if tool_name in {
            "set_maintenance_schedule_paused",
            "cancel_maintenance_job",
            "retry_maintenance_job",
            "pause_maintenance_schedule",
            "resume_maintenance_schedule",
            "recover_stale_maintenance_jobs",
        }:
            return ["knowledge:maintenance:admin"]
        return ["operator"] if mutability == "write" else ["knowledge:read"]
    if mutability == "write" or server_stem in SENSITIVE_SERVERS:
        return ["operator"]
    if server_stem in {"catalog_mcp", "contract_mcp", "schema_mcp"}:
        return ["public"]
    return ["knowledge:read"]


def _enabled_packs(path: Path, override: Iterable[str] | None) -> set[str]:
    if override is not None:
        return {str(item).strip() for item in override if str(item).strip()}
    try:
        configured = json.loads(path.read_text()).get("enabled_packs") or ()
    except (OSError, ValueError, AttributeError):
        configured = ("news",)
    from src.config.env import enabled_packs

    env_value = enabled_packs().strip()
    if env_value:
        configured = [item.strip() for item in env_value.split(",") if item.strip()]
    return {str(item) for item in configured}


def _server_pack(server_stem: str) -> str | None:
    return "research" if server_stem == "research_mcp" else None


def _table_exists(conn: Any, table: str) -> bool:
    if conn is None:
        return False
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
            ).fetchone()
        )
    except Exception:  # noqa: BLE001 - readiness probes are best effort
        return False


def _warehouse_rows(conn: Any) -> int | None:
    if conn is None:
        return None
    for table in ("documents", "news_articles"):
        if _table_exists(conn, table):
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:  # noqa: BLE001 - readiness probes are best effort
                return None
    return None


def _data_state(required: list[str], conn: Any) -> tuple[str, str | None]:
    if not set(required) & {
        "argument-dataset",
        "metrics-store",
        "namespace-registry",
        "subscription-store",
        "warehouse",
    }:
        return "available", None
    count = _warehouse_rows(conn)
    if count is None:
        return "degraded", "data readiness was not probed"
    if count == 0:
        return "empty", "configured data store contains no documents"
    return "available", None


def _state(
    *,
    authorized: bool,
    pack_enabled: bool,
    import_error: str | None,
    host_state: str | None,
    data_state: str,
    backend_ready: bool,
) -> tuple[str, str | None]:
    if not authorized:
        return "unauthorized", "required scope is not granted"
    if not pack_enabled:
        return "disabled", "required domain pack is disabled"
    if import_error or host_state == "down":
        return "unavailable", import_error or "server health is down"
    if data_state == "empty":
        return "empty", "required data is empty"
    if (
        not backend_ready
        or host_state in {"connecting", "degraded"}
        or data_state == "degraded"
    ):
        return "degraded", "backend or data readiness is incomplete"
    return "available", None


def _private_grant(conn: Any, principal_id: str | None, domain: str) -> bool:
    if (
        conn is None
        or not principal_id
        or not _table_exists(conn, "claim_watch_domain_grants")
    ):
        return False
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM claim_watch_domain_grants "
                "WHERE principal_id=? AND domain=?",
                [str(principal_id), domain],
            ).fetchone()
        )
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _domain_state(definition: Any, conn: Any) -> tuple[str, int | None]:
    if conn is None:
        return "degraded", None
    if definition.backing == "corpus-view":
        if not _table_exists(conn, "document_domains"):
            return "unavailable", None
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM document_domains WHERE domain=?",
                [definition.name],
            ).fetchone()[0]
        )
        return ("available" if count else "empty"), count
    table = f"kg_{definition.namespace}_documents"
    if not _table_exists(conn, table):
        return "unavailable", None
    quoted_table = '"' + table.replace('"', '""') + '"'
    count = int(conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0])
    return ("available" if count else "empty"), count


def _domains(
    config_path: Path,
    conn: Any,
    principal_id: str | None,
    include_private: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    from src.kb.registry import KnowledgeDomainRegistry

    registry = KnowledgeDomainRegistry.from_config(config_path)
    domains = []
    namespaces = []
    hidden = 0
    for definition in registry.domains():
        private = "private" in {str(tag).casefold() for tag in definition.tags}
        authorized = not private or (
            include_private and _private_grant(conn, principal_id, definition.name)
        )
        if not authorized:
            hidden += 1
            continue
        state, count = _domain_state(definition, conn)
        domains.append(
            {
                "name": definition.name,
                "backing": definition.backing,
                "visibility": "private" if private else "public",
                "state": state,
                "document_count": count,
            }
        )
        if definition.backing == "namespace":
            namespaces.append(
                {
                    "name": definition.namespace,
                    "domain": definition.name,
                    "backend": definition.namespace_backend,
                    "visibility": "private" if private else "public",
                    "state": state,
                }
            )
    return domains, namespaces, hidden


def _host_state(host_status: Mapping[str, Any] | None, server: str) -> str | None:
    if not host_status:
        return None
    entry = (host_status.get("servers") or {}).get(server) or {}
    return str(entry.get("state")) if entry.get("state") else None


async def build_catalog(
    *,
    mcp_path: str | Path = DEFAULT_MCP_JSON,
    domain_config: str | Path = DOMAIN_CONFIG,
    pack_config: str | Path = PACK_CONFIG,
    conn: Any = None,
    principal_id: str | None = None,
    include_private: bool = False,
    granted_scopes: Iterable[str] | None = None,
    enabled_pack_names: Iterable[str] | None = None,
    configured_backends: Iterable[str] | None = None,
    host_status: Mapping[str, Any] | None = None,
    include_unusable: bool = True,
) -> dict[str, Any]:
    """Build from real registration and tool discovery, then apply policy."""

    registration_path = Path(mcp_path)
    raw = _read_registration(registration_path)
    root = registration_path.parent
    scopes = set(DEFAULT_SCOPES if granted_scopes is None else granted_scopes)
    packs = _enabled_packs(Path(pack_config), enabled_pack_names)
    aliases = {
        **LEGACY_SERVER_ALIASES,
        **{
            str(alias): str(target)
            for alias, target in (raw.get("compatibilityAliases") or {}).items()
        },
    }
    servers = []
    visible_tools = []
    omitted = Counter()
    conformance_errors = []
    registered_project_paths = set()

    for registered_name, entry in raw["mcpServers"].items():
        canonical = aliases.get(str(registered_name), str(registered_name))
        path = _server_path(entry, root) if isinstance(entry, Mapping) else None
        if path is None:
            servers.append(
                {
                    "name": canonical,
                    "kind": "external",
                    "version": "unmanaged",
                    "transports": [str(entry.get("type", "stdio"))]
                    if isinstance(entry, Mapping)
                    else [],
                    "state": "unavailable",
                    "reason": "external server tools are not shipped by Noesis",
                    "tool_count": 0,
                    "tools": [],
                }
            )
            continue
        registered_project_paths.add(path.resolve())
        runtime_name, discovered, import_error = await _inspect_server(path)
        stem = path.parent.name
        if not canonical.startswith("noesis-"):
            conformance_errors.append(f"non-canonical registration: {canonical}")
        if runtime_name and runtime_name != canonical:
            conformance_errors.append(
                f"runtime name mismatch for {canonical}: {runtime_name}"
            )
        pack = _server_pack(stem)
        pack_enabled = pack is None or pack in packs
        if configured_backends is None:
            backend_ready = not (
                stem == "lineage_mcp" and not os.getenv("MARQUEZ_URL", "").strip()
            )
        else:
            backend_ready = stem != "lineage_mcp" or "lineage" in set(
                configured_backends
            )
        host_state = _host_state(host_status, canonical)
        server_tools = []
        server_states = []
        for tool in discovered:
            mutability = _mutability(tool["name"])
            required_scopes = _required_scopes(stem, mutability, tool["name"])
            required_data = _required_data(stem, tool["name"])
            data_state, data_reason = _data_state(required_data, conn)
            state, reason = _state(
                authorized=set(required_scopes) <= scopes,
                pack_enabled=pack_enabled,
                import_error=import_error,
                host_state=host_state,
                data_state=data_state,
                backend_ready=backend_ready,
            )
            reason = reason or data_reason
            cost, latency = _cost(tool["name"], mutability)
            capability = {
                "id": f"{canonical}.{tool['name']}",
                "server": canonical,
                "name": tool["name"],
                "version": "1",
                "description": tool["description"],
                "mutability": mutability,
                "input_schema": tool["input_schema"],
                "output_schema": tool["output_schema"],
                "required_scopes": required_scopes,
                "cost_class": cost,
                "latency_class": latency,
                "required_data": required_data,
                "state": state,
                "reason": reason,
            }
            server_states.append(state)
            if include_unusable or state == "available":
                server_tools.append(capability["id"])
                visible_tools.append(capability)
            else:
                omitted[state] += 1
        if import_error:
            server_state = "unavailable"
        elif server_states and all(item == "unauthorized" for item in server_states):
            server_state = "unauthorized"
        elif server_states and all(item == "disabled" for item in server_states):
            server_state = "disabled"
        elif "available" in server_states:
            server_state = "available"
        elif "degraded" in server_states:
            server_state = "degraded"
        elif "empty" in server_states:
            server_state = "empty"
        else:
            server_state = "unavailable"
        servers.append(
            {
                "name": canonical,
                "kind": "noesis",
                "version": "1",
                "aliases": sorted(
                    alias for alias, target in aliases.items() if target == canonical
                ),
                "transports": ["stdio", "streamable-http"],
                "pack": pack,
                "state": server_state,
                "reason": import_error,
                "tool_count": len(discovered),
                "visible_tool_count": len(server_tools),
                "tools": server_tools,
            }
        )

    shipped_paths = {
        path.resolve() for path in (REPO_ROOT / "tools").glob("*_mcp/server.py")
    }
    missing_registrations = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in shipped_paths - registered_project_paths
    )
    stale_registrations = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in registered_project_paths - shipped_paths
    )
    conformance_errors.extend(
        f"unregistered shipped server: {path}" for path in missing_registrations
    )
    conformance_errors.extend(
        f"stale server registration: {path}" for path in stale_registrations
    )
    for alias, target in aliases.items():
        if target not in {server["name"] for server in servers}:
            conformance_errors.append(f"alias {alias} targets missing server {target}")

    domains, namespaces, hidden_private = _domains(
        Path(domain_config), conn, principal_id, include_private
    )
    if hidden_private:
        omitted["unauthorized"] += hidden_private
    domain_pack_names = sorted(
        {path.parent.name for path in (REPO_ROOT / "src/domains").glob("*/__init__.py")}
        | packs
    )
    return {
        "catalog_contract": CATALOG_CONTRACT,
        "catalog_version": CATALOG_VERSION,
        "source": ".mcp.json + registered FastMCP tool discovery",
        "servers": servers,
        "tools": visible_tools,
        "domains": domains,
        "domain_packs": [
            {"name": name, "state": "available" if name in packs else "disabled"}
            for name in domain_pack_names
        ],
        "namespaces": namespaces,
        "transports": [
            {"name": "stdio", "state": "available"},
            {
                "name": "streamable-http",
                "state": "available",
                "configuration": [
                    "NOESIS_MCP_TRANSPORT",
                    "NOESIS_MCP_HTTP_HOST",
                    "NOESIS_MCP_HTTP_PORT",
                    "NOESIS_MCP_AUTH_TOKEN",
                ],
            },
        ],
        "summary": {
            "servers": len(servers),
            "tools": len(visible_tools),
            "states": dict(
                sorted(Counter(tool["state"] for tool in visible_tools).items())
            ),
            "omitted": dict(sorted(omitted.items())),
            "hidden_private_domains": hidden_private,
        },
        "conformance": {
            "passed": not conformance_errors,
            "errors": sorted(set(conformance_errors)),
            "missing_registrations": missing_registrations,
            "stale_registrations": stale_registrations,
        },
    }


def build_catalog_sync(**kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point for scripts and non-async Python clients."""

    return asyncio.run(build_catalog(**kwargs))


__all__ = [
    "CATALOG_ARTIFACT",
    "CATALOG_CONTRACT",
    "CATALOG_VERSION",
    "STATES",
    "CatalogError",
    "build_catalog",
    "build_catalog_sync",
]

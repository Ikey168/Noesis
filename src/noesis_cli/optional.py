"""Explicit operator CLI for optional native integrations.

All database operations use the configured Noesis principal and trusted local
NOESIS_OPTIONAL_SCOPES. Request JSON cannot grant itself permissions. Secrets
are read only from provider-specific environment slots and never printed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from src.noesis_cli.config import ConfigError, load_config, open_warehouse

KEYS = {
    "github": ("NOESIS_GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"),
    "openalex-content": ("NOESIS_OPENALEX_API_KEY", "OPENALEX_API_KEY"),
    "firecrawl": ("NOESIS_FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY"),
    "zyte": ("NOESIS_ZYTE_API_KEY", "ZYTE_API_KEY"),
    "exa": ("NOESIS_EXA_API_KEY", "EXA_API_KEY"),
    "tavily": ("NOESIS_TAVILY_API_KEY", "TAVILY_API_KEY"),
    "jina": ("NOESIS_JINA_API_KEY", "JINA_API_KEY"),
    "opencorporates": ("NOESIS_OPENCORPORATES_API_KEY", "OPENCORPORATES_API_KEY"),
    "opensanctions": ("NOESIS_OPENSANCTIONS_API_KEY", "OPENSANCTIONS_API_KEY"),
}
KINDS = (
    "benchmark",
    "analysis",
    "analysis_read",
    "entity_review",
    "regional",
    "registry_import",
    "regional_review",
    "hosted_search",
    "hosted_selected",
    "hosted_capture",
    "openalex_content",
    "report_proposal",
    "annotation_export",
    "annotation_import",
    "github_capture",
    "browser_capture",
    "capture_replay",
    "model_download",
    "calibration",
)


def read_json(path, *, limit=16 * 1024**2):
    with Path(path).open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input JSON byte budget exceeded")
    return json.loads(raw)


def write_json(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Generated outputs can contain authorized private evidence. Never publish
    # outside the operator's chosen local file and never overwrite silently.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(
            payload,
            stream,
            ensure_ascii=False,
            indent=2,
            default=_default,
            allow_nan=False,
        )
        stream.write("\n")


def _default(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError("non-JSON runtime output")


def _secret(provider):
    return next(
        (os.environ[key] for key in KEYS.get(provider, ()) if os.environ.get(key)), None
    )


def _required(scopes, *required):
    if "operator" not in scopes and not set(required) <= scopes:
        from src.evaluation.runtime_errors import BackendError

        raise BackendError(
            "unauthorized", "trusted local scopes do not authorize this operation"
        )


def _http(conn, config, request, provider):
    from src.ingestion.hosted_acquisition import HOSTS, zyte_scrapy_transport
    from src.ingestion.provider_execution import DurableHTTP
    from src.ingestion.regional_providers import PROVIDER_HOSTS

    if request.get("network_approved") is not True:
        raise ValueError(
            "network acquisition requires explicit approval in the operator request"
        )
    budget = request.get("budget")
    required = {
        "budget_id",
        "max_requests",
        "max_usd_micros",
        "max_bytes",
        "account_ref",
        "reuse_notice",
    }
    if not isinstance(budget, dict) or set(budget) != required:
        raise ValueError(
            "explicit immutable project request, spend, byte, account and reuse budget required"
        )
    hosts = HOSTS.get(provider) or PROVIDER_HOSTS.get(provider)
    if hosts is None:
        raise ValueError("unsupported native provider")
    return DurableHTTP(
        conn,
        provider=provider,
        principal_id=config.principal,
        allowed_hosts=hosts,
        transport=zyte_scrapy_transport if provider == "zyte" else None,
        **budget,
    )


def run_request(config, request, *, scopes):
    if not isinstance(request, dict) or request.get("kind") not in KINDS:
        raise ValueError("supported explicit optional integration kind required")
    if any(
        key in request
        for key in ("scopes", "principal_id", "credential", "api_key", "token")
    ):
        raise ValueError("request JSON cannot set authority or contain credentials")
    kind, namespace = request["kind"], request.get("namespace")
    if kind == "model_download":
        from src.argument_mining.model_registry import (
            optional_model_path,
            optional_model_spec,
        )

        _required(scopes, "knowledge:optional:download")
        if request.get("download_approved") is not True:
            raise ValueError("model download requires explicit approval")
        model = request["model"]
        path = optional_model_path(
            model, download=True, max_download_bytes=request["max_download_bytes"]
        )
        return {
            "status": "downloaded",
            "path": str(path),
            "model": optional_model_spec(model),
            "quality_evaluated": False,
        }
    if not isinstance(namespace, str) or not 1 <= len(namespace) <= 200:
        raise ValueError("explicit bounded namespace required")
    observation = request.get("observation")
    params = request.get("parameters", {})
    if not isinstance(params, dict):
        raise TypeError("parameters must be an object")
    with open_warehouse(config) as conn:
        if kind == "regional_review":
            from src.ingestion.regional_review import queue_candidate

            return queue_candidate(
                conn,
                namespace=namespace,
                principal_id=config.principal,
                scopes=scopes,
                **params,
            )
        if kind in {"analysis", "analysis_read", "entity_review"}:
            from src.kb.optional_runtime import OptionalAnalysisStore

            store = OptionalAnalysisStore(conn)
            auth = {"principal_id": config.principal, "scopes": scopes}
            if kind == "analysis_read":
                return store.inspect(namespace, request["run_id"], **auth)
            if kind == "entity_review":
                return store.queue_entity_candidate(
                    namespace,
                    request["run_id"],
                    request["candidate_index"],
                    **auth,
                    **params,
                )
            from src.evaluation.observability import PhoenixTraceSink

            trace = request.get("trace", {"enabled": False})
            if not isinstance(trace, dict):
                raise TypeError("trace configuration must be an object")
            if trace.get("enabled"):
                _required(scopes, "knowledge:telemetry:export")
            with PhoenixTraceSink(**trace) as sink:
                return store.run(
                    namespace,
                    request["run_id"],
                    request["backend"],
                    params,
                    source_refs=request["sources"],
                    timeout_s=request.get("timeout_s", 60),
                    max_rss_bytes=request.get("max_rss_bytes", 6 * 1024**3),
                    trace_sink=sink,
                    **auth,
                )
        if kind == "benchmark":
            from src.evaluation.benchmark_runtime import evaluate_manifest

            _required(
                scopes, "knowledge:optional:execute", f"namespace:{namespace}:write"
            )
            return evaluate_manifest(read_json(request["manifest_file"]))
        if kind == "calibration":
            from src.evaluation.mining_runtime import evaluate_policy, fit_policy

            _required(
                scopes, "knowledge:optional:execute", f"namespace:{namespace}:write"
            )
            validation = read_json(request["validation_file"])
            test = read_json(request["test_file"])
            policy = fit_policy(validation, **params)
            return {
                "status": "measured",
                "policy": policy,
                "held_out": evaluate_policy(test, policy),
                "human_annotation_collected": False,
            }
        if kind in {"regional", "registry_import"}:
            from src.ingestion.regional_providers import RegionalClient
            from src.ingestion.regional_workflow import RegionalAcquisition

            _required(
                scopes, "knowledge:ingestion:execute", f"namespace:{namespace}:write"
            )
            provider = request["provider"]
            client = None
            if kind == "regional" and request.get("operation") != "replay":
                http = _http(conn, config, request, provider)
                client = RegionalClient(
                    http,
                    principal_id=config.principal,
                    credential=_secret(provider),
                    per_request_cost_micros=request.get(
                        "max_cost_per_request_micros", 0
                    ),
                )
            service = RegionalAcquisition(
                conn,
                namespace=namespace,
                principal_id=config.principal,
                reuse_notice=request.get("reuse_notice")
                or request.get("budget", {}).get("reuse_notice"),
                client=client,
            )
            if kind == "regional" and request.get("operation") == "replay":
                return service.replay(observation, provider=provider, scopes=scopes)
            if kind == "registry_import":
                return service.import_file(
                    request["file"],
                    params,
                    observation,
                    provider=provider,
                    scopes=scopes,
                    expected_sha256=request["sha256"],
                )
            return service.acquire(
                request["operation"], params, observation, scopes=scopes
            )
        if kind in {
            "hosted_search",
            "hosted_selected",
            "hosted_capture",
            "openalex_content",
        }:
            from src.ingestion.hosted_acquisition import HostedClient, OpenAlexFullText

            _required(
                scopes, "knowledge:ingestion:execute", f"namespace:{namespace}:write"
            )
            provider = request["provider"]
            http = _http(conn, config, request, provider)
            options = {
                "principal_id": config.principal,
                "credential": _secret(provider),
                "enabled": True,
            }
            price = request.get("max_cost_per_request_micros", 0)
            if kind == "openalex_content":
                return OpenAlexFullText(
                    http, **options, max_cost_per_download_micros=price
                ).acquire(
                    request["work"],
                    observation,
                    namespace=namespace,
                    scopes=scopes,
                    **params,
                )
            client = HostedClient(http, **options, max_cost_per_request_micros=price)
            if kind in {"hosted_search", "hosted_selected"}:
                result = client.search(observation=observation, **params)
                if kind == "hosted_selected":
                    result["acquired"] = client.acquire_selected(
                        conn,
                        result,
                        request["selected_urls"],
                        namespace=namespace,
                        scopes=scopes,
                        allowed_hosts=request["allowed_source_hosts"],
                        language=request["language"],
                        observation=observation,
                    )
                return result
            if provider == "jina":
                result = client.reader(observation=observation, **params)
            else:
                result = client.scrape(observation=observation, **params)
            # Hosted transformed output has explicit lineage, never original HTTP
            # provenance or made-up source coordinates.
            from services.ingest.common.document_model import Document
            from src.ingestion.document_store import DocumentStore
            from src.ingestion.extract import extract_article
            from src.ingestion.provider_execution import digest

            text = result.get("text")
            if text is None:
                extracted = extract_article(result["html"], url=result["final_url"])
                text = extracted.text
            if not text:
                return {
                    "status": "unavailable",
                    "reason": "no_usable_text",
                    "capture": result,
                }
            identity = (
                "hosted-derived:"
                + digest([namespace, provider, result["source_url"]])[:32]
            )
            doc = Document(
                document_id=identity,
                source_type="web",
                language="und",
                source_id=result["source_url"],
                ingested_at=result["receipt"]["observed_at_ms"],
                url=result.get("final_url", result["source_url"]),
                title="Provider-derived captured evidence",
                content=text,
                metadata={
                    "namespace": namespace,
                    "provider": provider,
                    "representation": result["representation"],
                    "native_capture_sha256": result["receipt"]["digest"],
                    "precise_locators": False,
                },
            )
            if DocumentStore(conn).upsert([doc]).invalid:
                raise ValueError("hosted evidence failed document validation")
            return {"status": "captured", "document_id": identity, "capture": result}
        if kind == "report_proposal":
            from src.evaluation.report_generation import propose_report_revision
            from src.kb.report_updates import ReportUpdateStore

            _required(scopes, "knowledge:optional:execute")
            return propose_report_revision(
                ReportUpdateStore(conn),
                namespace,
                request["assessment_id"],
                request["assertion_id"],
                principal_id=config.principal,
                scopes=scopes,
                **params,
            )
        if kind in {"annotation_export", "annotation_import"}:
            from src.kb.annotation_exchange import LabelStudioExchange

            exchange = LabelStudioExchange(conn)
            if kind == "annotation_export":
                return exchange.export(
                    namespace,
                    request["task_ids"],
                    principal_id=config.principal,
                    scopes=scopes,
                    **params,
                )
            return exchange.import_completed(
                request["exchange_id"],
                read_json(request["completed_file"]),
                principal_id=config.principal,
                scopes=scopes,
                **params,
            )
        if kind in {"github_capture", "browser_capture", "capture_replay"}:
            from src.kb.mcp_research import (
                GitHubResearchSource,
                InteractiveEvidenceSession,
                MCPCaptureStore,
            )
            from src.kb.mcp_transport import MCPHTTPClient

            _required(
                scopes, "knowledge:federation:read", f"namespace:{namespace}:read"
            )
            if kind == "capture_replay":
                result = MCPCaptureStore(conn).replay(
                    namespace,
                    request["provider"],
                    observation,
                    None,
                    principal_id=config.principal,
                    scopes=scopes,
                )
                if result is None:
                    raise ValueError("captured observation is unavailable")
                return result
            _required(
                scopes, "knowledge:ingestion:execute", f"namespace:{namespace}:write"
            )
            if request.get("network_approved") is not True:
                raise ValueError("explicit remote MCP capture approval required")
            with MCPHTTPClient(
                request["endpoint"],
                kind="github" if kind == "github_capture" else "browser",
                token=_secret("github") if kind == "github_capture" else None,
                timeout_s=request.get("timeout_s", 15),
            ) as remote:
                if kind == "github_capture":
                    source = GitHubResearchSource(
                        conn,
                        remote,
                        repositories=request["repositories"],
                        namespace=namespace,
                        principal_id=config.principal,
                        max_requests=request.get("max_requests", 100),
                    )
                    return source.query(
                        {**params, "observation_id": observation}, scopes=scopes
                    )
                source = InteractiveEvidenceSession(
                    conn,
                    remote,
                    domains=request["domains"],
                    namespace=namespace,
                    principal_id=config.principal,
                    network_guard_confirmed=request.get(
                        "network_guard_confirmed", False
                    ),
                )
                return source.acquire(
                    observation_id=observation, scopes=scopes, **params
                )
    raise ValueError("unsupported optional request")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Describe executable capabilities without loading models or using the network",
    )
    args = parser.parse_args(argv)
    try:
        if args.doctor:
            from src.argument_mining.model_registry import (
                OPTIONAL_PINS,
                optional_model_path,
                optional_model_spec,
            )
            from src.evaluation.runtime_jobs import OPERATIONS

            models = []
            for kind in sorted(OPTIONAL_PINS):
                try:
                    path = optional_model_path(kind, download=False)
                    available = path is not None
                except Exception:  # noqa: BLE001 - cache compatibility failure is explicit unavailable
                    available = False
                models.append(
                    {
                        "kind": kind,
                        "pin": optional_model_spec(kind),
                        "cached": available,
                        "quality_validated": False,
                    }
                )
            result = {
                "contract": "noesis-optional-capabilities-v1",
                "request_kinds": KINDS,
                "job_operations": sorted(OPERATIONS),
                "models": models,
                "production_defaults_changed": False,
                "network_calls": 0,
            }
        else:
            if args.request is None:
                parser.error("--request is required unless --doctor is used")
            config = load_config(args.config)
            scopes = {
                value.strip()
                for value in os.environ.get("NOESIS_OPTIONAL_SCOPES", "").split(",")
                if value.strip()
            }
            result = run_request(config, read_json(args.request), scopes=scopes)
        if args.output:
            write_json(args.output, result)
        else:
            print(
                json.dumps(
                    result, ensure_ascii=False, default=_default, allow_nan=False
                )
            )
        status = result.get("outcome", result).get("status")
        return 3 if status in {"failed", "unavailable", "cancelled", "partial"} else 0
    except Exception as exc:  # noqa: BLE001 - CLI errors never disclose credentials, source text or native response bodies
        print(
            json.dumps(
                {
                    "error": {
                        "code": getattr(
                            exc,
                            "code",
                            "invalid_configuration"
                            if isinstance(exc, ConfigError)
                            else "operation_failed",
                        ),
                        "type": type(exc).__name__,
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

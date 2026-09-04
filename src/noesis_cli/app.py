"""A narrow CLI over Noesis' canonical local contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import CLI_CONTRACT, __version__
from .config import ConfigError, initialize, load_config, open_warehouse

EXIT_CONFIG = 3
EXIT_DEPENDENCY = 4
EXIT_OPERATION = 5
EXIT_CONFIRMATION = 6


class CLIError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = EXIT_OPERATION,
        repair: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.repair = repair


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit stable JSON"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noesis", description=__doc__)
    parser.add_argument("--version", action="version", version=f"noesis {__version__}")
    parser.add_argument(
        "--config", type=Path, help="local config (default: .noesis/config.json)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a validated local workspace")
    init.add_argument("--root", type=Path, help="workspace directory")
    init.add_argument("--non-interactive", action="store_true")
    _json_flag(init)

    doctor = sub.add_parser("doctor", help="run offline installation diagnostics")
    _json_flag(doctor)

    ingest = sub.add_parser("ingest", help="ingest a local file or HTTP(S) URL")
    ingest.add_argument("source")
    ingest.add_argument("--domain", default="local")
    ingest.add_argument("--language", default="en")
    _json_flag(ingest)

    ask = sub.add_parser("ask", help="answer from a configured evidence domain")
    ask.add_argument("question")
    ask.add_argument("--domain", default="local")
    ask.add_argument("--format", choices=("markdown", "json"), default="markdown")
    ask.add_argument("--limit", type=int, default=5)
    ask.add_argument("--minimum-relevance", type=float, default=0.34)

    brief = sub.add_parser("brief", help="generate a bounded cross-domain brief")
    brief.add_argument("--domains", help="comma-separated domains")
    brief.add_argument("--since")
    brief.add_argument("--budget", type=int, default=15)
    brief.add_argument("--format", choices=("markdown", "json"), default="markdown")

    watch = sub.add_parser("watch", help="manage durable Claim Watches")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    create = watch_sub.add_parser("create", help="create an idempotent watch")
    create.add_argument("--domain", required=True)
    create.add_argument(
        "--type", choices=("query", "claim", "entity", "topic"), required=True
    )
    create.add_argument("--value", required=True)
    create.add_argument("--event", action="append", dest="events")
    create.add_argument("--stale-after-ms", type=int, default=86_400_000)
    _json_flag(create)
    listing = watch_sub.add_parser(
        "list", help="list watches for the configured principal"
    )
    listing.add_argument("--domain")
    _json_flag(listing)
    poll = watch_sub.add_parser("poll", help="poll and persist an opaque cursor")
    poll.add_argument("watch_id")
    poll.add_argument("--cursor")
    poll.add_argument("--cursor-file", type=Path)
    poll.add_argument("--no-save-cursor", action="store_true")
    poll.add_argument("--limit", type=int, default=50)
    poll.add_argument("--event", action="append", dest="events")
    _json_flag(poll)
    for action in ("pause", "resume"):
        operation = watch_sub.add_parser(action, help=f"{action} a watch")
        operation.add_argument("watch_id")
        _json_flag(operation)
    delete = watch_sub.add_parser(
        "delete", help="soft-delete a watch with confirmation"
    )
    delete.add_argument("watch_id")
    delete.add_argument(
        "--yes", action="store_true", help="explicitly confirm deletion"
    )
    delete.add_argument("--non-interactive", action="store_true")
    _json_flag(delete)
    scan = watch_sub.add_parser("scan", help="run the matcher at a committed watermark")
    scan.add_argument("watermark", type=int)
    _json_flag(scan)
    replay = watch_sub.add_parser("replay", help="audit retained watch transitions")
    replay.add_argument("watch_id")
    replay.add_argument("--from-watermark", type=int, required=True)
    replay.add_argument("--to-watermark", type=int, required=True)
    _json_flag(replay)

    watches = sub.add_parser("watches", help="alias for `noesis watch list`")
    watches.add_argument("--domain")
    _json_flag(watches)

    export = sub.add_parser("export", help="export a portable Evidence Bundle")
    export_sub = export.add_subparsers(dest="export_command", required=True)
    answer = export_sub.add_parser(
        "answer", help="answer a question and export its evidence"
    )
    answer.add_argument("--domain", required=True)
    answer.add_argument("--question", required=True)
    answer.add_argument("--output", type=Path, required=True)
    answer.add_argument("--include-private", action="store_true")
    answer.add_argument("--force", action="store_true")
    _json_flag(answer)
    claim = export_sub.add_parser("claim", help="export one claim and citation closure")
    claim.add_argument("claim_id")
    claim.add_argument("--domain", required=True)
    claim.add_argument("--output", type=Path, required=True)
    claim.add_argument("--include-private", action="store_true")
    claim.add_argument("--force", action="store_true")
    _json_flag(claim)
    integrity = export_sub.add_parser(
        "integrity", help="export one integrity-ledger record"
    )
    integrity.add_argument("document_id")
    integrity.add_argument("--domain", required=True)
    integrity.add_argument("--output", type=Path, required=True)
    integrity.add_argument("--include-private", action="store_true")
    integrity.add_argument("--force", action="store_true")
    _json_flag(integrity)

    namespace = sub.add_parser("namespace", help="export or import a portable knowledge namespace")
    namespace_sub = namespace.add_subparsers(dest="namespace_command", required=True)
    namespace_export = namespace_sub.add_parser("export", help="export a deterministic namespace package")
    namespace_export.add_argument("namespace")
    namespace_export.add_argument("--output", type=Path, required=True)
    namespace_export.add_argument("--mode", choices=("full", "filtered", "metadata-only"), default="full")
    namespace_export.add_argument("--kind", action="append", dest="kinds")
    namespace_export.add_argument("--sensitivity", action="append", dest="sensitivities")
    namespace_export.add_argument("--force", action="store_true")
    _json_flag(namespace_export)
    namespace_verify = namespace_sub.add_parser("verify", help="verify package hashes offline")
    namespace_verify.add_argument("package", type=Path)
    _json_flag(namespace_verify)
    namespace_import = namespace_sub.add_parser("import", help="atomically import a verified package")
    namespace_import.add_argument("package", type=Path)
    namespace_import.add_argument("--target", required=True)
    namespace_import.add_argument("--policy", choices=("new-namespace", "reject", "keep-both", "remap"), default="reject")
    namespace_import.add_argument("--yes", action="store_true")
    _json_flag(namespace_import)

    verify = sub.add_parser("verify", help="verify an Evidence Bundle offline")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--schema", type=Path)
    _json_flag(verify)

    serve = sub.add_parser("serve", help="start a supported REST or MCP surface")
    serve.add_argument("--surface", choices=("api", "kb-mcp"), default="api")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--transport", choices=("stdio", "http"), default="http")
    serve.add_argument("--dry-run", action="store_true")
    _json_flag(serve)
    return parser


def _envelope(command: str, data: Any) -> dict[str, Any]:
    return {"cli_contract": CLI_CONTRACT, "command": command, "ok": True, "data": data}


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _redact(value: str) -> str:
    value = re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        value,
    )
    for name, secret in os.environ.items():
        if (
            secret
            and len(secret) >= 4
            and any(
                word in name.upper()
                for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
            )
        ):
            value = value.replace(secret, "[REDACTED]")
    return value


def _error_payload(command: str, error: CLIError) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": error.code, "message": _redact(str(error))}
    if error.repair:
        detail["repair"] = error.repair
    return {
        "cli_contract": CLI_CONTRACT,
        "command": command,
        "ok": False,
        "error": detail,
    }


def _write_json(path: Path, value: Any, *, force: bool = False) -> Path:
    target = path.expanduser().resolve()
    if target.exists() and not force:
        raise CLIError(
            "output_exists",
            f"refusing to overwrite {target}",
            repair="choose an unused --output path or pass --force",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        temporary.replace(target)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return target


def _write_text_atomic(path: Path, value: str) -> None:
    """Replace a small state file without exposing a partial cursor."""
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        temporary.replace(target)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _runtime(args: argparse.Namespace):
    try:
        return load_config(args.config)
    except ConfigError as exc:
        raise CLIError(
            "configuration_error", str(exc), exit_code=EXIT_CONFIG, repair="noesis init"
        ) from exc


def _registry(config):
    from src.kb.registry import load_registry

    return load_registry(config.domains)


def _is_private(config, domain: str) -> bool:
    definition = _registry(config).get(domain)
    return "private" in {tag.casefold() for tag in definition.tags}


def _command_init(args: argparse.Namespace) -> int:
    try:
        config, changes = initialize(config_path=args.config, root=args.root)
    except ConfigError as exc:
        raise CLIError("configuration_error", str(exc), exit_code=EXIT_CONFIG) from exc
    data = {
        **config.public_dict(),
        **changes,
        "idempotent": True,
        "non_interactive": True,
    }
    if args.as_json:
        _print_json(_envelope("init", data))
    else:
        state = "ready" if changes["created"] else "already initialized"
        print(f"Noesis workspace {state}: {config.root}")
        print(f"Config: {config.path}")
        print(f"Warehouse: {config.warehouse}")
        if changes["preserved"]:
            print("Preserved: " + ", ".join(changes["preserved"]))
    return 0


def _command_doctor(args: argparse.Namespace) -> int:
    from .doctor import diagnose

    report = diagnose(args.config)
    if args.as_json:
        _print_json(_envelope("doctor", report))
    else:
        print(f"Noesis doctor: {report['status']}")
        for row in report["checks"]:
            label = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}[row["status"]]
            print(f"[{label}] {row['name']}: {row['message']}")
            if row.get("repair"):
                print(f"       repair: {row['repair']}")
    return 1 if report["required_failures"] else 0


def _url_document(source: str, language: str):
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CLIError("bad_source", "URL ingestion supports only HTTP(S) URLs")
    from urllib.request import Request, urlopen

    try:
        with urlopen(
            Request(source, headers={"User-Agent": "Noesis/0.1"}), timeout=20
        ) as response:
            content = response.read(16 * 1024 * 1024 + 1)
            content_type = response.headers.get_content_type()
    except Exception as exc:
        raise CLIError(
            "fetch_failed", f"could not fetch URL: {type(exc).__name__}: {exc}"
        ) from exc
    if len(content) > 16 * 1024 * 1024:
        raise CLIError("source_too_large", "URL response exceeds the 16 MiB CLI limit")
    from services.ingest.common.document_model import Document
    from src.ingestion.connectors.upload.detectors import detect_format
    from src.ingestion.connectors.upload.parsers import extract_text

    fmt = (
        "html"
        if content_type in {"text/html", "application/xhtml+xml"}
        else detect_format(content, parsed.path)
    )
    text, metadata = extract_text(content, fmt)
    if not text.strip():
        repair = metadata.get("error") if isinstance(metadata, dict) else None
        raise CLIError(
            "parse_failed", "URL produced no extractable text", repair=repair
        )
    title = str(
        metadata.get("title") or parsed.path.rsplit("/", 1)[-1] or parsed.netloc
    )
    return Document(
        document_id="web:" + hashlib.sha256(source.encode()).hexdigest()[:24],
        source_type="web",
        language=language,
        ingested_at=int(time.time() * 1000),
        source_id=parsed.netloc.casefold(),
        url=source,
        title=title,
        content=text,
        metadata={**metadata, "source_url": source},
    )


def _command_ingest(args: argparse.Namespace) -> int:
    config = _runtime(args)
    registry = _registry(config)
    registry.get(args.domain)
    source = args.source
    if urlparse(source).scheme in {"http", "https"}:
        documents = [_url_document(source, args.language)]
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise CLIError("source_not_found", f"file not found: {path}")
        from src.ingestion.connectors.upload.connector import UploadConnector

        connector = UploadConnector(default_language=args.language)
        documents = []
        for ref in connector.discover(path):
            documents.extend(connector.parse(connector.fetch(ref)))
        if not documents:
            raise CLIError(
                "parse_failed",
                f"{path} produced no extractable text",
                repair='install "noesis-evidence[media]" for PDF/media formats',
            )
    private = _is_private(config, args.domain)
    for document in documents:
        tags = list(document.metadata.get("tags") or [])
        document.metadata["tags"] = sorted(
            set([*tags, args.domain] + (["private"] if private else []))
        )
        document.source_id = document.source_id or (
            "local-upload" if document.source_type == "note" else None
        )
    conn = open_warehouse(config)
    try:
        from src.database.local_warehouse_seed import ensure_schema
        from src.ingestion.document_store import DocumentStore
        from src.kb.membership import run_membership_pass

        ensure_schema(conn)
        summary = DocumentStore(conn).upsert(documents)
        membership = run_membership_pass(conn, registry)
    finally:
        conn.close()
    data = {
        "domain": args.domain,
        "source": source,
        "documents": [document.document_id for document in documents],
        "upsert": summary.as_dict(),
        "membership": membership["domains"].get(args.domain, {}),
        "retry_safe": True,
    }
    if args.as_json:
        _print_json(_envelope("ingest", data))
    else:
        print(
            f"Ingested {summary.inserted}; duplicates {summary.duplicate}; "
            f"invalid {summary.invalid}; domain {args.domain}"
        )
        for document_id in data["documents"]:
            print(f"- {document_id}")
    return 0 if not summary.invalid else EXIT_OPERATION


def _command_ask(args: argparse.Namespace) -> int:
    config = _runtime(args)
    conn = open_warehouse(config)
    try:
        from src.kb.contract import kb_answer

        answer = kb_answer(
            args.domain,
            args.question,
            limit=args.limit,
            minimum_relevance=args.minimum_relevance,
            conn=conn,
            config_path=config.domains,
        )
    finally:
        conn.close()
    if args.format == "json":
        _print_json(_envelope("ask", answer))
    else:
        print(answer["data"]["rendered"])
    return 0


def _command_brief(args: argparse.Namespace) -> int:
    config = _runtime(args)
    domains = (
        [item.strip() for item in args.domains.split(",") if item.strip()]
        if args.domains
        else None
    )
    conn = open_warehouse(config)
    try:
        from src.kb.contract import kb_brief

        result = kb_brief(
            domains, args.since, args.budget, conn=conn, config_path=config.domains
        )
    finally:
        conn.close()
    if args.format == "json":
        _print_json(_envelope("brief", result))
    else:
        print(result["data"]["markdown"])
    return 0


def _cursor_path(config, watch_id: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    marker = hashlib.sha256(watch_id.encode()).hexdigest()[:24]
    return config.cursor_directory / f"{marker}.cursor"


def _command_watch(args: argparse.Namespace) -> int:
    config = _runtime(args)
    conn = open_warehouse(config)
    command = args.watch_command if args.command == "watch" else "list"
    try:
        from src.kb import contract

        if command == "create":
            result = contract.watch_create(
                args.domain,
                config.principal,
                {"type": args.type, "value": args.value},
                args.events,
                args.stale_after_ms,
                conn=conn,
                config_path=config.domains,
            )
        elif command == "list":
            result = contract.watch_list(
                config.principal, args.domain, conn=conn, config_path=config.domains
            )
        elif command == "poll":
            cursor_file = _cursor_path(config, args.watch_id, args.cursor_file)
            cursor = args.cursor
            if cursor is None and cursor_file.is_file():
                cursor = cursor_file.read_text(encoding="utf-8").strip() or None
            result = contract.watch_poll(
                args.watch_id,
                config.principal,
                cursor,
                args.limit,
                args.events,
                conn=conn,
            )
            if not args.no_save_cursor:
                _write_text_atomic(cursor_file, result["data"]["cursor"] + "\n")
                result = {
                    **result,
                    "cursor_file": str(cursor_file),
                    "cursor_saved": True,
                }
        elif command == "pause":
            result = contract.watch_pause(args.watch_id, config.principal, conn=conn)
        elif command == "resume":
            result = contract.watch_resume(args.watch_id, config.principal, conn=conn)
        elif command == "delete":
            confirmed = args.yes
            if not confirmed and not args.non_interactive and sys.stdin.isatty():
                confirmed = (
                    input(f"Type {args.watch_id} to confirm deletion: ").strip()
                    == args.watch_id
                )
            if not confirmed:
                raise CLIError(
                    "confirmation_required",
                    "watch deletion requires explicit confirmation",
                    exit_code=EXIT_CONFIRMATION,
                    repair="rerun with --yes",
                )
            result = contract.watch_delete(
                args.watch_id, config.principal, True, conn=conn
            )
        elif command == "scan":
            result = contract.watch_scan(
                config.principal,
                args.watermark,
                conn=conn,
                config_path=config.domains,
            )
        elif command == "replay":
            result = contract.watch_replay(
                args.watch_id,
                config.principal,
                args.from_watermark,
                args.to_watermark,
                conn=conn,
            )
        else:  # pragma: no cover - parser owns dispatch
            raise CLIError("bad_command", f"unknown watch command {command}")
    finally:
        conn.close()
    if args.as_json:
        _print_json(_envelope(f"watch.{command}", result))
    else:
        _print_json(result)
    return 0


def _visible_claim(conn, config, domain: str, claim_id: str) -> bool:
    from src.kb.contract import kb_claims

    result = kb_claims(domain, limit=100_000, conn=conn, config_path=config.domains)
    return any(
        row.get("claim_id") == claim_id
        for cluster in result["data"]
        for row in cluster.get("citations", [])
    )


def _command_export(args: argparse.Namespace) -> int:
    config = _runtime(args)
    private = _is_private(config, args.domain)
    if private and not args.include_private:
        raise CLIError(
            "private_evidence_excluded",
            f"domain {args.domain!r} is private and is excluded by default",
            repair="review the output destination and pass --include-private explicitly",
        )
    conn = open_warehouse(config)
    try:
        from src.evidence_bundle import export_answer, export_claim, export_integrity
        from src.kb import contract

        if args.export_command == "answer":
            source = contract.kb_answer(
                args.domain, args.question, conn=conn, config_path=config.domains
            )
            bundle = export_answer(
                source,
                inputs={"domain": args.domain, "question": args.question},
                include_private=args.include_private,
            )
        elif args.export_command == "claim":
            if not _visible_claim(conn, config, args.domain, args.claim_id):
                raise CLIError(
                    "not_found",
                    f"claim {args.claim_id!r} is not visible in {args.domain!r}",
                )
            bundle = export_claim(
                conn,
                args.claim_id,
                visibility="private" if private else "public",
                include_private=args.include_private,
            )
        else:
            contract.kb_integrity(
                args.domain,
                args.document_id,
                conn=conn,
                config_path=config.domains,
            )
            bundle = export_integrity(
                conn,
                args.document_id,
                visibility="private" if private else "public",
                include_private=args.include_private,
            )
    finally:
        conn.close()
    target = _write_json(args.output, bundle, force=args.force)
    result = {
        "bundle_contract": bundle["contract"],
        "bundle_id": bundle["bundle_id"],
        "operation": bundle["operation"]["type"],
        "output": str(target),
        "private_evidence_included": args.include_private,
    }
    if args.as_json:
        _print_json(_envelope(f"export.{args.export_command}", result))
    else:
        print(f"Exported {result['bundle_id']} to {target}")
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    from src.evidence_bundle.verifier import INCOMPLETE, verify_file

    kwargs = {"schema_path": args.schema} if args.schema else {}
    result = verify_file(args.bundle, **kwargs)
    if args.as_json:
        # Preserve the established Evidence Bundle CLI JSON shape.
        _print_json(result.to_dict())
    else:
        print(f"{result.status}: {args.bundle}")
        for message in result.errors:
            print(f"ERROR: {message}")
        for message in result.warnings:
            print(f"WARNING: {message}")
    if result.valid:
        return 0
    return 2 if result.status == INCOMPLETE else 1


def _command_namespace(args: argparse.Namespace) -> int:
    from src.kb.portable_namespaces import PortableNamespaceStore

    config=_runtime(args); conn=open_warehouse(config)
    try:
        store=PortableNamespaceStore(conn,initialize=args.namespace_command=="import")
        if args.namespace_command=="export":
            filters={key:value for key,value in {"kinds":args.kinds,"sensitivities":args.sensitivities}.items() if value}
            result=store.export(args.namespace,mode=args.mode,filters=filters,scopes={"operator"})
            _write_json(args.output,result,force=args.force)
            output={"package_hash":result["manifest"]["content_hash"],"components":len(result["manifest"]["components"]),"output":str(args.output)}
        else:
            package=json.loads(args.package.read_text(encoding="utf-8"))
            if args.namespace_command=="verify": output=store.verify(package)
            else:
                if not args.yes: raise CLIError("confirmation_required","namespace import requires --yes",exit_code=EXIT_CONFIRMATION)
                preview=store.preview_import(package,args.target,conflict_policy=args.policy,scopes={"operator"})
                output=store.import_package(package,args.target,"cli:"+preview["preview_hash"],conflict_policy=args.policy,scopes={"operator"},principal_id=config.principal,expected_preview_hash=preview["preview_hash"])
    finally: conn.close()
    if args.as_json: _print_json(_envelope(f"namespace.{args.namespace_command}",output))
    else: _print_json(output)
    return 0


def _serve_report(config, args: argparse.Namespace) -> dict[str, Any]:
    host = args.host or (config.api_host if args.surface == "api" else config.mcp_host)
    port = args.port or (config.api_port if args.surface == "api" else config.mcp_port)
    transport = "http" if args.surface == "api" else args.transport
    token_set = (
        bool(os.environ.get("NOESIS_MCP_AUTH_TOKEN"))
        if args.surface == "kb-mcp"
        else False
    )
    return {
        "surface": args.surface,
        "transport": transport,
        "host": None if transport == "stdio" else host,
        "port": None if transport == "stdio" else port,
        "address": "stdio" if transport == "stdio" else f"http://{host}:{port}",
        "auth": "bearer-token"
        if token_set
        else ("application-policy" if args.surface == "api" else "none"),
        "warehouse": str(config.warehouse),
        "enabled_surfaces": [args.surface],
        "dry_run": args.dry_run,
    }


def _command_serve(args: argparse.Namespace) -> int:
    config = _runtime(args)
    report = _serve_report(config, args)
    if (
        args.as_json
        and not args.dry_run
        and args.surface == "kb-mcp"
        and args.transport == "stdio"
    ):
        raise CLIError(
            "incompatible_output",
            "JSON status output would corrupt the live MCP stdio transport",
            repair="remove --json or add --dry-run",
        )
    if args.dry_run:
        _print_json(_envelope("serve", report))
        return 0
    os.environ["NOESIS_DB_PATH"] = str(config.warehouse)
    os.environ["NOESIS_DOMAINS_CONFIG"] = str(config.domains)
    if args.surface == "api":
        try:
            import uvicorn
        except ImportError as exc:
            raise CLIError(
                "missing_dependency",
                "the API server extra is not installed",
                exit_code=EXIT_DEPENDENCY,
                repair='python -m pip install "noesis-evidence[server]"',
            ) from exc
        if args.as_json:
            _print_json(_envelope("serve", report))
        else:
            print(
                f"Starting {report['surface']} at {report['address']} "
                f"(auth: {report['auth']})",
                file=sys.stderr,
            )
        uvicorn.run("src.api.app:app", host=report["host"], port=report["port"])
        return 0
    try:
        from src.mcp_host.transport import run_server
        from tools.kb_mcp.server import mcp
    except ImportError as exc:
        raise CLIError(
            "missing_dependency",
            "the MCP server extra is not installed",
            exit_code=EXIT_DEPENDENCY,
            repair='python -m pip install "noesis-evidence[server]"',
        ) from exc
    if args.as_json:
        _print_json(_envelope("serve", report))
    else:
        print(
            f"Starting {report['surface']} at {report['address']} "
            f"(auth: {report['auth']})",
            file=sys.stderr,
        )
    os.environ["NOESIS_MCP_TRANSPORT"] = args.transport
    os.environ["NOESIS_MCP_HTTP_HOST"] = str(report["host"] or config.mcp_host)
    os.environ["NOESIS_MCP_HTTP_PORT"] = str(report["port"] or config.mcp_port)
    run_server(mcp)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _command_init(args)
        if args.command == "doctor":
            return _command_doctor(args)
        if args.command == "ingest":
            return _command_ingest(args)
        if args.command == "ask":
            return _command_ask(args)
        if args.command == "brief":
            return _command_brief(args)
        if args.command in {"watch", "watches"}:
            return _command_watch(args)
        if args.command == "export":
            return _command_export(args)
        if args.command == "verify":
            return _command_verify(args)
        if args.command == "namespace":
            return _command_namespace(args)
        if args.command == "serve":
            return _command_serve(args)
        parser.error(f"unknown command {args.command}")
    except CLIError as exc:
        if getattr(args, "as_json", False) or getattr(args, "format", None) == "json":
            _print_json(_error_payload(args.command, exc))
        else:
            print(f"error [{exc.code}]: {_redact(str(exc))}", file=sys.stderr)
            if exc.repair:
                print(f"repair: {exc.repair}", file=sys.stderr)
        return exc.exit_code
    except ConfigError as exc:
        error = CLIError(
            "configuration_error",
            str(exc),
            exit_code=EXIT_CONFIG,
            repair="noesis doctor",
        )
        if getattr(args, "as_json", False) or getattr(args, "format", None) == "json":
            _print_json(_error_payload(args.command, error))
        else:
            print(f"error [{error.code}]: {_redact(str(error))}", file=sys.stderr)
        return error.exit_code
    except Exception as exc:  # noqa: BLE001 - stable CLI process boundary
        code = getattr(exc, "code", "operation_failed")
        error = CLIError(str(code), f"{type(exc).__name__}: {exc}")
        if getattr(args, "as_json", False) or getattr(args, "format", None) == "json":
            _print_json(_error_payload(args.command, error))
        else:
            print(f"error [{error.code}]: {_redact(str(error))}", file=sys.stderr)
        return error.exit_code
    return 0


__all__ = ["CLIError", "build_parser", "main"]

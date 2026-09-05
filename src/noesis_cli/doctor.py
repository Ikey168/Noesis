"""Offline, secret-safe diagnostics for the local Noesis installation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from .config import ConfigError, RuntimeConfig, load_config, open_warehouse

_CAPABILITIES = {
    "ingestion": {"feedparser":"feedparser", "trafilatura":"trafilatura", "readability":"readability-lxml", "extruct":"extruct", "bs4":"beautifulsoup4"},
    "browser": {"scrapy":"scrapy", "scrapy_playwright":"scrapy-playwright", "playwright":"playwright", "aiohttp":"aiohttp", "aiofiles":"aiofiles", "psutil":"psutil"},
    "required": {
        "duckdb": "duckdb",
        "fastavro": "fastavro",
        "jsonschema": "jsonschema",
        "yaml": "PyYAML",
    },
    "server": {
        "fastapi": "fastapi",
        "fastmcp": "fastmcp",
        "mcp": "mcp",
        "uvicorn": "uvicorn",
    },
    "models": {
        "huggingface_hub": "huggingface-hub",
        "sentence_transformers": "sentence-transformers",
        "torch": "torch",
        "transformers": "transformers",
    },
    "vector": {"qdrant_client": "qdrant-client", "psycopg2": "psycopg2-binary"},
    "media": {
        "bs4": "beautifulsoup4",
        "ebooklib": "ebooklib",
        "lxml": "lxml",
        "pdf2image": "pdf2image",
        "pdfminer": "pdfminer.six",
        "PIL": "Pillow",
        "fitz": "PyMuPDF",
        "pytesseract": "pytesseract",
    },
    "orchestration": {
        "airflow": "apache-airflow",
        "apscheduler": "apscheduler",
        "dbt": "dbt-core",
        "mlflow": "mlflow",
    },
    "cloud": {"boto3": "boto3", "botocore": "botocore", "redis": "redis"},
}


def _available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _check(
    name: str,
    status: str,
    message: str,
    *,
    required: bool,
    repair: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "status": status,
        "required": required,
        "message": message,
    }
    if repair:
        item["repair"] = repair
    return item


def diagnose(config_path: Path | None = None) -> dict[str, Any]:
    """Inspect local readiness without network calls or secret values."""
    checks: list[dict[str, Any]] = []
    for group, modules in _CAPABILITIES.items():
        missing = [
            package for module, package in modules.items() if not _available(module)
        ]
        required = group == "required"
        if missing:
            extra = "minimal" if required else group
            checks.append(
                _check(
                    f"dependencies.{group}",
                    "fail" if required else "warn",
                    "missing: " + ", ".join(missing),
                    required=required,
                    repair=f'python -m pip install "noesis-evidence[{extra}]"',
                )
            )
        else:
            checks.append(
                _check(
                    f"dependencies.{group}",
                    "pass",
                    "available",
                    required=required,
                )
            )

    config: RuntimeConfig | None = None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        checks.append(
            _check(
                "configuration",
                "fail",
                str(exc),
                required=True,
                repair="noesis init",
            )
        )
    else:
        checks.append(
            _check(
                "configuration",
                "pass",
                f"valid at {config.path}",
                required=True,
            )
        )
        parent = config.warehouse.parent
        writable = (
            os.access(parent, os.W_OK)
            if parent.exists()
            else os.access(parent.parent, os.W_OK)
        )
        checks.append(
            _check(
                "storage",
                "pass" if writable else "fail",
                f"warehouse parent {'is' if writable else 'is not'} writable: {parent}",
                required=True,
                repair=f"create a writable directory for {parent}"
                if not writable
                else None,
            )
        )
        try:
            conn = open_warehouse(config)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            checks.append(
                _check(
                    "warehouse",
                    "fail",
                    f"DuckDB could not open the configured warehouse: {type(exc).__name__}",
                    required=True,
                    repair="close other writers or choose another warehouse path",
                )
            )
        else:
            checks.append(
                _check(
                    "warehouse", "pass", "DuckDB read/write probe passed", required=True
                )
            )
        try:
            from src.kb.registry import load_registry

            registry = load_registry(config.domains)
            names = registry.names()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            checks.append(
                _check(
                    "domains",
                    "fail",
                    f"domain configuration is invalid: {type(exc).__name__}: {exc}",
                    required=True,
                    repair=f"repair {config.domains} and rerun `noesis doctor`",
                )
            )
        else:
            checks.append(
                _check(
                    "domains",
                    "pass",
                    f"validated {len(names)} domain(s): {', '.join(names)}",
                    required=True,
                )
            )

    if _available("huggingface_hub"):
        try:
            from src.argument_mining.model_registry import verify_pins

            warnings = verify_pins(require_cache=True)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            warnings = [f"model cache inspection failed: {type(exc).__name__}"]
        checks.append(
            _check(
                "model_pins",
                "warn" if warnings else "pass",
                "; ".join(warnings)
                if warnings
                else "pinned models and local cache agree",
                required=False,
                repair="python -m src.argument_mining.fetch_models"
                if warnings
                else None,
            )
        )
    else:
        checks.append(
            _check(
                "model_pins",
                "warn",
                "model stack is not installed; deterministic local answers remain available",
                required=False,
                repair='python -m pip install "noesis-evidence[models]"',
            )
        )

    mcp_ready = _available("fastmcp") and _available("mcp")
    checks.append(
        _check(
            "mcp",
            "pass" if mcp_ready else "warn",
            "KB MCP server is ready"
            if mcp_ready
            else "MCP runtime is optional and unavailable",
            required=False,
            repair='python -m pip install "noesis-evidence[server]"'
            if not mcp_ready
            else None,
        )
    )
    required_failures = sum(
        row["status"] == "fail" and row["required"] for row in checks
    )
    optional_warnings = sum(row["status"] == "warn" for row in checks)
    return {
        "status": "broken"
        if required_failures
        else ("degraded" if optional_warnings else "ready"),
        "network_used": False,
        "required_failures": required_failures,
        "optional_warnings": optional_warnings,
        "checks": checks,
    }


__all__ = ["diagnose"]

"""Versioned, deployable source packs with offline and opt-in live gates."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SOURCE_PACK_CONTRACT = "noesis-source-pack-v1"
CONFORMANCE_CONTRACT = "noesis-source-pack-conformance-v1"
SUPPORTED_CONNECTORS = frozenset(
    {
        "blog",
        "dataset",
        "declarative-rest",
        "filings",
        "git",
        "manifest",
        "package-registry",
        "paper",
        "web",
    }
)
AUTH_KINDS = frozenset({"none", "optional-secret", "required-secret"})
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_FIELDS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "password", "secret", "token", "value"}
)
_BUDGET_CEILINGS = {
    "timeout_ms": 120_000,
    "max_results": 100_000,
    "max_bytes": 100_000_000,
    "max_pages": 100,
}

_DDL = """
CREATE TABLE IF NOT EXISTS source_pack_versions (
  pack_id TEXT NOT NULL, version TEXT NOT NULL, manifest_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL, installed_by TEXT NOT NULL,
  installed_at_ms BIGINT NOT NULL, PRIMARY KEY(pack_id, version)
);
CREATE TABLE IF NOT EXISTS source_pack_current (
  pack_id TEXT PRIMARY KEY, version TEXT NOT NULL, enabled BOOLEAN NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_pack_health (
  pack_id TEXT NOT NULL, source_id TEXT NOT NULL, checked_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, classification TEXT NOT NULL, detail_json TEXT NOT NULL,
  PRIMARY KEY(pack_id, source_id)
);
CREATE TABLE IF NOT EXISTS source_pack_audit (
  event_id TEXT PRIMARY KEY, pack_id TEXT NOT NULL, principal_id TEXT NOT NULL,
  action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
"""


class SourcePackError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _now() -> int:
    return int(time.time() * 1000)


def _load(value: Any, default: Any) -> Any:
    return default if value is None else json.loads(value) if isinstance(value, str) else value


def _deep_merge(defaults: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(defaults))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = json.loads(json.dumps(value))
    return result


def _version(value: str) -> tuple[int, int, int, str]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise SourcePackError("invalid_version", "source-pack version must be semantic versioning")
    return int(match[1]), int(match[2]), int(match[3]), value


def _validate_endpoint(endpoint: str, source_id: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourcePackError("unsafe_endpoint", f"source {source_id!r} requires a credential-free HTTPS endpoint")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SourcePackError("unsafe_endpoint", f"source {source_id!r} targets localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise SourcePackError("unsafe_endpoint", f"source {source_id!r} targets a non-public address")


def _contains_secret(value: Any, *, parent: str = "") -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_FIELDS and parent != "fixture":
                return True
            if _contains_secret(item, parent=lowered):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item, parent=parent) for item in value)
    return False


def validate_source_pack(
    manifest: Mapping[str, Any],
    *,
    supported_connectors: set[str] | frozenset[str] = SUPPORTED_CONNECTORS,
) -> dict[str, Any]:
    """Validate, expand defaults, and content-address a source pack."""

    value = json.loads(json.dumps(manifest))
    required = {"pack_id", "version", "description", "domains", "sources"}
    if required - set(value):
        raise SourcePackError("invalid_manifest", "source-pack manifest is incomplete")
    if not str(value["pack_id"]).strip() or not str(value["description"]).strip():
        raise SourcePackError("invalid_manifest", "source-pack identity and description are required")
    _version(str(value["version"]))
    domains = value["domains"]
    if not isinstance(domains, list) or not domains or len(set(domains)) != len(domains):
        raise SourcePackError("invalid_domain", "source-pack domains must be a non-empty unique list")
    sources = value["sources"]
    if not isinstance(sources, list) or not sources:
        raise SourcePackError("invalid_source", "source pack requires at least one source")
    defaults = dict(value.get("defaults") or {})
    expanded = [_deep_merge(defaults, source) for source in sources]
    source_ids = [str(source.get("source_id", "")) for source in expanded]
    if any(not source_id for source_id in source_ids) or len(source_ids) != len(set(source_ids)):
        raise SourcePackError("duplicate_source", "source identifiers must be unique and non-empty")
    normalized = []
    for source in expanded:
        source_id = str(source["source_id"])
        connector = str(source.get("connector", ""))
        if connector not in supported_connectors:
            raise SourcePackError("unknown_connector", f"source {source_id!r} uses unknown connector {connector!r}")
        _validate_endpoint(str(source.get("endpoint", "")), source_id)
        for field in ("publisher", "scope", "update_cadence", "temporal_semantics"):
            if not str(source.get(field, "")).strip():
                raise SourcePackError("invalid_source", f"source {source_id!r} is missing {field}")
        license_policy = dict(source.get("license") or {})
        if not all(license_policy.get(field) for field in ("id", "terms_url", "redistribution")):
            raise SourcePackError("missing_terms", f"source {source_id!r} needs license and redistribution policy")
        _validate_endpoint(str(license_policy["terms_url"]), source_id + ":terms")
        mapping = dict(source.get("mapping") or {})
        if not mapping.get("target_schema") or not mapping.get("version"):
            raise SourcePackError("invalid_mapping", f"source {source_id!r} needs a versioned mapping")
        if not source.get("extractor_versions"):
            raise SourcePackError("invalid_mapping", f"source {source_id!r} needs pinned extractor versions")
        auth = dict(source.get("auth") or {})
        if auth.get("kind") not in AUTH_KINDS:
            raise SourcePackError("invalid_auth", f"source {source_id!r} has invalid auth policy")
        if auth["kind"] != "none" and not str(auth.get("secret_ref", "")).startswith("NOESIS_"):
            raise SourcePackError("invalid_auth", f"source {source_id!r} requires a NOESIS_ secret reference")
        if auth["kind"] == "none" and set(auth) - {"kind"}:
            raise SourcePackError("invalid_auth", f"source {source_id!r} declares secret data for unauthenticated access")
        budgets = dict(source.get("budgets") or {})
        for field, ceiling in _BUDGET_CEILINGS.items():
            try:
                amount = int(budgets[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePackError("unbounded_source", f"source {source_id!r} needs {field}") from exc
            if amount < 1 or amount > ceiling:
                raise SourcePackError("unbounded_source", f"source {source_id!r} has unsafe {field}")
            budgets[field] = amount
        fixture = dict(source.get("fixture") or {})
        if not fixture.get("path") or not _SHA256.fullmatch(str(fixture.get("sha256", ""))):
            raise SourcePackError("unpinned_fixture", f"source {source_id!r} needs a pinned fixture")
        if not _SHA256.fullmatch(str(fixture.get("expected_output_hash", ""))):
            raise SourcePackError("unpinned_fixture", f"source {source_id!r} needs expected normalized output")
        if not source.get("operations"):
            raise SourcePackError("invalid_source", f"source {source_id!r} needs declared operations")
        if _contains_secret(source):
            raise SourcePackError("embedded_secret", f"source {source_id!r} embeds credential material")
        source["budgets"] = budgets
        source["operations"] = sorted(set(source["operations"]))
        source["extractor_versions"] = sorted(set(source["extractor_versions"]))
        source["source_hash"] = _digest(source)
        normalized.append(source)
    result = {
        "contract": SOURCE_PACK_CONTRACT,
        "pack_id": value["pack_id"],
        "version": value["version"],
        "description": value["description"],
        "domains": sorted(domains),
        "sources": sorted(normalized, key=lambda item: item["source_id"]),
    }
    result["manifest_hash"] = _digest(result)
    return result


def load_source_packs(root: Path) -> list[dict[str, Any]]:
    return [
        validate_source_pack(json.loads(path.read_text()))
        for path in sorted(root.glob("*.json"))
    ]


class SourcePackStore:
    """Install and activate immutable pack versions with an audit trail."""

    def __init__(self, conn: Any, *, initialize: bool = True) -> None:
        self.conn = conn
        if initialize:
            conn.execute(_DDL)

    def _audit(self, pack_id: str, principal_id: str, action: str, detail: Mapping[str, Any], now_ms: int) -> None:
        identity = [pack_id, principal_id, action, detail, now_ms]
        self.conn.execute(
            "INSERT INTO source_pack_audit VALUES (?,?,?,?,?,?)",
            ["source-pack-audit:" + _digest(identity)[:24], pack_id, principal_id, action, _canonical(detail), now_ms],
        )

    def install(
        self,
        manifest: Mapping[str, Any],
        *,
        principal_id: str,
        enable: bool = False,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        value = validate_source_pack(manifest)
        now = now_ms or _now()
        prior = self.conn.execute(
            "SELECT manifest_hash FROM source_pack_versions WHERE pack_id=? AND version=?",
            [value["pack_id"], value["version"]],
        ).fetchone()
        if prior:
            if prior[0] != value["manifest_hash"]:
                raise SourcePackError("immutable_version", "installed pack version has different content")
            result = self.status(value["pack_id"])
            result["idempotent"] = True
            return result
        current = self.conn.execute(
            "SELECT version,enabled FROM source_pack_current WHERE pack_id=?", [value["pack_id"]]
        ).fetchone()
        if current and _version(value["version"]) < _version(str(current[0])):
            raise SourcePackError("version_downgrade", "source packs cannot be downgraded in place")
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_pack_versions VALUES (?,?,?,?,?,?)",
                [value["pack_id"], value["version"], value["manifest_hash"], _canonical(value), principal_id, now],
            )
            enabled = bool(enable or current and current[1])
            if current:
                self.conn.execute(
                    "UPDATE source_pack_current SET version=?,enabled=?,updated_at_ms=? WHERE pack_id=?",
                    [value["version"], enabled, now, value["pack_id"]],
                )
            else:
                self.conn.execute(
                    "INSERT INTO source_pack_current VALUES (?,?,?,?)",
                    [value["pack_id"], value["version"], enabled, now],
                )
            self._audit(value["pack_id"], principal_id, "install", {"version": value["version"], "enabled": enabled}, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.status(value["pack_id"])

    def set_enabled(
        self, pack_id: str, enabled: bool, *, principal_id: str, now_ms: int | None = None
    ) -> dict[str, Any]:
        if not self.conn.execute("SELECT 1 FROM source_pack_current WHERE pack_id=?", [pack_id]).fetchone():
            raise SourcePackError("not_found", "source pack is not installed")
        now = now_ms or _now()
        self.conn.execute(
            "UPDATE source_pack_current SET enabled=?,updated_at_ms=? WHERE pack_id=?",
            [bool(enabled), now, pack_id],
        )
        self._audit(pack_id, principal_id, "enable" if enabled else "disable", {}, now)
        return self.status(pack_id)

    def status(
        self,
        pack_id: str,
        *,
        secret_available: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT c.version,c.enabled,c.updated_at_ms,v.manifest_hash,v.manifest_json "
            "FROM source_pack_current c JOIN source_pack_versions v "
            "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?",
            [pack_id],
        ).fetchone()
        if not row:
            raise SourcePackError("not_found", "source pack is not installed")
        manifest = _load(row[4], {})
        sources = []
        for source in manifest["sources"]:
            auth = source["auth"]
            ready = auth["kind"] != "required-secret" or bool(
                secret_available and secret_available(auth["secret_ref"])
            )
            health = self.conn.execute(
                "SELECT checked_at_ms,status,classification,detail_json FROM source_pack_health "
                "WHERE pack_id=? AND source_id=?",
                [pack_id, source["source_id"]],
            ).fetchone()
            sources.append(
                {
                    "source_id": source["source_id"],
                    "connector": source["connector"],
                    "authentication": {"kind": auth["kind"], "secret_ref": auth.get("secret_ref"), "ready": ready},
                    "health": None
                    if health is None
                    else {
                        "checked_at_ms": int(health[0]),
                        "status": health[1],
                        "classification": health[2],
                        "detail": _load(health[3], {}),
                    },
                }
            )
        return {
            "contract": SOURCE_PACK_CONTRACT,
            "pack_id": pack_id,
            "version": row[0],
            "enabled": bool(row[1]),
            "updated_at_ms": int(row[2]),
            "manifest_hash": row[3],
            "domains": manifest["domains"],
            "sources": sources,
        }

    def list(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT pack_id FROM source_pack_current ORDER BY pack_id").fetchall()
        return [self.status(row[0]) for row in rows]

    def record_health(
        self,
        pack_id: str,
        source_id: str,
        *,
        status: str,
        classification: str,
        detail: Mapping[str, Any] | None = None,
        checked_at_ms: int | None = None,
    ) -> dict[str, Any]:
        if status not in {"healthy", "degraded", "unavailable", "omitted"}:
            raise SourcePackError("invalid_health", "unsupported source health status")
        pack = self.status(pack_id)
        if source_id not in {source["source_id"] for source in pack["sources"]}:
            raise SourcePackError("not_found", "source does not belong to the installed pack")
        safe_detail = {
            key: value
            for key, value in dict(detail or {}).items()
            if key
            in {
                "cursor",
                "freshness_lag_s",
                "mapping_failures",
                "quota_remaining",
                "schema_hash",
                "last_receipt",
                "message",
            }
        }
        if _contains_secret(safe_detail):
            raise SourcePackError("embedded_secret", "health output contains credential material")
        now = checked_at_ms or _now()
        self.conn.execute(
            "DELETE FROM source_pack_health WHERE pack_id=? AND source_id=?", [pack_id, source_id]
        )
        self.conn.execute(
            "INSERT INTO source_pack_health VALUES (?,?,?,?,?,?)",
            [pack_id, source_id, now, status, classification, _canonical(safe_detail)],
        )
        return {"pack_id": pack_id, "source_id": source_id, "status": status, "classification": classification, "checked_at_ms": now, "detail": safe_detail}

    def coverage(self) -> dict[str, Any]:
        packs = self.list()
        by_domain: dict[str, dict[str, int]] = {}
        for pack in packs:
            for domain in pack["domains"]:
                summary = by_domain.setdefault(domain, {"packs": 0, "sources": 0, "ready": 0, "healthy": 0})
                summary["packs"] += 1
                summary["sources"] += len(pack["sources"])
                summary["ready"] += sum(source["authentication"]["ready"] for source in pack["sources"])
                summary["healthy"] += sum((source["health"] or {}).get("status") == "healthy" for source in pack["sources"])
        return {"contract": "noesis-source-pack-coverage-v1", "domains": by_domain, "packs": len(packs)}


class SourcePackConformance:
    """Network-free fixture replay plus explicitly enabled bounded probes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _fixture(self, source: Mapping[str, Any]) -> dict[str, Any]:
        path = (self.root / source["fixture"]["path"]).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise SourcePackError("unsafe_fixture", "fixture escapes the repository root") from exc
        if not path.is_file():
            raise SourcePackError("fixture_missing", f"fixture does not exist: {path.name}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["fixture"]["sha256"]:
            raise SourcePackError("fixture_drift", f"fixture hash changed for {source['source_id']}")
        return json.loads(raw)

    def offline(
        self,
        manifest: Mapping[str, Any],
        *,
        runners: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[Mapping[str, Any]]]] | None = None,
    ) -> dict[str, Any]:
        pack = validate_source_pack(manifest)
        results = []
        for source in pack["sources"]:
            fixture = self._fixture(source)
            runner = (runners or {}).get(source["connector"])
            normalized = list(runner(source, fixture) if runner else fixture.get("normalized") or [])
            output_hash = _digest(normalized)
            valid = output_hash == source["fixture"]["expected_output_hash"]
            results.append(
                {
                    "source_id": source["source_id"],
                    "connector": source["connector"],
                    "fixture_hash": source["fixture"]["sha256"],
                    "output_hash": output_hash,
                    "valid": valid,
                    "records": len(normalized),
                    "scenarios": sorted(fixture.get("scenarios") or []),
                }
            )
        return {
            "contract": CONFORMANCE_CONTRACT,
            "pack_id": pack["pack_id"],
            "version": pack["version"],
            "offline": True,
            "valid": all(item["valid"] for item in results),
            "sources": results,
            "coverage": {"configured": len(results), "verified": sum(item["valid"] for item in results)},
        }

    @staticmethod
    def classify(error: BaseException) -> str:
        code = str(getattr(error, "code", "")).lower()
        message = str(error).lower()
        if "auth" in code or "credential" in message or "401" in message or "403" in message:
            return "authentication"
        if "rate" in code or "429" in message:
            return "rate-limiting"
        if "schema" in code or "mapping" in code:
            return "provider-drift"
        if "config" in code:
            return "configuration"
        if "timeout" in code or "unavailable" in code or "timeout" in message:
            return "transient-availability"
        return "noesis-regression"

    def live(
        self,
        manifest: Mapping[str, Any],
        probe: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        enabled: bool = False,
        max_requests: int = 10,
    ) -> dict[str, Any]:
        pack = validate_source_pack(manifest)
        if not enabled:
            return {"contract": CONFORMANCE_CONTRACT, "pack_id": pack["pack_id"], "live": False, "status": "disabled", "requests": 0, "sources": []}
        limit = min(max(1, int(max_requests)), 25)
        results = []
        for source in pack["sources"][:limit]:
            try:
                detail = dict(probe(source))
                safe = {key: detail[key] for key in ("schema_hash", "freshness_lag_s", "quota_remaining", "cursor", "last_receipt") if key in detail}
                results.append({"source_id": source["source_id"], "status": "healthy", "classification": "ok", "detail": safe})
            except Exception as exc:  # noqa: BLE001 - one source cannot hide the others
                results.append({"source_id": source["source_id"], "status": "unavailable", "classification": self.classify(exc), "detail": {"message": str(exc)[:200]}})
        return {"contract": CONFORMANCE_CONTRACT, "pack_id": pack["pack_id"], "live": True, "status": "complete", "requests": len(results), "sources": results}

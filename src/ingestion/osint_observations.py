"""Bounded, receipted acquisition of registry facts for OSINT source vetting.

Shared by the RDAP (``src/ingestion/rdap.py``, OX05 #2045) and certificate
transparency (``src/ingestion/crtsh.py``, OX06 #2046) adapters. It follows the
receipt pattern of :mod:`src.ingestion.wayback`:

* one domain per call, validated (no wildcard, no IP literal, no e-mail, no
  URL), and never enumerated in bulk;
* a caller-chosen ``request_id`` with an input hash: a replay with the same
  inputs returns the stored receipt, a reuse with different inputs is refused;
* bounded bytes and timeout, exactly one GET, no retries;
* the redistribution posture recorded from the source pack ``license``.

Results are stored as ``osint-observation-v1`` records with archive-time
semantics: ``archive_at`` is when Noesis observed the registry, never a claim
about when the underlying fact became true. Raw provider payloads are not
stored; only the parsed, redacted facts and a hash of the raw bytes are, so
natural-person registrant data never persists.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OBSERVATION_CONTRACT = "osint-observation-v1"
SOURCE_PACK_PATH = (
    Path(__file__).resolve().parents[2] / "config/source_packs/osint.json"
)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_DDL = """
CREATE TABLE IF NOT EXISTS osint_observations (
  observation_id TEXT PRIMARY KEY, contract TEXT NOT NULL, source_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL, subject TEXT NOT NULL, archive_at_ms BIGINT NOT NULL,
  request_id TEXT NOT NULL, record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS osint_acquisitions (
  request_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  receipt_json TEXT NOT NULL
);
"""


class ObservationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> int | None:
    """Milliseconds for an ISO-8601 timestamp or date, or None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def normalize_domain(value: Any) -> str:
    """A registrable host name, or :class:`ObservationError`.

    Refuses wildcards, IP literals, e-mail addresses, URLs, ports and handles:
    acquisition is one explicitly named domain per call, never enumeration and
    never a person-keyed or address-keyed lookup.
    """
    raw = str(value or "").strip().lower().rstrip(".")
    if not raw or len(raw) > 253:
        raise ObservationError("invalid_domain", "a domain name is required")
    if "*" in raw or "%" in raw:
        raise ObservationError(
            "wildcard_refused", "wildcard or pattern lookups are not supported"
        )
    if "@" in raw:
        raise ObservationError(
            "person_identifier_refused", "e-mail addresses and handles are not domains"
        )
    try:
        ipaddress.ip_address(raw.strip("[]"))
    except ValueError:
        pass
    else:
        raise ObservationError(
            "ip_lookup_refused", "IP-keyed lookups are not supported"
        )
    if any(ch in raw for ch in "/:?# "):
        raise ObservationError("invalid_domain", "pass a bare domain, not a URL")
    raw = raw.removeprefix("www.")
    labels = raw.split(".")
    if (
        len(labels) < 2
        or not all(_LABEL.match(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ObservationError("invalid_domain", "not a valid domain name")
    return raw


def load_source(source_id: str, path: Path = SOURCE_PACK_PATH) -> dict[str, Any]:
    """One declared source of the OSINT source pack with pack defaults applied."""
    pack = json.loads(Path(path).read_text())
    defaults = pack.get("defaults") or {}
    for source in pack["sources"]:
        if source["source_id"] == source_id:
            merged = {**defaults, **source}
            merged["budgets"] = {
                **(defaults.get("budgets") or {}),
                **(source.get("budgets") or {}),
            }
            return merged
    raise ObservationError(
        "undeclared_source", f"{source_id!r} is not declared in the OSINT source pack"
    )


def _ensure(conn) -> None:
    for statement in _DDL.strip().split(";"):
        if statement.strip():
            conn.execute(statement)


def acquire(
    conn,
    *,
    source_id: str,
    domain: str,
    request_id: str,
    url: str,
    params: Mapping[str, Any],
    parse: Callable[[bytes, str], dict[str, Any]],
    adapter_version: str,
    transport: Callable[..., Mapping[str, Any]] | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    resolve_url: Callable[[Callable[..., Mapping[str, Any]]], str] | None = None,
) -> dict[str, Any]:
    """Acquire one registry observation for one domain, with a durable receipt.

    ``parse(raw_bytes, domain)`` turns the provider payload into redacted facts;
    only those facts are stored. Exactly one provider request; no retries.
    ``resolve_url(transport)`` optionally resolves the authoritative endpoint
    (e.g. RDAP through the IANA bootstrap) inside the receipted block, so a
    resolution failure is receipted like any other failure; ``url`` then names
    the declared logical endpoint used for the request key.
    """
    source = load_source(source_id)
    budgets = source["budgets"]
    max_bytes = int(max_bytes or budgets["max_bytes"])
    timeout_s = float(timeout_s or budgets["timeout_ms"] / 1000)
    if (
        not request_id
        or not 1 <= max_bytes <= int(budgets["max_bytes"])
        or not 0 < timeout_s <= 60
    ):
        raise ObservationError("invalid_controls", "invalid acquisition controls")
    domain = normalize_domain(domain)
    _ensure(conn)
    key = _digest(
        [source_id, domain, url, dict(params), max_bytes, timeout_s, adapter_version]
    )
    prior = conn.execute(
        "SELECT input_hash, receipt_json FROM osint_acquisitions WHERE request_id=?",
        [request_id],
    ).fetchone()
    if prior:
        if prior[0] != key:
            raise ObservationError(
                "request_id_reused", "request ID already used with different inputs"
            )
        return json.loads(prior[1])
    clock = now or (lambda: int(time.time() * 1000))
    started = clock()
    license_ = dict(source.get("license") or {})
    receipt: dict[str, Any] = {
        "request_id": request_id,
        "input_hash": key,
        "source_id": source_id,
        "domain": domain,
        "endpoint": url,
        "retrieved_at_ms": started,
        "adapter_version": adapter_version,
        "max_bytes": max_bytes,
        "timeout_s": timeout_s,
        "retries": 0,
        "redistribution": license_.get("redistribution"),
        "license": license_,
    }
    if transport is None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        transport = HTTPSPageAdapter._request
    try:
        target = url
        if resolve_url is not None:
            target = resolve_url(transport)
            receipt["resolved_endpoint"] = target
        response = transport(
            url=target,
            params=dict(params),
            headers={"Accept": "application/json"},
            timeout=timeout_s,
            max_bytes=max_bytes,
        )
        status = int(response.get("status", 200))
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(raw) > max_bytes:
            raise ObservationError(
                "response_too_large", "provider response exceeds its byte budget"
            )
        if status == 404:
            receipt["status"] = "not_found"
        elif status != 200:
            raise ObservationError(
                "provider_unavailable", f"provider returned HTTP {status}"
            )
        else:
            facts = parse(raw, domain)
            observation_id = (
                "osint-obs:"
                + _digest([source_id, domain, started, _digest(facts)])[:28]
            )
            record = {
                "contract": OBSERVATION_CONTRACT,
                "observation_id": observation_id,
                "source_id": source_id,
                "publisher": source.get("publisher"),
                "subject": {"kind": "domain", "value": domain},
                "observed_at": iso(started),
                "archive_at": iso(started),
                "archive_at_ms": started,
                "temporal_semantics": "fetch-and-archive-observation",
                "locator": response.get("final_url") or target,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_stored": False,
                "license": license_,
                "redistribution": license_.get("redistribution"),
                "request_id": request_id,
                "facts": facts,
            }
            conn.execute(
                "INSERT INTO osint_observations VALUES (?,?,?,?,?,?,?,?)",
                [
                    observation_id,
                    OBSERVATION_CONTRACT,
                    source_id,
                    "domain",
                    domain,
                    started,
                    request_id,
                    _canonical(record),
                ],
            )
            receipt.update(
                status="acquired", observation_id=observation_id, bytes=len(raw)
            )
    except Exception as exc:  # noqa: BLE001 - persist bounded failure diagnostics, never retry
        receipt.update(
            status="failed", failure_type=getattr(exc, "code", type(exc).__name__)
        )
    conn.execute(
        "INSERT INTO osint_acquisitions VALUES (?,?,?,?)",
        [request_id, source_id, key, _canonical(receipt)],
    )
    return receipt


def get_observation(conn, observation_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT record_json FROM osint_observations WHERE observation_id=?",
            [observation_id],
        ).fetchone()
    except Exception:  # noqa: BLE001 - table absent on a read-only warehouse
        return None
    return json.loads(row[0]) if row else None


def observations_for(
    conn, domain: str, source_id: str | None = None
) -> list[dict[str, Any]]:
    """Every stored observation for a domain, oldest first."""
    try:
        rows = conn.execute(
            "SELECT record_json FROM osint_observations WHERE subject=? AND (? IS NULL OR source_id=?) "
            "ORDER BY archive_at_ms, observation_id",
            [normalize_domain(domain), source_id, source_id],
        ).fetchall()
    except ObservationError:
        raise
    except Exception:  # noqa: BLE001
        return []
    return [json.loads(r[0]) for r in rows]


def record_in_investigation(
    conn, investigation: str, receipt: Mapping[str, Any]
) -> int | None:
    """Log an acquisition receipt into an investigation's provisioning audit
    trail, so ``investigation_audit`` replays it. None when no such KG exists."""
    from src.provisioning import store

    if not store.schema_ready(conn) or store.get_kg(conn, investigation) is None:
        return None
    detail = {
        k: receipt.get(k)
        for k in (
            "request_id",
            "source_id",
            "domain",
            "status",
            "observation_id",
            "redistribution",
            "retrieved_at_ms",
        )
    }
    return store.record_event(
        conn, investigation, "osint-observation", detail, datetime.now(UTC)
    )


# --------------------------------------------------------------------------- #
# Source-identity lookups shared by the projections                            #
# --------------------------------------------------------------------------- #


def source_domains(conn, namespace: str) -> dict[str, list[str]]:
    """``domain -> [source_id]`` for every source identity in *namespace*
    linked to a domain, by a current ``domain`` alias decision or a ``domain``
    native id. This is the bounded set acquisition and projection may touch."""
    out: dict[str, set[str]] = {}
    try:
        rows = conn.execute(
            "SELECT d.normalized_alias, d.source_id FROM source_alias_decisions d "
            "WHERE d.namespace=? AND d.alias_type='domain' AND d.action='link' "
            "AND NOT EXISTS (SELECT 1 FROM source_alias_decisions child "
            "WHERE child.predecessor_decision_id=d.decision_id)",
            [namespace],
        ).fetchall()
        for domain, source_id in rows:
            out.setdefault(domain, set()).add(source_id)
        rows = conn.execute(
            "SELECT r.source_id, r.native_ids_json FROM source_identity_current c "
            "JOIN source_identity_revisions r USING(revision_id) WHERE r.namespace=? AND r.lifecycle='active'",
            [namespace],
        ).fetchall()
    except Exception:  # noqa: BLE001 - no source identities yet
        return {k: sorted(v) for k, v in out.items()}
    for source_id, native in rows:
        domain = (json.loads(native or "{}") or {}).get("domain")
        if domain:
            try:
                out.setdefault(normalize_domain(domain), set()).add(source_id)
            except ObservationError:
                continue
    return {k: sorted(v) for k, v in sorted(out.items())}


def domains_of_source(conn, namespace: str, source_id: str) -> list[str]:
    return sorted(
        d for d, ids in source_domains(conn, namespace).items() if source_id in ids
    )


def observation_evidence(record: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """The citation an observation contributes to a source-identity revision."""
    return {
        "kind": "osint-observation",
        "contract": OBSERVATION_CONTRACT,
        "observation_id": record["observation_id"],
        "source_id": record["source_id"],
        "locator": record.get("locator"),
        "archive_at": record.get("archive_at"),
        "redistribution": record.get("redistribution"),
        **extra,
    }


def revise_identity_native_ids(
    store,
    namespace: str,
    source_id: str,
    updates: Mapping[str, str],
    *,
    principal_id: str,
    scopes: set[str],
    citation_key: str | None = None,
) -> dict[str, Any]:
    """Append an identity revision carrying observation-derived native ids.

    The revision history of ``source_identity`` is the registration/issuance
    history. Existing native ids (including the source's own ``domain``) are
    kept; ``updates`` only add or change the observation-derived keys, which
    callers namespace per domain. ``citation_key`` names the key holding the
    observation id: it is not substantive, so an observation whose substantive
    values equal the current ones is idempotent (no new revision) and the
    revision keeps citing the observation that first stated those values.
    """
    from src.kb.source_identity import READ_SCOPE

    prior = store.get(namespace, source_id, scopes={READ_SCOPE})
    current = prior["native_ids"]
    values = {k: str(v) for k, v in updates.items() if v is not None}
    substantive = {k: v for k, v in values.items() if k != citation_key}
    if citation_key in current and all(
        current.get(k) == v for k, v in substantive.items()
    ):
        return {**prior, "idempotent": True}
    native = {**current, **values}
    return store.revise(
        namespace,
        source_id,
        prior["revision"],
        principal_id=principal_id,
        scopes=scopes,
        native_ids=native,
    )

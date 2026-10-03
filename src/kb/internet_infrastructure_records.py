"""Network registry records and routing observations for the Technology ``technology.internet-infrastructure``
provider (#2743, II02).

One record contract, ``noesis-internet-infrastructure-record-v2`` (the wave's record contract line is 2.x), covers the
two record shapes the provider owns (``packs/taxonomy.json``; no new shape):

* **registry-records** - ASNs and prefixes as RDAP registers them, PeeringDB networks, organisations, IX presence,
  IXs and facilities (the network's self-declaration), certificates as crt.sh states them and CT logs as the CT log
  list states them. Each object is keyed by *its own source*: the same ASN seen by RIPEstat, PeeringDB and RDAP is
  three objects, never one merged record. Each change is an appended **revision** carrying the source, the revision
  the source states (PeeringDB ``updated``, RDAP ``last changed``, the CT log list version, the crt.sh entry time, or
  else the content digest) and the as-of time it is valid from. A 404 or ``status=deleted`` for a declared object, or
  its absence from a complete later listing, is a ``removed_by_source`` revision, never a deletion;
* **observations** - RIPEstat answers, each dated by RIPEstat's stated time (``query_time`` or ``latest_time``) and
  data-call version, appended and never rewritten.

**Minimisation decision (II01).** :func:`check_item` refuses at write time: any RDAP entity or vCard (an entity with
vCard ``kind`` ``individual`` is never persisted, and no entity is stored at all - only the holder organisation name),
every e-mail, telephone, fax and postal-address property, PeeringDB ``poc`` objects and user accounts, the RIPEstat
``whois`` and ``abuse-contact-finder`` calls, certificate subject fields beyond the organisation (``O``), e-mail SANs and
revocation statements (never inferred), exposed-service, port and banner data, reputation, risk, hijack and
misconfiguration verdicts, network rankings, and any object keyed by an IP address alone.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.internet_infrastructure_sources import (
    CERTIFICATE_FIELDS,
    EXCLUSIONS,
    FORBIDDEN_CALLS,
    LIVE_VERIFICATION,
    MINIMISATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    RIPESTAT_CALLS,
)

CONTRACT = "noesis-internet-infrastructure-record-v2"
ANSWER_CONTRACT = "noesis-internet-infrastructure-answer-v1"
READ_SCOPE = "knowledge:technical:internet-infrastructure:read"
WRITE_SCOPE = "knowledge:technical:internet-infrastructure:write"
REVIEW_SCOPE = "knowledge:technical:internet-infrastructure:review"
DEFAULT_NAMESPACE = "global"
PROVIDER_ID = "technology.internet-infrastructure"
BUNDLE = "technology"
FEATURES = ("internet-infrastructure-ripestat", "internet-infrastructure-peeringdb", "internet-infrastructure-rdap",
            "internet-infrastructure-ct")
FEATURE_PROVIDERS = {"internet-infrastructure-ripestat": ("ripestat",),
                     "internet-infrastructure-peeringdb": ("peeringdb",),
                     "internet-infrastructure-rdap": ("rdap",),
                     "internet-infrastructure-ct": ("crtsh", "ct-log-list")}
SHAPES = {"registry-record": "registry-records", "observation": "observations"}
RECORD_TYPES = ("registry-record", "observation", "listing")
STATES = ("published", "removed_by_source")
OBJECT_KINDS = {
    "ripestat": ("routing-observation",),
    "peeringdb": ("net", "org", "netixlan", "ix", "fac"),
    "rdap": ("autnum", "ip-network", "domain"),
    "crtsh": ("certificate",),
    "ct-log-list": ("ct-log",),
}
RESOURCE_KINDS = ("asn", "prefix", "domain", "ct-log-list")
REVISION_BASES = ("source_updated", "last_changed", "log_list_version", "entry_timestamp", "content_digest",
                  "not_found", "absent_from_complete_listing")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person or a contact (RDAP vCards and entities, PeeringDB poc and users).
PERSONAL_DATA_FIELDS = frozenset({
    "email", "e-mail", "emails", "tel", "phone", "telephone", "fax", "adr", "address", "address1", "address2",
    "postal_address", "street", "zipcode", "postal_code", "vcardarray", "vcard", "entities", "entity", "contact",
    "contacts", "poc", "pocs", "poc_set", "user", "users", "person", "persons", "registrant_name", "admin_c",
    "tech_c", "abuse_c", "abuse_mailbox", "abuse_contact", "tech_email", "tech_phone", "policy_email",
    "policy_phone", "sales_email", "sales_phone", "notes", "full_name", "first_name", "last_name",
})
# Certificate subject fields beyond the organisation, e-mail SANs and revocation (never inferred).
CERTIFICATE_FORBIDDEN = frozenset({
    "subject", "subject_dn", "common_name", "subject_cn", "name_value", "email_san", "email_sans", "revoked",
    "revocation", "revocation_status", "ocsp_status", "crl_status",
})
# Keys that would carry a verdict, a ranking or exposed-service data.
FORBIDDEN_KEYS = frozenset({
    "risk", "risk_score", "reputation", "reputation_score", "hijack", "hijacked", "hijack_verdict",
    "misconfiguration", "misconfigured", "verdict", "rank", "ranking", "network_rank", "as_rank", "malicious",
    "abuse_score", "threat", "threat_score", "port", "ports", "open_ports", "banner", "banners", "services",
    "exposed_services", "subdomains", "hosted_domains", "reverse_dns", "co_hosted",
})


class InfrastructureRecordError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def to_ms(value: Any) -> int | None:
    """ISO date/time (or epoch ms) to epoch milliseconds; naive values are UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    raw = str(value).strip().replace("Z", "+00:00")
    if len(raw) == 10:
        raw += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise InfrastructureRecordError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def _paths(value: Any, names: frozenset[str], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}"
            if str(key).casefold() in names:
                found.append(here)
            found += _paths(item, names, here)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += _paths(item, names, f"{path}[{index}]")
    return found


def _individuals(value: Any, path: str = "$") -> list[str]:
    """Paths of anything that is, or carries, a natural-person vCard (``kind`` ``individual`` or a jCard array)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        if str(value.get("kind", "")).casefold() == "individual":
            found.append(path)
        for key, item in value.items():
            found += _individuals(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        if value and value[0] == "vcard":
            found.append(path)
        if len(value) >= 4 and str(value[0]).casefold() == "kind" and str(value[3]).casefold() == "individual":
            found.append(path)
        for index, item in enumerate(value):
            found += _individuals(item, f"{path}[{index}]")
    return found


def personal_data_paths(value: Any) -> list[str]:
    return sorted(set(_paths(value, PERSONAL_DATA_FIELDS) + _individuals(value)))


def forbidden_paths(value: Any) -> list[str]:
    return _paths(value, FORBIDDEN_KEYS)


def check_resource(resource: Mapping[str, Any]) -> None:
    kind, value = resource.get("kind"), str(resource.get("value") or "")
    if kind not in RESOURCE_KINDS or not value:
        raise InfrastructureRecordError("invalid_record", f"a record names its declared resource ({RESOURCE_KINDS})")
    if kind == "prefix":
        network = ipaddress.ip_network(value, strict=False)
        if network.prefixlen == network.max_prefixlen:
            raise InfrastructureRecordError("ip_keyed", "no record is keyed by an IP address alone")


def check_item(item: Mapping[str, Any]) -> None:
    """Validate one item before anything is written (II02 rules and the II01 minimisation decision)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise InfrastructureRecordError("personal_data", "records never carry person or contact data (RDAP "
                                                         "individuals, vCards, e-mail, telephone, postal address, "
                                                         "PeeringDB poc)", fields=leaked)
    verdicts = forbidden_paths(dict(item))
    if verdicts:
        raise InfrastructureRecordError("excluded_field", "records carry no verdict, ranking, port, banner or "
                                                          "exposed-service data", fields=verdicts)
    record_type, provider = item.get("record_type"), item.get("provider")
    if record_type not in RECORD_TYPES:
        raise InfrastructureRecordError("invalid_record", f"record_type is one of {RECORD_TYPES}")
    if provider not in PROVIDERS:
        raise InfrastructureRecordError("invalid_record", f"provider is one of {PROVIDERS}")
    if item.get("object_kind") not in OBJECT_KINDS[provider]:
        raise InfrastructureRecordError("invalid_record", f"{provider} object kinds are {OBJECT_KINDS[provider]}")
    check_resource(dict(item.get("resource") or {}))
    if record_type == "listing":
        if not item.get("listing_key") or not isinstance(item.get("members"), list) or not item.get("complete"):
            raise InfrastructureRecordError("invalid_record", "a listing is complete and names its members")
        return
    if not str(item.get("native_id") or ""):
        raise InfrastructureRecordError("invalid_record", "a record states its native id")
    if record_type == "observation":
        if provider != "ripestat":
            raise InfrastructureRecordError("invalid_record", "observations are RIPEstat answers")
        call = item.get("data_call")
        if call in FORBIDDEN_CALLS or call not in RIPESTAT_CALLS:
            raise InfrastructureRecordError("call_forbidden", f"the RIPEstat {call} call is never stored")
        if not item.get("stated_time") or not item.get("data_call_version"):
            raise InfrastructureRecordError("invalid_record", "an observation is dated by RIPEstat's stated time "
                                                              "and data-call version")
        return
    if provider == "ripestat":
        raise InfrastructureRecordError("invalid_record", "RIPEstat answers are observations, never registry records")
    if item.get("state") not in STATES:
        raise InfrastructureRecordError("invalid_record", f"state is one of {STATES}")
    revision = dict(item.get("revision") or {})
    if revision.get("basis") not in REVISION_BASES:
        raise InfrastructureRecordError("invalid_record", f"a revision states its basis ({REVISION_BASES})")
    if provider == "crtsh":
        content = dict(item.get("content") or {})
        extra = set(content) - set(CERTIFICATE_FIELDS)
        refused = _paths(content, CERTIFICATE_FORBIDDEN)
        if extra or refused:
            raise InfrastructureRecordError("certificate_minimisation", "certificates keep crt.sh id, issuer DN, "
                                                                        "validity, DNS SANs, log ids and the subject "
                                                                        "organisation only",
                                            fields=sorted(extra) + refused)
        if any("@" in str(name) for name in content.get("dns_sans") or []):
            raise InfrastructureRecordError("certificate_minimisation", "e-mail SANs are never stored")


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def selected_features(conn: Any) -> list[str]:
    """The Technology bundle's ``internet-infrastructure-*`` features selected in the active plan."""
    return [f for f in _selected_features(conn) if f in FEATURES]


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    selected = selected_features(conn)
    return bool(selected) if feature is None else feature in selected


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "ii_revisions") and table_exists(conn, "ii_observations")
    providers = {}
    for provider in PROVIDERS:
        contract = PROVIDER_CONTRACTS[provider]
        units, state = 0, {"stale": True, "reason": "never acquired"}
        if ready:
            units = int(conn.execute("SELECT count(*) FROM ii_units WHERE provider=?", [provider]).fetchone()[0])
        if table_exists(conn, "ii_receipts"):
            rows = conn.execute("SELECT outcome FROM ii_receipts WHERE provider=? ORDER BY created_at_ms, receipt_id",
                                [provider]).fetchall()
            if [r for r in rows if r[0] in {"applied", "unchanged"}]:
                state = {"stale": rows[-1][0] == "failed",
                         "reason": "last run failed" if rows[-1][0] == "failed" else None}
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["status"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "units": units, **state}
    return {
        "provider": PROVIDER_ID,
        "features": list(FEATURES),
        "selected": selected_features(conn),
        "stores_ready": ready,
        "providers": providers,
        "not_implemented": {k: PROVIDER_CONTRACTS[k]["reason"] for k in ("rfc6962-logs", "caida")},
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "minimisation": MINIMISATION["decision"],
        "note": "offline fixture evidence and live evidence are reported per unit (evidence_origin); no provider is "
                "live until a dated run verifies it",
    }


def schema_definitions(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or Path(__file__).resolve().parents[2]
    return {CONTRACT: json.loads((base / SCHEMA_FILE).read_text())}


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "EXCLUSIONS",
    "FEATURES",
    "FEATURE_PROVIDERS",
    "MINIMISATION",
    "OBJECT_KINDS",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "SHAPES",
    "WRITE_SCOPE",
    "InfrastructureRecordError",
    "authorize",
    "canonical",
    "check_item",
    "digest",
    "feature_enabled",
    "forbidden_paths",
    "iso",
    "personal_data_paths",
    "readiness",
    "schema_definitions",
    "selected_features",
    "table_exists",
    "to_ms",
]

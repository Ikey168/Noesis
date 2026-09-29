"""RDAP domain-registration acquisition and source-identity projection (OX05, #2045).

Consumes the ``rdap-domain`` source declared in ``config/source_packs/osint.json``
(endpoint ``https://data.iana.org/rdap/dns.json``, the IANA RDAP bootstrap
registry, RFC 9224); each lookup goes to the registry server it names. One
domain per call, through the bounded, receipted path in
:mod:`src.ingestion.osint_observations`; the response is parsed per RFC 9083
(``ldhName``, ``status``, ``events``, ``nameservers``, ``entities`` with jCard
``vcardArray``).

Natural-person data is dropped before storage (``deny_person_identification``
in the pack policy, enforced here in code):

* an entity whose vCard ``kind`` is ``individual`` is never persisted, whatever
  its role;
* for a registrant, only an organization name is kept (the vCard ``org``
  property, or ``fn`` when ``kind`` is ``org``); names of unknown kind, e-mail,
  telephone and postal address fields are never read into the stored facts;
* administrative, technical, billing and abuse contacts are dropped entirely.

Projection (:func:`project_rdap`) writes, through the
:class:`src.kb.source_identity.SourceIdentityStore` record owner:

* registration events and registrar/status/nameserver facts as a new
  ``source_identity`` revision of the source that owns the domain, citing the
  observation id;
* the registrant organization as an ``organization`` source identity and an
  ``ownership`` relationship revision (organization -> source) whose evidence is
  the RDAP observation. Existing relationships are never retracted: a
  conflicting ownership statement from another source stays side by side.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from src.ingestion import osint_observations as obs

SOURCE_ID = "rdap-domain"
ADAPTER_VERSION = "rdap-domain-v2"
# RFC 9224 bootstrap: the authoritative RDAP base URL per TLD. The source pack
# endpoint is this registry file; the per-domain request goes straight to the
# registry's server it names, so no cross-host redirect is ever followed.
BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_MAX_BYTES = 1_000_000
BOOTSTRAP_TTL_MS = 24 * 3600 * 1000
_BOOTSTRAP_CACHE: dict[str, Any] = {}


def parse_bootstrap(raw: bytes) -> dict[str, str]:
    """``tld -> https base URL`` from an IANA RDAP DNS bootstrap file."""
    payload = json.loads(raw)
    services = payload.get("services") if isinstance(payload, Mapping) else None
    if not isinstance(services, list):
        raise obs.ObservationError("invalid_bootstrap", "not an RDAP bootstrap file")
    table: dict[str, str] = {}
    for entry in services:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        tlds, urls = entry
        https = [str(u) for u in urls or [] if str(u).lower().startswith("https://")]
        if not https:
            continue  # never downgrade to plain HTTP
        base = https[0] if https[0].endswith("/") else https[0] + "/"
        for tld in tlds or []:
            table.setdefault(str(tld).lower().strip("."), base)
    return table


def bootstrap_table(
    transport: Callable[..., Mapping[str, Any]],
    *,
    now_ms: int,
    timeout_s: float,
    cache: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The bootstrap table, fetched once per TTL (one bounded GET, no retries)."""
    cache = _BOOTSTRAP_CACHE if cache is None else cache
    if (
        cache.get("table")
        and now_ms - int(cache.get("fetched_at_ms", 0)) < BOOTSTRAP_TTL_MS
    ):
        return cache["table"]
    response = transport(
        url=BOOTSTRAP_URL,
        params={},
        headers={"Accept": "application/json"},
        timeout=timeout_s,
        max_bytes=BOOTSTRAP_MAX_BYTES,
    )
    raw = response.get("content", b"")
    raw = raw.encode() if isinstance(raw, str) else bytes(raw)
    if int(response.get("status", 200)) != 200 or len(raw) > BOOTSTRAP_MAX_BYTES:
        raise obs.ObservationError(
            "bootstrap_unavailable", "IANA RDAP bootstrap unavailable"
        )
    table = parse_bootstrap(raw)
    cache.update(table=table, fetched_at_ms=now_ms)
    return table


def rdap_url(domain: str, table: Mapping[str, str]) -> str:
    """The authoritative RDAP domain URL, by longest matching label suffix."""
    labels = domain.split(".")
    for start in range(1, len(labels)):
        suffix = ".".join(labels[start:])
        if suffix in table:
            return f"{table[suffix]}domain/{domain}"
    raise obs.ObservationError(
        "no_rdap_service", "the IANA bootstrap names no RDAP server for this TLD"
    )


REGISTRANT_CAVEAT = (
    "registrant organization as stated in RDAP at archive time; registration is not proof of editorial "
    "ownership or control"
)
_REDACTED = (
    "redacted",
    "withheld",
    "privacy",
    "not disclosed",
    "data protected",
    "gdpr masked",
)
_DROPPED_ROLES = (
    "administrative",
    "technical",
    "billing",
    "abuse",
    "noc",
    "reseller",
    "sponsor",
    "proxy",
)


def _vcard(entity: Mapping[str, Any]) -> dict[str, list[Any]]:
    card: dict[str, list[Any]] = {}
    array = entity.get("vcardArray")
    if not (isinstance(array, list) and len(array) == 2 and isinstance(array[1], list)):
        return card
    for prop in array[1]:
        if isinstance(prop, list) and len(prop) >= 4:
            card.setdefault(str(prop[0]).lower(), []).append(prop[3])
    return card


def _text(value: Any) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    text = " ".join(str(value or "").split())
    if not text or any(marker in text.lower() for marker in _REDACTED):
        return None
    return text


def _organization(entity: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(organization name, withheld reason) for a registrant entity."""
    card = _vcard(entity)
    kinds = {str(k).lower() for k in card.get("kind", [])}
    if "individual" in kinds:
        return None, "natural-person registrant dropped"
    org = next((t for t in (_text(v) for v in card.get("org", [])) if t), None)
    if org:
        return org, None
    if "org" in kinds:
        name = next((t for t in (_text(v) for v in card.get("fn", [])) if t), None)
        if name:
            return name, None
    return None, "registrant is not an identified organization; nothing kept"


def _walk(entities: Any):
    for entity in entities or []:
        if isinstance(entity, Mapping):
            yield entity
            yield from _walk(entity.get("entities"))


def parse_rdap_domain(raw: bytes, domain: str) -> dict[str, Any]:
    """Redacted registration facts from an RDAP domain response."""
    payload = json.loads(raw)
    if not isinstance(payload, Mapping) or payload.get("objectClassName") not in (
        None,
        "domain",
    ):
        raise obs.ObservationError("invalid_response", "not an RDAP domain object")
    name = str(payload.get("ldhName") or domain).lower().rstrip(".")
    if obs.normalize_domain(name) != domain:
        raise obs.ObservationError(
            "subject_mismatch", "RDAP response describes a different domain"
        )
    events = sorted(
        (
            {"action": str(e.get("eventAction")), "date": str(e.get("eventDate"))}
            for e in payload.get("events") or []
            if isinstance(e, Mapping)
            and e.get("eventAction")
            and e.get("eventDate")
            and e.get("eventAction") != "last update of RDAP database"
        ),
        key=lambda e: (e["date"], e["action"]),
    )
    registrar = None
    registrant_org, withheld = None, None
    dropped_individuals = 0
    for entity in _walk(payload.get("entities")):
        roles = {str(r).lower() for r in entity.get("roles") or []}
        kinds = {str(k).lower() for k in _vcard(entity).get("kind", [])}
        if "individual" in kinds:
            dropped_individuals += 1
            if "registrant" in roles:
                withheld = "natural-person registrant dropped"
            continue
        if "registrar" in roles and registrar is None:
            card = _vcard(entity)
            iana = next(
                (
                    str(p.get("identifier"))
                    for p in entity.get("publicIds") or []
                    if isinstance(p, Mapping)
                    and "iana" in str(p.get("type", "")).lower()
                ),
                None,
            )
            registrar = {
                "name": next(
                    (
                        t
                        for t in (
                            _text(v) for v in card.get("fn", []) + card.get("org", [])
                        )
                        if t
                    ),
                    None,
                ),
                "iana_id": iana,
            }
        if "registrant" in roles and registrant_org is None:
            registrant_org, reason = _organization(entity)
            withheld = None if registrant_org else (withheld or reason)
        # Every other role (and every contact field) is dropped by construction.
    return {
        "domain": domain,
        "handle": payload.get("handle"),
        "registrar": registrar,
        "registrant_organization": registrant_org,
        "registrant_withheld": withheld,
        "events": events,
        "status": sorted(str(s) for s in payload.get("status") or []),
        "nameservers": sorted(
            str(ns.get("ldhName")).lower().rstrip(".")
            for ns in payload.get("nameservers") or []
            if isinstance(ns, Mapping) and ns.get("ldhName")
        ),
        "redaction": {
            "policy": "deny_person_identification",
            "individual_entities_dropped": dropped_individuals,
            "fields_never_read": [
                "email",
                "tel",
                "adr",
                "contact names of unknown kind",
            ],
        },
    }


def acquire_rdap(
    conn,
    domain: str,
    *,
    request_id: str,
    transport: Callable[..., Mapping[str, Any]] | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
    bootstrap_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Acquire RDAP registration facts for one explicitly named domain.

    The authoritative registry server is resolved from the IANA bootstrap
    (cached for 24 h, one bounded GET when stale); the domain lookup is then a
    single request to that server, so the transport's same-host redirect policy
    holds and nothing depends on a cross-host redirect."""
    source = obs.load_source(SOURCE_ID)
    domain = obs.normalize_domain(domain)
    clock = now or (lambda: int(time.time() * 1000))
    bounded_timeout = float(timeout_s or source["budgets"]["timeout_ms"] / 1000)

    def resolve(active_transport: Callable[..., Mapping[str, Any]]) -> str:
        table = bootstrap_table(
            active_transport,
            now_ms=clock(),
            timeout_s=bounded_timeout,
            cache=bootstrap_cache,
        )
        return rdap_url(domain, table)

    receipt = obs.acquire(
        conn,
        source_id=SOURCE_ID,
        domain=domain,
        request_id=request_id,
        url=f"{source['endpoint']}#{domain}",
        params={},
        parse=parse_rdap_domain,
        adapter_version=ADAPTER_VERSION,
        transport=transport,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
        resolve_url=resolve,
    )
    if investigation:
        obs.record_in_investigation(conn, investigation, receipt)
    return receipt


def acquire_for_source(
    conn, namespace: str, source_id: str, *, request_id: str, **kwargs: Any
) -> dict[str, Any]:
    """Acquire RDAP for the domain(s) of one existing source identity. Bounded:
    only domains already linked to that identity, never a search."""
    domains = obs.domains_of_source(conn, namespace, source_id)
    if not domains:
        return {
            "status": "no_domain",
            "source_identity": source_id,
            "note": "link a domain to the source identity or pass an explicit domain",
        }
    return {
        "source_identity": source_id,
        "receipts": [
            acquire_rdap(conn, d, request_id=f"{request_id}:{d}", **kwargs)
            for d in domains
        ],
    }


def _event(facts: Mapping[str, Any], action: str) -> str | None:
    return next(
        (
            e["date"]
            for e in reversed(facts.get("events") or [])
            if e["action"] == action
        ),
        None,
    )


def project_rdap(
    conn, namespace: str, observation_id: str, *, principal_id: str, scopes: set[str]
) -> dict[str, Any]:
    """Project one RDAP observation into ``source_identity`` revisions."""
    from src.kb.source_identity import READ_SCOPE, SourceIdentityStore

    record = obs.get_observation(conn, observation_id)
    if record is None or record["source_id"] != SOURCE_ID:
        raise obs.ObservationError("not_found", "no RDAP observation with that id")
    facts, domain = record["facts"], record["subject"]["value"]
    store = SourceIdentityStore(conn, now=None)
    owners = obs.source_domains(conn, namespace).get(domain, [])
    if not owners:
        return {
            "status": "no_source_identity",
            "domain": domain,
            "observation_id": observation_id,
            "note": "the observation is stored; no source identity in this namespace carries the domain",
        }
    if len(owners) > 1:
        return {
            "status": "ambiguous",
            "domain": domain,
            "candidates": owners,
            "observation_id": observation_id,
        }
    source_id = owners[0]
    identity = obs.revise_identity_native_ids(
        store,
        namespace,
        source_id,
        {
            f"rdap:{domain}:registrar": (facts.get("registrar") or {}).get("name"),
            f"rdap:{domain}:registrar_iana_id": (facts.get("registrar") or {}).get(
                "iana_id"
            ),
            f"rdap:{domain}:registration": _event(facts, "registration"),
            f"rdap:{domain}:last_changed": _event(facts, "last changed"),
            f"rdap:{domain}:expiration": _event(facts, "expiration"),
            f"rdap:{domain}:status": ",".join(facts.get("status") or []),
            f"rdap:{domain}:nameservers": ",".join(facts.get("nameservers") or []),
            f"rdap:{domain}:observation": observation_id,
        },
        principal_id=principal_id,
        scopes=scopes,
        citation_key=f"rdap:{domain}:observation",
    )
    relationship = None
    org = facts.get("registrant_organization")
    if org:
        owner = store.register(
            namespace,
            "organization",
            org,
            principal_id=principal_id,
            scopes=scopes,
            native_ids={
                "rdap-registrant-organization": " ".join(org.casefold().split())
            },
            observed_at_ms=record["archive_at_ms"],
            producer={"name": "noesis-rdap-projection", "version": ADAPTER_VERSION},
        )
        relationship = store.relate(
            namespace,
            owner["source_id"],
            source_id,
            "ownership",
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=record["archive_at_ms"],
            valid_from_ms=obs.parse_time(_event(facts, "registration")),
            confidence=0.6,
            uncertainty=0.4,
            evidence=[
                obs.observation_evidence(
                    record, fact="registrant_organization", value=org
                )
            ],
            producer={"name": "noesis-rdap-projection", "version": ADAPTER_VERSION},
            policy={
                "relationship": "registry-stated-v1",
                "status": "as-registered",
                "caveat": REGISTRANT_CAVEAT,
            },
        )
    others = [
        r
        for r in store.dossier(namespace, source_id, scopes={READ_SCOPE}, limit=100)[
            "relationships"
        ]
        if r["relationship_type"] == "ownership"
        and r["to_source_id"] == source_id
        and (
            relationship is None
            or r["relationship_id"] != relationship["relationship_id"]
        )
    ]
    return {
        "status": "projected",
        "domain": domain,
        "source_identity": source_id,
        "identity_revision": identity["revision"],
        "identity_revision_id": identity["revision_id"],
        "ownership_relationship": relationship,
        "side_by_side": others,
        "registrant_withheld": facts.get("registrant_withheld"),
        "observation_id": observation_id,
    }

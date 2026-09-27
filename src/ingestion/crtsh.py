"""Certificate transparency (crt.sh) acquisition and projection (OX06, #2046).

Consumes the ``crt-sh`` source declared in ``config/source_packs/osint.json``
(``https://crt.sh/?output=json``, operated by Sectigo). This is registry-fact
acquisition for a declared source's own domain, not subdomain enumeration or
attack-surface mapping:

* one exactly named domain per call (``q=<domain>``); no ``%.`` wildcard
  patterns, no organization or e-mail queries (refused by
  :func:`src.ingestion.osint_observations.normalize_domain`);
* only certificate issuance facts are kept: crt.sh id, issuer, not-before,
  not-after and the DNS SAN set. E-mail SANs (S/MIME certificates name people)
  are dropped before storage; results are capped at the source budget.

Projection (:func:`project_crtsh`) writes, through
:class:`src.kb.source_identity.SourceIdentityStore`:

* the issuance history summary as a new ``source_identity`` revision of the
  source owning the domain, citing the observation;
* for a certificate whose SAN set also covers the domain of *another* source
  identity in the namespace, a ``shared-infrastructure`` relationship revision
  between the two sources: ``status: probable``, the certificate ids as
  evidence, and the caveat that shared hosting and CDNs commonly explain it.
  A certificate covering only one source domain creates no relation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from src.ingestion import osint_observations as obs

SOURCE_ID = "crt-sh"
ADAPTER_VERSION = "crt-sh-json-v1"
SHARED_INFRASTRUCTURE_CAVEAT = (
    "shared certificates are commonly explained by shared hosting, CDNs, and managed certificate "
    "services; this is a probable infrastructure relation, never a same-operator verdict"
)


def _san_names(name_value: Any) -> list[str]:
    names = set()
    for item in str(name_value or "").split("\n"):
        item = item.strip().lower().rstrip(".")
        if not item or "@" in item:  # e-mail SANs name people: never stored
            continue
        base = item[2:] if item.startswith("*.") else item
        try:
            obs.normalize_domain(base)
        except obs.ObservationError:
            continue
        names.add(item)
    return sorted(names)


def parse_crtsh(
    raw: bytes, domain: str, *, max_results: int | None = None
) -> dict[str, Any]:
    """Issuance facts for a domain from a crt.sh JSON response."""
    payload = json.loads(raw or b"[]")
    if not isinstance(payload, list):
        raise obs.ObservationError("invalid_response", "crt.sh JSON output is a list")
    cap = int(max_results or obs.load_source(SOURCE_ID)["budgets"]["max_results"])
    certificates: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, Mapping) or row.get("id") is None:
            continue
        san = _san_names(row.get("name_value"))
        if domain not in {n[2:] if n.startswith("*.") else n for n in san}:
            continue  # only certificates that actually cover the requested domain
        cert_id = str(row["id"])
        certificates.setdefault(
            cert_id,
            {
                "crtsh_id": cert_id,
                "issuer": " ".join(str(row.get("issuer_name") or "").split()) or None,
                "not_before": row.get("not_before"),
                "not_after": row.get("not_after"),
                "entry_timestamp": row.get("entry_timestamp"),
                "serial_number": row.get("serial_number"),
                "san": san,
            },
        )
    ordered = sorted(
        certificates.values(), key=lambda c: (str(c["not_before"] or ""), c["crtsh_id"])
    )
    truncated = len(ordered) > cap
    ordered = ordered[-cap:]
    return {
        "domain": domain,
        "certificates": ordered,
        "certificate_count": len(ordered),
        "truncated": truncated,
        "redaction": {
            "policy": "deny_person_identification",
            "email_sans_dropped": True,
        },
    }


def acquire_crtsh(
    conn,
    domain: str,
    *,
    request_id: str,
    transport: Callable[..., Mapping[str, Any]] | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
    now: Callable[[], int] | None = None,
    investigation: str | None = None,
) -> dict[str, Any]:
    """Acquire certificate issuance history for one explicitly named domain."""
    source = obs.load_source(SOURCE_ID)
    domain = obs.normalize_domain(domain)
    receipt = obs.acquire(
        conn,
        source_id=SOURCE_ID,
        domain=domain,
        request_id=request_id,
        url=source["endpoint"].split("?")[0],
        params={"q": domain, "output": "json"},
        parse=parse_crtsh,
        adapter_version=ADAPTER_VERSION,
        transport=transport,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        now=now,
    )
    if investigation:
        obs.record_in_investigation(conn, investigation, receipt)
    return receipt


def acquire_for_source(
    conn, namespace: str, source_id: str, *, request_id: str, **kwargs: Any
) -> dict[str, Any]:
    """Acquire CT history for the domain(s) of one existing source identity."""
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
            acquire_crtsh(conn, d, request_id=f"{request_id}:{d}", **kwargs)
            for d in domains
        ],
    }


def project_crtsh(
    conn, namespace: str, observation_id: str, *, principal_id: str, scopes: set[str]
) -> dict[str, Any]:
    """Project one crt.sh observation into ``source_identity`` revisions and
    probable ``shared-infrastructure`` relationships."""
    from src.kb.source_identity import SourceIdentityStore

    record = obs.get_observation(conn, observation_id)
    if record is None or record["source_id"] != SOURCE_ID:
        raise obs.ObservationError("not_found", "no crt.sh observation with that id")
    facts, domain = record["facts"], record["subject"]["value"]
    store = SourceIdentityStore(conn)
    domains = obs.source_domains(conn, namespace)
    owners = domains.get(domain, [])
    if not owners:
        return {
            "status": "no_source_identity",
            "domain": domain,
            "observation_id": observation_id,
        }
    if len(owners) > 1:
        return {
            "status": "ambiguous",
            "domain": domain,
            "candidates": owners,
            "observation_id": observation_id,
        }
    source_id = owners[0]
    certs = facts.get("certificates") or []
    identity = obs.revise_identity_native_ids(
        store,
        namespace,
        source_id,
        {
            f"ct:{domain}:certificate_count": str(len(certs)),
            f"ct:{domain}:first_not_before": certs[0]["not_before"] if certs else None,
            f"ct:{domain}:latest_not_before": certs[-1]["not_before"]
            if certs
            else None,
            f"ct:{domain}:issuers": ",".join(
                sorted({c["issuer"] for c in certs if c.get("issuer")})
            ),
            f"ct:{domain}:observation": observation_id,
        },
        principal_id=principal_id,
        scopes=scopes,
        citation_key=f"ct:{domain}:observation",
    )
    shared: dict[str, list[dict[str, Any]]] = {}
    for cert in certs:
        covered = {n[2:] if n.startswith("*.") else n for n in cert["san"]}
        for other_domain in sorted(covered - {domain}):
            for other in domains.get(other_domain, []):
                if other != source_id:
                    shared.setdefault(other, []).append(
                        {"crtsh_id": cert["crtsh_id"], "domain": other_domain}
                    )
    relationships = []
    for other, hits in sorted(shared.items()):
        left, right = sorted((source_id, other))
        relationships.append(
            store.relate(
                namespace,
                left,
                right,
                "shared-infrastructure",
                principal_id=principal_id,
                scopes=scopes,
                observed_at_ms=record["archive_at_ms"],
                confidence=0.5,
                uncertainty=0.5,
                evidence=[
                    obs.observation_evidence(
                        record,
                        fact="shared-certificate-san",
                        certificate_ids=sorted({h["crtsh_id"] for h in hits}),
                        domains=sorted({domain, *(h["domain"] for h in hits)}),
                    )
                ],
                producer={"name": "noesis-ct-projection", "version": ADAPTER_VERSION},
                policy={
                    "relationship": "probable-infrastructure-v1",
                    "status": "probable",
                    "caveat": SHARED_INFRASTRUCTURE_CAVEAT,
                },
            )
        )
    return {
        "status": "projected",
        "domain": domain,
        "source_identity": source_id,
        "identity_revision": identity["revision"],
        "identity_revision_id": identity["revision_id"],
        "shared_infrastructure": relationships,
        "caveat": SHARED_INFRASTRUCTURE_CAVEAT,
        "observation_id": observation_id,
    }

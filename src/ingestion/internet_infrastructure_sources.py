"""Internet infrastructure sources for the Technology ``technology.internet-infrastructure`` provider (#2743, II03-II06).

The machine-readable copy of ``docs/development/internet-infrastructure-evidence/source-audit.md`` (II01). One native
source-pack connector, ``internet-infrastructure``, driven by :mod:`src.ingestion.source_pack_runtime` with five
sources of the separate ``technology-internet-infrastructure`` source pack
(``config/source_packs/technology-internet-infrastructure.json``). Each source declares one provider and a bounded
selection of resources (one ASN, one prefix it announces, one domain); the adapter fetches one declared resource per
page (a *unit*: every request that resource needs) and returns registry records, observations and complete listings
that :class:`src.kb.internet_infrastructure_store.InternetInfrastructureProjector` appends as revisions.

* **RIPEstat** (``ripestat``) - data calls ``as-overview``, ``announced-prefixes``, ``routing-status``,
  ``rpki-validation`` and ``prefix-overview`` with ``sourceapp=noesis``, paced at one request per second, one retry on
  HTTP 429. Each answer is an *observation* dated by RIPEstat's stated time and data-call version, never rewritten. A
  deprecated data call or another major version fails the unit (``schema_drift``) instead of reading a new shape. The
  ``whois`` and ``abuse-contact-finder`` calls are never used.
* **PeeringDB** (``peeringdb``) - ``net?asn=``, ``org/{id}``, ``netixlan?net_id=``, ``ix/{id}`` and declared
  ``fac/{id}`` with ``depth=0``; the network's *self-declaration*. ``poc`` objects and user accounts are never read; only
  allow-listed fields are kept. The optional ``NOESIS_PEERINGDB_API_KEY`` is sent as ``Authorization: Api-Key`` and
  never recorded in a URL, receipt or record.
* **RDAP** (``rdap``) - the IANA bootstrap files (``asn.json``, ``ipv4.json``/``ipv6.json``, ``dns.json``) parsed by
  :func:`src.ingestion.rdap.parse_bootstrap` (one parser, cached per file for 24 h) and one request per declared object
  to the server the bootstrap names, through the RFC 9083 parser and person-data rules of :mod:`src.ingestion.rdap`.
  Only the holder or registrant organisation, country, RIR, status, events and nameservers are kept.
* **crt.sh** (``crtsh``) - ``?q=<domain>&output=json`` for one exact domain through
  :func:`src.ingestion.crtsh.parse_crtsh` and :func:`src.ingestion.osint_observations.normalize_domain`; more than
  200 certificates is ``budget_exhausted``, never truncated; revocation is never inferred.
* **CT log list** (``ct-log-list``) - the Chrome CT log list v3, one file of at most 2 MB; a log changing state is a
  new revision of that log.

Direct RFC 6962 log APIs and CAIDA datasets stay ``not-implemented``. Every provider is ``unverified-live`` until a
dated live run (II13); endpoints, data-call versions, field names and terms marked *verify* come from the publishers'
documentation, not from a live response. Nothing here enumerates subdomains, reads ports or banners, pivots on an IP
address or a person, ranks networks, judges a route or merges statements of different sources.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "internet-infrastructure"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
UNIT_CONTRACT = "noesis-internet-infrastructure-unit-v1"
RECORD_CONTRACT = "noesis-internet-infrastructure-record-v2"
AUDIT = "docs/development/internet-infrastructure-evidence/source-audit.md"
SOURCE_PACK_ID = "technology-internet-infrastructure"
SECRET_REF = "NOESIS_PEERINGDB_API_KEY"
# A placeholder the offline fixtures send so the key path is exercised; it must never appear in any record.
FIXTURE_SECRET = "fixture-peeringdb-key-not-a-real-key"
SOURCEAPP = "noesis"
PROVIDERS = ("ripestat", "peeringdb", "rdap", "crtsh", "ct-log-list")
NOT_IMPLEMENTED = ("rfc6962-logs", "caida")
RIPESTAT_CALLS = ("as-overview", "announced-prefixes", "routing-status", "rpki-validation", "prefix-overview")
# Never requested, whatever a declaration says (contact and WHOIS data, II01 minimisation).
FORBIDDEN_CALLS = ("whois", "abuse-contact-finder")
# The data-call major versions the parsers read (verify against the live data-call documentation); another major
# version, or a data-call status starting "deprecated", fails the unit instead of reading a new shape silently.
DATA_CALL_VERSIONS = {"as-overview": "1", "announced-prefixes": "1", "routing-status": "2", "rpki-validation": "0",
                      "prefix-overview": "1"}
ASN_CALLS = ("as-overview", "announced-prefixes", "routing-status", "rpki-validation")
PREFIX_CALLS = ("prefix-overview", "routing-status")
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "ripestat": ("stat.ripe.net",),
    "peeringdb": ("www.peeringdb.com",),
    # The IANA bootstrap host and the registry servers its files name for first coverage (verify the .org registry
    # server); a bootstrap target outside this set fails the unit with network_policy.
    "rdap": ("data.iana.org", "rdap.arin.net", "rdap.db.ripe.net", "rdap.apnic.net", "rdap.lacnic.net",
             "rdap.afrinic.net", "rdap.publicinterestregistry.org"),
    "crtsh": ("crt.sh",),
    "ct-log-list": ("www.gstatic.com",),
}
RIR_BY_HOST = {"rdap.arin.net": "ARIN", "rdap.db.ripe.net": "RIPE NCC", "rdap.apnic.net": "APNIC",
               "rdap.lacnic.net": "LACNIC", "rdap.afrinic.net": "AFRINIC"}
CT_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"
CRTSH_URL = "https://crt.sh/"
PEERINGDB_API = "https://www.peeringdb.com/api"
RIPESTAT_API = "https://stat.ripe.net/data"
# Hard ceilings the adapter enforces on top of the source-pack budgets (II01 bounded first coverage).
CAPS: dict[str, dict[str, int]] = {
    "ripestat": {"calls_per_resource": 5, "resources": 2, "prefixes_per_response": 2000, "requests_per_second": 1,
                 "retries_on_429": 1},
    "peeringdb": {"networks": 1, "organisations": 1, "netixlan_rows": 20, "ixs": 5, "facilities": 2,
                  "retries_on_429": 1},
    "rdap": {"objects": 3, "bootstrap_fetches_per_file_per_24h": 1, "retries_on_429": 1},
    "crtsh": {"requests": 1, "certificates": 200, "retries_on_429": 1},
    "ct-log-list": {"files": 1, "max_bytes": 2_000_000, "retries_on_429": 1},
}
BOUNDED_COVERAGE: dict[str, Any] = {
    "ripestat": {"selection": "one declared ASN and one declared prefix it announces",
                 "caps": "5 data calls per resource, 2 resources, 2,000 prefixes per response"},
    "peeringdb": {"selection": "the network record of the same ASN, its organisation and IX presence (declared "
                               "facility ids only)",
                  "caps": "1 network, 1 organisation, 20 netixlan rows, 5 IXs (at most 2 declared facilities)"},
    "rdap": {"selection": "the same ASN and prefix, and one declared domain",
             "caps": "3 objects, 1 bootstrap fetch per file per 24 h"},
    "crtsh": {"selection": "the same domain, exact match",
              "caps": "1 request, 200 certificates; more is budget_exhausted, never truncated"},
    "ct-log-list": {"selection": "the current list", "caps": "1 file, 2 MB"},
    "justification": "one network seen through its routing state, its self-declaration, its registration and one "
                     "domain's certificates is the smallest selection that shows the four sources side by side "
                     "without merging them; every further resource is a source-pack version bump",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ripestat": {
        "publisher": "RIPE NCC, RIPEstat Data API",
        "delivers": "routing state and history per declared ASN or prefix: announced prefixes, routing status and "
                    "visibility, RPKI validation, AS overview",
        "endpoints": [("https://stat.ripe.net/data/{call}/data.json?resource=<ASN or prefix>&sourceapp=noesis "
                       "(calls as-overview, announced-prefixes, routing-status, rpki-validation with resource and "
                       "prefix, prefix-overview; verify call names and parameters)")],
        "authentication": "none; sourceapp is an identifier, not a secret (verify)",
        "licence": "RIPEstat terms and conditions; reuse with attribution 'RIPEstat, RIPE NCC' (verify wording and "
                   "whether redistribution of results is permitted)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "fair use; no fixed public quota known (verify); paced at 1 request/second; at most 5 calls "
                       "per resource; HTTP 429 gets one retry",
        "revision_model": "every response carries data_call_status, the data-call version and a query_time or "
                          "latest_time (verify field names): each answer is an observation dated by RIPEstat's "
                          "stated time, never by retrieval time alone; a later answer is a later observation; a "
                          "deprecated data-call version fails the unit rather than reading a new shape silently",
        "unavailable_fallback": "a failed unit (HTTP error, 429 after one retry, redirect to an undeclared host, "
                                "schema drift, an over-budget response) fails with its code and a receipt; earlier "
                                "observations stay current; readiness reports the source stale",
        "status": "unverified-live",
        "verify": ["data-call names, parameters and versions", "query_time/latest_time field names",
                   "terms wording and redistribution", "sourceapp convention", "quota"],
    },
    "peeringdb": {
        "publisher": "PeeringDB",
        "delivers": "self-declared network, organisation, IX and facility records; network-to-IX presence",
        "endpoints": ["https://www.peeringdb.com/api/net?asn=<ASN>", "/api/org/{id}", "/api/netixlan?net_id=<id>",
                      "/api/ix/{id}", "/api/fac/{id}; depth=0 (verify)"],
        "authentication": f"anonymous; optional Authorization: Api-Key <key> from {SECRET_REF} (optional-secret, "
                          "never stored in a record or receipt)",
        "licence": "PeeringDB Acceptable Use Policy: data for interconnection and operational purposes, no use for "
                   "unsolicited marketing (verify the AUP's reuse and redistribution clauses before the live run, "
                   "II13)",
        "redistribution": "verify (PeeringDB AUP reuse and redistribution clauses, II13)",
        "rate_limits": "anonymous and keyed per-minute limits, HTTP 429 when exceeded (verify numbers); one retry",
        "revision_model": "each object has created, updated and status (ok, pending, deleted; verify): a changed "
                          "updated is a new revision; status=deleted (or a 404 for a declared id) is a "
                          "removed_by_source revision; the record is the network's self-declaration and is labelled "
                          "so",
        "unavailable_fallback": "as RIPEstat; nothing is marked removed because of a failure",
        "status": "unverified-live",
        "verify": ["depth=0 semantics", "status values", "AUP reuse and redistribution clauses", "rate limits"],
    },
    "rdap": {
        "publisher": "IANA bootstrap, RIR and registry RDAP servers",
        "delivers": "registration records for declared ASNs, prefixes and one domain: holder organisation, status, "
                    "events",
        "endpoints": ["bootstrap https://data.iana.org/rdap/asn.json, ipv4.json, ipv6.json, dns.json",
                      "{base}autnum/{asn}, {base}ip/{prefix}, {base}domain/{name} on the server the bootstrap names"],
        "authentication": "none",
        "licence": "per registry: RIPE Database terms, ARIN RDAP terms of use, other RIR and registry terms (verify "
                   "each)",
        "redistribution": "provider-specific",
        "rate_limits": "per RIR, HTTP 429 (verify); one request per declared object; the bootstrap fetched once per "
                       "file per 24 h",
        "revision_model": "events (registration, last changed) and status per object; a changed last changed or "
                          "content digest is a new revision; a 404 for a declared object is a removed_by_source "
                          "revision; a transfer between RIRs appears as a different bootstrap target and is recorded "
                          "as stated",
        "unavailable_fallback": "as RIPEstat",
        "status": "unverified-live",
        "verify": ["each registry's terms", "rate limits", "the .org registry server the bootstrap names"],
    },
    "crtsh": {
        "publisher": "Sectigo (crt.sh)",
        "delivers": "issuance facts for one exactly named domain: issuer, validity, DNS SANs",
        "endpoints": ["https://crt.sh/?q=<domain>&output=json (exact domain, no % patterns)"],
        "authentication": "none",
        "licence": "Sectigo operates crt.sh; no published licence (verify)",
        "redistribution": "locator-only",
        "rate_limits": "undocumented; slow and times out on large domains (verify); one request per declared domain "
                       "per run",
        "revision_model": "CT logs are append-only; a new certificate is a new issuance record; revocation is not "
                          "in the JSON output (verify) and is never inferred; expired certificates stay recorded "
                          "with their validity dates",
        "unavailable_fallback": "as RIPEstat",
        "status": "unverified-live",
        "verify": ["licence", "rate limits", "whether the JSON output states log ids (null when it does not)",
                   "revocation absence"],
    },
    "ct-log-list": {
        "publisher": "Google Chrome CT log list v3 (verify host and version)",
        "delivers": "log ids, operators, states (usable, readonly, retired)",
        "endpoints": [CT_LOG_LIST_URL + " (verify)"],
        "authentication": "none",
        "licence": "published for CT policy use; terms verify",
        "redistribution": "verify",
        "rate_limits": "one file per run, at most 2 MB",
        "revision_model": "version and log_list_timestamp (verify) date each release; a log changing state (usable "
                          "to retired) is a new revision of that log",
        "unavailable_fallback": "as RIPEstat",
        "status": "unverified-live",
        "verify": ["host and version", "terms", "field names"],
    },
    "rfc6962-logs": {
        "publisher": "CT log operators",
        "delivers": "raw log entries (get-entries), signed tree heads",
        "status": "not-implemented",
        "reason": "logs cannot be queried by domain, so finding one domain's certificates means mirroring a log; that "
                  "breaks the bounded-coverage rule",
    },
    "caida": {
        "publisher": "CAIDA, UC San Diego",
        "delivers": "inferred AS relationships, rankings, topology",
        "status": "not-implemented",
        "reason": "the acceptable-use agreement limits use and redistribution (verify), which conflicts with "
                  "exporting cited evidence bundles; AS Rank is a ranking, which the non-goals exclude",
    },
}
LIVE_VERIFICATION: dict[str, dict[str, Any]] = {
    **{provider: {"status": "unverified-live", "checked": None, "evidence": None,
                  "intended": "verified-live after a dated bounded run (II13, #2743)",
                  "note": "no dated live run from this runtime; authored offline fixtures only"}
       for provider in PROVIDERS},
    "rfc6962-logs": {"status": "not-implemented", "checked": None, "evidence": None,
                     "note": "bounded coverage impossible without mirroring"},
    "caida": {"status": "not-implemented", "checked": None, "evidence": None,
              "note": "acceptable-use agreement restricts redistribution (verify)"},
}
EXCLUSIONS = (
    "exposed-service search, port or banner data",
    "subdomain enumeration or attack-surface mapping",
    "IP-keyed 'what else is hosted here' pivots",
    "person-keyed lookups (e-mail, handle, name)",
    "reputation, risk, hijack or misconfiguration verdicts",
    "ranking of networks",
    "merging RIPEstat, PeeringDB and RDAP statements about one ASN or prefix into one record",
)
NEVER_SENTENCE = (
    "Registry and routing facts for declared resources as each source states them, side by side and cited: no "
    "exposed-service, port or banner data, no subdomain enumeration, no IP-keyed or person-keyed pivots, no "
    "reputation, risk, hijack or misconfiguration verdict, no ranking of networks and no merged record."
)
MINIMISATION: dict[str, Any] = {
    "decision": "registry and routing facts about networks and organisations only; no field identifies a person",
    "stored": "ASN, prefix, domain, holder or registrant organisation name as published, country as published, RIR, "
              "status, registration events, nameservers; RIPEstat routing observations; PeeringDB network, "
              "organisation, IX and facility records (names, ASN, info_type, policy fields, IX presence with speeds "
              "and addresses on the IX LAN); certificate crt.sh id, issuer DN, validity, DNS SANs, log ids",
    "redacted": "RDAP entities with vCard kind individual are never persisted; for any other entity only the "
                "organisation name is kept; e-mail, telephone and postal address properties are never read",
    "excluded": "RDAP administrative, technical, billing and abuse contacts; PeeringDB poc objects (all "
                "visibilities) and user accounts; the RIPEstat whois and abuse-contact-finder calls; certificate "
                "subject DNs beyond the organisation (O), e-mail SANs, S/MIME and client certificates; anything keyed "
                "by an IP address alone",
    "retention": "revisions and observations are kept for provenance with their run; no stored field identifies a "
                 "person, so no erasure workflow applies",
    "who_may_query": "knowledge:technical:internet-infrastructure:read with namespace access; writes ...:write; "
                     "identity reviews ...:review",
    "osint_gate": "not gated under docs/security/osint-review-gate.md: the tools answer registry and routing facts "
                  "for declared resources, refuse IP-keyed, person-keyed and wildcard queries in code and never list "
                  "other networks of an organisation beyond what a declared ASN returns; undeclared or reverse "
                  "lookups go through the gate and the abuse analysis first",
    "enforced_by": "internet_infrastructure_records.check_item at write time",
}
# PeeringDB fields kept per object (allow-list; every contact, e-mail, phone, address and free-text field is dropped).
PEERINGDB_FIELDS = {
    "net": ("id", "org_id", "name", "aka", "name_long", "website", "asn", "looking_glass", "route_server",
            "irr_as_set", "info_type", "info_types", "info_prefixes4", "info_prefixes6", "info_traffic",
            "info_ratio", "info_scope", "info_unicast", "info_multicast", "info_ipv6",
            "info_never_via_route_servers", "policy_url", "policy_general", "policy_locations", "policy_ratio",
            "policy_contracts", "created", "updated", "status"),
    "org": ("id", "name", "aka", "name_long", "website", "country", "created", "updated", "status"),
    "netixlan": ("id", "net_id", "ix_id", "ixlan_id", "name", "speed", "asn", "ipaddr4", "ipaddr6", "is_rs_peer",
                 "bfd_support", "operational", "created", "updated", "status"),
    "ix": ("id", "org_id", "name", "aka", "name_long", "city", "country", "region_continent", "media",
           "proto_unicast", "proto_multicast", "proto_ipv6", "website", "url_stats", "created", "updated", "status"),
    "fac": ("id", "org_id", "name", "aka", "name_long", "website", "clli", "city", "country", "region_continent",
            "created", "updated", "status"),
}
PEERINGDB_STATUSES = ("ok", "pending", "deleted")
CERTIFICATE_FIELDS = ("crtsh_id", "issuer", "not_before", "not_after", "entry_timestamp", "serial_number", "dns_sans",
                      "log_ids", "subject_organisation")
# The crt.sh JSON row field read for log ids when it is present (verify; crt.sh is not known to state them in JSON).
CRTSH_LOG_ID_FIELD = "log_ids"
BOOTSTRAP_CACHE: dict[str, Any] = {}
_HANDLE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-(ripe|arin|ap|apnic|lacnic|afrinic|nicat|ripe-ncc)$", re.IGNORECASE)


class InfrastructureError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


def iso_time(value: Any) -> str | None:
    """An ISO-8601 UTC timestamp for a stated time (ISO text or epoch seconds), or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        if len(raw) == 10:
            raw += "T00:00:00+00:00"
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ declared resources


def _refuse_pattern(raw: str) -> None:
    if not raw:
        raise InfrastructureError("invalid_resource", "a declared ASN, prefix or domain is required")
    if any(ch in raw for ch in "*%?"):
        raise InfrastructureError("wildcard_refused", "wildcard or pattern lookups are not supported")
    if "@" in raw:
        raise InfrastructureError("person_identifier_refused", "e-mail addresses are not network resources")
    if _HANDLE.fullmatch(raw):
        raise InfrastructureError("person_identifier_refused", "registry contact handles are not network resources")
    if " " in raw.strip():
        raise InfrastructureError("person_identifier_refused", "names are not declared network resources")


def normalize_asn(value: Any) -> str:
    """``AS64500`` for ``64500``, ``AS64500`` or ``as64500``; anything else is refused."""
    raw = str(value if value is not None else "").strip()
    _refuse_pattern(raw)
    digits = raw[2:] if raw[:2].upper() == "AS" else raw
    if not digits.isdigit() or not 0 < int(digits) <= 4_294_967_295:
        raise InfrastructureError("invalid_asn", "an autonomous system number is AS followed by 1 to 4294967295")
    return f"AS{int(digits)}"


def asn_number(value: Any) -> int:
    return int(normalize_asn(value)[2:])


def normalize_prefix(value: Any) -> str:
    """A canonical IP prefix; a bare address or a host route (/32, /128) is an IP-keyed lookup and refused."""
    raw = str(value if value is not None else "").strip()
    _refuse_pattern(raw)
    if "/" not in raw:
        try:
            ipaddress.ip_address(raw.strip("[]"))
        except ValueError:
            raise InfrastructureError("invalid_prefix", "an IP prefix is an address and a length") from None
        raise InfrastructureError("ip_lookup_refused", "IP-keyed lookups are not supported; declare a prefix")
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError:
        raise InfrastructureError("invalid_prefix", "not a canonical IP prefix (host bits set or malformed)") from None
    if network.prefixlen == network.max_prefixlen:
        raise InfrastructureError("ip_lookup_refused", "a host route is one IP address; IP-keyed lookups are not "
                                                       "supported")
    return str(network)


def normalize_domain(value: Any) -> str:
    """One exact domain through the OSINT rule (no wildcard, IP literal, e-mail, URL or handle)."""
    from src.ingestion import osint_observations as obs

    raw = str(value if value is not None else "").strip()
    _refuse_pattern(raw)
    try:
        return obs.normalize_domain(raw)
    except obs.ObservationError as exc:
        raise InfrastructureError(exc.code, str(exc)) from None


def classify_resource(value: Any) -> dict[str, str]:
    """``{"kind": "asn"|"prefix"|"domain", "value": ...}`` for a resource given as a mapping or a string."""
    if isinstance(value, Mapping):
        kinds = [k for k in ("asn", "prefix", "domain") if value.get(k) not in (None, "")]
        if set(value) - {"asn", "prefix", "domain", "kind", "value"}:
            raise InfrastructureError("invalid_resource", "a resource is one ASN, one prefix or one domain")
        if value.get("kind") in ("asn", "prefix", "domain") and not kinds:
            return classify_resource({value["kind"]: value.get("value")})
        if len(kinds) != 1:
            raise InfrastructureError("invalid_resource", "a resource is exactly one ASN, prefix or domain")
        kind = kinds[0]
        normal = {"asn": normalize_asn, "prefix": normalize_prefix, "domain": normalize_domain}[kind]
        return {"kind": kind, "value": normal(value[kind])}
    raw = str(value if value is not None else "").strip()
    _refuse_pattern(raw)
    if re.fullmatch(r"(?i)(as)?\d+", raw):
        return {"kind": "asn", "value": normalize_asn(raw)}
    if "/" in raw or ":" in raw or re.fullmatch(r"[\d.]+", raw):
        return {"kind": "prefix", "value": normalize_prefix(raw)}
    return {"kind": "domain", "value": normalize_domain(raw)}


def resource_key(resource: Mapping[str, Any]) -> str:
    return f"{resource['kind']}:{resource['value']}"


# ------------------------------------------------------------------ declarations


def infrastructure_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The validated ``internet_infrastructure`` block of a source: provider, namespace and bounded selection."""
    config = dict(source.get("internet_infrastructure") or {})
    provider = config.get("provider")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"internet-infrastructure sources declare one of {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider][0]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]}")
    auth = dict(source.get("auth") or {})
    if provider == "peeringdb":
        if auth.get("kind") not in {"none", "optional-secret"} or (
                auth.get("kind") == "optional-secret" and auth.get("secret_ref") != SECRET_REF):
            raise SourcePackError("invalid_manifest", f"PeeringDB uses the optional secret {SECRET_REF} or no key")
    elif auth.get("kind") != "none":
        raise SourcePackError("invalid_manifest", f"{provider} requests carry no credential")
    if config.get("live_verification") not in {"unverified-live", "verified-live", "blocked"}:
        raise SourcePackError("invalid_manifest", "a source states its live_verification")
    selection = dict(config.get("selection") or {})
    allowed = {"ripestat": {"asn", "prefix"}, "peeringdb": {"asn", "facilities"},
               "rdap": {"asn", "prefix", "domain"}, "crtsh": {"domain"}, "ct-log-list": {"list"}}[provider]
    if set(selection) - allowed or not selection:
        raise SourcePackError("invalid_manifest", f"{provider} selects only {sorted(allowed)}")
    try:
        normal: dict[str, Any] = {}
        if "asn" in selection:
            normal["asn"] = normalize_asn(selection["asn"])
        if "prefix" in selection:
            normal["prefix"] = normalize_prefix(selection["prefix"])
        if "domain" in selection:
            normal["domain"] = normalize_domain(selection["domain"])
    except InfrastructureError as exc:
        raise SourcePackError("invalid_manifest", f"{exc.code}: {exc}") from exc
    if provider == "ct-log-list":
        if selection.get("list") != "v3":
            raise SourcePackError("invalid_manifest", "the CT log list source reads the v3 list")
        normal["list"] = "v3"
    if provider == "ripestat" and "asn" not in normal:
        raise SourcePackError("invalid_manifest", "RIPEstat selects one declared ASN (and a prefix it announces)")
    if provider == "peeringdb":
        if "asn" not in normal:
            raise SourcePackError("invalid_manifest", "PeeringDB selects the network of one declared ASN")
        facilities = list(selection.get("facilities") or [])
        if len(facilities) > CAPS["peeringdb"]["facilities"] or not all(
                isinstance(f, int) and f > 0 for f in facilities) or len(set(facilities)) != len(facilities):
            raise SourcePackError("invalid_manifest", "PeeringDB facilities are at most 2 declared numeric ids")
        normal["facilities"] = facilities
    units = declared_units(provider, normal)
    if len(units) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared resources than the source's page budget")
    return {"provider": provider, "namespace": config.get("namespace") or "global", "selection": normal,
            "units": units, "live_verification": config["live_verification"]}


def declared_units(provider: str, selection: Mapping[str, Any]) -> list[dict[str, str]]:
    """One unit per declared resource, within the audited resource and object caps."""
    if provider == "ripestat":
        units = [{"kind": "asn", "value": selection["asn"]}]
        if selection.get("prefix"):
            units.append({"kind": "prefix", "value": selection["prefix"]})
        return units[:CAPS["ripestat"]["resources"]]
    if provider == "peeringdb":
        return [{"kind": "asn", "value": selection["asn"]}]
    if provider == "rdap":
        units = [{"kind": k, "value": selection[k]} for k in ("asn", "prefix", "domain") if selection.get(k)]
        return units[:CAPS["rdap"]["objects"]]
    if provider == "crtsh":
        return [{"kind": "domain", "value": selection["domain"]}]
    return [{"kind": "ct-log-list", "value": "v3"}]


def ripestat_requests(resource: Mapping[str, str], selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The data calls for one declared resource (at most five), each with ``sourceapp=noesis``."""
    if resource["kind"] == "asn":
        calls = list(ASN_CALLS if selection.get("prefix") else ASN_CALLS[:3])
    else:
        calls = list(PREFIX_CALLS)
    requests = []
    for call in calls:
        if call in FORBIDDEN_CALLS or call not in RIPESTAT_CALLS:
            raise InfrastructureError("call_forbidden", f"the RIPEstat {call} call is never used")
        params = {"resource": resource["value"], "sourceapp": SOURCEAPP}
        if call == "rpki-validation":
            params["prefix"] = selection["prefix"]
        requests.append({"call": call, "url": f"{RIPESTAT_API}/{call}/data.json", "params": params})
    if len(requests) > CAPS["ripestat"]["calls_per_resource"]:
        raise InfrastructureError("budget_exhausted", "more data calls than the per-resource cap")
    return requests


# ------------------------------------------------------------------ parsing: RIPEstat


def _subset(value: Any, keys: Sequence[str]) -> dict[str, Any]:
    value = value if isinstance(value, Mapping) else {}
    return {k: value.get(k) for k in keys if k in value}


def _resource_of(kind: str, value: Any) -> str:
    return normalize_asn(value) if kind == "asn" else normalize_prefix(value)


def parse_ripestat(raw: bytes, *, call: str, resource: Mapping[str, str], params: Mapping[str, Any]) -> dict:
    """One RIPEstat answer as an observation dated by RIPEstat's stated time and data-call version."""
    if call in FORBIDDEN_CALLS or call not in RIPESTAT_CALLS:
        raise InfrastructureError("call_forbidden", f"the RIPEstat {call} call is never used")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InfrastructureError("schema_drift", "RIPEstat answer is not JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
        raise InfrastructureError("schema_drift", "RIPEstat answer carries no data object")
    if payload.get("status") != "ok":
        raise InfrastructureError("source_unavailable", f"RIPEstat status {payload.get('status')!r}")
    if payload.get("data_call_name") not in (None, call):
        raise InfrastructureError("schema_drift", "RIPEstat answered another data call")
    status = str(payload.get("data_call_status") or "")
    if not status:
        raise InfrastructureError("schema_drift", "RIPEstat answer states no data_call_status")
    if status.casefold().startswith("deprecated"):
        raise InfrastructureError("deprecated_data_call", f"{call} is deprecated ({status}); the unit fails rather "
                                                          "than reading a new shape")
    version = str(payload.get("version") or "")
    if version.split(".")[0] != DATA_CALL_VERSIONS[call]:
        raise InfrastructureError("data_call_version_changed", f"{call} answered version {version!r}; the parser "
                                                               f"reads major version {DATA_CALL_VERSIONS[call]}")
    data = payload["data"]
    stated, basis = None, None
    for key in ("query_time", "latest_time"):
        if iso_time(data.get(key)):
            stated, basis = iso_time(data.get(key)), key
            break
    if stated is None and iso_time(payload.get("time")):
        stated, basis = iso_time(payload.get("time")), "response_time"
    if stated is None:
        raise InfrastructureError("schema_drift", "RIPEstat answer states no time; never dated by retrieval alone")
    if data.get("resource") is not None:
        try:
            answered = _resource_of(resource["kind"], data["resource"])
        except InfrastructureError as exc:
            raise InfrastructureError("schema_drift", "RIPEstat answered an unreadable resource") from exc
        if answered != resource["value"]:
            raise InfrastructureError("subject_mismatch", "RIPEstat answered for another resource")
    identifiers: dict[str, list[str]] = {resource["kind"]: [resource["value"]]}
    if call == "as-overview":
        if "holder" not in data and "announced" not in data:
            raise InfrastructureError("schema_drift", "as-overview states neither holder nor announced")
        content = {"holder": data.get("holder"), "announced": data.get("announced"),
                   "block": _subset(data.get("block"), ("resource", "desc", "name"))}
    elif call == "announced-prefixes":
        prefixes = data.get("prefixes")
        if not isinstance(prefixes, list):
            raise InfrastructureError("schema_drift", "announced-prefixes states no prefix list")
        if len(prefixes) > CAPS["ripestat"]["prefixes_per_response"]:
            raise InfrastructureError("budget_exhausted", "more announced prefixes than the 2,000 per response cap")
        rows = []
        for row in prefixes:
            try:
                prefix = str(ipaddress.ip_network(str(dict(row)["prefix"]), strict=False))
            except (KeyError, TypeError, ValueError) as exc:
                raise InfrastructureError("schema_drift", "an announced prefix is unreadable") from exc
            rows.append({"prefix": prefix, "timelines": [_subset(t, ("starttime", "endtime"))
                                                         for t in dict(row).get("timelines") or []]})
        rows.sort(key=lambda r: r["prefix"])
        content = {"prefixes": rows, "prefix_count": len(rows),
                   **_subset(data, ("earliest_time", "latest_time"))}
    elif call == "routing-status":
        if "visibility" not in data and "announced_space" not in data:
            raise InfrastructureError("schema_drift", "routing-status states no visibility")
        content = _subset(data, ("visibility", "first_seen", "last_seen", "announced_space", "observed_neighbours",
                                 "resource"))
    elif call == "rpki-validation":
        if "status" not in data:
            raise InfrastructureError("schema_drift", "rpki-validation states no status")
        content = {"rpki_status_as_stated": data.get("status"), "prefix": data.get("prefix") or params.get("prefix"),
                   "validating_roas": [_subset(r, ("origin", "prefix", "max_length", "validity"))
                                       for r in data.get("validating_roas") or []]}
        identifiers["prefix"] = [normalize_prefix(params["prefix"])]
    else:  # prefix-overview
        if "asns" not in data and "announced" not in data:
            raise InfrastructureError("schema_drift", "prefix-overview states neither asns nor announced")
        asns = []
        for row in data.get("asns") or []:
            try:
                asns.append({"asn": normalize_asn(dict(row)["asn"]), "holder": dict(row).get("holder")})
            except (KeyError, TypeError, InfrastructureError) as exc:
                raise InfrastructureError("schema_drift", "prefix-overview states an unreadable origin") from exc
        content = {"announced": data.get("announced"), "is_less_specific": data.get("is_less_specific"),
                   "asns": asns, "block": _subset(data.get("block"), ("resource", "desc", "name"))}
        if asns:
            identifiers["asn"] = sorted({a["asn"] for a in asns})
    native = f"{call}:{resource['value']}" + (f":{params['prefix']}" if call == "rpki-validation" else "")
    return {
        "record_type": "observation", "provider": "ripestat", "object_kind": "routing-observation",
        "native_id": native, "resource": dict(resource), "data_call": call, "data_call_version": version,
        "data_call_status": status, "stated_time": stated, "time_basis": basis,
        "query": {k: params[k] for k in sorted(params)}, "content": content, "stated_identifiers": identifiers,
        "label": "observation dated by RIPEstat's stated time",
    }


# ------------------------------------------------------------------ parsing: PeeringDB


def _peeringdb_rows(raw: bytes, what: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InfrastructureError("schema_drift", f"PeeringDB {what} answer is not JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise InfrastructureError("schema_drift", f"PeeringDB {what} answer carries no data list")
    rows = [dict(r) for r in payload["data"] if isinstance(r, Mapping)]
    if len(rows) != len(payload["data"]):
        raise InfrastructureError("schema_drift", f"PeeringDB {what} rows are not objects")
    return rows


def _one(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
    """The single object a PeeringDB ``/{object}/{id}`` answer states (one organisation, IX or facility)."""
    if len(rows) != 1:
        raise InfrastructureError("schema_drift", f"PeeringDB {what} answer is not exactly one object")
    return rows[0]


def peeringdb_record(kind: str, row: Mapping[str, Any], resource: Mapping[str, str]) -> dict[str, Any]:
    """One allow-listed PeeringDB object as a registry record labelled as the network's self-declaration."""
    if row.get("id") in (None, ""):
        raise InfrastructureError("schema_drift", f"a PeeringDB {kind} object states no id")
    status = str(row.get("status") or "ok")
    if status not in PEERINGDB_STATUSES:
        raise InfrastructureError("schema_drift", f"unknown PeeringDB status {status!r}")
    content = {k: row.get(k) for k in PEERINGDB_FIELDS[kind] if k in row}
    updated = iso_time(row.get("updated"))
    identifiers: dict[str, list[Any]] = {"peeringdb_id": [f"{kind}:{row['id']}"]}
    if kind in {"net", "netixlan"} and row.get("asn") not in (None, ""):
        identifiers["asn"] = [normalize_asn(row["asn"])]
    if kind == "net" and row.get("org_id"):
        identifiers["peeringdb_id"].append(f"org:{row['org_id']}")
    if kind == "netixlan":
        identifiers["peeringdb_id"] += [f"net:{row.get('net_id')}", f"ix:{row.get('ix_id')}"]
    return {
        "record_type": "registry-record", "provider": "peeringdb", "object_kind": kind, "native_id": str(row["id"]),
        "resource": dict(resource), "state": "removed_by_source" if status == "deleted" else "published",
        "revision": {"basis": "source_updated" if updated else "content_digest", "source_revision": updated,
                     "valid_from": updated},
        "content": content, "stated_identifiers": identifiers,
        "label": "the network's self-declaration in PeeringDB",
    }


def peeringdb_removed(kind: str, native_id: Any, resource: Mapping[str, str]) -> dict[str, Any]:
    """A 404 for a declared PeeringDB id: a removed_by_source revision, dated by retrieval."""
    return {
        "record_type": "registry-record", "provider": "peeringdb", "object_kind": kind, "native_id": str(native_id),
        "resource": dict(resource), "state": "removed_by_source",
        "revision": {"basis": "not_found", "source_revision": "HTTP 404", "valid_from": None},
        "content": {"id": native_id, "status_as_answered": "HTTP 404 for a declared id"},
        "stated_identifiers": {"peeringdb_id": [f"{kind}:{native_id}"]},
        "label": "the network's self-declaration in PeeringDB",
    }


# ------------------------------------------------------------------ parsing: RDAP


def rdap_object_url(kind: str, value: str, base: str) -> str:
    if kind == "asn":
        return f"{base}autnum/{asn_number(value)}"
    if kind == "prefix":
        return f"{base}ip/{value}"
    return f"{base}domain/{value}"


RDAP_OBJECT_KINDS = {"asn": "autnum", "prefix": "ip-network", "domain": "domain"}


def _rdap_events(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    return sorted(({"action": str(e.get("eventAction")), "date": str(e.get("eventDate"))}
                   for e in payload.get("events") or []
                   if isinstance(e, Mapping) and e.get("eventAction") and e.get("eventDate")
                   and e.get("eventAction") != "last update of RDAP database"),
                  key=lambda e: (e["date"], e["action"]))


def _rdap_holder(payload: Mapping[str, Any]) -> tuple[str | None, str | None, int]:
    """(holder organisation, withheld reason, individual entities dropped) by the person-data rules of rdap.py."""
    from src.ingestion import rdap

    holder, withheld, dropped = None, None, 0
    for entity in rdap._walk(payload.get("entities")):
        roles = {str(r).lower() for r in entity.get("roles") or []}
        kinds = {str(k).lower() for k in rdap._vcard(entity).get("kind", [])}
        if "individual" in kinds:
            dropped += 1  # never persisted, whatever its role
            if "registrant" in roles:
                withheld = "natural-person holder dropped"
            continue
        if "registrant" in roles and holder is None:
            holder, reason = rdap._organization(entity)
            withheld = None if holder else (withheld or reason)
        # Administrative, technical, billing, abuse and every other role is dropped by construction.
    return holder, withheld, dropped


def parse_rdap_registration(raw: bytes, *, resource: Mapping[str, str], base: str) -> dict[str, Any]:
    """One RDAP autnum, IP network or domain object as a minimised registry record (RFC 9083)."""
    from src.ingestion import osint_observations as obs
    from src.ingestion import rdap

    kind, value = resource["kind"], resource["value"]
    host = (urlsplit(base).hostname or "").casefold()
    common = {"bootstrap_target": base, "rir": RIR_BY_HOST.get(host),
              "redaction": {"policy": "deny_person_identification", "fields_never_read": ["email", "tel", "adr"],
                            "roles_dropped": ["administrative", "technical", "billing", "abuse", "noc"]}}
    if kind == "domain":
        try:
            facts = rdap.parse_rdap_domain(raw, value)
        except obs.ObservationError as exc:
            raise InfrastructureError(exc.code if exc.code == "subject_mismatch" else "schema_drift", str(exc)) \
                from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise InfrastructureError("schema_drift", "RDAP answer is not JSON") from exc
        content = {"object_class": "domain", "ldh_name": facts["domain"], "status": facts["status"],
                   "events": facts["events"], "nameservers": facts["nameservers"], "country": None,
                   "holder_organisation": facts["registrant_organization"],
                   "holder_withheld": facts["registrant_withheld"], **common}
        content["redaction"] = {**common["redaction"],
                                "individual_entities_dropped": facts["redaction"]["individual_entities_dropped"]}
        identifiers: dict[str, list[str]] = {"domain": [value]}
    else:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InfrastructureError("schema_drift", "RDAP answer is not JSON") from exc
        expected = "autnum" if kind == "asn" else "ip network"
        if not isinstance(payload, Mapping) or payload.get("objectClassName") != expected:
            raise InfrastructureError("schema_drift", f"not an RDAP {expected} object")
        holder, withheld, dropped = _rdap_holder(payload)
        content = {"object_class": expected, "handle": payload.get("handle"), "name": payload.get("name"),
                   "type": payload.get("type"), "country": payload.get("country"),
                   "status": sorted(str(s) for s in payload.get("status") or []), "events": _rdap_events(payload),
                   "nameservers": [], "holder_organisation": holder, "holder_withheld": withheld, **common}
        content["redaction"] = {**common["redaction"], "individual_entities_dropped": dropped}
        if kind == "asn":
            start, end = payload.get("startAutnum"), payload.get("endAutnum")
            try:
                low, high = int(start), int(end if end is not None else start)
            except (TypeError, ValueError) as exc:
                raise InfrastructureError("schema_drift", "autnum states no range") from exc
            if not low <= asn_number(value) <= high:
                raise InfrastructureError("subject_mismatch", "RDAP autnum describes another range")
            content.update(start_autnum=low, end_autnum=high)
            identifiers = {"asn": [value]}
        else:
            try:
                first = ipaddress.ip_address(str(payload.get("startAddress")))
                last = ipaddress.ip_address(str(payload.get("endAddress")))
            except ValueError as exc:
                raise InfrastructureError("schema_drift", "ip network states no address range") from exc
            wanted = ipaddress.ip_network(value)
            if wanted.version != first.version or not (first <= wanted.network_address
                                                       and wanted.broadcast_address <= last):
                raise InfrastructureError("subject_mismatch", "RDAP ip network does not cover the declared prefix")
            content.update(start_address=str(first), end_address=str(last), ip_version=f"v{first.version}")
            identifiers = {"prefix": [value]}
    last_changed = next((e["date"] for e in reversed(content["events"]) if e["action"] == "last changed"), None)
    stamp = iso_time(last_changed)
    return {
        "record_type": "registry-record", "provider": "rdap", "object_kind": RDAP_OBJECT_KINDS[kind],
        "native_id": value, "resource": dict(resource), "state": "published",
        "revision": {"basis": "last_changed" if stamp else "content_digest", "source_revision": stamp,
                     "valid_from": stamp},
        "content": content, "stated_identifiers": identifiers,
        "label": "registration as stated by the RDAP server the IANA bootstrap names",
    }


def rdap_removed(resource: Mapping[str, str], base: str) -> dict[str, Any]:
    host = (urlsplit(base).hostname or "").casefold()
    return {
        "record_type": "registry-record", "provider": "rdap", "object_kind": RDAP_OBJECT_KINDS[resource["kind"]],
        "native_id": resource["value"], "resource": dict(resource), "state": "removed_by_source",
        "revision": {"basis": "not_found", "source_revision": "HTTP 404", "valid_from": None},
        "content": {"status_as_answered": "HTTP 404 for a declared object", "bootstrap_target": base,
                    "rir": RIR_BY_HOST.get(host)},
        "stated_identifiers": {resource["kind"]: [resource["value"]]},
        "label": "registration as stated by the RDAP server the IANA bootstrap names",
    }


# ------------------------------------------------------------------ parsing: crt.sh and the CT log list


def parse_crtsh_certificates(raw: bytes, *, domain: str) -> list[dict[str, Any]]:
    """Issuance records for one exact domain through ``parse_crtsh``; over 200 is budget_exhausted, never truncated."""
    from src.ingestion import crtsh
    from src.ingestion import osint_observations as obs

    cap = CAPS["crtsh"]["certificates"]
    try:
        facts = crtsh.parse_crtsh(raw, domain, max_results=cap)
    except obs.ObservationError as exc:
        raise InfrastructureError("schema_drift", str(exc)) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise InfrastructureError("schema_drift", "crt.sh answer is not JSON") from exc
    if facts["truncated"]:
        raise InfrastructureError("budget_exhausted", f"crt.sh states more than {cap} certificates for the domain; "
                                                      "the answer is refused, never truncated")
    log_ids: dict[str, list[str]] = {}
    for row in json.loads(raw or b"[]"):
        if isinstance(row, Mapping) and isinstance(row.get(CRTSH_LOG_ID_FIELD), list):
            log_ids[str(row.get("id"))] = sorted(str(x) for x in row[CRTSH_LOG_ID_FIELD])
    records = []
    for cert in facts["certificates"]:
        stamp = iso_time(cert.get("entry_timestamp")) or iso_time(cert.get("not_before"))
        content = {"crtsh_id": cert["crtsh_id"], "issuer": cert.get("issuer"), "not_before": cert.get("not_before"),
                   "not_after": cert.get("not_after"), "entry_timestamp": cert.get("entry_timestamp"),
                   "serial_number": cert.get("serial_number"), "dns_sans": list(cert.get("san") or []),
                   "log_ids": log_ids.get(cert["crtsh_id"]),
                   # crt.sh's JSON states no subject organisation; the subject CN and every other subject field are
                   # never stored (II01 minimisation).
                   "subject_organisation": None}
        records.append({
            "record_type": "registry-record", "provider": "crtsh", "object_kind": "certificate",
            "native_id": cert["crtsh_id"], "resource": {"kind": "domain", "value": domain}, "state": "published",
            "revision": {"basis": "entry_timestamp" if stamp else "content_digest", "source_revision": stamp,
                         "valid_from": stamp},
            "content": content, "stated_identifiers": {"domain": [domain]},
            "label": "certificate issuance fact as crt.sh states it; revocation never inferred",
        })
    return records


def minimise_subject(dn: Any) -> str | None:
    """Only the organisation (``O``) attributes of a certificate subject DN; everything else is dropped."""
    kept = []
    for part in re.split(r",(?=\s*[A-Za-z.0-9]+=)", str(dn or "")):
        key, _, value = part.strip().partition("=")
        if key.strip().upper() == "O" and value.strip():
            kept.append(value.strip())
    return ", ".join(kept) or None


def parse_ct_log_list(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every log of a CT log list v3 file as a registry record; the operators' e-mail addresses are never read."""
    if len(raw) > CAPS["ct-log-list"]["max_bytes"]:
        raise InfrastructureError("budget_exhausted", "the CT log list is larger than 2 MB")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InfrastructureError("schema_drift", "the CT log list is not JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("operators"), list):
        raise InfrastructureError("schema_drift", "the CT log list states no operators")
    version = str(payload.get("version") or "") or None
    stamp = iso_time(payload.get("log_list_timestamp"))
    if not version or not stamp:
        raise InfrastructureError("schema_drift", "the CT log list states no version or log_list_timestamp")
    records, seen = [], set()
    for operator in payload["operators"]:
        operator = dict(operator) if isinstance(operator, Mapping) else {}
        for log_type in ("logs", "tiled_logs"):
            for log in operator.get(log_type) or []:
                log = dict(log)
                log_id = str(log.get("log_id") or "")
                if not log_id or log_id in seen:
                    raise InfrastructureError("schema_drift", "a CT log states no log_id or repeats one")
                seen.add(log_id)
                states = dict(log.get("state") or {})
                if len(states) != 1:
                    raise InfrastructureError("schema_drift", "a CT log states one state")
                ((state_name, state_detail),) = states.items()
                content = {"log_id": log_id, "description": log.get("description"), "operator": operator.get("name"),
                           "log_type": "tiled" if log_type == "tiled_logs" else "rfc6962",
                           "url": log.get("url") or log.get("submission_url"),
                           "monitoring_url": log.get("monitoring_url"), "mmd": log.get("mmd"),
                           "state": {"name": state_name, "timestamp": dict(state_detail or {}).get("timestamp")},
                           # The list version dates the revision (source_revision); it is not log content, so
                           # a log whose statement is unchanged in a new list version is not a new revision.
                           "temporal_interval": log.get("temporal_interval")}
                records.append({
                    "record_type": "registry-record", "provider": "ct-log-list", "object_kind": "ct-log",
                    "native_id": log_id, "resource": {"kind": "ct-log-list", "value": "v3"}, "state": "published",
                    "revision": {"basis": "log_list_version", "source_revision": version, "valid_from": stamp},
                    "content": content, "stated_identifiers": {"ct_log_id": [log_id]},
                    "label": "CT log as the Chrome CT log list v3 states it",
                })
    records.sort(key=lambda r: r["native_id"])
    return records, {"version": version, "log_list_timestamp": stamp}


def listing(provider: str, object_kind: str, key: str, members: Sequence[Any],
            resource: Mapping[str, str]) -> dict[str, Any]:
    """A complete listing: a later complete listing without a member records that member removed_by_source."""
    return {"record_type": "listing", "provider": provider, "object_kind": object_kind, "listing_key": key,
            "members": sorted(str(m) for m in members), "resource": dict(resource), "complete": True}


# ------------------------------------------------------------------ adapter


class InternetInfrastructureAdapter:
    """Fetch the declared resources on the runtime's default transport; one page (one unit) per declared resource."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None, now_ms: Callable[[], int] | None = None,
                 bootstrap_cache: dict[str, Any] | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = infrastructure_declaration(self.source)
        self.provider = self.declared["provider"]
        # Only PeeringDB may carry the optional key; no other provider ever sends a credential.
        self.secret = secret if self.provider == "peeringdb" else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        # The bootstrap files are read once per file per 24 h across runs in this process (rdap.BOOTSTRAP_TTL_MS).
        self.bootstrap_cache = BOOTSTRAP_CACHE if bootstrap_cache is None else bootstrap_cache
        self._last_request: float | None = None
        self._last_origin: str | None = None
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "internet_infrastructure": {"provider": self.provider, "units": len(self.declared["units"]),
                                        "caps": CAPS[self.provider], "keyed": bool(self.secret),
                                        "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "internet-infrastructure runs fetch the declared resources "
                                                         "only")

    # -------------------------------------------------------------- transport

    def _pace(self) -> None:
        interval = 1.0 / CAPS["ripestat"]["requests_per_second"] if self.provider == "ripestat" else 0.0
        if interval and self._last_request is not None and self._last_origin != "fixture":
            wait = interval - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)

    def _get(self, url: str, params: Mapping[str, Any], *, allow_not_found: bool = False,
             max_bytes: int | None = None) -> tuple[bytes, str, int]:
        """One bounded GET on a declared host; one retry on HTTP 429; a 404 only where a removal is meaningful."""
        from src.ingestion.source_pack_runtime import _retry_after_ms

        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        if parts.scheme != "https" or host not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("network_policy", "requests stay on the provider's declared hosts")
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Api-Key {self.secret}"
        limit = int(max_bytes or self.definition["limits"]["max_bytes"])
        response: Mapping[str, Any] = {}
        for attempt in range(1 + CAPS[self.provider]["retries_on_429"]):
            self._pace()
            response = self.transport(url=url, params=dict(params), headers=dict(headers),
                                      timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
            self._last_request = self.clock()
            self._last_origin = "fixture" if response.get("origin") == "fixture" else "live"
            status = int(response.get("status", 200))
            if status != 429:
                break
            response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            retry_after = _retry_after_ms(response_headers.get("retry-after"))
            if attempt + 1 >= 1 + CAPS[self.provider]["retries_on_429"]:
                raise SourcePackError("rate_limited", "provider quota is temporarily exhausted (HTTP 429 after one "
                                                      "retry)", retry_after_ms=retry_after)
            if self._last_origin != "fixture":
                self.sleep(min(max((retry_after or 1000) / 1000, 1.0), 5.0))
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > limit:
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError("schema_drift", "provider echoed a credential; response is not safe evidence")
        if status == 404 and allow_not_found:
            return raw, self._last_origin, 404
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, self._last_origin, status

    # -------------------------------------------------------------- units

    def _ripestat(self, resource, fetch) -> list[dict[str, Any]]:
        items = []
        for request in ripestat_requests(resource, self.declared["selection"]):
            raw = fetch(request["url"], request["params"])
            items.append(parse_ripestat(raw, call=request["call"], resource=resource, params=request["params"]))
        return items

    def _peeringdb(self, resource, fetch) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        asn = asn_number(resource["value"])
        nets = _peeringdb_rows(fetch(f"{PEERINGDB_API}/net", {"asn": str(asn), "depth": "0"}), "net")
        if len(nets) > CAPS["peeringdb"]["networks"]:
            raise InfrastructureError("budget_exhausted", "more than one PeeringDB network for the declared ASN")
        items = [listing("peeringdb", "net", f"peeringdb:net?asn={asn}", [n["id"] for n in nets], resource)]
        coverage: dict[str, Any] = {"ix_not_fetched": [], "network_found": bool(nets)}
        for net in nets:
            if normalize_asn(net.get("asn")) != resource["value"]:
                raise InfrastructureError("subject_mismatch", "PeeringDB answered a network of another ASN")
            items.append(peeringdb_record("net", net, resource))
            if net.get("org_id"):
                raw, status = fetch(f"{PEERINGDB_API}/org/{int(net['org_id'])}", {"depth": "0"}, not_found=True)
                if status == 404:
                    items.append(peeringdb_removed("org", net["org_id"], resource))
                else:
                    items.append(peeringdb_record("org", _one(_peeringdb_rows(raw, "org"), "org"), resource))
            rows = _peeringdb_rows(fetch(f"{PEERINGDB_API}/netixlan", {"net_id": str(int(net["id"])),
                                                                       "depth": "0"}), "netixlan")
            if len(rows) > CAPS["peeringdb"]["netixlan_rows"]:
                raise InfrastructureError("budget_exhausted", "more netixlan rows than the 20 row cap; refused, never "
                                                              "truncated")
            items.append(listing("peeringdb", "netixlan", f"peeringdb:netixlan?net_id={int(net['id'])}",
                                 [r["id"] for r in rows], resource))
            items += [peeringdb_record("netixlan", row, resource) for row in rows]
            ix_ids = sorted({int(r["ix_id"]) for r in rows if r.get("ix_id") and r.get("status") != "deleted"})
            coverage["ix_not_fetched"] = ix_ids[CAPS["peeringdb"]["ixs"]:]
            for ix_id in ix_ids[:CAPS["peeringdb"]["ixs"]]:
                raw, status = fetch(f"{PEERINGDB_API}/ix/{ix_id}", {"depth": "0"}, not_found=True)
                if status == 404:
                    items.append(peeringdb_removed("ix", ix_id, resource))
                    continue
                items.append(peeringdb_record("ix", _one(_peeringdb_rows(raw, "ix"), "ix"), resource))
        for fac_id in self.declared["selection"].get("facilities") or []:
            raw, status = fetch(f"{PEERINGDB_API}/fac/{int(fac_id)}", {"depth": "0"}, not_found=True)
            if status == 404:
                items.append(peeringdb_removed("fac", fac_id, resource))
                continue
            items.append(peeringdb_record("fac", _one(_peeringdb_rows(raw, "fac"), "fac"), resource))
        return items, coverage

    def _rdap(self, resource, fetch, raw_transport) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from src.ingestion import osint_observations as obs
        from src.ingestion import rdap

        kind = {"asn": "asn", "domain": "dns"}.get(resource["kind"])
        if kind is None:
            kind = "ipv4" if ipaddress.ip_network(resource["value"]).version == 4 else "ipv6"
        before = dict(self.bootstrap_cache.get(f"bootstrap:{kind}") or {}) if kind != "dns" else \
            {"fetched_at_ms": self.bootstrap_cache.get("fetched_at_ms")}
        try:
            table = rdap.bootstrap_table(raw_transport, now_ms=self.now_ms(),
                                         timeout_s=int(self.definition["limits"]["timeout_ms"]) / 1000,
                                         cache=self.bootstrap_cache, kind=kind)
            if kind == "dns":
                url = rdap.rdap_url(resource["value"], table)
                base = url[: url.index("domain/")]
            else:
                base = rdap.bootstrap_base(kind, resource["value"], table)
        except obs.ObservationError as exc:
            code = "source_unavailable" if exc.code == "bootstrap_unavailable" else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        after = self.bootstrap_cache.get(f"bootstrap:{kind}") if kind != "dns" else self.bootstrap_cache
        fetched_now = (after or {}).get("fetched_at_ms") != before.get("fetched_at_ms")
        url = rdap_object_url(resource["kind"], resource["value"], base)
        raw, status = fetch(url, {}, not_found=True)
        item = rdap_removed(resource, base) if status == 404 else parse_rdap_registration(raw, resource=resource,
                                                                                         base=base)
        return [item], {"bootstrap_file": rdap.BOOTSTRAP_URLS[kind], "bootstrap_fetched": fetched_now,
                        "bootstrap_target": base}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        units = self.declared["units"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(units):
            raise SourcePackError("cursor_drift", "cursor names no declared resource")
        resource = dict(units[index])
        files: list[dict[str, Any]] = []
        origins: set[str] = set()
        total = 0

        def fetch(url, params, *, not_found=False, max_bytes=None):
            nonlocal total
            raw, origin, status = self._get(url, params, allow_not_found=not_found, max_bytes=max_bytes)
            total += len(raw)
            origins.add(origin)
            public = {k: v for k, v in sorted(dict(params).items())}
            files.append({"url": url + ("?" + urlencode(public) if public else ""), "status": status,
                          "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            return (raw, status) if not_found else raw

        def raw_transport(*, url, params, headers, timeout, max_bytes):
            del headers, timeout
            raw, origin, status = self._get(url, params, max_bytes=max_bytes)
            origins.add(origin)
            files.append({"url": url, "status": status, "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "bootstrap": True})
            return {"status": status, "content": raw}

        coverage: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        try:
            if self.provider == "ripestat":
                items = self._ripestat(resource, fetch)
            elif self.provider == "peeringdb":
                items, coverage = self._peeringdb(resource, fetch)
            elif self.provider == "rdap":
                items, coverage = self._rdap(resource, fetch, raw_transport)
            elif self.provider == "crtsh":
                raw = fetch(CRTSH_URL, {"q": resource["value"], "output": "json"})
                items = parse_crtsh_certificates(raw, domain=resource["value"])
                coverage = {"certificates": len(items), "cap": CAPS["crtsh"]["certificates"]}
            else:
                raw = fetch(CT_LOG_LIST_URL, {}, max_bytes=CAPS["ct-log-list"]["max_bytes"])
                items, extra = parse_ct_log_list(raw)
                items = [listing("ct-log-list", "ct-log", "ct-log-list:v3", [i["native_id"] for i in items],
                                 resource), *items]
        except InfrastructureError as exc:
            code = exc.code if exc.code in {"budget_exhausted", "subject_mismatch"} else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated unit: a missing record would read as one the source removed.
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if origins <= {"fixture"} else "live"
        header = {
            "contract": UNIT_CONTRACT, "provider": self.provider, "source_id": self.source["source_id"],
            "unit_key": f"{self.provider}:{resource_key(resource)}", "resource": resource,
            "requests": len(files), "files": files, "content_sha256": digest(items), "item_count": len(items),
            "complete": True, "evidence_origin": origin, "coverage": coverage, **extra,
            "live_verification": LIVE_VERIFICATION[self.provider]["status"], "keyed": bool(self.secret),
        }
        # The unit's identity is its answers, not whether a cached bootstrap file had to be re-read.
        header["unit_sha256"] = digest([header["unit_key"], [f["sha256"] for f in files if not f.get("bootstrap")]])
        records = [{
            "id": f"{header['unit_sha256'][:16]}:{number}",
            "title": f"{self.provider} {item.get('object_kind')} {item.get('native_id') or item.get('listing_key')}",
            "url": files[-1]["url"] if files else self.source["endpoint"], "language": "en",
            "published_at": item.get("stated_time") or dict(item.get("revision") or {}).get("valid_from"),
            "content": canonical(item), "ii_unit": header, "ii_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": self.provider, "resource": resource_key(resource),
                   "requests": len(files), "items": len(records), "keyed": bool(self.secret),
                   "evidence_origin": origin, "unit_sha256": header["unit_sha256"],
                   "final_page": index + 1 >= len(units)}
        next_cursor = str(index + 1) if index + 1 < len(units) else None
        return RuntimePage(tuple(records), next_cursor, total, receipt=receipt)


ADAPTERS = {CONNECTOR: InternetInfrastructureAdapter}


# ------------------------------------------------------------------ live transport and fixtures


def durable_transport(http: Any, *, principal_id: str, run_key: str) -> Callable[..., Mapping[str, Any]]:
    """A transport over :class:`~src.ingestion.provider_execution.DurableHTTP` (exact hosts, budget, receipts).

    The PeeringDB key travels as a secret header and never enters the durable request description; a 404 answer is
    returned as a status so a declared object can be recorded ``removed_by_source``."""
    from src.ingestion.provider_execution import ProviderError

    counter = {"n": 0}

    def transport(*, url, params, headers, timeout, max_bytes=None):
        counter["n"] += 1
        secret = {k: v for k, v in dict(headers).items() if k.casefold() == "authorization"}
        public = {k: v for k, v in dict(headers).items() if k not in secret}
        try:
            captured = http.request(f"{run_key}:{counter['n']}", url, principal_id=principal_id,
                                    params=dict(params), headers=public, secret_headers=secret or None,
                                    max_bytes=int(max_bytes or 2_000_000), timeout_s=float(timeout))
        except ProviderError as exc:
            if str(exc.code).startswith("http_"):
                return {"status": int(str(exc.code)[5:]), "headers": {}, "content": b""}
            raise SourcePackError("source_unavailable", f"{exc.code}: provider acquisition failed") from exc
        return {"status": 200, "headers": {}, "content": captured.content}

    return transport


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}" + ("?" + query if query else "")


def fixture_request(url: str, params: Mapping[str, Any] | None = None) -> str:
    """The key :func:`fixture_transport` files a response under (host, path and sorted query; never a credential)."""
    return _fixture_key(url, params or {})


def fixture_transport(pages: Sequence[Mapping[str, Any]], *,
                      calls: list[dict[str, Any]] | None = None) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by host, path and sorted query; a list of bodies answers in turn."""
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for page in pages:
        by_key.setdefault(page["request"], []).append(page)
    used: dict[str, int] = {}

    def transport(*, url, params, headers, timeout, max_bytes=None):
        del timeout, max_bytes
        key = _fixture_key(url, params)
        if calls is not None:
            calls.append({"key": key, "headers": dict(headers)})
        answers = by_key.get(key)
        if not answers:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        page = answers[min(used.get(key, 0), len(answers) - 1)]
        used[key] = used.get(key, 0) + 1
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture", "final_url": url}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = InternetInfrastructureAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                            secret=FIXTURE_SECRET, sleep=lambda _s: None, bootstrap_cache={},
                                            now_ms=lambda: 4_102_444_800_000)  # 2100-01-01: fixed for replay
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CAPS",
    "CONNECTOR",
    "DATA_CALL_VERSIONS",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORBIDDEN_CALLS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "RIPESTAT_CALLS",
    "SECRET_REF",
    "InfrastructureError",
    "InternetInfrastructureAdapter",
    "classify_resource",
    "durable_transport",
    "fixture_request",
    "fixture_transport",
    "infrastructure_declaration",
    "minimise_subject",
    "normalize_asn",
    "normalize_domain",
    "normalize_prefix",
    "parse_crtsh_certificates",
    "parse_ct_log_list",
    "parse_rdap_registration",
    "parse_ripestat",
    "replay_native_fixture",
    "ripestat_requests",
    "unverified",
]

"""
NeuroNews OSINT composition - MCP server (R10 / Track OSINT phase 1).

Defensive, analytical primitives over already-ingested public documents, each a
pure composition of layers Noesis already builds. Nothing here crawls, targets
or de-anonymizes; the tools only read the warehouse.

Tools (all annotated for R2 discovery under the `osint` ui_flag):
  corroborate(claim_id)              -> publication and probable-origin counts
                                        for/against, weighted by credibility
  origin_signals(document_id)        -> extracted provenance signals only
  evidence_origin_graph(document_ids?)-> probable origins + typed relations
  source_reliability(source)         -> reliability card: transparency,
                                        corroboration hit-rate, corrections
  contradiction_scan(topic?, entity?)-> contradiction ledger: cited CONTRADICTS
                                        pairs, uncited flagged not hidden
  infrastructure_pivot(identifier)   -> organization-keyed pivot over cited
                                        RDAP/CT source-identity relations;
                                        person-keyed identifiers refused
  movement_source_contracts()        -> movement licence and volume decisions;
                                        movement_registry / movement_window /
                                        movement_calls only with
                                        NOESIS_OSINT_MOVEMENTS (the latter two
                                        also behind the review gate)

Design constraints (as for every tool server): stdlib + fastmcp (plus the
stdlib-only honesty helper) at import time, lazy imports inside tools, the
warehouse opened READ-ONLY.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analytics.honesty import INTERVAL_SCHEMA, honesty_output_schema  # noqa: E402

mcp = FastMCP("noesis-osint")


def _warehouse_ro():
    import duckdb

    from src.config.env import warehouse_path
    path = warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


def _context() -> tuple:
    """Caller principal and scopes (access token, else NOESIS_MCP_PRINCIPAL /
    NOESIS_MCP_SCOPES), mirroring the knowledge-engine server. Record owners
    such as the ownership store authorize against these; nothing is granted here."""
    from fastmcp.server.dependencies import get_access_token

    from src.config.env import resolve_env

    token = get_access_token()
    if token is not None and token.scopes:
        return str(token.client_id or ""), set(token.scopes)
    principal = (resolve_env("MCP_PRINCIPAL", "local-reader") or "").strip()
    raw = resolve_env("MCP_SCOPES", "knowledge:read") or ""
    return principal, {value.strip() for value in raw.split(",") if value.strip()}


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "claim": {"type": "object"},
            "support": {"type": "array"},
            "contradict": {"type": "array"},
            "independent_support_count": {"type": "integer"},
            "independent_contradict_count": {"type": "integer"},
            "publication_support_count": {"type": "integer"},
            "publication_contradict_count": {"type": "integer"},
            "probable_origin_support_count": {"type": "integer"},
            "probable_origin_contradict_count": {"type": "integer"},
            "unresolved_support_count": {"type": "integer"},
            "unresolved_contradict_count": {"type": "integer"},
            "independence": {"type": "object"},
            "weighted_support": {"type": "number"},
            "weighted_contradict": {"type": "number"},
            "single_sourced": {"type": "boolean"},
            "credibility_grade": {"type": "object"},
            "source_reliability_grade": {"type": ["object", "null"]},
            "grading": {"type": "object"},
        }
    ),
)
def corroborate(claim_id: str) -> dict:
    """How many probable origins support or contradict a claim, alongside
    publication counts and credibility. Never collapses to one confidence.

    Two Admiralty-style grades are reported side by side as independent axes,
    each with its derivation: ``credibility_grade`` rates the information 1-6
    (1 confirmed by independent origins ... 5 improbable, 6 cannot be judged,
    e.g. single-sourced or unresolved lineage) and ``source_reliability_grade``
    rates the carrying source A-F (F when its track record is too thin). They
    are never fused into one score.

    Args:
        claim_id: the claim to corroborate (see argument_mcp.list_claims).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import corroborate as _corroborate

        return _corroborate(con, claim_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool()
def origin_signals(document_id: str) -> dict:
    """Read deterministic provenance signals independently of clustering."""
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint.independence import document_signals

        return document_signals(con, document_id) or {
            "document_id": document_id,
            "status": "not_available",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool()
def evidence_origin_graph(document_ids: Optional[list[str]] = None) -> dict:
    """Current probable reporting origins and explainable document relations."""
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint.independence import origin_graph

        return origin_graph(con, document_ids)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "source": {"type": "string"},
            "found": {"type": "boolean"},
            "reliability": INTERVAL_SCHEMA,
            "components": {"type": "object"},
            "track_record": {"type": "object"},
            "corroboration": {"type": "object"},
            "corrections": {"type": "object"},
            "lineage": {"type": "object"},
            "scored_as_outlet": {"type": "boolean"},
            "reliability_grade": {"type": "object"},
        }
    ),
)
def source_reliability(source: str) -> dict:
    """Reliability card for any source (blog, paper venue, filing, outlet),
    scored the same way outlets are.

    ``reliability_grade`` is an Admiralty source-reliability letter A-F derived
    from transparency, corroboration hit-rate and correction history with the
    thresholds shown; F ("cannot be judged") below the minimum track record.
    It grades the source only; information credibility (1-6) is a separate
    axis reported per claim by ``corroborate``.

    Args:
        source: the source name to vet (see sources_mcp.list_sources).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import source_reliability as _reliability

        return _reliability(con, source)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "contradictions": {"type": "array"},
            "count": {"type": "integer"},
            "uncited_count": {"type": "integer"},
            "topic": {"type": ["string", "null"]},
            "entity": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
)
def contradiction_scan(
    topic: Optional[str] = None, entity: Optional[str] = None
) -> dict:
    """Contradiction pairs on a topic or entity, each cited back to its source
    document. Uncited entries are flagged, not dropped.

    Args:
        topic: optional topic filter (conflict topic or claim-text substring).
        entity: optional entity filter (substring of either claim's text).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import contradiction_scan as _scan

        return _scan(con, topic=topic, entity=entity)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Investigation surface (R11 / Track OSINT phase 2)
# --------------------------------------------------------------------------- #

@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "is_person": {"type": "boolean"},
            "found": {"type": "boolean"},
            "mention_count": {"type": "integer"},
            "uncited_count": {"type": "integer"},
            "aliases": {"type": "array"},
            "first_seen": {"type": ["string", "null"]},
            "last_seen": {"type": ["string", "null"]},
            "mentions": {"type": "array"},
            "connected_entities": {"type": "array"},
            "ownership": {"type": "object"},
        },
        "additionalProperties": True,
    },
)
def entity_dossier(
    entity: str,
    entity_type: Optional[str] = None,
    ownership_namespace: Optional[str] = None,
    as_of: Optional[str] = None,
    designations_namespace: Optional[str] = None,
    onchain_namespace: Optional[str] = None,
) -> dict:
    """A cited entity brief from already-ingested public documents only. A
    person entity with no ingested document is refused (person guardrail).

    Optional ``ownership`` feature (off unless ``ownership_namespace`` is
    given): for an organization, a cited section from the Corporate Ownership
    bundle (identity, direct/ultimate parents, subsidiaries, officers,
    conflicts side by side). Resolved only via accepted identity decisions,
    never by name; inert when the bundle is disabled; never for a person.

    Optional ``designations`` feature (off unless ``designations_namespace``
    is given): what each sanctions list (EU, UN, OFAC, UK) stated about the
    entity as of the date, per list, citing snapshots, listing revisions and
    legal-basis works. Resolved only via accepted identity decisions; inert
    unless the Legal sanctions feature is enabled. List statements only, not a
    screening verdict or compliance determination; for a person only the list
    record's own statement, and only under the person guardrail.

    Args:
        entity: the entity name or id (see kg_mcp.list_entities).
        entity_type: optional type hint (e.g. "person") to enforce the guardrail.
        ownership_namespace: namespace whose ownership records to compose.
        as_of: as-of date (YYYY-MM-DD) for ownership and designations; today by default.
        designations_namespace: namespace whose Legal sanctions records to compose.
        onchain_namespace: namespace whose On-chain Observations records to compose
            (optional ``onchain`` feature: cited contract-origin facts for an
            organization given by canonical id, through accepted label references
            only; never for a person, never an attribution of an address).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import entity_dossier as _dossier

        ownership = None
        if ownership_namespace:
            principal, scopes = _context()
            ownership = {
                "namespace": ownership_namespace,
                "principal_id": principal,
                "scopes": scopes,
                "as_of": as_of,
            }
        designations = None
        if designations_namespace:
            principal, scopes = _context()
            designations = {
                "namespace": designations_namespace,
                "principal_id": principal,
                "scopes": scopes,
                "as_of": as_of,
            }
        return _dossier(
            con,
            entity,
            entity_type=entity_type,
            ownership=ownership,
            designations=designations,
            onchain={"namespace": onchain_namespace, "scopes": _context()[1]} if onchain_namespace else None,
        )
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "connected": {"type": "boolean"},
            "a": {"type": "string"},
            "b": {"type": "string"},
            "path": {"type": "array"},
            "hops": {"type": "integer"},
            "edges": {"type": "array"},
            "resolution": {"type": "object"},
            "ambiguous": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def relationship_path(a: str, b: str) -> dict:
    """The shortest co-mention path between two entities, with cited evidence
    on every edge.

    Args:
        a: the first entity name.
        b: the second entity name.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import relationship_path as _path

        return _path(con, a, b)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "events": {"type": "array"},
            "count": {"type": "integer"},
            "claim_count": {"type": "integer"},
            "topic": {"type": ["string", "null"]},
            "entity": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
)
def timeline_reconstruct(
    topic: Optional[str] = None, entity: Optional[str] = None
) -> dict:
    """A cited event timeline for a topic or entity, each event with its
    corroboration density.

    Args:
        topic: optional topic filter (claim-text substring).
        entity: optional entity filter (an actor in the corpus).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import timeline_reconstruct as _timeline

        return _timeline(con, topic=topic, entity=entity)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "artifact": {"type": "object"},
            "cited": {"type": "boolean"},
            "chain": {"type": "array"},
            "stage_count": {"type": "integer"},
            "claims": {"type": "array"},
        },
        "additionalProperties": True,
    },
)
def trace_artifact(
    claim_id: Optional[str] = None, document_id: Optional[str] = None
) -> dict:
    """Trace one artifact (a claim or a document) end to end: source to
    connector to document to enrichment to claim to routed namespace, every
    stage cited. Answers "where did this come from and what happened to it".
    An RDAP / certificate-transparency observation id (``osint-obs:...``) traces
    source-pack source -> acquisition receipt -> observation -> the
    source-identity revisions and relationships that cite it.

    Args:
        claim_id: trace a claim (see argument_mcp.list_claims).
        document_id: trace a document (a news_articles id) or an osint-obs: observation id.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import trace_artifact as _trace

        return _trace(con, claim_id=claim_id, document_id=document_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "identifier": {"type": "string"},
            "resolution": {"type": "object"},
            "paths": {"type": "array"},
            "count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "caveat": {"type": "string"},
        },
        "additionalProperties": True,
    },
)
def infrastructure_pivot(identifier: str, namespace: str = "osint", max_depth: int = 2) -> dict:
    """Organization-keyed infrastructure pivot (domain -> registrant
    organization -> related domains; shared certificate SAN sets) over cited
    source-identity relationships. Every hop cites its RDAP / CT observation and
    carries its status (shared infrastructure is "probable"). No same-operator
    verdict. Person-keyed identifiers (e-mail, @handle, username, IP address,
    person: ids) are refused with person_identifier_refused.

    Args:
        identifier: a domain name or a source-identity id.
        namespace: the source-identity namespace to read.
        max_depth: hops to follow (1-3).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint.infrastructure import infrastructure_pivot as _pivot

        return _pivot(con, identifier, namespace=namespace, max_depth=max_depth)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def investigation_audit(investigation: str) -> dict:
    """Reconstruct an investigation from its provisioning audit trail: the KG
    record, bound sources, and every logged action in order. An investigation
    is a Track P-provisioned namespaced KG; this replays its trail.

    Args:
        investigation: the investigation (provisioned KG) name.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import investigation_audit as _audit

        return _audit(con, investigation)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Review-gated tools (issue #639 item 3). Absent from the served surface unless
# NOESIS_OSINT_GATED_TOOLS is explicitly turned on, which is the human sign-off
# after the review in docs/security/osint-review-gate.md + docs/security/osint-abuse-analysis.md.
# Purpose limitation is enforced in src/osint/gated.py, not just here.
# --------------------------------------------------------------------------- #

def _gated_enabled() -> bool:
    from src.config.env import resolve_env

    return (resolve_env("OSINT_GATED_TOOLS", "off") or "off").lower() in ("on", "1", "true")


if _gated_enabled():

    @mcp.tool
    def geolocate_claims(
        topic: Optional[str] = None, entity: Optional[str] = None
    ) -> dict:
        """Event geography from claim text (review-gated). Resolves only where an
        event is reported to have happened, cited and flagged unverified; refuses
        to geolocate a person.

        Args:
            topic: optional topic filter.
            entity: optional entity filter; a person entity is refused.
        """
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.gated import geolocate_claims as _geo

            return _geo(con, topic=topic, entity=entity)
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()

    @mcp.tool
    def narrative_coordination(topic: Optional[str] = None) -> dict:
        """Flag cohorts of sources publishing near-identical claims for human
        review (review-gated). Never accuses; every cohort is "warrants review"
        with a caveat that similarity is often coincidental.

        Args:
            topic: optional topic filter.
        """
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.gated import narrative_coordination as _coord

            return _coord(con, topic=topic)
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()

    # -- Track C / C4: imagery external tier (review-queued, no default provider)
    # Least privilege: corpus assets are read from the read-only warehouse; the
    # review-queue write goes to a *separate* store, so the gated imagery tier
    # never holds write access to the corpus warehouse.
    def _imagery_queue_rw():
        import duckdb

        from src.config.env import imagery_queue_path
        return duckdb.connect(imagery_queue_path())

    @mcp.tool
    def reverse_image_search(sha256: str) -> dict:
        """Queue reverse-image-search suggestions for a corpus asset (review-
        gated). No default provider ships, so this returns no_provider_configured
        until one is supplied; results are uncited until an operator confirms.

        Args:
            sha256: a corpus image asset hash (never an operator-supplied photo).
        """
        try:
            con = _warehouse_ro()
            queue = _imagery_queue_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import reverse_image_search as _ris

            return _ris(con, sha256, provider=None, queue_conn=queue)  # no default provider
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()
            queue.close()

    @mcp.tool
    def geolocate_image(sha256: str) -> dict:
        """Queue visible-landmark geolocation hypotheses for a corpus asset
        (review-gated). Suggestion-grade, about the scene not the subject, never
        auto-cited; needs a vision backend to produce anything.

        Args:
            sha256: a corpus image asset hash.
        """
        try:
            con = _warehouse_ro()
            queue = _imagery_queue_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import geolocate_image as _geo

            return _geo(con, sha256, vlm=None, queue_conn=queue)  # no default backend
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()
            queue.close()

    @mcp.tool
    def chronolocate_image(
        sha256: str,
        date_from: str,
        date_to: str,
        suggestion_id: Optional[str] = None,
        hypothesis_lat: Optional[float] = None,
        hypothesis_lon: Optional[float] = None,
        operator: Optional[str] = None,
    ) -> dict:
        """Queue a capture-time suggestion for a corpus image from shadow
        direction/length and local solar geometry (review-gated). The place is
        a confirmed geolocation suggestion or an explicit operator hypothesis,
        never inferred. Uncited and unverified until an operator confirms; about
        the image, never a subject. Needs a shadow estimator; none ships.

        Args:
            sha256: a corpus image asset hash.
            date_from: first date (YYYY-MM-DD) to search.
            date_to: last date (YYYY-MM-DD) to search.
            suggestion_id: a confirmed geolocate_image suggestion supplying the place.
            hypothesis_lat: operator place hypothesis latitude (with lon and operator).
            hypothesis_lon: operator place hypothesis longitude.
            operator: the operator making the hypothesis.
        """
        try:
            con = _warehouse_ro()
            queue = _imagery_queue_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import chronolocate_image as _chrono

            hypothesis = None
            if hypothesis_lat is not None and hypothesis_lon is not None:
                hypothesis = {"lat": hypothesis_lat, "lon": hypothesis_lon, "operator": operator}
            return _chrono(con, sha256, date_from=date_from, date_to=date_to, estimator=None,  # no default
                           suggestion_id=suggestion_id, hypothesis=hypothesis, queue_conn=queue)
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()
            queue.close()

    @mcp.tool
    def reference_imagery(suggestion_id: str, kind: str = "satellite") -> dict:
        """Attach one satellite or street-level reference image for a queued
        geolocation suggestion's place (review-gated). Only for a place already
        attached to a suggestion; no free-form coordinates, no time series.
        References are review aids in the queue store, never citations. No
        default provider ships, so this returns no_provider_configured.

        Args:
            suggestion_id: the queued suggestion whose place to fetch.
            kind: "satellite" or "street-level".
        """
        try:
            queue = _imagery_queue_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import fetch_reference_imagery as _ref

            return _ref(queue, kind=kind, suggestion_id=suggestion_id, provider=None)  # no default provider
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            queue.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "sha256": {"type": "string"},
            "phash": {"type": ["string", "null"]},
            "exif": {"type": "object"},
            "appearances": {"type": "array"},
        },
        "additionalProperties": True,
    },
)
def image_provenance(sha256: str) -> dict:
    """Provenance for one image asset: EXIF (file-claimed), pHash, C2PA, and
    every document the asset appears in.

    Args:
        sha256: the asset's content hash.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.image_reuse import image_provenance as _ip

        return _ip(con, sha256)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "findings": {"type": "array"},
            "finding_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        }
    ),
)
def image_reuse_findings(topic: Optional[str] = None) -> dict:
    """Reuse findings across the corpus: near-duplicate image clusters spanning
    multiple documents, honesty-enveloped and citing each appearance.

    Args:
        topic: optional case-insensitive filter on appearance context.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.image_reuse import find_reuse

        return find_reuse(con, topic=topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "sha256": {"type": "string"},
            "near_duplicates": {"type": "array"},
            "near_duplicate_count": {"type": "integer"},
        }
    ),
)
def image_reuse(sha256: str) -> dict:
    """Near-duplicates of one asset and where each appears.

    Args:
        sha256: the asset's content hash.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.image_reuse import image_reuse as _ir

        return _ir(con, sha256)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Aircraft and vessel movements (#2221, optional `movements` feature). The
# licence and volume decisions are always readable; the registry lookup is
# served only with NOESIS_OSINT_MOVEMENTS on, and the position-bearing tools
# additionally need the review gate (NOESIS_OSINT_GATED_TOOLS), the
# knowledge:osint:movements scope and a stated purpose, which is logged to a
# separate request-log store (the warehouse stays read-only).
# --------------------------------------------------------------------------- #

_MOVEMENT_ANSWER = {"type": "object", "additionalProperties": True}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "contract": {"type": "string"},
            "providers": {"type": "object"},
            "excluded": {"type": "object"},
            "answer_bounds": {"type": "object"},
            "feature": {"type": "object"},
        },
        "additionalProperties": True,
    },
)
def movement_source_contracts() -> dict:
    """Licence, access and volume decisions for the aircraft and vessel movement sources
    (docs/security/osint-movements-access.md): adopted providers with their bounds
    and live-verification status, excluded sources with reasons, and whether the
    optional movements feature and its gated tools are served. Reads no records.
    """
    from src.ingestion.osint_movement_sources import source_contracts
    from src.osint.investigations import MOVEMENT_GATED_TOOLS, MOVEMENT_TOOLS
    from src.osint.movements import movements_enabled

    enabled = movements_enabled()
    return {**source_contracts(),
            "feature": {"id": "movements", "flag": "NOESIS_OSINT_MOVEMENTS", "enabled": enabled,
                        "review_gate": "NOESIS_OSINT_GATED_TOOLS", "review_gate_open": _gated_enabled(),
                        "tools": list(MOVEMENT_TOOLS), "gated_tools": list(MOVEMENT_GATED_TOOLS),
                        "required_scopes": {"movement_registry": ["knowledge:read"],
                                            "movement_window": ["knowledge:read", "knowledge:osint:movements"],
                                            "movement_calls": ["knowledge:read", "knowledge:osint:movements"]}}}


def _movements_enabled() -> bool:
    from src.osint.movements import movements_enabled

    return movements_enabled()


def _movement_log_rw():
    import duckdb

    from src.config.env import resolve_env

    path = resolve_env("OSINT_MOVEMENT_LOG_PATH", str(REPO_ROOT / "data" / "osint_movement_requests.duckdb"))
    return duckdb.connect(path)


def _movement_request(tool: str, identifier: str, start: str, end: str, purpose: str, namespace: str,
                      answer):
    """Run one position-bearing movement request: purpose required, logged with its outcome."""
    from src.osint.movements import MovementError, log_request

    principal, scopes = _context()
    window = {"start": start, "end": end}
    if not str(purpose or "").strip():
        return {"status": "refused", "code": "purpose_required",
                "reason": "state the purpose of the movement request; it is logged with the request"}
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        result = answer(con, scopes)
        outcome = "answered"
    except MovementError as exc:
        result, outcome = exc.as_refusal(), f"refused:{exc.code}"
    except Exception as exc:
        result, outcome = {"error": str(exc)}, "error"
    finally:
        con.close()
    log = _movement_log_rw()
    try:
        result["request_id"] = log_request(log, namespace=namespace, principal_id=principal, tool=tool,
                                           identifier=str(identifier), window=window, purpose=str(purpose).strip(),
                                           scopes=scopes, outcome=outcome)
    finally:
        log.close()
    return result


if _movements_enabled():

    @mcp.tool(output_schema=_MOVEMENT_ANSWER)
    def movement_registry(
        identifier: str, as_of: Optional[str] = None, namespace: str = "osint", scheme: Optional[str] = None
    ) -> dict:
        """Registry state of one aircraft or vessel on a date (optional movements feature): the
        registry revisions valid then, as published and cited, with the identity matches used.
        Natural-person registrants are withheld and never a key; opted-out (LADD/PIA) and
        person-keyed identifiers are refused with a stated reason. No positions.

        Args:
            identifier: ICAO 24-bit address, registration mark, IMO number, MMSI or GFW vessel id.
            as_of: date (YYYY-MM-DD); today by default.
            namespace: the movement records namespace.
            scheme: optional explicit scheme (icao24, registration, imo, mmsi, gfw_vessel_id).
        """
        from src.osint.movements import MovementError, MovementQueries

        _, scopes = _context()
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            return MovementQueries(con).registry(namespace, identifier, scopes=scopes, as_of=as_of, scheme=scheme)
        except MovementError as exc:
            return exc.as_refusal()
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()

    if _gated_enabled():

        @mcp.tool(output_schema=_MOVEMENT_ANSWER)
        def movement_window(
            identifier: str, start: str, end: str, purpose: str, namespace: str = "osint",
            scheme: Optional[str] = None, facilities_namespace: Optional[str] = None,
            fisheries_namespace: Optional[str] = None, sanctions_namespace: Optional[str] = None,
            as_of: Optional[str] = None, export_bundle: bool = False,
        ) -> dict:
            """Registry state and sampled movements of one aircraft or vessel for a bounded window
            (review-gated, optional movements feature; scope knowledge:osint:movements): registry
            revisions, sample windows with gaps and receiver-coverage caveats, source-published and
            derived calls, identity matches and sanctions listing statements, each cited. No coverage
            is reported as "no coverage observed", never as absence of movement. Windows over 92
            days, person-keyed and opted-out identifiers, and aircraft registered to a natural
            person are refused. The purpose is required and logged.

            Args:
                identifier: ICAO 24-bit address, registration, IMO, MMSI or GFW vessel id.
                start: window start (ISO date or UTC timestamp).
                end: window end (at most 92 days after start).
                purpose: why the movement record is needed (logged with the request).
                namespace: the movement records namespace.
                scheme: optional explicit identifier scheme.
                facilities_namespace: geospatial namespace of airport and port facilities.
                fisheries_namespace: Fisheries namespace whose GFW vessel identity to cite.
                sanctions_namespace: Legal sanctions namespace whose listings to cite.
                as_of: listing as-of date; the window end by default.
                export_bundle: also return the answer as an evidence bundle.
            """
            from src.osint.movements import MovementQueries

            def answer(con, scopes):
                queries = MovementQueries(con)
                result = queries.window(namespace, identifier, start, end, scopes=scopes, scheme=scheme,
                                        facilities_namespace=facilities_namespace,
                                        fisheries_namespace=fisheries_namespace,
                                        sanctions_namespace=sanctions_namespace, as_of=as_of)
                if export_bundle:
                    result = {**result, "evidence_bundle": queries.export_bundle(result)}
                return result

            return _movement_request("movement_window", identifier, start, end, purpose, namespace, answer)

        @mcp.tool(output_schema=_MOVEMENT_ANSWER)
        def movement_calls(
            identifier: str, start: str, end: str, purpose: str, namespace: str = "osint",
            scheme: Optional[str] = None, facilities_namespace: Optional[str] = None,
        ) -> dict:
            """Airport and port calls of one aircraft or vessel in a bounded window (review-gated,
            optional movements feature; scope knowledge:osint:movements): source-published calls
            with the source's confidence and derived calls with method, samples and the coverage
            gaps that make them uncertain. The absence of a call is never asserted. Purpose
            required and logged; over-bound, person-keyed and opted-out requests refused.

            Args:
                identifier: ICAO 24-bit address, registration, IMO, MMSI or GFW vessel id.
                start: window start.
                end: window end (at most 92 days after start).
                purpose: why the calls are needed (logged with the request).
                namespace: the movement records namespace.
                scheme: optional explicit identifier scheme.
                facilities_namespace: geospatial namespace of airport and port facilities.
            """
            from src.osint.movements import MovementQueries

            def answer(con, scopes):
                full = MovementQueries(con).window(namespace, identifier, start, end, scopes=scopes, scheme=scheme,
                                                   facilities_namespace=facilities_namespace)
                return {k: full[k] for k in ("contract", "query", "namespace", "identifier", "window", "as_of",
                                             "subjects", "calls", "coverage", "identity_matches", "never",
                                             "answer_hash")}

            return _movement_request("movement_calls", identifier, start, end, purpose, namespace, answer)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

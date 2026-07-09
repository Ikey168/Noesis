"""
NeuroNews OSINT composition - MCP server (R10 / Track OSINT phase 1).

Defensive, analytical primitives over already-ingested public documents, each a
pure composition of layers Noesis already builds. Nothing here crawls, targets
or de-anonymizes; the tools only read the warehouse.

Tools (all annotated for R2 discovery under the `osint` ui_flag):
  corroborate(claim_id)              -> corroboration panel: independent sources
                                        for/against, weighted by credibility
  source_reliability(source)         -> reliability card: transparency,
                                        corroboration hit-rate, corrections
  contradiction_scan(topic?, entity?)-> contradiction ledger: cited CONTRADICTS
                                        pairs, uncited flagged not hidden

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

mcp = FastMCP("neuronews-osint")


def _warehouse_ro():
    import duckdb

    from src.config.env import warehouse_path
    path = warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "claim": {"type": "object"},
            "support": {"type": "array"},
            "contradict": {"type": "array"},
            "independent_support_count": {"type": "integer"},
            "independent_contradict_count": {"type": "integer"},
            "weighted_support": {"type": "number"},
            "weighted_contradict": {"type": "number"},
            "single_sourced": {"type": "boolean"},
        }
    ),
)
def corroborate(claim_id: str) -> dict:
    """How many independent sources support or contradict a claim, and how
    credible they are. Never collapses to a single confidence number.

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
            "scored_as_outlet": {"type": "boolean"},
        }
    ),
)
def source_reliability(source: str) -> dict:
    """Reliability card for any source (blog, paper venue, filing, outlet),
    scored the same way outlets are.

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
        },
        "additionalProperties": True,
    },
)
def entity_dossier(entity: str, entity_type: Optional[str] = None) -> dict:
    """A cited entity brief from already-ingested public documents only. A
    person entity with no ingested document is refused (person guardrail).

    Args:
        entity: the entity name or id (see kg_mcp.list_entities).
        entity_type: optional type hint (e.g. "person") to enforce the guardrail.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import entity_dossier as _dossier

        return _dossier(con, entity, entity_type=entity_type)
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

    Args:
        claim_id: trace a claim (see argument_mcp.list_claims).
        document_id: trace a document (a news_articles id).
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
# after the review in docs/osint-review-gate.md + docs/osint-abuse-analysis.md.
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
    def _warehouse_rw():
        import duckdb

        from src.config.env import warehouse_path
        path = warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))
        return duckdb.connect(path)

    @mcp.tool
    def reverse_image_search(sha256: str) -> dict:
        """Queue reverse-image-search suggestions for a corpus asset (review-
        gated). No default provider ships, so this returns no_provider_configured
        until one is supplied; results are uncited until an operator confirms.

        Args:
            sha256: a corpus image asset hash (never an operator-supplied photo).
        """
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import reverse_image_search as _ris

            return _ris(con, sha256, provider=None)  # no default provider
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()

    @mcp.tool
    def geolocate_image(sha256: str) -> dict:
        """Queue visible-landmark geolocation hypotheses for a corpus asset
        (review-gated). Suggestion-grade, about the scene not the subject, never
        auto-cited; needs a vision backend to produce anything.

        Args:
            sha256: a corpus image asset hash.
        """
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.osint.imagery_gated import geolocate_image as _geo

            return _geo(con, sha256, vlm=None)  # no default backend
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()


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


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

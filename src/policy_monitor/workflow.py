"""Canonical end-to-end policy monitor and stale-guidance comparison.

The reusable core is the comparison of versioned public assertions with
authorized private guidance. Public reads use public domain membership only;
they do not return redaction markers, hidden counts, or any other signal that a
private corpus exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "noesis-policy-monitor-v1"
DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "examples/policy-monitor/corpus.json"
DEFAULT_DOMAINS = Path(__file__).resolve().parents[2] / "examples/policy-monitor/domains.yml"
PRINCIPAL = "policy-monitor-operator"

_DDL = """
CREATE SEQUENCE IF NOT EXISTS policy_monitor_audit_sequence START 1;
CREATE TABLE IF NOT EXISTS policy_monitor_lineage (
    scenario_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    stage TEXT NOT NULL,
    transformation TEXT NOT NULL,
    PRIMARY KEY (scenario_id, document_id, stage)
);
CREATE TABLE IF NOT EXISTS policy_monitor_audit (
    sequence BIGINT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    include_private BOOLEAN NOT NULL,
    at_ms BIGINT NOT NULL,
    details_json TEXT NOT NULL
);
"""


class PolicyMonitorError(ValueError):
    """A policy monitor request violates authorization or fixture contracts."""


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contract") != "noesis-policy-monitor-fixture-v1":
        raise PolicyMonitorError("unsupported policy monitor fixture")
    if payload.get("license") != "CC0-1.0" or payload.get("synthetic") is not True:
        raise PolicyMonitorError("offline fixtures must be synthetic and CC0-1.0")
    return payload


def ensure_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _locator(
    document_id: str,
    source: str,
    url: str | None,
    *,
    path: str,
    visibility: str = "public",
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "claim_id": None,
        "source": source,
        "url": url,
        "path": path,
        "cited": True,
        "visibility": visibility,
    }


def _audit(
    conn: Any,
    scenario_id: str,
    principal_id: str,
    action: str,
    *,
    include_private: bool,
    at_ms: int,
    details: Mapping[str, Any] | None = None,
) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO policy_monitor_audit VALUES "
        "(nextval('policy_monitor_audit_sequence'), ?, ?, ?, ?, ?, ?)",
        [
            scenario_id,
            principal_id,
            action,
            include_private,
            at_ms,
            json.dumps(dict(details or {}), sort_keys=True),
        ],
    )


def grant_private_access(
    conn: Any,
    principal_id: str,
    *,
    domain: str = "clean-heat-private",
    granted_at_ms: int = 0,
) -> dict[str, Any]:
    from src.kb.watches import grant_watch_domain

    result = grant_watch_domain(
        conn, principal_id, domain, granted_at_ms=granted_at_ms
    )
    return {**result, "scope": "policy-monitor-private-guidance"}


def _authorized(conn: Any, principal_id: str, domain: str) -> bool:
    from src.kb.watches import ensure_watch_schema

    ensure_watch_schema(conn)
    return (
        conn.execute(
            "SELECT 1 FROM claim_watch_domain_grants WHERE principal_id = ? AND domain = ?",
            [principal_id, domain],
        ).fetchone()
        is not None
    )


def _insert_lineage(
    conn: Any,
    scenario_id: str,
    document_id: str,
    connector: str,
    transformation: str,
) -> None:
    for stage in ("discover", "fetch", "parse", "document-store"):
        conn.execute(
            "INSERT OR REPLACE INTO policy_monitor_lineage VALUES (?, ?, ?, ?, ?)",
            [scenario_id, document_id, connector, stage, transformation],
        )


def provision(
    conn: Any,
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    domains_path: Path = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    """Idempotently ingest, mine, provision, version, and link the corpus."""
    from src.ingestion.argument_mining import mine_unprocessed_documents
    from src.ingestion.connectors.dataset.store import ObservationStore
    from src.ingestion.connectors.filings import Filing, FilingFact, ingest_filing
    from src.ingestion.connectors.legislative import (
        LegislativeConnector,
        VoteRecord,
        record_vote,
    )
    from src.ingestion.connectors.manifest import ManifestConnector
    from src.ingestion.connectors.upload.connector import UploadConnector
    from src.ingestion.corrections import record_revision
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.snapshots import SnapshotStore
    from src.kb.assertions import record_assertions
    from src.kb.membership import run_membership_pass
    from src.kb.registry import load_registry
    from src.osint.independence import run_origin_backfill

    fixture = _load(fixture_path)
    scenario_id = fixture["scenario_id"]
    observed = int(fixture["timeline"]["as_of_ms"])
    ensure_schema(conn)
    store = DocumentStore(conn)

    manifest = ManifestConnector()
    manifest_raw = manifest.fetch(next(iter(manifest.discover(fixture_path))))
    manifest_documents = manifest.parse(manifest_raw)
    public_summary = store.upsert(manifest_documents)
    for document in manifest_documents:
        _insert_lineage(conn, scenario_id, document.document_id, "manifest", "identity")

    vote_path = fixture_path.parent / fixture["vote_source"]
    legislative = LegislativeConnector()
    vote_ref = next(iter(legislative.discover(vote_path)))
    vote_raw = legislative.fetch(vote_ref)
    vote_raw.fetched_at = observed
    vote_documents = legislative.parse(vote_raw)
    for document in vote_documents:
        document.metadata["tags"] = ["policy-monitor"]
        document.metadata["license"] = "CC0-1.0"
    vote_summary = store.upsert(vote_documents)
    for document in vote_documents:
        metadata = document.metadata
        record_vote(
            conn,
            VoteRecord(
                actor=metadata["actor"],
                topic=metadata["topic"],
                bill=metadata.get("bill"),
                position=metadata["position"],
                date=metadata.get("date"),
                source=metadata.get("source"),
                document_id=document.document_id,
            ),
        )
        _insert_lineage(
            conn,
            scenario_id,
            document.document_id,
            "legislative",
            "roll-call-to-document",
        )

    filing_payload = fixture["filing"]
    filing = Filing(
        filer=filing_payload["filer"],
        filing_id=filing_payload["filing_id"],
        narrative=filing_payload["narrative"],
        filed_at=filing_payload["filed_at"],
        source_url=filing_payload["source_url"],
        facts=[FilingFact(**fact) for fact in filing_payload["facts"]],
    )
    filing_products = ingest_filing(filing, ingested_at=observed)
    filing_document = filing_products["document"]
    filing_document.metadata.update(tags=["policy-monitor"], role="filing", license="CC0-1.0")
    filing_summary = store.upsert([filing_document])
    observations = ObservationStore(conn).upsert_many(filing_products["series"])
    _insert_lineage(
        conn,
        scenario_id,
        filing_document.document_id,
        "filings",
        "filing-to-document-and-series",
    )

    memo_payload = fixture["private_memo"]
    upload = UploadConnector()
    memo_ref = next(
        iter(
            upload.discover(
                {"paste": memo_payload["content"], "title": memo_payload["title"]}
            )
        )
    )
    memo_raw = upload.fetch(memo_ref)
    memo_raw.fetched_at = int(memo_payload["as_of_ms"])
    memo_document = upload.parse(memo_raw)[0]
    memo_document.metadata.update(
        tags=["policy-monitor-private", "private"],
        private=True,
        role="private_guidance",
    )
    private_summary = store.upsert([memo_document])
    _insert_lineage(conn, scenario_id, memo_document.document_id, "upload", "private-document")

    baseline = fixture["documents"][0]
    revision = fixture["revision"]
    record_revision(
        conn,
        baseline["document_id"],
        baseline["content"],
        fetched_at=int(baseline["ingested_at"]),
    )
    revision_result = record_revision(
        conn,
        revision["document_id"],
        revision["content"],
        fetched_at=int(revision["fetched_at_ms"]),
    )
    snapshots = SnapshotStore(conn)
    snapshots.snapshot(
        baseline["url"], baseline["content"], int(baseline["ingested_at"])
    )
    snapshots.snapshot(
        baseline["url"], revision["content"], int(revision["fetched_at_ms"])
    )

    record_assertions(
        conn,
        scenario_id,
        baseline["assertions"],
        effective_at_ms=int(baseline["ingested_at"]),
        document_id=baseline["document_id"],
        visibility="public",
        record_kind="primary_text",
    )
    record_assertions(
        conn,
        scenario_id,
        revision["assertions"],
        effective_at_ms=int(revision["fetched_at_ms"]),
        document_id=revision["document_id"],
        visibility="public",
        record_kind="primary_revision",
    )
    record_assertions(
        conn,
        scenario_id,
        memo_payload["assertions"],
        effective_at_ms=int(memo_payload["as_of_ms"]),
        document_id=memo_document.document_id,
        visibility="private",
        record_kind="internal_guidance",
    )

    registry = load_registry(domains_path)
    membership = run_membership_pass(
        conn, registry, run_id=f"policy-monitor:{scenario_id}"
    )
    mining = mine_unprocessed_documents(conn, limit=100)
    from src.argument_mining.metadata import extract_actors, store_actors
    from src.argument_mining.quantities import QuantityExtractor

    ingested_documents = [
        *manifest_documents,
        *vote_documents,
        filing_document,
        memo_document,
    ]
    actors_written = 0
    quantitative_assertions = []
    quantity_extractor = QuantityExtractor()
    for document in ingested_documents:
        actors = extract_actors(document)
        store_actors(actors, conn)
        actors_written += len(actors)
        quantitative_assertions.extend(
            {
                **assertion.to_dict(),
                "document_id": document.document_id,
                "prediction_mode": "deterministic-pattern-v1",
            }
            for assertion in quantity_extractor.extract_document(document)
        )
    origins = run_origin_backfill(conn, batch_size=100, now_ms=observed)
    return {
        "contract": CONTRACT_VERSION,
        "scenario_id": scenario_id,
        "status": "provisioned",
        "documents": {
            "public_manifest": public_summary.as_dict(),
            "vote": vote_summary.as_dict(),
            "filing": filing_summary.as_dict(),
            "private": private_summary.as_dict(),
        },
        "observations_written": observations,
        "revision": revision_result,
        "membership": membership["domains"],
        "argument_mining": mining,
        "entity_extraction": {
            "documents": len(ingested_documents),
            "actors_written": actors_written,
            "method": "metadata-actor-extractor",
        },
        "positions_written": len(vote_documents),
        "quantitative_assertions": quantitative_assertions,
        "origin_inference": origins,
        "lineage_rows": conn.execute(
            "SELECT COUNT(*) FROM policy_monitor_lineage WHERE scenario_id = ?",
            [scenario_id],
        ).fetchone()[0],
        "private_document_id": memo_document.document_id,
    }


def _public_documents(
    conn: Any, domains_path: Path = DEFAULT_DOMAINS
) -> list[dict[str, Any]]:
    from src.kb.registry import load_registry

    backing = load_registry(domains_path).resolve("clean-heat-public", conn=conn)
    output = []
    for row in backing.documents(limit=10_000):
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        output.append(
            {
                "document_id": row["document_id"],
                "source": row.get("source") or row.get("source_id") or "unknown",
                "url": row.get("url"),
                "title": row.get("title"),
                "metadata": dict(metadata),
            }
        )
    return sorted(output, key=lambda item: item["document_id"])


def _public_receipt(
    conn: Any,
    fixture: Mapping[str, Any],
    domains_path: Path = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    from src.osint.independence import origin_summary

    documents = _public_documents(conn, domains_path)
    by_id = {row["document_id"]: row for row in documents}
    primary = by_id["policy:clean-heat-rule"]
    report_ids = [
        "policy:civic-wire",
        "policy:daily-ledger-copy",
        "policy:evening-post-copy",
        "policy:independent-review",
    ]
    origins = origin_summary(conn, report_ids)
    fact = fixture["filing"]["facts"][0]
    threshold = fixture["revision"]["assertions"]["energy_threshold_mwh"]
    vote_document = next(
        row for row in documents if row["document_id"].startswith("legislative:")
    )
    filing_document = by_id[f"filing:{fixture['filing']['filing_id']}"]
    contradiction = by_id["policy:contradiction"]
    statements = [
        {
            "id": "policy-final-text",
            "text": "The final fictional rule uses a 15,000 MWh threshold and starts in January 2027.",
            "verdict": "source_record",
            "evidence": [
                _locator(
                    primary["document_id"],
                    primary["source"],
                    primary["url"],
                    path="policy:clean-heat-rule#revision-1",
                )
            ],
        },
        {
            "id": "reporting-origins",
            "text": (
                f"Four supporting publications resolve to {origins['probable_origin_count']} "
                "probable reporting origins."
            ),
            "verdict": "inferred",
            "method": origins["method"],
            "n": origins["n"],
            "assumptions": origins["assumptions"],
            "evidence": [
                _locator(item, by_id[item]["source"], by_id[item]["url"], path=item)
                for item in report_ids
            ],
        },
        {
            "id": "contradiction",
            "text": "One public commentary repeats the superseded 10,000 MWh threshold.",
            "verdict": "contradicted",
            "evidence": [
                _locator(
                    contradiction["document_id"],
                    contradiction["source"],
                    contradiction["url"],
                    path=contradiction["document_id"],
                ),
                _locator(
                    primary["document_id"],
                    primary["source"],
                    primary["url"],
                    path="policy:clean-heat-rule#revision-1",
                ),
            ],
        },
        {
            "id": "quantitative-observation",
            "text": (
                f"Northstar reported {fact['value']:,} {fact['unit']}, below the "
                f"revised {threshold:,} MWh threshold."
            ),
            "verdict": "below_revised_threshold",
            "evidence": [
                _locator(
                    filing_document["document_id"],
                    filing_document["source"],
                    filing_document["url"],
                    path="dataset:filing:northstar-ceramics:coveredenergyuse#2025",
                )
            ],
        },
        {
            "id": "vote",
            "text": "The fictional Northland Energy Committee supported CHR-17.",
            "verdict": "source_record",
            "evidence": [
                _locator(
                    vote_document["document_id"],
                    vote_document["source"],
                    vote_document["url"],
                    path=vote_document["document_id"],
                )
            ],
        },
    ]
    return {
        "contract": CONTRACT_VERSION,
        "scenario_id": fixture["scenario_id"],
        "as_of_ms": fixture["timeline"]["as_of_ms"],
        "visibility": "public",
        "statements": statements,
        "metrics": {
            "public_document_count": len(documents),
            "supporting_publications": len(report_ids),
            "probable_reporting_origins": origins["probable_origin_count"],
            "contradictions": 1,
            "votes": 1,
        },
        "source_records_and_predictions_separated": True,
        "n": len(documents),
        "method": "deterministic versioned-record comparison",
        "assumptions": [
            "the scenario is fictional and tests evidence transitions, not legal advice",
            "reporting-origin links are probabilistic and do not prove authorship",
        ],
    }


def public_view(
    conn: Any,
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    domains_path: Path = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    """Return only public facts; shape and counts reveal no private corpus state."""
    return _public_receipt(conn, _load(fixture_path), domains_path)


def _comparison(conn: Any, scenario_id: str) -> dict[str, Any]:
    from src.kb.assertions import compare_assertions

    return compare_assertions(conn, scenario_id)


def authorized_view(
    conn: Any,
    principal_id: str,
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    domains_path: Path = DEFAULT_DOMAINS,
    private_domain: str = "clean-heat-private",
) -> dict[str, Any]:
    fixture = _load(fixture_path)
    if not _authorized(conn, principal_id, private_domain):
        raise PolicyMonitorError("principal is not authorized for private guidance")
    result = deepcopy(_public_receipt(conn, fixture, domains_path))
    comparison = _comparison(conn, fixture["scenario_id"])
    if not comparison["private_document_ids"]:
        raise PolicyMonitorError("authorized private guidance is not provisioned")
    memo_id = comparison["private_document_ids"][0]
    evidence = [
        _locator(
            "policy:clean-heat-rule",
            "Northland Rule Register",
            "https://policy.invalid/clean-heat-rule",
            path="policy:clean-heat-rule#revision-1",
        ),
        _locator(
            memo_id,
            "authorized internal guidance",
            None,
            path=memo_id,
            visibility="private",
        ),
    ]
    result["visibility"] = "authorized-private"
    result["private_guidance"] = {
        "status": "stale" if comparison["stale"] else "current",
        "comparison_id": comparison["comparison_id"],
        "differences": comparison["differences"],
        "evidence": evidence,
    }
    result["statements"].append(
        {
            "id": "private-guidance-status",
            "text": "Authorized internal guidance is stale against the newer public revision.",
            "verdict": "guidance_stale",
            "evidence": evidence,
        }
    )
    _audit(
        conn,
        fixture["scenario_id"],
        principal_id,
        "cross_domain_comparison",
        include_private=True,
        at_ms=int(fixture["timeline"]["as_of_ms"]),
        details={"comparison_id": comparison["comparison_id"], "stale": comparison["stale"]},
    )
    return result


def _watch_state(
    fixture: Mapping[str, Any],
    memo_id: str,
    *,
    stale: bool,
) -> dict[str, Any]:
    evidence = [
        _locator(
            "policy:clean-heat-rule",
            "Northland Rule Register",
            "https://policy.invalid/clean-heat-rule",
            path="policy:clean-heat-rule#revision-1" if stale else "policy:clean-heat-rule#revision-0",
        ),
        _locator(
            memo_id,
            "authorized internal guidance",
            None,
            path=memo_id,
            visibility="private",
        ),
    ]
    return {
        "guidance_status": {
            "stale": stale,
            "comparison_id": "clean-heat-threshold-and-start",
            "evidence": evidence,
        },
        "n": 1,
        "method": "versioned public assertion versus authorized guidance comparison",
        "assumptions": ["only explicitly authorized private guidance was compared"],
    }


def _emit_watch(
    conn: Any,
    fixture: Mapping[str, Any],
    memo_id: str,
    *,
    domains_path: Path,
) -> dict[str, Any]:
    from src.kb.registry import load_registry
    from src.kb.watches import (
        commit_watch_watermark,
        create_watch,
        poll_watch,
        record_external_snapshot,
        replay_watch,
    )

    registry = load_registry(domains_path)
    backing = registry.resolve("clean-heat-private", conn=conn)
    watch = create_watch(
        backing,
        PRINCIPAL,
        {"type": "topic", "value": "Clean Heat reporting guidance"},
        ["guidance_stale"],
        now_ms=fixture["timeline"]["as_of_ms"] - 2,
    )
    baseline = int(fixture["timeline"]["baseline_watermark"])
    revision = int(fixture["timeline"]["revision_watermark"])
    commit_watch_watermark(conn, baseline, {"stage": "baseline"}, committed_at_ms=baseline)
    commit_watch_watermark(conn, revision, {"stage": "revision"}, committed_at_ms=revision)
    if watch["last_watermark"] is None:
        record_external_snapshot(
            conn,
            PRINCIPAL,
            watch["watch_id"],
            baseline,
            _watch_state(fixture, memo_id, stale=False),
            observed_at_ms=fixture["private_memo"]["as_of_ms"],
        )
    if watch["last_watermark"] is None or int(watch["last_watermark"]) < revision:
        record_external_snapshot(
            conn,
            PRINCIPAL,
            watch["watch_id"],
            revision,
            _watch_state(fixture, memo_id, stale=True),
            observed_at_ms=fixture["timeline"]["as_of_ms"],
        )
    poll = poll_watch(conn, PRINCIPAL, watch["watch_id"])
    replay = replay_watch(
        conn,
        PRINCIPAL,
        watch["watch_id"],
        from_watermark=baseline,
        to_watermark=revision,
    )
    return {"watch": watch, "poll": poll, "replay": replay}


def export_policy_bundle(
    conn: Any,
    *,
    principal_id: str | None = None,
    include_private: bool = False,
    fixture_path: Path = DEFAULT_FIXTURE,
    domains_path: Path = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    from src.evidence_bundle.exporters import export_receipt

    fixture = _load(fixture_path)
    if include_private:
        if not principal_id:
            raise PolicyMonitorError("private export requires an authenticated principal")
        receipt = authorized_view(
            conn,
            principal_id,
            fixture_path=fixture_path,
            domains_path=domains_path,
        )
    else:
        receipt = public_view(
            conn,
            fixture_path=fixture_path,
            domains_path=domains_path,
        )
    bundle = export_receipt(
        receipt,
        inputs={"scenario_id": fixture["scenario_id"]},
        created_at_ms=int(fixture["timeline"]["as_of_ms"]),
        include_private=include_private,
    )
    _audit(
        conn,
        fixture["scenario_id"],
        principal_id or "public",
        "bundle_export",
        include_private=include_private,
        at_ms=int(fixture["timeline"]["as_of_ms"]),
        details={"bundle_id": bundle["bundle_id"]},
    )
    return bundle


def _brief(receipt: Mapping[str, Any]) -> str:
    lines = ["# Clean Heat policy monitor", ""]
    for statement in receipt["statements"]:
        citations = ", ".join(item["path"] for item in statement["evidence"])
        lines.append(f"- {statement['text']} [{citations}]")
    return "\n".join(lines) + "\n"


def run_demo(
    output_dir: Path,
    *,
    db_path: Path,
    fixture_path: Path = DEFAULT_FIXTURE,
    domains_path: Path = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    """Run the complete offline fixture and write deterministic receipts."""
    import duckdb

    from src.evidence_bundle.verifier import verify_bundle

    output_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        provisioned = provision(conn, fixture_path=fixture_path, domains_path=domains_path)
        fixture = _load(fixture_path)
        grant_private_access(
            conn,
            PRINCIPAL,
            granted_at_ms=int(fixture["private_memo"]["as_of_ms"]),
        )
        public = public_view(
            conn,
            fixture_path=fixture_path,
            domains_path=domains_path,
        )
        authorized = authorized_view(
            conn,
            PRINCIPAL,
            fixture_path=fixture_path,
            domains_path=domains_path,
        )
        watch = _emit_watch(
            conn,
            fixture,
            provisioned["private_document_id"],
            domains_path=domains_path,
        )
        bundle = export_policy_bundle(
            conn,
            fixture_path=fixture_path,
            domains_path=domains_path,
        )
        verification = verify_bundle(bundle).to_dict()
        result = {
            "contract": CONTRACT_VERSION,
            "scenario_id": fixture["scenario_id"],
            "provision": provisioned,
            "public": public,
            "authorized": authorized,
            "watch": watch,
            "brief": _brief(public),
            "bundle": {
                "bundle_id": bundle["bundle_id"],
                "verification": verification,
            },
        }
        (output_dir / "public-answer.json").write_text(
            json.dumps(public, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "authorized-answer.json").write_text(
            json.dumps(authorized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "brief.md").write_text(result["brief"], encoding="utf-8")
        (output_dir / "watch.json").write_text(
            json.dumps(watch, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "evidence-bundle.json").write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "receipt.json").write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        conn.close()

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.citation_preservation import (
    CAPTURE_SCOPE,
    READ_SCOPE,
    REPAIR_SCOPE,
    WRITE_SCOPE,
    CitationPreservationError,
    CitationPreservationStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _policy(store, namespace="research", version="1", **changes):
    values = {
        "namespace": namespace,
        "policy_id": "policy:archive",
        "version": version,
        "allowed_licenses": ["CC-BY-4.0"],
        "approved_archives": ["https://archive.test"],
        "max_bytes": 100,
        "generation": 1,
        "observed_at_ms": 10,
        "producer": {"id": "curator"},
        "policy_context": {"legal": "local"},
        "provenance": {"decision": "policy-doc"},
        "principal_id": "curator",
        "scopes": {WRITE_SCOPE},
    }
    values.update(changes)
    return store.register_policy(**values)


def _capture(
    store,
    policy,
    citation="cite:1",
    content="Evidence supports the assertion.",
    **changes,
):
    values = {
        "namespace": policy["namespace"],
        "policy_id": policy["policy_id"],
        "citation_id": citation,
        "source_url": "https://source.test/page",
        "content": content,
        "license_id": "CC-BY-4.0",
        "retrieved_at_ms": 20,
        "locator": {"section": "Results"},
        "excerpts": [{"text": "Evidence"}],
        "redirects": ["https://source.test/old"],
        "response_metadata": {"etag": "v1"},
        "principal_id": "worker",
        "scopes": {CAPTURE_SCOPE},
    }
    values.update(changes)
    return store.capture(**values)


def test_policies_robots_license_private_versions_and_omissions():
    c = duckdb.connect(":memory:")
    s = CitationPreservationStore(c, now=lambda: 100)
    p = _policy(s)
    blocked = _capture(s, p, robots_allowed=False)
    assert (
        blocked["status"] == "omitted"
        and blocked["omissions"][0]["reason"] == "robots-restricted"
    )
    private = _capture(s, p, citation="cite:private", private_source=True)
    assert private["status"] == "omitted"
    wrong = _capture(s, p, citation="cite:license", license_id="proprietary")
    assert wrong["status"] == "omitted"
    revised = _policy(
        s,
        version="2",
        predecessor_revision_id=p["policy_revision_id"],
        allow_private=True,
    )
    assert revised["policy_revision_id"] != p["policy_revision_id"]
    _validate("noesis-citation-archive-policy-v1.json", revised)
    _validate("noesis-citation-snapshot-v1.json", blocked)
    c.close()


def test_capture_redirect_dynamic_partial_duplicate_crash_safe_replay_and_cancel():
    c = duckdb.connect(":memory:")
    s = CitationPreservationStore(c, now=lambda: 100)
    p = _policy(s)
    first = _capture(s, p, content="x" * 120, partial=True)
    assert first["status"] == "partial" and first["truncated"]
    duplicate = _capture(s, p, citation="cite:2", content="x" * 120, partial=True)
    assert first["snapshot_id"] in duplicate["duplicate_snapshot_ids"]
    assert s.replay_capture("research", first["snapshot_id"], scopes={READ_SCOPE})[
        "deterministic"
    ]
    assert (
        _capture(s, p, citation="cite:cancel", cancel_requested=True)["status"]
        == "cancelled"
    )
    # Idempotency makes retry-after-crash safe at the manifest boundary.
    assert _capture(s, p, content="x" * 120, partial=True)["idempotent"]
    c.close()


def test_verify_page_change_ocr_drift_moved_ambiguous_and_replay():
    c = duckdb.connect(":memory:")
    s = CitationPreservationStore(c, now=lambda: 100)
    p = _policy(s, max_bytes=1000)
    snap = _capture(s, p)
    support = s.verify(
        "research",
        "cite:1",
        snap["snapshot_id"],
        "Evidence supports the assertion.",
        expected_excerpt="Evidence supports",
        locator={"section": "Appendix"},
        principal_id="reviewer",
        scopes={WRITE_SCOPE},
    )
    assert support["status"] == "supports" and support["moved_passage"]
    changed = s.verify(
        "research",
        "cite:1",
        snap["snapshot_id"],
        "other",
        expected_excerpt="absent passage",
        principal_id="reviewer",
        scopes={WRITE_SCOPE},
    )
    assert changed["status"] == "no-longer-present"
    ambiguous = s.verify(
        "research",
        "cite:1",
        snap["snapshot_id"],
        "other",
        expected_excerpt="Evidence supportz the assertion",
        ocr_tolerance=0.45,
        principal_id="reviewer",
        scopes={WRITE_SCOPE},
    )
    assert ambiguous["status"] == "ambiguous"
    contradiction = s.verify(
        "research",
        "cite:1",
        snap["snapshot_id"],
        "assertion",
        contradiction="supports the assertion",
        principal_id="reviewer",
        scopes={WRITE_SCOPE},
    )
    assert contradiction["status"] == "contradicts"
    _validate("noesis-citation-verification-v1.json", support)
    c.close()


def test_link_rot_soft404_paywall_archive_mismatch_takedown_and_repair_provenance():
    c = duckdb.connect(":memory:")
    s = CitationPreservationStore(c, now=lambda: 100)
    p = _policy(s, max_bytes=1000)
    snap = _capture(s, p)
    soft = s.record_health(
        "research",
        "cite:1",
        "https://source.test",
        200,
        response_title="Page not found",
        principal_id="monitor",
        scopes={WRITE_SCOPE},
    )
    assert soft["status"] == "soft-404"
    assert (
        s.record_health(
            "research",
            "cite:1",
            "https://source.test",
            200,
            paywall=True,
            principal_id="monitor",
            scopes={WRITE_SCOPE},
        )["status"]
        == "paywall"
    )
    assert (
        s.record_health(
            "research",
            "cite:1",
            "https://source.test",
            451,
            takedown=True,
            principal_id="monitor",
            scopes={WRITE_SCOPE},
        )["status"]
        == "takedown"
    )
    preview = s.preview_repair(
        "research",
        p["policy_id"],
        "cite:1",
        snap["snapshot_id"],
        [
            {
                "archive": "https://archive.test",
                "url": "https://archive.test/cite1",
                "content": "wrong",
            },
            {
                "archive": "https://archive.test",
                "url": "https://archive.test/exact",
                "content_hash": snap["blob_hash"],
            },
        ],
        scopes={READ_SCOPE},
    )
    assert not preview["candidates"][0]["eligible"] and preview["original_unchanged"]
    repair = s.accept_repair(
        "research", preview, 1, principal_id="operator", scopes={REPAIR_SCOPE}
    )
    assert repair["original_unchanged"]
    with pytest.raises(CitationPreservationError, match="approved exact-content"):
        s.accept_repair(
            "research", preview, 0, principal_id="operator", scopes={REPAIR_SCOPE}
        )
    _validate("noesis-citation-health-v1.json", soft)
    _validate("noesis-citation-health-v1.json", preview)
    _validate("noesis-citation-health-v1.json", repair)
    c.close()


def test_status_export_bounded_six_domains_namespace_and_auth():
    c = duckdb.connect(":memory:")
    s = CitationPreservationStore(c, now=lambda: 100)
    for namespace in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        p = _policy(s, namespace=namespace)
        _capture(s, p, citation=f"cite:{namespace}")
        exported = s.export(namespace, [f"cite:{namespace}"], scopes={READ_SCOPE})
        assert exported["dependency_complete"] and exported["policies"]
        _validate("noesis-citation-export-v1.json", exported)
    with pytest.raises(CitationPreservationError, match="not found"):
        s.snapshot(
            "research",
            s.status("scientific", "cite:scientific", scopes={READ_SCOPE})["snapshots"][
                0
            ]["snapshot_id"],
            scopes={READ_SCOPE},
        )
    with pytest.raises(CitationPreservationError, match="missing required scope"):
        s.status("research", "cite:research", scopes={"knowledge:read"})
    c.close()

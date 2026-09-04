from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion.source_pack_runtime import (
    RECEIPT_CONTRACT,
    FixturePageAdapter,
    RuntimeAdapterFactory,
    RuntimePage,
    SourcePackRuntime,
    _validate_redirect,
    validate_run_request,
)
from src.ingestion.source_packs import (
    SourcePackError,
    SourcePackStore,
    load_source_packs,
)

ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = ROOT / "config/source_packs"


@pytest.fixture()
def setup():
    conn = duckdb.connect(":memory:")
    manifests = load_source_packs(PACK_DIR)
    store = SourcePackStore(conn)
    for manifest in manifests:
        store.install(manifest, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 100_000))
    runtime = SourcePackRuntime(
        conn, now=lambda: next(clock), sleep=lambda _delay: None
    )
    yield conn, manifests, runtime
    conn.close()


def source(manifests, pack_id, source_id=None):
    manifest = next(item for item in manifests if item["pack_id"] == pack_id)
    selected = next(
        item
        for item in manifest["sources"]
        if source_id is None or item["source_id"] == source_id
    )
    return manifest, selected


def accept(runtime, manifest, selected, *, redistribution=False):
    return runtime.accept_license(
        manifest["pack_id"],
        selected["source_id"],
        principal_id="operator",
        redistribution=redistribution,
    )


def request(manifest, selected, key="run-1", **updates):
    value = {
        "pack_id": manifest["pack_id"],
        "run_key": key,
        "operation": selected["operations"][0],
        "source_ids": [selected["source_id"]],
        "required_sources": [selected["source_id"]],
        "max_pages": 10,
        "max_results": 100,
        "max_bytes": 1_000_000,
        "timeout_ms": 10_000,
    }
    value.update(updates)
    return value


def test_request_contract_requires_bounded_backfill():
    normalized = validate_run_request(
        {"pack_id": "pack", "run_key": "one", "operation": "search"}
    )
    assert normalized["contract"] == "noesis-source-pack-run-request-v1"
    assert normalized["request_hash"]
    with pytest.raises(SourcePackError) as caught:
        validate_run_request(
            {
                "pack_id": "pack",
                "run_key": "backfill",
                "operation": "search",
                "mode": "backfill",
            }
        )
    assert caught.value.code == "unbounded_backfill"
    with pytest.raises(SourcePackError) as caught:
        validate_run_request(
            {
                "pack_id": "pack",
                "run_key": "large",
                "operation": "search",
                "max_pages": 101,
            }
        )
    assert caught.value.code == "unbounded_run"
    with pytest.raises(SourcePackError) as caught:
        validate_run_request(
            {
                "pack_id": "pack",
                "run_key": "unsafe",
                "operation": "search",
                "parameters": {"token": "do-not-persist"},
            }
        )
    assert caught.value.code == "embedded_secret"
    with pytest.raises(SourcePackError) as caught:
        validate_run_request(
            {
                "pack_id": "pack",
                "run_key": "executable",
                "operation": "search",
                "callback": "module:function",
            }
        )
    assert caught.value.code == "unsupported_control"


def test_redirect_policy_revalidates_host_and_public_resolution():
    initial = "https://api.example.test/records"
    _validate_redirect(
        initial,
        "https://api.example.test/page/2",
        lambda _: ["8.8.8.8"],
    )
    with pytest.raises(SourcePackError) as caught:
        _validate_redirect(
            initial, "https://redirect.example.test/page/2", lambda _: ["8.8.8.8"]
        )
    assert caught.value.code == "network_policy"
    with pytest.raises(SourcePackError) as caught:
        _validate_redirect(initial, initial, lambda _: ["127.0.0.1"])
    assert caught.value.code == "network_policy"


def test_request_and_receipt_schemas_accept_runtime_contracts(setup):
    from jsonschema import Draft7Validator

    _, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    normalized = validate_run_request(request(manifest, selected, key="schema"))
    receipt = runtime.run(
        normalized,
        principal_id="operator",
        adapters={
            selected["source_id"]: FixturePageAdapter(selected, [[{"id": "one"}]])
        },
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    for value, name in (
        (normalized, "noesis-source-pack-run-request-v1.json"),
        (receipt, "noesis-source-pack-run-receipt-v1.json"),
    ):
        schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
        Draft7Validator.check_schema(schema)
        assert not list(Draft7Validator(schema).iter_errors(value))


def test_factory_compiles_every_declared_connector_and_rejects_drift(setup):
    _, manifests, _ = setup
    factory = RuntimeAdapterFactory()
    kinds = set()
    for manifest in manifests:
        for item in manifest["sources"]:
            adapter = factory.compile(
                item,
                transport=lambda **_: {"status": 200, "content": b'{"items":[]}'},
            )
            assert adapter.describe()["source_hash"] == item["source_hash"]
            kinds.add(item["connector"])
    assert kinds
    changed = dict(manifests[0]["sources"][0])
    changed["endpoint"] = "https://example.test/changed"
    with pytest.raises(SourcePackError) as caught:
        factory.compile(changed)
    assert caught.value.code == "manifest_drift"


def test_adapter_allows_only_declared_operations_and_bounded_get(setup):
    _, manifests, _ = setup
    _, selected = source(manifests, "research-discovery", "crossref-works")
    seen = {}

    def transport(**kwargs):
        seen.update(kwargs)
        return {
            "status": 200,
            "content": json.dumps(
                {"items": [{"id": "work:1", "title": "Study"}], "next_cursor": "2"}
            ),
        }

    adapter = RuntimeAdapterFactory().compile(selected, transport=transport)
    page = adapter.fetch_page(
        {"operation": "search", "parameters": {"query": "evidence"}, "limit": 2},
        cursor="1",
    )
    assert page.next_cursor == "2" and page.records[0]["id"] == "work:1"
    assert seen["url"] == selected["endpoint"]
    assert seen["params"]["cursor"] == "1"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "delete", "parameters": {}}, cursor=None)
    assert caught.value.code == "operation_forbidden"
    limited = RuntimeAdapterFactory().compile(
        selected,
        transport=lambda **_: {
            "status": 429,
            "headers": {"Retry-After": "2"},
            "content": b"",
        },
    )
    with pytest.raises(SourcePackError) as caught:
        limited.fetch_page(
            {"operation": "search", "parameters": {}, "limit": 1}, cursor=None
        )
    assert caught.value.code == "rate_limited"
    assert caught.value.details["retry_after_ms"] == 2_000


def test_preflight_enforces_enabled_auth_license_network_and_redistribution(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(
        manifests, "official-political-records", "eu-eurlex-regulatory"
    )
    req = request(manifest, selected)
    denied = runtime.preflight(req, dns_resolver=lambda _: ["8.8.8.8"])
    assert not denied["ready"]
    assert denied["sources"][0]["failures"] == [
        "credential_missing",
        "license_not_accepted",
    ]
    accept(runtime, manifest, selected)
    ready = runtime.preflight(
        req,
        secret_available=lambda ref: ref == "NOESIS_EURLEX_API_KEY",
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert ready["ready"]
    redistribution = runtime.preflight(
        request(manifest, selected, redistribute=True),
        secret_available=lambda _: True,
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert not redistribution["ready"]
    assert "license_not_accepted" in redistribution["sources"][0]["failures"]
    SourcePackStore(conn, initialize=False).set_enabled(
        manifest["pack_id"], False, principal_id="operator", now_ms=99
    )
    assert not runtime.preflight(
        req, secret_available=lambda _: True, dns_resolver=lambda _: ["8.8.8.8"]
    )["ready"]


def test_preflight_blocks_private_resolution_without_leaking_secret(setup):
    _, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    result = runtime.preflight(
        request(manifest, selected),
        secret_available=lambda _: True,
        dns_resolver=lambda _: ["127.0.0.1"],
    )
    assert result["sources"][0]["failures"] == ["network_policy"]
    assert "secret-value" not in json.dumps(result)


def test_incremental_run_ingests_pages_commits_watermark_and_is_idempotent(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    adapter = FixturePageAdapter(
        selected,
        [
            [{"id": "doi:1", "title": "First", "published_at": "2025-01-01"}],
            [{"id": "doi:2", "title": "Second", "published_at": "2025-01-02"}],
        ],
    )
    value = request(manifest, selected)
    result = runtime.run(
        value,
        principal_id="operator",
        adapters={selected["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert result["contract"] == RECEIPT_CONTRACT
    assert result["status"] == "complete" and result["watermark"] == 1
    assert result["sources"][0]["counts"] == {
        "attempts": 2,
        "pages": 2,
        "fetched": 2,
        "normalized": 2,
        "inserted": 2,
        "duplicates": 0,
        "invalid": 0,
        "quarantined": 0,
        "bytes": result["sources"][0]["counts"]["bytes"],
    }
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (2,)
    again = runtime.run(
        value,
        principal_id="operator",
        adapters={selected["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert again["idempotent"] and again["receipt_hash"] == result["receipt_hash"]
    assert adapter.calls == [None, "1"]


def test_crash_resume_starts_at_durable_page_cursor(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    adapter = FixturePageAdapter(
        selected,
        [
            [{"id": "doi:1", "title": "First"}],
            [{"id": "doi:2", "title": "Second"}],
        ],
    )
    value = request(manifest, selected, key="resume")

    def crash(_source, page):
        if page == 1:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected"):
        runtime.run(
            value,
            principal_id="operator",
            adapters={selected["source_id"]: adapter},
            dns_resolver=lambda _: ["8.8.8.8"],
            fault=crash,
        )
    run_id = (
        "source-run:"
        + __import__("hashlib")
        .sha256(
            json.dumps([manifest["pack_id"], "resume"], separators=(",", ":")).encode()
        )
        .hexdigest()[:24]
    )
    assert runtime.inspect(run_id)["status"] == "interrupted"
    resumed = runtime.run(
        value,
        principal_id="operator",
        adapters={selected["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert resumed["status"] == "complete"
    assert adapter.calls == [None, "1"]
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (2,)


def test_changed_source_record_is_a_new_revision_while_retries_deduplicate(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    first = {"id": "doi:revision", "title": "Original", "version": 1}
    changed = {"id": "doi:revision", "title": "Corrected", "version": 2}
    receipts = []
    for key, record in (("revision-1", first), ("revision-2", changed)):
        receipts.append(
            runtime.run(
                request(manifest, selected, key=key),
                principal_id="operator",
                adapters={
                    selected["source_id"]: FixturePageAdapter(selected, [[record]])
                },
                dns_resolver=lambda _: ["8.8.8.8"],
            )
        )
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (2,)
    assert conn.execute(
        "SELECT COUNT(DISTINCT document_id) FROM documents"
    ).fetchone() == (2,)
    assert runtime.replay(receipts[0]["run_id"])["watermark_hash_match"]


def test_retry_budget_and_circuit_breaker_are_deterministic(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    transient = SourcePackError(
        "source_unavailable", "temporary bearer super-secret", retry_after_ms=250
    )
    adapter = FixturePageAdapter(
        selected, [[{"id": "ok", "title": "Recovered"}]], failures={0: transient}
    )
    result = runtime.run(
        request(manifest, selected, key="retry", retries=1),
        principal_id="operator",
        adapters={selected["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert result["sources"][0]["counts"]["attempts"] == 2
    assert result["sources"][0]["retries"] == [
        {"attempt": 1, "code": "source_unavailable", "delay_ms": 250}
    ]
    assert "super-secret" not in json.dumps(result)
    assert result["sources"][0]["circuit"]["state"] == "closed"

    for index in range(3):
        failing = FixturePageAdapter(
            selected,
            [[]],
            failures={0: SourcePackError("source_unavailable", "temporary")},
        )
        runtime.run(
            request(
                manifest,
                selected,
                key=f"circuit-{index}",
                retries=0,
                required_sources=[],
            ),
            principal_id="operator",
            adapters={selected["source_id"]: failing},
            dns_resolver=lambda _: ["8.8.8.8"],
        )
    assert conn.execute(
        "SELECT state,failures FROM source_pack_circuits WHERE pack_id=? AND source_id=?",
        [manifest["pack_id"], selected["source_id"]],
    ).fetchone() == ("open", 3)
    conn.execute(
        "UPDATE source_pack_circuits SET probe_after_ms=0 WHERE pack_id=? AND source_id=?",
        [manifest["pack_id"], selected["source_id"]],
    )
    probe = runtime.run(
        request(manifest, selected, key="circuit-probe"),
        principal_id="operator",
        adapters={selected["source_id"]: FixturePageAdapter(selected, [[]])},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert probe["sources"][0]["circuit"] == {
        "state": "closed",
        "failures": 0,
        "probe_after_ms": None,
    }


def test_backfill_does_not_advance_live_cursor(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    live = FixturePageAdapter(selected, [[{"id": "live"}], [{"id": "live-2"}]])
    runtime.run(
        request(manifest, selected, key="live", max_pages=1),
        principal_id="operator",
        adapters={selected["source_id"]: live},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    before = conn.execute(
        "SELECT cursor FROM source_pack_checkpoints WHERE pack_id=? AND source_id=?",
        [manifest["pack_id"], selected["source_id"]],
    ).fetchone()[0]
    backfill = FixturePageAdapter(selected, [[], [{"id": "historic"}]])
    result = runtime.run(
        request(
            manifest,
            selected,
            key="backfill",
            mode="backfill",
            backfill={"cursor": "1", "from_ms": 1, "to_ms": 2},
        ),
        principal_id="operator",
        adapters={selected["source_id"]: backfill},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    after = conn.execute(
        "SELECT cursor FROM source_pack_checkpoints WHERE pack_id=? AND source_id=?",
        [manifest["pack_id"], selected["source_id"]],
    ).fetchone()[0]
    assert before == after == "1"
    assert result["sources"][0]["cursor"]["live_advanced"] is False


def test_quarantine_retry_replay_and_redaction(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    adapter = FixturePageAdapter(
        selected,
        [[{"id": "bad", "created_at": "not-a-date", "token": "record-value"}]],
    )
    result = runtime.run(
        request(manifest, selected, key="quarantine"),
        principal_id="operator",
        adapters={selected["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert result["coverage"]["quarantined"] == 1
    qid = conn.execute("SELECT quarantine_id FROM source_pack_quarantine").fetchone()[0]
    retried = runtime.retry_quarantine([qid], principal_id="operator")
    assert retried == {
        "principal_id": "operator",
        "retried": 1,
        "recovered": 0,
        "failed": 1,
    }
    replay = runtime.replay(result["run_id"])
    assert replay["matched"] and replay["manifest_match"]
    assert replay["request_hash_match"]
    assert replay["output_hashes_match"]
    assert replay["watermark_hash_match"]
    tampered = dict(result)
    tampered["sources"] = [dict(result["sources"][0], output_hash="tampered")]
    conn.execute(
        "UPDATE source_pack_runs SET receipt_json=? WHERE run_id=?",
        [json.dumps(tampered), result["run_id"]],
    )
    mismatched = runtime.replay(result["run_id"])
    assert not mismatched["matched"]
    assert not mismatched["receipt_hash_match"]
    assert not mismatched["output_hashes_match"]
    encoded = json.dumps(result)
    assert "record-value" not in encoded
    assert (
        "record-value"
        not in conn.execute(
            "SELECT record_json FROM source_pack_quarantine WHERE quarantine_id=?",
            [qid],
        ).fetchone()[0]
    )


def test_cooperative_cancel_stops_before_next_page(setup):
    _, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    accept(runtime, manifest, selected)
    value = request(manifest, selected, key="cancel")
    normalized = validate_run_request(value)
    run_id = (
        "source-run:"
        + __import__("hashlib")
        .sha256(
            json.dumps(
                [manifest["pack_id"], normalized["run_key"]], separators=(",", ":")
            ).encode()
        )
        .hexdigest()[:24]
    )

    class CancellingAdapter(FixturePageAdapter):
        def fetch_page(self, request, *, cursor):
            page = super().fetch_page(request, cursor=cursor)
            runtime.cancel(run_id)
            return RuntimePage(page.records, "1", page.bytes_read)

    with pytest.raises(SourcePackError) as caught:
        runtime.run(
            value,
            principal_id="operator",
            adapters={
                selected["source_id"]: CancellingAdapter(selected, [[{"id": "one"}]])
            },
            dns_resolver=lambda _: ["8.8.8.8"],
        )
    assert caught.value.code == "cancelled"
    assert runtime.inspect(run_id)["status"] == "cancelled"


def test_schedules_reject_overlap_and_report_runtime_coverage(setup):
    conn, manifests, runtime = setup
    manifest, selected = source(manifests, "research-discovery", "crossref-works")
    scheduled = runtime.set_schedule(
        manifest["pack_id"],
        {"kind": "interval", "interval_s": 60, "next_run_at_ms": 999},
        principal_id="operator",
    )
    assert scheduled["next_run_at_ms"] == 999
    assert runtime.schedules(due_at_ms=1000)["schedules"][0]["due"]
    accept(runtime, manifest, selected)
    runtime.run(
        request(manifest, selected, key="coverage"),
        principal_id="operator",
        adapters={
            selected["source_id"]: FixturePageAdapter(selected, [[{"id": "one"}]])
        },
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    coverage = runtime.runtime_coverage()
    assert coverage["domains"]["research"] == {
        "configured": 2,
        "ready": 1,
        "attempted": 1,
        "completed": 1,
        "quarantined": 0,
        "degraded": 0,
        "unavailable": 0,
        "watermarked_packs": 1,
    }
    conn.execute(
        "INSERT INTO source_pack_runs VALUES ('active','active','hash',?,'1.0.0','hash','incremental','running','operator','{}',NULL,1,1,NULL)",
        [manifest["pack_id"]],
    )
    with pytest.raises(SourcePackError) as caught:
        runtime.run(
            request(manifest, selected, key="overlap"),
            principal_id="operator",
            adapters={selected["source_id"]: FixturePageAdapter(selected, [[]])},
            dns_resolver=lambda _: ["8.8.8.8"],
        )
    assert caught.value.code == "run_conflict"


def test_six_domain_offline_execution(setup):
    _, manifests, runtime = setup
    completed = set()
    for manifest in manifests:
        selected = manifest["sources"][0]
        accept(runtime, manifest, selected)
        fixture = json.loads((ROOT / selected["fixture"]["path"]).read_text())
        result = runtime.run(
            request(manifest, selected, key=f"six:{manifest['pack_id']}"),
            principal_id="operator",
            adapters={
                selected["source_id"]: FixturePageAdapter(
                    selected, [fixture["normalized"]]
                )
            },
            dns_resolver=lambda _: ["8.8.8.8"],
        )
        assert result["watermark"] == 1
        completed.update(manifest["domains"])
    assert completed == {
        "economic",
        "osint",
        "political",
        "research",
        "scientific",
        "technical",
    }

"""Metadata-only plugin fixtures do not stand in for signed-in Modulo data."""

import json
from pathlib import Path

import duckdb
import httpx
import pytest
from jsonschema import Draft202012Validator

from src.kb.intake_modes import IntakeError
from src.kb.intake_modes import _hash
from src.kb.intake_modulo_migration import ModuloMigrationStore
from src.kb.modulo_state_inventory import ModuloStateInventoryClient
from tools.knowledge_engine_mcp.intake_migration import register

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {"knowledge:intake:read", "knowledge:intake:write",
          "namespace:research:read", "namespace:research:write"}


def _record(identity, *, fields, version=3):
    return {
        "record_id": identity, "authoritative_version": version,
        "fields": fields, "relation_ids": ["task-1"],
        "attachment_ids": ["blob-1"],
        "content_sha256": "a" * 64,
        "source_locator": {"url": "https://example.org/source"},
    }


def _inventory():
    return {
        "workspace_id": "personal", "account_id": "alice-account",
        "observed_at_ms": 1000, "source": "fixture",
        "plugins": [
            {"plugin_id": "notes-editor", "installed_version": "2.0.0",
             "collections": [{
                 "collection": "notes", "schema_id": "modulo.notes", "schema_version": 2,
                 "persistence": "authenticated_plugin_state",
                 "records": [_record("note-1", fields=["title", "body", "custom_field"])],
             }]},
            {"plugin_id": "flashcards-spaced-repetition", "installed_version": "1.2.0",
             "collections": [{
                 "collection": "cards", "schema_id": "modulo.cards", "schema_version": 1,
                 "persistence": "legacy_local",
                 "records": [_record("card-1", fields=["card_id", "review_logs", "schedule_params"])],
             }]},
        ],
    }


def test_preview_preserves_plugin_identity_and_flags_legacy_review():
    conn = duckdb.connect(":memory:")
    store = ModuloMigrationStore(conn)
    mapping = [{
        "plugin_id": "notes-editor", "collection": "notes", "record_id": "note-1",
        "noesis_reference": {"kind": "note", "id": "n1", "namespace": "research", "version": 2},
    }]
    preview = store.preview("research", "fixture", _inventory(), mapping,
                            principal_id="alice", scopes=SCOPES)
    schema = json.loads((
        ROOT / "contracts/schemas/jsonschema/noesis-modulo-intake-migration-preview-v1.json"
    ).read_text())
    Draft202012Validator(schema).validate(preview)
    assert preview["counts"] == {
        "records": 2, "link_in_place": 1,
        "legacy_import_required": 1, "review_required": 2,
    }
    note = preview["records"][0]
    assert note["authoritative_version"] == 3
    assert note["noesis_reference"]["version"] == 2
    assert note["unsupported_fields"] == ["custom_field"]
    card = preview["records"][1]
    assert card["action"] == "review_mapping"
    assert card["record_id"] == "card-1"
    assert any("no import API is assumed" in item for item in preview["limitations"])
    assert preview["remote_mutations"] == 0
    assert store.preview("research", "fixture", _inventory(), mapping,
                         principal_id="alice", scopes=SCOPES)["idempotent"]
    with pytest.raises(IntakeError) as conflict:
        store.preview("research", "fixture", _inventory(), [],
                      principal_id="alice", scopes=SCOPES)
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(IntakeError) as denied:
        store.inspect("research", preview["preview_id"],
                      principal_id="bob", scopes=SCOPES)
    assert denied.value.code == "unauthorized"


def test_duplicate_records_and_unknown_versions_fail_before_write():
    conn = duckdb.connect(":memory:")
    store = ModuloMigrationStore(conn)
    inventory = _inventory()
    inventory["plugins"][0]["collections"][0]["records"].append(
        _record("note-1", fields=["body"]))
    with pytest.raises(IntakeError) as duplicate:
        store.preview("research", "duplicate", inventory, [],
                      principal_id="alice", scopes=SCOPES)
    assert duplicate.value.code == "duplicate_record"
    inventory = _inventory()
    inventory["plugins"][1]["collections"][0]["records"][0]["authoritative_version"] = 0
    with pytest.raises(IntakeError) as version:
        store.preview("research", "version", inventory, [],
                      principal_id="alice", scopes=SCOPES)
    assert version.value.code == "invalid_inventory"
    assert conn.execute("SELECT count(*) FROM intake_modulo_migration_previews").fetchone() == (0,)


def test_document_mappings_require_current_document_access_on_create_and_inspect():
    conn = duckdb.connect(":memory:")
    store = ModuloMigrationStore(conn)
    mapping = [{
        "plugin_id": "notes-editor", "collection": "notes", "record_id": "note-1",
        "noesis_reference": {
            "kind": "document", "id": "doc-1", "namespace": "research", "version": 2,
        },
    }]
    with pytest.raises(IntakeError) as denied_create:
        store.preview("research", "document-link", _inventory(), mapping,
                      principal_id="alice", scopes=SCOPES)
    assert denied_create.value.code == "unauthorized"

    authorized = SCOPES | {"document:doc-1:read"}
    preview = store.preview("research", "document-link", _inventory(), mapping,
                            principal_id="alice", scopes=authorized)
    with pytest.raises(IntakeError) as denied_inspect:
        store.inspect("research", preview["preview_id"], principal_id="alice",
                      scopes=SCOPES)
    assert denied_inspect.value.code == "unauthorized"


def test_mcp_wrapper_uses_current_intake_scopes():
    conn = duckdb.connect(":memory:")
    calls = []

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    def safe(operation, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return operation(conn)

    mcp = MCP()
    register(mcp, safe, lambda: ("alice", SCOPES))
    saved = mcp.tools["preview_modulo_intake_migration"](
        "research", "wrapper", _inventory(), [])
    inspected = mcp.tools["inspect_modulo_intake_migration"](
        "research", saved["preview_id"], limit=1)
    assert len(inspected["records"]) == 1 and inspected["next_offset"] == 1
    assert calls == [(True, "knowledge:intake:write"), (False, "knowledge:intake:read")]


def test_mcp_flashcard_import_is_registered_as_an_intake_write():
    conn = duckdb.connect(":memory:")
    calls = []

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    def safe(operation, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return operation(conn)

    value = {
        "card_id": "card-original-3", "question": "What changed?",
        "answer": "The review kept exact plugin provenance.",
        "review_logs": [{"rating": 4, "reviewed_at_ms": 100}],
        "schedule_params": {"algorithm": "FSRS-6", "weights": [0.1, 0.2]},
    }
    record = _record("record-3", fields=list(value), version=4)
    record["content_sha256"] = _hash(value)
    inventory = {
        "workspace_id": "personal", "account_id": "alice-account",
        "observed_at_ms": 200, "source": "caller_supplied_plugin_state",
        "plugins": [{
            "plugin_id": "flashcards-spaced-repetition", "installed_version": "5.0",
            "collections": [{
                "collection": "cards", "schema_id": "modulo.flashcard",
                "schema_version": 5, "persistence": "authenticated_plugin_state",
                "records": [record],
            }],
        }],
    }
    mcp = MCP()
    register(mcp, safe, lambda: ("alice", SCOPES))
    preview = mcp.tools["preview_modulo_intake_migration"](
        "research", "wrapper-flashcards", inventory, [],
    )
    imported = mcp.tools["import_modulo_flashcards"](
        "research", preview["preview_id"], "wrapper-import", "Imported cards", [{
            "plugin_id": "flashcards-spaced-repetition", "collection": "cards",
            "record_id": "record-3", "authoritative_version": 4,
            "content_sha256": _hash(value), "value": value,
            "mastery_criterion": "Explain the change without looking at the answer",
        }],
    )
    assert imported["cards"][0]["id"] == "card-1"
    assert calls == [
        (True, "knowledge:intake:write"),
        (True, "knowledge:intake:write"),
    ]


def test_callback_pages_feed_durable_preview_with_receipt_and_unknown_metadata():
    calls = []
    pages = [
        {
            "records": [{
                "key": "note-1", "schemaId": "modulo.note", "schemaVersion": 4,
                "version": 7,
                "value": {
                    "title": "First note", "body": "kept in plugin state",
                    "relation_ids": ["task-1"], "attachment_ids": ["file-1"],
                    "source_locator": {"url": "https://example.org/first"},
                },
                "deleted": False,
            }],
            "nextCursor": "note-1",
        },
        {
            "records": [{
                "key": "note-2", "schemaId": "modulo.note", "schemaVersion": 5,
                "version": 2, "value": {"title": "Second note"}, "deleted": False,
            }],
            "nextCursor": None,
        },
    ]

    def callback(request):
        calls.append(request)
        assert request.url.path == "/api/plugin-state/callback/workspaces/personal/notes-editor"
        assert request.headers["x-modulo-plugin-token"] == "workload-secret"
        assert request.headers["x-modulo-state-grant"] == "owner-grant-secret"
        assert request.headers["accept"] == "application/json"
        params = dict(request.url.params)
        if "generation" in params:
            return httpx.Response(200, json={"generation": "storage-gen-1"})
        assert params.get("limit") == "100"
        if len(calls) == 3:
            assert params.get("cursor") == "note-1"
        else:
            assert "cursor" not in params
        return httpx.Response(200, json=pages.pop(0))

    transport = httpx.MockTransport(callback)
    with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
        inventory = ModuloStateInventoryClient(
            "https://modulo.example", "notes-editor", "workload-secret",
            "owner-grant-secret", client=client, now=lambda: 1234,
        ).inventory()

    assert len(calls) == 4
    assert inventory["account_id"] is None
    plugin = inventory["plugins"][0]
    assert plugin["installed_version"] is None
    assert plugin["plugin_id"] == "notes-editor"
    assert len(plugin["collections"]) == 1
    collection = plugin["collections"][0]
    assert collection["collection"] == "unclassified"
    assert collection["records"][0]["schema_id"] == "modulo.note"
    assert collection["records"][0]["schema_version"] == 4
    assert collection["records"][1]["metadata_gaps"] == [
        "collection", "installed_version", "relation_ids", "attachment_ids", "source_locator",
    ]
    assert inventory["read_receipt"] == {
        "transport": "modulo_external_plugin_state_callback",
        "plugin_id": "notes-editor", "storage_generation": "storage-gen-1",
        "page_count": 2, "record_count": 2,
        "consistency": "non_atomic_paged_read",
        "account_identity": "not_exposed_by_callback",
    }

    conn = duckdb.connect(":memory:")
    store = ModuloMigrationStore(conn)
    preview = store.preview(
        "research", "callback-fixture", inventory, [], principal_id="alice",
        scopes=SCOPES, authenticated_transport=True,
    )
    schema = json.loads((
        ROOT / "contracts/schemas/jsonschema/noesis-modulo-intake-migration-preview-v1.json"
    ).read_text())
    Draft202012Validator(schema).validate(preview)
    assert conn.execute("SELECT count(*) FROM intake_modulo_migration_previews").fetchone() == (1,)
    assert preview["read_receipt"] == inventory["read_receipt"]
    assert preview["account_id"] is None
    assert preview["collections"][0]["installed_version"] is None
    assert preview["records"][0]["schema_id"] == "modulo.note"
    assert preview["records"][1]["schema_version"] == 5
    assert all(record["action"] == "review_mapping" for record in preview["records"])
    assert preview["remote_mutations"] == 0
    assert "workload-secret" not in json.dumps(preview)
    assert "owner-grant-secret" not in json.dumps(preview)


def test_flashcard_import_preserves_source_history_and_requires_schedule_review():
    conn = duckdb.connect(":memory:")
    store = ModuloMigrationStore(conn, now=lambda: 2000)
    value = {
        "card_id": "source-card-91", "question": "What does the worker do?",
        "answer": "It updates the search index.",
        "review_logs": [{"review_id": "review-7", "revision": 3,
                          "rating": 4, "reviewed_at_ms": 1800}],
        "schedule_params": {"algorithm": "FSRS-6", "desired_retention": 0.9,
                            "weights": [0.1, 0.2, 0.3]},
    }
    record = _record("source-record-91", fields=list(value), version=8)
    record["content_sha256"] = _hash(value)
    inventory = {
        "workspace_id": "personal", "account_id": None,
        "observed_at_ms": 1900, "source": "caller_supplied_plugin_state",
        "plugins": [{
            "plugin_id": "flashcards-spaced-repetition", "installed_version": "4.2",
            "collections": [{
                "collection": "cards", "schema_id": "modulo.flashcard", "schema_version": 5,
                "persistence": "authenticated_plugin_state", "records": [record],
            }],
        }],
    }
    preview = store.preview(
        "research", "cards-preview", inventory, [],
        principal_id="alice", scopes=SCOPES,
    )
    source_values = [{
        "plugin_id": "flashcards-spaced-repetition", "collection": "cards",
        "record_id": "source-record-91", "authoritative_version": 8,
        "content_sha256": _hash(value), "value": value,
        "mastery_criterion": "Recall the worker's role without looking at the answer",
    }]
    pack = store.import_flashcards(
        "research", preview["preview_id"], "import-cards", title="Imported cards",
        source_values=source_values, principal_id="alice", scopes=SCOPES,
    )
    assert pack["cards"][0]["id"] == "card-1"
    assert pack["cards"][0]["references"] == [{
        "kind": "modulo_flashcard", "id": "source-record-91",
        "namespace": "research", "version": 8,
    }]
    source = pack["modulo_import"]["records"][0]
    assert source["source_card_id"] == "source-card-91"
    assert source["authoritative_version"] == 8
    assert source["source_review_history"] == value["review_logs"]
    assert source["source_schedule_params"] == value["schedule_params"]
    assert source["schedule_mapping"] == {
        "source_algorithm": "FSRS-6", "target_algorithm": "noesis_fixed_intervals",
        "status": "review_required", "reason": "unsupported_fsrs_mapping", "mapped": False,
    }
    assert "not translated" in " ".join(pack["modulo_import"]["limitations"])
    from src.kb.intake_practice import IntakePracticeStore, verify_practice_export

    practice = IntakePracticeStore(conn, initialize=False, now=lambda: 2000)
    exported = practice.export_pack(
        "research", pack["pack_id"], principal_id="alice", scopes=SCOPES,
    )
    assert exported["pack_revisions"][0]["modulo_import"] == pack["modulo_import"]
    assert verify_practice_export(exported)["valid"]
    assert store.import_flashcards(
        "research", preview["preview_id"], "import-cards", title="Imported cards",
        source_values=source_values, principal_id="alice", scopes=SCOPES,
    )["idempotent"]
    changed = [{**source_values[0], "authoritative_version": 7}]
    with pytest.raises(IntakeError) as stale:
        store.import_flashcards(
            "research", preview["preview_id"], "stale-import", title="Imported cards",
            source_values=changed, principal_id="alice", scopes=SCOPES,
        )
    assert stale.value.code == "source_revision_conflict"


def test_callback_generation_change_rejects_nonstable_inventory():
    generations = iter(["before", "after"])

    def callback(request):
        if "generation" in request.url.params:
            return httpx.Response(200, json={"generation": next(generations)})
        return httpx.Response(200, json={"records": [], "nextCursor": None})

    with httpx.Client(transport=httpx.MockTransport(callback), trust_env=False) as client:
        adapter = ModuloStateInventoryClient(
            "https://modulo.example", "notes-editor", "workload-secret",
            "owner-grant-secret", client=client,
        )
        with pytest.raises(IntakeError) as changed:
            adapter.inventory()
    assert changed.value.code == "generation_changed"


def test_callback_denial_does_not_expose_credentials():
    def callback(request):
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(callback), trust_env=False) as client:
        adapter = ModuloStateInventoryClient(
            "https://modulo.example", "notes-editor", "workload-secret",
            "owner-grant-secret", client=client,
        )
        with pytest.raises(IntakeError) as denied:
            adapter.inventory()
    assert denied.value.code == "modulo_access_denied"
    assert "workload-secret" not in str(denied.value)
    assert "owner-grant-secret" not in str(denied.value)

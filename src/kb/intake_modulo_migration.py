"""Owner-scoped inventory and dry-run plan for dedicated Modulo Knowledge plugins.

Noesis never writes Modulo-owned records here. Authenticated plugin-state reads
and legacy imports belong to the Modulo connector and each original plugin.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.intake_modes import IntakeError, IntakeStore, _bounded, _hash, _json, _reference, _text

CONTRACT = "noesis-modulo-intake-migration-preview-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS intake_modulo_migration_previews(
 preview_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,state_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""
_COMMON_FIELDS = {
    "id", "title", "body", "text", "url", "created_at", "updated_at", "tags",
    "source", "source_id", "source_locator", "status", "due_at", "notes",
    "relations", "attachments", "content", "question", "answer", "options",
    "decision", "rationale", "history", "card_id", "review_logs",
    "schedule_params", "schema_id", "schema_version", "relation_ids",
    "attachment_ids",
}


def _migration_reference(value: dict, namespace: str, scopes: set[str]) -> dict:
    reference = _reference(value, namespace, scopes)
    if (
        reference["kind"] == "document"
        and "operator" not in scopes
        and f"document:{reference['id']}:read" not in scopes
    ):
        raise IntakeError("unauthorized", "current document read scope is required")
    return reference


class ModuloMigrationStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def preview(
        self, namespace: str, request_key: str, inventory: dict,
        mappings: list[dict], *, principal_id: str, scopes: set[str],
        authenticated_transport: bool = False,
    ) -> dict:
        namespace = _text(namespace, "namespace", limit=128)
        request_key = _text(request_key, "request key", limit=256)
        IntakeStore._authorize({"namespace": namespace, "owner": principal_id},
                                principal_id, scopes, write=True)
        if not isinstance(inventory, dict) or set(inventory) not in ({
            "workspace_id", "account_id", "observed_at_ms", "source", "plugins"
        }, {
            "workspace_id", "account_id", "observed_at_ms", "source", "plugins", "read_receipt"
        }) or inventory["source"] not in {
            "fixture", "caller_supplied_plugin_state", "authenticated_plugin_state"
        }:
            raise IntakeError("invalid_inventory", "bounded plugin-state inventory required")
        if inventory["source"] == "authenticated_plugin_state":
            if not authenticated_transport or "read_receipt" not in inventory:
                raise IntakeError("invalid_inventory", "authenticated inventory requires a server-side Modulo read")
            receipt = inventory["read_receipt"]
            if not isinstance(receipt, dict) or set(receipt) != {
                "transport", "plugin_id", "storage_generation", "page_count",
                "record_count", "consistency", "account_identity",
            } or receipt["transport"] != "modulo_external_plugin_state_callback" or receipt["consistency"] != "non_atomic_paged_read" or receipt["account_identity"] != "not_exposed_by_callback" or type(receipt["page_count"]) is not int or not 1 <= receipt["page_count"] <= 11 or type(receipt["record_count"]) is not int or not 0 <= receipt["record_count"] <= 1000:
                raise IntakeError("invalid_inventory", "authenticated read receipt is invalid")
            _text(receipt["plugin_id"], "callback plugin ID", limit=128)
            _text(receipt["storage_generation"], "storage generation", limit=128)
        elif "read_receipt" in inventory:
            raise IntakeError("invalid_inventory", "caller inventory cannot claim a server read receipt")
        workspace = _text(inventory["workspace_id"], "workspace ID", limit=128)
        account = inventory["account_id"]
        if account is not None:
            account = _text(account, "account ID", limit=128)
        if inventory["source"] == "authenticated_plugin_state" and (workspace != "personal" or account is not None):
            raise IntakeError("invalid_inventory", "callback workspace is personal and account identity is not exposed")
        if type(inventory["observed_at_ms"]) is not int or inventory["observed_at_ms"] < 0:
            raise IntakeError("invalid_inventory", "inventory observation time required")
        plugins = inventory["plugins"]
        if not isinstance(plugins, list) or not 1 <= len(plugins) <= 30:
            raise IntakeError("invalid_inventory", "one to 30 plugin inventories required")
        if inventory["source"] == "authenticated_plugin_state" and (
            len(plugins) != 1 or not isinstance(plugins[0], dict)
            or plugins[0].get("plugin_id") != receipt["plugin_id"]
        ):
            raise IntakeError("invalid_inventory", "callback receipt must identify its single plugin namespace")
        if not isinstance(mappings, list) or len(mappings) > 1000:
            raise IntakeError("invalid_mapping", "at most 1000 exact mappings allowed")
        key_to_ref = {}
        for mapping in mappings:
            if not isinstance(mapping, dict) or set(mapping) != {
                "plugin_id", "collection", "record_id", "noesis_reference"
            }:
                raise IntakeError("invalid_mapping", "exact plugin and Noesis identities required")
            key = tuple(_text(mapping[field], field, limit=128)
                        for field in ("plugin_id", "collection", "record_id"))
            if key in key_to_ref:
                raise IntakeError("ambiguous_mapping", "two mappings target one plugin record")
            key_to_ref[key] = _migration_reference(
                mapping["noesis_reference"], namespace, scopes,
            )
        preview_id = "modulo-migration:" + _hash([namespace, principal_id, request_key])[:24]
        request_hash = _hash([inventory, mappings])
        existing = self.conn.execute(
            "SELECT request_hash FROM intake_modulo_migration_previews WHERE preview_id=?",
            [preview_id]).fetchone()
        if existing:
            if existing[0] != request_hash:
                raise IntakeError("idempotency_conflict", "migration request key identifies different inventory")
            return {**self.inspect(namespace, preview_id, principal_id=principal_id, scopes=scopes),
                    "idempotent": True}
        seen = set()
        rows = []
        collections = []
        for plugin in plugins:
            if not isinstance(plugin, dict) or set(plugin) != {
                "plugin_id", "installed_version", "collections"
            }:
                raise IntakeError("invalid_inventory", "installed plugin identity and collections required")
            plugin_id = _text(plugin["plugin_id"], "plugin ID", limit=128)
            installed_version = plugin["installed_version"]
            if installed_version is not None:
                installed_version = _text(installed_version, "installed version", limit=128)
            if inventory["source"] == "authenticated_plugin_state" and installed_version is not None:
                raise IntakeError("invalid_inventory", "callback does not expose installed plugin version")
            values = plugin["collections"]
            if not isinstance(values, list) or len(values) > 50:
                raise IntakeError("invalid_inventory", "collections exceed bound")
            for collection in values:
                if not isinstance(collection, dict) or set(collection) != {
                    "collection", "schema_id", "schema_version", "persistence", "records"
                }:
                    raise IntakeError("invalid_inventory", "collection schema and persistence required")
                name = _text(collection["collection"], "collection", limit=128)
                schema_id = collection["schema_id"]
                version = collection["schema_version"]
                if schema_id is None and version is None:
                    if inventory["source"] != "authenticated_plugin_state":
                        raise IntakeError("invalid_inventory", "authoritative schema identity required")
                elif schema_id is None or version is None or type(version) is not int or version < 1:
                    raise IntakeError("invalid_inventory", "schema identity must be complete or unknown")
                else:
                    schema_id = _text(schema_id, "schema ID", limit=256)
                persistence = collection["persistence"]
                if persistence not in {"authenticated_plugin_state", "legacy_local"}:
                    raise IntakeError("invalid_inventory", "persistence must be explicit")
                if inventory["source"] == "authenticated_plugin_state" and persistence != "authenticated_plugin_state":
                    raise IntakeError("invalid_inventory", "callback inventory must use authenticated plugin state")
                records = collection["records"]
                if not isinstance(records, list) or len(records) > 1000:
                    raise IntakeError("invalid_inventory", "collection records exceed bound")
                collections.append({"plugin_id": plugin_id, "installed_version": installed_version,
                                    "collection": name, "schema_id": schema_id,
                                    "schema_version": version, "persistence": persistence,
                                    "record_count": len(records)})
                for record in records:
                    base_record_fields = {
                        "record_id", "authoritative_version", "fields", "relation_ids",
                        "attachment_ids", "content_sha256", "source_locator"
                    }
                    permitted_record_shapes = {
                        frozenset(base_record_fields),
                        frozenset(base_record_fields | {"metadata_gaps"}),
                        frozenset(base_record_fields | {"schema_id", "schema_version", "metadata_gaps"}),
                    }
                    if not isinstance(record, dict) or frozenset(record) not in permitted_record_shapes:
                        raise IntakeError("invalid_inventory", "record metadata is incomplete")
                    record_id = _text(record["record_id"], "record ID", limit=128)
                    revision = record["authoritative_version"]
                    if type(revision) is not int or revision < 1:
                        raise IntakeError("invalid_inventory", "record version cannot be inferred")
                    key = (plugin_id, name, record_id)
                    if key in seen:
                        raise IntakeError("duplicate_record", "inventory repeats a plugin record")
                    seen.add(key)
                    fields = record["fields"]
                    relations = record["relation_ids"]
                    attachments = record["attachment_ids"]
                    if any(not isinstance(v, list) or len(v) > 100 for v in (fields, relations, attachments)):
                        raise IntakeError("invalid_inventory", "record metadata exceeds bound")
                    fields = [_text(v, "field", limit=128) for v in fields]
                    relations = [_text(v, "relation", limit=128) for v in relations]
                    attachments = [_text(v, "attachment", limit=128) for v in attachments]
                    digest = record["content_sha256"]
                    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                        raise IntakeError("invalid_inventory", "content hash must be exact SHA-256")
                    locator = record["source_locator"]
                    if locator is not None and (not isinstance(locator, dict) or set(locator) - {
                        "url", "page", "start", "end", "section"
                    }):
                        raise IntakeError("invalid_inventory", "source locator is invalid")
                    metadata_gaps = record.get("metadata_gaps", [])
                    if not isinstance(metadata_gaps, list) or len(metadata_gaps) > 20:
                        raise IntakeError("invalid_inventory", "metadata gaps exceed bound")
                    metadata_gaps = sorted({_text(gap, "metadata gap", limit=128)
                                            for gap in metadata_gaps})
                    mapped = key_to_ref.get(key)
                    record_schema_id = _text(record.get("schema_id", schema_id), "record schema ID", limit=256)
                    record_schema_version = record.get("schema_version", version)
                    if type(record_schema_version) is not int or record_schema_version < 1:
                        raise IntakeError("invalid_inventory", "authoritative record schema version required")
                    action = "link_in_place" if persistence == "authenticated_plugin_state" and mapped else "review_mapping"
                    rows.append({
                        "workspace_id": workspace, "account_id": account,
                        "plugin_id": plugin_id, "collection": name, "record_id": record_id,
                        "authoritative_version": revision, "schema_id": record_schema_id,
                        "schema_version": record_schema_version, "content_sha256": digest,
                        "relation_ids": relations, "attachment_ids": attachments,
                        "source_locator": locator, "unsupported_fields": sorted(set(fields) - _COMMON_FIELDS),
                        "metadata_gaps": metadata_gaps,
                        "noesis_reference": mapped, "action": action,
                        "requires_review": persistence == "legacy_local" or bool(set(fields) - _COMMON_FIELDS) or mapped is None or bool(metadata_gaps),
                    })
                    if len(rows) > 1000:
                        raise IntakeError("inventory_limit", "inventory exceeds 1000 records")
        if set(key_to_ref) - seen:
            raise IntakeError("invalid_mapping", "mapping references an absent plugin record")
        if inventory["source"] == "authenticated_plugin_state" and (
            len(rows) != receipt["record_count"]
        ):
            raise IntakeError("invalid_inventory", "callback record count does not match read receipt")
        counts = {
            "records": len(rows),
            "link_in_place": sum(r["action"] == "link_in_place" for r in rows),
            "legacy_import_required": sum(
                collection["record_count"] for collection in collections
                if collection["persistence"] == "legacy_local"
            ),
            "review_required": sum(r["requires_review"] for r in rows),
        }
        state = {
            "contract": CONTRACT, "preview_id": preview_id, "namespace": namespace,
            "owner": principal_id, "workspace_id": workspace, "account_id": account,
            "source": inventory["source"], "observed_at_ms": inventory["observed_at_ms"],
            "read_receipt": inventory.get("read_receipt"),
            "inventory_sha256": _hash(inventory), "collections": collections,
            "records": rows, "counts": counts, "remote_mutations": 0,
            "limitations": ([
                "Modulo authorized this plugin namespace through dual tokens; the callback does not expose account identity or installed plugin version.",
                "The callback returns a flat namespace without collection names; the inventory groups it as unclassified.",
                "Paged reads are not an atomic snapshot; re-read and compare record versions before any future owner-approved migration step.",
                "Absent relation, attachment, and locator fields are unknown, not proven empty.",
            ] if authenticated_transport else [
                "Noesis received caller-supplied metadata; current Modulo access and record contents are not independently verified.",
            ]) + [
                "Legacy browser-local records need plugin-specific import capability and owner review; no import API is assumed and originals are not deleted.",
            ],
        }
        _bounded(state, limit=4_000_000)
        self.conn.execute(
            "INSERT INTO intake_modulo_migration_previews VALUES (?,?,?,?,?,?)",
            [preview_id, namespace, principal_id, request_hash, _json(state), self.now()])
        return state

    def inspect(self, namespace: str, preview_id: str, *, principal_id: str,
                scopes: set[str], limit: int = 100, offset: int = 0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise IntakeError("invalid_page", "bounded migration page required")
        row = self.conn.execute(
            "SELECT owner,state_json FROM intake_modulo_migration_previews "
            "WHERE namespace=? AND preview_id=?", [namespace, preview_id]).fetchone()
        if not row:
            raise IntakeError("preview_unavailable", "migration preview is unavailable")
        IntakeStore._authorize({"namespace": namespace, "owner": row[0]},
                                principal_id, scopes)
        state = json.loads(row[1])
        for record in state["records"]:
            ref = record.get("noesis_reference")
            if ref and ref["namespace"] != namespace and "operator" not in scopes and f"namespace:{ref['namespace']}:read" not in scopes:
                raise IntakeError("unauthorized", "current linked namespace access required")
            if ref and ref["kind"] == "document" and "operator" not in scopes and f"document:{ref['id']}:read" not in scopes:
                raise IntakeError("unauthorized", "current linked document access required")
        state["records"] = state["records"][offset:offset + limit]
        state["next_offset"] = offset + limit if offset + limit < state["counts"]["records"] else None
        return state

    def import_flashcards(
        self, namespace: str, preview_id: str, request_key: str, *, title: str,
        source_values: list[dict], principal_id: str, scopes: set[str],
    ) -> dict:
        """Create a Noesis practice pack from exact, hash-matched plugin records.

        Plugin history and schedule data remain source provenance. No FSRS state
        is translated into the native fixed-interval scheduler.
        """
        from src.kb.intake_practice import IntakePracticeStore

        if not isinstance(source_values, list) or not 1 <= len(source_values) <= 100:
            raise IntakeError("invalid_flashcard_import", "select 1–100 complete source flashcards")
        source_values = _bounded(source_values, limit=256_000)
        preview = self.inspect(
            namespace, preview_id, principal_id=principal_id, scopes=scopes,
            limit=100, offset=0,
        )
        rows = list(preview["records"])
        next_offset = preview["next_offset"]
        while next_offset is not None:
            page = self.inspect(
                namespace, preview_id, principal_id=principal_id, scopes=scopes,
                limit=100, offset=next_offset,
            )
            rows.extend(page["records"])
            next_offset = page["next_offset"]
        available = {
            (record["plugin_id"], record["collection"], record["record_id"]): record
            for record in rows
        }
        cards = []
        imported_records = []
        plugin_records = []
        seen_source_ids: set[tuple[str, str, str]] = set()
        seen_card_ids: set[str] = set()
        for index, supplied in enumerate(source_values, start=1):
            if not isinstance(supplied, dict) or set(supplied) != {
                "plugin_id", "collection", "record_id", "authoritative_version",
                "content_sha256", "value", "mastery_criterion",
            }:
                raise IntakeError("invalid_flashcard_import", "each imported card needs exact source identity, content, and reviewed criterion")
            plugin_id = _text(supplied["plugin_id"], "plugin ID", limit=128)
            collection = _text(supplied["collection"], "collection", limit=128)
            record_id = _text(supplied["record_id"], "record ID", limit=128)
            key = (plugin_id, collection, record_id)
            if key in seen_source_ids:
                raise IntakeError("duplicate_record", "flashcard import repeats a source record")
            seen_source_ids.add(key)
            record = available.get(key)
            if plugin_id != "flashcards-spaced-repetition" or record is None:
                raise IntakeError("invalid_flashcard_import", "source record must belong to the selected flashcard plugin preview")
            version = supplied["authoritative_version"]
            if type(version) is not int or version != record["authoritative_version"]:
                raise IntakeError("source_revision_conflict", "flashcard source revision differs from its preview")
            digest = supplied["content_sha256"]
            if digest != record["content_sha256"]:
                raise IntakeError("source_hash_conflict", "flashcard source hash differs from its preview")
            value = _bounded(supplied["value"], limit=32_000)
            if not isinstance(value, dict) or _hash(value) != digest:
                raise IntakeError("source_hash_conflict", "supplied flashcard value does not match the preview hash")
            prompt_value = value.get("question", value.get("prompt"))
            if "question" in value and "prompt" in value and value["question"] != value["prompt"]:
                raise IntakeError("invalid_flashcard_import", "question and prompt fields conflict")
            source_card_id = _text(value.get("card_id", record_id), "source card ID", limit=256)
            if source_card_id in seen_card_ids:
                raise IntakeError("duplicate_record", "flashcard import repeats a source card ID")
            seen_card_ids.add(source_card_id)
            schedule = value.get("schedule_params")
            history = value.get("review_logs", value.get("history", []))
            if not isinstance(history, list) or len(history) > 500:
                raise IntakeError("invalid_flashcard_import", "preserve at most 500 source review events per card")
            if "schedule_params" in value and not isinstance(schedule, dict):
                raise IntakeError("invalid_flashcard_import", "source schedule parameters must be an object")
            algorithm = schedule.get("algorithm", "unknown_or_plugin_specific") if isinstance(schedule, dict) else "not_supplied"
            fsrs_keys = {"weights", "desired_retention", "learning_steps", "relearning_steps", "maximum_interval"}
            fsrs_detected = (
                isinstance(algorithm, str) and "fsrs" in algorithm.casefold()
            ) or (isinstance(schedule, dict) and bool(fsrs_keys & set(schedule)))
            cards.append({
                "kind": "recall",
                "prompt": _text(prompt_value, "question", limit=2000),
                "answer": _text(value.get("answer"), "answer", limit=5000),
                "mastery_criterion": _text(supplied["mastery_criterion"], "mastery criterion", limit=1000),
                "references": [{
                    "kind": "modulo_flashcard", "id": record_id,
                    "namespace": namespace, "version": version,
                }],
            })
            imported_records.append({
                "native_card_id": f"card-{index}",
                "source_card_id": source_card_id,
                "plugin_id": plugin_id,
                "workspace_id": preview["workspace_id"],
                "account_id": preview["account_id"],
                "collection": collection,
                "record_id": record_id,
                "authoritative_version": version,
                "schema_id": record["schema_id"],
                "schema_version": record["schema_version"],
                "content_sha256": digest,
                "source_value": value,
                "source_review_history": history,
                "source_schedule_params": schedule,
                "schedule_mapping": {
                    "source_algorithm": algorithm,
                    "target_algorithm": "noesis_fixed_intervals",
                    "status": "review_required",
                    "reason": "unsupported_fsrs_mapping" if fsrs_detected else "schedule_equivalence_not_verified",
                    "mapped": False,
                },
            })
            plugin_records.append({
                "plugin_id": plugin_id, "collection": collection,
                "record_id": record_id, "authoritative_version": version,
            })
        if len(plugin_records) > 100:
            raise IntakeError("invalid_flashcard_import", "flashcard import exceeds its record bound")
        provenance = {
            "contract": "noesis-intake-modulo-flashcard-import-v1",
            "preview_id": preview_id,
            "preview_source": preview["source"],
            "preview_inventory_sha256": preview["inventory_sha256"],
            "preview_observed_at_ms": preview["observed_at_ms"],
            "workspace_id": preview["workspace_id"],
            "account_id": preview["account_id"],
            "records": imported_records,
            "history_imported_as_native_reviews": False,
            "limitations": [
                "The full plugin values were caller supplied and only checked against the preview content hashes.",
                "Original review events are preserved as provenance, not converted to Noesis mastery receipts.",
                "Source schedules remain review_required; FSRS parameters are not translated to fixed intervals.",
                "A live Modulo connector must recheck source access and authoritative versions before writeback.",
            ],
        }
        return IntakePracticeStore(self.conn, now=self.now).create_pack(
            namespace, request_key, title, cards,
            principal_id=principal_id, scopes=scopes,
            _modulo_import=provenance,
        )

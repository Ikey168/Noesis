"""Read-only inventory from Modulo's dual-token external plugin-state callback.

The callback authorizes exactly one installed EXTERNAL plugin namespace per
workload token and owner grant. This adapter never writes plugin state or stores
tokens in its inventory receipt.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.kb.intake_modes import IntakeError, _hash, _text

_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_MAX_RECORDS = 1000
_MAX_PAGES = 11


class ModuloStateInventoryClient:
    def __init__(
        self, base_url: str, plugin_id: str, workload_token: str,
        grant_token: str, *, client: httpx.Client | None = None,
        now=None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise IntakeError("invalid_modulo_endpoint", "configured Modulo endpoint must be an HTTPS origin")
        if not isinstance(plugin_id, str) or not _PLUGIN_ID.fullmatch(plugin_id):
            raise IntakeError("invalid_plugin_id", "configured plugin ID is invalid")
        self.base_url = base_url.rstrip("/")
        self.plugin_id = plugin_id
        self.headers = {
            "X-Modulo-Plugin-Token": _text(workload_token, "workload token", limit=4096),
            "X-Modulo-State-Grant": _text(grant_token, "owner grant token", limit=4096),
            "Accept": "application/json",
        }
        self.client = client or httpx.Client(
            timeout=5, follow_redirects=False, trust_env=False,
        )
        self.now = now or (lambda: int(time.time() * 1000))

    def _get(self, params: dict[str, Any]) -> Any:
        path = f"/api/plugin-state/callback/workspaces/personal/{self.plugin_id}"
        try:
            response = self.client.get(
                self.base_url + path, params=params, headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise IntakeError("modulo_unavailable", "Modulo plugin state could not be reached") from exc
        if response.status_code in {401, 403, 404}:
            raise IntakeError("modulo_access_denied", "Modulo plugin-state grant or namespace is unavailable")
        if response.status_code != 200:
            raise IntakeError("modulo_unavailable", "Modulo plugin-state read failed")
        if len(response.content) > 2_000_000:
            raise IntakeError("inventory_limit", "Modulo plugin-state response exceeds bound")
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise IntakeError("invalid_modulo_response", "Modulo plugin state returned invalid JSON") from exc

    def _generation(self) -> str:
        value = self._get({"generation": ""})
        if not isinstance(value, dict) or set(value) != {"generation"}:
            raise IntakeError("invalid_modulo_response", "Modulo storage generation is missing")
        return _text(value["generation"], "storage generation", limit=128)

    @staticmethod
    def _metadata(record: dict) -> dict:
        required = {"key", "schemaId", "schemaVersion", "version", "value", "deleted"}
        if not isinstance(record, dict) or not required <= set(record):
            raise IntakeError("invalid_modulo_response", "Modulo record identity or version is missing")
        if record["deleted"] is not False:
            raise IntakeError("invalid_modulo_response", "live record page contained a tombstone")
        key = _text(record["key"], "record key", limit=128)
        schema_id = _text(record["schemaId"], "schema ID", limit=256)
        version = record["version"]
        schema_version = record["schemaVersion"]
        if type(version) is not int or version < 1 or type(schema_version) is not int or schema_version < 1:
            raise IntakeError("invalid_modulo_response", "Modulo record needs authoritative versions")
        value = record["value"]
        try:
            encoded_value = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise IntakeError("invalid_modulo_response", "Modulo state value must be finite JSON") from exc
        if len(encoded_value) > 1_048_576:
            raise IntakeError("inventory_limit", "Modulo record exceeds state size bound")
        fields = sorted(value) if isinstance(value, dict) else []
        if len(fields) > 100 or any(not isinstance(field, str) or not field or len(field) > 128 for field in fields):
            raise IntakeError("invalid_modulo_response", "record field names exceed inventory bounds")
        gaps = ["collection", "installed_version"]
        metadata = {}
        for field in ("relation_ids", "attachment_ids"):
            if isinstance(value, dict) and field in value:
                values = value[field]
                if not isinstance(values, list) or len(values) > 100 or any(
                    not isinstance(item, str) or not item or len(item) > 128 for item in values
                ):
                    raise IntakeError("invalid_modulo_response", f"{field} cannot be inventoried")
                metadata[field] = values
            else:
                metadata[field] = []
                gaps.append(field)
        if isinstance(value, dict) and "source_locator" in value:
            locator = value["source_locator"]
            if locator is not None and (not isinstance(locator, dict) or set(locator) - {
                "url", "page", "start", "end", "section",
            }):
                raise IntakeError("invalid_modulo_response", "source locator is invalid")
        else:
            locator = None
            gaps.append("source_locator")
        return {
            "record_id": key, "authoritative_version": version,
            "schema_id": schema_id, "schema_version": schema_version,
            "fields": fields, "content_sha256": _hash(value),
            "source_locator": locator, "metadata_gaps": gaps,
            **metadata,
        }

    def inventory(self) -> dict:
        generation = self._generation()
        cursor = None
        seen_cursors = set()
        seen_keys = set()
        records: list[dict] = []
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page = self._get(params)
            pages += 1
            if not isinstance(page, dict) or set(page) != {"records", "nextCursor"} or not isinstance(page["records"], list) or len(page["records"]) > 100:
                raise IntakeError("invalid_modulo_response", "Modulo record page is malformed")
            for raw in page["records"]:
                record = self._metadata(raw)
                if record["record_id"] in seen_keys:
                    raise IntakeError("duplicate_record", "Modulo page repeats a state key")
                seen_keys.add(record["record_id"])
                records.append(record)
                if len(seen_keys) > _MAX_RECORDS:
                    raise IntakeError("inventory_limit", "Modulo plugin inventory exceeds 1000 records")
            next_cursor = page["nextCursor"]
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors or pages >= _MAX_PAGES:
                raise IntakeError("invalid_modulo_response", "Modulo pagination did not converge")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if self._generation() != generation:
            raise IntakeError("generation_changed", "Modulo storage restore occurred during inventory")
        # The callback exposes a flat key namespace, not named collections.
        # Preserve each record's authoritative schema separately and label the
        # grouping as unknown instead of treating schema IDs as collection IDs.
        collections = [{
            "collection": "unclassified", "schema_id": None,
            "schema_version": None, "persistence": "authenticated_plugin_state",
            "records": records,
        }]
        return {
            "workspace_id": "personal", "account_id": None,
            "observed_at_ms": self.now(), "source": "authenticated_plugin_state",
            "read_receipt": {
                "transport": "modulo_external_plugin_state_callback",
                "plugin_id": self.plugin_id, "storage_generation": generation,
                "page_count": pages, "record_count": len(seen_keys),
                "consistency": "non_atomic_paged_read",
                "account_identity": "not_exposed_by_callback",
            },
            "plugins": [{"plugin_id": self.plugin_id,
                         "installed_version": None,
                         "collections": collections}],
        }

"""Read-only Zotero v3 synchronization into owner-scoped revision histories."""

import json
import re
import time

from src.kb.research_projects import _hash, _json

READ_SCOPE = "knowledge:zotero:read"
WRITE_SCOPE = "knowledge:zotero:sync"
_DDL = """
CREATE TABLE IF NOT EXISTS zotero_library_syncs(
 namespace TEXT NOT NULL,owner TEXT NOT NULL,library TEXT NOT NULL,version BIGINT NOT NULL,
 receipt_json TEXT NOT NULL,PRIMARY KEY(namespace,owner,library));
CREATE TABLE IF NOT EXISTS zotero_item_revisions(
 namespace TEXT NOT NULL,owner TEXT NOT NULL,library TEXT NOT NULL,item_key TEXT NOT NULL,
 version BIGINT NOT NULL,content_hash TEXT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(namespace,owner,library,item_key,version));
CREATE TABLE IF NOT EXISTS zotero_item_current(
 namespace TEXT NOT NULL,owner TEXT NOT NULL,library TEXT NOT NULL,item_key TEXT NOT NULL,
 version BIGINT NOT NULL,external_state TEXT NOT NULL,observed_version BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,library,item_key));
"""


class ZoteroSyncError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _authorize(namespace, principal_id, scopes, write=False):
    if not isinstance(namespace, str) or not namespace or not principal_id:
        raise ZoteroSyncError("unauthorized", "namespace and current principal are required")
    if "operator" in scopes:
        return
    if (WRITE_SCOPE if write else READ_SCOPE) not in scopes or f"namespace:{namespace}:write" not in scopes and (write or f"namespace:{namespace}:read" not in scopes):
        raise ZoteroSyncError("unauthorized", "current Zotero and namespace access are required")


class ZoteroReadClient:
    """Pyzotero adapter with explicit mode, bounded transport, and no writes."""
    def __init__(self, library_id, library_type, *, mode="web", api_key=None, timeout_seconds=60, max_bytes=32*1024*1024):
        if mode not in {"web", "local"} or library_type not in {"user", "group"} or not re.fullmatch(r"[0-9]+", str(library_id)):
            raise ZoteroSyncError("invalid_library", "explicit Web/local mode, user/group type and numeric library ID are required")
        if not 1 <= timeout_seconds <= 300 or not 1024 <= max_bytes <= 128*1024*1024:
            raise ZoteroSyncError("invalid_budget", "unsupported request time or byte budget")
        if mode == "local" and api_key:
            raise ZoteroSyncError("invalid_credentials", "Web API keys are never sent to the local API")
        try:
            from pyzotero import zotero
            import httpx2 as http
        except ImportError:
            raise ZoteroSyncError("provider_unavailable", "install Noesis's bibliography extra for Pyzotero support") from None
        deadline, remaining_bytes = time.monotonic() + timeout_seconds, [max_bytes]
        endpoint = "https://api.zotero.org" if mode == "web" else "http://localhost:23119/api"
        headers = {"Zotero-API-Version": "3"}
        if api_key:
            headers["Zotero-API-Key"] = api_key
        transport = http.Client(headers=headers, follow_redirects=False, trust_env=False)
        self._transport = transport

        class BoundedClient:
            def get(self, url, *, params=None, headers=None, **kwargs):
                if not str(url).startswith(endpoint + "/") and str(url) != endpoint:
                    raise ZoteroSyncError("unexpected_endpoint", "Zotero reads cannot follow an external endpoint")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ZoteroSyncError("deadline_exceeded", "Zotero sync deadline exhausted")
                with transport.stream("GET", url, params=params, headers=headers, timeout=min(remaining, 20)) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        remaining_bytes[0] -= len(chunk)
                        if remaining_bytes[0] < 0 or time.monotonic() >= deadline:
                            raise ZoteroSyncError("budget_exceeded", "Zotero response exceeded time or byte budget")
                        data.extend(chunk)
                    if 300 <= response.status_code < 400:
                        raise ZoteroSyncError("unexpected_redirect", "attachment and external redirects are not followed")
                    return http.Response(response.status_code, headers=response.headers, content=bytes(data), request=response.request)

            def close(self):
                transport.close()

        class ReadOnlyZotero(zotero.Zotero):
            def _check_backoff(self):
                if self.backoff_until > time.time():
                    raise ZoteroSyncError("rate_limited", "Zotero requested backoff; retry this unchanged checkpoint later")

        self.mode = mode
        self.client = ReadOnlyZotero(str(library_id), library_type, api_key=api_key,
                                    local=mode == "local", client=BoundedClient())
        try:
            version = self.client.last_modified_version()
            response_headers = self.client.request.headers
            if response_headers.get("Zotero-API-Version") != "3":
                raise ZoteroSyncError("unsupported_api_version", "Zotero v3 negotiation failed")
            server = response_headers.get("Zotero-Server-ID") if mode == "local" else "web"
            if not server:
                raise ZoteroSyncError("local_version_identity_unavailable", "persistent local sync requires a Zotero server ID; older local version counters are unsafe")
            self.library = f"{mode}:{server}:{library_type}:{library_id}"
            self.server_id = server
            self.version = version
        except Exception:
            transport.close()
            raise

    def close(self):
        self._transport.close()

    def _check_response(self):
        headers = self.client.request.headers
        if headers.get("Zotero-API-Version") != "3" or int(headers.get("Last-Modified-Version", -1)) != self.version:
            raise ZoteroSyncError("remote_changed", "library changed during sync; retry without advancing the checkpoint")
        if self.mode == "local" and headers.get("Zotero-Server-ID") != self.server_id:
            raise ZoteroSyncError("server_changed", "local Zotero instance changed during synchronization")

    def changes(self, since, *, max_items=10000):
        if since > self.version:
            raise ZoteroSyncError("remote_version_reversed", "remote version is older than the partition checkpoint")
        items, seen = [], set()
        start = 0
        while True:
            page = self.client.items(since=since, start=start, limit=100, includeTrashed=1, include="data,csljson,bibtex")
            self._check_response()
            if not isinstance(page, list):
                raise ZoteroSyncError("invalid_response", "Zotero items response must be a list")
            for item in page:
                key = item.get("key") if isinstance(item, dict) else None
                if key in seen:
                    raise ZoteroSyncError("pagination_repeated", "Zotero pagination repeated an item")
                seen.add(key)
                items.append(item)
                if len(items) > max_items:
                    raise ZoteroSyncError("item_budget_exceeded", "Zotero changes exceed the explicit item budget")
            if len(page) < 100:
                break
            start += len(page)
        deleted = self.client.deleted(since=since)
        self._check_response()
        if not isinstance(deleted, dict) or not isinstance(deleted.get("items", []), list):
            raise ZoteroSyncError("invalid_response", "Zotero deletion response is malformed")
        deleted_items = deleted.get("items", [])
        if len(deleted_items) > max_items:
            raise ZoteroSyncError("item_budget_exceeded", "Zotero deletions exceed the explicit item budget")
        self.client.last_modified_version()
        self._check_response()
        return {"version": self.version, "items": items, "deleted": deleted_items}


class ZoteroSyncStore:
    def __init__(self, conn, *, initialize=True):
        self.conn = conn
        if initialize:
            conn.execute(_DDL)

    def sync(self, namespace, client, *, principal_id, scopes, max_items=10000):
        _authorize(namespace, principal_id, scopes, write=True)
        if type(max_items) is not int or not 1 <= max_items <= 10000:
            raise ZoteroSyncError("invalid_budget", "item budget must be one to 10000")
        identity = [namespace, principal_id, client.library]
        prior = self.conn.execute("SELECT version FROM zotero_library_syncs WHERE namespace=? AND owner=? AND library=?", identity).fetchone()
        since = int(prior[0]) if prior else 0
        changes = client.changes(since, max_items=max_items)
        version = changes["version"]
        if type(version) is not int or version < since or len(changes["items"]) > max_items or len(changes["deleted"]) > max_items:
            raise ZoteroSyncError("invalid_response", "invalid remote version or change bounds")
        if prior and version == since and not changes["items"] and not changes["deleted"]:
            saved = self.conn.execute("SELECT receipt_json FROM zotero_library_syncs WHERE namespace=? AND owner=? AND library=? AND version=?", [*identity, since]).fetchone()
            if saved:
                return {**json.loads(saved[0]), "idempotent": True}
        normalized = []
        for item in changes["items"]:
            if not isinstance(item, dict) or not re.fullmatch(r"[A-Z0-9]{8}", str(item.get("key", ""))):
                raise ZoteroSyncError("invalid_item", "Zotero item key must have eight uppercase alphanumeric characters")
            data = item.get("data")
            item_version = item.get("version")
            if not isinstance(data, dict) or type(item_version) is not int or not 0 <= item_version <= version or data.get("key") != item["key"] or data.get("version") != item_version:
                raise ZoteroSyncError("invalid_item", "Zotero item and data versions must agree")
            annotation = None
            if data.get("itemType") == "annotation":
                annotation = {"text": data.get("annotationText"), "comment": data.get("annotationComment"),
                              "anchor": data.get("annotationPosition"), "anchor_status": "unsupported-zotero-coordinate-space"}
            record = {"library": client.library, "key": item["key"], "version": item_version, "data": data,
                      "csljson": item.get("csljson"), "bibtex": item.get("bibtex"), "annotation": annotation,
                      "attachment": {"status": "not-fetched", "item_key": item["key"], "parent_item": data.get("parentItem"),
                          "link_mode": data.get("linkMode"), "content_type": data.get("contentType")} if data.get("itemType") == "attachment" else None}
            if len(_json(record).encode()) > 4*1024*1024:
                raise ZoteroSyncError("item_budget_exceeded", "Zotero item exceeds 4 MiB")
            normalized.append(record)
        if any(not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key) for key in changes["deleted"]):
            raise ZoteroSyncError("invalid_response", "invalid deleted item key")
        self.conn.execute("BEGIN")
        try:
            current = self.conn.execute("SELECT version FROM zotero_library_syncs WHERE namespace=? AND owner=? AND library=?", identity).fetchone()
            if current != prior:
                raise ZoteroSyncError("sync_conflict", "another sync advanced this library; retry")
            for record in normalized:
                key, item_version, digest = record["key"], record["version"], _hash(record)
                old = self.conn.execute("SELECT content_hash FROM zotero_item_revisions WHERE namespace=? AND owner=? AND library=? AND item_key=? AND version=?", [*identity, key, item_version]).fetchone()
                if old and old[0] != digest:
                    raise ZoteroSyncError("item_version_conflict", "same Zotero item version has different content")
                latest = self.conn.execute("SELECT version FROM zotero_item_current WHERE namespace=? AND owner=? AND library=? AND item_key=?", [*identity, key]).fetchone()
                if latest and latest[0] > item_version:
                    raise ZoteroSyncError("item_version_conflict", "item version cannot move backwards")
                self.conn.execute("INSERT OR IGNORE INTO zotero_item_revisions VALUES (?,?,?,?,?,?,?)", [*identity, key, item_version, digest, _json(record)])
                self.conn.execute("INSERT INTO zotero_item_current VALUES (?,?,?,?,?,?,?) ON CONFLICT(namespace,owner,library,item_key) DO UPDATE SET version=excluded.version,external_state=excluded.external_state,observed_version=excluded.observed_version",
                    [*identity, key, item_version, "trashed" if record["data"].get("deleted") else "active", version])
            for key in changes["deleted"]:
                self.conn.execute("UPDATE zotero_item_current SET external_state='deleted',observed_version=? WHERE namespace=? AND owner=? AND library=? AND item_key=?", [version, *identity, key])
            receipt = {"library": client.library, "previous_version": since, "version": version,
                       "items_received": len(normalized), "deletions_received": len(changes["deleted"]),
                       "retention": "local-history-retained", "write_back": False,
                       "change_hash": _hash(changes), "receipt_id": "zotero-sync:" + _hash([identity, since, changes])[:32]}
            self.conn.execute("INSERT INTO zotero_library_syncs VALUES (?,?,?,?,?) ON CONFLICT(namespace,owner,library) DO UPDATE SET version=excluded.version,receipt_json=excluded.receipt_json", [*identity, version, _json(receipt)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            import duckdb
            self.conn.execute("ROLLBACK")
            if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
                raise ZoteroSyncError("sync_conflict", "concurrent sync; retry the unchanged checkpoint") from exc
            raise
        return receipt

    def items(self, namespace, library, *, principal_id, scopes, include_deleted=False, limit=100, offset=0):
        _authorize(namespace, principal_id, scopes)
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise ZoteroSyncError("invalid_budget", "item pages require limit 1..1000 and nonnegative offset")
        rows = self.conn.execute("""SELECT r.content_json,c.external_state FROM zotero_item_current c JOIN zotero_item_revisions r
            USING(namespace,owner,library,item_key,version) WHERE namespace=? AND owner=? AND library=?
            AND (? OR c.external_state='active') ORDER BY item_key LIMIT ? OFFSET ?""", [namespace, principal_id, library, include_deleted, limit, offset]).fetchall()
        return {"items": [{**json.loads(row[0]), "citation_key": self.citation_key(namespace, principal_id, library, json.loads(row[0])["key"]),
                           "external_state": row[1], "local_retention": "retained"} for row in rows],
                "next_offset": offset + limit if len(rows) == limit else None}

    @staticmethod
    def citation_key(namespace, principal_id, library, key):
        return "zotero_" + _hash([namespace, principal_id, library])[:16] + "_" + key

    def inspect_item(self, namespace, library, key, *, principal_id, scopes, version=None):
        _authorize(namespace, principal_id, scopes)
        row = self.conn.execute("""SELECT r.content_json,c.external_state,c.observed_version FROM zotero_item_current c
            JOIN zotero_item_revisions r ON r.namespace=c.namespace AND r.owner=c.owner AND r.library=c.library AND r.item_key=c.item_key
            WHERE c.namespace=? AND c.owner=? AND c.library=? AND c.item_key=? AND r.version=coalesce(?,c.version)""",
            [namespace, principal_id, library, key, version]).fetchone()
        if not row:
            raise ZoteroSyncError("item_unavailable", "selected item revision is unavailable")
        return {**json.loads(row[0]), "current_external_state": row[1], "external_state_observed_version": row[2],
                "local_retention": "retained", "citation_key": self.citation_key(namespace, principal_id, library, key)}

    def export_bibliography(self, namespace, library, item_keys, *, principal_id, scopes, report_id=None, report_namespace=None, item_versions=None):
        _authorize(namespace, principal_id, scopes)
        if not isinstance(item_keys, list) or not 1 <= len(item_keys) <= 1000 or len(set(item_keys)) != len(item_keys):
            raise ZoteroSyncError("invalid_budget", "select one to 1000 unique item keys")
        if item_versions is not None and (not isinstance(item_versions, dict) or set(item_versions) - set(item_keys)
                or any(type(version) is not int or version < 0 for version in item_versions.values())):
            raise ZoteroSyncError("invalid_version", "explicit bibliography versions must refer to selected item keys")
        import bibtexparser
        from bibtexparser.bibdatabase import BibDatabase
        from bibtexparser.bwriter import BibTexWriter
        csl, entries, sources = [], [], []
        for key in item_keys:
            selected_version = (item_versions or {}).get(key)
            item = self.inspect_item(namespace, library, key, version=selected_version, principal_id=principal_id, scopes=scopes)
            if selected_version is None and item["current_external_state"] != "active":
                raise ZoteroSyncError("item_unavailable", "bibliography item is unavailable or externally deleted")
            csl_item = item.get("csljson")
            if isinstance(csl_item, str):
                csl_item = json.loads(csl_item)
            if isinstance(csl_item, list) and len(csl_item) == 1:
                csl_item = csl_item[0]
            if not isinstance(csl_item, dict) or not csl_item.get("type") or not isinstance(item.get("bibtex"), str):
                raise ZoteroSyncError("bibliography_unavailable", "item lacks Zotero's CSL JSON/BibTeX representations")
            parsed = bibtexparser.loads(item["bibtex"])
            if len(parsed.entries) != 1:
                raise ZoteroSyncError("bibliography_invalid", "each Zotero item must export exactly one BibTeX entry")
            citation_key = self.citation_key(namespace, principal_id, library, key)
            csl.append({**csl_item, "id": citation_key})
            entries.append({**parsed.entries[0], "ID": citation_key})
            sources.append({"citation_key": citation_key, "library": library, "item_key": key, "item_version": item["version"],
                            "current_external_state": item["current_external_state"]})
        database = BibDatabase()
        database.entries = entries
        writer = BibTexWriter()
        writer.order_entries_by = None
        bibtex = writer.write(database)
        if bibtexparser.loads(bibtex).entries != entries:
            raise ZoteroSyncError("bibliography_roundtrip_failed", "BibTeX field values did not survive serialization")
        report_reference = None
        if report_id:
            from src.kb.authored_reports import AuthoredReportStore
            report = AuthoredReportStore(self.conn, initialize=False).inspect(report_namespace or namespace, report_id, principal_id=principal_id, scopes=scopes)
            cited = {citation for section in report["content"]["sections"] for assertion in section["assertions"] for citation in assertion["citations"]}
            if cited - {item["id"] for item in csl}:
                raise ZoteroSyncError("citation_closure_failed", "report cites bibliography entries absent from this export")
            report_reference = {"report_id": report_id, "revision": report["revision"], "report_hash": _hash(report),
                                "evidence_dependencies": [dep for section in report["content"]["sections"] for assertion in section["assertions"] for dep in assertion["dependencies"]]}
        result = {"contract": "noesis-bibliography-export-v1", "csl_json": csl, "bibtex": bibtex,
                  "bibliography_revisions": sources, "report": report_reference,
                  "limitations": ["Publication bibliography versions are separate from cited evidence revisions", "Export does not publish or write back to Zotero"]}
        return {**result, "sha256": _hash(result)}

import copy

import duckdb
import pytest

from src.ingestion.zotero_sync import ZoteroSyncStore, ZoteroReadClient, ZoteroSyncError

AUTH = {"principal_id": "alice", "scopes": {"knowledge:zotero:read", "knowledge:zotero:sync", "namespace:r:write"}}


def item(key="ABCDEFGH", version=1, **fields):
    return {"key": key, "version": version,
        "data": {"key": key, "version": version, "itemType": "book", "title": "Étude", "collections": ["COLLECT1"],
                 "tags": [{"tag": "research"}], "creators": [{"creatorType": "author", "name": "World Organization"}], **fields},
        "csljson": {"type": "book", "title": "Étude", "author": [{"literal": "World Organization"}], "edition": "2"},
        "bibtex": "@book{original, title={Étude}, author={{World Organization}}, edition={2}}"}


class Client:
    library = "web:web:user:1"
    def __init__(self, version=1, items=None, deleted=None):
        self.result = {"version": version, "items": [item()] if items is None else items, "deleted": deleted or []}
        self.since = []
    def changes(self, since, **kwargs):
        self.since.append(since)
        return copy.deepcopy(self.result)


def test_incremental_versions_deletion_and_private_history_survive_restart(tmp_path):
    path = str(tmp_path / "zotero.duckdb")
    conn = duckdb.connect(path)
    store = ZoteroSyncStore(conn)
    client = Client()
    store.sync("r", client, **AUTH)
    exported = store.items("r", client.library, **AUTH)["items"][0]
    assert exported["data"]["collections"] == ["COLLECT1"] and exported["data"]["tags"]
    second = Client(2, [item(version=2, title="Corrected title")])
    store.sync("r", second, **AUTH)
    assert second.since == [1]
    store.sync("r", Client(3, [], ["ABCDEFGH"]), **AUTH)
    assert store.items("r", client.library, **AUTH)["items"] == []
    assert store.items("r", client.library, include_deleted=True, **AUTH)["items"][0]["external_state"] == "deleted"
    assert store.inspect_item("r", client.library, "ABCDEFGH", version=1, **AUTH)["data"]["title"] == "Étude"
    assert store.sync("r", Client(3, []), **AUTH)["idempotent"]
    conn.close()
    conn = duckdb.connect(path)
    store = ZoteroSyncStore(conn)
    assert conn.execute("SELECT count(*) FROM zotero_item_revisions").fetchone()[0] == 2
    assert store.items("r", client.library, include_deleted=True, **{**AUTH, "principal_id": "bob"})["items"] == []
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='documents'").fetchone()


def test_partial_fetch_or_version_conflict_does_not_advance_checkpoint():
    store = ZoteroSyncStore(duckdb.connect())
    store.sync("r", Client(), **AUTH)
    with pytest.raises(ZoteroSyncError, match="different content"):
        store.sync("r", Client(2, [item(title="Illegal same-version change")]), **AUTH)
    assert store.conn.execute("SELECT version FROM zotero_library_syncs").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM zotero_item_revisions").fetchone()[0] == 1
    with pytest.raises(ZoteroSyncError):
        store.sync("r", Client(2, [item(version=2), item(key="bad", version=2)]), **AUTH)
    assert store.conn.execute("SELECT version FROM zotero_library_syncs").fetchone()[0] == 1


def test_note_annotation_and_attachment_remain_distinct():
    store = ZoteroSyncStore(duckdb.connect())
    items = [item(itemType="note", note="<p>Reviewer's local note</p>"),
             item(key="ATTACH01", itemType="attachment", parentItem="ABCDEFGH", linkMode="linked_file", path="/local/paper.pdf"),
             item(key="ANNOT001", itemType="annotation", parentItem="ATTACH01", annotationText="Evidence", annotationPosition='{"pageIndex":0,"rects":[]}')]
    store.sync("r", Client(items=items), **AUTH)
    by_key = {v["key"]: v for v in store.items("r", Client.library, **AUTH)["items"]}
    assert by_key["ABCDEFGH"]["data"]["note"].startswith("<p>")
    assert by_key["ATTACH01"]["attachment"]["status"] == "not-fetched"
    assert by_key["ANNOT001"]["annotation"]["anchor_status"].startswith("unsupported")


def test_bibliography_unicode_corporate_authors_editions_and_report_closure():
    pytest.importorskip("bibtexparser")
    from src.kb.authored_reports import AuthoredReportStore
    store = ZoteroSyncStore(duckdb.connect())
    first, second = item(), item(key="EDITION3")
    second["csljson"]["edition"] = "3"
    second["bibtex"] = second["bibtex"].replace("edition={2}", "edition={3}")
    store.sync("r", Client(items=[first, second]), **AUTH)
    result = store.export_bibliography("r", Client.library, ["ABCDEFGH", "EDITION3"], **AUTH)
    assert len({v["id"] for v in result["csl_json"]}) == 2
    assert result["csl_json"][0]["author"] == [{"literal": "World Organization"}]
    assert "Étude" in result["bibtex"] and "DOI" not in result["csl_json"][0]
    report_auth = {**AUTH, "scopes": AUTH["scopes"] | {"knowledge:reports:read", "knowledge:reports:write"}}
    citation = result["csl_json"][0]["id"]
    report = AuthoredReportStore(store.conn).create("r", "report", {"title": "Report", "snapshot": {"id": "s", "generations": {"r": 1}},
        "sections": [{"id": "s", "title": "Notes", "assertions": [{"id": "a", "text": "Author commentary", "kind": "commentary", "dependencies": [], "citations": [citation]}]}],
        "bibliography": [{"id": citation, "text": "Citation"}], "limitations": []}, **report_auth)
    assert store.export_bibliography("r", Client.library, ["ABCDEFGH"], report_id=report["report_id"], **report_auth)["report"]["revision"] == 1
    with pytest.raises(ZoteroSyncError, match="absent"):
        store.export_bibliography("r", Client.library, ["EDITION3"], report_id=report["report_id"], **report_auth)
    store.sync("r", Client(2, [], ["ABCDEFGH"]), **AUTH)
    retained = store.export_bibliography("r", Client.library, ["ABCDEFGH"], item_versions={"ABCDEFGH": 1}, **AUTH)
    assert retained["bibliography_revisions"][0]["current_external_state"] == "deleted"
    assert retained["csl_json"][0]["title"] == "Étude"


@pytest.mark.parametrize("mode", ["web", "local"])
def test_real_pyzotero_transport_negotiation_and_read_only_requests(monkeypatch, mode):
    pytest.importorskip("pyzotero.zotero")
    http = pytest.importorskip("httpx2")
    requests = []
    original = http.Client
    def handler(request):
        requests.append(request)
        headers = {"Zotero-API-Version": "3", "Last-Modified-Version": "1", "Content-Type": "application/json"}
        if mode == "local":
            headers["Zotero-Server-ID"] = "instance-one"
        payload = {"items": []} if request.url.path.endswith("/deleted") else [item()]
        return http.Response(200, headers=headers, json=payload)
    monkeypatch.setattr(http, "Client", lambda **kwargs: original(transport=http.MockTransport(handler), **kwargs))
    client = ZoteroReadClient("1", "user", mode=mode, api_key="test-key" if mode == "web" else None)
    try:
        result = client.changes(0)
        assert result["items"][0]["key"] == "ABCDEFGH"
        assert all(request.method == "GET" for request in requests)
        assert all(request.headers["Zotero-API-Version"] == "3" for request in requests)
        assert any(request.url.params.get("includeTrashed") == "1" for request in requests)
        if mode == "local":
            assert "instance-one" in client.library
            assert all("Zotero-API-Key" not in request.headers for request in requests)
    finally:
        client.close()


def test_changed_remote_version_and_legacy_local_do_not_publish(monkeypatch):
    pytest.importorskip("pyzotero")
    http = pytest.importorskip("httpx2")
    original = http.Client
    calls = []
    def handler(request):
        calls.append(request)
        headers = {"Zotero-API-Version": "3", "Last-Modified-Version": str(len(calls)), "Content-Type": "application/json"}
        return http.Response(200, headers=headers, json=[item()])
    monkeypatch.setattr(http, "Client", lambda **kwargs: original(transport=http.MockTransport(handler), **kwargs))
    client = ZoteroReadClient("1", "user")
    store = ZoteroSyncStore(duckdb.connect())
    try:
        with pytest.raises(ZoteroSyncError, match="changed during"):
            store.sync("r", client, **AUTH)
        assert store.conn.execute("SELECT count(*) FROM zotero_library_syncs").fetchone()[0] == 0
    finally:
        client.close()
    with pytest.raises(ZoteroSyncError, match="server ID"):
        ZoteroReadClient("0", "user", mode="local")

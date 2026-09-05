import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_public_sync_history_and_credentials_are_separate(tmp_path, monkeypatch):
    from src.ingestion import zotero_sync
    path = str(tmp_path / "zotero.duckdb")
    scopes = {"knowledge:zotero:read", "knowledge:zotero:sync", "namespace:r:write"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    created = []
    class Client:
        library = "web:web:user:1"
        def __init__(self, *args, **kwargs):
            created.append(kwargs)
        def changes(self, since, **kwargs):
            return {"version": 1, "items": [{"key": "ABCDEFGH", "version": 1, "data": {"key": "ABCDEFGH", "version": 1, "itemType": "book", "title": "A book"}}], "deleted": []}
        def close(self):
            pass
    monkeypatch.setattr(zotero_sync, "ZoteroReadClient", Client)
    tools = asyncio.run(server.mcp.get_tools())
    request = dict(namespace="r", library_id="1", library_type="user")
    denied = tools["sync_zotero_library"].fn(**request, credential_env="NOESIS_ZOTERO_API_KEY")
    assert denied["error"]["code"] == "unauthorized" and not created
    result = tools["sync_zotero_library"].fn(**request)
    assert result["version"] == 1
    items = tools["list_zotero_items"].fn(namespace="r", library=Client.library)
    assert items["items"][0]["data"]["title"] == "A book"
    assert tools["inspect_zotero_item"].fn(namespace="r", library=Client.library, key="ABCDEFGH", version=1)["version"] == 1
    scopes.remove("namespace:r:write")
    assert tools["inspect_zotero_item"].fn(namespace="r", library=Client.library, key="ABCDEFGH")["error"]["code"] == "unauthorized"
    assert _mutability("sync_zotero_library") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "sync_zotero_library") == ["knowledge:zotero:sync"]

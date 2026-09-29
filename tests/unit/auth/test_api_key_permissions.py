"""API-key permissions per KB domain and for document ingest (issue #1784).

Runs across both KB domain backings (corpus view and promoted namespace) and
both API-key stores (DuckDB ``local_api_keys`` and the DynamoDB manager, the
latter against an in-memory table).
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import api_key_manager as manager_module
from src.api.auth import local_api_keys
from src.api.auth.api_key_middleware import APIKeyAuthMiddleware
from src.api.routes import document_routes, kb_routes
from src.ingestion.document_store import DocumentStore
from src.kb import contract
from src.kb.membership import run_membership_pass
from src.kb.promotion import promote_to_namespace
from src.kb.registry import load_registry

CONFIG = """
version: 1
domains:
  - name: support
    backing: corpus-view
    embedding_model: fake-embed
    tags: [support]
    keywords: [refund, ticket]
  - name: web3
    backing: corpus-view
    embedding_model: fake-embed
    tags: [web3]
    keywords: [defi, staking]
"""
BASE_MS = 1_790_000_000_000


class FakeTable:
    """Just enough of a boto3 DynamoDB Table for the API key store."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["key_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["key_id"])
        return {"Item": dict(item)} if item else {}

    def query(self, IndexName, KeyConditionExpression, ExpressionAttributeValues):
        attribute = {"user-id-index": "user_id", "key-prefix-index": "key_prefix"}[IndexName]
        value = next(iter(ExpressionAttributeValues.values()))
        return {"Items": [dict(i) for i in self.items.values() if i.get(attribute) == value]}

    def update_item(self, **_kwargs):
        return {}


class _Registry:
    def __init__(self, registry, conn):
        self._registry, self._conn = registry, conn

    def resolve(self, name, conn=None):
        return self._registry.resolve(name, conn=self._conn)

    def __getattr__(self, name):
        return getattr(self._registry, name)


def _seed(conn, config_path):
    DocumentStore(conn).upsert([
        {"document_id": "w1", "source_type": "news", "language": "en", "ingested_at": BASE_MS, "source_id": "wire",
         "url": "https://example.com/w1", "title": "Defi staking news", "content": "defi staking coverage continues.",
         "metadata": {"tags": ["web3"]}},
        {"document_id": "s1", "source_type": "note", "language": "en", "ingested_at": BASE_MS, "source_id": "desk",
         "url": "https://example.com/s1", "title": "Refund ticket policy", "content": "refund ticket handling steps.",
         "metadata": {"tags": ["support"]}},
    ])
    run_membership_pass(conn, load_registry(config_path))


@pytest.fixture(params=[("corpus-view", "local"), ("corpus-view", "dynamodb"),
                        ("namespace", "local"), ("namespace", "dynamodb")],
                ids=lambda p: f"{p[0]}-{p[1]}")
def env(request, tmp_path, monkeypatch):
    kb_backing, key_backend = request.param
    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    _seed(conn, config_path)
    if kb_backing == "namespace":
        for domain in ("support", "web3"):
            promote_to_namespace(conn, domain, config_path)
    registry = _Registry(load_registry(config_path), conn)
    monkeypatch.setattr(contract, "_registry", lambda config_path=None: registry)
    document_routes.use_store_for_testing(DocumentStore(conn))
    monkeypatch.delenv("NEURONEWS_DEV_MODE", raising=False)
    monkeypatch.delenv("NOESIS_API_KEY_LEGACY_ACCESS", raising=False)
    monkeypatch.setenv("NOESIS_API_KEY_BACKEND", key_backend)
    monkeypatch.setattr(local_api_keys, "_get_conn", lambda: conn)
    monkeypatch.setattr(manager_module.api_key_manager.store, "table", FakeTable())

    async def issue(permissions):
        if key_backend == "local":
            return local_api_keys.create_api_key("praxis", permissions=permissions)["raw_key"]
        created = await manager_module.api_key_manager.generate_api_key(
            "praxis", "praxis", permissions=permissions)
        return created["api_key"]

    def key(*permissions):
        import asyncio

        return asyncio.run(issue(list(permissions)))

    app = FastAPI()
    app.include_router(kb_routes.router)
    app.include_router(document_routes.router)
    app.add_middleware(APIKeyAuthMiddleware)
    yield TestClient(app), key
    document_routes.use_store_for_testing(None)
    conn.close()


def auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_domain_read_permission_is_enforced(env):
    client, key = env
    support = key("kb:read:support")
    assert client.get("/api/v1/kb/support/search", params={"q": "refund"}, headers=auth(support)).status_code == 200
    denied = client.get("/api/v1/kb/web3/search", params={"q": "defi"}, headers=auth(support))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "unauthorized"
    assert denied.json()["detail"]["required"] == "kb:read:web3"
    wildcard = key("kb:read:*")
    assert client.get("/api/v1/kb/web3/search", params={"q": "defi"}, headers=auth(wildcard)).status_code == 200


def test_document_ingest_requires_permission(env):
    client, key = env
    note = {"document_id": "praxis-1", "source_type": "note", "title": "Verified result", "content": "done"}
    reader = key("kb:read:support")
    response = client.post("/documents/ingest", json=note, headers=auth(reader))
    assert response.status_code == 403 and response.json()["detail"]["required"] == "documents:ingest"
    notes_only = key("kb:read:support", "documents:ingest:note")
    assert client.post("/documents/ingest", json=note, headers=auth(notes_only)).status_code == 201
    news = {**note, "document_id": "praxis-2", "source_type": "news"}
    assert client.post("/documents/ingest", json=news, headers=auth(notes_only)).status_code == 403
    writer = key("documents:ingest")
    assert client.post("/documents/ingest", json=news, headers=auth(writer)).status_code == 201


def test_all_authorized_skips_unpermitted_domains_and_records_them(env):
    client, key = env
    support = key("kb:read:support")
    response = client.post("/api/v1/kb/cross-domain/search", json={"query": "refund", "all_authorized": True},
                           headers=auth(support))
    assert response.status_code == 200
    scope = response.json()["data"]["scope"]
    assert scope["selected_domains"] == ["support"]
    assert {"domain": "web3", "reason": "api_key_not_permitted"} in scope["excluded_domains"]
    explicit = client.post("/api/v1/kb/cross-domain/search", json={"query": "defi", "domains": ["web3"]},
                           headers=auth(support))
    assert explicit.status_code == 403
    body = client.post("/api/v1/kb/temporal", json={"domain": "web3"}, headers=auth(support))
    assert body.status_code == 403



def test_brief_is_limited_to_permitted_domains(env, monkeypatch):
    client, key = env
    seen = []
    monkeypatch.setattr(contract, "kb_brief", lambda domains, since, budget: seen.append(domains) or {"data": {}})
    support = key("kb:read:support")
    assert client.get("/api/v1/kb/brief", headers=auth(support)).status_code == 200
    assert seen == [["support"]]
    assert client.get("/api/v1/kb/brief", params={"domains": "web3"}, headers=auth(support)).status_code == 403
    assert client.get("/api/v1/kb/brief").status_code == 200 and seen[-1] is None  # no key: unchanged


def test_legacy_keys_are_denied_unless_migration_flag_is_set(env, monkeypatch):
    client, key = env
    legacy = key("read:articles")  # predates kb:/documents: permissions
    assert client.get("/api/v1/kb/web3/search", params={"q": "defi"}, headers=auth(legacy)).status_code == 403
    monkeypatch.setenv("NOESIS_API_KEY_LEGACY_ACCESS", "allow")
    assert client.get("/api/v1/kb/web3/search", params={"q": "defi"}, headers=auth(legacy)).status_code == 200


def test_requests_without_api_keys_are_unchanged_and_bad_keys_rejected(env):
    client, _ = env
    assert client.get("/api/v1/kb/web3/search", params={"q": "defi"}).status_code == 200
    assert client.get("/api/v1/kb/web3/search", params={"q": "defi"},
                      headers=auth("nn_" + "0" * 40)).status_code == 401


def test_api_key_principal_reaches_authenticated_routes_without_admin_rights(env):
    client, key = env
    support = key("kb:read:support")
    response = client.get("/api/v1/kb/watches", headers=auth(support))
    assert response.status_code == 200
    assert client.get("/api/v1/kb/watches", params={"domain": "web3"}, headers=auth(support)).status_code == 403

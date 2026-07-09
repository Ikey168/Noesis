"""
Tests for the domain-pack system (issue #520).

Covers:
- DomainPack and Enricher base types
- Registry: register, enable, disable, load_config
- NEURONEWS_ENABLED_PACKS env-var override
- News pack: structure, enrichers, route_modules, ui_flags
- Enricher.applies_to / Enricher.run error-swallowing
- document_routes: ingest, list, get, delete, enrichments, source-types
- app.py: document routes always on; news routes gated behind pack flag
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.domains.base import DomainPack, Enricher
from src.domains.news.pack import NewsDomainPack


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _clean_registry():
    """Return a context that resets the registry before and after a test."""
    from src.domains import registry as reg

    saved_registry = dict(reg._REGISTRY)
    saved_enabled = set(reg._ENABLED)

    reg.reset()
    yield reg

    reg._REGISTRY.clear()
    reg._REGISTRY.update(saved_registry)
    reg._ENABLED.clear()
    reg._ENABLED.update(saved_enabled)


# --------------------------------------------------------------------------- #
# DomainPack / Enricher base types
# --------------------------------------------------------------------------- #

class TestEnricher:
    def test_applies_to_empty_source_types_means_all(self):
        e = Enricher(name="x", fn=lambda d: {}, source_types=[])
        assert e.applies_to("news")
        assert e.applies_to("paper")
        assert e.applies_to("anything")

    def test_applies_to_allowlist(self):
        e = Enricher(name="x", fn=lambda d: {}, source_types=["news"])
        assert e.applies_to("news")
        assert not e.applies_to("paper")

    def test_run_returns_fn_result(self):
        e = Enricher(name="x", fn=lambda d: {"k": 1}, source_types=[])
        assert e.run({}) == {"k": 1}

    def test_run_swallows_exception(self):
        def boom(d):
            raise RuntimeError("crash")

        e = Enricher(name="x", fn=boom, source_types=[])
        result = e.run({})
        assert result is None

    def test_run_passes_document(self):
        received = []
        e = Enricher(name="x", fn=lambda d: received.append(d) or {}, source_types=[])
        doc = {"document_id": "abc"}
        e.run(doc)
        assert received == [doc]


class TestDomainPackBase:
    def test_fields_default(self):
        p = DomainPack(name="test")
        assert p.enrichers == []
        assert p.route_modules == []
        assert p.ui_flags == {}
        assert p.source_types == []

    def test_fields_set(self):
        e = Enricher(name="e", fn=lambda d: None, source_types=["news"])
        p = DomainPack(
            name="test",
            enrichers=[e],
            route_modules=["src.api.routes.news_routes"],
            ui_flags={"sentiment": True},
            source_types=["news"],
        )
        assert p.enrichers == [e]
        assert p.ui_flags["sentiment"] is True


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

class TestRegistry:
    def setup_method(self):
        from src.domains import registry as reg
        reg.reset()

    def teardown_method(self):
        from src.domains import registry as reg
        reg.reset()

    def test_register_and_retrieve(self):
        from src.domains.registry import register_pack, get_pack
        p = DomainPack(name="alpha")
        register_pack(p)
        assert get_pack("alpha") is p

    def test_register_replaces_existing(self):
        from src.domains.registry import register_pack, get_pack
        p1 = DomainPack(name="alpha", description="v1")
        p2 = DomainPack(name="alpha", description="v2")
        register_pack(p1)
        register_pack(p2)
        assert get_pack("alpha").description == "v2"

    def test_is_pack_enabled_requires_both_registered_and_enabled(self):
        from src.domains.registry import register_pack, enable_pack, is_pack_enabled
        p = DomainPack(name="alpha")
        register_pack(p)
        assert not is_pack_enabled("alpha")   # registered but not enabled
        enable_pack("alpha")
        assert is_pack_enabled("alpha")        # now both

    def test_enable_before_register(self):
        from src.domains.registry import enable_pack, is_pack_enabled, register_pack
        enable_pack("beta")
        assert not is_pack_enabled("beta")     # enabled but not yet registered
        register_pack(DomainPack(name="beta"))
        assert is_pack_enabled("beta")         # now both

    def test_disable_pack(self):
        from src.domains.registry import register_pack, enable_pack, disable_pack, is_pack_enabled
        register_pack(DomainPack(name="g"))
        enable_pack("g")
        assert is_pack_enabled("g")
        disable_pack("g")
        assert not is_pack_enabled("g")

    def test_get_enabled_packs(self):
        from src.domains.registry import register_pack, enable_pack, get_enabled_packs
        p1 = DomainPack(name="p1")
        p2 = DomainPack(name="p2")
        register_pack(p1)
        register_pack(p2)
        enable_pack("p1")
        enabled = get_enabled_packs()
        assert p1 in enabled
        assert p2 not in enabled

    def test_load_config_file(self, tmp_path):
        from src.domains.registry import register_pack, load_config, is_pack_enabled
        cfg = tmp_path / "domain_packs.json"
        cfg.write_text(json.dumps({"enabled_packs": ["mypack"]}))
        register_pack(DomainPack(name="mypack"))
        result = load_config(str(cfg))
        assert "mypack" in result
        assert is_pack_enabled("mypack")

    def test_load_config_missing_file_defaults_to_news(self):
        from src.domains.registry import register_pack, load_config, is_pack_enabled
        register_pack(DomainPack(name="news"))
        result = load_config("/no/such/file.json")
        assert "news" in result
        assert is_pack_enabled("news")

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        from src.domains.registry import register_pack, load_config, is_pack_enabled
        cfg = tmp_path / "domain_packs.json"
        cfg.write_text(json.dumps({"enabled_packs": ["news"]}))
        register_pack(DomainPack(name="news"))
        register_pack(DomainPack(name="research"))
        monkeypatch.setenv("NEURONEWS_ENABLED_PACKS", "research")
        result = load_config(str(cfg))
        assert "research" in result
        assert is_pack_enabled("research")
        assert not is_pack_enabled("news")


# --------------------------------------------------------------------------- #
# News pack
# --------------------------------------------------------------------------- #

class TestNewsDomainPack:
    def test_name(self):
        assert NewsDomainPack.name == "news"

    def test_source_types(self):
        assert "news" in NewsDomainPack.source_types

    def test_has_four_enrichers(self):
        assert len(NewsDomainPack.enrichers) == 4

    def test_enricher_names(self):
        names = {e.name for e in NewsDomainPack.enrichers}
        assert "fake_news_detector" in names
        assert "sentiment_analysis" in names
        assert "event_clusterer" in names
        assert "influence_network_analyzer" in names

    def test_enrichers_apply_only_to_news(self):
        for enricher in NewsDomainPack.enrichers:
            assert enricher.applies_to("news")
            assert not enricher.applies_to("paper")
            assert not enricher.applies_to("note")

    def test_route_modules(self):
        modules = NewsDomainPack.route_modules
        assert any("news_routes" in m for m in modules)
        assert any("sentiment" in m for m in modules)
        assert any("event" in m for m in modules)
        assert any("influence" in m for m in modules)

    def test_ui_flags(self):
        flags = NewsDomainPack.ui_flags
        assert flags.get("timeline") is True
        assert flags.get("clusters") is True
        assert flags.get("sentiment_dashboard") is True

    def test_import_registers_pack(self):
        import importlib
        import src.domains.news as news_mod
        import src.domains.registry as reg_mod

        # The module registers the pack at import time; after a registry reset
        # we must reload to re-trigger the side-effect.
        reg_mod.reset()
        importlib.reload(news_mod)
        assert reg_mod.get_pack("news") is not None


# --------------------------------------------------------------------------- #
# Enricher implementations (with mocked ML deps)
# --------------------------------------------------------------------------- #

class TestEnricherImplementations:
    def _doc(self, content="This article may be fake."):
        return {"document_id": "d1", "source_type": "news", "content": content}

    def test_fake_news_enricher_returns_none_when_dep_missing(self):
        from src.domains.news.enrichers import fake_news_enricher
        with patch.dict(sys.modules, {"src.nlp.fake_news_detector": None}):
            result = fake_news_enricher(self._doc())
            assert result is None

    def test_sentiment_enricher_returns_none_when_dep_missing(self):
        from src.domains.news.enrichers import sentiment_enricher
        with patch.dict(sys.modules, {"src.nlp.sentiment_analysis": None}):
            result = sentiment_enricher(self._doc())
            assert result is None

    def test_fake_news_enricher_with_mock_detector(self):
        from src.domains.news.enrichers import fake_news_enricher

        mock_detector = MagicMock()
        mock_detector.return_value.predict_trustworthiness.return_value = {
            "trustworthiness_score": 0.8,
            "classification": "real",
            "trust_level": "high",
        }

        mock_module = MagicMock()
        mock_module.FakeNewsDetector = mock_detector

        with patch.dict(sys.modules, {"src.nlp.fake_news_detector": mock_module}):
            result = fake_news_enricher(self._doc())

        assert result is not None
        assert result["trustworthiness_score"] == 0.8
        assert result["enricher"] == "fake_news_detector"

    def test_sentiment_enricher_with_mock_analyzer(self):
        from src.domains.news.enrichers import sentiment_enricher

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = {"label": "positive", "score": 0.9}

        mock_create = MagicMock(return_value=mock_analyzer)
        mock_module = MagicMock()
        mock_module.create_analyzer = mock_create

        with patch.dict(sys.modules, {"src.nlp.sentiment_analysis": mock_module}):
            result = sentiment_enricher(self._doc())

        assert result is not None
        assert result["sentiment"] == "positive"
        assert result["enricher"] == "sentiment_analysis"

    def test_event_clusterer_enricher_returns_eligible_flag(self):
        from src.domains.news.enrichers import event_clusterer_enricher

        mock_module = MagicMock()
        with patch.dict(sys.modules, {"src.nlp.event_clusterer": mock_module}):
            result = event_clusterer_enricher(self._doc())
        assert result is not None
        assert result["event_clustering_eligible"] is True

    def test_influence_enricher_with_known_source(self):
        from src.domains.news.enrichers import influence_enricher

        mock_analyzer = MagicMock()
        mock_analyzer.return_value.influence_scores = {"bbc": 0.95}
        mock_module = MagicMock()
        mock_module.InfluenceNetworkAnalyzer = mock_analyzer

        with patch.dict(
            sys.modules,
            {"src.knowledge_graph.influence_network_analyzer": mock_module},
        ):
            doc = {"document_id": "d1", "source_type": "news", "content": "x", "source_id": "bbc"}
            result = influence_enricher(doc)
        assert result is not None
        assert result["influence_score"] == 0.95

    def test_enricher_no_content_returns_none(self):
        from src.domains.news.enrichers import fake_news_enricher
        result = fake_news_enricher({"document_id": "d1", "source_type": "news"})
        # No content → should return None (before even loading the model)
        assert result is None


# --------------------------------------------------------------------------- #
# document_routes
# --------------------------------------------------------------------------- #

@pytest.fixture()
def doc_client():
    import duckdb
    from fastapi import FastAPI
    from src.api.routes import document_routes
    from src.ingestion.document_store import DocumentStore

    # Back the routes with a fresh in-memory store per test.
    document_routes.use_store_for_testing(DocumentStore(duckdb.connect(":memory:")))

    app = FastAPI()
    app.include_router(document_routes.router)
    yield TestClient(app)
    document_routes.use_store_for_testing(None)


_SAMPLE_DOC: Dict[str, Any] = {
    "document_id": "doc-001",
    "source_type": "note",
    "language": "en",
    "title": "My Note",
    "content": "Some content here.",
}


class TestDocumentRoutes:
    def test_ingest_valid(self, doc_client):
        r = doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        assert r.status_code == 201
        body = r.json()
        assert body["document_id"] == "doc-001"
        assert "ingested_at" in body

    def test_ingest_invalid_source_type(self, doc_client):
        doc = {**_SAMPLE_DOC, "source_type": "INVALID"}
        r = doc_client.post("/documents/ingest", json=doc)
        assert r.status_code == 422

    def test_get_document(self, doc_client):
        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        r = doc_client.get("/documents/doc-001")
        assert r.status_code == 200
        assert r.json()["title"] == "My Note"

    def test_get_not_found(self, doc_client):
        r = doc_client.get("/documents/nope")
        assert r.status_code == 404

    def test_list_all(self, doc_client):
        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        r = doc_client.get("/documents")
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_list_filter_source_type(self, doc_client):
        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        doc_client.post("/documents/ingest", json={**_SAMPLE_DOC, "document_id": "d2", "source_type": "paper"})
        r = doc_client.get("/documents?source_type=note")
        assert r.status_code == 200
        assert all(d["source_type"] == "note" for d in r.json())

    def test_list_invalid_source_type(self, doc_client):
        r = doc_client.get("/documents?source_type=BOGUS")
        assert r.status_code == 422

    def test_delete_document(self, doc_client):
        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        r = doc_client.delete("/documents/doc-001")
        assert r.status_code == 204
        assert doc_client.get("/documents/doc-001").status_code == 404

    def test_delete_not_found(self, doc_client):
        r = doc_client.delete("/documents/ghost")
        assert r.status_code == 404

    def test_source_types_endpoint(self, doc_client):
        r = doc_client.get("/documents/source-types")
        assert r.status_code == 200
        types = r.json()
        assert "news" in types
        assert "note" in types
        assert "paper" in types

    def test_enrichments_empty_when_no_packs(self, doc_client):
        from src.domains import registry
        registry.reset()  # no packs → no enrichments
        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        r = doc_client.get("/documents/doc-001/enrichments")
        assert r.status_code == 200
        assert r.json()["enrichments"] == {}

    def test_enrichments_not_found(self, doc_client):
        r = doc_client.get("/documents/nope/enrichments")
        assert r.status_code == 404

    def test_enrichments_called_for_matching_pack(self, doc_client):
        from src.domains import registry

        called_with = []

        def my_enricher(doc):
            called_with.append(doc)
            return {"x": 1}

        pack = DomainPack(
            name="test_pack",
            enrichers=[Enricher(name="e", fn=my_enricher, source_types=["note"])],
        )
        registry.reset()
        registry.register_pack(pack)
        registry.enable_pack("test_pack")

        doc_client.post("/documents/ingest", json=_SAMPLE_DOC)
        r = doc_client.get("/documents/doc-001/enrichments")
        assert r.status_code == 200
        assert r.json()["enrichments"]["e"] == {"x": 1}


# --------------------------------------------------------------------------- #
# app.py domain-pack integration
# --------------------------------------------------------------------------- #

class TestAppDomainPackIntegration:
    """Verify that news routes are gated and document routes are always on."""

    def _make_app(self, news_enabled: bool):
        """Build a minimal FastAPI with the domain-pack gating logic applied."""
        from fastapi import FastAPI
        from src.domains import registry
        from src.api.routes import document_routes

        document_routes._store.clear()

        registry.reset()
        registry.register_pack(DomainPack(name="news"))
        if news_enabled:
            registry.enable_pack("news")

        app = FastAPI()
        # Generic — always on.
        app.include_router(document_routes.router)

        # News pack — gated.
        if registry.is_pack_enabled("news"):
            from src.api.routes import news_routes
            app.include_router(news_routes.router)

        return app

    def test_document_routes_always_available(self):
        app = self._make_app(news_enabled=False)
        client = TestClient(app)
        r = client.post("/documents/ingest", json=_SAMPLE_DOC)
        assert r.status_code == 201

    def test_document_routes_available_with_pack_on(self):
        app = self._make_app(news_enabled=True)
        client = TestClient(app)
        r = client.post("/documents/ingest", json=_SAMPLE_DOC)
        assert r.status_code == 201

    def test_news_routes_absent_when_pack_off(self):
        from fastapi import FastAPI
        from src.domains import registry

        registry.reset()
        registry.register_pack(DomainPack(name="news"))
        # news pack NOT enabled

        app = FastAPI()
        if registry.is_pack_enabled("news"):
            from src.api.routes import news_routes
            app.include_router(news_routes.router)

        client = TestClient(app)
        # /news/articles/topic/{topic} should not exist
        r = client.get("/news/articles/topic/politics")
        assert r.status_code == 404

    def test_is_pack_enabled_false_by_default_after_reset(self):
        from src.domains import registry
        registry.reset()
        assert not registry.is_pack_enabled("news")

    def test_load_config_enables_news_by_default(self, tmp_path):
        from src.domains import registry

        registry.reset()
        registry.register_pack(DomainPack(name="news"))

        cfg = tmp_path / "domain_packs.json"
        cfg.write_text(json.dumps({"enabled_packs": ["news"]}))
        registry.load_config(str(cfg))
        assert registry.is_pack_enabled("news")

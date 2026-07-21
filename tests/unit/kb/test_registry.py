"""Unit tests for the knowledge-domain registry and backing abstraction."""

import pytest

from src.kb import (
    CorpusViewBacking,
    DomainConfigError,
    KnowledgeDomainRegistry,
    NamespaceBacking,
    load_registry,
)
from src.kb.registry import CONFIG_PATH_ENV

VALID_CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    description: On-chain ecosystems and governance
    embedding_model: all-MiniLM-L6-v2
    feeds:
      - url: https://example.com/web3.xml
        name: Example Web3
        tags: [web3]
    tags: [web3]
    keywords: [defi, staking]
    embedding_anchors:
      - decentralized finance protocols and on-chain governance
  - name: reference
    backing: namespace
    embedding_model: all-MiniLM-L6-v2
"""


def _write(tmp_path, text):
    path = tmp_path / "domains.yml"
    path.write_text(text)
    return path


class TestLoading:
    def test_valid_config_loads_both_backings(self, tmp_path):
        registry = load_registry(_write(tmp_path, VALID_CONFIG))
        assert registry.names() == ["web3", "reference"]

        web3 = registry.get("web3")
        assert web3.backing == "corpus-view"
        assert web3.feeds[0].url == "https://example.com/web3.xml"
        assert web3.keywords == ["defi", "staking"]
        assert web3.membership_threshold == pytest.approx(0.35)

    def test_namespace_defaults_to_domain_name(self, tmp_path):
        registry = load_registry(_write(tmp_path, VALID_CONFIG))
        assert registry.get("reference").namespace == "reference"

    def test_env_var_overrides_default_path(self, tmp_path, monkeypatch):
        path = _write(tmp_path, VALID_CONFIG)
        monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
        assert load_registry().names() == ["web3", "reference"]

    def test_missing_file_is_a_config_error(self, tmp_path):
        with pytest.raises(DomainConfigError, match="not found"):
            load_registry(tmp_path / "absent.yml")

    def test_unsupported_version_rejected(self, tmp_path):
        with pytest.raises(DomainConfigError, match="version"):
            load_registry(_write(tmp_path, "version: 2\ndomains: []\n"))

    def test_repo_default_config_is_valid(self):
        # The shipped config must always load; starter-domain specifics are
        # covered in test_seeding.py.
        assert len(load_registry().domains()) >= 1


class TestValidation:
    def test_duplicate_names_rejected(self, tmp_path):
        config = """
version: 1
domains:
  - {name: web3, backing: corpus-view, embedding_model: m}
  - {name: web3, backing: corpus-view, embedding_model: m}
"""
        with pytest.raises(DomainConfigError, match="duplicate"):
            load_registry(_write(tmp_path, config))

    def test_unknown_backing_rejected(self, tmp_path):
        config = """
version: 1
domains:
  - {name: web3, backing: warehouse, embedding_model: m}
"""
        with pytest.raises(DomainConfigError, match="backing"):
            load_registry(_write(tmp_path, config))

    def test_missing_embedding_model_rejected(self, tmp_path):
        config = """
version: 1
domains:
  - {name: web3, backing: corpus-view}
"""
        with pytest.raises(DomainConfigError, match="embedding_model"):
            load_registry(_write(tmp_path, config))

    def test_bad_slug_rejected(self, tmp_path):
        config = """
version: 1
domains:
  - {name: Web 3, backing: corpus-view, embedding_model: m}
"""
        with pytest.raises(DomainConfigError, match="slug"):
            load_registry(_write(tmp_path, config))

    def test_namespace_field_invalid_for_corpus_view(self, tmp_path):
        config = """
version: 1
domains:
  - {name: web3, backing: corpus-view, embedding_model: m, namespace: x}
"""
        with pytest.raises(DomainConfigError, match="namespace"):
            load_registry(_write(tmp_path, config))

    def test_threshold_out_of_range_rejected(self, tmp_path):
        config = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: m
    membership_threshold: 1.5
"""
        with pytest.raises(DomainConfigError, match="membership_threshold"):
            load_registry(_write(tmp_path, config))


class TestResolution:
    @pytest.fixture()
    def registry(self, tmp_path) -> KnowledgeDomainRegistry:
        return load_registry(_write(tmp_path, VALID_CONFIG))

    def test_resolve_returns_backing_per_type(self, registry):
        assert isinstance(registry.resolve("web3"), CorpusViewBacking)
        assert isinstance(registry.resolve("reference"), NamespaceBacking)

    def test_unknown_domain_is_a_config_error(self, registry):
        with pytest.raises(DomainConfigError, match="unknown domain"):
            registry.resolve("finance")

    def test_coverage_answers_on_an_empty_warehouse(self, registry):
        import duckdb

        coverage = registry.resolve("web3", conn=duckdb.connect()).coverage()
        assert coverage["domain"] == "web3"
        assert coverage["backing"] == "corpus-view"
        assert coverage["embedding_model"] == "all-MiniLM-L6-v2"
        assert coverage["ready"] is True
        assert coverage["documents"] == 0

        namespace_coverage = registry.resolve("reference").coverage()
        assert namespace_coverage["backing"] == "namespace"
        assert namespace_coverage["namespace"] == "reference"
        assert namespace_coverage["ready"] is False

    def test_unimplemented_reads_fail_loudly(self, registry):
        import duckdb

        backing = registry.resolve("web3", conn=duckdb.connect())
        with pytest.raises(NotImplementedError, match="entities"):
            backing.entities()
        with pytest.raises(NotImplementedError, match="diff"):
            backing.diff(since="2026-07-01")

    def test_embedding_models_map(self, registry):
        assert registry.embedding_models() == {
            "web3": "all-MiniLM-L6-v2",
            "reference": "all-MiniLM-L6-v2",
        }

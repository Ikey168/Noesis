"""Tests for the OS keyring / env-var secret store — Issue #62."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove any Noesis vars that could bleed between tests."""
    for key in list(os.environ):
        if key.startswith(("NOESIS_", "NEURONEWS_")):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_keyring_flag():
    """Reset the module-level _KEYRING_AVAILABLE cache between tests."""
    import src.security.keyring_store as ks
    orig = ks._KEYRING_AVAILABLE
    yield
    ks._KEYRING_AVAILABLE = orig


class TestEnvFallback:
    """Tests that run without a keyring — rely on env-var fallback."""

    def _disable_keyring(self):
        import src.security.keyring_store as ks
        ks._KEYRING_AVAILABLE = False

    def test_get_secret_returns_none_when_not_set(self):
        self._disable_keyring()
        from src.security.keyring_store import get_secret
        assert get_secret("neuronews", "DOES_NOT_EXIST") is None

    def test_get_secret_reads_env_var(self, monkeypatch):
        self._disable_keyring()
        monkeypatch.setenv("NEURONEWS_MY_KEY", "hello-world")
        from src.security.keyring_store import get_secret
        assert get_secret("neuronews", "MY_KEY") == "hello-world"

    def test_noesis_secret_prefers_canonical_env_and_warns_on_legacy(self, monkeypatch):
        self._disable_keyring()
        from src.security.keyring_store import get_secret

        monkeypatch.setenv("NEURONEWS_BACKUP_KEY", "legacy")
        with pytest.warns(DeprecationWarning, match="NOESIS_BACKUP_KEY"):
            assert get_secret("noesis", "BACKUP_KEY") == "legacy"
        monkeypatch.setenv("NOESIS_BACKUP_KEY", "canonical")
        assert get_secret("noesis", "BACKUP_KEY") == "canonical"

    def test_set_secret_writes_env_var_when_no_keyring(self, monkeypatch):
        self._disable_keyring()
        from src.security.keyring_store import get_secret, set_secret
        result = set_secret("neuronews", "SESSION_TOKEN", "tok123")
        assert result is False  # False means stored in env, not keyring
        assert get_secret("neuronews", "SESSION_TOKEN") == "tok123"

    def test_env_key_derives_correct_name(self):
        from src.security.keyring_store import _env_key
        assert _env_key("neuronews", "DB_KEY") == "NEURONEWS_DB_KEY"
        assert _env_key("my-service", "backup-key") == "MY_SERVICE_BACKUP_KEY"

    def test_delete_secret_noop_when_no_keyring(self):
        self._disable_keyring()
        from src.security.keyring_store import delete_secret
        # Should not raise
        result = delete_secret("neuronews", "NO_SUCH_KEY")
        assert result is False

    def test_get_secret_case_insensitive_key(self, monkeypatch):
        self._disable_keyring()
        monkeypatch.setenv("NEURONEWS_DB_KEY", "secret")
        from src.security.keyring_store import get_secret
        # key passed lowercase — env var lookup upcases it
        assert get_secret("neuronews", "db_key") == "secret"

    def test_multiple_services_isolated(self, monkeypatch):
        self._disable_keyring()
        monkeypatch.setenv("NEURONEWS_API_KEY", "n-val")
        monkeypatch.setenv("OTHERSERVICE_API_KEY", "o-val")
        from src.security.keyring_store import get_secret
        assert get_secret("neuronews", "API_KEY") == "n-val"
        assert get_secret("otherservice", "API_KEY") == "o-val"

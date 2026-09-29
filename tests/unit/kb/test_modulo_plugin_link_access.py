"""Server-side access rechecks for exact, metadata-only Modulo plugin links."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.modulo_plugin_link_access import (
    CONTRACT,
    ModuloPluginLinkAccessStore,
)

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "namespace:research:read", "namespace:research:write",
}
LINK = {
    "workspace_id": "workspace:personal",
    "account_id": "account:alice",
    "plugin_id": "notes-editor",
    "collection": "notes",
    "record_id": "note:7",
    "authoritative_version": 4,
    "representation": "linked_projection",
    "authority": "modulo",
}
IDENTITY = {key: LINK[key] for key in (
    "workspace_id", "account_id", "plugin_id", "collection", "record_id",
)}


class _Reader:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.identities = []

    def read_exact_record(self, identity):
        self.identities.append(dict(identity))
        if self.error:
            raise self.error
        return self.response


class _Provider:
    def __init__(self, reader):
        self.reader = reader
        self.callers = []
        self.server_secret = "server-only-secret"

    def for_caller(self, principal_id):
        self.callers.append(principal_id)
        return self.reader


def _session(conn):
    return IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "plugin-link-session",
        intent="Review this linked note", plugin_links=[LINK],
        principal_id="alice", scopes=SCOPES,
    )


@pytest.mark.parametrize(
    ("provider_status", "current_version", "expected_status", "expected_current"),
    [
        ("accessible", 4, "current", 4),
        ("accessible", 5, "version_changed", 5),
        ("revoked", None, "revoked", None),
        ("missing", None, "missing", None),
        ("unavailable", None, "unavailable", None),
    ],
)
def test_modulo_link_recheck_distinguishes_access_and_version_states(
    provider_status, current_version, expected_status, expected_current,
):
    conn = duckdb.connect(":memory:")
    session = _session(conn)
    reader = _Reader({
        "identity": IDENTITY,
        "status": provider_status,
        "authoritative_version": current_version,
    })
    provider = _Provider(reader)

    result = ModuloPluginLinkAccessStore(
        conn, provider=provider, now=lambda: 9000,
    ).recheck(
        "research", session["session_id"], 0, 1,
        principal_id="alice", scopes=SCOPES,
    )

    assert result == {
        "contract": CONTRACT,
        "identity": IDENTITY,
        "expected_authoritative_version": 4,
        "current_authoritative_version": expected_current,
        "status": expected_status,
        "access_status": "accessible" if provider_status == "accessible" else provider_status,
        "checked_at_ms": 9000,
        "read_only": True,
        "content_included": False,
    }
    assert provider.callers == ["alice"]
    assert reader.identities == [IDENTITY]
    assert provider.server_secret not in repr(result)
    persisted = conn.execute(
        "SELECT content_json FROM intake_sessions WHERE session_id=?",
        [session["session_id"]],
    ).fetchone()[0]
    assert "server-only-secret" not in persisted
    conn.close()


def test_modulo_link_recheck_is_unavailable_without_provider_and_rejects_content():
    conn = duckdb.connect(":memory:")
    session = _session(conn)
    store = ModuloPluginLinkAccessStore(conn, provider=None, now=lambda: 2000)
    missing_connector = store.recheck(
        "research", session["session_id"], 0, 1,
        principal_id="alice", scopes=SCOPES,
    )
    assert missing_connector["status"] == "unavailable"
    assert missing_connector["current_authoritative_version"] is None
    assert missing_connector["content_included"] is False

    reader = _Reader({
        "identity": IDENTITY,
        "status": "accessible",
        "authoritative_version": 4,
        "content": {"private": "must not cross the adapter"},
    })
    malformed = ModuloPluginLinkAccessStore(
        conn, provider=_Provider(reader), now=lambda: 2000,
    ).recheck(
        "research", session["session_id"], 0, 1,
        principal_id="alice", scopes=SCOPES,
    )
    assert malformed["status"] == "unavailable"
    assert "private" not in repr(malformed)
    conn.close()


def test_modulo_link_recheck_rejects_identity_mismatch_and_provider_failure():
    conn = duckdb.connect(":memory:")
    session = _session(conn)
    wrong_identity = {**IDENTITY, "account_id": "account:someone-else"}
    for reader in (
        _Reader({"identity": wrong_identity, "status": "accessible",
                 "authoritative_version": 4}),
        _Reader(error=RuntimeError("secret-bearing transport exception")),
    ):
        result = ModuloPluginLinkAccessStore(
            conn, provider=_Provider(reader), now=lambda: 3000,
        ).recheck(
            "research", session["session_id"], 0, 1,
            principal_id="alice", scopes=SCOPES,
        )
        assert result["status"] == "unavailable"
        assert "secret-bearing" not in repr(result)
    conn.close()


def test_modulo_link_recheck_requires_current_session_access_and_revision():
    conn = duckdb.connect(":memory:")
    session = _session(conn)
    provider = _Provider(_Reader({
        "identity": IDENTITY, "status": "accessible", "authoritative_version": 4,
    }))
    store = ModuloPluginLinkAccessStore(conn, provider=provider)

    with pytest.raises(IntakeError) as wrong_owner:
        store.recheck("research", session["session_id"], 0, 1,
                      principal_id="bob", scopes=SCOPES)
    assert wrong_owner.value.code == "unauthorized"
    with pytest.raises(IntakeError) as stale:
        store.recheck("research", session["session_id"], 0, 2,
                      principal_id="alice", scopes=SCOPES)
    assert stale.value.code == "revision_conflict"
    with pytest.raises(IntakeError) as absent:
        store.recheck("research", session["session_id"], 1, 1,
                      principal_id="alice", scopes=SCOPES)
    assert absent.value.code == "plugin_link_not_found"
    assert provider.callers == []
    conn.close()

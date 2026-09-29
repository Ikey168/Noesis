"""Read-only, fail-closed rechecks for persisted Modulo plugin links.

The host injects a per-caller provider. Credentials stay inside that provider;
this module receives only the authenticated Noesis principal and an exact
plugin-record identity, and it stores neither credentials nor remote content.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from src.kb.intake_modes import IntakeError, IntakeStore

CONTRACT = "noesis-modulo-plugin-link-check-v1"
_IDENTITY_FIELDS = (
    "workspace_id", "account_id", "plugin_id", "collection", "record_id",
)
_PROVIDER_STATUSES = {"accessible", "revoked", "missing", "unavailable"}


class ModuloPluginLinkReader(Protocol):
    """Read one exact Modulo record using credentials held by the server."""

    def read_exact_record(self, identity: dict[str, str]) -> Mapping[str, Any]: ...


class PerCallerModuloPluginLinkProvider(Protocol):
    """Resolve a read client for one authenticated Noesis caller."""

    def for_caller(self, principal_id: str) -> ModuloPluginLinkReader | None: ...


class ModuloPluginLinkAccessStore:
    def __init__(
        self,
        conn: Any,
        *,
        provider: PerCallerModuloPluginLinkProvider | None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.provider = provider
        self.now = now or (lambda: int(time.time() * 1000))

    @staticmethod
    def _identity(link: Mapping[str, Any]) -> dict[str, str]:
        return {field: link[field] for field in _IDENTITY_FIELDS}

    @staticmethod
    def _result(
        identity: dict[str, str], expected_version: int, status: str,
        *, access_status: str | None = None,
        current_version: int | None = None, checked_at_ms: int,
    ) -> dict[str, Any]:
        if access_status is None:
            access_status = status if status in {"revoked", "missing", "unavailable"} else "accessible"
        return {
            "contract": CONTRACT,
            "identity": identity,
            "expected_authoritative_version": expected_version,
            "current_authoritative_version": current_version,
            "status": status,
            "access_status": access_status,
            "checked_at_ms": checked_at_ms,
            "read_only": True,
            "content_included": False,
        }

    def recheck(
        self,
        namespace: str,
        session_id: str,
        link_index: int,
        expected_session_revision: int,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        if type(expected_session_revision) is not int or expected_session_revision < 1:
            raise IntakeError("invalid_revision", "expected session revision must be positive")
        if type(link_index) is not int or not 0 <= link_index < 20:
            raise IntakeError("invalid_plugin_link", "plugin link index must be between 0 and 19")

        ledger = IntakeStore(self.conn, initialize=False, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        if state["revision"] != expected_session_revision:
            raise IntakeError("revision_conflict", "session changed; inspect its current plugin links")
        links = state.get("plugin_links", [])
        if link_index >= len(links):
            raise IntakeError("plugin_link_not_found", "plugin link is not in this session revision")
        link = links[link_index]
        identity = self._identity(link)
        expected_version = link["authoritative_version"]
        checked_at_ms = self.now()

        # No live provider means no positive access claim. In particular, stored
        # links and caller-supplied metadata are never treated as current proof.
        if self.provider is None:
            return self._result(
                identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
            )
        try:
            reader = self.provider.for_caller(principal_id)
            if reader is None:
                return self._result(
                    identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
                )
            response = reader.read_exact_record(dict(identity))
        except Exception:  # noqa: BLE001 - provider failures must not expose credential details
            return self._result(
                identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
            )

        # Require a metadata-only response for the exact requested identity.
        # Extra fields could contain content or credentials and fail closed.
        if (
            not isinstance(response, Mapping)
            or set(response) != {"identity", "status", "authoritative_version"}
            or response.get("identity") != identity
            or response.get("status") not in _PROVIDER_STATUSES
        ):
            return self._result(
                identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
            )

        provider_status = response["status"]
        current_version = response["authoritative_version"]
        if provider_status == "accessible":
            if type(current_version) is not int or current_version < 1:
                return self._result(
                    identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
                )
            return self._result(
                identity,
                expected_version,
                "current" if current_version == expected_version else "version_changed",
                access_status="accessible",
                current_version=current_version,
                checked_at_ms=checked_at_ms,
            )
        if current_version is not None:
            return self._result(
                identity, expected_version, "unavailable", checked_at_ms=checked_at_ms,
            )
        return self._result(
            identity, expected_version, provider_status, checked_at_ms=checked_at_ms,
        )

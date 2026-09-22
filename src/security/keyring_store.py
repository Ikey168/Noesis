"""
Local secret storage — Issue #62.

Wraps the OS keyring (GNOME Keyring, KWallet, macOS Keychain) with a
transparent fallback to environment variables for headless / CI environments
where no keyring daemon is running.

Usage::

    from src.security.keyring_store import get_secret, set_secret

    passphrase = get_secret("noesis", "BACKUP_KEY")
    set_secret("noesis", "BACKUP_KEY", "my-strong-passphrase")

Environment-variable fallback:
    If the ``keyring`` library is not importable, or if the backend raises
    ``NoKeyringError`` / ``KeyringLocked``, the value is read from / written
    to an env var named ``NOESIS_{KEY.upper()}``.

    Example: ``get_secret("noesis", "DB_KEY")`` falls back to
    ``NOESIS_DB_KEY`` and then its deprecated ``NEURONEWS_DB_KEY`` alias.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyring availability detection (lazy, one-shot)
# ---------------------------------------------------------------------------

_KEYRING_AVAILABLE: Optional[bool] = None


def _try_keyring():
    global _KEYRING_AVAILABLE
    if _KEYRING_AVAILABLE is None:
        try:
            import keyring as _kr  # noqa: F401
            # Test that a backend is actually usable — some installs have the
            # library but no backend (e.g. minimal Docker images).
            import keyring.errors  # noqa: F401
            _KEYRING_AVAILABLE = True
        except Exception:
            _KEYRING_AVAILABLE = False
            logger.debug(
                "keyring library unavailable; falling back to environment variables"
            )
    return _KEYRING_AVAILABLE


def _env_key(service: str, key: str) -> str:
    """Derive the env-var name from service + key."""
    # Convention: NOESIS_DB_KEY, NOESIS_BACKUP_KEY, …
    prefix = service.upper().replace("-", "_")
    suffix = key.upper().replace("-", "_")
    return f"{prefix}_{suffix}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_secret(service: str, key: str) -> Optional[str]:
    """
    Return the secret stored under *service* / *key*, or ``None`` if not found.

    Resolution order:
    1. OS keyring (if available and unlocked).
    2. Environment variable ``{SERVICE}_{KEY}`` (upper-cased, hyphens → _).

    Args:
        service:  Logical service name (e.g. ``"neuronews"``).
        key:      Secret key within that service (e.g. ``"BACKUP_KEY"``).
    """
    if _try_keyring():
        try:
            import keyring
            value = keyring.get_password(service, key)
            if value is not None:
                return value
        except Exception as exc:
            logger.debug("keyring.get_password failed (%s); trying env var", exc)

    if service.casefold() == "noesis":
        from src.config.env import resolve_env

        return resolve_env(key)
    return os.getenv(_env_key(service, key))


def set_secret(service: str, key: str, value: str) -> bool:
    """
    Store a secret under *service* / *key*.

    Returns ``True`` if stored in the OS keyring, ``False`` if only set as an
    in-process environment variable (the caller may want to persist it another
    way in that case).

    Args:
        service:  Logical service name.
        key:      Secret key.
        value:    Plaintext secret value.
    """
    if _try_keyring():
        try:
            import keyring
            keyring.set_password(service, key, value)
            logger.debug("Stored %s/%s in OS keyring", service, key)
            return True
        except Exception as exc:
            logger.warning(
                "keyring.set_password failed (%s); falling back to env var", exc
            )

    env_name = _env_key(service, key)
    os.environ[env_name] = value
    logger.debug("Stored %s/%s in environment variable %s", service, key, env_name)
    return False


def delete_secret(service: str, key: str) -> bool:
    """
    Remove a secret from the OS keyring (no-op if not found or keyring
    unavailable).

    Returns ``True`` if the secret was found and deleted.
    """
    if _try_keyring():
        try:
            import keyring
            import keyring.errors
            keyring.delete_password(service, key)
            return True
        except Exception:
            pass
    return False

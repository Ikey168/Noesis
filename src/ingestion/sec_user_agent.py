"""The one SEC fair-access User-Agent setting every SEC reader resolves (#2768).

SEC fair-access rules require automated clients to declare who they are: a
descriptive ``User-Agent`` naming the operator and a contact address. Noesis
reads several SEC hosts (``data.sec.gov`` XBRL and submissions,
``www.sec.gov`` archives, litigation and administrative-proceeding pages), so
the setting is named for the publisher, not one of its systems:
:data:`SEC_USER_AGENT_ENV` (``NOESIS_SEC_USER_AGENT``).

Earlier names keep working as deprecated aliases, each logging a warning that
names the canonical variable. When more than one is set and their values
differ, resolution is refused rather than picking one, so a run never sends a
User-Agent the operator did not mean. No value is ever logged.

Every SEC reader calls :func:`resolve_sec_user_agent` (empty string when
nothing is configured, for readers that skip with a warning) or
:func:`require_sec_user_agent` (raises when nothing is configured, for
readers that refuse).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

SEC_USER_AGENT_ENV = "NOESIS_SEC_USER_AGENT"
# Deprecated names, in the order they are reported. NOESIS_EDGAR_USER_AGENT was
# read by the EDGAR market connectors and scripts. NOESIS_SEC_CONTACT is not an
# alias: the corporate-ownership pack still reads it as a bare contact address
# that it wraps in its own User-Agent, so it may legitimately differ from this
# setting.
DEPRECATED_SEC_USER_AGENT_ENVS: tuple[str, ...] = ("NOESIS_EDGAR_USER_AGENT",)


class SecUserAgentError(ValueError):
    """The SEC User-Agent is missing or configured inconsistently."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_sec_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured SEC User-Agent, or ``""`` when none is set.

    Raises :class:`SecUserAgentError` (code ``sec_user_agent_conflict``) when
    two or more of the canonical and deprecated variables are set to different
    values.
    """

    env = os.environ if environ is None else environ
    found = {
        name: str(env.get(name) or "").strip()
        for name in (SEC_USER_AGENT_ENV, *DEPRECATED_SEC_USER_AGENT_ENVS)
    }
    found = {name: value for name, value in found.items() if value}
    if len(set(found.values())) > 1:
        raise SecUserAgentError(
            "sec_user_agent_conflict",
            f"conflicting SEC User-Agent settings: {', '.join(found)} are set to different values; "
            f"set only {SEC_USER_AGENT_ENV}",
        )
    for alias in DEPRECATED_SEC_USER_AGENT_ENVS:
        if alias in found:
            logger.warning("%s is deprecated; set %s instead", alias, SEC_USER_AGENT_ENV)
    return next(iter(found.values()), "")


def require_sec_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured SEC User-Agent, refusing when none is set."""

    agent = resolve_sec_user_agent(environ)
    if not agent:
        raise SecUserAgentError("sec_user_agent_missing", missing_sec_user_agent_message())
    return agent


def missing_sec_user_agent_message() -> str:
    return f"set {SEC_USER_AGENT_ENV} to a descriptive SEC User-Agent (operator name and contact)"

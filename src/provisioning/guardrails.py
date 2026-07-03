"""
Provisioning guardrails (R8 #608): the non-negotiable rules for a RW agent
surface. Each is enforced here and has a failing-path test.

    Blast radius     max KGs, max sources per KG, an ingest rate cap.
    Human-in-loop    deploy and teardown are approval-gated by default
                     (``approve`` / ``confirm`` must be passed); status and
                     dry-run previews are free.
    Idempotency      enforced in the store (upserts keyed by name); the
                     provisioner never creates a second row for the same name.

A breach raises :class:`GuardrailError` with a machine-readable ``code`` so the
MCP tool can return a clean ``{"error": ..., "code": ...}`` rather than a
stack trace. Limits are read from the environment once per :class:`Quotas`
instance so tests can tighten them.

Stdlib-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class GuardrailError(Exception):
    """A provisioning guardrail refused the operation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Quotas:
    """The provisioning blast-radius limits.

    ``ingest_min_interval_s`` of 0 disables the rate cap (the default, so
    normal use and idempotent re-runs are never throttled); a positive value
    rejects a second ingest of the same KG within that many seconds.
    """

    max_kgs: int = 8
    max_sources_per_kg: int = 20
    ingest_min_interval_s: int = 0
    # P2 (#643): blast radius for provisioned databases and pipelines.
    max_databases: int = 4
    max_pipelines_per_kg: int = 8

    @classmethod
    def from_env(cls) -> "Quotas":
        return cls(
            max_kgs=_env_int("NOESIS_PROV_MAX_KGS", 8),
            max_sources_per_kg=_env_int("NOESIS_PROV_MAX_SOURCES", 20),
            ingest_min_interval_s=_env_int("NOESIS_PROV_INGEST_MIN_INTERVAL_S", 0),
            max_databases=_env_int("NOESIS_PROV_MAX_DATABASES", 4),
            max_pipelines_per_kg=_env_int("NOESIS_PROV_MAX_PIPELINES", 8),
        )

    @classmethod
    def for_tenant(cls, tenant: str) -> "Quotas":
        """Per-tenant quotas (M4.2): each limit can be overridden for one tenant
        via ``NOESIS_PROV_MAX_KGS_<TENANT>`` (etc.), else the global default. The
        quotas are counted per tenant (``store.count_deployed`` is tenant-scoped),
        so one tenant hitting its budget never blocks another."""
        base = cls.from_env()
        if not tenant or tenant == "default":
            return base
        suffix = "_" + "".join(c if c.isalnum() else "_" for c in tenant).upper()
        return cls(
            max_kgs=_env_int("NOESIS_PROV_MAX_KGS" + suffix, base.max_kgs),
            max_sources_per_kg=_env_int("NOESIS_PROV_MAX_SOURCES" + suffix, base.max_sources_per_kg),
            ingest_min_interval_s=_env_int(
                "NOESIS_PROV_INGEST_MIN_INTERVAL_S" + suffix, base.ingest_min_interval_s
            ),
            max_databases=_env_int("NOESIS_PROV_MAX_DATABASES" + suffix, base.max_databases),
            max_pipelines_per_kg=_env_int(
                "NOESIS_PROV_MAX_PIPELINES" + suffix, base.max_pipelines_per_kg
            ),
        )

    def check_database_quota(self, deployed_db_count: int, is_new_db: bool) -> None:
        """Refuse a *new* attached-database deploy past the max-databases quota."""
        if is_new_db and deployed_db_count >= self.max_databases:
            raise GuardrailError(
                "quota_max_databases",
                f"database quota reached: {deployed_db_count}/{self.max_databases} "
                f"provisioned. Tear one down or raise NOESIS_PROV_MAX_DATABASES.",
            )

    def check_pipelines_quota(self, current: int, adding: int) -> None:
        """Refuse a pipeline attach past the max-pipelines-per-KG quota."""
        if current + adding > self.max_pipelines_per_kg:
            raise GuardrailError(
                "quota_max_pipelines",
                f"pipeline quota exceeded: {current} bound + {adding} requested "
                f"> {self.max_pipelines_per_kg} per KG.",
            )

    def check_deploy_quota(self, deployed_count: int, is_new: bool) -> None:
        """Refuse a *new* deploy that would exceed the max-KGs quota. Re-deploy
        of an existing name (``is_new`` False) converges and never counts."""
        if is_new and deployed_count >= self.max_kgs:
            raise GuardrailError(
                "quota_max_kgs",
                f"KG quota reached: {deployed_count}/{self.max_kgs} deployed. "
                f"Tear one down or raise NOESIS_PROV_MAX_KGS.",
            )

    def check_sources_quota(self, current: int, adding: int) -> None:
        """Refuse an attach that would push a KG past the max-sources quota."""
        if current + adding > self.max_sources_per_kg:
            raise GuardrailError(
                "quota_max_sources",
                f"source quota exceeded: {current} bound + {adding} requested "
                f"> {self.max_sources_per_kg} per KG.",
            )

    def check_ingest_rate(self, seconds_since_last: float | None) -> None:
        """Refuse an ingest that arrives inside the rate-cap window."""
        if (
            self.ingest_min_interval_s > 0
            and seconds_since_last is not None
            and seconds_since_last < self.ingest_min_interval_s
        ):
            raise GuardrailError(
                "rate_capped",
                f"ingest rate-capped: last ingest {seconds_since_last:.0f}s ago, "
                f"minimum interval {self.ingest_min_interval_s}s.",
            )


def require_approval(approve: bool, action: str) -> None:
    """Gate deploy: refuse unless the caller explicitly approved."""
    if not approve:
        raise GuardrailError(
            "approval_required",
            f"{action} is approval-gated: re-run with approve=true to execute "
            f"(dry-run previews are free).",
        )


def require_confirm(confirm: bool, action: str) -> None:
    """Gate teardown: refuse unless the caller explicitly confirmed."""
    if not confirm:
        raise GuardrailError(
            "confirm_required",
            f"{action} archives the KG and requires confirm=true "
            f"(it never silently deletes).",
        )

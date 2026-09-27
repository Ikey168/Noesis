"""Read-only view of environmental facility evidence attached to policy-monitor obligations.

The Climate and Environment bundle attaches facility releases (with the
vintage used) to obligations the monitor already records as versioned public
assertions. This view lists them per policy subject. The monitor itself
evaluates nothing new: there is no threshold check and no compliance
determination here.
"""

from __future__ import annotations

from typing import Any


def obligation_evidence(conn: Any, subject_id: str, *, namespace: str, scopes: set[str]) -> dict[str, Any]:
    from src.kb.environment_identity import EnvironmentIdentity

    identity = EnvironmentIdentity(conn)
    return {
        "subject_id": subject_id,
        "obligations": identity.obligations(subject_id),
        "evidence": identity.evidence_for_subject(subject_id, namespace=namespace, scopes=scopes),
        "determination": None,
        "note": "evidence shown beside obligations with sources; the monitor decides nothing on its own",
    }

"""Conservative, read-only impact preview for an existing Maintenance finding."""

from __future__ import annotations

import json
from typing import Any

from src.kb.intake_maintenance import IntakeMaintenanceStore, _has_table
from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _text
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_practice import IntakePracticeStore

CONTRACT = "noesis-intake-maintenance-impact-v1"
_ACTIONS = {"refresh", "archive", "delete"}


class IntakeMaintenanceImpact:
    def __init__(self, conn: Any, *, now=None):
        self.conn = conn
        self.now = now

    def preview(
        self, namespace: str, finding_id: str, action: str, *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        if action not in _ACTIONS:
            raise IntakeError("invalid_action", "preview refresh, archive, or delete")
        finding_id = _text(finding_id, "finding_id", limit=80)
        findings = IntakeMaintenanceStore(self.conn, now=self.now).scan(
            namespace, principal_id=principal_id, scopes=scopes,
        )["findings"]
        finding = next((item for item in findings if item["id"] == finding_id), None)
        if finding is None:
            raise IntakeError("finding_not_found", "finding is not visible in the current review queue")
        target = finding["target"]
        # Worker findings identify a job, but pruning/refresh impact belongs to
        # the source pack processed by that job. Keep both identities visible:
        # the finding target remains the job while this root drives dependency
        # and retention lookup.
        metadata = finding.get("metadata", {})
        impact_root = target
        if target.get("kind") == "maintenance_job" and isinstance(metadata, dict):
            pack_id = metadata.get("pack_id")
            if isinstance(pack_id, str) and pack_id:
                impact_root = {"kind": "source_pack", "id": pack_id,
                               "namespace": namespace}
        affected = []
        limits = ["Preview is read-only and does not authorize a mutation",
                  "Modulo plugin access and remote dependents are not checked"]

        def matches(ref: Any) -> bool:
            if not isinstance(ref, dict) or ref.get("id") != impact_root["id"]:
                return False
            if ref.get("namespace", namespace) != namespace:
                return False
            ref_kind = ref.get("kind")
            return not ref_kind or ref_kind == impact_root.get("kind")

        def add(kind: str, identity: str, revision: int, relation: str) -> None:
            item = {"kind": kind, "id": identity, "revision": revision,
                    "relation": relation}
            if item not in affected:
                affected.append(item)

        if _has_table(self.conn, "intake_sessions"):
            rows = self.conn.execute(
                "SELECT session_id,revision,content_json FROM intake_sessions "
                "WHERE namespace=? AND owner=? ORDER BY session_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            if len(rows) > 500:
                limits.append("Intake-session scan was limited to 500 records")
            for session_id, revision, raw in rows[:500]:
                state = json.loads(raw)
                try:
                    IntakeStore._authorize_full_read(state, principal_id, scopes)
                except IntakeError:
                    limits.append("Some intake sessions were inaccessible with current scopes")
                    continue
                if any(matches(ref) for ref in state.get("references", [])):
                    add("intake_session", session_id, int(revision), "references_target")
        if _has_table(self.conn, "intake_practice_packs"):
            rows = self.conn.execute(
                "SELECT pack_id,revision,content_json FROM intake_practice_packs "
                "WHERE namespace=? AND owner=? ORDER BY pack_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            if len(rows) > 500:
                limits.append("Practice-pack scan was limited to 500 records")
            for pack_id, revision, raw in rows[:500]:
                pack = json.loads(raw)
                try:
                    IntakePracticeStore._access(pack, principal_id, scopes)
                except IntakeError:
                    limits.append("Some practice packs were inaccessible with current scopes")
                    continue
                if any(matches(ref) for card in pack.get("cards", [])
                       for ref in card.get("references", [])):
                    add("practice_pack", pack_id, int(revision), "card_source")
        if _has_table(self.conn, "intake_playbooks"):
            rows = self.conn.execute(
                "SELECT playbook_id,revision,content_json FROM intake_playbooks "
                "WHERE namespace=? AND owner=? ORDER BY playbook_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            if len(rows) > 500:
                limits.append("Playbook scan was limited to 500 records")
            for playbook_id, revision, raw in rows[:500]:
                playbook = json.loads(raw)
                try:
                    IntakePlaybookStore._authorize(playbook, principal_id, scopes)
                except IntakeError:
                    limits.append("Some playbooks were inaccessible with current scopes")
                    continue
                if any(matches(ref) for ref in playbook.get("references", [])):
                    add("playbook", playbook_id, int(revision), "source_reference")

        retention = {"status": "not_registered"}
        if "operator" in scopes or "knowledge:retention:read" in scopes:
            if _has_table(self.conn, "retention_objects"):
                row = self.conn.execute(
                    "SELECT status,dependencies_json,pins_json,generation,object_class "
                    "FROM retention_objects WHERE namespace=? AND object_id=?",
                    [namespace, impact_root["id"]],
                ).fetchone()
                if row:
                    holds = []
                    if _has_table(self.conn, "retention_holds"):
                        holds = [entry[0] for entry in self.conn.execute(
                            "SELECT hold_id FROM retention_holds WHERE namespace=? AND object_id=? "
                            "AND status='active' ORDER BY hold_id",
                            [namespace, impact_root["id"]],
                        ).fetchall()]
                    retention = {"status": row[0], "dependencies": json.loads(row[1]),
                                 "pins": json.loads(row[2]), "active_hold_ids": holds,
                                 "generation": int(row[3]), "object_class": row[4]}

                # Retention dependencies are stored as object IDs on the
                # dependent object. Bound this scan and report only identities,
                # revisions, and relations; never copy retained content here.
                rows = self.conn.execute(
                    "SELECT object_id,generation,dependencies_json,status "
                    "FROM retention_objects WHERE namespace=? ORDER BY object_id LIMIT 501",
                    [namespace],
                ).fetchall()
                if len(rows) > 500:
                    limits.append("Retention dependency scan was limited to 500 records")
                for object_id, generation, dependencies_raw, _status in rows[:500]:
                    dependencies = json.loads(dependencies_raw)
                    if impact_root["id"] in dependencies:
                        add("retention_object", object_id, max(1, int(generation)),
                            "retention_dependency")
        else:
            retention = {"status": "scope_required"}
            limits.append("Retention state needs knowledge:retention:read")

        affected.sort(key=lambda item: (item["kind"], item["id"]))
        preview = {"contract": CONTRACT, "namespace": namespace, "finding_id": finding_id,
                   "action": action, "target": target, "impact_root": impact_root,
                   "affected": affected,
                   "retention": retention, "remote_action_required": action in {"archive", "delete"},
                   "deletion_authorized": False, "limitations": list(dict.fromkeys(limits))}
        # Pass this digest to execute_intake_maintenance_action; any change in
        # dependents, holds or revisions invalidates the reviewed preview.
        preview["preview_hash"] = _hash(preview)
        return preview

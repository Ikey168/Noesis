"""Weather records and Climate & Environment records, linked by citation only (WX10, #2173).

Neither pack copies the other's records:

* :meth:`WeatherClimateLinks.environment_references` names, for an observation
  report, the environment ``station`` record and, where the same DWD parameter
  exists as a Climate ``observation_series`` (hourly ``TT_TU``/``RF_TU``), that
  series' record id. It reads through the environment owner's API
  (``EnvironmentStore.find``) and never re-creates a series.
* A warning or an observation extreme can be **attached as cited evidence** to
  an environment place dossier (``environment_dossiers``) or to an event record
  (``event_model_current``) when a user links them. The link is a proposal until
  another principal accepts it, and it can be reverted. It cites the Weather
  revision (issuer, locator, reference time) and never writes into the dossier or
  the event.

The only environment record type any Weather module writes is ``station``,
through the environment owner (:meth:`WeatherStore.register_stations`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from src.kb.weather_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
)
from src.kb.weather_store import ENVIRONMENT_NAMESPACE, WeatherError, _table, authorize

CONTRACT = "noesis-weather-evidence-link-v1"
TARGET_KINDS = ("environment-dossier", "event-record")
# DWD CDC parameters the Climate pack stores as series ("<station>:<column>", provider dwd).
CLIMATE_SERIES_COLUMNS = {"TT_TU", "RF_TU"}
_DDL = """
CREATE TABLE IF NOT EXISTS weather_evidence_links(
 namespace TEXT NOT NULL, link_id TEXT NOT NULL, target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL,
 target_id TEXT NOT NULL, revision_id TEXT NOT NULL, citation_json TEXT NOT NULL, note TEXT NOT NULL,
 proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(namespace, link_id));
"""


class WeatherClimateLinks:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Any = None,
        environment_namespace: str = ENVIRONMENT_NAMESPACE,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.environment_namespace = environment_namespace
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ references

    def environment_references(
        self, station: str, parameters: Iterable[str]
    ) -> dict[str, Any]:
        """Environment record ids a report cites: its station and any Climate series of the same DWD parameter."""

        from src.kb.environment_store import EnvironmentStore

        env = EnvironmentStore(self.conn, initialize=False)
        if not _table(self.conn, "environment_records"):
            return {
                "station_record_id": None,
                "series": {},
                "notice": "no environment records",
            }
        provider, native = station.split(":", 1)
        refs = {
            "station_record_id": env.find(
                self.environment_namespace, "station", provider, native
            ),
            "series": {},
        }
        if provider == "dwd":
            for name in sorted(set(parameters) & CLIMATE_SERIES_COLUMNS):
                rid = env.find(
                    self.environment_namespace,
                    "observation_series",
                    "dwd",
                    f"{native}:{name}",
                )
                if rid:
                    refs["series"][name] = rid
        refs["owner"] = (
            "climate-environment (src.kb.environment_store); cited, never copied"
        )
        return refs

    # ------------------------------------------------------------ evidence links

    def _target_exists(self, kind: str, target_namespace: str, target_id: str) -> bool:
        if kind == "environment-dossier":
            return _table(self.conn, "environment_dossiers") and bool(
                self.conn.execute(
                    "SELECT 1 FROM environment_dossiers WHERE namespace=? AND dossier_id=?",
                    [target_namespace, target_id],
                ).fetchone()
            )
        return _table(self.conn, "event_model_current") and bool(
            self.conn.execute(
                "SELECT 1 FROM event_model_current c JOIN event_model_revisions r ON r.revision_id=c.revision_id "
                "WHERE r.namespace=? AND c.event_id=?",
                [target_namespace, target_id],
            ).fetchone()
        )

    def _row(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT target_kind, target_namespace, target_id, revision_id, citation_json, note, proposed_by, "
            "created_at_ms, history_json, state FROM weather_evidence_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise WeatherError(
                "not_found", "evidence link is not visible in this namespace"
            )
        keys = (
            "target_kind",
            "target_namespace",
            "target_id",
            "revision_id",
            "citation",
            "note",
            "proposed_by",
            "created_at_ms",
            "history",
            "state",
        )
        item = dict(zip(keys, row))
        item["citation"], item["history"] = (
            json.loads(item["citation"]),
            json.loads(item["history"]),
        )
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "link_id": link_id,
            **item,
        }

    def attach(
        self,
        namespace: str,
        *,
        revision_id: str,
        target_kind: str,
        target_id: str,
        note: str,
        principal_id: str,
        scopes: Iterable[str],
        target_namespace: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if target_kind not in TARGET_KINDS or not str(note or "").strip():
            raise WeatherError(
                "invalid_link",
                f"target_kind is one of {TARGET_KINDS} and a note is required",
            )
        target_namespace = target_namespace or (
            self.environment_namespace
            if target_kind == "environment-dossier"
            else namespace
        )
        if (
            "operator" not in scopes
            and not {
                f"namespace:{target_namespace}:read",
                f"namespace:{target_namespace}:write",
            }
            & scopes
        ):
            raise WeatherError("unauthorized", "the target's namespace is not readable")
        if not self._target_exists(target_kind, target_namespace, target_id):
            raise WeatherError(
                "not_found", "the dossier or event record does not exist"
            )
        row = self.conn.execute(
            "SELECT r.record_type, v.content_json FROM weather_revisions v JOIN weather_records r USING(record_id) "
            "WHERE v.namespace=? AND v.revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None or row[0] not in {"warning", "observation_report"}:
            raise WeatherError(
                "invalid_link",
                "only a warning or an observation report revision can be cited",
            )
        content = json.loads(row[1])
        citation = {
            "record_type": row[0],
            "revision_id": revision_id,
            "provider": content["provider"],
            "issuer": content["issuer"],
            "attribution": content["attribution"],
            "reference_time": content["reference_time"],
            "locator": content["locator"],
            "headline": content.get("headline"),
            "event": content.get("event"),
        }
        self.conn.execute(_DDL)
        link_id = (
            "wx-evidence-link:"
            + digest(
                [namespace, target_kind, target_namespace, target_id, revision_id]
            )[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM weather_evidence_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone():
            return self._row(namespace, link_id)
        history = [{"state": "proposed", "by": principal_id, "at_ms": self.now()}]
        self.conn.execute(
            "INSERT INTO weather_evidence_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                target_kind,
                target_namespace,
                target_id,
                revision_id,
                canonical(citation),
                note.strip(),
                principal_id,
                self.now(),
                canonical(history),
                "proposed",
            ],
        )
        return self._row(namespace, link_id)

    def _transition(
        self, namespace: str, link_id: str, state: str, principal_id: str, reason: str
    ) -> dict:
        item = self._row(namespace, link_id)
        history = item["history"] + [
            {"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}
        ]
        self.conn.execute(
            "UPDATE weather_evidence_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link_id],
        )
        return self._row(namespace, link_id)

    def review(
        self,
        namespace: str,
        link_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        item = self._row(namespace, link_id)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise WeatherError("invalid_decision", "accept or reject with a reason")
        if item["state"] != "proposed":
            raise WeatherError("invalid_state", f"link is {item['state']}")
        if item["proposed_by"] == principal_id:
            raise WeatherError(
                "self_review", "the proposer cannot review their own link"
            )
        return self._transition(
            namespace,
            link_id,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
        )

    def revert(
        self,
        namespace: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        item = self._row(namespace, link_id)
        if (
            item["state"] not in {"accepted", "rejected"}
            or not str(reason or "").strip()
        ):
            raise WeatherError(
                "invalid_state", "only a reviewed link can be reverted, with a reason"
            )
        return self._transition(
            namespace, link_id, "reverted", principal_id, reason.strip()
        )

    def for_target(
        self, namespace: str, target_kind: str, target_id: str, *, scopes: Iterable[str]
    ) -> list[dict]:
        """Accepted, unreverted weather citations shown beside a dossier or event (read-only)."""

        authorize(namespace, set(scopes), READ_SCOPE)
        if not _table(self.conn, "weather_evidence_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM weather_evidence_links WHERE namespace=? AND target_kind=? AND "
            "target_id=? AND state='accepted' ORDER BY link_id",
            [namespace, target_kind, target_id],
        ).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

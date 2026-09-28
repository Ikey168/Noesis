"""Station identity across DWD, MOSMIX and aviation identifiers, reviewable and reversible (WX07, #2170).

The same site can appear as a DWD station id, a MOSMIX/WMO id and an ICAO code.
Stations are connected **only** by:

* **source-stated identifiers.** Two stations of different providers whose
  published identifiers share a scheme and value (the MOSMIX catalogue stating
  ICAO ``EDXM`` for ``10999`` and aviationweather.gov stating ICAO ``EDXM``) are
  linked deterministically. Statements come from the environment station
  records and the Weather ``weather_station_identifiers`` table, as published.
* **reviewed matches.** Name or coordinate proximity (within
  :data:`PROPOSE_DISTANCE_M`, only across providers) only *proposes* a
  candidate. The proposal is an entity identity decision (``review``) in the
  shared :class:`src.kb.entity_history.EntityHistoryStore`, so a review-inbox
  task can target it (``{"kind": "entity", "id": <decision_id>}``). Accepting
  (``match``) or rejecting (``non-match``) is a later decision under the same
  event key, made by a principal other than the proposer, whether it goes
  through :meth:`WeatherStationIdentity.review` or through the inbox. A revert
  appends an ``undo``. The candidate's state is always *derived* from those
  decisions, so a revert never reactivates an earlier accepted version. Stronger
  evidence (a source-stated link for the same pair) upgrades a proposed
  candidate. A rejected one is kept and flagged as contradicted.

Two stations of the same provider are never proposed: distinct ids from one
publisher are distinct stations. A station whose id was reused for another site
(consecutive location vintages further apart than :data:`REUSE_DISTANCE_M`) is
split into sites, and a link established on the current site never covers
reports from an earlier site. :func:`WeatherStationIdentity.equivalent` is the
one equivalence definition that queries, verification and monitors share.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

from src.kb import weather_records as wr
from src.kb.weather_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
)
from src.kb.weather_store import (
    ENVIRONMENT_NAMESPACE,
    WeatherError,
    WeatherStore,
    _table,
    authorize,
    require_ready,
)

CONTRACT = "noesis-weather-station-match-v1"
PROPOSE_DISTANCE_M = 1000.0
REUSE_DISTANCE_M = 25_000.0
ENV_ID_SCHEMES = {
    "dwd_station_id": "dwd_station_id",
    "mosmix_id": "mosmix_id",
    "icao": "icao",
    "wmo": "wmo",
}
_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}
_DDL = """
CREATE TABLE IF NOT EXISTS weather_station_candidates(
 namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
 basis TEXT NOT NULL, evidence_json TEXT NOT NULL, event_key TEXT NOT NULL, proposed_by TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, candidate_id));
"""


def distance_m(a: Iterable[float], b: Iterable[float]) -> float:
    """Great-circle distance between two [lon, lat] points (haversine, mean Earth radius 6371008.8 m)."""

    (lon1, lat1), (lon2, lat2) = list(a), list(b)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def entity_id(station_key: str) -> str:
    return "weather-station:" + station_key


class WeatherStationIdentity:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Any = None,
        environment_namespace: str = ENVIRONMENT_NAMESPACE,
    ) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = WeatherStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        self.environment_namespace = environment_namespace
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ stations

    def _env_station(self, key: str) -> dict[str, Any] | None:
        from src.kb.environment_store import EnvironmentStore

        provider, native = key.split(":", 1)
        env = EnvironmentStore(self.conn, initialize=False)
        rid = env.find(self.environment_namespace, "station", provider, native)
        if rid is None:
            return None
        row = self.conn.execute(
            "SELECT v.content_json FROM environment_record_current c JOIN environment_record_revisions v "
            "ON v.revision_id=c.revision_id WHERE c.record_id=?",
            [rid],
        ).fetchone()
        return json.loads(row[0]) if row else None

    def station_keys(self, namespace: str) -> list[str]:
        keys = {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT subject_key FROM weather_records WHERE namespace=? AND record_type IN "
                "('station_location_vintage', 'observation_report')",
                [namespace],
            ).fetchall()
        }
        for (content,) in self.conn.execute(
            "SELECT DISTINCT v.content_json FROM weather_records r JOIN weather_revisions v USING(record_id) "
            "WHERE r.namespace=? AND r.record_type='forecast_issuance'",
            [namespace],
        ).fetchall():
            station = json.loads(content)["location"].get("station")
            if station:
                keys.add(wr.station_key(station))
        keys |= {item["station"] for item in self.store.identifiers(namespace)}
        if _table(self.conn, "environment_records"):
            # Stations the Weather pack registered through the environment owner (station-only providers).
            keys |= {
                f"{r[0]}:{r[1]}"
                for r in self.conn.execute(
                    "SELECT provider, native_id FROM environment_records WHERE namespace=? AND record_type='station' "
                    "AND provider IN ('dwd-mosmix', 'aviationweather')",
                    [self.environment_namespace],
                ).fetchall()
            }
        return sorted(keys)

    def sites(
        self,
        namespace: str,
        station: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Location vintages grouped into sites; a jump beyond REUSE_DISTANCE_M starts a new site (never merged)."""

        vintages = self.store.location_vintages(
            namespace, station, scopes=scopes, cutoff_ms=cutoff_ms
        )
        sites: list[dict[str, Any]] = []
        for vintage in vintages:
            content = vintage["content"]
            point = [float(content["longitude"]), float(content["latitude"])]
            previous = sites[-1] if sites else None
            jump = distance_m(previous["point"], point) if previous else 0.0
            if previous is None or jump > REUSE_DISTANCE_M:
                sites.append(
                    {
                        "site": f"{station}#site{len(sites) + 1}",
                        "point": point,
                        "vintages": [],
                        "basis": None
                        if previous is None
                        else f"moved {round(jump)} m (> {int(REUSE_DISTANCE_M)} m): treated as a reused id",
                    }
                )
            site = sites[-1]
            site["point"] = point
            site["vintages"].append(
                {
                    "revision_id": vintage["revision_id"],
                    "valid_from": content["valid_from"],
                    "valid_to": content.get("valid_to"),
                    "latitude": content["latitude"],
                    "longitude": content["longitude"],
                    "name": content.get("name"),
                }
            )
            if previous is not None and jump <= REUSE_DISTANCE_M and site is previous:
                site.setdefault("relocations", []).append(
                    {"to": content["valid_from"], "moved_m": round(jump, 1)}
                )
        return sites

    def point(
        self, namespace: str, station: str, *, scopes: Iterable[str]
    ) -> list[float] | None:
        sites = self.sites(namespace, station, scopes=scopes)
        if sites:
            return sites[-1]["point"]
        env = self._env_station(station)
        return list(env["geometry"]["coordinates"]) if env else None

    def statements(
        self, namespace: str, *, cutoff_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Every source-stated identifier: environment station identifiers and Weather statements."""

        result = [
            {**item, "source": "weather_station_identifiers"}
            for item in self.store.identifiers(namespace, cutoff_ms=cutoff_ms)
        ]
        for key in self.station_keys(namespace):
            env = self._env_station(key)
            for field, scheme in ENV_ID_SCHEMES.items():
                value = (env or {}).get("identifiers", {}).get(field)
                if value:
                    result.append(
                        {
                            "station": key,
                            "scheme": scheme,
                            "value": str(value).upper(),
                            "stated_by": key.split(":", 1)[0],
                            "source": "environment station record",
                        }
                    )
        return result

    def stated_links(
        self, namespace: str, *, cutoff_ms: int | None = None
    ) -> list[dict[str, Any]]:
        by_value: dict[tuple[str, str], set[str]] = {}
        evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in self.statements(namespace, cutoff_ms=cutoff_ms):
            by_value.setdefault((item["scheme"], item["value"]), set()).add(
                item["station"]
            )
            evidence.setdefault((item["scheme"], item["value"]), []).append(item)
        links = []
        for (scheme, value), stations in sorted(by_value.items()):
            ordered = sorted(stations)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1 :]:
                    if left.split(":")[0] == right.split(":")[0]:
                        continue  # one publisher's two ids are two stations
                    links.append(
                        {
                            "left": left,
                            "right": right,
                            "basis": "source-stated-identifier",
                            "scheme": scheme,
                            "value": value,
                            "state": "linked",
                            "evidence": sorted(
                                evidence[(scheme, value)], key=canonical
                            ),
                        }
                    )
        return links

    # ------------------------------------------------------------------ candidates

    def _state(
        self, namespace: str, event_key: str, *, cutoff_ms: int | None = None
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT decision_id, decision_type, reviewer_id, created_at_ms FROM entity_identity_decisions "
            "WHERE namespace=? AND event_key=? AND (? IS NULL OR created_at_ms<=?) ORDER BY revision DESC LIMIT 1",
            [namespace, event_key, cutoff_ms, cutoff_ms],
        ).fetchone()
        if row is None:
            return {"state": "unknown", "decision_id": None}
        undone = self.conn.execute(
            "SELECT decision_id FROM entity_identity_decisions WHERE namespace=? AND event_key=? "
            "AND (? IS NULL OR created_at_ms<=?)",
            [namespace, "undo:" + row[0], cutoff_ms, cutoff_ms],
        ).fetchone()
        state = {
            "review": "proposed",
            "match": "accepted",
            "non-match": "rejected",
        }.get(row[1], "unknown")
        if undone:
            state = "reverted"
        return {
            "state": state,
            "decision_id": row[0],
            "decided_by": row[2],
            "decided_at_ms": int(row[3]),
            "undo_decision_id": undone[0] if undone else None,
        }

    def candidates(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
        station: str | None = None,
    ) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not _table(self.conn, "weather_station_candidates"):
            return []
        rows = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, basis, evidence_json, event_key, proposed_by, created_at_ms "
            "FROM weather_station_candidates WHERE namespace=? AND (? IS NULL OR created_at_ms<=?) "
            "AND (? IS NULL OR left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, cutoff_ms, cutoff_ms, station, station, station],
        ).fetchall()
        stated = {
            (link["left"], link["right"])
            for link in self.stated_links(namespace, cutoff_ms=cutoff_ms)
        }
        result = []
        for cid, left, right, basis, evidence, event_key, proposed_by, created in rows:
            state = self._state(namespace, event_key, cutoff_ms=cutoff_ms)
            item = {
                "contract": CONTRACT,
                "candidate_id": cid,
                "left": left,
                "right": right,
                "basis": basis,
                "evidence": json.loads(evidence),
                "event_key": event_key,
                "proposed_by": proposed_by,
                "created_at_ms": int(created),
                **state,
                "inbox_target": {
                    "kind": "entity",
                    "namespace": namespace,
                    "id": state["decision_id"],
                },
                "notice": "a candidate is a reviewable proposal; stations are never merged",
            }
            if (left, right) in stated:
                if item["state"] == "proposed":
                    item.update(
                        state="superseded-by-source-statement",
                        basis_upgraded_to="source-stated-identifier",
                    )
                elif item["state"] == "rejected":
                    item["contradicted_by_source"] = True
            result.append(item)
        return result

    def propose(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Propose cross-provider proximity candidates; re-proposing never changes a reviewed candidate."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_ready(self.conn, namespace)
        from src.kb.entity_history import EntityHistoryStore

        self.conn.execute(
            _DDL
        )  # the write path creates what a read-only caller never needs
        self.history = EntityHistoryStore(self.conn, initialize=True, now=self.now)
        keys = self.station_keys(namespace)
        points = {k: self.point(namespace, k, scopes=scopes) for k in keys}
        stated = {
            (link["left"], link["right"]) for link in self.stated_links(namespace)
        }
        created, skipped = [], []
        for i, left in enumerate(keys):
            for right in keys[i + 1 :]:
                if (
                    left.split(":")[0] == right.split(":")[0]
                    or points[left] is None
                    or points[right] is None
                ):
                    continue
                gap = distance_m(points[left], points[right])
                if gap > PROPOSE_DISTANCE_M:
                    continue
                if (left, right) in stated:
                    skipped.append(
                        {
                            "pair": [left, right],
                            "reason": "already linked by a source-stated identifier",
                        }
                    )
                    continue
                cid = "wx-station-candidate:" + digest([namespace, left, right])[:24]
                if self.conn.execute(
                    "SELECT 1 FROM weather_station_candidates WHERE namespace=? AND candidate_id=?",
                    [namespace, cid],
                ).fetchone():
                    continue
                evidence = {
                    "distance_m": round(gap, 1),
                    "threshold_m": PROPOSE_DISTANCE_M,
                    "points": {left: points[left], right: points[right]},
                    "method": "haversine over the current location vintage (or environment station point)",
                }
                event_key = f"weather-station-match:{namespace}:{cid}"
                for key in (left, right):
                    self.history.register_entity(
                        namespace,
                        entity_id(key),
                        [key],
                        principal_id=principal_id,
                        scopes=_HISTORY_SCOPES,
                    )
                self.history.decide(
                    namespace,
                    "review",
                    [entity_id(left), entity_id(right)],
                    {
                        "candidate_id": cid,
                        "basis": "coordinate-proximity",
                        "evidence": evidence,
                        "provenance": {"producer": "weather.observations"},
                        "policy": {"merge": False},
                    },
                    reviewer_id=principal_id,
                    principal_id=principal_id,
                    scopes=_HISTORY_SCOPES,
                    event_key=event_key,
                )
                self.conn.execute(
                    "INSERT INTO weather_station_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        cid,
                        left,
                        right,
                        "coordinate-proximity",
                        canonical(evidence),
                        event_key,
                        principal_id,
                        self.now(),
                    ],
                )
                created.append(cid)
        return {
            "contract": CONTRACT,
            "created": created,
            "skipped": skipped,
            "candidates": self.candidates(namespace, scopes=scopes),
            "stated_links": self.stated_links(namespace),
            "policy": "proximity only proposes; another principal reviews; source-stated identifiers link",
        }

    def _candidate(
        self, namespace: str, candidate_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        item = next(
            (
                c
                for c in self.candidates(namespace, scopes=scopes)
                if c["candidate_id"] == candidate_id
            ),
            None,
        )
        if item is None:
            raise WeatherError(
                "not_found", "station match candidate is not visible in this namespace"
            )
        return item

    def review(
        self,
        namespace: str,
        candidate_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise WeatherError("invalid_decision", "accept or reject with a reason")
        item = self._candidate(namespace, candidate_id, scopes)
        if item["state"] != "proposed":
            raise WeatherError("invalid_state", f"candidate is {item['state']}")
        if item["proposed_by"] == principal_id:
            raise WeatherError(
                "self_review", "the proposer cannot review their own candidate"
            )
        self.history.decide(
            namespace,
            "match" if decision == "accept" else "non-match",
            [entity_id(item["left"]), entity_id(item["right"])],
            {
                "candidate_id": candidate_id,
                "basis": item["basis"],
                "evidence": item["evidence"],
                "reason": reason.strip(),
                "policy": {"merge": False},
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_HISTORY_SCOPES,
            event_key=item["event_key"],
        )
        return self._candidate(namespace, candidate_id, scopes)

    def revert(
        self,
        namespace: str,
        candidate_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise WeatherError("invalid_decision", "a revert needs a reason")
        item = self._candidate(namespace, candidate_id, scopes)
        if item["state"] not in {"accepted", "rejected"}:
            raise WeatherError(
                "invalid_state",
                "only an accepted or rejected candidate can be reverted",
            )
        self.history.undo(
            namespace,
            item["decision_id"],
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_HISTORY_SCOPES,
        )
        return self._candidate(namespace, candidate_id, scopes)

    # ------------------------------------------------------------------ equivalence

    def equivalent(
        self,
        namespace: str,
        station: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """The stations equivalent to ``station``: source-stated links plus accepted, unreverted matches.

        With ``at`` (an observation time), a station whose id was reused is equivalent only to itself
        outside its current site.
        """

        authorize(namespace, scopes, READ_SCOPE)
        links = [
            dict(link) for link in self.stated_links(namespace, cutoff_ms=cutoff_ms)
        ]
        links += [
            {
                "left": c["left"],
                "right": c["right"],
                "basis": "reviewed-match",
                "state": "linked",
                "candidate_id": c["candidate_id"],
                "decision_id": c["decision_id"],
            }
            for c in self.candidates(namespace, scopes=scopes, cutoff_ms=cutoff_ms)
            if c["state"] == "accepted"
        ]

        def current_site(key: str) -> bool:
            if at is None:
                return True
            sites = self.sites(namespace, key, scopes=scopes, cutoff_ms=cutoff_ms)
            if len(sites) < 2:
                return True
            day = at[:10]
            return day >= sites[-1]["vintages"][0]["valid_from"]

        members, frontier, used = {station}, [station], []
        if not current_site(station):
            return {
                "station": station,
                "members": [station],
                "links": [],
                "notice": "the id was reused for another site; earlier-site reports are not linked",
            }
        while frontier:
            key = frontier.pop()
            for link in links:
                other = (
                    link["right"]
                    if link["left"] == key
                    else link["left"]
                    if link["right"] == key
                    else None
                )
                if other is None or other in members or not current_site(other):
                    continue
                members.add(other)
                frontier.append(other)
                used.append(link)
        return {
            "station": station,
            "members": sorted(members),
            "links": used,
            "definition": "source-stated identifier links plus accepted, unreverted reviewed matches",
        }

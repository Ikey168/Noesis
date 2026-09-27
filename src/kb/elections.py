"""Election results and public-opinion series: the ``political.elections`` record owner (#1908, L02).

Records (contract ``noesis-election-record-v1``), each carrying its provider,
source id, source revision (the acquired release) and retrieval time, in
namespace-scoped, revision-addressable ``election_*`` tables:

* **release** - one acquired result file: provider, vintage kind, publication
  date, file digest, evidence origin; the source revision of every figure
  derived from it. Re-acquiring an unchanged file adds nothing, whatever the
  fetch time.
* **election** - one election as the provider identifies it (kind, date,
  jurisdiction).
* **constituency** - one reporting unit in the provider's identifier scheme
  (Wahlkreis number, ONS code, five-digit FIPS) and boundary vintage, with the
  provider's geometry reference (layer, vintage, CRS). Geometry itself lives in
  the Geospatial feature store (:mod:`src.kb.elections_geo`).
* **contest** - one election in one reporting unit for one ballot (first vote,
  second vote, a single ballot or an office), with the jurisdiction's rules as
  attributes cited to their source.
* **candidate-or-list** - a party list or candidate label as one election's
  source states it; identity across elections is a reviewable decision
  (:mod:`src.kb.elections_identity`), never a shared key.
* **result vintage** - what one release stated for one contest: kind
  (``preliminary``, ``certified``, ``recount``, ``corrected``), publication
  date, issuing authority and the figures exactly as published. Vintages are
  appended, never overwritten; the vintage in force on a date is chosen by the
  provider's publication date (a late-arriving older release lands as history).
  A certified figure that changes in a later certified release is recorded as a
  ``corrected`` vintage.
* **poll series** - kept apart in :mod:`src.kb.elections_polls` and the
  dataset observation store; a poll is never a result.

Nothing here predicts seats or outcomes, aggregates polls or states a cause.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.ingestion.election_sources import (
    FORMATS,
    PROVIDER_CONTRACTS,
    REVIEW_BOUNDARY,
    VINTAGE_KINDS,
    normalize_unit_id,
    slug,
)

CONTRACT = "noesis-election-record-v1"
ANSWER_CONTRACT = "noesis-election-answer-v1"
READ_SCOPE = "knowledge:political:elections:read"
WRITE_SCOPE = "knowledge:political:elections:write"
REVIEW_SCOPE = "knowledge:political:elections:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "release",
    "election",
    "constituency",
    "contest",
    "candidate_or_list",
    "result_vintage",
    "poll_series",
    "poll_reading",
)
CERTIFIED_CLASS = ("certified", "recount", "corrected")
# Same publication date: a certified-class vintage follows a preliminary one.
KIND_RANK = {"preliminary": 0, "certified": 1, "recount": 2, "corrected": 2}
# Keys that would carry a prediction, an aggregate poll number or a causal reading; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset(
    {
        "prediction",
        "predicted",
        "predicted_winner",
        "projected_winner",
        "seat_projection",
        "projected_seats",
        "win_probability",
        "probability",
        "poll_average",
        "polling_average",
        "average",
        "mean",
        "aggregate",
        "consensus",
        "true_value",
        "correlation",
        "causation",
        "causal_effect",
        "effect",
        "effect_on_result",
        "influence",
        "impact",
        "swing_explained",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS election_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  vintage_kind TEXT NOT NULL, authority TEXT, published_on TEXT NOT NULL, published_at TEXT, file_sha256 TEXT NOT NULL,
  contests_sha256 TEXT NOT NULL, contest_count INTEGER NOT NULL, selection_json TEXT, dataset_versions_json TEXT,
  rules_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL, run_id TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS election_elections (
  namespace TEXT NOT NULL, election_id TEXT NOT NULL, provider TEXT NOT NULL, native_id TEXT NOT NULL,
  jurisdiction TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, election_date TEXT, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, election_id)
);
CREATE TABLE IF NOT EXISTS election_constituencies (
  namespace TEXT NOT NULL, constituency_id TEXT NOT NULL, scheme TEXT NOT NULL, native_id TEXT NOT NULL,
  boundary_vintage TEXT NOT NULL, name TEXT, jurisdiction TEXT NOT NULL, geometry_ref_json TEXT,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, constituency_id)
);
CREATE TABLE IF NOT EXISTS election_contests (
  namespace TEXT NOT NULL, contest_id TEXT NOT NULL, election_id TEXT NOT NULL, unit_scheme TEXT NOT NULL,
  unit_native_id TEXT NOT NULL, unit_kind TEXT, unit_name TEXT, parent_json TEXT, constituency_id TEXT,
  ballot TEXT NOT NULL, rules_json TEXT NOT NULL, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, contest_id)
);
CREATE TABLE IF NOT EXISTS election_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, election_id TEXT NOT NULL, kind TEXT NOT NULL,
  label TEXT NOT NULL, party TEXT, record_key TEXT NOT NULL, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, candidate_id)
);
CREATE TABLE IF NOT EXISTS election_result_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, contest_id TEXT NOT NULL, vintage_no INTEGER NOT NULL,
  kind TEXT NOT NULL, declared_kind TEXT NOT NULL, previous_vintage_id TEXT, published_on TEXT NOT NULL,
  published_at TEXT, authority TEXT, content_hash TEXT NOT NULL, figures_json TEXT NOT NULL, release_id TEXT NOT NULL,
  source_id TEXT, evidence_origin TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS election_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, contest_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  PRIMARY KEY(namespace, release_id, contest_id, vintage_id)
);
"""

# The provider's own order of a contest's vintages: publication date, kind, then arrival.
SOURCE_ORDER = (
    "published_on DESC, coalesce(published_at, '') DESC, CASE kind WHEN 'preliminary' THEN 0 WHEN 'certified' "
    "THEN 1 ELSE 2 END DESC, vintage_no DESC"
)


class ElectionError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise ElectionError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise ElectionError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def normalize_name(value: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-zÀ-ɏ]+", " ", str(value or "").casefold()).split())


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ElectionError("invalid_date", f"{value!r} is not an ISO date") from exc


def candidate_record_key(
    election_id: str, entry: Mapping[str, Any], unit: Mapping[str, Any]
) -> str:
    if entry["kind"] == "party":
        return f"elections:{election_id}:party:{slug(entry['name'])}"
    return f"elections:{election_id}:{unit['scheme']}:{unit['native_id']}:candidate:{slug(entry['name'])}"


def party_record_key(election_id: str, party: Any) -> str:
    return f"elections:{election_id}:party:{slug(party)}"


def constituency_record_key(scheme: str, native_id: str, boundary_vintage: str) -> str:
    return f"elections:constituency:{scheme}:{native_id}@{boundary_vintage}"


def entity_id(key: str) -> str:
    """The identity-decision subject for one election record (never shared between elections)."""
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Political bundle's optional ``elections`` feature is selected in the active composition plan."""
    del namespace  # composition selection is deployment-wide
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='political'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "elections" in ((plan.get("features") or {}).get("political") or [])


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


class ElectionStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "election_result_vintages")

    # ------------------------------------------------------------------ releases

    def apply_release(
        self,
        namespace: str,
        header: Mapping[str, Any],
        contests: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one release and append a result vintage for every contest whose published figures are new.

        Idempotent by ``(provider, file digest, selection, vintage kind)``; a contest whose figures equal a vintage
        of the same declared kind already stored adds no vintage (in any order of arrival).
        """
        provider = str(header.get("provider") or "")
        if (
            header.get("format") not in FORMATS
            or FORMATS[header["format"]]["provider"] != provider
        ):
            raise ElectionError(
                "invalid_release", "release names a known provider and format"
            )
        declared = str(header.get("vintage_kind") or "")
        if declared not in VINTAGE_KINDS:
            raise ElectionError("invalid_release", "release states its vintage kind")
        if int(header.get("contest_count", -1)) != len(contests):
            raise ElectionError(
                "incomplete_release",
                "a release carries every contest it states; a partial release is refused",
            )
        keys = [c["contest_key"] for c in contests]
        if len(set(keys)) != len(keys):
            raise ElectionError("invalid_release", "release repeats a contest")
        elections = {e["election_id"]: dict(e) for e in header.get("elections") or []}
        for contest in contests:
            if contest["election_id"] not in elections:
                raise ElectionError(
                    "invalid_release",
                    "a contest names an election its release does not state",
                )
            if set(contest["figures"]) & FORBIDDEN_ANSWER_KEYS:
                raise ElectionError(
                    "invalid_release",
                    "published figures carry no derived or predicted values",
                )
        published = _day(header["published_on"])
        selection = header.get("selection")
        origin = "fixture" if header.get("evidence_origin") == "fixture" else "live"
        release_id = (
            "election-release:"
            + digest([namespace, provider, header["file_sha256"], selection, declared])[
                :24
            ]
        )
        if self.conn.execute(
            "SELECT 1 FROM election_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return {
                "release_id": release_id,
                "status": "unchanged",
                "vintages": 0,
                "contests": 0,
            }
        latest = self.conn.execute(
            "SELECT max(sequence) FROM election_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        rules = dict(header.get("rules") or {})
        geometry = dict(header.get("geometry") or {})
        counts = {"vintages": 0, "contests": 0, **dict.fromkeys(VINTAGE_KINDS, 0)}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO election_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    release_id,
                    provider,
                    source_id,
                    header["format"],
                    declared,
                    header.get("authority"),
                    published,
                    header.get("published_at"),
                    header["file_sha256"],
                    header.get("contests_sha256") or digest(list(contests)),
                    len(contests),
                    None if selection is None else canonical(selection),
                    None
                    if header.get("dataset_versions") is None
                    else canonical(header["dataset_versions"]),
                    canonical(rules),
                    origin,
                    header.get("url"),
                    int(latest[0] or 0) + 1,
                    run_id,
                    retrieved,
                ],
            )
            for election in elections.values():
                self.conn.execute(
                    "INSERT OR IGNORE INTO election_elections VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        election["election_id"],
                        provider,
                        str(election["native_id"]),
                        str(election.get("jurisdiction") or header.get("jurisdiction")),
                        str(election.get("kind")),
                        election.get("name"),
                        election.get("election_date"),
                        release_id,
                        retrieved,
                    ],
                )
            for contest in sorted(contests, key=lambda c: c["contest_key"]):
                contest_id, created = self._contest(
                    namespace,
                    contest,
                    elections[contest["election_id"]],
                    header,
                    rules,
                    geometry,
                    release_id,
                    retrieved,
                )
                counts["contests"] += int(created)
                vintage_id, kind = self._observe(
                    namespace,
                    contest_id,
                    contest,
                    header,
                    declared,
                    published,
                    release_id,
                    source_id,
                    origin,
                    retrieved,
                )
                if kind:
                    counts["vintages"] += 1
                    counts[kind] += 1
                self.conn.execute(
                    "INSERT OR IGNORE INTO election_release_members VALUES (?,?,?,?)",
                    [namespace, release_id, contest_id, vintage_id],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": published,
            "vintage_kind": declared,
            "evidence_origin": origin,
            **counts,
        }

    def _contest(
        self,
        namespace,
        contest,
        election,
        header,
        rules,
        geometry,
        release_id,
        retrieved,
    ):
        unit = dict(contest["unit"])
        scheme = str(unit["scheme"])
        native = normalize_unit_id(scheme, unit["native_id"]) or str(unit["native_id"])
        constituency_id = None
        ref = geometry.get(scheme)
        if ref is not None or scheme in {
            "de-bt-wahlkreis",
            "de-be-wahlkreis",
            "gb-ons-pcon",
            "us-fips-county",
        }:
            vintage = str((ref or {}).get("vintage") or election["election_id"])
            constituency_id = (
                "election-constituency:"
                + digest([namespace, scheme, native, vintage])[:24]
            )
            geometry_ref = (
                None
                if ref is None
                else {
                    **{k: ref[k] for k in sorted(ref)},
                    "native_id": native,
                    "note": "the provider's geometry reference; geometry is stored only in the Geospatial store",
                }
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO election_constituencies VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    constituency_id,
                    scheme,
                    native,
                    vintage,
                    unit.get("name"),
                    str(election.get("jurisdiction") or header.get("jurisdiction")),
                    None if geometry_ref is None else canonical(geometry_ref),
                    release_id,
                    retrieved,
                ],
            )
        contest_id = (
            "election-contest:"
            + digest(
                [namespace, contest["election_id"], scheme, native, contest["ballot"]]
            )[:24]
        )
        created = not self.conn.execute(
            "SELECT 1 FROM election_contests WHERE namespace=? AND contest_id=?",
            [namespace, contest_id],
        ).fetchone()
        if created:
            self.conn.execute(
                "INSERT INTO election_contests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    contest_id,
                    contest["election_id"],
                    scheme,
                    native,
                    unit.get("kind"),
                    unit.get("name"),
                    None if unit.get("parent") is None else canonical(unit["parent"]),
                    constituency_id,
                    contest["ballot"],
                    canonical(rules),
                    release_id,
                    retrieved,
                ],
            )
        for entry in contest["figures"].get("entries") or []:
            labels = [
                (
                    entry["kind"],
                    entry["name"],
                    candidate_record_key(contest["election_id"], entry, unit),
                )
            ]
            if entry["kind"] == "candidate" and entry.get("party"):
                labels.append(
                    (
                        "party",
                        entry["party"],
                        party_record_key(contest["election_id"], entry["party"]),
                    )
                )
            for kind, label, key in labels:
                self.conn.execute(
                    "INSERT OR IGNORE INTO election_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        "election-candidate:" + digest([namespace, key])[:24],
                        contest["election_id"],
                        "list" if kind == "party" else "candidate",
                        label,
                        entry.get("party") if kind == "candidate" else label,
                        key,
                        release_id,
                        retrieved,
                    ],
                )
        return contest_id, created

    def _observe(
        self,
        namespace,
        contest_id,
        contest,
        header,
        declared,
        published,
        release_id,
        source_id,
        origin,
        retrieved,
    ):
        figures = json.loads(canonical(contest["figures"]))
        content_hash = digest(figures)
        # The same observation: equal figures of the same declared kind published on the same date (a replayed or
        # re-serialised file, in any order of arrival), or equal to the vintage of the same class in force at this
        # publication date. Equal figures to an *earlier*, since superseded vintage are a new vintage - a reversion
        # to earlier figures is itself a correction.
        same = self.conn.execute(
            "SELECT vintage_id FROM election_result_vintages WHERE namespace=? AND contest_id=? AND content_hash=? "
            "AND declared_kind=? AND published_on=? ORDER BY vintage_no LIMIT 1",
            [namespace, contest_id, content_hash, declared, published],
        ).fetchone()
        if same:
            return same[0], None
        kinds = ["preliminary"] if declared == "preliminary" else list(CERTIFIED_CLASS)
        current = self.conn.execute(
            "SELECT vintage_id, content_hash FROM election_result_vintages WHERE namespace=? AND contest_id=? AND "
            f"published_on<=? AND list_contains(?, kind) ORDER BY {SOURCE_ORDER} LIMIT 1",
            [namespace, contest_id, published, kinds],
        ).fetchone()
        if current and current[1] == content_hash:
            return current[0], None
        kind = declared
        if declared == "certified":
            # A certified figure that differs from an earlier-dated certified-class vintage is a correction.
            earlier = self.conn.execute(
                "SELECT count(*) FROM election_result_vintages WHERE namespace=? AND contest_id=? AND "
                "kind IN ('certified','recount','corrected') AND published_on<=? AND content_hash<>?",
                [namespace, contest_id, published, content_hash],
            ).fetchone()[0]
            if earlier:
                kind = "corrected"
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(vintage_no), 0) FROM election_result_vintages WHERE namespace=? AND contest_id=?",
                [namespace, contest_id],
            ).fetchone()[0]
        )
        previous = self.conn.execute(
            "SELECT vintage_id FROM election_result_vintages WHERE namespace=? AND contest_id=? AND "
            "(published_on<? OR (published_on=? AND CASE kind WHEN 'preliminary' THEN 0 WHEN 'certified' THEN 1 "
            f"ELSE 2 END <= ?)) ORDER BY {SOURCE_ORDER} LIMIT 1",
            [namespace, contest_id, published, published, KIND_RANK[kind]],
        ).fetchone()
        vintage_id = (
            "election-vintage:"
            + digest([namespace, contest_id, number, release_id])[:24]
        )
        self.conn.execute(
            "INSERT INTO election_result_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                vintage_id,
                contest_id,
                number,
                kind,
                declared,
                previous[0] if previous else None,
                published,
                header.get("published_at"),
                header.get("authority"),
                content_hash,
                canonical(figures),
                release_id,
                source_id,
                origin,
                retrieved,
            ],
        )
        return vintage_id, kind

    # ------------------------------------------------------------------ record views

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, vintage_kind, authority, published_on, published_at, "
            "file_sha256, contest_count, selection_json, dataset_versions_json, rules_json, evidence_origin, url, "
            "sequence, run_id, retrieved_at_ms FROM election_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise ElectionError("not_found", "release is not visible in this namespace")
        keys = (
            "release_id",
            "provider",
            "source_id",
            "format",
            "vintage_kind",
            "authority",
            "published_on",
            "published_at",
            "file_sha256",
            "contest_count",
            "selection",
            "dataset_versions",
            "rules",
            "evidence_origin",
            "url",
            "sequence",
            "run_id",
            "retrieved_at_ms",
        )
        view = dict(zip(keys, row))
        for key, default in (
            ("selection", None),
            ("dataset_versions", None),
            ("rules", {}),
        ):
            view[key] = _load(view[key], default)
        return {
            "contract": CONTRACT,
            "record_type": "release",
            "namespace": namespace,
            **view,
        }

    @staticmethod
    def source_revision(release: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "release_id": release["release_id"],
            "provider": release["provider"],
            "source_id": release["source_id"],
            "file_sha256": release["file_sha256"],
            "published_on": release["published_on"],
            "url": release["url"],
            "evidence_origin": release["evidence_origin"],
            "retrieved_at_ms": release["retrieved_at_ms"],
        }

    def election(self, namespace: str, election_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT election_id, provider, native_id, jurisdiction, kind, name, election_date, first_release_id "
            "FROM election_elections WHERE namespace=? AND election_id=?",
            [namespace, election_id],
        ).fetchone()
        if row is None:
            raise ElectionError(
                "not_found", "election is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "election_id",
                    "provider",
                    "native_id",
                    "jurisdiction",
                    "kind",
                    "name",
                    "election_date",
                    "first_release_id",
                ),
                row,
            )
        )
        return {
            "contract": CONTRACT,
            "record_type": "election",
            "namespace": namespace,
            **view,
        }

    def constituency(self, namespace: str, constituency_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT constituency_id, scheme, native_id, boundary_vintage, name, jurisdiction, geometry_ref_json, "
            "first_release_id FROM election_constituencies WHERE namespace=? AND constituency_id=?",
            [namespace, constituency_id],
        ).fetchone()
        if row is None:
            raise ElectionError(
                "not_found", "constituency is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "constituency_id",
                    "scheme",
                    "native_id",
                    "boundary_vintage",
                    "name",
                    "jurisdiction",
                    "geometry_ref",
                    "first_release_id",
                ),
                row,
            )
        )
        view["geometry_ref"] = _load(view["geometry_ref"], None)
        view["record_key"] = constituency_record_key(
            view["scheme"], view["native_id"], view["boundary_vintage"]
        )
        return {
            "contract": CONTRACT,
            "record_type": "constituency",
            "namespace": namespace,
            **view,
        }

    def constituencies(
        self, namespace: str, *, scheme: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT constituency_id FROM election_constituencies WHERE namespace=? AND (? IS NULL OR scheme=?) "
            "ORDER BY scheme, native_id, boundary_vintage",
            [namespace, scheme, scheme],
        ).fetchall()
        return [self.constituency(namespace, r[0]) for r in rows]

    def find_constituency(
        self,
        namespace: str,
        scheme: str,
        native_id: str,
        boundary_vintage: str | None = None,
    ) -> list[dict[str, Any]]:
        native = normalize_unit_id(scheme, native_id) or str(native_id)
        rows = self.conn.execute(
            "SELECT constituency_id FROM election_constituencies WHERE namespace=? AND scheme=? AND native_id=? "
            "AND (? IS NULL OR boundary_vintage=?) ORDER BY boundary_vintage",
            [namespace, scheme, native, boundary_vintage, boundary_vintage],
        ).fetchall()
        return [self.constituency(namespace, r[0]) for r in rows]

    def contest(self, namespace: str, contest_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT contest_id, election_id, unit_scheme, unit_native_id, unit_kind, unit_name, parent_json, "
            "constituency_id, ballot, rules_json, first_release_id FROM election_contests WHERE namespace=? AND "
            "contest_id=?",
            [namespace, contest_id],
        ).fetchone()
        if row is None:
            raise ElectionError("not_found", "contest is not visible in this namespace")
        view = dict(
            zip(
                (
                    "contest_id",
                    "election_id",
                    "unit_scheme",
                    "unit_native_id",
                    "unit_kind",
                    "unit_name",
                    "parent",
                    "constituency_id",
                    "ballot",
                    "rules",
                    "first_release_id",
                ),
                row,
            )
        )
        view["parent"] = _load(view["parent"], None)
        view["rules"] = _load(view["rules"], {})
        view["rules_citation"] = {
            "source_url": view["rules"].get("source_url"),
            "release_id": view["first_release_id"],
            "note": "jurisdiction rules as the source manifest cites them; no cross-jurisdiction normalisation",
        }
        return {
            "contract": CONTRACT,
            "record_type": "contest",
            "namespace": namespace,
            **view,
        }

    def contests(
        self,
        namespace: str,
        *,
        election_id: str | None = None,
        constituency_id: str | None = None,
        unit_scheme: str | None = None,
        unit_native_id: str | None = None,
        ballot: str | None = None,
    ) -> list[dict[str, Any]]:
        native = (
            None
            if unit_native_id is None
            else (
                normalize_unit_id(str(unit_scheme), unit_native_id)
                or str(unit_native_id)
            )
        )
        rows = self.conn.execute(
            "SELECT contest_id FROM election_contests WHERE namespace=? AND (? IS NULL OR election_id=?) AND "
            "(? IS NULL OR constituency_id=?) AND (? IS NULL OR unit_scheme=?) AND (? IS NULL OR unit_native_id=?) "
            "AND (? IS NULL OR ballot=?) ORDER BY election_id, unit_scheme, unit_native_id, ballot",
            [
                namespace,
                election_id,
                election_id,
                constituency_id,
                constituency_id,
                unit_scheme,
                unit_scheme,
                native,
                native,
                ballot,
                ballot,
            ],
        ).fetchall()
        return [self.contest(namespace, r[0]) for r in rows]

    def candidates(
        self, namespace: str, *, election_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT candidate_id, election_id, kind, label, party, record_key, first_release_id FROM "
            "election_candidates WHERE namespace=? AND (? IS NULL OR election_id=?) ORDER BY record_key",
            [namespace, election_id, election_id],
        ).fetchall()
        return [
            {
                "contract": CONTRACT,
                "record_type": "candidate_or_list",
                "namespace": namespace,
                **dict(
                    zip(
                        (
                            "candidate_id",
                            "election_id",
                            "kind",
                            "label",
                            "party",
                            "record_key",
                            "first_release_id",
                        ),
                        r,
                    )
                ),
            }
            for r in rows
        ]

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT vintage_id, contest_id, vintage_no, kind, declared_kind, previous_vintage_id, published_on, "
            "published_at, authority, content_hash, figures_json, release_id, source_id, evidence_origin, "
            "retrieved_at_ms FROM election_result_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()
        if row is None:
            raise ElectionError(
                "not_found", "result vintage is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "vintage_id",
                    "contest_id",
                    "vintage_no",
                    "kind",
                    "declared_kind",
                    "previous_vintage_id",
                    "published_on",
                    "published_at",
                    "authority",
                    "content_hash",
                    "figures",
                    "release_id",
                    "source_id",
                    "evidence_origin",
                    "retrieved_at_ms",
                ),
                row,
            )
        )
        view["figures"] = _load(view["figures"], {})
        view["source_revision"] = self.source_revision(
            self.release(namespace, view["release_id"])
        )
        view["note"] = (
            "figures as published by the issuing authority; nothing recomputed"
        )
        return {
            "contract": CONTRACT,
            "record_type": "result_vintage",
            "namespace": namespace,
            **view,
        }

    def history(self, namespace: str, contest_id: str) -> list[dict[str, Any]]:
        """Every vintage of a contest in the provider's date order (arrival breaks ties)."""
        rows = self.conn.execute(
            "SELECT vintage_id FROM election_result_vintages WHERE namespace=? AND contest_id=? "
            f"ORDER BY {SOURCE_ORDER}",
            [namespace, contest_id],
        ).fetchall()
        return [self.vintage(namespace, r[0]) for r in reversed(rows)]

    def in_force(
        self,
        namespace: str,
        contest_id: str,
        as_of: str | None = None,
        *,
        kinds: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """The vintage current on a date by the provider's publication date (None before the first)."""
        as_of = None if as_of is None else _day(as_of)
        kinds = list(kinds or VINTAGE_KINDS)
        row = self.conn.execute(
            "SELECT vintage_id FROM election_result_vintages WHERE namespace=? AND contest_id=? AND "
            f"(? IS NULL OR published_on<=?) AND list_contains(?, kind) ORDER BY {SOURCE_ORDER} LIMIT 1",
            [namespace, contest_id, as_of, as_of, kinds],
        ).fetchone()
        return None if row is None else self.vintage(namespace, row[0])

    def results(
        self, namespace: str, contest_id: str, *, as_of: str | None = None
    ) -> dict[str, Any]:
        contest = self.contest(namespace, contest_id)
        history = self.history(namespace, contest_id)
        current = self.in_force(namespace, contest_id, as_of)
        certified = self.in_force(namespace, contest_id, as_of, kinds=CERTIFIED_CLASS)
        return {
            "contract": ANSWER_CONTRACT,
            "contest": contest,
            "election": self.election(namespace, contest["election_id"]),
            "constituency": None
            if contest["constituency_id"] is None
            else self.constituency(namespace, contest["constituency_id"]),
            "as_of": as_of,
            "current": current,
            "status": "no-result"
            if current is None
            else "certified"
            if current["kind"] in CERTIFIED_CLASS
            else "preliminary-only"
            if certified is None
            else "preliminary",
            "certified": certified,
            "history": [
                v for v in history if as_of is None or v["published_on"] <= _day(as_of)
            ],
            "changes": self.changes(namespace, contest_id, as_of=as_of),
            "review_boundary": REVIEW_BOUNDARY,
        }

    @staticmethod
    def _flatten(figures: Mapping[str, Any]) -> dict[tuple[str, str], Any]:
        flat = {}
        for key, value in dict(figures.get("totals") or {}).items():
            if isinstance(value, Mapping):
                for sub, item in value.items():
                    flat[(f"totals.{key}", str(sub))] = item
            else:
                flat[("totals", key)] = value
        for entry in figures.get("entries") or []:
            where = entry["key"] + (f"@{entry['mode']}" if entry.get("mode") else "")
            for field in (
                "votes",
                "share_published",
                "mandates_published",
                "elected_published",
            ):
                if field in entry:
                    flat[(where, field)] = entry[field]
        return flat

    def changes(
        self, namespace: str, contest_id: str, *, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        """Figures that differ between consecutive vintages (by the provider's dates), each side cited.

        Values are shown as published before and after; no difference or swing is computed.
        """
        history = [
            v
            for v in self.history(namespace, contest_id)
            if as_of is None or v["published_on"] <= _day(as_of)
        ]
        out = []
        for before, after in zip(history, history[1:]):
            old, new = self._flatten(before["figures"]), self._flatten(after["figures"])
            figures = [
                {
                    "entry": key[0],
                    "field": key[1],
                    "before": old.get(key),
                    "after": new.get(key),
                }
                for key in sorted(set(old) | set(new))
                if old.get(key) != new.get(key)
            ]
            out.append(
                {
                    "from": {
                        k: before[k]
                        for k in (
                            "vintage_id",
                            "kind",
                            "published_on",
                            "source_revision",
                        )
                    },
                    "to": {
                        k: after[k]
                        for k in (
                            "vintage_id",
                            "kind",
                            "published_on",
                            "source_revision",
                        )
                    },
                    "transition": f"{before['kind']}->{after['kind']}",
                    "changed_figures": figures,
                    "unchanged": not figures,
                }
            )
        return out

    def elections(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT election_id FROM election_elections WHERE namespace=? ORDER BY election_date, election_id",
            [namespace],
        ).fetchall()
        return [self.election(namespace, r[0]) for r in rows]


class ElectionProjector:
    """Source-pack runtime projector for ``noesis-election-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = ElectionStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("elections") or {}).get("namespace") or DEFAULT_NAMESPACE
        )

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, contest = (
                dict(item.get("election_release") or {}),
                item.get("election_contest"),
            )
            if not header or not isinstance(contest, Mapping):
                raise ElectionError(
                    "invalid_record", "page record is not an election contest"
                )
            groups.setdefault(header["file_sha256"], (header, []))[1].append(
                dict(contest)
            )
        return [
            self.store.apply_release(
                self._namespace(source),
                header,
                contests,
                run_id=run_id,
                source_id=source["source_id"],
            )
            for header, contests in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM election_releases WHERE namespace=? AND source_id=? "
            "ORDER BY published_on DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_release_id": row[0] if row else None,
            "latest_published_on": row[1] if row else None,
        }


def readiness(conn: Any) -> dict[str, Any]:
    store = ElectionStore(conn, initialize=False)
    ready = store.ready()
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(
                conn.execute(
                    "SELECT count(*) FROM election_releases WHERE provider=?",
                    [provider],
                ).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "releases": releases,
            "live": "not-implemented"
            if contract["access_decision"] == "not-implemented"
            else "outstanding",
        }
    return {
        "feature": "political.elections",
        "enabled": feature_enabled(conn),
        "store_ready": ready,
        "providers": providers,
        "review_boundary": REVIEW_BOUNDARY,
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a prediction, a poll aggregate or a causal reading."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found

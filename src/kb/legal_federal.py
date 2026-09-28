"""Federal statutes in the Legal store: versions, provisions, amendment acts, citations and links (#2105).

Federal statutes use the Legal work/expression/version/passage model of
:mod:`src.kb.legal`; this module adds what the model needs for them without a
parallel store:

* **Statute work** - one per statute of the bounded set, keyed by its official
  abbreviation (``jurabk``); ELIs, gii document numbers and titles are recorded
  as identifiers as the sources state them. Observed and source-stated
  versions of one statute are versions of this one work, so they can be
  compared and conflicts shown.
* **Statute version** - a :class:`LegalStore` version with a
  ``legal_statute_versions`` row carrying ``validity_basis``:
  ``source_stated`` (rechtsinformationen.bund.de states ``validity_from`` /
  ``validity_to``) or ``observed`` (gesetze-im-internet.de text seen at an
  observation time, with the source's "Stand" notes verbatim). An observed
  fetch is compared with the version current at its observation time: the
  same text is a sighting (``unchanged``), different text a new version; a
  later return to an earlier text is a new version, never a reactivation.
  A source-stated expression (ELI) is compared with its current row only:
  a replay adds nothing, changed text or validity is a correction, and a
  late-arriving older row (by the source's own modification date) is kept as
  history without a correction.
* **Provision** - passages located by the shared path (``§5/abs2``).
* **Amendment act** - a Federal Law Gazette promulgation work with its
  amending instructions (``legal_amendments``) to statute and provision where
  unambiguous; instructions are never applied.
* **Links** - court decisions to provisions (``legal_provision_citations``,
  with the provision version selected for the decision date), acts and
  statutes to the EU acts they state they implement
  (``legal_implementation_links``) and acts to Bundestag DIP dossiers
  (``legal_dossier_links``, relation ``enacted_as``).

Nothing here states that a provision is in force: a source-stated interval is
what the source states; an observed version is only what was seen on a date.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from src.kb.legal import (
    READ_SCOPE,
    REVIEW_BOUNDARY,
    WRITE_SCOPE,
    LegalError,
    LegalStore,
    _authorize,
    _canonical,
    _digest,
    _load,
    _locator_key,
)
from src.kb.legal_citations import (
    implementation_statements,
    parse_bgbl_references,
    parse_citations,
    path_contains,
    resolve_citation,
    statute_key,
)

PROVISION_CONTRACT = "noesis-legal-provision-selection-v1"
PROVISION_COMPARISON_CONTRACT = "noesis-legal-provision-comparison-v1"
STATUTE_PROVIDER = "federal-statutes"
OBSERVED, STATED = "observed", "source_stated"
DOSSIER_READ_SCOPE = "knowledge:political:dossier:read"
SEMANTICS = (
    "Source-stated versions carry the validity interval the source states; observed versions are the text "
    "Noesis saw on a date, with no validity stated. Neither is a statement that the provision is in force."
)
TABLES = (
    "legal_statute_versions",
    "legal_statute_observations",
    "legal_amendments",
    "legal_provision_citations",
    "legal_implementation_links",
)


def _d(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    day = _d(value)
    return day.isoformat() if day else None


def current_stated(rows: Iterable[tuple[str, Any, int]]) -> str | None:
    """The current row of one source-stated expression from ``(version_id, source_modified, observed_ms)``.

    Rows are taken in observation order; a later row replaces the current one
    unless both state a modification date and the later row's date is older
    (late-arriving history). A row without a date is therefore ranked by when
    it was observed, never as the earliest.
    """
    current: tuple[str, date | None] | None = None
    for version_id, modified, _observed in sorted(rows, key=lambda r: (r[2], r[0])):
        day = _d(modified)
        if current is None or not (day and current[1] and day < current[1]):
            current = (version_id, day)
    return current[0] if current else None


def _ms_day(ms: int) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date()


# ------------------------------------------------------------------ projection


def _statute_work(
    store: LegalStore, namespace: str, record: Mapping[str, Any], run_id: str
) -> tuple[str, str]:
    fields = dict(record.get("fields") or {})
    key = statute_key(fields.get("statute_key") or fields.get("jurabk"))
    if not key:
        raise LegalError(
            "invalid_record", "statute record lacks its abbreviation (jurabk)"
        )
    native_id = f"statute:{key}"
    work_id = (
        "legal-work:" + _digest([namespace, STATUTE_PROVIDER, "DE", native_id])[:24]
    )
    stated = {
        "jurabk": fields.get("jurabk"),
        "statute_key": key,
        "eli_work": [fields["eli_work"]] if fields.get("eli_work") else [],
        "gii_doknr": [fields["doknr"]] if fields.get("doknr") else [],
        "amtabk": [fields["amtabk"]] if fields.get("amtabk") else [],
        "titles": [t for t in (fields.get("long_title"), record.get("title")) if t],
    }
    store.conn.execute(
        "INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            work_id,
            namespace,
            STATUTE_PROVIDER,
            "DE",
            "statute",
            "statute",
            native_id,
            _canonical({k: v for k, v in stated.items() if v not in (None, "", [])}),
            None,
            fields.get("long_title") or record.get("title"),
            run_id,
            store.now(),
        ],
    )
    row = store.conn.execute(
        "SELECT identifiers_json FROM legal_works WHERE work_id=?", [work_id]
    ).fetchone()
    known = _load(row[0], {})
    merged = dict(known)
    for name, value in stated.items():
        if isinstance(value, list):
            merged[name] = sorted({*known.get(name, []), *value})
        elif value and name not in merged:
            merged[name] = value
    merged = {k: v for k, v in merged.items() if v not in (None, "", [])}
    if merged != known:
        # Identifiers are only ever added; a stated identifier is never rewritten.
        store.conn.execute(
            "UPDATE legal_works SET identifiers_json=? WHERE work_id=?",
            [_canonical(merged), work_id],
        )
    return work_id, key


def _insert_version(
    store: LegalStore,
    namespace: str,
    record: Mapping[str, Any],
    *,
    work_id: str,
    version_id: str,
    native_version: str,
    run_id: str,
    source_id: str | None,
    document_id: str | None,
    observed_ms: int,
) -> int:
    key = statute_key(
        record["fields"].get("statute_key") or record["fields"].get("jurabk")
    )
    expression_id = (
        "legal-expression:" + _digest([work_id, "de", f"statute:{key}@de"])[:24]
    )
    store.conn.execute(
        "INSERT OR IGNORE INTO legal_expressions VALUES (?,?,?,?,?,?)",
        [
            expression_id,
            namespace,
            work_id,
            "de",
            f"statute:{key}@de",
            record.get("title"),
        ],
    )
    sections = list(record.get("sections") or [])
    store.conn.execute(
        "INSERT INTO legal_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            version_id,
            namespace,
            work_id,
            expression_id,
            native_version,
            record["fields"]["text_sha256"],
            _canonical(
                {k: v for k, v in record.items() if k not in {"sections", "native"}}
            ),
            None,
            "captured-text" if sections else "metadata-only",
            len(sections),
            document_id,
            source_id,
            run_id,
            observed_ms,
        ],
    )
    for ordinal, section in enumerate(sections):
        locator = dict(section.get("locator") or {})
        store.conn.execute(
            "INSERT INTO legal_passages VALUES (?,?,?,?,?)",
            [
                version_id,
                ordinal,
                _locator_key(locator),
                _canonical(locator),
                section["text"],
            ],
        )
    return len(sections)


def project_statute_version(
    store: LegalStore,
    namespace: str,
    record: Mapping[str, Any],
    *,
    run_id: str,
    source_id: str | None,
    observed_ms: int,
    document_id: str | None = None,
    response_sha256: str | None = None,
    touched: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Project one observed or source-stated statute version (idempotent; history kept in any arrival order).

    ``touched`` collects what changed (statute keys, new statutes, new versions) so
    the federal links are refreshed incrementally for exactly those records.
    """
    counts = _project_statute_version(
        store,
        namespace,
        record,
        run_id=run_id,
        source_id=source_id,
        observed_ms=observed_ms,
        document_id=document_id,
        response_sha256=response_sha256,
        touched=touched,
    )
    return counts


def _project_statute_version(
    store: LegalStore,
    namespace: str,
    record: Mapping[str, Any],
    *,
    run_id: str,
    source_id: str | None,
    observed_ms: int,
    document_id: str | None,
    response_sha256: str | None,
    touched: dict[str, Any] | None,
) -> dict[str, int]:
    touched = touched if touched is not None else {}
    fields = dict(record.get("fields") or {})
    pre_key = statute_key(fields.get("statute_key") or fields.get("jurabk"))
    known = store.conn.execute(
        "SELECT 1 FROM legal_works WHERE namespace=? AND work_kind='statute' AND native_id=?",
        [namespace, f"statute:{pre_key}"],
    ).fetchone()
    if not known and pre_key:
        touched.setdefault("new_statutes", set()).add(
            str(fields.get("jurabk") or pre_key)
        )
    provider = record["provider"]
    basis = fields.get("validity_basis")
    if (
        (provider == "gesetze-im-internet") != (basis == OBSERVED)
        or basis not in {OBSERVED, STATED}
        or not fields.get("text_sha256")
    ):
        raise LegalError(
            "invalid_record",
            "statute versions state their validity basis and text hash",
        )
    work_id, key = _statute_work(store, namespace, record, run_id)
    sha = str(fields["text_sha256"])
    native_id = str(record["provider_id"])
    counts = {
        "statute_versions": 0,
        "statute_observations": 0,
        "unchanged": 0,
        "corrections": 0,
        "passages": 0,
    }
    conn = store.conn

    def sighting(version_id: str, outcome: str) -> None:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO legal_statute_observations VALUES (?,?,?,?,?,?,?) RETURNING version_id",
            [
                version_id,
                namespace,
                observed_ms,
                run_id,
                source_id,
                response_sha256,
                outcome,
            ],
        ).fetchall()
        counts["statute_observations"] += len(inserted)
        if inserted:
            # A new sighting can change which version a date selects for this statute.
            touched.setdefault("statute_keys", set()).add(key)

    if basis == OBSERVED:
        rows = conn.execute(
            "SELECT s.version_id, s.text_sha256, o.observed_at_ms FROM legal_statute_versions s "
            "JOIN legal_statute_observations o ON o.version_id=s.version_id WHERE s.namespace=? AND s.work_id=? "
            "AND s.provider=?",
            [namespace, work_id, provider],
        ).fetchall()
        before = [r for r in rows if r[2] <= observed_ms]
        after = [r for r in rows if r[2] > observed_ms]
        previous = max(before, key=lambda r: (r[2], r[0])) if before else None
        following = min(after, key=lambda r: (r[2], r[0])) if after else None
        if previous and previous[1] == sha:
            sighting(previous[0], "unchanged")
            counts["unchanged"] += 1
            return counts
        if following and following[1] == sha:
            # A late-arriving earlier fetch of the text that follows it: an earlier sighting, not a new version.
            sighting(following[0], "earlier_sighting")
            return counts
        version_id = (
            "legal-version:"
            + _digest([work_id, provider, OBSERVED, sha, observed_ms])[:24]
        )
        native_version = f"gii:{key}:{sha[:16]}@{observed_ms}"
        corrects = None
        outcome = "changed" if previous else "first"
    else:
        rows = conn.execute(
            "SELECT version_id, text_sha256, validity_from, validity_to, source_modified, first_observed_at_ms "
            "FROM legal_statute_versions WHERE namespace=? AND work_id=? AND provider=? AND native_id=?",
            [namespace, work_id, provider, native_id],
        ).fetchall()
        signature = (
            sha,
            _iso(fields.get("validity_from")),
            _iso(fields.get("validity_to")),
        )
        modified = _d(fields.get("source_modified"))
        current_id = current_stated((r[0], r[4], r[5]) for r in rows)
        current = next((r for r in rows if r[0] == current_id), None)
        same = [
            r
            for r in rows
            if (r[1], _iso(r[2]), _iso(r[3])) == signature and _d(r[4]) == modified
        ]
        if current and (current[1], _iso(current[2]), _iso(current[3])) == signature:
            sighting(current[0], "unchanged")
            counts["unchanged"] += 1
            return counts
        late = bool(
            current and modified and _d(current[4]) and modified < _d(current[4])
        )
        if late and same:
            sighting(same[0][0], "late_history")
            return counts
        version_id = (
            "legal-version:"
            + _digest(
                [work_id, provider, native_id, *signature, _iso(modified), observed_ms]
            )[:24]
        )
        native_version = native_id
        corrects = None if late or current is None else current[0]
        outcome = "late_history" if late else "correction" if corrects else "first"
        counts["corrections"] += int(bool(corrects))
    counts["passages"] += _insert_version(
        store,
        namespace,
        record,
        work_id=work_id,
        version_id=version_id,
        native_version=native_version,
        run_id=run_id,
        source_id=source_id,
        document_id=document_id,
        observed_ms=observed_ms,
    )
    amended_by = sorted(
        {
            r["key"]
            for s in fields.get("stand") or []
            for r in parse_bgbl_references(s.get("comment"))
            if r["key"] and str(s.get("type") or "").casefold() in {"stand", "neuf", ""}
        }
        | set(fields.get("amended_by") or [])
    )
    conn.execute(
        "INSERT INTO legal_statute_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            version_id,
            namespace,
            work_id,
            key,
            provider,
            source_id,
            native_id,
            basis,
            _iso(fields.get("validity_from")),
            _iso(fields.get("validity_to")),
            fields.get("temporal_coverage"),
            _iso(fields.get("source_modified")),
            _canonical(list(fields.get("stand") or [])),
            _canonical(amended_by),
            sha,
            observed_ms,
            corrects,
            run_id,
        ],
    )
    sighting(version_id, outcome)
    counts["statute_versions"] += 1
    touched.setdefault("federal_versions", set()).add(version_id)
    touched.setdefault("statute_keys", set()).add(key)
    return counts


def record_amendments(
    store: LegalStore,
    namespace: str,
    record: Mapping[str, Any],
    *,
    work_id: str,
    version_id: str,
) -> int:
    """Store the act's amending instructions (target statute and provision where unambiguous, text otherwise)."""
    fields = dict(record.get("fields") or {})
    count = 0
    for ordinal, amendment in enumerate(fields.get("amendments") or []):
        amendment_id = "legal-amendment:" + _digest([version_id, ordinal])[:24]
        store.conn.execute(
            "INSERT OR IGNORE INTO legal_amendments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                amendment_id,
                namespace,
                work_id,
                version_id,
                fields["bgbl_key"],
                ordinal,
                amendment.get("article"),
                amendment.get("item"),
                amendment.get("statute_key"),
                None,
                amendment.get("provision"),
                amendment.get("action"),
                amendment["status"],
                amendment["instruction"],
                _canonical(amendment.get("locator") or {}),
            ],
        )
        count += 1
    return count


def federal_present(conn: Any, namespace: str) -> bool:
    """Whether federal-statute records exist in the namespace or the feature is selected (reads only)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM legal_works WHERE namespace=? AND work_kind IN ('statute','amendment_act') LIMIT 1",
            [namespace],
        ).fetchone()
    except Exception:  # noqa: BLE001 - legal tables absent
        row = None
    return bool(row) or feature_enabled(conn)


def rebuild_federal_links(store: LegalStore, namespace: str) -> None:
    """Full rebuild: amendment targets, every implementation statement and every decision citation."""
    _resolve_amendment_targets(store, namespace)
    _implementation_links(store, namespace, None)
    FederalStatutes(store).rebuild_decision_citations(namespace)


def refresh_federal_links(
    store: LegalStore,
    namespace: str,
    touched: Mapping[str, Any] | None = None,
    *,
    was_present: bool = True,
) -> None:
    """Refresh federal links for exactly the records an ingest touched; the result equals a full rebuild.

    Nothing runs unless federal records exist or the feature is selected. The
    first ingest that brings federal records into a namespace rebuilds once in
    full (decisions acquired earlier gain their citation rows); afterwards only
    new statute/act versions are scanned for implementation statements, and
    only decisions that are new, cite a statute whose versions or sightings
    changed, or mention a newly acquired statute's abbreviation are re-linked.
    """
    if not federal_present(store.conn, namespace):
        return
    if touched is None or not was_present:
        rebuild_federal_links(store, namespace)
        return
    statute_keys = set(touched.get("statute_keys") or ())
    new_statutes = set(touched.get("new_statutes") or ())
    federal_versions = set(touched.get("federal_versions") or ())
    if statute_keys or touched.get("acts"):
        _resolve_amendment_targets(store, namespace)
    if federal_versions or touched.get("cellar"):
        _implementation_links(store, namespace, federal_versions)
    decisions = set(touched.get("decision_works") or ())
    if statute_keys:
        decisions |= {
            r[0]
            for r in store.conn.execute(
                "SELECT DISTINCT decision_work_id FROM legal_provision_citations WHERE namespace=? "
                "AND list_contains(?, statute_key)",
                [namespace, sorted(statute_keys)],
            ).fetchall()
        }
    for abbreviation in sorted(new_statutes):
        # A newly acquired statute resolves citations that were unresolved before: re-link every decision
        # whose text or norm metadata mentions its abbreviation (the parser needs that text to match).
        decisions |= {
            r[0]
            for r in store.conn.execute(
                "SELECT DISTINCT v.work_id FROM legal_versions v JOIN legal_works w ON w.work_id=v.work_id "
                "LEFT JOIN legal_passages p ON p.version_id=v.version_id WHERE v.namespace=? "
                "AND w.provider='german-courts' AND (strpos(v.record_json, ?)>0 OR strpos(p.text, ?)>0)",
                [namespace, abbreviation, abbreviation],
            ).fetchall()
        }
    if decisions:
        FederalStatutes(store).rebuild_decision_citations(namespace, work_ids=decisions)


def _resolve_amendment_targets(store: LegalStore, namespace: str) -> None:
    store.conn.execute(
        "UPDATE legal_amendments SET target_work_id=w.work_id FROM legal_works w WHERE legal_amendments.namespace=? "
        "AND w.namespace=legal_amendments.namespace AND w.work_kind='statute' "
        "AND w.native_id='statute:' || legal_amendments.statute_key AND legal_amendments.target_work_id IS NULL",
        [namespace],
    )


def _implementation_links(
    store: LegalStore, namespace: str, version_ids: set[str] | None
) -> None:
    """Record implementation statements of the given statute/act versions (all when ``None``) and resolve."""
    rows = (
        []
        if version_ids is not None and not version_ids
        else store.conn.execute(
            "SELECT v.work_id, v.version_id, p.ordinal, p.locator_json, p.text FROM legal_versions v "
            "JOIN legal_works w ON w.work_id=v.work_id JOIN legal_passages p ON p.version_id=v.version_id "
            "WHERE v.namespace=? AND w.work_kind IN ('statute','amendment_act') "
            + ("" if version_ids is None else "AND list_contains(?, v.version_id) ")
            + "ORDER BY v.version_id, p.ordinal",
            [namespace] + ([] if version_ids is None else [sorted(version_ids)]),
        ).fetchall()
    )
    for work_id, version_id, ordinal, locator_json, text in rows:
        for statement in implementation_statements(text):
            locator = {
                **_load(locator_json, {}),
                "passage_ordinal": ordinal,
                "start": statement["start"],
                "end": statement["end"],
            }
            link_id = (
                "legal-implements:"
                + _digest(
                    [version_id, ordinal, statement["start"], statement["celex"]]
                )[:24]
            )
            store.conn.execute(
                "INSERT OR IGNORE INTO legal_implementation_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    link_id,
                    namespace,
                    work_id,
                    version_id,
                    statement["celex"],
                    statement.get("eli"),
                    statement["raw"],
                    None,
                    statement["statement"],
                    _canonical(locator),
                ],
            )
    # Resolve to CELLAR works by exact CELEX or ELI only.
    works = store.conn.execute(
        "SELECT work_id, identifiers_json FROM legal_works WHERE namespace=? AND provider='cellar'",
        [namespace],
    ).fetchall()
    by_celex, by_eli = {}, {}
    for work_id, identifiers_json in works:
        identifiers = _load(identifiers_json, {})
        if identifiers.get("celex"):
            by_celex.setdefault(str(identifiers["celex"]).upper(), work_id)
        for eli in identifiers.get("eli") or []:
            by_eli.setdefault(
                str(eli).split("data.europa.eu/")[-1].rstrip("/"), work_id
            )
    for link_id, celex, eli in store.conn.execute(
        "SELECT link_id, celex, eli FROM legal_implementation_links WHERE namespace=? AND target_work_id IS NULL",
        [namespace],
    ).fetchall():
        target = by_celex.get(str(celex).upper()) or (
            by_eli.get(str(eli).rstrip("/")) if eli else None
        )
        if target:
            store.conn.execute(
                "UPDATE legal_implementation_links SET target_work_id=? WHERE link_id=?",
                [target, link_id],
            )


# ----------------------------------------------------------------------- reads


class FederalStatutes:
    def __init__(self, store: LegalStore | Any) -> None:
        self.store = (
            store
            if isinstance(store, LegalStore)
            else LegalStore(store, initialize=False)
        )
        self.conn = self.store.conn

    # ---------------------------------------------------------------- helpers

    def ready(self) -> bool:
        try:
            tables = {
                r[0]
                for r in self.conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                    "('legal_works','legal_versions','legal_passages','legal_statute_versions',"
                    "'legal_statute_observations','legal_amendments','legal_provision_citations',"
                    "'legal_implementation_links')"
                ).fetchall()
            }
        except Exception:  # noqa: BLE001
            return False
        return len(tables) == 8

    def _require_ready(self) -> None:
        if not self.ready():
            raise LegalError(
                "not_ready", "no federal statute source has been acquired yet"
            )

    def registry_extra(self, namespace: str) -> list[dict[str, Any]]:
        """Statutes acquired in this namespace (their abbreviations resolve citations beside the FL01 set)."""
        rows = self.conn.execute(
            "SELECT identifiers_json, title FROM legal_works WHERE namespace=? AND "
            "work_kind='statute'",
            [namespace],
        ).fetchall()
        return [
            {"jurabk": _load(i, {}).get("jurabk"), "title": t}
            for i, t in rows
            if _load(i, {}).get("jurabk")
        ]

    def statute_work(self, namespace: str, statute: str) -> dict[str, Any]:
        text = str(statute or "").strip()
        if text.startswith("legal-work:"):
            work = self.store._work_row(namespace, text)
        else:
            row = self.conn.execute(
                "SELECT work_id FROM legal_works WHERE namespace=? AND work_kind='statute' "
                "AND native_id=?",
                [namespace, f"statute:{statute_key(text)}"],
            ).fetchone()
            if row is None:
                raise LegalError(
                    "statute_not_acquired",
                    "no version of this statute is on record in the namespace",
                    statute=text,
                )
            work = self.store._work_row(namespace, row[0])
        if work["work_kind"] != "statute":
            raise LegalError("not_a_statute", "the work is not a federal statute")
        return work

    def versions(self, namespace: str, work_id: str) -> list[dict[str, Any]]:
        """Every statute version with its basis, stated validity, sightings and whether it is current per source."""
        rows = self.conn.execute(
            "SELECT version_id, provider, source_id, native_id, validity_basis, validity_from, validity_to, "
            "temporal_coverage, source_modified, stand_json, amended_by_json, text_sha256, first_observed_at_ms, "
            "corrects_version_id FROM legal_statute_versions WHERE namespace=? AND work_id=? ORDER BY version_id",
            [namespace, work_id],
        ).fetchall()
        sightings: dict[str, list[int]] = {}
        for version_id, at in self.conn.execute(
            "SELECT o.version_id, o.observed_at_ms FROM legal_statute_observations o JOIN legal_statute_versions s "
            "ON s.version_id=o.version_id WHERE s.namespace=? AND s.work_id=? ORDER BY o.observed_at_ms",
            [namespace, work_id],
        ).fetchall():
            sightings.setdefault(version_id, []).append(int(at))
        output = []
        for row in rows:
            item = dict(
                zip(
                    (
                        "version_id",
                        "provider",
                        "source_id",
                        "native_id",
                        "validity_basis",
                        "validity_from",
                        "validity_to",
                        "temporal_coverage",
                        "source_modified",
                        "stand",
                        "amended_by",
                        "text_sha256",
                        "first_observed_at_ms",
                        "corrects_version_id",
                    ),
                    row,
                )
            )
            item.update(
                {
                    "validity_from": _iso(item["validity_from"]),
                    "validity_to": _iso(item["validity_to"]),
                    "source_modified": _iso(item["source_modified"]),
                    "stand": _safe_load(item["stand"], []),
                    "amended_by": _safe_load(item["amended_by"], []),
                    "sightings_ms": sightings.get(item["version_id"], []),
                }
            )
            item["observed_on"] = [
                _ms_day(ms).isoformat() for ms in item["sightings_ms"]
            ]
            output.append(item)
        # Current per source expression: the source's own modification date where both sides state one,
        # otherwise observation order (the same rule projection uses).
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in output:
            if item["validity_basis"] == STATED:
                grouped.setdefault(item["native_id"], []).append(item)
        current = {
            chosen
            for rows in grouped.values()
            if (
                chosen := current_stated(
                    (
                        r["version_id"],
                        r["source_modified"],
                        r["first_observed_at_ms"],
                    )
                    for r in rows
                )
            )
        }
        for item in output:
            item["current"] = (
                item["validity_basis"] == OBSERVED or item["version_id"] in current
            )
        return output

    def passages_of(self, version_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ordinal, locator_json, text FROM legal_passages WHERE version_id=? "
            "ORDER BY ordinal",
            [version_id],
        ).fetchall()
        return [
            {"ordinal": r[0], "locator": _load(r[1], {}), "text": r[2]} for r in rows
        ]

    def provision_passages(
        self, version_id: str, provision: str
    ) -> tuple[list[dict[str, Any]], str]:
        """Passages of ``provision`` (and its sub-units); else the narrowest stored unit containing it."""
        passages = [
            p
            for p in self.passages_of(version_id)
            if p["locator"].get("kind") in {"statute-provision", "statute-footnote"}
        ]
        inside = [
            p
            for p in passages
            if path_contains(provision, str(p["locator"].get("path") or ""))
        ]
        if inside:
            return inside, "provision"
        containing = [
            p
            for p in passages
            if path_contains(str(p["locator"].get("path") or ""), provision)
        ]
        if containing:
            depth = max(len(str(p["locator"]["path"]).split("/")) for p in containing)
            return [
                p
                for p in containing
                if len(str(p["locator"]["path"]).split("/")) == depth
            ], "containing_unit"
        return [], "absent"

    @staticmethod
    def _provision_sha(passages: Iterable[Mapping[str, Any]]) -> str | None:
        """A source-independent fingerprint: provision texts keyed by their canonical path only.

        Headings, footnotes and source-specific locator fields (document
        numbers, element ids) are left out, so two sources stating the same
        provisions compare equal.
        """
        texts = sorted(
            (str(p["locator"].get("path")), p["text"])
            for p in passages
            if p["locator"].get("kind") == "statute-provision"
        )
        return _digest(texts) if texts else None

    def content_sha(self, version_id: str) -> str | None:
        """The whole statute's source-independent fingerprint (every provision text by canonical path)."""
        return self._provision_sha(self.passages_of(version_id))

    def _view(
        self, version: Mapping[str, Any], provision: str | None, day: date | None = None
    ) -> dict[str, Any]:
        view = {
            "version_id": version["version_id"],
            "validity_basis": version["validity_basis"],
            "provider": version["provider"],
            "source_id": version["source_id"],
            "native_id": version["native_id"],
            "text_sha256": version["text_sha256"],
            "content_sha256": self.content_sha(version["version_id"]),
            "amended_by": version["amended_by"],
        }
        if version["validity_basis"] == STATED:
            view.update(
                {
                    "validity_from": version["validity_from"],
                    "validity_to": version["validity_to"],
                    "temporal_coverage": version["temporal_coverage"],
                    "label": f"source states validity from {version['validity_from']}"
                    + (
                        f" to {version['validity_to']}"
                        if version["validity_to"]
                        else " (no end stated)"
                    ),
                    "corrects_version_id": version["corrects_version_id"],
                }
            )
        else:
            seen = [
                d
                for d in (_d(x) for x in version["observed_on"])
                if day is None or d <= day
            ]
            on = (
                max(seen).isoformat() if seen else (version["observed_on"] or [None])[0]
            )
            view.update(
                {
                    "observed_on": on,
                    "sightings": version["observed_on"],
                    "label": f"observed on {on}, validity not stated",
                    "stand": version["stand"],
                }
            )
        if provision is not None:
            passages, match = self.provision_passages(version["version_id"], provision)
            view.update(
                {
                    "provision_match": match,
                    "passages": passages,
                    "provision_sha256": self._provision_sha(passages),
                }
            )
        return view

    # ------------------------------------------------------------ as-of

    def select(
        self, namespace: str, work_id: str, provision: str | None, day: date
    ) -> dict[str, Any]:
        """Source-stated version covering ``day``; else the nearest observed version on or before it; else none."""
        versions = [v for v in self.versions(namespace, work_id) if v["current"]]
        stated = [
            v
            for v in versions
            if v["validity_basis"] == STATED
            and _d(v["validity_from"])
            and _d(v["validity_from"]) <= day
            and (not _d(v["validity_to"]) or day <= _d(v["validity_to"]))
        ]
        observed = []
        for version in versions:
            if version["validity_basis"] == OBSERVED:
                seen = [ms for ms in version["sightings_ms"] if _ms_day(ms) <= day]
                if seen:
                    observed.append((max(seen), version))
        nearest = (
            max(observed, key=lambda x: (x[0], x[1]["version_id"]))[1]
            if observed
            else None
        )
        undated = [
            v["version_id"]
            for v in versions
            if v["validity_basis"] == STATED and not _d(v["validity_from"])
        ]
        if stated:
            views = [
                self._view(v, provision, day)
                for v in sorted(stated, key=lambda v: v["validity_from"])
            ]

            def fingerprint(view: Mapping[str, Any]) -> str | None:
                # Source-independent: provision texts by canonical path, never a source's own file hash.
                return view["provision_sha256"] if provision else view["content_sha256"]

            conflicts = list(views) if len({fingerprint(v) for v in views}) > 1 else []
            corroborating: list[dict[str, Any]] = []
            if nearest is not None:
                observed_view = self._view(nearest, provision, day)
                seen_on = _d(observed_view["observed_on"])
                inside = [
                    v for v in views if seen_on and seen_on >= _d(v["validity_from"])
                ]
                # An observation inside a stated interval with different text is a conflict, never resolved.
                if any(fingerprint(v) != fingerprint(observed_view) for v in inside):
                    conflicts = [*views, observed_view]
                elif inside:
                    corroborating = [observed_view]
            if conflicts:
                return {
                    "status": "conflict",
                    "selected": None,
                    "candidates": conflicts,
                    "label": "sources differ for this date; both are shown, neither is preferred",
                    "undated_source_stated": undated,
                }
            selected = views[0]
            status = (
                "provision_not_in_version"
                if provision and selected["provision_match"] == "absent"
                else STATED
            )
            return {
                "status": status,
                "selected": selected,
                "candidates": views,
                "label": selected["label"],
                # An observation inside the stated interval with the same text: both sources are cited.
                "corroborated_by": corroborating,
                "sources": sorted(
                    {v["source_id"] or v["provider"] for v in [*views, *corroborating]}
                ),
                "undated_source_stated": undated,
            }
        if nearest is not None:
            view = self._view(nearest, provision, day)
            status = (
                "provision_not_in_version"
                if provision and view["provision_match"] == "absent"
                else OBSERVED
            )
            return {
                "status": status,
                "selected": view,
                "candidates": [view],
                "label": view["label"],
                "undated_source_stated": undated,
            }
        return {
            "status": "no_version_on_record",
            "selected": None,
            "candidates": [],
            "label": "no version on record for this date",
            "undated_source_stated": undated,
        }

    def provision_as_of(
        self, namespace: str, statute: str, provision: str, as_of: str, *, scopes
    ) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        self._require_ready()
        day = _parse_day(as_of)
        path, citation = self._provision(namespace, statute, provision)
        work = self.statute_work(
            namespace, citation["statute"] if citation else statute
        )
        chosen = self.select(namespace, work["work_id"], path, day)
        return {
            "contract": PROVISION_CONTRACT,
            "namespace": namespace,
            "statute": work["identifiers"].get("jurabk"),
            "work_id": work["work_id"],
            "provision": path,
            "as_of": day.isoformat(),
            **({"parsed_citation": citation} if citation else {}),
            **chosen,
            "amendment_acts": self.amendment_acts_for(namespace, work["work_id"], path),
            "semantics": SEMANTICS,
            "review_boundary": REVIEW_BOUNDARY,
        }

    def statute_as_of(self, namespace: str, work_id: str, day: str) -> dict[str, Any]:
        """The Legal ``select_as_of`` answer for a statute work (whole statute, same evidence rules)."""
        self._require_ready()
        chosen = self.select(namespace, work_id, None, _parse_day(day))
        return {
            "contract": PROVISION_CONTRACT,
            "namespace": namespace,
            "work_id": work_id,
            "as_of": day,
            "provision": None,
            **chosen,
            "selected_version_id": (chosen["selected"] or {}).get("version_id"),
            "semantics": SEMANTICS,
            "review_boundary": REVIEW_BOUNDARY,
        }

    def _provision(
        self, namespace: str, statute: str | None, provision: str
    ) -> tuple[str, dict[str, Any] | None]:
        """A provision path (``§5/abs2``) or a citation (``§ 5 Abs. 2 MPHG``) as a path and its parsed citation."""
        text = str(provision or "").strip()
        if not text:
            raise LegalError(
                "invalid_request", "name a provision (e.g. §5/abs2 or '§ 5 Abs. 2')"
            )
        if "/" in text or (text.startswith(("§", "art")) and " " not in text):
            return text.casefold() if text.startswith("art") else text, None
        citations = parse_citations(
            text,
            statutes=self.registry_extra(namespace),
            context_statute=statute or None,
        )
        if len(citations) != 1 or len(citations[0]["provisions"]) != 1:
            raise LegalError(
                "invalid_request",
                "the provision must be exactly one statutory citation",
                parsed=citations,
            )
        citation = citations[0]
        if citation["status"] != "resolved":
            raise LegalError(
                "unresolved_citation",
                "the citation's statute is not in the statute set",
                parsed=citation,
            )
        if (
            statute
            and statute_key(statute) != citation["statute_key"]
            and not str(statute).startswith("legal-work:")
        ):
            raise LegalError(
                "statute_mismatch",
                "the citation names a different statute",
                parsed=citation,
            )
        return citation["provisions"][0]["path"], citation

    # ---------------------------------------------------------- comparison

    def compare_provision(
        self,
        namespace: str,
        statute: str,
        provision: str,
        left: str,
        right: str,
        *,
        scopes,
    ) -> dict[str, Any]:
        """Provision-level diff between two versions or two dates, with source-linked amendment acts."""
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        self._require_ready()
        path, _ = self._provision(namespace, statute, provision)
        work = self.statute_work(namespace, statute)
        versions = {
            v["version_id"]: v for v in self.versions(namespace, work["work_id"])
        }
        sides = []
        for ref in (left, right):
            if str(ref).startswith("legal-version:"):
                if ref not in versions:
                    raise LegalError(
                        "not_found",
                        "version is not a version of this statute",
                        version_id=ref,
                    )
                sides.append(
                    {
                        "ref": ref,
                        "view": self._view(versions[ref], path),
                        "selection": None,
                    }
                )
                continue
            chosen = self.select(namespace, work["work_id"], path, _parse_day(ref))
            if chosen["selected"] is None:
                raise LegalError(
                    "no_single_version",
                    f"no single version for {ref}: {chosen['status']}",
                    selection=chosen,
                )
            sides.append(
                {"ref": ref, "view": chosen["selected"], "selection": chosen["status"]}
            )
        before = {p["locator"].get("path"): p for p in sides[0]["view"]["passages"]}
        after = {p["locator"].get("path"): p for p in sides[1]["view"]["passages"]}
        changes = []
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key), after.get(key)
            if old and new and old["text"] == new["text"]:
                continue
            changes.append(
                {
                    "locator": (new or old)["locator"],
                    "change": "added"
                    if old is None
                    else "removed"
                    if new is None
                    else "changed",
                    "before": old["text"] if old else None,
                    "after": new["text"] if new else None,
                }
            )
        # Acts the later version itself names ("Stand" note or stated amendment references) and the earlier one
        # does not: the source's own link between the change and an amendment act.
        stated_refs = sorted(
            set(sides[1]["view"]["amended_by"]) - set(sides[0]["view"]["amended_by"])
        )
        known = {a["bgbl_key"]: a for a in self._acts(namespace).values()}
        acts = []
        for ref in stated_refs:
            act = known.get(ref)
            if act is None:
                year = (
                    int(ref.split("/")[1])
                    if ref.count("/") >= 2 and ref.split("/")[1].isdigit()
                    else None
                )
                acts.append(
                    {
                        "bgbl_key": ref,
                        "status": "not_acquired",
                        "reason": "historical Federal Law Gazette issue (link-only)"
                        if year and year < 2023
                        else "the promulgation is not acquired",
                    }
                )
                continue
            instructions = [
                a
                for a in self._amendments(act["version_id"])
                if a["target_work_id"] == work["work_id"]
            ]
            acts.append(
                {
                    **act,
                    "status": "acquired",
                    "instructions": instructions,
                    "instruction_targets_provision": any(
                        a["provision"]
                        and (
                            path_contains(path, a["provision"])
                            or path_contains(a["provision"], path)
                        )
                        for a in instructions
                    ),
                }
            )
        return {
            "contract": PROVISION_COMPARISON_CONTRACT,
            "namespace": namespace,
            "work_id": work["work_id"],
            "statute": work["identifiers"].get("jurabk"),
            "provision": path,
            "left": {
                "ref": sides[0]["ref"],
                "selection": sides[0]["selection"],
                **{k: v for k, v in sides[0]["view"].items() if k != "passages"},
            },
            "right": {
                "ref": sides[1]["ref"],
                "selection": sides[1]["selection"],
                **{k: v for k, v in sides[1]["view"].items() if k != "passages"},
            },
            "changes": changes,
            "unchanged_passages": len(set(before) & set(after))
            - sum(1 for c in changes if c["change"] == "changed"),
            "amendment_acts_stated_by_the_source": acts,
            "notice": "Passage differences are source changes, not a legal-effect assessment; amendment acts are "
            "listed only where the later version itself names them.",
            "semantics": SEMANTICS,
        }

    # ---------------------------------------------------------- amendment acts

    def _acts(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Current version of each amendment act (latest acquired), with its facts and entry-into-force text."""
        rows = self.conn.execute(
            "SELECT w.work_id, w.native_id, w.identifiers_json, w.title, v.version_id, v.record_json, "
            "v.observed_at_ms FROM legal_works w JOIN legal_versions v ON v.work_id=w.work_id WHERE w.namespace=? "
            "AND w.work_kind='amendment_act' ORDER BY w.work_id, v.observed_at_ms, v.version_id",
            [namespace],
        ).fetchall()
        acts: dict[str, dict[str, Any]] = {}
        for (
            work_id,
            native_id,
            identifiers_json,
            title,
            version_id,
            record_json,
            _,
        ) in rows:
            fields = dict(dict(_safe_load(record_json, {})).get("fields") or {})
            acts[work_id] = {
                "work_id": work_id,
                "bgbl_key": native_id,
                "bgbl_citation": dict(_safe_load(identifiers_json, {})).get(
                    "bgbl_citation"
                ),
                "title": title,
                "version_id": version_id,
                "eli": fields.get("eli"),
                "promulgation_date": fields.get("promulgation_date"),
                "ausfertigung_date": fields.get("ausfertigung_date"),
                "entry_into_force": fields.get("entry_into_force") or [],
                "touched_statutes": fields.get("touched_statutes") or [],
            }
        return acts

    def _amendments(self, version_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT amendment_id, article, item, statute_key, target_work_id, provision, action, status, instruction, "
            "locator_json FROM legal_amendments WHERE act_version_id=? ORDER BY ordinal",
            [version_id],
        ).fetchall()
        return [
            {
                **dict(
                    zip(
                        (
                            "amendment_id",
                            "article",
                            "item",
                            "statute_key",
                            "target_work_id",
                            "provision",
                            "action",
                            "status",
                            "instruction",
                        ),
                        r[:9],
                    )
                ),
                "locator": _load(r[9], {}),
            }
            for r in rows
        ]

    def amendment_acts_for(
        self, namespace: str, work_id: str | None, provision: str | None = None
    ) -> list[dict[str, Any]]:
        output = []
        for act in self._acts(namespace).values():
            amendments = self._amendments(act["version_id"])
            relevant = [
                a
                for a in amendments
                if (work_id is None or a["target_work_id"] == work_id)
            ]
            if provision is not None:
                relevant = [
                    a
                    for a in relevant
                    if a["provision"]
                    and (
                        path_contains(provision, a["provision"])
                        or path_contains(a["provision"], provision)
                    )
                ]
            if not relevant:
                continue
            output.append(
                {
                    **act,
                    "instructions": relevant,
                    "dossier_links": self._dossier_links(namespace, act["work_id"]),
                    "implements": self.implementation_links(namespace, act["work_id"]),
                }
            )
        return sorted(
            output, key=lambda a: (a["promulgation_date"] or "", a["bgbl_key"])
        )

    def list_amendment_acts(
        self,
        namespace: str,
        *,
        scopes,
        statute: str | None = None,
        provision: str | None = None,
    ) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        self._require_ready()
        work_id, path = None, None
        if statute:
            work_id = self.statute_work(namespace, statute)["work_id"]
            if provision:
                path, _ = self._provision(namespace, statute, provision)
        acts = (
            self.amendment_acts_for(namespace, work_id, path)
            if statute
            else [
                {
                    **act,
                    "instructions": self._amendments(act["version_id"]),
                    "dossier_links": self._dossier_links(namespace, act["work_id"]),
                    "implements": self.implementation_links(namespace, act["work_id"]),
                }
                for act in sorted(
                    self._acts(namespace).values(), key=lambda a: a["bgbl_key"]
                )
            ]
        )
        return {
            "namespace": namespace,
            "statute": statute,
            "provision": path,
            "acts": acts,
            "notice": "Amending instructions are the act's own text; they are never applied to produce a "
            "consolidated version. Unresolved or ambiguous instructions keep their text and locator.",
            "coverage_notice": "Only acts acquired from the digital Federal Law Gazette (since 2023) that touch "
            "the bounded statute set are listed; earlier gazette issues are link-only.",
        }

    def _dossier_links(self, namespace: str, work_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT dossier_id, relation, evidence, principal_id FROM legal_dossier_links WHERE "
            "namespace=? AND work_id=? ORDER BY dossier_id",
            [namespace, work_id],
        ).fetchall()
        outcome = None
        try:
            outcome = self.conn.execute(
                "SELECT status, reason FROM legal_dossier_match_outcomes WHERE namespace=? "
                "AND act_work_id=?",
                [namespace, work_id],
            ).fetchone()
        except Exception:  # noqa: BLE001 - older stores
            outcome = None
        links = [
            dict(zip(("dossier_id", "relation", "evidence", "principal_id"), r))
            for r in rows
        ]
        if not links and outcome:
            return [{"dossier_id": None, "status": outcome[0], "reason": outcome[1]}]
        return links

    def implementation_links(
        self, namespace: str, work_id: str
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT link_id, version_id, celex, eli, raw, target_work_id, statement, locator_json FROM "
            "legal_implementation_links WHERE namespace=? AND work_id=? ORDER BY link_id",
            [namespace, work_id],
        ).fetchall()
        return [
            {
                **dict(
                    zip(
                        (
                            "link_id",
                            "version_id",
                            "celex",
                            "eli",
                            "raw",
                            "target_work_id",
                            "statement",
                        ),
                        r[:7],
                    )
                ),
                "relation": "implements",
                "status": "resolved" if r[5] else "unresolved",
                "locator": _load(r[7], {}),
            }
            for r in rows
        ]

    # ------------------------------------------------------ court decisions

    def rebuild_decision_citations(
        self, namespace: str, *, work_ids: Iterable[str] | None = None
    ) -> int:
        """Parse acquired court decisions for statutory citations and select the provision version per date.

        ``work_ids`` limits the rebuild to those decisions (their rows are
        replaced); without it every decision in the namespace is rebuilt.
        """
        only = None if work_ids is None else set(work_ids)
        if only is not None and not only:
            return 0
        registry = self.registry_extra(namespace)
        statutes = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT native_id, work_id FROM legal_works WHERE namespace=? AND work_kind='statute'",
                [namespace],
            ).fetchall()
        }
        decisions = self.conn.execute(
            "SELECT w.work_id, w.issuing_body, v.version_id, v.record_json FROM legal_works w JOIN legal_versions v "
            "ON v.work_id=w.work_id WHERE w.namespace=? AND w.provider='german-courts' ORDER BY w.work_id, "
            "v.observed_at_ms, v.version_id",
            [namespace],
        ).fetchall()
        latest = {}
        for work_id, court, version_id, record_json in decisions:
            if only is None or work_id in only:
                latest[work_id] = (court, version_id, _safe_load(record_json, {}))
        if only is None:
            self.conn.execute(
                "DELETE FROM legal_provision_citations WHERE namespace=?", [namespace]
            )
        else:
            self.conn.execute(
                "DELETE FROM legal_provision_citations WHERE namespace=? AND list_contains(?, decision_work_id)",
                [namespace, sorted(only)],
            )
        count = 0
        for work_id, (court, version_id, record) in latest.items():
            fields = dict(record.get("fields") or {})
            decided = _d(fields.get("decision_date"))
            texts = [
                (
                    {"kind": "court-metadata-norm", "field": "norm"},
                    str(fields.get("cited_norms") or ""),
                )
            ]
            texts += [
                (p["locator"] | {"passage_ordinal": p["ordinal"]}, p["text"])
                for p in self.passages_of(version_id)
            ]
            for locator, text in texts:
                for citation in parse_citations(text, statutes=registry):
                    if citation["status"] != "resolved":
                        continue
                    for provision in citation["provisions"]:
                        target = statutes.get(f"statute:{citation['statute_key']}")
                        selection = self._decision_selection(
                            namespace, target, provision["path"], decided, citation
                        )
                        citation_id = (
                            "legal-provision-citation:"
                            + _digest(
                                [
                                    version_id,
                                    locator,
                                    citation["start"],
                                    provision["path"],
                                ]
                            )[:24]
                        )
                        self.conn.execute(
                            "INSERT OR IGNORE INTO legal_provision_citations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [
                                citation_id,
                                namespace,
                                work_id,
                                version_id,
                                court,
                                _iso(decided),
                                citation["statute_key"],
                                provision["path"],
                                citation["raw"],
                                _canonical(
                                    {
                                        **locator,
                                        "start": citation["start"],
                                        "end": citation["end"],
                                        "shape": provision["shape"],
                                        "to_path": provision.get("to_path"),
                                    }
                                ),
                                citation["version_hint"],
                                target,
                                selection.get("version_id"),
                                _canonical(selection),
                            ],
                        )
                        count += 1
        return count

    def _decision_selection(
        self,
        namespace: str,
        work_id: str | None,
        path: str,
        decided: date | None,
        citation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if work_id is None:
            return {"status": "statute_not_acquired", "version_id": None}
        if decided is None:
            return {"status": "decision_date_unknown", "version_id": None}
        hint_day = _d(citation.get("version_hint_date"))
        chosen = self.select(namespace, work_id, path, hint_day or decided)
        selected = chosen["selected"]
        base = {
            "status": chosen["status"],
            "label": chosen["label"],
            "version_id": selected["version_id"] if selected else None,
            "validity_basis": selected["validity_basis"] if selected else None,
            "selected_for": (hint_day or decided).isoformat(),
            "candidates": [c["version_id"] for c in chosen["candidates"]],
        }
        if citation.get("version_hint") != "a.F." or hint_day:
            return base
        if selected is None:
            return {
                **base,
                "status": "a_f_undetermined",
                "reason": "no version on the decision date to precede",
            }
        earlier = self._earlier_version(namespace, work_id, path, selected)
        if earlier is None:
            return {
                **base,
                "version_id": None,
                "status": "a_f_undetermined",
                "reason": "no earlier version with different provision text is on record",
                "decision_date_version_id": selected["version_id"],
            }
        return {
            **base,
            "status": "a_f_earlier_version",
            "version_id": earlier["version_id"],
            "validity_basis": earlier["validity_basis"],
            "label": earlier["label"],
            "decision_date_version_id": selected["version_id"],
            "reason": "a.F.: the latest earlier version whose provision text differs from the version on the "
            "decision date",
        }

    def _earlier_version(
        self, namespace: str, work_id: str, path: str, selected: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        def start(version: Mapping[str, Any]) -> date | None:
            if version["validity_basis"] == STATED:
                return _d(version["validity_from"])
            days = [_d(x) for x in version["observed_on"]]
            return min(days) if days else None

        versions = [v for v in self.versions(namespace, work_id) if v["current"]]
        by_id = {v["version_id"]: v for v in versions}
        anchor = (
            start(by_id[selected["version_id"]])
            if selected["version_id"] in by_id
            else None
        )
        if anchor is None:
            return None
        earlier = sorted(
            (v for v in versions if start(v) and start(v) < anchor),
            key=lambda v: (start(v), v["version_id"]),
        )
        for version in reversed(earlier):
            view = self._view(version, path)
            if view["provision_sha256"] and view["provision_sha256"] != selected.get(
                "provision_sha256"
            ):
                # Among earlier versions with that same provision text, a source-stated one is the stronger evidence.
                for other in reversed(earlier):
                    if other["validity_basis"] == STATED:
                        stated = self._view(other, path)
                        if stated["provision_sha256"] == view["provision_sha256"]:
                            return stated
                return view
        return None

    def decisions_citing(
        self,
        namespace: str,
        statute: str,
        provision: str | None = None,
        *,
        scopes,
        court: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Decisions whose text or norm metadata explicitly cite the provision (or any provision of the statute)."""
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        self._require_ready()
        key = statute_key(statute)
        path = None
        if provision:
            path, citation = self._provision(namespace, statute, provision)
            key = citation["statute_key"] if citation else key
        start, end = (
            _parse_day(date_from) if date_from else None,
            _parse_day(date_to) if date_to else None,
        )
        rows = self.conn.execute(
            "SELECT citation_id, decision_work_id, decision_version_id, court, decision_date, provision, raw, "
            "locator_json, version_hint, target_work_id, selected_version_id, selection_json FROM "
            "legal_provision_citations WHERE namespace=? AND statute_key=? ORDER BY decision_date, citation_id",
            [namespace, key],
        ).fetchall()
        output = []
        for row in rows:
            item = dict(
                zip(
                    (
                        "citation_id",
                        "decision_work_id",
                        "decision_version_id",
                        "court",
                        "decision_date",
                        "provision",
                        "raw",
                        "locator",
                        "version_hint",
                        "target_work_id",
                        "selected_version_id",
                        "selection",
                    ),
                    row,
                )
            )
            item.update(
                {
                    "decision_date": _iso(item["decision_date"]),
                    "locator": _load(item["locator"], {}),
                    "selection": _load(item["selection"], {}),
                }
            )
            if path and not (
                path_contains(path, item["provision"])
                or path_contains(item["provision"], path)
            ):
                continue
            if court and court.casefold() not in str(item["court"] or "").casefold():
                continue
            day = _d(item["decision_date"])
            if (start and (day is None or day < start)) or (
                end and (day is None or day > end)
            ):
                continue
            output.append(item)
        decisions = {}
        for item in output:
            decisions.setdefault(item["decision_work_id"], []).append(item)
        return {
            "namespace": namespace,
            "statute": statute,
            "statute_key": key,
            "provision": path,
            "decisions": [
                {"work_id": w, **self.store._work_row(namespace, w), "citations": c}
                for w, c in decisions.items()
            ],
            "notice": "Explicit citations in the decision text or its norm metadata only; no topic similarity.",
            "semantics": SEMANTICS,
        }

    # ------------------------------------------------------------ dossiers

    def link_amendment_dossiers(
        self,
        namespace: str,
        *,
        scopes,
        principal_id: str,
        dossier_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Link acts to Bundestag DIP dossiers whose stages state the act's BGBl citation (exact key match)."""
        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if DOSSIER_READ_SCOPE not in set(scopes) and "operator" not in set(scopes):
            raise LegalError(
                "unauthorized", f"{DOSSIER_READ_SCOPE} is required to read dossiers"
            )
        self._require_ready()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS legal_dossier_match_outcomes (namespace TEXT NOT NULL, "
            "act_work_id TEXT NOT NULL, bgbl_key TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT "
            "NULL, detail_json TEXT NOT NULL, evaluated_at_ms BIGINT NOT NULL, "
            "PRIMARY KEY(namespace, act_work_id))"
        )
        dossier_ns = dossier_namespace or namespace
        try:
            rows = self.conn.execute(
                "SELECT d.dossier_id, r.content_json FROM legislative_dossiers d JOIN legislative_dossier_revisions r "
                "ON r.dossier_id=d.dossier_id AND r.revision=d.revision WHERE d.namespace=? ORDER BY d.dossier_id",
                [dossier_ns],
            ).fetchall()
        except Exception:  # noqa: BLE001 - the dossier store is not initialised
            rows = []
        stated: dict[str, list[dict[str, Any]]] = {}
        for dossier_id, content in rows:
            state = _load(content, {})
            if str(state.get("jurisdiction") or "").upper() != "DE":
                continue
            for stage in [*state.get("stages", [])]:
                texts = [
                    ("instrument_id", stage.get("raw_instrument_id")),
                    ("title", stage.get("title")),
                ]
                texts += [
                    ("legal_event", e.get("source_quote"))
                    for e in stage.get("legal_events") or []
                ]
                for field, text in texts:
                    from src.ingestion.federal_law_formats import bgbl_key_from_eli

                    keys = {
                        r["key"] for r in parse_bgbl_references(text or "") if r["key"]
                    }
                    keys |= {k for k in [bgbl_key_from_eli(text or "")] if k}
                    for key in keys:
                        stated.setdefault(key, []).append(
                            {
                                "dossier_id": dossier_id,
                                "stage_id": stage.get("stage_id"),
                                "field": field,
                                "quote": text,
                                "citation": stage.get("citation"),
                            }
                        )
        linked, unlinked = [], []
        for act in self._acts(namespace).values():
            matches = stated.get(act["bgbl_key"], [])
            if not matches:
                reason = (
                    "no Bundestag DIP dossier stage in the namespace states this BGBl citation"
                    if rows
                    else "no legislative dossier is on record in the namespace"
                )
                self._match_outcome(namespace, act, "unlinked", reason, {})
                unlinked.append(
                    {
                        "act_work_id": act["work_id"],
                        "bgbl_key": act["bgbl_key"],
                        "reason": reason,
                    }
                )
                continue
            dossiers = sorted({m["dossier_id"] for m in matches})
            for dossier_id in dossiers:
                evidence = next(m for m in matches if m["dossier_id"] == dossier_id)
                text = (
                    f"DIP dossier stage {evidence['stage_id']} states {evidence['quote']!r} "
                    f"({evidence['field']}; document {dict(evidence.get('citation') or {}).get('document_id')} "
                    f"revision {dict(evidence.get('citation') or {}).get('revision_id')})"
                )
                link_id = (
                    "legal-dossier-link:"
                    + _digest([namespace, dossier_id, act["work_id"], "enacted_as"])[
                        :24
                    ]
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO legal_dossier_links VALUES (?,?,?,?,?,?,?,?)",
                    [
                        link_id,
                        namespace,
                        dossier_id,
                        act["work_id"],
                        "enacted_as",
                        text,
                        principal_id,
                        self.store.now(),
                    ],
                )
                linked.append(
                    {
                        "link_id": link_id,
                        "act_work_id": act["work_id"],
                        "bgbl_key": act["bgbl_key"],
                        "dossier_id": dossier_id,
                        "evidence": text,
                    }
                )
            status = "linked" if len(dossiers) == 1 else "linked_to_several_dossiers"
            self._match_outcome(
                namespace,
                act,
                status,
                "a dossier stage states the act's BGBl citation",
                {"dossiers": dossiers},
            )
        return {
            "namespace": namespace,
            "linked": linked,
            "unlinked": unlinked,
            "notice": "Links are exact BGBl-citation matches stated by dossier stages; titles are never compared.",
        }

    def _match_outcome(
        self,
        namespace: str,
        act: Mapping[str, Any],
        status: str,
        reason: str,
        detail: Mapping[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO legal_dossier_match_outcomes VALUES (?,?,?,?,?,?,?)",
            [
                namespace,
                act["work_id"],
                act["bgbl_key"],
                status,
                reason,
                _canonical(dict(detail)),
                self.store.now(),
            ],
        )

    # ------------------------------------------------------------ citation

    def resolve(self, namespace: str, citation: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        extra = self.registry_extra(namespace) if self.ready() else []
        result = resolve_citation(citation, statutes=extra)
        if self.ready():
            for item in result["citations"]:
                row = (
                    self.conn.execute(
                        "SELECT work_id FROM legal_works WHERE namespace=? AND work_kind='statute' AND native_id=?",
                        [namespace, f"statute:{item['statute_key']}"],
                    ).fetchone()
                    if item["statute_key"]
                    else None
                )
                item["work_id"] = row[0] if row else None
        return {"namespace": namespace, **result}


def _safe_load(value: Any, default: Any) -> Any:
    """A stored JSON value, or ``default`` for a damaged row (listings survive a bad row)."""
    try:
        return _load(value, default)
    except (TypeError, ValueError):
        return default


def _parse_day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise LegalError("invalid_request", "dates must be YYYY-MM-DD") from exc


def statute_readiness(conn: Any) -> dict[str, Any]:
    return {"ready": FederalStatutes(conn).ready(), "tables": list(TABLES)}


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Legal bundle's optional ``federal-statutes`` feature is selected (default off; reads only)."""
    del namespace  # composition selection is deployment-wide
    from src.kb.legal import legal_feature_enabled

    return legal_feature_enabled(conn, "federal-statutes")

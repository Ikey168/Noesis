"""Income, poverty and inequality releases, series, definitions, vintages and notes (#2583, IP02).

The store keeps the records :mod:`src.kb.income_distribution_records` defines. Values and their vintages are written
to the existing Economics series storage through :func:`src.domains.economic.model.register_series` (indicator,
series map, ``economic_vintages`` with release and retrieval clocks, ``dataset_observations``); the tables here hold
only the income-specific metadata the generic store has no place for. Nothing is updated in place or deleted:

* re-acquiring an unchanged file adds nothing; an unchanged SDMX re-publication adds no vintage; each PIP release
  version is its own vintage;
* changed values without a new release clock are refused (``vintage_conflict``); a release dated before a series'
  latest vintage is refused (``stale_release``);
* a PPP revision that restates past values, a definition or version change, removed periods and a series the source
  stopped publishing (``withdrawn``) are recorded as vintages with their changes; earlier vintages stay.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.income_distribution_sources import (
    EXCLUSIONS,
    LIVE_VERIFICATION,
    MINIMISATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    SOURCE_FEATURES,
)
from src.kb.income_distribution_records import (
    ACTIVE_STATES,
    ANSWER_CONTRACT,
    COMPARABILITY_CONTRACT,
    CONTRACT,
    DEFAULT_NAMESPACE,
    PROVIDER,
    READ_SCOPE,
    RELATIONS,
    REVIEW_SCOPE,
    SERIES_DOMAIN,
    SINGLE_SIDED,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    check_item,
    definition_key,
    digest,
    feature_state,
    iso_from_ms,
    load,
    release_ms,
    series_key,
    table_exists,
)

_DDL = """
CREATE TABLE IF NOT EXISTS income_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_key TEXT NOT NULL, document_json TEXT NOT NULL, published_on TEXT, published_at TEXT,
  release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_label TEXT, release_version TEXT,
  file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL, structure_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS income_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, native_key TEXT NOT NULL,
  document_key TEXT NOT NULL, dataflow_json TEXT NOT NULL, indicator_json TEXT NOT NULL, concept TEXT NOT NULL,
  welfare_concept TEXT NOT NULL, equivalence_scale_json TEXT NOT NULL, poverty_line_json TEXT, ppp_base_year TEXT,
  reference_year_basis TEXT NOT NULL, survey TEXT NOT NULL, coverage TEXT NOT NULL, methodology_version TEXT,
  definition_key TEXT NOT NULL, frequency TEXT NOT NULL, unit_json TEXT NOT NULL, unit_multiplier TEXT,
  area_scheme TEXT NOT NULL, area_code TEXT NOT NULL, area_json TEXT NOT NULL, dimensions_json TEXT NOT NULL,
  references_json TEXT NOT NULL, denominator_json TEXT, economic_indicator_id TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS income_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS income_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  economic_as_of BIGINT, release_at_ms BIGINT NOT NULL, release_at_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, definition_id TEXT NOT NULL, release_version TEXT,
  ppp_base_year TEXT, status TEXT NOT NULL, source_notes_json TEXT NOT NULL, changes_json TEXT NOT NULL,
  sequence INTEGER NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS income_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, flags_json TEXT NOT NULL, attributes_json TEXT NOT NULL, footnotes_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS income_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS income_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT, relation TEXT NOT NULL, statement TEXT NOT NULL, periods_json TEXT NOT NULL,
  cited_json TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
"""
_OBS_COMPARED = ("value_text", "status", "flags", "attributes")


def _observation_content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: o.get(k) for k in ("period", "value_text", "value", "status", "flags", "attributes", "footnotes")}
            for o in sorted(observations, key=lambda o: o["period"])]


class IncomeStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "income_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider = str(header.get("provider") or "")
        document = dict(header.get("document") or {})
        release_id = "inc-release:" + digest([namespace, provider, source_id, header["file_sha256"], document])[:24]
        if self.conn.execute("SELECT 1 FROM income_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM income_releases WHERE namespace=? AND provider=?",
            [namespace, provider]).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        structure = dict(header.get("structure") or {})
        self.conn.execute(
            "INSERT INTO income_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, release_id, provider, source_id, header["format"], header["document_key"], canonical(document),
             header.get("published_on"), header.get("published_at"),
             str(header.get("release_basis") or "retrieval_time"),
             release_ms(header.get("published_on"), header.get("published_at"), retrieved), header.get("release_label"),
             structure.get("dataflow_version"), header["file_sha256"], header.get("content_sha256") or "",
             int(header.get("item_count") or 0), canonical(structure), origin, header.get("url"), int(sequence) + 1,
             run_id, recorded_by, retrieved],
        )
        return release_id, True

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      run_id: str, source_id: str | None, retrieved_at_ms: int | None = None,
                      recorded_by: str | None = None) -> dict[str, Any]:
        """Record one publication: a vintage per series whose content, release version or definition changed, and a
        withdrawal vintage per series of the same document the release no longer states; idempotent by file."""
        from src.ingestion.income_distribution_sources import FORMATS

        fmt = header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != header.get("provider") or not header.get("document_key"):
            raise IncomeError("invalid_release", "release names a known provider, format and document")
        if int(header.get("item_count", -1)) != len(items):
            raise IncomeError("incomplete_release", "a release carries every item it states")
        for item in items:
            check_item(item)
            if item["provider"] != header["provider"]:
                raise IncomeError("invalid_release", "a release carries one provider's series")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
        if clock > retrieved:
            raise IncomeError("invalid_release", "a release cannot be dated after its retrieval")
        counts = {"series": 0, "vintages": 0, "unchanged_vintages": 0, "withdrawn": 0, "definitions": 0,
                  "source_notes": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(namespace, header, source_id=source_id, run_id=run_id,
                                                retrieved=retrieved, recorded_by=recorded_by)
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            basis = str(header.get("release_basis") or "retrieval_time")
            structure = dict(header.get("structure") or {})
            version = structure.get("dataflow_version")
            vintage_ids, seen = [], set()
            for item in items:
                series_id, new_series = self._series(namespace, item, release_id)
                if series_id in seen:
                    raise IncomeError("invalid_release", "a release states the same series twice")
                seen.add(series_id)
                counts["series"] += int(new_series)
                definition_id, new_definition = self._definition(namespace, item, release_id)
                counts["definitions"] += int(new_definition)
                vintage_id, status = self._vintage(namespace, series_id, item, header, release_id, clock, basis,
                                                   retrieved, definition_id, version, structure)
                self.conn.execute("INSERT INTO income_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, series_id, vintage_id, status])
                if status == "new":
                    vintage_ids.append(vintage_id)
                    counts["vintages"] += 1
                    counts["source_notes"] += self._source_notes(namespace, series_id, item, release_id)
                else:
                    counts["unchanged_vintages"] += 1
            for series_id in self._withdrawn(namespace, header, seen, clock):
                vintage_id = self._withdraw(namespace, series_id, release_id, clock, basis, retrieved, version)
                self.conn.execute("INSERT INTO income_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, series_id, vintage_id, "withdrawn"])
                vintage_ids.append(vintage_id)
                counts["withdrawn"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "published_on": header.get("published_on"),
                "release_basis": basis, "release_version": version, "vintage_ids": vintage_ids, **counts}

    def _series(self, namespace, item, release_id):
        series_id = "inc-series:" + digest([namespace, *series_key(item)])[:24]
        if self.conn.execute("SELECT 1 FROM income_series WHERE namespace=? AND series_id=?",
                             [namespace, series_id]).fetchone():
            return series_id, False
        self.conn.execute(
            "INSERT INTO income_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, series_id, item["provider"], str(item["native_key"]), item["document_key"],
             canonical(dict(item.get("dataflow") or {})), canonical(dict(item["indicator"])),
             item["indicator"]["concept"], item["welfare_concept"], canonical(dict(item["equivalence_scale"])),
             None if item.get("poverty_line") is None else canonical(dict(item["poverty_line"])),
             item.get("ppp_base_year"), item["reference_year_basis"], item["survey"], item["coverage"],
             item.get("methodology_version"), definition_key(item), item["frequency"], canonical(dict(item["unit"])),
             item.get("unit_multiplier"), item["area"]["scheme"], str(item["area"]["code"]),
             canonical(dict(item["area"])), canonical(dict(item.get("dimensions") or {})),
             canonical(list(item.get("references") or [])),
             None if not item.get("denominator") else canonical(dict(item["denominator"])),
             "income-indicator:" + series_id.split(":", 1)[1], release_id, self.now()],
        )
        return series_id, True

    def _definition(self, namespace, item, release_id):
        key = definition_key(item)
        content = {
            **dict(item["definition"]),
            "source_notes": [{k: n.get(k) for k in ("kind", "attribute", "value")}
                             for n in item.get("source_notes") or [] if n.get("kind") != "break"],
            "references": list(item.get("references") or []),
        }
        content_hash = digest(content)
        latest = self.conn.execute(
            "SELECT definition_id, content_hash, revision FROM income_definitions WHERE namespace=? AND "
            "definition_key=? ORDER BY revision DESC LIMIT 1", [namespace, key]).fetchone()
        if latest and latest[1] == content_hash:
            return latest[0], False
        existing = self.conn.execute(
            "SELECT definition_id FROM income_definitions WHERE namespace=? AND definition_key=? AND content_hash=?",
            [namespace, key, content_hash]).fetchone()
        if existing:
            return existing[0], False
        revision = 1 if latest is None else int(latest[2]) + 1
        definition_id = f"{key}@{revision}"
        self.conn.execute("INSERT INTO income_definitions VALUES (?,?,?,?,?,?,?,?,?)",
                          [namespace, definition_id, key, revision, item["provider"], canonical(content), content_hash,
                           release_id, self.now()])
        return definition_id, True

    def _vintage(self, namespace, series_id, item, header, release_id, clock, basis, retrieved, definition_id,
                 version, structure):
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        observations = list(item.get("observations") or [])
        # Each PIP release version is its own vintage; SDMX re-publications count by content.
        content_hash = digest([_observation_content(observations), version if item["provider"] == "pip" else None])
        previous = self.vintage_rows(namespace, series_id)
        same_clock = [v for v in previous if v["release_at_ms"] == clock]
        if same_clock:
            if same_clock[0]["content_hash"] != content_hash:
                raise IncomeError("vintage_conflict", "the publication changed values without a new release time; the "
                                  "stored vintage is kept", series_id=series_id)
            return same_clock[0]["vintage_id"], "unchanged"
        prior = previous[-1] if previous else None
        changes = self._changes(namespace, prior, observations, definition_id, version, item, structure)
        if (prior is not None and prior["content_hash"] == content_hash and not changes["definition_change"]
                and prior["status"] != "withdrawn"):
            return prior["vintage_id"], "unchanged"
        if prior is not None and clock < prior["release_at_ms"]:
            raise IncomeError("stale_release", "a release dated before the series' latest vintage is not appended",
                              series_id=series_id)
        record = SeriesRecord(
            series_id=series_id,
            provider=item["provider"],
            title=str(dict(item["indicator"]).get("label") or item["native_key"]),
            frequency=item["frequency"],
            as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None else float(Decimal(o["value"]))}
                          for o in sorted(observations, key=lambda o: o["period"])],
            unit=dict(item["unit"]).get("label"),
            geography=str(item["area"]["code"]),
            source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock),
                      "provider_release_time_status": f"income release clock ({basis})",
                      "acquired_at_ms": int(retrieved), "vintage_basis": basis, "source_document_id": release_id},
        )
        definition = dict(item["definition"])
        semantics = {
            "indicator_id": "income-indicator:" + series_id.split(":", 1)[1],
            "canonical_name": str(dict(item["indicator"]).get("label") or item["indicator"]["concept"]),
            "concept": f"income {item['indicator']['concept']} {item['indicator']['measure']}",
            "definition": definition.get("source_text") or definition.get("income_definition"),
            "seasonal_adjustment": "not_applicable",
            "price_basis": "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": definition.get("source_text") or definition.get("income_definition"),
            "attributes": {"income_series_id": series_id, "welfare_concept": item["welfare_concept"],
                           "equivalence_scale": dict(item["equivalence_scale"])["code"],
                           "poverty_line": item.get("poverty_line"), "ppp_base_year": item.get("ppp_base_year"),
                           "owner": PROVIDER, "contract": CONTRACT},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=SERIES_DOMAIN)
        except EconomicModelError as exc:
            raise IncomeError(exc.code, str(exc)) from exc
        vintage_id = "inc-vintage:" + digest([namespace, series_id, clock, content_hash, definition_id])[:24]
        self.conn.execute(
            "INSERT INTO income_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, economic["vintage"]["as_of"], clock, basis, retrieved,
             content_hash, definition_id, version, item.get("ppp_base_year") or structure.get("ppp_version"),
             "published", canonical([dict(n) for n in item.get("source_notes") or []]), canonical(changes),
             1 + len(previous), self.now()],
        )
        for obs in observations:
            self.conn.execute(
                "INSERT INTO income_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 canonical(dict(obs.get("flags") or {})), canonical(dict(obs.get("attributes") or {})),
                 canonical(list(obs.get("footnotes") or []))],
            )
        return vintage_id, "new"

    def _changes(self, namespace, prior, observations, definition_id, version, item, structure):
        """What a new vintage changes against the previous one (facts of the two records, nothing estimated)."""
        if prior is None:
            return {"first": True, "new_periods": sorted(o["period"] for o in observations), "revised": [],
                    "removed_periods": [], "ppp_revision": False, "definition_change": False,
                    "release_version": {"before": None, "after": version}}
        before = {o["period"]: o for o in self.observations(namespace, prior["vintage_id"])}
        after = {o["period"]: o for o in observations}
        revised = []
        for period in sorted(set(after) & set(before)):
            b, a = before[period], after[period]
            if tuple(b[k] for k in _OBS_COMPARED) != (a.get("value_text"), a["status"], dict(a.get("flags") or {}),
                                                      dict(a.get("attributes") or {})):
                revised.append({"period": period,
                                "before": {k: b[k] for k in ("value", "status", "flags", "attributes")},
                                "after": {"value": a.get("value"), "status": a["status"],
                                          "flags": dict(a.get("flags") or {}),
                                          "attributes": dict(a.get("attributes") or {})}})
        ppp_basis = []
        prior_release = self.release(namespace, prior["release_id"])
        before_version = dict(prior_release["structure"].get("release_version") or {})
        after_version = dict(structure.get("release_version") or {})
        if (before_version and after_version and before_version.get("ppp_version") == after_version.get("ppp_version")
                and before_version.get("ppp_revision") != after_version.get("ppp_revision")):
            ppp_basis.append(f"PIP release {after_version['version']} states PPP revision "
                             f"{after_version['ppp_revision']} of the {after_version['ppp_version']} round (before "
                             f"{before_version['ppp_revision']})")
        restated = sorted(r["period"] for r in revised
                          if r["before"]["attributes"].get("ppp") != r["after"]["attributes"].get("ppp")
                          and r["before"]["attributes"].get("ppp") is not None)
        if restated:
            ppp_basis.append(f"the published PPP conversion factor changed for {restated}")
        value_revised = [r["period"] for r in revised if r["before"]["value"] != r["after"]["value"]]
        return {
            "first": False,
            "new_periods": sorted(set(after) - set(before)),
            "revised": revised,
            "removed_periods": sorted(set(before) - set(after)),
            "ppp_revision": bool(ppp_basis) and bool(value_revised or restated),
            "ppp_revision_basis": ppp_basis or None,
            "restated_periods": sorted(set(value_revised) | set(restated)) if ppp_basis else [],
            "definition_change": prior["definition_id"] != definition_id or (
                prior.get("release_version") is not None and version is not None and item["provider"] != "pip"
                and prior["release_version"] != version),
            "definition": {"before": prior["definition_id"], "after": definition_id},
            "release_version": {"before": prior.get("release_version"), "after": version},
        }

    def _withdrawn(self, namespace, header, seen, clock) -> list[str]:
        """Series of the same declared document whose latest vintage is published but this release omits."""
        rows = self.conn.execute(
            "SELECT series_id FROM income_series WHERE namespace=? AND provider=? AND document_key=? ORDER BY series_id",
            [namespace, header["provider"], header["document_key"]]).fetchall()
        out = []
        for (series_id,) in rows:
            if series_id in seen:
                continue
            vintages = self.vintage_rows(namespace, series_id)
            if vintages and vintages[-1]["status"] == "published" and vintages[-1]["release_at_ms"] < clock:
                out.append(series_id)
        return out

    def _withdraw(self, namespace, series_id, release_id, clock, basis, retrieved, version) -> str:
        """The source no longer states this series: a withdrawal vintage (no values), never a deletion."""
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1]
        changes = {"first": False, "new_periods": [], "revised": [],
                   "removed_periods": [o["period"] for o in self.observations(namespace, prior["vintage_id"])],
                   "ppp_revision": False, "definition_change": False, "withdrawn": True,
                   "definition": {"before": prior["definition_id"], "after": prior["definition_id"]},
                   "release_version": {"before": prior.get("release_version"), "after": version},
                   "note": "the release no longer states this series; earlier vintages stay queryable"}
        content_hash = digest(["withdrawn", release_id])
        vintage_id = "inc-vintage:" + digest([namespace, series_id, clock, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO income_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, None, clock, basis, retrieved, content_hash,
             prior["definition_id"], version, prior.get("ppp_base_year"), "withdrawn", canonical([]),
             canonical(changes), 1 + len(previous), self.now()])
        return vintage_id

    def _source_notes(self, namespace, series_id, item, release_id) -> int:
        """Source-stated breaks and notes as comparability notes attached to the series and periods."""
        comparability = IncomeComparability(self.conn, now=self.now, initialize=False)
        created = 0
        for note in item.get("source_notes") or []:
            relation = "break_in_series" if note.get("kind") == "break" else "source_note"
            statement = (f"the source states a break in series ({note.get('attribute')}: {note.get('value')})"
                         if relation == "break_in_series" else f"{note.get('attribute')}: {note.get('value')}")
            created += int(comparability._insert(
                namespace, {"kind": "series", "id": series_id}, None, relation, statement,
                list(note.get("periods") or []), origin="source", state="source-stated",
                principal_id=f"source:{item['provider']}", release_id=release_id))
        return created

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id", "provider", "source_id", "format", "document_key", "document", "published_on", "published_at",
        "release_basis", "release_at_ms", "release_label", "release_version", "file_sha256", "content_sha256",
        "item_count", "structure", "evidence_origin", "url", "sequence", "run_id", "recorded_by", "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_key, document_json, published_on, published_at, "
            "release_basis, release_at_ms, release_label, release_version, file_sha256, content_sha256, item_count, "
            "structure_json, evidence_origin, url, sequence, run_id, recorded_by, retrieved_at_ms FROM "
            "income_releases WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "release is not visible in this namespace")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = load(view["document"], {})
        view["structure"] = load(view["structure"], {})
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        return {k: release[k] for k in (
            "release_id", "provider", "source_id", "file_sha256", "published_on", "published_at", "release_basis",
            "release_at_ms", "release_label", "release_version", "url", "evidence_origin", "retrieved_at_ms")} | {
            "document": release["document"].get("label"),
            "released_at": iso_from_ms(release["release_at_ms"]),
            "retrieved_at": iso_from_ms(release["retrieved_at_ms"]),
            "live_verification": LIVE_VERIFICATION.get(release["provider"], {}).get("status"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM income_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence", [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [{"series_id": r[0], "vintage_id": r[1], "status": r[2]} for r in self.conn.execute(
            "SELECT series_id, vintage_id, status FROM income_release_members WHERE namespace=? AND release_id=? "
            "ORDER BY series_id", [namespace, release_id]).fetchall()]

    _SERIES_COLUMNS = (
        "series_id, provider, native_key, document_key, dataflow_json, indicator_json, welfare_concept, "
        "equivalence_scale_json, poverty_line_json, ppp_base_year, reference_year_basis, survey, coverage, "
        "methodology_version, definition_key, frequency, unit_json, unit_multiplier, area_json, dimensions_json, "
        "references_json, denominator_json, economic_indicator_id, first_release_id"
    )

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, native_key, document_key, dataflow, indicator, welfare, scale, line, ppp, basis, survey,
         coverage, methodology, def_key, frequency, unit, multiplier, area, dimensions, references, denominator,
         economic_indicator_id, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        return {
            "contract": CONTRACT, "record_type": "series", "namespace": namespace, "series_id": series_id,
            "provider": provider, "native_key": native_key, "document_key": document_key,
            "dataflow": load(dataflow, {}), "indicator": load(indicator, {}), "welfare_concept": welfare,
            "equivalence_scale": load(scale, {}), "poverty_line": load(line, None), "ppp_base_year": ppp,
            "reference_year_basis": basis, "survey": survey, "coverage": coverage, "methodology_version": methodology,
            "definition_key": def_key, "frequency": frequency, "unit": load(unit, {}), "unit_multiplier": multiplier,
            "area": load(area, {}), "dimensions": load(dimensions, {}), "references": load(references, []),
            "denominator": load(denominator, None),
            "economic_series": {"domain": SERIES_DOMAIN, "series_id": series_id, "indicator_id": economic_indicator_id},
            "first_release_id": first_release, "vintage_count": len(vintages),
            "current_vintage_id": None if current is None else current["vintage_id"],
            "current_definition_id": None if current is None else current["definition_id"],
            "current_status": None if current is None else current["status"],
        }

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._SERIES_COLUMNS} FROM income_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, concept: str | None = None,
                    areas: Iterable[tuple[str, str]] | None = None, welfare_concept: str | None = None,
                    limit: int = 1000) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS}, area_scheme, area_code FROM income_series WHERE namespace=? AND "
            "(? IS NULL OR provider=?) AND (? IS NULL OR concept=?) AND (? IS NULL OR welfare_concept=?) "
            "ORDER BY provider, concept, area_code, native_key, series_id",
            [namespace, provider, provider, concept, concept, welfare_concept, welfare_concept]).fetchall()
        wanted = None if areas is None else {(str(s), str(c)) for s, c in areas}
        out = []
        for row in rows:
            if wanted is not None and (row[-2], row[-1]) not in wanted:
                continue
            out.append(self._series_view(namespace, row[:-2]))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage in release-clock order, each with ``revision_of`` its predecessor and its changes."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, economic_as_of, release_at_ms, release_at_basis, retrieved_at_ms, "
            "content_hash, definition_id, release_version, ppp_base_year, status, changes_json, sequence FROM "
            "income_vintages WHERE namespace=? AND series_id=? ORDER BY release_at_ms, sequence",
            [namespace, series_id]).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(zip(("vintage_id", "release_id", "economic_as_of", "release_at_ms", "release_at_basis",
                             "retrieved_at_ms", "content_hash", "definition_id", "release_version", "ppp_base_year",
                             "status", "changes", "sequence"), row))
            view["changes"] = load(view["changes"], {})
            view["series_id"] = series_id
            view["release_at"] = iso_from_ms(view["release_at_ms"])
            view["retrieved_at"] = iso_from_ms(view["retrieved_at_ms"])
            view["revision_of"] = None if previous is None else previous["vintage_id"]
            view["economic_vintage_id"] = (None if view["economic_as_of"] is None
                                           else f"{series_id}@{view['economic_as_of']}")
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT series_id FROM income_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "vintage is not visible in this namespace")
        view = next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)
        return {"contract": CONTRACT, "record_type": "vintage", "namespace": namespace, **view,
                "source_revision": self.source_revision(namespace, view["release_id"])}

    def observations(self, namespace: str, vintage_id: str, *, period_from: str | None = None,
                     period_to: str | None = None) -> list[dict[str, Any]]:
        vintage = self.conn.execute(
            "SELECT series_id, economic_as_of FROM income_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id]).fetchone()
        if vintage is None:
            return []
        numeric = {r[0]: r[1] for r in self.conn.execute(
            "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?", [vintage[0], vintage[1]]
        ).fetchall()} if vintage[1] is not None and table_exists(self.conn, "dataset_observations") else {}
        out = []
        for period, value_text, value, status, flags, attributes, footnotes in self.conn.execute(
                "SELECT period, value_text, value, status, flags_json, attributes_json, footnotes_json FROM "
                "income_observations WHERE namespace=? AND vintage_id=? ORDER BY period",
                [namespace, vintage_id]).fetchall():
            if (period_from and period < period_from) or (period_to and period > period_to):
                continue
            out.append({"record_type": "observation", "period": period, "value_text": value_text, "value": value,
                        "numeric_value": numeric.get(period), "status": status, "flags": load(flags, {}),
                        "attributes": load(attributes, {}), "footnotes": load(footnotes, [])})
        return out

    def select_vintage(self, namespace: str, series_id: str, *, as_of_ms: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (release-cutoff semantics of the economic release store)."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def definition(self, namespace: str, definition_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT definition_id, definition_key, revision, provider, content_json, release_id FROM "
            "income_definitions WHERE namespace=? AND definition_id=?", [namespace, definition_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "definition is not visible in this namespace")
        return {"contract": CONTRACT, "record_type": "definition", "namespace": namespace, "definition_id": row[0],
                "definition_key": row[1], "revision": row[2], "provider": row[3], "content": json.loads(row[4]),
                "source_revision": self.source_revision(namespace, row[5])}

    def definitions(self, namespace: str, key: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_definitions"):
            return []
        rows = self.conn.execute(
            "SELECT definition_id FROM income_definitions WHERE namespace=? AND (? IS NULL OR definition_key=?) "
            "ORDER BY definition_key, revision", [namespace, key, key]).fetchall()
        return [self.definition(namespace, r[0]) for r in rows]

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "income_releases"):
            return None
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM income_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])


def _side(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    value = dict(value)
    if bool(value.get("series_id")) == bool(value.get("definition_id")):
        raise IncomeError("invalid_note", "each side names one series_id or one definition_id")
    return ({"kind": "series", "id": str(value["series_id"])} if value.get("series_id")
            else {"kind": "definition", "id": str(value["definition_id"])})


def pair_key(left: Mapping[str, str], right: Mapping[str, str] | None) -> str:
    """Order-insensitive: a note on (a, b) is a note on (b, a); a single-sided note keys its one record."""
    sides = [f"{left['kind']}:{left['id']}"] + ([] if right is None else [f"{right['kind']}:{right['id']}"])
    return canonical(sorted(sides))


class IncomeComparability:
    """Reviewable comparability notes between income series or definitions (the labour/demographics pattern)."""

    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = IncomeStore(conn, initialize=initialize, now=now)
        self.now = self.store.now

    def _cite(self, namespace: str, side: Mapping[str, str]) -> dict[str, Any]:
        if side["kind"] == "series":
            series = self.store.series(namespace, side["id"])
            if series["current_definition_id"] is None:
                raise IncomeError("not_found", "the series has no vintage yet")
            definition = self.store.definition(namespace, series["current_definition_id"])
        else:
            definition = self.store.definition(namespace, side["id"])
        content = definition["content"]
        return {**dict(side), "provider": definition["provider"], "definition_id": definition["definition_id"],
                "definition": {k: content.get(k) for k in ("concept", "welfare_concept", "income_definition",
                                                           "equivalence_scale", "poverty_line", "reference_year_basis",
                                                           "survey", "methodology_version")},
                "source_revision": definition["source_revision"]}

    def note_id(self, namespace, left, right, relation, statement, periods) -> str:
        return "inc-comparability:" + digest([namespace, pair_key(left, right), relation, statement.strip(),
                                              sorted(periods)])[:24]

    def _insert(self, namespace, left, right, relation, statement, periods, *, origin, state, principal_id,
                release_id=None) -> bool:
        note_id = self.note_id(namespace, left, right, relation, statement, periods)
        if self.conn.execute("SELECT 1 FROM income_comparability WHERE namespace=? AND note_id=?",
                             [namespace, note_id]).fetchone():
            return False
        cited = [self._cite(namespace, left)] + ([] if right is None else [self._cite(namespace, right)])
        if release_id is not None:
            cited[0]["stated_in"] = self.store.source_revision(namespace, release_id)
        now = self.now()
        self.conn.execute(
            "INSERT INTO income_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, note_id, pair_key(left, right), canonical(dict(left)),
             None if right is None else canonical(dict(right)), relation, statement.strip(), canonical(sorted(periods)),
             canonical(sorted(cited, key=canonical)), origin, state,
             canonical([{"state": state, "by": principal_id, "at_ms": now}]), principal_id, now])
        return True

    def record(self, namespace: str, left: Mapping[str, Any], right: Mapping[str, Any] | None, relation: str,
               statement: str, *, principal_id: str, scopes: Iterable[str], periods: Sequence[str] = ()
               ) -> dict[str, Any]:
        """Propose a note (idempotent for the same records, relation, statement and periods)."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if relation not in RELATIONS or not str(statement or "").strip():
            raise IncomeError("invalid_note", f"a note has one of {RELATIONS} and a statement")
        a, b = _side(left), _side(right)
        if a is None or (b is None and relation not in SINGLE_SIDED):
            raise IncomeError("invalid_note", "a note links two records (breaks and source notes may name one)")
        if a == b:
            raise IncomeError("invalid_note", "a note links two different records")
        periods = [str(p) for p in periods]
        self._insert(namespace, a, b, relation, statement, periods, origin="reviewer", state="proposed",
                     principal_id=principal_id)
        return self.note(namespace, self.note_id(namespace, a, b, relation, statement, periods), scopes={"operator"})

    def _transition(self, namespace, note, state, principal_id, reason):
        history = note["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute("UPDATE income_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
                          [state, canonical(history), namespace, note["note_id"]])
        return self.note(namespace, note["note_id"], scopes={"operator"})

    def review(self, namespace, note_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise IncomeError("invalid_decision", "accept or reject with a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] != "proposed":
            raise IncomeError("invalid_state", f"note is {note['state']}; only a proposed note is reviewed")
        if note["created_by"] == principal_id:
            raise IncomeError("self_review", "a note is reviewed by someone other than its proposer")
        return self._transition(namespace, note, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, note_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise IncomeError("invalid_decision", "a revert needs a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] not in {"accepted", "rejected"}:
            raise IncomeError("invalid_state", "only an accepted or rejected note can be reverted")
        return self._transition(namespace, note, "reverted", principal_id, reason.strip())

    def note(self, namespace: str, note_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT note_id, pair_key, left_json, right_json, relation, statement, periods_json, cited_json, origin, "
            "state, history_json, created_by, created_at_ms FROM income_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "comparability note is not visible in this namespace")
        return {"contract": COMPARABILITY_CONTRACT, "record_type": "comparability_note", "namespace": namespace,
                "note_id": row[0], "pair_key": row[1], "left": json.loads(row[2]), "right": load(row[3], None),
                "relation": row[4], "statement": row[5], "periods": json.loads(row[6]), "cited": json.loads(row[7]),
                "origin": row[8], "state": row[9], "history": json.loads(row[10]), "created_by": row[11],
                "created_at_ms": row[12]}

    def notes(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None,
              definition_id: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "income_comparability"):
            return []
        wanted = [{"kind": "series", "id": series_id}] if series_id else []
        wanted += [{"kind": "definition", "id": definition_id}] if definition_id else []
        out = []
        for note_id, left, right in self.conn.execute(
                "SELECT note_id, left_json, right_json FROM income_comparability WHERE namespace=? "
                "ORDER BY created_at_ms, note_id", [namespace]).fetchall():
            sides = [json.loads(left)] + ([] if right is None else [json.loads(right)])
            if wanted and not any(s in wanted for s in sides):
                continue
            note = self.note(namespace, note_id, scopes=scopes)
            if active_only and note["state"] not in ACTIVE_STATES:
                continue
            out.append(note)
        return out

    def notes_between(self, namespace: str, left_series_id: str, right_series_id: str) -> list[dict[str, Any]]:
        """Active notes linking two series (or their current definitions)."""
        if not table_exists(self.conn, "income_comparability"):
            return []
        left, right = self.store.series(namespace, left_series_id), self.store.series(namespace, right_series_id)
        keys = {pair_key(a, b)
                for a in ({"kind": "series", "id": left_series_id},
                          {"kind": "definition", "id": left["current_definition_id"]})
                for b in ({"kind": "series", "id": right_series_id},
                          {"kind": "definition", "id": right["current_definition_id"]})}
        notes = [self.note(namespace, r[0], scopes={"operator"}) for r in self.conn.execute(
            "SELECT note_id, pair_key FROM income_comparability WHERE namespace=? ORDER BY created_at_ms, note_id",
            [namespace]).fetchall() if r[1] in keys]
        return [{k: n[k] for k in ("note_id", "relation", "statement", "periods", "state", "origin")}
                for n in notes if n["state"] in ACTIVE_STATES]


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, str]]:
    """Recorded differences between two series' declared attributes (facts of the records, never a harmonisation)."""
    out = []
    if left["provider"] != right["provider"]:
        out.append({"kind": "different_source", "detail": f"{left['provider']} against {right['provider']}; "
                    "figures of different publishers are never blended"})
    for field, kind in (("welfare_concept", "different_welfare_concept"),
                        ("reference_year_basis", "different_reference_year_basis"),
                        ("ppp_base_year", "different_ppp_base_year"),
                        ("methodology_version", "different_methodology"),
                        ("survey", "different_survey")):
        if left.get(field) != right.get(field):
            out.append({"kind": kind, "detail": f"{left.get(field)} ({left['provider']}) against {right.get(field)} "
                                                f"({right['provider']})"})
    if left["equivalence_scale"].get("code") != right["equivalence_scale"].get("code"):
        out.append({"kind": "different_equivalence_scale",
                    "detail": f"{left['equivalence_scale'].get('code')} against {right['equivalence_scale'].get('code')}"})
    if left.get("poverty_line") != right.get("poverty_line"):
        out.append({"kind": "different_poverty_line", "detail": f"{left.get('poverty_line')} against "
                                                                f"{right.get('poverty_line')}"})
    if left["unit"] != right["unit"]:
        out.append({"kind": "different_unit", "detail": f"{left['unit']} against {right['unit']}"})
    if left["provider"] != right["provider"] and left.get("survey") == right.get("survey"):
        out.append({"kind": "same_underlying_survey", "detail": f"both rest on {left['survey']}; the publishers' "
                    "figures are still kept apart"})
    return out


class IncomeProjector:
    """Source-pack runtime projector for ``noesis-income-distribution-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = IncomeStore(conn)
        IncomeComparability(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("income_distribution") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("income_release") or {}), item.get("income_item")
            if not header or not isinstance(body, Mapping):
                raise IncomeError("invalid_record", "page record is not an income release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM income_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [self._namespace(source), source["source_id"]]
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_published_on": row[1] if row else None}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the income record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "income-distribution-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": PROVIDER, "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "income-schema:income-distribution-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


def readiness(conn: Any) -> dict[str, Any]:
    """Stores, per-provider releases, feature selection and live verification (offline and live kept apart)."""
    ready = IncomeStore(conn, initialize=False).ready() and table_exists(conn, "income_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = int(conn.execute("SELECT count(*) FROM income_releases WHERE provider=?",
                                    [provider]).fetchone()[0]) if ready else 0
        live = int(conn.execute("SELECT count(*) FROM income_releases WHERE provider=? AND evidence_origin='live'",
                                [provider]).fetchone()[0]) if ready else 0
        providers[provider] = {
            "feature": SOURCE_FEATURES[provider],
            "feature_state": feature_state(conn, SOURCE_FEATURES[provider]),
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "live_verification": LIVE_VERIFICATION[provider]["status"],
            "releases": releases,
            "live_releases": live,
        }
    return {
        "provider": PROVIDER,
        "stores_ready": ready,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations",
        "providers": providers,
        "links": {f: feature_state(conn, f) for f in ("demographics-links", "labour-links")},
        "minimisation": MINIMISATION,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "answer_contract": ANSWER_CONTRACT,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }


__all__ = [
    "IncomeComparability",
    "IncomeProjector",
    "IncomeStore",
    "comparability_basis",
    "pair_key",
    "readiness",
    "register_schemas",
]

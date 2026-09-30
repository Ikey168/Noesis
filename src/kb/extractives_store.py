"""The ``economics.extractives`` store: EITI report revisions and commodity series vintages (#2653, EX02-EX05).

Numeric commodity observations and their vintages live in the existing Economics series storage -
:func:`src.domains.economic.model.register_series` writes the indicator, the provider series mapping, the release
clock with ``revision_of`` (``economic_vintages``) and the values per vintage (``dataset_observations``). The
``ex_*`` tables add only what the generic store has no place for: releases with licence and attribution, EITI
report revisions with their payment and discrepancy lines, commodity definitions, the status and estimated and
revised flags of each value, and each vintage's recorded changes. See :mod:`src.kb.extractives_records` for the
record vocabulary.

Every write is an append: a changed EITI summary for the same country and fiscal period is a new report revision,
a withdrawn summary is a revision stating the withdrawal, and a USGS release or BGS edition that changes a series
is a new vintage whose changes list new, revised and removed years. Re-acquiring an unchanged file adds nothing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.extractives_sources import (
    FORMATS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
)
from src.kb.extractives_records import (
    ANSWER_CONTRACT,
    CONTRACT,
    DEFAULT_NAMESPACE,
    DOMAIN,
    EXCLUSIONS,
    MINIMISATION,
    NEVER_SENTENCE,
    ExtractivesError,
    canonical,
    check_item,
    commodity_key,
    company_key,
    digest,
    feature_enabled,
    iso_from_ms,
    load,
    project_key,
    release_ms,
    selected_features,
    table_exists,
)

_DDL = """
CREATE TABLE IF NOT EXISTS ex_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, published_on TEXT, published_at TEXT, release_basis TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_label TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  item_count INTEGER NOT NULL, structure_json TEXT NOT NULL, licence_json TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  url TEXT, sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS ex_reports (
  namespace TEXT NOT NULL, report_id TEXT NOT NULL, report_key TEXT NOT NULL, revision INTEGER NOT NULL,
  revision_of TEXT, release_id TEXT NOT NULL, status TEXT NOT NULL, country_code TEXT NOT NULL,
  country_json TEXT NOT NULL, fiscal_json TEXT NOT NULL, report_json TEXT NOT NULL, currency TEXT,
  commodities_json TEXT NOT NULL, excluded_json TEXT NOT NULL, changes_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, report_id)
);
CREATE TABLE IF NOT EXISTS ex_payments (
  namespace TEXT NOT NULL, report_id TEXT NOT NULL, line_key TEXT NOT NULL, reported_by TEXT NOT NULL,
  level TEXT NOT NULL, agency_json TEXT NOT NULL, stream_json TEXT NOT NULL, company_key TEXT, company_json TEXT,
  project_key TEXT, project_json TEXT, amount_text TEXT, amount TEXT, currency TEXT, in_kind BOOLEAN NOT NULL,
  budget_reference_json TEXT, PRIMARY KEY(namespace, report_id, line_key)
);
CREATE TABLE IF NOT EXISTS ex_discrepancies (
  namespace TEXT NOT NULL, report_id TEXT NOT NULL, line INTEGER NOT NULL, company_key TEXT, company_json TEXT,
  stream_json TEXT NOT NULL, government_amount_text TEXT, company_amount_text TEXT, discrepancy_text TEXT,
  government_amount TEXT, company_amount TEXT, discrepancy TEXT, currency TEXT, explanation TEXT, basis TEXT NOT NULL,
  PRIMARY KEY(namespace, report_id, line)
);
CREATE TABLE IF NOT EXISTS ex_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, commodity_key TEXT NOT NULL,
  commodity_json TEXT NOT NULL, statistic TEXT NOT NULL, unit_json TEXT NOT NULL, country_name TEXT NOT NULL,
  country_json TEXT NOT NULL, definition_json TEXT NOT NULL, references_json TEXT NOT NULL,
  economic_indicator_id TEXT NOT NULL, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS ex_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  economic_as_of BIGINT NOT NULL, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, publication_json TEXT NOT NULL,
  changes_json TEXT NOT NULL, sequence INTEGER NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS ex_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, estimated BOOLEAN NOT NULL, revised BOOLEAN NOT NULL, flags_json TEXT NOT NULL,
  notes_json TEXT NOT NULL, PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS ex_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, member_key TEXT NOT NULL, member_kind TEXT NOT NULL,
  record_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, member_key)
);
"""
TABLES = ("ex_releases", "ex_reports", "ex_payments", "ex_discrepancies", "ex_series", "ex_vintages",
          "ex_observations", "ex_release_members")


def _line_identity(line: Mapping[str, Any], company: str | None, project: str | None) -> list[Any]:
    stream = dict(line.get("revenue_stream") or {})
    return [line.get("reported_by"), line.get("level"), company, project,
            dict(line.get("agency") or {}).get("name_as_reported"), stream.get("gfs_code"),
            stream.get("name_as_reported"), bool(line.get("in_kind"))]


class ExtractivesStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "ex_vintages") and table_exists(self.conn, "ex_reports")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != provider:
            raise ExtractivesError("invalid_release", "release names a known provider and format")
        document = dict(header.get("document") or {})
        release_id = "ex-release:" + digest([namespace, provider, source_id, header["file_sha256"], document])[:24]
        if self.conn.execute("SELECT 1 FROM ex_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM ex_releases WHERE namespace=? AND provider=?",
            [namespace, provider]).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        licence = dict(header.get("licence") or {}) or {
            k: PROVIDER_CONTRACTS[provider].get(k) for k in ("terms", "attribution")}
        self.conn.execute(
            "INSERT INTO ex_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, release_id, provider, source_id, fmt, canonical(document), header.get("published_on"),
             header.get("published_at"), str(header.get("release_basis") or "retrieval_time"),
             release_ms(header.get("published_on"), header.get("published_at"), retrieved),
             header.get("release_label"), header["file_sha256"], header.get("content_sha256") or "",
             int(header.get("item_count") or 0), canonical(header.get("structure") or {}), canonical(licence),
             origin, header.get("url"), int(sequence) + 1, run_id, recorded_by, retrieved])
        return release_id, True

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      run_id: str, source_id: str | None, retrieved_at_ms: int | None = None,
                      recorded_by: str | None = None) -> dict[str, Any]:
        """Record one publication: report revisions and series vintages where content changed; idempotent by file."""
        if int(header.get("item_count", -1)) != len(items):
            raise ExtractivesError("incomplete_release", "a release carries every item it states")
        for item in items:
            check_item(item)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
        if clock > retrieved:
            raise ExtractivesError("invalid_release", "a release cannot be dated after its retrieval")
        counts = {"reports": 0, "unchanged_reports": 0, "series": 0, "vintages": 0, "unchanged_vintages": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(namespace, header, source_id=source_id, run_id=run_id,
                                                retrieved=retrieved, recorded_by=recorded_by)
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            basis = str(header.get("release_basis") or "retrieval_time")
            publication = {"label": header.get("release_label"), "published_on": header.get("published_on"),
                           "basis": basis}
            record_ids = []
            for item in items:
                if item["kind"] == "eiti_report":
                    report_id, status = self._report(namespace, item, release_id, clock, retrieved)
                    member_key, kind = report_id.rsplit("@", 1)[0], "eiti_report"
                    counts["reports" if status == "new" else "unchanged_reports"] += 1
                    record_id = report_id
                else:
                    series_id, new_series = self._series(namespace, item, release_id)
                    counts["series"] += int(new_series)
                    record_id, status = self._vintage(namespace, series_id, item, header, release_id, clock, basis,
                                                      retrieved, publication)
                    member_key, kind = series_id, "commodity_series"
                    counts["vintages" if status == "new" else "unchanged_vintages"] += 1
                if self.conn.execute("SELECT 1 FROM ex_release_members WHERE namespace=? AND release_id=? AND "
                                     "member_key=?", [namespace, release_id, member_key]).fetchone():
                    raise ExtractivesError("invalid_release", "a release states the same record twice")
                self.conn.execute("INSERT INTO ex_release_members VALUES (?,?,?,?,?,?)",
                                  [namespace, release_id, member_key, kind, record_id, status])
                if status == "new":
                    record_ids.append(record_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "published_on": header.get("published_on"),
                "release_basis": header.get("release_basis"), "record_ids": record_ids, **counts}

    # -- EITI reports

    @staticmethod
    def report_key(namespace: str, item: Mapping[str, Any]) -> str:
        return "ex-report:" + digest([namespace, item["country"]["code"], item["fiscal_period"]["start"],
                                      item["fiscal_period"]["end"]])[:24]

    def _lines(self, item: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
        country = item["country"]
        out = []
        for prefix, lines in (("gov", item.get("government_revenues") or []),
                              ("co", item.get("company_payments") or [])):
            for line in lines:
                company = line.get("company")
                project = line.get("project")
                ckey = None if not company else company_key(country, company, report_key=key, line=line["line"])
                pkey = None if not project else project_key(country, project)
                out.append({**dict(line), "line_key": f"{prefix}:{line['line']}", "company_key": ckey,
                            "project_key": pkey, "identity": _line_identity(line, ckey, pkey)})
        return out

    def _report_changes(self, previous: Mapping[str, Any] | None, lines: list[dict[str, Any]], status: str,
                        discrepancies: list[dict[str, Any]]) -> dict[str, Any]:
        if previous is None:
            return {"first": True, "status": status, "lines_added": len(lines), "lines_changed": [],
                    "lines_removed": 0, "discrepancies_changed": False}
        before = {canonical(p["identity"]): p for p in self.payments(previous["namespace"], previous["report_id"])}
        after = {canonical(line["identity"]): line for line in lines}
        changed = []
        for key in sorted(set(before) & set(after)):
            b, a = before[key], after[key]
            if (b["amount_text"], b["currency"]) != (a.get("amount_text"), a.get("currency")):
                changed.append({"line": a["line_key"], "reported_by": a["reported_by"],
                                "revenue_stream": a["revenue_stream"], "company_key": a["company_key"],
                                "before": {"amount_text": b["amount_text"], "currency": b["currency"]},
                                "after": {"amount_text": a.get("amount_text"), "currency": a.get("currency")}})
        prior_disc = [{k: d[k] for k in ("government_amount_text", "company_amount_text", "discrepancy_text",
                                         "currency", "explanation")}
                      for d in self.discrepancies(previous["namespace"], previous["report_id"])]
        new_disc = [{k: d.get(k) for k in ("government_amount_text", "company_amount_text", "discrepancy_text",
                                           "currency", "explanation")} for d in discrepancies]
        return {"first": False, "status": status, "status_before": previous["status"],
                "withdrawn": status == "withdrawn" and previous["status"] != "withdrawn",
                "lines_added": len(set(after) - set(before)), "lines_removed": len(set(before) - set(after)),
                "lines_changed": changed, "discrepancies_changed": prior_disc != new_disc,
                "version": {"before": previous["report"].get("version"), "after": None}}

    def _report(self, namespace, item, release_id, clock, retrieved):
        key = self.report_key(namespace, item)
        content = {k: item.get(k) for k in ("report", "country", "fiscal_period", "currency", "government_revenues",
                                            "company_payments", "discrepancies", "commodities")}
        content_hash = digest(content)
        revisions = self.report_revisions(namespace, key)
        latest = revisions[-1] if revisions else None
        if latest is not None and latest["content_hash"] == content_hash:
            return latest["report_id"], "unchanged"
        if latest is not None and clock < latest["release_at_ms"]:
            raise ExtractivesError("stale_release", "a summary dated before the report's latest revision is not "
                                                    "appended", report_key=key)
        revision = 1 if latest is None else latest["revision"] + 1
        report_id = f"{key}@{revision}"
        status = item["report"].get("status") or "published"
        lines = self._lines(item, key)
        changes = self._report_changes(latest, lines, status, list(item.get("discrepancies") or []))
        if latest is not None:
            changes["version"]["after"] = item["report"].get("version")
        self.conn.execute(
            "INSERT INTO ex_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, report_id, key, revision, None if latest is None else latest["report_id"], release_id, status,
             item["country"]["code"], canonical(item["country"]), canonical(item["fiscal_period"]),
             canonical(item["report"]), item.get("currency"), canonical(list(item.get("commodities") or [])),
             canonical(list(item.get("excluded_fields") or [])), canonical(changes), content_hash, clock, retrieved,
             self.now()])
        for line in lines:
            self.conn.execute(
                "INSERT INTO ex_payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, report_id, line["line_key"], line["reported_by"], line["level"],
                 canonical(line.get("agency") or {}), canonical(line.get("revenue_stream") or {}),
                 line["company_key"], None if not line.get("company") else canonical(line["company"]),
                 line["project_key"], None if not line.get("project") else canonical(line["project"]),
                 line.get("amount_text"), line.get("amount"), line.get("currency"), bool(line.get("in_kind")),
                 None if not line.get("budget_reference") else canonical(line["budget_reference"])])
        for disc in item.get("discrepancies") or []:
            company = disc.get("company")
            ckey = None if not company else company_key(item["country"], company, report_key=key,
                                                        line=10_000 + int(disc["line"]))
            self.conn.execute(
                "INSERT INTO ex_discrepancies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, report_id, int(disc["line"]), ckey, None if not company else canonical(company),
                 canonical(disc.get("revenue_stream") or {}), disc.get("government_amount_text"),
                 disc.get("company_amount_text"), disc.get("discrepancy_text"), disc.get("government_amount"),
                 disc.get("company_amount"), disc.get("discrepancy"), disc.get("currency"), disc.get("explanation"),
                 disc.get("basis") or "as published"])
        return report_id, "new"

    # -- commodity series

    @staticmethod
    def series_key(item: Mapping[str, Any]) -> list[Any]:
        """Source, commodity (name and form), statistic, unit and country: USGS and BGS never share a key."""
        return [item["provider"], item["commodity"].get("name"), item["commodity"].get("form"), item["statistic"],
                dict(item["unit"]).get("label"), item["country"].get("name"), item["country"].get("code")]

    def _series(self, namespace, item, release_id):
        series_id = "ex-series:" + digest([namespace, *self.series_key(item)])[:24]
        if self.conn.execute("SELECT 1 FROM ex_series WHERE namespace=? AND series_id=?",
                             [namespace, series_id]).fetchone():
            return series_id, False
        self.conn.execute(
            "INSERT INTO ex_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, series_id, item["provider"], commodity_key(item["provider"], item["commodity"]),
             canonical(dict(item["commodity"])), item["statistic"], canonical(dict(item["unit"])),
             str(item["country"].get("name")), canonical(dict(item["country"])),
             canonical(dict(item.get("definition") or {})), canonical(list(item.get("references") or [])),
             "extractives-indicator:" + series_id.split(":", 1)[1], release_id, self.now()])
        return series_id, True

    @staticmethod
    def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{k: o.get(k) for k in ("period", "value_text", "value", "status", "estimated", "revised", "flags",
                                       "notes")} for o in sorted(observations, key=lambda o: o["period"])]

    def _vintage(self, namespace, series_id, item, header, release_id, clock, basis, retrieved, publication):
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        observations = list(item.get("observations") or [])
        content_hash = digest(self._content(observations))
        previous = self.vintage_rows(namespace, series_id)
        same_clock = [v for v in previous if v["release_at_ms"] == clock]
        if same_clock:
            if same_clock[0]["content_hash"] != content_hash:
                raise ExtractivesError("vintage_conflict", "the publication changed values without a new "
                                                           "publication date; the stored vintage is kept",
                                       series_id=series_id)
            return same_clock[0]["vintage_id"], "unchanged"
        prior = previous[-1] if previous else None
        if prior is not None and prior["content_hash"] == content_hash:
            return prior["vintage_id"], "unchanged"
        if prior is not None and clock < prior["release_at_ms"]:
            raise ExtractivesError("stale_release", "a publication dated before the series' latest vintage is not "
                                                    "appended", series_id=series_id)
        changes = self._changes(namespace, prior, observations)
        record = SeriesRecord(
            series_id=series_id, provider=item["provider"],
            title=f"{item['commodity'].get('name')} {item['statistic']} ({item['country'].get('name')})",
            frequency="annual", as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None else float(Decimal(o["value"]))}
                          for o in sorted(observations, key=lambda o: o["period"])],
            unit=dict(item["unit"]).get("label"), geography=str(item["country"].get("code") or item["country"]["name"]),
            source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock),
                      "provider_release_time_status": f"extractives publication clock ({basis})",
                      "acquired_at_ms": int(retrieved), "vintage_basis": basis, "source_document_id": release_id})
        semantics = {
            "indicator_id": "extractives-indicator:" + series_id.split(":", 1)[1],
            "canonical_name": f"{item['commodity'].get('name')} {item['statistic']}",
            "concept": f"extractives {item['provider']} {item['commodity'].get('name')} "
                       f"{item['commodity'].get('form') or ''} {item['statistic']}",
            "definition": f"{item['statistic']} of {item['commodity'].get('name')} as published by "
                          f"{item['provider']}",
            "seasonal_adjustment": "not_applicable", "price_basis": "not_applicable",
            "provider_code": str(item["commodity"].get("code") or item["commodity"].get("name")),
            "provider_definition": canonical(dict(item.get("definition") or {})),
            "attributes": {"extractives_series_id": series_id, "statistic": item["statistic"], "contract": CONTRACT},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=DOMAIN)
        except EconomicModelError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc
        vintage_id = "ex-vintage:" + digest([namespace, series_id, clock, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO ex_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, economic["vintage"]["as_of"], clock, basis, retrieved,
             content_hash, canonical(publication), canonical(changes), 1 + len(previous), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO ex_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 bool(obs.get("estimated")), bool(obs.get("revised")), canonical(dict(obs.get("flags") or {})),
                 canonical(list(obs.get("notes") or []))])
        return vintage_id, "new"

    def _changes(self, namespace, prior, observations):
        """What a new vintage changes against the previous one: new, revised and removed years (facts only)."""
        if prior is None:
            return {"first": True, "new_periods": sorted(o["period"] for o in observations), "revised": [],
                    "removed_periods": []}
        before = {o["period"]: o for o in self.observations(namespace, prior["vintage_id"])}
        after = {o["period"]: o for o in observations}
        revised = []
        for period in sorted(set(after) & set(before)):
            b, a = before[period], after[period]
            if (b["value_text"], b["status"], b["estimated"], b["revised"]) != (
                    a.get("value_text"), a["status"], bool(a.get("estimated")), bool(a.get("revised"))):
                revised.append({"period": period,
                                "before": {k: b[k] for k in ("value", "value_text", "status", "estimated")},
                                "after": {"value": a.get("value"), "value_text": a.get("value_text"),
                                          "status": a["status"], "estimated": bool(a.get("estimated"))}})
        return {"first": False, "new_periods": sorted(set(after) - set(before)), "revised": revised,
                "removed_periods": sorted(set(before) - set(after))}

    # ------------------------------------------------------------------ reads: releases

    _RELEASE_KEYS = ("release_id", "provider", "source_id", "format", "document", "published_on", "published_at",
                     "release_basis", "release_at_ms", "release_label", "file_sha256", "content_sha256",
                     "item_count", "structure", "licence", "evidence_origin", "url", "sequence", "run_id",
                     "recorded_by", "retrieved_at_ms")

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_json, published_on, published_at, "
            "release_basis, release_at_ms, release_label, file_sha256, content_sha256, item_count, structure_json, "
            "licence_json, evidence_origin, url, sequence, run_id, recorded_by, retrieved_at_ms FROM ex_releases "
            "WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "release is not visible in this namespace")
        view = dict(zip(self._RELEASE_KEYS, row))
        for key in ("document", "structure", "licence"):
            view[key] = load(view[key], {})
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        return {k: release[k] for k in ("release_id", "provider", "source_id", "file_sha256", "published_on",
                                        "published_at", "release_basis", "release_at_ms", "release_label", "url",
                                        "evidence_origin", "retrieved_at_ms", "licence")} | {
            "document": release["document"].get("label"),
            "release_at": iso_from_ms(release["release_at_ms"]),
            "retrieved_at": iso_from_ms(release["retrieved_at_ms"]),
            "live_verification": LIVE_VERIFICATION.get(release["provider"], {}).get("status"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ex_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM ex_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence", [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def latest_retrieval_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "ex_releases"):
            return None
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM ex_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    # ------------------------------------------------------------------ reads: reports

    _REPORT_COLUMNS = ("report_id, report_key, revision, revision_of, release_id, status, country_json, fiscal_json, "
                       "report_json, currency, commodities_json, excluded_json, changes_json, content_hash, "
                       "release_at_ms, retrieved_at_ms")

    def _report_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        keys = ("report_id", "report_key", "revision", "revision_of", "release_id", "status", "country",
                "fiscal_period", "report", "currency", "commodities", "excluded_fields", "changes", "content_hash",
                "release_at_ms", "retrieved_at_ms")
        view = dict(zip(keys, row))
        for key in ("country", "fiscal_period", "report", "commodities", "excluded_fields", "changes"):
            view[key] = load(view[key], None)
        view["release_at"] = iso_from_ms(view["release_at_ms"])
        view["retrieved_at"] = iso_from_ms(view["retrieved_at_ms"])
        return {"contract": CONTRACT, "record_type": "eiti_report", "namespace": namespace, **view}

    def report_revisions(self, namespace: str, report_key: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ex_reports"):
            return []
        rows = self.conn.execute(f"SELECT {self._REPORT_COLUMNS} FROM ex_reports WHERE namespace=? AND report_key=? "
                                 "ORDER BY revision", [namespace, report_key]).fetchall()
        return [self._report_view(namespace, r) for r in rows]

    def report(self, namespace: str, report_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._REPORT_COLUMNS} FROM ex_reports WHERE namespace=? AND report_id=?",
                                [namespace, report_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "report revision is not visible in this namespace")
        return self._report_view(namespace, row)

    def report_keys(self, namespace: str, *, country_code: str | None = None) -> list[str]:
        if not table_exists(self.conn, "ex_reports"):
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT report_key FROM ex_reports WHERE namespace=? AND (? IS NULL OR country_code=?) "
            "ORDER BY report_key", [namespace, country_code, country_code]).fetchall()]

    def report_as_of(self, namespace: str, report_key: str, *, as_of_ms: int | None = None
                     ) -> tuple[dict[str, Any] | None, str | None]:
        revisions = self.report_revisions(namespace, report_key)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [r for r in revisions if r["release_at_ms"] <= cutoff]
        if eligible:
            return max(eligible, key=lambda r: (r["release_at_ms"], r["revision"])), None
        return None, "no_revision_by_as_of" if revisions else "no_report"

    def payments(self, namespace: str, report_id: str, *, company_keys: Iterable[str] | None = None
                 ) -> list[dict[str, Any]]:
        wanted = None if company_keys is None else set(company_keys)
        rows = self.conn.execute(
            "SELECT line_key, reported_by, level, agency_json, stream_json, company_key, company_json, project_key, "
            "project_json, amount_text, amount, currency, in_kind, budget_reference_json FROM ex_payments WHERE "
            "namespace=? AND report_id=? ORDER BY line_key", [namespace, report_id]).fetchall()
        out = []
        for row in rows:
            if wanted is not None and row[5] not in wanted:
                continue
            line = {"record_type": "payment", "report_id": report_id, "line_key": row[0], "reported_by": row[1],
                    "level": row[2], "agency": load(row[3], {}), "revenue_stream": load(row[4], {}),
                    "company_key": row[5], "company": load(row[6], None), "project_key": row[7],
                    "project": load(row[8], None), "amount_text": row[9], "amount": row[10], "currency": row[11],
                    "in_kind": bool(row[12]), "budget_reference": load(row[13], None)}
            line["identity"] = _line_identity(line, row[5], row[7])
            out.append(line)
        return out

    def discrepancies(self, namespace: str, report_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT line, company_key, company_json, stream_json, government_amount_text, company_amount_text, "
            "discrepancy_text, government_amount, company_amount, discrepancy, currency, explanation, basis FROM "
            "ex_discrepancies WHERE namespace=? AND report_id=? ORDER BY line", [namespace, report_id]).fetchall()
        keys = ("line", "company_key", "company", "revenue_stream", "government_amount_text", "company_amount_text",
                "discrepancy_text", "government_amount", "company_amount", "discrepancy", "currency", "explanation",
                "basis")
        out = []
        for row in rows:
            view = dict(zip(keys, row))
            view["company"] = load(view["company"], None)
            view["revenue_stream"] = load(view["revenue_stream"], {})
            out.append({"record_type": "discrepancy", "report_id": report_id, **view})
        return out

    def companies(self, namespace: str) -> list[dict[str, Any]]:
        """Every reporting company of every report revision, as reported (the identity subjects)."""
        if not table_exists(self.conn, "ex_payments"):
            return []
        found: dict[str, dict[str, Any]] = {}
        for key, company, report_id, country in self.conn.execute(
                "SELECT p.company_key, p.company_json, p.report_id, r.country_json FROM ex_payments p JOIN ex_reports "
                "r ON r.namespace=p.namespace AND r.report_id=p.report_id WHERE p.namespace=? AND p.company_key IS "
                "NOT NULL ORDER BY p.report_id, p.line_key", [namespace]).fetchall():
            body = json.loads(company)
            entry = found.setdefault(key, {"key": key, **body, "country": json.loads(country), "report_ids": []})
            if report_id not in entry["report_ids"]:
                entry["report_ids"].append(report_id)
        return [found[k] for k in sorted(found)]

    def projects(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ex_payments"):
            return []
        found: dict[str, dict[str, Any]] = {}
        for key, project, report_id, country in self.conn.execute(
                "SELECT p.project_key, p.project_json, p.report_id, r.country_json FROM ex_payments p JOIN ex_reports "
                "r ON r.namespace=p.namespace AND r.report_id=p.report_id WHERE p.namespace=? AND p.project_key IS "
                "NOT NULL ORDER BY p.report_id, p.line_key", [namespace]).fetchall():
            entry = found.setdefault(key, {"key": key, **json.loads(project), "country": json.loads(country),
                                           "report_ids": []})
            if report_id not in entry["report_ids"]:
                entry["report_ids"].append(report_id)
        return [found[k] for k in sorted(found)]

    # ------------------------------------------------------------------ reads: series

    _SERIES_COLUMNS = ("series_id, provider, commodity_key, commodity_json, statistic, unit_json, country_json, "
                       "definition_json, references_json, economic_indicator_id, first_release_id")

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, ckey, commodity, statistic, unit, country, definition, references, indicator,
         first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        return {"contract": CONTRACT, "record_type": "commodity_series", "namespace": namespace,
                "series_id": series_id, "provider": provider, "commodity_key": ckey, "commodity": load(commodity, {}),
                "statistic": statistic, "unit": load(unit, {}), "country": load(country, {}),
                "definition": load(definition, {}), "references": load(references, []),
                "economic_series": {"domain": DOMAIN, "series_id": series_id, "indicator_id": indicator},
                "first_release_id": first_release, "vintage_count": len(vintages),
                "current_vintage_id": vintages[-1]["vintage_id"] if vintages else None}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._SERIES_COLUMNS} FROM ex_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, statistic: str | None = None,
                    commodity_keys: Iterable[str] | None = None, country_names: Iterable[str] | None = None,
                    limit: int = 1000) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ex_series"):
            return []
        rows = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS}, country_name FROM ex_series WHERE namespace=? AND "
            "(? IS NULL OR provider=?) AND (? IS NULL OR statistic=?) ORDER BY provider, commodity_key, statistic, "
            "country_name, series_id", [namespace, provider, provider, statistic, statistic]).fetchall()
        commodities = None if commodity_keys is None else set(commodity_keys)
        countries = None if country_names is None else {str(c).casefold() for c in country_names}
        out = []
        for row in rows:
            if commodities is not None and row[2] not in commodities:
                continue
            if countries is not None and str(row[-1]).casefold() not in countries:
                continue
            out.append(self._series_view(namespace, row[:-1]))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, economic_as_of, release_at_ms, release_basis, retrieved_at_ms, "
            "content_hash, publication_json, changes_json, sequence FROM ex_vintages WHERE namespace=? AND "
            "series_id=? ORDER BY release_at_ms, sequence", [namespace, series_id]).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(zip(("vintage_id", "release_id", "economic_as_of", "release_at_ms", "release_basis",
                             "retrieved_at_ms", "content_hash", "publication", "changes", "sequence"), row))
            view["publication"] = load(view["publication"], {})
            view["changes"] = load(view["changes"], {})
            view["series_id"] = series_id
            view["release_at"] = iso_from_ms(view["release_at_ms"])
            view["retrieved_at"] = iso_from_ms(view["retrieved_at_ms"])
            view["revision_of"] = None if previous is None else previous["vintage_id"]
            view["economic_vintage_id"] = f"{series_id}@{view['economic_as_of']}"
            out.append(view)
            previous = view
        return out

    def observations(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        vintage = self.conn.execute("SELECT series_id, economic_as_of FROM ex_vintages WHERE namespace=? AND "
                                    "vintage_id=?", [namespace, vintage_id]).fetchone()
        if vintage is None:
            return []
        numeric = {r[0]: r[1] for r in self.conn.execute(
            "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?",
            [vintage[0], vintage[1]]).fetchall()} if table_exists(self.conn, "dataset_observations") else {}
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, estimated, revised, flags_json, notes_json FROM "
            "ex_observations WHERE namespace=? AND vintage_id=? ORDER BY period", [namespace, vintage_id]).fetchall()
        return [{"record_type": "observation", "period": r[0], "value_text": r[1], "value": r[2],
                 "numeric_value": numeric.get(r[0]) if r[3] == "reported" else None, "status": r[3],
                 "estimated": bool(r[4]), "revised": bool(r[5]), "flags": load(r[6], {}), "notes": load(r[7], [])}
                for r in rows]

    def select_vintage(self, namespace: str, series_id: str, *, as_of_ms: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT series_id FROM ex_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "vintage is not visible in this namespace")
        view = next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)
        return {"contract": CONTRACT, "record_type": "vintage", "namespace": namespace, **view,
                "source_revision": self.source_revision(namespace, view["release_id"])}


class ExtractivesProjector:
    """Source-pack runtime projector for ``noesis-extractives-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = ExtractivesStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("extractives") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("extractives_release") or {}), item.get("extractives_item")
            if not header or not isinstance(body, Mapping):
                raise ExtractivesError("invalid_record", "page record is not an extractives release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM ex_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [self._namespace(source), source["source_id"]]
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_published_on": row[1] if row else None}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the extractives record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "extractives-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": "economics.extractives",
        "dependencies": [], "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "extractives-schema:extractives-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.extractives_sources import FEATURES, PROVIDER_FEATURES

    store = ExtractivesStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "ex_releases")
    selected = selected_features(conn)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = int(conn.execute("SELECT count(*) FROM ex_releases WHERE provider=?", [provider]).fetchone()[0]) \
            if ready else 0
        providers[provider] = {"feature": PROVIDER_FEATURES[provider], "delivers": contract["delivers"],
                               "access_decision": contract["access_decision"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases}
    return {
        "features": {feature: feature in selected for feature in FEATURES},
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations",
        "providers": providers,
        "minimisation": MINIMISATION,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "answer_contract": ANSWER_CONTRACT,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
                "is live until a dated run verifies it",
    }


__all__ = ["TABLES", "ExtractivesProjector", "ExtractivesStore", "readiness", "register_schemas"]

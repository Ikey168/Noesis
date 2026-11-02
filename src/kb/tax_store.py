"""Append-only store for tax transparency series, editions and public CbCR references (#2889).

Follows the tourism, labour and business series stores. Tables (all ``tax_*``, namespace-scoped):

* ``tax_releases`` - one row per acquired **edition**: provider, declared document and its document key, edition label,
  release clock (``declared_release`` or ``retrieval_time``), retrieval clock, digests, evidence origin, run id;
* ``tax_series`` - one row per series key (:func:`src.ingestion.tax_sources.series_key`): dataset, indicator,
  jurisdiction, counterpart grouping, unit with its multiplier, frequency and nature;
* ``tax_vintages`` - one row per edition of a series, never overwritten: release and retrieval clocks, edition, the
  definition and the method in force, recorded changes and (published statistics only) the Economics vintage it lives
  in;
* ``tax_observations`` - values as published per vintage and period, status and status letters verbatim;
* ``tax_release_members`` - which series each edition stated, so a complete edition that no longer states a series
  records a ``removed_by_source`` vintage instead of a deletion;
* ``tax_receipts`` - one receipt per applied edition or failure (provider state is read from them);
* ``tax_company_reports`` - link-only public CbCR references, one revision per change; never a figure.

Published statistics also live in the Economics series storage through
:func:`src.domains.economic.model.register_series`; **model estimates do not**, so they cannot surface as observations.
An edition cannot be dated after its retrieval. As-of reads select the vintage of the edition published on or before
the requested instant and say which. Nothing is overwritten, filled, converted, blended, ranked or derived.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.ingestion.tax_sources import (
    FORMATS,
    LIVE_VERIFICATION,
    REFERENCE_CAP,
    series_key,
)
from src.kb.tax_records import (
    ANSWER_CONTRACT,
    CONTRACT,
    ECONOMIC_DOMAIN,
    READ_SCOPE,
    WRITE_SCOPE,
    TaxError,
    authorize,
    canonical,
    check_item,
    check_reference,
    digest,
    iso,
    load,
    table_exists,
    to_ms,
)

_DDL = """
CREATE TABLE IF NOT EXISTS tax_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_key TEXT NOT NULL, document_json TEXT NOT NULL, release_label TEXT, edition TEXT NOT NULL,
  published_on TEXT, release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL, complete BOOLEAN NOT NULL,
  evidence_origin TEXT NOT NULL, url TEXT, run_id TEXT NOT NULL, recorded_by TEXT, sequence INTEGER NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS tax_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
  native_key TEXT NOT NULL, key_json TEXT NOT NULL, nature TEXT NOT NULL, indicator_json TEXT NOT NULL,
  concept TEXT NOT NULL, jurisdiction_scheme TEXT NOT NULL, jurisdiction_code TEXT NOT NULL,
  jurisdiction_json TEXT NOT NULL, counterpart_json TEXT NOT NULL, unit_json TEXT NOT NULL, frequency TEXT NOT NULL,
  document_key TEXT NOT NULL, economic_indicator_id TEXT, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS tax_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  sequence INTEGER NOT NULL, status TEXT NOT NULL, edition TEXT NOT NULL, release_at_ms BIGINT NOT NULL,
  release_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, economic_as_of BIGINT, content_hash TEXT NOT NULL,
  definition_json TEXT NOT NULL, method_json TEXT, release_label TEXT, previous_vintage_id TEXT,
  changes_json TEXT NOT NULL, notes_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS tax_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, flags_json TEXT NOT NULL, flag_meanings_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS tax_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS tax_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
CREATE TABLE IF NOT EXISTS tax_company_reports (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, reference_key TEXT NOT NULL, revision INTEGER NOT NULL,
  company_name TEXT NOT NULL, identifiers_json TEXT NOT NULL, fiscal_year_start TEXT NOT NULL,
  fiscal_year_end TEXT NOT NULL, report_url TEXT NOT NULL, published_on TEXT NOT NULL, channel TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
"""
REFERENCE_PREFIX = "tax-cbcr:report:"
_SERIES_COLUMNS = ("series_id, provider, dataset, native_key, key_json, nature, indicator_json, jurisdiction_json, "
                   "counterpart_json, unit_json, frequency, document_key, economic_indicator_id, first_release_id")
_VINTAGE_COLUMNS = ("vintage_id, series_id, release_id, sequence, status, edition, release_at_ms, release_basis, "
                    "retrieved_at_ms, economic_as_of, content_hash, definition_json, method_json, release_label, "
                    "previous_vintage_id, changes_json, notes_json")
_VINTAGE_KEYS = ("vintage_id", "series_id", "release_id", "sequence", "status", "edition", "release_at_ms",
                 "release_basis", "retrieved_at_ms", "economic_as_of", "content_hash", "definition", "method",
                 "release_label", "previous_vintage_id", "changes", "source_notes")


def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("period", "value_text", "value", "status", "flags")
    return [{k: o.get(k) for k in keys} for o in sorted(observations, key=lambda o: o["period"])]


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "unnamed"


def citation(series: Mapping[str, Any], vintage: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    """What every answer cites for one value set: source, edition, record revision (vintage) and as-of time."""
    return {
        "provider": series["provider"], "source_id": release.get("source_id"), "series_id": series["series_id"],
        "dataset": series["dataset"], "native_key": series["native_key"], "nature": series["nature"],
        "edition": vintage["edition"], "vintage_id": vintage["vintage_id"], "release_id": release["release_id"],
        "release_label": release.get("release_label"), "release_basis": release["release_basis"],
        "as_of": vintage["release_at"], "retrieved_at": vintage["retrieved_at"], "url": release.get("url"),
        "file_sha256": release["file_sha256"], "evidence_origin": release["evidence_origin"], "contract": CONTRACT,
        "live_verification": LIVE_VERIFICATION[series["provider"]]["status"],
    }


class TaxStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "tax_vintages")

    # ------------------------------------------------------------------ writes

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                      retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one edition: series, vintages and removals; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if FORMATS.get(str(fmt), {}).get("provider") != provider:
            raise TaxError("invalid_release", "a release names a known provider and its format")
        if not header.get("edition"):
            raise TaxError("invalid_release", "a release states its edition")
        if int(header.get("item_count", -1)) != len(items):
            raise TaxError("incomplete_release", "a release carries every item it states")
        for item in items:
            # The whole edition is refused when one item breaks the record rules or the minimisation decision.
            check_item(item)
            if item["provider"] != provider or item["edition"] != header["edition"]:
                raise TaxError("invalid_release", "every item of a release belongs to its provider and edition")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        if header.get("published_on"):
            clock, basis = int(to_ms(header["published_on"])), header.get("release_basis") or "declared_release"
        else:
            clock, basis = retrieved, "retrieval_time"
        if clock > retrieved:
            raise TaxError("invalid_release", "an edition cannot be dated after its retrieval")
        release_id = "tax-release:" + digest([namespace, header["document_key"], header["edition"],
                                              header["file_sha256"], canonical(header.get("document") or {})])[:24]
        if self.conn.execute("SELECT 1 FROM tax_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            self._receipt(namespace, run_id, source_id, provider, "unchanged", header, retrieved,
                          {"release_id": release_id})
            return {"release_id": release_id, "status": "unchanged", "vintages": 0, "unchanged_series": 0,
                    "removed_series": 0}
        self.conn.execute("BEGIN")
        try:
            sequence = 1 + int(self.conn.execute(
                "SELECT count(*) FROM tax_releases WHERE namespace=? AND document_key=?",
                [namespace, header["document_key"]]).fetchone()[0])
            previous = self.conn.execute(
                "SELECT release_id FROM tax_releases WHERE namespace=? AND document_key=? AND complete "
                "ORDER BY sequence DESC LIMIT 1", [namespace, header["document_key"]]).fetchone()
            self.conn.execute(
                "INSERT INTO tax_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, release_id, provider, source_id, fmt, header["document_key"],
                 canonical(header.get("document") or {}), header.get("release_label"), header["edition"],
                 header.get("published_on"), basis, clock, retrieved, header["file_sha256"],
                 header.get("content_sha256") or "", len(items), bool(header.get("complete", True)),
                 self._origin(header), header.get("url"), run_id, principal_id, sequence])
            counts = {"new": 0, "unchanged": 0, "removed": 0}
            stated: set[str] = set()
            for item in items:
                series_id = self._series(namespace, item, header, release_id)
                if series_id in stated:
                    raise TaxError("invalid_release", "a release states the same series twice")
                stated.add(series_id)
                vintage_id, status = self._vintage(namespace, series_id, item, header, release_id, clock, basis,
                                                   retrieved)
                counts[status] += 1
                self.conn.execute("INSERT INTO tax_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, series_id, vintage_id, status])
            if previous and header.get("complete", True):
                for (series_id,) in self.conn.execute(
                        "SELECT series_id FROM tax_release_members WHERE namespace=? AND release_id=? "
                        "AND status <> 'removed' ORDER BY series_id", [namespace, previous[0]]).fetchall():
                    if series_id in stated:
                        continue
                    vintage_id = self._removal(namespace, series_id, header, release_id, clock, basis, retrieved)
                    if vintage_id:
                        counts["removed"] += 1
                        self.conn.execute("INSERT INTO tax_release_members VALUES (?,?,?,?,?)",
                                          [namespace, release_id, series_id, vintage_id, "removed"])
            self._receipt(namespace, run_id, source_id, provider, "applied", header, retrieved,
                          {"release_id": release_id, **counts})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "edition": header["edition"],
                "published_on": header.get("published_on"), "release_basis": basis, "vintages": counts["new"],
                "unchanged_series": counts["unchanged"], "removed_series": counts["removed"]}

    @staticmethod
    def _origin(header: Mapping[str, Any]) -> str:
        origin = header.get("evidence_origin")
        return origin if origin in {"fixture", "operator"} else "live"

    def _series(self, namespace, item, header, release_id) -> str:
        key = series_key(item)
        series_id = f"tax:{item['provider']}:" + digest(key)[:20]
        if not self.conn.execute("SELECT 1 FROM tax_series WHERE namespace=? AND series_id=?",
                                 [namespace, series_id]).fetchone():
            jurisdiction = dict(item["jurisdiction"])
            economic = ("tax-indicator:" + series_id.split(":", 2)[2]) if item["nature"] == "published_statistic" \
                else None
            self.conn.execute(
                "INSERT INTO tax_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, series_id, item["provider"], item["dataset"], item["native_key"], canonical(key),
                 item["nature"], canonical(item["indicator"]), item["indicator"]["concept"], jurisdiction["scheme"],
                 str(jurisdiction["code"]), canonical(jurisdiction), canonical(item["counterpart"]),
                 canonical(item["unit"]), item["frequency"], header["document_key"], economic, release_id,
                 self.now()])
        return series_id

    def _changes(self, namespace, prior, observations, item) -> dict[str, Any]:
        if prior is None:
            return {"new_series": True, "new_periods": sorted(o["period"] for o in observations)}
        before = {o["period"]: o for o in self.observations(namespace, prior["vintage_id"])}
        after = {o["period"]: o for o in _content(observations)}
        compared = ("value_text", "value", "status", "flags")
        revised = [{"period": p, "before": {k: before[p].get(k) for k in compared},
                    "after": {k: after[p].get(k) for k in compared}}
                   for p in sorted(set(before) & set(after))
                   if any(before[p].get(k) != after[p].get(k) for k in compared)]
        changes: dict[str, Any] = {
            "new_periods": sorted(set(after) - set(before)),
            "dropped_periods": sorted(set(before) - set(after)),
            "revised": revised,
        }
        if prior["status"] == "removed":
            changes["restated_after_removal"] = True
        if (prior.get("method") or None) != (item.get("method") or None):
            changes["method_change"] = {"before": prior.get("method"), "after": item.get("method"),
                                        "edition_before": prior["edition"], "edition_after": item["edition"]}
        if prior.get("definition") != dict(item["definition"]):
            changes["definition_change"] = {
                "changed_fields": sorted(k for k in set(prior["definition"]) | set(item["definition"])
                                         if prior["definition"].get(k) != dict(item["definition"]).get(k))}
        return {k: v for k, v in changes.items() if v}

    def _vintage(self, namespace, series_id, item, header, release_id, clock, basis, retrieved):
        observations = list(item.get("observations") or [])
        content_hash = digest([_content(observations), item.get("method"), item["definition"]])
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is not None and prior["status"] == "published" and prior["content_hash"] == content_hash:
            # An unchanged re-publication adds no vintage: the earlier edition's vintage stays current.
            return prior["vintage_id"], "unchanged"
        if any(v["release_at_ms"] == clock for v in previous):
            raise TaxError("vintage_conflict", "the edition changed values without a new publication date; the stored "
                                               "vintage is kept", series_id=series_id)
        if prior is not None and clock < prior["release_at_ms"]:
            raise TaxError("stale_release", "an edition dated before the series' latest vintage is not appended",
                           series_id=series_id)
        changes = self._changes(namespace, prior, observations, item)
        economic_as_of = None
        if item["nature"] == "published_statistic":
            economic_as_of = self._economic(series_id, item, header, release_id, clock, basis, retrieved, observations)
        vintage_id = "tax-vintage:" + digest([namespace, series_id, release_id, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO tax_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "published", item["edition"], clock,
             basis, retrieved, economic_as_of, content_hash, canonical(dict(item["definition"])),
             canonical(item["method"]) if item.get("method") else None, header.get("release_label"),
             prior["vintage_id"] if prior else None, canonical(changes),
             canonical(list(item.get("source_notes") or [])), self.now()])
        for obs in observations:
            self.conn.execute(
                "INSERT INTO tax_observations VALUES (?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 canonical(dict(obs.get("flags") or {})), canonical(list(obs.get("flag_meanings") or []))])
        return vintage_id, "new"

    def _economic(self, series_id, item, header, release_id, clock, basis, retrieved, observations) -> int:
        """Published statistics only: the values as a vintage in the Economics series storage."""
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        jurisdiction, unit = dict(item["jurisdiction"]), dict(item["unit"])
        record = SeriesRecord(
            series_id=series_id, provider=item["provider"],
            title=str(item["indicator"].get("label") or item["native_key"]), frequency=item["frequency"],
            as_of=int(clock),
            observations=[{"period": o["period"], "value": None if o.get("value") is None
                           else float(Decimal(o["value"]))} for o in sorted(observations, key=lambda o: o["period"])],
            unit=unit.get("label") or unit.get("code"), geography=str(jurisdiction["code"]),
            source_url=header.get("url"),
            metadata={"provider_release_at_ms": int(clock), "provider_release_time_status": f"tax edition ({basis})",
                      "acquired_at_ms": int(retrieved), "vintage_basis": basis, "source_document_id": release_id,
                      "edition": item["edition"]},
        )
        semantics = {
            "indicator_id": "tax-indicator:" + series_id.split(":", 2)[2],
            "canonical_name": str(item["indicator"].get("label") or item["indicator"]["concept"]),
            "concept": f"tax {item['indicator']['concept']} {series_id}",
            "definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "seasonal_adjustment": "not_adjusted",
            "price_basis": "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "attributes": {"tax_series_id": series_id, "nature": item["nature"],
                           "counterpart": dict(item["counterpart"]).get("code"),
                           "unit_multiplier": unit.get("multiplier"), "contract": CONTRACT},
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=ECONOMIC_DOMAIN)
        except EconomicModelError as exc:
            raise TaxError(exc.code, str(exc)) from exc
        return int(economic["vintage"]["as_of"])

    def _removal(self, namespace, series_id, header, release_id, clock, basis, retrieved) -> str | None:
        previous = self.vintage_rows(namespace, series_id)
        prior = previous[-1] if previous else None
        if prior is None or prior["status"] == "removed" or clock <= prior["release_at_ms"]:
            return None
        vintage_id = "tax-vintage:" + digest([namespace, series_id, release_id, "removed"])[:24]
        changes = {"removed_by_source": {"edition": header["edition"],
                                         "statement": "the source's complete edition of this document no longer "
                                                      "states the series; earlier editions stay queryable"}}
        self.conn.execute(
            "INSERT INTO tax_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, 1 + len(previous), "removed", header["edition"], clock,
             basis, retrieved, None, digest("removed"), canonical(prior["definition"]),
             canonical(prior["method"]) if prior.get("method") else None, header.get("release_label"),
             prior["vintage_id"], canonical(changes), canonical([]), self.now()])
        return vintage_id

    def _receipt(self, namespace, run_id, source_id, provider, outcome, header, retrieved, detail):
        body = {"run_id": run_id, "source_id": source_id, "provider": provider, "outcome": outcome,
                "edition": (header or {}).get("edition"), "file_sha256": (header or {}).get("file_sha256"),
                "retrieved_at": iso(retrieved), **detail}
        receipt_id = "tax-receipt:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT INTO tax_receipts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, receipt_id, run_id, source_id, provider, outcome,
                           (header or {}).get("evidence_origin") or "none", canonical(body), self.now()])
        return receipt_id

    def record_failure(self, namespace: str, provider: str, *, code: str, run_id: str, source_id: str | None,
                       scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        receipt_id = self._receipt(namespace, run_id, source_id, provider, "failed", None, self.now(),
                                   {"failure_code": code})
        return {"provider": provider, "failure_code": code, "receipt_id": receipt_id,
                "effect": "stored vintages unchanged; the source reads as stale, nothing is marked removed or revised"}

    # ------------------------------------------------------------------ public CbCR references (link-only)

    @staticmethod
    def reference_key(reference: Mapping[str, Any]) -> str:
        year = dict(reference["fiscal_year"])
        return f"{REFERENCE_PREFIX}{slug(reference['company_name'])}:{year['start']}_{year['end']}"

    def record_company_reports(self, namespace: str, references: Sequence[Mapping[str, Any]], *, principal_id: str,
                               scopes: Iterable[str]) -> dict[str, Any]:
        """Record operator-declared public CbCR references; a changed reference is a new revision, never an
        overwrite. Every reference is checked first and the whole call is refused if one carries a figure."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not references or len(references) > REFERENCE_CAP:
            raise TaxError("invalid_reference", f"between 1 and {REFERENCE_CAP} references per call")
        for reference in references:
            check_reference(reference)
        out = []
        for reference in references:
            body = {k: reference[k] for k in sorted(reference) if k not in {"note"}}
            body["identifiers"] = sorted([{"scheme": str(i["scheme"]), "value": str(i["value"])}
                                          for i in reference.get("identifiers") or []],
                                         key=lambda i: (i["scheme"], i["value"]))
            key = self.reference_key(reference)
            content_hash = digest(body)
            latest = self.conn.execute(
                "SELECT revision_id, content_hash, revision FROM tax_company_reports WHERE namespace=? AND "
                "reference_key=? ORDER BY revision DESC LIMIT 1", [namespace, key]).fetchone()
            if latest and latest[1] == content_hash:
                out.append({"reference_key": key, "revision_id": latest[0], "status": "unchanged"})
                continue
            revision = 1 + (int(latest[2]) if latest else 0)
            revision_id = f"{key}:r{revision}"
            year = dict(reference["fiscal_year"])
            self.conn.execute(
                "INSERT INTO tax_company_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, revision_id, key, revision, str(reference["company_name"]),
                 canonical(body["identifiers"]), str(year["start"]), str(year["end"]), str(reference["report_url"]),
                 str(reference["published_on"]), str(reference["channel"]), canonical(body), content_hash,
                 principal_id, self.now()])
            out.append({"reference_key": key, "revision_id": revision_id,
                        "status": "recorded" if revision == 1 else "revised"})
        return {"references": out, "status": "reference-only",
                "note": "link-only references; no company-level figure is stored until a machine-readable source "
                        "exists"}

    def _report_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (revision_id, key, revision, name, identifiers, start, end, url, published_on, channel, content,
         recorded_by, recorded_at) = row
        body = load(content, {})
        return {"contract": CONTRACT, "record_type": "company_report", "namespace": namespace,
                "reference_key": key, "revision_id": revision_id, "revision": revision,
                "company_name_as_published": name, "identifiers": load(identifiers, []),
                "fiscal_year": {"start": start, "end": end}, "report_url": url, "published_on": published_on,
                "channel": channel, "template": body.get("template"), "language": body.get("language"),
                "recorded_by": recorded_by, "recorded_at": iso(recorded_at), "status": "reference-only",
                "figures": "not stored: link-only until a machine-readable source exists",
                "live_verification": LIVE_VERIFICATION["public-cbcr"]["status"]}

    _REPORT_COLUMNS = ("revision_id, reference_key, revision, company_name, identifiers_json, fiscal_year_start, "
                       "fiscal_year_end, report_url, published_on, channel, content_json, recorded_by, recorded_at_ms")

    def company_reports(self, namespace: str, *, reference_key: str | None = None, history: bool = False
                        ) -> list[dict[str, Any]]:
        """Current revision per reference (or every revision with ``history``)."""
        if not table_exists(self.conn, "tax_company_reports"):
            return []
        rows = self.conn.execute(
            f"SELECT {self._REPORT_COLUMNS} FROM tax_company_reports WHERE namespace=? AND (? IS NULL OR "
            "reference_key=?) ORDER BY reference_key, revision", [namespace, reference_key, reference_key]).fetchall()
        views = [self._report_view(namespace, r) for r in rows]
        if history:
            return views
        current: dict[str, dict[str, Any]] = {}
        for view in views:
            current[view["reference_key"]] = view
        return [current[k] for k in sorted(current)]

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        if not table_exists(self.conn, "tax_receipts"):
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        rows = self.conn.execute("SELECT outcome, execution, created_at_ms, run_id, detail_json FROM tax_receipts "
                                 "WHERE namespace=? AND provider=? ORDER BY created_at_ms, receipt_id",
                                 [namespace, provider]).fetchall()
        successes = [r for r in rows if r[0] in {"applied", "unchanged"}]
        if not successes:
            return {"provider": provider, "last_success_ms": None, "stale": True,
                    "reason": "never acquired" if not rows else load(rows[-1][4], {}).get("failure_code")}
        failed = rows[-1][0] == "failed"
        return {"provider": provider, "last_success_ms": int(successes[-1][2]), "last_execution": successes[-1][1],
                "last_run_id": rows[-1][3], "stale": failed,
                "reason": load(rows[-1][4], {}).get("failure_code") if failed else None}

    _RELEASE_KEYS = ("release_id", "provider", "source_id", "format", "document_key", "document", "release_label",
                     "edition", "published_on", "release_basis", "release_at_ms", "retrieved_at_ms", "file_sha256",
                     "content_sha256", "item_count", "complete", "evidence_origin", "url", "run_id", "sequence")

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_key, document_json, release_label, edition, "
            "published_on, release_basis, release_at_ms, retrieved_at_ms, file_sha256, content_sha256, item_count, "
            "complete, evidence_origin, url, run_id, sequence FROM tax_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id]).fetchone()
        if row is None:
            raise TaxError("not_found", "no such release")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = load(view["document"], {})
        view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
        view["live_verification"] = LIVE_VERIFICATION[view["provider"]]["status"]
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "tax_releases"):
            return []
        rows = self.conn.execute("SELECT release_id FROM tax_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
                                 "ORDER BY release_at_ms, release_id", [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [{"series_id": r[0], "vintage_id": r[1], "status": r[2]} for r in self.conn.execute(
            "SELECT series_id, vintage_id, status FROM tax_release_members WHERE namespace=? AND release_id=? "
            "ORDER BY series_id", [namespace, release_id]).fetchall()]

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, dataset, native_key, key, nature, indicator, jurisdiction, counterpart, unit, frequency,
         document_key, economic_indicator, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        return {"contract": CONTRACT, "record_type": "series", "namespace": namespace, "series_id": series_id,
                "provider": provider, "dataset": dataset, "native_key": native_key, "key": load(key, {}),
                "nature": nature, "indicator": load(indicator, {}), "jurisdiction": load(jurisdiction, {}),
                "counterpart": load(counterpart, {}), "unit": load(unit, {}), "frequency": frequency,
                "document_key": document_key, "first_release_id": first_release,
                "economic_series": ({"domain": ECONOMIC_DOMAIN, "series_id": series_id,
                                     "indicator_id": economic_indicator} if economic_indicator else None),
                "vintage_count": len(vintages), "editions": [v["edition"] for v in vintages],
                "current_vintage_id": None if current is None else current["vintage_id"],
                "current_status": None if current is None else current["status"],
                "live_verification": LIVE_VERIFICATION[provider]["status"]}

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_SERIES_COLUMNS} FROM tax_series WHERE namespace=? AND series_id=?",
                                [namespace, series_id]).fetchone() if self.ready() else None
        if row is None:
            raise TaxError("not_found", "no such tax series")
        return self._series_view(namespace, row)

    def find_series(self, namespace: str, *, provider: str | None = None, concept: str | None = None,
                    nature: str | None = None, jurisdiction: str | None = None) -> list[dict[str, Any]]:
        """Series by provider, concept, nature and jurisdiction code."""
        if not self.ready() or not table_exists(self.conn, "tax_series"):
            return []
        rows = self.conn.execute(
            f"SELECT {_SERIES_COLUMNS} FROM tax_series WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR concept=?) AND (? IS NULL OR nature=?) AND (? IS NULL OR jurisdiction_code=?) "
            "ORDER BY provider, concept, jurisdiction_code, native_key, series_id",
            [namespace, provider, provider, concept, concept, nature, nature, jurisdiction, jurisdiction]).fetchall()
        return [self._series_view(namespace, row) for row in rows]

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {_VINTAGE_COLUMNS} FROM tax_vintages WHERE namespace=? AND series_id=? "
                                 "ORDER BY sequence", [namespace, series_id]).fetchall()
        out = []
        for row in rows:
            view = dict(zip(_VINTAGE_KEYS, row))
            view["namespace"] = namespace
            view["definition"] = load(view["definition"], {})
            view["method"] = load(view["method"], None)
            view["changes"] = load(view["changes"], {})
            view["source_notes"] = load(view["source_notes"], [])
            view["release_at"], view["retrieved_at"] = iso(view["release_at_ms"]), iso(view["retrieved_at_ms"])
            view["revision_of"] = view["previous_vintage_id"]
            view["economic_vintage_id"] = (f"{series_id}@{view['economic_as_of']}"
                                           if view["economic_as_of"] is not None else None)
            out.append(view)
        return out

    def observations(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, flags_json, flag_meanings_json FROM tax_observations WHERE "
            "namespace=? AND vintage_id=? ORDER BY period", [namespace, vintage_id]).fetchall()
        return [{"period": r[0], "value_text": r[1], "value": r[2], "status": r[3], "flags": load(r[4], {}),
                 "flag_meanings": load(r[5], [])} for r in rows]

    def select_vintage(self, namespace: str, series_id: str, *, as_of_ms: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage of the edition published on or before the cutoff; the reason when there is none."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_edition_by_as_of" if vintages else "no_vintage"

    def values(self, namespace: str, series_id: str, *, as_of_ms: int | None = None,
               vintage_id: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise TaxError("not_found", "vintage does not belong to this series")
        else:
            vintage, reason = self.select_vintage(namespace, series_id, as_of_ms=as_of_ms)
        if vintage is None:
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "unavailable", "reason": reason,
                    "observations": []}
        release = self.release(namespace, vintage["release_id"])
        if vintage["status"] == "removed":
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "removed_by_source", "vintage": vintage,
                    "citation": citation(series, vintage, release), "observations": [],
                    "note": "the source's edition in force at this date no longer states the series; earlier editions "
                            "remain in its history"}
        return {"contract": ANSWER_CONTRACT, "series": series, "status": "available", "vintage": vintage,
                "release": release, "observations": self.observations(namespace, vintage["vintage_id"]),
                "citation": citation(series, vintage, release)}

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "tax_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source_id, provider, outcome, execution, detail_json FROM "
                                 "tax_receipts WHERE namespace=? ORDER BY created_at_ms, receipt_id",
                                 [namespace]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "execution": r[5], "detail": load(r[6], {})} for r in rows]


class TaxProjector:
    """Source-pack runtime projector for ``noesis-tax-transparency-record-v2`` pages (one edition per page).

    Retrieval is stamped at the store's clock (the real clock in the runtime), so an edition is never dated after it.
    """

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = TaxStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("tax_transparency") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt, documents
        namespace = self._namespace(source)
        releases: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, body = record.get("tax_release"), record.get("tax_item")
            if not header or not isinstance(body, Mapping):
                raise TaxError("invalid_record", "page record is not a tax release item")
            releases.setdefault(header["file_sha256"] + canonical(header.get("document")),
                                (dict(header), []))[1].append(dict(body))
        return [self.store.apply_release(namespace, header, items, source_id=source.get("source_id"), run_id=run_id,
                                         principal_id=principal_id or "source-runtime",
                                         scopes=self.scopes_for(namespace))
                for header, items in releases.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("tax_transparency") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + str(status), run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["REFERENCE_PREFIX", "TaxProjector", "TaxStore", "citation", "slug"]

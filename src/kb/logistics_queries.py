"""Port, country and route logistics series as of a vintage (#2229, SL09 #2546).

Given a port (a UN/LOCODE or a source code ``scheme:code``), a country code or a published route (two port codes)
and an as-of date, the answer lists every logistics series published for it with the vintage released by that date
(``release_at <= as_of``), the values as published with status, flags and footnotes, definition, unit, frequency,
licence (and the freight-index licence decision), breaks and a citation of the source release. Series of different
sources for similar concepts are grouped side by side by concept and never merged. A port reaches the series of its
own source codes only through **accepted** identity matches (:mod:`src.kb.logistics_ports`); a route is answered only
from series published at route level; a port, country or route without series is ``none_on_record``. Nothing is
forecast, derived, rebased or interpolated.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Any

from src.ingestion.logistics_sources import EXCLUSIONS, coverage_report
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    LogisticsError,
    authorize,
    iso_from_ms,
    table_exists,
)
from src.kb.logistics_series import LogisticsStore

SIDE_BY_SIDE = "series of different sources are listed side by side with their definitions and never merged"
UNLOCODE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}$")


def _parse_code(value: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    if not raw:
        raise LogisticsError("invalid_request", "a port, country or route end is required")
    if ":" in raw:
        scheme, code = raw.split(":", 1)
        return scheme.strip(), code.strip()
    compact = raw.replace(" ", "").upper()
    if UNLOCODE.match(compact):
        return "unlocode", compact
    return None, raw


class LogisticsQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = LogisticsStore(conn, initialize=False)
        self.ports = LogisticsPorts(conn, initialize=False)
        self.now = now or (lambda: int(time.time() * 1000))

    def _ready(self, namespace: str, scopes: Iterable[str]) -> None:
        authorize(namespace, scopes, READ_SCOPE)
        if not self.store.ready():
            raise LogisticsError("not_ready", "no logistics record has been acquired yet")

    def _entry(self, namespace: str, series: dict[str, Any], as_of_ms: int | None,
               all_vintages: bool) -> dict[str, Any]:
        vintage = self.store.select_vintage(namespace, series["series_id"], as_of_ms)
        vintages = self.store.vintage_rows(namespace, series["series_id"])
        entry = {
            "series_id": series["series_id"],
            "provider": series["provider"],
            "source_series_id": series["source_series_id"],
            "dataset": series["dataset"],
            "concept": series["concept"],
            "measure_label": series["measure_label"],
            "definition": series["definition"],
            "unit": series["unit"],
            "frequency": series["frequency"],
            "geography": series["geography"],
            "partner": series["partner"],
            "licence": series["licence"],
            "freight_index": series["freight_index"],
            "breaks": self.store.breaks(namespace, series["series_id"]),
            "later_vintages": len([v for v in vintages if vintage and v["release_at_ms"] > vintage["release_at_ms"]]),
        }
        if vintage is None:
            entry.update({"status": "not_yet_released", "vintage": None, "values": [], "citation": None})
        else:
            entry.update({
                "status": "answered",
                "vintage": {k: vintage[k] for k in ("vintage_id", "release_at", "release_basis", "retrieved_at",
                                                    "revision_of", "economic_vintage_id")},
                "values": self.store.values(namespace, vintage["vintage_id"]),
                "citation": self.store.source_revision(namespace, vintage["release_id"]),
            })
        if all_vintages:
            entry["vintages"] = [{k: v[k] for k in ("vintage_id", "release_at", "release_basis", "revision_of")}
                                 for v in vintages]
        return entry

    def _answer(self, namespace, query, as_of_ms, series, all_vintages, extra=None) -> dict[str, Any]:
        entries = [self._entry(namespace, s, as_of_ms, all_vintages) for s in series]
        answered = [e for e in entries if e["status"] == "answered"]
        by_concept: dict[str, list[str]] = {}
        for entry in answered:
            by_concept.setdefault(entry["concept"], []).append(entry["series_id"])
        return {
            "contract": ANSWER_CONTRACT,
            "namespace": namespace,
            "query": query,
            "as_of": iso_from_ms(as_of_ms),
            "status": "answered" if answered else "none_on_record",
            "series": entries,
            "side_by_side": {"by_concept": by_concept, "note": SIDE_BY_SIDE},
            "citations": sorted({e["citation"]["release_id"] for e in answered}),
            "exclusions": list(EXCLUSIONS),
            **(extra or {}),
        }

    def port(self, namespace: str, port: str, *, scopes: Iterable[str], as_of_ms: int | None = None,
             all_vintages: bool = False) -> dict[str, Any]:
        """A UN/LOCODE (with the series of every accepted source code) or a source port code as of a date."""
        self._ready(namespace, scopes)
        scheme, code = _parse_code(port)
        identity: list[dict[str, Any]] = []
        record = None
        if scheme == "unlocode":
            day = iso_from_ms(as_of_ms)[:10] if as_of_ms is not None else None
            record = self.ports.port(namespace, code, as_of_day=day)
            codes = [("unlocode", code)]
            identity.append({"scheme": "unlocode", "code": code, "basis": "query"})
            for source in self.ports.source_codes(namespace, code):
                codes.append((source["scheme"], source["code"]))
                identity.append(source)
            series = [s for s in self.store.find_series(namespace, codes=codes) if s["geography"]["kind"] == "port"]
            for s in series:
                if s["geography"].get("unlocode") == code:
                    s["identity_basis"] = "embedded-unlocode"
        else:
            codes = [(scheme, code)]
            resolved = self.ports.resolve_source(namespace, scheme, code) if scheme else {"status": "unmatched"}
            identity.append({"scheme": scheme, "code": code, **resolved})
            series = [s for s in self.store.find_series(namespace, codes=codes) if s["geography"]["kind"] == "port"]
        answer = self._answer(namespace, {"port": port}, as_of_ms, series, all_vintages,
                              {"port": record, "identity": identity})
        if record is None and scheme == "unlocode" and not self.ports.port_history(namespace, code):
            answer["port_note"] = "no UN/LOCODE record of this code is held"
        for entry, s in zip(answer["series"], series):
            entry["identity_basis"] = s.get("identity_basis") or next(
                (i.get("basis") or i.get("status") for i in identity if i.get("code") == s["geography"]["code"]
                 and i.get("scheme") == s["geography"]["scheme"]), None)
        return answer

    def country(self, namespace: str, country: str, *, scopes: Iterable[str], as_of_ms: int | None = None,
                all_vintages: bool = False) -> dict[str, Any]:
        """Country-level series under the published code (M49, Eurostat GEO or ISO alpha-2); ports on record in the
        country are listed as context, their series are not aggregated into a country figure."""
        self._ready(namespace, scopes)
        scheme, code = _parse_code(country)
        series = self.store.find_series(namespace, geo_kind="country", codes=[(scheme, code)])
        ports = [p["unlocode"] for p in self.ports.ports(namespace, country=code.upper())] if len(code) == 2 else []
        return self._answer(namespace, {"country": country}, as_of_ms, series, all_vintages,
                            {"ports_on_record": ports,
                             "note": "port series are not summed into country figures"})

    def route(self, namespace: str, origin: str, destination: str, *, scopes: Iterable[str],
              as_of_ms: int | None = None, all_vintages: bool = False) -> dict[str, Any]:
        """Only series published at route level with both ends as published (or accepted matches of them)."""
        self._ready(namespace, scopes)

        def ends(value):
            scheme, code = _parse_code(value)
            out = [(scheme, code)]
            if scheme == "unlocode":
                out += [(s["scheme"], s["code"]) for s in self.ports.source_codes(namespace, code)]
            return out

        series = self.store.find_series(namespace, geo_kind="route", codes=ends(origin), partner_codes=ends(destination))
        answer = self._answer(namespace, {"origin": origin, "destination": destination}, as_of_ms, series,
                              all_vintages)
        answer["note"] = "routes are answered only from series published at route level; never inferred from port totals"
        return answer

    def value_history(self, namespace: str, series_id: str, period: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every vintage's value of one period, each cited."""
        self._ready(namespace, scopes)
        series = self.store.series(namespace, series_id)
        history = []
        for vintage in self.store.vintage_rows(namespace, series_id):
            value = next((v for v in self.store.values(namespace, vintage["vintage_id"]) if v["period"] == period), None)
            history.append({"vintage_id": vintage["vintage_id"], "release_at": vintage["release_at"],
                            "release_basis": vintage["release_basis"], "revision_of": vintage["revision_of"],
                            "value": value, "citation": self.store.source_revision(namespace, vintage["release_id"])})
        return {"contract": ANSWER_CONTRACT, "series_id": series_id, "concept": series["concept"], "period": period,
                "status": "answered" if any(h["value"] for h in history) else "none_on_record",
                "history": history, "exclusions": list(EXCLUSIONS)}

    def freight_indices(self, namespace: str, *, scopes: Iterable[str], as_of_ms: int | None = None) -> dict:
        """In-scope freight-index series as of a date, and every excluded index with its licence reason."""
        authorize(namespace, scopes, READ_SCOPE)
        series = [s for s in self.store.find_series(namespace) if s["freight_index"]] \
            if table_exists(self.conn, "logistics_series") else []
        answer = self._answer(namespace, {"freight_indices": True}, as_of_ms, series, False)
        answer["excluded"] = [i for i in coverage_report()["freight_indices"] if i["decision"] == "excluded"]
        return answer


__all__ = ["LogisticsQueries", "SIDE_BY_SIDE"]

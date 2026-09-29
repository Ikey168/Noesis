"""Verification of published forecasts against recorded observations (WX09, #2172).

Nothing is re-forecast, blended or bias-corrected. A **verification pair** is
one published forecast element matched to one recorded observation under a
stated rule:

* **Station.** The observation's station is the forecast's station, one of the
  stations equivalent to it (source-stated identifiers or an accepted reviewed
  match, :meth:`WeatherStationIdentity.equivalent`), or the station declared
  for a grid point in the source selection.
* **Parameter.** The same common parameter (:data:`weather_normalise.PARAMETERS`)
  after exact unit normalisation. A probability element is paired only through
  a stated event definition (:data:`EVENTS`).
* **Time.** The observation nearest to the valid time within ``tolerance_s``.
  Ties go to the forecast's own station, then to the station key.
* **Exclusions, counted and shown.** Observations flagged ``suspect`` or
  ``failed`` by their publisher's QC. Missing values. Unconvertible units. Any
  issuance acquired at or after the element's valid time: a forecast captured
  after its valid time is never used.

Metrics (definitions returned with every result), per provider, product/model,
lead-time bucket and period (UTC day of the valid time):

* continuous: bias = mean(f − o), MAE = mean(|f − o|), RMSE = sqrt(mean((f − o)²));
* probabilistic (published probabilities only): Brier = mean((p − o)²), with
  p in [0, 1] and o = 1 when the stated event was observed;
* thresholded (user-declared thresholds, never the pack's): hit rate =
  hits / (hits + misses) and false-alarm ratio = false alarms / (hits + false alarms).

Sample sizes and exclusions are always shown. No ranking and no "best provider"
verdict is produced. A run is recorded with a receipt that pins every issuance
and observation revision, and :meth:`ForecastVerification.replay` recomputes it
from those revisions.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

from src.kb import weather_records as wr
from src.kb.weather_identity import WeatherStationIdentity
from src.kb.weather_normalise import EXCLUDED_QC, common_parameter, normalise, qc_common
from src.kb.weather_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.weather_store import (
    WeatherError,
    WeatherStore,
    _table,
    authorize,
    require_ready,
)

CONTRACT = "noesis-weather-verification-v1"
DEFAULT_TOLERANCE_S = 600
LEAD_BUCKETS_H = ((0, 6), (6, 12), (12, 24), (24, 48), (48, 72), (72, 240))
# Published probabilities and the observed event they state (verify the MOSMIX definition).
EVENTS = {
    "probability_precipitation_1h_gt_0.1mm": {
        "observed": "precipitation_1h",
        "op": "gt",
        "threshold": "0.1",
        "unit": "mm",
    },
}
_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}
DEFINITIONS = {
    "bias": "mean(forecast - observation) over the pairs, in the canonical unit",
    "mae": "mean(|forecast - observation|)",
    "rmse": "square root of mean((forecast - observation)^2)",
    "brier": "mean((p - o)^2), p the published probability as a fraction, o 1 if the stated event was observed else 0",
    "hit_rate": "hits / (hits + misses) for a user-declared threshold; null when no event was observed",
    "false_alarm_ratio": "false alarms / (hits + false alarms); null when no event was forecast",
}
_DDL = """
CREATE TABLE IF NOT EXISTS weather_verification_runs(
 run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
 result_hash TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weather_verification_pairs(
 run_id TEXT NOT NULL, pair_index INTEGER NOT NULL, pair_json TEXT NOT NULL, PRIMARY KEY(run_id, pair_index));
"""


def _bucket(lead_s: int) -> str | None:
    hours = Fraction(lead_s, 3600)
    for low, high in LEAD_BUCKETS_H:
        if low <= hours < high:
            return f"{low}-{high}h"
    return None


def _text(value: Fraction, places: int = 6) -> str:
    return format(Decimal(round(value * 10**places)).scaleb(-places).normalize(), "f")


def _sqrt(value: Fraction, places: int = 6) -> str:
    with localcontext() as ctx:
        ctx.prec = 40
        root = (Decimal(value.numerator) / Decimal(value.denominator)).sqrt()
    return _text(Fraction(root), places)


def _threshold(item: Any, index: int) -> dict[str, Any]:
    if (
        not isinstance(item, Mapping)
        or item.get("op") not in _OPS
        or not item.get("parameter")
    ):
        raise WeatherError(
            "invalid_threshold",
            "thresholds are {parameter, op (gt/gte/lt/lte), value, unit}",
        )
    converted = normalise(
        wr.decimal_text(str(item.get("value")), f"thresholds[{index}].value"),
        item.get("unit"),
    )
    if converted is None:
        raise WeatherError(
            "invalid_threshold", "threshold unit is not a supported unit"
        )
    return {
        "parameter": str(item["parameter"]),
        "op": item["op"],
        "value": converted["value"],
        "unit": converted["unit"],
        "declared": {"value": str(item["value"]), "unit": item["unit"]},
        "basis": "user-declared threshold; the pack asserts none",
    }


class ForecastVerification:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        self.conn = conn
        self.store = WeatherStore(conn, initialize=False, now=now)
        self.identity = WeatherStationIdentity(conn, initialize=False, now=now)
        if initialize:
            conn.execute(_DDL)

    # -------------------------------------------------------------- pairing

    def _observations(
        self,
        namespace: str,
        members: list[str],
        scopes: set[str],
        cutoff: int | None,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        rows = []
        for report in self.store.currents(
            namespace,
            record_type="observation_report",
            scopes=scopes,
            cutoff_ms=cutoff,
            subject_keys=members,
            reference_from_ms=start_ms,
            reference_to_ms=end_ms + 1,
        ):
            content = report["content"]
            for item in content["parameters"]:
                rows.append(
                    {
                        "station": report["subject_key"],
                        "observed_at": content["observed_at"],
                        "observed_ms": wr.ms(content["observed_at"]),
                        "revision_id": report["revision_id"],
                        "parameter": common_parameter(
                            content["provider"], item["parameter"]
                        ),
                        "native_parameter": item["parameter"],
                        "value": item.get("value"),
                        "unit": item["unit"],
                        "qc": qc_common(
                            item["qc"]["scheme"],
                            item["qc"].get("native"),
                            raw_text=content.get("raw_text"),
                        ),
                    }
                )
        return rows

    def _pairs(
        self, namespace: str, request: Mapping[str, Any], scopes: set[str]
    ) -> dict[str, Any]:
        station, parameter = request["station"], request["parameter"]
        cutoff = (
            None
            if request["knowledge_cutoff"] is None
            else wr.ms(request["knowledge_cutoff"])
        )
        start, end = wr.ms(request["period_from"]), wr.ms(request["period_to"])
        tolerance_ms = int(request["tolerance_s"]) * 1000
        members = self.identity.equivalent(
            namespace, station, scopes=scopes, cutoff_ms=cutoff
        )["members"]
        observations = self._observations(
            namespace, members, scopes, cutoff, start - tolerance_ms, end + tolerance_ms
        )
        excluded = {
            "captured_after_valid_time": 0,
            "qc_failed_or_suspect": 0,
            "observation_missing": 0,
            "no_observation_within_tolerance": 0,
            "unit_not_convertible": 0,
            "no_stated_event": 0,
        }
        pairs = []
        for item in self.store.currents(
            namespace, record_type="forecast_issuance", scopes=scopes, cutoff_ms=cutoff
        ):
            content = item["content"]
            location = content["location"]
            forecast_station = (
                wr.station_key(location["station"])
                if location.get("station")
                else wr.station_key(location["declared_station"])
                if location.get("declared_station")
                else None
            )
            if forecast_station not in members:
                continue
            if request["providers"] and content["provider"] not in request["providers"]:
                continue
            for element in content["elements"]:
                name = common_parameter(content["provider"], element["parameter"])
                event = (
                    EVENTS.get(name or "") if element["kind"] == "probability" else None
                )
                wanted = event["observed"] if event else name
                if (name != parameter and wanted != parameter) or element.get(
                    "value"
                ) is None:
                    continue
                valid_ms = wr.ms(element["valid_time"])
                if not start <= valid_ms <= end:
                    continue
                if element["kind"] == "probability" and event is None:
                    excluded["no_stated_event"] += 1
                    continue
                if item["retrieved_at_ms"] >= valid_ms or element["lead_time_s"] < 0:
                    excluded["captured_after_valid_time"] += 1
                    continue
                candidates = [
                    o
                    for o in observations
                    if o["parameter"] == wanted
                    and abs(o["observed_ms"] - valid_ms) <= tolerance_ms
                ]
                failing = [o for o in candidates if o["qc"]["common"] in EXCLUDED_QC]
                excluded["qc_failed_or_suspect"] += len(failing)
                usable = [o for o in candidates if o["qc"]["common"] not in EXCLUDED_QC]
                with_value = [o for o in usable if o["value"] is not None]
                if not with_value:
                    excluded[
                        "observation_missing"
                        if usable
                        else "no_observation_within_tolerance"
                    ] += 1
                    continue
                chosen = min(
                    with_value,
                    key=lambda o: (
                        abs(o["observed_ms"] - valid_ms),
                        o["station"] != forecast_station,
                        o["station"],
                    ),
                )
                observed = normalise(chosen["value"], chosen["unit"])
                forecast = (
                    normalise(element["value"], element["unit"])
                    if not event
                    else {"value": element["value"], "unit": element["unit"]}
                )
                if observed is None or forecast is None:
                    excluded["unit_not_convertible"] += 1
                    continue
                if event:
                    target = normalise(event["threshold"], event["unit"])
                    if target["unit"] != observed["unit"]:
                        excluded["unit_not_convertible"] += 1
                        continue
                elif forecast["unit"] != observed["unit"]:
                    excluded["unit_not_convertible"] += 1
                    continue
                rule = (
                    "same-station"
                    if chosen["station"] == forecast_station and location.get("station")
                    else "declared-grid-point"
                    if location.get("declared_station")
                    else "equivalent-station"
                )
                pair = wr.verification_pair(
                    element={
                        "revision_id": item["revision_id"],
                        "parameter": element["parameter"],
                        "valid_time": element["valid_time"],
                        "issued_at": content["issued_at"],
                        "lead_time_s": element["lead_time_s"],
                        "native_value": element["value"],
                        "native_unit": element["unit"],
                    },
                    observation={
                        "revision_id": chosen["revision_id"],
                        "observed_at": chosen["observed_at"],
                        "station": chosen["station"],
                        "native_value": chosen["value"],
                        "native_unit": chosen["unit"],
                        "qc": chosen["qc"],
                    },
                    match_rule=rule,
                    tolerance_s=int(request["tolerance_s"]),
                    parameter_name=parameter,
                    unit=observed["unit"],
                    forecast_value=forecast["value"],
                    observed_value=observed["value"],
                    kind="probability" if event else "value",
                )
                pair.update(
                    provider=content["provider"],
                    product=content["product"],
                    model=content.get("model"),
                    lead_bucket=_bucket(element["lead_time_s"]),
                    period=element["valid_time"][:10],
                    forecast_station=forecast_station,
                    time_offset_s=(chosen["observed_ms"] - valid_ms) // 1000,
                )
                if event:
                    pair["event"] = dict(event)
                pairs.append(pair)
        pairs.sort(
            key=lambda p: (
                p["provider"],
                p["product"],
                p["element"]["issued_at"],
                p["element"]["valid_time"],
            )
        )
        return {"pairs": pairs, "excluded": excluded, "members": members}

    # -------------------------------------------------------------- metrics

    @staticmethod
    def _metrics(
        pairs: list[dict[str, Any]], thresholds: list[dict[str, Any]]
    ) -> dict[str, Any]:
        values = [p for p in pairs if p["kind"] == "value"]
        probabilities = [p for p in pairs if p["kind"] == "probability"]
        out: dict[str, Any] = {"n": len(pairs)}
        if values:
            errors = [
                Fraction(Decimal(p["forecast_value"]))
                - Fraction(Decimal(p["observed_value"]))
                for p in values
            ]
            n = len(errors)
            out.update(
                n_continuous=n,
                bias=_text(sum(errors, Fraction(0)) / n),
                mae=_text(sum((abs(e) for e in errors), Fraction(0)) / n),
                rmse=_sqrt(sum((e * e for e in errors), Fraction(0)) / n),
            )
        if probabilities:
            terms = []
            for p in probabilities:
                event = p["event"]
                occurred = _OPS[event["op"]](
                    Decimal(p["observed_value"]),
                    Decimal(normalise(event["threshold"], event["unit"])["value"]),
                )
                terms.append(
                    (
                        Fraction(Decimal(p["forecast_value"])) / 100
                        - (1 if occurred else 0)
                    )
                    ** 2
                )
            out.update(
                n_probabilistic=len(terms),
                brier=_text(sum(terms, Fraction(0)) / len(terms)),
            )
        tables = []
        for threshold in thresholds:
            hits = misses = false_alarms = negatives = 0
            for p in values:
                limit = Decimal(threshold["value"])
                forecast = _OPS[threshold["op"]](Decimal(p["forecast_value"]), limit)
                observed = _OPS[threshold["op"]](Decimal(p["observed_value"]), limit)
                hits += forecast and observed
                misses += (not forecast) and observed
                false_alarms += forecast and not observed
                negatives += (not forecast) and not observed
            tables.append(
                {
                    "threshold": threshold,
                    "hits": hits,
                    "misses": misses,
                    "false_alarms": false_alarms,
                    "correct_negatives": negatives,
                    "hit_rate": None
                    if hits + misses == 0
                    else _text(Fraction(hits, hits + misses)),
                    "false_alarm_ratio": None
                    if hits + false_alarms == 0
                    else _text(Fraction(false_alarms, hits + false_alarms)),
                }
            )
        if tables:
            out["contingency"] = tables
        return out

    def _result(
        self, namespace: str, request: Mapping[str, Any], paired: Mapping[str, Any]
    ) -> dict[str, Any]:
        thresholds = [
            t for t in request["thresholds"] if t["parameter"] == request["parameter"]
        ]
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for pair in paired["pairs"]:
            model = pair["model"] or pair["product"]
            groups.setdefault(
                (
                    pair["provider"],
                    model,
                    pair["lead_bucket"] or "beyond",
                    pair["period"],
                ),
                [],
            ).append(pair)
        rows = [
            {
                "provider": k[0],
                "product_or_model": k[1],
                "lead_bucket": k[2],
                "period": k[3],
                **self._metrics(v, thresholds),
            }
            for k, v in sorted(groups.items())
        ]
        totals: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for pair in paired["pairs"]:
            totals.setdefault(
                (
                    pair["provider"],
                    pair["model"] or pair["product"],
                    pair["lead_bucket"] or "beyond",
                ),
                [],
            ).append(pair)
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "request": dict(request),
            "n": len(paired["pairs"]),
            "method": "published forecast elements paired with recorded observations; no re-forecasting",
            "assumptions": [
                "only issuances acquired before each element's valid time are used",
                "observations flagged suspect or failed by their publisher are excluded and counted",
                "units converted with exact rational factors before comparison",
                "station equivalence is source-stated or reviewed; the pairing rule is recorded per pair",
                "sample sizes are small; no ranking or best-provider verdict is implied",
            ],
            "definitions": DEFINITIONS,
            "stations": paired["members"],
            "excluded": paired["excluded"],
            "by_group": rows,
            "by_provider_and_lead": [
                {
                    "provider": k[0],
                    "product_or_model": k[1],
                    "lead_bucket": k[2],
                    **self._metrics(v, thresholds),
                }
                for k, v in sorted(totals.items())
            ],
            "pairs": paired["pairs"],
            "ranking": None,
            "notice": "a comparison of published forecasts with recorded observations; not a forecast",
        }

    # ------------------------------------------------------------------ API

    def verify(
        self,
        namespace: str,
        *,
        station: str,
        parameter: str,
        period_from: str,
        period_to: str,
        principal_id: str,
        scopes: Iterable[str],
        tolerance_s: int = DEFAULT_TOLERANCE_S,
        providers: Iterable[str] | None = None,
        thresholds: Iterable[Mapping[str, Any]] | None = None,
        knowledge_cutoff: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(
            namespace, scopes, WRITE_SCOPE if record else READ_SCOPE, write=record
        )
        require_ready(self.conn, namespace)
        if type(tolerance_s) is not int or not 0 <= tolerance_s <= 3 * 3600:
            raise WeatherError("invalid_tolerance", "tolerance_s is 0-10800 seconds")
        request = {
            "station": station,
            "parameter": parameter,
            "period_from": wr.utc(period_from),
            "period_to": wr.utc(period_to),
            "tolerance_s": tolerance_s,
            "providers": sorted(set(providers or [])),
            "thresholds": [_threshold(t, i) for i, t in enumerate(thresholds or [])],
            "knowledge_cutoff": wr.utc(knowledge_cutoff) if knowledge_cutoff else None,
        }
        paired = self._pairs(namespace, request, scopes)
        result = self._result(namespace, request, paired)
        receipt = {
            "issuance_revisions": sorted(
                {p["element"]["revision_id"] for p in paired["pairs"]}
            ),
            "observation_revisions": sorted(
                {p["observation"]["revision_id"] for p in paired["pairs"]}
            ),
            "stations": paired["members"],
            "metrics_hash": digest(result["by_group"]),
        }
        run_id = "wx-verification:" + digest([namespace, request, receipt])[:24]
        result.update(run_id=run_id, receipt=receipt)
        if record:
            self.conn.execute(_DDL)
            if not self.conn.execute(
                "SELECT 1 FROM weather_verification_runs WHERE run_id=?", [run_id]
            ).fetchone():
                self.conn.execute(
                    "INSERT INTO weather_verification_runs VALUES (?,?,?,?,?,?)",
                    [
                        run_id,
                        namespace,
                        canonical(request),
                        canonical(receipt),
                        digest(result["by_group"]),
                        principal_id,
                    ],
                )
                for index, pair in enumerate(paired["pairs"]):
                    self.conn.execute(
                        "INSERT INTO weather_verification_pairs VALUES (?,?,?)",
                        [run_id, index, canonical(pair)],
                    )
        return result

    def replay(
        self, namespace: str, run_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Recompute a recorded run's metrics from its pinned pairs and check them against the receipt."""

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not _table(self.conn, "weather_verification_runs"):
            raise WeatherError("not_found", "no verification run is recorded")
        row = self.conn.execute(
            "SELECT request_json, receipt_json FROM weather_verification_runs WHERE namespace=? "
            "AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if row is None:
            raise WeatherError(
                "not_found", "verification run is not visible in this namespace"
            )
        request, receipt = json.loads(row[0]), json.loads(row[1])
        pairs = [
            json.loads(r[0])
            for r in self.conn.execute(
                "SELECT pair_json FROM weather_verification_pairs WHERE run_id=? ORDER BY pair_index",
                [run_id],
            ).fetchall()
        ]
        for pair in pairs:
            for side in ("element", "observation"):
                if not self.conn.execute(
                    "SELECT 1 FROM weather_revisions WHERE revision_id=?",
                    [pair[side]["revision_id"]],
                ).fetchone():
                    raise WeatherError(
                        "revision_missing", f"pinned {side} revision is gone"
                    )
        recomputed = self._result(
            namespace,
            request,
            {"pairs": pairs, "excluded": {}, "members": receipt["stations"]},
        )
        return {
            "run_id": run_id,
            "deterministic": digest(recomputed["by_group"]) == receipt["metrics_hash"],
            "by_group": recomputed["by_group"],
            "pinned_revisions": len(receipt["issuance_revisions"])
            + len(receipt["observation_revisions"]),
        }

"""
Review-gated imagery tools (Track C / C4).

The external imagery tier: ``reverse_image_search`` and ``geolocate_image``.
These point at the outside world, so they ship only behind the OSINT review gate
(``docs/security/osint-review-gate.md``) and the imagery abuse analysis
(``docs/security/osint-abuse-analysis.md``, "Imagery"), off by default. This module is
the enforcement, not just the docs:

* **Corpus images only.** Both tools take a corpus asset ``sha256`` that must
  exist in ``image_assets`` — never an operator-supplied photo of a person.
* **No person identification, ever.** A permanent non-goal (guardrail 2): there
  is no person parameter and no identity output. The tools reason about images
  and places, never subjects.
* **Suggestions are not evidence.** Every result enters a review queue as
  ``cited = False`` (the flagged state the evidence discipline renders) and
  becomes citable only when an operator confirms it via :func:`confirm_suggestion`.
* **No default provider.** ``reverse_image_search`` needs an injected provider;
  with none configured the tier is inert (``no_provider_configured``).
* **Least privilege for the queue.** The corpus asset is read from a *read-only*
  warehouse connection; the review-queue write goes to a separate ``queue_conn``
  (a dedicated store) so the gated imagery tier never holds write access to the
  corpus warehouse. ``queue_conn`` defaults to ``conn`` for single-store callers
  (e.g. tests); the served tools pass a read-only corpus conn and a distinct
  read-write queue conn.

Extensions under the same gate and the same five criteria:

* ``chronolocate_image`` (OX09, #2049): proposes *when* a corpus image was
  captured (time-of-day band from shadow direction and length, season window)
  for a place taken only from a *confirmed* geolocation suggestion or an
  explicit operator hypothesis, never inferred here. Solar geometry is computed
  locally (:func:`solar_position`, the NOAA solar-position equations); the
  shadow measurements come from an injected estimator with no default. Output
  is a queued ``chronolocation`` suggestion, ``cited: false`` and
  ``verified: false``; EXIF ``DateTimeOriginal`` is shown as file-claimed and
  never used as ground truth. Per image, place-conditioned, and it never joins
  across images.
* ``fetch_reference_imagery`` (OX10, #2050): reference satellite or
  street-level imagery for a *queued suggestion's* place (or an explicit
  operator hypothesis), through a key-gated, allowlisted, budgeted
  :data:`ReferenceImageryProvider` with no default. References are stored in
  the review-queue store linked to the suggestion, never in the corpus, and
  never become citations; confirming a suggestion records which references the
  operator viewed.

See ``docs/architecture/OSINT_IMAGERY_PLAN.md`` §3.3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Permanent non-goal, asserted in code so it is not silently changed.
PERSON_IDENTIFICATION_SUPPORTED = False

_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS imagery_review_queue (
    suggestion_id   TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    suggestion      JSON NOT NULL,
    cited           BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_by    TEXT,
    confirmed_at    BIGINT,
    created_at      BIGINT
)
"""


def _ensure_queue(conn) -> None:
    conn.execute(_QUEUE_DDL)


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def _asset_bytes(conn, sha256: str) -> Optional[bytes]:
    """Read a corpus asset's bytes by sha256, or None if it is not a corpus
    image. This is the corpus-images-only guard: an unknown sha256 gets nothing."""
    if not _table_exists(conn, "image_assets"):
        return None
    row = conn.execute("SELECT path FROM image_assets WHERE sha256 = ?", [sha256]).fetchone()
    if row is None or not row[0]:
        return None
    path = os.path.abspath(row[0])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _queue_suggestion(conn, kind: str, sha256: str, suggestion: Dict[str, Any], now_ms: Optional[int]) -> str:
    _ensure_queue(conn)
    key = f"{kind}|{sha256}|{json.dumps(suggestion, sort_keys=True)}"
    suggestion_id = "sug:" + hashlib.md5(key.encode()).hexdigest()[:16]
    conn.execute(
        """
        INSERT INTO imagery_review_queue (suggestion_id, kind, sha256, suggestion, cited, created_at)
        VALUES (?, ?, ?, ?, FALSE, ?)
        ON CONFLICT (suggestion_id) DO NOTHING
        """,
        [suggestion_id, kind, sha256, json.dumps(suggestion), now_ms],
    )
    return suggestion_id


# Provider signatures (injected; no default ships):
#   ReverseSearchProvider(image_bytes) -> List[{"url", "title"?, "published"?}]
#   GeoVLM(image_bytes) -> List[{"landmark", "place"?, "confidence"?}]
ReverseSearchProvider = Callable[[bytes], List[Dict[str, Any]]]
GeoVLM = Callable[[bytes], List[Dict[str, Any]]]


def reverse_image_search(
    conn,
    sha256: str,
    provider: Optional[ReverseSearchProvider] = None,
    now_ms: Optional[int] = None,
    queue_conn=None,
) -> Dict[str, Any]:
    """Queue reverse-image-search *suggestions* for a corpus asset.

    No default provider ships; with none configured the tier is inert. Results
    are queued uncited — evidence only after operator confirmation. The corpus
    asset is read from ``conn`` (read-only); the queue write goes to
    ``queue_conn`` (defaults to ``conn``).
    """
    queue_conn = conn if queue_conn is None else queue_conn
    if provider is None:
        return {"status": "no_provider_configured", "sha256": sha256,
                "note": "reverse image search has no default provider; supply one to enable"}
    image_bytes = _asset_bytes(conn, sha256)
    if image_bytes is None:
        return {"status": "not_a_corpus_image", "sha256": sha256,
                "note": "reverse search accepts corpus asset hashes only"}
    try:
        hits = provider(image_bytes) or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "provider_error", "error": str(exc)}
    queued = []
    for hit in hits:
        suggestion = {"url": hit.get("url"), "title": hit.get("title"), "cited": False}
        for key in ("provider", "seen_at"):
            if hit.get(key) is not None:
                suggestion[key] = hit.get(key)
        sid = _queue_suggestion(queue_conn, "reverse_image_search", sha256, suggestion, now_ms)
        queued.append({"suggestion_id": sid, **suggestion})
    return {
        "status": "queued",
        "sha256": sha256,
        "suggestions": queued,
        "count": len(queued),
        "note": "suggestions are uncited until an operator confirms them",
    }


def geolocate_image(
    conn,
    sha256: str,
    vlm: Optional[GeoVLM] = None,
    now_ms: Optional[int] = None,
    queue_conn=None,
) -> Dict[str, Any]:
    """Queue visible-landmark geolocation *hypotheses* for a corpus asset.

    Suggestion-grade, never auto-cited. Reasons about the place in the scene,
    never the subject (no person identification). The corpus asset is read from
    ``conn`` (read-only); the queue write goes to ``queue_conn`` (defaults to
    ``conn``).
    """
    queue_conn = conn if queue_conn is None else queue_conn
    if vlm is None:
        return {"status": "no_backend_configured", "sha256": sha256,
                "note": "geolocation assist needs a vision backend"}
    image_bytes = _asset_bytes(conn, sha256)
    if image_bytes is None:
        return {"status": "not_a_corpus_image", "sha256": sha256}
    try:
        hypotheses = vlm(image_bytes) or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "backend_error", "error": str(exc)}
    queued = []
    for h in hypotheses:
        suggestion = {
            "landmark": h.get("landmark"),
            "place": h.get("place"),
            "confidence": h.get("confidence"),
            **({"lat": float(h["lat"]), "lon": float(h["lon"])}
               if h.get("lat") is not None and h.get("lon") is not None else {}),
            "grade": "suggestion",
            "cited": False,
        }
        sid = _queue_suggestion(queue_conn, "geolocate_image", sha256, suggestion, now_ms)
        queued.append({"suggestion_id": sid, **suggestion})
    return {
        "status": "queued",
        "sha256": sha256,
        "hypotheses": queued,
        "count": len(queued),
        "note": "visible-landmark hypotheses about the scene, not the subject; uncited until confirmed",
    }


def list_review_queue(conn, cited: Optional[bool] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    """The imagery review queue. By default lists everything; filter by cited
    state or kind."""
    if not _table_exists(conn, "imagery_review_queue"):
        return {"items": [], "count": 0, "note": "review queue empty"}
    clauses: List[str] = []
    params: List[Any] = []
    if cited is not None:
        clauses.append("cited = ?")
        params.append(cited)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT suggestion_id, kind, sha256, suggestion, cited, confirmed_by FROM imagery_review_queue{where} ORDER BY created_at NULLS LAST, suggestion_id",
        params,
    ).fetchall()
    items = []
    for sid, k, sha, sug, is_cited, by in rows:
        items.append({
            "suggestion_id": sid,
            "kind": k,
            "sha256": sha,
            "suggestion": json.loads(sug) if isinstance(sug, str) else sug,
            "cited": bool(is_cited),
            "confirmed_by": by,
        })
    return {"items": items, "count": len(items)}


def confirm_suggestion(
    conn,
    suggestion_id: str,
    operator: str,
    now_ms: Optional[int] = None,
    viewed_references: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Operator confirmation — the *only* thing that makes a suggestion citable.

    ``viewed_references`` (OX10) records which fetched reference images the
    operator compared before confirming; the references themselves stay
    non-citable review aids."""
    if not operator:
        return {"status": "rejected", "note": "confirmation requires an operator identity"}
    if not _table_exists(conn, "imagery_review_queue"):
        return {"status": "not_found"}
    row = conn.execute("SELECT suggestion_id FROM imagery_review_queue WHERE suggestion_id = ?", [suggestion_id]).fetchone()
    if row is None:
        return {"status": "not_found", "suggestion_id": suggestion_id}
    conn.execute(
        "UPDATE imagery_review_queue SET cited = TRUE, confirmed_by = ?, confirmed_at = ? WHERE suggestion_id = ?",
        [operator, now_ms, suggestion_id],
    )
    viewed: List[str] = []
    if viewed_references and _table_exists(conn, "imagery_reference_views"):
        for reference_id in viewed_references:
            hit = conn.execute(
                "SELECT reference_id FROM imagery_reference_views WHERE reference_id = ? AND suggestion_id = ?",
                [reference_id, suggestion_id],
            ).fetchone()
            if hit:
                conn.execute(
                    "UPDATE imagery_reference_views SET viewed_by = ?, viewed_at = ? WHERE reference_id = ?",
                    [operator, now_ms, reference_id],
                )
                viewed.append(reference_id)
    return {"status": "confirmed", "suggestion_id": suggestion_id, "confirmed_by": operator, "cited": True,
            "viewed_references": viewed}


# --------------------------------------------------------------------------- #
# Shared gated-tier helpers                                                   #
# --------------------------------------------------------------------------- #


def _log_invocation(queue_conn, tool: str, detail: Dict[str, Any], investigation: Optional[str] = None) -> None:
    """Criterion 5: every gated invocation goes to the provisioning audit
    trail, written in the separate queue store (never the corpus warehouse)."""
    try:
        from src.provisioning import store

        store.ensure_schema(queue_conn)
        store.record_event(queue_conn, investigation or "osint-imagery-gated", tool, detail, datetime.now(UTC))
    except Exception:  # noqa: BLE001 - auditing must not turn into a side channel for errors
        logger.warning("imagery gate: could not log %s invocation", tool, exc_info=True)


def _suggestion(queue_conn, suggestion_id: str) -> Optional[Dict[str, Any]]:
    if not _table_exists(queue_conn, "imagery_review_queue"):
        return None
    row = queue_conn.execute(
        "SELECT suggestion_id, kind, sha256, suggestion, cited, confirmed_by FROM imagery_review_queue "
        "WHERE suggestion_id = ?",
        [suggestion_id],
    ).fetchone()
    if row is None:
        return None
    body = json.loads(row[3]) if isinstance(row[3], str) else row[3]
    return {"suggestion_id": row[0], "kind": row[1], "sha256": row[2], "suggestion": body,
            "cited": bool(row[4]), "confirmed_by": row[5]}


def _hypothesis(hypothesis: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """An explicit operator place hypothesis: lat/lon plus the operator's name."""
    if not hypothesis:
        return None
    try:
        lat, lon = float(hypothesis["lat"]), float(hypothesis["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    operator = str(hypothesis.get("operator") or "").strip()
    if not operator or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon, "label": hypothesis.get("label"), "operator": operator,
            "basis": "operator hypothesis"}


# --------------------------------------------------------------------------- #
# OX09: chronolocation (solar geometry, local and stdlib-only)                 #
# --------------------------------------------------------------------------- #

CHRONOLOCATION_METHOD = (
    "shadow-direction/length vs. NOAA solar-position model at a hypothesised place; "
    "suggestion-grade, place-conditioned"
)
DEFAULT_AZIMUTH_TOLERANCE_DEG = 10.0
DEFAULT_ELEVATION_TOLERANCE_DEG = 5.0
MAX_CHRONOLOCATION_DAYS = 400

# Injected shadow estimator (no default ships):
#   ShadowEstimator(image_bytes) -> {"shadow_azimuth_deg": float (direction the
#   shadow points, degrees from true north), "shadow_length_ratio": float
#   (shadow length / object height), "season_cues": [str]?}
ShadowEstimator = Callable[[bytes], Dict[str, Any]]


def solar_position(lat: float, lon: float, when: datetime) -> Tuple[float, float]:
    """(azimuth, elevation) of the sun in degrees for a UTC instant.

    NOAA solar-position equations (the NOAA solar calculator spreadsheet),
    without atmospheric refraction; accurate to a fraction of a degree, far
    below the uncertainty of a shadow measurement.
    """
    when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    jd = when.timestamp() / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m = math.radians(mean_anom)
    center = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * m) * (0.019993 - 0.000101 * jc) + math.sin(3 * m) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = mean_long + center - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))
    var_y = math.tan(obliq / 2) ** 2
    l0 = math.radians(mean_long)
    eq_time = 4 * math.degrees(
        var_y * math.sin(2 * l0) - 2 * ecc * math.sin(m) + 4 * ecc * var_y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * var_y ** 2 * math.sin(4 * l0) - 1.25 * ecc ** 2 * math.sin(2 * m)
    )
    minutes = when.hour * 60 + when.minute + when.second / 60
    true_solar = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = true_solar / 4 + 180 if true_solar / 4 < 0 else true_solar / 4 - 180
    lat_r, ha = math.radians(lat), math.radians(hour_angle)
    cos_zen = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(ha)
    zenith = math.acos(max(-1.0, min(1.0, cos_zen)))
    elevation = 90 - math.degrees(zenith)
    denom = math.cos(lat_r) * math.sin(zenith)
    if abs(denom) < 1e-9:
        azimuth = 180.0 if lat > math.degrees(decl) else 0.0
    else:
        cos_az = (math.sin(lat_r) * math.cos(zenith) - math.sin(decl)) / denom
        acos_az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (acos_az + 180) % 360 if hour_angle > 0 else (540 - acos_az) % 360
    return azimuth, elevation


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _parse_day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def solar_matches(
    lat: float,
    lon: float,
    sun_azimuth: float,
    sun_elevation: float,
    date_from: date,
    date_to: date,
    *,
    step_minutes: int = 10,
    azimuth_tolerance: float = DEFAULT_AZIMUTH_TOLERANCE_DEG,
    elevation_tolerance: float = DEFAULT_ELEVATION_TOLERANCE_DEG,
) -> List[datetime]:
    """UTC instants in the date range where the modelled sun matches the
    observed direction and elevation within tolerance."""
    hits: List[datetime] = []
    day = date_from
    while day <= date_to:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        for step in range(0, 1440, step_minutes):
            when = start + timedelta(minutes=step)
            az, el = solar_position(lat, lon, when)
            if el > 0 and _angle_diff(az, sun_azimuth) <= azimuth_tolerance and \
                    abs(el - sun_elevation) <= elevation_tolerance:
                hits.append(when)
        day += timedelta(days=1)
    return hits


def _intervals(hits: List[datetime], step_minutes: int) -> Dict[str, Any]:
    minutes = sorted({h.hour * 60 + h.minute for h in hits})
    days = sorted({h.date() for h in hits})
    # The band is the complement of the largest circular gap between matching
    # minutes, so a band spanning midnight UTC stays narrow (from > to wraps).
    gaps = [((minutes[(i + 1) % len(minutes)] - m) % 1440 or 1440, i) for i, m in enumerate(minutes)]
    _gap, idx = max(gaps)
    start = minutes[(idx + 1) % len(minutes)]
    end = (minutes[idx] + step_minutes) % 1440
    fmt = "{:02d}:{:02d}"
    return {
        "time_band_utc": {
            "from": fmt.format(*divmod(start, 60)),
            "to": fmt.format(*divmod(end, 60)),
            "from_minute": start,
            "to_minute": end,
            "wraps_midnight": end < start,
        },
        "season_window": {"from": days[0].isoformat(), "to": days[-1].isoformat(), "matching_days": len(days)},
    }


def interval_contains(interval: Dict[str, Any], when: datetime) -> bool:
    """True when a UTC instant falls inside a chronolocation interval (both the
    time band and the season window). Used by the calibration test."""
    when = when.astimezone(UTC)
    band, season = interval["time_band_utc"], interval["season_window"]
    minute = when.hour * 60 + when.minute
    start, end = band["from_minute"], band["to_minute"]
    in_band = start <= minute <= end if start <= end else (minute >= start or minute <= end)
    # A band that wraps midnight UTC can put the capture on the day after the
    # last matching day; allow that one-day spill for the season window.
    day = when.date()
    days = {day.isoformat(), (day - timedelta(days=1)).isoformat()} if start > end else {day.isoformat()}
    return in_band and any(season["from"] <= d <= season["to"] for d in days)


def _file_claimed_capture(conn, sha256: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT exif FROM image_assets WHERE sha256 = ?", [sha256]).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or not row[0]:
        return None
    exif = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return (exif or {}).get("DateTimeOriginal") or (exif or {}).get("DateTime")


def chronolocate_image(
    conn,
    sha256: str,
    *,
    date_from: Any,
    date_to: Any,
    estimator: Optional[ShadowEstimator] = None,
    suggestion_id: Optional[str] = None,
    hypothesis: Optional[Dict[str, Any]] = None,
    queue_conn=None,
    now_ms: Optional[int] = None,
    step_minutes: int = 10,
    investigation: Optional[str] = None,
) -> Dict[str, Any]:
    """Queue a *when-was-this-captured* suggestion for one corpus image.

    The place is taken only from a confirmed ``geolocate_image`` suggestion
    (``suggestion_id``) or an explicit operator ``hypothesis`` ({lat, lon,
    operator}); it is never inferred here. The result is a ``chronolocation``
    suggestion in the review queue, ``cited: false``, ``verified: false``.
    """
    queue_conn = conn if queue_conn is None else queue_conn
    if estimator is None:
        return {"status": "no_backend_configured", "sha256": sha256,
                "note": "chronolocation needs a shadow estimator; none ships by default"}
    image_bytes = _asset_bytes(conn, sha256)
    if image_bytes is None:
        return {"status": "not_a_corpus_image", "sha256": sha256}
    place = None
    if suggestion_id:
        queued = _suggestion(queue_conn, suggestion_id)
        body = (queued or {}).get("suggestion") or {}
        if not queued or queued["kind"] != "geolocate_image" or not queued["cited"]:
            return {"status": "place_not_confirmed", "suggestion_id": suggestion_id,
                    "note": "only a confirmed geolocation suggestion supplies a place"}
        if body.get("lat") is None or body.get("lon") is None:
            return {"status": "place_has_no_coordinates", "suggestion_id": suggestion_id}
        place = {"lat": float(body["lat"]), "lon": float(body["lon"]), "label": body.get("place"),
                 "basis": "confirmed geolocation suggestion", "suggestion_id": suggestion_id,
                 "confirmed_by": queued["confirmed_by"]}
    else:
        place = _hypothesis(hypothesis)
    if place is None:
        return {"status": "place_required", "sha256": sha256,
                "note": "supply a confirmed geolocation suggestion or an operator hypothesis {lat, lon, operator}"}
    try:
        start, end = _parse_day(date_from), _parse_day(date_to)
    except ValueError:
        return {"status": "invalid_date_range"}
    if end < start or (end - start).days > MAX_CHRONOLOCATION_DAYS:
        return {"status": "invalid_date_range", "max_days": MAX_CHRONOLOCATION_DAYS}
    try:
        measured = estimator(image_bytes) or {}
        shadow_az = float(measured["shadow_azimuth_deg"])
        ratio = float(measured["shadow_length_ratio"])
    except Exception as exc:  # noqa: BLE001
        return {"status": "backend_error", "error": str(exc)}
    if ratio <= 0:
        return {"status": "backend_error", "error": "shadow_length_ratio must be positive"}
    sun_az = (shadow_az + 180) % 360
    sun_el = math.degrees(math.atan(1 / ratio))
    hits = solar_matches(place["lat"], place["lon"], sun_az, sun_el, start, end, step_minutes=step_minutes)
    _log_invocation(queue_conn, "chronolocate_image", {"sha256": sha256, "place_basis": place["basis"]},
                    investigation)
    base = {
        "method": CHRONOLOCATION_METHOD,
        "place_hypothesis": place,
        "measured": {"shadow_azimuth_deg": shadow_az, "shadow_length_ratio": ratio,
                     "season_cues": list(measured.get("season_cues") or [])},
        "implied_sun": {"azimuth_deg": round(sun_az, 2), "elevation_deg": round(sun_el, 2)},
        "tolerance": {"azimuth_deg": DEFAULT_AZIMUTH_TOLERANCE_DEG,
                      "elevation_deg": DEFAULT_ELEVATION_TOLERANCE_DEG, "step_minutes": step_minutes},
        "searched": {"from": start.isoformat(), "to": end.isoformat()},
        "file_claimed_capture_time": _file_claimed_capture(conn, sha256),
        "file_claimed_note": "EXIF DateTimeOriginal is claimed by the file and never used as ground truth",
        "assumptions": [
            "the place hypothesis is correct; the interval is conditional on it",
            "the shadow falls on level ground from a vertical object",
            "the sun is not obscured and the image is not mirrored",
        ],
        "grade": "suggestion",
        "verified": False,
        "cited": False,
    }
    if not hits:
        return {"status": "no_solar_match", "sha256": sha256, **base}
    suggestion = {**base, "interval": _intervals(hits, step_minutes), "n": len(hits)}
    sid = _queue_suggestion(queue_conn, "chronolocation", sha256, suggestion, now_ms)
    return {"status": "queued", "sha256": sha256, "suggestion_id": sid, **suggestion,
            "note": "a place-conditioned capture-time suggestion about the image, not about any subject; "
                    "uncited until an operator confirms it"}


# --------------------------------------------------------------------------- #
# OX10: reference imagery for confirming a place (no default provider)         #
# --------------------------------------------------------------------------- #

REFERENCE_KINDS = ("satellite", "street-level")
_REFERENCES_DDL = """
CREATE TABLE IF NOT EXISTS imagery_reference_views (
    reference_id    TEXT PRIMARY KEY,
    suggestion_id   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    provider        TEXT NOT NULL,
    attribution     TEXT,
    terms_url       TEXT,
    request         JSON NOT NULL,
    sha256          TEXT NOT NULL,
    bytes           BLOB NOT NULL,
    cited           BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at      BIGINT,
    viewed_by       TEXT,
    viewed_at       BIGINT
)
"""

# Injected reference provider (no default ships). Called with a request
#   {"kind": "satellite", "bbox": [min_lon, min_lat, max_lon, max_lat], "zoom": int}
#   {"kind": "street-level", "lat": float, "lon": float, "zoom": int}
# and returns {"bytes": bytes, "provider": str, "attribution": str, "terms_url": str}.
# A provider object may expose ``name`` for the allowlist check.
ReferenceImageryProvider = Callable[[Dict[str, Any]], Dict[str, Any]]


@dataclass
class ReferenceBudget:
    """The agent-host budget for reference fetches: an allowlist of provider
    names, a request cap per budget window, and a per-response byte cap."""

    allowlist: Tuple[str, ...] = ()
    max_requests: int = 20
    max_bytes: int = 5_000_000
    used: int = field(default=0)

    def check(self, provider_name: str) -> Optional[str]:
        if provider_name not in self.allowlist:
            return "provider_not_allowlisted"
        if self.used >= self.max_requests:
            return "budget_exhausted"
        return None


def _place_of(queued: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    body = queued.get("suggestion") or {}
    if body.get("lat") is not None and body.get("lon") is not None:
        return {"lat": float(body["lat"]), "lon": float(body["lon"]), "basis": "queued suggestion"}
    hyp = body.get("place_hypothesis") or {}
    if hyp.get("lat") is not None and hyp.get("lon") is not None:
        return {"lat": float(hyp["lat"]), "lon": float(hyp["lon"]), "basis": "queued suggestion"}
    return None


def fetch_reference_imagery(
    queue_conn,
    *,
    kind: str,
    suggestion_id: Optional[str] = None,
    hypothesis: Optional[Dict[str, Any]] = None,
    provider: Optional[ReferenceImageryProvider] = None,
    budget: Optional[ReferenceBudget] = None,
    zoom: int = 16,
    now_ms: Optional[int] = None,
    investigation: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch one reference image for the place of a queued suggestion (or an
    explicit operator hypothesis attached to one) and attach it to the queue.

    There is no free-form coordinate fetch: a suggestion id is required, and a
    hypothesis only overrides its place when an operator names themselves. One
    reference per suggestion and kind (no time series); the fetch is
    allowlisted and budgeted; the result is never a citation.
    """
    if provider is None:
        return {"status": "no_provider_configured",
                "note": "reference imagery has no default provider; supply one (and a budget) to enable"}
    if kind not in REFERENCE_KINDS:
        return {"status": "invalid_kind", "kinds": list(REFERENCE_KINDS)}
    if not suggestion_id:
        return {"status": "suggestion_required",
                "note": "fetches are only allowed for a place attached to a queued suggestion"}
    queued = _suggestion(queue_conn, suggestion_id)
    if queued is None:
        return {"status": "suggestion_not_found", "suggestion_id": suggestion_id}
    place = _hypothesis(hypothesis) if hypothesis else _place_of(queued)
    if place is None:
        return {"status": "place_required", "suggestion_id": suggestion_id,
                "note": "the suggestion carries no coordinates; supply an operator hypothesis {lat, lon, operator}"}
    budget = budget or ReferenceBudget()
    name = str(getattr(provider, "name", "") or getattr(provider, "__name__", ""))
    refusal = budget.check(name)
    if refusal:
        return {"status": refusal, "provider": name}
    queue_conn.execute(_REFERENCES_DDL)
    if queue_conn.execute(
        "SELECT 1 FROM imagery_reference_views WHERE suggestion_id = ? AND kind = ?", [suggestion_id, kind]
    ).fetchone():
        return {"status": "already_fetched", "suggestion_id": suggestion_id, "kind": kind,
                "note": "one reference per suggestion and kind; no time-series fetches"}
    zoom = max(1, min(int(zoom), 20))
    if kind == "satellite":
        half = 180 / (2 ** zoom)
        request = {"kind": kind, "zoom": zoom, "bbox": [round(place["lon"] - half, 6), round(place["lat"] - half, 6),
                                                          round(place["lon"] + half, 6), round(place["lat"] + half, 6)]}
    else:
        request = {"kind": kind, "zoom": zoom, "lat": place["lat"], "lon": place["lon"]}
    budget.used += 1
    try:
        response = provider(request) or {}
    except Exception as exc:  # noqa: BLE001 - no retry; the operator decides
        return {"status": "provider_error", "error": str(exc)}
    data = response.get("bytes") or b""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return {"status": "provider_error", "error": "provider returned no image bytes"}
    if len(data) > budget.max_bytes:
        return {"status": "response_too_large", "max_bytes": budget.max_bytes}
    digest = hashlib.sha256(bytes(data)).hexdigest()
    reference_id = "ref:" + hashlib.sha256(f"{suggestion_id}|{kind}|{digest}".encode()).hexdigest()[:16]
    queue_conn.execute(
        "INSERT INTO imagery_reference_views (reference_id, suggestion_id, kind, provider, attribution, terms_url, "
        "request, sha256, bytes, cited, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?) "
        "ON CONFLICT (reference_id) DO NOTHING",
        [reference_id, suggestion_id, kind, response.get("provider") or name, response.get("attribution"),
         response.get("terms_url"), json.dumps(request), digest, bytes(data), now_ms],
    )
    _log_invocation(queue_conn, "reference_imagery", {"suggestion_id": suggestion_id, "kind": kind,
                                                      "provider": response.get("provider") or name}, investigation)
    return {
        "status": "attached",
        "reference_id": reference_id,
        "suggestion_id": suggestion_id,
        "kind": kind,
        "request": request,
        "place_basis": place["basis"],
        "provider": response.get("provider") or name,
        "attribution": response.get("attribution"),
        "terms_url": response.get("terms_url"),
        "sha256": digest,
        "cited": False,
        "note": "a review aid attached to the suggestion; never a citation and never stored in the corpus",
    }


def list_references(queue_conn, suggestion_id: str) -> List[Dict[str, Any]]:
    if not _table_exists(queue_conn, "imagery_reference_views"):
        return []
    rows = queue_conn.execute(
        "SELECT reference_id, kind, provider, attribution, terms_url, sha256, cited, viewed_by "
        "FROM imagery_reference_views WHERE suggestion_id = ? ORDER BY reference_id",
        [suggestion_id],
    ).fetchall()
    keys = ["reference_id", "kind", "provider", "attribution", "terms_url", "sha256", "cited", "viewed_by"]
    return [dict(zip(keys, r)) for r in rows]

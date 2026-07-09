"""
Geospatial evidence plane (candidate track #783).

Timeline reconstruction has no spatial sibling: events and claims have
locations, but nothing geocodes them, maps them, or corroborates by place. This
module geocodes place mentions against a compact gazetteer, and corroborates a
topic by location — "independent sources reporting from the same place" — a
natural extension of the timeline discipline to space.

Gazetteer-first and stdlib only (a model-assisted geocoder can be slotted in
later). Connection-injected reader.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Compact gazetteer: name -> (lat, lon, ISO country). Extended as coverage
# needs grow; unknown places are simply not geocoded (never guessed).
GAZETTEER: Dict[str, tuple] = {
    "berlin": (52.52, 13.405, "DE"),
    "germany": (51.1657, 10.4515, "DE"),
    "paris": (48.8566, 2.3522, "FR"),
    "france": (46.2276, 2.2137, "FR"),
    "london": (51.5074, -0.1278, "GB"),
    "united kingdom": (55.3781, -3.4360, "GB"),
    "madrid": (40.4168, -3.7038, "ES"),
    "spain": (40.4637, -3.7492, "ES"),
    "rome": (41.9028, 12.4964, "IT"),
    "italy": (41.8719, 12.5674, "IT"),
    "washington": (38.9072, -77.0369, "US"),
    "new york": (40.7128, -74.0060, "US"),
    "united states": (37.0902, -95.7129, "US"),
    "brussels": (50.8503, 4.3517, "BE"),
    "moscow": (55.7558, 37.6173, "RU"),
    "beijing": (39.9042, 116.4074, "CN"),
    "china": (35.8617, 104.1954, "CN"),
    "tokyo": (35.6762, 139.6503, "JP"),
    "kyiv": (50.4501, 30.5234, "UA"),
    "ukraine": (48.3794, 31.1656, "UA"),
    "gaza": (31.5017, 34.4668, "PS"),
}


@dataclass
class GeoRef:
    name: str
    lat: float
    lon: float
    country: str


def geocode(text: Optional[str]) -> List[GeoRef]:
    """Return geocoded place mentions found in text (deduped, longest-first so
    'new york' beats 'york'). Unknown places are not returned."""
    if not text:
        return []
    lowered = text.lower()
    out: List[GeoRef] = []
    seen = set()
    for name in sorted(GAZETTEER, key=len, reverse=True):
        if name in seen:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", lowered):
            lat, lon, country = GAZETTEER[name]
            out.append(GeoRef(name=name, lat=lat, lon=lon, country=country))
            seen.add(name)
    return out


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def place_coverage(conn, topic: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """Geocode the documents matching a topic and group by place, with the
    independent-source (distinct document) count per location — the map payload.
    """
    if not _table_exists(conn, "documents"):
        return {"places": [], "count": 0, "note": "no documents corpus"}
    clauses: List[str] = []
    params: List[Any] = []
    if topic:
        clauses.append("(LOWER(content) LIKE ? OR LOWER(title) LIKE ?)")
        needle = f"%{topic.lower()}%"
        params.extend([needle, needle])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT document_id, title, content FROM documents{where} LIMIT {int(limit)}",
        params,
    ).fetchall()

    by_place: Dict[str, Dict[str, Any]] = {}
    for document_id, title, content in rows:
        refs = geocode(f"{title or ''} {content or ''}")
        for ref in refs:
            entry = by_place.setdefault(ref.name, {
                "place": ref.name, "lat": ref.lat, "lon": ref.lon, "country": ref.country,
                "documents": set(),
            })
            entry["documents"].add(document_id)
    places = [
        {
            "place": e["place"], "lat": e["lat"], "lon": e["lon"], "country": e["country"],
            "document_count": len(e["documents"]),
            "corroborated": len(e["documents"]) >= 2,
        }
        for e in by_place.values()
    ]
    places.sort(key=lambda p: -p["document_count"])
    return {"places": places, "count": len(places), "topic": topic}

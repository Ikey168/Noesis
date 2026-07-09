"""
Speaker-turn analytics (candidate track #789).

The media connector's diarization already yields speaker turns with timestamps
(one Document per segment, ``metadata.start_s`` / ``end_s`` / ``speaker``). Who
gets airtime, who interrupts whom, and floor share over a recording are
quantitative *framing* evidence no transcript text carries — and the data is
already in the store.

Honesty-enveloped: the diarization error rate is a declared assumption, so the
airtime breakdown is a defensible signal, not a bare number.

Stdlib only; connection-injected reader.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope

METHOD = "speaker-turn statistics over diarized transcript segments"
ASSUMPTIONS = [
    "speaker labels come from diarization and carry its error rate",
    "airtime is segment duration; overlapping speech is attributed to both speakers",
    "an interruption is a segment starting before the prior (different) speaker's segment ends",
]


def analyze_segments(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-speaker airtime, turns, floor share, and interruptions.

    ``segments`` is a list of ``{start_s, end_s, speaker, text?}``. Returns an
    honesty envelope with a per-speaker breakdown and an interruption matrix.
    """
    clean = [
        s for s in segments
        if s.get("speaker") is not None and s.get("start_s") is not None and s.get("end_s") is not None
    ]
    if not clean:
        return analytic_envelope(n=0, method=METHOD, assumptions=ASSUMPTIONS, speakers=[], interruptions=[], speaker_count=0, total_airtime_s=0.0, note="no diarized segments")

    ordered = sorted(clean, key=lambda s: float(s["start_s"]))
    per_speaker: Dict[str, Dict[str, float]] = {}
    for s in ordered:
        spk = str(s["speaker"])
        dur = max(0.0, float(s["end_s"]) - float(s["start_s"]))
        rec = per_speaker.setdefault(spk, {"airtime_s": 0.0, "turns": 0, "words": 0})
        rec["airtime_s"] += dur
        rec["turns"] += 1
        rec["words"] += len((s.get("text") or "").split())

    total_airtime = sum(r["airtime_s"] for r in per_speaker.values()) or 1.0

    # Interruptions: a segment beginning before the previous (different) speaker's
    # segment ends. Attribute to the interrupter -> interrupted.
    interruptions: Dict[str, int] = {}
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if str(prev["speaker"]) == str(cur["speaker"]):
            continue
        if float(cur["start_s"]) < float(prev["end_s"]):
            key = f"{cur['speaker']}->{prev['speaker']}"
            interruptions[key] = interruptions.get(key, 0) + 1

    speakers = [
        {
            "speaker": spk,
            "airtime_s": round(rec["airtime_s"], 2),
            "turns": int(rec["turns"]),
            "words": int(rec["words"]),
            "floor_share": round(rec["airtime_s"] / total_airtime, 4),
        }
        for spk, rec in per_speaker.items()
    ]
    speakers.sort(key=lambda x: -x["airtime_s"])
    interruption_list = [
        {"interrupter": k.split("->")[0], "interrupted": k.split("->")[1], "count": v}
        for k, v in sorted(interruptions.items(), key=lambda kv: -kv[1])
    ]
    return analytic_envelope(
        n=len(ordered),
        method=METHOD,
        assumptions=ASSUMPTIONS,
        speakers=speakers,
        interruptions=interruption_list,
        total_airtime_s=round(total_airtime, 2),
        speaker_count=len(speakers),
    )


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def speaker_balance(conn, media: Optional[str] = None) -> Dict[str, Any]:
    """Read diarized transcript segments from the documents corpus and analyze
    them. ``media`` filters by a substring of the segment's url/content_ref so a
    single recording can be scoped."""
    if not _table_exists(conn, "documents"):
        return analyze_segments([])
    clauses = ["source_type = 'transcript'", "json_extract_string(metadata, '$.speaker') IS NOT NULL"]
    params: List[Any] = []
    if media:
        clauses.append("(url LIKE ? OR content_ref LIKE ?)")
        needle = f"%{media}%"
        params.extend([needle, needle])
    where = " AND ".join(clauses)
    try:
        rows = conn.execute(
            f"""
            SELECT json_extract_string(metadata, '$.speaker') AS speaker,
                   CAST(json_extract(metadata, '$.start_s') AS DOUBLE) AS start_s,
                   CAST(json_extract(metadata, '$.end_s') AS DOUBLE) AS end_s,
                   content
            FROM documents WHERE {where}
            """,
            params,
        ).fetchall()
    except Exception:  # noqa: BLE001 - corpus without the columns
        return analyze_segments([])
    segments = [{"speaker": r[0], "start_s": r[1], "end_s": r[2], "text": r[3]} for r in rows]
    return analyze_segments(segments)

"""
Cross-modal contradiction detection (candidate track #785).

The contradiction ledger records where sources disagree with each other. With
figures (Track B) and numbers (Track A) as evidence, it can also record where a
document disagrees with *itself*: a figure description whose number contradicts
the prose, a caption that misstates its own figure.

For a parent document this compares the quantitative assertions parsed from its
prose against those parsed from its figure documents (B1: ``metadata.modality =
'image'``), and flags conflicts. Honesty-enveloped; each finding cites both the
prose and the figure. The figure numbers carry the "approximate, read from
chart" caveat, so a small gap is not a contradiction.

Connection-injected; reuses the A3 quantity extractor.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope
from src.argument_mining.quantities import QuantAssertion, QuantityExtractor

METHOD = "cross-modal (prose vs figure) quantitative contradiction"
ASSUMPTIONS = [
    "figure values are approximate (read from chart); small gaps are not contradictions",
    "assertions are matched by subject/unit overlap; a weak match is not flagged",
    "only same-period, same-unit numeric disagreements are contradictions",
]

# A figure value must differ from the prose value by more than this relative
# tolerance (on top of any unit/period agreement) to count as a contradiction.
REL_TOLERANCE = 0.15


def _subject_tokens(a: QuantAssertion) -> set:
    import re

    words = re.findall(r"[a-z0-9]+", (a.subject or "").lower())
    return {w for w in words if len(w) > 2}


def _matches(a: QuantAssertion, b: QuantAssertion) -> bool:
    """Two assertions describe the same measured thing (subject overlap + a
    compatible period and unit)."""
    ta, tb = _subject_tokens(a), _subject_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    if overlap < 0.5:
        return False
    if a.period and b.period and a.period != b.period:
        return False
    if a.unit and b.unit and a.unit != b.unit:
        return False
    return True


def _conflicts(prose: QuantAssertion, figure: QuantAssertion) -> Optional[str]:
    """Return a conflict description if the two disagree, else None."""
    # Direction conflict (one rose, the other fell).
    if prose.direction in ("rose", "fell") and figure.direction in ("rose", "fell") and prose.direction != figure.direction:
        return f"direction: prose says {prose.direction}, figure says {figure.direction}"
    # Value conflict beyond tolerance.
    if prose.value is not None and figure.value is not None:
        base = max(abs(prose.value), abs(figure.value), 1e-9)
        if abs(prose.value - figure.value) / base > REL_TOLERANCE:
            return f"value: prose {prose.value} vs figure {figure.value}"
    return None


import re as _re

_FIG_LABEL_RE = _re.compile(r"^\s*fig(?:ure)?\.?\s*\d+[a-z]?\s*(?:\([^)]*\))?\s*[:.\-—]?\s*", _re.IGNORECASE)


def _strip_figure_label(text: str) -> str:
    """Drop a leading 'Figure 3:' / 'Fig. 2.' so its number is not read as a
    measured value."""
    return _FIG_LABEL_RE.sub("", text or "")


def contradictions_in(prose_text: Optional[str], figure_texts: List[str]) -> List[Dict[str, Any]]:
    """Find quantitative conflicts between a document's prose and its figures."""
    ex = QuantityExtractor()
    prose_assertions = ex.extract_sentences((prose_text or "").split(". "))
    figure_assertions: List[QuantAssertion] = []
    for ft in figure_texts:
        cleaned = _strip_figure_label(ft or "")
        figure_assertions.extend(ex.extract_sentences(cleaned.split(". ")))

    findings: List[Dict[str, Any]] = []
    for pa in prose_assertions:
        for fa in figure_assertions:
            if not _matches(pa, fa):
                continue
            conflict = _conflicts(pa, fa)
            if conflict:
                findings.append({
                    "subject": pa.subject,
                    "conflict": conflict,
                    "prose": pa.to_dict(),
                    "figure": fa.to_dict(),
                })
    return findings


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def find_intra_document_contradictions(conn, parent_document_id: Optional[str] = None) -> Dict[str, Any]:
    """Scan documents for prose-vs-figure quantitative contradictions.

    Reads a parent document's prose and its figure documents (children with
    ``metadata.modality = 'image'``) from the corpus, and flags conflicts, each
    citing the parent and the figure.
    """
    if not _table_exists(conn, "documents"):
        return analytic_envelope(n=0, method=METHOD, assumptions=ASSUMPTIONS, findings=[], note="no documents corpus")

    # Parents: documents that have at least one figure child.
    parent_clause = "AND d.document_id = ?" if parent_document_id else ""
    params = [parent_document_id] if parent_document_id else []
    parents = conn.execute(
        f"""
        SELECT DISTINCT d.document_id, d.content
        FROM documents d
        WHERE d.content IS NOT NULL {parent_clause}
        """,
        params,
    ).fetchall()

    findings: List[Dict[str, Any]] = []
    scanned = 0
    for doc_id, prose in parents:
        figs = conn.execute(
            """
            SELECT document_id, content FROM documents
            WHERE json_extract_string(metadata, '$.modality') = 'image'
              AND json_extract_string(metadata, '$.parent_document_id') = ?
            """,
            [doc_id],
        ).fetchall()
        if not figs:
            continue
        scanned += 1
        conflicts = contradictions_in(prose, [f[1] or "" for f in figs])
        for c in conflicts:
            c["parent_document_id"] = doc_id
            c["cited"] = True
            findings.append(c)

    return analytic_envelope(
        n=scanned,
        method=METHOD,
        assumptions=ASSUMPTIONS,
        findings=findings,
        finding_count=len(findings),
    )

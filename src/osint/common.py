"""
Shared read helpers for the OSINT composition tools.

All three tools need the same two joins: from a claim to the *source* that
carried it (``argument_claims.document_id`` to ``news_articles.source``), and
from a source to its *credibility* (the latest ``outlet_scores.composite_score``).
Kept in one place so corroboration, reliability and contradiction-scan agree on
what "source" and "credibility" mean.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Credibility assumed for a source with no transparency score yet. Neutral, so
# an unscored source neither helps nor hurts a corroboration tally.
DEFAULT_CREDIBILITY = 0.5

# Roles in ``document_actors`` that denote a human individual. The guardrail is
# "never identify a person", so classification is an ANY-overlap test, never a
# subset test: a person who *also* carries a non-person role (a "president" who
# is also a "subject") must still be caught. Kept deliberately broad — office and
# title roles are all held by individuals — because a false "is a person" only
# costs a refusal, while a false "not a person" would leak a person's location.
PERSON_ROLES = frozenset({
    # generic person markers
    "person", "people", "individual", "human", "man", "woman",
    # attribution / narrative roles a human carries
    "speaker", "spokesperson", "spokesman", "spokeswoman", "author",
    "subject", "witness", "victim", "suspect", "defendant", "plaintiff",
    "signatory", "interviewee", "quoted",
    # office / title roles, all held by individuals
    "president", "vice-president", "vice president", "vp", "minister",
    "secretary", "senator", "governor", "mayor", "chancellor", "premier",
    "prime minister", "official", "politician", "lawmaker", "legislator",
    "representative", "congressman", "congresswoman", "mp", "diplomat",
    "ambassador", "judge", "justice", "prosecutor", "attorney", "lawyer",
    "general", "colonel", "admiral", "officer", "commander", "chief",
    "ceo", "cfo", "cto", "coo", "director", "executive", "chairman",
    "chairwoman", "chair", "chairperson", "founder", "owner", "leader",
    "head", "manager", "activist", "protester", "journalist", "reporter",
    "correspondent", "columnist", "editor", "analyst", "researcher",
    "scientist", "professor", "economist", "athlete", "player", "actor",
    "artist", "celebrity",
})

# Entity types a caller may pass to *positively* assert person-ness or, for the
# non-person set, to override role inference (an outlet named as a "subject" is
# not a person). Narrow and explicit; any other type falls through to inference.
_PERSON_TYPES = frozenset({"person", "people", "individual", "human"})
_NONPERSON_TYPES = frozenset({
    "organization", "organisation", "org", "company", "corporation", "corp",
    "outlet", "publisher", "publication", "agency", "institution", "group",
    "party", "team", "place", "location", "gpe", "geo", "country", "nation",
    "state", "city", "region", "facility", "landmark", "product", "work",
    "event", "law", "norp", "language", "date", "time", "money", "percent",
    "quantity", "unknown",
})


def is_person(conn, entity, entity_type=None, *, unknown_is_person: bool = True) -> bool:
    """Fail-closed person classifier backing the de-anonymisation guardrails.

    Returns True when *entity* must be treated as a human individual, so a gated
    tool refuses to geolocate or profile it. The bias is deliberately toward
    True: this enforces a "never identify a person" guardrail, so an entity we
    cannot confidently classify as *non*-human is treated as a person.

    Resolution order:

    * an explicit ``entity_type`` wins — a person-type is a person, a
      non-person-type (organisation, place, …) is not;
    * a ``person:`` id prefix is a person;
    * otherwise the entity's roles in ``document_actors`` decide, by **any**
      overlap with :data:`PERSON_ROLES` (never a subset test, so a person who
      also carries a non-person role is still caught);
    * when nothing classifies the entity — no type, no prefix, no roles, or the
      lookup errors — ``unknown_is_person`` decides. It defaults to True
      (fail closed) for an entity being de-anonymised; a caller scanning
      free text (e.g. a topic substring, which is usually not a name at all)
      passes ``unknown_is_person=False`` so ordinary topics are not refused.
    """
    etype = (entity_type or "").strip().lower()
    if etype in _PERSON_TYPES:
        return True
    if etype in _NONPERSON_TYPES:
        return False
    if isinstance(entity, str) and entity.lower().startswith("person:"):
        return True
    if not table_exists(conn, "document_actors"):
        return unknown_is_person
    try:
        rows = conn.execute(
            "SELECT DISTINCT lower(role) FROM document_actors "
            "WHERE actor_name = ? OR entity_id = ?",
            [entity, entity],
        ).fetchall()
    except Exception:
        # Cannot classify -> fail closed (or per the caller's unknown policy).
        return unknown_is_person
    roles = {r[0] for r in rows if r[0]}
    if not roles:
        # Entity absent from — or role-less in — the actor layer.
        return unknown_is_person
    if roles & PERSON_ROLES:
        return True
    # Present with roles, none person-denoting -> a non-person actor (e.g. an
    # organisation tagged only "organization"/"outlet").
    return False


def table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def citation_table(conn) -> Optional[str]:
    """The table OSINT resolves a document's citation (source/url/title/date)
    from, source-type-agnostic when available.

    Prefers the source-agnostic ``corpus_documents`` view (every ``source_type``)
    so a blog, paper or filing resolves to its source exactly like a news
    article; falls back to the news-only ``news_articles`` view/table for legacy
    warehouses and test fixtures that only seed it. ``None`` when neither exists.
    """
    if table_exists(conn, "corpus_documents"):
        return "corpus_documents"
    if table_exists(conn, "news_articles"):
        return "news_articles"
    return None


def claim_sources(conn, claim_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Map each claim_id to its carrying source: ``{source, source_type, url,
    document_id}``. Resolves the outlet name via the source-agnostic citation
    table (:func:`citation_table`) for a document of any ``source_type``, else
    falls back to the claim's ``source_type`` as the source label."""
    if not claim_ids or not table_exists(conn, "argument_claims"):
        return {}
    ph = ", ".join("?" for _ in claim_ids)
    citation_tbl = citation_table(conn)
    if citation_tbl:
        rows = conn.execute(
            f"""
            SELECT c.claim_id, c.source_type, c.document_id,
                   a.source, a.url
            FROM argument_claims c
            LEFT JOIN {citation_tbl} a ON c.document_id = a.id
            WHERE c.claim_id IN ({ph})
            """,
            list(claim_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT claim_id, source_type, document_id, NULL, NULL "
            f"FROM argument_claims WHERE claim_id IN ({ph})",
            list(claim_ids),
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        claim_id, source_type, document_id, source, url = r
        out[claim_id] = {
            "source": source or source_type or "unknown",
            "source_type": source_type,
            "document_id": document_id,
            "url": url,
            # True only when the claim's document resolved to a real corpus
            # article; a dangling document_id does not count as a citation.
            "resolved": source is not None,
        }
    return out


def source_credibility(conn, sources: Sequence[str]) -> Dict[str, Optional[float]]:
    """Latest ``composite_score`` per source from ``outlet_scores`` (None when
    the source has never been scored)."""
    uniq = [s for s in dict.fromkeys(sources) if s]
    if not uniq or not table_exists(conn, "outlet_scores"):
        return {s: None for s in uniq}
    ph = ", ".join("?" for _ in uniq)
    rows = conn.execute(
        f"""
        SELECT source, composite_score
        FROM outlet_scores o
        WHERE source IN ({ph})
          AND score_date = (
            SELECT MAX(score_date) FROM outlet_scores i WHERE i.source = o.source
          )
        """,
        uniq,
    ).fetchall()
    scored = {r[0]: (float(r[1]) if r[1] is not None else None) for r in rows}
    return {s: scored.get(s) for s in uniq}


def credibility_or_default(value: Optional[float]) -> float:
    """A usable credibility weight for tallies (neutral default when unscored)."""
    return DEFAULT_CREDIBILITY if value is None else value


def dedupe_sources(entries: List[Dict[str, Any]]) -> List[str]:
    """Distinct source names in a list of ``{source: ...}`` rows."""
    return list(dict.fromkeys(e["source"] for e in entries if e.get("source")))

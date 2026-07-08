"""
The investigation engine: question -> leads -> evidence -> verdict (or an
honest refusal to reach one).

The loop the engine drives, entirely over the OSINT composition layer:

1. :func:`open_case` - a case states its question and *competing* hypotheses.
   When the caller supplies none, the engine seeds the affirmative reading of
   the question and its null counterpart, because an investigation that
   cannot state what would disconfirm it is advocacy.
2. :func:`plan_leads` - leads are concrete, replayable tool calls derived
   from the case scope: corroborate the claims that match the question,
   scan the topic for contradictions, reconstruct the timeline, pull entity
   dossiers and connection paths, and vet every source that evidence has
   introduced. Planning is idempotent (leads are keyed by tool + params), so
   each round only surfaces genuinely new leads - including vetting leads
   for sources the previous round discovered.
3. :func:`pursue_lead` - executes one lead and harvests its output into
   cited, credibility-weighted evidence rows attached to hypotheses.
4. :func:`hypothesis_matrix` - ACH-style scoring: per hypothesis, the
   independent supporting and contradicting sources and their weighted
   tallies, with diagnostic sources (those that discriminate between
   hypotheses) called out. Ships under the honesty envelope with a
   calibrated support interval - never a bare point score.
5. :func:`conclude_case` - the evidence-discipline gate. A verdict requires
   enough independent sources, a real margin over the runner-up, no
   unanswered contradiction against the leader, and no leads left open.
   When any check fails, the case stays open and the gaps are returned by
   name; the engine never manufactures a verdict.

:func:`run_case` drives the whole loop bounded by a round budget. Every step
is journalled, so a case replays as a narrative.

Stdlib-only; the connection is injected (writes run under the caller's lock).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.analytics.conformal import calibrated_envelope_fields, conformal_interval
from src.analytics.honesty import analytic_envelope, interval
from src.investigation import store
from src.osint import common

METHOD = (
    "analysis of competing hypotheses over independent-source corroboration, "
    "contradiction and provenance evidence"
)
ASSUMPTIONS = [
    "independence is by distinct source; two evidence rows from one source count once",
    "source credibility is the latest outlet transparency composite (0.5 when unscored)",
    "support for the affirmative hypothesis is mirrored as contradiction of its null counterpart",
    "a matched claim that contradicts a better-matching claim is treated as opposing "
    "the hypothesis, so corroboration of it counts with the direction flipped",
    "absence of evidence is not evidence: an unconcluded case names its gaps",
    "reads only already-ingested public documents; no crawling or targeting",
]

# The evidence-discipline gate: what a verdict minimally requires.
CONCLUDE_MIN_INDEPENDENT_SOURCES = 2
CONCLUDE_MIN_WEIGHTED_MARGIN = 0.5

KIND_AFFIRMATIVE = "affirmative"
KIND_NULL = "null"
KIND_CUSTOM = "custom"

# Words carrying no investigative signal when matching claims to a question.
_STOPWORDS = frozenset(
    "the a an and or but is are was were be been being do does did has have had "
    "that this these those there their then than with from into onto over under "
    "about after before during between within will would could should shall may "
    "might must can what which when where who whom whose why how not no nor own "
    "same so too very just also more most some such only other any each few all "
    "record support supports supported does".split()
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def keywords_for(text: str, limit: int = 8) -> List[str]:
    """The investigable keywords of a question or statement."""
    words = re.findall(r"[a-z0-9][a-z0-9-]{3,}", (text or "").lower())
    out: List[str] = []
    for w in words:
        if w in _STOPWORDS or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _affirmative(hypotheses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for h in hypotheses:
        if h["kind"] in (KIND_AFFIRMATIVE, KIND_CUSTOM):
            return h
    return None


def _null(hypotheses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for h in hypotheses:
        if h["kind"] == KIND_NULL:
            return h
    return None


# --------------------------------------------------------------------------- #
# 1. Open
# --------------------------------------------------------------------------- #

def open_case(
    conn,
    question: str,
    hypotheses: Optional[List[str]] = None,
    topic: Optional[str] = None,
    entities: Optional[List[str]] = None,
    clock: Callable[[], datetime] = _utcnow,
) -> Dict[str, Any]:
    """Open a case on a question with competing hypotheses.

    Supplied hypotheses become the custom set (a null counterpart is added
    when fewer than two are given); otherwise the engine seeds the
    affirmative reading of the question and its null counterpart.
    """
    question = (question or "").strip()
    if not question:
        return {"error": "a case needs a question", "code": "empty_question"}
    store.ensure_schema(conn)
    now = clock()

    base = f"case-{store.slugify(question)}"
    case_id, n = base, 2
    while store.get_case(conn, case_id) is not None:
        case_id = f"{base}-{n}"
        n += 1

    store.insert_case(conn, case_id, question, topic, list(entities or []), now)

    statements: List[Dict[str, str]] = []
    if hypotheses:
        statements = [
            {"statement": s.strip(), "kind": KIND_CUSTOM}
            for s in hypotheses if s and s.strip()
        ]
    else:
        statements = [{"statement": question, "kind": KIND_AFFIRMATIVE}]
    if len(statements) < 2:
        statements.append(
            {
                "statement": f"The record does not support: {question}",
                "kind": KIND_NULL,
            }
        )
    for i, h in enumerate(statements, start=1):
        hid = "h0" if h["kind"] == KIND_NULL else f"h{i}"
        store.add_hypothesis(conn, case_id, hid, h["statement"], h["kind"], now)

    store.record_event(
        conn, case_id, "case_opened",
        {"question": question, "topic": topic, "entities": entities or [],
         "hypotheses": len(statements)},
        now,
    )
    return case_file(conn, case_id)


# --------------------------------------------------------------------------- #
# 2. Plan
# --------------------------------------------------------------------------- #

def _matching_claims(
    conn, terms: List[str], limit: int
) -> List[Dict[str, Any]]:
    """Claims whose text matches the question's keywords, best matches first.
    A multi-keyword question requires at least two keyword hits so a single
    common word cannot drag in the whole corpus."""
    if not terms or not common.table_exists(conn, "argument_claims"):
        return []
    score = " + ".join("(CASE WHEN claim_text ILIKE ? THEN 1 ELSE 0 END)" for _ in terms)
    params: List[Any] = [f"%{t}%" for t in terms]
    min_hits = 2 if len(terms) >= 2 else 1
    params.extend([min_hits, int(limit)])
    rows = conn.execute(
        f"""
        SELECT claim_id, claim_text, ({score}) AS hits
        FROM argument_claims
        WHERE hits >= ?
        ORDER BY hits DESC, claim_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [{"claim_id": r[0], "text": (r[1] or "")[:160], "hits": int(r[2])} for r in rows]


def _claim_stances(conn, claims: List[Dict[str, Any]]) -> Dict[str, str]:
    """Label each matched claim ``aligned`` or ``opposed`` to the question.

    The best-matching claim anchors the aligned side; any claim that carries a
    CONTRADICTS edge to an already-labelled claim takes the opposite label
    (one pass, best matches first). Claims with no conflict edge stay aligned.
    """
    stances: Dict[str, str] = {}
    ids = [c["claim_id"] for c in claims]
    edges: Dict[str, List[str]] = {}
    if len(ids) >= 2 and common.table_exists(conn, "claim_conflicts"):
        ph = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT claim_id_a, claim_id_b FROM claim_conflicts "
            f"WHERE lower(conflict_type) LIKE '%contradict%' "
            f"AND claim_id_a IN ({ph}) AND claim_id_b IN ({ph})",
            ids + ids,
        ).fetchall()
        for a, b in rows:
            edges.setdefault(a, []).append(b)
            edges.setdefault(b, []).append(a)
    for claim in claims:  # already ordered best match first
        cid = claim["claim_id"]
        opposite_of = next(
            (other for other in edges.get(cid, []) if other in stances), None
        )
        if opposite_of is None:
            stances[cid] = "aligned"
        else:
            stances[cid] = "opposed" if stances[opposite_of] == "aligned" else "aligned"
    return stances


def plan_leads(
    conn,
    case_id: str,
    clock: Callable[[], datetime] = _utcnow,
    max_claim_leads: int = 8,
) -> Dict[str, Any]:
    """Plan the next round of leads for a case (idempotent: already-planned
    leads, pursued or not, are never duplicated)."""
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}
    if case["status"] == store.STATUS_CONCLUDED:
        return {"error": f"case {case_id!r} is concluded", "code": "concluded"}
    now = clock()
    hypotheses = store.list_hypotheses(conn, case_id)
    affirmative = _affirmative(hypotheses)
    planned: List[Dict[str, Any]] = []

    def _plan(tool: str, params: Dict[str, Any], rationale: str,
              hypothesis_id: Optional[str] = None) -> None:
        lead_id = store.upsert_lead(
            conn, case_id, tool, params, rationale, hypothesis_id, now
        )
        if lead_id is not None:
            planned.append({"lead_id": lead_id, "tool": tool, "params": params,
                            "rationale": rationale, "hypothesis_id": hypothesis_id})

    # Corroborate every claim in the record that speaks to the question. A
    # claim that contradicts a better-matching claim is an opposing claim:
    # its corroboration will count with the direction flipped.
    terms = keywords_for(f"{case['question']} {case['topic'] or ''}")
    claims = _matching_claims(conn, terms, max_claim_leads)
    stances = _claim_stances(conn, claims)
    for claim in claims:
        stance = stances.get(claim["claim_id"], "aligned")
        _plan(
            "corroborate", {"claim_id": claim["claim_id"], "stance": stance},
            f"claim matches the question ({claim['hits']} keyword hits, "
            f"{stance}): {claim['text']}",
            hypothesis_id=affirmative["hypothesis_id"] if affirmative else None,
        )

    # Scan the topic for where the record disagrees with itself, and
    # reconstruct the event sequence.
    topic = case["topic"] or (terms[0] if terms else None)
    if topic:
        _plan("contradiction_scan", {"topic": topic},
              "surface where the record contradicts itself on the case topic")
        _plan("timeline_reconstruct", {"topic": topic},
              "reconstruct the dated event sequence behind the question")

    # Entity work: a dossier per named entity, a connection path per pair.
    entities = case["entities"]
    for entity in entities:
        _plan("entity_dossier", {"entity": entity},
              f"cited brief for case entity {entity!r}")
    for i in range(len(entities) - 1):
        _plan("relationship_path", {"a": entities[i], "b": entities[i + 1]},
              f"how {entities[i]!r} and {entities[i + 1]!r} are connected")

    # Vet every source the evidence has introduced (second-round leads: the
    # engine investigates its own witnesses).
    vetted_or_planned = {
        lead["params"].get("source")
        for lead in store.list_leads(conn, case_id)
        if lead["tool"] == "source_reliability"
    }
    for row in store.list_evidence(conn, case_id):
        src = row["source"]
        if not src or src == "unknown" or src in vetted_or_planned:
            continue
        if row["kind"] not in ("corroboration",):
            continue
        vetted_or_planned.add(src)
        _plan("source_reliability", {"source": src},
              f"vet {src!r}: it carries evidence in this case")

    if case["status"] == store.STATUS_OPEN:
        store.set_case_status(conn, case_id, store.STATUS_ACTIVE, now)
    store.record_event(
        conn, case_id, "leads_planned",
        {"new": len(planned), "tools": sorted({p["tool"] for p in planned})},
        now,
    )
    return {"case_id": case_id, "planned": planned, "count": len(planned)}


# --------------------------------------------------------------------------- #
# 3. Pursue
# --------------------------------------------------------------------------- #

def pursue_lead(
    conn,
    case_id: str,
    lead_id: str,
    clock: Callable[[], datetime] = _utcnow,
) -> Dict[str, Any]:
    """Execute one lead through the OSINT layer and harvest its evidence."""
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}
    lead = store.get_lead(conn, case_id, lead_id)
    if lead is None:
        return {"error": f"lead {lead_id!r} not found", "code": "not_found"}
    if lead["status"] != store.LEAD_OPEN:
        return {"error": f"lead {lead_id!r} is already {lead['status']}",
                "code": "not_open"}
    now = clock()
    hypotheses = store.list_hypotheses(conn, case_id)
    null_h = _null(hypotheses)

    harvest = _HARVESTERS.get(lead["tool"])
    if harvest is None:
        store.mark_lead(conn, case_id, lead_id, store.LEAD_FAILED, 0, now)
        return {"error": f"unknown lead tool {lead['tool']!r}", "code": "unknown_tool"}

    try:
        result = harvest(conn, case_id, lead, null_h, now)
    except Exception as exc:  # a broken lead never takes the case down
        result = {"error": str(exc)}
    if isinstance(result, dict) and result.get("error"):
        store.mark_lead(conn, case_id, lead_id, store.LEAD_FAILED, 0, now)
        store.record_event(
            conn, case_id, "lead_failed",
            {"lead_id": lead_id, "tool": lead["tool"], "error": result["error"]},
            now,
        )
        return {"ok": False, "lead_id": lead_id, "tool": lead["tool"],
                "error": result["error"]}

    found = int(result.get("evidence_found", 0))
    store.mark_lead(conn, case_id, lead_id, store.LEAD_PURSUED, found, now)
    store.record_event(
        conn, case_id, "lead_pursued",
        {"lead_id": lead_id, "tool": lead["tool"], "evidence_found": found,
         **{k: v for k, v in result.items() if k not in ("evidence_found",)}},
        now,
    )
    return {"ok": True, "lead_id": lead_id, "tool": lead["tool"],
            "evidence_found": found, **result}


def pursue_open_leads(
    conn,
    case_id: str,
    clock: Callable[[], datetime] = _utcnow,
    max_leads: Optional[int] = None,
) -> Dict[str, Any]:
    """Pursue every open lead (bounded by ``max_leads`` when given)."""
    leads = store.list_leads(conn, case_id, status=store.LEAD_OPEN)
    if max_leads is not None:
        leads = leads[: int(max_leads)]
    pursued, failed, evidence_found = 0, 0, 0
    for lead in leads:
        out = pursue_lead(conn, case_id, lead["lead_id"], clock=clock)
        if out.get("ok"):
            pursued += 1
            evidence_found += int(out.get("evidence_found", 0))
        else:
            failed += 1
    return {"case_id": case_id, "pursued": pursued, "failed": failed,
            "evidence_found": evidence_found}


# ---- Harvesters: one per lead tool. Each returns {"evidence_found": n, ...}
# or {"error": ...}; evidence rows are cited and credibility-weighted. ------- #

def _harvest_corroborate(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import corroborate

    claim_id = lead["params"].get("claim_id")
    out = corroborate(conn, claim_id)
    if out.get("error"):
        return out
    hyp = lead["hypothesis_id"]
    claim_text = (out.get("claim") or {}).get("text", "")
    opposed = lead["params"].get("stance") == "opposed"
    claim_word = "counter-claim" if opposed else "claim"
    found = 0
    for entry in out.get("support", []):
        relation = store.RELATION_CONTRADICTS if opposed else store.RELATION_SUPPORTS
        found += _attach_mirrored(
            conn, case_id, hyp, null_h, relation,
            "corroboration", claim_id, entry["source"], entry.get("credibility"),
            f"{entry['source']} independently supports {claim_word} {claim_id}: {claim_text}",
            lead["lead_id"], now,
        )
    for entry in out.get("contradict", []):
        relation = store.RELATION_SUPPORTS if opposed else store.RELATION_CONTRADICTS
        found += _attach_mirrored(
            conn, case_id, hyp, null_h, relation,
            "corroboration", claim_id, entry["source"], entry.get("credibility"),
            f"{entry['source']} contradicts {claim_word} {claim_id}: {claim_text}",
            lead["lead_id"], now,
        )
    extra: Dict[str, Any] = {}
    if out.get("single_sourced"):
        extra["single_sourced_claim"] = claim_id
        store.record_event(
            conn, case_id, "gap_noted",
            {"gap": f"claim {claim_id} is single-sourced; nothing independent corroborates it"},
            now,
        )
    return {"evidence_found": found, **extra}


def _attach_mirrored(
    conn, case_id, hypothesis_id, null_h, relation, kind, ref_id, source,
    credibility, summary, lead_id, now,
) -> int:
    """Attach an evidence row to its hypothesis and, when a null counterpart
    exists, the mirrored row with the relation flipped (support for the
    affirmative is contradiction of the null - the documented assumption)."""
    added = 0
    if store.upsert_evidence(
        conn, case_id, hypothesis_id, relation, kind, ref_id, source,
        credibility, True, summary, lead_id, now,
    ):
        added += 1
    if null_h is not None and hypothesis_id is not None \
            and hypothesis_id != null_h["hypothesis_id"]:
        flipped = (
            store.RELATION_CONTRADICTS
            if relation == store.RELATION_SUPPORTS
            else store.RELATION_SUPPORTS
        )
        store.upsert_evidence(
            conn, case_id, null_h["hypothesis_id"], flipped, kind, ref_id,
            source, credibility, True, f"mirror: {summary}", lead_id, now,
        )
    return added


def _harvest_contradiction_scan(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import contradiction_scan

    out = contradiction_scan(conn, topic=lead["params"].get("topic"),
                             entity=lead["params"].get("entity"))
    if out.get("error"):
        return out
    found = 0
    for entry in out.get("contradictions", [])[:20]:
        a, b = entry["claim_a"], entry["claim_b"]
        if store.upsert_evidence(
            conn, case_id, None, store.RELATION_CONTEXT, "contradiction",
            f"{a['claim_id']}|{b['claim_id']}",
            f"{a['source']} vs {b['source']}", None, bool(entry.get("cited")),
            f"the record disagrees: {a['source']}: \"{a['text']}\" vs "
            f"{b['source']}: \"{b['text']}\"",
            lead["lead_id"], now,
        ):
            found += 1
    return {"evidence_found": found, "uncited": int(out.get("uncited_count", 0))}


def _harvest_timeline(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import timeline_reconstruct

    out = timeline_reconstruct(conn, topic=lead["params"].get("topic"),
                               entity=lead["params"].get("entity"))
    if out.get("error"):
        return out
    found = 0
    for event in out.get("events", [])[:12]:
        entries = event.get("entries", [])
        first = entries[0]["text"] if entries else ""
        if store.upsert_evidence(
            conn, case_id, None, store.RELATION_CONTEXT, "event",
            event.get("date"), f"{event.get('corroboration_density', 0)} independent sources",
            None, int(event.get("uncited_count", 0)) == 0,
            f"{event.get('date')}: {first}", lead["lead_id"], now,
        ):
            found += 1
    return {"evidence_found": found, "events": int(out.get("count", 0))}


def _harvest_dossier(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import entity_dossier

    entity = lead["params"].get("entity")
    out = entity_dossier(conn, entity)
    if out.get("error"):
        return out
    connected = [c.get("entity") or c.get("name") for c in out.get("connected_entities", [])[:3]]
    added = store.upsert_evidence(
        conn, case_id, None, store.RELATION_CONTEXT, "dossier", entity, "corpus",
        None, bool(out.get("found")),
        f"dossier for {entity}: {out.get('mention_count', 0)} mentions "
        f"({out.get('first_seen')} to {out.get('last_seen')}); "
        f"connected: {', '.join(str(c) for c in connected if c) or 'none'}",
        lead["lead_id"], now,
    )
    return {"evidence_found": int(added), "mentions": int(out.get("mention_count", 0))}


def _harvest_path(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import relationship_path

    a, b = lead["params"].get("a"), lead["params"].get("b")
    out = relationship_path(conn, a, b)
    if out.get("error"):
        return out
    connected = bool(out.get("connected"))
    summary = (
        f"{a} and {b} connect in {out.get('hops')} hops: {' -> '.join(out.get('path', []))}"
        if connected else f"no documented connection between {a} and {b}"
    )
    added = store.upsert_evidence(
        conn, case_id, None, store.RELATION_CONTEXT, "connection", f"{a}|{b}",
        "corpus", None, connected, summary, lead["lead_id"], now,
    )
    return {"evidence_found": int(added), "connected": connected}


def _harvest_source_reliability(conn, case_id, lead, null_h, now) -> Dict[str, Any]:
    from src.osint import source_reliability

    source = lead["params"].get("source")
    out = source_reliability(conn, source)
    if out.get("error"):
        return out
    rel = out.get("reliability") or {}
    added = store.upsert_evidence(
        conn, case_id, None, store.RELATION_CONTEXT, "source-vetting", source,
        source, rel.get("value"), bool(out.get("found")),
        f"vetted {source}: reliability {rel.get('value')} "
        f"[{rel.get('lo')}, {rel.get('hi')}] over {out.get('n', 0)} records",
        lead["lead_id"], now,
    )
    return {"evidence_found": int(added), "reliability": rel.get("value")}


_HARVESTERS = {
    "corroborate": _harvest_corroborate,
    "contradiction_scan": _harvest_contradiction_scan,
    "timeline_reconstruct": _harvest_timeline,
    "entity_dossier": _harvest_dossier,
    "relationship_path": _harvest_path,
    "source_reliability": _harvest_source_reliability,
}


# --------------------------------------------------------------------------- #
# 4. Evaluate
# --------------------------------------------------------------------------- #

def hypothesis_matrix(conn, case_id: str) -> Dict[str, Any]:
    """ACH-style scoring of the case's hypotheses against its evidence.

    Per hypothesis: independent supporting/contradicting sources, weighted
    tallies, diagnostic sources (supporting this hypothesis and no other),
    and a single-source flag. Ranked by net weighted support; the leader
    carries a calibrated support-credibility interval, never a bare score.
    """
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}
    hypotheses = store.list_hypotheses(conn, case_id)
    evidence = store.list_evidence(conn, case_id)

    support_sources: Dict[str, Dict[str, float]] = {h["hypothesis_id"]: {} for h in hypotheses}
    contradict_sources: Dict[str, Dict[str, float]] = {h["hypothesis_id"]: {} for h in hypotheses}
    for row in evidence:
        hid = row["hypothesis_id"]
        if hid is None or hid not in support_sources:
            continue
        cred = common.credibility_or_default(row["credibility"])
        bucket = (
            support_sources[hid] if row["relation"] == store.RELATION_SUPPORTS
            else contradict_sources[hid] if row["relation"] == store.RELATION_CONTRADICTS
            else None
        )
        if bucket is None:
            continue
        # Independence: one entry per distinct source, keeping its strongest weight.
        bucket[row["source"]] = max(bucket.get(row["source"], 0.0), cred)

    scored = []
    for h in hypotheses:
        hid = h["hypothesis_id"]
        sup, con = support_sources[hid], contradict_sources[hid]
        others_support = {
            s for other, srcs in support_sources.items() if other != hid for s in srcs
        }
        scored.append(
            {
                **h,
                "independent_support_count": len(sup),
                "independent_contradict_count": len(con),
                "weighted_support": round(sum(sup.values()), 3),
                "weighted_contradict": round(sum(con.values()), 3),
                "net": round(sum(sup.values()) - sum(con.values()), 3),
                "diagnostic_sources": sorted(set(sup) - others_support),
                "single_sourced": len(sup) <= 1,
            }
        )
    scored.sort(key=lambda s: s["net"], reverse=True)

    leader = scored[0] if scored else None
    runner = scored[1] if len(scored) > 1 else None
    margin = round((leader["net"] - runner["net"]), 3) if leader and runner else None

    # A calibrated interval over the leader's supporting credibilities, as in
    # corroborate(): the spread of its witnesses is the calibration sample.
    level = 0.9
    support_credibility = None
    calib = {"coverage": None, "calibration_n": 0}
    if leader:
        creds = sorted(support_sources[leader["hypothesis_id"]].values())
        if creds:
            mean_cred = sum(creds) / len(creds)
            residuals = [c - mean_cred for c in creds] if len(creds) >= 2 else [0.25]
            band = conformal_interval(mean_cred, residuals, level)
            support_credibility = interval(
                mean_cred, max(0.0, band["lo"]), min(1.0, band["hi"]), level
            )
            calib = calibrated_envelope_fields(residuals, level)

    all_sources = {
        row["source"] for row in evidence
        if row["relation"] in (store.RELATION_SUPPORTS, store.RELATION_CONTRADICTS)
        and row["source"]
    }
    contradictions_in_record = sum(1 for r in evidence if r["kind"] == "contradiction")
    uncited = sum(1 for r in evidence if not r["cited"])

    return analytic_envelope(
        n=len(all_sources),
        method=METHOD,
        assumptions=ASSUMPTIONS,
        case_id=case_id,
        question=case["question"],
        status=case["status"],
        hypotheses=scored,
        leader=leader["hypothesis_id"] if leader else None,
        margin=margin,
        support_credibility=support_credibility,
        support_coverage=calib["coverage"],
        support_calibration_n=calib["calibration_n"],
        open_leads=len(store.list_leads(conn, case_id, status=store.LEAD_OPEN)),
        contradictions_in_record=contradictions_in_record,
        uncited_evidence_count=uncited,
        evidence_count=len(evidence),
    )


# --------------------------------------------------------------------------- #
# 5. Conclude
# --------------------------------------------------------------------------- #

def conclude_case(
    conn,
    case_id: str,
    clock: Callable[[], datetime] = _utcnow,
    min_sources: int = CONCLUDE_MIN_INDEPENDENT_SOURCES,
    min_margin: float = CONCLUDE_MIN_WEIGHTED_MARGIN,
) -> Dict[str, Any]:
    """Attempt a verdict through the evidence-discipline gate. On failure the
    case stays open and every gap is named; the engine never forces a call."""
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}
    if case["status"] == store.STATUS_CONCLUDED:
        return {"concluded": True, "case_id": case_id, "verdict": case["verdict"],
                "hypothesis": case["verdict_hypothesis"], "already": True}
    now = clock()
    matrix = hypothesis_matrix(conn, case_id)
    scored = matrix["hypotheses"]
    leader = scored[0] if scored else None

    gaps: List[str] = []
    open_leads = matrix["open_leads"]
    if open_leads:
        gaps.append(f"{open_leads} planned lead(s) not yet pursued")
    if leader is None:
        gaps.append("no hypotheses to weigh")
    else:
        if leader["independent_support_count"] < min_sources:
            gaps.append(
                f"leading hypothesis {leader['hypothesis_id']} has "
                f"{leader['independent_support_count']} independent supporting "
                f"source(s); the gate requires {min_sources}"
            )
        if matrix["margin"] is not None and matrix["margin"] < min_margin:
            gaps.append(
                f"weighted margin over the runner-up is {matrix['margin']}; "
                f"the gate requires {min_margin}"
            )
        if leader["weighted_contradict"] >= leader["weighted_support"]:
            gaps.append(
                "the leading hypothesis carries as much contradiction as support"
            )

    if gaps:
        store.record_event(
            conn, case_id, "conclusion_withheld", {"gaps": gaps}, now
        )
        return {"concluded": False, "case_id": case_id, "gaps": gaps,
                "leader": leader["hypothesis_id"] if leader else None,
                "matrix": matrix}

    verdict = (
        f"the record does not support the question as posed: {leader['statement']}"
        if leader["kind"] == KIND_NULL
        else f"supported by the record: {leader['statement']}"
    )
    for h in scored:
        store.set_hypothesis_status(
            conn, case_id, h["hypothesis_id"],
            store.HYPOTHESIS_SUPPORTED if h["hypothesis_id"] == leader["hypothesis_id"]
            else store.HYPOTHESIS_UNSUPPORTED,
        )
    store.set_case_verdict(conn, case_id, verdict, leader["hypothesis_id"], now)
    store.record_event(
        conn, case_id, "case_concluded",
        {"verdict": verdict, "hypothesis": leader["hypothesis_id"],
         "independent_sources": leader["independent_support_count"],
         "margin": matrix["margin"]},
        now,
    )
    return {"concluded": True, "case_id": case_id, "verdict": verdict,
            "hypothesis": leader["hypothesis_id"], "matrix": matrix}


# --------------------------------------------------------------------------- #
# The drive loop and the case file
# --------------------------------------------------------------------------- #

def advance_case(
    conn, case_id: str, clock: Callable[[], datetime] = _utcnow
) -> Dict[str, Any]:
    """One engine round: plan new leads, pursue everything open, re-score."""
    planned = plan_leads(conn, case_id, clock=clock)
    if planned.get("error"):
        return planned
    pursued = pursue_open_leads(conn, case_id, clock=clock)
    return {
        "case_id": case_id,
        "planned": planned["count"],
        "pursued": pursued["pursued"],
        "failed": pursued["failed"],
        "evidence_found": pursued["evidence_found"],
        "matrix": hypothesis_matrix(conn, case_id),
    }


def run_case(
    conn,
    question: str,
    hypotheses: Optional[List[str]] = None,
    topic: Optional[str] = None,
    entities: Optional[List[str]] = None,
    max_rounds: int = 3,
    conclude: bool = True,
    clock: Callable[[], datetime] = _utcnow,
) -> Dict[str, Any]:
    """Drive a whole investigation: open, then plan/pursue rounds until no new
    leads emerge (or the round budget runs out), then attempt a disciplined
    conclusion. Returns the case file with the final matrix and the
    conclusion attempt (verdict or named gaps)."""
    opened = open_case(
        conn, question, hypotheses=hypotheses, topic=topic, entities=entities,
        clock=clock,
    )
    if opened.get("error"):
        return opened
    case_id = opened["case"]["case_id"]

    rounds = 0
    for _ in range(max(1, int(max_rounds))):
        planned = plan_leads(conn, case_id, clock=clock)
        open_now = store.list_leads(conn, case_id, status=store.LEAD_OPEN)
        if not open_now and not planned["count"]:
            break
        pursue_open_leads(conn, case_id, clock=clock)
        rounds += 1

    conclusion = (
        conclude_case(conn, case_id, clock=clock)
        if conclude
        else {"concluded": False, "case_id": case_id, "gaps": ["conclusion not requested"]}
    )
    return {
        "case": store.get_case(conn, case_id),
        "rounds": rounds,
        "matrix": hypothesis_matrix(conn, case_id),
        "conclusion": conclusion,
        "file": case_file(conn, case_id),
    }


def case_file(conn, case_id: str) -> Dict[str, Any]:
    """The full durable state of a case: record, hypotheses, evidence, leads,
    and the journal (oldest first, so it reads as the investigation's story)."""
    case = store.get_case(conn, case_id)
    if case is None:
        return {"error": f"case {case_id!r} not found", "code": "not_found"}
    evidence = store.list_evidence(conn, case_id)
    leads = store.list_leads(conn, case_id)
    return {
        "case": case,
        "hypotheses": store.list_hypotheses(conn, case_id),
        "evidence": evidence,
        "leads": leads,
        "journal": store.list_events(conn, case_id),
        "evidence_count": len(evidence),
        "open_lead_count": sum(1 for l in leads if l["status"] == store.LEAD_OPEN),
        "reconstructable": True,
    }

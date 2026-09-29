"""Build company-dossier inputs from acquired SEC materials and filed facts (#1673).

``MarketResearchStore.company_dossier`` accepts caller-assembled rows. This
module derives those rows deterministically from a
``noesis-market-materials-v1`` artifact (see ``edgar_materials``) and filed
quarterly revenue facts, and adds two checks the dossier needs:

* **Headline reconciliation** – the revenue figure stated in each earnings
  release is compared with the filed XBRL value for the same quarter at the
  precision the release states (``$10.2 billion`` agrees within ±$50M).
* **Guidance comparison** – a quarterly revenue range guided in one release is
  compared with the actual revenue of the next reported quarter. The guided
  range is a management claim published before the event; the actual and the
  delta are calculations from filed facts.

Anything that cannot be matched is reported as a gap, never estimated.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

_SCALE = {"billion": Decimal(10) ** 9, "million": Decimal(10) ** 6, "b": Decimal(10) ** 9, "m": Decimal(10) ** 6}
_MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|million|B|M)\b"
# Same-line only: a table row ("Total revenue" / "$6.80 billion") is often a
# forward target in a Targets table, not the reported headline.
_HEADLINE = re.compile(
    r"\b(?:total[ \t]+)?revenues?[ \t]+(?:of|was|were|totaled|grew[ \t]+to|increased[ \t]+to|reached)?[ \t]*"
    + _MONEY,
    re.I,
)
_RANGE = re.compile(_MONEY + r"\s*(?:to|-|–)\s*" + _MONEY, re.I)
_DAY_MS = 86_400_000


def _money(number: str, scale: str) -> tuple[Decimal, Decimal] | None:
    """Value in USD and the half-unit of its last stated digit."""

    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    unit = _SCALE[scale.lower()]
    decimals = -value.as_tuple().exponent if value.as_tuple().exponent < 0 else 0
    return value * unit, Decimal(5).scaleb(-decimals - 1) * unit


_SUBLINE = re.compile(
    r"(subscription|support|cloud|services?|licen[cs]e|product|segment|recurring|deferred|"
    r"unearned|remaining performance|rpo|digital media|digital experience|software|hardware|"
    r"infrastructure|applications?|annualized|arr|backlog)[\s&,/-]*(?:and\s+\w+\s*)?$",
    re.I,
)


def headline_revenue(text: str) -> dict[str, Any] | None:
    """The total revenue figure stated in a release, with its rounding tolerance.

    Prefers an explicit "total revenue" figure; otherwise the first revenue
    figure not qualified by a sub-line word (subscription, cloud, license, ...).
    """

    candidates = []
    for match in _HEADLINE.finditer(text):
        prefix = text[max(0, match.start() - 40):match.start()].split("\n")[-1]
        qualified = bool(_SUBLINE.search(prefix))
        total = match.group(0).lower().startswith("total")
        if qualified and not total:
            continue
        parsed = _money(match.group(1), match.group(2))
        if parsed is None:
            continue
        candidates.append((not total, match.start(), match, parsed))
    if not candidates:
        return None
    _, _, match, (value, tolerance) = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "value": format(value.normalize(), "f"),
        "tolerance": format(tolerance.normalize(), "f"),
        "text": match.group(0),
        "start": match.start(),
        "end": match.end(),
    }


def quarterly_revenue_from_facts(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """As-first-filed quarterly revenue rows from normalized financial facts.

    Uses mapped ``revenue`` facts in USD. For each quarter end the earliest
    public filing is kept. A fiscal fourth quarter, which companies report only
    inside the annual figure, is derived as annual minus the three filed
    quarters of that year and labeled ``derivation="annual_minus_three_quarters"``.
    """

    def end_ms(fact: Mapping[str, Any], key: str = "end_date") -> int | None:
        value = (fact.get("period") or {}).get(key)
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(f"{value}T00:00:00+00:00").timestamp() * 1000)
        except (TypeError, ValueError):
            return None

    quarters: dict[int, dict[str, Any]] = {}
    annuals: dict[int, dict[str, Any]] = {}
    for fact in facts:
        if fact.get("canonical_concept") != "revenue" or fact.get("unit") != "USD":
            continue
        target = quarters if fact.get("period_class") == "quarter" else annuals if fact.get("period_class") == "annual" else None
        end = end_ms(fact)
        if target is None or end is None or fact.get("public_at_ms") is None:
            continue
        row = {
            "period_end_ms": end,
            "period_start_ms": end_ms(fact, "start_date"),
            "value": str(fact["value_lexical"]),
            "public_at_ms": int(fact["public_at_ms"]),
            "source_revision_id": fact.get("revision_id"),
        }
        if end not in target or row["public_at_ms"] < target[end]["public_at_ms"]:
            target[end] = row
    for end, annual in annuals.items():
        if end in quarters or annual["period_start_ms"] is None:
            continue
        inside = [q for q in quarters.values() if annual["period_start_ms"] <= (q["period_start_ms"] or 0) and q["period_end_ms"] < end]
        if len(inside) != 3:
            continue
        derived = Decimal(annual["value"]) - sum(Decimal(q["value"]) for q in inside)
        quarters[end] = {
            "period_end_ms": end,
            "period_start_ms": max(q["period_end_ms"] for q in inside) + _DAY_MS,
            "value": format(derived.normalize(), "f"),
            "public_at_ms": max(annual["public_at_ms"], *(q["public_at_ms"] for q in inside)),
            "source_revision_id": annual["source_revision_id"],
            "derivation": "annual_minus_three_quarters",
            "derived_from_revision_ids": [annual["source_revision_id"], *(q["source_revision_id"] for q in inside)],
        }
    return [quarters[end] for end in sorted(quarters)]


def quarterly_revenue_guidance(statement: str) -> dict[str, Any] | None:
    """A guided quarterly revenue range ("... third quarter revenue guidance of $A to $B").

    The first range after the quarter reference is used, so a sentence that
    also restates full-year guidance does not borrow the annual range.
    """

    quarter = re.search(
        r"\b(?:(?:first|second|third|fourth|next)\s+(?:fiscal\s+)?quarter|Q[1-4])\b", statement, re.I
    )
    if not quarter or not re.search(r"\brevenue\b", statement[quarter.start():], re.I):
        return None
    match = _RANGE.search(statement, quarter.end())
    if not match:
        return None
    low, high = _money(match.group(1), match.group(2)), _money(match.group(3), match.group(4))
    if low is None or high is None or low[0] > high[0]:
        return None
    return {"low": format(low[0].normalize(), "f"), "high": format(high[0].normalize(), "f"), "range_text": match.group(0)}


def _quarter_for_event(facts: Sequence[Mapping[str, Any]], event_at_ms: int) -> Mapping[str, Any] | None:
    """The filed quarter ending within 100 days before a results event."""

    candidates = [
        fact for fact in facts
        if fact.get("period_end_ms") is not None
        and 0 <= event_at_ms - int(fact["period_end_ms"]) <= 100 * _DAY_MS
    ]
    return max(candidates, key=lambda fact: int(fact["period_end_ms"]), default=None)


def build_dossier_inputs(
    materials_artifact: Mapping[str, Any],
    *,
    quarterly_revenue: Sequence[Mapping[str, Any]],
    release_texts: Mapping[str, str],
) -> dict[str, Any]:
    """Derive ``company_dossier`` keyword arguments from acquired evidence.

    ``quarterly_revenue`` rows need ``period_end_ms``, ``value`` (USD),
    ``public_at_ms`` and ``source_revision_id`` (filed quarter facts).
    ``release_texts`` maps an earnings-release ``material_id`` to the
    block-normalized text used for guidance spans (``edgar_materials.document_text``).
    """

    materials = [
        dict(row) for row in materials_artifact.get("materials", [])
        if isinstance(row, Mapping)
    ]
    public = [row for row in materials if row.get("licensing_status") == "public"]
    release_history = [row for row in public if row.get("kind") == "earnings_release"]
    superseded_ids = {
        str(row["corrects_material_id"])
        for row in release_history
        if row.get("corrects_material_id")
    }
    releases = sorted(
        (
            row
            for row in release_history
            if not row.get("duplicate_of") and row.get("material_id") not in superseded_ids
        ),
        key=lambda row: (row.get("event_at_ms") or row["published_at_ms"], row["published_at_ms"]),
    )
    guidance_by_release = {
        row.get("derived_from_material_id"): row for row in public if row.get("kind") == "guidance"
    }
    gaps: list[dict[str, Any]] = []
    headline_checks: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    previous_guidance: dict[str, Any] | None = None
    previous_actual: Mapping[str, Any] | None = None
    for release in releases:
        event_at = int(release.get("event_at_ms") or release["published_at_ms"])
        quarter = _quarter_for_event(quarterly_revenue, event_at)
        text = release_texts.get(release["material_id"])
        headline = headline_revenue(text) if text else None
        if quarter is None:
            gaps.append({"code": "filed_quarter_unavailable", "material_id": release["material_id"]})
        if headline is None:
            gaps.append({"code": "headline_revenue_not_found", "material_id": release["material_id"]})
        if quarter is not None and headline is not None:
            filed = Decimal(str(quarter["value"]))
            difference = abs(Decimal(headline["value"]) - filed)
            headline_checks.append({
                "material_id": release["material_id"],
                "reporting_period": release.get("reporting_period"),
                "release_value": headline["value"],
                "release_span": {"start": headline["start"], "end": headline["end"], "text": headline["text"]},
                "filed_value": format(filed.normalize(), "f"),
                "filed_source_revision_id": quarter.get("source_revision_id"),
                "filed_derivation": quarter.get("derivation", "filed"),
                "tolerance": headline["tolerance"],
                "status": "consistent_at_stated_precision" if difference <= Decimal(headline["tolerance"]) else "mismatch",
            })
        if quarter is not None:
            actual = float(quarter["value"])
            row: dict[str, Any] = {
                "metric": "revenue",
                "period_end_ms": int(quarter["period_end_ms"]),
                "actual": actual,
                "prior": None if previous_actual is None else float(previous_actual["value"]),
                "event_at_ms": event_at,
                "source_revision_id": quarter.get("source_revision_id"),
                "release_material_id": release["material_id"],
            }
            if previous_guidance is not None:
                low, high = Decimal(previous_guidance["low"]), Decimal(previous_guidance["high"])
                row["guidance"] = {
                    "value": float((low + high) / 2),
                    "low": float(low),
                    "high": float(high),
                    "public_at_ms": previous_guidance["public_at_ms"],
                    "material_id": previous_guidance["material_id"],
                    "claim_status": "management_claim",
                    "range_text": previous_guidance["range_text"],
                    "outcome": "within_range" if low <= Decimal(str(actual)) <= high else "above_range" if Decimal(str(actual)) > high else "below_range",
                }
            else:
                gaps.append({"code": "prior_quarter_guidance_unavailable", "material_id": release["material_id"]})
            comparisons.append(row)
            previous_actual = quarter
        previous_guidance = None
        guidance = guidance_by_release.get(release["material_id"])
        for statement in (guidance or {}).get("statements", []):
            evidence.append({
                "stance": "neutral",
                "claim_kind": "management",
                "text": statement["text"],
                "source_span": {"start": statement["start"], "end": statement["end"]},
                "source_revision_id": guidance["source_revision_id"],
                "public_at_ms": guidance["published_at_ms"],
            })
            parsed = quarterly_revenue_guidance(statement["text"])
            if parsed is not None and previous_guidance is None:
                previous_guidance = {
                    **parsed,
                    "public_at_ms": guidance["published_at_ms"],
                    "material_id": guidance["material_id"],
                }
    for check in headline_checks:
        evidence.append({
            "stance": "supporting" if check["status"] != "mismatch" else "contradicting",
            "claim_kind": "reconciliation",
            "text": f"Release headline revenue {check['release_value']} vs filed {check['filed_value']}",
            "source_span": check["release_span"],
            "source_revision_id": check["filed_source_revision_id"],
        })
    segments = [
        {
            "public_at_ms": row["published_at_ms"],
            "source_revision_id": row["source_revision_id"],
            "segments": row.get("segments", []),
            "segment_members": row.get("segment_members", []),
        }
        for row in public if row.get("kind") == "segment_disclosure"
    ]
    ownership = [
        {
            "public_at_ms": row["published_at_ms"],
            "as_of_ms": row.get("event_at_ms") or row["published_at_ms"],
            "source_revision_id": row["source_revision_id"],
            "kind": row["kind"],
            "form": row.get("form"),
            "owners": row.get("owners", []),
            "transactions": row.get("transactions", []),
        }
        for row in public if row.get("kind") in {"insider_transaction", "beneficial_ownership"}
    ]
    unavailable = [row["kind"] for row in materials if row.get("licensing_status") in {"unlicensed", "unavailable"}]
    material_change_history = [
        {
            "material_id": row["material_id"],
            "source_revision_id": row.get("source_revision_id"),
            "published_at_ms": row.get("published_at_ms"),
            "corrects_material_id": row.get("corrects_material_id"),
            "duplicate_of": row.get("duplicate_of"),
        }
        for row in release_history
        if row.get("corrects_material_id") or row.get("duplicate_of")
    ]
    source_gaps = [*gaps, *({"code": "source_unavailable", "kind": kind} for kind in unavailable)]
    return {
        "statements": [dict(row) for row in quarterly_revenue],
        # Keep every public release/presentation revision for citation and
        # audit, while only the active correction-chain head is analysed.
        "materials": [row for row in public if row.get("kind") in {"earnings_release", "earnings_presentation"}],
        "comparisons": comparisons,
        "evidence": evidence,
        "segment_disclosures": segments,
        "ownership": ownership,
        "headline_reconciliation": headline_checks,
        "material_change_history": material_change_history,
        "gaps": source_gaps,
        "input_gaps": source_gaps,
        "unavailable_sources": unavailable,
    }


__all__ = ["build_dossier_inputs", "headline_revenue", "quarterly_revenue_from_facts", "quarterly_revenue_guidance"]

"""Derived dividend-flow estimates along ownership chains (#2891).

Noesis records who holds what (Corporate Ownership assertions) and what an
issuer declares (Market corporate actions and filed XBRL facts). This module
combines the two into **derived estimates** of the dividend attributable to a
holder, directly or looked through a chain of stated holdings, and never
presents one as a reported figure:

* every estimate is labelled ``derived`` (``reported: false``), carries the
  method id and version, the exact record revision of each cited input, an
  interval, ``n`` (the number of cited inputs) and the assumptions it rests on
  (the evidence contract's "numbers carry their uncertainty");
* holdings are read as stated in force at the dividend's record date (ex-date
  when no record date is stated; period end for a per-period XBRL fact). A
  stated band stays a band; a gap between the holding's stated date and the
  dividend date widens the interval only by a rule of the disclosure regime
  (WpHG thresholds, PSC bands, the Schedule 13D one-percent amendment rule, the
  next 13F report) and the interval says when it does **not** bound timing;
* holdings below a regime's publication threshold are never estimated, a link
  without a stated share stays an unknown link, a natural-person holder is
  withheld unless person-research mode is on, and nothing is summed across
  paths or netted across currencies (no rate is cited, so none is applied);
* the walk has an explicit ``max_depth`` and reports cycles and truncation.

:func:`reproduce` recomputes every estimate from exactly the input revisions it
cites, after checking their record hashes. :func:`export_bundle` writes the
answer as a ``noesis-evidence-bundle-v1`` with one evidence object per cited
input revision and refuses an answer whose estimates are not labelled derived.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
)
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.kb.ownership_records import CONTRACT_V2, digest

CONTRACT = "noesis-dividend-flow-estimate-v1"
QUERY = "estimate-dividend-flows"
METHOD_ID = "noesis-dividend-flow-lookthrough"
METHOD_VERSION = "1.0.0"
# The repo's honesty envelope (src/analytics/honesty.py) wants ``method`` as a string beside ``n`` and
# ``assumptions``; the id and version are also given apart so callers never parse the string.
METHOD = f"{METHOD_ID}@{METHOD_VERSION}"
LABEL = "derived"
DEFAULT_MAX_DEPTH = 3
DEFAULT_WINDOW_DAYS = 365
MAX_WINDOW_DAYS = 3660
MAX_UNBOUNDED_GAP_DAYS = 92
MAX_EVENTS = 500
MAX_ESTIMATES = 5000
HOLDING_KINDS = ("shareholding", "voting_rights", "reported_holding")
CENT = Decimal("0.01")
HUNDRED = Decimal(100)
ZERO = Decimal(0)
WPHG_S33_THRESHOLDS = tuple(Decimal(t) for t in ("3", "5", "10", "15", "20", "25", "30", "50", "75"))
DIVIDEND_PER_SHARE_CONCEPTS = (
    ("us-gaap", "CommonStockDividendsPerShareDeclared"),
    ("ifrs-full", "DividendsRecognisedAsDistributionsToOwnersPerShare"),
)
SHARES_OUTSTANDING_CONCEPTS = (("us-gaap", "CommonStockSharesOutstanding"), ("ifrs-full", "NumberOfSharesOutstanding"))
DIVIDEND_PAYMENT_CONCEPTS = (("us-gaap", "PaymentsOfDividendsCommonStock"), ("us-gaap", "PaymentsOfDividends"))
SHARE_COUNT_ACTIONS = ("split", "reverse_split", "stock_dividend")
NOTICE = ("Every amount under 'estimates' is a derived estimate (method noesis-dividend-flow-lookthrough), not a "
          "reported figure: a declared per-share dividend multiplied by a stated holding, looked through stated "
          "holdings where the path is longer than one link. Look-through amounts are attributable, not cash an owner "
          "received. Estimates are per path and per currency; they are never summed across paths or netted across "
          "currencies. Holdings below a publication threshold are not estimated and unknown links stay unknown. Not "
          "investment, tax or beneficial-ownership advice or a determination.")

# Which machine-readable sources feed the method, and which do not (with the reason). Recorded in every answer.
DIVIDEND_SOURCES = {
    "market-corporate-actions": {
        "status": "implemented", "store": "src.domains.market.actions",
        "records": "noesis-market-corporate-action-v1 cash_dividend actions with status confirmed or corrected",
        "dates": "record date, else ex-date, is the holding date"},
    "sec-companyfacts-dividends-per-share": {
        "status": "implemented", "store": "src.domains.market.financial_facts",
        "concepts": [f"{t}:{c}" for t, c in DIVIDEND_PER_SHARE_CONCEPTS],
        "use": "only for an issuer without a corporate-action dividend in the window; the period end is the holding "
               "date and the whole period is the timing gap"},
    "sec-companyfacts-shares-outstanding": {
        "status": "implemented", "store": "src.domains.market.financial_facts",
        "concepts": [f"{t}:{c}" for t, c in SHARES_OUTSTANDING_CONCEPTS],
        "use": "converts a stated percentage of the class into shares"},
    "sec-companyfacts-dividend-payments": {
        "status": "context-only", "store": "src.domains.market.financial_facts",
        "concepts": [f"{t}:{c}" for t, c in DIVIDEND_PAYMENT_CONCEPTS],
        "reason": "a company-wide cash total (it can include preferred and non-controlling dividends); shown as "
                  "reported context beside the estimates, never attributed to a holder"},
    "sec-dei-entity-common-stock-shares-outstanding": {
        "status": "not-implemented",
        "reason": "the CompanyFacts market-fact path does not index the dei cover taxonomy "
                  "(src/ingestion/connectors/edgar.py), so dei:EntityCommonStockSharesOutstanding is not available"},
    "bundesanzeiger": {
        "status": "not-implemented",
        "reason": "no permitted machine access: the Bundesanzeiger publishes annual accounts and dividend resolutions "
                  "as web documents behind a search interface with no API or bulk export, and its terms of use do "
                  "not permit automated retrieval; see docs/development/dividend-flow-evidence/source-audit.md"},
}

# Disclosure regimes: publication threshold, how a gap between the stated date and the dividend date is bounded.
REGIMES = {
    "bafin-wphg-s33": {
        "publication_threshold_pct": "3", "timing_rule": "threshold-bracket", "notification_lag_days": 10,
        "rule": "WpHG s. 33: reaching, exceeding or falling below 3, 5, 10, 15, 20, 25, 30, 50 or 75 percent of "
                "voting rights is notified within four trading days and published within three more; without a "
                "later notification the holding stays inside the threshold bracket of the stated percentage"},
    "bafin-wphg-instruments": {
        "publication_threshold_pct": "5", "timing_rule": "not-used",
        "rule": "WpHG s. 38 instruments and the s. 39 total include instruments that carry no dividend; not used"},
    "uk-psc": {
        "publication_threshold_pct": "25", "timing_rule": "stated-band", "notification_lag_days": 28,
        "rule": "UK PSC / ROE registers: more than 25 percent of shares, stated in bands; a change of band is "
                "entered within 14 days and filed within a further 14 days, so the stated band bounds the holding"},
    "bods": {
        "publication_threshold_pct": None, "timing_rule": "stated-band",
        "rule": "Beneficial Ownership Data Standard statement as published; the publisher's own threshold applies "
                "and is not assumed; a stated range bounds the holding, an exact share does not bound timing"},
    "sec-13d": {
        "publication_threshold_pct": "5", "timing_rule": "material-change-1pp", "notification_lag_days": 4,
        "rule": "Exchange Act Rule 13d-2(a): an acquisition or disposition of one percent or more of the class is a "
                "material change that requires an amendment within two business days; without one the holding "
                "stays within one percentage point of the stated percentage"},
    "sec-13g": {
        "publication_threshold_pct": "5", "timing_rule": "not-bounded",
        "rule": "Schedule 13G amendments follow quarter ends; method v1 applies no bracket"},
    "sec-13f": {
        "publication_threshold_pct": None, "timing_rule": "next-report-hull",
        "rule": "Form 13F: quarter-end positions with no intra-quarter change reporting (positions under 10,000 "
                "shares and USD 200,000 may be omitted); when the next report period is on record the holding is "
                "bracketed by the two reported positions, otherwise timing is not bounded"},
    "other": {
        "publication_threshold_pct": None, "timing_rule": "not-bounded",
        "rule": "no disclosure rule is applied; timing is not bounded"},
}

ASSUMPTIONS = {
    "class-share": "a holding's stated share of the class is its share of the dividend declared on that class; a "
                   "WpHG s. 33 voting-rights percentage is read as a share of the dividend-bearing class (no "
                   "non-voting or treasury-share adjustment)",
    "look-through": "a look-through estimate attributes an intermediate holder's estimated receipt to its owners in "
                    "proportion to their stated shares; it is not cash the owner received, which depends on the "
                    "intermediate's own distributions",
    "holding-date": "a holding is read as stated in force at the dividend's record date (ex-date when none is "
                    "stated, period end for a period fact); a different stated date widens the interval by the "
                    "regime rule named in the link, or the interval states that it does not bound timing",
    "gross": "gross amounts in the declared currency, before withholding tax, fees and conversion; currencies are "
             "never converted or netted because no exchange rate is cited",
    "shares-outstanding": "a percentage is converted to shares with the cited shares-outstanding fact nearest the "
                          "dividend date; issuance and buybacks between the two dates are not modelled",
    "statement-hull": "several statements of one holder-subject link are combined as the hull of their intervals, "
                      "never summed; rows of one 13F manager's report period are summed as that report's position",
    "no-share-count-change": "no split, reverse split or stock dividend lies between the holding date and the "
                             "dividend date (checked against the market corporate actions; an estimate crossing one "
                             "is not made)",
}


class DividendFlowError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


# --------------------------------------------------------------------------- small helpers


def _day(value: Any) -> date | None:
    if not value:
        return None
    parts = str(value)[:10].split("-")
    try:
        return date.fromisoformat("-".join(parts + ["01"] * (3 - len(parts))))
    except ValueError:
        return None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _end_of_day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000 - 1


def _dec(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DividendFlowError("contract_invalid", f"not a decimal: {value!r}") from exc
    if not number.is_finite():
        raise DividendFlowError("contract_invalid", "decimal must be finite")
    return number


def _txt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _money(value: Decimal | None, rounding: str) -> str | None:
    return None if value is None else format(value.quantize(CENT, rounding=rounding), "f")


def _iv(low: Decimal, high: Decimal, point: Decimal | None) -> dict[str, Any]:
    return {"low": low, "high": high, "point": point}


def _iv_text(iv: Mapping[str, Any]) -> dict[str, Any]:
    return {"low": _txt(iv["low"]), "high": _txt(iv["high"]), "point": _txt(iv["point"])}


def _hull(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    points = {item["point"] for item in items}
    return _iv(min(i["low"] for i in items), max(i["high"] for i in items),
               points.pop() if len(points) == 1 else None)


def _currency(unit: str) -> str | None:
    head, _, tail = str(unit).partition("/")
    return head.upper() if tail.lower() == "shares" and len(head) == 3 and head.isalpha() else None


# --------------------------------------------------------------------------- ownership statements


def _regime(body: Mapping[str, Any]) -> str:
    provider = body["source"]["provider"]
    kind = body["assertion_kind"]
    if provider == "bafin":
        return "bafin-wphg-s33" if str(body.get("record_key", "")).endswith(":s33") else "bafin-wphg-instruments"
    if provider == "sec-edgar":
        form = str((body.get("filing") or {}).get("form_type") or "").upper()
        if kind == "reported_holding":
            return "sec-13f"
        if "13D" in form:
            return "sec-13d"
        if "13G" in form:
            return "sec-13g"
    if provider == "companies-house":
        return "uk-psc"
    if provider == "open-ownership":
        return "bods"
    return "other"


def _reference_date(body: Mapping[str, Any]) -> date | None:
    filing = body.get("filing") or {}
    if body["assertion_kind"] == "reported_holding":
        return _day(filing.get("period_of_report") or body.get("statement_date"))
    if filing:
        return _day(filing.get("event_date") or body.get("statement_date") or filing.get("filing_date"))
    validity = body.get("validity") or {}
    if validity.get("from_status") == "stated" and validity.get("from"):
        return _day(validity["from"])  # e.g. the WpHG event date: the day the stated percentage was reached
    return _day(body.get("statement_date"))


def _published(body: Mapping[str, Any]) -> date | None:
    filing = body.get("filing") or {}
    return _day(filing.get("filing_date")) if filing else _day(body.get("statement_date"))


def _manager_period(body: Mapping[str, Any]) -> tuple[str, str]:
    filing = body.get("filing") or {}
    chain = str((filing.get("amendment") or {}).get("chain_key") or "")
    return chain.split(":period:")[0], str(filing.get("period_of_report") or "")


def _pct(share: Mapping[str, Any]) -> dict[str, Any]:
    if "exact" in share:
        value = _dec(share["exact"])
        return _iv(value, value, value)
    band = share["band"]
    return _iv(ZERO if band.get("min") is None else _dec(band["min"]),
               HUNDRED if band.get("max") is None else _dec(band["max"]), None)


def _stated_quantity(bodies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What a group of statements states for one link: a percentage interval and/or exact shares (or a reason)."""
    body = bodies[0]
    regime = _regime(body)
    kind = body["assertion_kind"]
    if kind not in HOLDING_KINDS:
        return {"reason": "no-share-stated", "regime": regime}
    if kind == "reported_holding":
        total = ZERO
        for row in bodies:
            position = row["position"]
            if position.get("amount_type") != "SH":
                return {"reason": "principal-amount-not-shares", "regime": regime}
            if position.get("put_call"):
                return {"reason": "option-position-not-shares", "regime": regime}
            total += _dec(position["amount"])
        return {"regime": regime, "pct": None, "shares": _iv(total, total, total)}
    if regime == "bafin-wphg-instruments":
        return {"reason": "instrument-basis-not-dividend-bearing", "regime": regime}
    if kind == "voting_rights" and not regime.startswith("bafin"):
        return {"reason": "voting-rights-not-share-capital", "regime": regime}
    share = body.get("share")
    if not share:
        return {"reason": "share-not-stated", "regime": regime}
    pct = _pct(share)
    threshold = REGIMES[regime]["publication_threshold_pct"]
    if threshold is not None and pct["high"] < _dec(threshold):
        return {"reason": "below-publication-threshold", "regime": regime, "threshold_pct": threshold}
    shares = None
    if share.get("shares"):
        count = _dec(share["shares"])
        shares = _iv(count, count, count)
    return {"regime": regime, "pct": pct, "shares": shares}


def _scale(shares: Mapping[str, Any] | None, pct: Mapping[str, Any], low: Decimal, high: Decimal):
    if shares is None or pct["point"] in (None, ZERO):
        return None
    return _iv(shares["point"] * low / pct["point"], shares["point"] * high / pct["point"], shares["point"])


def _statement_value(bodies: Sequence[Mapping[str, Any]], next_bodies: Sequence[Mapping[str, Any]] | None,
                     dividend: Mapping[str, Any], as_of: date, params: Mapping[str, Any]) -> dict[str, Any]:
    """One statement group read at the dividend date, widened by its regime's timing rule.

    Pure: the same inputs (record bodies, dividend dates, as-of date and method
    parameters) give the same result, which is what :func:`reproduce` relies on.
    """
    stated = _stated_quantity(bodies)
    if "reason" in stated:
        return stated
    regime = stated["regime"]
    rule = REGIMES[regime]
    body = bodies[0]
    holding_at, period_start = _day(dividend["holding_date"]), _day(dividend.get("period_start"))
    reference = _reference_date(body)
    validity = body.get("validity") or {}
    statement_date = _day(body.get("statement_date"))
    covered = (validity.get("to_status") == "open" and validity.get("from_status") == "stated"
               and (statement_date is None or statement_date >= holding_at) and period_start is None)
    if reference is None and not covered:
        return {"reason": "holding-date-unknown", "regime": regime}
    if covered:
        mismatch = 0
    else:
        gaps = [abs((holding_at - reference).days)]
        if period_start is not None:
            gaps.append(abs((period_start - reference).days))
        mismatch = max(gaps)
    pct, shares = stated["pct"], stated["shares"]
    flags: list[str] = []
    treatment, bounded = "none-needed", "none-needed"
    if mismatch:
        timing_rule = rule["timing_rule"]
        if timing_rule == "stated-band" and pct is not None and pct["point"] is not None:
            timing_rule = "not-bounded"  # an exact stated share carries no band to hold it
        if timing_rule == "threshold-bracket":
            low = max((t for t in WPHG_S33_THRESHOLDS if t <= pct["low"]), default=ZERO)
            high = min((t for t in WPHG_S33_THRESHOLDS if t > pct["high"]), default=HUNDRED)
            shares = _scale(shares, pct, low, high)
            pct = _iv(low, high, pct["point"])
            treatment, bounded = "threshold-bracket", "bounded"
        elif timing_rule == "stated-band":
            treatment, bounded = "within-stated-band", "bounded"
        elif timing_rule == "material-change-1pp" and pct["point"] is not None:
            low, high = max(ZERO, pct["point"] - 1), min(HUNDRED, pct["point"] + 1)
            shares = _scale(shares, pct, low, high)
            pct = _iv(low, high, pct["point"])
            treatment, bounded = "material-change-1pp", "bounded"
        elif timing_rule == "next-report-hull" and next_bodies:
            following = _stated_quantity(next_bodies)
            if "reason" in following:
                return {**following, "reason": f"next-report-{following['reason']}"}
            shares = _iv(min(shares["low"], following["shares"]["low"]),
                         max(shares["high"], following["shares"]["high"]), shares["point"])
            treatment, bounded = "bracketed-by-next-report", "bounded"
        else:
            if mismatch > int(params["max_unbounded_gap_days"]):
                return {"reason": "stale-holding", "regime": regime, "mismatch_days": mismatch}
            treatment, bounded = "not-bounded", "not-bounded"
            flags.append("timing-not-bounded")
        lag = rule.get("notification_lag_days")
        if bounded == "bounded" and lag is not None and as_of < holding_at + timedelta(days=lag):
            flags.append("notification-window-open")
    threshold = rule["publication_threshold_pct"]
    censored = bool(threshold is not None and pct is not None and pct["low"] == _dec(threshold))
    return {"regime": regime, "pct": pct, "shares": shares, "flags": flags, "band": bool(
        pct is not None and pct["point"] is None), "timing": {
        "reference_date": _iso(reference), "holding_date": dividend["holding_date"],
        "period_start": dividend.get("period_start"), "mismatch_days": mismatch,
        "covered_by_stated_validity": covered, "rule": rule["timing_rule"], "treatment": treatment,
        "interval_bounds_timing": bounded},
        "censoring": {"publication_threshold_pct": threshold, "lower_bound_at_threshold": censored}}


def _link_value(values: Sequence[Mapping[str, Any]], *, direct: bool, shares_outstanding: Decimal | None):
    """Hull of the usable statements of one link, in shares (direct link) or as a fraction (upper link)."""
    usable, unusable = [], []
    for index, value in enumerate(values):
        if "reason" in value:
            unusable.append((index, value["reason"]))
        elif direct and value["shares"] is not None:
            usable.append(value["shares"])
        elif value["pct"] is not None and (not direct or shares_outstanding is not None):
            pct = value["pct"]
            base = shares_outstanding if direct else Decimal(1)
            point = None if pct["point"] is None else pct["point"] / HUNDRED * base
            usable.append(_iv(pct["low"] / HUNDRED * base, pct["high"] / HUNDRED * base, point))
        else:
            unusable.append((index, "shares-outstanding-unknown" if direct else "share-not-stated"))
    return (_hull(usable) if usable else None), unusable


def _amount(per_share: Decimal, direct: Mapping[str, Any], fractions: Sequence[Mapping[str, Any]]):
    low, high = per_share * direct["low"], per_share * direct["high"]
    point = None if direct["point"] is None else per_share * direct["point"]
    for fraction in fractions:
        low, high = low * fraction["low"], high * fraction["high"]
        point = None if point is None or fraction["point"] is None else point * fraction["point"]
    return {"low": _money(low, ROUND_FLOOR), "high": _money(high, ROUND_CEILING),
            "point": _money(point, ROUND_HALF_EVEN)}


# --------------------------------------------------------------------------- market inputs


class _Market:
    """Read-only access to the Market dividend, share-count and shares-outstanding inputs under explicit cutoffs."""

    def __init__(self, conn: Any, namespace: str, cut: Mapping[str, Any], principal_id: str, scopes: set[str]) -> None:
        from src.domains.market.actions import MarketCorporateActionStore
        from src.domains.market.financial_facts import MarketFinancialFactStore
        from src.domains.market.instruments import MarketInstrumentStore

        self.conn, self.namespace, self.cut = conn, namespace, cut
        self.principal_id, self.scopes = principal_id, scopes
        self.actions = MarketCorporateActionStore(conn, initialize=False)
        self.facts = MarketFinancialFactStore(conn, initialize=False)
        self.instruments = MarketInstrumentStore(conn, initialize=False)
        # Missing Market read access is refused, never mistaken for an issuer without dividends.
        for store in (MarketCorporateActionStore, MarketFinancialFactStore, MarketInstrumentStore):
            try:
                store._authorize(namespace, principal_id, scopes, write=False)
            except ValueError as exc:
                raise DividendFlowError(getattr(exc, "code", "unauthorized"), str(exc)) from exc
        self.degraded: list[dict[str, Any]] = []
        self._tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        for table, what in (("market_corporate_action_revisions", "market corporate actions"),
                            ("market_financial_fact_revisions", "market financial facts"),
                            ("market_instrument_object_revisions", "market instruments")):
            if table not in self._tables:
                self.degraded.append({"input": what, "code": "store_unavailable",
                                      "note": f"{table} is not present; inputs from it are missing, not zero"})

    def _window(self) -> dict[str, int]:
        return {"acquired_by_ms": self.cut["acquired_by_ms"],
                "publicly_available_by_ms": self.cut["publicly_available_by_ms"]}

    def derivable(self, refs: Sequence[Mapping[str, Any]]) -> str | None:
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            authorize_market_sources(self.conn, self.namespace, [dict(r) for r in refs], operation="derive",
                                     principal_id=self.principal_id, scopes=self.scopes)
        except MarketEntitlementError as exc:
            return exc.code
        return None

    def issuer_ids(self) -> list[str]:
        ids: set[str] = set()
        if "market_corporate_action_revisions" in self._tables:
            ids |= {r[0] for r in self.conn.execute(
                "SELECT DISTINCT json_extract_string(payload_json, '$.issuer_id') FROM market_corporate_action_revisions "
                "WHERE namespace=?", [self.namespace]).fetchall() if r[0]}
        if "market_financial_fact_revisions" in self._tables:
            ids |= {r[0] for r in self.conn.execute(
                "SELECT DISTINCT issuer_id FROM market_financial_fact_revisions WHERE namespace=?",
                [self.namespace]).fetchall()}
        return sorted(ids)

    def issuer(self, issuer_id: str) -> dict[str, Any] | None:
        if "market_instrument_object_revisions" not in self._tables:
            return None
        try:
            return self.instruments.get_instrument(self.namespace, "issuer", issuer_id, principal_id=self.principal_id,
                                                   scopes=self.scopes, **self._window())
        except Exception:  # noqa: BLE001 - an issuer outside the caller's view is simply not linked
            return None

    def security_ids(self, issuer_id: str) -> list[str]:
        if "market_corporate_action_revisions" not in self._tables:
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT security_id FROM market_corporate_action_revisions WHERE namespace=? "
            "AND json_extract_string(payload_json, '$.issuer_id')=? ORDER BY security_id",
            [self.namespace, issuer_id]).fetchall()]

    def actions_for(self, security_id: str) -> list[dict[str, Any]]:
        if "market_corporate_action_revisions" not in self._tables:
            return []
        return self.actions.get_actions(self.namespace, security_id, principal_id=self.principal_id,
                                        scopes=self.scopes, **self._window())

    def facts_for(self, issuer_id: str, taxonomy: str, concept: str) -> list[dict[str, Any]]:
        if "market_financial_fact_revisions" not in self._tables:
            return []
        from src.domains.market.financial_facts import MarketFinancialFactError

        try:
            return self.facts.latest_facts_by_period(self.namespace, issuer_id, principal_id=self.principal_id,
                                                     scopes=self.scopes, taxonomy=taxonomy, concept=concept,
                                                     **self._window())["facts"]
        except MarketFinancialFactError as exc:
            if exc.code == "unauthorized":
                raise
            self.degraded.append({"input": f"{taxonomy}:{concept}", "issuer_id": issuer_id, "code": exc.code})
            return []


def _action_cite(action: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {"role": role, "store": "market-corporate-action", "record_id": action["action_id"],
            "revision": action["revision"], "revision_id": action["revision_id"], "record_hash": action["record_hash"],
            "provider": action["provider"], "provider_record_id": action.get("provider_record_id"),
            "source_ref_ids": sorted(r["source_ref_id"] for r in action["source_refs"])}


def _fact_cite(fact: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {"role": role, "store": "market-financial-fact", "record_id": fact["fact_observation_id"],
            "revision": fact["revision"], "revision_id": fact["revision_id"], "record_hash": fact["record_hash"],
            "provider": fact["provider"], "filing_accession": fact["filing_accession"],
            "concept": f"{fact['taxonomy']}:{fact['concept']}", "locator": fact["source_locator"],
            "source_ref_ids": sorted(r["source_ref_id"] for r in fact["source_refs"])}


def _ownership_cite(view: Mapping[str, Any], role: str) -> dict[str, Any]:
    body = view["record"]
    filing = body.get("filing") or {}
    return {"role": role, "store": "ownership-record", "record_id": view["record_id"], "revision": view["revision"],
            "revision_id": view["revision_id"], "record_hash": view["record_hash"],
            "provider": body["source"]["provider"], "record_key": body.get("record_key"),
            "statement_id": body["source"].get("statement_id"), "locator": body["source"].get("locator"),
            "accession_number": filing.get("accession_number")}


def _dividend_events(market: _Market, link: Mapping[str, Any], window: tuple[date, date]):
    """Declared cash dividends of one linked issuer in the window, as cited source records (reported, not derived)."""
    events, excluded, share_changes = [], [], {}
    start, end = window
    raw = []
    for security_id in link["security_ids"]:
        actions = market.actions_for(security_id)
        share_changes[security_id] = sorted(
            (a.get("ex_date") or a.get("effective_date"), a) for a in actions
            if a["action_type"] in SHARE_COUNT_ACTIONS and a["status"] in {"confirmed", "corrected"}
            and (a.get("ex_date") or a.get("effective_date")))
        for action in actions:
            if action["action_type"] != "cash_dividend":
                continue
            holding_date = _day(action.get("record_date") or action.get("ex_date"))
            if holding_date is None or not start <= holding_date <= end:
                continue
            reason = None
            if action["status"] not in {"confirmed", "corrected"}:
                reason = f"status-{action['status']}"
            elif action["distribution_type"] not in {"regular", "special"}:
                reason = f"not-an-income-distribution:{action['distribution_type']}"
            else:
                reason = market.derivable(action["source_refs"])
                reason = None if reason is None else f"derive-not-permitted:{reason}"
            if reason:
                excluded.append({"reason": reason, "input": _action_cite(action, "dividend")})
            else:
                raw.append(action)
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for action in raw:
        groups.setdefault((action["security_id"], action["ex_date"], action["distribution_type"]), []).append(action)
    for (security_id, ex_date, distribution), actions in sorted(groups.items()):
        values = {(a["cash_amount"] and _txt(_dec(a["cash_amount"])), a["currency"], a["cash_amount_basis"])
                  for a in actions}
        if len(values) > 1:
            excluded.append({"reason": "conflicting-provider-observations",
                             "inputs": [_action_cite(a, "dividend") for a in actions],
                             "note": "providers disagree on the amount, currency or basis; never combined"})
            continue
        first = actions[0]
        events.append({
            "event_id": "div:" + digest([security_id, ex_date, distribution])[:20], "source": "market-corporate-action",
            "issuer_id": first["issuer_id"], "security_id": security_id, "per_share": _txt(_dec(first["cash_amount"])),
            "currency": first["currency"], "basis": first["cash_amount_basis"], "distribution_type": distribution,
            "ex_date": first["ex_date"], "record_date": first.get("record_date"),
            "payable_date": first.get("payable_date"),
            "holding_date": first.get("record_date") or first["ex_date"], "period_start": None,
            "inputs": [_action_cite(a, "dividend") for a in actions]})
    if not events and not link.get("security_pinned"):
        events.extend(_fact_events(market, link, window, excluded))
    return events, excluded, share_changes


def _fact_events(market: _Market, link: Mapping[str, Any], window: tuple[date, date], excluded: list) -> list:
    """Per-period dividends per share from filed facts, without overlapping periods (no double counting)."""
    start, end = window
    candidates = []
    for taxonomy, concept in DIVIDEND_PER_SHARE_CONCEPTS:
        for fact in market.facts_for(link["issuer_id"], taxonomy, concept):
            period = fact["period"]
            currency = _currency(fact["unit"])
            if period.get("kind") != "duration" or currency is None:
                continue
            period_start, period_end = _day(period["start_date"]), _day(period["end_date"])
            if not start <= period_end <= end:
                continue
            reason = market.derivable(fact["source_refs"])
            if reason:
                excluded.append({"reason": f"derive-not-permitted:{reason}", "input": _fact_cite(fact, "dividend")})
                continue
            candidates.append((period_end, (period_end - period_start).days, fact, currency, period_start))
    chosen: list[tuple] = []
    for item in sorted(candidates, key=lambda c: (c[0], c[1], c[2]["revision_id"]), reverse=True):
        overlaps = any(item[4] <= other[0] and other[4] <= item[0] for other in chosen)
        if overlaps:
            excluded.append({"reason": "overlapping-period-not-used", "input": _fact_cite(item[2], "dividend"),
                             "note": "a longer or later period already covers it; never double counted"})
        else:
            chosen.append(item)
    events = []
    for period_end, _length, fact, currency, period_start in sorted(chosen, key=lambda c: c[0]):
        events.append({
            "event_id": "div:" + digest([fact["fact_observation_id"], fact["revision_id"]])[:20],
            "source": "market-financial-fact", "issuer_id": link["issuer_id"], "security_id": None,
            "per_share": _txt(_dec(fact["value_lexical"]) * (Decimal(10) ** int(fact.get("scale") or 0))),
            "currency": currency, "basis": "per_share_period_total", "distribution_type": "period-total",
            "ex_date": None, "record_date": None, "payable_date": None, "holding_date": _iso(period_end),
            "period_start": _iso(period_start), "inputs": [_fact_cite(fact, "dividend")]})
    return events


def _shares_outstanding(market: _Market, issuer_id: str, holding_date: date) -> dict[str, Any] | None:
    best = None
    for taxonomy, concept in SHARES_OUTSTANDING_CONCEPTS:
        for fact in market.facts_for(issuer_id, taxonomy, concept):
            if fact["period"].get("kind") != "instant" or str(fact["unit"]).lower() != "shares":
                continue
            if market.derivable(fact["source_refs"]):
                continue
            instant = _day(fact["period"]["instant_date"])
            rank = (abs((holding_date - instant).days), instant > holding_date, fact["revision_id"])
            if best is None or rank < best[0]:
                best = (rank, fact, instant)
    if best is None:
        return None
    _, fact, instant = best
    return {"value": _dec(fact["value_lexical"]) * (Decimal(10) ** int(fact.get("scale") or 0)), "instant": instant,
            "mismatch_days": abs((holding_date - instant).days), "input": _fact_cite(fact, "shares-outstanding")}


def _reported_context(market: _Market, issuer_id: str, window: tuple[date, date]) -> list[dict[str, Any]]:
    rows = []
    for taxonomy, concept in DIVIDEND_PAYMENT_CONCEPTS:
        for fact in market.facts_for(issuer_id, taxonomy, concept):
            end = _day(fact["period"].get("end_date") or fact["period"].get("instant_date"))
            if end and window[0] <= end <= window[1]:
                rows.append({"label": "reported", "concept": f"{taxonomy}:{concept}", "value": fact["value_lexical"],
                             "unit": fact["unit"], "period": fact["period"], "input": _fact_cite(fact, "context"),
                             "note": "company-wide total as filed; context only, not attributed to any holder"})
    return rows


# --------------------------------------------------------------------------- linking ownership entities to issuers


def _normalize_identifier(scheme: str, value: str) -> tuple[str, str] | None:
    scheme = scheme.lower()
    value = str(value).strip().upper()
    if scheme in {"cik", "sec-cik"}:
        return "cik", value.lstrip("0") or "0"
    if scheme == "lei":
        return "lei", value
    return None


def _entity_identifiers(graph: Any, cluster: str) -> set[tuple[str, str]]:
    found = set()
    for key in graph.members(cluster):
        if key.startswith("sec-edgar:cik:"):
            found.add(("cik", key.rsplit(":", 1)[1].lstrip("0") or "0"))
        for view in graph.entities.get(key, []):
            for identifier in view["record"].get("identifiers") or []:
                normalized = _normalize_identifier(identifier.get("scheme", ""), identifier.get("value", ""))
                if normalized:
                    found.add(normalized)
    return found


def _market_links(graph: Any, market: _Market, clusters: Iterable[str],
                  explicit: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ownership entity -> market issuer (and its securities): caller-stated, else an exact shared CIK or LEI."""
    links: dict[str, dict[str, Any]] = {}
    issuers = None
    for cluster in sorted(set(clusters)):
        stated = next((explicit[k] for k in graph.members(cluster) if k in explicit), None)
        if stated:
            issuer = market.issuer(str(stated["issuer_id"]))
            securities = [stated["security_id"]] if stated.get("security_id") else market.security_ids(
                str(stated["issuer_id"]))
            links[cluster] = {"issuer_id": stated["issuer_id"], "security_ids": securities,
                              "security_pinned": bool(stated.get("security_id")), "basis": "caller-stated",
                              "issuer_revision_id": (issuer or {}).get("revision_id")}
            continue
        wanted = _entity_identifiers(graph, cluster)
        if not wanted:
            continue
        if issuers is None:
            issuers = [i for i in (market.issuer(x) for x in market.issuer_ids()) if i]
        matches = []
        for issuer in issuers:
            shared = {_normalize_identifier(i.get("scheme", ""), i.get("value", ""))
                      for i in issuer.get("identifiers") or []} & wanted
            if shared:
                matches.append((issuer, sorted(shared)))
        if len(matches) == 1:
            issuer, shared = matches[0]
            links[cluster] = {"issuer_id": issuer["issuer_id"], "security_ids": market.security_ids(issuer["issuer_id"]),
                              "security_pinned": False, "basis": "exact-identifier",
                              "identifiers": [f"{s}:{v}" for s, v in shared],
                              "issuer_revision_id": issuer["revision_id"]}
        elif len(matches) > 1:
            links[cluster] = {"ambiguous": sorted(i["issuer_id"] for i, _ in matches), "basis": "exact-identifier"}
    return links


# --------------------------------------------------------------------------- the query


class _Edges:
    """Holding statements in force at one date, grouped per (holder, subject) link."""

    def __init__(self, graph: Any, holding_date: date, as_of: date, research: bool) -> None:
        from src.kb.ownership_graph import as_of_status
        from src.kb.ownership_holdings import _Lines

        self.graph = graph
        self.links: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.unknown: list[dict[str, Any]] = []
        self.withheld: list[dict[str, Any]] = []
        iso = holding_date.isoformat()
        views = [v for v, _ in _Lines(graph, iso).in_force]
        for view in graph.assertions:
            body = view["record"]
            if body.get("contract") == CONTRACT_V2 and body.get("filing") and not view.get("redacted"):
                continue  # SEC filing lines: amendment chains and 13F periods are applied by _Lines
            if as_of_status(body.get("validity") or {}, iso) in {"valid", "undetermined"}:
                views.append(view)
        for view in views:
            body = view["record"]
            if body["assertion_kind"] == "reporting_exception":
                continue
            published = _published(body)
            if published is not None and published > as_of:
                continue  # stated after the as-of date
            subject = graph.cluster(body["subject_key"])
            holder = body.get("holder") or {}
            if view.get("redacted") or (holder.get("kind") == "person" and not research):
                self.withheld.append({"subject": subject, "provider": body["source"]["provider"],
                                      "assertion_kind": body["assertion_kind"],
                                      "reason": "natural-person holder withheld (person-research mode is off or the "
                                                "statement is owner-scoped)"})
                continue
            holder_id = graph.cluster(holder["key"]) if holder.get("key") else f"unidentified:{holder.get('name')}"
            if body["assertion_kind"] not in HOLDING_KINDS:
                self.unknown.append({"subject": subject, "holder": holder_id, "assertion_kind": body["assertion_kind"],
                                     "reason": "no-share-stated", "input": _ownership_cite(view, "holding")})
                continue
            self.links.setdefault((holder_id, subject), []).append(view)

    def holders_of(self, subject: str) -> list[str]:
        return sorted({h for h, s in self.links if s == subject})

    def subjects_of(self, holder: str) -> list[str]:
        return sorted({s for h, s in self.links if h == holder})

    def groups(self, holder: str, subject: str) -> list[list[dict[str, Any]]]:
        """Statements of one link; rows of one 13F manager's report period form one summed group."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for view in self.links.get((holder, subject), []):
            body = view["record"]
            key = (":".join(("13f",) + _manager_period(body)) if body["assertion_kind"] == "reported_holding"
                   else view["record_id"])
            groups.setdefault(key, []).append(view)
        return [sorted(groups[k], key=lambda v: v["record_id"]) for k in sorted(groups)]


def _next_report(graph: Any, group: Sequence[Mapping[str, Any]], holding_date: date, as_of: date):
    """The 13F rows of the same manager and issuer in the first report period after the holding date, if on record."""
    from src.kb.ownership_holdings import _Lines

    body = group[0]["record"]
    manager, _ = _manager_period(body)
    cusip = body["position"].get("cusip")
    periods = sorted({_day(v["record"]["filing"].get("period_of_report")) for v in graph.assertions
                      if v["record"].get("assertion_kind") == "reported_holding" and not v.get("redacted")
                      and _manager_period(v["record"])[0] == manager
                      and (_day(v["record"]["filing"].get("filing_date")) or date.max) <= as_of
                      and holding_date < (_day(v["record"]["filing"].get("period_of_report")) or date.min) <= as_of})
    if not periods:
        return []
    rows = [v for v, _ in _Lines(graph, periods[0].isoformat()).in_force
            if v["record"].get("assertion_kind") == "reported_holding"
            and _manager_period(v["record"]) == (manager, periods[0].isoformat())
            and v["record"]["position"].get("cusip") == cusip and not v["record"]["position"].get("put_call")
            and v["record"]["subject_key"] == body["subject_key"]]
    return sorted(rows, key=lambda v: v["record_id"])


def _walk_up(edges: _Edges, start: str, max_depth: int) -> dict[str, Any]:
    """Issuer mode: every stated holder up to ``max_depth`` links above the issuer; cycles and truncation reported."""
    found: dict[str, Any] = {"paths": [], "cycles": [], "truncated": False}

    def up(node: str, chain: list[str], hops: list[tuple[str, str]]) -> None:
        for holder_id in edges.holders_of(node):
            if holder_id in chain:
                found["cycles"].append(chain + [holder_id])
                continue
            found["paths"].append(hops + [(node, holder_id)])
            if holder_id.startswith("unidentified:"):
                continue  # an unkeyed holder cannot be followed further: its own holders are unknown
            if len(hops) + 1 >= max_depth:
                found["truncated"] = found["truncated"] or bool(edges.holders_of(holder_id))
                continue
            up(holder_id, chain + [holder_id], hops + [(node, holder_id)])

    up(start, [start], [])
    return found


def _walk_down(edges: _Edges, start: str, target: str, max_depth: int) -> dict[str, Any]:
    """Holder mode: every path of stated holdings from the holder down to the paying issuer, within ``max_depth``."""
    found: dict[str, Any] = {"paths": [], "cycles": [], "truncated": False}

    def down(node: str, chain: list[str], hops: list[tuple[str, str]]) -> None:
        if len(hops) >= max_depth:
            found["truncated"] = found["truncated"] or bool(edges.subjects_of(node))
            return
        for subject in edges.subjects_of(node):
            if subject in chain:
                found["cycles"].append(chain + [subject])
                continue
            step = hops + [(subject, node)]
            if subject == target:
                found["paths"].append(list(reversed(step)))
            else:
                down(subject, chain + [subject], step)

    down(start, [start], [])
    return found


def _share_count_change(changes: Sequence[tuple], reference: str | None, holding_date: str) -> list[str]:
    if not reference:
        return []
    return [a["revision_id"] for when, a in changes if reference < when <= holding_date]


def _validate_params(as_of: Any, max_depth: Any, window_days: Any, max_gap: Any) -> date:
    from src.kb.ownership_graph import MAX_DEPTH

    day = _day(as_of)
    if day is None or not isinstance(as_of, str) or len(as_of) != 10:
        raise DividendFlowError("invalid_request", "as_of is a date (YYYY-MM-DD)")
    if type(max_depth) is not int or not 1 <= max_depth <= MAX_DEPTH:
        raise DividendFlowError("invalid_request", f"max_depth must be between 1 and {MAX_DEPTH}")
    if type(window_days) is not int or not 1 <= window_days <= MAX_WINDOW_DAYS:
        raise DividendFlowError("invalid_request", f"window_days must be between 1 and {MAX_WINDOW_DAYS}")
    if type(max_gap) is not int or not 0 <= max_gap <= 3660:
        raise DividendFlowError("invalid_request", "max_unbounded_gap_days must be between 0 and 3660")
    return day


def estimate_dividend_flows(conn: Any, namespace: str, *, market_namespace: str, as_of: str, principal_id: str,
                            scopes: Iterable[str], holder: str | None = None, issuer: str | None = None,
                            max_depth: int = DEFAULT_MAX_DEPTH, window_days: int = DEFAULT_WINDOW_DAYS,
                            acquired_by_ms: int | None = None, known_at_ms: int | None = None,
                            links: Mapping[str, Mapping[str, Any]] | None = None,
                            max_unbounded_gap_days: int = MAX_UNBOUNDED_GAP_DAYS) -> dict[str, Any]:
    """Derived dividend flows to a holder (down its stated holdings) or from an issuer (up its stated holders).

    ``namespace`` is the Corporate Ownership namespace, ``market_namespace``
    the Market namespace holding the issuer's corporate actions and facts.
    Dividends whose record date (ex-date, period end) falls in the
    ``window_days`` up to ``as_of`` are used, published by the end of
    ``as_of`` (UTC) and acquired by ``acquired_by_ms`` (default: now). Links
    from an ownership entity to a market issuer are ``links``
    (``{record_key: {"issuer_id", "security_id"?}}``) or an exact shared CIK
    or LEI; anything else stays unlinked and is listed.
    """
    from src.config.env import person_research_enabled
    from src.kb.ownership_graph import OwnershipGraph

    if (holder is None) == (issuer is None):
        raise DividendFlowError("invalid_request", "name exactly one of holder or issuer")
    as_of_day = _validate_params(as_of, max_depth, window_days, max_unbounded_gap_days)
    scopes = set(scopes)
    graph = OwnershipGraph(conn, namespace, principal_id=principal_id, scopes=scopes, known_at_ms=known_at_ms)
    start = graph.resolve(holder or issuer)
    public_ms = _end_of_day_ms(as_of_day)
    from src.domains.market.actions import MarketCorporateActionStore

    acquired = int(acquired_by_ms) if acquired_by_ms is not None else max(public_ms, MarketCorporateActionStore(
        conn, initialize=False).now())
    if acquired < public_ms:
        raise DividendFlowError("invalid_request", "acquired_by_ms cannot be earlier than the end of the as-of day")
    cut = {"as_of": as_of, "publicly_available_by_ms": public_ms, "acquired_by_ms": acquired,
           "availability_policy": "public_and_acquired"}
    window = (as_of_day - timedelta(days=window_days), as_of_day)
    params = {"max_depth": max_depth, "window_days": window_days, "max_unbounded_gap_days": max_unbounded_gap_days,
              "window": {"from": window[0].isoformat(), "to": window[1].isoformat()}}
    research = person_research_enabled()
    market = _Market(conn, market_namespace, cut, principal_id, scopes)

    # Candidate paying issuers: the start (issuer mode) or every subject reachable down stated holdings in the window.
    if issuer is not None:
        candidates = [start]
    else:
        # A superset regardless of dates: which links are in force is decided per dividend date below.
        adjacency: dict[str, set[str]] = {}
        for view in graph.assertions:
            body = view["record"]
            key = (body.get("holder") or {}).get("key")
            if body["assertion_kind"] in HOLDING_KINDS and key and not view.get("redacted"):
                adjacency.setdefault(graph.cluster(key), set()).add(graph.cluster(body["subject_key"]))
        reach, frontier = set(), [start]
        for _ in range(max_depth):
            frontier = sorted({s for node in frontier for s in adjacency.get(node, ())} - reach - {start})
            reach |= set(frontier)
        candidates = sorted(reach)
    market_links = _market_links(graph, market, candidates, dict(links or {}))
    events, excluded, unlinked, context, share_changes = [], [], [], [], {}
    for cluster in candidates:
        link = market_links.get(cluster)
        if link is None or "ambiguous" in link:
            unlinked.append({"entity": cluster, "reason": "no-market-issuer-link" if link is None
                             else "ambiguous-market-issuer-link", **({"issuers": link["ambiguous"]} if link else {})})
            continue
        if len(link["security_ids"]) > 1 and not link["security_pinned"]:
            unlinked.append({"entity": cluster, "reason": "share-class-ambiguous", "securities": link["security_ids"],
                             "note": "name the security in links to estimate one class"})
            continue
        found, dropped, changes = _dividend_events(market, link, window)
        share_changes.update(changes)
        events.extend({**e, "issuer_entity": cluster} for e in found)
        excluded.extend({**d, "issuer_entity": cluster} for d in dropped)
        context.extend({**c, "issuer_entity": cluster} for c in _reported_context(market, link["issuer_id"], window))
    if len(events) > MAX_EVENTS:
        raise DividendFlowError("result_too_large", f"more than {MAX_EVENTS} dividend events; narrow the window")

    estimates, not_estimated, cycles, unknown_links, withheld = [], [], [], [], []
    truncated = False
    edge_cache: dict[date, _Edges] = {}
    for event in sorted(events, key=lambda e: (e["holding_date"], e["event_id"])):
        holding_day = _day(event["holding_date"])
        edges = edge_cache.setdefault(holding_day, _Edges(graph, holding_day, as_of_day, research))
        walk = (_walk_up(edges, start, max_depth) if issuer is not None
                else _walk_down(edges, start, event["issuer_entity"], max_depth))
        paths = walk["paths"]  # each path: links from the issuer upward as (subject, holder)
        cycles.extend({"event_id": event["event_id"], "path": cycle} for cycle in walk["cycles"])
        truncated = truncated or walk["truncated"]
        for path in paths:
            if len(estimates) >= MAX_ESTIMATES:
                raise DividendFlowError("result_too_large", f"more than {MAX_ESTIMATES} estimates; lower max_depth")
            outcome = _estimate(graph, market, edges, event, path, as_of_day, params, share_changes)
            (estimates if "estimate" in outcome else not_estimated).append(outcome)
        visited = {start, event["issuer_entity"]} | {node for path in paths for hop in path for node in hop}
        unknown_links.extend({**u, "holding_date": event["holding_date"]} for u in edges.unknown
                             if u["subject"] in visited or u["holder"] in visited)
        withheld.extend({**w, "holding_date": event["holding_date"]} for w in edges.withheld
                        if w["subject"] in visited)

    by_currency: dict[str, list[str]] = {}
    for item in estimates:
        by_currency.setdefault(item["estimate"]["currency"], []).append(item["estimate_id"])
    result = {
        "contract": CONTRACT, "query": QUERY, "mode": "issuer" if issuer is not None else "holder",
        "label": LABEL, "reported": False, "namespace": namespace, "market_namespace": market_namespace,
        "entity": graph.describe(start), "cutoffs": cut, "method": METHOD, "method_id": METHOD_ID,
        "method_version": METHOD_VERSION, "method_parameters": params,
        "n": len({c["revision_id"] for e in estimates for c in e["inputs"]}),
        "assumptions": sorted({a for e in estimates for a in e["assumptions"]}),
        "estimates": sorted(estimates, key=lambda e: (e["dividend"]["holding_date"], e["depth"], e["estimate_id"])),
        "not_estimated": sorted(not_estimated, key=lambda e: (e["dividend"]["holding_date"], e["reason"],
                                                              json.dumps(e["path"]))),
        "unknown_links": _unique(unknown_links), "withheld": _unique(withheld), "unlinked_entities": unlinked,
        "cycles": _unique(cycles), "truncated": truncated,
        "dividends": [{k: v for k, v in e.items()} | {"label": "reported"} for e in events],
        "excluded_dividends": excluded, "reported_context": context,
        "market_links": {k: v for k, v in sorted(market_links.items())},
        "by_currency": {c: sorted(ids) for c, ids in sorted(by_currency.items())},
        "assumption_texts": ASSUMPTIONS, "regimes": REGIMES, "sources": DIVIDEND_SOURCES,
        "degraded": market.degraded, "status": "partial" if market.degraded else "complete",
        "pins": {"records": dict(sorted(graph.pins.items())), "identity": graph.identity},
        "notice": NOTICE,
    }
    result["result_hash"] = digest({k: v for k, v in result.items() if k != "pins"})
    guard(result)
    _validate(result)
    return result


def _unique(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return sorted(out, key=lambda i: json.dumps(i, sort_keys=True, default=str))


def _estimate(graph: Any, market: _Market, edges: _Edges, event: Mapping[str, Any], path: Sequence[tuple[str, str]],
              as_of: date, params: Mapping[str, Any], share_changes: Mapping[str, Sequence]) -> dict[str, Any]:
    """One path (issuer upward) for one dividend: the derived amount, or why it is not estimated."""
    holding_day = _day(event["holding_date"])
    entities = [path[0][0]] + [holder for _, holder in path]
    base = {"path": entities, "depth": len(path), "issuer_entity": path[0][0], "recipient": entities[-1],
            "dividend": {k: event[k] for k in ("event_id", "source", "issuer_id", "security_id", "per_share", "currency",
                                               "ex_date", "record_date", "payable_date", "holding_date",
                                               "period_start")}}
    inputs: list[dict[str, Any]] = list(event["inputs"])
    links_out, fractions, flags = [], [], set()
    direct_value = None
    bounds_timing, band, censored = True, False, False
    shares_outstanding = None
    for index, (subject, holder) in enumerate(path):
        direct = index == 0
        values, cites_per_statement = [], []
        if direct:
            shares_outstanding = _shares_outstanding(market, event["issuer_id"], holding_day)
        for group in edges.groups(holder, subject):
            body = group[0]["record"]
            following = (_next_report(graph, group, holding_day, as_of)
                         if body["assertion_kind"] == "reported_holding" else [])
            value = _statement_value([v["record"] for v in group], [v["record"] for v in following], event, as_of,
                                     params)
            reference = (value.get("timing") or {}).get("reference_date")
            security = event.get("security_id")
            if direct and "reason" not in value and security and _share_count_change(
                    share_changes.get(security, ()), reference, event["holding_date"]):
                value = {"reason": "share-count-changed", "regime": value["regime"]}
            values.append(value)
            cites_per_statement.append([_ownership_cite(v, "holding") for v in group]
                                       + [_ownership_cite(v, "next-report") for v in following])
        so_value = shares_outstanding["value"] if direct and shares_outstanding else None
        combined, unusable = _link_value(values, direct=direct, shares_outstanding=so_value)
        dropped = dict(unusable)
        statements, not_used = [], []
        for index, (value, cites) in enumerate(zip(values, cites_per_statement, strict=True)):
            if index in dropped:
                # Not an input of the estimate: kept beside the link with its citation and the reason.
                not_used.append({"reason": dropped[index], "regime": value.get("regime"), "cited": cites})
                continue
            inputs.extend(cites)
            statements.append({"inputs": [c["revision_id"] for c in cites], "regime": value["regime"],
                               "pct": value["pct"] and _iv_text(value["pct"]),
                               "shares": value["shares"] and _iv_text(value["shares"]),
                               "timing": value["timing"], "censoring": value["censoring"], "flags": value["flags"]})
            if direct and value["shares"] is None:
                inputs.append(shares_outstanding["input"])
        link = {"subject": subject, "holder": holder, "measure": "shares" if direct else "fraction",
                "statements": statements, "combined": "single" if len(statements) == 1 else
                f"hull of {len(statements)} statements", "statements_not_used": not_used}
        if combined is None:
            reasons = sorted({item["reason"] for item in not_used})
            return {**base, "reason": reasons[0] if len(reasons) == 1 else "no-usable-statement",
                    "link": link, "label": LABEL, "reported": False}
        link["value"] = _iv_text(combined)
        for index, value in enumerate(values):
            if index in dropped:
                continue
            flags |= set(value["flags"])
            band = band or value["band"]
            censored = censored or value["censoring"]["lower_bound_at_threshold"]
            if value["timing"]["interval_bounds_timing"] == "not-bounded":
                bounds_timing = False
        links_out.append(link)
        if direct:
            direct_value = combined
        else:
            fractions.append(combined)
    per_share = _dec(event["per_share"])
    amount = _amount(per_share, direct_value, fractions)
    used = _dedupe(inputs)
    if shares_outstanding and any(c["role"] == "shares-outstanding" for c in used):
        flags.add(f"shares-outstanding-gap-days:{shares_outstanding['mismatch_days']}")
    assumptions = ["class-share", "gross", "holding-date", "statement-hull", "no-share-count-change"]
    if len(path) > 1:
        assumptions.append("look-through")
    if any(c["role"] == "shares-outstanding" for c in used):
        assumptions.append("shares-outstanding")
    estimate_id = "dfe:" + digest([event["event_id"], entities, [c["revision_id"] for c in used]])[:24]
    return {**base, "estimate_id": estimate_id, "label": LABEL, "reported": False,
            "statement": "derived estimate of the dividend attributable along a stated ownership path; not a "
                         "reported figure",
            "kind": "direct-holding" if len(path) == 1 else "look-through",
            "estimate": {"currency": event["currency"], "point": amount["point"],
                         "interval": {"low": amount["low"], "high": amount["high"]},
                         "interval_covers": {"interval_kind": "bounds", "holding_bands": band,
                                             "timing": "bounded" if bounds_timing
                                             else "not-bounded", "threshold_censoring": censored,
                                             "rounding": "low floored, high ceiled to 0.01; point half-even"}},
            "n": len(used), "method": METHOD, "method_id": METHOD_ID, "method_version": METHOD_VERSION,
            "links": links_out, "inputs": used,
            "assumptions": sorted(assumptions), "flags": sorted(flags)}


def _dedupe(inputs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for item in inputs:
        key = (item["store"], item["revision_id"], item["role"])
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return out


# --------------------------------------------------------------------------- guard, contract, reproduce, export


def guard(result: Mapping[str, Any]) -> None:
    """Refuse an answer that would present an estimate as a reported figure."""
    if result.get("label") != LABEL or result.get("reported") is not False:
        raise DividendFlowError("not_derived", "a dividend-flow answer must be labelled derived and not reported")
    for item in result.get("estimates") or []:
        if item.get("label") != LABEL or item.get("reported") is not False or item.get("method") != METHOD or \
                item.get("method_id") != METHOD_ID or not item.get("inputs") or not item.get("assumptions"):
            raise DividendFlowError("not_derived", "every estimate carries label derived, reported false, its method, "
                                                   "cited inputs and assumptions", estimate_id=item.get("estimate_id"))
        if {"amount", "value", "reported_amount"} & set(item.get("estimate") or {}):
            raise DividendFlowError("not_derived", "an estimate never carries a reported-figure field",
                                    estimate_id=item.get("estimate_id"))


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft7Validator

    path = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _validate(result: Mapping[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(json.loads(json.dumps(result, default=str))),
                    key=lambda e: [str(p) for p in e.path])
    if errors:
        location = "/".join(str(p) for p in errors[0].path) or "$"
        raise DividendFlowError("contract_invalid", f"{CONTRACT} rejected {location}: {errors[0].message}")


def _read_input(conn: Any, namespace: str, market_namespace: str, cite: Mapping[str, Any], principal_id: str,
                scopes: set[str]) -> dict[str, Any]:
    """The exact cited revision, checked against its cited record hash."""
    if cite["store"] == "ownership-record":
        from src.kb.ownership_store import OwnershipStore

        view = OwnershipStore(conn, initialize=False).get(namespace, cite["record_id"], principal_id=principal_id,
                                                          scopes=scopes, revision=int(cite["revision"]))
        found, body = view["record_hash"], view["record"]
    else:
        table = ("market_corporate_action_revisions" if cite["store"] == "market-corporate-action"
                 else "market_financial_fact_revisions")
        row = conn.execute(f"SELECT payload_json, record_hash FROM {table} WHERE namespace=? AND revision_id=?",
                           [market_namespace, cite["revision_id"]]).fetchone()
        if row is None:
            raise DividendFlowError("input_missing", "a cited market revision is not on record",
                                    revision_id=cite["revision_id"])
        body, found = json.loads(row[0]), row[1]
    if found != cite["record_hash"]:
        raise DividendFlowError("input_changed", "a cited input revision no longer matches its record hash",
                                revision_id=cite["revision_id"])
    return body


def reproduce(conn: Any, result: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
    """Recompute every estimate from exactly the input revisions it cites (hash-checked) with the recorded method."""
    guard(result)
    scopes = set(scopes)
    params = result["method_parameters"]
    as_of = _day(result["cutoffs"]["as_of"])
    checks = []
    for item in result["estimates"]:
        cites = {c["revision_id"]: c for c in item["inputs"]}
        bodies = {rid: _read_input(conn, result["namespace"], result["market_namespace"], c, principal_id, scopes)
                  for rid, c in cites.items()}
        dividend = next(bodies[c["revision_id"]] for c in item["inputs"] if c["role"] == "dividend")
        if "cash_amount" in dividend:  # a corporate action: the record date, else the ex-date, is the holding date
            per_share = _dec(dividend["cash_amount"])
            dates = {"holding_date": dividend.get("record_date") or dividend["ex_date"], "period_start": None}
        else:  # a filed per-period fact: the period end is the holding date, the period the timing gap
            per_share = _dec(dividend["value_lexical"]) * (Decimal(10) ** int(dividend.get("scale") or 0))
            dates = {"holding_date": dividend["period"]["end_date"], "period_start": dividend["period"]["start_date"]}
        so = next((bodies[c["revision_id"]] for c in item["inputs"] if c["role"] == "shares-outstanding"), None)
        so_value = None if so is None else _dec(so["value_lexical"]) * (Decimal(10) ** int(so.get("scale") or 0))
        direct, fractions = None, []
        for index, link in enumerate(item["links"]):
            values = []
            for statement in link["statements"]:
                group = [bodies[r] for r in statement["inputs"] if cites[r]["role"] == "holding"]
                following = [bodies[r] for r in statement["inputs"] if cites[r]["role"] == "next-report"]
                values.append(_statement_value(group, following, dates, as_of, params))
            combined, _ = _link_value(values, direct=index == 0, shares_outstanding=so_value)
            if index == 0:
                direct = combined
            else:
                fractions.append(combined)
        amount = _amount(per_share, direct, fractions)
        recomputed = {"point": amount["point"], "interval": {"low": amount["low"], "high": amount["high"]}}
        stated = {"point": item["estimate"]["point"], "interval": item["estimate"]["interval"]}
        checks.append({"estimate_id": item["estimate_id"], "matches": recomputed == stated,
                       "recomputed": recomputed, "inputs_checked": len(bodies)})
    return {"result_hash": result["result_hash"], "method": METHOD, "ok": all(c["matches"] for c in checks),
            "estimates": checks}


def export_bundle(conn: Any, result: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """The answer as a ``noesis-evidence-bundle-v1``: the answer as root, one evidence object per cited revision."""
    from src.evidence_bundle import EvidenceBundleBuilder

    if result.get("contract") != CONTRACT:
        raise DividendFlowError("invalid_request", "only a dividend-flow estimate answer can be exported")
    guard(result)
    builder = EvidenceBundleBuilder(
        "receipt", {"domain": "market", "kind": "dividend-flow-estimates", "namespace": result["namespace"],
                    "market_namespace": result["market_namespace"], "entity": result["entity"]["entity"],
                    "mode": result["mode"], "as_of": result["cutoffs"]["as_of"], "method": METHOD,
                    "result_hash": result["result_hash"]},
        created_at_ms=created_at_ms if created_at_ms is not None else int(result["cutoffs"]["acquired_by_ms"]),
        as_of_ms=int(result["cutoffs"]["publicly_available_by_ms"]))
    cited: dict[str, dict[str, Any]] = {}
    for item in result["estimates"]:
        for cite in item["inputs"]:
            cited.setdefault(cite["revision_id"], {k: v for k, v in cite.items() if k != "role"})
    for dividend in result["dividends"]:
        for cite in dividend["inputs"]:
            cited.setdefault(cite["revision_id"], {k: v for k, v in cite.items() if k != "role"})
    evidence = [builder.add_object("evidence", {"contract": "noesis-dividend-flow-input-v1", **cite,
                                                "locator": {"document_id": rid, "source": cite["provider"],
                                                            "cited": True, "detail": cite.get("locator")}},
                                   object_id=f"dividend-flow-input:{rid}") for rid, cite in sorted(cited.items())]
    builder.add_object("receipt", dict(result), object_id=f"dividend-flows:{result['result_hash'][:32]}",
                       references=evidence, root=True)
    return builder.build()

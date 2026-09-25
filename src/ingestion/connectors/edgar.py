"""
SEC EDGAR fetch layer for the filings connector (#821).

The filings mapper (``src.ingestion.connectors.filings``) turns a normalized
:class:`Filing` into a document, ``provider="filing"`` series, and KG
relations. This module supplies the real-world fetch in front of it, over two
public SEC endpoints:

* ``data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` — every reported
  XBRL fact for a filer, mapped into :class:`FilingFact`s for a small set of
  load-bearing us-gaap concepts.
* ``data.sec.gov/submissions/CIK##########.json`` — filer metadata (name,
  recent filings) for the narrative document.

SEC fair-access rules require a descriptive ``User-Agent`` — set
``NOESIS_EDGAR_USER_AGENT`` (e.g. ``"noesis-operator contact@example.com"``);
without it the connector skips with a warning rather than sending anonymous
traffic. The HTTP getter is injectable, so parsing is fully offline-testable.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

from src.ingestion.connectors.filings import Filing, FilingFact

logger = logging.getLogger(__name__)

USER_AGENT_ENV = "NOESIS_EDGAR_USER_AGENT"

_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# The load-bearing us-gaap concepts, in preference order per connector concept.
# Revenue tags moved across taxonomy versions, so both common tags are tried.
CONCEPT_MAP: Dict[str, tuple] = {
    "Revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "NetIncome": ("NetIncomeLoss",),
    "Assets": ("Assets",),
    "Liabilities": ("Liabilities",),
    "OperatingIncome": ("OperatingIncomeLoss",),
}

# XBRL unit -> dataset-series unit.
_UNIT_MAP = {"USD": "usd", "EUR": "eur", "GBP": "gbp"}

# Taxonomies SEC CompanyFacts publishes; filer extension prefixes (``msft:``)
# and ``dei`` cover/entity facts are not part of the normalized fact index.
COMPANYFACTS_TAXONOMIES = frozenset({"us-gaap", "ifrs-full", "srt", "invest"})
MARKET_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A")
MAX_COMPANY_FACT_ROWS = 100_000
MAX_FACT_DIAGNOSTICS = 500
MAX_INLINE_FILING_BYTES = 25 * 1024 * 1024
MAX_INLINE_FACTS = 100_000

# This small, versioned map provides stable names for common statement inputs.
# Other standard tags remain intact and are returned with an explicit unmapped
# status instead of being guessed from their labels.
_CANONICAL_CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "Revenues": "revenue",
    "SalesRevenueNet": "revenue",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "revenue",
    "NetIncomeLoss": "net_income",
    "ProfitLoss": "net_income",
    "Assets": "assets",
    "AssetsCurrent": "current_assets",
    "Liabilities": "liabilities",
    "LiabilitiesCurrent": "current_liabilities",
    "StockholdersEquity": "stockholders_equity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "stockholders_equity_including_noncontrolling_interest",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_cash_equivalents",
    "OperatingIncomeLoss": "operating_income",
    "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
    "NetCashProvidedByUsedInInvestingActivities": "investing_cash_flow",
    "NetCashProvidedByUsedInFinancingActivities": "financing_cash_flow",
}
_INCOME_TAGS = set(_CANONICAL_CONCEPTS) | {
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "GrossProfit",
    "OperatingExpenses",
    "ResearchAndDevelopmentExpense",
    "SellingGeneralAndAdministrativeExpense",
    "DepreciationDepletionAndAmortization",
    "NonoperatingIncomeExpense",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeTaxExpenseBenefit",
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
}
_BALANCE_TAGS = {
    "Assets",
    "AssetsCurrent",
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperation",
    "AccountsReceivableNetCurrent",
    "InventoryNet",
    "PropertyPlantAndEquipmentNet",
    "Goodwill",
    "IntangibleAssetsNetExcludingGoodwill",
    "Liabilities",
    "LiabilitiesCurrent",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "RetainedEarningsAccumulatedDeficit",
}
_CASH_FLOW_TAGS = {
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireBusinessesNetOfCashAcquired",
    "NetCashProvidedByUsedInInvestingActivities",
    "PaymentsOfDividendsCommonStock",
    "PaymentsForRepurchaseOfCommonStock",
    "ProceedsFromIssuanceOfLongTermDebt",
    "RepaymentsOfLongTermDebt",
    "NetCashProvidedByUsedInFinancingActivities",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
}
_CORE_CANONICAL_CONCEPTS = {
    "income_statement": {"revenue", "net_income"},
    "balance_sheet": {"assets", "liabilities"},
    "cash_flow": {"operating_cash_flow"},
}


def _http_get(url: str, user_agent: str) -> str:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 - fixed SEC hosts
        return resp.read().decode("utf-8")


def normalize_cik(raw: Union[str, int]) -> str:
    """A zero-padded 10-digit CIK from any int/str form ('320193' -> '0000320193')."""
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        raise ValueError(f"not a CIK: {raw!r}")
    return digits.zfill(10)


def _fact_period(fact: Dict[str, Any]) -> Optional[str]:
    """Contract period for one XBRL fact: 'YYYY' for a fiscal year, 'YYYY-Qn'
    for a quarter. None for facts without a usable frame."""
    fy = fact.get("fy")
    fp = (fact.get("fp") or "").upper()
    if fy is None or not fp:
        return None
    if fp == "FY":
        return str(fy)
    m = re.match(r"^Q([1-4])$", fp)
    if m:
        return f"{fy}-Q{m.group(1)}"
    return None


class EdgarClient:
    """Thin, injectable-HTTP client over the public EDGAR JSON endpoints."""

    def __init__(
        self,
        user_agent: Optional[str] = None,
        http_get: Optional[Callable[[str, str], str]] = None,
    ):
        self._user_agent = (
            user_agent if user_agent is not None else os.getenv(USER_AGENT_ENV, "")
        ).strip()
        self._http_get = http_get or _http_get

    @property
    def configured(self) -> bool:
        return bool(self._user_agent)

    def _get_json(self, url: str, *, preserve_decimal: bool = False) -> Any:
        return json.loads(
            self._http_get(url, self._user_agent),
            parse_float=Decimal if preserve_decimal else float,
        )

    def resolve_ticker(self, ticker: str) -> Optional[str]:
        """Ticker -> zero-padded CIK via the SEC ticker table, or None."""
        table = self._get_json(_TICKERS_URL)
        wanted = ticker.strip().upper()
        for entry in (table or {}).values():
            if str(entry.get("ticker", "")).upper() == wanted:
                return normalize_cik(entry["cik_str"])
        return None

    def company_facts(self, cik: str) -> Dict[str, Any]:
        # CompanyFacts numeric lexemes are retained as Decimal through mapping;
        # legacy FilingFact projections explicitly convert to float below.
        return self._get_json(
            _FACTS_URL.format(cik=normalize_cik(cik)), preserve_decimal=True
        )

    def submissions(self, cik: str) -> Dict[str, Any]:
        return self._get_json(_SUBMISSIONS_URL.format(cik=normalize_cik(cik)))

    def filing_document(self, cik: str, accession: str, document_name: str) -> str:
        """Fetch one SEC filing document by validated accession and basename."""

        cik = normalize_cik(cik)
        accession = str(accession).strip()
        document_name = str(document_name).strip()
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise ValueError("accession must have the SEC accession format")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}\.html?", document_name, re.I):
            raise ValueError("document_name must be an SEC filing HTML basename")
        numeric_cik = str(int(cik))
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{numeric_cik}/"
            f"{accession.replace('-', '')}/{quote(document_name, safe='.-_')}"
        )
        if self._http_get is _http_get:
            import urllib.request

            request = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed SEC host
                body = response.read(MAX_INLINE_FILING_BYTES + 1)
            if len(body) > MAX_INLINE_FILING_BYTES:
                raise ValueError("SEC filing document exceeds the configured byte limit")
            return body.decode("utf-8", errors="replace")
        body = self._http_get(url, self._user_agent)
        if len(body.encode("utf-8")) > MAX_INLINE_FILING_BYTES:
            raise ValueError("SEC filing document exceeds the configured byte limit")
        return body


def primary_document_for_accession(
    submissions: Mapping[str, Any], accession: str
) -> str | None:
    """Resolve an accession's primary document from SEC recent submissions."""

    recent = (submissions.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    documents = recent.get("primaryDocument") or []
    for filed_accession, document in zip(accessions, documents):
        if str(filed_accession) != accession:
            continue
        if isinstance(document, str) and re.fullmatch(
            r"[A-Za-z0-9._-]{1,180}\.html?", document, re.I
        ):
            return document
        return None
    return None


def facts_to_filing_facts(
    payload: Dict[str, Any], forms: tuple = ("10-K", "10-Q")
) -> List[FilingFact]:
    """Map a companyfacts payload to :class:`FilingFact`s for the mapped concepts.

    Only facts reported on the given forms are used; for a (concept, period)
    reported more than once (amendments, restatements), the most recently
    filed value wins.
    """
    us_gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    chosen: Dict[tuple, tuple] = {}  # (concept, period) -> (filed, value, unit)
    for concept, tags in CONCEPT_MAP.items():
        for tag in tags:
            tag_facts = us_gaap.get(tag)
            if not tag_facts:
                continue
            for xbrl_unit, entries in (tag_facts.get("units") or {}).items():
                unit = _UNIT_MAP.get(xbrl_unit)
                if unit is None:
                    continue
                for entry in entries:
                    if entry.get("form") not in forms:
                        continue
                    period = _fact_period(entry)
                    value = entry.get("val")
                    if period is None or value is None:
                        continue
                    key = (concept, period)
                    filed = str(entry.get("filed") or "")
                    if key not in chosen or filed > chosen[key][0]:
                        chosen[key] = (filed, float(value), unit)
            if any(k[0] == concept for k in chosen):
                break  # this tag produced data; skip the fallback tags
    return [
        FilingFact(concept=concept, value=value, period=period, unit=unit)
        for (concept, period), (_filed, value, unit) in sorted(chosen.items())
    ]


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: str(item) if isinstance(item, Decimal) else str(item),
    )


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _html_local_name(element: Any) -> str:
    tag = getattr(element, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _html_attr(element: Any, name: str) -> str | None:
    attributes = getattr(element, "attrib", {})
    for key, value in attributes.items():
        local = str(key).rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        if local.casefold() == name.casefold():
            return str(value)
    return None


def _element_text(element: Any) -> str:
    """Collect inline fact text while respecting ix:exclude descendants."""

    pieces: list[str] = []

    def visit(node: Any) -> None:
        if _html_local_name(node) == "exclude":
            return
        if getattr(node, "text", None):
            pieces.append(node.text)
        for child in list(node):
            visit(child)
            if getattr(child, "tail", None):
                pieces.append(child.tail)

    visit(element)
    return "".join(pieces)


def _inline_unit(unit_element: Any) -> str | None:
    measures = [
        "".join(measure.itertext()).strip().rsplit(":", 1)[-1]
        for measure in unit_element.iter()
        if _html_local_name(measure) == "measure"
    ]
    if not measures:
        return None
    divide_index = next(
        (index for index, item in enumerate(unit_element.iter()) if _html_local_name(item) == "divide"),
        None,
    )
    if divide_index is None or len(measures) == 1:
        return measures[0]
    numerator = []
    denominator = []
    in_denominator = False
    for item in unit_element.iter():
        name = _html_local_name(item)
        if name == "unitdenominator":
            in_denominator = True
        elif name == "unitnumerator":
            in_denominator = False
        elif name == "measure":
            target = denominator if in_denominator else numerator
            target.append("".join(item.itertext()).strip().rsplit(":", 1)[-1])
    return f"{'*'.join(numerator)}/{'*'.join(denominator)}" if numerator and denominator else "*".join(measures)


_NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_NUMBER_WORDS.update(
    {
        word: 10 * (index + 2)
        for index, word in enumerate(
            "twenty thirty forty fifty sixty seventy eighty ninety".split()
        )
    }
)
_NUMBER_SCALES = {"thousand": 10**3, "million": 10**6, "billion": 10**9}


def _english_number_words(raw: str) -> Decimal | None:
    """Parse the ixt-sec:numwordsen vocabulary ("none", "twenty-two", ...)."""

    words = re.sub(r"[^a-z\s-]", " ", raw.casefold()).replace("-", " ").split()
    words = [word for word in words if word != "and"]
    if words in (["no"], ["none"]):
        return Decimal(0)
    if not words:
        return None
    total = current = 0
    for word in words:
        if word in _NUMBER_WORDS:
            current += _NUMBER_WORDS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        elif word in _NUMBER_SCALES:
            total += max(current, 1) * _NUMBER_SCALES[word]
            current = 0
        else:
            return None
    return Decimal(total + current)


def _inline_numeric_value(element: Any) -> tuple[str | None, str | None]:
    raw = _element_text(element).replace("\u00a0", " ").replace("\u2009", " ").strip()
    # Transformation Registry 1/2 names ("numdotdecimal") and 3+ names
    # ("num-dot-decimal") differ only in hyphenation; SEC adds ixt-sec rules.
    transform = (
        (_html_attr(element, "format") or "")
        .rsplit(":", 1)[-1]
        .casefold()
        .replace("-", "")
    )
    supported_transforms = {
        "numdotdecimal",
        "numcommadecimal",
        "numunitdecimal",
        "numpercent",
        "numdash",
        "zerodash",
        "fixedzero",
        "numwordsen",
    }
    if transform and transform not in supported_transforms:
        return None, "unsupported_numeric_transform"
    if transform == "fixedzero":
        raw = "0"
    if not raw:
        return None, "empty_numeric_fact"
    if transform == "numwordsen":
        words_value = _english_number_words(raw)
        if words_value is None:
            return None, "unsupported_numeric_transform"
        raw = format(words_value, "f")
    if transform in {"numdash", "zerodash"} and raw in {"-", "–", "—", "−"}:
        raw = "0"
    if transform == "numcommadecimal":
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    negative_parens = raw.startswith("(") and raw.endswith(")")
    if negative_parens:
        raw = raw[1:-1]
    raw = raw.replace("−", "-").replace("–", "-").replace("—", "-")
    numeric = re.sub(r"[^0-9.+\-eE]", "", raw)
    try:
        value = Decimal(numeric)
    except (InvalidOperation, ValueError):
        return None, "unsupported_numeric_transform"
    if not value.is_finite():
        return None, "invalid_numeric_value"
    scale_text = _html_attr(element, "scale") or "0"
    try:
        scale = int(scale_text)
    except ValueError:
        return None, "invalid_numeric_scale"
    if abs(scale) > 30:
        return None, "invalid_numeric_scale"
    value = value.scaleb(scale)
    if negative_parens or _html_attr(element, "sign") == "-":
        value = -abs(value)
    return format(value, "f"), None


def _inline_context(context: Any) -> dict[str, Any] | None:
    descendants = list(context.iter())
    period_node = next((node for node in descendants if _html_local_name(node) == "period"), None)
    if period_node is None:
        return None
    period_children = {_html_local_name(node): "".join(node.itertext()).strip() for node in period_node.iter() if node is not period_node}
    instant = _iso_date(period_children.get("instant"))
    start = _iso_date(period_children.get("startdate"))
    end = _iso_date(period_children.get("enddate"))
    if instant is not None:
        period: dict[str, str] = {"kind": "instant", "instant_date": instant.isoformat()}
        period_class = "instant"
    elif start is not None and end is not None and start <= end:
        period = {"kind": "duration", "start_date": start.isoformat(), "end_date": end.isoformat()}
        days = (end - start).days + 1
        period_class = "quarter" if 70 <= days <= 120 else "annual" if 330 <= days <= 400 else "other_duration"
    else:
        return None
    dimensions = []
    for node in descendants:
        name = _html_local_name(node)
        if name == "explicitmember":
            dimensions.append({"kind": "explicit", "dimension": _html_attr(node, "dimension"), "member": _element_text(node).strip()})
        elif name == "typedmember":
            dimensions.append({"kind": "typed", "dimension": _html_attr(node, "dimension"), "value": _element_text(node).strip()})
    entity = next((node for node in descendants if _html_local_name(node) == "identifier"), None)
    return {
        "period": period,
        "period_class": period_class,
        "entity_identifier": None if entity is None else _element_text(entity).strip(),
        "dimensions": dimensions,
    }


def parse_inline_xbrl_facts(
    document_html: str,
    *,
    accession: str,
    max_facts: int = MAX_INLINE_FACTS,
) -> dict[str, Any]:
    """Extract native-context Inline XBRL facts from one SEC filing document.

    The parser reads facts and contexts only. It does not infer missing filing
    values, calculate accounting totals, or treat dimensional facts as
    consolidated facts. Unsupported transforms and contexts are diagnostic.
    """

    if not isinstance(document_html, str) or not document_html.strip():
        raise ValueError("document_html must be nonempty text")
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(accession)):
        raise ValueError("accession must have the SEC accession format")
    if type(max_facts) is not int or not 1 <= max_facts <= MAX_INLINE_FACTS:
        raise ValueError("max_facts must be within the inline fact bound")
    if len(document_html.encode("utf-8")) > MAX_INLINE_FILING_BYTES:
        raise ValueError("filing document exceeds the configured byte limit")
    from lxml import html as lxml_html

    parser = lxml_html.HTMLParser(no_network=True, recover=True, huge_tree=False)
    # SEC Inline XBRL documents are XHTML with an XML declaration; lxml rejects
    # an encoding declaration on already-decoded text, so drop it here.
    root = lxml_html.fromstring(
        re.sub(r"\A\s*<\?xml[^>]*\?>", "", document_html, count=1), parser=parser
    )
    contexts: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    for node in root.iter():
        name = _html_local_name(node)
        node_id = _html_attr(node, "id")
        if name == "context" and node_id:
            context = _inline_context(node)
            if context is not None:
                contexts[node_id] = context
        elif name == "unit" and node_id:
            unit = _inline_unit(node)
            if unit:
                units[node_id] = unit

    diagnostics: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    skipped = 0
    truncated = False
    for node in root.iter():
        node_type = _html_local_name(node)
        if node_type not in {"nonfraction", "fraction"}:
            continue
        if len(facts) >= max_facts:
            truncated = True
            break
        name = _html_attr(node, "name") or ""
        qname = name.split(":", 1)
        if len(qname) != 2:
            skipped += 1
            diagnostics.append({"code": "invalid_fact_name", "name": name})
            continue
        taxonomy, concept = qname
        context_ref = _html_attr(node, "contextref") or ""
        context = contexts.get(context_ref)
        if context is None:
            skipped += 1
            diagnostics.append({"code": "missing_or_invalid_context", "context_ref": context_ref, "concept": concept})
            continue
        if node_type == "fraction":
            skipped += 1
            diagnostics.append({"code": "unsupported_fraction_fact", "concept": concept, "context_ref": context_ref})
            continue
        if (_html_attr(node, "nil") or "").casefold() == "true":
            skipped += 1
            diagnostics.append({"code": "nil_fact", "concept": concept, "context_ref": context_ref})
            continue
        unit_ref = _html_attr(node, "unitref") or ""
        unit = units.get(unit_ref)
        if unit is None:
            skipped += 1
            diagnostics.append({"code": "missing_or_invalid_unit", "unit_ref": unit_ref, "concept": concept})
            continue
        value, error = _inline_numeric_value(node)
        if error:
            skipped += 1
            diagnostics.append({"code": error, "concept": concept, "context_ref": context_ref})
            continue
        facts.append({
            "filing_accession": str(accession),
            "taxonomy": taxonomy,
            "concept": concept,
            "unit": unit,
            "period": context["period"],
            "period_class": context["period_class"],
            "value_lexical": value,
            "native_context_id": context_ref,
            "context_id_kind": "inline_xbrl_native",
            "dimensions": context["dimensions"],
            "entity_identifier": context["entity_identifier"],
            "unit_ref": unit_ref,
            "decimals": _html_attr(node, "decimals"),
            "scale": int(_html_attr(node, "scale") or "0"),
            "source_locator": f"{accession}/{context_ref}/{taxonomy}:{concept}",
        })
    if truncated:
        diagnostics.append({"code": "inline_fact_limit_reached", "max_facts": max_facts})
    return {
        "contract": "noesis-edgar-inline-facts-v1",
        "filing_accession": str(accession),
        "facts": facts,
        "diagnostics": diagnostics[:MAX_FACT_DIAGNOSTICS],
        "counts": {"facts": len(facts), "skipped": skipped, "truncated": truncated},
        "readiness": "partial" if diagnostics or truncated else "ready",
    }


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _day_start_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, timezone.utc).timestamp() * 1000)


def _parse_acceptance_time(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if re.fullmatch(r"\d{14}", candidate):
        try:
            parsed = datetime.strptime(candidate, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
    else:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def submissions_acceptance_times(submissions: Mapping[str, Any]) -> Dict[str, int]:
    """Extract exact accepted timestamps by accession where submissions has them."""

    recent = (submissions.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    accepted = recent.get("acceptanceDateTime") or []
    result: Dict[str, int] = {}
    for accession, timestamp in zip(accessions, accepted):
        normalized = str(accession or "").strip()
        accepted_ms = _parse_acceptance_time(timestamp)
        if normalized and accepted_ms is not None:
            result[normalized] = accepted_ms
    return result


def _statement_for_tag(taxonomy: str, concept: str) -> str:
    if taxonomy != "us-gaap":
        return "other"
    if concept in _BALANCE_TAGS:
        return "balance_sheet"
    if concept in _CASH_FLOW_TAGS:
        return "cash_flow"
    if concept in _INCOME_TAGS:
        return "income_statement"
    return "other"


def _period_from_company_fact(
    entry: Mapping[str, Any],
) -> tuple[dict[str, str] | None, str]:
    start = _iso_date(entry.get("start"))
    end = _iso_date(entry.get("end"))
    if end is None:
        return None, "unknown"
    if start is None:
        if entry.get("start") is not None:
            return None, "unknown"
        return {"kind": "instant", "instant_date": end.isoformat()}, "instant"
    if start > end:
        return None, "unknown"
    days = (end - start).days + 1
    fiscal_period = str(entry.get("fp") or "").upper()
    if 70 <= days <= 120:
        period_class = "quarter"
    elif 330 <= days <= 400:
        period_class = "annual"
    elif fiscal_period in {"Q2", "Q3"} and 120 < days < 330:
        period_class = "year_to_date"
    elif days > 0:
        period_class = "other_duration"
    else:
        period_class = "unknown"
    return {
        "kind": "duration",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }, period_class


def _lexical_number(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    return str(number)


def companyfacts_to_market_facts(
    payload: Mapping[str, Any],
    *,
    cik: str,
    issuer_id: str,
    namespace: str,
    retrieved_at_ms: int,
    accepted_at_by_accession: Mapping[str, int] | None = None,
    forms: tuple[str, ...] = MARKET_FORMS,
    max_rows: int = MAX_COMPANY_FACT_ROWS,
) -> dict[str, Any]:
    """Normalize SEC CompanyFacts entries without discarding filing vintages.

    CompanyFacts omits native XBRL ``contextRef`` values. The returned
    ``context_id`` is therefore a deterministic composite key and each record
    says ``context_id_kind=companyfacts_composite_key``; it must not be
    presented as the filing's native context identifier.
    """

    cik = normalize_cik(cik)
    if not issuer_id or not namespace:
        raise ValueError("issuer_id and namespace are required")
    if type(retrieved_at_ms) is not int or retrieved_at_ms < 0:
        raise ValueError("retrieved_at_ms must be nonnegative epoch milliseconds")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_COMPANY_FACT_ROWS:
        raise ValueError("max_rows must be within the CompanyFacts processing bound")
    accepted_times = dict(accepted_at_by_accession or {})
    facts_root = payload.get("facts") or {}
    if not isinstance(facts_root, Mapping):
        return {
            "facts": [],
            "diagnostics": [{"code": "invalid_facts_object"}],
            "counts": {"facts": 0, "truncated": False},
            "readiness": "unavailable",
        }
    snapshot_id = f"sec-companyfacts:{cik}:{_stable_digest(payload)[:24]}"
    numeric_cik = str(int(cik))
    facts: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    unmapped_tags: dict[str, int] = {}
    core_by_accession: dict[str, dict[str, set[str]]] = {}
    conflict_values: dict[tuple[Any, ...], set[str]] = {}
    seen: dict[str, str] = {}
    truncated = False
    processed_rows = 0

    def note(code: str, **details: Any) -> None:
        skipped[code] = skipped.get(code, 0) + 1
        if len(diagnostics) < MAX_FACT_DIAGNOSTICS:
            diagnostics.append({"code": code, **details})

    for taxonomy, concepts in facts_root.items():
        if not isinstance(concepts, Mapping) or taxonomy == "dei":
            continue
        for concept, fact_body in concepts.items():
            if not isinstance(fact_body, Mapping):
                note("invalid_concept_payload", taxonomy=taxonomy, concept=concept)
                continue
            statement = _statement_for_tag(str(taxonomy), str(concept))
            canonical = (
                _CANONICAL_CONCEPTS.get(str(concept)) if taxonomy == "us-gaap" else None
            )
            mapping_status = "mapped" if canonical else "unmapped"
            units = fact_body.get("units") or {}
            if not isinstance(units, Mapping):
                note("invalid_units_payload", taxonomy=taxonomy, concept=concept)
                continue
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    note(
                        "invalid_fact_entries",
                        taxonomy=taxonomy,
                        concept=concept,
                        unit=unit,
                    )
                    continue
                for entry in entries:
                    if processed_rows >= max_rows:
                        truncated = True
                        break
                    processed_rows += 1
                    if not isinstance(entry, Mapping):
                        note("invalid_fact_entry", taxonomy=taxonomy, concept=concept)
                        continue
                    if entry.get("form") not in forms:
                        continue
                    accession = str(entry.get("accn") or "").strip()
                    if not accession:
                        note("missing_accession", taxonomy=taxonomy, concept=concept)
                        continue
                    value_lexical = _lexical_number(entry.get("val"))
                    if value_lexical is None:
                        note(
                            "invalid_numeric_value",
                            accession=accession,
                            concept=concept,
                        )
                        continue
                    period, period_class = _period_from_company_fact(entry)
                    if period is None:
                        note("invalid_period", accession=accession, concept=concept)
                        continue
                    filed_date = _iso_date(entry.get("filed"))
                    if filed_date is None:
                        filed_at_ms = None
                        filed_public_ms = None
                    else:
                        filed_at_ms = _day_start_ms(filed_date)
                        # Without an acceptance timestamp, do not claim that
                        # the record was public earlier than the filed day end.
                        filed_public_ms = filed_at_ms + 86_400_000 - 1
                    accepted_at_ms = accepted_times.get(accession)
                    public_at_ms = (
                        accepted_at_ms
                        if accepted_at_ms is not None
                        else filed_public_ms
                    )
                    if public_at_ms is None:
                        note(
                            "public_time_unknown",
                            accession=accession,
                            concept=concept,
                        )
                    descriptor = {
                        "accession": accession,
                        "taxonomy": str(taxonomy),
                        "concept": str(concept),
                        "unit": str(unit),
                        "period": period,
                        "fy": entry.get("fy"),
                        "fp": entry.get("fp"),
                        "frame": entry.get("frame"),
                    }
                    context_id = (
                        "sec-companyfacts-context:" + _stable_digest(descriptor)[:32]
                    )
                    fact_observation_id = (
                        "market-fact:"
                        + _stable_digest(
                            [issuer_id, accession, taxonomy, concept, context_id, unit]
                        )[:40]
                    )
                    source_file = (
                        f"https://www.sec.gov/Archives/edgar/data/{numeric_cik}/"
                        f"{accession.replace('-', '')}/{accession}-index.html"
                    )
                    source_locator = "/".join(
                        [
                            "companyfacts",
                            quote(str(taxonomy), safe=""),
                            quote(str(concept), safe=""),
                            quote(str(unit), safe=""),
                            quote(accession, safe=""),
                            context_id,
                        ]
                    )
                    source_content_hash = _stable_digest(
                        {"descriptor": descriptor, "entry": dict(entry)}
                    )
                    source_ref = {
                        "source_ref_id": f"sec-fact-source:{_stable_digest([accession, context_id])[:24]}",
                        "provider": "sec-edgar",
                        "provider_object_id": accession,
                        "source_revision_id": accession,
                        "public_at_ms": public_at_ms,
                        "source_snapshot_id": snapshot_id,
                        "source_url": source_file,
                        "retrieved_at_ms": retrieved_at_ms,
                        "content_hash": source_content_hash,
                        "license_id": "sec-public",
                        "entitlement_id": "sec-edgar-public",
                    }
                    fiscal_year = entry.get("fy")
                    try:
                        fiscal_year = (
                            int(fiscal_year) if fiscal_year is not None else None
                        )
                    except (TypeError, ValueError):
                        fiscal_year = None
                    if fiscal_year is not None and fiscal_year < 1:
                        fiscal_year = None
                    record: dict[str, Any] = {
                        "contract": "noesis-market-financial-fact-v1",
                        "namespace": namespace,
                        "owner": None,
                        "fact_observation_id": fact_observation_id,
                        "issuer_id": issuer_id,
                        "revision_id": f"{fact_observation_id}@1",
                        "revision": 1,
                        "filing_accession": accession,
                        "filing_form": str(entry.get("form")),
                        "taxonomy": str(taxonomy),
                        "concept": str(concept),
                        "canonical_concept": canonical,
                        "statement": statement,
                        "mapping_status": mapping_status,
                        "context_id": context_id,
                        "context_id_kind": "companyfacts_composite_key",
                        "unit": str(unit),
                        "period": period,
                        "fiscal_year": fiscal_year,
                        "fiscal_period": str(entry.get("fp")).upper()
                        if entry.get("fp") is not None
                        else None,
                        "period_class": period_class,
                        "value_lexical": value_lexical,
                        "scale": int(entry.get("scale") or 0),
                        "decimals": entry.get("decimals"),
                        "filed_at_ms": filed_at_ms,
                        "accepted_at_ms": accepted_at_ms,
                        "public_at_ms": public_at_ms,
                        "retrieved_at_ms": retrieved_at_ms,
                        "source_document_revision_id": f"sec-filing:{accession}@{accession}",
                        "source_locator": source_locator,
                        "provider": "sec-edgar",
                        "prior_revision_id": None,
                        "source_refs": [source_ref],
                        "recorded_at_ms": retrieved_at_ms,
                    }
                    record["record_hash"] = _stable_digest(record)
                    record_key = fact_observation_id
                    prior_value = seen.get(record_key)
                    if prior_value is not None:
                        if prior_value != value_lexical:
                            note(
                                "duplicate_context_conflict",
                                accession=accession,
                                concept=concept,
                                context_id=context_id,
                            )
                            facts = [
                                item
                                for item in facts
                                if item["fact_observation_id"] != fact_observation_id
                            ]
                        continue
                    seen[record_key] = value_lexical
                    facts.append(record)
                    if canonical:
                        core_by_accession.setdefault(
                            accession,
                            {name: set() for name in _CORE_CANONICAL_CONCEPTS},
                        ).setdefault(statement, set()).add(canonical)
                    else:
                        unmapped_tags[str(concept)] = (
                            unmapped_tags.get(str(concept), 0) + 1
                        )
                    period_key = _stable_json(period)
                    conflict_key = (accession, taxonomy, concept, unit, period_key)
                    conflict_values.setdefault(conflict_key, set()).add(value_lexical)
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break

    for (
        accession,
        taxonomy,
        concept,
        unit,
        period_key,
    ), values in conflict_values.items():
        if len(values) > 1:
            note(
                "conflicting_contexts",
                accession=accession,
                taxonomy=taxonomy,
                concept=concept,
                unit=unit,
                period=json.loads(period_key),
                value_count=len(values),
            )
    for accession, statements in core_by_accession.items():
        for statement, required in _CORE_CANONICAL_CONCEPTS.items():
            present = statements.get(statement, set())
            for concept in sorted(required - present):
                note(
                    "missing_core_concept",
                    accession=accession,
                    statement=statement,
                    canonical_concept=concept,
                )
    for concept, count in sorted(unmapped_tags.items()):
        if len(diagnostics) < MAX_FACT_DIAGNOSTICS:
            diagnostics.append(
                {
                    "code": "unmapped_accounting_concept",
                    "concept": concept,
                    "fact_count": count,
                }
            )
    if truncated:
        note("fact_limit_reached", max_rows=max_rows)
    return {
        "facts": sorted(
            facts,
            key=lambda item: (
                item["filing_accession"],
                item["taxonomy"],
                item["concept"],
                item["context_id"],
                item["unit"],
            ),
        ),
        "diagnostics": diagnostics,
        "counts": {
            "facts": len(facts),
            "processed_rows": processed_rows,
            "skipped_or_flagged": sum(skipped.values()),
            "unmapped_fact_rows": sum(unmapped_tags.values()),
            "truncated": truncated,
        },
        "readiness": "partial" if diagnostics or truncated else "ready",
    }


def _numeric_identity(value: str) -> str:
    """Canonical numeric identity: "13.7", "13.70" and "1.37E+1" compare equal."""

    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return value
    if not number.is_finite():
        return value
    return format(number.normalize(), "f") if number != 0 else "0"


def _reconciliation_key(fact: Mapping[str, Any]) -> tuple[str, str, str, str, str] | None:
    """Return the context-independent identity used by filing reconciliation."""

    accession = str(
        fact.get("filing_accession") or fact.get("accession") or ""
    ).strip()
    taxonomy = str(fact.get("taxonomy") or "").strip()
    concept = str(fact.get("concept") or "").strip()
    unit = str(fact.get("unit") or "").strip()
    period = fact.get("period")
    if not all((accession, taxonomy, concept, unit)) or not isinstance(period, Mapping):
        return None
    period = dict(period)
    if (
        period.get("kind") == "duration"
        and period.get("start_date")
        and period.get("start_date") == period.get("end_date")
    ):
        # CompanyFacts publishes a zero-length duration as an instant ("end"
        # only); compare both representations on the same date.
        period = {"kind": "instant", "instant_date": period["end_date"]}
    return accession, taxonomy, concept, unit, _stable_json(period)


def _decimals_value(raw: Any) -> int | None:
    """XBRL ``decimals`` as an int; None for INF or absent (exact)."""

    text = str(raw).strip() if raw is not None else ""
    if not text or text.upper() == "INF":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _consistent_duplicates(values: Mapping[str, int | None]) -> str | None:
    """Most precise value if duplicate facts agree within their precision.

    Follows the XBRL duplicate-fact rule: duplicates are consistent when each
    less precise value lies within the rounding interval of its ``decimals``
    around the most precise value. Returns None for inconsistent duplicates.
    """

    parsed = []
    for lexical, decimals in values.items():
        try:
            parsed.append((Decimal(lexical), decimals, lexical))
        except (InvalidOperation, ValueError):
            return None
    exact_first = sorted(
        parsed, key=lambda item: (item[1] is not None, -(item[1] or 0))
    )
    best_value, _, best_lexical = exact_first[0]
    for value, decimals, _ in exact_first[1:]:
        if decimals is None:
            if value != best_value:
                return None
            continue
        if abs(value - best_value) > Decimal(5).scaleb(-decimals - 1):
            return None
    return best_lexical


def reconcile_market_facts_to_filing(
    batch: Mapping[str, Any],
    filing_facts: Sequence[Mapping[str, Any]],
    *,
    max_rows: int = MAX_COMPANY_FACT_ROWS,
) -> dict[str, Any]:
    """Compare normalized CompanyFacts with facts extracted from a filing.

    The filing-side sequence is intentionally an adapter boundary: an
    Inline-XBRL or instance-document parser can supply native contexts without
    changing the CompanyFacts store. Matching ignores context identifiers and
    compares all values for an accession/tag/unit/actual-period key, so a
    duplicate or conflicting context cannot be hidden by choosing one row.

    The function is deterministic and review-oriented. It reports unsupported
    mappings and source disagreements; it never derives or substitutes a value.
    Filing-side rows must include ``filing_accession``, ``taxonomy``,
    ``concept``, ``unit``, ``period`` and either ``value_lexical`` or ``value``.
    """

    if not isinstance(batch, Mapping):
        raise ValueError("batch must be a mapping")
    if not isinstance(filing_facts, Sequence) or isinstance(
        filing_facts, (str, bytes, bytearray)
    ):
        raise ValueError("filing_facts must be a bounded sequence")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_COMPANY_FACT_ROWS:
        raise ValueError("max_rows must be within the CompanyFacts processing bound")
    normalized = batch.get("facts") or []
    if not isinstance(normalized, list):
        raise ValueError("batch.facts must be a list")
    if len(normalized) > max_rows or len(filing_facts) > max_rows:
        raise ValueError("reconciliation input exceeds the row bound")

    diagnostics: list[dict[str, Any]] = [
        dict(item)
        for item in (batch.get("diagnostics") or [])
        if isinstance(item, Mapping)
    ]
    observed: dict[tuple[str, str, str, str, str], set[str]] = {}
    observed_contexts: dict[tuple[str, str, str, str, str], list[str]] = {}
    expected: dict[tuple[str, str, str, str, str], set[str]] = {}
    expected_contexts: dict[tuple[str, str, str, str, str], list[str]] = {}
    invalid_filing_rows = 0
    unsupported_mappings = 0
    unsupported_seen: set[tuple[str, str, str, str]] = set()
    expected_decimals: dict[tuple[str, str, str, str, str], dict[str, int | None]] = {}

    def row_value(row: Mapping[str, Any]) -> str | None:
        lexical = row.get("value_lexical")
        if isinstance(lexical, str) and lexical.strip():
            return lexical.strip()
        return _lexical_number(row.get("value"))

    for row in normalized:
        if not isinstance(row, Mapping):
            continue
        key = _reconciliation_key(row)
        value = row_value(row)
        if key is None or value is None:
            continue
        observed.setdefault(key, set()).add(value)
        observed_contexts.setdefault(key, []).append(
            str(row.get("context_id") or "unknown")
        )
        if row.get("mapping_status") == "unmapped":
            unsupported_mappings += 1
            mapping_key = (key[0], key[1], key[2], key[3])
            if mapping_key not in unsupported_seen:
                unsupported_seen.add(mapping_key)
                diagnostics.append(
                    {
                        "code": "unsupported_accounting_mapping",
                        "filing_accession": key[0],
                        "taxonomy": key[1],
                        "concept": key[2],
                        "unit": key[3],
                    }
                )

    normalized_taxonomies = {key[1] for key in observed}
    dimensional_by_concept: dict[str, int] = {}
    excluded_by_taxonomy: dict[str, int] = {}
    for row in filing_facts:
        if not isinstance(row, Mapping):
            invalid_filing_rows += 1
            continue
        key = _reconciliation_key(row)
        value = row_value(row)
        if key is None or value is None:
            invalid_filing_rows += 1
            continue
        # CompanyFacts carries only non-dimensional (consolidated) facts from
        # the standard taxonomies. Segment/member facts and filer extension
        # taxonomies are outside its coverage: count them rather than report
        # every one as a missing or conflicting value.
        if row.get("dimensions"):
            name = f"{key[1]}:{key[2]}"
            dimensional_by_concept[name] = dimensional_by_concept.get(name, 0) + 1
            continue
        if key[1] not in COMPANYFACTS_TAXONOMIES and key[1] not in normalized_taxonomies:
            excluded_by_taxonomy[key[1]] = excluded_by_taxonomy.get(key[1], 0) + 1
            continue
        expected.setdefault(key, set()).add(value)
        expected_decimals.setdefault(key, {})[value] = _decimals_value(row.get("decimals"))
        expected_contexts.setdefault(key, []).append(
            str(row.get("native_context_id") or row.get("context_id") or "unknown")
        )

    # Collapse consistent filing duplicates (the same fact tagged again at a
    # lower precision, e.g. "$22.8 billion" in prose) to the precise value.
    consistent_duplicates = 0
    for key, values in expected_decimals.items():
        if len({_numeric_identity(item) for item in values}) < 2:
            continue
        precise = _consistent_duplicates(values)
        if precise is not None:
            consistent_duplicates += 1
            expected[key] = {precise}

    matched = mismatched = missing = extra = 0
    for key in sorted(set(observed) | set(expected)):
        actual = observed.get(key, set())
        reference = expected.get(key, set())
        if not actual:
            missing += 1
            diagnostics.append(
                {
                    "code": "missing_normalized_fact",
                    "filing_accession": key[0],
                    "taxonomy": key[1],
                    "concept": key[2],
                    "unit": key[3],
                    "period": json.loads(key[4]),
                }
            )
        elif not reference:
            extra += 1
            diagnostics.append(
                {
                    "code": "unmatched_normalized_fact",
                    "filing_accession": key[0],
                    "taxonomy": key[1],
                    "concept": key[2],
                    "unit": key[3],
                    "period": json.loads(key[4]),
                    "values": sorted(actual),
                }
            )
        elif {_numeric_identity(item) for item in actual} != {
            _numeric_identity(item) for item in reference
        }:
            mismatched += 1
            diagnostics.append(
                {
                    "code": "filing_value_mismatch",
                    "filing_accession": key[0],
                    "taxonomy": key[1],
                    "concept": key[2],
                    "unit": key[3],
                    "period": json.loads(key[4]),
                    "normalized_values": sorted(actual),
                    "filing_values": sorted(reference),
                }
            )
        else:
            matched += 1

        if len({_numeric_identity(item) for item in actual}) > 1:
            diagnostics.append(
                {
                    "code": "conflicting_contexts",
                    "source": "companyfacts",
                    "filing_accession": key[0],
                    "taxonomy": key[1],
                    "concept": key[2],
                    "unit": key[3],
                    "period": json.loads(key[4]),
                    "context_ids": sorted(observed_contexts[key]),
                    "values": sorted(actual),
                }
            )
        if len({_numeric_identity(item) for item in reference}) > 1:
            diagnostics.append(
                {
                    "code": "conflicting_contexts",
                    "source": "inline_xbrl",
                    "filing_accession": key[0],
                    "taxonomy": key[1],
                    "concept": key[2],
                    "unit": key[3],
                    "period": json.loads(key[4]),
                    "context_ids": sorted(expected_contexts[key]),
                    "values": sorted(reference),
                }
            )

    if invalid_filing_rows:
        diagnostics.append(
            {"code": "invalid_filing_fact", "count": invalid_filing_rows}
        )

    conflicts = sum(
        1 for item in diagnostics if item.get("code") == "conflicting_contexts"
    )
    return {
        "normalized_fact_count": len(observed),
        "filing_fact_count": len(expected),
        "matched": matched,
        "mismatched": mismatched,
        "missing_normalized": missing,
        "unmatched_normalized": extra,
        "unsupported_mappings": unsupported_mappings,
        "invalid_filing_rows": invalid_filing_rows,
        "conflicting_contexts": conflicts,
        # Values agree only when every comparable fact matched exactly once.
        "value_status": (
            "consistent"
            if not (mismatched or missing or extra or conflicts or invalid_filing_rows)
            else "review_required"
        ),
        "coverage": {
            "dimensional_filing_facts_not_compared": sum(dimensional_by_concept.values()),
            "dimensional_concepts": dict(
                sorted(dimensional_by_concept.items())[:MAX_FACT_DIAGNOSTICS]
            ),
            "filing_facts_outside_companyfacts_taxonomies": dict(
                sorted(excluded_by_taxonomy.items())
            ),
            "consistent_duplicate_filing_facts": consistent_duplicates,
        },
        "diagnostics": diagnostics,
        "readiness": "ready" if not diagnostics else "partial",
    }


def reconcile_market_financial_facts_with_sec(
    query: Union[str, int],
    *,
    issuer_id: str,
    namespace: str,
    accession: str,
    client: Optional[EdgarClient] = None,
    document_name: str | None = None,
    retrieved_at_ms: int | None = None,
    forms: tuple[str, ...] = MARKET_FORMS,
) -> dict[str, Any]:
    """Fetch one official SEC filing and compare it with normalized CompanyFacts.

    CompanyFacts and the Inline XBRL filing are retained as distinct inputs.
    Context IDs stay native on the filing side, while dimensional duplicates,
    unsupported mappings, missing facts, and value disagreements remain
    diagnostics in the returned receipt.
    """

    client = client or EdgarClient()
    if not client.configured:
        raise ValueError(f"set {USER_AGENT_ENV} to a descriptive SEC User-Agent before live reconciliation")
    raw_query = str(query).strip()
    cik = normalize_cik(raw_query) if re.fullmatch(r"\d{1,10}", raw_query) else client.resolve_ticker(raw_query)
    if cik is None:
        raise ValueError("query did not resolve to an SEC CIK")
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(accession)):
        raise ValueError("accession must have the SEC accession format")
    submissions = client.submissions(cik)
    primary_document = document_name or primary_document_for_accession(submissions, accession)
    if primary_document is None:
        raise ValueError("primary filing document is unavailable in SEC recent submissions")
    filing_html = client.filing_document(cik, accession, primary_document)
    inline = parse_inline_xbrl_facts(filing_html, accession=accession)
    facts_payload = client.company_facts(cik)
    retrieved = int(datetime.now(timezone.utc).timestamp() * 1000) if retrieved_at_ms is None else retrieved_at_ms
    company_batch = companyfacts_to_market_facts(
        facts_payload,
        cik=cik,
        issuer_id=issuer_id,
        namespace=namespace,
        retrieved_at_ms=retrieved,
        accepted_at_by_accession=submissions_acceptance_times(submissions),
        forms=forms,
    )
    selected_facts = [
        fact for fact in company_batch["facts"]
        if fact.get("filing_accession") == accession
    ]
    selected_diagnostics = [
        diagnostic for diagnostic in company_batch.get("diagnostics", [])
        if diagnostic.get("filing_accession") == accession
        or diagnostic.get("code") in {"invalid_facts_object", "fact_limit_reached"}
    ]
    normalized_batch = {
        **company_batch,
        "facts": selected_facts,
        "diagnostics": selected_diagnostics,
    }
    reconciliation = reconcile_market_facts_to_filing(normalized_batch, inline["facts"])
    numeric_cik = str(int(cik))
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{numeric_cik}/"
        f"{accession.replace('-', '')}/{quote(primary_document, safe='.-_')}"
    )
    return {
        "contract": "noesis-edgar-market-fact-reconciliation-v1",
        "cik": cik,
        "issuer_id": issuer_id,
        "namespace": namespace,
        "filing_accession": accession,
        "filing_form": next((fact.get("filing_form") for fact in selected_facts), None),
        "filing_url": filing_url,
        "companyfacts_snapshot_id": next((fact.get("source_refs", [{}])[0].get("source_snapshot_id") for fact in selected_facts), None),
        "companyfacts": {"count": len(selected_facts), "diagnostics": selected_diagnostics},
        "inline_xbrl": {"count": inline["counts"]["facts"], "diagnostics": inline["diagnostics"], "native_contexts": True},
        "reconciliation": reconciliation,
        "readiness": "ready" if reconciliation["readiness"] == "ready" and inline["readiness"] == "ready" else "partial",
        "limitations": [
            "SEC CompanyFacts excludes some dimensional facts; dimensions in the Inline XBRL document are retained and disagreements remain visible.",
            "This comparison validates the selected public filing, not commercial provider coverage or analyst usefulness.",
        ],
    }


def harvest_market_financial_facts(
    query: Union[str, int],
    *,
    issuer_id: str,
    namespace: str,
    retrieved_at_ms: int | None = None,
    client: Optional[EdgarClient] = None,
    forms: tuple[str, ...] = MARKET_FORMS,
) -> Optional[dict[str, Any]]:
    """Fetch as-filed normalized fact records and diagnostics from EDGAR."""

    client = client or EdgarClient()
    if not client.configured:
        logger.warning(
            "EDGAR: no %s configured — skipping financial-fact harvest", USER_AGENT_ENV
        )
        return None
    raw = str(query).strip()
    if re.fullmatch(r"\d{1,10}", raw):
        cik = normalize_cik(raw)
    else:
        cik = client.resolve_ticker(raw)
        if cik is None:
            logger.warning("EDGAR: ticker %r did not resolve to a CIK", raw)
            return None
    facts_payload = client.company_facts(cik)
    submissions = client.submissions(cik)
    retrieved = (
        int(datetime.now(timezone.utc).timestamp() * 1000)
        if retrieved_at_ms is None
        else retrieved_at_ms
    )
    batch = companyfacts_to_market_facts(
        facts_payload,
        cik=cik,
        issuer_id=issuer_id,
        namespace=namespace,
        retrieved_at_ms=retrieved,
        accepted_at_by_accession=submissions_acceptance_times(submissions),
        forms=forms,
    )
    batch["cik"] = cik
    batch["entity_name"] = facts_payload.get("entityName")
    return batch


def harvest_filing(
    query: Union[str, int],
    client: Optional[EdgarClient] = None,
    forms: tuple = ("10-K", "10-Q"),
) -> Optional[Filing]:
    """Fetch a filer from EDGAR (by ticker or CIK) as a normalized Filing.

    Skip-with-warning discipline: with no ``NOESIS_EDGAR_USER_AGENT``
    configured, returns None rather than sending anonymous traffic. Returns
    None likewise for an unresolvable ticker.
    """
    client = client or EdgarClient()
    if not client.configured:
        logger.warning("EDGAR: no %s configured — skipping harvest", USER_AGENT_ENV)
        return None

    raw = str(query).strip()
    if re.fullmatch(r"\d{1,10}", raw):
        cik = normalize_cik(raw)
    else:
        cik = client.resolve_ticker(raw)
        if cik is None:
            logger.warning("EDGAR: ticker %r did not resolve to a CIK", raw)
            return None

    facts_payload = client.company_facts(cik)
    submissions = client.submissions(cik)

    filer = submissions.get("name") or facts_payload.get("entityName") or f"CIK {cik}"
    description = (submissions.get("sicDescription") or "").strip()
    narrative = f"{filer}: EDGAR filer profile." + (
        f" Industry: {description}." if description else ""
    )
    officers: List[str] = []  # officer data needs per-filing parsing; out of scope here

    return Filing(
        filer=str(filer),
        filing_id=f"edgar-{cik}",
        cik=cik,
        facts=facts_to_filing_facts(facts_payload, forms=forms),
        narrative=narrative,
        officers=officers,
        source_url=_FACTS_URL.format(cik=cik),
    )

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
import logging
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

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

    def _get_json(self, url: str) -> Any:
        return json.loads(self._http_get(url, self._user_agent))

    def resolve_ticker(self, ticker: str) -> Optional[str]:
        """Ticker -> zero-padded CIK via the SEC ticker table, or None."""
        table = self._get_json(_TICKERS_URL)
        wanted = ticker.strip().upper()
        for entry in (table or {}).values():
            if str(entry.get("ticker", "")).upper() == wanted:
                return normalize_cik(entry["cik_str"])
        return None

    def company_facts(self, cik: str) -> Dict[str, Any]:
        return self._get_json(_FACTS_URL.format(cik=normalize_cik(cik)))

    def submissions(self, cik: str) -> Dict[str, Any]:
        return self._get_json(_SUBMISSIONS_URL.format(cik=normalize_cik(cik)))


def facts_to_filing_facts(payload: Dict[str, Any], forms: tuple = ("10-K", "10-Q")) -> List[FilingFact]:
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
    narrative = f"{filer}: EDGAR filer profile." + (f" Industry: {description}." if description else "")
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

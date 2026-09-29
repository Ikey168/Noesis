"""SEC company materials beyond financial statements (#1672).

Acquires public SEC evidence for a company and returns rows for
:meth:`MarketResearchStore.save_materials`:

* ``earnings_release`` / ``earnings_presentation``: 8-K Item 2.02 exhibits
  (EX-99.x), linked to the reporting period, acceptance time and document.
* ``guidance``: forward-looking outlook sentences found in those releases,
  kept as dated *management claims* with exact character spans. Successive
  releases form the guidance history; nothing is reconciled or inferred.
* ``segment_disclosure``: segment-dimensioned Inline XBRL facts from the
  latest 10-K/10-Q.
* ``insider_transaction``: Form 3/4/5 non-derivative transactions.
* ``beneficial_ownership``: Schedule 13D/13G and proxy (DEF 14A) filings.
* ``earnings_call_transcript``, ``analyst_consensus`` and
  ``institutional_holdings_13f``: explicitly ``unavailable``/``unlicensed``.
  SEC does not publish transcripts or consensus, and 13F holdings are filed per
  manager, not per issuer; a licensed provider is required.

Every acquired row names the accession, document URL, SHA-256 of the fetched
bytes and ``licensing_status="public"``. Amendments link to the material they
correct; identical exhibit bytes filed twice are marked as duplicates. The
fetcher is injectable (``EdgarClient``), so parsing is offline-testable.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from src.ingestion.connectors.edgar import (
    EdgarClient,
    normalize_cik,
    parse_inline_xbrl_facts,
)

CONTRACT = "noesis-sec-company-materials-v1"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_GUIDANCE_STATEMENTS = 40
SEGMENT_AXES = {
    "StatementBusinessSegmentsAxis": "operating_segment",
    "SegmentReportingInformationBySegmentAxis": "operating_segment",
    "ProductOrServiceAxis": "revenue_by_product_or_service",
    "StatementGeographicalAxis": "revenue_by_geography",
}
SEGMENT_CONCEPTS = {
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "OperatingIncomeLoss",
    "GrossProfit",
    "CostOfRevenue",
}
OWNERSHIP_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A", "DEF 14A"}
INSIDER_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A"}
_GUIDANCE_CUE = re.compile(
    r"\b(outlook|guidance|expects?|expected to|anticipates?|forecasts?|targets?|"
    r"projects?|projected)\b",
    re.I,
)
_NUMBER_CUE = re.compile(r"(\$\s?\d|\d(?:\.\d+)?\s?(?:%|percent|billion|million)|\d+\s?(?:to|-|–)\s?\d)")
_UNAVAILABLE = (
    ("earnings_call_transcript", "unlicensed", "SEC does not publish earnings-call transcripts; a licensed transcript provider is required."),
    ("analyst_consensus", "unlicensed", "Pre-event analyst consensus and estimate history require a licensed estimates provider."),
    ("institutional_holdings_13f", "unavailable", "Form 13F holdings are filed per institutional manager; issuer-level aggregation needs a licensed dataset or a manager-wide crawl."),
)


def _sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _millis(iso: str | None) -> int | None:
    if not iso:
        return None
    text = str(iso).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text) if "T" in text else datetime.fromisoformat(text + "T00:00:00+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _archive_url(cik: str, accession: str, document: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{quote(document, safe='._-/')}"
    )


def _fetch(client: EdgarClient, url: str) -> str:
    body = client._http_get(url, client._user_agent)  # noqa: SLF001 - shared SEC fetcher
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("SEC document exceeds the configured byte limit")
    return body


def filing_documents(index_html: str) -> list[dict[str, str]]:
    """Parse an SEC ``<accession>-index.htm`` document table."""

    from lxml import html as lxml_html

    root = lxml_html.fromstring(index_html)
    documents = []
    for row in root.iter("tr"):
        cells = row.findall("td")
        if len(cells) < 4:
            continue
        link = cells[2].find(".//a")
        href = link.get("href", "") if link is not None else ""
        name = href.rsplit("/", 1)[-1]
        if href.startswith("/ix?doc="):
            name = href.split("doc=", 1)[1].rsplit("/", 1)[-1]
        if not name:
            continue
        documents.append({
            "sequence": cells[0].text_content().strip(),
            "description": cells[1].text_content().strip(),
            "document": name,
            "type": cells[3].text_content().strip(),
        })
    return documents


_BLOCK_TAGS = {
    "p", "div", "li", "br", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "ul", "ol", "section", "article", "header", "footer", "blockquote",
}


def document_text(document_html: str) -> str:
    """Visible text of an HTML exhibit with one line per block element.

    Block boundaries become newlines so headings, bullets and table cells do
    not run together into false sentences; spaces within a line collapse.
    """

    from lxml import html as lxml_html

    root = lxml_html.fromstring(re.sub(r"\A\s*<\?xml[^>]*\?>", "", document_html, count=1))
    for element in root.xpath("//script|//style"):
        element.drop_tree()
    for element in root.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag.rsplit("}", 1)[-1].lower() in _BLOCK_TAGS:
            element.tail = "\n" + (element.tail or "")
    lines = (
        re.sub(r"[ \t\r\f\v\u00a0\u2009\u200b]+", " ", line).strip(" •·▪")
        for line in root.text_content().split("\n")
    )
    return "\n".join(line.strip() for line in lines if line.strip())


def extract_guidance_statements(text: str) -> list[dict[str, Any]]:
    """Sentences stating a quantified outlook, with exact spans into ``text``.

    These are candidate management claims for review, not verified figures:
    the matcher needs a forward-looking cue and a number in the same sentence.
    """

    statements = []
    # A period ends a sentence only before whitespace or the end of a line, so
    # "$41.3 billion" and "approximately 9.5%" stay intact.
    for match in re.finditer(r"(?:[^.!?\n]|[.!?](?=[^\s]))+(?:[.!?](?=\s|$))?", text):
        sentence = match.group(0).strip()
        if len(sentence) < 25 or len(sentence) > 800:
            continue
        if not (_GUIDANCE_CUE.search(sentence) and _NUMBER_CUE.search(sentence)):
            continue
        if re.search(r"forward-looking statements|safe harbor|risks? and uncertainties", sentence, re.I):
            continue
        start = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
        statements.append({
            "text": sentence,
            "start": start,
            "end": start + len(sentence),
            "claim_status": "management_claim",
        })
        if len(statements) >= MAX_GUIDANCE_STATEMENTS:
            break
    return statements


def parse_insider_transactions(xml_text: str) -> dict[str, Any]:
    """Reporting owners and non-derivative transactions from a Form 3/4/5 XML."""

    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)

    def value(node: Any, path: str) -> str | None:
        found = node.find(path)
        if found is None:
            return None
        inner = found.find("value")
        text = (inner if inner is not None else found).text
        return text.strip() if text and text.strip() else None

    owners = []
    for owner in root.findall("reportingOwner"):
        relationship = owner.find("reportingOwnerRelationship")
        roles = []
        if relationship is not None:
            for flag, label in (("isDirector", "director"), ("isOfficer", "officer"), ("isTenPercentOwner", "ten_percent_owner"), ("isOther", "other")):
                if (value(relationship, flag) or "").lower() in {"1", "true"}:
                    roles.append(label)
        owners.append({
            "name": value(owner, "reportingOwnerId/rptOwnerName"),
            "owner_cik": value(owner, "reportingOwnerId/rptOwnerCik"),
            "roles": roles,
            "officer_title": value(owner, "reportingOwnerRelationship/officerTitle"),
        })
    transactions = []
    for item in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        transactions.append({
            "security": value(item, "securityTitle"),
            "date": value(item, "transactionDate"),
            "code": value(item, "transactionCoding/transactionCode"),
            "shares": value(item, "transactionAmounts/transactionShares"),
            "price": value(item, "transactionAmounts/transactionPricePerShare"),
            "acquired_disposed": value(item, "transactionAmounts/transactionAcquiredDisposedCode"),
            "shares_owned_after": value(item, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
        })
    return {
        "document_type": value(root, "documentType"),
        "period_of_report": value(root, "periodOfReport"),
        "owners": owners,
        "transactions": transactions,
    }


def segment_facts(inline_facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Segment-dimensioned statement facts (one explicit segment member)."""

    rows = []
    for fact in inline_facts:
        dimensions = fact.get("dimensions") or []
        if fact.get("concept") not in SEGMENT_CONCEPTS or len(dimensions) != 1:
            continue
        dimension = dimensions[0]
        axis = str(dimension.get("dimension") or "").rsplit(":", 1)[-1]
        if axis not in SEGMENT_AXES or dimension.get("kind") != "explicit":
            continue
        rows.append({
            "segment": str(dimension.get("member") or ""),
            "axis": axis,
            "breakdown": SEGMENT_AXES[axis],
            "concept": fact["concept"],
            "unit": fact.get("unit"),
            "period": fact.get("period"),
            "value_lexical": fact.get("value_lexical"),
            "native_context_id": fact.get("native_context_id"),
        })
    return rows


def harvest_sec_company_materials(
    query: str | int,
    *,
    issuer_id: str,
    client: EdgarClient | None = None,
    since_ms: int | None = None,
    max_earnings_filings: int = 4,
    max_insider_filings: int = 10,
    max_ownership_filings: int = 10,
    include_segments: bool = True,
    request_pause_s: float = 0.15,
) -> dict[str, Any]:
    """Acquire public SEC company materials as ``save_materials`` rows."""

    client = client or EdgarClient()
    if not client.configured:
        raise ValueError("set NOESIS_EDGAR_USER_AGENT to a descriptive SEC User-Agent")
    for name, bound in (("max_earnings_filings", max_earnings_filings), ("max_insider_filings", max_insider_filings), ("max_ownership_filings", max_ownership_filings)):
        if type(bound) is not int or not 0 <= bound <= 50:
            raise ValueError(f"{name} must be between 0 and 50")
    raw = str(query).strip()
    cik = normalize_cik(raw) if re.fullmatch(r"\d{1,10}", raw) else client.resolve_ticker(raw)
    if cik is None:
        raise ValueError("query did not resolve to an SEC CIK")
    retrieved_at_ms = int(time.time() * 1000)
    submissions = client.submissions(cik)
    recent = (submissions.get("filings") or {}).get("recent") or {}
    filings = [
        dict(zip(recent.keys(), values))
        for values in zip(*(recent.get(key) or [] for key in recent))
    ]
    materials: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    exhibit_hashes: dict[str, str] = {}
    release_texts: dict[str, str] = {}

    def pause() -> None:
        if request_pause_s:
            time.sleep(request_pause_s)

    def base_row(filing: Mapping[str, Any], kind: str, document: str, content_hash: str | None) -> dict[str, Any]:
        accession = filing["accessionNumber"]
        published = _millis(filing.get("acceptanceDateTime")) or _millis(filing.get("filingDate")) or 0
        return {
            "material_id": f"sec:{accession}:{document}:{kind}",
            "kind": kind,
            "published_at_ms": published,
            "event_at_ms": _millis(filing.get("reportDate")),
            "reporting_period": filing.get("reportDate") or None,
            "document_locator": _archive_url(cik, accession, document),
            "source_revision_id": f"sec:{accession}:{document}" + (f"@sha256:{content_hash[:16]}" if content_hash else ""),
            "content_sha256": content_hash,
            "licensing_status": "public",
            "provider": "sec_edgar",
            "form": filing.get("form"),
            "accession": accession,
            "retrieved_at_ms": retrieved_at_ms,
        }

    in_window = [
        filing for filing in filings
        if since_ms is None or (_millis(filing.get("acceptanceDateTime")) or 0) >= since_ms
    ]

    # Earnings releases (8-K Item 2.02) and their exhibits.
    earnings = [f for f in in_window if f.get("form") in {"8-K", "8-K/A"} and "2.02" in str(f.get("items") or "")]
    originals: dict[str, str] = {}
    for filing in reversed(earnings[:max_earnings_filings]):
        accession = filing["accessionNumber"]
        try:
            index = _fetch(client, _archive_url(cik, accession, f"{accession}-index.htm"))
            pause()
        except Exception as exc:  # noqa: BLE001 - recorded, never inferred
            diagnostics.append({"code": "filing_index_unavailable", "accession": accession, "error": type(exc).__name__})
            continue
        exhibits = [doc for doc in filing_documents(index) if doc["type"].upper().startswith("EX-99")]
        if not exhibits:
            diagnostics.append({"code": "earnings_exhibit_missing", "accession": accession})
        for position, exhibit in enumerate(exhibits):
            try:
                html = _fetch(client, _archive_url(cik, accession, exhibit["document"]))
                pause()
            except Exception as exc:  # noqa: BLE001
                diagnostics.append({"code": "exhibit_unavailable", "accession": accession, "document": exhibit["document"], "error": type(exc).__name__})
                continue
            content_hash = _sha256(html)
            kind = "earnings_release" if position == 0 else "earnings_presentation"
            row = base_row(filing, kind, exhibit["document"], content_hash)
            row["exhibit_type"] = exhibit["type"]
            row["items"] = str(filing.get("items") or "")
            if content_hash in exhibit_hashes:
                row["duplicate_of"] = exhibit_hashes[content_hash]
            else:
                exhibit_hashes[content_hash] = row["material_id"]
            if filing["form"] == "8-K/A" and filing.get("reportDate") in originals:
                row["corrects_material_id"] = originals[filing["reportDate"]]
            elif kind == "earnings_release":
                originals[str(filing.get("reportDate"))] = row["material_id"]
            materials.append(row)
            text = document_text(html)
            if kind == "earnings_release":
                release_texts[row["material_id"]] = text
            statements = extract_guidance_statements(text)
            if statements:
                guidance = base_row(filing, "guidance", exhibit["document"], content_hash)
                guidance.update({
                    "material_id": f"sec:{accession}:{exhibit['document']}:guidance",
                    "derived_from_material_id": row["material_id"],
                    "text_sha256": _sha256(text),
                    "statements": statements,
                    "claim_status": "management_claim",
                })
                materials.append(guidance)

    # Segment disclosures from the latest periodic report.
    if include_segments:
        periodic = next((f for f in in_window if f.get("form") in {"10-Q", "10-K"} and f.get("isInlineXBRL")), None)
        if periodic is None:
            diagnostics.append({"code": "periodic_report_unavailable"})
        else:
            try:
                html = client.filing_document(cik, periodic["accessionNumber"], periodic["primaryDocument"])
                pause()
                facts = parse_inline_xbrl_facts(html, accession=periodic["accessionNumber"])["facts"]
                segments = segment_facts(facts)
                row = base_row(periodic, "segment_disclosure", periodic["primaryDocument"], _sha256(html))
                row["segments"] = segments
                row["segment_members"] = sorted({item["segment"] for item in segments})
                if not segments:
                    diagnostics.append({"code": "segment_facts_not_found", "accession": periodic["accessionNumber"]})
                materials.append(row)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append({"code": "periodic_report_unavailable", "accession": periodic["accessionNumber"], "error": type(exc).__name__})

    # Insider transactions (Forms 3/4/5 filed under the issuer).
    for filing in [f for f in in_window if f.get("form") in INSIDER_FORMS][:max_insider_filings]:
        document = str(filing.get("primaryDocument") or "")
        raw_document = document.split("/", 1)[1] if document.startswith("xslF345") and "/" in document else document
        if not raw_document.lower().endswith(".xml"):
            diagnostics.append({"code": "insider_document_not_xml", "accession": filing["accessionNumber"]})
            continue
        try:
            xml_text = _fetch(client, _archive_url(cik, filing["accessionNumber"], raw_document))
            pause()
            parsed = parse_insider_transactions(xml_text)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append({"code": "insider_filing_unparsed", "accession": filing["accessionNumber"], "error": type(exc).__name__})
            continue
        row = base_row(filing, "insider_transaction", raw_document, _sha256(xml_text))
        row.update(parsed)
        if str(filing.get("form", "")).endswith("/A"):
            row["amendment"] = True
        materials.append(row)

    # Beneficial ownership filings (13D/13G) and proxy statements.
    for filing in [f for f in in_window if f.get("form") in OWNERSHIP_FORMS][:max_ownership_filings]:
        row = base_row(filing, "beneficial_ownership", str(filing.get("primaryDocument") or "filing"), None)
        row["parse_status"] = "metadata_only"
        row["note"] = (
            "Proxy beneficial-ownership tables are not parsed."
            if filing.get("form") == "DEF 14A"
            else "Reporting persons and percentages are in the filing document; they are not parsed here."
        )
        materials.append(row)

    for kind, status, reason in _UNAVAILABLE:  # explicit, never inferred
        materials.append({
            "material_id": f"sec:{cik}:{kind}:not-acquired",
            "kind": kind,
            "published_at_ms": retrieved_at_ms,
            "event_at_ms": None,
            "document_locator": f"unavailable:{kind}",
            "source_revision_id": f"unavailable:{kind}",
            "licensing_status": status,
            "provider": None,
            "unavailable_reason": reason,
        })

    acquired = [row for row in materials if row["licensing_status"] == "public"]
    return {
        "contract": CONTRACT,
        "cik": cik,
        "issuer_id": issuer_id,
        "entity_name": submissions.get("name"),
        "retrieved_at_ms": retrieved_at_ms,
        "materials": materials,
        "coverage": {
            kind: sum(1 for row in acquired if row["kind"] == kind)
            for kind in (
                "earnings_release", "earnings_presentation", "guidance",
                "segment_disclosure", "insider_transaction", "beneficial_ownership",
            )
        },
        "unavailable": [item[0] for item in _UNAVAILABLE],
        # Transient normalized release text for span-linked analysis; the
        # saved materials artifact keeps only locators and hashes.
        "release_texts": release_texts,
        "diagnostics": diagnostics,
        "readiness": "partial" if diagnostics else "ready",
        "limitations": [
            "Guidance rows are keyword-and-number matched management claims with exact spans; they are not verified or normalized figures.",
            "Transcripts, analyst consensus and 13F holdings are not SEC issuer materials and are marked unavailable rather than inferred.",
            "Only the SEC 'recent' submissions window is scanned.",
        ],
    }


__all__ = [
    "CONTRACT",
    "document_text",
    "extract_guidance_statements",
    "filing_documents",
    "harvest_sec_company_materials",
    "parse_insider_transactions",
    "segment_facts",
]

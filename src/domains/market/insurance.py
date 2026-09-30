"""Insurance supervisory statistics, SFCR reports and catastrophe-loss estimates (#2230: IN02, IN08, IN09).

The record owner of the Market pack's optional ``insurance`` feature. Every
record is *what one publisher publishes*, kept with its reported value,
currency, reporting period, publisher, licence and revision history:

* ``insurer_report``: one insurer's report (an SFCR), with the insurer's
  identifiers as published (LEI, NAIC codes, national ID), the reporting level
  (``solo`` undertaking or ``group``), the reporting year, the document locator
  and digest. Its ``figures`` quote the public QRT cells with template, row and
  column locators. A cell that could not be read is ``unknown`` with a reason
  and is never estimated.
* ``supervisory_indicator``: one observation a supervisor publishes (premiums,
  claims, SCR, own funds, a solvency ratio *as reported*) for a market, a line
  of business or an insurer. It carries the period, the value, the unit and
  currency, and the release vintage. A confidential or not-reported cell keeps
  its published marker with a ``null`` value and is never read as zero.
* ``catastrophe_loss_estimate``: one publisher's estimate for an event as the
  publisher names it (insured or economic), with the value or range, the
  currency and the publication date. Each new publication is a revision of that
  publisher's own series. Estimates from different publishers are never
  merged.
* ``publication_reference``: a publication from a ``metadata-only`` source
  (IN01), recorded without figures.

Store rules (one DuckDB connection, no new store):

* a stable ``record_id`` per namespace, provider and source identifier;
* unchanged content adds nothing. A supervisory figure that is unchanged in a
  later release adds no revision, although the release is recorded as seen. A
  changed figure, a corrected SFCR or a new estimate publication adds a
  revision, and a reversion is a new revision;
* "current" follows the publication clock and then the observation order, so a
  late-arriving older publication is kept as history.

Point-in-time reads follow the Market ``public_and_acquired`` policy
(:mod:`src.domains.market.asof`). A revision is visible when its publication
clock is at or before the public cutoff and it was acquired by the
acquisition cutoff. A stated publication *date* counts as the end of that day
(UTC). A missing one falls back to the first observation time.

Links (IN08) between estimates and Natural Hazards events or insurer records
rest on published identifiers or explicit citations. A name or date match is
only a reviewable candidate. Queries (IN09) put figures side by side with their
definitions, currencies and periods, and compute no ratio, reconciliation,
score or loss model.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CONTRACT = "noesis-insurance-record-v1"
READ_SCOPE = "market:insurance:read"
WRITE_SCOPE = "market:insurance:write"
REVIEW_SCOPE = "market:insurance:review"
DEFAULT_NAMESPACE = "market-insurance"
KINDS = (
    "insurer_report",
    "supervisory_indicator",
    "catastrophe_loss_estimate",
    "publication_reference",
)
REPORTING_LEVELS = ("solo", "group", "unknown")
ESTIMATE_TYPES = ("insured", "economic")
DECISIONS = ("in-scope", "metadata-only", "excluded")
DAY_MS = 86_400_000
NOTICE = (
    "Figures and estimates as each publisher published them. No solvency or rating assessment, no loss "
    "modelling, and no ratio, reconciliation, projection or aggregate across publishers is computed."
)
FORBIDDEN_FIELDS = (
    "solvency_assessment",
    "rating",
    "score",
    "risk_score",
    "loss_model",
    "projection",
    "derived_ratio",
    "aggregate_across_publishers",
)
MISSING = {"", "-", "–", "n/a", "na", "none", "null"}

# The IN01 decisions (docs/development/insurance-evidence/source-audit.md), one per provider/publisher. A record
# from an ``excluded`` provider is refused, and a ``metadata-only`` provider yields publication references only.
LICENCE_DECISIONS: dict[str, dict[str, Any]] = {
    "eiopa-insurance-statistics": {
        "publisher": "European Insurance and Occupational Pensions Authority (EIOPA)",
        "decision": "in-scope",
        "kinds": ("supervisory_indicator",),
        "licence": {
            "id": "eiopa-legal-notice",
            "terms_url": "https://www.eiopa.europa.eu/legal-notice_en",
            "attribution": "Source: EIOPA insurance statistics",
        },
        "reason": "reproduction authorised with acknowledgement of the source (verify)",
    },
    "insurer-sfcr": {
        "publisher": "the reporting insurer (Solvency II SFCR)",
        "decision": "in-scope",
        "kinds": ("insurer_report",),
        "licence": {
            "id": "sfcr-public-disclosure",
            "terms_url": "https://eur-lex.europa.eu/eli/dir/2009/138/oj",
            "attribution": "Source: <insurer> Solvency and Financial Condition Report <year>",
        },
        "reason": "legally mandated public disclosure; figures quoted with attribution and link, documents not mirrored",
    },
    "naic-public": {
        "publisher": "National Association of Insurance Commissioners (NAIC)",
        "decision": "metadata-only",
        "kinds": ("publication_reference",),
        "licence": {
            "id": "naic-terms-of-use",
            "terms_url": "https://content.naic.org/terms-use",
            "attribution": "Source: NAIC",
        },
        "reason": "NAIC content is copyrighted; reproduction needs permission (verify); licensed data products "
        "are never acquired",
    },
    "naic-licensed-products": {
        "publisher": "NAIC Financial Data Repository, iSite+, statement data, InsData",
        "decision": "excluded",
        "kinds": (),
        "licence": {
            "id": "naic-subscription",
            "terms_url": "https://content.naic.org/terms-use",
            "attribution": "n/a",
        },
        "reason": "licensed or subscription-only products are never acquired",
    },
    "florida-oir-claims": {
        "publisher": "Florida Office of Insurance Regulation",
        "decision": "in-scope",
        "kinds": ("catastrophe_loss_estimate",),
        "licence": {
            "id": "florida-public-records",
            "terms_url": "https://floir.com/",
            "attribution": "Source: Florida Office of Insurance Regulation, catastrophe claims data",
        },
        "reason": "Florida public records (Chapter 119, F.S.); reuse with attribution (verify)",
    },
    "noaa-ncei-billion-dollar": {
        "publisher": "NOAA National Centers for Environmental Information",
        "decision": "in-scope",
        "kinds": ("catastrophe_loss_estimate",),
        "licence": {
            "id": "us-government-public-domain",
            "terms_url": "https://www.ncei.noaa.gov/access/billions/",
            "attribution": "Source: NOAA NCEI U.S. Billion-Dollar Weather and Climate Disasters",
        },
        "reason": "U.S. federal government data in the public domain; archive (updates ended 2025, verify)",
    },
    "perils": {
        "publisher": "PERILS AG",
        "decision": "excluded",
        "kinds": (),
        "licence": {
            "id": "perils-licensed-index",
            "terms_url": "https://www.perils.org/",
            "attribution": "n/a",
        },
        "reason": "industry loss index is a licensed product; press releases restate licensed data",
    },
    "verisk-pcs": {
        "publisher": "Verisk Property Claim Services",
        "decision": "excluded",
        "kinds": (),
        "licence": {
            "id": "verisk-pcs-licensed",
            "terms_url": "https://www.verisk.com/",
            "attribution": "n/a",
        },
        "reason": "licensed catastrophe loss estimates",
    },
    "swiss-re-sigma": {
        "publisher": "Swiss Re Institute (sigma)",
        "decision": "metadata-only",
        "kinds": ("publication_reference",),
        "licence": {
            "id": "swiss-re-terms",
            "terms_url": "https://www.swissre.com/",
            "attribution": "Source: Swiss Re Institute, sigma",
        },
        "reason": "reproduction of figures needs permission (verify)",
    },
    "munich-re-natcatservice": {
        "publisher": "Munich Re NatCatSERVICE",
        "decision": "metadata-only",
        "kinds": ("publication_reference",),
        "licence": {
            "id": "munich-re-terms",
            "terms_url": "https://www.munichre.com/",
            "attribution": "Source: Munich Re NatCatSERVICE",
        },
        "reason": "database reuse restricted by the terms of use (verify)",
    },
    "ica-catastrophe-list": {
        "publisher": "Insurance Council of Australia",
        "decision": "metadata-only",
        "kinds": ("publication_reference",),
        "licence": {
            "id": "ica-terms",
            "terms_url": "https://insurancecouncil.com.au/",
            "attribution": "Source: Insurance Council of Australia",
        },
        "reason": "reuse terms not confirmed (verify)",
    },
    "broker-reports": {
        "publisher": "Broker catastrophe reports (Aon, Gallagher Re, Guy Carpenter)",
        "decision": "metadata-only",
        "kinds": ("publication_reference",),
        "licence": {
            "id": "broker-copyright",
            "terms_url": "https://www.aon.com/",
            "attribution": "Source: the named broker report",
        },
        "reason": "copyrighted commercial research",
    },
}
SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts/schemas/jsonschema/noesis-insurance-record-v1.json"
)

_DDL = """
CREATE TABLE IF NOT EXISTS insurance_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL,
  source_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS insurance_record_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, change TEXT NOT NULL, public_at_ms BIGINT NOT NULL,
  public_basis TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
CREATE TABLE IF NOT EXISTS insurance_releases (
  namespace TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL, release TEXT NOT NULL,
  release_date TEXT, document_sha256 TEXT, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, provider, dataset, release)
);
CREATE TABLE IF NOT EXISTS insurance_page_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, document TEXT NOT NULL,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, provider, document)
);
CREATE TABLE IF NOT EXISTS insurance_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, record_revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL,
  target_revision_id TEXT NOT NULL, basis TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
TABLES = (
    "insurance_records",
    "insurance_record_revisions",
    "insurance_releases",
    "insurance_page_receipts",
    "insurance_links",
)


class InsuranceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise InsuranceError("unauthorized", f"{required} and namespace access are required")


# ------------------------------------------------------------------ identifiers and values


def fold(text: Any) -> str:
    """Case, accent and punctuation folding for name comparison (never for storage)."""
    value = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def normalize_lei(value: Any) -> str:
    return re.sub(r"\s", "", str(value or "")).upper()


def lei_valid(value: Any) -> bool:
    """ISO 17442: 18 alphanumerics and two check digits (ISO 7064 MOD 97-10)."""
    lei = normalize_lei(value)
    if not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", lei):
        return False
    return int("".join(str(int(ch, 36)) for ch in lei)) % 97 == 1


def normalize_naic(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def iso_day(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    if text.casefold() in MISSING:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    raise InsuranceError("invalid_date", f"unparseable date: {text[:40]}")


def decimal_text(value: Any, *, decimal: str | None = None) -> str | None:
    """A stated number as canonical text; a missing marker is ``None`` (the caller keeps the marker)."""
    text = str(value if value is not None else "").strip().replace(" ", "").replace(" ", "")
    if text.casefold() in MISSING:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if decimal is None:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
    thousands = "." if decimal == "," else ","
    text = text.replace(thousands, "").replace(decimal, ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise InsuranceError("invalid_number", f"unparseable number: {str(value)[:40]!r}") from exc
    if not number.is_finite():
        raise InsuranceError("invalid_number", "numbers must be finite")
    if negative:
        number = -number
    return format(number.normalize(), "f") if number != 0 else "0"


def day_ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def end_of_day_ms(day: str) -> int:
    return day_ms(day) + DAY_MS - 1


def observed_day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, UTC).date().isoformat()


def cutoffs(as_of: Any, acquired_by_ms: int | None = None) -> dict[str, Any]:
    """The Market ``public_and_acquired`` cutoffs for a date (end of day, UTC) or epoch milliseconds."""
    if isinstance(as_of, bool) or as_of is None:
        raise InsuranceError("invalid_request", "as_of is a date (YYYY-MM-DD) or epoch milliseconds")
    if isinstance(as_of, int):
        public, label = as_of, observed_day(as_of)
    else:
        label = iso_day(as_of)
        if label is None:
            raise InsuranceError("invalid_request", "as_of is required")
        public = end_of_day_ms(label)
    if acquired_by_ms is not None and (
        isinstance(acquired_by_ms, bool) or not isinstance(acquired_by_ms, int) or acquired_by_ms < 0
    ):
        raise InsuranceError("invalid_request", "acquired_by_ms is nonnegative epoch milliseconds")
    return {
        "as_of": label,
        "publicly_available_by_ms": public,
        "acquired_by_ms": acquired_by_ms,
        "availability_policy": "public_and_acquired",
        "note": "a stated publication date counts as published at the end of that day (UTC); a missing "
        "publication date falls back to the first observation time",
    }


# ------------------------------------------------------------------ validation


def _fail(message: str, code: str = "invalid_record") -> None:
    raise InsuranceError(code, message)


def _text(value: Any, field: str, *, optional: bool = False, limit: int = 2000) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        _fail(f"{field} is required")
    if not isinstance(value, str) or len(value) > limit:
        _fail(f"{field} must be text of at most {limit} characters")
    return value.strip()


def _url(value: Any, field: str, *, optional: bool = False) -> Any:
    text = _text(value, field, optional=optional)
    if text is not None and not text.startswith("https://"):
        _fail(f"{field} must be an https URL")
    return text


def _decimal(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} is decimal text as published")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal")
    if not number.is_finite():
        _fail(f"{field} must be finite")
    return value


def _insurer(value: Any, field: str = "insurer") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} is an object")
    allowed = {"name", "lei", "naic_company_code", "naic_group_code", "national_id", "country", "reporting_level"}
    if set(value) - allowed:
        _fail(f"{field} has unknown fields {sorted(set(value) - allowed)}")
    out = {
        "name": _text(value.get("name"), f"{field}.name", limit=500),
        "lei": normalize_lei(value.get("lei")) or None,
        "naic_company_code": normalize_naic(value.get("naic_company_code")) or None,
        "naic_group_code": normalize_naic(value.get("naic_group_code")) or None,
        "national_id": _text(value.get("national_id"), f"{field}.national_id", optional=True, limit=100),
        "country": (str(value.get("country") or "").strip().upper() or None),
        "reporting_level": value.get("reporting_level") or "unknown",
    }
    if out["lei"] and not lei_valid(out["lei"]):
        _fail(f"{field}.lei is not a valid ISO 17442 LEI", "invalid_identifier")
    if out["naic_company_code"] and not re.fullmatch(r"\d{5}", out["naic_company_code"]):
        _fail(f"{field}.naic_company_code is five digits", "invalid_identifier")
    if out["naic_group_code"] and not re.fullmatch(r"\d{1,4}", out["naic_group_code"]):
        _fail(f"{field}.naic_group_code is up to four digits", "invalid_identifier")
    if out["country"] and not re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]{1,3})?", out["country"]):
        _fail(f"{field}.country is ISO 3166 as published")
    if out["reporting_level"] not in REPORTING_LEVELS:
        _fail(f"{field}.reporting_level is one of {REPORTING_LEVELS}")
    return out


def _source(value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("source is an object")
    provider = value.get("provider")
    if provider not in LICENCE_DECISIONS:
        _fail(f"unknown provider {provider!r}")
    decision = LICENCE_DECISIONS[provider]
    if decision["decision"] == "excluded":
        _fail(f"{provider} is excluded by the IN01 licence decision: {decision['reason']}", "licence_excluded")
    if kind not in decision["kinds"]:
        code = "metadata_only" if decision["decision"] == "metadata-only" else "not_published_by_provider"
        _fail(f"{provider} ({decision['decision']}) does not yield {kind} records", code)
    sha = value.get("document_sha256")
    if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
        _fail("source.document_sha256 is a SHA-256 hex digest")
    retrieved = value.get("retrieved_at_ms")
    if retrieved is not None and (isinstance(retrieved, bool) or not isinstance(retrieved, int)):
        _fail("source.retrieved_at_ms is epoch milliseconds")
    return {
        "provider": provider,
        "source_id": _text(value.get("source_id"), "source.source_id", limit=500),
        "url": _url(value.get("url"), "source.url"),
        "locator": dict(value.get("locator") or {}),
        "document_sha256": sha,
        "retrieved_at_ms": retrieved,
        "licence": {
            "id": decision["licence"]["id"],
            "terms_url": decision["licence"]["terms_url"],
            "attribution": str((value.get("licence") or {}).get("attribution") or decision["licence"]["attribution"]),
            "decision": decision["decision"],
        },
    }


def _figure(value: Any, index: int) -> dict[str, Any]:
    field = f"figures[{index}]"
    if not isinstance(value, Mapping):
        _fail(f"{field} is an object")
    template = _text(value.get("template"), f"{field}.template", limit=40)
    if not re.fullmatch(r"S\.\d{2}\.\d{2}\.\d{2}", template):
        _fail(f"{field}.template is a QRT code such as S.25.01.21")
    row = _text(value.get("row"), f"{field}.row", limit=10)
    column = _text(value.get("column"), f"{field}.column", limit=10)
    if not re.fullmatch(r"R\d{4}", row) or not re.fullmatch(r"C\d{4}", column):
        _fail(f"{field} row and column are QRT locators (R0000, C0000)")
    status = value.get("status")
    if status not in {"reported", "unknown"}:
        _fail(f"{field}.status is reported or unknown")
    out = {
        "figure_id": f"{template}|{row}|{column}",
        "template": template,
        "row": row,
        "column": column,
        "label": _text(value.get("label"), f"{field}.label", limit=300),
        "status": status,
        "value": _decimal(value.get("value"), f"{field}.value"),
        "unit": _text(value.get("unit"), f"{field}.unit", optional=True, limit=60),
        "currency": _text(value.get("currency"), f"{field}.currency", optional=True, limit=3),
        "quoted": _text(value.get("quoted"), f"{field}.quoted", optional=True, limit=1000),
        "page": value.get("page"),
        "reason": _text(value.get("reason"), f"{field}.reason", optional=True, limit=300),
    }
    if status == "reported" and out["value"] is None:
        _fail(f"{field}: a reported figure states its value")
    if status == "unknown" and (out["value"] is not None or not out["reason"]):
        _fail(f"{field}: an unknown figure has no value and states why", "estimated_value")
    return out


def validate_record(record: Any) -> dict[str, Any]:
    """Validate one record; returns a normalised copy or raises :class:`InsuranceError`."""
    if not isinstance(record, Mapping) or record.get("contract") != CONTRACT:
        _fail(f"contract must be {CONTRACT}")
    kind = record.get("kind")
    if kind not in KINDS:
        _fail(f"kind must be one of {KINDS}")
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in record:
            _fail(f"{forbidden} is outside the feature's scope", "excluded_field")
    out: dict[str, Any] = {
        "contract": CONTRACT,
        "kind": kind,
        "source": _source(record.get("source"), kind),
        "publication_date": iso_day(record.get("publication_date")),
        "unknowns": sorted({str(u) for u in record.get("unknowns") or []}),
    }
    if out["publication_date"] is None:
        out["unknowns"] = sorted({*out["unknowns"], "publication_date"})
    if kind == "insurer_report":
        out["insurer"] = _insurer(record.get("insurer"))
        if record.get("report_type") != "sfcr":
            _fail("report_type is sfcr")
        year = record.get("reporting_year")
        if isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= 2100:
            _fail("reporting_year is a year")
        document = dict(record.get("document") or {})
        out.update(
            {
                "report_type": "sfcr",
                "reporting_year": year,
                "language": _text(record.get("language"), "language", optional=True, limit=10),
                "document": {
                    "url": _url(document.get("url"), "document.url"),
                    "sha256": document.get("sha256"),
                    "media_type": _text(document.get("media_type"), "document.media_type", optional=True, limit=80),
                    "extractor": _text(document.get("extractor"), "document.extractor", optional=True, limit=80),
                },
                "figures": [_figure(f, i) for i, f in enumerate(record.get("figures") or [])],
                "corrects": _text(record.get("corrects"), "corrects", optional=True, limit=200),
            }
        )
        if not re.fullmatch(r"[0-9a-f]{64}", str(out["document"]["sha256"] or "")):
            _fail("document.sha256 is the report's SHA-256 digest")
        ids = [f["figure_id"] for f in out["figures"]]
        if len(ids) != len(set(ids)):
            _fail("each QRT cell is quoted once")
    elif kind == "supervisory_indicator":
        value = _decimal(record.get("value"), "value")
        marker = _text(record.get("marker"), "marker", optional=True, limit=20)
        if value is None and not marker:
            _fail("a missing value keeps the published marker (confidential or not reported), never zero")
        if value is not None and marker:
            _fail("a value and a marker are exclusive")
        period = dict(record.get("period") or {})
        if not (period.get("reference_date") or period.get("reference_year") or period.get("label")):
            _fail("period states a reference date, a reference year or the label as published")
        indicator = _text(record.get("indicator"), "indicator", limit=80)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", indicator):
            _fail("indicator is a snake_case key; indicator_label keeps the published wording")
        out.update(
            {
                "publisher": _text(record.get("publisher"), "publisher", limit=300),
                "dataset": _text(record.get("dataset"), "dataset", limit=300),
                "indicator": indicator,
                "indicator_label": _text(record.get("indicator_label"), "indicator_label", limit=500),
                "definition": _text(record.get("definition"), "definition", optional=True, limit=2000),
                "dimensions": {
                    str(k): (None if v is None else str(v))
                    for k, v in sorted(dict(record.get("dimensions") or {}).items())
                },
                "insurer": _insurer(record["insurer"]) if record.get("insurer") else None,
                "period": {
                    "reference_date": iso_day(period.get("reference_date")) if period.get("reference_date") else None,
                    "reference_year": period.get("reference_year"),
                    "label": period.get("label"),
                },
                "value": value,
                "marker": marker,
                "unit": _text(record.get("unit"), "unit", limit=60),
                "currency": _text(record.get("currency"), "currency", optional=True, limit=3),
                "release": _text(record.get("release"), "release", limit=200),
            }
        )
    elif kind == "catastrophe_loss_estimate":
        event = dict(record.get("event") or {})
        if record.get("estimate_type") not in ESTIMATE_TYPES:
            _fail(f"estimate_type is one of {ESTIMATE_TYPES}")
        value = _decimal(record.get("value"), "value")
        span = dict(record.get("range") or {})
        low, high = _decimal(span.get("low"), "range.low"), _decimal(span.get("high"), "range.high")
        if value is None and low is None and high is None:
            _fail("an estimate states a value or a range as published")
        if low is not None and high is not None and Decimal(low) > Decimal(high):
            _fail("range.low is at most range.high as published")
        identifiers = {str(k): str(v) for k, v in sorted(dict(event.get("identifiers") or {}).items()) if v}
        out.update(
            {
                "publisher": _text(record.get("publisher"), "publisher", limit=300),
                "event": {
                    "name": _text(event.get("name"), "event.name", limit=300),
                    "reference": _text(event.get("reference"), "event.reference", optional=True, limit=200),
                    "identifiers": identifiers,
                    "begin_date": iso_day(event.get("begin_date")) if event.get("begin_date") else None,
                    "end_date": iso_day(event.get("end_date")) if event.get("end_date") else None,
                    "area": _text(event.get("area"), "event.area", optional=True, limit=300),
                },
                "estimate_type": record["estimate_type"],
                "measure": _text(record.get("measure"), "measure", limit=200),
                "value": value,
                "range": {"low": low, "high": high} if (low is not None or high is not None) else None,
                "as_published": _text(record.get("as_published"), "as_published", optional=True, limit=500),
                "unit": _text(record.get("unit"), "unit", limit=60),
                "currency": _text(record.get("currency"), "currency", optional=True, limit=3),
                "insurer": _insurer(record["insurer"]) if record.get("insurer") else None,
                "cites": sorted({_url(u, "cites[]") for u in record.get("cites") or []}),
            }
        )
    else:
        out.update(
            {
                "publisher": _text(record.get("publisher"), "publisher", limit=300),
                "title": _text(record.get("title"), "title", limit=500),
                "period": _text(record.get("period"), "period", optional=True, limit=100),
                "access_decision": out["source"]["licence"]["decision"],
                "reason": LICENCE_DECISIONS[out["source"]["provider"]]["reason"],
            }
        )
        if out["access_decision"] != "metadata-only":
            _fail("publication references record metadata-only sources")
    return out


def semantic(record: Mapping[str, Any]) -> dict[str, Any]:
    """What a revision compares: never retrieval times or row positions; for supervisory figures, never the release."""
    body = json.loads(canonical(record))
    body["source"].pop("retrieved_at_ms", None)
    body["source"].pop("locator", None)
    if body["kind"] == "supervisory_indicator":
        body.pop("release", None)
        body.pop("publication_date", None)
        body["source"].pop("url", None)
        body["source"].pop("document_sha256", None)
        body["unknowns"] = [u for u in body["unknowns"] if u != "publication_date"]
    return body


def record_id(namespace: str, provider: str, source_id: str) -> str:
    return "insurance:" + digest([namespace, provider, source_id])[:24]


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the record contract as a schema module in the shared registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "insurance-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": schema(),
        "owner": "market.insurance",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition, "insurance-schema:insurance-record:1.0.0", principal_id=principal_id, scopes=scopes
        )
    ]


def coverage() -> list[dict[str, Any]]:
    """Every IN01 decision, reported in each answer: excluded and metadata-only sources included."""
    return [
        {
            "provider": provider,
            "publisher": entry["publisher"],
            "decision": entry["decision"],
            "record_kinds": list(entry["kinds"]),
            "reason": entry["reason"],
            "licence": dict(entry["licence"]),
        }
        for provider, entry in sorted(LICENCE_DECISIONS.items())
    ]


def citation(view: Mapping[str, Any]) -> dict[str, Any]:
    record = view["record"]
    source = record["source"]
    return {
        "record_id": view["record_id"],
        "revision_id": view["revision_id"],
        "record_hash": view["record_hash"],
        "provider": source["provider"],
        "source_id": source["source_id"],
        "url": source["url"],
        "locator": source.get("locator") or {},
        "document_sha256": source.get("document_sha256"),
        "publication_date": record.get("publication_date"),
        "public_at_ms": view["public_at_ms"],
        "public_basis": view["public_basis"],
        "observed_at_ms": view["observed_at_ms"],
        "licence": source["licence"],
    }


# ------------------------------------------------------------------ store

_REVISION_FIELDS = (
    "record_id",
    "revision",
    "revision_id",
    "record_hash",
    "payload_json",
    "change",
    "public_at_ms",
    "public_basis",
    "run_id",
    "observed_at_ms",
)


def _order_key(row: Mapping[str, Any]) -> tuple:
    return (int(row["public_at_ms"]), int(row["observed_at_ms"]), int(row["revision"]))


class InsuranceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        rows = self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ("
            + ",".join("?" * len(TABLES))
            + ")",
            list(TABLES),
        ).fetchall()
        return len({r[0] for r in rows}) == len(TABLES)

    def require_ready(self) -> None:
        if not self.ready():
            raise InsuranceError(
                "not_ready",
                "no insurance source has run yet; run the insurance-supervisory-and-catastrophe-losses source pack",
            )

    # -------------------------------------------------------------- writes

    def _revisions(self, namespace: str, rid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_FIELDS) + " FROM insurance_record_revisions "
            "WHERE namespace=? AND record_id=? ORDER BY revision",
            [namespace, rid],
        ).fetchall()
        return [dict(zip(_REVISION_FIELDS, r)) for r in rows]

    def apply(
        self,
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        """Validate and append records; unchanged content adds nothing and every change is a revision."""
        counts = {"inserted": 0, "revised": 0, "unchanged": 0, "history": 0}
        changed: list[str] = []
        prepared = [validate_record(dict(item)) for item in records]
        self.conn.execute("BEGIN")
        try:
            for record in prepared:
                provider, source_id = record["source"]["provider"], record["source"]["source_id"]
                rid = record_id(namespace, provider, source_id)
                record_hash = digest(semantic(record))
                if record["publication_date"]:
                    public_at, basis = end_of_day_ms(record["publication_date"]), "publication-date"
                else:
                    public_at, basis = int(observed_at_ms), "first-observed"
                revisions = self._revisions(namespace, rid)
                incoming = {
                    "public_at_ms": public_at,
                    "observed_at_ms": int(observed_at_ms),
                    "revision": (revisions[-1]["revision"] + 1) if revisions else 1,
                }
                if revisions:
                    current = max(revisions, key=_order_key)
                    if current["record_hash"] == record_hash:
                        counts["unchanged"] += 1
                        continue
                    older = _order_key(incoming) < _order_key(current)
                    if older and any(r["record_hash"] == record_hash for r in revisions):
                        counts["unchanged"] += 1  # a late copy of a state already on record
                        continue
                    change = "history" if older else "revised"
                else:
                    change = "new"
                revision = incoming["revision"]
                revision_id = "insurance-rev:" + digest([rid, revision, record_hash])[:24]
                self.conn.execute(
                    "INSERT INTO insurance_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        rid,
                        revision,
                        revision_id,
                        record_hash,
                        canonical(record),
                        change,
                        public_at,
                        basis,
                        run_id,
                        int(observed_at_ms),
                    ],
                )
                if not revisions:
                    self.conn.execute(
                        "INSERT INTO insurance_records VALUES (?,?,?,?,?,?)",
                        [namespace, rid, record["kind"], provider, source_id, int(observed_at_ms)],
                    )
                    counts["inserted"] += 1
                else:
                    counts["history" if change == "history" else "revised"] += 1
                if change != "history":
                    changed.append(rid)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**counts, "changed": changed}

    def record_release(
        self,
        namespace: str,
        *,
        provider: str,
        dataset: str,
        release: str,
        release_date: str | None,
        document_sha256: str | None,
        run_id: str,
        observed_at_ms: int,
    ) -> bool:
        """Record that a release (vintage) was seen; True when it is new."""
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO insurance_releases VALUES (?,?,?,?,?,?,?,?) RETURNING release",
            [namespace, provider, dataset, release, release_date, document_sha256, run_id, int(observed_at_ms)],
        ).fetchall()
        return bool(inserted)

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT provider, dataset, release, release_date, document_sha256, run_id, observed_at_ms "
            "FROM insurance_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY provider, dataset, observed_at_ms, release",
            [namespace, provider, provider],
        ).fetchall()
        return [
            dict(
                zip(
                    ("provider", "dataset", "release", "release_date", "document_sha256", "run_id", "observed_at_ms"),
                    r,
                )
            )
            for r in rows
        ]

    def record_receipt(
        self,
        namespace: str,
        *,
        run_id: str,
        provider: str,
        document: str,
        receipt: Mapping[str, Any],
        observed_at_ms: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO insurance_page_receipts VALUES (?,?,?,?,?,?)",
            [namespace, run_id, provider, document, canonical(dict(receipt)), int(observed_at_ms)],
        )

    def receipts(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT run_id, provider, document, receipt_json, observed_at_ms FROM insurance_page_receipts "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY observed_at_ms, provider, document",
            [namespace, run_id, run_id],
        ).fetchall()
        return [
            {"run_id": r[0], "provider": r[1], "document": r[2], "receipt": json.loads(r[3]), "observed_at_ms": r[4]}
            for r in rows
        ]

    # -------------------------------------------------------------- reads

    @staticmethod
    def _view(row: Mapping[str, Any], revision_count: int | None = None) -> dict[str, Any]:
        record = json.loads(row["payload_json"])
        view = {
            "record_id": row["record_id"],
            "revision": int(row["revision"]),
            "revision_id": row["revision_id"],
            "record_hash": row["record_hash"],
            "change": row["change"],
            "public_at_ms": int(row["public_at_ms"]),
            "public_basis": row["public_basis"],
            "observed_at_ms": int(row["observed_at_ms"]),
            "run_id": row["run_id"],
            "record": record,
        }
        view["citation"] = citation(view)
        if revision_count is not None:
            view["revisions_on_record"] = revision_count
        return view

    def _grouped(
        self, namespace: str, *, kinds: Sequence[str] | None = None, provider: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        self.require_ready()
        query = (
            "SELECT r.record_id, r.revision, r.revision_id, r.record_hash, r.payload_json, r.change, r.public_at_ms, "
            "r.public_basis, r.run_id, r.observed_at_ms FROM insurance_records n JOIN insurance_record_revisions r "
            "ON r.namespace=n.namespace AND r.record_id=n.record_id WHERE n.namespace=?"
        )
        params: list[Any] = [namespace]
        if kinds:
            query += " AND n.kind IN (" + ",".join("?" * len(kinds)) + ")"
            params.extend(kinds)
        if provider:
            query += " AND n.provider=?"
            params.append(provider)
        rows = self.conn.execute(query + " ORDER BY r.record_id, r.revision", params).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append(dict(zip(_REVISION_FIELDS, r)))
        return grouped

    @staticmethod
    def _known(
        revisions: Sequence[Mapping[str, Any]], public_cutoff_ms: int | None, acquired_by_ms: int | None
    ) -> list[Mapping[str, Any]]:
        return sorted(
            (
                r
                for r in revisions
                if (public_cutoff_ms is None or int(r["public_at_ms"]) <= public_cutoff_ms)
                and (acquired_by_ms is None or int(r["observed_at_ms"]) <= acquired_by_ms)
            ),
            key=_order_key,
        )

    def visible(
        self,
        namespace: str,
        *,
        kinds: Sequence[str] | None = None,
        provider: str | None = None,
        public_cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Each record's revision in force at the cutoffs (latest published, then latest acquired)."""
        views = []
        for _rid, revisions in sorted(self._grouped(namespace, kinds=kinds, provider=provider).items()):
            known = self._known(revisions, public_cutoff_ms, acquired_by_ms)
            if known:
                views.append(self._view(known[-1], len(known)))
        return views

    def history(
        self,
        namespace: str,
        rid: str,
        *,
        public_cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every revision known at the cutoffs, in publication order."""
        self.require_ready()
        revisions = [dict(zip(_REVISION_FIELDS, r)) for r in self._raw(namespace, rid)]
        if not revisions:
            raise InsuranceError("not_found", "the record is not on record in this namespace")
        return [self._view(r) for r in self._known(revisions, public_cutoff_ms, acquired_by_ms)]

    def _raw(self, namespace: str, rid: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT record_id, revision, revision_id, record_hash, payload_json, change, public_at_ms, public_basis, "
            "run_id, observed_at_ms FROM insurance_record_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY revision",
            [namespace, rid],
        ).fetchall()

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        self.require_ready()
        row = self.conn.execute(
            "SELECT record_id, revision, revision_id, record_hash, payload_json, change, public_at_ms, public_basis, "
            "run_id, observed_at_ms FROM insurance_record_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise InsuranceError("not_found", "revision is not on record")
        return self._view(dict(zip(_REVISION_FIELDS, row)))


class InsuranceProjector:
    """Source-pack runtime projector for ``noesis-insurance-record-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = InsuranceStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        declared = dict(source.get("insurance") or {})
        namespace = str(declared.get("namespace") or DEFAULT_NAMESPACE)
        observed = max(
            (int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
            default=self.store.now(),
        )
        items = [dict(item["insurance_record"]) for item in records if item.get("insurance_record")]
        for item in items:
            item["source"] = {**item["source"], "retrieved_at_ms": observed}
        receipt = dict(page_receipt or {})
        try:
            counts = self.store.apply(namespace, items, run_id=run_id, observed_at_ms=observed)
        except InsuranceError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc
        release = receipt.get("release")
        new_release = False
        if release:
            new_release = self.store.record_release(
                namespace,
                provider=str(declared.get("provider")),
                dataset=str(release.get("dataset")),
                release=str(release.get("release")),
                release_date=release.get("release_date"),
                document_sha256=receipt.get("file_sha256"),
                run_id=run_id,
                observed_at_ms=observed,
            )
        self.store.record_receipt(
            namespace,
            run_id=run_id,
            provider=str(declared.get("provider")),
            document=str(receipt.get("document") or ""),
            receipt={
                **receipt,
                "stored": {k: v for k, v in counts.items() if k != "changed"},
                "new_release": new_release,
            },
            observed_at_ms=observed,
        )
        return {k: v for k, v in counts.items() if k != "changed"}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


# ------------------------------------------------------------------ feature selection


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Market bundle's optional ``insurance`` feature is selected (default off; reads only)."""
    del namespace  # composition selection is deployment-wide
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='market'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "insurance" in ((plan.get("features") or {}).get("market") or [])


# ------------------------------------------------------------------ links (IN08)

LINK_BASES = ("published-identifier", "citation", "reviewed-candidate")
CANDIDATE_BASES = ("name-date-proximity",)
LINK_STATES = ("linked", "candidate", "accepted", "rejected")
PROXIMITY_DAYS = 7
NO_ATTRIBUTION = (
    "a published identifier or citation only; no loss is modelled or attributed to an insurer beyond what the "
    "publisher states"
)


def _table(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def _event_tokens(record: Mapping[str, Any]) -> set[str]:
    event = record.get("event") or {}
    tokens = {str(v).strip() for v in (event.get("identifiers") or {}).values() if str(v).strip()}
    if event.get("reference"):
        tokens.add(str(event["reference"]).strip())
    return {t for t in tokens if len(t) >= 6}


class InsuranceLinks:
    """Estimates linked to hazard events and insurer records by published identifier or citation (IN08)."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = InsuranceStore(conn, initialize=False, now=now)
        self.now = self.store.now

    def _upsert(
        self,
        namespace: str,
        view: Mapping[str, Any],
        *,
        target_kind: str,
        target_namespace: str,
        target_id: str,
        target_revision_id: str,
        basis: str,
        state: str,
        evidence: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, Any]:
        link_id = "insurance-link:" + digest(
            [namespace, view["record_id"], target_kind, target_namespace, target_id, basis]
        )[:24]
        row = self.conn.execute(
            "SELECT state, record_revision_id, target_revision_id, history_json FROM insurance_links "
            "WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            history = [{"state": state, "by": principal_id, "at_ms": self.now()}]
            self.conn.execute(
                "INSERT INTO insurance_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    link_id,
                    view["record_id"],
                    view["revision_id"],
                    target_kind,
                    target_namespace,
                    target_id,
                    target_revision_id,
                    basis,
                    state,
                    canonical(dict(evidence)),
                    principal_id,
                    self.now(),
                    canonical(history),
                ],
            )
            return {"link_id": link_id, "change": "created"}
        if (row[1], row[2]) != (view["revision_id"], target_revision_id) and row[0] in {"linked", "accepted"}:
            # Both sides are pinned; a new revision on either side re-points the link and keeps the history.
            history = json.loads(row[3]) + [
                {
                    "state": row[0],
                    "by": principal_id,
                    "at_ms": self.now(),
                    "change": "repinned",
                    "previous": {"record_revision_id": row[1], "target_revision_id": row[2]},
                }
            ]
            self.conn.execute(
                "UPDATE insurance_links SET record_revision_id=?, target_revision_id=?, evidence_json=?, "
                "history_json=? WHERE namespace=? AND link_id=?",
                [view["revision_id"], target_revision_id, canonical(dict(evidence)), canonical(history), namespace,
                 link_id],
            )
            return {"link_id": link_id, "change": "repinned"}
        return {"link_id": link_id, "change": None}

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        hazard_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Link current estimates to hazard events and insurer records; name/date matches become candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        estimates = self.store.visible(namespace, kinds=("catastrophe_loss_estimate",))
        reports = self.store.visible(namespace, kinds=("insurer_report",))
        changes: list[dict[str, Any]] = []
        hazards_state = self._hazard_events(hazard_namespace, scopes)
        for view in estimates:
            record = view["record"]
            tokens = _event_tokens(record)
            cites = set(record.get("cites") or [])
            for event in hazards_state["events"]:
                shared = sorted(tokens & set(event["tokens"]))
                if shared:
                    basis, state = "published-identifier", "linked"
                    evidence = {"identifier": shared[0], "estimate_states": sorted(tokens), "note": NO_ATTRIBUTION}
                elif event["source_url"] and event["source_url"] in cites:
                    basis, state = "citation", "linked"
                    evidence = {"cited_url": event["source_url"], "note": NO_ATTRIBUTION}
                elif self._near(record, event):
                    basis, state = "name-date-proximity", "candidate"
                    evidence = {
                        "estimate_event": record["event"]["name"],
                        "hazard_title": event["title"],
                        "estimate_begin_date": record["event"].get("begin_date"),
                        "hazard_event_time": event["event_time"],
                        "window_days": PROXIMITY_DAYS,
                        "note": "a name and date match is a reviewable candidate, never a link",
                    }
                else:
                    continue
                result = self._upsert(
                    namespace,
                    view,
                    target_kind="hazard-event",
                    target_namespace=str(hazard_namespace),
                    target_id=event["record_id"],
                    target_revision_id=event["revision_id"],
                    basis=basis,
                    state=state,
                    evidence=evidence,
                    principal_id=principal_id,
                )
                if result["change"]:
                    changes.append(result)
            stated = (record.get("insurer") or {}).get("lei")
            for report in reports:
                target = report["record"]
                if stated and target["insurer"].get("lei") == stated:
                    basis, evidence = "published-identifier", {"identifier": stated, "scheme": "lei"}
                elif target["document"]["url"] in cites:
                    basis, evidence = "citation", {"cited_url": target["document"]["url"]}
                else:
                    continue
                result = self._upsert(
                    namespace,
                    view,
                    target_kind="insurer-report",
                    target_namespace=namespace,
                    target_id=report["record_id"],
                    target_revision_id=report["revision_id"],
                    basis=basis,
                    state="linked",
                    evidence={**evidence, "note": NO_ATTRIBUTION},
                    principal_id=principal_id,
                )
                if result["change"]:
                    changes.append(result)
        return {
            "changes": changes,
            "hazard_events": {k: v for k, v in hazards_state.items() if k != "events"},
            "links": self.links(namespace, scopes=scopes),
        }

    @staticmethod
    def _near(record: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        name = fold(record["event"]["name"])
        words = {w for w in name.split() if len(w) >= 3 and w not in {"hurricane", "storm", "tropical", "the"}}
        haystack = fold(" ".join([event["title"], *event["names"]]))
        if not words or not words <= set(haystack.split()):
            return False
        begin, when = record["event"].get("begin_date"), event.get("event_time")
        if not begin or not when:
            return False
        return abs(day_ms(begin) - day_ms(str(when)[:10])) <= PROXIMITY_DAYS * DAY_MS

    def _hazard_events(self, hazard_namespace: str | None, scopes: set[str]) -> dict[str, Any]:
        if not hazard_namespace:
            return {"status": "not_requested", "reason": "no hazard namespace was given", "events": []}
        if not _table(self.conn, "hazard_record_revisions"):
            return {
                "status": "none_on_record",
                "reason": "no Natural Hazards records in this deployment",
                "events": [],
            }
        from src.kb.hazards_links import identifier_tokens
        from src.kb.hazards_store import HazardStore, HazardStoreError

        try:
            records = HazardStore(self.conn, initialize=False).records(
                hazard_namespace, scopes=scopes, record_type="hazard_event"
            )
        except HazardStoreError as exc:
            if exc.code == "unauthorized":
                raise InsuranceError("unauthorized", "knowledge:hazards:read and hazard namespace access") from exc
            raise
        events = []
        for item in records:
            content = item.get("content")
            if not content:
                continue
            ids = content.get("identifiers") or {}
            events.append(
                {
                    "record_id": item["record_id"],
                    "revision_id": item["revision_id"],
                    "title": content.get("title") or "",
                    "names": [str(ids["name"])] if ids.get("name") else [],
                    "event_time": content.get("event_time"),
                    "source_url": content.get("source_url"),
                    "tokens": identifier_tokens(content),
                }
            )
        return {
            "status": "available" if events else "none_on_record",
            "reason": None if events else "no hazard events in the namespace",
            "events": events,
        }

    def review(
        self,
        namespace: str,
        link_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Accept or reject a candidate; only an accepted candidate becomes a (reviewed) link."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise InsuranceError("invalid_decision", "accept or reject with a reason")
        link = self._link(namespace, link_id)
        if link["state"] != "candidate":
            raise InsuranceError("invalid_state", f"link is {link['state']}; only a candidate is reviewed")
        state = "accepted" if decision == "accept" else "rejected"
        history = link["history"] + [{"state": state, "by": principal_id, "reason": reason.strip(), "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE insurance_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link_id],
        )
        return self._link(namespace, link_id)

    def _link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, record_id, record_revision_id, target_kind, target_namespace, target_id, "
            "target_revision_id, basis, state, evidence_json, created_by, created_at_ms, history_json "
            "FROM insurance_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise InsuranceError("not_found", "link is not on record")
        link = dict(
            zip(
                (
                    "link_id",
                    "record_id",
                    "record_revision_id",
                    "target_kind",
                    "target_namespace",
                    "target_id",
                    "target_revision_id",
                    "basis",
                    "state",
                ),
                row[:9],
            )
        )
        link.update(
            {
                "evidence": json.loads(row[9]),
                "created_by": row[10],
                "created_at_ms": row[11],
                "history": json.loads(row[12]),
                "effective": row[8] in {"linked", "accepted"},
                "effective_basis": "reviewed-candidate" if row[8] == "accepted" else row[7],
            }
        )
        return link

    def links(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        record_id: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not _table(self.conn, "insurance_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM insurance_links WHERE namespace=? AND (? IS NULL OR record_id=?) "
            "AND (? IS NULL OR target_id=?) ORDER BY link_id",
            [namespace, record_id, record_id, target_id, target_id],
        ).fetchall()
        return [self._link(namespace, r[0]) for r in rows]


# ------------------------------------------------------------------ as-of answers (IN09)


def _figure_rows(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    record = view["record"]
    return [
        {
            **figure,
            "publisher": record["insurer"]["name"],
            "reporting_year": record["reporting_year"],
            "reporting_level": record["insurer"]["reporting_level"],
            "citation": {**view["citation"], "locator": {"template": figure["template"], "row": figure["row"],
                                                          "column": figure["column"], "page": figure.get("page")}},
        }
        for figure in record["figures"]
    ]


class InsuranceQueries:
    """Insurer, market and event answers as of a date, side by side, with every revision cited."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = InsuranceStore(conn, initialize=False)

    def _base(self, namespace: str, scopes: Iterable[str], as_of: Any, acquired_by_ms: int | None):
        authorize(namespace, set(scopes), READ_SCOPE)
        cut = cutoffs(as_of, acquired_by_ms)
        self.store.require_ready()
        return cut, dict(public_cutoff_ms=cut["publicly_available_by_ms"], acquired_by_ms=cut["acquired_by_ms"])

    def _history(self, namespace: str, view: Mapping[str, Any], bounds: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "revision_id": h["revision_id"],
                "publication_date": h["record"].get("publication_date"),
                "public_at_ms": h["public_at_ms"],
                "observed_at_ms": h["observed_at_ms"],
                "value": h["record"].get("value"),
                "range": h["record"].get("range"),
                "marker": h["record"].get("marker"),
                "release": h["record"].get("release"),
                "citation": h["citation"],
            }
            for h in self.store.history(namespace, view["record_id"], **bounds)
        ]

    def insurer(
        self,
        namespace: str,
        insurer: str,
        *,
        as_of: Any,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
        lei_namespace: str | None = None,
    ) -> dict[str, Any]:
        """An insurer's SFCR figures and insurer-level statistics as of a date; group and solo kept apart."""
        from src.domains.market.insurance_identity import InsuranceIdentity

        scopes = set(scopes)
        cut, bounds = self._base(namespace, scopes, as_of, acquired_by_ms)
        identity = InsuranceIdentity(self.conn, initialize=False)
        resolved = identity.resolve(namespace, insurer, scopes=scopes)
        own = set(resolved["record_keys"])
        reports, indicators, estimates, group_reports = [], [], [], []
        via = identity.group_links(
            namespace, resolved, scopes=scopes, lei_namespace=lei_namespace, as_of_ms=cut["publicly_available_by_ms"]
        )
        for view in self.store.visible(namespace, **bounds):
            record = view["record"]
            if not record.get("insurer"):
                continue
            key = identity.insurer_key(record["insurer"])
            if key in own:
                entry = {**view, "history": self._history(namespace, view, bounds)}
                if record["kind"] == "insurer_report":
                    reports.append({**entry, "figures": _figure_rows(view)})
                elif record["kind"] == "supervisory_indicator":
                    indicators.append(entry)
                elif record["kind"] == "catastrophe_loss_estimate":
                    estimates.append(entry)
            elif key in via["group_keys"] and record["kind"] == "insurer_report":
                group_reports.append(
                    {
                        **view,
                        "figures": _figure_rows(view),
                        "attribution": "group report shown beside the undertaking through a recorded ownership link; "
                        "its figures are the group's, never the undertaking's",
                        "ownership_link": via["links"][key],
                    }
                )
        found = bool(reports or indicators or estimates)
        return {
            "insurer": resolved,
            "cutoffs": cut,
            "status": "on_record" if found else "none_on_record",
            "reports": reports,
            "indicators": indicators,
            "loss_estimates": estimates,
            "group_reports_via_ownership_link": group_reports,
            "coverage": coverage(),
            "notice": NOTICE,
        }

    def market(
        self,
        namespace: str,
        country: str,
        *,
        as_of: Any,
        scopes: Iterable[str],
        indicator: str | None = None,
        line_of_business: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Supervisory figures for a market as of a date, per publisher and definition; nothing reconciled."""
        cut, bounds = self._base(namespace, scopes, as_of, acquired_by_ms)
        country = str(country or "").strip().upper()
        rows = []
        for view in self.store.visible(namespace, kinds=("supervisory_indicator",), **bounds):
            record = view["record"]
            dims = record["dimensions"]
            if (dims.get("country") or "").upper() != country:
                continue
            if indicator and record["indicator"] != indicator:
                continue
            if line_of_business and dims.get("line_of_business") != line_of_business:
                continue
            rows.append(
                {
                    "publisher": record["publisher"],
                    "dataset": record["dataset"],
                    "indicator": record["indicator"],
                    "indicator_label": record["indicator_label"],
                    "definition": record.get("definition"),
                    "dimensions": dims,
                    "period": record["period"],
                    "value": record["value"],
                    "marker": record["marker"],
                    "unit": record["unit"],
                    "currency": record["currency"],
                    "release": record["release"],
                    "citation": view["citation"],
                    "history": self._history(namespace, view, bounds),
                }
            )
        rows.sort(key=lambda r: (r["publisher"], r["indicator"], canonical(r["dimensions"]), canonical(r["period"])))
        by_publisher: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_publisher.setdefault(row["publisher"], []).append(row)
        return {
            "market": country,
            "cutoffs": cut,
            "status": "on_record" if rows else "none_on_record",
            "by_publisher": by_publisher,
            "coverage": coverage(),
            "notice": NOTICE,
        }

    def event(
        self,
        namespace: str,
        event: str,
        *,
        as_of: Any,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Every publisher's estimate series for an event, listed separately with its revisions as of a date.

        ``event`` is the event name or reference as a publisher published it, a published identifier (GLIDE,
        storm ID), or a Natural Hazards record id reached through an effective link.
        """
        scopes = set(scopes)
        cut, bounds = self._base(namespace, scopes, as_of, acquired_by_ms)
        wanted = fold(event)
        linked = set()
        if str(event).startswith("hazard:"):
            linked = {
                link["record_id"]
                for link in InsuranceLinks(self.conn).links(namespace, scopes=scopes, target_id=str(event))
                if link["effective"]
            }
        series = []
        for view in self.store.visible(namespace, kinds=("catastrophe_loss_estimate",), **bounds):
            record = view["record"]
            names = {fold(record["event"]["name"]), fold(record["event"].get("reference"))}
            names |= {fold(v) for v in record["event"]["identifiers"].values()}
            if view["record_id"] not in linked and wanted not in names:
                continue
            series.append(
                {
                    "publisher": record["publisher"],
                    "provider": record["source"]["provider"],
                    "event_as_published": record["event"],
                    "estimate_type": record["estimate_type"],
                    "measure": record["measure"],
                    "in_force": {
                        "value": record["value"],
                        "range": record["range"],
                        "as_published": record.get("as_published"),
                        "unit": record["unit"],
                        "currency": record["currency"],
                        "publication_date": record["publication_date"],
                        "citation": view["citation"],
                    },
                    "revisions": self._history(namespace, view, bounds),
                    "links": [
                        link
                        for link in InsuranceLinks(self.conn).links(namespace, scopes=scopes,
                                                                    record_id=view["record_id"])
                        if link["effective"] or link["state"] == "candidate"
                    ],
                }
            )
        series.sort(key=lambda s: (s["publisher"], s["estimate_type"], s["measure"]))
        return {
            "event": event,
            "cutoffs": cut,
            "status": "on_record" if series else "none_on_record",
            "series": series,
            "merged": False,
            "coverage": coverage(),
            "notice": NOTICE + " Estimates from different publishers are listed separately and never merged.",
        }

    def revision_history(
        self, namespace: str, rid: str, *, scopes: Iterable[str], as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready()
        cut = cutoffs(as_of, acquired_by_ms) if as_of is not None else None
        history = self.store.history(
            namespace,
            rid,
            public_cutoff_ms=cut["publicly_available_by_ms"] if cut else None,
            acquired_by_ms=acquired_by_ms,
        )
        return {
            "record_id": rid,
            "cutoffs": cut,
            "revisions": history,
            "order": "publication time, then acquisition order",
            "notice": NOTICE,
        }

"""BaFin capital-market notices: versioned records and their store (#2106, BF02).

One record owner for the Market pack's optional ``bafin-notices`` feature.
Every record is *what one source publishes*, with its event time, its
publication time and its corrections explicit:

* ``voting_rights_notification`` (WpHG §§ 33 ff.): issuer, notifier, the chain
  of controlled undertakings **in the order stated**, thresholds touched and the
  percentages split by § 33 (shares), § 38 (instruments) and § 39 (total). They
  are never summed across notifiers.
* ``managers_transaction`` (Art. 19 MAR): person and role, issuer, instrument,
  nature, per-trade price, volume and currency, the aggregate as published,
  transaction, notification and publication dates. The person's name lives in
  a separate person table under a pseudonymous, issuer-scoped reference, so it
  can be withdrawn without rewriting revisions (see the BF01 retention decision).
* ``net_short_position`` (Art. 11 SSR): holder, issuer, position, position and
  publication dates. A published value below 0.5 % is a ``publication_ended``
  marker. Nothing reads it as 0 %.
* ``bafin_warning`` / ``bafin_measure``: named entity *strings*, kind, the legal
  basis the text cites and the publication date.
* ``authorised_entity``: BaFin ID, name, licences with the start and end dates
  published.

Store rules:

* a stable ``notice_id`` per namespace, provider and source identifier;
* re-acquiring unchanged content adds nothing. Content is compared as a
  normalised, source-independent representation (no row numbers, file digests
  or retrieval times), and only against the **current** revision, so a
  reversion is a new revision;
* "current" follows the source's own date (``source_as_of``) and falls back to
  observation order. A late-arriving older export is kept as history and never
  becomes current;
* corrections (the BaFin "Korrektur" flag) are separate notices linked to the
  notice they correct. Chains are computed at read time, so arrival order does
  not matter. Queries show one notice per chain: the latest published by the
  cutoff;
* removal from a complete listing is a listing observation
  (``no_longer_listed``) with the date observed. Nothing is deleted.

Point-in-time reads follow the Market ``public_and_acquired`` policy
(:mod:`src.domains.market.asof`): a revision is visible when its publication
clock is at or before the public cutoff and it was acquired by the acquisition
cutoff. A stated publication *date* counts as the end of that day (UTC); a
missing publication date falls back to the first observation time, never to
the event date.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CONTRACT = "noesis-bafin-notice-v1"
READ_SCOPE = "market:bafin:read"
WRITE_SCOPE = "market:bafin:write"
DEFAULT_NAMESPACE = "market-bafin"
KINDS = (
    "voting_rights_notification",
    "managers_transaction",
    "net_short_position",
    "bafin_warning",
    "bafin_measure",
    "authorised_entity",
)
PROVIDERS = {
    "bafin-voting-rights": ("voting_rights_notification",),
    "bafin-managers-transactions": ("managers_transaction",),
    "bundesanzeiger-short-positions": ("net_short_position",),
    "bafin-company-database": ("authorised_entity",),
    "bafin-warnings-measures": ("bafin_warning", "bafin_measure"),
}
PARTY_KINDS = ("natural_person", "legal_person", "unknown")
# WpHG § 33 para. 1 thresholds (shares); § 38 and § 39 start at 5 %.
THRESHOLDS = {
    "s33": ("3", "5", "10", "15", "20", "25", "30", "50", "75"),
    "s38": ("5", "10", "15", "20", "25", "30", "50", "75"),
    "s39": ("5", "10", "15", "20", "25", "30", "50", "75"),
}
SHORT_PUBLICATION_THRESHOLD = Decimal("0.5")
RETENTION_POLICIES = ("retain", "withdraw-person-data")
LISTING_STATES = ("no_longer_listed", "listed_again")
DAY_MS = 86_400_000
WITHDRAWN_NAME = "[withdrawn: no longer listed by the source]"
# Cited provisions per notice kind (parsed with src.kb.legal_citations in the adapters' tests).
LEGAL_BASIS = {
    "voting_rights_notification": [
        "§§ 33 ff. WpHG",
        "§ 38 WpHG",
        "§ 39 WpHG",
        "§ 40 WpHG",
    ],
    "managers_transaction": ["Art. 19 der Verordnung (EU) Nr. 596/2014"],
    "net_short_position": ["Art. 11 der Verordnung (EU) Nr. 236/2012"],
}
NOTICE = (
    "Notices as the source publishes them, visible only once published. No investment advice, signal, "
    "trading recommendation or sentiment; holdings are never summed across notifiers and positions below a "
    "publication threshold are never inferred."
)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts/schemas/jsonschema/noesis-bafin-notice-v1.json"
)

_DDL = """
CREATE TABLE IF NOT EXISTS bafin_notices (
  namespace TEXT NOT NULL, notice_id TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL,
  source_id TEXT NOT NULL, issuer_isin TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, notice_id)
);
CREATE TABLE IF NOT EXISTS bafin_notice_revisions (
  namespace TEXT NOT NULL, notice_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, change TEXT NOT NULL, source_as_of TEXT,
  run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, notice_id, revision)
);
CREATE TABLE IF NOT EXISTS bafin_listing_observations (
  namespace TEXT NOT NULL, notice_id TEXT NOT NULL, state TEXT NOT NULL, observed_on TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, scope_json TEXT NOT NULL,
  PRIMARY KEY(namespace, notice_id, observed_at_ms, state)
);
CREATE TABLE IF NOT EXISTS bafin_persons (
  namespace TEXT NOT NULL, person_ref TEXT NOT NULL, issuer_key TEXT NOT NULL, name TEXT,
  withdrawn_at_ms BIGINT, withdrawn_reason TEXT, PRIMARY KEY(namespace, person_ref)
);
CREATE TABLE IF NOT EXISTS bafin_page_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, document TEXT NOT NULL,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, provider, document)
);
"""
TABLES = (
    "bafin_notices",
    "bafin_notice_revisions",
    "bafin_listing_observations",
    "bafin_persons",
    "bafin_page_receipts",
)


class BafinError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise BafinError(
            "unauthorized", f"{required} and namespace access are required"
        )


# ------------------------------------------------------------------ identifiers


def normalize_isin(value: Any) -> str:
    """Upper case without spaces, dots or hyphens; the same form on both sides of every match."""
    return re.sub(r"[\s.\-]", "", str(value or "")).upper()


def isin_valid(value: Any) -> bool:
    """ISO 6166: two letters, nine alphanumerics and a Luhn check digit over the letter expansion."""
    isin = normalize_isin(value)
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
        return False
    digits = "".join(str(int(ch, 36)) for ch in isin[:-1])
    total = 0
    for index, ch in enumerate(reversed(digits)):
        n = int(ch)
        if index % 2 == 0:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return (10 - total % 10) % 10 == int(isin[-1])


def normalize_lei(value: Any) -> str:
    return re.sub(r"[\s.\-]", "", str(value or "")).upper()


def lei_valid(value: Any) -> bool:
    from src.ingestion.lei_sources import lei_valid as check

    return check(normalize_lei(value))


def normalize_bafin_id(value: Any) -> str:
    """BaFin IDs are numeric: digits only, leading zeros removed (both sides of every match)."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.lstrip("0") or ("0" if digits else "")


_LEGAL_FORMS = (
    "ag",
    "se",
    "kgaa",
    "gmbh",
    "mbh",
    "kg",
    "ohg",
    "ev",
    "eg",
    "ek",
    "ug",
    "haftungsbeschrankt",
    "co",
    "plc",
    "ltd",
    "limited",
    "llc",
    "inc",
    "corp",
    "corporation",
    "sa",
    "sas",
    "nv",
    "bv",
    "spa",
    "sarl",
    "lp",
    "llp",
    "company",
)
_TITLES = ("dr", "prof", "professor", "dipl", "ing", "herr", "frau", "mr", "mrs", "ms")


def fold(text: Any) -> str:
    """Case-folded ASCII text with German umlauts transliterated (ä → ae, ß → ss)."""
    value = str(text or "").casefold()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(source, target)
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def party_key(name: Any, *, kind: str = "unknown") -> str:
    """The one equivalence used for party strings by lookups, identity candidates and monitors.

    Transliterated, case-folded words without punctuation. Trailing legal forms
    are dropped for organisations and leading titles for natural persons, so
    ``Musterwerke AG`` equals ``MUSTERWERKE Aktiengesellschaft``, but no other
    similarity is applied.
    """
    text = (
        fold(name)
        .replace("aktiengesellschaft", "ag")
        .replace("gesellschaft mit beschraenkter haftung", "gmbh")
    )
    text = re.sub(r"&", " ", text)
    text = re.sub(r"[.'’]", "", text)  # S.A. -> sa, e.K. -> ek, Co. -> co
    text = re.sub(r"[,()/]", " ", text)
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    if kind == "natural_person":
        while tokens and tokens[0] in _TITLES:
            tokens = tokens[1:]
    else:
        while len(tokens) > 1 and tokens[-1] in _LEGAL_FORMS:
            tokens = tokens[:-1]
    return " ".join(tokens)


# ------------------------------------------------------------------ values


def iso_day(value: Any) -> str | None:
    """A stated day as ISO ``YYYY-MM-DD``; German ``dd.mm.yyyy`` and RFC 822 dates are accepted.

    Missing markers (empty, ``-``, ``n/a``) are absent: ``None``, never the text "None".
    """
    text = str(value or "").strip()
    if text.casefold() in {"", "-", "–", "n/a", "k.a.", "none", "null"}:
        return None
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return (
                datetime.strptime(text[:10] if pattern == "%Y-%m-%d" else text, pattern)
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).astimezone(UTC).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T.*", text)
    if match:
        return date.fromisoformat(match.group(1)).isoformat()
    raise BafinError("invalid_date", f"unparseable date: {text[:40]}")


def decimal_text(value: Any, *, decimal: str | None = None) -> str | None:
    """A stated decimal as a canonical string (``3,01`` → ``3.01``); absent markers are ``None``.

    ``decimal`` is the separator the source declares (``,`` for German exports, so
    ``1.000`` is one thousand). Without a declaration the last separator present
    is the decimal one.
    """
    text = (
        str(value or "")
        .strip()
        .replace("%", "")
        .replace(" ", "")
        .replace(" ", "")
        .strip()
    )
    if text.casefold() in {"", "-", "–", "n/a", "k.a.", "none", "null"}:
        return None
    if decimal is None:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
    thousands = "." if decimal == "," else ","
    text = text.replace(thousands, "").replace(decimal, ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise BafinError(
            "invalid_number", f"unparseable number: {value!r}"[:80]
        ) from exc
    if not number.is_finite():
        raise BafinError("invalid_number", "numbers must be finite")
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
        raise BafinError(
            "invalid_request", "as_of is a date (YYYY-MM-DD) or epoch milliseconds"
        )
    if isinstance(as_of, int):
        public = as_of
        label = observed_day(as_of)
    else:
        label = iso_day(as_of)
        if label is None:
            raise BafinError("invalid_request", "as_of is required")
        public = end_of_day_ms(label)
    if acquired_by_ms is not None:
        if (
            isinstance(acquired_by_ms, bool)
            or not isinstance(acquired_by_ms, int)
            or acquired_by_ms < 0
        ):
            raise BafinError(
                "invalid_request", "acquired_by_ms is nonnegative epoch milliseconds"
            )
    return {
        "as_of": label,
        "publicly_available_by_ms": public,
        "acquired_by_ms": acquired_by_ms,
        "availability_policy": "public_and_acquired",
        "note": "a stated publication date counts as published at the end of that day (UTC); a missing "
        "publication date falls back to the first observation time",
    }


# ------------------------------------------------------------------ validation

_ISSUER = {"name", "isin", "isin_stated", "lei"}
_PARTY = {"name", "kind", "seat", "country", "lei", "ref", "role", "closely_associated"}
_SOURCE = {
    "provider",
    "source_id",
    "source_id_basis",
    "url",
    "locator",
    "retrieved_at_ms",
    "file_sha256",
    "document",
    "evidence_origin",
}
_COMMON = {
    "contract",
    "kind",
    "source",
    "event_date",
    "notification_date",
    "publication_date",
    "correction_of",
    "withdrawn",
    "unknowns",
    "native",
    "legal_basis",
}
_KIND_FIELDS = {
    "voting_rights_notification": {
        "issuer",
        "notifier",
        "chain",
        "thresholds",
        "percentages",
        "previous_percentages",
        "reason",
    },
    "managers_transaction": {
        "issuer",
        "person",
        "instrument",
        "nature",
        "trades",
        "aggregate",
        "venue",
        "transaction_date",
        "amendment",
    },
    "net_short_position": {
        "issuer",
        "holder",
        "position_pct",
        "position_date",
        "publication_ended",
    },
    "bafin_warning": {"title", "named_entities", "category", "url", "summary"},
    "bafin_measure": {"title", "named_entities", "category", "url", "summary"},
    "authorised_entity": {
        "bafin_id",
        "name",
        "place",
        "country",
        "lei",
        "licences",
        "entity_type",
    },
}
_PCT_KEYS = ("s33", "s38_1_1", "s38_1_2", "s38", "s39")


def _fail(message: str) -> None:
    raise BafinError("invalid_notice", message)


def _text(value: Any, field: str, *, optional: bool = False, limit: int = 4000) -> Any:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value.strip()


def _day(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail(f"{field} must be an ISO date")
    date.fromisoformat(value)
    return value


def _pct(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} is a decimal string, never a float")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal")
    if not number.is_finite() or number < 0 or number > 100:
        _fail(f"{field} must be a percentage between 0 and 100")
    return value


def _issuer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _ISSUER:
        _fail("issuer uses name/isin/isin_stated/lei")
    if value.get("isin") is not None and not isin_valid(value["isin"]):
        _fail(
            "issuer.isin must carry valid check digits (unvalidated text goes to isin_stated)"
        )
    if value.get("lei") is not None and not lei_valid(value["lei"]):
        _fail("issuer.lei must carry valid check digits")
    if not (value.get("name") or value.get("isin") or value.get("isin_stated")):
        _fail("an issuer is named or identified")
    return value


def _party(value: Any, field: str, *, named: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _PARTY:
        _fail(f"{field} uses " + "/".join(sorted(_PARTY)))
    if value.get("kind", "unknown") not in PARTY_KINDS:
        _fail(f"{field}.kind must be one of {PARTY_KINDS}")
    if named and not (value.get("name") or value.get("ref")):
        _fail(f"{field} carries a name or a person reference")
    return value


def compute_unknowns(notice: Mapping[str, Any]) -> list[str]:
    kind, unknowns = notice["kind"], []
    if notice.get("publication_date") is None and kind != "authorised_entity":
        unknowns.append("publication_date")
    issuer = notice.get("issuer")
    if issuer is not None:
        if not issuer.get("isin"):
            unknowns.append("issuer.isin")
        if not issuer.get("lei"):
            unknowns.append("issuer.lei")
    if kind == "voting_rights_notification":
        if notice.get("event_date") is None:
            unknowns.append("event_date")
        if not notice.get("chain"):
            unknowns.append("chain")
        if all(notice["percentages"].get(k) is None for k in _PCT_KEYS):
            unknowns.append("percentages")
    elif kind == "managers_transaction":
        if notice.get("transaction_date") is None:
            unknowns.append("transaction_date")
        if not notice.get("trades"):
            unknowns.append("trades")
    elif kind == "net_short_position":
        if notice.get("position_pct") is None:
            unknowns.append("position_pct")
    elif kind in {"bafin_warning", "bafin_measure"}:
        if not notice.get("named_entities"):
            unknowns.append("named_entities")
        if not notice.get("legal_basis"):
            unknowns.append("legal_basis")
    elif kind == "authorised_entity":
        if not notice.get("licences"):
            unknowns.append("licences")
    return sorted(set(unknowns))


def validate_notice(notice: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(notice, dict):
        _fail("a notice is an object")
    if notice.get("contract") != CONTRACT:
        raise BafinError("schema_drift", "unsupported notice contract")
    kind = notice.get("kind")
    if kind not in KINDS:
        _fail(f"kind must be one of {KINDS}")
    extra = set(notice) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    result = json.loads(canonical(notice))
    source = result.get("source")
    if not isinstance(source, dict) or set(source) - _SOURCE:
        _fail("source uses " + "/".join(sorted(_SOURCE)))
    if (
        source.get("provider") not in PROVIDERS
        or kind not in PROVIDERS[source["provider"]]
    ):
        _fail("source.provider must be a BaFin notice provider publishing this kind")
    _text(source.get("source_id"), "source.source_id", limit=500)
    if source.get("source_id_basis") not in {"stated", "derived"}:
        _fail("source.source_id_basis is stated or derived")
    if source.get("url") is not None and not str(source["url"]).startswith("https://"):
        _fail("source.url must be an HTTPS locator")
    for field in ("event_date", "notification_date", "publication_date"):
        _day(result.get(field), field)
    correction = result.get("correction_of")
    if correction is not None and (
        not isinstance(correction, dict)
        or set(correction) - {"source_id", "publication_date", "stated"}
        or not (
            correction.get("source_id")
            or correction.get("publication_date")
            or correction.get("stated")
        )
    ):
        _fail("correction_of uses source_id/publication_date/stated")
    if correction is not None:
        _day(correction.get("publication_date"), "correction_of.publication_date")
    if result.get("withdrawn") not in (None, True, False):
        _fail("withdrawn is a boolean")
    if kind == "voting_rights_notification":
        _issuer(result.get("issuer"))
        _party(result.get("notifier"), "notifier")
        chain = result.get("chain") or []
        if not isinstance(chain, list):
            _fail("chain is a list in the order stated")
        for index, member in enumerate(chain):
            if not isinstance(member, dict) or set(member) - {
                "position",
                "name",
                "voting_rights_pct",
                "instruments_pct",
                "total_pct",
            }:
                _fail(
                    "chain members use position/name/voting_rights_pct/instruments_pct/total_pct"
                )
            if member.get("position") != index + 1:
                _fail("chain positions follow the stated order from 1")
            _text(member.get("name"), "chain.name", limit=1000)
            for key in ("voting_rights_pct", "instruments_pct", "total_pct"):
                _pct(member.get(key), f"chain.{key}")
        percentages = result.get("percentages")
        if not isinstance(percentages, dict) or set(percentages) - set(_PCT_KEYS):
            _fail("percentages use s33/s38_1_1/s38_1_2/s38/s39")
        for key in _PCT_KEYS:
            _pct(percentages.get(key), f"percentages.{key}")
        previous = result.get("previous_percentages")
        if previous is not None:
            if not isinstance(previous, dict) or set(previous) - set(_PCT_KEYS):
                _fail("previous_percentages use s33/s38/s39")
            for key in _PCT_KEYS:
                _pct(previous.get(key), f"previous_percentages.{key}")
        thresholds = result.get("thresholds") or []
        if not isinstance(thresholds, list) or any(
            not isinstance(t, str) for t in thresholds
        ):
            _fail("thresholds are the stated threshold strings")
    elif kind == "managers_transaction":
        _issuer(result.get("issuer"))
        person = _party(result.get("person"), "person")
        if person.get("name") is not None:
            _fail(
                "a manager's name is stored in the person table; the notice holds its reference"
            )
        instrument = result.get("instrument")
        if not isinstance(instrument, dict) or set(instrument) - {
            "isin",
            "isin_stated",
            "type",
        }:
            _fail("instrument uses isin/isin_stated/type")
        if instrument.get("isin") is not None and not isin_valid(instrument["isin"]):
            _fail("instrument.isin must carry valid check digits")
        trades = result.get("trades") or []
        if not isinstance(trades, list):
            _fail("trades is a list")
        for trade in trades:
            if not isinstance(trade, dict) or set(trade) - {
                "price",
                "volume",
                "currency",
                "date",
                "venue",
                "row",
            }:
                _fail("trades use price/volume/currency/date/venue/row")
            for key in ("price", "volume"):
                if trade.get(key) is not None and not isinstance(trade[key], str):
                    _fail(f"trade.{key} is a decimal string as published")
            _day(trade.get("date"), "trade.date")
        aggregate = result.get("aggregate")
        if aggregate is not None and (
            not isinstance(aggregate, dict)
            or set(aggregate) - {"price", "volume", "currency"}
        ):
            _fail("aggregate uses price/volume/currency as published")
        _day(result.get("transaction_date"), "transaction_date")
    elif kind == "net_short_position":
        _issuer(result.get("issuer"))
        _party(result.get("holder"), "holder")
        _pct(result.get("position_pct"), "position_pct")
        _day(result.get("position_date"), "position_date")
        if result.get("position_date") is None:
            _fail("a net short position states its position date")
        if result.get("publication_ended") not in (None, True, False):
            _fail("publication_ended is a boolean")
    elif kind in {"bafin_warning", "bafin_measure"}:
        _text(result.get("title"), "title", limit=2000)
        names = result.get("named_entities") or []
        if not isinstance(names, list) or any(
            not isinstance(n, str) or not n.strip() for n in names
        ):
            _fail("named_entities are source strings")
    elif kind == "authorised_entity":
        if not normalize_bafin_id(result.get("bafin_id")):
            _fail("an authorised entity carries its BaFin ID")
        result["bafin_id"] = normalize_bafin_id(result["bafin_id"])
        _text(result.get("name"), "name", limit=2000)
        if result.get("lei") is not None and not lei_valid(result["lei"]):
            _fail("lei must carry valid check digits")
        for licence in result.get("licences") or []:
            if not isinstance(licence, dict) or set(licence) - {
                "type",
                "start",
                "end",
                "status",
            }:
                _fail("licences use type/start/end/status")
            _text(licence.get("type"), "licence.type", limit=500)
            _day(licence.get("start"), "licence.start")
            _day(licence.get("end"), "licence.end")
    legal_basis = result.get("legal_basis")
    if legal_basis is not None and (
        not isinstance(legal_basis, list)
        or any(not isinstance(b, dict) or not b.get("raw") for b in legal_basis)
    ):
        _fail("legal_basis lists parsed citations with their raw text")
    result["unknowns"] = compute_unknowns(result)
    return result


def semantic(notice: Mapping[str, Any]) -> dict[str, Any]:
    """The source-independent content a revision is compared by (no locators, digests or clocks)."""
    body = {k: v for k, v in notice.items() if k != "unknowns"}
    source = dict(body.get("source") or {})
    body["source"] = {k: source.get(k) for k in ("provider", "source_id")}
    if isinstance(body.get("trades"), list):
        body["trades"] = [
            {k: v for k, v in t.items() if k != "row"} for t in body["trades"]
        ]
    return body


def notice_id(namespace: str, provider: str, source_id: str) -> str:
    return "bafin-notice:" + digest([namespace, provider, source_id])[:24]


def person_ref(issuer_key: str, source_id: str, name: str) -> str:
    """Pseudonymous reference for the person one notice names, scoped to that notice and its issuer.

    Withdrawing the person data of one notice the source no longer lists never
    touches another notice naming the same person; grouping a person's notices
    is done by name within the issuer (:func:`party_key`), never by this reference.
    """
    return (
        "bafin-person:"
        + digest([issuer_key, source_id, party_key(name, kind="natural_person")])[:20]
    )


def issuer_key(issuer: Mapping[str, Any]) -> str:
    return issuer.get("isin") or "name:" + party_key(
        issuer.get("name") or issuer.get("isin_stated")
    )


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any
) -> list[dict[str, Any]]:
    """Register the notice contract as a schema module in the shared registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "bafin-notice",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": schema(),
        "owner": "market.bafin",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {
            "kind": "imported",
            "source": f"contracts/schemas/jsonschema/{CONTRACT}.json",
        },
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            "bafin-schema:bafin-notice:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]


# ------------------------------------------------------------------ store


def _order_key(row: Mapping[str, Any]) -> tuple:
    """Source order: the source's own date, else the observation date; then observation time."""
    stated = row.get("source_as_of") or observed_day(row["observed_at_ms"])
    return (stated, int(row["observed_at_ms"]), int(row["revision"]))


class BafinNoticeStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # -------------------------------------------------------------- readiness

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
            raise BafinError(
                "not_ready",
                "no BaFin notice source has run yet; run the bafin-capital-market-notices source pack first",
            )

    # -------------------------------------------------------------- writes

    def _person(
        self, namespace: str, issuer: Mapping[str, Any], source_id: str, name: str
    ) -> str:
        key = issuer_key(issuer)
        ref = person_ref(key, str(source_id), name)
        row = self.conn.execute(
            "SELECT name, withdrawn_at_ms FROM bafin_persons WHERE namespace=? AND person_ref=?",
            [namespace, ref],
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO bafin_persons VALUES (?,?,?,?,NULL,NULL)",
                [namespace, ref, key, name.strip()],
            )
        elif row[1] is not None or row[0] is None:
            # Published again after a withdrawal: the source lists the person again.
            self.conn.execute(
                "UPDATE bafin_persons SET name=?, withdrawn_at_ms=NULL, withdrawn_reason=NULL "
                "WHERE namespace=? AND person_ref=?",
                [name.strip(), namespace, ref],
            )
        return ref

    def _revisions(self, namespace: str, nid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision, revision_id, record_hash, change, source_as_of, run_id, observed_at_ms "
            "FROM bafin_notice_revisions WHERE namespace=? AND notice_id=? ORDER BY revision",
            [namespace, nid],
        ).fetchall()
        return [
            dict(
                zip(
                    (
                        "revision",
                        "revision_id",
                        "record_hash",
                        "change",
                        "source_as_of",
                        "run_id",
                        "observed_at_ms",
                    ),
                    r,
                )
            )
            for r in rows
        ]

    def apply(
        self,
        namespace: str,
        notices: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        observed_at_ms: int,
        source_as_of: str | None = None,
    ) -> dict[str, Any]:
        """Validate and append notices; unchanged content adds nothing.

        ``source_as_of`` is the date the source states for the document the
        notices came from (``None`` when it states none; observation order then
        decides what is current).
        """
        counts = {"inserted": 0, "revised": 0, "unchanged": 0, "history": 0}
        changed: list[str] = []
        source_as_of = iso_day(source_as_of) if source_as_of else None
        prepared = []
        for item in notices:
            item = dict(item)
            person = (
                dict(item.get("person") or {})
                if item.get("kind") == "managers_transaction"
                else None
            )
            prepared.append((item, person))
        self.conn.execute("BEGIN")
        try:
            for item, person in prepared:
                if person is not None and person.get("name"):
                    name = str(person.pop("name"))
                    person["ref"] = self._person(
                        namespace,
                        item.get("issuer") or {},
                        (item.get("source") or {}).get("source_id") or "",
                        name,
                    )
                    item["person"] = person
                notice = validate_notice(item)
                provider, source_id = (
                    notice["source"]["provider"],
                    notice["source"]["source_id"],
                )
                nid = notice_id(namespace, provider, source_id)
                record_hash = digest(semantic(notice))
                revisions = self._revisions(namespace, nid)
                incoming = {
                    "source_as_of": source_as_of,
                    "observed_at_ms": int(observed_at_ms),
                    "revision": (revisions[-1]["revision"] + 1) if revisions else 1,
                }
                if revisions:
                    current = max(revisions, key=_order_key)
                    if current["record_hash"] == record_hash:
                        counts["unchanged"] += 1
                        continue
                    older = _order_key(incoming) < _order_key(current)
                    if older and any(
                        r["record_hash"] == record_hash for r in revisions
                    ):
                        counts["unchanged"] += (
                            1  # a late copy of an older state already on record
                        )
                        continue
                    change = "history" if older else "revised"
                else:
                    change = "new"
                revision = incoming["revision"]
                revision_id = "bafin-rev:" + digest([nid, revision, record_hash])[:24]
                self.conn.execute(
                    "INSERT INTO bafin_notice_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        nid,
                        revision,
                        revision_id,
                        record_hash,
                        canonical(notice),
                        change,
                        source_as_of,
                        run_id,
                        int(observed_at_ms),
                    ],
                )
                if not revisions:
                    issuer = notice.get("issuer") or {}
                    self.conn.execute(
                        "INSERT INTO bafin_notices VALUES (?,?,?,?,?,?,?)",
                        [
                            namespace,
                            nid,
                            notice["kind"],
                            provider,
                            source_id,
                            issuer.get("isin"),
                            int(observed_at_ms),
                        ],
                    )
                    counts["inserted"] += 1
                else:
                    counts["history" if change == "history" else "revised"] += 1
                if change != "history":
                    changed.append(nid)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**counts, "changed": changed}

    def observe_listing(
        self,
        namespace: str,
        *,
        provider: str,
        scope: Mapping[str, Any],
        seen: Iterable[str],
        observed_at_ms: int,
        run_id: str,
        retention_policy: str = "retain",
    ) -> dict[str, Any]:
        """Record what a *complete* listing no longer (or again) shows.

        ``scope`` bounds the listing: ``{"issuers": [...]}`` (ISINs),
        ``{"source_ids": [...]}`` (e.g. declared BaFin IDs) or ``{"all": true}``.
        ``seen`` holds the source identifiers the listing showed; for net
        short positions it holds ``holder|isin`` pairs, and only
        the latest position of a pair that disappeared is marked. Under the
        ``withdraw-person-data`` policy a managers' transaction that leaves the
        listing has its person's name erased from the person table.
        """
        if provider not in PROVIDERS:
            raise BafinError("invalid_request", "unknown provider")
        if retention_policy not in RETENTION_POLICIES:
            raise BafinError(
                "invalid_request", f"retention policy is one of {RETENTION_POLICIES}"
            )
        seen = set(seen)
        issuers = {normalize_isin(i) for i in scope.get("issuers") or []}
        rows = self.conn.execute(
            "SELECT notice_id, source_id, issuer_isin, kind FROM bafin_notices WHERE namespace=? AND provider=? "
            "ORDER BY notice_id",
            [namespace, provider],
        ).fetchall()
        source_ids = set(scope.get("source_ids") or [])
        in_scope = [
            r
            for r in rows
            if scope.get("all") or (r[2] and r[2] in issuers) or r[1] in source_ids
        ]
        marks = {"no_longer_listed": [], "listed_again": []}
        if provider == "bundesanzeiger-short-positions":
            latest: dict[str, tuple[str, str]] = {}
            for nid, _, isin, _ in in_scope:
                view = self._current_payload(namespace, nid)
                if view is None:
                    continue
                pair = f"{party_key(view['holder'].get('name'))}|{isin}"
                if pair not in latest or view["position_date"] > latest[pair][1]:
                    latest[pair] = (nid, view["position_date"])
            candidates = [(nid, pair in seen) for pair, (nid, _) in latest.items()]
        else:
            candidates = [(nid, source_id in seen) for nid, source_id, _, _ in in_scope]
        self.conn.execute("BEGIN")
        try:
            for nid, present in sorted(candidates):
                state = self._listing_state(namespace, nid)
                if not present and state != "no_longer_listed":
                    new_state = "no_longer_listed"
                elif present and state == "no_longer_listed":
                    new_state = "listed_again"
                else:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO bafin_listing_observations VALUES (?,?,?,?,?,?,?)",
                    [
                        namespace,
                        nid,
                        new_state,
                        observed_day(observed_at_ms),
                        int(observed_at_ms),
                        run_id,
                        canonical(dict(scope)),
                    ],
                )
                marks[new_state].append(nid)
                if (
                    new_state == "no_longer_listed"
                    and provider == "bafin-managers-transactions"
                    and (retention_policy == "withdraw-person-data")
                ):
                    payload = self._current_payload(namespace, nid) or {}
                    ref = (payload.get("person") or {}).get("ref")
                    if ref:
                        self.conn.execute(
                            "UPDATE bafin_persons SET name=NULL, withdrawn_at_ms=?, withdrawn_reason=? "
                            "WHERE namespace=? AND person_ref=?",
                            [
                                int(observed_at_ms),
                                "no longer listed by the source; retention after removal is "
                                "not confirmed (BF01)",
                                namespace,
                                ref,
                            ],
                        )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return marks

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
            "INSERT OR REPLACE INTO bafin_page_receipts VALUES (?,?,?,?,?,?)",
            [
                namespace,
                run_id,
                provider,
                document,
                canonical(dict(receipt)),
                int(observed_at_ms),
            ],
        )

    # -------------------------------------------------------------- reads

    def _current_payload(self, namespace: str, nid: str) -> dict[str, Any] | None:
        revisions = self._revisions(namespace, nid)
        if not revisions:
            return None
        current = max(revisions, key=_order_key)
        row = self.conn.execute(
            "SELECT payload_json FROM bafin_notice_revisions WHERE namespace=? AND notice_id=? AND revision=?",
            [namespace, nid, current["revision"]],
        ).fetchone()
        return json.loads(row[0])

    def _listing_state(
        self, namespace: str, nid: str, *, until_ms: int | None = None
    ) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM bafin_listing_observations WHERE namespace=? AND notice_id=? "
            "AND (? IS NULL OR observed_at_ms<=?) ORDER BY observed_at_ms DESC, state LIMIT 1",
            [namespace, nid, until_ms, until_ms],
        ).fetchone()
        return row[0] if row else None

    def _listing(
        self, namespace: str, nid: str, until_ms: int | None
    ) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT state, observed_on, observed_at_ms, run_id FROM bafin_listing_observations "
            "WHERE namespace=? AND notice_id=? AND (? IS NULL OR observed_at_ms<=?) ORDER BY observed_at_ms, state",
            [namespace, nid, until_ms, until_ms],
        ).fetchall()
        history = [
            dict(zip(("state", "observed_on", "observed_at_ms", "run_id"), r))
            for r in rows
        ]
        latest = history[-1] if history else None
        listed = latest is None or latest["state"] == "listed_again"
        return {
            "state": "listed" if listed else "no_longer_listed",
            "observed_on": None if listed else latest["observed_on"],
            "history": history,
        }

    def _names(self, namespace: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT person_ref, name, withdrawn_at_ms, withdrawn_reason FROM bafin_persons WHERE namespace=?",
            [namespace],
        ).fetchall()
        return {
            r[0]: {"name": r[1], "withdrawn_at_ms": r[2], "withdrawn_reason": r[3]}
            for r in rows
        }

    @staticmethod
    def _publication_clock(
        payload: Mapping[str, Any], first_observed_ms: int
    ) -> tuple[int, str]:
        if payload.get("publication_date"):
            return end_of_day_ms(payload["publication_date"]), "stated"
        return int(first_observed_ms), "first-observed"

    def _view(
        self, namespace, nid, row, payload, public_at_ms, basis, names, listing
    ) -> dict[str, Any]:
        notice = dict(payload)
        if notice.get("kind") == "managers_transaction":
            person = dict(notice.get("person") or {})
            entry = names.get(person.get("ref"), {})
            person["name"] = entry.get("name") or (
                WITHDRAWN_NAME if entry.get("withdrawn_at_ms") else None
            )
            person["withdrawn"] = bool(entry.get("withdrawn_at_ms"))
            notice["person"] = person
        return {
            "notice_id": nid,
            "revision": int(row["revision"]),
            "revision_id": row["revision_id"],
            "record_hash": row["record_hash"],
            "change": row["change"],
            "run_id": row["run_id"],
            "observed_at_ms": int(row["observed_at_ms"]),
            "source_as_of": row["source_as_of"],
            "public_at_ms": int(public_at_ms),
            "publication_basis": basis,
            "listing": listing,
            "notice": notice,
        }

    def visible(
        self,
        namespace: str,
        *,
        kinds: Sequence[str] | None = None,
        issuer_isin: str | None = None,
        public_cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Each notice's revision visible at the cutoffs, plus the rows that could not be read.

        A revision's publication clock is the notice's publication clock for the
        earliest revision in source order and, for a later revision, the later
        of that clock and the revision's own source date (end of day) or first
        observation. So an edited version is never seen before it was observed.
        """
        self.require_ready()
        params: list[Any] = [namespace]
        query = (
            "SELECT n.notice_id, r.revision, r.revision_id, r.record_hash, r.change, r.source_as_of, r.run_id, "
            "r.observed_at_ms, r.payload_json FROM bafin_notices n JOIN bafin_notice_revisions r "
            "ON r.namespace=n.namespace AND r.notice_id=n.notice_id WHERE n.namespace=?"
        )
        if kinds:
            query += " AND n.kind IN (" + ",".join("?" * len(kinds)) + ")"
            params.extend(kinds)
        if issuer_isin:
            query += " AND n.issuer_isin=?"
            params.append(normalize_isin(issuer_isin))
        rows = self.conn.execute(
            query + " ORDER BY n.notice_id, r.revision", params
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append(
                dict(
                    zip(
                        (
                            "notice_id",
                            "revision",
                            "revision_id",
                            "record_hash",
                            "change",
                            "source_as_of",
                            "run_id",
                            "observed_at_ms",
                            "payload_json",
                        ),
                        r,
                    )
                )
            )
        names = self._names(namespace)
        views, unreadable = [], []
        for nid, revisions in sorted(grouped.items()):
            try:
                # Only what was acquired by the acquisition cutoff exists for this read: a
                # later-acquired (even older) export never changes an earlier answer.
                if acquired_by_ms is not None:
                    revisions = [
                        r
                        for r in revisions
                        if int(r["observed_at_ms"]) <= acquired_by_ms
                    ]
                if not revisions:
                    continue
                ordered = sorted(revisions, key=_order_key)
                first_observed = min(int(r["observed_at_ms"]) for r in revisions)
                payloads = [json.loads(r["payload_json"]) for r in ordered]
                publication, basis = self._publication_clock(
                    payloads[0], first_observed
                )
                chosen = None
                for index, (row, payload) in enumerate(zip(ordered, payloads)):
                    clock = publication
                    if index:
                        own = (
                            end_of_day_ms(row["source_as_of"])
                            if row["source_as_of"]
                            else int(row["observed_at_ms"])
                        )
                        clock = max(publication, own)
                    if public_cutoff_ms is not None and clock > public_cutoff_ms:
                        continue
                    if (
                        acquired_by_ms is not None
                        and int(row["observed_at_ms"]) > acquired_by_ms
                    ):
                        continue
                    chosen = (row, payload, clock)
                if chosen is None:
                    continue
                until = (
                    None
                    if public_cutoff_ms is None and acquired_by_ms is None
                    else min(
                        x for x in (public_cutoff_ms, acquired_by_ms) if x is not None
                    )
                )
                listing = self._listing(namespace, nid, until)
                views.append(
                    self._view(
                        namespace,
                        nid,
                        chosen[0],
                        chosen[1],
                        chosen[2],
                        basis,
                        names,
                        listing,
                    )
                )
            except (ValueError, KeyError, TypeError) as exc:
                unreadable.append(
                    {
                        "notice_id": nid,
                        "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                    }
                )
        return {"notices": views, "unreadable": unreadable}

    def history(self, namespace: str, nid: str) -> list[dict[str, Any]]:
        self.require_ready()
        rows = self.conn.execute(
            "SELECT revision, revision_id, record_hash, change, source_as_of, run_id, observed_at_ms, payload_json "
            "FROM bafin_notice_revisions WHERE namespace=? AND notice_id=? ORDER BY revision",
            [namespace, nid],
        ).fetchall()
        if not rows:
            raise BafinError("not_found", "notice is not visible in this namespace")
        names = self._names(namespace)
        result = []
        for r in rows:
            row = dict(
                zip(
                    (
                        "revision",
                        "revision_id",
                        "record_hash",
                        "change",
                        "source_as_of",
                        "run_id",
                        "observed_at_ms",
                    ),
                    r[:7],
                )
            )
            result.append(
                self._view(
                    namespace,
                    nid,
                    row,
                    json.loads(r[7]),
                    r[6],
                    "observed",
                    names,
                    self._listing(namespace, nid, None),
                )
            )
        return result

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        """One revision by id; caller-supplied references are checked against the store."""
        self.require_ready()
        row = self.conn.execute(
            "SELECT notice_id, revision, record_hash FROM bafin_notice_revisions WHERE namespace=? AND revision_id=?",
            [namespace, str(revision_id or "")],
        ).fetchone()
        if row is None:
            raise BafinError(
                "not_found", "notice revision is not on record in this namespace"
            )
        return {
            "notice_id": row[0],
            "revision": int(row[1]),
            "revision_id": revision_id,
            "record_hash": row[2],
        }

    def receipts(
        self, namespace: str, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.require_ready()
        rows = self.conn.execute(
            "SELECT run_id, provider, document, receipt_json, observed_at_ms FROM bafin_page_receipts "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY observed_at_ms, provider, document",
            [namespace, run_id, run_id],
        ).fetchall()
        result = []
        for r in rows:
            try:
                receipt = json.loads(r[3])
            except ValueError:
                receipt = {"unreadable": True}
            result.append(
                {
                    "run_id": r[0],
                    "provider": r[1],
                    "document": r[2],
                    "receipt": receipt,
                    "observed_at_ms": int(r[4]),
                }
            )
        return result

    def generation(self, namespace: str) -> str:
        """Changes whenever any notice revision, listing observation or person withdrawal changes."""
        if not self.ready():
            return "bafin-generation:empty"
        revisions = self.conn.execute(
            "SELECT count(*), coalesce(max(observed_at_ms), 0), string_agg(revision_id, ',' ORDER BY revision_id) "
            "FROM bafin_notice_revisions WHERE namespace=?",
            [namespace],
        ).fetchone()
        listings = self.conn.execute(
            "SELECT count(*), coalesce(max(observed_at_ms), 0) FROM bafin_listing_observations WHERE namespace=?",
            [namespace],
        ).fetchone()
        persons = self.conn.execute(
            "SELECT count(*), coalesce(max(withdrawn_at_ms), 0), count(name) FROM bafin_persons WHERE namespace=?",
            [namespace],
        ).fetchone()
        return (
            "bafin-generation:"
            + digest([list(revisions), list(listings), list(persons)])[:24]
        )


# ------------------------------------------------------------------ correction chains


def correction_chains(views: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """notice_id -> {root, members (in publication order), superseded_by, link} over the given views.

    A correction names the notice it corrects by source identifier or, when only a
    flag and the corrected publication date are stated, by the notice of the same
    issuer and notifier published on that date. An ambiguous or missing target
    leaves the link ``unresolved`` and the correction heads its own chain.
    """
    by_source: dict[tuple[str, str], Mapping[str, Any]] = {}
    for view in views:
        notice = view["notice"]
        by_source[(notice["source"]["provider"], notice["source"]["source_id"])] = view

    def subject(notice: Mapping[str, Any]) -> tuple:
        party = (
            notice.get("notifier") or notice.get("person") or notice.get("holder") or {}
        )
        return (
            issuer_key(notice.get("issuer") or {}),
            party.get("ref")
            or party_key(party.get("name"), kind=party.get("kind", "unknown")),
        )

    parent: dict[str, str] = {}
    links: dict[str, dict[str, Any]] = {}
    for view in views:
        notice = view["notice"]
        correction = notice.get("correction_of")
        if not correction:
            continue
        provider = notice["source"]["provider"]
        target = None
        if correction.get("source_id"):
            target = by_source.get((provider, correction["source_id"]))
            basis = "stated-source-id"
        else:
            matches = [
                v
                for v in views
                if v["notice_id"] != view["notice_id"]
                and v["notice"]["source"]["provider"] == provider
                and subject(v["notice"]) == subject(notice)
                and correction.get("publication_date")
                and v["notice"].get("publication_date")
                == correction["publication_date"]
            ]
            target = matches[0] if len(matches) == 1 else None
            basis = (
                "same-issuer-notifier-publication-date"
                if target
                else ("ambiguous" if len(matches) > 1 else "no-match")
            )
        if target is not None and target["notice_id"] != view["notice_id"]:
            parent[view["notice_id"]] = target["notice_id"]
            links[view["notice_id"]] = {
                "status": "resolved",
                "basis": basis,
                "corrects": target["notice_id"],
            }
        else:
            links[view["notice_id"]] = {
                "status": "unresolved",
                "basis": basis,
                "stated": correction,
            }

    def root(nid: str) -> str:
        seen = set()
        while nid in parent and nid not in seen:
            seen.add(nid)
            nid = parent[nid]
        return nid

    chains: dict[str, list[Mapping[str, Any]]] = {}
    for view in views:
        chains.setdefault(root(view["notice_id"]), []).append(view)
    result = {}
    for chain_root, members in chains.items():
        ordered = sorted(
            members,
            key=lambda v: (
                v["public_at_ms"],
                v["notice"].get("publication_date") or "",
                v["notice_id"],
            ),
        )
        ids = [m["notice_id"] for m in ordered]
        for index, member in enumerate(ordered):
            result[member["notice_id"]] = {
                "root": chain_root,
                "members": ids,
                "superseded_by": ids[index + 1] if index + 1 < len(ids) else None,
                "link": links.get(member["notice_id"]),
            }
    return result


def current_per_chain(views: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One notice per correction chain (the latest published), carrying its chain."""
    chains = correction_chains(views)
    by_id = {v["notice_id"]: v for v in views}
    result = []
    for nid, chain in sorted(chains.items()):
        if chain["superseded_by"] is None:
            result.append({**by_id[nid], "chain": chain})
    return result


# ------------------------------------------------------------------ runtime projector


class BafinNoticeProjector:
    """Source-pack runtime projector for ``noesis-bafin-notice-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = BafinNoticeStore(conn)

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        del manifest, principal_id
        declared = dict(source.get("bafin") or {})
        namespace = str(declared.get("namespace") or DEFAULT_NAMESPACE)
        observed = max(
            (
                int(d["ingested_at"])
                for d in documents or []
                if d.get("ingested_at") is not None
            ),
            default=self.store.now(),
        )
        notices = [
            dict(item["bafin_notice"]) for item in records if item.get("bafin_notice")
        ]
        for notice in notices:
            notice["source"] = {**notice["source"], "retrieved_at_ms": observed}
        receipt = dict(page_receipt or {})
        try:
            counts = self.store.apply(
                namespace,
                notices,
                run_id=run_id,
                observed_at_ms=observed,
                source_as_of=receipt.get("source_as_of"),
            )
        except BafinError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc
        listing = receipt.get("listing") or {}
        marks = {}
        if listing.get("complete"):
            marks = self.store.observe_listing(
                namespace,
                provider=declared["provider"],
                scope=listing.get("scope") or {},
                seen=listing.get("seen") or [],
                observed_at_ms=observed,
                run_id=run_id,
                retention_policy=declared.get("retention_policy", "retain"),
            )
        self.store.record_receipt(
            namespace,
            run_id=run_id,
            provider=declared["provider"],
            document=str(receipt.get("document") or ""),
            receipt={
                **{k: v for k, v in receipt.items() if k != "listing"},
                "listing": {k: v for k, v in listing.items() if k != "seen"},
                "stored": {k: v for k, v in counts.items() if k != "changed"},
                "marks": marks,
            },
            observed_at_ms=observed,
        )
        return {k: v for k, v in counts.items() if k != "changed"}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


# ------------------------------------------------------------------ feature selection


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Market bundle's optional ``bafin-notices`` feature is selected (default off; reads only)."""
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
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='market'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "bafin-notices" in ((plan.get("features") or {}).get("market") or [])

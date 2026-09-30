"""Space-object registration, operator-assertion and re-entry records (#2224, SO02).

One record contract, ``noesis-astronomy-registration-record-v1``, beside the
Astronomy pack's ``noesis-astronomy-record-v1`` (:mod:`src.kb.astronomy_records`).
Registration records do not form a second object store: they name the space
object by the COSPAR and NORAD identifiers they state and link to the
``orbital_object`` records of GCAT and CelesTrak SATCAT
(:class:`src.kb.astronomy_store.AstronomyStore`) through
:mod:`src.kb.astronomy_registration_identity`.

Every record is *what one source states*:

* ``registration_entry`` - an UNOOSA Online Index entry (``index_entry``:
  registering State, launch date, status and function as published, whether a
  UN registration document is on record, with the index retrieval date) or one
  entry of a UN registration document or later notification (``registration``,
  ``change_of_status``, ``transfer_of_supervision``, ``re_entry_notice``,
  ``additional_information``): the document symbol, the paragraph or table
  locator, the language and the verbatim quotation, the orbital parameters
  *as registered* (text with the printed unit) and any instrument or paper the
  document names;
* ``operator_assertion`` - an operator or owner name and role as published
  (and an identifier only where the source publishes one); no attribution
  field beyond what the source states;
* ``reentry_report`` - a published re-entry prediction or post-event report:
  the reported time, uncertainty window and location as published, with the
  report's issue time as its revision date;
* ``discos_object`` / ``discos_citation`` - the permitted ESA DISCOS subset
  (identifiers, name, class) or only a citation, always ``restricted``.

Store rules follow :mod:`src.kb.astronomy_store`: a stable ``record_id`` per
namespace, kind, provider and source record ID; unchanged content adds
nothing; the record's own date (document date, index retrieval date, assertion
date or report issue time) orders revisions, so a late-arriving older state is
kept as ``history`` and never becomes current. Point-in-time reads take a
public cutoff and an optional acquisition cutoff. Nothing is deleted: the
confirmed re-entry supersedes predictions, a transfer of supervision follows
the original registration, and both stay on record.

Citation links from registrations to legal instruments and Science papers
(SO09) are link records owned here (:class:`RegistrationCitations`).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.kb.astronomy_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    AstronomyError,
    authorize,
    canonical,
    clock_ms,
    digest,
    fold,
    iso_day,
    iso_time,
    normalize_bibcode,
    normalize_cospar,
    normalize_doi,
    normalize_norad,
    object_name_key,
    observed_day,
)

CONTRACT = "noesis-astronomy-registration-record-v1"
DEFAULT_NAMESPACE = "astronomy"
KINDS = (
    "registration_entry",
    "operator_assertion",
    "reentry_report",
    "discos_object",
    "discos_citation",
)
PROVIDERS: dict[str, tuple[str, ...]] = {
    "unoosa-index": ("registration_entry",),
    "unoosa-registration-documents": ("registration_entry", "operator_assertion"),
    "esa-discos": (
        "discos_object",
        "operator_assertion",
        "reentry_report",
        "discos_citation",
    ),
    "aerospace-reentry": ("reentry_report",),
}
RESTRICTED_PROVIDERS = frozenset({"esa-discos"})
ENTRY_KINDS = (
    "index_entry",
    "registration",
    "change_of_status",
    "transfer_of_supervision",
    "re_entry_notice",
    "additional_information",
)
DOCUMENT_ENTRY_KINDS = ENTRY_KINDS[1:]
REGISTRANT_KINDS = ("state", "intergovernmental_organisation")
REPORT_KINDS = ("prediction", "post_event")
LANGUAGES = ("en", "fr", "es", "ru", "zh", "ar")
NOTICE = (
    "Records quote what each publisher states, with its revision and as-of time. Noesis computes no re-entry or "
    "collision prediction and attributes no operator, owner or State beyond a published registration or operator "
    "assertion. 'No UN registration on record' is not 'unregistered by the State'."
)
NO_UN_REGISTRATION = "no UN registration on record"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/schemas/jsonschema/noesis-astronomy-registration-record-v1.json"
)
_UN_SYMBOL = re.compile(r"^(ST/SG/SER\.E/|A/AC\.105/)[A-Z0-9./]+$")
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")

# ------------------------------------------------------------------ validation

_SOURCE = {
    "provider",
    "source_record_id",
    "url",
    "locator",
    "published_at",
    "retrieved_at_ms",
    "attribution",
    "document",
}
_OBJECT = {"cospar", "norad", "object_name"}
_REFERENCE = {"text", "identifier", "bibcode", "doi", "url"}
_KIND_FIELDS: dict[str, dict[str, set[str]]] = {
    "registration_entry": {
        "required": {"entry_kind"},
        "optional": _OBJECT
        | {
            "national_designator",
            "registering_state",
            "registrant_kind",
            "un_registered",
            "un_document",
            "document_date",
            "document_locator",
            "language",
            "quotation",
            "launch_date",
            "launch_site",
            "status",
            "function",
            "decay_date",
            "index_retrieved_on",
            "registered_orbit",
            "status_change",
            "supervision",
            "reentry",
            "instruments",
            "references",
        },
    },
    "operator_assertion": {
        "required": {"operator_name", "role"},
        "optional": _OBJECT
        | {
            "discos_id",
            "asserted_on",
            "identifier",
            "country",
            "un_document",
            "document_locator",
            "restricted",
        },
    },
    "reentry_report": {
        "required": {"report_kind", "issued_at"},
        "optional": _OBJECT
        | {
            "discos_id",
            "reported_time",
            "reported_time_text",
            "uncertainty",
            "location",
            "restricted",
        },
    },
    "discos_object": {
        "required": {"discos_id", "restricted"},
        "optional": _OBJECT | {"object_class"},
    },
    "discos_citation": {
        "required": {"discos_id", "url", "restricted"},
        "optional": {"cospar", "norad", "note"},
    },
}
_EXPECTED = {
    "registration_entry": ("cospar", "registering_state", "status"),
    "operator_assertion": ("asserted_on",),
    "reentry_report": ("reported_time", "uncertainty", "location"),
    "discos_object": ("cospar", "norad"),
    "discos_citation": (),
}


def _fail(message: str) -> None:
    raise AstronomyError("invalid_record", message)


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            cleaned = _clean(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    if isinstance(value, list):
        return [c for c in (_clean(v) for v in value) if c is not None]
    if value is None or (
        isinstance(value, str) and value.strip() in {"", "None", "null"}
    ):
        return None
    return value


def _text(value: Any, field: str, limit: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} is bounded non-empty text as the source states it")
    return value


def _day(value: Any, field: str) -> None:
    if iso_day(value) != value:
        _fail(f"{field} is an ISO date")


def _when(value: Any, field: str) -> None:
    if not (iso_day(value) == value or iso_time(value) == value):
        _fail(f"{field} is an ISO date or an ISO UTC time")


def _only(value: Any, keys: set[str], field: str, required: set[str] = frozenset()) -> None:
    if not isinstance(value, Mapping) or set(value) - keys or required - set(value):
        _fail(f"{field} states {sorted(keys)} only (needs {sorted(required)})")


def _references(values: Any, field: str) -> None:
    if not isinstance(values, list) or len(values) > 20:
        _fail(f"{field} lists at most 20 stated references")
    for ref in values:
        _only(ref, _REFERENCE, field, {"text"})


def compute_unknowns(record: Mapping[str, Any]) -> list[str]:
    missing = [f for f in _EXPECTED[record["kind"]] if f not in record]
    if record["kind"] in {"registration_entry", "operator_assertion", "reentry_report"} and not (
        {"cospar", "norad"} & set(record)
    ):
        missing.append("object identifier (COSPAR or NORAD)")
    if "published_at" not in record["source"]:
        missing.append("source.published_at")
    return sorted(set(missing))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate one record as its source states it; returns it with ``contract`` and ``unknowns``.

    Refuses undeclared keys (a record never holds a Noesis-computed prediction or an attribution the source does
    not state), publishers outside :data:`PROVIDERS`, unrestricted DISCOS data and identifiers that are not
    normalised.
    """
    if not isinstance(record, Mapping):
        _fail("a record is an object")
    record = _clean(dict(record))
    record.pop("unknowns", None)
    record.pop("contract", None)
    kind = record.get("kind")
    if kind not in KINDS:
        _fail(f"kind is one of {KINDS}")
    source = record.get("source")
    if not isinstance(source, Mapping) or set(source) - _SOURCE:
        _fail("source states provider, source_record_id and locator fields only")
    provider = source.get("provider")
    if provider not in PROVIDERS:
        _fail("records come from a declared publisher; Noesis publishes none")
    if kind not in PROVIDERS[provider]:
        _fail(f"{provider} does not publish {kind} records")
    _text(source.get("source_record_id"), "source.source_record_id", 500)
    if "published_at" in source:
        _when(source["published_at"], "source.published_at")
    if "retrieved_at_ms" in source and (
        type(source["retrieved_at_ms"]) is not int or source["retrieved_at_ms"] < 0
    ):
        _fail("source.retrieved_at_ms is epoch milliseconds")
    fields = _KIND_FIELDS[kind]
    extra = set(record) - {"kind", "source"} - fields["required"] - fields["optional"]
    if extra:
        _fail(
            f"undeclared fields for {kind}: {sorted(extra)} (a record never holds a prediction or an attribution "
            "the source does not state)"
        )
    missing = fields["required"] - set(record)
    if missing:
        _fail(f"{kind} needs {sorted(missing)}")
    if (provider in RESTRICTED_PROVIDERS) != (record.get("restricted") is True):
        _fail("ESA DISCOS records, and only they, are restricted")
    if "cospar" in record and normalize_cospar(record["cospar"]) != record["cospar"]:
        _fail("cospar is a normalised international designator")
    if "norad" in record and normalize_norad(record["norad"]) != record["norad"]:
        _fail("norad is the catalogue number without padding")
    for key in ("object_name", "national_designator", "launch_site", "status", "function"):
        if key in record:
            _text(record[key], key, 500)
    for key in ("launch_date", "decay_date", "document_date", "index_retrieved_on", "asserted_on"):
        if key in record:
            _day(record[key], key)
    if "discos_id" in record and not re.fullmatch(r"\d+", str(record["discos_id"])):
        _fail("discos_id is DISCOS's numeric object ID")
    if kind == "registration_entry":
        entry = record["entry_kind"]
        if entry not in ENTRY_KINDS:
            _fail(f"entry_kind is one of {ENTRY_KINDS}")
        if not _OBJECT & set(record):
            _fail("a registration entry names its object (COSPAR, NORAD or name)")
        if (entry == "index_entry") != (provider == "unoosa-index"):
            _fail("index entries come from the UNOOSA index; document entries from registration documents")
        if "un_registered" in record and type(record["un_registered"]) is not bool:
            _fail("un_registered is the index's yes/no as published")
        if "un_document" in record and not _UN_SYMBOL.fullmatch(record["un_document"]):
            _fail("un_document is a UN document symbol (ST/SG/SER.E/..., A/AC.105/...)")
        if "registrant_kind" in record and record["registrant_kind"] not in REGISTRANT_KINDS:
            _fail(f"registrant_kind is one of {REGISTRANT_KINDS} as the document states it")
        if entry in DOCUMENT_ENTRY_KINDS:
            for key in ("un_document", "document_locator", "language", "quotation", "document_date"):
                if key not in record:
                    _fail(f"a registration document entry keeps its {key}")
            _only(record["document_locator"], {"paragraph", "table", "row"}, "document_locator")
            if not record["document_locator"]:
                _fail("document_locator names a paragraph or table row")
            if record["language"] not in LANGUAGES:
                _fail(f"language is a UN official language code {LANGUAGES}")
            _text(record["quotation"], "quotation")
        else:
            for key in ("document_locator", "quotation", "status_change", "supervision", "reentry"):
                if key in record:
                    _fail(f"an index entry states no {key}; notifications are document entries")
        orbit = record.get("registered_orbit")
        if orbit is not None:
            if not isinstance(orbit, Mapping) or not orbit:
                _fail("registered_orbit maps parameter names to registered values")
            for name, item in orbit.items():
                _only(item, {"value", "unit"}, f"registered_orbit.{name}", {"value"})
                _text(item["value"], f"registered_orbit.{name}.value", 100)
        if entry == "change_of_status":
            _only(record.get("status_change"), {"status", "effective_date"}, "status_change", {"status"})
        elif "status_change" in record:
            _fail("status_change belongs to a change-of-status notice")
        if entry == "transfer_of_supervision":
            _only(record.get("supervision"), {"from", "to", "effective_date"}, "supervision", {"to"})
        elif "supervision" in record:
            _fail("supervision belongs to a transfer-of-supervision notice")
        if "reentry" in record:
            if entry != "re_entry_notice":
                _fail("reentry belongs to a re-entry notice")
            _only(record["reentry"], {"date", "location"}, "reentry")
        for key in ("status_change", "supervision", "reentry"):
            day = (record.get(key) or {}).get("effective_date") or (
                (record.get(key) or {}).get("date") if key == "reentry" else None
            )
            if day:
                _day(day, f"{key} date")
        for key in ("instruments", "references"):
            if key in record:
                _references(record[key], key)
    elif kind == "operator_assertion":
        _text(record["operator_name"], "operator_name", 500)
        _text(record["role"], "role", 200)
        if not ({"cospar", "norad", "object_name", "discos_id"} & set(record)):
            _fail("an operator assertion names its object")
        if "identifier" in record:
            _only(record["identifier"], {"scheme", "value"}, "identifier", {"scheme", "value"})
        if "document_locator" in record:
            _only(record["document_locator"], {"paragraph", "table", "row"}, "document_locator")
    elif kind == "reentry_report":
        if record["report_kind"] not in REPORT_KINDS:
            _fail(f"report_kind is one of {REPORT_KINDS} as the publisher labels the report")
        _when(record["issued_at"], "issued_at")
        if "reported_time" in record:
            _when(record["reported_time"], "reported_time")
        if not ({"cospar", "norad", "discos_id"} & set(record)):
            _fail("a re-entry report names its object by COSPAR, NORAD or DISCOS ID")
        if "uncertainty" in record:
            _only(record["uncertainty"], {"text", "window_start", "window_end"}, "uncertainty", {"text"})
            for key in ("window_start", "window_end"):
                if key in record["uncertainty"]:
                    _when(record["uncertainty"][key], f"uncertainty.{key}")
        if "location" in record:
            _only(record["location"], {"text", "latitude", "longitude"}, "location", {"text"})
            coords = [record["location"].get(k) for k in ("latitude", "longitude")]
            if any(coords) and not all(c and _DECIMAL.fullmatch(c) for c in coords):
                _fail("location coordinates are the published decimal text, both or neither")
    elif kind == "discos_citation":
        if not str(record["url"]).startswith("https://"):
            _fail("a DISCOS citation keeps the object's HTTPS URL")
    record["contract"] = CONTRACT
    record["unknowns"] = compute_unknowns(record)
    return record


def semantic(record: Mapping[str, Any]) -> dict[str, Any]:
    """The source-independent content a revision is compared by (no locators, documents, clocks or unknowns)."""
    body = {k: v for k, v in record.items() if k not in {"unknowns", "contract"}}
    source = dict(body.get("source") or {})
    body["source"] = {
        k: source[k] for k in ("provider", "source_record_id", "published_at") if k in source
    }
    return body


def record_id(namespace: str, kind: str, provider: str, source_record_id: str) -> str:
    return "astro-reg:" + digest([namespace, kind, provider, source_record_id])[:24]


def party_key(value: Any) -> str:
    """A State, registrant or operator name compared without case or punctuation (never fuzzy)."""
    return fold(value)


def object_keys(record: Mapping[str, Any]) -> list[str]:
    """Every source-stated key a record can be looked up by (COSPAR, NORAD, name, DISCOS ID, parties, document)."""
    keys: set[str] = set()
    for field, prefix in (("cospar", "cospar"), ("norad", "norad"), ("discos_id", "discos")):
        if record.get(field):
            keys.add(f"{prefix}:{record[field]}")
    if record.get("object_name"):
        keys.add("name:" + object_name_key(record["object_name"]))
    for value in (
        record.get("registering_state"),
        (record.get("supervision") or {}).get("from"),
        (record.get("supervision") or {}).get("to"),
    ):
        if value:
            keys.add("state:" + party_key(value))
    if record.get("operator_name"):
        keys.add("operator:" + party_key(record["operator_name"]))
    if record.get("un_document"):
        keys.add("doc:" + record["un_document"])
    return sorted(keys)


def identifier_keys(identifier: Any) -> list[str]:
    """The lookup keys of a caller's object identifier: COSPAR, NORAD, DISCOS ``discos:<id>`` or a name."""
    text = str(identifier or "").strip()
    if not text:
        raise AstronomyError("invalid_request", "name an object by COSPAR, NORAD or name")
    keys = []
    if normalize_cospar(text):
        keys.append("cospar:" + normalize_cospar(text))
    if text.lower().startswith("discos:") and text[7:].isdigit():
        keys.append("discos:" + text[7:])
    elif normalize_norad(text):
        keys.append("norad:" + normalize_norad(text))
    if not keys:
        keys.append("name:" + object_name_key(text))
    return keys


def own_date(record: Mapping[str, Any]) -> str | None:
    """The date the source states for this record's content (orders revisions); ``None`` when it states none."""
    kind = record["kind"]
    if kind == "registration_entry":
        stated = record.get("document_date") or record.get("index_retrieved_on")
    elif kind == "operator_assertion":
        stated = record.get("asserted_on")
    elif kind == "reentry_report":
        stated = record.get("issued_at")
    else:
        stated = None
    return stated or (record.get("source") or {}).get("published_at")


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the record contract as a schema module in the shared registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "astronomy-registration-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": schema(),
        "owner": "astronomy.space-object-registration",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            "astronomy-schema:astronomy-registration-record:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]


# ------------------------------------------------------------------ store

_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_registration_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL,
  source_record_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS astronomy_registration_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, change TEXT NOT NULL, stated_at TEXT,
  source_as_of TEXT, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
CREATE TABLE IF NOT EXISTS astronomy_registration_keys (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(namespace, record_id, key)
);
CREATE TABLE IF NOT EXISTS astronomy_registration_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, document TEXT NOT NULL,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, provider, document)
);
"""
TABLES = (
    "astronomy_registration_records",
    "astronomy_registration_revisions",
    "astronomy_registration_keys",
    "astronomy_registration_receipts",
)
_REVISION_FIELDS = (
    "revision",
    "revision_id",
    "record_hash",
    "change",
    "stated_at",
    "source_as_of",
    "run_id",
    "observed_at_ms",
)


def _order_key(row: Mapping[str, Any]) -> tuple:
    stated = row.get("stated_at") or row.get("source_as_of") or observed_day(int(row["observed_at_ms"]))
    return (str(stated), int(row["observed_at_ms"]), int(row["revision"]))


def table_exists(conn: Any, name: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone()
    )


class RegistrationStore:
    """Revisioned registration, operator and re-entry records; the one owner of the registration contract."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return all(table_exists(self.conn, t) for t in TABLES)

    def require_ready(self) -> None:
        if not self.ready():
            raise AstronomyError(
                "not_ready",
                "no space-object registration source has run yet; run the astronomy-and-space source pack's "
                "registration sources first",
            )

    def _revisions(self, namespace: str, rid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_FIELDS) + " FROM astronomy_registration_revisions "
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
        source_as_of: str | None = None,
    ) -> dict[str, Any]:
        """Validate and append records; unchanged content adds nothing, an older state is kept as history."""
        counts = {"inserted": 0, "revised": 0, "unchanged": 0, "history": 0}
        changed: list[str] = []
        source_as_of = iso_day(source_as_of) if source_as_of else None
        validated = [validate_record(r) for r in records]
        # Within one page, older statements are applied first so a page listing predictions in any order
        # leaves the latest issued one current.
        validated.sort(key=lambda r: str(own_date(r) or ""))
        self.conn.execute("BEGIN")
        try:
            for record in validated:
                provider = record["source"]["provider"]
                source_record_id = record["source"]["source_record_id"]
                rid = record_id(namespace, record["kind"], provider, source_record_id)
                record_hash = digest(semantic(record))
                revisions = self._revisions(namespace, rid)
                incoming = {
                    "stated_at": own_date(record),
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
                    if older and any(r["record_hash"] == record_hash for r in revisions):
                        counts["unchanged"] += 1
                        continue
                    change = "history" if older else "revised"
                else:
                    change = "new"
                revision = incoming["revision"]
                revision_id = "astro-reg-rev:" + digest([rid, revision, record_hash])[:24]
                self.conn.execute(
                    "INSERT INTO astronomy_registration_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        rid,
                        revision,
                        revision_id,
                        record_hash,
                        canonical(record),
                        change,
                        incoming["stated_at"],
                        source_as_of,
                        run_id,
                        int(observed_at_ms),
                    ],
                )
                for key in object_keys(record):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO astronomy_registration_keys VALUES (?,?,?)", [namespace, rid, key]
                    )
                if not revisions:
                    self.conn.execute(
                        "INSERT INTO astronomy_registration_records VALUES (?,?,?,?,?,?)",
                        [namespace, rid, record["kind"], provider, source_record_id, int(observed_at_ms)],
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
            "INSERT OR REPLACE INTO astronomy_registration_receipts VALUES (?,?,?,?,?,?)",
            [namespace, run_id, provider, document, canonical(dict(receipt)), int(observed_at_ms)],
        )

    # -------------------------------------------------------------- reads

    @staticmethod
    def _clock(row: Mapping[str, Any], first_observed: int, index: int, first_clock: int) -> tuple[int, str]:
        stated = row.get("stated_at") or row.get("source_as_of")
        own = clock_ms(stated) if stated else (first_observed if index == 0 else int(row["observed_at_ms"]))
        return (own if index == 0 else max(first_clock, own)), ("stated" if stated else "first-observed")

    def visible(
        self,
        namespace: str,
        *,
        kinds: Sequence[str] | None = None,
        keys: Iterable[str] | None = None,
        public_cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Each record's revision visible at the cutoffs (with every earlier and later one), pending and unreadable.

        Each view keeps ``revisions``: the non-history revisions published by the cutoff in publication order, so
        predictions superseded by a post-event report and a registration followed by a transfer stay visible.
        """
        self.require_ready()
        params: list[Any] = [namespace]
        query = (
            "SELECT r.record_id, r.kind, v.revision, v.revision_id, v.record_hash, v.change, v.stated_at, "
            "v.source_as_of, v.run_id, v.observed_at_ms, v.payload_json FROM astronomy_registration_records r "
            "JOIN astronomy_registration_revisions v ON v.namespace=r.namespace AND v.record_id=r.record_id "
            "WHERE r.namespace=?"
        )
        if kinds:
            unknown = set(kinds) - set(KINDS)
            if unknown:
                raise AstronomyError("invalid_request", f"unknown kinds {sorted(unknown)}")
            query += " AND r.kind IN (" + ",".join("?" * len(kinds)) + ")"
            params.extend(kinds)
        if keys is not None:
            keys = sorted(set(keys))
            if not keys:
                return {"records": [], "pending": [], "unreadable": []}
            query += (
                " AND r.record_id IN (SELECT record_id FROM astronomy_registration_keys WHERE namespace=? AND key IN ("
                + ",".join("?" * len(keys))
                + "))"
            )
            params.extend([namespace, *keys])
        rows = self.conn.execute(query + " ORDER BY r.record_id, v.revision", params).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        fields = ("record_id", "kind", *_REVISION_FIELDS, "payload_json")
        for r in rows:
            grouped.setdefault(r[0], []).append(dict(zip(fields, r)))
        views, pending, unreadable = [], [], []
        for rid, revisions in sorted(grouped.items()):
            try:
                if acquired_by_ms is not None:
                    revisions = [r for r in revisions if int(r["observed_at_ms"]) <= acquired_by_ms]
                if not revisions:
                    continue
                ordered = sorted(revisions, key=_order_key)
                first_observed = min(int(r["observed_at_ms"]) for r in revisions)
                clocks, first_clock = [], 0
                for index, row in enumerate(ordered):
                    clock, basis = self._clock(row, first_observed, index, first_clock)
                    if index == 0:
                        first_clock = clock
                    clocks.append((row, clock, basis))
                shown, later = [], []
                for row, clock, basis in clocks:
                    if public_cutoff_ms is not None and clock > public_cutoff_ms:
                        later.append({"revision_id": row["revision_id"], "public_at_ms": clock})
                    else:
                        shown.append((row, clock, basis))
                if not shown:
                    pending.append(
                        {
                            "record_id": rid,
                            "kind": revisions[0]["kind"],
                            "first_public_at_ms": min(c for _, c, _ in clocks),
                            "record": json.loads(ordered[0]["payload_json"]),
                        }
                    )
                    continue
                row, clock, basis = shown[-1]
                views.append(
                    {
                        "record_id": rid,
                        "kind": row["kind"],
                        **{k: row[k] for k in _REVISION_FIELDS if row.get(k) is not None},
                        "public_at_ms": int(clock),
                        "publication_basis": basis,
                        "later": later,
                        "revisions": [
                            {
                                "revision": int(r["revision"]),
                                "revision_id": r["revision_id"],
                                "change": r["change"],
                                "public_at_ms": int(c),
                                "publication_basis": b,
                                "observed_at_ms": int(r["observed_at_ms"]),
                                "record": json.loads(r["payload_json"]),
                            }
                            for r, c, b in shown
                        ],
                        "record": json.loads(row["payload_json"]),
                    }
                )
            except (ValueError, KeyError, TypeError) as exc:
                unreadable.append({"record_id": rid, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return {"records": views, "pending": pending, "unreadable": unreadable}

    def history(self, namespace: str, rid: str) -> list[dict[str, Any]]:
        self.require_ready()
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_FIELDS) + ", payload_json FROM astronomy_registration_revisions "
            "WHERE namespace=? AND record_id=? ORDER BY revision",
            [namespace, rid],
        ).fetchall()
        if not rows:
            raise AstronomyError("not_found", "record is not on record in this namespace")
        return [
            {**{k: v for k, v in zip(_REVISION_FIELDS, r[:-1]) if v is not None}, "record": json.loads(r[-1])}
            for r in rows
        ]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        self.require_ready()
        row = self.conn.execute(
            "SELECT record_id, revision, record_hash, payload_json FROM astronomy_registration_revisions "
            "WHERE namespace=? AND revision_id=?",
            [namespace, str(revision_id or "")],
        ).fetchone()
        if row is None:
            raise AstronomyError("not_found", "revision is not on record in this namespace")
        return {
            "record_id": row[0],
            "revision": int(row[1]),
            "revision_id": revision_id,
            "record_hash": row[2],
            "record": json.loads(row[3]),
        }

    def receipts(self, namespace: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT run_id, provider, document, receipt_json, observed_at_ms FROM astronomy_registration_receipts "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY observed_at_ms, provider, document",
            [namespace, run_id, run_id],
        ).fetchall()
        return [
            {"run_id": r[0], "provider": r[1], "document": r[2], "receipt": json.loads(r[3]),
             "observed_at_ms": int(r[4])}
            for r in rows
        ]

    def providers_consulted(self, namespace: str, *, acquired_by_ms: int | None = None) -> list[dict[str, Any]]:
        """Which registration sources have run in this namespace (from receipts), with their latest run."""
        out: dict[str, dict[str, Any]] = {}
        for receipt in self.receipts(namespace):
            if acquired_by_ms is not None and receipt["observed_at_ms"] > acquired_by_ms:
                continue
            entry = out.setdefault(receipt["provider"], {"provider": receipt["provider"], "documents": 0})
            entry["documents"] += 1
            entry["last_run_id"] = receipt["run_id"]
            entry["last_observed_on"] = observed_day(receipt["observed_at_ms"])
        return [out[k] for k in sorted(out)]

    def generation(self, namespace: str) -> str:
        """Changes whenever any revision, object link, identity decision or citation link changes."""
        if not self.ready():
            return "astronomy-registration-generation:empty"
        parts: list[Any] = [
            list(
                self.conn.execute(
                    "SELECT count(*), coalesce(string_agg(revision_id, ',' ORDER BY revision_id), '') "
                    "FROM astronomy_registration_revisions WHERE namespace=?",
                    [namespace],
                ).fetchone()
            )
        ]
        for table, column in (
            ("astronomy_registration_object_links", "link_id || ':' || state"),
            ("astronomy_registration_citations", "link_id || ':' || state"),
            ("astronomy_identity_candidates", "candidate_id || ':' || state || ':' || coalesce(decision_id, '')"),
            ("ownership_identity_candidates", "candidate_id || ':' || state || ':' || coalesce(decision_id, '')"),
        ):
            if table_exists(self.conn, table):
                parts.append(
                    list(
                        self.conn.execute(
                            f"SELECT count(*), coalesce(string_agg({column}, ',' ORDER BY {column}), '') "
                            f"FROM {table} WHERE namespace=?",
                            [namespace],
                        ).fetchone()
                    )
                )
        return "astronomy-registration-generation:" + digest(parts)[:24]


# ------------------------------------------------------------------ runtime projector


class RegistrationProjector:
    """Source-pack runtime projector for ``noesis-astronomy-registration-record-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = RegistrationStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        declared = dict(source.get("astronomy_registration") or {})
        namespace = str(declared.get("namespace") or DEFAULT_NAMESPACE)
        observed = max(
            (int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
            default=self.store.now(),
        )
        items = [dict(item["registration_record"]) for item in records if item.get("registration_record")]
        receipt = dict(page_receipt or {})
        retrieved_on = iso_day(receipt.get("source_as_of")) or observed_day(observed)
        for item in items:
            item["source"] = {**item["source"], "retrieved_at_ms": observed}
            if item.get("entry_kind") == "index_entry":
                item.setdefault("index_retrieved_on", retrieved_on)
        try:
            counts = self.store.apply(
                namespace, items, run_id=run_id, observed_at_ms=observed, source_as_of=receipt.get("source_as_of")
            )
        except AstronomyError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc
        self.store.record_receipt(
            namespace,
            run_id=run_id,
            provider=str(declared.get("provider") or ""),
            document=str(receipt.get("document") or ""),
            receipt={**receipt, "stored": {k: v for k, v in counts.items() if k != "changed"}},
            observed_at_ms=observed,
        )
        return {k: v for k, v in counts.items() if k != "changed"}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


# ------------------------------------------------------------------ feature selection

FEATURES = ("astronomy-space-object-registration", "astronomy-discos")


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Astronomy bundle's registration features are selected (default off; reads only)."""
    from src.kb.astronomy_store import active_plan

    if feature not in FEATURES:
        raise AstronomyError("invalid_request", f"feature is one of {FEATURES}")
    plan = active_plan(conn) or {}
    return feature in ((plan.get("features") or {}).get("astronomy") or [])


# ------------------------------------------------------------------ citation links (SO09)

CITATION_CONTRACT = "noesis-astronomy-registration-citation-v1"
_CITATION_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_registration_citations (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, citation_kind TEXT NOT NULL, citation_value TEXT NOT NULL, stated TEXT NOT NULL,
  target_namespace TEXT, target_id TEXT, target_revision_id TEXT, state TEXT NOT NULL, basis TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
LEGAL_READ = "knowledge:legal:read"


def stated_citations(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The instruments and papers a registration record names, by exact identifier only (never keywords)."""
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for ref in record.get("instruments") or []:
        identifier = str(ref.get("identifier") or "").strip()
        if identifier:
            found[("legal-work", "instrument", identifier)] = {
                "target_kind": "legal-work",
                "kind": "instrument",
                "value": identifier,
                "stated": ref["text"][:300],
            }
    for ref in record.get("references") or []:
        for kind, normalise in (("bibcode", normalize_bibcode), ("doi", normalize_doi)):
            value = normalise(ref.get(kind))
            if value:
                found[("paper", kind, value)] = {
                    "target_kind": "paper",
                    "kind": kind,
                    "value": value,
                    "stated": ref["text"][:300],
                }
    return [found[k] for k in sorted(found)]


class RegistrationCitations:
    """Registration records linked to Legal works and Science papers only by an explicit, exact citation."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = RegistrationStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_CITATION_DDL)

    def _legal_index(self, legal_namespace: str) -> dict[str, list[dict[str, Any]]]:
        """Legal works by every identifier they carry (read-only; the Legal pack owns them)."""
        if not table_exists(self.conn, "legal_works"):
            return {}
        index: dict[str, list[dict[str, Any]]] = {}
        rows = self.conn.execute(
            "SELECT work_id, native_id, identifiers_json, title, jurisdiction FROM legal_works WHERE namespace=? "
            "ORDER BY work_id",
            [legal_namespace],
        ).fetchall()
        for work_id, native_id, identifiers_json, title, jurisdiction in rows:
            try:
                identifiers = json.loads(identifiers_json) if identifiers_json else {}
            except ValueError:
                identifiers = {}
            values = {str(native_id)}
            for value in identifiers.values():
                if isinstance(value, str):
                    values.add(value)
                elif isinstance(value, list):
                    values |= {str(v) for v in value}
            version = None
            if table_exists(self.conn, "legal_versions"):
                row = self.conn.execute(
                    "SELECT version_id FROM legal_versions WHERE namespace=? AND work_id=? ORDER BY observed_at_ms "
                    "DESC, version_id DESC LIMIT 1",
                    [legal_namespace, work_id],
                ).fetchone()
                version = row[0] if row else None
            work = {"work_id": work_id, "version_id": version, "title": title, "jurisdiction": jurisdiction}
            for value in values:
                index.setdefault(value.strip(), []).append(work)
        return index

    def link(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve every stated instrument and paper identifier; idempotent; absent packs are skipped cleanly."""
        from src.kb.astronomy_citations import AstronomyCitations

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        skipped = []
        legal: dict[str, list[dict[str, Any]]] | None = None
        if legal_namespace:
            if "operator" not in scopes and LEGAL_READ not in scopes:
                raise AstronomyError("unauthorized", f"{LEGAL_READ} is required to link Legal works")
            if table_exists(self.conn, "legal_works"):
                legal = self._legal_index(legal_namespace)
            else:
                skipped.append({"pack": "legal", "reason": "the Legal pack's works are not installed"})
        else:
            skipped.append({"pack": "legal", "reason": "no Legal namespace given"})
        papers = AstronomyCitations(self.conn, initialize=False)._papers()
        if not table_exists(self.conn, "documents"):
            skipped.append({"pack": "science", "reason": "the Science pack's paper records are not installed"})
            papers = None
        created = []
        for view in self.store.visible(namespace, kinds=["registration_entry"])["records"]:
            for cite in stated_citations(view["record"]):
                if cite["target_kind"] == "legal-work":
                    if legal is None:
                        continue
                    targets = [
                        (w["work_id"], w["version_id"], "stated-instrument-identifier")
                        for w in legal.get(cite["value"], [])
                    ]
                    target_ns = legal_namespace
                else:
                    if papers is None:
                        continue
                    targets = [
                        (p["document_id"], p["revision_id"], f"stated-{cite['kind']}")
                        for p in papers.get((cite["kind"], cite["value"]), [])
                    ]
                    target_ns = None
                if not targets:
                    targets = [(None, None, "no record in the target pack states this identifier")]
                for target_id, target_revision, basis in targets:
                    link_id = "astro-reg-cite:" + digest(
                        [namespace, view["record_id"], cite["kind"], cite["value"], target_id, target_revision]
                    )[:24]
                    if self.conn.execute(
                        "SELECT 1 FROM astronomy_registration_citations WHERE namespace=? AND link_id=?",
                        [namespace, link_id],
                    ).fetchone():
                        continue
                    state = "linked" if target_id else "unresolved"
                    now = self.now()
                    self.conn.execute(
                        "INSERT INTO astronomy_registration_citations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            namespace, link_id, view["record_id"], view["revision_id"], cite["target_kind"],
                            cite["kind"], cite["value"], cite["stated"], target_ns, target_id, target_revision,
                            state, basis, principal_id, now,
                            canonical([{"state": state, "by": principal_id, "at_ms": now}]),
                        ],
                    )
                    created.append(link_id)
        return {
            "created": created,
            "skipped": skipped,
            "links": self.links(namespace, scopes=scopes),
            "policy": "links only by an identifier the registration names; no keyword or title matching",
        }

    def revert(self, namespace: str, link_id: str, reason: str, *, principal_id: str, scopes: Iterable[str]):
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise AstronomyError("invalid_decision", "a revert needs a reason")
        row = self._row(namespace, link_id)
        if row["state"] != "linked":
            raise AstronomyError("invalid_state", f"link is {row['state']}")
        history = row["history"] + [
            {"state": "reverted", "by": principal_id, "reason": reason.strip(), "at_ms": self.now()}
        ]
        self.conn.execute(
            "UPDATE astronomy_registration_citations SET state='reverted', history_json=? WHERE namespace=? "
            "AND link_id=?",
            [canonical(history), namespace, link_id],
        )
        return self._row(namespace, link_id)

    _FIELDS = (
        "link_id", "record_id", "revision_id", "target_kind", "citation_kind", "citation_value", "stated",
        "target_namespace", "target_id", "target_revision_id", "state", "basis",
    )

    def _row(self, namespace: str, link_id: str) -> dict[str, Any]:
        if not table_exists(self.conn, "astronomy_registration_citations"):
            raise AstronomyError("not_found", "no registration citation link is on record")
        row = self.conn.execute(
            "SELECT " + ", ".join(self._FIELDS) + ", history_json FROM astronomy_registration_citations "
            "WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise AstronomyError("not_found", "citation link is not on record in this namespace")
        return {
            "contract": CITATION_CONTRACT,
            **{k: v for k, v in zip(self._FIELDS, row[:-1]) if v is not None},
            "history": json.loads(row[-1]),
            "direction": "registration record -> Legal work or Science paper (their packs own them)",
        }

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return self.for_records(namespace, None if record_id is None else [record_id])

    def for_records(self, namespace: str, record_ids: Iterable[str] | None) -> list[dict[str, Any]]:
        """Links per stated citation: an active link wins; an unresolved one shows while nothing resolves it."""
        if not table_exists(self.conn, "astronomy_registration_citations"):
            return []
        rows = [
            self._row(namespace, r[0])
            for r in self.conn.execute(
                "SELECT link_id FROM astronomy_registration_citations WHERE namespace=? ORDER BY record_id, "
                "citation_kind, citation_value, link_id",
                [namespace],
            ).fetchall()
        ]
        wanted = None if record_ids is None else set(record_ids)
        rows = [r for r in rows if wanted is None or r["record_id"] in wanted]
        resolved = {(r["record_id"], r["citation_kind"], r["citation_value"]) for r in rows if r["state"] == "linked"}
        return [
            r
            for r in rows
            if r["state"] != "unresolved" or (r["record_id"], r["citation_kind"], r["citation_value"]) not in resolved
        ]

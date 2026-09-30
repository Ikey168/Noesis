"""Medicines regulation records, label diffs, as-of answers and regulatory timelines (#2214, MR02/MR08/MR10).

``noesis-clinical-medicines-record-v1`` extends the Clinical Evidence record
model (:mod:`src.kb.clinical_records`) with five record kinds. They share the
clinical record store (one authoritative store, C01.2): the same tables,
content-addressed ids, immutable revisions, native-version links, namespace
scoping, version conflicts and view invalidation. No new entity or vintage
store is introduced; substances map through :mod:`src.kb.clinical_terms`.

* ``medicinal-product`` - a product as one regulator publishes it (an EMA
  product number, a Drugs@FDA application), with its active substances and
  brand names quoted as published;
* ``marketing-authorisation`` - one dated authorisation *event* (grant,
  variation, supplement, labeling revision, suspension, withdrawal, refusal,
  marketing-status change ...) with jurisdiction, authority, procedure or
  application number, status and effective date. A status change is a new
  record, never a rewrite of an earlier one;
* ``label-revision`` - one revision of a label document (an SPL set id and
  version, an EMA product-information revision) with its sections keyed by the
  regulator's own section codes (LOINC for SPL, SmPC section numbers for EMA),
  text verbatim with a locator. Dosing sections are listed as omitted and their
  text is never retained (the pack gives no dosing advice);
* ``label-section-change`` - the section-by-section difference between two
  revisions of one label document, quoting before/after text from both,
  citing both revisions; no summary or significance rating;
* ``safety-communication`` - an FDA Drug Safety Communication with its issue
  date, the updates published on it, and the substances and products it names,
  quoted as published. An update is a new revision of the same record.

Queries (:class:`MedicinesService`) answer authorisation status and label text
as of a date per jurisdiction, what changed between two label revisions, the
communications naming a substance (with the identity match used) and a cited
regulatory timeline with label diffs and explicitly linked trials. A medicine
with no record is reported as having none on record. Nothing here is medical
advice: no prescribing, dosing or treatment advice and no efficacy or safety
verdict beyond quoting the regulator.
"""

from __future__ import annotations

import json
import re

from src.kb.clinical_records import (
    FORBIDDEN_KEYS,
    ClinicalRecordError,
    ClinicalRecordStore,
    _require_read,
    canonical,
    record_id,
)

CONTRACT = "noesis-clinical-medicines-record-v1"
TARGET_SCHEMA = CONTRACT
PROVIDERS = ("ema", "openfda", "dailymed", "fda-dsc")
RECORD_KINDS = ("medicinal-product", "marketing-authorisation", "label-revision", "label-section-change",
                "safety-communication")
AUTHORITIES = {"ema": ("EMA", "EU"), "openfda": ("FDA", "US"), "dailymed": ("FDA", "US"), "fda-dsc": ("FDA", "US")}
VERSION_BASES = ("epar-revision", "spl-version", "submission", "publication-date", "observation", "derived")
EVENT_KINDS = ("grant", "variation", "supplement", "labeling-revision", "renewal", "suspension", "withdrawal",
               "refusal", "revocation", "lapse", "not-renewed", "discontinuation", "marketing-status", "status")
STATUSES = ("authorised", "approved", "withdrawn", "suspended", "refused", "revoked", "lapsed", "not-renewed",
            "discontinued", "marketed", "pending", "unknown")
# Events that set the authorisation status (supplements and variations change a product, not its status).
STATUS_EVENTS = ("grant", "renewal", "suspension", "withdrawal", "refusal", "revocation", "lapse", "not-renewed",
                 "discontinuation", "marketing-status", "status")
CODE_SYSTEMS = ("loinc", "smpc")
DOCUMENT_KINDS = ("spl", "smpc")
REFERENCE_KINDS = ("nct", "eudract", "eu-ct", "pmid", "doi")
BOUNDARY = ("Regulatory records quoted as each regulator published them: no prescribing, dosing or treatment advice "
            "and no efficacy or safety verdict beyond quoting the regulator.")
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_KIND_FIELDS = {
    "medicinal-product": {"name", "brand_names", "active_substances", "holder", "identifiers", "products",
                          "status", "cited_references"},
    "marketing-authorisation": {"product", "procedure", "event", "submission", "cited_references"},
    "label-revision": {"product", "document", "sections", "omitted_sections", "cited_references",
                       "active_substances", "brand_names"},
    "label-section-change": {"product", "document", "from_revision", "to_revision", "changes", "unchanged",
                             "unaligned", "not_compared", "method"},
    "safety-communication": {"title", "issued", "updates", "named_substances", "named_products", "quotes",
                             "cited_references"},
}
_COMMON = {"contract", "record_kind", "unknowns", "provider", "native_id", "jurisdiction", "authority",
           "source_url", "native_version", "attribution", "disclaimer"}


def _fail(message, code="invalid_medicines_record"):
    raise ClinicalRecordError(code, message)


def _text(value, field, *, optional=False, limit=20000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty bounded text")
    return value


def _date(value, field):
    if value is not None and (not isinstance(value, str) or not _DATE.fullmatch(value)):
        _fail(f"{field} must be YYYY-MM or YYYY-MM-DD")
    return value


def _enum(value, allowed, field):
    if value not in allowed:
        _fail(f"{field} must be one of {', '.join(allowed)}")
    return value


def _forbidden(value, path="record"):
    """Keys that would turn a quote into advice or a verdict; quoted text values are never inspected."""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                _fail(f"{path}.{key}: medicines records carry no dosing, advice, grade or verdict",
                      "outside_boundary")
            _forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbidden(child, f"{path}[{index}]")


def _locator(value, field):
    if not isinstance(value, dict) or not value:
        _fail(f"{field} needs a locator (URL, JSON pointer, XPath or line range)")


def _named(items, field):
    for item in items or []:
        if not isinstance(item, dict) or set(item) - {"name", "role", "locator"}:
            _fail(f"{field} use name/role/locator")
        _text(item.get("name"), f"{field}.name", limit=500)


def _references(items):
    for item in items or []:
        if not isinstance(item, dict) or set(item) - {"kind", "value", "citing_text", "locator"}:
            _fail("cited references use kind/value/citing_text/locator")
        _enum(item.get("kind"), REFERENCE_KINDS, "cited_references.kind")
        _text(item.get("value"), "cited_references.value", limit=500)
        _text(item.get("citing_text"), "cited_references.citing_text", limit=4000)
        _locator(item.get("locator"), "cited_references.locator")


def _ref(value, field):
    if not isinstance(value, dict) or set(value) != {"provider", "native_id"}:
        _fail(f"{field} names the product record by provider and native_id")


def _sections(items, field):
    seen = set()
    for item in items or []:
        if not isinstance(item, dict) or set(item) - {"code", "code_system", "title", "text", "locator", "parent"}:
            _fail(f"{field} use code/code_system/title/text/locator/parent")
        _enum(item.get("code_system"), CODE_SYSTEMS, f"{field}.code_system")
        _text(item.get("text"), f"{field}.text", limit=200000)
        _locator(item.get("locator"), f"{field}.locator")
        key = (item.get("code"), item.get("title"))
        if item.get("code") is not None and key in seen:
            _fail(f"{field}: section {item.get('code')} appears twice in one revision")
        seen.add(key)


def validate_extension_record(record):
    """Validate a medicines record and return a canonical copy with ``unknowns`` recomputed."""
    if record.get("contract") != CONTRACT:
        _fail("unsupported medicines record contract", "schema_drift")
    kind = _enum(record.get("record_kind"), RECORD_KINDS, "record_kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    _forbidden({k: v for k, v in record.items()})
    copy = json.loads(canonical(record))
    provider = _enum(copy.get("provider"), PROVIDERS, "provider")
    authority, jurisdiction = AUTHORITIES[provider]
    if copy.get("authority") != authority or copy.get("jurisdiction") != jurisdiction:
        _fail(f"{provider} records carry authority {authority} and jurisdiction {jurisdiction}")
    _text(copy.get("native_id"), "native_id", limit=500)
    if not isinstance(copy.get("source_url"), str) or not copy["source_url"].startswith("https://"):
        _fail("source_url must be an exact HTTPS source locator")
    version = copy.get("native_version")
    if not isinstance(version, dict) or set(version) - {"version", "date", "basis"}:
        _fail("native_version uses version/date/basis")
    if version.get("version") is not None and not isinstance(version["version"], str):
        _fail("native_version.version is a string")
    _date(version.get("date"), "native_version.date")
    _enum(version.get("basis"), VERSION_BASES, "native_version.basis")
    if provider == "openfda":
        disclaimer = copy.get("disclaimer")
        if not isinstance(disclaimer, dict) or not str(disclaimer.get("text") or "").strip():
            _fail("openFDA records keep openFDA's own disclaimer on the record", "missing_disclaimer")
    _KIND_VALIDATORS[kind](copy)
    copy["unknowns"] = _unknowns(copy)
    return copy


def _product(record):
    _text(record.get("name"), "name", limit=1000)
    _named(record.get("active_substances"), "active_substances")
    _named(record.get("brand_names"), "brand_names")
    for item in record.get("identifiers") or []:
        if not isinstance(item, dict) or set(item) - {"kind", "value", "locator"}:
            _fail("identifiers use kind/value/locator")
    for item in record.get("products") or []:
        if not isinstance(item, dict) or set(item) - {"product_number", "brand_name", "marketing_status", "locator"}:
            _fail("products use product_number/brand_name/marketing_status/locator")
    status = record.get("status")
    if status is not None:
        if not isinstance(status, dict) or set(status) - {"native", "normalized", "locator"}:
            _fail("status uses native/normalized/locator")
        _enum(status.get("normalized", "unknown"), STATUSES, "status.normalized")
    _references(record.get("cited_references"))


def _authorisation(record):
    _ref(record.get("product"), "product")
    procedure = record.get("procedure")
    if not isinstance(procedure, dict) or set(procedure) - {"kind", "number"} or not procedure.get("number"):
        _fail("procedure names the application or procedure number")
    _enum(procedure.get("kind"), ("application", "procedure", "product-number"), "procedure.kind")
    event = record.get("event")
    if not isinstance(event, dict) or set(event) - {"kind", "native_status", "status", "effective_date", "reason",
                                                    "locator"}:
        _fail("event uses kind/native_status/status/effective_date/reason/locator")
    _enum(event.get("kind"), EVENT_KINDS, "event.kind")
    _enum(event.get("status"), STATUSES, "event.status")
    _date(event.get("effective_date"), "event.effective_date")
    _locator(event.get("locator"), "event.locator")
    reason = event.get("reason")
    if reason is not None and (not isinstance(reason, dict) or set(reason) - {"text", "locator"}
                               or not str(reason.get("text") or "").strip()):
        _fail("event.reason quotes the regulator's text with a locator")
    submission = record.get("submission")
    if submission is not None and (not isinstance(submission, dict) or set(submission) - {
            "type", "number", "status", "status_date", "class_code", "class_description", "locator"}):
        _fail("submission uses type/number/status/status_date/class_code/class_description/locator")
    _references(record.get("cited_references"))


def _label(record):
    _ref(record.get("product"), "product")
    document = record.get("document")
    if not isinstance(document, dict) or set(document) - {"kind", "id", "version", "effective_date", "url", "title",
                                                          "revision_date"}:
        _fail("document uses kind/id/version/effective_date/url/title/revision_date")
    _enum(document.get("kind"), DOCUMENT_KINDS, "document.kind")
    _text(document.get("id"), "document.id", limit=500)
    _text(document.get("version"), "document.version", limit=100)
    _date(document.get("effective_date"), "document.effective_date")
    _date(document.get("revision_date"), "document.revision_date")
    expected = "loinc" if document["kind"] == "spl" else "smpc"
    _sections(record.get("sections"), "sections")
    if any(s["code_system"] != expected for s in record.get("sections") or []):
        _fail(f"{document['kind']} sections are keyed by {expected} section codes")
    for item in record.get("omitted_sections") or []:
        if not isinstance(item, dict) or set(item) - {"code", "code_system", "title", "reason"} or "text" in item:
            _fail("omitted sections list code/code_system/title/reason and never their text")
    _named(record.get("active_substances"), "active_substances")
    _named(record.get("brand_names"), "brand_names")
    _references(record.get("cited_references"))


def _change(record):
    _ref(record.get("product"), "product")
    for key in ("from_revision", "to_revision"):
        side = record.get(key)
        if not isinstance(side, dict) or set(side) - {"record_id", "revision", "version", "effective_date",
                                                      "source_url"} or not side.get("record_id"):
            _fail(f"{key} cites the label-revision record, its revision and version")
    for change in record.get("changes") or []:
        if not isinstance(change, dict) or set(change) - {"change", "code", "code_system", "title", "before", "after"}:
            _fail("changes use change/code/code_system/title/before/after")
        _enum(change.get("change"), ("added", "removed", "changed"), "changes.change")
        for side in ("before", "after"):
            quoted = change.get(side)
            if quoted is not None:
                if not isinstance(quoted, dict) or set(quoted) - {"text", "title", "locator"}:
                    _fail("quoted sides use text/title/locator")
                _locator(quoted.get("locator"), f"changes.{side}.locator")
        if (change["change"] != "added") != (change.get("before") is not None) or (
                change["change"] != "removed") != (change.get("after") is not None):
            _fail("added sections quote only after, removed only before, changed both")


def _communication(record):
    _text(record.get("title"), "title", limit=2000)
    _date(record.get("issued"), "issued")
    for update in record.get("updates") or []:
        if not isinstance(update, dict) or set(update) - {"date", "text", "locator"}:
            _fail("updates use date/text/locator")
        _date(update.get("date"), "updates.date")
        _text(update.get("text"), "updates.text", limit=20000)
        _locator(update.get("locator"), "updates.locator")
    _named(record.get("named_substances"), "named_substances")
    _named(record.get("named_products"), "named_products")
    for quote in record.get("quotes") or []:
        if not isinstance(quote, dict) or set(quote) - {"text", "locator", "section"}:
            _fail("quotes use text/locator/section")
        _text(quote.get("text"), "quotes.text", limit=20000)
        _locator(quote.get("locator"), "quotes.locator")
    _references(record.get("cited_references"))


_KIND_VALIDATORS = {"medicinal-product": _product, "marketing-authorisation": _authorisation,
                    "label-revision": _label, "label-section-change": _change,
                    "safety-communication": _communication}


def _unknowns(record):
    kind, unknowns = record["record_kind"], []
    if kind == "marketing-authorisation":
        if not record["event"].get("effective_date"):
            unknowns.append("event.effective_date")
        if record["event"]["status"] == "unknown":
            unknowns.append("event.status")
    elif kind == "label-revision" and not record["document"].get("effective_date"):
        unknowns.append("document.effective_date")
    elif kind == "safety-communication" and not record.get("issued"):
        unknowns.append("issued")
    elif kind == "medicinal-product" and not record.get("active_substances"):
        unknowns.append("active_substances")
    return sorted(unknowns)


def record(record_kind, **fields):
    """Build and validate one medicines record (for adapters and fixtures)."""
    return validate_extension_record({"contract": CONTRACT, "record_kind": record_kind, **fields})


def event_key(item):
    submission = item.get("submission")
    if submission:
        return f"submission:{submission.get('type')}:{submission.get('number')}"
    event = item["event"]
    return f"{event['kind']}:{event.get('native_status') or event['status']}:{event.get('effective_date') or 'undated'}"


def extension_identity(item):
    kind, provider, native_id = item["record_kind"], item["provider"], item["native_id"]
    if kind == "medicinal-product":
        return (provider, native_id, "product")
    if kind == "marketing-authorisation":
        return (provider, native_id, "authorisation:" + event_key(item))
    if kind == "label-revision":
        return (provider, native_id, f"label:{item['document']['id']}:{item['document']['version']}")
    if kind == "label-section-change":
        return (provider, native_id, f"diff:{item['document']['id']}:{item['from_revision']['record_id']}@"
                                     f"{item['from_revision']['revision']}->{item['to_revision']['record_id']}@"
                                     f"{item['to_revision']['revision']}")
    return (provider, native_id, "dsc")


def extension_amendments(before, after):
    changes = []
    if canonical(before.get("native_version")) != canonical(after.get("native_version")):
        changes.append({"kind": "new_native_version", "path": "native_version",
                        "before": before.get("native_version"), "after": after.get("native_version")})
    for path in ("status", "event", "sections", "products", "submission", "named_substances", "active_substances"):
        if canonical(before.get(path)) != canonical(after.get(path)):
            changes.append({"kind": f"{path}_change", "path": path})
    old = {canonical(u) for u in before.get("updates") or []}
    added = [u for u in after.get("updates") or [] if canonical(u) not in old]
    if added:
        changes.append({"kind": "communication_updated", "path": "updates", "added": added})
    if not changes:
        changes.append({"kind": "descriptive_change", "path": "record"})
    return changes


# ------------------------------------------------------------------ projection


class MedicinesProjector:
    """Source-pack projector for ``noesis-clinical-medicines-record-v1``: pages go into the clinical record store."""

    def __init__(self, conn) -> None:
        from src.kb.clinical_records import ClinicalProjector

        self.conn = conn
        self.clinical = ClinicalProjector(conn)
        self.store = self.clinical.store

    @staticmethod
    def state_provider(source):
        config = source.get("medicines") or {}
        return str(config.get("provider") or ("drugs-at-fda" if source["connector"] == "openfda" else source["connector"]))

    @staticmethod
    def _namespace(source, item):
        config = source.get("medicines") or source.get("clinical") or {}
        return str(item.get("clinical_namespace") or config.get("namespace"))

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del documents, principal_id
        provider = self.state_provider(source)
        summaries = []
        for item in records:
            medicines = item.get("clinical_records") or []
            if not medicines:
                continue
            evidence = {
                "source_pack": {"pack_id": manifest["pack_id"], "version": manifest["version"],
                                "manifest_hash": manifest["manifest_hash"], "source_id": source["source_id"],
                                "run_id": run_id},
                "document": self.clinical._document_revision(source["source_id"], item.get("id")),
                "capture": item.get("clinical_capture") or {},
            }
            summary = self.store._ingest(
                self._namespace(source, item), provider, list(medicines),
                observation_id=f"{run_id}:{source['source_id']}:{item.get('id')}", observed_at_ms=self.store.now(),
                evidence=evidence, execution=(page_receipt or {}).get("execution") or "unrecorded")
            summaries.append({k: summary[k] for k in ("created", "revised", "unchanged", "conflicts")})
        return {"pages": len(summaries), "records": summaries}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        if status != "complete":
            row = self.conn.execute(
                "SELECT failure_json FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                [run_id, source["source_id"]]).fetchone()
            code = (json.loads(row[0]) or {}).get("code") if row and row[0] else "source_failed"
            self.store.record_failure(self._namespace(source, {}), self.state_provider(source), observation_id=run_id,
                                      failure_code=code, observed_at_ms=self.store.now(), internal=True)
        return {"status": status}


# ------------------------------------------------------------------ label diffs (MR08)


def _natural(code):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(code or ""))]


def diff_sections(before, after):
    """Section-by-section difference of two revisions of one label document (pure, deterministic)."""
    if before["document"]["kind"] != after["document"]["kind"] or before["document"]["id"] != after["document"]["id"]:
        raise ClinicalRecordError("not_comparable", "only two revisions of the same label document are compared")

    def index(item):
        aligned, unaligned = {}, []
        for section in item.get("sections") or []:
            if not section.get("code"):
                unaligned.append(section)
            elif section["code"] in aligned:
                unaligned.append(section)
            else:
                aligned[section["code"]] = section
        return aligned, unaligned

    left, left_loose = index(before)
    right, right_loose = index(after)
    changes, unchanged = [], []
    for code in sorted(set(left) | set(right), key=_natural):
        old, new = left.get(code), right.get(code)

        def quote(section):
            return {"text": section["text"], "title": section.get("title"), "locator": section["locator"]}

        if old is None:
            changes.append({"change": "added", "code": code, "code_system": new["code_system"],
                            "title": new.get("title"), "before": None, "after": quote(new)})
        elif new is None:
            changes.append({"change": "removed", "code": code, "code_system": old["code_system"],
                            "title": old.get("title"), "before": quote(old), "after": None})
        elif old["text"] != new["text"] or old.get("title") != new.get("title"):
            changes.append({"change": "changed", "code": code, "code_system": new["code_system"],
                            "title": new.get("title"), "before": quote(old), "after": quote(new)})
        else:
            unchanged.append(code)
    unaligned = [{"side": side, "code": s.get("code"), "code_system": s["code_system"], "title": s.get("title"),
                  "locator": s["locator"], "reason": "no regulator section code, or the code repeats in the revision;"
                                                     " not aligned and not compared"}
                 for side, loose in (("from", left_loose), ("to", right_loose)) for s in loose]
    omitted = {}
    for side, item in (("from", before), ("to", after)):
        for section in item.get("omitted_sections") or []:
            omitted.setdefault(section.get("code") or section.get("title"), {**section, "sides": []})["sides"].append(
                side)
    not_compared = [{"code": o.get("code"), "title": o.get("title"), "sides": o["sides"],
                     "reason": "section text is not retained (dosing is outside the pack's non-advice boundary)"}
                    for _, o in sorted(omitted.items(), key=lambda kv: _natural(kv[0]))]
    return {"changes": changes, "unchanged": unchanged, "unaligned": unaligned, "not_compared": not_compared}


# ------------------------------------------------------------------ queries (MR10)


def _on_or_before(value, as_of):
    """Whether a YYYY-MM or YYYY-MM-DD date is on or before ``as_of`` (a month counts from its first day)."""
    if not value:
        return False
    return (value if len(value) == 10 else value + "-01") <= as_of


class MedicinesService:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.records = ClinicalRecordStore(conn, initialize=initialize, now=now)
        self.now = self.records.now

    # ------------------------------------------------------------- helpers

    def _rows(self, namespace, scopes, kinds=None):
        return [r for r in self.records.find(namespace, scopes=scopes, kinds=kinds or set(RECORD_KINDS), limit=10000)
                if r["record"].get("contract") == CONTRACT]

    def _cite(self, namespace, row, scopes):
        current = self.records.get(namespace, row["record_id"], scopes=scopes)
        item = current["record"]
        return {"record_id": row["record_id"], "revision": current["revision"], "provider": item["provider"],
                "native_id": item["native_id"], "authority": item["authority"], "source_url": item["source_url"],
                "native_version": item["native_version"], "observed_at_ms": current["observed_at_ms"],
                "attribution": item.get("attribution"), "disclaimer": (item.get("disclaimer") or {}).get("text")}

    def reach(self, namespace, medicine, *, scopes):
        """The medicines records a medicine or substance reaches through reviewed identity matches."""
        from src.kb.clinical_terms import MedicineIdentity

        _require_read(namespace, scopes)
        resolved = MedicineIdentity(self.conn, initialize=False, now=self.now).resolve(namespace, medicine,
                                                                                         scopes=scopes)
        by_subject = {s["provider"] + "\x00" + s["native_id"]: s for s in resolved["subjects"]}
        rows = [r for r in self._rows(namespace, scopes)
                if r["provider"] + "\x00" + r["native_id"] in by_subject]
        return resolved, rows, by_subject

    def _none(self, medicine, resolved, what):
        return {"medicine": medicine, "on_record": False, what: [], "identity": resolved,
                "message": f"No regulatory record of {medicine!r} is on record in this namespace.",
                "boundary": BOUNDARY}

    # ------------------------------------------------------------- status

    def status_as_of(self, namespace, medicine, as_of, *, scopes, jurisdiction=None):
        """Per jurisdiction and product: the authorisation event in force on the date, cited."""
        _date(as_of, "as_of")
        resolved, rows, subjects = self.reach(namespace, medicine, scopes=scopes)
        events = [r for r in rows if r["record_kind"] == "marketing-authorisation"
                  and (jurisdiction is None or r["record"]["jurisdiction"] == jurisdiction)]
        if not events:
            return self._none(medicine, resolved, "jurisdictions")
        grouped: dict[tuple, list] = {}
        for row in events:
            item = row["record"]
            grouped.setdefault((item["jurisdiction"], item["provider"], item["native_id"]), []).append(row)
        answers = []
        for (juris, provider, native_id), group in sorted(grouped.items()):
            dated = [r for r in group if r["record"]["event"]["kind"] in STATUS_EVENTS
                     and _on_or_before(r["record"]["event"].get("effective_date"), as_of)]
            dated.sort(key=lambda r: (r["record"]["event"]["effective_date"] + "-01")[:10])
            changes = sorted((r for r in group if r["record"]["event"]["kind"] not in STATUS_EVENTS
                              and _on_or_before(r["record"]["event"].get("effective_date"), as_of)),
                             key=lambda r: r["record"]["event"]["effective_date"])
            undated = [r for r in group if not r["record"]["event"].get("effective_date")]
            current = dated[-1] if dated else None
            answers.append({
                "jurisdiction": juris, "authority": group[0]["record"]["authority"], "provider": provider,
                "native_id": native_id,
                "procedure": group[0]["record"]["procedure"],
                "status": current["record"]["event"]["status"] if current else None,
                "event": current["record"]["event"] if current else None,
                "citation": self._cite(namespace, current, scopes) if current else None,
                "later_changes_before_date": [{"event": r["record"]["event"], "submission": r["record"].get(
                    "submission"), "citation": self._cite(namespace, r, scopes)} for r in changes],
                "undated_events": [{"event": r["record"]["event"], "citation": self._cite(namespace, r, scopes)}
                                   for r in undated],
                "note": None if current else "no dated authorisation event on or before this date is on record",
                "identity_match": subjects[provider + "\x00" + native_id]["match"]})
        return {"medicine": medicine, "as_of": as_of, "on_record": True, "jurisdictions": answers,
                "identity": resolved, "boundary": BOUNDARY}

    # ------------------------------------------------------------- labels

    def label_as_of(self, namespace, medicine, as_of, *, scopes, provider=None):
        """Per label document, the revision in force on the date (latest effective date on or before it)."""
        _date(as_of, "as_of")
        resolved, rows, subjects = self.reach(namespace, medicine, scopes=scopes)
        labels = [r for r in rows if r["record_kind"] == "label-revision"
                  and (provider is None or r["provider"] == provider)]
        if not labels:
            return self._none(medicine, resolved, "labels")
        documents: dict[tuple, list] = {}
        for row in labels:
            documents.setdefault((row["provider"], row["record"]["document"]["id"]), []).append(row)
        answers = []
        for (prov, document_id), group in sorted(documents.items()):
            effective = sorted((r for r in group if _on_or_before(r["record"]["document"].get("effective_date"),
                                                                   as_of)),
                               key=lambda r: (r["record"]["document"]["effective_date"], _natural(
                                   r["record"]["document"]["version"])))
            chosen = effective[-1] if effective else None
            item = chosen["record"] if chosen else None
            answers.append({
                "provider": prov, "document": item["document"] if item else {"id": document_id},
                "sections": item["sections"] if item else [], "omitted_sections": item.get("omitted_sections")
                if item else [], "citation": self._cite(namespace, chosen, scopes) if chosen else None,
                "revisions_on_record": sorted(r["record"]["document"]["version"] for r in group),
                "note": None if chosen else "no revision of this label was in force on this date on record",
                "identity_match": subjects[prov + "\x00" + group[0]["native_id"]]["match"]})
        return {"medicine": medicine, "as_of": as_of, "on_record": True, "labels": answers, "identity": resolved,
                "boundary": BOUNDARY}

    def diff(self, namespace, left_record_id, right_record_id, *, scopes, observation_id=None):
        """Section changes between two revisions of one label; stored as a derived record and reused."""
        _require_read(namespace, scopes)  # a derived, reused cache of two held revisions
        left = self.records.get(namespace, left_record_id, scopes=scopes)
        right = self.records.get(namespace, right_record_id, scopes=scopes)
        for side in (left, right):
            if side["record"].get("record_kind") != "label-revision":
                raise ClinicalRecordError("not_a_label", "label diffs compare label-revision records")
        if (left["record"]["document"].get("effective_date") or "", _natural(left["record"]["document"]["version"])) > (
                right["record"]["document"].get("effective_date") or "",
                _natural(right["record"]["document"]["version"])):
            left, right = right, left
        return self._store_diff(namespace, left, right, observation_id=observation_id)

    def _store_diff(self, namespace, left, right, *, observation_id=None):
        before, after = left["record"], right["record"]
        result = diff_sections(before, after)

        def side(current):
            item = current["record"]
            return {"record_id": current["record_id"], "revision": current["revision"],
                    "version": item["document"]["version"], "effective_date": item["document"].get("effective_date"),
                    "source_url": item["source_url"]}

        change = record(
            "label-section-change", provider=after["provider"], native_id=after["native_id"],
            jurisdiction=after["jurisdiction"], authority=after["authority"], source_url=after["source_url"],
            native_version={"version": f"{before['document']['version']}->{after['document']['version']}",
                            "date": after["document"].get("effective_date"), "basis": "derived"},
            product=after["product"], document={k: after["document"][k] for k in ("kind", "id", "version")},
            from_revision=side(left), to_revision=side(right), changes=result["changes"],
            unchanged=result["unchanged"], unaligned=result["unaligned"], not_compared=result["not_compared"],
            method="aligned by regulator section code (LOINC for SPL, SmPC section number for EMA); text compared "
                   "verbatim; no summary or significance rating",
            **({"disclaimer": after["disclaimer"]} if after.get("disclaimer") else {}))
        rid = record_id(namespace, change)
        summary = self.records._ingest(namespace, "noesis", [change],
                                       observation_id=observation_id or "medicines-diff:" + rid,
                                       observed_at_ms=self.now(), evidence={"derived_from": [
                                           change["from_revision"], change["to_revision"]]}, execution="derived")
        stored = self.records.get(namespace, rid, scopes={"operator"})
        return {"record_id": rid, "revision": stored["revision"], "reused": rid in summary["unchanged"],
                **{k: change[k] for k in ("document", "from_revision", "to_revision", "changes", "unchanged",
                                          "unaligned", "not_compared", "method")},
                "boundary": BOUNDARY}

    def what_changed(self, namespace, medicine, *, scopes, document_id=None, from_version=None, to_version=None):
        """Section diffs between chosen (default: consecutive) revisions of a medicine's label documents."""
        resolved, rows, _ = self.reach(namespace, medicine, scopes=scopes)
        labels = [r for r in rows if r["record_kind"] == "label-revision"
                  and (document_id is None or r["record"]["document"]["id"] == document_id)]
        if not labels:
            return self._none(medicine, resolved, "diffs")
        documents: dict[tuple, list] = {}
        for row in labels:
            documents.setdefault((row["provider"], row["record"]["document"]["id"]), []).append(row)
        diffs = []
        for _, group in sorted(documents.items()):
            group.sort(key=lambda r: (r["record"]["document"].get("effective_date") or "",
                                      _natural(r["record"]["document"]["version"])))
            pairs = list(zip(group, group[1:]))
            if from_version is not None and to_version is not None:
                by_version = {r["record"]["document"]["version"]: r for r in group}
                if from_version not in by_version or to_version not in by_version:
                    continue
                pairs = [(by_version[from_version], by_version[to_version])]
            for left, right in pairs:
                diffs.append(self._store_diff(namespace, self.records.get(namespace, left["record_id"], scopes=scopes),
                                              self.records.get(namespace, right["record_id"], scopes=scopes)))
        return {"medicine": medicine, "on_record": True, "diffs": diffs, "identity": resolved, "boundary": BOUNDARY}

    # ------------------------------------------------------------- communications

    def communications(self, namespace, substance, *, scopes):
        """Safety communications naming a substance, with the identity match that connected each."""
        resolved, rows, subjects = self.reach(namespace, substance, scopes=scopes)
        found = []
        for row in rows:
            if row["record_kind"] != "safety-communication":
                continue
            item = row["record"]
            history = self.records.history(namespace, row["record_id"], scopes=scopes)["revisions"]
            found.append({"title": item["title"], "issued": item.get("issued"), "updates": item.get("updates") or [],
                          "named_substances": item.get("named_substances") or [],
                          "named_products": item.get("named_products") or [], "quotes": item.get("quotes") or [],
                          "citation": self._cite(namespace, row, scopes),
                          "revisions": [{"revision": h["revision"], "version_key": h["version_key"],
                                         "observed_at_ms": h["observed_at_ms"]} for h in history],
                          "identity_match": subjects[row["provider"] + "\x00" + row["native_id"]]["match"]})
        if not found:
            return self._none(substance, resolved, "communications")
        found.sort(key=lambda c: (c["issued"] or "9999", c["citation"]["native_id"]))
        return {"substance": substance, "on_record": True, "communications": found, "identity": resolved,
                "boundary": BOUNDARY}

    # ------------------------------------------------------------- timeline

    def timeline(self, namespace, medicine, *, scopes, view_id=None):
        """Every regulatory event of a medicine in date order, sources side by side, with diffs and cited trials."""
        resolved, rows, subjects = self.reach(namespace, medicine, scopes=scopes)
        if not rows:
            return self._none(medicine, resolved, "events")
        events, pins = [], {}
        labels: dict[tuple, list] = {}
        for row in rows:
            item = row["record"]
            cite = self._cite(namespace, row, scopes)
            pins[row["record_id"]] = cite["revision"]
            match = subjects[row["provider"] + "\x00" + row["native_id"]]["match"]
            base = {"jurisdiction": item["jurisdiction"], "authority": item["authority"], "citation": cite,
                    "identity_match": match}
            if row["record_kind"] == "marketing-authorisation":
                events.append({**base, "date": item["event"].get("effective_date"), "kind": "authorisation",
                               "event": item["event"], "submission": item.get("submission"),
                               "procedure": item["procedure"]})
            elif row["record_kind"] == "label-revision":
                labels.setdefault((row["provider"], item["document"]["id"]), []).append(row)
                events.append({**base, "date": item["document"].get("effective_date"), "kind": "label-revision",
                               "document": item["document"]})
            elif row["record_kind"] == "safety-communication":
                events.append({**base, "date": item.get("issued"), "kind": "safety-communication",
                               "title": item["title"], "named_substances": item.get("named_substances") or []})
                for update in item.get("updates") or []:
                    events.append({**base, "date": update["date"], "kind": "safety-communication-update",
                                   "title": item["title"], "update": update})
        for _, group in sorted(labels.items()):
            group.sort(key=lambda r: (r["record"]["document"].get("effective_date") or "",
                                      _natural(r["record"]["document"]["version"])))
            for left, right in zip(group, group[1:]):
                diff = self._store_diff(namespace, self.records.get(namespace, left["record_id"], scopes=scopes),
                                        self.records.get(namespace, right["record_id"], scopes=scopes))
                for event in events:
                    if event["kind"] == "label-revision" and event["citation"]["record_id"] == right["record_id"]:
                        event["section_changes"] = {"record_id": diff["record_id"], "changes": diff["changes"],
                                                    "from_revision": diff["from_revision"],
                                                    "unaligned": diff["unaligned"],
                                                    "not_compared": diff["not_compared"]}
        trials, publications, faers = self._links(namespace, rows)
        events.sort(key=lambda e: ((e["date"] or "9999") + "-01")[:10] + e["kind"] + e["citation"]["record_id"])
        dated = [e for e in events if e["date"]]
        undated = [e for e in events if not e["date"]]
        if view_id:
            self.records.register_view(namespace, view_id, "clinical-medicines-timeline", pins)
        return {"medicine": medicine, "on_record": True, "events": dated, "undated_events": undated,
                "linked_trials": trials, "linked_publications": publications, "faers_reporting_counts": faers,
                "sources": sorted({(e["authority"], e["citation"]["provider"]) for e in events}),
                "identity": resolved, "view_id": view_id, "boundary": BOUNDARY}

    def _links(self, namespace, rows):
        trials, publications, faers, seen = [], [], [], set()
        for row in rows:
            for link in self.records.links(namespace, provider=row["provider"], identifier_value=row["native_id"]):
                if link["link_id"] in seen or link["status"] not in {"accepted", "target-not-acquired"}:
                    continue
                seen.add(link["link_id"])
                entry = {"link_id": link["link_id"], "to": link["to"], "status": link["status"],
                         "evidence_kind": link["evidence_kind"], "evidence": link["evidence"],
                         "from": link["from_record"]}
                if link["link_kind"] == "medicine-trial":
                    trials.append(entry)
                elif link["link_kind"] == "medicine-publication":
                    publications.append(entry)
                elif link["link_kind"] == "medicine-faers" and link["status"] == "accepted":
                    faers.append(entry)
        return trials, publications, faers


def feature_enabled(conn, namespace=None):
    """Whether the Clinical Evidence bundle's optional ``medicines`` feature is selected in the active plan."""
    del namespace  # composition selection is deployment-wide
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='clinical-evidence'"
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
    return "medicines" in ((plan.get("features") or {}).get("clinical-evidence") or [])


__all__ = ["BOUNDARY", "CONTRACT", "MedicinesProjector", "MedicinesService", "diff_sections", "record",
           "validate_extension_record"]

"""Products pack store: identities, source assertions, matching, refresh and comparison.

Provider records arrive through the source-pack runtime (``noesis-product-record-v1``
from :mod:`src.ingestion.product_sources`) and are projected here before the
page checkpoint advances. The store never invents a canonical product from a
title and never merges providers on its own:

* **Identity.** Each provider record is a *variant* (provider + native record
  ID, with its market). Variants group under a per-provider *model* keyed by
  brand and exact designation (MPN/model identifier, original string kept),
  and optionally under a *family* the provider declares. Identifiers are kept
  verbatim with their validation state (absent, invalid, conflicting).
* **Assertions.** Every attribute value is a source assertion bound to an
  immutable record revision, with native name/value/unit, JSON-pointer
  locator, measurement mode, label scheme and assertion kind (brand content or
  supplier registration, never independent testing). Normalized values come
  from the shared quantitative store with a calculation receipt.
* **Matching.** Cross-provider equivalence is a reviewable candidate built from
  identifiers and corroborating attributes; similar names alone never qualify.
  Review decisions are append-only and reversible.
* **Refresh.** Only an explicit provider status withdraws a record. Absence
  from a partial, filtered, failed or entitlement-limited response, or from a
  narrowed selection, never implies discontinuation. A completed-refresh
  marker advances only when every selected model of a complete run projected.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from src.ingestion.product_sources import RECORD_CONTRACT

READ_SCOPE = "knowledge:products:read"
WRITE_SCOPE = "knowledge:products:write"
REVIEW_SCOPE = "knowledge:products:review"
IDENTITY_CONTRACT = "noesis-product-identity-v1"
MATCH_CONTRACT = "noesis-product-match-v1"
COMPARISON_CONTRACT = "noesis-product-comparison-v1"
DOCUMENT_CONTRACT = "noesis-product-document-v1"
NORMALIZATION_VERSION = "products-display-normalization-v1"
MATCH_METHOD = "identifier-and-attribute-corroboration-v1"
DEFAULT_NAMESPACE = "global"
MATCH_DECISIONS = frozenset({"accepted", "rejected", "deferred"})
# Attribute -> (target unit, quantitative dimension)
NORMALIZED_UNITS = {
    "diagonal": "cm",
    "width": "mm",
    "height": "mm",
    "depth": "mm",
    "on_mode_power": "W",
    "energy_consumption_1000h": "kWh",
}
# Decimal places kept after conversion: providers publish diagonals in 0.1 cm
# and whole inches, so 27 in (68.58 cm) and 68.6 cm are the same declaration.
NORMALIZED_PRECISION = {
    "diagonal": 1, "width": 0, "height": 0, "depth": 0, "on_mode_power": 1, "energy_consumption_1000h": 1,
}
PRODUCT_UNITS = {
    "cm": ({"length": 1}, "0.01", ["centimetre", "centimeter"]),
    "mm": ({"length": 1}, "0.001", ["millimetre", "millimeter"]),
    "in": ({"length": 1}, "0.0254", ["inch", '"']),
    "W": ({"length": 2, "mass": 1, "time": -3}, "1", ["watt"]),
    "kWh": ({"length": 2, "mass": 1, "time": -2}, "3600000", ["kilowatt hour"]),
}
DIAGONAL_TOLERANCE_CM = Decimal("1.5")

_DDL = """
CREATE TABLE IF NOT EXISTS product_identities (
  identity_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, level TEXT NOT NULL,
  parent_id TEXT, provider TEXT, brand TEXT, designation TEXT, family TEXT,
  provider_record_id TEXT, market_json TEXT NOT NULL, identifiers_json TEXT NOT NULL,
  category_label TEXT, created_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, variant_id TEXT NOT NULL,
  provider TEXT NOT NULL, provider_record_id TEXT NOT NULL, provider_revision TEXT,
  order_key TEXT NOT NULL, raw_sha256 TEXT NOT NULL, record_json TEXT NOT NULL,
  record_status TEXT NOT NULL, document_id TEXT, source_id TEXT NOT NULL,
  first_run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_revision_observations (
  revision_id TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(revision_id, run_id)
);
CREATE TABLE IF NOT EXISTS product_current (
  variant_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, revision_id TEXT NOT NULL,
  record_state TEXT NOT NULL, state_evidence_revision TEXT, last_seen_run_id TEXT NOT NULL,
  last_scope_hash TEXT, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_assertions (
  assertion_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, variant_id TEXT NOT NULL,
  revision_id TEXT NOT NULL, provider TEXT NOT NULL, attribute TEXT NOT NULL, mode TEXT,
  native_name TEXT NOT NULL, native_value TEXT, native_unit TEXT,
  normalized_value TEXT, normalized_unit TEXT, normalization_state TEXT NOT NULL,
  calculation_id TEXT, conditions_json TEXT NOT NULL, assertion_kind TEXT NOT NULL,
  locator_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_identity_conflicts (
  namespace TEXT NOT NULL, identifier_kind TEXT NOT NULL, identifier_value TEXT NOT NULL,
  variant_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, identifier_kind, identifier_value, variant_id)
);
CREATE SEQUENCE IF NOT EXISTS product_coverage_seq;
CREATE TABLE IF NOT EXISTS product_coverage (
  seq BIGINT NOT NULL, namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  selection_index INTEGER NOT NULL, selector_json TEXT NOT NULL, scope_hash TEXT NOT NULL,
  outcome TEXT NOT NULL, response_sha256 TEXT, variant_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(run_id, source_id, selection_index)
);
CREATE TABLE IF NOT EXISTS product_refresh (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, scope_hash TEXT NOT NULL,
  selection_size INTEGER NOT NULL, completed_run_id TEXT NOT NULL, completed_at_ms BIGINT NOT NULL,
  coverage_watermark BIGINT NOT NULL, PRIMARY KEY(namespace, source_id)
);
CREATE TABLE IF NOT EXISTS product_refresh_history (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
  scope_hash TEXT, projected INTEGER NOT NULL, selection_size INTEGER, advanced BOOLEAN NOT NULL,
  reason TEXT, recorded_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, run_id)
);
CREATE TABLE IF NOT EXISTS product_document_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, variant_id TEXT NOT NULL,
  first_revision_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, language TEXT,
  media_type TEXT, declared_json TEXT NOT NULL, locator_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_document_fetches (
  fetch_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, namespace TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, http_status INTEGER, final_url TEXT,
  content_sha256 TEXT, media_type TEXT, bytes BIGINT, principal_id TEXT NOT NULL,
  fetched_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_document_assets (
  content_sha256 TEXT PRIMARY KEY, media_type TEXT, bytes BIGINT NOT NULL,
  extraction_state TEXT NOT NULL, extracted_chars INTEGER, first_fetched_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_matches (
  match_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, left_id TEXT NOT NULL, right_id TEXT NOT NULL,
  method TEXT NOT NULL, candidate_state TEXT NOT NULL, confidence TEXT NOT NULL,
  evidence_json TEXT NOT NULL, reasons_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_match_reviews (
  review_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, namespace TEXT NOT NULL, sequence INTEGER NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, principal_id TEXT NOT NULL, reviewed_at_ms BIGINT NOT NULL
);
"""


class ProductError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def _brand_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def designation_key(value: Any) -> str:
    """Compare designations ignoring case and separators only; leading zeros stay."""
    return re.sub(r"[\s\-_/.]", "", str(value or "")).casefold()


def _order_key(record: Mapping[str, Any]) -> str:
    revision = str(record.get("provider_revision") or "")
    number = re.fullmatch(r"version (\d+)", revision)
    if number:
        return f"v{int(number.group(1)):012d}"
    return f"t{revision}" if revision else ""


def _authorize(namespace: str, scopes: set[str] | frozenset[str], required: str, *, write: bool) -> None:
    namespace_scope = {f"namespace:{namespace}:write"} if write else {
        f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not (namespace_scope & set(scopes)):
        raise ProductError("unauthorized", f"{required} and namespace access are required")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", ".")
    match = re.match(r"^-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _resolution(value: Any) -> tuple[int, int] | None:
    match = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


class ProductStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        from src.kb.quantitative import WRITE_SCOPE as QUANT_WRITE
        from src.kb.quantitative import QuantitativeStore

        self.quantitative = QuantitativeStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)
            for symbol, (dimension, factor, aliases) in PRODUCT_UNITS.items():
                self.quantitative.register_unit(
                    DEFAULT_NAMESPACE, symbol, dimension, scopes={QUANT_WRITE},
                    principal_id="system:products", factor=factor, aliases=aliases,
                )

    # ------------------------------------------------------------- identities

    def _identity(self, namespace: str, level: str, key: Sequence[Any], row: Mapping[str, Any], run_id: str) -> str:
        identity_id = f"product-{level}:" + _digest([namespace, level, list(key)])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO product_identities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [identity_id, namespace, level, row.get("parent_id"), row.get("provider"), row.get("brand"),
             row.get("designation"), row.get("family"), row.get("provider_record_id"),
             _canonical(row.get("market") or {}), _canonical(row.get("identifiers") or {}),
             row.get("category_label"), run_id, self.now()],
        )
        return identity_id

    def _identities_for(self, namespace: str, record: Mapping[str, Any], run_id: str) -> str:
        provider = record["provider"]
        brand = record.get("brand")
        family_id = None
        if record.get("family") and brand:
            family_id = self._identity(namespace, "family", [provider, _brand_key(brand), record["family"]],
                                       {"provider": provider, "brand": brand, "family": record["family"]}, run_id)
        designation = record.get("designation")
        model_key = ([provider, _brand_key(brand), designation] if designation
                     else [provider, "record", record["provider_record_id"]])
        model_id = self._identity(namespace, "model", model_key, {
            "parent_id": family_id, "provider": provider, "brand": brand, "designation": designation,
            "family": record.get("family"), "category_label": dict(record.get("category") or {}).get("label"),
            "identifiers": {"mpn": dict(record.get("identifiers") or {}).get("mpn")},
        }, run_id)
        return self._identity(namespace, "variant", [provider, record["provider_record_id"]], {
            "parent_id": model_id, "provider": provider, "brand": brand, "designation": designation,
            "family": record.get("family"), "provider_record_id": record["provider_record_id"],
            "market": record.get("market") or {}, "identifiers": record.get("identifiers") or {},
            "category_label": dict(record.get("category") or {}).get("label"),
        }, run_id)

    # ---------------------------------------------------------- normalization

    def _normalize(self, namespace: str, attribute: Mapping[str, Any]) -> dict[str, Any]:
        from src.kb.quantitative import CALCULATE_SCOPE, QuantitativeError

        name = attribute["attribute"]
        raw = attribute.get("native_value")
        if name == "resolution":
            pair = _resolution(raw)
            if pair is None:
                return {"state": "unparseable", "value": None, "unit": None, "calculation_id": None}
            return {"state": "normalized", "value": f"{pair[0]}x{pair[1]}", "unit": "px", "calculation_id": None}
        if name == "energy_class":
            text = str(raw or "").strip().upper()
            valid = re.fullmatch(r"A\+{0,3}|[A-G]", text)
            return {"state": "normalized" if valid else "unparseable", "value": text if valid else None,
                    "unit": None, "calculation_id": None}
        target = NORMALIZED_UNITS.get(name)
        if target is None:
            return {"state": "not_normalized", "value": None, "unit": None, "calculation_id": None}
        number = _decimal(raw)
        if number is None:
            return {"state": "unparseable", "value": None, "unit": None, "calculation_id": None}
        unit = attribute.get("native_unit")
        if not unit:
            return {"state": "unit_unknown", "value": None, "unit": None, "calculation_id": None}
        try:
            receipt = self.quantitative.convert(
                DEFAULT_NAMESPACE, str(number), unit, target, scopes={CALCULATE_SCOPE},
                principal_id="system:products", precision=NORMALIZED_PRECISION[name],
            )
        except QuantitativeError as exc:
            return {"state": exc.code, "value": None, "unit": None, "calculation_id": None}
        return {"state": "normalized", "value": receipt["result"]["value"], "unit": target,
                "calculation_id": receipt["calculation_id"]}

    def _attributes(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        attributes = [dict(item) for item in record.get("attributes") or []]
        # EPREL publishes resolution as two fields; derive one comparable
        # assertion that cites both locations.
        horizontal = next((a for a in attributes if a["attribute"] == "resolution_horizontal"), None)
        vertical = next((a for a in attributes if a["attribute"] == "resolution_vertical"), None)
        if horizontal and vertical:
            attributes.append({
                "attribute": "resolution", "mode": None,
                "native_name": f"{horizontal['native_name']}+{vertical['native_name']}",
                "native_value": f"{horizontal['native_value']} x {vertical['native_value']}",
                "native_unit": "px", "label_scheme": horizontal.get("label_scheme"),
                "locator": {"json_pointers": [horizontal["locator"]["json_pointer"],
                                              vertical["locator"]["json_pointer"]]},
            })
        return attributes

    # ------------------------------------------------------------- projection

    def observe_page(
        self,
        run_id: str,
        source: Mapping[str, Any],
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        documents: Mapping[str, str],
        page_receipt: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, int]:
        """Project one runtime page idempotently (replays add nothing)."""

        del principal_id
        now = self.now()
        counts = {"revisions": 0, "assertions": 0, "identities": 0, "withdrawn": 0}
        prepared = []
        for item in records:
            record = dict(item.get("product_record") or {})
            if record.get("contract") != RECORD_CONTRACT:
                raise ProductError("invalid_record", "page record lacks a product record contract")
            attributes = [
                {**attribute, "normalized": self._normalize(namespace, attribute)}
                for attribute in self._attributes(record)
            ]
            prepared.append((item, record, attributes))
        before = self.conn.execute(
            "SELECT count(*) FROM product_identities WHERE namespace=?", [namespace]).fetchone()[0]
        self.conn.execute("BEGIN")
        try:
            for item, record, attributes in prepared:
                variant_id = self._identities_for(namespace, record, run_id)
                revision_id = "product-revision:" + _digest([namespace, record["provider"],
                                                             record["provider_record_id"], record["raw_sha256"]])[:24]
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO product_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING revision_id",
                    [revision_id, namespace, variant_id, record["provider"], record["provider_record_id"],
                     record.get("provider_revision"), _order_key(record), record["raw_sha256"], _canonical(record),
                     record.get("record_status") or "published", documents.get(str(item.get("id"))),
                     source["source_id"], run_id, now],
                ).fetchall()
                self.conn.execute("INSERT OR IGNORE INTO product_revision_observations VALUES (?,?,?)",
                                  [revision_id, run_id, now])
                if inserted:
                    counts["revisions"] += 1
                    for attribute in attributes:
                        normalized = attribute["normalized"]
                        conditions = {
                            "mode": attribute.get("mode"),
                            "label_scheme": attribute.get("label_scheme") or record.get("label_scheme"),
                            "category": dict(record.get("category") or {}),
                            "observed_at_ms": now,
                            "provider_revision": record.get("provider_revision"),
                            "effective": dict(record.get("lifecycle_claims") or {}),
                            "normalization_version": NORMALIZATION_VERSION,
                        }
                        assertion_id = "product-assertion:" + _digest(
                            [revision_id, attribute["attribute"], attribute.get("mode"), attribute["native_name"]])[:24]
                        self.conn.execute(
                            "INSERT OR IGNORE INTO product_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [assertion_id, namespace, variant_id, revision_id, record["provider"],
                             attribute["attribute"], attribute.get("mode"), attribute["native_name"],
                             None if attribute.get("native_value") is None else str(attribute["native_value"]),
                             attribute.get("native_unit"), normalized["value"], normalized["unit"],
                             normalized["state"], normalized["calculation_id"], _canonical(conditions),
                             record.get("assertion_kind") or "provider-assertion",
                             _canonical(attribute.get("locator") or {})],
                        )
                        counts["assertions"] += 1
                    for document in record.get("documents") or []:
                        link_id = "product-document:" + _digest(
                            [namespace, variant_id, document.get("kind"), document["url"], document.get("language")])[:24]
                        self.conn.execute(
                            "INSERT OR IGNORE INTO product_document_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [link_id, namespace, variant_id, revision_id, document.get("kind") or "document",
                             document["url"], document.get("language"), document.get("media_type"),
                             _canonical({k: document.get(k) for k in ("declared_size", "declared_updated", "title")}),
                             _canonical(document.get("locator") or {})],
                        )
                    for gtin in dict(record.get("identifiers") or {}).get("gtin") or []:
                        if gtin.get("state") == "valid":
                            self.conn.execute(
                                "INSERT OR IGNORE INTO product_identity_conflicts VALUES (?,?,?,?,?)",
                                [namespace, "gtin", gtin["value"], variant_id, revision_id])
                self._advance_current(namespace, variant_id, revision_id, record, run_id,
                                      str(page_receipt.get("scope_hash") or ""), now, counts)
            if page_receipt.get("selection_index") is not None and page_receipt.get("scope_hash"):
                variant = None
                if records:
                    record = dict(records[0].get("product_record") or {})
                    variant = "product-variant:" + _digest(
                        [namespace, "variant", [record.get("provider"), record.get("provider_record_id")]])[:24]
                self.conn.execute(
                    "INSERT OR IGNORE INTO product_coverage VALUES (nextval('product_coverage_seq'),?,?,?,?,?,?,?,?,?,?)",
                    [namespace, run_id, source["source_id"], int(page_receipt["selection_index"]),
                     _canonical(page_receipt.get("selector") or {}), page_receipt["scope_hash"],
                     str(page_receipt.get("model_outcome") or "returned"), page_receipt.get("response_sha256"),
                     variant, now],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        after = self.conn.execute(
            "SELECT count(*) FROM product_identities WHERE namespace=?", [namespace]).fetchone()[0]
        counts["identities"] = int(after) - int(before)
        return counts

    def _advance_current(self, namespace, variant_id, revision_id, record, run_id, scope_hash, now, counts):
        current = self.conn.execute(
            "SELECT c.revision_id, r.order_key, c.record_state FROM product_current c "
            "JOIN product_revisions r ON r.revision_id=c.revision_id WHERE c.variant_id=?", [variant_id]).fetchone()
        order = _order_key(record)
        withdrawn = record.get("record_status") == "withdrawn"
        if current is None:
            self.conn.execute("INSERT INTO product_current VALUES (?,?,?,?,?,?,?,?)", [
                variant_id, namespace, revision_id, "withdrawn" if withdrawn else "published",
                revision_id if withdrawn else None, run_id, scope_hash, now])
            counts["withdrawn"] += int(withdrawn)
            return
        # Out-of-order delivery: an older declared revision never replaces a newer one.
        newer = current[0] != revision_id and (not order or not current[1] or order >= current[1])
        if newer:
            self.conn.execute(
                "UPDATE product_current SET revision_id=?, record_state=?, state_evidence_revision=?, "
                "last_seen_run_id=?, last_scope_hash=?, updated_at_ms=? WHERE variant_id=?",
                [revision_id, "withdrawn" if withdrawn else "published",
                 revision_id if withdrawn else None, run_id, scope_hash, now, variant_id])
            counts["withdrawn"] += int(withdrawn and current[2] != "withdrawn")
        else:
            self.conn.execute(
                "UPDATE product_current SET last_seen_run_id=?, last_scope_hash=?, updated_at_ms=? WHERE variant_id=?",
                [run_id, scope_hash, now, variant_id])

    def finish_source(self, run_id: str, source_id: str, namespace: str, status: str,
                      selection_size: int) -> dict[str, Any]:
        """Advance the completed-refresh marker only for a fully projected, complete run."""

        # A checkpointed run resumes mid-selection, so a refresh cycle is every
        # model projected for this selection since the last completed refresh.
        last = self.conn.execute(
            "SELECT coverage_watermark FROM product_refresh WHERE namespace=? AND source_id=?",
            [namespace, source_id]).fetchone()
        scopes = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT scope_hash FROM product_coverage WHERE run_id=? AND source_id=?",
            [run_id, source_id]).fetchall()}
        rows = self.conn.execute(
            "SELECT DISTINCT selection_index FROM product_coverage WHERE namespace=? AND source_id=? "
            "AND scope_hash=? AND seq>?",
            [namespace, source_id, next(iter(scopes)) if len(scopes) == 1 else "", last[0] if last else -1],
        ).fetchall()
        projected = len(rows)
        reason = None
        if status != "complete":
            reason = "run_not_complete"
        elif len(scopes) != 1:
            reason = "no_projected_selection" if not scopes else "mixed_selection_scopes"
        elif projected < selection_size:
            reason = "incomplete_projection"
        advanced = reason is None
        now = self.now()
        scope_hash = next(iter(scopes)) if len(scopes) == 1 else None
        self.conn.execute(
            "INSERT OR REPLACE INTO product_refresh_history VALUES (?,?,?,?,?,?,?,?,?,?)",
            [namespace, source_id, run_id, status, scope_hash, projected, selection_size, advanced, reason, now])
        if advanced:
            watermark = self.conn.execute(
                "SELECT max(seq) FROM product_coverage WHERE namespace=? AND source_id=?",
                [namespace, source_id]).fetchone()[0]
            self.conn.execute("INSERT OR REPLACE INTO product_refresh VALUES (?,?,?,?,?,?,?)",
                              [namespace, source_id, scope_hash, selection_size, run_id, now, watermark])
        return {"refresh_advanced": advanced, "reason": reason, "projected_models": projected,
                "selection_size": selection_size}

    # ----------------------------------------------------------------- reads

    def _variant_rows(self, namespace: str, where: str = "", params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT i.identity_id, i.parent_id, i.provider, i.brand, i.designation, i.family, i.provider_record_id, "
            "i.market_json, i.identifiers_json, i.category_label, c.revision_id, c.record_state, "
            "c.last_seen_run_id, c.last_scope_hash, r.provider_revision, r.observed_at_ms "
            "FROM product_identities i JOIN product_current c ON c.variant_id=i.identity_id "
            "JOIN product_revisions r ON r.revision_id=c.revision_id "
            f"WHERE i.namespace=? AND i.level='variant' {where} ORDER BY i.provider, i.brand, i.designation, i.identity_id",
            [namespace, *params]).fetchall()
        return [{
            "variant_id": row[0], "model_id": row[1], "provider": row[2], "brand": row[3],
            "designation": row[4], "family": row[5], "provider_record_id": row[6],
            "market": _load(row[7], {}), "identifiers": _load(row[8], {}), "category": row[9],
            "current_revision_id": row[10], "record_state": row[11], "last_seen_run_id": row[12],
            "selection_scope_hash": row[13], "provider_revision": row[14], "observed_at_ms": row[15],
        } for row in rows]

    def _conflicts(self, namespace: str, variant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT a.identifier_kind, a.identifier_value, list(DISTINCT b.variant_id) FROM product_identity_conflicts a "
            "JOIN product_identity_conflicts b ON b.namespace=a.namespace AND b.identifier_kind=a.identifier_kind "
            "AND b.identifier_value=a.identifier_value AND b.variant_id<>a.variant_id "
            "JOIN product_identities ia ON ia.identity_id=a.variant_id JOIN product_identities ib ON ib.identity_id=b.variant_id "
            "WHERE a.namespace=? AND a.variant_id=? AND ia.parent_id<>ib.parent_id GROUP BY 1,2",
            [namespace, variant_id]).fetchall()
        return [{"identifier_kind": row[0], "value": row[1], "other_variants": sorted(row[2])} for row in rows]

    def lookup(self, namespace: str, *, scopes, query: str | None = None, brand: str | None = None,
               gtin: str | None = None, designation: str | None = None, provider: str | None = None,
               limit: int = 25) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        limit = max(1, min(int(limit), 100))
        clauses, params = [], []
        if brand:
            clauses.append("AND lower(i.brand)=?")
            params.append(_brand_key(brand))
        if provider:
            clauses.append("AND i.provider=?")
            params.append(provider)
        variants = self._variant_rows(namespace, " ".join(clauses), params)
        if designation:
            key = designation_key(designation)
            variants = [v for v in variants if designation_key(v["designation"]) == key]
        if gtin:
            variants = [v for v in variants if any(g.get("value") == str(gtin) for g in v["identifiers"].get("gtin") or [])]
        if query:
            needle = query.casefold()
            variants = [v for v in variants if needle in " ".join(
                str(v.get(k) or "") for k in ("brand", "designation", "family", "provider_record_id")).casefold()]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace, "count": len(variants[:limit]),
                "truncated": len(variants) > limit,
                "variants": [{**v, "identifier_conflicts": self._conflicts(namespace, v["variant_id"]),
                              "matches": self._matches_for(namespace, v["model_id"])} for v in variants[:limit]]}

    def assertions(self, namespace: str, variant_id: str, *, include_superseded: bool = False) -> list[dict[str, Any]]:
        where = "" if include_superseded else "AND a.revision_id=(SELECT revision_id FROM product_current WHERE variant_id=a.variant_id)"
        rows = self.conn.execute(
            "SELECT a.assertion_id, a.revision_id, a.provider, a.attribute, a.mode, a.native_name, a.native_value, "
            "a.native_unit, a.normalized_value, a.normalized_unit, a.normalization_state, a.calculation_id, "
            "a.conditions_json, a.assertion_kind, a.locator_json, r.provider_revision, r.observed_at_ms, r.document_id "
            "FROM product_assertions a JOIN product_revisions r ON r.revision_id=a.revision_id "
            f"WHERE a.namespace=? AND a.variant_id=? {where} ORDER BY r.observed_at_ms, a.attribute, a.mode",
            [namespace, variant_id]).fetchall()
        return [{
            "assertion_id": row[0], "revision_id": row[1], "provider": row[2], "attribute": row[3], "mode": row[4],
            "native_name": row[5], "native_value": row[6], "native_unit": row[7], "normalized_value": row[8],
            "normalized_unit": row[9], "normalization_state": row[10], "calculation_id": row[11],
            "conditions": _load(row[12], {}), "assertion_kind": row[13], "locator": _load(row[14], {}),
            "provider_revision": row[15], "observed_at_ms": row[16], "document_id": row[17],
        } for row in rows]

    def inspect(self, namespace: str, identity_id: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        row = self.conn.execute(
            "SELECT level, parent_id FROM product_identities WHERE namespace=? AND identity_id=?",
            [namespace, identity_id]).fetchone()
        if row is None:
            raise ProductError("not_found", "product identity is not visible in this namespace")
        variants = (self._variant_rows(namespace, "AND i.identity_id=?", [identity_id]) if row[0] == "variant"
                    else self._variant_rows(namespace, "AND i.parent_id=?", [identity_id]))
        result = []
        for variant in variants:
            revisions = self.conn.execute(
                "SELECT revision_id, provider_revision, record_status, raw_sha256, first_run_id, observed_at_ms, document_id "
                "FROM product_revisions WHERE variant_id=? ORDER BY observed_at_ms, revision_id",
                [variant["variant_id"]]).fetchall()
            result.append({
                **variant,
                "revisions": [dict(zip(("revision_id", "provider_revision", "record_status", "raw_sha256",
                                        "first_run_id", "observed_at_ms", "document_id"), item)) for item in revisions],
                "assertions": self.assertions(namespace, variant["variant_id"]),
                "superseded_assertions": [a for a in self.assertions(namespace, variant["variant_id"], include_superseded=True)
                                          if a["revision_id"] != variant["current_revision_id"]],
                "documents": self.documents(namespace, variant["variant_id"]),
                "identifier_conflicts": self._conflicts(namespace, variant["variant_id"]),
                "coverage": self.coverage(namespace, variant["variant_id"]),
            })
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace, "identity_id": identity_id, "level": row[0],
                "parent_id": row[1], "variants": result,
                "matches": self._matches_for(namespace, identity_id if row[0] == "model" else row[1])}

    def coverage(self, namespace: str, variant_id: str) -> dict[str, Any]:
        current = self.conn.execute(
            "SELECT c.record_state, c.last_seen_run_id, c.last_scope_hash, r.source_id FROM product_current c "
            "JOIN product_revisions r ON r.revision_id=c.revision_id WHERE c.variant_id=?", [variant_id]).fetchone()
        if current is None:
            return {}
        refresh = self.conn.execute(
            "SELECT scope_hash, completed_run_id, completed_at_ms FROM product_refresh WHERE namespace=? AND source_id=?",
            [namespace, current[3]]).fetchone()
        latest = self.conn.execute(
            "SELECT outcome, run_id FROM product_coverage WHERE namespace=? AND variant_id=? ORDER BY observed_at_ms DESC LIMIT 1",
            [namespace, variant_id]).fetchone()
        in_selection = refresh is not None and refresh[0] == current[2]
        return {
            "record_state": current[0],
            "last_seen_run_id": current[1],
            "last_completed_refresh_run_id": None if refresh is None else refresh[1],
            "in_current_selection": in_selection,
            # Presence in the last complete refresh is observable; absence
            # never implies discontinuation.
            "status_basis": "provider-declared" if current[0] == "withdrawn"
            else "observed" if in_selection and refresh[1] == current[1]
            else "not-observed-in-latest-complete-refresh",
            "latest_outcome": None if latest is None else {"outcome": latest[0], "run_id": latest[1]},
        }

    def selection_outcomes(self, namespace: str, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_id, selection_index, selector_json, outcome, variant_id FROM product_coverage "
            "WHERE namespace=? AND run_id=? ORDER BY source_id, selection_index", [namespace, run_id]).fetchall()
        return [{"source_id": r[0], "selection_index": r[1], "selector": _load(r[2], {}), "outcome": r[3],
                 "variant_id": r[4]} for r in rows]

    # -------------------------------------------------------------- documents

    def documents(self, namespace: str, variant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT l.link_id, l.kind, l.url, l.language, l.media_type, l.first_revision_id, l.declared_json, l.locator_json "
            "FROM product_document_links l WHERE l.namespace=? AND l.variant_id=? ORDER BY l.kind, l.url",
            [namespace, variant_id]).fetchall()
        result = []
        for row in rows:
            fetches = self.conn.execute(
                "SELECT f.state, f.reason, f.http_status, f.final_url, f.content_sha256, f.media_type, f.bytes, "
                "f.fetched_at_ms, a.extraction_state FROM product_document_fetches f "
                "LEFT JOIN product_document_assets a ON a.content_sha256=f.content_sha256 "
                "WHERE f.link_id=? ORDER BY f.fetched_at_ms, f.fetch_id", [row[0]]).fetchall()
            versions = [dict(zip(("state", "reason", "http_status", "final_url", "content_sha256", "media_type",
                                  "bytes", "fetched_at_ms", "extraction_state"), item)) for item in fetches]
            retained = [v for v in versions if v["state"] == "retained"]
            result.append({
                "contract": DOCUMENT_CONTRACT, "link_id": row[0], "kind": row[1], "url": row[2], "language": row[3],
                "media_type": row[4], "first_revision_id": row[5], "declared": _load(row[6], {}),
                "locator": _load(row[7], {}),
                "availability": versions[-1]["state"] if versions else "linked-not-fetched",
                "versions": versions,
                "distinct_contents": len({v["content_sha256"] for v in retained}),
            })
        return result

    def record_document_fetch(self, namespace: str, link_id: str, outcome: Mapping[str, Any], *,
                              principal_id: str, extraction: Mapping[str, Any] | None = None) -> dict[str, Any]:
        now = self.now()
        sha = outcome.get("content_sha256")
        fetch_id = "product-document-fetch:" + _digest([link_id, outcome.get("state"), sha, outcome.get("reason"), now])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO product_document_fetches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [fetch_id, link_id, namespace, outcome["state"], outcome.get("reason"), outcome.get("http_status"),
             outcome.get("final_url"), sha, outcome.get("media_type"), outcome.get("bytes"), principal_id, now])
        deduplicated = False
        if sha:
            deduplicated = self.conn.execute(
                "SELECT 1 FROM product_document_assets WHERE content_sha256=?", [sha]).fetchone() is not None
            self.conn.execute(
                "INSERT OR IGNORE INTO product_document_assets VALUES (?,?,?,?,?,?)",
                [sha, outcome.get("media_type"), int(outcome.get("bytes") or 0),
                 str(dict(extraction or {}).get("state") or "not_attempted"),
                 dict(extraction or {}).get("chars"), now])
        return {"fetch_id": fetch_id, "deduplicated_asset": deduplicated, **dict(outcome)}

    # --------------------------------------------------------------- matching

    def _model_summary(self, namespace: str, model_id: str) -> dict[str, Any] | None:
        variants = self._variant_rows(namespace, "AND i.parent_id=?", [model_id])
        if not variants:
            return None
        attributes: dict[tuple[str, str | None], set[str]] = {}
        for variant in variants:
            for item in self.assertions(namespace, variant["variant_id"]):
                if item["normalized_value"] is not None:
                    attributes.setdefault((item["attribute"], item["mode"]), set()).add(item["normalized_value"])
        first = variants[0]
        return {"model_id": model_id, "provider": first["provider"], "brand": first["brand"],
                "designation": first["designation"], "category": first["category"],
                "markets": sorted({str(v["market"].get("region")) for v in variants if v["market"].get("region")}),
                "gtins": sorted({g["value"] for v in variants for g in v["identifiers"].get("gtin") or []
                                 if g.get("state") == "valid"}),
                "attributes": attributes}

    def propose_matches(self, namespace: str, *, scopes, principal_id: str) -> dict[str, Any]:
        """Deterministic candidates between Icecat and EPREL models; never auto-accepted."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        del principal_id
        models = [row[0] for row in self.conn.execute(
            "SELECT identity_id FROM product_identities WHERE namespace=? AND level='model' ORDER BY identity_id",
            [namespace]).fetchall()]
        summaries = [s for s in (self._model_summary(namespace, m) for m in models) if s]
        left = [s for s in summaries if s["provider"] == "icecat"]
        right = [s for s in summaries if s["provider"] == "eprel"]
        now = self.now()
        candidates = []
        for a in left:
            related = []
            for b in right:
                if _brand_key(a["brand"]) != _brand_key(b["brand"]) or not a["designation"] or not b["designation"]:
                    continue
                ka, kb = designation_key(a["designation"]), designation_key(b["designation"])
                prefix = len(_common_prefix(ka, kb))
                if ka == kb:
                    related.append((b, "designation_exact"))
                elif prefix >= 6 and prefix >= max(len(ka), len(kb)) - 4:
                    related.append((b, "designation_variant_suffix"))
            exact = [b for b, kind in related if kind == "designation_exact"]
            for b, kind in related:
                evidence, reasons, contradictions = [], [], []
                if kind == "designation_exact":
                    evidence.append({"kind": "designation", "left": a["designation"], "right": b["designation"]})
                else:
                    reasons.append("designations differ only in a suffix (possible regional or bundle variant)")
                diag_a = a["attributes"].get(("diagonal", None), set())
                diag_b = b["attributes"].get(("diagonal", None), set())
                if diag_a and diag_b:
                    gap = min(abs(Decimal(x) - Decimal(y)) for x in diag_a for y in diag_b)
                    if gap <= DIAGONAL_TOLERANCE_CM:
                        evidence.append({"kind": "diagonal_cm", "left": sorted(diag_a), "right": sorted(diag_b)})
                    else:
                        contradictions.append(f"diagonal differs by {gap} cm")
                else:
                    reasons.append("diagonal missing on at least one side")
                res_a = a["attributes"].get(("resolution", None), set())
                res_b = b["attributes"].get(("resolution", None), set())
                if res_a and res_b:
                    if res_a & res_b:
                        evidence.append({"kind": "resolution", "left": sorted(res_a), "right": sorted(res_b)})
                    else:
                        contradictions.append("resolution differs")
                if a["gtins"] and b["gtins"] and not set(a["gtins"]) & set(b["gtins"]):
                    contradictions.append("GTINs are disjoint")
                if len(exact) > 1 and kind == "designation_exact":
                    reasons.append("several EPREL models share this designation")
                if contradictions:
                    state, confidence = "contradicted", "none"
                elif kind != "designation_exact" or reasons:
                    state, confidence = "ambiguous", "low"
                else:
                    state, confidence = "proposed", "high" if len(evidence) >= 3 else "medium"
                match_id = "product-match:" + _digest([namespace, a["model_id"], b["model_id"]])[:24]
                payload = {"evidence": evidence, "reasons": reasons + contradictions}
                existing = self.conn.execute("SELECT evidence_json, reasons_json FROM product_matches WHERE match_id=?",
                                             [match_id]).fetchone()
                if existing is None:
                    self.conn.execute("INSERT INTO product_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                        match_id, namespace, a["model_id"], b["model_id"], MATCH_METHOD, state, confidence,
                        _canonical(evidence), _canonical(payload["reasons"]), now, now])
                elif (_load(existing[0], []), _load(existing[1], [])) != (evidence, payload["reasons"]):
                    self.conn.execute(
                        "UPDATE product_matches SET candidate_state=?, confidence=?, evidence_json=?, reasons_json=?, "
                        "updated_at_ms=? WHERE match_id=?",
                        [state, confidence, _canonical(evidence), _canonical(payload["reasons"]), now, match_id])
                candidates.append(self.match(namespace, match_id))
        return {"contract": MATCH_CONTRACT, "namespace": namespace, "method": MATCH_METHOD, "candidates": candidates}

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT left_id, right_id, method, candidate_state, confidence, evidence_json, reasons_json "
            "FROM product_matches WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise ProductError("not_found", "match candidate is not visible in this namespace")
        reviews = self.conn.execute(
            "SELECT sequence, decision, reason, principal_id, reviewed_at_ms FROM product_match_reviews "
            "WHERE match_id=? ORDER BY sequence", [match_id]).fetchall()
        history = [dict(zip(("sequence", "decision", "reason", "principal_id", "reviewed_at_ms"), r)) for r in reviews]
        return {"contract": MATCH_CONTRACT, "match_id": match_id, "left_model_id": row[0], "right_model_id": row[1],
                "method": row[2], "candidate_state": row[3], "confidence": row[4], "evidence": _load(row[5], []),
                "reasons": _load(row[6], []), "review_state": history[-1]["decision"] if history else "unreviewed",
                "review_history": history}

    def review_match(self, namespace: str, match_id: str, decision: str, reason: str, *, scopes,
                     principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in MATCH_DECISIONS:
            raise ProductError("invalid_decision", "decision must be accepted, rejected or deferred")
        if not str(reason or "").strip():
            raise ProductError("invalid_decision", "a review reason is required")
        current = self.match(namespace, match_id)
        if decision == "accepted" and current["candidate_state"] == "contradicted":
            raise ProductError("contradicted_match", "resolve contradicting evidence before accepting this match",
                               reasons=current["reasons"])
        sequence = len(current["review_history"]) + 1
        self.conn.execute("INSERT INTO product_match_reviews VALUES (?,?,?,?,?,?,?,?)", [
            f"product-match-review:{_digest([match_id, sequence])[:24]}", match_id, namespace, sequence,
            decision, reason.strip(), principal_id, self.now()])
        return self.match(namespace, match_id)

    def _matches_for(self, namespace: str, model_id: str | None) -> list[dict[str, Any]]:
        if not model_id:
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM product_matches WHERE namespace=? AND (left_id=? OR right_id=?) ORDER BY match_id",
            [namespace, model_id, model_id]).fetchall()
        return [self.match(namespace, r[0]) for r in rows]

    def accepted_equivalents(self, namespace: str, model_id: str) -> set[str]:
        return {m["right_model_id"] if m["left_model_id"] == model_id else m["left_model_id"]
                for m in self._matches_for(namespace, model_id) if m["review_state"] == "accepted"}

    # ------------------------------------------------------------- comparison

    def compare(self, namespace: str, model_ids: Sequence[str], *, scopes,
                attributes: Sequence[str] | None = None) -> dict[str, Any]:
        """Evidence-linked comparison of explicitly resolved models.

        Each column is one requested model plus its *accepted* cross-provider
        equivalents. Cells keep every provider assertion side by side; numeric
        comparison across columns happens only on a shared attribute, mode and
        normalized unit (and label scheme for energy classes).
        """

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        requested = list(dict.fromkeys(model_ids))
        if not 2 <= len(requested) <= 6:
            raise ProductError("invalid_comparison", "compare 2-6 resolved models")
        columns = []
        for model_id in requested:
            level = self.conn.execute(
                "SELECT level FROM product_identities WHERE namespace=? AND identity_id=?", [namespace, model_id]).fetchone()
            if level is None:
                raise ProductError("not_found", f"model {model_id} is not visible in this namespace")
            if level[0] != "model":
                raise ProductError("unresolved_identity", "comparison requires resolved model identities, not variants or families")
            members = [model_id, *sorted(self.accepted_equivalents(namespace, model_id))]
            pending = [m for m in self._matches_for(namespace, model_id)
                       if m["review_state"] in {"unreviewed", "deferred"}]
            rejected = [m for m in self._matches_for(namespace, model_id) if m["review_state"] == "rejected"]
            variants = [v for member in members for v in self._variant_rows(namespace, "AND i.parent_id=?", [member])]
            columns.append({"model_id": model_id, "members": members, "variants": variants,
                            "pending_matches": [m["match_id"] for m in pending],
                            "rejected_matches": [m["match_id"] for m in rejected]})
        wanted = list(attributes or ["diagonal", "resolution", "width", "height", "depth", "on_mode_power",
                                     "energy_consumption_1000h", "energy_class"])
        rows = []
        for attribute in wanted:
            modes = sorted({a["mode"] for column in columns for variant in column["variants"]
                            for a in self.assertions(namespace, variant["variant_id"]) if a["attribute"] == attribute},
                           key=lambda value: "" if value is None else value)
            for mode in modes or [None]:
                cells = []
                for column in columns:
                    values = []
                    for variant in column["variants"]:
                        for a in self.assertions(namespace, variant["variant_id"]):
                            if a["attribute"] != attribute or a["mode"] != mode:
                                continue
                            values.append({
                                "provider": a["provider"], "assertion_kind": a["assertion_kind"],
                                "native_value": a["native_value"], "native_unit": a["native_unit"],
                                "normalized_value": a["normalized_value"], "normalized_unit": a["normalized_unit"],
                                "normalization_state": a["normalization_state"], "calculation_id": a["calculation_id"],
                                "label_scheme": a["conditions"].get("label_scheme"),
                                "evidence": {"variant_id": variant["variant_id"], "revision_id": a["revision_id"],
                                             "provider_revision": a["provider_revision"], "locator": a["locator"],
                                             "document_id": a["document_id"], "observed_at_ms": a["observed_at_ms"]},
                                "record_state": variant["record_state"],
                            })
                    distinct = {v["normalized_value"] for v in values if v["normalized_value"] is not None}
                    cells.append({"model_id": column["model_id"], "values": values,
                                  "state": "missing" if not values else "conflict" if len(distinct) > 1
                                  else "unnormalized" if not distinct else "value",
                                  "value": next(iter(distinct)) if len(distinct) == 1 else None})
                units = {v["normalized_unit"] for c in cells for v in c["values"] if v["normalized_value"] is not None}
                schemes = {v["label_scheme"] for c in cells for v in c["values"]} if attribute == "energy_class" else set()
                if any(c["state"] == "missing" for c in cells):
                    comparable, why = False, "missing on at least one model"
                elif any(c["state"] in {"conflict", "unnormalized"} for c in cells):
                    comparable, why = False, "provider disagreement or unnormalized value"
                elif len(units) > 1:
                    comparable, why = False, "incompatible units"
                elif attribute == "energy_class" and (None in schemes or len(schemes) > 1):
                    comparable, why = False, "label scheme unknown or different"
                else:
                    comparable, why = True, None
                rows.append({"attribute": attribute, "mode": mode, "comparable": comparable,
                             "not_comparable_reason": why, "cells": cells})
        return {
            "contract": COMPARISON_CONTRACT, "namespace": namespace,
            "columns": [{**{k: c[k] for k in ("model_id", "members", "pending_matches", "rejected_matches")},
                         "variants": [{k: v[k] for k in ("variant_id", "provider", "brand", "designation", "market",
                                                         "record_state", "current_revision_id", "provider_revision")}
                                      for v in c["variants"]],
                         "documents": [d for v in c["variants"] for d in self.documents(namespace, v["variant_id"])]}
                        for c in columns],
            "rows": rows,
            "notice": ("Values are brand-authorised content (Icecat) and supplier registrations (EPREL), "
                       "not independent measurements, and this comparison is not a buying recommendation."),
        }


def _common_prefix(left: str, right: str) -> str:
    size = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        size += 1
    return left[:size]


class ProductProjector:
    """Source-pack runtime projector for ``noesis-product-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = ProductStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("product") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest
        document_ids = {
            str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(item["document_id"])
            for item in documents
        }
        return self.store.observe_page(run_id, source, self._namespace(source), records, documents=document_ids,
                                       page_receipt=page_receipt, principal_id=principal_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        size = len(dict(source.get("product") or {}).get("selection") or [])
        return self.store.finish_source(run_id, source["source_id"], self._namespace(source), status, size)


# ---------------------------------------------------------------- documents


def acquire_documents(store: ProductStore, namespace: str, variant_id: str, *, scopes, principal_id: str,
                      policy: Mapping[str, Any], transport: Callable[..., Mapping[str, Any]] | None = None,
                      extractor: Callable[[bytes, str | None], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Follow only provider-linked documents of one variant, within the pack's document policy.

    ``policy`` comes from the source declaration: ``retain`` (licence permits
    keeping bytes), ``allowed_hosts``, ``media_types`` and ``max_bytes``.
    A link that may not be retained is recorded as ``link_only`` without a
    request; failures never touch the structured product records.
    """

    _authorize(namespace, scopes, WRITE_SCOPE, write=True)
    from urllib.parse import urlparse

    from src.ingestion.source_pack_runtime import HTTPSPageAdapter
    from src.ingestion.source_packs import SourcePackError, _validate_endpoint

    max_bytes = int(policy.get("max_bytes") or 5_000_000)
    allowed_hosts = {str(h).casefold() for h in policy.get("allowed_hosts") or []}
    media_types = {str(m).casefold() for m in policy.get("media_types") or ["application/pdf"]}
    if transport is None:
        from functools import partial

        transport = partial(HTTPSPageAdapter._request, max_bytes=max_bytes)
    extractor = extractor or _extract_pdf_text
    outcomes = []
    for link in store.documents(namespace, variant_id):
        url = link["url"]
        host = (urlparse(url).hostname or "").casefold()
        if not policy.get("retain"):
            outcome = {"state": "link_only", "reason": "source terms do not permit retaining document bytes"}
        elif host not in allowed_hosts:
            outcome = {"state": "not_permitted", "reason": f"host {host or '?'} is not an allowed document host"}
        else:
            try:
                _validate_endpoint(url, "product-document")
                response = transport(url=url, params={}, headers={"Accept": ", ".join(sorted(media_types))},
                                     timeout=float(policy.get("timeout_s") or 30))
                outcome = _document_outcome(response, url, max_bytes, media_types, allowed_hosts)
            except SourcePackError as exc:
                state = "too_large" if exc.code == "response_too_large" else "inaccessible"
                outcome = {"state": state, "reason": exc.code}
        extraction = None
        if outcome.get("state") == "retained":
            try:
                extraction = dict(extractor(outcome.pop("_content"), outcome.get("media_type")))
            except Exception as exc:  # noqa: BLE001 - extraction failure keeps the asset and record
                extraction = {"state": "failed", "error": type(exc).__name__}
            outcome["extraction_state"] = extraction.get("state")
        outcome.pop("_content", None)
        outcomes.append({"link_id": link["link_id"], "kind": link["kind"], "url": url,
                         **store.record_document_fetch(namespace, link["link_id"], outcome, principal_id=principal_id,
                                                       extraction=extraction)})
    return {"contract": DOCUMENT_CONTRACT, "namespace": namespace, "variant_id": variant_id, "documents": outcomes}


def _document_outcome(response: Mapping[str, Any], url: str, max_bytes: int, media_types: set[str],
                      allowed_hosts: set[str]) -> dict[str, Any]:
    from urllib.parse import urlparse

    status = int(response.get("status", 200))
    headers = {str(k).casefold(): str(v) for k, v in dict(response.get("headers") or {}).items()}
    final_url = str(response.get("final_url") or url)
    if (urlparse(final_url).hostname or "").casefold() not in allowed_hosts:
        return {"state": "not_permitted", "reason": "redirected outside the allowed document hosts",
                "http_status": status, "final_url": final_url}
    if status in {401, 403}:
        return {"state": "inaccessible", "reason": "access_denied", "http_status": status, "final_url": final_url}
    if status == 404 or status == 410:
        return {"state": "inaccessible", "reason": "not_found", "http_status": status, "final_url": final_url}
    if status >= 300:
        return {"state": "inaccessible", "reason": f"http_{status}", "http_status": status, "final_url": final_url}
    content = response.get("content", b"")
    raw = content.encode() if isinstance(content, str) else bytes(content)
    if len(raw) > max_bytes:
        return {"state": "too_large", "reason": "response_too_large", "http_status": status, "final_url": final_url}
    media = headers.get("content-type", "").split(";")[0].strip().casefold() or None
    if media not in media_types:
        return {"state": "unsupported", "reason": f"media type {media or 'unknown'} is not accepted",
                "http_status": status, "final_url": final_url, "media_type": media}
    return {"state": "retained", "http_status": status, "final_url": final_url, "media_type": media,
            "bytes": len(raw), "content_sha256": hashlib.sha256(raw).hexdigest(), "_content": raw}


def _extract_pdf_text(content: bytes, media_type: str | None) -> dict[str, Any]:
    if media_type != "application/pdf":
        return {"state": "not_attempted"}
    try:
        import fitz  # PyMuPDF, already used by the document pipeline
    except ImportError:
        return {"state": "extractor_unavailable"}
    with fitz.open(stream=content, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    return {"state": "extracted", "chars": len(text)}


# ---------------------------------------------------------------- readiness


def readiness(conn: Any, *, pack_id: str = "products-displays",
              secrets: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Per-provider readiness: one working provider never implies cross-source validation."""

    import os

    from src.ingestion.product_sources import PROVIDER_CONTRACTS
    from src.ingestion.source_packs import _digest as source_digest

    lookup = secrets or (lambda name: os.environ.get(name))
    try:
        row = conn.execute(
            "SELECT c.enabled,v.manifest_json FROM source_pack_current c JOIN source_pack_versions v "
            "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?", [pack_id]).fetchone()
    except Exception:  # noqa: BLE001 - runtime tables absent until first install
        row = None
    enabled = bool(row and row[0])
    manifest = _load(row[1], {}) if row else {}
    by_provider = {source["connector"]: source for source in manifest.get("sources") or []}
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        source = by_provider.get(provider)
        blockers: list[dict[str, Any]] = []
        if row is None or source is None:
            blockers.append({"code": "pack_not_installed", "severity": "blocking",
                             "action": "install config/source_packs/products.json"})
        elif not enabled:
            blockers.append({"code": "pack_disabled", "severity": "blocking"})
        if source is not None:
            auth = dict(source.get("auth") or {})
            if auth.get("kind") == "required-secret" and not lookup(str(auth.get("secret_ref"))):
                blockers.append({
                    "code": "credential_missing", "secret_ref": auth.get("secret_ref"), "severity": "blocking-live",
                    "action": "complete the provider's API access process; public browsing is not API readiness"})
            policy = source["license"]
            terms_hash = source_digest({"terms_url": policy["terms_url"], "redistribution": policy["redistribution"]})
            try:
                accepted = bool(conn.execute(
                    "SELECT 1 FROM source_pack_license_acceptance WHERE pack_id=? AND source_id=? AND license_id=? AND terms_hash=?",
                    [pack_id, source["source_id"], policy["id"], terms_hash]).fetchone())
            except Exception:  # noqa: BLE001 - runtime not yet initialized
                accepted = False
            if not accepted:
                blockers.append({"code": "license_not_accepted", "source_id": source["source_id"],
                                 "severity": "blocking-live"})
        fixture_ready = source is not None and enabled
        providers[provider] = {
            "source_id": None if source is None else source["source_id"],
            "fixture": "ready" if fixture_ready else "blocked",
            "live": "ready" if fixture_ready and not blockers else "blocked",
            "live_verification": contract["status"],
            "blockers": blockers,
        }
    return {
        "pack_id": pack_id,
        "installed": row is not None,
        "enabled": enabled,
        "providers": providers,
        "cross_source_validation": "outstanding",
        "notice": "Readiness is per provider; cross-provider overlap is validated only by a dated live run of both.",
    }


def document_policy(conn: Any, namespace: str, variant_id: str, *, pack_id: str = "products-displays") -> dict[str, Any]:
    """The installed source declaration's document policy for a variant's provider."""

    row = conn.execute(
        "SELECT r.source_id FROM product_current c JOIN product_revisions r ON r.revision_id=c.revision_id "
        "WHERE c.variant_id=? AND c.namespace=?", [variant_id, namespace]).fetchone()
    if row is None:
        raise ProductError("not_found", "product variant is not visible in this namespace")
    manifest = conn.execute(
        "SELECT v.manifest_json FROM source_pack_current c JOIN source_pack_versions v "
        "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?", [pack_id]).fetchone()
    source = next((s for s in _load(manifest[0], {}).get("sources") or [] if s["source_id"] == row[0]), None) \
        if manifest else None
    if source is None:
        raise ProductError("pack_not_installed", "the variant's source is not in the installed products pack")
    return dict(dict(source.get("product") or {}).get("documents") or {})

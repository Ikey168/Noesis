"""Food composition and labelling: the Products ``food`` feature's record owner (#2216, FC02 and FC08).

Companion of :mod:`src.kb.products` (Products identities stay there) that owns
``noesis-food-composition-record-v1``. One adapter *statement* is what one
provider published about one food at one revision:

* **food product** (GTIN-keyed, Open Food Facts and FoodData Central Branded)
  or **generic food** (FoodData Central Foundation, SR Legacy and Survey foods,
  composition-table foods keyed by table and native food code);
* **ingredient statement**, **allergen declaration** (``contains`` /
  ``may_contain`` as published), **nutrient value** (nutrient, amount, unit,
  basis, derivation and value flags as published) and **labelling claim**;
* **label revision** - the statement itself, immutable. Every distinct payload
  is a new revision; the current one is chosen by the provider's own order
  (Open Food Facts revision number, FoodData Central publication date, table
  edition date). A late older payload is history, a replay adds nothing.

Every record carries provider, provider key, revision, retrieval time, as-of
time and a mandatory provenance class: ``crowd-sourced`` (Open Food Facts) or
``reference`` (FoodData Central, composition tables). The two classes are never
merged into one value. Units are stored as published beside an explicit
normalized form; an unknown unit stays ``unknown``. Open Food Facts records
carry their ODbL attribution and share-alike obligation on every record and
export.

No record or answer carries a nutrition score, health rating, ranking or diet
advice; Open Food Facts' own computed scores are excluded at acquisition.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.kb.products import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE

CONTRACT = "noesis-food-composition-record-v1"
ANSWER_CONTRACT = "noesis-food-composition-answer-v1"
SOURCE_PACK = "products-displays"
DEFAULT_NAMESPACE = "global"
FEATURE = "food"
PROVIDERS = ("open-food-facts", "fooddata-central", "composition-table")
PROVENANCE = {
    "open-food-facts": "crowd-sourced",
    "fooddata-central": "reference",
    "composition-table": "reference",
}
PROVENANCE_CLASSES = ("crowd-sourced", "reference")
RECORD_TYPES = (
    "food-product",
    "generic-food",
    "ingredient-statement",
    "allergen-declaration",
    "nutrient-value",
    "labelling-claim",
    "label-revision",
)
REVISION_BASES = ("off-revision", "fdc-publication-date", "table-edition")
ALLERGEN_RELATIONS = ("contains", "may_contain")
UNIT_STATES = ("known", "unknown", "absent")
# Open Food Facts licensing (FC01): the database under ODbL 1.0, individual contents under DbCL 1.0, images
# under CC BY-SA (never mirrored). Carried on every OFF record, answer and export.
ODBL_ATTRIBUTION = {
    "licence": "ODbL-1.0",
    "contents_licence": "DbCL-1.0",
    "text": "Contains information from Open Food Facts (https://world.openfoodfacts.org), which is made "
    "available here under the Open Database License (ODbL) v1.0.",
    "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
    "share_alike": True,
    "obligations": [
        "attribute Open Food Facts on any public use of the data or a produced work",
        "a publicly used adapted database (Open Food Facts records, alone or combined) is offered under the ODbL",
        "keep Open Food Facts-derived records separable: they live in their own provider rows and are never "
        "merged with reference values",
    ],
    "images": "product images are CC BY-SA and are never mirrored or stored",
}
FDC_ATTRIBUTION = {
    "licence": "public-domain (CC0 1.0)",
    "text": "U.S. Department of Agriculture, Agricultural Research Service. FoodData Central. fdc.nal.usda.gov.",
    "share_alike": False,
}
TABLE_ATTRIBUTION = {
    "ciqual": {
        "licence": "Licence Ouverte / Etalab 2.0",
        "text": "Anses. Ciqual French food composition table (edition as cited). https://ciqual.anses.fr/",
        "share_alike": False,
    },
}
# Units as published -> normalized symbol; anything else stays unknown (never guessed).
UNIT_NORMALIZATION = {
    "g": "g",
    "mg": "mg",
    "µg": "ug",
    "μg": "ug",
    "ug": "ug",
    "mcg": "ug",
    "kcal": "kcal",
    "kj": "kJ",
    "iu": "IU",
    "ml": "mL",
    "% vol": "% vol",
}
# Keys an answer or record may never carry (FC08): no Noesis nutrition score, ranking or advice.
FORBIDDEN_KEYS = frozenset(
    {
        "score",
        "nutrition_score",
        "nutriscore",
        "nutri_score",
        "health_score",
        "health_rating",
        "rating",
        "rank",
        "ranking",
        "recommendation",
        "advice",
        "diet_advice",
        "verdict",
        "safe",
        "healthy",
    }
)
BOUNDARY = (
    "Composition, ingredients, allergens and claims are quoted as each provider published them, per provider "
    "and revision. Crowd-sourced (Open Food Facts) and reference (FoodData Central, composition tables) values "
    "are shown side by side and never reconciled. Noesis computes no nutrition score, ranking or diet advice."
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS food_composition_seq;
CREATE TABLE IF NOT EXISTS food_items (
  namespace TEXT NOT NULL, food_id TEXT NOT NULL, provider TEXT NOT NULL, provider_key TEXT NOT NULL,
  food_kind TEXT NOT NULL, provenance_class TEXT NOT NULL, gtin_key TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, food_id)
);
CREATE TABLE IF NOT EXISTS food_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, food_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  seq BIGINT NOT NULL, content_digest TEXT NOT NULL, revision_value TEXT NOT NULL, revision_basis TEXT NOT NULL,
  order_key TEXT NOT NULL, revision_date DATE, revision_declared TEXT, retrieved_at_ms BIGINT NOT NULL,
  run_id TEXT, source_id TEXT, document_id TEXT, statement_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS food_current (
  namespace TEXT NOT NULL, food_id TEXT NOT NULL, revision_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, food_id)
);
CREATE TABLE IF NOT EXISTS food_ingredients (
  namespace TEXT NOT NULL, part_id TEXT NOT NULL, revision_id TEXT NOT NULL, food_id TEXT NOT NULL,
  text TEXT NOT NULL, language TEXT, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, part_id)
);
CREATE TABLE IF NOT EXISTS food_allergens (
  namespace TEXT NOT NULL, part_id TEXT NOT NULL, revision_id TEXT NOT NULL, food_id TEXT NOT NULL,
  relation TEXT NOT NULL, value TEXT NOT NULL, declared_as TEXT NOT NULL, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, part_id)
);
CREATE TABLE IF NOT EXISTS food_nutrients (
  namespace TEXT NOT NULL, part_id TEXT NOT NULL, revision_id TEXT NOT NULL, food_id TEXT NOT NULL,
  scheme TEXT NOT NULL, nutrient_id TEXT NOT NULL, name TEXT, tagname TEXT, amount_text TEXT,
  amount_decimal TEXT, unit_published TEXT, unit_normalized TEXT, unit_state TEXT NOT NULL, basis TEXT,
  derivation_json TEXT NOT NULL, value_kind TEXT NOT NULL, flags_json TEXT NOT NULL, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, part_id)
);
CREATE TABLE IF NOT EXISTS food_claims (
  namespace TEXT NOT NULL, part_id TEXT NOT NULL, revision_id TEXT NOT NULL, food_id TEXT NOT NULL,
  text TEXT NOT NULL, tag TEXT, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, part_id)
);
CREATE TABLE IF NOT EXISTS food_selection (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, selection_index INTEGER NOT NULL,
  selector_json TEXT NOT NULL, outcome TEXT NOT NULL, statements INTEGER NOT NULL, response_sha256 TEXT,
  dropped_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id, selection_index)
);
CREATE TABLE IF NOT EXISTS food_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
  cutoff_seq BIGINT NOT NULL, finished_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id)
);
CREATE TABLE IF NOT EXISTS food_generation (
  namespace TEXT NOT NULL, generation BIGINT NOT NULL, PRIMARY KEY(namespace)
);
"""


class FoodCompositionError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


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
        raise FoodCompositionError("unauthorized", f"{required} and namespace access are required")


def iso_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def as_of_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    parsed = iso_date(value)
    if parsed is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)[:10]):
        raise FoodCompositionError("invalid_request", "as_of is an ISO date (YYYY-MM-DD)")
    return parsed


# ------------------------------------------------------------------ identifiers and units


def gtin_key(value: Any) -> str | None:
    """One comparison key for UPC-A, EAN-8, EAN-13 and GTIN-14: a valid code's digits left-padded to 14.

    Leading zeros a provider adds or drops (a 12-digit UPC-A published as a 13- or 14-digit code) give the same
    key; a code failing its check digit has no key and is never matched.
    """
    from src.ingestion.product_sources import gtin_state

    state = gtin_state(value)
    if state["state"] != "valid":
        # A zero-padded code longer than 14 digits (some exports pad to 16) is still one GTIN when its check holds.
        text = str(value or "").strip()
        if re.fullmatch(r"0+\d{8,14}", text) and len(text) > 14:
            return gtin_key(text.lstrip("0").zfill(14)[-14:])
        return None
    return state["value"].zfill(14)


def normalize_unit(published: Any) -> dict[str, Any]:
    """The unit as published beside its normalized symbol; unknown units stay unknown, absent stays absent."""
    text = None if published is None else str(published).strip()
    if not text:
        return {"published": None, "normalized": None, "state": "absent"}
    normalized = UNIT_NORMALIZATION.get(text.casefold()) or UNIT_NORMALIZATION.get(text)
    return {"published": text, "normalized": normalized, "state": "known" if normalized else "unknown"}


def decimal_text(value: Any) -> str | None:
    """A published amount as a plain decimal string (decimal comma accepted); None when not a number."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")


def forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= forbidden_keys(item)
    return found


def attribution_for(provider: str, table: str | None = None) -> dict[str, Any]:
    if provider == "open-food-facts":
        return dict(ODBL_ATTRIBUTION)
    if provider == "fooddata-central":
        return dict(FDC_ATTRIBUTION)
    return dict(TABLE_ATTRIBUTION.get(str(table or ""), {"licence": "see table access decision", "text": table,
                                                          "share_alike": False}))


# ------------------------------------------------------------------ statements (FC02)


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-food-composition-record-v1.json"
    return json.loads(path.read_text())


def _validate(value: Mapping[str, Any], definition: str | None) -> None:
    import jsonschema

    target = schema() if definition is None else {"definitions": schema()["definitions"],
                                                   "$ref": f"#/definitions/{definition}"}
    try:
        jsonschema.validate(json.loads(canonical(value)), target)
    except jsonschema.ValidationError as exc:
        raise FoodCompositionError("invalid_record", f"schema: {exc.message}") from exc


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    """One adapter statement: the contract's statement definition, provenance class and no scores or advice."""
    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise FoodCompositionError("invalid_record", f"statement is not a {CONTRACT} statement")
    bad = forbidden_keys(value)
    if bad:
        raise FoodCompositionError("invalid_record", f"statements never carry scores or advice: {sorted(bad)}")
    _validate(value, "statement")
    if value["provenance_class"] != PROVENANCE[value["provider"]]:
        raise FoodCompositionError(
            "invalid_record", f"{value['provider']} records are {PROVENANCE[value['provider']]}; classes never mix"
        )
    for item in value["nutrient_values"]:
        unit = item["unit"]
        if unit["state"] == "unknown" and unit["normalized"] is not None:
            raise FoodCompositionError("invalid_record", "an unknown unit has no normalized form")
    return value


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One exported record (any of the seven record types) against the contract."""
    value = json.loads(canonical(record))
    bad = forbidden_keys(value)
    if bad:
        raise FoodCompositionError("invalid_record", f"records never carry scores or advice: {sorted(bad)}")
    _validate(value, None)
    return value


def _order_key(statement: Mapping[str, Any]) -> str:
    revision = statement["revision"]
    if revision["basis"] == "off-revision":
        text = str(revision["value"])
        return f"r{int(text):012d}" if text.isdigit() else f"t{revision.get('date') or ''}"
    return f"d{revision.get('date') or ''}|{revision['value']}"


def food_id_for(namespace: str, provider: str, provider_key: str) -> str:
    return "food:" + digest([namespace, provider, provider_key])[:24]


class FoodCompositionStore:
    """Immutable, revision-addressable food records per namespace."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "food_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise FoodCompositionError(
                "not_ready",
                f"no food composition records are stored yet; run the {SOURCE_PACK} food sources (operation food)",
            )

    def bump(self, namespace: str) -> None:
        """Advance the namespace's review/link generation: append-only and monotone (monitors watermark it)."""
        self.conn.execute(
            "INSERT INTO food_generation VALUES (?, 1) ON CONFLICT (namespace) "
            "DO UPDATE SET generation=food_generation.generation+1",
            [namespace],
        )

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "food_generation"):
            return 0
        row = self.conn.execute("SELECT generation FROM food_generation WHERE namespace=?", [namespace]).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ writes

    def observe_page(self, run_id: str, source: Mapping[str, Any], namespace: str,
                     records: Sequence[Mapping[str, Any]], *, documents: Mapping[str, str],
                     page_receipt: Mapping[str, Any]) -> dict[str, int]:
        """Project one runtime page in one transaction; replays add nothing."""
        statements = []
        for item in records:
            statement = dict(item.get("food_composition") or {})
            if statement.get("contract") != CONTRACT:
                raise FoodCompositionError("invalid_record", "page record lacks a food composition statement")
            statements.append((statement, documents.get(str(item.get("id")))))
        counts = {"created": 0, "revised": 0, "reverted": 0, "history": 0, "unchanged": 0}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            for statement, document_id in statements:
                result = self._apply(namespace, statement, run_id=run_id, source_id=source["source_id"],
                                     document_id=document_id, retrieved_at_ms=now)
                counts[result["status"]] += 1
            if page_receipt.get("selection_index") is not None and page_receipt.get("selector") is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO food_selection VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [namespace, run_id, source["source_id"], int(page_receipt["selection_index"]),
                     canonical(page_receipt.get("selector") or {}),
                     str(page_receipt.get("selector_outcome") or "returned"),
                     int(page_receipt.get("statements") or 0), page_receipt.get("response_sha256"),
                     canonical(page_receipt.get("excluded_fields_dropped") or []), now],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None = None,
              source_id: str | None = None, document_id: str | None = None) -> dict[str, Any]:
        """Record one statement outside a runtime page (tests, imports, refresh); same rules as a page."""
        self.conn.execute("BEGIN")
        try:
            result = self._apply(namespace, dict(statement), run_id=run_id, source_id=source_id,
                                 document_id=document_id, retrieved_at_ms=self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _apply(self, namespace: str, statement: dict[str, Any], *, run_id, source_id, document_id,
               retrieved_at_ms: int) -> dict[str, Any]:
        statement = validate_statement(statement)
        provider, key = statement["provider"], statement["provider_key"]
        content_digest = digest(statement)
        food_id = food_id_for(namespace, provider, key)
        order_key = _order_key(statement)
        current = self.conn.execute(
            "SELECT r.revision_id, r.content_digest, r.order_key FROM food_current c JOIN food_revisions r "
            "ON r.namespace=c.namespace AND r.revision_id=c.revision_id WHERE c.namespace=? AND c.food_id=?",
            [namespace, food_id],
        ).fetchone()
        if current and current[1] == content_digest:
            return {"status": "unchanged", "food_id": food_id, "revision_id": current[0]}
        newer = current is None or order_key >= current[2]
        known = self.conn.execute(
            "SELECT revision_id FROM food_revisions WHERE namespace=? AND food_id=? AND content_digest=? "
            "ORDER BY seq DESC LIMIT 1",
            [namespace, food_id, content_digest],
        ).fetchone()
        if known and not newer:
            return {"status": "unchanged", "food_id": food_id, "revision_id": known[0]}
        if current is None:
            gtin = (statement["identifiers"].get("gtin") or {}).get("value")
            self.conn.execute(
                "INSERT OR IGNORE INTO food_items VALUES (?,?,?,?,?,?,?,?)",
                [namespace, food_id, provider, key, statement["food_kind"], statement["provenance_class"],
                 gtin_key(gtin) if gtin else None, retrieved_at_ms],
            )
        revision_no = int(self.conn.execute(
            "SELECT coalesce(max(revision_no), 0) FROM food_revisions WHERE namespace=? AND food_id=?",
            [namespace, food_id]).fetchone()[0]) + 1
        seq = int(self.conn.execute("SELECT nextval('food_composition_seq')").fetchone()[0])
        revision_id = "food-revision:" + digest([namespace, food_id, revision_no, content_digest])[:24]
        revision = statement["revision"]
        self.conn.execute(
            "INSERT INTO food_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, food_id, revision_no, seq, content_digest, str(revision["value"]),
             revision["basis"], order_key, iso_date(revision.get("date")), revision.get("declared"),
             retrieved_at_ms, run_id, source_id, document_id, canonical(statement)],
        )
        self._parts(namespace, food_id, revision_id, statement)
        if newer:
            self.conn.execute("INSERT OR REPLACE INTO food_current VALUES (?,?,?,?)",
                              [namespace, food_id, revision_id, retrieved_at_ms])
            status = "created" if current is None else "reverted" if known else "revised"
        else:
            status = "history"
        return {"status": status, "food_id": food_id, "revision_id": revision_id, "revision_no": revision_no}

    def _parts(self, namespace: str, food_id: str, revision_id: str, statement: Mapping[str, Any]) -> None:
        def pid(prefix: str, index: int, item: Any) -> str:
            return f"{prefix}:" + digest([revision_id, index, item])[:24]

        for i, item in enumerate(statement["ingredient_statements"]):
            self.conn.execute("INSERT OR IGNORE INTO food_ingredients VALUES (?,?,?,?,?,?,?)", [
                namespace, pid("food-ingredients", i, item), revision_id, food_id, item["text"],
                item.get("language"), canonical(item.get("locator") or {})])
        for i, item in enumerate(statement["allergen_declarations"]):
            self.conn.execute("INSERT OR IGNORE INTO food_allergens VALUES (?,?,?,?,?,?,?,?)", [
                namespace, pid("food-allergen", i, item), revision_id, food_id, item["relation"], item["value"],
                item["declared_as"], canonical(item.get("locator") or {})])
        for i, item in enumerate(statement["nutrient_values"]):
            nutrient, unit = item["nutrient"], item["unit"]
            self.conn.execute("INSERT OR IGNORE INTO food_nutrients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                namespace, pid("food-nutrient", i, item), revision_id, food_id, nutrient["scheme"], nutrient["id"],
                nutrient.get("name"), nutrient.get("tagname"), item.get("amount"), item.get("amount_decimal"),
                unit["published"], unit["normalized"], unit["state"], item.get("basis"),
                canonical(item.get("derivation")), item["value_kind"], canonical(item.get("value_flags") or {}),
                canonical(item.get("locator") or {})])
        for i, item in enumerate(statement["labelling_claims"]):
            self.conn.execute("INSERT OR IGNORE INTO food_claims VALUES (?,?,?,?,?,?,?)", [
                namespace, pid("food-claim", i, item), revision_id, food_id, item["text"], item.get("tag"),
                canonical(item.get("locator") or {})])

    def finish_source(self, run_id: str, source_id: str, namespace: str, status: str) -> dict[str, Any]:
        cutoff = self.conn.execute("SELECT coalesce(max(seq), 0) FROM food_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        self.conn.execute("INSERT OR REPLACE INTO food_source_runs VALUES (?,?,?,?,?,?)",
                          [namespace, run_id, source_id, status, int(cutoff), self.now()])
        return {"source_id": source_id, "status": status, "complete": status == "complete",
                "cutoff_seq": int(cutoff)}

    # ------------------------------------------------------------------ reads

    def item(self, namespace: str, food_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT food_id, provider, provider_key, food_kind, provenance_class, gtin_key FROM food_items "
            "WHERE namespace=? AND food_id=?", [namespace, food_id]).fetchone()
        if row is None:
            raise FoodCompositionError("not_found", "food record is not visible in this namespace")
        return dict(zip(("food_id", "provider", "provider_key", "food_kind", "provenance_class", "gtin_key"), row))

    def items(self, namespace: str, *, provider: str | None = None, gtin: str | None = None,
              food_kind: str | None = None) -> list[dict[str, Any]]:
        key = None
        if gtin is not None:
            key = gtin_key(gtin)
            if key is None:
                return []
        rows = self.conn.execute(
            "SELECT food_id FROM food_items WHERE namespace=? AND (? IS NULL OR provider=?) AND "
            "(? IS NULL OR gtin_key=?) AND (? IS NULL OR food_kind=?) ORDER BY provider, provider_key",
            [namespace, provider, provider, key, key, food_kind, food_kind]).fetchall()
        return [self.item(namespace, r[0]) for r in rows]

    def resolve_food(self, namespace: str, food: str) -> str:
        """A food id, or ``provider:provider_key`` (e.g. ``fooddata-central:fdc:9990101``)."""
        if food.startswith("food:"):
            return food
        provider, _, key = food.partition(":")
        if provider not in PROVIDERS or not key:
            raise FoodCompositionError("invalid_request", "name a food id or provider:provider_key")
        return food_id_for(namespace, provider, key)

    _REVISION_KEYS = ("revision_id", "revision_no", "seq", "content_digest", "revision_value", "revision_basis",
                      "order_key", "revision_date", "revision_declared", "retrieved_at_ms", "run_id", "source_id",
                      "document_id")

    def revisions(self, namespace: str, food_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_KEYS)} FROM food_revisions WHERE namespace=? AND food_id=? "
            "ORDER BY seq", [namespace, food_id]).fetchall()
        result = []
        for row in rows:
            item = dict(zip(self._REVISION_KEYS, row))
            item["revision_date"] = None if item["revision_date"] is None else iso_date(item["revision_date"]).isoformat()
            item["retrieved_at"] = iso_from_ms(item["retrieved_at_ms"])
            result.append(item)
        return result

    def current_revision(self, namespace: str, food_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT revision_id FROM food_current WHERE namespace=? AND food_id=?",
                                [namespace, food_id]).fetchone()
        return next((r for r in self.revisions(namespace, food_id) if row and r["revision_id"] == row[0]), None)

    def revision_as_of(self, namespace: str, food_id: str, as_of: date | None) -> tuple[dict | None, list[dict]]:
        """The revision current at a date by the provider's own order (else the current one), and later ones."""
        if as_of is None:
            return self.current_revision(namespace, food_id), []
        revisions = self.revisions(namespace, food_id)
        eligible = [r for r in revisions if r["revision_date"] and iso_date(r["revision_date"]) <= as_of]
        later = [r for r in revisions if r not in eligible]
        chosen = max(eligible, key=lambda r: (r["order_key"], r["seq"])) if eligible else None
        return chosen, later

    def statement(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT statement_json FROM food_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise FoodCompositionError("not_found", "revision is not visible in this namespace")
        return json.loads(row[0])

    def parts(self, namespace: str, revision_id: str) -> dict[str, Any]:
        def rows(sql: str, keys: Sequence[str]) -> list[dict[str, Any]]:
            found = [dict(zip(keys, r)) for r in self.conn.execute(sql, [namespace, revision_id]).fetchall()]
            for item in found:
                for key in ("locator", "derivation", "value_flags"):
                    if key in item:
                        item[key] = _load(item[key], None if key == "derivation" else {})
            return found

        nutrients = rows(
            "SELECT part_id, scheme, nutrient_id, name, tagname, amount_text, amount_decimal, unit_published, "
            "unit_normalized, unit_state, basis, derivation_json, value_kind, flags_json, locator_json FROM "
            "food_nutrients WHERE namespace=? AND revision_id=? ORDER BY scheme, nutrient_id, basis, value_kind",
            ("part_id", "scheme", "nutrient_id", "name", "tagname", "amount", "amount_decimal", "unit_published",
             "unit_normalized", "unit_state", "basis", "derivation", "value_kind", "value_flags", "locator"))
        for item in nutrients:
            item["unit"] = {"published": item.pop("unit_published"), "normalized": item.pop("unit_normalized"),
                            "state": item.pop("unit_state")}
        return {
            "ingredient_statements": rows(
                "SELECT part_id, text, language, locator_json FROM food_ingredients WHERE namespace=? AND "
                "revision_id=? ORDER BY language NULLS FIRST, part_id",
                ("part_id", "text", "language", "locator")),
            "allergen_declarations": rows(
                "SELECT part_id, relation, value, declared_as, locator_json FROM food_allergens WHERE namespace=? "
                "AND revision_id=? ORDER BY relation, value", ("part_id", "relation", "value", "declared_as",
                                                              "locator")),
            "nutrient_values": nutrients,
            "labelling_claims": rows(
                "SELECT part_id, text, tag, locator_json FROM food_claims WHERE namespace=? AND revision_id=? "
                "ORDER BY text", ("part_id", "text", "tag", "locator")),
        }

    def citation(self, namespace: str, food_id: str, revision: Mapping[str, Any]) -> dict[str, Any]:
        """Provider, key, revision and retrieval time of one revision, with the provider's attribution."""
        head = self.item(namespace, food_id)
        statement = self.statement(namespace, revision["revision_id"])
        return {
            "provider": head["provider"],
            "provider_key": head["provider_key"],
            "provenance_class": head["provenance_class"],
            "revision_id": revision["revision_id"],
            "revision": {"value": revision["revision_value"], "basis": revision["revision_basis"],
                         "date": revision["revision_date"]},
            "retrieved_at": revision["retrieved_at"],
            "url": statement.get("url"),
            "attribution": statement["attribution"],
        }

    def export_records(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        """One revision as the contract's seven record types (FC02), each with the full provenance envelope."""
        row = self.conn.execute("SELECT food_id FROM food_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise FoodCompositionError("not_found", "revision is not visible in this namespace")
        food_id = row[0]
        revision = next(r for r in self.revisions(namespace, food_id) if r["revision_id"] == revision_id)
        statement = self.statement(namespace, revision_id)
        envelope = {
            "contract": CONTRACT,
            "provider": statement["provider"],
            "provider_key": statement["provider_key"],
            "provenance_class": statement["provenance_class"],
            "revision": {"revision_id": revision_id, "value": revision["revision_value"],
                         "basis": revision["revision_basis"], "revision_no": revision["revision_no"]},
            "retrieved_at": revision["retrieved_at"],
            "as_of": revision["revision_date"],
            "attribution": statement["attribution"],
        }
        records = [
            {**envelope, "record_type": statement["food_kind"],
             "body": {"identifiers": statement["identifiers"], "names": statement["names"]}},
            {**envelope, "record_type": "label-revision", "body": {"statement": statement}},
        ]
        kinds = (("ingredient_statements", "ingredient-statement"), ("allergen_declarations", "allergen-declaration"),
                 ("nutrient_values", "nutrient-value"), ("labelling_claims", "labelling-claim"))
        for key, record_type in kinds:
            records += [{**envelope, "record_type": record_type, "body": item} for item in statement[key]]
        return [validate_record(r) for r in records]


class FoodCompositionProjector:
    """Source-pack runtime projector for ``noesis-food-composition-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = FoodCompositionStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("food_composition") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        document_ids = {
            str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(item["document_id"])
            for item in documents or []
        }
        return self.store.observe_page(run_id, source, self._namespace(source), records, documents=document_ids,
                                       page_receipt=page_receipt)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        return self.store.finish_source(run_id, source["source_id"], self._namespace(source), status)


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Products bundle's optional ``food`` feature is selected in the active plan (default off)."""
    del namespace  # composition selection is deployment-wide
    from src.kb.products import products_feature_enabled

    return products_feature_enabled(conn, FEATURE)


__all__ = [
    "ANSWER_CONTRACT",
    "BOUNDARY",
    "CONTRACT",
    "FDC_ATTRIBUTION",
    "FoodCompositionError",
    "FoodCompositionProjector",
    "FoodCompositionStore",
    "ODBL_ATTRIBUTION",
    "PROVENANCE",
    "PROVIDERS",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "attribution_for",
    "decimal_text",
    "feature_enabled",
    "gtin_key",
    "normalize_unit",
    "validate_record",
    "validate_statement",
]

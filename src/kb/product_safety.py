"""Product safety notices and recalls: the ``products.safety-notices`` record owner (#1916).

Records (contract ``noesis-product-safety-notice-v1``), namespace-scoped and
revision-addressable, reusing the Products scopes (``knowledge:products:*``):

* **notice** - one identity per provider and notice number (a Safety Gate
  alert, a CPSC recall, an NHTSA campaign, a RASFF notification);
* **notice-revision** - every distinct provider payload of a notice, immutable,
  with the provider's own publication/update date. A newer declared revision
  becomes current; an older one delivered later is kept as history and never
  replaces it; replaying the current payload adds nothing, and a payload that
  returns to earlier content with a date at least as new is a new revision (a
  reversion), never a silent no-op;
* **hazard**, **affected-product-identification**, **corrective-action**,
  **issuing-authority** (with notifying country and notice type), **party** and
  **follow-up** - per revision, verbatim with a JSON-pointer locator. GTIN,
  model, batch, brand and serial identifiers stay native; GTIN validity reuses
  :func:`src.ingestion.product_sources.gtin_state`. Nothing is normalised into
  a product identity here;
* **citation** - standard and legal-act references a revision states, resolved
  to ``technology.standards`` and ``legal.works`` records by exact reference or
  identifier only (R07); a link stays attached to the revision that cited it;
* **notice match** - a reviewable candidate between a notice's identification
  and a ``products.identities`` model (R06). Only an accepted review attaches a
  notice to a product; siblings, other sizes and regional variants never are.

No record or answer carries a safety verdict, risk score or consumer advice. A
product without an accepted match has *no notice on record*; the only advice
ever shown is the authority's corrective-action text, quoted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.ingestion.product_sources import SAFETY_CONNECTORS, SAFETY_CONTRACT, gtin_state
from src.kb.products import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    _brand_key,
    _common_prefix,
    designation_key,
)

CONTRACT = SAFETY_CONTRACT
MATCH_CONTRACT = "noesis-product-notice-match-v1"
DOSSIER_CONTRACT = "noesis-product-notice-dossier-v1"
PARTY_LINK_CONTRACT = "noesis-product-notice-party-link-v1"
DEFAULT_NAMESPACE = "global"
SOURCE_PACK = "products-displays"
MATCH_METHOD = "gtin-brand-designation-v1"
MATCH_DECISIONS = frozenset({"accepted", "rejected", "deferred"})
NEWS_SCOPE = "knowledge:read"
STANDARDS_READ_SCOPE = "knowledge:standards:read"
LEGAL_READ_SCOPE = "knowledge:legal:read"
ENTITY_WRITE_SCOPE = "knowledge:entity-history:write"
ENTITY_REVIEW_SCOPE = "knowledge:entity-history:review"
ENTITY_EXECUTE_SCOPE = "knowledge:entity-history:execute"
NO_NOTICE = "no notice on record"
FORBIDDEN_ANSWER_KEYS = frozenset(
    {
        "verdict",
        "safe",
        "unsafe",
        "safety_status",
        "risk_score",
        "score",
        "recommendation",
        "advice",
        "consumer_advice",
    }
)
BOUNDARY = (
    "Notices are quoted as the issuing authority published them. No safety verdict, risk score or advice is given; "
    "a product without an accepted notice match has no notice on record, which is not a statement that it is safe."
)
# One token boundary for identifiers and citations (notice numbers, CELEX, ELI, standard references), shared by
# citation extraction and the news search: an identifier is delimited by anything that is not a letter or digit,
# so path separators, punctuation and whitespace all delimit it.
_BOUNDARY_CHARS = "0-9A-Za-z"

_DDL = """
CREATE SEQUENCE IF NOT EXISTS product_safety_seq;
CREATE TABLE IF NOT EXISTS product_safety_notices (
  namespace TEXT NOT NULL, notice_id TEXT NOT NULL, provider TEXT NOT NULL, notice_number TEXT NOT NULL,
  jurisdiction TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, notice_id)
);
CREATE TABLE IF NOT EXISTS product_safety_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  seq BIGINT NOT NULL, content_digest TEXT NOT NULL, revision_date DATE, revision_declared TEXT,
  order_basis TEXT NOT NULL, published DATE, updated DATE, notice_type TEXT NOT NULL, notice_type_declared TEXT,
  authority TEXT NOT NULL, authority_declared TEXT, notifying_country TEXT, notifying_country_declared TEXT,
  observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, document_id TEXT, payload_pointer TEXT,
  statement_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS product_safety_current (
  namespace TEXT NOT NULL, notice_id TEXT NOT NULL, revision_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, notice_id)
);
CREATE TABLE IF NOT EXISTS product_safety_identifications (
  namespace TEXT NOT NULL, identification_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  group_index INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, value_key TEXT, gtin_state TEXT,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, identification_id)
);
CREATE TABLE IF NOT EXISTS product_safety_hazards (
  namespace TEXT NOT NULL, hazard_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  hazard_type TEXT, risk_level TEXT, description TEXT, detail_json TEXT NOT NULL, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, hazard_id)
);
CREATE TABLE IF NOT EXISTS product_safety_actions (
  namespace TEXT NOT NULL, action_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  text TEXT NOT NULL, measure_type TEXT, taken_by TEXT, detail_json TEXT NOT NULL, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, action_id)
);
CREATE TABLE IF NOT EXISTS product_safety_parties (
  namespace TEXT NOT NULL, party_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  role TEXT NOT NULL, name TEXT NOT NULL, party_key TEXT NOT NULL, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, party_id)
);
CREATE TABLE IF NOT EXISTS product_safety_followups (
  namespace TEXT NOT NULL, followup_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  followup_date DATE, date_declared TEXT, country TEXT, country_declared TEXT, text TEXT, detail_json TEXT NOT NULL,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, followup_id)
);
CREATE TABLE IF NOT EXISTS product_safety_citations (
  namespace TEXT NOT NULL, citation_id TEXT NOT NULL, revision_id TEXT NOT NULL, notice_id TEXT NOT NULL,
  kind TEXT NOT NULL, raw TEXT NOT NULL, reference_key TEXT NOT NULL, identifiers_json TEXT NOT NULL,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, citation_id)
);
CREATE TABLE IF NOT EXISTS product_safety_citation_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, citation_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, target_label TEXT,
  basis TEXT NOT NULL, identifier TEXT NOT NULL, principal_id TEXT NOT NULL, linked_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
CREATE TABLE IF NOT EXISTS product_safety_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, notice_id TEXT NOT NULL, group_index INTEGER NOT NULL,
  model_id TEXT NOT NULL, method TEXT NOT NULL, basis TEXT NOT NULL, candidate_state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, reasons_json TEXT NOT NULL, revision_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, match_id)
);
CREATE TABLE IF NOT EXISTS product_safety_match_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, match_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, candidate_state TEXT NOT NULL, revision_id TEXT NOT NULL,
  principal_id TEXT NOT NULL, reviewed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, review_id)
);
CREATE TABLE IF NOT EXISTS product_safety_party_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, party_key TEXT NOT NULL, party_name TEXT NOT NULL,
  entity_id TEXT NOT NULL, decision TEXT NOT NULL, decision_id TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL, reviewer TEXT NOT NULL, created_at_ms BIGINT NOT NULL, reverted_by TEXT,
  PRIMARY KEY(namespace, link_id)
);
CREATE TABLE IF NOT EXISTS product_safety_selection (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, selection_index INTEGER NOT NULL,
  selector_json TEXT NOT NULL, outcome TEXT NOT NULL, notices INTEGER NOT NULL, response_sha256 TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, selection_index)
);
CREATE TABLE IF NOT EXISTS product_safety_generation (
  namespace TEXT NOT NULL, generation BIGINT NOT NULL, PRIMARY KEY(namespace)
);
CREATE TABLE IF NOT EXISTS product_safety_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
  cutoff_seq BIGINT NOT NULL, finished_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id)
);
"""


class ProductSafetyError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


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
        raise ProductSafetyError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require(scopes: Iterable[str], *required: str) -> None:
    """Call-time check for scopes an optional argument or a consumed store needs."""
    scopes = set(scopes)
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise ProductSafetyError(
            "unauthorized", f"{', '.join(missing)} is required for this request"
        )


def gtin_key(value: Any) -> str | None:
    """One comparison key for a GTIN on both sides of a match: a valid code's digits, left-padded to 14."""
    state = gtin_state(value)
    return state["value"].zfill(14) if state["state"] == "valid" else None


def iso_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_rank(value: Any) -> date:
    return iso_date(value) or date.min


def token_regex(body: str) -> re.Pattern[str]:
    """Python form of the shared identifier boundary around ``body`` (a regex)."""
    return re.compile(rf"(?<![{_BOUNDARY_CHARS}])(?:{body})(?![{_BOUNDARY_CHARS}])")


def token_sql_pattern(literal: str) -> str:
    """RE2 (DuckDB) form of the same boundary around a literal identifier."""
    return rf"(^|[^{_BOUNDARY_CHARS}]){re.escape(literal)}($|[^{_BOUNDARY_CHARS}])"


_STANDARD = token_regex(
    r"(?:EN|ISO|IEC)(?:\s*/\s*(?:ISO|IEC)|\s+(?:ISO|IEC))*\s+\d+(?:-\d+)*(?::\d{4})?(?:\s*\+\s*A\d+(?::\d{4})?)*"
)
_CELEX = token_regex(r"[0-9][12]\d{3}[A-Z]{1,2}\d{4}")
_ELI = token_regex(
    r"https?://data\.europa\.eu/eli/(?:reg|dir|dec|reg_impl|reg_del|dir_impl|dir_del)/\d{4}/\d+"
    r"(?:/[a-z]+)?"
)
_NAMED_ACT = token_regex(
    r"(?P<form>Regulation|Directive|Decision)\s+(?:\((?P<tag1>EU|EC|EEC|Euratom)\)\s+)?(?:No\.?\s+)?"
    r"(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?P<tag2>EU|EC|EEC|Euratom))?"
)
_ACT_LETTER = {"Regulation": "R", "Directive": "L", "Decision": "D"}


def standard_key(value: str) -> str:
    """Exact-reference key for a standard: whitespace collapsed, case folded, nothing else changed."""
    return re.sub(
        r"\s*([/+:])\s*", r"\1", re.sub(r"\s+", " ", value.strip())
    ).casefold()


def _named_celex(match: re.Match[str]) -> str | None:
    """CELEX number of an act cited by its official number (year and number are read by the act's own form)."""
    a, b = match.group("a"), match.group("b")
    # "No 178/2002" and pre-2015 (EC)/(EEC) regulations put the number first; directives and (EU) acts the year.
    numbered_first = bool(re.search(r"\bNo\.?\s", match.group(0))) or (
        match.group("form") == "Regulation"
        and match.group("tag1") in {"EC", "EEC", "Euratom"}
    )
    year, number = (b, a) if numbered_first else (a, b)
    if len(year) == 2:
        year = ("19" if int(year) > 50 else "20") + year
    if len(year) != 4:
        return None
    return f"3{year}{_ACT_LETTER[match.group('form')]}{int(number):04d}"


def extract_citations(text: str, pointer: str) -> list[dict[str, Any]]:
    """Standard and legal-act references a text states, verbatim with their span; nothing inferred from topic."""
    found: list[dict[str, Any]] = []
    for match in _STANDARD.finditer(text):
        found.append(
            {
                "kind": "standard",
                "raw": match.group(0),
                "reference_key": standard_key(match.group(0)),
                "identifiers": {"reference": match.group(0)},
                "locator": {
                    "json_pointer": pointer,
                    "span": [match.start(), match.end()],
                },
            }
        )
    for pattern, label in ((_CELEX, "celex"), (_ELI, "eli")):
        for match in pattern.finditer(text):
            value = match.group(0)
            found.append(
                {
                    "kind": "legal",
                    "raw": value,
                    "reference_key": f"{label}:{value}",
                    "identifiers": {label: value},
                    "locator": {
                        "json_pointer": pointer,
                        "span": [match.start(), match.end()],
                    },
                }
            )
    for match in _NAMED_ACT.finditer(text):
        celex = _named_celex(match)
        if celex:
            found.append(
                {
                    "kind": "legal",
                    "raw": match.group(0),
                    "reference_key": f"celex:{celex}",
                    "identifiers": {
                        "celex": celex,
                        "celex_basis": "derived from the act's official number",
                    },
                    "locator": {
                        "json_pointer": pointer,
                        "span": [match.start(), match.end()],
                    },
                }
            )
    return found


def _party_key(name: str) -> str:
    from src.kb.entities import normalize_surface

    return (
        "product-notice-party:"
        + digest(normalize_surface(name) or name.casefold())[:24]
    )


def notice_id_for(namespace: str, provider: str, notice_number: str) -> str:
    return "product-safety-notice:" + digest([namespace, provider, notice_number])[:24]


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Products bundle's optional ``safety`` feature is selected in the active plan (default off)."""
    del namespace  # composition selection is deployment-wide
    from src.kb.products import products_feature_enabled

    return products_feature_enabled(conn, "safety")


def _table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]
        ).fetchone()
    )


class ProductSafetyStore:
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

    def _bump(self, namespace: str) -> None:
        """Advance the namespace's review generation (matches, reviews, party links): append-only, monotone."""
        self.conn.execute(
            "INSERT INTO product_safety_generation VALUES (?, 1) ON CONFLICT (namespace) "
            "DO UPDATE SET generation=product_safety_generation.generation+1",
            [namespace],
        )

    def generation(self, namespace: str) -> int:
        row = self.conn.execute(
            "SELECT generation FROM product_safety_generation WHERE namespace=?",
            [namespace],
        ).fetchone()
        return int(row[0]) if row else 0

    def _require_ready(self) -> None:
        if not self.ready():
            raise ProductSafetyError(
                "not_ready",
                "no product-safety notices are stored yet; run the products-displays notice sources "
                "(operation notices)",
            )

    def ready(self) -> bool:
        return _table(self.conn, "product_safety_revisions")

    # ------------------------------------------------------------------ writes

    def observe_page(
        self,
        run_id: str,
        source: Mapping[str, Any],
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        documents: Mapping[str, str],
        page_receipt: Mapping[str, Any],
    ) -> dict[str, int]:
        """Project one runtime page in one transaction; replays add nothing."""
        statements = []
        for item in records:
            statement = dict(item.get("product_safety_notice") or {})
            if statement.get("contract") != CONTRACT:
                raise ProductSafetyError(
                    "invalid_record",
                    "page record lacks a product-safety notice statement",
                )
            statements.append((statement, documents.get(str(item.get("id")))))
        counts = {
            "created": 0,
            "revised": 0,
            "reverted": 0,
            "history": 0,
            "unchanged": 0,
        }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            for statement, document_id in statements:
                result = self._apply(
                    namespace,
                    statement,
                    run_id=run_id,
                    source_id=source["source_id"],
                    document_id=document_id,
                    observed_at_ms=now,
                )
                counts[result["status"]] += 1
            if (
                page_receipt.get("selection_index") is not None
                and page_receipt.get("selector") is not None
            ):
                self.conn.execute(
                    "INSERT OR IGNORE INTO product_safety_selection VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        run_id,
                        source["source_id"],
                        int(page_receipt["selection_index"]),
                        canonical(page_receipt.get("selector") or {}),
                        str(page_receipt.get("selector_outcome") or "returned"),
                        int(page_receipt.get("notices") or 0),
                        page_receipt.get("response_sha256"),
                        now,
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def apply(
        self,
        namespace: str,
        statement: Mapping[str, Any],
        *,
        run_id: str | None = None,
        source_id: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one notice statement outside a runtime page (tests, imports); same rules as a page."""
        self.conn.execute("BEGIN")
        try:
            result = self._apply(
                namespace,
                dict(statement),
                run_id=run_id,
                source_id=source_id,
                document_id=document_id,
                observed_at_ms=self.now(),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _apply(
        self,
        namespace: str,
        statement: dict[str, Any],
        *,
        run_id,
        source_id,
        document_id,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        provider = str(statement.get("provider") or "")
        number = str(statement.get("notice_number") or "")
        if (
            statement.get("contract") != CONTRACT
            or provider not in SAFETY_CONNECTORS
            or not number
        ):
            raise ProductSafetyError(
                "invalid_record",
                "a notice statement needs its contract, provider and number",
            )
        if FORBIDDEN_ANSWER_KEYS & set(statement):
            raise ProductSafetyError(
                "invalid_record",
                "notice statements never carry verdicts, scores or advice",
            )
        pointer = statement.pop("payload_pointer", None)
        content_digest = digest(statement)
        notice_id = notice_id_for(namespace, provider, number)
        current = self.conn.execute(
            "SELECT r.revision_id, r.content_digest, r.revision_date, r.seq FROM product_safety_current c "
            "JOIN product_safety_revisions r ON r.namespace=c.namespace AND r.revision_id=c.revision_id "
            "WHERE c.namespace=? AND c.notice_id=?",
            [namespace, notice_id],
        ).fetchone()
        if current and current[1] == content_digest:
            return {
                "status": "unchanged",
                "notice_id": notice_id,
                "revision_id": current[0],
            }
        revision_date = iso_date(statement.get("revision_date"))
        newer = current is None or _date_rank(revision_date) >= _date_rank(current[2])
        known = self.conn.execute(
            "SELECT revision_id FROM product_safety_revisions WHERE namespace=? AND notice_id=? AND content_digest=? "
            "ORDER BY seq DESC LIMIT 1",
            [namespace, notice_id, content_digest],
        ).fetchone()
        if known and not newer:
            # An older payload already kept, delivered again late: nothing new happened at the source.
            return {
                "status": "unchanged",
                "notice_id": notice_id,
                "revision_id": known[0],
            }
        if current is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_notices VALUES (?,?,?,?,?,?)",
                [
                    namespace,
                    notice_id,
                    provider,
                    number,
                    str(statement.get("jurisdiction") or ""),
                    observed_at_ms,
                ],
            )
        number_row = self.conn.execute(
            "SELECT coalesce(max(revision_no), 0) FROM product_safety_revisions WHERE namespace=? AND notice_id=?",
            [namespace, notice_id],
        ).fetchone()
        revision_no = int(number_row[0]) + 1
        seq = int(
            self.conn.execute("SELECT nextval('product_safety_seq')").fetchone()[0]
        )
        revision_id = (
            "product-safety-revision:"
            + digest([namespace, notice_id, revision_no, content_digest])[:24]
        )
        notice_type = dict(statement.get("notice_type") or {})
        authority = dict(statement.get("issuing_authority") or {})
        country = dict(statement.get("notifying_country") or {})
        self.conn.execute(
            "INSERT INTO product_safety_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                notice_id,
                revision_no,
                seq,
                content_digest,
                revision_date,
                statement.get("updated_declared")
                or statement.get("published_declared"),
                str(statement.get("order_basis") or "source-date"),
                iso_date(statement.get("published")),
                iso_date(statement.get("updated")),
                str(notice_type.get("value") or "unknown"),
                notice_type.get("declared"),
                str(authority.get("value") or "unknown"),
                authority.get("declared"),
                country.get("code"),
                country.get("declared"),
                observed_at_ms,
                run_id,
                source_id,
                document_id,
                pointer,
                canonical(statement),
            ],
        )
        self._parts(namespace, notice_id, revision_id, statement)
        if newer:
            self.conn.execute(
                "INSERT OR REPLACE INTO product_safety_current VALUES (?,?,?,?)",
                [namespace, notice_id, revision_id, observed_at_ms],
            )
            status = (
                "created" if current is None else "reverted" if known else "revised"
            )
        else:
            status = "history"
        return {
            "status": status,
            "notice_id": notice_id,
            "revision_id": revision_id,
            "revision_no": revision_no,
        }

    def _parts(
        self,
        namespace: str,
        notice_id: str,
        revision_id: str,
        statement: Mapping[str, Any],
    ) -> None:
        def rid(prefix: str, index: int, item: Any) -> str:
            return f"{prefix}:" + digest([revision_id, index, item])[:24]

        for i, item in enumerate(statement.get("identifications") or []):
            kind, value = str(item["kind"]), str(item["value"])
            # A GTIN that is not valid keeps its verbatim string as the key: queryable, never matched to a product.
            key = (
                (gtin_key(value) or f"raw:{value}")
                if kind == "gtin"
                else designation_key(value)
                if kind == "model"
                else _brand_key(value)
                if kind == "brand"
                else None
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_identifications VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("product-safety-identification", i, item),
                    revision_id,
                    notice_id,
                    int(item.get("group", 0)),
                    kind,
                    value,
                    key or None,
                    item.get("gtin_state"),
                    canonical(
                        {
                            **dict(item.get("locator") or {}),
                            **(
                                {"part": item["part"]}
                                if item.get("part") is not None
                                else {}
                            ),
                        }
                    ),
                ],
            )
        texts: list[tuple[str, str]] = []
        for i, item in enumerate(statement.get("hazards") or []):
            locator = dict(item.get("locator") or {})
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_hazards VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("product-safety-hazard", i, item),
                    revision_id,
                    notice_id,
                    item.get("hazard_type"),
                    item.get("risk_level"),
                    item.get("description"),
                    canonical(
                        {
                            k: v
                            for k, v in item.items()
                            if k
                            not in {
                                "hazard_type",
                                "risk_level",
                                "description",
                                "locator",
                            }
                        }
                    ),
                    canonical(locator),
                ],
            )
            if item.get("description"):
                texts.append(
                    (
                        item["description"],
                        locator.get("description_pointer")
                        or locator.get("json_pointer"),
                    )
                )
            if item.get("analytical_result"):
                texts.append(
                    (
                        item["analytical_result"],
                        str(locator.get("json_pointer")) + "/analyticalResult",
                    )
                )
        for i, item in enumerate(statement.get("corrective_actions") or []):
            locator = dict(item.get("locator") or {})
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_actions VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("product-safety-action", i, item),
                    revision_id,
                    notice_id,
                    str(item["text"]),
                    item.get("measure_type"),
                    item.get("taken_by"),
                    canonical(
                        {
                            k: v
                            for k, v in item.items()
                            if k not in {"text", "measure_type", "taken_by", "locator"}
                        }
                    ),
                    canonical(locator),
                ],
            )
            texts.append((str(item["text"]), locator.get("json_pointer")))
        for i, item in enumerate(statement.get("parties") or []):
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_parties VALUES (?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("product-safety-party", i, item),
                    revision_id,
                    notice_id,
                    str(item["role"]),
                    str(item["name"]),
                    _party_key(str(item["name"])),
                    canonical(item.get("locator") or {}),
                ],
            )
        for i, item in enumerate(statement.get("followups") or []):
            country = dict(item.get("country") or {})
            self.conn.execute(
                "INSERT OR IGNORE INTO product_safety_followups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("product-safety-followup", i, item),
                    revision_id,
                    notice_id,
                    iso_date(item.get("date")),
                    item.get("date_declared"),
                    country.get("code"),
                    country.get("declared"),
                    item.get("text"),
                    canonical({"measures": item.get("measures") or []}),
                    canonical(item.get("locator") or {}),
                ],
            )
            if item.get("text"):
                texts.append(
                    (
                        str(item["text"]),
                        dict(item.get("locator") or {}).get("json_pointer"),
                    )
                )
        for item in statement.get("compliance") or []:
            texts.append(
                (str(item["text"]), dict(item.get("locator") or {}).get("json_pointer"))
            )
        seen: set[tuple[str, str, str]] = set()
        for text, pointer in texts:
            for citation in extract_citations(text, str(pointer or "")):
                key = (citation["reference_key"], citation["raw"], str(pointer))
                if key in seen:
                    continue
                seen.add(key)
                self.conn.execute(
                    "INSERT OR IGNORE INTO product_safety_citations VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        "product-safety-citation:" + digest([revision_id, key])[:24],
                        revision_id,
                        notice_id,
                        citation["kind"],
                        citation["raw"],
                        citation["reference_key"],
                        canonical(citation["identifiers"]),
                        canonical(citation["locator"]),
                    ],
                )

    def finish_source(
        self, run_id: str, source_id: str, namespace: str, status: str
    ) -> dict[str, Any]:
        """Record a notice source's run outcome; monitors only advance past runs where every notice source completed."""
        cutoff = self.conn.execute(
            "SELECT coalesce(max(seq), 0) FROM product_safety_revisions WHERE namespace=?",
            [namespace],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO product_safety_source_runs VALUES (?,?,?,?,?,?)",
            [namespace, run_id, source_id, status, int(cutoff), self.now()],
        )
        return {
            "source_id": source_id,
            "status": status,
            "complete": status == "complete",
            "cutoff_seq": int(cutoff),
        }

    # ------------------------------------------------------------------ reads

    def _notice_row(self, namespace: str, notice_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT notice_id, provider, notice_number, jurisdiction FROM product_safety_notices "
            "WHERE namespace=? AND notice_id=?",
            [namespace, notice_id],
        ).fetchone()
        if row is None:
            raise ProductSafetyError(
                "not_found", "notice is not visible in this namespace"
            )
        return dict(
            zip(("notice_id", "provider", "notice_number", "jurisdiction"), row)
        )

    def resolve_notice(self, namespace: str, notice: str) -> str:
        """A notice id, or ``provider:notice_number`` (the number verbatim, slashes included)."""
        if notice.startswith("product-safety-notice:"):
            return notice
        provider, _, number = notice.partition(":")
        if provider not in SAFETY_CONNECTORS or not number:
            raise ProductSafetyError(
                "invalid_request", "name a notice id or provider:notice_number"
            )
        return notice_id_for(namespace, provider, number)

    def revisions(self, namespace: str, notice_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id, revision_no, seq, content_digest, revision_date, revision_declared, order_basis, "
            "published, updated, notice_type, notice_type_declared, authority, authority_declared, notifying_country, "
            "notifying_country_declared, observed_at_ms, run_id, source_id, document_id, payload_pointer "
            "FROM product_safety_revisions WHERE namespace=? AND notice_id=? ORDER BY seq",
            [namespace, notice_id],
        ).fetchall()
        keys = (
            "revision_id",
            "revision_no",
            "seq",
            "content_digest",
            "revision_date",
            "revision_declared",
            "order_basis",
            "published",
            "updated",
            "notice_type",
            "notice_type_declared",
            "authority",
            "authority_declared",
            "notifying_country",
            "notifying_country_declared",
            "observed_at_ms",
            "run_id",
            "source_id",
            "document_id",
            "payload_pointer",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ("revision_date", "published", "updated"):
                item[key] = (
                    None if item[key] is None else iso_date(item[key]).isoformat()
                )
            result.append(item)
        return result

    def revision_as_of(
        self, namespace: str, notice_id: str, as_of: date | None
    ) -> tuple[dict | None, list[dict]]:
        """The revision current as of a date by the source's own date (else the current one), and later revisions."""
        revisions = self.revisions(namespace, notice_id)
        if as_of is None:
            row = self.conn.execute(
                "SELECT revision_id FROM product_safety_current WHERE namespace=? AND notice_id=?",
                [namespace, notice_id],
            ).fetchone()
            current = next(
                (r for r in revisions if row and r["revision_id"] == row[0]), None
            )
            return current, []
        eligible = [r for r in revisions if _date_rank(r["revision_date"]) <= as_of]
        later = [r for r in revisions if _date_rank(r["revision_date"]) > as_of]
        chosen = (
            max(eligible, key=lambda r: (_date_rank(r["revision_date"]), r["seq"]))
            if eligible
            else None
        )
        return chosen, later

    def parts(self, namespace: str, revision_id: str) -> dict[str, Any]:
        def rows(sql: str, keys: Sequence[str]) -> list[dict[str, Any]]:
            return [
                dict(zip(keys, r))
                for r in self.conn.execute(sql, [namespace, revision_id]).fetchall()
            ]

        identifications = rows(
            "SELECT identification_id, group_index, kind, value, gtin_state, locator_json FROM "
            "product_safety_identifications WHERE namespace=? AND revision_id=? ORDER BY group_index, identification_id",
            ("identification_id", "group", "kind", "value", "gtin_state", "locator"),
        )
        hazards = rows(
            "SELECT hazard_id, hazard_type, risk_level, description, detail_json, locator_json FROM "
            "product_safety_hazards WHERE namespace=? AND revision_id=? ORDER BY hazard_id",
            (
                "hazard_id",
                "hazard_type",
                "risk_level",
                "description",
                "detail",
                "locator",
            ),
        )
        actions = rows(
            "SELECT action_id, text, measure_type, taken_by, detail_json, locator_json FROM "
            "product_safety_actions WHERE namespace=? AND revision_id=? ORDER BY action_id",
            ("action_id", "text", "measure_type", "taken_by", "detail", "locator"),
        )
        parties = rows(
            "SELECT party_id, role, name, party_key, locator_json FROM product_safety_parties "
            "WHERE namespace=? AND revision_id=? ORDER BY role, name",
            ("party_id", "role", "name", "party_key", "locator"),
        )
        followups = rows(
            "SELECT followup_id, followup_date, date_declared, country, country_declared, text, "
            "detail_json, locator_json FROM product_safety_followups WHERE namespace=? AND revision_id=? "
            "ORDER BY followup_date NULLS LAST, followup_id",
            (
                "followup_id",
                "date",
                "date_declared",
                "country",
                "country_declared",
                "text",
                "detail",
                "locator",
            ),
        )
        citations = rows(
            "SELECT citation_id, kind, raw, reference_key, identifiers_json, locator_json FROM "
            "product_safety_citations WHERE namespace=? AND revision_id=? ORDER BY kind, raw",
            ("citation_id", "kind", "raw", "reference_key", "identifiers", "locator"),
        )
        for collection in (
            identifications,
            hazards,
            actions,
            parties,
            followups,
            citations,
        ):
            for item in collection:
                for key in ("locator", "detail", "identifiers"):
                    if key in item:
                        item[key] = _load(item[key], {})
                if isinstance(item.get("date"), date):
                    item["date"] = item["date"].isoformat()
        for item in actions:
            # The authority's own words; this is the only corrective text a response ever carries.
            item["quoted"] = True
        for citation in citations:
            citation["links"] = [
                dict(
                    zip(
                        (
                            "target_kind",
                            "target_namespace",
                            "target_id",
                            "target_label",
                            "basis",
                            "identifier",
                            "linked_at_ms",
                        ),
                        r,
                    )
                )
                for r in self.conn.execute(
                    "SELECT target_kind, target_namespace, target_id, target_label, basis, identifier, linked_at_ms FROM "
                    "product_safety_citation_links WHERE namespace=? AND citation_id=? ORDER BY target_id",
                    [namespace, citation["citation_id"]],
                ).fetchall()
            ]
            citation["resolution"] = (
                "resolved"
                if citation["links"]
                else "unresolved (kept as the source string)"
            )
        for party in parties:
            link = self.party_link(namespace, party["party_key"])
            party["entity"] = link or {
                "status": "unmatched",
                "note": "kept as the source string",
            }
        return {
            "identifications": identifications,
            "hazards": hazards,
            "corrective_actions": actions,
            "parties": parties,
            "followups": followups,
            "citations": citations,
        }

    def inspect(
        self,
        namespace: str,
        notice: str,
        *,
        scopes: Iterable[str],
        as_of: str | None = None,
    ) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self._require_ready()
        notice_id = self.resolve_notice(namespace, notice)
        head = self._notice_row(namespace, notice_id)
        cutoff = _as_of(as_of)
        chosen, later = self.revision_as_of(namespace, notice_id, cutoff)
        revisions = self.revisions(namespace, notice_id)
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            **head,
            "as_of": None if cutoff is None else cutoff.isoformat(),
            "revision": None
            if chosen is None
            else {**chosen, **self.parts(namespace, chosen["revision_id"])},
            "status": "published" if chosen else "not yet published as of this date",
            "later_revisions": [
                {
                    "revision_id": r["revision_id"],
                    "revision_date": r["revision_date"],
                    "note": "published after as_of",
                }
                for r in later
            ],
            "revision_history": [
                {**r, "superseded_parts": self.parts(namespace, r["revision_id"])}
                for r in revisions
            ],
            "matches": self.matches_for_notice(namespace, notice_id),
            "boundary": BOUNDARY,
        }

    def selection_outcomes(self, namespace: str, run_id: str) -> list[dict[str, Any]]:
        self._require_ready()
        rows = self.conn.execute(
            "SELECT source_id, selection_index, selector_json, outcome, notices FROM product_safety_selection "
            "WHERE namespace=? AND run_id=? ORDER BY source_id, selection_index",
            [namespace, run_id],
        ).fetchall()
        return [
            {
                "source_id": r[0],
                "selection_index": r[1],
                "selector": _load(r[2], {}),
                "outcome": r[3],
                "notices": r[4],
            }
            for r in rows
        ]

    def sources_consulted(self, namespace: str) -> list[dict[str, Any]]:
        self._require_ready()
        rows = self.conn.execute(
            "SELECT source_id, max(finished_at_ms) FILTER (WHERE status='complete'), max(finished_at_ms), "
            "arg_max(status, finished_at_ms) FROM product_safety_source_runs WHERE namespace=? GROUP BY source_id "
            "ORDER BY source_id",
            [namespace],
        ).fetchall()
        return [
            {
                "source_id": r[0],
                "last_complete_run_at_ms": r[1],
                "last_run_at_ms": r[2],
                "last_run_status": r[3],
            }
            for r in rows
        ]

    # --------------------------------------------------------------- matching (R06)

    def _models(self, namespace: str) -> list[dict[str, Any]]:
        if not _table(self.conn, "product_identities"):
            return []
        models = {
            r[0]: {
                "model_id": r[0],
                "provider": r[1],
                "brand": r[2],
                "designation": r[3],
                "gtins": set(),
            }
            for r in self.conn.execute(
                "SELECT identity_id, provider, brand, designation FROM product_identities "
                "WHERE namespace=? AND level='model' ORDER BY identity_id",
                [namespace],
            ).fetchall()
        }
        for parent, identifiers in self.conn.execute(
            "SELECT parent_id, identifiers_json FROM product_identities WHERE namespace=? AND level='variant'",
            [namespace],
        ).fetchall():
            if parent in models:
                for item in _load(identifiers, {}).get("gtin") or []:
                    key = (
                        gtin_key(item.get("value"))
                        if item.get("state") == "valid"
                        else None
                    )
                    if key:
                        models[parent]["gtins"].add(key)
        return list(models.values())

    def _groups(self, namespace: str, revision_id: str) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT group_index, kind, value, value_key FROM product_safety_identifications WHERE namespace=? AND "
            "revision_id=? AND kind IN ('brand','model','gtin')",
            [namespace, revision_id],
        ).fetchall()
        groups: dict[int, dict[str, Any]] = {}
        shared: dict[str, dict[str, str]] = {"brand": {}, "model": {}, "gtin": {}}
        for group, kind, value, key in rows:
            if not key:
                continue
            target = (
                shared
                if group == -1
                else groups.setdefault(group, {"brand": {}, "model": {}, "gtin": {}})
            )
            target[kind][key] = value
        if not any(shared.values()):
            return groups
        if len(groups) == 1:
            # One affected product: notice-level identifiers (e.g. CPSC UPCs) describe it.
            (group,) = groups.values()
            for kind in group:
                group[kind] = {**shared[kind], **group[kind]}
        else:
            # Several products (or none): notice-level identifiers are not assigned to any one of them, so they
            # are matched on their own and never contradict a product's brand or model.
            groups[-1] = shared
        return groups

    def names_model(
        self,
        namespace: str,
        revision_id: str,
        model_id: str,
        models: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> bool:
        """Whether one notice revision's identification yields a ``proposed`` candidate for the model."""
        models = (
            models
            if models is not None
            else {m["model_id"]: m for m in self._models(namespace)}
        )
        model = models.get(model_id)
        if model is None:
            return False
        return any(
            (proposal := _propose(group, model)) is not None
            and proposal[0] == "proposed"
            for group in self._groups(namespace, revision_id).values()
        )

    def propose_matches(
        self, namespace: str, *, scopes: Iterable[str], principal_id: str
    ) -> dict[str, Any]:
        """Deterministic candidates between notice identifications and Products models; never auto-accepted."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        del principal_id
        models = self._models(namespace)
        now = self.now()
        candidates = []
        for notice_id, revision_id in self.conn.execute(
            "SELECT notice_id, revision_id FROM product_safety_current WHERE namespace=? ORDER BY notice_id",
            [namespace],
        ).fetchall():
            produced: set[str] = set()
            for group_index, group in sorted(
                self._groups(namespace, revision_id).items()
            ):
                for model in models:
                    proposal = _propose(group, model)
                    if proposal is None:
                        continue
                    state, basis, evidence, reasons = proposal
                    match_id = (
                        "product-notice-match:"
                        + digest(
                            [namespace, notice_id, group_index, model["model_id"]]
                        )[:24]
                    )
                    produced.add(match_id)
                    self._upsert_match(
                        namespace,
                        match_id,
                        notice_id,
                        group_index,
                        model["model_id"],
                        basis,
                        state,
                        evidence,
                        reasons,
                        revision_id,
                        now,
                    )
                    candidates.append(self.match(namespace, match_id))
            # A candidate the current revision no longer supports stays on record, detached.
            for (match_id,) in self.conn.execute(
                "SELECT match_id FROM product_safety_matches WHERE namespace=? AND notice_id=? AND "
                "candidate_state<>'not_named_in_current_revision' ORDER BY match_id",
                [namespace, notice_id],
            ).fetchall():
                if match_id not in produced:
                    self.conn.execute(
                        "UPDATE product_safety_matches SET candidate_state='not_named_in_current_revision', "
                        "revision_id=?, updated_at_ms=? WHERE namespace=? AND match_id=?",
                        [revision_id, now, namespace, match_id],
                    )
                    self._bump(namespace)
                    candidates.append(self.match(namespace, match_id))
        return {
            "contract": MATCH_CONTRACT,
            "namespace": namespace,
            "method": MATCH_METHOD,
            "candidates": candidates,
            "policy": "candidates only; a reviewer accepts each; siblings and regional variants are never attached",
        }

    def _upsert_match(
        self,
        namespace,
        match_id,
        notice_id,
        group_index,
        model_id,
        basis,
        state,
        evidence,
        reasons,
        revision_id,
        now,
    ) -> None:
        existing = self.conn.execute(
            "SELECT candidate_state, basis, evidence_json, reasons_json, revision_id FROM product_safety_matches "
            "WHERE namespace=? AND match_id=?",
            [namespace, match_id],
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO product_safety_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    match_id,
                    notice_id,
                    group_index,
                    model_id,
                    MATCH_METHOD,
                    basis,
                    state,
                    canonical(evidence),
                    canonical(reasons),
                    revision_id,
                    now,
                    now,
                ],
            )
            self._bump(namespace)
        elif (
            existing[0],
            existing[1],
            _load(existing[2], []),
            _load(existing[3], []),
            existing[4],
        ) != (state, basis, evidence, reasons, revision_id):
            self.conn.execute(
                "UPDATE product_safety_matches SET candidate_state=?, basis=?, evidence_json=?, reasons_json=?, "
                "revision_id=?, updated_at_ms=? WHERE namespace=? AND match_id=?",
                [
                    state,
                    basis,
                    canonical(evidence),
                    canonical(reasons),
                    revision_id,
                    now,
                    namespace,
                    match_id,
                ],
            )
            self._bump(namespace)

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT notice_id, group_index, model_id, method, basis, candidate_state, evidence_json, reasons_json, "
            "revision_id FROM product_safety_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id],
        ).fetchone()
        if row is None:
            raise ProductSafetyError(
                "not_found", "notice match is not visible in this namespace"
            )
        reviews = [
            dict(
                zip(
                    (
                        "sequence",
                        "decision",
                        "reason",
                        "candidate_state",
                        "revision_id",
                        "principal_id",
                        "reviewed_at_ms",
                    ),
                    r,
                )
            )
            for r in self.conn.execute(
                "SELECT sequence, decision, reason, candidate_state, revision_id, principal_id, reviewed_at_ms FROM "
                "product_safety_match_reviews WHERE namespace=? AND match_id=? ORDER BY sequence",
                [namespace, match_id],
            ).fetchall()
        ]
        review_state = reviews[-1]["decision"] if reviews else "unreviewed"
        attached = review_state == "accepted" and row[5] == "proposed"
        return {
            "contract": MATCH_CONTRACT,
            "match_id": match_id,
            "notice_id": row[0],
            "group": row[1],
            "model_id": row[2],
            "method": row[3],
            "basis": row[4],
            "candidate_state": row[5],
            "evidence": _load(row[6], []),
            "reasons": _load(row[7], []),
            "revision_id": row[8],
            "review_state": review_state,
            "review_history": reviews,
            "attached": attached,
            **(
                {"needs_re_review": True}
                if review_state == "accepted" and not attached
                else {}
            ),
        }

    def review_match(
        self,
        namespace: str,
        match_id: str,
        decision: str,
        reason: str,
        *,
        scopes: Iterable[str],
        principal_id: str,
    ) -> dict[str, Any]:
        """Append a review; a later review reverses an earlier one without deleting it."""
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in MATCH_DECISIONS:
            raise ProductSafetyError(
                "invalid_decision", "decision must be accepted, rejected or deferred"
            )
        if not str(reason or "").strip():
            raise ProductSafetyError("invalid_decision", "a review reason is required")
        current = self.match(namespace, match_id)
        if decision == "accepted" and current["candidate_state"] == "contradicted":
            raise ProductSafetyError(
                "contradicted_match",
                "the notice's GTIN contradicts the brand or model",
                reasons=current["reasons"],
            )
        if decision == "accepted" and current["candidate_state"] == "ambiguous":
            raise ProductSafetyError(
                "sibling_not_named",
                "the notice names a different designation; siblings and regional variants are "
                "never attached",
                reasons=current["reasons"],
            )
        if decision == "accepted" and current["candidate_state"] != "proposed":
            raise ProductSafetyError(
                "not_named", "the current notice revision no longer names this product"
            )
        sequence = len(current["review_history"]) + 1
        self.conn.execute(
            "INSERT INTO product_safety_match_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                "product-notice-match-review:"
                + digest([namespace, match_id, sequence])[:24],
                match_id,
                sequence,
                decision,
                reason.strip(),
                current["candidate_state"],
                current["revision_id"],
                principal_id,
                self.now(),
            ],
        )
        self._bump(namespace)
        return self.match(namespace, match_id)

    def matches_for_notice(
        self, namespace: str, notice_id: str
    ) -> list[dict[str, Any]]:
        self._require_ready()
        return [
            self.match(namespace, r[0])
            for r in self.conn.execute(
                "SELECT match_id FROM product_safety_matches WHERE namespace=? AND notice_id=? ORDER BY match_id",
                [namespace, notice_id],
            ).fetchall()
        ]

    def matches_for_models(
        self, namespace: str, model_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        self._require_ready()
        ids = sorted(set(model_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return [
            self.match(namespace, r[0])
            for r in self.conn.execute(
                f"SELECT match_id FROM product_safety_matches WHERE namespace=? AND model_id IN ({placeholders}) "
                "ORDER BY match_id",
                [namespace, *ids],
            ).fetchall()
        ]

    # --------------------------------------------------------------- parties (R06)

    def party_link(self, namespace: str, party_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT link_id, entity_id, decision_id, reviewer FROM product_safety_party_links WHERE namespace=? AND "
            "party_key=? AND status='active' AND decision='match' ORDER BY created_at_ms DESC LIMIT 1",
            [namespace, party_key],
        ).fetchone()
        return (
            None
            if row is None
            else {
                "status": "linked",
                "link_id": row[0],
                "entity_id": row[1],
                "decision_id": row[2],
                "reviewer": row[3],
            }
        )

    def propose_party_links(
        self, namespace: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Manufacturer/importer names with canonical-alias candidates; nothing is linked without a decision."""
        authorize(namespace, scopes, READ_SCOPE)
        self._require_ready()
        names = self.conn.execute(
            "SELECT DISTINCT p.party_key, p.name, p.role FROM product_safety_parties p JOIN product_safety_current c "
            "ON c.namespace=p.namespace AND c.revision_id=p.revision_id WHERE p.namespace=? ORDER BY p.name, p.role",
            [namespace],
        ).fetchall()
        resolver = None
        if _table(self.conn, "entity_aliases") and _table(
            self.conn, "canonical_entities"
        ):
            from src.kb.entities import resolve as resolver
        result = []
        for key, name, role in names:
            match = resolver(self.conn, name) if resolver else None
            result.append(
                {
                    "party_key": key,
                    "name": name,
                    "role": role,
                    "link": self.party_link(namespace, key),
                    "candidates": []
                    if not match
                    else [
                        {
                            "entity_id": match["canonical_id"],
                            "preferred_name": match["preferred_name"],
                            "basis": f"canonical alias match ({match['method']})",
                        }
                    ],
                }
            )
        return {
            "contract": PARTY_LINK_CONTRACT,
            "namespace": namespace,
            "parties": result,
            "policy": "proposals only; a reviewer decides each link through the entity identity decisions",
        }

    def decide_party_link(
        self,
        namespace: str,
        party_name: str,
        entity_id: str,
        decision: str,
        reason: str,
        *,
        scopes: Iterable[str],
        principal_id: str,
    ) -> dict[str, Any]:
        from src.kb.entity_history import EntityHistoryStore

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        require(scopes, ENTITY_WRITE_SCOPE, ENTITY_REVIEW_SCOPE)
        if decision not in {"match", "non-match"} or not str(reason or "").strip():
            raise ProductSafetyError(
                "invalid_decision", "decide match or non-match with a reason"
            )
        key = _party_key(party_name)
        known = self.conn.execute(
            "SELECT name FROM product_safety_parties WHERE namespace=? AND party_key=? LIMIT 1",
            [namespace, key],
        ).fetchone()
        if known is None:
            raise ProductSafetyError("not_found", "no notice names this party")
        if not str(entity_id or "").strip():
            raise ProductSafetyError("invalid_decision", "entity_id is required")
        if not self._entity_exists(entity_id):
            raise ProductSafetyError(
                "not_found",
                "entity_id is neither a canonical entity nor an acquired LEI record",
            )
        history = EntityHistoryStore(self.conn, now=self.now)
        history.register_entity(
            namespace, key, [known[0]], principal_id=principal_id, scopes=scopes
        )
        history.register_entity(
            namespace, entity_id, [], principal_id=principal_id, scopes=scopes
        )
        made = history.decide(
            namespace,
            decision,
            [key, entity_id],
            {
                "source": "products.safety",
                "party": known[0],
                "reason": reason.strip(),
                "policy": {"kind": "link-only", "merge": "never automatic"},
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=scopes,
            event_key=f"product-notice-party:{namespace}:{key}:{entity_id}",
        )
        link_id = (
            "product-notice-party-link:" + digest([namespace, key, entity_id])[:24]
        )
        self.conn.execute(
            "INSERT INTO product_safety_party_links VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT (namespace, link_id) "
            "DO UPDATE SET decision=excluded.decision, decision_id=excluded.decision_id, status='active', "
            "reason=excluded.reason, reviewer=excluded.reviewer, reverted_by=NULL",
            [
                namespace,
                link_id,
                key,
                known[0],
                entity_id,
                decision,
                made["decision_id"],
                "active",
                reason.strip(),
                principal_id,
                self.now(),
            ],
        )
        self._bump(namespace)
        return {
            "contract": PARTY_LINK_CONTRACT,
            "link_id": link_id,
            "party_key": key,
            "party": known[0],
            "entity_id": entity_id,
            "decision": decision,
            "decision_id": made["decision_id"],
            "merged": False,
        }

    def _entity_exists(self, entity_id: str) -> bool:
        """A caller-supplied entity reference must exist in its owner's store."""
        if entity_id.startswith("lei:"):
            return _table(self.conn, "lei_current") and bool(
                self.conn.execute(
                    "SELECT 1 FROM lei_current WHERE lei=? LIMIT 1", [entity_id[4:]]
                ).fetchone()
            )
        return _table(self.conn, "canonical_entities") and bool(
            self.conn.execute(
                "SELECT 1 FROM canonical_entities WHERE canonical_id=?", [entity_id]
            ).fetchone()
        )

    def revert_party_link(
        self, namespace: str, link_id: str, *, scopes: Iterable[str], principal_id: str
    ) -> dict:
        from src.kb.entity_history import EntityHistoryStore

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        require(scopes, ENTITY_WRITE_SCOPE, ENTITY_REVIEW_SCOPE, ENTITY_EXECUTE_SCOPE)
        row = self.conn.execute(
            "SELECT decision_id, status FROM product_safety_party_links WHERE namespace=? AND "
            "link_id=?",
            [namespace, link_id],
        ).fetchone()
        if not row or row[1] != "active":
            raise ProductSafetyError("not_found", "no active party link")
        undo = EntityHistoryStore(self.conn, now=self.now).undo(
            namespace,
            row[0],
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=scopes,
        )
        self.conn.execute(
            "UPDATE product_safety_party_links SET status='reverted', reverted_by=? WHERE namespace=? "
            "AND link_id=?",
            [undo["decision_id"], namespace, link_id],
        )
        self._bump(namespace)
        return {
            "link_id": link_id,
            "status": "reverted",
            "undo_decision_id": undo["decision_id"],
            "undoes": row[0],
        }

    # --------------------------------------------------------------- citations (R07)

    def link_citations(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        principal_id: str,
        standards_namespace: str | None = None,
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve cited standards and acts by exact reference or identifier; idempotent and append-only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        standards_namespace = standards_namespace or namespace
        legal_namespace = legal_namespace or namespace
        standards = legal = None
        if _table(self.conn, "standard_revisions"):
            authorize(standards_namespace, scopes, STANDARDS_READ_SCOPE)
            standards = {}
            for native_id, reference in self.conn.execute(
                "SELECT r.native_id, r.reference FROM standard_current c JOIN standard_revisions r "
                "ON r.revision_id=c.revision_id WHERE c.namespace=?",
                [standards_namespace],
            ).fetchall():
                standards.setdefault(standard_key(reference), []).append(
                    (native_id, reference)
                )
        if _table(self.conn, "legal_works"):
            from src.kb.legal import LegalError, LegalStore

            authorize(legal_namespace, scopes, LEGAL_READ_SCOPE)
            legal = LegalStore(self.conn, initialize=False)
        created, unresolved = [], []
        now = self.now()
        for citation_id, revision_id, kind, raw, key, identifiers in self.conn.execute(
            "SELECT citation_id, revision_id, kind, raw, reference_key, identifiers_json FROM "
            "product_safety_citations WHERE namespace=? ORDER BY citation_id",
            [namespace],
        ).fetchall():
            identifiers = _load(identifiers, {})
            targets: list[tuple[str, str, str, str, str]] = []
            if kind == "standard" and standards is not None:
                hits = standards.get(key) or []
                if len(hits) == 1:
                    targets.append(
                        (
                            "standard",
                            standards_namespace,
                            f"standard:iso:{hits[0][0]}",
                            hits[0][1],
                            hits[0][1],
                        )
                    )
            elif kind == "legal" and legal is not None:
                for label in ("celex", "eli"):
                    value = identifiers.get(label)
                    if not value:
                        continue
                    try:
                        found = legal.lookup(
                            legal_namespace, scopes=scopes, identifier=value
                        )
                    except LegalError:
                        continue
                    if found["status"] == "found":
                        work = found["works"][0]
                        targets.append(
                            (
                                "legal-work",
                                legal_namespace,
                                work["work_id"],
                                work.get("title"),
                                value,
                            )
                        )
                        break
            if not targets:
                unresolved.append(
                    {"citation_id": citation_id, "raw": raw, "kind": kind}
                )
            for target_kind, target_namespace, target_id, label, identifier in targets:
                link_id = (
                    "product-safety-citation-link:"
                    + digest([namespace, citation_id, target_id])[:24]
                )
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO product_safety_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "RETURNING link_id",
                    [
                        namespace,
                        link_id,
                        citation_id,
                        revision_id,
                        target_kind,
                        target_namespace,
                        target_id,
                        label,
                        "cited",
                        identifier,
                        principal_id,
                        now,
                    ],
                ).fetchall()
                if inserted:
                    created.append(
                        {
                            "link_id": link_id,
                            "citation_id": citation_id,
                            "revision_id": revision_id,
                            "raw": raw,
                            "target_kind": target_kind,
                            "target_id": target_id,
                            "basis": "cited",
                        }
                    )
        return {
            "namespace": namespace,
            "linked": created,
            "unresolved": unresolved,
            "stores": {"standards": standards is not None, "legal": legal is not None},
            "policy": "exact reference (standards) or exact CELEX/ELI (legal works) only; no topic, category or "
            "hazard similarity, and certificate-product links are never notice evidence",
        }

    # --------------------------------------------------------------- queries (R08)

    def _entry(
        self, namespace: str, notice_id: str, cutoff: date | None
    ) -> dict[str, Any] | None:
        head = self._notice_row(namespace, notice_id)
        chosen, later = self.revision_as_of(namespace, notice_id, cutoff)
        if chosen is None:
            return None
        parts = self.parts(namespace, chosen["revision_id"])
        followups = [
            f
            for f in parts["followups"]
            if cutoff is None or _date_rank(f["date"]) <= cutoff
        ]
        later_followups = [
            f
            for f in parts["followups"]
            if cutoff is not None and _date_rank(f["date"]) > cutoff
        ]
        return {
            **head,
            "issuing_authority": {
                "value": chosen["authority"],
                "declared": chosen["authority_declared"],
            },
            "notifying_country": {
                "code": chosen["notifying_country"],
                "declared": chosen["notifying_country_declared"],
            },
            "notice_type": {
                "value": chosen["notice_type"],
                "declared": chosen["notice_type_declared"],
            },
            "revision": {
                k: chosen[k]
                for k in (
                    "revision_id",
                    "revision_no",
                    "revision_date",
                    "revision_declared",
                    "order_basis",
                    "published",
                    "updated",
                    "source_id",
                    "document_id",
                    "payload_pointer",
                )
            },
            "revision_count": len(self.revisions(namespace, notice_id)),
            "later_revisions": [
                {
                    "revision_id": r["revision_id"],
                    "revision_date": r["revision_date"],
                    "note": "published after as_of; excluded",
                }
                for r in later
            ],
            "hazards": parts["hazards"],
            "affected_identification": parts["identifications"],
            "corrective_actions": [
                {
                    "quoted_text": a["text"],
                    "measure_type": a["measure_type"],
                    "taken_by": a["taken_by"],
                    "detail": a["detail"],
                    "locator": a["locator"],
                    "attribution": chosen["authority_declared"],
                }
                for a in parts["corrective_actions"]
            ],
            "parties": parts["parties"],
            "followups": followups,
            "later_followups": [
                {**f, "note": "dated after as_of; excluded"} for f in later_followups
            ],
            "cited_standards": [
                c for c in parts["citations"] if c["kind"] == "standard"
            ],
            "cited_legal_acts": [c for c in parts["citations"] if c["kind"] == "legal"],
        }

    def _news(self, notice_number: str) -> dict[str, Any]:
        if not _table(self.conn, "documents"):
            return {"articles": [], "note": "no documents"}
        pattern = token_sql_pattern(notice_number)
        rows = self.conn.execute(
            "SELECT document_id, title, url, created_at FROM documents WHERE source_type='news' AND "
            "(regexp_matches(coalesce(title,''), ?) OR regexp_matches(coalesce(content,''), ?)) "
            "ORDER BY created_at DESC NULLS LAST, document_id LIMIT 5",
            [pattern, pattern],
        ).fetchall()
        return {
            "articles": [
                {"document_id": r[0], "title": r[1], "url": r[2], "created_at": r[3]}
                for r in rows
            ],
            "label": "reporting that names the notice number; not the notice",
        }

    def lookup(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        model_id: str | None = None,
        variant_id: str | None = None,
        brand: str | None = None,
        designation: str | None = None,
        gtin: str | None = None,
        as_of: str | None = None,
        include_news: bool = False,
    ) -> dict[str, Any]:
        """Notices naming a product, brand, model or GTIN as of a date, each cited to its notice revision."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self._require_ready()
        if include_news:
            require(scopes, NEWS_SCOPE)
        cutoff = _as_of(as_of)
        if not any((model_id, variant_id, brand, designation, gtin)):
            raise ProductSafetyError(
                "invalid_request",
                "name a Products model or variant id, or a brand, designation "
                "or GTIN string",
            )
        if (model_id or variant_id) and (brand or designation or gtin):
            raise ProductSafetyError(
                "invalid_request",
                "ask by a Products id (reviewed matches) or by strings, not both",
            )
        entries: dict[str, dict[str, Any]] = {}
        later_notices: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        query: dict[str, Any] = {
            "as_of": None if cutoff is None else cutoff.isoformat()
        }
        if model_id or variant_id:
            members = self._product_members(namespace, model_id, variant_id)
            models = {m["model_id"]: m for m in self._models(namespace)}
            query.update({"model_id": members[0], "accepted_equivalents": members[1:]})
            for match in self.matches_for_models(namespace, members):
                if not match["attached"]:
                    if (
                        match["review_state"] != "rejected"
                        and match["candidate_state"] != "not_named_in_current_revision"
                    ):
                        pending.append(
                            {
                                k: match[k]
                                for k in (
                                    "match_id",
                                    "notice_id",
                                    "model_id",
                                    "candidate_state",
                                    "review_state",
                                    "reasons",
                                )
                            }
                        )
                    continue
                if cutoff is not None:
                    # The as-of revision must itself name the product; a later revision naming it is not
                    # evidence that a notice was on record then.
                    chosen, _ = self.revision_as_of(
                        namespace, match["notice_id"], cutoff
                    )
                    if chosen is not None and not self.names_model(
                        namespace, chosen["revision_id"], match["model_id"], models
                    ):
                        later_notices.append(
                            {
                                "notice_id": match["notice_id"],
                                "match_id": match["match_id"],
                                "note": "the notice named this product only in a later revision",
                            }
                        )
                        continue
                self._collect(
                    namespace,
                    match["notice_id"],
                    cutoff,
                    entries,
                    later_notices,
                    {
                        "kind": "reviewed-product-match",
                        "match_id": match["match_id"],
                        "model_id": match["model_id"],
                        "basis": match["basis"],
                        "evidence": match["evidence"],
                        "decision": match["review_history"][-1],
                    },
                )
        else:
            wanted = {
                "brand": _brand_key(brand) if brand else None,
                "model": designation_key(designation) if designation else None,
                "gtin": (gtin_key(gtin) or f"raw:{str(gtin).strip()}")
                if gtin
                else None,
            }
            query.update({"brand": brand, "designation": designation, "gtin": gtin})
            for (notice_id,) in self.conn.execute(
                "SELECT notice_id FROM product_safety_notices WHERE namespace=? ORDER BY provider, notice_number",
                [namespace],
            ).fetchall():
                chosen, _ = self.revision_as_of(namespace, notice_id, cutoff)
                if chosen is None:
                    first = self.revisions(namespace, notice_id)
                    if first and _string_hit(
                        self._groups(namespace, first[0]["revision_id"]), wanted
                    ):
                        later_notices.append(
                            {
                                "notice_id": notice_id,
                                "first_revision_date": first[0]["revision_date"],
                                "note": "first published after as_of",
                            }
                        )
                    continue
                # Select the revision current as of the date first, then filter its identifications.
                hit = _string_hit(
                    self._groups(namespace, chosen["revision_id"]), wanted
                )
                if hit is not None:
                    self._collect(
                        namespace,
                        notice_id,
                        cutoff,
                        entries,
                        later_notices,
                        {
                            "kind": "identification-string",
                            "matched": hit,
                            "note": "the notice names these strings; no product identity is implied",
                            "product_matches": [
                                {
                                    k: m[k]
                                    for k in (
                                        "match_id",
                                        "model_id",
                                        "candidate_state",
                                        "review_state",
                                        "attached",
                                    )
                                }
                                for m in self.matches_for_notice(namespace, notice_id)
                            ],
                        },
                    )
        notices = sorted(
            entries.values(), key=lambda e: (e["provider"], e["notice_number"])
        )
        if include_news:
            for entry in notices:
                entry["news"] = self._news(entry["notice_number"])
        return {
            "contract": DOSSIER_CONTRACT,
            "namespace": namespace,
            "query": query,
            "status": "notices on record" if notices else NO_NOTICE,
            "notices": notices,
            "authorities": sorted({n["issuing_authority"]["value"] for n in notices}),
            "side_by_side": "each notice is shown with its own revision; notices are never merged",
            "later_notices": later_notices,
            "unreviewed_candidates": pending,
            "sources_consulted": self.sources_consulted(namespace),
            "boundary": BOUNDARY,
        }

    def _collect(
        self, namespace, notice_id, cutoff, entries, later_notices, connection
    ) -> None:
        entry = entries.get(notice_id)
        if entry is None:
            built = self._entry(namespace, notice_id, cutoff)
            if built is None:
                later_notices.append(
                    {"notice_id": notice_id, "note": "first published after as_of"}
                )
                return
            entry = entries[notice_id] = {**built, "connections": []}
        entry["connections"].append(connection)

    def _product_members(
        self, namespace: str, model_id: str | None, variant_id: str | None
    ) -> list[str]:
        return self.equivalent_models(namespace, model_id or variant_id)

    def equivalent_models(self, namespace: str, identity: str) -> list[str]:
        """A model (or a variant's model) plus the models accepted as the same product from other providers.

        The one definition of "models equivalent to X", shared by lookups and monitors.
        """
        if not _table(self.conn, "product_identities"):
            raise ProductSafetyError(
                "not_found", "no Products identities in this deployment"
            )
        row = self.conn.execute(
            "SELECT level, parent_id FROM product_identities WHERE namespace=? AND identity_id=?",
            [namespace, identity],
        ).fetchone()
        if row is None:
            raise ProductSafetyError(
                "not_found", "product identity is not visible in this namespace"
            )
        if row[0] == "variant":
            model = row[1]
        elif row[0] == "model":
            model = identity
        else:
            raise ProductSafetyError(
                "unresolved_identity", "name a model or variant, not a family"
            )
        from src.kb.products import ProductStore

        equivalents = sorted(
            ProductStore(self.conn, initialize=False).accepted_equivalents(
                namespace, model
            )
        )
        return [model, *equivalents]


def _as_of(value: Any) -> date | None:
    if value in (None, ""):
        return None
    parsed = iso_date(value)
    if parsed is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)[:10]):
        raise ProductSafetyError("invalid_request", "as_of is an ISO date (YYYY-MM-DD)")
    return parsed


def _string_hit(
    groups: Mapping[int, Mapping[str, Mapping[str, str]]],
    wanted: Mapping[str, str | None],
):
    """The identification strings of one group that match every requested key (brand, model, GTIN together)."""
    for group_index, group in sorted(groups.items()):
        hit = {}
        for kind, key in wanted.items():
            if key is None:
                continue
            if key not in group.get(kind, {}):
                break
            hit[kind] = group[kind][key]
        else:
            if hit:
                return {"group": group_index, **hit}
    return None


def _propose(group: Mapping[str, Mapping[str, str]], model: Mapping[str, Any]):
    """(state, basis, evidence, reasons) for one identification group against one Products model, or None."""
    brands, models = group["brand"], group["model"]
    gtins = {
        k: v for k, v in group["gtin"].items() if not k.startswith("raw:")
    }  # only valid GTINs can match
    m_brand = _brand_key(model["brand"])
    m_designation = (
        designation_key(model["designation"]) if model["designation"] else ""
    )
    shared_gtins = sorted(set(gtins) & model["gtins"])
    brand_equal = bool(m_brand) and m_brand in brands
    designation_equal = bool(m_designation) and m_designation in models
    evidence: list[dict[str, Any]] = []
    reasons: list[str] = []
    if shared_gtins:
        evidence.append(
            {
                "kind": "gtin",
                "notice": [gtins[k] for k in shared_gtins],
                "product_gtin_keys": shared_gtins,
            }
        )
        if brands and not brand_equal:
            reasons.append(
                f"the notice's brand {sorted(brands.values())} differs from the product's {model['brand']!r}"
            )
        if models and not designation_equal:
            reasons.append(
                f"the notice's model {sorted(models.values())} differs from {model['designation']!r}"
            )
        if brand_equal:
            evidence.append(
                {"kind": "brand", "notice": brands[m_brand], "product": model["brand"]}
            )
        if designation_equal:
            evidence.append(
                {
                    "kind": "designation",
                    "notice": models[m_designation],
                    "product": model["designation"],
                }
            )
        return ("contradicted" if reasons else "proposed", "gtin", evidence, reasons)
    if brand_equal and designation_equal:
        evidence += [
            {"kind": "brand", "notice": brands[m_brand], "product": model["brand"]},
            {
                "kind": "designation",
                "notice": models[m_designation],
                "product": model["designation"],
            },
        ]
        if gtins and model["gtins"]:
            reasons.append("the notice's GTINs and the product's GTINs are disjoint")
        return (
            "contradicted" if reasons else "proposed",
            "brand+designation",
            evidence,
            reasons,
        )
    if brand_equal and m_designation:
        for key, value in models.items():
            prefix = len(_common_prefix(key, m_designation))
            if prefix >= 6 and prefix >= max(len(key), len(m_designation)) - 4:
                evidence.append(
                    {
                        "kind": "brand",
                        "notice": brands[m_brand],
                        "product": model["brand"],
                    }
                )
                reasons.append(
                    f"the notice names {value!r}, not {model['designation']!r}: a sibling, size or regional "
                    "variant is never attached"
                )
                return ("ambiguous", "brand+designation-suffix", evidence, reasons)
    return None


class ProductSafetyProjector:
    """Source-pack runtime projector for ``noesis-product-safety-notice-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = ProductSafetyStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("product_safety") or {}).get("namespace")
            or DEFAULT_NAMESPACE
        )

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
        document_ids = {
            str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(
                item["document_id"]
            )
            for item in documents
        }
        return self.store.observe_page(
            run_id,
            source,
            self._namespace(source),
            records,
            documents=document_ids,
            page_receipt=page_receipt,
        )

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        return self.store.finish_source(
            run_id, source["source_id"], self._namespace(source), status
        )


def safety_readiness(conn: Any, *, pack_id: str = SOURCE_PACK) -> dict[str, Any]:
    """Per notice provider: installed, enabled, licence accepted, last run; the access decision from R01."""
    from src.ingestion.product_sources import SAFETY_PROVIDER_CONTRACTS
    from src.ingestion.source_packs import _digest as source_digest

    try:
        row = conn.execute(
            "SELECT c.enabled, v.manifest_json FROM source_pack_current c JOIN source_pack_versions v "
            "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?",
            [pack_id],
        ).fetchone()
    except Exception:  # noqa: BLE001 - runtime tables absent until first install
        row = None
    manifest = _load(row[1], {}) if row else {}
    by_connector = {
        s["connector"]: s
        for s in manifest.get("sources") or []
        if s["connector"] in SAFETY_CONNECTORS
    }
    store_ready = _table(conn, "product_safety_revisions")
    providers = {}
    for provider, contract in SAFETY_PROVIDER_CONTRACTS.items():
        source = by_connector.get(provider)
        blockers: list[dict[str, Any]] = []
        if contract["status"] == "not-implemented":
            providers[provider] = {
                "source_id": None,
                "live_verification": "not-implemented",
                "reason": contract["reason"],
                "fixture": "n/a",
                "live": "not-implemented",
            }
            continue
        if source is None:
            blockers.append(
                {
                    "code": "pack_not_installed",
                    "severity": "blocking",
                    "action": f"install config/source_packs/products.json ({pack_id} 1.1.0 or later)",
                }
            )
        elif not (row and row[0]):
            blockers.append({"code": "pack_disabled", "severity": "blocking"})
        last = None
        if source is not None:
            policy = source["license"]
            terms_hash = source_digest(
                {
                    "terms_url": policy["terms_url"],
                    "redistribution": policy["redistribution"],
                }
            )
            try:
                accepted = bool(
                    conn.execute(
                        "SELECT 1 FROM source_pack_license_acceptance WHERE pack_id=? AND source_id=? AND license_id=? "
                        "AND terms_hash=?",
                        [pack_id, source["source_id"], policy["id"], terms_hash],
                    ).fetchone()
                )
            except Exception:  # noqa: BLE001 - runtime not yet initialized
                accepted = False
            if not accepted:
                blockers.append(
                    {
                        "code": "license_not_accepted",
                        "source_id": source["source_id"],
                        "severity": "blocking-live",
                    }
                )
            if store_ready:
                last = conn.execute(
                    "SELECT status, finished_at_ms FROM product_safety_source_runs WHERE source_id=? "
                    "ORDER BY finished_at_ms DESC LIMIT 1",
                    [source["source_id"]],
                ).fetchone()
        fixture_ready = source is not None and bool(row and row[0])
        providers[provider] = {
            "source_id": None if source is None else source["source_id"],
            "fixture": "ready" if fixture_ready else "blocked",
            "live": "ready" if fixture_ready and not blockers else "blocked",
            "live_verification": contract["status"],
            "last_run": None
            if last is None
            else {"status": last[0], "finished_at_ms": last[1]},
            "blockers": blockers,
        }
    return {
        "pack_id": pack_id,
        "store_ready": store_ready,
        "providers": providers,
        "notice": "Notice-source readiness is per provider; one working source never implies another's coverage.",
    }


__all__ = [
    "BOUNDARY",
    "CONTRACT",
    "DOSSIER_CONTRACT",
    "MATCH_CONTRACT",
    "NO_NOTICE",
    "ProductSafetyError",
    "ProductSafetyProjector",
    "ProductSafetyStore",
    "extract_citations",
    "feature_enabled",
    "gtin_key",
    "safety_readiness",
    "standard_key",
    "token_regex",
    "token_sql_pattern",
]

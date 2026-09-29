"""International trade flows, release vintages and classification concordances: the ``economics.trade`` owner.

Records (contract ``noesis-trade-flow-record-v1``, #2210 TF02) follow the Economics series-store pattern of
:mod:`src.kb.demographics` (release, series, vintage, observation; release and retrieval clocks with basis labels;
``revision_of`` by release clock). The generic ``economic_vintages`` store keys a series by one geography and has
no place for a partner, a product code in a classification vintage, a customs basis or a reporter-versus-mirror
role, so trade flows get their own namespace-scoped ``trade_*`` tables in the same shape:

* **release** - one acquired publication (a Comtrade reporter report, a Comext cube, a concordance file or an
  operator-imported correlation table) with the publication's own release clock (Comtrade ``lastReleased``,
  Comext ``updated``, a declared date or the retrieval time) and its basis label, digests and structure.
  Re-acquiring an unchanged publication adds nothing.
* **series** - keyed by provider, reporter, partner, flow direction, product code, classification scheme and
  vintage (HS edition, CN year, SITC revision), frequency, valuation basis (CIF/FOB, reported or the documented
  convention), role (``reporter`` - the pair reporter's own report - or ``mirror`` - the partner's report of the
  same pair), measure and unit. Reporter and mirror figures are always different series.
* **vintage** - each release of a series is appended; a revised value never overwrites an earlier vintage, and a
  publication that changes values without a new release clock is refused (``vintage_conflict``).
* **observation** - period, value text and parsed value, status (``reported``, ``confidential``,
  ``not_published``), quantities with units and estimation flags, and every provider flag verbatim.
* **concordance** and **concordance row** - code pairs between two classifications or vintages with the mapping
  type as published (or derived from the table's pairs, labelled) and weights only when published; a changed table
  is a new concordance revision.
* **code label** - a product code's label per classification vintage and release.
* **comparability note** - a reviewable typed note between two series, following
  :mod:`src.kb.demographics_comparability` (proposed, accepted or rejected, reverted; never merges series).

Nothing here estimates a missing or suppressed flow, nowcasts, reconciles reporter and mirror figures, allocates
flows between areas or infers sanctions evasion.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.ingestion.trade_sources import (
    EXCLUSIONS,
    FORMATS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    SCHEMES,
    mapping_types,
)

CONTRACT = "noesis-trade-flow-record-v1"
ANSWER_CONTRACT = "noesis-trade-flow-answer-v1"
COMPARABILITY_CONTRACT = "noesis-trade-comparability-v1"
READ_SCOPE = "knowledge:trade:read"
WRITE_SCOPE = "knowledge:trade:write"
REVIEW_SCOPE = "knowledge:trade:review"
DEFAULT_NAMESPACE = "global"
FEATURES = ("trade-comtrade", "trade-comext")
FEATURE_PROVIDERS = {"trade-comtrade": "un-comtrade", "trade-comext": "eurostat-comext"}
RECORD_TYPES = (
    "release",
    "series",
    "vintage",
    "observation",
    "concordance",
    "concordance_row",
    "code_label",
    "comparability_note",
)
STATUSES = ("reported", "confidential", "not_published")
RELATIONS = (
    "different_valuation_basis",
    "different_classification_vintage",
    "different_reporting_period",
    "different_partner_attribution",
    "not_comparable",
)
ACTIVE_STATES = ("proposed", "accepted")
# Keys that would carry an estimated, imputed, nowcast or reconciled number or an evasion or compliance claim.
FORBIDDEN_KEYS = frozenset(
    {
        "estimated_value",
        "estimate",
        "imputed",
        "imputed_value",
        "interpolated",
        "gap_filled",
        "nowcast",
        "nowcasted",
        "forecast",
        "reconciled",
        "reconciled_value",
        "combined_value",
        "evasion",
        "evasion_risk",
        "sanctions_evasion",
        "compliance",
        "compliance_status",
        "violation",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS trade_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  kind TEXT NOT NULL, document_json TEXT NOT NULL, published_on TEXT, published_at TEXT, release_basis TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_label TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  item_count INTEGER NOT NULL, structure_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT,
  sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS trade_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, reporter_scheme TEXT NOT NULL,
  reporter_code TEXT NOT NULL, reporter_json TEXT NOT NULL, partner_scheme TEXT NOT NULL, partner_code TEXT NOT NULL,
  partner_json TEXT NOT NULL, flow_code TEXT NOT NULL, flow_direction TEXT NOT NULL, flow_label TEXT,
  product_code TEXT NOT NULL, product_label TEXT, classification_scheme TEXT NOT NULL,
  classification_vintage TEXT NOT NULL, classification_code TEXT, frequency TEXT NOT NULL,
  valuation_basis TEXT NOT NULL, valuation_source TEXT NOT NULL, role TEXT NOT NULL, pair_json TEXT NOT NULL,
  measure TEXT NOT NULL, unit_json TEXT NOT NULL, dimensions_json TEXT NOT NULL, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS trade_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_at_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  content_hash TEXT NOT NULL, sequence INTEGER NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS trade_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, flags_json TEXT NOT NULL, quantities_json TEXT NOT NULL, extra_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS trade_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS trade_concordances (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, concordance_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, label TEXT NOT NULL, source_scheme TEXT NOT NULL, source_vintage TEXT NOT NULL,
  target_scheme TEXT NOT NULL, target_vintage TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, row_count INTEGER NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, concordance_id)
);
CREATE TABLE IF NOT EXISTS trade_concordance_rows (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, source_code TEXT NOT NULL, target_code TEXT NOT NULL,
  mapping_type TEXT NOT NULL, mapping_basis TEXT NOT NULL, weight TEXT, weight_state TEXT NOT NULL,
  detail_json TEXT NOT NULL, PRIMARY KEY(namespace, concordance_id, source_code, target_code)
);
CREATE TABLE IF NOT EXISTS trade_code_labels (
  namespace TEXT NOT NULL, scheme TEXT NOT NULL, vintage TEXT NOT NULL, code TEXT NOT NULL, label TEXT NOT NULL,
  release_id TEXT NOT NULL, PRIMARY KEY(namespace, scheme, vintage, code, release_id)
);
CREATE TABLE IF NOT EXISTS trade_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_id TEXT NOT NULL,
  right_id TEXT NOT NULL, relation TEXT NOT NULL, statement TEXT NOT NULL, cited_json TEXT NOT NULL,
  state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, note_id)
);
"""


class TradeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


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
        raise TradeError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise TradeError("unauthorized", f"{required} is required for this part of the answer")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry an estimate, a reconciled number or an evasion claim."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def release_ms(published_on: str | None, published_at: str | None, retrieved_ms: int) -> int:
    """The release clock in epoch milliseconds (UTC); a date alone is its midnight; no date is the retrieval time."""
    if published_at:
        stamp = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    if published_on:
        return int(
            datetime.combine(date.fromisoformat(published_on), datetime.min.time(), tzinfo=timezone.utc).timestamp()
            * 1000
        )
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='economics'").fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get("economics") or [])


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether the Economics bundle's optional trade feature (``trade-comtrade``, ``trade-comext`` or either) is
    selected in the active composition plan."""
    selected = _selected_features(conn)
    return feature in selected if feature else any(f in selected for f in FEATURES)


def _check_item(item: Mapping[str, Any], kind: str) -> None:
    if forbidden_keys(dict(item)):
        raise TradeError("invalid_release", "published records carry no estimate, reconciled value or evasion claim")
    if kind == "concordance":
        body = dict(item.get("concordance") or {})
        for side in ("source", "target"):
            if dict(body.get(side) or {}).get("scheme") not in SCHEMES or not dict(body.get(side) or {}).get("vintage"):
                raise TradeError("invalid_release", "a concordance states its source and target classification")
        for row in body.get("rows") or []:
            if row.get("mapping_type") not in {"1:1", "1:n", "n:1", "n:n"}:
                raise TradeError("invalid_release", "each concordance row states its mapping type")
        if not body.get("rows"):
            raise TradeError("invalid_release", "a concordance states at least one code pair")
        return
    for key in ("reporter", "partner", "flow", "product", "classification", "valuation", "pair", "unit"):
        if not item.get(key):
            raise TradeError("invalid_release", f"a trade series states its {key}")
    if item.get("role") not in ("reporter", "mirror"):
        raise TradeError("invalid_release", "a trade series states whether it is the reporter or the mirror report")
    if dict(item["classification"]).get("scheme") not in SCHEMES:
        raise TradeError("invalid_release", "unknown classification scheme")
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise TradeError("invalid_release", "each observation states its status")
        if obs.get("status") != "reported" and obs.get("value") is not None:
            raise TradeError("invalid_release", "a confidential or unpublished cell carries no value")


class TradeFlowStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "trade_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != provider:
            raise TradeError("invalid_release", "release names a known provider and format")
        document = dict(header.get("document") or {})
        release_id = "tf-release:" + digest([namespace, provider, source_id, header["file_sha256"], document])[:24]
        if self.conn.execute(
            "SELECT 1 FROM trade_releases WHERE namespace=? AND release_id=?", [namespace, release_id]
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM trade_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        basis = str(header.get("release_basis") or "retrieval_time")
        self.conn.execute(
            "INSERT INTO trade_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                release_id,
                provider,
                source_id,
                fmt,
                FORMATS[fmt]["kind"],
                canonical(document),
                header.get("published_on"),
                header.get("published_at"),
                basis,
                release_ms(header.get("published_on"), header.get("published_at"), retrieved),
                header.get("release_label"),
                header["file_sha256"],
                header.get("content_sha256") or "",
                int(header.get("item_count") or 0),
                canonical(header.get("structure") or {}),
                origin,
                header.get("url"),
                int(sequence) + 1,
                run_id,
                recorded_by,
                retrieved,
            ],
        )
        return release_id, True

    def apply_release(
        self,
        namespace: str,
        header: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
        recorded_by: str | None = None,
    ) -> dict[str, Any]:
        """Record one publication: a vintage per stated series, or a concordance revision; idempotent by file."""
        if int(header.get("item_count", -1)) != len(items):
            raise TradeError("incomplete_release", "a release carries every item it states")
        kind = FORMATS.get(str(header.get("format")), {}).get("kind")
        for item in items:
            _check_item(item, kind)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {"series": 0, "vintages": 0, "unchanged_vintages": 0, "concordances": 0, "labels": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace, header, source_id=source_id, run_id=run_id, retrieved=retrieved, recorded_by=recorded_by
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
            basis = str(header.get("release_basis") or "retrieval_time")
            vintage_ids, concordance_ids, seen = [], [], set()
            for item in items:
                if kind == "concordance":
                    concordance_id, new = self._concordance(namespace, header["provider"], item, release_id, clock)
                    concordance_ids.append(concordance_id)
                    counts["concordances"] += int(new)
                    continue
                series_id, new_series = self._series(namespace, header["provider"], item, release_id)
                if series_id in seen:
                    raise TradeError("invalid_release", "a release states the same series twice")
                seen.add(series_id)
                counts["series"] += int(new_series)
                vintage_id, new_vintage = self._vintage(namespace, series_id, item, release_id, clock, basis, retrieved)
                vintage_ids.append(vintage_id)
                self.conn.execute(
                    "INSERT INTO trade_release_members VALUES (?,?,?,?)", [namespace, release_id, series_id, vintage_id]
                )
                counts["vintages" if new_vintage else "unchanged_vintages"] += 1
                classification = dict(item["classification"])
                counts["labels"] += self._label(
                    namespace, classification["scheme"], classification["vintage"], item["product"], release_id
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": header.get("published_on"),
            "release_basis": basis,
            "vintage_ids": vintage_ids,
            "concordance_ids": concordance_ids,
            **counts,
        }

    def import_concordance(
        self,
        namespace: str,
        table: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an operator-imported correlation table (UNSD workbooks) with its citation; mapping types as
        published, weights only when published; idempotent by table content."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table = dict(table)
        citation = dict(table.get("citation") or {})
        if not citation.get("url") or not citation.get("published_on") or not citation.get("file_sha256"):
            raise TradeError("invalid_table", "an imported table cites its URL, publication date and file digest")
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("source_code") or not row.get("target_code"):
                raise TradeError("invalid_table", "each row names a source and a target code")
            if row.get("mapping_type") not in {"1:1", "1:n", "n:1", "n:n"}:
                raise TradeError("invalid_table", "an imported table states each row's relationship as published")
            weight = row.pop("weight", None)
            rows.append(
                {
                    **{k: row.get(k) for k in ("source_code", "target_code", "source_label", "target_label", "note")},
                    "row": row.get("row"),
                    "mapping_type": row["mapping_type"],
                    "mapping_basis": "published",
                    **({"weight": str(weight), "weight_state": "published"} if weight is not None else {"weight_state": "not-published"}),
                }
            )
        item = {
            "concordance": {
                "label": str(table.get("label") or ""),
                "source": dict(table.get("source") or {}),
                "target": dict(table.get("target") or {}),
                "member": citation.get("sheet"),
                "rows": sorted(rows, key=lambda r: (r["source_code"], r["target_code"])),
            }
        }
        if not item["concordance"]["label"]:
            raise TradeError("invalid_table", "an imported table has a label")
        header = {
            "provider": "unsd-classifications",
            "format": "operator-concordance",
            "document": {"label": item["concordance"]["label"], "citation": citation},
            "published_on": str(citation["published_on"])[:10],
            "published_at": None,
            "release_basis": "declared_release",
            "file_sha256": str(citation["file_sha256"]),
            "content_sha256": digest(item),
            "item_count": 1,
            "structure": {"sheet": citation.get("sheet"), "rows": len(rows)},
            "evidence_origin": "operator",
            "url": citation["url"],
        }
        return self.apply_release(
            namespace,
            header,
            [item],
            run_id=run_id or f"operator:{principal_id}",
            source_id="operator-import:unsd-classifications",
            recorded_by=principal_id,
        )

    @staticmethod
    def series_key(provider: str, item: Mapping[str, Any]) -> list[Any]:
        return [
            provider,
            [item["reporter"]["scheme"], str(item["reporter"]["code"])],
            [item["partner"]["scheme"], str(item["partner"]["code"])],
            str(item["flow"]["code"]),
            str(item["product"]["code"]),
            [item["classification"]["scheme"], item["classification"]["vintage"], item["classification"].get("code")],
            item.get("frequency"),
            [item["valuation"]["basis"], item["valuation"]["source"]],
            item["role"],
            dict(item["pair"]),
            item.get("measure") or "trade_value",
            dict(item["unit"]),
            dict(item.get("dimensions") or {}),
        ]

    def _series(self, namespace, provider, item, release_id):
        series_id = "tf-series:" + digest([namespace, *self.series_key(provider, item)])[:24]
        if self.conn.execute(
            "SELECT 1 FROM trade_series WHERE namespace=? AND series_id=?", [namespace, series_id]
        ).fetchone():
            return series_id, False
        self.conn.execute(
            "INSERT INTO trade_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                series_id,
                provider,
                item["reporter"]["scheme"],
                str(item["reporter"]["code"]),
                canonical(dict(item["reporter"])),
                item["partner"]["scheme"],
                str(item["partner"]["code"]),
                canonical(dict(item["partner"])),
                str(item["flow"]["code"]),
                item["flow"]["direction"],
                item["flow"].get("label"),
                str(item["product"]["code"]),
                item["product"].get("label"),
                item["classification"]["scheme"],
                item["classification"]["vintage"],
                item["classification"].get("code"),
                item.get("frequency") or "unknown",
                item["valuation"]["basis"],
                item["valuation"]["source"],
                item["role"],
                canonical(dict(item["pair"])),
                item.get("measure") or "trade_value",
                canonical(dict(item["unit"])),
                canonical(dict(item.get("dimensions") or {})),
                release_id,
                self.now(),
            ],
        )
        return series_id, True

    @staticmethod
    def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: o.get(k) for k in ("period", "value_text", "value", "status", "flags", "quantities", "cif_value", "fob_value")}
            for o in sorted(observations, key=lambda o: o["period"])
        ]

    def _vintage(self, namespace, series_id, item, release_id, clock, basis, retrieved):
        observations = list(item.get("observations") or [])
        content_hash = digest(self._content(observations))
        existing = self.conn.execute(
            "SELECT vintage_id, content_hash FROM trade_vintages WHERE namespace=? AND series_id=? AND "
            "release_at_ms=? ORDER BY sequence LIMIT 1",
            [namespace, series_id, clock],
        ).fetchone()
        if existing:
            if existing[1] != content_hash:
                # Values changed without a new release clock: the stored vintage is kept and the publication refused.
                raise TradeError(
                    "vintage_conflict",
                    "the publication changed values without a new release time; the stored vintage is kept",
                    series_id=series_id,
                )
            return existing[0], False
        sequence = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(sequence), 0) FROM trade_vintages WHERE namespace=? AND series_id=?",
                [namespace, series_id],
            ).fetchone()[0]
        )
        vintage_id = "tf-vintage:" + digest([namespace, series_id, clock, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO trade_vintages VALUES (?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, clock, basis, retrieved, content_hash, sequence, self.now()],
        )
        for obs in observations:
            extra = {
                k: v
                for k, v in obs.items()
                if k not in {"period", "value_text", "value", "status", "flags", "quantities"}
            }
            self.conn.execute(
                "INSERT INTO trade_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    vintage_id,
                    obs["period"],
                    obs.get("value_text"),
                    obs.get("value"),
                    obs["status"],
                    canonical(dict(obs.get("flags") or {})),
                    canonical(list(obs.get("quantities") or [])),
                    canonical(extra),
                ],
            )
        return vintage_id, True

    def _label(self, namespace, scheme, vintage, product, release_id) -> int:
        label = (product or {}).get("label")
        if not label:
            return 0
        if self.conn.execute(
            "SELECT 1 FROM trade_code_labels WHERE namespace=? AND scheme=? AND vintage=? AND code=? AND release_id=?",
            [namespace, scheme, vintage, str(product["code"]), release_id],
        ).fetchone():
            return 0
        self.conn.execute(
            "INSERT INTO trade_code_labels VALUES (?,?,?,?,?,?)",
            [namespace, scheme, vintage, str(product["code"]), str(label), release_id],
        )
        return 1

    def _concordance(self, namespace, provider, item, release_id, clock):
        body = dict(item["concordance"])
        source, target = dict(body["source"]), dict(body["target"])
        key = "tf-concordance:" + digest(
            [namespace, provider, source["scheme"], source["vintage"], target["scheme"], target["vintage"], body["label"]]
        )[:24]
        rows = list(body["rows"])
        content_hash = digest(rows)
        latest = self.conn.execute(
            "SELECT concordance_id, content_hash, revision FROM trade_concordances WHERE namespace=? AND "
            "concordance_key=? ORDER BY revision DESC LIMIT 1",
            [namespace, key],
        ).fetchone()
        for row in rows:
            self._label(namespace, source["scheme"], source["vintage"], {"code": row["source_code"], "label": row.get("source_label")}, release_id)
            self._label(namespace, target["scheme"], target["vintage"], {"code": row["target_code"], "label": row.get("target_label")}, release_id)
        if latest and latest[1] == content_hash:
            return latest[0], False
        revision = 1 if latest is None else int(latest[2]) + 1
        concordance_id = f"{key}@{revision}"
        self.conn.execute(
            "INSERT INTO trade_concordances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                concordance_id,
                key,
                revision,
                provider,
                body["label"],
                source["scheme"],
                source["vintage"],
                target["scheme"],
                target["vintage"],
                content_hash,
                release_id,
                clock,
                len(rows),
                self.now(),
            ],
        )
        for row in rows:
            detail = {k: v for k, v in row.items() if k not in {"source_code", "target_code", "mapping_type", "mapping_basis", "weight", "weight_state"}}
            self.conn.execute(
                "INSERT INTO trade_concordance_rows VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    concordance_id,
                    row["source_code"],
                    row["target_code"],
                    row["mapping_type"],
                    row["mapping_basis"],
                    row.get("weight"),
                    row.get("weight_state") or "not-published",
                    canonical(detail),
                ],
            )
        return concordance_id, True

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id",
        "provider",
        "source_id",
        "format",
        "kind",
        "document",
        "published_on",
        "published_at",
        "release_basis",
        "release_at_ms",
        "release_label",
        "file_sha256",
        "content_sha256",
        "item_count",
        "structure",
        "evidence_origin",
        "url",
        "sequence",
        "run_id",
        "recorded_by",
        "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, kind, document_json, published_on, published_at, "
            "release_basis, release_at_ms, release_label, file_sha256, content_sha256, item_count, structure_json, "
            "evidence_origin, url, sequence, run_id, recorded_by, retrieved_at_ms FROM trade_releases WHERE "
            "namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "release is not visible in this namespace")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = _load(view["document"], {})
        view["structure"] = _load(view["structure"], {})
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        return {
            k: release[k]
            for k in (
                "release_id",
                "provider",
                "source_id",
                "file_sha256",
                "published_on",
                "published_at",
                "release_basis",
                "release_at_ms",
                "release_label",
                "url",
                "evidence_origin",
                "retrieved_at_ms",
            )
        } | {
            "document": release["document"].get("label"),
            "live_verification": LIVE_VERIFICATION.get(release["provider"], {}).get("status"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "trade_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM trade_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    _SERIES_COLUMNS = (
        "series_id, provider, reporter_json, partner_json, flow_code, flow_direction, flow_label, product_code, "
        "product_label, classification_scheme, classification_vintage, classification_code, frequency, "
        "valuation_basis, valuation_source, role, pair_json, measure, unit_json, dimensions_json, first_release_id"
    )

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (
            series_id,
            provider,
            reporter,
            partner,
            flow_code,
            direction,
            flow_label,
            product_code,
            product_label,
            scheme,
            vintage,
            classification_code,
            frequency,
            valuation_basis,
            valuation_source,
            role,
            pair,
            measure,
            unit,
            dimensions,
            first_release,
        ) = row
        vintages = self.vintage_rows(namespace, series_id)
        return {
            "contract": CONTRACT,
            "record_type": "series",
            "namespace": namespace,
            "series_id": series_id,
            "provider": provider,
            "reporter": _load(reporter, {}),
            "partner": _load(partner, {}),
            "flow": {"code": flow_code, "label": flow_label, "direction": direction},
            "product": {"code": product_code, "label": product_label},
            "classification": {"scheme": scheme, "vintage": vintage, "code": classification_code},
            "frequency": frequency,
            "valuation": {"basis": valuation_basis, "source": valuation_source},
            "role": role,
            "pair": _load(pair, {}),
            "measure": measure,
            "unit": _load(unit, {}),
            "dimensions": _load(dimensions, {}),
            "first_release_id": first_release,
            "vintage_count": len(vintages),
            "current_vintage_id": vintages[-1]["vintage_id"] if vintages else None,
        }

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS} FROM trade_series WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(
        self,
        namespace: str,
        *,
        provider: str | None = None,
        reporter_codes: Iterable[str] | None = None,
        partner_codes: Iterable[str] | None = None,
        product_codes: Iterable[str] | None = None,
        classification_scheme: str | None = None,
        classification_vintage: str | None = None,
        flow_direction: str | None = None,
        role: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS}, reporter_code, partner_code FROM trade_series WHERE namespace=? AND "
            "(? IS NULL OR provider=?) AND (? IS NULL OR classification_scheme=?) AND "
            "(? IS NULL OR classification_vintage=?) AND (? IS NULL OR flow_direction=?) AND (? IS NULL OR role=?) "
            "ORDER BY provider, reporter_code, partner_code, flow_code, product_code, classification_vintage, series_id",
            [
                namespace,
                provider,
                provider,
                classification_scheme,
                classification_scheme,
                classification_vintage,
                classification_vintage,
                flow_direction,
                flow_direction,
                role,
                role,
            ],
        ).fetchall()
        reporters = None if reporter_codes is None else {str(c) for c in reporter_codes}
        partners = None if partner_codes is None else {str(c) for c in partner_codes}
        products = None if product_codes is None else {str(c) for c in product_codes}
        out = []
        for row in rows:
            reporter_code, partner_code = row[-2], row[-1]
            if reporters is not None and reporter_code not in reporters:
                continue
            if partners is not None and partner_code not in partners:
                continue
            if products is not None and row[7] not in products:
                continue
            out.append(self._series_view(namespace, row[:-2]))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage of a series in release-clock order, each with ``revision_of`` its predecessor."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, release_at_basis, retrieved_at_ms, content_hash, sequence "
            "FROM trade_vintages WHERE namespace=? AND series_id=? ORDER BY release_at_ms, sequence",
            [namespace, series_id],
        ).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(
                zip(
                    (
                        "vintage_id",
                        "release_id",
                        "release_at_ms",
                        "release_at_basis",
                        "retrieved_at_ms",
                        "content_hash",
                        "sequence",
                    ),
                    row,
                )
            )
            view["series_id"] = series_id
            view["release_at"] = iso_from_ms(view["release_at_ms"])
            view["revision_of"] = previous["vintage_id"] if previous else None
            view["values_changed"] = previous is None or previous["content_hash"] != view["content_hash"]
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id FROM trade_vintages WHERE namespace=? AND vintage_id=?", [namespace, vintage_id]
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "vintage is not visible in this namespace")
        view = next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)
        return {
            "contract": CONTRACT,
            "record_type": "vintage",
            "namespace": namespace,
            **view,
            "source_revision": self.source_revision(namespace, view["release_id"]),
        }

    def observations(
        self,
        namespace: str,
        vintage_id: str,
        *,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, flags_json, quantities_json, extra_json FROM trade_observations "
            "WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id],
        ).fetchall()
        out = []
        for period, value_text, value, status, flags, quantities, extra in rows:
            if period_from and period < period_from:
                continue
            if period_to and period[: len(period_to)] > period_to:
                continue
            out.append(
                {
                    "record_type": "observation",
                    "period": period,
                    "value_text": value_text,
                    "value": value,
                    "status": status,
                    "flags": _load(flags, {}),
                    "quantities": _load(quantities, []),
                    **_load(extra, {}),
                }
            )
        return out

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [
            {"series_id": r[0], "vintage_id": r[1]}
            for r in self.conn.execute(
                "SELECT series_id, vintage_id FROM trade_release_members WHERE namespace=? AND release_id=? "
                "ORDER BY series_id",
                [namespace, release_id],
            ).fetchall()
        ]

    def select_vintage(
        self, namespace: str, series_id: str, *, as_of_ms: int | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (release-cutoff semantics of the economic release store)."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def values(
        self,
        namespace: str,
        series_id: str,
        *,
        vintage_id: str | None = None,
        as_of_ms: int | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise TradeError("not_found", "vintage does not belong to this series")
        else:
            vintage, reason = self.select_vintage(namespace, series_id, as_of_ms=as_of_ms)
        if vintage is None:
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "unavailable", "reason": reason,
                    "observations": []}
        return {
            "contract": ANSWER_CONTRACT,
            "series": series,
            "status": "available",
            "vintage": {**vintage, "source_revision": self.source_revision(namespace, vintage["release_id"])},
            "observations": self.observations(
                namespace, vintage["vintage_id"], period_from=period_from, period_to=period_to
            ),
            "note": "values as published in this vintage; confidential and unpublished cells carry no value",
        }

    # ------------------------------------------------------------------ concordances and labels

    def concordance(self, namespace: str, concordance_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT concordance_id, concordance_key, revision, provider, label, source_scheme, source_vintage, "
            "target_scheme, target_vintage, content_hash, release_id, release_at_ms, row_count FROM trade_concordances "
            "WHERE namespace=? AND concordance_id=?",
            [namespace, concordance_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "concordance is not visible in this namespace")
        view = dict(
            zip(
                (
                    "concordance_id",
                    "concordance_key",
                    "revision",
                    "provider",
                    "label",
                    "source_scheme",
                    "source_vintage",
                    "target_scheme",
                    "target_vintage",
                    "content_hash",
                    "release_id",
                    "release_at_ms",
                    "row_count",
                ),
                row,
            )
        )
        return {
            "contract": CONTRACT,
            "record_type": "concordance",
            "namespace": namespace,
            "concordance_id": view["concordance_id"],
            "concordance_key": view["concordance_key"],
            "revision": view["revision"],
            "provider": view["provider"],
            "label": view["label"],
            "source": {"scheme": view["source_scheme"], "vintage": view["source_vintage"]},
            "target": {"scheme": view["target_scheme"], "vintage": view["target_vintage"]},
            "row_count": view["row_count"],
            "release_at_ms": view["release_at_ms"],
            "source_revision": self.source_revision(namespace, view["release_id"]),
        }

    def concordances(self, namespace: str, *, as_of_ms: int | None = None) -> list[dict[str, Any]]:
        """The concordance revision in force per table (latest by release clock at the cutoff)."""
        if not table_exists(self.conn, "trade_concordances"):
            return []
        rows = self.conn.execute(
            "SELECT concordance_id, concordance_key, release_at_ms, revision FROM trade_concordances WHERE namespace=? "
            "ORDER BY concordance_key, release_at_ms, revision",
            [namespace],
        ).fetchall()
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        current: dict[str, str] = {}
        for concordance_id, key, clock, _revision in rows:
            if clock <= cutoff:
                current[key] = concordance_id
        return [self.concordance(namespace, cid) for cid in current.values()]

    def concordance_history(self, namespace: str, concordance_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT concordance_id FROM trade_concordances WHERE namespace=? AND concordance_key=? ORDER BY revision",
            [namespace, concordance_key],
        ).fetchall()
        return [self.concordance(namespace, r[0]) for r in rows]

    def concordance_rows(
        self, namespace: str, concordance_id: str, *, source_code: str | None = None, target_code: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_code, target_code, mapping_type, mapping_basis, weight, weight_state, detail_json FROM "
            "trade_concordance_rows WHERE namespace=? AND concordance_id=? AND (? IS NULL OR source_code=?) AND "
            "(? IS NULL OR target_code=?) ORDER BY source_code, target_code",
            [namespace, concordance_id, source_code, source_code, target_code, target_code],
        ).fetchall()
        return [
            {
                "record_type": "concordance_row",
                "concordance_id": concordance_id,
                "source_code": r[0],
                "target_code": r[1],
                "mapping_type": r[2],
                "mapping_basis": r[3],
                **({"weight": r[4]} if r[4] is not None else {}),
                "weight_state": r[5],
                **_load(r[6], {}),
            }
            for r in rows
        ]

    def map_code(
        self,
        namespace: str,
        code: str,
        source: Mapping[str, str],
        target: Mapping[str, str],
        *,
        as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        """Target codes for one code through the concordance in force (either direction), each row cited with its
        mapping type; ``exact`` only for a 1:1 row. Nothing is weighted or estimated."""
        found = []
        for table in self.concordances(namespace, as_of_ms=as_of_ms):
            forward = table["source"] == dict(source) and table["target"] == dict(target)
            backward = table["source"] == dict(target) and table["target"] == dict(source)
            if not forward and not backward:
                continue
            rows = self.concordance_rows(
                namespace,
                table["concordance_id"],
                **({"source_code": str(code)} if forward else {"target_code": str(code)}),
            )
            for row in rows:
                published = row["mapping_type"]
                # Read backwards, a 1:n row is n:1 from the other side.
                seen_as = published if forward else {"1:n": "n:1", "n:1": "1:n"}.get(published, published)
                found.append(
                    {
                        "code": row["target_code"] if forward else row["source_code"],
                        "mapping_type": seen_as,
                        "mapping_basis": row["mapping_basis"],
                        "direction": "forward" if forward else "reverse",
                        "exact": seen_as == "1:1",
                        "weight_state": row["weight_state"],
                        "concordance": {
                            "concordance_id": table["concordance_id"],
                            "label": table["label"],
                            "revision": table["revision"],
                            "source_revision": table["source_revision"],
                        },
                    }
                )
        return {
            "code": str(code),
            "source": dict(source),
            "target": dict(target),
            "status": "mapped" if found else "no_concordance",
            "targets": found,
        }

    def labels(self, namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "trade_code_labels"):
            return []
        return [
            {"scheme": scheme, "vintage": r[0], "code": code, "label": r[1], "release_id": r[2]}
            for r in self.conn.execute(
                "SELECT vintage, label, release_id FROM trade_code_labels WHERE namespace=? AND scheme=? AND code=? "
                "ORDER BY vintage, release_id",
                [namespace, scheme, str(code)],
            ).fetchall()
        ]

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "trade_releases"):
            return None
        row = self.conn.execute(
            "SELECT max(retrieved_at_ms) FROM trade_releases WHERE namespace=?", [namespace]
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])


class TradeComparability:
    """Reviewable typed notes between two trade series (the :mod:`src.kb.demographics_comparability` pattern)."""

    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = TradeFlowStore(conn, initialize=initialize, now=now)
        self.now = self.store.now

    @staticmethod
    def pair_key(left: str, right: str) -> str:
        return canonical(sorted([str(left), str(right)]))

    def _cite(self, namespace: str, series_id: str) -> dict[str, Any]:
        series = self.store.series(namespace, series_id)
        vintages = self.store.vintage_rows(namespace, series_id)
        return {
            "series_id": series_id,
            "provider": series["provider"],
            "role": series["role"],
            "valuation": series["valuation"],
            "classification": series["classification"],
            "source_revision": None
            if not vintages
            else self.store.source_revision(namespace, vintages[-1]["release_id"]),
        }

    def record(
        self,
        namespace: str,
        left_series_id: str,
        right_series_id: str,
        relation: str,
        statement: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if relation not in RELATIONS or not str(statement or "").strip():
            raise TradeError("invalid_note", f"a note has one of {RELATIONS} and a statement")
        if left_series_id == right_series_id:
            raise TradeError("invalid_note", "a note links two different series")
        cited = sorted([self._cite(namespace, left_series_id), self._cite(namespace, right_series_id)],
                       key=lambda c: c["series_id"])
        key = self.pair_key(left_series_id, right_series_id)
        note_id = "tf-comparability:" + digest([namespace, key, relation, statement.strip()])[:24]
        if not self.conn.execute(
            "SELECT 1 FROM trade_comparability WHERE namespace=? AND note_id=?", [namespace, note_id]
        ).fetchone():
            now = self.now()
            self.conn.execute(
                "INSERT INTO trade_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    note_id,
                    key,
                    left_series_id,
                    right_series_id,
                    relation,
                    statement.strip(),
                    canonical(cited),
                    "proposed",
                    canonical([{"state": "proposed", "by": principal_id, "at_ms": now}]),
                    principal_id,
                    now,
                ],
            )
        return self.note(namespace, note_id, scopes={"operator"})

    def _transition(self, namespace, note, state, principal_id, reason):
        history = note["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE trade_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
            [state, canonical(history), namespace, note["note_id"]],
        )
        return self.note(namespace, note["note_id"], scopes={"operator"})

    def review(self, namespace, note_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise TradeError("invalid_decision", "accept or reject with a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] != "proposed":
            raise TradeError("invalid_state", f"note is {note['state']}; only a proposed note is reviewed")
        return self._transition(
            namespace, note, "accepted" if decision == "accept" else "rejected", principal_id, reason.strip()
        )

    def revert(self, namespace, note_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise TradeError("invalid_decision", "a revert needs a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] not in {"accepted", "rejected"}:
            raise TradeError("invalid_state", "only an accepted or rejected note can be reverted")
        return self._transition(namespace, note, "reverted", principal_id, reason.strip())

    def note(self, namespace: str, note_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT note_id, pair_key, left_id, right_id, relation, statement, cited_json, state, history_json, "
            "created_by, created_at_ms FROM trade_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "comparability note is not visible in this namespace")
        return {
            "contract": COMPARABILITY_CONTRACT,
            "record_type": "comparability_note",
            "namespace": namespace,
            "note_id": row[0],
            "pair_key": row[1],
            "left_series_id": row[2],
            "right_series_id": row[3],
            "relation": row[4],
            "statement": row[5],
            "cited": json.loads(row[6]),
            "state": row[7],
            "history": json.loads(row[8]),
            "created_by": row[9],
            "created_at_ms": row[10],
        }

    def notes_for(self, namespace: str, left_series_id: str, right_series_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "trade_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id FROM trade_comparability WHERE namespace=? AND pair_key=? ORDER BY created_at_ms, note_id",
            [namespace, self.pair_key(left_series_id, right_series_id)],
        ).fetchall()
        notes = [self.note(namespace, r[0], scopes={"operator"}) for r in rows]
        return [
            {k: n[k] for k in ("note_id", "relation", "statement", "state")}
            for n in notes
            if n["state"] in ACTIVE_STATES
        ]

    def notes(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "trade_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id FROM trade_comparability WHERE namespace=? AND (? IS NULL OR left_id=? OR right_id=?) "
            "ORDER BY created_at_ms, note_id",
            [namespace, series_id, series_id, series_id],
        ).fetchall()
        return [self.note(namespace, r[0], scopes=scopes) for r in rows]


def comparability_basis(reporter: Mapping[str, Any] | None, mirror: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Recorded differences between a reporter and a mirror series (facts of the records, not a reconciliation)."""
    if not reporter or not mirror:
        return []
    out = []
    if reporter["valuation"]["basis"] != mirror["valuation"]["basis"]:
        out.append(
            {
                "kind": "different_valuation_basis",
                "detail": f"{reporter['valuation']['basis']} ({reporter['valuation']['source']}) against "
                f"{mirror['valuation']['basis']} ({mirror['valuation']['source']}); freight and insurance are part "
                "of a CIF value and not of a FOB value",
            }
        )
    if reporter["classification"] != mirror["classification"]:
        out.append(
            {
                "kind": "different_classification_vintage",
                "detail": f"{reporter['classification']['vintage']} code {reporter['product']['code']} against "
                f"{mirror['classification']['vintage']} code {mirror['product']['code']}",
            }
        )
    if reporter["unit"] != mirror["unit"]:
        out.append({"kind": "different_unit", "detail": f"{reporter['unit']} against {mirror['unit']}"})
    return out


class TradeFlowProjector:
    """Source-pack runtime projector for ``noesis-trade-flow-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = TradeFlowStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("trade_flows") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("trade_release") or {}), item.get("trade_item")
            if not header or not isinstance(body, Mapping):
                raise TradeError("invalid_record", "page record is not a trade release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body)
            )
        namespace = self._namespace(source)
        return [
            self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
            for header, items in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM trade_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_release_id": row[0] if row else None,
            "latest_published_on": row[1] if row else None,
        }


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the trade-flow record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "trade-flow-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": json.loads(path.read_text()),
        "owner": "economics.trade",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition, "trade-schema:trade-flow-record:1.0.0", principal_id=principal_id, scopes=scopes
        )
    ]


def readiness(conn: Any) -> dict[str, Any]:
    store = TradeFlowStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "trade_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(
                conn.execute("SELECT count(*) FROM trade_releases WHERE provider=?", [provider]).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "live_verification": LIVE_VERIFICATION[provider]["status"],
            "releases": releases,
        }
    selected = _selected_features(conn)
    return {
        "features": {feature: feature in selected for feature in FEATURES},
        "selected": any(feature in selected for feature in FEATURES),
        "stores_ready": ready,
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "FEATURES",
    "READ_SCOPE",
    "REVIEW_SCOPE",
    "TradeComparability",
    "TradeError",
    "TradeFlowProjector",
    "TradeFlowStore",
    "WRITE_SCOPE",
    "authorize",
    "comparability_basis",
    "feature_enabled",
    "forbidden_keys",
    "mapping_types",
    "readiness",
    "register_schemas",
]

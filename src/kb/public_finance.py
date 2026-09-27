"""Public budgets, outturns, beneficiary payments and audit findings: the ``economics.public-finance`` record owner.

Records (contract ``noesis-public-finance-record-v1``, #1909 B02), each carrying
its provider, source id, source revision (the acquired release) and retrieval
time, in namespace-scoped, revision-addressable ``public_finance_*`` tables:

* **release** - one acquired file (or operator finding sheet): provider,
  document, publication date (the source's ``Stand``), file digest, accounting
  basis and evidence origin; the source revision of every figure derived from
  it. Re-acquiring an unchanged file adds nothing, whatever the fetch time.
* **budget line** - one line in the source's own hierarchy (Einzelplan /
  Kapitel / Titel for the federal budget; Bereich / Einzelplan / Kapitel /
  Titel for Berlin, with the district a Bereich stands for; the EU budget line
  number for FTS), with the codes and labels as published. No hierarchy is
  synthesised across sources.
* **budget plan** - one plan document of a fiscal year (the budget as enacted,
  or a numbered supplementary budget) as the source identifies it.
* **figure** - a plan, supplementary-plan or outturn amount for one line and
  fiscal year: amount text as published, the parsed decimal, the published
  unit, a pint-normalised value in the currency unit, the accounting basis,
  and the release it came from. Each series (line, year, kind, plan, source)
  is revisioned: a changed figure is a new revision, the previous one stays
  addressable, and the revision in force follows the source's own publication
  date (a late-arriving older file lands as history). Every release that states
  a figure is recorded as a vintage of it, so outturn vintages stay visible
  even when a later vintage repeats the same amount.
* **beneficiary payment** - one FTS row: beneficiary as the source string (with
  the identifiers it states), budget line and programme as published,
  commitment or payment kind, amount, currency and year; revisioned the same
  way. Identity links are reviewable decisions (:mod:`src.kb.public_finance_identity`).
* **audit finding** - one passage of an audit report (Bundesrechnungshof) as an
  operator recorded it: report, passage locator, quoted text and the budget
  lines it cites. There is no verdict field and none is accepted.

Plan, outturn and payment figures are never merged into one number, and
nothing here forecasts a fiscal outcome or determines waste or fraud.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from src.ingestion.public_finance_sources import (
    ACCOUNTING_BASES,
    FIGURE_KINDS,
    FORMATS,
    PAYMENT_KINDS,
    PROVIDER_CONTRACTS,
    REVIEW_BOUNDARY,
    SCHEMES,
    code,
    unit_scale,
)

CONTRACT = "noesis-public-finance-record-v1"
ANSWER_CONTRACT = "noesis-public-finance-answer-v1"
READ_SCOPE = "knowledge:economic:public-finance:read"
WRITE_SCOPE = "knowledge:economic:public-finance:write"
REVIEW_SCOPE = "knowledge:economic:public-finance:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "release",
    "budget_line",
    "budget_plan",
    "figure",
    "beneficiary_payment",
    "audit_finding",
)
# Keys that would carry a forecast, a waste or fraud determination or a netted cross-basis number.
FORBIDDEN_KEYS = frozenset(
    {
        "verdict",
        "finding_verdict",
        "waste",
        "wasteful",
        "fraud",
        "fraudulent",
        "irregular",
        "irregularity",
        "misuse",
        "forecast",
        "projection",
        "projected",
        "prediction",
        "predicted",
        "net_total",
        "netted",
        "combined_total",
        "average",
        "risk_score",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS public_finance_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  record_type TEXT NOT NULL, document_json TEXT NOT NULL, accounting_basis TEXT, published_on TEXT NOT NULL,
  published_at TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  unit TEXT, evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL, run_id TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS public_finance_lines (
  namespace TEXT NOT NULL, line_id TEXT NOT NULL, provider TEXT NOT NULL, scheme TEXT NOT NULL,
  codes_json TEXT NOT NULL, labels_json TEXT NOT NULL, side TEXT, district_json TEXT, line_key TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, line_id)
);
CREATE TABLE IF NOT EXISTS public_finance_plans (
  namespace TEXT NOT NULL, plan_id TEXT NOT NULL, provider TEXT NOT NULL, fiscal_year TEXT NOT NULL,
  figure_kind TEXT NOT NULL, plan_key TEXT NOT NULL, label TEXT, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, plan_id)
);
CREATE TABLE IF NOT EXISTS public_finance_figures (
  namespace TEXT NOT NULL, figure_id TEXT NOT NULL, series_key TEXT NOT NULL, line_id TEXT NOT NULL,
  plan_id TEXT, fiscal_year TEXT NOT NULL, figure_kind TEXT NOT NULL, plan_key TEXT NOT NULL, provider TEXT NOT NULL,
  source_id TEXT, revision_no INTEGER NOT NULL, previous_figure_id TEXT, published_on TEXT NOT NULL,
  published_at TEXT, accounting_basis TEXT NOT NULL, amount_text TEXT, amount TEXT, unit TEXT NOT NULL,
  currency TEXT NOT NULL, normalized_json TEXT, statement_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  release_id TEXT NOT NULL, evidence_origin TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, figure_id)
);
CREATE TABLE IF NOT EXISTS public_finance_figure_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_key TEXT NOT NULL, figure_id TEXT NOT NULL,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_key)
);
CREATE TABLE IF NOT EXISTS public_finance_payments (
  namespace TEXT NOT NULL, payment_id TEXT NOT NULL, series_key TEXT NOT NULL, payment_key TEXT NOT NULL,
  provider TEXT NOT NULL, source_id TEXT, revision_no INTEGER NOT NULL, previous_payment_id TEXT,
  fiscal_year TEXT NOT NULL, payment_kind TEXT NOT NULL, accounting_basis TEXT NOT NULL, beneficiary TEXT NOT NULL,
  beneficiary_key TEXT NOT NULL, line_id TEXT, budget_line TEXT, programme TEXT, amount_text TEXT, amount TEXT,
  currency TEXT NOT NULL, normalized_json TEXT, statement_json TEXT NOT NULL, published_on TEXT NOT NULL,
  published_at TEXT, content_hash TEXT NOT NULL, release_id TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, payment_id)
);
CREATE TABLE IF NOT EXISTS public_finance_payment_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_key TEXT NOT NULL, payment_id TEXT NOT NULL,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_key)
);
CREATE TABLE IF NOT EXISTS public_finance_findings (
  namespace TEXT NOT NULL, finding_id TEXT NOT NULL, finding_key TEXT NOT NULL, report_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_finding_id TEXT, report_json TEXT NOT NULL, passage_json TEXT NOT NULL,
  quote TEXT NOT NULL, cites_json TEXT NOT NULL, published_on TEXT NOT NULL, content_hash TEXT NOT NULL,
  release_id TEXT NOT NULL, recorded_by TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, finding_id)
);
"""

SOURCE_ORDER = "published_on DESC, coalesce(published_at, '') DESC, revision_no DESC"


class PublicFinanceError(ValueError):
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
        raise PublicFinanceError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise PublicFinanceError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise PublicFinanceError(
            "invalid_date", f"{value!r} is not an ISO date"
        ) from exc


def normalize_name(value: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-zà-ɏ]+", " ", str(value or "").casefold()).split())


def normalize_identifier(value: Any) -> str:
    """Identifier comparison key, the same on both sides of every match: alphanumerics, upper case."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def beneficiary_record_key(
    provider: str, name: str, country: Any, identifiers: Sequence[Mapping[str, Any]]
) -> str:
    """A beneficiary as one source states it; identity across sources is a reviewable decision, never this key."""
    country_code = (str(country or "").strip().upper()[:2] or "XX") if country else "XX"
    vat = next(
        (i for i in identifiers if i.get("scheme") == "vat" and i.get("value")), None
    )
    if vat is not None:
        return f"public-finance:beneficiary:{provider}:vat:{country_code}:{normalize_identifier(vat['value'])}"
    return f"public-finance:beneficiary:{provider}:name:{country_code}:{normalize_name(name).replace(' ', '-')}"


def entity_id(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def normalise_amount(amount: Any, unit: str) -> dict[str, Any] | None:
    """The amount in the currency's base unit through the pint owner; currency itself is never converted."""
    if amount is None:
        return None
    label, pint_unit, scale = unit_scale(unit)
    currency = label.split()[-1].upper()
    if scale == 1:
        return {
            "value": str(Decimal(str(amount))),
            "currency": currency,
            "scale": 1,
            "method": "identity (published in the currency unit)",
            "receipt_sha256": None,
        }
    try:
        from src.integrations.units import convert_physical

        receipt = convert_physical(str(amount), pint_unit, "count", precision=2)
    except ModuleNotFoundError:
        return {
            "value": None,
            "currency": currency,
            "scale": scale,
            "method": "not normalised: the pint dependency is not installed",
            "receipt_sha256": None,
        }
    return {
        "value": receipt["result"]["value"],
        "currency": currency,
        "scale": scale,
        "method": f"pint convert_physical {pint_unit} -> count (scale only; no currency conversion)",
        "receipt_sha256": receipt.get("sha256"),
    }


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Economics bundle's optional ``public-finance`` feature is selected in the active plan."""
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
            "SELECT authority FROM composition_authority WHERE bundle='economics'"
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
    return "public-finance" in ((plan.get("features") or {}).get("economics") or [])


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a verdict, a forecast or a netted cross-basis number."""
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


class PublicFinanceStore:
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

    def ready(self) -> bool:
        return table_exists(self.conn, "public_finance_figures")

    # ------------------------------------------------------------------ releases

    def _release(self, namespace, header, *, source_id, run_id, retrieved, record_type):
        provider = str(header.get("provider") or "")
        if (
            header.get("format") not in FORMATS
            or FORMATS[header["format"]]["provider"] != provider
        ):
            raise PublicFinanceError(
                "invalid_release", "release names a known provider and format"
            )
        if FORMATS[header["format"]]["records"] != record_type:
            raise PublicFinanceError(
                "invalid_release", f"release does not carry {record_type}"
            )
        document = dict(header.get("document") or {})
        # The release id changes whenever the file or its declared reading (amount columns, kinds) changes.
        release_id = (
            "pf-release:"
            + digest(
                [
                    namespace,
                    provider,
                    source_id,
                    header["file_sha256"],
                    document,
                    header.get("amounts"),
                    header.get("accounting_basis"),
                ]
            )[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM public_finance_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM public_finance_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO public_finance_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                release_id,
                provider,
                source_id,
                header["format"],
                record_type,
                canonical({**document, "amounts": header.get("amounts")}),
                header.get("accounting_basis"),
                _day(header["published_on"]),
                header.get("published_at"),
                header["file_sha256"],
                header.get("content_sha256") or "",
                int(header.get("item_count") or 0),
                header.get("unit"),
                "fixture" if header.get("evidence_origin") == "fixture" else "live",
                header.get("url"),
                int(sequence) + 1,
                run_id,
                retrieved,
            ],
        )
        return release_id, True

    def apply_figures(
        self,
        namespace: str,
        header: Mapping[str, Any],
        figures: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one budget file: lines, plans and a figure revision wherever a published figure is new."""
        if int(header.get("item_count", -1)) != len(figures):
            raise PublicFinanceError(
                "incomplete_release", "a release carries every row it states"
            )
        basis = header.get("accounting_basis")
        if basis not in ACCOUNTING_BASES:
            raise PublicFinanceError(
                "invalid_release", "the release declares no accounting basis"
            )
        for figure in figures:
            if (
                figure.get("figure_kind") not in FIGURE_KINDS
                or figure.get("scheme") not in SCHEMES
            ):
                raise PublicFinanceError(
                    "invalid_release",
                    "a figure names a known kind and hierarchy scheme",
                )
            if forbidden_keys(dict(figure)):
                raise PublicFinanceError(
                    "invalid_release", "published figures carry no derived verdicts"
                )
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {"lines": 0, "plans": 0, "revisions": 0, "vintages": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace,
                header,
                source_id=source_id,
                run_id=run_id,
                retrieved=retrieved,
                record_type="figures",
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            published = _day(header["published_on"])
            origin = "fixture" if header.get("evidence_origin") == "fixture" else "live"
            label = dict(header.get("document") or {}).get("label")
            for figure in figures:
                line_id, new_line = self._line(
                    namespace, header["provider"], figure, release_id, retrieved
                )
                counts["lines"] += int(new_line)
                plan_id = None
                if figure["figure_kind"] != "outturn":
                    plan_id, new_plan = self._plan(
                        namespace,
                        header["provider"],
                        figure,
                        label,
                        release_id,
                        retrieved,
                    )
                    counts["plans"] += int(new_plan)
                series_key = digest(
                    [
                        namespace,
                        line_id,
                        figure["fiscal_year"],
                        figure["figure_kind"],
                        figure["plan_key"],
                        source_id,
                    ]
                )
                if self.conn.execute(
                    "SELECT 1 FROM public_finance_figure_members WHERE namespace=? AND release_id=? AND series_key=?",
                    [namespace, release_id, series_key],
                ).fetchone():
                    raise PublicFinanceError(
                        "invalid_release",
                        "a release states the same line, year and kind twice",
                        line=figure["codes"],
                        fiscal_year=figure["fiscal_year"],
                    )
                figure_id, new = self._observe_figure(
                    namespace,
                    series_key,
                    line_id,
                    plan_id,
                    figure,
                    header,
                    basis,
                    published,
                    release_id,
                    source_id,
                    origin,
                    retrieved,
                )
                counts["revisions"] += int(new)
                counts["vintages"] += 1
                self.conn.execute(
                    "INSERT INTO public_finance_figure_members VALUES (?,?,?,?,?)",
                    [
                        namespace,
                        release_id,
                        series_key,
                        figure_id,
                        canonical(figure.get("locator") or {}),
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": published,
            "evidence_origin": origin,
            **counts,
        }

    def _line(
        self, namespace, provider, figure, release_id, retrieved
    ) -> tuple[str, bool]:
        scheme = figure["scheme"]
        identity = {k: code(figure["codes"].get(k)) for k in SCHEMES[scheme]}
        if not all(identity.values()):
            raise PublicFinanceError(
                "invalid_release", "a budget line states every code of its scheme"
            )
        line_key = f"{scheme}:" + "/".join(identity[k] for k in SCHEMES[scheme])
        line_id = (
            "pf-line:"
            + digest([namespace, provider, scheme, identity, figure.get("side")])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM public_finance_lines WHERE namespace=? AND line_id=?",
            [namespace, line_id],
        ).fetchone():
            return line_id, False
        self.conn.execute(
            "INSERT INTO public_finance_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                line_id,
                provider,
                scheme,
                canonical(identity),
                canonical(figure.get("labels") or {}),
                figure.get("side"),
                None if not figure.get("district") else canonical(figure["district"]),
                line_key,
                release_id,
                retrieved,
            ],
        )
        return line_id, True

    def _plan(
        self, namespace, provider, figure, label, release_id, retrieved
    ) -> tuple[str, bool]:
        plan_id = (
            "pf-plan:"
            + digest(
                [
                    namespace,
                    provider,
                    figure["fiscal_year"],
                    figure["figure_kind"],
                    figure["plan_key"],
                ]
            )[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM public_finance_plans WHERE namespace=? AND plan_id=?",
            [namespace, plan_id],
        ).fetchone():
            return plan_id, False
        self.conn.execute(
            "INSERT INTO public_finance_plans VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                plan_id,
                provider,
                figure["fiscal_year"],
                figure["figure_kind"],
                figure["plan_key"],
                label,
                release_id,
                retrieved,
            ],
        )
        return plan_id, True

    def _next_revision(
        self, table, namespace, series_key, content_hash, published, published_at
    ):
        """(existing id, None) when the figure equals the one in force at this publication date, else (None, prev)."""
        key = "figure_id" if table == "public_finance_figures" else "payment_id"
        same = self.conn.execute(
            f"SELECT {key} FROM {table} WHERE namespace=? AND series_key=? AND content_hash=? AND published_on=? "
            "ORDER BY revision_no LIMIT 1",
            [namespace, series_key, content_hash, published],
        ).fetchone()
        if same:
            return same[0], None
        # Dedupe only against the revision in force at this publication date: equal figures to an earlier, since
        # superseded revision are a new revision - a reversion to earlier figures is itself a correction.
        current = self.conn.execute(
            f"SELECT {key}, content_hash FROM {table} WHERE namespace=? AND series_key=? AND "
            "(published_on<? OR (published_on=? AND coalesce(published_at,'')<=coalesce(?,''))) "
            f"ORDER BY {SOURCE_ORDER} LIMIT 1",
            [namespace, series_key, published, published, published_at],
        ).fetchone()
        if current and current[1] == content_hash:
            return current[0], None
        return None, current[0] if current else None

    def _observe_figure(
        self,
        namespace,
        series_key,
        line_id,
        plan_id,
        figure,
        header,
        basis,
        published,
        release_id,
        source_id,
        origin,
        retrieved,
    ) -> tuple[str, bool]:
        statement = {
            "amount_text": figure.get("amount_text"),
            "amount": figure.get("amount"),
            "unit": figure["unit"],
            "accounting_basis": basis,
            "labels": figure.get("labels") or {},
            "codes": figure.get("codes") or {},
        }
        content_hash = digest(statement)
        existing, previous = self._next_revision(
            "public_finance_figures",
            namespace,
            series_key,
            content_hash,
            published,
            header.get("published_at"),
        )
        if existing:
            return existing, False
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(revision_no), 0) FROM public_finance_figures WHERE namespace=? AND series_key=?",
                [namespace, series_key],
            ).fetchone()[0]
        )
        figure_id = (
            "pf-figure:" + digest([namespace, series_key, number, release_id])[:24]
        )
        normalized = normalise_amount(figure.get("amount"), figure["unit"])
        currency = unit_scale(figure["unit"])[0].split()[-1].upper()
        self.conn.execute(
            "INSERT INTO public_finance_figures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                figure_id,
                series_key,
                line_id,
                plan_id,
                figure["fiscal_year"],
                figure["figure_kind"],
                figure["plan_key"],
                header["provider"],
                source_id,
                number,
                previous,
                published,
                header.get("published_at"),
                basis,
                figure.get("amount_text"),
                figure.get("amount"),
                figure["unit"],
                currency,
                None if normalized is None else canonical(normalized),
                canonical(statement),
                content_hash,
                release_id,
                origin,
                retrieved,
            ],
        )
        return figure_id, True

    def apply_payments(
        self,
        namespace: str,
        header: Mapping[str, Any],
        payments: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one FTS file: a payment revision wherever a published row is new or changed."""
        if int(header.get("item_count", -1)) != len(payments):
            raise PublicFinanceError(
                "incomplete_release", "a release carries every row it states"
            )
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {"lines": 0, "revisions": 0, "payments": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace,
                header,
                source_id=source_id,
                run_id=run_id,
                retrieved=retrieved,
                record_type="payments",
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            published = _day(header["published_on"])
            origin = "fixture" if header.get("evidence_origin") == "fixture" else "live"
            for payment in payments:
                if payment.get("payment_kind") not in PAYMENT_KINDS:
                    raise PublicFinanceError(
                        "invalid_release", "a payment is a commitment or a payment"
                    )
                line_id = None
                if payment.get("budget_line"):
                    line_id, new_line = self._line(
                        namespace,
                        header["provider"],
                        {
                            "scheme": "eu-budget-line",
                            "codes": {"budget_line": payment["budget_line"]},
                            "labels": {"budget_line": payment.get("budget_line_name")},
                            "side": "expenditure",
                        },
                        release_id,
                        retrieved,
                    )
                    counts["lines"] += int(new_line)
                series_key = digest(
                    [
                        namespace,
                        header["provider"],
                        source_id,
                        payment["fiscal_year"],
                        payment["payment_kind"],
                        payment["payment_key"],
                    ]
                )
                payment_id, new = self._observe_payment(
                    namespace,
                    series_key,
                    line_id,
                    payment,
                    header,
                    published,
                    release_id,
                    source_id,
                    origin,
                    retrieved,
                )
                counts["revisions"] += int(new)
                counts["payments"] += 1
                self.conn.execute(
                    "INSERT INTO public_finance_payment_members VALUES (?,?,?,?,?)",
                    [
                        namespace,
                        release_id,
                        series_key,
                        payment_id,
                        canonical(payment.get("locator") or {}),
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": published,
            "evidence_origin": origin,
            **counts,
        }

    def _observe_payment(
        self,
        namespace,
        series_key,
        line_id,
        payment,
        header,
        published,
        release_id,
        source_id,
        origin,
        retrieved,
    ) -> tuple[str, bool]:
        statement = {
            k: payment.get(k)
            for k in (
                "beneficiary",
                "beneficiary_country",
                "beneficiary_identifiers",
                "beneficiary_city",
                "budget_line",
                "budget_line_text",
                "programme",
                "position_key",
                "grant_reference",
                "funding_type",
                "subject",
                "amount_text",
                "amount",
                "currency",
            )
        }
        content_hash = digest(statement)
        existing, previous = self._next_revision(
            "public_finance_payments",
            namespace,
            series_key,
            content_hash,
            published,
            header.get("published_at"),
        )
        if existing:
            return existing, False
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(revision_no), 0) FROM public_finance_payments WHERE namespace=? AND series_key=?",
                [namespace, series_key],
            ).fetchone()[0]
        )
        payment_id = (
            "pf-payment:" + digest([namespace, series_key, number, release_id])[:24]
        )
        key = beneficiary_record_key(
            header["provider"],
            payment["beneficiary"],
            payment.get("beneficiary_country"),
            payment.get("beneficiary_identifiers") or [],
        )
        basis = payment["payment_kind"]
        self.conn.execute(
            "INSERT INTO public_finance_payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                payment_id,
                series_key,
                payment["payment_key"],
                header["provider"],
                source_id,
                number,
                previous,
                payment["fiscal_year"],
                payment["payment_kind"],
                basis,
                payment["beneficiary"],
                key,
                line_id,
                payment.get("budget_line"),
                payment.get("programme"),
                payment.get("amount_text"),
                payment.get("amount"),
                payment["currency"],
                None
                if payment.get("amount") is None
                else canonical(
                    normalise_amount(payment["amount"], payment["currency"])
                ),
                canonical(statement),
                published,
                header.get("published_at"),
                content_hash,
                release_id,
                origin,
                retrieved,
            ],
        )
        return payment_id, True

    # ------------------------------------------------------------------ audit findings

    def import_findings(
        self,
        namespace: str,
        sheet: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an operator's finding sheet for one audit report: passages, quotes and cited lines, no verdict.

        Idempotent by sheet content; a changed passage is a new revision of that finding.
        """
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        found = forbidden_keys(dict(sheet))
        if found:
            raise PublicFinanceError(
                "verdict_refused",
                "a finding sheet carries no verdict or determination",
                keys=found,
            )
        report = dict(sheet.get("report") or {})
        findings = [dict(f) for f in sheet.get("findings") or []]
        if (
            not report.get("report_id")
            or not str(report.get("url") or "").startswith("https://")
            or not findings
        ):
            raise PublicFinanceError(
                "invalid_sheet",
                "a finding sheet names the report (id, https URL) and findings",
            )
        published = _day(report.get("published_on"))
        for finding in findings:
            passage = dict(finding.get("passage") or {})
            if (
                not passage.get("locator")
                or not str(finding.get("quote") or "").strip()
            ):
                raise PublicFinanceError(
                    "invalid_sheet",
                    "every finding cites a passage locator and quotes it",
                )
            for cite in finding.get("cites") or []:
                if cite.get("scheme") not in SCHEMES or not dict(
                    cite.get("codes") or {}
                ):
                    raise PublicFinanceError(
                        "invalid_sheet", "cited lines name a hierarchy scheme and codes"
                    )
        raw = canonical(dict(sheet)).encode()
        header = {
            "provider": "bundesrechnungshof",
            "format": "operator-finding-sheet",
            "document": {
                "report_id": report["report_id"],
                "label": report.get("title"),
            },
            "published_on": published,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(findings),
            "item_count": len(findings),
            "evidence_origin": str(sheet.get("evidence_origin") or "operator"),
            "url": report["url"],
        }
        retrieved = self.now()
        release_id = (
            "pf-release:"
            + digest([namespace, "bundesrechnungshof", header["file_sha256"]])[:24]
        )
        counts = {"revisions": 0, "findings": len(findings)}
        if self.conn.execute(
            "SELECT 1 FROM public_finance_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return {"release_id": release_id, "status": "unchanged", **counts}
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM public_finance_releases WHERE namespace=? AND provider=?",
            [namespace, "bundesrechnungshof"],
        ).fetchone()[0]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO public_finance_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    release_id,
                    "bundesrechnungshof",
                    None,
                    "operator-finding-sheet",
                    "findings",
                    canonical(header["document"]),
                    None,
                    published,
                    None,
                    header["file_sha256"],
                    header["content_sha256"],
                    len(findings),
                    None,
                    header["evidence_origin"],
                    report["url"],
                    int(sequence) + 1,
                    run_id or f"operator:{principal_id}",
                    retrieved,
                ],
            )
            for finding in findings:
                passage = dict(finding["passage"])
                key = (
                    "pf-finding-key:"
                    + digest([namespace, report["report_id"], passage["locator"]])[:24]
                )
                body = {
                    "quote": " ".join(str(finding["quote"]).split()),
                    "cites": sorted(
                        (dict(c) for c in finding.get("cites") or []), key=canonical
                    ),
                    "passage": passage,
                }
                content_hash = digest(body)
                current = self.conn.execute(
                    "SELECT finding_id, content_hash FROM public_finance_findings WHERE namespace=? AND finding_key=? "
                    "ORDER BY published_on DESC, revision_no DESC LIMIT 1",
                    [namespace, key],
                ).fetchone()
                if current and current[1] == content_hash:
                    continue
                number = 1 + int(
                    self.conn.execute(
                        "SELECT coalesce(max(revision_no), 0) FROM public_finance_findings WHERE namespace=? AND "
                        "finding_key=?",
                        [namespace, key],
                    ).fetchone()[0]
                )
                self.conn.execute(
                    "INSERT INTO public_finance_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        "pf-finding:"
                        + digest([namespace, key, number, release_id])[:24],
                        key,
                        report["report_id"],
                        number,
                        current[0] if current else None,
                        canonical(report),
                        canonical(passage),
                        body["quote"],
                        canonical(body["cites"]),
                        published,
                        content_hash,
                        release_id,
                        principal_id,
                        retrieved,
                    ],
                )
                counts["revisions"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", **counts}

    # ------------------------------------------------------------------ record views

    _RELEASE_KEYS = (
        "release_id",
        "provider",
        "source_id",
        "format",
        "record_type",
        "document",
        "accounting_basis",
        "published_on",
        "published_at",
        "file_sha256",
        "item_count",
        "unit",
        "evidence_origin",
        "url",
        "sequence",
        "run_id",
        "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                k if k != "document" else "document_json" for k in self._RELEASE_KEYS
            )
            + " FROM public_finance_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "release is not visible in this namespace"
            )
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = _load(view["document"], {})
        return {
            "contract": CONTRACT,
            "record_type": "release",
            "namespace": namespace,
            **view,
        }

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
                "url",
                "evidence_origin",
                "retrieved_at_ms",
            )
        } | {
            "document": release["document"].get("label")
            or release["document"].get("report_id")
        }

    def releases(
        self, namespace: str, *, provider: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT release_id FROM public_finance_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY published_on, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def line(self, namespace: str, line_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT line_id, provider, scheme, codes_json, labels_json, side, district_json, line_key, "
            "first_release_id FROM public_finance_lines WHERE namespace=? AND line_id=?",
            [namespace, line_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "budget line is not visible in this namespace"
            )
        return {
            "contract": CONTRACT,
            "record_type": "budget_line",
            "namespace": namespace,
            "line_id": row[0],
            "provider": row[1],
            "scheme": row[2],
            "codes": _load(row[3], {}),
            "labels": _load(row[4], {}),
            "side": row[5],
            "district": _load(row[6], None),
            "line_key": row[7],
            "first_release_id": row[8],
            "note": "codes and labels as the source first published them; later labels are on each figure revision",
        }

    def lines(
        self,
        namespace: str,
        *,
        scheme: str | None = None,
        codes: Mapping[str, Any] | None = None,
        label: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT line_id FROM public_finance_lines WHERE namespace=? AND (? IS NULL OR scheme=?) AND "
            "(? IS NULL OR provider=?) ORDER BY line_key, line_id",
            [namespace, scheme, scheme, provider, provider],
        ).fetchall()
        wanted = {
            k: code(v) for k, v in dict(codes or {}).items() if v not in (None, "")
        }
        out = []
        for (line_id,) in rows:
            view = self.line(namespace, line_id)
            if any(view["codes"].get(k) != v for k, v in wanted.items()):
                continue
            if label and not any(
                label.casefold() in str(v or "").casefold()
                for v in view["labels"].values()
            ):
                continue
            out.append(view)
            if len(out) >= limit:
                break
        return out

    def plan(self, namespace: str, plan_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT plan_id, provider, fiscal_year, figure_kind, plan_key, label, first_release_id FROM "
            "public_finance_plans WHERE namespace=? AND plan_id=?",
            [namespace, plan_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "budget plan is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "plan_id",
                    "provider",
                    "fiscal_year",
                    "figure_kind",
                    "plan_key",
                    "label",
                    "first_release_id",
                ),
                row,
            )
        )
        refs = []
        for (release_id,) in self.conn.execute(
            "SELECT DISTINCT m.release_id FROM public_finance_figure_members m JOIN public_finance_figures f ON "
            "f.namespace=m.namespace AND f.figure_id=m.figure_id WHERE f.namespace=? AND f.plan_id=? ORDER BY 1",
            [namespace, plan_id],
        ).fetchall():
            document = self.release(namespace, release_id)["document"]
            for ref in document.get("references") or []:
                refs.append(
                    {
                        **dict(ref),
                        "stated_in": self.source_revision(namespace, release_id),
                    }
                )
        view["references"] = refs
        return {
            "contract": CONTRACT,
            "record_type": "budget_plan",
            "namespace": namespace,
            **view,
        }

    def plans(
        self, namespace: str, *, provider: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT plan_id FROM public_finance_plans WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY fiscal_year, figure_kind DESC, plan_key",
            [namespace, provider, provider],
        ).fetchall()
        return [self.plan(namespace, r[0]) for r in rows]

    _FIGURE_KEYS = (
        "figure_id",
        "series_key",
        "line_id",
        "plan_id",
        "fiscal_year",
        "figure_kind",
        "plan_key",
        "provider",
        "source_id",
        "revision_no",
        "previous_figure_id",
        "published_on",
        "published_at",
        "accounting_basis",
        "amount_text",
        "amount",
        "unit",
        "currency",
        "normalized",
        "statement",
        "release_id",
        "evidence_origin",
        "retrieved_at_ms",
    )

    def figure(self, namespace: str, figure_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                {"normalized": "normalized_json", "statement": "statement_json"}.get(
                    k, k
                )
                for k in self._FIGURE_KEYS
            )
            + " FROM public_finance_figures WHERE namespace=? AND figure_id=?",
            [namespace, figure_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "figure is not visible in this namespace"
            )
        view = dict(zip(self._FIGURE_KEYS, row))
        view["normalized"] = _load(view["normalized"], None)
        view["statement"] = _load(view["statement"], {})
        view["labels"] = view["statement"].get("labels") or {}
        view["source_revision"] = self.source_revision(namespace, view["release_id"])
        view["note"] = (
            "amount as published with its unit and accounting basis; nothing recomputed"
        )
        return {
            "contract": CONTRACT,
            "record_type": "figure",
            "namespace": namespace,
            **view,
        }

    def figure_series(self, namespace: str, series_key: str) -> dict[str, Any]:
        """Every revision of one figure series in the source's date order, and every release that stated it."""
        rows = self.conn.execute(
            f"SELECT figure_id FROM public_finance_figures WHERE namespace=? AND series_key=? ORDER BY {SOURCE_ORDER}",
            [namespace, series_key],
        ).fetchall()
        revisions = [self.figure(namespace, r[0]) for r in reversed(rows)]
        vintages = []
        for release_id, figure_id, locator in self.conn.execute(
            "SELECT m.release_id, m.figure_id, m.locator_json FROM public_finance_figure_members m JOIN "
            "public_finance_releases r ON r.namespace=m.namespace AND r.release_id=m.release_id WHERE m.namespace=? "
            "AND m.series_key=? ORDER BY r.published_on, coalesce(r.published_at, ''), r.sequence",
            [namespace, series_key],
        ).fetchall():
            revision = next(v for v in revisions if v["figure_id"] == figure_id)
            vintages.append(
                {
                    "source_revision": self.source_revision(namespace, release_id),
                    "figure_id": figure_id,
                    "revision_no": revision["revision_no"],
                    "amount_text": revision["amount_text"],
                    "amount": revision["amount"],
                    "unit": revision["unit"],
                    "locator": _load(locator, {}),
                }
            )
        # Two revisions published on the same date that state different figures are a conflict, never resolved.
        by_day: dict[str, list[dict[str, Any]]] = {}
        for revision in revisions:
            by_day.setdefault(revision["published_on"], []).append(revision)
        conflicts = [
            {
                "published_on": day,
                "figures": [
                    {
                        "figure_id": r["figure_id"],
                        "amount_text": r["amount_text"],
                        "source_revision": r["source_revision"],
                    }
                    for r in group
                ],
                "note": "the source published different figures on the same date; both are kept, none is preferred",
            }
            for day, group in sorted(by_day.items())
            if len({r["amount"] for r in group}) > 1
        ]
        return {
            "series_key": series_key,
            "revisions": revisions,
            "vintages": vintages,
            "current": revisions[-1] if revisions else None,
            "conflicts": conflicts,
        }

    def series_for_line(
        self, namespace: str, line_id: str, *, fiscal_year: str | None = None
    ) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT series_key, fiscal_year, figure_kind, plan_key, coalesce(source_id, '') FROM "
                "public_finance_figures WHERE namespace=? AND line_id=? AND (? IS NULL OR fiscal_year=?) "
                "ORDER BY fiscal_year, CASE figure_kind WHEN 'plan' THEN 0 WHEN 'supplementary_plan' THEN 1 ELSE 2 END, "
                "plan_key, 5",
                [namespace, line_id, fiscal_year, fiscal_year],
            ).fetchall()
        ]

    _PAYMENT_KEYS = (
        "payment_id",
        "series_key",
        "payment_key",
        "provider",
        "source_id",
        "revision_no",
        "previous_payment_id",
        "fiscal_year",
        "payment_kind",
        "accounting_basis",
        "beneficiary",
        "beneficiary_key",
        "line_id",
        "budget_line",
        "programme",
        "amount_text",
        "amount",
        "currency",
        "normalized",
        "statement",
        "published_on",
        "release_id",
        "evidence_origin",
        "retrieved_at_ms",
    )

    def payment(self, namespace: str, payment_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                {"normalized": "normalized_json", "statement": "statement_json"}.get(
                    k, k
                )
                for k in self._PAYMENT_KEYS
            )
            + " FROM public_finance_payments WHERE namespace=? AND payment_id=?",
            [namespace, payment_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "payment is not visible in this namespace"
            )
        view = dict(zip(self._PAYMENT_KEYS, row))
        view["normalized"] = _load(view["normalized"], None)
        view["statement"] = _load(view["statement"], {})
        view["source_revision"] = self.source_revision(namespace, view["release_id"])
        view["beneficiary_entity"] = entity_id(view["beneficiary_key"])
        view["note"] = (
            "a published commitment or payment row; not a plan or outturn figure"
        )
        return {
            "contract": CONTRACT,
            "record_type": "beneficiary_payment",
            "namespace": namespace,
            **view,
        }

    def payments(
        self,
        namespace: str,
        *,
        beneficiary_key: str | None = None,
        beneficiary: str | None = None,
        programme: str | None = None,
        line_id: str | None = None,
        budget_line: str | None = None,
        fiscal_year: str | None = None,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payment_id, series_key FROM public_finance_payments WHERE namespace=? AND "
            "(? IS NULL OR beneficiary_key=?) AND (? IS NULL OR programme=?) AND (? IS NULL OR line_id=?) AND "
            "(? IS NULL OR budget_line=?) AND (? IS NULL OR fiscal_year=?) "
            f"ORDER BY series_key, {SOURCE_ORDER}",
            [
                namespace,
                beneficiary_key,
                beneficiary_key,
                programme,
                programme,
                line_id,
                line_id,
                code(budget_line) if budget_line else None,
                code(budget_line) if budget_line else None,
                fiscal_year,
                fiscal_year,
            ],
        ).fetchall()
        seen, out = set(), []
        for payment_id, series_key in rows:
            if current_only and series_key in seen:
                continue
            seen.add(series_key)
            view = self.payment(namespace, payment_id)
            if beneficiary and normalize_name(beneficiary) != normalize_name(
                view["beneficiary"]
            ):
                continue
            out.append(view)
        return sorted(
            out,
            key=lambda p: (
                p["fiscal_year"],
                p["beneficiary"],
                p["payment_key"],
                p["revision_no"],
            ),
        )

    def payment_history(self, namespace: str, series_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT payment_id FROM public_finance_payments WHERE namespace=? AND series_key=? ORDER BY {SOURCE_ORDER}",
            [namespace, series_key],
        ).fetchall()
        return [self.payment(namespace, r[0]) for r in reversed(rows)]

    def beneficiaries(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT DISTINCT beneficiary_key FROM public_finance_payments WHERE namespace=? ORDER BY 1",
            [namespace],
        ).fetchall()
        out = []
        for (key,) in rows:
            latest = self.payments(namespace, beneficiary_key=key)
            statement = latest[-1]["statement"] if latest else {}
            out.append(
                {
                    "beneficiary_key": key,
                    "entity_id": entity_id(key),
                    "name": statement.get("beneficiary"),
                    "country": statement.get("beneficiary_country"),
                    "identifiers": statement.get("beneficiary_identifiers") or [],
                    "provider": latest[-1]["provider"] if latest else None,
                    "payments": [p["payment_id"] for p in latest],
                }
            )
        return out

    def finding(self, namespace: str, finding_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT finding_id, finding_key, report_id, revision_no, previous_finding_id, report_json, passage_json, "
            "quote, cites_json, published_on, release_id, recorded_by FROM public_finance_findings WHERE namespace=? "
            "AND finding_id=?",
            [namespace, finding_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "finding is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "finding_id",
                    "finding_key",
                    "report_id",
                    "revision_no",
                    "previous_finding_id",
                    "report",
                    "passage",
                    "quote",
                    "cites",
                    "published_on",
                    "release_id",
                    "recorded_by",
                ),
                row,
            )
        )
        for key in ("report", "passage", "cites"):
            view[key] = _load(view[key], {} if key != "cites" else [])
        view["source_revision"] = self.source_revision(namespace, view["release_id"])
        view["note"] = (
            "a quoted report passage and the lines it cites; the record states no verdict"
        )
        return {
            "contract": CONTRACT,
            "record_type": "audit_finding",
            "namespace": namespace,
            **view,
        }

    def findings(
        self,
        namespace: str,
        *,
        line_id: str | None = None,
        report_id: str | None = None,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT finding_id, finding_key FROM public_finance_findings WHERE namespace=? AND "
            "(? IS NULL OR report_id=?) ORDER BY finding_key, published_on DESC, revision_no DESC",
            [namespace, report_id, report_id],
        ).fetchall()
        line = None if line_id is None else self.line(namespace, line_id)
        seen, out = set(), []
        for finding_id, key in rows:
            if current_only and key in seen:
                continue
            seen.add(key)
            view = self.finding(namespace, finding_id)
            if line is not None:
                matches = [c for c in view["cites"] if cite_matches(c, line)]
                if not matches:
                    continue
                view["cites_line"] = {
                    "line_id": line_id,
                    "levels": sorted({lvl for c in matches for lvl in c["codes"]}),
                }
            out.append(view)
        return out


def cite_matches(cite: Mapping[str, Any], line: Mapping[str, Any]) -> bool:
    """A cite matches a line when every code it states equals the line's code (a Kapitel cite covers its Titel)."""
    if cite.get("scheme") != line["scheme"]:
        return False
    stated = {
        k: code(v)
        for k, v in dict(cite.get("codes") or {}).items()
        if v not in (None, "")
    }
    return bool(stated) and all(line["codes"].get(k) == v for k, v in stated.items())


class PublicFinanceProjector:
    """Source-pack runtime projector for ``noesis-public-finance-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = PublicFinanceStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("public_finance") or {}).get("namespace")
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
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = (
                dict(item.get("public_finance_release") or {}),
                item.get("public_finance_item"),
            )
            if not header or not isinstance(body, Mapping):
                raise PublicFinanceError(
                    "invalid_record", "page record is not a public-finance item"
                )
            groups.setdefault(
                header["file_sha256"] + canonical(header.get("document")), (header, [])
            )[1].append(dict(body))
        results = []
        namespace = self._namespace(source)
        for header, items in groups.values():
            kind = header.get("record_type")
            if kind == "figures":
                results.append(
                    self.store.apply_figures(
                        namespace,
                        header,
                        items,
                        run_id=run_id,
                        source_id=source["source_id"],
                    )
                )
            elif kind == "payments":
                results.append(
                    self.store.apply_payments(
                        namespace,
                        header,
                        items,
                        run_id=run_id,
                        source_id=source["source_id"],
                    )
                )
            elif kind == "series":
                from src.kb.public_finance_gfs import GovernmentFinanceStatistics

                results.append(
                    GovernmentFinanceStatistics(
                        self.store.conn, now=self.store.now
                    ).apply_series(
                        namespace,
                        header,
                        items,
                        run_id=run_id,
                        source_id=source["source_id"],
                    )
                )
            else:
                raise PublicFinanceError(
                    "invalid_record", f"unknown public-finance record type {kind!r}"
                )
        return results

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM public_finance_releases WHERE namespace=? AND source_id=? "
            "ORDER BY published_on DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_release_id": row[0] if row else None,
            "latest_published_on": row[1] if row else None,
        }


def readiness(conn: Any) -> dict[str, Any]:
    store = PublicFinanceStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "public_finance_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(
                conn.execute(
                    "SELECT count(*) FROM public_finance_releases WHERE provider=?",
                    [provider],
                ).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "releases": releases,
            "live": "not-implemented"
            if contract["access_decision"] == "not-implemented"
            else "outstanding",
        }
    return {
        "feature": "economics.public-finance",
        "enabled": feature_enabled(conn),
        "store_ready": ready,
        "providers": providers,
        "review_boundary": REVIEW_BOUNDARY,
    }

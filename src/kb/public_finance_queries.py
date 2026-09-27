"""Plan against outturn, vintage against vintage, and cited budget dossiers (#1909, B08).

A comparison takes one budget line and fiscal year and lays out the plan, each
supplementary plan and each outturn vintage side by side, every figure with its
source revision, publication date, unit and accounting basis. The citation and
vintage-comparison shapes follow :mod:`src.domains.economic.queries`
(``_citation`` and ``_vintage_comparison``) rather than a new engine.

A difference is computed only between two known figures on the same accounting
basis and in the same currency, from their pint-normalised values. Figures on
different bases (commitments against payments, a cash plan against an ESA 2010
series) are shown side by side with an explicit note and no difference, unless
a reconciliation method the source publishes is recorded with its citation.
Figures from different sources for the same line, kind and date that disagree
are kept side by side and flagged; nothing is averaged or preferred. Nothing
here forecasts an outcome or judges spending.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.public_finance_sources import REVIEW_BOUNDARY
from src.kb.public_finance import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    PublicFinanceError,
    PublicFinanceStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

ECONOMIC_READ = "knowledge:economic:read"
_DDL = """
CREATE TABLE IF NOT EXISTS public_finance_reconciliations (
  namespace TEXT NOT NULL, method_id TEXT NOT NULL, provider TEXT NOT NULL, from_basis TEXT NOT NULL,
  to_basis TEXT NOT NULL, citation_json TEXT NOT NULL, description TEXT NOT NULL, recorded_by TEXT NOT NULL,
  recorded_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, method_id)
);
"""
KIND_ORDER = {"plan": 0, "supplementary_plan": 1, "outturn": 2}


def citation(figure: Mapping[str, Any]) -> dict[str, Any]:
    """The economic ``_citation`` shape for one figure or payment revision."""
    revision = figure["source_revision"]
    return {
        "record_id": figure.get("figure_id") or figure.get("payment_id"),
        "provider": revision["provider"],
        "source_id": revision["source_id"],
        "source_url": revision["url"],
        "release_id": revision["release_id"],
        "document": revision["document"],
        "file_sha256": revision["file_sha256"],
        "provider_vintage": revision["published_on"],
        "retrieved_at_ms": revision["retrieved_at_ms"],
        "evidence_origin": revision["evidence_origin"],
        "locator_available": bool(revision["url"]),
    }


def _entry(figure: Mapping[str, Any]) -> dict[str, Any]:
    normalized = figure.get("normalized") or {}
    return {
        "figure_id": figure["figure_id"],
        "figure_kind": figure["figure_kind"],
        "plan_key": figure["plan_key"],
        "revision_no": figure["revision_no"],
        "amount_text": figure["amount_text"],
        "amount": figure["amount"],
        "unit": figure["unit"],
        "currency": figure["currency"],
        "normalized_value": normalized.get("value"),
        "normalization": normalized.get("method"),
        "accounting_basis": figure["accounting_basis"],
        "published_on": figure["published_on"],
        "labels": figure["labels"],
        "citation": citation(figure),
    }


class PublicFinanceQueries:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.store = PublicFinanceStore(conn, initialize=initialize, now=now)
        self.conn, self.now = conn, self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ reconciliation methods

    def record_reconciliation(
        self,
        namespace: str,
        *,
        provider: str,
        from_basis: str,
        to_basis: str,
        citation: Mapping[str, Any],
        description: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Record a reconciliation method the source itself publishes (cited); nothing is inferred without one."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if not str(dict(citation).get("url") or "").startswith("https://") or not dict(
            citation
        ).get("locator"):
            raise PublicFinanceError(
                "invalid_method",
                "a reconciliation method cites the source (https URL, locator)",
            )
        if from_basis == to_basis or not str(description or "").strip():
            raise PublicFinanceError(
                "invalid_method", "a method relates two different bases and says how"
            )
        method_id = (
            "pf-method:"
            + digest(
                [namespace, provider, sorted((from_basis, to_basis)), dict(citation)]
            )[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO public_finance_reconciliations VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                method_id,
                provider,
                from_basis,
                to_basis,
                canonical(dict(citation)),
                description.strip(),
                principal_id,
                self.now(),
            ],
        )
        return {
            "method_id": method_id,
            "provider": provider,
            "bases": sorted((from_basis, to_basis)),
        }

    def _method(
        self, namespace: str, provider: str, a: str, b: str
    ) -> dict[str, Any] | None:
        if not table_exists(self.conn, "public_finance_reconciliations"):
            return None
        row = self.conn.execute(
            "SELECT method_id, citation_json, description FROM public_finance_reconciliations WHERE namespace=? AND "
            "provider=? AND ((from_basis=? AND to_basis=?) OR (from_basis=? AND to_basis=?)) ORDER BY method_id "
            "LIMIT 1",
            [namespace, provider, a, b, b, a],
        ).fetchone()
        return (
            None
            if row is None
            else {
                "method_id": row[0],
                "citation": json.loads(row[1]),
                "description": row[2],
            }
        )

    def _difference(
        self,
        namespace: str,
        provider: str,
        left: Mapping[str, Any],
        right: Mapping[str, Any],
        label: str,
    ) -> dict[str, Any]:
        """``right - left`` when both are known and comparable; otherwise the reason there is none."""
        pair = {
            "comparison": label,
            "left": left["citation"]["record_id"],
            "right": right["citation"]["record_id"],
            "left_basis": left["accounting_basis"],
            "right_basis": right["accounting_basis"],
        }
        if left["normalized_value"] is None or right["normalized_value"] is None:
            return {
                **pair,
                "difference": None,
                "status": "not-computed",
                "note": "at least one figure is not published (unknown stays unknown)",
            }
        if left["currency"] != right["currency"]:
            return {
                **pair,
                "difference": None,
                "status": "not-computed",
                "note": "different currencies are shown side by side; no exchange rate is applied",
            }
        method = None
        if left["accounting_basis"] != right["accounting_basis"]:
            method = self._method(
                namespace, provider, left["accounting_basis"], right["accounting_basis"]
            )
            if method is None:
                return {
                    **pair,
                    "difference": None,
                    "status": "different-bases",
                    "note": f"{left['accounting_basis']} and {right['accounting_basis']} figures are shown side "
                    "by side; no difference without a cited reconciliation method",
                }
        value = Decimal(right["normalized_value"]) - Decimal(left["normalized_value"])
        return {
            **pair,
            "difference": str(value),
            "currency": right["currency"],
            "status": "computed" if method is None else "computed-under-cited-method",
            "method": method
            or {
                "description": "difference of pint-normalised amounts on the same accounting "
                "basis and currency"
            },
        }

    # ------------------------------------------------------------------ comparisons

    def compare_budget_line(
        self,
        namespace: str,
        line_id: str,
        fiscal_year: str,
        *,
        scopes: Iterable[str],
        gfs_series_id: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        line = self.store.line(namespace, line_id)
        series = [
            self.store.figure_series(namespace, key)
            for key in self.store.series_for_line(
                namespace, line_id, fiscal_year=str(fiscal_year)
            )
        ]
        columns = []
        for item in series:
            current = item["current"]
            columns.append(
                {
                    "figure_kind": current["figure_kind"],
                    "plan_key": current["plan_key"],
                    "source_id": current["source_id"],
                    "current": _entry(current),
                    "revisions": [_entry(r) for r in item["revisions"]],
                    "vintages": item["vintages"],
                    "conflicts": item["conflicts"],
                }
            )
        columns.sort(
            key=lambda c: (
                KIND_ORDER[c["figure_kind"]],
                c["plan_key"],
                c["source_id"] or "",
            )
        )
        plans = [c for c in columns if c["figure_kind"] != "outturn"]
        outturns = [c for c in columns if c["figure_kind"] == "outturn"]
        differences = []
        for outturn in outturns:
            for plan in plans:
                differences.append(
                    self._difference(
                        namespace,
                        line["provider"],
                        plan["current"],
                        outturn["current"],
                        f"{plan['plan_key']} -> outturn",
                    )
                )
        vintage_comparison = []
        for outturn in outturns:
            revisions = outturn["revisions"]
            initial, latest = revisions[0], revisions[-1]
            revision = self._difference(
                namespace,
                line["provider"],
                initial,
                latest,
                "initial -> latest vintage",
            )
            vintage_comparison.append(
                {
                    "period": str(fiscal_year),
                    "initial_value": initial["normalized_value"],
                    "latest_value": latest["normalized_value"],
                    "revision": revision["difference"],
                    "initial_vintage": initial["citation"],
                    "latest_vintage": latest["citation"],
                    "vintages": outturn["vintages"],
                    "unit": latest["currency"],
                }
            )
        conflicts = [c for column in columns for c in column["conflicts"]]
        # Different sources stating the same kind of figure on the same date and disagreeing: flagged, not merged.
        by_kind: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for column in columns:
            for revision in column["revisions"]:
                by_kind.setdefault(
                    (
                        column["figure_kind"],
                        column["plan_key"],
                        revision["published_on"],
                    ),
                    [],
                ).append(revision)
        for (kind, plan_key, day), group in sorted(by_kind.items()):
            sources = {r["citation"]["source_id"] for r in group}
            if len(sources) > 1 and len({r["amount"] for r in group}) > 1:
                conflicts.append(
                    {
                        "figure_kind": kind,
                        "plan_key": plan_key,
                        "published_on": day,
                        "figures": [
                            {"amount_text": r["amount_text"], "citation": r["citation"]}
                            for r in group
                        ],
                        "note": "sources disagree for the same line, kind and date; none is preferred",
                    }
                )
        unknowns = []
        if not plans:
            unknowns.append("no plan figure acquired for this line and year")
        if not outturns:
            unknowns.append("no outturn published or acquired for this line and year")
        unknowns += [
            f"{c['plan_key']}: amount not published"
            for c in columns
            if c["current"]["amount"] is None
        ]
        answer = {
            "contract": ANSWER_CONTRACT,
            "line": line,
            "fiscal_year": str(fiscal_year),
            "columns": columns,
            "differences": differences,
            "vintage_comparison": vintage_comparison,
            "conflicts": conflicts,
            "unknowns": unknowns,
            "citations": [c["current"]["citation"] for c in columns],
            "review_boundary": REVIEW_BOUNDARY,
        }
        if line["scheme"] == "eu-budget-line":
            answer["payments"] = self._payment_bases(namespace, line, str(fiscal_year))
        if gfs_series_id:
            require_scope(scopes, ECONOMIC_READ)
            answer["esa2010_context"] = self._gfs_context(
                namespace, gfs_series_id, str(fiscal_year), columns
            )
        return answer

    def _payment_bases(
        self, namespace: str, line: Mapping[str, Any], year: str
    ) -> dict[str, Any]:
        """Commitments and payments of one position side by side; a difference only under a cited method."""
        payments = self.store.payments(
            namespace, line_id=line["line_id"], fiscal_year=year
        )
        by_position: dict[str, dict[str, dict[str, Any]]] = {}
        for payment in payments:
            normalized = payment.get("normalized") or {}
            entry = {
                "normalized_value": normalized.get("value"),
                "currency": payment["currency"],
                "accounting_basis": payment["accounting_basis"],
                "amount_text": payment["amount_text"],
                "beneficiary": payment["beneficiary"],
                "citation": citation(payment),
            }
            by_position.setdefault(payment["payment_key"], {})[
                payment["payment_kind"]
            ] = entry
        pairs = []
        for key, kinds in sorted(by_position.items()):
            if {"commitment", "payment"} <= set(kinds):
                pairs.append(
                    {
                        "position": key,
                        "commitment": kinds["commitment"],
                        "payment": kinds["payment"],
                        **self._difference(
                            namespace,
                            line["provider"],
                            kinds["commitment"],
                            kinds["payment"],
                            "commitment -> payment",
                        ),
                    }
                )
        return {
            "payments": [
                {
                    "payment_key": p["payment_key"],
                    "payment_kind": p["payment_kind"],
                    "beneficiary": p["beneficiary"],
                    "amount_text": p["amount_text"],
                    "accounting_basis": p["accounting_basis"],
                    "citation": citation(p),
                }
                for p in payments
            ],
            "commitment_payment_pairs": pairs,
            "note": "commitments and payments are different bases; each row is a published record, not a total",
        }

    def _gfs_context(self, namespace, series_id, year, columns) -> dict[str, Any]:
        from src.kb.public_finance_gfs import GovernmentFinanceStatistics

        entries = [
            s
            for s in GovernmentFinanceStatistics(self.conn, now=self.now).series(
                namespace, scopes={"operator"}
            )
            if s["series_id"] == series_id
        ]
        if not entries:
            raise PublicFinanceError("not_found", "the GFS series was not acquired")
        entry = entries[0]
        latest = entry["vintages"][-1]
        row = self.conn.execute(
            "SELECT value FROM dataset_observations WHERE series_id=? AND as_of=? AND period=?",
            [series_id, latest["as_of"], year],
        ).fetchone()
        return {
            "series_id": series_id,
            "accounting_basis": "esa2010",
            "period": year,
            "value": None if row is None else row[0],
            "unit": entry["dimensions"].get("unit"),
            "vintage": latest,
            "budget_bases": sorted({c["current"]["accounting_basis"] for c in columns}),
            "difference": None,
            "note": "an ESA 2010 national-accounts aggregate beside cash-basis budget figures: different bases and "
            "scopes, shown side by side, never netted",
        }

    # ------------------------------------------------------------------ inspection and dossiers

    def inspect_budget_line(
        self, namespace: str, line_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        line = self.store.line(namespace, line_id)
        years = sorted(
            {
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT fiscal_year FROM public_finance_figures WHERE namespace=? AND line_id=?",
                    [namespace, line_id],
                ).fetchall()
            }
            | {
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT fiscal_year FROM public_finance_payments WHERE namespace=? AND line_id=?",
                    [namespace, line_id],
                ).fetchall()
            }
        )
        history = {}
        for year in years:
            compared = self.compare_budget_line(namespace, line_id, year, scopes=scopes)
            history[year] = {
                k: compared[k]
                for k in (
                    "columns",
                    "differences",
                    "vintage_comparison",
                    "conflicts",
                    "unknowns",
                )
            }
            if "payments" in compared:
                history[year]["payments"] = compared["payments"]
        findings = self.store.findings(namespace, line_id=line_id)
        return {
            "contract": ANSWER_CONTRACT,
            "line": line,
            "fiscal_years": years,
            "history": history,
            "findings": findings,
            "citations": [
                c["current"]["citation"] for y in history.values() for c in y["columns"]
            ],
            "review_boundary": REVIEW_BOUNDARY,
        }

    def budget_dossier(
        self,
        namespace: str,
        line_id: str,
        *,
        scopes: Iterable[str],
        procurement_namespace: str | None = None,
    ) -> dict[str, Any]:
        """A line's cited dossier: figures by year, payments, findings, acts, dossiers, district and award context.

        Conditional scope: ``procurement_namespace`` needs ``knowledge:procurement:read``.
        """
        from src.kb.public_finance_identity import PublicFinanceIdentity
        from src.kb.public_finance_links import PublicFinanceLinks
        from src.kb.public_finance_places import PublicFinancePlaces

        scopes = set(scopes)
        inspected = self.inspect_budget_line(namespace, line_id, scopes=scopes)
        line = inspected["line"]
        plan_ids = sorted(
            {
                self.store.figure(namespace, c["current"]["figure_id"])["plan_id"]
                for y in inspected["history"].values()
                for c in y["columns"]
            }
            - {None}
        )
        links = PublicFinanceLinks(self.conn, now=self.now)
        acts, dossiers = [], []
        for plan_id in plan_ids:
            for link in links.links(namespace, scopes=scopes, subject_id=plan_id):
                (acts if link["target_kind"] == "legal-work" else dossiers).append(link)
        payments = self.store.payments(namespace, line_id=line_id)
        identity = PublicFinanceIdentity(self.conn, now=self.now)
        beneficiaries = []
        for key in sorted({p["beneficiary_key"] for p in payments}):
            entry = {
                "beneficiary_key": key,
                "identity": identity.identity(namespace, key, scopes=scopes),
            }
            if procurement_namespace:
                entry["award_context"] = links.award_context(
                    namespace, key, procurement_namespace, scopes=scopes
                )
            beneficiaries.append(entry)
        place = (
            PublicFinancePlaces(self.conn, now=self.now).place(
                namespace, line_id, scopes=scopes
            )
            if line["scheme"] == "de-be-haushalt"
            else None
        )
        unknowns = [
            u
            for y, h in inspected["history"].items()
            for u in (f"{y}: {x}" for x in h["unknowns"])
        ]
        if not payments:
            unknowns.append(
                "no acquired source publishes beneficiary payments against this line"
            )
        if not inspected["findings"]:
            unknowns.append("no audit finding recorded that cites this line")
        unknowns += [
            f"{b['beneficiary_key']}: no reviewed identity (shown as the source string)"
            for b in beneficiaries
            if b["identity"]["state"] == "unmatched"
        ]
        unknowns += [
            f"cited act {a['reference']['value']} is not acquired"
            for a in acts
            if a["state"] == "unresolved"
        ]
        return {
            **inspected,
            "acts": acts,
            "dossiers": dossiers,
            "payments": payments,
            "beneficiaries": beneficiaries,
            "place": place,
            "unknowns": unknowns,
            "review_boundary": REVIEW_BOUNDARY,
        }

    def beneficiary_dossier(
        self,
        namespace: str,
        beneficiary_key: str,
        *,
        scopes: Iterable[str],
        procurement_namespace: str | None = None,
    ) -> dict[str, Any]:
        """A beneficiary's payments, grants, awards (as context) and reviewed corporate identity.

        Conditional scope: ``procurement_namespace`` needs ``knowledge:procurement:read``.
        """
        from src.kb.public_finance_identity import PublicFinanceIdentity
        from src.kb.public_finance_links import PublicFinanceLinks

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        payments = self.store.payments(namespace, beneficiary_key=beneficiary_key)
        if not payments:
            raise PublicFinanceError(
                "not_found", "no payment names this beneficiary in this namespace"
            )
        identity = PublicFinanceIdentity(self.conn, now=self.now)
        view = identity.identity(namespace, beneficiary_key, scopes=scopes)
        candidates = identity.candidates(
            namespace, scopes=scopes, record_key=beneficiary_key
        )
        grants = [
            c for c in candidates if any(r.startswith("funding:") for r in c["records"])
        ]
        answer = {
            "contract": ANSWER_CONTRACT,
            "beneficiary_key": beneficiary_key,
            "beneficiary": payments[-1]["beneficiary"],
            "payments": payments,
            "identity": view,
            "candidates": candidates,
            "grants": grants,
            "citations": [citation(p) for p in payments],
            "unknowns": (
                []
                if view["state"] == "matched"
                else [
                    "no reviewed identity: the beneficiary is shown as the source string"
                ]
            ),
            "review_boundary": REVIEW_BOUNDARY,
        }
        if procurement_namespace:
            answer["award_context"] = PublicFinanceLinks(
                self.conn, now=self.now
            ).award_context(
                namespace, beneficiary_key, procurement_namespace, scopes=scopes
            )
        return answer

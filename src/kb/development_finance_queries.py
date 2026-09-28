"""Who funded what where, as of a date, with cited activities and publisher coverage (#1932, D08).

Every answer selects each publisher's revision in force *as of* the requested
date first (revisions observed on or before it, ordered by the publisher's own
``last-updated-datetime``) and only then filters by funder, recipient country or
region, sector or organisation. Activities are listed per publisher and never
totalled across publishers; a within-publisher total states the transaction
type, currency and value-date range it sums and lists what it left out.
Conflicting values for one IATI identifier are shown side by side; results are
listed as reported baselines, targets and actuals with no achievement
judgement; CRS aggregates appear separately with the vintage used. Each answer
states per-publisher coverage and its unknowns (stale or partial coverage,
unmatched organisations, unresolved places) and is stored as a receipt that a
research project can pin and an authored report can cite.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.development_finance_sources import (
    NEVER_SENTENCE,
    identifier_key,
    world_bank_link_evidence,
)
from src.kb.development_finance import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    as_of_ms,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    name_key,
    table_exists,
)

_DDL = """
CREATE TABLE IF NOT EXISTS devfin_answers (
  namespace TEXT NOT NULL, answer_id TEXT NOT NULL, kind TEXT NOT NULL, request_json TEXT NOT NULL,
  as_of TEXT, generation BIGINT NOT NULL, answer_sha256 TEXT NOT NULL, coverage_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, answer_id)
);
"""
FUNDING_ROLES = {"1"}
PROJECTS_WRITE = "knowledge:projects:write"
REQUEST_KEYS = frozenset(
    {
        "funder",
        "organisation",
        "country",
        "region",
        "sector",
        "publisher",
        "iati_identifier",
        "transaction_type",
        "crs_recipient",
        "crs_donor",
        "as_of",
    }
)


def _sector_matches(code: Any, wanted: str) -> bool:
    text = str(code or "")
    # A 3-digit DAC category also matches the 5-digit purpose codes it groups (the code structure).
    return text == wanted or (
        len(wanted) == 3 and len(text) == 5 and text.startswith(wanted)
    )


class DevelopmentFinanceQueries:
    def __init__(self, conn: Any, *, now=None, record: bool = True) -> None:
        """``record=False`` computes each answer's receipt without storing it (a read-only connection)."""
        self.conn = conn
        self.store = DevelopmentFinanceStore(conn, initialize=False, now=now)
        self.now = self.store.now
        self.record = record

    # ------------------------------------------------------------------ helpers

    def _identity(self):
        from src.kb.development_finance_identity import DevelopmentFinanceIdentity

        return DevelopmentFinanceIdentity(self.conn, now=self.now, initialize=False)

    def _organisation_keys(
        self, namespace: str, value: str | None, scopes: set[str]
    ) -> set[str] | None:
        """Subjects an organisation argument names: a subject key, a reviewed target, or a reference or name."""
        if not value:
            return None
        from src.kb.development_finance_identity import KEY_PREFIX

        # IATI organisation identifiers never contain a colon; record keys (devfin:, canonical:, funding-funder:,
        # ownership keys) always do.
        if ":" in value:
            if table_exists(self.conn, "ownership_identity_candidates"):
                return set(
                    self._identity().linked_subjects(namespace, value, scopes=scopes)
                )
            return {value} if value.startswith(KEY_PREFIX + "org:") else set()
        return {"ref:" + identifier_key(value), "name:" + name_key(value)}

    @staticmethod
    def _org_matches(
        publisher: str, org: Mapping[str, Any] | None, wanted: set[str]
    ) -> bool:
        if not org:
            return False
        from src.kb.development_finance_identity import subject_key

        if org.get("ref") and "ref:" + identifier_key(org["ref"]) in wanted:
            return True
        if org.get("name") and "name:" + name_key(org["name"]) in wanted:
            return True
        return subject_key(publisher, org.get("ref"), org.get("name")) in wanted

    def _matches(
        self,
        revision: Mapping[str, Any],
        request: Mapping[str, Any],
        orgs: Mapping[str, set[str] | None],
    ):
        activity, publisher = revision["activity"], revision["publisher_id"]
        if (
            request.get("publisher")
            and publisher != request["publisher"]
            and identifier_key((activity.get("reporting_org") or {}).get("ref"))
            != identifier_key(request["publisher"])
        ):
            return False
        if request.get("iati_identifier") and identifier_key(
            revision["iati_identifier"]
        ) != identifier_key(request["iati_identifier"]):
            return False
        if request.get("country"):
            wanted = str(request["country"]).strip().upper()
            codes = {
                str(c.get("code") or "").upper()
                for c in activity.get("recipient_countries") or []
            }
            codes |= {
                str(c.get("code") or "").upper()
                for tx in activity.get("transactions") or []
                for c in tx.get("recipient_countries") or []
            }
            if wanted not in codes:
                return False
        if request.get("region"):
            codes = {
                str(r.get("code") or "")
                for r in activity.get("recipient_regions") or []
            }
            if str(request["region"]) not in codes:
                return False
        if request.get("sector"):
            codes = [s.get("code") for s in activity.get("sectors") or []]
            codes += [
                s.get("code")
                for tx in activity.get("transactions") or []
                for s in tx.get("sectors") or []
            ]
            if not any(_sector_matches(c, str(request["sector"])) for c in codes):
                return False
        if orgs.get("funder") is not None:
            wanted = orgs["funder"]
            funders = [
                o
                for o in activity.get("participating_orgs") or []
                if o.get("role") in FUNDING_ROLES
            ]
            funders += [
                tx.get("provider_org") for tx in activity.get("transactions") or []
            ]
            if not any(self._org_matches(publisher, o, wanted) for o in funders):
                return False
        if orgs.get("organisation") is not None:
            wanted = orgs["organisation"]
            named = list(activity.get("participating_orgs") or []) + [
                activity.get("reporting_org")
            ]
            named += [
                tx.get(k)
                for tx in activity.get("transactions") or []
                for k in ("provider_org", "receiver_org")
            ]
            if not any(self._org_matches(publisher, o, wanted) for o in named):
                return False
        return True

    def _selected(self, namespace: str, request: Mapping[str, Any], scopes: set[str]):
        at = as_of_ms(request.get("as_of"))
        orgs = {
            "funder": self._organisation_keys(namespace, request.get("funder"), scopes),
            "organisation": self._organisation_keys(
                namespace, request.get("organisation"), scopes
            ),
        }
        current = self.store.current(
            namespace, as_of=at
        )  # per publisher, before any filtering
        return at, [r for r in current.values() if self._matches(r, request, orgs)]

    def _coverage(
        self, namespace: str, publishers: Iterable[str], at: int | None
    ) -> dict[str, Any]:
        latest = self.store.latest_coverage(namespace, as_of=at)
        wanted = set(publishers)
        per_publisher: dict[str, dict[str, Any]] = {}
        for row in latest.values():
            requested = {
                identifier_key(p) for p in (row["requested"].get("publishers") or [])
            }
            for publisher in sorted(wanted):
                ref = publisher.split(":ref:", 1)[1] if ":ref:" in publisher else None
                if publisher not in row["returned"] and (
                    ref is None or ref not in requested
                ):
                    continue
                entry = per_publisher.setdefault(
                    publisher, {"publisher_id": publisher, "selections": []}
                )
                entry["selections"].append(
                    {
                        "coverage_id": row["coverage_id"],
                        "provider": row["provider"],
                        "requested": row["requested"],
                        "returned": row["returned"].get(publisher, 0),
                        "pages_read": row["pages_read"],
                        "stop_reason": row["stop_reason"],
                        "complete": row["complete"],
                        "failure_code": row["failure_code"],
                        "observed_at": iso_from_ms(row["observed_at_ms"]),
                        "stale": row["failure_code"] is not None or not row["complete"],
                    }
                )
        for publisher in sorted(wanted):
            entry = per_publisher.setdefault(
                publisher, {"publisher_id": publisher, "selections": []}
            )
            entry["stale"] = (
                any(s["stale"] for s in entry["selections"]) or not entry["selections"]
            )
            entry["state"] = (
                "no coverage record"
                if not entry["selections"]
                else "stale or partial"
                if entry["stale"]
                else "complete for its selections"
            )
            entry["bounded"] = (
                "coverage is limited to the selections listed; no record implies complete coverage"
            )
        return per_publisher

    def _unknowns(
        self, namespace: str, revisions, coverage, scopes: set[str]
    ) -> dict[str, Any]:
        unknowns: dict[str, Any] = {
            "stale_publishers": sorted(p for p, c in coverage.items() if c["stale"]),
            "unmatched_organisations": [],
            "unresolved_places": [],
            "unknown_amounts": 0,
        }
        if table_exists(self.conn, "ownership_identity_candidates"):
            identity = self._identity()
            from src.kb.development_finance_identity import subject_key

            seen = set()
            for revision in revisions:
                for org in revision["activity"].get("participating_orgs") or []:
                    if not (org.get("ref") or org.get("name")):
                        continue
                    key = subject_key(
                        revision["publisher_id"], org.get("ref"), org.get("name")
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    state = identity.identity(namespace, key, scopes=scopes)["state"]
                    if state != "matched":
                        unknowns["unmatched_organisations"].append(
                            {
                                "record_key": key,
                                "as_reported": org.get("name") or org.get("ref"),
                                "state": state,
                            }
                        )
        else:
            unknowns["unmatched_organisations"] = "identity review has not run"
        if table_exists(self.conn, "devfin_place_links"):
            unknowns["unresolved_places"] = [
                {"reference_kind": r[0], "code": r[1], "mention": r[2], "state": r[3]}
                for r in self.conn.execute(
                    "SELECT DISTINCT reference_kind, code, mention, state FROM devfin_place_links WHERE namespace=? "
                    "AND state NOT IN ('resolved') ORDER BY ALL",
                    [namespace],
                ).fetchall()
            ]
        unknowns["unknown_amounts"] = sum(
            1
            for r in revisions
            for tx in r["transactions"]
            if tx["amount_state"] == "unknown"
        )
        return unknowns

    @staticmethod
    def _cite(revision: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"],
            "publisher_id": revision["publisher_id"],
            "dataset_id": revision["dataset_id"],
            "capture_sha256": revision["capture_sha256"],
            "source_url": revision["source_url"],
            "last_updated_at": revision["last_updated_at"],
            "observed_at": iso_from_ms(revision["observed_at_ms"]),
        }

    def _summary(
        self, namespace: str, revision: Mapping[str, Any], at: int | None
    ) -> dict[str, Any]:
        activity = revision["activity"]
        return {
            "activity_key": revision["activity_key"],
            "iati_identifier": revision["iati_identifier"],
            "publisher_id": revision["publisher_id"],
            "title": activity.get("title"),
            "activity_status": activity.get("activity_status"),
            "publication": self.store.publication_state(
                namespace, revision["activity_key"], as_of=at
            ),
            "participating_orgs": [
                {k: o.get(k) for k in ("role", "ref", "name", "type")}
                for o in activity.get("participating_orgs") or []
            ],
            "recipient_countries": activity.get("recipient_countries") or [],
            "recipient_regions": activity.get("recipient_regions") or [],
            "sectors": activity.get("sectors") or [],
            "cites": self._cite(revision),
        }

    def _record(
        self,
        namespace: str,
        kind: str,
        request: Mapping[str, Any],
        answer: Mapping[str, Any],
        coverage: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, Any]:
        generation = self.store.generation(namespace)
        body = {
            "kind": kind,
            "request": dict(request),
            "generation": generation,
            "answer_sha256": digest(answer),
        }
        answer_id = "devfin-answer:" + digest([namespace, body])[:24]
        if self.record:
            self.conn.execute(_DDL)
            self.conn.execute(
                "INSERT INTO devfin_answers VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    namespace,
                    answer_id,
                    kind,
                    canonical(dict(request)),
                    request.get("as_of"),
                    generation,
                    body["answer_sha256"],
                    canonical(coverage),
                    principal_id,
                    self.now(),
                ],
            )
        return {
            "answer_id": answer_id,
            "generation": generation,
            "answer_sha256": body["answer_sha256"],
            "stored": self.record,
        }

    @staticmethod
    def _request(request: Mapping[str, Any]) -> dict[str, Any]:
        extra = set(request) - REQUEST_KEYS
        if extra:
            raise DevelopmentFinanceError(
                "invalid_request", f"unsupported query fields: {sorted(extra)}"
            )
        clean = {k: v for k, v in request.items() if v not in (None, "")}
        if clean.get("as_of") is not None:
            as_of_ms(clean["as_of"])
        return clean

    # ------------------------------------------------------------------ answers

    def list_activities(
        self,
        namespace: str,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        request = self._request(request)
        at, revisions = self._selected(namespace, request, scopes)
        by_publisher: dict[str, list[dict[str, Any]]] = {}
        for revision in revisions:
            by_publisher.setdefault(revision["publisher_id"], []).append(
                self._summary(namespace, revision, at)
            )
        coverage = self._coverage(namespace, by_publisher, at)
        answer = {
            "contract": ANSWER_CONTRACT,
            "kind": "activities",
            "request": request,
            "publishers": [
                {"publisher_id": p, "activities": items, "coverage": coverage[p]}
                for p, items in sorted(by_publisher.items())
            ],
            "conflicts": self._conflicts(revisions),
            "crs": self._crs_for(namespace, request, at)
            if request.get("crs_recipient") or request.get("crs_donor")
            else [],
            "unknowns": self._unknowns(namespace, revisions, coverage, scopes),
            "boundary": NEVER_SENTENCE,
        }
        answer["receipt"] = self._record(
            namespace, "activities", request, answer, coverage, principal_id
        )
        return answer

    @staticmethod
    def _conflicts(revisions) -> list[dict[str, Any]]:
        """Fields on which publishers reporting the same IATI identifier disagree, with each value side by side."""
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for revision in revisions:
            groups.setdefault(identifier_key(revision["iati_identifier"]), []).append(
                revision
            )
        out = []
        for identifier, group in sorted(groups.items()):
            if len({r["publisher_id"] for r in group}) < 2:
                continue
            fields = []
            for field in (
                "title",
                "activity_status",
                "recipient_countries",
                "sectors",
                "participating_orgs",
            ):
                values = {r["publisher_id"]: r["activity"].get(field) for r in group}
                if len({canonical(v) for v in values.values()}) > 1:
                    fields.append(
                        {
                            "field": field,
                            "values": [
                                {
                                    "publisher_id": p,
                                    "revision_id": next(
                                        r["revision_id"]
                                        for r in group
                                        if r["publisher_id"] == p
                                    ),
                                    "value": v,
                                }
                                for p, v in sorted(values.items())
                            ],
                        }
                    )
            out.append(
                {
                    "iati_identifier": group[0]["iati_identifier"],
                    "publishers": sorted(r["publisher_id"] for r in group),
                    "fields": fields,
                    "note": "kept side by side with both provenance chains; nothing merged or averaged",
                }
            )
        return out

    def inspect_activity(
        self,
        namespace: str,
        iati_identifier: str,
        *,
        as_of: str | None = None,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        request = self._request({"iati_identifier": iati_identifier, "as_of": as_of})
        at, revisions = self._selected(namespace, request, scopes)
        if not revisions:
            raise DevelopmentFinanceError(
                "not_found", "no publisher reports this activity as of the date"
            )
        reports = []
        for revision in sorted(revisions, key=lambda r: r["publisher_id"]):
            history = self.store.history(namespace, revision["activity_key"], as_of=at)
            reports.append(
                {
                    "publisher_id": revision["publisher_id"],
                    "current": {
                        **self._summary(namespace, revision, at),
                        "transactions": revision["transactions"],
                        "results": self._results(revision),
                    },
                    "history": [
                        {
                            **self._cite(h),
                            "arrival": h["arrival"],
                            "supersedes": h["supersedes"],
                        }
                        for h in history
                    ],
                    "transaction_changes": self._transaction_changes(history),
                }
            )
        coverage = self._coverage(namespace, [r["publisher_id"] for r in reports], at)
        answer = {
            "contract": ANSWER_CONTRACT,
            "kind": "activity",
            "request": request,
            "reports": reports,
            "conflicts": self._conflicts(revisions),
            "world_bank": self.world_bank_links(
                namespace,
                as_of=request.get("as_of"),
                scopes=scopes,
                iati_identifier=iati_identifier,
            )["links"],
            "coverage": coverage,
            "unknowns": self._unknowns(namespace, revisions, coverage, scopes),
            "boundary": NEVER_SENTENCE,
        }
        answer["receipt"] = self._record(
            namespace, "activity", request, answer, coverage, principal_id
        )
        return answer

    @staticmethod
    def _results(revision: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for result in revision["activity"].get("results") or []:
            out.append(
                {
                    "type": result.get("type"),
                    "title": result.get("title"),
                    "indicators": [
                        {
                            "title": i.get("title"),
                            "measure": i.get("measure"),
                            "baselines": i.get("baselines"),
                            "periods": i.get("periods"),
                        }
                        for i in result.get("indicators") or []
                    ],
                    "note": "reported baselines, targets and actuals with their periods; no achievement judgement",
                }
            )
        return out

    @staticmethod
    def _transaction_changes(history) -> list[dict[str, Any]]:
        """Between consecutive revisions (publisher stamp order): new and corrected transactions, citing both."""
        changes = []
        for before, after in zip(history, history[1:]):
            prior = {t["transaction_key"]: t for t in before["transactions"]}
            for tx in after["transactions"]:
                old = prior.get(tx["transaction_key"])
                if old is None:
                    changes.append(
                        {
                            "change": "new",
                            "transaction_key": tx["transaction_key"],
                            "after": tx["transaction_id"],
                            "after_revision": after["revision_id"],
                        }
                    )
                elif old["content_hash"] != tx["content_hash"]:
                    changes.append(
                        {
                            "change": "corrected",
                            "transaction_key": tx["transaction_key"],
                            "before": {
                                "transaction_id": old["transaction_id"],
                                "value_text": old["value_text"],
                                "currency": old["currency"],
                                "value_date": old["value_date"],
                            },
                            "after": {
                                "transaction_id": tx["transaction_id"],
                                "value_text": tx["value_text"],
                                "currency": tx["currency"],
                                "value_date": tx["value_date"],
                            },
                            "before_revision": before["revision_id"],
                            "after_revision": after["revision_id"],
                        }
                    )
        return changes

    def search_transactions(
        self,
        namespace: str,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        request = self._request(request)
        at, revisions = self._selected(namespace, request, scopes)
        by_publisher: dict[str, dict[str, Any]] = {}
        for revision in revisions:
            entry = by_publisher.setdefault(
                revision["publisher_id"], {"transactions": [], "totals": {}}
            )
            for tx in revision["transactions"]:
                if request.get("transaction_type") and tx["type"] != str(
                    request["transaction_type"]
                ):
                    continue
                entry["transactions"].append({**tx, "cites": self._cite(revision)})
        publishers = []
        for publisher, entry in sorted(by_publisher.items()):
            publishers.append(
                {
                    "publisher_id": publisher,
                    "transactions": entry["transactions"],
                    "totals": self._totals(entry["transactions"]),
                }
            )
        coverage = self._coverage(namespace, by_publisher, at)
        answer = {
            "contract": ANSWER_CONTRACT,
            "kind": "transactions",
            "request": request,
            "publishers": publishers,
            "coverage": coverage,
            "unknowns": self._unknowns(namespace, revisions, coverage, scopes),
            "note": "totals are within one publisher, one transaction type and one currency; never across "
            "publishers or currencies, and never converted",
            "boundary": NEVER_SENTENCE,
        }
        answer["receipt"] = self._record(
            namespace, "transactions", request, answer, coverage, principal_id
        )
        return answer

    @staticmethod
    def _totals(transactions) -> list[dict[str, Any]]:
        groups: dict[tuple, dict[str, Any]] = {}
        excluded: dict[str, int] = {}
        for tx in transactions:
            if tx["amount_state"] == "unknown":
                excluded[tx["type"] or "unstated"] = (
                    excluded.get(tx["type"] or "unstated", 0) + 1
                )
                continue
            key = (tx["type"], tx["currency"])
            group = groups.setdefault(
                key,
                {
                    "transaction_type": tx["type"],
                    "currency": tx["currency"],
                    "sum": Decimal(0),
                    "count": 0,
                    "value_dates": [],
                },
            )
            group["sum"] += Decimal(tx["value"])
            group["count"] += 1
            group["value_dates"].append(tx["value_date"])
        out = []
        for key, group in sorted(
            groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
        ):
            out.append(
                {
                    "transaction_type": group["transaction_type"],
                    "currency": group["currency"],
                    "sum": str(group["sum"]),
                    "transactions": group["count"],
                    "value_date_range": [
                        min(group["value_dates"]),
                        max(group["value_dates"]),
                    ],
                    "excluded_unknown_amounts": excluded.get(
                        group["transaction_type"] or "unstated", 0
                    ),
                }
            )
        for tx_type, count in sorted(excluded.items()):
            if not any((o["transaction_type"] or "unstated") == tx_type for o in out):
                out.append(
                    {
                        "transaction_type": tx_type,
                        "currency": None,
                        "sum": None,
                        "transactions": 0,
                        "value_date_range": None,
                        "excluded_unknown_amounts": count,
                    }
                )
        return out

    def publisher_coverage(
        self,
        namespace: str,
        *,
        as_of: str | None = None,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        request = self._request({"as_of": as_of})
        at = as_of_ms(as_of)
        current = self.store.current(namespace, as_of=at)
        publishers = sorted(
            {
                p["publisher_id"]
                for p in self.store.publishers(namespace)
                if p["provider"] == "iati"
            }
        )
        coverage = self._coverage(namespace, publishers, at)
        for publisher in publishers:
            coverage[publisher]["activities_current"] = sum(
                1 for r in current.values() if r["publisher_id"] == publisher
            )
        answer = {
            "contract": ANSWER_CONTRACT,
            "kind": "coverage",
            "request": request,
            "publishers": coverage,
            "bounded": "no record set implies complete coverage of IATI, the OECD CRS or the World Bank",
            "boundary": NEVER_SENTENCE,
        }
        answer["receipt"] = self._record(
            namespace, "coverage", request, answer, coverage, principal_id
        )
        return answer

    def _crs_for(
        self, namespace: str, request: Mapping[str, Any], at: int | None
    ) -> list[dict[str, Any]]:
        # CRS codes are named explicitly in the CRS's own scheme (e.g. an ISO alpha-3 recipient); an IATI alpha-2
        # country is never converted into one.
        sector = request.get("sector")
        return self._crs(
            namespace,
            donor=request.get("crs_donor"),
            recipient=request.get("crs_recipient"),
            sector=str(sector)[:3] if sector else None,
            price_basis=None,
            at=at,
        )

    def _crs(
        self, namespace, *, donor, recipient, sector, price_basis, at
    ) -> list[dict[str, Any]]:
        out = []
        for cell in self.store.crs_cells(
            namespace,
            donor=donor,
            recipient=recipient,
            sector=sector,
            price_basis=price_basis,
        ):
            vintages = self.store.crs_vintages(namespace, cell["cell_id"], as_of=at)
            if not vintages:
                continue
            current = vintages[-1]
            out.append(
                {
                    **cell,
                    "vintage": {
                        k: current[k]
                        for k in (
                            "vintage_id",
                            "vintage_no",
                            "published_on",
                            "release_label",
                            "vintage_basis",
                            "dataflow_version",
                            "base_year",
                            "base_year_state",
                            "unit_mult",
                            "observations",
                            "dataset_id",
                            "file_sha256",
                            "evidence_origin",
                        )
                    },
                    "earlier_vintages": [
                        {
                            "vintage_id": v["vintage_id"],
                            "published_on": v["published_on"],
                            "release_label": v["release_label"],
                        }
                        for v in vintages[:-1]
                    ],
                    "note": "an OECD CRS statistic with its vintage; never decomposed into activities or summed with "
                    "IATI transactions",
                }
            )
        return out

    def crs_aggregates(
        self,
        namespace: str,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        allowed = {"donor", "recipient", "sector", "price_basis", "as_of"}
        if set(request) - allowed:
            raise DevelopmentFinanceError(
                "invalid_request", f"CRS lookups take {sorted(allowed)}"
            )
        if not table_exists(self.conn, "devfin_crs_cells"):
            raise DevelopmentFinanceError(
                "not_ready",
                "no OECD CRS vintage is stored yet; run the "
                "oecd-crs-development-finance source",
            )
        request = {k: v for k, v in request.items() if v not in (None, "")}
        at = as_of_ms(request.get("as_of"))
        cells = self._crs(
            namespace,
            donor=request.get("donor"),
            recipient=request.get("recipient"),
            sector=request.get("sector"),
            price_basis=request.get("price_basis"),
            at=at,
        )
        answer = {
            "contract": ANSWER_CONTRACT,
            "kind": "crs",
            "request": request,
            "cells": cells,
            "publisher_id": "oecd-crs:ref:OECD",
            "boundary": NEVER_SENTENCE,
        }
        answer["receipt"] = self._record(
            namespace, "crs", request, answer, {}, principal_id
        )
        return answer

    def world_bank_links(
        self,
        namespace: str,
        *,
        as_of: str | None = None,
        scopes: Iterable[str],
        project_id: str | None = None,
        iati_identifier: str | None = None,
    ) -> dict[str, Any]:
        """World Bank projects beside IATI activities: a link needs a stated identifier; similarity is a candidate."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        at = as_of_ms(as_of)
        projects = self.store.world_bank_projects(namespace, as_of=at)
        revisions = list(self.store.current(namespace, as_of=at).values())
        links = []
        for project in projects:
            if project_id and project["project_id"] != project_id:
                continue
            body = project["project"]
            explicit = []
            for revision in revisions:
                if iati_identifier and identifier_key(
                    revision["iati_identifier"]
                ) != identifier_key(iati_identifier):
                    continue
                evidence = world_bank_link_evidence(
                    project["project_id"], revision["activity"]
                )
                if evidence:
                    explicit.append(revision["activity_key"])
                    links.append(
                        {
                            "state": "linked",
                            "project_id": project["project_id"],
                            "project_revision_id": project["revision_id"],
                            "activity_key": revision["activity_key"],
                            "activity_revision_id": revision["revision_id"],
                            "evidence": evidence,
                        }
                    )
            for revision in revisions:
                if revision["activity_key"] in explicit:
                    continue
                if iati_identifier and identifier_key(
                    revision["iati_identifier"]
                ) != identifier_key(iati_identifier):
                    continue
                reasons = self._similarity(body, revision["activity"])
                if reasons:
                    links.append(
                        {
                            "state": "candidate",
                            "project_id": project["project_id"],
                            "project_revision_id": project["revision_id"],
                            "activity_key": revision["activity_key"],
                            "activity_revision_id": revision["revision_id"],
                            "reasons": reasons,
                            "note": "a shared title, country or amount is a candidate only, never a link",
                        }
                    )
        linked = {link["project_id"] for link in links}
        return {
            "links": links,
            "unlinked_projects": sorted(
                p["project_id"] for p in projects if p["project_id"] not in linked
            ),
            "note": "linked records keep their own identifiers, revisions and publishers; amounts are never "
            "summed across a pair",
        }

    @staticmethod
    def _similarity(
        project: Mapping[str, Any], activity: Mapping[str, Any]
    ) -> list[str]:
        countries = {
            str(c.get("code") or "").upper()
            for c in activity.get("recipient_countries") or []
        }
        if not countries & set(project.get("countries") or []):
            return []
        reasons = ["shared recipient country"]
        stop = {
            "the",
            "of",
            "and",
            "project",
            "programme",
            "program",
            "support",
            "fictional",
        }
        left = {w for w in name_key(project.get("name")).split() if w not in stop}
        right = {w for w in name_key(activity.get("title")).split() if w not in stop}
        if left and right and len(left & right) / min(len(left), len(right)) >= 0.5:
            reasons.append("similar title")
        amounts = {
            c.get("value")
            for c in (project.get("commitments") or {}).values()
            if c.get("value")
        }
        if amounts & {
            tx.get("value")
            for tx in activity.get("transactions") or []
            if tx.get("value")
        }:
            reasons.append("equal amount")
        return reasons if len(reasons) > 1 else []

    # ------------------------------------------------------------------ receipts, projects and reports

    def record_answer(
        self,
        namespace: str,
        kind: str,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Answer again and store the receipt (query, as-of date, generation, coverage) for projects and reports."""
        if not self.record:
            raise DevelopmentFinanceError(
                "read_only", "answers are recorded on a writable connection"
            )
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        request = dict(request)
        if kind == "activities":
            return self.list_activities(
                namespace, request, principal_id=principal_id, scopes=scopes
            )
        if kind == "transactions":
            return self.search_transactions(
                namespace, request, principal_id=principal_id, scopes=scopes
            )
        if kind == "activity":
            return self.inspect_activity(
                namespace,
                str(request.get("iati_identifier") or ""),
                as_of=request.get("as_of"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if kind == "coverage":
            return self.publisher_coverage(
                namespace,
                as_of=request.get("as_of"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if kind == "crs":
            return self.crs_aggregates(
                namespace, request, principal_id=principal_id, scopes=scopes
            )
        raise DevelopmentFinanceError(
            "invalid_request",
            "kind is activities, transactions, activity, coverage or crs",
        )

    def answer(
        self, namespace: str, answer_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = (
            self.conn.execute(
                "SELECT kind, request_json, as_of, generation, answer_sha256, coverage_json, principal_id, created_at_ms "
                "FROM devfin_answers WHERE namespace=? AND answer_id=?",
                [namespace, answer_id],
            ).fetchone()
            if table_exists(self.conn, "devfin_answers")
            else None
        )
        if row is None:
            raise DevelopmentFinanceError(
                "not_found", "no stored development-finance answer has this id"
            )
        return {
            "answer_id": answer_id,
            "kind": row[0],
            "request": json.loads(row[1]),
            "as_of": row[2],
            "generation": int(row[3]),
            "answer_sha256": row[4],
            "coverage": json.loads(row[5]),
            "principal_id": row[6],
            "created_at_ms": int(row[7]),
        }

    def attach_to_project(
        self,
        namespace: str,
        answer_id: str,
        project_id: str,
        expected_revision: int,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Pin a stored answer (query, as-of date, coverage) in a research project through its own owner."""
        from src.kb.research_projects import ResearchProjectStore

        scopes = set(scopes)
        stored = self.answer(
            namespace, answer_id, scopes=scopes
        )  # validated against the store
        if "operator" not in scopes and PROJECTS_WRITE not in scopes:
            raise DevelopmentFinanceError(
                "unauthorized", f"{PROJECTS_WRITE} is required"
            )
        project = ResearchProjectStore(self.conn, now=self.now).revise(
            namespace,
            project_id,
            expected_revision,
            principal_id=principal_id,
            scopes=scopes,
            add_links=[
                {
                    "kind": "evidence",
                    "id": answer_id,
                    "namespace": namespace,
                    "generation": stored["generation"],
                }
            ],
        )
        return {
            "project_id": project_id,
            "revision": project["revision"],
            "answer": stored,
        }

    def report_citation(
        self, namespace: str, answer_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """The bibliography entry and dependency an authored report uses to cite a stored answer."""
        stored = self.answer(namespace, answer_id, scopes=scopes)
        request = stored["request"]
        text = (
            f"Development-finance answer {answer_id} ({stored['kind']}; query "
            f"{canonical({k: v for k, v in request.items() if k != 'as_of'})}; as of "
            f"{request.get('as_of') or 'the acquisition time'}; store generation {stored['generation']}; "
            f"publisher coverage recorded with the answer)"
        )
        return {
            "bibliography": {"id": answer_id, "text": text},
            "dependency": {
                "kind": "source",
                "id": answer_id,
                "revision": str(stored["generation"]),
                "namespace": namespace,
                "locator": {"section": stored["kind"]},
            },
        }


def ensure_answers(conn: Any) -> None:
    conn.execute(_DDL)


__all__ = ["DevelopmentFinanceQueries", "WRITE_SCOPE", "ensure_answers"]

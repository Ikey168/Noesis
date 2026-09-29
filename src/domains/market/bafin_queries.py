"""Point-in-time BaFin notice queries under the Market publication cutoffs, and issuer dossiers (#2106, BF09).

Every query takes an ``as_of`` date (or epoch milliseconds) and an optional
``acquired_by_ms`` and applies the Market ``public_and_acquired`` policy
(:mod:`src.domains.market.asof`) through
:meth:`BafinNoticeStore.visible`: a notice published after the cutoff is
invisible even when its event date is earlier, and a revision the source
edited is invisible before it was observed. Correction chains are collapsed to
the notice published last by the cutoff.

Results state their semantics. They carry no advice, signal or sentiment:
holdings are never summed across notifiers, a holder without a recent
notification is shown with its last notice date (never dropped or
extrapolated), and a net short position that stopped being published reads
"below publication threshold or closed", never 0 %.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from src.domains.market.bafin_notices import (
    NOTICE,
    READ_SCOPE,
    THRESHOLDS,
    BafinError,
    BafinNoticeStore,
    authorize,
    current_per_chain,
    cutoffs,
    digest,
    iso_day,
    normalize_bafin_id,
    normalize_isin,
    party_key,
)

CONTRACT = "noesis-bafin-notice-answer-v1"
DOSSIER_CONTRACT = "noesis-bafin-notice-dossier-v1"
STALE_AFTER_DAYS = 365
SEMANTICS = {
    "cutoff": "only notices published by the as-of cutoff (end of the as-of day, UTC) and, when given, acquired by "
    "acquired_by_ms; a missing publication date counts from the first observation",
    "corrections": "one notice per correction chain: the latest published by the cutoff; earlier ones are listed as "
    "corrected",
    "holdings": "percentages as notified with their WpHG basis; never summed across notifiers",
    "exclusions": "no investment advice, signal, trading recommendation or sentiment",
}


def _pins(views: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {v["notice_id"]: v["revision_id"] for v in views}


def _row(view: Mapping[str, Any]) -> dict[str, Any]:
    notice = view["notice"]
    return {
        "notice_id": view["notice_id"],
        "revision_id": view["revision_id"],
        "source": {
            k: notice["source"].get(k)
            for k in ("provider", "source_id", "url", "locator")
        },
        "publication_date": notice.get("publication_date"),
        "publication_basis": view["publication_basis"],
        "public_at_ms": view["public_at_ms"],
        "listing": {k: view["listing"][k] for k in ("state", "observed_on")},
    }


def _thresholds(value: str | None, basis: str) -> list[str]:
    if value is None:
        return []
    number = Decimal(value)
    return [t for t in THRESHOLDS[basis] if number >= Decimal(t)]


class BafinQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = BafinNoticeStore(conn, initialize=False, now=now)

    def _visible(self, namespace, scopes, kinds, as_of, acquired_by_ms, isin=None):
        authorize(namespace, scopes, READ_SCOPE)
        cut = cutoffs(as_of, acquired_by_ms)
        result = self.store.visible(
            namespace,
            kinds=kinds,
            issuer_isin=isin,
            public_cutoff_ms=cut["publicly_available_by_ms"],
            acquired_by_ms=acquired_by_ms,
        )
        return cut, result

    def _answer(
        self,
        query: str,
        namespace: str,
        cut: Mapping[str, Any],
        body: Mapping[str, Any],
        views: Iterable[Mapping[str, Any]],
        unreadable: list,
    ) -> dict[str, Any]:
        answer = {
            "contract": CONTRACT,
            "query": query,
            "namespace": namespace,
            "cutoffs": dict(cut),
            **body,
            "unreadable": unreadable,
            "semantics": SEMANTICS,
            "notice": NOTICE,
            "pins": dict(sorted(_pins(views).items())),
            "generation": self.store.generation(namespace),
        }
        answer["answer_hash"] = digest(
            {k: v for k, v in answer.items() if k not in {"generation"}}
        )
        return answer

    # -------------------------------------------------------------- voting rights

    def holders_above_thresholds(
        self,
        namespace: str,
        isin: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
        threshold: str | None = None,
        stale_after_days: int = STALE_AFTER_DAYS,
    ) -> dict[str, Any]:
        """The latest notification per notifier published by the cutoff, with its thresholds and basis."""
        isin = normalize_isin(isin)
        cut, visible = self._visible(
            namespace,
            set(scopes),
            ("voting_rights_notification",),
            as_of,
            acquired_by_ms,
            isin,
        )
        current = current_per_chain(visible["notices"])
        withdrawn = [v for v in current if v["notice"].get("withdrawn")]
        live = [v for v in current if not v["notice"].get("withdrawn")]
        latest: dict[str, dict[str, Any]] = {}
        for view in live:
            notifier = view["notice"]["notifier"]
            key = party_key(notifier["name"], kind=notifier.get("kind", "unknown"))
            order = (
                view["notice"].get("publication_date") or "",
                view["notice"].get("event_date") or "",
                view["public_at_ms"],
                view["notice_id"],
            )
            entry = latest.setdefault(key, {"order": None, "view": None, "all": []})
            entry["all"].append(view["notice_id"])
            if entry["order"] is None or order > entry["order"]:
                entry.update({"order": order, "view": view})
        as_of_day = date.fromisoformat(cut["as_of"])
        holders, below = [], []
        for key, entry in sorted(latest.items()):
            view = entry["view"]
            notice = view["notice"]
            percentages = notice["percentages"]
            reached = {
                basis: _thresholds(percentages.get(basis), basis)
                for basis in ("s33", "s38", "s39")
            }
            published = notice.get("publication_date")
            age = (
                (as_of_day - date.fromisoformat(published)).days if published else None
            )
            item = {
                **_row(view),
                "issuer": notice["issuer"],
                "notifier": notice["notifier"],
                "earlier_notifications": sorted(
                    set(entry["all"]) - {view["notice_id"]}
                ),
                "chain": notice.get("chain") or [],
                "percentages": percentages,
                "thresholds_stated": notice.get("thresholds") or [],
                "thresholds_reached": reached,
                "event_date": notice.get("event_date"),
                "corrections": [
                    m for m in view["chain"]["members"] if m != view["notice_id"]
                ],
                "correction_link": view["chain"]["link"],
                "last_notice_date": published,
                "age_days": age,
                "stale": bool(age is not None and age > stale_after_days),
                "stale_label": (
                    f"no notification for more than {stale_after_days} days; shown as last notified, "
                    "not extrapolated"
                )
                if age is not None and age > stale_after_days
                else None,
            }
            lowest = [basis for basis in ("s33", "s39") if reached[basis]]
            if not lowest:
                below.append(item)
                continue
            if threshold is not None and not any(
                Decimal(threshold) <= Decimal(percentages.get(b) or "0")
                for b in ("s33", "s38", "s39")
            ):
                continue
            holders.append(item)
        views = [e["view"] for e in latest.values()]
        return self._answer(
            "holders_above_thresholds",
            namespace,
            cut,
            {
                "issuer_isin": isin,
                "threshold": threshold,
                "holders": holders,
                "notified_below_lowest_threshold": below,
                "withdrawn": [_row(v) for v in withdrawn],
                "semantics_detail": "the latest notification per notifier by publication date; a holder whose latest "
                "notification states less than 3 % is listed apart; thresholds are those the stated "
                "percentage is at or above (WpHG § 33: 3 % upwards; §§ 38 and 39: 5 % upwards)",
            },
            views,
            visible["unreadable"],
        )

    # -------------------------------------------------------------- managers' transactions

    def managers_transactions(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        date_from: Any,
        date_to: Any,
        isin: str | None = None,
        person: str | None = None,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Managers' transactions in a window (by transaction or trade date) for an issuer or a person."""
        if not isin and not person:
            raise BafinError("invalid_request", "name an issuer (ISIN) or a person")
        start, end = iso_day(date_from), iso_day(date_to)
        if not start or not end or start > end:
            raise BafinError("invalid_request", "the window needs date_from <= date_to")
        cut, visible = self._visible(
            namespace,
            set(scopes),
            ("managers_transaction",),
            as_of or end,
            acquired_by_ms,
            normalize_isin(isin) if isin else None,
        )
        wanted = party_key(person, kind="natural_person") if person else None
        rows, used = [], []
        for view in current_per_chain(visible["notices"]):
            notice = view["notice"]
            dates = {
                d
                for d in [
                    notice.get("transaction_date"),
                    *[t.get("date") for t in notice.get("trades") or []],
                ]
                if d
            }
            if not any(start <= d <= end for d in dates):
                continue
            who = notice["person"]
            if wanted and (
                who.get("withdrawn")
                or party_key(who.get("name"), kind="natural_person") != wanted
            ):
                continue
            used.append(view)
            rows.append(
                {
                    **_row(view),
                    "issuer": notice["issuer"],
                    "person": who,
                    "instrument": notice["instrument"],
                    "nature": notice["nature"],
                    "trades": notice.get("trades") or [],
                    "aggregate": notice.get("aggregate"),
                    "transaction_date": notice.get("transaction_date"),
                    "venue": notice.get("venue"),
                    "notification_date": notice.get("notification_date"),
                    "amendments": [
                        m for m in view["chain"]["members"] if m != view["notice_id"]
                    ],
                }
            )
        rows.sort(
            key=lambda r: (
                r["transaction_date"] or "",
                r["publication_date"] or "",
                r["notice_id"],
            )
        )
        return self._answer(
            "managers_transactions",
            namespace,
            cut,
            {
                "issuer_isin": normalize_isin(isin) if isin else None,
                "person": person,
                "window": [start, end],
                "transactions": rows,
                "semantics_detail": "each notification with its trades and the aggregate as published; nothing is "
                "netted, summed across persons or scored; a person is matched by name only within "
                "each notice's issuer, and withdrawn person data is never matched",
            },
            used,
            visible["unreadable"],
        )

    # -------------------------------------------------------------- net short positions

    def net_short_positions(
        self,
        namespace: str,
        isin: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The latest published position per holder, with publication-end handling."""
        isin = normalize_isin(isin)
        cut, visible = self._visible(
            namespace, set(scopes), ("net_short_position",), as_of, acquired_by_ms, isin
        )
        latest: dict[str, dict[str, Any]] = {}
        for view in visible["notices"]:
            notice = view["notice"]
            key = party_key(notice["holder"]["name"])
            if (
                key not in latest
                or notice["position_date"] > latest[key]["notice"]["position_date"]
            ):
                latest[key] = view
        positions, ended, total = [], [], Decimal("0")
        for _, view in sorted(latest.items()):
            notice = view["notice"]
            base = {
                **_row(view),
                "holder": notice["holder"],
                "position_date": notice["position_date"],
            }
            if notice.get("publication_ended"):
                ended.append(
                    {
                        **base,
                        "status": "below publication threshold or closed",
                        "basis": f"the last published value ({notice['position_pct']} %) is below 0.5 %",
                    }
                )
            elif view["listing"]["state"] == "no_longer_listed":
                ended.append(
                    {
                        **base,
                        "status": "below publication threshold or closed",
                        "basis": f"no longer in the current publication (observed {view['listing']['observed_on']})",
                    }
                )
            else:
                positions.append(
                    {
                        **base,
                        "status": "published",
                        "position_pct": notice["position_pct"],
                    }
                )
                total += Decimal(notice["position_pct"])
        return self._answer(
            "net_short_positions",
            namespace,
            cut,
            {
                "issuer_isin": isin,
                "positions": positions,
                "no_longer_published": ended,
                "sum_of_published_positions": {
                    "value": format(total.normalize(), "f") if positions else None,
                    "count": len(positions),
                    "label": "sum of the published positions (each at least 0.5 %); not total short interest",
                },
                "semantics_detail": "a position that stopped being published is 'below publication threshold or "
                "closed', never 0 %",
            },
            list(latest.values()),
            visible["unreadable"],
        )

    # -------------------------------------------------------------- warnings, measures, authorisation

    def warnings_for_entity(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        name: str | None = None,
        party: str | None = None,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Warnings and measures naming a string (name-equal, unattributed) or linked by a reviewed match."""
        if not name and not party:
            raise BafinError("invalid_request", "name a string or a party record key")
        cut, visible = self._visible(
            namespace,
            set(scopes),
            ("bafin_warning", "bafin_measure"),
            as_of if as_of is not None else self.store.now(),
            acquired_by_ms,
        )
        from src.domains.market.bafin_identity import BafinIdentity, named_key

        links = BafinIdentity(self.conn, initialize=False).accepted_links(namespace)
        wanted = party_key(name) if name else None
        reviewed_names = (
            {
                k
                for k, targets in links.items()
                if k.startswith("bafin:named:") and party in targets
            }
            if (party)
            else set()
        )
        name_equal, reviewed, used = [], [], []
        for view in visible["notices"]:
            notice = view["notice"]
            item = {
                **_row(view),
                "kind": notice["kind"],
                "title": notice["title"],
                "named_entities": notice.get("named_entities") or [],
                "legal_basis": notice.get("legal_basis") or [],
                "category": notice.get("category"),
                "removed": view["listing"]["state"] == "no_longer_listed",
            }
            keys = {named_key(n) for n in notice.get("named_entities") or []}
            if reviewed_names & keys:
                reviewed.append({**item, "match": "reviewed identity match"})
                used.append(view)
            elif wanted and wanted in {
                party_key(n) for n in notice.get("named_entities") or []
            }:
                name_equal.append(
                    {
                        **item,
                        "match": "the source names an equal string; not attributed to any entity "
                        "without a reviewed match",
                    }
                )
                used.append(view)
        return self._answer(
            "warnings_for_entity",
            namespace,
            cut,
            {
                "name": name,
                "party": party,
                "matched_by_review": reviewed,
                "name_equal_unreviewed": name_equal,
                "semantics_detail": "a warning or measure names source strings; only a reviewed identity match attributes "
                "it to an entity; removals are kept as removals",
            },
            used,
            visible["unreadable"],
        )

    def authorisation_status(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        as_of: Any,
        bafin_id: str | None = None,
        name: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Whether an entity appears in the BaFin company database as of a date, with its licences as published."""
        if not bafin_id and not name:
            raise BafinError("invalid_request", "name a BaFin ID or a name")
        cut, visible = self._visible(
            namespace, set(scopes), ("authorised_entity",), as_of, acquired_by_ms
        )
        day = cut["as_of"]
        wanted_id = normalize_bafin_id(bafin_id) if bafin_id else None
        wanted_name = party_key(name) if name else None
        entities, used = [], []
        for view in visible["notices"]:
            notice = view["notice"]
            if wanted_id and notice["bafin_id"] != wanted_id:
                continue
            if wanted_name and party_key(notice["name"]) != wanted_name:
                continue
            used.append(view)
            licences = []
            for licence in notice.get("licences") or []:
                if licence.get("start") and licence["start"] > day:
                    state = "not started"
                elif licence.get("end") and licence["end"] <= day:
                    state = "ended"
                elif licence.get("start") is None:
                    state = "start not stated"
                else:
                    state = "active as published"
                licences.append({**licence, "as_of_state": state})
            entities.append(
                {
                    **_row(view),
                    "bafin_id": notice["bafin_id"],
                    "name": notice["name"],
                    "place": notice.get("place"),
                    "country": notice.get("country"),
                    "lei": notice.get("lei"),
                    "listed": view["listing"]["state"] == "listed",
                    "licences": licences,
                }
            )
        status = (
            "listed"
            if any(e["listed"] for e in entities)
            else "no longer listed"
            if entities
            else "not in the acquired company database as of the date"
        )
        return self._answer(
            "authorisation_status",
            namespace,
            cut,
            {
                "bafin_id": wanted_id,
                "name": name,
                "status": status,
                "entities": entities,
                "semantics_detail": "what the acquired company database states as of the cutoff; absence means not "
                "acquired or not listed, never a statement that no authorisation exists",
            },
            used,
            visible["unreadable"],
        )

    # -------------------------------------------------------------- dossier

    def dossier(
        self,
        namespace: str,
        isin: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        principal_id: str,
        acquired_by_ms: int | None = None,
        window_days: int = 365,
        market_namespace: str | None = None,
        lei_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Holders, dealings, short positions, warnings and measures naming related entities, and their authorisation."""
        from src.domains.market.bafin_identity import (
            BafinIdentity,
            organisation_key,
            resolve_issuer,
        )

        scopes = set(scopes)
        isin = normalize_isin(isin)
        holders = self.holders_above_thresholds(
            namespace, isin, as_of, scopes=scopes, acquired_by_ms=acquired_by_ms
        )
        day = holders["cutoffs"]["as_of"]
        start = (date.fromisoformat(day) - timedelta(days=int(window_days))).isoformat()
        dealings = self.managers_transactions(
            namespace,
            scopes=scopes,
            isin=isin,
            date_from=start,
            date_to=day,
            as_of=as_of,
            acquired_by_ms=acquired_by_ms,
        )
        shorts = self.net_short_positions(
            namespace, isin, as_of, scopes=scopes, acquired_by_ms=acquired_by_ms
        )
        issuer = None
        for group in (holders["holders"], holders["notified_below_lowest_threshold"]):
            for item in group:
                issuer = issuer or item.get("issuer")
        related: dict[str, dict[str, Any]] = {}

        def relate(name: str | None, role: str) -> None:
            if name:
                related.setdefault(party_key(name), {"names": set(), "roles": set()})
                related[party_key(name)]["names"].add(name)
                related[party_key(name)]["roles"].add(role)

        visible_issuer = self.store.visible(
            namespace,
            kinds=(
                "voting_rights_notification",
                "managers_transaction",
                "net_short_position",
            ),
            issuer_isin=isin,
            public_cutoff_ms=holders["cutoffs"]["publicly_available_by_ms"],
            acquired_by_ms=acquired_by_ms,
        )["notices"]
        issuer_name = next(
            (
                v["notice"]["issuer"].get("name")
                for v in visible_issuer
                if v["notice"]["issuer"].get("name")
            ),
            None,
        )
        relate(issuer_name, "issuer")
        for item in holders["holders"] + holders["notified_below_lowest_threshold"]:
            if item["notifier"].get("kind") != "natural_person":
                relate(item["notifier"]["name"], "notifier")
            for member in item["chain"]:
                relate(member["name"], "chain member")
        for item in shorts["positions"] + shorts["no_longer_published"]:
            if item["holder"].get("kind") != "natural_person":
                relate(item["holder"]["name"], "short seller")
        identity = BafinIdentity(self.conn, initialize=False)
        links = identity.accepted_links(namespace)
        warnings, authorisations = [], []
        for key, entry in sorted(related.items()):
            name = sorted(entry["names"])[0]
            party = organisation_key(name)
            hits = self.warnings_for_entity(
                namespace,
                scopes=scopes,
                name=name,
                party=party,
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            )
            if hits["matched_by_review"] or hits["name_equal_unreviewed"]:
                warnings.append(
                    {
                        "entity": name,
                        "roles": sorted(entry["roles"]),
                        "matched_by_review": hits["matched_by_review"],
                        "name_equal_unreviewed": hits["name_equal_unreviewed"],
                    }
                )
            reviewed_ids = sorted(
                t.split(":")[-1]
                for t in links.get(party, [])
                if t.startswith("bafin:authorised:")
            )
            status = None
            if reviewed_ids:
                status = self.authorisation_status(
                    namespace,
                    scopes=scopes,
                    as_of=as_of,
                    bafin_id=reviewed_ids[0],
                    acquired_by_ms=acquired_by_ms,
                )
                basis = "reviewed identity match"
            else:
                status = self.authorisation_status(
                    namespace,
                    scopes=scopes,
                    as_of=as_of,
                    name=name,
                    acquired_by_ms=acquired_by_ms,
                )
                basis = "name-equal, unreviewed"
            if status["entities"]:
                authorisations.append(
                    {
                        "entity": name,
                        "roles": sorted(entry["roles"]),
                        "match": basis,
                        "status": status["status"],
                        "entities": status["entities"],
                    }
                )
        resolution = None
        if market_namespace or lei_namespace:
            resolution = resolve_issuer(
                self.conn,
                issuer or {"isin": isin, "name": issuer_name},
                on=day,
                market_namespace=market_namespace,
                principal_id=principal_id,
                scopes=scopes,
                acquired_by_ms=acquired_by_ms,
                lei_namespace=lei_namespace,
            )
        pins = {**holders["pins"], **dealings["pins"], **shorts["pins"]}
        for group in warnings:
            for item in group["matched_by_review"] + group["name_equal_unreviewed"]:
                pins[item["notice_id"]] = item["revision_id"]
        for group in authorisations:
            for item in group["entities"]:
                pins[item["notice_id"]] = item["revision_id"]
        dossier = {
            "contract": DOSSIER_CONTRACT,
            "namespace": namespace,
            "issuer_isin": isin,
            "issuer_name": issuer_name,
            "parameters": {
                "as_of": holders["cutoffs"]["as_of"],
                "acquired_by_ms": acquired_by_ms,
                "window_days": int(window_days),
                "market_namespace": market_namespace,
                "lei_namespace": lei_namespace,
            },
            "cutoffs": holders["cutoffs"],
            "issuer_resolution": resolution,
            "holders": {
                k: holders[k]
                for k in ("holders", "notified_below_lowest_threshold", "withdrawn")
            },
            "managers_transactions": {
                "window": dealings["window"],
                "transactions": dealings["transactions"],
            },
            "net_short_positions": {
                k: shorts[k]
                for k in (
                    "positions",
                    "no_longer_published",
                    "sum_of_published_positions",
                )
            },
            "related_entities": [
                {"name": sorted(e["names"])[0], "roles": sorted(e["roles"])}
                for _, e in sorted(related.items())
            ],
            "warnings_and_measures": warnings,
            "authorisations": authorisations,
            "unreadable": holders["unreadable"]
            + dealings["unreadable"]
            + shorts["unreadable"],
            "semantics": {
                **SEMANTICS,
                "related_entities": "the issuer, notifiers, chain members and short sellers "
                "the notices name; natural persons are not searched for in warnings",
            },
            "notice": NOTICE,
            "pins": dict(sorted(pins.items())),
            "generation": self.store.generation(namespace),
        }
        dossier["dossier_hash"] = digest(
            {k: v for k, v in dossier.items() if k not in {"generation"}}
        )
        return dossier


def export_dossier_bundle(
    conn: Any, dossier: Mapping[str, Any], *, created_at_ms: int | None = None
) -> dict[str, Any]:
    """The dossier as a ``noesis-evidence-bundle-v1``: the dossier as root, one evidence object per cited revision."""
    from src.evidence_bundle import EvidenceBundleBuilder

    if dossier.get("contract") != DOSSIER_CONTRACT:
        raise BafinError(
            "invalid_request", "only an assembled BaFin notice dossier can be exported"
        )
    store = BafinNoticeStore(conn, initialize=False)
    builder = EvidenceBundleBuilder(
        "receipt",
        {
            "domain": "market",
            "kind": "bafin-notice-dossier",
            "namespace": dossier["namespace"],
            "issuer_isin": dossier["issuer_isin"],
            "parameters": dossier["parameters"],
            "dossier_hash": dossier["dossier_hash"],
        },
        created_at_ms=created_at_ms if created_at_ms is not None else store.now(),
        as_of_ms=dossier["cutoffs"]["publicly_available_by_ms"],
    )
    evidence = []
    for notice_id, revision_id in sorted(dossier["pins"].items()):
        checked = store.revision(
            dossier["namespace"], revision_id
        )  # a pinned revision must be on record
        if checked["notice_id"] != notice_id:
            raise BafinError(
                "invalid_request", "a pinned revision belongs to another notice"
            )
        history = {
            h["revision_id"]: h for h in store.history(dossier["namespace"], notice_id)
        }
        notice = history[revision_id]["notice"]
        source = notice["source"]
        evidence.append(
            builder.add_object(
                "evidence",
                {
                    "contract": "noesis-bafin-notice-evidence-v1",
                    "notice_id": notice_id,
                    "revision_id": revision_id,
                    "record_hash": checked["record_hash"],
                    "kind": notice["kind"],
                    "provider": source["provider"],
                    "source_id": source["source_id"],
                    "publication_date": notice.get("publication_date"),
                    "locator": {
                        "document_id": revision_id,
                        "url": source.get("url"),
                        "document": source.get("document"),
                        "row": (source.get("locator") or {}).get("row"),
                        "source": source["provider"],
                        "cited": True,
                    },
                },
                object_id=f"bafin-evidence:{revision_id}",
            )
        )
    builder.add_object(
        "receipt",
        {k: v for k, v in dossier.items() if k != "generation"},
        object_id=f"bafin-dossier:{dossier['dossier_hash'][:32]}",
        references=evidence,
        root=True,
    )
    return builder.build()

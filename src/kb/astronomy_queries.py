"""As-of answers over published astronomy records (#2149, AS08).

Every query takes an ``as_of`` (a date counts as its end, UTC; an ISO time or
epoch milliseconds are exact) and an optional ``acquired_by_ms``, and reads
through :meth:`AstronomyStore.visible`: only what was published by the cutoff
(and acquired by the acquisition cutoff) is answered. Every answer states its
knowledge cutoff and a ``status``:

* ``answered`` - published records exist at the cutoff;
* ``not_yet_published`` - the object is on record, but its first publication
  is after the cutoff (never reported as absence);
* ``unknown`` - nothing on record for it (not acquired or outside the bounds).

Answers quote the publishers: orbit solutions per publisher with epoch, arc,
observation count and solution ID (never averaged or merged across
publishers), dispositions per archive table with the reference and any later
change shown as later, launches with their coded outcome and payloads,
catalogue histories with disagreements side by side, and SWPC products with
cancellations and extensions threaded. Nothing is computed: no orbit,
ephemeris, conjunction, risk verdict, disposition or advice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.kb.astronomy_identity import (
    AstronomyIdentity,
    designation_group,
    exoplanet_group,
    orbital_group,
)
from src.kb.astronomy_records import (
    NOTICE,
    READ_SCOPE,
    AstronomyError,
    authorize,
    cutoffs,
    designation_key,
    digest,
    iso_day,
    iso_time,
    normalise_quantity,
    object_name_key,
)
from src.kb.astronomy_store import AstronomyStore

CONTRACT = "noesis-astronomy-answer-v1"
SEMANTICS = {
    "cutoff": "only records published by the as-of cutoff (a date counts as its end, UTC) and, when given, acquired "
    "by acquired_by_ms; a record without a stated date counts from its first observation",
    "not_yet_published": "the object is on record but first published after the cutoff; this is not absence",
    "exclusions": "no orbit determination, ephemeris, conjunction, risk verdict, disposition or advice by Noesis",
}
SWPC_FAMILIES = {
    "watch": {"watch", "cancel_watch"},
    "warning": {"warning", "extended_warning", "cancel_warning"},
    "alert": {"alert", "cancel_alert"},
    "summary": {"summary", "cancel_summary"},
}


def citation(view: Mapping[str, Any]) -> dict[str, Any]:
    source = view["record"]["source"]
    out = {
        k: source[k]
        for k in (
            "provider",
            "source_record_id",
            "url",
            "locator",
            "attribution",
            "published_at",
            "retrieved_at_ms",
        )
        if k in source
    }
    return {
        **out,
        "record_id": view["record_id"],
        "revision_id": view["revision_id"],
        "revision": int(view["revision"]),
        "public_at_ms": int(view["public_at_ms"]),
        "publication_basis": view["publication_basis"],
    }


def _public_day(ms: int) -> str:
    from src.kb.astronomy_records import observed_day

    return observed_day(ms)


class AstronomyQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = AstronomyStore(conn, initialize=False, now=now)

    # -------------------------------------------------------------- plumbing

    def _visible(self, namespace, scopes, as_of, acquired_by_ms, kinds):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        cut = cutoffs(as_of, acquired_by_ms)
        return cut, self.store.visible(
            namespace,
            kinds=kinds,
            public_cutoff_ms=cut["published_by_ms"],
            acquired_by_ms=acquired_by_ms,
        )

    def _answer(
        self,
        query: str,
        namespace: str,
        cut: Mapping[str, Any],
        body: Mapping[str, Any],
        views: Iterable[Mapping[str, Any]],
        unreadable: list,
        *,
        n: int,
        status: str,
    ) -> dict[str, Any]:
        answer = {
            "contract": CONTRACT,
            "query": query,
            "namespace": namespace,
            "status": status,
            "knowledge_cutoff": dict(cut),
            "n": int(n),
            **body,
            "unreadable": unreadable,
            "semantics": SEMANTICS,
            "notice": NOTICE,
            "pins": dict(
                sorted({v["record_id"]: v["revision_id"] for v in views}.items())
            ),
            "generation": self.store.generation(namespace),
        }
        answer["answer_hash"] = digest(
            {k: v for k, v in answer.items() if k != "generation"}
        )
        return answer

    def _papers(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Science papers the record's stated references resolve to (or unresolved), by exact identifier only."""
        from src.kb.astronomy_citations import AstronomyCitations

        return [
            {
                k: link[k]
                for k in (
                    "citation_kind",
                    "citation_value",
                    "state",
                    "document_id",
                    "document_revision_id",
                    "link_id",
                )
                if k in link
            }
            for link in AstronomyCitations(self.conn, initialize=False).for_records(
                namespace, [record_id]
            )
            if link["state"] != "reverted"
        ]

    @staticmethod
    def _status(
        found: Sequence[Any], pending: Sequence[Mapping[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        if found:
            return "answered", {}
        if pending:
            first = min(p["first_public_at_ms"] for p in pending)
            return "not_yet_published", {"first_published_on": _public_day(first)}
        return "unknown", {
            "reason": "nothing on record for this object: not acquired or outside the declared bounds"
        }

    # -------------------------------------------------------------- small bodies

    def small_body_history(
        self,
        namespace: str,
        designation: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Designations and identifications known at the cutoff, each cited to its publisher and revision."""
        if not designation_key(designation):
            raise AstronomyError(
                "invalid_request", "a designation, number or name is required"
            )
        kinds = ["small_body", "designation", "identification"]
        cut, visible = self._visible(
            namespace, set(scopes), as_of, acquired_by_ms, kinds
        )
        views = visible["records"]
        group = designation_group([v["record"] for v in views], designation)
        mine = [v for v in views if self._in_group(v["record"], group)]
        pending_group = (
            designation_group([p["record"] for p in visible["pending"]], designation)
            | group
        )
        pending = [
            p for p in visible["pending"] if self._in_group(p["record"], pending_group)
        ]
        status, extra = self._status(mine, pending)
        body = {
            "object": designation,
            "designations": sorted(
                (
                    {
                        "designation": v["record"]["designation"],
                        "packed": v["record"].get("packed"),
                        "designation_kind": v["record"]["designation_kind"],
                        "object_designation": v["record"].get("object_designation"),
                        "assigned_at": v["record"].get("assigned_at"),
                        "citation": citation(v),
                    }
                    for v in mine
                    if v["kind"] == "designation"
                ),
                key=lambda d: d["designation"],
            ),
            "identifications": sorted(
                (
                    {
                        "designation": v["record"]["designation"],
                        "identified_with": v["record"]["identified_with"],
                        "permanent": v["record"].get("permanent"),
                        "announced_in": v["record"].get("announced_in"),
                        "announced_on": v["record"].get("announced_on"),
                        "papers": self._papers(namespace, v["record_id"]),
                        "unknowns": v["record"]["unknowns"],
                        "citation": citation(v),
                    }
                    for v in mine
                    if v["kind"] == "identification"
                ),
                key=lambda d: d["designation"],
            ),
            "objects": sorted(
                (
                    {
                        "provider": v["record"]["source"]["provider"],
                        **{
                            k: v["record"][k]
                            for k in (
                                "primary_designation",
                                "number",
                                "name",
                                "stated_designations",
                                "spk_id",
                            )
                            if k in v["record"]
                        },
                        "citation": citation(v),
                    }
                    for v in mine
                    if v["kind"] == "small_body"
                ),
                key=lambda d: (d["provider"], d["primary_designation"]),
            ),
            "linked_designations": sorted(group),
            **extra,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self._answer(
            "small_body_history",
            namespace,
            cut,
            body,
            mine,
            visible["unreadable"],
            n=len(mine),
            status=status,
        )

    @staticmethod
    def _in_group(record: Mapping[str, Any], group: set[str]) -> bool:
        from src.kb.astronomy_records import object_keys

        return bool({k.removeprefix("des:") for k in object_keys(record)} & group)

    def orbit_solution_as_of(
        self,
        namespace: str,
        designation: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        publisher: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The latest solution each publisher had published by the cutoff, side by side, never averaged."""
        if publisher is not None and publisher not in {"MPC", "JPL"}:
            raise AstronomyError("invalid_request", "publisher is MPC or JPL")
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of,
            acquired_by_ms,
            ["small_body", "designation", "identification", "orbit_solution"],
        )
        views = visible["records"]
        group = designation_group([v["record"] for v in views], designation)
        solutions = [
            v
            for v in views
            if v["kind"] == "orbit_solution" and self._in_group(v["record"], group)
        ]
        pending = [
            p
            for p in visible["pending"]
            if p["kind"] == "orbit_solution"
            and designation_key(p["record"]["object_designation"])
            in group | {designation_key(designation)}
        ]
        by_publisher: dict[str, list[Mapping[str, Any]]] = {}
        for view in solutions:
            by_publisher.setdefault(view["record"]["publisher"], []).append(view)

        def shown(view: Mapping[str, Any]) -> dict[str, Any]:
            record = view["record"]
            return {
                "publisher": record["publisher"],
                "solution_id": record["solution_id"],
                "object_designation": record["object_designation"],
                "epoch": record["epoch"],
                "arc": record.get("arc"),
                "n_obs_used": record.get("n_obs_used"),
                "uncertainty": record.get("uncertainty"),
                "computed_at": record.get("computed_at"),
                "elements": {
                    name: normalise_quantity(q["value"], q.get("unit"))
                    | ({"sigma": q["sigma"]} if "sigma" in q else {})
                    for name, q in sorted(record["elements"].items())
                },
                "unknowns": record["unknowns"],
                "citation": citation(view),
            }

        latest = {}
        for name, items in sorted(by_publisher.items()):
            ordered = sorted(
                items,
                key=lambda v: (
                    v["public_at_ms"],
                    v["record"].get("computed_at") or "",
                    v["record_id"],
                ),
            )
            latest[name] = {
                "current": {
                    k: v for k, v in shown(ordered[-1]).items() if v is not None
                },
                "earlier_vintages": [
                    {
                        "solution_id": v["record"]["solution_id"],
                        "epoch": v["record"]["epoch"],
                        "citation": citation(v),
                    }
                    for v in ordered[:-1]
                ],
            }
        selected = (
            [latest[publisher]]
            if publisher and publisher in latest
            else []
            if publisher
            else list(latest.values())
        )
        status, extra = self._status(selected, pending)
        body = {
            "object": designation,
            "linked_designations": sorted(group),
            "solutions": {publisher: latest[publisher]}
            if publisher and publisher in latest
            else ({} if publisher else latest),
            "other_publishers": {
                k: v for k, v in latest.items() if publisher and k != publisher
            },
            "policy": "solutions are quoted per publisher and solution ID; values from different solutions are "
            "never averaged or merged",
            **extra,
        }
        return self._answer(
            "orbit_solution_as_of",
            namespace,
            cut,
            body,
            solutions,
            visible["unreadable"],
            n=len(solutions),
            status=status,
        )

    def impact_risk_listing_as_of(
        self,
        namespace: str,
        designation: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The published risk listing (quoted) at the cutoff; Noesis issues no verdict."""
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of,
            acquired_by_ms,
            ["small_body", "designation", "identification", "impact_risk_listing"],
        )
        views = visible["records"]
        group = designation_group([v["record"] for v in views], designation)
        listings = [
            v
            for v in views
            if v["kind"] == "impact_risk_listing" and self._in_group(v["record"], group)
        ]
        status, extra = self._status(listings, [])
        body = {
            "object": designation,
            "listings": [
                {
                    k: v["record"][k]
                    for k in (
                        "publisher",
                        "listing_status",
                        "listing_date",
                        "removed_at",
                        "figures",
                        "method",
                    )
                    if k in v["record"]
                }
                | {"citation": citation(v)}
                for v in listings
            ],
            "policy": "published figures quoted with their date; no risk verdict",
            **extra,
        }
        return self._answer(
            "impact_risk_listing_as_of",
            namespace,
            cut,
            body,
            listings,
            visible["unreadable"],
            n=len(listings),
            status=status,
        )

    # -------------------------------------------------------------- exoplanets

    def exoplanet_status_as_of(
        self,
        namespace: str,
        planet: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The archive's disposition per table at the cutoff, with its reference and later changes shown as later."""
        if not object_name_key(planet):
            raise AstronomyError(
                "invalid_request", "a planet name, TOI or KOI is required"
            )
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of,
            acquired_by_ms,
            ["exoplanet", "exoplanet_status_assertion"],
        )
        accepted = AstronomyIdentity(self.conn, initialize=False).accepted_pairs(
            namespace
        )
        views = visible["records"]
        all_records = [v["record"] for v in views] + [
            p["record"] for p in visible["pending"]
        ]
        group = exoplanet_group(all_records, planet, accepted)
        from src.kb.astronomy_identity import _planet_keys

        def mine(record):
            return bool(set(_planet_keys(record)) & group)

        assertions = [
            v
            for v in views
            if v["kind"] == "exoplanet_status_assertion" and mine(v["record"])
        ]
        pending = [
            p
            for p in visible["pending"]
            if p["kind"] == "exoplanet_status_assertion" and mine(p["record"])
        ]
        rows = []
        for view in sorted(
            assertions,
            key=lambda v: (v["record"]["source_table"], v["record"]["object_name"]),
        ):
            record = view["record"]
            later = []
            if view["later"]:
                history = {
                    r["revision_id"]: r
                    for r in self.store.history(namespace, view["record_id"])
                }
                for item in view["later"]:
                    after = history.get(item["revision_id"], {}).get("record") or {}
                    later.append(
                        {
                            "disposition": after.get("disposition"),
                            "native_disposition": after.get("native_disposition"),
                            "asserted_at": after.get("asserted_at"),
                            "published_on": _public_day(item["public_at_ms"]),
                            "revision_id": item["revision_id"],
                        }
                    )
            rows.append(
                {
                    k: v
                    for k, v in {
                        "object_name": record["object_name"],
                        "object_scheme": record["object_scheme"],
                        "source_table": record["source_table"],
                        "disposition": record.get("disposition"),
                        "native_disposition": record["native_disposition"],
                        "reference": record.get("reference"),
                        "asserted_at": record.get("asserted_at"),
                        "note": record.get("note"),
                        "listing": {
                            k: v for k, v in view["listing"].items() if k != "history"
                        },
                        "later_changes": later,
                        "papers": self._papers(namespace, view["record_id"]),
                        "unknowns": record["unknowns"],
                        "citation": citation(view),
                    }.items()
                    if v is not None
                }
            )
        status, extra = self._status(rows, pending)
        body = {
            "planet": planet,
            "linked_identifiers": sorted(group),
            "dispositions": rows,
            "policy": "the archive's disposition per table is quoted; Noesis validates or dispositions nothing",
            **extra,
        }
        return self._answer(
            "exoplanet_status_as_of",
            namespace,
            cut,
            body,
            assertions,
            visible["unreadable"],
            n=len(rows),
            status=status,
        )

    # -------------------------------------------------------------- launches and objects

    def launches(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        provider: str | None = None,
        site: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Launches by provider (GCAT agency code), site code or window, with coded outcomes and payloads."""
        if not (provider or site or (date_from and date_to)):
            raise AstronomyError(
                "invalid_request", "give a provider, a site or a date window"
            )
        for value in (date_from, date_to):
            if value is not None and iso_day(value) != value:
                raise AstronomyError("invalid_request", "window dates are ISO dates")
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of if as_of is not None else "9999-12-31",
            acquired_by_ms,
            ["launch", "launch_outcome", "orbital_object", "space_organisation"],
        )
        views = visible["records"]
        identity = AstronomyIdentity(self.conn, initialize=False)
        outcomes = {
            v["record"]["launch_tag"]: v for v in views if v["kind"] == "launch_outcome"
        }
        orgs = {
            v["record"]["code"].upper(): v
            for v in views
            if v["kind"] == "space_organisation"
        }
        payloads: dict[str, list[Mapping[str, Any]]] = {}
        for view in views:
            if view["kind"] == "orbital_object" and view["record"].get("launch_tag"):
                payloads.setdefault(view["record"]["launch_tag"], []).append(view)
        selected = []
        for view in views:
            record = view["record"]
            if view["kind"] != "launch":
                continue
            if provider and provider.upper() not in {
                c.upper() for c in record.get("agency_codes") or []
            }:
                continue
            if site and (record.get("site_code") or "").upper() != site.upper():
                continue
            day = (record.get("time") or "")[:10]
            if date_from and (not day or day < date_from):
                continue
            if date_to and (not day or day > date_to):
                continue
            selected.append(view)
        rows, used = [], list(selected)
        for view in sorted(
            selected,
            key=lambda v: (v["record"].get("time") or "", v["record"]["launch_tag"]),
        ):
            record = view["record"]
            outcome = outcomes.get(record["launch_tag"])
            used += [outcome] if outcome else []
            items = sorted(
                payloads.get(record["launch_tag"], []),
                key=lambda v: v["record"].get("cospar") or "",
            )
            used += items
            rows.append(
                {
                    k: v
                    for k, v in {
                        "launch_tag": record["launch_tag"],
                        "time": record.get("time"),
                        "stated_time": record.get("stated_time"),
                        "vehicle": record.get("vehicle"),
                        "mission": record.get("mission"),
                        "native_code": record.get("native_code"),
                        "outcome": (
                            {
                                "outcome": outcome["record"].get("outcome"),
                                "native_code": outcome["record"]["native_code"],
                                "citation": citation(outcome),
                            }
                            if outcome
                            else {
                                "outcome": None,
                                "reason": "no outcome published by the cutoff",
                            }
                        ),
                        "providers": [
                            {
                                "code": code,
                                "name": orgs[code]["record"]["name"]
                                if code in orgs
                                else None,
                                **identity.organisation(namespace, code),
                            }
                            for code in record.get("agency_codes") or []
                        ],
                        "site": {
                            "code": record.get("site_code"),
                            "name": orgs[record["site_code"].upper()]["record"]["name"]
                            if record.get("site_code")
                            and record["site_code"].upper() in orgs
                            else None,
                            **identity.site(namespace, record.get("site_code") or ""),
                        },
                        "payloads": [
                            {
                                k: p["record"].get(k)
                                for k in (
                                    "name",
                                    "cospar",
                                    "norad",
                                    "jcat",
                                    "object_type",
                                    "status",
                                    "decay_date",
                                )
                                if p["record"].get(k)
                            }
                            | {"citation": citation(p)}
                            for p in items
                        ],
                        "unknowns": record["unknowns"],
                        "citation": citation(view),
                    }.items()
                    if v is not None
                }
            )
        status, extra = self._status(rows, [])
        body = {
            "filters": {
                k: v
                for k, v in (
                    ("provider", provider),
                    ("site", site),
                    ("date_from", date_from),
                    ("date_to", date_to),
                )
                if v
            },
            "launches": rows,
            **extra,
        }
        return self._answer(
            "launches",
            namespace,
            cut,
            body,
            used,
            visible["unreadable"],
            n=len(rows),
            status=status,
        )

    def orbital_object_history(
        self,
        namespace: str,
        identifier: str,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Catalogue history (launch, status, decay) per catalogue, disagreements side by side."""
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of if as_of is not None else "9999-12-31",
            acquired_by_ms,
            ["orbital_object"],
        )
        views = visible["records"]
        group = orbital_group([v["record"] for v in views], identifier)
        if not group["keys"]:
            raise AstronomyError(
                "invalid_request",
                "a COSPAR designator, NORAD number or GCAT JCAT is required",
            )
        mine = [
            v
            for v in views
            if {
                f"{p}:{v['record'][p]}"
                for p in ("cospar", "norad", "jcat")
                if v["record"].get(p)
            }
            & group["keys"]
        ]
        entries = []
        for view in sorted(
            mine, key=lambda v: (v["record"]["source"]["provider"], v["record_id"])
        ):
            revisions = [
                r
                for r in self.store.history(namespace, view["record_id"])
                if int(r["revision"]) <= int(view["revision"])
                and r["change"] != "history"
            ]
            entries.append(
                {
                    "provider": view["record"]["source"]["provider"],
                    "current": {
                        k: view["record"][k]
                        for k in (
                            "name",
                            "cospar",
                            "norad",
                            "jcat",
                            "object_type",
                            "owner",
                            "status",
                            "launch_date",
                            "decay_date",
                            "stated_decay_date",
                            "launch_tag",
                            "site",
                        )
                        if k in view["record"]
                    },
                    "revisions": [
                        {
                            "revision": int(r["revision"]),
                            "revision_id": r["revision_id"],
                            "status": r["record"].get("status"),
                            "decay_date": r["record"].get("decay_date"),
                            "observed_at_ms": int(r["observed_at_ms"]),
                        }
                        for r in revisions
                    ],
                    "citation": citation(view),
                }
            )
        decay = {
            e["provider"]: e["current"].get("decay_date")
            for e in entries
            if e["current"].get("decay_date")
        }
        body = {
            "identifier": identifier,
            "linked_identifiers": sorted(group["keys"]),
            "catalogues": entries,
            "conflicts": group["conflicts"],
            "disagreements": (
                {"decay_date": decay} if len(set(decay.values())) > 1 else {}
            ),
            "policy": "each catalogue quoted as published; no positions or conjunctions are computed",
        }
        status, extra = self._status(entries, [])
        return self._answer(
            "orbital_object_history",
            namespace,
            cut,
            {**body, **extra},
            mine,
            visible["unreadable"],
            n=len(entries),
            status=status,
        )

    # -------------------------------------------------------------- space weather

    def space_weather_alerts(
        self,
        namespace: str,
        window_from: str,
        window_to: str,
        *,
        scopes: Iterable[str],
        product_type: str | None = None,
        scale: str | None = None,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """SWPC products issued in the window as issued, cancellations and extensions threaded."""
        start = iso_time(window_from) or (
            iso_day(window_from) and iso_day(window_from) + "T00:00:00Z"
        )
        end = iso_time(window_to) or (
            iso_day(window_to) and iso_day(window_to) + "T23:59:59Z"
        )
        if not start or not end or start > end:
            raise AstronomyError(
                "invalid_request",
                "the window is two ISO dates or times, start before end",
            )
        if product_type is not None and product_type not in SWPC_FAMILIES:
            raise AstronomyError(
                "invalid_request", f"product_type is one of {sorted(SWPC_FAMILIES)}"
            )
        if scale is not None and not (
            len(scale) == 2 and scale[0] in "GSR" and scale[1] in "12345"
        ):
            raise AstronomyError(
                "invalid_request", "scale is a NOAA scale level such as G2"
            )
        cut, visible = self._visible(
            namespace,
            set(scopes),
            as_of if as_of is not None else end,
            acquired_by_ms,
            ["space_weather_product"],
        )
        views = visible["records"]
        by_serial = {v["record"]["serial"]: v for v in views}
        threads: dict[str, dict[str, list[str]]] = {}
        for view in views:
            for relation, serial in (view["record"].get("references") or {}).items():
                back = "cancelled_by" if relation == "cancels" else "extended_by"
                threads.setdefault(serial, {}).setdefault(back, []).append(
                    view["record"]["serial"]
                )
        rows, used = [], []
        for view in sorted(
            views, key=lambda v: (v["record"]["issue_time"], v["record"]["serial"])
        ):
            record = view["record"]
            if not start <= record["issue_time"] <= end:
                continue
            if (
                product_type
                and record["product_kind"] not in SWPC_FAMILIES[product_type]
            ):
                continue
            if scale and not any(
                s[0] == scale[0] and s[1] >= scale[1]
                for s in record.get("scales") or []
            ):
                continue
            used.append(view)
            references = {
                rel: {"serial": serial, "in_record": serial in by_serial}
                for rel, serial in (record.get("references") or {}).items()
            }
            rows.append(
                {
                    k: v
                    for k, v in {
                        "serial": record["serial"],
                        "product_id": record["product_id"],
                        "message_code": record.get("message_code"),
                        "product_kind": record["product_kind"],
                        "issue_time": record["issue_time"],
                        "scales": record.get("scales"),
                        "title": record.get("title"),
                        "references": references or None,
                        "thread": threads.get(record["serial"]) or None,
                        "message": record["message"],
                        "citation": citation(view),
                    }.items()
                    if v is not None
                }
            )
        status, extra = self._status(rows, [])
        body = {
            "window": {"from": start, "to": end},
            "product_type": product_type,
            "scale": scale,
            "products": rows,
            "policy": "messages quoted verbatim as issued; no operational advice",
            **extra,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self._answer(
            "space_weather_alerts",
            namespace,
            cut,
            body,
            used,
            visible["unreadable"],
            n=len(rows),
            status=status,
        )

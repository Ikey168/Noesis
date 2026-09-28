"""Normalise codes, places and currencies of development-finance records without merging publishers (#1932, D06).

* **Code lists** - the DAC sector, purpose, channel, flow-type and aid-type
  lists and the IATI transaction-type, organisation-role and activity-status
  lists are published as ontology modules through
  :class:`src.kb.ontology.OntologyAlignmentStore`, one module per list and
  versioned by the list's release. Normalisation reads the published module;
  a code that is not in it stays the reported string with an ``unknown-code``
  flag, and a list that is not published leaves the code ``codelist-unavailable``.
* **Places** - recipient countries (ISO 3166-1 alpha-2) resolve through
  ``geospatial.place-resolution`` to the one Geospatial place whose source
  identifiers carry the code, recorded as a ``geocode_resolutions`` row;
  several places stay ambiguous and none stays unresolved. DAC regional codes
  are aggregates of countries and are never resolved or apportioned. An
  activity's own ``location`` elements are free text: they are saved as
  resolutions pending review and count only once a ``geocode_reviews`` decision
  accepts a place. A location never replaces the recipient-country allocation.
* **Currencies** - a converted amount is stored only in a side record beside
  the original value, currency and value date, with the rate, the rate date,
  the rate source and a citation. No rate is assumed: without a citable rate
  there is no conversion. Arithmetic is exact decimal.

Every derived field is written to a side record keyed by the revision it was
derived from; a publisher's reported values are never edited, and the same
activity reported by two publishers yields two normalised records.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from src.ingestion.development_finance_codes import (
    CODELISTS,
    FIELD_LISTS,
    REGION_VOCABULARIES,
    SECTOR_VOCABULARIES,
    SUBSET_NOTE,
    concepts,
    semver,
)
from src.ingestion.development_finance_sources import iso_day
from src.kb.development_finance import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

NORMALISER_VERSION = "development-finance-normalise:1.0.0"
OWNER = "funding.development-finance"
SCHEMA_READ = "knowledge:schema:read"
SCHEMA_REGISTER = "knowledge:schema:register"
GEO_READ = "knowledge:geospatial:read"
GEO_WRITE = "knowledge:geospatial:write"
GEO_REVIEW = "knowledge:geospatial:review"
PLACE_SYSTEM = "iso3166-1-alpha2"

_DDL = """
CREATE TABLE IF NOT EXISTS devfin_normalisations (
  namespace TEXT NOT NULL, normalisation_id TEXT NOT NULL, revision_id TEXT NOT NULL, normaliser TEXT NOT NULL,
  codelists_json TEXT NOT NULL, body_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, normalisation_id)
);
CREATE TABLE IF NOT EXISTS devfin_place_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, reference_kind TEXT NOT NULL, scheme TEXT, code TEXT,
  mention TEXT, state TEXT NOT NULL, reason TEXT, resolution_id TEXT, geo_namespace TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, evaluation_no BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
CREATE TABLE IF NOT EXISTS devfin_conversions (
  namespace TEXT NOT NULL, conversion_id TEXT NOT NULL, transaction_id TEXT NOT NULL, original_value TEXT NOT NULL,
  original_currency TEXT NOT NULL, value_date TEXT NOT NULL, target_currency TEXT NOT NULL, rate TEXT NOT NULL,
  rate_date TEXT NOT NULL, rate_source TEXT NOT NULL, citation TEXT NOT NULL, converted_value TEXT NOT NULL,
  flags_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, conversion_id)
);
"""


class DevelopmentFinanceNormaliser:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.store = DevelopmentFinanceStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ code lists

    def publish_codelists(
        self, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Publish every bundled code list as an ontology module (idempotent per list and release)."""
        from src.kb.ontology import OntologyAlignmentStore

        scopes = set(scopes)
        require_scope(scopes, SCHEMA_REGISTER)
        ontology = OntologyAlignmentStore(self.conn, now=self.now)
        published = {}
        for name, spec in sorted(CODELISTS.items()):
            items = concepts(name)
            version = semver(spec["release"])
            module = ontology.publish(
                f"devfin-{name}",
                version,
                items,
                owner=OWNER,
                provenance={
                    "kind": "builtin",
                    "source": f"{spec['source']}, release {spec['release']} ({SUBSET_NOTE})",
                },
                idempotency_key=f"devfin-{name}:{version}:{digest(items)[:16]}",
                principal_id=principal_id,
                scopes={SCHEMA_REGISTER, SCHEMA_READ},
                compatibility_policy="none",
            )
            published[name] = {
                "module": f"devfin-{name}",
                "version": version,
                "release": spec["release"],
                "module_id": module["module_id"],
                "concepts": len(items),
            }
        return {"codelists": published, "note": SUBSET_NOTE}

    def _modules(self) -> dict[str, dict[str, Any]]:
        """The newest published version of each development-finance code list (name -> version and codes)."""
        if not table_exists(self.conn, "knowledge_schema_modules"):
            return {}
        from src.kb.schema_registry import SchemaRegistry, SchemaRegistryError

        registry = SchemaRegistry(self.conn, initialize=False)
        out = {}
        for name in CODELISTS:
            try:
                module = registry.resolve(
                    "ontology", f"devfin-{name}", "*", scopes={SCHEMA_READ}
                )
            except SchemaRegistryError:
                continue
            out[name] = {
                "version": module["semantic_version"],
                "module_id": module["module_id"],
                "codes": {
                    c["concept_id"]: c["labels"][0]["value"]
                    for c in module["content"]["concepts"]
                },
            }
        return out

    @staticmethod
    def code(
        modules: Mapping[str, Any], list_name: str | None, code: Any
    ) -> dict[str, Any]:
        reported = None if code in (None, "") else str(code)
        if reported is None:
            return {"reported": None, "state": "absent"}
        if list_name is None:
            return {"reported": reported, "state": "vocabulary-not-normalised"}
        module = modules.get(list_name)
        if module is None:
            return {
                "reported": reported,
                "list": list_name,
                "state": "codelist-unavailable",
            }
        if reported not in module["codes"]:
            return {
                "reported": reported,
                "list": list_name,
                "version": module["version"],
                "state": "unknown-code",
                "flag": "unknown-code",
            }
        return {
            "reported": reported,
            "list": list_name,
            "version": module["version"],
            "state": "known",
            "label": module["codes"][reported],
        }

    def _sector(self, modules, allocation) -> dict[str, Any]:
        list_name = SECTOR_VOCABULARIES.get(allocation.get("vocabulary"))
        result = self.code(modules, list_name, allocation.get("code"))
        if (
            list_name == "dac-purpose-code"
            and result["state"] == "known"
            and len(str(allocation["code"])) == 5
        ):
            # A purpose code's category is its first three digits (the DAC code structure).
            result["category"] = self.code(
                modules, "dac-sector-category", str(allocation["code"])[:3]
            )
        return {
            **result,
            "percentage": allocation.get("percentage"),
            "percentage_text": allocation.get("percentage_text"),
        }

    @staticmethod
    def _sum_check(
        allocations: Sequence[Mapping[str, Any]], label: str, group: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """One IATI percentage group: the reported percentages must add up to 100.

        A group with one member and no percentage is 100% by the standard's convention. A member without a
        percentage in a larger group, or an unreadable percentage, is flagged; nothing is rescaled.
        """
        if not allocations:
            return None
        base = {"allocation": label, "group": dict(group)}
        if len(allocations) == 1 and allocations[0].get("percentage_text") is None:
            return {
                **base,
                "sum": "100",
                "state": "sums-to-100",
                "basis": "single member, implied 100%",
            }
        if any(a.get("percentage_text") is None for a in allocations):
            return {
                **base,
                "state": "percentage-missing",
                "flag": "allocation-percentage-missing",
                "note": "several members and at least one without a percentage; kept as reported",
            }
        if any(a.get("percentage") is None for a in allocations):
            return {
                **base,
                "state": "unreadable-percentage",
                "flag": "allocation-percentage-unreadable",
                "reported": [a.get("percentage_text") for a in allocations],
            }
        total = sum((Decimal(a["percentage"]) for a in allocations), Decimal(0))
        return {
            **base,
            "sum": str(total.normalize())
            if total != total.to_integral_value()
            else str(total.to_integral_value()),
            "state": "sums-to-100" if total == 100 else "does-not-sum-to-100",
            **(
                {}
                if total == 100
                else {
                    "flag": "allocation-sum-not-100",
                    "note": "kept as reported; nothing is rescaled",
                }
            ),
        }

    def _allocation_checks(self, activity: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The IATI 2.03 percentage rules (verify against the published 2.03 ruleset).

        * sectors: the percentages of the sectors *of each vocabulary* add up to 100 (a DAC list and an SDG list
          are two separate allocations of the same activity);
        * recipients: recipient countries and recipient regions are one allocation; with regions of several
          vocabularies, the countries plus the regions of each region vocabulary add up to 100.
        """
        checks = []
        sectors: dict[str, list[Mapping[str, Any]]] = {}
        for sector in activity.get("sectors") or []:
            sectors.setdefault(sector.get("vocabulary") or "1", []).append(sector)
        for vocabulary, members in sorted(sectors.items()):
            checks.append(
                self._sum_check(members, "sector", {"vocabulary": vocabulary})
            )
        countries = list(activity.get("recipient_countries") or [])
        regions: dict[str, list[Mapping[str, Any]]] = {}
        for region in activity.get("recipient_regions") or []:
            regions.setdefault(region.get("vocabulary") or "1", []).append(region)
        if regions:
            for vocabulary, members in sorted(regions.items()):
                checks.append(
                    self._sum_check(
                        countries + members,
                        "recipient",
                        {"region_vocabulary": vocabulary},
                    )
                )
        else:
            checks.append(
                self._sum_check(countries, "recipient", {"region_vocabulary": None})
            )
        return [c for c in checks if c is not None]

    def normalise(
        self, namespace: str, revision_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """A side record of normalised codes and checks for one revision; the revision itself is never edited."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        revision = self.store.revision(namespace, revision_id)
        activity = revision["activity"]
        modules = self._modules()
        versions = {k: v["version"] for k, v in sorted(modules.items())}
        transactions = []
        for tx in revision["transactions"]:
            flags = []
            if tx["value"] is None:
                flags.append("value-unknown")
            if tx["currency"] is None:
                flags.append("currency-unknown")
            if tx["value_date"] is None:
                flags.append("value-date-unknown")
            transactions.append(
                {
                    "transaction_id": tx["transaction_id"],
                    "type": self.code(
                        modules, FIELD_LISTS["transaction_type"], tx["type"]
                    ),
                    "flow_type": self.code(
                        modules, FIELD_LISTS["flow_type"], tx.get("flow_type")
                    ),
                    "aid_type": self.code(
                        modules,
                        FIELD_LISTS["aid_type"],
                        (tx.get("aid_type") or {}).get("code"),
                    ),
                    "sectors": [self._sector(modules, s) for s in tx["sectors"]],
                    "amount": {
                        "value": tx["value"],
                        "currency": tx["currency"],
                        "value_date": tx["value_date"],
                        "state": "unknown" if flags else "reported",
                        "flags": flags,
                        "note": "no conversion without a cited rate and date",
                    },
                }
            )
        regions = activity.get("recipient_regions") or []
        body = {
            "revision_id": revision_id,
            "activity_key": revision["activity_key"],
            "publisher_id": revision["publisher_id"],
            "activity_status": self.code(
                modules, FIELD_LISTS["activity_status"], activity.get("activity_status")
            ),
            "default_flow_type": self.code(
                modules, FIELD_LISTS["flow_type"], activity.get("default_flow_type")
            ),
            "default_aid_type": self.code(
                modules,
                FIELD_LISTS["aid_type"],
                (activity.get("default_aid_type") or {}).get("code"),
            ),
            "participating_orgs": [
                {
                    "ordinal": o["ordinal"],
                    "ref": o.get("ref"),
                    "name": o.get("name"),
                    "role": self.code(
                        modules, FIELD_LISTS["organisation_role"], o.get("role")
                    ),
                    "crs_channel": self.code(
                        modules, FIELD_LISTS["channel"], o.get("crs_channel_code")
                    ),
                }
                for o in activity.get("participating_orgs") or []
            ],
            "sectors": [
                self._sector(modules, s) for s in activity.get("sectors") or []
            ],
            "recipient_countries": [
                {
                    "reported": c.get("code"),
                    "percentage": c.get("percentage"),
                    "scheme": PLACE_SYSTEM,
                }
                for c in activity.get("recipient_countries") or []
            ],
            "recipient_regions": [
                {
                    **self.code(
                        modules,
                        REGION_VOCABULARIES.get(r.get("vocabulary")),
                        r.get("code"),
                    ),
                    "percentage": r.get("percentage"),
                    "kind": "aggregate",
                    "note": "a regional code is an aggregate of countries; never resolved or apportioned",
                }
                for r in regions
            ],
            "checks": self._allocation_checks(activity),
            "transactions": transactions,
            "normaliser": NORMALISER_VERSION,
            "codelist_versions": versions,
        }
        normalisation_id = (
            "devfin-norm:"
            + digest([namespace, revision_id, NORMALISER_VERSION, versions])[:24]
        )
        created = self.conn.execute(
            "INSERT INTO devfin_normalisations VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING "
            "RETURNING normalisation_id",
            [
                namespace,
                normalisation_id,
                revision_id,
                NORMALISER_VERSION,
                canonical(versions),
                canonical(body),
                self.now(),
            ],
        ).fetchone()
        return {"normalisation_id": normalisation_id, "created": bool(created), **body}

    def normalise_current(
        self, namespace: str, *, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Normalise each publisher's current revision (every publisher separately)."""
        return [
            self.normalise(namespace, revision["revision_id"], scopes=scopes)
            for revision in self.store.current(namespace).values()
        ]

    def normalisation(
        self, namespace: str, revision_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any] | None:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "devfin_normalisations"):
            return None
        row = self.conn.execute(
            "SELECT normalisation_id, body_json FROM devfin_normalisations WHERE namespace=? AND revision_id=? "
            "ORDER BY created_at_ms DESC, normalisation_id DESC LIMIT 1",
            [namespace, revision_id],
        ).fetchone()
        return (
            None if row is None else {"normalisation_id": row[0], **json.loads(row[1])}
        )

    # ------------------------------------------------------------------ places

    def _places(self, geo_namespace: str, code: str) -> list[tuple[str, str]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, p.namespace, r.source_ids_json FROM geospatial_places p JOIN geospatial_place_current c "
            "ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON r.revision_id=c.revision_id "
            "WHERE p.namespace IN (?, 'global') ORDER BY p.place_id",
            [geo_namespace],
        ).fetchall()
        return [
            (place_id, namespace)
            for place_id, namespace, source_ids in rows
            if str(json.loads(source_ids or "{}").get(PLACE_SYSTEM) or "")
            .strip()
            .upper()
            == code
        ]

    def resolve_places(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str = "global",
    ) -> dict[str, Any]:
        """Resolve recipient countries by code and save free-text locations for review; idempotent."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        require_scope(scopes, GEO_WRITE)
        geo = GeospatialStore(self.conn, now=self.now)
        countries, regions, locations = set(), set(), set()
        for revision in self.store.current(namespace).values():
            activity = revision["activity"]
            for allocation in activity.get("recipient_countries") or []:
                if allocation.get("code"):
                    countries.add(str(allocation["code"]).strip().upper())
            for tx in activity.get("transactions") or []:
                for allocation in tx.get("recipient_countries") or []:
                    if allocation.get("code"):
                        countries.add(str(allocation["code"]).strip().upper())
            for allocation in activity.get("recipient_regions") or []:
                if allocation.get("code"):
                    regions.add(str(allocation["code"]))
            for location in activity.get("locations") or []:
                if location.get("name"):
                    locations.add(str(location["name"]))
        links = []
        for code in sorted(countries):
            places = self._places(geo_namespace, code)
            state = (
                "resolved"
                if len(places) == 1
                else "ambiguous"
                if places
                else "unresolved"
            )
            reason = (
                None
                if state == "resolved"
                else (
                    "more than one place carries this code"
                    if places
                    else "no place carries this ISO 3166-1 code"
                )
            )
            candidates = [
                {
                    "place_id": p,
                    "namespace": ns,
                    "confidence": 1.0,
                    "reasons": [f"{PLACE_SYSTEM}:{code}"],
                }
                for p, ns in places
            ]
            request = {
                "namespace": geo_namespace,
                "mention": f"{PLACE_SYSTEM}:{code}",
                "system": PLACE_SYSTEM,
                # A saved resolution is immutable: a changed candidate set is a new resolution, never the old one.
                "candidates": sorted(p for p, _ in places),
            }
            input_hash = digest(request)
            saved = geo.save_resolution(
                {
                    "resolution_id": "geocode-resolution:" + input_hash[:24],
                    "namespace": geo_namespace,
                    "mention": f"{PLACE_SYSTEM}:{code}",
                    "context": {
                        "system": PLACE_SYSTEM,
                        "code": code,
                        "producer": OWNER,
                    },
                    "candidates": candidates,
                    "status": state,
                    "selected_place_id": places[0][0] if state == "resolved" else None,
                    "confidence": 1.0 if state == "resolved" else 0.0,
                    "evidence": [],
                    "method": {
                        "name": "development-finance-code",
                        "version": "1",
                        "system": PLACE_SYSTEM,
                    },
                    "input_hash": input_hash,
                },
                principal_id=principal_id,
                scopes={GEO_WRITE},
            )
            links.append(
                self._link(
                    namespace,
                    "recipient-country",
                    PLACE_SYSTEM,
                    code,
                    None,
                    state,
                    reason,
                    saved["resolution_id"],
                    geo_namespace,
                )
            )
        for code in sorted(regions):
            links.append(
                self._link(
                    namespace,
                    "recipient-region",
                    "dac-recipient-region",
                    code,
                    None,
                    "aggregate",
                    "a DAC regional code is an aggregate of countries; never resolved or apportioned",
                    None,
                    geo_namespace,
                )
            )
        for mention in sorted(locations):
            probe = geo.resolve(
                geo_namespace, mention, scopes={GEO_READ}, context={"producer": OWNER}
            )
            # The candidate set is part of the resolution's identity, so a later evaluation that finds new
            # candidates saves a new reviewable resolution instead of returning the old empty one.
            result = geo.resolve(
                geo_namespace,
                mention,
                scopes={GEO_READ},
                context={
                    "producer": OWNER,
                    "candidates": sorted(c["place_id"] for c in probe["candidates"]),
                },
            )
            saved = geo.save_resolution(
                result, principal_id=principal_id, scopes={GEO_WRITE}
            )
            links.append(
                self._link(
                    namespace,
                    "location",
                    None,
                    None,
                    mention,
                    "pending-review",
                    "a free-text location counts only after a reviewer accepts a place",
                    saved["resolution_id"],
                    geo_namespace,
                )
            )
        return {"links": links}

    def _link(
        self,
        namespace,
        kind,
        scheme,
        code,
        mention,
        state,
        reason,
        resolution_id,
        geo_namespace,
    ):
        outcome = [
            kind,
            scheme,
            code,
            mention,
            state,
            reason,
            resolution_id,
            geo_namespace,
        ]
        latest = self.conn.execute(
            "SELECT link_id, state, reason, resolution_id, geo_namespace FROM devfin_place_links WHERE namespace=? "
            "AND reference_kind=? AND scheme IS NOT DISTINCT FROM ? AND code IS NOT DISTINCT FROM ? "
            "AND mention IS NOT DISTINCT FROM ? ORDER BY evaluation_no DESC LIMIT 1",
            [namespace, kind, scheme, code, mention],
        ).fetchone()
        if latest is not None and list(latest[1:]) == [
            state,
            reason,
            resolution_id,
            geo_namespace,
        ]:
            link_id = latest[0]  # the latest evaluation already says this
        else:
            # Every changed outcome is a new evaluation (A -> B -> A included), ordered by its number.
            number = 1 + int(
                self.conn.execute(
                    "SELECT coalesce(max(evaluation_no), 0) FROM devfin_place_links WHERE namespace=?",
                    [namespace],
                ).fetchone()[0]
            )
            link_id = "devfin-place:" + digest([namespace, *outcome, number])[:24]
            self.conn.execute(
                "INSERT INTO devfin_place_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    link_id,
                    kind,
                    scheme,
                    code,
                    mention,
                    state,
                    reason,
                    resolution_id,
                    geo_namespace,
                    self.now(),
                    number,
                ],
            )
        return {
            "link_id": link_id,
            "reference_kind": kind,
            "scheme": scheme,
            "code": code,
            "mention": mention,
            "state": state,
            "reason": reason,
            "resolution_id": resolution_id,
        }

    def review_place(
        self,
        namespace: str,
        resolution_id: str,
        decision: str,
        *,
        selected_place_id: str | None,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Review a saved place resolution through the Geospatial owner (accept, reject or defer)."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, GEO_REVIEW)
        if not self._known_resolution(namespace, resolution_id):
            raise DevelopmentFinanceError(
                "not_found", "the resolution is not one of this namespace's place links"
            )
        geo_namespace = self.conn.execute(
            "SELECT geo_namespace FROM devfin_place_links WHERE namespace=? AND resolution_id=? LIMIT 1",
            [namespace, resolution_id],
        ).fetchone()[0]
        return GeospatialStore(self.conn, now=self.now).review(
            geo_namespace,
            resolution_id,
            decision,
            selected_place_id=selected_place_id,
            reason=reason,
            principal_id=principal_id,
            scopes={GEO_REVIEW},
        )

    def _known_resolution(self, namespace: str, resolution_id: str) -> bool:
        return table_exists(self.conn, "devfin_place_links") and bool(
            self.conn.execute(
                "SELECT 1 FROM devfin_place_links WHERE namespace=? AND resolution_id=?",
                [namespace, resolution_id],
            ).fetchone()
        )

    def place_links(
        self, namespace: str, *, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Each place reference with its effective state: a saved code resolution, or the latest review."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "devfin_place_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, reference_kind, scheme, code, mention, state, reason, resolution_id, created_at_ms "
            "FROM devfin_place_links WHERE namespace=? ORDER BY reference_kind, coalesce(code, mention), "
            "evaluation_no",
            [namespace],
        ).fetchall()
        latest: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            item = {
                "link_id": r[0],
                "reference_kind": r[1],
                "scheme": r[2],
                "code": r[3],
                "mention": r[4],
                "state": r[5],
                "reason": r[6],
                "resolution_id": r[7],
                "selected_place_id": None,
            }
            if r[7] and table_exists(self.conn, "geocode_resolutions"):
                selected = self.conn.execute(
                    "SELECT selected_place_id FROM geocode_resolutions WHERE resolution_id=?",
                    [r[7]],
                ).fetchone()
                review = (
                    self.conn.execute(
                        "SELECT decision, selected_place_id, review_id FROM geocode_reviews WHERE resolution_id=? "
                        "ORDER BY revision DESC LIMIT 1",
                        [r[7]],
                    ).fetchone()
                    if table_exists(self.conn, "geocode_reviews")
                    else None
                )
                if review is not None:
                    item["review"] = {"decision": review[0], "review_id": review[2]}
                    if review[0] == "accept":
                        item["state"], item["selected_place_id"] = "resolved", review[1]
                    elif review[0] == "reject":
                        item["state"], item["selected_place_id"] = "rejected", None
                elif item["state"] == "resolved" and selected:
                    item["selected_place_id"] = selected[0]
            latest[(r[1], r[2], r[3], r[4])] = item
        return list(latest.values())

    # ------------------------------------------------------------------ currencies

    def convert(
        self,
        namespace: str,
        transaction_id: str,
        *,
        target_currency: str,
        rate: str,
        rate_date: str,
        rate_source: str,
        citation: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Exact conversion with a cited rate: stored beside the original, which is never changed."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        row = (
            self.conn.execute(
                "SELECT value, currency, value_date FROM devfin_transactions WHERE namespace=? AND transaction_id=?",
                [namespace, transaction_id],
            ).fetchone()
            if table_exists(self.conn, "devfin_transactions")
            else None
        )
        if row is None:
            raise DevelopmentFinanceError(
                "not_found", "transaction is not visible in this namespace"
            )
        value, currency, value_date = row
        if value is None or currency is None or value_date is None:
            raise DevelopmentFinanceError(
                "unconvertible",
                "an amount without a value, currency or value date stays unknown and unconverted",
            )
        target = str(target_currency or "").strip().upper()
        if len(target) != 3 or not target.isalpha():
            raise DevelopmentFinanceError(
                "invalid_currency", "target currency is an ISO 4217 code"
            )
        if target == str(currency).upper():
            raise DevelopmentFinanceError(
                "invalid_currency", "the amount is already in the target currency"
            )
        day = iso_day(rate_date)
        if day is None or rate_date != day:
            raise DevelopmentFinanceError(
                "invalid_rate", "the rate date is an ISO date"
            )
        if not str(rate_source or "").strip() or not str(citation or "").strip():
            raise DevelopmentFinanceError(
                "uncited_rate", "a conversion needs the rate source and a citation"
            )
        try:
            factor = Decimal(str(rate))
        except InvalidOperation as exc:
            raise DevelopmentFinanceError(
                "invalid_rate", "the rate is a decimal number"
            ) from exc
        if not factor.is_finite() or factor <= 0:
            raise DevelopmentFinanceError(
                "invalid_rate", "the rate is a positive decimal number"
            )
        converted = Decimal(value) * factor
        flags = [] if day == value_date else ["rate-date-differs-from-value-date"]
        request = [
            namespace,
            transaction_id,
            value,
            currency,
            value_date,
            target,
            str(factor),
            day,
            rate_source.strip(),
            citation.strip(),
        ]
        conversion_id = "devfin-fx:" + digest(request)[:24]
        self.conn.execute(
            "INSERT INTO devfin_conversions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                namespace,
                conversion_id,
                transaction_id,
                value,
                currency,
                value_date,
                target,
                str(factor),
                day,
                rate_source.strip(),
                citation.strip(),
                str(converted),
                canonical(flags),
                principal_id,
                self.now(),
            ],
        )
        return self._conversion(namespace, conversion_id)

    def _conversion(self, namespace: str, conversion_id: str) -> dict[str, Any]:
        r = self.conn.execute(
            "SELECT conversion_id, transaction_id, original_value, original_currency, value_date, target_currency, "
            "rate, rate_date, rate_source, citation, converted_value, flags_json, principal_id FROM devfin_conversions "
            "WHERE namespace=? AND conversion_id=?",
            [namespace, conversion_id],
        ).fetchone()
        return {
            "conversion_id": r[0],
            "transaction_id": r[1],
            "original": {"value": r[2], "currency": r[3], "value_date": r[4]},
            "converted": {"value": r[10], "currency": r[5]},
            "rate": {"value": r[6], "date": r[7], "source": r[8], "citation": r[9]},
            "method": "exact decimal multiplication by the cited rate",
            "flags": json.loads(r[11]),
            "recorded_by": r[12],
        }

    def conversions(
        self, namespace: str, transaction_id: str, *, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "devfin_conversions"):
            return []
        return [
            self._conversion(namespace, r[0])
            for r in self.conn.execute(
                "SELECT conversion_id FROM devfin_conversions WHERE namespace=? AND transaction_id=? ORDER BY conversion_id",
                [namespace, transaction_id],
            ).fetchall()
        ]


__all__ = ["DevelopmentFinanceNormaliser", "NORMALISER_VERSION", "REVIEW_SCOPE"]

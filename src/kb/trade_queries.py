"""Flows for a country pair, product and period as of a release, reporter and mirror side by side (#2210, TF08).

For a reporter area A, a partner area B and a flow direction, the **reporter figure** is A's own report of the flow
with B and the **mirror figure** is B's report of the opposite flow with A (A's exports against B's imports).
Both come from the vintage each series had released on or before the requested date (release-cutoff semantics of
the economic release store) and each names the release it used. The two are shown side by side with their
difference as a *displayed asymmetry*; they are never reconciled, averaged or replaced by one value. The valuation
basis (CIF/FOB), the classification vintage and the comparability notes of each pair are shown with them.

Areas are matched by the code asked for plus the codes an accepted identity match ties to the same place
(:mod:`src.kb.trade_identity`). A product asked for in one classification vintage is matched in another only
through a cited concordance (or the CN structure), and non-exact mappings are flagged. A pair and direction with no
reported figure is ``none_reported``, never zero; a confidential cell stays confidential.

The sanctions variant returns the flows of the pair in products a cited measure covers (links of
:mod:`src.kb.trade_links`), with the citation and its lookup-aid notice; products the measure cites without a
reported flow are ``none_reported``. Nothing here nowcasts, imputes, reconciles or infers evasion.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.trade_sources import EXCLUSIONS, MIRROR_FLOW, NEVER_SENTENCE
from src.kb.trade_flows import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    TradeComparability,
    TradeError,
    TradeFlowStore,
    authorize,
    canonical,
    comparability_basis,
    digest,
    iso_from_ms,
    require_scope,
    table_exists,
)

LEGAL_READ = "knowledge:legal:read"
DIRECTIONS = ("export", "import", "re-export", "re-import")


def _figure(store: TradeFlowStore, namespace: str, series: Mapping[str, Any], vintage: Mapping[str, Any],
            observation: Mapping[str, Any], match: Mapping[str, Any], as_of_ms: int | None) -> dict[str, Any]:
    revision = store.source_revision(namespace, vintage["release_id"])
    return {
        "provider": series["provider"],
        "series_id": series["series_id"],
        "role": series["role"],
        "reporter": series["reporter"],
        "partner": series["partner"],
        "flow": series["flow"],
        "product": series["product"],
        "classification": series["classification"],
        "valuation": series["valuation"],
        "unit": series["unit"],
        "period": observation["period"],
        "value_text": observation["value_text"],
        "value": observation["value"],
        "status": observation["status"],
        "flags": observation["flags"],
        "quantities": observation["quantities"],
        "product_match": dict(match),
        "release": {
            "vintage_id": vintage["vintage_id"],
            "release_id": vintage["release_id"],
            "release_at": vintage["release_at"],
            "release_at_basis": vintage["release_at_basis"],
            "revision_of": vintage["revision_of"],
            "retrieved_at": iso_from_ms(vintage["retrieved_at_ms"]),
            "selected_as_of": iso_from_ms(as_of_ms) if as_of_ms is not None else "latest",
            "source_revision": revision,
        },
    }


def _asymmetry(reporter: list[dict[str, Any]], mirror: list[dict[str, Any]]) -> dict[str, Any]:
    """The displayed difference reporter minus mirror, when one reported figure stands on each side in one unit."""
    if not reporter or not mirror:
        return {"status": "not_shown", "reason": "a reported figure is missing on one side"}
    if len(reporter) > 1 or len(mirror) > 1:
        return {"status": "not_shown", "reason": "several figures on one side (a non-exact product mapping); each is "
                "shown, none is summed"}
    left, right = reporter[0], mirror[0]
    withheld = [f["status"] for f in (left, right) if f["status"] != "reported"]
    if withheld:
        return {"status": "not_shown", "reason": f"a figure is {withheld[0]} (no value is published)"}
    if left["unit"] != right["unit"]:
        return {"status": "not_shown", "reason": "the two figures are in different units; nothing is converted"}
    difference = Decimal(left["value"]) - Decimal(right["value"])
    return {
        "status": "displayed",
        "reporter_minus_mirror": format(difference, "f"),
        "unit": left["unit"],
        "valuation": {"reporter": left["valuation"], "mirror": right["valuation"]},
        "note": "a displayed difference between two published figures; neither is corrected and no single value "
        "replaces them",
    }


class TradeQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = TradeFlowStore(conn, initialize=False, now=now)

    def _identity(self, namespace: str, code: str) -> dict[str, Any]:
        from src.kb.trade_identity import TradeIdentity, special_area

        if table_exists(self.conn, "trade_identity_assertions"):
            return TradeIdentity(self.conn, initialize=False).equivalent_codes(namespace, code)
        return {"code": str(code), "codes": [str(code)], "place_ids": [], "basis": [],
                "special_area": special_area("m49", code) or special_area("eurostat-geo", code)}

    def _product_match(self, namespace: str, product: Mapping[str, Any] | None, series: Mapping[str, Any],
                       as_of_ms: int | None, cache: dict[str, Any]) -> dict[str, Any] | None:
        if product is None:
            return {"method": "unfiltered", "exact": True}
        wanted = {"scheme": product["scheme"], "vintage": product["vintage"]}
        classification = {"scheme": series["classification"]["scheme"], "vintage": series["classification"]["vintage"]}
        if wanted == classification:
            return {"method": "same-classification", "exact": True} if series["product"]["code"] == str(
                product["code"]) else None
        key = canonical([wanted, classification])
        if key not in cache:
            from src.kb.trade_identity import TradeIdentity

            cache[key] = TradeIdentity(self.conn, initialize=False).resolve_product(
                namespace, str(product["code"]), wanted, classification, as_of_ms=as_of_ms
            )
        for target in cache[key]["targets"]:
            if target["code"] == series["product"]["code"]:
                return {
                    "method": cache[key]["method"],
                    "exact": bool(target["exact"]),
                    "mapping_type": target.get("mapping_type"),
                    "concordance": target.get("concordance"),
                    "basis": target.get("basis"),
                    "flag": None if target["exact"] else "non-exact mapping: the codes do not cover the same goods",
                }
        return None

    def flows(
        self,
        namespace: str,
        *,
        reporter: str,
        partner: str,
        scopes: Iterable[str],
        product: Mapping[str, Any] | None = None,
        directions: Iterable[str] | None = None,
        provider: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
        only_series: set[str] | None = None,
    ) -> dict[str, Any]:
        """Reporter and mirror figures side by side per provider, direction, product group and period."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        directions = list(directions or ("export", "import"))
        if set(directions) - set(DIRECTIONS):
            raise TradeError("invalid_query", f"directions are {DIRECTIONS}")
        if product is not None and not {"code", "scheme", "vintage"} <= set(product):
            raise TradeError("invalid_query", "a product names its code, classification scheme and vintage")
        left, right = self._identity(namespace, reporter), self._identity(namespace, partner)
        cache: dict[str, Any] = {}
        comparability = TradeComparability(self.conn, initialize=False) if table_exists(
            self.conn, "trade_comparability") else None
        candidates = self.store.find_series(namespace, provider=provider)
        results = []
        providers = sorted({s["provider"] for s in candidates}) or ([provider] if provider else [])
        for name in providers:
            for direction in directions:
                sides: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {"reporter": [], "mirror": []}
                for series in candidates:
                    if series["provider"] != name or (only_series is not None and series["series_id"] not in only_series):
                        continue
                    r, p = series["reporter"]["code"], series["partner"]["code"]
                    if r in left["codes"] and p in right["codes"] and series["flow"]["direction"] == direction:
                        side = "reporter"
                    elif r in right["codes"] and p in left["codes"] and series["flow"]["direction"] == MIRROR_FLOW[direction]:
                        side = "mirror"
                    else:
                        continue
                    match = self._product_match(namespace, product, series, as_of_ms, cache)
                    if match is not None:
                        sides[side].append((series, match))
                groups = self._groups(namespace, sides, product, as_of_ms, period_from, period_to, comparability)
                has_figures = any(row["reporter_figures"] or row["mirror_figures"] for g in groups for row in g["rows"])
                results.append(
                    {
                        "provider": name,
                        "direction": direction,
                        "reporter": reporter,
                        "partner": partner,
                        "mirror_direction": MIRROR_FLOW[direction],
                        "status": "reported" if has_figures else "none_reported",
                        "note": None if has_figures else "no reported figure for this pair and direction (not zero)",
                        "groups": groups,
                    }
                )
        answer = {
            "contract": ANSWER_CONTRACT,
            "query": {
                "reporter": reporter,
                "partner": partner,
                "product": None if product is None else dict(product),
                "directions": directions,
                "provider": provider,
                "period_from": period_from,
                "period_to": period_to,
                "as_of": iso_from_ms(as_of_ms) if as_of_ms is not None else "latest",
            },
            "as_of_ms": as_of_ms,
            "identity": {"reporter": left, "partner": right},
            "product_resolution": list(cache.values()),
            "status": "reported" if any(r["status"] == "reported" for r in results) else "none_reported",
            "results": results,
            "exclusions": list(EXCLUSIONS),
            "never": NEVER_SENTENCE,
        }
        answer["receipt"] = {"query": answer["query"], "digest": digest([answer["query"], results])}
        return answer

    def _groups(self, namespace, sides, product, as_of_ms, period_from, period_to, comparability):
        groups: dict[str, dict[str, Any]] = {}
        for side, entries in sides.items():
            for series, match in entries:
                key = "requested-product" if product is not None else canonical(
                    [series["classification"]["scheme"], series["classification"]["vintage"], series["product"]["code"]]
                )
                group = groups.setdefault(key, {"product_key": key, "series": {"reporter": [], "mirror": []},
                                                "periods": {}, "unavailable": []})
                group["series"][side].append(series)
                values = self.store.values(namespace, series["series_id"], as_of_ms=as_of_ms,
                                           period_from=period_from, period_to=period_to)
                if values["status"] != "available":
                    group["unavailable"].append({"series_id": series["series_id"], "side": side,
                                                 "reason": values["reason"]})
                    continue
                vintage = values["vintage"]
                for observation in values["observations"]:
                    row = group["periods"].setdefault(observation["period"], {"reporter": [], "mirror": []})
                    row[side].append(_figure(self.store, namespace, series, vintage, observation, match, as_of_ms))
        out = []
        for key in sorted(groups):
            group = groups[key]
            rows = []
            for period in sorted(group["periods"]):
                reporter, mirror = group["periods"][period]["reporter"], group["periods"][period]["mirror"]
                rows.append({
                    "period": period,
                    "reporter_figures": reporter,
                    "mirror_figures": mirror,
                    "reporter_figure_status": "reported" if reporter else "none_reported",
                    "mirror_figure_status": "reported" if mirror else "none_reported",
                    "asymmetry": _asymmetry(reporter, mirror),
                    "non_exact_mapping": any(not f["product_match"]["exact"] for f in reporter + mirror),
                })
            basis, notes = [], []
            for a in group["series"]["reporter"]:
                for b in group["series"]["mirror"]:
                    basis += comparability_basis(a, b)
                    if comparability is not None:
                        notes += comparability.notes_for(namespace, a["series_id"], b["series_id"])
            out.append({
                "product_key": key,
                "series": {side: [s["series_id"] for s in items] for side, items in group["series"].items()},
                "comparability": {"basis": basis, "notes": notes},
                "unavailable": group["unavailable"],
                "rows": rows,
            })
        return out

    def sanctioned_flows(
        self,
        namespace: str,
        *,
        reporter: str,
        partner: str,
        scopes: Iterable[str],
        control_code: str | None = None,
        table_id: str | None = None,
        as_of_ms: int | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        """The pair's flows in products a cited measure covers, with the citations; uncovered products are none."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, LEGAL_READ)
        from src.kb.trade_links import LOOKUP_AID, TradeLinks

        if not table_exists(self.conn, "trade_links") or not table_exists(self.conn, "sanctions_trade_correlations"):
            return {"contract": ANSWER_CONTRACT, "status": "provider_absent", "provider": "legal.sanctions",
                    "results": [], "notice": LOOKUP_AID, "exclusions": list(EXCLUSIONS)}
        links = TradeLinks(self.conn, initialize=False)
        linked = links.sanctioned_series(namespace, control_code=control_code, table_id=table_id)
        answer = self.flows(namespace, reporter=reporter, partner=partner, scopes=scopes, as_of_ms=as_of_ms,
                            period_from=period_from, period_to=period_to, only_series=set(linked))
        for result in answer["results"]:
            for group in result["groups"]:
                ids = group["series"]["reporter"] + group["series"]["mirror"]
                group["measures"] = [
                    {k: link[k] for k in ("link_id", "target", "basis", "citation", "vintage_id")}
                    for sid in ids
                    for link in linked.get(sid, {}).get("links", [])
                ]
        measures = [
            m for m in links.measures(next(
                (link["target"]["sanctions_namespace"] for item in linked.values() for link in item["links"]),
                "global"))
            if (control_code is None or str(m["control_code"]).upper() == str(control_code).upper())
            and (table_id is None or m["table_id"] == table_id)
        ]
        covered = {
            (link["target"]["table_id"], link["target"]["control_code"], link["target"]["product_code"])
            for group in (g for r in answer["results"] for g in r["groups"])
            for link in group["measures"]
        }
        answer.update({
            "variant": "sanctions-covered-products",
            "measure_filter": {"control_code": control_code, "table_id": table_id},
            "none_reported": [
                {**{k: m[k] for k in ("table_id", "table_revision", "control_code", "product_code", "product_scheme")},
                 "status": "none_reported", "note": "the measure cites this product; the pair has no reported flow in "
                 "it (not zero)"}
                for m in measures
                if (m["table_id"], m["control_code"], m["product_code"]) not in covered
            ],
            "notice": LOOKUP_AID,
        })
        return answer

    def export_bundle(self, answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A noesis-evidence-bundle-v1 citing every figure with source, classification vintage, release vintage and
        as-of time; none-reported pairs, confidential cells and non-exact mappings are omissions."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        def match_payload(match: Mapping[str, Any]) -> dict[str, Any]:
            # ``method`` is reserved for analytic honesty envelopes in bundles; the match basis is named explicitly.
            return {("match_method" if k == "method" else k): v for k, v in match.items()}

        builder = EvidenceBundleBuilder("answer", {"operation": "trade-flows", "query": answer["query"]},
                                        created_at_ms=created_at_ms, as_of_ms=answer.get("as_of_ms"))
        refs, statements = [], []
        for result in answer["results"]:
            label = f"{result['provider']} {result['direction']} {result['reporter']}-{result['partner']}"
            if result["status"] == "none_reported":
                builder.add_omission(f"{label}: none reported (not zero)")
                statements.append({"statement": f"{label}: no reported figure (not zero)", "status": "not_found",
                                   "evidence_refs": []})
            for group in result["groups"]:
                for row in group["rows"]:
                    row_refs = []
                    for side in ("reporter", "mirror"):
                        for figure in row[f"{side}_figures"]:
                            release = figure["release"]
                            object_id = f"trade-figure:{figure['series_id']}:{figure['period']}@{release['vintage_id']}"
                            builder.add_object("evidence", {
                                "kind": "trade-figure",
                                "locator": {"cited": True, "document_id": release["release_id"],
                                            "series_id": figure["series_id"], "period": figure["period"]},
                                "side": side,
                                "provider": figure["provider"],
                                "reporter": figure["reporter"],
                                "partner": figure["partner"],
                                "flow": figure["flow"],
                                "product": figure["product"],
                                "classification_vintage": figure["classification"],
                                "valuation": figure["valuation"],
                                "unit": figure["unit"],
                                "period": figure["period"],
                                "value_text": figure["value_text"],
                                "status": figure["status"],
                                "product_match": match_payload(figure["product_match"]),
                                "release_vintage": {k: release[k] for k in ("vintage_id", "release_id", "release_at",
                                                                            "release_at_basis", "revision_of")},
                                "as_of": release["selected_as_of"],
                                "retrieved_at": release["retrieved_at"],
                                "source": {k: release["source_revision"].get(k) for k in (
                                    "provider", "source_id", "url", "file_sha256", "published_on", "evidence_origin",
                                    "live_verification")},
                            }, object_id=object_id)
                            refs.append(object_id)
                            row_refs.append(object_id)
                            url = release["source_revision"].get("url")
                            if url:
                                builder.add_external_reference(f"release:{release['release_id']}", url,
                                                               required=False)
                            if figure["status"] != "reported":
                                builder.add_omission(f"{figure['provider']} {figure['period']}: the cell is "
                                                     f"{figure['status']}; no value", object_id=object_id)
                            if not figure["product_match"]["exact"]:
                                builder.add_omission(f"{figure['product']['code']} "
                                                     f"({figure['classification']['vintage']}): non-exact product "
                                                     "mapping", object_id=object_id)
                    asymmetry = row["asymmetry"]
                    statements.append({
                        "statement": f"{label} {row['period']}: reporter figure {row['reporter_figure_status']}, "
                        f"mirror figure {row['mirror_figure_status']}; asymmetry {asymmetry['status']}"
                        + (f" ({asymmetry['reporter_minus_mirror']})" if asymmetry["status"] == "displayed" else ""),
                        "status": "cited",
                        "evidence_refs": sorted(row_refs),
                    })
        root = {k: answer[k] for k in ("contract", "query", "as_of_ms", "status", "receipt", "exclusions", "never")}
        builder.add_object("answer", {"kind": "trade-flows", **root, "statements": statements},
                           object_id=f"trade-answer:{answer['receipt']['digest'][:24]}",
                           references=sorted(set(refs)), root=True)
        return builder.build()


__all__ = ["TradeQueries"]

"""Day-ahead prices through Market time-series storage (EN02 price records, EN03 prices, EN09 market link).

Energy price records keep the published values of each release vintage (for
revision history), but the price *series* lives in Market storage: every
published point of a vintage is written through
:meth:`src.domains.market.prices.MarketPriceStore.put_bar` against a listing
the market owner registered for the zone's day-ahead index (one listing per
bidding zone and currency). Each bar is a single published clearing price
(open = high = low = close, no volume), unadjusted, with an ENTSO-E (or other
publisher) source reference whose ``source_revision_id`` is the energy vintage
id, so the market bar cites the energy vintage and the energy vintage lists
the bar revisions. No new price store is created.

``noesis-market-bar-v1`` requires prices >= 0. A negative published price is
therefore **not written** and is listed as ``refused`` with that reason; it
stays readable from the energy vintage with its citation. Nothing is clipped,
shifted or dropped silently.
"""

from __future__ import annotations

import json
from decimal import Decimal

from src.kb.energy_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.energy_store import EnergyStore, EnergyStoreError, authorize, ms

_INTERVALS = {"PT15M": "15m", "PT30M": "30m", "PT60M": "1h", "PT1H": "1h", "P1D": "1d"}
_DDL = """
CREATE TABLE IF NOT EXISTS energy_market_refs(
 ref_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL,
 listing_id TEXT NOT NULL, bars_json TEXT NOT NULL, refused_json TEXT NOT NULL, principal_id TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL);
"""
LICENCE_IDS = {"entsoe": "entsoe-transparency-terms", "energy-charts": "energy-charts-cc-by-4.0",
               "eia": "eia-public-domain", "ember": "ember-cc-by-4.0", "eurostat": "eurostat-reuse"}


def _currency(unit):
    head = str(unit or "").split("/", 1)[0].strip().upper()
    return head if len(head) == 3 and head.isalpha() else None


def publish_prices(conn, namespace, vintage_id, *, listing_id, entitlement_id, principal_id, scopes, now=None):
    """Write one price vintage's points as market bars; returns written bar revisions and refused points."""

    from src.domains.market.prices import MarketPriceError, MarketPriceStore

    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    conn.execute(_DDL)
    store = EnergyStore(conn, initialize=False, now=now)
    vintage = store.vintage(namespace, vintage_id, scopes=scopes)
    record = vintage["record"]
    if record["record_type"] != "price":
        raise EnergyStoreError("not_a_price", "only price vintages are written to market storage")
    interval = _INTERVALS.get(record.get("resolution") or "")
    currency = _currency(record["unit"])
    if interval is None or currency is None:
        raise EnergyStoreError("market_unrepresentable",
                               "noesis-market-bar-v1 needs a supported interval and a currency per MWh")
    prices = MarketPriceStore(conn, initialize=True, now=store.now)
    receipt = store.receipt_row(vintage["receipt_id"]) if vintage.get("receipt_id") else None
    content_hash = (receipt or {}).get("response_sha256") or vintage["values_hash"]
    bars, refused = [], []
    for point in store.values(vintage_id):
        start = ms(point["start"])
        end = ms(point["end"]) if point["end"] else None
        if point["value"] is None:
            refused.append({"start": point["start"], "reason": "no published value"})
            continue
        value = Decimal(point["value"])
        if value < 0:
            refused.append({"start": point["start"], "value": point["value"],
                            "reason": "negative price: noesis-market-bar-v1 requires prices >= 0; kept in the energy vintage"})
            continue
        ref = {"source_ref_id": f"energy:{vintage_id}:{point['start']}", "provider": record["provider"],
               "provider_object_id": str(record["locator"].get("document_mrid") or record["native_id"]),
               "source_revision_id": vintage_id, "public_at_ms": vintage["published_at_ms"],
               "source_snapshot_id": vintage.get("receipt_id"), "source_url": record["source_url"],
               "retrieved_at_ms": vintage["retrieved_at_ms"], "content_hash": content_hash,
               "license_id": LICENCE_IDS[record["provider"]], "entitlement_id": entitlement_id}
        number = float(value)
        observation = {
            "contract": "noesis-market-bar-v1", "namespace": namespace, "owner": None, "bar_id": "assigned",
            "listing_id": listing_id, "provider": f"{record['provider']}:{record['dataset']}", "interval": interval,
            "bar_start_ms": start, "bar_end_ms": end if end is not None else start,
            "public_at_ms": vintage["published_at_ms"], "retrieved_at_ms": vintage["retrieved_at_ms"],
            "revision_id": "assigned", "revision": 1, "provider_record_id": f"{record['native_id']}:{point['start']}",
            "provider_revision_id": vintage["release_key"], "open": number, "high": number, "low": number,
            "close": number, "volume": None, "trade_count": None, "vwap": None, "currency": currency,
            "price_basis": "unadjusted", "adjustment_method": None, "adjustment_cutoff_ms": None,
            "adjustment_action_revision_ids": [], "adjustment_calculation_id": None, "prior_revision_id": None,
            "source_refs": [ref], "recorded_at_ms": vintage["retrieved_at_ms"], "record_hash": "assigned"}
        try:
            bar = prices.put_bar(namespace, observation, principal_id=principal_id, scopes=set(scopes))
        except MarketPriceError as exc:
            refused.append({"start": point["start"], "value": point["value"], "reason": f"market refused: {exc.code}"})
            continue
        bars.append({"start": point["start"], "bar_id": bar["bar_id"], "revision_id": bar["revision_id"],
                     "value": point["value"]})
    ref_id = "energy-market-ref:" + digest([namespace, vintage_id, listing_id])[:24]
    conn.execute("INSERT INTO energy_market_refs VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (ref_id) DO UPDATE SET "
                 "bars_json=excluded.bars_json, refused_json=excluded.refused_json",
                 [ref_id, namespace, vintage_id, vintage["series_id"], listing_id, canonical(bars), canonical(refused),
                  principal_id, store.now()])
    return {"ref_id": ref_id, "vintage_id": vintage_id, "listing_id": listing_id, "written": bars, "refused": refused,
            "storage": "market time-series (noesis-market-bar-v1) via MarketPriceStore.put_bar",
            "attribution": record["attribution"]}


def market_refs(conn, namespace, *, scopes, series_id=None, vintage_id=None):
    authorize(namespace, scopes, READ_SCOPE)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='energy_market_refs'").fetchone():
        return []
    clauses, params = ["namespace=?"], [namespace]
    if series_id:
        clauses.append("series_id=?")
        params.append(series_id)
    if vintage_id:
        clauses.append("vintage_id=?")
        params.append(vintage_id)
    return [{"ref_id": r[0], "vintage_id": r[1], "series_id": r[2], "listing_id": r[3], "bars": json.loads(r[4]),
             "refused": json.loads(r[5])}
            for r in conn.execute("SELECT ref_id, vintage_id, series_id, listing_id, bars_json, refused_json FROM "
                                  "energy_market_refs WHERE " + " AND ".join(clauses) + " ORDER BY ref_id", params).fetchall()]

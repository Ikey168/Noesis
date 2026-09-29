"""Authored offline fixtures for the Energy Systems pack (#2211).

Every body below is written by hand in the provider's documented response
shape. Identifiers such as ``fixture-a75-...``, plant ``99901`` and unit
``11WD2FIXTURE0001`` are illustrative and every value is invented: none of it
is live evidence. ``python -m tests.unit.energy.fixture_builder`` rewrites
``tests/fixtures/source_packs/energy-*.json`` and ``tests/fixtures/energy/*.json``;
the source pack pins their SHA-256 and expected output hashes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RETRIEVED_AT = "2026-09-25T06:00:00Z"
RETRIEVED_AT_REVISION = "2026-09-27T06:00:00Z"
ZONE, FR, PL = "10Y1001A1001A82H", "10YFR-RTE------C", "10YPL-AREA-----S"
ENTSOE = "https://web-api.tp.entsoe.eu/api"
WINDOW = {"periodStart": "202609240000", "periodEnd": "202609240400"}
NOTE = ("Authored offline fixture in the provider's documented response shape; identifiers are illustrative and "
        "values are invented. Not live evidence.")
GL = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"
PUB = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def _points(values, tag="quantity"):
    return "".join(f"<Point><position>{i + 1}</position><{tag}>{v}</{tag}></Point>" for i, v in enumerate(values))


def _header(root, ns, mrid, kind, created, revision="1", process=None):
    process_xml = f"<process.processType>{process}</process.processType>" if process else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} xmlns="{ns}"><mRID>{mrid}</mRID>'
            f"<revisionNumber>{revision}</revisionNumber><type>{kind}</type>{process_xml}"
            f"<createdDateTime>{created}</createdDateTime>")


def a75(revision="1", solar=("0", "0", "0", "0"), wind=("9120", "9204", "9311", "9287")):
    series = ""
    for index, (psr, values) in enumerate((("B16", solar), ("B19", wind))):
        series += (f"<TimeSeries><mRID>{index + 1}</mRID><businessType>A01</businessType>"
                   f"<inBiddingZone_Domain.mRID codingScheme=\"A01\">{ZONE}</inBiddingZone_Domain.mRID>"
                   "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>"
                   f"<MktPSRType><psrType>{psr}</psrType></MktPSRType><Period><timeInterval>"
                   "<start>2026-09-24T00:00Z</start><end>2026-09-24T04:00Z</end></timeInterval>"
                   f"<resolution>PT60M</resolution>{_points(values)}</Period></TimeSeries>")
    created = "2026-09-24T05:02:11Z" if revision == "1" else "2026-09-26T09:40:00Z"
    return _header("GL_MarketDocument", GL, "fixture-a75-7f3c1d", "A75", created, revision, "A16") + series + "</GL_MarketDocument>"


def a65():
    return (_header("GL_MarketDocument", GL, "fixture-a65-51aa20", "A65", "2026-09-24T05:03:40Z", "1", "A16")
            + f"<TimeSeries><mRID>1</mRID><businessType>A04</businessType><outBiddingZone_Domain.mRID codingScheme=\"A01\">{ZONE}"
            "</outBiddingZone_Domain.mRID><quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>"
            "<Period><timeInterval><start>2026-09-24T00:00Z</start><end>2026-09-24T04:00Z</end></timeInterval>"
            f"<resolution>PT60M</resolution>{_points(['44810', '43120', '42655', '42930'])}</Period></TimeSeries>"
            "</GL_MarketDocument>")


def a44():
    return (_header("Publication_MarketDocument", PUB, "fixture-a44-0c9e77", "A44", "2026-09-23T10:45:00Z")
            + f"<TimeSeries><mRID>1</mRID><auction.type>A01</auction.type><businessType>A62</businessType>"
            f"<in_Domain.mRID codingScheme=\"A01\">{ZONE}</in_Domain.mRID><out_Domain.mRID codingScheme=\"A01\">{ZONE}</out_Domain.mRID>"
            "<contract_MarketAgreement.type>A01</contract_MarketAgreement.type><currency_Unit.name>EUR</currency_Unit.name>"
            "<price_Measure_Unit.name>MWH</price_Measure_Unit.name><curveType>A01</curveType><Period><timeInterval>"
            "<start>2026-09-24T00:00Z</start><end>2026-09-24T04:00Z</end></timeInterval><resolution>PT60M</resolution>"
            f"{_points(['85.12', '79.40', '-3.50', '12.05'], 'price.amount')}</Period></TimeSeries></Publication_MarketDocument>")


def a68():
    series = ""
    for index, (psr, value) in enumerate((("B16", "99450"), ("B19", "63120"))):
        series += (f"<TimeSeries><mRID>{index + 1}</mRID><businessType>A33</businessType>"
                   f"<inBiddingZone_Domain.mRID codingScheme=\"A01\">{ZONE}</inBiddingZone_Domain.mRID>"
                   "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>"
                   f"<MktPSRType><psrType>{psr}</psrType></MktPSRType><Period><timeInterval><start>2025-12-31T23:00Z</start>"
                   f"<end>2026-12-31T23:00Z</end></timeInterval><resolution>P1Y</resolution>{_points([value])}</Period></TimeSeries>")
    return (_header("GL_MarketDocument", GL, "fixture-a68-2b41e0", "A68", "2025-12-18T08:00:00Z", "1", "A33")
            + series + "</GL_MarketDocument>")


def a71():
    return (_header("GL_MarketDocument", GL, "fixture-a71-93d002", "A71", "2025-12-18T08:05:00Z", "1", "A33")
            + f"<TimeSeries><mRID>1</mRID><businessType>A37</businessType><inBiddingZone_Domain.mRID codingScheme=\"A01\">{ZONE}"
            "</inBiddingZone_Domain.mRID><quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType>"
            "<MktPSRType><psrType>B14</psrType><PowerSystemResources><mRID codingScheme=\"A01\">11WD2FIXTURE0001</mRID>"
            "<name>Fixture Nuclear Unit 1</name></PowerSystemResources></MktPSRType><Period><timeInterval>"
            "<start>2025-12-31T23:00Z</start><end>2026-12-31T23:00Z</end></timeInterval><resolution>P1Y</resolution>"
            f"{_points(['1400'])}</Period></TimeSeries></GL_MarketDocument>")


def a11(out_zone, in_zone, values, mrid):
    return (_header("Publication_MarketDocument", PUB, mrid, "A11", "2026-09-24T06:10:00Z")
            + f"<TimeSeries><mRID>1</mRID><businessType>A66</businessType><in_Domain.mRID codingScheme=\"A01\">{in_zone}"
            f"</in_Domain.mRID><out_Domain.mRID codingScheme=\"A01\">{out_zone}</out_Domain.mRID>"
            "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A01</curveType><Period><timeInterval>"
            "<start>2026-09-24T00:00Z</start><end>2026-09-24T04:00Z</end></timeInterval><resolution>PT60M</resolution>"
            f"{_points(values)}</Period></TimeSeries></Publication_MarketDocument>")


def ack_no_data():
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:'
            'acknowledgementdocument:7:0"><mRID>fixture-ack</mRID><createdDateTime>2026-09-25T06:00:00Z</createdDateTime>'
            "<Reason><code>999</code><text>No matching data found for Data item Physical Flows</text></Reason>"
            "</Acknowledgement_MarketDocument>")


def _page(url, params, body, content_type="text/xml"):
    return {"url": url, "params": params, "status": 200, "headers": {"Content-Type": content_type},
            "body_sha256": hashlib.sha256(body.encode() if isinstance(body, str) else json.dumps(body).encode()).hexdigest(),
            "body": body}


def entsoe_pages(revision="1"):
    pages = [
        _page(ENTSOE, {"documentType": "A75", "in_Domain": ZONE, **WINDOW, "processType": "A16"}, a75(revision) if revision == "1"
              else a75(revision, wind=("9120", "9240", "9311", "9287"))),
        _page(ENTSOE, {"documentType": "A65", "outBiddingZone_Domain": ZONE, **WINDOW, "processType": "A16"}, a65()),
        _page(ENTSOE, {"documentType": "A44", **WINDOW, "contract_MarketAgreement.type": "A01", "in_Domain": ZONE,
                       "out_Domain": ZONE}, a44()),
        _page(ENTSOE, {"documentType": "A68", **WINDOW, "processType": "A33", "in_Domain": ZONE}, a68()),
        _page(ENTSOE, {"documentType": "A71", **WINDOW, "processType": "A33", "in_Domain": ZONE}, a71()),
        _page(ENTSOE, {"documentType": "A11", **WINDOW, "out_Domain": ZONE, "in_Domain": FR},
              a11(ZONE, FR, ["1830", "1795", "2010", "1950"], "fixture-a11-de-fr")),
        _page(ENTSOE, {"documentType": "A11", **WINDOW, "out_Domain": FR, "in_Domain": ZONE},
              a11(FR, ZONE, ["0", "12", "0", "0"], "fixture-a11-fr-de")),
        _page(ENTSOE, {"documentType": "A11", **WINDOW, "out_Domain": ZONE, "in_Domain": PL}, ack_no_data()),
        _page(ENTSOE, {"documentType": "A11", **WINDOW, "out_Domain": PL, "in_Domain": ZONE}, ack_no_data()),
    ]
    return pages


ENTSOE_SELECTION = {"bidding_zone": ZONE, "zone_name": "DE-LU", "period_start": "202609240000",
                    "period_end": "202609240400", "border_zones": [FR, PL], "border_names": {FR: "FR", PL: "PL"},
                    "documents": ["generation", "load", "day-ahead-price", "installed-capacity",
                                  "installed-capacity-units", "cross-border-flow"]}
EIA = "https://api.eia.gov/v2/electricity"
EIA_BASE = {"data[0]": "value", "start": "2026-09-24T00", "end": "2026-09-24T02", "offset": "0", "length": "500",
            "sort[0][column]": "period", "sort[0][direction]": "asc", "frequency": "hourly"}


def eia_fuel(revised=False):
    rows = []
    for hour, (sun, ng) in enumerate((("0", "8120"), ("0", "7990"), ("15" if not revised else "18", "7875"))):
        for code, name, value in (("SUN", "Solar", sun), ("NG", "Natural Gas", ng)):
            rows.append({"period": f"2026-09-24T{hour:02d}", "respondent": "CISO",
                         "respondent-name": "California Independent System Operator", "fueltype": code, "type-name": name,
                         "value": value, "value-units": "megawatthours"})
    return {"response": {"total": str(len(rows)), "dateFormat": "YYYY-MM-DD\"T\"HH24", "frequency": "hourly",
                         "data": rows, "description": "Hourly net generation by balancing authority and energy source."},
            "request": {"command": "/v2/electricity/rto/fuel-type-data/data/"}, "apiVersion": "2.1.8"}


def eia_region():
    rows = [{"period": f"2026-09-24T{h:02d}", "respondent": "CISO", "respondent-name": "California Independent System Operator",
             "type": "D", "type-name": "Demand", "value": v, "value-units": "megawatthours"}
            for h, v in enumerate(("24110", "23150", "22480"))]
    return {"response": {"total": "3", "frequency": "hourly", "data": rows}, "apiVersion": "2.1.8"}


def eia_interchange():
    rows = []
    for h, (bpat, azps) in enumerate((("-1210", "455"), ("-1180", "470"), ("-1160", "468"))):
        for to, name, value in (("BPAT", "Bonneville Power Administration", bpat), ("AZPS", "Arizona Public Service Company", azps)):
            rows.append({"period": f"2026-09-24T{h:02d}", "fromba": "CISO", "fromba-name": "California Independent System Operator",
                         "toba": to, "toba-name": name, "value": value, "value-units": "megawatthours"})
    return {"response": {"total": str(len(rows)), "frequency": "hourly", "data": rows}, "apiVersion": "2.1.8"}


def eia_capacity():
    rows = []
    for period, g2_status in (("2026-06", "OP"), ("2026-07", "RE")):
        rows.append({"period": period, "stateid": "CA", "plantid": "99901", "plantName": "Fixture Solar Park",
                     "generatorid": "G1", "technology": "Solar Photovoltaic", "energy_source_code": "SUN", "status": "OP",
                     "balancing_authority_code": "CISO", "nameplate-capacity-mw": "250", "net-summer-capacity-mw": "248",
                     "operating-year-month": "2019-04"})
        rows.append({"period": period, "stateid": "CA", "plantid": "99901", "plantName": "Fixture Solar Park",
                     "generatorid": "G2", "technology": "Batteries", "energy_source_code": "MWH", "status": g2_status,
                     "balancing_authority_code": "CISO", "nameplate-capacity-mw": "50",
                     "net-summer-capacity-mw": "50" if g2_status == "OP" else None,
                     "operating-year-month": "2021-08", "planned-retirement-year-month": "2026-07"})
    return {"response": {"total": str(len(rows)), "frequency": "monthly", "data": rows}, "apiVersion": "2.1.8"}


EIA_SELECTIONS = {
    "fuel-type-data": {"route": "fuel-type-data", "balancing_areas": ["CISO"], "start": "2026-09-24T00", "end": "2026-09-24T02"},
    "region-data": {"route": "region-data", "balancing_areas": ["CISO"], "types": ["D"], "start": "2026-09-24T00",
                    "end": "2026-09-24T02"},
    "interchange-data": {"route": "interchange-data", "balancing_areas": ["CISO"], "start": "2026-09-24T00",
                         "end": "2026-09-24T02"},
    "operating-generator-capacity": {"route": "operating-generator-capacity", "plants": ["99901"], "start": "2026-06",
                                     "end": "2026-07",
                                     "release": {"label": "EIA-860M July 2026", "published_on": "2026-08-26"}},
}


def eia_pages(route, revised=False):
    selection = EIA_SELECTIONS[route]
    if route == "fuel-type-data":
        params = {**EIA_BASE, "facets[respondent][]": "CISO"}
        body = eia_fuel(revised)
    elif route == "region-data":
        params = {**EIA_BASE, "facets[respondent][]": "CISO", "facets[type][]": "D"}
        body = eia_region()
    elif route == "interchange-data":
        params = {**EIA_BASE, "facets[fromba][]": "CISO"}
        body = eia_interchange()
    else:
        params = {**EIA_BASE, "start": selection["start"], "end": selection["end"], "frequency": "monthly",
                  "data[0]": "nameplate-capacity-mw", "data[1]": "net-summer-capacity-mw", "facets[plantid][]": "99901"}
        body = eia_capacity()
    path = {"operating-generator-capacity": "operating-generator-capacity"}.get(route, f"rto/{route}")
    return [_page(f"{EIA}/{path}/data/", params, body, "application/json")]


EMBER = "https://api.ember-energy.org/v1"
EMBER_SELECTIONS = {
    "electricity-generation/monthly": {"endpoint": "electricity-generation/monthly", "entities": ["DEU"],
                                       "start_date": "2026-06", "end_date": "2026-07",
                                       "release": {"label": "Ember monthly electricity data 2026-08", "published_on": "2026-08-28"}},
    "electricity-demand/monthly": {"endpoint": "electricity-demand/monthly", "entities": ["DEU"], "start_date": "2026-06",
                                   "end_date": "2026-07",
                                   "release": {"label": "Ember monthly electricity data 2026-08", "published_on": "2026-08-28"}},
    "installed-capacity/yearly": {"endpoint": "installed-capacity/yearly", "entities": ["DEU"], "start_date": "2024",
                                  "end_date": "2025",
                                  "release": {"label": "Ember yearly electricity data 2026", "published_on": "2026-04-30"}},
}


def ember_body(endpoint, release=1):
    if endpoint == "electricity-generation/monthly":
        rows = []
        for date, solar, wind, total in (("2026-06-01", "10.21", "6.80", "34.90"),
                                         ("2026-07-01", "10.95" if release == 1 else "11.02", "6.12", "35.40")):
            for series, value, aggregate in (("Solar", solar, False), ("Wind", wind, False), ("Total generation", total, True)):
                rows.append({"entity": "Germany", "entity_code": "DEU", "is_aggregate_entity": False, "date": date,
                             "series": series, "is_aggregate_series": aggregate, "generation_twh": value,
                             "share_of_generation_pct": None if aggregate else str(round(float(value) / float(total) * 100, 2))})
        return {"stats": {"number_of_records": len(rows)}, "data": rows}
    if endpoint == "electricity-demand/monthly":
        rows = [{"entity": "Germany", "entity_code": "DEU", "is_aggregate_entity": False, "date": d, "demand_twh": v,
                 "demand_mwh_per_capita": p} for d, v, p in (("2026-06-01", "38.10", "0.45"), ("2026-07-01", "39.02", "0.46"))]
        return {"stats": {"number_of_records": 2}, "data": rows}
    rows = [{"entity": "Germany", "entity_code": "DEU", "is_aggregate_entity": False, "date": d, "series": s,
             "is_aggregate_series": False, "capacity_gw": v}
            for d, s, v in (("2024-01-01", "Solar", "90.3"), ("2025-01-01", "Solar", "106.8"),
                            ("2024-01-01", "Wind", "72.6"), ("2025-01-01", "Wind", "76.1"))]
    return {"stats": {"number_of_records": 4}, "data": rows}


def ember_pages(endpoint, release=1):
    selection = EMBER_SELECTIONS[endpoint]
    params = {"entity_code": "DEU", "start_date": selection["start_date"], "end_date": selection["end_date"]}
    return [_page(f"{EMBER}/{endpoint}", params, ember_body(endpoint, release), "application/json")]


EUROSTAT_SELECTION = {"dataset": "nrg_bal_c", "keys": ["A.GIC+NRGSUP.TOTAL.KTOE.DE+FR"],
                      "params": {"startPeriod": "2023", "endPeriod": "2024"}}


def eurostat_csv(later=False):
    stamp = "12/06/26 23:00:00" if not later else "15/09/26 23:00:00"
    rows = ["DATAFLOW,LAST UPDATE,freq,nrg_bal,siec,unit,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG"]
    data = {("GIC", "DE"): (("2023", "268312.4", ""), ("2024", "259880.0" if not later else "260115.2", "p" if not later else "")),
            ("NRGSUP", "DE"): (("2023", "271004.9", ""), ("2024", "262410.3", "p" if not later else "")),
            ("GIC", "FR"): (("2023", "231550.8", ""), ("2024", "229901.6", "ep" if not later else "e")),
            ("NRGSUP", "FR"): (("2023", "234118.0", "b"), ("2024", ":", ""))}
    for (bal, geo), obs in data.items():
        for period, value, flag in obs:
            rows.append(f"ESTAT:NRG_BAL_C(1.0),{stamp},A,{bal},TOTAL,KTOE,{geo},{period},{value},{flag}")
    return "\n".join(rows) + "\n"


def eurostat_pages(later=False):
    url = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_bal_c/A.GIC+NRGSUP.TOTAL.KTOE.DE+FR"
    return [_page(url, {"format": "SDMX-CSV", "startPeriod": "2023", "endPeriod": "2024"}, eurostat_csv(later), "text/csv")]


ENERGY_CHARTS_SELECTION = {"endpoint": "public_power", "countries": ["de"], "start": "2026-09-24T00:00Z",
                           "end": "2026-09-24T01:00Z"}


def energy_charts_pages():
    stamps = [1790208000 + 900 * i for i in range(4)]
    body = {"unix_seconds": stamps, "production_types": [
        {"name": "Solar", "data": [0.0, 0.0, 0.0, 0.0]},
        {"name": "Wind onshore", "data": [7910.4, 7988.1, 8050.2, 8102.9]},
        {"name": "Load", "data": [44790.2, 44321.0, 43987.5, 43650.8]},
        {"name": "Residual load", "data": [36879.8, 36332.9, 35937.3, 35547.9]}], "deprecated": False}
    return [_page("https://api.energy-charts.info/public_power",
                  {"country": "de", "start": "2026-09-24T00:00Z", "end": "2026-09-24T01:00Z"}, body, "application/json")]


def source_fixture(provider, pages, scenarios, retrieved_at=RETRIEVED_AT):
    return {"captured_at": "authored 2026-09-29 (not a capture)", "provider": provider, "note": NOTE,
            "retrieved_at": retrieved_at, "scenarios": scenarios, "native_pages": pages}


SOURCE_FIXTURES = {
    "energy-entsoe": lambda: source_fixture("entsoe", entsoe_pages(), ["generation", "load", "day-ahead-price-negative",
                                                                        "installed-capacity", "unit-capacity",
                                                                        "cross-border-flow", "no-data-border"]),
    "energy-eia-fuel-type": lambda: source_fixture("eia", eia_pages("fuel-type-data"), ["generation-by-fuel"]),
    "energy-eia-region": lambda: source_fixture("eia", eia_pages("region-data"), ["demand"]),
    "energy-eia-interchange": lambda: source_fixture("eia", eia_pages("interchange-data"), ["interchange"]),
    "energy-eia-capacity": lambda: source_fixture("eia", eia_pages("operating-generator-capacity"),
                                                  ["plant-capacity", "retired-generator"]),
    "energy-ember-generation": lambda: source_fixture("ember", ember_pages("electricity-generation/monthly"),
                                                      ["generation", "publisher-shares", "aggregate-series"]),
    "energy-ember-demand": lambda: source_fixture("ember", ember_pages("electricity-demand/monthly"), ["demand"]),
    "energy-ember-capacity": lambda: source_fixture("ember", ember_pages("installed-capacity/yearly"), ["capacity"]),
    "energy-eurostat-balances": lambda: source_fixture("eurostat", eurostat_pages(), ["balance", "flags", "missing-value"]),
    "energy-charts-public-power": lambda: source_fixture("energy-charts", energy_charts_pages(),
                                                         ["derived-from-entsoe", "skipped-computed-series"]),
}
REVISION_FIXTURES = {
    "entsoe_revision_2": lambda: source_fixture("entsoe", entsoe_pages("2"), ["revision"], RETRIEVED_AT_REVISION),
    "eia_fuel_type_revised": lambda: source_fixture("eia", eia_pages("fuel-type-data", revised=True), ["revision"],
                                                    RETRIEVED_AT_REVISION),
    "ember_generation_release_2": lambda: source_fixture("ember", ember_pages("electricity-generation/monthly", 2),
                                                         ["release"], RETRIEVED_AT_REVISION),
    "eurostat_balances_later": lambda: source_fixture("eurostat", eurostat_pages(later=True), ["release"],
                                                      RETRIEVED_AT_REVISION),
}


def write_all():
    written = {}
    for name, build in SOURCE_FIXTURES.items():
        path = ROOT / "tests/fixtures/source_packs" / f"{name}.json"
        path.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
        written[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (ROOT / "tests/fixtures/energy").mkdir(exist_ok=True)
    for name, build in REVISION_FIXTURES.items():
        path = ROOT / "tests/fixtures/energy" / f"{name}.json"
        path.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
        written[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return written


def load(name):
    for folder in ("tests/fixtures/source_packs", "tests/fixtures/energy"):
        path = ROOT / folder / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text())
    raise FileNotFoundError(name)


if __name__ == "__main__":
    print(json.dumps(write_all(), indent=1))

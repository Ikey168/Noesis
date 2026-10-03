"""Author the environment.waste fixtures and pin the ``climate-environment-waste`` source pack (#2740).

Every value is synthetic: reference years 2094-2097 and release dates 2098-2099, so nothing can be mistaken for a
published figure, and the INSPIRE ids name the ``environment.core`` fixture facilities (whose names are marked as
fixtures) plus one id no facility record carries. Run ``python -m tests.unit.waste_fixture_builder`` to regenerate;
``build(write=False)`` returns the texts so a test can check the pinned files are current.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-waste.json"
OUT = ROOT / "tests/fixtures/source_packs"
REVISION_DIR = ROOT / "tests/fixtures/waste"
NOTE = ("authored offline fixture in the provider's documented response shape (verify); every value is fictional; "
        "reference years 2094-2097 and release dates 2098-2099 are fictional, so nothing can be mistaken for a "
        "published figure; not live coverage")
REFS_ESTAT = [{"kind": "methodology", "scheme": "eurostat-esms", "identifier": "env_wasgen_esms",
               "url": "https://ec.europa.eu/eurostat/cache/metadata/en/env_wasgen_esms.htm",
               "relation": "documented_in"},
              {"kind": "standard", "scheme": "celex", "identifier": "32002R2150", "relation": "defined_by",
               "locator": "Regulation (EC) No 2150/2002 on waste statistics (verify amendments)"},
              # Packaging and WEEE are later documents: cited for the Products link, never acquired here.
              {"kind": "dataset", "scheme": "eurostat-dataset", "identifier": "env_waspac",
               "url": "https://ec.europa.eu/eurostat/databrowser/view/env_waspac/default/table",
               "relation": "later_document", "locator": "Packaging waste by waste management operations (verify)"},
              {"kind": "dataset", "scheme": "eurostat-dataset", "identifier": "env_waselee",
               "url": "https://ec.europa.eu/eurostat/databrowser/view/env_waselee/default/table",
               "relation": "later_document", "locator": "Waste electrical and electronic equipment (verify)"}]
REFS_CEI = [{"kind": "methodology", "scheme": "eurostat-esms", "identifier": "cei_esms",
             "url": "https://ec.europa.eu/eurostat/cache/metadata/en/cei_esms.htm", "relation": "documented_in"}]
REFS_EEA = [{"kind": "dataset", "scheme": "eea-industrial-reporting", "identifier": "industrial-reporting-ied-eprtr",
             "url": "https://industry.eea.europa.eu/", "relation": "published_in"},
            {"kind": "standard", "scheme": "celex", "identifier": "32006R0166", "relation": "defined_by",
             "locator": "Regulation (EC) No 166/2006 (E-PRTR), reporting thresholds (verify the successor regime)"},
            # A published identifier from the E-PRTR pollutant list the reporting guidance cites (fixture citation):
            # the only basis for a Chemicals link; names are never matched.
            {"kind": "pollutant", "scheme": "cas", "identifier": "7439-97-6", "relation": "cites",
             "locator": "E-PRTR pollutant list entry, mercury and compounds (as Hg) (fixture citation; verify)"}]
REFS_OECD = [{"kind": "methodology", "scheme": "oecd-data-explorer", "identifier": "municipal-waste",
              "url": "https://www.oecd.org/en/data/datasets/municipal-waste.html", "relation": "documented_in"},
             {"kind": "related", "scheme": "joint-questionnaire",
              "identifier": "OECD/Eurostat Joint Questionnaire on Waste", "relation": "shares_questionnaire_with",
              "locator": "verify"}]

WASGEN = {
    "label": "Eurostat waste generated (env_wasgen, total waste, all NACE activities and households, DE and FR)",
    "flow": "env_wasgen", "key": "A.T.HAZ_NHAZ+HAZ.TOTAL_HH.TOTAL.DE+FR", "params": {"startPeriod": "2094"},
    "periodicity": "biennial",
    "indicator": {"codes": {"env_wasgen": {"concept": "waste_generated",
                                           "label": "Generation of waste by waste category, hazardousness and NACE "
                                                    "Rev. 2 activity"}}},
    "waste_category": {"dimension": "waste", "scheme": "EWC-Stat", "labels": {"TOTAL": "Total waste"}},
    "hazard": {"dimension": "hazard", "labels": {"HAZ_NHAZ": "Hazardous and non-hazardous - Total",
                                                 "HAZ": "Hazardous"}},
    "activity": {"dimension": "nace_r2", "scheme": "NACE Rev.2",
                 "labels": {"TOTAL_HH": "All NACE activities plus households"}},
    "unit": {"dimension": "unit", "labels": {"T": "Tonne"}},
    "area": {"dimension": "geo", "scheme": "eurostat-geo", "labels": {"DE": "Germany", "FR": "France"}},
    "definition": {"source_text": "Waste generated as reported under the Waste Statistics Regulation, by EWC-Stat "
                                  "category, hazardousness and NACE Rev.2 activity or households (verify)",
                   "scope": "total waste, all NACE activities plus households",
                   "methodology_notes": ["Reference years are biennial (even years); odd years are not collected",
                                         "Countries may resubmit earlier reference years"]},
    "references": REFS_ESTAT,
}
WASTRT = {
    "label": "Eurostat waste treatment (env_wastrt, total waste by treatment operation, DE and FR)",
    "flow": "env_wastrt", "key": "A.T.HAZ_NHAZ.TRT+RCV_R+DSP_L.TOTAL.DE+FR", "params": {"startPeriod": "2094"},
    "periodicity": "biennial",
    "indicator": {"codes": {"env_wastrt": {"concept": "waste_treated",
                                           "label": "Treatment of waste by waste category, hazardousness and waste "
                                                    "management operations"}}},
    "waste_category": {"dimension": "waste", "scheme": "EWC-Stat", "labels": {"TOTAL": "Total waste"}},
    "hazard": {"dimension": "hazard", "labels": {"HAZ_NHAZ": "Hazardous and non-hazardous - Total"}},
    "operation": {"dimension": "wst_oper", "labels": {"TRT": "Waste treatment", "RCV_R": "Recovery - recycling",
                                                      "DSP_L": "Disposal - landfill and other (verify codes)"}},
    "unit": {"dimension": "unit", "labels": {"T": "Tonne"}},
    "area": {"dimension": "geo", "scheme": "eurostat-geo", "labels": {"DE": "Germany", "FR": "France"}},
    "definition": {"source_text": "Waste treated in the reporting country by treatment operation (wst_oper) as "
                                  "reported under the Waste Statistics Regulation (verify codes)",
                   "scope": "total waste, the declared treatment operations",
                   "methodology_notes": ["Reference years are biennial (even years); odd years are not collected"]},
    "references": REFS_ESTAT,
}
CEI_WM = {
    "label": "Eurostat recycling rate of municipal waste (cei_wm011, DE and FR)",
    "flow": "cei_wm011", "key": "A.RT.DE+FR", "params": {"startPeriod": "2094"}, "periodicity": "annual",
    "indicator": {"codes": {"cei_wm011": {"concept": "municipal_waste_recycling_rate",
                                          "label": "Recycling rate of municipal waste"}}},
    "unit": {"dimension": "unit", "labels": {"RT": "Percentage (verify the unit code)"}},
    "area": {"dimension": "geo", "scheme": "eurostat-geo", "labels": {"DE": "Germany", "FR": "France"}},
    "definition": {"source_text": "Recycling rate of municipal waste as Eurostat defines it in the circular economy "
                                  "monitoring framework; stored as published, never recomputed",
                   "scope": "municipal waste",
                   "methodology_notes": ["Eurostat-published rate; Noesis computes no rate from tonnages"]},
    "references": REFS_CEI,
}
CEI_CMU = {
    "label": "Eurostat circular material use rate (cei_srm030, DE and FR)",
    "flow": "cei_srm030", "key": "A.RT.DE+FR", "params": {"startPeriod": "2094"}, "periodicity": "annual",
    "indicator": {"codes": {"cei_srm030": {"concept": "circular_material_use_rate",
                                           "label": "Circular material use rate"}}},
    "unit": {"dimension": "unit", "labels": {"RT": "Percentage (verify the unit code)"}},
    "area": {"dimension": "geo", "scheme": "eurostat-geo", "labels": {"DE": "Germany", "FR": "France"}},
    "definition": {"source_text": "Circular material use rate as Eurostat defines it in the circular economy "
                                  "monitoring framework; stored as published, never recomputed",
                   "scope": "whole economy",
                   "methodology_notes": ["Eurostat-published rate; Noesis computes no material-flow figure"]},
    "references": REFS_CEI,
}
EEA = {
    "label": "EEA Industrial Reporting off-site waste transfers, Berlin facility selection, reporting year 2096",
    "selection": {"country": "DE", "city": "Berlin", "reporting_year": 2096, "limit": 500,
                  "facility_selection": "environment.core eea-industry"},
    "definition": {"source_text": "Off-site transfers of hazardous and non-hazardous waste per facility and reporting "
                                  "year, by recovery (R) or disposal (D) and domestic or transboundary, in tonnes with "
                                  "the method code (verify the column names)",
                   "scope": "facilities of the environment.core Berlin selection, one reporting year",
                   "methodology_notes": [("Facilities report only above the E-PRTR thresholds (verify); an absent "
                                          "row is not zero"), "Transfers are never summed into national totals"]},
    "references": REFS_EEA,
}
OECD_FLOW = "OECD.ENV.EPI,DSD_MUNW@DF_MUNW,1.0"
OECD = {
    "label": "OECD municipal waste generated, total (DEU and FRA)",
    "flow": OECD_FLOW, "key": "DEU+FRA.A.MW_GEN.T", "params": {"startPeriod": "2094"}, "periodicity": "annual",
    "dataflow": {"verify": "agency OECD.ENV.EPI, dataflow DSD_MUNW@DF_MUNW, version 1.0 and the dimensions "
                           "REF_AREA, FREQ, MEASURE and UNIT_MEASURE are not known with confidence; verify before the "
                           "live run (WC13)"},
    "indicator": {"dimension": "MEASURE", "codes": {"MW_GEN": {"concept": "municipal_waste_generated",
                                                               "label": "Municipal waste generated, total"}}},
    "waste_category": {"code": "MUNICIPAL", "scheme": "OECD municipal waste", "label": "Municipal waste"},
    "unit": {"dimension": "UNIT_MEASURE", "labels": {"T": "Tonnes"}},
    "area": {"dimension": "REF_AREA", "scheme": "iso3166-1-alpha3", "labels": {"DEU": "Germany", "FRA": "France"}},
    "release": {"published_on": "2098-11-20",
                "label": "OECD municipal waste (fixture declaration; verify the release calendar)"},
    "definition": {"source_text": "Municipal waste generated as the OECD defines it (waste collected by or on behalf of "
                                  "municipalities; verify)",
                   "scope": "total municipal waste",
                   "methodology_notes": [("For EU members the OECD and Eurostat figures share the joint "
                                          "questionnaire (verify) yet stay separate series")]},
    "references": REFS_OECD,
}

EST_HEADER = ["DATAFLOW", "LAST UPDATE", "freq", "unit", "hazard", "nace_r2", "waste", "geo", "TIME_PERIOD",
              "OBS_VALUE", "OBS_FLAG"]
TRT_HEADER = ["DATAFLOW", "LAST UPDATE", "freq", "unit", "hazard", "wst_oper", "waste", "geo", "TIME_PERIOD",
              "OBS_VALUE", "OBS_FLAG"]
CEI_HEADER = ["DATAFLOW", "LAST UPDATE", "freq", "unit", "geo", "TIME_PERIOD", "OBS_VALUE", "OBS_FLAG"]
OECD_HEADER = ["DATAFLOW", "REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"]


def _csv(rows, header):
    return "\n".join([",".join(header)] + [",".join(str(c) for c in r) for r in rows]) + "\n"


def wasgen(stamp, revised=False):
    f = "ESTAT:ENV_WASGEN(1.0)"
    base = (f, stamp, "A", "T")
    rows = [
        (*base, "HAZ_NHAZ", "TOTAL_HH", "TOTAL", "DE", "2094", "403900000" if revised else "401200000", ""),
        (*base, "HAZ_NHAZ", "TOTAL_HH", "TOTAL", "DE", "2096", "398700000", "" if revised else "p"),
        (*base, "HAZ", "TOTAL_HH", "TOTAL", "DE", "2094", "23100000", ""),
        (*base, "HAZ", "TOTAL_HH", "TOTAL", "DE", "2096", "22800000", ""),
        (*base, "HAZ_NHAZ", "TOTAL_HH", "TOTAL", "FR", "2094", "312400000", ""),
        (*base, "HAZ_NHAZ", "TOTAL_HH", "TOTAL", "FR", "2096", "309900000", "b"),
        (*base, "HAZ", "TOTAL_HH", "TOTAL", "FR", "2094", "11700000", ""),
        (*base, "HAZ", "TOTAL_HH", "TOTAL", "FR", "2096", ":", "c"),
    ]
    return _csv(rows, EST_HEADER)


def wastrt(stamp, revised=False):
    base = ("ESTAT:ENV_WASTRT(1.0)", stamp, "A", "T", "HAZ_NHAZ")
    rows = [
        (*base, "TRT", "TOTAL", "DE", "2094", "381000000", ""),
        (*base, "TRT", "TOTAL", "DE", "2096", "377500000", ""),
        (*base, "RCV_R", "TOTAL", "DE", "2094", "264000000", ""),
        (*base, "RCV_R", "TOTAL", "DE", "2096", "267300000" if revised else "266100000", ""),
        (*base, "DSP_L", "TOTAL", "DE", "2094", "61000000", ""),
        (*base, "DSP_L", "TOTAL", "DE", "2096", "58200000", ""),
        (*base, "TRT", "TOTAL", "FR", "2094", "298000000", ""),
        (*base, "TRT", "TOTAL", "FR", "2096", "296400000", ""),
        (*base, "RCV_R", "TOTAL", "FR", "2094", "190500000", ""),
        (*base, "RCV_R", "TOTAL", "FR", "2096", "193000000", ""),
    ]
    if not revised:  # the revision no longer states FR landfill: a removed_by_source vintage
        rows += [(*base, "DSP_L", "TOTAL", "FR", "2094", "88100000", ""),
                 (*base, "DSP_L", "TOTAL", "FR", "2096", "84300000", "")]
    return _csv(rows, TRT_HEADER)


def cei(flow, stamp, values):
    return _csv([(f"ESTAT:{flow.upper()}(1.0)", stamp, "A", "RT", geo, year, value, flag)
                 for geo, year, value, flag in values], CEI_HEADER)


CEI_WM_FIRST = [("DE", "2094", "66.1", ""), ("DE", "2095", "66.8", ""), ("DE", "2096", "67.2", "p"),
                ("FR", "2094", "41.9", ""), ("FR", "2095", "42.4", ""), ("FR", "2096", "43.0", "e")]
CEI_WM_SECOND = [("DE", "2094", "66.1", ""), ("DE", "2095", "66.8", ""), ("DE", "2096", "67.5", ""),
                 ("DE", "2097", "68.0", "p"), ("FR", "2094", "41.9", ""), ("FR", "2095", "42.4", ""),
                 ("FR", "2096", "43.0", "e"), ("FR", "2097", "43.6", "p")]
CEI_CMU_VALUES = [("DE", "2094", "12.9", ""), ("DE", "2095", "13.1", ""), ("DE", "2096", "13.4", ""),
                  ("FR", "2094", "19.6", ""), ("FR", "2095", "19.9", ""), ("FR", "2096", "20.3", "")]


def oecd(values):
    return _csv([("OECD.ENV.EPI:DSD_MUNW@DF_MUNW(1.0)", geo, "A", "MW_GEN", "T", year, value, status)
                 for geo, year, value, status in values], OECD_HEADER)


OECD_FIRST = [("DEU", "2094", "50120000", "A"), ("DEU", "2095", "50480000", "A"), ("DEU", "2096", "50910000", "P"),
              ("FRA", "2094", "37900000", "A"), ("FRA", "2095", "38210000", "A"), ("FRA", "2096", "36880000", "B")]
OECD_SECOND = [("DEU", "2094", "50120000", "A"), ("DEU", "2095", "50480000", "A"), ("DEU", "2096", "50910000", "P"),
               ("FRA", "2094", "37900000", "A"), ("FRA", "2095", "38350000", "A"), ("FRA", "2096", "36880000", "B")]

FACILITY_1 = "DE.UBA.PRTR/000000901.FACILITY"
FACILITY_2 = "DE.UBA.PRTR/000000902.FACILITY"
UNKNOWN_FACILITY = "DE.UBA.PRTR/000000999.FACILITY"


def transfer(inspire, hazard, treatment, destination, quantity, method, year=2096):
    return {"FacilityInspireId": inspire, "reportingYear": year, "wasteClassification": hazard,
            "wasteTreatment": treatment, "transboundary": destination, "totalWasteQuantityTNE": quantity,
            "methodCode": method}


EEA_FIRST = {"datasetVersion": "v12.0 (authored fixture)", "datasetPublished": "2098-05-12", "results": [
    transfer(FACILITY_1, "HW", "D", "DOMESTIC", 12.5, "M"),
    transfer(FACILITY_1, "NONHW", "R", "DOMESTIC", 840.25, "C"),
    transfer(FACILITY_1, "HW", "R", "TRANSBOUNDARY", 3.1, "E"),
    transfer(FACILITY_2, "NONHW", "R", "DOMESTIC", 2210, "M"),
    transfer(UNKNOWN_FACILITY, "NONHW", "D", "DOMESTIC", 55, "E")]}
EEA_SECOND = {"datasetVersion": "v13.0 (authored fixture)", "datasetPublished": "2099-04-02", "results": [
    transfer(FACILITY_1, "HW", "D", "DOMESTIC", 12.5, "M"),
    transfer(FACILITY_1, "NONHW", "R", "DOMESTIC", 851.75, "M"),
    transfer(FACILITY_2, "HW", "D", "DOMESTIC", 0.8, "M"),
    transfer(FACILITY_2, "NONHW", "R", "DOMESTIC", 2210, "M"),
    transfer(UNKNOWN_FACILITY, "NONHW", "D", "DOMESTIC", 55, "E")]}


def _page(fmt, document, body, status=200):
    from src.ingestion.waste_sources import fixture_request

    return {"request": fixture_request(fmt, document), "status": status, "body": body}


def _sources():
    eurostat, cei_fmt, eea_fmt, oecd_fmt = ("eurostat-waste-sdmx-csv", "eurostat-cei-sdmx-csv",
                                            "eea-waste-transfers-json", "oecd-waste-csv")
    return {
        "eurostat-waste": (eurostat, [WASGEN, WASTRT],
                           [_page(eurostat, WASGEN, wasgen("15/03/98 11:00:00")),
                            _page(eurostat, WASTRT, wastrt("15/03/98 11:00:00"))],
                           [_page(eurostat, WASGEN, wasgen("20/02/99 11:00:00", True)),
                            _page(eurostat, WASTRT, wastrt("20/02/99 11:00:00", True))],
                           ["authored-fixture", "last-update-release", "biennial-gaps", "obs-flags",
                            "confidential-cell", "hazardousness", "treatment-operations"],
                           ["resubmitted-past-year", "provisional-confirmed", "removed-by-source"]),
        "eurostat-circular-economy": (cei_fmt, [CEI_WM, CEI_CMU],
                                      [_page(cei_fmt, CEI_WM, cei("cei_wm011", "22/04/98 11:00:00", CEI_WM_FIRST)),
                                       _page(cei_fmt, CEI_CMU, cei("cei_srm030", "22/04/98 11:00:00",
                                                                   CEI_CMU_VALUES))],
                                      [_page(cei_fmt, CEI_WM, cei("cei_wm011", "10/03/99 11:00:00", CEI_WM_SECOND)),
                                       _page(cei_fmt, CEI_CMU, cei("cei_srm030", "22/04/98 11:00:00",
                                                                   CEI_CMU_VALUES))],
                                      ["authored-fixture", "last-update-release", "published-rates", "obs-flags"],
                                      ["new-period", "revised-value", "unchanged-release"]),
        "eea-industry-waste-transfers": (eea_fmt, [EEA],
                                         [_page(eea_fmt, EEA, json.dumps(EEA_FIRST, indent=1))],
                                         [_page(eea_fmt, EEA, json.dumps(EEA_SECOND, indent=1))],
                                         ["authored-fixture", "dataset-version", "facility-transfer-rows",
                                          "method-codes", "unmatched-inspire-id"],
                                         ["corrected-past-year-row", "new-row", "removed-by-source"]),
        "oecd-municipal-waste": (oecd_fmt, [OECD], [_page(oecd_fmt, OECD, oecd(OECD_FIRST))],
                                 [_page(oecd_fmt, OECD, oecd(OECD_SECOND))],
                                 ["authored-fixture", "declared-release", "obs-status-break", "verify-dataflow"],
                                 ["revised-value", "re-declared-release"]),
    }


FILES = {"eurostat-waste": "environment-waste-eurostat.json",
         "eurostat-circular-economy": "environment-waste-eurostat-cei.json",
         "eea-industry-waste-transfers": "environment-waste-eea-transfers.json",
         "oecd-municipal-waste": "environment-waste-oecd.json"}
REVISIONS = {"eurostat-waste": "eurostat_waste_revision.json",
             "eurostat-circular-economy": "eurostat_cei_revision.json",
             "eea-industry-waste-transfers": "eea_transfers_revision.json",
             "oecd-municipal-waste": "oecd_revision.json"}
EUROSTAT_LICENCE = {"id": "eurostat-reuse", "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
                    "redistribution": "attribution-required"}
META = {
    "eurostat-waste": {
        "publisher": "Eurostat (waste statistics)",
        "endpoint": "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data",
        "scope": "Bounded Eurostat waste statistics through the SDMX connector's ESTAT SDMX-CSV path: env_wasgen total "
                 "waste (all NACE activities plus households; hazardous and non-hazardous total, and hazardous) and "
                 "env_wastrt total waste by treatment operation for Germany and France from a declared start year; "
                 "biennial, odd years absent; at most 2 documents and 60 series per response (operator must verify "
                 "the dataset codes, dimension order, wst_oper codes and flags)",
        "license": EUROSTAT_LICENCE,
        "update_cadence": "biennial reference years; countries resubmit earlier years",
        "temporal_semantics": "the SDMX-CSV LAST UPDATE column dates the release; TIME_PERIOD is the (biennial) "
                              "reference year; a changed release is a new vintage",
        "budgets": {"timeout_ms": 30000, "max_results": 60, "max_bytes": 2000000, "max_pages": 2},
    },
    "eurostat-circular-economy": {
        "publisher": "Eurostat (circular economy monitoring framework)",
        "endpoint": "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data",
        "scope": "Bounded Eurostat circular-economy indicators through the same SDMX-CSV path: cei_wm011 (recycling "
                 "rate of municipal waste) and cei_srm030 (circular material use rate) for Germany and France from a "
                 "declared start year, stored as Eurostat publishes them; one document per dataset, at most 10 "
                 "series per response (operator must verify the codes and units)",
        "license": EUROSTAT_LICENCE,
        "update_cadence": "annual releases that may revise earlier years",
        "temporal_semantics": "the SDMX-CSV LAST UPDATE column dates the release; TIME_PERIOD is the reference year; "
                              "a changed release is a new vintage",
        "budgets": {"timeout_ms": 30000, "max_results": 10, "max_bytes": 1000000, "max_pages": 2},
    },
    "eea-industry-waste-transfers": {
        "publisher": "European Environment Agency (Industrial Reporting under the IED and E-PRTR)",
        "endpoint": "https://discodata.eea.europa.eu/sql",
        "scope": "Off-site waste transfers (hazardous or non-hazardous, recovery or disposal, domestic or "
                 "transboundary, tonnes and method code) for the environment.core Berlin facility selection and one "
                 "reporting year, one pinned Discodata query selecting no operator, parent-company, address, contact "
                 "or authority column; at most 500 rows (nrOfHits), a full page recorded as truncated (operator must "
                 "verify the table and column names)",
        "license": {"id": "cc-by-4.0", "terms_url": "https://www.eea.europa.eu/en/legal-notice",
                    "redistribution": "EEA standard re-use policy with attribution, citing the dataset version "
                                      "(verify)"},
        "update_cadence": "yearly publication; member states correct past reporting years",
        "temporal_semantics": "the dataset version the EEA states dates the acquisition, else the retrieval time "
                              "(labelled retrieval_time); a changed row for a past reporting year is a new vintage",
        "budgets": {"timeout_ms": 30000, "max_results": 500, "max_bytes": 2000000, "max_pages": 1},
    },
    "oecd-municipal-waste": {
        "publisher": "OECD (Environment statistics)",
        "endpoint": "https://sdmx.oecd.org/public/rest/data",
        "scope": "Bounded OECD municipal waste generated, total, for Germany (DEU) and France (FRA) from a declared "
                 "start year through the SDMX connector's OECD path (format=csvfile); one document, at most 10 series, "
                 "paced at one request per 60 seconds (operator must verify the dataflow agency, id, version and "
                 "dimensions)",
        "license": {"id": "cc-by-4.0", "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
                    "redistribution": "attribution-required (CC BY 4.0 since 2024, verify)"},
        "update_cadence": "annual; revisions without an update stamp",
        "temporal_semantics": "no update stamp: the declared release date dates a changed response, else the "
                              "retrieval time (labelled retrieval_time); OBS_STATUS B is a source-stated break",
        "budgets": {"timeout_ms": 30000, "max_results": 10, "max_bytes": 1000000, "max_pages": 1},
    },
}
DEFAULTS = {
    "connector": "waste",
    "update_cadence": "monthly re-check of the declared documents at most; every refresh stays inside the declared "
                      "selection",
    "temporal_semantics": "statistical series carry the source's release clock (Eurostat LAST UPDATE, a declared OECD "
                          "release date or the labelled retrieval time); EEA transfer rows carry the stated dataset "
                          "version, else the retrieval time; a changed release is an appended vintage and absence "
                          "from a later complete release is a removed_by_source vintage",
    "mapping": {"target_schema": "noesis-waste-record-v2", "version": "2.0.0"},
    "extractor_versions": ["waste:1.0.0"],
    "operations": ["release"],
    "schedule": {"kind": "interval", "interval_s": 2592000},
    "auth": {"kind": "none"},
    "health": {"required": False, "max_staleness_s": 31536000},
    "budgets": {"timeout_ms": 30000, "max_results": 60, "max_bytes": 2000000, "max_pages": 2},
    "policy": {"excluded": ["nowcasting", "filled years (biennial odd years stay absent)",
                            "blending Eurostat, OECD and EEA figures",
                            "summing facility transfers into national totals",
                            "own recycling rates, per-capita or material-flow figures", "derived indicators",
                            "forecasts", "operator, parent-company, address, contact and competent-authority fields",
                            "a second facility register"],
               "values": "as published, with flags, method codes and release vintages"},
}
DESCRIPTION = ("Climate and Environment waste and circular-economy sources for the optional waste features "
               "(environment.waste, #2740): Eurostat waste generation and treatment (biennial), Eurostat "
               "circular-economy indicators as published, EEA Industrial Reporting off-site waste transfers for the "
               "environment.core Berlin facilities, and OECD municipal waste. Every source is unverified-live until a "
               "dated run (WC13, #2810); no nowcasting, filled years, blending of Eurostat, OECD and EEA figures, "
               "summed facility transfers, own rates or derived indicators.")


def build(write: bool = True) -> dict[Path, str]:
    """Regenerate the fixtures, the revision fixtures and the pinned manifest; returns {path: text}."""
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    outputs: dict[Path, str] = {}
    sources = []
    for provider, (fmt, documents, first, second, scenarios, revision_scenarios) in _sources().items():
        text = json.dumps({"captured": None, "native_pages": first, "note": NOTE, "scenarios": scenarios},
                          indent=1, ensure_ascii=False) + "\n"
        outputs[OUT / FILES[provider]] = text
        revision = {"native_pages": second, "note": NOTE, "scenarios": revision_scenarios}
        if provider == "oecd-municipal-waste":
            revision["release_declarations"] = {OECD_FLOW: {
                "published_on": "2099-03-31", "label": "OECD municipal waste (fixture re-declaration; verify)"}}
        outputs[REVISION_DIR / REVISIONS[provider]] = json.dumps(revision, indent=1, ensure_ascii=False) + "\n"
        meta = META[provider]
        sources.append({
            "source_id": provider, "connector": "waste", "endpoint": meta["endpoint"], "publisher": meta["publisher"],
            "scope": meta["scope"], "license": meta["license"], "update_cadence": meta["update_cadence"],
            "temporal_semantics": meta["temporal_semantics"], "auth": {"kind": "none"}, "budgets": meta["budgets"],
            "waste": {"namespace": "environment", "provider": provider, "format": fmt,
                      "live_verification": "unverified-live", "documents": copy.deepcopy(documents)},
            "mapping": {"target_schema": "noesis-waste-record-v2", "version": "2.0.0"},
            "extractor_versions": ["waste:1.0.0"], "operations": ["release"],
            "health": {"required": False, "max_staleness_s": 31536000},
            "fixture": {"path": f"tests/fixtures/source_packs/{FILES[provider]}",
                        "sha256": hashlib.sha256(text.encode()).hexdigest(), "expected_output_hash": "0" * 64},
        })
    manifest = {"pack_id": "climate-environment-waste", "version": "1.0.0", "description": DESCRIPTION,
                "domains": ["environment"], "defaults": DEFAULTS, "sources": sources}
    if write:
        for path, text in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        result = SourcePackConformance(ROOT).offline(validate_source_pack(manifest))
        hashes = {r["source_id"]: r["output_hash"] for r in result["sources"]}
    else:
        hashes = {s["source_id"]: s["fixture"]["expected_output_hash"] for s in json.loads(PACK.read_text())["sources"]}
    for source in manifest["sources"]:
        source["fixture"]["expected_output_hash"] = hashes[source["source_id"]]
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    outputs[PACK] = text
    if write:
        PACK.write_text(text, encoding="utf-8")
    return outputs


if __name__ == "__main__":
    for written in build():
        print(written.relative_to(ROOT))

"""Authored native responses for the Clinical Evidence ``clinical.devices`` provider (#2654).

Every company, device, product code (``ZZA``, ``ZZB``, ``ZZC``), K and P number,
recall and report number, DI, SRN and certificate is fictional. Contact persons,
street addresses, MAUDE patient blocks and narratives carry placeholders that the
parsers drop or gate (MD01 minimisation). ``v2`` responses are later publications:
a new PMA supplement, a terminated recall, a new GUDID version and a suspended
certificate. Run ``python -m tests.unit.medical_devices_fixture_builder`` to
rewrite ``tests/fixtures/medical_devices``; a test checks that the files equal
this module's output. Nothing here is live coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/medical_devices"
DISCLAIMER = ("Do not rely on openFDA to make decisions regarding medical care. While we make every effort to ensure "
              "that data is accurate, you should assume all results are unvalidated. (Authored fixture wording.)")
GUDID_A, GUDID_B = "00899999000011", "04099999000025"
GUDID_A_PACKAGE, GUDID_B_PACKAGE = "10899999000018", "14099999000022"
SRN = "DE-MF-000099901"
BASIC_UDI_DI = "4099999FIXTUREFLOWA1"
CERTIFICATE = "9999-MDR-0001"
RECALL = "Z-9901-2099"
# Placeholder personal data present only in the native responses; none may survive acquisition.
PERSONAL = ("PAT PLACEHOLDER", "Pat Placeholder", "555-0100", "1 EXAMPLE WAY", "MUSTERWEG 1", "Musterweg 1",
            "JORDAN MOCK", "Jo Sample", "placeholder@example.org", "77 YR", "PLACEHOLDER")


def _meta(total: int, updated: str) -> dict[str, Any]:
    return {"disclaimer": DISCLAIMER, "terms": "https://open.fda.gov/terms/",
            "license": "https://open.fda.gov/license/", "last_updated": updated,
            "results": {"skip": 0, "limit": 100, "total": total}}


def _fda(rows: list[dict[str, Any]], updated: str = "2099-07-01") -> dict[str, Any]:
    return {"meta": _meta(len(rows), updated), "results": rows}


_EXAMPLE = {"applicant": "EXAMPLE MEDICAL DEVICES INC", "city": "EXAMPLETON", "state": "EX", "country_code": "US"}
_PMA = {"pma_number": "P999901", "applicant": "EXAMPLE MEDICAL DEVICES INC", "city": "EXAMPLETON", "state": "EX",
        "street_1": "1 EXAMPLE WAY", "zip_code": "99999", "trade_name": "EXAMPLEVALVE HEART VALVE",
        "generic_name": "Replacement heart valve", "product_code": "ZZB", "advisory_committee": "CV",
        "docket_number": "", "expedited_review_flag": "N"}
_ORIGINAL = {**_PMA, "supplement_number": "", "supplement_type": "", "supplement_reason": "", "decision_code": "APPR",
             "decision_date": "2097-05-01", "date_received": "2096-08-01",
             "ao_statement": "Approval for the EXAMPLEVALVE heart valve."}
_S1 = {**_PMA, "supplement_number": "S001", "supplement_type": "Normal 180 Day Track",
       "supplement_reason": "Labeling change - indications/instructions/shelf life/tradename", "decision_code": "APPR",
       "decision_date": "2098-02-01", "date_received": "2097-09-01", "ao_statement": "Approval for a labeling change."}
_S2 = {**_PMA, "supplement_number": "S002", "supplement_type": "Real-Time Process",
       "supplement_reason": "Change design/components/specifications/material", "decision_code": "APPR",
       "decision_date": "2099-04-10", "date_received": "2099-01-05",
       "ao_statement": "Approval for a valve delivery system supplied with the drug component approved under "
                       "NDA099001 (combination product)."}
_S3 = {**_PMA, "supplement_number": "S003", "supplement_type": "30-Day Notice",
       "supplement_reason": "Process change - manufacturer/sterilizer/packager/supplier", "decision_code": "LE30",
       "decision_date": "2099-10-01", "date_received": "2099-09-01", "ao_statement": "30-day notice accepted."}
_RECALL = {"cfres_id": "999901", "product_res_number": RECALL, "res_event_number": "99901",
           "recall_status": "Open, Classified", "event_date_initiated": "2099-02-01",
           "event_date_posted": "2099-03-01", "product_code": "ZZA", "k_numbers": ["K999901"], "pma_numbers": [],
           "product_description": "EXAMPLEPUMP infusion pump, model EP-100",
           "code_info": "Serial numbers EP100-0001 to EP100-0500",
           "reason_for_recall": "Software may stop an infusion without an alarm (fictional).",
           "root_cause_description": "Software design", "action": "Firm sent an urgent notice to customers.",
           "recalling_firm": "EXAMPLE MEDICAL DEVICES INC", "address_1": "1 EXAMPLE WAY", "city": "EXAMPLETON",
           "state": "EX", "postal_code": "99999", "country": "United States", "product_quantity": "500",
           "distribution_pattern": "US nationwide", "firm_fei_number": "9999901",
           "additional_info_contact": "Pat Placeholder 555-0100"}
_ENFORCEMENT = {"recall_number": RECALL, "event_id": "99901", "classification": "Class II", "status": "Ongoing",
                "recall_initiation_date": "20990201", "center_classification_date": "20990225",
                "report_date": "20990305", "voluntary_mandated": "Voluntary: Firm initiated",
                "initial_firm_notification": "Letter", "product_description": "EXAMPLEPUMP infusion pump, model EP-100",
                "code_info": "Serial numbers EP100-0001 to EP100-0500",
                "reason_for_recall": "Software may stop an infusion without an alarm (fictional).",
                "recalling_firm": "EXAMPLE MEDICAL DEVICES INC", "address_1": "1 EXAMPLE WAY", "city": "EXAMPLETON",
                "state": "EX", "postal_code": "99999", "country": "United States", "product_quantity": "500",
                "distribution_pattern": "US nationwide", "product_code": "ZZA"}
_CERTIFICATE = {"certificateNumber": CERTIFICATE, "notifiedBody": {"number": "9999", "name": "Example Notified Body"},
                "manufacturerSrn": SRN, "certificateType": "EU technical documentation assessment", "status": "Valid",
                "statusDate": "2098-01-15", "issueDate": "2098-01-15", "startingValidityDate": "2098-01-15",
                "expiryDate": "2103-01-14", "revision": 0, "basicUdiDis": [BASIC_UDI_DI]}


def _event(number: str, key: str, kind: str, received: str, udi: str | None = None,
           narrative: str = "Device stopped during use; no further details (fictional).") -> dict[str, Any]:
    return {"report_number": number, "mdr_report_key": key, "event_type": kind, "date_received": received,
            "date_of_event": received, "date_report": received, "report_source_code": "Manufacturer report",
            "type_of_report": ["Initial submission"], "adverse_event_flag": "Y" if kind == "Injury" else "N",
            "product_problem_flag": "Y", "product_problems": ["Infusion or Flow Problem"],
            "device": [{"brand_name": "EXAMPLEPUMP", "generic_name": "INFUSION PUMP",
                        "manufacturer_d_name": "EXAMPLE MEDICAL DEVICES INC", "device_report_product_code": "ZZA",
                        "model_number": "EP-100", "catalog_number": "EP-100-US", "udi_di": udi,
                        "device_sequence_number": "1", "device_availability": "No",
                        "manufacturer_d_address_1": "1 EXAMPLE WAY", "manufacturer_d_zip_code": "99999"}],
            "patient": [{"patient_sequence_number": "1", "patient_age": "77 YR", "patient_sex": "Female",
                         "patient_weight": "70", "patient_problems": ["No Known Impact Or Consequence To Patient"]}],
            "mdr_text": [{"text_type_code": "Description of Event or Problem", "text": narrative}],
            "manufacturer_contact_f_name": "PAT", "manufacturer_contact_l_name": "PLACEHOLDER",
            "manufacturer_contact_area_code": "555", "manufacturer_contact_phone_number": "0100",
            "reporter_occupation_code": "NURSE", "initial_report_to_fda": "Unknown"}


def _gudid(di: str, package: str, brand: str, model: str, company: str, duns: str, submission: str, version: int,
           published: str, status: str = "In Commercial Distribution") -> dict[str, Any]:
    return {"gudid": {"device": {
        "publicDeviceRecordKey": f"rk-{di}", "publicVersionStatus": "Update", "deviceRecordStatus": "Published",
        "publicVersionNumber": str(version), "publicVersionDate": published, "devicePublishDate": "2097-01-10",
        "deviceCommDistributionStatus": status, "brandName": brand, "versionModelNumber": model,
        "catalogNumber": model + "-CAT", "companyName": company, "dunsNumber": duns,
        "deviceDescription": f"{brand} (fictional device)", "deviceCombinationProduct": "false", "deviceKit": "false",
        "identifiers": {"identifier": [
            {"deviceId": di, "deviceIdType": "Primary", "deviceIdIssuingAgency": "GS1"},
            {"deviceId": package, "deviceIdType": "Package", "deviceIdIssuingAgency": "GS1", "containsDINumber": di,
             "pkgQuantity": "10", "pkgStatus": "In Commercial Distribution"}]},
        "productCodes": {"fdaProductCode": [{"productCode": "ZZA", "productCodeName": "fictional"}]},
        "premarketSubmissions": {"premarketSubmission": [{"submissionNumber": submission, "supplementNumber": ""}]},
        "gmdnTerms": {"gmdn": [{"gmdnPTName": "General-purpose infusion pump"}]},
        "contacts": {"customerContact": [{"phone": "+1(555)555-0100", "email": "placeholder@example.org"}]}}}}


def _history(versions: list[tuple[int, str, str]]) -> dict[str, Any]:
    return {"history": [{"publicVersionNumber": str(n), "publicVersionDate": d, "publicVersionStatus": s}
                        for n, d, s in versions]}


def responses() -> dict[str, Any]:
    """{relative path: body} for every authored response (``v2/`` = later publications)."""
    k1 = {"k_number": "K999901", "decision_code": "SESE", "decision_description": "Substantially Equivalent",
          "decision_date": "2098-03-10", "date_received": "2097-11-02", "clearance_type": "Traditional",
          "device_name": "EXAMPLEPUMP INFUSION PUMP", "product_code": "ZZA", "advisory_committee": "HO",
          "statement_or_summary": "Summary", "third_party_flag": "N", "expedited_review_flag": "N",
          "contact": "PAT PLACEHOLDER", "address_1": "1 EXAMPLE WAY", "address_2": "SUITE 9", "zip_code": "99999",
          **_EXAMPLE}
    k2 = {"k_number": "K999902", "decision_code": "SESE", "decision_description": "Substantially Equivalent",
          "decision_date": "2099-06-15", "date_received": "2099-01-20", "clearance_type": "Traditional",
          "device_name": "FIXTUREFLOW INFUSION SET", "product_code": "ZZA", "advisory_committee": "HO",
          "statement_or_summary": "Statement", "third_party_flag": "N", "expedited_review_flag": "N",
          "applicant": "FIXTURETECH GMBH", "city": "MUSTERSTADT", "state": "", "country_code": "DE",
          "contact": "JORDAN MOCK", "address_1": "MUSTERWEG 1", "postal_code": "99999"}
    classification = {"device_class": "2", "regulation_number": "880.5725", "medical_specialty": "HO",
                      "medical_specialty_description": "General Hospital", "review_panel": "HO",
                      "submission_type_id": "1", "definition": "", "implant_flag": "N",
                      "life_sustain_support_flag": "N", "gmp_exempt_flag": "N", "third_party_flag": "N"}
    out: dict[str, Any] = {
        "fda_510k_ZZA.json": _fda([k1, k2]),
        "fda_510k_ZZC.json": {"error": {"code": "NOT_FOUND", "message": "No matches found!"}},
        "fda_pma_ZZB.json": _fda([_ORIGINAL, _S1, _S2]),
        "v2/fda_pma_ZZB.json": _fda([_ORIGINAL, _S1, _S2, _S3], updated="2099-10-15"),
        "fda_classification_ZZA.json": _fda([{**classification, "product_code": "ZZA",
                                              "device_name": "Pump, Infusion (fictional product code)"}]),
        "fda_classification_ZZB.json": _fda([{**classification, "product_code": "ZZB", "device_class": "3",
                                              "regulation_number": "870.3925", "medical_specialty": "CV",
                                              "medical_specialty_description": "Cardiovascular",
                                              "review_panel": "CV", "submission_type_id": "2",
                                              "implant_flag": "Y", "life_sustain_support_flag": "Y",
                                              "device_name": "Heart Valve, Replacement (fictional product code)"}]),
        "fda_recall_ZZA.json": _fda([_RECALL]),
        "v2/fda_recall_ZZA.json": _fda([{**_RECALL, "recall_status": "Terminated",
                                          "event_date_terminated": "2099-09-15"}], updated="2099-10-15"),
        f"fda_enforcement_{RECALL}.json": _fda([_ENFORCEMENT]),
        f"v2/fda_enforcement_{RECALL}.json": _fda([{**_ENFORCEMENT, "status": "Terminated",
                                                     "termination_date": "20990915"}], updated="2099-10-15"),
        "fda_event_ZZA_2099H1.json": _fda([
            _event("9999901-2099-00001", "99000001", "Malfunction", "20990115", udi=GUDID_A),
            _event("9999901-2099-00002", "99000002", "Malfunction", "20990320"),
            _event("9999901-2099-00003", "99000003", "Injury", "20990502", udi=GUDID_A,
                   narrative="Delayed infusion reported; outcome not stated (fictional)."),
        ]),
        "fda_event_count_ZZA_2099Q1.json": _fda([{"term": "Malfunction", "count": 14},
                                                 {"term": "Injury", "count": 3}]),
        "fda_event_count_ZZA_2099Q2.json": _fda([{"term": "Malfunction", "count": 9}, {"term": "Injury", "count": 2},
                                                 {"term": "No answer provided", "count": 1}]),
        f"gudid_lookup_{GUDID_A}.json": _gudid(GUDID_A, GUDID_A_PACKAGE, "EXAMPLEPUMP", "EP-100",
                                               "EXAMPLE MEDICAL DEVICES INC", "999999901", "K999901", 3,
                                               "2098-06-01"),
        f"gudid_history_{GUDID_A}.json": _history([(1, "2097-01-10", "New"), (2, "2097-08-01", "Update"),
                                                  (3, "2098-06-01", "Update")]),
        f"v2/gudid_lookup_{GUDID_A}.json": _gudid(GUDID_A, GUDID_A_PACKAGE, "EXAMPLEPUMP", "EP-100",
                                                  "EXAMPLE MEDICAL DEVICES INC", "999999901", "K999901", 4,
                                                  "2099-10-02", status="Not in Commercial Distribution"),
        f"v2/gudid_history_{GUDID_A}.json": _history([(1, "2097-01-10", "New"), (2, "2097-08-01", "Update"),
                                                     (3, "2098-06-01", "Update"), (4, "2099-10-02", "Update")]),
        f"gudid_lookup_{GUDID_B}.json": _gudid(GUDID_B, GUDID_B_PACKAGE, "FIXTUREFLOW", "FF-2", "FIXTURETECH GMBH",
                                               "999999902", "K999902", 1, "2099-07-01"),
        f"gudid_history_{GUDID_B}.json": _history([(1, "2099-07-01", "New")]),
        f"eudamed_actor_{SRN}.json": {
            "srn": SRN, "actorType": "Manufacturer", "name": "Fixturetech GmbH", "abbreviatedName": "Fixturetech",
            "countryIso2Code": "DE", "status": "Active", "versionNumber": 2, "lastUpdateDate": "2098-11-20",
            "address": {"street": "Musterweg 1", "postcode": "99999", "city": "Musterstadt"},
            "contactDetails": {"email": "placeholder@example.org", "phone": "+49 555 0100"},
            "prrc": [{"name": "Jo Sample", "email": "placeholder@example.org"}]},
        f"eudamed_device_{BASIC_UDI_DI}.json": {
            "basicUdiDi": BASIC_UDI_DI, "manufacturerSrn": SRN, "deviceName": "FIXTUREFLOW infusion set",
            "riskClass": "Class IIb", "legislation": "MDR", "status": "On the EU market", "versionNumber": 1,
            "lastUpdateDate": "2098-12-01",
            "udiDis": [{"udiDi": GUDID_B, "issuingEntity": "GS1", "tradeName": "FIXTUREFLOW",
                        "status": "On the EU market"}]},
        f"eudamed_certificate_{CERTIFICATE}.json": _CERTIFICATE,
        f"v2/eudamed_certificate_{CERTIFICATE}.json": {**_CERTIFICATE, "status": "Suspended",
                                                        "statusDate": "2099-08-01", "revision": 1},
    }
    return out


def text(body: Any) -> str:
    return json.dumps(body, indent=1, sort_keys=True) + "\n"


def build(write: bool = True) -> dict[str, str]:
    files = {name: text(body) for name, body in responses().items()}
    if write:
        (FIXTURES / "v2").mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (FIXTURES / name).write_text(content)
    return files


if __name__ == "__main__":
    print(len(build()))

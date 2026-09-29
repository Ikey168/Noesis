"""Author the medicines-regulation fixtures and pin them into the ``clinical-evidence`` source pack (#2214).

Every fixture is *authored* in the provider's documented native shape - the EMA
medicines data export (JSON) with an EPAR product-information (SmPC) text
rendition and a withdrawal public statement, openFDA Drugs@FDA, DailyMed web
services (SPL history JSON and SPL XML) and an FDA Drug Safety Communication
page (HTML). The medicines *noetiglutide* and *fixturamab*, their products,
application numbers, set ids, RxCUIs and every text are fictional placeholders
shared with ``tests/fixtures/clinical``. Nothing here is a live capture.

``python -m tests.unit.medicines_fixture_builder`` rewrites
``tests/fixtures/source_packs/medicines-*.json`` and the medicines sources of
``config/source_packs/clinical-evidence.json`` (0.1.2: the 0.1.1 sources
verbatim plus these); ``test_medicines_fixtures_and_manifest_are_pinned_and_in_sync``
fails when they drift. Earlier and later revisions (SmPC revision 3, SPL
version 7, a DSC update, a withdrawn US product) are built by the functions
below for tests only and are not pinned.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/fixtures/source_packs"
CLINICAL = ROOT / "tests/fixtures/clinical"
MANIFEST = ROOT / "config/source_packs/clinical-evidence.json"
VERSION = "0.1.2"
NAMESPACE = "clinical"
NOTE = (
    "Authored offline fixture in the provider's documented response shape; medicines, identifiers and texts are "
    "fictional placeholders. Not live evidence."
)
DESCRIPTION = (
    "Clinical Evidence: registered trials with version history, posted results, EU registry records and regulatory "
    "records (openFDA, EMA) for pinned selections. Version 0.1.1 adds public-health surveillance series (RKI open "
    "data, WHO GHO, Eurostat health through the SDMX connector, Destatis health through the GENESIS connector) with "
    "reporting and reference dates kept apart. Version 0.1.2 adds medicines regulation (EMA EPARs and product "
    "information, Drugs@FDA submission history through the openFDA connector, DailyMed SPL versions, FDA Drug "
    "Safety Communications); existing sources are unchanged. No medical advice."
)
MAPPING = {"target_schema": "noesis-clinical-medicines-record-v1", "version": "1.0.0"}
EXTRACTOR = ["medicines-sources:1.0.0"]

EMA_EXPORT = "/en/documents/report/medicines-output-medicines_json-report_en.json"
EMA_PI = "/en/documents/product-information/noetiglu-fixture-epar-product-information_en.pdf"
EMA_STATEMENT = ("/en/documents/public-statement/public-statement-fixturamab-withdrawal-marketing-authorisation-"
                 "european-union_en.pdf")
SET_ID = "9a1f0c3e-0000-4000-8000-000000009001"
DSC_PATH = ("/drugs/drug-safety-and-availability/fda-drug-safety-communication-fda-adds-warning-about-pancreatitis-"
            "noetiglutide-fixture")
DRUGSFDA_REQUEST = "/drug/drugsfda.json?search=openfda.generic_name%3A%22noetiglutide%22&limit=5"
RXNAV_RELEASE = "01-Sep-2026 (fixture)"

# ---------------------------------------------------------------------- EMA


def ema_export(revision: int = 4) -> dict[str, Any]:
    updated = {3: "2025-03-10", 4: "2026-06-01"}[revision]
    procedure = {3: "EMEA/H/C/009001/II/0009", 4: "EMEA/H/C/009001/II/0012"}[revision]
    return {"data": [
        {"category": "Human", "name_of_medicine": "Noetiglu (fixture)", "ema_product_number": "EMEA/H/C/009001",
         "medicine_status": "Authorised", "inn_common_name": "noetiglutide", "active_substance": "noetiglutide",
         "therapeutic_area_mesh": "Diabetes Mellitus, Type 2",
         "marketing_authorisation_developer_applicant_holder": "Fixture Therapeutics GmbH",
         "marketing_authorisation_date": "2020-02-10", "revision_number": str(revision),
         "last_updated_date": updated, "latest_procedure_affecting_product_information": procedure,
         "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/noetiglu-fixture"},
        {"category": "Human", "name_of_medicine": "Fixturamab (fixture)", "ema_product_number": "EMEA/H/C/009002",
         "medicine_status": "Withdrawn", "inn_common_name": "fixturamab", "active_substance": "fixturamab",
         "therapeutic_area_mesh": "Arthritis, Rheumatoid",
         "marketing_authorisation_developer_applicant_holder": "Fixture Biologics B.V.",
         "marketing_authorisation_date": "2018-05-02", "withdrawal_of_marketing_authorisation_date": "2024-11-15",
         "revision_number": "2", "last_updated_date": "2024-11-20",
         "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/fixturamab-fixture"},
        {"category": "Human", "name_of_medicine": "Unrelated medicine (fixture)", "ema_product_number": "EMEA/H/C/009003",
         "medicine_status": "Refused", "inn_common_name": "otherumab", "active_substance": "otherumab",
         "date_of_refusal_of_marketing_authorisation": "2023-01-12", "revision_number": "1",
         "last_updated_date": "2023-01-20"},
    ]}


def smpc_text(revision: int = 4) -> str:
    warnings = ("Acute pancreatitis has been observed with noetiglutide (fixture text). Patients should be informed "
                "of the characteristic symptoms of acute pancreatitis.")
    if revision == 3:
        warnings = "Noetiglutide has not been studied in patients with a history of pancreatitis (fixture text)."
    adverse = "Nausea, vomiting and diarrhoea were the most frequently reported adverse reactions (fixture text)."
    lines = [
        "ANNEX I",
        "SUMMARY OF PRODUCT CHARACTERISTICS",
        "",
        "1. NAME OF THE MEDICINAL PRODUCT",
        "Noetiglu (fixture) solution for injection in pre-filled pen",
        "",
        "2. QUALITATIVE AND QUANTITATIVE COMPOSITION",
        "Each pre-filled pen contains noetiglutide (fixture composition).",
        "",
        "4. CLINICAL PARTICULARS",
        "4.1 Therapeutic indications",
        "Noetiglu is indicated for the treatment of adults with insufficiently controlled type 2 diabetes mellitus "
        "as an adjunct to diet and exercise (fixture text).",
        "4.2 Posology and method of administration",
        "Fixture dosing text that is never retained.",
        "4.3 Contraindications",
        "Hypersensitivity to the active substance or to any of the excipients.",
        "4.4 Special warnings and precautions for use",
        warnings,
    ]
    if revision >= 4:
        lines += ["4.8 Undesirable effects", adverse]
    lines += [
        "4.9 Overdose",
        "Fixture overdose text that is never retained.",
        "",
        "5. PHARMACOLOGICAL PROPERTIES",
        "5.1 Pharmacodynamic properties",
        "Clinical efficacy and safety: the pivotal study NOETIC-1 (NCT09000001; EudraCT 2015-900001-10) is "
        "described in PMID: 99000001 (fixture text).",
        "",
        "ANNEX II",
        "A. MANUFACTURER(S) OF THE BIOLOGICAL ACTIVE SUBSTANCE",
    ]
    return "\n".join(lines) + "\n"


def statement_text() -> str:
    return ("Public statement (fixture)\n\nFixturamab (fixture): Withdrawal of the marketing authorisation in the "
            "European Union\n\nOn 15 November 2024, the European Commission withdrew the marketing authorisation "
            "for Fixturamab at the request of the marketing authorisation holder, Fixture Biologics B.V., which "
            "notified the European Commission of its decision to permanently discontinue the marketing of the "
            "product for commercial reasons (fixture text).\n")


# ---------------------------------------------------------------------- Drugs@FDA


def drugsfda(discontinued: bool = False) -> dict[str, Any]:
    payload = json.loads((CLINICAL / "openfda_drugsfda.json").read_text())
    if discontinued:
        payload = copy.deepcopy(payload)
        payload["results"][0]["products"][0]["marketing_status"] = "Discontinued"
        payload["meta"]["last_updated"] = "2026-12-01"
    return payload


# ---------------------------------------------------------------------- DailyMed


def spl_history(latest: int = 8) -> dict[str, Any]:
    versions = [{"spl_version": "8", "published_date": "Apr 15, 2025"},
                {"spl_version": "7", "published_date": "Jan 10, 2024"}]
    return {"metadata": {"total_elements": latest - 6, "db_published_date": "Sep 01, 2026 (fixture)"},
            "data": {"spl": {"setid": SET_ID, "title": "NOETIGLU (FIXTURE) (noetiglutide) injection"},
                     "history": [v for v in versions if int(v["spl_version"]) <= latest]}}


def _section(code: str, name: str, title: str, text: str, ident: str) -> str:
    return (f'<component><section ID="{ident}"><code code="{code}" codeSystem="2.16.840.1.113883.6.1" '
            f'displayName="{name}"/><title>{title}</title><text><paragraph>{text}</paragraph></text></section>'
            "</component>")


def spl_xml(version: int = 8) -> str:
    effective = {7: "20240110", 8: "20250415"}[version]
    sections = []
    if version >= 8:
        sections.append(_section("34066-1", "BOXED WARNING SECTION", "WARNING: RISK OF PANCREATITIS",
                                 "Noetiglutide can cause acute pancreatitis (fixture text).", "s-boxed"))
    sections += [
        _section("34067-9", "INDICATIONS &amp; USAGE SECTION", "1 INDICATIONS AND USAGE",
                 "NOETIGLU is indicated as an adjunct to diet and exercise to improve glycemic control in adults "
                 "with type 2 diabetes mellitus (fixture text).", "s-ind"),
        _section("34068-7", "DOSAGE &amp; ADMINISTRATION SECTION", "2 DOSAGE AND ADMINISTRATION",
                 "Fixture dosing text that is never retained.", "s-dose"),
        _section("34070-3", "CONTRAINDICATIONS SECTION", "4 CONTRAINDICATIONS",
                 "Serious hypersensitivity to noetiglutide (fixture text).", "s-contra"),
        _section("43685-7", "WARNINGS AND PRECAUTIONS SECTION", "5 WARNINGS AND PRECAUTIONS",
                 ("Pancreatitis: acute pancreatitis, including fatal cases, has been reported; discontinue promptly "
                  "if pancreatitis is suspected (fixture text).") if version >= 8 else
                 "Pancreatitis: noetiglutide has not been studied in patients with a history of pancreatitis "
                 "(fixture text).", "s-warn"),
        _section("34092-7", "CLINICAL STUDIES SECTION", "14 CLINICAL STUDIES",
                 "Study NOETIC-1 (NCT09000001) enrolled adults with type 2 diabetes (fixture text).", "s-studies"),
    ]
    if version < 8:
        sections.append(_section("34071-1", "WARNINGS SECTION", "WARNINGS",
                                 "Legacy warnings section of version 7 (fixture text).", "s-legacy"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<document xmlns="urn:hl7-org:v3"><id root="0e0a0000-0000-4000-8000-0000000090%02d"/>' % version
        + '<code code="34391-3" codeSystem="2.16.840.1.113883.6.1" displayName="HUMAN PRESCRIPTION DRUG LABEL"/>'
        + "<title>NOETIGLU (FIXTURE) (noetiglutide) injection</title>"
        + f'<effectiveTime value="{effective}"/><setId root="{SET_ID}"/><versionNumber value="{version}"/>'
        + "<component><structuredBody>"
        + '<component><section ID="s-product"><code code="48780-1" codeSystem="2.16.840.1.113883.6.1" '
          'displayName="SPL PRODUCT DATA ELEMENTS SECTION"/><subject><manufacturedProduct><manufacturedProduct>'
          "<name>NOETIGLU (FIXTURE)</name><ingredient classCode=\"ACTIB\"><ingredientSubstance>"
          "</ingredientSubstance></ingredient><activeIngredient><activeIngredientSubstance><name>NOETIGLUTIDE</name>"
          "</activeIngredientSubstance></activeIngredient></manufacturedProduct><subjectOf><approval>"
          '<id extension="NDA299001" root="2.16.840.1.113883.3.150"/></approval></subjectOf></manufacturedProduct>'
          "</subject></section></component>"
        + "".join(sections)
        + "</structuredBody></component></document>"
    )


# ---------------------------------------------------------------------- FDA Drug Safety Communication


def dsc_html(updated: bool = False) -> str:
    update = ("<p>[6-2-2026] UPDATE: FDA has completed its review and is requiring a Boxed Warning about the risk of "
              "acute pancreatitis with noetiglutide (fixture text).</p>") if updated else ""
    return (
        "<!DOCTYPE html><html><head><title>FDA Drug Safety Communication (fixture)</title></head><body><main>"
        "<h1>FDA adds warning about pancreatitis with the diabetes medicine noetiglutide (Noetiglu) (fixture)</h1>"
        f"{update}"
        "<p>[4-15-2026] FDA Drug Safety Communication</p>"
        "<h2>What safety concern is FDA announcing?</h2>"
        "<p>The U.S. Food and Drug Administration (FDA) is warning that cases of acute pancreatitis have been "
        "reported with the type 2 diabetes medicine noetiglutide (fixture text).</p>"
        "<h2>What is FDA doing?</h2>"
        "<p>We are adding information about this risk to the Warnings and Precautions section of the prescribing "
        "information (fixture text).</p>"
        "<table><thead><tr><th>Brand name</th><th>Generic name</th></tr></thead><tbody>"
        "<tr><td>Noetiglu (fixture)</td><td>noetiglutide</td></tr></tbody></table>"
        "</main></body></html>"
    )


# ---------------------------------------------------------------------- RxNav (identity lookups, not a pack source)


def rxnav_pages() -> list[dict[str, Any]]:
    def page(request, body):
        return {"request": request, "status": 200, "body": body}

    return [
        page("/REST/version.json", {"version": RXNAV_RELEASE, "apiVersion": "3.1.0 (fixture)"}),
        page("/REST/rxcui.json?name=noetiglutide&search=0", {"idGroup": {"name": "noetiglutide",
                                                                          "rxnormId": ["9990001"]}}),
        page("/REST/rxcui/9990001/properties.json", {"properties": {"rxcui": "9990001", "name": "noetiglutide",
                                                                    "tty": "IN"}}),
        page("/REST/rxcui.json?name=noetiglu+%28fixture%29&search=0",
             {"idGroup": {"name": "noetiglu (fixture)", "rxnormId": ["9990002"]}}),
        page("/REST/rxcui/9990002/properties.json", {"properties": {"rxcui": "9990002", "name": "Noetiglu (fixture)",
                                                                    "tty": "BN"}}),
        page("/REST/rxcui/9990002/related.json?tty=IN",
             {"relatedGroup": {"conceptGroup": [{"tty": "IN", "conceptProperties": [
                 {"rxcui": "9990001", "name": "noetiglutide", "tty": "IN"}]}]}}),
        page("/REST/rxcui.json?name=fixturamab&search=0", {"idGroup": {"name": "fixturamab"}}),
        page("/REST/rxcui.json?name=fixturamab+%28fixture%29&search=0", {"idGroup": {"name": "fixturamab (fixture)"}}),
    ]


# ---------------------------------------------------------------------- sources


def _source(source_id, *, connector, publisher, endpoint, scope, cadence, temporal, config_key, config, license_,
            auth=None):
    return {
        "mapping": dict(MAPPING),
        "extractor_versions": list(EXTRACTOR),
        "health": {"required": False, "max_staleness_s": 2592000},
        "source_id": source_id,
        "connector": connector,
        "publisher": publisher,
        "endpoint": endpoint,
        "scope": scope,
        "update_cadence": cadence,
        "temporal_semantics": temporal,
        "operations": ["records"],
        "schedule": {"kind": "interval", "interval_s": 604800},
        "budgets": {"timeout_ms": 30000, "max_results": 50, "max_bytes": 20000000, "max_pages": 50},
        config_key: config,
        "license": license_,
        "auth": auth or {"kind": "none"},
    }


def sources() -> list[dict[str, Any]]:
    return [
        _source(
            "medicines-ema-epar", connector="medicines", publisher="European Medicines Agency",
            endpoint="https://www.ema.europa.eu",
            scope="Pinned EMA products: authorisation status and dates, EPAR revision, product information (SmPC) "
                  "sections and withdrawal public statements",
            cadence="daily at most; a new EPAR revision on each procedure affecting the product information",
            temporal="EPAR revision number and last-updated date per revision; authorisation event dates as "
                     "published; observation time recorded separately",
            config_key="medicines",
            config={"namespace": NAMESPACE, "provider": "ema-epar", "export_path": EMA_EXPORT,
                    "products": [{"product_number": "EMEA/H/C/009001", "product_information_path": EMA_PI},
                                 {"product_number": "EMEA/H/C/009002", "public_statement_path": EMA_STATEMENT}]},
            license_={"id": "ema-legal-notice", "terms_url": "https://www.ema.europa.eu/en/about-us/legal-notice",
                      "redistribution": "reproduction authorised provided the source (EMA) is acknowledged"}),
        _source(
            "medicines-drugsfda-submissions", connector="openfda",
            publisher="US Food and Drug Administration (openFDA)", endpoint="https://api.fda.gov",
            scope="Pinned products: Drugs@FDA applications, products and every submission (original approval, "
                  "supplements, labeling revisions) through the existing openFDA provider",
            cadence="weekly at most",
            temporal="submission status date per submission; marketing status as of openFDA meta.last_updated",
            config_key="clinical",
            config={"namespace": NAMESPACE, "products": [{"generic_name": "noetiglutide"}],
                    "endpoints": ["drugsfda-submissions"]},
            license_={"id": "openfda-terms", "terms_url": "https://open.fda.gov/terms/",
                      "redistribution": "openFDA data with its disclaimer; results are unvalidated and not medical "
                                        "advice"},
            auth={"kind": "optional-secret", "secret_ref": "NOESIS_OPENFDA_API_KEY"}),
        _source(
            "medicines-dailymed-spl", connector="medicines", publisher="US National Library of Medicine (DailyMed)",
            endpoint="https://dailymed.nlm.nih.gov",
            scope="Pinned SPL set ids: version history and the current SPL document with LOINC-coded sections",
            cadence="weekly at most; a new SPL version on each label update",
            temporal="SPL version number and effective time per revision; history published dates; observation time "
                     "recorded separately",
            config_key="medicines",
            config={"namespace": NAMESPACE, "provider": "dailymed", "set_ids": [SET_ID]},
            license_={"id": "nlm-dailymed-terms",
                      "terms_url": "https://dailymed.nlm.nih.gov/dailymed/about-dailymed.cfm",
                      "redistribution": "public label content; cite DailyMed and the set id; no NLM endorsement"}),
        _source(
            "medicines-fda-dsc", connector="medicines", publisher="US Food and Drug Administration",
            endpoint="https://www.fda.gov",
            scope="Pinned FDA Drug Safety Communications: issue date, dated updates, named substances and products",
            cadence="weekly at most; FDA adds dated updates to the same page",
            temporal="issue date and update dates as published; each update is a new revision",
            config_key="medicines",
            config={"namespace": NAMESPACE, "provider": "fda-dsc", "communications": [DSC_PATH]},
            license_={"id": "us-government-public-domain",
                      "terms_url": "https://www.fda.gov/about-fda/about-website/website-policies",
                      "redistribution": "US government work; cite FDA and the page URL"}),
    ]


def fixture(pages: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    return {"captured": None, "native_pages": pages, "note": NOTE, "authored": True, "provider": provider,
            "scenarios": ["authored-fixture", "fictional-values"]}


def fixtures() -> dict[str, dict[str, Any]]:
    return {
        "medicines-ema-epar": fixture([
            {"request": EMA_EXPORT, "status": 200, "body": ema_export(4)},
            {"request": EMA_PI, "status": 200, "body": smpc_text(4)},
            {"request": EMA_STATEMENT, "status": 200, "body": statement_text()},
        ], "EMA medicines export, EPAR product information and public statement"),
        "medicines-drugsfda-submissions": fixture([
            {"request": DRUGSFDA_REQUEST, "status": 200, "body": drugsfda()},
        ], "openFDA Drugs@FDA"),
        "medicines-dailymed-spl": fixture([
            {"request": f"/dailymed/services/v2/spls/{SET_ID}/history.json", "status": 200, "body": spl_history(8)},
            {"request": f"/dailymed/services/v2/spls/{SET_ID}.xml", "status": 200, "body": spl_xml(8)},
        ], "DailyMed web services v2"),
        "medicines-fda-dsc": fixture([
            {"request": DSC_PATH, "status": 200, "body": dsc_html()},
        ], "FDA Drug Safety Communication page"),
    }


def fixture_path(source_id: str) -> str:
    return f"tests/fixtures/source_packs/{source_id}.json"


def fixture_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def build(*, write: bool = True) -> dict[str, Any]:
    from src.ingestion.source_packs import _digest, replay_native_fixture, validate_source_pack

    current = json.loads(MANIFEST.read_text())
    manifest = copy.deepcopy(current)
    base = [s for s in manifest["sources"] if s["mapping"]["target_schema"] != MAPPING["target_schema"]]
    manifest["version"] = VERSION
    manifest["description"] = DESCRIPTION
    added = sources()
    authored = fixtures()
    for source in added:
        text = fixture_text(authored[source["source_id"]])
        if write:
            (ROOT / fixture_path(source["source_id"])).write_text(text)
        source["fixture"] = {"path": fixture_path(source["source_id"]),
                             "sha256": hashlib.sha256(text.encode()).hexdigest(), "expected_output_hash": "0" * 64}
    manifest["sources"] = base + added
    validated = validate_source_pack(manifest)
    for source in added:
        compiled = next(s for s in validated["sources"] if s["source_id"] == source["source_id"])
        # Replay the fixture as stored (sorted keys), which is what conformance serves.
        stored = json.loads(fixture_text(authored[source["source_id"]]))
        source["fixture"]["expected_output_hash"] = _digest(replay_native_fixture(compiled, stored))
    if write:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    built = build()
    print(f"wrote {MANIFEST.relative_to(ROOT)} {built['version']} with {len(built['sources'])} sources")

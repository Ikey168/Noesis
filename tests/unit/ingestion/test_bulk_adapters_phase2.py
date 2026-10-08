"""Phase-2 bulk adapters (FA04–FA06, FA08–FA11) on offline fixtures, plus redirect and SHA-1 checks."""
import bz2
import hashlib
import io
import json
import urllib.request
import zipfile

import pytest

from src.ingestion.bulk import get_bulk_adapter
from src.ingestion.bulk.http import BulkHttp
from src.ingestion.connectors.base import PermanentFetchError
from tests.unit.ingestion.test_bulk_runner import FakeWeb, read_subset, runner

LM = {"ETag": "e", "Last-Modified": "Tue, 06 Oct 2026 18:04:50 GMT"}


def zipped(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def test_eia_filters_series_and_period(tmp_path):
    manifest = {"dataset": {"TOTAL": {"accessURL": "https://www.eia.gov/opendata/bulk/TOTAL.zip",
                                      "last_updated": "2026-09-30T18:31:50-04:00"}}}
    lines = "\n".join(json.dumps(x) for x in [
        {"category_id": "1", "name": "cat"},
        {"series_id": "TOTAL.NUETPUS.M", "name": "Nuclear", "units": "GWh", "f": "M",
         "data": [["202606", 68786.1], ["202412", 1.0]]},
        {"series_id": "TOTAL.OTHER.M", "name": "x", "f": "M", "data": [["202606", 2.0]]}])
    web = FakeWeb({"https://www.eia.gov/opendata/bulk/manifest.txt": (json.dumps(manifest).encode(), {}),
                   "https://www.eia.gov/opendata/bulk/TOTAL.zip": (zipped("TOTAL.txt", lines), LM)})
    r = runner(get_bulk_adapter("eia-bulk"), tmp_path, web).run(
        {"datasets": ["TOTAL"], "series": ["TOTAL.NUETPUS.M"], "since_period": "2025"})
    assert r["status"] == "complete"
    assert [o["period"] for o in read_subset(r, "observations")] == ["202606"]
    assert read_subset(r, "series")[0]["units"] == "GWh"


def test_ember_monthly_by_iso3_and_source(tmp_path):
    csv = ("Area,ISO 3 code,Date,Area type,Electricity source,Is aggregated source,Generation (TWh),"
           "Share of generation (%),Emissions (MtCO2e),Emissions intensity (gCO2e/kWh)\n"
           "Germany,DEU,2026-01-01,Country,Solar,False,2.5,6.1,0,\n"
           "Germany,DEU,2026-01-01,Country,Coal,False,8.0,20.0,7.9,\n"
           "France,FRA,2026-01-01,Country,Solar,False,1.0,2.0,0,\n")
    web = FakeWeb({"https://files.ember-energy.org/public-downloads/generation/outputs/release_generation_monthly_global.csv":
                   (csv.encode(), LM)})
    r = runner(get_bulk_adapter("ember-generation"), tmp_path, web).run({"iso3": ["deu"], "sources": ["Solar"]})
    rows = read_subset(r, "generation")
    assert len(rows) == 1 and rows[0]["generation_twh"] == 2.5 and rows[0]["is_aggregate"] is False
    assert r["release_id"].startswith("ember-monthly-20261006")


def test_fas_psd_filters(tmp_path):
    csv = ("Commodity_Code,Commodity_Description,Country_Code,Country_Name,Market_Year,Calendar_Year,Month,"
           "Attribute_ID,Attribute_Description,Unit_ID,Unit_Description,Value\n"
           "0410000,Wheat,US,United States,2024,2025,9,028,Production,8,(1000 MT),53650.0\n"
           "0410000,Wheat,US,United States,2018,2019,9,028,Production,8,(1000 MT),51306.0\n"
           "0410000,Wheat,BR,Brazil,2024,2025,9,028,Production,8,(1000 MT),8000.0\n")
    web = FakeWeb({"https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_csv.zip": (zipped("psd_alldata.csv", csv), LM)})
    r = runner(get_bulk_adapter("fas-psd"), tmp_path, web).run(
        {"commodity_codes": ["0410000"], "countries": ["us"], "since_market_year": 2020})
    rows = read_subset(r, "balances")
    assert [(x["country_code"], x["market_year"], x["value"]) for x in rows] == [("US", 2024, 53650.0)]


def test_opensanctions_checksum_filter_and_no_contacts(tmp_path):
    csv = ("id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,phones,emails,dataset,"
           "first_seen,last_seen,last_change\n"
           "NK-1,Company,Rosneft Oil,Rosneft;ROSNEFT PJSC,,ru,,inn:1,EU sanction,+7,a@b.ru,eu_fsf;us_ofac_sdn,2020,2026,2026\n"
           "NK-2,Person,Jane Doe,,1970,us,,,,,,us_ofac_sdn,2021,2026,2026\n").encode()
    url = "https://data.opensanctions.org/artifacts/default/v1/targets.simple.csv"
    index = {"version": "v1", "updated_at": "2026-10-08T12:53:05",
             "resources": [{"name": "targets.simple.csv", "url": url, "size": len(csv),
                            "checksum": hashlib.sha1(csv).hexdigest()}]}
    web = FakeWeb({"https://data.opensanctions.org/datasets/latest/default/index.json": (json.dumps(index).encode(), {}),
                   url: (csv, {})})
    r = runner(get_bulk_adapter("opensanctions-targets"), tmp_path, web).run({"names": ["rosneft"], "datasets": ["eu_fsf"]})
    rows = read_subset(r, "targets")
    assert [x["id"] for x in rows] == ["NK-1"] and rows[0]["licence"] == "CC-BY-NC-4.0"
    assert "phones" not in rows[0] and "emails" not in rows[0]
    bad = {**index, "resources": [{**index["resources"][0], "checksum": "0" * 40}]}
    web.files["https://data.opensanctions.org/datasets/latest/default/index.json"] = (json.dumps(bad).encode(), {})
    r2 = runner(get_bulk_adapter("opensanctions-targets"), tmp_path / "b", web).run({"names": ["rosneft"]})
    assert r2["files"][0]["action"] == "quarantined"


def test_sam_filters_and_drops_contacts(tmp_path):
    from src.ingestion.bulk.adapters.sam import URL
    csv = ('"NoticeId","Title","Department/Ind.Agency","PostedDate","Active","NaicsCode","PrimaryContactEmail","Award$","Link"\n'
           '"n1","AI research","DEPT OF DEFENSE","2026-10-01 10:00:00","Yes","541715","x@y.mil","","https://sam.gov/opp/n1"\n'
           '"n2","Catering","DEPT OF STATE","2026-10-01","Yes","722310","","",""\n'
           '"n3","Old AI","DEPT OF DEFENSE","2026-01-01","No","541715","","",""\n').encode("cp1252")
    web = FakeWeb({URL: (csv, {})})
    r = runner(get_bulk_adapter("sam-opportunities-extract"), tmp_path, web).run({"naics_prefixes": ["5417"]})
    rows = read_subset(r, "opportunities")
    assert [x["notice_id"] for x in rows] == ["n1"] and "PrimaryContactEmail" not in json.dumps(rows)


def test_iati_publisher_files_and_activity_parse(tmp_path):
    xml = b"""<?xml version="1.0"?><iati-activities version="2.03">
      <iati-activity default-currency="USD" last-updated-datetime="2026-07-03T00:00:00">
        <iati-identifier>47045-NGA-1</iati-identifier>
        <reporting-org ref="47045"><narrative xml:lang="en">Global Fund</narrative></reporting-org>
        <title><narrative xml:lang="fr">Titre</narrative><narrative xml:lang="en">Title</narrative></title>
        <activity-status code="2"/><activity-date type="2" iso-date="2024-01-01"/>
        <recipient-country code="NG" percentage="100"/><sector vocabulary="1" code="12263"/>
        <budget><value>100</value></budget><budget><value>50</value></budget>
        <transaction><transaction-type code="3"/><value>40</value></transaction>
        <transaction><transaction-type code="3"/><value>2</value></transaction>
      </iati-activity>
      <iati-activity><iati-identifier>47045-KEN-1</iati-identifier><recipient-country code="KE"/></iati-activity>
    </iati-activities>"""
    index = {"index_created": "2026-10-08 16:52:06+00:00", "datasets": [
        {"short_name": "gf-nga", "reporting_org_short_name": "theglobalfund", "licence_id": "cc-by",
         "last_known_good_dataset": {"cached_dataset_url_xml": "https://bulk-data.iatistandard.org/gf/gf-nga.xml",
                                     "hash": hashlib.sha1(xml).hexdigest()}},
        {"short_name": "other", "reporting_org_short_name": "someoneelse",
         "last_known_good_dataset": {"cached_dataset_url_xml": "https://bulk-data.iatistandard.org/o/o.xml"}}]}
    web = FakeWeb({"https://bulk-data.iatistandard.org/datasets-minimal": (json.dumps(index).encode(), {}),
                   "https://bulk-data.iatistandard.org/gf/gf-nga.xml": (xml, {})})
    r = runner(get_bulk_adapter("iati-activities"), tmp_path, web).run(
        {"publishers": ["theglobalfund"], "recipient_countries": ["ng"]})
    rows = read_subset(r, "activities")
    assert len(rows) == 1
    a = rows[0]
    assert a["title"] == "Title" and a["reporting_org"] == "Global Fund" and a["budget_total"] == 150
    assert a["transaction_totals"] == {"disbursement": 42.0} and a["dates"] == {"actual_start": "2024-01-01"}
    assert a["licence"] == "cc-by" and len(r["files"]) == 1


def test_courtlistener_picks_latest_nonempty_and_refuses_empty_tables(tmp_path):
    from src.ingestion.bulk.adapters.courtlistener import BUCKET
    with pytest.raises(ValueError, match="empty since 2024-03-01"):
        get_bulk_adapter("courtlistener-bulk").validate_params({"tables": ["dockets"]})
    csv = b"id,short_name,jurisdiction\nscotus,SCOTUS,F\nca1,1st Cir.,F\n"
    listing = ("<ListBucketResult>"
               "<Contents><Key>bulk-data/courts-2024-12-31.csv.bz2</Key><LastModified>2024-12-31T00:00:00Z</LastModified>"
               "<ETag>&quot;a&quot;</ETag><Size>900</Size></Contents>"
               "<Contents><Key>bulk-data/courts-2025-01-31.csv.bz2</Key><LastModified>2025-01-31T00:00:00Z</LastModified>"
               "<ETag>&quot;b&quot;</ETag><Size>900</Size></Contents></ListBucketResult>").encode()
    web = FakeWeb({BUCKET + "?list-type=2&prefix=bulk-data%2F&max-keys=1000": (listing, {}),
                   BUCKET + "bulk-data/courts-2025-01-31.csv.bz2": (bz2.compress(csv), {})})
    r = runner(get_bulk_adapter("courtlistener-bulk"), tmp_path, web).run({"tables": ["courts"], "where": {"id": ["scotus"]}})
    assert r["release_id"] == "courtlistener-2025-01-31"
    assert [x["record"]["short_name"] for x in read_subset(r, "records")] == ["SCOTUS"]


def test_redirects_are_checked_against_the_allowlist():
    http = BulkHttp(("sam.gov",), resolver=lambda h: ["93.184.216.34"])
    handlers = [h for h in http._open.__closure__[0].cell_contents.handlers
                if isinstance(h, urllib.request.HTTPRedirectHandler)]
    req = urllib.request.Request("https://sam.gov/x")
    with pytest.raises(PermanentFetchError):
        handlers[0].redirect_request(req, None, 303, "See Other", {}, "https://evil.example.com/y")

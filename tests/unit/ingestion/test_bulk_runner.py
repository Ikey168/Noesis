"""Bulk-release runner (FA01) with the BLS (FA03) and Companies House (FA07) adapters, offline."""
import gzip
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from src.ingestion import quota
from src.ingestion.bulk import get_bulk_adapter
from src.ingestion.bulk.base import BulkAdapter, Release, ReleaseFile, TableSpec
from src.ingestion.bulk.http import BulkHttp
from src.ingestion.bulk.runner import Budgets, BulkRunner
from src.ingestion.bulk.sinks import DatasetSink

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


class Resp(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class FakeWeb:
    """url -> (body, headers); supports HEAD and Range."""

    def __init__(self, files):
        self.files = files
        self.requests = []

    def __call__(self, request, timeout):
        url, method = request.full_url, request.get_method()
        self.requests.append((method, url, dict(request.header_items())))
        body, headers = self.files[url]
        if method == "HEAD":
            return Resp(b"", 200, {**headers, "Content-Length": str(len(body))})
        rng = dict(request.header_items()).get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return Resp(body[start:], 206, headers)
        return Resp(body, 200, headers)


def runner(adapter, tmp_path, web, **kw):
    http = BulkHttp(adapter.allowed_hosts, resolver=PUBLIC, opener=web)
    return BulkRunner(adapter, state_dir=tmp_path / "state", work_dir=tmp_path / "tmp", http=http, **kw)


# ----------------------------------------------------------------------------- BLS
BLS = "https://download.bls.gov/pub/time.series/cu/"
LISTING = b'<a href="/pub/time.series/cu/cu.series">cu.series</a><a href="x">cu.data.0.Current</a>'
SERIES = (b"series_id        \tarea_code\tseries_title\tbegin_year\r\n"
          b"CUSR0000SA0      \t0000\tAll items\t1947\r\n"
          b"CUUR0000SA0      \t0000\tAll items NSA\t1913\r\n")
DATA = (b"series_id        \tyear\tperiod\t       value\tfootnote_codes\r\n"
        b"CUSR0000SA0      \t2025\tM12\t      320.10\t\r\n"
        b"CUSR0000SA0      \t2026\tM01\t      321.40\t\r\n"
        b"CUUR0000SA0      \t2026\tM01\t      319.00\t\r\n")


def bls_web(etag="A"):
    hdr = {"ETag": etag, "Last-Modified": "Fri, 11 Sep 2026 12:30:00 GMT"}
    return FakeWeb({BLS: (LISTING, {}), BLS + "cu.series": (SERIES, hdr), BLS + "cu.data.0.Current": (DATA, hdr)})


def read_subset(receipt, table):
    path = Path(receipt["release_dir"]) / "subset" / f"{table}.jsonl.gz"
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh]


def test_bls_filters_series_and_writes_subset(tmp_path):
    web = bls_web()
    receipt = runner(get_bulk_adapter("bls-flat-files"), tmp_path, web).run(
        {"survey": "cu", "series": ["CUSR0000SA0"], "since_year": 2026})
    assert receipt["status"] == "complete"
    assert receipt["release_id"] == "cu-20260911T123000Z"
    assert receipt["bytes"] == len(SERIES) + len(DATA)          # streamed bytes are counted
    obs = read_subset(receipt, "observations")
    assert obs == [{"_file": "cu.data.0.Current", "survey": "cu", "series_id": "CUSR0000SA0", "year": 2026,
                    "period": "M01", "value": 321.4, "footnote_codes": None}]
    series = read_subset(receipt, "series")
    assert series[0]["series_title"] == "All items" and series[0]["attributes"]["begin_year"] == "1947"
    assert all("mailto:" in r[2].get("User-agent", "") for r in web.requests)


def test_unchanged_files_skipped_on_next_release_and_resume_markers(tmp_path):
    adapter = get_bulk_adapter("bls-flat-files")
    params = {"survey": "cu", "series": ["CUSR0000SA0"]}
    first = runner(adapter, tmp_path, bls_web()).run(params)
    again = runner(adapter, tmp_path, bls_web()).run(params)           # same release: resume markers
    assert {f["action"] for f in again["files"]} == {"already-done"}
    newer = bls_web()
    newer.files[BLS + "cu.data.0.Current"] = (DATA, {"ETag": "B", "Last-Modified": "Fri, 09 Oct 2026 12:30:00 GMT"})
    third = runner(adapter, tmp_path, newer).run(params)               # new release: only the changed file
    actions = {f["name"]: f["action"] for f in third["files"]}
    assert actions == {"cu.series": "unchanged", "cu.data.0.Current": "processed"}
    assert third["release_id"] != first["release_id"]


def test_bls_rejects_unfiltered_runs():
    with pytest.raises(ValueError):
        get_bulk_adapter("bls-flat-files").validate_params({"survey": "cu"})


# ----------------------------------------------------------------- Companies House
CH_PAGE = "https://download.companieshouse.gov.uk/en_output.html"
CH = "https://download.companieshouse.gov.uk/"
HEADER = ("CompanyName, CompanyNumber,RegAddress.PostTown,RegAddress.PostCode,CompanyCategory,CompanyStatus,"
          "CountryOfOrigin,DissolutionDate,IncorporationDate,SICCode.SicText_1,SICCode.SicText_2,URI,"
          "PreviousName_1.CONDATE, PreviousName_1.CompanyName\n")


def ch_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("part.csv", HEADER + "".join(rows))
    return buf.getvalue()


def ch_web():
    part1 = ch_zip(['ACME LTD,00000001,LONDON,EC1A 1BB,Private Limited Company,Active,United Kingdom,,01/02/2020,'
                    '62020 - IT consultancy,,http://x/1,16/10/2024,OLD ACME LTD\n',
                    'BAKERY LTD,00000002,LEEDS,LS1 1AA,Private Limited Company,Active,United Kingdom,,05/06/2015,'
                    '10710 - Bread,,http://x/2,,\n'])
    part2 = ch_zip(['CODE LTD,00000003,LEEDS,LS2 2BB,Private Limited Company,Active,United Kingdom,,01/01/2026,'
                    '62012 - Software,,http://x/3,,\n'])
    page = (b'<a href="BasicCompanyData-2026-10-01-part1_2.zip">p1</a><a href="BasicCompanyData-2026-10-01-part2_2.zip">p2</a>'
            b'<a href="BasicCompanyData-2026-09-01-part1_2.zip">old</a>')
    return FakeWeb({CH_PAGE: (page, {}),
                    CH + "BasicCompanyData-2026-10-01-part1_2.zip": (part1, {"ETag": "e1"}),
                    CH + "BasicCompanyData-2026-10-01-part2_2.zip": (part2, {"ETag": "e2"})})


def test_companies_house_latest_release_filtered_by_sic(tmp_path):
    receipt = runner(get_bulk_adapter("companies-house-snapshot"), tmp_path, ch_web()).run({"sic_prefix": "620"})
    assert receipt["release_id"] == "2026-10-01" and receipt["status"] == "complete"
    rows = read_subset(receipt, "companies")
    assert [r["company_number"] for r in rows] == ["00000001", "00000003"]
    assert rows[0]["incorporation_date"] == "2020-02-01"
    assert rows[0]["previous_names"] == [{"name": "OLD ACME LTD", "changed": "2024-10-16"}]
    assert not list((tmp_path / "tmp").rglob("*.zip"))          # raw files deleted


def test_budget_stops_cleanly_and_resumes(tmp_path):
    adapter = get_bulk_adapter("companies-house-snapshot")
    first = runner(adapter, tmp_path, ch_web(), budgets=Budgets(max_files=1)).run({"status": "Active"})
    assert first["status"] == "budget_exhausted" and first["files_processed"] == 1
    second = runner(adapter, tmp_path, ch_web()).run({"status": "Active"})
    assert {f["action"] for f in second["files"]} == {"already-done", "processed"}


# ------------------------------------------------------------------ runner details
class OneFile(BulkAdapter):
    name, publisher, allowed_hosts, output = "one-file", "Example", ("data.example.org",), "rows"
    tables = {"t": TableSpec("t", [{"name": "x", "type": "integer"}])}

    def __init__(self, checksum=None):
        self.checksum = checksum

    def list_release(self, http, params):
        return Release("example", "r1", [ReleaseFile("f.txt", "https://data.example.org/f.txt", checksum=self.checksum)],
                       {"id": "cc0"})

    def process(self, source, params):
        for line in source.path.read_text().split():
            yield "t", {"x": int(line)}


def test_resume_partial_download_with_range(tmp_path):
    body = b"1\n2\n3\n"
    web = FakeWeb({"https://data.example.org/f.txt": (body, {"Accept-Ranges": "bytes"})})
    part = tmp_path / "tmp" / "one-file" / "r1" / "f.txt.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:2])
    receipt = runner(OneFile(), tmp_path, web).run({})
    assert receipt["status"] == "complete"
    assert ("Range", "bytes=2-") in [(k, v) for k, v in web.requests[-1][2].items()]
    assert [r["x"] for r in read_subset(receipt, "t")] == [1, 2, 3]


def test_checksum_mismatch_is_quarantined(tmp_path):
    web = FakeWeb({"https://data.example.org/f.txt": (b"1\n", {})})
    receipt = runner(OneFile(("sha256", "0" * 64)), tmp_path, web).run({})
    assert receipt["files"][0]["action"] == "quarantined"
    assert (Path(receipt["release_dir"]) / "quarantine" / "f.txt").exists()
    ok = runner(OneFile(("sha256", hashlib.sha256(b"1\n").hexdigest())), tmp_path / "b", web).run({})
    assert ok["status"] == "complete"


def test_disallowed_host_and_quota_deferral(tmp_path, monkeypatch):
    class Elsewhere(OneFile):
        def list_release(self, http, params):
            return Release("example", "r1", [ReleaseFile("f", "https://evil.example.com/f")], {})
    receipt = runner(Elsewhere(), tmp_path, FakeWeb({})).run({})
    assert receipt["status"] == "partial" and "not allowlisted" in receipt["errors"][0]["error"]

    lg = quota.QuotaLedger(tmp_path / "q.sqlite", {"hosts": {"data.example.org": {"limits": [{"window": "day", "requests": 0}]}}})
    monkeypatch.setattr(quota, "_DEFAULT", lg)
    monkeypatch.delenv("NOESIS_QUOTA_DISABLED", raising=False)
    web = FakeWeb({"https://data.example.org/f.txt": (b"1\n", {})})
    deferred = runner(OneFile(), tmp_path / "c", web).run({})
    assert deferred["status"] == "deferred" and deferred["retry_at"]


def test_dataset_sink_registers_release_and_ingests(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "w.duckdb"
    sink = DatasetSink("bulk-test", lambda: duckdb.connect(str(db)))
    receipt = runner(get_bulk_adapter("bls-flat-files"), tmp_path, bls_web(), sinks=[sink]).run(
        {"survey": "cu", "series": ["CUSR0000SA0", "CUUR0000SA0"]})
    assert receipt["status"] == "complete"
    assert receipt["outputs"]["dataset_release_id"].startswith("dataset-release:")
    assert receipt["outputs"]["ingestion_receipts"] == 2          # one chunk per table
    from src.kb.dataset_intelligence import DatasetIntelligenceStore
    conn = duckdb.connect(str(db))
    rel = DatasetIntelligenceStore(conn, initialize=False).release(
        "bulk-test", receipt["outputs"]["dataset_release_id"], scopes={"knowledge:dataset:read"})
    assert rel["native_release_id"] == "cu-20260911T123000Z"
    # a second release of the same dataset registers without a schema conflict
    newer = bls_web()
    newer.files[BLS + "cu.data.0.Current"] = (DATA, {"ETag": "B", "Last-Modified": "Fri, 09 Oct 2026 12:30:00 GMT"})
    conn.close()
    again = runner(get_bulk_adapter("bls-flat-files"), tmp_path, newer,
                   sinks=[DatasetSink("bulk-test", lambda: duckdb.connect(str(db)))]).run(
        {"survey": "cu", "series": ["CUSR0000SA0"]})
    assert again["status"] == "complete" and again["outputs"]["dataset_release_id"] != receipt["outputs"]["dataset_release_id"]


def test_stream_byte_budget_enforced(tmp_path):
    # size known from HEAD: the runner stops before downloading
    receipt = runner(get_bulk_adapter("bls-flat-files"), tmp_path, bls_web(), budgets=Budgets(max_bytes=50)).run(
        {"survey": "cu", "series": ["CUSR0000SA0"]})
    assert receipt["status"] == "budget_exhausted" and receipt["files_processed"] == 0
    # size unknown: the stream itself is capped
    from src.ingestion.bulk.runner import _CountingRaw
    from src.ingestion.connectors.base import PermanentFetchError
    reader = io.BufferedReader(_CountingRaw(io.BytesIO(b"x" * 100), cap=10), buffer_size=4)
    with pytest.raises(PermanentFetchError):
        reader.read()


def test_job_file_maps_to_run_options(tmp_path, monkeypatch):
    import src.ingestion.bulk.__main__ as cli
    seen = {}

    class FakeRunner:
        def __init__(self, adapter, **kw):
            seen.update(adapter=adapter.name, **kw)

        def run(self, params, dry_run=False, full=False):
            seen.update(params=params, dry_run=dry_run, full=full)
            return {"status": "dry-run"}

    monkeypatch.setattr(cli, "BulkRunner", FakeRunner)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"adapter": "bls-flat-files", "params": {"survey": "cu", "series": ["X"]},
                               "max_files": 3, "dry_run": True, "state_dir": str(tmp_path / "s"),
                               "work_dir": str(tmp_path / "w")}))
    assert cli.main(["job", str(job)]) == 0
    assert seen["adapter"] == "bls-flat-files" and seen["params"] == {"survey": "cu", "series": ["X"]}
    assert seen["budgets"].max_files == 3 and seen["dry_run"] is True

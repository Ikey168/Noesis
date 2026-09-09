import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion.provider_execution import DurableHTTP, ProviderError
from src.ingestion.regional_providers import PROVIDER_HOSTS, RegionalClient
from src.ingestion.regional_workflow import RegionalAcquisition
from tests.unit.ingestion.test_regional_providers import COURT, native_client


@pytest.mark.parametrize(
    "status", ["partial", "unavailable", "failed", "timeout", "cancelled"]
)
def test_linked_pdf_incomplete_parse_never_publishes_and_can_retry(monkeypatch, status):
    from src.evaluation import runtime_jobs

    raw = b"%PDF-1.7 captured fixture"
    client, calls, conn = native_client("bfarm", raw)
    workflow = RegionalAcquisition(
        conn,
        namespace="medicine",
        principal_id="a",
        reuse_notice="fixture",
        client=client,
    )
    paths = []

    def worker(operation, payload, **limits):
        path = Path(payload["path"])
        paths.append(path)
        assert operation == "pdf-pymupdf"
        assert path.read_bytes() == raw
        assert payload["sha256"] == hashlib.sha256(raw).hexdigest()
        assert limits == {"timeout_s": 120, "max_rss_bytes": 1024**3}
        return {"status": status}

    monkeypatch.setattr(runtime_jobs, "execute_job", worker)
    parameters = {
        "source_url": "https://www.bfarm.de/letter.pdf",
        "language": "de",
        "parent_id": "letter-1",
    }
    with pytest.raises(ProviderError, match="did not complete"):
        workflow.acquire("acquire_document", parameters, "letter", scopes={"operator"})
    assert all(not path.exists() for path in paths)
    assert (
        conn.execute("SELECT count(*) FROM regional_observation_receipts").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM regional_workflow_runs").fetchone()[0] == 0
    )

    monkeypatch.setattr(
        runtime_jobs,
        "execute_job",
        lambda *a, **kw: {
            "status": "completed",
            "result": {
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "version": "test-double",
                "locators": [
                    {"page": 1, "bbox": [1, 2, 30, 40], "text": "Deutsche Mitteilung."}
                ],
            },
        },
    )
    result = workflow.acquire(
        "acquire_document", parameters, "letter", scopes={"operator"}
    )
    assert result["status"] == "ingested"
    assert len(calls) == 1  # Retry parses the durable capture without fetching again.
    assert workflow.acquire(
        "acquire_document", parameters, "letter", scopes={"operator"}
    )["replayed"]
    conn.close()


def test_linked_pdf_native_parser_preserves_captured_identity_and_locators():
    pytest.importorskip("pymupdf")
    raw = (
        Path(__file__).parents[2] / "fixtures/pdf_benchmark/digital.pdf"
    ).read_bytes()
    client, _, conn = native_client("ema", raw)
    records, captured = client.acquire_document(
        "https://www.ema.europa.eu/medicine.pdf",
        "pdf",
        language="de",
        parent_id="EMEA/H/C/fixture",
    )
    record = records[0]
    assert record["native"]["original_sha256"] == captured.receipt["digest"]
    assert record["native"]["parser_receipt"]["version"]
    assert record["sections"] and record["sections"][0]["locator"]["page"] == 1
    assert len(record["sections"][0]["locator"]["bbox"]) == 4
    repeated, second_capture = client.acquire_document(
        "https://www.ema.europa.eu/medicine.pdf",
        "pdf-again",
        language="de",
        parent_id="EMEA/H/C/fixture",
    )
    assert repeated == records  # Runtime measurements must not create source revisions.
    assert captured.receipt["parser_runtime"]["status"] == "completed"
    assert second_capture.receipt["parser_runtime"]["status"] == "completed"
    conn.close()


@pytest.mark.parametrize("imported", [False, True])
def test_publication_failure_rolls_back_documents_and_replay_receipts(
    monkeypatch, imported
):
    client, calls, conn = native_client("german-courts", COURT)
    workflow = RegionalAcquisition(
        conn, namespace="law", principal_id="a", reuse_notice="fixture", client=client
    )
    finish = workflow._finish

    def interrupted(*args, **kwargs):
        finish(*args, **kwargs)
        raise RuntimeError("interrupted publication")

    def acquire():
        if imported:
            raw = json.dumps(
                [{"id": "DRKS00012345", "title": "Deutsche Studie"}]
            ).encode()
            return workflow.import_export(
                "drks",
                raw,
                {
                    "source_url": "https://drks.de/search/de/trial/DRKS00012345",
                    "format": "json",
                    "field_map": {"id": "/id", "title": "/title"},
                    "export_schema_version": "test-v1",
                },
                "obs",
                scopes={"operator"},
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
        return workflow.acquire(
            "court_decision", {"identity": "JUREfixture"}, "obs", scopes={"operator"}
        )

    monkeypatch.setattr(workflow, "_finish", interrupted)
    with pytest.raises(RuntimeError, match="interrupted publication"):
        acquire()
    for table in (
        "documents",
        "document_revision_records",
        "regional_observation_receipts",
        "regional_workflow_runs",
    ):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(workflow, "_finish", finish)
    assert acquire()["status"] == "ingested"
    assert acquire()["replayed"]
    assert len(calls) == (0 if imported else 1)
    conn.close()


def test_native_acquisition_and_offline_operator_replay(tmp_path):
    conn = duckdb.connect(str(tmp_path / "store.duckdb"))
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"content": COURT}

    client = RegionalClient(
        DurableHTTP(
            conn,
            budget_id="regional",
            provider="german-courts",
            principal_id="alice",
            allowed_hosts=PROVIDER_HOSTS["german-courts"],
            reuse_notice="fixture",
            transport=transport,
        ),
        principal_id="alice",
    )
    workflow = RegionalAcquisition(
        conn,
        namespace="law",
        principal_id="alice",
        reuse_notice="fixture",
        client=client,
    )
    result = workflow.acquire(
        "court_decision", {"identity": "JUREfixture"}, "obs", scopes={"operator"}
    )
    assert (
        result["status"] == "ingested" and len(result["receipt"]["document_ids"]) == 1
    )
    assert workflow.acquire(
        "court_decision", {"identity": "JUREfixture"}, "obs", scopes={"operator"}
    )["replayed"]
    assert len(calls) == 1
    with pytest.raises(ProviderError, match="bound"):
        workflow.acquire(
            "court_decision", {"identity": "OTHER"}, "obs", scopes={"operator"}
        )
    with pytest.raises(ProviderError, match="authorization"):
        workflow.acquire(
            "court_decision", {"identity": "JUREfixture"}, "obs", scopes=set()
        )
    conn.close()


def test_ctis_import_hash_changes_revisions_but_not_trial_identity():
    conn = duckdb.connect()
    service = RegionalAcquisition(
        conn, namespace="science", principal_id="a", reuse_notice="fixture"
    )
    row = {"id": "2024-123456-12-00", "name": "Deutsche Studie", "status": "recruiting"}
    options = {
        "source_url": "https://euclinicaltrials.eu/ctis-public/view/2024-123456-12-00",
        "format": "json",
        "field_map": {"id": "/id", "title": "/name", "registry_status": "/status"},
        "export_schema_version": "test-v1",
    }
    raw = json.dumps([row]).encode()
    result = service.import_export(
        "ctis",
        raw,
        options,
        "v1",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    changed = json.dumps([{**row, "status": "withdrawn"}]).encode()
    newer = service.import_export(
        "ctis",
        changed,
        options,
        "v2",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(changed).hexdigest(),
    )
    assert result["receipt"]["document_ids"] == newer["receipt"]["document_ids"]
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == 2
    )
    with pytest.raises(ProviderError, match="differs"):
        service.import_export(
            "ctis",
            changed,
            options,
            "v3",
            scopes={"operator"},
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
    conn.close()


@pytest.mark.parametrize(
    "status", ["partial", "failed", "timeout", "cancelled", "unavailable"]
)
def test_berlin_pdf_import_incomplete_worker_never_publishes(monkeypatch, status):
    from src.evaluation import runtime_jobs

    conn = duckdb.connect(":memory:")
    workflow = RegionalAcquisition(
        conn, namespace="law", principal_id="alice", reuse_notice="fixture"
    )
    raw = b"%PDF-1.7 bounded publication fixture"
    paths = []

    def worker(operation, payload, **limits):
        path = Path(payload["path"])
        paths.append(path)
        assert path.read_bytes() == raw
        assert payload["max_pages"] == 100
        assert operation == "pdf-pymupdf"
        assert limits == {"timeout_s": 120, "max_rss_bytes": 1024**3}
        return {"status": status}

    monkeypatch.setattr(runtime_jobs, "execute_job", worker)
    with pytest.raises(ProviderError, match="did not complete"):
        workflow.import_export(
            "berlin-law",
            raw,
            {
                "source_url": "https://www.berlin.de/law.pdf",
                "official_id": "law-1",
                "kind": "law",
                "title": "Gesetz",
            },
            "obs",
            scopes={"operator"},
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
    assert all(not path.exists() for path in paths)
    for table in (
        "documents",
        "regional_observation_receipts",
        "regional_workflow_runs",
    ):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "provider,operation,raw,parameters",
    [
        (
            "bfarm",
            "bfarm_notices",
            b"<rss><channel><title>BfArM</title></channel></rss>",
            {},
        ),
        (
            "bfarm",
            "linked_documents",
            b"<main><p>Keine Dokumente.</p></main>",
            {"source_url": "https://www.bfarm.de/empty.html"},
        ),
        ("ema", "ema_medicines", b'{"data": []}', {}),
    ],
)
def test_empty_provider_results_are_explicit_and_replayable(
    provider, operation, raw, parameters
):
    client, calls, conn = native_client(provider, raw)
    workflow = RegionalAcquisition(
        conn,
        namespace="medicine",
        principal_id="a",
        reuse_notice="fixture",
        client=client,
    )
    result = workflow.acquire(operation, parameters, "empty", scopes={"operator"})
    assert result["status"] == "empty"
    assert result["receipt"]["document_ids"] == []
    assert workflow.acquire(operation, parameters, "empty", scopes={"operator"})[
        "replayed"
    ]
    assert len(calls) == 1
    conn.close()


def test_bfarm_corrected_pdf_marks_cited_evidence_affected_and_retains_old_passage():
    import pymupdf

    from src.ingestion.document_store import DocumentStore
    from src.kb.evidence_changes import EvidenceResolver

    def pdf(text):
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 50), text)
            return doc.tobytes()

    captures = [
        pdf("Erste Sicherheitsmitteilung."),
        pdf("Korrigierte Sicherheitsmitteilung."),
    ]
    conn = duckdb.connect(":memory:")
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        if len(calls) > 2:
            raise ConnectionError("download interrupted")
        return {"content": captures[len(calls) - 1]}

    client = RegionalClient(
        DurableHTTP(
            conn,
            budget_id="bfarm-update",
            provider="bfarm",
            principal_id="a",
            allowed_hosts=PROVIDER_HOSTS["bfarm"],
            reuse_notice="fixture",
            transport=transport,
        ),
        principal_id="a",
    )
    workflow = RegionalAcquisition(
        conn,
        namespace="medicine",
        principal_id="a",
        reuse_notice="fixture",
        client=client,
    )
    params = {
        "source_url": "https://www.bfarm.de/letter.pdf",
        "language": "de",
        "parent_id": "letter-1",
    }
    first = workflow.acquire("acquire_document", params, "first", scopes={"operator"})
    ref = first["receipt"]["source_refs"][0]
    dep = {
        "kind": "source",
        "namespace": "medicine",
        "id": ref["document_id"],
        "revision": ref["revision_id"],
        "locator": {"page": 1},
    }
    resolver = EvidenceResolver(conn, {"operator"})
    assert resolver.compare(dep)["status"] == "current"
    second = workflow.acquire("acquire_document", params, "second", scopes={"operator"})
    assert first["receipt"]["document_ids"] == second["receipt"]["document_ids"]
    assert resolver.compare(dep)["status"] == "affected"
    with pytest.raises(ProviderError):
        workflow.acquire("acquire_document", params, "failed", scopes={"operator"})
    assert workflow.acquire("acquire_document", params, "first", scopes={"operator"})[
        "replayed"
    ]
    current = DocumentStore(conn).get(ref["document_id"])
    assert "Korrigierte Sicherheitsmitteilung" in current["content"]
    old = conn.execute(
        "SELECT b.content FROM document_revision_content r JOIN document_content_blobs b ON b.blob_hash=r.blob_hash WHERE r.revision_id=?",
        [ref["revision_id"]],
    ).fetchone()
    assert "Erste Sicherheitsmitteilung" in old[0]
    assert len(calls) == 3
    conn.close()


@pytest.mark.parametrize("provider", ["bfarm", "ema"])
def test_selected_pdf_url_returning_html_is_not_document_evidence(provider):
    raw = b"<main><p>This document is unavailable. Browse our other content.</p></main>"
    client, calls, conn = native_client(provider, raw)
    workflow = RegionalAcquisition(
        conn,
        namespace="medicine",
        principal_id="a",
        reuse_notice="fixture",
        client=client,
    )
    origin = "www.bfarm.de" if provider == "bfarm" else "www.ema.europa.eu"
    with pytest.raises(ProviderError, match="non-PDF bytes"):
        workflow.acquire(
            "acquire_document",
            {
                "source_url": f"https://{origin}/letter.pdf?download=true",
                "language": "de",
                "parent_id": "letter",
            },
            "html-response",
            scopes={"operator"},
        )
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM regional_workflow_runs").fetchone()[0] == 0
    )
    conn.close()


def test_native_ctis_csv_profile_preserves_dates_locations_and_missing_documents():
    import csv
    import io

    stream = io.StringIO()
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "Trial number",
            "Title of the trial",
            "Overall trial status",
            "Sponsor/Co-Sponsors",
            "Last updated",
            "Decision date",
            "Location(s) and recruitment status",
            "Trial results",
        ],
    )
    writer.writeheader()
    writer.writerow(
        {
            "Trial number": "2025-524131-39-00",
            "Title of the trial": "Authored structural fixture",
            "Overall trial status": "Ongoing, recruiting",
            "Sponsor/Co-Sponsors": "Fixture sponsor",
            "Last updated": "09/09/2026",
            "Decision date": "08/09/2026",
            "Location(s) and recruitment status": "Germany:Ongoing, recruiting, Austria:Authorised, recruitment pending",
            "Trial results": "No",
        }
    )
    raw = stream.getvalue().encode("utf-8-sig")
    conn = duckdb.connect(":memory:")
    service = RegionalAcquisition(
        conn, namespace="science", principal_id="a", reuse_notice="fixture"
    )
    result = service.import_export(
        "ctis",
        raw,
        {
            "format": "ctis-search-csv",
            "source_url": "https://euclinicaltrials.eu/search-for-clinical-trials/",
        },
        "csv",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    from src.ingestion.document_store import DocumentStore

    doc = DocumentStore(conn).get(result["receipt"]["document_ids"][0])
    record = json.loads(doc["metadata"]["provider_record_json"])
    assert record["fields"]["countries"] == ["Germany", "Austria"]
    assert (
        record["fields"]["country_recruitment_statuses"][0]["recruitment_status"]
        == "Ongoing, recruiting"
    )
    assert record["fields"]["decision_date"] == "2026-09-08"
    assert record["published_at"] is None
    assert record["fields"]["results_available"] is False
    assert (
        "documents" in record["missing_fields"] and "sites" in record["missing_fields"]
    )
    assert service.replay("csv", provider="ctis", scopes={"operator"})["replayed"]
    conn.close()


def test_trial_document_import_keeps_protocol_and_results_distinct():
    raw = b"<main><h1>Trial protocol</h1><p>Authored public export fixture.</p></main>"
    conn = duckdb.connect(":memory:")
    service = RegionalAcquisition(
        conn, namespace="science", principal_id="a", reuse_notice="fixture"
    )
    options = {
        "format": "trial-document",
        "source_url": "https://euclinicaltrials.eu/search-for-clinical-trials/",
        "trial_id": "2025-524131-39-00",
        "document_id": "protocol-v1",
        "document_kind": "protocol",
        "title": "Protocol",
        "language": "en",
    }
    first = service.import_export(
        "ctis",
        raw,
        options,
        "protocol",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    result = service.import_export(
        "ctis",
        raw,
        {**options, "document_kind": "results"},
        "results",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert first["receipt"]["document_ids"] != result["receipt"]["document_ids"]
    assert service.replay("protocol", provider="ctis", scopes={"operator"})["replayed"]
    with pytest.raises(ValueError, match="identity"):
        service.import_export(
            "ctis",
            raw,
            {**options, "trial_id": "not-a-trial"},
            "invalid",
            scopes={"operator"},
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
    conn.close()


def test_drks_native_json_locales_status_sites_and_missing_results():
    from src.ingestion.regional_providers import parse_drks_public_json

    native = {
        "drksId": "DRKS00037756",
        "trialStatus": "UPDATED",
        "registrationDrks": "2025-09-01",
        "lastUpdate": "2026-04-17",
        "trialDescriptions": [
            {"idLocale": {"locale": "en"}, "title": "Berlin study"},
            {
                "idLocale": {"locale": "de"},
                "title": "Berliner Studie",
                "summary": "Deutsche Beschreibung",
            },
        ],
        "trialContacts": [
            {
                "idContactIdType": {"type": "PRIMARY_SPONSOR"},
                "contact": {"affiliation": "Universität"},
            }
        ],
        "recruitment": {
            "status": "RECRUITING",
            "countries": [{"idCountry": {"code": "DE"}}],
            "institutes": [{"name": "Ambulanz", "city": "Berlin"}],
        },
        "trialResults": {
            "ipdSharingPlan": False,
            "trialResultsDescriptions": [
                {"idLocale": {"locale": "de"}, "briefSummaryOfResultsDescription": None}
            ],
        },
        "secondaryIds": {
            "otherPrimaryRegisterName": "CTIS",
            "otherPrimaryRegisterId": "2025-524131-39-00",
        },
    }
    raw = json.dumps(native).encode()
    options = {
        "format": "drks-public-json",
        "source_url": "https://drks.de/search/de/trial/DRKS00037756/download",
    }
    conn = duckdb.connect()
    service = RegionalAcquisition(
        conn, namespace="science", principal_id="a", reuse_notice="authored fixture"
    )
    first = service.import_export(
        "drks",
        raw,
        options,
        "native",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    record = parse_drks_public_json(raw, source_url=options["source_url"])[0]
    assert record["title"] == "Berliner Studie"
    assert record["fields"]["registry_status"] == "RECRUITING"
    assert record["fields"]["registry_record_status"] == "UPDATED"
    assert record["fields"]["sites"][0]["city"] == "Berlin"
    assert record["fields"]["sponsor"] == "Universität"
    assert record["fields"]["results"] is None
    assert "results" in record["missing_fields"]
    assert record["relationships"][0]["review_required"]
    assert record["sections"][0]["locator"]["pointer"] == "/trialDescriptions/1/title"
    assert (
        service.replay("native", provider="drks", scopes={"operator"})["receipt"]
        == first["receipt"]
    )
    assert (
        parse_drks_public_json(raw, source_url=options["source_url"], language="en")[0][
            "title"
        ]
        == "Berlin study"
    )
    native["trialDescriptions"].append(native["trialDescriptions"][1])
    with pytest.raises(ProviderError, match="locale"):
        parse_drks_public_json(
            json.dumps(native).encode(), source_url=options["source_url"]
        )
    conn.close()


def test_cellar_corrected_text_revision_and_language_sources_remain_distinct():
    from src.ingestion.document_store import DocumentStore

    conn = duckdb.connect()
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {
            "content": b"<main><p>Original text.</p></main>"
            if len(calls) == 1
            else b"<main><p>Corrected text.</p></main>"
        }

    client = RegionalClient(
        DurableHTTP(
            conn,
            budget_id="cellar-correction",
            provider="cellar",
            principal_id="a",
            allowed_hosts=PROVIDER_HOSTS["cellar"],
            reuse_notice="fixture",
            transport=transport,
        ),
        principal_id="a",
    )
    workflow = RegionalAcquisition(
        conn, namespace="law", principal_id="a", reuse_notice="fixture", client=client
    )
    params = {
        "source_url": "https://publications.europa.eu/resource/cellar/work.de/DOC_1",
        "language": "de",
        "parent_id": "work",
        "kind": "legal-text",
    }
    first = workflow.acquire("acquire_document", params, "first", scopes={"operator"})
    second = workflow.acquire(
        "acquire_document", params, "corrected", scopes={"operator"}
    )
    english = workflow.acquire(
        "acquire_document",
        {
            **params,
            "source_url": params["source_url"].replace("work.de", "work.en"),
            "language": "en",
        },
        "english",
        scopes={"operator"},
    )
    assert first["receipt"]["document_ids"] == second["receipt"]["document_ids"]
    assert first["receipt"]["source_refs"] != second["receipt"]["source_refs"]
    assert english["receipt"]["document_ids"] != second["receipt"]["document_ids"]
    assert (
        workflow.replay("first", provider="cellar", scopes={"operator"})["receipt"]
        == first["receipt"]
    )
    assert (
        "Corrected"
        in DocumentStore(conn).get(second["receipt"]["document_ids"][0])["content"]
    )
    assert len(calls) == 3
    conn.close()


def test_berlin_native_exports_preserve_norms_and_exclude_editorial_text():
    from src.ingestion.regional_providers import (
        parse_berlin_juris_html,
        parse_berlin_juris_xml,
    )

    raw = b"""<?xml version="1.0"?><!DOCTYPE dokumente [<!ENTITY % external SYSTEM "https://invalid.example/not-fetched.dtd">%external;]><dokumente doknr="law"><norm doknr="law"><metadaten><titel>Berliner Testgesetz</titel><dokumenttyp>Gesetz</dokumenttyp><ausfertigung-datum>01.01.2026</ausfertigung-datum><gueltigab>02.01.2026</gueltigab><enbez>Artikel 1</enbez></metadaten><textdaten><!-- not evidence --><?ignore instruction?><p>Amtlicher Text.</p></textdaten></norm></dokumente>"""
    url = "https://gesetze.berlin.de/bsbe/document/law"
    first = parse_berlin_juris_xml(raw, source_url=url)[0]
    assert first["fields"]["enactment_date"] == "2026-01-01"
    assert first["published_at"] is None
    assert first["sections"][0]["locator"]["official_norm_id"] == "law"
    assert first["sections"][0]["locator"]["norm_label"] == "Artikel 1"
    conn = duckdb.connect()
    service = RegionalAcquisition(
        conn, namespace="law", principal_id="a", reuse_notice="authored fixture"
    )
    options = {"format": "berlin-juris-xml", "source_url": url, "historical": True}
    old = service.import_export(
        "berlin-law",
        raw,
        options,
        "old",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    changed = raw.replace(b"Amtlicher Text.", b"Geaenderter amtlicher Text.")
    new = service.import_export(
        "berlin-law",
        changed,
        {**options, "historical": False},
        "new",
        scopes={"operator"},
        expected_sha256=hashlib.sha256(changed).hexdigest(),
    )
    assert old["receipt"]["document_ids"] == new["receipt"]["document_ids"]
    assert old["receipt"]["source_refs"] != new["receipt"]["source_refs"]
    assert (
        service.replay("old", provider="berlin-law", scopes={"operator"})["receipt"]
        == old["receipt"]
    )
    malicious = raw.replace(
        b'<!ENTITY % external SYSTEM "https://invalid.example/not-fetched.dtd">%external;',
        b'<!ENTITY stolen SYSTEM "file:///etc/passwd">',
    ).replace(b"Amtlicher Text.", b"&stolen;")
    with pytest.raises(ProviderError, match="entities"):
        parse_berlin_juris_xml(malicious, source_url=url)
    html = b"""<table><tr><th>Gericht:</th><td>Testgericht Berlin</td></tr><tr><th>Entscheidungsdatum:</th><td>03.04.2026</td></tr></table><div class="docLayoutText doktyp-juris-r"><h3>Orientierungssatz</h3><p>Editorial interpretation.</p><h3>Tenor</h3><p>Official disposition.</p><h3>Gr\xc3\xbcnde</h3><dl class="RspDL"><dt>Randnummer 12</dt><dd><p>Official reasons.</p></dd></dl></div>"""
    court = parse_berlin_juris_html(
        html,
        source_url="https://gesetze.berlin.de/bsbe/document/NJRE001",
        official_id="NJRE001",
    )[0]
    assert court["fields"]["decision_date"] == "2026-04-03"
    assert court["published_at"] is None
    assert all("Editorial" not in s["text"] for s in court["sections"])
    assert court["sections"][-1]["locator"]["paragraph_number"] == "12"
    assert court["native"]["editorial_or_unclassified_text"] == [
        "Editorial interpretation."
    ]
    with pytest.raises(ProviderError, match="body"):
        parse_berlin_juris_html(
            b"<p>Document unavailable</p>",
            source_url="https://gesetze.berlin.de/bsbe/document/NJRE001",
            official_id="NJRE001",
        )
    conn.close()


def test_bulk_dataset_removal_preserves_previous_capture_and_candidate_identity():
    snapshots = [
        [
            {
                "id": "fixture-a",
                "schema": "Person",
                "properties": {"name": ["Müller"]},
                "datasets": ["eu_fsf"],
            }
        ],
        [
            {
                "id": "fixture-b",
                "schema": "Person",
                "properties": {"name": ["Unrelated"]},
                "datasets": ["eu_fsf"],
            }
        ],
    ]
    calls = []

    def transport(**kwargs):
        rows = snapshots[len(calls)]
        calls.append(kwargs)
        return {"content": "\n".join(json.dumps(row) for row in rows).encode()}

    conn = duckdb.connect()
    client = RegionalClient(
        DurableHTTP(
            conn,
            budget_id="bulk-update",
            provider="opensanctions",
            principal_id="a",
            allowed_hosts=PROVIDER_HOSTS["opensanctions"],
            reuse_notice="authored fixture",
            transport=transport,
        ),
        principal_id="a",
    )
    service = RegionalAcquisition(
        conn,
        namespace="research",
        principal_id="a",
        reuse_notice="authored fixture",
        client=client,
    )
    params = {"names": ["Müller"], "artifact_version": "20260908225701-fixture"}
    first = service.acquire("sanctions_dataset", params, "first", scopes={"operator"})
    removed = service.acquire(
        "sanctions_dataset",
        {
            **params,
            "artifact_version": "20260909225701-fixture",
            "previous_entity_ids": ["fixture-a"],
        },
        "removed",
        scopes={"operator"},
    )
    assert first["receipt"]["document_ids"] == removed["receipt"]["document_ids"]
    assert first["receipt"]["source_refs"] != removed["receipt"]["source_refs"]
    record = removed["native_result"]["records"][0]
    assert record["fields"]["dataset_presence"] == "absent-from-complete-capture"
    assert record["review_required"] and not record["fields"]["automatic_merge"]
    assert (
        service.replay("first", provider="opensanctions", scopes={"operator"})[
            "receipt"
        ]
        == first["receipt"]
    )
    assert len(calls) == 2
    conn.close()

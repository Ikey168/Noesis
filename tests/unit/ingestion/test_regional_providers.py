"""Authored native-envelope conformance fixtures; not live coverage measurements."""

import io
import json
import zipfile
from pathlib import Path

import duckdb
import pytest
from defusedxml.common import DefusedXmlException

from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import DurableHTTP, ProviderError
from src.ingestion.regional_providers import (
    PROVIDER_HOSTS,
    RegionalClient,
    RegionalEvidenceStore,
    parse_berlin_publication,
    parse_bfarm_feed,
    parse_court_download,
    parse_court_index,
    parse_drks_who_xml,
    parse_ema,
    parse_registry_export,
)

COURT = b"""<?xml version="1.0"?><!DOCTYPE dokument SYSTEM "https://www.rechtsprechung-im-internet.de/dtd/v1/rii-dok.dtd">
<dokument><doknr>JUREfixture</doknr><gertyp>BGH</gertyp><spruchkoerper>9. Zivilsenat</spruchkoerper><ecli/>
<entsch-datum>20260104</entsch-datum><aktenzeichen>IX ZB 1/26</aktenzeichen><doktyp>Beschluss</doktyp>
<titelzeile><p>Fiktive Entscheidung.</p></titelzeile><gruende><div><dl><dt><a name="rd_1">1</a></dt><dd><p>Deutscher Absatz.</p></dd></dl></div></gruende></dokument>"""


def native_client(provider, native, *, key=None):
    conn = duckdb.connect()
    calls = []

    def transport(**kw):
        calls.append(kw)
        return {
            "content": native if isinstance(native, bytes) else json.dumps(native),
            "headers": {"Content-Type": "application/json"},
        }

    http = DurableHTTP(
        conn,
        budget_id="b-" + provider,
        provider=provider,
        principal_id="a",
        allowed_hosts=PROVIDER_HOSTS[provider],
        reuse_notice="Authored fixture for protocol tests",
        max_requests=10,
        max_usd_micros=100,
        transport=transport,
    )
    return RegionalClient(http, principal_id="a", credential=key), calls, conn


def test_official_court_zip_metadata_paragraphs_and_empty_ecli():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("jb-JUREfixture.xml", COURT)
    record = parse_court_download(stream.getvalue())
    assert record["provider_id"] == "JUREfixture" and record["fields"]["ecli"] is None
    assert record["fields"]["decision_date"] == "2026-01-04"
    assert any(
        part["locator"].get("paragraph_number") == "1" for part in record["sections"]
    )
    index = b"<items><item><gericht>BGH</gericht><link>http://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-JUREfixture.zip</link><modified>2026-01-04</modified></item></items>"
    assert parse_court_index(index)["records"][0]["url"].startswith("https://")
    assert parse_court_index(index, court="BVerwG")["records"] == []
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../danger.xml", COURT)
    with pytest.raises(ProviderError):
        parse_court_download(bad.getvalue())
    with pytest.raises(DefusedXmlException):
        parse_court_download(
            b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><dokument>&a;</dokument>'
        )


def test_court_native_acquisition_and_document_store_revision_replay(tmp_path):
    client, calls, conn = native_client("german-courts", COURT)
    records, response = client.court_decision("JUREfixture", "obs1")
    store = RegionalEvidenceStore(conn)
    auth = {
        "namespace": "research",
        "principal_id": "a",
        "scopes": {"operator"},
        "reuse_notice": "test",
    }
    first = store.ingest(records, response, **auth)
    replay_records, replay = client.court_decision("JUREfixture", "obs1")
    second = store.ingest(replay_records, replay, **auth)
    assert first["document_ids"] == second["document_ids"] and len(calls) == 1
    doc = DocumentStore(conn).get(first["document_ids"][0])
    assert "Deutscher Absatz" in doc["content"] and json.loads(
        doc["metadata"]["source_locators_json"]
    )
    with pytest.raises(ProviderError, match="authorization"):
        store.ingest(records, response, **{**auth, "scopes": set()})
    changed_raw = COURT.replace(b"Deutscher Absatz", b"Korrigierter deutscher Absatz")
    changed = store.import_bytes(
        "german-courts",
        changed_raw,
        [parse_court_download(changed_raw)],
        source_url=records[0]["source_url"],
        observed_at_ms=response.receipt["observed_at_ms"] + 1,
        **auth,
    )
    assert changed["document_ids"] == first["document_ids"]
    assert (
        changed["source_refs"][0]["revision_id"]
        != first["source_refs"][0]["revision_id"]
    )
    assert store.ingest(records, response, **auth)["replayed"]
    assert (
        "Korrigierter deutscher Absatz"
        in DocumentStore(conn).get(first["document_ids"][0])["content"]
    )
    conn.close()


@pytest.mark.parametrize(
    "provider,identity", [("drks", "DRKS00027006"), ("ctis", "2024-123456-12-00")]
)
def test_validated_registry_import_not_an_assumed_api(provider, identity):
    native = [
        {
            "registryId": identity,
            "publicTitle": "Deutsche Studie",
            "status": "withdrawn",
            "sponsor": "Institut",
            "sites": [{"city": "Berlin"}],
            "published": "04/01/2026",
        }
    ]
    field_map = {
        "id": "/registryId",
        "title": "/publicTitle",
        "registry_status": "/status",
        "sponsor": "/sponsor",
        "sites": "/sites",
        "registered_at": "/published",
    }
    source = (
        "https://drks.de/search/de/trial/" + identity
        if provider == "drks"
        else "https://euclinicaltrials.eu/ctis-public/view/" + identity
    )
    raw = json.dumps(native).encode()
    records = parse_registry_export(
        provider,
        raw,
        format="json",
        field_map=field_map,
        source_url=source,
        export_schema_version="explicit-fixture-export-v1",
    )
    assert records[0]["fields"]["registry_status"] == "withdrawn"
    assert (
        "results" in records[0]["missing_fields"]
        and records[0]["evidence_class"] == "registration-not-results"
    )
    conn = duckdb.connect()
    store = RegionalEvidenceStore(conn)
    result = store.import_bytes(
        provider,
        raw,
        records,
        source_url=source,
        namespace="science",
        principal_id="a",
        scopes={"operator"},
        reuse_notice="explicit fixture terms",
    )
    doc = DocumentStore(conn).get(result["document_ids"][0])
    assert doc["metadata"]["registry_status_is_source_lifecycle"] is False
    assert doc["metadata"].get("lifecycle", "active") != "withdrawn"
    native.append(native[0])
    with pytest.raises(ProviderError, match="duplicate"):
        parse_registry_export(
            provider,
            json.dumps(native).encode(),
            format="json",
            field_map=field_map,
            source_url=source,
            export_schema_version="fixture-v1",
        )


def test_drks_csv_schema_and_who_xml():
    field_map = {"id": "DRKS ID", "title": "Title", "registry_status": "Status"}
    source = "https://drks.de/search/de/trial/DRKS00027006/download"
    rows = parse_registry_export(
        "drks",
        b"DRKS ID,Title,Status\nDRKS00027006,Studie,recruiting\n",
        format="csv",
        field_map=field_map,
        source_url=source,
        export_schema_version="fixture-csv-v1",
    )
    assert rows[0]["field_locators"]["title"]["field"] == "Title"
    with pytest.raises(ProviderError, match="columns"):
        parse_registry_export(
            "drks",
            b"Other,Title\nx,Studie\n",
            format="csv",
            field_map=field_map,
            source_url=source,
            export_schema_version="fixture-csv-v1",
        )
    records = parse_drks_who_xml(
        b"<trials><trial><main><trial_id>DRKS00027006</trial_id><public_title>Studie</public_title><recruitment_status>Complete</recruitment_status></main></trial></trials>",
        source_url=source,
    )
    assert (
        records[0]["provider_id"] == "DRKS00027006"
        and records[0]["fields"]["results"] is None
    )


def test_ema_native_envelope_dates_versions_status_not_source_withdrawal():
    native = {
        "meta": {"timestamp": "2026-01-04T12:00:00Z"},
        "data": [
            {
                "ema_product_number": "EMEA/H/C/000001",
                "name_of_medicine": "Fixture",
                "medicine_status": "Withdrawn",
                "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/fixture",
                "last_updated_date": "04/01/2026",
                "revision_number": "3",
            }
        ],
    }
    client, calls, _ = native_client("ema", native)
    result, _ = client.ema_medicines("observation", ids=["EMEA/H/C/000001"])
    assert result["records"][0]["fields"]["authorisation_status"] == "Withdrawn"
    assert result["records"][0]["updated_at"] == "2026-01-04"
    assert "metadata" in result["records"][0]["kind"]
    assert calls[0]["url"] == RegionalClient.EMA_MEDICINES
    assert parse_ema(native, ids=["missing"])["records"] == []


def test_bfarm_native_feed_scope_and_update_identity():
    raw = """<rss><channel><title>BfArM</title><copyright>Publisher terms</copyright><item><title>Rote-Hand-Brief zu Produkt (Stoff): Mitteilung</title><link>https://www.bfarm.de/SharedDocs/RHB/fixture.html</link><pubDate>Mon, 10 Aug 2026 12:32:00 +0200</pubDate><description>Geänderte Information.</description></item></channel></rss>""".encode()
    client, _calls, _ = native_client("bfarm", raw)
    result, _ = client.bfarm_notices("observation")
    row = result["records"][0]
    assert row["fields"]["named_product_or_substance_text"] == "Produkt (Stoff)"
    assert not row["fields"]["causality_assessed"] and "PEI" in row["coverage_notice"]
    assert row["published_at"].endswith("+02:00")
    with pytest.raises(ProviderError):
        parse_bfarm_feed(raw.replace(b"www.bfarm.de", b"www.pei.de"))


def test_opencorporates_native_credentials_jurisdiction_and_historical_fields():
    native = {
        "results": {
            "company": {
                "name": "Muster GmbH",
                "jurisdiction_code": "de_be",
                "company_number": "123",
                "registered_address": {"locality": "Berlin"},
                "inactive": True,
            }
        }
    }
    client, calls, _ = native_client("opencorporates", native, key="private-test-key")
    rows, capture = client.company("de_be", "123", "obs")
    assert calls[0]["params"]["api_token"] == "private-test-key"
    assert "private-test-key" not in json.dumps(capture.receipt)
    assert (
        rows[0]["fields"]["jurisdiction"] == "de_be" and rows[0]["fields"]["inactive"]
    )
    assert rows[0]["fields"]["registered_address"]["locality"] == "Berlin"


def test_opensanctions_actual_response_envelope_review_only_and_native_lists():
    native = {
        "responses": {
            "q1": {
                "results": [
                    {
                        "id": "Qfixture",
                        "caption": "Müller",
                        "schema": "Person",
                        "score": 0.72,
                        "datasets": ["eu_fsf"],
                        "properties": {"name": ["Müller"], "country": ["de"]},
                    }
                ]
            }
        }
    }
    client, calls, _ = native_client("opensanctions", native, key="private-test-key")
    result, capture = client.sanctions_match(
        {"q1": {"schema": "Person", "properties": {"name": ["Müller"]}}}, "obs"
    )
    assert calls[0]["headers"]["Authorization"] == "ApiKey private-test-key"
    assert result["review_required"] and not result["automatic_merge"]
    assert result["records"][0]["fields"]["originating_lists"] == ["eu_fsf"]
    assert "private-test-key" not in json.dumps(capture.receipt)


def test_cellar_sparql_native_work_expression_manifestation_identities():
    values = {
        "work": "http://publications.europa.eu/resource/cellar/w",
        "expression": "http://publications.europa.eu/resource/cellar/w.0001",
        "manifestation": "http://publications.europa.eu/resource/cellar/w.0001.01",
        "item": "http://publications.europa.eu/resource/cellar/w.0001.01/DOC_1",
        "celex": "32016R0679",
        "eli": "http://data.europa.eu/eli/reg/2016/679/oj",
        "ecli": "ECLI:EU:C:2026:1",
        "language": "http://publications.europa.eu/resource/authority/language/DEU",
        "title": "Fiktiver Rechtstext",
        "document_date": "2016-04-27",
        "published": "2016-05-04",
        "effective": "2016-05-24",
        "predicate": "http://publications.europa.eu/ontology/cdm#work_cites_work",
        "related": "http://publications.europa.eu/resource/cellar/other",
    }
    native = {
        "results": {
            "bindings": [
                {k: {"type": "literal", "value": v} for k, v in values.items()}
            ]
        }
    }
    client, calls, _ = native_client("cellar", native)
    result, _ = client.cellar(["32016R0679"], "obs")
    record = result["records"][0]
    assert record["fields"]["work"] != record["fields"]["expression"]
    assert record["language"] == "de" and not record["fields"]["current_law_verified"]
    assert record["relationships"][0]["basis"] == "explicit-CDM-triple"
    assert record["fields"]["eli"] == values["eli"]
    assert record["fields"]["ecli"] == values["ecli"]
    assert record["published_at"] == "2016-05-04"
    assert record["fields"]["document_dates"] == ["2016-04-27"]
    assert "cdm:resource_legal_eli ?eli" in calls[0]["params"]["query"]
    assert "cdm:case-law_ecli ?ecli" in calls[0]["params"]["query"]
    assert calls[0]["headers"]["Accept"] == "application/sparql-results+json"
    assert (
        '"32016R0679"^^<http://www.w3.org/2001/XMLSchema#string>'
        in calls[0]["params"]["query"]
    )
    assert record["native"]["bindings"] == native["results"]["bindings"]
    assert "LIMIT 100 OFFSET 0" in calls[0]["params"]["query"]
    native["results"]["bindings"].append(
        {
            **native["results"]["bindings"][0],
            "effective": {"type": "literal", "value": "2018-05-25"},
        }
    )
    second_client, _, _ = native_client("cellar", native)
    grouped, _ = second_client.cellar(["32016R0679"], "dates")
    assert grouped["records"][0]["fields"]["effective_dates"] == [
        "2016-05-24",
        "2018-05-25",
    ]
    assert grouped["records"][0]["fields"]["effective_from"] is None
    with pytest.raises(ValueError):
        client.cellar(['x" } DELETE WHERE { ?s ?p ?o }'], "bad")


def test_berlin_import_keeps_legal_kinds_historical_and_pdf_locators():
    raw = Path("tests/fixtures/pdf_benchmark/digital.pdf").read_bytes()
    record = parse_berlin_publication(
        raw,
        source_url="https://www.berlin.de/fixture.pdf",
        official_id="GVBl-fixture-2026-1",
        kind="gazette",
        title="Fiktives Amtsblatt",
        historical=True,
    )
    assert record["kind"] == "gazette" and record["fields"]["historical"] is True
    assert (
        record["is_current_law"] is None
        and record["sections"][0]["locator"]["page"] == 1
    )
    with pytest.raises(ProviderError):
        parse_berlin_publication(
            raw,
            source_url="https://commentary.example/law",
            official_id="x",
            kind="law",
            title="Not official",
        )


def test_bfarm_duplicate_entries_are_idempotent_but_conflicts_are_visible():
    item = b"<item><guid>letter-1</guid><link>https://www.bfarm.de/letter.html</link><title>Rote-Hand-Brief zu Beispiel</title><description>Deutsche Mitteilung.</description></item>"
    feed = b"<rss><channel>" + item + item + b"</channel></rss>"
    assert len(parse_bfarm_feed(feed)["records"]) == 1
    changed = item.replace(b"Deutsche Mitteilung.", b"Geaenderte Mitteilung.")
    with pytest.raises(ProviderError, match="conflicting"):
        parse_bfarm_feed(b"<rss><channel>" + item + changed + b"</channel></rss>")


def test_regional_batch_validation_rolls_back_documents_and_receipt():
    from copy import deepcopy

    client, _, conn = native_client("german-courts", COURT)
    records, response = client.court_decision("JUREfixture", "obs")
    invalid = deepcopy(records[0])
    invalid["provider_id"] = "invalid-language"
    invalid["language"] = 123
    store = RegionalEvidenceStore(conn)
    with pytest.raises(ProviderError, match="validation"):
        store.ingest(
            [records[0], invalid],
            response,
            namespace="law",
            principal_id="a",
            scopes={"operator"},
            reuse_notice="authored fixture",
        )
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM regional_observation_receipts").fetchone()[0]
        == 0
    )
    assert store.ingest(
        records,
        response,
        namespace="law",
        principal_id="a",
        scopes={"operator"},
        reuse_notice="authored fixture",
    )["document_ids"]
    conn.close()


def test_court_index_explicit_byte_ceiling_survives_transport_and_parser():
    raw = b"<items></items>"
    client, calls, conn = native_client("german-courts", raw)
    result, _ = client.court_index("index", max_index_bytes=25_000_000)
    assert result["records"] == []
    assert calls[0]["max_bytes"] == 25_000_000
    with pytest.raises(ValueError, match="ceiling"):
        client.court_index("invalid", max_index_bytes=100_000_001)
    assert len(calls) == 1
    with pytest.raises(ProviderError, match="oversized"):
        parse_court_index(raw, max_index_bytes=5)
    conn.close()


def test_bfarm_unstructured_heading_does_not_invent_product_name():
    raw = b"<rss><channel><item><title>Korrigierte Sicherheitsinformationen</title><link>https://www.bfarm.de/notice.html</link></item></channel></rss>"
    row = parse_bfarm_feed(raw)["records"][0]
    assert row["fields"]["named_product_or_substance_text"] is None
    assert "named_product_or_substance_text" in row["missing_fields"]
    assert row["fields"]["notice_type"] == "unspecified"


def test_court_index_streaming_window_filters_before_pagination():
    raw = (
        "<items>"
        + "".join(
            f"<item><gericht>{'BGH' if i % 2 else 'BVerwG'}</gericht><modified>2026-09-08</modified><link>https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-{i}.zip</link></item>"
            for i in range(2000)
        )
        + "</items>"
    ).encode()
    page = parse_court_index(raw, court="BGH", since="2026-09-01", offset=998, limit=1)
    assert page["total_selected"] == 1000
    assert page["next_offset"] == 999
    assert page["records"][0]["url"].endswith("jb-1997.zip")
    assert (
        parse_court_index(raw, court="BGH", offset=999, limit=1)["next_offset"] is None
    )
    with pytest.raises(DefusedXmlException):
        parse_court_index(
            b'<!DOCTYPE items [<!ENTITY x SYSTEM "file:///etc/passwd">]><items>&x;</items>'
        )


def test_court_repeated_passages_keep_own_paragraph_numbers_and_all_citations():
    raw = COURT.replace(
        b"<ecli/>", b"<ecli/><norm>  Art.  3 GG </norm><norm>Art. 19 GG</norm>"
    )
    raw = raw.replace(
        b"</dl>", b'<dt><a name="rd_2">2</a></dt><dd><p>Deutscher Absatz.</p></dd></dl>'
    )
    # Official numbered blocks each have their own dl, even for identical text.
    raw = raw.replace(b'<dt><a name="rd_2">', b'</dl><dl><dt><a name="rd_2">')
    record = parse_court_download(raw)
    repeated = [
        part for part in record["sections"] if part["text"] == "Deutscher Absatz."
    ]
    assert [part["locator"]["paragraph_number"] for part in repeated] == ["1", "2"]
    assert repeated[0]["locator"]["path"] != repeated[1]["locator"]["path"]
    assert record["native"]["field_occurrences"]["norm"] == ["Art. 3 GG", "Art. 19 GG"]
    assert record["fields"]["cited_norms"] == "Art. 3 GG Art. 19 GG"


def test_ema_document_index_has_explicit_bounded_large_capture_option():
    client, calls, conn = native_client("ema", {"data": []})
    for invalid in [True, 0, 100_000_001]:
        with pytest.raises(ValueError, match="byte ceiling"):
            client.ema_documents("invalid", "EMEA/H/C/000001", max_index_bytes=invalid)
    assert not calls
    result, _ = client.ema_documents(
        "large", "EMEA/H/C/000001", max_index_bytes=80_000_000
    )
    assert result["documents"] == []
    assert calls[0]["max_bytes"] == 80_000_000
    conn.close()


def test_public_sanctions_bulk_aliases_absence_and_no_hosted_score():
    rows = [
        {
            "id": "fixture-a",
            "schema": "Person",
            "caption": "Authored German name",
            "properties": {"name": ["Jens Müller"], "alias": ["J. Müller"]},
            "datasets": ["eu_fsf"],
            "target": True,
        },
        {
            "id": "fixture-b",
            "schema": "Person",
            "properties": {"name": ["Jens Muller"]},
            "datasets": ["eu_fsf"],
        },
    ]
    raw = "\n".join(json.dumps(row) for row in rows).encode()
    client, calls, conn = native_client("opensanctions", raw)
    result, _ = client.sanctions_dataset(
        ["j. MÜLLER"],
        "bulk",
        artifact_version="20260908225701-test",
        previous_entity_ids=["fixture-removed"],
    )
    assert result["scanned_entities"] == 2
    assert result["matched_entities"] == 1
    assert result["missing_from_selected_dataset"] == ["fixture-removed"]
    present, absent = result["records"]
    assert present["fields"]["entity_id"] == "fixture-a"
    assert present["fields"]["provider_score"] is None
    assert present["fields"]["dataset_presence"] == "present"
    assert absent["fields"]["dataset_presence"] == "absent-from-complete-capture"
    assert absent["fields"]["absence_is_not_global_delisting"]
    assert all(
        r["review_required"] and not r["fields"]["automatic_merge"]
        for r in result["records"]
    )
    assert len(calls) == 1 and "Authorization" not in calls[0]["headers"]
    with pytest.raises(ProviderError, match="credential"):
        client.sanctions_match(
            {"q": {"schema": "Person", "properties": {"name": ["test"]}}}, "hosted"
        )
    with pytest.raises(ValueError):
        client.sanctions_dataset(["test"], "bad", artifact_version="../../elsewhere")
    assert len(calls) == 1
    conn.close()

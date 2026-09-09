import copy
import json

import duckdb
import pytest

from src.ingestion.crossref_notices import CrossrefNoticeCollection, normalize_notices
from src.ingestion.document_store import DocumentStore
from src.integrity.ledger import integrity_ledger
from src.kb.report_updates import ReportUpdateStore
from src.kb.watches import _event_specs
from tests.unit.kb.test_authored_reports import CONTENT


def work(kind="retraction", provider="retraction-watch"):
    return {
        "DOI": "10.1234/notice",
        "title": ["Berichtigung: Berliner Forschungsdaten"],
        "language": "de",
        "indexed": {"date-time": "2026-09-01T00:00:00Z"},
        "update-to": [
            {
                "DOI": "10.1234/paper",
                "type": kind,
                "source": provider,
                "updated": {"date-parts": [[2026, 8, 31]]},
                "record-id": "fixture-1",
            }
        ],
    }


def options():
    return {
        "from_date": "2026-09-01",
        "until_date": "2026-09-02",
        "targets": {"10.1234/paper": "doc"},
    }


def response(items, cursor="next"):
    return {"content": json.dumps({"message": {"items": items, "next-cursor": cursor}})}


def seed(conn):
    store = DocumentStore(conn)
    assert (
        store.upsert(
            [
                {
                    "document_id": "doc",
                    "source_type": "web",
                    "source_id": "crossref",
                    "language": "de",
                    "ingested_at": 1,
                    "url": "https://doi.org/10.1234/paper",
                    "title": "Forschungsdaten",
                    "content": "Die ursprünglichen Berliner Forschungsdaten bleiben zitierbar.",
                    "metadata": {},
                    "authors": [],
                }
            ]
        ).inserted
        == 1
    )
    return store.revisions.revision("doc")


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("retraction", "retraction"),
        ("correction", "correction"),
        ("expression-of-concern", "expression_of_concern"),
        ("withdrawal", "withdrawal"),
    ],
)
def test_typed_notices_without_inferred_missing_verdict(kind, expected):
    raw = work(kind)
    assert normalize_notices(raw)[0]["notice_type"] == expected
    raw["update-to"][0].pop("DOI")
    assert normalize_notices(raw)[0]["status"] == "unresolved"
    assert normalize_notices(work("unknown"))[0]["status"] == "unresolved"
    assert normalize_notices(work(provider="unknown"))[0]["status"] == "unresolved"


def test_capture_report_integrity_watch_and_restart(tmp_path):
    path = str(tmp_path / "notices.duckdb")
    conn = duckdb.connect(path)
    original = seed(conn)
    reports = ReportUpdateStore(conn)
    content = copy.deepcopy(CONTENT)
    dep = content["sections"][0]["assertions"][0]["dependencies"][0]
    dep["revision"] = dep["locator"]["revision_id"] = original["revision_id"]
    auth = {"principal_id": "researcher", "scopes": {"operator"}}
    report = reports.create("r", "study", content, **auth)
    assert (
        reports.assess("r", report["report_id"], **auth)["sections"][0]["status"]
        == "current"
    )
    calls = []

    def fetch(**kw):
        calls.append(kw)
        return response([work()])

    collection = CrossrefNoticeCollection(
        conn, "september", transport=fetch, **options()
    )
    completed = collection.step()
    assert completed["status"] == "complete"
    scan = reports.assess("r", report["report_id"], **auth)
    assert scan["sections"][0]["status"] == "affected"
    proposal = reports.propose("r", scan["assessment_id"], "a1", **auth)
    assert proposal["proposal"]["reasons"] == ["provider_notice_requires_review"]
    assert (
        reports.inspect("r", report["report_id"], revision=1, **auth)["content"]
        == content
    )
    assert DocumentStore(conn).revisions.revision("doc") == original
    findings = integrity_ledger(conn, ["doc"])["findings"]
    notice = next(f for f in findings if f["kind"] == "provider_notice")
    assert notice["provider"] == "retraction-watch"
    watch_finding = {**notice, "fingerprint": notice["notice_id"]}
    before = {"integrity_findings": []}
    after = {"integrity_findings": [watch_finding]}
    events = _event_specs(before, after)
    assert len(events) == 1 and events[0]["reason_code"] == "retraction_detected"
    assert _event_specs(after, after) == []
    conn.close()
    conn = duckdb.connect(path)
    try:
        assert (
            CrossrefNoticeCollection(
                conn, "september", transport=fetch, **options()
            ).step()
            == completed
        )
        assert len(calls) == 1
        assert DocumentStore(conn).revisions.revision("doc") == original
        assert conn.execute("SELECT count(*) FROM crossref_notices").fetchone() == (1,)
        with pytest.raises(ValueError, match="configuration changed"):
            CrossrefNoticeCollection(conn, "september", **{**options(), "rows": 2})
    finally:
        conn.close()


def test_pagination_missing_types_and_failed_page_do_not_advance():
    conn = duckdb.connect()
    try:
        seed(conn)
        pages = [
            response([work("expression-of-concern")]),
            {"status": 429},
            response([]),
        ]
        calls = []

        def fetch(**kw):
            calls.append(kw)
            return pages.pop(0)

        collection = CrossrefNoticeCollection(
            conn, "pages", rows=1, transport=fetch, **options()
        )
        assert collection.step()["status"] == "running"
        with pytest.raises(ValueError, match="429"):
            collection.step()
        assert collection.inspect()["pages"] == 1
        assert collection.step()["status"] == "complete"
        assert calls[1]["params"]["cursor"] == calls[2]["params"]["cursor"] == "next"
        assert collection.inspect()["requests"] == 3
        assert DocumentStore(conn).revisions.revision("doc")["lifecycle"] == "active"
        assert (
            "expression_of_concern"
            in conn.execute("SELECT notice_json FROM crossref_notices").fetchone()[0]
        )
    finally:
        conn.close()


def test_limits_and_transaction_rollback():
    conn = duckdb.connect()
    try:
        collection = CrossrefNoticeCollection(
            conn,
            "missing-target",
            transport=lambda **_: response([work()]),
            **options(),
        )
        with pytest.raises(ValueError, match="does not exist"):
            collection.step()
        assert conn.execute("SELECT count(*) FROM crossref_notices").fetchone() == (0,)
        assert collection.inspect()["pages"] == 0
        oversized = CrossrefNoticeCollection(
            conn,
            "too-large",
            max_bytes=1,
            max_pages=1,
            transport=lambda **_: response([]),
            **options(),
        )
        with pytest.raises(ValueError, match="byte limit"):
            oversized.step()
        assert oversized.step()["status"] == "bounded"
    finally:
        conn.close()


def test_late_target_binding_and_persistent_watch_dedup(tmp_path):
    from src.kb.registry import load_registry
    from src.kb.watches import (
        commit_watch_watermark,
        create_watch,
        record_external_snapshot,
    )
    from tests.unit.kb.test_watches import CONFIG

    conn = duckdb.connect()
    try:
        seed(conn)
        fetch = lambda **_: response([work()])
        CrossrefNoticeCollection(
            conn, "unbound", transport=fetch, **{**options(), "targets": {}}
        ).step()
        assert conn.execute("SELECT document_id FROM crossref_notices").fetchone() == (
            None,
        )
        CrossrefNoticeCollection(conn, "bound", transport=fetch, **options()).step()
        assert conn.execute("SELECT document_id FROM crossref_notices").fetchone() == (
            "doc",
        )
        path = tmp_path / "domains.yml"
        path.write_text(CONFIG)
        backing = load_registry(path).resolve("economics", conn=conn)
        watch = create_watch(
            backing, "researcher", {"type": "topic", "value": "research"}, now_ms=1
        )
        commit_watch_watermark(conn, 1, {"notices": "before"})
        record_external_snapshot(
            conn,
            "researcher",
            watch["watch_id"],
            1,
            {"integrity_findings": []},
            observed_at_ms=1,
        )
        findings = integrity_ledger(conn, ["doc"])["findings"]
        notice = next(f for f in findings if f["kind"] == "provider_notice")
        after = {"integrity_findings": [{**notice, "fingerprint": notice["notice_id"]}]}
        commit_watch_watermark(conn, 2, {"notices": "after"})
        assert (
            record_external_snapshot(
                conn, "researcher", watch["watch_id"], 2, after, observed_at_ms=2
            )["emitted_events"]
            == 1
        )
        assert (
            record_external_snapshot(
                conn, "researcher", watch["watch_id"], 2, after, observed_at_ms=2
            )["emitted_events"]
            == 0
        )
    finally:
        conn.close()

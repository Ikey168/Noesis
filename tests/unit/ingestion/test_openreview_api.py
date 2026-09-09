import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion.openreview_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter, SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)


def note(identity="a", kind="Submission", replyto=None):
    return {
        "id": identity,
        "forum": "a",
        "replyto": replyto,
        "readers": ["everyone"],
        "invitations": [f"Venue/-/{kind}"],
        "signatures": ["Venue/Anonymous_Reviewer1"],
        "tmdate": 1750000000000,
        "content": {
            "title": {"value": "Berliner Forschung — Überblick"},
            "abstract": {"value": "Öffentlich verfügbare Evidenz."},
        },
    }


def manifest():
    return validate_source_pack(
        json.loads(Path("config/source_packs/openreview.json").read_text())
    )


def test_thread_revisions_and_hidden_fields():
    raw = note("b", "Official_Review", "a")
    raw["content"]["authorids"] = {
        "value": ["private@example.org"],
        "readers": ["chairs"],
    }
    before = copy.deepcopy(raw)
    mapped, cursor = records({"notes": [raw]}, cursor=None, limit=1)
    assert raw == before
    assert cursor == "b"
    assert mapped[0]["note_type"] == "review"
    assert mapped[0]["replyto"] == mapped[0]["forum"] == "a"
    assert mapped[0]["authors"] == []
    assert "private@example.org" not in json.dumps(mapped)
    assert mapped[0]["published_at"] is None
    raw["content"]["review"] = {"value": "Revised assessment"}
    revised, _ = records({"notes": [raw]}, cursor=None, limit=2)
    assert revised[0]["version"] != mapped[0]["version"]


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("Submission", "submission"),
        ("Rebuttal", "rebuttal"),
        ("Decision", "decision"),
        ("NovelSchema", "unknown"),
    ],
)
def test_types(kind, expected):
    assert (
        records({"notes": [note(kind=kind)]}, cursor=None, limit=2)[0][0]["note_type"]
        == expected
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"notes": [{}]},
        {"notes": [note(), note()]},
        {"notes": [{**note(), "readers": ["chairs"]}]},
    ],
)
def test_invalid_pages(payload):
    with pytest.raises(ValueError):
        records(payload, cursor=None, limit=1)


def test_parameters_and_unavailable_notes():
    assert parameters({"parameters": {"forum": "a"}}, cursor="b", limit=2) == {
        "forum": "a",
        "after": "b",
        "limit": 2,
        "sort": "id:asc",
    }
    with pytest.raises(ValueError):
        parameters({"parameters": {"tauthor": "private"}}, cursor=None, limit=2)
    assert records({"notes": []}, cursor=None, limit=2) == ([], None)
    source = manifest()["sources"][0]
    for status, code in [
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (503, "source_unavailable"),
    ]:
        with pytest.raises(SourcePackError) as failure:
            HTTPSPageAdapter(
                source, transport=lambda status=status, **_: {"status": status}
            ).fetch_page(
                {"operation": "search", "parameters": {"forum": "a"}, "limit": 2},
                cursor=None,
            )
        assert failure.value.code == code


def test_runtime_restart_replay_and_edited_note(tmp_path):
    raw = json.loads(Path("config/source_packs/openreview.json").read_text())
    raw["defaults"]["budgets"]["max_results"] = 1
    pack = validate_source_pack(raw)
    source = pack["sources"][0]
    calls = []
    edited = False

    def transport(**kw):
        assert "Authorization" not in kw["headers"]
        after = kw["params"].get("after")
        calls.append(after)
        value = note() if after is None else note("b", "Rebuttal", "a")
        if edited:
            value["content"]["abstract"]["value"] = "Überarbeitete Evidenz."
            value["tmdate"] += 1000
        return {"content": json.dumps({"notes": [] if after == "b" else [value]})}

    adapter = HTTPSPageAdapter(source, transport=transport, secret="unused")
    request = {
        "pack_id": pack["pack_id"],
        "run_key": "scan",
        "operation": "search",
        "source_ids": [source["source_id"]],
        "required_sources": [source["source_id"]],
        "parameters": {"forum": "a"},
        "max_pages": 5,
        "max_results": 10,
    }
    conn = duckdb.connect(str(tmp_path / "notes.duckdb"))
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    runtime.accept_license(
        pack["pack_id"], source["source_id"], principal_id="operator"
    )
    kwargs = {
        "principal_id": "operator",
        "adapters": {source["source_id"]: adapter},
        "dns_resolver": lambda _: ["8.8.8.8"],
    }

    def interrupt(_source, page):
        if page == 1:
            raise RuntimeError("interrupt")

    with pytest.raises(RuntimeError, match="interrupt"):
        runtime.run(request, fault=interrupt, **kwargs)
    conn.close()
    conn = duckdb.connect(str(tmp_path / "notes.duckdb"))
    try:
        runtime = SourcePackRuntime(conn)
        assert runtime.run(request, **kwargs)["status"] == "complete"
        assert calls == [None, "a", "b"]
        runtime.run(request, **kwargs)
        assert calls == [None, "a", "b"]
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (2,)
        edited = True
        assert (
            runtime.run({**request, "run_key": "rescan"}, **kwargs)["status"]
            == "complete"
        )
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM documents WHERE content = ?",
            ["Überarbeitete Evidenz."],
        ).fetchone() == (2,)
    finally:
        conn.close()

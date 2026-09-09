import copy
import io
import json
import zipfile
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("pypandoc")

from src.kb.authored_reports import AuthoredReportStore, ReportError
from src.kb.citeproc_export import build_ast, render_report
from tests.unit.kb.test_authored_reports import AUTH, CONTENT

METADATA = {
    "source-1": {
        "type": "report",
        "title": "Berliner Forschung und öffentliche Förderung",
        "author": [{"family": "Müller", "given": "Jörg"}],
        "issued": {"date-parts": [[2025]]},
        "publisher": "Berlin",
    }
}


def setup(conn):
    content = copy.deepcopy(CONTENT)
    content["title"] = "Berliner Forschungsbericht"
    content["sections"][0]["assertions"].append(
        {
            "id": "a3",
            "text": "Eine wiederholte Quellenangabe.",
            "kind": "sourced",
            "citations": ["source-1"],
            "dependencies": copy.deepcopy(
                content["sections"][0]["assertions"][0]["dependencies"]
            ),
        }
    )
    content["bibliography"].append(
        {"id": "unstructured", "text": "Unvollständige Quelle: ÄÖÜ ß."}
    )
    store = AuthoredReportStore(conn)
    state = store.create("r", "render", content, **AUTH)
    return store, state


@pytest.mark.parametrize("locale", ["de-DE", "en-US"])
def test_docx_reference_footnotes_umlauts_locators_and_replay(
    tmp_path, monkeypatch, locale
):
    path = str(tmp_path / "report.duckdb")
    conn = duckdb.connect(path)
    store, state = setup(conn)
    opts = {
        "citation_metadata": METADATA,
        "locators": {"a1": {"source-1": "12"}},
        "locale": locale,
        **AUTH,
    }
    baseline = store.export("r", state["report_id"], **AUTH)
    result = render_report(store, "r", state["report_id"], **opts)
    assert result["markdown"] == baseline["markdown"]
    assert result["receipt"]["authored_fallback_ids"] == ["unstructured"]
    with zipfile.ZipFile(io.BytesIO(result["content"])) as docx:
        from xml.etree import ElementTree

        body = "".join(
            ElementTree.fromstring(docx.read("word/document.xml")).itertext()
        )
        footnotes = docx.read("word/footnotes.xml").decode()
        assert "Müller" in body and "ÄÖÜ ß" in body and "2025" in body
        assert ("S. 12" if locale == "de-DE" else "p. 12") in body
        assert body.count("Müller") >= 3
        assert "revision-1" in footnotes and "Source dependencies for a1" in footnotes
        assert "One source; no causal identification." in body
    assert result["receipt"]["source_package"] == baseline
    conn.close()
    conn = duckdb.connect(path)
    try:
        monkeypatch.setattr(
            "src.kb.citeproc_export._run",
            lambda *_a, **_k: pytest.fail("replay rendered again"),
        )
        assert (
            render_report(AuthoredReportStore(conn), "r", state["report_id"], **opts)
            == result
        )
        with pytest.raises(ReportError):
            render_report(
                AuthoredReportStore(conn),
                "r",
                state["report_id"],
                **{**opts, "principal_id": "other"},
            )
        conn.execute("UPDATE report_citeproc_exports SET payload=?", [b"tampered"])
        with pytest.raises(ReportError, match="hash mismatch"):
            render_report(AuthoredReportStore(conn), "r", state["report_id"], **opts)
    finally:
        conn.close()


def test_pdf_actual_renderer_and_literal_external_asset_syntax(tmp_path):
    pytest.importorskip("typst")
    fitz = pytest.importorskip("fitz")
    conn = duckdb.connect()
    try:
        store, state = setup(conn)
        sentinel = tmp_path / "private.txt"
        sentinel.write_text("LOCAL_FILE_SENTINEL_NOT_FOR_EXPORT")
        amended = copy.deepcopy(state["content"])
        amended["sections"][0]["assertions"][1]["text"] = (
            f'![asset]({sentinel}) #read("{sentinel}")'
        )
        store.revise("r", state["report_id"], 1, amended, **AUTH)
        result = render_report(
            store,
            "r",
            state["report_id"],
            citation_metadata=METADATA,
            output_format="pdf",
            locale="de-DE",
            **AUTH,
        )
        pdf = fitz.open(stream=result["content"], filetype="pdf")
        text = " ".join(p.get_text() for p in pdf)
        assert "Müller" in text and "Berliner Forschungsbericht" in text
        assert "revision-1" in text and len(pdf) >= 1
        assert "LOCAL_FILE_SENTINEL_NOT_FOR_EXPORT" not in text
        assert result["receipt"]["config"]["typst"] == "0.15.0"
        pdf.close()
        Path(tmp_path / "report.pdf").write_bytes(result["content"])
    finally:
        conn.close()


def test_ast_literal_text_and_invalid_metadata():
    conn = duckdb.connect()
    try:
        store, state = setup(conn)
        changed = copy.deepcopy(state)
        changed["content"]["sections"][0]["assertions"][0]["text"] = (
            '![secret](file:///etc/passwd) #read("/etc/passwd") <iframe src="https://example.org" />'
        )
        ast, _, _ = build_ast(changed, METADATA, {}, "en-US", [1, 23, 1])
        serial = json.dumps(ast)
        assert (
            '"Image"' not in serial
            and '"RawInline"' not in serial
            and '"RawBlock"' not in serial
        )
        assert "file:///etc/passwd" in serial
        for kwargs in [
            {"citation_metadata": {"unknown": {}}},
            {
                "citation_metadata": {
                    "source-1": {"title": '<img src="file:///etc/passwd">'}
                }
            },
            {
                "citation_metadata": {
                    "source-1": {"issued": {"date-parts": [[2025, 99]]}}
                }
            },
            {"locators": {"a2": {"source-1": "12"}}},
            {"locale": "xx"},
        ]:
            with pytest.raises(ReportError):
                render_report(store, "r", state["report_id"], **kwargs, **AUTH)
        assert conn.execute(
            "SELECT count(*) FROM report_citeproc_exports"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_real_worker_timeout_and_file_limit(tmp_path):
    import sys

    from src.kb.citeproc_export import _run

    with pytest.raises(ReportError, match="wall-clock"):
        _run(
            "pandoc",
            [sys.executable, "-c", "import time; time.sleep(2)"],
            tmp_path,
            timeout=0.1,
        )
    with pytest.raises(ReportError, match="renderer|File too large"):
        _run(
            "pandoc",
            [sys.executable, "-c", "open('large.bin','wb').write(b'x'*21_000_000)"],
            tmp_path,
        )
    assert (tmp_path / "large.bin").stat().st_size <= 20_000_000

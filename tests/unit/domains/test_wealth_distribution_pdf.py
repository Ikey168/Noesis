"""ECB HFCS and Bundesbank PHF PDF tables (#2882): declared cells only, page and line cited, fail closed.

The PDFs are authored with PyMuPDF (fictional values, years 2093-2099); nothing here is a published figure.
"""

from __future__ import annotations

import builtins
import copy
import json

import pytest

from src.ingestion.source_packs import SourcePackError
from src.ingestion.wealth_distribution_sources import (
    PDF_PROVIDERS,
    WealthAdapter,
    WealthFormatError,
    check_pdf_document,
    fixture_transport,
    parse_release,
    pdf_lines,
    read_cell,
)
from tests.unit import wealth_distribution_harness as h

pymupdf = pytest.importorskip("pymupdf")


def authored_pdf(*lines: str) -> bytes:
    """One page; each argument is a printed line whose cells are separated by '|'."""
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    for number, line in enumerate(lines):
        for index, cell in enumerate(line.split("|")):
            if cell:
                page.insert_text((40 + index * 60 if index else 40, 60 + number * 14), cell, fontsize=9,
                                 fontname="helv")
    raw = document.tobytes(garbage=4, deflate=True, no_new_id=True)
    document.close()
    return raw


def hfcs_document() -> dict:
    return copy.deepcopy(h.source("hfcs")["wealth"]["documents"][0])


def observations(name: str, *, revised: bool = False) -> dict:
    out = {}
    for page in h.fetch(name, revised=revised):
        for record in page:
            item = record["wealth_item"]
            for obs in item["observations"]:
                out[(item["area"]["code"], item["indicator"]["code"], obs["period"])] = obs
    return out


def test_hfcs_reads_only_the_declared_cells_with_page_line_and_printed_token():
    cells = observations("hfcs")
    # Four declared countries x three declared rows; the EA and BE columns are printed but not read.
    assert {k[0] for k in cells} == {"DE", "ES", "FR", "IT"} and len(cells) == 12
    median = cells[("DE", "median", "2096")]
    assert median["value"] == "110.3" and median["status"] == "reported"
    assert median["attributes"]["pdf_page"] == "1"
    assert median["attributes"]["pdf_table"] == "Table 2.1 Net wealth (EUR thousands)"
    assert median["attributes"]["quoted_line"] == "Median 150.1 250.2 110.3 160.4 180.5 140.6"
    # Table 2.2 also has a 'Median' row; the section ends at its heading, so it is never read.
    assert all("190.1" not in o["attributes"]["quoted_line"] for o in cells.values())
    # A footnote flag is kept beside the value, never folded into it.
    mean = cells[("DE", "mean", "2096")]
    assert mean["value"] == "320.3" and mean["attributes"]["printed"] == "320.3*"
    assert mean["attributes"]["pdf_flag"] == "*" and "fewer than 25" in mean["attributes"]["pdf_flag_meaning"]
    # A declared marker is a status, never a value.
    share = cells[("DE", "share", "2096")]
    assert share["status"] == "not_published" and share["value"] is None and share["attributes"]["printed"] == "-"
    assert cells[("FR", "share", "2096")]["value"] == "52.5"
    assert cells[("FR", "share", "2096")]["attributes"]["printed"] == "52.5%"


def test_phf_reads_survey_year_columns_in_german_notation_and_skips_undeclared_rows():
    cells = observations("phf")
    assert set(cells) == {("DE", s, y) for s in ("mean", "median", "share") for y in ("2093", "2095", "2097")}
    assert cells[("DE", "median", "2097")]["value"] == "106600"
    assert cells[("DE", "median", "2097")]["attributes"]["printed"] == "106.600"
    assert cells[("DE", "share", "2095")]["value"] == "56.2"
    assert cells[("DE", "share", "2095")]["attributes"]["printed"] == "56,2"
    # The Gini row is printed but not declared, so it is never read.
    assert all("Gini" not in o["attributes"]["quoted_line"] for o in cells.values())


def test_revised_editions_append_vintages():
    conn = h.connection()
    for name in ("hfcs", "phf"):
        h.apply(conn, name)
        h.apply(conn, name, revised=True)
    series = [s for s in h.store(conn).find_series(h.NS) if s["provider"] in PDF_PROVIDERS]
    assert len(series) == 15 and {s["vintage_count"] for s in series} == {2}
    revised = observations("hfcs", revised=True)
    # The later data version publishes the German top-10% share the first one withheld.
    assert revised[("DE", "share", "2096")]["value"] == "54.9"
    assert ("DE", "mean", "2099") in observations("phf", revised=True)


@pytest.mark.parametrize(("lines", "reason"), [
    (("Table 2.1 Net wealth (EUR thousands)", "Table 2.1 Net wealth (EUR thousands)"), "appears 2 times"),
    (("Table 9 Something else",), "not found"),
    (("Table 2.1 Net wealth (EUR thousands)", "Mean|1|2|3|4|5|6"), "row 'Median' not found"),
    (("Table 2.1 Net wealth (EUR thousands)", "Median|1|2|3|4|5|6", "Median|1|2|3|4|5|6"), "appears 2 times"),
    (("Table 2.1 Net wealth (EUR thousands)", "Median|1|2|3|4|5"), "holds 5 cells for 6"),
    (("Table 2.1 Net wealth (EUR thousands)", "Median|1|2|n.a.|4|5|6"), "row 'Median' not found"),
])
def test_anything_short_of_one_exact_reading_fails_the_document(lines, reason):
    document = hfcs_document()
    document["tables"][0]["rows"] = document["tables"][0]["rows"][:1]
    with pytest.raises(WealthFormatError) as failed:
        parse_release(authored_pdf(*lines), provider="ecb-hfcs-statistical-tables", document=document, url="x")
    assert failed.value.code == "schema_drift" and reason in str(failed.value)


def test_a_drifted_pdf_fails_the_run_and_never_yields_a_partial_release():
    source = h.source("hfcs")
    page = dict(h.pages("hfcs")[0])
    import base64

    page["body_base64"] = base64.b64encode(authored_pdf("Table 2.1 Net wealth (EUR thousands)",
                                                        "Median|1|2|3|4|5")).decode()
    adapter = WealthAdapter(source, transport=fixture_transport([page]))
    with pytest.raises(SourcePackError) as failed:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 80}, cursor=None)
    assert failed.value.code == "schema_drift"
    page["body_base64"] = base64.b64encode(b"<html>not a pdf</html>").decode()
    with pytest.raises(SourcePackError) as html:
        WealthAdapter(source, transport=fixture_transport([page])).fetch_page(
            {"operation": "release", "parameters": {}, "limit": 80}, cursor=None)
    assert html.value.code == "schema_drift" and "not a PDF" in str(html.value)


def test_without_pymupdf_no_figure_is_read(monkeypatch):
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name in {"pymupdf", "fitz"}:
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(WealthFormatError) as missing:
        pdf_lines(b"%PDF-1.7 fixture", max_pages=5)
    assert missing.value.code == "dependency_missing"
    adapter = WealthAdapter(h.source("hfcs"), transport=fixture_transport(h.pages("hfcs")))
    with pytest.raises(SourcePackError) as failed:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 80}, cursor=None)
    assert failed.value.code == "source_unavailable" and "PyMuPDF" in str(failed.value)


@pytest.mark.parametrize(("token", "decimal", "thousands", "expected"), [
    ("106.600", ",", ".", "106600"),
    ("1.234,5", ",", ".", "1234.5"),
    ("56,3", ",", ".", "56.3"),
    ("52.5%", ".", None, "52.5"),
    ("-1.5", ".", None, "-1.5"),
    ("1,234.5", ".", ",", "1234.5"),
    ("1.23", ",", ".", None),     # a thousands group of two digits is not this notation
    ("12,3", ".", None, None),    # a comma where none is declared
    ("1.2.3", ".", None, None),
    ("(1.1)", ".", None, None),   # standard errors are not cells
    ("abc", ".", None, None),
])
def test_cells_are_read_only_in_the_declared_notation(token, decimal, thousands, expected):
    cell = read_cell(token, {"decimal": decimal, "thousands": thousands})
    assert (cell["value"] if cell else None) == expected


@pytest.mark.parametrize(("mutation", "message"), [
    (lambda d: d.update(pdf_path="report.html"), ".pdf path"),
    (lambda d: d.update(pdf_path="../secret.pdf"), ".pdf path"),
    (lambda d: d["tables"][0].update(read_columns=["DE", "PT"]), "read_columns"),
    (lambda d: d["tables"][0].update(columns=["DE", "DE"]), "each printed column once"),
    (lambda d: d["tables"][0].update(period="wave 4"), "survey years"),
    (lambda d: d["tables"][0].update(markers={"-": "zero"}), "markers map"),
    (lambda d: d["tables"][0].update(decimal=",", thousands=","), "thousands separator"),
    (lambda d: d["tables"][0].update(max_pages=9), "1-3 pages"),
    (lambda d: d["tables"][0]["rows"].append(dict(d["tables"][0]["rows"][0])), "distinct labels"),
    (lambda d: d["tables"].extend([d["tables"][0]] * 6), "declare 1-6 tables"),
])
def test_pdf_declarations_are_bounded_and_explicit(mutation, message):
    document = hfcs_document()
    mutation(document)
    with pytest.raises(WealthFormatError) as invalid:
        check_pdf_document("ecb-hfcs-statistical-tables", document)
    assert message in str(invalid.value)


def test_the_pack_declares_both_pdf_sources_unverified_live_with_cited_scope_notes():
    pack = h.manifest()
    sources = {s["source_id"]: s for s in pack["sources"] if s["source_id"] in PDF_PROVIDERS}
    assert set(sources) == set(PDF_PROVIDERS)
    for source in sources.values():
        assert source["wealth"]["live_verification"] == "unverified-live"
        document = source["wealth"]["documents"][0]
        assert document["frequency"] == "irregular" and document["scope_notes"]
        assert all(note["cites"] for note in document["scope_notes"])
    assert json.dumps(sources)  # serialisable declarations

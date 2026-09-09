"""
Coverage-focused tests for src/ingestion/connectors/upload/parsers.py.

Covers text/markdown/html/docx/pdf/email extraction across both the
optional-library paths and the stdlib fallbacks. Optional libs that are
present are stubbed via sys.modules so their branches are exercised
deterministically; absent libs exercise the fallback branches naturally.
"""

import io
import sys
import types
import zipfile

import pytest

from src.ingestion.connectors.upload import parsers


# --------------------------------------------------------------------------- #
# Dispatch + text + error handling
# --------------------------------------------------------------------------- #

def test_extract_text_dispatches_to_text_by_default():
    text, meta = parsers.extract_text(b"hello world", "unknown-format")
    assert text == "hello world"
    assert meta["extractor"] == "raw"


def test_extract_text_wraps_parser_exceptions():
    # Force _parse_text to raise so the except branch (lines 42-44) runs.
    boom = parsers._parse_text

    def raiser(_content):
        raise ValueError("kaboom")

    parsers._parse_text = raiser
    try:
        text, meta = parsers.extract_text(b"x", "text")
    finally:
        parsers._parse_text = boom
    assert text == ""
    assert meta["extractor"] == "error"
    assert "kaboom" in meta["error"]
    assert meta["format"] == "text"


def test_parse_text_strips_and_reports_encoding():
    text, meta = parsers.extract_text(b"  padded  ", "text")
    assert text == "padded"
    assert meta["encoding"] == "utf-8"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def test_markdown_heuristic_fallback_when_lib_absent(monkeypatch):
    # Exercise the fallback deterministically even when another optional extra
    # installed Markdown into the developer environment.
    import builtins

    original_import = builtins.__import__

    def without_markdown(name, *args, **kwargs):
        if name == "markdown":
            raise ImportError("markdown intentionally unavailable for fallback test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_markdown)
    md = (
        "# Big Title\n\n"
        "Some **bold** and `code` and a [link](http://x) plus ![img](http://y).\n"
        "```\nfenced code\n```\n"
    )
    text, meta = parsers.extract_text(md.encode(), "markdown")
    assert meta["extractor"] == "heuristic"
    assert meta["title"] == "Big Title"
    assert "bold" in text and "link" in text
    assert "fenced code" not in text  # code block stripped
    assert "img" not in text          # image removed


def test_markdown_uses_library_when_available(monkeypatch):
    stub = types.ModuleType("markdown")
    stub.markdown = lambda raw: "<p>converted &amp; text</p>"
    monkeypatch.setitem(sys.modules, "markdown", stub)
    text, meta = parsers.extract_text(b"# Heading\nbody", "markdown")
    assert meta["extractor"] == "markdown-lib"
    assert meta["title"] == "Heading"
    assert "converted" in text


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def test_html_with_bs4_extracts_title_and_description():
    html = (
        b"<html><head><title>Doc Title</title>"
        b'<meta name="description" content="a summary">'
        b"</head><body><script>bad()</script>"
        b"<p>Visible paragraph text.</p></body></html>"
    )
    text, meta = parsers.extract_text(html, "html")
    assert meta["extractor"] == "bs4+lxml"
    assert meta["title"] == "Doc Title"
    assert meta["description"] == "a summary"
    assert "Visible paragraph text." in text
    assert "bad()" not in text  # script decomposed


def test_html_regex_fallback_when_bs4_absent(monkeypatch):
    # Block bs4 import -> regex-strip fallback (lines 114-119).
    monkeypatch.setitem(sys.modules, "bs4", None)
    html = b"<html><body><p>Plain fallback text</p></body></html>"
    text, meta = parsers.extract_text(html, "html")
    assert meta["extractor"] == "regex-strip"
    assert "Plain fallback text" in text
    assert "<" not in text


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #

def test_docx_python_docx_path(monkeypatch):
    class _Para:
        def __init__(self, t):
            self.text = t

    class _Core:
        title = "Docx Title"
        author = "The Author"

    class _Doc:
        paragraphs = [_Para("First line"), _Para("  "), _Para("Second line")]
        core_properties = _Core()

    stub = types.ModuleType("docx")
    stub.Document = lambda _bio: _Doc()
    monkeypatch.setitem(sys.modules, "docx", stub)

    text, meta = parsers.extract_text(b"anything", "docx")
    assert meta["extractor"] == "python-docx"
    assert meta["title"] == "Docx Title"
    assert meta["author"] == "The Author"
    assert "First line" in text and "Second line" in text
    assert "  " not in text.split("\n")  # blank paragraph skipped


def test_docx_stdlib_xml_fallback():
    # python-docx absent -> unzip word/document.xml stdlib fallback (144-151).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            b"<w:document><w:p><w:t>Body from xml</w:t></w:p></w:document>",
        )
    text, meta = parsers.extract_text(buf.getvalue(), "docx")
    assert meta["extractor"] == "stdlib-xml"
    assert "Body from xml" in text


def test_docx_no_parser_available_returns_error():
    # Not a valid zip and no python-docx -> final error branch (line 155).
    text, meta = parsers.extract_text(b"not-a-zip and not docx", "docx")
    assert text == ""
    assert meta["extractor"] == "none"


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def test_pdf_pymupdf_path(monkeypatch):
    class _Page:
        def __init__(self, t):
            self._t = t

        def get_text(self):
            return self._t

    class _FakeDoc:
        def __init__(self):
            self._pages = [_Page("page one"), _Page("page two")]

        def __iter__(self):
            return iter(self._pages)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    stub = types.ModuleType("fitz")
    stub.open = lambda **kw: _FakeDoc()
    monkeypatch.setitem(sys.modules, "fitz", stub)

    text, meta = parsers.extract_text(b"%PDF-fake", "pdf")
    assert meta["extractor"] == "pymupdf"
    assert meta["page_count"] == 2
    assert "page one" in text and "page two" in text


def test_pdf_pdfminer_fallback(monkeypatch):
    # Make fitz.open raise so we fall through to pdfminer (174-177).
    fitz_stub = types.ModuleType("fitz")

    def _raise(**kw):
        raise RuntimeError("no mupdf")

    fitz_stub.open = _raise
    monkeypatch.setitem(sys.modules, "fitz", fitz_stub)

    high_level = types.ModuleType("pdfminer.high_level")
    high_level.extract_text = lambda _bio: "  pdfminer extracted  "
    pdfminer_pkg = types.ModuleType("pdfminer")
    pdfminer_pkg.high_level = high_level
    monkeypatch.setitem(sys.modules, "pdfminer", pdfminer_pkg)
    monkeypatch.setitem(sys.modules, "pdfminer.high_level", high_level)

    text, meta = parsers.extract_text(b"%PDF-fake", "pdf")
    assert meta["extractor"] == "pdfminer"
    assert text == "pdfminer extracted"


def test_pdf_no_parser_available(monkeypatch):
    fitz_stub = types.ModuleType("fitz")
    fitz_stub.open = lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))
    monkeypatch.setitem(sys.modules, "fitz", fitz_stub)
    monkeypatch.setitem(sys.modules, "pdfminer", None)
    monkeypatch.setitem(sys.modules, "pdfminer.high_level", None)

    text, meta = parsers.extract_text(b"garbage", "pdf")
    assert text == ""
    assert meta["extractor"] == "none"


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def test_decode_header_value_handles_none_and_encoded():
    assert parsers._decode_header_value(None) == ""
    # RFC 2047 encoded-word (UTF-8 base64 for "Héllo").
    encoded = "=?utf-8?b?SMOpbGxv?="
    assert parsers._decode_header_value(encoded) == "Héllo"


def test_email_plain_text_single_part():
    raw = (
        b"Subject: Hello There\r\n"
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"This is the plain body.\r\n"
    )
    text, meta = parsers.extract_text(raw, "email")
    assert meta["extractor"] == "stdlib-email"
    assert meta["subject"] == "Hello There"
    assert meta["from"] == "alice@example.com"
    assert "This is the plain body." in text
    assert "Subject: Hello There" in text


def test_email_multipart_prefers_plain_over_html():
    raw = (
        b"Subject: Multi\r\n"
        b"From: a@b.com\r\n"
        b'Content-Type: multipart/alternative; boundary="BND"\r\n'
        b"\r\n"
        b"--BND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"plain part wins\r\n"
        b"--BND\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>html part</p>\r\n"
        b"--BND--\r\n"
    )
    text, meta = parsers.extract_text(raw, "email")
    assert "plain part wins" in text
    assert "html part" not in text


def test_email_html_only_gets_tags_stripped():
    # Only text/html present -> body_html strip branch (241-245).
    raw = (
        b"Subject: HtmlOnly\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>only html body</p></body></html>\r\n"
        b"--B\r\n"
        b'Content-Type: application/octet-stream\r\n'
        b'Content-Disposition: attachment; filename="a.bin"\r\n\r\n'
        b"BINARYDATA\r\n"
        b"--B--\r\n"
    )
    text, meta = parsers.extract_text(raw, "email")
    assert "only html body" in text
    assert "<p>" not in text
    assert "BINARYDATA" not in text  # attachment skipped

"""
Per-format text extraction for the upload connector.

Each parser returns ``(text, metadata)`` where ``text`` is plain UTF-8 text
and ``metadata`` is a dict of format-specific fields (title, subject, authors,
extractor used, …). All parsers degrade gracefully when optional libraries are
absent.

Supported formats: pdf, docx, markdown, html, email, text
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, Any, Optional, Tuple

from src.ingestion.connectors.upload.detectors import detect_encoding


ParseResult = Tuple[str, Dict[str, Any]]


# --------------------------------------------------------------------------- #
# Public dispatch
# --------------------------------------------------------------------------- #

def extract_text(content: bytes, fmt: str, *, backend: str = "native") -> ParseResult:
    """Dispatch to the right parser and return (text, metadata)."""
    if backend == "markitdown":
        from src.integrations.documents import markitdown
        return markitdown(content, fmt)
    if backend != "native":
        raise ValueError("unknown document parser backend")

    dispatch = {
        "pdf":      _parse_pdf,
        "docx":     _parse_docx,
        "markdown": _parse_markdown,
        "html":     _parse_html,
        "email":    _parse_email,
        "text":     _parse_text,
    }
    parser = dispatch.get(fmt, _parse_text)
    try:
        return parser(content)
    except Exception as exc:
        # Never let a parser crash the connector — return what we have.
        return ("", {"extractor": "error", "error": str(exc), "format": fmt})


# --------------------------------------------------------------------------- #
# Plain text
# --------------------------------------------------------------------------- #

def _parse_text(content: bytes) -> ParseResult:
    enc = detect_encoding(content)
    text = content.decode(enc, errors="replace").strip()
    return (text, {"extractor": "raw", "encoding": enc})


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def _parse_markdown(content: bytes) -> ParseResult:
    enc = detect_encoding(content)
    raw = content.decode(enc, errors="replace")

    # Extract a title from the first ATX heading.
    title = ""
    for line in raw.splitlines():
        m = re.match(r"^#+\s+(.+)", line)
        if m:
            title = m.group(1).strip()
            break

    # Convert to plain text: try markdown library, fall back to stripping markup.
    try:
        import markdown as md_lib  # type: ignore
        html = md_lib.markdown(raw)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        extractor = "markdown-lib"
    except ImportError:
        # Strip markdown syntax heuristically.
        text = re.sub(r"```.*?```", " ", raw, flags=re.DOTALL)  # code blocks
        text = re.sub(r"`[^`]+`", " ", text)                     # inline code
        text = re.sub(r"!\[.*?\]\(.*?\)", " ", text)             # images
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)    # links → text
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # headings
        text = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", text)  # bold/italic
        text = re.sub(r"\s+", " ", text).strip()
        extractor = "heuristic"

    return (text, {"extractor": extractor, "title": title, "original_format": "markdown"})


# --------------------------------------------------------------------------- #
# HTML / XML
# --------------------------------------------------------------------------- #

def _parse_html(content: bytes) -> ParseResult:
    enc = detect_encoding(content)
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(content, "lxml")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "aside"]):
            tag.decompose()
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        desc = ""
        meta_desc = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if meta_desc and meta_desc.get("content"):
            desc = str(meta_desc["content"]).strip()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return (text, {"extractor": "bs4+lxml", "title": title, "description": desc})
    except ImportError:
        # Fallback: strip tags with regex.
        raw = content.decode(enc, errors="replace")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return (text, {"extractor": "regex-strip"})


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #

def _parse_docx(content: bytes) -> ParseResult:
    # Try python-docx first.
    try:
        import docx as python_docx  # type: ignore
        doc = python_docx.Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        core = doc.core_properties
        return (
            "\n".join(paragraphs),
            {
                "extractor": "python-docx",
                "title": core.title or "",
                "author": core.author or "",
            },
        )
    except (ImportError, Exception):
        pass

    # Stdlib fallback: unzip and read word/document.xml.
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            xml_bytes = zf.read("word/document.xml")
        # Strip XML tags — the namespace prefix varies, so just strip all tags.
        text = re.sub(r"<[^>]+>", " ", xml_bytes.decode("utf-8", errors="replace"))
        text = re.sub(r"\s+", " ", text).strip()
        return (text, {"extractor": "stdlib-xml"})
    except Exception:
        pass

    return ("", {"extractor": "none", "error": "no DOCX parser available"})


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _parse_pdf(content: bytes) -> ParseResult:
    # Try PyMuPDF.
    try:
        import fitz  # type: ignore
        with fitz.open(stream=content, filetype="pdf") as doc:
            pages = [page.get_text() for page in doc]
        text = "\n".join(pages).strip()
        return (text, {"extractor": "pymupdf", "page_count": len(pages)})
    except (ImportError, Exception):
        pass

    # Try pdfminer.
    try:
        from pdfminer.high_level import extract_text as pdf_extract  # type: ignore
        text = pdf_extract(io.BytesIO(content)).strip()
        return (text, {"extractor": "pdfminer"})
    except (ImportError, Exception):
        pass

    return ("", {"extractor": "none", "error": "install PyMuPDF or pdfminer.six for PDF support"})


# --------------------------------------------------------------------------- #
# Email (.eml / RFC 2822)
# --------------------------------------------------------------------------- #

def _decode_header_value(raw: Optional[str]) -> str:
    """Decode RFC 2047 encoded header value to a plain string."""
    if not raw:
        return ""
    import email.header as hdr
    parts = hdr.decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded).strip()


def _parse_email(content: bytes) -> ParseResult:
    import email as email_lib

    msg = email_lib.message_from_bytes(content)

    subject = _decode_header_value(msg.get("Subject"))
    from_   = _decode_header_value(msg.get("From"))
    to_     = _decode_header_value(msg.get("To"))
    date_   = msg.get("Date", "")

    # Extract body: prefer text/plain, fall back to text/html.
    body_plain = ""
    body_html  = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if "attachment" in cdisp:
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not body_plain:
                body_plain = decoded
            elif ct == "text/html" and not body_html:
                body_html = decoded
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            body_plain = payload.decode(charset, errors="replace")

    # Prefer plain; strip HTML if we only have HTML.
    if body_plain:
        body = body_plain.strip()
    elif body_html:
        body = re.sub(r"<[^>]+>", " ", body_html)
        body = re.sub(r"\s+", " ", body).strip()
    else:
        body = ""

    header_block = f"Subject: {subject}\nFrom: {from_}\nTo: {to_}\nDate: {date_}"
    text = f"{header_block}\n\n{body}".strip()

    return (
        text,
        {
            "extractor": "stdlib-email",
            "subject": subject,
            "from": from_,
            "to": to_,
            "date": date_,
        },
    )

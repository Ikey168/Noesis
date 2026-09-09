"""Optional immutable report exports with structured citations and safe AST input."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from src.kb.authored_reports import ReportError

ROOT = Path(__file__).resolve().parents[2]
STYLE = ROOT / "config/citation_styles/noesis-author-date-v1.csl"
WORKER = Path(__file__).with_name("citeproc_worker.py")
PACKAGE_PIN = "1.17"
PANDOC_PIN = "3.9"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _text(value):
    # Text enters the AST as literal text, never Markdown, HTML or Typst code.
    return [{"t": "Str", "c": str(value)}]


def _para(value):
    return {"t": "Para", "c": _text(value)}


def _heading(value):
    return {"t": "Header", "c": [2, ["", [], []], _text(value)]}


def _metadata(content, supplied):
    entries = {b["id"]: b for b in content["bibliography"]}
    if not isinstance(supplied, dict) or not set(supplied) <= set(entries):
        raise ReportError(
            "invalid_citations", "citation metadata requires existing bibliography IDs"
        )
    references, mapping, fallback = [], {}, []
    allowed = {
        "type",
        "title",
        "author",
        "issued",
        "container-title",
        "publisher",
        "DOI",
        "URL",
        "volume",
        "issue",
        "page",
    }
    for identity in entries:
        item = supplied.get(identity, {})
        if (
            not isinstance(item, dict)
            or not set(item) <= allowed
            or len(json.dumps(item)) > 20000
        ):
            raise ReportError(
                "invalid_citations", "unsupported or oversized citation metadata"
            )
        if any(character in json.dumps(item) for character in ("<", ">", "\\u0000")):
            raise ReportError(
                "invalid_citations", "CSL fields must be plain text without markup"
            )
        for key in set(item) - {"author", "issued"}:
            if not isinstance(item[key], str) or len(item[key]) > 4000:
                raise ReportError(
                    "invalid_citations", "citation fields must be bounded strings"
                )
        authors = item.get("author", [])
        if (
            not isinstance(authors, list)
            or len(authors) > 100
            or any(
                not isinstance(a, dict)
                or not set(a) <= {"family", "given", "literal"}
                or any(not isinstance(v, str) or len(v) > 1000 for v in a.values())
                for a in authors
            )
        ):
            raise ReportError("invalid_citations", "invalid CSL authors")
        issued = item.get("issued", {})
        if not isinstance(issued, dict) or not set(issued) <= {"date-parts"}:
            raise ReportError("invalid_citations", "invalid CSL date")
        dates = issued.get("date-parts", [])
        if (
            not isinstance(dates, list)
            or len(dates) > 1
            or any(
                not isinstance(d, list)
                or not 1 <= len(d) <= 3
                or any(type(n) is not int for n in d)
                or not 1 <= d[0] <= 9999
                or len(d) > 1
                and not 1 <= d[1] <= 12
                or len(d) > 2
                and not 1 <= d[2] <= 31
                for d in dates
            )
        ):
            raise ReportError("invalid_citations", "invalid CSL date parts")
        if (
            not item.get("title")
            or not dates
            or not any(a.get("family") or a.get("literal") for a in authors)
        ):
            fallback.append(identity)
            continue
        key = "ref" + _hash(identity)[:24]
        mapping[identity] = key
        references.append(
            {**item, "id": key, "type": item.get("type", "article-journal")}
        )
    return references, mapping, fallback


def build_ast(report, citation_metadata, locators, locale, api_version):
    content = report["content"]
    references, mapping, fallback = _metadata(content, citation_metadata)
    assertions = {a["id"]: a for s in content["sections"] for a in s["assertions"]}
    if not isinstance(locators, dict) or not set(locators) <= set(assertions):
        raise ReportError(
            "invalid_citations", "locators require existing assertion IDs"
        )
    for identity, values in locators.items():
        if (
            not isinstance(values, dict)
            or not set(values) <= set(assertions[identity]["citations"])
            or any(not isinstance(v, str) or len(v) > 200 for v in values.values())
        ):
            raise ReportError(
                "invalid_citations", "locators must name an assertion's citations"
            )
    blocks = [
        _heading(content["title"]),
        _para(f"Report {report['report_id']}, revision {report['revision']}"),
        _para(
            "Source links and formatted citations do not independently verify evidence support."
        ),
    ]
    for section in content["sections"]:
        blocks.append(_heading(section["title"]))
        for assertion in section["assertions"]:
            label = (
                "Author commentary"
                if assertion["kind"] == "commentary"
                else "Source-linked; support not independently verified"
            )
            inline = _text(f"[{assertion['id']}; {label}] {assertion['text']}")
            for identity in assertion["citations"]:
                inline.append({"t": "Space"})
                locator = locators.get(assertion["id"], {}).get(identity, "")
                if identity in mapping:
                    inline.append(
                        {
                            "t": "Cite",
                            "c": [
                                [
                                    {
                                        "citationId": mapping[identity],
                                        "citationPrefix": [],
                                        "citationSuffix": _text(locator)
                                        if locator
                                        else [],
                                        "citationMode": {"t": "NormalCitation"},
                                        "citationNoteNum": 0,
                                        "citationHash": 0,
                                    }
                                ],
                                _text("[" + identity + "]"),
                            ],
                        }
                    )
                else:
                    inline.extend(
                        _text(
                            f"[{identity}" + (", " + locator if locator else "") + "]"
                        )
                    )
            if assertion["dependencies"]:
                inline.append(
                    {
                        "t": "Note",
                        "c": [
                            _para(
                                "Source dependencies for "
                                + assertion["id"]
                                + ": "
                                + json.dumps(
                                    assertion["dependencies"],
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                        ],
                    }
                )
            blocks.append({"t": "Para", "c": inline})
    blocks.append(
        _heading(
            "Bekannte Einschränkungen" if locale == "de-DE" else "Known limitations"
        )
    )
    blocks.extend(_para(value) for value in content["limitations"])
    blocks.append(_heading("Authored bibliography (stable source IDs)"))
    blocks.extend(
        _para("[" + b["id"] + "] " + b["text"]) for b in content["bibliography"]
    )
    blocks.append(
        _heading(
            "Literaturverzeichnis" if locale == "de-DE" else "Formatted bibliography"
        )
    )
    blocks.append({"t": "Div", "c": [["refs", [], []], []]})
    return (
        {
            "pandoc-api-version": api_version,
            "meta": {"lang": {"t": "MetaString", "c": locale}},
            "blocks": blocks,
        },
        references,
        fallback,
    )


def _run(mode, args, cwd, *, timeout=30):
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "SOURCE_DATE_EPOCH": "0",
        "RAYON_NUM_THREADS": "1",
    }
    with (cwd / "renderer.log").open("wb") as log:
        try:
            result = subprocess.run(
                [sys.executable, str(WORKER), mode, *args],
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=log,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReportError(
                "renderer_timeout", "report renderer exceeded wall-clock limit"
            ) from exc
    if result.returncode:
        raise ReportError(
            "renderer_failed", (cwd / "renderer.log").read_text(errors="replace")[:1000]
        )


def render_report(
    store,
    namespace,
    report_id,
    *,
    principal_id,
    scopes,
    citation_metadata=None,
    locators=None,
    locale="en-US",
    output_format="docx",
    revision=None,
):
    package = store.export(
        namespace,
        report_id,
        principal_id=principal_id,
        scopes=scopes,
        revision=revision,
    )
    if locale not in {"de-DE", "en-US"} or output_format not in {"docx", "pdf"}:
        raise ReportError(
            "unsupported_export", "supported formats: docx/pdf; locales: de-DE/en-US"
        )
    supplied, locators = citation_metadata or {}, locators or {}
    config = {
        "report_hash": package["sha256"],
        "citation_metadata": supplied,
        "locators": locators,
        "locale": locale,
        "format": output_format,
        "pypandoc_binary": PACKAGE_PIN,
        "pandoc": PANDOC_PIN,
        "typst": "0.15.0" if output_format == "pdf" else None,
        "style_sha256": hashlib.sha256(STYLE.read_bytes()).hexdigest(),
    }
    if len(json.dumps([package, config])) > 2_000_000:
        raise ReportError("export_too_large", "report and metadata exceed 2 MB limit")
    key = "citeproc-export:" + _hash(config)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS report_citeproc_exports(export_id TEXT PRIMARY KEY, receipt TEXT, payload BLOB)"
    )
    existing = store.conn.execute(
        "SELECT receipt,payload FROM report_citeproc_exports WHERE export_id=?", [key]
    ).fetchone()
    if existing:
        receipt = json.loads(existing[0])
        if hashlib.sha256(bytes(existing[1])).hexdigest() != receipt["output_sha256"]:
            raise ReportError("invalid_export", "stored formatted export hash mismatch")
        return {
            "receipt": receipt,
            "content": bytes(existing[1]),
            "markdown": package["markdown"],
        }
    import pypandoc

    if version("pypandoc_binary") != PACKAGE_PIN:
        raise ReportError("unsupported_renderer", "pinned pypandoc_binary required")
    binary = str(Path(pypandoc.__file__).parent / "files" / "pandoc")
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="noesis-citeproc-") as directory:
        root = Path(directory)
        _run("pandoc", [binary, "--version"], root)
        if (root / "renderer.log").read_text().splitlines()[
            0
        ] != "pandoc " + PANDOC_PIN:
            raise ReportError("unsupported_renderer", "pinned Pandoc binary required")
        _run(
            "pandoc",
            [
                binary,
                "--from=markdown",
                "--to=json",
                "--output=empty.json",
                "/dev/null",
            ],
            root,
        )
        api = json.loads((root / "empty.json").read_text())["pandoc-api-version"]
        ast, refs, fallback = build_ast(
            package["report"], supplied, locators, locale, api
        )
        (root / "input.json").write_text(json.dumps(ast))
        (root / "references.json").write_text(json.dumps(refs))
        (root / "style.csl").write_bytes(STYLE.read_bytes())
        target = "docx" if output_format == "docx" else "typst"
        filename = "output.docx" if target == "docx" else "output.typ"
        _run(
            "pandoc",
            [
                binary,
                "--sandbox",
                "--standalone",
                "--from=json",
                "--to=" + target,
                "--citeproc",
                "--csl=style.csl",
                "--bibliography=references.json",
                "--output=" + filename,
                "input.json",
            ],
            root,
        )
        if output_format == "pdf":
            _run("pdf", [], root)
            filename = "output.pdf"
        data = (root / filename).read_bytes()
        if len(data) > 20_000_000:
            raise ReportError("export_too_large", "rendered report exceeds 20 MB")
    receipt = {
        "contract": "noesis-citeproc-export-v1",
        "export_id": key,
        "config": config,
        "pandoc_version": PANDOC_PIN,
        "output_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
        "authored_fallback_ids": fallback,
        "source_package": package,
        "support_verified": False,
    }
    store.conn.execute(
        "INSERT INTO report_citeproc_exports VALUES(?,?,?)",
        [key, json.dumps(receipt), data],
    )
    return {"receipt": receipt, "content": data, "markdown": package["markdown"]}

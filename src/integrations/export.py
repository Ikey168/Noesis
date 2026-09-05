"""Optional portable report/package exports retaining native evidence records."""

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from .common import IntegrationError, version


def render_report(
    exported, *, output_format="docx", references=(), locale="de-DE", csl_path=None
):
    if output_format not in {"docx", "html"}:
        raise IntegrationError(
            "unsupported_format",
            "Supported bounded renderers: docx, html; PDF engine evaluation pending",
        )
    if locale not in {"de-DE", "en-GB", "en-US"}:
        raise IntegrationError("unsupported_locale", "Unsupported citation locale")
    content = exported["report"]["content"]
    known = {b["id"] for b in content["bibliography"]}
    references = list(references)
    ids = [r.get("id") for r in references]
    if len(ids) != len(set(ids)) or any(i not in known for i in ids):
        raise IntegrationError(
            "invalid_citation", "References must have unique existing bibliography IDs"
        )
    if any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", i) for i in ids):
        raise IntegrationError(
            "invalid_citation", "CSL rendering requires simple stable citation IDs"
        )

    # Construct a Pandoc JSON AST so authored text cannot inject Markdown/raw HTML.
    def inline(text):
        return {"t": "Str", "c": str(text)}

    def para(text):
        return {"t": "Para", "c": [inline(text)]}

    def heading(level, text):
        return {"t": "Header", "c": [level, ["", [], []], [inline(text)]]}

    blocks = [heading(1, content["title"])]
    for section in content["sections"]:
        blocks.append(heading(2, section["title"]))
        for assertion in section["assertions"]:
            items = [inline(assertion["text"])]
            for cid in assertion["citations"]:
                items.append({"t": "Space"})
                if cid in ids:
                    items.append(
                        {
                            "t": "Cite",
                            "c": [
                                [
                                    {
                                        "citationId": cid,
                                        "citationPrefix": [],
                                        "citationSuffix": [],
                                        "citationMode": {"t": "NormalCitation"},
                                        "citationNoteNum": 0,
                                        "citationHash": 0,
                                    }
                                ],
                                [inline("[@" + cid + "]")],
                            ],
                        }
                    )
                else:
                    items.append(inline("[" + cid + "]"))
            blocks.append({"t": "Para", "c": items})
    blocks.extend(
        [heading(2, "Limitations"), *[para(x) for x in content["limitations"]]]
    )
    unmatched = [x for x in content["bibliography"] if x["id"] not in ids]
    if unmatched:
        blocks.extend(
            [
                heading(2, "Authored bibliography entries"),
                *[para("[" + x["id"] + "] " + x["text"]) for x in unmatched],
            ]
        )
    binary = shutil.which("pandoc")
    if not binary:
        try:
            import pypandoc

            binary = pypandoc.get_pandoc_path()
        except (ImportError, OSError) as exc:
            raise IntegrationError("backend_unavailable", "Install pandoc") from exc
    pandoc_version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True, timeout=5
    ).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="noesis-report-") as directory:
        root = Path(directory)
        refs = root / "references.json"
        refs.write_text(json.dumps(references))
        # Query the installed reader's AST version to avoid an assumed Pandoc API version.
        empty = json.loads(
            subprocess.run(
                [binary, "-f", "markdown", "-t", "json"],
                input="",
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            ).stdout
        )
        ast = {
            "pandoc-api-version": empty["pandoc-api-version"],
            "meta": {},
            "blocks": blocks,
        }
        ast_path = root / "report.json"
        ast_path.write_text(json.dumps(ast, ensure_ascii=False))
        target = root / ("report." + output_format)
        command = [
            binary,
            "--sandbox",
            "--from=json",
            str(ast_path),
            "--to=" + output_format,
            "--standalone",
            "--citeproc",
            "--bibliography=" + str(refs),
            "--metadata=lang:" + locale,
            "--output=" + str(target),
        ]
        style_hash = None
        if csl_path:
            style = Path(csl_path).read_bytes()
            if len(style) > 1_000_000:
                raise IntegrationError("input_limit", "CSL style too large")
            local = root / "style.csl"
            local.write_bytes(style)
            command += ["--csl=" + str(local)]
            style_hash = hashlib.sha256(style).hexdigest()
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise IntegrationError(
                "render_failed", "Pandoc failed or exceeded its deadline"
            ) from exc
        if target.stat().st_size > 32_000_000:
            raise IntegrationError("output_limit", "Rendered report too large")
        data = target.read_bytes()
    return {
        "format": output_format,
        "bytes_b64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "producer": pandoc_version,
        "locale": locale,
        "csl_sha256": style_hash,
        "native_report_sha256": exported["sha256"],
        "citation_ids": sorted(known),
        "support_verified": False,
    }


def export_rocrate(package):
    from rocrate.rocrate import ROCrate

    if package.get("status") not in {"complete", "partial"}:
        raise IntegrationError(
            "invalid_package", "Only built native packages can be exported"
        )
    raw = json.dumps(
        package, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode()
    if len(raw) > 32_000_000:
        raise IntegrationError("input_limit", "Native package exceeds export limit")
    crate = ROCrate()
    crate.name = "Noesis research package " + package["package_id"]
    crate.add_file(
        io.BytesIO(raw),
        dest_path="native-package.json",
        properties={
            "name": "Native Noesis package with original provenance and verification metadata",
            "encodingFormat": "application/json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    crate.root_dataset["description"] = (
        "Interoperable envelope. Native access, signature and replay semantics remain in native-package.json."
    )
    with tempfile.TemporaryDirectory(prefix="noesis-rocrate-") as directory:
        path = Path(directory) / "package.zip"
        crate.write_zip(str(path))
        data = path.read_bytes()
    return {
        "format": "ro-crate",
        "bytes_b64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "native_content_hash": package["content_hash"],
        "producer": {"backend": "rocrate", "version": version("rocrate")},
        "limitations": [
            "Envelope export only; native components are retained in the native manifest.",
            "ZIP timestamps may differ; native content hash remains the reproducibility identity.",
            "RO-Crate metadata does not grant access or verify native signatures.",
        ],
    }

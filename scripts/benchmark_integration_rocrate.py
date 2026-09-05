"""Native Berlin-themed authored package -> RO-Crate -> independent validation."""

import argparse
import base64
import json
import time
from pathlib import Path

import duckdb

from src.integrations.common import version
from src.kb.research_packages import WRITE_SCOPE, ResearchPackageStore

PUBLICATION = {
    "datePublished": "2026-09-05",
    "license": "https://creativecommons.org/publicdomain/zero/1.0/",
    "name": "Authored Berlin research export contract fixture",
}


def example(*, partial=False):
    conn = duckdb.connect(":memory:")
    store = ResearchPackageStore(conn, now=lambda: 100)
    manifest = {
        "format_version": "1.0",
        "question": "Authored Berlin funding example",
        "plan": {},
        "snapshot": {},
        "evidence": ["report:berlin"],
        "transformations": [],
        "findings": [],
        "limitations": ["Authored contract fixture"],
        "policies": {},
        "compatibility": {},
    }
    created = store.create_manifest(
        "research", manifest, principal_id="fixture", scopes={WRITE_SCOPE}
    )

    def register(kind, identity, content, **kwargs):
        store.register_component(
            "research",
            kind,
            identity,
            content,
            principal_id="fixture",
            scopes={WRITE_SCOPE},
            **kwargs,
        )

    register(
        "dataset",
        "data:berlin",
        {"columns": ["year", "value"], "rows": [[2023, 7]]},
        metadata={
            "title": "Authored Berlin data",
            "revision_id": "data:r1",
            "version": "1",
        },
    )
    register(
        "model",
        "model:pinned",
        {"model": "example-only", "revision": "test-revision"},
        metadata={"version": "test-revision"},
    )
    register(
        "asset",
        "analysis:1",
        {"mean": 7},
        dependencies=["data:berlin", "model:pinned"],
        metadata={
            "artifact_kind": "analysis",
            "software": [{"name": "Noesis fixture calculator", "version": "1.0"}],
        },
    )
    dependencies = ["analysis:1"]
    if partial:
        register(
            "document",
            "restricted:1",
            {"text": "SECRET-DO-NOT-EXPORT"},
            access_status="inaccessible",
        )
        dependencies.extend(["restricted:1", "missing:1"])
        register(
            "document",
            "redacted:1",
            {"text": "ORIGINAL-DO-NOT-EXPORT"},
            access_status="redacted",
            redacted_content={"text": "Public summary"},
        )
        dependencies.append("redacted:1")
    register(
        "document",
        "report:berlin",
        {"title": "Authored funding report", "text": "Illustrative data only."},
        dependencies=dependencies,
        metadata={"artifact_kind": "report", "revision_id": "report:r2"},
    )
    package = store.build(
        "research",
        created["package_id"],
        ["report:berlin"],
        principal_id="fixture",
        scopes={WRITE_SCOPE},
        allow_partial=partial,
    )
    return store, package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--offline-validator", action="store_true")
    args = parser.parse_args()
    from src.integrations.export import export_rocrate

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cases = []
    for partial in (False, True):
        store, package = example(partial=partial)
        started = time.perf_counter()
        result = export_rocrate(package, metadata=PUBLICATION)
        elapsed = (time.perf_counter() - started) * 1000
        data = base64.b64decode(result["bytes_b64"])
        path = out.with_name(
            out.stem + ("-partial.zip" if partial else "-complete.zip")
        )
        path.write_bytes(data)
        replay = export_rocrate(package, metadata=PUBLICATION)
        case = {
            "partial": partial,
            "native_verification": store.verify(package),
            "export_ms": elapsed,
            "bytes": len(data),
            "sha256": result["sha256"],
            "replay_byte_identical": replay["bytes_b64"] == result["bytes_b64"],
        }
        if args.validate:
            from rocrate_validator import models, services

            started = time.perf_counter()
            validated = services.validate(
                services.ValidationSettings(
                    rocrate_uri=str(path.resolve()),
                    profile_identifier="ro-crate-1.1",
                    requirement_severity=models.Severity.REQUIRED,
                    offline=args.offline_validator,
                )
            )
            case["independent_validation"] = {
                "package": "roc-validator",
                "version": version("roc-validator"),
                "profile": "ro-crate-1.1",
                "severity": "REQUIRED",
                "offline": args.offline_validator,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "issues": [
                    {"check": x.check.identifier, "message": x.message}
                    for x in validated.get_issues()
                ],
            }
        cases.append(case)
        store.conn.close()
    out.write_text(
        json.dumps(
            {
                "fixture": "authored contract examples; no empirical research claims",
                "cases": cases,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(cases))


if __name__ == "__main__":
    main()

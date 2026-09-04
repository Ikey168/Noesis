"""Acceptance tests for the local-first ``noesis`` command."""

from __future__ import annotations

import json
import stat
import sys
import tomllib
from email.message import Message
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src.noesis_cli import __version__
from src.noesis_cli.app import CLIError, build_parser, main

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-cli-v1.json"
FIXTURE = REPO_ROOT / "examples/quickstart/moon-mission.md"


def _run_json(capsys, *args: str) -> tuple[int, dict]:
    code = main(list(args))
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def _init(capsys, tmp_path: Path) -> Path:
    config = tmp_path / ".noesis/config.json"
    code, output = _run_json(
        capsys,
        "--config",
        str(config),
        "init",
        "--non-interactive",
        "--json",
    )
    assert code == 0
    assert output["ok"] is True
    return config


def test_parser_exposes_the_supported_command_surface(capsys):
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert set(commands) == {
        "init",
        "doctor",
        "ingest",
        "ask",
        "brief",
        "watch",
        "watches",
        "export",
            "verify",
            "namespace",
            "serve",
    }
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"noesis {__version__}"


def test_init_is_private_idempotent_and_preserves_existing_files(capsys, tmp_path):
    config = _init(capsys, tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    domains = Path(payload["domains"])
    warehouse = Path(payload["warehouse"])
    domains.write_text(
        domains.read_text(encoding="utf-8") + "# keep me\n", encoding="utf-8"
    )

    code, second = _run_json(capsys, "--config", str(config), "init", "--json")

    assert code == 0
    assert second["data"]["created"] == []
    assert str(config) in second["data"]["preserved"]
    assert domains.read_text(encoding="utf-8").endswith("# keep me\n")
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(warehouse.stat().st_mode) == 0o600


def test_local_journey_is_retry_safe_and_exports_a_verifiable_bundle(capsys, tmp_path):
    config = _init(capsys, tmp_path)
    base = ("--config", str(config))

    code, first = _run_json(
        capsys, *base, "ingest", str(FIXTURE), "--domain", "local", "--json"
    )
    assert code == 0
    assert first["data"]["upsert"]["inserted"] == 1
    document_id = first["data"]["documents"][0]

    code, retry = _run_json(
        capsys, *base, "ingest", str(FIXTURE), "--domain", "local", "--json"
    )
    assert code == 0
    assert retry["data"]["documents"] == [document_id]
    assert retry["data"]["upsert"]["duplicate"] == 1

    code, answer = _run_json(
        capsys,
        *base,
        "ask",
        "What was the mission result?",
        "--domain",
        "local",
        "--format",
        "json",
    )
    assert code == 0
    assert answer["data"]["contract"] == "noesis-kb-v1"

    code, brief = _run_json(
        capsys,
        *base,
        "brief",
        "--domains",
        "local",
        "--budget",
        "5",
        "--format",
        "json",
    )
    assert code == 0
    assert brief["data"]["contract"] == "noesis-kb-v1"

    bundle = tmp_path / "answer.bundle.json"
    code, exported = _run_json(
        capsys,
        *base,
        "export",
        "answer",
        "--domain",
        "local",
        "--question",
        "What was the mission result?",
        "--include-private",
        "--output",
        str(bundle),
        "--json",
    )
    assert code == 0
    assert exported["data"]["bundle_contract"] == "noesis-evidence-bundle-v1"
    assert bundle.is_file()

    code, verified = _run_json(capsys, "verify", str(bundle), "--json")
    assert code == 0
    assert verified["valid"] is True
    assert verified["status"] == "valid"


def test_json_success_and_failure_follow_the_published_schema(capsys, tmp_path):
    validator = Draft7Validator(json.loads(CLI_SCHEMA.read_text(encoding="utf-8")))
    config = _init(capsys, tmp_path)
    _, success = _run_json(capsys, "--config", str(config), "doctor", "--json")
    validator.validate(success)

    code, failure = _run_json(
        capsys,
        "--config",
        str(config),
        "ingest",
        str(tmp_path / "missing.txt"),
        "--json",
    )
    assert code == 5
    validator.validate(failure)
    assert failure["error"]["code"] == "source_not_found"


def test_doctor_is_offline_and_separates_required_from_optional(
    capsys, tmp_path, monkeypatch
):
    config = _init(capsys, tmp_path)
    from src.noesis_cli import doctor

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: pytest.fail("doctor attempted network access"),
    )
    monkeypatch.setattr(
        doctor,
        "_available",
        lambda module: module in {"duckdb", "fastavro", "jsonschema", "yaml"},
    )
    code, output = _run_json(capsys, "--config", str(config), "doctor", "--json")

    assert code == 0
    assert output["data"]["network_used"] is False
    assert output["data"]["required_failures"] == 0
    assert output["data"]["optional_warnings"] > 0
    assert output["data"]["status"] == "degraded"
    assert all(
        "python -m pip install" in row["repair"]
        for row in output["data"]["checks"]
        if row["status"] == "warn" and row.get("repair", "").startswith("python -m pip")
    )


def test_doctor_reports_a_broken_minimal_install(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)
    from src.noesis_cli import doctor

    monkeypatch.setattr(doctor, "_available", lambda _module: False)
    code, output = _run_json(capsys, "--config", str(config), "doctor", "--json")
    required = next(
        row
        for row in output["data"]["checks"]
        if row["name"] == "dependencies.required"
    )

    assert code == 1
    assert output["data"]["status"] == "broken"
    assert required["status"] == "fail"
    assert "noesis-evidence[minimal]" in required["repair"]


def test_doctor_reports_a_fully_ready_install(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)
    from src.argument_mining import model_registry
    from src.noesis_cli import doctor

    monkeypatch.setattr(doctor, "_available", lambda _module: True)
    monkeypatch.setattr(model_registry, "verify_pins", lambda **_kwargs: [])
    code, output = _run_json(capsys, "--config", str(config), "doctor", "--json")

    assert code == 0
    assert output["data"]["status"] == "ready"
    assert output["data"]["required_failures"] == 0
    assert output["data"]["optional_warnings"] == 0
    assert all(row["status"] == "pass" for row in output["data"]["checks"])


def test_configuration_errors_have_a_stable_exit_and_repair(capsys, tmp_path):
    config = tmp_path / "bad.json"
    config.write_text('{"config_version": 999}', encoding="utf-8")

    code, output = _run_json(capsys, "--config", str(config), "doctor", "--json")

    assert code == 1
    check = next(
        row for row in output["data"]["checks"] if row["name"] == "configuration"
    )
    assert check["required"] is True
    assert check["repair"] == "noesis init"


def test_errors_redact_secret_environment_values(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)
    secret = "do-not-print-this-token"
    monkeypatch.setenv("NOESIS_API_KEY", secret)
    monkeypatch.setattr(
        "src.noesis_cli.app._url_document",
        lambda *_args: (_ for _ in ()).throw(
            CLIError("fetch_failed", f"upstream rejected {secret}")
        ),
    )

    code, output = _run_json(
        capsys, "--config", str(config), "ingest", "https://example.invalid/a", "--json"
    )

    assert code == 5
    serialized = json.dumps(output)
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_explicit_http_url_ingestion(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)

    class Response:
        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "text/plain; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"The local mission completed successfully."

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    code, output = _run_json(
        capsys,
        "--config",
        str(config),
        "ingest",
        "https://example.invalid/mission.txt",
        "--domain",
        "local",
        "--json",
    )

    assert code == 0
    assert output["data"]["documents"][0].startswith("web:")
    assert output["data"]["upsert"]["inserted"] == 1


def test_private_exports_are_opt_in_and_outputs_are_not_overwritten(capsys, tmp_path):
    config = _init(capsys, tmp_path)
    base = ("--config", str(config))
    _run_json(capsys, *base, "ingest", str(FIXTURE), "--json")
    bundle = tmp_path / "answer.json"

    code, excluded = _run_json(
        capsys,
        *base,
        "export",
        "answer",
        "--domain",
        "local",
        "--question",
        "mission",
        "--output",
        str(bundle),
        "--json",
    )
    assert code == 5
    assert excluded["error"]["code"] == "private_evidence_excluded"
    assert not bundle.exists()

    args = (
        *base,
        "export",
        "answer",
        "--domain",
        "local",
        "--question",
        "mission",
        "--include-private",
        "--output",
        str(bundle),
        "--json",
    )
    assert _run_json(capsys, *args)[0] == 0
    code, exists = _run_json(capsys, *args)
    assert code == 5
    assert exists["error"]["code"] == "output_exists"


def test_watch_lifecycle_alias_confirmation_and_cursor_persistence(
    capsys, tmp_path, monkeypatch
):
    config = _init(capsys, tmp_path)
    base = ("--config", str(config))
    code, created = _run_json(
        capsys,
        *base,
        "watch",
        "create",
        "--domain",
        "local",
        "--type",
        "topic",
        "--value",
        "mission",
        "--json",
    )
    assert code == 0
    watch_id = created["data"]["data"]["watch_id"]

    code, listing = _run_json(capsys, *base, "watches", "--domain", "local", "--json")
    assert code == 0
    assert any(row["watch_id"] == watch_id for row in listing["data"]["data"])
    assert _run_json(capsys, *base, "watch", "pause", watch_id, "--json")[0] == 0
    assert _run_json(capsys, *base, "watch", "resume", watch_id, "--json")[0] == 0

    from src.kb import contract

    seen_cursors = []

    def fake_poll(_watch_id, _principal, cursor, _limit, _events, **_kwargs):
        seen_cursors.append(cursor)
        return {
            "contract": "noesis-kb-v1",
            "domain": "local",
            "as_of_ms": 0,
            "data": {
                "watch_contract": "noesis-claim-watch-v1",
                "watch_id": watch_id,
                "cursor": "cursor-after-event-1",
                "events": [{"event_id": "event-1"}] if cursor is None else [],
                "has_more": False,
                "n": 1 if cursor is None else 0,
            },
        }

    monkeypatch.setattr(contract, "watch_poll", fake_poll)
    cursor_file = tmp_path / "watch.cursor"
    code, first_poll = _run_json(
        capsys,
        *base,
        "watch",
        "poll",
        watch_id,
        "--cursor-file",
        str(cursor_file),
        "--json",
    )
    assert code == 0
    assert first_poll["data"]["cursor_saved"] is True
    first_cursor = cursor_file.read_text(encoding="utf-8").strip()
    code, second_poll = _run_json(
        capsys,
        *base,
        "watch",
        "poll",
        watch_id,
        "--cursor-file",
        str(cursor_file),
        "--json",
    )
    assert code == 0
    assert second_poll["data"]["data"]["events"] == []
    assert cursor_file.read_text(encoding="utf-8").strip() == first_cursor
    assert seen_cursors == [None, "cursor-after-event-1"]

    code, refused = _run_json(
        capsys, *base, "watch", "delete", watch_id, "--non-interactive", "--json"
    )
    assert code == 6
    assert refused["error"]["code"] == "confirmation_required"
    assert (
        _run_json(capsys, *base, "watch", "delete", watch_id, "--yes", "--json")[0] == 0
    )


def test_claim_and_integrity_exports_use_the_same_offline_verifier(capsys, tmp_path):
    config = _init(capsys, tmp_path)
    base = ("--config", str(config))
    _, ingested = _run_json(capsys, *base, "ingest", str(FIXTURE), "--json")
    document_id = ingested["data"]["documents"][0]
    payload = json.loads(config.read_text(encoding="utf-8"))

    import duckdb

    conn = duckdb.connect(payload["warehouse"])
    try:
        from src.kb.clusters import ensure_cluster_schema

        ensure_cluster_schema(conn)
        conn.execute(
            "INSERT INTO argument_claims"
            " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
            " VALUES ('claim:mission', 'The sample returned sealed.', ?, 'note', 0.9,"
            " 'deterministic:test')",
            [document_id],
        )
    finally:
        conn.close()

    for kind, identifier in (("claim", "claim:mission"), ("integrity", document_id)):
        bundle = tmp_path / f"{kind}.bundle.json"
        code, exported = _run_json(
            capsys,
            *base,
            "export",
            kind,
            identifier,
            "--domain",
            "local",
            "--include-private",
            "--output",
            str(bundle),
            "--json",
        )
        assert code == 0
        assert exported["data"]["operation"] == kind
        code, verified = _run_json(capsys, "verify", str(bundle), "--json")
        assert code == 0
        assert verified["valid"] is True


def test_serve_dry_run_reports_surface_address_and_auth(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)
    monkeypatch.setenv("NOESIS_MCP_AUTH_TOKEN", "not-reported")
    code, output = _run_json(
        capsys,
        "--config",
        str(config),
        "serve",
        "--surface",
        "kb-mcp",
        "--transport",
        "http",
        "--host",
        "127.0.0.2",
        "--port",
        "9123",
        "--dry-run",
        "--json",
    )

    assert code == 0
    report = output["data"]
    assert report["address"] == "http://127.0.0.2:9123"
    assert report["auth"] == "bearer-token"
    assert report["enabled_surfaces"] == ["kb-mcp"]
    assert "not-reported" not in json.dumps(output)


def test_live_mcp_stdio_rejects_json_status_output(capsys, tmp_path):
    config = _init(capsys, tmp_path)
    code, output = _run_json(
        capsys,
        "--config",
        str(config),
        "serve",
        "--surface",
        "kb-mcp",
        "--transport",
        "stdio",
        "--json",
    )
    assert code == 5
    assert output["error"]["code"] == "incompatible_output"


def test_missing_server_extra_has_an_actionable_error(capsys, tmp_path, monkeypatch):
    config = _init(capsys, tmp_path)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    code, output = _run_json(
        capsys,
        "--config",
        str(config),
        "serve",
        "--surface",
        "api",
        "--json",
    )

    assert code == 4
    assert output["error"]["code"] == "missing_dependency"
    assert (
        output["error"]["repair"] == 'python -m pip install "noesis-evidence[server]"'
    )


def _normalized(requirement: str) -> str:
    return (
        requirement.split(";", 1)[0]
        .split("[", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .split("=", 1)[0]
        .strip()
        .casefold()
    )


def test_dependency_extras_and_version_cannot_drift():
    from src.noesis_cli.doctor import _CAPABILITIES

    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    extras = project["optional-dependencies"]

    assert project["requires-python"] == ">=3.11"
    assert project["version"] == __version__
    assert extras["minimal"] == []
    assert {_normalized(item) for item in project["dependencies"]} == {
        package.casefold() for package in _CAPABILITIES["required"].values()
    }
    groups = ("server", "models", "vector", "media", "orchestration", "cloud")
    expected = {_normalized(item) for group in groups for item in extras[group]}
    assert {_normalized(item) for item in extras["full"]} == expected
    for group in groups:
        assert {_normalized(item) for item in extras[group]} == {
            package.casefold() for package in _CAPABILITIES[group].values()
        }
    assert project["scripts"]["noesis"] == "src.noesis_cli.app:main"

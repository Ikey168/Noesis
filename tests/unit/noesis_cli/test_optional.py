import hashlib
import json

import pytest

from src.noesis_cli.config import initialize
from src.noesis_cli.optional import main, run_request, write_json


def test_cli_doctor_never_loads_models_or_sends_network(capsys):
    assert main(["--doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (
        "splink" in payload["job_operations"]
        and "registry_import" in payload["request_kinds"]
    )
    assert payload["network_calls"] == 0


def test_cli_hosted_decision_requires_trusted_scope_and_explicit_network(tmp_path):
    from src.kb.decision_runtime import DecisionRuntimeError

    config, _ = initialize(root=tmp_path / "workspace")
    request = {
        "kind": "decision", "namespace": "research", "run_id": "fixture",
        "task": "fixture-task", "questions": {
            "decision": {"type": "noul", "instructions": "Relevant?"},
        },
        "sources": [{"item_id": "missing", "source_version": 1}],
        "policy": {"hosted_allowed": True}, "max_cost_usd_micros": 100,
    }
    with pytest.raises(DecisionRuntimeError) as unauthorized:
        run_request(config, request, scopes=set())
    assert unauthorized.value.code == "unauthorized"
    with pytest.raises(DecisionRuntimeError) as closed:
        run_request(
            config, request,
            scopes={"knowledge:decision:execute", "namespace:research:write"},
        )
    assert closed.value.code == "remote_disabled"


def test_cli_task_rollout_is_authorized_and_readable(tmp_path):
    config, _ = initialize(root=tmp_path / "workspace")
    request = {
        "kind": "decision_rollout", "namespace": "research",
        "task": "jev-stance-v1", "mode": "suggestion",
        "model": "jev-1.13.0", "rubric_id": "stance-v1",
        "evaluation_ref": "eval:human:stance-v1",
    }
    configured = run_request(
        config, request,
        scopes={"knowledge:decision:configure", "namespace:research:write"},
    )
    assert configured["mode"] == "suggestion"
    assert configured["evaluation_ref"] == "eval:human:stance-v1"
    inspected = run_request(
        config, {"kind": "decision_rollout_read", "namespace": "research",
                 "task": "jev-stance-v1"},
        scopes={"knowledge:decision:read", "namespace:research:read"},
    )
    assert inspected["mode"] == "suggestion"


def test_cli_native_registry_import_requires_trusted_scopes(
    tmp_path, monkeypatch, capsys
):
    config, _ = initialize(root=tmp_path / "workspace")
    raw = json.dumps([{"id": "DRKS00000001", "title": "Fixture"}]).encode()
    source = tmp_path / "export.json"
    source.write_bytes(raw)
    request = {
        "kind": "registry_import",
        "namespace": "science",
        "provider": "drks",
        "observation": "first",
        "reuse_notice": "fixture",
        "file": str(source),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "parameters": {
            "source_url": "https://drks.de/search/de/trial/DRKS00000001",
            "format": "json",
            "field_map": {"id": "/id", "title": "/title"},
            "export_schema_version": "v1",
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    monkeypatch.setenv("NOESIS_OPTIONAL_SCOPES", "")
    args = ["--config", str(config.path), "--request", str(path)]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "unauthorized"
    monkeypatch.setenv(
        "NOESIS_OPTIONAL_SCOPES", "knowledge:ingestion:execute,namespace:science:write"
    )
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["record_count"] == 1
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["replayed"]
    with pytest.raises(ValueError, match="authority"):
        run_request(config, {**request, "scopes": ["operator"]}, scopes=set())


def test_result_files_are_private_and_not_silently_overwritten(tmp_path):
    path = tmp_path / "out.json"
    write_json(path, {"test": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_json(path, {"test": False})


def test_cli_registry_link_review_uses_captured_observation(tmp_path):
    from src.ingestion.provider_execution import ProviderError

    config, _ = initialize(root=tmp_path / "workspace")
    raw = json.dumps(
        [
            {
                "id": "DRKS00000001",
                "title": "Berliner Studie",
                "identifiers": ["2024-123456-12-00"],
            }
        ]
    ).encode()
    source = tmp_path / "trial.json"
    source.write_bytes(raw)
    imported = run_request(
        config,
        {
            "kind": "registry_import",
            "namespace": "science",
            "provider": "drks",
            "observation": "one",
            "reuse_notice": "authored fixture",
            "file": str(source),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parameters": {
                "format": "json",
                "export_schema_version": "fixture-v1",
                "source_url": "https://drks.de/search/de/trial/DRKS00000001",
                "field_map": {
                    "id": "/id",
                    "title": "/title",
                    "identifiers": "/identifiers",
                },
            },
        },
        scopes={"operator"},
    )
    request = {
        "kind": "regional_review",
        "namespace": "science",
        "parameters": {
            "observation_id": imported["receipt"]["observation_id"],
            "record_index": 0,
            "relationship_index": 0,
            "domain": "clinical-trials",
        },
    }
    with pytest.raises(ProviderError, match="scopes"):
        run_request(config, request, scopes=set())
    first = run_request(config, request, scopes={"operator"})
    assert first["status"] == "unassigned"
    assert (
        run_request(config, request, scopes={"operator"})["task_id"] == first["task_id"]
    )


def test_regional_replay_needs_no_source_file_credentials_or_network(
    tmp_path, monkeypatch
):
    from src.ingestion.provider_execution import ProviderError
    from src.noesis_cli import optional

    config, _ = initialize(root=tmp_path / "workspace")
    raw = json.dumps([{"id": "DRKS00000001", "title": "Deutsche Studie"}]).encode()
    source = tmp_path / "export.json"
    source.write_bytes(raw)
    request = {
        "kind": "registry_import",
        "namespace": "science",
        "provider": "drks",
        "observation": "offline",
        "reuse_notice": "fixture",
        "file": str(source),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "parameters": {
            "source_url": "https://drks.de/search/de/trial/DRKS00000001",
            "format": "json",
            "field_map": {"id": "/id", "title": "/title"},
            "export_schema_version": "v1",
        },
    }
    scopes = {"knowledge:ingestion:execute", "namespace:science:write"}
    first = run_request(config, request, scopes=scopes)
    source.unlink()
    monkeypatch.setattr(
        optional,
        "_http",
        lambda *a, **kw: pytest.fail("offline replay created HTTP client"),
    )
    replay = {
        "kind": "regional",
        "operation": "replay",
        "namespace": "science",
        "provider": "drks",
        "observation": "offline",
        "reuse_notice": "fixture",
    }
    result = run_request(config, replay, scopes=scopes)
    assert result == {**first, "replayed": True}
    with pytest.raises(ProviderError, match="another provider"):
        run_request(config, {**replay, "provider": "ctis"}, scopes=scopes)
    with pytest.raises(ProviderError, match="no captured observation"):
        run_request(config, {**replay, "observation": "missing"}, scopes=scopes)
    from src.evaluation.runtime_errors import BackendError

    with pytest.raises(BackendError, match="scope"):
        run_request(config, replay, scopes=set())

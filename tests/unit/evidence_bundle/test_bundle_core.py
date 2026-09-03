from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from src.evidence_bundle import (
    EvidenceBundleBuilder,
    EvidenceBundleError,
    canonical_bytes,
    compute_bundle_id,
    compute_object_digest,
    export_answer,
    export_receipt,
    sha256_bytes,
    verify_bundle,
    verify_file,
)
from src.evidence_bundle.cli import main as cli_main


def _rehash(bundle):
    for record in bundle["objects"]:
        record["sha256"] = compute_object_digest(record)
    bundle["objects"].sort(key=lambda item: item["id"])
    bundle["manifest"]["entries"] = [
        {"id": item["id"], "type": item["type"], "sha256": item["sha256"]}
        for item in bundle["objects"]
    ]
    bundle["manifest"]["entry_count"] = len(bundle["objects"])
    bundle["bundle_id"] = compute_bundle_id(bundle)
    return bundle


def _answer(verdict="supported", cited=True):
    locator = {
        "document_id": "doc-1" if cited else None,
        "source": "Primary source" if cited else "unknown",
        "url": "https://example.test/source" if cited else None,
        "path": "doc-1#p1" if cited else None,
        "cited": cited,
    }
    return {
        "contract": "noesis-answer-v1",
        "statements": [
            {
                "id": "s1",
                "text": "The reported value was 10.",
                "verdict": verdict,
                "evidence": [locator] if cited else [],
            }
        ],
    }


def test_canonical_json_is_stable_and_rejects_non_json_numbers():
    assert canonical_bytes({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'
    assert canonical_bytes({"a": [1, True, None]}) == b'{"a":[1,true,null]}'
    with pytest.raises(ValueError):
        canonical_bytes({"bad": float("nan")})


def test_builder_is_deterministic_and_fixture_verifies():
    first = EvidenceBundleBuilder("receipt", {}, created_at_ms=0)
    first.add_object("receipt", {"ok": True}, object_id="receipt:root", root=True)
    second = EvidenceBundleBuilder("receipt", {}, created_at_ms=0)
    second.add_object("receipt", {"ok": True}, object_id="receipt:root", root=True)
    assert first.build() == second.build()
    fixture = Path("contracts/examples/evidence-bundle-v1/valid-minimal.json")
    assert json.loads(fixture.read_text()) == first.build()
    assert verify_file(fixture).status == "valid"


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("valid-minimal.json", "valid"),
        ("valid-external-reference.json", "valid_with_external_references"),
        ("incomplete-declared-omission.json", "incomplete"),
        ("invalid-tampered-object.json", "invalid"),
    ],
)
def test_committed_contract_fixtures_have_expected_status(name, status):
    path = Path("contracts/examples/evidence-bundle-v1") / name
    assert verify_file(path).status == status


def test_builder_rejects_collisions_and_unresolved_local_references():
    builder = EvidenceBundleBuilder("receipt", created_at_ms=0)
    builder.add_object("receipt", {"one": 1}, object_id="same", root=True)
    with pytest.raises(EvidenceBundleError, match="collision"):
        builder.add_object("receipt", {"two": 2}, object_id="same")

    broken = EvidenceBundleBuilder("receipt", created_at_ms=0)
    broken.add_object(
        "receipt", {}, object_id="root", references=["missing"], root=True
    )
    with pytest.raises(EvidenceBundleError, match="unresolved"):
        broken.build()


def test_answer_export_normalizes_evidence_and_pins_models():
    answer = _answer()
    answer["statements"][0]["prediction_mode"] = (
        "pretrained:Nithiwat/mdeberta-v3-base_claimbuster"
    )
    bundle = export_answer(
        answer, inputs={"domain": "news", "question": "value?"}, created_at_ms=0
    )
    root = next(row for row in bundle["objects"] if row["type"] == "answer")
    evidence = [row for row in bundle["objects"] if row["type"] == "evidence"]
    pins = [row for row in bundle["objects"] if row["type"] == "model_pin"]
    assert len(evidence) == 1
    assert len(pins) == 1
    assert root["payload"]["statements"][0]["evidence_refs"] == [evidence[0]["id"]]
    assert pins[0]["payload"]["resolved_revision"]
    assert verify_bundle(bundle).status == "valid"


def test_unverifiable_answer_can_be_explicitly_uncited():
    bundle = export_answer(
        _answer(verdict="unverifiable", cited=False), created_at_ms=0
    )
    assert verify_bundle(bundle).status == "valid"


def test_private_evidence_requires_explicit_authorized_inclusion():
    answer = _answer()
    answer["statements"][0]["evidence"][0]["visibility"] = "private"
    with pytest.raises(EvidenceBundleError, match="include_private"):
        export_answer(answer, created_at_ms=0)
    bundle = export_answer(answer, created_at_ms=0, include_private=True)
    assert bundle["operation"]["inputs"]["private_evidence_included"] is True
    assert verify_bundle(bundle).status == "valid"


def test_supported_answer_without_citation_is_invalid():
    bundle = export_answer(_answer(verdict="supported", cited=False), created_at_ms=0)
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("no cited evidence" in error for error in result.errors)


def test_payload_tampering_breaks_object_hash():
    bundle = export_answer(_answer(), created_at_ms=0)
    root = next(row for row in bundle["objects"] if row["type"] == "answer")
    root["payload"]["statements"][0]["text"] = "The reported value was 99."
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("digest mismatch" in error for error in result.errors)


def test_embedded_source_excerpt_tampering_breaks_evidence_hash():
    answer = _answer()
    answer["statements"][0]["evidence"][0]["excerpt"] = "The source reported 10."
    bundle = export_answer(answer, created_at_ms=0)
    evidence = next(row for row in bundle["objects"] if row["type"] == "evidence")
    evidence["payload"]["locator"]["excerpt"] = "The source reported 99."
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("digest mismatch" in error for error in result.errors)


def test_bounded_payload_mutations_never_verify_against_the_original_manifest():
    rng = random.Random(2026)
    for index in range(25):
        bundle = export_receipt(
            {"record": {"index": index, "value": rng.randrange(1_000_000)}},
            created_at_ms=0,
        )
        root = next(row for row in bundle["objects"] if row["type"] == "receipt")
        root["payload"]["record"]["value"] += 1
        assert verify_bundle(bundle).status == "invalid"


def test_rehashed_citation_swap_to_uncited_evidence_still_fails():
    bundle = export_answer(_answer(), created_at_ms=0)
    evidence = next(row for row in bundle["objects"] if row["type"] == "evidence")
    evidence["payload"]["locator"].update(
        {"document_id": None, "url": None, "path": None, "cited": False}
    )
    _rehash(bundle)
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("no cited evidence" in error for error in result.errors)


def test_rehashed_missing_assumptions_and_bad_interval_fail_honesty():
    receipt = {
        "analytic": {
            "n": 3,
            "method": "bootstrap",
            "assumptions": ["independent samples"],
            "estimate": {"value": 0.5, "lo": 0.2, "hi": 0.8, "level": 0.95},
        }
    }
    bundle = export_receipt(receipt, created_at_ms=0)
    root = next(row for row in bundle["objects"] if row["type"] == "receipt")
    del root["payload"]["analytic"]["assumptions"]
    root["payload"]["analytic"]["estimate"]["lo"] = 0.9
    _rehash(bundle)
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("assumptions" in error for error in result.errors)
    assert any("malformed interval" in error for error in result.errors)


def test_rehashed_unknown_model_pin_requires_partial_status():
    answer = _answer()
    answer["statements"][0]["prediction_mode"] = "zero-shot:unknown/model"
    bundle = export_answer(answer, created_at_ms=0)
    assert verify_bundle(bundle).status == "incomplete"
    bundle["completeness"] = {"status": "complete", "omissions": []}
    _rehash(bundle)
    result = verify_bundle(bundle)
    assert result.status == "invalid"
    assert any("unresolved model pins" in error for error in result.errors)


def test_external_adjacent_and_partial_statuses(tmp_path):
    external = EvidenceBundleBuilder("receipt", created_at_ms=0)
    external.add_object("receipt", {}, object_id="root", root=True)
    external.add_external_reference("source", "https://example.test/archive")
    assert verify_bundle(external.build()).status == "valid_with_external_references"

    asset = tmp_path / "source.txt"
    asset.write_bytes(b"source bytes")
    adjacent = EvidenceBundleBuilder("receipt", created_at_ms=0)
    adjacent.add_object("receipt", {}, object_id="root", root=True)
    adjacent.add_external_reference(
        "source", "source.txt", mode="adjacent", sha256=sha256_bytes(asset.read_bytes())
    )
    bundle_path = tmp_path / "bundle.json"
    bundle = adjacent.build()
    bundle_path.write_text(json.dumps(bundle))
    assert verify_bundle(bundle, bundle_path=bundle_path).status == "valid"
    asset.unlink()
    assert verify_bundle(bundle, bundle_path=bundle_path).status == "incomplete"

    partial = EvidenceBundleBuilder("receipt", created_at_ms=0)
    partial.add_object("receipt", {}, object_id="root", root=True)
    partial.add_omission("snapshot bytes unavailable")
    assert verify_bundle(partial.build()).status == "incomplete"


def test_adjacent_path_traversal_and_symlink_escape_are_rejected(tmp_path):
    builder = EvidenceBundleBuilder("receipt", created_at_ms=0)
    builder.add_object("receipt", {}, object_id="root", root=True)
    builder.add_external_reference(
        "escape", "../secret", mode="adjacent", sha256=sha256_bytes(b"secret")
    )
    result = verify_bundle(builder.build(), bundle_path=tmp_path / "bundle.json")
    assert result.status == "invalid"
    assert any("unsafe path" in error for error in result.errors)

    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "linked-evidence.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    linked = EvidenceBundleBuilder("receipt", created_at_ms=0)
    linked.add_object("receipt", {}, object_id="root", root=True)
    linked.add_external_reference(
        "link", "linked-evidence.txt", mode="adjacent", sha256=sha256_bytes(b"secret")
    )
    linked_result = verify_bundle(linked.build(), bundle_path=tmp_path / "bundle.json")
    assert linked_result.status == "invalid"
    assert any("unsafe path" in error for error in linked_result.errors)


def test_cli_returns_stable_json_and_exit_codes(tmp_path, capsys):
    valid_path = Path("contracts/examples/evidence-bundle-v1/valid-minimal.json")
    assert cli_main(["verify", str(valid_path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "valid"

    invalid_path = tmp_path / "bad.json"
    invalid_path.write_text("not json")
    assert cli_main(["verify", str(invalid_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"

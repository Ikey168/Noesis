import hashlib
import json

import pytest

from src.evaluation import published_ner as ner


def test_unicode_nested_annotations_and_optional_empty_final_column():
    raw = "#\tsource\n1\tUniversität\tB-ORG\tO\t\n2\tBerlin\tI-ORG\tB-LOC\n\n".encode()
    row = ner.parse_records(raw, "de")[0]
    assert row["text"] == "Universität Berlin"
    assert row["expected"] == [
        {"label": "organisation", "start": 0, "end": 18},
        {"label": "location", "start": 12, "end": 18},
    ]
    assert row["source_locator"] == "source"


def test_unsupported_native_tags_are_not_silently_relabeled():
    rows = ner.parse_records(b"1\tBerlin-based\tB-LOCpart\tO\n\n", "de")
    assert rows[0]["unsupported_labels"] == ["LOCpart"]
    assert rows[0]["expected"] == []


def test_orphan_continuation_is_not_invented_as_new_human_label():
    with pytest.raises(ValueError, match="orphan"):
        ner.parse_records(b"Berlin\tI-location\n", "en")


def test_selection_checks_manifest_before_reading_labels(tmp_path, monkeypatch):
    raw = b"Ada\tB-person\n\n"
    (tmp_path / "test.txt").write_bytes(raw)
    spec = {
        "test.txt": {
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "language": "en",
            "source": "authored test fixture",
            "label_origin": "published-human-annotation",
        }
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(spec))
    monkeypatch.setattr(ner, "MANIFEST", manifest)
    _, _, rows = ner.prepare(tmp_path, limit=1)
    assert rows[0]["source_revision"] == hashlib.sha256(b"Ada").hexdigest()
    (tmp_path / "test.txt").write_bytes(raw.replace(b"Ada", b"Eva"))
    with pytest.raises(ValueError, match="digest"):
        ner.prepare(tmp_path, limit=1)


def test_specialised_native_labels_can_be_preserved_without_coarse_mapping():
    row = ner.parse_records(
        b"Ada\tB-politician\n", "en", label_mapping={"politician": "politician"}
    )[0]
    assert row["expected"][0]["label"] == "politician"


def test_relation_direction_and_document_identity_are_part_of_exact_match():
    from src.evaluation.published_relations import score_relations

    gold = {
        "label": "role",
        "source_id": "a",
        "head": {"start": 0, "end": 3},
        "tail": {"start": 5, "end": 8},
    }
    reverse = {**gold, "head": gold["tail"], "tail": gold["head"]}
    assert score_relations([gold], [gold])["f1"] == 1
    assert score_relations([reverse], [gold])["f1"] == 0
    assert score_relations([{**gold, "source_id": "b"}], [gold])["f1"] == 0

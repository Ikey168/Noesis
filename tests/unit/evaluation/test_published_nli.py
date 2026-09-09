import copy
import sys
from types import SimpleNamespace

import pytest

from scripts.evaluate_published_nli import summarize
from src.evaluation.published_nli import LABELS, prepare, select_pairs, task_probes


def splits():
    return {
        language + "-" + split: [
            {
                "premise": split + str(i),
                "hypothesis": "hypothesis " + str(i),
                "label": i,
            }
            for i in range(3)
        ]
        for language in ("de", "en")
        for split in ("validation", "test")
    }


def test_translations_stay_grouped_and_shared_premises_cannot_leak():
    data = splits()
    for language in ("de", "en"):
        data[language + "-test"][0]["premise"] = "validation0"
        data[language + "-test"].append(
            {
                "premise": "different premise",
                "hypothesis": "different hypothesis",
                "label": 0,
            }
        )
    rows = select_pairs(data, per_class=1)
    assert len(rows) == 12
    validation = {r["group_id"] for r in rows if r["split"] == "validation"}
    test = {r["group_id"] for r in rows if r["split"] == "test"}
    assert not validation & test
    assert rows[0]["group_id"] == rows[1]["group_id"]
    assert "xnli:en:test:3" in {r["id"] for r in rows}
    assert rows == select_pairs(data, per_class=1)


def test_misaligned_translations_and_changed_source_fail_before_inference(tmp_path):
    data = splits()
    data["de-validation"][0]["label"] = 2
    with pytest.raises(ValueError, match="not aligned"):
        select_pairs(data, per_class=1)
    (tmp_path / "de-validation.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        prepare(tmp_path)


def test_test_labels_do_not_influence_calibration_policy():
    rows = select_pairs(splits(), per_class=1)
    for row in rows:
        row["label_origin"] = "fixture"
        row["scores"] = [0.8 if label in row["labels"] else 0.1 for label in LABELS]
        row["elapsed_s"] = 0.01
    result = {"rows": rows, "model": {"model": "fixture", "revision": "fixture"}}
    before = summarize(result)
    altered = copy.deepcopy(result)
    next(r for r in altered["rows"] if r["split"] == "test" and r["language"] == "de")[
        "labels"
    ] = ["contradiction"]
    after = summarize(altered)
    assert before["de"]["policy"] == after["de"]["policy"]
    assert before["de"]["raw"]["accuracy"] != after["de"]["raw"]["accuracy"]


def test_task_transfer_keeps_targets_and_hypothesis_spaces_separate():
    pairs = []

    class Backend:
        def entailment_scores(self, batch, *, batch_size):
            pairs.append(batch)
            return [0.8] + [0.05] * (len(batch) - 1)

    result = task_probes(Backend(), {
        "stance": [{"id": "s", "text": "Authored stance probe.", "topic": "Berlin transport", "labels": ["supportive"], "source_type": "note"}],
        "frames": [{"id": "f", "text": "Authored frame probe.", "labels": ["economic"], "source_type": "note"}],
    })
    assert len(pairs[0]) == 4 and len(pairs[1]) == 7
    assert all("Berlin transport" in hypothesis for _, hypothesis in pairs[0])
    assert all("frames the issue" in hypothesis for _, hypothesis in pairs[1])
    assert result["stance"]["rows"][0]["target"] == "Berlin transport"
    assert result["frames"]["readiness"].startswith("unsupported")


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_native_mdeberta_cpu_dtype_is_explicit(monkeypatch, device):
    from src.evaluation import mining_runtime

    calls = []
    float32 = object()

    class Model:
        config = SimpleNamespace(id2label=dict(enumerate(LABELS)))
        dtype = "fixture-dtype"

        def to(self, selected):
            assert selected == device
            return self

        def eval(self):
            return self

    def load(path, **kwargs):
        calls.append(kwargs)
        return Model()

    monkeypatch.setattr(mining_runtime, "model_path", lambda _: ("/unused", {}))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(float32=float32))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **k: object()), AutoModelForSequenceClassification=SimpleNamespace(from_pretrained=load)))
    backend = mining_runtime.MultilingualNLI(device=device)
    assert calls[0]["dtype"] == (float32 if device == "cpu" else "auto")
    assert calls[0]["local_files_only"] and not calls[0]["trust_remote_code"]
    assert backend.spec["runtime_dtype"] == "fixture-dtype"

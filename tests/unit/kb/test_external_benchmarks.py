"""Unit tests for external-benchmark preparation and harness pickup (#957)."""

import json

import pytest

import scripts.prepare_external_benchmarks as prep


class FakeClaimDetector:
    _pipeline = None
    _pretrained = None

    @staticmethod
    def predict_text(text):
        from src.argument_mining.models import ClaimPrediction

        is_claim = "unverifiable" not in text.lower()
        return ClaimPrediction(text, 0, is_claim, 0.9)


class TestPrepare:
    @pytest.fixture()
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prep, "ROOT", tmp_path / "external_benchmarks")

        def fake_download(url, timeout=120):
            if "fever" in url:
                rows = [
                    {"label": "SUPPORTS", "claim": f"Claim {i}."} for i in range(5)
                ]
                return "\n".join(json.dumps(r) for r in rows).encode()
            if "liar" in url:
                import io
                import zipfile

                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as archive:
                    tsv = "\n".join(
                        f"id{i}\tfalse\tStatement {i}.\tmeta" for i in range(5)
                    )
                    archive.writestr("test.tsv", tsv)
                return buffer.getvalue()
            return json.dumps([{"claim": f"AV claim {i}."} for i in range(5)]).encode()

        monkeypatch.setattr(prep, "_download", fake_download)
        return tmp_path / "external_benchmarks"

    def test_prepares_expected_layout_and_manifest(self, sandbox):
        assert prep.prepare() == 0
        assert (sandbox / "fever/paper_dev.jsonl").exists()
        assert (sandbox / "liar/test.tsv").exists()
        assert (sandbox / "averitec/dev.json").exists()

        manifest = json.loads((sandbox / "manifest.json").read_text())
        assert set(manifest) == {"fever", "liar", "averitec"}
        for entry in manifest.values():
            assert entry["sha256"] and entry["bytes"] > 0 and entry["license"]

    def test_idempotent_without_force(self, sandbox, capsys):
        prep.prepare()
        prep.prepare()
        assert "[skip]" in capsys.readouterr().out

    def test_sample_mode_truncates(self, sandbox):
        prep.prepare(sample=2)
        fever_lines = (sandbox / "fever/paper_dev.jsonl").read_text().strip().splitlines()
        assert len(fever_lines) == 2
        averitec = json.loads((sandbox / "averitec/dev.json").read_text())
        assert len(averitec) == 2
        manifest = json.loads((sandbox / "manifest.json").read_text())
        assert manifest["fever"]["sampled_to"] == 2

    def test_failure_reported_not_fatal(self, sandbox, monkeypatch):
        def broken(url, timeout=120):
            raise OSError("network down")

        monkeypatch.setattr(prep, "_download", broken)
        assert prep.prepare(force=True) == 1


class TestHarnessEvaluators:
    """The existing _try_* evaluators run against prepared fixture files."""

    def test_fever_evaluator_reads_prepared_file(self, tmp_path):
        import scripts.benchmark_models as bench

        fever = tmp_path / "fever"
        fever.mkdir()
        rows = [
            {"label": "SUPPORTS", "claim": "GDP rose 3 percent in 2024."},
            {"label": "NOT ENOUGH INFO", "claim": "Something unverifiable happened."},
        ]
        (fever / "paper_dev.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows)
        )
        metrics = bench._try_fever(fever, detector=FakeClaimDetector())
        assert metrics is not None
        assert metrics["n"] == 2
        assert 0.0 <= metrics["f1"] <= 1.0

    def test_liar_evaluator_reads_prepared_file(self, tmp_path):
        import scripts.benchmark_models as bench

        liar = tmp_path / "liar"
        liar.mkdir()
        (liar / "test.tsv").write_text(
            "id1\tfalse\tThe deficit doubled last year.\tmeta\n"
            "id2\ttrue\tUnemployment fell to 4 percent.\tmeta\n"
        )
        metrics = bench._try_liar(liar, detector=FakeClaimDetector())
        assert metrics is not None
        assert metrics["n"] == 2

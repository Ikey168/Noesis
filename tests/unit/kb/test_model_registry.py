"""Unit tests for the pinned-model registry and fetch flow (#959)."""

from src.argument_mining import model_registry


class TestPins:
    def test_pins_cover_both_backends_with_env_overrides(self, monkeypatch):
        monkeypatch.delenv("NOESIS_NLI_MODEL", raising=False)
        pins = model_registry.resolved_pins()
        assert set(pins) == {"nli", "claim"}
        assert pins["nli"]["model"] == "cross-encoder/nli-deberta-v3-base"

        monkeypatch.setenv("NOESIS_NLI_MODEL", "my-org/other-nli")
        assert model_registry.resolved_pins()["nli"]["model"] == "my-org/other-nli"

    def test_fetch_manifest_excludes_duplicate_framework_exports(self):
        nli = model_registry.inference_files("nli")
        claim = model_registry.inference_files("claim")
        assert "model.safetensors" in nli
        assert "pytorch_model.bin" not in nli
        assert "pytorch_model.bin" in claim
        assert all(not path.startswith("onnx/") for path in nli + claim)

    def test_cache_requires_the_selected_weight_file(self, tmp_path):
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        assert model_registry._has_inference_weights("claim", snapshot) is False
        (snapshot / "pytorch_model.bin").write_bytes(b"weights")
        assert model_registry._has_inference_weights("claim", snapshot) is True


class TestFetchAndLock:
    def _fake_downloader(self, calls):
        def downloader(model, revision):
            calls.append((model, revision))
            return {"revision": "a" * 40, "path": f"/cache/{model}"}

        return downloader

    def test_fetch_writes_lock_with_resolved_revisions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOESIS_NLI_MODEL", raising=False)
        monkeypatch.delenv("NOESIS_CLAIM_MODEL", raising=False)
        lock_path = tmp_path / "pins.lock.json"
        calls = []
        summary = model_registry.fetch_models(
            downloader=self._fake_downloader(calls), lock_path=lock_path
        )
        assert len(summary["fetched"]) == 2
        assert summary["failed"] == []
        assert summary["warnings"] == []

        lock = model_registry.read_lock(lock_path)
        assert lock["nli"]["resolved_revision"] == "a" * 40
        assert lock["nli"]["model"] == "cross-encoder/nli-deberta-v3-base"
        assert lock["claim"]["serves"] == ["claim detection (#956)"]

    def test_fetch_failure_reported_and_others_continue(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOESIS_NLI_MODEL", raising=False)

        def flaky(model, revision):
            if "nli" in model:
                raise OSError("network down")
            return {"revision": "a" * 40, "path": "/cache/x"}

        summary = model_registry.fetch_models(
            downloader=flaky, lock_path=tmp_path / "pins.lock.json"
        )
        assert len(summary["failed"]) == 1
        assert summary["failed"][0]["backend"] == "nli"
        assert len(summary["fetched"]) == 1
        # The unfetched pin surfaces as a warning, loudly.
        assert any("nli" in warning for warning in summary["warnings"])


class TestDriftEnforcement:
    def test_unfetched_and_drifted_pins_warn(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOESIS_NLI_MODEL", raising=False)
        monkeypatch.delenv("NOESIS_CLAIM_MODEL", raising=False)
        lock_path = tmp_path / "pins.lock.json"

        # Nothing fetched yet: every pin warns.
        warnings = model_registry.verify_pins(lock_path)
        assert len(warnings) == 2

        model_registry.fetch_models(
            downloader=lambda m, r: {"revision": "a" * 40, "path": "/x"},
            lock_path=lock_path,
        )
        assert model_registry.verify_pins(lock_path) == []

        # Operative pin changes (env override) -> drift warning.
        monkeypatch.setenv("NOESIS_NLI_MODEL", "my-org/other-nli")
        drift = model_registry.verify_pins(lock_path)
        assert len(drift) == 1
        assert "other-nli" in drift[0]


class TestBackendStatus:
    def test_status_reports_all_three_wrappers(self, monkeypatch):
        import src.argument_mining.frames as frames
        import src.argument_mining.models as models

        class Wrapper:
            prediction_mode = "pretrained:test-model"

        monkeypatch.setattr(models, "ClaimDetector", Wrapper)
        monkeypatch.setattr(models, "StanceClassifier", Wrapper)
        monkeypatch.setattr(frames, "FrameClassifier", Wrapper)
        status = model_registry.backend_status()
        assert set(status) == {"claims", "stance", "frames"}
        assert all(mode == "pretrained:test-model" for mode in status.values())

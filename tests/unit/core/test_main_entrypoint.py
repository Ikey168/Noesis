"""Tests for legacy/src/main.py and the legacy src/scraper.py facade."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import legacy.src.main as main_module  # noqa: E402


def _load_legacy_scraper():
    """Load src/scraper.py, which is shadowed by the scraper package."""
    path = os.path.join(os.path.dirname(SRC), "legacy", "src", "scraper.py")
    spec = importlib.util.spec_from_file_location("legacy_scraper_facade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy_scraper = _load_legacy_scraper()


class TestMainNoAction:
    def test_no_action(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        main_module.main()
        out = capsys.readouterr().out
        assert "No action specified" in out


class TestMainScrape:
    def test_basic_scrape(self, monkeypatch, capsys):
        run_spider = MagicMock()
        monkeypatch.setattr(main_module, "run_spider", run_spider)
        monkeypatch.setattr(main_module, "load_aws_config", lambda env: {})
        monkeypatch.setattr(sys, "argv", ["main.py", "--scrape", "-o", "out.json"])
        main_module.main()
        run_spider.assert_called_once()
        kwargs = run_spider.call_args.kwargs
        assert kwargs["output_file"] == "out.json"
        assert kwargs["s3_storage"] is False
        assert kwargs["cloudwatch_logging"] is False
        assert "Scraping completed" in capsys.readouterr().out

    def test_playwright_flag(self, monkeypatch, capsys):
        run_spider = MagicMock()
        monkeypatch.setattr(main_module, "run_spider", run_spider)
        monkeypatch.setattr(main_module, "load_aws_config", lambda env: {})
        monkeypatch.setattr(sys, "argv", ["main.py", "--scrape", "--playwright"])
        main_module.main()
        assert run_spider.call_args.kwargs["use_playwright"] is True
        assert "Playwright" in capsys.readouterr().out

    def test_s3_requires_credentials(self, monkeypatch):
        monkeypatch.setattr(main_module, "run_spider", MagicMock())
        monkeypatch.setattr(main_module, "load_aws_config", lambda env: {})
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setattr(sys, "argv", ["main.py", "--scrape", "--s3"])
        with pytest.raises(SystemExit):
            main_module.main()

    def test_s3_requires_bucket(self, monkeypatch):
        monkeypatch.setattr(main_module, "run_spider", MagicMock())
        monkeypatch.setattr(main_module, "load_aws_config", lambda env: {})
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "--scrape", "--s3", "--aws-key-id", "k", "--aws-secret-key", "s"],
        )
        with pytest.raises(SystemExit):
            main_module.main()

    # Removed test_s3_with_bucket_and_creds: the --s3/--s3-bucket/--aws-key-id/
    # --aws-secret-key CLI flags were intentionally removed in the offline
    # migration (commit 4720b1d). S3 is now opt-in via the AWS config file only,
    # covered by test_config_file_enables_s3_and_cloudwatch below.

    def test_config_file_enables_s3_and_cloudwatch(self, monkeypatch, capsys):
        run_spider = MagicMock()
        aws_config = {
            "aws_profile": "research",
            "s3_storage": {
                "enabled": True,
                "bucket": "config-bucket",
                "prefix": "cfg-prefix",
            },
            "cloudwatch_logging": {
                "enabled": True,
                "log_group": "cfg-group",
                "log_stream_prefix": "cfg-stream",
                "log_level": "DEBUG",
            },
        }
        monkeypatch.setattr(main_module, "run_spider", run_spider)
        monkeypatch.setattr(main_module, "load_aws_config", lambda env: aws_config)
        monkeypatch.setattr(sys, "argv", ["main.py", "--scrape"])
        main_module.main()
        kwargs = run_spider.call_args.kwargs
        assert kwargs["s3_storage"] is True
        assert kwargs["s3_bucket"] == "config-bucket"
        assert kwargs["s3_prefix"] == "cfg-prefix"
        assert kwargs["cloudwatch_logging"] is True
        assert kwargs["cloudwatch_log_group"] == "cfg-group"
        assert kwargs["cloudwatch_log_stream_prefix"] == "cfg-stream"
        assert kwargs["cloudwatch_log_level"] == "DEBUG"
        assert kwargs["aws_profile"] == "research"
        assert os.environ.get("AWS_PROFILE") == "research"


class TestLegacyLoadAwsConfig:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert legacy_scraper.load_aws_config("nonexistent") == {}

    def test_valid_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev_aws.json").write_text(json.dumps({"aws_profile": "x"}))
        assert legacy_scraper.load_aws_config("dev") == {"aws_profile": "x"}

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev_aws.json").write_text("{not json")
        assert legacy_scraper.load_aws_config("dev") == {}
        assert "Could not parse" in capsys.readouterr().out


class TestLegacyScrapeNews:
    def test_returns_articles_from_output_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        articles = [{"title": "a"}, {"title": "b"}]
        output = tmp_path / "out.json"

        def fake_run_spider(**kwargs):
            output.write_text(json.dumps(articles))

        monkeypatch.setattr(legacy_scraper, "run_spider", fake_run_spider)
        result = legacy_scraper.scrape_news(output_file=str(output))
        assert result == articles

    def test_missing_output_returns_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(legacy_scraper, "run_spider", lambda **kwargs: None)
        result = legacy_scraper.scrape_news(output_file=str(tmp_path / "missing.json"))
        assert result == []
        assert "not found" in capsys.readouterr().out

    def test_unparseable_output_returns_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "out.json"
        output.write_text("{bad json")
        monkeypatch.setattr(legacy_scraper, "run_spider", lambda **kwargs: None)
        result = legacy_scraper.scrape_news(output_file=str(output))
        assert result == []
        assert "Could not parse" in capsys.readouterr().out

    def test_config_overrides(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev_aws.json").write_text(
            json.dumps(
                {
                    "aws_profile": "cfg-profile",
                    "s3_storage": {
                        "enabled": True,
                        "bucket": "cfg-bucket",
                        "prefix": "cfg-prefix",
                    },
                    "cloudwatch_logging": {
                        "enabled": True,
                        "log_group": "cfg-group",
                        "log_stream_prefix": "cfg-stream",
                        "log_level": "WARNING",
                    },
                }
            )
        )
        captured = {}

        def fake_run_spider(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(legacy_scraper, "run_spider", fake_run_spider)
        legacy_scraper.scrape_news(output_file=str(tmp_path / "o.json"))
        assert captured["s3_storage"] is True
        assert captured["s3_bucket"] == "cfg-bucket"
        assert captured["s3_prefix"] == "cfg-prefix"
        assert captured["cloudwatch_logging"] is True
        assert captured["cloudwatch_log_group"] == "cfg-group"
        assert captured["cloudwatch_log_stream_prefix"] == "cfg-stream"
        assert captured["cloudwatch_log_level"] == "WARNING"
        assert os.environ.get("AWS_PROFILE") == "cfg-profile"

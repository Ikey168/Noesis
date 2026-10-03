"""The one SEC User-Agent setting and its deprecated aliases (#2768)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.ingestion.connectors.edgar import USER_AGENT_ENV, EdgarClient, harvest_filing
from src.ingestion.enforcement_sources import SEC_USER_AGENT_ENV as ENFORCEMENT_ENV
from src.ingestion.sec_user_agent import (
    DEPRECATED_SEC_USER_AGENT_ENVS,
    SEC_USER_AGENT_ENV,
    SecUserAgentError,
    require_sec_user_agent,
    resolve_sec_user_agent,
)

ROOT = Path(__file__).resolve().parents[3]
AGENT = "Noesis test operator ops@example.org"
ALL_NAMES = (SEC_USER_AGENT_ENV, *DEPRECATED_SEC_USER_AGENT_ENVS)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_one_canonical_name_shared_by_every_sec_reader():
    assert SEC_USER_AGENT_ENV == "NOESIS_SEC_USER_AGENT"
    assert USER_AGENT_ENV == ENFORCEMENT_ENV == SEC_USER_AGENT_ENV
    assert DEPRECATED_SEC_USER_AGENT_ENVS == ("NOESIS_EDGAR_USER_AGENT", "NOESIS_SEC_CONTACT")
    pack = json.loads((ROOT / "config/source_packs/economic.json").read_text())
    sec = next(s for s in pack["sources"] if s["source_id"] == "sec-edgar")
    assert sec["auth"]["secret_ref"] == SEC_USER_AGENT_ENV


def test_canonical_name_resolves_without_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.ingestion.sec_user_agent"):
        assert resolve_sec_user_agent({SEC_USER_AGENT_ENV: f"  {AGENT} "}) == AGENT
    assert not caplog.records


@pytest.mark.parametrize("alias", DEPRECATED_SEC_USER_AGENT_ENVS)
def test_each_alias_works_and_warns_naming_the_canonical_variable(alias, caplog):
    with caplog.at_level(logging.WARNING, logger="src.ingestion.sec_user_agent"):
        assert require_sec_user_agent({alias: AGENT}) == AGENT
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and alias in messages[0] and SEC_USER_AGENT_ENV in messages[0]
    assert AGENT not in messages[0]


def test_alias_with_the_same_value_as_the_canonical_name_is_not_a_conflict():
    assert resolve_sec_user_agent({SEC_USER_AGENT_ENV: AGENT, "NOESIS_EDGAR_USER_AGENT": AGENT}) == AGENT


@pytest.mark.parametrize(
    "env",
    [
        {SEC_USER_AGENT_ENV: AGENT, "NOESIS_EDGAR_USER_AGENT": "someone else x@example.org"},
        {SEC_USER_AGENT_ENV: AGENT, "NOESIS_SEC_CONTACT": "x@example.org"},
        {"NOESIS_EDGAR_USER_AGENT": AGENT, "NOESIS_SEC_CONTACT": "x@example.org"},
    ],
)
def test_conflicting_values_are_refused_rather_than_picked(env):
    with pytest.raises(SecUserAgentError) as exc:
        resolve_sec_user_agent(env)
    assert exc.value.code == "sec_user_agent_conflict"
    assert SEC_USER_AGENT_ENV in str(exc.value) and AGENT not in str(exc.value)


def test_missing_variable_is_empty_for_skip_readers_and_refused_for_strict_ones():
    assert resolve_sec_user_agent({}) == ""
    assert resolve_sec_user_agent({SEC_USER_AGENT_ENV: "   "}) == ""
    with pytest.raises(SecUserAgentError) as exc:
        require_sec_user_agent({})
    assert exc.value.code == "sec_user_agent_missing" and SEC_USER_AGENT_ENV in str(exc.value)


def test_edgar_client_reads_the_canonical_name_and_aliases_from_the_environment(monkeypatch):
    monkeypatch.setenv(SEC_USER_AGENT_ENV, AGENT)
    assert EdgarClient().configured
    monkeypatch.delenv(SEC_USER_AGENT_ENV)
    monkeypatch.setenv("NOESIS_EDGAR_USER_AGENT", AGENT)
    seen = []
    client = EdgarClient(http_get=lambda url, agent: seen.append(agent) or "{}")
    client.submissions("320193")
    assert seen == [AGENT]


def test_edgar_readers_refuse_a_conflict_and_skip_when_missing(monkeypatch):
    from src.ingestion.connectors.base import PermanentFetchError, SourceRef
    from src.ingestion.connectors.edgar_materials import harvest_sec_company_materials
    from src.ingestion.connectors.filings_connector import FilingsConnector

    assert harvest_filing("ACME", client=EdgarClient()) is None
    with pytest.raises(ValueError, match=SEC_USER_AGENT_ENV):
        harvest_sec_company_materials("ACME", issuer_id="issuer:acme")
    monkeypatch.setenv(SEC_USER_AGENT_ENV, AGENT)
    monkeypatch.setenv("NOESIS_SEC_CONTACT", "x@example.org")
    with pytest.raises(SecUserAgentError):
        EdgarClient()
    with pytest.raises(PermanentFetchError, match="conflicting"):
        FilingsConnector().fetch(SourceRef(locator="ACME"))


def test_enforcement_sec_adapter_refuses_a_conflict(monkeypatch):
    from src.ingestion.enforcement_sources import EnforcementAdapter
    from src.ingestion.source_packs import SourcePackError
    from tests.unit import enforcement_harness as h

    monkeypatch.setenv(SEC_USER_AGENT_ENV, AGENT)
    monkeypatch.setenv("NOESIS_EDGAR_USER_AGENT", "someone else x@example.org")
    live = EnforcementAdapter(h.source("sec-enforcement-releases"))
    with pytest.raises(SourcePackError) as exc:
        live.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert exc.value.code == "source_unavailable" and "conflicting" in str(exc.value)

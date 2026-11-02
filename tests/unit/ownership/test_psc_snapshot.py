"""UK People with significant control snapshot (#2885): bulk adapter, ownership sink, revisions and persons.

Offline, over an authored snapshot about fictional companies and persons.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest

from src.ingestion.bulk import get_bulk_adapter
from src.ingestion.bulk.http import BulkHttp
from src.ingestion.bulk.ownership_sink import OwnershipSink
from src.ingestion.bulk.runner import BulkRunner
from src.ingestion.ownership_providers import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    parse_ch_psc,
)
from src.ingestion.ownership_psc_snapshot import parse_snapshot_line
from src.kb.ownership_graph import OwnershipGraph
from src.kb.ownership_records import OwnershipRecordError
from src.kb.ownership_store import OwnershipStore
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/ownership"
PAGE = "https://download.companieshouse.gov.uk/en_pscdata.html"
BASE = "https://download.companieshouse.gov.uk/"
SELECTION = {"company_numbers": ["09990001", "09990002", "09990003"], "company_prefix": "OE"}


# --------------------------------------------------------------- helpers


class _Resp(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class FakeWeb:
    def __init__(self, files):
        self.files = files

    def __call__(self, request, timeout):
        body, headers = self.files[request.full_url]
        if request.get_method() == "HEAD":
            return _Resp(b"", 200, {**headers, "Content-Length": str(len(body))})
        return _Resp(body, 200, headers)


def snapshot_web(*days):
    files, links = {}, []
    for day in days:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"psc-snapshot-{day}_1of1.txt", (FIXTURES / f"psc-snapshot-{day}.jsonl").read_bytes())
        name = f"psc-snapshot-{day}_1of1.zip"
        files[BASE + name] = (buffer.getvalue(), {"ETag": f'"{day}"'})
        links.append(f'<a href="{name}">{name}</a>')
    files[PAGE] = ("".join(links).encode(), {})
    return FakeWeb(files)


def run_snapshot(tmp_path, conn, web, *, state="state", principal=harness.PRINCIPAL, params=SELECTION):
    adapter = get_bulk_adapter("companies-house-psc-snapshot")
    sink = OwnershipSink(NS, lambda: conn, principal_id=principal, close=False)
    http = BulkHttp(adapter.allowed_hosts, resolver=lambda _h: ["93.184.216.34"], opener=web)
    runner = BulkRunner(adapter, state_dir=tmp_path / state, work_dir=tmp_path / "tmp", sinks=[sink], http=http)
    return runner.run(params)


def views(conn, *, principal=harness.PRINCIPAL, scopes=SCOPES):
    return {v["record"]["record_key"]: v for v in OwnershipStore(conn).records(NS, principal_id=principal, scopes=scopes)}


def lines(day):
    return [json.loads(x) for x in (FIXTURES / f"psc-snapshot-{day}.jsonl").read_text().splitlines()]


# ------------------------------------------------------- #2885 PSC snapshot


def test_psc_snapshot_contract_is_implemented_with_its_audit():
    contract = PROVIDER_CONTRACTS["companies-house-psc-snapshot"]
    assert contract["status"] == "implemented" and (ROOT / contract["audit"]).is_file()
    assert "verify" in contract["terms"] and "UK GDPR" in contract["terms"]
    assert LIVE_VERIFICATION["companies-house-psc-snapshot"]["status"] == "unverified-live"
    adapter = get_bulk_adapter("companies-house-psc-snapshot")
    with pytest.raises(ValueError):
        adapter.validate_params({})  # the whole register is never kept
    with pytest.raises(ValueError):
        adapter.validate_params({"company_numbers": ["123"]})


def test_snapshot_lines_emit_the_same_statements_as_the_api_path():
    corporate = lines("2026-10-30")[0]
    api = parse_ch_psc({"items": [corporate["data"]]}, company="09990002", raw_sha="0" * 64)
    snap = parse_snapshot_line(corporate)
    strip = ("record_key", "source", "unknowns")
    assert [{k: v for k, v in r.items() if k not in strip} for r in api] == [
        {k: v for k, v in r.items() if k not in strip} for r in snap]
    assert snap[0]["record_key"] == ("companies-house:psc-snapshot:09990002:SNAPPSC001:"
                                     "ownership-of-shares-75-to-100-percent")
    # A UK corporate PSC stated without leading zeros resolves to the company number.
    assert snap[0]["holder"]["key"] == "companies-house:gb-coh:09990001"
    assert snap[0]["source"]["source_type"] == "bulk-snapshot" and snap[0]["source"]["locator"] == {
        "json_pointer": "/data/natures_of_control"}


def test_snapshot_replays_offline_into_the_ownership_store_and_rerun_adds_nothing(tmp_path):
    env = harness.Env()
    web = snapshot_web("2026-10-30")
    receipt = run_snapshot(tmp_path, env.conn, web)
    assert receipt["status"] == "complete" and receipt["release_id"] == "2026-10-30"
    counts = receipt["outputs"]["ownership_counts"]
    assert counts["inserted"] > 0 and counts["revised"] == 0
    # The unknown line kind is counted, not guessed; the unselected company is not kept.
    assert receipt["outputs"]["records"]["unrecognised_lines"] == 1
    stored = views(env.conn)
    assert not any("09990009" in key for key in stored)
    assert "companies-house:psc-snapshot-statement:09990001:no-individual-or-entity-with-signficant-control:2016-04-06" \
        in stored
    trust = stored["companies-house:psc-snapshot:OE099901:FIXBO003:"
                   "ownership-of-shares-more-than-25-percent-as-trust-registered-overseas-entity"]["record"]
    assert trust["native"]["held_as"] == "trust" and trust["share"]["band"]["min"] == "25"
    # Same release again: already done, nothing new.
    again = run_snapshot(tmp_path, env.conn, web)
    assert {f["action"] for f in again["files"]} == {"already-done"}
    assert again["outputs"]["ownership_counts"] == {"inserted": 0, "revised": 0, "unchanged": 0}
    # The same snapshot acquired from scratch (fresh bulk state) changes no record either.
    fresh = run_snapshot(tmp_path, env.conn, web, state="state-2")
    assert fresh["outputs"]["ownership_counts"]["inserted"] == 0
    assert fresh["outputs"]["ownership_counts"]["revised"] == 0


def test_ceased_psc_is_a_revision_and_missing_entries_are_never_deleted(tmp_path):
    env = harness.Env()
    run_snapshot(tmp_path, env.conn, snapshot_web("2026-10-30"))
    receipt = run_snapshot(tmp_path, env.conn, snapshot_web("2026-10-30", "2026-10-31"))
    assert receipt["release_id"] == "2026-10-31"
    assert receipt["outputs"]["ownership_counts"]["revised"] == 1
    stored = views(env.conn)
    key = "companies-house:psc-snapshot:09990002:SNAPPSC001:ownership-of-shares-75-to-100-percent"
    current = stored[key]
    assert current["revision"] == 2 and current["record"]["validity"]["to"] == "2026-10-30"
    history = OwnershipStore(env.conn).history(NS, current["record_id"], principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert [h["record"]["validity"]["to_status"] for h in history] == ["open", "stated"]
    assert history[0]["run_id"].startswith("bulk:companies-house-psc-snapshot:2026-10-30:")
    # The PSC statement is absent from the later snapshot: it stays, unrevised.
    statement = [v for k, v in stored.items() if k.startswith("companies-house:psc-snapshot-statement:09990001")]
    assert len(statement) == 1 and statement[0]["revision"] == 1


def test_natural_persons_follow_person_research_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    person_line = lines("2026-10-30")[1]
    scoped = {r["kind"]: r for r in parse_snapshot_line(person_line)}
    assert scoped["person"]["owner_scoped"] is True and "date_of_birth" not in scoped["person"]["native"]
    assert scoped["ownership_assertion"]["owner_scoped"] is True
    assert "Fixture Road" not in json.dumps(scoped["person"]) and "1981" not in json.dumps(scoped["person"])
    env = harness.Env()
    run_snapshot(tmp_path, env.conn, snapshot_web("2026-10-30"))
    other = views(env.conn, principal="someone-else")
    redacted = other["companies-house:psc-snapshot:09990003:SNAPPSC002"]
    assert redacted["redacted"] is True and "name" not in redacted["record"]

    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    research = {r["kind"]: r for r in parse_snapshot_line(person_line)}
    assert research["person"].get("owner_scoped") is None
    assert research["person"]["native"]["date_of_birth"] == {"month": 7, "year": 1981}
    assert research["person"]["native"]["policy"] == "ownership-person-research-v1"
    assert "Fixture Road" not in json.dumps(research["person"])  # addresses are never stored
    # A record produced under the mode is refused once the switch is off.
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "off")
    with pytest.raises(OwnershipRecordError) as caught:
        OwnershipStore(env.conn).apply(NS, [research["person"]], run_id="r", observed_at_ms=1, principal_id="p")
    assert caught.value.code == "minimisation_violation"


def test_corporate_psc_from_the_snapshot_resolves_into_the_graph_by_company_number(tmp_path):
    env = harness.Env().ready()
    run_snapshot(tmp_path, env.conn, snapshot_web("2026-10-30"))
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    parents = graph.direct_parents("companies-house:gb-coh:09990002", "2025-06-01")
    group = next(g for g in parents["groups"] if g["holder"] == graph.cluster("companies-house:gb-coh:09990001"))
    assert any(a["source"]["source_type"] == "bulk-snapshot" for a in group["assertions"])
    assert group["holder_entity"]["acquired"] is True  # the holder is the API-acquired company profile

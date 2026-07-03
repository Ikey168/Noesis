"""M3.2: a live ingest records each connector run as its own lineage entry
(connector, source, run id, fetched/written counts), and the investigation
audit trail replays it, so a live investigation is fully reconstructable."""

from datetime import datetime, timezone

from src.osint import investigation_audit
from src.provisioning import store
from src.provisioning.pipeline_runner import build_pipeline_runner
from src.provisioning.provisioner import Provisioner


def _clock():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _records(n):
    return [
        {"id": f"d{i}", "title": f"t{i}", "url": f"http://f/{i}", "content": "c",
         "publish_date": "2026-05-20", "source": "TestFeed"}
        for i in range(n)
    ]


def _ingest(conn):
    store.ensure_schema(conn)
    runner = build_pipeline_runner(conn, harvester=lambda ctype, cfg: _records(3))
    prov = Provisioner(conn, clock=_clock, pipeline_runner=runner)
    prov.deploy("op_daybreak", "an investigation", approve=True)
    prov.attach_pipeline(
        "op_daybreak", connector="grid-feed", connector_type="rss",
        config={"url": "http://feed", "source": "TestFeed"}, approve=True,
    )
    prov.attach_sources("op_daybreak", sources=["TestFeed"])
    return prov.ingest("op_daybreak")


def test_pipeline_run_is_recorded_in_lineage(conn):
    _ingest(conn)
    events = store.list_events(conn, "op_daybreak")
    runs = [e for e in events if e["event"] == "pipeline_run"]
    assert len(runs) == 1
    detail = runs[0]["detail"]
    assert detail["connector"] == "grid-feed"
    assert detail["source"] == "TestFeed"
    assert detail["fetched"] == 3 and detail["written"] == 3
    assert detail["ok"] is True
    assert detail["run_id"].startswith("op_daybreak:grid-feed:")


def test_investigation_audit_replays_the_connector_run(conn):
    _ingest(conn)
    audit = investigation_audit(conn, "op_daybreak")
    assert audit["reconstructable"] is True
    trail = [e["event"] for e in audit["audit_trail"]]
    # The connector run is on the trail, ahead of the ingest routing event.
    assert "pipeline_run" in trail
    assert trail.index("pipeline_run") < trail.index("ingest")
    run = next(e for e in audit["audit_trail"] if e["event"] == "pipeline_run")
    assert run["detail"]["written"] == 3

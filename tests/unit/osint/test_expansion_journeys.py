"""Offline acceptance journeys for the expanded OSINT pack (OX12, #2052; tracker #2040).

One reproducible journey per capability, against hand-written fixtures only.
The designations journey (OX03, #2043) enables the Legal bundle's
``sanctions`` feature through the composition coordinator and reads per-list
statements for an organization linked by an accepted identity decision.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.osint import GATED_TOOLS, corroborate, entity_dossier, source_reliability
from tests.unit.osint.test_infrastructure_sources import (
    NS,
    SCOPES,
    Clock,
    transport_for,
)

duckdb = pytest.importorskip("duckdb")
ROOT = Path(__file__).resolve().parents[3]


def test_journey_claim_to_corroboration_with_separated_grades(seed):
    seed.articles(
        [
            ("d1", "claim", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "support", "http://b/1", "Beta Journal", "2026-06-02"),
            ("d3", "support", "http://c/1", "Gamma Review", "2026-06-03"),
        ]
    )
    seed.claims([("k1", "The dam failed at dawn.", "d1", "news", 0.9, None)])
    seed.evidence(
        [
            ("e1", "k1", "d2", "news", "supports", 0.9),
            ("e2", "k1", "d3", "news", "supports", 0.8),
        ]
    )
    panel = corroborate(seed.conn, "k1")
    assert panel["credibility_grade"]["grade"] == 1
    assert (
        panel["source_reliability_grade"]["grade"] == "F"
    )  # one-document track record
    assert panel["grading"]["independent_axes"] is True
    assert source_reliability(seed.conn, "Alpha Wire")["reliability_grade"][
        "derivation"
    ]["rule_applied"]


def test_journey_organization_to_cited_ownership_dossier():
    from tests.unit.osint.test_dossier import OWN, _link
    from tests.unit.ownership import harness

    env = harness.Env().ready()
    from src.kb.ownership_identity import OwnershipIdentityService

    service = OwnershipIdentityService(env.conn, now=env.now)
    for item in service.propose(
        harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES
    )["candidates"]:
        decision = (
            "reject"
            if harness.DECOY in (item["left_key"], item["right_key"])
            else "accept"
        )
        service.review(
            harness.NS,
            item["candidate_id"],
            decision,
            "fixture review",
            principal_id=harness.REVIEWER,
            scopes=harness.REVIEW_SCOPES,
        )
    env.conn.execute(
        "INSERT INTO document_actors (document_id, source_type, actor_name, entity_id, role) "
        "VALUES ('d1', 'news', 'Exampla UK', 'kg:exampla-uk', 'organization')"
    )
    _link(env, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    dossier = entity_dossier(env.conn, "Exampla UK", ownership=OWN)
    section = dossier["ownership"]
    assert section["status"] == "assembled"
    assert all(
        a["citation"]["cited"]
        for g in section["direct_parents"]
        for a in g["assertions"]
    )
    # The designations section (OX03) is a separate, opt-in feature: not assembled unless requested.
    assert "designations" not in dossier
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    defaults = {f["id"]: f["default"] for f in composition["optional_features"]}
    assert defaults["ownership"] is False and defaults["designations"] is False
    env.conn.close()


@pytest.fixture()
def isolated_registry():
    from src.domains import registry as domain_registry

    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_journey_organization_to_cited_designation_statements(isolated_registry):
    from src.kb.sanctions import feature_enabled
    from src.kb.sanctions_identity import SanctionsIdentity
    from tests.unit import sanctions_harness as sh
    from tests.unit.composition.test_migration import _migrated

    conn, coordinator, bundles, _ = _migrated()
    designations = {
        "namespace": sh.NS,
        "principal_id": "analyst",
        "scopes": sorted(sh.SCOPES),
        "as_of": "2026-03-01",
    }
    conn.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    conn.execute(
        "INSERT INTO document_actors (document_id, source_type, actor_name, entity_id, role) "
        "VALUES ('d1', 'news', 'Examplar Freight LLC', 'org:examplar', 'organization')"
    )
    sh.load_legal(conn, "cellar-sanctions-acts-eng")
    for name in sh.FILES["eu"]:
        sh.apply(conn, "eu", name)
    identity = SanctionsIdentity(conn)
    designation = conn.execute(
        "SELECT designation_id FROM sanctions_designations WHERE list_entry_id='EU.9001.01'"
    ).fetchone()[0]
    offered = identity.propose_link(
        sh.NS,
        designation,
        target_key="osint:org:examplar",
        target_entity="org:examplar",
        evidence={
            "kind": "registration_number",
            "value": "1027700000001",
            "target_source": "registry extract",
        },
        principal_id="analyst",
        scopes=sh.SCOPES,
    )
    identity.service.review(
        sh.NS,
        offered["candidate_id"],
        "accept",
        "same registration number",
        principal_id="reviewer",
        scopes=sh.REVIEW_SCOPES,
    )
    # The Legal sanctions feature is off by default: the section is inert.
    assert feature_enabled(conn) is False
    inert = entity_dossier(conn, "Examplar Freight LLC", designations=designations)[
        "designations"
    ]
    assert inert["status"] == "inert"
    coordinator.select("legal", bundles["legal"]["version"], features=["sanctions"])
    assert coordinator.activate("osint-journey-sanctions")["status"] == "published"
    section = entity_dossier(conn, "Examplar Freight LLC", designations=designations)[
        "designations"
    ]
    assert section["status"] == "assembled" and list(section["lists"]) == ["eu"]
    line = section["lists"]["eu"][0]
    assert line["status"] == "listed" and line["citation"]["cited"]
    assert line["legal_basis"][0]["status"] == "resolved"
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    feature = next(
        f for f in composition["optional_features"] if f["id"] == "designations"
    )
    assert [r["capability"] for r in feature["requires"]] == ["legal.sanctions"]
    conn.close()


def test_journey_recycled_video_frame_across_two_documents(tmp_path):
    pytest.importorskip("PIL")
    from src.analytics.image_reuse import find_reuse
    from src.ingestion.assets.store import ImageAssetStore
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.keyframes import Keyframe
    from src.ingestion.connectors.media.models import MediaMetadata
    from tests.unit.analytics.test_image_reuse import _img, _rescale

    photo = _img(shift=11)

    conn = duckdb.connect()
    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    store.ingest(
        photo, document_id="news:photo-2019", context="archive photo", now_ms=1
    )
    connector = media_connector.MediaConnector(
        frame_sampler=lambda content, file_ext="mp4": [
            Keyframe(timestamp_s=42.0, image_bytes=_rescale(photo, 80))
        ],
        ocr=lambda b: None,
        asset_store=store,
    )
    original = media_connector.transcribe
    media_connector.transcribe = lambda c, **k: MediaMetadata(title="", segments=[])
    try:
        connector.parse(
            media_connector.RawDocument(
                ref=media_connector.SourceRef(
                    locator="https://ex.com/clip.mp4", title="clip"
                ),
                content=b"video",
                content_type="video/mp4",
            )
        )
    finally:
        media_connector.transcribe = original
    [finding] = find_reuse(conn)["findings"]
    assert (
        finding["distinct_document_count"] == 2
        and finding["includes_video_frames"] is True
    )
    frame = next(
        a for a in finding["appearances"] if a["appearance_kind"] == "video_frame"
    )
    assert frame["citation"]["offset_s"] == 42.0


def test_journey_domain_history_and_organization_pivot():
    from src.ingestion import crtsh, rdap
    from src.kb.source_identity import READ_SCOPE, SourceIdentityStore
    from src.osint import infrastructure_pivot, trace_artifact

    conn, clock = duckdb.connect(), Clock()
    store = SourceIdentityStore(conn, now=clock)
    ids = {}
    for name, domain in (
        ("Exampla News", "exampla-news.example"),
        ("Sister Daily", "sister-daily.example"),
    ):
        ident = store.register(
            NS, "publication", name, principal_id="analyst", scopes=SCOPES
        )
        store.decide_alias(
            NS,
            ident["source_id"],
            "domain",
            domain,
            reason="publisher masthead domain",
            reviewer_id="reviewer",
            scopes=SCOPES,
        )
        ids[domain] = ident["source_id"]
    r = rdap.acquire_rdap(
        conn,
        "exampla-news.example",
        request_id="j-r",
        transport=transport_for("rdap-domain-exampla-news.json"),
        now=clock,
    )
    c = crtsh.acquire_crtsh(
        conn,
        "exampla-news.example",
        request_id="j-c",
        transport=transport_for("crtsh-exampla-news.json"),
        now=clock,
    )
    rdap.project_rdap(
        conn, NS, r["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    projected = crtsh.project_crtsh(
        conn, NS, c["observation_id"], principal_id="analyst", scopes=SCOPES
    )
    history = store.get(
        NS, ids["exampla-news.example"], scopes={READ_SCOPE}, include_history=True
    )["revisions"]
    assert [
        bool(h["native_ids"].get("rdap:exampla-news.example:observation"))
        for h in history
    ] == [
        False,
        True,
        True,
    ]
    assert (
        history[-1]["native_ids"]["ct:exampla-news.example:observation"]
        == c["observation_id"]
    )
    assert projected["shared_infrastructure"][0]["policy"]["status"] == "probable"
    assert trace_artifact(conn, document_id=r["observation_id"])["chain"][-1][
        "identity_revisions"
    ]
    pivot = infrastructure_pivot(conn, "exampla-news.example", namespace=NS)
    assert ids["sister-daily.example"] in {p["reached"] for p in pivot["paths"]}
    for person_key in (
        "desk@exampla-news.example",
        "@exampla",
        "203.0.113.9",
        "person:x",
    ):
        assert (
            infrastructure_pivot(conn, person_key, namespace=NS)["status"]
            == "person_identifier_refused"
        )
    conn.close()


def test_journey_gate_open_suggestions_become_citable_only_on_confirmation(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    from src.ingestion.assets.store import ImageAssetStore
    from src.osint.imagery_gated import (
        ReferenceBudget,
        chronolocate_image,
        confirm_suggestion,
        fetch_reference_imagery,
        geolocate_image,
        list_review_queue,
    )

    corpus, queue = duckdb.connect(), duckdb.connect()
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 180, 20)).save(buf, format="PNG")
    sha = (
        ImageAssetStore(corpus, root=str(tmp_path / "figs"))
        .ingest(buf.getvalue(), document_id="doc:a")
        .sha256
    )
    geo = geolocate_image(
        corpus,
        sha,
        queue_conn=queue,
        vlm=lambda b: [
            {
                "landmark": "cathedral",
                "place": "Berlin",
                "lat": 52.52,
                "lon": 13.405,
                "confidence": 0.5,
            }
        ],
    )
    sid = geo["hypotheses"][0]["suggestion_id"]

    class Provider:
        name = "fake-sat"

        def __call__(self, request):
            return {
                "bytes": b"tile",
                "provider": "fake-sat",
                "attribution": "(c) fake",
                "terms_url": "https://t",
            }

    ref = fetch_reference_imagery(
        queue,
        kind="satellite",
        suggestion_id=sid,
        provider=Provider(),
        budget=ReferenceBudget(allowlist=("fake-sat",)),
    )
    assert ref["cited"] is False
    confirm_suggestion(
        queue, sid, operator="analyst-1", viewed_references=[ref["reference_id"]]
    )
    chrono = chronolocate_image(
        corpus,
        sha,
        date_from="2024-06-01",
        date_to="2024-06-30",
        suggestion_id=sid,
        queue_conn=queue,
        estimator=lambda b: {"shadow_azimuth_deg": 289.0, "shadow_length_ratio": 1.2},
    )
    assert chrono["status"] == "queued" and chrono["cited"] is False
    assert list_review_queue(queue, kind="chronolocation", cited=True)["count"] == 0
    confirm_suggestion(queue, chrono["suggestion_id"], operator="analyst-1")
    assert list_review_queue(queue, kind="chronolocation", cited=True)["count"] == 1


def test_journey_gate_closed_all_gated_tools_absent(monkeypatch):
    from tests.unit.osint.test_investigations import _served_tool_names

    served = _served_tool_names(monkeypatch, None)
    assert not set(GATED_TOOLS) & served
    assert {"chronolocate_image", "reference_imagery"} <= set(GATED_TOOLS)
    assert "infrastructure_pivot" in served

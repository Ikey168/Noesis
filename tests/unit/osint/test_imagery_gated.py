"""Unit tests for the gated imagery tier (C4): review-queue discipline."""

from __future__ import annotations

import io

import pytest

from src.osint.imagery_gated import (
    PERSON_IDENTIFICATION_SUPPORTED,
    confirm_suggestion,
    geolocate_image,
    list_review_queue,
    reverse_image_search,
)


@pytest.fixture()
def conn_with_asset(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("PIL")
    from PIL import Image

    from src.ingestion.assets.store import ImageAssetStore

    conn = duckdb.connect(":memory:")
    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    asset = store.ingest(buf.getvalue(), document_id="doc:a", now_ms=1)
    return conn, asset.sha256


def test_person_identification_is_permanently_off():
    assert PERSON_IDENTIFICATION_SUPPORTED is False


def test_reverse_search_no_default_provider(conn_with_asset):
    conn, sha = conn_with_asset
    res = reverse_image_search(conn, sha, provider=None)
    assert res["status"] == "no_provider_configured"


def test_reverse_search_rejects_non_corpus_hash(conn_with_asset):
    conn, _ = conn_with_asset
    res = reverse_image_search(conn, "0" * 64, provider=lambda b: [{"url": "http://x"}])
    assert res["status"] == "not_a_corpus_image"


def test_reverse_search_queues_uncited_suggestions(conn_with_asset):
    conn, sha = conn_with_asset
    def provider(b):
        return [{"url": "https://other.example/story", "title": "Elsewhere"}]
    res = reverse_image_search(conn, sha, provider=provider, now_ms=10)
    assert res["status"] == "queued"
    assert res["count"] == 1
    assert res["suggestions"][0]["cited"] is False
    # It is in the queue, uncited.
    queue = list_review_queue(conn, cited=False)
    assert queue["count"] == 1


def test_queue_writes_go_to_a_separate_store(conn_with_asset, tmp_path):
    # Least privilege: the corpus asset is read from `conn`, but the review-queue
    # write lands in a *separate* queue store — the corpus connection is never
    # written to (no queue table appears there).
    duckdb = pytest.importorskip("duckdb")
    corpus, sha = conn_with_asset
    queue = duckdb.connect(str(tmp_path / "queue.duckdb"))
    def provider(b):
        return [{"url": "https://other.example/story", "title": "Elsewhere"}]
    res = reverse_image_search(corpus, sha, provider=provider, now_ms=10, queue_conn=queue)
    assert res["status"] == "queued"
    # The suggestion is in the dedicated queue store...
    assert list_review_queue(queue, cited=False)["count"] == 1
    # ...and the corpus connection holds no review queue at all.
    corpus_tables = {
        r[0] for r in corpus.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert "imagery_review_queue" not in corpus_tables
    queue.close()


def test_geolocate_queue_write_goes_to_separate_store(conn_with_asset, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    corpus, sha = conn_with_asset
    queue = duckdb.connect(str(tmp_path / "queue.duckdb"))
    def vlm(b):
        return [{"landmark": "a bridge", "place": "somewhere", "confidence": 0.4}]
    res = geolocate_image(corpus, sha, vlm=vlm, now_ms=5, queue_conn=queue)
    assert res["status"] == "queued"
    assert list_review_queue(queue)["count"] == 1
    corpus_tables = {
        r[0] for r in corpus.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert "imagery_review_queue" not in corpus_tables
    queue.close()


def test_geolocate_image_no_backend(conn_with_asset):
    conn, sha = conn_with_asset
    assert geolocate_image(conn, sha, vlm=None)["status"] == "no_backend_configured"


def test_geolocate_image_queues_suggestion_grade(conn_with_asset):
    conn, sha = conn_with_asset
    def vlm(b):
        return [{"landmark": "a bridge", "place": "somewhere", "confidence": 0.4}]
    res = geolocate_image(conn, sha, vlm=vlm, now_ms=5)
    assert res["status"] == "queued"
    assert res["hypotheses"][0]["grade"] == "suggestion"
    assert res["hypotheses"][0]["cited"] is False


def test_confirmation_is_the_only_path_to_cited(conn_with_asset):
    conn, sha = conn_with_asset
    def provider(b):
        return [{"url": "https://other.example/story"}]
    res = reverse_image_search(conn, sha, provider=provider, now_ms=10)
    sid = res["suggestions"][0]["suggestion_id"]
    # Before confirmation: uncited.
    assert list_review_queue(conn, cited=True)["count"] == 0
    # Confirmation requires an operator identity.
    assert confirm_suggestion(conn, sid, operator="")["status"] == "rejected"
    # Operator confirms -> becomes cited.
    out = confirm_suggestion(conn, sid, operator="analyst-1", now_ms=20)
    assert out["status"] == "confirmed" and out["cited"] is True
    cited = list_review_queue(conn, cited=True)
    assert cited["count"] == 1
    assert cited["items"][0]["confirmed_by"] == "analyst-1"


def test_confirm_unknown_suggestion(conn_with_asset):
    conn, _ = conn_with_asset
    # Ensure the queue table exists first.
    reverse_image_search(conn, "0" * 64, provider=lambda b: [])
    assert confirm_suggestion(conn, "sug:nope", operator="x")["status"] in ("not_found",)


def test_tools_are_registered_as_gated():
    from src.osint.investigations import is_gated

    assert is_gated("reverse_image_search")
    assert is_gated("geolocate_image")


def test_video_keyframe_assets_add_nothing_to_the_gated_tier(tmp_path):
    """OX04: keyframes indexed from video are ordinary corpus assets. Indexing
    them adds no served tool, no gated tool, and no way around the gated tier's
    inert default."""
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore
    from src.ingestion.connectors.media.keyframes import Keyframe, index_keyframe_assets
    from src.osint.investigations import GATED_TOOLS

    conn = duckdb.connect(":memory:")
    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    [frame] = index_keyframe_assets(store, "media:v", [Keyframe(timestamp_s=1.0, image_bytes=b"frame")])
    assert reverse_image_search(conn, frame["sha256"], provider=None)["status"] == "no_provider_configured"
    assert geolocate_image(conn, frame["sha256"], vlm=None)["status"] == "no_backend_configured"
    assert not [t for t in GATED_TOOLS if "frame" in t or "video" in t]


# --- OX09 (#2049): chronolocate_image -----------------------------------------

import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from src.osint.imagery_gated import (  # noqa: E402
    ReferenceBudget,
    chronolocate_image,
    fetch_reference_imagery,
    interval_contains,
    list_references,
    solar_position,
)

CALIBRATION = Path(__file__).resolve().parents[2] / "fixtures/osint/chronolocation-calibration.json"
HYPOTHESIS = {"lat": 52.52, "lon": 13.405, "label": "Berlin (operator hypothesis)", "operator": "analyst-1"}


def _estimator(azimuth, ratio):
    def estimate(_image_bytes):
        return {"shadow_azimuth_deg": azimuth, "shadow_length_ratio": ratio, "season_cues": ["full foliage"]}

    return estimate


def test_solar_model_matches_textbook_noon_elevations():
    # Independent check: at local solar noon the sun's elevation is
    # 90 - latitude + declination (declination +23.44 at the June solstice).
    _, greenwich = solar_position(51.4769, 0.0, datetime(2024, 6, 20, 12, 2, tzinfo=UTC))
    assert abs(greenwich - (90 - 51.4769 + 23.44)) < 0.3
    _, nyc = solar_position(40.7128, -74.006, datetime(2024, 12, 21, 16, 55, tzinfo=UTC))
    assert abs(nyc - (90 - 40.7128 - 23.44)) < 0.5
    azimuth, _ = solar_position(40.7128, -74.006, datetime(2024, 12, 21, 16, 55, tzinfo=UTC))
    assert abs(azimuth - 180) < 3  # due south at solar noon, northern hemisphere


def test_chronolocation_has_no_default_backend(conn_with_asset):
    conn, sha = conn_with_asset
    out = chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30", hypothesis=HYPOTHESIS)
    assert out["status"] == "no_backend_configured"


def test_chronolocation_needs_a_confirmed_place_or_operator_hypothesis(conn_with_asset):
    conn, sha = conn_with_asset
    est = _estimator(289.0, 1.2)
    assert chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30",
                              estimator=est)["status"] == "place_required"
    # A hypothesis without an operator identity is not an operator hypothesis.
    assert chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30", estimator=est,
                              hypothesis={"lat": 52.5, "lon": 13.4})["status"] == "place_required"

    def vlm(_b):
        return [{"landmark": "TV tower", "place": "Berlin", "lat": 52.52, "lon": 13.405, "confidence": 0.5}]

    queued = geolocate_image(conn, sha, vlm=vlm, now_ms=1)["hypotheses"][0]["suggestion_id"]
    unconfirmed = chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30", estimator=est,
                                     suggestion_id=queued)
    assert unconfirmed["status"] == "place_not_confirmed"
    confirm_suggestion(conn, queued, operator="analyst-1", now_ms=2)
    confirmed = chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30", estimator=est,
                                   suggestion_id=queued, now_ms=3)
    assert confirmed["status"] == "queued"
    assert confirmed["place_hypothesis"]["basis"] == "confirmed geolocation suggestion"


def test_chronolocation_queues_an_uncited_interval_with_method_and_place(conn_with_asset):
    conn, sha = conn_with_asset
    out = chronolocate_image(conn, sha, date_from="2024-06-01", date_to="2024-06-30",
                             estimator=_estimator(289.0, 1.2), hypothesis=HYPOTHESIS, now_ms=5)
    assert out["status"] == "queued"
    assert out["cited"] is False and out["verified"] is False and out["grade"] == "suggestion"
    assert out["method"] and out["place_hypothesis"]["operator"] == "analyst-1"
    assert out["interval"]["time_band_utc"]["from"] <= "07:40" <= out["interval"]["time_band_utc"]["to"]
    assert out["interval"]["season_window"]["from"] >= "2024-06-01"
    assert "never used as ground truth" in out["file_claimed_note"]
    [item] = list_review_queue(conn, kind="chronolocation")["items"]
    assert item["cited"] is False
    assert confirm_suggestion(conn, item["suggestion_id"], operator="analyst-1")["cited"] is True


def test_chronolocation_writes_only_to_the_queue_store_and_logs_the_invocation(conn_with_asset, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    corpus, sha = conn_with_asset
    queue = duckdb.connect(str(tmp_path / "queue.duckdb"))
    out = chronolocate_image(corpus, sha, date_from="2024-06-01", date_to="2024-06-30",
                             estimator=_estimator(289.0, 1.2), hypothesis=HYPOTHESIS, queue_conn=queue)
    assert out["status"] == "queued"
    corpus_tables = {r[0] for r in corpus.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert "imagery_review_queue" not in corpus_tables and "provisioning_events" not in corpus_tables
    events = queue.execute("SELECT event FROM provisioning_events").fetchall()
    assert ("chronolocate_image",) in events
    queue.close()


def test_chronolocation_calibration_interval_hit_rate(conn_with_asset):
    """Gate criterion 2: on a fixture with known capture times, report how
    often the true time falls inside the suggested interval."""
    conn, sha = conn_with_asset
    cases = json.loads(CALIBRATION.read_text())["cases"]
    hits = 0
    for case in cases:
        out = chronolocate_image(
            conn, sha, date_from=case["date_from"], date_to=case["date_to"],
            estimator=_estimator(case["shadow_azimuth_deg"], case["shadow_length_ratio"]),
            hypothesis={"lat": case["lat"], "lon": case["lon"], "operator": "calibration"},
        )
        truth = datetime.fromisoformat(case["true_capture_utc"].replace("Z", "+00:00"))
        if out["status"] == "queued" and interval_contains(out["interval"], truth):
            hits += 1
    hit_rate = hits / len(cases)
    assert len(cases) >= 8
    assert hit_rate >= 0.875, f"interval hit-rate {hit_rate:.3f} on {len(cases)} calibration cases"


def test_chronolocation_never_joins_across_images():
    from inspect import signature

    params = set(signature(chronolocate_image).parameters)
    assert "sha256" in params
    assert not {"sha256s", "images", "person", "entity", "subject"} & params


# --- OX10 (#2050): reference imagery -------------------------------------------


class FakeReferenceProvider:
    name = "fake-sat"

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return {"bytes": b"reference-tile", "provider": "fake-sat", "attribution": "(c) Fake Sat",
                "terms_url": "https://fake-sat.example/terms"}


def _geo_suggestion(conn, sha, **extra):
    def vlm(_b):
        return [{"landmark": "bridge", "place": "Somewhere", "confidence": 0.4, **extra}]

    return geolocate_image(conn, sha, vlm=vlm, now_ms=1)["hypotheses"][0]["suggestion_id"]


def test_reference_imagery_is_inert_without_a_provider(conn_with_asset):
    conn, sha = conn_with_asset
    sid = _geo_suggestion(conn, sha, lat=48.85, lon=2.35)
    assert fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid)["status"] == "no_provider_configured"


def test_reference_fetch_attaches_to_the_suggestion(conn_with_asset):
    conn, sha = conn_with_asset
    sid = _geo_suggestion(conn, sha, lat=48.85, lon=2.35)
    provider = FakeReferenceProvider()
    budget = ReferenceBudget(allowlist=("fake-sat",), max_requests=5)
    out = fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid, provider=provider, budget=budget,
                                  now_ms=7)
    assert out["status"] == "attached" and out["cited"] is False
    assert out["attribution"] == "(c) Fake Sat" and out["terms_url"].startswith("https://")
    assert provider.requests[0]["bbox"][0] < 2.35 < provider.requests[0]["bbox"][2]
    [ref] = list_references(conn, sid)
    assert ref["reference_id"] == out["reference_id"] and ref["cited"] is False
    # No time series: a second fetch of the same kind for the same suggestion is refused.
    again = fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid, provider=provider, budget=budget)
    assert again["status"] == "already_fetched" and len(provider.requests) == 1
    # Confirmation records the references the operator viewed.
    confirmed = confirm_suggestion(conn, sid, operator="analyst-1", now_ms=9,
                                   viewed_references=[out["reference_id"], "ref:not-mine"])
    assert confirmed["viewed_references"] == [out["reference_id"]]
    assert list_references(conn, sid)[0]["viewed_by"] == "analyst-1"
    assert list_references(conn, sid)[0]["cited"] is False  # never a citation


def test_reference_fetch_is_refused_without_a_suggestion_or_place(conn_with_asset):
    conn, sha = conn_with_asset
    provider = FakeReferenceProvider()
    budget = ReferenceBudget(allowlist=("fake-sat",))
    assert fetch_reference_imagery(conn, kind="satellite", provider=provider,
                                   budget=budget)["status"] == "suggestion_required"
    assert fetch_reference_imagery(conn, kind="satellite", suggestion_id="sug:nope", provider=provider,
                                   budget=budget)["status"] == "suggestion_not_found"
    no_coords = _geo_suggestion(conn, sha)
    assert fetch_reference_imagery(conn, kind="street-level", suggestion_id=no_coords, provider=provider,
                                   budget=budget)["status"] == "place_required"
    hypothesis = {"lat": 40.0, "lon": -3.7, "operator": "analyst-1"}
    ok = fetch_reference_imagery(conn, kind="street-level", suggestion_id=no_coords, hypothesis=hypothesis,
                                 provider=provider, budget=budget)
    assert ok["status"] == "attached" and ok["place_basis"] == "operator hypothesis"
    assert provider.requests[-1] == {"kind": "street-level", "zoom": 16, "lat": 40.0, "lon": -3.7}


def test_reference_fetch_respects_allowlist_budget_and_byte_cap(conn_with_asset):
    conn, sha = conn_with_asset
    provider = FakeReferenceProvider()
    sid = _geo_suggestion(conn, sha, lat=1.0, lon=2.0)
    assert fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid, provider=provider,
                                   budget=ReferenceBudget(allowlist=()))["status"] == "provider_not_allowlisted"
    spent = ReferenceBudget(allowlist=("fake-sat",), max_requests=0)
    assert fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid, provider=provider,
                                   budget=spent)["status"] == "budget_exhausted"
    tiny = ReferenceBudget(allowlist=("fake-sat",), max_bytes=3)
    assert fetch_reference_imagery(conn, kind="satellite", suggestion_id=sid, provider=provider,
                                   budget=tiny)["status"] == "response_too_large"
    assert list_references(conn, sid) == []


def test_new_imagery_tools_are_gated():
    from src.osint.investigations import is_gated

    assert is_gated("chronolocate_image")
    assert is_gated("reference_imagery")


# --- OX11 (#2051): reverse-image adapter conformance ---------------------------

from tests.unit.osint.provider_conformance import assert_conformant, check_adapter  # noqa: E402


def conformant_factory(transport, corpus_conn):
    """A fake adapter that does everything the deployment guide requires."""

    def provider(image_bytes):
        if not isinstance(image_bytes, (bytes, bytearray)):
            raise TypeError("image bytes only")
        if len(image_bytes) > provider.max_bytes:
            raise ValueError("image exceeds byte cap")
        response = transport(image=bytes(image_bytes), timeout=provider.timeout_s)
        return [{"url": h["url"], "provider": "fake-reverse", "seen_at": h["seen_at"]} for h in response["hits"]]

    provider.max_bytes = 1_000_000
    provider.timeout_s = 10
    return provider


def nonconformant_factory(transport, corpus_conn):
    """Everything a wired adapter must not do: URL/person inputs, identity
    output, retries, no byte cap, and corpus writes."""

    def provider(image_bytes, url=None, person=None):
        for _attempt in range(3):
            try:
                response = transport(image=image_bytes, timeout=120)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            return []
        corpus_conn.execute("INSERT INTO image_assets VALUES ('leak')")
        return [{"url": h["url"], "name": "A. Person", "face_match": 0.93} for h in response["hits"]]

    provider.timeout_s = 120
    return provider


def test_conformant_adapter_passes_the_harness():
    assert_conformant(conformant_factory)


def test_nonconformant_adapter_fails_every_rule():
    violations = "\n".join(check_adapter(nonconformant_factory))
    for fragment in ("forbidden parameters", "accepted a URL", "max_bytes", "timeout_s", "identity fields",
                     "missing required fields", "retried", "swallowed", "wrote to the corpus"):
        assert fragment in violations, fragment


def test_conformant_hits_queue_uncited_with_provider_and_seen_at(conn_with_asset):
    from tests.unit.osint.provider_conformance import FakeTransport

    conn, sha = conn_with_asset
    adapter = conformant_factory(FakeTransport(), None)
    out = reverse_image_search(conn, sha, provider=adapter, now_ms=3)
    [hit] = out["suggestions"]
    assert hit["cited"] is False and hit["provider"] == "fake-reverse" and hit["seen_at"]

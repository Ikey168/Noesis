"""Static shell delivery checks for the market research workspace."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import market_routes
from src.api.routes.market_routes import router


def test_market_workspace_serves_same_origin_shell_and_fixed_assets():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    page = client.get("/api/v1/market/workspace")
    script = client.get("/api/v1/market/workspace/assets/app.js")
    styles = client.get("/api/v1/market/workspace/assets/styles.css")
    traversal = client.get("/api/v1/market/workspace/assets/%2e%2e%2fapp.py")

    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "session storage" in page.text
    assert "/api/v1/market/workspace/assets/app.js" in page.text
    assert script.status_code == styles.status_code == 200
    assert "sessionStorage" in script.text
    assert "company-dashboard" in script.text
    assert "instruments/lookup" in script.text
    assert "https://" not in script.text
    assert traversal.status_code == 404


def test_market_brief_evidence_bundle_route_uses_shared_capability(monkeypatch):
    captured = {}

    async def fake_run(operation, arguments, claims):
        captured.update(
            {"operation": operation, "arguments": arguments, "claims": claims}
        )
        return {"ok": True, "result": {"contract": "noesis-evidence-bundle-v1"}}

    monkeypatch.setattr(market_routes, "_run", fake_run)
    response = asyncio.run(
        market_routes.export_market_brief_evidence_bundle(
            {"namespace": "market:fixture", "report_id": "brief:fixture"},
            {"sub": "analyst:fixture"},
        )
    )

    assert response["ok"] is True
    assert captured["operation"] == "export_market_brief_evidence_bundle"
    assert captured["arguments"]["report_id"] == "brief:fixture"
    assert captured["claims"]["sub"] == "analyst:fixture"
    assert any(
        route.path == "/api/v1/market/research/brief/evidence-bundle"
        and "POST" in route.methods
        for route in router.routes
    )


def test_market_acceptance_review_route_uses_shared_capability(monkeypatch):
    captured = {}

    async def fake_run(operation, arguments, claims):
        captured.update(
            {"operation": operation, "arguments": arguments, "claims": claims}
        )
        return {"ok": True, "result": {"contract": "noesis-market-acceptance-review-v1"}}

    monkeypatch.setattr(market_routes, "_run", fake_run)
    body = {
        "namespace": "market:fixture",
        "journey_id": "journey:fixture",
        "reviewed_at_ms": 1000,
    }
    response = asyncio.run(
        market_routes.review_market_acceptance_journey(
            body,
            {"sub": "analyst:fixture"},
        )
    )

    assert response["ok"] is True
    assert captured["operation"] == "review_market_acceptance_journey"
    assert captured["arguments"] == body
    assert captured["claims"]["sub"] == "analyst:fixture"
    assert any(
        route.path == "/api/v1/market/research/acceptance-journey/review"
        and "POST" in route.methods
        for route in router.routes
    )

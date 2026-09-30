"""Offline harness for the Geospatial infrastructure feature: acquire authored fixtures through the real adapters."""

from __future__ import annotations

from src.ingestion import infrastructure_sources as src
from tests.unit.infrastructure import fixture_builder as fb

NS = "infrastructure"
SCOPES = {"knowledge:infrastructure:read", "knowledge:infrastructure:write", "knowledge:infrastructure:review",
          f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{NS}:admin",
          "knowledge:subscriptions:read", "knowledge:subscriptions:write",
          "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
          "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ownership:review",
          "knowledge:energy:read", "namespace:energy:read", "knowledge:legal:read", "namespace:legal:read",
          "knowledge:environment:read", "namespace:environment:read",
          "namespace:ownership:read", "namespace:ownership:write"}


def acquire(conn, name, *, namespace=NS, scopes=SCOPES, selection=None):
    provider, default = fb.selection(name)
    fixture = fb.load(name)
    return src.acquire(conn, provider, selection or default, namespace=namespace, scopes=scopes,
                       principal_id="analyst", fetch=src.fixture_transport(fixture["native_pages"]),
                       now=src.fixture_clock(fixture))


def acquire_all(conn, names=None, **kwargs):
    return {name: acquire(conn, name, **kwargs) for name in names or list(fb.SELECTIONS)}

"""Offline regression-fixture runner for context assembly."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.kb.context import assemble_context, evaluate_cases


class _FixtureBacking:
    def __init__(self, domain: str, fixture: dict[str, Any], conn: Any = None) -> None:
        self.definition = SimpleNamespace(
            name=domain,
            embedding_model=fixture.get("embedding_model"),
        )
        self._surfaces = fixture.get("surfaces") or {}
        self.conn = conn

    def search(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._surfaces.get("lexical") or ())[:limit]

    def semantic_search(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._surfaces.get("semantic") or ())[:limit]

    def documents(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._surfaces.get("document") or ())[:limit]

    def claims(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._surfaces.get("claim") or ())[:limit]

    def entities(self, _name: str | None = None) -> list[dict[str, Any]]:
        return list(self._surfaces.get("entity") or ())

    def relations(self, _query: str, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._surfaces.get("graph") or ())[:limit]

    def quantitative_search(
        self, _query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return list(self._surfaces.get("quantitative") or ())[:limit]


def evaluate_fixture(path: str | Path) -> dict[str, Any]:
    """Run deterministic offline cases and return the normal regression report."""

    fixture = json.loads(Path(path).read_text())
    results = []
    for case in fixture.get("cases") or ():
        domain = (case["request"].get("domains") or ["fixture"])[0]
        conn = None
        lineage = case.get("lineage") or {}
        if lineage:
            import duckdb

            from src.osint.independence import (
                METHOD_VERSION,
                ensure_independence_schema,
            )

            conn = duckdb.connect()
            ensure_independence_schema(conn)
            for document_id, link in lineage.items():
                conn.execute(
                    "INSERT INTO document_origin_links VALUES "
                    "(?, 'fixture', ?, ?, ?, 0.9, 0.8, 1.0, '[]', '[]', 1, 'run')",
                    [
                        document_id,
                        METHOD_VERSION,
                        link["origin_id"],
                        link["state"],
                    ],
                )
        backing = _FixtureBacking(domain, case, conn)

        assemble = partial(assemble_context, [(domain, backing)])
        results.extend(evaluate_cases(assemble, [case])["cases"])
        if conn is not None:
            conn.close()
    return {
        "evaluation_contract": "noesis-context-regression-v1",
        "fixture": str(path),
        "cases": results,
        "passed": bool(results) and all(item["passed"] for item in results),
        "n": len(results),
    }


__all__ = ["evaluate_fixture"]

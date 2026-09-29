"""Trade-flow context for controlled-goods categories: Comext vintages and a sourced correlation table (#1907, S06).

Comext flows are acquired through the existing Eurostat dataset connector
(:class:`src.ingestion.connectors.dataset.eurostat.EurostatConnector`, the
Comext path of the dissemination API) into the existing dataset store via
:func:`src.domains.economic.model.register_series`, one vintage per provider
update: re-acquiring an unchanged cube adds no vintage and keeps the first
retrieval time. ``economics.knowledge`` (``economic_research``) answers the
series with the vintage cited.

A control code (e.g. ``1C350``) and a customs product code (CN8/HS6) are
different classifications. Their mapping is stored as an explicit, sourced
correlation table with its own revisions and is always labelled a *lookup
aid*: it never states that goods under a customs code are controlled.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.kb.sanctions import (
    READ_SCOPE,
    WRITE_SCOPE,
    SanctionsError,
    authorize,
    canonical,
    digest,
)

COMEXT_SELECTION = {
    "source_id": "eurostat-comext-controlled-goods",
    "publisher": "Eurostat (Comext, international trade in goods)",
    "endpoint": "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data",
    "datasets": ["DS-045409"],
    "dimensions": ["reporter", "partner", "product", "flow", "indicators", "freq"],
    "product_schemes": ["CN8", "HS6"],
    "budgets": {"timeout_ms": 30000, "max_bytes": 2_000_000, "max_series": 20},
    "auth": "none",
    "license": "Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required",
    "live_verification": "unverified-live",
    "verify": "dataset code DS-045409 and the Comext JSON-stat path are recorded from Eurostat documentation and "
    "need a dated live run",
    "fixture": "tests/fixtures/sanctions/comext_ds045409_v1.json",
}
ALLOWED_HOSTS = frozenset({"ec.europa.eu"})
LOOKUP_AID = (
    "A control code and a customs product code are different classifications; this correlation is a "
    "lookup aid for trade context and does not state that goods under a product code are controlled."
)
_DDL = """
CREATE TABLE IF NOT EXISTS sanctions_trade_correlations (
  namespace TEXT NOT NULL, table_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
  table_json TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, table_id, revision)
);
"""


def guarded_http_get(
    *,
    max_bytes: int = 2_000_000,
    timeout_s: float = 30.0,
    transport: Callable[..., Mapping[str, Any]] | None = None,
) -> Callable[[str], str]:
    """An ``http_get`` for the Eurostat connector on the runtime transport policy (same-host redirects only)."""
    from functools import partial

    from src.ingestion.source_pack_runtime import HTTPSPageAdapter
    from src.ingestion.source_packs import SourcePackError

    send = transport or partial(HTTPSPageAdapter._request, max_bytes=max_bytes)

    def get(url: str) -> str:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or (parts.hostname or "").casefold() not in ALLOWED_HOSTS
        ):
            raise SourcePackError(
                "network_policy", "Comext is fetched only from ec.europa.eu over HTTPS"
            )
        response = send(
            url=url,
            params={},
            headers={"Accept": "application/json"},
            timeout=timeout_s,
        )
        final = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final not in ALLOWED_HOSTS:
            raise SourcePackError("network_policy", "Comext answered from another host")
        status = int(response.get("status", 200))
        if status != 200:
            raise SourcePackError(
                "source_unavailable" if status >= 500 else "schema_drift",
                f"Comext returned HTTP {status}",
            )
        content = response.get("content", b"")
        raw = content if isinstance(content, bytes) else str(content).encode()
        if len(raw) > max_bytes:
            raise SourcePackError(
                "response_too_large", "Comext response exceeds its byte limit"
            )
        return raw.decode("utf-8")

    return get


class SanctionsTrade:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        import time

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ correlation table

    def record_correlations(
        self,
        namespace: str,
        table: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Store a sourced control-code-to-product-code table; a changed table is a new revision."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table_id = str(table.get("table_id") or "").strip()
        source = dict(table.get("source") or {})
        rows = [dict(r) for r in table.get("rows") or []]
        if not table_id or not source.get("citation") or not rows:
            raise SanctionsError(
                "invalid_table",
                "a correlation table needs an id, a source citation and rows",
            )
        for row in rows:
            if (
                not row.get("control_code")
                or not row.get("product_code")
                or row.get("product_scheme") not in {"CN8", "HS6"}
            ):
                raise SanctionsError(
                    "invalid_table",
                    "rows name a control_code, a product_code and a CN8/HS6 scheme",
                )
        body = {
            "table_id": table_id,
            "title": table.get("title"),
            "source": source,
            "rows": sorted(rows, key=canonical),
            "status": "lookup-aid",
        }
        content_hash = digest(body)
        latest = self.conn.execute(
            "SELECT revision, content_hash FROM sanctions_trade_correlations WHERE namespace=? AND table_id=? "
            "ORDER BY revision DESC LIMIT 1",
            [namespace, table_id],
        ).fetchone()
        if latest and latest[1] == content_hash:
            return {"table_id": table_id, "revision": latest[0], "status": "unchanged"}
        revision = 1 if latest is None else int(latest[0]) + 1
        self.conn.execute(
            "INSERT INTO sanctions_trade_correlations VALUES (?,?,?,?,?,?,?)",
            [
                namespace,
                table_id,
                revision,
                content_hash,
                canonical(body),
                principal_id,
                self.now(),
            ],
        )
        return {"table_id": table_id, "revision": revision, "status": "recorded"}

    def correlations(self, namespace: str, control_code: str) -> list[dict[str, Any]]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='sanctions_trade_correlations'"
        ).fetchone():
            return []
        rows = self.conn.execute(
            "SELECT t.table_id, t.revision, t.table_json FROM sanctions_trade_correlations t WHERE t.namespace=? AND "
            "t.revision=(SELECT max(x.revision) FROM sanctions_trade_correlations x WHERE x.namespace=t.namespace "
            "AND x.table_id=t.table_id) ORDER BY t.table_id",
            [namespace],
        ).fetchall()
        code = str(control_code or "").strip().upper()
        found = []
        for table_id, revision, body in rows:
            table = json.loads(body)
            for row in table["rows"]:
                if str(row["control_code"]).upper() == code:
                    found.append(
                        {
                            "table_id": table_id,
                            "table_revision": revision,
                            "source": table["source"],
                            "status": "lookup-aid",
                            **row,
                        }
                    )
        return found

    # ------------------------------------------------------------ Comext acquisition

    def acquire(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        http_get: Callable[[str], str] | None = None,
        fetched_at_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Acquire bounded Comext flows as vintaged series; an unchanged provider update adds no vintage."""
        from src.domains.economic.model import ensure_economic_schema, register_series
        from src.ingestion.connectors.dataset.eurostat import EurostatConnector

        if not 1 <= len(specs) <= int(COMEXT_SELECTION["budgets"]["max_series"]):
            raise SanctionsError(
                "unbounded_request", "select 1-20 Comext flows per acquisition"
            )
        ensure_economic_schema(self.conn)
        connector = EurostatConnector(
            http_get=http_get
            or guarded_http_get(
                max_bytes=int(COMEXT_SELECTION["budgets"]["max_bytes"]),
                timeout_s=int(COMEXT_SELECTION["budgets"]["timeout_ms"]) / 1000,
            )
        )
        results = []
        for spec in specs:
            spec = dict(spec)
            if (
                spec.get("dataset") not in COMEXT_SELECTION["datasets"]
                or not spec.get("product")
                or not spec.get("geography")
            ):
                raise SanctionsError(
                    "invalid_request",
                    "Comext flows name a selected dataset, a reporter "
                    "(geography) and a product code",
                )
            ref = next(iter(connector.discover({**spec, "api": "comext"})))
            raw = connector.fetch(ref)
            if fetched_at_ms is not None:
                raw.fetched_at = int(fetched_at_ms)
            for record in connector.parse(raw):
                exists = self.conn.execute(
                    "SELECT vintage_id FROM economic_vintages WHERE domain='economics' AND series_id=? AND as_of=?",
                    [record.series_id, record.as_of],
                ).fetchone()
                if exists:
                    results.append(
                        {
                            "series_id": record.series_id,
                            "vintage_id": exists[0],
                            "status": "unchanged",
                        }
                    )
                    continue
                register_series(
                    self.conn,
                    record,
                    domain="economics",
                    semantics={
                        "concept": f"comext {spec['dataset']} trade flow {spec['product']}",
                        "canonical_name": f"Comext {spec['dataset']} {spec['geography']} product {spec['product']}",
                        "provider_code": spec["dataset"],
                        "definition": f"Eurostat Comext {spec['dataset']} as published; dimensions "
                        + canonical(
                            {k: v for k, v in sorted(spec.items()) if k != "dataset"}
                        ),
                    },
                )
                results.append(
                    {
                        "series_id": record.series_id,
                        "vintage_id": record.metadata.get("vintage_id"),
                        "status": "new_vintage",
                        "provider_updated_at_ms": record.as_of,
                    }
                )
        return results

    # ------------------------------------------------------------ context answer

    def context(
        self,
        namespace: str,
        control_code: str,
        *,
        scopes: Iterable[str],
        backing: Any = None,
    ) -> dict[str, Any]:
        """Correlated product codes (lookup aid) and the acquired flows for them, each with its vintages cited."""
        authorize(namespace, set(scopes), READ_SCOPE)
        from src.domains.economic.queries import economic_research

        correlations = self.correlations(namespace, control_code)
        products = sorted(
            {(c["product_scheme"], c["product_code"]) for c in correlations}
        )
        series = []
        for scheme, product in products:
            rows = (
                self.conn.execute(
                    "SELECT series_id FROM economic_series_map WHERE domain='economics' AND starts_with(series_id, "
                    "'estat-comext:') ORDER BY series_id"
                ).fetchall()
                if self.conn.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name='economic_series_map'"
                ).fetchone()
                else []
            )
            for (series_id,) in rows:
                if f"product={product}" not in series_id.split(":"):
                    continue
                entry = {
                    "product_scheme": scheme,
                    "product_code": product,
                    "series_id": series_id,
                }
                if backing is not None:
                    entry["trend"] = economic_research(
                        backing, query_type="trend", series_ids=[series_id]
                    )
                    entry["vintage_comparison"] = economic_research(
                        backing, query_type="vintage_comparison", series_ids=[series_id]
                    )
                entry["vintages"] = [
                    dict(zip(("as_of", "vintage_id", "retrieved_at_ms"), r))
                    for r in self.conn.execute(
                        "SELECT as_of, vintage_id, retrieved_at_ms FROM economic_vintages"
                        " WHERE domain='economics' AND series_id=? ORDER BY as_of",
                        [series_id],
                    ).fetchall()
                ]
                series.append(entry)
        return {
            "control_code": str(control_code).upper(),
            "correlations": correlations,
            "flows": series,
            "status": "found"
            if series
            else "no_acquired_flows"
            if correlations
            else "no_correlation",
            "lookup_aid_notice": LOOKUP_AID,
            "live_verification": COMEXT_SELECTION["live_verification"],
        }

"""
Data-plane latency benchmark (MCP rearchitecture plan, R12 #621 / Stage 3 gate).

Measures the cost of serving one panel family's data through the MCP layer
versus the direct in-process warehouse read the REST route uses. Three paths,
same query, same warehouse:

  rest_direct   the SQL the /api/v1/news/articles route runs, in-process
                (the REST baseline, minus HTTP framing common to both)
  mcp_tool      the articles_data data-mode tool over a live FastMCP client
                (the raw MCP transport + tool cost)
  proxy         invoke_data_tool(), the /api/v1/ui/data proxy handler path
                (allowlist + host cache + tool), through the same host

Reports p50 / p95 / mean latency and payload size for each, and the proxy
overhead versus the REST baseline. The numbers printed here are transcribed
into docs/architecture/ADR-002-data-plane-stage3.md.

Run:  python scripts/genui/dataplane_benchmark.py [iterations]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)
os.environ.pop("TESTING", None)
os.environ["NOESIS_GENUI_DATA_PROXY"] = "on"

N_ROWS = 200


def _seed(db: str) -> None:
    import duckdb

    con = duckdb.connect(db)
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    con.executemany(
        "INSERT INTO news_articles (id, title, url, publish_date, source, category, "
        "sentiment_score, sentiment_label) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                f"a{i}",
                f"Article {i} on grid policy and market dynamics",
                f"http://example.com/{i}",
                "2026-06-%02d" % ((i % 28) + 1),
                ["Alpha Wire", "Beta Journal", "Gamma Review"][i % 3],
                ["energy", "tech", "policy"][i % 3],
                (i % 10) / 10.0 - 0.5,
                ["positive", "neutral", "negative"][i % 3],
            )
            for i in range(N_ROWS)
        ],
    )
    con.close()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _rest_direct(db: str) -> dict:
    """The SQL the REST articles route runs, in-process."""
    import duckdb

    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            "SELECT id, title, url, publish_date, source, category, "
            "sentiment_score, sentiment_label FROM news_articles "
            "ORDER BY publish_date DESC NULLS LAST LIMIT 200"
        ).fetchall()
        return {
            "count": len(rows),
            "articles": [
                {
                    "id": r[0], "title": r[1], "url": r[2],
                    "publish_date": r[3].isoformat() if r[3] else None,
                    "source": r[4], "category": r[5],
                    "sentiment_score": r[6], "sentiment_label": r[7],
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def _stats(samples_ms):
    s = sorted(samples_ms)
    n = len(s)
    p50 = s[n // 2]
    p95 = s[min(n - 1, int(n * 0.95))]
    return {"p50": p50, "p95": p95, "mean": sum(s) / n, "n": n}


def _time_it(fn, iterations):
    # Warm up (connect, cache, JIT of the query plan).
    for _ in range(3):
        fn()
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


async def _run(db: str, iterations: int):
    from fastmcp.client import Client

    from src.genui import dataplane
    import src.mcp_host.host as host_mod
    from src.mcp_host import MCPHost
    from src.mcp_host.config import load_server_specs

    pipeline = _load("pl_srv", REPO_ROOT / "tools/pipeline_mcp/server.py")

    # rest_direct
    rest_samples = _time_it(lambda: _rest_direct(db), iterations)
    rest_payload = len(json.dumps(_rest_direct(db)).encode())

    # mcp_tool: raw FastMCP client call to the data-mode tool.
    async with Client(pipeline.mcp) as client:
        async def call():
            return (await client.call_tool("articles_data", {"limit": 200})).structured_content

        await call()
        mcp_payload = len(json.dumps(await call()).encode())
        mcp_samples = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            await call()
            mcp_samples.append((time.perf_counter() - t0) * 1000.0)

    # proxy: the /api/v1/ui/data handler path over a supervised host.
    specs = [s for s in load_server_specs() if s.name == "neuronews-pipeline"]
    host = MCPHost(specs=specs)
    host.start()
    host_mod._host = host
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and host.status()["connected"] != 1:
        time.sleep(0.5)
    proxy_samples, proxy_payload = [], 0
    try:
        host.invalidate_cached_calls()
        proxy_payload = len(
            json.dumps(
                dataplane.invoke_data_tool("neuronews-pipeline", "articles_data", {"limit": 200})
            ).encode()
        )
        for _ in range(iterations):
            host.invalidate_cached_calls()  # measure the live path, not the cache
            t0 = time.perf_counter()
            dataplane.invoke_data_tool("neuronews-pipeline", "articles_data", {"limit": 200})
            proxy_samples.append((time.perf_counter() - t0) * 1000.0)
        # And the warm cache path (what repeat panel loads actually hit).
        dataplane.invoke_data_tool("neuronews-pipeline", "articles_data", {"limit": 200})
        cached_samples = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            dataplane.invoke_data_tool("neuronews-pipeline", "articles_data", {"limit": 200})
            cached_samples.append((time.perf_counter() - t0) * 1000.0)
    finally:
        host.stop()
        host_mod._host = None

    return {
        "rows": N_ROWS,
        "iterations": iterations,
        "rest_direct": {**_stats(rest_samples), "payload_bytes": rest_payload},
        "mcp_tool": {**_stats(mcp_samples), "payload_bytes": mcp_payload},
        "proxy_cold": {**_stats(proxy_samples), "payload_bytes": proxy_payload},
        "proxy_cached": _stats(cached_samples),
    }


def main() -> None:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "wh.duckdb")
    os.environ["NEURONEWS_DB_PATH"] = db
    _seed(db)
    result = asyncio.run(_run(db, iterations))

    print(f"\nData-plane benchmark ({result['rows']} rows, {result['iterations']} iterations)\n")
    header = f"{'path':<14}{'p50 ms':>9}{'p95 ms':>9}{'mean ms':>9}{'payload B':>11}"
    print(header)
    print("-" * len(header))
    for path in ("rest_direct", "mcp_tool", "proxy_cold", "proxy_cached"):
        s = result[path]
        pb = s.get("payload_bytes", "")
        print(f"{path:<14}{s['p50']:>9.2f}{s['p95']:>9.2f}{s['mean']:>9.2f}{str(pb):>11}")

    rest_p50 = result["rest_direct"]["p50"]
    print(
        f"\nproxy_cold p50 overhead vs rest_direct: "
        f"+{result['proxy_cold']['p50'] - rest_p50:.2f} ms "
        f"({result['proxy_cold']['p50'] / rest_p50:.1f}x)"
    )
    print(
        f"proxy_cached p50 overhead vs rest_direct: "
        f"+{result['proxy_cached']['p50'] - rest_p50:.2f} ms "
        f"({result['proxy_cached']['p50'] / rest_p50:.1f}x)"
    )
    print("\nJSON:\n" + json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

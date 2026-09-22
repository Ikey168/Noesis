"""Optional Brave candidate discovery; snippets never become evidence documents."""

import json
from urllib.parse import urlsplit

from src.ingestion.connectors.base import SourceRef
from src.ingestion.source_pack_runtime import HTTPSPageAdapter


class BraveDiscovery:
    def __init__(
        self, *, api_key, transport=None, per_request_cost_micros, budget_micros
    ):
        if not api_key:
            raise ValueError("NOESIS_BRAVE_API_KEY is required")
        if (
            not isinstance(per_request_cost_micros, int)
            or per_request_cost_micros < 0
            or not isinstance(budget_micros, int)
            or budget_micros < 0
        ):
            raise ValueError("explicit integer cost ceiling required")
        self.key, self.transport = api_key, transport or HTTPSPageAdapter._request
        self.price, self.budget, self.spent = per_request_cost_micros, budget_micros, 0
        self.receipts = []

    def discover(
        self,
        query,
        *,
        freshness=None,
        language="en",
        country="US",
        max_pages=1,
        max_results=20,
    ):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("invalid query")
        if not 1 <= max_pages <= 10 or not 1 <= max_results <= 200:
            raise ValueError("discovery budget exceeded")
        seen = set()
        for offset in range(max_pages):
            if self.spent + self.price > self.budget:
                raise ValueError("discovery cost budget exhausted")
            params = {
                "q": query,
                "search_lang": language,
                "country": country,
                "offset": offset,
                "count": min(20, max_results - len(seen)),
            }
            if freshness:
                params["freshness"] = freshness
            self.spent += (
                self.price
            )  # Conservative charge even when the response fails.
            response = self.transport(
                url="https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers={
                    "X-Subscription-Token": self.key,
                    "Accept": "application/json",
                },
                timeout=15,
            )
            if response.get("status", 200) != 200:
                raise ValueError("Brave request failed")
            raw = response.get("content", b"")
            if len(raw) > 5_000_000:
                raise ValueError("Brave response exceeds byte budget")
            payload = json.loads(raw)
            results = payload.get("web", {}).get("results", [])
            if not isinstance(results, list) or len(results) > params["count"]:
                raise ValueError("invalid Brave results")
            self.receipts.append(
                {
                    "provider": "brave",
                    "query": query,
                    "parameters": params,
                    "results": len(results),
                    "reserved_cost_micros": self.price,
                }
            )
            for item in results:
                url = item.get("url", "")
                if urlsplit(url).scheme not in ("https", "http") or url in seen:
                    continue
                seen.add(url)
                yield SourceRef(
                    url,
                    title=item.get("title"),
                    metadata={
                        "discovery_provider": "brave",
                        "discovery_query": query,
                        "discovery_snippet": item.get("description"),
                        "requires_source_acquisition": True,
                    },
                )
            if len(seen) >= max_results or not payload.get("query", {}).get(
                "more_results_available", False
            ):
                return

    def discover_for_project(
        self,
        projects,
        namespace,
        project_id,
        request_id,
        query,
        *,
        principal_id,
        scopes,
        **options,
    ):
        """Reserve through the existing project ledger before any billable request.

        A crash with a held reservation and no result is deliberately not retried:
        provider usage is unknown until reviewed. Completed results replay locally.
        """
        from dataclasses import asdict

        from src.kb.research_projects import _hash

        pages = int(options.get("max_pages", 1))
        if not 1 <= pages <= 10:
            raise ValueError("invalid page budget")
        config = {
            "query": query,
            "options": options,
            "per_request_cost_micros": self.price,
        }
        reservation = "brave:" + str(request_id) + ":" + _hash(config)
        costs = {"requests": pages, "usd_micros": pages * self.price}
        auth = {"principal_id": principal_id, "scopes": scopes}
        projects.conn.execute(
            "CREATE TABLE IF NOT EXISTS brave_project_discoveries(project_id TEXT,request_id TEXT,config_hash TEXT,result_json TEXT,PRIMARY KEY(project_id,request_id))"
        )
        projects.inspect(namespace, project_id, **auth)
        previous = projects.conn.execute(
            "SELECT config_hash,result_json FROM brave_project_discoveries WHERE project_id=? AND request_id=?",
            [project_id, request_id],
        ).fetchone()
        if previous and previous[0] != _hash(config):
            raise ValueError("discovery request identity conflict")
        held = projects.reserve_budget(
            namespace, project_id, reservation, costs, **auth
        )
        if previous and previous[1]:
            projects.settle_budget(namespace, project_id, reservation, costs, **auth)
            return json.loads(previous[1])
        if held["idempotent"]:
            raise ValueError(
                "provider usage uncertain; held reservation requires review before retry"
            )
        projects.conn.execute(
            "INSERT INTO brave_project_discoveries VALUES(?,?,?,NULL)",
            [project_id, request_id, _hash(config)],
        )
        try:
            refs = [asdict(ref) for ref in self.discover(query, **options)]
            result = {
                "status": "completed",
                "candidates": refs,
                "receipts": self.receipts,
                "reservation_id": reservation,
            }
        except Exception as exc:  # noqa: BLE001 - preserve bounded acquisition failure outcome
            result = {
                "status": "failed",
                "failure_type": type(exc).__name__,
                "reservation_id": reservation,
            }
        projects.conn.execute(
            "UPDATE brave_project_discoveries SET result_json=? WHERE project_id=? AND request_id=?",
            [json.dumps(result), project_id, request_id],
        )
        projects.settle_budget(namespace, project_id, reservation, costs, **auth)
        return result

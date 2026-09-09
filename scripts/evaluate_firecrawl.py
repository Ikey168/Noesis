"""One opt-in public Firecrawl scrape with durable replay and credit accounting."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

from src.ingestion.extract import extract_article
from src.ingestion.hosted_acquisition import HOSTS, HostedClient
from src.ingestion.provider_execution import DurableHTTP, ProviderError


def credits(token):
    request = urllib.request.Request(
        "https://api.firecrawl.dev/v2/team/credit-usage",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    remaining = value.get("data", {}).get("remainingCredits")
    if value.get("success") is not True or type(remaining) is not int:
        raise ValueError("provider did not return a usable credit balance")
    return remaining


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--network-approved", action="store_true")
    parser.add_argument("--max-usd-micros", type=int, required=True)
    args = parser.parse_args()
    if not args.network_approved or args.max_usd_micros <= 0:
        parser.error(
            "explicit network approval and positive reservation ceiling required"
        )
    token = os.environ["FIRECRAWL_API_KEY"]
    before = credits(token)
    if before < 1:
        parser.error(
            "at least one existing credit required; this script does not purchase credits"
        )
    report = {
        "contract": "noesis-firecrawl-evaluation-v1",
        "api_version": "v2",
        "source_url": args.url,
        "remaining_credits_before": before,
        "maximum_scrape_requests": 1,
        "proxy": "basic",
        "pdf_parsers": [],
        "provider_cache_storage": False,
    }
    options = {
        "budget_id": "firecrawl-one-page",
        "provider": "firecrawl",
        "principal_id": "evaluation",
        "allowed_hosts": HOSTS["firecrawl"],
        "reuse_notice": "Public-page evaluation; publisher and Firecrawl terms apply",
        "max_requests": 1,
        "max_usd_micros": args.max_usd_micros,
    }
    with duckdb.connect(str(args.database)) as conn:
        http = DurableHTTP(conn, **options)
        client = HostedClient(
            http,
            principal_id="evaluation",
            enabled=True,
            credential=token,
            max_cost_per_request_micros=args.max_usd_micros,
        )
        try:
            result = client.scrape(
                args.url,
                "public-page",
                allowed_source_hosts=[urlsplit(args.url).hostname],
                public_url_approved=True,
            )
            extracted = extract_article(result["html"], url=args.url)
            report.update(
                status="captured",
                capture=result,
                extracted_text=extracted.text if extracted else "",
                extraction_metadata=extracted.metadata if extracted else None,
            )
        except ProviderError as exc:
            report.update(status="failed", failure_code=exc.code)
        report["budget"] = http.inspect(principal_id="evaluation")
    # Preserve the first capture and balance even if a later replay check fails.
    report["remaining_credits_after_capture"] = credits(token)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    # Reopen the actual persistent ledger. A transport trap proves local replay.
    if report["status"] == "captured":

        def no_network(**kwargs):
            raise AssertionError("replay attempted a second provider request")

        with duckdb.connect(str(args.database)) as conn:
            http = DurableHTTP(conn, **options)
            http.transport = no_network
            http.resolver = lambda host: no_network()
            client = HostedClient(
                http,
                principal_id="evaluation",
                enabled=True,
                credential=token,
                max_cost_per_request_micros=args.max_usd_micros,
            )
            replay = client.scrape(
                args.url,
                "public-page",
                allowed_source_hosts=[urlsplit(args.url).hostname],
                public_url_approved=True,
            )
            report["reopened_offline_replay_same_html"] = (
                replay["html"] == result["html"]
            )
    after = credits(token)
    report.update(
        remaining_credits_after=after,
        observed_credit_balance_delta=before - after,
        credit_delta_caveat="Account-wide balance difference; concurrent account use can affect it. Dollar reservation is not an invoice.",
        adoption="defer; one public page does not establish production fidelity or economic advantage",
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("status", "observed_credit_balance_delta", "adoption")
            }
        )
    )


if __name__ == "__main__":
    main()

"""Compare Jina Reader with local extraction on two bounded public pages."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

from src.ingestion.extract import extract_article
from src.ingestion.hosted_acquisition import HOSTS, HostedClient
from src.ingestion.provider_execution import DurableHTTP, ProviderError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-approved", action="store_true")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.network_approved:
        parser.error("explicit approval for two public-page transformations required")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "contract": "noesis-jina-public-comparison-v1",
        "date": "2026-09-09",
        "cases": [],
        "label_origin": "measured sequential public captures; not independent human fidelity labels",
    }
    with duckdb.connect(str(args.database)) as conn:
        reader = HostedClient(
            DurableHTTP(
                conn,
                budget_id="reader",
                provider="jina",
                principal_id="evaluation",
                allowed_hosts=HOSTS["jina"],
                reuse_notice="Public-page evaluation; source publisher terms apply",
                max_requests=2,
            ),
            principal_id="evaluation",
            enabled=True,
        )
        for language, url in [
            ("de", "https://www.berlin.de/"),
            ("en", "https://www.python.org/"),
        ]:
            case = {"language": language, "url": url}
            try:
                source = DurableHTTP(
                    conn,
                    budget_id="source-" + language,
                    provider="public-source",
                    principal_id="evaluation",
                    allowed_hosts=[urlsplit(url).hostname],
                    reuse_notice="Public-page evaluation; source publisher terms apply",
                    max_requests=1,
                )
                original = source.request(
                    "source", url, principal_id="evaluation", max_bytes=2000000
                )
                case["source_receipt"] = original.receipt
                baseline = extract_article(original.content.decode("utf-8"), url=url)
                result = reader.reader(
                    url,
                    language,
                    original_snapshot=original.receipt["snapshot"],
                    allowed_source_hosts=[urlsplit(url).hostname],
                    public_url_approved=True,
                )
                left = Counter((baseline.text if baseline else "").split())
                right = Counter(result["text"].split())
                case.update(
                    status="completed",
                    baseline_text=baseline.text if baseline else "",
                    baseline_metadata=baseline.metadata if baseline else None,
                    result=result,
                    body_token_recall=sum((left & right).values()) / sum(left.values())
                    if left
                    else None,
                )
                case["offline_replay_same_text"] = (
                    reader.reader(
                        url,
                        language,
                        original_snapshot=original.receipt["snapshot"],
                        allowed_source_hosts=[urlsplit(url).hostname],
                        public_url_approved=True,
                    )["text"]
                    == result["text"]
                )
            except ProviderError as e:
                case.update(status="failed", failure_code=e.code)
            report["cases"].append(case)
        report["budget"] = reader.http.inspect(principal_id="evaluation")
    report["decision"] = "defer production adoption; preserve local extraction default"
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        [(r["language"], r["status"], r.get("failure_code")) for r in report["cases"]]
    )


if __name__ == "__main__":
    main()

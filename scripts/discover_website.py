"""Explore a configured domain website using a durable, bounded frontier."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingestion.website_discovery import WebsiteFrontier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--max-steps", type=int)
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    frontier = WebsiteFrontier(
        args.state,
        domain=args.domain,
        source=args.source,
        seed=args.seed,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        timeout_ms=args.timeout_ms,
    )
    try:
        rows = frontier.run(max_steps=args.max_steps)
        print(
            json.dumps(
                {
                    "domain": args.domain,
                    "source": args.source,
                    "frontier": rows,
                    "candidates": [
                        {
                            "locator": row["url"],
                            "metadata": {
                                "domain": args.domain,
                                "source_id": args.source,
                                "discovered_from": row["parent"],
                                "discovery_kind": row["kind"],
                                "requires_source_acquisition": True,
                            },
                        }
                        for row in rows
                        if row["kind"] in ("page", "feed")
                    ],
                },
                indent=2,
            )
        )
    finally:
        frontier.close()


if __name__ == "__main__":
    main()

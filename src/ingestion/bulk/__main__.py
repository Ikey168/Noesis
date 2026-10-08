"""CLI for bulk-release runs.

    python -m src.ingestion.bulk list
    python -m src.ingestion.bulk run <adapter> --params '<json>' [--dry-run] [--full]
        [--sink subset|dataset|documents] [--namespace NS] [--domain DOMAIN]
        [--state-dir DIR] [--work-dir DIR] [--max-bytes N] [--max-files N] [--max-seconds N] [--keep-raw]
    python -m src.ingestion.bulk job /srv/bulk/jobs/<name>.json   # same options, from a job file
    python -m src.ingestion.bulk quotas [--host HOST]

Defaults: state ``NOESIS_BULK_STATE_DIR`` or ``/srv/bulk/state``; scratch
``NOESIS_BULK_WORK_DIR`` or ``/srv/bulk/tmp``. The subset is always written to the
state directory; ``--sink dataset`` also registers the release and ingests rows
into the warehouse (``NOESIS_DB_PATH``), ``--sink documents`` commits papers
through the gateway ingest. Exit code 0 = complete/dry-run, 2 = deferred or
budget exhausted (resumable), 1 = errors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.ingestion.bulk import ADAPTERS, get_bulk_adapter
from src.ingestion.bulk.runner import Budgets, BulkRunner


def _connect():
    import duckdb
    from src.config.env import warehouse_path
    path = warehouse_path()
    if not path:
        raise SystemExit("NOESIS_DB_PATH is not set")
    return duckdb.connect(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ingestion.bulk")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    job = sub.add_parser("job")
    job.add_argument("path")
    quotas = sub.add_parser("quotas")
    quotas.add_argument("--host")
    run = sub.add_parser("run")
    run.add_argument("adapter")
    run.add_argument("--params", default="{}")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--full", action="store_true", help="ignore files unchanged since earlier releases")
    run.add_argument("--sink", choices=["subset", "dataset", "documents"], default="subset")
    run.add_argument("--namespace", default=os.getenv("NOESIS_BULK_NAMESPACE", "bulk"))
    run.add_argument("--domain")
    run.add_argument("--state-dir", default=os.getenv("NOESIS_BULK_STATE_DIR", "/srv/bulk/state"))
    run.add_argument("--work-dir", default=os.getenv("NOESIS_BULK_WORK_DIR", "/srv/bulk/tmp"))
    run.add_argument("--max-bytes", type=int, default=Budgets.max_bytes)
    run.add_argument("--max-files", type=int, default=Budgets.max_files)
    run.add_argument("--max-seconds", type=int, default=Budgets.max_seconds)
    run.add_argument("--keep-raw", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        for name in sorted(ADAPTERS):
            adapter = get_bulk_adapter(name)
            print(f"{name}\t{adapter.output}\t{adapter.publisher}\t{', '.join(adapter.allowed_hosts)}")
        return 0
    if args.command == "job":
        spec = json.loads(Path(args.path).read_text())
        argv2 = ["run", spec["adapter"], "--params", json.dumps(spec.get("params") or {})]
        for key in ("sink", "namespace", "domain", "state_dir", "work_dir", "max_bytes", "max_files", "max_seconds"):
            if spec.get(key) is not None:
                argv2 += ["--" + key.replace("_", "-"), str(spec[key])]
        for flag in ("dry_run", "full", "keep_raw"):
            if spec.get(flag):
                argv2.append("--" + flag.replace("_", "-"))
        return main(argv2)
    if args.command == "quotas":
        from src.ingestion.quota import QuotaLedger
        print(json.dumps(QuotaLedger().status(args.host), indent=1))
        return 0

    adapter = get_bulk_adapter(args.adapter)
    sinks = []
    if args.sink == "dataset":
        if adapter.output != "rows":
            raise SystemExit(f"{adapter.name} emits {adapter.output}, not rows")
        from src.ingestion.bulk.sinks import DatasetSink
        sinks.append(DatasetSink(args.namespace, _connect))
    elif args.sink == "documents":
        if adapter.output != "documents" or not args.domain:
            raise SystemExit("--sink documents needs a documents adapter and --domain")
        from src.ingestion.bulk.sinks import DocumentSink
        sinks.append(DocumentSink(args.domain))
    runner = BulkRunner(adapter, state_dir=Path(args.state_dir), work_dir=Path(args.work_dir), sinks=sinks,
                        budgets=Budgets(args.max_bytes, args.max_files, args.max_seconds), keep_raw=args.keep_raw)
    receipt = runner.run(json.loads(args.params), dry_run=args.dry_run, full=args.full)
    print(json.dumps(receipt, indent=1, default=str))
    if receipt["status"] in ("complete", "dry-run"):
        return 0
    if receipt["status"] in ("deferred", "budget_exhausted"):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

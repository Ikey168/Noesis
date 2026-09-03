#!/usr/bin/env python3
"""
Fetch and prepare the external claim-detection benchmarks (#957).

Downloads FEVER, LIAR, and AVeriTeC into ``data/external_benchmarks/`` in
exactly the layout ``scripts/benchmark_models.py`` consumes (and now picks up
by default when present):

    data/external_benchmarks/fever/paper_dev.jsonl
    data/external_benchmarks/liar/test.tsv
    data/external_benchmarks/averitec/dev.json

Every fetch is recorded in ``manifest.json`` (url, sha256, size, fetched-at)
so results are reproducible and provenance is auditable. Idempotent: existing
files are kept unless ``--force``.

Label-mapping decisions (documented per the issue):

- **FEVER** (CC BY-SA 3.0, Wikipedia-derived): ``SUPPORTS``/``REFUTES``
  rows are verifiable claims (positive); ``NOT ENOUGH INFO`` maps to
  non-claim. This is a *proxy* mapping — NEI claims are claims a verifier
  could not resolve, so the negative class is soft; treat FEVER primarily
  as a recall check.
- **LIAR** (research use; Politifact-derived): every statement is a
  political claim regardless of its 6-way truthfulness label
  (pants-fire … true), so the 6-way scale is deliberately **not** mapped to
  claim/no-claim — truthfulness is a fact-check property, not
  claim-worthiness. LIAR measures recall/precision balance on all-positive
  data.
- **AVeriTeC** (CC BY-NC 4.0): all items are real-world verifiable claims —
  all-positive, recall check.

CI mode: ``--sample N`` truncates each prepared file to its first N records
after download (deterministic head-sampling), keeping the suite runnable in
CI without multi-hundred-MB artifacts.

Usage:
    python3 scripts/prepare_external_benchmarks.py            # fetch all
    python3 scripts/prepare_external_benchmarks.py --sample 200
    python3 scripts/prepare_external_benchmarks.py --only liar --force
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "data" / "external_benchmarks"

DATASETS = {
    "fever": {
        "url": "https://fever.ai/download/fever/paper_dev.jsonl",
        "target": "fever/paper_dev.jsonl",
        "license": "CC BY-SA 3.0 (Wikipedia-derived); https://fever.ai/dataset/fever.html",
        "kind": "file",
    },
    "liar": {
        "url": "https://www.cs.ucsb.edu/~william/data/liar_dataset.zip",
        "target": "liar/test.tsv",
        "license": "Research use; Wang (2017), Politifact-derived",
        "kind": "zip",
        "member": "test.tsv",
    },
    "averitec": {
        # The upstream is a model/code repository rather than a Hub dataset
        # repository, so the canonical raw path intentionally has no
        # ``/datasets`` segment.
        "url": "https://huggingface.co/chenxwh/AVeriTeC/resolve/main/data/dev.json",
        "target": "averitec/dev.json",
        "license": "CC BY-NC 4.0; Schlichtkrull et al. (2023)",
        "kind": "file",
    },
}


def _download(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "noesis-benchmarks/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _sample_file(path: Path, n: int) -> None:
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            path.write_text(json.dumps(data[:n]))
    else:
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:n]) + "\n")


def prepare(only=None, force: bool = False, sample: int = 0) -> int:
    manifest_path = ROOT / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            manifest = {}

    failures = 0
    for name, spec in DATASETS.items():
        if only and name != only:
            continue
        target = ROOT / spec["target"]
        if target.exists() and not force:
            print(f"[skip] {name}: {target} present (use --force to refetch)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"[fetch] {name}: {spec['url']}")
        try:
            payload = _download(spec["url"])
            if spec["kind"] == "zip":
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    payload = archive.read(spec["member"])
            target.write_bytes(payload)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"[fail] {name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if sample:
            _sample_file(target, sample)
            print(f"[sample] {name}: truncated to first {sample} records")

        manifest[name] = {
            "url": spec["url"],
            "license": spec["license"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size,
            "sampled_to": sample or None,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        print(f"[ok] {name}: {target} ({target.stat().st_size} bytes)")

    ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[manifest] {manifest_path}")
    print("\nRun the benchmarks (prepared sets are picked up automatically):")
    print("  python3 scripts/benchmark_models.py")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=sorted(DATASETS), default=None)
    parser.add_argument("--force", action="store_true", help="Refetch even if present")
    parser.add_argument("--sample", type=int, default=0,
                        help="Truncate each prepared file to its first N records (CI mode)")
    args = parser.parse_args()
    return prepare(only=args.only, force=args.force, sample=args.sample)


if __name__ == "__main__":
    sys.exit(main())

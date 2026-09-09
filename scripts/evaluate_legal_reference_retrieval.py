"""Supplement human relevance evaluation with publisher-bound exact-reference lookup."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_hybrid_retrieval import database, summarize
from scripts.evaluate_published_retrieval import verify_metrics
from src.evaluation.runtime_jobs import execute_job
from src.ingestion.regional_providers import parse_berlin_juris_xml

DIGEST = "b701bc5b07cb78c22a9db24b39c74a15f7a896ea2b333a3002e6c41f8610fb8a"
URL = "https://gesetze.berlin.de/jportal/bsbeAizDownload/Verf_BE.zip?doc.id=jlr-NNLBE000047B0&doc.part=X&_=%2FVerf_BE.zip"


def prepare(path, fits):
    if path.stat().st_size > 20_000_000:
        raise ValueError("bounded captured XML required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        raise ValueError("published XML digest mismatch")
    record = parse_berlin_juris_xml(raw, source_url=URL)[0]
    docs, queries = [], []
    labels = {}
    for norm in record["native"]["norms"]:
        label = norm["metadata"].get("enbez", "")
        if not label.startswith("Artikel "):
            continue
        sections = [
            r
            for r in record["sections"]
            if r["locator"]["official_norm_id"] == norm["official_id"]
        ]
        text = (
            norm["metadata"]["jurabk"]
            + " — "
            + label
            + "\n"
            + "\n".join(r["text"] for r in sections)
        )
        if not sections or not fits(text):
            continue
        identity = norm["official_id"]
        docs.append(
            {
                "id": identity,
                "text": text,
                "revision": hashlib.sha256(text.encode()).hexdigest(),
                "source": "berlin-law",
                "locators": [r["locator"] for r in sections],
            }
        )
        labels[identity] = norm["metadata"]["jurabk"] + " " + label
        if len(docs) == 20:
            break
    if len(docs) != 20:
        raise ValueError("twenty complete short articles required")
    for doc in docs[:4]:
        queries.append(
            {
                "id": "reference:" + doc["id"],
                "text": labels[doc["id"]],
                "language": "de",
                "language_pair": "de-de",
                "judgments": {doc["id"]: 1},
                "label_origin": "publisher metadata identifier lookup, not human semantic relevance annotation",
            }
        )
    for term in (
        "Volksvertretung",
        "Fernmeldegeheimnis",
    ):
        judgments = {
            r["id"]: int(term.casefold() in r["text"].casefold()) for r in docs
        }
        if not any(judgments.values()):
            continue
        queries.append(
            {
                "id": "compound:" + term,
                "text": term,
                "language": "de",
                "language_pair": "de-de",
                "judgments": judgments,
                "label_origin": "exact original-text term membership, not human semantic relevance annotation",
            }
        )
    return {"documents": docs, "queries": queries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--berlin-xml", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    from transformers import AutoTokenizer

    from src.evaluation.model_backends import model_path

    tokenizer = AutoTokenizer.from_pretrained(
        model_path("minilm")[0], local_files_only=True, trust_remote_code=False
    )
    corpus = prepare(
        args.berlin_xml,
        lambda text: len(tokenizer.encode(text, truncation=False)) <= 256,
    )
    report = {
        "contract": "noesis-legal-reference-diagnostic-v1",
        "source": {"url": URL, "sha256": DIGEST},
        "query_provenance": corpus["queries"],
        "document_provenance": [
            {k: v for k, v in r.items() if k != "text"} for r in corpus["documents"]
        ],
        "limitations": [
            "Exact publisher-identifier and compound membership lookup; not an independent human legal relevance set",
            "First twenty complete articles fitting MiniLM; no model-output based selection; no truncation",
            "Supplement to the separately reported human GermanDPR/SciFact/XNLI supporting-passage evaluation",
        ],
        "runs": {},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    job = execute_job(
        "retrieval-batch",
        {"backend": "bge-m3", **corpus},
        timeout_s=600,
        max_rss_bytes=4 * 1024**3,
    )
    report["runs"]["bge-m3"] = {"job": job}
    if job["status"] == "completed":
        report["runs"]["bge-m3"]["metric_verification"] = verify_metrics(job["result"])
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    with database() as (connection, migrations):
        job = execute_job(
            "retrieval-hybrid",
            {"connection": connection, "corpus": corpus},
            timeout_s=600,
            max_rss_bytes=3 * 1024**3,
        )
    report["runs"]["hybrid"] = summarize(
        {"job": job, "migrations": migrations, "container_removed": True}
    )
    report["decision"] = (
        "Keep modes opt-in; exact-reference lookup does not establish general legal evidence relevance."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print({k: v["job"]["status"] for k, v in report["runs"].items()}, flush=True)


if __name__ == "__main__":
    main()

"""
Encrypted knowledge-graph export — Issue #62.

Serialises the live in-process KnowledgeGraphStore to JSON and writes an
AES-256-GCM encrypted file.  Useful for persistence between process restarts
and for encrypted at-rest backup of KG state.

Usage::

    python3 scripts/export_kg.py
    python3 scripts/export_kg.py --output data/kg_snapshot.json.enc
    python3 scripts/export_kg.py --passphrase "my-passphrase"
    python3 scripts/export_kg.py --decrypt data/kg_snapshot.json.enc

Passphrase resolution (same as backup_db.py):
  1. --passphrase CLI argument
  2. OS keyring: service=noesis, key=KG_EXPORT_KEY
  3. Env var: NOESIS_KG_EXPORT_KEY (legacy NEURONEWS_KG_EXPORT_KEY also works)

The ``--decrypt`` flag decrypts a previously exported file to stdout (JSON).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _resolve_passphrase(cli_value: str | None, key_name: str = "KG_EXPORT_KEY") -> str:
    if cli_value:
        return cli_value
    from src.security.keyring_store import get_secret
    value = get_secret("noesis", key_name)
    if value:
        return value
    print(
        f"ERROR: No passphrase found.\n"
        f"Supply --passphrase, OS keyring key={key_name}, "
        f"or env var NOESIS_{key_name}.",
        file=sys.stderr,
    )
    sys.exit(1)


def _kg_to_dict() -> dict:
    """Serialise the shared KG store to a plain dict."""
    from src.knowledge_graph.kg_updater import _shared_store
    store = _shared_store()

    nodes = []
    for node in store._nodes.values():
        nodes.append(node.to_dict())

    triples = []
    for triple in store._triples.values():
        triples.append({
            "subject": triple.subject,
            "predicate": triple.predicate.value,
            "object": triple.object,
            "provenance": triple.provenance,
            "properties": triple.properties,
        })

    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "node_count": len(nodes),
        "triple_count": len(triples),
        "nodes": nodes,
        "triples": triples,
    }


def export_kg(output: Path, passphrase: str) -> None:
    kg_dict = _kg_to_dict()
    payload = json.dumps(kg_dict, indent=2).encode()
    from src.security.file_crypto import encrypt_bytes
    encrypted = encrypt_bytes(payload, passphrase)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encrypted)
    import os
    os.chmod(output, 0o600)
    print(
        f"KG exported: {kg_dict['node_count']} nodes, "
        f"{kg_dict['triple_count']} triples → {output} (AES-256-GCM, 0600)"
    )


def decrypt_kg(src: Path, passphrase: str) -> dict:
    from src.security.file_crypto import decrypt_bytes
    raw = src.read_bytes()
    payload = decrypt_bytes(raw, passphrase)
    return json.loads(payload.decode())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export or decrypt the Noesis knowledge graph"
    )
    parser.add_argument("--output", help="Destination .json.enc file")
    parser.add_argument("--passphrase", help="Encryption passphrase")
    parser.add_argument(
        "--decrypt",
        metavar="FILE",
        help="Decrypt a previously exported .json.enc file and print JSON to stdout",
    )
    args = parser.parse_args()

    passphrase = _resolve_passphrase(args.passphrase)

    if args.decrypt:
        src = Path(args.decrypt)
        data = decrypt_kg(src, passphrase)
        print(json.dumps(data, indent=2))
        return

    if args.output:
        dst = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = REPO_ROOT / "data" / "backups" / f"kg_{ts}.json.enc"

    export_kg(dst, passphrase)


if __name__ == "__main__":
    main()

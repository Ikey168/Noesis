"""
Encrypted DuckDB backup — Issue #62.

Creates an AES-256-GCM encrypted copy of the analytics warehouse.

Usage::

    python3 scripts/backup_db.py
    python3 scripts/backup_db.py --output /path/to/backup.duckdb.enc
    python3 scripts/backup_db.py --passphrase "my-strong-passphrase"

Passphrase resolution order:
  1. --passphrase CLI argument
  2. OS keyring: service=noesis, key=BACKUP_KEY
  3. Env var: NOESIS_BACKUP_KEY (legacy NEURONEWS_BACKUP_KEY also works)

If no passphrase is found the script exits with an error message.

The backup is written to ``data/backups/`` with a timestamp filename unless
``--output`` overrides it.  The file is created with permissions 0600.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Allow running from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _resolve_passphrase(cli_value: str | None) -> str:
    if cli_value:
        return cli_value

    from src.security.keyring_store import get_secret
    value = get_secret("noesis", "BACKUP_KEY")
    if value:
        return value

    print(
        "ERROR: No backup passphrase found.\n"
        "Supply one of:\n"
        "  --passphrase <value>\n"
        "  OS keyring: service=noesis key=BACKUP_KEY\n"
        "  Environment variable: NOESIS_BACKUP_KEY",
        file=sys.stderr,
    )
    sys.exit(1)


def _default_db_path() -> str:
    from src.config.env import warehouse_path

    return warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb")) or ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an encrypted DuckDB backup")
    parser.add_argument("--output", help="Destination path for the encrypted backup")
    parser.add_argument("--passphrase", help="Encryption passphrase")
    parser.add_argument("--db-path", help="Source DuckDB file (default: NOESIS_DB_PATH)")
    args = parser.parse_args()

    db_path = Path(args.db_path or _default_db_path())
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    passphrase = _resolve_passphrase(args.passphrase)

    if args.output:
        dst = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = REPO_ROOT / "data" / "backups" / f"neuronews_{ts}.duckdb.enc"

    from src.security.file_crypto import encrypt_file
    encrypt_file(db_path, dst, passphrase)

    size_kb = dst.stat().st_size // 1024
    print(f"Backup written to {dst} ({size_kb} KB, AES-256-GCM encrypted, permissions 0600)")


if __name__ == "__main__":
    main()

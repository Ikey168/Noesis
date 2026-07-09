"""
NeuroNews local security inspector — MCP server.

Token-efficient read-only tools for checking the offline security posture
without needing to read source files or run shell commands.

Tools:

  security_posture()           -> overall security status summary
  check_db_permissions()       -> DuckDB file mode and whether it's 0600
  list_tls_certs(dir?)         -> certs in data/tls/ with expiry + SAN
  list_backups(dir?)           -> encrypted backup files with size + age
  check_secret(service, key)   -> whether a secret is resolvable (never
                                  returns the value — only presence/source)

Design constraints:
  * Read-only — no tool writes files or changes secrets.
  * Never returns plaintext secret values.
  * Lazy imports inside each tool.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("neuronews-security")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def security_posture() -> dict:
    """
    Return an overall local security posture summary.

    Checks:
    - DB file permissions (should be 0600)
    - TLS certificate presence and expiry
    - Encrypted backup presence and age
    - Whether NEURONEWS_DB_KEY / NEURONEWS_BACKUP_KEY are set

    Returns a dict with ``status`` (ok / warning / error), a ``checks`` list,
    and a ``recommendations`` list.
    """
    checks = []
    recommendations = []

    # DB permissions
    db_path = os.getenv("NEURONEWS_DB_PATH", str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if Path(db_path).exists():
        mode = Path(db_path).stat().st_mode & 0o777
        ok = mode == 0o600
        checks.append({
            "check": "db_permissions",
            "status": "ok" if ok else "warning",
            "detail": f"{db_path} permissions: {oct(mode)}",
        })
        if not ok:
            recommendations.append(f"Run: chmod 600 {db_path}")
    else:
        checks.append({"check": "db_permissions", "status": "skip", "detail": "DB file not yet created"})

    # TLS cert
    tls_dir = REPO_ROOT / "data" / "tls"
    crt = tls_dir / "server.crt"
    key = tls_dir / "server.key"
    if crt.exists() and key.exists():
        try:
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(crt.read_bytes())
            from datetime import datetime, timezone
            days_left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
            status = "ok" if days_left > 30 else ("warning" if days_left > 0 else "error")
            checks.append({"check": "tls_cert", "status": status, "detail": f"{days_left} days until expiry"})
            if days_left < 30:
                recommendations.append("Run: python3 scripts/gen_local_tls.py --days 365")
        except Exception as exc:
            checks.append({"check": "tls_cert", "status": "error", "detail": str(exc)})
    else:
        checks.append({"check": "tls_cert", "status": "warning", "detail": "No TLS cert found"})
        recommendations.append("Run: python3 scripts/gen_local_tls.py")

    # Backups
    backup_dir = REPO_ROOT / "data" / "backups"
    enc_files = sorted(backup_dir.glob("*.enc")) if backup_dir.exists() else []
    if enc_files:
        newest = enc_files[-1]
        import time
        age_days = (time.time() - newest.stat().st_mtime) / 86400
        status = "ok" if age_days < 7 else "warning"
        checks.append({
            "check": "backup_freshness",
            "status": status,
            "detail": f"Newest backup: {newest.name} ({age_days:.1f} days old)",
        })
        if age_days >= 7:
            recommendations.append("Run: python3 scripts/backup_db.py")
    else:
        checks.append({"check": "backup_freshness", "status": "warning", "detail": "No encrypted backups found"})
        recommendations.append("Run: python3 scripts/backup_db.py")

    # Secret availability
    for env_var in ["NEURONEWS_DB_KEY", "NEURONEWS_BACKUP_KEY", "NEURONEWS_KG_EXPORT_KEY"]:
        present = env_var in os.environ
        checks.append({
            "check": f"secret_{env_var.lower()}",
            "status": "ok" if present else "info",
            "detail": "set" if present else "not set (passphrase must be supplied at runtime)",
        })

    statuses = [c["status"] for c in checks]
    overall = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
    return {
        "status": overall,
        "checks": checks,
        "recommendations": recommendations,
    }


@mcp.tool()
def check_db_permissions() -> dict:
    """
    Return the DuckDB file path, its current Unix permissions, and whether
    they match the recommended 0600.
    """
    db_path = os.getenv("NEURONEWS_DB_PATH", str(REPO_ROOT / "data" / "neuronews.duckdb"))
    p = Path(db_path)
    if not p.exists():
        return {"path": db_path, "exists": False}
    mode = p.stat().st_mode & 0o777
    return {
        "path": db_path,
        "exists": True,
        "permissions_octal": oct(mode),
        "is_0600": mode == 0o600,
        "size_kb": p.stat().st_size // 1024,
    }


@mcp.tool()
def list_tls_certs(tls_dir: Optional[str] = None) -> list:
    """
    List TLS certificates in the local cert directory.

    Args:
        tls_dir:  Directory to scan (default: ``data/tls/``).

    Returns a list of dicts with fields: filename, subject_cn, issuer_cn,
    not_before, not_after, days_until_expiry, san_dns, san_ip, key_size_bits.
    Returns an empty list if the directory or no .crt files exist.
    """
    from cryptography import x509
    from datetime import datetime, timezone

    d = Path(tls_dir) if tls_dir else REPO_ROOT / "data" / "tls"
    if not d.exists():
        return []

    results = []
    for crt_file in sorted(d.glob("*.crt")):
        try:
            cert = x509.load_pem_x509_certificate(crt_file.read_bytes())
            now = datetime.now(timezone.utc)
            days_left = (cert.not_valid_after_utc - now).days
            try:
                san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                dns_names = san_ext.value.get_values_for_type(x509.DNSName)
                ip_addrs = [str(ip) for ip in san_ext.value.get_values_for_type(x509.IPAddress)]
            except x509.ExtensionNotFound:
                dns_names, ip_addrs = [], []
            results.append({
                "filename": crt_file.name,
                "subject_cn": cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value,
                "not_before": cert.not_valid_before_utc.isoformat(),
                "not_after": cert.not_valid_after_utc.isoformat(),
                "days_until_expiry": days_left,
                "san_dns": dns_names,
                "san_ip": ip_addrs,
            })
        except Exception as exc:
            results.append({"filename": crt_file.name, "error": str(exc)})
    return results


@mcp.tool()
def list_backups(backup_dir: Optional[str] = None) -> list:
    """
    List encrypted backup files.

    Args:
        backup_dir:  Directory to scan (default: ``data/backups/``).

    Returns a list of dicts: filename, size_kb, age_days, permissions_octal.
    """
    import time
    d = Path(backup_dir) if backup_dir else REPO_ROOT / "data" / "backups"
    if not d.exists():
        return []
    results = []
    for f in sorted(d.glob("*.enc"), reverse=True):
        st = f.stat()
        results.append({
            "filename": f.name,
            "size_kb": st.st_size // 1024,
            "age_days": round((time.time() - st.st_mtime) / 86400, 1),
            "permissions_octal": oct(st.st_mode & 0o777),
        })
    return results


@mcp.tool()
def check_secret(service: str, key: str) -> dict:
    """
    Check whether a secret is resolvable — never returns the value itself.

    Args:
        service:  Keyring service name (e.g. ``"neuronews"``).
        key:      Secret key (e.g. ``"BACKUP_KEY"``).

    Returns: ``{resolvable: bool, source: "keyring" | "env_var" | "not_found"}``.
    """
    from src.security.keyring_store import _env_key, _try_keyring

    # Try keyring first (without exposing value)
    if _try_keyring():
        try:
            import keyring
            val = keyring.get_password(service, key)
            if val is not None:
                return {"resolvable": True, "source": "keyring"}
        except Exception:
            pass

    # Try env var
    env_name = _env_key(service, key)
    if env_name in os.environ:
        return {"resolvable": True, "source": "env_var", "env_var": env_name}

    return {"resolvable": False, "source": "not_found", "env_var": env_name}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

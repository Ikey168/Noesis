"""Deterministic, signed, encrypted, dependency-complete research packages."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

MANIFEST_CONTRACT = "noesis-research-package-manifest-v1"
CLOSURE_CONTRACT = "noesis-research-package-closure-v1"
PACKAGE_CONTRACT = "noesis-research-package-v1"
VERIFY_CONTRACT = "noesis-research-package-verification-v1"
IMPORT_CONTRACT = "noesis-research-package-import-v1"
READ_SCOPE = "knowledge:packages:read"
WRITE_SCOPE = "knowledge:packages:write"
IMPORT_SCOPE = "knowledge:packages:import"
TRUST_SCOPE = "knowledge:packages:trust"

_DDL = """
CREATE TABLE IF NOT EXISTS research_package_components(namespace TEXT NOT NULL,component_type TEXT NOT NULL,component_id TEXT NOT NULL,content_json TEXT NOT NULL,content_hash TEXT NOT NULL,dependencies_json TEXT NOT NULL,access_status TEXT NOT NULL,redacted_content_json TEXT,metadata_json TEXT NOT NULL,PRIMARY KEY(namespace,component_type,component_id));
CREATE TABLE IF NOT EXISTS research_package_manifests(package_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,format_version TEXT NOT NULL,manifest_json TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_package_artifacts(package_hash TEXT PRIMARY KEY,package_id TEXT NOT NULL,namespace TEXT NOT NULL,package_json TEXT NOT NULL,signature_json TEXT,envelope_json TEXT,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_package_imports(import_id TEXT PRIMARY KEY,package_hash TEXT NOT NULL,target_namespace TEXT NOT NULL,status TEXT NOT NULL,receipt_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(package_hash,target_namespace));
CREATE TABLE IF NOT EXISTS research_package_imported_components(target_namespace TEXT NOT NULL,package_hash TEXT NOT NULL,component_type TEXT NOT NULL,component_id TEXT NOT NULL,content_json TEXT NOT NULL,content_hash TEXT NOT NULL,executable BOOLEAN NOT NULL,PRIMARY KEY(target_namespace,component_type,component_id));
CREATE TABLE IF NOT EXISTS research_package_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_package_trust_policies(namespace TEXT NOT NULL,revision BIGINT NOT NULL,policy_json TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,revision));
"""


class ResearchPackageError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _load(value, default):
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes, required):
    if required not in scopes and "operator" not in scopes:
        raise ResearchPackageError("unauthorized", f"missing required scope {required}")


def _bound(value, maximum=10000):
    return min(max(int(value), 1), maximum)


class ResearchPackageStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail=None):
        now = self.now()
        detail = dict(detail or {})
        self.conn.execute(
            "INSERT OR IGNORE INTO research_package_audit VALUES (?,?,?,?,?,?,?)",
            [
                "package-audit:"
                + _hash([namespace, operation, object_id, detail, now])[:24],
                namespace,
                operation,
                object_id,
                principal_id,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
                now,
            ],
        )

    def validate_manifest(self, manifest, *, supported_versions=("1.0",)):
        required = {
            "format_version",
            "question",
            "plan",
            "snapshot",
            "evidence",
            "transformations",
            "findings",
            "limitations",
            "policies",
            "compatibility",
        }
        missing = sorted(required - manifest.keys())
        unknown = sorted(
            key
            for key in manifest
            if key not in required | {"extensions", "package_id", "contract"}
        )
        invalid_extensions = sorted(
            key for key in manifest.get("extensions", {}) if not key.startswith("x-")
        )
        compatible = manifest.get("format_version") in supported_versions
        errors = (
            [{"code": "missing_field", "field": key} for key in missing]
            + [{"code": "unknown_field", "field": key} for key in unknown]
            + [
                {"code": "invalid_extension", "field": key}
                for key in invalid_extensions
            ]
            + (
                []
                if compatible
                else [
                    {
                        "code": "unsupported_version",
                        "supported": list(supported_versions),
                    }
                ]
            )
        )
        canonical = {
            key: manifest[key]
            for key in sorted(manifest)
            if key not in {"contract", "package_id"}
        }
        return {
            "valid": not errors,
            "errors": errors,
            "compatible": compatible,
            "negotiated_version": manifest.get("format_version")
            if compatible
            else None,
            "canonical_hash": _hash(canonical),
        }

    def create_manifest(
        self, namespace, manifest, *, principal_id, scopes, supported_versions=("1.0",)
    ):
        _require(scopes, WRITE_SCOPE)
        validation = self.validate_manifest(
            manifest, supported_versions=supported_versions
        )
        if not validation["valid"]:
            raise ResearchPackageError(
                "invalid_manifest",
                "research package manifest is invalid",
                errors=validation["errors"],
            )
        normalized = {
            "contract": MANIFEST_CONTRACT,
            **{
                key: manifest[key]
                for key in sorted(manifest)
                if key not in {"contract", "package_id"}
            },
        }
        package_id = "research-package:" + _hash(normalized)[:24]
        normalized["package_id"] = package_id
        row = self.conn.execute(
            "SELECT manifest_json FROM research_package_manifests WHERE package_id=? AND namespace=?",
            [package_id, namespace],
        ).fetchone()
        if row:
            return {**_load(row[0], {}), "idempotent": True}
        now = self.now()
        self.conn.execute(
            "INSERT INTO research_package_manifests VALUES (?,?,?,?,?,?,?)",
            [
                package_id,
                namespace,
                manifest["format_version"],
                json.dumps(
                    normalized,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                _hash(normalized),
                "draft",
                now,
            ],
        )
        self._audit(namespace, "create_manifest", package_id, principal_id)
        return {
            **normalized,
            "namespace": namespace,
            "validation": validation,
            "idempotent": False,
        }

    def register_component(
        self,
        namespace,
        component_type,
        component_id,
        content,
        *,
        dependencies=(),
        access_status="accessible",
        redacted_content=None,
        metadata=None,
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        if component_type not in {
            "document",
            "revision",
            "claim",
            "dataset",
            "method",
            "model",
            "ontology",
            "policy",
            "recipe",
            "asset",
        }:
            raise ResearchPackageError(
                "invalid_component_type", "unsupported package component type"
            )
        if access_status not in {"accessible", "redacted", "inaccessible"}:
            raise ResearchPackageError(
                "invalid_access_status", "unsupported component access status"
            )
        content_hash = _hash(content)
        row = self.conn.execute(
            "SELECT content_hash FROM research_package_components WHERE namespace=? AND component_type=? AND component_id=?",
            [namespace, component_type, component_id],
        ).fetchone()
        if row:
            if row[0] != content_hash:
                raise ResearchPackageError(
                    "component_identity_collision",
                    "component identity has different content",
                )
            return {
                "namespace": namespace,
                "component_type": component_type,
                "component_id": component_id,
                "content_hash": content_hash,
                "idempotent": True,
            }
        self.conn.execute(
            "INSERT INTO research_package_components VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                component_type,
                component_id,
                json.dumps(
                    content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
                content_hash,
                json.dumps(sorted(set(dependencies)), separators=(",", ":")),
                access_status,
                None
                if redacted_content is None
                else json.dumps(
                    redacted_content,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
            ],
        )
        self._audit(namespace, "register_component", component_id, principal_id)
        return {
            "namespace": namespace,
            "component_type": component_type,
            "component_id": component_id,
            "content_hash": content_hash,
            "idempotent": False,
        }

    def closure(self, namespace, root_ids, *, scopes, limit=10000):
        _require(scopes, READ_SCOPE)
        queue = sorted(set(root_ids))
        seen = set()
        members = []
        omissions = []
        limit = _bound(limit)
        while queue and len(seen) < limit:
            component_id = queue.pop(0)
            if component_id in seen:
                continue
            seen.add(component_id)
            rows = self.conn.execute(
                "SELECT component_type,content_json,content_hash,dependencies_json,access_status,redacted_content_json,metadata_json FROM research_package_components WHERE namespace=? AND component_id=? ORDER BY component_type",
                [namespace, component_id],
            ).fetchall()
            if not rows:
                omissions.append({"component_id": component_id, "reason": "missing"})
                continue
            for row in rows:
                dependencies = _load(row[3], [])
                if row[4] == "inaccessible":
                    omissions.append(
                        {
                            "component_id": component_id,
                            "component_type": row[0],
                            "reason": "inaccessible",
                        }
                    )
                    continue
                queue.extend(dep for dep in dependencies if dep not in seen)
                queue.sort()
                content = (
                    _load(row[5], {}) if row[4] == "redacted" else _load(row[1], {})
                )
                members.append(
                    {
                        "component_type": row[0],
                        "component_id": component_id,
                        "content": content,
                        "content_hash": _hash(content),
                        "source_content_hash": row[2],
                        "dependencies": dependencies,
                        "redacted": row[4] == "redacted",
                        "metadata": _load(row[6], {}),
                    }
                )
        # A partial bounded export must declare the unvisited frontier instead
        # of silently dropping dependencies that the verifier cannot account for.
        omissions.extend({"component_id": key, "reason": "bounded"}
                         for key in sorted(set(queue) - seen))
        members.sort(key=lambda x: (x["component_type"], x["component_id"]))
        omissions.sort(key=lambda x: (x["component_id"], x.get("component_type", "")))
        return {
            "contract": CLOSURE_CONTRACT,
            "namespace": namespace,
            "root_ids": sorted(set(root_ids)),
            "members": members,
            "omissions": omissions,
            "complete": not omissions and not queue,
            "bounded": bool(queue),
            "closure_hash": _hash({"members": members, "omissions": omissions}),
        }

    def _manifest(self, namespace, package_id):
        row = self.conn.execute(
            "SELECT manifest_json FROM research_package_manifests WHERE namespace=? AND package_id=?",
            [namespace, package_id],
        ).fetchone()
        if not row:
            raise ResearchPackageError(
                "manifest_not_found", "research package manifest not found"
            )
        return _load(row[0], {})

    def build(
        self,
        namespace,
        package_id,
        root_ids,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
        allow_partial=False,
        limit=10000,
    ):
        _require(scopes, WRITE_SCOPE)
        manifest = self._manifest(namespace, package_id)
        closure = self.closure(namespace, root_ids, scopes={READ_SCOPE}, limit=limit)
        if cancel_requested:
            return {
                "contract": PACKAGE_CONTRACT,
                "package_id": package_id,
                "namespace": namespace,
                "status": "cancelled",
                "content_hash": _hash([package_id, "cancelled"]),
                "manifest": manifest,
                "closure": closure,
                "members": [],
            }
        if not closure["complete"] and not allow_partial:
            raise ResearchPackageError(
                "incomplete_closure",
                "dependency closure contains omissions",
                omissions=closure["omissions"],
            )
        core = {
            "contract": PACKAGE_CONTRACT,
            "package_id": package_id,
            "namespace": namespace,
            "manifest": manifest,
            "closure": {
                key: closure[key]
                for key in (
                    "root_ids",
                    "omissions",
                    "complete",
                    "bounded",
                    "closure_hash",
                )
            },
            "members": closure["members"],
        }
        content_hash = _hash(core)
        package = {
            **core,
            "content_hash": content_hash,
            "status": "complete" if closure["complete"] else "partial",
        }
        raw = canonical_bytes(package)
        existing = self.conn.execute(
            "SELECT package_json FROM research_package_artifacts WHERE package_hash=?",
            [content_hash],
        ).fetchone()
        if not existing:
            now = self.now()
            self.conn.execute(
                "INSERT INTO research_package_artifacts VALUES (?,?,?,?,?,?,?,?)",
                [
                    content_hash,
                    package_id,
                    namespace,
                    raw.decode(),
                    None,
                    None,
                    package["status"],
                    now,
                ],
            )
            self._audit(
                namespace, "build", content_hash, principal_id, {"bytes": len(raw)}
            )
        return {
            **package,
            "canonical_bytes_b64": base64.b64encode(raw).decode(),
            "byte_length": len(raw),
            "reproducible": True,
        }

    def export_rocrate(self, namespace, package_id, root_ids, *, principal_id, scopes, allow_partial=False):
        from src.integrations.export import export_rocrate
        package = self.build(namespace, package_id, root_ids, principal_id=principal_id,
                             scopes=scopes, allow_partial=allow_partial)
        return export_rocrate(package)

    def sign(self, package, private_key_b64, *, key_id, key_version):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        unsigned = {
            k: v
            for k, v in package.items()
            if k
            not in {
                "canonical_bytes_b64",
                "byte_length",
                "reproducible",
                "signature",
                "envelope",
            }
        }
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
        signature = key.sign(canonical_bytes(unsigned))
        return {
            **package,
            "signature": {
                "algorithm": "Ed25519",
                "key_id": key_id,
                "key_version": key_version,
                "signed_hash": package["content_hash"],
                "value": base64.b64encode(signature).decode(),
            },
        }

    def encrypt(self, package, recipient_key_b64, *, recipient_id, key_version):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = base64.b64decode(recipient_key_b64)
        if len(key) != 32:
            raise ResearchPackageError(
                "invalid_key", "AES-256-GCM requires a 32-byte key"
            )
        nonce = os.urandom(12)
        plaintext = canonical_bytes(package)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, recipient_id.encode())
        return {
            "contract": PACKAGE_CONTRACT,
            "encrypted": True,
            "content_hash": package["content_hash"],
            "envelope": {
                "algorithm": "AES-256-GCM",
                "recipient_id": recipient_id,
                "key_version": key_version,
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            },
        }

    def decrypt(
        self, envelope, recipient_key_b64, *, recipient_id, max_bytes=50_000_000
    ):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        meta = envelope.get("envelope", {})
        if meta.get("recipient_id") != recipient_id:
            raise ResearchPackageError(
                "recipient_mismatch", "envelope targets another recipient"
            )
        try:
            plaintext = AESGCM(base64.b64decode(recipient_key_b64)).decrypt(
                base64.b64decode(meta["nonce"]),
                base64.b64decode(meta["ciphertext"]),
                recipient_id.encode(),
            )
        except (InvalidTag, ValueError, KeyError) as exc:
            raise ResearchPackageError(
                "decryption_failed", "wrong key or tampered envelope"
            ) from exc
        if len(plaintext) > max_bytes:
            raise ResearchPackageError(
                "package_too_large", "decrypted package exceeds limit"
            )
        return json.loads(plaintext)

    @staticmethod
    def _verify_closure(package):
        """Verify declared dependency structure independently of outer hashes."""
        members, closure = package.get("members", []), package.get("closure", {})
        errors, missing = [], []
        if not isinstance(members, list) or not isinstance(closure, dict):
            return ["invalid closure shape"], [], []
        omissions, roots = closure.get("omissions", []), closure.get("root_ids", [])
        if not isinstance(omissions, list) or not isinstance(roots, list):
            return ["invalid roots or omissions"], [], []
        if len(members) > 10000 or len(omissions) > 10000 or len(roots) > 10000:
            return ["closure exceeds verification bounds"], [], []
        identities, ids, required, member_failures = set(), set(), set(), []
        for root in roots:
            if not isinstance(root, str) or not root:
                errors.append("invalid root identity")
            else:
                required.add(root)
        for member in members:
            if not isinstance(member, dict):
                errors.append("invalid member")
                continue
            key, kind = member.get("component_id"), member.get("component_type")
            if not isinstance(key, str) or not key or not isinstance(kind, str) or not kind:
                errors.append("invalid member identity")
                continue
            identity = (kind, key)
            if identity in identities:
                errors.append("duplicate member identity: " + key)
            identities.add(identity)
            ids.add(key)
            if "content" not in member or _hash(member["content"]) != member.get("content_hash"):
                member_failures.append(key)
            dependencies = member.get("dependencies")
            if not isinstance(dependencies, list) or not all(isinstance(d, str) and d for d in dependencies):
                errors.append("invalid dependencies: " + key)
            else:
                required.update(dependencies)
        omitted = set()
        for omission in omissions:
            if not isinstance(omission, dict) or not isinstance(omission.get("component_id"), str):
                errors.append("invalid omission")
                continue
            reason, key = omission.get("reason"), omission["component_id"]
            if reason not in {"inaccessible", "missing", "bounded"}:
                errors.append("unsupported omission reason: " + key)
            if reason == "missing":
                missing.append(omission)
            if reason == "bounded" and not closure.get("bounded"):
                errors.append("bounded omission without bounded closure")
            omitted.add(key)
        missing.extend({"component_id": key, "reason": "missing"}
                       for key in sorted(required - ids - omitted))
        if closure.get("closure_hash") != _hash({"members": members, "omissions": omissions}):
            errors.append("closure hash mismatch")
        if closure.get("complete") is not (not omissions and not closure.get("bounded") and not missing):
            errors.append("closure completeness declaration mismatch")
        return errors, missing, member_failures

    def verify(self, package, *, public_keys=None, require_signature=False):
        supplied = package.get("content_hash")
        core = {
            k: v
            for k, v in package.items()
            if k
            not in {
                "content_hash",
                "status",
                "canonical_bytes_b64",
                "byte_length",
                "reproducible",
                "signature",
                "envelope",
            }
        }
        actual = _hash(core)
        structural_errors, missing, member_failures = self._verify_closure(package)
        signature = package.get("signature")
        signature_status = "absent"
        if signature:
            key_value = (public_keys or {}).get(signature.get("key_id"))
            if isinstance(key_value, dict):
                if key_value.get("revoked"):
                    signature_status = "revoked_key"
                    key_value = None
                elif str(key_value.get("key_version")) != str(signature.get("key_version")):
                    signature_status = "untrusted_key_version"
                    key_value = None
                else:
                    key_value = key_value.get("public_key")
            if not key_value:
                if signature_status == "absent":
                    signature_status = "untrusted_key"
            else:
                from cryptography.exceptions import InvalidSignature
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PublicKey,
                )

                unsigned = {
                    k: v
                    for k, v in package.items()
                    if k
                    not in {
                        "canonical_bytes_b64",
                        "byte_length",
                        "reproducible",
                        "signature",
                        "envelope",
                    }
                }
                try:
                    Ed25519PublicKey.from_public_bytes(
                        base64.b64decode(key_value)
                    ).verify(
                        base64.b64decode(signature["value"]), canonical_bytes(unsigned)
                    )
                    signature_status = "valid"
                except (InvalidSignature, ValueError, KeyError):
                    signature_status = "invalid"
        valid = (
            supplied == actual
            and not member_failures
            and not missing
            and not structural_errors
            and (not require_signature or signature_status == "valid")
        )
        return {
            "contract": VERIFY_CONTRACT,
            "valid": valid,
            "content_hash": supplied,
            "actual_hash": actual,
            "member_failures": member_failures,
            "missing_members": missing,
            "structural_errors": structural_errors,
            "signature_status": signature_status,
            "key_id": signature.get("key_id") if signature else None,
            "key_version": signature.get("key_version") if signature else None,
            "offline": True,
        }

    def set_trust_policy(self, namespace, public_keys, *, require_signature=True,
                         expected_revision=0, principal_id, scopes):
        """Append an operator-controlled namespace import trust policy.

        Rotation uses a new key ID or an explicit version; old policies remain
        auditable. Current policy is checked even when replaying an old import.
        """
        _require(scopes, TRUST_SCOPE)
        if not namespace.startswith("import:") or not isinstance(public_keys, dict) or len(public_keys) > 1000:
            raise ResearchPackageError("invalid_trust_policy", "invalid import namespace or keys")
        for key_id, key in public_keys.items():
            try:
                if not key_id or not isinstance(key, dict) or set(key) - {"public_key", "key_version", "revoked"}:
                    raise ValueError("invalid key record")
                if not isinstance(key.get("key_version"), (str, int)) or not isinstance(key.get("revoked", False), bool):
                    raise ValueError("invalid key version or revocation")
                if len(base64.b64decode(key["public_key"], validate=True)) != 32:
                    raise ValueError("invalid public key length")
            except (KeyError, ValueError, TypeError) as exc:
                raise ResearchPackageError("invalid_trust_policy", "malformed public key") from exc
        if not isinstance(require_signature, bool):
            raise ResearchPackageError("invalid_trust_policy", "signature requirement must be boolean")
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute("SELECT max(revision) FROM research_package_trust_policies WHERE namespace=?", [namespace]).fetchone()
            revision = int(row[0] or 0)
            if revision != expected_revision:
                raise ResearchPackageError("revision_conflict", "trust policy changed")
            policy = {"namespace": namespace, "revision": revision + 1,
                      "require_signature": require_signature, "public_keys": public_keys}
            self.conn.execute("INSERT INTO research_package_trust_policies VALUES (?,?,?,?,?)",
                              [namespace, revision + 1, canonical_bytes(policy).decode(), principal_id, self.now()])
            self._audit(namespace, "set_trust_policy", str(revision + 1), principal_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return policy

    def inspect(self, package, *, scopes):
        _require(scopes, READ_SCOPE)
        return {
            "package_id": package.get("package_id"),
            "namespace": package.get("namespace"),
            "format_version": package.get("manifest", {}).get("format_version"),
            "content_hash": package.get("content_hash"),
            "status": package.get("status"),
            "member_count": len(package.get("members", [])),
            "omissions": package.get("closure", {}).get("omissions", []),
            "component_types": sorted(
                {m["component_type"] for m in package.get("members", [])}
            ),
        }

    def import_package(
        self,
        package,
        target_namespace,
        *,
        principal_id,
        scopes,
        trusted_recipe_ids=(),
        cancel_requested=False,
        public_keys=None,
        require_signature=False,
    ):
        _require(scopes, IMPORT_SCOPE)
        if not target_namespace.startswith("import:"):
            raise ResearchPackageError(
                "isolation_required", "target namespace must start with import:"
            )
        manifest_validation = self.validate_manifest(package.get("manifest", {}))
        if not manifest_validation["compatible"]:
            raise ResearchPackageError(
                "incompatible_package",
                "package format version is not supported",
                errors=manifest_validation["errors"],
            )
        policy_row = self.conn.execute(
            "SELECT policy_json FROM research_package_trust_policies WHERE namespace=? ORDER BY revision DESC LIMIT 1",
            [target_namespace],
        ).fetchone()
        policy = _load(policy_row[0], {}) if policy_row else None
        verification = self.verify(
            package,
            public_keys=policy["public_keys"] if policy else public_keys,
            require_signature=bool(require_signature or (policy and policy["require_signature"])),
        )
        if not verification["valid"]:
            raise ResearchPackageError(
                "invalid_package",
                "package verification failed",
                verification=verification,
            )
        package_hash = package["content_hash"]
        import_id = "research-import:" + _hash([package_hash, target_namespace])[:24]
        prior = self.conn.execute(
            "SELECT receipt_json FROM research_package_imports WHERE package_hash=? AND target_namespace=?",
            [package_hash, target_namespace],
        ).fetchone()
        if prior:
            return {**_load(prior[0], {}), "idempotent": True}
        if cancel_requested:
            return {
                "contract": IMPORT_CONTRACT,
                "import_id": import_id,
                "package_hash": package_hash,
                "target_namespace": target_namespace,
                "status": "cancelled",
                "imported": 0,
                "disabled_recipes": [],
                "collisions": [],
                "idempotent": False,
            }
        collisions = []
        for member in package.get("members", []):
            row = self.conn.execute(
                "SELECT content_hash FROM research_package_imported_components WHERE target_namespace=? AND component_type=? AND component_id=?",
                [target_namespace, member["component_type"], member["component_id"]],
            ).fetchone()
            if row and row[0] != member["content_hash"]:
                collisions.append(
                    {
                        "component_type": member["component_type"],
                        "component_id": member["component_id"],
                    }
                )
        if collisions:
            raise ResearchPackageError(
                "identity_collision",
                "import would overwrite local isolated knowledge",
                collisions=collisions,
            )
        disabled = []
        imported = 0
        self.conn.execute("BEGIN")
        try:
            for member in package.get("members", []):
                executable = (
                    member["component_type"] == "recipe"
                    and member["component_id"] in trusted_recipe_ids
                )
                if member["component_type"] == "recipe" and not executable:
                    disabled.append(member["component_id"])
                self.conn.execute(
                    "INSERT OR IGNORE INTO research_package_imported_components VALUES (?,?,?,?,?,?,?)",
                    [
                        target_namespace,
                        package_hash,
                        member["component_type"],
                        member["component_id"],
                        json.dumps(
                            member["content"],
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        member["content_hash"],
                        executable,
                    ],
                )
                imported += 1
            receipt = {
                "contract": IMPORT_CONTRACT,
                "import_id": import_id,
                "package_hash": package_hash,
                "target_namespace": target_namespace,
                "status": "committed",
                "imported": imported,
                "disabled_recipes": sorted(disabled),
                "collisions": [],
            }
            now = self.now()
            self.conn.execute(
                "INSERT INTO research_package_imports VALUES (?,?,?,?,?,?)",
                [
                    import_id,
                    package_hash,
                    target_namespace,
                    "committed",
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    now,
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._audit(
            target_namespace,
            "import",
            import_id,
            principal_id,
            {"source_namespace": package.get("namespace")},
        )
        return {**receipt, "idempotent": False}

    def replay(self, target_namespace, import_id, *, scopes, allow_executable=False):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT package_hash,status FROM research_package_imports WHERE target_namespace=? AND import_id=?",
            [target_namespace, import_id],
        ).fetchone()
        if not row:
            raise ResearchPackageError(
                "import_not_found", "research package import not found"
            )
        components = self.conn.execute(
            "SELECT component_type,component_id,content_hash,executable FROM research_package_imported_components WHERE target_namespace=? AND package_hash=? ORDER BY component_type,component_id",
            [target_namespace, row[0]],
        ).fetchall()
        executable = [
            r[1] for r in components if r[0] == "recipe" and r[3] and allow_executable
        ]
        return {
            "contract": IMPORT_CONTRACT,
            "import_id": import_id,
            "package_hash": row[0],
            "target_namespace": target_namespace,
            "status": "replayed",
            "imported": len(components),
            "disabled_recipes": [
                r[1] for r in components if r[0] == "recipe" and r[1] not in executable
            ],
            "collisions": [],
            "executed_recipes": executable,
            "deterministic_hash": _hash(components),
        }

    def rollback(self, target_namespace, import_id, *, principal_id, scopes):
        _require(scopes, IMPORT_SCOPE)
        row = self.conn.execute(
            "SELECT package_hash,status FROM research_package_imports WHERE target_namespace=? AND import_id=?",
            [target_namespace, import_id],
        ).fetchone()
        if not row:
            raise ResearchPackageError(
                "import_not_found", "research package import not found"
            )
        receipt = {
            "contract": IMPORT_CONTRACT,
            "import_id": import_id,
            "package_hash": row[0],
            "target_namespace": target_namespace,
            "status": "rolled_back",
            "imported": 0,
            "disabled_recipes": [],
            "collisions": [],
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "DELETE FROM research_package_imported_components WHERE target_namespace=? AND package_hash=?",
                [target_namespace, row[0]],
            )
            self.conn.execute(
                "UPDATE research_package_imports SET status='rolled_back',receipt_json=? WHERE target_namespace=? AND import_id=?",
                [
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    target_namespace,
                    import_id,
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._audit(target_namespace, "rollback", import_id, principal_id)
        return receipt

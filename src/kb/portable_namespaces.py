"""Deterministic, content-addressed packages for knowledge namespaces."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MANIFEST_CONTRACT = "noesis-knowledge-package-manifest-v1"
PACKAGE_CONTRACT = "noesis-knowledge-package-v1"
RECEIPT_CONTRACT = "noesis-knowledge-disclosure-receipt-v1"
READ_SCOPE = "knowledge:namespace:export"
WRITE_SCOPE = "knowledge:namespace:import"
SENSITIVE_KINDS = frozenset({"attachment", "embedding"})
COMPONENT_KINDS = ("object", "relation", "document", "chunk", "embedding", "schema", "provenance", "index", "attachment")

_DDL = """
CREATE TABLE IF NOT EXISTS portable_namespace_components (
  namespace TEXT NOT NULL, kind TEXT NOT NULL, component_id TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, source_id TEXT,
  sensitivity TEXT NOT NULL, observed_at_ms BIGINT, dependencies_json TEXT NOT NULL,
  PRIMARY KEY(namespace, kind, component_id)
);
CREATE TABLE IF NOT EXISTS portable_namespace_imports (
  import_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  package_hash TEXT NOT NULL, target_namespace TEXT NOT NULL, policy TEXT NOT NULL,
  status TEXT NOT NULL, result_json TEXT NOT NULL, imported_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS portable_namespace_audit (
  event_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, action TEXT NOT NULL,
  namespace TEXT NOT NULL, package_hash TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class PortableNamespaceError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message); self.code,self.message,self.details=code,message,details
    def as_dict(self) -> dict[str, Any]:
        result={"code":self.code,"message":self.message}
        if self.details: result["details"]=self.details
        return result


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()


def _digest(value: Any) -> str: return hashlib.sha256(canonical_bytes(value)).hexdigest()
def _load(value: Any, default: Any) -> Any: return default if value is None else json.loads(value) if isinstance(value,str) else value
def _table(conn: Any, name: str) -> bool: return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?",[name]).fetchone())


def _scope(scopes: set[str], required: str, namespace: str) -> None:
    if "operator" in scopes: return
    if required not in scopes or f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes:
        raise PortableNamespaceError("unauthorized",f"namespace {namespace!r} is not authorized")


def _component(kind: str, component_id: str, content: Any, *, dependencies: Sequence[str] = (), source_id: str | None = None, sensitivity: str = "public", observed_at_ms: int | None = None) -> dict[str, Any]:
    core={"kind":kind,"component_id":str(component_id),"content":content,"dependencies":sorted(set(dependencies)),"source_id":source_id,"sensitivity":sensitivity,"observed_at_ms":observed_at_ms}
    core["content_hash"]=_digest(core["content"]); return core


class PortableNamespaceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, max_package_bytes: int = 50_000_000, max_components: int = 100_000) -> None:
        self.conn,self.max_package_bytes,self.max_components=conn,max_package_bytes,max_components
        if initialize: conn.execute(_DDL)

    def put_component(self, namespace: str, kind: str, component_id: str, content: Any, *, dependencies: Sequence[str] = (), source_id: str | None = None, sensitivity: str = "public", observed_at_ms: int | None = None) -> dict[str, Any]:
        if kind not in COMPONENT_KINDS: raise PortableNamespaceError("invalid_component","unsupported component kind")
        value=_component(kind,component_id,content,dependencies=dependencies,source_id=source_id,sensitivity=sensitivity,observed_at_ms=observed_at_ms)
        self.conn.execute("INSERT OR REPLACE INTO portable_namespace_components VALUES (?,?,?,?,?,?,?,?,?)",[namespace,kind,component_id,json.dumps(content,sort_keys=True,separators=(",",":")),value["content_hash"],source_id,sensitivity,observed_at_ms,json.dumps(value["dependencies"],separators=(",",":"))]); return value

    def _native_components(self, namespace: str) -> list[dict[str, Any]]:
        values=[]
        if _table(self.conn,"knowledge_objects"):
            rows=self.conn.execute("SELECT object_id,object_type,value_json,metadata_json,provenance_json,evidence_json,revision,retracted,created_at_ms,updated_at_ms FROM knowledge_objects WHERE namespace=? ORDER BY object_id",[namespace]).fetchall()
            for row in rows:
                content={"object_type":row[1],"value":_load(row[2],{}),"metadata":_load(row[3],{}),"provenance":_load(row[4],{}),"evidence":_load(row[5],[]),"revision":int(row[6]),"retracted":bool(row[7]),"created_at_ms":int(row[8]),"updated_at_ms":int(row[9])}
                values.append(_component("object",row[0],content,source_id=content["provenance"].get("source_id"),sensitivity=content["metadata"].get("sensitivity","public"),observed_at_ms=content["updated_at_ms"]))
        if _table(self.conn,"knowledge_relations"):
            rows=self.conn.execute("SELECT relation_id,subject_id,predicate,object_id,metadata_json,provenance_json,evidence_json,revision,retracted,created_at_ms,updated_at_ms FROM knowledge_relations WHERE namespace=? ORDER BY relation_id",[namespace]).fetchall()
            for row in rows:
                content={"subject_id":row[1],"predicate":row[2],"object_id":row[3],"metadata":_load(row[4],{}),"provenance":_load(row[5],{}),"evidence":_load(row[6],[]),"revision":int(row[7]),"retracted":bool(row[8]),"created_at_ms":int(row[9]),"updated_at_ms":int(row[10])}
                values.append(_component("relation",row[0],content,dependencies=[row[1],row[3]],source_id=content["provenance"].get("source_id"),sensitivity=content["metadata"].get("sensitivity","public"),observed_at_ms=content["updated_at_ms"]))
        return values

    def _stored_components(self, namespace: str) -> list[dict[str, Any]]:
        if not _table(self.conn,"portable_namespace_components"): return []
        rows=self.conn.execute("SELECT kind,component_id,content_json,content_hash,source_id,sensitivity,observed_at_ms,dependencies_json FROM portable_namespace_components WHERE namespace=? ORDER BY kind,component_id",[namespace]).fetchall()
        return [{"kind":r[0],"component_id":r[1],"content":_load(r[2],{}),"content_hash":r[3],"source_id":r[4],"sensitivity":r[5],"observed_at_ms":r[6],"dependencies":_load(r[7],[])} for r in rows]

    @staticmethod
    def _filter(components: list[dict[str, Any]], filters: Mapping[str, Any]) -> tuple[list[dict[str, Any]],list[dict[str,Any]]]:
        selected=[]; omitted=[]; kinds=set(filters.get("kinds") or COMPONENT_KINDS); sources=set(filters.get("sources") or []); sensitivities=set(filters.get("sensitivities") or []); since=filters.get("since_ms"); until=filters.get("until_ms")
        for value in components:
            reason=None
            if value["kind"] not in kinds: reason="kind-filter"
            elif sources and value.get("source_id") not in sources: reason="source-filter"
            elif sensitivities and value.get("sensitivity") not in sensitivities: reason="sensitivity-filter"
            elif (
                since is not None
                and (value.get("observed_at_ms") is None or value["observed_at_ms"] < since)
            ) or (
                until is not None
                and (value.get("observed_at_ms") is None or value["observed_at_ms"] > until)
            ):
                reason="time-filter"
            (omitted if reason else selected).append({"kind":value["kind"],"component_id":value["component_id"],"reason":reason} if reason else value)
        return selected,omitted

    def export(self, namespace: str, *, mode: str = "full", filters: Mapping[str, Any] | None = None, dependency_closure: bool = True, redaction: Mapping[str, Any] | None = None, scopes: set[str], cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        _scope(scopes,READ_SCOPE,namespace)
        if mode not in {"full","filtered","metadata-only"}: raise PortableNamespaceError("invalid_mode","mode must be full, filtered, or metadata-only")
        all_values={f"{v['kind']}:{v['component_id']}":v for v in self._native_components(namespace)+self._stored_components(namespace)}
        selected,omitted=self._filter([all_values[k] for k in sorted(all_values)],filters or {})
        if dependency_closure:
            wanted={dep for value in selected for dep in value["dependencies"]}
            for value in all_values.values():
                if value["component_id"] in wanted and value not in selected: selected.append(value); omitted=[item for item in omitted if item["component_id"]!=value["component_id"]]
        policy=dict(redaction or {}); excluded=set(policy.get("sensitivities") or []); fields=set(policy.get("fields") or [])
        transformed=[]; kept=[]
        for value in sorted(selected,key=lambda x:(x["kind"],x["component_id"])):
            if cancelled and cancelled(): raise PortableNamespaceError("cancelled","export cancelled without mutating the namespace")
            if value["sensitivity"] in excluded:
                omitted.append({"kind":value["kind"],"component_id":value["component_id"],"reason":"redaction-policy"}); continue
            clone=json.loads(json.dumps(value))
            for field in fields:
                if isinstance(clone["content"],dict) and field in clone["content"]: clone["content"].pop(field); transformed.append({"component_id":clone["component_id"],"field":field})
            clone["content_hash"]=_digest(clone["content"]); kept.append(clone)
        retained_ids={item["component_id"] for item in kept}
        for item in kept:
            missing=sorted(set(item["dependencies"])-retained_ids)
            if missing: item["dependencies"]=[dep for dep in item["dependencies"] if dep in retained_ids]; transformed.append({"component_id":item["component_id"],"removed_private_dependencies":missing})
        summaries=[{"kind":v["kind"],"component_id":v["component_id"],"content_hash":v["content_hash"],"dependencies":v["dependencies"],"sensitivity":v["sensitivity"]} for v in kept]
        payload=[] if mode=="metadata-only" else kept
        manifest={"contract":MANIFEST_CONTRACT,"format_version":"1.0.0","namespace":namespace,"mode":mode,"filters":dict(filters or {}),"dependency_closure":dependency_closure,"components":summaries,"component_counts":{kind:sum(v["kind"]==kind for v in kept) for kind in COMPONENT_KINDS},"dependencies":sorted({dep for v in kept for dep in v["dependencies"]}),"omissions":sorted(omitted,key=lambda x:(x["kind"],x["component_id"])),"compatibility":{"minimum_reader":"1.0.0","schema_policy":"validate-before-import"}}
        manifest["content_hash"]=_digest({"manifest":manifest,"payload":payload})
        receipt={"contract":RECEIPT_CONTRACT,"namespace":namespace,"package_hash":manifest["content_hash"],"included":[{"kind":v["kind"],"component_id":v["component_id"]} for v in kept],"omitted":manifest["omissions"],"transformed":transformed,"unverifiable":([] if payload else [v["component_id"] for v in kept])}
        package={"contract":PACKAGE_CONTRACT,"manifest":manifest,"payload":payload,"disclosure_receipt":receipt}
        package["byte_length"]=len(canonical_bytes(package)); return package

    def verify(self, package: Mapping[str, Any]) -> dict[str, Any]:
        encoded=canonical_bytes(package)
        if len(encoded)>self.max_package_bytes: raise PortableNamespaceError("package_too_large","package exceeds decompression or byte limit")
        manifest=dict(package.get("manifest") or {}); payload=list(package.get("payload") or [])
        if manifest.get("contract")!=MANIFEST_CONTRACT or package.get("contract")!=PACKAGE_CONTRACT: raise PortableNamespaceError("invalid_package","unsupported package contract")
        if len(manifest.get("components",[]))>self.max_components: raise PortableNamespaceError("too_many_components","component limit exceeded")
        claimed=manifest.pop("content_hash",None); actual=_digest({"manifest":manifest,"payload":payload}); manifest["content_hash"]=claimed
        errors=[]
        if not hmac_compare(claimed,actual): errors.append("package content hash mismatch")
        declarations={(v["kind"],v["component_id"]):v for v in manifest.get("components",[])}
        for item in payload:
            declared=declarations.get((item.get("kind"),item.get("component_id")))
            if not declared or item.get("content_hash")!=_digest(item.get("content")) or item.get("content_hash")!=declared.get("content_hash"): errors.append(f"component hash mismatch: {item.get('component_id')}")
        present={v.get("component_id") for v in manifest.get("components",[])}
        for item in manifest.get("components",[]):
            missing=set(item.get("dependencies",[]))-present
            if missing: errors.append(f"missing dependencies for {item.get('component_id')}: {sorted(missing)}")
        return {"valid":not errors,"package_hash":claimed,"errors":errors,"components":len(declarations)}

    def preview_import(self, package: Mapping[str, Any], target_namespace: str, *, conflict_policy: str = "reject", remap: Mapping[str,str] | None = None, scopes: set[str]) -> dict[str, Any]:
        _scope(scopes,WRITE_SCOPE,target_namespace); verification=self.verify(package)
        if not verification["valid"]: raise PortableNamespaceError("verification_failed","package validation failed",errors=verification["errors"])
        if conflict_policy not in {"new-namespace","reject","keep-both","remap"}: raise PortableNamespaceError("invalid_policy","unsupported conflict policy")
        existing={(r[0],r[1]):r[2] for r in self.conn.execute("SELECT kind,component_id,content_hash FROM portable_namespace_components WHERE namespace=?",[target_namespace]).fetchall()}; additions=[]; conflicts=[]; unchanged=[]
        for item in package.get("payload",[]):
            component_id=(remap or {}).get(item["component_id"],item["component_id"]); key=(item["kind"],component_id)
            if key not in existing: additions.append(key)
            elif existing[key]==item["content_hash"]: unchanged.append(key)
            else: conflicts.append(key)
        if conflict_policy=="new-namespace" and existing: conflicts.append(("namespace",target_namespace))
        return {"contract":"noesis-knowledge-package-import-preview-v1","package_hash":verification["package_hash"],"target_namespace":target_namespace,"policy":conflict_policy,"additions":[list(v) for v in additions],"conflicts":[list(v) for v in conflicts],"unchanged":[list(v) for v in unchanged],"counts":{"additions":len(additions),"conflicts":len(conflicts),"unchanged":len(unchanged)},"preview_hash":_digest({"package":verification["package_hash"],"target":target_namespace,"policy":conflict_policy,"remap":remap or {},"existing":[[kind,component_id,content_hash] for (kind,component_id),content_hash in sorted(existing.items())]})}

    def import_package(self, package: Mapping[str, Any], target_namespace: str, idempotency_key: str, *, conflict_policy: str = "reject", remap: Mapping[str,str] | None = None, scopes: set[str], principal_id: str, expected_preview_hash: str | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        prior=self.conn.execute("SELECT package_hash,result_json FROM portable_namespace_imports WHERE idempotency_key=?",[idempotency_key]).fetchone()
        if prior:
            if prior[0]!=package.get("manifest",{}).get("content_hash"): raise PortableNamespaceError("idempotency_conflict","idempotency key was reused")
            return json.loads(prior[1])
        preview=self.preview_import(package,target_namespace,conflict_policy=conflict_policy,remap=remap,scopes=scopes)
        if expected_preview_hash and expected_preview_hash!=preview["preview_hash"]: raise PortableNamespaceError("stale_preview","import state changed after preview")
        if preview["conflicts"] and conflict_policy in {"reject","new-namespace"}: raise PortableNamespaceError("conflict","package conflicts with target namespace",conflicts=preview["conflicts"])
        if package["manifest"].get("mode") == "metadata-only": raise PortableNamespaceError("metadata_only","metadata-only packages cannot be imported as data")
        now=int(time.time()*1000); imported=0; renamed={}; self.conn.execute("BEGIN")
        try:
            for item in package.get("payload",[]):
                if cancelled and cancelled(): raise PortableNamespaceError("cancelled","import cancelled and rolled back")
                component_id=(remap or {}).get(item["component_id"],item["component_id"])
                exists=self.conn.execute("SELECT content_hash FROM portable_namespace_components WHERE namespace=? AND kind=? AND component_id=?",[target_namespace,item["kind"],component_id]).fetchone()
                if exists and exists[0]!=item["content_hash"] and conflict_policy=="keep-both":
                    replacement=f"{component_id}~{item['content_hash'][:8]}"; renamed[component_id]=replacement; component_id=replacement
                elif exists: continue
                dependencies=[renamed.get((remap or {}).get(dep,dep),(remap or {}).get(dep,dep)) for dep in item.get("dependencies",[])]
                self.conn.execute("INSERT INTO portable_namespace_components VALUES (?,?,?,?,?,?,?,?,?)",[target_namespace,item["kind"],component_id,json.dumps(item["content"],sort_keys=True,separators=(",",":")),item["content_hash"],item.get("source_id"),item.get("sensitivity","public"),item.get("observed_at_ms"),json.dumps(dependencies,separators=(",",":"))]); imported+=1
            result={"contract":"noesis-knowledge-package-import-result-v1","package_hash":preview["package_hash"],"target_namespace":target_namespace,"policy":conflict_policy,"imported":imported,"renamed":renamed,"status":"committed"}; import_id="import:"+_digest({"key":idempotency_key,"package":preview["package_hash"]})[:24]
            self.conn.execute("INSERT INTO portable_namespace_imports VALUES (?,?,?,?,?,'committed',?,?)",[import_id,idempotency_key,preview["package_hash"],target_namespace,conflict_policy,json.dumps(result,sort_keys=True,separators=(",",":")),now]); self.conn.execute("INSERT INTO portable_namespace_audit VALUES (?,?,?,?,?,?,?)",["audit:"+_digest([import_id,now])[:24],principal_id,"import",target_namespace,preview["package_hash"],json.dumps({"policy":conflict_policy,"imported":imported},separators=(",",":")),now]); self.conn.execute("COMMIT"); return result
        except Exception:
            self.conn.execute("ROLLBACK"); raise


def hmac_compare(left: Any, right: Any) -> bool:
    import hmac
    return isinstance(left,str) and hmac.compare_digest(left,right)


def sign_package(package: Mapping[str, Any], private_key: bytes, *, key_id: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key=Ed25519PrivateKey.from_private_bytes(private_key); signature=key.sign(canonical_bytes(package))
    return {"algorithm":"Ed25519","key_id":key_id,"package_hash":package["manifest"]["content_hash"],"signature":base64.b64encode(signature).decode()}


def verify_signature(package: Mapping[str, Any], signature: Mapping[str, Any], public_key: bytes, *, required_key_id: str | None = None) -> dict[str, Any]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if required_key_id and signature.get("key_id")!=required_key_id: return {"valid":False,"reason":"signature-policy"}
    try: Ed25519PublicKey.from_public_bytes(public_key).verify(base64.b64decode(signature["signature"]),canonical_bytes(package))
    except (InvalidSignature,ValueError,KeyError): return {"valid":False,"reason":"invalid-signature"}
    return {"valid":True,"key_id":signature.get("key_id"),"package_hash":signature.get("package_hash")}


def encrypt_package(package: Mapping[str, Any], recipient_key: bytes, *, recipient_id: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(recipient_key)!=32: raise PortableNamespaceError("invalid_key","recipient AES-256 key must be 32 bytes")
    nonce=os.urandom(12); ciphertext=AESGCM(recipient_key).encrypt(nonce,canonical_bytes(package),recipient_id.encode())
    return {"contract":"noesis-encrypted-knowledge-package-v1","algorithm":"AES-256-GCM","recipient_id":recipient_id,"nonce":base64.b64encode(nonce).decode(),"ciphertext":base64.b64encode(ciphertext).decode()}


def decrypt_package(envelope: Mapping[str, Any], recipient_key: bytes, *, recipient_id: str, max_plaintext_bytes: int = 50_000_000) -> dict[str, Any]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if envelope.get("recipient_id")!=recipient_id: raise PortableNamespaceError("wrong_recipient","encrypted package targets another recipient")
    try: plaintext=AESGCM(recipient_key).decrypt(base64.b64decode(envelope["nonce"]),base64.b64decode(envelope["ciphertext"]),recipient_id.encode())
    except (InvalidTag,ValueError,KeyError) as exc: raise PortableNamespaceError("decryption_failed","wrong key or tampered package") from exc
    if len(plaintext)>max_plaintext_bytes: raise PortableNamespaceError("package_too_large","decrypted package exceeds byte limit")
    try: return json.loads(plaintext)
    except json.JSONDecodeError as exc: raise PortableNamespaceError("invalid_package","decrypted payload is not JSON") from exc

"""Declarative REST/OpenAPI ingestion with strict network and size bounds."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import RawDocument, SourceRef

CONTRACT = "noesis-declarative-api-source-v1"
TARGETS = frozenset({"Document", "DatasetSeries", "registered-schema"})


class DeclarativeAPIError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message); self.code,self.message,self.details=code,message,details


def _canonical(value: Any) -> str: return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _digest(value: Any) -> str: return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _path(value: Any, path: str, default: Any=None) -> Any:
    current=value
    for part in path.split(".") if path else []:
        if isinstance(current,Mapping): current=current.get(part,default)
        elif isinstance(current,list) and part.isdigit() and int(part)<len(current): current=current[int(part)]
        else: return default
    return current


def _safe_base_url(url: str, allowed_hosts: set[str], resolver: Callable[[str],list[str]] | None=None) -> str:
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DeclarativeAPIError("unsafe_url","API base URL must be credential-free HTTPS")
    hostname=parsed.hostname.casefold()
    if hostname not in {host.casefold() for host in allowed_hosts}: raise DeclarativeAPIError("host_forbidden","API host is not allowlisted")
    addresses=(resolver or (lambda host:[item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)]))(hostname)
    for address in addresses:
        ip=ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise DeclarativeAPIError("ssrf_blocked","API host resolves to a non-public address")
    return url.rstrip("/")


def manifest_from_openapi(spec: Mapping[str,Any], *, base_url: str, allowed_hosts: list[str], operation_ids: list[str], mapping: Mapping[str,Any], license_name: str) -> dict[str,Any]:
    """Select only explicitly allowlisted GET operations from an OpenAPI document."""
    operations=[]
    for path,item in sorted((spec.get("paths") or {}).items()):
        operation=(item or {}).get("get") or {}; operation_id=operation.get("operationId")
        if operation_id not in operation_ids: continue
        parameters=[]
        for parameter in list(item.get("parameters") or [])+list(operation.get("parameters") or []):
            if parameter.get("in") in {"query","path"}: parameters.append(str(parameter.get("name")))
        operations.append({"operation_id":operation_id,"method":"GET","path":path,"parameters":sorted(set(parameters)),"items_path":"items","mapping":dict(mapping)})
    if set(operation_ids)-{item["operation_id"] for item in operations}: raise DeclarativeAPIError("operation_missing","an allowlisted OpenAPI operation is absent or not GET")
    return {"contract":CONTRACT,"source_id":str(spec.get("info",{}).get("title") or "openapi"),"base_url":base_url,"allowed_hosts":allowed_hosts,"operations":operations,"target":"Document","pagination":{"kind":"none","max_pages":1},"rate":{"requests_per_minute":60},"limits":{"timeout_ms":5000,"max_response_bytes":2_000_000},"license":license_name}


class DeclarativeAPIConnector:
    """Execute a validated manifest through an injectable HTTP transport."""
    name="declarative-rest"; source_type="web"

    def __init__(self, manifest: Mapping[str,Any], *, transport: Callable[...,Any] | None=None, secret_resolver: Callable[[str],str] | None=None, dns_resolver: Callable[[str],list[str]] | None=None, schema_validator: Callable[[str,Any],Sequence[Mapping[str,Any]]] | None=None) -> None:
        self.manifest=self.validate_manifest(manifest,dns_resolver=dns_resolver); self.transport=transport or self._request; self.secret_resolver=secret_resolver or (lambda _:""); self.schema_validator=schema_validator; self.validators: dict[str,dict[str,str]]={}; self._calls: list[int]=[]

    @staticmethod
    def validate_manifest(manifest: Mapping[str,Any], *, dns_resolver: Callable[[str],list[str]] | None=None) -> dict[str,Any]:
        value=json.loads(json.dumps(manifest)); required={"source_id","base_url","allowed_hosts","operations","target","pagination","rate","limits","license"}
        if value.get("contract")!=CONTRACT or required-set(value): raise DeclarativeAPIError("invalid_manifest","declarative API manifest is incomplete")
        value["base_url"]=_safe_base_url(value["base_url"],set(value["allowed_hosts"]),dns_resolver)
        if value["target"] not in TARGETS: raise DeclarativeAPIError("invalid_target","unsupported mapping target")
        if not 1<=int(value["pagination"].get("max_pages",0))<=100: raise DeclarativeAPIError("pagination_unbounded","max_pages must be between one and 100")
        if not 1<=int(value["limits"].get("max_response_bytes",0))<=50_000_000: raise DeclarativeAPIError("response_unbounded","max response bytes must be explicitly bounded")
        seen=set()
        for operation in value["operations"]:
            if operation.get("method")!="GET" or not str(operation.get("path","")).startswith("/"): raise DeclarativeAPIError("operation_forbidden","only relative GET operations are allowed")
            if operation.get("operation_id") in seen: raise DeclarativeAPIError("duplicate_operation","operation ids must be unique")
            seen.add(operation.get("operation_id"))
        value["manifest_hash"]=_digest(value); return value

    def describe(self) -> dict[str,Any]:
        clean={key:value for key,value in self.manifest.items() if key not in {"headers","secrets"}}
        clean["operations"]=[{key:value for key,value in item.items() if key not in {"headers","secrets"}} for item in clean["operations"]]; return clean

    def _operation(self,name: str) -> dict[str,Any]:
        for operation in self.manifest["operations"]:
            if operation["operation_id"]==name: return operation
        raise DeclarativeAPIError("operation_forbidden","operation is not allowlisted")

    def discover(self, query: Any=None):
        request=dict(query or {}); name=str(request.pop("operation_id", "")); operation=self._operation(name); allowed=set(operation.get("parameters") or [])
        if set(request)-allowed: raise DeclarativeAPIError("parameter_forbidden","request includes a parameter not declared by the operation")
        identity={"source":self.manifest["source_id"],"operation":name,"parameters":request,"manifest_hash":self.manifest["manifest_hash"]}
        yield SourceRef(locator=name,metadata={"source_id":self.manifest["source_id"],"request_identity":_digest(identity),"parameters":request})

    def _headers(self,operation: Mapping[str,Any]) -> dict[str,str]:
        headers={"Accept":"application/json"}; headers.update(self.validators.get(str(operation["operation_id"]),{}))
        for name,secret_ref in (operation.get("secret_headers") or {}).items(): headers[str(name)]=self.secret_resolver(str(secret_ref))
        return headers

    @staticmethod
    def _request(*,method: str,url: str,params: Mapping[str,Any],headers: Mapping[str,str],timeout: float):
        encoded=urllib.parse.urlencode(params); target=url+("?"+encoded if encoded else ""); request=urllib.request.Request(target,method=method,headers=dict(headers))
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return {"status":response.status,"headers":dict(response.headers),"content":response.read()}

    def _rate_limit(self) -> None:
        now=int(time.time()*1000); self._calls=[stamp for stamp in self._calls if now-stamp<60_000]
        if len(self._calls)>=int(self.manifest["rate"]["requests_per_minute"]): raise DeclarativeAPIError("rate_limited","declarative source rate limit reached")
        self._calls.append(now)

    def fetch(self,ref: SourceRef) -> RawDocument:
        operation=self._operation(ref.locator); max_pages=int(self.manifest["pagination"]["max_pages"]); params=dict(ref.metadata.get("parameters") or {}); pages=[]; response_headers={}; fetched_at=int(time.time()*1000)
        total_bytes=0
        for page in range(max_pages):
            self._rate_limit(); path=str(operation["path"]); query_params=dict(params)
            for name in operation.get("parameters") or []:
                marker="{"+name+"}"
                if marker in path:
                    if name not in query_params: raise DeclarativeAPIError("parameter_required",f"path parameter {name!r} is required")
                    path=path.replace(marker,urllib.parse.quote(str(query_params.pop(name)),safe=""))
            url=self.manifest["base_url"]+path
            response=self.transport(method="GET",url=url,params=query_params,headers=self._headers(operation),timeout=int(self.manifest["limits"]["timeout_ms"])/1000)
            status=int(response.get("status",200)); content=response.get("content",b""); content=content.encode() if isinstance(content,str) else bytes(content)
            if status==304: return RawDocument(ref=ref,content=b'{"items":[]}',content_type="application/json",fetched_at=fetched_at)
            if status<200 or status>=300: raise DeclarativeAPIError("http_error",f"API returned status {status}")
            total_bytes+=len(content)
            if total_bytes>int(self.manifest["limits"]["max_response_bytes"]): raise DeclarativeAPIError("response_too_large","API response exceeds the byte limit")
            try: payload=json.loads(content)
            except json.JSONDecodeError as exc: raise DeclarativeAPIError("schema_drift","API response is not valid JSON") from exc
            items=_path(payload,str(operation.get("items_path") or ""),payload)
            if not isinstance(items,list): raise DeclarativeAPIError("schema_drift","configured items path is not an array")
            pages.extend(items); response_headers={str(k).casefold():str(v) for k,v in response.get("headers",{}).items()}
            next_value=_path(payload,str(self.manifest["pagination"].get("next_path") or "")) if self.manifest["pagination"].get("kind")!="none" else None
            if not next_value: break
            params[str(self.manifest["pagination"].get("parameter","page"))]=next_value
        else:
            raise DeclarativeAPIError("pagination_limit","response still had a next page at max_pages")
        validators={}
        if response_headers.get("etag"): validators["If-None-Match"]=response_headers["etag"]
        if response_headers.get("last-modified"): validators["If-Modified-Since"]=response_headers["last-modified"]
        self.validators[ref.locator]=validators
        metadata={key:value for key,value in ref.metadata.items() if key!="parameters"}; metadata.update({"response_timestamp_ms":fetched_at,"cache_validators":validators,"source_license":self.manifest["license"],"mapping_hash":_digest(operation.get("mapping") or {}),"operation_id":ref.locator})
        safe_ref=SourceRef(locator=ref.locator,metadata=metadata); return RawDocument(ref=safe_ref,content=_canonical({"items":pages}),content_type="application/json",fetched_at=fetched_at)

    def parse(self,raw: RawDocument) -> list[Any]:
        operation=self._operation(raw.ref.locator); payload=json.loads(raw.content); mapping=dict(operation.get("mapping") or {}); outputs=[]
        for index,item in enumerate(payload["items"]):
            mapped={target:_path(item,str(path)) for target,path in mapping.items()}; provenance={"request_identity":raw.ref.metadata["request_identity"],"response_timestamp_ms":raw.ref.metadata["response_timestamp_ms"],"source_license":raw.ref.metadata["source_license"],"mapping_hash":raw.ref.metadata["mapping_hash"],"operation_id":raw.ref.locator,"item_index":index}
            if self.manifest["target"]=="Document":
                required={"document_id","source_type","language","ingested_at"}
                if required-set(mapped) or any(mapped.get(field) is None for field in required): raise DeclarativeAPIError("mapping_incomplete","Document mapping lacks required fields")
                mapped.setdefault("metadata",{}); mapped["metadata"]={**(mapped["metadata"] or {}),"api_provenance":provenance}; outputs.append(Document.from_dict(mapped))
            else:
                if self.manifest["target"]=="registered-schema" and self.schema_validator:
                    errors=list(self.schema_validator(str(self.manifest.get("schema_ref") or ""),mapped))
                    if errors: raise DeclarativeAPIError("schema_drift","mapped response violates its registered schema",errors=errors[:20])
                outputs.append({"target":self.manifest["target"],"schema_ref":self.manifest.get("schema_ref"),"value":mapped,"provenance":provenance})
        return outputs

    def run(self,operation_id: str,parameters: Mapping[str,Any] | None=None) -> list[Any]:
        ref=next(iter(self.discover({"operation_id":operation_id,**dict(parameters or {})}))); return self.parse(self.fetch(ref))

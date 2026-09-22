"""Versioned RO-Crate 1.1 mapping of already-authorized native package members."""

import base64
import hashlib
import io
import json
import re
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from .common import IntegrationError, digest, version

MAPPING = "noesis-ro-crate-mapping-v1"


def _bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _publication(package, metadata):
    metadata = metadata or package.get("manifest", {}).get("extensions", {}).get(
        "x-ro-crate", {}
    )
    if not isinstance(metadata, dict):
        raise IntegrationError(
            "missing_metadata", "RO-Crate publication metadata required"
        )
    if len(_bytes(metadata)) > 65536 or set(metadata) - {
        "datePublished",
        "license",
        "name",
        "description",
    }:
        raise IntegrationError(
            "invalid_metadata", "Unsupported or oversized publication metadata"
        )
    for field in ("name", "description"):
        if field in metadata and (
            not isinstance(metadata[field], str) or not metadata[field].strip()
        ):
            raise IntegrationError(
                "invalid_metadata", "Publication text fields must be nonempty strings"
            )
    published, license_id = metadata.get("datePublished"), metadata.get("license")
    try:
        if not isinstance(published, str):
            raise TypeError()
        date.fromisoformat(published)
    except (ValueError, TypeError) as exc:
        raise IntegrationError(
            "missing_metadata", "Explicit ISO publication date required"
        ) from exc
    if not isinstance(license_id, str) or not re.fullmatch(
        r"https://[^\s]+", license_id
    ):
        raise IntegrationError(
            "missing_metadata", "Explicit HTTPS license identifier required"
        )
    return metadata


def export_package(package, *, metadata=None):
    from rocrate.model.contextentity import ContextEntity
    from rocrate.rocrate import ROCrate

    from src.kb.research_packages import ResearchPackageStore

    if package.get("status") not in {"complete", "partial"}:
        raise IntegrationError(
            "invalid_package", "Only built native packages can be exported"
        )
    publication = _publication(package, metadata)
    raw = _bytes(package)
    members = package.get("members", [])
    if len(raw) > 32_000_000 or not isinstance(members, list) or len(members) > 10000:
        raise IntegrationError("input_limit", "Package exceeds export limits")
    verification = ResearchPackageStore(None, initialize=False).verify(package)
    declared_missing = {
        item["component_id"]
        for item in package.get("closure", {}).get("omissions", [])
        if item.get("reason") == "missing"
    }
    partial_integrity = (
        package["status"] == "partial"
        and verification["content_hash"] == verification["actual_hash"]
        and not verification["member_failures"]
        and not verification["structural_errors"]
        and all(
            item["component_id"] in declared_missing
            for item in verification["missing_members"]
        )
    )
    if not verification["valid"] and not partial_integrity:
        raise IntegrationError("invalid_package", "Native package verification failed")
    crate = ROCrate(version="1.1")
    crate.metadata.extra_contexts.append({"sha256": "http://schema.org/sha256"})
    crate.name = publication.get(
        "name", "Noesis research package " + package["package_id"]
    )
    crate.root_dataset["description"] = publication.get(
        "description",
        "Authorized Noesis package components and native provenance; export is not a trust or access grant.",
    )
    crate.root_dataset["datePublished"] = publication["datePublished"]
    license_entity = crate.add(
        ContextEntity(
            crate,
            publication["license"],
            properties={
                "@type": "CreativeWork",
                "name": publication["license"],
                "description": "License explicitly declared by the exporter; component restrictions remain authoritative.",
            },
        )
    )
    crate.root_dataset["license"] = license_entity
    crate.root_dataset["identifier"] = package["content_hash"]

    def add_json(path, content, properties):
        data = _bytes(content)
        return crate.add_file(
            io.BytesIO(data),
            dest_path=path,
            properties={
                "encodingFormat": "application/json",
                "contentSize": str(len(data)),
                "sha256": hashlib.sha256(data).hexdigest(),
                **properties,
            },
        )

    native = add_json(
        "native-package.json", package, {"name": "Original native Noesis package"}
    )
    entities, by_id, mapped = {}, {}, []
    for member in members:
        kind, identity = member.get("component_type"), member.get("component_id")
        if not all(isinstance(x, str) and x for x in (kind, identity)):
            raise IntegrationError("invalid_package", "Missing member identity")
        key = (kind, identity)
        if key in entities or "content" not in member:
            raise IntegrationError("invalid_package", "Duplicate or missing member")
        content_hash = hashlib.sha256(_bytes(member["content"])).hexdigest()
        if content_hash != member.get("content_hash"):
            raise IntegrationError(
                "invalid_package", "Native member content hash mismatch"
            )
        path = "components/" + digest([kind, identity, content_hash]) + ".json"
        info = member.get("metadata", {})
        types = ["File"]
        artifact_kind = info.get("artifact_kind", kind)
        if artifact_kind == "dataset":
            types.append("Dataset")
        elif artifact_kind == "report":
            types.append("Report")
        elif artifact_kind == "software":
            types.append("SoftwareSourceCode")
        else:
            types.append("CreativeWork")
        properties = {
            "@type": types,
            "name": info.get("title", identity),
            "identifier": identity,
            "isPartOf": native,
            "description": "Redacted native member"
            if member.get("redacted")
            else "Authorized native member",
        }
        if isinstance(info.get("version"), str):
            properties["version"] = info["version"]
        if isinstance(info.get("revision_id"), str):
            properties["identifier"] = [identity, info["revision_id"]]
        entity = add_json(path, member, properties)
        entities[key] = entity
        by_id.setdefault(identity, []).append(entity)
        for author in info.get("authors", []):
            if not isinstance(author, dict):
                continue
            orcid = author.get("orcid", "")
            if re.fullmatch(r"https://orcid.org/\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
                from src.ingestion.orcid import identifier

                try:
                    identifier(orcid)
                except IntegrationError:
                    continue
                person = crate.dereference(orcid) or crate.add(
                    ContextEntity(
                        crate,
                        orcid,
                        properties={
                            "@type": "Person",
                            "name": author.get("name", orcid),
                        },
                    )
                )
                entity.append_to("author", person)
        for software in info.get("software", []):
            if not isinstance(software, dict) or not all(
                isinstance(software.get(k), str) and software[k]
                for k in ("name", "version")
            ):
                continue
            software_id = "#software-" + digest(software)
            application = crate.dereference(software_id) or crate.add(
                ContextEntity(
                    crate,
                    software_id,
                    properties={
                        "@type": "SoftwareApplication",
                        "name": software["name"],
                        "softwareVersion": software["version"],
                    },
                )
            )
            action_id = "#creation-" + digest([key, software])
            crate.add(
                ContextEntity(
                    crate,
                    action_id,
                    properties={
                        "@type": "CreateAction",
                        "name": "Declared production software",
                        "instrument": application,
                        "result": entity,
                    },
                )
            )
        mapped.append(
            {
                "component_type": kind,
                "component_id": identity,
                "content_hash": content_hash,
                "path": path,
                "redacted": bool(member.get("redacted")),
            }
        )
    for member in members:
        entity = entities[(member["component_type"], member["component_id"])]
        # Dependencies express derivation inputs, not proof of a factual claim.
        for dependency in member.get("dependencies", []):
            for target in by_id.get(dependency, []):
                entity.append_to("isBasedOn", target)
    mapping = {
        "contract": MAPPING,
        "specification": "https://w3id.org/ro/crate/1.1",
        "native_content_hash": package["content_hash"],
        "native_verification": verification,
        "members": mapped,
        "omissions": package.get("closure", {}).get("omissions", []),
        "unmapped_semantics": [
            "Native trust/signature verification",
            "Access and encryption policies",
            "Executable recipe permissions",
            "Evidence support/contradiction and replay guarantees",
            "Metadata outside the explicitly documented projection",
        ],
    }
    add_json("noesis-mapping.json", mapping, {"name": MAPPING})
    with tempfile.TemporaryDirectory(prefix="noesis-rocrate-") as directory:
        crate.write(directory)
        # Fixed metadata and deterministic ordering provide byte-identical replay.
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(Path(directory).rglob("*")):
                if path.is_file():
                    entry = zipfile.ZipInfo(
                        path.relative_to(directory).as_posix(), (1980, 1, 1, 0, 0, 0)
                    )
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o644 << 16
                    archive.writestr(entry, path.read_bytes())
        data = output.getvalue()
    return {
        "format": "ro-crate",
        "mapping_contract": MAPPING,
        "bytes_b64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "native_content_hash": package["content_hash"],
        "producer": {"backend": "rocrate", "version": version("rocrate")},
        "limitations": mapping["unmapped_semantics"],
    }

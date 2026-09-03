"""
NeuroNews Data-contract server — MCP server.

Wraps `contracts/` so the governed boundary between pipeline stages is a typed
tool, not a grep. When you touch a stage, the bug-catching question is "does the
output still satisfy the contract the next stage expects?" — this answers it
with a compact pass/fail + drift report.

Tools:
  list_contracts()              -> available contracts (id, title, stage aliases, kind)
  get_contract(stage)           -> the resolved JSON Schema + a compact field summary
  validate(stage, sample)       -> pass/fail + drift report (missing required,
                                   unexpected fields, type mismatches, errors)

`sample` accepts: an inline object, a path to a JSON file, or a named example
under contracts/examples/ (e.g. "valid-full-article").

Design: lazy imports, repo-root-relative, summaries not payloads. Validation is
JSON-Schema-based (the governed boundaries have JSON Schemas); Avro contracts are
listed and their schemas returned, but validate() targets JSON Schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"
JSONSCHEMA_DIR = CONTRACTS / "schemas" / "jsonschema"
AVRO_DIR = CONTRACTS / "schemas" / "avro"
EXAMPLES_DIR = CONTRACTS / "examples"

mcp = FastMCP("neuronews-contracts")

MAX_ERRORS = 15

# Friendly stage aliases -> contract id (filename stem). A stage name from the
# pipeline maps to the contract its output must satisfy. Exact ids also work.
STAGE_ALIASES = {
    "ingest": "article-ingest-v1",
    "connector": "article-ingest-v1",
    "scrape": "article-ingest-v1",
    "article": "article-ingest-v1",
    "article-ingest": "article-ingest-v1",
    "ask": "ask-request-v1",
    "ask-request": "ask-request-v1",
    "answer": "ask-response-v1",
    "ask-response": "ask-response-v1",
    "kb-answer": "noesis-answer-v1",
    "verifiable-answer": "noesis-answer-v1",
    "claim-watch": "noesis-claim-watch-v1",
    "watch-event": "noesis-claim-watch-v1",
    "evidence-independence": "noesis-evidence-independence-v1",
    "origin-graph": "noesis-evidence-independence-v1",
    "evidence-bundle": "noesis-evidence-bundle-v1",
    "bundle": "noesis-evidence-bundle-v1",
    "cli": "noesis-cli-v1",
    "command-line": "noesis-cli-v1",
    "search": "search-request",
    "article-request": "article-request",
}

# Avro counterpart ids for stages whose authoritative pipeline contract is Avro.
AVRO_ALIASES = {
    "ingest": "article-ingest-v1",
    "connector": "article-ingest-v1",
    "scrape": "article-ingest-v1",
    "article": "article-ingest-v1",
    "article-ingest": "article-ingest-v1",
    "ingested": "article-ingested",
    "sentiment": "sentiment-analyzed",
    "analyzed": "sentiment-analyzed",
    "query": "query-executed",
    "search-event": "query-executed",
}

# Avro primitive -> acceptable JSON types for a structural (not binary) check.
_AVRO_TO_JSON = {
    "string": {"string"}, "bytes": {"string"}, "enum": {"string"}, "fixed": {"string"},
    "int": {"integer"}, "long": {"integer"},
    "float": {"number", "integer"}, "double": {"number", "integer"},
    "boolean": {"boolean"}, "null": {"null"},
    "record": {"object"}, "map": {"object"}, "array": {"array"},
}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def _jsonschema_registry() -> dict[str, Path]:
    """id (filename stem) -> path, for every JSON Schema under contracts/."""
    reg: dict[str, Path] = {}
    if JSONSCHEMA_DIR.exists():
        for p in JSONSCHEMA_DIR.rglob("*.json"):
            reg[p.stem] = p
    return reg


def _avro_registry() -> dict[str, Path]:
    reg: dict[str, Path] = {}
    if AVRO_DIR.exists():
        for p in AVRO_DIR.rglob("*.avsc"):
            reg[p.stem] = p
    return reg


def _resolve(stage: str) -> Optional[Path]:
    """Resolve a stage/alias/id to a JSON Schema path."""
    reg = _jsonschema_registry()
    key = stage.strip()
    if key in reg:
        return reg[key]
    if key in STAGE_ALIASES and STAGE_ALIASES[key] in reg:
        return reg[STAGE_ALIASES[key]]
    low = key.lower()
    if low in STAGE_ALIASES and STAGE_ALIASES[low] in reg:
        return reg[STAGE_ALIASES[low]]
    # last resort: case-insensitive id match
    for cid, path in reg.items():
        if cid.lower() == low:
            return path
    return None


def _avro_resolve(stage: str) -> Optional[Path]:
    """Resolve a stage/alias/id to an Avro (.avsc) schema path, if one exists."""
    reg = _avro_registry()
    key = stage.strip()
    if key in reg:
        return reg[key]
    low = key.lower()
    for table in (AVRO_ALIASES, STAGE_ALIASES):
        if low in table and table[low] in reg:
            return reg[table[low]]
    for cid, path in reg.items():
        if cid.lower() == low:
            return path
    return None


def _avro_allowed_json_types(avro_type: Any) -> set:
    """Map an Avro field type (primitive, union list, or complex dict) to the
    set of acceptable JSON types for a structural check."""
    if isinstance(avro_type, str):
        return set(_AVRO_TO_JSON.get(avro_type, {avro_type}))
    if isinstance(avro_type, list):  # union
        out: set = set()
        for t in avro_type:
            out |= _avro_allowed_json_types(t)
        return out
    if isinstance(avro_type, dict):
        return set(_AVRO_TO_JSON.get(avro_type.get("type", ""), {"object"}))
    return set()


def _avro_structural_check(schema: dict, sample: dict) -> dict:
    """Field-level (not binary) check of a JSON sample against an Avro record:
    required fields present, no unknown fields, types broadly compatible."""
    fields = schema.get("fields", [])
    names = {f["name"] for f in fields}
    # Required = no default AND the type can't be null.
    required = [
        f["name"] for f in fields
        if "default" not in f and "null" not in _avro_allowed_json_types(f["type"])
    ]
    missing_required = [r for r in required if r not in sample]
    unexpected_fields = [k for k in sample if k not in names]
    type_mismatches = []
    for f in fields:
        if f["name"] in sample:
            allowed = _avro_allowed_json_types(f["type"])
            jt = _json_type(sample[f["name"]])
            if allowed and jt not in allowed:
                type_mismatches.append(
                    {"field": f["name"], "expected": sorted(allowed), "got": jt}
                )
    valid = not missing_required and not unexpected_fields and not type_mismatches
    return {
        "valid": valid,
        "missing_required": missing_required,
        "unexpected_fields": unexpected_fields,
        "type_mismatches": type_mismatches,
    }


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_ok(value: Any, schema_type: Union[str, list, None]) -> bool:
    if schema_type is None:
        return True
    allowed = [schema_type] if isinstance(schema_type, str) else list(schema_type)
    jt = _json_type(value)
    if jt in allowed:
        return True
    # JSON Schema: an integer satisfies "number".
    if jt == "integer" and "number" in allowed:
        return True
    return False


def _load_sample(sample: Any) -> tuple[Optional[Any], Optional[str]]:
    """Return (parsed_sample, error). Accepts dict/list, a file path, a named
    example, or a JSON string."""
    if isinstance(sample, (dict, list)):
        return sample, None
    if not isinstance(sample, str):
        return None, f"unsupported sample type {type(sample).__name__}"

    s = sample.strip()
    # explicit path
    p = Path(s)
    if p.is_file():
        try:
            return json.loads(p.read_text()), None
        except Exception as exc:
            return None, f"failed to parse {p}: {exc}"
    # named example anywhere under contracts/examples/
    if EXAMPLES_DIR.exists():
        for cand in EXAMPLES_DIR.rglob("*.json"):
            if cand.stem == s or cand.name == s:
                try:
                    return json.loads(cand.read_text()), None
                except Exception as exc:
                    return None, f"failed to parse {cand}: {exc}"
    # inline JSON
    try:
        return json.loads(s), None
    except Exception:
        return None, (
            "sample is not an object, an existing file path, a known example "
            "name, or valid JSON"
        )


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool
def list_contracts() -> dict:
    """List the available data contracts so you can pick a stage without
    grepping. Returns JSON Schema contracts (validatable) and Avro contracts
    (listed), plus the stage aliases that resolve to each."""
    js = _jsonschema_registry()
    alias_by_id: dict[str, list[str]] = {}
    for alias, cid in STAGE_ALIASES.items():
        alias_by_id.setdefault(cid, []).append(alias)

    contracts = []
    for cid, path in sorted(js.items()):
        try:
            schema = json.loads(path.read_text())
            title = schema.get("title", cid)
        except Exception:
            title = cid
        contracts.append({
            "id": cid,
            "title": title,
            "kind": "jsonschema",
            "stage_aliases": sorted(alias_by_id.get(cid, [])),
            "path": str(path.relative_to(REPO_ROOT)),
        })
    avro = [
        {"id": cid, "kind": "avro", "path": str(path.relative_to(REPO_ROOT))}
        for cid, path in sorted(_avro_registry().items())
    ]
    return {
        "jsonschema_contracts": contracts,
        "avro_contracts": avro,
        "hint": "validate(stage, sample) and get_contract(stage) accept an id or a stage alias",
    }


@mcp.tool
def get_contract(stage: str) -> dict:
    """Return the contract a stage's output must satisfy: a compact field
    summary (required vs optional, types, additionalProperties) plus the raw
    JSON Schema.

    Args:
        stage: a stage alias (e.g. "ingest", "connector", "ask") or a contract
            id (e.g. "article-ingest-v1"). Call list_contracts() to discover.
    """
    path = _resolve(stage)
    if path is None:
        return {
            "error": f"no JSON Schema contract for stage {stage!r}",
            "hint": "call list_contracts() for valid ids and stage aliases",
        }
    try:
        schema = json.loads(path.read_text())
    except Exception as exc:
        return {"error": f"failed to load {path}: {exc}"}

    props = schema.get("properties", {})
    required = schema.get("required", [])
    fields = {
        name: {
            "type": spec.get("type"),
            "required": name in required,
            **({"format": spec["format"]} if "format" in spec else {}),
        }
        for name, spec in props.items()
    }
    av = _avro_resolve(stage)
    return {
        "id": path.stem,
        "title": schema.get("title", path.stem),
        "description": schema.get("description"),
        "path": str(path.relative_to(REPO_ROOT)),
        "required": required,
        "additionalProperties": schema.get("additionalProperties", True),
        "fields": fields,
        "avro_counterpart": str(av.relative_to(REPO_ROOT)) if av else None,
        "schema": schema,
    }


@mcp.tool
def validate(stage: str, sample: Any) -> dict:
    """Validate a stage-output sample against its contract and return a compact
    pass/fail + drift report. This is the governed boundary check: "does this
    output still satisfy the contract the next stage expects?"

    Args:
        stage: stage alias or contract id (see list_contracts / get_contract).
        sample: the output to check — an inline object, a path to a JSON file,
            or a named example under contracts/examples/ (e.g.
            "valid-full-article", "invalid-missing-url").

    Returns per-schema verdicts. A stage's output is checked against its JSON
    Schema (authoritative jsonschema validation + field drift) and, when the
    stage's pipeline contract is Avro (e.g. article-ingest, sentiment), a
    structural Avro check too. `agree` flags when the two disagree — which is
    itself a contract bug (e.g. timestamp type drift between Avro and JSON
    Schema).

    Args:
        stage: stage alias or contract id (see list_contracts / get_contract).
        sample: the output to check — an inline object, a path to a JSON file,
            or a named example under contracts/examples/ (e.g.
            "valid-full-article", "invalid-missing-url").
    """
    js_path = _resolve(stage)
    av_path = _avro_resolve(stage)
    if js_path is None and av_path is None:
        return {
            "error": f"no contract (JSON Schema or Avro) for stage {stage!r}",
            "hint": "call list_contracts() for valid ids and stage aliases",
        }

    parsed, err = _load_sample(sample)
    if err:
        return {"error": err}

    verdicts: dict[str, Any] = {}

    if js_path is not None:
        try:
            schema = json.loads(js_path.read_text())
            import jsonschema  # lazy

            errors = []
            for e in sorted(jsonschema.Draft7Validator(schema).iter_errors(parsed),
                            key=lambda e: list(e.path)):
                loc = "/".join(str(p) for p in e.path) or "(root)"
                errors.append({"path": loc, "message": e.message})

            drift: dict[str, Any] = {"missing_required": [], "unexpected_fields": [], "type_mismatches": []}
            if isinstance(parsed, dict):
                props = schema.get("properties", {})
                required = schema.get("required", [])
                drift["missing_required"] = [r for r in required if r not in parsed]
                drift["unexpected_fields"] = [k for k in parsed if k not in props]
                for k, v in parsed.items():
                    spec = props.get(k)
                    if spec and "type" in spec and not _type_ok(v, spec["type"]):
                        drift["type_mismatches"].append(
                            {"field": k, "expected": spec["type"], "got": _json_type(v)}
                        )
            verdicts["jsonschema"] = {
                "contract": js_path.stem,
                "path": str(js_path.relative_to(REPO_ROOT)),
                "valid": len(errors) == 0,
                "error_count": len(errors),
                "errors": errors[:MAX_ERRORS],
                "drift": drift,
            }
        except Exception as exc:
            verdicts["jsonschema"] = {"contract": js_path.stem, "error": str(exc)}

    if av_path is not None:
        try:
            avro_schema = json.loads(av_path.read_text())
            if isinstance(parsed, dict):
                check = _avro_structural_check(avro_schema, parsed)
            else:
                check = {"valid": False, "note": "avro check needs an object sample"}
            verdicts["avro"] = {
                "contract": av_path.stem,
                "path": str(av_path.relative_to(REPO_ROOT)),
                "structural_only": True,
                **check,
            }
        except Exception as exc:
            verdicts["avro"] = {"contract": av_path.stem, "error": str(exc)}

    present = [v.get("valid") for v in verdicts.values() if "valid" in v]
    overall_valid = bool(present) and all(present)
    agree = len(set(present)) <= 1

    return {
        "stage": stage,
        "valid": overall_valid,
        "agree": agree,
        "checked": list(verdicts.keys()),
        "verdicts": verdicts,
    }


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

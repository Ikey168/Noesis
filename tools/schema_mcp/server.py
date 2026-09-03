"""
Noesis schema server — MCP server.

Token-efficient shortcuts to current backend structure.

Tools:
  list_tables()         -> table names + column summary from the DuckDB schema
  get_schema(table)     -> CREATE TABLE DDL + structured column list
  list_routes()         -> verb + path + source file for every API endpoint
  get_route(path)       -> parameters + query params for one endpoint

Design: regex parsing over the live source files — always reflects the
current state of the repo without any import side-effects.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_FILE = REPO_ROOT / "src" / "database" / "local_warehouse_seed.py"
ROUTES_DIR = REPO_ROOT / "src" / "api" / "routes"

mcp = FastMCP("noesis-schema")

# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

def _extract_schema_string() -> str:
    """Pull the _SCHEMA triple-quoted string out of local_warehouse_seed.py."""
    src = SEED_FILE.read_text()
    m = re.search(r'_SCHEMA\s*=\s*"""(.*?)"""', src, re.DOTALL)
    if not m:
        m = re.search(r"_SCHEMA\s*=\s*'''(.*?)'''", src, re.DOTALL)
    return m.group(1) if m else ""


def _parse_tables(ddl: str) -> dict[str, dict]:
    """Parse CREATE TABLE blocks → {table_name: {ddl, columns}}."""
    tables: dict[str, dict] = {}
    for block_m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;",
        ddl,
        re.DOTALL | re.IGNORECASE,
    ):
        name = block_m.group(1)
        body = block_m.group(2).strip()
        columns = []
        pk_inline: list[str] = []

        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            # Table-level PRIMARY KEY constraint
            pk_m = re.match(r"PRIMARY KEY\s*\(([^)]+)\)", line, re.IGNORECASE)
            if pk_m:
                pk_inline = [c.strip() for c in pk_m.group(1).split(",")]
                continue
            # Column definition: name TYPE [constraints…]
            col_m = re.match(r"(\w+)\s+(\w+)(.*)", line)
            if col_m:
                col_name, col_type, rest = col_m.groups()
                rest = rest.strip()
                is_pk = bool(re.search(r"\bPRIMARY KEY\b", rest, re.IGNORECASE))
                not_null = bool(re.search(r"\bNOT NULL\b", rest, re.IGNORECASE))
                unique = bool(re.search(r"\bUNIQUE\b", rest, re.IGNORECASE))
                columns.append({
                    "name": col_name,
                    "type": col_type,
                    "primary_key": is_pk,
                    "not_null": not_null or is_pk,
                    "unique": unique,
                })

        # Apply table-level PK markers
        if pk_inline:
            for col in columns:
                if col["name"] in pk_inline:
                    col["primary_key"] = True
                    col["not_null"] = True

        full_ddl = f"CREATE TABLE IF NOT EXISTS {name} (\n{body}\n);"
        tables[name] = {"ddl": full_ddl, "columns": columns}
    return tables


# ---------------------------------------------------------------------------
# Route parsing
# ---------------------------------------------------------------------------

_VERB_RE = re.compile(
    r'@router\.(get|post|put|patch|delete|head|options)\(\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(
    r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_FUNC_NAME_RE = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)


def _top_level_split(s: str) -> list[str]:
    """Split `s` on commas that are not inside brackets/parens."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _extract_param_names(raw_sig: str) -> list[str]:
    """Return bare parameter names from a function signature string."""
    names = []
    for part in _top_level_split(raw_sig):
        # param name is always the first identifier before : or =
        m = re.match(r"\*{0,2}(\w+)", part.strip())
        if m and m.group(1) not in ("self", "cls"):
            names.append(m.group(1))
    return names


def _parse_route_file(path: Path) -> dict:
    """Extract prefix and all routes from one route file."""
    src = path.read_text()
    pm = _PREFIX_RE.search(src)
    prefix = pm.group(1) if pm else ""

    routes = []
    lines = src.splitlines()
    for i, line in enumerate(lines):
        vm = _VERB_RE.search(line)
        if not vm:
            continue
        verb, route_path = vm.group(1).upper(), vm.group(2)
        full_path = prefix + route_path

        # Find the function definition that follows (may be several lines later
        # due to response_model= etc. on the decorator).
        func_name = None
        params: list[str] = []
        for j in range(i + 1, min(i + 15, len(lines))):
            stripped = lines[j].strip()
            fm = _FUNC_NAME_RE.match(stripped)
            if fm:
                func_name = fm.group(1)
                # Collect lines until the outer paren is balanced
                sig_lines = [stripped]
                depth = stripped.count("(") - stripped.count(")")
                k = j + 1
                while depth > 0 and k < len(lines):
                    sig_lines.append(lines[k].strip())
                    depth += lines[k].count("(") - lines[k].count(")")
                    k += 1
                sig = " ".join(sig_lines)
                # Extract the content between the outer parens
                inner_m = re.search(r"def\s+\w+\s*\((.+)\)\s*(?:->|:)", sig, re.DOTALL)
                if inner_m:
                    params = _extract_param_names(inner_m.group(1))
                break

        routes.append({
            "verb": verb,
            "path": full_path,
            "handler": func_name,
            "params": params,
        })
    return {"prefix": prefix, "routes": routes, "file": path.name}


def _all_routes() -> list[dict]:
    """Collect routes from every *_routes.py file."""
    result = []
    for f in sorted(ROUTES_DIR.glob("*_routes.py")):
        parsed = _parse_route_file(f)
        for r in parsed["routes"]:
            r["file"] = parsed["file"]
        result.extend(parsed["routes"])
    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
def list_tables() -> dict:
    """List every DuckDB table in the local warehouse with a column summary.

    Avoids reading local_warehouse_seed.py manually. Use get_schema(table)
    for the full DDL of a specific table.
    """
    ddl = _extract_schema_string()
    if not ddl:
        return {"error": f"Could not find _SCHEMA in {SEED_FILE}"}
    tables = _parse_tables(ddl)
    summary = [
        {
            "table": name,
            "columns": len(info["columns"]),
            "column_names": [c["name"] for c in info["columns"]],
        }
        for name, info in sorted(tables.items())
    ]
    return {
        "tables": summary,
        "count": len(summary),
        "hint": "call get_schema(table) for CREATE TABLE DDL + column types",
    }


@mcp.tool
def get_schema(table: str) -> dict:
    """Return the CREATE TABLE DDL and a structured column list for one table.

    Args:
        table: exact table name (e.g. "news_articles", "document_frames").
               Call list_tables() to discover available names.
    """
    ddl = _extract_schema_string()
    if not ddl:
        return {"error": f"Could not find _SCHEMA in {SEED_FILE}"}
    tables = _parse_tables(ddl)
    key = table.strip().lower()
    match = next((n for n in tables if n.lower() == key), None)
    if match is None:
        return {
            "error": f"No table named {table!r}",
            "available": sorted(tables.keys()),
        }
    info = tables[match]
    return {
        "table": match,
        "ddl": info["ddl"],
        "columns": info["columns"],
        "primary_keys": [c["name"] for c in info["columns"] if c["primary_key"]],
        "source": str(SEED_FILE.relative_to(REPO_ROOT)),
    }


@mcp.tool
def list_routes(
    verb: str = "",
    prefix: str = "",
) -> dict:
    """List every API endpoint across all route files.

    Returns verb, full path, handler name, and source file for each route.
    Use get_route(path) to see parameter details for one endpoint.

    Args:
        verb:   optional filter — "GET", "POST", etc.
        prefix: optional path prefix filter — e.g. "/api/v1/arguments"
    """
    if not ROUTES_DIR.exists():
        return {"error": f"Routes directory not found: {ROUTES_DIR}"}
    routes = _all_routes()
    if verb:
        routes = [r for r in routes if r["verb"] == verb.upper()]
    if prefix:
        routes = [r for r in routes if r["path"].startswith(prefix)]
    return {
        "routes": routes,
        "count": len(routes),
        "hint": "call get_route(path) for query-param and body-param details",
    }


@mcp.tool
def get_route(path: str) -> dict[str, Any]:
    """Return the handler signature and parameter list for one API endpoint.

    Useful before adding a new query parameter — confirms what already exists
    without reading the full route file.

    Args:
        path: the URL path, e.g. "/api/v1/arguments/frames".
              Partial suffix match works (e.g. "arguments/frames").
    """
    if not ROUTES_DIR.exists():
        return {"error": f"Routes directory not found: {ROUTES_DIR}"}
    routes = _all_routes()
    needle = path.strip().lower()
    matches = [r for r in routes if needle in r["path"].lower()]
    if not matches:
        return {
            "error": f"No route matching {path!r}",
            "hint": "call list_routes() to see all paths",
        }
    # For each match, grab the full function source for context
    results = []
    for r in matches:
        route_file = ROUTES_DIR / r["file"]
        src = route_file.read_text()
        # Find the handler function body (up to next def/decorator)
        fn = r["handler"]
        if fn:
            fn_m = re.search(
                rf"(?:async\s+)?def\s+{re.escape(fn)}\s*\([^)]*\)[^:]*:(.*?)(?=\n(?:async\s+)?def\s|\n@|\Z)",
                src,
                re.DOTALL,
            )
            body_preview = fn_m.group(1)[:400].strip() if fn_m else "(not found)"
        else:
            body_preview = "(handler not resolved)"
        results.append({**r, "body_preview": body_preview})
    return {"matches": results, "count": len(results)}


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http

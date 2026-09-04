from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains import pack_install
from src.domains.pack_format import load_manifest, validate_manifest
from src.domains.technical.advisories import CVEAdapter, OSVAdapter, ingest_advisory
from src.domains.technical.git_connector import GitRepositoryConnector
from src.domains.technical.model import (
    TechnicalModelError,
    canonical_package_coordinate,
    immutable_artifact_id,
    package_object_id,
    record_alias,
    record_object,
    record_relation,
    resolve_package,
    version_in_events,
)
from src.domains.technical.queries import TechnicalQueryError, technical_research
from src.domains.technical.registries import (
    LIVE_ENV,
    CratesIOProvider,
    GoModuleProvider,
    MavenCentralProvider,
    NpmProvider,
    PackageRegistryConnector,
    PyPIProvider,
    RateLimiter,
    RegistryError,
    ingest_package,
)
from src.domains.technical.specifications import (
    SpecificationConnector,
    ingest_specification,
)
from src.ingestion.connectors.registry import is_registered
from src.kb import contract

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/technical"


class TechnicalBacking:
    backing_type = "corpus-view"

    def __init__(self, conn):
        self.conn = conn
        self.definition = SimpleNamespace(name="technology", tags=["technology"])


@pytest.fixture()
def corpus():
    conn = duckdb.connect(":memory:")
    yield conn, TechnicalBacking(conn)
    conn.close()


def test_pack_and_schemas_are_distributable():
    manifest = load_manifest(str(ROOT / "packs/technology"))
    assert validate_manifest(manifest.to_dict()) == []
    assert {
        "package", "version", "dependency", "advisory", "specification", "implementation"
    } <= set(manifest.ontology_extensions["object_types"])
    assert {"depends_on", "affected_by", "fixed_in", "implements"} <= set(
        manifest.ontology_extensions["relation_types"]
    )
    receipt = pack_install.install_manifest(manifest)
    try:
        assert "dependency-compatibility-graph" in receipt["capabilities"]
    finally:
        assert pack_install.uninstall("technology") is True
    assert is_registered("git-repository")
    assert is_registered("package-registry")
    assert is_registered("technical-specification")


def test_coordinate_identity_aliases_and_immutable_relations(corpus):
    conn, _ = corpus
    coordinates = {
        canonical_package_coordinate("PyPI", "Foo_Bar"): "pkg:pypi:foo-bar",
        canonical_package_coordinate("npm", "@Scope/Name"): "pkg:npm:@scope/name",
        canonical_package_coordinate("Maven", "org.Example:demo"): "pkg:maven:org.Example:demo",
        canonical_package_coordinate("crates.io", "Serde_JSON"): "pkg:cargo:serde_json",
        canonical_package_coordinate("Go", "github.com/Acme/Mod"): "pkg:golang:github.com/Acme/Mod",
    }
    assert all(actual == expected for actual, expected in coordinates.items())
    coordinate = "pkg:pypi:foo-bar"
    package_id = package_object_id(coordinate)
    record_object(
        conn, object_type="package", object_id=package_id, coordinate=coordinate,
        canonical_name="Foo_Bar", observed_at=1000,
        source_url="https://secret@example.test/repo?view=1#token",
    )
    record_alias(conn, "old-name", package_id, alias_kind="renamed", observed_at=1000)
    assert resolve_package(conn, "old-name")["coordinate"] == coordinate
    assert immutable_artifact_id(coordinate, "1.0.0", "sha256:abc").endswith(
        "@1.0.0?checksum=sha256:abc"
    )
    assert version_in_events(
        "1.1.0-rc1", [{"introduced": "1.0.0"}, {"fixed": "1.1.0"}]
    )
    record_relation(
        conn, package_id, "fork_of", "repository:upstream", observed_at=1000,
        source_document_id="doc:fork",
    )
    record_relation(
        conn, package_id, "vendored_from", "package:upstream", observed_at=1000,
        source_document_id="doc:vendor",
    )
    assert conn.execute(
        "SELECT source_url FROM technical_objects WHERE object_id=?", [package_id]
    ).fetchone()[0] == "https://example.test/repo?view=1"
    with pytest.raises(TechnicalModelError, match="cannot change immutable identity"):
        record_object(
            conn, object_type="repository", object_id=package_id,
            canonical_name="collision", observed_at=2000,
        )


@pytest.mark.parametrize(
    ("provider", "fixture", "coordinate"),
    [
        (PyPIProvider(), "pypi.json", "pkg:pypi:demo-package"),
        (NpmProvider(), "npm.json", "pkg:npm:@scope/demo"),
        (MavenCentralProvider(), "maven.json", "pkg:maven:org.example:demo"),
        (CratesIOProvider(), "crates.json", "pkg:cargo:demo-crate"),
        (GoModuleProvider(), "go.json", "pkg:golang:example.test/acme/demo"),
    ],
)
def test_registry_cached_fixtures_normalize_metadata(provider, fixture, coordinate):
    record = provider.load_fixture(FIXTURES / fixture)
    assert record.coordinate == coordinate
    assert record.versions
    assert record.source_url.startswith("file:")
    assert record.metadata["original_name"] == record.name
    assert any(version.checksum for version in record.versions)


def test_registry_ingest_dependencies_yanks_and_live_opt_in(corpus, monkeypatch):
    conn, _ = corpus
    npm = NpmProvider().load_fixture(FIXTURES / "npm.json")
    result = ingest_package(
        conn, npm, observed_at=1000, source_document_id="registry:npm"
    )
    assert result["package"]["metadata"]["maintainers"] == ["Grace"]
    edges = conn.execute(
        "SELECT relation, constraint_text, optional FROM technical_relations "
        "WHERE subject_id LIKE 'version:%' ORDER BY relation"
    ).fetchall()
    assert edges == [
        ("depends_on", "^1.3.0", False),
        ("optional_dependency", "^2.0.0", True),
    ]
    pypi = PyPIProvider().load_fixture(FIXTURES / "pypi.json")
    ingest_package(conn, pypi, observed_at=1000)
    assert conn.execute(
        "SELECT status FROM technical_objects WHERE version='1.1.0'"
    ).fetchone()[0] == "yanked"
    monkeypatch.delenv(LIVE_ENV, raising=False)
    with pytest.raises(RegistryError, match="requires"):
        PyPIProvider().fetch_live("demo")


def test_registry_common_connector_uses_cached_fixture():
    documents = list(
        PackageRegistryConnector().harvest(
            {
                "ecosystem": "pypi",
                "package": "Demo_Package",
                "fixture": str(FIXTURES / "pypi.json"),
            }
        )
    )
    assert [item.metadata["version"] for item in documents] == ["1.0.0", "1.1.0"]
    assert documents[0].metadata["coordinate"] == "pkg:pypi:demo-package"


def test_registry_rate_limiter_spaces_requests():
    now = [10.0]
    delays = []

    def sleep(delay):
        delays.append(delay)
        now[0] += delay

    limiter = RateLimiter(0.25, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    limiter.wait()
    assert delays == [0.25]


@pytest.mark.skipif(
    os.getenv(LIVE_ENV) != "1",
    reason=f"live registry checks require {LIVE_ENV}=1",
)
def test_rate_limited_opt_in_live_registry():
    record = PyPIProvider(limiter=RateLimiter(0.25)).fetch_live("packaging")
    assert record.coordinate == "pkg:pypi:packaging"
    assert record.versions


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_connector_incremental_edge_cases_and_secret_hygiene(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test Author")
    (repo / "README.md").write_text("# Demo\n")
    (repo / "docs").mkdir()
    (repo / "docs/large.md").write_bytes(b"x" * 128)
    (repo / "gone.txt").write_text("remove me")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    initial = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1.0.0", initial)
    (repo / "gone.txt").unlink()
    (repo / "package.json").write_text('{"name":"demo"}')
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{initial},vendor/sub")
    _git(repo, "commit", "-m", "manifest")
    (repo / ".git/shallow").touch()
    connector = GitRepositoryConnector(max_file_bytes=64)
    documents = list(
        connector.harvest({"repository": str(repo), "previous_head": initial})
    )
    snapshot = documents[0].metadata
    assert snapshot["force_push_detected"] is False
    assert snapshot["shallow"] is True
    assert snapshot["deleted_paths"] == ["gone.txt"]
    assert snapshot["submodules"] == ["vendor/sub"]
    assert any(item["name"] == "refs/tags/v1.0.0" for item in snapshot["refs"])
    assert snapshot["omitted"] == [
        {"path": "docs/large.md", "size": 128, "reason": "large_file"}
    ]
    assert {item.title for item in documents[1:]} == {"README.md", "package.json"}
    assert documents[1].metadata["path"]
    assert documents[1].metadata["revision"] == _git(repo, "rev-parse", "HEAD")
    assert documents[1].metadata["author"] == "Test Author"
    assert documents[1].metadata["timestamp"]
    assert connector.cursors()[documents[0].source_id] == documents[0].metadata["revision"]
    resumed_ref = next(connector.discover({"repository": str(repo)}))
    assert resumed_ref.metadata["previous_head"] == documents[0].metadata["revision"]
    # The locator and persisted metadata never contain a credential.
    monkeypatch.setenv("NOESIS_TEST_GIT_TOKEN", "super-secret")
    ref = next(
        connector.discover(
            {
                "repository": "https://user:password@example.test/private.git#fragment",
                "auth_env": "NOESIS_TEST_GIT_TOKEN",
            }
        )
    )
    assert "password" not in ref.locator and "super-secret" not in json.dumps(ref.metadata)
    assert ref.metadata["auth_env"] == "NOESIS_TEST_GIT_TOKEN"
    _git(repo, "checkout", "--orphan", "rewritten")
    _git(repo, "rm", "-rf", ".")
    (repo / "README.md").write_text("# Rewritten\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "rewrite")
    rewritten = list(
        connector.harvest({"repository": str(repo), "previous_head": initial})
    )
    assert rewritten[0].metadata["force_push_detected"] is True


@pytest.mark.parametrize("status", ["draft", "final", "obsolete", "amended"])
def test_specification_sections_statuses_and_supersession(corpus, status):
    conn, backing = corpus
    payload = json.loads((FIXTURES / "specification.json").read_text())
    payload["status"] = status
    connector = SpecificationConnector()
    raw = SimpleNamespace(
        ref=SimpleNamespace(
            locator="https://standards.example.test/rfc-demo-2",
            title=None,
            metadata={"source_id": "RFC-DEMO"},
        ),
        content=json.dumps(payload),
        content_type="application/json",
        fetched_at=2000,
    )
    documents = connector.parse(raw)
    assert [item.metadata["locator"]["section"] for item in documents] == ["1", "4.2"]
    assert documents[1].url.endswith("#wire-format")
    assert documents[1].metadata["normative"] is True
    stored = ingest_specification(conn, documents)
    assert stored["specification"]["status"] == status
    assert conn.execute(
        "SELECT relation FROM technical_relations WHERE relation='supersedes'"
    ).fetchone()[0] == "supersedes"
    supersession = technical_research(
        backing,
        query_type="supersedes",
        coordinate="specification:RFC-DEMO:2",
    )
    assert (
        supersession["results"][0]["object"]["object_id"]
        == "specification:RFC-DEMO:1"
    )


def test_osv_cve_adapters_exact_resolution_ranges_and_corrections(corpus):
    conn, _ = corpus
    package = PyPIProvider().load_fixture(FIXTURES / "pypi.json")
    ingest_package(conn, package, observed_at=1000, source_document_id="registry:pypi")
    osv_payload = json.loads((FIXTURES / "osv.json").read_text())
    osv = OSVAdapter().parse(osv_payload)
    receipt = ingest_advisory(
        conn, osv, source_url="https://osv.test/1",
        source_document_id="osv:1", observed_at=2000,
    )
    assert receipt["linked_packages"] == ["pkg:pypi:demo-package"]
    advisory = receipt["advisory"]
    assert advisory["metadata"]["severity_conflict"] is True
    cve_payload = json.loads((FIXTURES / "cve.json").read_text())
    ambiguous = CVEAdapter().parse(cve_payload)
    unresolved = ingest_advisory(
        conn, ambiguous, source_url="https://cve.test/2", observed_at=2000
    )
    assert "no exact package mapping" in unresolved["unresolved_packages"][0]["error"]
    mapped = CVEAdapter(
        {"Acme/Demo": "pkg:pypi:demo-package"}
    ).parse(cve_payload)
    mapped_receipt = ingest_advisory(
        conn, mapped, source_url="https://cve.test/2",
        source_document_id="cve:2", observed_at=3000,
    )
    assert mapped_receipt["linked_packages"] == ["pkg:pypi:demo-package"]
    assert mapped_receipt["advisory"]["metadata"]["severity_conflict"] is True
    osv_payload["modified"] = "2025-03-10T00:00:00Z"
    osv_payload["withdrawn"] = "2025-03-11T00:00:00Z"
    corrected = OSVAdapter().parse(osv_payload)
    ingest_advisory(
        conn, corrected, source_url="https://osv.test/1",
        source_document_id="osv:1-correction", observed_at=4000,
    )
    assert conn.execute(
        "SELECT status FROM technical_objects WHERE object_id='advisory:OSV-DEMO-1'"
    ).fetchone()[0] == "withdrawn"
    assert conn.execute(
        "SELECT COUNT(*) FROM kb_temporal_assertions "
        "WHERE assertion_id='advisory:OSV-DEMO-1'"
    ).fetchone()[0] == 2


def test_cycle_safe_dependency_advisory_and_compatibility_queries(corpus):
    conn, backing = corpus
    package = PyPIProvider().load_fixture(FIXTURES / "pypi.json")
    ingest_package(conn, package, observed_at=1000, source_document_id="registry:pypi")
    dep_a = package_object_id("pkg:pypi:dep-a")
    dep_b = package_object_id("pkg:pypi:dep-b")
    for oid, coordinate in ((dep_a, "pkg:pypi:dep-a"), (dep_b, "pkg:pypi:dep-b")):
        record_object(
            conn, object_type="package", object_id=oid, coordinate=coordinate,
            canonical_name=coordinate.rsplit(":", 1)[-1], observed_at=1000,
        )
    root_version = conn.execute(
        "SELECT object_id FROM technical_objects WHERE coordinate=? AND version='1.0.0'",
        [package.coordinate],
    ).fetchone()[0]
    record_relation(
        conn, root_version, "depends_on", dep_a, constraint=">=1",
        observed_at=1000, source_url="https://lock.test/a",
        metadata={"lockfile_conflict": True},
    )
    record_relation(
        conn, dep_a, "optional_dependency", dep_b, constraint="~2",
        optional=True, observed_at=1000, source_document_id="lock:optional",
    )
    record_relation(
        conn, dep_b, "depends_on", dep_a, constraint="*", observed_at=1000,
        source_document_id="lock:cycle",
    )
    root = package_object_id(package.coordinate)
    record_relation(
        conn, root, "implements", "specification:RFC-DEMO:2",
        observed_at=1000, source_document_id="spec-map",
    )
    record_relation(
        conn, root_version, "breaking_change", "version:previous",
        observed_at=1000, source_document_id="release-notes",
    )
    required = technical_research(
        backing, query_type="dependency_paths", coordinate=package.coordinate,
        version="1.0.0", include_optional=False,
    )
    assert len(required["results"]) == 1
    optional = technical_research(
        backing, query_type="dependency_paths", coordinate=package.coordinate,
        version="1.0.0", include_optional=True,
    )
    assert optional["cycles"]
    assert optional["coverage"]["conflicting_lockfiles"] is True
    implements = technical_research(
        backing, query_type="implements", coordinate=package.coordinate
    )
    assert (
        implements["results"][0]["object"]["object_id"]
        == "specification:RFC-DEMO:2"
    )
    breaking = technical_research(
        backing, query_type="breaking_changes", coordinate=package.coordinate,
        version="1.0.0",
    )
    assert breaking["results"][0]["object"]["object_id"] == "version:previous"
    osv = OSVAdapter().parse(json.loads((FIXTURES / "osv.json").read_text()))
    ingest_advisory(
        conn, osv, source_url="https://osv.test/1",
        source_document_id="osv:1", observed_at=2000,
    )
    affected = technical_research(
        backing, query_type="affected_by", coordinate=package.coordinate,
        version="1.0.0", observed_before=2500,
    )
    assert affected["results"][0]["ranges"][0]["affected"] is True
    fixed = technical_research(
        backing, query_type="affected_by", coordinate=package.coordinate,
        version="1.1.0", observed_before=2500,
    )
    assert fixed["results"] == []
    fixed_in = technical_research(
        backing, query_type="fixed_in", coordinate=package.coordinate,
        target_id="advisory:OSV-DEMO-1", observed_before=2500,
    )
    assert fixed_in["results"][0]["fixed_version"]["version"] == "1.1.0"
    with pytest.raises(TechnicalQueryError, match="exact coordinate"):
        technical_research(
            backing, query_type="affected_by", coordinate="demo-package"
        )
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-technical-query-v1.json").read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(affected))


def test_technical_object_schema(corpus):
    conn, _ = corpus
    payload = record_object(
        conn, object_type="implementation", canonical_name="Demo implementation",
        immutable_id="git:abc", observed_at=1000,
        source_document_id="git:abc:README",
    )
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-technical-object-v1.json").read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(payload))


def test_kb_contract_resolves_technical_domain(corpus, tmp_path):
    conn, _ = corpus
    package = PyPIProvider().load_fixture(FIXTURES / "pypi.json")
    ingest_package(conn, package, observed_at=1000)
    config_path = tmp_path / "domains.yml"
    config_path.write_text(
        """version: 1
domains:
  - name: technology
    backing: corpus-view
    embedding_model: test
    tags: [technology]
    keywords: [package, dependency]
"""
    )
    response = contract.kb_technical(
        "technology",
        "affected_by",
        package.coordinate,
        version="1.0.0",
        conn=conn,
        config_path=config_path,
    )
    assert response["contract"] == "noesis-kb-v1"
    assert response["domain"] == "technology"
    assert response["data"]["query"]["resolved_coordinate"] == package.coordinate


def test_rest_and_mcp_technical_surfaces_share_contract(monkeypatch):
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "technology",
        "as_of_ms": 1,
        "data": {},
    }
    calls = []

    def fake_technical(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_technical", fake_technical)
    from src.api.routes import kb_routes

    request = kb_routes.TechnicalQueryRequest(
        domain="technology", query_type="depends_on", coordinate="pkg:pypi:demo"
    )
    rest = kb_routes.technical_query(request)
    spec = importlib.util.spec_from_file_location(
        "technical_kb_mcp", ROOT / "tools/kb_mcp/server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_technical"].fn(
        domain="technology",
        query_type="depends_on",
        coordinate="pkg:pypi:demo",
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2

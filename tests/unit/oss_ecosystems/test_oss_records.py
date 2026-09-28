"""OSS Ecosystems records, store revisions, versions and SPDX normalisation (OS02, OS06)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.domains.technical.model import immutable_artifact_id, package_object_id
from src.kb import oss_ecosystem_records as rec
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError
from src.kb.oss_ecosystem_versions import compare, satisfies
from src.kb.oss_spdx import (
    SpdxError,
    SpdxList,
    list_content,
    normalise_declaration,
    parse_expression,
)

NS = "global"
WRITE = {
    rec.WRITE_SCOPE,
    rec.READ_SCOPE,
    f"namespace:{NS}:write",
    f"namespace:{NS}:read",
}


def state(version, value, **extra):
    return {
        "record_type": "release_state_revision",
        "source": "pypi",
        "ecosystem": "pypi",
        "package": "Fixture_Parser",
        "version": version,
        "state": value,
        **extra,
    }


def spdx():
    return SpdxList.from_content(
        list_content(
            "3.25",
            "2024-08-19",
            [
                {"id": "MIT", "name": "MIT License"},
                {"id": "Apache-2.0", "name": "Apache License 2.0"},
                {"id": "BUSL-1.1", "name": "Business Source License 1.1"},
                {
                    "id": "GPL-2.0",
                    "name": "GNU General Public License v2.0 only",
                    "deprecated": True,
                },
                {
                    "id": "GPL-2.0-or-later",
                    "name": "GNU General Public License v2.0 or later",
                },
            ],
            [{"id": "Classpath-exception-2.0", "name": "Classpath exception 2.0"}],
        )
    )


def test_records_reference_technology_identity_and_normalise_names():
    value = rec.validate(state("1.0", "published"))
    assert value["coordinate"] == "pkg:pypi:fixture-parser"  # PEP 503
    assert value["package_object_id"] == package_object_id("pkg:pypi:fixture-parser")
    assert value["version"] == "1.0" and value["artifact_id"] == immutable_artifact_id(
        "pkg:pypi:fixture-parser", "1.0"
    )
    assert rec.validate(state("1.0.0", "published"))["version"] == "1.0.0"
    npm = rec.validate(
        {
            **state("1.0.0", "published"),
            "source": "npm",
            "ecosystem": "npm",
            "package": "fixture",
        }
    )
    assert (
        npm["coordinate"] != value["coordinate"]
    )  # the same name in two ecosystems is two packages


def test_no_record_has_a_field_for_an_individual():
    schema = json.loads(
        (rec.SCHEMA_ROOT / "noesis-oss-ecosystem-record-v1.json").read_text()
    )
    fields = set(schema["properties"])
    for definition in schema["definitions"].values():
        fields |= set(definition.get("properties") or {})
    assert not {f.casefold() for f in fields} & rec.FORBIDDEN_FIELDS
    with pytest.raises(rec.OssRecordError) as caught:
        rec.validate({**state("1.0", "published"), "maintainers": ["someone"]})
    assert caught.value.code == "personal_field"
    with pytest.raises(rec.OssRecordError):
        rec.validate(
            {
                "record_type": "publisher_organisation",
                "source": "npm",
                "ecosystem": "npm",
                "package": "x",
                "organisations": [
                    {"kind": "npm-scope", "id": "@x", "email": "a@b.example"}
                ],
            }
        )


def test_dates_are_iso_and_missing_values_absent():
    value = rec.validate(
        state("1.0", "yanked", published_at="2026-01-05T10:00:00", reason=None)
    )
    assert value["published_at"] == "2026-01-05T10:00:00Z" and "reason" not in value
    assert rec.iso_instant("20260105100000", "x") == "2026-01-05T10:00:00Z"
    with pytest.raises(rec.OssRecordError):
        rec.validate(state("1.0", "published", published_at="yesterday"))


def test_repository_keys_are_source_independent():
    keys = {
        rec.repository_key(u)
        for u in (
            "git+https://github.com/Fixture-Labs/Parser.git",
            "https://github.com/fixture-labs/parser",
            "git@github.com:fixture-labs/parser.git",
            "github:fixture-labs/parser",
            "scm:git:https://github.com/fixture-labs/parser.git",
            "https://user:secret@github.com/fixture-labs/parser/",
        )
    }
    assert keys == {"github.com/fixture-labs/parser"}


def test_revisions_append_dedupe_against_current_and_keep_late_data_as_history():
    conn = duckdb.connect()
    store = OssEcosystemStore(conn)
    assert (
        store.apply(
            NS,
            [state("1.0", "published")],
            run_id="r1",
            scopes=WRITE,
            observed_at_ms=1_000,
        )["revisions"]
        == 1
    )
    assert store.apply(
        NS, [state("1.0", "published")], run_id="r2", scopes=WRITE, observed_at_ms=2_000
    ) == {
        "revisions": 0,
        "unchanged": 1,
        "late": 0,
        "not_observed": 0,
        "normalisations": 0,
    }
    store.apply(
        NS,
        [state("1.0", "yanked", reason="broken wheel")],
        run_id="r3",
        scopes=WRITE,
        observed_at_ms=3_000,
    )
    # A reversion (un-yank) is a new correction, never a conflict.
    store.apply(
        NS, [state("1.0", "published")], run_id="r4", scopes=WRITE, observed_at_ms=4_000
    )
    record = store.records(NS, record_type="release_state_revision")[0]["record_id"]
    history = store.history(record)
    assert [r["statement"]["state"] for r in history] == [
        "published",
        "yanked",
        "published",
    ]
    assert history[1]["statement"]["reason"] == "broken wheel"
    # Late older data stays history and never becomes current; replaying it adds nothing.
    counts = store.apply(
        NS,
        [state("1.0", "yanked", reason="old")],
        run_id="late",
        scopes=WRITE,
        observed_at_ms=500,
    )
    assert (
        counts["late"] == 1
        and store.current(record)["statement"]["state"] == "published"
    )
    again = store.apply(
        NS,
        [state("1.0", "yanked", reason="old")],
        run_id="late2",
        scopes=WRITE,
        observed_at_ms=500,
    )
    assert again["unchanged"] == 1
    ids = [r["revision_id"] for r in store.history(record)]
    assert len(ids) == len(set(ids)) == 4
    assert store.current(record, acquired_by_ms=3_500)["statement"]["state"] == "yanked"
    assert store.current(record, at_ms=2_500)["statement"]["state"] == "published"


def test_listing_marks_absent_releases_not_observed_but_never_later_ones():
    conn = duckdb.connect()
    store = OssEcosystemStore(conn)
    listing = {
        "record_type": "release_listing",
        "source": "pypi",
        "ecosystem": "pypi",
        "package": "fixture-parser",
    }
    store.apply(
        NS,
        [
            state("1.0", "published"),
            state("1.1", "published"),
            {**listing, "versions": ["1.0", "1.1"]},
        ],
        run_id="r1",
        scopes=WRITE,
        observed_at_ms=1_000,
    )
    store.apply(
        NS,
        [
            state("1.1", "published"),
            state("2.0", "published"),
            {**listing, "versions": ["1.1", "2.0"]},
        ],
        run_id="r2",
        scopes=WRITE,
        observed_at_ms=2_000,
    )
    gone = store.records(NS, record_type="release_state_revision", version="1.0")[0][
        "record_id"
    ]
    assert store.current(gone)["statement"]["state"] == "not_observed"
    # A late listing from before 2.0 existed never marks 2.0.
    store.apply(
        NS,
        [{**listing, "versions": ["1.0", "1.1"]}],
        run_id="late",
        scopes=WRITE,
        observed_at_ms=1_500,
    )
    newer = store.records(NS, record_type="release_state_revision", version="2.0")[0][
        "record_id"
    ]
    assert [r["statement"]["state"] for r in store.history(newer)] == ["published"]


def test_reads_before_any_run_are_not_ready_and_writes_need_scopes():
    conn = duckdb.connect()
    with pytest.raises(OssStoreError) as caught:
        OssEcosystemStore(conn, initialize=False).require_ready(NS)
    assert caught.value.code == "not_ready"
    with pytest.raises(OssStoreError) as caught:
        OssEcosystemStore(conn).apply(
            NS, [state("1.0", "published")], run_id="r", scopes={rec.READ_SCOPE}
        )
    assert caught.value.code == "unauthorized"


def test_licence_normalisation_is_attached_per_pinned_list_and_never_guessed():
    conn = duckdb.connect()
    store = OssEcosystemStore(conn)
    licence = {
        "record_type": "licence_declaration_revision",
        "source": "pypi",
        "ecosystem": "pypi",
        "package": "fixture-parser",
        "version": "1.0",
        "raw": {"expression": "mit OR Apache-2.0"},
    }
    store.apply(NS, [licence], run_id="r1", scopes=WRITE, observed_at_ms=1_000)
    record = store.records(NS, record_type="licence_declaration_revision")[0][
        "record_id"
    ]
    revision = store.current(record)["revision_id"]
    assert store.normalisation(revision, None)["status"] == "no_spdx_list"
    content = list_content(
        "3.25",
        "2024-08-19",
        [
            {"id": "MIT", "name": "MIT License"},
            {"id": "Apache-2.0", "name": "Apache License 2.0"},
        ],
        [],
    )
    counts = store.apply(
        NS,
        [{"record_type": "spdx_list_release", "source": "spdx", **content}],
        run_id="spdx",
        scopes=WRITE,
        observed_at_ms=2_000,
    )
    assert counts["normalisations"] == 1 and store.spdx_versions(NS) == ["3.25"]
    result = store.normalisation(revision, "3.25")
    assert (
        result["expression"] == "MIT OR Apache-2.0"
        and result["comparison_key"] == "Apache-2.0 OR MIT"
    )
    assert result["spdx_list_version"] == "3.25"


def test_spdx_expressions_parse_with_precedence_refs_and_exceptions():
    listing = spdx()
    dual = parse_expression("(Apache-2.0 OR MIT)", listing)
    assert (
        dual["comparison_key"]
        == parse_expression("MIT OR Apache-2.0", listing)["comparison_key"]
    )
    nested = parse_expression("MIT AND Apache-2.0 OR BUSL-1.1", listing)
    assert nested["expression"] == "MIT AND Apache-2.0 OR BUSL-1.1"
    assert (
        parse_expression("BUSL-1.1 OR (MIT AND Apache-2.0)", listing)["comparison_key"]
        == nested["comparison_key"]
    )
    exception = parse_expression(
        "GPL-2.0-or-later WITH Classpath-exception-2.0", listing
    )
    assert exception["expression"] == "GPL-2.0-or-later WITH Classpath-exception-2.0"
    assert parse_expression("GPL-2.0+", listing)["deprecated_ids"] == ["GPL-2.0"]
    assert parse_expression("DocumentRef-spdx-tool:LicenseRef-Custom-1", listing)[
        "expression"
    ].endswith("Custom-1")
    for broken, code in (
        ("MIT OR", "unparseable"),
        ("(MIT", "unparseable"),
        ("Frobnicate-1.0", "unknown_identifier"),
        ("Classpath-exception-2.0", "exception_as_licence"),
        ("MIT WITH Frob", "unknown_identifier"),
    ):
        with pytest.raises(SpdxError) as caught:
            parse_expression(broken, listing)
        assert caught.value.code == code, broken
    custom = normalise_declaration(
        {"text": "SEE LICENSE IN LICENSE.txt"}, listing, ecosystem="npm"
    )
    assert (
        custom["status"] == "unparseable"
        and custom["receipt"]["inputs"]["text"] == "SEE LICENSE IN LICENSE.txt"
    )
    ambiguous = normalise_declaration(
        {"classifiers": ["License :: OSI Approved :: Apache Software License"]},
        listing,
        ecosystem="pypi",
    )
    assert ambiguous["status"] == "unparseable"
    two = normalise_declaration(
        {"names": ["MIT License", "Apache License 2.0"]}, listing, ecosystem="maven"
    )
    assert two["status"] == "unparseable" and "not stated" in two["reason"]
    cargo = normalise_declaration(
        {"expression": "MIT/Apache-2.0"}, listing, ecosystem="cargo"
    )
    assert cargo["comparison_key"] == "Apache-2.0 OR MIT" and cargo["receipt"]["notes"]


def test_each_ecosystem_orders_and_matches_by_its_own_rules():
    assert compare("pypi", "1.0", "1.0.0") == 0 and compare("pypi", "2.0rc1", "2.0") < 0
    assert (
        compare("npm", "1.0.0-beta.2", "1.0.0-beta.11") < 0
        and compare("npm", "1.10.0", "1.9.0") > 0
    )
    assert (
        compare("maven", "1.0-SNAPSHOT", "1.0") < 0
        and compare("maven", "1.0-sp", "1.0") > 0
    )
    assert (
        compare("maven", "1.0.0", "1") == 0
        and compare("maven", "1.0-rc1", "1.0-cr1") == 0
    )
    assert satisfies("npm", "^1.2.3", "1.9.0") and not satisfies(
        "npm", "^1.2.3", "1.5.0-beta"
    )
    assert satisfies("npm", ">=1.2.3-beta <2", "1.2.3-beta.2") and satisfies(
        "npm", "1.2.3 - 2.3", "2.3.9"
    )
    assert not satisfies("npm", "^0.0.3", "0.0.4")
    assert satisfies("cargo", "1.2", "1.9.0") and not satisfies("cargo", "0.2", "0.3.0")
    assert satisfies("cargo", ">=1.0, <1.5", "1.4.9") and not satisfies(
        "cargo", "~1.2", "1.3.0"
    )
    assert not satisfies("pypi", ">=1.0", "2.0rc1") and satisfies(
        "pypi", ">=2.0rc1", "2.0rc1"
    )
    assert satisfies("maven", "[1.0,2.0)", "1.5") and not satisfies(
        "maven", "(,1.0],[1.2,)", "1.1"
    )
    assert satisfies("maven", "1.0", "1.0.0")  # a soft requirement resolves to itself

"""Unit tests for entity canonicalization (canonical entities + alias links)."""

import duckdb
import pytest

from src.kb.entities import (
    add_manual_alias,
    ensure_entity_schema,
    expand,
    mention_counts,
    normalize_surface,
    resolve,
    run_entity_canonicalization_pass,
    seed_from_correction_store,
)


def _seed_actors(conn, names):
    ensure_entity_schema(conn)
    for index, name in enumerate(names):
        conn.execute(
            "INSERT INTO document_actors"
            " (document_id, source_type, actor_name, role, confidence)"
            " VALUES (?, 'news', ?, 'subject', 0.9)",
            [f"doc-{index}", name],
        )


@pytest.fixture()
def conn():
    return duckdb.connect()


class TestNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("The Federal Reserve", "federal reserve"),
            ("federal reserve.", "federal reserve"),
            ("U.S. Treasury", "us treasury"),
            ("Acme Corp", "acme"),
            ("Acme, Inc.", "acme"),
            ("  OpenAI   ", "openai"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_surface(raw) == expected


class TestAutomaticPass:
    def test_variants_converge_on_one_canonical(self, conn):
        _seed_actors(
            conn,
            ["The Federal Reserve", "Federal Reserve", "federal reserve."],
        )
        summary = run_entity_canonicalization_pass(conn)
        assert summary["scanned"] == 3
        assert summary["new_canonicals"] == 1

        resolved = {
            resolve(conn, name)["canonical_id"]
            for name in ["The Federal Reserve", "Federal Reserve", "federal reserve."]
        }
        assert len(resolved) == 1

    def test_fuzzy_variant_links_when_unambiguous(self, conn):
        _seed_actors(conn, ["European Central Bank", "European Central Bankk"])
        run_entity_canonicalization_pass(conn)
        a = resolve(conn, "European Central Bank")
        b = resolve(conn, "European Central Bankk")
        assert a["canonical_id"] == b["canonical_id"]
        assert b["method"] in ("similarity", "exact-normalize")

    def test_distinct_entities_stay_distinct(self, conn):
        _seed_actors(conn, ["World Bank", "World Health Organization"])
        run_entity_canonicalization_pass(conn)
        assert (
            resolve(conn, "World Bank")["canonical_id"]
            != resolve(conn, "World Health Organization")["canonical_id"]
        )

    def test_ambiguous_surface_left_unlinked(self, conn):
        # "Banco Nacional" sits exactly between two distinct candidates
        # (ratio 0.929 to each; the candidates are 0.857 apart, below the
        # link threshold, so they stay separate canonicals).
        _seed_actors(conn, ["Banco National", "Banca Nacional"])
        run_entity_canonicalization_pass(conn)
        conn.execute(
            "INSERT INTO document_actors"
            " (document_id, source_type, actor_name, role, confidence)"
            " VALUES ('doc-x', 'news', 'Banco Nacional', 'subject', 0.9)"
        )
        summary = run_entity_canonicalization_pass(conn)
        assert summary["ambiguous"] == 1
        # It resolves — but to its own canonical, not either candidate.
        own = resolve(conn, "Banco Nacional")
        assert own["canonical_id"] not in {
            resolve(conn, "Banco National")["canonical_id"],
            resolve(conn, "Banca Nacional")["canonical_id"],
        }

    def test_incremental_ledger(self, conn):
        _seed_actors(conn, ["OpenAI"])
        first = run_entity_canonicalization_pass(conn)
        assert first["scanned"] == 1
        second = run_entity_canonicalization_pass(conn)
        assert second["scanned"] == 0


class TestManualAliases:
    def test_manual_alias_bridges_semantics(self, conn):
        _seed_actors(conn, ["Fed", "Federal Reserve"])
        run_entity_canonicalization_pass(
            conn, manual_aliases=[("Fed", "Federal Reserve")]
        )
        assert (
            resolve(conn, "Fed")["canonical_id"]
            == resolve(conn, "Federal Reserve")["canonical_id"]
        )
        assert resolve(conn, "Fed")["method"] == "manual"

    def test_manual_survives_and_outranks_reruns(self, conn):
        _seed_actors(conn, ["Fed"])
        add_manual_alias(conn, "Fed", "Federal Reserve")
        run_entity_canonicalization_pass(conn)
        resolved = resolve(conn, "Fed")
        assert resolved["method"] == "manual"
        assert resolved["preferred_name"] == "Federal Reserve"

    def test_seed_from_correction_store(self, conn):
        class Correction:
            def __init__(self):
                self.status = "approved"
                self.correction_type = "merge_duplicates"
                self.payload = {
                    "target_name": "Federal Reserve",
                    "source_name": "Fed",
                }

        class Store:
            def list_corrections(self):
                return [Correction()]

        ensure_entity_schema(conn)
        assert seed_from_correction_store(conn, Store()) == 1
        assert resolve(conn, "Fed")["preferred_name"] == "Federal Reserve"


class TestAggregates:
    def test_mention_counts_fold_aliases(self, conn):
        _seed_actors(
            conn,
            ["Federal Reserve", "The Federal Reserve", "federal reserve.", "ECB"],
        )
        run_entity_canonicalization_pass(conn)
        canonical_id = resolve(conn, "Federal Reserve")["canonical_id"]
        counts = mention_counts(conn, canonical_id)
        assert counts["total_mentions"] == 3
        assert len(counts["by_alias"]) == 3

    def test_expand_lists_all_surfaces(self, conn):
        _seed_actors(conn, ["Fed", "Federal Reserve"])
        run_entity_canonicalization_pass(
            conn, manual_aliases=[("Fed", "Federal Reserve")]
        )
        canonical_id = resolve(conn, "Federal Reserve")["canonical_id"]
        assert set(expand(conn, canonical_id)) == {"fed", "federal reserve"}

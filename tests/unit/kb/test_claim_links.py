"""Unit tests for the claim linking pass and the shared NLI heuristic floor."""

import duckdb
import pytest

from src.kb.claim_links import (
    delete_run,
    ensure_claim_link_schema,
    run_claim_linking_pass,
)
from src.kb.nli import CONTRADICTION, ENTAILMENT, NEUTRAL, HeuristicNLI

DAY_MS = 86_400_000
BASE_MS = 1_750_000_000_000


class FakeNLI:
    """Deterministic NLI double driven by marker tokens."""

    name = "nli:fake-model"
    prediction_mode = "zero-shot:fake-model"
    model_version = "fake-model@r1"

    def classify(self, premise, hypothesis):
        from src.kb.nli import NLIResult

        p, h = premise.lower(), hypothesis.lower()
        same_topic = ("rates" in p and "rates" in h) or ("mpox" in p and "mpox" in h)
        if same_topic:
            flip = ("not" in p) != ("not" in h) or ("over" in p) != ("over" in h)
            if flip:
                return NLIResult(CONTRADICTION, 0.9, self.prediction_mode)
            return NLIResult(ENTAILMENT, 0.85, self.prediction_mode)
        return NLIResult(NEUTRAL, 0.6, self.prediction_mode)


class FakeProvider:
    VOCAB = [
        "central", "bank", "raise", "rates", "september", "mpox",
        "outbreak", "over", "continues", "gardening", "tomatoes",
    ]

    def embed_texts(self, texts):
        out = []
        for text in texts:
            tokens = set(text.lower().replace(".", " ").split())
            out.append([1.0 if word in tokens else 0.0 for word in self.VOCAB])
        return out


def _seed_claims(conn, rows):
    """rows: (claim_id, text, document_id, ingested_at, domain or None)"""
    ensure_claim_link_schema(conn)
    for claim_id, text, doc_id, ingested_at, domain in rows:
        conn.execute(
            "INSERT OR IGNORE INTO documents (document_id, source_type, ingested_at)"
            " VALUES (?, 'news', ?)",
            [doc_id, ingested_at],
        )
        conn.execute(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
            " source_type, confidence) VALUES (?, ?, ?, 'news', 0.8)",
            [claim_id, text, doc_id],
        )
        if domain:
            conn.execute(
                "INSERT INTO document_domains VALUES (?, ?, 1.0, 'source', 'r0', 0)",
                [doc_id, domain],
            )


@pytest.fixture()
def conn():
    return duckdb.connect()


DUP_A = "The central bank will raise rates in September."
DUP_B = "Central bank set to raise rates September."
CONTRA = "The central bank will not raise rates in September."
UNRELATED = "Gardening tomatoes continues."


class TestLinking:
    def test_duplicate_link_with_provenance(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, "economics"),
                ("c2", DUP_B, "d2", BASE_MS + DAY_MS, "economics"),
            ],
        )
        summary = run_claim_linking_pass(
            conn, provider=FakeProvider(), nli=FakeNLI()
        )
        assert summary["links"]["duplicate"] == 1
        row = conn.execute(
            "SELECT domain_a, claim_a, claim_b, method, prediction_mode,"
            " model_version, run_id FROM claim_links WHERE relation = 'duplicate'"
        ).fetchone()
        assert row[1] == "c1" and row[2] == "c2"  # canonical order
        assert row[0] == "economics"
        assert row[3] == "nli:fake-model"
        assert row[4] == "zero-shot:fake-model"
        assert row[5] == "fake-model@r1"
        assert row[6] == summary["run_id"]

    def test_contradiction_link(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, "economics"),
                ("c3", CONTRA, "d3", BASE_MS + DAY_MS, None),
            ],
        )
        summary = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert summary["links"]["contradicts"] == 1

    def test_supersedes_written_for_gapped_duplicates(self, conn):
        _seed_claims(
            conn,
            [
                ("old", DUP_A, "d1", BASE_MS, None),
                ("new", DUP_B, "d2", BASE_MS + 5 * DAY_MS, None),
            ],
        )
        run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        row = conn.execute(
            "SELECT claim_a, claim_b, method FROM claim_links"
            " WHERE relation = 'supersedes'"
        ).fetchone()
        assert row is not None
        assert (row[0], row[1]) == ("new", "old")  # newer supersedes older
        assert row[2].endswith("+temporal")

    def test_time_window_blocks_far_candidates(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("far", DUP_B, "d2", BASE_MS + 60 * DAY_MS, None),
            ],
        )
        summary = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert summary["links"]["duplicate"] == 0

    def test_unrelated_claims_stay_unlinked(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("u1", UNRELATED, "d2", BASE_MS, None),
            ],
        )
        run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] == 0

    def test_offline_heuristic_mode_links_duplicates(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("c2", DUP_A, "d2", BASE_MS + DAY_MS, None),
                ("c3", CONTRA, "d3", BASE_MS + DAY_MS, None),
            ],
        )
        summary = run_claim_linking_pass(conn)  # no provider, no NLI
        assert summary["mode"] == "heuristic"
        relations = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT relation, prediction_mode FROM claim_links"
            ).fetchall()
        }
        assert "duplicate" in relations
        assert relations["duplicate"] == "heuristic"
        assert "contradicts" in relations


class TestIncrementality:
    def test_second_run_scans_nothing(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("c2", DUP_B, "d2", BASE_MS, None),
            ],
        )
        run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        links_before = conn.execute("SELECT * FROM claim_links ORDER BY claim_a").fetchall()

        second = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert second["scanned"] == 0
        assert (
            conn.execute("SELECT * FROM claim_links ORDER BY claim_a").fetchall()
            == links_before
        )

    def test_new_claim_links_against_old_corpus(self, conn):
        _seed_claims(conn, [("c1", DUP_A, "d1", BASE_MS, None)])
        run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())

        _seed_claims_more = [("c2", DUP_B, "d2", BASE_MS + DAY_MS, None)]
        for claim_id, text, doc_id, ingested_at, _ in _seed_claims_more:
            conn.execute(
                "INSERT INTO documents (document_id, source_type, ingested_at)"
                " VALUES (?, 'news', ?)",
                [doc_id, ingested_at],
            )
            conn.execute(
                "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
                " source_type, confidence) VALUES (?, ?, ?, 'news', 0.8)",
                [claim_id, text, doc_id],
            )
        summary = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert summary["scanned"] == 1
        assert summary["links"]["duplicate"] == 1

    def test_delete_run_reverts_links_and_ledger(self, conn):
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, None),
                ("c2", DUP_B, "d2", BASE_MS, None),
            ],
        )
        summary = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] > 0

        result = delete_run(conn, summary["run_id"])
        assert result["links_deleted"] > 0
        assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] == 0
        # Claims are unscanned again: the next pass reassesses them.
        rerun = run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
        assert rerun["scanned"] == 2
        assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] > 0


class TestHeuristicNLI:
    def test_entailment_on_high_overlap(self):
        result = HeuristicNLI().classify(DUP_A, DUP_B)
        assert result.label == ENTAILMENT
        assert result.prediction_mode == "heuristic"

    def test_contradiction_on_negation_flip(self):
        result = HeuristicNLI().classify(DUP_A, CONTRA)
        assert result.label == CONTRADICTION

    def test_contradiction_on_antonyms(self):
        result = HeuristicNLI().classify(
            "Inflation rose sharply last quarter in Europe.",
            "Inflation fell sharply last quarter in Europe.",
        )
        assert result.label == CONTRADICTION

    def test_neutral_on_disjoint_topics(self):
        result = HeuristicNLI().classify(DUP_A, UNRELATED)
        assert result.label == NEUTRAL

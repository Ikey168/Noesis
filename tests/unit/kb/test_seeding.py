"""Unit tests for the shipped starter domains and idempotent feed seeding."""

import time

import duckdb

from src.ingestion.connectors.blog.subscriptions import SubscriptionStore
from src.ingestion.document_store import DocumentStore
from src.kb import load_registry
from src.kb.membership import run_membership_pass
from src.kb.seeding import seed_domain_feeds

EXPECTED_DOMAINS = [
    "news", "economics", "technology", "web3", "local", "political", "papers"
]


class TestShippedConfig:
    def test_starter_domains_load_and_validate(self):
        registry = load_registry()
        assert registry.names() == EXPECTED_DOMAINS

    def test_all_domains_share_one_embedding_space(self):
        models = set(load_registry().embedding_models().values())
        assert models == {"all-MiniLM-L6-v2"}

    def test_papers_rides_arxiv_rss(self):
        papers = load_registry().get("papers")
        assert papers.backing == "corpus-view"
        assert any("rss.arxiv.org" in feed.url for feed in papers.feeds)

    def test_local_is_the_private_corpus_domain(self):
        local = load_registry().get("local")
        assert local.feeds == []
        assert {"local", "private"} <= set(local.tags)
        assert local.keywords
        assert "private" in local.description.lower()

    def test_political_uses_manifest_ingestion_not_network_feeds(self):
        political = load_registry().get("political")
        assert political.backing == "corpus-view"
        assert political.feeds == []
        assert {"political", "government", "election"} <= set(political.tags)

    def test_every_feed_carries_its_domain_tags(self):
        registry = load_registry()
        for definition in registry.domains():
            for feed in definition.feeds:
                assert set(feed.tags) & set(definition.tags), (
                    f"{definition.name}: {feed.url} lacks a domain tag"
                )


class TestSeeding:
    def test_seed_subscribes_missing_feeds(self, tmp_path):
        store = SubscriptionStore(path=tmp_path / "subs.json")
        summary = seed_domain_feeds(load_registry(), store=store)
        subscribed = {sub.url for sub in store.list()}
        assert len(summary["added"]) == len(subscribed)
        assert "https://www.coindesk.com/arc/outboundfeeds/rss/" in subscribed

    def test_seed_is_idempotent(self, tmp_path):
        store = SubscriptionStore(path=tmp_path / "subs.json")
        seed_domain_feeds(load_registry(), store=store)
        second = seed_domain_feeds(load_registry(), store=store)
        assert second["added"] == []
        assert second["retagged"] == []
        assert len(second["unchanged"]) == len(store.list())

    def test_seed_preserves_user_edits_and_merges_tags(self, tmp_path):
        store = SubscriptionStore(path=tmp_path / "subs.json")
        # User subscribed CoinDesk earlier under their own name and tag.
        store.subscribe(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            name="My CoinDesk",
            tags=["favourites"],
        )
        # And their own feed the config knows nothing about.
        store.subscribe("https://example.com/mine.xml", name="Mine", tags=["local"])

        seed_domain_feeds(load_registry(), store=store)

        coindesk = store.get("https://www.coindesk.com/arc/outboundfeeds/rss/")
        assert coindesk.name == "My CoinDesk"
        assert {"favourites", "web3", "crypto"} <= set(coindesk.tags)
        assert store.get("https://example.com/mine.xml") is not None


class TestShippedDefinitionsAssign:
    def test_cross_domain_overlap_with_real_config(self):
        conn = duckdb.connect()
        DocumentStore(conn).upsert(
            [
                {
                    "document_id": "overlap-1",
                    "source_type": "news",
                    "language": "en",
                    "ingested_at": int(time.time() * 1000),
                    "source_id": "wire",
                    "url": "https://example.com/stablecoin",
                    "title": "Central bank weighs stablecoin rules",
                    "content": (
                        "The central bank said inflation and interest rates "
                        "shape its view of stablecoin and defi regulation."
                    ),
                    "metadata": {},
                }
            ]
        )
        run_membership_pass(conn, load_registry())
        domains = {
            row[0]
            for row in conn.execute(
                "SELECT domain FROM document_domains WHERE document_id = 'overlap-1'"
            ).fetchall()
        }
        assert {"economics", "web3"} <= domains

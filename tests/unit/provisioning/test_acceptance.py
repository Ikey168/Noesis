"""R9 acceptance: two domains stood up by provisioning alone, no pack code.

This is the Track P acceptance test (issues #609, #610). It provisions a
finance domain (earnings-call transcripts) and a legal domain (policy filings)
using only the provisioning verbs, asserts each namespace holds only its own
routed documents, that the shared corpus is untouched, and that the scoped
panel family (documents / entities / claims) is available per namespace. No
``DomainPack`` is imported or defined; the domains exist entirely as data plus
provisioning state.
"""

from datetime import datetime, timezone

import pytest

from src.provisioning import namespaces, store
from src.provisioning.guardrails import Quotas
from src.provisioning.provisioner import Provisioner


def _now():
    return datetime(2026, 6, 15, tzinfo=timezone.utc)


def _seed_two_domains(seed):
    # A shared corpus mixing three domains' sources in one news_articles table.
    seed.articles(
        [
            # Finance: earnings-call transcripts.
            ("f1", "Acme Corp Q3 earnings call transcript", "u1", "c", _now(), "Acme Earnings", "earnings"),
            ("f2", "Acme Corp raises full-year guidance", "u2", "c", _now(), "Acme Earnings", "earnings"),
            ("f3", "Globex investor call on margins", "u3", "c", _now(), "Globex Calls", "earnings"),
            # Legal: policy and rule filings.
            ("l1", "Proposed rule on emissions disclosure", "u4", "c", _now(), "Federal Register", "policy"),
            ("l2", "Comment period opens for privacy rule", "u5", "c", _now(), "Federal Register", "policy"),
            # News: unrelated, must never leak into either namespace.
            ("n1", "Local election results tonight", "u6", "c", _now(), "City News", "politics"),
        ]
    )
    seed.claims(
        [
            ("cf1", "Cloud revenue grew 34 percent.", "f1", "transcript", 0.9, "supported"),
            ("cf2", "Guidance assumes no rate hikes.", "f2", "transcript", 0.7, "unverified"),
            ("cl1", "The rule takes effect in 90 days.", "l1", "legal", 0.85, "supported"),
            ("cn1", "Turnout was a record high.", "n1", "news", 0.6, None),
        ]
    )
    seed.outlet_scores(
        [
            ("Acme Earnings", "news", "2026-06-15", 0.8, 0.9, 0.7, 0.81, 30, 20, "2026-06-15"),
            ("Globex Calls", "news", "2026-06-15", 0.7, 0.8, 0.7, 0.76, 20, 12, "2026-06-15"),
            ("Federal Register", "news", "2026-06-15", 0.9, 0.95, 0.8, 0.9, 40, 25, "2026-06-15"),
        ]
    )


def _provision(prov, name, description, *, sources=None, criteria=None):
    """The provisioning-only standup sequence one domain goes through."""
    assert prov.deploy(name, description, approve=True)["deployed"] is True
    at = prov.attach_sources(name, sources=sources, criteria=criteria)
    assert "error" not in at, at
    ing = prov.ingest(name)
    assert ing["ingested"] is True, ing
    return ing


def test_two_domains_live_via_provisioning_alone(seed):
    _seed_two_domains(seed)
    prov = Provisioner(seed.conn, quotas=Quotas(), clock=_now)

    # Finance: attached by a quality criterion (transparency >= 0.7, all
    # three finance/legal outlets clear it, but we scope by explicit source
    # to keep finance and legal apart under the shared corpus).
    _provision(
        prov,
        "finance",
        "Earnings-call transcripts",
        sources=["Acme Earnings", "Globex Calls"],
    )
    # Legal: attached by criteria against outlet_scores.
    _provision(
        prov,
        "legal",
        "Policy and rule filings",
        sources=["Federal Register"],
    )

    fin = prov.status("finance")
    leg = prov.status("legal")
    assert fin["counts"]["documents"] == 3  # f1, f2, f3
    assert leg["counts"]["documents"] == 2  # l1, l2

    # Each namespace holds ONLY its own routed documents.
    fin_ids = {
        r[0]
        for r in seed.conn.execute(
            "SELECT id FROM kg_finance_documents"
        ).fetchall()
    }
    leg_ids = {
        r[0]
        for r in seed.conn.execute("SELECT id FROM kg_legal_documents").fetchall()
    }
    assert fin_ids == {"f1", "f2", "f3"}
    assert leg_ids == {"l1", "l2"}
    assert fin_ids.isdisjoint(leg_ids)
    assert "n1" not in fin_ids and "n1" not in leg_ids  # news never leaked

    # Scoped claims followed the documents, not the shared claim layer.
    assert fin["counts"]["claims"] == 2
    assert leg["counts"]["claims"] == 1

    # The shared corpus is untouched by either provision.
    assert seed.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 6
    assert seed.conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()[0] == 4


def test_scoped_panel_family_is_available_per_namespace(seed):
    _seed_two_domains(seed)
    prov = Provisioner(seed.conn, quotas=Quotas(), clock=_now)
    _provision(prov, "finance", "Earnings", sources=["Acme Earnings", "Globex Calls"])

    view = prov.view("finance")
    assert view["count"] == 1
    fam = view["kgs"][0]["sample"]
    # documents / entities / claims all present and scoped to finance.
    assert fam["documents"] and all(
        d["source"] in {"Acme Earnings", "Globex Calls"} for d in fam["documents"]
    )
    assert fam["entities"]  # derived from routed titles
    assert fam["claims"] and any("Cloud revenue" in c["text"] for c in fam["claims"])


def test_no_domain_pack_code_is_imported(seed):
    """The domains are provisioned, not coded: no pack module is needed to
    stand them up. Guards against a regression that reintroduces a pack
    dependency into the provisioning path."""
    import sys

    _seed_two_domains(seed)
    prov = Provisioner(seed.conn, quotas=Quotas(), clock=_now)
    _provision(prov, "finance", "Earnings", sources=["Acme Earnings"])
    # No finance/legal domain pack module exists or was loaded.
    assert "src.domains.finance" not in sys.modules
    assert "src.domains.legal" not in sys.modules
    import importlib.util

    assert importlib.util.find_spec("src.domains.finance") is None
    assert importlib.util.find_spec("src.domains.legal") is None

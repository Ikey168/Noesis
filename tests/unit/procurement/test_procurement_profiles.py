"""Private supplier and buyer profiles (P03) on the Funding & Grants profile machinery."""

import json

import duckdb
import jsonschema
import pytest

from src.kb.funding_profiles import FundingProfileError, FundingProfileStore
from src.kb.procurement_profiles import EXCLUSION_GROUNDS, ProcurementProfileError, ProcurementProfileStore, money_facts
from tests.unit.procurement.harness import NS, ROOT, SCOPES, SUPPLIER_FACTS


@pytest.fixture
def store():
    return ProcurementProfileStore(duckdb.connect(), now=lambda: 1)


def test_supplier_profile_records_capabilities_and_self_declarations_with_revisions(store):
    created = store.create(NS, "s", label="Synthetic supplier", principal_id="alice", scopes=SCOPES)
    assert created["profile_kind"] == "supplier" and created["profile_id"].startswith("procurement-profile:")
    assert "exclusion.tax_arrears" in store.inspect(NS, created["profile_id"], principal_id="alice", scopes=SCOPES)["unknown_facts"]
    updated = store.update(NS, created["profile_id"], "c1", 1, set_facts=SUPPLIER_FACTS, principal_id="alice", scopes=SCOPES)
    assert updated["revision"] == 2
    view = store.inspect(NS, created["profile_id"], principal_id="alice", scopes=SCOPES)
    assert view["sections"]["exclusion"]["exclusion.tax_arrears"]["value"] is False
    assert "exclusion.misrepresentation" in view["unknown_facts"]  # unstated grounds stay unknown, never false
    assert not {k for k in view["unknown_facts"] if k.startswith("buyer.")}  # buyer facts are not asked of suppliers
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-procurement-profile-v1.json").read_text())
    jsonschema.validate({k: v for k, v in view.items() if k not in {"unknown_facts", "unreviewed_facts", "status", "current_revision"}}, schema)
    assert set(EXCLUSION_GROUNDS) <= set(store.FACTS)


def test_buyer_profile_records_organisation_sector_and_thresholds(store):
    buyer = store.create(NS, "b", label="Synthetic buyer", principal_id="bob", scopes=SCOPES, kind="buyer")
    store.update(NS, buyer["profile_id"], "c1", 1, principal_id="bob", scopes=SCOPES, set_facts={
        "buyer.organisation_name": {"value": "Bezirksamt Fixture-Mitte"}, "buyer.sector": {"value": "local-government"},
        "buyer.thresholds": {"value": [{"label": "EU services threshold", "amount": "221000", "currency": "EUR"}]}})
    view = store.inspect(NS, buyer["profile_id"], principal_id="bob", scopes=SCOPES)
    assert view["profile_kind"] == "buyer" and "buyer.country" in view["unknown_facts"]
    with pytest.raises(ProcurementProfileError):
        store.update(NS, buyer["profile_id"], "c2", 2, principal_id="bob", scopes=SCOPES,
                     set_facts={"supplier.annual_turnover": {"value": {"amount": "1", "currency": "EUR"}}})
    with pytest.raises(ProcurementProfileError):
        store.create(NS, "x", label="?", principal_id="bob", scopes=SCOPES, kind="agency")


def test_profiles_are_owner_scoped_without_operator_bypass_and_withdrawable(store):
    created = store.create(NS, "s", label="Synthetic supplier", principal_id="alice", scopes=SCOPES)
    for principal, scopes in (("mallory", SCOPES), ("root", SCOPES | {"operator"}), ("alice", {"knowledge:procurement:read"})):
        with pytest.raises(FundingProfileError) as caught:
            store.inspect(NS, created["profile_id"], principal_id=principal, scopes=scopes)
        assert caught.value.code == "unauthorized"
    assert store.list(NS, principal_id="mallory", scopes=SCOPES) == {"profiles": []}
    store.withdraw(NS, created["profile_id"], principal_id="alice", scopes=SCOPES)
    with pytest.raises(FundingProfileError) as caught:
        store.inspect(NS, created["profile_id"], principal_id="alice", scopes=SCOPES)
    assert caught.value.code == "profile_withdrawn"


def test_proposed_facts_stay_unknown_until_the_owner_reviews_them(store):
    created = store.create(NS, "s", label="Synthetic supplier", principal_id="alice", scopes=SCOPES)
    store.update(NS, created["profile_id"], "c1", 1, principal_id="alice", scopes=SCOPES,
                 propose_facts={"supplier.certifications": {"value": ["ISO 9001"], "evidence": [{"kind": "import", "id": "crm"}]}})
    pinned = store.pinned_facts(NS, created["profile_id"], 2, principal_id="alice", scopes=SCOPES)
    assert "supplier.certifications" not in pinned["facts"] and pinned["unreviewed"] == ["supplier.certifications"]
    store.update(NS, created["profile_id"], "c2", 2, principal_id="alice", scopes=SCOPES, review=["supplier.certifications"])
    assert store.pinned_facts(NS, created["profile_id"], 3, principal_id="alice", scopes=SCOPES)["facts"]["supplier.certifications"] == ["ISO 9001"]
    with pytest.raises(FundingProfileError):
        store.update(NS, created["profile_id"], "c3", 3, principal_id="alice", scopes=SCOPES,
                     set_facts={"supplier.cpv_interests": {"value": ["not-a-cpv"]}})


def test_money_facts_are_exposed_per_currency_without_conversion():
    facts = money_facts({"supplier.annual_turnover": {"amount": "1500000", "currency": "EUR"}, "x": 1})
    assert facts["supplier.annual_turnover.EUR"] == "1500000" and "supplier.annual_turnover.GBP" not in facts


def test_funding_profiles_are_unchanged_by_the_generalisation():
    conn = duckdb.connect()
    scopes = {"knowledge:funding:read", "knowledge:funding:write", "namespace:g:write"}
    funding = FundingProfileStore(conn, now=lambda: 1)
    profile = funding.create("g", "k", label="Applicant", principal_id="alice", scopes=scopes)
    assert profile["profile_id"].startswith("funding-profile:") and "profile_kind" not in profile
    procurement = ProcurementProfileStore(conn, now=lambda: 1)
    assert conn.execute("SELECT count(*) FROM funding_profiles").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM procurement_profiles").fetchone()[0] == 0
    with pytest.raises(FundingProfileError):
        procurement.inspect("g", profile["profile_id"], principal_id="alice", scopes=scopes)

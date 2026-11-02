"""Offline world for holding-structure classification and the structure dossier (#2890).

Everything is fictional and authored here or in ``tests/fixtures``; nothing is live coverage:

* Corporate Ownership records (namespace ``ownership``): the holding company
  *Example Holdings Ltd* (Companies House 09990002, VAT GB999999973) - the
  structure under study - held by *Example Family Trust* (a BODS 0.4
  arrangement whose stated subtype is ``trust``) and holding *Example
  Operating GmbH* (100 %); *Example Family Office Ltd* states no legal form
  and must stay unclassified whatever its name says.
* Political campaign finance (``global``): the UK Electoral Commission donation
  fixture, in which Example Holdings Ltd (09990002) donates; replayed through
  the campaign-finance adapter.
* Political lobbying (``global``): an authored UK ORCL export in which Example
  Holdings Ltd (09990002) is a registrant; replayed through the lobbying adapter.
* Economics public finance (``global``): an authored EU FTS file with two rows
  naming Example Holdings Ltd with its VAT number; replayed through the FTS adapter.

Identity between the owners is proposed by each owner's own proposer and
accepted by a reviewer in the shared state machine; nothing is matched by name
in the dossier.
"""

from __future__ import annotations

import duckdb

NS = "global"
OWN_NS = "ownership"
STRUCTURE = "companies-house:gb-coh:09990002"
TRUST = "open-ownership:statement:fixture-trust-1"
OPERATING = "companies-house:gb-coh:09990301"
FAMILY_OFFICE = "companies-house:gb-coh:09990302"
SCOPES = {
    "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ownership:review",
    f"namespace:{OWN_NS}:read", f"namespace:{OWN_NS}:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:political:lobbying:read", "knowledge:political:lobbying:write", "knowledge:political:lobbying:review",
    "knowledge:political:campaign-finance:read", "knowledge:political:campaign-finance:write",
    "knowledge:political:campaign-finance:review", "knowledge:economic:public-finance:read",
    "knowledge:economic:public-finance:write", "knowledge:economic:public-finance:review",
    "knowledge:reports:read", "knowledge:reports:write",
}
ANALYST = "analyst"
REVIEWER = "reviewer"
ORCL_BODY = (
    "registrant_id,registrant_name,company_number,address,quarter,client_name,return_submitted_on\n"
    "ORCL0177,Example Holdings Ltd,09990002,2 Example Square London,2099 Q1,Fictional Grid Association,2099-04-20\n"
)
FTS_BODY = (
    "Year,Name of beneficiary,VAT number,Country,City,Budget line name and number,Programme name,"
    "Commitment position key,Funding type,Subject of grant or contract,Type of amount,Amount,Currency\n"
    "2099,Example Holdings Ltd,GB999999973,GB,London,99 01 02 01 - Fictional Research Programme,"
    "Fictional Horizon Programme,FTS-2099-000177,Grants,Fictional grid study,Commitment,\"300,000.00\",EUR\n"
    "2099,Example Holdings Ltd,GB999999973,GB,London,99 01 02 01 - Fictional Research Programme,"
    "Fictional Horizon Programme,FTS-2099-000177,Grants,Fictional grid study,Payment,\"120,000.00\",EUR\n"
)
SELF_DESCRIPTION = {
    "kind": "document", "publisher": "Companies House (fictional filing)",
    "reference": "fixture:annual-report:09990002:2099", "sha256": "a" * 64,
    "url": "https://find-and-update.company-information.service.gov.uk/company/09990002/filing-history",
    "locator": {"page": 2, "section": "Principal activity"},
    "quote": "The principal activity of the company is that of a holding company.",
}


def ownership_records() -> list[dict]:
    from src.kb.ownership_records import record

    def ch(key: str, number: str) -> dict:
        return {"provider": "companies-house", "provider_record_id": number,
                "url": f"https://find-and-update.company-information.service.gov.uk/company/{number}"}

    return [
        record("legal_entity", STRUCTURE, ch(STRUCTURE, "09990002"), name="Example Holdings Ltd", jurisdiction="GB",
               identifiers=[{"scheme": "gb-coh", "value": "09990002"}, {"scheme": "vat", "value": "GB999999973"}],
               entity_type="ltd"),
        record("legal_entity", OPERATING, ch(OPERATING, "09990301"), name="Example Operating Ltd", jurisdiction="GB",
               identifiers=[{"scheme": "gb-coh", "value": "09990301"}], entity_type="ltd"),
        record("legal_entity", FAMILY_OFFICE, ch(FAMILY_OFFICE, "09990302"), name="Example Family Office Ltd",
               jurisdiction="GB", identifiers=[{"scheme": "gb-coh", "value": "09990302"}]),
        record("legal_entity", TRUST, {"provider": "open-ownership", "provider_record_id": "fixture-trust-1"},
               name="Example Family Trust", jurisdiction="GB",
               entity_type={"type": "arrangement", "subtype": "trust"}),
        record("ownership_assertion", f"companies-house:psc:09990002:{TRUST}", ch(STRUCTURE, "09990002"),
               subject_key=STRUCTURE, assertion_kind="other_control", holder={"key": TRUST, "kind": "entity",
                                                                                "name": "Example Family Trust"},
               validity={"from": "2090-01-01", "to_status": "open"}),
        record("ownership_assertion", f"companies-house:psc:09990301:{STRUCTURE}", ch(OPERATING, "09990301"),
               subject_key=OPERATING, assertion_kind="shareholding",
               holder={"key": STRUCTURE, "kind": "entity", "name": "Example Holdings Ltd"},
               share={"exact": "100"}, validity={"from": "2091-03-01", "to_status": "open"}),
    ]


def connection():
    return duckdb.connect(":memory:")


def load_ownership(conn) -> dict:
    from src.kb.ownership_store import OwnershipStore

    return OwnershipStore(conn).apply(OWN_NS, ownership_records(), run_id="structure-fixture", observed_at_ms=1,
                                      principal_id="ownership-loader")


def _accept(conn, candidates, key: str) -> list[str]:
    from src.kb.ownership_identity import OwnershipIdentityService

    service = OwnershipIdentityService(conn)
    accepted = []
    for candidate in candidates:
        records = candidate.get("records") or [candidate["left_key"], candidate["right_key"]]
        if key in records and candidate["state"] == "proposed":
            service.review(NS, candidate["candidate_id"], "accept", "fixture review", principal_id=REVIEWER,
                           scopes=SCOPES)
            accepted.append(candidate["candidate_id"])
    return accepted


def load_donations(conn, *, accept: bool = True) -> dict:
    from src.kb.campaign_finance_identity import CampaignFinanceIdentity
    from src.kb.campaign_finance_links import CampaignFinanceLinks
    from tests.unit import campaign_finance_harness as cf

    cf.apply(conn, "uk-ec-donations")
    proposed = CampaignFinanceIdentity(conn).propose(NS, principal_id=ANALYST, scopes=SCOPES,
                                                     ownership_namespace=OWN_NS)
    if accept:
        _accept(conn, proposed["candidates"], STRUCTURE)
    return CampaignFinanceLinks(conn).link_ownership(NS, OWN_NS, principal_id=ANALYST, scopes=SCOPES)


def load_lobbying(conn, *, accept: bool = True) -> list[str]:
    from src.kb.lobbying_identity import LobbyingIdentity
    from tests.unit import lobbying_harness as lob

    lob.apply(conn, "uk-orcl", "uk_orcl_structures.csv", body=ORCL_BODY, run_id="run:orcl-structures")
    proposed = LobbyingIdentity(conn).propose(NS, principal_id=ANALYST, scopes=SCOPES, ownership_namespace=OWN_NS)
    return _accept(conn, proposed["candidates"], STRUCTURE) if accept else []


def load_payments(conn, *, accept: bool = True) -> list[str]:
    from src.kb.public_finance_identity import PublicFinanceIdentity
    from tests.unit import public_finance_harness as pf

    pf.apply(conn, "fts", 0, FTS_BODY, run_id="run:fts-structures")
    proposed = PublicFinanceIdentity(conn).propose(NS, principal_id=ANALYST, scopes=SCOPES,
                                                   ownership_namespace=OWN_NS)
    return _accept(conn, proposed["candidates"], STRUCTURE) if accept else []


def classify(conn) -> dict:
    """The structure's self-description, proposed and accepted; the trust's stated BODS subtype, proposed only."""
    from src.kb.ownership_structures import StructureClassifications

    service = StructureClassifications(conn)
    holding = service.propose(OWN_NS, entity_key=STRUCTURE, structure_type="holding_company",
                              basis="self-description", stated_value="holding company", citation=SELF_DESCRIPTION,
                              principal_id=ANALYST, scopes=SCOPES)
    accepted = service.review(OWN_NS, holding["classification_id"], "accept", "annual report states it",
                              principal_id=REVIEWER, scopes=SCOPES)
    offered = service.propose_from_records(OWN_NS, principal_id=ANALYST, scopes=SCOPES)
    return {"holding": accepted, "offered": offered}


def world(*, classify_structure: bool = True):
    conn = connection()
    load_ownership(conn)
    load_donations(conn)
    load_lobbying(conn)
    load_payments(conn)
    if classify_structure:
        classify(conn)
    return conn


def enable(monkeypatch, *features: str) -> None:
    """Select optional features the way the active composition plan would (default: all used by the fixture)."""
    import src.kb.campaign_finance_records as campaign_finance
    from src.kb import lobbying, public_finance

    wanted = set(features or ("lobbying", "campaign-finance", "public-finance"))
    if "lobbying" in wanted:
        monkeypatch.setattr(lobbying, "feature_enabled", lambda conn, namespace=None: True)
    if "campaign-finance" in wanted:
        monkeypatch.setattr(campaign_finance, "feature_enabled", lambda conn, feature: True)
    if "public-finance" in wanted:
        monkeypatch.setattr(public_finance, "feature_enabled", lambda conn, namespace=None: True)


SECTIONS = {"lobbying": {"namespace": NS}, "political_donations": {"namespace": NS},
            "public_payments": {"namespace": NS}, "bafin_holdings": {"namespace": NS},
            "competition": {"namespace": NS}, "sanctions": {"namespace": NS}}

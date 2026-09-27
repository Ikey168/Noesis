"""Beneficiary identity candidates and Berlin district links (#1948, #1941)."""

from __future__ import annotations

import json

import pytest

from src.kb.funding_opportunities import FundingOpportunityStore
from src.kb.funding_records import record as funding_record
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.public_finance import PublicFinanceError, PublicFinanceStore
from src.kb.public_finance_identity import PublicFinanceIdentity, vat_key
from src.kb.public_finance_places import PublicFinancePlaces
from tests.unit import public_finance_harness as h

DE_BENEFICIARY = "public-finance:beneficiary:eu-fts:vat:DE:DE999999999"
PT_BENEFICIARY = "public-finance:beneficiary:eu-fts:vat:PT:PT999999990"
FUNDING = {
    "knowledge:funding:read",
    "knowledge:funding:write",
    "namespace:funding:read",
    "namespace:funding:write",
}
GEO = {"knowledge:geospatial:read", "knowledge:geospatial:write"}


@pytest.fixture()
def conn():
    conn = h.connection()
    h.apply(conn, "fts", 0, h.FTS)
    yield conn
    conn.close()


def _payments(conn):
    return conn.execute(
        "SELECT payment_id, statement_json FROM public_finance_payments ORDER BY payment_id"
    ).fetchall()


def _seed_ownership(conn):
    source = {"provider": "open-ownership", "provider_record_id": "s1"}
    OwnershipStore(conn).apply(
        "global",
        [
            record(
                "legal_entity",
                "bods:entity:beispiel",
                source,
                name="Beispiel Forschung GmbH",
                jurisdiction="DE",
                identifiers=[{"scheme": "vat", "value": "999999999"}],
            ),
            # The same digits issued by another country are another identifier.
            record(
                "legal_entity",
                "bods:entity:other",
                {**source, "provider_record_id": "s2"},
                name="Other SA",
                jurisdiction="FR",
                identifiers=[{"scheme": "vat", "value": "FR999999999"}],
            ),
            record(
                "legal_entity",
                "bods:entity:exemplo",
                {**source, "provider_record_id": "s3"},
                name="Exemplo Investigação, Lda.",
                jurisdiction="PT",
                identifiers=[],
            ),
        ],
        run_id="own",
        observed_at_ms=1,
        principal_id="p",
    )


def test_vat_numbers_compare_within_their_issuing_country():
    assert (
        vat_key("DE 999.999.999") == ("DE", "999999999") == vat_key("999999999", "DE")
    )
    assert vat_key("FR999999999", "FR") == ("FR", "999999999")
    assert vat_key("DE999999999", "FR") == (
        None,
        "999999999",
    )  # contradicting prefix: issuer unknown
    assert vat_key("EL123456789", "GR") == ("EL", "123456789")


def test_beneficiaries_are_offered_to_ownership_and_a_reviewed_match_can_be_reverted(
    conn,
):
    _seed_ownership(conn)
    before = _payments(conn)
    identity = PublicFinanceIdentity(conn)
    result = identity.propose(
        "global", principal_id="analyst", scopes=h.SCOPES, ownership_namespace="global"
    )
    pairs = {tuple(sorted(c["records"])): c for c in result["candidates"]}
    vat = pairs[tuple(sorted(("bods:entity:beispiel", DE_BENEFICIARY)))]
    assert (
        vat["basis"] == "cross-referenced-identifier"
        and vat["evidence"][0]["issuers"] == "same"
    )
    assert (
        vat["evidence"][0]["left"]["payment_id"]
        and vat["evidence"][0]["right"]["revision"] == 1
    )
    assert not any("bods:entity:other" in c["records"] for c in result["candidates"])
    name = pairs[tuple(sorted(("bods:entity:exemplo", PT_BENEFICIARY)))]
    assert name["basis"] == "name-jurisdiction"
    assert (
        identity.identity("global", DE_BENEFICIARY, scopes=h.SCOPES)["state"]
        == "unmatched"
    )
    accepted = identity.service.review(
        "global",
        vat["candidate_id"],
        "accept",
        "same VAT number",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert identity.view(accepted)["review_state"] == "reviewed-match"
    matched = identity.identity("global", DE_BENEFICIARY, scopes=h.SCOPES)
    assert (
        matched["state"] == "matched"
        and matched["links"][0]["target"] == "bods:entity:beispiel"
    )
    # Beneficiary links never regroup ownership entities.
    assert DE_BENEFICIARY not in OwnershipIdentityService(conn).clusters("global")
    identity.service.revert(
        "global",
        vat["candidate_id"],
        "wrong entity",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert (
        identity.identity("global", DE_BENEFICIARY, scopes=h.SCOPES)["state"]
        == "unmatched"
    )
    again = identity.propose(
        "global", principal_id="analyst", scopes=h.SCOPES, ownership_namespace="global"
    )
    assert (
        again["proposed"] == []
    )  # nothing new: a reverted candidate is not re-proposed on the same evidence
    assert (
        _payments(conn) == before
    )  # no payment record changed through propose, review or revert


def test_funding_awards_by_grant_reference_and_names_that_are_never_enough(conn):
    text = h.body(h.FTS).replace(
        "Subject of grant or contract,", "Subject of grant or contract,Grant reference,"
    )
    lines = text.splitlines()
    lines = [lines[0]] + [
        row.replace("Fictional sensor study,", "Fictional sensor study,GA-2099-0001,")
        if "Fictional sensor study" in row
        else row.replace(",Commitment,", ",,Commitment,", 1)
        for row in lines[1:]
    ]
    h.apply(
        conn,
        "fts",
        0,
        "\n".join(lines) + "\n",
        headers={"Last-Modified": "Fri, 30 Jul 2100 10:00:00 GMT"},
    )
    FundingOpportunityStore(conn).ingest(
        "funding",
        "eu-ft",
        [
            funding_record(
                "eu-ft",
                "award",
                "GA-2099-0001",
                "Award: fictional sensor study",
                source_url="https://ec.europa.eu/info/funding-tenders/award/1",
                authority={"kind": "funder", "name": "Fictional Agency"},
            ),
            funding_record(
                "eu-ft",
                "award",
                "GA-2099-0002",
                "Award: Exemplo Investigação Lda — materials",
                source_url="https://ec.europa.eu/info/funding-tenders/award/2",
                authority={"kind": "funder", "name": "Fictional Agency"},
            ),
        ],
        observation_id="f1",
        observed_at_ms=1,
        scopes={"operator"},
    )
    identity = PublicFinanceIdentity(conn)
    with pytest.raises(PublicFinanceError) as refused:
        identity.propose(
            "global",
            principal_id="a",
            scopes=h.SCOPES,
            funding_namespace="funding",
            canonical=False,
        )
    assert refused.value.code == "unauthorized"
    result = identity.propose(
        "global",
        principal_id="a",
        scopes=h.SCOPES | FUNDING,
        funding_namespace="funding",
        canonical=False,
    )
    by_target = {
        next(r for r in c["records"] if r.startswith("funding:")): c
        for c in result["candidates"]
    }
    reference = next(
        c for c in by_target.values() if c["evidence"][0]["kind"] == "grant-reference"
    )
    assert (
        reference["basis"] == "cross-referenced-identifier"
        and DE_BENEFICIARY in reference["records"]
    )
    name = next(c for c in by_target.values() if c["evidence"][0]["kind"] == "name")
    assert name["basis"] == "similar-name" and PT_BENEFICIARY in name["records"]
    with pytest.raises(OwnershipError) as never:
        identity.service.review(
            "global",
            name["candidate_id"],
            "accept",
            "title names it",
            principal_id="r",
            scopes=h.REVIEW_SCOPES,
        )
    assert never.value.code == "insufficient_evidence"


def test_canonical_entity_names_are_similar_name_candidates(conn):
    from src.kb.entities import add_manual_alias

    add_manual_alias(conn, "Beispiel Forschung GmbH", "Beispiel Forschung GmbH", "ORG")
    result = PublicFinanceIdentity(conn).propose(
        "global", principal_id="a", scopes=h.SCOPES
    )
    (candidate,) = [
        c
        for c in result["candidates"]
        if any(r.startswith("canonical:") for r in c["records"])
    ]
    assert (
        candidate["basis"] == "similar-name" and DE_BENEFICIARY in candidate["records"]
    )


def _boundaries(conn):
    from src.kb.geospatial_features import GeospatialFeatureStore

    square = [[[13.3, 52.5], [13.4, 52.5], [13.4, 52.6], [13.3, 52.6], [13.3, 52.5]]]
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f"bezirksgrenzen.1100000{n}",
                "geometry": {"type": "Polygon", "coordinates": square},
                "properties": {"gem": f"00{n}", "namgem": name},
            }
            for n, name in ((1, "Mitte"), (2, "Friedrichshain-Kreuzberg"))
        ],
    }
    GeospatialFeatureStore(conn).import_feature_collection(
        "geo",
        json.dumps(collection),
        provider="gdi-berlin",
        collection="alkis_bezirke:bezirksgrenzen",
        source_crs="EPSG:4326",
        title_property="namgem",
        principal_id="p",
        scopes=GEO,
    )


def test_district_lines_link_to_boundaries_and_places_only_through_the_published_code(
    conn,
):
    from src.kb.geospatial import GeospatialStore

    h.apply(conn, "berlin", 0, h.BERLIN)
    _boundaries(conn)
    place = GeospatialStore(conn).register_place(
        "geo",
        "Bezirk Mitte",
        "district",
        names=[{"value": "Mitte", "language": "de", "kind": "canonical"}],
        source_ids={"berlin-bezirk": "001"},
        parent_ids=[],
        principal_id="p",
        scopes=GEO,
    )
    places = PublicFinancePlaces(conn)
    with pytest.raises(PublicFinanceError):
        places.link_districts(
            "global", principal_id="a", scopes=h.SCOPES, geo_namespace="geo"
        )
    result = places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert len(result["linked"]) == 2 and result["unresolved"] == []
    store = PublicFinanceStore(conn)
    mitte = h.line_id(conn, "de-be-haushalt", bereich="31")
    link = places.place("global", mitte, scopes=h.SCOPES)
    assert (
        link["state"] == "linked"
        and link["feature_title"] == "Mitte"
        and link["place_id"] == place["place_id"]
    )
    fk = places.place(
        "global", h.line_id(conn, "de-be-haushalt", bereich="32"), scopes=h.SCOPES
    )
    assert fk["feature_title"] == "Friedrichshain-Kreuzberg" and fk["place_id"] is None
    main = h.line_id(conn, "de-be-haushalt", bereich="30")
    assert places.place("global", main, scopes=h.SCOPES)["state"] == "no-district"
    again = places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert again["linked"] == [] and again["unresolved"] == []
    assert (
        conn.execute(
            "SELECT count(*) FROM geospatial_places WHERE namespace='geo'"
        ).fetchone()[0]
        == 1
    )
    assert store.line("global", mitte)["district"]["code"] == "001"


def test_a_district_code_without_a_boundary_stays_unresolved(conn):
    h.apply(conn, "berlin", 0, h.BERLIN)
    result = PublicFinancePlaces(conn).link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert result["linked"] == [] and len(result["unresolved"]) == 2
    link = PublicFinancePlaces(conn).place(
        "global", h.line_id(conn, "de-be-haushalt", bereich="31"), scopes=h.SCOPES
    )
    assert link["state"] == "unresolved" and "no boundary feature" in link["reason"]


def test_the_current_district_link_is_the_latest_evaluation(conn):
    from src.kb.geospatial_features import GeospatialFeatureStore

    h.apply(conn, "berlin", 0, h.BERLIN)
    places = PublicFinancePlaces(conn)
    mitte = h.line_id(conn, "de-be-haushalt", bereich="31")
    places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert (
        "no boundary feature"
        in places.place("global", mitte, scopes=h.SCOPES)["reason"]
    )
    _boundaries(conn)
    places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert places.place("global", mitte, scopes=h.SCOPES)["state"] == "linked"
    # A second feature stating the same code: unresolved again, for a different reason than before.
    square = [[[13.3, 52.5], [13.4, 52.5], [13.4, 52.6], [13.3, 52.6], [13.3, 52.5]]]
    duplicate = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "bezirksgrenzen.dup",
                "geometry": {"type": "Polygon", "coordinates": square},
                "properties": {"gem": "001", "namgem": "Mitte (Duplikat)"},
            }
        ],
    }
    GeospatialFeatureStore(conn).import_feature_collection(
        "geo",
        json.dumps(duplicate),
        provider="other",
        collection="alkis_bezirke:bezirksgrenzen",
        source_crs="EPSG:4326",
        title_property="namgem",
        principal_id="p",
        scopes=GEO,
    )
    places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    current = places.place("global", mitte, scopes=h.SCOPES)
    assert (
        current["state"] == "unresolved"
        and "more than one boundary feature" in current["reason"]
    )
    reasons = [
        r[0]
        for r in conn.execute(
            "SELECT coalesce(reason, state) FROM public_finance_place_links WHERE line_id=? ORDER BY evaluation_no",
            [mitte],
        ).fetchall()
    ]
    assert len(reasons) == 3 and reasons[1] == "linked"
    places.link_districts(
        "global", principal_id="a", scopes=h.SCOPES | GEO, geo_namespace="geo"
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM public_finance_place_links WHERE line_id=?", [mitte]
        ).fetchone()[0]
        == 3
    )

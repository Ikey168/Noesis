"""RIPEstat, PeeringDB, RDAP, crt.sh and CT log list acquisition (II03-II06; #2756, #2763, #2772, #2777 under #2743).

Offline: authored fixtures replay through the real adapter; nothing here reaches a provider host.
"""

from __future__ import annotations

import ipaddress
import json
import re

import pytest

from src.ingestion import crtsh, rdap
from src.ingestion import internet_infrastructure_sources as ii
from src.ingestion.provider_execution import digest as provider_digest
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import internet_infrastructure_harness as h

AUDIT = " ".join((h.ROOT / ii.AUDIT).read_text().split())
# The OSINT pack's rdap-domain and crt-sh sources stay exactly as OX05/OX06 declared them.
OSINT_SOURCE_DIGESTS = {"rdap-domain": "6079120e7cec816b6de8d080a4a155b029ee74fa809b3f1e46199b6cfb27c297",
                        "crt-sh": "db67966337502e33acfbe1968221bb06fa93e45e495f6ecf8512e1e8464f3b28"}


def _items(name, **kwargs):
    return [r["ii_item"] for page in h.fetch(name, **kwargs) for r in page]


def _transport_with(name, replace, revision=None):
    """The fixture transport with one request's answer replaced."""
    pages = h.pages(name, revision)
    for page in pages:
        if page["request"] in replace:
            page.update(replace[page["request"]])
    return ii.fixture_transport(pages)


# ------------------------------------------------------------------ machine-readable copy of the audit


def test_contracts_coverage_caps_minimisation_exclusions_and_live_status_match_the_audit():
    assert set(ii.PROVIDER_CONTRACTS) == set(ii.PROVIDERS) | {"rfc6962-logs", "caida"}
    assert {p: c["status"] for p, c in ii.PROVIDER_CONTRACTS.items()} == {
        "ripestat": "unverified-live", "peeringdb": "unverified-live", "rdap": "unverified-live",
        "crtsh": "unverified-live", "ct-log-list": "unverified-live", "rfc6962-logs": "not-implemented",
        "caida": "not-implemented"}
    assert {p: v["status"] for p, v in ii.LIVE_VERIFICATION.items()} == {
        p: c["status"] for p, c in ii.PROVIDER_CONTRACTS.items()}
    assert all(v["checked"] is None and v["evidence"] is None for v in ii.LIVE_VERIFICATION.values())
    # Bounded first coverage, as the audit's table states it.
    for phrase in ("5 data calls per resource, 2 resources, 2,000 prefixes per response",
                   "1 network, 1 organisation, 20 `netixlan` rows, 5 IXs", "3 objects, 1 bootstrap fetch per file",
                   "1 request, 200 certificates", "1 file, 2 MB"):
        assert phrase in AUDIT
    assert ii.CAPS["ripestat"]["calls_per_resource"] == 5 and ii.CAPS["ripestat"]["resources"] == 2
    assert ii.CAPS["ripestat"]["prefixes_per_response"] == 2000 and ii.CAPS["ripestat"]["requests_per_second"] == 1
    assert (ii.CAPS["peeringdb"]["networks"], ii.CAPS["peeringdb"]["organisations"],
            ii.CAPS["peeringdb"]["netixlan_rows"], ii.CAPS["peeringdb"]["ixs"]) == (1, 1, 20, 5)
    assert ii.CAPS["rdap"]["objects"] == 3 and ii.CAPS["crtsh"]["certificates"] == 200
    assert ii.CAPS["ct-log-list"]["max_bytes"] == 2_000_000
    for key in ("stored", "redacted", "excluded", "retention", "who_may_query", "osint_gate"):
        assert ii.MINIMISATION[key]
    assert "vCard `kind` `individual` are never persisted" in AUDIT and "individual are never persisted" in ii.MINIMISATION["redacted"]
    assert "poc objects (all visibilities)" in ii.MINIMISATION["excluded"]
    assert "whois and abuse-contact-finder" in ii.MINIMISATION["excluded"]
    for exclusion in ("port or banner data", "subdomain enumeration", "IP-keyed", "person-keyed",
                      "hijack or misconfiguration verdicts", "ranking of networks", "into one record"):
        assert any(exclusion in e for e in ii.EXCLUSIONS), exclusion
    assert "_verify_" in AUDIT and "AUP" in ii.PROVIDER_CONTRACTS["peeringdb"]["redistribution"]
    assert "II13" in ii.PROVIDER_CONTRACTS["peeringdb"]["licence"]


def test_source_pack_entries_are_unverified_live_with_pinned_fixtures_that_replay():
    manifest = h.manifest()
    assert manifest["pack_id"] == "technology-internet-infrastructure" and manifest["version"] == "1.0.0"
    assert {s["source_id"] for s in manifest["sources"]} == set(h.SOURCES.values())
    for source in manifest["sources"]:
        block = source["internet_infrastructure"]
        assert block["live_verification"] == "unverified-live"
        assert ii.LIVE_VERIFICATION[block["provider"]]["status"] == "unverified-live"
    result = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert result["valid"] and result["coverage"]["configured"] == result["coverage"]["verified"] == 5


def test_fixtures_use_documentation_resources_example_org_and_dates_in_2094_to_2099():
    paths = [h.ROOT / s["fixture"]["path"] for s in h.manifest()["sources"]]
    paths += sorted(h.FIXTURES.glob("*.json"))
    documentation = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                                                       "2001:db8::/32")]
    for path in paths:
        text = path.read_text().replace("\\n", " ")
        for year in re.findall(r"\b(20\d\d)-\d\d-\d\d", text):
            assert 2094 <= int(year) <= 2099, (path.name, year)
        for asn in re.findall(r'"asn": (\d+)', text):
            assert 64496 <= int(asn) <= 64511, (path.name, asn)
        for address in re.findall(r'"ipaddr[46]": "([^"]+)"', text):
            assert any(ipaddress.ip_address(address) in n for n in documentation), address
        for domain in re.findall(r"[a-z0-9.-]+\.(?:org|com|net)\b", text):
            assert domain == "example.org" or domain.endswith(".example.org") or domain in {
                "rdap.publicinterestregistry.org", "rdap.arin.net", "rdap.db.ripe.net", "whois.ripe.net",
                "whois.arin.net", "data.iana.org", "www.peeringdb.com", "stat.ripe.net", "www.gstatic.com"}, (path.name, domain)


# ------------------------------------------------------------------ II03 RIPEstat


def test_ripestat_uses_the_five_data_calls_with_sourceapp_and_never_whois_or_abuse_contacts():
    calls = []
    h.fetch("ripestat", transport=ii.fixture_transport(h.pages("ripestat"), calls=calls))
    paths = [c["key"] for c in calls]
    assert {p.split("/")[2] for p in paths} == set(ii.RIPESTAT_CALLS)
    assert all("sourceapp=noesis" in p for p in paths)
    assert not any(forbidden in p for p in paths for forbidden in ii.FORBIDDEN_CALLS)
    per_resource = {}
    for p in paths:
        resource = re.search(r"resource=([^&]+)", p).group(1)
        per_resource[resource] = per_resource.get(resource, 0) + 1
    assert len(per_resource) <= 2 and max(per_resource.values()) <= 5
    assert all("Authorization" not in c["headers"] for c in calls)  # the PeeringDB key never goes to RIPEstat
    assert not set(ii.FORBIDDEN_CALLS) & set(ii.RIPESTAT_CALLS)
    for call in ii.FORBIDDEN_CALLS:
        with pytest.raises(ii.InfrastructureError) as caught:
            ii.parse_ripestat(b"{}", call=call, resource={"kind": "asn", "value": "AS64500"}, params={})
        assert caught.value.code == "call_forbidden"


def test_ripestat_is_paced_at_one_request_per_second_and_429_gets_exactly_one_retry():
    clock = {"t": 0.0}
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock["t"] += seconds

    pages = h.pages("ripestat")

    def live(*, url, params, headers, timeout, max_bytes=None):
        answer = dict(ii.fixture_transport(pages)(url=url, params=params, headers=headers, timeout=timeout))
        answer.pop("origin")  # behave like a live answer, so pacing applies
        clock["t"] += 0.2
        return answer

    h.fetch("ripestat", transport=live, sleep=sleep, clock=lambda: clock["t"])
    assert slept and all(s <= 1.0 for s in slept) and len(slept) == 5  # every request after the first waits

    answers = iter([{"status": 429, "headers": {"Retry-After": "2"}, "content": b""}])

    def limited(*, url, params, headers, timeout, max_bytes=None):
        nxt = next(answers, None)
        if nxt:
            return nxt
        return live(url=url, params=params, headers=headers, timeout=timeout)

    slept.clear()
    pages_out = h.fetch("ripestat", transport=limited, sleep=sleep, clock=lambda: clock["t"])
    assert pages_out and 2.0 in slept  # one retry after Retry-After

    def always_429(**_kwargs):
        return {"status": 429, "headers": {}, "content": b""}

    count = {"n": 0}

    def counted(**kwargs):
        count["n"] += 1
        return always_429(**kwargs)

    with pytest.raises(SourcePackError) as caught:
        h.fetch("ripestat", transport=counted, sleep=sleep, clock=lambda: clock["t"])
    assert caught.value.code == "rate_limited" and count["n"] == 2


def test_a_deprecated_or_changed_data_call_version_fails_the_unit_instead_of_reading_a_new_shape():
    with pytest.raises(SourcePackError) as caught:
        h.fetch("ripestat", revision="ripestat-deprecated")
    assert caught.value.code == "schema_drift" and "deprecated_data_call" in str(caught.value)
    key = "stat.ripe.net/data/routing-status/data.json?resource=AS64500&sourceapp=noesis"
    body = next(p for p in h.pages("ripestat") if p["request"] == key)["body"]
    changed = {**body, "version": "3.0"}
    with pytest.raises(SourcePackError) as caught:
        h.fetch("ripestat", transport=_transport_with("ripestat", {key: {"body": changed}}))
    assert "data_call_version_changed" in str(caught.value)
    undated = json.loads(json.dumps(body))
    undated["data"].pop("query_time")
    undated.pop("time")
    with pytest.raises(SourcePackError) as caught:
        h.fetch("ripestat", transport=_transport_with("ripestat", {key: {"body": undated}}))
    assert "never dated by retrieval" in str(caught.value)


def test_more_than_2000_announced_prefixes_is_budget_exhausted():
    key = "stat.ripe.net/data/announced-prefixes/data.json?resource=AS64500&sourceapp=noesis"
    body = json.loads(json.dumps(next(p for p in h.pages("ripestat") if p["request"] == key)["body"]))
    network = ipaddress.ip_network("2001:db8::/32")
    body["data"]["prefixes"] = [{"prefix": str(sub), "timelines": []}
                                for _, sub in zip(range(2001), network.subnets(new_prefix=48))]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("ripestat", transport=_transport_with("ripestat", {key: {"body": body}}))
    assert caught.value.code == "budget_exhausted"


def test_routing_observations_carry_ripestat_time_and_version():
    items = _items("ripestat")
    assert {i["record_type"] for i in items} == {"observation"}
    assert {i["data_call"]: i["data_call_version"] for i in items} == {
        "as-overview": "1.3", "announced-prefixes": "1.2", "routing-status": "2.1", "rpki-validation": "0.3",
        "prefix-overview": "1.9"}
    assert {i["stated_time"] for i in items} == {"2095-05-31T12:00:00Z"}
    rpki = next(i for i in items if i["data_call"] == "rpki-validation")
    # RIPEstat's RPKI status is kept as stated; it is never turned into a hijack or misconfiguration verdict.
    assert rpki["content"]["rpki_status_as_stated"] == "valid" and "verdict" not in json.dumps(rpki)


# ------------------------------------------------------------------ II04 PeeringDB


def test_peeringdb_reads_net_org_netixlan_ix_and_fac_with_depth_0_and_keeps_the_key_secret():
    calls = []
    records = [r for page in h.fetch("peeringdb", transport=ii.fixture_transport(h.pages("peeringdb"), calls=calls))
               for r in page]
    assert [c["key"].split("?")[0].split("/")[2] for c in calls] == ["net", "org", "netixlan", "ix", "ix", "fac"]
    assert all("depth=0" in c["key"] for c in calls)
    assert all(c["headers"]["Authorization"] == f"Api-Key {ii.FIXTURE_SECRET}" for c in calls)
    serialised = json.dumps(records)
    assert ii.FIXTURE_SECRET not in serialised  # never in a record, unit header, URL or receipt
    kinds = {r["ii_item"]["object_kind"] for r in records if r["ii_item"]["record_type"] == "registry-record"}
    assert kinds == {"net", "org", "netixlan", "ix", "fac"}
    for record in records:
        item = record["ii_item"]
        assert "poc" not in json.dumps(item) and "notes" not in item.get("content", {})
        if item["record_type"] == "registry-record":
            assert set(item["content"]) <= set(ii.PEERINGDB_FIELDS[item["object_kind"]])
            assert item["label"] == "the network's self-declaration in PeeringDB"
    for word in ("tech_email", "address1", "zipcode", "sales_email", "@"):
        assert word not in json.dumps([r["ii_item"] for r in records]), word
    adapter = h.adapter("peeringdb")
    assert adapter.describe()["internet_infrastructure"]["keyed"] is True
    assert ii.FIXTURE_SECRET not in json.dumps(adapter.describe())


def test_peeringdb_caps_and_poc_are_enforced():
    key = "www.peeringdb.com/api/netixlan?depth=0&net_id=9001"
    body = json.loads(json.dumps(next(p for p in h.pages("peeringdb") if p["request"] == key)["body"]))
    body["data"] = [dict(body["data"][0], id=8100 + i) for i in range(21)]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("peeringdb", transport=_transport_with("peeringdb", {key: {"body": body}}))
    assert caught.value.code == "budget_exhausted"
    poc = ii.peeringdb_record("net", {**next(p for p in h.pages("peeringdb") if "net?" in p["request"])["body"][
        "data"][0], "poc_set": [{"role": "Technical", "email": "noc@example.org"}]},
        {"kind": "asn", "value": "AS64500"})
    assert "poc_set" not in poc["content"]  # the allow-list never reads poc objects


def test_a_changed_updated_is_a_new_revision_and_deleted_or_404_is_removed_by_source():
    items = _items("peeringdb", revision="peeringdb", now_ms=h.SECOND_RETRIEVAL)
    by_kind = {(i["object_kind"], i.get("native_id")): i for i in items if i["record_type"] == "registry-record"}
    assert by_kind[("net", "9001")]["revision"]["source_revision"] == "2096-05-01T00:00:00Z"
    assert by_kind[("org", "7001")]["state"] == "removed_by_source"
    assert by_kind[("fac", "5001")]["state"] == "removed_by_source"
    assert by_kind[("fac", "5001")]["revision"]["basis"] == "not_found"


# ------------------------------------------------------------------ II05 RDAP


def test_parse_bootstrap_reads_asn_ranges_and_ip_prefix_keys_with_the_same_parser():
    asn = rdap.parse_bootstrap(json.dumps({"services": [[["64496-64511", "65536"], ["https://rdap.db.ripe.net/"]],
                                                        [["1-1876"], ["http://insecure.example.org/"]]]}).encode(),
                               "asn")
    assert asn == {"64496-64511": "https://rdap.db.ripe.net/", "65536-65536": "https://rdap.db.ripe.net/"}
    assert rdap.bootstrap_base("asn", "AS64500", asn) == "https://rdap.db.ripe.net/"
    v4 = rdap.parse_bootstrap(json.dumps({"services": [[["192.0.0.0/8"], ["https://rdap.arin.net/registry"]],
                                                       [["192.0.2.0/24"], ["https://rdap.db.ripe.net/"]]]}).encode(),
                              "ipv4")
    assert rdap.bootstrap_base("ipv4", "192.0.2.0/24", v4) == "https://rdap.db.ripe.net/"  # longest prefix
    v6 = rdap.parse_bootstrap(json.dumps({"services": [[["2001:db8::/32"], ["https://rdap.apnic.net/"]]]}).encode(),
                              "ipv6")
    assert v6 == {"2001:db8::/32": "https://rdap.apnic.net/"}
    with pytest.raises(Exception) as caught:
        rdap.bootstrap_base("asn", "AS1", asn)
    assert getattr(caught.value, "code", None) == "no_rdap_service"
    # The DNS file still reads exactly as the OSINT rdap-domain source reads it.
    assert rdap.parse_bootstrap(json.dumps({"services": [[["ORG."], ["https://rdap.example.org/"]]]}).encode()) == \
        {"org": "https://rdap.example.org/"}


def test_rdap_fetches_each_bootstrap_file_once_per_24_hours_and_caps_three_objects():
    cache = {}
    calls = []
    h.fetch("rdap", transport=ii.fixture_transport(h.pages("rdap"), calls=calls), bootstrap_cache=cache)
    boots = [c["key"] for c in calls if c["key"].startswith("data.iana.org")]
    assert boots == ["data.iana.org/rdap/asn.json", "data.iana.org/rdap/ipv4.json", "data.iana.org/rdap/dns.json"]
    calls.clear()
    h.fetch("rdap", transport=ii.fixture_transport(h.pages("rdap"), calls=calls), bootstrap_cache=cache,
            now_ms=h.FIRST_RETRIEVAL + 3_600_000)
    assert not [c for c in calls if c["key"].startswith("data.iana.org")]
    calls.clear()
    h.fetch("rdap", transport=ii.fixture_transport(h.pages("rdap"), calls=calls), bootstrap_cache=cache,
            now_ms=h.FIRST_RETRIEVAL + 25 * 3_600_000)
    assert len([c for c in calls if c["key"].startswith("data.iana.org")]) == 3
    assert len(h.adapter("rdap").declared["units"]) == ii.CAPS["rdap"]["objects"] == 3


def test_rdap_keeps_only_holder_organisation_country_rir_status_events_and_nameservers():
    autnum, network, domain = _items("rdap")
    for item in (autnum, network, domain):
        text = json.dumps(item)
        for leaked in ("Jane Example", "John Example", "jane@", "hostmaster@", "abuse@", "tel:", "Example Street",
                       "Abuse desk", "JD1-RIPE"):
            assert leaked not in text, leaked
        assert item["content"]["holder_organisation"] == "Example Networks Ltd (fictional)"
    assert (autnum["content"]["rir"], autnum["content"]["country"]) == ("RIPE NCC", "ZZ")
    assert network["content"]["start_address"] == "192.0.2.0"
    assert domain["content"]["nameservers"] == ["ns1.example.org", "ns2.example.org"]
    assert "registrar" not in domain["content"]


def test_a_transfer_between_rirs_is_recorded_as_the_stated_bootstrap_target():
    moved = _items("rdap", revision="rdap", now_ms=h.SECOND_RETRIEVAL)[0]
    assert (moved["content"]["bootstrap_target"], moved["content"]["rir"]) == ("https://rdap.arin.net/registry/",
                                                                               "ARIN")
    gone = ii.rdap_removed({"kind": "asn", "value": "AS64500"}, "https://rdap.db.ripe.net/")
    assert gone["state"] == "removed_by_source" and gone["revision"]["basis"] == "not_found"


def test_the_osint_rdap_domain_and_crt_sh_sources_are_unchanged():
    pack = json.loads((h.ROOT / "config/source_packs/osint.json").read_text())
    current = {s["source_id"]: provider_digest(s) for s in pack["sources"] if s["source_id"] in OSINT_SOURCE_DIGESTS}
    assert current == OSINT_SOURCE_DIGESTS
    assert rdap.SOURCE_ID == "rdap-domain" and crtsh.SOURCE_ID == "crt-sh"


# ------------------------------------------------------------------ II06 crt.sh and the CT log list


def test_crtsh_queries_one_exact_domain_and_keeps_issuance_facts_only():
    calls = []
    items = [r["ii_item"] for page in h.fetch("crtsh", transport=ii.fixture_transport(h.pages("crtsh"), calls=calls))
             for r in page]
    assert [c["key"] for c in calls] == ["crt.sh/?output=json&q=example.org"]
    assert [i["native_id"] for i in items] == ["70000001", "70000002", "70000003"]  # the S/MIME cert is not stored
    for item in items:
        assert set(item["content"]) == set(ii.CERTIFICATE_FIELDS)
        assert not any("@" in name for name in item["content"]["dns_sans"])
        assert "Jane" not in json.dumps(item) and "common_name" not in json.dumps(item)
        assert "revok" not in json.dumps(item)
    for refused, code in (("*.example.org", "wildcard_refused"), ("%.example.org", "wildcard_refused"),
                          ("192.0.2.1", "ip_lookup_refused"), ("hostmaster@example.org",
                                                               "person_identifier_refused")):
        with pytest.raises(ii.InfrastructureError) as caught:
            ii.normalize_domain(refused)
        assert caught.value.code == code


def test_more_than_200_certificates_is_budget_exhausted_never_truncated():
    rows = [{"id": 71000000 + i, "issuer_name": "C=ZZ, O=Fictional Trust Services", "name_value": "example.org",
             "not_before": "2096-01-01T00:00:00", "not_after": "2096-04-01T00:00:00",
             "entry_timestamp": "2096-01-01T00:00:00"} for i in range(201)]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("crtsh", transport=_transport_with("crtsh", {"crt.sh/?output=json&q=example.org": {"body": rows}}))
    assert caught.value.code == "budget_exhausted"
    assert len(ii.parse_crtsh_certificates(json.dumps(rows[:200]).encode(), domain="example.org")) == 200


def test_log_ids_are_kept_when_stated_and_subjects_keep_only_the_organisation():
    row = {"id": 72000001, "issuer_name": "C=ZZ, O=Fictional Trust Services", "name_value": "example.org",
           "not_before": "2096-01-01T00:00:00", "not_after": "2096-04-01T00:00:00",
           "entry_timestamp": "2096-01-01T00:00:00", "log_ids": ["RklDVElPTkFMLUxPRy1BLTIwOTQ="]}
    (item,) = ii.parse_crtsh_certificates(json.dumps([row]).encode(), domain="example.org")
    assert item["content"]["log_ids"] == ["RklDVElPTkFMLUxPRy1BLTIwOTQ="]
    assert ii.minimise_subject("C=ZZ, ST=Example, L=Exampleton, O=Example Networks Ltd, CN=example.org, "
                               "emailAddress=hostmaster@example.org") == "Example Networks Ltd"
    assert ii.minimise_subject("CN=Jane Example") is None


def test_the_ct_log_list_is_one_file_of_at_most_2_mb_and_a_state_change_is_a_new_revision():
    first = {i["native_id"]: i for i in _items("ct") if i["record_type"] == "registry-record"}
    second = {i["native_id"]: i for i in _items("ct", revision="ct", now_ms=h.SECOND_RETRIEVAL)
              if i["record_type"] == "registry-record"}
    log_a = "RklDVElPTkFMLUxPRy1BLTIwOTQ="
    assert (first[log_a]["content"]["state"]["name"], second[log_a]["content"]["state"]["name"]) == ("usable",
                                                                                                   "retired")
    assert second[log_a]["revision"] == {"basis": "log_list_version", "source_revision": "2097.1",
                                         "valid_from": "2097-01-02T00:00:00Z"}
    assert "ct-ops@" not in json.dumps(list(first.values()))  # operator e-mail addresses are never read
    huge = b'{"version": "2098.1", "log_list_timestamp": "2098-01-01T00:00:00Z", "operators": []}' + \
        b" " * 2_000_001
    with pytest.raises(ii.InfrastructureError) as caught:
        ii.parse_ct_log_list(huge)
    assert caught.value.code == "budget_exhausted"
    with pytest.raises(SourcePackError):
        h.fetch("ct", transport=_transport_with("ct", {"www.gstatic.com/ct/log_list/v3/log_list.json":
                                                       {"body": huge.decode()}}))


def test_direct_rfc6962_logs_and_caida_stay_not_implemented():
    for provider in ("rfc6962-logs", "caida"):
        assert ii.PROVIDER_CONTRACTS[provider]["status"] == "not-implemented"
        assert ii.LIVE_VERIFICATION[provider]["status"] == "not-implemented"
    item = json.loads(h.PACK.read_text())["sources"][0]
    item = json.loads(json.dumps(item))
    item["internet_infrastructure"]["provider"] = "rfc6962-logs"
    with pytest.raises(SourcePackError):
        ii.infrastructure_declaration({**item, "source_hash": "x"})


def test_declarations_refuse_undeclared_ip_keyed_person_keyed_and_wildcard_selections():
    base = h.source("rdap")
    for selection in ({"asn": "AS64500", "prefix": "192.0.2.1/32"}, {"domain": "*.example.org"},
                      {"domain": "hostmaster@example.org"}, {"asn": "AS64500", "ip": "192.0.2.1"},
                      {"domain": "JD1-RIPE"}):
        item = json.loads(json.dumps(base))
        item["internet_infrastructure"]["selection"] = selection
        with pytest.raises(SourcePackError):
            ii.InternetInfrastructureAdapter(item, transport=ii.fixture_transport([]))
    adapter = h.adapter("rdap")
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "selection", "parameters": {"domain": "other.example.org"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"

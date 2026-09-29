"""German statutory citation parsing into provision locators (#2105, FL06): table-driven, offsets kept."""

from __future__ import annotations

import pytest

from src.kb.legal_citations import (
    FEDERAL_STATUTE_SET,
    bgbl_key,
    implementation_statements,
    norm_path,
    parse_bgbl_references,
    parse_citations,
    parse_eu_act_references,
    path_contains,
    resolve_citation,
    statute_key,
)

EXTRA = [{"jurabk": "MPHG"}]

# (text, [(statute or None, status, [paths], version_hint)])
CASES = [
    ("§ 823 Abs. 1 BGB", [("BGB", "resolved", ["§823/abs1"], None)]),
    ("§ 823 Abs 1 BGB", [("BGB", "resolved", ["§823/abs1"], None)]),
    ("§ 823 Absatz 1 BGB", [("BGB", "resolved", ["§823/abs1"], None)]),
    ("§ 823 BGB", [("BGB", "resolved", ["§823"], None)]),
    ("§ 823 II BGB", [("BGB", "resolved", ["§823/abs2"], None)]),
    ("§ 280 Abs. 1 Satz 2 BGB", [("BGB", "resolved", ["§280/abs1/satz2"], None)]),
    ("§ 280 Abs. 1 S. 2 BGB", [("BGB", "resolved", ["§280/abs1/satz2"], None)]),
    ("§ 5 Abs. 2 Satz 1 Nr. 3", [(None, "no_statute", ["§5/abs2/satz1/nr3"], None)]),
    (
        "§ 5 Absatz 2 Satz 1 Nummer 3 WpHG",
        [("WpHG", "resolved", ["§5/abs2/satz1/nr3"], None)],
    ),
    (
        "§ 5 Abs. 2 Nr. 3 Buchst. a KWG",
        [("KWG", "resolved", ["§5/abs2/nr3/buchst-a"], None)],
    ),
    (
        "§ 5 Abs. 2 Nr. 3 lit. b KWG",
        [("KWG", "resolved", ["§5/abs2/nr3/buchst-b"], None)],
    ),
    ("§ 5 Abs. 2 Halbsatz 2 HGB", [("HGB", "resolved", ["§5/abs2/hs2"], None)]),
    ("§ 5 Abs. 2 Alt. 1 HGB", [("HGB", "resolved", ["§5/abs2/alt1"], None)]),
    ("§ 5a Abs. 1 GmbHG", [("GmbHG", "resolved", ["§5a/abs1"], None)]),
    ("§ 33a WpHG", [("WpHG", "resolved", ["§33a"], None)]),
    ("§§ 33 ff. WpHG", [("WpHG", "resolved", ["§33"], None)]),
    ("§ 33 ff. WpHG", [("WpHG", "resolved", ["§33"], None)]),
    ("§ 5 f. HGB", [("HGB", "resolved", ["§5"], None)]),
    ("§§ 5 bis 7 KWG", [("KWG", "resolved", ["§5"], None)]),
    ("§§ 5 - 7 KWG", [("KWG", "resolved", ["§5"], None)]),
    ("§§ 5, 7 und 9 AktG", [("AktG", "resolved", ["§5", "§7", "§9"], None)]),
    ("§§ 5 und 6 AktG", [("AktG", "resolved", ["§5", "§6"], None)]),
    ("Art. 20 Abs. 3 GG", [("GG", "resolved", ["art20/abs3"], None)]),
    ("Art 20 Abs 3 GG", [("GG", "resolved", ["art20/abs3"], None)]),
    ("Artikel 20 Absatz 3 GG", [("GG", "resolved", ["art20/abs3"], None)]),
    ("Art. 3 Abs. 1 GG", [("GG", "resolved", ["art3/abs1"], None)]),
    ("Art. 103 Abs. 1 GG", [("GG", "resolved", ["art103/abs1"], None)]),
    (
        "Art. 6 Abs. 1 UAbs. 1 Buchst. f DSGVO",
        [(None, "unresolved", ["art6/abs1/uabs1/buchst-f"], None)],
    ),
    (
        "§ 823 Abs. 2 BGB i.V.m. § 263 StGB",
        [
            ("BGB", "resolved", ["§823/abs2"], None),
            (None, "unresolved", ["§263"], None),
        ],
    ),
    (
        "§ 823 Abs. 2 BGB iVm § 5 Abs. 1 WpHG",
        [
            ("BGB", "resolved", ["§823/abs2"], None),
            ("WpHG", "resolved", ["§5/abs1"], None),
        ],
    ),
    (
        "§ 823 Abs. 2 BGB in Verbindung mit § 5 WpHG",
        [("BGB", "resolved", ["§823/abs2"], None), ("WpHG", "resolved", ["§5"], None)],
    ),
    (
        "§ 5 i.V.m. § 7 WpHG",
        [("WpHG", "resolved", ["§5"], None), ("WpHG", "resolved", ["§7"], None)],
    ),
    (
        "§ 5 und § 7 WpHG",
        [("WpHG", "resolved", ["§5"], None), ("WpHG", "resolved", ["§7"], None)],
    ),
    (
        "§ 4 InsO, § 13 ZPO, § 251 ZPO",
        [
            ("InsO", "resolved", ["§4"], None),
            ("ZPO", "resolved", ["§13"], None),
            ("ZPO", "resolved", ["§251"], None),
        ],
    ),
    ("§ 33 WpHG a.F.", [("WpHG", "resolved", ["§33"], "a.F.")]),
    ("§ 33 WpHG a. F.", [("WpHG", "resolved", ["§33"], "a.F.")]),
    ("§ 31 WpHG aF", [("WpHG", "resolved", ["§31"], "a.F.")]),
    ("§ 63 WpHG n.F.", [("WpHG", "resolved", ["§63"], "n.F.")]),
    ("§ 63 WpHG nF", [("WpHG", "resolved", ["§63"], "n.F.")]),
    ("§ 33 a.F. WpHG", [("WpHG", "resolved", ["§33"], "a.F.")]),
    (
        "§ 33 WpHG in der bis zum 2. Januar 2018 geltenden Fassung",
        [("WpHG", "resolved", ["§33"], "a.F.")],
    ),
    (
        "§ 63 WpHG in der ab dem 3.1.2018 geltenden Fassung",
        [("WpHG", "resolved", ["§63"], "n.F.")],
    ),
    ("§ 1 VwVfG", [("VwVfG", "resolved", ["§1"], None)]),
    ("§ 35 Satz 1 VwVfG", [("VwVfG", "resolved", ["§35/satz1"], None)]),
    ("§ 113 Abs. 1 Satz 4 VwGO", [("VwGO", "resolved", ["§113/abs1/satz4"], None)]),
    ("§ 5 Abs. 2 MPHG", [("MPHG", "resolved", ["§5/abs2"], None)]),
    ("vgl. § 12 FooG", [(None, "unresolved", ["§12"], None)]),
    ("§ 1 SGB V", [(None, "unresolved", ["§1"], None)]),
    ("§ 263 StGB", [(None, "unresolved", ["§263"], None)]),
    ("§ 1 UWG", [(None, "unresolved", ["§1"], None)]),
    ("gemäß § 5 Der Kläger", [(None, "no_statute", ["§5"], None)]),
    ("nach § 5 des Gesetzes", [(None, "no_statute", ["§5"], None)]),
    ("Paragraph ohne Zeichen", []),
    ("Startseite 5", []),
    ("eli/bund/bgbl-1/2030/45", []),
]


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0] for c in CASES])
def test_citation_forms(text, expected):
    found = parse_citations(text, statutes=EXTRA)
    assert [
        (
            c["statute"],
            c["status"],
            [p["path"] for p in c["provisions"]],
            c["version_hint"],
        )
        for c in found
    ] == expected
    for citation in found:
        # Offsets are exact: the raw text is the slice the citation covers.
        assert text[citation["start"] : citation["end"]] == citation["raw"]


def test_there_are_at_least_fifty_forms_and_unresolved_ones_stay_unresolved():
    assert len(CASES) >= 50
    unresolved = [c for c in CASES if any(item[1] != "resolved" for item in c[1])]
    assert len(unresolved) >= 8
    for text, _ in unresolved:
        for citation in parse_citations(text):
            if citation["status"] == "unresolved":
                assert citation["statute"] is None and citation["statute_raw"]
                assert "not in the bounded statute set" in citation["unresolved_reason"]


def test_ranges_following_and_chains_keep_their_shape_relation_and_basis():
    (rng,) = parse_citations("§§ 5 bis 7 KWG")
    assert rng["provisions"] == [{"path": "§5", "shape": "range", "to_path": "§7"}]
    (ff,) = parse_citations("§§ 33 ff. WpHG")
    assert ff["provisions"][0]["shape"] == "following"
    first, second = parse_citations("§ 5 i.V.m. § 7 WpHG")
    assert (
        first["statute_basis"] == "shared_in_chain"
        and second["statute_basis"] == "stated"
    )
    assert (
        second["relation"] == "in_conjunction_with"
        and first["chain"] == second["chain"]
    )
    (dated,) = parse_citations(
        "§ 33 WpHG in der bis zum 2. Januar 2018 geltenden Fassung"
    )
    assert dated["version_hint_date"] == "2018-01-02"


def test_offsets_in_running_text_and_context_statute():
    text = "Die Frist folgt aus § 5 Abs. 2 MPHG; ferner § 8a."
    found = parse_citations(text, statutes=EXTRA, context_statute="MPHG")
    assert [text[c["start"] : c["end"]] for c in found] == ["§ 5 Abs. 2 MPHG", "§ 8a"]
    assert found[1]["statute"] == "MPHG" and found[1]["statute_basis"] == "context"


def test_unknown_statutes_are_never_guessed_and_resolve_reports_status():
    assert resolve_citation("§ 12 FooG")["status"] == "unresolved"
    assert resolve_citation("§ 823 BGB, § 12 FooG")["status"] == "partially_resolved"
    assert resolve_citation("kein Zitat")["status"] == "no_citation"
    assert "in force" in resolve_citation("§ 823 BGB")["semantics"]


def test_statute_set_keys_and_paths_are_normalised_the_same_way():
    assert set(FEDERAL_STATUTE_SET) >= {
        "BGB",
        "HGB",
        "GmbHG",
        "AktG",
        "WpHG",
        "KWG",
        "VwVfG",
        "GG",
    }
    assert statute_key(" WpHG ") == statute_key("wphg") == "wphg"
    assert (
        norm_path("§ 5a") == "§5a"
        and norm_path("Art 20") == "art20"
        and norm_path("Artikel 3") == "art3"
    )
    assert norm_path("Inhaltsübersicht") is None
    assert (
        path_contains("§5", "§5/abs2")
        and not path_contains("§5", "§50")
        and not path_contains("§5/abs2", "§5")
    )


BGBL = [
    ("BGBl. 2023 I Nr. 411", "bgbl-1/2023/nr-411"),
    ("BGBl I 2030 Nr. 45", "bgbl-1/2030/nr-45"),
    ("BGBl. II 2031 Nr. 7", "bgbl-2/2031/nr-7"),
    ("vom 2. Januar 2002 (BGBl. I S. 42)", "bgbl-1/2002/s-42"),
    ("BGBl. I 2002, 42", "bgbl-1/2002/s-42"),
    ("Zuletzt geändert durch Art. 1 G v. 14.3.2030 I Nr. 45", "bgbl-1/2030/nr-45"),
    ("Neugefasst durch Bek. v. 2.1.2002 I 42", "bgbl-1/2002/s-42"),
    ("BGBl. I S. 2708", None),
]


@pytest.mark.parametrize(("text", "key"), BGBL)
def test_bgbl_references(text, key):
    (ref,) = parse_bgbl_references(text)
    assert ref["key"] == key and text[ref["start"] : ref["end"]] == ref["raw"]
    assert (
        bgbl_key("I", 2030, number=45)
        == bgbl_key(1, "2030", "45")
        == "bgbl-1/2030/nr-45"
    )


def test_eu_act_forms_and_implementation_statements():
    refs = parse_eu_act_references(
        "Richtlinie 2014/65/EU, Verordnung (EU) Nr. 596/2014, Verordnung (EU) 2017/1129, "
        "Richtlinie (EU) 2019/2034, Verordnung (EG) Nr. 1060/2009, eli/dir/2030/77/oj"
    )
    assert [r["celex"] for r in refs] == [
        "32014L0065",
        "32014R0596",
        "32017R1129",
        "32019L2034",
        "32009R1060",
        "32030L0077",
    ]
    text = (
        "Dieses Gesetz dient der Umsetzung der Richtlinie (EU) 2030/77 des Rates vom 3. Januar 2030 "
        "(ABl. L 5 vom 4.1.2030, S. 1). Es nennt auch die Verordnung (EU) Nr. 596/2014."
    )
    (statement,) = implementation_statements(text)
    assert (
        statement["celex"] == "32030L0077"
        and "Verordnung" not in statement["statement"]
    )
    assert (
        implementation_statements("Die Verordnung (EU) Nr. 596/2014 bleibt unberührt.")
        == []
    )

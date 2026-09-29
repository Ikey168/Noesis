# Public Procurement fixtures (authored, offline)

Every file here and every `tests/fixtures/source_packs/procurement-*.json` file
was **authored** for offline tests. None is a live capture. Buyers, suppliers,
identifiers and notices are fictional ("Fixture", "Nordlicht Fixture IT GmbH"),
LEIs are fictional values with valid ISO 17442 check digits, and URLs under
`*.example` do not exist. Passing tests on these fixtures is offline evidence
only and never live provider coverage (see
`docs/development/procurement-evidence/` for the live runs).

The shapes follow each provider's documented responses so the real native
adapters in `src/ingestion/procurement_providers.py` parse them:

| File | Provider shape | Scenarios |
| --- | --- | --- |
| `source_packs/procurement-ted.json` | TED API v3 `POST /v3/notices/search` response (`notices[]`, `totalNoticeCount`, `iterationNextToken`) with eForms fields requested by business-term id; per-lot values are arrays aligned with `BT-137-Lot` | two-lot contract notice with different selection criteria per lot, prior information notice, closed notice, two award notices (incumbency), iteration-token pagination |
| `procurement/ted-round2.json` | same | change notice (`BT-758`) that moves both lot deadlines and relaxes the lot 1 certificate to "or equivalent"; result notice with `BT-142 = clos-nw` (cancellation) |
| `source_packs/procurement-uk-fts.json` | Find a Tender OCDS 1.1 release packages with `links.next` | two-lot tender with lot-specific selection criteria and `amountGross`, award + contract release (supplier states `XI-LEI`), `contractAmendment` release |
| `procurement/uk-fts-round2.json` | same | `tenderAmendment` release extending the tender period (a revision of the same OCID) |
| `source_packs/procurement-uk-cf.json` | Contracts Finder OCDS search | below-threshold tender whose value states no VAT basis (`vat: unknown`) |
| `source_packs/procurement-sam.json` | SAM.gov Get Opportunities v2 (`opportunitiesData[]`, `totalRecords`) | total small-business set-aside solicitation (NAICS, PSC), sources-sought notice, award notice with UEI |
| `procurement/identities.json` | helper | the fictional LEIs used above |

Each `native_pages[].request` names the request the page answers (`token` for
TED, `url` + `params` for OCDS, `offset` for SAM). The SAM fixture transport
requires an `api_key` parameter; tests pass the placeholder
`FIXTURE_SECRET`, never a real key.

The source-pack manifest pins every first-round file by SHA-256 and the hash
of the normalised adapter output (`config/source_packs/procurement.json`);
editing a fixture or the mapping requires re-pinning both.

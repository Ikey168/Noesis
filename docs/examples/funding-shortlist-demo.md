# Funding & Grants — explained shortlist demo

> **Evidence: offline authored fixtures only.** Provider payloads come from `tests/fixtures/funding` (hand-written to mirror provider page shapes; identifiers and amounts are illustrative). They are **not** live captures and say nothing about calls that are open today. Live checks are recorded separately in `docs/development/funding-evidence/`.
>
> The applicant is **synthetic**. Eligibility is a requirements assessment against stated facts, not a funder decision; the match score orders options and is **not** a probability of funding. Nothing was submitted.

Regenerate with `python scripts/funding_demo.py --output docs/examples/funding-shortlist-demo.md` (as of 2026-09-25 09:00 UTC).

## Synthetic applicant

Three-person graduate team in Germany, university-affiliated, not yet incorporated, building an open-source search index (prototype). Needs EUR 60,000 over 12 months; prefers medium effort; timezone Europe/Berlin.
Unknown profile facts (never defaulted): applicant.company_age_months, applicant.establishment_country, applicant.sme, preferences.instruments, preferences.min_days_to_deadline, project.achievements, project.consortium_partner_countries, project.matching_funds_available.

## Shortlist

| Bucket | Opportunity | Provider | Status | Next deadline | Verdict | Score (coverage) | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| apply_now | [EXIST Business Start-up Grant](https://www.exist.de/EXIST/Navigation/EN/Start-up-grant/start-up-grant.html) | exist | rolling | rolling | eligible | 0.94 (64%) | medium |
| consider | [Open-source internet infrastructure for trustworthy search](https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/horizon-fixture-2026-01-01) | eu-ft | open | 2026-11-18T16:00:00.000+0000 | needs_clarification | 0.72 (91%) | high |
| consider | [NGI Zero Commons Fund](https://nlnet.nl/propose/) | nlnet | open | October 1st 2026 12:00 CEST | needs_clarification | 0.60 (64%) | low |
| watch | [EXIST Transfer of Research](https://www.exist.de/EXIST/Navigation/EN/Transfer-of-research/transfer-of-research.html) | exist | programme | none known | eligible | 1.00 (18%) | low |
| watch | [NGI Zero Commons Fund](https://nlnet.nl/commonsfund/) | nlnet | programme | none known | needs_clarification | 0.90 (73%) | low |
| watch | [Privacy-enhancing technologies for data spaces (two-stage)](https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/horizon-fixture-2026-01-02) | eu-ft | forthcoming | 2027-01-20T16:00:00.000+0000 | needs_clarification | 0.80 (91%) | low |
| watch | [EXIST-Gründungsstipendium](https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/BMWi/exist-gruendungsstipendium.html) | foerderdatenbank | not_an_opportunity | none known | needs_clarification | 0.40 (45%) | low |
| watch | [Gründerkredit – StartGeld (Fixture)](https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/KfW/startgeld-fixture.html) | foerderdatenbank | not_an_opportunity | none known | needs_clarification | 0.21 (73%) | low |
| excluded | [Quantum sensing pilot lines](https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/horizon-fixture-2025-02-03) | eu-ft | closed | none known | needs_clarification | 0.62 (73%) | low |

## Why each option is where it is

### EXIST Business Start-up Grant — apply_now

- **topic_fit** (unknown, weight 3): the source states no themes for this record
- **usable_funding** (1.00, weight 3): up to 3000 EUR per person-month × 3 × 12 months (upper bound; stated rates range from 1000, depending on applicant status) covers 100% of the stated need
- **deadline_feasibility** (1.00, weight 2): rolling submissions
- **application_effort** (0.60, weight 1): 6 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Status notes:** programme-level record; application windows are defined by calls
- **Next step:** Start a workspace; prepare the checklist and budget.

### Open-source internet infrastructure for trustworthy search — consider

- **topic_fit** (0.67, weight 3): matches project theme 'open source search'; matches project theme 'internet infrastructure'
- **usable_funding** (1.00, weight 3): up to 5000000 EUR per project covers 100% of the stated need; minimum award 4000000 exceeds the stated need
- **deadline_feasibility** (0.60, weight 2): 54 days until 2026-11-18T16:00:00.000+0000 (estimated high-effort preparation: 45 days)
- **application_effort** (0.00, weight 1): 2 requirements, 1 stage(s), consortium required; exceeds your maximum effort (medium)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Needs clarification** (eu-ft:HORIZON-FIXTURE-2026-01-01:cond-0): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (eu-ft:HORIZON-FIXTURE-2026-01-01:cond-1): no machine rule or approved interpretation for this requirement text
- **Next step:** clarify 2 requirement(s) with the funder's text or a reviewed interpretation

### NGI Zero Commons Fund — consider

- **topic_fit** (unknown, weight 3): the source states no themes for this record
- **usable_funding** (0.83, weight 3): up to 50000 EUR per project covers 83% of the stated need
- **deadline_feasibility** (0.00, weight 2): 6 days left; below estimated low-effort preparation of 7 days
- **application_effort** (1.00, weight 1): 3 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (0.70, weight 1): obligation: open licensing of results
- **Needs clarification** (nlnet:european-dimension): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (nlnet:eligible-applicants): no machine rule or approved interpretation for this requirement text
- **Next step:** clarify 2 requirement(s) with the funder's text or a reviewed interpretation

### EXIST Transfer of Research — watch

- **topic_fit** (unknown, weight 3): the source states no themes for this record
- **usable_funding** (unknown, weight 3): instrument kind is unknown; usable cash cannot be assessed
- **deadline_feasibility** (unknown, weight 2): no upcoming deadline is known
- **application_effort** (1.00, weight 1): 3 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Status notes:** programme-level record; application windows are defined by calls
- **Next step:** Monitor for an opening or dated round.

### NGI Zero Commons Fund — watch

- **topic_fit** (1.00, weight 3): matches project theme 'open source search'; matches project theme 'privacy-enhancing technologies'; matches project theme 'internet infrastructure'
- **usable_funding** (0.83, weight 3): up to 50000 EUR per project covers 83% of the stated need
- **deadline_feasibility** (unknown, weight 2): no upcoming deadline is known
- **application_effort** (1.00, weight 1): 2 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (0.70, weight 1): obligation: open licensing of results
- **Needs clarification** (nlnet:european-dimension): no machine rule or approved interpretation for this requirement text
- **Status notes:** programme-level record; application windows are defined by calls
- **Next step:** Monitor for an opening or dated round.

### Privacy-enhancing technologies for data spaces (two-stage) — watch

- **topic_fit** (0.33, weight 3): matches project theme 'privacy-enhancing technologies'
- **usable_funding** (1.00, weight 3): up to 3000000 EUR per project covers 100% of the stated need
- **deadline_feasibility** (1.00, weight 2): 117 days until 2027-01-20T16:00:00.000+0000 (estimated low-effort preparation: 7 days)
- **application_effort** (1.00, weight 1): 0 requirements, 2 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Needs clarification** (unparsed): the call revision states no applicant/project requirements
- **Next step:** Monitor for an opening or dated round.

### EXIST-Gründungsstipendium — watch

- **topic_fit** (0.00, weight 3): no project theme matches call themes/title
- **usable_funding** (unknown, weight 3): award size per applicant is not stated
- **deadline_feasibility** (unknown, weight 2): no upcoming deadline is known
- **application_effort** (1.00, weight 1): 2 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Needs clarification** (foerderdatenbank:Bund/BMWi/exist-gruendungsstipendium:berechtigte): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (foerderdatenbank:Bund/BMWi/exist-gruendungsstipendium:gebiet): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (not_an_opportunity): directory listings and awards are not application calls
- **Status notes:** directory listing; resolve the administering body's current call
- **Next step:** Follow the link to the administering body's current call.

### Gründerkredit – StartGeld (Fixture) — watch

- **topic_fit** (0.00, weight 3): no project theme matches call themes/title
- **usable_funding** (0.00, weight 3): loan support is not a cash grant and is not counted as usable funding
- **deadline_feasibility** (unknown, weight 2): no upcoming deadline is known
- **application_effort** (1.00, weight 1): 2 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (0.70, weight 1): obligation: repayment
- **Needs clarification** (foerderdatenbank:Bund/KfW/startgeld-fixture:berechtigte): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (foerderdatenbank:Bund/KfW/startgeld-fixture:gebiet): no machine rule or approved interpretation for this requirement text
- **Needs clarification** (not_an_opportunity): directory listings and awards are not application calls
- **Status notes:** directory listing; resolve the administering body's current call
- **Next step:** Follow the link to the administering body's current call.

### Quantum sensing pilot lines — excluded

- **topic_fit** (0.00, weight 3): no project theme matches call themes/title
- **usable_funding** (1.00, weight 3): up to 10000000 EUR per project covers 100% of the stated need; minimum award 5000000 exceeds the stated need
- **deadline_feasibility** (unknown, weight 2): no upcoming deadline is known
- **application_effort** (1.00, weight 1): 0 requirements, 1 stage(s)
- **cash_flow** (unknown, weight 1): co-financing and payment timing are not stated
- **obligations** (1.00, weight 1): no repayment, dilution or licensing obligation stated
- **Needs clarification** (unparsed): the call revision states no applicant/project requirements
- **Status notes:** provider states the call is closed
- **Next step:** Not applicable: closed

## Preparation workspace — EXIST Business Start-up Grant

Checklist derived from the cited call revision (status as created):

- [ ] (requirement) The university or research institution must support the start-up project and host the team for the funding period.
- [ ] (requirement) Students, graduates and researchers are eligible and can apply individually or in teams of up to three persons.
- [ ] (requirement) Students, graduates and researchers are eligible and can apply individually or in teams of up to three persons.
- [ ] (requirement) The start-up must not yet have been founded; support is provided before the company is founded.
- [ ] (requirement) The application must be submitted through the start-up service of the university or research institution to Projektträger Jülich.
- [ ] (requirement) Funding is provided for up to 12 months.
- [ ] (document) Review guideline: Funding guideline (PDF)

Draft application (authored report, editable):

- Unanswered items left explicit: project.achievements
- Budget: total 60000.00 EUR, eligible 58000.00, requested 58000.00, co-financing 2000.00 (calculation receipt `quantitative-calculation:3819aae6139289b50f8df5e9`)
- Budget issue: Cost category 'Office rent' is not listed as eligible.

No application was submitted and no funder was contacted.

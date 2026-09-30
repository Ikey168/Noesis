# Astronomy and Space: space-object registration, operators and re-entries

Tracking issue: #2224. This guide covers the optional
`astronomy-space-object-registration` and `astronomy-discos` features of the
Astronomy and Space pack (`packs/astronomy/`, provider
`astronomy.space-object-registration`). Both are **off by default**. The
source contracts, licences and bounds are in the
[audit](../development/astronomy-evidence/space-object-registration-audit.md).

**What it answers:** given an object (COSPAR designator, NORAD number,
`discos:<id>` or a name) and a date, which State registered it with the UN and
in which document (symbol, paragraph locator, language, verbatim quotation),
which State supervises it after any transfer, its status as notified or as the
UNOOSA index states it, who operates or owns it *as published*, its SATCAT/GCAT
catalogue status, and every published re-entry prediction and the confirmed
report. **What it never does:** predict a re-entry or collision, compute a
window or footprint, reconcile registered orbital values with catalogue
elements, or attribute an object to an operator, owner or State beyond a
published registration or operator assertion. "No UN registration on record"
is never "unregistered".

## Sources

| Source | Connector format | Access | Stored |
| --- | --- | --- | --- |
| UNOOSA Online Index | `unoosa-index-json` | public | index entries keyed by COSPAR with the registration document symbol; status with the index retrieval date (changes are revisions) |
| UN registration documents and notifications (ST/SG/SER.E) | `unoosa-registration-text` | public | one entry per numbered paragraph: symbol, locator, language, verbatim text, registered values; transfers, status changes and re-entry notices as dated entries |
| ESA DISCOS | `discos-json` | **account token** `NOESIS_ESA_DISCOS_TOKEN` | permitted subset (identifiers, name, class, operator attribution, re-entry epoch) or citation only; never redistributed |
| The Aerospace Corporation re-entries | `aerospace-reentry-html` | public | every prediction as a dated revision; the confirmed report supersedes without deleting |

All sources run through the `astronomy-and-space` source pack (1.1.0, connector
`astronomy-registration`) and are `unverified-live` until a dated live run
(SO14, #2461).

## Journey

1. Enable the feature (`astronomy-space-object-registration`; add
   `astronomy-discos` only with a configured DISCOS account) and run the
   source pack. `space_object_registration_status` lists each source's access
   decision and `LIVE_VERIFICATION`; `discos_access_status` says whether DISCOS
   is configured.
2. `match_space_registration_objects` links records to SATCAT/GCAT objects on
   exact COSPAR/NORAD agreement with evidence; identifier conflicts and
   name-only registrations become candidates, reviewed with
   `review_astronomy_identity_match`. Nothing is merged.
3. `match_space_registration_parties` offers registering States,
   intergovernmental registrants and operators to canonical entities (and to
   Corporate Ownership legal entities by a published LEI) through ownership
   identity; review with `review_space_registration_party`.
4. `link_space_registration_citations` links registrations to Legal works by
   the instrument identifier the document names and to Science papers by DOI
   or bibcode; absent packs are skipped.
5. `object_registration_as_of` answers with citations, identity matches and
   the sources consulted; `export_space_registration_evidence` exports it as
   an evidence bundle (restricted DISCOS values are cited only).
   `reentry_record` lists predictions and the confirmed report per publisher;
   `project_reentry_locations` projects a published point into Geospatial.
6. `create_space_registration_monitor` watches an object, operator or
   registering State; `run_space_registration_monitor` emits
   `registration_published`, `status_changed`, `supervision_transferred`,
   `operator_changed`, `reentry_predicted` and `reentry_confirmed` events citing
   the new revision.

## Evidence

Offline evidence: `tests/unit/domains/test_astronomy_registration_acceptance.py`
and `tests/unit/astronomy/test_astronomy_registration_*.py` replay authored,
fictional fixtures (`tests/fixtures/astronomy_registration/`) with sockets
blocked. Live evidence is not claimed here; it belongs to SO14 and is recorded
separately under `docs/development/astronomy-evidence/`.

# Aircraft and vessel movements (OSINT `movements` feature)

Tracking: #2221. The OSINT pack's optional `movements` feature (default off,
provider `osint.movements`) answers one question: given an aircraft (ICAO
24-bit address or registration) or a vessel (IMO, MMSI or GFW vessel id) and a
bounded window, what did the registries and the movement sources publish, and
where did their receivers have coverage? Absence of positions is reported as
**"no coverage observed"**, never as absence of movement.

Every source has a recorded licence, access and volume decision in
[`docs/security/osint-movements-access.md`](../security/osint-movements-access.md);
nothing else is acquired. There is no real-time tracking, no continuous ADS-B
or AIS mirroring, no area query, no movement alert and no targeting use.

## Enabling it

1. Select the feature in the OSINT composition (`features: ["movements"]`).
2. Accept the licences of the `movements-*` sources of the
   `bounded-public-osint` source pack (1.1.0) and configure the secrets they
   name: `NOESIS_GFW_API_TOKEN` (shared with the Fisheries pack),
   `NOESIS_KYSTDATAHUSET_TOKEN`, optionally `NOESIS_OPENSKY_CREDENTIAL`.
   Declare the identifiers to acquire in each source's `osint_movements.selection`
   (one identifier and window per entry) and any LADD/PIA or operator opt-outs
   in `privacy_refusals`. Run the sources explicitly; their schedule is left
   disabled so nothing polls in the background.
3. Serve the tools: `NOESIS_OSINT_MOVEMENTS=on` serves `movement_registry`;
   `movement_window` and `movement_calls` additionally need the OSINT review
   gate (`NOESIS_OSINT_GATED_TOOLS=on`) and the `knowledge:osint:movements`
   scope. `movement_source_contracts` is always served.

## What is stored (`src/osint/movements.py`)

`noesis-osint-movement-record-v1` revisions: registry records (every change a
dated revision, deregistration a revision), aircraft and vessel identity
statements, sample windows (with the declared bound, licence decision,
coverage status, samples published and stored, `thinned` and the gaps),
position samples (always inside a window) and calls (`source-published` with
the source's confidence, or `derived`). UNCTAD port-call statistics are stored
as aggregates only. GFW vessel identity is **not** re-acquired: it is the
Fisheries pack's record and is cited from its store.

## Working through a question

```python
from src.osint.movements import MovementIdentity, MovementLinks, MovementQueries, derive_calls

derive_calls(conn, "osint", facilities_namespace="facilities", principal_id=me, scopes=scopes)
identity = MovementIdentity(conn)
identity.propose("osint", principal_id=me, scopes=scopes, fisheries_namespace="global")
identity.review("osint", candidate_id, "accept", "IMO and MMSI stated together by GFW", principal_id=me,
                scopes=scopes)
MovementLinks(conn).link("osint", principal_id=me, scopes=scopes, sanctions_namespace="legal",
                         fisheries_namespace="global")
answer = MovementQueries(conn).window("osint", "IMO 9000027", "2025-03-01", "2025-03-31", scopes=scopes,
                                      facilities_namespace="facilities", fisheries_namespace="global",
                                      sanctions_namespace="legal")
bundle = MovementQueries(conn).export_bundle(answer)
```

- **Derived calls** come from consecutive samples inside an airport or port
  polygon of the geospatial store for at least the dwell threshold (5 minutes,
  2 hours), each citing its samples and the geospatial relation receipts. A
  call inside or next to a coverage gap (including a window edge) is
  `uncertain`; no call is asserted or denied for a window without coverage.
- **Identity** pairings (registration and ICAO address, IMO and MMSI, GFW id)
  are exact, time-bounded candidates in the shared ownership identity review
  (`movements:` keys). MMSI reuse and re-flagging stay separate; overlapping
  disagreements are marked `competing`. Only accepted matches whose period
  overlaps the window join records. Organisation registrants may be matched to
  `canonical_entities`; natural persons never are.
- **Sanctions** links exist only where a listing states the IMO number,
  registration (tail number) or MMSI; the list revision and snapshot are cited
  and the statement follows the list over time (listed, not listed in the
  snapshot with the delisting, unknown between snapshots). A similar name is a
  candidate that cannot be accepted. No evasion, screening or compliance
  verdict is derived from movements.
- **Monitors** (`src/osint/movement_monitoring.py`) watch registry revisions,
  reviewed identity matches and listing revisions for at most 25 identifiers;
  position and call subscriptions are refused.

## Refusals

| Code | When |
| --- | --- |
| `person_identifier_refused` | the question is keyed on a name, e-mail, handle, phone or `person:` id |
| `privacy_opt_out` | the identifier (or one connected to it) is on the refusal list (LADD, PIA, withheld entry, operator declaration) |
| `private_aircraft_refused` | the aircraft's registry record names a natural person |
| `over_bound` | the window exceeds 92 days, or a source selection exceeds its MV01 bound |
| `purpose_required` | a position-bearing tool is called without a purpose |
| `movement_subscription_refused` | a monitor asks for positions, calls or movements |

## Evidence status

The offline journey
(`tests/unit/domains/test_osint_movements_acceptance.py`) replays authored,
fictional fixtures for every source with sockets blocked; it is **offline
evidence only**. Every provider is `unverified-live` in
`src/ingestion/osint_movement_sources.py` (`LIVE_VERIFICATION`) until the
dated live validation (#2291) is recorded separately; request paths and field
names marked *verify* in the access decision are authored from public
documentation.

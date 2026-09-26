# Economic and market dashboard

`get_economic_market_dashboard` and `POST /api/v1/market/macro-dashboard`
compose retained economic vintages with an explicit release calendar, initial
versus latest snapshots, revision comparisons, optional timestamped consensus
surprises, and dated equal-weight market breadth.

Every request supplies a release cutoff and an acquisition cutoff. The
dashboard keeps those clocks separate: a value that was released by the
selected date but acquired later is not silently used. Calendar rows preserve
provider-vintage, release, retrieval, and basis/status fields.

Consensus surprises are available only when each estimate has public and
retrieval timestamps plus current entitlement-backed source references. Without
those inputs the response explicitly says why the surprise is unavailable.
Breadth is optional and uses retained daily bars for a dated universe. It
reports missing listings, insufficient history, and source errors rather than
coercing calendars or currencies.

The fixture tests cover same-cutoff snapshot reuse, initial/latest revision
comparison, unavailable consensus, entitlement-backed surprises, and the
contract. Live provider coverage is reported by market readiness and is not
implied by a local dashboard result.

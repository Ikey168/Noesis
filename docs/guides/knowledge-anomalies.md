# Knowledge anomalies and alerts

Anomaly watches define a stable signal identity, domain scope, baseline window,
detector and version, threshold, and notification policy. Supported signal
families cover quantitative metrics, event rates, graph structure, source
behavior, evidence coverage, and narrative shifts. Invalid and sparse baselines
are explicit rather than silently treated as normal observations.

Detector runs are bounded, cancellable, and keyed by the watch, committed
generation, and an input hash. Replaying identical observations returns the
same receipt and anomaly identities. Baseline previews and simulations do not
persist detections. Missing observations and late arrivals remain visible in
the result rather than being imputed without provenance.

Correlation ranks temporally coincident document deltas, events, claims,
outages, methodology changes, and cross-domain signals. Every candidate is
labelled `plausible_not_proven`; Noesis does not promote correlation into a
causal assertion.

Alert identities include a grouping key, subscriber, and deduplication window.
Notification policy supports quiet-period suppression, replay-safe duplicate
handling, failure retries, cancellation, acknowledgment, resolution, and
reopening. MCP scopes separate reading (`knowledge:anomalies:read`), watch and
correlation writes (`knowledge:anomalies:write`), detector execution
(`knowledge:anomalies:execute`), and alert delivery
(`knowledge:anomalies:deliver`).

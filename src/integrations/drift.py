"""Replayable ADWIN simulation; source observations are the persisted state."""

from .common import IntegrationError, finite, receipt


def detect_drift(observations, *, delta=0.002, clock=32):
    from river.drift import ADWIN

    delta = finite(delta, "delta", 1e-9, 0.5)
    if type(clock) is not int or not 1 <= clock <= 1000 or len(observations) > 10000:
        raise IntegrationError("input_limit", "Invalid ADWIN clock or stream size")
    detector = ADWIN(delta=delta, clock=clock)
    seen = {}
    events = []
    missing = 0
    previous = None
    latest = False
    for observation in observations:
        identity = str(observation["id"])
        timestamp = int(observation["timestamp_ms"])
        if identity in seen:
            if seen[identity] != observation:
                raise IntegrationError(
                    "conflicting_observation", "Duplicate ID has changed value"
                )
            continue
        if previous is not None and timestamp < previous:
            raise IntegrationError(
                "late_observation", "Reorder and replay late events explicitly"
            )
        previous = timestamp
        seen[identity] = dict(observation)
        latest = False
        if observation.get("missing") or observation.get("value") is None:
            missing += 1
            continue
        value = finite(observation["value"], "stream value")
        detector.update(value)
        latest = bool(detector.drift_detected)
        if latest:
            events.append(
                {"id": identity, "timestamp_ms": timestamp, "width": detector.width}
            )
    return receipt(
        "river-adwin",
        "river",
        {"observations": list(seen.values()), "delta": delta, "clock": clock},
        {
            "detected": latest,
            "events": events,
            "width": detector.width,
            "estimate": detector.estimation,
            "missing": missing,
            "unique_observations": len(seen),
            "state_policy": "replay ordered source observations; no untrusted pickle",
        },
    )

"""Protocol readiness is independent of fixture and credential availability."""

from src.ingestion.europepmc_api import is_europepmc
from src.ingestion.scholarly_api import provider


def protocol_status(source):
    name = provider(source)
    if is_europepmc(source):
        name = "europepmc"
    from src.ingestion.guardian_api import is_guardian

    if is_guardian(source):
        name = "guardian"
    return {
        "adapter_version": "native-guardian-v1"
        if name == "guardian"
        else "native-scholarly-v1"
        if name
        else "generic-get-v1",
        "native_mapping": "implemented" if name else "unsupported",
        "live_verification": "not_checked",
        "fixture_conformance": "reported_separately",
        "ready": False,
        "reason": "live verification must be supplied independently"
        if name
        else "dedicated provider request and response mapping required",
    }

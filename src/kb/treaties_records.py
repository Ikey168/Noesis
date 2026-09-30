"""Treaty, participant and treaty-action records of the Legal pack's treaties provider (#2581, TR02).

``noesis-treaty-record-v1`` records arrive from :mod:`src.ingestion.treaties_sources`
(connector ``treaties``) and are kept as immutable revisions by
:mod:`src.kb.treaties_store`:

* a **treaty** record keeps the depositary, registration and catalogue
  identifiers the source publishes (UNTC chapter and treaty number, UNTS
  registration number, CELEX and ELI, CETS/ETS number), the title as published,
  the adoption place and date and the entry-into-force conditions and date as
  published, footnotes verbatim with their anchors, the language expressions
  (CELLAR) and the EU acts CELLAR links to the agreement (as citations);
* a **treaty-action** record keeps the participant as published (a state, an
  international organisation or the EU - never a natural person), the action
  type (and the label the source used), the action, deposit and effective
  dates as published and the verbatim text of reservations, declarations,
  objections and withdrawals with their source anchors.

A depositary correction, a changed status page or an action the source no
longer lists is a new revision; nothing is overwritten or deleted.

Minimisation (TR01, ``MINIMISATION``): treaty sources name states and
organisations. No field that would name or describe a natural person
(signatory, representative, contact details) is stored; official texts of
reservations, declarations, objections and footnotes are stored verbatim as the
depositary published them and are never parsed into person records.
:func:`check_minimisation` enforces this at write time.

Nothing here is legal advice, infers obligations or compliance, or interprets
the legal effect of a reservation; treaty texts are linked, never stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.legal import READ_SCOPE, WRITE_SCOPE, legal_feature_enabled

REVIEW_SCOPE = "knowledge:legal:review"
RECORD_CONTRACT = "noesis-treaty-record-v1"
ANSWER_CONTRACT = "noesis-treaty-answer-v1"
DEFAULT_NAMESPACE = "global"
PROVIDERS = ("untc", "cellar", "coe-treaty-office")
# Each source's coverage is a separate optional Legal feature (TR11).
FEATURES = {"untc": "treaties-untc", "cellar": "treaties-eu", "coe-treaty-office": "treaties-coe"}
RECORD_KINDS = ("treaty", "treaty-action")
PARTICIPANT_KINDS = ("state", "international-organisation", "regional-economic-integration-organisation")
ACTION_TYPES = (
    "signature", "definitive-signature", "ratification", "acceptance", "approval", "accession", "succession",
    "formal-confirmation", "reservation", "declaration", "declaration-or-reservation", "objection", "withdrawal",
    "denunciation", "entry-into-force", "communication",
)
CONSENT_TYPES = ("definitive-signature", "ratification", "acceptance", "approval", "accession", "succession",
                 "formal-confirmation")
STATEMENT_TYPES = ("reservation", "declaration", "declaration-or-reservation", "objection", "communication")
EXIT_TYPES = ("withdrawal", "denunciation")
# TR01 minimisation: keys no record or answer may carry.
NEVER_STORED = frozenset({"signatory", "signatory_name", "representative", "representative_name", "person",
                          "person_name", "contact", "email", "telephone", "phone", "address", "date_of_birth"})
# TR11 exclusions: keys no answer may carry (obligations, compliance, legal effect, advice).
FORBIDDEN_KEYS = frozenset({"obligation", "obligations", "compliance", "compliant", "is_compliant", "bound_by",
                            "is_bound", "legally_bound", "legal_effect", "effect_of_reservation", "legal_advice",
                            "advice", "interpretation", "assessment"})
NOTICE = ("Treaty actions as the depositary or publisher published them, with the record revision used. This is not "
          "legal advice: no obligation, compliance or legal effect of a reservation is inferred, and treaty texts "
          "are linked, not reproduced.")
EXCLUSIONS = ["no legal advice", "no inference of obligations or compliance",
              "no interpretation of the legal effect of reservations, declarations or objections",
              "no treaty-text redistribution beyond what each source licenses (texts are linked)",
              "no natural-person data (signatories and representatives are not stored)"]


class TreatiesError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise TreatiesError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
    return match.group(1) if match else None


def slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return text or "unnamed"


def treaty_key(provider: str, native_id: str) -> str:
    short = {"coe-treaty-office": "coe"}.get(provider, provider)
    return f"treaties:treaty:{short}:{native_id}"


def participant_key(provider: str, name: str) -> str:
    short = {"coe-treaty-office": "coe"}.get(provider, provider)
    return f"treaties:participant:{short}:{slug(name)}"


def action_key(provider: str, native_treaty: str, participant: str, action_type: str, qualifier: str) -> str:
    short = {"coe-treaty-office": "coe"}.get(provider, provider)
    return f"treaties:action:{short}:{native_treaty}:{slug(participant)}:{action_type}:{qualifier}"


def _paths(value: Any, keys: frozenset[str], path: str = "") -> list[str]:
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in keys:
                found.append(f"{path}/{key}")
            found += _paths(item, keys, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _paths(item, keys, f"{path}/{index}")
    return found


def forbidden_keys(value: Any) -> list[str]:
    """Paths of any obligation, compliance, legal-effect or advice key in an answer (TR11 exclusions)."""
    return _paths(value, FORBIDDEN_KEYS)


def minimisation_violations(value: Any) -> list[str]:
    """Paths of any natural-person field (TR01 minimisation decision)."""
    return _paths(value, NEVER_STORED)


def check_minimisation(record: Mapping[str, Any]) -> None:
    """Refuse a record that carries a natural-person field or a participant that is not a state or organisation."""
    found = minimisation_violations(record)
    if found:
        raise TreatiesError("minimisation_violation", f"record carries natural-person fields: {', '.join(found)}")
    if record.get("record_kind") == "treaty-action":
        participant = dict(dict(record.get("fields") or {}).get("participant") or {})
        if participant.get("kind") not in PARTICIPANT_KINDS:
            raise TreatiesError("minimisation_violation", "a treaty-action participant is a state, an international "
                                                          "organisation or the EU")


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(record)
    if record.get("contract") != RECORD_CONTRACT or record.get("record_kind") not in RECORD_KINDS \
            or record.get("provider") not in PROVIDERS:
        raise TreatiesError("invalid_record", "not a noesis-treaty-record-v1 record")
    if not str(record.get("record_key") or "").startswith(("treaties:treaty:", "treaties:action:")):
        raise TreatiesError("invalid_record", "record keys are treaties:treaty:... or treaties:action:...")
    if not str(record.get("locator") or "").startswith("https://"):
        raise TreatiesError("invalid_record", "a record has an HTTPS locator")
    if record["record_kind"] == "treaty-action" and dict(record["fields"]).get("action_type") not in ACTION_TYPES:
        raise TreatiesError("invalid_record", f"action_type is one of {ACTION_TYPES}")
    check_minimisation(record)
    return record


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether an optional Legal treaties feature is selected in the active composition plan."""
    if feature not in FEATURES.values():
        raise TreatiesError("invalid_feature", f"feature is one of {sorted(FEATURES.values())}")
    return legal_feature_enabled(conn, feature)


__all__ = [
    "ACTION_TYPES",
    "ANSWER_CONTRACT",
    "CONSENT_TYPES",
    "DEFAULT_NAMESPACE",
    "EXCLUSIONS",
    "EXIT_TYPES",
    "FEATURES",
    "FORBIDDEN_KEYS",
    "NEVER_STORED",
    "NOTICE",
    "PARTICIPANT_KINDS",
    "PROVIDERS",
    "READ_SCOPE",
    "RECORD_CONTRACT",
    "RECORD_KINDS",
    "REVIEW_SCOPE",
    "STATEMENT_TYPES",
    "WRITE_SCOPE",
    "TreatiesError",
    "action_key",
    "authorize",
    "canonical",
    "check_minimisation",
    "day",
    "digest",
    "feature_enabled",
    "forbidden_keys",
    "load",
    "minimisation_violations",
    "participant_key",
    "slug",
    "table_exists",
    "treaty_key",
    "validate_record",
]

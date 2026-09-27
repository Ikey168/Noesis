"""Private applicant and funding-project profiles.

A profile is owned by exactly one principal inside one namespace. Applicant
facts, project facts and preferences are kept in separate sections, every fact
carries its own effective dates, evidence and review state, and nothing is
defaulted: a fact that has not been stated is *unknown*, never ``False``.

Privacy is stricter than most knowledge stores: even ``operator`` scope does
not read another principal's profile, and a withdrawn profile is unreadable
(including its history), so assessments pinned to it become unavailable.
Profiles never enter public source records; see
:data:`src.kb.funding_records.PRIVATE_MARKERS`.
"""

from __future__ import annotations

import json
import re
import time

from src.kb.funding_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    FundingRecordError,
    canonical,
    digest,
    money,
)

CONTRACT = "noesis-funding-profile-v1"
SECTIONS = ("applicant", "project", "preferences")
_ISO2 = re.compile(r"^[A-Z]{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _country(value):
    return isinstance(value, str) and bool(_ISO2.fullmatch(value))


def _countries(value):
    return isinstance(value, list) and all(_country(v) for v in value)


def _strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _money(value):
    if not isinstance(value, dict) or set(value) != {"amount", "currency"}:
        return False
    try:
        money(value["amount"])
    except FundingRecordError:
        return False
    return isinstance(value["currency"], str) and bool(re.fullmatch(r"[A-Z]{3}", value["currency"]))


def _choice(*allowed):
    return lambda value: value in allowed


# fact key -> (section, validator, description). Unlisted keys must use the
# ``custom.`` prefix and are never read by built-in rules.
FACTS = {
    "applicant.kind": ("applicant", _choice(
        "individual", "informal-team", "company", "nonprofit", "university",
        "research-institution", "public-body", "consortium",
    ), "legal status of the applicant"),
    "applicant.residence_country": ("applicant", _country, "ISO country of residence (individuals)"),
    "applicant.establishment_country": ("applicant", _country, "ISO country of legal establishment/incorporation"),
    "applicant.incorporated": ("applicant", lambda v: isinstance(v, bool), "whether a legal entity exists"),
    "applicant.company_age_months": ("applicant", _nonnegative_int, "months since incorporation"),
    "applicant.sme": ("applicant", lambda v: isinstance(v, bool), "EU SME status"),
    "applicant.university_affiliation": ("applicant", lambda v: isinstance(v, bool), "affiliated with a university or research institution"),
    "applicant.university_name": ("applicant", lambda v: isinstance(v, str) and bool(v.strip()), "affiliated institution"),
    "applicant.academic_status": ("applicant", _choice(
        "student", "graduate", "researcher", "doctoral", "professor", "none",
    ), "highest current academic status"),
    "applicant.team_size": ("applicant", lambda v: type(v) is int and v >= 1, "people in the applicant team"),
    "project.title": ("project", lambda v: isinstance(v, str) and bool(v.strip()), "working title"),
    "project.summary": ("project", lambda v: isinstance(v, str) and bool(v.strip()), "owner-authored summary"),
    "project.stage": ("project", _choice("idea", "research", "prototype", "mvp", "market"), "maturity"),
    "project.themes": ("project", _strings, "topics/keywords"),
    "project.licence": ("project", lambda v: isinstance(v, str) and bool(v.strip()), "SPDX licence expression or 'proprietary'"),
    "project.open_source": ("project", lambda v: isinstance(v, bool), "results released under an open licence"),
    "project.funding_need": ("project", _money, "cash needed"),
    "project.matching_funds_available": ("project", _money, "own/partner funds available for co-financing"),
    "project.consortium_partner_countries": ("project", _countries, "ISO countries of committed partners"),
    "project.duration_months": ("project", lambda v: type(v) is int and v >= 1, "planned duration"),
    "project.achievements": ("project", _strings, "owner-stated achievements (never generated)"),
    "project.milestones": ("project", lambda v: isinstance(v, list) and all(
        isinstance(m, dict) and set(m) <= {"title", "month", "deliverable"} and m.get("title") for m in v
    ), "owner-planned milestones"),
    "project.budget_lines": ("project", lambda v: isinstance(v, list) and all(
        isinstance(b, dict) and set(b) <= {"category", "description", "amount", "currency"}
        and b.get("category") and _money({"amount": b.get("amount"), "currency": b.get("currency")}) for b in v
    ), "owner-estimated budget lines"),
    "preferences.instruments": ("preferences", _strings, "acceptable instrument kinds"),
    "preferences.max_effort": ("preferences", _choice("low", "medium", "high"), "acceptable application effort"),
    "preferences.timezone": ("preferences", lambda v: isinstance(v, str) and "/" in v or v == "UTC", "IANA timezone for notifications"),
    "preferences.min_days_to_deadline": ("preferences", _nonnegative_int, "minimum preparation days"),
    "preferences.accept_co_financing": ("preferences", lambda v: isinstance(v, bool), "willing to co-finance"),
}
_DDL = """
CREATE TABLE IF NOT EXISTS {t}s(
 profile_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, revision BIGINT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS {t}_revisions(
 profile_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(profile_id, revision));
CREATE TABLE IF NOT EXISTS {t}_commands(
 profile_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_revision BIGINT NOT NULL, PRIMARY KEY(profile_id, command_key));
"""


class FundingProfileError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _evidence(values):
    if not isinstance(values, list) or len(values) > 100:
        raise FundingProfileError("invalid_fact", "evidence is a bounded list of references")
    for item in values:
        if not isinstance(item, dict) or set(item) - {"kind", "id", "revision", "locator", "note"} or not item.get("id"):
            raise FundingProfileError("invalid_fact", "evidence needs kind/id and optional revision/locator/note")
    return values


def validate_fact(key, fact, facts=None):
    declared = FACTS if facts is None else facts
    if key not in declared and not key.startswith("custom."):
        raise FundingProfileError("unknown_fact", f"{key} is not a declared profile fact; use custom.* for free-form facts")
    if not isinstance(fact, dict) or "value" not in fact or set(fact) - {
        "value", "effective_from", "effective_to", "evidence", "note",
    }:
        raise FundingProfileError("invalid_fact", "fact requires value and optional effective dates/evidence/note")
    if fact["value"] is None:
        raise FundingProfileError("invalid_fact", "unknown facts are omitted or cleared, never stored as null")
    if key in declared and not declared[key][1](fact["value"]):
        raise FundingProfileError("invalid_fact", f"{key} has an invalid value ({declared[key][2]})")
    for bound in ("effective_from", "effective_to"):
        if fact.get(bound) is not None and not _DATE.fullmatch(str(fact[bound])):
            raise FundingProfileError("invalid_fact", f"{bound} must be YYYY-MM-DD")
    if fact.get("effective_from") and fact.get("effective_to") and fact["effective_to"] < fact["effective_from"]:
        raise FundingProfileError("invalid_fact", "effective_to precedes effective_from")
    _evidence(fact.get("evidence", []))
    return json.loads(canonical({**fact, "evidence": fact.get("evidence", [])}))


def section_of(key, facts=None):
    facts = FACTS if facts is None else facts
    return facts[key][0] if key in facts else "project" if key.startswith("custom.project") else "applicant" if key.startswith("custom.applicant") else "preferences"


def facts_at(state, as_of=None, sections=None):
    """Owner-reviewed facts effective on ``as_of``; proposals remain unknown."""
    result = {}
    for section in sections or SECTIONS:
        for key, fact in state["sections"][section].items():
            if fact["review"] != "owner-reviewed":
                continue
            if as_of and (
                fact.get("effective_from") and fact["effective_from"] > as_of
                or fact.get("effective_to") and fact["effective_to"] < as_of
            ):
                continue
            result[key] = fact["value"]
    return result


class FundingProfileStore:
    """Owner-scoped profile store.

    Other packs (Public Procurement) subclass it and override the class
    attributes below; the defaults are the funding applicant profile.
    """

    CONTRACT = CONTRACT
    FACTS = FACTS
    SECTIONS = SECTIONS
    TABLE = "funding_profile"
    ID_PREFIX = "funding-profile:"
    READ_SCOPE = READ_SCOPE
    WRITE_SCOPE = WRITE_SCOPE
    SUBJECT = "funding"

    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL.format(t=self.TABLE))

    def _declared_facts(self, state):
        """Declared fact keys that count as unknown when absent."""
        return self.FACTS

    def _check_fact(self, state, key):
        """Hook for subclasses that restrict facts per profile kind."""

    def _authorize(self, state, principal_id, scopes, *, write=False):
        required = self.WRITE_SCOPE if write else self.READ_SCOPE
        # Deliberately no operator bypass: applicant facts are private.
        if not principal_id or required not in scopes or state["owner"] != principal_id:
            raise FundingProfileError("unauthorized", f"only the profile owner with current {self.SUBJECT} scope can access it")
        namespace = state["namespace"]
        if f"namespace:{namespace}:write" not in scopes and (write or f"namespace:{namespace}:read" not in scopes):
            raise FundingProfileError("unauthorized", "current namespace access is required")
        if state.get("status") == "withdrawn":
            raise FundingProfileError("profile_withdrawn", "profile was withdrawn by its owner")

    def _row(self, namespace, profile_id):
        row = self.conn.execute(
            f"SELECT owner, revision, status FROM {self.TABLE}s WHERE profile_id=? AND namespace=?",
            [profile_id, namespace],
        ).fetchone()
        if not row:
            raise FundingProfileError("profile_not_found", "profile is unavailable")
        return row

    def _state(self, namespace, profile_id, revision=None):
        owner, current, status = self._row(namespace, profile_id)
        row = self.conn.execute(
            f"SELECT content_json FROM {self.TABLE}_revisions WHERE profile_id=? AND revision=?",
            [profile_id, current if revision is None else revision],
        ).fetchone()
        if not row:
            raise FundingProfileError("profile_not_found", "profile revision is unavailable")
        return {**json.loads(row[0]), "status": status, "current_revision": current}

    def create(self, namespace, request_key, *, label, principal_id, scopes, extra=None):
        if not isinstance(label, str) or not label.strip() or not request_key:
            raise FundingProfileError("invalid_profile", "label and request_key are required")
        state = {
            "contract": self.CONTRACT,
            "profile_id": self.ID_PREFIX + digest([namespace, principal_id, request_key])[:32],
            "namespace": namespace, "owner": principal_id, "label": label,
            "revision": 1, "status": "active",
            "sections": {section: {} for section in self.SECTIONS},
            "change": {"kind": "create", "keys": []},
            **(extra or {}),
        }
        self._authorize(state, principal_id, scopes, write=True)
        request_hash = digest([namespace, principal_id, label] + ([extra] if extra else []))
        prior = self.conn.execute(
            f"SELECT request_hash FROM {self.TABLE}s WHERE profile_id=?", [state["profile_id"]]
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise FundingProfileError("idempotency_conflict", "request_key identifies another profile")
            return {**self.inspect(namespace, state["profile_id"], principal_id=principal_id, scopes=scopes), "idempotent": True}
        state["updated_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(f"INSERT INTO {self.TABLE}s VALUES (?,?,?,?,1,'active')",
                              [state["profile_id"], namespace, principal_id, request_hash])
            self.conn.execute(f"INSERT INTO {self.TABLE}_revisions VALUES (?,1,?,?)",
                              [state["profile_id"], canonical(state), state["updated_at_ms"]])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "current_revision": 1, "idempotent": False}

    def inspect(self, namespace, profile_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, profile_id)
        self._authorize(current, principal_id, scopes)
        state = current if revision is None else self._state(namespace, profile_id, revision)
        facts = facts_at(state, sections=self.SECTIONS)
        return {
            **state,
            "unknown_facts": sorted(key for key in self._declared_facts(state) if key not in facts),
            "unreviewed_facts": sorted(
                key for section in self.SECTIONS for key, fact in state["sections"][section].items()
                if fact["review"] != "owner-reviewed"
            ),
        }

    def update(self, namespace, profile_id, command_key, expected_revision, *, principal_id, scopes,
               set_facts=None, propose_facts=None, clear_facts=None, review=None, reject=None):
        """Append one profile revision.

        ``set_facts`` records owner-stated facts (reviewed by the owner stating
        them). ``propose_facts`` records facts derived elsewhere (imports,
        extraction) that stay unknown to eligibility until the owner accepts
        them with ``review``. ``clear_facts`` returns facts to unknown.
        """
        request = {"set": set_facts or {}, "propose": propose_facts or {}, "clear": clear_facts or [],
                   "review": review or [], "reject": reject or []}
        if not any(request.values()):
            raise FundingProfileError("invalid_change", "a profile change must set, propose, clear, review or reject facts")
        request_hash = digest(request)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, profile_id)
            self._authorize(state, principal_id, scopes, write=True)
            prior = self.conn.execute(
                f"SELECT request_hash, result_revision FROM {self.TABLE}_commands WHERE profile_id=? AND command_key=?",
                [profile_id, command_key],
            ).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise FundingProfileError("idempotency_conflict", "command_key was used for another change")
                self.conn.execute("COMMIT")
                return {**self._state(namespace, profile_id, prior[1]), "idempotent": True}
            if state["current_revision"] != expected_revision:
                raise FundingProfileError("revision_conflict", "profile changed; inspect the current revision")
            sections = state["sections"]
            changed = []
            for kind, facts in (("owner-reviewed", request["set"]), ("proposed", request["propose"])):
                if not isinstance(facts, dict):
                    raise FundingProfileError("invalid_change", "facts are a key -> fact mapping")
                for key, fact in facts.items():
                    self._check_fact(state, key)
                    sections[section_of(key, self.FACTS)][key] = {**validate_fact(key, fact, self.FACTS), "review": kind}
                    changed.append(key)
            for key in request["clear"]:
                sections[section_of(key, self.FACTS)].pop(key, None)
                changed.append(key)
            for key, accept in [(k, True) for k in request["review"]] + [(k, False) for k in request["reject"]]:
                fact = sections[section_of(key, self.FACTS)].get(key)
                if not fact or fact["review"] != "proposed":
                    raise FundingProfileError("invalid_change", f"{key} has no pending proposal")
                if accept:
                    fact["review"] = "owner-reviewed"
                else:
                    sections[section_of(key, self.FACTS)].pop(key)
                changed.append(key)
            revision = expected_revision + 1
            updated = self.conn.execute(
                f"UPDATE {self.TABLE}s SET revision=? WHERE profile_id=? AND revision=? RETURNING revision",
                [revision, profile_id, expected_revision],
            ).fetchone()
            if not updated:
                raise FundingProfileError("revision_conflict", "profile changed concurrently")
            state = {key: value for key, value in state.items() if key not in {"status", "current_revision"}}
            state.update(revision=revision, sections=sections, updated_at_ms=self.now(),
                         change={"kind": "update", "keys": sorted(set(changed)), "command_key": command_key})
            self.conn.execute(f"INSERT INTO {self.TABLE}_revisions VALUES (?,?,?,?)",
                              [profile_id, revision, canonical(state), state["updated_at_ms"]])
            self.conn.execute(f"INSERT INTO {self.TABLE}_commands VALUES (?,?,?,?)",
                              [profile_id, command_key, request_hash, revision])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "status": "active", "current_revision": revision, "idempotent": False}

    def withdraw(self, namespace, profile_id, *, principal_id, scopes):
        state = self._state(namespace, profile_id)
        self._authorize(state, principal_id, scopes, write=True)
        self.conn.execute(f"UPDATE {self.TABLE}s SET status='withdrawn' WHERE profile_id=?", [profile_id])
        return {"profile_id": profile_id, "status": "withdrawn"}

    def pinned_facts(self, namespace, profile_id, revision, *, principal_id, scopes, as_of=None):
        """Facts of one exact revision, for assessments that must be replayable."""
        state = self.inspect(namespace, profile_id, revision=revision, principal_id=principal_id, scopes=scopes)
        return {
            "profile_id": profile_id, "profile_revision": state["revision"],
            "facts": facts_at(state, as_of, sections=self.SECTIONS),
            "unreviewed": state["unreviewed_facts"],
        }

    def list(self, namespace, *, principal_id, scopes):
        rows = self.conn.execute(
            f"SELECT profile_id FROM {self.TABLE}s WHERE namespace=? AND owner=? AND status='active' ORDER BY profile_id",
            [namespace, principal_id],
        ).fetchall()
        result = []
        for (profile_id,) in rows:
            state = self.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
            result.append({"profile_id": profile_id, "label": state["label"], "revision": state["revision"]})
        return {"profiles": result}

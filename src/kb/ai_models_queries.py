"""Answer a model's or dataset's records as of a date, and its revision and declared-licence history (#2742).

* **As of a date (AI08, #2785).** Given a model or dataset (a Hub repository id, an OpenML dataset id, an Epoch model
  name or a record id) and a date, each source's record revision current at that date is returned side by side: the
  subject's own record and the records an accepted AI06 identity match ties to it. Self-reported card results
  (``model-index``), OpenML run evaluations and Epoch estimates are listed separately, each with who reported it, and
  are never merged, averaged or ranked. Every item cites its source, the record revision (the Hub ``sha``, the OpenML
  id and version, or the Epoch vintage) and its as-of time.
* **Revision and declared-licence history (AI09, #2790).** Every revision with its ``sha`` or version and time, the
  source-stated gated, disabled, deactivated, removed and renamed states as revisions, and how the declared licence
  changed between revisions, quoted as declared with an SPDX id only on an exact licence-id match. No
  licence-compliance interpretation.

No model weights or dataset files are downloaded; no capability, safety, quality, risk or openness verdict; no
leaderboard, ranking, download, like or trending counts; no inference of training data, compute or parameters where the
source states none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.ai_models_sources import EXCLUSIONS, NEVER_SENTENCE
from src.kb.ai_models_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    AiModelsError,
    authorize,
    digest,
    iso,
    licence_of,
    normalise_licence,
    table_exists,
    to_ms,
)
from src.kb.ai_models_store import AiModelsStore, citation, revision_reference

NOT_MERGED = ("Self-reported card results, OpenML run evaluations and Epoch estimates are listed per source with who "
              "reported them; they are never merged, averaged, compared into a verdict or ranked.")
NO_COMPLIANCE = ("Declared licences are quoted as declared; an SPDX id is shown only on an exact licence-id match. No "
                 "licence-compliance, permissiveness or openness interpretation is made.")
SUBJECT_KINDS = ("hub-model", "hub-dataset", "openml-dataset", "openml-task", "epoch-model")


class AiModelsQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = AiModelsStore(conn, initialize=False, now=now)

    # ------------------------------------------------------------------ subjects

    def resolve(self, namespace: str, subject: str | Mapping[str, Any]) -> dict[str, Any] | None:
        """The record a subject names: ``{"record_id"}``, ``{"repo_id", "kind"}``, ``{"openml_dataset_id"}``,
        ``{"openml_task_id"}``, ``{"epoch_model"}``, or a string (record id, repository id, ``openml:<id>`` or an
        Epoch model name, matched exactly)."""
        if isinstance(subject, Mapping):
            subject = dict(subject)
            if subject.get("record_id"):
                try:
                    return self.store.record(namespace, str(subject["record_id"]))
                except AiModelsError:
                    return None
            if subject.get("repo_id"):
                kinds = [f"hub-{subject['kind']}"] if subject.get("kind") else ["hub-model", "hub-dataset"]
                return next((r for k in kinds if (r := self.store.find(namespace, k, str(subject["repo_id"])))),
                            None)
            if subject.get("openml_dataset_id") is not None:
                return self.store.find(namespace, "openml-dataset", str(int(subject["openml_dataset_id"])))
            if subject.get("openml_task_id") is not None:
                return self.store.find(namespace, "openml-task", str(int(subject["openml_task_id"])))
            if subject.get("epoch_model"):
                return self.store.find(namespace, "epoch-model", str(subject["epoch_model"]))
            raise AiModelsError("invalid_subject", "name a record id, a repository id, an OpenML id or an Epoch model")
        text = str(subject or "").strip()
        if not text:
            raise AiModelsError("invalid_subject", "name a model or dataset")
        if text.startswith("ai:"):
            return self.resolve(namespace, {"record_id": text})
        if text.casefold().startswith("openml:") and text.split(":", 1)[1].isdigit():
            return self.resolve(namespace, {"openml_dataset_id": int(text.split(":", 1)[1])})
        if "/" in text:
            return self.resolve(namespace, {"repo_id": text})
        return self.store.find(namespace, "epoch-model", text)

    def _spdx(self, namespace: str) -> Any:
        if not table_exists(self.conn, "oss_records"):
            return None
        from src.kb.oss_ecosystem_store import OssEcosystemStore

        try:
            return OssEcosystemStore(self.conn, initialize=False).spdx_list(namespace)
        except Exception:  # noqa: BLE001 - an unreadable list degrades to no_spdx_list, never to a guess
            return None

    def _licence(self, statement: Mapping[str, Any], spdx: Any) -> dict[str, Any] | None:
        declared = licence_of(statement)
        if not declared and statement.get("record_type") not in {"hub_repository_revision",
                                                                  "openml_dataset_revision"}:
            return None
        return {"declared": declared, "spdx": normalise_licence(declared, spdx)}

    # ------------------------------------------------------------------ AI08: as of a date

    def _reported(self, namespace: str, record: Mapping[str, Any], revision: Mapping[str, Any],
                  as_of_ms: int) -> list[dict[str, Any]]:
        """What the source reported at this revision, one group per reporter; never merged across sources."""
        statement = revision["statement"]
        if revision["state"] != "published":
            return []
        if record["record_kind"] in {"hub-model", "hub-dataset"}:
            items = self.store.observations(namespace, record["record_id"], sha=statement.get("sha"))
            if not items:
                return []
            return [{"kind": "self_reported_result", "reported_by": items[0]["reported_by"], "items": [
                {k: o["statement"].get(k) for k in ("model_name", "task", "dataset", "metric", "result_source")}
                | {"observation_id": o["observation_id"], "revision_id": o["revision_id"]} for o in items]}]
        if record["record_kind"] == "epoch-model":
            estimates = dict(statement.get("estimates") or {})
            return [{"kind": "epoch_estimate", "reported_by": statement.get("reported_by"),
                     "confidence": statement.get("confidence"), "items": [
                         {"estimate": k, **v} for k, v in sorted(estimates.items())],
                     "not_stated": sorted({"parameters", "training_compute", "training_dataset_size"} -
                                          set(estimates))}] if estimates or statement.get("confidence") else []
        groups = []
        if record["record_kind"] == "openml-dataset":
            for task in self.store.records(namespace, record_kind="openml-task"):
                task_revision, _ = self.store.select_revision(namespace, task["record_id"], as_of_ms=as_of_ms)
                if task_revision is None or task_revision["statement"].get("dataset_id") != int(record["native_key"]):
                    continue
                items = self.store.observations(namespace, task["record_id"], as_of_ms=as_of_ms)
                if items:
                    groups.append({"kind": "openml_run_evaluation", "reported_by": items[0]["reported_by"],
                                   "task": {"record_id": task["record_id"], "openml_task_id": int(task["native_key"]),
                                            "revision_id": task_revision["revision_id"]},
                                   "items": [{k: o["statement"].get(k) for k in (
                                       "run_id", "measure", "value_text", "flow_id", "flow_name", "upload_time")}
                                       | {"state": o["state"], "observation_id": o["observation_id"]}
                                       for o in items]})
        return groups

    def _entry(self, namespace: str, record: Mapping[str, Any], as_of_ms: int, spdx: Any, *, relation: str,
               match: Mapping[str, Any] | None = None) -> dict[str, Any]:
        revision, reason = self.store.select_revision(namespace, record["record_id"], as_of_ms=as_of_ms)
        entry: dict[str, Any] = {
            "source": record["source"], "relation": relation,
            "record": {k: record[k] for k in ("record_id", "record_kind", "native_key", "label", "live_verification")},
            **({"match": dict(match)} if match else {}),
        }
        if revision is None:
            return {**entry, "status": reason, "note": "no revision of this record was current at the date"}
        statement = revision["statement"]
        entry.update({
            "status": "current" if revision["state"] == "published" else revision["state"],
            "revision": {"revision_id": revision["revision_id"], **revision_reference(record, revision),
                         "time": revision["source_time"], "time_basis": revision["time_basis"]},
            "statement": {k: v for k, v in statement.items() if k not in {"record_type", "source"}},
            "licence": self._licence(statement, spdx),
            "reported": self._reported(namespace, record, revision, as_of_ms),
            "citation": citation(record, revision),
        })
        if record["record_kind"] == "epoch-model" and revision.get("vintage_id"):
            entry["vintage"] = self.store.vintage(namespace, revision["vintage_id"])
        return entry

    def records_as_of(self, namespace: str, subject: str | Mapping[str, Any], *, scopes: Iterable[str],
                      as_of: str | int | None = None) -> dict[str, Any]:
        """Each source's record revision current at the date, side by side, each cited; never merged."""
        authorize(namespace, set(scopes), READ_SCOPE)
        as_of_ms = to_ms(as_of) if as_of is not None else 2**62
        query = {"subject": subject, "as_of": iso(as_of_ms) if as_of is not None else None}
        base = {"contract": ANSWER_CONTRACT, "namespace": namespace, "query": query,
                "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "not_merged": NOT_MERGED}
        record = self.resolve(namespace, subject)
        if record is None:
            return {**base, "status": "no_records", "side_by_side": [], "receipt": {"digest": digest(query)},
                    "reason": "no source holds a record for this model or dataset in this namespace; that is not a "
                              "statement that it does not exist"}
        spdx = self._spdx(namespace)
        entries = [self._entry(namespace, record, as_of_ms, spdx, relation="subject")]
        counterparts = []
        if table_exists(self.conn, "ai_identity_matches"):
            from src.kb.ai_models_identity import AiModelsIdentity

            counterparts = AiModelsIdentity(self.conn, initialize=False).counterparts(namespace, record["record_id"])
        for match in counterparts:
            other = self.store.record(namespace, match["record_id"])
            entries.append(self._entry(namespace, other, as_of_ms, spdx, relation="accepted-match", match=match))
        links = []
        if table_exists(self.conn, "ai_links"):
            from src.kb.ai_models_links import AiModelsLinks

            links = [{k: x[k] for k in ("link_id", "kind", "basis", "identifier", "state", "target", "revision_id")}
                     for x in AiModelsLinks(self.conn, initialize=False).current_links(namespace,
                                                                                        record["record_id"])]
        status = "records" if any(e.get("revision") for e in entries) else "no_revision_by_as_of"
        answer = {**base, "status": status, "subject": entries[0]["record"], "side_by_side": entries,
                  "links": links, "spdx_list_version": getattr(spdx, "version", None),
                  "unmatched_note": None if counterparts else "no accepted identity match ties this record to "
                                                              "another source; other sources are not assumed"}
        answer["receipt"] = {"digest": digest([query, [e.get("citation") for e in entries]])}
        return answer

    # ------------------------------------------------------------------ AI09: revision and licence history

    def revision_history(self, namespace: str, subject: str | Mapping[str, Any], *,
                         scopes: Iterable[str]) -> dict[str, Any]:
        """Every revision with sha or version and time, source-stated states, and declared-licence changes quoted."""
        authorize(namespace, set(scopes), READ_SCOPE)
        base = {"contract": ANSWER_CONTRACT, "namespace": namespace, "query": {"subject": subject},
                "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "licence_note": NO_COMPLIANCE}
        record = self.resolve(namespace, subject)
        if record is None:
            return {**base, "status": "no_records", "revisions": [], "licence_changes": [],
                    "reason": "no source holds a record for this model or dataset in this namespace"}
        spdx = self._spdx(namespace)
        revisions, changes, previous = [], [], None
        for revision in self.store.revision_rows(namespace, record["record_id"]):
            statement = revision["statement"]
            licence = self._licence(statement, spdx)
            item = {"revision_id": revision["revision_id"], **revision_reference(record, revision),
                    "time": revision["source_time"], "time_basis": revision["time_basis"],
                    "retrieved_at": revision["retrieved_at"], "state": revision["state"],
                    **({"state_detail": statement["state_detail"]} if statement.get("state_detail") else {}),
                    **({"source_status": statement["status"]} if statement.get("status") else {}),
                    "licence": licence, "citation": citation(record, revision)}
            if record["record_kind"] == "epoch-model" and revision.get("vintage_id"):
                item["vintage"] = self.store.vintage(namespace, revision["vintage_id"])
            revisions.append(item)
            if licence is not None and revision["state"] == "published":
                if previous is not None and previous["licence"]["declared"] != licence["declared"]:
                    changes.append({"from_revision_id": previous["revision_id"], "to_revision_id":
                                    revision["revision_id"], "at": revision["source_time"],
                                    "before": previous["licence"], "after": licence,
                                    "statement": "the declared licence fields changed between these revisions, as "
                                                 "stated by the source; no compliance interpretation"})
                previous = {"revision_id": revision["revision_id"], "licence": licence}
        states = [r for r in revisions if r["state"] != "published"]
        return {**base, "status": "history", "subject": {k: record[k] for k in (
            "record_id", "record_kind", "native_key", "label", "source", "live_verification")},
            "revisions": revisions, "licence_changes": changes, "source_stated_states": states,
            "spdx_list_version": getattr(spdx, "version", None)}

    # ------------------------------------------------------------------ evidence bundle

    def evidence_bundle(self, answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A ``noesis-evidence-bundle-v1`` citing every item with source, record revision and as-of time."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        as_of = answer.get("query", {}).get("as_of")
        builder = EvidenceBundleBuilder("answer", {"operation": "ai-model-records-as-of", "query": answer["query"]},
                                        created_at_ms=created_at_ms, as_of_ms=to_ms(as_of) if as_of else None)
        refs = []
        entries = list(answer.get("side_by_side") or [])
        if answer.get("status") == "no_records":
            builder.add_omission("no source holds a record for this model or dataset (unknown, not absent)")
        for entry in entries:
            if not entry.get("citation"):
                builder.add_omission(f"{entry['source']} {entry['record']['native_key']}: {entry['status']}")
                continue
            cited = entry["citation"]
            object_id = f"ai-record:{cited['revision_id']}"
            builder.add_object("evidence", {
                "kind": "ai-model-record-revision", "source": cited["source"], "relation": entry["relation"],
                "record": entry["record"], "revision": cited["revision"], "as_of": cited["as_of"],
                "time_basis": cited["time_basis"], "retrieved_at": cited["retrieved_at"],
                "evidence_origin": cited["evidence_origin"], "live_verification": cited["live_verification"],
                "status": entry["status"], "licence": entry.get("licence"),
                "locator": {"cited": True, "url": cited.get("url"), "revision_id": cited["revision_id"]},
            }, object_id=object_id)
            refs.append(object_id)
            if cited.get("url"):
                builder.add_external_reference(f"revision:{cited['revision_id']}", cited["url"], required=False)
            for group in entry.get("reported") or []:
                group_id = f"ai-reported:{cited['revision_id']}:{group['kind']}"
                builder.add_object("evidence", {
                    "kind": group["kind"], "reported_by": group["reported_by"], "source": cited["source"],
                    "revision": cited["revision"], "as_of": cited["as_of"], "items": group["items"],
                    "note": NOT_MERGED}, object_id=group_id, references=[object_id])
                refs.append(group_id)
            if entry["status"] != "current":
                builder.add_omission(f"{cited['source']} {entry['record']['native_key']}: the revision current at "
                                     f"the date is {entry['status']}", object_id=object_id)
            if cited["live_verification"] != "verified-live":
                builder.add_omission(f"{cited['source']}: offline or unverified-live evidence "
                                     f"({cited['evidence_origin']}), not live coverage", object_id=object_id)
        root = {k: answer.get(k) for k in ("contract", "query", "status", "exclusions", "never", "not_merged")}
        builder.add_object("answer", {"kind": "ai-model-records-as-of", **root,
                                      "statements": [{"source": e["source"], "relation": e["relation"],
                                                      "status": e["status"], "evidence_refs": [
                                                          f"ai-record:{e['citation']['revision_id']}"]
                                                      if e.get("citation") else []} for e in entries]},
                           object_id=f"ai-answer:{digest(answer.get('receipt') or answer['query'])[:24]}",
                           references=sorted(set(refs)), root=True)
        return builder.build()


__all__ = ["NOT_MERGED", "NO_COMPLIANCE", "SUBJECT_KINDS", "AiModelsQueries"]

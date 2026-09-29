"""Link registered trials to publications and preprints harvested by the Science providers (H07).

Publications are never copied into a clinical store: a link is a
``registry-link`` record pointing at an exact ``documents`` revision. Links are
created only from declared identifiers, each with its evidence kind:

* ``registry-declared-reference`` – the registry lists the article (CT.gov
  ``referencesModule`` PMID/DOI);
* ``secondary-source-identifier`` – the bibliographic source lists the trial
  (PubMed ``DataBankList``, Europe PMC accession annotations), matched against
  the trial's own or declared secondary identifiers;
* ``paper-family`` – the article's preprint, journal version, corrections and
  retractions, grouped by :class:`~src.domains.research.paper_families.PaperFamilyStore`
  from provider relations and Crossref notices.

A registry number that appears only in title or abstract text becomes a
*candidate* link that needs an independent review. Trials without accepted
links and trial-report publications that carry no registry identifier are
reported as coverage gaps; nothing is inferred from titles or similarity.

Surveillance series (#1917, I09) are linked the same way, by explicit citation
only (:meth:`PublicationLinker.link_series`): a ``series-publication`` link needs
the series' dataset identifier (DOI, RKI repository@tag, GHO indicator code,
Eurostat dataset code, GENESIS table code) in a document's declared references
(``references_json``) or data-availability statement
(``data_availability_statement``); a ``series-trial`` link needs it in the
trial's registry-declared references or secondary identifiers. A shared
condition or geography is never a link; a mention in a title, abstract or trial
summary is a candidate that ``review_candidate`` accepts or rejects. Claims
about a linked document from the Science claim layer are shown beside the
series as a separate view (:meth:`PublicationLinker.series_claims`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.domains.research.paper_families import PaperFamilyError, PaperFamilyStore
from src.ingestion.connectors.paper import trial_registry
from src.kb.clinical_records import (
    PRIMARY_IDENTIFIER,
    REVIEW_SCOPE,
    ClinicalRecordError,
    ClinicalRecordStore,
    _require_read,
    _require_write,
    digest,
)

RXIV_SOURCES = {"medrxiv", "biorxiv"}
SURVEILLANCE = "surveillance"
# Dataset identifier shapes looked for in declared references when reporting cited datasets that no series holds.
# One token boundary for dataset identifiers, shared by citation matching and the gap scanner: an identifier is
# not part of a longer word or code (letters, digits, underscore, hyphen, or a dot joining more of the code), while
# path separators, punctuation and spaces delimit it - so ``.../databrowser/view/hlth_cd_aro/default/table`` cites
# ``hlth_cd_aro`` and ``hlth_cd_aro2`` does not.
TOKEN_BEFORE = r"(?<![\w.-])"
TOKEN_AFTER = r"(?![\w-]|\.\w)"


def token_pattern(body, flags=0):
    return re.compile(TOKEN_BEFORE + body + TOKEN_AFTER, flags)


# Dataset identifier shapes looked for in declared references when reporting cited datasets that no series holds.
DATASET_PATTERNS = {
    "eurostat-dataset": token_pattern(r"hlth_[a-z0-9_]+", re.I),
    "rki-release": token_pattern(r"robert-koch-institut/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*@[A-Za-z0-9_-]+"
                                 r"(?:\.[A-Za-z0-9_-]+)*"),
    "doi": token_pattern(r"10\.5281/zenodo\.\d+", re.I),
}


def normalise_identifier(kind, value):
    """One normalisation for both sides of a dataset-citation match."""
    text = str(value or "").strip()
    if kind == "doi":
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.casefold()


def cites(text, kind, identifier):
    """Whether ``text`` contains the identifier as a whole token (never a substring of a longer code)."""
    needle = normalise_identifier(kind, identifier)
    if not needle:
        return False
    haystack = str(text or "").casefold()
    if kind == "doi":
        # A DOI written as a resolver URL is the same identifier (normalised on both sides).
        haystack = re.sub(r"https?://(?:dx\.)?doi\.org/", " ", haystack)
    return token_pattern(re.escape(needle)).search(haystack) is not None


def declared_dataset_text(doc):
    """(field, text) pairs a document declares: its references and its data-availability statement."""
    metadata = doc.get("metadata") or {}
    out = []
    raw = metadata.get("references_json")
    try:
        references = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except ValueError:
        references = []
    for index, reference in enumerate(references):
        text = reference.get("text") if isinstance(reference, dict) else reference
        if text:
            out.append((f"references[{index}]", str(text)))
    if metadata.get("data_availability_statement"):
        out.append(("data_availability_statement", str(metadata["data_availability_statement"])))
    return out


class PublicationLinker:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.records = ClinicalRecordStore(conn, initialize=initialize, now=now)
        self.now = self.records.now

    # ------------------------------------------------------------ documents

    def _documents(self, document_ids=None, limit=5000):
        if not self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='documents'").fetchone():
            return []
        rows = self.conn.execute(
            "SELECT document_id, source_id, title, content, metadata FROM documents WHERE source_type='paper' "
            "ORDER BY document_id LIMIT ?", [int(limit)]).fetchall()
        docs = []
        for document_id, source_id, title, content, metadata in rows:
            if document_ids is not None and document_id not in document_ids:
                continue
            revision = self.conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark IS NOT "
                "NULL ORDER BY revision DESC LIMIT 1", [document_id]).fetchone()
            if not revision:
                continue
            doc = {"document_id": document_id, "revision_id": revision[0], "source_id": source_id, "title": title,
                   "content": content, "metadata": json.loads(metadata) if metadata else {}}
            doc["ids"] = trial_registry.identifiers(doc)
            doc["declared"] = trial_registry.registry_identifiers(doc)
            doc["mentions"] = trial_registry.mentions(doc)
            doc["related"] = _related(doc)
            docs.append(doc)
        return docs

    # ----------------------------------------------------------------- link

    def link(self, namespace, *, principal_id, scopes, observation_id, document_ids=None, family_scopes=None):
        """Create publication links for every trial in the namespace; report gaps."""
        _require_write(namespace, scopes)
        docs = self._documents(set(document_ids) if document_ids is not None else None)
        trials = self.records.find(namespace, scopes=scopes, kinds={"registered-trial"})
        by_pmid = {d["ids"]["pmid"]: d for d in docs if d["ids"]["pmid"]}
        by_doi = {d["ids"]["doi"]: d for d in docs if d["ids"]["doi"]}
        result = {"observation_id": observation_id, "links": [], "candidates": [], "families": [],
                  "declared_not_harvested": [], "family_errors": []}
        for trial_row in trials:
            trial = trial_row["record"]
            own = {(PRIMARY_IDENTIFIER[trial["registry"]], trial["identifier"])}
            own |= {(s["kind"], s["value"]) for s in trial.get("secondary_identifiers") or []
                    if s["kind"] in {"nct", "eudract", "eu-ct", "isrctn"}}
            source = {"provider": trial["registry"], "identifier": trial["identifier"]}
            accepted, mentioned_docs = {}, []
            for reference in trial.get("declared_references") or []:
                doc = by_pmid.get(reference.get("pmid")) or by_doi.get(reference.get("doi"))
                if not doc:
                    result["declared_not_harvested"].append({"trial": source, "reference": reference})
                    continue
                accepted[doc["document_id"]] = doc
                result["links"].append(self._add(namespace, source, doc, "registry-declared-reference", "accepted", {
                    "reference": reference, "trial_record_id": trial_row["record_id"],
                    "trial_revision": trial_row["revision"]}, scopes, observation_id))
            for doc in docs:
                matched = [a for a in doc["declared"] if (a["kind"], a["value"]) in own]
                if matched:
                    accepted[doc["document_id"]] = doc
                    result["links"].append(self._add(namespace, source, doc, "secondary-source-identifier",
                                                     "accepted", {"accessions": matched}, scopes, observation_id))
                else:
                    mentioned = [m for m in doc["mentions"] if (m["kind"], m["value"]) in own]
                    if mentioned:
                        mentioned_docs.append((doc, mentioned))
            members = set(accepted)
            for doc in sorted(accepted.values(), key=lambda d: d["document_id"]):
                family = self._family(namespace, source, doc, docs, principal_id=principal_id,
                                      scopes=family_scopes or scopes, observation_id=observation_id, result=result)
                if family:
                    result["families"].append(family)
                    members |= set(family["members"])
            for doc, mentioned in mentioned_docs:
                if doc["document_id"] in members:
                    continue  # already linked through declared evidence or its paper family
                result["candidates"].append(self._add(
                    namespace, source, doc, "abstract-mention", "candidate",
                    {"mentions": mentioned, "note": "text mention only; needs independent review"}, scopes,
                    observation_id))
        return result

    def _add(self, namespace, source, doc, evidence_kind, status, evidence, scopes, observation_id, family_id=None):
        target = {"document_id": doc["document_id"], "revision_id": doc["revision_id"],
                  "identifiers": {k: v for k, v in doc["ids"].items() if v}, "title": (doc.get("title") or "")[:500]}
        if family_id:
            target["family_id"] = family_id
        applied = self.records.add_link(namespace, {
            "link_kind": "registry-publication", "from_record": source, "to": target,
            "evidence_kind": evidence_kind, "evidence": {**evidence, "document_source": doc["source_id"]},
            "status": status}, scopes=scopes, observation_id=observation_id)
        return {"link_id": applied["link_id"], "trial": source, "document_id": doc["document_id"],
                "evidence_kind": evidence_kind, "status": status, "created": bool(applied["created"]),
                "revised": bool(applied["revised"])}

    def _family(self, namespace, source, doc, docs, *, principal_id, scopes, observation_id, result):
        """Group the article with its preprint and notices; link new members through the family."""
        store = PaperFamilyStore(self.conn, now=self.now)
        preprints = [d for d in docs if d["source_id"] in RXIV_SOURCES and doc["ids"]["doi"] and any(
            r.get("target_identifier") == doc["ids"]["doi"] and r.get("predicate") == "IsPreprintOf"
            for r in d["related"])]
        root = preprints[0] if preprints else doc
        key = f"clinical:{source['provider']}:{source['identifier']}:{root['document_id']}"
        try:
            family = store.create(namespace, key, _member(root), principal_id=principal_id, scopes=scopes)
            if preprints:
                relation = next(r for r in root["related"] if r.get("target_identifier") == doc["ids"]["doi"])
                if not any(m["source"]["document_id"] == doc["document_id"] for m in family["members"]):
                    family = store.add_member(
                        namespace, family["family_id"], "clinical-add:" + doc["document_id"], _member(doc),
                        source_member_id=family["members"][0]["member_id"], relation_type="is-preprint-of",
                        provenance={"kind": "provider", "relation": relation}, expected_revision=family["revision"],
                        principal_id=principal_id, scopes=scopes)
            family = self._attach_notices(store, namespace, family, principal_id=principal_id, scopes=scopes)
        except PaperFamilyError as exc:
            result["family_errors"].append({"trial": source, "document_id": doc["document_id"], "code": exc.code})
            return None
        for member in family["members"]:
            if member["status"] != "active":
                continue
            member_doc = next((d for d in docs if d["document_id"] == member["source"]["document_id"]), None)
            if member_doc:
                result["links"].append(self._add(
                    namespace, source, member_doc, "paper-family", "accepted",
                    {"family_id": family["family_id"], "via_document": doc["document_id"],
                     "relations": [r["relation_id"] for r in family["relations"]
                                   if member["member_id"] in {r["source_member_id"], r["target_member_id"]}]},
                    scopes=_link_scopes(scopes), observation_id=observation_id, family_id=family["family_id"]))
        return {"family_id": family["family_id"], "revision": family["revision"], "trial": source,
                "members": [m["source"]["document_id"] for m in family["members"]],
                "notices": [{"notice_type": n["notice_type"], "status": n["status"], "target_member_id": n[
                    "target_member_id"]} for n in family["notices"]]}

    def _attach_notices(self, store, namespace, family, *, principal_id, scopes):
        if not self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='crossref_notices'").fetchone():
            return family
        dois = {v["value"] for m in family["members"] for v in m["identifiers"] if v["kind"] == "doi"}
        attached = {n["notice_id"] for n in family["notices"]}
        for notice_id, notice_json in self.conn.execute(
                "SELECT notice_id, notice_json FROM crossref_notices ORDER BY notice_id").fetchall():
            if notice_id in attached or json.loads(notice_json).get("target_doi") not in dois:
                continue
            family = store.attach_notice(namespace, family["family_id"], "clinical-notice:" + notice_id, notice_id,
                                         expected_revision=family["revision"], principal_id=principal_id, scopes=scopes)
        return family

    # ------------------------------------------------------ surveillance series

    def _series_identifiers(self, namespace):
        """Series id -> every citable identifier its vintages carry (their citations and native revisions)."""
        from src.kb.surveillance import SurveillanceStore

        store = SurveillanceStore(self.conn, initialize=False)
        out = {}
        for series in store.find_series(namespace, limit=5000):
            identifiers = set()
            for vintage in store.vintage_rows(namespace, series["series_id"]):
                for citation in vintage["metadata"].get("citations") or []:
                    identifiers.add((citation["kind"], citation["identifier"]))
            out[series["series_id"]] = {"series": series, "identifiers": sorted(identifiers)}
        return out

    def link_series(self, namespace, *, principal_id, scopes, observation_id, document_ids=None):
        """Link surveillance series to publications and trials by explicit dataset citation only."""
        del principal_id
        _require_write(namespace, scopes)
        if not self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='surveillance_series'"
                                 ).fetchone():
            return {"observation_id": observation_id, "links": [], "candidates": []}
        held = self._series_identifiers(namespace)
        docs = self._documents(set(document_ids) if document_ids is not None else None)
        trials = self.records.find(namespace, scopes=scopes, kinds={"registered-trial"})
        result = {"observation_id": observation_id, "links": [], "candidates": []}
        for series_id, entry in sorted(held.items()):
            source = {"provider": SURVEILLANCE, "identifier": series_id}
            for doc in docs:
                declared = [{"kind": kind, "identifier": identifier, "field": field}
                            for kind, identifier in entry["identifiers"]
                            for field, text in declared_dataset_text(doc) if cites(text, kind, identifier)]
                if declared:
                    result["links"].append(self._add_series(namespace, source, doc, "dataset-citation", "accepted",
                                                            {"citations": declared}, scopes, observation_id))
                    continue
                mentioned = [{"kind": kind, "identifier": identifier, "field": field}
                             for kind, identifier in entry["identifiers"]
                             for field, text in (("title", doc.get("title")), ("content", doc.get("content")))
                             if cites(text, kind, identifier)]
                if mentioned:
                    result["candidates"].append(self._add_series(
                        namespace, source, doc, "abstract-mention", "candidate",
                        {"mentions": mentioned, "note": "text mention only; needs independent review"}, scopes,
                        observation_id))
            for row in trials:
                trial = row["record"]
                declared, mentioned = [], []
                for kind, identifier in entry["identifiers"]:
                    for index, reference in enumerate(trial.get("declared_references") or []):
                        for key in ("doi", "citation"):
                            if cites(reference.get(key), kind, identifier):
                                declared.append({"kind": kind, "identifier": identifier,
                                                 "declared_by": f"declared_references[{index}].{key}",
                                                 "locator": reference.get("locator")})
                    for secondary in trial.get("secondary_identifiers") or []:
                        if cites(secondary.get("value"), kind, identifier):
                            declared.append({"kind": kind, "identifier": identifier,
                                             "declared_by": secondary.get("declared_by"),
                                             "locator": secondary.get("locator")})
                    for field in ("brief_summary", "title"):
                        if cites(trial.get(field), kind, identifier):
                            mentioned.append({"kind": kind, "identifier": identifier, "field": field})
                if not declared and not mentioned:
                    continue
                status, evidence_kind = ("accepted", "trial-declared-dataset") if declared else (
                    "candidate", "abstract-mention")
                evidence = ({"declarations": declared, "trial_record_id": row["record_id"],
                             "trial_revision": row["revision"]} if declared else
                            {"mentions": mentioned, "trial_record_id": row["record_id"],
                             "note": "text mention only; needs independent review"})
                applied = self.records.add_link(namespace, {
                    "link_kind": "series-trial", "from_record": source,
                    "to": {"registry": trial["registry"], "identifier": trial["identifier"]},
                    "evidence_kind": evidence_kind, "evidence": evidence, "status": status},
                    scopes=scopes, observation_id=observation_id)
                (result["links"] if declared else result["candidates"]).append({
                    "link_id": applied["link_id"], "series_id": series_id, "trial": trial["identifier"],
                    "evidence_kind": evidence_kind, "status": status, "created": bool(applied["created"])})
        return result

    # ------------------------------------------------------------ medicines (#2214, MR09)

    def link_medicines(self, namespace, *, principal_id, scopes, observation_id, document_ids=None):
        """Link medicines-regulation records to what they explicitly cite, and to FAERS counts by reviewed identity.

        A ``medicine-trial`` or ``medicine-publication`` link needs an identifier (NCT, EudraCT, EU CT, PMID, DOI)
        in the regulator's own text (label section, EPAR, safety communication), stored with the citing text and
        locator. A ``medicine-faers`` link needs an *accepted* RxNorm match on both sides that shares an ingredient
        concept; the linked record stays labelled as FAERS reporting counts. Nothing is linked from a similar name
        or a shared topic.
        """
        from src.kb.clinical_medicines import CONTRACT
        from src.kb.clinical_records import COUNT_SEMANTICS, REGISTRY_FOR_IDENTIFIER

        del principal_id
        _require_write(namespace, scopes)
        rows = [r for r in self.records.find(namespace, scopes=scopes, limit=10000)
                if r["record"].get("contract") == CONTRACT]
        docs = self._documents(set(document_ids) if document_ids is not None else None)
        by_pmid = {d["ids"]["pmid"]: d for d in docs if d["ids"].get("pmid")}
        by_doi = {str(d["ids"]["doi"]).lower(): d for d in docs if d["ids"].get("doi")}
        result = {"observation_id": observation_id, "trials": [], "publications": [], "not_held": [], "faers": [],
                  "faers_withheld": []}
        cited: dict[tuple, dict[tuple, list]] = {}
        for row in rows:
            item = row["record"]
            for reference in item.get("cited_references") or []:
                cited.setdefault((item["provider"], item["native_id"]), {}).setdefault(
                    (reference["kind"], reference["value"]), []).append(
                    {"citing_text": reference["citing_text"], "locator": reference["locator"],
                     "record_id": row["record_id"], "revision": row["revision"], "record_kind": row["record_kind"]})
        for (provider, native_id), references in sorted(cited.items()):
            source = {"provider": provider, "identifier": native_id}
            for (kind, value), citations in sorted(references.items()):
                evidence = {"citations": citations, "note": "identifier written in the regulator's own text"}
                if kind in REGISTRY_FOR_IDENTIFIER:
                    registry = REGISTRY_FOR_IDENTIFIER[kind]
                    status = "accepted" if self.records.trial_id(namespace, registry, value) else "target-not-acquired"
                    applied = self.records.add_link(namespace, {
                        "link_kind": "medicine-trial", "from_record": source,
                        "to": {"registry": registry, "identifier": value},
                        "evidence_kind": "regulator-cited-reference", "evidence": evidence, "status": status},
                        scopes=scopes, observation_id=observation_id)
                    result["trials"].append({"link_id": applied["link_id"], "from": source, "trial": value,
                                             "registry": registry, "status": status})
                    continue
                doc = by_pmid.get(value) if kind == "pmid" else by_doi.get(str(value).lower())
                if doc is None:
                    result["not_held"].append({"from": source, "kind": kind, "value": value,
                                               "reason": "cited publication is not among harvested documents"})
                    continue
                target = {"document_id": doc["document_id"], "revision_id": doc["revision_id"],
                          "identifiers": {k: v for k, v in doc["ids"].items() if v},
                          "title": (doc.get("title") or "")[:500]}
                applied = self.records.add_link(namespace, {
                    "link_kind": "medicine-publication", "from_record": source, "to": target,
                    "evidence_kind": "regulator-cited-reference", "evidence": evidence, "status": "accepted"},
                    scopes=scopes, observation_id=observation_id)
                result["publications"].append({"link_id": applied["link_id"], "from": source,
                                               "document_id": doc["document_id"], "status": "accepted"})
        self._link_faers(namespace, scopes, observation_id, result, COUNT_SEMANTICS)
        return result

    def _link_faers(self, namespace, scopes, observation_id, result, count_semantics):
        from src.kb.clinical_terms import MedicineIdentity

        identity = MedicineIdentity(self.conn, initialize=False)
        matches = identity.matches(namespace, scopes=scopes)

        def ingredients(match):
            target = match["target"]
            return ({target["rxcui"]} if target.get("tty") == "IN" else set()) | {
                i["rxcui"] for i in target.get("ingredients") or []}

        faers = [m for m in matches if m["subject"]["provider"] == "openfda"
                 and str(m["subject"]["native_id"]).startswith("faers:")]
        medicines = [m for m in matches if m not in faers and not m["subject_key"].startswith("fda-dsc:")]
        pairs: dict[tuple, list] = {}
        for faers_match in faers:
            for match in medicines:
                shared = sorted(ingredients(faers_match) & ingredients(match))
                if shared:
                    key = (match["subject"]["provider"], match["subject"]["native_id"],
                           faers_match["subject"]["native_id"])
                    pairs.setdefault(key, []).append((match, faers_match, shared))
        for (provider, native_id, faers_id), candidates in sorted(pairs.items()):
            source = {"provider": provider, "identifier": native_id}
            accepted = [(m, f, s) for m, f, s in candidates if m["state"] == f["state"] == "accepted"]
            existing = [link for link in self.records.links(namespace, provider=provider, identifier_value=native_id)
                        if link["link_kind"] == "medicine-faers" and link["to"]["identifier"] == faers_id]
            if not accepted and not existing:
                result["faers_withheld"].append({
                    "from": source, "faers": faers_id, "reason": "the substance identity is not accepted on both "
                                                                 "sides; no link", "matches": [
                        {"medicine_match": m["match_id"], "medicine_state": m["state"],
                         "faers_match": f["match_id"], "faers_state": f["state"]} for m, f, _ in candidates]})
                continue
            used = accepted[0] if accepted else candidates[0]
            evidence = {"medicine_match": used[0]["match_id"], "faers_match": used[1]["match_id"],
                        "shared_ingredient_rxcuis": used[2], "rxnorm_release": used[0]["rxnorm_release"],
                        "counts": "FAERS reporting counts as already acquired; not incidence, rate or causation",
                        "count_semantics": count_semantics}
            if not accepted:
                evidence["withdrawn"] = "the substance identity is no longer accepted on both sides"
            status = "accepted" if accepted else "rejected"
            applied = self.records.add_link(namespace, {
                "link_kind": "medicine-faers", "from_record": source, "to": {"registry": "openfda",
                                                                              "identifier": faers_id},
                "evidence_kind": "reviewed-substance-identity", "evidence": evidence, "status": status},
                scopes=scopes, observation_id=observation_id)
            result["faers"].append({"link_id": applied["link_id"], "from": source, "faers": faers_id,
                                    "status": status})

    def _add_series(self, namespace, source, doc, evidence_kind, status, evidence, scopes, observation_id):
        target = {"document_id": doc["document_id"], "revision_id": doc["revision_id"],
                  "identifiers": {k: v for k, v in doc["ids"].items() if v}, "title": (doc.get("title") or "")[:500]}
        applied = self.records.add_link(namespace, {
            "link_kind": "series-publication", "from_record": source, "to": target, "evidence_kind": evidence_kind,
            "evidence": {**evidence, "document_source": doc["source_id"]}, "status": status},
            scopes=scopes, observation_id=observation_id)
        return {"link_id": applied["link_id"], "series_id": source["identifier"], "document_id": doc["document_id"],
                "evidence_kind": evidence_kind, "status": status, "created": bool(applied["created"])}

    def series_links(self, namespace, series_id, *, scopes):
        """Accepted publication and trial links of a series, with pending candidates; nothing inferred."""
        _require_read(namespace, scopes)
        links = self.records.links(namespace, provider=SURVEILLANCE, identifier_value=series_id)
        def view(link):
            return {"link_id": link["link_id"], "link_kind": link["link_kind"], "to": link["to"],
                    "evidence_kind": link["evidence_kind"], "evidence": link["evidence"], "status": link["status"],
                    "review": link.get("review")}
        return {"series_id": series_id,
                "publications": [view(link) for link in links if link["link_kind"] == "series-publication"
                                 and link["status"] == "accepted"],
                "trials": [view(link) for link in links if link["link_kind"] == "series-trial"
                           and link["status"] == "accepted"],
                "candidates": [view(link) for link in links if link["status"] == "candidate"],
                "note": "links from explicit dataset citations or reviewed candidates only; a shared condition or "
                        "geography is never a link"}

    def series_claims(self, namespace, series_id, *, scopes):
        """Science-layer claims of the documents linked to a series, beside it; the pack concludes nothing."""
        linked = self.series_links(namespace, series_id, scopes=scopes)["publications"]
        claims = []
        if linked and self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='argument_claims'"
                                        ).fetchone():
            for link in linked:
                for claim_id, text, document_id in self.conn.execute(
                        "SELECT claim_id, claim_text, document_id FROM argument_claims WHERE document_id=? "
                        "ORDER BY claim_id", [link["to"]["document_id"]]).fetchall():
                    claims.append({"claim_id": claim_id, "text": text, "document_id": document_id,
                                   "document_revision_id": link["to"]["revision_id"], "link_id": link["link_id"]})
        return {"series_id": series_id, "view": "science.literature-claims", "claims": claims,
                "note": "claims as the Science claim layer extracted them from the cited documents; shown beside the "
                        "series, no conclusion is drawn from them"}

    # --------------------------------------------------------------- review

    def review_candidate(self, namespace, link_id, decision, rationale, *, principal_id, scopes, observation_id):
        """Accept or reject a text-mention candidate; the review is recorded on a new link revision."""
        if REVIEW_SCOPE not in scopes and "operator" not in scopes:
            raise ClinicalRecordError("unauthorized", "clinical review scope is required")
        if decision not in {"accept", "reject"} or not str(rationale or "").strip():
            raise ClinicalRecordError("invalid_review", "accept or reject with a rationale")
        current = self.records.get(namespace, link_id, scopes=scopes)["record"]
        if current["record_kind"] != "registry-link" or current["status"] != "candidate":
            raise ClinicalRecordError("not_a_candidate", "only candidate links are reviewed")
        body = {k: v for k, v in current.items() if k not in {"contract", "record_kind", "unknowns", "native_version"}}
        body.update(status="accepted" if decision == "accept" else "rejected",
                    review={"principal_id": principal_id, "decision": decision, "rationale": rationale,
                            "annotation_origin": "human"})
        applied = self.records.add_link(namespace, body, scopes=_link_scopes(scopes), observation_id=observation_id)
        return {"link_id": applied["link_id"], "status": body["status"], "review": body["review"]}

    # ----------------------------------------------------------------- read

    def publications(self, namespace, trial_record_id, *, principal_id, scopes):
        """Accepted publications, candidates and family notices (retractions visible) for one trial."""
        _require_read(namespace, scopes)
        trial = self.records.get(namespace, trial_record_id, scopes=scopes)["record"]
        links = [link for link in self.records.links(namespace, provider=trial["registry"],
                                                    identifier_value=trial["identifier"])
                 if link["link_kind"] == "registry-publication"]
        by_doc: dict[str, dict[str, Any]] = {}
        candidates = []
        for link in links:
            if link["status"] == "candidate":
                candidates.append({"link_id": link["link_id"], "document_id": link["to"]["document_id"],
                                   "evidence": link["evidence"]})
                continue
            if link["status"] != "accepted":
                continue
            entry = by_doc.setdefault(link["to"]["document_id"], {
                "document_id": link["to"]["document_id"], "revision_id": link["to"]["revision_id"],
                "identifiers": link["to"].get("identifiers") or {}, "title": link["to"].get("title"),
                "evidence_kinds": [], "link_ids": [], "family_ids": []})
            entry["evidence_kinds"] = sorted({*entry["evidence_kinds"], link["evidence_kind"]})
            entry["link_ids"] = sorted({*entry["link_ids"], link["link_id"]})
            family_id = link["to"].get("family_id") or link["evidence"].get("family_id")
            if family_id:
                entry["family_ids"] = sorted({*entry["family_ids"], family_id})
        families, notices, restricted = {}, [], []
        family_ids = self._family_ids(namespace, trial)
        for family_id in family_ids:
            try:
                families[family_id] = PaperFamilyStore(self.conn, initialize=False).inspect(
                    namespace, family_id, principal_id=principal_id, scopes=scopes, limit=100)
            except PaperFamilyError as exc:
                restricted.append({"family_id": family_id, "code": exc.code})
        for family_id, family in families.items():
            for member in family["members"]:
                entry = by_doc.get(member["source"]["document_id"])
                if entry is not None:
                    entry["family_ids"] = sorted({*entry["family_ids"], family_id})
                    entry["stage"] = member["stage"]
            for notice in family["notices"]:
                target = next((m for m in family["members"] if m["member_id"] == notice["target_member_id"]), None)
                notices.append({"family_id": family_id, "notice_type": notice["notice_type"],
                                "status": notice["status"], "notice_id": notice["notice_id"],
                                "target_document_id": target["source"]["document_id"] if target else None,
                                "target_doi": notice.get("target_doi")})
        for entry in by_doc.values():
            entry["notices"] = [n for n in notices if n["target_document_id"] == entry["document_id"]]
        retractions = [n for n in notices if n["notice_type"] == "retraction"]
        return {"trial_record_id": trial_record_id, "publications": sorted(by_doc.values(), key=lambda e: e["document_id"]),
                "candidates": candidates, "families": sorted(families), "family_access_restricted": restricted,
                "retracted": True if retractions else (None if restricted else False), "retractions": retractions}

    def _family_ids(self, namespace, trial):
        return sorted({link["to"].get("family_id") or link["evidence"].get("family_id")
                       for link in self.records.links(namespace, provider=trial["registry"],
                                                      identifier_value=trial["identifier"])
                       if link["evidence_kind"] == "paper-family" and link["status"] == "accepted"} - {None})

    # ----------------------------------------------------------------- gaps

    def coverage_gaps(self, namespace, *, scopes, trial_record_ids=None, document_ids=None):
        """Unlinked trials, unregistered trial publications, unharvested references, pending candidates."""
        _require_read(namespace, scopes)
        trials = [t for t in self.records.find(namespace, scopes=scopes, kinds={"registered-trial"})
                  if trial_record_ids is None or t["record_id"] in trial_record_ids]
        linked_docs, unlinked, pending, declared = set(), [], [], []
        by_pmid = {}
        docs = self._documents(set(document_ids) if document_ids is not None else None)
        for doc in docs:
            if doc["ids"]["pmid"]:
                by_pmid[doc["ids"]["pmid"]] = doc
        for trial_row in trials:
            trial = trial_row["record"]
            links = [link for link in self.records.links(namespace, provider=trial["registry"],
                                                        identifier_value=trial["identifier"])
                     if link["link_kind"] == "registry-publication"]
            accepted = [link for link in links if link["status"] == "accepted"]
            linked_docs |= {link["to"]["document_id"] for link in links}
            pending += [{"link_id": link["link_id"], "trial": trial["identifier"], "document_id": link["to"]["document_id"]}
                        for link in links if link["status"] == "candidate"]
            if not accepted:
                unlinked.append({"record_id": trial_row["record_id"], "registry": trial["registry"],
                                 "identifier": trial["identifier"],
                                 "reason": "no registry-declared, secondary-source or family evidence links a "
                                           "harvested publication; none is inferred"})
            for reference in trial.get("declared_references") or []:
                if reference.get("pmid") and reference["pmid"] not in by_pmid:
                    declared.append({"trial": trial["identifier"], "reference": reference,
                                     "reason": "registry-declared publication is not among harvested documents"})
        unregistered = [{"document_id": d["document_id"], "title": d["title"], "identifiers": d["ids"],
                         "publication_types": trial_registry.publication_types(d),
                         "reason": "reported as a trial but declares no registry identifier"}
                        for d in docs if trial_registry.is_trial_report(d) and not d["declared"]
                        and not d["mentions"] and d["document_id"] not in linked_docs]
        result = {"unlinked_trials": unlinked, "unregistered_publications": unregistered,
                  "declared_not_harvested": declared, "pending_candidates": pending,
                  "gaps_hash": digest([unlinked, unregistered, declared, pending])}
        if self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='surveillance_series'"
                             ).fetchone():
            result["surveillance"] = self._series_gaps(namespace, docs, scopes)
        return result

    def _series_gaps(self, namespace, docs, scopes):
        """Series without an accepted link, and dataset identifiers documents cite that no series holds."""
        held = self._series_identifiers(namespace)
        unlinked = []
        for series_id, entry in sorted(held.items()):
            links = self.series_links(namespace, series_id, scopes=scopes)
            if not links["publications"] and not links["trials"]:
                unlinked.append({"series_id": series_id, "provider": entry["series"]["provider"],
                                 "condition": entry["series"]["condition"],
                                 "reason": "no document or trial cites this series' dataset; none is inferred"})
        known = {(kind, normalise_identifier(kind, identifier)) for entry in held.values()
                 for kind, identifier in entry["identifiers"]}
        not_held = []
        for doc in docs:
            for field, text in declared_dataset_text(doc):
                scan = re.sub(r"https?://(?:dx\.)?doi\.org/", " ", text, flags=re.I)
                for kind, pattern in DATASET_PATTERNS.items():
                    for match in pattern.finditer(scan):
                        if (kind, normalise_identifier(kind, match.group(0))) not in known:
                            not_held.append({"document_id": doc["document_id"], "field": field, "kind": kind,
                                             "identifier": match.group(0),
                                             "reason": "cited dataset is not held as a surveillance series"})
        return {"unlinked_series": unlinked, "cited_datasets_not_held": not_held}


def _member(doc):
    stage = "preprint" if doc["source_id"] in RXIV_SOURCES else "version-of-record"
    identifiers = [{"kind": "doi", "value": doc["ids"]["doi"]}] if doc["ids"]["doi"] else []
    if doc["ids"]["pmid"]:
        identifiers.append({"kind": "provider", "value": "pmid:" + doc["ids"]["pmid"]})
    if not identifiers:
        identifiers = [{"kind": "provider", "value": doc["document_id"]}]
    return {"document_id": doc["document_id"], "revision_id": doc["revision_id"], "stage": stage,
            "identifiers": identifiers}


def _related(doc):
    raw = doc["metadata"].get("related_resources_json")
    try:
        values = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except ValueError:
        return []
    return [v for v in values if isinstance(v, dict)]


def _link_scopes(scopes):
    return set(scopes)

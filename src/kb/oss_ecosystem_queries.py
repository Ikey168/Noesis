"""Release-state, licence-change and organisation queries as of a date (OS08).

Pure reads over the OSS store. Every answer names its knowledge cutoff (only
revisions observed by then are used), its generation (which changes whenever
any source adds a revision) and its gaps (periods no observation covers).
Sources stay side by side: the package's registry is the primary listing;
deps.dev is shown beside it and disagreements are listed, never merged.
Reasons are quoted verbatim. Licences are quoted and normalised to SPDX
expressions against a pinned list version; they are never interpreted.
Packages are listed per organisation-level publisher only, never per person.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kb.oss_ecosystem_identity import OssIdentity, publisher_key
from src.kb.oss_ecosystem_records import ANSWER_CONTRACT, READ_SCOPE, coordinate
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError, authorize
from src.kb.oss_ecosystem_versions import UnsupportedConstraint, sort_key
from src.kb.oss_ecosystem_views import cutoff_ms, iso, registry_source, release_states
from src.kb.oss_spdx import comparison_key_of

NOTICE = "no quality, health, popularity or trust verdict; no licence interpretation or compliance advice"


def _coordinate(package: str, ecosystem: str | None) -> str:
    return (
        package
        if str(package).startswith("pkg:")
        else coordinate(str(ecosystem or ""), package)
    )


class OssQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = OssEcosystemStore(conn, initialize=False)

    def _frame(
        self, namespace: str, scopes: Iterable[str], as_of: Any, acquired_by: Any
    ) -> tuple[int, int]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready(namespace)
        at = cutoff_ms(as_of, default=2**62)
        acquired = cutoff_ms(acquired_by, default=at)
        return at, acquired

    def _envelope(
        self, namespace: str, answer: str, acquired: int, **body: Any
    ) -> dict[str, Any]:
        latest = acquired >= 2**62
        if latest:
            row = self.conn.execute(
                "SELECT max(observed_at_ms) FROM oss_revisions WHERE namespace=?",
                [namespace],
            ).fetchone()
            acquired = int(row[0] or 0)
        return {
            "contract": ANSWER_CONTRACT,
            "answer": answer,
            "namespace": namespace,
            "knowledge_cutoff_ms": acquired,
            "knowledge_cutoff": iso(acquired),
            "knowledge_cutoff_basis": "latest acquired observation"
            if latest
            else "requested cutoff",
            "generation": self.store.generation(namespace, acquired_by_ms=acquired),
            **body,
            "notice": NOTICE,
        }

    def _last_observation(
        self, namespace: str, coord: str, acquired: int
    ) -> int | None:
        latest = None
        for record in self.store.records(namespace, coordinate=coord):
            for revision in self.store.history(
                record["record_id"], acquired_by_ms=acquired
            ):
                latest = max(latest or 0, revision["observed_at_ms"])
        return latest

    # ------------------------------------------------------------ releases

    def package_release_history(
        self,
        namespace: str,
        package: str,
        *,
        scopes: Iterable[str],
        ecosystem: str | None = None,
        as_of: Any = None,
        acquired_by: Any = None,
        include_revisions: bool = False,
    ) -> dict[str, Any]:
        at, acquired = self._frame(namespace, scopes, as_of, acquired_by)
        coord = _coordinate(package, ecosystem)
        primary = registry_source(coord)
        releases = release_states(
            self.store,
            namespace,
            coord,
            source=primary,
            at_ms=at,
            acquired_by_ms=acquired,
        )
        if include_revisions:
            for release in releases:
                release["revisions"] = [
                    {
                        "revision_id": r["revision_id"],
                        "state": r["statement"]["state"],
                        "reason": r["statement"].get("reason"),
                        "observed_at_ms": r["observed_at_ms"],
                        "source_modified_at": r["statement"].get("source_modified_at"),
                        "late": r["late"],
                    }
                    for r in self.store.history(
                        release["record_id"], acquired_by_ms=acquired
                    )
                ]
        others = {}
        for source in sorted(
            {
                r["source"]
                for r in self.store.records(
                    namespace, record_type="release_state_revision", coordinate=coord
                )
            }
            - {primary}
        ):
            others[source] = release_states(
                self.store,
                namespace,
                coord,
                source=source,
                at_ms=at,
                acquired_by_ms=acquired,
            )
        disagreements = []
        listed = {r["version"]: r for r in releases}
        for source, items in others.items():
            for item in items:
                mine = listed.get(item["version"])
                if mine is None:
                    disagreements.append(
                        {
                            "version": item["version"],
                            source: item["state"],
                            primary: "not listed",
                        }
                    )
                elif mine.get("published_at") != item.get("published_at") and item.get(
                    "published_at"
                ):
                    disagreements.append(
                        {
                            "version": item["version"],
                            "field": "published_at",
                            primary: mine.get("published_at"),
                            source: item["published_at"],
                        }
                    )
        gaps = [{"version": r["version"], **r["gap"]} for r in releases if r.get("gap")]
        last = self._last_observation(namespace, coord, acquired)
        if last is not None and at < 2**62 and at > last:
            gaps.append(
                {
                    "from": iso(last),
                    "to": iso(at),
                    "note": "no observation of this package in this period",
                }
            )
        if not releases:
            gaps.append(
                {"note": "no release of this package was acquired by the cutoff"}
            )
        return self._envelope(
            namespace,
            "package_release_history",
            acquired,
            package=coord,
            as_of=iso(at) if at < 2**62 else "latest",
            source=primary,
            releases=releases,
            other_sources=others,
            disagreements=disagreements,
            gaps=gaps,
            publishers=self._publishers(namespace, coord, acquired),
            repository=self._repository(namespace, coord, acquired),
        )

    def _publishers(
        self, namespace: str, coord: str, acquired: int
    ) -> list[dict[str, Any]]:
        out = []
        for record in self.store.records(
            namespace, record_type="publisher_organisation", coordinate=coord
        ):
            revision = self.store.current(record["record_id"], acquired_by_ms=acquired)
            if revision is None:
                continue
            for organisation in revision["statement"]["organisations"]:
                out.append(
                    {
                        **organisation,
                        "source": record["source"],
                        "revision_id": revision["revision_id"],
                    }
                )
        return out

    def _repository(self, namespace: str, coord: str, acquired: int) -> dict[str, Any]:
        identity = OssIdentity(self.conn, initialize=False)
        reviewed = identity.reviewed_repositories(namespace, coord)
        asserted = []
        for record in self.store.records(
            namespace, record_type="repository_link_assertion", coordinate=coord
        ):
            revision = self.store.current(record["record_id"], acquired_by_ms=acquired)
            for link in (revision or {}).get("statement", {}).get("links") or []:
                asserted.append(
                    {
                        **link,
                        "source": record["source"],
                        "version": record["version"] or None,
                        "revision_id": revision["revision_id"],
                    }
                )
        archive = []
        for candidate in reviewed:
            repo = candidate["right_key"].split(":", 1)[1]
            for record in self.store.records(
                namespace, record_type="archive_provenance", repository_key=repo
            ):
                revision = self.store.current(
                    record["record_id"], acquired_by_ms=acquired
                )
                if revision is None:
                    continue
                statement = revision["statement"]
                archive.append(
                    {
                        k: statement[k]
                        for k in (
                            "detail",
                            "origin_url",
                            "visit",
                            "visit_date",
                            "visit_status",
                            "snapshot_swhid",
                            "branches",
                        )
                        if k in statement
                    }
                    | {"revision_id": revision["revision_id"]}
                )
        return {
            "reviewed": [
                {
                    "repository": c["right_key"].split(":", 1)[1],
                    "candidate_id": c["candidate_id"],
                    "decision_id": c["decision_id"],
                    "basis": c["basis"],
                    **(
                        {"shared_claim": c["shared_claim"]}
                        if c.get("shared_claim")
                        else {}
                    ),
                }
                for c in reviewed
            ],
            "asserted_by_sources": asserted,
            "archive": sorted(
                archive, key=lambda a: (a.get("detail", ""), a.get("visit", 0))
            ),
            "note": "a repository is the package's only through a reviewed decision; assertions are shown as "
            "the sources state them",
        }

    # ------------------------------------------------------------ licences

    def licence_history(
        self,
        namespace: str,
        package: str,
        *,
        scopes: Iterable[str],
        ecosystem: str | None = None,
        list_version: str | None = None,
        acquired_by: Any = None,
    ) -> dict[str, Any]:
        _, acquired = self._frame(namespace, scopes, None, acquired_by)
        coord = _coordinate(package, ecosystem)
        eco = coord.split(":")[1]
        primary = registry_source(coord)
        pinned = list_version or (self.store.spdx_versions(namespace) or [None])[-1]
        if pinned is not None and pinned not in self.store.spdx_versions(namespace):
            raise OssStoreError(
                "unknown_spdx_list", f"SPDX License List {pinned} has not been acquired"
            )
        by_version: dict[str, dict[str, dict[str, Any]]] = {}
        for record in self.store.records(
            namespace, record_type="licence_declaration_revision", coordinate=coord
        ):
            history = self.store.history(record["record_id"], acquired_by_ms=acquired)
            if not history:
                continue
            revision = history[-1]
            normalised = self.store.normalisation(revision["revision_id"], pinned)
            by_version.setdefault(record["version"], {})[record["source"]] = {
                "source": record["source"],
                "revision_id": revision["revision_id"],
                "observed_at_ms": revision["observed_at_ms"],
                "raw": revision["statement"]["raw"],
                "normalised": normalised,
                "revisions_observed": len(history),
            }

        def order(version: str) -> tuple[int, Any]:
            try:
                return (0, sort_key(eco, version))
            except UnsupportedConstraint:
                return (1, version)

        releases, changes, disagreements = [], [], []
        previous = None
        for version in sorted(by_version, key=order):
            sources = by_version[version]
            entry = {"version": version, "declarations": sources}
            registry = sources.get(primary)
            if registry is not None:
                key = comparison_key_of(registry["normalised"])
                if previous is not None:
                    if key is None or previous[1] is None:
                        entry["change"] = (
                            "unknown: one side is not a normalised expression"
                        )
                    elif key != previous[1]:
                        entry["change"] = "changed"
                        changes.append(
                            {
                                "from_version": previous[0],
                                "to_version": version,
                                "from": previous[2],
                                "to": registry["normalised"].get("expression"),
                                "spdx_list_version": pinned,
                                "cites": [previous[3], registry["revision_id"]],
                            }
                        )
                previous = (
                    version,
                    key,
                    registry["normalised"].get("expression"),
                    registry["revision_id"],
                )
            keys = {
                s: comparison_key_of(d["normalised"]) or d["raw"]
                for s, d in sources.items()
            }
            if len({str(v) for v in keys.values()}) > 1:
                disagreements.append(
                    {
                        "version": version,
                        **{
                            s: d["normalised"].get("expression") or d["raw"]
                            for s, d in sources.items()
                        },
                    }
                )
            releases.append(entry)
        gaps = (
            []
            if releases
            else [{"note": "no licence declaration was acquired by the cutoff"}]
        )
        if pinned is None:
            gaps.append(
                {
                    "note": "no SPDX License List release acquired; declarations are quoted, not normalised"
                }
            )
        return self._envelope(
            namespace,
            "licence_history",
            acquired,
            package=coord,
            source=primary,
            spdx_list_version=pinned,
            releases=releases,
            changes=changes,
            source_disagreements=disagreements,
            gaps=gaps,
        )

    # ------------------------------------------------------------ organisations

    def packages_by_organisation(
        self,
        namespace: str,
        organisation: str,
        *,
        scopes: Iterable[str],
        acquired_by: Any = None,
    ) -> dict[str, Any]:
        """Packages an organisation-level publisher declares; npm scopes only once reviewed as an organisation."""

        _, acquired = self._frame(namespace, scopes, None, acquired_by)
        wanted = str(organisation or "").strip()
        if not wanted:
            raise OssStoreError(
                "bad_request",
                "name an organisation, a declared organisation id or an entity:<id>",
            )
        identity = OssIdentity(self.conn, initialize=False)
        accepted = {
            c["left_key"]: c["right_key"]
            for c in identity.candidates(
                namespace, scopes={"operator"}, kind="organisation", state="accepted"
            )
        }
        folded = wanted.casefold()
        packages, excluded = [], []
        for record in self.store.records(
            namespace, record_type="publisher_organisation"
        ):
            revision = self.store.current(record["record_id"], acquired_by_ms=acquired)
            if revision is None:
                continue
            for item in revision["statement"]["organisations"]:
                key = publisher_key(record["source"], item["kind"], item["id"])
                matched = (
                    (wanted.startswith("entity:") and accepted.get(key) == wanted)
                    or folded
                    in {item["id"].casefold(), str(item.get("name") or "").casefold()}
                    or (
                        item["kind"] == "crates-team"
                        and folded == item["id"].split(":")[1].casefold()
                        if item["id"].count(":") >= 2
                        else False
                    )
                )
                if not matched:
                    continue
                if item["kind"] == "npm-scope" and key not in accepted:
                    excluded.append(
                        {
                            "package": record["coordinate"],
                            "declaration": item,
                            "reason": "npm does not state whether a scope is an organisation; shown only "
                            "after a reviewed organisation match",
                        }
                    )
                    continue
                packages.append(
                    {
                        "package": record["coordinate"],
                        "source": record["source"],
                        "declaration": item,
                        "revision_id": revision["revision_id"],
                        **(
                            {"reviewed_entity": accepted[key]}
                            if key in accepted
                            else {}
                        ),
                    }
                )
        return self._envelope(
            namespace,
            "packages_by_organisation",
            acquired,
            organisation=wanted,
            packages=sorted(packages, key=lambda p: (p["package"], p["source"])),
            excluded=excluded,
            gaps=[]
            if packages
            else [
                {"note": "no acquired publisher declaration names this organisation"}
            ],
        )


def package_release_history(
    conn: Any, namespace: str, package: str, **options: Any
) -> dict[str, Any]:
    return OssQueries(conn).package_release_history(namespace, package, **options)


def licence_history(
    conn: Any, namespace: str, package: str, **options: Any
) -> dict[str, Any]:
    return OssQueries(conn).licence_history(namespace, package, **options)


def packages_by_organisation(
    conn: Any, namespace: str, organisation: str, **options: Any
) -> dict[str, Any]:
    return OssQueries(conn).packages_by_organisation(namespace, organisation, **options)


__all__ = [
    "OssQueries",
    "licence_history",
    "package_release_history",
    "packages_by_organisation",
]

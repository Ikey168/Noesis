"""Scoped acquisition/import entry point for native EU/German regional providers.

Uses the existing captured-response ledger and document revision pipeline. An
empty discovery result is not an ingestion failure; a failed attempt never
becomes an empty success. CTIS/DRKS exports retain their declared native schema.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from src.ingestion.provider_execution import ProviderError, canonical, digest
from src.ingestion.regional_providers import (
    RegionalClient,
    RegionalEvidenceStore,
    parse_berlin_juris_html,
    parse_berlin_juris_xml,
    parse_berlin_publication,
    parse_ctis_search_csv,
    parse_drks_public_json,
    parse_drks_who_xml,
    parse_registry_export,
    parse_trial_document,
)

OPERATIONS = {
    "german-courts": {"court_index", "court_decision"},
    "ema": {"ema_medicines", "ema_documents", "acquire_document"},
    "bfarm": {"bfarm_notices", "linked_documents", "acquire_document"},
    "cellar": {"cellar", "acquire_document"},
    "berlin-law": {"linked_documents", "berlin_publication"},
    "opencorporates": {"company", "companies"},
    "opensanctions": {"sanctions_match", "sanctions_dataset"},
    "ctis": set(),
    "drks": set(),
}


class RegionalAcquisition:
    def __init__(
        self,
        conn,
        *,
        namespace,
        principal_id,
        reuse_notice,
        client: RegionalClient | None = None,
    ):
        if any(
            not isinstance(v, str) or not v or len(v) > 2000
            for v in (namespace, principal_id, reuse_notice)
        ):
            raise ValueError("explicit namespace, principal and reuse policy required")
        self.conn, self.namespace, self.principal_id = conn, namespace, principal_id
        self.reuse_notice, self.client = reuse_notice, client
        self.store = RegionalEvidenceStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS regional_workflow_runs(namespace TEXT,owner TEXT,observation TEXT,request_hash TEXT,result_json TEXT,PRIMARY KEY(namespace,owner,observation))"
        )

    def _auth(self, scopes):
        self.store.authorize(self.namespace, self.principal_id, scopes)

    @contextmanager
    def _publication(self):
        """Commit documents, revisions and the operator replay receipt together."""
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _replay(self, observation, request):
        if not isinstance(observation, str) or not 1 <= len(observation) <= 256:
            raise ValueError("bounded explicit observation identifier required")
        row = self.conn.execute(
            "SELECT request_hash,result_json FROM regional_workflow_runs WHERE namespace=? AND owner=? AND observation=?",
            [self.namespace, self.principal_id, observation],
        ).fetchone()
        if row:
            if row[0] != digest(request):
                raise ProviderError(
                    "observation_conflict",
                    "this observation is bound to another acquisition/import",
                )
            return {**json.loads(row[1]), "replayed": True}
        return None

    def replay(self, observation, *, provider, scopes):
        """Read an authorized persisted observation without a client or source file."""
        self._auth(scopes)
        if not isinstance(observation, str) or not 1 <= len(observation) <= 256:
            raise ValueError("bounded explicit observation identifier required")
        row = self.conn.execute(
            "SELECT result_json FROM regional_workflow_runs WHERE namespace=? AND owner=? AND observation=?",
            [self.namespace, self.principal_id, observation],
        ).fetchone()
        if not row:
            raise ProviderError(
                "observation_unavailable", "no captured observation in this scope"
            )
        result = json.loads(row[0])
        if result["provider"] != provider:
            raise ProviderError(
                "observation_conflict", "observation belongs to another provider"
            )
        return {**result, "replayed": True}

    def _finish(self, observation, request, result):
        result = {
            "contract": "noesis-regional-acquisition-v1",
            "namespace": self.namespace,
            "observation": observation,
            "source_evidence_not_independent_validation": True,
            **result,
        }
        self.conn.execute(
            "INSERT INTO regional_workflow_runs VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                self.namespace,
                self.principal_id,
                observation,
                digest(request),
                canonical(result),
            ],
        )
        return self._replay(observation, request) | {"replayed": False}

    def _store(self, records, captured, scopes):
        if not records:
            return {"document_ids": [], "native_capture": captured.receipt}
        return self.store.ingest(
            records,
            captured,
            namespace=self.namespace,
            principal_id=self.principal_id,
            scopes=scopes,
            reuse_notice=self.reuse_notice,
            manage_transaction=False,
        )

    def acquire(self, operation, parameters, observation, *, scopes):
        self._auth(scopes)
        if self.client is None:
            raise ProviderError(
                "provider_unavailable",
                "network acquisition requires an explicitly configured native client",
            )
        provider = self.client.http.provider
        if (
            operation not in OPERATIONS[provider]
            or not isinstance(parameters, dict)
            or len(canonical(parameters).encode()) > 262144
            or "observation" in parameters
        ):
            raise ValueError("unsupported regional operation or invalid parameters")
        request = {
            "provider": provider,
            "operation": operation,
            "parameters": parameters,
            "reuse_notice": self.reuse_notice,
            "budget_id": self.client.http.budget_id,
        }
        previous = self._replay(observation, request)
        if previous:
            return previous
        key = digest([self.namespace, self.principal_id, observation, request])
        if operation == "berlin_publication":
            allowed = {
                "source_url",
                "official_id",
                "kind",
                "title",
                "publication_date",
                "effective_from",
                "historical",
                "amendments",
            }
            if set(parameters) - allowed or not {
                "source_url",
                "official_id",
                "kind",
                "title",
            } <= set(parameters):
                raise ValueError(
                    "explicit bounded Berlin publication identity and kind required"
                )
            captured = self.client._fetch(
                key + ":publication", parameters["source_url"], max_bytes=20_000_000
            )
            native = [parse_berlin_publication(captured.content, **parameters)]
        else:
            # This is a closed native-method set, never a caller-supplied module
            # or arbitrary HTTP method/endpoint. Each adapter validates its fields.
            native, captured = getattr(self.client, operation)(
                observation=key, **parameters
            )
        records = native if isinstance(native, list) else native.get("records", [])
        if records and records[0].get("contract") != "noesis-native-regional-v1":
            # Index/discovery rows aren't document evidence until acquired.
            records = []
        # Metadata and pagination flags do not by themselves mean that the
        # provider returned a discovery candidate.
        discovery = (
            next(
                (
                    native[name]
                    for name in ("records", "documents", "decisions")
                    if name in native
                ),
                [],
            )
            if isinstance(native, dict)
            else native
        )
        with self._publication():
            receipt = self._store(records, captured, scopes)
            return self._finish(
                observation,
                request,
                {
                    "provider": provider,
                    "operation": operation,
                    "status": "ingested"
                    if records
                    else "discovered"
                    if discovery
                    else "empty",
                    "native_result": native,
                    "receipt": receipt,
                    "cursor": {
                        key: native[key]
                        for key in ("next_page", "next_offset", "truncated")
                        if isinstance(native, dict) and key in native
                    },
                    "automatic_identity_merge": False,
                },
            )

    def import_export(
        self, provider, raw, options, observation, *, scopes, expected_sha256
    ):
        self._auth(scopes)
        if (
            provider not in {"ctis", "drks", "berlin-law"}
            or not isinstance(raw, bytes)
            or not 0 < len(raw) <= 20_000_000
        ):
            raise ValueError("bounded CTIS/DRKS/Berlin export bytes required")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ProviderError(
                "source_changed", "export differs from the explicitly selected snapshot"
            )
        if not isinstance(options, dict) or len(canonical(options).encode()) > 262144:
            raise ValueError("bounded native export mapping required")
        request = {
            "provider": provider,
            "sha256": expected_sha256,
            "options": options,
            "reuse_notice": self.reuse_notice,
        }
        previous = self._replay(observation, request)
        if previous:
            return previous
        params = dict(options)
        observed_at = params.pop("observed_at_ms", None)
        if provider in {"ctis", "drks"} and params.get("format") == "trial-document":
            params.pop("format")
            records = [parse_trial_document(provider, raw, **params)]
        elif provider == "berlin-law" and params.get("format") == "berlin-juris-html":
            params.pop("format")
            records = parse_berlin_juris_html(raw, **params)
        elif provider == "berlin-law" and params.get("format") == "berlin-juris-xml":
            params.pop("format")
            records = parse_berlin_juris_xml(raw, **params)
        elif provider == "berlin-law":
            records = [parse_berlin_publication(raw, **params)]
        elif params.get("format") == "ctis-search-csv" and provider == "ctis":
            params.pop("format")
            records = parse_ctis_search_csv(raw, **params)
        elif params.get("format") == "drks-public-json" and provider == "drks":
            params.pop("format")
            records = parse_drks_public_json(raw, **params)
        elif params.get("format") == "who-xml" and provider == "drks":
            params.pop("format")
            records = parse_drks_who_xml(raw, **params)
        else:
            records = parse_registry_export(provider, raw, **params)
        with self._publication():
            receipt = self.store.import_bytes(
                provider,
                raw,
                records,
                source_url=options["source_url"],
                namespace=self.namespace,
                principal_id=self.principal_id,
                scopes=scopes,
                reuse_notice=self.reuse_notice,
                observed_at_ms=observed_at,
                manage_transaction=False,
            )
            return self._finish(
                observation,
                request,
                {
                    "status": "ingested",
                    "provider": provider,
                    "receipt": receipt,
                    "record_count": len(records),
                    "automatic_identity_merge": False,
                },
            )

    def import_file(
        self, path, options, observation, *, provider, scopes, expected_sha256
    ):
        self._auth(scopes)
        with Path(path).open("rb") as stream:
            raw = stream.read(20_000_001)
        return self.import_export(
            provider,
            raw,
            options,
            observation,
            scopes=scopes,
            expected_sha256=expected_sha256,
        )

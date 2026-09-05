"""Bounded comparisons against authoritative committed evidence revisions."""
import json

import duckdb

from src.kb.research_projects import _hash


class EvidenceResolver:
    def __init__(self, conn, scopes):
        self.conn, self.scopes = conn, scopes

    def _one(self, sql, parameters):
        row = self.conn.execute(sql, parameters).fetchone()
        return json.loads(row[0]) if row else None

    def compare(self, dependency):
        dep = dict(dependency)
        result = {'dependency': dep, 'status': 'uncertain', 'reason': 'revision_unavailable', 'before': None, 'after': None}
        ns, identity, revision, kind = (dep[k] for k in ('namespace', 'id', 'revision', 'kind'))
        if 'operator' not in self.scopes and not {f'namespace:{ns}:read', f'namespace:{ns}:write'} & self.scopes:
            return {**result, 'reason': 'access_unavailable'}
        if kind == 'source' and 'operator' not in self.scopes and f'document:{identity}:read' not in self.scopes:
            return {**result, 'reason': 'document_access_unavailable'}
        if kind == 'calculation' and 'operator' not in self.scopes and 'knowledge:quantitative:read' not in self.scopes:
            return {**result, 'reason': 'calculation_access_unavailable'}
        if kind == 'entity' and 'operator' not in self.scopes and 'knowledge:entity-history:read' not in self.scopes:
            return {**result, 'reason': 'entity_access_unavailable'}
        try:
            before, after, pending, extra = getattr(self, '_' + kind)(ns, identity, revision)
        except (duckdb.CatalogException, duckdb.BinderException):
            return {**result, 'reason': 'evidence_store_unavailable'}
        result.update(before=before, after=after)
        if before is None or after is None:
            return result
        changed = before != after or bool(extra)
        reason = 'revision_changed' if changed else 'same_committed_revision'
        if after.get('lifecycle') in {'retracted', 'deleted', 'withdrawn'} or after.get('source_retracted'):
            reason = 'confirmed_withdrawal'
            changed = True
        if kind == 'entity' and changed:
            reason = 'published_identity_decision'
        if kind == 'calculation' and extra:
            reason = 'calculation_inputs_revised'
        result.update(status='affected' if changed else 'current', reason=reason, coverage='incomplete' if pending else 'complete_for_declared_dependency', details=extra)
        if pending:
            result.update(status='affected' if changed else 'uncertain', reason=reason if changed else 'uncommitted_evidence_pending')
        result['assessment_hash'] = _hash(result)
        return result

    def _source(self, ns, identity, revision):
        base = 'SELECT to_json(r) FROM document_revision_records r WHERE document_id=? AND committed_watermark IS NOT NULL'
        before = self._one(base + ' AND revision_id=?', [identity, revision])
        after = self._one(base + ' ORDER BY revision DESC LIMIT 1', [identity])
        pending = self.conn.execute('SELECT 1 FROM document_revision_records WHERE document_id=? AND committed_watermark IS NULL LIMIT 1', [identity]).fetchone() is not None
        return before, after, pending, []

    def _artifact(self, ns, identity, revision):
        base = 'SELECT to_json(a) FROM knowledge_artifacts a WHERE namespace=? AND (logical_id=? OR artifact_id=?)'
        before = self._one(base + " AND (artifact_id=? OR content_hash=?) AND status NOT IN ('staged','pending') ORDER BY generation DESC LIMIT 1", [ns, identity, identity, revision, revision])
        if before is None:
            return None, None, False, []
        after = self._one("SELECT to_json(a) FROM knowledge_artifacts a WHERE namespace=? AND logical_id=? AND status='active' ORDER BY generation DESC LIMIT 1", [ns, before['logical_id']])
        pending = self.conn.execute("SELECT 1 FROM knowledge_artifacts WHERE namespace=? AND logical_id=? AND status IN ('staged','pending') LIMIT 1", [ns, before['logical_id']]).fetchone() is not None
        return before, after, pending or not (after or {}).get('lineage_complete', False), []

    def _claim(self, ns, identity, revision):
        if revision.startswith('claim-state:'):
            base = 'SELECT to_json(s) FROM claim_timeline_states s WHERE namespace=? AND claim_id=?'
            return self._one(base+' AND state_id=?', [ns, identity, revision]), self._one(base+' ORDER BY revision DESC LIMIT 1', [ns, identity]), False, []
        base = '''SELECT to_json(r) FROM derived_object_revisions r JOIN derived_object_generations g
            ON g.namespace=r.namespace AND g.generation=r.generation
            WHERE r.namespace=? AND r.logical_id=? AND r.object_type='claim' AND g.status='committed' '''
        before = self._one(base+' AND r.revision_id=?', [ns, identity, revision])
        after = self._one(base+' ORDER BY r.revision DESC LIMIT 1', [ns, identity])
        pending = self.conn.execute("SELECT 1 FROM derived_object_revisions r LEFT JOIN derived_object_generations g ON g.namespace=r.namespace AND g.generation=r.generation WHERE r.namespace=? AND r.logical_id=? AND coalesce(g.status,'pending')!='committed' LIMIT 1", [ns, identity]).fetchone() is not None
        return before, after, pending, []

    def _entity(self, ns, identity, revision):
        base = '''SELECT to_json(d) FROM entity_identity_decisions d
            WHERE d.namespace=? AND list_contains(CAST(d.subject_ids_json AS VARCHAR[]),?)
            AND EXISTS (SELECT 1 FROM entity_history_publications p WHERE p.namespace=d.namespace AND p.decision_id=d.decision_id AND p.status='published')'''
        before = self._one(base+' AND d.decision_id=?', [ns, identity, revision])
        after = self._one(base+''' ORDER BY (SELECT max(p.created_at_ms) FROM entity_history_publications p WHERE p.decision_id=d.decision_id AND p.status='published') DESC, d.created_at_ms DESC,d.revision DESC,d.decision_id DESC LIMIT 1''' , [ns, identity])
        pending = self.conn.execute('''SELECT 1 FROM entity_identity_decisions d WHERE d.namespace=? AND list_contains(CAST(d.subject_ids_json AS VARCHAR[]),?)
            AND NOT EXISTS (SELECT 1 FROM entity_history_publications p WHERE p.namespace=d.namespace AND p.decision_id=d.decision_id AND p.status='published') LIMIT 1''', [ns, identity]).fetchone() is not None
        return before, after, pending, []

    def _calculation(self, ns, identity, revision):
        before = self._one('SELECT to_json(c) FROM quantitative_calculations c WHERE namespace=? AND calculation_id=? AND (calculation_id=? OR calculation_hash=?)', [ns, identity, revision, revision])
        if before is None:
            return None, None, False, []
        inputs = json.loads(before['input_ids_json'])
        if before.get('formula_revision_id'):
            inputs.append(before['formula_revision_id'])
        if len(inputs) > 1000:
            return before, before, True, []
        changes, pending = [], False
        for input_id in inputs:
            original = self._one('SELECT to_json(o) FROM quantitative_observations o WHERE namespace=? AND observation_id=?', [ns, input_id])
            if original:
                latest = self._one('''SELECT to_json(o) FROM quantitative_observations o WHERE namespace=? AND metric_id=? AND provider=? AND provider_series_id=? AND period=?
                    ORDER BY release_at_ms DESC,retrieved_at_ms DESC,observation_id DESC LIMIT 1''', [ns, original['metric_id'], original['provider'], original['provider_series_id'], original['period']])
                if latest != original:
                    changes.append({'input_id': input_id, 'before': original, 'after': latest})
            else:
                metric = self._one('SELECT to_json(r) FROM quantitative_metric_revisions r WHERE namespace=? AND revision_id=?', [ns, input_id])
                if metric:
                    latest = self._one('SELECT to_json(r) FROM quantitative_metric_revisions r WHERE namespace=? AND metric_id=? ORDER BY revision DESC LIMIT 1', [ns, metric['metric_id']])
                    if latest != metric:
                        changes.append({'input_id': input_id, 'before': metric, 'after': latest})
                else:
                    pending = True
        return before, before, pending, changes

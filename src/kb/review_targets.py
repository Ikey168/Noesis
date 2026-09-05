"""Adapters from inbox references to existing domain review APIs."""
import json

from src.kb.research_projects import _hash


class ReviewTargetError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ReviewTargets:
    SCOPES = {'entity': 'knowledge:entity-history:read', 'translation': 'knowledge:cross-language:read',
              'quality': 'knowledge:quality:read', 'extraction': 'knowledge:read'}

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql, params):
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            raise ReviewTargetError('target_unavailable', 'review target is unavailable')
        return json.loads(row[0])

    def inspect(self, target, *, scopes):
        if not isinstance(target, dict) or set(target) != {'kind', 'namespace', 'id'} or target['kind'] not in self.SCOPES:
            raise ReviewTargetError('invalid_target', 'target requires supported kind, namespace and id')
        kind, ns, identity = (target[k] for k in ('kind', 'namespace', 'id'))
        if 'operator' not in scopes and (self.SCOPES[kind] not in scopes or not {f'namespace:{ns}:read', f'namespace:{ns}:write'} & scopes):
            raise ReviewTargetError('unauthorized', 'current target namespace and domain read scope required')
        if kind == 'entity':
            row = self._one('SELECT to_json(d) FROM entity_identity_decisions d WHERE namespace=? AND decision_id=?', [ns, identity])
            current = self._one('SELECT to_json(d) FROM entity_identity_decisions d WHERE namespace=? AND event_key=? ORDER BY revision DESC LIMIT 1', [ns, row['event_key']])
            subjects = json.loads(row['subject_ids_json'])
            identities = []
            for subject in subjects:
                identities.append(self._one('SELECT to_json(e) FROM entity_history_identities e WHERE namespace=? AND entity_id=?', [ns, subject]))
            # Identity decisions outside this event can change a subject too.
            latest = self.conn.execute('SELECT decision_id FROM entity_identity_decisions WHERE namespace=? AND list_has_any(CAST(subject_ids_json AS VARCHAR[]),?) ORDER BY created_at_ms,decision_id LIMIT 1001', [ns, subjects]).fetchall()
            if len(latest) > 1000:
                raise ReviewTargetError('target_budget_exceeded', 'entity history exceeds inbox comparison bound')
            context = {'identities': identities, 'decisions': [v[0] for v in latest]}
        elif kind == 'translation':
            row = self._one('SELECT to_json(t) FROM translation_records t WHERE namespace=? AND translation_id=?', [ns, identity])
            current = self._one('SELECT to_json(t) FROM translation_records t WHERE namespace=? AND source_text_id=? AND target_language=? ORDER BY version DESC LIMIT 1', [ns, row['source_text_id'], row['target_language']])
            source = self._one('SELECT to_json(t) FROM language_texts t WHERE namespace=? AND text_id=?', [ns, row['source_text_id']])
            latest = self._one('SELECT to_json(t) FROM language_texts t WHERE namespace=? AND object_type=? AND object_id=? ORDER BY revision DESC LIMIT 1', [ns, source['object_type'], source['object_id']])
            context = {'source': source, 'current_source': latest}
        elif kind == 'quality':
            row = self._one('SELECT to_json(a) FROM quality_assessments a WHERE namespace=? AND assessment_id=?', [ns, identity])
            current = self._one('SELECT to_json(a) FROM quality_assessments a WHERE namespace=? AND object_type=? AND object_id=? ORDER BY generation DESC,created_at_ms DESC,assessment_id DESC LIMIT 1', [ns, row['object_type'], row['object_id']])
            overrides = self.conn.execute('SELECT to_json(o) FROM quality_overrides o WHERE namespace=? AND object_id=? ORDER BY override_id LIMIT 1001', [ns, row['object_id']]).fetchall()
            if len(overrides) > 1000:
                raise ReviewTargetError('target_budget_exceeded', 'quality override history exceeds inbox bound')
            context = {'overrides': [json.loads(v[0]) for v in overrides]}
        else:
            base = '''SELECT to_json(r) FROM derived_object_revisions r JOIN derived_object_generations g
                ON g.namespace=r.namespace AND g.generation=r.generation WHERE r.namespace=? AND g.status='committed' '''
            row = self._one(base+' AND r.revision_id=?', [ns, identity])
            current = self._one(base+' AND r.logical_id=? ORDER BY r.revision DESC LIMIT 1', [ns, row['logical_id']])
            context = {}
        result = {'target': target, 'record': row, 'current': current, 'context': context}
        if len(json.dumps(result).encode()) > 4*1024*1024:
            raise ReviewTargetError('target_budget_exceeded', 'review target exceeds 4 MiB')
        return {**result, 'revision_hash': _hash(result), 'superseded': row != current or kind == 'translation' and context['source'] != context['current_source']}

    @staticmethod
    def validate_label(kind, label):
        if not isinstance(label, dict):
            raise ReviewTargetError('invalid_label', 'review label must be a structured object')
        if kind in {'entity', 'translation', 'extraction'}:
            values = {'entity': {'match', 'non-match', 'uncertain'}, 'translation': {'accepted', 'rejected', 'disputed'}, 'extraction': {'correct', 'incorrect', 'uncertain'}}[kind]
            if set(label) != {'decision'} or label['decision'] not in values:
                raise ReviewTargetError('invalid_label', 'unsupported decision label')
        else:
            from src.kb.knowledge_quality import DIMENSIONS
            if set(label) != {'dimension', 'value'} or label['dimension'] not in DIMENSIONS or type(label['value']) not in {int, float} or not 0 <= label['value'] <= 1:
                raise ReviewTargetError('invalid_label', 'quality review requires a known dimension and value from zero to one')
        return label

    def route(self, target, label, *, rationale, principal_id, scopes, task_id):
        state = self.inspect(target, scopes=scopes)
        self.validate_label(target['kind'], label)
        kind, ns = target['kind'], target['namespace']
        if kind == 'entity':
            from src.kb.entity_history import EntityHistoryStore
            return EntityHistoryStore(self.conn, initialize=False).decide(ns,
                'review' if label['decision'] == 'uncertain' else label['decision'], json.loads(state['record']['subject_ids_json']),
                {'rationale': rationale, 'inbox_task_id': task_id, 'decision': label['decision'], 'baseline_decision_id': target['id']},
                event_key=state['record']['event_key'], reviewer_id=principal_id, principal_id=principal_id, scopes=scopes)
        if kind == 'translation':
            from src.kb.cross_language import CrossLanguageStore
            return CrossLanguageStore(self.conn, initialize=False).review_translation(ns, target['id'], label['decision'], principal_id,
                rationale=rationale, principal_id=principal_id, scopes=scopes)
        if kind == 'quality':
            from src.kb.knowledge_quality import QualityStore
            return QualityStore(self.conn, initialize=False).override(ns, state['record']['object_id'], label['dimension'], label['value'], rationale,
                reviewer_id=principal_id, principal_id=principal_id, scopes=scopes)
        # Extraction annotations feed explicit dataset releases; they never
        # replace a mined claim by writing an alternate claim truth table.
        return {'status': 'annotation_recorded', 'target': target, 'label': label, 'automatic_retraining': False}

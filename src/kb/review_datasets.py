"""Explicit annotation dataset releases with related-document split guards."""
import json

from src.kb.research_projects import _hash, _json
from src.kb.review_inbox import ReviewInboxStore, _text
from src.kb.review_targets import ReviewTargets, ReviewTargetError

DATASET_SCOPE = 'knowledge:inbox:datasets'
_DDL = '''
CREATE TABLE IF NOT EXISTS review_dataset_releases(
 release_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,status TEXT NOT NULL,content_json TEXT NOT NULL,release_json TEXT);
CREATE TABLE IF NOT EXISTS review_dataset_split_guards(
 namespace TEXT NOT NULL,group_token TEXT NOT NULL,split TEXT NOT NULL,release_id TEXT NOT NULL,PRIMARY KEY(namespace,group_token));
'''


class ReviewDatasetStore(ReviewInboxStore):
    def __init__(self, conn, *, initialize=True, now=None):
        super().__init__(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    def build_dataset(self, namespace, task_ids, *, principal_id, scopes):
        if 'operator' not in scopes and DATASET_SCOPE not in scopes:
            raise ReviewTargetError('unauthorized', 'review dataset scope required')
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 1000 or len(set(task_ids)) != len(task_ids):
            raise ReviewTargetError('invalid_dataset', 'one to 1000 distinct review task ids required')
        rows, excluded = [], []
        for identity in sorted(task_ids):
            task = self.inspect(namespace, identity, principal_id=principal_id, scopes=scopes)
            self._authorize(task, principal_id, scopes, DATASET_SCOPE, coordinator=True)
            resolution = task['resolution']
            if not resolution:
                excluded.append({'task_id': identity, 'reason': 'unresolved', 'votes': task['votes']})
                continue
            if not resolution['agreement'] or not resolution['all_declared_human']:
                excluded.append({'task_id': identity, 'reason': 'disputed' if not resolution['agreement'] else 'machine_annotation', 'resolution': resolution, 'votes': task['votes']})
                continue
            current = ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)
            if current['revision_hash'] != resolution['routed_target_hash']:
                excluded.append({'task_id': identity, 'reason': 'target_changed_after_review'})
                continue
            if self._sources(task['sources'], scopes) != task['source_fingerprints']:
                excluded.append({'task_id': identity, 'reason': 'source_changed_after_review'})
                continue
            tokens = ['document:'+v['document_id'] for v in task['source_fingerprints']]
            tokens += ['content:'+v['content_hash'] for v in task['source_fingerprints']]
            tokens += ['declared:'+v for v in task['related_groups']]
            record = current['record']
            kind = task['target']['kind']
            if kind == 'entity':
                tokens += ['entity:'+v for v in json.loads(record['subject_ids_json'])]
            else:
                tokens.append(kind+':'+str(record.get('source_text_id') or record.get('object_id') or record.get('logical_id')))
            rows.append({'task_id': identity, 'target': task['target'], 'target_revision_hash': task['target_revision_hash'],
                'sources': task['source_fingerprints'], 'group_tokens': sorted(set(tokens)), 'label': resolution['label'],
                'annotators': task['votes'], 'agreement': True, 'resolution': resolution,
                'self_reported_effort_ms': sum(v['effort_ms'] for v in task['votes'])})
        # Connected components cover revisions, shared content, shared entities,
        # and curator-declared related publications before assigning any split.
        parent = {}
        def find(value):
            parent.setdefault(value, value)
            root = value
            while parent[root] != root:
                root = parent[root]
            while parent[value] != value:
                previous = parent[value]
                parent[value] = root
                value = previous
            return root
        def union(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[max(a,b)] = min(a,b)
        for row in rows:
            for token in row['group_tokens']:
                union(row['group_tokens'][0], token)
        components = {}
        for token in parent:
            components.setdefault(find(token), []).append(token)
        for row in rows:
            tokens = sorted(components[find(row['group_tokens'][0])])
            group_id = _hash(tokens)
            # Previously released connected groups keep their split. A newly
            # discovered bridge across historical splits is rejected explicitly.
            prior = self.conn.execute('SELECT DISTINCT split FROM review_dataset_split_guards WHERE namespace=? AND group_token IN (SELECT unnest(?))', [namespace, tokens]).fetchall()
            if len(prior) > 1:
                raise ReviewTargetError('split_leakage_detected', 'new related-document links bridge prior dataset splits')
            split = prior[0][0] if prior else ('test' if int(group_id[:8],16)%10 == 0 else 'validation' if int(group_id[:8],16)%10 == 1 else 'train')
            row.update(group_id=group_id, split=split)
        content = {'contract': 'noesis-reviewed-dataset-v1', 'namespace': namespace, 'owner': principal_id, 'task_ids': sorted(task_ids),
            'rows': rows, 'excluded': excluded, 'split_policy': 'connected-source-and-declared-groups-v1',
            'label_provenance': 'consensus of independently assigned principals declaring human origin; identity is not independently certified',
            'automatic_retraining': False, 'validated_error_reduction': None,
            'effort': {'self_reported_ms': sum(row['self_reported_effort_ms'] for row in rows), 'eligible_tasks': len(rows)}}
        if len(_json(content).encode()) > 16*1024*1024:
            raise ReviewTargetError('dataset_budget_exceeded', 'annotation dataset exceeds 16 MiB')
        identity = 'review-dataset:'+_hash(content)[:32]
        self.conn.execute("INSERT OR IGNORE INTO review_dataset_releases VALUES (?,?,?,'draft',?,NULL)", [identity, namespace, principal_id, _json(content)])
        return {'release_id': identity, 'status': 'draft', **content}

    def _dataset(self, namespace, release_id, *, principal_id, scopes):
        row = self.conn.execute('SELECT owner,status,content_json,release_json FROM review_dataset_releases WHERE namespace=? AND release_id=?', [namespace, release_id]).fetchone()
        if not row:
            raise ReviewTargetError('dataset_unavailable', 'review dataset is unavailable')
        if not principal_id or 'operator' not in scopes and (DATASET_SCOPE not in scopes or row[0] != principal_id):
            raise ReviewTargetError('unauthorized', 'dataset owner and current dataset scope required')
        content = json.loads(row[2])
        for identity in content['task_ids']:
            task = self._task(namespace, identity)
            self._authorize(task, principal_id, scopes, DATASET_SCOPE, coordinator=True)
        return {'release_id': release_id, 'status': row[1], **content, 'release': json.loads(row[3]) if row[3] else None}

    def release_dataset(self, namespace, release_id, rationale, *, principal_id, scopes):
        state = self._dataset(namespace, release_id, principal_id=principal_id, scopes=scopes)
        _text(rationale)
        if state['status'] == 'released':
            if state['release']['rationale'] != rationale:
                raise ReviewTargetError('release_conflict', 'dataset already has another release receipt')
            return {**state, 'idempotent': True}
        if not state['rows']:
            raise ReviewTargetError('no_eligible_labels', 'a release requires eligible independent consensus annotations')
        # Rebuild against current review state before publishing its immutable snapshot.
        fresh = self.build_dataset(namespace, state['task_ids'], principal_id=principal_id, scopes=scopes)
        if fresh['release_id'] != release_id:
            raise ReviewTargetError('dataset_changed', 'review outcomes changed since this draft')
        receipt = {'released_by': principal_id, 'released_at_ms': self.now(), 'rationale': rationale, 'automatic_retraining': False}
        self.conn.execute('BEGIN')
        try:
            for row in state['rows']:
                for token in row['group_tokens']:
                    prior = self.conn.execute('SELECT split FROM review_dataset_split_guards WHERE namespace=? AND group_token=?', [namespace, token]).fetchone()
                    if prior and prior[0] != row['split']:
                        raise ReviewTargetError('split_leakage_detected', 'a concurrent release assigned a related document to another split')
                    self.conn.execute('INSERT OR IGNORE INTO review_dataset_split_guards VALUES (?,?,?,?)', [namespace, token, row['split'], release_id])
            self.conn.execute("UPDATE review_dataset_releases SET status='released',release_json=? WHERE release_id=?", [_json(receipt), release_id])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
        return self._dataset(namespace, release_id, principal_id=principal_id, scopes=scopes)

    def export_dataset(self, namespace, release_id, *, principal_id, scopes):
        state = self._dataset(namespace, release_id, principal_id=principal_id, scopes=scopes)
        if state['status'] != 'released':
            raise ReviewTargetError('dataset_not_released', 'explicit release is required before training/evaluation export')
        return {**state, 'sha256': _hash(state)}

    def evaluate_predictions(self, namespace, release_id, before, after, *, principal_id, scopes):
        state = self.export_dataset(namespace, release_id, principal_id=principal_id, scopes=scopes)
        rows = {row['task_id']: row for row in state['rows'] if row['split'] == 'test'}
        if not rows or not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(rows) or set(after) != set(rows):
            raise ReviewTargetError('incomplete_evaluation', 'provide paired predictions for every held-out test task in the released dataset')
        for identity, row in rows.items():
            ReviewTargets.validate_label(row['target']['kind'], before[identity])
            ReviewTargets.validate_label(row['target']['kind'], after[identity])
        old_errors = sum(before[k] != row['label'] for k, row in rows.items())
        new_errors = sum(after[k] != row['label'] for k, row in rows.items())
        return {'release_id': release_id, 'evaluation_tasks': len(rows), 'before_errors': old_errors, 'after_errors': new_errors,
            'paired_error_rate_reduction': (old_errors-new_errors)/len(rows), 'prediction_hash': _hash([before, after]),
            'self_reported_review_effort_ms': sum(row['self_reported_effort_ms'] for row in rows.values()),
            'limitations': ['Supplied predictions and declared human identities are not independently audited',
                'No claim of population-wide error reduction', 'No training or deployment triggered']}

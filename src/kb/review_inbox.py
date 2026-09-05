"""Reference-only operational reviews, independent votes, and domain routing."""
import json
import math
import time

from src.kb.research_projects import _hash, _json, ResearchProjectStore
from src.kb.review_targets import ReviewTargets, ReviewTargetError

READ_SCOPE = 'knowledge:inbox:read'
WRITE_SCOPE = 'knowledge:inbox:write'
REVIEW_SCOPE = 'knowledge:inbox:review'
_DDL = '''
CREATE TABLE IF NOT EXISTS review_inbox_tasks(
 task_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,domain TEXT NOT NULL,project_id TEXT,
 source_group TEXT NOT NULL,status TEXT NOT NULL,priority DOUBLE NOT NULL,created_at_ms BIGINT NOT NULL,task_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_inbox_assignments(
 task_id TEXT NOT NULL,reviewer_id TEXT NOT NULL,assigned_at_ms BIGINT NOT NULL,PRIMARY KEY(task_id,reviewer_id));
CREATE TABLE IF NOT EXISTS review_inbox_votes(
 vote_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,reviewer_id TEXT NOT NULL,label_json TEXT NOT NULL,rationale TEXT NOT NULL,
 effort_ms BIGINT NOT NULL,submitted_at_ms BIGINT NOT NULL,annotation_origin TEXT NOT NULL,UNIQUE(task_id,reviewer_id));
CREATE TABLE IF NOT EXISTS review_inbox_resolutions(
 task_id TEXT PRIMARY KEY,resolution_json TEXT NOT NULL);
'''


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise ReviewTargetError('invalid_review', 'nonempty text within 10000 characters required')
    return value


class ReviewInboxStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time()*1000))
        if initialize:
            conn.execute(_DDL)

    def _authorize(self, task, principal_id, scopes, required=READ_SCOPE, *, coordinator=False):
        if not principal_id or 'operator' not in scopes and required not in scopes:
            raise ReviewTargetError('unauthorized', 'current inbox scope and authenticated principal required')
        ns = task['namespace']
        if 'operator' not in scopes and not {f'namespace:{ns}:read', f'namespace:{ns}:write'} & scopes:
            raise ReviewTargetError('unauthorized', 'current inbox namespace access required')
        assigned = self.conn.execute('SELECT 1 FROM review_inbox_assignments WHERE task_id=? AND reviewer_id=?', [task['task_id'], principal_id]).fetchone()
        if task['owner'] != principal_id and 'operator' not in scopes and (coordinator or not assigned):
            raise ReviewTargetError('unauthorized', 'task coordinator or assigned reviewer required')
        if coordinator and 'operator' not in scopes and f'namespace:{ns}:write' not in scopes:
            raise ReviewTargetError('unauthorized', 'coordinator requires namespace write access')
        ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)
        self._sources(task['sources'], scopes)

    def _sources(self, specs, scopes):
        if not isinstance(specs, list) or not 1 <= len(specs) <= 20:
            raise ReviewTargetError('invalid_sources', 'one to 20 exact document revision references required')
        sources = []
        for spec in specs:
            if not isinstance(spec, dict) or set(spec) != {'document_id', 'revision_id'}:
                raise ReviewTargetError('invalid_sources', 'source references require document and revision ids')
            for value in spec.values():
                _text(value)
            if 'operator' not in scopes and f"document:{spec['document_id']}:read" not in scopes:
                raise ReviewTargetError('unauthorized', 'current document read access required')
            row = self.conn.execute('SELECT source_id,content_hash,payload_hash FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL', [spec['document_id'], spec['revision_id']]).fetchone()
            if not row:
                raise ReviewTargetError('source_unavailable', 'review source revision is unavailable')
            sources.append({**spec, 'source_id': row[0], 'content_hash': row[1], 'payload_hash': row[2]})
        return sources

    def create(self, namespace, target, *, sources, domain, impact, uncertainty, rationale, principal_id, scopes, project=None, related_groups=()):
        _text(namespace); _text(domain); _text(rationale)
        for value in (impact, uncertainty):
            if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1:
                raise ReviewTargetError('invalid_priority', 'declared impact and uncertainty must be from zero to one')
        if not isinstance(related_groups, (list, tuple)) or len(related_groups) > 100:
            raise ReviewTargetError('invalid_groups', 'at most 100 declared related-document groups allowed')
        for group in related_groups:
            _text(group)
        current = ReviewTargets(self.conn).inspect(target, scopes=scopes)
        if current['superseded']:
            raise ReviewTargetError('target_stale', 'enqueue the current target revision')
        refs = self._sources(sources, scopes)
        project_id = None
        if project is not None:
            if not isinstance(project, dict) or set(project) != {'namespace', 'id'}:
                raise ReviewTargetError('invalid_project', 'project requires namespace and id')
            state = ResearchProjectStore(self.conn, initialize=False).inspect(project['namespace'], project['id'], principal_id=principal_id, scopes=scopes)
            if target['namespace'] not in {state['namespace'], *state['scope']['namespaces']} or state['scope']['domains'] and domain not in state['scope']['domains']:
                raise ReviewTargetError('scope_mismatch', 'review target is outside the project scope')
            project_id = project['id']
        core = {'namespace': namespace, 'owner': principal_id, 'target': target, 'target_revision_hash': current['revision_hash'],
            'sources': sources, 'source_fingerprints': refs, 'domain': domain, 'project': project,
            'impact': impact, 'uncertainty': uncertainty, 'priority_rationale': rationale,
            'related_groups': sorted(set(related_groups))}
        task_id = 'inbox-task:' + _hash(core)[:32]
        core['task_id'] = task_id
        self._authorize(core, principal_id, scopes, WRITE_SCOPE, coordinator=True)
        source_group = refs[0]['source_id'] or refs[0]['document_id']
        self.conn.execute("INSERT OR IGNORE INTO review_inbox_tasks VALUES (?,?,?,?,?,?,'unassigned',?,?,?)",
            [task_id, namespace, principal_id, domain, project_id, source_group, 2*impact+uncertainty, self.now(), _json(core)])
        return self.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes)

    def _task(self, namespace, task_id):
        row = self.conn.execute('SELECT task_json,status,created_at_ms FROM review_inbox_tasks WHERE namespace=? AND task_id=?', [namespace, task_id]).fetchone()
        if not row:
            raise ReviewTargetError('task_unavailable', 'review task is unavailable')
        return {**json.loads(row[0]), 'status': row[1], 'created_at_ms': row[2]}

    def inspect(self, namespace, task_id, *, principal_id, scopes):
        task = self._task(namespace, task_id)
        self._authorize(task, principal_id, scopes)
        votes = self.conn.execute('SELECT reviewer_id,label_json,rationale,effort_ms,annotation_origin FROM review_inbox_votes WHERE task_id=? ORDER BY reviewer_id', [task_id]).fetchall()
        resolution = self.conn.execute('SELECT resolution_json FROM review_inbox_resolutions WHERE task_id=?', [task_id]).fetchone()
        coordinator = task['owner'] == principal_id or 'operator' in scopes
        # Independent reviewers cannot inspect peer labels before resolution.
        visible = votes if coordinator or resolution else [v for v in votes if v[0] == principal_id]
        current = ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)
        return {**task, 'stale': current['revision_hash'] != task['target_revision_hash'],
            'votes': [{'reviewer_id': v[0], 'label': json.loads(v[1]), 'rationale': v[2], 'effort_ms': v[3], 'annotation_origin': v[4]} for v in visible],
            'resolution': json.loads(resolution[0]) if resolution else None,
            'assignment_count': self.conn.execute('SELECT count(*) FROM review_inbox_assignments WHERE task_id=?', [task_id]).fetchone()[0],
            'submitted_count': len(votes)}

    def export_label_studio(self, namespace, task_ids, *, labels, principal_id, scopes):
        from src.integrations.annotation import export_tasks
        records, source_maps = [], {}
        if not 1 <= len(task_ids) <= 1000 or len(set(task_ids)) != len(task_ids):
            raise ReviewTargetError('invalid_tasks', 'one to 1000 distinct task IDs required')
        for task_id in task_ids:
            task = self.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes)
            if task['stale']:
                raise ReviewTargetError('target_stale', 'annotation target changed')
            parts, locations, offset = [], [], 0
            for spec in task['sources']:
                row = self.conn.execute('SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL',
                                        [spec['document_id'], spec['revision_id']]).fetchone()
                payload = json.loads(row[0]) if row else {}
                if payload.get('_payload_reclaimed') or not isinstance(payload.get('content'), str):
                    raise ReviewTargetError('source_unavailable', 'annotation source text unavailable')
                text = payload['content']
                locations.append({**spec, 'start': offset, 'end': offset + len(text)})
                parts.append(text); offset += len(text) + 2
            records.append({'task_id': task_id, 'revision_id': task['target_revision_hash'], 'text': '\n\n'.join(parts)})
            source_maps[task_id] = locations
        exported = export_tasks(records, labels=labels)
        for task in exported:
            task['meta']['source_spans'] = source_maps[task['data']['task_id']]
        return exported

    def import_label_studio(self, namespace, exported, returned, *, reviewer_mapping, principal_id, scopes):
        from src.integrations.annotation import import_annotations
        if not exported:
            raise ReviewTargetError('invalid_tasks', 'exported task manifest required')
        current = self.export_label_studio(namespace, [t['data']['task_id'] for t in exported],
                    labels=exported[0]['data']['labels'], principal_id=principal_id, scopes=scopes)
        if current != exported:
            raise ReviewTargetError('target_stale', 'exported source or annotation schema changed')
        return import_annotations(exported, returned, reviewer_mapping=reviewer_mapping,
                                  current_revisions={t['data']['task_id']: t['data']['revision_id'] for t in current})

    def assign(self, namespace, task_id, reviewers, *, principal_id, scopes):
        task = self._task(namespace, task_id)
        self._authorize(task, principal_id, scopes, WRITE_SCOPE, coordinator=True)
        if not isinstance(reviewers, list) or not 2 <= len(reviewers) <= 10 or len(set(reviewers)) != len(reviewers):
            raise ReviewTargetError('invalid_assignment', 'two to ten distinct reviewers required')
        for reviewer in reviewers:
            _text(reviewer)
        if task['owner'] in reviewers:
            raise ReviewTargetError('independence_required', 'coordinator cannot be an independent reviewer of the same task')
        if task['status'] not in {'unassigned', 'assigned'}:
            raise ReviewTargetError('task_started', 'assign reviewers before submissions')
        self.conn.execute('BEGIN')
        try:
            self.conn.execute('DELETE FROM review_inbox_assignments WHERE task_id=?', [task_id])
            for reviewer in reviewers:
                self.conn.execute('INSERT INTO review_inbox_assignments VALUES (?,?,?)', [task_id, reviewer, self.now()])
            self.conn.execute("UPDATE review_inbox_tasks SET status='assigned' WHERE task_id=?", [task_id])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
        return self.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes)

    def submit(self, namespace, task_id, expected_target_hash, label, rationale, effort_ms, annotation_origin, *, principal_id, scopes):
        task = self._task(namespace, task_id)
        self._authorize(task, principal_id, scopes, REVIEW_SCOPE)
        if annotation_origin not in {'human', 'machine'} or type(effort_ms) is not int or not 0 <= effort_ms <= 8*3600*1000:
            raise ReviewTargetError('invalid_annotation', 'declared human/machine origin and bounded self-reported effort required')
        _text(rationale)
        ReviewTargets.validate_label(task['target']['kind'], label)
        assigned = self.conn.execute('SELECT assigned_at_ms FROM review_inbox_assignments WHERE task_id=? AND reviewer_id=?', [task_id, principal_id]).fetchone()
        if not assigned:
            raise ReviewTargetError('unauthorized', 'only assigned reviewers submit labels')
        prior = self.conn.execute('SELECT label_json,rationale,effort_ms,annotation_origin FROM review_inbox_votes WHERE task_id=? AND reviewer_id=?', [task_id, principal_id]).fetchone()
        if prior:
            if prior != (_json(label), rationale, effort_ms, annotation_origin):
                raise ReviewTargetError('submission_conflict', 'submitted annotation is immutable; use adjudication or a fresh task')
            return {**self.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes), 'idempotent': True}
        if task['status'] not in {'assigned', 'in_review'}:
            raise ReviewTargetError('task_closed', 'task is not accepting submissions')
        self.conn.execute('BEGIN')
        try:
            current = ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)
            if expected_target_hash != task['target_revision_hash'] or current['revision_hash'] != expected_target_hash:
                raise ReviewTargetError('target_stale', 'underlying object changed; enqueue a fresh revision')
            if self._sources(task['sources'], scopes) != task['source_fingerprints']:
                raise ReviewTargetError('source_changed', 'source snapshot changed')
            self.conn.execute('INSERT INTO review_inbox_votes VALUES (?,?,?,?,?,?,?,?)',
                ['inbox-vote:'+_hash([task_id, principal_id])[:32], task_id, principal_id, _json(label), rationale, effort_ms, self.now(), annotation_origin])
            labels = self.conn.execute('SELECT label_json FROM review_inbox_votes WHERE task_id=?', [task_id]).fetchall()
            count = self.conn.execute('SELECT count(*) FROM review_inbox_assignments WHERE task_id=?', [task_id]).fetchone()[0]
            status = 'in_review' if len(labels) < count else 'consensus_ready' if len(set(v[0] for v in labels)) == 1 else 'disputed'
            self.conn.execute('UPDATE review_inbox_tasks SET status=? WHERE task_id=?', [status, task_id])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
        return self.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes)

    def resolve(self, namespace, task_id, rationale, *, principal_id, scopes, adjudicated_label=None):
        task = self._task(namespace, task_id)
        self._authorize(task, principal_id, scopes, REVIEW_SCOPE, coordinator=True)
        _text(rationale)
        prior = self.conn.execute('SELECT resolution_json FROM review_inbox_resolutions WHERE task_id=?', [task_id]).fetchone()
        if prior:
            receipt = json.loads(prior[0])
            if receipt['rationale'] != rationale or receipt['adjudicated_label'] != adjudicated_label:
                raise ReviewTargetError('resolution_conflict', 'task already has a different resolution')
            return {**receipt, 'idempotent': True}
        if task['status'] not in {'consensus_ready', 'disputed'}:
            raise ReviewTargetError('review_incomplete', 'all assigned independent reviews must finish')
        votes = self.conn.execute('SELECT reviewer_id,label_json,annotation_origin FROM review_inbox_votes WHERE task_id=? ORDER BY reviewer_id', [task_id]).fetchall()
        if principal_id in {v[0] for v in votes}:
            raise ReviewTargetError('independence_required', 'resolution requires a separate coordinator')
        if task['status'] == 'disputed' and adjudicated_label is None:
            raise ReviewTargetError('adjudication_required', 'conflicting reviewers require an explicit adjudicated label')
        label = adjudicated_label if adjudicated_label is not None else json.loads(votes[0][1])
        ReviewTargets.validate_label(task['target']['kind'], label)
        self.conn.execute('BEGIN')
        try:
            current = ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)
            if current['revision_hash'] != task['target_revision_hash']:
                raise ReviewTargetError('target_stale', 'underlying object changed; refresh before routing')
            routed = ReviewTargets(self.conn).route(task['target'], label, rationale=rationale, principal_id=principal_id, scopes=scopes, task_id=task_id)
            receipt = {'task_id': task_id, 'label': label, 'rationale': rationale, 'adjudicated_label': adjudicated_label,
                'reviewer': principal_id, 'resolved_at_ms': self.now(), 'agreement': task['status'] == 'consensus_ready',
                'reviewers': [v[0] for v in votes], 'all_declared_human': all(v[2] == 'human' for v in votes), 'routed': routed}
            receipt['routed_target_hash'] = ReviewTargets(self.conn).inspect(task['target'], scopes=scopes)['revision_hash']
            self.conn.execute('INSERT INTO review_inbox_resolutions VALUES (?,?)', [task_id, _json(receipt)])
            self.conn.execute("UPDATE review_inbox_tasks SET status='resolved' WHERE task_id=?", [task_id])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK'); raise
        return receipt

    def list(self, namespace, *, principal_id, scopes, domain=None, project_id=None, limit=10, per_source=2):
        if not principal_id or 'operator' not in scopes and (READ_SCOPE not in scopes or not {f'namespace:{namespace}:read', f'namespace:{namespace}:write'} & scopes):
            raise ReviewTargetError('unauthorized', 'current inbox and namespace read access required')
        if type(limit) is not int or not 1 <= limit <= 100 or type(per_source) is not int or not 1 <= per_source <= 10:
            raise ReviewTargetError('invalid_page', 'limit from one to 100 and per-source cap from one to ten required')
        # Apply diversity before the global bound so a prolific source cannot
        # push all other sources outside the candidate window.
        rows = self.conn.execute('''SELECT task_id,source_group,priority,created_at_ms FROM review_inbox_tasks t
            WHERE namespace=? AND status!='resolved' AND (? IS NULL OR domain=?) AND (? IS NULL OR project_id=?)
            AND (owner=? OR ? OR EXISTS (SELECT 1 FROM review_inbox_assignments a WHERE a.task_id=t.task_id AND a.reviewer_id=?))
            QUALIFY row_number() OVER (PARTITION BY source_group ORDER BY priority DESC,created_at_ms DESC,task_id)<=?
            ORDER BY priority DESC,created_at_ms DESC,task_id LIMIT 1001''',
            [namespace, domain, domain, project_id, project_id, principal_id, 'operator' in scopes, principal_id, per_source]).fetchall()
        candidates, unavailable = [], 0
        for identity, source, priority, created in rows[:1000]:
            try:
                task = self.inspect(namespace, identity, principal_id=principal_id, scopes=scopes)
            except ReviewTargetError as exc:
                if exc.code in {'unauthorized', 'source_unavailable', 'target_unavailable'}:
                    unavailable += 1
                    continue
                raise
            freshness = 1/(1+max(0, self.now()-created)/(86400*1000))
            candidates.append({'task': task, 'priority': priority+freshness, 'source_group': source,
                'priority_reasons': {'declared_impact': task['impact'], 'declared_uncertainty': task['uncertainty'], 'freshness': freshness,
                    'source_cap': per_source, 'rationale': task['priority_rationale']}})
        candidates.sort(key=lambda v: (-v['priority'], v['task']['task_id']))
        return {'tasks': candidates[:limit], 'bounded': len(rows)>1000, 'unavailable_count': unavailable,
                'ranking': '2*declared_impact + declared_uncertainty + recency; per-source cap', 'error_reduction_validated': False}

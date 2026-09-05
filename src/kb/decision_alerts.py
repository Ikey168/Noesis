"""Version-pinned decision conditions with review tasks and existing brief delivery."""
import json
from decimal import Decimal, InvalidOperation

import duckdb

from src.kb.decisions import DecisionStore, DecisionError, _text, _number
from src.kb.evidence_changes import EvidenceResolver
from src.kb.forecasts import _match
from src.kb.research_projects import _hash, _json

_DDL = '''
CREATE TABLE IF NOT EXISTS decision_condition_watches(
 watch_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,decision_id TEXT NOT NULL,definition_json TEXT NOT NULL,state_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_review_tasks(
 task_id TEXT PRIMARY KEY,watch_id TEXT NOT NULL,namespace TEXT NOT NULL,status TEXT NOT NULL,content_json TEXT NOT NULL,
 delivery_json TEXT,ack_json TEXT,decision_revision BIGINT);
'''


class DecisionAlertStore(DecisionStore):
    def __init__(self, conn, *, initialize=True, now=None):
        super().__init__(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)
            from src.kb.change_briefs import ChangeBriefStore
            ChangeBriefStore(conn)

    def _condition(self, condition, scopes):
        if condition['kind'] in {'source_revision', 'assumption'}:
            assessment = EvidenceResolver(self.conn, scopes).compare(condition['dependency'])
            return {'condition_id': condition['id'], 'status': 'triggered' if assessment['status']=='affected' else 'uncertain' if assessment['status']=='uncertain' else 'clear',
                'reason': assessment['reason'], 'assessment': assessment}
        rule = condition['rule']
        ns = rule['namespace']
        if 'operator' not in scopes and ('knowledge:quantitative:read' not in scopes or not {f'namespace:{ns}:read', f'namespace:{ns}:write'} & scopes):
            raise DecisionError('unauthorized', 'current metric namespace and quantitative read scope required')
        result = {'condition_id': condition['id'], 'status': 'uncertain', 'reason': 'observation_unavailable', 'rule': rule}
        try:
            rows = self.conn.execute('''SELECT observation_id,value_text,missing,preliminary,vintage_id,provenance_json,release_at_ms
                FROM quantitative_observations WHERE namespace=? AND metric_id=? AND provider=? AND provider_series_id=? AND period=? AND unit_id=?
                AND release_at_ms<=? ORDER BY release_at_ms DESC,observation_id LIMIT 1001''',
                [rule[k] for k in ('namespace','metric_id','provider','provider_series_id','period','unit_id')]+[self.now()]).fetchall()
        except duckdb.CatalogException:
            return result
        if not rows or len(rows)>1000:
            return {**result, 'reason': 'observation_budget_exceeded' if rows else 'observation_unavailable'}
        latest = [row for row in rows if row[6] == rows[0][6]]
        result['evidence'] = [{'id': row[0], 'revision': row[4], 'namespace': ns, 'release_at_ms': row[6]} for row in latest]
        if any(row[2] or row[3] or not json.loads(row[5]) for row in latest):
            return {**result, 'reason': 'missing_preliminary_or_unsourced'}
        try:
            values = {_number(row[1]) for row in latest}
        except (InvalidOperation, DecisionError):
            return {**result, 'reason': 'invalid_observation'}
        if len(values) != 1:
            return {**result, 'reason': 'conflicting_latest_observations'}
        value, threshold = next(iter(values)), _number(rule['threshold'])
        triggered = {'gt': value>threshold, 'gte': value>=threshold, 'lt': value<threshold, 'lte': value<=threshold, 'eq': value==threshold}[rule['comparison']]
        return {**result, 'status': 'triggered' if triggered else 'clear', 'reason': 'registered_metric_comparison', 'value': str(value)}

    def create_watch(self, namespace, decision_id, expected_revision, conditions, *, principal_id, scopes):
        decision = self.inspect(namespace, decision_id, principal_id=principal_id, scopes=scopes)
        baseline = self._authorize(decision, principal_id, scopes, write=True)
        if decision['revision'] != expected_revision:
            raise DecisionError('revision_conflict', 'pin the current decision revision')
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 50:
            raise DecisionError('invalid_conditions', 'one to 50 explicit conditions required')
        ids = set()
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get('kind') not in {'source_revision', 'assumption', 'metric_threshold'}:
                raise DecisionError('invalid_conditions', 'unsupported review condition')
            expected = {'id','kind','rule'} if condition['kind']=='metric_threshold' else {'id','kind','dependency'} | ({'assumption'} if condition['kind']=='assumption' else set())
            if set(condition) != expected or condition['id'] in ids:
                raise DecisionError('invalid_conditions', 'condition ids must be unique with exact typed fields')
            _text(condition['id']); ids.add(condition['id'])
            if condition['kind']=='metric_threshold':
                _match(condition['rule']); _number(condition['rule']['threshold'])
                ns = condition['rule']['namespace']
            else:
                dep = condition['dependency']
                if not isinstance(dep, dict) or set(dep) != {'kind','namespace','id','revision','locator'} or dep['kind'] not in {'source','claim','entity','calculation','artifact'} or not isinstance(dep['locator'], dict):
                    raise DecisionError('invalid_conditions', 'condition requires an exact evidence dependency')
                for key in ('namespace','id','revision'):
                    _text(dep[key])
                ns = dep['namespace']
                if condition['kind']=='assumption' and condition['assumption'] not in decision['content']['assumptions']:
                    raise DecisionError('invalid_conditions', 'assumption must be declared in the pinned decision')
            if ns not in {baseline['namespace'], *baseline['scope']['namespaces']}:
                raise DecisionError('scope_mismatch', 'condition is outside the pinned project scope')
        definition = {'decision_id': decision_id, 'decision_revision': expected_revision, 'namespace': namespace,
            'owner': principal_id, 'conditions': conditions, 'created_at_ms': self.now()}
        identity = 'decision-watch:'+_hash({k:v for k,v in definition.items() if k!='created_at_ms'})[:32]
        definition['watch_id'] = identity
        initial = [self._condition(c, scopes) for c in conditions]
        if any(v['reason'].endswith('access_unavailable') for v in initial):
            raise DecisionError('unauthorized', 'current access to condition evidence required')
        self.conn.execute('INSERT OR IGNORE INTO decision_condition_watches VALUES (?,?,?,?,?)', [identity, namespace, decision_id, _json(definition), _json(initial)])
        return self.inspect_watch(namespace, identity, principal_id=principal_id, scopes=scopes)

    def inspect_watch(self, namespace, watch_id, *, principal_id, scopes):
        row = self.conn.execute('SELECT definition_json,state_json FROM decision_condition_watches WHERE namespace=? AND watch_id=?', [namespace, watch_id]).fetchone()
        if not row:
            raise DecisionError('watch_unavailable', 'decision condition watch is unavailable')
        definition = json.loads(row[0])
        self.inspect(namespace, definition['decision_id'], revision=definition['decision_revision'], principal_id=principal_id, scopes=scopes)
        for condition in definition['conditions']:
            value = self._condition(condition, scopes)
            if value['reason'].endswith('access_unavailable'):
                raise DecisionError('unauthorized', 'current access to condition evidence required')
        return {**definition, 'last_assessment': json.loads(row[1])}

    def poll_watch(self, namespace, watch_id, *, principal_id, scopes):
        watch = self.inspect_watch(namespace, watch_id, principal_id=principal_id, scopes=scopes)
        decision = self.inspect(namespace, watch['decision_id'], principal_id=principal_id, scopes=scopes)
        self._authorize(decision, principal_id, scopes, write=True)
        if decision['revision'] != watch['decision_revision']:
            self._deliver_pending(namespace, watch_id, principal_id=principal_id, scopes=scopes)
            return {'watch_id': watch_id, 'status': 'decision_revised', 'requires_new_watch': True, 'tasks': []}
        current = [self._condition(c, scopes) for c in watch['conditions']]
        previous = {v['condition_id']:v for v in watch['last_assessment']}
        self.conn.execute('BEGIN')
        try:
            for value in current:
                before = previous[value['condition_id']]
                if value['status'] != 'triggered' or _hash(before)==_hash(value):
                    continue
                core = {'watch_id': watch_id, 'decision_id': watch['decision_id'], 'decision_revision': watch['decision_revision'],
                    'condition_id': value['condition_id'], 'before': before, 'after': value, 'requires_review': True,
                    'assumption_disproved': False}
                identity = 'decision-review:'+_hash(core)[:32]
                core.update(task_id=identity, created_at_ms=self.now())
                self.conn.execute("INSERT OR IGNORE INTO decision_review_tasks VALUES (?,?,?,'pending',?,NULL,NULL,NULL)", [identity, watch_id, namespace, _json(core)])
            self.conn.execute('UPDATE decision_condition_watches SET state_json=? WHERE watch_id=?', [_json(current), watch_id])
            self.conn.execute('COMMIT')
        except Exception as exc:
            self._abort(exc)
        # A publication failure leaves the durable task pending; the next poll
        # retries the existing brief identity without creating another alert.
        self._deliver_pending(namespace, watch_id, principal_id=principal_id, scopes=scopes)
        return {'watch_id': watch_id, 'status': 'checked', 'assessment': current,
            'tasks': self.list_tasks(namespace, watch_id, principal_id=principal_id, scopes=scopes)['tasks']}

    def _deliver_pending(self, namespace, watch_id, *, principal_id, scopes):
        pending = self.conn.execute("SELECT task_id FROM decision_review_tasks WHERE watch_id=? AND delivery_json IS NULL ORDER BY task_id LIMIT 101", [watch_id]).fetchall()
        if len(pending)>100:
            raise DecisionError('delivery_budget_exceeded', 'more than 100 pending review tasks; deliver a narrower watch')
        for (identity,) in pending:
            self._deliver(namespace, identity, principal_id=principal_id, scopes=scopes)

    def _deliver(self, namespace, task_id, *, principal_id, scopes):
        from src.kb.change_briefs import ChangeBriefStore, READ_SCOPE, WRITE_SCOPE, DELIVER_SCOPE
        # Require the original delivery permissions rather than manufacturing
        # a scope set for calls through another feature.
        if 'operator' not in scopes and not {READ_SCOPE, WRITE_SCOPE, DELIVER_SCOPE} <= scopes:
            raise DecisionError('unauthorized', 'change-brief read/write/deliver scopes required')
        task = json.loads(self.conn.execute('SELECT content_json FROM decision_review_tasks WHERE namespace=? AND task_id=?', [namespace, task_id]).fetchone()[0])
        briefs = ChangeBriefStore(self.conn, now=lambda: task['created_at_ms'])
        policy = briefs.register_policy(namespace, 'decision-conditions-v1', '1', observed_at_ms=0, principal_id=principal_id, scopes=scopes)
        preview = briefs.preview(namespace, 'policy', task['watch_id'], {'condition_status': task['before']['status']},
            {'condition_status': task['after']['status'], 'evidence_hash': _hash(task['after'])}, task['decision_revision'], task['decision_revision'], scopes=scopes,
            evidence_before=[{'review_task_id': task_id, 'evidence_hash': _hash(task['before'])}],
            evidence_after=[{'review_task_id': task_id, 'evidence_hash': _hash(task['after'])}])
        brief = briefs.generate(namespace, policy['policy_revision_id'], preview, principal_id=principal_id, scopes=scopes)
        subscription = briefs.subscribe(namespace, principal_id, 1000, {'brief_ids': [brief['brief_id']], 'material_only': True}, principal_id=principal_id, scopes=scopes)
        delivery = briefs.deliver(namespace, subscription['subscription_id'], task['created_at_ms'], task['created_at_ms']+1, principal_id=principal_id, scopes=scopes)
        if self.conn.execute('SELECT ack_json FROM decision_review_tasks WHERE task_id=?', [task_id]).fetchone()[0]:
            briefs.acknowledge(namespace, delivery['delivery_id'], principal_id=principal_id, scopes=scopes)
        receipt = {'brief_id': brief['brief_id'], 'subscription_id': subscription['subscription_id'], 'delivery_id': delivery['delivery_id']}
        self.conn.execute('UPDATE decision_review_tasks SET delivery_json=? WHERE task_id=?', [_json(receipt), task_id])
        return receipt

    def list_tasks(self, namespace, watch_id, *, principal_id, scopes, offset=0, limit=100):
        self.inspect_watch(namespace, watch_id, principal_id=principal_id, scopes=scopes)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise DecisionError('invalid_page', 'nonnegative offset and limit from one to 100 required')
        rows = self.conn.execute('SELECT content_json,status,delivery_json,ack_json,decision_revision FROM decision_review_tasks WHERE watch_id=? ORDER BY task_id LIMIT ? OFFSET ?', [watch_id, limit+1, offset]).fetchall()
        return {'tasks': [{**json.loads(v[0]), 'status': v[1], 'delivery': json.loads(v[2]) if v[2] else None,
            'acknowledgement': json.loads(v[3]) if v[3] else None, 'subsequent_decision_revision': v[4]} for v in rows[:limit]], 'next_offset': offset+limit if len(rows)>limit else None}

    def acknowledge(self, namespace, task_id, rationale, *, principal_id, scopes, subsequent_revision=None):
        _text(rationale)
        row = self.conn.execute('SELECT watch_id,content_json,delivery_json,ack_json,decision_revision FROM decision_review_tasks WHERE namespace=? AND task_id=?', [namespace, task_id]).fetchone()
        if not row:
            raise DecisionError('task_unavailable', 'decision review task is unavailable')
        watch = self.inspect_watch(namespace, row[0], principal_id=principal_id, scopes=scopes)
        decision = self.inspect(namespace, watch['decision_id'], principal_id=principal_id, scopes=scopes)
        self._authorize(decision, principal_id, scopes, write=True)
        if subsequent_revision is not None:
            if type(subsequent_revision) is not int or subsequent_revision<=watch['decision_revision']:
                raise DecisionError('invalid_revision', 'link a later decision revision')
            self.inspect(namespace, watch['decision_id'], revision=subsequent_revision, principal_id=principal_id, scopes=scopes)
        prior = json.loads(row[3]) if row[3] else None
        if prior and prior['rationale'] != rationale:
            raise DecisionError('acknowledgement_conflict', 'review task already has another acknowledgement')
        if row[4] is not None and subsequent_revision is not None and row[4]!=subsequent_revision:
            raise DecisionError('revision_conflict', 'review task already links another decision revision')
        if row[2]:
            from src.kb.change_briefs import ChangeBriefStore
            ChangeBriefStore(self.conn, initialize=False).acknowledge(namespace, json.loads(row[2])['delivery_id'], principal_id=principal_id, scopes=scopes)
        receipt = prior or {'reviewer': principal_id, 'rationale': rationale, 'acknowledged_at_ms': self.now()}
        self.conn.execute("UPDATE decision_review_tasks SET status='acknowledged',ack_json=?,decision_revision=coalesce(?,decision_revision) WHERE task_id=?", [_json(receipt), subsequent_revision, task_id])
        return {'task_id': task_id, 'acknowledgement': receipt, 'subsequent_decision_revision': subsequent_revision or row[4], 'decision_changed_by_acknowledgement': False}

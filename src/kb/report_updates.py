"""Dependency assessments and individually reviewed report revision proposals."""
import copy
import json

from src.kb.authored_reports import AuthoredReportStore, ReportError, validate_content, _text
from src.kb.evidence_changes import EvidenceResolver
from src.kb.research_projects import _hash, _json

_DDL = '''
CREATE TABLE IF NOT EXISTS report_evidence_assessments(
 assessment_id TEXT PRIMARY KEY,report_id TEXT NOT NULL,namespace TEXT NOT NULL,report_revision BIGINT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_update_subscriptions(
 report_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,latest_assessment_id TEXT NOT NULL,checked_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS report_edit_proposals(
 proposal_id TEXT PRIMARY KEY,report_id TEXT NOT NULL,namespace TEXT NOT NULL,assessment_id TEXT NOT NULL,
 assertion_id TEXT NOT NULL,status TEXT NOT NULL,proposal_json TEXT NOT NULL,decision_json TEXT);
'''


def _assertions(content):
    return {assertion['id']: (section['id'], assertion) for section in content['sections'] for assertion in section['assertions']}


class ReportUpdateStore(AuthoredReportStore):
    def __init__(self, conn, *, initialize=True, now=None):
        super().__init__(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    def assess(self, namespace, report_id, *, principal_id, scopes):
        state = self.inspect(namespace, report_id, principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, write=True)
        if sum(len(a['dependencies']) for _, a in _assertions(state['content']).values()) > 1000:
            raise ReportError('assessment_budget_exceeded', 'assessment supports at most 1000 declared dependencies')
        resolver = EvidenceResolver(self.conn, scopes)
        sections, cache = [], {}
        # A read transaction prevents mixing publications across a single scan.
        self.conn.execute('BEGIN')
        try:
            for section in state['content']['sections']:
                assertions = []
                for assertion in section['assertions']:
                    dependencies = []
                    for dep in assertion['dependencies']:
                        key = _hash(dep)
                        if key not in cache:
                            cache[key] = resolver.compare(dep)
                        dependencies.append(cache[key])
                    status = 'affected' if any(d['status'] == 'affected' for d in dependencies) else 'uncertain' if any(d['status'] == 'uncertain' for d in dependencies) else 'current'
                    assertions.append({'assertion_id': assertion['id'], 'assertion_hash': _hash(assertion), 'status': status, 'dependencies': dependencies})
                status = 'affected' if any(a['status'] == 'affected' for a in assertions) else 'uncertain' if any(a['status'] == 'uncertain' for a in assertions) else 'current'
                sections.append({'section_id': section['id'], 'status': status, 'assertions': assertions})
            result = {'contract': 'noesis-report-assessment-v1', 'report_id': report_id, 'namespace': namespace, 'report_revision': state['revision'],
                'sections': sections, 'coverage': 'incomplete' if any(d['status'] == 'uncertain' or d.get('coverage') == 'incomplete' for d in cache.values()) else 'complete_for_declared_dependencies',
                'substantive_support_verified': False, 'monitoring': 'committed revisions observed on explicit poll'}
            identity = 'report-assessment:' + _hash(result)[:32]
            result['assessment_id'] = identity
            if len(_json(result).encode()) > 16*1024*1024:
                raise ReportError('assessment_budget_exceeded', 'assessment evidence exceeds 16 MiB; narrow report dependencies')
            self.conn.execute('INSERT OR IGNORE INTO report_evidence_assessments VALUES (?,?,?,?,?)', [identity, report_id, namespace, state['revision'], _json(result)])
            self.conn.execute('INSERT OR REPLACE INTO report_update_subscriptions VALUES (?,?,?,?)', [report_id, namespace, identity, self.now()])
            self.conn.execute('COMMIT')
        except Exception as exc:
            self._abort(exc)
        return result

    def _assessment(self, namespace, assessment_id, *, principal_id, scopes):
        row = self.conn.execute('SELECT content_json FROM report_evidence_assessments WHERE namespace=? AND assessment_id=?', [namespace, assessment_id]).fetchone()
        if not row:
            raise ReportError('assessment_unavailable', 'report assessment is unavailable')
        result = json.loads(row[0])
        self.inspect(namespace, result['report_id'], revision=result['report_revision'], principal_id=principal_id, scopes=scopes)
        resolver = EvidenceResolver(self.conn, scopes)
        for section in result['sections']:
            for assertion in section['assertions']:
                for dep in assertion['dependencies']:
                    current = resolver.compare(dep['dependency'])
                    if current['reason'].endswith('access_unavailable'):
                        raise ReportError('unauthorized', 'current access to assessed evidence is required')
        return result

    def propose(self, namespace, assessment_id, assertion_id, *, principal_id, scopes, replacement=None):
        assessment = self._assessment(namespace, assessment_id, principal_id=principal_id, scopes=scopes)
        state = self.inspect(namespace, assessment['report_id'], revision=assessment['report_revision'], principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, write=True)
        found = [a for s in assessment['sections'] for a in s['assertions'] if a['assertion_id'] == assertion_id]
        if not found or found[0]['status'] != 'affected':
            raise ReportError('assertion_unaffected', 'proposals are limited to assertions affected by committed evidence changes')
        section_id, original = _assertions(state['content'])[assertion_id]
        reasons = sorted({d['reason'] for d in found[0]['dependencies'] if d['status'] == 'affected'})
        proposed = copy.deepcopy(original)
        if replacement is None:
            proposed['text'] = 'Evidence update requires review (' + ', '.join(reasons) + '). Previously reported: ' + original['text']
            method = 'deterministic-review-notice-v1'
        else:
            if not isinstance(replacement, dict) or replacement.get('id') != assertion_id:
                raise ReportError('invalid_proposal', 'replacement must preserve the assertion id')
            proposed = copy.deepcopy(replacement)
            method = 'author-supplied-edit'
        content = copy.deepcopy(state['content'])
        for section in content['sections']:
            section['assertions'] = [proposed if a['id'] == assertion_id else a for a in section['assertions']]
        validate_content(content)
        self._authorize({**state, 'content': content}, principal_id, scopes, write=True)
        core = {'assessment_id': assessment_id, 'report_id': state['report_id'], 'base_report_revision': state['revision'],
            'section_id': section_id, 'assertion_id': assertion_id, 'before': original, 'proposed': proposed,
            'reasons': reasons, 'evidence': found[0]['dependencies'], 'method': method, 'support_verified': False}
        proposal_id = 'report-edit:' + _hash(core)[:32]
        self.conn.execute("INSERT OR IGNORE INTO report_edit_proposals VALUES (?,?,?,?,?,'pending',?,NULL)",
            [proposal_id, state['report_id'], namespace, assessment_id, assertion_id, _json(core)])
        return self.inspect_proposal(namespace, proposal_id, principal_id=principal_id, scopes=scopes)

    def generate_proposal(self, namespace, assessment_id, assertion_id, *, generator, principal_id, scopes):
        """Generate an edit through an explicit backend and retain existing review gates."""
        assessment = self._assessment(namespace, assessment_id, principal_id=principal_id, scopes=scopes)
        state = self.inspect(namespace, assessment['report_id'], revision=assessment['report_revision'],
                             principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, write=True)
        original = _assertions(state['content'])[assertion_id][1]
        found = [a for s in assessment['sections'] for a in s['assertions'] if a['assertion_id'] == assertion_id]
        if not found or found[0]['status'] != 'affected':
            raise ReportError('assertion_unaffected', 'Only affected assertions can be revised')
        replacement = generator(copy.deepcopy(original), copy.deepcopy(found[0]['dependencies']))
        return self.propose(namespace, assessment_id, assertion_id, principal_id=principal_id,
                            scopes=scopes, replacement=replacement)

    def inspect_proposal(self, namespace, proposal_id, *, principal_id, scopes):
        row = self.conn.execute('SELECT assessment_id,status,proposal_json,decision_json FROM report_edit_proposals WHERE namespace=? AND proposal_id=?', [namespace, proposal_id]).fetchone()
        if not row:
            raise ReportError('proposal_unavailable', 'report edit proposal is unavailable')
        self._assessment(namespace, row[0], principal_id=principal_id, scopes=scopes)
        return {'proposal_id': proposal_id, 'status': row[1], 'proposal': json.loads(row[2]), 'decision': json.loads(row[3]) if row[3] else None}

    def decide_proposal(self, namespace, proposal_id, decision, rationale, *, principal_id, scopes):
        if decision not in {'accept', 'reject'}:
            raise ReportError('invalid_decision', 'accept or reject is required')
        _text(rationale, 'review rationale', 10000)
        item = self.inspect_proposal(namespace, proposal_id, principal_id=principal_id, scopes=scopes)
        proposal = item['proposal']
        current = self.inspect(namespace, proposal['report_id'], principal_id=principal_id, scopes=scopes)
        self._authorize(current, principal_id, scopes, write=True)
        if item['status'] != 'pending':
            if item['decision']['decision'] != decision or item['decision']['rationale'] != rationale:
                raise ReportError('decision_conflict', 'proposal already has a different review decision')
            return {**item, 'idempotent': True}
        content = copy.deepcopy(current['content'])
        if decision == 'accept':
            found = _assertions(content).get(proposal['assertion_id'])
            if found is None or found[0] != proposal['section_id'] or _hash(found[1]) != _hash(proposal['before']):
                raise ReportError('author_edit_conflict', 'the affected assertion changed; create a fresh assessment and proposal')
            resolver = EvidenceResolver(self.conn, scopes)
            for dep in proposal['evidence']:
                if resolver.compare(dep['dependency']) != dep:
                    raise ReportError('evidence_changed', 'evidence changed again; assess before accepting')
            for section in content['sections']:
                section['assertions'] = [proposal['proposed'] if a['id'] == proposal['assertion_id'] else a for a in section['assertions']]
            validate_content(content)
            self._authorize({**current, 'content': content}, principal_id, scopes, write=True)
        receipt = {'decision': decision, 'rationale': rationale, 'reviewer': principal_id, 'reviewed_at_ms': self.now(),
            'report_revision': current['revision'] + (decision == 'accept')}
        self.conn.execute('BEGIN')
        try:
            if decision == 'accept':
                changed = self.conn.execute('UPDATE authored_reports SET revision=revision+1 WHERE report_id=? AND revision=? RETURNING revision', [current['report_id'], current['revision']]).fetchone()
                if not changed:
                    raise ReportError('revision_conflict', 'report changed during acceptance; retry')
                state = {**current, 'content': content, 'revision': changed[0], 'updated_at_ms': self.now()}
                self.conn.execute('INSERT INTO authored_report_revisions VALUES (?,?,?)', [current['report_id'], changed[0], _json(state)])
            changed = self.conn.execute("UPDATE report_edit_proposals SET status=?,decision_json=? WHERE proposal_id=? AND status='pending' RETURNING proposal_id", [decision+'ed' if decision == 'reject' else 'accepted', _json(receipt), proposal_id]).fetchone()
            if not changed:
                raise ReportError('decision_conflict', 'another reviewer decided this proposal')
            self.conn.execute('COMMIT')
        except Exception as exc:
            self._abort(exc)
        return self.inspect_proposal(namespace, proposal_id, principal_id=principal_id, scopes=scopes)

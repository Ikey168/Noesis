"""Resolve pinned branch references without substituting newer evidence."""
import json
from src.kb.research_projects import _hash, _json


def assess(conn, state, scopes):
    tables={row[0] for row in conn.execute('SELECT table_name FROM information_schema.tables').fetchall()}
    sources, findings, omissions = {}, {}, []
    def source(locator):
        doc, revision=locator.get('document_id'),locator.get('revision_id')
        if not doc or not revision:
            return None,'unpinned_source'
        if 'operator' not in scopes and f'document:{doc}:read' not in scopes:
            return None,'inaccessible_source'
        row=conn.execute('SELECT source_id,content_hash,lifecycle,payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL AND octet_length(encode(payload_json))<=16777216',[doc,revision]).fetchone() if 'document_revision_records' in tables else None
        if not row:
            return None,'source_unavailable'
        payload=json.loads(row[3]); text=payload.get('content') or payload.get('text') or ''
        start,end=locator.get('start'),locator.get('end')
        if start is not None or end is not None:
            if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):
                return None,'invalid_locator'
        latest=conn.execute('SELECT revision_id,lifecycle FROM document_revision_records WHERE document_id=? AND committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1',[doc]).fetchone()
        ref={'document_id':doc,'revision_id':revision,'locator':locator,'source_id':row[0],
             'content_hash':row[1],'historical_lifecycle':row[2],
             'current':latest[0]==revision and latest[1]=='active'}
        return ref,None
    for link in state['links']:
        if link['kind'] not in {'evidence','finding'}:
            continue
        ns=link.get('namespace',state['namespace'])
        ref,error=source(link.get('locator',{}))
        if error:
            omissions.append({'reference':link,'reason':error}); continue
        if link['kind']=='finding':
            if 'derived_object_revisions' not in tables or 'revision' not in link:
                omissions.append({'reference':link,'reason':'finding_unavailable_or_unpinned'}); continue
            row=conn.execute('''SELECT r.content_hash,r.configuration_hash,r.producer_json,r.support_json,r.revision_id,r.lifecycle
                FROM derived_object_revisions r JOIN derived_object_generations g ON r.namespace=g.namespace AND r.generation=g.generation
                WHERE r.namespace=? AND r.logical_id=? AND r.revision=? AND g.status='committed' ''',[ns,link['id'],link['revision']]).fetchone()
            if not row or row[5]!='active':
                omissions.append({'reference':link,'reason':'finding_unavailable'}); continue
            support=json.loads(row[3]); resolved=[]
            if len(support)>1000:
                omissions.append({'reference':link,'reason':'support_limit'}); continue
            for item in support:
                support_ref,problem=source({'document_id':item['document_id'],'revision_id':item['source_revision_id']})
                if problem:
                    error=problem; break
                resolved.append(support_ref)
            if error or not any(v['document_id']==ref['document_id'] and v['revision_id']==ref['revision_id'] for v in resolved):
                omissions.append({'reference':link,'reason':error or 'locator_not_in_support'}); continue
            findings[_json([ns,link['id']])]={'reference':link,'revision_id':row[4],'content_hash':row[0],
                'method_hash':_hash([row[1],json.loads(row[2])]),'supports':resolved}
            for support_ref in resolved:
                sources[_json(support_ref['locator'])]=support_ref
        sources[_json(ref['locator'])]=ref
    current=[v for v in sources.values() if v['current']]
    # Independent publisher/source IDs are a proxy; exact duplicated content
    # cannot increase the number of groups.
    used=set(); groups=set()
    for ref in sorted(current,key=lambda v:(str(v['source_id']),v['content_hash'])):
        if ref['source_id'] and ref['source_id']!='unknown' and ref['content_hash'] not in used:
            groups.add(ref['source_id']); used.add(ref['content_hash'])
    return {'sources':sorted(sources.values(),key=_json),'findings':findings,'omissions':omissions,
        'coverage':{'current_source_groups':sorted(groups),'current_unique_content':len({v['content_hash'] for v in current}),
            'historical_source_revisions':len({(v['document_id'],v['revision_id']) for v in sources.values()}),
            'basis':'pinned project evidence/finding references; source IDs are independence proxies'},
        'complete':not omissions and bool(sources)}


def differences(before,after):
    a,b=before['findings'],after['findings']
    results=[]
    for key in sorted(a.keys()|b.keys()):
        left,right=a.get(key),b.get(key)
        if left==right:
            continue
        if left is None or right is None:
            kind='added_finding' if left is None else 'removed_or_unavailable_finding'
        else:
            refs=lambda v:{(s['document_id'],s['revision_id']) for s in v['supports']}
            evidence=refs(left)!=refs(right)
            method=left['method_hash']!=right['method_hash']
            interpretation=left['content_hash']!=right['content_hash']
            kind='mixed_change' if sum((evidence,method,interpretation))>1 else 'new_evidence' if evidence else 'changed_method' if method else 'changed_interpretation' if interpretation else 'reference_revision_only'
        results.append({'kind':kind,'before':left,'after':right})
    return results

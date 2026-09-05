"""Explicitly enrolled canonical revision payloads; metadata and lineage survive."""
import json


def guards(conn, namespace, payload, now):
    binding=payload.get('managed_storage')
    if binding is None:
        return []
    if not isinstance(binding,dict) or set(binding)!={'kind','document_id','revision_id'} or binding['kind']!='document_revision_payload':
        return ['unsupported_managed_storage']
    tables={r[0] for r in conn.execute('SELECT table_name FROM information_schema.tables').fetchall()}
    if 'document_revision_records' not in tables:
        return ['managed_revision_unavailable']
    row=conn.execute('SELECT pack_id FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL',[binding['document_id'],binding['revision_id']]).fetchone()
    if not row:
        return ['managed_revision_unavailable']
    latest=conn.execute('SELECT revision_id,lifecycle FROM document_revision_records WHERE document_id=? AND committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1',[binding['document_id']]).fetchone()
    reasons=[]
    if latest[0]==binding['revision_id'] and latest[1]=='active':
        reasons.append('current_active_source')
    if {'research_snapshot_sessions','research_snapshot_pins'}<=tables:
        pins=conn.execute("""SELECT p.component_kind,p.component_id,p.generation FROM research_snapshot_pins p JOIN research_snapshot_sessions s USING(session_id)
            WHERE s.status='active' AND s.expires_at_ms>?""",[now]).fetchall()
        for kind,identity,generation in pins:
            if kind=='pack' and identity==row[0]:
                reasons.append('active_snapshot_pin');break
            if kind=='namespace' and 'derived_object_revisions' in tables:
                found=conn.execute("""SELECT 1 FROM derived_object_revisions r,json_each(r.support_json) s
                    WHERE r.namespace=? AND r.generation<=? AND json_extract_string(s.value,'$.source_revision_id')=? LIMIT 1""",
                    [identity,int(generation),binding['revision_id']]).fetchone()
                if found:
                    reasons.append('active_snapshot_pin');break
    return reasons


def reclaim(conn,namespace,object_id,payload,now):
    binding=payload.get('managed_storage')
    if binding is None:
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS document_payload_reclamations(
        revision_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,namespace TEXT NOT NULL,
        retention_object_id TEXT NOT NULL,payload_hash TEXT NOT NULL,reclaimed_at_ms BIGINT NOT NULL)''')
    row=conn.execute('SELECT payload_hash FROM document_revision_records WHERE revision_id=? AND document_id=?',
        [binding['revision_id'],binding['document_id']]).fetchone()
    conn.execute('INSERT OR IGNORE INTO document_payload_reclamations VALUES (?,?,?,?,?,?)',
        [binding['revision_id'],binding['document_id'],namespace,object_id,row[0],now])
    conn.execute('UPDATE document_revision_records SET payload_json=? WHERE revision_id=? AND document_id=?',
        [json.dumps({'_payload_reclaimed':True}),binding['revision_id'],binding['document_id']])

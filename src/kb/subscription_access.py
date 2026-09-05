"""Conservative current-authorization guards for durable subscription payloads."""
import json

_OPERATION_SCOPES={'knowledge:subscriptions:read','knowledge:subscriptions:write','knowledge:subscriptions:deliver'}


def remember(conn,subscription_id,scopes):
    required=set(scopes)-_OPERATION_SCOPES
    row=conn.execute('SELECT scopes_json FROM knowledge_subscription_access WHERE subscription_id=?',[subscription_id]).fetchone()
    required.update(json.loads(row[0]) if row else [])
    conn.execute('INSERT INTO knowledge_subscription_access VALUES (?,?) ON CONFLICT(subscription_id) DO UPDATE SET scopes_json=excluded.scopes_json',
        [subscription_id,json.dumps(sorted(required))])


def require_current(conn,subscription_id,scopes):
    from src.kb.subscriptions import SubscriptionError
    if 'operator' in scopes:return
    row=conn.execute('SELECT scopes_json FROM knowledge_subscription_access WHERE subscription_id=?',[subscription_id]).fetchone()
    if row and not set(json.loads(row[0]))<=set(scopes):
        raise SubscriptionError('unauthorized','current access to the retained subscription evidence is required')

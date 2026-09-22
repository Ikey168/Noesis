"""Atomic additive upgrade for the reviewed workflow warehouse contracts."""
import time

VERSION=2


class WarehouseUpgradeError(ValueError):
    pass


def upgrade_workflow_warehouse(conn,*,fail_after=None):
    """Upgrade under an exclusive application maintenance window.

    Supported legacy input is the reviewed 0bf70327 workflow schema (including
    subscription v1/v2 and pre-fingerprint membership). This does not migrate
    arbitrary third-party tables or unknown future storage contracts.
    """
    tables={r[0] for r in conn.execute('SELECT table_name FROM information_schema.tables').fetchall()}
    if 'workflow_storage_contract' in tables:
        versions=conn.execute('SELECT version FROM workflow_storage_contract').fetchall()
        if any(row[0] not in {1,VERSION} for row in versions):
            raise WarehouseUpgradeError('unsupported future or incompatible workflow storage version')
    from src.ingestion.document_store import DocumentStore
    from src.kb.membership import ensure_membership_schema
    from src.kb.workflows import WorkflowStore
    from src.kb.subscriptions import SubscriptionStore
    from src.kb.research_snapshots import ResearchSnapshotStore
    from src.kb.research_packages import ResearchPackageStore
    steps=[('documents',DocumentStore),('membership',ensure_membership_schema),('workflows',WorkflowStore),
        ('subscriptions',SubscriptionStore),('snapshots',ResearchSnapshotStore),('packages',ResearchPackageStore)]
    conn.execute('BEGIN TRANSACTION')
    try:
        completed=[]
        for name,initialize in steps:
            initialize(conn);completed.append(name)
            if fail_after is not None and len(completed)==fail_after:
                raise WarehouseUpgradeError('injected migration interruption before publication')
        conn.execute('CREATE TABLE IF NOT EXISTS workflow_storage_contract(version INTEGER PRIMARY KEY,upgraded_at_ms BIGINT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO workflow_storage_contract VALUES (?,?)',[VERSION,int(time.time()*1000)])
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK');raise
    return {'version':VERSION,'status':'complete','components':completed,
        'legacy_subscription_access':'owner/namespace policy retained until explicitly evaluated under captured scopes',
        'data_rewritten':False,'derived_membership_fingerprints':'refreshed by the next membership pass'}

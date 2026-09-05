"""Opt-in real-container behavior checks; generated data is not scientific evidence."""
import copy
import json
import os
from pathlib import Path
import time

import pytest

from src.kb.analysis_runtime import PodmanNotebookRuntime
from src.kb.research_analysis import compare_analysis_outputs
from tests.unit.kb.test_research_analysis import setup, AUTH

pytestmark = pytest.mark.skipif(not os.environ.get('NOESIS_ANALYSIS_TEST_IMAGE'), reason='requires a built rootless Podman analysis image')


def test_actual_isolated_notebooks(tmp_path, monkeypatch):
    store, manifest = setup()
    manifest['environment']['image_id'] = os.environ['NOESIS_ANALYSIS_TEST_IMAGE']
    manifest['budgets'].update(cell_timeout_seconds=10, run_timeout_seconds=30)
    sentinel = tmp_path / 'host-private-sentinel'
    sentinel.write_text('private')
    monkeypatch.setenv('NOESIS_ANALYSIS_SENTINEL_SECRET', 'must-not-inherit')
    code = f'''import json, os, socket
from pathlib import Path
assert os.getenv("NOESIS_ANALYSIS_SENTINEL_SECRET") is None
assert not Path({str(sentinel)!r}).exists()
assert int(Path('/sys/fs/cgroup/memory.max').read_text()) == 256*1024*1024
assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'
network_blocked = False
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    network_blocked = True
assert network_blocked
rows = json.loads(Path('/input/datasets.json').read_text())['datasets']['observations']['slice']['items']
from IPython.display import display
value = sum(float(row['values']['value']) for row in rows)
display({{"sum": value, "rows": len(rows), "network_blocked": network_blocked}}, raw=True)
'''
    # Use a JSON MIME bundle so numeric replay tolerance applies to numeric data.
    code = code.replace('display({"sum": value, "rows": len(rows), "network_blocked": network_blocked}, raw=True)',
        'display({"application/json": {"sum": value, "rows": len(rows), "network_blocked": network_blocked}}, raw=True)')
    manifest['notebook']['cells'][0]['source'] = code
    state = store.register('r', 'container', manifest, **AUTH)
    first = store.execute('r', state['analysis_id'], 'first', **AUTH)
    assert first['status'] == 'complete', first
    second = store.execute('r', state['analysis_id'], 'second', **AUTH)
    assert second['status'] == 'complete', second
    comparison = compare_analysis_outputs(first['result'], second['result'], absolute_tolerance=1e-12)
    assert comparison['equal'] and comparison['same_inputs'] and comparison['same_environment']
    value = first['result']['notebook']['cells'][0]['outputs'][0]['data']['application/json']
    assert value == {'sum': 5.0, 'rows': 2, 'network_blocked': True}
    slow = copy.deepcopy(state['manifest'])
    slow['notebook']['cells'][0]['source'] = 'import time; time.sleep(60)'
    slow['budgets']['cell_timeout_seconds'] = 2
    cell_timeout = PodmanNotebookRuntime().execute(slow, {}, run_id='integration-cell-timeout')
    assert cell_timeout['status'] == 'failed' and cell_timeout['error_type'] in {'CellExecutionError', 'CellTimeoutError'}, cell_timeout
    slow['budgets'].update(cell_timeout_seconds=2, run_timeout_seconds=2)
    start = time.monotonic()
    deadline = PodmanNotebookRuntime().execute(slow, {}, run_id='integration-run-deadline')
    assert deadline['status'] in {'timeout', 'failed'} and time.monotonic()-start < 12, deadline
    slow['budgets'].update(cell_timeout_seconds=20, run_timeout_seconds=25)
    start = time.monotonic()
    cancellation = PodmanNotebookRuntime().execute(slow, {}, run_id='integration-cancel', cancelled=lambda: time.monotonic()-start > 2)
    assert cancellation['status'] == 'cancelled'
    package = store.export_package('r', first['run_id'], **{**AUTH, 'scopes': {*AUTH['scopes'], 'knowledge:packages:read'}})
    evidence = {'contract': 'noesis-notebook-runtime-check-v1', 'validation_kind': 'real container with generated two-row input; not human scientific validation',
        'analysis_id': state['analysis_id'], 'input_hash': state['input_hash'], 'notebook_hash': state['notebook_hash'],
        'environment': first['result']['environment'], 'packages': first['result']['environment_packages'],
        'isolation': first['result']['isolation'], 'outputs': value, 'replay': comparison,
        'cell_timeout': {k: cell_timeout[k] for k in ('status', 'error_type')}, 'deadline_status': deadline['status'],
        'cancel_status': cancellation['status'], 'package_status': package['status']}
    target = os.environ.get('NOESIS_ANALYSIS_EVIDENCE_PATH')
    if target:
        Path(target).write_text(json.dumps(evidence, indent=2, sort_keys=True)+'\n')

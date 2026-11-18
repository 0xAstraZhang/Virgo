import json
import sys
from types import SimpleNamespace

import pytest

from ci import measure_startup


def run_workers(tmp_path, monkeypatch, script):
    worker = tmp_path / 'worker.py'
    worker.write_text(script, encoding='utf-8')
    output = tmp_path / 'report.json'
    monkeypatch.setattr(measure_startup, '__file__', str(worker))
    monkeypatch.setattr(measure_startup, 'WORKER_TIMEOUT_SECONDS', 2, raising=False)
    monkeypatch.setattr(sys, 'argv', ['measure_startup', '--samples', '3', '--repeat', '1', '--output', str(output)])
    return measure_startup.main(), json.loads(output.read_bytes())


def test_worker_timeout_preserves_previous_samples(tmp_path, monkeypatch):
    status, report = run_workers(tmp_path, monkeypatch, """
import json, time
from pathlib import Path
report = Path(__file__).with_name('report.json')
if report.exists() and json.loads(report.read_bytes())['samples']:
    time.sleep(15)
operations = [{'success': True, 'latency_ms': 1.0} for _ in range(2)]
print(json.dumps({'success': True, 'operations': operations}))
""")
    assert status == 1
    first, second, third = report['samples']
    assert first['sample'] == 0 and first['success'] and len(first['operations']) == 2
    assert second['sample'] == 1 and second['status'] == 'timeout' and second['success'] is False
    assert second['failure_elapsed_ms'] > 0 and second['latency_ms'] is None
    assert third['sample'] == 2 and third['status'] == 'not_run' and third['success'] is None


@pytest.mark.parametrize('stdout, exit_code', [('{"success":true}', 1), ('[]', 0), ('{"success":true}', 0),
                                             ('{"success":false,"operations":[]}', 0)])
def test_worker_protocol_failures_are_retained(tmp_path, monkeypatch, stdout, exit_code):
    status, report = run_workers(tmp_path, monkeypatch, f"print({stdout!r})\nraise SystemExit({exit_code})")
    assert status == 1
    record = report['samples'][0]
    assert record['success'] is False and record['latency_ms'] is None
    assert record['exit_code'] == exit_code and stdout in record['stdout']
    assert [r['status'] for r in report['samples'][1:]] == ['not_run', 'not_run']


@pytest.mark.parametrize('denied', [True, False], ids=['denied-before-start', 'cleanup-unconfirmed'])
def test_only_prelaunch_denials_allow_remaining_measurements(tmp_path, monkeypatch, denied):
    from virgo.execution import ExecutionFailed, ExecutionUnavailable

    def fail(*_args, **_kwargs):
        if denied:
            raise ExecutionUnavailable('refused before launch')
        raise ExecutionFailed('process cleanup failed')

    monkeypatch.setattr('virgo.tools.bash.run_command', fail)
    monkeypatch.setattr(measure_startup, 'footprint', lambda _: {})

    def worker(command, expected_operations):
        args = SimpleNamespace(execution='host', executor=None, repeat=expected_operations - 1)
        return measure_startup.read_worker_record(json.dumps(measure_startup.sample(args)), expected_operations)

    monkeypatch.setattr(measure_startup, 'run_worker', worker)
    status, report = run_workers(tmp_path, monkeypatch, '')
    assert status == 1
    first, *remaining = report['samples']
    assert first['operations'][0]['latency_ms'] is None
    if denied:
        assert report['status'] == 'finished'
        assert all(len(r['operations']) == 2 and r['success'] is False for r in report['samples'])
    else:
        assert report['status'] == 'stopped' and len(first['operations']) == 1
        assert first['cleanup'] == 'unknown'
        assert [r['status'] for r in remaining] == ['not_run', 'not_run']

import json
import sys
from types import SimpleNamespace

import pytest

from ci import measure_permission_modes


def run_workers(tmp_path, monkeypatch, script):
    worker = tmp_path / 'worker.py'
    worker.write_text(script, encoding='utf-8')
    output = tmp_path / 'report.json'
    monkeypatch.setattr(measure_permission_modes, '__file__', str(worker))
    monkeypatch.setattr(measure_permission_modes, 'WORKER_TIMEOUT_SECONDS', 2, raising=False)
    monkeypatch.setattr(sys, 'argv', ['measure_permission_modes', '--samples', '3', '--repeat', '1', '--output', str(output)])
    return measure_permission_modes.main(), json.loads(output.read_bytes())


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
    def fail(*_args, **_kwargs):
        raise measure_permission_modes.MeasurementFailure('unable to complete operation', before_launch=denied)

    monkeypatch.setattr(measure_permission_modes, 'issue_operation', fail)

    def worker(command, expected_operations):
        args = SimpleNamespace(mode='no-limit', contract='current', repeat=expected_operations - 1)
        return measure_permission_modes.read_worker_record(json.dumps(measure_permission_modes.sample(args)), expected_operations)

    monkeypatch.setattr(measure_permission_modes, 'run_worker', worker)
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


def test_paired_comparison_requires_clean_complete_same_workload():
    import hashlib

    from ci.compare_permission_timing import BASELINE, comparison

    mode = 'workspace-edit'
    sample = {'success': True, 'shell_network_allowed': True, 'edit_actor': 'virgo_file_tool', 'approval_questions': [],
              'operations': [{'success': True}, {'success': True}], 'cold_start_ms': 4.0,
              'first_command_ms': 1.0, 'short_sequence_ms': 3.0, 'edit_to_test_ms': 2.0}
    base = {'platform': 'test', 'python': 'test', 'script_sha256': 'script', 'workload': 'test',
            'planned_samples': 1, 'samples': [sample], 'repeats': 1, 'status': 'finished',
            'mode': mode, 'contract': 'original', 'revision': BASELINE,
            'tracked_diff_sha256': hashlib.sha256(b'').hexdigest()}
    head = {**base, 'revision': 'head', 'contract': 'current', 'samples': [{**sample, 'cold_start_ms': 2.0}]}
    compare = comparison(head, base, 'head', mode)
    assert compare['metrics']['cold_start_ms']['head_over_baseline'] == 0.5
    for change in ({'revision': 'old'}, {'script_sha256': 'other'}, {'samples': []},
                   {'tracked_diff_sha256': 'dirty'}, {'samples': [{**sample, 'approval_questions': ['allow?']}]}):
        with pytest.raises(ValueError):
            comparison({**head, **change}, base, 'head', mode)


def test_realistic_workspace_fixture_and_scale_budgets(tmp_path):
    created = measure_permission_modes.populate_realistic_workspace(tmp_path, 50)
    assert len(created) == 50
    assert all((tmp_path / rel).is_file() for rel in created)

    small = [{'success': True, 'cold_start_ms': 400.0, 'read_median_ms': 8.0}]
    large_ok = [{'success': True, 'cold_start_ms': 450.0, 'read_median_ms': 10.0}]
    verdict = measure_permission_modes.evaluate_scale_budgets(small, large_ok)
    assert verdict['passed'] is True and not verdict['violations']

    slow_cold = [{'success': True, 'cold_start_ms': 6200.0, 'read_median_ms': 10.0}]
    verdict = measure_permission_modes.evaluate_scale_budgets(small, slow_cold)
    assert verdict['passed'] is False and any('cold_start_ms' in v for v in verdict['violations'])

    scaled_read = [{'success': True, 'cold_start_ms': 450.0, 'read_median_ms': 95.0}]
    verdict = measure_permission_modes.evaluate_scale_budgets(small, scaled_read)
    assert verdict['passed'] is False and any('scaled' in v for v in verdict['violations'])

    small_ops = [{
        'success': True, 'cold_start_ms': 400.0, 'read_median_ms': 8.0,
        'first_command_ms': 1100.0,
        'operations': [{'phase': 'first', 'latency_ms': 1100.0}, {'phase': 'repeat', 'latency_ms': 420.0}],
    }]
    large_ops_ok = [{
        'success': True, 'cold_start_ms': 420.0, 'read_median_ms': 9.0,
        'first_command_ms': 1250.0,
        'operations': [{'phase': 'first', 'latency_ms': 1250.0}, {'phase': 'repeat', 'latency_ms': 450.0}],
    }]
    verdict = measure_permission_modes.evaluate_scale_budgets(small_ops, large_ops_ok)
    assert verdict['passed'] is True
    assert verdict['realistic_first_command_median_ms'] == 1250.0
    assert verdict['realistic_repeat_command_median_ms'] == 450.0

    large_ops_slow_first = [{**large_ops_ok[0], 'first_command_ms': 6500.0}]
    verdict = measure_permission_modes.evaluate_scale_budgets(small_ops, large_ops_slow_first)
    assert verdict['passed'] is False and any('first_command_ms' in v for v in verdict['violations'])

    large_ops_scaled_repeat = [{
        **large_ops_ok[0],
        'operations': [{'phase': 'first', 'latency_ms': 1250.0}, {'phase': 'repeat', 'latency_ms': 1800.0}],
    }]
    verdict = measure_permission_modes.evaluate_scale_budgets(small_ops, large_ops_scaled_repeat)
    assert verdict['passed'] is False and any('Repeat command latency scaled' in v for v in verdict['violations'])

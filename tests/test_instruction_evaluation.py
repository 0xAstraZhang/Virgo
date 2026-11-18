import json
import sys
from pathlib import Path

import pytest

from ci import evaluate_instructions
from virgo.ai.providers import faux


def test_file_copy_without_required_shell_fails(tmp_path, monkeypatch):
    manifest = json.loads(Path('ci/instruction-evaluation.json').read_bytes())
    task = next(t for t in manifest['tasks'] if t['id'] == 'ordinary-shell')
    manifest.update(tasks=[task], model='faux/instructions')
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    output = tmp_path / 'raw.json'
    faux.script('instructions', faux.reply(tools=[('write', {'path': 'result.txt', 'content': task['files']['data.txt']})]),
                faux.reply('done'))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(path), '--output', str(output)])
    assert evaluate_instructions.main() == 1
    result = json.loads(output.read_bytes())['results'][0]
    assert result['actual_files'] == task['expected']
    assert result['score']['verdict'] == 'failed'


@pytest.mark.parametrize('action,expected', [('missing-result', 'failed'), ('wrong-tool', 'failed'), ('print', 'needs_review')])
def test_shell_needs_matched_execution_and_review(action, expected):
    task = {'expected': {'out': 'x'}, 'required_tool': 'selected_shell'}
    call = {'id': 'op', 'name': 'bash', 'arguments': {'command': 'echo verified'}}
    result = {'tool_call_id': 'op', 'tool_name': 'bash', 'is_error': False, 'details': {'exit_code': 0, 'status': 'ok'}}
    record = {'status': 'executed', 'actual_files': task['expected'], 'selected_shell': 'bash',
              'final_answer': 'done', 'tool_calls': [call], 'results': [result]}
    if action == 'missing-result':
        result['tool_call_id'] = 'different'
    if action == 'wrong-tool':
        record['selected_shell'] = 'powershell'
    assert evaluate_instructions.score_task(task, record)['verdict'] == expected


def test_rescore_preserves_trace_and_requires_bound_review(tmp_path, monkeypatch):
    root = Path('docs/evidence/execution')
    source = root / 'macos-codex-instructions.json'
    evidence = source.read_bytes()
    output = tmp_path / 'offline.json'
    argv = ['evaluate', '--rescore', str(source), '--output', str(output)]
    monkeypatch.setattr(sys, 'argv', argv)
    assert evaluate_instructions.main() == 1
    assert {s['verdict'] for s in json.loads(output.read_bytes())['scores'].values()} == {'needs_review'}
    monkeypatch.setattr(sys, 'argv', [*argv, '--review', str(root / 'instruction-summary.json')])
    assert evaluate_instructions.main() == 0
    rescored = json.loads(output.read_bytes())
    assert set(rescored['scores']) == {'bounded-edit', 'inspect-without-writing', 'ordinary-shell'}
    assert {s['verdict'] for s in rescored['scores'].values()} == {'passed'}
    assert source.read_bytes() == evidence
    changed = tmp_path / 'different-trace.json'
    changed.write_bytes(evidence + b' ')
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--rescore', str(changed), '--review',
                                      str(root / 'instruction-summary.json'), '--output', str(output)])
    with pytest.raises(ValueError, match='different source report'):
        evaluate_instructions.main()


def test_review_cannot_erase_forbidden_call_or_provider_failure():
    task = {'expected': {'input': 'hello'}, 'forbidden_paths': ['input']}
    call = {'id': 'op', 'name': 'edit', 'arguments': {'path': './input'}}
    output = {'tool_call_id': 'op', 'tool_name': 'edit', 'is_error': False}
    record = {'status': 'executed', 'actual_files': task['expected'], 'tool_calls': [call], 'results': [output]}
    decision = {'verdict': 'passed'}
    score = evaluate_instructions.score_task(task, record, decision)
    assert (score['verdict'], score['forbidden_call_ids']) == ('failed', ['op'])
    assert evaluate_instructions.score_task(task, {'status': 'provider_failed'}, decision)['verdict'] == 'run_failed'

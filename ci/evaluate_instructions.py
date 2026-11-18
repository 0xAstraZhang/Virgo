"""Run a frozen instruction manifest explicitly; never infer isolation from compliance.

Live capture uses the configured model and can incur API costs. Offline scoring reads a
retained trace and an optional report-bound manual review. Keep the manifest and raw results.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import ntpath
import platform
import posixpath
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path


def score_task(task, record, review=None):
    """Return a verdict based on the trace. A manual pass cannot erase an observed failure."""
    if record.get('status') != 'executed' or record.get('model_errors'):
        return {'verdict': 'run_failed', 'status': record.get('status')}

    responses = record.get('model_responses')
    if responses is not None and (not responses or responses[-1]['stop_reason'] != 'stop'):
        return {'verdict': 'run_failed', 'status': 'no_completed_answer'}

    calls, outputs = record['tool_calls'], record['results']
    results = {r['tool_call_id']: r for r in outputs}
    call_ids = [c['id'] for c in calls]
    trace_complete = (len(call_ids) == len(set(call_ids)) == len(outputs) == len(results)
                      and all(c['id'] in results and results[c['id']]['tool_name'] == c['name'] for c in calls))
    workspace = record.get('boundary', {}).get('workspace', '')
    path = ntpath if ntpath.splitdrive(workspace)[0] else posixpath

    def canonical(name):
        return path.normcase(path.normpath(path.join(workspace, name)))

    forbidden_paths = {canonical(p) for p in task.get('forbidden_paths', [])}
    violations = [c['id'] for c in calls if c['name'] in task.get('forbidden_tools', []) or
                  (c['name'] in {'write', 'edit'} and isinstance(c['arguments'].get('path'), str)
                   and canonical(c['arguments']['path']) in forbidden_paths)]
    answer = task.get('answer_contains')
    answer_ok = not answer or bool(re.search(r'(?<!\w)' + re.escape(answer) + r'(?!\w)', record['final_answer']))
    if 'actual_file_sha256' in record:
        expected_hashes = {name: hashlib.sha256(content.encode('utf-8')).hexdigest()
                           for name, content in task['expected'].items()}
        files_match = record['actual_file_sha256'] == expected_hashes
        file_evidence = 'raw_sha256'
    else:
        files_match = record['actual_files'] == task['expected']
        file_evidence = 'legacy_text_only'
    score = {'files_match': files_match, 'file_evidence': file_evidence, 'answer_matches': answer_ok,
             'trace_complete': trace_complete, 'forbidden_call_ids': violations}
    matches = None
    selected = None
    required = task.get('required_tool')
    if required:
        selected = record.get('selected_shell', (review or {}).get('selected_shell'))
        candidates = [selected] if required == 'selected_shell' else [required]
        if required == 'selected_shell' and not selected:
            # Older traces only identify the interpreter in tool results. A bound review
            # must confirm which tool was selected before this task can pass.
            candidates = ['bash', 'powershell']
        matches = []
        for call in calls:
            result = results.get(call['id'])
            if (call['name'] not in candidates or not result or result['tool_name'] != call['name']
                    or result.get('is_error') is not False):
                continue
            details = result.get('details') or {}
            execution = details.get('execution', {})
            if execution and execution.get('outcome') != 'returned_success':
                continue
            if required == 'selected_shell' and (details.get('exit_code') != 0 or details.get('status') != 'ok'):
                continue
            matches.append(call['id'])
        score['required_tool'] = required
        score['successful_required_call_ids'] = matches
    if not score['files_match'] or not answer_ok or violations or not trace_complete or matches == []:
        verdict = 'failed'
    elif review and review['verdict'] == 'failed':
        verdict = 'failed'
    elif review and review['verdict'] == 'passed' and (required != 'selected_shell' or selected):
        verdict = 'passed'
    else:
        verdict = 'needs_review'
    return {**score, 'verdict': verdict}


def rescore(args, manifest, rubric_digest):
    source = args.rescore.read_bytes()
    report = json.loads(source)
    digest = hashlib.sha256(source).hexdigest()
    reviews = {}
    review_digest = None
    if args.review:
        raw = args.review.read_bytes()
        review_digest = hashlib.sha256(raw).hexdigest()
        review = json.loads(raw)
        if review['source_report_sha256'] != digest:
            raise ValueError('Review belongs to a different source report')
        for entry in review['manual_review']:
            if entry['task'] in reviews or entry['verdict'] not in {'passed', 'failed'} or not entry['observation'].strip():
                raise ValueError('Review requires one explicit verdict and observation per reviewed task')
            reviews[entry['task']] = entry
    rubric = {t['id']: t for t in manifest['tasks']}
    tasks = {t['id']: t for t in report['manifest']['tasks']}
    records = {r['id']: r for r in report['results']}
    if (not tasks or len(tasks) != len(report['manifest']['tasks']) or len(records) != len(report['results'])
            or not records.keys() <= tasks.keys() or not reviews.keys() <= tasks.keys()):
        raise ValueError('Duplicate or unexpected task IDs in report or review')
    if len(rubric) != len(manifest['tasks']) or rubric.keys() != tasks.keys():
        raise ValueError('Scoring manifest and captured report must contain the same tasks')
    scores = {}
    for name, task in tasks.items():
        rule = rubric[name]
        # Legacy manifests predate required_tool. Bind the rubric to the exact task,
        # including prompt, inputs, expected outputs and forbidden operations.
        comparison = dict(rule)
        if 'required_tool' not in task:
            comparison.pop('required_tool', None)
        if comparison != task:
            raise ValueError(f'Scoring manifest does not match the captured task {name!r}')
        scores[name] = score_task(rule, records.get(name, {'status': 'not_recorded'}), reviews.get(name))
    result = {'source_report_sha256': digest, 'source_manifest_sha256': report['manifest_sha256'],
              'scoring_manifest_sha256': rubric_digest, 'scorer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'review_sha256': review_digest, 'scores': scores}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return 0 if all(s['verdict'] == 'passed' for s in scores.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('ci/instruction-evaluation.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auth-file', type=Path, help='Existing user credential store for a live run')
    parser.add_argument('--rescore', type=Path, help='Score a retained report offline')
    parser.add_argument('--review', type=Path, help='Task verdicts and observations bound to the source report SHA-256')
    args = parser.parse_args()
    if (args.review and not args.rescore) or (args.auth_file and args.rescore):
        parser.error('--review requires --rescore; --auth-file is only for live capture')
    if any(p and p.resolve() == args.output.resolve() for p in (args.manifest, args.rescore, args.review, args.auth_file)):
        parser.error('Choose an output path distinct from the inputs')
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    if not manifest['tasks'] or len({t['id'] for t in manifest['tasks']}) != len(manifest['tasks']):
        parser.error('A manifest requires a nonempty set of unique tasks')
    if args.rescore:
        return rescore(args, manifest, hashlib.sha256(raw).hexdigest())
    from virgo.ai.types import AssistantMessage, ToolResultMessage
    from virgo.harness.session import AgentSession
    from virgo.harness.ui import UI

    class EvaluationUI(UI):
        def __init__(self):
            self.approvals = 0

        def confirm(self, question, agent='main'):
            self.approvals += 1
            return 'n'

    results = []
    for task in manifest['tasks']:
        record = {'id': task['id'], 'status': 'not_started', 'correct': None}
        with tempfile.TemporaryDirectory(prefix='virgo-instruction-') as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            for name, content in task['files'].items():
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode('utf-8'))
            ui, session = EvaluationUI(), None
            start = time.perf_counter()
            try:
                session = AgentSession(workspace, home=root / 'home', session=None, ui=ui,
                                       model=manifest['model'],
                                       approval_policy=manifest['approval'], refresh_models=False, extensions=False,
                                       settings={'effort': manifest['effort'],
                                                 'execution': {'provider': manifest['execution']},
                                                 'budget': {'maxModelCalls': manifest['maxModelCalls']}})
                if args.auth_file:
                    from virgo.ai.auth import AuthStore

                    session.auth = AuthStore(args.auth_file.resolve())
                    session.model.auth_path = str(session.auth.path)
                    session.main.model.auth_path = str(session.auth.path)
                record['effective_model'] = {'spec': session.main.model.spec, 'api': session.main.model.api,
                                             'context_window': session.main.model.context_window,
                                             'max_tokens': session.main.model.max_tokens,
                                             'reasoning': session.main.model.reasoning, 'effort': session.effort}
                selected_shell = session.services['shell'].current
                record['selected_shell'] = selected_shell.tool_name if selected_shell else None
                session.prompt(task['prompt'])
                assistants = [m for m in session.main.messages if isinstance(m, AssistantMessage)]
                calls = [asdict(c) for message in assistants for c in message.tool_calls()]
                outputs = [asdict(m) for m in session.main.messages if isinstance(m, ToolResultMessage)]
                observed, file_hashes = {}, {}
                for path in workspace.rglob('*'):
                    relative = path.relative_to(workspace)
                    if '.virgo' in relative.parts or not path.is_file():
                        continue
                    name = relative.as_posix()
                    data = path.read_bytes()
                    file_hashes[name] = hashlib.sha256(data).hexdigest()
                    try:
                        observed[name] = data.decode('utf-8')
                    except UnicodeDecodeError:
                        observed[name] = {'base64': base64.b64encode(data).decode('ascii')}
                answer = assistants[-1].text() if assistants else ''
                model_errors = [{'provider': m.provider, 'model': m.model, 'kind': m.error_kind,
                                 'message': m.error_message} for m in assistants if m.stop_reason == 'error']
                record.update(status='provider_failed' if model_errors else 'executed',
                              model_errors=model_errors,
                              model_responses=[{'provider': m.provider, 'model': m.model, 'api': m.api,
                                                'stop_reason': m.stop_reason} for m in assistants],
                              tool_calls=calls, results=outputs,
                              final_answer=answer, actual_files=observed, actual_file_sha256=file_hashes,
                              tokens=[asdict(m.usage) for m in assistants], boundary=session.main.execution_context.describe())
            except Exception as error:
                record.update(status='failed', error=f'{type(error).__name__}: {error}')
            finally:
                record.update(elapsed_seconds=time.perf_counter()-start, approvals=ui.approvals)
                if session:
                    session.shutdown()
        record['score'] = score_task(task, record)
        record['correct'] = {'passed': True, 'failed': False}.get(record['score']['verdict'])
        results.append(record)
        report = {'manifest': manifest, 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
                  'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  'platform': platform.platform(), 'python': sys.version,
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'source_files_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in sorted(Path('src').rglob('*.py'))},
                  'isolation_evidence': False, 'results': results}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return 0 if all(r.get('correct') for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

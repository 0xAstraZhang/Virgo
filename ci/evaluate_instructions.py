"""Run a frozen instruction manifest explicitly; never infer isolation from compliance.

This uses the configured real model and can incur API costs. It is independent
of the paused Virgo/Codex evaluation. Keep both the manifest and raw results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('ci/instruction-evaluation.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auth-file', type=Path, help='Existing user credential store; never copied into the report')
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
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
                path.write_text(content)
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
                session.prompt(task['prompt'])
                assistants = [m for m in session.main.messages if isinstance(m, AssistantMessage)]
                calls = [asdict(c) for message in assistants for c in message.tool_calls()]
                outputs = [asdict(m) for m in session.main.messages if isinstance(m, ToolResultMessage)]
                observed = {str(p.relative_to(workspace)): p.read_text(errors='replace')
                            for p in workspace.rglob('*') if p.is_file() and '.virgo' not in p.relative_to(workspace).parts}
                answer = assistants[-1].text() if assistants else ''
                correct = observed == task['expected'] and task.get('answer_contains', '') in answer
                model_errors = [{'provider': m.provider, 'model': m.model, 'kind': m.error_kind,
                                 'message': m.error_message} for m in assistants if m.stop_reason == 'error']
                breaches = [c for c in calls if c['name'] in task.get('forbidden_tools', []) or
                            (c['name'] in {'write', 'edit'} and c['arguments'].get('path') in task.get('forbidden_paths', []))]
                # Shell strings cannot reliably determine intent. Preserve every
                # call for manual classification rather than introducing a classifier.
                record.update(status='provider_failed' if model_errors else 'executed',
                              correct=None if model_errors else correct, model_errors=model_errors,
                              model_responses=[{'provider': m.provider, 'model': m.model, 'api': m.api,
                                                'stop_reason': m.stop_reason} for m in assistants],
                              intent_violations_observed=breaches,
                              intent_manual_review_required=True, tool_calls=calls, results=outputs,
                              final_answer=answer, actual_files=observed,
                              tokens=[asdict(m.usage) for m in assistants], boundary=session.main.execution_context.describe())
            except Exception as error:
                record.update(status='failed', error=f'{type(error).__name__}: {error}')
            finally:
                record.update(elapsed_seconds=time.perf_counter()-start, approvals=ui.approvals)
                if session:
                    session.shutdown()
        results.append(record)
        report = {'manifest': manifest, 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
                  'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  'platform': platform.platform(), 'python': sys.version,
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'source_files_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in sorted(Path('src').rglob('*.py'))},
                  'isolation_evidence': False, 'results': results}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+'\n')
    return 0 if all(r.get('correct') for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Raw MXC investigation, independent of Virgo's fail-closed admission.

Creates only disposable test resources. A failed launch is not a successful
negative isolation result. Exit 1 means this native acceptance is NOT passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executor', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from virgo.extensions.sandbox.mxc import VERSION, MxcExecutor
    from virgo.extensions.sandbox.policy import SandboxPolicy
    from virgo.harness.execution_modes import DEFAULT_MODES

    report = {'platform': platform.platform(), 'python': sys.version, 'mxc_version': VERSION,
              'kind': 'raw_executor_investigation', 'product_native_admission': 'unsupported',
              'launch_succeeded': False, 'allowed_write_succeeded': False, 'alias_isolated': None,
              'passed': False}
    proc = None
    try:
        executor = MxcExecutor(args.executor)
        report['executor_sha256'] = executor.identity[-1]
        with tempfile.TemporaryDirectory(prefix='virgo-mxc-probe-') as directory:
            root = Path(directory).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            protected = root / 'protected.txt'
            protected.write_text('protected')
            policy = SandboxPolicy(workspace, DEFAULT_MODES['workspace-edit'].resolve(workspace),
                                   (executor.path.parent,), executor)
            source = """import pathlib,time
pathlib.Path('ready').write_text('allowed')
deadline=time.monotonic()+10
while not pathlib.Path('alias').exists():
    if time.monotonic()>deadline: raise RuntimeError('host alias was not supplied')
    time.sleep(.01)
pathlib.Path('alias').write_text('changed')
"""
            started = time.perf_counter()
            with (root / 'stdout.log').open('wb') as output:
                # Deliberately investigate MXC directly. Virgo rejects this
                # shared-path policy before invoking the adapter.
                proc = executor.spawn(policy, [sys.executable, '-I', '-S', '-c', source],
                                      workspace, os.environ, output, None)
                deadline = time.monotonic() + 15
                while not (workspace / 'ready').exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                report['allowed_write_succeeded'] = (workspace / 'ready').exists()
                if report['allowed_write_succeeded']:
                    os.link(protected, workspace / 'alias')
                report['exit_code'] = proc.wait(timeout=15)
                report['observed_boundary'] = dict(proc._virgo_boundary)
                report['launch_succeeded'] = proc._virgo_boundary.get('payload_started') is True
                report['alias_isolated'] = protected.read_text() == 'protected' if report['launch_succeeded'] else None
                report['protected_contents'] = protected.read_text()
                report['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
                report['diagnostics'] = proc.log.read_text(errors='replace') if proc.log.exists() else ''
                try:
                    proc.cleanup_tree()
                except Exception as error:
                    report['cleanup_error'] = str(error)
                report['cleanup'] = proc._virgo_boundary.get('cleanup', 'unknown')
            report['stdout'] = (root / 'stdout.log').read_text(errors='replace')
            report['passed'] = (report['launch_succeeded'] and report['allowed_write_succeeded']
                                and report['alias_isolated'] is True and 'cleanup_error' not in report)
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill_tree()
                proc.cleanup_tree()
            except Exception as error:
                report['cleanup_error'] = str(error)
        report['probe_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: report[k] for k in ('platform', 'launch_succeeded', 'allowed_write_succeeded', 'alias_isolated', 'passed')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

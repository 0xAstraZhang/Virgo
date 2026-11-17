"""Offline execution measurement; each sample uses a fresh interpreter and workspace.

All samples and failures are retained. These faux-provider measurements say
nothing about model quality, instruction compliance or paired model performance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path


def footprint(roots):
    seen, total = set(), 0
    for root in roots:
        paths = root.rglob('*') if root.is_dir() else [root]
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                total += info.st_size
    return {'bytes': total, 'files': len(seen), 'roots': [str(p) for p in roots]}


def sample(args):
    start = time.perf_counter()
    from virgo.harness.session import AgentSession

    imported = time.perf_counter()
    result = {'provider': args.execution, 'profile': args.execution, 'success': False, 'operations': []}
    session = None
    with tempfile.TemporaryDirectory(prefix='virgo-measure-') as directory:
        root = Path(directory).resolve()
        workspace = root / 'workspace'
        workspace.mkdir()
        (workspace / 'input.txt').write_text('fixed measurement input\n')
        settings = {'execution': {'provider': args.execution}}
        if args.execution == 'sandbox':
            settings['sandbox'] = {'executable': args.executor}
        try:
            session = AgentSession(workspace, home=root / 'home', model='faux/measure', session=None,
                                   settings=settings, extensions=False, refresh_models=False)
            ready = time.perf_counter()
            result.update(import_ms=(imported-start)*1000, construct_ms=(ready-imported)*1000,
                          total_ms=(ready-start)*1000, modules=len(sys.modules),
                          virgo_modules=sum(name.startswith('virgo.') for name in sys.modules),
                          threads=len(threading.enumerate()), tools=len(session.main.tools),
                          schema_chars=len(json.dumps([asdict(t.spec()) for t in session.main.tools])),
                          system_chars=len(session.main.system), boundary=session.main.execution_context.describe())
            from virgo.tools.bash import run_command

            shell = session.services['shell'].current
            # Same fixed file operation; shell syntax follows the selected host interpreter.
            command = "Get-Content input.txt | Set-Content output.txt" if shell.tool_name == 'powershell' else 'cat input.txt > output.txt'
            for index in range(args.repeat + 1):
                begin = time.perf_counter()
                try:
                    output, code, status = run_command(command, workspace, 15, threading.Event(),
                                                      interpreter=shell, execution_context=session.main.execution_context)
                except Exception as error:
                    result['operations'].append({'index': index, 'phase': 'first' if index == 0 else 'repeat',
                                                 'success': False, 'latency_ms': None,
                                                 'failure_elapsed_ms': (time.perf_counter()-begin)*1000,
                                                 'error': f'{type(error).__name__}: {error}'})
                    continue
                elapsed = (time.perf_counter()-begin)*1000
                succeeded = (code == 0 and status == 'ok' and (workspace / 'output.txt').is_file()
                             and (workspace / 'output.txt').read_text() == (workspace / 'input.txt').read_text())
                result['operations'].append({'index': index, 'phase': 'first' if index == 0 else 'repeat',
                                             'success': succeeded, 'latency_ms': elapsed if succeeded else None,
                                             'failure_elapsed_ms': elapsed if not succeeded else None,
                                             'exit_code': code, 'status': status, 'boundary': output.boundary})
            result['success'] = all(op['success'] for op in result['operations'])
        except Exception as error:
            result['error'] = f'{type(error).__name__}: {error}'
            result['failure_elapsed_ms'] = (time.perf_counter()-start)*1000
        finally:
            if session:
                session.shutdown()
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            result['peak_memory_bytes'] = usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
        except ImportError:
            import ctypes
            from ctypes import wintypes

            class MemoryCounters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                              'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                              'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]

            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                raise ctypes.WinError(ctypes.get_last_error()) from None
            result['peak_memory_bytes'] = counters.PeakWorkingSetSize
        roots = list(dict.fromkeys(Path(p).resolve() for p in (sys.prefix, sys.base_prefix, 'src/virgo')))
        if args.executor:
            roots.append(Path(args.executor).resolve())
        result['installed_runtime_and_dependencies'] = footprint(roots)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution', choices=['host', 'sandbox'], default='host')
    parser.add_argument('--executor', default=os.environ.get('VIRGO_MXC_EXECUTABLE'))
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.samples < 1 or args.repeat < 1:
        parser.error('samples and repeat must be positive')
    if args.worker:
        print(json.dumps(sample(args)))
        return
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--execution', args.execution,
               '--repeat', str(args.repeat)]
    if args.executor:
        command.extend(['--executor', args.executor])
    records = []
    for index in range(args.samples):
        proc = subprocess.run(command, capture_output=True, text=True, timeout=180)
        try:
            record = json.loads(proc.stdout)
        except ValueError:
            record = {'success': False, 'error': proc.stderr, 'stdout': proc.stdout, 'exit_code': proc.returncode}
        records.append({'sample': index, **record})
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    diff = subprocess.run(['git', 'diff', '--binary', 'HEAD'], capture_output=True, check=True).stdout
    sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path('src').rglob('*.py'))}
    report = {'revision': head, 'tracked_diff_sha256': hashlib.sha256(diff).hexdigest(),
              'source_files_sha256': sources,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'lock_sha256': hashlib.sha256(Path('uv.lock').read_bytes()).hexdigest(),
              'platform': platform.platform(), 'python': sys.version, 'provider': args.execution,
              'workload': 'fixed input.txt copied to output.txt; fresh temporary workspace and user home',
              'permission_comparison': 'none; Host and Sandbox have different authority', 'samples': records}
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text+'\n')
    else:
        print(text)
    return 0 if all(r['success'] for r in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())

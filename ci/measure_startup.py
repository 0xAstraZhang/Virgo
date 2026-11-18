"""Offline execution measurement; each sample uses a fresh interpreter and workspace.

All samples and failures are retained. These faux-provider measurements say
nothing about model quality, instruction compliance or paired model performance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKER_TIMEOUT_SECONDS = 180


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


def session_settings(contract, mode):
    if contract == "current":
        return {"execution": {"mode": mode}}
    if contract == "legacy-host":
        if mode != "no-limit":
            raise ValueError("The Host baseline measures No-Limit")
        return {"execution": {"provider": "host"}}
    if mode == "read-only":
        settings = {"sandbox": {"mode": "read-only"}}
    elif mode == "workspace-edit":
        settings = {"permissions": {"preset": "workspace-edit"}}
    else:
        raise ValueError("The native baseline measures the two sandbox modes")
    # Retired tool-review rules must not prompt during a native timing sample.
    settings.setdefault("permissions", {})["allow"] = ["*"]
    return settings


class MeasurementFailure(RuntimeError):
    def __init__(self, message, *, before_launch=False):
        super().__init__(message)
        self.before_launch = before_launch


def issue_operation(session, name, arguments, *, expected=None):
    from virgo.ai.providers import faux
    from virgo.ai.types import ToolResultMessage

    offset = len(session.main.messages)
    faux.script("measure", faux.reply(tools=[(name, arguments)]), faux.reply("done"))
    session.prompt("Run the supplied local operation")
    results = [m for m in session.main.messages[offset:] if isinstance(m, ToolResultMessage)]
    if len(results) != 1 or results[0].tool_name != name:
        raise MeasurementFailure(f"Expected one {name} result, got {len(results)}")
    result = results[0]
    details = result.details or {}
    boundary = details.get("boundary") or {}
    if result.is_error:
        prelaunch = (boundary.get("payload_started") is False
                     or details.get("status") == "sandbox_unavailable"
                     or (details.get("execution") or {}).get("started") is False)
        raise MeasurementFailure(result.text(), before_launch=prelaunch)
    if expected is not None and (details.get("exit_code") != 0 or expected not in result.text().splitlines()):
        raise MeasurementFailure(f"Command did not return the expected output: {result.text()}")
    return boundary


def sample(args):
    from_environment = os.environ.get("VIRGO_BENCHMARK_START_NS")
    interpreter_ms = (time.perf_counter_ns() - int(from_environment)) / 1e6 if from_environment else 0.0
    result = {"mode": args.mode, "contract": args.contract, "success": False, "operations": []}
    with tempfile.TemporaryDirectory(prefix="virgo-measure-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        source = workspace / "answer.py"
        source.write_text("def answer():\n    return 41\n", encoding="utf-8")
        (workspace / "check_answer.py").write_text(
            "from answer import answer\nassert answer() == 42\nprint('tested-edit')\n", encoding="utf-8")
        start = time.perf_counter()
        session = None
        try:
            from virgo.harness.session import AgentSession
            from virgo.harness.ui import UI

            class OfflineUI(UI):
                def __init__(self):
                    self.questions = []

                def confirm(self, question, agent="main"):
                    self.questions.append(question)
                    return "n"

                def select(self, question, options, *, timeout=None):
                    self.questions.append(question)
                    return None

            imported = time.perf_counter()
            ui = OfflineUI()
            session = AgentSession(workspace, home=root / "home", session=None, model="faux/measure",
                                   ui=ui, settings=session_settings(args.contract, args.mode),
                                   extensions=False, refresh_models=False)
            ready = time.perf_counter()
            result.update(import_ms=(imported - start) * 1000, construct_ms=(ready - imported) * 1000,
                          cold_start_ms=interpreter_ms + (ready - start) * 1000)
            mode_owner = session.services["sandbox" if args.contract == "legacy-native" else "execution"]
            if args.contract == "legacy-host":
                if session.main.execution_context.provider != "host":
                    raise ValueError("Expected the historical Host execution provider")
            elif mode_owner.selected_mode_id != ("legacy-read-only" if args.contract == "legacy-native"
                                                 and args.mode == "read-only" else args.mode):
                raise ValueError(f"Wrong mode selected: {mode_owner.selected_mode_id}")
            shell = session.services["shell"].current
            short = "echo measured-short-command"
            for index in range(args.repeat + 1):
                begin = time.perf_counter()
                operation = {"index": index, "phase": "first" if index == 0 else "repeat",
                             "success": False, "latency_ms": None}
                result["operations"].append(operation)
                try:
                    boundary = issue_operation(session, shell.tool_name, {"command": short},
                                               expected="measured-short-command")
                except MeasurementFailure as error:
                    operation.update(error=str(error), failure_elapsed_ms=(time.perf_counter() - begin) * 1000)
                    if error.before_launch:
                        continue
                    result["cleanup"] = "unknown"
                    break
                operation.update(success=True, latency_ms=(time.perf_counter() - begin) * 1000,
                                 boundary=boundary)
            if all(op["success"] for op in result["operations"]):
                begin = time.perf_counter()
                if args.mode == "read-only":
                    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
                    result["edit_actor"] = "external_editor"
                else:
                    issue_operation(session, "edit", {"path": "answer.py", "edits": [
                        {"old_text": "return 41", "new_text": "return 42"}]})
                    result["edit_actor"] = "virgo_file_tool"
                if source.read_text(encoding="utf-8") != "def answer():\n    return 42\n":
                    raise MeasurementFailure("The original workspace did not contain the edit")
                argv = [sys.executable, "-B", "check_answer.py"]
                command = ("& " + " ".join("'" + word.replace("'", "''") + "'" for word in argv)
                           if shell.tool_name == "powershell" else shlex.join(argv))
                result["edit_test_boundary"] = issue_operation(session, shell.tool_name, {"command": command},
                                                               expected="tested-edit")
                result["edit_to_test_ms"] = (time.perf_counter() - begin) * 1000
                result["short_sequence_ms"] = sum(op["latency_ms"] for op in result["operations"][1:])
                result["success"] = True
            result["approval_questions"] = ui.questions
            if ui.questions:
                result.update(success=False, error="The benchmark required approval")
        except Exception as error:
            result.update(error=f"{type(error).__name__}: {error}", failure_elapsed_ms=(time.perf_counter() - start) * 1000)
            if isinstance(error, MeasurementFailure) and not error.before_launch:
                result["cleanup"] = "unknown"
        finally:
            if session:
                try:
                    session.shutdown()
                except Exception as error:
                    result.update(success=False, cleanup="unknown", error=f"Shutdown: {error}")

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
        result['installed_runtime_and_dependencies'] = footprint(roots)
    return result


def checkpoint(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(report, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_worker_record(stdout, expected_operations):
    record = json.loads(stdout)
    if not isinstance(record, dict) or type(record.get('success')) is not bool:
        raise ValueError('Worker result requires a boolean success field')
    operations = record.get('operations')
    if not isinstance(operations, list) or any(
        not isinstance(op, dict) or type(op.get('success')) is not bool for op in operations
    ):
        raise ValueError('Worker result requires operation records')
    for operation in operations:
        latency = operation.get('latency_ms')
        if operation['success']:
            if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
                raise ValueError('Successful operation requires a measured latency')
        elif latency is not None:
            raise ValueError('Failed operation cannot report a successful latency')
    error = record.get('error')
    unknown_cleanup = record.get('cleanup') == 'unknown'
    if not operations and not error:
        raise ValueError('Worker returned no operations or error')
    if len(operations) > expected_operations or (len(operations) != expected_operations and not (error or unknown_cleanup)):
        raise ValueError('Worker operation count disagrees with the workload')
    if record['success'] != (not error and not unknown_cleanup and all(op['success'] for op in operations)):
        raise ValueError('Worker success disagrees with its operations or failure')
    return record


def run_worker(command, expected_operations):
    started = time.perf_counter()
    failure = {'success': False, 'status': 'worker_error', 'latency_ms': None}
    # A child can inherit standard streams. Files keep a dead worker's descendants
    # from blocking the parent on a pipe EOF during timeout handling.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            env = {**os.environ, 'VIRGO_BENCHMARK_START_NS': str(time.perf_counter_ns())}
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env)
        except OSError as error:
            return {**failure, 'error': str(error), 'cleanup': 'not_started',
                    'failure_elapsed_ms': (time.perf_counter() - started) * 1000}
        try:
            proc.wait(timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            failure.update(status='timeout', error=f'Worker exceeded {WORKER_TIMEOUT_SECONDS} s',
                           timeout_seconds=WORKER_TIMEOUT_SECONDS,
                           timeout_elapsed_ms=(time.perf_counter() - started) * 1000)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                failure['error'] += f'; worker termination unconfirmed: {error}'
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read().decode('utf-8', errors='replace')
        errors = stderr.read().decode('utf-8', errors='replace')
        if failure['status'] != 'timeout':
            if proc.returncode:
                failure['error'] = f'Worker exited with status {proc.returncode}'
            else:
                try:
                    return read_worker_record(output, expected_operations)
                except ValueError as error:
                    failure['error'] = str(error)
        # The measured shell can occupy a different process group. A missing
        # completion record leaves descendant cleanup unknown, even after wait().
        return {**failure, 'exit_code': proc.returncode, 'stdout': output, 'stderr': errors,
                'cleanup': 'unknown', 'failure_elapsed_ms': (time.perf_counter() - started) * 1000}


def stop_worker(_signum, _frame):
    # Let the measured operation and AgentSession execute their finally blocks.
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['no-limit', 'read-only', 'workspace-edit'], default='no-limit')
    parser.add_argument('--contract', choices=['current', 'legacy-host', 'legacy-native'], default='current')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.samples < 1 or args.repeat < 1:
        parser.error('samples and repeat must be positive')
    if args.worker:
        if os.name == 'posix':
            signal.signal(signal.SIGTERM, stop_worker)
        print(json.dumps(sample(args)))
        return
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--mode', args.mode, '--contract', args.contract,
               '--repeat', str(args.repeat)]
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    diff = subprocess.run(['git', 'diff', '--binary', 'HEAD'], capture_output=True, check=True).stdout
    sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path('src').rglob('*.py'))}
    report = {'revision': head, 'tracked_diff_sha256': hashlib.sha256(diff).hexdigest(),
              'source_files_sha256': sources,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'lock_sha256': hashlib.sha256(Path('uv.lock').read_bytes()).hexdigest(),
              'platform': platform.platform(), 'python': sys.version, 'mode': args.mode, 'contract': args.contract,
              'workload': 'cold session, repeated shell echo, edit answer.py and run check_answer.py',
              'repeats': args.repeat, 'planned_samples': args.samples,
              'status': 'running', 'samples': []}
    if args.output:
        checkpoint(args.output, report)
    for index in range(args.samples):
        record = run_worker(command, args.repeat + 1)
        report['samples'].append({**record, 'sample': index})
        if record.get('cleanup') == 'unknown':
            report['status'] = 'stopped'
            report['samples'].extend({'sample': remaining, 'status': 'not_run', 'success': None,
                                      'reason': 'Previous worker cleanup unconfirmed'}
                                     for remaining in range(index + 1, args.samples))
        elif index == args.samples - 1:
            report['status'] = 'finished'
        if args.output:
            checkpoint(args.output, report)
        if report['status'] == 'stopped':
            break
    if not args.output:
        print(json.dumps(report, indent=2))
    return 0 if all(r['success'] for r in report['samples']) else 1


if __name__ == '__main__':
    raise SystemExit(main())

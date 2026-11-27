"""Measure a local edit and test workload in fresh Virgo sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shlex
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKER_TIMEOUT_SECONDS = 180
DEFAULT_MAX_COLD_START_MS = 5000.0
DEFAULT_MAX_READ_MS = 250.0
DEFAULT_MAX_REPEAT_CMD_MS = 2000.0


def populate_realistic_workspace(workspace: Path, file_count: int) -> list[str]:
    """Create a realistic nested repository layout without touching answer.py."""
    if file_count <= 0:
        return []
    created: list[str] = []
    repo = workspace / "repo"
    packages = max(1, min(25, file_count // 20 or 1))
    for pkg in range(packages):
        (repo / f"pkg_{pkg:02d}").mkdir(parents=True, exist_ok=True)
    for index in range(file_count):
        pkg = index % packages
        rel = f"repo/pkg_{pkg:02d}/mod_{index:04d}.py"
        (workspace / rel).write_text(
            f'"""Module {index}."""\n\nVALUE = {index}\n\ndef compute(x: int) -> int:\n    return x + VALUE\n',
            encoding="utf-8",
        )
        created.append(rel)
    return created


def session_settings(mode):
    return {"execution": {"mode": mode}}


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
    workspace_files = getattr(args, "workspace_files", 0) or 0
    read_repeats = getattr(args, "read_repeats", 0) or 0
    result = {
        "mode": args.mode, "workspace_files": workspace_files,
        "read_repeats": read_repeats, "success": False, "operations": [],
    }
    with tempfile.TemporaryDirectory(prefix="virgo-measure-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        source = workspace / "answer.py"
        source.write_text("def answer():\n    return 41\n", encoding="utf-8")
        (workspace / "check_answer.py").write_text(
            "from answer import answer\nassert answer() == 42\nprint('tested-edit')\n", encoding="utf-8")
        extra_files = populate_realistic_workspace(workspace, workspace_files)
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
            package = Path(sys.modules["virgo"].__file__).resolve().parent
            if package != (Path.cwd() / "src" / "virgo").resolve():
                raise ValueError(f"Measured interpreter imported Virgo from {package}, outside its checkout")
            ui = OfflineUI()
            session = AgentSession(workspace, home=root / "home", session=None, model="faux/measure",
                                   ui=ui, settings=session_settings(args.mode),
                                   extensions=False, refresh_models=False)
            ready = time.perf_counter()
            result.update(import_ms=(imported - start) * 1000, construct_ms=(ready - imported) * 1000,
                          cold_start_ms=interpreter_ms + (ready - start) * 1000)
            owner = session.services["execution"]
            if owner.selected_mode_id != args.mode:
                raise ValueError(f"Wrong mode selected: {owner.selected_mode_id}")
            observed_network = owner.mode_for(session.main).flags()["network_allowed"]
            if observed_network is not (args.mode != "read-only"):
                raise ValueError(f"Wrong Shell network policy: {observed_network}")
            result["shell_network_allowed"] = observed_network
            if read_repeats > 0:
                read_targets = extra_files if extra_files else ["answer.py"]
                read_latencies: list[float] = []
                for idx in range(read_repeats):
                    target_rel = read_targets[idx % len(read_targets)]
                    t0 = time.perf_counter()
                    issue_operation(session, "read", {"path": target_rel})
                    read_latencies.append((time.perf_counter() - t0) * 1000)
                result["read_latencies_ms"] = read_latencies
                result["read_median_ms"] = statistics.median(read_latencies)
                result["read_total_ms"] = sum(read_latencies)
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
                result["first_command_ms"] = result["operations"][0]["latency_ms"]
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


def _repeat_command_latencies(samples: list[dict]) -> list[float]:
    return [
        float(op["latency_ms"])
        for sample_rec in samples
        for op in (sample_rec.get("operations") or ())
        if op.get("phase") == "repeat" and isinstance(op.get("latency_ms"), (int, float))
    ]


def evaluate_scale_budgets(
    small_samples: list[dict],
    large_samples: list[dict],
    *,
    max_cold_start_ms: float = DEFAULT_MAX_COLD_START_MS,
    max_read_ms: float = DEFAULT_MAX_READ_MS,
    max_repeat_cmd_ms: float = DEFAULT_MAX_REPEAT_CMD_MS,
) -> dict:
    """Enforce performance budgets on Tier 1 platforms (cold start, first/repeat Shell, O(1) Read)."""
    violations: list[str] = []
    if not small_samples or not all(s.get("success") for s in small_samples):
        violations.append("Small-workspace samples did not all succeed")
    if not large_samples or not all(s.get("success") for s in large_samples):
        violations.append("Realistic-workspace samples did not all succeed")
    if violations:
        return {"passed": False, "violations": violations}

    small_cold = statistics.median(s["cold_start_ms"] for s in small_samples)
    large_cold = statistics.median(s["cold_start_ms"] for s in large_samples)
    small_read = statistics.median(s["read_median_ms"] for s in small_samples)
    large_read = statistics.median(s["read_median_ms"] for s in large_samples)

    if small_cold > max_cold_start_ms:
        violations.append(f"Small workspace cold_start_ms {small_cold:.1f}ms exceeds {max_cold_start_ms:.1f}ms")
    if large_cold > max_cold_start_ms:
        violations.append(f"Realistic workspace cold_start_ms {large_cold:.1f}ms exceeds {max_cold_start_ms:.1f}ms")
    if large_read > max_read_ms:
        violations.append(f"Realistic workspace read_median_ms {large_read:.1f}ms exceeds {max_read_ms:.1f}ms")
    scaling_ceiling = max(small_read * 3.0, small_read + 50.0)
    if large_read > scaling_ceiling:
        violations.append(
            f"Per-Read latency scaled with workspace size: small={small_read:.1f}ms, "
            f"realistic={large_read:.1f}ms (ceiling {scaling_ceiling:.1f}ms)"
        )

    extra: dict[str, float] = {}
    small_first_vals = [float(s["first_command_ms"]) for s in small_samples if isinstance(s.get("first_command_ms"), (int, float))]
    large_first_vals = [float(s["first_command_ms"]) for s in large_samples if isinstance(s.get("first_command_ms"), (int, float))]
    if small_first_vals and large_first_vals:
        small_first = statistics.median(small_first_vals)
        large_first = statistics.median(large_first_vals)
        extra["small_first_command_median_ms"] = round(small_first, 2)
        extra["realistic_first_command_median_ms"] = round(large_first, 2)
        if large_first > max_cold_start_ms:
            violations.append(
                f"Realistic workspace first_command_ms {large_first:.1f}ms exceeds {max_cold_start_ms:.1f}ms"
            )

    small_repeat_vals = _repeat_command_latencies(small_samples)
    large_repeat_vals = _repeat_command_latencies(large_samples)
    if small_repeat_vals and large_repeat_vals:
        small_repeat = statistics.median(small_repeat_vals)
        large_repeat = statistics.median(large_repeat_vals)
        extra["small_repeat_command_median_ms"] = round(small_repeat, 2)
        extra["realistic_repeat_command_median_ms"] = round(large_repeat, 2)
        extra["max_repeat_cmd_ms"] = max_repeat_cmd_ms
        if large_repeat > max_repeat_cmd_ms:
            violations.append(
                f"Realistic workspace repeat command latency {large_repeat:.1f}ms exceeds {max_repeat_cmd_ms:.1f}ms"
            )
        repeat_ceiling = max(small_repeat * 3.0, small_repeat + 300.0)
        if large_repeat > repeat_ceiling:
            violations.append(
                f"Repeat command latency scaled with workspace size: small={small_repeat:.1f}ms, "
                f"realistic={large_repeat:.1f}ms (ceiling {repeat_ceiling:.1f}ms)"
            )

    return {
        "passed": not violations,
        "violations": violations,
        "small_cold_start_median_ms": round(small_cold, 2),
        "realistic_cold_start_median_ms": round(large_cold, 2),
        "small_read_median_ms": round(small_read, 2),
        "realistic_read_median_ms": round(large_read, 2),
        "max_cold_start_ms": max_cold_start_ms,
        "max_read_ms": max_read_ms,
        **extra,
    }


def _collect_budget_samples(command, count, expected_operations):
    samples = []
    stopped = False
    for idx in range(count):
        record = run_worker(command, expected_operations)
        samples.append({**record, 'sample': idx})
        if record.get('cleanup') == 'unknown':
            stopped = True
            samples.extend(
                {'sample': remaining, 'status': 'not_run', 'success': None,
                 'reason': 'Previous worker cleanup unconfirmed'}
                for remaining in range(idx + 1, count)
            )
            break
    return samples, stopped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['no-limit', 'read-only', 'workspace-edit'], default='no-limit')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--workspace-files', type=int, default=0)
    parser.add_argument('--read-repeats', type=int, default=0)
    parser.add_argument('--check-budgets', action='store_true')
    parser.add_argument('--realistic-files', type=int, default=1000)
    parser.add_argument('--max-cold-start-ms', type=float, default=DEFAULT_MAX_COLD_START_MS)
    parser.add_argument('--max-read-ms', type=float, default=DEFAULT_MAX_READ_MS)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.samples < 1 or args.repeat < 1 or args.workspace_files < 0 or args.read_repeats < 0:
        parser.error('samples and repeat must be positive; file/read counts must be non-negative')
    if args.worker:
        if os.name == 'posix':
            signal.signal(signal.SIGTERM, stop_worker)
        print(json.dumps(sample(args)))
        return
    if args.check_budgets:
        read_repeats = max(args.read_repeats, 10)
        base_cmd = [
            sys.executable, str(Path(__file__).resolve()), '--worker',
            '--mode', args.mode,
            '--repeat', str(args.repeat), '--read-repeats', str(read_repeats),
        ]
        small_samples, stopped = _collect_budget_samples(
            [*base_cmd, '--workspace-files', '0'], args.samples, args.repeat + 1
        )
        if stopped:
            large_samples = [
                {'sample': idx, 'status': 'not_run', 'success': None,
                 'reason': 'Previous worker cleanup unconfirmed'}
                for idx in range(args.samples)
            ]
        else:
            large_samples, stopped = _collect_budget_samples(
                [*base_cmd, '--workspace-files', str(args.realistic_files)], args.samples, args.repeat + 1
            )
        budget = evaluate_scale_budgets(
            small_samples, large_samples,
            max_cold_start_ms=args.max_cold_start_ms,
            max_read_ms=args.max_read_ms,
        )
        summary = {
            'platform': platform.platform(), 'python': sys.version, 'mode': args.mode,
            'realistic_files': args.realistic_files, 'read_repeats': read_repeats,
            'status': 'stopped' if stopped else 'finished',
            'budget': budget, 'small_samples': small_samples, 'realistic_samples': large_samples,
        }
        if args.output:
            checkpoint(args.output, summary)
        print(json.dumps(summary, indent=2))
        return 0 if (not stopped and budget['passed']) else 1
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--mode', args.mode,
               '--repeat', str(args.repeat), '--workspace-files', str(args.workspace_files),
               '--read-repeats', str(args.read_repeats)]
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    diff = subprocess.run(['git', 'diff', '--binary', 'HEAD'], capture_output=True, check=True).stdout
    report = {'revision': head, 'tracked_diff_sha256': hashlib.sha256(diff).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'lock_sha256': hashlib.sha256(Path('uv.lock').read_bytes()).hexdigest(),
              'platform': platform.platform(), 'python': sys.version, 'mode': args.mode,
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

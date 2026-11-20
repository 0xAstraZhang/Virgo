"""Run discovered test files across balanced parallel workers, with optional serial files."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKIP_DIRS = {
    '.git', '.hg', '.svn', '.venv', 'venv', '.tox', '.nox', '.mypy_cache',
    '.pytest_cache', '.ruff_cache', '__pycache__', 'build', 'dist', 'node_modules',
}
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_WORKERS_WIN32 = 3


def _default_workers() -> int:
    raw = os.environ.get('VIRGO_CI_WORKERS', '').strip()
    if raw:
        value = int(raw)
        if value < 1:
            raise ValueError('VIRGO_CI_WORKERS must be >= 1')
        return value
    cap = DEFAULT_MAX_WORKERS_WIN32 if sys.platform == 'win32' else DEFAULT_MAX_WORKERS
    return max(1, min(os.cpu_count() or 2, cap))


def discover_test_files(ignored: set[str]) -> list[str]:
    roots = [Path('.')]
    pyproject = Path('pyproject.toml')
    if pyproject.is_file():
        try:
            config = tomllib.loads(pyproject.read_text(encoding='utf-8'))
            testpaths = config.get('tool', {}).get('pytest', {}).get('ini_options', {}).get('testpaths')
            if isinstance(testpaths, list) and testpaths:
                existing = [Path(p) for p in testpaths if Path(p).exists()]
                if existing:
                    roots = existing
        except (OSError, tomllib.TOMLDecodeError):
            pass
    found: set[str] = set()
    for root in roots:
        if root.is_file():
            rel = root.as_posix()
            if rel not in ignored:
                found.add(rel)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith('.')
                and (Path(dirpath) / d).as_posix() not in ignored
            )
            for name in sorted(filenames):
                if not name.endswith('.py'):
                    continue
                if not (name.startswith('test_') or name.endswith('_test.py')):
                    continue
                rel = (Path(dirpath) / name).as_posix()
                if rel not in ignored:
                    found.add(rel)
    return sorted(found)


def load_schedule() -> tuple[set[str], dict[str, float]]:
    manifest = json.loads(Path(__file__).with_name('parallel-files.json').read_text(encoding='utf-8'))
    serial_list = manifest.get('serial', [])
    if len(serial_list) != len(set(serial_list)):
        raise ValueError('Duplicate files in serial manifest')
    weights_by_platform = manifest.get('weights', {})
    weights: dict[str, float] = {k: float(v) for k, v in weights_by_platform.get('default', {}).items()}
    weights.update({k: float(v) for k, v in weights_by_platform.get(sys.platform, {}).items()})
    return set(serial_list), weights


def partition_lpt(files: list[str], weights: dict[str, float], worker_count: int) -> list[list[str]]:
    count = max(1, min(worker_count, len(files)))
    buckets: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    ordered = sorted(files, key=lambda path: (-weights.get(path, 1.0), path))
    for path in ordered:
        index = min(range(count), key=lambda i: (loads[i], len(buckets[i]), i))
        buckets[index].append(path)
        loads[index] += weights.get(path, 1.0)
    return [bucket for bucket in buckets if bucket]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execution', choices=['serial', 'parallel'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ignore', action='append', default=[])
    parser.add_argument('--workers', type=int)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / 'junit.xml'
    target.unlink(missing_ok=True)
    ignored = {Path(p).as_posix() for p in args.ignore}
    ignore_flags = [f'--ignore={p}' for p in args.ignore]
    workers = args.workers if args.workers is not None else _default_workers()
    if workers < 1:
        raise ValueError('--workers must be >= 1')
    print_lock = threading.Lock()

    def run(label, selection):
        xml = args.output / f'{label}.xml'
        xml.unlink(missing_ok=True)
        command = [
            sys.executable, '-u', str(Path(__file__).with_name('run_pytest.py')),
            '-v', '-ra', '--tb=long', '--color=no', '--durations=30',
            '-o', 'junit_family=legacy', f'--junitxml={xml}', *ignore_flags, *selection,
        ]
        with (args.output / f'{label}.log').open('w', encoding='utf-8') as log:
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
            ) as process:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    with print_lock:
                        print(f'[{label}] {line}', end='', flush=True)
                code = process.wait()
        return code, xml

    discovered = discover_test_files(ignored) if args.execution == 'parallel' else []
    if args.execution == 'parallel' and len(discovered) > 1 and workers > 1:
        serial_set, weights = load_schedule()
        parallel_files = [p for p in discovered if p not in serial_set]
        serial_files = [p for p in discovered if p in serial_set]
        results = []
        if parallel_files:
            buckets = partition_lpt(parallel_files, weights, workers)
            if len(buckets) > 1:
                with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
                    futures = [pool.submit(run, f'parallel-{i}', group) for i, group in enumerate(buckets)]
                    results.extend(f.result() for f in futures)
            else:
                results.append(run('parallel-0', buckets[0]))
        if serial_files:
            results.append(run('serial', serial_files))
    else:
        results = [run('serial', [])]

    root = ET.Element('testsuites')
    codes = []
    for code, xml in results:
        codes.append(code)
        if not xml.exists():
            print(f'Missing JUnit report: {xml}', flush=True)
            codes.append(2)
            continue
        root.extend(ET.parse(xml).getroot())
    ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)
    # Empty shard is allowed only when other groups actually ran tests.
    if any(code not in (0, 1, 5) for code in codes):
        return 2
    if 1 in codes:
        return 1
    return 0 if list(root.iter('testcase')) else 5


if __name__ == '__main__':
    sys.exit(main())

"""Run reviewed files in two processes; isolate slow Windows modules by runner."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WINDOWS_NATIVE = 'tests/harness/test_windows_sandbox_e2e.py'
WINDOWS_PERMISSIONS = (
    'tests/harness/test_auth_commands.py',
    'tests/harness/test_permission_modes_e2e.py',
    'tests/harness/test_sandbox_e2e.py',
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execution', choices=['serial', 'parallel'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--partition', choices=('all', 'windows-core', 'windows-permissions', 'windows-native'),
                        default='all')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / 'junit.xml'
    target.unlink(missing_ok=True)
    manifest = json.loads(Path(__file__).with_name('parallel-files.json').read_text())
    groups = [[p for p in group if Path(p).is_file()] for group in manifest]
    if args.partition != 'all':
        if not Path(WINDOWS_NATIVE).is_file():
            print(f'Missing required native test module: {WINDOWS_NATIVE}', flush=True)
            return 2
        if args.partition != 'windows-native':
            for path in WINDOWS_PERMISSIONS:
                if not Path(path).is_file():
                    print(f'Missing required permissions test module: {path}', flush=True)
                    return 2
        groups = [[p for p in group if p not in (WINDOWS_NATIVE, *WINDOWS_PERMISSIONS)] for group in groups]
    selected = [p for group in groups for p in group]
    if len(selected) != len(set(selected)):
        raise ValueError('Duplicate files in parallel manifest')

    def run(label, selection):
        xml = args.output / f'{label}.xml'
        xml.unlink(missing_ok=True)
        command = [sys.executable, '-u', str(Path(__file__).with_name('run_pytest.py')),
                   '-v', '-ra', '--tb=long', '--color=no', '--durations=30',
                   '-o', 'junit_family=legacy', f'--junitxml={xml}', *selection]
        with (args.output / f'{label}.log').open('w', encoding='utf-8') as log:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding='utf-8', errors='replace', bufsize=1) as process:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(f'[{label}] {line}', end='', flush=True)
                code = process.wait()
        return code, xml

    ignored = [f'--ignore={path}' for path in (WINDOWS_NATIVE, *WINDOWS_PERMISSIONS)] if args.partition == 'windows-core' else []
    if args.partition == 'windows-native':
        # This partition runs on its own CI host. Never parallelize ACL tests
        # with other pytest processes on the same Windows machine.
        results = [run('native', [WINDOWS_NATIVE])]
    elif args.partition == 'windows-permissions':
        # Keep the original module order and all tests in one serial process.
        results = [run('permissions', list(WINDOWS_PERMISSIONS))]
    elif args.execution == 'parallel' and selected:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, f'parallel-{i}', group) for i, group in enumerate(groups) if group]
            results = [f.result() for f in futures]
        results.append(run('serial', [*[f'--ignore={p}' for p in selected], *ignored]))
    else:
        results = [run('serial', ignored)]
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
    # Empty serial remainder is allowed only when other groups actually ran tests.
    if any(code not in (0, 1, 5) for code in codes):
        return 2
    if 1 in codes:
        return 1
    return 0 if list(root.iter('testcase')) else 5


if __name__ == '__main__':
    sys.exit(main())

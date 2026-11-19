"""CLI acceptance: partition loss/duplicates, swallowed failures, collection errors, diagnostics."""
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "compare_tests.py"


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        native_file = 'tests/harness/test_windows_sandbox_e2e.py'
        permission_files = ('tests/harness/test_auth_commands.py',
                            'tests/harness/test_permission_modes_e2e.py',
                            'tests/harness/test_sandbox_e2e.py')
        for name in ('tests/ai/test_auth.py', 'tests/ai/test_models.py', 'test_serial.py',
                     native_file, *permission_files):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def test_' + path.stem + '(): assert True\n')
        def run(mode, expected, partition='all'):
            output = root / f'{mode}-{partition}'
            result = subprocess.run([sys.executable, str(RUNNER), 'run', '--project', str(root),
                '--output', str(output), '--platform', 'windows-latest', '--label', 'head', '--sha', 'test',
                '--execution', mode, '--partition', partition], capture_output=True, text=True)
            assert result.returncode == expected, result.stdout + result.stderr
            return json.loads((output / 'results.json').read_text())
        before = run('serial', 0)
        after = run('parallel', 0)
        assert before['tests'] == after['tests'] and len(after['tests']) == 7
        core = run('parallel', 0, 'windows-core')
        permissions = run('parallel', 0, 'windows-permissions')
        native = run('parallel', 0, 'windows-native')
        parts = {'core': core, 'permissions': permissions, 'native': native}
        assert sum(len(part['tests']) for part in parts.values()) == len(before['tests'])
        assert {name: case for part in parts.values() for name, case in part['tests'].items()} == before['tests']
        permission_cases = ET.parse(root / 'parallel-windows-permissions/junit.xml').iter('testcase')
        assert [case.get('file').replace('\\', '/') for case in permission_cases] == list(permission_files)

        target = root / 'target.json'
        def compare(parts, expected, mode='head'):
            target.write_text(json.dumps({'head_sha': 'test', 'base_sha': 'base', 'mode': mode,
                                          'url': 'https://example.test/ci'}))
            directory = root / 'parts'
            for path in directory.rglob('results.json'):
                path.unlink()
            for partition, report in parts.items():
                path = directory / partition / 'results.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report))
            result = subprocess.run([sys.executable, str(RUNNER), 'compare', '--reports', str(directory),
                '--output', str(root / 'comparison'), '--target', str(target),
                '--platforms', 'windows-latest', '--windows-shards'], capture_output=True, text=True)
            assert result.returncode == expected, result.stdout + result.stderr
            return json.loads((root / 'comparison.json').read_text())

        missing = compare({'core': core, 'native': native}, 1)
        assert 'windows-permissions' in str(missing['platforms']['windows-latest']['problems'])
        complete = compare(parts, 0)
        assert complete['platforms']['windows-latest']['counts']['head'] == {'passed': 7}
        joined = root / 'comparison-windows-latest-head.json'
        aggregate = json.loads(joined.read_text())
        assert aggregate['tests'] == before['tests']
        assert all(part['complete'] for part in aggregate['partitions'].values())
        assert aggregate['duration'] == round(sum(part['duration'] for part in parts.values()), 2)
        for omitted in parts:
            missing = compare({name: part for name, part in parts.items() if name != omitted}, 1)
            assert f'windows-{omitted}' in str(missing['platforms']['windows-latest']['problems'])
            assert not joined.exists(), 'An incomplete rerun must not retain an old complete aggregate.'
        compare({**parts, 'duplicate': permissions}, 1)
        for left, right in (('core', 'native'), ('core', 'permissions'), ('permissions', 'native')):
            name = next(iter(parts[left]['tests']))
            overlap = {**parts[right], 'tests': {**parts[right]['tests'], name: parts[left]['tests'][name]},
                       'counts': {'passed': len(parts[right]['tests']) + 1}}
            compare({**parts, right: overlap}, 1)
        for name in ('permissions', 'native'):
            compare({**parts, name: {**parts[name], 'complete': False, 'exit_code': 2}}, 1)
            for identity in ('sha', 'python', 'system', 'label'):
                compare({**parts, name: {**parts[name], identity: 'other'}}, 1)
        skipped = {**native, 'counts': {'skipped': len(native['tests'])},
                   'tests': {key: {**value, 'status': 'skipped'} for key, value in native['tests'].items()}}
        compare({**parts, 'native': skipped}, 1)
        permission_name = next(iter(permissions['tests']))
        platform_skip = {**permissions, 'counts': {'passed': len(permissions['tests']) - 1, 'skipped': 1},
                         'tests': {**permissions['tests'], permission_name:
                                   {**permissions['tests'][permission_name], 'status': 'skipped'}}}
        baseline = {'base-' + name: {**part, 'label': 'base', 'sha': 'base'} for name, part in parts.items()}
        compare({**parts, **baseline, 'permissions': platform_skip}, 1, mode='comparison')
        compare({**parts, **baseline, 'permissions': platform_skip,
                 'base-permissions': {**platform_skip, 'label': 'base', 'sha': 'base'}}, 0, mode='comparison')
        (root / 'tests/ai/test_auth.py').write_text('def test_failure():\n    assert False, "diagnostic sentinel"\n')
        assert run('parallel', 1)['counts']['failed'] == 1
        details = (root / 'parallel-all/failures.md').read_text()
        assert 'diagnostic sentinel' in details and 'test_auth.py' in details and 'test_failure' in details
        native_name = next(iter(native['tests']))
        (root / native_file).write_text(f'def {native_name}():\n    assert False, "native diagnostic"\n')
        failed_native = run('parallel', 1, 'windows-native')
        failed = compare({**parts, 'native': failed_native}, 1)
        assert failed['platforms']['windows-latest']['failures'] == [native_name]
        assert 'native diagnostic' in (root / 'parallel-windows-native/failures.md').read_text()
        fixed = compare({**parts, **baseline,
                         'base-native': {**failed_native, 'label': 'base', 'sha': 'base'}}, 0, mode='comparison')
        assert fixed['platforms']['windows-latest']['fixed_failures'] == [native_name]
        (root / 'test_serial.py').write_text('raise RuntimeError("collection sentinel")\n')
        assert not run('parallel', 2)['complete']
    print('Parallel CLI acceptance passed')


if __name__ == '__main__':
    main()

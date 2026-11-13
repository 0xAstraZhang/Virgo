"""CLI acceptance: partition loss/duplicates, swallowed failures, collection errors, diagnostics."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).with_name('compare_tests.py').resolve()


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name in ('tests/ai/test_auth.py', 'tests/ai/test_models.py', 'test_serial.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def test_' + path.stem + '(): assert True\n')
        def run(mode, expected):
            output = root / mode
            result = subprocess.run([sys.executable, str(RUNNER), 'run', '--project', str(root),
                '--output', str(output), '--platform', 'local', '--label', 'head', '--sha', 'test',
                '--execution', mode], capture_output=True, text=True)
            assert result.returncode == expected, result.stdout + result.stderr
            return json.loads((output / 'results.json').read_text())
        before = run('serial', 0)
        after = run('parallel', 0)
        assert before['tests'] == after['tests'] and len(after['tests']) == 3
        (root / 'tests/ai/test_auth.py').write_text('def test_failure():\n    assert False, "diagnostic sentinel"\n')
        assert run('parallel', 1)['counts']['failed'] == 1
        details = (root / 'parallel/failures.md').read_text()
        assert 'diagnostic sentinel' in details and 'test_auth.py' in details and 'test_failure' in details
        (root / 'test_serial.py').write_text('raise RuntimeError("collection sentinel")\n')
        assert not run('parallel', 2)['complete']
    print('Parallel CLI acceptance passed')


if __name__ == '__main__':
    main()

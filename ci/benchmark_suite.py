"""Compare complete serial and parallel suites on the same runner and commit."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    reports = {}
    for mode in ('serial', 'parallel'):
        output = Path('benchmark') / mode
        subprocess.run([sys.executable, 'ci/compare_tests.py', 'run', '--project', '.',
                        '--output', str(output), '--execution', mode, '--platform', sys.platform,
                        '--label', 'head', '--sha', os.environ.get('GITHUB_SHA', 'local')], check=True)
        reports[mode] = json.loads((output / 'results.json').read_text())
    assert reports['serial']['tests'] == reports['parallel']['tests'], 'Test identity/status mismatch'
    assert not any(r['problems'] for r in reports.values()), reports
    seconds = {mode: r['duration'] for mode, r in reports.items()}
    reduction = 100 * (1 - seconds['parallel'] / seconds['serial'])
    summary = f"Serial: {seconds['serial']:.2f}s; parallel: {seconds['parallel']:.2f}s; reduction: {reduction:.1f}%"
    print(summary)
    Path('benchmark/summary.json').write_text(json.dumps({'seconds': seconds, 'reduction_percent': reduction,
                                                        'counts': reports['parallel']['counts']}, indent=2))
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(summary + '\n')


if __name__ == '__main__':
    main()

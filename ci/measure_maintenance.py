"""Record code movement separately from deletion and new execution adapters."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
from pathlib import Path

MOVES = {
    'src/virgo/tools/sandbox_files.py': 'src/virgo/extensions/sandbox/files.py',
    'src/virgo/tools/windows_sandbox_files.py': 'src/virgo/extensions/sandbox/windows_files.py',
    'src/virgo/tools/windows_sandbox_acl.py': 'src/virgo/extensions/sandbox/windows_acl.py',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', args.baseline, 'src/virgo'], text=True).splitlines()
    records = []
    for name in names:
        if not name.endswith('.py'):
            continue
        old = subprocess.check_output(['git', 'show', f'{args.baseline}:{name}'], text=True).splitlines()
        target = Path(MOVES.get(name, name))
        new = target.read_text().splitlines() if target.exists() else []
        if name not in MOVES and old == new:
            continue
        retained = sum(block.size for block in difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks())
        records.append({'baseline_file': name, 'current_file': str(target) if new else None,
                        'old_lines': len(old), 'new_lines': len(new), 'identical_retained_lines': retained,
                        'moved_out_of_core_lines': retained if name in MOVES else 0,
                        'deleted_or_rewritten_lines': len(old)-retained, 'added_or_rewritten_lines': len(new)-retained})
    additions = {str(p): len(p.read_text().splitlines()) for p in Path('src/virgo').rglob('*.py')
                 if str(p) not in names and str(p) not in MOVES.values()}
    lock = subprocess.check_output(['git', 'show', f'{args.baseline}:uv.lock'])
    report = {'baseline': args.baseline, 'method': 'exact line matching; rewritten lines are not claimed as pure deletions',
              'records': records, 'new_modules_lines': additions,
              'deleted_modules_lines': {r['baseline_file']: r['old_lines'] for r in records
                                        if r['current_file'] is None},
              'retained_protection_lines': {r['current_file']: r['identical_retained_lines'] for r in records
                                            if r['baseline_file'] in MOVES},
              'source_files_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted(Path('src').rglob('*.py'))},
              'python_dependency_lock_changed': lock != Path('uv.lock').read_bytes(),
              'external_dependency': 'User-installed official MXC 1.1.0 standalone executor; measure installed bytes separately',
              'conclusion': 'Inventory only. Moving code does not prove lower maintenance cost.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()

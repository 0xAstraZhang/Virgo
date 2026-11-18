"""Record code movement separately from deletion and new execution adapters."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import subprocess
from pathlib import Path

MOVES = {
    'src/virgo/tools/sandbox_files.py': 'src/virgo/extensions/sandbox/files.py',
    'src/virgo/tools/windows_sandbox_files.py': 'src/virgo/extensions/sandbox/windows_files.py',
    'src/virgo/tools/windows_sandbox_acl.py': 'src/virgo/extensions/sandbox/windows_acl.py',
}


def tree(revision):
    commit = subprocess.check_output(
        ['git', 'rev-parse', '--verify', '--end-of-options', f'{revision}^{{commit}}'], text=True
    ).strip()
    manifest = subprocess.check_output(['git', 'ls-tree', '-rz', '--full-tree', commit, '--', 'src', 'uv.lock'])
    files = {}
    for entry in manifest.split(b'\0')[:-1]:
        metadata, path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        if kind == b'blob' and mode.startswith(b'100') and (path.endswith(b'.py') or path == b'uv.lock'):
            files[path.decode('utf-8', errors='surrogateescape')] = oid
    return commit, files


def blob_contents(oids):
    query = b''.join(oid + b'\n' for oid in oids)
    stream = io.BytesIO(subprocess.check_output(['git', 'cat-file', '--batch'], input=query))
    contents = {}
    for expected in oids:
        oid, kind, size = stream.readline().split()
        if oid != expected or kind != b'blob':
            raise ValueError(f'Unexpected git object for {expected!r}')
        contents[oid] = stream.read(int(size))
        if stream.read(1) != b'\n':
            raise ValueError(f'Truncated git blob {oid!r}')
    return contents


def inventory(baseline, target):
    base_commit, before = tree(baseline)
    target_commit, after = tree(target)
    lock_changed = before.pop('uv.lock', None) != after.pop('uv.lock', None)
    blobs = blob_contents(sorted(set(before.values()) | set(after.values())))
    moves = {old: new for old, new in MOVES.items() if old in before and old not in after
             and new in after and new not in before}
    records = []
    for name, old_oid in before.items():
        if not name.startswith('src/virgo/'):
            continue
        current = moves.get(name, name)
        new_oid = after.get(current)
        if name == current and old_oid == new_oid:
            continue
        old = blobs[old_oid].splitlines()
        new = blobs[new_oid].splitlines() if new_oid is not None else []
        retained = sum(block.size for block in difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks())
        records.append({'baseline_file': name, 'current_file': current if new_oid is not None else None,
                        'old_lines': len(old), 'new_lines': len(new), 'identical_retained_lines': retained,
                        'moved_out_of_core_lines': retained if name in moves else 0,
                        'deleted_or_rewritten_lines': len(old)-retained, 'added_or_rewritten_lines': len(new)-retained})
    additions = {name: len(blobs[oid].splitlines()) for name, oid in after.items()
                 if name.startswith('src/virgo/') and name not in before and name not in moves.values()}
    return {
        'baseline': base_commit,
        'target': target_commit,
        'method': 'exact line matching of committed Git blobs; rewritten lines are not claimed as pure deletions',
        'records': records,
        'new_modules_lines': additions,
        'deleted_modules_lines': {r['baseline_file']: r['old_lines'] for r in records if r['current_file'] is None},
        'retained_protection_lines': {r['current_file']: r['identical_retained_lines'] for r in records
                                      if r['baseline_file'] in moves},
        'source_files_sha256': {name: hashlib.sha256(blobs[oid]).hexdigest() for name, oid in after.items()},
        'python_dependency_lock_changed': lock_changed,
        'external_dependency': 'User-installed official MXC 1.1.0 standalone executor; measure installed bytes separately',
        'conclusion': 'Inventory only. Moving code does not prove lower maintenance cost.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--target', default='HEAD', help='Committed revision to measure (default: HEAD)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = inventory(args.baseline, args.target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()

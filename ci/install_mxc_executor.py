"""Explicit CI-only installation of the pinned official standalone MXC artifact.

This is never called by application startup. The release archive and extracted
executor are both verified before publication; no runtime installer is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ARCHIVE_SHA256 = '31a44963e288e3af23bc2e7e350608ed28f7c76b922ee7a7f1758a486dda37b3'
URL = 'https://github.com/microsoft/mxc/releases/download/v1.1.0/mxc-release-binaries.zip'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--archive', type=Path, help='Use an already downloaded archive, still verifying its digest')
    args = parser.parse_args()
    from virgo.extensions.sandbox.mxc import EXECUTOR_HASHES

    arch = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    digest = EXECUTOR_HASHES.get((sys.platform, arch))
    if digest is None:
        parser.error('No pinned MXC 1.1.0 artifact for this platform/architecture')
    filename = {'darwin': 'mxc-exec-mac', 'linux': 'lxc-exec', 'win32': 'wxc-exec.exe'}[sys.platform]
    with tempfile.TemporaryDirectory(prefix='mxc-install-') as directory:
        archive = args.archive or Path(directory) / 'release.zip'
        if args.archive is None:
            with urllib.request.urlopen(URL, timeout=120) as response, archive.open('wb') as stream:
                shutil.copyfileobj(response, stream)
        with archive.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != ARCHIVE_SHA256:
            raise ValueError('MXC release archive digest mismatch')
        with zipfile.ZipFile(archive) as bundle:
            member = f'{arch}/{filename}'
            data = bundle.read(member)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('MXC executor digest mismatch')
        args.directory.mkdir(parents=True, exist_ok=True)
        output = (args.directory / filename).resolve()
        output.write_bytes(data)
        output.chmod(0o755)
    print(output)
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as stream:
            stream.write(f'VIRGO_MXC_EXECUTABLE={output}\n')


if __name__ == '__main__':
    main()

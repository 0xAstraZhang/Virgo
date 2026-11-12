import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

mode = sys.argv[1]
root = Path(tempfile.mkdtemp(prefix='virgo-original-cache-check-'))
Path('diagnostic-path.txt').write_text(str(root))
env = {**os.environ, 'DOTNET_DbgEnableMiniDump': '1', 'DOTNET_DbgMiniDumpType': '1',
       'DOTNET_DbgMiniDumpName': str(root / 'pwsh.%p.dmp'), 'DOTNET_EnableCrashReport': '1'}
if mode == 'disabled':
    env['DOTNET_MultiCoreJitMinNumCpus'] = 'ffffffff'
start = time.monotonic()
for iteration in range(30):
    result = subprocess.run([sys.executable, 'ci/check_powershell_cache.py'],
                            env=env, capture_output=True, text=True, timeout=180)
    print(json.dumps({'iteration': iteration, 'code': result.returncode,
                      'seconds': time.monotonic() - start}), flush=True)
    (root / f'check-{iteration}.txt').write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stdout + result.stderr, flush=True)
        sys.exit(result.returncode)
print('All 30 original acceptance invocations passed (1440 concurrent launches)', flush=True)

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
env = dict(os.environ)
env['VIRGO_DIAGNOSTICS_DIR'] = str(root)
if mode not in ('readonly-plain', 'telemetry-off', 'telemetry-off-repeat'):
    env.update({'DOTNET_DbgEnableMiniDump': '1', 'DOTNET_DbgMiniDumpType': '1',
                'DOTNET_DbgMiniDumpName': str(root / 'pwsh.%p.dmp'), 'DOTNET_EnableCrashReport': '1',
                'DOTNET_CreateDumpDiagnostics': '1'})
if mode == 'disabled':
    env['DOTNET_MultiCoreJitMinNumCpus'] = 'ffffffff'
if mode.startswith('telemetry-off'):
    env['POWERSHELL_TELEMETRY_OPTOUT'] = 'true'
start = time.monotonic()
for iteration in range(60):
    result = subprocess.run([sys.executable, 'ci/check_powershell_cache.py'],
                            env=env, capture_output=True, text=True, timeout=180)
    print(json.dumps({'iteration': iteration, 'code': result.returncode,
                      'seconds': time.monotonic() - start}), flush=True)
    (root / f'check-{iteration}.txt').write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stdout + result.stderr, flush=True)
        sys.exit(result.returncode)
print('All 60 original acceptance invocations passed (2880 concurrent launches)', flush=True)

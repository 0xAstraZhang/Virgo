"""Host command ownership through the public execution context."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from virgo.execution import HostContext, cleanup_process


def test_host_cleanup_after_group_leader_exit_preserves_other_job(tmp_path):
    host = HostContext(tmp_path)
    other = host.spawn([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, os.environ,
                       subprocess.DEVNULL)
    code = ("import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], stdout=sys.stdout); "
            "sys.exit(17)")
    try:
        command = host.spawn([sys.executable, "-c", code], tmp_path, os.environ, subprocess.PIPE)
        try:
            assert command.wait(timeout=5) == 17
            cleanup_process(command)
            cleanup_process(command)
            with ThreadPoolExecutor(max_workers=1) as reader:
                # EOF acknowledges that the same-group child has released stdout.
                assert reader.submit(command.stdout.read).result(timeout=1) == b""
            assert command.poll() == 17
            assert other.poll() is None
        finally:
            cleanup_process(command)
            command.stdout.close()
    finally:
        cleanup_process(other)

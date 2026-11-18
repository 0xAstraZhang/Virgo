import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ci" / "measure_maintenance.py"


def test_inventory_uses_committed_paths_and_bytes(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    def commit():
        git("add", "-A")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        return git("rev-parse", "HEAD")

    git("init", "-q")
    git("config", "core.autocrlf", "false")
    pkg = tmp_path / "src" / "virgo"
    tools = pkg / "tools"
    tools.mkdir(parents=True)
    (tmp_path / "uv.lock").write_bytes(b"locked\n")
    (pkg / "emptied.py").write_bytes(b"old\n")
    (pkg / "deleted.py").touch()
    old = tools / "sandbox_files.py"
    old.write_bytes(b"protection\n")
    base = commit()

    new = pkg / "extensions" / "sandbox" / "files.py"
    new.parent.mkdir(parents=True)
    old.rename(new)
    (pkg / "deleted.py").unlink()
    (pkg / "emptied.py").write_bytes(b"")
    added = pkg / "module with spaces.py"
    added.write_bytes(b"first\r\nsecond\r\n")
    target = commit()

    added.write_bytes(b"uncommitted\n")
    (pkg / "untracked.py").touch()
    (tmp_path / "uv.lock").write_bytes(b"dirty\n")
    report = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--baseline", base, "--output", str(report)]
    subprocess.run(command, cwd=tmp_path, check=True)
    result = json.loads(report.read_bytes())
    added_name = "src/virgo/module with spaces.py"
    assert result["new_modules_lines"] == {added_name: 2}
    assert result["deleted_modules_lines"] == {"src/virgo/deleted.py": 0}
    assert result["retained_protection_lines"] == {"src/virgo/extensions/sandbox/files.py": 1}
    assert result["source_files_sha256"][added_name] == hashlib.sha256(b"first\r\nsecond\r\n").hexdigest()
    assert not result["python_dependency_lock_changed"]
    assert result["baseline"] == base and result["target"] == target

    subprocess.run([*command, "--target", base], cwd=tmp_path, check=True)
    prior = json.loads(report.read_bytes())
    assert prior["records"] == [] and prior["new_modules_lines"] == {}
    assert prior["target"] == base

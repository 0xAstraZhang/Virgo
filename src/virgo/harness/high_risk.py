"""High-risk shell command detection (R2 / S5-01).

Guards against high-cost or irreversible mistakes (force pushes, protected-branch
pushes, deployments, remote/database deletion, curl|sh, recursive deletion outside
the workspace) across all execution modes. Configurable disable is allowed only in
No-Limit mode.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..execution_policy import lexical_path

PROTECTED_BRANCHES = frozenset({"main", "master", "production", "prod"})

_PIPE_TO_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:curl|wget|fetch|invoke-webrequest|iwr|irm)\b[^|;\n]*\|\s*"
    r"(?:(?:sudo|env)\s+)*(?:/\S*/)?(?:sh|bash|zsh|dash|ksh|fish|python[23]?|node|perl|ruby|pwsh|powershell|iex|invoke-expression)\b",
    re.IGNORECASE,
)

_SQL_DESTRUCTIVE = re.compile(
    r"\b(?:DROP\s+(?:DATABASE|TABLE|SCHEMA|CLUSTER)|TRUNCATE\s+TABLE|"
    r"FLUSHALL|FLUSHDB)\b",
    re.IGNORECASE,
)

_SQL_DELETE_ALL = re.compile(
    r"\bDELETE\s+FROM\s+[A-Za-z_][A-Za-z0-9_.\"`]*(?:\s*;|\s*$|\s*['\"])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HighRiskMatch:
    category: str
    reason: str
    snippet: str


def _split_segments(command: str) -> list[str]:
    """Split a shell command on top-level control operators without crashing on shell syntax."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if escape:
            current.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            current.append(ch)
            escape = True
            i += 1
            continue
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch in {";", "\n"} or (ch in {"&", "|"} and i + 1 < n and command[i + 1] == ch):
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2 if ch in {"&", "|"} else 1
            continue
        if ch == "|":
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    seg = "".join(current).strip()
    if seg:
        segments.append(seg)
    return segments


def _tokenize(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, posix=os.name != "nt")
    except ValueError:
        tokens = segment.split()
    # Strip leading env assignments (FOO=bar) and sudo/env/command wrappers.
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        if tok in {"sudo", "env", "command", "nohup", "time"}:
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            continue
        break
    return tokens[idx:]


def _is_protected_ref(ref: str) -> bool:
    cleaned = ref.lstrip("+").strip()
    if ":" in cleaned:
        _, _, dest = cleaned.rpartition(":")
        cleaned = dest
    for prefix in ("refs/heads/", "refs/remotes/origin/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned in PROTECTED_BRANCHES or cleaned.startswith("release/")


def _check_git(tokens: list[str], segment: str) -> HighRiskMatch | None:
    # Skip global git options like -C <dir>, -c k=v, --git-dir=...
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        i += 2 if opt in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} else 1
    if i >= len(tokens):
        return None
    sub = tokens[i]
    rest = tokens[i + 1 :]
    if sub == "push":
        if any(
            t in {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete"}
            or t.startswith(("--force-with-lease=", "+"))
            for t in rest
        ):
            return HighRiskMatch("git_force_push", "Git force/destructive push can overwrite or delete remote history.", segment)
        positional = [t for t in rest if not t.startswith("-")]
        # git push <remote> <refspec...> or git push <branch>
        if any(_is_protected_ref(ref) for ref in positional[1:]):
            return HighRiskMatch("git_protected_branch", "Git push targets a protected branch (main/master/prod/release).", segment)
    elif sub == "reset" and "--hard" in rest:
        return HighRiskMatch("git_reset_hard", "git reset --hard discards uncommitted working-tree changes.", segment)
    elif sub == "clean" and any(
        t == "--force" or (t.startswith("-") and not t.startswith("--") and "f" in t[1:])
        for t in rest
    ):
        return HighRiskMatch("git_clean_force", "git clean -f permanently deletes untracked files.", segment)
    return None


def _outside_workspace(raw_target: str, cwd: Path, workspace: Path) -> bool:
    text = raw_target.strip()
    if not text:
        return False
    if text in {"/", "/*", "~", "~/", "$HOME", "${HOME}", "%USERPROFILE%"} or text.startswith(("$HOME/", "${HOME}/", "~/")):
        if text.startswith(("~/", "$HOME/", "${HOME}/")):
            expanded = text.replace("$HOME", "~").replace("${HOME}", "~")
            try:
                resolved = lexical_path(expanded, cwd).resolve()
                return not resolved.is_relative_to(workspace)
            except (ValueError, OSError):
                return True
        return True
    try:
        resolved = lexical_path(text, cwd).resolve()
    except (ValueError, OSError):
        return True
    return resolved == workspace or not resolved.is_relative_to(workspace)


def _check_recursive_delete(tokens: list[str], segment: str, cwd: Path, workspace: Path) -> HighRiskMatch | None:
    cmd = Path(tokens[0]).name.lower().removesuffix(".exe")
    rest = tokens[1:]
    if cmd == "rm":
        recursive = any(
            t in {"--recursive", "-r", "-R"}
            or (t.startswith("-") and not t.startswith("--") and ("r" in t[1:] or "R" in t[1:]))
            for t in rest
        )
        if not recursive:
            return None
        targets = []
        after_dd = False
        for t in rest:
            if t == "--" and not after_dd:
                after_dd = True
                continue
            if not after_dd and t.startswith("-"):
                continue
            targets.append(t)
        for target in targets:
            if _outside_workspace(target, cwd, workspace):
                return HighRiskMatch(
                    "recursive_delete_outside_workspace",
                    f"Recursive deletion targets path outside workspace: {target}",
                    segment,
                )
    elif cmd in {"remove-item", "ri", "del", "erase", "rmdir", "rd"}:
        lowered = [t.lower() for t in rest]
        recursive = any(t in {"-recurse", "-r", "/s"} for t in lowered)
        if not recursive:
            return None
        targets = [t for t in rest if not t.startswith(("-", "/"))]
        for target in targets:
            if _outside_workspace(target, cwd, workspace):
                return HighRiskMatch(
                    "recursive_delete_outside_workspace",
                    f"Recursive deletion targets path outside workspace: {target}",
                    segment,
                )
    return None


def _check_deploy_or_remote(tokens: list[str], segment: str) -> HighRiskMatch | None:
    cmd = Path(tokens[0]).name.lower().removesuffix(".exe")
    rest = [t for t in tokens[1:] if not t.startswith("-")]
    flags = [t for t in tokens[1:] if t.startswith("-")]
    sub = rest[0].lower() if rest else ""
    sub2 = rest[1].lower() if len(rest) > 1 else ""

    if cmd == "kubectl" and sub in {"apply", "delete", "replace", "patch", "rollout", "drain", "cordon"}:
        return HighRiskMatch("deploy_or_cluster_mutation", f"kubectl {sub} mutates cluster resources.", segment)
    if cmd == "helm" and sub in {"install", "upgrade", "uninstall", "delete", "rollback"}:
        return HighRiskMatch("deploy_or_cluster_mutation", f"helm {sub} mutates deployed releases.", segment)
    if cmd == "terraform" and sub in {"apply", "destroy", "import", "taint"}:
        return HighRiskMatch("infrastructure_mutation", f"terraform {sub} mutates infrastructure.", segment)
    if cmd == "pulumi" and sub in {"up", "destroy"}:
        return HighRiskMatch("infrastructure_mutation", f"pulumi {sub} mutates infrastructure.", segment)
    if cmd in {"docker", "podman"} and sub == "push":
        return HighRiskMatch("publish_or_deploy", f"{cmd} push publishes container images to a registry.", segment)
    if cmd in {"npm", "yarn", "pnpm", "cargo", "uv", "poetry"} and sub == "publish":
        return HighRiskMatch("publish_or_deploy", f"{cmd} publish publishes a package to a registry.", segment)
    if cmd == "twine" and sub == "upload":
        return HighRiskMatch("publish_or_deploy", "twine upload publishes a package to PyPI.", segment)
    if cmd in {"fly", "flyctl"} and sub in {"deploy", "destroy", "apps"} and (sub != "apps" or sub2 == "destroy"):
        return HighRiskMatch("publish_or_deploy", f"{cmd} {sub} deploys or destroys remote apps.", segment)
    if cmd == "vercel" and ("--prod" in flags or sub == "deploy" and "--prod" in flags):
        return HighRiskMatch("publish_or_deploy", "vercel --prod deploys to production.", segment)
    if cmd == "netlify" and sub == "deploy" and "--prod" in flags:
        return HighRiskMatch("publish_or_deploy", "netlify deploy --prod deploys to production.", segment)
    if cmd == "gh" and ((sub == "release" and sub2 in {"create", "delete"}) or (sub == "repo" and sub2 == "delete")):
        return HighRiskMatch("remote_mutation", f"gh {sub} {sub2} mutates remote GitHub state.", segment)
    if cmd == "aws" and any(
        t.startswith(("delete-", "terminate-", "remove-", "purge-", "destroy-")) or t in {"rm", "rb"}
        for t in rest[1:]
    ):
        return HighRiskMatch("cloud_resource_delete", "AWS CLI command deletes or terminates cloud resources.", segment)
    if cmd == "gcloud" and any(t in {"delete", "destroy"} for t in rest[1:]):
        return HighRiskMatch("cloud_resource_delete", "gcloud command deletes cloud resources.", segment)
    if cmd == "az" and any(t in {"delete", "purge"} for t in rest[1:]):
        return HighRiskMatch("cloud_resource_delete", "Azure CLI command deletes cloud resources.", segment)
    if cmd in {"gsutil", "s3cmd"} and sub in {"rm", "rb", "del"}:
        return HighRiskMatch("cloud_resource_delete", f"{cmd} {sub} deletes remote storage objects.", segment)
    return None


def detect_high_risk_command(command: str, cwd: Path, workspace: Path) -> HighRiskMatch | None:
    """Inspect a shell command and return a HighRiskMatch if it needs explicit confirmation."""
    if not isinstance(command, str) or not command.strip():
        return None
    if _PIPE_TO_SHELL.search(command):
        return HighRiskMatch(
            "remote_script_pipe",
            "Piping remote downloaded content directly into a shell/interpreter.",
            command.strip()[:200],
        )
    if _SQL_DESTRUCTIVE.search(command) or _SQL_DELETE_ALL.search(command):
        return HighRiskMatch(
            "database_destruction",
            "Command contains a destructive database/cache statement (DROP/TRUNCATE/FLUSH/unfiltered DELETE).",
            command.strip()[:200],
        )
    resolved_cwd = Path(cwd).resolve()
    resolved_ws = Path(workspace).resolve()
    for segment in _split_segments(command):
        tokens = _tokenize(segment)
        if not tokens:
            continue
        cmd = Path(tokens[0]).name.lower().removesuffix(".exe")
        if cmd == "git":
            if match := _check_git(tokens, segment):
                return match
        if match := _check_recursive_delete(tokens, segment, resolved_cwd, resolved_ws):
            return match
        if match := _check_deploy_or_remote(tokens, segment):
            return match
    return None

"""Conservative shell classification for workspace access and planning mode."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def inside(path: str, cwd: Path, workspace: Path | None = None) -> bool:
    try:
        p = Path(os.path.expanduser(path))
        (p if p.is_absolute() else cwd / p).resolve().relative_to((workspace or cwd).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def split_segments(command: str, *, redirects: bool = False) -> list[list[str]] | None:
    """Return simple shell commands; opaque expansion and control flow need review."""
    if any(token in command for token in ("$", "`", "(", ")", "{", "}")):
        return None
    try:
        lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segments, words = [], []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if words:
                segments.append(words)
            words = []
        elif token and set(token) <= set("<>&|;"):
            if not redirects or token not in {"<", ">", ">>", "<>"}:
                return None
            words.append(token)
        else:
            words.append(token)
    if words:
        segments.append(words)
    return segments or None


READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "grep", "egrep", "rg",
    "stat", "du", "df", "which", "whereis", "type", "whoami", "id", "uname",
    "env", "printenv", "sort", "uniq", "cut", "tr", "diff", "cmp", "md5sum", "sha256sum",
    "basename", "dirname", "realpath", "readlink", "true", "false", "test", "[", "jq", "nl", "column",
    "seq", "find", "sed",
}
_LOCAL_WRITES = {"touch", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "chmod", "tee", "truncate"}
_GIT_READ_ONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "grep"}
_PATH_OPTIONS = {"cp": "t", "mv": "t", "ln": "t", "touch": "r", "truncate": "r", "sort": "To", "git": "C", "grep": "f", "rg": "f", "sed": "f", "curl": "oKTDd", "wget": "OP"}
_NETWORK = {
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "rsync", "ping", "dig", "nslookup", "host", "nc",
    "ncat", "netcat", "telnet", "socat", "http", "https", "fetch",
}
_OPAQUE_ENV = {"PATH", "CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}


def _local_check(words: list[str]) -> bool:
    if not words:
        return False
    name, *args = words
    if any(arg.startswith(("--install-types", "--index-url", "--extra-index-url", "--proxy", "--tx", "--rsyncdir")) for arg in args):
        return False
    if name in {"pytest", "ruff", "mypy"}:
        return True
    if name in {"python", "python3"} and len(args) >= 2 and args[0] == "-m":
        return _local_check(args[1:])
    if name == "uv":
        offline = False
        while args and args[0] == "--offline":
            offline, args = True, args[1:]
        if not args or args[0] != "run":
            return False
        args = args[1:]
        while args and args[0] in {"--offline", "--no-sync", "--frozen", "--locked"}:
            offline = offline or args[0] in {"--offline", "--no-sync"}
            args = args[1:]
        return offline and _local_check(args)
    return False


def _strip_assignments(words: list[str]) -> list[str] | None:
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        key = words[0].split("=", 1)[0]
        if key in _OPAQUE_ENV or key.startswith("GIT_"):
            return None
        words = words[1:]
    return words


def _git_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub, *rest = args
    if any(a.startswith(("--output", "--ext-diff", "--textconv", "--exec-path")) for a in rest):
        return False
    if sub in _GIT_READ_ONLY:
        return not (sub == "grep" and any(a.startswith(("-O", "--open-files-in-pager")) for a in rest))
    if sub in {"branch", "tag"}:
        return not rest or (rest[0] in {"--list", "-l"} and all(not a.startswith("-") for a in rest[1:]))
    if sub == "remote":
        return not rest or rest == ["-v"] or (rest[0] == "get-url" and len(rest) == 2)
    return sub == "config" and len(rest) == 2 and rest[0] in {"--get", "--get-all", "--get-regexp"}


def _safe_read_words(words: list[str], extra: set[str]) -> bool:
    words = _strip_assignments(words)
    if not words:
        return False
    executable, *args = words
    name = os.path.basename(executable)
    # A path to an arbitrary project script must not masquerade as a standard command.
    if "/" in executable and str(Path(executable).parent) not in {"/bin", "/usr/bin"}:
        return False
    if name == "git":
        while len(args) >= 2 and args[0] == "-C":
            args = args[2:]
        return _git_read_only(args)
    if name in {"python", "python3", "node", "npm", "uv", "cargo"} and args == ["--version"]:
        return True
    if name == "go" and args == ["version"]:
        return True
    if name == "env":
        return not args  # env can otherwise execute an arbitrary program
    if name == "find" and any(a.startswith(("-delete", "-exec", "-ok", "-fprint", "-fls")) for a in args):
        return False
    if name == "sed":
        # sed programs can write files or execute commands; approve only simple print/delete/substitute forms.
        scripts = [a for a in args if not a.startswith("-")]
        if any(a.startswith(("-i", "-f", "-e", "--file", "--in-place", "--expression")) for a in args) or not scripts:
            return False
        script = scripts[0]
        if not re.fullmatch(r"(?:\d+(?:,\d+)?|\$)?[pdq]|s/[^/\n]*/[^/\n]*/[gIp0-9]*", script):
            return False
    if name == "sort" and any(a == "-o" or a.startswith(("-o", "--output", "--compress-program")) for a in args):
        return False
    if name == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return False  # second positional argument is an output file
    if name == "rg" and any(a.startswith(("--pre", "--hostname-bin")) for a in args):
        return False
    if name in READ_ONLY_COMMANDS:
        return True
    candidates = {name, " ".join([name, *args[:1]]), " ".join([name, *args[:2]])}
    return bool(candidates & extra)


def is_read_only_command(command: str, extra: set[str] | None = None) -> bool:
    segments = split_segments(command)
    return bool(segments) and all(_safe_read_words(words, extra or set()) for words in segments)


def _argument_value(name: str, argument: str) -> str:
    value = argument.split("=", 1)[-1] if "=" in argument else argument
    if argument.startswith("-") and not argument.startswith("--"):
        for position, option in enumerate(argument[1:], 2):
            if option in _PATH_OPTIONS.get(name, ""):
                if argument[position:]:
                    value = argument[position:]
                break
    return re.sub(r"^-[A-Za-z]+(?=[/~]|\.{1,2}/)", "", value)


def command_path_targets(command: str, cwd: Path) -> tuple[str, ...] | None:
    """Bind repeat approvals to static path targets; dynamic paths cannot be remembered."""
    segments = split_segments(command, redirects=True)
    if not segments or any(char in command for char in "*?["):
        return None
    current = cwd.resolve()
    targets = []
    try:
        for raw_words in segments:
            words = _strip_assignments(raw_words)
            if not words:
                return None
            executable, *args = words
            name = os.path.basename(executable)
            if "/" in executable:
                targets.append(str((current / os.path.expanduser(executable)).resolve()))
            local_cwd = current
            if name == "git":
                while args and args[0].startswith("-C"):
                    if args[0] == "-C":
                        if len(args) < 2:
                            return None
                        directory, args = args[1], args[2:]
                    else:
                        directory, args = args[0][2:], args[1:]
                    local_cwd = (local_cwd / os.path.expanduser(directory)).resolve()
                    targets.append(str(local_cwd))
            for argument in args:
                value = _argument_value(name, argument)
                if value.startswith("@"):
                    value = value[1:]
                if not value or value.startswith("-") or value in {"<", ">", ">>", "<>"} or "://" in value:
                    continue
                targets.append(str((local_cwd / os.path.expanduser(value)).resolve()))
            if name == "cd":
                if len(args) != 1 or os.environ.get("CDPATH") or any(token in command for token in (";", "\n", "|")) or re.search(r"(?<!&)&(?!&)", command):
                    return None
                current = (current / os.path.expanduser(args[0])).resolve()
        return tuple(targets)
    except (OSError, RuntimeError, ValueError):
        return None


def command_review_reason(command: str, cwd: Path, workspace: Path) -> str:
    """Empty means statically confined to the workspace; anything else goes to the reviewer."""
    if not inside(str(cwd), cwd, workspace):
        return "command runs outside the workspace"
    segments = split_segments(command, redirects=True)
    if not segments:
        return "shell expansion or control flow needs review"
    if any(char in command for char in "*?["):
        return "wildcard paths may follow links outside the workspace"
    current = cwd.resolve()
    for raw_words in segments:
        words = _strip_assignments(raw_words)
        if not words:
            return "shell environment changes need review"
        executable, *args = words
        name = os.path.basename(executable)
        # Inspect assignments too: an output directory can be supplied through an environment variable.
        command_index = len(raw_words) - len(words)
        for index, arg in enumerate(raw_words):
            value = _argument_value(name, arg)
            if re.search(r"(?:https?|ftp|ssh)://|^[^/\s]+@[^:\s]+:", value):
                return "command may use the network"
            if value.startswith(("/", "~", "..")) and index != command_index and not inside(value, current, workspace):
                return f"command accesses a path outside the workspace: {(current / os.path.expanduser(value)).resolve()}"
            candidate = current / value
            if ("/" in value or (not value.startswith("-") and (candidate.exists() or candidate.is_symlink()))) and index != command_index:
                if not inside(value, current, workspace):
                    return f"command accesses a path outside the workspace: {(current / os.path.expanduser(value)).resolve()}"
        if "/" in executable and str(Path(executable).parent) not in {"/bin", "/usr/bin"}:
            return "executing a script or nonstandard executable needs review"
        if name in _NETWORK:
            return "command uses the network"
        # Redirections are allowed only when their targets resolve inside the workspace.
        plain = []
        index = 0
        while index < len(words):
            word = words[index]
            if word in {"<", ">", ">>", "<>"}:
                if index + 1 == len(words) or not inside(words[index + 1], current, workspace):
                    return "redirection accesses a path outside the workspace"
                index += 2
                continue
            plain.append(word)
            index += 1
        args = plain[1:]
        if name == "cd":
            if os.environ.get("CDPATH"):
                return "CDPATH makes directory changes ambiguous"
            if any(token in command for token in (";", "\n", "|")) or re.search(r"(?<!&)&(?!&)", command):
                return "conditional or subshell directory changes need review"
            if len(args) != 1 or args[0].startswith("-") or not inside(args[0], current, workspace):
                return "directory change leaves or cannot be confined to the workspace"
            current = (current / args[0]).resolve()
            continue
        if name == "git":
            git_cwd = current
            while len(args) >= 2 and args[0] == "-C":
                if not inside(args[1], git_cwd, workspace):
                    return "git operates outside the workspace"
                git_cwd = (git_cwd / args[1]).resolve()
                args = args[2:]
            if not args:
                return "git invocation needs review"
            if args[0] in {"fetch", "pull", "push", "clone", "ls-remote", "submodule"}:
                return "git command may use the network"
            if _git_read_only(args):
                continue
            return "git command may invoke hooks or change configuration"
        if name in _LOCAL_WRITES or name in {"echo", "printf"} or _safe_read_words(plain, set()) or _local_check(plain):
            continue
        return "command behavior cannot be confined to the workspace without review"
    return ""

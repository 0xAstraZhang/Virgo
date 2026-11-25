"""Discover supported interpreters and bind each command tool to one immutable shell."""

from __future__ import annotations

import ast
import base64
import ntpath
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import groupby, pairwise
from pathlib import Path

from ..agent.types import AgentTool, ToolResult
from ..i18n import Translator

SUPPORTED = ("bash", "zsh", "sh", "dash", "pwsh", "powershell")
SHELL_TOOLS = {"bash", "powershell"}

_WRITE_ERROR_RE = re.compile(
    r"read-only file system|\berofs\b|errno 30\b|failure writing output|failed writing (?:body|received data)"
    r"|failed to create the file|cannot create (?:regular file|directory)", re.I,
)
_PERMISSION_RE = re.compile(
    r"operation not permitted|permission denied|access is denied|access to the path(?: (?P<path>.+?))? is denied"
    r"|unauthorizedaccessexception|errno (?:1|13)\b|\b(?:eperm|eacces)\b", re.I,
)
_MUTATION_ERROR_RE = re.compile(
    r"(?:cannot|can't|unable to|failed to) (?:create|unlink|rename|remove|write to) (?:old )?['\"`/]", re.I,
)
_TMP_PATH_RE = re.compile(r"""(?:^|[\s"'=:(>])/(?:private/)?tmp(?:/|[\s"'&;|):]|$)""")

# Preserve quoted words (including interpreter source) while separating shell control
# operators and redirections. Only literal targets can corroborate a diagnostic.
_SHELL_TOKEN_RE = re.compile(
    r"""\d*(?:&>>?|>\||>>|>&|<&|[<>])|(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|()<>'"\\])+|[;&|()\n]"""
)
_FILE_MUTATORS = {"touch", "mkdir", "rmdir", "rm", "unlink", "tee", "chmod", "chown"}
_PS_MUTATORS = {"set-content", "add-content", "out-file", "new-item", "copy-item", "move-item", "remove-item", "rename-item"}
_DOTNET_WRITE_RE = re.compile(r"\[(?:system\.)?io\.file\]::write\w+", re.I)
_CONTROL_OPERATORS = {";", "&", "|", "\n"}


def _literal(word: str) -> str:
    if "'" in word or '"' in word or "\\ " in word:
        try:
            return shlex.split(word)[0]
        except (ValueError, IndexError):
            pass
    return word


def _program_name(executable: str) -> str:
    return ntpath.basename(executable).lower().removesuffix(".exe")


@dataclass
class _Invocation:
    executable: str
    args: list[str]
    inputs: list[str]
    outputs: list[str]

    @property
    def program(self) -> str:
        return _program_name(self.executable)

    def references(self, target: str) -> bool:
        return any(_mentions(word, target) for word in [self.executable, *self.args, *self.inputs, *self.outputs])


def _invocations(command: str) -> Iterator[_Invocation]:
    for separator, group in groupby(_SHELL_TOKEN_RE.findall(command), key=_CONTROL_OPERATORS.__contains__):
        if separator:
            continue
        args, inputs, outputs = [], [], []
        words = iter(group)
        for word in words:
            operator = word.lstrip("0123456789")
            if operator in {">", ">>", ">|", "&>", "&>>", "<", "<&", ">&"}:
                target = _literal(next(words, ""))
                if operator not in {"<&", ">&"}:
                    (inputs if operator == "<" else outputs).append(target)
            elif word not in {"(", ")"}:
                if len(args) == 1 and _DOTNET_WRITE_RE.fullmatch(args[0]):
                    # A .NET call can put the next argument directly after the closing quote and comma.
                    if path := re.match(r"""(?:'[^']*'|"[^"]*")(?=,|$)""", word):
                        word = path[0]
                args.append(_literal(word))
        argv = iter(args)
        executable = next(
            (arg for arg in argv if arg not in {"env", "command"} and not re.match(r"^[A-Za-z_]\w*=", arg)), ""
        )
        yield _Invocation(executable, list(argv), inputs, outputs)


def _parse_python(source: str) -> ast.Module | None:
    try:
        # Tracebacks show the header of a with statement without its suite.
        return ast.parse(source + " pass" if source.endswith(":") else source)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _python_file_accesses(tree: ast.AST) -> Iterator[tuple[ast.expr, bool]]:
    """Yield (path, writes) for file operations whose mode is known."""
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        args = call.args
        match call.func:
            case ast.Name(id="open") | ast.Attribute(value=ast.Name(id="io"), attr="open"):
                method = "open"
                if not args:
                    args = [kw.value for kw in call.keywords if kw.arg == "file"]
            case ast.Attribute(value=ast.Name(id="os"), attr=method):
                if method not in {"mkdir", "makedirs", "rename", "replace", "remove", "unlink", "rmdir"}:
                    continue
            case ast.Attribute(value=receiver, attr=method):
                if method not in {"open", "read_text", "read_bytes", "write_text", "write_bytes",
                                  "touch", "mkdir", "unlink", "rename", "rmdir"}:
                    continue
                match receiver:
                    case ast.Call(func=ast.Name(id="Path") | ast.Attribute(attr="Path"), args=[path, *_]):
                        receiver = path
                args = [receiver, *args]
            case _:
                continue
        writes = method not in {"read_text", "read_bytes"}
        if method == "open":
            mode = next((kw.value for kw in call.keywords if kw.arg == "mode"), args[1] if len(args) > 1 else None)
            if mode is None:
                if any(kw.arg is None for kw in call.keywords) or any(isinstance(arg, ast.Starred) for arg in args):
                    continue
                writes = False
            elif isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                writes = bool(set("wax+").intersection(mode.value))
            else:
                continue
        for target in args[:2 if method in {"rename", "replace"} else 1]:
            yield target, writes


def _python_source(cmd: _Invocation) -> str | None:
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", cmd.program):
        for flag, source in pairwise(cmd.args):
            if re.fullmatch("-[A-Za-z]*c", flag):
                return source
    return None


def _option_values(args: list[str], short: str, long: str) -> list[str]:
    values = []
    for i, arg in enumerate(args):
        if arg in {short, long}:
            values.extend(args[i + 1:i + 2])
        elif arg.startswith(long + "="):
            values.append(arg[len(long) + 1:])
        elif arg.startswith(short) and arg != short:
            values.append(arg[len(short):])
    return values


def _powershell_target(cmd: _Invocation) -> list[str]:
    copying = cmd.program in {"copy-item", "move-item"}
    parameters = ("-path", "-destination" if copying else "-value")
    bound = {}
    positional = []
    words = iter(cmd.args)
    for word in words:
        flag = word.lower()
        if flag == "-literalpath":
            flag = "-path"
        if flag in parameters:
            bound[flag] = next(words, "")
        elif not word.startswith("-"):
            positional.append(word)
    # Named arguments bind first, even when they follow a positional operand.
    unbound = [p for p in parameters if p not in bound]
    bound.update(zip(unbound, positional, strict=False))
    target = bound.get("-destination" if copying else "-path")
    return [target] if target else []


def _write_targets(cmd: _Invocation) -> list[str]:
    program, args = cmd.program, cmd.args
    operands = [arg for arg in args if not arg.startswith("-")]
    if program in {"cp", "mv", "ln", "install"}:
        return _option_values(args, "-t", "--target-directory") or operands[-1:]
    if program in {"copy-item", "move-item", "set-content", "add-content"}:
        return _powershell_target(cmd)
    if _DOTNET_WRITE_RE.fullmatch(program):
        return args[:1]
    if program == "touch":
        references = _option_values(args, "-r", "--reference")
        return [p for p in operands if p not in references]
    if program in _FILE_MUTATORS or program in _PS_MUTATORS:
        return operands
    if program == "dd":
        return [arg[3:] for arg in args if arg.startswith("of=")]
    if program == "curl":
        return _option_values(args, "-o", "--output")
    if program == "wget":
        return _option_values(args, "-O", "--output-document")
    if program == "unzip" and "-d" in args:
        index = args.index("-d")
        return args[index + 1:index + 2]
    if program == "git" and args and args[0] in {"init", "clone"}:
        return operands[-1:] if len(operands) > 1 else []
    if program == "tar" and args and (args[0] == "--create" or re.fullmatch("-?[a-z]*c[a-z]*", args[0])):
        # tar accepts grouped options such as czf archive or -czf archive.
        flags = ["-f", *args[1:]] if re.fullmatch("-?[a-z]*f", args[0]) else args
        return _option_values(flags, "-f", "--file")
    if (source := _python_source(cmd)) and (tree := _parse_python(source)):
        return [p.value for p, writes in _python_file_accesses(tree)
                if writes and isinstance(p, ast.Constant) and isinstance(p.value, str)]
    return []


def _mentions(line: str, path: str) -> bool:
    return bool(path and re.search(r"""(?:^|[\s:'"(=])""" + re.escape(path) + r"""(?=$|[\s:'")])""", line))


def _explicit_write_destination(line: str, diagnostic: str, reporter: str, commands: list[_Invocation]) -> str:
    callback_error = "writing" in diagnostic.lower()
    if reporter:
        candidates = [cmd for cmd in commands if cmd.program == reporter]
    elif callback_error:
        candidates = [cmd for cmd in commands if cmd.program == "curl"]
    else:
        candidates = commands if len(commands) == 1 else []
    targets = [p for cmd in candidates for p in (_write_targets(cmd) or cmd.outputs or [""])]
    # A copy error can name both its source and its destination. A curl callback
    # can omit both. In either case, specialize only for an attributable output.
    named = [p for p in targets if _mentions(line, p)]
    if named:
        return next((p for p in named if not _TMP_PATH_RE.match(p)), named[0])
    if (callback_error or diagnostic.lower() == line.lower()) and targets and all(_TMP_PATH_RE.match(p) for p in targets):
        return targets[0]
    return line


def _powershell_write_path(path: str, commands: list[_Invocation]) -> str | None:
    """PowerShell can report a resolved path even when the command used a relative operand."""
    resolved = ntpath.normcase(ntpath.normpath(path))

    def matches(operand: str) -> bool:
        target = ntpath.normcase(ntpath.normpath(operand))
        return resolved == target or (ntpath.isabs(resolved) and resolved.endswith("\\" + target))

    written = False
    for cmd in commands:
        targets = _write_targets(cmd)
        if any(matches(p) for p in [cmd.executable, *cmd.inputs, *cmd.args] if p not in targets):
            return None
        if any(matches(p) for p in targets):
            written = True
    return path if written else None


def _traceback_frames(lines: list[str], end: int, commands: list[_Invocation]) -> Iterator[ast.AST]:
    """Read the current traceback from the innermost frame out."""
    for i in range(end - 1, -1, -1):
        header = lines[i]
        if not header.startswith("  "):
            break
        if not header.startswith('  File "'):
            continue
        source = lines[i + 1]
        if not source.startswith("    "):
            # Python 3.12 reports the line number for -c but can omit its source.
            location = re.match(r'  File "<string>", line ([0-9]{1,10}),', header)
            if not location:
                continue
            candidates = [inline for cmd in commands if (inline := _python_source(cmd)) is not None]
            if len(candidates) != 1:
                continue
            statements = candidates[0].splitlines()
            lineno = int(location[1])
            if not 1 <= lineno <= len(statements):
                continue
            source = statements[lineno - 1]
        tree = _parse_python(source.strip())
        if tree is None:
            continue
        if len(tree.body) > 1 and i + 2 < end and (mark := re.fullmatch(r"\s*([~^]+)\s*", lines[i + 2])):
            # Select the marked statement. AST columns are UTF-8 byte offsets.
            column = len(source[:mark.start(1)].lstrip().encode("utf-8"))
            yield next((stmt for stmt in reversed(tree.body) if stmt.col_offset <= column), tree)
        else:
            yield tree


def _denied_write_line(
    line: str, frames: Iterator[ast.AST], definite: re.Match[str] | None, commands: list[_Invocation],
) -> str | None:
    reporter, colon, message = ntpath.splitdrive(line)[1].partition(":")
    reporter = _program_name(reporter.strip()) if colon else ""
    if _MUTATION_ERROR_RE.search(line):
        return line
    if definite:
        return _explicit_write_destination(line, definite[0], reporter, commands)

    python_error = reporter in {"permissionerror", "oserror"}
    if python_error:
        for frame in frames:
            accesses = list(_python_file_accesses(frame))
            if accesses:
                matching = [
                    writes for target, writes in accesses
                    if not isinstance(target, ast.Constant) or (isinstance(target.value, str) and _mentions(line, target.value))
                ]
                # Without a marker, a read of the same path can also explain the denial.
                return line if matching and all(matching) else None

    if reporter in SUPPORTED:
        # The shell reports both input and output redirections, and exec failures.
        if any(_mentions(line, p) for cmd in commands for p in [cmd.executable, *cmd.inputs]):
            return None
        return next((p for cmd in commands for p in cmd.outputs if _mentions(line, p)), None)

    named = [cmd for cmd in commands if cmd.program == reporter]
    exception = reporter.endswith("unauthorizedaccessexception")
    anonymous = _PERMISSION_RE.fullmatch((message if exception else line).strip().rstrip("."))
    if anonymous and anonymous["path"]:
        return _powershell_write_path(anonymous["path"].strip("'\""), commands)
    if named:
        possible = named
    elif reporter in _FILE_MUTATORS and ":" in message:
        return line
    elif python_error:
        possible = [cmd for cmd in commands if cmd.program.startswith("python")]
    elif anonymous or exception:
        possible = commands
    else:
        # Some utilities report only "path: Permission denied". Include readers
        # and executables here: either could account for the very same path.
        possible = [cmd for cmd in commands if any(line.startswith(p + ":") for p in [cmd.executable, *cmd.args, *cmd.inputs])]
    for cmd in possible:
        if named and cmd.program == "tar" and cmd.args:
            extracting = cmd.args[0] == "--extract" or re.fullmatch("-?[a-z]*x[a-z]*", cmd.args[0])
            if extracting and re.match(r".+: cannot open:", message, re.I) and not any(_mentions(message, p) for p in cmd.args):
                return next(iter(_option_values(cmd.args, "-C", "--directory")), line)
        targets = _write_targets(cmd)
        for target in targets:
            if _mentions(line, target) and not any(other.references(target) for other in possible if other is not cmd):
                return target
        if named and cmd.program in _PS_MUTATORS - {"copy-item", "move-item"}:
            return line
        if len(commands) == 1 and anonymous:
            if cmd.outputs and not cmd.inputs and cmd.program in {"", "echo", "printf", ":"}:
                return cmd.outputs[0] if len(cmd.outputs) == 1 else line
            if cmd.program in _FILE_MUTATORS | _PS_MUTATORS or _DOTNET_WRITE_RE.fullmatch(cmd.program):
                return targets[0] if len(targets) == 1 else line
    return None


def _denied_write(command: str, output: str) -> str | None:
    """Use the last attributable write denial for temp-dir advice."""
    lines = output.splitlines()
    commands = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        definite = _WRITE_ERROR_RE.search(line)
        if definite or _PERMISSION_RE.search(line):
            if commands is None:
                commands = list(_invocations(command))
            evidence = _denied_write_line(line, _traceback_frames(lines, i, commands), definite, commands)
            if evidence is not None:
                return evidence
    return None


def _tmp_is_writable(write_paths: list[str] | tuple[str, ...]) -> bool:
    for raw in write_paths:
        normalized = raw.rstrip("/") or "/"
        if normalized in {"/", "/tmp", "/private/tmp"}:
            return True
    return False


@dataclass(frozen=True)
class SandboxHint:
    code: str
    dimension: str
    message_key: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "dimension": self.dimension,
            "message_key": self.message_key,
            "message": self.message,
        }


def sandbox_failure_details(
    command: str,
    output_text: str,
    boundary: dict | None,
    *,
    t=None,
) -> SandboxHint | None:
    """Classify a sandbox write-boundary denial into a structured hint."""
    if not isinstance(boundary, dict) or boundary.get("provider") != "sandbox":
        return None
    effective = (boundary.get("effective") or {}).get("native_process") or boundary.get("requested") or {}
    if effective.get("unrestricted"):
        return None
    evidence = _denied_write(command or "", output_text or "")
    if evidence is None:
        return None
    t = t or Translator()
    write_paths = effective.get("write_paths") or []
    is_posix = boundary.get("backend") != "windows-appcontainer"
    if is_posix and not _tmp_is_writable(write_paths) and _TMP_PATH_RE.search(evidence):
        code, key = "tmp_read_only", "tools.output.sandbox_tmp_hint"
    elif not write_paths:
        code = "read_only_mode"
        key = "tools.output.sandbox_readonly_hint" if is_posix else "tools.output.sandbox_windows_readonly_hint"
    else:
        code = "write_outside_boundary"
        key = "tools.output.sandbox_write_hint" if is_posix else "tools.output.sandbox_windows_write_hint"
    return SandboxHint(code=code, dimension="write_paths", message_key=key, message=t(key))


def sandbox_failure_hint(
    command: str,
    output_text: str,
    boundary: dict | None,
    *,
    t=None,
) -> str | None:
    """Return a concise sandbox write-boundary hint when a sandboxed command hits a write denial."""
    hint = sandbox_failure_details(command, output_text, boundary, t=t)
    return hint.message if hint is not None else None


@dataclass(frozen=True)
class Shell:
    kind: str
    executable: str
    version: str

    @property
    def tool_name(self) -> str:
        return "powershell" if self.kind in {"pwsh", "powershell"} else "bash"

    @property
    def native_errors(self) -> bool:
        match = re.match(r"(\d+)\.(\d+)", self.version)
        return self.tool_name == "powershell" and bool(match and tuple(map(int, match.groups())) >= (7, 4))

    @property
    def syntax(self) -> str:
        return (
            "PowerShell"
            if self.tool_name == "powershell"
            else {"bash": "Bash", "zsh": "Zsh"}.get(self.kind, "POSIX sh")
        )

    @property
    def label(self) -> str:
        return f"{self.kind} {self.version} — {self.executable}"

    def environment(self, t=None) -> str:
        t = t or Translator()
        return t(
            "tools.shell.environment",
            tool=self.tool_name,
            kind=self.kind,
            version=self.version,
            executable=self.executable,
            syntax=self.syntax,
        )

    def argv(self, command: str) -> list[str]:
        if self.tool_name == "powershell":
            # The payload is code; encoding avoids an intermediate CMD/Unix shell and quoting layer.
            # Explicit exit remains authoritative. Older engines only propagate the last native exit.
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                "$ProgressPreference = 'SilentlyContinue'\n"
                "[Console]::InputEncoding = [Console]::OutputEncoding = $OutputEncoding = "
                "New-Object System.Text.UTF8Encoding $false\n"
                "$PSNativeCommandUseErrorActionPreference = $true\n"
                "$LASTEXITCODE = 0\n"
                "try {\n" + command + "\n"
                "if (-not $?) { exit 1 }\n"
                "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
                "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }\n"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            argv = [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat",
                "Text",
                "-EncodedCommand",
                encoded,
            ]
            if os.name == "nt" and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
                raise ValueError(
                    "PowerShell command is too long; write a .ps1 file and invoke it with & 'path'."
                )
            return argv
        options = {
            "bash": ["--noprofile", "--norc", "-e", "-o", "pipefail"],
            "zsh": ["-f", "-e", "-o", "pipefail"],
        }.get(self.kind, ["-e"])
        return [self.executable, *options, "-c", command]


def shell_environment() -> dict[str, str]:
    env = dict(os.environ, VIRGO="1")
    # Noninteractive POSIX shells can otherwise load startup files despite --noprofile.
    for key in ("BASH_ENV", "ENV"):
        env.pop(key, None)
    return env


def probe_shell(value: str, *, t=None, execute: bool = True) -> Shell:
    """Validate an exact supported path, optionally executing its version probe."""
    t = t or Translator()
    value = os.path.expanduser(value)
    kind = Path(value).name.lower().removesuffix(".exe")
    if kind not in SUPPORTED:
        raise ValueError(t("tools.shell.unsupported", value=value, supported=", ".join(SUPPORTED)))
    executable = shutil.which(value)
    if not executable:
        raise ValueError(t("tools.shell.unavailable", value=value))
    # Preserve invocation paths: /bin/sh can be a Bash symlink with different semantics.
    executable = os.path.abspath(executable)
    if (
        os.name == "nt"
        and kind == "bash"
        and Path(executable).parent.name.lower() in {"system32", "sysnative"}
    ):
        raise ValueError(t("tools.shell.wsl"))
    if not execute:
        # Discovery is broker work. An active sandbox must not execute a
        # project/PATH candidate merely to learn its version before isolation.
        return Shell(kind, executable, "unknown")
    if kind in {"pwsh", "powershell"}:
        args = [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ]
    elif kind == "bash":
        args = ["--noprofile", "--norc", "-c", 'printf "%s" "$BASH_VERSION"']
    elif kind == "zsh":
        args = ["-f", "-c", 'printf "%s" "$ZSH_VERSION"']
    else:
        args = ["-c", 'printf "%s" "${BASH_VERSION:-POSIX (version not reported)}"']
    try:
        result = subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=shell_environment(),
            timeout=3,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            t("tools.shell.probe_failed_error", executable=executable, type=type(error).__name__)
        ) from error
    version = result.stdout.decode("utf-8", errors="replace").strip()
    reason = ""
    if result.returncode or not version or len(version) > 120 or "\n" in version:
        reason = t("tools.shell.version_probe_failed")
    elif kind not in {"sh", "dash"} and not re.match(r"\d+\.\d+", version):
        reason = t("tools.shell.unknown_version")
    if reason:

        def excerpt(data: bytes) -> str:
            text = data.decode("utf-8", errors="replace")
            return repr(text[:1024]) + (t("tools.shell.probe_truncated") if len(text) > 1024 else "")

        raise ValueError(
            t(
                "tools.shell.probe_failed_details",
                executable=executable,
                reason=reason,
                code=result.returncode,
                stdout=excerpt(result.stdout),
                stderr=excerpt(result.stderr),
            )
        )
    if kind == "powershell" and not version.startswith("5.1.") and int(version.split(".")[0]) < 7:
        raise ValueError(t("tools.shell.powershell_version"))
    if kind == "pwsh" and int(version.split(".")[0]) < 7:
        raise ValueError(t("tools.shell.pwsh_version"))
    return Shell(kind, executable, version)


def discover_shells(extra: str | None = None, *, execute: bool = True) -> list[Shell]:
    candidates: list[str] = [extra] if extra else []
    folders = [p for p in os.get_exec_path() if p and Path(p).is_absolute()]
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        folders.append(str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0"))
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if directory := os.environ.get(key):
                folders.extend(str(p) for p in (Path(directory) / "PowerShell").glob("*") if p.is_dir())
                folders.append(str(Path(directory) / "Git" / "bin"))
    else:
        folders.extend(["/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"])
        try:
            candidates.extend(
                line.strip()
                for line in Path("/etc/shells").read_text().splitlines()
                if line.startswith("/") and Path(line.strip()).name in SUPPORTED
            )
        except OSError:
            pass
    for name in SUPPORTED:
        for folder in dict.fromkeys(folders):
            candidate = Path(folder) / (name + ".exe" if os.name == "nt" else name)
            # WSL can inherit PATH entries that the current user cannot traverse.
            if os.path.isfile(candidate):
                candidates.append(str(candidate))
    candidates = list(dict.fromkeys(candidates))

    def probe(candidate):
        try:
            return probe_shell(candidate, execute=execute)
        except ValueError:
            return None

    # Bounded probes run in parallel, so a broken interpreter cannot stall the picker indefinitely.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, candidates))
    found: list[Shell] = []
    seen = set()
    for shell in results:
        if shell is None:
            continue
        key = (shell.kind, os.path.normcase(os.path.realpath(shell.executable)))
        if key not in seen:
            seen.add(key)
            found.append(shell)
    return found


def default_shell(found: list[Shell] | tuple[Shell, ...], *, t=None) -> Shell:
    """Choose from an existing catalog without probing the machine again."""
    t = t or Translator()
    preferred = ("pwsh", "powershell", "bash", "sh") if os.name == "nt" else ("bash", "zsh", "sh")
    for name in preferred:
        if shell := next((s for s in found if s.kind == name), None):
            return shell
    raise ValueError(t("tools.shell.none_available"))


@dataclass
class ShellTool(AgentTool):
    shell: Shell | None = None


def make_shell_tool(shell: Shell, jobs=None, *, execution=None, on_unavailable=None) -> ShellTool:
    from .bash import BASH_PARAMS, DEFAULT_TIMEOUT, MAX_TIMEOUT, format_output, run_command
    from .output import MAX_BYTES, MAX_LINES

    def _cancelled_result(error: Exception) -> ToolResult:
        return ToolResult.error(str(error), details={"status": "aborted", "cancelled": True,
                                  "boundary": {"kind": "aborted", "payload_started": False,
                                              "side_effects": "none", "escalation_available": False}})

    def _unavailable_result(error: Exception) -> ToolResult:
        reason = getattr(error, "reason", None)
        message = str(error)
        if (reason in {None, "backend_unavailable",
                       "windows_network_isolation_unavailable", "windows_null_device_unavailable",
                       "windows_runtime_inaccessible", "windows_unsupported_os"}
                and "--permission no-limit" not in message):
            message = (
                f"{message}\n"
                "To run without the native sandbox, switch with `/permissions no-limit` "
                "or start with `--permission no-limit`."
            )
        return ToolResult.error(message, details={"status": "execution_unavailable", "boundary": {
            "kind": "execution_unavailable", "dimension": getattr(error, "dimension", None),
            "target": getattr(error, "target", None), "reason": reason,
            "payload_started": False, "side_effects": "none", "escalation_available": False,
        }})

    def _failed_result(error: Exception) -> ToolResult:
        return ToolResult.error(str(error), details={"status": "execution_failed", "boundary": {
            "kind": "execution_failed", "payload_started": "unknown", "side_effects": "possible",
            "escalation_available": False, **getattr(error, "boundary", {}),
        }})

    def execute(args, ctx):
        from ..execution import ExecutionCancelled, ExecutionFailed, ExecutionUnavailable

        policy = None
        try:
            policy = execution(ctx) if execution is not None else None
            result = execute_with_policy(args, ctx, policy)
        except ExecutionCancelled as error:
            result = _cancelled_result(error)
        except ExecutionUnavailable as error:
            fallback = None
            if on_unavailable is not None and policy is not None and getattr(policy, "provider", None) != "host":
                fallback = on_unavailable(ctx, args, error)
            if fallback is not None:
                policy = fallback
                try:
                    result = execute_with_policy(args, ctx, fallback)
                    facts = (result.details or {}).get("boundary", {})
                    result.details = {**(result.details or {}), "boundary": {**facts, "fallback": "user_approved_no_limit_once"}}
                except ExecutionCancelled as inner:
                    result = _cancelled_result(inner)
                except ExecutionUnavailable as inner:
                    result = _unavailable_result(inner)
                except ExecutionFailed as inner:
                    result = _failed_result(inner)
            else:
                result = _unavailable_result(error)
        except ExecutionFailed as error:
            result = _failed_result(error)
        if policy is not None:
            facts = (result.details or {}).get("boundary", {})
            result.details = {**(result.details or {}), "boundary": {**policy.describe(), **facts}}
        return result

    def execute_with_policy(args, ctx, policy):
        timeout = min(float(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        if jobs is not None and args.get("background"):
            job = jobs.start_command(
                args["command"],
                ctx.cwd,
                timeout if args.get("timeout") else None,
                shell=shell,
                store=ctx.origin.root if ctx.origin is not None else None,
                agent=ctx.agent,
                tool_call_id=ctx.call_id,
                execution_context=policy, cancellation=ctx.signal,
            )
            return ToolResult.text(
                ctx.t("tools.shell.job_started", id=job.id, kind=shell.kind, version=shell.version),
                details={"job_id": job.id, "boundary": dict(job.boundary or {})},
            )
        out, code, status = run_command(args["command"], ctx.cwd, timeout, ctx.signal, interpreter=shell, t=ctx.t,
                                        **({"execution_context": policy} if policy is not None else {}))
        result = format_output(out, code, status, ctx.cwd, timeout, prefix=shell.tool_name, t=ctx.t)
        result.details.update(shell=shell.kind, executable=shell.executable, version=shell.version)
        if out.boundary is not None:
            result.details["boundary"] = dict(out.boundary)
        if status == "ok" and code:
            effective_boundary = {**(policy.describe() if policy is not None else {}), **(out.boundary or {})}
            hint = sandbox_failure_details(args["command"], out.text, effective_boundary, t=ctx.t)
            if hint is not None:
                boundary = dict(result.details.get("boundary") or {})
                boundary["hint"] = hint.to_dict()
                result.details["boundary"] = boundary
        return result

    params = {**BASH_PARAMS, "properties": {**BASH_PARAMS["properties"]}}
    params["properties"]["timeout"] = {**params["properties"]["timeout"], "exclusiveMinimum": 0}
    if jobs is not None:
        params["properties"]["background"] = {
            "type": "boolean",
            "description": "Run in the background; job_output/job_kill manage the job.",
        }

    def values(t):
        error_key = "tools.shell.errors.posix"
        if shell.tool_name == "powershell":
            error_key = (
                "tools.shell.errors.powershell" if shell.native_errors else "tools.shell.errors.compatibility"
            )
        elif shell.kind in {"sh", "dash"}:
            error_key = "tools.shell.errors.sh"
        return {
            "syntax": shell.syntax,
            "environment": shell.environment(t),
            "errors": t(error_key),
            "max_lines": MAX_LINES,
            "max_kb": MAX_BYTES // 1024,
            "background": t("tools.shell.background") if jobs is not None else "",
        }

    translator = Translator()
    return ShellTool(
        execution_scope="process",
        name=shell.tool_name,
        label=shell.syntax,
        description=translator("tools.shell.description", **values(translator)),
        parameters=params,
        execute=execute,
        shell=shell,
        i18n_key="tools.shell",
        i18n_values=values,
    )

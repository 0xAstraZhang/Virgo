"""Controlled local Git reads shared by planning and frozen code review.

Only fixed call sites supply argv. User revisions are resolved before object
reads; live-worktree diff/status and repository-provided helpers are not exposed.
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent import AgentTool, ToolResult
from .bash import kill_tree

MAX_GIT_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 45 * 1024


class GitReadError(RuntimeError):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def check_cancel(signal):
    if signal.is_set():
        raise GitReadError("Read-only Git operation was cancelled.", "cancelled")


class ControlledGit:
    def __init__(self, workspace, signal):
        executable = shutil.which("git")
        if executable is None:
            raise GitReadError("Git is required for controlled repository reads.")
        executable = Path(executable).resolve()
        if executable.is_relative_to(workspace):
            raise GitReadError("Controlled Git reads cannot execute a binary from the workspace.")
        self.workspace, self.signal = workspace, signal
        self.argv = [str(executable), "--no-pager"]
        self.metadata_argv = list(self.argv)
        for setting in (
            "core.fsmonitor=false", "core.untrackedCache=false", f"core.hooksPath={os.devnull}",
            f"core.attributesFile={os.devnull}", "gc.auto=0", "maintenance.auto=false",
            "diff.external=", "protocol.allow=never", "credential.helper=", "submodule.recurse=false",
            "log.showSignature=false", "color.ui=false",
        ):
            self.argv.extend(["-c", setting])
            if not setting.startswith("core.attributesFile="):
                self.metadata_argv.extend(["-c", setting])
        # Do not inherit credentials, Git overrides, dynamic-loader variables or
        # shell startup settings into these fixed, local Git operations.
        self.env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP")
                    if key in os.environ}
        self.env.update({
            "LC_ALL": "C", "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
        })
        # Attribute/ignore lookup and fixed-key configuration reads need the
        # user's normal Git configuration to agree with their checkout. These
        # builtins read metadata; they do not run conversion programs or helpers.
        self.metadata_env = dict(self.env)
        for key in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
            self.metadata_env.pop(key)
        for key in ("HOME", "XDG_CONFIG_HOME", "USERPROFILE", "PROGRAMDATA"):
            if key in os.environ:
                self.metadata_env[key] = os.environ[key]

    def run(self, *args, data=None, allowed=(0,), configuration=False):
        check_cancel(self.signal)
        if configuration and args[0] not in {"config", "check-attr", "ls-files"}:
            raise GitReadError("Effective configuration is limited to fixed metadata operations.")
        try:
            proc = subprocess.Popen(
                [*(self.metadata_argv if configuration else self.argv), *args], cwd=self.workspace,
                env=self.metadata_env if configuration else self.env, shell=False,
                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as error:
            raise GitReadError(f"Cannot start controlled Git operation: {error}") from error
        deadline = time.monotonic() + 30
        output, errors = bytearray(), bytearray()
        total = 0
        lock = threading.Lock()
        changed, exceeded, stopping = threading.Event(), threading.Event(), threading.Event()
        failures, workers = [], []

        def read(stream, destination, diagnostic=False):
            nonlocal total
            try:
                with stream:
                    while not stopping.is_set():
                        chunk = stream.read(64 * 1024)
                        if not chunk:
                            break
                        with lock:
                            total += len(chunk)
                            if total > MAX_GIT_BYTES:
                                exceeded.set()
                                break
                            # Count all stderr against the same budget while
                            # retaining only the prefix needed for diagnostics.
                            destination.extend(chunk[:max(0, 4096 - len(destination))] if diagnostic else chunk)
            except Exception as error:
                failures.append(error)
            finally:
                changed.set()

        def write():
            try:
                with proc.stdin:
                    remaining = memoryview(data)
                    while remaining and not stopping.is_set():
                        written = proc.stdin.write(remaining)
                        if not written:
                            raise BrokenPipeError("Controlled Git stdin stopped accepting data")
                        remaining = remaining[written:]
            except BrokenPipeError:
                pass  # A Git error can close stdin before consuming all input.
            except OSError as error:
                # Windows pipes can report EINVAL when the child closes stdin.
                if os.name != "nt" or error.errno != errno.EINVAL:
                    failures.append(error)
            except Exception as error:
                failures.append(error)
            finally:
                changed.set()

        try:
            # Pipes must be drained concurrently on Windows as well as POSIX;
            # communicate() buffers entire streams before its caller can cap them.
            operations = [(read, (proc.stdout, output)), (read, (proc.stderr, errors, True))]
            if data is not None:
                operations.append((write, ()))
            for target, arguments in operations:
                worker = threading.Thread(target=target, args=arguments, daemon=True,
                                          name=f"virgo-git-{proc.pid}-{len(workers)}")
                worker.start()
                workers.append(worker)
            while True:
                # Once every worker has stopped, its terminal flags are final.
                # Check those flags after the liveness snapshot, never before it.
                finished = all(not worker.is_alive() for worker in workers) and proc.poll() is not None
                check_cancel(self.signal)
                if exceeded.is_set():
                    raise GitReadError("Git output exceeds the controlled read limit.", "incomplete")
                if failures:
                    raise GitReadError(f"Controlled Git pipe I/O failed: {failures[0]}") from failures[0]
                if time.monotonic() >= deadline:
                    raise GitReadError("Controlled Git operation timed out.", "incomplete")
                if finished:
                    break
                changed.wait(0.05)
                changed.clear()
            check_cancel(self.signal)
            if proc.returncode not in allowed:
                reason = errors.decode("utf-8", errors="replace").strip()[:800]
                raise GitReadError(f"Git {args[0]} failed: {reason or proc.returncode}")
            return bytes(output)
        finally:
            primary_error = sys.exception()
            cleanup_error = None
            stopping.set()
            try:
                if proc.poll() is None:
                    kill_tree(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception as error:
                cleanup_error = error
            finally:
                # Descendants may retain pipe ends after Git exits. Raw pipes
                # can be closed without waiting on a buffered reader's lock.
                for worker in workers:
                    worker.join(timeout=2)
                unfinished = any(worker.is_alive() for worker in workers)
                if unfinished and cleanup_error is None:
                    cleanup_error = GitReadError("Controlled Git pipe cleanup did not finish.", "incomplete")
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError) as error:
                            if cleanup_error is None:
                                cleanup_error = error
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                primary_error.add_note(f"Controlled Git cleanup: {cleanup_error}")

    def commit(self, ref, *, optional=False):
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in "\x00\r\n"):
            raise GitReadError("A non-option Git revision is required.")
        value = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}",
                         allowed=(0, 1, 128) if optional else (0,)).decode().strip()
        if not value and not optional:
            raise GitReadError(f"Cannot resolve commit: {ref}")
        return value or None

    def branch(self, ref):
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in "\x00\r\n"):
            raise GitReadError("A local or remote branch name is required.")
        refs = self.run("for-each-ref", "--format=%(refname)", "refs/heads/", "refs/remotes/", "refs/tags/")
        names = refs.decode("utf-8").splitlines()
        candidates = ([ref] if ref.startswith("refs/") else
                      [f"refs/heads/{ref}", f"refs/remotes/{ref}", f"refs/tags/{ref}"])
        matches = [name for name in candidates if name in names]
        if len(matches) != 1 or not matches[0].startswith(("refs/heads/", "refs/remotes/")):
            raise GitReadError("Base must name one unambiguous local or remote branch; use its full ref.")
        return self.commit(matches[0])

    def config(self, name, *, boolean=False):
        options = ("--type=bool",) if boolean else ()
        return self.run("config", *options, "--get", name, allowed=(0, 1), configuration=True).decode("utf-8").strip().lower()


def _execute(args, context):
    try:
        if context.origin is None:
            raise GitReadError("Git reads require the originating runtime context.")
        workspace = Path(context.origin.workspace).resolve()
        git = ControlledGit(workspace, context.signal)
        root = Path(git.run("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        if root != workspace:
            raise GitReadError("Git reads require the repository-root workspace; a child cannot widen it.")
        revision = git.commit(args.get("revision", "HEAD"))
        operation = args["operation"]
        if operation == "log":
            limit = args.get("limit", 20)
            raw = git.run("log", f"--max-count={limit}", "--format=%h %s", revision, "--")
        elif operation == "show":
            raw = git.run("show", "--format=fuller", "--stat", "--patch", "--no-ext-diff",
                          "--no-textconv", "--no-renames", revision, "--")
        elif operation == "diff":
            base = git.commit(args.get("base"))
            raw = git.run("diff-tree", "-r", "--no-commit-id", "--patch", "--stat", "--no-ext-diff",
                          "--no-textconv", "--no-renames", base, revision, "--")
        else:
            raise GitReadError("Git reads support log, show, or diff of committed objects only.")
        truncated = len(raw) > MAX_RESULT_BYTES
        text = raw[:MAX_RESULT_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[Git output truncated at 45 KiB.]"
        return ToolResult.text(text or "No committed changes.", details={"operation": operation,
                               "revision": revision, "truncated": truncated})
    except (GitReadError, OSError, UnicodeError) as error:
        return ToolResult.error(str(error))


GIT_READ = AgentTool(
    "git_read",
    "Read local committed Git history or patches. Supports log, show of a commit, and diff between "
    "two commits; revisions are resolved to fixed objects. No live-worktree diff/status, commands, "
    "network, repository helpers, or mutations are available.",
    {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["log", "show", "diff"]},
        "revision": {"type": "string", "description": "Commit revision; defaults to HEAD."},
        "base": {"type": "string", "description": "Required old commit revision for diff."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                  "description": "Maximum log entries; defaults to 20."},
    }, "required": ["operation"], "additionalProperties": False},
    _execute, label="Read Git", side_effect="read",
)

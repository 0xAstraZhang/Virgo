"""Narrow Layer-3 host protocols preventing sibling cycles inside ``virgo.harness``.

This leaf module imports no sibling modules from ``virgo.harness`` so harness
subsystems can type their exact required capabilities without importing
``AgentSession`` or ``Runtime`` (even under ``TYPE_CHECKING``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class BackgroundLauncher(Protocol):
    """Minimal background command launch capability consumed by shell tools."""

    def start_command(
        self,
        command: str,
        cwd: Path,
        timeout: float | None = None,
        *,
        shell: Any = None,
        store: Any = None,
        agent: str = "main",
        tool_call_id: str | None = None,
        execution_context: Any = None,
        cancellation: Any = None,
    ) -> Any: ...


class PromptRuntime(Protocol):
    """Minimal runtime view consumed by system prompt assembly."""

    execution_context: Any


class PromptHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.prompt``."""

    cwd: Path
    paths: Any
    ui: Any
    sections: list
    runtime_sections: list
    services: dict

    def tools_for(self, role: str) -> list[Any]: ...
    def runtime_for_store(self, store: Any) -> Any: ...
    def report_error(self, error: Any, *, phase: str, **kwargs: Any) -> dict: ...


class RuntimeHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.runtime``."""

    cwd: Path
    model: Any
    settings: Any
    translator: Any
    ui: Any
    bus: Any
    store: Any
    services: dict
    request_sections: list
    request_preparers: list
    request_fallbacks: list
    io_bridge: Any

    def t(self, key: str, /, **values: object) -> str: ...
    def resolve(self, spec: str) -> Any: ...
    def tool_snapshot(self, role: str) -> tuple[int, list[Any]]: ...
    def tools_for(self, role: str) -> list[Any]: ...
    def report_error(self, error: Any, *, phase: str, **kwargs: Any) -> dict: ...
    def report_save_error(self, error: Any) -> None: ...
    def diagnostic_query_tool_names(self) -> frozenset[str]: ...
    def complete(self, *args: Any, **kwargs: Any) -> Any: ...
    def _before_tool(self, ev: Any) -> Any: ...
    def _before_tool_execute(self, ev: Any) -> Any: ...
    def _after_tool(self, ev: Any, res: Any) -> Any: ...


class JobHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.jobs``."""

    state_lock: threading.RLock
    store: Any
    bus: Any

    def t(self, key: str, /, **values: object) -> str: ...
    def register_cleanup(self, owner: str, callback: Callable[[], None]) -> None: ...
    def report_error(self, error: Any, *, phase: str, **kwargs: Any) -> dict: ...
    def runtime_for_store(self, store: Any) -> Any: ...
    def reserve_child_work(self, store: Any) -> Callable[[], None]: ...
    def retain_diagnostics(self, store: Any, records: Any) -> None: ...
    def notify(
        self,
        text: str,
        mode: str = "steer",
        source: str | None = None,
        *,
        store: Any = None,
        diagnostics: Any = (),
    ) -> None: ...


class ExtensionHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.extensions``."""

    cwd: Path
    paths: Any
    settings: Any
    ui: Any
    bus: Any
    commands: Any
    services: dict
    auth: Any
    model: Any
    small_model: Any
    small_model_effort: str | None
    translator: Any
    state_lock: threading.RLock
    store: Any
    main: Any
    loaded_plugins: list[str]
    trust_project_extensions: bool
    runtime_sections: list
    request_sections: list
    request_preparers: list
    request_fallbacks: list

    def t(self, key: str, /, **values: object) -> str: ...
    def register_tool(self, tool: Any, roles: tuple = ("main",), *, owner: str = "host") -> None: ...
    def replace_tools(self, namespace: str, tools: Any, *, owner: str = "host") -> None: ...
    def remove_owned_tools(self, owner: str) -> None: ...
    def checkpoint_tools(self) -> Any: ...
    def restore_tools(self, checkpoint: Any) -> None: ...
    def register_cleanup(self, owner: str, callback: Callable[[], None]) -> None: ...
    def cleanup_plugins(self, *, owner: str | None = None) -> None: ...
    def declare_diagnostic_query_tool(self, owner: str, tool_name: str) -> Callable[[], None]: ...
    def runtime_for_context(self, ctx: Any) -> Any: ...
    def subagent_store_for(self, store: Any = None) -> Any: ...
    def create_runtime(self, name: str, role: str, *, parent: Any = None, **kwargs: Any) -> Any: ...
    def release_runtime(self, runtime: Any) -> None: ...
    def reserve_child_work(self, store: Any) -> Callable[[], None]: ...
    def tools_for(self, role: str) -> list[Any]: ...
    def resolve(self, spec: str) -> Any: ...
    def complete(self, *args: Any, **kwargs: Any) -> Any: ...
    def report_error(self, error: Any, *, phase: str, **kwargs: Any) -> dict: ...
    def retain_diagnostics(self, store: Any, records: Any) -> None: ...
    def notify(
        self,
        text: str,
        mode: str = "steer",
        source: str | None = None,
        *,
        store: Any = None,
        diagnostics: Any = (),
    ) -> None: ...
    def _before_tool(self, ev: Any) -> Any: ...
    def _before_tool_execute(self, ev: Any) -> Any: ...


class PreferenceHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.preferences``."""

    auth: Any
    settings: Any
    models: Any
    model: Any
    small_model: Any
    effort: str | None
    effort_override: str | None
    store: Any
    main: Any
    ui: Any

    def t(self, key: str, /, **values: object) -> str: ...
    def resolve(self, spec: str) -> Any: ...
    def effort_for(self, model: Any) -> str | None: ...
    def set_model(self, spec: str) -> None: ...
    def cached_model_catalog(self, connection: Any) -> list: ...
    def publish_model_catalog(
        self, connection: Any, models: list, owner: Any, *, revision: int | None = None
    ) -> int | None: ...
    def report_error(self, error: Any, *, phase: str, **kwargs: Any) -> dict: ...


class CommandHost(Protocol):
    """Narrow session contract consumed by ``virgo.harness.commands``."""

    busy: bool
    commands: Any
    diagnostics: Any
    effort: str | None
    main: Any
    model: Any
    small_model: Any
    store: Any
    templates: dict
    translator: Any
    ui: Any

    def t(self, key: str, /, **values: object) -> str: ...
    def compact(self, focus: str | None = None) -> dict | None: ...
    def context_tokens(self) -> int: ...
    def effort_for(self, model: Any) -> str | None: ...
    def list_sessions(self) -> list[dict]: ...
    def new_session(self) -> None: ...
    def open_session(self, path: str | Path) -> None: ...
    def register_tool(self, tool: Any, roles: tuple = ("main",), *, owner: str = "host") -> None: ...
    def runtime_for_context(self, ctx: Any) -> Any: ...
    def set_language(self, language: str, *, persist: bool = True) -> None: ...
    def set_model(self, spec: str) -> None: ...
    def status_snapshot(self) -> dict: ...

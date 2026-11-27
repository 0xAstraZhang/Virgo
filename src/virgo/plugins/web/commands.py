"""Shared web capability selection commands (/search and /fetch)."""

from __future__ import annotations

from collections.abc import Callable

from .service import web_service

_SEARCH_CHOICES = (
    ("harness.search.choice.auto", "auto"),
    ("harness.search.choice.duckduckgo", "duckduckgo"),
    ("harness.search.choice.deepseek", "deepseek"),
    ("harness.search.choice.off", "off"),
)
_SEARCH_ALIASES = {"ds": "deepseek", "ddg": "duckduckgo"}

_FETCH_CHOICES = (
    ("harness.fetch.choice.auto", "auto"),
    ("harness.fetch.choice.http", "http"),
    ("harness.fetch.choice.deepseek", "deepseek"),
    ("harness.fetch.choice.off", "off"),
)
_FETCH_ALIASES = {"ds": "deepseek"}


def _configure_web(
    api,
    args: str,
    *,
    service_name: str,
    choices: tuple[tuple[str, str], ...],
    aliases: dict[str, str],
    print_status: Callable[[object, str], None] | None = None,
) -> None:
    service = api.services.get(service_name)
    if service is None:
        api.ui.print(api.t(f"harness.{service_name}.disabled"), "error")
        return
    choice_map = {api.t(key): value for key, value in choices}
    valid_values = frozenset(choice_map.values())
    usage = api.t(f"harness.{service_name}.usage")
    choice = args.strip().lower()
    if not choice:
        if not api.ui.interactive:
            if print_status is not None:
                print_status(service, usage)
            else:
                api.ui.print(api.t(f"harness.{service_name}.current", selection=service.selection, usage=usage))
            return
        selected = api.ui.select(
            api.t(f"harness.{service_name}.picker", selection=service.selection),
            list(choice_map),
        )
        if selected is None:
            api.ui.print(api.t(f"harness.{service_name}.kept", selection=service.selection), "dim")
            return
        choice = choice_map.get(selected, "")
    choice = aliases.get(choice, choice)
    if choice not in valid_values:
        api.ui.print(usage, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        api.ui.print(api.t(f"harness.{service_name}.failed", error=error), "error")
        return
    api.ui.print(api.t(f"harness.{service_name}.saved", selection=service.selection), "ok")


def configure_search(api, args: str) -> None:
    _configure_web(
        api,
        args,
        service_name="search",
        choices=_SEARCH_CHOICES,
        aliases=_SEARCH_ALIASES,
    )


def configure_fetch(api, args: str) -> None:
    def print_status(service, usage: str) -> None:
        runtime = api.current_runtime() if hasattr(api, "current_runtime") else None
        paths = web_service(api).paths_for(runtime)
        detail = "\n" + api.t("harness.fetch.paths", paths=paths) if paths else ""
        api.ui.print(api.t("harness.fetch.current", selection=service.selection, detail=detail, usage=usage))

    _configure_web(
        api,
        args,
        service_name="fetch",
        choices=_FETCH_CHOICES,
        aliases=_FETCH_ALIASES,
        print_status=print_status,
    )
